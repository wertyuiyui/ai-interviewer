from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from typing import Any

from pydantic import ValidationError

from .config import ROOT_DIR, Settings, get_settings
from .content import COMPANIES, load_topic_links
from .db import Database
from .errors import AppError, LLMError
from .llm import BailianChatClient
from .schemas import (
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


REPORT_SYSTEM_PROMPT = """你是严格但建设性的技术面试复盘官。面试已经结束，现在才允许点评。
根据完整逐题转写输出结构化 JSON 报告。每道题必须有具体扣分点和一段可直接学习的改写示范；不得笼统说“需要加强”。
评分 rubric 固定：项目深度40%、基础八股30%、手撕思路20%、表达逻辑10%。
不要编造候选人未说过的项目事实。没有观测证据的维度必须标记不可评分，不得填写5分或其他中间值。
不要生成题库来源、帖子标题或 URL；大厂真实面经引用由服务端可信资料覆盖。"""


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
            "ended_reason": interview["end_reason"],
            "specialization": interview.get("specialization"),
            "resume": interview.get("resume"),
            "transcript": transcript,
            "required": (
                "输出每题扣分点、改写示范、四维 rubric、知识点分数、下次必练清单，"
                "并分析简历内容、时间把握、措辞与岗位契合度；没有语音证据时不要猜语速或流畅度"
            ),
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
                schema_version="2.0",
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
                scorable=turn.scorable and turn.score is not None,
                status=(
                    "scored"
                    if turn.scorable and turn.score is not None
                    else "not_scorable"
                ),
                evidence=(
                    [self._turn_evidence(turn)]
                    if turn.scorable and turn.score is not None
                    else ["本轮评分服务不可用，问答原文保留但不据此生成数值分"]
                ),
                deductions=turn.deductions
                or (
                    ["回答缺少一项可验证的数据、原理或边界说明"]
                    if turn.scorable
                    else ["本轮不可评分，未据此扣分"]
                ),
                better_answer=self._better_answer(turn),
                recommended_answer_seconds=turn.recommended_answer_seconds,
                answer_duration_seconds=turn.answer_duration_seconds,
                input_mode=turn.input_mode,
                transcript_edited=turn.transcript_edited,
                original_answer=turn.original_answer,
            )
            for turn in turns
        ]
        practice = self._practice_items(topic_scores, turns)
        focus = [item.topic for item in practice]
        scorable = any(turn.scorable and turn.score is not None for turn in turns)
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
            rubric=self._rubric(missing_scores, []),
            question_feedback=[],
            topic_scores={},
            must_practice=[],
            summary=(
                "本场没有有效回答或可用转写，评分数据不足，因此本次不计分，"
                "也不会写入后续面试的弱项记忆。"
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
        scores = self._dimension_scores(turns)
        candidate.rubric = self._rubric(scores, turns)
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
                            [self._turn_evidence(turn)]
                            if turn.scorable and turn.score is not None
                            else ["本轮评分服务不可用，未生成数值分"]
                        )
                    ),
                    deductions=(generated.deductions if generated else turn.deductions)
                    or (
                        ["回答缺少可验证的关键细节"]
                        if turn.scorable
                        else ["本轮不可评分，未据此扣分"]
                    ),
                    better_answer=(generated.better_answer if generated else "")
                    or self._better_answer(turn),
                    recommended_answer_seconds=turn.recommended_answer_seconds,
                    answer_duration_seconds=turn.answer_duration_seconds,
                    input_mode=turn.input_mode,
                    transcript_edited=turn.transcript_edited,
                    original_answer=turn.original_answer,
                )
            )
        candidate.question_feedback = normalized_feedback
        candidate.topic_scores = self._topic_scores(turns)
        candidate.must_practice = self._practice_items(candidate.topic_scores, turns)
        candidate.next_focus = [item.topic for item in candidate.must_practice]
        if not candidate.scored:
            candidate.summary = (
                f"本场保留了 {len(turns)} 轮有效问答，但评分服务没有返回可验证的数值证据；"
                "本次不生成默认分，也不写入后续弱项记忆。"
            )
        return self._attach_extended_analysis(candidate, interview, turns)

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
        scores: dict[str, float | None], turns: list[InterviewTurn]
    ) -> Rubric:
        deductions_by_dimension: dict[str, list[str]] = defaultdict(list)
        evidence_by_dimension: dict[str, list[str]] = defaultdict(list)
        for turn in turns:
            if not turn.scorable or turn.score is None:
                continue
            deductions_by_dimension[turn.category].extend(turn.deductions)
            evidence = ReportEngine._turn_evidence(turn)
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
        self, topic_scores: dict[str, float], turns: list[InterviewTurn]
    ) -> list[PracticeItem]:
        links = load_topic_links()
        weakest = sorted(topic_scores.items(), key=lambda item: item[1])[:3]
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
    def _turn_evidence(turn: InterviewTurn) -> str:
        answer = " ".join(turn.answer.split())
        if len(answer) > 96:
            answer = answer[:93] + "…"
        return f"第{turn.ordinal}题回答：{answer}"

    def _attach_extended_analysis(
        self,
        report: InterviewReport,
        interview: dict[str, Any],
        turns: list[InterviewTurn],
    ) -> InterviewReport:
        report.resume_analysis = self._resume_analysis(interview)
        report.process_analysis = self._process_analysis(turns)
        report.role_fit = self._role_fit_analysis(interview)
        # Citations are always overwritten from a reviewed static file.  Model
        # output is never allowed to introduce a report URL.
        report.company_insights = self._company_insights(interview["company"])
        report.radar = self._radar(report)
        return report

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
    def _process_analysis(turns: list[InterviewTurn]) -> ProcessAnalysis:
        timed = [
            turn
            for turn in turns
            if turn.input_mode == "voice"
            and turn.answer_duration_seconds is not None
            and turn.answer_duration_seconds > 0
        ]
        rates = [
            float(turn.speech_rate_cpm)
            for turn in timed
            if turn.speech_rate_cpm is not None
        ]
        time_score: float | None = None
        time_evidence: list[str] = []
        if timed:
            per_turn: list[float] = []
            for turn in timed:
                ratio = float(turn.answer_duration_seconds) / max(
                    1, turn.recommended_answer_seconds
                )
                if 0.5 <= ratio <= 1.25:
                    per_turn.append(9.0)
                elif 0.3 <= ratio <= 1.6:
                    per_turn.append(7.0)
                else:
                    per_turn.append(4.5)
                time_evidence.append(
                    f"第{turn.ordinal}题实际 {turn.answer_duration_seconds:.1f}s，建议 {turn.recommended_answer_seconds}s。"
                )
            time_score = round(sum(per_turn) / len(per_turn), 1)
        rate_score: float | None = None
        average_rate = round(sum(rates) / len(rates), 1) if rates else None
        if average_rate is not None:
            if 160 <= average_rate <= 300:
                rate_score = 9.0
            elif 120 <= average_rate <= 360:
                rate_score = 7.0
            else:
                rate_score = 4.5

        structured = 0
        quantified = 0
        for turn in turns:
            if any(word in turn.answer for word in ("首先", "其次", "最后", "因为", "所以", "取舍")):
                structured += 1
            if any(char.isdigit() for char in turn.answer):
                quantified += 1
        wording_score: float | None = None
        wording_evidence: list[str] = []
        if turns:
            structured_ratio = structured / len(turns)
            metric_ratio = quantified / len(turns)
            wording_score = round(min(10.0, 4.0 + 3.5 * structured_ratio + 2.5 * metric_ratio), 1)
            wording_evidence = [
                f"{len(turns)} 个回答中，{structured} 个包含因果/分层连接词，{quantified} 个包含数字证据。"
            ]
        voice_turns = [turn for turn in turns if turn.input_mode == "voice"]
        filler_markers = ("嗯", "呃", "额", "那个", "就是说", "怎么说", "然后就是")
        filler_count = sum(
            turn.answer.count(marker)
            for turn in voice_turns
            for marker in filler_markers
        )
        voice_chars = sum(
            len("".join(turn.answer.split())) for turn in voice_turns
        )
        filler_per_hundred = (
            round(filler_count * 100 / voice_chars, 2) if voice_chars else None
        )
        fluency_score: float | None = None
        if filler_per_hundred is not None:
            if filler_per_hundred <= 0.5:
                fluency_score = 8.5
            elif filler_per_hundred <= 2:
                fluency_score = 7.0
            else:
                fluency_score = 4.5
        return ProcessAnalysis(
            time_control=EvidenceAnalysis(
                score=time_score,
                scorable=time_score is not None,
                evidence=time_evidence,
                suggestions=["优先在建议时长的 50%—125% 内先给结论，再补证据与边界。"],
            ),
            speech_rate=EvidenceAnalysis(
                score=rate_score,
                scorable=rate_score is not None,
                evidence=(
                    [f"{len(rates)} 个语音回答的平均转写字符速率为 {average_rate:.1f} 字/分钟。"]
                    if average_rate is not None
                    else ["本场没有带有效 VAD 时长的语音回答，无法估算语速。"]
                ),
                suggestions=["技术回答保持稳定节奏，在术语、数字和结论前后主动停顿。"],
            ),
            wording=EvidenceAnalysis(
                score=wording_score,
                scorable=wording_score is not None,
                evidence=wording_evidence,
                suggestions=["使用“结论—依据—取舍—边界”结构，减少无指代的“这个/然后/就是”。"],
            ),
            fluency=EvidenceAnalysis(
                score=fluency_score,
                scorable=fluency_score is not None,
                evidence=(
                    [
                        f"{len(voice_turns)} 个语音回答的最终转写共 {voice_chars} 字，识别到 {filler_count} 个常见填充词（每百字 {filler_per_hundred:.2f} 个）。",
                        "该分数只反映转写中的词语流畅度；系统未保存停顿分布、音高或原始音频，因此不评价声学流畅度。",
                    ]
                    if filler_per_hundred is not None
                    else ["本场没有语音回答，无法评价口语流畅度。"]
                ),
                weaknesses=(
                    ["填充词密度偏高，容易削弱结论和关键术语的清晰度。"]
                    if filler_per_hundred is not None and filler_per_hundred > 2
                    else []
                ),
                suggestions=["用“结论—依据—边界”短句替代填充词；录音回听时另外标记两秒以上停顿和重复起句。"],
            ),
            average_answer_seconds=(
                round(
                    sum(float(turn.answer_duration_seconds) for turn in timed) / len(timed),
                    1,
                )
                if timed
                else None
            ),
            average_speech_rate_cpm=average_rate,
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
    def _company_insights(company: str) -> CompanyInsights:
        path = ROOT_DIR / "resources" / "company_interview_experiences.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            item = (payload.get("companies") or {}).get(company) or {}
        except (OSError, ValueError, json.JSONDecodeError):
            item = {}
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
        return CompanyInsights(
            company_label=str(item.get("display_name") or COMPANIES.get(company, company)),
            sample_caveat=str(item.get("sample_caveat") or ""),
            recurring_patterns=[str(value) for value in (item.get("trend_summary") or [])],
            interview_advice=[str(value) for value in (item.get("report_advice") or [])],
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
