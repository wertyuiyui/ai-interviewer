from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from typing import Any

from pydantic import ValidationError

from .config import Settings, get_settings
from .content import COMPANIES, load_topic_links
from .db import Database
from .errors import AppError, LLMError
from .llm import BailianChatClient
from .schemas import (
    HintEvent,
    InterviewReport,
    InterviewTurn,
    PracticeItem,
    QuestionFeedback,
    Rubric,
    RubricDimension,
)
from .topics import canonical_topic


RUBRIC_WEIGHTS = {
    "project_depth": 0.4,
    "fundamentals": 0.3,
    "coding_thought": 0.2,
    "communication": 0.1,
}


REPORT_SYSTEM_PROMPT = """你是严格但建设性的技术面试复盘官。面试已经结束，现在才允许点评。
根据完整逐题转写输出结构化 JSON 报告。每道题必须有具体扣分点和一段可直接学习的改写示范；不得笼统说“需要加强”。
评分 rubric 固定：项目深度40%、基础八股30%、手撕思路20%、表达逻辑10%。
不要编造候选人未说过的项目事实。资源只写 JavaGuide 或 CodeTop，URL 将由服务端安全映射。"""


class ReportEngine:
    def __init__(
        self,
        db: Database,
        settings: Settings | None = None,
        client: BailianChatClient | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.client = client or BailianChatClient(self.settings)
        self._locks: dict[str, asyncio.Lock] = {}

    async def generate(self, interview_id: str) -> InterviewReport:
        existing = await self.db.get_report(interview_id)
        if existing:
            return InterviewReport.model_validate(existing)
        lock = self._locks.setdefault(interview_id, asyncio.Lock())
        async with lock:
            existing = await self.db.get_report(interview_id)
            if existing:
                return InterviewReport.model_validate(existing)
            interview = await self.db.get_interview(interview_id)
            if not interview:
                raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
            if interview["status"] in {"created", "active"}:
                raise AppError("INTERVIEW_ACTIVE", "请先结束面试再生成报告", status_code=409)
            await self.db.mark_reporting(interview_id)
            turns = await self.db.list_turns(interview_id)
            report = await self._build(interview, turns)
            previous = await self.db.history(interview["client_id"], limit=1)
            report.comparison = self._comparison(previous[0] if previous else None, report)
            await self.db.save_report(interview, report)
            return report

    async def _build(
        self, interview: dict[str, Any], turns: list[InterviewTurn]
    ) -> InterviewReport:
        report_id = uuid.uuid4().hex
        if self.settings.mock_llm:
            return self._deterministic_report(interview, turns, report_id)

        transcript = [
            {
                "ordinal": turn.ordinal,
                "question": turn.question,
                "answer": turn.answer,
                "private_score": turn.score,
                "private_deductions": turn.deductions,
                "topic": turn.topic,
                "dimension": turn.category,
            }
            for turn in turns
        ]
        prompt = {
            "interview_id": interview["id"],
            "report_id": report_id,
            "company": interview["company"],
            "ended_reason": interview["end_reason"],
            "transcript": transcript,
            "required": "输出每题扣分点、改写示范、四维 rubric、知识点分数和下次必练清单",
        }
        try:
            raw = await self.client.chat_json(
                [
                    {"role": "system", "content": REPORT_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(prompt, ensure_ascii=False),
                    },
                ],
                response_schema=InterviewReport.model_json_schema(),
                schema_name="interview_report",
                model=self.settings.qwen_report_model,
                temperature=0.2,
                max_tokens=7000,
            )
            raw.update(
                report_id=report_id,
                interview_id=interview["id"],
                company=interview["company"],
                schema_version="1.0",
            )
            candidate = InterviewReport.model_validate(raw)
        except (ValidationError, LLMError, ValueError):
            candidate = self._deterministic_report(interview, turns, report_id)
        return self._normalize(candidate, interview, turns)

    def _deterministic_report(
        self,
        interview: dict[str, Any],
        turns: list[InterviewTurn],
        report_id: str,
    ) -> InterviewReport:
        scores = self._dimension_scores(turns)
        rubric = self._rubric(scores, turns)
        overall = self._overall(rubric)
        topic_scores = self._topic_scores(turns)
        feedback = [
            QuestionFeedback(
                question=turn.question,
                answer=turn.answer,
                category=turn.category,
                score=turn.score,
                deductions=turn.deductions
                or ["回答缺少一项可验证的数据、原理或边界说明"],
                better_answer=self._better_answer(turn),
            )
            for turn in turns
        ]
        practice = self._practice_items(topic_scores, turns)
        focus = [item.topic for item in practice]
        summary = (
            f"本场完成 {len(turns)} 轮问答。"
            f"当前最需要补强的是{'、'.join(focus) if focus else '项目证据链与表达结构'}。"
        )
        return InterviewReport(
            report_id=report_id,
            interview_id=interview["id"],
            company=interview["company"],
            overall_score=overall,
            rubric=rubric,
            question_feedback=feedback,
            topic_scores=topic_scores,
            must_practice=practice,
            summary=summary,
            next_focus=focus,
            memory_enabled=bool(interview.get("memory_enabled", True)),
            hint_count=len(interview.get("hint_events") or []),
            hint_events=interview.get("hint_events") or [],
        )

    def _normalize(
        self,
        candidate: InterviewReport,
        interview: dict[str, Any],
        turns: list[InterviewTurn],
    ) -> InterviewReport:
        scores = self._dimension_scores(turns)
        candidate.rubric = self._rubric(scores, turns)
        candidate.overall_score = self._overall(candidate.rubric)
        candidate.report_id = candidate.report_id or uuid.uuid4().hex
        candidate.interview_id = interview["id"]
        candidate.company = interview["company"]
        candidate.memory_enabled = bool(interview.get("memory_enabled", True))
        candidate.hint_count = len(interview.get("hint_events") or [])
        candidate.hint_events = [
            HintEvent.model_validate(event)
            for event in (interview.get("hint_events") or [])
        ]

        normalized_feedback: list[QuestionFeedback] = []
        for index, turn in enumerate(turns):
            generated = (
                candidate.question_feedback[index]
                if index < len(candidate.question_feedback)
                else None
            )
            normalized_feedback.append(
                QuestionFeedback(
                    question=turn.question,
                    answer=turn.answer,
                    category=turn.category,
                    score=turn.score,
                    deductions=(generated.deductions if generated else turn.deductions)
                    or ["回答缺少可验证的关键细节"],
                    better_answer=(generated.better_answer if generated else "")
                    or self._better_answer(turn),
                )
            )
        candidate.question_feedback = normalized_feedback
        candidate.topic_scores = self._topic_scores(turns)
        candidate.must_practice = self._practice_items(candidate.topic_scores, turns)
        candidate.next_focus = [item.topic for item in candidate.must_practice]
        return candidate

    @staticmethod
    def _dimension_scores(turns: list[InterviewTurn]) -> dict[str, float]:
        grouped: dict[str, list[float]] = defaultdict(list)
        for turn in turns:
            grouped[turn.category].append(turn.score)
            if turn.category != "communication":
                # Expression is observed on every answer without a second LLM call.
                expression = min(10.0, max(0.0, turn.score + (0.5 if len(turn.answer) >= 60 else -0.5)))
                grouped["communication"].append(expression)
        return {
            dimension: round(sum(grouped[dimension]) / len(grouped[dimension]), 1)
            if grouped[dimension]
            else 5.0
            for dimension in RUBRIC_WEIGHTS
        }

    @staticmethod
    def _rubric(
        scores: dict[str, float], turns: list[InterviewTurn]
    ) -> Rubric:
        deductions_by_dimension: dict[str, list[str]] = defaultdict(list)
        for turn in turns:
            deductions_by_dimension[turn.category].extend(turn.deductions)
        return Rubric(
            project_depth=RubricDimension(
                score=scores["project_depth"],
                weight=0.4,
                deductions=deductions_by_dimension["project_depth"][:4],
            ),
            fundamentals=RubricDimension(
                score=scores["fundamentals"],
                weight=0.3,
                deductions=deductions_by_dimension["fundamentals"][:4],
            ),
            coding_thought=RubricDimension(
                score=scores["coding_thought"],
                weight=0.2,
                deductions=deductions_by_dimension["coding_thought"][:4],
            ),
            communication=RubricDimension(
                score=scores["communication"],
                weight=0.1,
                deductions=deductions_by_dimension["communication"][:4],
            ),
        )

    @staticmethod
    def _overall(rubric: Rubric) -> float:
        value = (
            rubric.project_depth.score * 0.4
            + rubric.fundamentals.score * 0.3
            + rubric.coding_thought.score * 0.2
            + rubric.communication.score * 0.1
        )
        return round(value, 1)

    @staticmethod
    def _topic_scores(turns: list[InterviewTurn]) -> dict[str, float]:
        grouped: dict[str, list[float]] = defaultdict(list)
        for turn in turns:
            grouped[canonical_topic(turn.topic, turn.category)].append(turn.score)
        return {
            topic: round(sum(values) / len(values), 1)
            for topic, values in grouped.items()
        }

    def _practice_items(
        self, topic_scores: dict[str, float], turns: list[InterviewTurn]
    ) -> list[PracticeItem]:
        links = load_topic_links()
        weakest = sorted(topic_scores.items(), key=lambda item: item[1])[:3]
        if not weakest:
            weakest = [("项目深挖", 5.0), ("MySQL", 5.0), ("手撕思路", 5.0)]
        result: list[PracticeItem] = []
        for topic, score in weakest:
            resource = self._resource_for_topic(topic, links)
            result.append(
                PracticeItem(
                    topic=topic,
                    reason=f"本场该知识点得分 {score:.1f}/10，需要优先补齐原理、证据和边界。",
                    resource_title=resource["title"],  # type: ignore[arg-type]
                    resource_url=resource["url"],
                )
            )
        return result

    @staticmethod
    def _resource_for_topic(
        topic: str, links: dict[str, dict[str, str]]
    ) -> dict[str, str]:
        lowered = topic.lower()
        for key, resource in links.items():
            if key != "default" and (key.lower() in lowered or lowered in key.lower()):
                return resource
        if any(word in topic for word in ("手撕", "算法", "LRU", "链表", "数组")):
            return {"title": "CodeTop", "url": "https://codetop.cc/home"}
        return links.get(
            "default", {"title": "JavaGuide", "url": "https://javaguide.cn/"}
        )

    @staticmethod
    def _better_answer(turn: InterviewTurn) -> str:
        anchor = turn.anchor_keyword or turn.topic
        return (
            f"我会先明确背景与目标，再说明我本人围绕“{anchor}”做的设计；"
            "随后按请求链路解释关键原理，用基线、统计窗口和结果数据验证收益；"
            "最后补充失败场景、替代方案与当前方案的取舍边界。"
        )

    @staticmethod
    def _comparison(
        previous: dict[str, Any] | None, current: InterviewReport
    ) -> dict[str, Any]:
        if not previous:
            return {}
        before = previous.get("topic_scores") or {}
        changes: dict[str, dict[str, float]] = {}
        for topic, score in current.topic_scores.items():
            if topic not in before:
                continue
            try:
                old = float(before[topic])
            except (TypeError, ValueError):
                continue
            changes[topic] = {
                "previous": round(old, 1),
                "current": round(score, 1),
                "delta": round(score - old, 1),
            }
        return {
            "previous_interview_id": previous.get("interview_id"),
            "overall_delta": round(
                current.overall_score - float(previous.get("overall_score", 0)), 1
            ),
            "topic_changes": changes,
        }
