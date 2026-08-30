from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import defaultdict
from typing import Any

from pydantic import ValidationError

from .config import ROOT_DIR, Settings, get_settings
from .content import COMPANIES, load_style_card, load_topic_links
from .db import Database
from .errors import AppError, LLMError
from .llm import BailianChatClient
from .schemas import (
    BehavioralAnalysis,
    CompanyExperienceCitation,
    CompanyInsights,
    EvidenceAnalysis,
    HintEvent,
    InterviewReport,
    InterviewTurn,
    PracticeItem,
    ProcessAnalysis,
    QuestionFeedback,
    RadarAxis,
    ResumeAnalysis,
    RoleFitAnalysis,
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

ENGLISH_TOPIC_LABELS = {
    "Java并发": "Java concurrency",
    "计网": "computer networking",
    "手撕思路": "coding thought process",
    "项目深度": "project depth",
    "表达逻辑": "communication structure",
    "综合基础": "backend fundamentals",
    "价值观与公司契合": "values and company fit",
    "职业规划与选择": "career planning and choices",
    "协作与冲突处理": "collaboration and conflict handling",
    "薪酬沟通": "compensation communication",
}

BEHAVIORAL_PRACTICE_GUIDANCE = {
    "价值观与公司契合": {
        "zh": "用真实取舍说明判断依据、个人行动、结果与复盘，并解释与目标团队的契合点。",
        "en": "Use a real trade-off to show your evidence, personal action, result, reflection, and fit with the target team.",
    },
    "职业规划与选择": {
        "zh": "把两三年目标拆成近期可验证行动，并说清选择不同实习机会时的排序依据。",
        "en": "Turn your two-to-three-year direction into verifiable near-term actions and explain how you rank different internship choices.",
    },
    "协作与冲突处理": {
        "zh": "按情境、分歧、本人沟通动作、结果与复盘说明，不要只给“善于沟通”的结论。",
        "en": "Cover the situation, disagreement, your communication, the result, and your reflection instead of merely claiming strong teamwork.",
    },
    "薪酬沟通": {
        "zh": "准备预期依据、薪酬与成长/导师/方向的排序及可协商边界；不以具体数值高低作为得失分依据。",
        "en": "Prepare the rationale for your expectations, rank compensation against growth, mentorship, and role fit, and state negotiable boundaries.",
    },
}


REPORT_SYSTEM_PROMPT = """你是严格但建设性的技术面试复盘官。面试已经结束，现在才允许点评。
根据完整逐题转写输出结构化 JSON 报告。每道题必须有具体扣分点和一段可直接学习的改写示范；不得笼统说“需要加强”。
评分 rubric 固定：项目深度40%、基础八股30%、手撕思路20%、表达逻辑10%。
不要编造候选人未说过的项目事实。没有观测证据的维度必须标记不可评分，不得填写5分或其他中间值。
不要生成题库来源、帖子标题或 URL；大厂真实面经引用由服务端可信资料覆盖。"""

REPORT_SYSTEM_PROMPT_EN = """You are a rigorous but constructive technical interview reviewer. The interview is over, so feedback is now allowed.
Return a structured JSON report in natural professional English. For every question, provide evidence-based deductions and a concrete improved answer that the candidate can study. Do not use vague advice such as 'needs improvement'.
Use the fixed rubric: project depth 40%, backend fundamentals 30%, coding thought process 20%, and communication 10%.
Do not invent project facts the candidate never stated. Any dimension without observed evidence must be marked unscorable and must never receive a default midpoint such as 5.
Do not generate question-bank sources, post titles, or URLs; trusted company interview citations are attached by the server."""


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
            turns = [
                turn
                for turn in await self.db.list_turns(interview_id)
                if turn.answer.strip()
            ]
            report = await self._build(interview, turns)
            if report.scored:
                history = await self.db.history(
                    interview["client_id"],
                    limit=1,
                    scored_only=True,
                    exclude_interview_id=interview_id,
                )
                previous = next(
                    (
                        item
                        for item in history
                        if item.get("scored", True)
                        and item.get("score_status", "scored") == "scored"
                    ),
                    None,
                )
                report.comparison = self._comparison(previous, report)
            else:
                report.comparison = {}
            await self.db.save_report(interview, report)
            return report

    async def _build(
        self, interview: dict[str, Any], turns: list[InterviewTurn]
    ) -> InterviewReport:
        report_id = uuid.uuid4().hex
        # With no usable answer there is no evidence to score.  In particular,
        # do not send an empty transcript to the report model: model-generated
        # midpoint values would look like a real assessment and pollute memory.
        if not turns:
            return self._insufficient_data_report(interview, report_id)
        if self.settings.mock_llm:
            return self._deterministic_report(interview, turns, report_id)

        transcript = [
            {
                "ordinal": turn.ordinal,
                "question": turn.question,
                "answer": turn.answer,
                "private_score": turn.score,
                "private_scorable": turn.scorable,
                "score_source": turn.score_source,
                "private_deductions": turn.deductions,
                "topic": turn.topic,
                "dimension": turn.category,
                "input_mode": turn.input_mode,
                "answer_duration_seconds": turn.answer_duration_seconds,
                "speech_rate_cpm": turn.speech_rate_cpm,
                "transcript_edited": turn.transcript_edited,
                "recommended_answer_seconds": turn.recommended_answer_seconds,
            }
            for turn in turns
        ]
        prompt = {
            "interview_id": interview["id"],
            "report_id": report_id,
            "company": interview["company"],
            "interview_type": interview.get("interview_type") or "technical",
            "ended_reason": interview["end_reason"],
            "specialization": interview.get("specialization"),
            "language_mode": interview.get("language_mode") or "bilingual",
            "resume": interview.get("resume"),
            "transcript": transcript,
            "required": self._report_requirements(interview),
        }
        try:
            raw = await self.client.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            REPORT_SYSTEM_PROMPT_EN
                            if interview.get("language_mode") == "en"
                            else REPORT_SYSTEM_PROMPT
                        ),
                    },
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
                schema_version="2.0",
            )
            candidate = InterviewReport.model_validate(raw)
        except (ValidationError, LLMError, ValueError):
            candidate = self._deterministic_report(interview, turns, report_id)
        return self._normalize(candidate, interview, turns)

    @staticmethod
    def _report_requirements(interview: dict[str, Any]) -> str:
        interview_type = interview.get("interview_type") or "technical"
        hr_enabled = interview_type in {"hr", "technical_hr"}
        combined = interview_type == "technical_hr"
        if interview.get("language_mode") == "en":
            requirement = (
                "Write every narrative field in English. Include per-question deductions, "
                "improved answers, the four-dimensional rubric, topic scores, and a next-practice "
                "list. Analyze resume content, answer timing, wording, fluency when voice evidence "
                "exists, and role fit. Do not infer speech rate or fluency without voice evidence."
            )
            if hr_enabled:
                requirement += (
                    " Also analyze values "
                    "and company fit, career choices and planning, and the evidence and communication "
                    "behind compensation expectations. Never deduct points for the compensation number itself."
                )
                if combined:
                    requirement += " This was a combined technical and behavioral interview."
                else:
                    requirement += " This was a standalone behavioral/HR interview."
            return requirement
        return (
            "输出每题扣分点、改写示范、四维 rubric、知识点分数、下次必练清单，"
            "并分析简历内容、时间把握、措辞与岗位契合度；没有语音证据时不要猜语速或流畅度。"
            + (
                "本场是技术/综合（HR）面，还要基于对应问答分析价值观与公司契合、"
                "人生规划和选择逻辑、薪酬预期的依据与沟通方式；不得因薪酬数值本身扣分。"
                if hr_enabled
                else ""
            )
        )

    def _deterministic_report(
        self,
        interview: dict[str, Any],
        turns: list[InterviewTurn],
        report_id: str,
    ) -> InterviewReport:
        english = interview.get("language_mode") == "en"
        scores = self._dimension_scores(turns)
        rubric = self._rubric(scores, turns, english=english)
        overall = self._overall(rubric)
        topic_scores = self._topic_scores(turns)
        feedback = [
            QuestionFeedback(
                question=turn.question,
                answer=turn.answer,
                category=turn.category,
                score=turn.score,
                scorable=turn.scorable and turn.score is not None,
                status=(
                    "scored"
                    if turn.scorable and turn.score is not None
                    else "not_scorable"
                ),
                evidence=(
                    [self._turn_evidence(turn, english=english)]
                    if turn.scorable and turn.score is not None
                    else (
                        ["Scoring was unavailable for this turn; the transcript is retained without a numeric score."]
                        if english
                        else ["本轮评分服务不可用，问答原文保留但不据此生成数值分"]
                    )
                ),
                deductions=turn.deductions
                or (
                    (["The answer needs one verifiable metric, mechanism, or boundary condition."] if english else ["回答缺少一项可验证的数据、原理或边界说明"])
                    if turn.scorable
                    else (["This turn was unscorable and no points were deducted."] if english else ["本轮不可评分，未据此扣分"])
                ),
                better_answer=self._better_answer(turn, english=english),
                recommended_answer_seconds=turn.recommended_answer_seconds,
                answer_duration_seconds=turn.answer_duration_seconds,
                input_mode=turn.input_mode,
                transcript_edited=turn.transcript_edited,
                original_answer=turn.original_answer,
            )
            for turn in turns
        ]
        practice = self._practice_items(
            topic_scores,
            turns,
            language_mode=str(interview.get("language_mode") or "bilingual"),
        )
        focus = [item.topic for item in practice]
        scorable = any(turn.scorable and turn.score is not None for turn in turns)
        if english:
            summary = (
                f"This interview covered {len(turns)} answered questions. The next priorities are {', '.join(focus)}."
                if scorable and focus
                else (
                    f"This interview retained {len(turns)} answered questions, but scoring returned no verifiable numeric evidence. "
                    "No default score or weak-topic memory was created."
                )
            )
        else:
            summary = (
                f"本场完成 {len(turns)} 轮问答。"
                f"当前最需要补强的是{'、'.join(focus)}。"
                if scorable and focus
                else (
                    f"本场保留了 {len(turns)} 轮有效问答，但评分服务没有返回可验证的数值证据；"
                    "本次不生成默认分，也不写入后续弱项记忆。"
                )
            )
        report = InterviewReport(
            schema_version="2.0",
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
            scored=scorable,
            score_status="scored" if scorable else "unscorable",
            scoring_coverage=self._scoring_coverage(rubric),
        )
        return self._attach_extended_analysis(report, interview, turns)

    def _insufficient_data_report(
        self, interview: dict[str, Any], report_id: str
    ) -> InterviewReport:
        missing_scores = {dimension: None for dimension in RUBRIC_WEIGHTS}
        report = InterviewReport(
            schema_version="2.0",
            report_id=report_id,
            interview_id=interview["id"],
            company=interview["company"],
            scored=False,
            score_status="insufficient_data",
            overall_score=0.0,
            rubric=self._rubric(
                missing_scores,
                [],
                english=interview.get("language_mode") == "en",
            ),
            question_feedback=[],
            topic_scores={},
            must_practice=[],
            summary=(
                "There was no usable answer or transcript, so this interview is not scored and does not contribute weak-topic memory."
                if interview.get("language_mode") == "en"
                else "本场没有有效回答或可用转写，评分数据不足，因此本次不计分，也不会写入后续面试的弱项记忆。"
            ),
            next_focus=[],
            comparison={},
            memory_enabled=bool(interview.get("memory_enabled", True)),
            hint_count=len(interview.get("hint_events") or []),
            hint_events=interview.get("hint_events") or [],
            scoring_coverage=0.0,
        )
        return self._attach_extended_analysis(report, interview, [])

    def _normalize(
        self,
        candidate: InterviewReport,
        interview: dict[str, Any],
        turns: list[InterviewTurn],
    ) -> InterviewReport:
        english = interview.get("language_mode") == "en"
        scores = self._dimension_scores(turns)
        candidate.rubric = self._rubric(scores, turns, english=english)
        candidate.overall_score = self._overall(candidate.rubric)
        candidate.report_id = candidate.report_id or uuid.uuid4().hex
        candidate.interview_id = interview["id"]
        candidate.company = interview["company"]
        candidate.scored = any(
            turn.scorable and turn.score is not None for turn in turns
        )
        candidate.score_status = "scored" if candidate.scored else "unscorable"
        candidate.scoring_coverage = self._scoring_coverage(candidate.rubric)
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
                    scorable=turn.scorable and turn.score is not None,
                    status=(
                        "scored"
                        if turn.scorable and turn.score is not None
                        else "not_scorable"
                    ),
                    evidence=(
                        generated.evidence
                        if generated and generated.evidence
                        else (
                            [self._turn_evidence(turn, english=english)]
                            if turn.scorable and turn.score is not None
                            else (
                                ["Scoring was unavailable for this turn, so no numeric score was generated."]
                                if english
                                else ["本轮评分服务不可用，未生成数值分"]
                            )
                        )
                    ),
                    deductions=self._localized_deductions(
                        generated.deductions if generated else [],
                        turn.deductions,
                        english=english,
                        scorable=turn.scorable,
                    ),
                    better_answer=(generated.better_answer if generated else "")
                    or self._better_answer(turn, english=english),
                    recommended_answer_seconds=turn.recommended_answer_seconds,
                    answer_duration_seconds=turn.answer_duration_seconds,
                    input_mode=turn.input_mode,
                    transcript_edited=turn.transcript_edited,
                    original_answer=turn.original_answer,
                )
            )
        candidate.question_feedback = normalized_feedback
        candidate.topic_scores = self._topic_scores(turns)
        candidate.must_practice = self._practice_items(
            candidate.topic_scores,
            turns,
            language_mode=str(interview.get("language_mode") or "bilingual"),
        )
        candidate.next_focus = [item.topic for item in candidate.must_practice]
        if not candidate.scored:
            candidate.summary = (
                f"This interview retained {len(turns)} answered questions, but scoring returned no verifiable numeric evidence. "
                "No default score or weak-topic memory was created."
                if english
                else (
                    f"本场保留了 {len(turns)} 轮有效问答，但评分服务没有返回可验证的数值证据；"
                    "本次不生成默认分，也不写入后续弱项记忆。"
                )
            )
        return self._attach_extended_analysis(candidate, interview, turns)

    @staticmethod
    def _localized_deductions(
        generated: list[str],
        fallback: list[str],
        *,
        english: bool,
        scorable: bool,
    ) -> list[str]:
        if english:
            return generated or fallback or [
                "The answer is missing verifiable technical detail."
                if scorable
                else "This turn was unscorable and no points were deducted."
            ]
        localized = [
            item for item in generated
            if re.search(r"[\u3400-\u9fff]", str(item))
        ] or [
            item for item in fallback
            if re.search(r"[\u3400-\u9fff]", str(item))
        ]
        return localized or [
            "回答缺少可验证的关键细节"
            if scorable
            else "本轮不可评分，未据此扣分"
        ]

    @staticmethod
    def _dimension_scores(
        turns: list[InterviewTurn],
    ) -> dict[str, float | None]:
        grouped: dict[str, list[float]] = defaultdict(list)
        for turn in turns:
            if not turn.scorable or turn.score is None:
                continue
            grouped[turn.category].append(turn.score)
            if turn.category != "communication":
                # Expression is observed on every answer without a second LLM call.
                expression = min(10.0, max(0.0, turn.score + (0.5 if len(turn.answer) >= 60 else -0.5)))
                grouped["communication"].append(expression)
        return {
            dimension: round(sum(grouped[dimension]) / len(grouped[dimension]), 1)
            if grouped[dimension]
            else None
            for dimension in RUBRIC_WEIGHTS
        }

    @staticmethod
    def _rubric(
        scores: dict[str, float | None],
        turns: list[InterviewTurn],
        *,
        english: bool = False,
    ) -> Rubric:
        deductions_by_dimension: dict[str, list[str]] = defaultdict(list)
        evidence_by_dimension: dict[str, list[str]] = defaultdict(list)
        for turn in turns:
            if not turn.scorable or turn.score is None:
                continue
            deductions_by_dimension[turn.category].extend(turn.deductions)
            evidence = ReportEngine._turn_evidence(turn, english=english)
            evidence_by_dimension[turn.category].append(evidence)
            evidence_by_dimension["communication"].append(evidence)

        def dimension(name: str, weight: float) -> RubricDimension:
            score = scores[name]
            scorable = score is not None
            return RubricDimension(
                score=score,
                weight=weight,
                scorable=scorable,
                status="scored" if scorable else "not_observed",
                evidence=evidence_by_dimension[name][:4],
                deductions=(deductions_by_dimension[name][:4] if scorable else []),
            )

        return Rubric(
            project_depth=dimension("project_depth", 0.4),
            fundamentals=dimension("fundamentals", 0.3),
            coding_thought=dimension("coding_thought", 0.2),
            communication=dimension("communication", 0.1),
        )

    @staticmethod
    def _overall(rubric: Rubric) -> float:
        dimensions = (
            rubric.project_depth,
            rubric.fundamentals,
            rubric.coding_thought,
            rubric.communication,
        )
        observed = [item for item in dimensions if item.scorable and item.score is not None]
        weight = sum(item.weight for item in observed)
        if not weight:
            return 0.0
        return round(sum(float(item.score) * item.weight for item in observed) / weight, 1)

    @staticmethod
    def _scoring_coverage(rubric: Rubric) -> float:
        return round(
            sum(
                item.weight
                for item in (
                    rubric.project_depth,
                    rubric.fundamentals,
                    rubric.coding_thought,
                    rubric.communication,
                )
                if item.scorable and item.score is not None
            ),
            2,
        )

    @staticmethod
    def _topic_scores(turns: list[InterviewTurn]) -> dict[str, float]:
        grouped: dict[str, list[float]] = defaultdict(list)
        for turn in turns:
            if not turn.scorable or turn.score is None:
                continue
            grouped[canonical_topic(turn.topic, turn.category)].append(turn.score)
        return {
            topic: round(sum(values) / len(values), 1)
            for topic, values in grouped.items()
        }

    def _practice_items(
        self,
        topic_scores: dict[str, float],
        turns: list[InterviewTurn],
        language_mode: str = "bilingual",
    ) -> list[PracticeItem]:
        links = load_topic_links()
        weakest = sorted(topic_scores.items(), key=lambda item: item[1])[:3]
        result: list[PracticeItem] = []
        for topic, score in weakest:
            resource = self._resource_for_topic(topic, links)
            behavioral_guidance = BEHAVIORAL_PRACTICE_GUIDANCE.get(topic)
            display_topic = (
                ENGLISH_TOPIC_LABELS.get(topic, topic)
                if language_mode == "en"
                else topic
            )
            if behavioral_guidance:
                guidance = behavioral_guidance[
                    "en" if language_mode == "en" else "zh"
                ]
                reason = (
                    f"This behavioral topic scored {score:.1f}/10. {guidance}"
                    if language_mode == "en"
                    else f"本场该综合面主题得分 {score:.1f}/10。{guidance}"
                )
            else:
                reason = (
                    f"This topic scored {score:.1f}/10; prioritize the mechanism, evidence, and boundary conditions."
                    if language_mode == "en"
                    else f"本场该知识点得分 {score:.1f}/10，需要优先补齐原理、证据和边界。"
                )
            result.append(
                PracticeItem(
                    topic=display_topic,
                    reason=reason,
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
    def _better_answer(turn: InterviewTurn, *, english: bool = False) -> str:
        anchor = turn.anchor_keyword or turn.topic
        if english:
            if any("\u3400" <= char <= "\u9fff" for char in anchor):
                anchor = ENGLISH_TOPIC_LABELS.get(turn.topic, "the topic")
            lowered = turn.topic.lower()
            if "compensation" in lowered:
                return (
                    "I would state a realistic range and the evidence behind it, then rank compensation, "
                    "mentorship, growth, and role fit, and finish with what is negotiable."
                )
            if "career" in lowered or "planning" in lowered:
                return (
                    "I would explain why backend engineering fits me now, name one or two measurable "
                    "two-to-three-year goals, and support them with actions I have already started."
                )
            if "values" in lowered or "company fit" in lowered:
                return (
                    "I would use one real situation: the constraint, the evidence behind my decision, "
                    "my communication and action, the outcome, and what I would change next time."
                )
            if "自我介绍" in turn.topic:
                return (
                    "In one minute, I would cover my university and year, relevant coursework, current "
                    "learning focus, backend direction, and internship goal, leaving project details for the next question."
                )
            return (
                f"I would establish the context and goal, explain my own work around {anchor}, walk through "
                "the request path and mechanism, support the result with a baseline and measurement window, "
                "and finish with failure modes, alternatives, and trade-offs."
            )
        if "综合面·薪酬期待" in turn.topic:
            return (
                "我会先坦诚说明预期范围和参考依据；再说明薪酬、成长空间、团队带教和方向匹配"
                "在我这里的排序，以及为什么这样排序；最后明确可协商项和会直接影响选择的条件。"
            )
        if "综合面·人生规划与选择" in turn.topic:
            return (
                "我会先说明当前选择后端实习的具体原因，再把未来两三年的目标落到一两项能力；"
                "接着给出已经开始的课程、项目或实习行动，最后说明遇到预期变化时的判断和调整标准。"
            )
        if "综合面·价值观与公司契合" in turn.topic:
            return (
                "我会选一个真实场景，先交代当时的约束和分歧，再说明我依据哪些事实做判断、"
                "如何与团队沟通并采取行动；最后给出结果、复盘和下一次会改进的地方。"
            )
        if turn.topic == "自我介绍·整体与学习情况":
            return (
                "我会在一分钟内依次说明学校专业和年级、已完成的核心课程与当前学习重点、"
                "熟悉的技术方向和本次实习目标；项目细节留到面试官单独追问时再展开。"
            )
        return (
            f"我会先明确背景与目标，再说明我本人围绕“{anchor}”做的设计；"
            "随后按请求链路解释关键原理，用基线、统计窗口和结果数据验证收益；"
            "最后补充失败场景、替代方案与当前方案的取舍边界。"
        )

    @staticmethod
    def _turn_evidence(turn: InterviewTurn, *, english: bool = False) -> str:
        answer = " ".join(turn.answer.split())
        if len(answer) > 96:
            answer = answer[:93] + "…"
        return (
            f"Answer to question {turn.ordinal}: {answer}"
            if english
            else f"第{turn.ordinal}题回答：{answer}"
        )

    def _attach_extended_analysis(
        self,
        report: InterviewReport,
        interview: dict[str, Any],
        turns: list[InterviewTurn],
    ) -> InterviewReport:
        language_mode = str(interview.get("language_mode") or "bilingual")
        report.resume_analysis = self._resume_analysis(interview)
        report.process_analysis = self._process_analysis(
            turns,
            language_mode=language_mode,
        )
        report.role_fit = self._role_fit_analysis(interview)
        report.behavioral_analysis = self._behavioral_analysis(
            interview,
            turns,
            language_mode=language_mode,
        )
        # Citations are always overwritten from a reviewed static file.  Model
        # output is never allowed to introduce a report URL.
        report.company_insights = self._company_insights(interview["company"], report)
        report.radar = self._radar(report)
        return report

    @staticmethod
    def _behavioral_analysis(
        interview: dict[str, Any],
        turns: list[InterviewTurn],
        language_mode: str = "bilingual",
    ) -> BehavioralAnalysis:
        """Build HR dimensions only from turns that were actually observed.

        The turn scorer already returns a 0-10 communication/evidence score.
        We reuse those observed scores here instead of asking another model or
        manufacturing neutral defaults.  Compensation is judged on reasoning
        and communication only, never on the requested number.
        """

        interview_type = str(interview.get("interview_type") or "technical")
        if interview_type not in {"hr", "technical_hr"}:
            return BehavioralAnalysis()
        english = language_mode == "en"

        dimensions: dict[str, list[InterviewTurn]] = {
            "company_fit": [],
            "career_planning": [],
            "collaboration": [],
            "compensation_communication": [],
        }
        for turn in turns:
            topic = str(turn.topic or "").casefold()
            question = str(turn.question or "").casefold()
            haystack = f"{topic} {question}"
            if any(marker in haystack for marker in ("价值观", "公司契合", "company fit", "values")):
                dimensions["company_fit"].append(turn)
            if any(marker in haystack for marker in ("人生规划", "职业规划", "选择", "career", "planning")):
                dimensions["career_planning"].append(turn)
            if any(marker in haystack for marker in ("协作", "冲突", "团队", "collaboration", "conflict", "team")):
                dimensions["collaboration"].append(turn)
            if any(marker in haystack for marker in ("薪酬", "compensation", "salary", "negoti")):
                dimensions["compensation_communication"].append(turn)

        def analyze(
            key: str, label: str, suggestion: str
        ) -> EvidenceAnalysis:
            observed = [
                turn
                for turn in dimensions[key]
                if turn.scorable and turn.score is not None
            ]
            score = (
                round(sum(float(turn.score) for turn in observed) / len(observed), 1)
                if observed
                else None
            )
            return EvidenceAnalysis(
                score=score,
                scorable=score is not None,
                evidence=[
                    (
                        f"Question {turn.ordinal}: {' '.join(turn.answer.split())[:120]}"
                        if english
                        else f"第{turn.ordinal}题：{' '.join(turn.answer.split())[:120]}"
                    )
                    for turn in observed[:3]
                ],
                strengths=(
                    (
                        [f"The {label} answer included a scorable real experience or decision rationale."]
                        if english
                        else [f"{label}回答已有可评分的真实经历或判断依据。"]
                    )
                    if score is not None and score >= 7
                    else []
                ),
                weaknesses=(
                    (
                        [f"The {label} answer still lacked verifiable action, outcome, or reflection."]
                        if english
                        else [f"{label}回答仍缺少可核验的行动、结果或复盘。"]
                    )
                    if score is not None and score < 7
                    else []
                ),
                suggestions=[suggestion] if dimensions[key] else [],
            )

        return BehavioralAnalysis(
            company_fit=analyze(
                "company_fit",
                "values and company fit" if english else "价值观与公司契合",
                (
                    "Use one real trade-off to explain the evidence behind your decision, your own actions and outcome, then connect it to the target team's values and the areas where you would need to adapt."
                    if english
                    else "用一次真实取舍说明判断依据、个人行动和结果，再说明与目标团队的契合点及需要适应的地方。"
                ),
            ),
            career_planning=analyze(
                "career_planning",
                "career planning and choices" if english else "人生规划与选择",
                (
                    "Break the two-to-three-year direction into verifiable actions for the current semester, and explain how you would rank different internship choices."
                    if english
                    else "把两三年目标拆成当前学期的可验证行动，并给出选择不同实习机会时的排序依据。"
                ),
            ),
            collaboration=analyze(
                "collaboration",
                "collaboration and conflict handling" if english else "协作与冲突处理",
                (
                    "Cover the situation, disagreement, your own communication, the result, and your reflection instead of merely claiming that you communicate well."
                    if english
                    else "按情境、分歧、本人沟通动作、结果与复盘描述，不要只说自己善于沟通。"
                ),
            ),
            compensation_communication=analyze(
                "compensation_communication",
                "compensation communication" if english else "薪酬沟通",
                (
                    "Explain your information sources, how you rank compensation against mentorship, role direction, and growth, and your negotiable boundaries; the specific number itself is not scored."
                    if english
                    else "说明信息来源、薪酬与导师/方向/成长的排序，以及可协商边界；系统不按具体数值扣分。"
                ),
            ),
        )

    @staticmethod
    def _resume_analysis(interview: dict[str, Any]) -> ResumeAnalysis:
        raw = interview.get("resume") or {}
        education = raw.get("教育") or raw.get("education") or []
        internships = raw.get("实习经历") or raw.get("internships") or []
        projects = raw.get("项目") or raw.get("projects") or []
        skills = raw.get("技能") or raw.get("skills") or []
        metrics: list[str] = []
        highlights: list[str] = []
        for item in [*internships, *projects]:
            if not isinstance(item, dict):
                continue
            metrics.extend(str(value) for value in (item.get("metrics") or []))
            highlights.extend(str(value) for value in (item.get("highlights") or []))

        evidence = [
            f"结构化简历包含教育经历 {len(education)} 段、实习 {len(internships)} 段、项目 {len(projects)} 个、技能 {len(skills)} 项。",
            f"可识别量化指标 {len(metrics)} 条、项目/实习要点 {len(highlights)} 条。",
        ]
        has_content = bool(education or internships or projects or skills)
        score = None
        if has_content:
            score = round(
                min(
                    10.0,
                    (1.5 if education else 0)
                    + (2.0 if internships else 0)
                    + (3.0 if projects else 0)
                    + (1.5 if skills else 0)
                    + min(2.0, len(metrics) * 0.7),
                ),
                1,
            )
        strengths: list[str] = []
        weaknesses: list[str] = []
        if projects:
            strengths.append(f"有 {len(projects)} 个可用于项目深挖的后端项目。")
        else:
            weaknesses.append("缺少可供面试官深挖的项目经历。")
        if metrics:
            strengths.append("包含量化结果，便于说明产出和验证口径。")
        else:
            weaknesses.append("项目要点缺少基线、统计窗口和结果指标。")
        if not internships:
            weaknesses.append("未呈现实习场景下的个人职责和协作边界。")
        content_suggestions = [
            "每个项目按“业务目标—个人职责—关键链路—技术取舍—量化结果”重写 2 至 4 条。",
            "指标同时注明基线、统计窗口和本人动作，避免只写孤立百分比。",
            "技能列表只保留能承受至少两层追问的技术，并与项目要点相互印证。",
        ]
        rewritten: list[str] = []
        if highlights:
            rewritten.append(
                f"负责核心后端链路（原始要点：{highlights[0][:60]}），补充请求规模、本人改动、方案取舍与上线结果。"
            )
        if metrics:
            rewritten.append(
                f"在明确基线与统计窗口后，将关键指标优化至“{metrics[0][:60]}”，并说明压测/监控口径。"
            )
        return ResumeAnalysis(
            overall=EvidenceAnalysis(
                score=score,
                scorable=score is not None,
                evidence=evidence,
                strengths=strengths,
                weaknesses=weaknesses,
                suggestions=content_suggestions,
            ),
            strengths=strengths,
            weaknesses=weaknesses,
            content_suggestions=content_suggestions,
            layout_suggestions=[
                "当前系统只保存 PDF 文本解析后的结构化内容，未保留字体、留白、分页和视觉层级；因此不对排版打分。",
                "人工复核时建议控制一页、统一日期格式，并让项目名/角色/指标形成清晰视觉层级。",
            ],
            layout_scorable=False,
            layout_evidence=["未保存原 PDF 页面图像或坐标布局，缺少视觉排版证据。"],
            rewritten_examples=rewritten,
        )

    @staticmethod
    def _process_analysis(
        turns: list[InterviewTurn], language_mode: str = "bilingual"
    ) -> ProcessAnalysis:
        """Analyze delivery without mixing text timing and voice-only signals.

        Per-question duration includes thinking time from question delivery to
        submission for both text and voice answers. Speech rate uses the
        capture-only rate persisted for voice turns, while transcript fluency
        remains voice-only. Chinese uses characters per minute, English uses
        words per minute, and bilingual answers are scored per turn with the
        matching unit before their scores are averaged.
        """

        mode = language_mode if language_mode in {"zh", "en", "bilingual"} else "bilingual"
        english_report = mode == "en"
        timed_answers = [
            turn
            for turn in turns
            if turn.answer_duration_seconds is not None
            and turn.answer_duration_seconds > 0
        ]
        voice_timed = [turn for turn in timed_answers if turn.input_mode == "voice"]

        time_score: float | None = None
        time_evidence: list[str] = []
        if timed_answers:
            per_turn: list[float] = []
            for turn in timed_answers:
                duration = float(turn.answer_duration_seconds)
                ratio = duration / max(1, turn.recommended_answer_seconds)
                if 0.5 <= ratio <= 1.25:
                    per_turn.append(9.0)
                elif 0.3 <= ratio <= 1.6:
                    per_turn.append(7.0)
                else:
                    per_turn.append(4.5)
                input_label = (
                    "voice" if turn.input_mode == "voice" else "text"
                ) if english_report else (
                    "语音" if turn.input_mode == "voice" else "文字"
                )
                time_evidence.append(
                    (
                        f"Question {turn.ordinal} ({input_label}) took {duration:.1f}s; the suggested time was {turn.recommended_answer_seconds}s."
                        if english_report
                        else f"第{turn.ordinal}题（{input_label}）实际 {duration:.1f}s，建议 {turn.recommended_answer_seconds}s。"
                    )
                )
            time_score = round(sum(per_turn) / len(per_turn), 1)

        english_word_pattern = re.compile(
            r"[A-Za-z]+(?:['’-][A-Za-z]+)*|\d+(?:\.\d+)?"
        )

        def counts(text: str) -> tuple[int, int, int]:
            cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
            english_words = len(english_word_pattern.findall(text))
            latin_chars = len(re.findall(r"[A-Za-z]", text))
            return cjk_count, english_words, latin_chars

        def turn_language(text: str) -> str:
            if mode in {"zh", "en"}:
                return mode
            cjk_count, english_words, latin_chars = counts(text)
            if english_words >= 3 and latin_chars > cjk_count * 2:
                return "en"
            return "zh"

        rate_samples: list[tuple[str, float, float]] = []
        for turn in voice_timed:
            cjk_count, english_words, _ = counts(turn.answer)
            sample_language = turn_language(turn.answer)
            units = (
                english_words
                if sample_language == "en"
                else cjk_count + english_words
            )
            if units <= 0:
                continue
            captured_units = len(re.sub(r"\s+", "", turn.answer))
            rate = (
                units * float(turn.speech_rate_cpm) / captured_units
                if turn.speech_rate_cpm is not None
                and turn.speech_rate_cpm > 0
                and captured_units > 0
                else units * 60 / float(turn.answer_duration_seconds)
            )
            if sample_language == "en":
                score = 9.0 if 110 <= rate <= 180 else 7.0 if 85 <= rate <= 210 else 4.5
            else:
                score = 9.0 if 180 <= rate <= 320 else 7.0 if 120 <= rate <= 400 else 4.5
            rate_samples.append((sample_language, rate, score))

        rate_score = (
            round(sum(sample[2] for sample in rate_samples) / len(rate_samples), 1)
            if rate_samples
            else None
        )
        chinese_rates = [rate for language, rate, _ in rate_samples if language == "zh"]
        english_rates = [rate for language, rate, _ in rate_samples if language == "en"]
        average_chinese_rate = (
            round(sum(chinese_rates) / len(chinese_rates), 1)
            if chinese_rates
            else None
        )
        average_english_rate = (
            round(sum(english_rates) / len(english_rates), 1)
            if english_rates
            else None
        )
        if not rate_samples:
            rate_evidence = [
                (
                    "No voice answer had a usable transcript and recorded voice duration, so speaking rate is unscorable."
                    if english_report
                    else "本场没有同时具备有效转写和语音时长的回答，无法估算语速。"
                )
            ]
        elif english_report:
            rate_evidence = [
                f"{len(english_rates)} voice answers averaged {average_english_rate:.1f} words per minute; the score uses an English-speaking range rather than Chinese character thresholds."
            ]
        else:
            rate_evidence = []
            if chinese_rates:
                rate_evidence.append(
                    f"{len(chinese_rates)} 个中文语音回答平均 {average_chinese_rate:.1f} 字/分钟。"
                )
            if english_rates:
                rate_evidence.append(
                    f"{len(english_rates)} 个英文语音回答平均 {average_english_rate:.1f} 词/分钟。"
                )
            if mode == "bilingual" and chinese_rates and english_rates:
                rate_evidence.append("双语场按每段主要语言分别计速和评分，不混算字/分钟与词/分钟。")

        zh_structure_markers = (
            "首先", "第一", "其次", "然后", "最后", "因为", "所以",
            "因此", "但是", "不过", "取舍", "结论", "边界", "例如",
        )
        en_structure_pattern = re.compile(
            r"\b(?:first(?:ly)?|first of all|second(?:ly)?|then|finally|because|"
            r"therefore|so|however|trade[ -]?off|in conclusion|boundary|"
            r"for example|as a result|on the one hand)\b",
            re.IGNORECASE,
        )
        structured = 0
        quantified = 0
        for turn in turns:
            has_zh_structure = any(word in turn.answer for word in zh_structure_markers)
            has_en_structure = bool(en_structure_pattern.search(turn.answer))
            if (
                (mode == "zh" and has_zh_structure)
                or (mode == "en" and has_en_structure)
                or (mode == "bilingual" and (has_zh_structure or has_en_structure))
            ):
                structured += 1
            if any(char.isdigit() for char in turn.answer):
                quantified += 1
        wording_score: float | None = None
        wording_evidence: list[str] = []
        if turns:
            structured_ratio = structured / len(turns)
            metric_ratio = quantified / len(turns)
            wording_score = round(
                min(10.0, 4.0 + 3.5 * structured_ratio + 2.5 * metric_ratio), 1
            )
            wording_evidence = [
                (
                    f"Of {len(turns)} answers, {structured} used English causal or layered transitions and {quantified} included numeric evidence."
                    if english_report
                    else f"{len(turns)} 个回答中，{structured} 个包含与本场语言匹配的因果/分层连接词，{quantified} 个包含数字证据。"
                )
            ]

        voice_turns = [turn for turn in turns if turn.input_mode == "voice"]
        zh_filler_markers = ("嗯", "呃", "额", "那个", "就是说", "怎么说", "然后就是")
        en_filler_pattern = re.compile(
            r"\b(?:um+|uh+|erm+|you know|i mean|sort of|kind of)\b",
            re.IGNORECASE,
        )
        zh_filler_count = sum(
            turn.answer.count(marker)
            for turn in voice_turns
            for marker in zh_filler_markers
        )
        en_filler_count = sum(
            len(en_filler_pattern.findall(turn.answer)) for turn in voice_turns
        )
        cjk_units = sum(counts(turn.answer)[0] for turn in voice_turns)
        english_units = sum(counts(turn.answer)[1] for turn in voice_turns)
        if mode == "zh":
            filler_count = zh_filler_count
            fluency_units = cjk_units + english_units
            fluent_unit_label = "字"
            good_filler_limit, acceptable_filler_limit = 0.5, 2.0
        elif mode == "en":
            filler_count = en_filler_count
            fluency_units = english_units
            fluent_unit_label = "words"
            good_filler_limit, acceptable_filler_limit = 1.0, 3.0
        else:
            filler_count = zh_filler_count + en_filler_count
            fluency_units = cjk_units + english_units
            fluent_unit_label = "口语单位"
            good_filler_limit, acceptable_filler_limit = 0.75, 2.5
        filler_per_hundred = (
            round(filler_count * 100 / fluency_units, 2) if fluency_units else None
        )
        fluency_score: float | None = None
        if filler_per_hundred is not None:
            if filler_per_hundred <= good_filler_limit:
                fluency_score = 8.5
            elif filler_per_hundred <= acceptable_filler_limit:
                fluency_score = 7.0
            else:
                fluency_score = 4.5

        if filler_per_hundred is None:
            fluency_evidence = [
                "There was no voice transcript, so spoken fluency is unscorable."
                if english_report
                else "本场没有语音回答，无法评价口语流畅度。"
            ]
        elif english_report:
            fluency_evidence = [
                f"Across {len(voice_turns)} voice transcripts ({fluency_units} {fluent_unit_label}), {filler_count} common English filler expressions were found ({filler_per_hundred:.2f} per 100 words).",
                "This score covers transcript-level verbal fluency only. Pause distribution, pitch, and raw audio were not retained, so acoustic fluency is not scored.",
            ]
        else:
            fluency_evidence = [
                f"{len(voice_turns)} 个语音回答的最终转写共 {fluency_units} {fluent_unit_label}，识别到 {filler_count} 个与本场语言匹配的常见填充词（每百{fluent_unit_label} {filler_per_hundred:.2f} 个）。",
                "该分数只反映转写中的词语流畅度；系统未保存停顿分布、音高或原始音频，因此不评价声学流畅度。",
            ]

        return ProcessAnalysis(
            time_control=EvidenceAnalysis(
                score=time_score,
                scorable=time_score is not None,
                evidence=time_evidence,
                suggestions=[
                    "Aim to give the conclusion first within 50%-125% of the suggested time, then add evidence and boundaries."
                    if english_report
                    else "优先在建议时长的 50%—125% 内先给结论，再补证据与边界。"
                ],
            ),
            speech_rate=EvidenceAnalysis(
                score=rate_score,
                scorable=rate_score is not None,
                evidence=rate_evidence,
                suggestions=[
                    "Keep a steady pace and pause around terminology, numbers, and conclusions."
                    if english_report
                    else "技术回答保持稳定节奏，在术语、数字和结论前后主动停顿。"
                ],
            ),
            wording=EvidenceAnalysis(
                score=wording_score,
                scorable=wording_score is not None,
                evidence=wording_evidence,
                suggestions=[
                    "Use a conclusion-evidence-trade-off-boundary structure and avoid vague references."
                    if english_report
                    else "使用“结论—依据—取舍—边界”结构，减少无指代的“这个/然后/就是”。"
                ],
            ),
            fluency=EvidenceAnalysis(
                score=fluency_score,
                scorable=fluency_score is not None,
                evidence=fluency_evidence,
                weaknesses=(
                    [
                        "Filler density was high enough to obscure conclusions and technical terms."
                        if english_report
                        else "填充词密度偏高，容易削弱结论和关键术语的清晰度。"
                    ]
                    if filler_per_hundred is not None
                    and filler_per_hundred > acceptable_filler_limit
                    else []
                ),
                suggestions=[
                    "Replace fillers with short conclusion-evidence-boundary sentences; separately mark pauses over two seconds when reviewing audio."
                    if english_report
                    else "用“结论—依据—边界”短句替代填充词；录音回听时另外标记两秒以上停顿和重复起句。"
                ],
            ),
            average_answer_seconds=(
                round(
                    sum(float(turn.answer_duration_seconds) for turn in timed_answers)
                    / len(timed_answers),
                    1,
                )
                if timed_answers
                else None
            ),
            # The public field is explicitly characters/minute. Do not put
            # English WPM into it; English and mixed-language rates remain in
            # the evidence above so clients cannot mislabel their unit.
            average_speech_rate_cpm=(
                average_chinese_rate if chinese_rates and not english_rates else None
            ),
        )

    @staticmethod
    def _role_fit_analysis(interview: dict[str, Any]) -> RoleFitAnalysis:
        raw = interview.get("resume") or {}
        skills = [str(item) for item in (raw.get("技能") or raw.get("skills") or [])]
        projects = raw.get("项目") or raw.get("projects") or []
        for project in projects:
            if isinstance(project, dict):
                skills.extend(str(item) for item in (project.get("technologies") or []))
        normalized = " ".join(skills).lower()
        requirements = {
            "后端语言": ("java", "go", "python", "c++"),
            "关系型数据库": ("mysql", "postgres", "sql"),
            "缓存": ("redis", "cache", "缓存"),
            "并发/网络": ("并发", "thread", "线程", "tcp", "http", "网络"),
        }
        matched = [
            label
            for label, markers in requirements.items()
            if any(marker in normalized for marker in markers)
        ]
        gaps = [label for label in requirements if label not in matched]
        score = (
            round(3.0 + 7.0 * len(matched) / len(requirements), 1)
            if skills
            else None
        )
        specialization = str(interview.get("specialization") or "通用后端")
        return RoleFitAnalysis(
            overall=EvidenceAnalysis(
                score=score,
                scorable=score is not None,
                evidence=[
                    f"简历技能/项目技术栈覆盖：{'、'.join(matched) if matched else '未识别到核心后端类别'}；目标方向：{specialization}。"
                ],
                strengths=[f"已覆盖{item}" for item in matched],
                weaknesses=[f"简历尚未证明{item}" for item in gaps],
                suggestions=["用项目证据证明目标岗位要求，不要只在技能栏罗列名词。"],
            ),
            matched_requirements=matched,
            gaps=gaps,
            improvement_plan=[
                *[f"为“{item}”补一个项目场景、原理说明和故障边界。" for item in gaps[:3]],
                f"围绕“{specialization}”准备一段两分钟岗位契合陈述。",
            ],
        )

    @staticmethod
    def _company_insights(
        company: str, report: InterviewReport | None = None
    ) -> CompanyInsights:
        path = ROOT_DIR / "resources" / "company_interview_experiences.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            item = (payload.get("companies") or {}).get(company) or {}
        except (OSError, ValueError, json.JSONDecodeError):
            item = {}
        if not item:
            card = load_style_card(company)
            preferences = [
                str(value) for value in (card.get("followup_preferences") or [])
            ]
            hr_focus = [
                str(value) for value in (card.get("technical_hr_focus") or [])
            ]
            item = {
                "display_name": COMPANIES.get(company, company),
                "sample_caveat": (
                    "当前资料只足以归纳练习侧重，尚无可在报告中逐帖引用的授权样本；"
                    "以下建议不是该公司官方标准，不补造面经链接。"
                ),
                "trend_summary": preferences,
                "report_advice": [
                    "项目回答准备个人职责、完整请求链路、量化口径、故障边界和替代方案。",
                    *[f"综合面额外准备：{focus}。" for focus in hr_focus[:3]],
                ],
                "sources": [],
            }
        citations = [
            CompanyExperienceCitation(
                title=str(source.get("title") or ""),
                url=str(source.get("url") or ""),
                platform=str(source.get("platform") or ""),
                published_at=str(source.get("published_at") or ""),
                round=str(source.get("round") or ""),
                report_takeaway=str(source.get("report_takeaway") or ""),
                takeaways=[str(value) for value in (source.get("signals") or [])],
            )
            for source in (item.get("sources") or [])
            if isinstance(source, dict)
            and str(source.get("url") or "").startswith("https://")
        ]
        advice = [str(value) for value in (item.get("report_advice") or [])]
        personalized: list[str] = []
        if report is not None and report.scored:
            weak_topics = [str(value) for value in report.next_focus]
            weak_topics.extend(
                topic
                for topic, score in report.topic_scores.items()
                if float(score) < 6.5
            )
            weak_text = " ".join(weak_topics).casefold()
            topic_groups = {
                "项目与系统设计": ("项目", "架构", "链路", "选型", "场景", "系统设计"),
                "数据库与缓存": ("mysql", "redis", "数据库", "缓存", "索引", "事务", "一致性"),
                "并发、网络与运行时": ("并发", "线程", "锁", "jvm", "网络", "http", "tcp", "linux"),
                "算法与手撕": ("算法", "手撕", "复杂度", "数据结构", "coding"),
                "表达与综合面": ("表达", "沟通", "价值观", "规划", "薪酬", "company fit"),
            }
            weak_groups = [
                (label, markers)
                for label, markers in topic_groups.items()
                if any(marker in weak_text for marker in markers)
            ]

            def relevance(value: str) -> int:
                lowered = value.casefold()
                return sum(
                    1
                    for _label, markers in weak_groups
                    if any(marker in lowered for marker in markers)
                )

            ranked_advice = sorted(
                enumerate(advice), key=lambda pair: (-relevance(pair[1]), pair[0])
            )
            for _index, value in ranked_advice:
                matched_label = next(
                    (
                        label
                        for label, markers in weak_groups
                        if any(marker in value.casefold() for marker in markers)
                    ),
                    "",
                )
                if matched_label:
                    personalized.append(f"针对本次的“{matched_label}”弱项：{value}")
                if len(personalized) >= 3:
                    break
            if weak_groups and not personalized and advice:
                personalized.append(
                    f"本次优先补“{weak_groups[0][0]}”：{advice[0]}"
                )

            def citation_relevance(value: CompanyExperienceCitation) -> int:
                return relevance(
                    " ".join([value.report_takeaway, *value.takeaways])
                )

            citations.sort(key=lambda value: -citation_relevance(value))
        return CompanyInsights(
            company_label=str(item.get("display_name") or COMPANIES.get(company, company)),
            sample_caveat=str(item.get("sample_caveat") or ""),
            recurring_patterns=[str(value) for value in (item.get("trend_summary") or [])],
            interview_advice=advice,
            personalized_advice=personalized,
            citations=citations,
        )

    @staticmethod
    def _radar(report: InterviewReport) -> list[RadarAxis]:
        axes: list[RadarAxis] = []

        def add(key: str, label: str, analysis: EvidenceAnalysis) -> None:
            axes.append(
                RadarAxis(
                    key=key,
                    label=label,
                    score=analysis.score,
                    scorable=analysis.scorable and analysis.score is not None,
                    evidence=analysis.evidence,
                )
            )

        rubric_labels = {
            "project_depth": "项目深度",
            "fundamentals": "基础八股",
            "coding_thought": "手撕思路",
            "communication": "表达逻辑",
        }
        for key, label in rubric_labels.items():
            item = getattr(report.rubric, key)
            axes.append(
                RadarAxis(
                    key=key,
                    label=label,
                    score=item.score,
                    scorable=item.scorable and item.score is not None,
                    evidence=item.evidence,
                )
            )
        add("resume_content", "简历内容", report.resume_analysis.overall)
        add("time_control", "时间把握", report.process_analysis.time_control)
        add("speech_rate", "语速", report.process_analysis.speech_rate)
        add("wording", "措辞结构", report.process_analysis.wording)
        add("fluency", "流畅度", report.process_analysis.fluency)
        add("role_fit", "岗位契合", report.role_fit.overall)
        behavioral = report.behavioral_analysis
        if any(
            item.scorable
            for item in (
                behavioral.company_fit,
                behavioral.career_planning,
                behavioral.collaboration,
                behavioral.compensation_communication,
            )
        ):
            add("company_fit", "价值观契合", behavioral.company_fit)
            add("career_planning", "规划选择", behavioral.career_planning)
            add("collaboration", "协作沟通", behavioral.collaboration)
            add(
                "compensation_communication",
                "薪酬沟通",
                behavioral.compensation_communication,
            )
        return axes

    @staticmethod
    def _comparison(
        previous: dict[str, Any] | None, current: InterviewReport
    ) -> dict[str, Any]:
        if (
            not previous
            or not current.scored
            or not previous.get("scored", True)
            or previous.get("score_status", "scored") != "scored"
        ):
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
