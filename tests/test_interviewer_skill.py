from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import get_settings
from app.content import COMPANIES, load_interview_skill, load_interviewer_core_skill
from app.db import Database
from app.interview_engine import InterviewEngine
from app.prompt_engine import build_system_prompt
from app.schemas import InterviewCreate, Project, ResumeData


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
    assert turns[-1].anchor_keyword == "秒杀服务"
    assert "不知道" not in turns[-1].anchor_keyword
    assert all(term not in unknown.question for term in ("参考答案", "得分", "扣分", "policy"))

    failed_again = await engine.answer(created["id"], "不会")
    assert failed_again.ended is False
    assert failed_again.breakdown_streak == 0
    assert "下一题" in failed_again.question
