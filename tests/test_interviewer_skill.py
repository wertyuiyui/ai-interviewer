from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.config import get_settings
from app.content import COMPANIES, load_interview_skill, load_interviewer_core_skill
from app.db import Database
from app.interview_engine import InterviewEngine
from app.prompt_engine import (
    build_system_prompt,
    is_obvious_placeholder_answer,
    is_vague_answer,
    project_followup,
)
from app.schemas import Experience, InterviewCreate, Project, ResumeData


def test_core_interviewer_skill_has_auditable_runtime_contract() -> None:
    core = load_interviewer_core_skill()

    assert core["schema_version"] == "1.0"
    assert core["scope"] == "backend-intern-first-round-practice"
    assert core["role_contract"]["one_primary_intent_per_turn"] is True
    assert "reviewed wording exactly" in core["question_policy"]["reviewed_bank"]
    assert "unscorable/not_observed" in core["assessment_policy"]["not_observed"]
    assert core["termination_policy"]["server_authority"] is True
    assert "accent" in core["fairness_policy"]["do_not_infer_from"]
    assert "same question" in core["modality_policy"]["semantic_parity"]
    assert "do not reuse any phrase from the denial" in core["adaptive_policy"][
        "experience_ownership_correction"
    ]
    assert "different dimension" in core["adaptive_policy"]["explicit_unknown"]
    assert "Compensation follow-ups" in core["question_policy"][
        "behavioral_followup_by_stage"
    ]


def test_polite_unknown_and_compensation_followup_are_stage_aware() -> None:
    assert InterviewEngine._explicit_unknown(
        "这道题涉及的具体机制我目前不能准确回答，我不想凭印象猜测。"
        "可以先记录为我的知识缺口，我们换下一道。"
    )
    assert InterviewEngine._explicit_unknown(
        "这个知识点我目前没有形成可靠答案，请先记为知识缺口并换下一题。"
    )
    assert InterviewEngine._explicit_unknown("这部分我没有做过验证，所以不能继续回答。")
    assert InterviewEngine._explicit_unknown(
        "这个推理优化点我只做过概念调研，不能把猜测当结论。"
        "请先记为知识缺口并换下一题。"
    )
    question = InterviewEngine._anchored_bank_followup(
        "哪段经历最能说明你的薪酬选择？",
        answer="我更关注岗位内容，也尊重公司的标准范围。",
        anchor="标准范围",
        bank_item={"question": "你的实习薪酬期望是什么？", "topic": "薪酬沟通"},
        stage="compensation",
        track="hr",
        vague=False,
        language_mode="zh",
    )
    assert "如何排序" in question
    assert "哪些条件可以协商" in question
    assert "哪段具体经历" not in question


def test_project_stage_prefers_a_resume_project_before_an_internship() -> None:
    question, _ = project_followup(
        1,
        "",
        ResumeData(
            项目=[Project(name="校园二手交易平台", role="核心开发")],
            实习经历=[Experience(company="示例科技", role="后端实习生")],
        ),
        language_mode="zh",
    )
    assert "校园二手交易平台" in question
    assert "示例科技" not in question
def test_obvious_placeholder_answer_requires_clarification() -> None:
    assert is_obvious_placeholder_answer("我叫xxxxx") is True
    assert is_obvious_placeholder_answer("My name is <name>") is True
    assert is_obvious_placeholder_answer("我不会") is False
    assert is_vague_answer("我叫xxxxx") is True
    assert is_vague_answer("我叫王伟，目前大三，想做 Java 后端开发。") is False


@pytest.mark.asyncio
async def test_incomplete_self_intro_stays_until_answered_or_skipped(tmp_path) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "placeholder-intro.db",
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="placeholder-intro-client",
            company="bytedance",
            language_mode="zh",
            resume=ResumeData(项目=[Project(name="校园二手交易平台后端")]),
        )
    )
    await database.start_interview(created["id"])

    placeholder = await engine.answer(created["id"], "我叫xxxxx")
    greeting_again = await engine.answer(created["id"], "你好")

    assert placeholder.stage["current"]["id"] == "self_intro"
    assert "学习进度" in placeholder.question
    assert greeting_again.stage["current"]["id"] == "self_intro"
    assert "校园二手交易平台" not in greeting_again.question
    row = await database.get_interview(created["id"])
    assert row is not None
    assert row["stage_state"]["turn_count"] == 0

    answered = await engine.answer(
        created["id"], "我目前大三，主要学习 Java 后端，希望继续做服务端开发。"
    )

    assert answered.stage["current"]["id"] == "self_intro"
    assert "校园二手交易平台" in answered.question

    completed = await engine.answer(
        created["id"], "这个项目服务校内二手交易，我负责商品发布和 Redis 缓存。"
    )

    assert completed.stage["current"]["id"] == "project_deep_dive"
    assert "校园二手交易平台" in completed.question


