from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import ROOT_DIR, Settings, get_settings
from .db import Database
from .errors import AppError, LLMError
from .llm import BailianChatClient


CodingLanguage = Literal["python", "java", "go", "javascript"]
CodingStage = Literal["clarify", "approach", "code", "test"]


class CodingReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(min_length=3, max_length=100)
    language: CodingLanguage
    assumptions: str = Field(min_length=2, max_length=3000)
    approach: str = Field(min_length=10, max_length=5000)
    code: str = Field(min_length=10, max_length=20000)
    complexity: str = Field(min_length=3, max_length=2000)
    test_cases: list[str] = Field(min_length=1, max_length=12)
    client_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]{8,128}$"
    )

    @field_validator("assumptions", "approach", "code", "complexity")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = value.replace("\x00", "").strip()
        if not value:
            raise ValueError("内容不能为空")
        return value

    @field_validator("test_cases")
    @classmethod
    def clean_tests(cls, values: list[str]) -> list[str]:
        cleaned = [value.replace("\x00", "").strip() for value in values]
        cleaned = [value for value in cleaned if value]
        if not cleaned:
            raise ValueError("至少写一个自测用例")
        return cleaned


class CodingHintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(min_length=3, max_length=100)
    stage: CodingStage
    client_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]{8,128}$"
    )


class CodingDimension(BaseModel):
    model_config = ConfigDict(extra="ignore")

    score: float = Field(ge=0, le=10)
    feedback: str = Field(min_length=1, max_length=1000)


class CodingAssessment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    overall_score: float = Field(ge=0, le=10)
    summary: str = Field(min_length=1, max_length=1200)
    communication: CodingDimension
    problem_solving: CodingDimension
    technical_competency: CodingDimension
    testing: CodingDimension
    strengths: list[str] = Field(default_factory=list, max_length=8)
    improvements: list[str] = Field(default_factory=list, max_length=8)
    improved_solution: str = Field(min_length=10, max_length=12000)
    next_drill: str = Field(min_length=1, max_length=500)
    execution_status: Literal["not_executed"] = "not_executed"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AppError("CODING_BANK_INVALID", "手撕代码题库暂时不可用", status_code=503) from exc
    if not isinstance(data, dict):
        raise AppError("CODING_BANK_INVALID", "手撕代码题库格式错误", status_code=503)
    return data


