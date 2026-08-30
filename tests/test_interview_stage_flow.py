from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import get_settings
from app.db import Database
from app.interview_engine import STAGE_LABELS, InterviewEngine, interview_stage_plan
from app.schemas import InterviewCreate, Project, ResumeData


def test_stage_plans_follow_interview_type_boundaries() -> None:
    assert "behavioral" not in STAGE_LABELS
    assert interview_stage_plan("technical") == [
        "self_intro", "project_deep_dive", "fundamentals", "coding", "candidate_questions"
    ]
    assert interview_stage_plan("technical_hr") == [
        "self_intro", "project_deep_dive", "fundamentals", "coding",
        "hr_fit", "career_planning", "compensation", "candidate_questions"
    ]
    assert interview_stage_plan("hr") == [
        "self_intro", "hr_fit", "career_planning", "compensation", "candidate_questions"
    ]


@pytest.mark.asyncio
async def test_unknown_switches_projects_before_closing_stage(tmp_path) -> None:
    settings = replace(get_settings(), mock_llm=True, db_path=tmp_path / "unknown-stage.db")
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="unknown-stage-client-001",
            company="bytedance",
            interview_type="technical",
            language_mode="zh",
            resume=ResumeData(
                项目=[
                    Project(name="订单系统", role="后端开发", technologies=["Redis"]),
                    Project(name="库存系统", role="接口开发", technologies=["MySQL"]),
                ]
            ),
        )
    )
    await database.start_interview(created["id"])
    opened = await engine.answer(
        created["id"], "我是计算机专业学生，主要学习后端开发；做过订单系统，负责后端开发。"
    )
    assert opened.stage["current"]["id"] == "project_deep_dive"
    assert "订单系统" in opened.question

    switched = await engine.answer(
        created["id"],
        "我不知道，请继续下一题",
        control_intent="unknown",
    )

    assert switched.stage["current"]["id"] == "project_deep_dive"
    assert "库存系统" in switched.question
    assert "订单系统" not in switched.question
    assert "换一个项目" in switched.question
    assert (await database.list_turns(created["id"]))[-1].anchor_keyword == ""

    skipped = await engine.answer(
        created["id"], "我不知道，请继续下一题", control_intent="unknown"
    )

    assert skipped.stage["current"]["id"] == "fundamentals"
    assert "库存系统" not in skipped.question


@pytest.mark.asyncio
async def test_unknown_project_followup_uses_other_dimensions_then_stops(tmp_path) -> None:
    settings = replace(get_settings(), mock_llm=True, db_path=tmp_path / "unknown-depth.db")
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="unknown-depth-client-001",
            company="tencent",
            interview_type="technical",
            language_mode="zh",
            resume=ResumeData(
                项目=[Project(name="订单系统", role="后端开发", technologies=["Redis"])]
            ),
        )
    )
    await database.start_interview(created["id"])
    opened = await engine.answer(
        created["id"], "我是计算机专业学生，做过订单系统，负责后端开发。"
    )
    secondary = await engine.answer(
        created["id"], "订单系统处理下单请求，我负责接口和 Redis 缓存。"
    )

    tertiary = await engine.answer(
        created["id"], "我不知道，请继续下一题", control_intent="unknown"
    )
    third_angle = await engine.answer(
        created["id"], "我不知道，请继续下一题", control_intent="unknown"
    )

    assert tertiary.stage["current"]["id"] == "project_deep_dive"
    assert third_angle.stage["current"]["id"] == "project_deep_dive"
    assert "订单系统" in tertiary.question
    assert "Redis" in third_angle.question
    assert "换个角度" in tertiary.question
    assert len({secondary.question, tertiary.question, third_angle.question}) == 3

    finished = await engine.answer(
        created["id"], "我不知道，请继续下一题", control_intent="unknown"
    )

    assert opened.stage["current"]["id"] == "project_deep_dive"
    assert finished.stage["current"]["id"] == "fundamentals"
    assert "订单系统" not in finished.question


