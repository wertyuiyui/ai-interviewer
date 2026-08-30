from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import get_settings
from app.db import Database
from app.interview_engine import InterviewEngine
from app.schemas import InterviewCreate, Project, ResumeData


@pytest.mark.parametrize(
    "answer",
    (
        "我没做过这个项目",
        "这个项目不是我简历里的项目啊",
        "这个项目不是我参与的",
        "I didn't work on this project.",
        "This project isn't on my resume.",
    ),
)
def test_project_ownership_correction_is_distinct_from_wrong_resume(answer: str) -> None:
    assert InterviewEngine._explicit_project_ownership_correction(answer) is True
    assert InterviewEngine._explicit_resume_mismatch(answer) is False


@pytest.mark.asyncio
async def test_project_ownership_correction_reopens_resume_grounded_experience(
    tmp_path,
) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "project-ownership-correction.db",
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="project-ownership-correction-client",
            company="bytedance",
            interview_type="technical",
            language_mode="zh",
            stress_level=2,
            duration_minutes=10,
            resume=ResumeData(
                项目=[
                    Project(
                        name="[匿名 Profile 项目] ai-interviewer",
                        role="配置与运行层；核心实现",
                        technologies=["Docker"],
                    ),
                    Project(
                        name="校园订单系统",
                        role="订单接口与缓存",
                        technologies=["Redis", "MySQL"],
                    ),
                ],
                技能=["Redis", "MySQL"],
            ),
        )
    )
    await database.start_interview(created["id"])

    selected_profile = await engine.answer(
        created["id"], "我是计算机专业学生，主要学习后端开发。"
    )
    assert "ai-interviewer" in selected_profile.question
    assert "需要你先确认" in selected_profile.question
    assert "你填写的负责范围" not in selected_profile.question

    corrected = await engine.answer(created["id"], "我没做过这个项目")

    assert corrected.ended is False
    assert corrected.pressure_action == "none"
    assert corrected.resume_selection_warning is False
    assert corrected.resume_consistency == "uncertain"
    assert "不作为你的经历继续追问" in corrected.question
    assert "校园订单系统" in corrected.question
    assert "ai-interviewer" not in corrected.question
    assert "没做过" not in corrected.question
    correction_turn = (await database.list_turns(created["id"]))[-1]
    assert correction_turn.topic == "项目归属澄清"
    assert correction_turn.scorable is False
    assert correction_turn.score is None
    assert correction_turn.failed is False
    assert correction_turn.anchor_keyword == ""
    assert correction_turn.deductions == []

    continued = await engine.answer(
        created["id"], "校园订单系统是我参与的，我负责 Redis 缓存和订单接口。"
    )
    assert "ai-interviewer" not in continued.question
    assert "Redis" in continued.question
    assert "本人完成" in continued.question
