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
    is_internal_interview_instruction,
    is_obvious_placeholder_answer,
    is_vague_answer,
    project_followup,
    select_questions,
    select_server_questions,
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
    resume_consistency: str
    resume_mismatch_reason: str
    resume_selection_warning: bool
    stage: dict[str, Any]
    turn: InterviewTurn


STAGE_LABELS = {
    "self_intro": "自我介绍",
    "project_deep_dive": "项目深挖",
    "fundamentals": "基础与场景题",
    "coding": "手撕代码",
    "hr_fit": "岗位匹配",
    "career_planning": "职业规划",
    "compensation": "薪酬沟通",
    "candidate_questions": "反问",
}
HR_STAGE_INDEX = {"hr_fit": 0, "career_planning": 1, "compensation": 2}


def interview_stage_plan(interview_type: str) -> list[str]:
    normalized = "technical_hr" if interview_type == "tech_hr" else interview_type
    if normalized == "hr":
        return ["self_intro", *HR_STAGE_INDEX, "candidate_questions"]
    plan = ["self_intro", "project_deep_dive", "fundamentals", "coding"]
    if normalized == "technical_hr":
        plan.extend(HR_STAGE_INDEX)
    return [*plan, "candidate_questions"]


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
        first_question = initial_question(
            request.company, request.language_mode, request.interview_type
        )
        effective_voice_mode = (
            "L3" if request.answer_mode == "text" else self.settings.voice_mode
        )
        stage_state = {
            "plan": interview_stage_plan(request.interview_type),
            "index": 0,
            "turn_count": 0,
            "revision": 1,
            "history": [],
        }
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
            voice_mode=effective_voice_mode,
            resume=request.resume,
            style=style,
            weak_topics=weak_topics,
            system_prompt=prompt,
            initial_question=first_question,
            stage_state=stage_state,
        )
        return {
            "id": interview_id,
            "status": "created",
            "voice_mode": effective_voice_mode,
            "answer_mode": "text" if effective_voice_mode == "L3" else "voice",
            "weak_topics": weak_topics,
            "initial_question": first_question,
            "recommended_answer_seconds": self._answer_time_allowance(
                self.recommended_answer_seconds(first_question),
                "text" if effective_voice_mode == "L3" else "voice",
            ),
            "company": request.company,
            "role": request.role,
            "interview_type": request.interview_type,
            "specialization": request.specialization,
            "language_mode": request.language_mode,
            "stress": request.stress,
            "stress_level": request.stress_level,
            "duration_minutes": request.duration_minutes,
            "memory_enabled": request.memory_enabled,
            "stage": self._stage_snapshot(stage_state),
        }

    @staticmethod
    def _stage_state(interview: dict[str, Any]) -> dict[str, Any]:
        raw = interview.get("stage_state") or {}
        plan = [
            item for item in raw.get("plan", [])
            if item in STAGE_LABELS
        ] or interview_stage_plan(str(interview.get("interview_type") or "technical"))
        index = max(0, min(int(raw.get("index") or 0), len(plan) - 1))
        return {
            "plan": plan,
            "index": index,
            "turn_count": max(0, int(raw.get("turn_count") or 0)),
            "revision": max(1, int(raw.get("revision") or 1)),
            "history": list(raw.get("history") or [])[-20:],
        }

    @staticmethod
    def _stage_snapshot(state: dict[str, Any]) -> dict[str, Any]:
        plan = list(state["plan"])
        index = int(state["index"])
        history = list(state.get("history") or [])
        skipped = {
            str(item.get("stage")) for item in history
            if isinstance(item, dict) and item.get("reason") == "user_skip"
        }
        stages = []
        for position, stage_id in enumerate(plan):
            status = (
                "current" if position == index
                else "skipped" if stage_id in skipped
                else "completed" if position < index
                else "upcoming"
            )
            stages.append(
                {
                    "id": stage_id,
                    "label": STAGE_LABELS[stage_id],
                    "status": status,
                }
            )
        next_stage = plan[index + 1] if index + 1 < len(plan) else None
        return {
            "revision": int(state.get("revision") or 1),
            "current": {
                "id": plan[index],
                "label": STAGE_LABELS[plan[index]],
                "index": index,
                "turn_count": int(state.get("turn_count") or 0),
            },
            "stages": stages,
            "can_skip": next_stage is not None,
            "next_stage": (
                {"id": next_stage, "label": STAGE_LABELS[next_stage]}
                if next_stage
                else None
            ),
        }

    def stage_snapshot(self, interview: dict[str, Any]) -> dict[str, Any]:
        return self._stage_snapshot(self._stage_state(interview))

    async def hint(self, interview_id: str) -> dict[str, Any]:
        interview = await self.db.get_interview(interview_id)
        if not interview:
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
        if interview["status"] != "active":
            if interview["status"] in {"ended", "reporting", "reported"}:
                raise AppError("INTERVIEW_ENDED", "本场面试已经结束", status_code=409)
            raise AppError("INTERVIEW_NOT_STARTED", "请先进入面试再获取提示", status_code=409)
        if interview.get("paused"):
            raise AppError("INTERVIEW_PAUSED", "本场面试已暂停", status_code=409)
        question = str(interview.get("last_question") or "").strip()
        if not question:
            raise AppError("QUESTION_NOT_READY", "当前问题还未准备好", status_code=409)
        turns = await self.db.list_turns(interview_id)
        ordinal = len(turns) + 1
        prior_levels = {
            int(item.get("level") or 1)
            for item in (interview.get("hint_events") or [])
            if isinstance(item, dict) and int(item.get("ordinal") or 0) == ordinal
        }
        level = 2 if 1 in prior_levels else 1
        hint = self._build_hint(
            question,
            str(interview.get("language_mode") or "zh"),
            level=level,
        )
        if level >= 2:
            hint = await self._recommended_answer_hint(interview, question, turns)
        event = await self.db.record_hint(
            interview_id,
            ordinal=ordinal,
            question=question,
            hint=hint,
            level=level,
        )
        if not event:
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
        return event

    async def _recommended_answer_hint(
        self,
        interview: dict[str, Any],
        question: str,
        turns: list[InterviewTurn],
    ) -> str:
        language_mode = str(interview.get("language_mode") or "zh")
        resume = ResumeData.model_validate(interview.get("resume") or {})
        fallback = self._personalized_hint_fallback(question, resume, language_mode)
        if self.settings.mock_llm:
            return fallback
        system = (
            "Write a first-person recommended answer to the current interview question. "
            "Use only personal facts present in the supplied resume or prior answers. General technical knowledge "
            "may explain mechanisms, but never invent personal actions, metrics, employers, or results. Mark missing "
            "personal details as [add your real detail]. Return one direct answer, not advice or an outline."
            if language_mode == "en"
            else
            "针对当前面试题写一段第一人称、可直接参考的推荐回答。个人经历只能使用所给简历和本场历史回答中的事实；"
            "技术原理可使用通用知识，但不得编造本人动作、公司、指标或结果，缺失信息写成【补充真实信息】。"
            "直接回答问题，不要输出答题建议、结构提纲或评分。允许保留 Java、Redis、P95 等英文技术术语。"
        )
        try:
            raw = await self.client.chat_json(
                [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "question": question,
                                "resume": resume.model_dump(by_alias=True),
                                "prior_answers": [
                                    {"question": turn.question, "answer": turn.answer}
                                    for turn in turns[-4:]
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                response_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                schema_name="interview_recommended_answer_hint",
                model=self.settings.qwen_text_model,
                temperature=0.2,
                max_tokens=900,
            )
            answer = " ".join(str(raw.get("answer") or "").split()).strip()
            if answer:
                label = "Recommended answer" if language_mode == "en" else "推荐回答"
                return f"{label}：{answer[:2400]}"
        except (LLMError, ValueError):
            pass
        return fallback

    @staticmethod
    def _personalized_hint_fallback(
        question: str, resume: ResumeData, language_mode: str
    ) -> str:
        project = resume.projects[0] if resume.projects else None
        internship = resume.internships[0] if resume.internships else None
        if language_mode == "en":
            if project:
                details = " ".join([*project.highlights[:1], *project.metrics[:1]])
                return (
                    f"Recommended answer: In my {project.name} project, I worked as {project.role or '[add your real role]'}. "
                    f"{details or '[add one action and result you can verify]'} For this question—{question}—"
                    "I would connect that experience to the relevant mechanism and state its failure boundary, "
                    "without adding any result I cannot verify."
                )
            return f"Recommended answer: For this question—{question}—[add your real conclusion, evidence, and boundary]."
        if project:
            details = "".join([*project.highlights[:1], *project.metrics[:1]])
            return (
                f"推荐回答：以我的“{project.name}”为例，我担任{project.role or '【补充真实角色】'}。"
                f"{details or '【补充一项本人真实动作和结果】'}针对“{question}”，"
                "我会结合这段真实实现说明相关机制、取舍和失效边界，不补造未验证的数据。"
            )
        if internship:
            details = "".join([*internship.highlights[:1], *internship.metrics[:1]])
            return (
                f"推荐回答：在{internship.company or '【补充真实公司】'}担任{internship.role or '【补充真实岗位】'}期间，"
                f"{details or '【补充本人真实动作和结果】'}针对“{question}”，我会据此说明具体行动、依据和复盘。"
            )
        return f"推荐回答：针对“{question}”，【补充你的真实结论、个人依据、具体行动和结果】。"

    def _question_for_stage(
        self,
        interview: dict[str, Any],
        state: dict[str, Any],
        *,
        resume: ResumeData,
        anchor: str = "",
        vague: bool = False,
    ) -> str:
        stage_id = state["plan"][state["index"]]
        language_mode = str(interview.get("language_mode") or "bilingual")
        if stage_id == "self_intro":
            return initial_question(
                str(interview.get("company") or "bytedance"),
                language_mode,
                str(interview.get("interview_type") or "technical"),
            )
        if stage_id == "project_deep_dive":
            return project_followup(
                max(1, min(7, int(state.get("turn_count") or 0) + 1)),
                anchor,
                resume,
                vague=vague,
                language_mode=language_mode,
            )[0]
        questions = select_server_questions(
            str(interview.get("company") or "bytedance"),
            list(interview.get("weak_topics") or []),
            interview.get("duration_minutes"),
            str(interview.get("specialization") or "通用后端"),
            selection_seed=str(interview.get("id") or "stage"),
            language_mode=language_mode,
            interview_type=str(interview.get("interview_type") or "technical"),
        )
        if stage_id == "fundamentals":
            pool = [
                item
                for item in questions
                if item.get("kind") not in {"behavioral", "coding"}
            ]
        elif stage_id == "coding":
            pool = [item for item in questions if item.get("kind") == "coding"]
        elif stage_id in HR_STAGE_INDEX:
            pool = [item for item in questions if item.get("kind") == "behavioral"]
        else:
            return (
                "What would you like to ask about the role, team, or work?"
                if language_mode == "en"
                else "最后留给你反问：关于岗位、团队或工作内容，你有什么想了解的？"
            )
        if not pool:
            next_state = self._next_stage_state(state, reason="unavailable")
            return self._question_for_stage(
                interview, next_state, resume=resume
            )
        turn_count = int(state.get("turn_count") or 0)
        # Fundamentals and HR sections use a reviewed opener followed
        # by at most one answer-anchored follow-up. This keeps the interview
        # responsive without letting a generated question replace the bank.
        item_index = (
            HR_STAGE_INDEX[stage_id]
            if stage_id in HR_STAGE_INDEX
            else turn_count // 2
            if stage_id == "fundamentals"
            else turn_count
        )
        item = pool[item_index % len(pool)]
        return str(item.get("question") or "").strip()

    @staticmethod
    def _next_stage_state(
        state: dict[str, Any], *, reason: str
    ) -> dict[str, Any]:
        next_state = {
            **state,
            "history": [
                *list(state.get("history") or []),
                {"stage": state["plan"][state["index"]], "reason": reason},
            ][-20:],
            "revision": int(state.get("revision") or 1) + 1,
            "turn_count": 0,
        }
        if next_state["index"] + 1 < len(next_state["plan"]):
            next_state["index"] += 1
        return next_state

    async def advance_stage(
        self, interview_id: str, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        interview = await self.db.get_interview(interview_id)
        if not interview:
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
        if interview["status"] in {"ended", "reporting", "reported"}:
            raise AppError("INTERVIEW_ENDED", "本场面试已经结束", status_code=409)
        if interview.get("paused"):
            raise AppError("INTERVIEW_PAUSED", "本场面试已暂停", status_code=409)
        state = self._stage_state(interview)
        if expected_revision is not None and expected_revision != state["revision"]:
            raise AppError("STALE_STAGE", "面试阶段已更新，请按最新进度操作", status_code=409)
        if state["index"] + 1 >= len(state["plan"]):
            raise AppError("NO_NEXT_STAGE", "当前已是最后阶段", status_code=409)
        state = self._next_stage_state(state, reason="user_skip")
        resume = ResumeData.model_validate(interview["resume"])
        question = self._question_for_stage(interview, state, resume=resume)
        await self.db.set_interview_stage(
            interview_id, stage_state=state, question=question
        )
        return {"question": question, "stage": self._stage_snapshot(state)}

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
    def _build_hint(
        question: str, language_mode: str = "bilingual", *, level: int = 1
    ) -> str:
        """Return a concrete scaffold, then a short illustrative answer."""

        lowered = question.lower()
        if level >= 2:
            if language_mode == "en":
                if any(marker in lowered for marker in ("project", "request", "traffic", "failure", "metric")):
                    return (
                        "Example shape: ‘The goal was to reduce checkout timeouts. I owned the service layer, "
                        "moved the slow dependency off the synchronous path, and added timeout and fallback controls. "
                        "I would prove the result with p95 latency and error rate, while noting that delayed consistency "
                        "is the trade-off.’ Replace every detail with facts from your own project."
                    )
                return (
                    "Example shape: ‘My conclusion is X because mechanism Y changes condition Z. "
                    "It works when A holds, but under B I would choose C instead.’ Replace X/Y/Z/A/B/C "
                    "with facts you can defend."
                )
            if any(marker in lowered for marker in ("手撕", "算法", "复杂度", "代码", "实现")):
                return (
                    "简化示例：‘我先用一次遍历确认约束；核心状态记录已经见过的信息，"
                    "每步只做常数次查询，因此时间复杂度 O(n)、额外空间 O(n)。我会补空输入、重复值和极端规模测试。’"
                    "请按本题真实数据结构替换，不能直接套用复杂度。"
                )
            if "mysql" in lowered or any(marker in question for marker in ("数据库", "事务", "索引", "慢查询")):
                return (
                    "简化示例：‘我先用执行计划确认扫描行数和索引命中，再根据查询条件与排序设计联合索引；"
                    "上线前对比 P95 延迟和写入成本。若选择性低或写多读少，我不会盲目加索引。’"
                )
            if "redis" in lowered or any(marker in question for marker in ("缓存", "过期", "热 key", "库存")):
                return (
                    "简化示例：‘读请求先查缓存，未命中再回源并受并发合并保护；更新时以数据库为准并删除缓存。"
                    "我会观察命中率、回源量和不一致窗口，热点或强一致场景需要换方案。’"
                )
            if any(marker in question for marker in ("项目", "请求", "流量", "故障", "指标", "架构")):
                return (
                    "简化示例：‘这个功能解决的是用户在高峰期请求失败的问题。我负责服务层改造，"
                    "把非关键步骤异步化，并加入超时和降级；用 P95 延迟与错误率验证。代价是结果可能短暂延迟。’"
                    "请把场景、本人动作和指标替换成你的真实经历。"
                )
            return (
                "简化示例：‘我的结论是 X，因为 Y 机制在 Z 条件下成立；它的边界是 A，"
                "如果出现 B，我会改用 C。’请把占位内容替换为本题事实，不要编造经历或指标。"
            )
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
        control_intent: str = "",
    ) -> EngineResult:
        interview = await self.db.get_interview(interview_id)
        if not interview:
            raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
        if interview["status"] in {"ended", "reporting", "reported"}:
            raise AppError("INTERVIEW_ENDED", "本场面试已经结束", status_code=409)
        if interview.get("paused"):
            raise AppError("INTERVIEW_PAUSED", "本场面试已暂停", status_code=409)
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
        stage_state = self._stage_state(interview)
        current_stage = stage_state["plan"][stage_state["index"]]
        question_elapsed = interview.get("question_elapsed_seconds")
        decision_interview = {
            **interview,
            "_answer_input_mode": "voice" if input_mode == "voice" else "text",
            "_answer_duration_seconds": (
                question_elapsed
                if question_elapsed is not None
                else answer_duration_seconds
            ),
        }
        decision = await self._decide(decision_interview, resume, turns, answer)
        vague_answer = is_vague_answer(answer)
        obvious_non_answer = is_obvious_placeholder_answer(answer) or re.sub(
            r"[\s，。！？,.!?]", "", answer
        ).casefold() in {"hello", "hi", "hey", "你好", "您好"}
        explicit_unknown = self._explicit_unknown(answer)
        unknown_control = control_intent == "unknown"
        project_ownership_correction = self._explicit_project_ownership_correction(
            answer
        )
        explicit_resume_mismatch = self._explicit_resume_mismatch(answer)
        needs_clarification = (
            obvious_non_answer
            and not explicit_unknown
            and not unknown_control
            and not project_ownership_correction
            and not explicit_resume_mismatch
        )
        experience_names = [
            value
            for value in [
                *(project.name for project in resume.projects),
                *(
                    experience.company or experience.role
                    for experience in resume.internships
                ),
            ]
            if value
        ]
        normalized_answer = re.sub(r"\W+", "", answer).casefold()
        mentions_experience = any(
            re.sub(r"\W+", "", value).casefold() in normalized_answer
            for value in experience_names
            if len(re.sub(r"\W+", "", value)) >= 2
        ) or any(
            marker in answer.casefold()
            for marker in (
                "项目",
                "实习",
                "我负责",
                "我主要负责",
                "本人负责",
                "做过",
                "开发过",
                "project",
                "internship",
                "worked on",
                "responsible for",
                "i built",
                "i developed",
            )
        )
        intro_experience_missing = (
            current_stage == "self_intro"
            and int(stage_state.get("turn_count") or 0) == 0
            and bool(experience_names)
            and not mentions_experience
            and not needs_clarification
            and not explicit_unknown
            and not unknown_control
            and not explicit_resume_mismatch
        )
        unknown_in_project_opening = (
            current_stage == "project_deep_dive"
            and int(stage_state.get("turn_count") or 0) == 0
            and (explicit_unknown or unknown_control)
        )
        unknown_in_project_followup = (
            current_stage == "project_deep_dive"
            and int(stage_state.get("turn_count") or 0) > 0
            and (explicit_unknown or unknown_control)
        )
        prior_project_corrections = [
            turn
            for turn in turns
            if self._explicit_project_ownership_correction(turn.answer)
        ]
        rejected_questions = [
            turn.question
            for turn in turns
            if self._explicit_project_ownership_correction(turn.answer)
            or (
                turn.topic.startswith("项目深挖")
                and turn.drill_depth <= 1
                and self._explicit_unknown(turn.answer)
            )
        ]
        if project_ownership_correction:
            rejected_questions.append(str(interview.get("last_question") or ""))
        if unknown_in_project_opening:
            rejected_questions.append(str(interview.get("last_question") or ""))
        drill_resume = self._resume_without_rejected_projects(
            resume, rejected_questions
        )
        switch_project_after_unknown = (
            unknown_in_project_opening
            and bool(drill_resume.projects or drill_resume.internships)
        )
        anchor = decision.anchor_keyword.strip()
        if not anchor or anchor.lower() not in answer.lower():
            anchor = extract_anchor_keyword(answer, drill_resume)
        # Bank follow-ups must stay tied to this answer. Project drilling may
        # deliberately reuse a previous project anchor for a vague response,
        # so retain the direct response anchor before that substitution.
        response_anchor = anchor
        if project_ownership_correction:
            anchor = ""
            response_anchor = ""
        elif needs_clarification:
            anchor = ""
            response_anchor = ""
        elif explicit_unknown:
            anchor = ""
            response_anchor = ""
        project_followup_anchor = anchor or (
            next(
                (
                    turn.anchor_keyword
                    for turn in reversed(turns)
                    if turn.topic.startswith("项目深挖") and turn.anchor_keyword
                ),
                "",
            )
            if unknown_in_project_followup
            else ""
        )

        completed_turns = len(turns) + 1
        last_project_correction_ordinal = (
            completed_turns
            if project_ownership_correction
            else prior_project_corrections[-1].ordinal
            if prior_project_corrections
            else 0
        )
        project_drill_completed_turns = (
            completed_turns - last_project_correction_ordinal + 1
            if last_project_correction_ordinal
            else completed_turns
        )
        resume_mismatch_reason = " ".join(
            str(decision.resume_mismatch_reason or "").replace("\x00", "").split()
        )[:600]
        if is_internal_interview_instruction(resume_mismatch_reason):
            resume_mismatch_reason = ""
        resume_mismatch = not project_ownership_correction and (
            explicit_resume_mismatch
            or (decision.resume_consistency == "mismatch" and bool(resume_mismatch_reason))
        )
        if project_ownership_correction:
            resume_mismatch_reason = ""
        if explicit_resume_mismatch and not resume_mismatch_reason:
            resume_mismatch_reason = "候选人明确表示当前简历并非本人材料或选择有误。"
        resume_selection_warning = completed_turns == 1 and resume_mismatch and (
            decision.resume_selection_warning or explicit_resume_mismatch
        )
        interview_type = str(interview.get("interview_type") or "technical")
        drill_target = interview_drill_target(
            interview["weak_topics"], interview_type
        )
        question, _, _ = enforce_project_drill(
            decision.next_question,
            completed_turns=project_drill_completed_turns,
            anchor=anchor,
            resume=drill_resume,
            vague=(
                vague_answer
                and not explicit_unknown
                and not project_ownership_correction
            ),
            max_depth=drill_target,
            language_mode=str(interview.get("language_mode") or "bilingual"),
        )
        server_questions = select_server_questions(
            interview["company"],
            interview["weak_topics"],
            interview["duration_minutes"],
            interview["specialization"],
            selection_seed=interview["id"],
            language_mode=str(interview.get("language_mode") or "bilingual"),
            interview_type=interview_type,
        )
        hr_questions = [
            item for item in server_questions if item.get("kind") == "behavioral"
        ]
        question = self._sanitize_question(
            question,
            interview["company"],
            language_mode=str(interview.get("language_mode") or "bilingual"),
        )
        if project_ownership_correction:
            question = (
                "Understood. We will not treat that project as your experience. "
                + question
                if interview.get("language_mode") == "en"
                else "明白，这个项目不作为你的经历继续追问。" + question
            )

        next_stage_state = {
            **stage_state,
            "turn_count": int(stage_state.get("turn_count") or 0)
            + (0 if needs_clarification else 1),
        }
        if project_ownership_correction:
            next_stage_state["turn_count"] = 0
        elif switch_project_after_unknown:
            next_stage_state["turn_count"] = 0
        if (
            (explicit_unknown or unknown_control)
            and current_stage in {"fundamentals", *HR_STAGE_INDEX}
            and next_stage_state["turn_count"] % 2 == 1
        ):
            # Skip both the current reviewed opener and its pending follow-up
            # before deciding whether this also completes the stage.
            next_stage_state["turn_count"] += 1
        should_advance_stage = False
        stage_reason = "coverage"
        if project_ownership_correction:
            current_dimension = "communication"
            current_topic = "项目归属澄清"
            current_drill_dimension = ""
            current_drill_depth = 0
        elif current_stage == "self_intro":
            should_advance_stage = not intro_experience_missing
        elif current_stage == "project_deep_dive":
            should_advance_stage = (
                (unknown_in_project_opening and not switch_project_after_unknown)
                or (
                    next_stage_state["turn_count"] >= 2
                    and (vague_answer or decision.assessment.failed)
                    and not unknown_in_project_followup
                )
                or next_stage_state["turn_count"]
                >= interview_drill_target(
                    interview["weak_topics"], str(interview.get("interview_type") or "technical")
                )
            )
            stage_reason = "unknown" if explicit_unknown or unknown_control else "adaptive"
        elif current_stage == "fundamentals":
            should_advance_stage = next_stage_state["turn_count"] >= 4
        elif current_stage == "coding":
            should_advance_stage = True
        elif current_stage in HR_STAGE_INDEX:
            should_advance_stage = next_stage_state["turn_count"] >= 2

        if needs_clarification:
            should_advance_stage = False

        if should_advance_stage:
            next_stage_state = self._next_stage_state(
                next_stage_state, reason=stage_reason
            )
        else:
            next_stage_state["revision"] = int(stage_state.get("revision") or 1) + 1
        next_stage_id = next_stage_state["plan"][next_stage_state["index"]]
        next_item: dict[str, Any] | None = None
        if next_stage_id == current_stage and current_stage == "project_deep_dive":
            proposed = self._sanitize_question(
                decision.next_question,
                interview["company"],
                language_mode=str(interview.get("language_mode") or "bilingual"),
            )
            answer_terms = {
                token.casefold()
                for token in re.findall(
                    r"[A-Za-z][A-Za-z0-9+.#_-]{2,}|[\u4e00-\u9fff]{2,10}",
                    answer,
                )
                if token not in {"这个", "那个", "我们", "然后", "就是", "项目", "回答"}
            }
            responsive = any(term in proposed.casefold() for term in answer_terms)
            if (
                proposed
                and not is_internal_interview_instruction(proposed)
                and not explicit_unknown
                and not unknown_control
                and not project_ownership_correction
                and responsive
            ):
                question = proposed
            else:
                question = self._question_for_stage(
                    interview,
                    next_stage_state,
                    resume=drill_resume,
                    anchor=project_followup_anchor,
                    vague=vague_answer,
                )
        elif (
            current_stage in {"fundamentals", *HR_STAGE_INDEX}
            and next_stage_id == current_stage
            and next_stage_state["turn_count"] % 2 == 1
        ):
            stage_pool = (
                [item for item in server_questions if item.get("kind") == "behavioral"]
                if current_stage in HR_STAGE_INDEX
                else [
                    item
                    for item in server_questions
                    if item.get("kind") not in {"behavioral", "coding"}
                ]
            )
            answered_index = (
                HR_STAGE_INDEX[current_stage]
                if current_stage in HR_STAGE_INDEX
                else int(stage_state.get("turn_count") or 0) // 2
            )
            bank_item = stage_pool[answered_index % len(stage_pool)] if stage_pool else None
            next_item = bank_item
            question = (
                self._anchored_bank_followup(
                    decision.next_question,
                    answer=answer,
                    anchor=response_anchor,
                    bank_item=bank_item,
                    track=(
                        "hr"
                        if current_stage in HR_STAGE_INDEX
                        else "technical"
                    ),
                    vague=vague_answer,
                    language_mode=str(interview.get("language_mode") or "bilingual"),
                )
                if bank_item
                else self._question_for_stage(
                    interview, next_stage_state, resume=drill_resume, anchor=anchor
                )
            )
        else:
            question = self._question_for_stage(
                interview,
                next_stage_state,
                resume=drill_resume,
                anchor="" if explicit_unknown or unknown_control else anchor,
            )
        if needs_clarification:
            if current_stage == "self_intro":
                question = (
                    "Hello. Start with your current studies and the technical direction you want to pursue."
                    if interview.get("language_mode") == "en"
                    else "你好。先简单说说你目前的学习进度，以及接下来想做的技术方向。"
                )
            else:
                question = (
                    "I haven't heard an answer to the current question yet. Start with what you can confirm; if you genuinely don't know, just say so."
                    if interview.get("language_mode") == "en"
                    else "我还没有听到对当前问题的回答。先说你能确认的部分；如果确实不知道，直接说不知道即可。"
                )
        elif intro_experience_missing:
            experience_name = experience_names[0]
            if experience_name.startswith("[匿名 Profile 项目]"):
                question = (
                    f'The profile materials mention "{experience_name}". First confirm whether '
                    "you personally worked on it; if so, briefly say what it did and what you owned."
                    if interview.get("language_mode") == "en"
                    else f"材料中提到“{experience_name}”，需要你先确认是否亲自参与。"
                    "如果是，请用两三句话说说它做什么，以及你负责什么。"
                )
            else:
                question = (
                    f'One more part for the introduction: your resume mentions "{experience_name}". '
                    "In two or three sentences, what did it do and what were you responsible for?"
                    if interview.get("language_mode") == "en"
                    else f"自我介绍里再补一小段经历：简历中提到“{experience_name}”。"
                    "请用两三句话说说它做什么，以及你负责什么。"
                )
        elif project_ownership_correction:
            if current_stage == "self_intro":
                remaining_name = next(
                    (
                        value
                        for value in [
                            *(project.name for project in drill_resume.projects),
                            *(
                                experience.company or experience.role
                                for experience in drill_resume.internships
                            ),
                        ]
                        if value
                    ),
                    "",
                )
                if remaining_name:
                    question = (
                        f'Briefly include "{remaining_name}" in your introduction instead: '
                        "what did it do, and what were you responsible for?"
                        if interview.get("language_mode") == "en"
                        else f"自我介绍里改为简单讲讲“{remaining_name}”："
                        "它做什么，以及你负责什么？"
                    )
            acknowledgement = (
                "Understood. We will not treat that project as your experience. "
                if interview.get("language_mode") == "en"
                else "明白，这个项目不作为你的经历继续追问。"
            )
            if not question.startswith(acknowledgement):
                question = acknowledgement + question

        # A row represents the question that was just answered, not the next
        # question generated above. Normalize server-forced phases accordingly
        # so reports and next-session memory are not shifted by one turn.
        current_dimension = decision.assessment.dimension
        current_topic = decision.assessment.topic
        current_drill_dimension = decision.drill_dimension
        current_drill_depth = decision.drill_depth
        answered_bank_item: dict[str, Any] | None = None
        if project_ownership_correction:
            current_dimension = "communication"
            current_topic = "项目归属澄清"
            current_drill_dimension = ""
            current_drill_depth = 0
        elif completed_turns == 1:
            current_dimension = "communication"
            current_topic = "自我介绍·整体与学习情况"
            current_drill_dimension = ""
            current_drill_depth = 0
        elif 2 <= project_drill_completed_turns <= drill_target + 1:
            current_drill_depth = project_drill_completed_turns - 1
            current_drill_dimension = SEVEN_DRILL_DIMENSIONS[
                min(current_drill_depth - 1, len(SEVEN_DRILL_DIMENSIONS) - 1)
            ]
            current_dimension = "project_depth"
            current_topic = f"项目深挖·{current_drill_dimension}"
        if project_ownership_correction:
            current_dimension = "communication"
            current_topic = "项目归属澄清"
            current_drill_dimension = ""
            current_drill_depth = 0
        elif current_stage == "self_intro":
            current_dimension = "communication"
            current_topic = "自我介绍·整体与学习情况"
            current_drill_dimension = ""
            current_drill_depth = 0
        elif current_stage == "project_deep_dive":
            depth = min(int(stage_state.get("turn_count") or 0), len(SEVEN_DRILL_DIMENSIONS) - 1)
            dimension = SEVEN_DRILL_DIMENSIONS[depth]
            current_dimension = "project_depth"
            current_topic = f"项目深挖·{dimension}"
            current_drill_dimension = dimension
            current_drill_depth = depth + 1
        elif current_stage == "fundamentals":
            stage_pool = [
                item
                for item in server_questions
                if item.get("kind") not in {"behavioral", "coding"}
            ]
            item_index = int(stage_state.get("turn_count") or 0) // 2
            answered_bank_item = stage_pool[item_index % len(stage_pool)] if stage_pool else None
            current_dimension = "fundamentals"
            current_topic = str(
                (answered_bank_item or {}).get("topic")
                or (answered_bank_item or {}).get("category")
                or "基础与场景题"
            )
            current_drill_dimension = "基础知识"
            current_drill_depth = int(stage_state.get("turn_count") or 0) % 2
        elif current_stage == "coding":
            coding_pool = [item for item in server_questions if item.get("kind") == "coding"]
            answered_bank_item = coding_pool[0] if coding_pool else None
            current_dimension = "coding_thought"
            current_topic = f"手撕思路·{(answered_bank_item or {}).get('topic') or '数据结构与算法'}"
            current_drill_dimension = "手撕思路"
        elif current_stage in HR_STAGE_INDEX:
            item_index = HR_STAGE_INDEX[current_stage]
            answered_bank_item = hr_questions[item_index % len(hr_questions)] if hr_questions else None
            current_dimension = "communication"
            current_topic = f"综合面·{(answered_bank_item or {}).get('topic') or STAGE_LABELS[current_stage]}"
            current_drill_dimension = ""
            current_drill_depth = int(stage_state.get("turn_count") or 0) % 2
        elif current_stage == "candidate_questions":
            current_dimension = "communication"
            current_topic = "反问"
            current_drill_dimension = ""

        if resume_selection_warning:
            pressure_action = "none"
            question = (
                "Your introduction appears inconsistent with the selected resume. "
                "Could you confirm whether you chose the wrong resume? You may clarify and continue, "
                "or select Exit to return home and start again."
                if interview.get("language_mode") == "en"
                else "你的自我介绍与当前简历中的基础经历明显不一致。请确认是否选错了简历？你可以澄清后继续，也可以点击“退出”返回首页重新选择。"
            )
        elif project_ownership_correction:
            pressure_action = "none"
        elif needs_clarification:
            pressure_action = "none"
        elif explicit_unknown:
            pressure_action = "none"
            transition = (
                "Understood. Let's discuss another project. "
                if switch_project_after_unknown
                and interview.get("language_mode") == "en"
                else "明白，我们换一个项目。"
                if switch_project_after_unknown
                else "Understood. Let's try another angle within this project. "
                if unknown_in_project_followup
                and next_stage_id == current_stage
                and interview.get("language_mode") == "en"
                else "这个点先放下，我们换个角度。"
                if unknown_in_project_followup and next_stage_id == current_stage
                else "Understood. Let's move on. "
                if interview.get("language_mode") == "en"
                else "明白，我们换一道。"
            )
            question = transition + question
        elif next_stage_id != current_stage:
            pressure_action = "none"
        elif (
            current_stage in {"fundamentals", *HR_STAGE_INDEX}
            and next_stage_state["turn_count"] % 2 == 0
        ):
            pressure_action = "none"
        elif completed_turns == 1 or next_stage_id in HR_STAGE_INDEX:
            # The introduction-to-experience handoff and the three required HR
            # openers should sound like coherent phase transitions. Pressure
            # resumes inside technical follow-ups instead of adding a generic
            # confrontational prefix at these boundaries.
            pressure_action = "none"
        else:
            pressure_action = self._pressure_action(
                stress_level=interview["stress_level"],
                ordinal=completed_turns,
                proposed=(
                    "interrupt"
                    if resume_mismatch and interview["stress_level"] >= 2
                    else "challenge"
                    if resume_mismatch
                    else decision.pressure_action
                ),
                expression_problem=self._has_expression_problem(
                    answer, decision.assessment.deductions
                ) or resume_mismatch,
            )
        question = self._apply_pressure_copy(
            question,
            pressure_action,
            ordinal=completed_turns,
            language_mode=str(interview.get("language_mode") or "bilingual"),
        )
        recommended_seconds = self._answer_time_allowance(
            self._recommended_seconds_for_item(next_item, question), input_mode
        )
        current_recommended_seconds = self._answer_time_allowance(
            self._recommended_seconds_for_item(
                answered_bank_item, str(interview["last_question"])
            ),
            input_mode,
        )
        capture_duration = (
            round(max(0.0, min(float(answer_duration_seconds), 3600.0)), 2)
            if answer_duration_seconds is not None
            else None
        )
        normalized_duration = (
            round(max(0.0, min(float(question_elapsed), 3600.0)), 2)
            if question_elapsed is not None
            else capture_duration
        )
        speech_rate_cpm = (
            round(len(re.sub(r"\s+", "", answer)) * 60 / capture_duration, 1)
            if input_mode == "voice" and capture_duration
            else None
        )
        turn = InterviewTurn(
            ordinal=completed_turns,
            question=interview["last_question"],
            answer=answer,
            category=current_dimension,
            topic=current_topic or "综合基础",
            score=None if project_ownership_correction else decision.assessment.score,
            scorable=(
                False
                if project_ownership_correction
                else decision.assessment.scorable and decision.assessment.score is not None
            ),
            score_source=(
                "unavailable"
                if project_ownership_correction
                else decision.assessment.score_source
            ),
            deductions=(
                []
                if project_ownership_correction
                else [
                    *decision.assessment.deductions,
                    f"简历一致性待澄清：{resume_mismatch_reason}",
                ]
                if resume_mismatch and resume_mismatch_reason
                else decision.assessment.deductions
            )
            or (
                ["回答缺少可验证的关键细节"]
                if decision.assessment.failed and not project_ownership_correction
                else []
            ),
            # A candid skip remains a low-scoring knowledge gap, but does not
            # count toward the consecutive-breakdown auto-end threshold.
            failed=(
                decision.assessment.failed
                and not explicit_unknown
                and not project_ownership_correction
            ),
            drill_dimension=current_drill_dimension,
            drill_depth=current_drill_depth,
            anchor_keyword=anchor,
            input_mode="voice" if input_mode == "voice" else "text",
            answer_duration_seconds=normalized_duration,
            speech_rate_cpm=speech_rate_cpm,
            recommended_answer_seconds=current_recommended_seconds,
        )
        streak = await self.db.append_turn(
            interview_id, turn, question, stage_state=next_stage_state
        )
        threshold = self._breakdown_threshold(interview["stress_level"])
        completed_normally = current_stage == "candidate_questions"
        ended = streak >= threshold or completed_normally
        end_reason: str | None = None
        if ended:
            end_reason = "completed" if completed_normally else "poor_performance"
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
            resume_consistency=(
                "uncertain"
                if project_ownership_correction
                else "mismatch"
                if resume_mismatch
                else decision.resume_consistency
            ),
            resume_mismatch_reason=resume_mismatch_reason,
            resume_selection_warning=resume_selection_warning,
            stage=self._stage_snapshot(next_stage_state),
            turn=turn,
        )

    @staticmethod
    def _anchored_bank_followup(
        candidate: str,
        *,
        answer: str,
        anchor: str,
        bank_item: dict[str, Any],
        track: str,
        vague: bool,
        language_mode: str,
    ) -> str:
        """Keep a generated follow-up only when it is demonstrably anchored.

        A bank follow-up has two independent anchors: the reviewed main
        question and a concrete word from the candidate's answer. Requiring
        both prevents an off-topic answer from steering the next question away
        from the reviewed item. Fallbacks keep one candidate detail and ask
        one question instead of reciting an evaluation checklist.
        """

        item_language = str(bank_item.get("language") or "").lower()
        item_question = str(bank_item.get("question") or "")
        english = language_mode == "en" or (
            language_mode == "bilingual"
            and (
                item_language == "en"
                or (
                    bool(re.search(r"[A-Za-z]", item_question))
                    and not re.search(r"[\u4e00-\u9fff]", item_question)
                )
            )
        )
        clean_answer = " ".join(str(answer or "").split()).strip()
        clean_anchor = " ".join(str(anchor or "").split()).strip("“”\"'，,。.!！?")
        if len(clean_anchor) < 2 or clean_anchor.casefold() not in clean_answer.casefold():
            answer_tokens = re.findall(
                r"[A-Za-z][A-Za-z0-9+.#_-]{2,}|[\u4e00-\u9fff]{2,8}",
                clean_answer,
            )
            clean_anchor = next(
                (token for token in answer_tokens if len(token.strip()) >= 2),
                "no concrete evidence" if english else "未给出具体依据",
            )
        if english and re.search(r"[\u4e00-\u9fff]", clean_anchor):
            english_tokens = re.findall(
                r"[A-Za-z][A-Za-z0-9+.#_-]*(?:\s+[A-Za-z][A-Za-z0-9+.#_-]*){0,4}",
                clean_answer,
            )
            clean_anchor = next(
                (value.strip() for value in english_tokens if len(value.strip()) >= 3),
                "no concrete evidence",
            )
        clean_anchor = clean_anchor[:64]

        topic = " ".join(str(bank_item.get("topic") or "").split()).strip()
        category = " ".join(str(bank_item.get("category") or "").split()).strip()
        normalized_item_question = " ".join(item_question.split()).strip()
        if english:
            # Runtime English variants can retain a Chinese canonical topic,
            # so the reviewed English question is the safest exact context.
            bank_context = normalized_item_question or (
                category if not re.search(r"[\u4e00-\u9fff]", category) else "the original question"
            )
            bank_context = bank_context[:140].rstrip(" ?")
        else:
            bank_context = topic or category or normalized_item_question or "原问题"
            bank_context = bank_context[:56].rstrip()

        theme_markers: set[str] = set()
        for value in (topic, category):
            for part in re.split(r"[/|·:：]", value):
                marker = " ".join(part.split()).strip().casefold()
                if len(marker) >= 2:
                    theme_markers.add(marker)
        english_stopwords = {
            "about",
            "after",
            "could",
            "does",
            "explain",
            "first",
            "from",
            "have",
            "into",
            "question",
            "should",
            "their",
            "under",
            "what",
            "when",
            "where",
            "which",
            "would",
        }
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+.#_-]{2,}", item_question):
            marker = token.casefold()
            if marker not in english_stopwords:
                theme_markers.add(marker)

        proposed = str(candidate or "").strip()
        if is_internal_interview_instruction(proposed):
            proposed = ""
        answer_anchored = bool(
            proposed
            and clean_anchor
            and clean_anchor.casefold() in proposed.casefold()
        )
        topic_anchored = any(
            marker in proposed.casefold() for marker in theme_markers
        )
        anchored = answer_anchored and topic_anchored
        if english and re.search(r"[\u4e00-\u9fff]", proposed):
            anchored = False
        if anchored and track == "hr":
            evidence_markers = (
                ("action", "result", "evidence", "specific", "measure", "learn")
                if english
                else ("行动", "结果", "证据", "具体", "验证", "复盘", "你做")
            )
            anchored = any(marker in proposed.casefold() for marker in evidence_markers)
        if anchored:
            return proposed

        if english:
            if track == "hr":
                return (
                    f'You mentioned "{clean_anchor}". Which specific experience best '
                    "shows that choice, and what did you personally do?"
                )
            return (
                f'While discussing "{bank_context}", you mentioned "{clean_anchor}". '
                "When would that claim stop being true?"
            )
        if track == "hr":
            return (
                f"你提到“{clean_anchor}”。哪段具体经历最能说明这个选择，"
                "当时你本人做了什么？"
            )
        return (
            f"你在聊“{bank_context}”时提到“{clean_anchor}”。"
            "这个判断在什么情况下会不成立？"
        )

    @classmethod
    def _recommended_seconds_for_item(
        cls, item: dict[str, Any] | None, question: str
    ) -> int:
        if item is not None:
            try:
                suggested = int(item.get("suggested_seconds"))
            except (TypeError, ValueError):
                suggested = 0
            if suggested > 0:
                return max(30, min(suggested, 600))
        return cls.recommended_answer_seconds(question)

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
        fallback_duration = (
            round(max(0.0, min(float(answer_duration_seconds), 3600.0)), 2)
            if answer_duration_seconds is not None
            else None
        )
        question_elapsed = interview.get("question_elapsed_seconds")
        duration = (
            round(max(0.0, min(float(question_elapsed), 3600.0)), 2)
            if question_elapsed is not None
            else fallback_duration
        )
        speech_rate = (
            round(len(re.sub(r"\s+", "", normalized)) * 60 / fallback_duration, 1)
            if input_mode == "voice" and fallback_duration
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
            {
                "question": turn.question,
                "answer": turn.answer,
                "topic": turn.topic,
                "anchor": turn.anchor_keyword,
            }
            for turn in turns[-8:]
        ]
        stage_state = self._stage_state(interview)
        current_stage = stage_state["plan"][stage_state["index"]]
        confirmed_facts = [
            {
                "topic": turn.topic,
                "anchor": turn.anchor_keyword,
                "candidate_words": turn.answer[:180],
            }
            for turn in turns[-8:]
            if turn.anchor_keyword
        ][-5:]
        user_payload = {
            "interview_context": {
                "current_stage": {
                    "id": current_stage,
                    "label": STAGE_LABELS[current_stage],
                    "topic_turn": stage_state["turn_count"],
                },
                "completed_stages": [
                    item.get("stage") for item in stage_state["history"]
                ],
                "confirmed_facts": confirmed_facts,
                "previous_gap": (
                    turns[-1].deductions[:2]
                    if turns and stage_state["turn_count"]
                    else []
                ),
                "continuity": {
                    "stage_transition_authority": "server",
                    "resolve_current_topic_before_transition": True,
                    "delivery": "Use one concrete detail from the candidate's latest answer, then ask one natural professional question. Do not repeat the full answer, restate the original question, use canned praise, or ask a checklist of subquestions.",
                },
            },
            "recent_transcript": recent,
            "current_question": interview["last_question"],
            "candidate_answer": answer,
            "answer_input": {
                "mode": interview.get("_answer_input_mode") or "text",
                "elapsed_seconds": interview.get("_answer_duration_seconds"),
                "timing_rule": (
                    "文字作答包含阅读、组织和输入时间，允许更长；不得仅因耗时扣分。"
                    if interview.get("_answer_input_mode") != "voice"
                    else "语音时长仅作表达节奏参考，不单独决定得分。"
                ),
            },
            "instruction": (
                "先结合当前问题、最近问答和简历事实理解候选人实际表达；"
                "简短但相关的回答也要自然承接，只有明确无关时才澄清。"
                "完成私有评分并生成下一问。只输出 JSON。"
            ),
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
        explicit_mismatch = self._explicit_resume_mismatch(answer)
        fallback.resume_consistency = "mismatch" if explicit_mismatch else "supported"
        fallback.resume_mismatch_reason = (
            "候选人明确表示当前简历并非本人材料或选择有误。"
            if explicit_mismatch else ""
        )
        fallback.resume_selection_warning = explicit_mismatch and not turns
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
        # A deliberate post-answer pause is reserved for standard/high
        # pressure and never replaces the first transition. Unlike an
        # interruption it does not presume an expression problem.
        if proposed == "silence" and stress_level >= 2 and ordinal >= 2:
            return "silence"
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
        lowered = answer.casefold()
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
        if any(
            marker in lowered
            for marker in (
                "i'm going in circles",
                "i am going in circles",
                "i lost my train of thought",
                "let me start over",
                "i'm not expressing this clearly",
                "i am not expressing this clearly",
            )
        ):
            return True
        filler_count = sum(
            answer.count(marker)
            for marker in ("嗯", "呃", "那个", "就是说", "怎么说呢")
        )
        english_fillers = len(
            re.findall(r"(?<![A-Za-z])(?:um+|uh+)(?![A-Za-z])", lowered)
        )
        english_fillers += len(
            re.findall(r"\b(?:i\s+think\s+maybe|maybe\s+i\s+think|you\s+know|i\s+mean)\b", lowered)
        )
        english_word_count = len(re.findall(r"\b[A-Za-z]+(?:'[A-Za-z]+)?\b", answer))
        return (
            (len(compact) >= 220 and filler_count >= 4)
            or english_fillers >= 5
            or (english_word_count >= 30 and english_fillers >= 3)
        )

    @staticmethod
    def _breakdown_threshold(stress_level: int) -> int:
        return 2 if stress_level >= 2 else 3

    @staticmethod
    def _explicit_resume_mismatch(answer: str) -> bool:
        normalized = " ".join(str(answer or "").casefold().split())
        return any(
            pattern.search(normalized)
            for pattern in (
                re.compile(r"(?:这|当前|这个|所选)?份?简历(?:不(?:是|属于)我|选错了|拿错了)"),
                re.compile(r"我(?:好像|可能|应该)?选错(?:了)?简历"),
                re.compile(r"这(?:些|个)(?:项目|经历|学校|实习).{0,8}不是我的"),
                re.compile(r"(?:wrong|incorrect) resume\b", re.I),
                re.compile(r"(?:this|the) resume (?:isn't|is not) mine\b", re.I),
                re.compile(r"I (?:selected|chose|uploaded) the wrong resume\b", re.I),
            )
        )

    @staticmethod
    def _explicit_project_ownership_correction(answer: str) -> bool:
        normalized = " ".join(str(answer or "").casefold().split())
        return any(
            pattern.search(normalized)
            for pattern in (
                re.compile(r"我(?:没|没有|未)(?:做过|参与过|负责过)(?:这|这个|该)?项目"),
                re.compile(r"(?:这|这个|该)项目(?:并)?不是我(?:做|参与|负责)的"),
                re.compile(r"(?:这|这个|该)项目(?:并)?不(?:在|属于)我(?:的)?简历(?:里|上|中)?"),
                re.compile(r"(?:这|这个|该)项目不是我简历(?:里|上|中)?的项目"),
                re.compile(
                    r"I (?:did not|didn't|never) work on (?:this|that) project\b",
                    re.I,
                ),
                re.compile(
                    r"(?:this|that) project (?:is not|isn't) (?:mine|on my resume)\b",
                    re.I,
                ),
            )
        )

    @staticmethod
    def _resume_without_rejected_projects(
        resume: ResumeData, rejected_questions: list[str]
    ) -> ResumeData:
        if not rejected_questions:
            return resume
        normalized_questions = " ".join(rejected_questions).casefold()
        remaining = []
        remaining_internships = []
        matched = False
        for project in resume.projects:
            raw_name = str(project.name or "").strip()
            display_name = raw_name.removeprefix("[匿名 Profile 项目]").strip()
            if display_name and display_name.casefold() in normalized_questions:
                matched = True
                continue
            remaining.append(project)
        for experience in resume.internships:
            name = str(experience.company or experience.role or "").strip()
            if name and name.casefold() in normalized_questions:
                matched = True
                continue
            remaining_internships.append(experience)
        if not matched:
            remaining = [
                project
                for project in remaining
                if not str(project.name or "").strip().startswith("[匿名 Profile 项目]")
            ]
        return resume.model_copy(
            update={"projects": remaining, "internships": remaining_internships}
        )

    @staticmethod
    def _explicit_unknown(answer: str) -> bool:
        normalized = " ".join(str(answer or "").casefold().split()).strip()
        return bool(
            re.fullmatch(
                r"(?:这个|这题|这个问题)?\s*(?:我)?\s*(?:确实|真的|目前|暂时)?\s*"
                r"(?:不知道|不会|不清楚|不太清楚|没学过|不了解)"
                r"(?:\s*[，,。.!！?？]?\s*(?:请)?(?:换(?:一|个|一道|下一道)?题|"
                r"(?:继续|进入)(?:下|下一)?(?:个|道)?题)(?:吧)?)?[。.!！?？]?",
                normalized,
            )
            or re.fullmatch(
                r"(?:i(?:'|’)m\s+not\s+sure|i\s+don(?:'|’)t\s+know|i\s+dont\s+know|"
                r"no\s+idea|skip)(?:[,.;!]?\s*(?:please\s+)?(?:move\s+on|next\s+question))?[.!?]?",
                normalized,
            )
            is not None
        )

    @staticmethod
    def _answer_time_allowance(seconds: int, input_mode: str) -> int:
        if input_mode == "voice":
            return seconds
        return min(600, max(seconds + 20, round(seconds * 1.5)))

    @staticmethod
    def _sanitize_question(
        question: str, company: str, *, language_mode: str = "bilingual"
    ) -> str:
        question = re.sub(r"```.*?```", "", question, flags=re.S)
        if is_internal_interview_instruction(question):
            question = (
                "In the project you just described, walk me through one real request "
                "across the part you personally implemented."
                if language_mode == "en"
                else "结合你刚才介绍的项目，请沿着一次真实请求说明你本人负责部分的完整处理链路。"
            )
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
        if "一分钟" in question or any(
            marker in lowered
            for marker in ("one minute", "1 minute", "brief introduction", "academic background")
        ) or "自我介绍" in question:
            return 60
        if any(marker in lowered for marker in ("手撕", "算法", "lru", "复杂度", "实现", "algorithm", "complexity", "design an")):
            return 180
        if any(marker in lowered for marker in ("项目", "链路", "故障", "取舍", "指标口径", "project", "request", "failure", "trade-off", "metric")):
            return 90
        if any(marker in lowered for marker in ("前沿", "怎么看", "研究", "论文", "趋势", "research", "paper", "trend")):
            return 120
        return 60
