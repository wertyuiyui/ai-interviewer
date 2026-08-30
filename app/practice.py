from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import ROOT_DIR, Settings, get_settings
from .content import COMPANIES
from .db import Database
from .errors import AppError, LLMError
from .llm import BailianChatClient


LanguageMode = Literal["zh", "bilingual", "en"]
InputMode = Literal["text", "voice"]
PracticeMode = Literal["quick", "review"]
GLOBAL_COMPANY_TAGS = {"all", "global", "global_tech", "overseas"}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_client_id(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
        raise ValueError("client_id 格式不正确")
    return value


class PracticeSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=8, max_length=128)
    mode: PracticeMode = "quick"
    company: str | None = Field(default=None, min_length=1, max_length=64)
    topic: str | None = Field(default=None, max_length=80)
    difficulty: Literal["easy", "medium", "hard", "discussion"] | None = None
    language_mode: LanguageMode = "zh"
    count: int = Field(default=5, ge=1, le=20, strict=True)
    source_interview_id: str | None = Field(default=None, max_length=64)
    review_score_lte: float = Field(default=6.0, ge=0, le=10)
    review_ordinals: list[int] = Field(default_factory=list, max_length=20)

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, value: str) -> str:
        return _clean_client_id(value)

    @field_validator("company", "topic")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.strip().split())
        return normalized or None

    @model_validator(mode="after")
    def validate_mode_fields(self) -> "PracticeSessionCreate":
        if self.mode == "review" and not self.source_interview_id:
            raise ValueError("错题重答必须提供 source_interview_id")
        if self.mode == "quick" and self.source_interview_id:
            raise ValueError("快速刷题不能提供 source_interview_id")
        if self.mode == "quick" and self.review_ordinals:
            raise ValueError("快速刷题不能指定面试题序")
        if any(ordinal < 1 for ordinal in self.review_ordinals):
            raise ValueError("面试题序必须从 1 开始")
        if len(set(self.review_ordinals)) != len(self.review_ordinals):
            raise ValueError("面试题序不能重复")
        return self


class PracticeAnswerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=8, max_length=128)
    question_id: str = Field(min_length=1, max_length=256)
    answer: str = Field(min_length=1, max_length=10000)
    input_mode: InputMode = "text"
    answer_duration_seconds: float | None = Field(default=None, ge=0, le=3600)
    reattempt: bool = False

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, value: str) -> str:
        return _clean_client_id(value)

    @field_validator("answer")
    @classmethod
    def normalize_answer(cls, value: str) -> str:
        normalized = value.replace("\x00", "").strip()
        if not normalized:
            raise ValueError("回答不能为空")
        return normalized


class PracticeHintCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=8, max_length=128)
    question_id: str = Field(min_length=1, max_length=256)

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, value: str) -> str:
        return _clean_client_id(value)


class PracticeAssessment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    score: float | None = Field(default=None, ge=0, le=10)
    scorable: bool = True
    status: Literal["scored", "unscored"] = "scored"
    evidence: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    deductions: list[str] = Field(default_factory=list)
    better_answer: str = Field(min_length=1, max_length=6000)
    key_points: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def keep_missing_score_explicit(self) -> "PracticeAssessment":
        if self.score is None or not self.scorable:
            self.score = None
            self.scorable = False
            self.status = "unscored"
        return self


@dataclass(frozen=True, slots=True)
class RealQuestion:
    id: str
    company: str | None
    companies: tuple[str, ...]
    category: str
    topic: str
    question: str
    followups: tuple[str, ...]
    difficulty: str
    language: str
    provenance: dict[str, Any]
    recommended_answer_seconds: int

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "company": self.company,
            "company_tags": list(self.companies),
            "category": self.category,
            "topic": self.topic,
            "question": self.question,
            "followups": list(self.followups),
            "difficulty": self.difficulty,
            "language": self.language,
            "provenance": self.provenance,
            "recommended_answer_seconds": self.recommended_answer_seconds,
        }


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def _recommended_seconds(question: str, difficulty: str) -> int:
    if difficulty in {"hard", "discussion"} or any(
        marker in question.lower()
        for marker in ("设计", "排查", "trade-off", "system design", "debug")
    ):
        return 120
    if difficulty == "easy":
        return 45
    return 75