def test_every_company_skill_compiles_with_the_same_core_contract() -> None:
    core = load_interviewer_core_skill()

    for company in COMPANIES:
        skill = load_interview_skill(company)
        assert skill["company"] == company
        assert skill["interviewer_core"] == core
        assert skill["interviewer_core"]["name"] == "core-interviewer"


def test_compiled_core_skill_is_injected_without_provenance() -> None:
    prompt = build_system_prompt(
        company="bytedance",
        resume=ResumeData(技能=["Java", "MySQL"]),
        duration_minutes=10,
        weak_topics=[],
        language_mode="zh",
        interview_type="technical",
        stress_level=1,
    )

    assert "【核心面试官契约】" in prompt
    assert '"name": "core-interviewer"' in prompt
    assert "unscorable/not_observed" in prompt
    assert "该契约高于公司风格" in prompt
    company_section = prompt.split("【公司针对性 interview skill】", 1)[1]
    assert '"interviewer_core"' not in company_section
    assert "experience-bytedance" not in prompt
    assert '"source_refs"' not in prompt


@pytest.mark.asyncio
async def test_each_decision_receives_authoritative_interview_context(tmp_path) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        async def chat_json(self, messages, **_kwargs):
            self.payloads.append(json.loads(messages[1]["content"]))
            return {
                "next_question": "你在这个项目里具体负责哪部分？",
                "assessment": {
                    "score": 7.0,
                    "scorable": True,
                    "score_source": "llm",
                    "failed": False,
                    "dimension": "communication",
                    "topic": "自我介绍",
                    "deductions": [],
                },
                "pressure_action": "none",
                "drill_dimension": "业务背景",
                "drill_depth": 1,
                "anchor_keyword": "Java",
                "should_end": False,
            }

    settings = replace(
        get_settings(), mock_llm=False, db_path=tmp_path / "context-harness.db"
    )
    database = Database(settings)
    await database.initialize()
    client = RecordingClient()
    engine = InterviewEngine(database, settings, client=client)
    created = await engine.create(
        InterviewCreate(
            client_id="context-harness-client",
            company="bytedance",
            language_mode="zh",
            resume=ResumeData(项目=[Project(name="校园二手交易平台后端")]),
        )
    )
    await database.start_interview(created["id"])

    await engine.answer(
        created["id"], "我目前大三，主修软件工程，想继续做 Java 后端；做过校园二手交易平台后端，负责商品发布链路。"
    )
    await engine.answer(created["id"], "我负责 Redis 缓存和商品发布链路。")

    context = client.payloads[1]["interview_context"]
    assert context["current_stage"] == {
        "id": "project_deep_dive",
        "label": "项目深挖",
        "topic_turn": 0,
    }
    assert context["completed_stages"] == ["self_intro"]
    assert context["confirmed_facts"][-1]["anchor"] == "Java"
    assert context["continuity"]["stage_transition_authority"] == "server"
    assert context["continuity"]["resolve_current_topic_before_transition"] is True
    assert "candidate's latest answer" in context["continuity"]["delivery"]
    assert "checklist of subquestions" in context["continuity"]["delivery"]
    assert client.payloads[1]["recent_transcript"][-1]["topic"].startswith(
        "自我介绍"
    )


@pytest.mark.asyncio
async def test_core_contract_matches_real_anchor_and_termination_behavior(tmp_path) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "interviewer-skill.db",
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="core-skill-forward-test",
            company="bytedance",
            interview_type="technical",
            language_mode="zh",
            stress_level=2,
            duration_minutes=10,
            resume=ResumeData(
                项目=[
                    Project(
                        name="秒杀服务",
                        role="后端开发",
                        technologies=["Redis", "MySQL"],
                        highlights=["使用 Redis 缓存库存"],
                    )
                ],
                技能=["Redis", "MySQL"],
            ),
        )
    )
    await database.start_interview(created["id"])

    first = await engine.answer(
        created["id"], "我负责 Redis 缓存和库存模块，性能提升了很多。"
    )
    assert first.ended is False
    assert all(term not in first.question for term in ("参考答案", "得分", "扣分", "policy"))

    unknown = await engine.answer(created["id"], "不知道")
    assert unknown.ended is False
    assert unknown.breakdown_streak == 0
    turns = await database.list_turns(created["id"])
    assert turns[-1].answer == "不知道"
    assert turns[-1].anchor_keyword == ""
    assert unknown.stage["current"]["id"] == "fundamentals"
    assert all(term not in unknown.question for term in ("参考答案", "得分", "扣分", "policy"))

    failed_again = await engine.answer(created["id"], "不会")
    assert failed_again.ended is False
    assert failed_again.breakdown_streak == 0
    assert failed_again.pressure_action == "none"
    assert failed_again.question.startswith("明白，我们换一道。")
    assert "缺少依据" not in failed_again.question
    assert "建议" not in failed_again.question