class CodingPracticeService:
    def __init__(
        self,
        settings: Settings | None = None,
        client: BailianChatClient | None = None,
        *,
        db: Database | None = None,
        bank_path: Path | None = None,
        manifest_path: Path | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or BailianChatClient(self.settings)
        self.db = db
        self.bank_path = bank_path or ROOT_DIR / "questions" / "coding_practice_bank.json"
        self.manifest_path = manifest_path or ROOT_DIR / "resources" / "coding_source_manifest.json"

    def _bank(self) -> list[dict[str, Any]]:
        questions = _read_json(self.bank_path).get("questions") or []
        if not isinstance(questions, list) or not questions:
            raise AppError("CODING_BANK_EMPTY", "手撕代码题库为空", status_code=503)
        return [item for item in questions if isinstance(item, dict) and item.get("id")]

    async def _challenge(
        self, challenge_id: str, client_id: str | None = None
    ) -> dict[str, Any]:
        challenge = next(
            (
                item
                for item in await self._mistake_challenges(client_id)
                if str(item.get("id")) == challenge_id
            ),
            None,
        )
        if challenge is None:
            challenge = next(
                (item for item in self._bank() if str(item.get("id")) == challenge_id),
                None,
            )
        if challenge is None:
            raise AppError("CODING_CHALLENGE_NOT_FOUND", "手撕代码题目不存在", status_code=404)
        return challenge

    @staticmethod
    def _mistake_challenge(question: dict[str, Any]) -> dict[str, Any]:
        prompt = str(question.get("question") or "").strip()
        topic = str(question.get("topic") or "手撕代码").removeprefix("手撕思路·")
        deductions = [
            str(item).strip()
            for item in (question.get("previous_deductions") or [])
            if str(item).strip()
        ][:4]
        better_answer = str(question.get("previous_better_answer") or "").strip()
        reference = better_answer if better_answer.count("\n") >= 3 else (
            "function solve(input):\n"
            "    clarify input, output, and edge cases\n"
            "    choose a data structure and state the invariant\n"
            "    implement the state transition\n"
            "    return the verified result"
        )
        return {
            "id": str(question.get("id") or ""),
            "title": {"zh": f"错题重练：{topic}", "en": f"Mistake retry: {topic}"},
            "topic": topic,
            "patterns": ["错题优先", "完整重做"],
            "difficulty": "medium",
            "recommended_minutes": 30,
            "prompt": {"zh": prompt, "en": prompt},
            "constraints": [
                *(f"上次扣分：{item}" for item in deductions),
                "先澄清输入输出，再说明方案、不变量、复杂度和边界用例",
            ],
            "examples": [],
            "signatures": {
                "python": "def solve(input):",
                "java": "Object solve(Object input)",
                "go": "func solve(input any) any",
                "javascript": "function solve(input)",
            },
            "rubric": {
                "key_points": ["方案与关键不变量一致", "实现、复杂度和边界用例完整"],
                "edge_cases": ["空输入", "最小输入", "极端或无解输入"],
                "expected_complexity": "按题意给出并论证目标复杂度",
            },
            "reference_pseudocode": reference,
            "hints": {
                "clarify": "先列出输入、输出、无解行为和可修改性。",
                "approach": "从上次扣分点反推缺失的不变量或状态转移。",
                "code": "先写最小完整流程，再补边界分支。",
                "test": "至少覆盖普通、最小、边界和反例。",
            },
            "source_ref": "interview",
            "from_mistake_book": True,
            "previous_score": question.get("previous_score"),
            "previous_deductions": deductions,
            "origin_label": question.get("origin_label") or "面试错题",
        }

    async def _mistake_challenges(
        self, client_id: str | None
    ) -> list[dict[str, Any]]:
        if self.db is None or not client_id:
            return []

        def operation(connection: Any) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT question_snapshot_json, latest_score, latest_deductions_json "
                "FROM practice_mistakes WHERE client_id = ? ORDER BY updated_at DESC",
                (client_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                try:
                    question = json.loads(row["question_snapshot_json"])
                    deductions = json.loads(row["latest_deductions_json"] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if str(question.get("kind") or "") != "coding":
                    continue
                question["previous_score"] = row["latest_score"]
                question["previous_deductions"] = deductions
                result.append(self._mistake_challenge(question))
            return result

        return await self.db._run(operation)

    @staticmethod
    def _public_challenge(challenge: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "id", "title", "topic", "patterns", "difficulty", "recommended_minutes",
            "prompt", "constraints", "examples", "signatures", "source_ref",
            "from_mistake_book", "previous_score", "previous_deductions", "origin_label",
        }
        return {key: challenge[key] for key in allowed if key in challenge}

    async def catalog(self, client_id: str | None = None) -> dict[str, Any]:
        curated = self._bank()
        mistakes = await self._mistake_challenges(client_id)
        mistake_ids = {str(item.get("id")) for item in mistakes}
        questions = [
            *mistakes,
            *(item for item in curated if str(item.get("id")) not in mistake_ids),
        ]
        manifest = _read_json(self.manifest_path)
        return {
            "question_count": len(questions),
            "curated_question_count": len(curated),
            "mistake_count": len(mistakes),
            "topics": sorted({str(item.get("topic")) for item in questions}),
            "difficulties": ["easy", "medium"],
            "languages": ["python", "java", "go", "javascript"],
            "workflow": ["clarify", "approach", "code", "test", "review"],
            "judge_mode": "static_review",
            "questions": [self._public_challenge(item) for item in questions],
            "source_policy": manifest.get("policy") or {},
            "sources": manifest.get("sources") or [],
        }

    async def hint(self, request: CodingHintRequest) -> dict[str, str]:
        challenge = await self._challenge(request.challenge_id, request.client_id)
        hint = str((challenge.get("hints") or {}).get(request.stage) or "").strip()
        if request.stage == "code" and not hint:
            hint = "先把方案拆成少量职责清晰的步骤，再逐步翻译为代码。"
        return {"stage": request.stage, "hint": hint or "先用一个最小示例逐步推演。"}

    async def review(self, request: CodingReviewRequest) -> dict[str, Any]:
        challenge = await self._challenge(request.challenge_id, request.client_id)
        if self.settings.mock_llm:
            assessment = self._mock_review(challenge, request)
        else:
            assessment = await self._llm_review(challenge, request)
        return {
            "challenge": self._public_challenge(challenge),
            "assessment": assessment.model_dump(),
            "source": "Grind 75 · Tech Interview Handbook",
            "source_url": "https://www.techinterviewhandbook.org/grind75/?grouping=topics",
        }

    def _mock_review(
        self, challenge: dict[str, Any], request: CodingReviewRequest
    ) -> CodingAssessment:
        assumptions_score = min(10.0, 4.0 + len(request.assumptions) / 50)
        approach_score = min(10.0, 3.5 + len(request.approach) / 80)
        code_lines = [line for line in request.code.splitlines() if line.strip()]
        placeholder = bool(re.search(r"\b(?:todo|pass|return\s+null)\b", request.code, re.I))
        technical_score = min(10.0, 4.0 + len(code_lines) / 8 - (2.0 if placeholder else 0.0))
        test_score = min(10.0, 3.0 + len(request.test_cases) * 1.6)
        if re.search(r"边界|empty|null|nil|空|重复|cycle|环", " ".join(request.test_cases), re.I):
            test_score = min(10.0, test_score + 1.0)
        scores = [assumptions_score, approach_score, technical_score, test_score]
        overall = round(sum(scores) / len(scores), 1)
        rubric = challenge.get("rubric") or {}
        points = [str(item) for item in rubric.get("key_points") or []]
        edge_cases = [str(item) for item in rubric.get("edge_cases") or []]
        improvements = []
        if assumptions_score < 7:
            improvements.append("把输入保证、无解行为和是否允许修改输入问清楚。")
        if approach_score < 7:
            improvements.append("在写代码前比较朴素方案与目标方案，并说明核心不变量。")
        if technical_score < 7:
            improvements.append("补全关键状态更新，去掉占位实现，并逐行检查返回路径。")
        if test_score < 7:
            improvements.append("至少补充普通、空输入、边界和反例四类自测。")
        return CodingAssessment(
            overall_score=overall,
            summary="已按真实代码面试的四个维度完成静态复盘；代码未在服务器执行。",
            communication=CodingDimension(
                score=round(assumptions_score, 1),
                feedback="澄清内容越具体，越能避免对题意和输入保证作隐含假设。",
            ),
            problem_solving=CodingDimension(
                score=round(approach_score, 1),
                feedback=f"重点检查：{'；'.join(points) if points else '方案、不变量与复杂度是否一致'}。",
            ),
            technical_competency=CodingDimension(
                score=round(max(0.0, technical_score), 1),
                feedback="静态检查实现完整性、状态更新和可读性；不声称编译或用例通过。",
            ),
            testing=CodingDimension(
                score=round(test_score, 1),
                feedback=f"建议覆盖：{'、'.join(edge_cases) if edge_cases else '普通、边界与失败输入'}。",
            ),
            strengths=["完成了从澄清、方案到实现和自测的完整作答链路。"],
            improvements=improvements or ["下一次尝试在建议时限内边写边口述，并主动 dry-run。"],
            improved_solution=str(challenge.get("reference_pseudocode") or (
                "function solve(input):\n"
                "    // 按方案维护核心状态并遍历输入\n"
                "    // 返回满足题意的结果"
            )),
            next_drill=f"重做本题时，把目标复杂度“{rubric.get('expected_complexity') or '按题意分析'}”作为提交前检查项。",
        )

    async def _llm_review(
        self, challenge: dict[str, Any], request: CodingReviewRequest
    ) -> CodingAssessment:
        payload = {
            "challenge": challenge,
            "language": request.language,
            "candidate": request.model_dump(exclude={"challenge_id", "client_id"}),
        }
        system = """
你是代码面试复盘官。按 communication、problem_solving、technical_competency、testing 四维评分。
只依据候选人提交内容和题目私有 rubric，不运行代码，不得声称编译或测试通过。
检查题意澄清、方案/不变量、实现完整性、复杂度和候选人自拟边界用例。
improved_solution 必须给出所选语言的完整改进代码；只有题目信息不足以写合法代码时才给完整伪代码。该字段不得只写自然语言思路。
反馈必须具体引用提交中可观察的信息；execution_status 固定为 not_executed。
只输出符合 JSON Schema 的对象。
""".strip()
        try:
            raw = await self.client.chat_json(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_schema=CodingAssessment.model_json_schema(),
                schema_name="coding_interview_assessment",
                model=self.settings.qwen_text_model,
                temperature=0.2,
                max_tokens=1800,
            )
            return CodingAssessment.model_validate(raw)
        except (ValidationError, LLMError, AppError):
            return CodingAssessment(
                overall_score=0,
                summary="评分服务暂时不可用；提交内容未执行，请稍后重试。",
                communication=CodingDimension(score=0, feedback="暂未评分"),
                problem_solving=CodingDimension(score=0, feedback="暂未评分"),
                technical_competency=CodingDimension(score=0, feedback="暂未评分"),
                testing=CodingDimension(score=0, feedback="暂未评分"),
                improvements=["保留当前草稿，稍后重新提交静态复盘。"],
                improved_solution=str(challenge.get("reference_pseudocode") or (
                    "function solve(input):\n"
                    "    // 评分服务不可用，请按题目不变量补全实现"
                )),
                next_drill="稍后重试本题。",
            )