def _applies_to_company(question: RealQuestion, company: str | None) -> bool:
    if not company:
        return True
    if question.company:
        return question.company == company
    if not question.companies:
        return True
    return company in question.companies or bool(
        GLOBAL_COMPANY_TAGS.intersection(question.companies)
    )


def _provenance_for(
    raw: dict[str, Any], root: dict[str, Any], path: Path
) -> dict[str, Any]:
    """Build private traceability metadata without exposing it in API output."""

    result: dict[str, Any] = {"question_file": path.name}
    for key in (
        "source_ids",
        "source_id",
        "source_ref",
        "source_url",
        "source_title",
        "source_path",
        "revision",
        "license",
        "authenticity",
        "status",
        "evidence_refs",
        "upstream_url",
    ):
        value = raw.get(key)
        if value:
            result[key] = value
    attribution = root.get("attribution")
    if isinstance(attribution, dict) and attribution:
        result["attribution"] = attribution
    provenance = raw.get("provenance")
    if isinstance(provenance, dict) and provenance:
        result["provenance"] = provenance
    return result


def _is_traceable(provenance: dict[str, Any]) -> bool:
    # A local filename alone is insufficient evidence that a question came
    # from a real public source. Every quick-drill item must carry a stable
    # upstream identifier, URL/path reference, or file-level attribution.
    return any(key != "question_file" for key in provenance)


def _question_groups(root: dict[str, Any]) -> list[tuple[str | None, list[Any]]]:
    groups: list[tuple[str | None, list[Any]]] = []
    top_questions = root.get("questions")
    if isinstance(top_questions, list):
        default_company = str(root.get("company") or "").strip() or None
        groups.append((default_company, top_questions))
    companies = root.get("companies")
    if isinstance(companies, dict):
        for company, values in companies.items():
            if isinstance(values, list):
                groups.append((str(company), values))
    return groups


def load_real_question_bank(question_dir: Path | None = None) -> list[RealQuestion]:
    """Load only authored questions with auditable upstream provenance.

    This loader deliberately never asks an LLM to invent missing questions.
    It accepts both the project's top-level ``questions`` format and the
    company-keyed interview-experience format so newly distilled banks can be
    added without changing application code.
    """

    directory = question_dir or ROOT_DIR / "questions"
    strict_reviewed_bank = question_dir is None
    paths = (
        sorted(directory.glob("*.json"))
        if question_dir is not None
        else [directory / "real_practice_bank.json"]
    )
    seen: set[str] = set()
    result: list[RealQuestion] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            root = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(root, dict):
            continue
        for group_company, values in _question_groups(root):
            for value in values:
                if not isinstance(value, dict):
                    continue
                base_id = str(value.get("id") or "").strip()
                if not base_id:
                    continue
                provenance = _provenance_for(value, root, path)
                if not _is_traceable(provenance):
                    continue
                if strict_reviewed_bank and not all(
                    str(value.get(key) or "").strip()
                    for key in ("source_id", "source_path", "revision", "license")
                ):
                    continue
                if strict_reviewed_bank and (
                    value.get("status") != "approved"
                    or value.get("authenticity") != "licensed_bank"
                ):
                    continue
                if value.get("status") and value.get("status") != "approved":
                    continue
                if value.get("authenticity") and value.get("authenticity") != "licensed_bank":
                    continue
                if value.get("authenticity") == "licensed_bank" and any(
                    not str(value.get(key) or "").strip()
                    for key in ("source_id", "source_path", "revision", "license")
                ):
                    continue
                difficulty = str(value.get("difficulty") or "medium").lower()
                if difficulty not in {"easy", "medium", "hard", "discussion"}:
                    difficulty = "medium"
                followups = tuple(
                    str(item).strip()
                    for item in (value.get("followups") or [])
                    if str(item).strip()
                )
                company = str(value.get("company") or group_company or "").strip()
                company_tags = tuple(
                    str(item).strip()
                    for item in (value.get("company_tags") or [])
                    if str(item).strip()
                )
                topics = [
                    str(item).strip()
                    for item in (value.get("topics") or [])
                    if str(item).strip()
                ]
                topic = str(
                    value.get("topic")
                    or (" / ".join(topics) if topics else "")
                    or value.get("category")
                    or "综合基础"
                ).strip()
                category = str(
                    value.get("category")
                    or (topics[0] if topics else "")
                    or value.get("kind")
                    or "综合基础"
                ).strip()
                raw_prompt = value.get("prompt")
                variants: list[tuple[str, str, str]] = []
                if isinstance(raw_prompt, dict):
                    for language in ("zh", "en"):
                        question = str(raw_prompt.get(language) or "").strip()
                        if question:
                            variants.append((f"{base_id}:{language}", language, question))
                else:
                    question = str(value.get("question") or "").strip()
                    language = str(
                        value.get("language")
                        or value.get("language_mode")
                        or root.get("language")
                        or ""
                    ).strip().lower()
                    if language not in {"zh", "en"}:
                        language = "zh" if _has_cjk(question) else "en"
                    if question:
                        variants.append((base_id, language, question))
                for question_id, language, question in variants:
                    if question_id in seen:
                        continue
                    scoring = value.get("scoring")
                    private_provenance = dict(provenance)
                    if isinstance(scoring, dict):
                        private_provenance["scoring"] = scoring
                    result.append(
                        RealQuestion(
                            id=question_id,
                            company=company or None,
                            companies=company_tags,
                            category=category,
                            topic=topic,
                            question=question,
                            followups=followups,
                            difficulty=difficulty,
                            language=language,
                            provenance=private_provenance,
                            recommended_answer_seconds=int(
                                value.get("suggested_seconds")
                                or value.get("recommended_answer_seconds")
                                or _recommended_seconds(question, difficulty)
                            ),
                        )
                    )
                    seen.add(question_id)
    return result


