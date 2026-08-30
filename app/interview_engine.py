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
from .topics import canonical_topic, project_depth_target


@dataclass(slots=True)
class EngineResult:
    question: str
    ended: bool
    end_reason: str | None
    pressure_action: str
    silence_seconds: int
    breakdown_streak: int
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
            stress=request.stress,
            stress_level=request.stress_level,
            specialization=request.specialization,
            language_mode=request.language_mode,
            duration_minutes=request.duration_minutes,
            weak_topics=weak_topics,
            selection_seed=interview_id,
        )
        first_question = initial_question(request.company)
        await self.db.create_interview(
            interview_id=interview_id,
            client_id=request.client_id,
            company=request.company,
            role=request.role,
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
            "company": request.company,
            "role": request.role,
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
            hint=self._build_hint(question),
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
    def _build_hint(question: str) -> str:
        """Return an answer scaffold, never a factual or complete answer."""

        lowered = question.lower()
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

    async def answer(self, interview_id: str, answer: str) -> EngineResult:
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
        drill_target = project_depth_target(interview["weak_topics"])
        question, forced_dimension, forced_depth = enforce_project_drill(
            decision.next_question,
            completed_turns=completed_turns,
            anchor=anchor,
            resume=resume,
            vague=vague_answer,
            max_depth=drill_target,
        )
        if not forced_depth and (
            interview["company"] == "bytedance"
            and completed_turns == drill_target + 1
        ):
            # 字节风格卡要求每场都有手撕思路。把它放在项目深挖之后
            # 的第一个切换点，避免短场面被普通八股挤掉。
            coding_question = next(
                (
                    item["question"]
                    for item in load_question_bank("bytedance")
                    if item.get("category") == "手撕思路"
                ),
                "请口述一个 O(1) LRU Cache 的数据结构、操作和边界。",
            )
            question = str(coding_question)
        question = self._sanitize_question(question, interview["company"])

        # A row represents the question that was just answered, not the next
        # question generated above. Normalize server-forced phases accordingly
        # so reports and next-session memory are not shifted by one turn.
        current_dimension = decision.assessment.dimension
        current_topic = decision.assessment.topic
        current_drill_dimension = decision.drill_dimension
        current_drill_depth = decision.drill_depth
        if 2 <= completed_turns <= drill_target + 1:
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

        pressure_action = self._pressure_action(
            stress_level=interview["stress_level"],
            ordinal=completed_turns,
            proposed=decision.pressure_action,
        )
        question = self._apply_pressure_copy(question, pressure_action)
        turn = InterviewTurn(
            ordinal=completed_turns,
            question=interview["last_question"],
            answer=answer,
            category=current_dimension,
            topic=current_topic or "综合基础",
            score=decision.assessment.score,
            deductions=decision.assessment.deductions
            or (["回答缺少可验证的关键细节"] if decision.assessment.failed else []),
            failed=decision.assessment.failed,
            drill_dimension=current_drill_dimension,
            drill_depth=current_drill_depth,
            anchor_keyword=anchor,
        )
        streak = await self.db.append_turn(interview_id, turn, question)
        threshold = self._breakdown_threshold(interview["stress_level"])
        ended = streak >= threshold
        end_reason: str | None = None
        if ended:
            end_reason = "poor_performance"
            question = "今天的面试就到这里，感谢你的时间。"
            await self.db.finish_interview(interview_id, end_reason)

        return EngineResult(
            question=question,
            ended=ended,
            end_reason=end_reason,
            pressure_action=pressure_action,
            silence_seconds=10 if pressure_action == "silence" and not ended else 0,
            breakdown_streak=streak,
            turn=turn,
        )

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
            )
        )
        research_gap = (vague or obvious_error) and self._is_current_research_question(
            str(interview.get("last_question") or ""),
            str(interview.get("specialization") or ""),
        )
        failed = (vague or obvious_error) and not research_gap
        score = 3.5 if research_gap else (2.5 if failed else (6.5 if len(answer) < 80 else 7.5))
        fallback = self._fallback_decision(interview, resume, turns, answer)
        fallback.assessment = TurnAssessment(
            score=score,
            failed=failed,
            dimension=fallback.assessment.dimension,
            topic=fallback.assessment.topic,
            deductions=(
                ["未形成对该前沿方向的判断；可从相关工程经验、验证指标与取舍展开"]
                if research_gap
                else (["未说明原理、证据或边界条件"] if failed else [])
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
        )
        if ordinal <= 4:
            question = ""
            dimension = "project_depth"
            topic = anchor
        else:
            item = questions[(ordinal - 5) % max(1, len(questions))] if questions else {}
            question = str(item.get("question") or "请说说你会如何排查一次接口超时。")
            category = str(item.get("category", "基础知识"))
            dimension = "coding_thought" if category == "手撕思路" else "fundamentals"
            topic = str(item.get("topic") or category)
        return TurnDecision(
            next_question=question,
            assessment=TurnAssessment(
                score=5.0,
                failed=False,
                dimension=dimension,  # type: ignore[arg-type]
                topic=topic,
                deductions=[],
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
    def _pressure_action(stress_level: int, ordinal: int, proposed: str) -> str:
        del proposed
        if stress_level <= 0:
            return "none"
        if stress_level == 1:
            if ordinal % 3:
                return "none"
            return ("chain", "challenge")[((ordinal // 3) - 1) % 2]
        if stress_level == 2:
            cycle = {1: "chain", 2: "challenge", 3: "interrupt", 0: "silence"}
            return cycle[ordinal % 4]
        # High pressure retains every technique but deliberately interrupts on
        # every other round. The live voice path mirrors the same cadence.
        cycle = (
            "chain",
            "interrupt",
            "challenge",
            "interrupt",
            "silence",
            "interrupt",
        )
        return cycle[(ordinal - 1) % len(cycle)]

    @staticmethod
    def _breakdown_threshold(stress_level: int) -> int:
        return 2 if stress_level >= 2 else 3

    @staticmethod
    def _sanitize_question(question: str, company: str) -> str:
        question = re.sub(r"```.*?```", "", question, flags=re.S)
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
    def _apply_pressure_copy(question: str, action: str) -> str:
        if action == "challenge":
            return f"我对这个前提存疑，{question}"
        if action == "interrupt":
            return f"先停一下，{question}"
        return question
