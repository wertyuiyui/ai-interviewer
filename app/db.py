from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from .config import Settings, get_settings
from .schemas import InterviewReport, InterviewTurn, ResumeData
from .topics import canonical_topic


T = TypeVar("T")


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    company TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'backend',
    interview_type TEXT NOT NULL DEFAULT 'technical',
    specialization TEXT NOT NULL DEFAULT '通用后端',
    language_mode TEXT NOT NULL DEFAULT 'bilingual',
    stress INTEGER NOT NULL DEFAULT 0,
    stress_level INTEGER NOT NULL DEFAULT 0,
    duration_minutes INTEGER,
    memory_enabled INTEGER NOT NULL DEFAULT 1,
    voice_mode TEXT NOT NULL,
    resume_json TEXT NOT NULL,
    style_json TEXT NOT NULL,
    weak_topics_json TEXT NOT NULL DEFAULT '[]',
    system_prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    breakdown_streak INTEGER NOT NULL DEFAULT 0,
    last_question TEXT NOT NULL DEFAULT '',
    hint_events_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    started_at REAL,
    deadline_at REAL,
    ended_at REAL,
    end_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_interviews_client_created
    ON interviews(client_id, created_at DESC);

CREATE TABLE IF NOT EXISTS interview_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    category TEXT NOT NULL,
    topic TEXT NOT NULL,
    score REAL NOT NULL,
    scorable INTEGER NOT NULL DEFAULT 1,
    score_source TEXT NOT NULL DEFAULT 'llm',
    deductions_json TEXT NOT NULL,
    failed INTEGER NOT NULL DEFAULT 0,
    drill_dimension TEXT NOT NULL DEFAULT '',
    drill_depth INTEGER NOT NULL DEFAULT 0,
    anchor_keyword TEXT NOT NULL DEFAULT '',
    input_mode TEXT NOT NULL DEFAULT 'text',
    answer_duration_seconds REAL,
    speech_rate_cpm REAL,
    transcript_edited INTEGER NOT NULL DEFAULT 0,
    original_answer TEXT NOT NULL DEFAULT '',
    recommended_answer_seconds INTEGER NOT NULL DEFAULT 60,
    created_at TEXT NOT NULL,
    UNIQUE(interview_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_turns_interview
    ON interview_turns(interview_id, ordinal);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL UNIQUE REFERENCES interviews(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    company TEXT NOT NULL,
    overall_score REAL NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_client_created
    ON reports(client_id, created_at DESC);
"""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_report_scoring(
    report: dict[str, Any],
    effective_turn_count: int,
    observed_dimensions: set[str] | None = None,
) -> dict[str, Any]:
    """Expose one unambiguous score state, including for legacy report JSON.

    Older zero-turn reports used the deterministic rubric's neutral 5.0
    defaults.  Deriving the state from persisted effective turns corrects those
    reports at read time without a destructive database migration.
    """

    normalized = dict(report)
    score_status = str(report.get("score_status") or "")
    insufficient = effective_turn_count <= 0 or score_status == "insufficient_data"
    if not insufficient:
        feedback = normalized.get("question_feedback") or []
        observed = (
            set()
            if score_status == "unscorable"
            else set(observed_dimensions or ())
        )
        if not observed and score_status != "unscorable":
            observed = {
                str(item.get("category"))
                for item in feedback
                if isinstance(item, dict)
                and item.get("answer", "").strip()
                and item.get("scorable", True)
                and isinstance(item.get("score"), (int, float))
            }
        # Schema 1.0 filled every missing rubric dimension with 5.0. Do not
        # infer that expression was observed merely because another category
        # had an answer; only 2.0 reports have evidence-derived communication.
        if observed and str(normalized.get("schema_version") or "1.0") == "2.0":
            observed.add("communication")
        rubric = normalized.get("rubric")
        weights = {
            "project_depth": 0.4,
            "fundamentals": 0.3,
            "coding_thought": 0.2,
            "communication": 0.1,
        }
        usable_weight = 0.0
        weighted_score = 0.0
        if isinstance(rubric, dict):
            for dimension, weight in weights.items():
                item = rubric.get(dimension)
                if not isinstance(item, dict):
                    item = {"weight": weight, "deductions": []}
                    rubric[dimension] = item
                raw_score = item.get("score")
                is_observed = (
                    dimension in observed
                    and isinstance(raw_score, (int, float))
                )
                item["weight"] = weight
                item["scorable"] = is_observed
                item["status"] = "scored" if is_observed else "not_observed"
                item.setdefault("evidence", [])
                if not is_observed:
                    item["score"] = None
                    item["deductions"] = []
                else:
                    usable_weight += weight
                    weighted_score += float(raw_score) * weight
            normalized["scoring_coverage"] = round(
                usable_weight, 2
            )
        if usable_weight > 0:
            normalized["scored"] = True
            normalized["score_status"] = "scored"
            # Normalize by the dimensions actually observed.  This removes
            # the legacy midpoint contribution from dimensions never asked.
            normalized["overall_score"] = round(weighted_score / usable_weight, 1)
        else:
            normalized["scored"] = False
            normalized["score_status"] = "unscorable"
            normalized["overall_score"] = 0.0
            normalized["scoring_coverage"] = 0.0
        return normalized

    normalized.update(
        scored=False,
        score_status="insufficient_data",
        overall_score=0.0,
        rubric={
            "project_depth": {"score": None, "weight": 0.4, "scorable": False, "status": "not_observed", "evidence": [], "deductions": []},
            "fundamentals": {"score": None, "weight": 0.3, "scorable": False, "status": "not_observed", "evidence": [], "deductions": []},
            "coding_thought": {"score": None, "weight": 0.2, "scorable": False, "status": "not_observed", "evidence": [], "deductions": []},
            "communication": {"score": None, "weight": 0.1, "scorable": False, "status": "not_observed", "evidence": [], "deductions": []},
        },
        question_feedback=[],
        topic_scores={},
        must_practice=[],
        next_focus=[],
        comparison={},
        summary=(
            "本场没有有效回答或可用转写，评分数据不足，因此本次不计分，"
            "也不会写入后续面试的弱项记忆。"
        ),
        scoring_coverage=0.0,
    )
    return normalized


class Database:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = Path(self.settings.db_path)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def operation(connection: sqlite3.Connection) -> None:
            connection.executescript(SCHEMA_SQL)
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(interviews)"
                ).fetchall()
            }
            if "specialization" not in columns:
                connection.execute(
                    "ALTER TABLE interviews ADD COLUMN specialization TEXT "
                    "NOT NULL DEFAULT '通用后端'"
                )
            if "interview_type" not in columns:
                connection.execute(
                    "ALTER TABLE interviews ADD COLUMN interview_type TEXT "
                    "NOT NULL DEFAULT 'technical'"
                )
            if "language_mode" not in columns:
                connection.execute(
                    "ALTER TABLE interviews ADD COLUMN language_mode TEXT "
                    "NOT NULL DEFAULT 'bilingual'"
                )
            if "stress_level" not in columns:
                connection.execute(
                    "ALTER TABLE interviews ADD COLUMN stress_level INTEGER "
                    "NOT NULL DEFAULT 0"
                )
                # Preserve the old boolean contract: enabled meant the current
                # standard pressure mode.
                connection.execute(
                    "UPDATE interviews SET stress_level = "
                    "CASE WHEN stress <> 0 THEN 2 ELSE 0 END"
                )
            if "memory_enabled" not in columns:
                connection.execute(
                    "ALTER TABLE interviews ADD COLUMN memory_enabled INTEGER "
                    "NOT NULL DEFAULT 1"
                )
            if "hint_events_json" not in columns:
                connection.execute(
                    "ALTER TABLE interviews ADD COLUMN hint_events_json TEXT "
                    "NOT NULL DEFAULT '[]'"
                )
            turn_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(interview_turns)"
                ).fetchall()
            }
            turn_migrations = {
                "scorable": "INTEGER NOT NULL DEFAULT 1",
                "score_source": "TEXT NOT NULL DEFAULT 'llm'",
                "input_mode": "TEXT NOT NULL DEFAULT 'text'",
                "answer_duration_seconds": "REAL",
                "speech_rate_cpm": "REAL",
                "transcript_edited": "INTEGER NOT NULL DEFAULT 0",
                "original_answer": "TEXT NOT NULL DEFAULT ''",
                "recommended_answer_seconds": "INTEGER NOT NULL DEFAULT 60",
            }
            for name, declaration in turn_migrations.items():
                if name not in turn_columns:
                    connection.execute(
                        f"ALTER TABLE interview_turns ADD COLUMN {name} {declaration}"
                    )
            connection.commit()

        await self._run(operation)

    async def create_interview(
        self,
        *,
        interview_id: str,
        client_id: str,
        company: str,
        role: str,
        interview_type: str,
        specialization: str,
        language_mode: str,
        stress: bool,
        stress_level: int,
        duration_minutes: int | None,
        memory_enabled: bool,
        voice_mode: str,
        resume: ResumeData,
        style: dict[str, Any],
        weak_topics: list[str],
        system_prompt: str,
        initial_question: str,
    ) -> None:
        values = (
            interview_id,
            client_id,
            company,
            role,
            interview_type,
            specialization,
            language_mode,
            int(stress),
            stress_level,
            # Older deployed databases declared this column NOT NULL. A zero
            # sentinel keeps those databases writable and is decoded as None.
            duration_minutes if duration_minutes is not None else 0,
            int(memory_enabled),
            voice_mode,
            resume.model_dump_json(by_alias=True),
            json.dumps(style, ensure_ascii=False),
            json.dumps(weak_topics, ensure_ascii=False),
            system_prompt,
            initial_question,
            _utc_iso(),
        )

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO interviews (
                    id, client_id, company, role, interview_type, specialization,
                    language_mode, stress, stress_level, duration_minutes,
                    memory_enabled, voice_mode, resume_json, style_json,
                    weak_topics_json, system_prompt, last_question, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.commit()

        await self._run(operation)

    async def get_interview(self, interview_id: str) -> dict[str, Any] | None:
        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM interviews WHERE id = ?", (interview_id,)
            ).fetchone()
            return self._decode_interview(row) if row else None

        return await self._run(operation)

    async def start_interview(self, interview_id: str) -> dict[str, Any] | None:
        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT * FROM interviews WHERE id = ?", (interview_id,)
            ).fetchone()
            if not row:
                return None
            if row["started_at"] is None and row["status"] == "created":
                started = time.time()
                duration = row["duration_minutes"]
                deadline = (
                    started + int(duration) * 60
                    if duration is not None and int(duration) > 0
                    else None
                )
                connection.execute(
                    """
                    UPDATE interviews
                    SET status = 'active', started_at = ?, deadline_at = ?
                    WHERE id = ?
                    """,
                    (started, deadline, interview_id),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM interviews WHERE id = ?", (interview_id,)
                ).fetchone()
            return self._decode_interview(row)

        return await self._run(operation)

    async def append_turn(
        self,
        interview_id: str,
        turn: InterviewTurn,
        next_question: str,
    ) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            connection.execute(
                """
                INSERT INTO interview_turns (
                    interview_id, ordinal, question, answer, category, topic,
                    score, scorable, score_source, deductions_json, failed,
                    drill_dimension, drill_depth,
                    anchor_keyword, input_mode, answer_duration_seconds,
                    speech_rate_cpm, transcript_edited, original_answer,
                    recommended_answer_seconds, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interview_id,
                    turn.ordinal,
                    turn.question,
                    turn.answer,
                    turn.category,
                    turn.topic,
                    turn.score if turn.score is not None else 0.0,
                    int(turn.scorable and turn.score is not None),
                    turn.score_source,
                    json.dumps(turn.deductions, ensure_ascii=False),
                    int(turn.failed),
                    turn.drill_dimension,
                    turn.drill_depth,
                    turn.anchor_keyword,
                    turn.input_mode,
                    turn.answer_duration_seconds,
                    turn.speech_rate_cpm,
                    int(turn.transcript_edited),
                    turn.original_answer,
                    turn.recommended_answer_seconds,
                    turn.created_at,
                ),
            )
            row = connection.execute(
                "SELECT breakdown_streak FROM interviews WHERE id = ?",
                (interview_id,),
            ).fetchone()
            old_streak = int(row["breakdown_streak"]) if row else 0
            streak = old_streak + 1 if turn.failed else 0
            connection.execute(
                """
                UPDATE interviews
                SET breakdown_streak = ?, last_question = ?
                WHERE id = ?
                """,
                (streak, next_question, interview_id),
            )
            connection.commit()
            return streak

        return await self._run(operation)

    async def correct_turn_answer(
        self,
        interview_id: str,
        *,
        ordinal: int,
        text: str,
        score: float | None,
        scorable: bool,
        score_source: str,
        deductions: list[str],
        failed: bool,
    ) -> dict[str, Any]:
        """Atomically replace a transcript and its private assessment.

        Corrections are rejected once report generation starts so persisted
        feedback can never describe a different answer than the report input.
        """

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            interview = connection.execute(
                "SELECT status FROM interviews WHERE id = ?", (interview_id,)
            ).fetchone()
            if not interview:
                raise LookupError("interview_not_found")
            if interview["status"] in {"reporting", "reported"} or connection.execute(
                "SELECT 1 FROM reports WHERE interview_id = ?", (interview_id,)
            ).fetchone():
                raise RuntimeError("report_already_generated")
            row = connection.execute(
                """
                SELECT answer, original_answer, transcript_edited,
                       speech_rate_cpm
                FROM interview_turns
                WHERE interview_id = ? AND ordinal = ?
                """,
                (interview_id, ordinal),
            ).fetchone()
            if not row:
                raise KeyError("turn_not_found")
            original = (
                str(row["original_answer"] or "")
                if bool(row["transcript_edited"])
                else str(row["answer"] or "")
            )
            # Editing an ASR transcript changes the semantic answer used for
            # technical scoring, not what was actually spoken. Preserve the
            # original live-transcript rate for delivery analysis.
            speech_rate = row["speech_rate_cpm"]
            connection.execute(
                """
                UPDATE interview_turns
                SET answer = ?, score = ?, scorable = ?, score_source = ?,
                    deductions_json = ?, failed = ?,
                    transcript_edited = 1, original_answer = ?
                WHERE interview_id = ? AND ordinal = ?
                """,
                (
                    text,
                    score if score is not None else 0.0,
                    int(scorable and score is not None),
                    score_source,
                    json.dumps(deductions, ensure_ascii=False),
                    int(failed),
                    original,
                    interview_id,
                    ordinal,
                ),
            )
            failed_rows = connection.execute(
                """
                SELECT failed FROM interview_turns
                WHERE interview_id = ? ORDER BY ordinal DESC
                """,
                (interview_id,),
            ).fetchall()
            streak = 0
            for failed_row in failed_rows:
                if not bool(failed_row["failed"]):
                    break
                streak += 1
            connection.execute(
                "UPDATE interviews SET breakdown_streak = ? WHERE id = ?",
                (streak, interview_id),
            )
            connection.commit()
            return {
                "ordinal": ordinal,
                "text": text,
                "original_text": original,
                "transcript_edited": True,
                "score": score,
                "scorable": bool(scorable and score is not None),
                "score_source": score_source,
                "deductions": deductions,
                "failed": failed,
                "speech_rate_cpm": speech_rate,
            }

        return await self._run(operation)

    async def set_last_question(self, interview_id: str, question: str) -> None:
        question = question.strip()
        if not question:
            return

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE interviews SET last_question = ? WHERE id = ?",
                (question, interview_id),
            )
            connection.commit()

        await self._run(operation)

    async def record_hint(
        self,
        interview_id: str,
        *,
        ordinal: int,
        question: str,
        hint: str,
    ) -> dict[str, Any]:
        """Persist at most one scaffold hint for each question ordinal."""

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT hint_events_json FROM interviews WHERE id = ?",
                (interview_id,),
            ).fetchone()
            if not row:
                return {}
            try:
                events = json.loads(row["hint_events_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                events = []
            if not isinstance(events, list):
                events = []
            existing = next(
                (
                    event
                    for event in events
                    if isinstance(event, dict)
                    and int(event.get("ordinal") or 0) == ordinal
                ),
                None,
            )
            if existing:
                return {**existing, "created": False, "hint_count": len(events)}
            event = {
                "ordinal": ordinal,
                "question": question,
                "hint": hint,
                "created_at": _utc_iso(),
            }
            events.append(event)
            connection.execute(
                "UPDATE interviews SET hint_events_json = ? WHERE id = ?",
                (json.dumps(events, ensure_ascii=False), interview_id),
            )
            connection.commit()
            return {**event, "created": True, "hint_count": len(events)}

        return await self._run(operation)

    async def set_voice_mode(self, interview_id: str, voice_mode: str) -> None:
        if voice_mode not in {"L0", "L1", "L2", "L3"}:
            return

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE interviews SET voice_mode = ? WHERE id = ?",
                (voice_mode, interview_id),
            )
            connection.commit()

        await self._run(operation)

    async def list_turns(self, interview_id: str) -> list[InterviewTurn]:
        def operation(connection: sqlite3.Connection) -> list[InterviewTurn]:
            rows = connection.execute(
                """
                SELECT * FROM interview_turns
                WHERE interview_id = ? ORDER BY ordinal
                """,
                (interview_id,),
            ).fetchall()
            return [
                InterviewTurn(
                    ordinal=row["ordinal"],
                    question=row["question"],
                    answer=row["answer"],
                    category=row["category"],
                    topic=row["topic"],
                    score=row["score"] if bool(row["scorable"]) else None,
                    scorable=bool(row["scorable"]),
                    score_source=(
                        row["score_source"]
                        if row["score_source"] in {"llm", "mock", "unavailable"}
                        else "unavailable"
                    ),
                    deductions=json.loads(row["deductions_json"]),
                    failed=bool(row["failed"]),
                    drill_dimension=row["drill_dimension"],
                    drill_depth=row["drill_depth"],
                    anchor_keyword=row["anchor_keyword"],
                    input_mode=(
                        "voice" if row["input_mode"] == "voice" else "text"
                    ),
                    answer_duration_seconds=row["answer_duration_seconds"],
                    speech_rate_cpm=row["speech_rate_cpm"],
                    transcript_edited=bool(row["transcript_edited"]),
                    original_answer=row["original_answer"],
                    recommended_answer_seconds=row["recommended_answer_seconds"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

        return await self._run(operation)

    async def finish_interview(self, interview_id: str, reason: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE interviews
                SET status = CASE WHEN status = 'reported' THEN status ELSE 'ended' END,
                    ended_at = COALESCE(ended_at, ?),
                    end_reason = COALESCE(end_reason, ?)
                WHERE id = ?
                """,
                (time.time(), reason, interview_id),
            )
            connection.commit()
            return cursor.rowcount > 0

        return await self._run(operation)

    async def mark_reporting(self, interview_id: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE interviews SET status = 'reporting'
                WHERE id = ? AND status IN ('ended', 'reporting')
                """,
                (interview_id,),
            )
            connection.commit()
            return cursor.rowcount > 0

        return await self._run(operation)

    async def save_report(
        self, interview: dict[str, Any], report: InterviewReport
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO reports (
                    id, interview_id, client_id, company, overall_score,
                    report_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(interview_id) DO UPDATE SET
                    id=excluded.id,
                    overall_score=excluded.overall_score,
                    report_json=excluded.report_json,
                    created_at=excluded.created_at
                """,
                (
                    report.report_id,
                    interview["id"],
                    interview["client_id"],
                    interview["company"],
                    report.overall_score,
                    report.model_dump_json(),
                    report.generated_at,
                ),
            )
            connection.execute(
                "UPDATE interviews SET status = 'reported' WHERE id = ?",
                (interview["id"],),
            )
            connection.commit()

        await self._run(operation)

    async def get_report(self, interview_id: str) -> dict[str, Any] | None:
        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                """
                SELECT r.report_json,
                       (SELECT COUNT(*) FROM interview_turns t
                        WHERE t.interview_id = r.interview_id
                          AND has_effective_text(t.answer) = 1) AS effective_turn_count,
                       (SELECT group_concat(DISTINCT t.category)
                        FROM interview_turns t
                        WHERE t.interview_id = r.interview_id
                          AND has_effective_text(t.answer) = 1
                          AND t.scorable = 1) AS observed_dimensions
                FROM reports r WHERE r.interview_id = ?
                """,
                (interview_id,),
            ).fetchone()
            if not row:
                return None
            return _normalize_report_scoring(
                json.loads(row["report_json"]),
                int(row["effective_turn_count"]),
                set(str(row["observed_dimensions"] or "").split(",")) - {""},
            )

        return await self._run(operation)

    async def history(
        self,
        client_id: str,
        limit: int = 20,
        *,
        memory_only: bool = False,
        scored_only: bool = False,
        exclude_interview_id: str = "",
    ) -> list[dict[str, Any]]:
        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                """
                SELECT r.report_json, i.role, i.interview_type, i.specialization,
                       i.language_mode, i.stress, i.stress_level, i.duration_minutes, i.ended_at,
                       i.end_reason, i.memory_enabled, i.hint_events_json,
                       (SELECT COUNT(*) FROM interview_turns t
                        WHERE t.interview_id = r.interview_id
                          AND has_effective_text(t.answer) = 1) AS effective_turn_count,
                       (SELECT group_concat(DISTINCT t.category)
                        FROM interview_turns t
                        WHERE t.interview_id = r.interview_id
                          AND has_effective_text(t.answer) = 1
                          AND t.scorable = 1) AS observed_dimensions
                FROM reports r
                JOIN interviews i ON i.id = r.interview_id
                WHERE r.client_id = ?
                  AND (? = 0 OR i.memory_enabled = 1)
                  AND (
                    ? = 0 OR (
                      EXISTS (
                        SELECT 1 FROM interview_turns scored_turn
                        WHERE scored_turn.interview_id = r.interview_id
                          AND has_effective_text(scored_turn.answer) = 1
                      )
                      AND COALESCE(json_extract(r.report_json, '$.scored'), 1) != 0
                      AND COALESCE(
                        json_extract(r.report_json, '$.score_status'), 'scored'
                      ) = 'scored'
                    )
                  )
                  AND (? = '' OR r.interview_id != ?)
                ORDER BY r.created_at DESC LIMIT ?
                """,
                (
                    client_id,
                    int(memory_only),
                    int(scored_only),
                    exclude_interview_id,
                    exclude_interview_id,
                    limit,
                ),
            ).fetchall()
            reports: list[dict[str, Any]] = []
            for row in rows:
                report = _normalize_report_scoring(
                    json.loads(row["report_json"]),
                    int(row["effective_turn_count"]),
                    set(str(row["observed_dimensions"] or "").split(",")) - {""},
                )
                try:
                    hint_events = json.loads(row["hint_events_json"] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    hint_events = []
                if not isinstance(hint_events, list):
                    hint_events = []
                report.update(
                    role=row["role"],
                    interview_type=(
                        "technical_hr"
                        if row["interview_type"] == "technical_hr"
                        else "technical"
                    ),
                    specialization=row["specialization"],
                    language_mode=row["language_mode"],
                    stress_level=int(row["stress_level"]),
                    stress=int(row["stress_level"]) > 0,
                    duration_minutes=(
                        int(row["duration_minutes"])
                        if row["duration_minutes"] is not None
                        and int(row["duration_minutes"]) > 0
                        else None
                    ),
                    ended_at=(
                        datetime.fromtimestamp(row["ended_at"], timezone.utc).isoformat()
                        if row["ended_at"] is not None
                        else report.get("generated_at")
                    ),
                    end_reason=row["end_reason"],
                    memory_enabled=bool(row["memory_enabled"]),
                    hint_events=hint_events,
                )
                report["hint_count"] = len(report["hint_events"])
                reports.append(report)
            return reports

        return await self._run(operation)

    async def delete_history_item(self, client_id: str, interview_id: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT id FROM interviews WHERE id = ? AND client_id = ?",
                (interview_id, client_id),
            ).fetchone()
            if not row:
                return False
            connection.execute("DELETE FROM interviews WHERE id = ?", (interview_id,))
            connection.commit()
            return True

        return await self._run(operation)

    async def clear_history(self, client_id: str) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM interviews WHERE client_id = ?", (client_id,)
            )
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def weak_topics(self, client_id: str, limit: int = 3) -> list[str]:
        reports = await self.history(
            client_id, limit=3, memory_only=True, scored_only=True
        )
        if not reports:
            return []
        weights = [0.6, 0.3, 0.1]
        weighted: dict[str, float] = {}
        totals: dict[str, float] = {}
        for index, report in enumerate(reports):
            weight = weights[index] if index < len(weights) else 0.05
            per_report: dict[str, list[float]] = {}
            for topic, raw_score in (report.get("topic_scores") or {}).items():
                try:
                    score = float(raw_score)
                except (TypeError, ValueError):
                    continue
                canonical = canonical_topic(str(topic))
                per_report.setdefault(canonical, []).append(score)
            for topic, scores in per_report.items():
                # Old reports can contain both fine-grained and broad aliases.
                # Consolidate them once per report before applying recency.
                score = sum(scores) / len(scores)
                weighted[topic] = weighted.get(topic, 0) + score * weight
                totals[topic] = totals.get(topic, 0) + weight
        averages = {
            topic: value / totals[topic]
            for topic, value in weighted.items()
            if totals.get(topic)
        }
        return [topic for topic, _ in sorted(averages.items(), key=lambda pair: pair[1])[:limit]]

    async def interview_count_today(self, client_id: str | None = None) -> int:
        today = datetime.now(timezone.utc).date().isoformat()

        def operation(connection: sqlite3.Connection) -> int:
            if client_id:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM interviews
                    WHERE substr(created_at, 1, 10) = ? AND client_id = ?
                    """,
                    (today, client_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM interviews
                    WHERE substr(created_at, 1, 10) = ?
                    """,
                    (today,),
                ).fetchone()
            return int(row["count"] if row else 0)

        return await self._run(operation)

    async def _run(self, function: Callable[[sqlite3.Connection], T]) -> T:
        # Each operation is deliberately short and never surrounds a model or
        # network call. Direct access avoids a thread-pool dependency on tiny
        # single-worker hackathon hosts while WAL still handles concurrent reads.
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        # SQLite TRIM only removes ASCII spaces by default.  Reuse Python's
        # Unicode whitespace semantics so report generation and legacy reads
        # agree for tabs, newlines, NBSP, and full-width spaces.
        connection.create_function(
            "has_effective_text",
            1,
            lambda value: int(bool(str(value or "").strip())),
            deterministic=True,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            return function(connection)
        finally:
            connection.close()

    @staticmethod
    def _decode_interview(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        raw_stress_level = data.get("stress_level")
        stress_level = int(
            raw_stress_level
            if raw_stress_level is not None
            else (2 if data.get("stress") else 0)
        )
        data["stress_level"] = stress_level
        data["stress"] = stress_level > 0
        data["interview_type"] = (
            "technical_hr"
            if data.get("interview_type") == "technical_hr"
            else "technical"
        )
        data["specialization"] = str(data.get("specialization") or "通用后端")
        data["language_mode"] = (
            "zh" if data.get("language_mode") == "zh" else "bilingual"
        )
        data["memory_enabled"] = bool(data.get("memory_enabled", 1))
        duration = data.get("duration_minutes")
        data["duration_minutes"] = (
            int(duration) if duration is not None and int(duration) > 0 else None
        )
        data["resume"] = json.loads(data.pop("resume_json"))
        data["style"] = json.loads(data.pop("style_json"))
        data["weak_topics"] = json.loads(data.pop("weak_topics_json"))
        try:
            hint_events = json.loads(data.pop("hint_events_json", "[]") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            hint_events = []
        data["hint_events"] = hint_events if isinstance(hint_events, list) else []
        data["hint_count"] = len(data["hint_events"])
        deadline = data.get("deadline_at")
        data["remaining_seconds"] = (
            max(0, int(float(deadline) - time.time())) if deadline else None
        )
        return data
