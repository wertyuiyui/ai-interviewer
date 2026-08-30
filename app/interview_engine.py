from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .config import Settings, get_settings
from .content import load_question_bank, load_style_card
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
from .topics import project_depth_target


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

    async def create(self, request: InterviewCreate) -> dict[str, Any]:
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
        weak_topics = await self.db.weak_topics(request.client_id)
        style = load_style_card(request.company)
        prompt = build_system_prompt(
            company=request.company,
            resume=request.resume,
            stress=request.stress,
            stress_level=request.stress_level,
            specialization=request.specialization,
            duration_minutes=request.duration_minutes,
            weak_topics=weak_topics,
        )
        interview_id = uuid.uuid4().hex
        first_question = initial_question(request.company)
        await self.db.create_interview(
            interview_id=interview_id,
            client_id=request.client_id,
            company=request.company,
            role=request.role,
            specialization=request.specialization,
            stress=request.stress,
            stress_level=request.stress_level,
            duration_minutes=request.duration_minutes,
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
            "specialization": request.specialization,
            "stress": request.stress,
            "stress_level": request.stress_level,
            "duration_minutes": request.duration_minutes,
        }

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
            duration_minutes=interview["duration_minutes"],
            weak_topics=interview["weak_topics"],
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
            for phrase in ("完全不会", "不知道", "不清楚", "没学过", "跳过")
        )
        failed = vague or obvious_error
        score = 2.5 if failed else (6.5 if len(answer) < 80 else 7.5)
        fallback = self._fallback_decision(interview, resume, turns, answer)
        fallback.assessment = TurnAssessment(
            score=score,
            failed=failed,
            dimension=fallback.assessment.dimension,
            topic=fallback.assessment.topic,
            deductions=["未说明原理、证据或边界条件"] if failed else [],
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
