from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .config import Settings, get_settings
from .content import (
    load_current_research_question_bank,
    load_hr_question_bank,
    load_question_bank,
    load_style_card,
)
from .db import Database
from .errors import AppError, LLMError
from .llm import BailianChatClient
from .prompt_engine import (
    SEVEN_DRILL_DIMENSIONS,
    build_system_prompt,
    enforce_project_drill,
    extract_anchor_keyword,
    initial_question,
    interview_drill_target,
    is_vague_answer,
    select_questions,
)
from .schemas import (
    InterviewCreate,
    InterviewTurn,
    ResumeData,
    TurnAssessment,
    TurnDecision,
)
from .topics import canonical_topic


@dataclass(slots=True)
class EngineResult:
    question: str
    ended: bool
    end_reason: str | None
    pressure_action: str
    silence_seconds: int
    breakdown_streak: int
    recommended_answer_seconds: int
    turn: InterviewTurn


class InterviewEngine:
    def __init__(
        self,
        db: Database,
        settings: Settings | None = None,
        client: BailianChatClient | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.client = client or BailianChatClient(self.settings)

    async def create(
        self,
        request: InterviewCreate,
        *,
        weak_topics_override: list[str] | None = None,
    ) -> dict[str, Any]:
        global_count = await self.db.interview_count_today()
        if global_count >= self.settings.daily_interview_limit:
            raise AppError(
                "DAILY_BUDGET_LIMIT",
                "今日公开体验场次已用完，请明天再来。",
                status_code=429,
            )
        client_count = await self.db.interview_count_today(request.client_id)
        if client_count >= self.settings.client_daily_interview_limit:
            raise AppError(
                "CLIENT_DAILY_LIMIT",
                "本设备今日练习场次已用完，请先复盘已有报告。",
                status_code=429,
            )
        if weak_topics_override is not None:
            weak_topics = self._normalize_weak_topics(weak_topics_override)
        elif request.memory_enabled:
            weak_topics = await self.db.weak_topics(request.client_id)
        else:
            weak_topics = []
        interview_id = uuid.uuid4().hex
        style = load_style_card(request.company)
        prompt = build_system_prompt(
            company=request.company,
            resume=request.resume,
            interview_type=request.interview_type,
            stress=request.stress,
            stress_level=request.stress_level,
            specialization=request.specialization,
            language_mode=request.language_mode,
            duration_minutes=request.duration_minutes,
            weak_topics=weak_topics,
            selection_seed=interview_id,
        )
        first_question = initial_question(request.company, request.language_mode)
        await self.db.create_interview(
            interview_id=interview_id,
            client_id=request.client_id,
            company=request.company,
            role=request.role,
            interview_type=request.interview_type,
            specialization=request.specialization,
            language_mode=request.language_mode,
            stress=request.stress,
            stress_level=request.stress_level,
            duration_minutes=request.duration_minutes,
            memory_enabled=request.memory_enabled,
            voice_mode=self.settings.voice_mode,
            resume=request.resume,
            style=style,
            weak_topics=weak_topics,
            system_prompt=prompt,
            initial_question=first_question,
        )
        return {
            "id": interview_id,
            "status": "created",
            "voice_mode": self.settings.voice_mode,
            "weak_topics": weak_topics,
            "initial_question": first_question,
            "recommended_answer_seconds": self.recommended_answer_seconds(first_question),
            "company": request.company,
            "role": request.role,
            "interview_type": request.interview_type,
            "specialization": request.specialization,
            "language_mode": request.language_mode,
            "stress": request.stress,
            "stress_level": request.stress_level,
            "duration_minutes": request.duration_minutes,
            "memory_enabled": request.memory_enabled,
        }

    async def hint(self, interview_id: str) -> dict[str, Any]:
        interview = await self.db.get_interview(interview_id)
        if not interview:
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
        if interview["status"] != "active":
            if interview["status"] in {"ended", "reporting", "reported"}:
                raise AppError("INTERVIEW_ENDED", "本场面试已经结束", status_code=409)
            raise AppError("INTERVIEW_NOT_STARTED", "请先进入面试再获取提示", status_code=409)
        question = str(interview.get("last_question") or "").strip()
        if not question:
            raise AppError("QUESTION_NOT_READY", "当前问题还未准备好", status_code=409)
        turns = await self.db.list_turns(interview_id)
        ordinal = len(turns) + 1
        event = await self.db.record_hint(
            interview_id,
            ordinal=ordinal,
            question=question,
            hint=self._build_hint(
                question, str(interview.get("language_mode") or "bilingual")
            ),
        )
        if not event:
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
        return event

    async def retry(self, interview_id: str, client_id: str) -> dict[str, Any]:
        source = await self.db.get_interview(interview_id)
        if not source or source.get("client_id") != client_id:
            # Do not reveal whether an interview owned by another device exists.
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
        report = await self.db.get_report(interview_id)
        if not report:
            raise AppError(
                "REPORT_NOT_READY",
                "报告生成后才能创建针对性复练",
                status_code=409,
            )
        request = InterviewCreate(
            client_id=client_id,
            resume=ResumeData.model_validate(source["resume"]),
            company=source["company"],
            role="backend",
            interview_type=source.get("interview_type") or "technical",
            specialization=source.get("specialization") or "通用后端",
            language_mode=source.get("language_mode") or "bilingual",
            stress_level=int(source.get("stress_level") or 0),
            duration_minutes=source.get("duration_minutes"),
            memory_enabled=True,
        )
        created = await self.create(
            request,
            weak_topics_override=self._weak_topics_from_report(report),
        )
        created["retry_of"] = interview_id
        return created

    @staticmethod
    def _build_hint(question: str, language_mode: str = "bilingual") -> str:
        """Return an answer scaffold, never a factual or complete answer."""

        lowered = question.lower()
        if language_mode == "en":
            if any(marker in lowered for marker in ("compensation", "salary", "pay")):
                return (
                    "State a realistic expectation or range, explain the information behind it, "
                    "then rank compensation, mentorship, and role fit. Finish with what is negotiable."
                )
            if any(marker in lowered for marker in ("career", "two to three years", "internship now")):
                return (
                    "Use this structure: current choice, evidence behind the choice, a measurable "
                    "two-to-three-year goal, and the action you are taking this semester."
                )
            if any(marker in lowered for marker in ("algorithm", "complexity", "lru", "implement", "coding")):
                return (
                    "Clarify inputs, outputs, and constraints; state a simple baseline; improve it "
                    "with the key data structure; then cover complexity, edge cases, and tests."
                )
            if any(marker in lowered for marker in ("project", "request", "traffic", "failure", "metric")):
                return (
                    "Lead with the conclusion, then cover the request path, your own contribution, "
                    "one measurable result, and the main boundary or trade-off."
                )
            return (
                "Answer in three parts: your conclusion, the mechanism or evidence, and one boundary "
                "or counterexample. Keep each part tied to the question."
            )
        if "薪酬" in question:
            return (
                "先坦诚给出你的预期或可接受范围，再说明参考信息；"
                "随后把薪酬、成长、团队/导师和方向匹配排个顺序，解释排序依据，"
                "最后说明哪些条件可以沟通。不要猜公司标准答案。"
            )
        if any(
            marker in question
            for marker in (
                "接下来两三年",
                "未来两三年",
                "毕业前",
                "毕业前后",
                "为什么在这个阶段选择后端",
                "为什么选择后端方向",
            )
        ):
            return (
                "按“当前选择 → 选择依据 → 两三年目标 → 最近行动”组织；"
                "目标不要只写职位名称，要落到一两项能力和可验证的学习/实践计划，"
                "再补如果现实与预期不同你会怎样调整。"
            )
        if any(
            marker in question
            for marker in (
                "需求变化很快",
                "技术方案看起来很漂亮",
                "方案没有被采用",
                "意见不一致",
                "怎么判断先做什么",
            )
        ):
            return (
                "选一个真实的小场景，按“当时约束 → 你的判断 → 具体沟通/行动 → 结果与复盘”回答；"
                "明确你如何兼顾用户/业务结果、事实依据和团队协作，不要只说抽象价值观。"
            )
        if any(marker in lowered for marker in ("手撕", "算法", "复杂度", "lru", "代码", "实现")):
            return (
                "按四步拆解：先复述输入、输出和边界；再说朴素方案的瓶颈；"
                "然后说明准备维护的数据结构或不变量；最后补时间/空间复杂度与极端用例。"
                "这里只给路线，不需要直接写出完整答案。"
            )
        if "mysql" in lowered or any(marker in question for marker in ("数据库", "事务", "索引", "慢查询")):
            return (
                "先界定问题发生在哪个读写阶段，再从数据量、访问模式和并发条件拆约束；"
                "随后说你会观察哪些证据，最后比较两个候选方案的收益、代价与失效边界。"
            )
        if "redis" in lowered or any(marker in question for marker in ("缓存", "过期", "热 key", "库存")):
            return (
                "先画出请求从入口、缓存到持久层的顺序，再分别说明命中、未命中和异常时的行为；"
                "补上并发一致性、容量/过期策略，以及你会用什么指标验证。"
            )
        if any(marker in lowered for marker in ("thread", "concurrent")) or any(
            marker in question for marker in ("线程", "并发", "锁", "原子", "可见性")
        ):
            return (
                "先明确共享状态和竞争窗口，再描述正确性目标；"
                "按同步边界、失败/超时路径、性能代价三层展开，并补一个能暴露竞态的测试场景。"
            )
        if any(marker in lowered for marker in ("tcp", "udp", "http", "https", "dns")) or any(
            marker in question for marker in ("网络", "连接", "协议", "超时")
        ):
            return (
                "按客户端发起、连接建立、数据传输、服务端返回的时序回答；"
                "每一步说明关键状态与失败点，再补超时/重试的边界和可观测指标。"
            )
        return (
            "用“我的职责 → 请求链路 → 关键取舍 → 指标口径/异常边界”组织；"
            "只说自己亲手做的部分，并补一个可验证的数据点。先给结论，再逐层展开。"
        )

    @staticmethod
    def _normalize_weak_topics(topics: list[str], limit: int = 3) -> list[str]:
        normalized: list[str] = []
        for topic in topics:
            value = canonical_topic(str(topic))
            if value and value not in normalized:
                normalized.append(value)
            if len(normalized) >= limit:
                break
        return normalized

    @classmethod
    def _weak_topics_from_report(cls, report: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        for item in report.get("next_focus") or []:
            candidates.append(str(item))
        for item in report.get("must_practice") or []:
            if isinstance(item, dict):
                candidates.append(str(item.get("topic") or item.get("name") or ""))
            else:
                candidates.append(str(item))
        topic_scores = report.get("topic_scores") or {}
        if isinstance(topic_scores, dict):
            scored: list[tuple[str, float]] = []
            for topic, score in topic_scores.items():
                try:
                    scored.append((str(topic), float(score)))
                except (TypeError, ValueError):
                    continue
            candidates.extend(topic for topic, _ in sorted(scored, key=lambda pair: pair[1]))
        return cls._normalize_weak_topics(candidates)

    async def answer(
        self,
        interview_id: str,
        answer: str,
        *,
        input_mode: str = "text",
        answer_duration_seconds: float | None = None,
    ) -> EngineResult:
        interview = await self.db.get_interview(interview_id)
        if not interview:
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
        if interview["status"] in {"ended", "reporting", "reported"}:
            raise AppError("INTERVIEW_ENDED", "本场面试已经结束", status_code=409)
        if interview.get("deadline_at") and float(interview["deadline_at"]) <= time.time():
            await self.db.finish_interview(interview_id, "time")
            raise AppError("INTERVIEW_TIMEOUT", "面试时间已到", status_code=409)

        answer = re.sub(r"\x00", "", answer).strip()
        if not answer:
            raise AppError("EMPTY_ANSWER", "回答不能为空", status_code=422)
        if len(answer) > 10000:
            raise AppError("ANSWER_TOO_LONG", "单次回答不能超过 10000 字", status_code=413)

        turns = await self.db.list_turns(interview_id)
        resume = ResumeData.model_validate(interview["resume"])
        decision = await self._decide(interview, resume, turns, answer)
        vague_answer = is_vague_answer(answer)
        anchor = decision.anchor_keyword.strip()
        if not anchor or anchor.lower() not in answer.lower():
            anchor = extract_anchor_keyword(answer, resume)
        if vague_answer:
            previous_anchor = next(
                (turn.anchor_keyword for turn in reversed(turns) if turn.anchor_keyword),
                "",
            )
            if previous_anchor:
                anchor = previous_anchor
            elif resume.projects and resume.projects[0].name:
                anchor = resume.projects[0].name

        completed_turns = len(turns) + 1
        interview_type = str(interview.get("interview_type") or "technical")
        drill_target = interview_drill_target(
            interview["weak_topics"], interview_type
        )
        question, forced_dimension, forced_depth = enforce_project_drill(
            decision.next_question,
            completed_turns=completed_turns,
            anchor=anchor,
            resume=resume,
            vague=vague_answer,
            max_depth=drill_target,
            language_mode=str(interview.get("language_mode") or "bilingual"),
        )
        if not forced_depth and (
            interview["company"] == "bytedance"
            and completed_turns == drill_target + 1
        ):
            # 字节风格卡要求每场都有手撕思路。把它放在项目深挖之后
            # 的第一个切换点，避免短场面被普通八股挤掉。
            if interview.get("language_mode") == "en":
                coding_question = (
                    "Design an O(1) LRU cache verbally. Explain the data structures, "
                    "the get and put operations, complexity, and edge cases."
                )
            else:
                coding_question = next(
                    (
                        item["question"]
                        for item in load_question_bank("bytedance")
                        if item.get("category") == "手撕思路"
                    ),
                    "请口述一个 O(1) LRU Cache 的数据结构、操作和边界。",
                )
            question = str(coding_question)
        hr_questions = (
            load_hr_question_bank(
                interview["company"],
                str(interview.get("language_mode") or "bilingual"),
            )
            if interview_type == "technical_hr"
            else []
        )
        # After the self introduction, >=3 project layers and one additional
        # technical question, a combined interview deterministically covers
        # all three behavioral areas instead of hoping the model happens to
        # select them before a short interview times out.
        next_hr_index = completed_turns - (drill_target + 2)
        if 0 <= next_hr_index < len(hr_questions):
            question = str(hr_questions[next_hr_index]["question"])
        question = self._sanitize_question(
            question,
            interview["company"],
            language_mode=str(interview.get("language_mode") or "bilingual"),
        )

        # A row represents the question that was just answered, not the next
        # question generated above. Normalize server-forced phases accordingly
        # so reports and next-session memory are not shifted by one turn.
        current_dimension = decision.assessment.dimension
        current_topic = decision.assessment.topic
        current_drill_dimension = decision.drill_dimension
        current_drill_depth = decision.drill_depth
        if completed_turns == 1:
            current_dimension = "communication"
            current_topic = "自我介绍·整体与学习情况"
            current_drill_dimension = ""
            current_drill_depth = 0
        elif 2 <= completed_turns <= drill_target + 1:
            current_drill_depth = completed_turns - 1
            current_drill_dimension = SEVEN_DRILL_DIMENSIONS[
                min(current_drill_depth - 1, len(SEVEN_DRILL_DIMENSIONS) - 1)
            ]
            current_dimension = "project_depth"
            current_topic = f"项目深挖·{current_drill_dimension}"
        elif (
            interview["company"] == "bytedance"
            and completed_turns == drill_target + 2
        ):
            current_dimension = "coding_thought"
            current_topic = "手撕思路·LRU"
            current_drill_dimension = "手撕思路"
            current_drill_depth = 0
        answered_hr_index = completed_turns - (drill_target + 3)
        if 0 <= answered_hr_index < len(hr_questions):
            current_dimension = "communication"
            current_topic = f"综合面·{hr_questions[answered_hr_index]['topic']}"
            current_drill_dimension = ""
            current_drill_depth = 0

        if completed_turns == 1 or 0 <= next_hr_index < len(hr_questions):
            # The introduction-to-experience handoff and the three required HR
            # openers should sound like coherent phase transitions. Pressure
            # resumes inside technical follow-ups instead of adding a generic
            # confrontational prefix at these boundaries.
            pressure_action = "none"
        else:
            pressure_action = self._pressure_action(
                stress_level=interview["stress_level"],
                ordinal=completed_turns,
                proposed=decision.pressure_action,
                expression_problem=self._has_expression_problem(
                    answer, decision.assessment.deductions
                ),
            )
        question = self._apply_pressure_copy(
            question,
            pressure_action,
            ordinal=completed_turns,
            language_mode=str(interview.get("language_mode") or "bilingual"),
        )
        recommended_seconds = self.recommended_answer_seconds(question)
        current_recommended_seconds = self.recommended_answer_seconds(
            str(interview["last_question"])
        )
        normalized_duration = (
            round(max(0.0, min(float(answer_duration_seconds), 3600.0)), 2)
            if answer_duration_seconds is not None
            else None
        )
        speech_rate_cpm = (
            round(len(re.sub(r"\s+", "", answer)) * 60 / normalized_duration, 1)
            if input_mode == "voice" and normalized_duration
            else None
        )
        turn = InterviewTurn(
            ordinal=completed_turns,
            question=interview["last_question"],
            answer=answer,
            category=current_dimension,
            topic=current_topic or "综合基础",
            score=decision.assessment.score,
            scorable=decision.assessment.scorable and decision.assessment.score is not None,
            score_source=decision.assessment.score_source,
            deductions=decision.assessment.deductions
            or (["回答缺少可验证的关键细节"] if decision.assessment.failed else []),
            failed=decision.assessment.failed,
            drill_dimension=current_drill_dimension,
            drill_depth=current_drill_depth,
            anchor_keyword=anchor,
            input_mode="voice" if input_mode == "voice" else "text",
            answer_duration_seconds=normalized_duration,
            speech_rate_cpm=speech_rate_cpm,
            recommended_answer_seconds=current_recommended_seconds,
        )
        streak = await self.db.append_turn(interview_id, turn, question)
        threshold = self._breakdown_threshold(interview["stress_level"])
        ended = streak >= threshold
        end_reason: str | None = None
        if ended:
            end_reason = "poor_performance"
            question = (
                "That concludes today's interview. Thank you for your time."
                if interview.get("language_mode") == "en"
                else "今天的面试就到这里，感谢你的时间。"
            )
            recommended_seconds = 0
            await self.db.finish_interview(interview_id, end_reason)

        return EngineResult(
            question=question,
            ended=ended,
            end_reason=end_reason,
            pressure_action=pressure_action,
            silence_seconds=10 if pressure_action == "silence" and not ended else 0,
            breakdown_streak=streak,
            recommended_answer_seconds=0 if ended else recommended_seconds,
            turn=turn,
        )

    async def correct_answer(
        self, interview_id: str, *, ordinal: int, text: str
    ) -> dict[str, Any]:
        """Re-score and persist an edited ASR transcript before reporting."""

        interview = await self.db.get_interview(interview_id)
        if not interview:
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
        if interview["status"] in {"reporting", "reported"}:
            raise AppError(
                "REPORT_ALREADY_GENERATED",
                "报告已开始生成，不能再修改转写；请在面试过程中完成修正。",
                status_code=409,
            )
        text = re.sub(r"\x00", "", text).strip()
        if not text:
            raise AppError("EMPTY_TRANSCRIPT", "修正后的转写不能为空", status_code=422)
        if len(text) > 10000:
            raise AppError("ANSWER_TOO_LONG", "单次回答不能超过 10000 字", status_code=413)
        turns = await self.db.list_turns(interview_id)
        target = next((turn for turn in turns if turn.ordinal == ordinal), None)
        if target is None:
            raise AppError("TURN_NOT_FOUND", "待修正的回答不存在", status_code=404)
        prior_turns = [turn for turn in turns if turn.ordinal < ordinal]
        resume = ResumeData.model_validate(interview["resume"])
        scoring_interview = dict(interview)
        scoring_interview["last_question"] = target.question
        decision = await self._decide(scoring_interview, resume, prior_turns, text)
        deductions = decision.assessment.deductions or (
            ["回答仍缺少可验证的关键细节"]
            if decision.assessment.failed
            else []
        )
        try:
            return await self.db.correct_turn_answer(
                interview_id,
                ordinal=ordinal,
                text=text,
                score=decision.assessment.score,
                scorable=decision.assessment.scorable,
                score_source=decision.assessment.score_source,
                deductions=deductions,
                failed=decision.assessment.failed,
            )
        except RuntimeError as exc:
            if str(exc) == "report_already_generated":
                raise AppError(
                    "REPORT_ALREADY_GENERATED",
                    "报告已开始生成，不能再修改转写；请在面试过程中完成修正。",
                    status_code=409,
                ) from exc
            raise
        except KeyError as exc:
            raise AppError("TURN_NOT_FOUND", "待修正的回答不存在", status_code=404) from exc
        except LookupError as exc:
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404) from exc

    async def preserve_unscored_answer(
        self,
        interview_id: str,
        text: str,
        *,
        input_mode: str = "text",
        answer_duration_seconds: float | None = None,
        ordinal: int | None = None,
    ) -> InterviewTurn:
        """Persist an accepted transcript when its model assessment is cancelled.

        Ending a session must not erase text already shown as recorded. The
        fallback deliberately carries no numeric score and therefore cannot
        pollute the report or next-session memory.
        """

        interview = await self.db.get_interview(interview_id)
        if not interview:
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
        normalized = re.sub(r"\x00", "", text).strip()
        if not normalized:
            raise AppError("EMPTY_ANSWER", "回答不能为空", status_code=422)
        turns = await self.db.list_turns(interview_id)
        expected_ordinal = ordinal or (len(turns) + 1)
        # The normal path may have committed just before cancellation reached
        # its caller. Reuse that row instead of creating a duplicate turn.
        existing = next(
            (turn for turn in turns if turn.ordinal == expected_ordinal), None
        )
        if existing is not None:
            return existing
        duration = (
            round(max(0.0, min(float(answer_duration_seconds), 3600.0)), 2)
            if answer_duration_seconds is not None
            else None
        )
        speech_rate = (
            round(len(re.sub(r"\s+", "", normalized)) * 60 / duration, 1)
            if input_mode == "voice" and duration
            else None
        )
        question = str(interview.get("last_question") or "本轮回答")
        turn = InterviewTurn(
            ordinal=expected_ordinal,
            question=question,
            answer=normalized,
            category="communication",
            topic="结束时保留的未评分回答",
            score=None,
            scorable=False,
            score_source="unavailable",
            deductions=["本轮回答已被保留，但结束面试时评分尚未完成，因此不生成数值分。"],
            failed=False,
            input_mode="voice" if input_mode == "voice" else "text",
            answer_duration_seconds=duration,
            speech_rate_cpm=speech_rate,
            recommended_answer_seconds=self.recommended_answer_seconds(question),
        )
        await self.db.append_turn(interview_id, turn, question)
        return turn

    async def _decide(
        self,
        interview: dict[str, Any],
        resume: ResumeData,
        turns: list[InterviewTurn],
        answer: str,
    ) -> TurnDecision:
        if self.settings.mock_llm:
            return self._mock_decision(interview, resume, turns, answer)

        system_prompt = build_system_prompt(
            company=interview["company"],
            resume=resume,
            interview_type=interview.get("interview_type") or "technical",
            stress=interview["stress"],
            stress_level=interview["stress_level"],
            specialization=interview["specialization"],
            language_mode=interview.get("language_mode") or "bilingual",
            duration_minutes=interview["duration_minutes"],
            weak_topics=interview["weak_topics"],
            selection_seed=interview["id"],
            turns=turns,
        )
        recent = [
            {"question": turn.question, "answer": turn.answer}
            for turn in turns[-8:]
        ]
        user_payload = {
            "recent_transcript": recent,
            "current_question": interview["last_question"],
            "candidate_answer": answer,
            "instruction": "完成私有评分并生成下一问。只输出 JSON。",
        }
        try:
            raw = await self.client.chat_json(
                [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(user_payload, ensure_ascii=False),
                    },
                ],
                response_schema=TurnDecision.model_json_schema(),
                schema_name="interview_turn_decision",
                model=self.settings.qwen_text_model,
                temperature=0.35,
                max_tokens=1200,
            )
            return TurnDecision.model_validate(raw)
        except (ValidationError, LLMError):
            # Keep the interview demo alive on a single malformed generation.
            # The conservative fallback does not mark a candidate as failed.
            return self._fallback_decision(interview, resume, turns, answer)

    def _mock_decision(
        self,
        interview: dict[str, Any],
        resume: ResumeData,
        turns: list[InterviewTurn],
        answer: str,
    ) -> TurnDecision:
        vague = is_vague_answer(answer)
        lowered = answer.lower()
        obvious_error = any(
            phrase in lowered
            for phrase in (
                "完全不会",
                "不知道",
                "不清楚",
                "没学过",
                "没读过",
                "没看过",
                "未读过",
                "跳过",
                "i don't know",
                "i dont know",
                "not sure",
                "no idea",
                "skip",
            )
        )
        research_gap = (vague or obvious_error) and self._is_current_research_question(
            str(interview.get("last_question") or ""),
            str(interview.get("specialization") or ""),
        )
        failed = (vague or obvious_error) and not research_gap
        score = 3.5 if research_gap else (2.5 if failed else (6.5 if len(answer) < 80 else 7.5))
        fallback = self._fallback_decision(interview, resume, turns, answer)
        english = interview.get("language_mode") == "en"
        fallback.assessment = TurnAssessment(
            score=score,
            scorable=True,
            score_source="mock",
            failed=failed,
            dimension=fallback.assessment.dimension,
            topic=fallback.assessment.topic,
            deductions=(
                (["No clear view was formed; connect relevant engineering experience, validation metrics, and trade-offs."] if english else ["未形成对该前沿方向的判断；可从相关工程经验、验证指标与取舍展开"])
                if research_gap
                else ((["The answer did not explain the mechanism, evidence, or boundary conditions."] if english else ["未说明原理、证据或边界条件"]) if failed else [])
            ),
        )
        return fallback

    def _fallback_decision(
        self,
        interview: dict[str, Any],
        resume: ResumeData,
        turns: list[InterviewTurn],
        answer: str,
    ) -> TurnDecision:
        ordinal = len(turns) + 1
        anchor = extract_anchor_keyword(answer, resume)
        questions = select_questions(
            interview["company"],
            interview["weak_topics"],
            interview["duration_minutes"],
            interview["specialization"],
            selection_seed=interview["id"],
            language_mode=str(interview.get("language_mode") or "bilingual"),
            interview_type=str(interview.get("interview_type") or "technical"),
        )
        if ordinal <= 4:
            question = ""
            dimension = "project_depth"
            topic = anchor
        else:
            item = questions[(ordinal - 5) % max(1, len(questions))] if questions else {}
            question = str(item.get("question") or "请说说你会如何排查一次接口超时。")
            if interview.get("language_mode") == "en" and re.search(
                r"[\u4e00-\u9fff]", question
            ):
                question = (
                    "An API's tail latency suddenly increased in production. "
                    "How would you investigate it and narrow down the root cause?"
                )
            category = str(item.get("category", "基础知识"))
            dimension = (
                "coding_thought"
                if category == "手撕思路" or "coding" in category.lower()
                else "fundamentals"
            )
            topic = str(item.get("topic") or category)
        # A malformed/failed scoring call is operational missing data, not
        # evidence of average performance.  Keep the interview moving while
        # explicitly marking this turn unavailable for numeric aggregation.
        fallback_score = None
        fallback_failed = False
        deductions = (
            ["Scoring was unavailable for this turn; the transcript is retained without a numeric score."]
            if interview.get("language_mode") == "en"
            else ["本轮评分服务不可用；保留问答原文，但不计入数值评分"]
        )
        return TurnDecision(
            next_question=question,
            assessment=TurnAssessment(
                score=fallback_score,
                scorable=False,
                score_source="unavailable",
                failed=fallback_failed,
                dimension=dimension,  # type: ignore[arg-type]
                topic=topic,
                deductions=deductions,
            ),
            drill_dimension="基础知识",
            drill_depth=0,
            anchor_keyword=anchor,
        )

    @staticmethod
    def _is_current_research_question(question: str, specialization: str) -> bool:
        normalized = " ".join(question.strip().split())
        if not normalized:
            return False
        for item in load_current_research_question_bank(specialization):
            candidate = " ".join(str(item.get("question") or "").strip().split())
            if candidate and (normalized == candidate or normalized.endswith(candidate)):
                return True
        return False

    @staticmethod
    def _pressure_action(
        stress_level: int,
        ordinal: int,
        proposed: str,
        expression_problem: bool = False,
    ) -> str:
        if stress_level <= 0:
            return "none"
        # Interruption is evidence-triggered, never a round-robin pressure
        # tactic.  Live voice has a separate partial-transcript guard; this
        # branch keeps text/L3 behavior aligned.
        if proposed == "interrupt" and expression_problem:
            return "interrupt"
        if stress_level == 1:
            if ordinal % 2:
                return "none"
            return "challenge" if proposed == "challenge" else "chain"
        # Standard/high pressure stays difficult on every round. Evidence is
        # challenged only when the model found an actual gap; otherwise the
        # pressure remains a deeper scenario follow-up.
        if proposed == "challenge":
            return "challenge"
        return "chain"

    @staticmethod
    def _has_expression_problem(answer: str, deductions: list[str]) -> bool:
        compact = re.sub(r"\s+", "", answer)
        evidence = " ".join(str(item) for item in deductions)
        if any(
            marker in evidence
            for marker in ("跑题", "冗长", "重复", "表述混乱", "逻辑混乱", "前后矛盾")
        ):
            return True
        if any(
            marker in answer
            for marker in ("我说乱了", "有点乱", "不对不对", "我重新说", "不知道怎么表达")
        ):
            return True
        filler_count = sum(
            answer.count(marker)
            for marker in ("嗯", "呃", "那个", "就是说", "怎么说呢")
        )
        return len(compact) >= 220 and filler_count >= 4

    @staticmethod
    def _breakdown_threshold(stress_level: int) -> int:
        return 2 if stress_level >= 2 else 3

    @staticmethod
    def _sanitize_question(
        question: str, company: str, *, language_mode: str = "bilingual"
    ) -> str:
        question = re.sub(r"```.*?```", "", question, flags=re.S)
        if language_mode == "en":
            question = re.sub(
                r"^(?:(?:okay|great|very good|thank you(?: for sharing)?|"
                r"let(?:'s| us) (?:continue|dive deeper))[,.!\s]*)+",
                "",
                question.strip(),
                flags=re.I,
            )
            question = re.sub(
                r"(?:your score|score|deductions?|correct answer|reference answer)\s*[:：]?.*",
                "",
                question,
                flags=re.I,
            ).strip()
            if not question or re.search(r"[\u4e00-\u9fff]", question):
                question = (
                    "Explain the underlying mechanism of this design and the boundary "
                    "conditions under which it would stop working."
                )
            if len(question) > 360:
                question = question[:357].rstrip() + "?"
            if not question.endswith(("?", ".")):
                question += "?"
            return question
        question = re.sub(
            r"^(?:(?:好的|很好|非常好|明白了|感谢(?:你的)?分享|"
            r"让我们(?:继续|深入)(?:聊聊|探讨)?)[，,。.!！\s]*)+",
            "",
            question.strip(),
        )
        question = re.sub(
            r"(?:你的得分|评分|扣分点|正确答案|参考答案)[:：]?.*", "", question
        ).strip()
        if not question:
            question = "请具体说说这个方案的底层原理和边界条件。"
        if question == "今天的面试就到这里":
            question = "请继续说明这个方案在高并发下的边界。"
        if len(question) > 260:
            question = question[:257] + "？"
        if not question.endswith(("？", "?", "。")):
            question += "？"
        return question

    @staticmethod
    def _apply_pressure_copy(
        question: str,
        action: str,
        *,
        ordinal: int = 0,
        language_mode: str = "bilingual",
    ) -> str:
        if language_mode == "en":
            if action == "chain":
                transitions = (
                    "Let's take that one level deeper. ",
                    "Following your implementation, I want to tighten the constraint. ",
                    "Let's examine the boundary of that approach. ",
                )
                return f"{transitions[ordinal % len(transitions)]}{question}"
            if action == "challenge":
                return f"That conclusion still needs evidence. {question}"
            if action == "interrupt":
                return (
                    "I am going to pause you because the explanation is becoming circular. "
                    f"Give me the conclusion in one sentence, then answer this: {question}"
                )
            return question
        if action == "chain":
            transitions = (
                "这个点我再往下追一步。",
                "沿着刚才的实现，我再问深一点。",
                "我们把条件再收紧一点。",
            )
            return f"{transitions[ordinal % len(transitions)]}{question}"
        if action == "challenge":
            return f"这个结论目前还缺少依据。{question}"
        if action == "interrupt":
            return f"我先打断一下，你刚才这段有些绕。请先用一句话给结论，再回答：{question}"
        return question

    @staticmethod
    def recommended_answer_seconds(question: str) -> int:
        lowered = question.lower()
        if "自我介绍" in question or any(
            marker in lowered for marker in ("brief introduction", "academic background")
        ):
            return 60
        if any(marker in lowered for marker in ("手撕", "算法", "lru", "复杂度", "实现", "algorithm", "complexity", "design an")):
            return 180
        if any(marker in lowered for marker in ("项目", "链路", "故障", "取舍", "指标口径", "project", "request", "failure", "trade-off", "metric")):
            return 90
        if any(marker in lowered for marker in ("前沿", "怎么看", "研究", "论文", "趋势", "research", "paper", "trend")):
            return 120
        return 60