PRACTICE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS practice_sessions (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    company TEXT,
    topic TEXT,
    difficulty TEXT,
    language_mode TEXT NOT NULL,
    source_interview_id TEXT,
    questions_json TEXT NOT NULL,
    hint_events_json TEXT NOT NULL DEFAULT '[]',
    current_index INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_practice_sessions_client_created
    ON practice_sessions(client_id, created_at DESC);

CREATE TABLE IF NOT EXISTS practice_attempts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES practice_sessions(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    question_snapshot_json TEXT NOT NULL,
    answer TEXT NOT NULL,
    input_mode TEXT NOT NULL,
    answer_duration_seconds REAL,
    assessment_json TEXT NOT NULL,
    hint_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_practice_attempts_session
    ON practice_attempts(session_id, created_at);
"""


class PracticeService:
    def __init__(
        self,
        db: Database,
        settings: Settings | None = None,
        client: BailianChatClient | None = None,
        *,
        question_dir: Path | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.client = client or BailianChatClient(self.settings)
        self.question_dir = question_dir

    async def initialize(self) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.executescript(PRACTICE_SCHEMA_SQL)
            session_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(practice_sessions)"
                ).fetchall()
            }
            if "hint_events_json" not in session_columns:
                connection.execute(
                    "ALTER TABLE practice_sessions ADD COLUMN hint_events_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            attempt_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(practice_attempts)"
                ).fetchall()
            }
            if "hint_used" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE practice_attempts ADD COLUMN hint_used "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            connection.commit()

        await self.db._run(operation)

    async def catalog(self) -> dict[str, Any]:
        bank = load_real_question_bank(self.question_dir)
        approved_question_count = len(
            {
                re.sub(r":(?:zh|en)$", "", item.id)
                for item in bank
            }
        )
        companies = [
            company
            for company in COMPANIES
            if any(_applies_to_company(item, company) for item in bank)
        ]
        return {
            "question_count": approved_question_count,
            "approved_question_count": approved_question_count,
            "companies": [
                {
                    "id": company,
                    "name": COMPANIES.get(str(company), str(company)),
                    "question_count": len(
                        {
                            re.sub(r":(?:zh|en)$", "", item.id)
                            for item in bank
                            if _applies_to_company(item, company)
                        }
                    ),
                }
                for company in companies
            ],
            "topics": sorted({item.topic for item in bank}),
            "difficulties": ["easy", "medium", "hard", "discussion"],
            "language_modes": ["zh", "bilingual", "en"],
        }

    async def create_session(self, request: PracticeSessionCreate) -> dict[str, Any]:
        if request.company and request.company not in COMPANIES:
            raise AppError("INVALID_COMPANY", "暂不支持该公司", status_code=422)
        session_id = uuid.uuid4().hex
        if request.mode == "review":
            questions = await self._review_questions(request)
        else:
            questions = self._quick_questions(request, session_id)
        if not questions:
            message = (
                "这场面试没有符合条件的低分题"
                if request.mode == "review"
                else "真实题库中没有符合当前筛选条件的题目"
            )
            raise AppError("PRACTICE_QUESTIONS_EMPTY", message, status_code=404)
        language_mode = request.language_mode
        if request.mode == "review" and questions:
            original_language = str(questions[0].get("language") or "").strip()
            if original_language in {"zh", "bilingual", "en"}:
                language_mode = original_language
        values = (
            session_id,
            request.client_id,
            request.mode,
            request.company,
            request.topic,
            request.difficulty,
            language_mode,
            request.source_interview_id,
            json.dumps(questions, ensure_ascii=False),
            _utc_iso(),
        )

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO practice_sessions (
                    id, client_id, mode, company, topic, difficulty,
                    language_mode, source_interview_id, questions_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.commit()

        await self.db._run(operation)
        session = await self._require_session(session_id, request.client_id)
        return self._public_session(session)

    def _quick_questions(
        self, request: PracticeSessionCreate, session_id: str
    ) -> list[dict[str, Any]]:
        bank = load_real_question_bank(self.question_dir)
        candidates = [
            item
            for item in bank
            if _applies_to_company(item, request.company)
            and (
                not request.topic
                or request.topic.casefold() in item.topic.casefold()
                or request.topic.casefold() in item.category.casefold()
            )
            and (not request.difficulty or item.difficulty == request.difficulty)
            and (
                request.language_mode == "bilingual"
                or item.language == request.language_mode
            )
        ]
        if request.language_mode == "bilingual":
            grouped: dict[str, dict[str, RealQuestion]] = {}
            for item in candidates:
                base_id = re.sub(r":(?:zh|en)$", "", item.id)
                grouped.setdefault(base_id, {})[item.language] = item
            snapshots: list[dict[str, Any]] = []
            for base_id, variants in grouped.items():
                primary = variants.get("zh") or variants.get("en")
                if primary is None:
                    continue
                snapshot = primary.snapshot()
                zh = variants.get("zh")
                en = variants.get("en")
                if zh and en:
                    snapshot.update(
                        id=f"{base_id}:bilingual",
                        language="bilingual",
                        question=f"{zh.question}\n\nEnglish: {en.question}",
                    )
                snapshots.append(snapshot)
        else:
            snapshots = [item.snapshot() for item in candidates]
        # Stable pseudo-random order makes a session retry/debuggable without
        # always presenting file order to every candidate.
        snapshots.sort(
            key=lambda item: hashlib.sha256(
                f"{session_id}:{item['id']}".encode("utf-8")
            ).hexdigest()
        )
        return snapshots[: request.count]

    async def _review_questions(
        self, request: PracticeSessionCreate
    ) -> list[dict[str, Any]]:
        assert request.source_interview_id is not None
        interview = await self.db.get_interview(request.source_interview_id)
        if not interview:
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
        if interview["client_id"] != request.client_id:
            raise AppError("PRACTICE_FORBIDDEN", "不能访问其他设备的面试记录", status_code=403)
        if interview["status"] in {"created", "active"}:
            raise AppError("INTERVIEW_NOT_FINISHED", "面试结束后才能重答错题", status_code=409)
        turns = await self.db.list_turns(request.source_interview_id)
        requested_ordinals = set(request.review_ordinals)
        if requested_ordinals:
            selected = [turn for turn in turns if turn.ordinal in requested_ordinals]
            missing = requested_ordinals - {turn.ordinal for turn in selected}
            if missing:
                raise AppError(
                    "TURN_NOT_FOUND",
                    f"找不到第 {min(missing)} 题",
                    status_code=404,
                )
            selected.sort(key=lambda turn: request.review_ordinals.index(turn.ordinal))
        else:
            selected = [
                turn
                for turn in turns
                if turn.scorable
                and turn.score is not None
                and (turn.failed or turn.score <= request.review_score_lte)
            ][: request.count]
        return [
            {
                "id": f"review-{request.source_interview_id}-{turn.ordinal}",
                "company": interview["company"],
                "category": turn.category,
                "topic": turn.topic,
                "question": turn.question,
                "followups": [],
                "difficulty": "review",
                "language": interview.get("language_mode") or "zh",
                "provenance": {
                    "origin": "interview_turn",
                    "interview_id": request.source_interview_id,
                    "ordinal": turn.ordinal,
                },
                "recommended_answer_seconds": turn.recommended_answer_seconds,
                "previous_score": turn.score,
                "previous_deductions": turn.deductions,
            }
            for turn in selected
        ]

    async def get_session(self, session_id: str, client_id: str) -> dict[str, Any]:
        session = await self._require_session(session_id, _clean_client_id(client_id))
        return self._public_session(session)

    async def submit_answer(
        self, session_id: str, request: PracticeAnswerCreate
    ) -> dict[str, Any]:
        session = await self._require_session(session_id, request.client_id)
        questions = session["questions"]
        index = int(session["current_index"])
        expected = questions[index] if index < len(questions) else None
        retry_question = next(
            (item for item in questions if item["id"] == request.question_id), None
        )
        already_attempted = any(
            attempt["question_id"] == request.question_id
            for attempt in session["attempts"]
        )
        is_reattempt = (
            request.reattempt and retry_question is not None and already_attempted
        )
        if session["status"] != "active" and not is_reattempt:
            raise AppError("PRACTICE_FINISHED", "本组题目已经完成", status_code=409)
        if not is_reattempt and (
            expected is None or request.question_id != expected["id"]
        ):
            raise AppError(
                "PRACTICE_QUESTION_MISMATCH",
                "提交的题目不是当前待答题目，请刷新后重试",
                status_code=409,
            )
        question = retry_question if is_reattempt else expected
        assert question is not None
        assessment = await self._assess(
            question=question,
            answer=request.answer,
            language_mode=session["language_mode"],
        )
        attempt_id = uuid.uuid4().hex
        created_at = _utc_iso()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT * FROM practice_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row or str(row["client_id"]) != request.client_id:
                raise LookupError("session_not_found")
            current_index = int(row["current_index"])
            persisted_questions = json.loads(row["questions_json"])
            retry_current = next(
                (
                    item
                    for item in persisted_questions
                    if str(item.get("id")) == request.question_id
                ),
                None,
            )
            prior_attempt = connection.execute(
                "SELECT 1 FROM practice_attempts WHERE session_id = ? AND question_id = ? LIMIT 1",
                (session_id, request.question_id),
            ).fetchone()
            reattempt = bool(
                request.reattempt
                and retry_current is not None
                and prior_attempt is not None
            )
            if not reattempt:
                if row["status"] != "active" or current_index >= len(persisted_questions):
                    raise RuntimeError("session_finished")
                current = persisted_questions[current_index]
                if str(current.get("id")) != request.question_id:
                    raise RuntimeError("question_mismatch")
            else:
                current = retry_current
            try:
                hint_events = json.loads(row["hint_events_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                hint_events = []
            hint_used = any(
                isinstance(event, dict)
                and event.get("question_id") == request.question_id
                for event in hint_events
            )
            connection.execute(
                """
                INSERT INTO practice_attempts (
                    id, session_id, question_id, question_snapshot_json,
                    answer, input_mode, answer_duration_seconds,
                    assessment_json, hint_used, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    session_id,
                    request.question_id,
                    json.dumps(current, ensure_ascii=False),
                    request.answer,
                    request.input_mode,
                    request.answer_duration_seconds,
                    assessment.model_dump_json(),
                    int(hint_used),
                    created_at,
                ),
            )
            next_index = current_index if reattempt else current_index + 1
            done = next_index >= len(persisted_questions)
            if not reattempt:
                connection.execute(
                    """
                    UPDATE practice_sessions
                    SET current_index = ?, status = ?, ended_at = ?
                    WHERE id = ?
                    """,
                    (next_index, "completed" if done else "active", created_at if done else None, session_id),
                )
            connection.commit()
            return {
                "id": attempt_id,
                "session_id": session_id,
                "question": current,
                "answer": request.answer,
                "input_mode": request.input_mode,
                "answer_duration_seconds": request.answer_duration_seconds,
                "assessment": assessment.model_dump(),
                "hint_used": hint_used,
                "reattempt": reattempt,
                "created_at": created_at,
                "done": done if not reattempt else row["status"] == "completed",
                "next_question": None if done else persisted_questions[next_index],
            }

        try:
            result = await self.db._run(operation)
        except LookupError as exc:
            raise AppError("PRACTICE_NOT_FOUND", "刷题记录不存在", status_code=404) from exc
        except RuntimeError as exc:
            code = "PRACTICE_FINISHED" if str(exc) == "session_finished" else "PRACTICE_QUESTION_MISMATCH"
            raise AppError(code, "题目状态已经变化，请刷新后重试", status_code=409) from exc
        result["question"] = self._public_question(result["question"])
        if result["next_question"]:
            result["next_question"] = self._public_question(result["next_question"])
        return result

    async def hint(
        self, session_id: str, request: PracticeHintCreate
    ) -> dict[str, Any]:
        session = await self._require_session(session_id, request.client_id)
        question = next(
            (
                item
                for item in session["questions"]
                if item["id"] == request.question_id
            ),
            None,
        )
        if not question:
            raise AppError("PRACTICE_QUESTION_MISMATCH", "题目不属于本组练习", status_code=409)
        followups = question.get("followups") or []
        if followups:
            hint = f"可以先想一想：{followups[0]}"
        elif session["language_mode"] == "en":
            hint = "Structure it as: core mechanism, one concrete example, then limits and trade-offs."
        else:
            hint = "可以按“核心机制—具体场景—边界与取舍”的顺序组织回答。"
        event = {
            "question_id": request.question_id,
            "hint": hint,
            "created_at": _utc_iso(),
        }

        def operation(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                "SELECT hint_events_json FROM practice_sessions WHERE id = ? AND client_id = ?",
                (session_id, request.client_id),
            ).fetchone()
            if not row:
                raise LookupError("session_not_found")
            try:
                events = json.loads(row["hint_events_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                events = []
            events.append(event)
            connection.execute(
                "UPDATE practice_sessions SET hint_events_json = ? WHERE id = ?",
                (json.dumps(events, ensure_ascii=False), session_id),
            )
            connection.commit()
            return len(events)

        try:
            hint_count = await self.db._run(operation)
        except LookupError as exc:
            raise AppError("PRACTICE_NOT_FOUND", "刷题记录不存在", status_code=404) from exc
        return {**event, "hint_count": hint_count}

    async def history(self, client_id: str, limit: int = 20) -> list[dict[str, Any]]:
        normalized = _clean_client_id(client_id)

        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                """
                SELECT * FROM practice_sessions
                WHERE client_id = ? ORDER BY created_at DESC LIMIT ?
                """,
                (normalized, limit),
            ).fetchall()
            return [self._decode_session(row, connection) for row in rows]

        sessions = await self.db._run(operation)
        return [self._public_session(session) for session in sessions]

    async def owns_session(self, session_id: str, client_id: str) -> bool:
        try:
            await self._require_session(session_id, _clean_client_id(client_id))
            return True
        except (AppError, ValueError):
            return False

    async def _require_session(self, session_id: str, client_id: str) -> dict[str, Any]:
        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM practice_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            return self._decode_session(row, connection)

        session = await self.db._run(operation)
        if not session:
            raise AppError("PRACTICE_NOT_FOUND", "刷题记录不存在", status_code=404)
        if session["client_id"] != client_id:
            raise AppError("PRACTICE_FORBIDDEN", "不能访问其他设备的刷题记录", status_code=403)
        return session

    @staticmethod
    def _decode_session(
        row: sqlite3.Row, connection: sqlite3.Connection
    ) -> dict[str, Any]:
        data = dict(row)
        data["questions"] = json.loads(data.pop("questions_json"))
        try:
            hints = json.loads(data.pop("hint_events_json", "[]") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            hints = []
        data["hint_events"] = hints if isinstance(hints, list) else []
        attempts = connection.execute(
            "SELECT * FROM practice_attempts WHERE session_id = ? ORDER BY created_at",
            (data["id"],),
        ).fetchall()
        data["attempts"] = [
            {
                "id": attempt["id"],
                "question_id": attempt["question_id"],
                "question": json.loads(attempt["question_snapshot_json"]),
                "answer": attempt["answer"],
                "input_mode": attempt["input_mode"],
                "answer_duration_seconds": attempt["answer_duration_seconds"],
                "assessment": json.loads(attempt["assessment_json"]),
                "hint_used": bool(attempt["hint_used"]),
                "created_at": attempt["created_at"],
            }
            for attempt in attempts
        ]
        return data

    @classmethod
    def _public_question(cls, question: dict[str, Any]) -> dict[str, Any]:
        # Provenance remains persisted for audits, but is intentionally not
        # rendered in the learner UI or returned by the public API.
        allowed = {
            "id",
            "company",
            "category",
            "topic",
            "question",
            "difficulty",
            "language",
            "recommended_answer_seconds",
            "previous_score",
            "previous_deductions",
        }
        return {key: question[key] for key in allowed if key in question}

    @classmethod
    def _public_session(cls, session: dict[str, Any]) -> dict[str, Any]:
        questions = session["questions"]
        index = int(session["current_index"])
        current = questions[index] if index < len(questions) else None
        numeric_scores = [
            float(attempt["assessment"]["score"])
            for attempt in session["attempts"]
            if isinstance(attempt["assessment"].get("score"), (int, float))
        ]
        question_stats: list[dict[str, Any]] = []
        for question in questions:
            question_attempts = [
                attempt
                for attempt in session["attempts"]
                if attempt["question_id"] == question["id"]
            ]
            scores = [
                float(attempt["assessment"]["score"])
                for attempt in question_attempts
                if isinstance(attempt["assessment"].get("score"), (int, float))
            ]
            question_stats.append(
                {
                    "question_id": question["id"],
                    "attempt_count": len(question_attempts),
                    "best_score": max(scores) if scores else None,
                    "latest_score": scores[-1] if scores else None,
                }
            )
        return {
            "id": session["id"],
            "client_id": session["client_id"],
            "mode": session["mode"],
            "company": session["company"],
            "topic": session["topic"],
            "difficulty": session["difficulty"],
            "language_mode": session["language_mode"],
            "source_interview_id": session["source_interview_id"],
            "status": session["status"],
            "total_questions": len(questions),
            "answered_questions": min(index, len(questions)),
            "attempt_count": len(session["attempts"]),
            "current_question": cls._public_question(current) if current else None,
            "attempts": [
                {
                    **{key: value for key, value in attempt.items() if key != "question"},
                    "question": cls._public_question(attempt["question"]),
                }
                for attempt in session["attempts"]
            ],
            "hint_count": len(session["hint_events"]),
            "best_score": max(numeric_scores) if numeric_scores else None,
            "latest_score": numeric_scores[-1] if numeric_scores else None,
            "question_stats": question_stats,
            "created_at": session["created_at"],
            "ended_at": session["ended_at"],
        }

    async def _assess(
        self, *, question: dict[str, Any], answer: str, language_mode: str
    ) -> PracticeAssessment:
        if self.settings.mock_llm:
            return self._mock_assessment(question, answer, language_mode)
        output_language = "English" if language_mode == "en" else "简体中文"
        private_scoring = (
            (question.get("provenance") or {}).get("scoring") or {}
        )
        payload = {
            "question": question["question"],
            "category": question.get("category"),
            "topic": question.get("topic"),
            "candidate_answer": answer,
            "followup_dimensions": question.get("followups") or [],
            "reviewed_key_points": private_scoring.get("key_points") or [],
            "reviewed_red_flags": private_scoring.get("red_flags") or [],
        }
        system = f"""
你是大厂后端实习技术面试的单题阅卷员。用 {output_language} 输出。
只评价候选人的回答，不因为语音或文字输入方式改变技术评分标准。
按 0-10 分评价：正确性 40%，原理与深度 30%，场景/边界/取舍 20%，表达 10%。
扣分必须指向回答中的缺失或错误；不要捏造候选人没说过的内容。
reviewed_key_points 和 reviewed_red_flags 来自已审核题库，仅用作评分依据，不向用户透露来源。
better_answer 给出适合本科实习面试、可以直接练习复述的示范回答。
若无法可靠评分，score=null、scorable=false、status=unscored，绝不填默认 5 分。
只输出符合 JSON Schema 的对象。不要输出或猜测题库来源。
""".strip()
        try:
            raw = await self.client.chat_json(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_schema=PracticeAssessment.model_json_schema(),
                schema_name="practice_assessment",
                model=self.settings.qwen_text_model,
                temperature=0.2,
                max_tokens=1800,
            )
            return PracticeAssessment.model_validate(raw)
        except (ValidationError, LLMError, AppError):
            return PracticeAssessment(
                score=None,
                scorable=False,
                status="unscored",
                evidence=[],
                deductions=["本次评分服务暂时不可用，回答已保存但不生成数值分。"],
                better_answer=(
                    "The answer was saved. Please retry scoring later."
                    if language_mode == "en"
                    else "回答已保存，请稍后重新提交评分。"
                ),
                key_points=[],
                next_steps=[],
            )

    @staticmethod
    def _mock_assessment(
        question: dict[str, Any], answer: str, language_mode: str
    ) -> PracticeAssessment:
        compact = re.sub(r"\s+", "", answer)
        admits_unknown = any(
            marker in answer.lower()
            for marker in ("不知道", "不会", "不清楚", "i don't know", "no idea")
        )
        if admits_unknown or len(compact) < 12:
            score = 2.5
            deductions = ["回答缺少可评分的原理、过程和边界条件。"]
        elif len(compact) < 55:
            score = 5.5
            deductions = ["给出了方向，但缺少关键机制、验证方法或取舍。"]
        elif len(compact) < 140:
            score = 7.0
            deductions = ["主体正确，可以补充失败场景和可观测指标。"]
        else:
            score = 8.5
            deductions = []
        private_scoring = (
            (question.get("provenance") or {}).get("scoring") or {}
        )
        reviewed_points = [
            str(item)
            for item in (private_scoring.get("key_points") or [])
            if str(item).strip()
        ]
        if language_mode == "en":
            better = (
                "Start with the core mechanism, walk through one concrete request or failure path, "
                "then close with observability signals and the trade-offs of your choice."
            )
            steps = ["Add one concrete example", "State a boundary case and a verification metric"]
        else:
            better = "；".join(reviewed_points) if reviewed_points else (
                f"先直接回答“{question.get('topic') or '核心结论'}”的机制，再用一个具体请求或故障链路展开，"
                "最后说明验证指标、边界条件和方案取舍。"
            )
            steps = ["补充一个具体场景", "说明边界条件和验证指标"]
        return PracticeAssessment(
            score=score,
            scorable=True,
            status="scored",
            evidence=[
                "回答长度与结构足以支持本次离线演示评分。"
            ],
            strengths=[] if score < 6 else ["回答包含了可继续追问的有效信息。"],
            deductions=deductions,
            better_answer=better,
            key_points=reviewed_points or ["核心机制", "具体场景", "边界与取舍"],
            next_steps=steps,
        )