@pytest.mark.asyncio
async def test_manual_stage_advance_persists_without_fake_turn(tmp_path) -> None:
    settings = replace(get_settings(), mock_llm=True, db_path=tmp_path / "manual-stage.db")
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="manual-stage-client-001",
            company="tencent",
            interview_type="technical",
            language_mode="zh",
            resume=ResumeData(项目=[Project(name="订单系统", role="后端开发")]),
        )
    )
    await database.start_interview(created["id"])

    advanced = await engine.advance_stage(
        created["id"], expected_revision=created["stage"]["revision"]
    )

    assert advanced["stage"]["current"]["id"] == "project_deep_dive"
    assert advanced["stage"]["stages"][0]["status"] == "skipped"
    assert await database.list_turns(created["id"]) == []
    restored = await database.get_interview(created["id"])
    assert restored is not None
    assert engine.stage_snapshot(restored)["current"]["id"] == "project_deep_dive"
    with pytest.raises(Exception) as stale:
        await engine.advance_stage(created["id"], expected_revision=1)
    assert getattr(stale.value, "code", "") == "STALE_STAGE"


@pytest.mark.asyncio
async def test_unknown_in_fundamentals_skips_followup_not_whole_interview(tmp_path) -> None:
    settings = replace(get_settings(), mock_llm=True, db_path=tmp_path / "unknown-topic.db")
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="unknown-topic-client-001",
            company="tencent",
            interview_type="technical",
            language_mode="zh",
            resume=ResumeData(项目=[Project(name="订单系统", role="后端开发")]),
        )
    )
    await database.start_interview(created["id"])
    # Use the explicit control path to reach fundamentals without creating
    # placeholder answers for the skipped stages.
    await engine.advance_stage(created["id"], expected_revision=1)
    current = await database.get_interview(created["id"])
    assert current is not None
    advanced = await engine.advance_stage(
        created["id"], expected_revision=engine.stage_snapshot(current)["revision"]
    )
    first_question = advanced["question"]

    greeting = await engine.answer(created["id"], "你好")

    assert greeting.stage["current"]["id"] == "fundamentals"
    assert "当前问题" in greeting.question
    current = await database.get_interview(created["id"])
    assert current is not None
    assert current["stage_state"]["turn_count"] == 0

    skipped = await engine.answer(
        created["id"], "我不知道，请继续下一题", control_intent="unknown"
    )

    assert skipped.stage["current"]["id"] == "fundamentals"
    assert skipped.question != first_question
    assert "仍围绕" not in skipped.question


@pytest.mark.asyncio
async def test_unknown_in_hr_stage_moves_to_next_question(tmp_path) -> None:
    settings = replace(get_settings(), mock_llm=True, db_path=tmp_path / "unknown-hr.db")
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="unknown-hr-client-001",
            company="tencent",
            interview_type="hr",
            language_mode="zh",
            resume=ResumeData(),
        )
    )
    await database.start_interview(created["id"])
    fit = await engine.answer(created["id"], "我是计算机专业学生，希望提升工程能力。")

    skipped = await engine.answer(
        created["id"], "我不知道，请继续下一题", control_intent="unknown"
    )

    assert skipped.stage["current"]["id"] == "career_planning"
    assert skipped.question != fit.question


@pytest.mark.asyncio
async def test_hr_plan_never_emits_technical_or_coding_stage(tmp_path) -> None:
    settings = replace(get_settings(), mock_llm=True, db_path=tmp_path / "hr-stage.db")
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="hr-stage-client-001",
            company="bytedance",
            interview_type="hr",
            language_mode="zh",
            resume=ResumeData(),
        )
    )
    assert [item["id"] for item in created["stage"]["stages"]] == [
        "self_intro", "hr_fit", "career_planning", "compensation", "candidate_questions"
    ]
    assert all(
        item["id"] not in {"project_deep_dive", "fundamentals", "coding"}
        for item in created["stage"]["stages"]
    )

    await database.start_interview(created["id"])
    fit = await engine.answer(created["id"], "我希望在真实业务中提升协作和交付能力。")
    assert fit.stage["current"]["id"] == "hr_fit"
    followup = await engine.answer(created["id"], "我调研了岗位要求，并按差距补项目。")
    assert followup.stage["current"]["id"] == "hr_fit"
    career = await engine.answer(created["id"], "我会用项目交付和复盘记录验证匹配度。")
    assert career.stage["current"]["id"] == "career_planning"
