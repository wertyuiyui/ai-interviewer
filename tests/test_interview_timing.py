from __future__ import annotations

from dataclasses import replace

import pytest

import app.db as db_module
from app.config import get_settings
from app.db import Database
from app.errors import AppError
from app.interview_engine import InterviewEngine
from app.schemas import InterviewCreate, ResumeData
from app.voice_session import BrowserVoiceSession


@pytest.mark.asyncio
async def test_question_clock_includes_thinking_and_only_explicit_pause_freezes_it(
    tmp_path, monkeypatch
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(db_module.time, "time", lambda: clock[0])
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "interview-timing.db",
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="timing-client-001",
            company="bytedance",
            duration_minutes=15,
            resume=ResumeData(),
        )
    )

    started = await database.start_interview(created["id"])
    assert started is not None
    assert started["remaining_seconds"] == 900
    assert started["elapsed_seconds"] == pytest.approx(0)
    clock[0] += 12
    running = await database.get_interview(created["id"])
    assert running is not None
    assert running["remaining_seconds"] == 888
    assert running["elapsed_seconds"] == pytest.approx(12)
    assert running["question_elapsed_seconds"] == pytest.approx(12)

    paused = await database.set_interview_paused(created["id"], True)
    assert paused is not None and paused["paused"] is True
    original_deadline = paused["deadline_at"]
    clock[0] += 50
    frozen = await database.get_interview(created["id"])
    assert frozen is not None
    assert frozen["remaining_seconds"] == 888
    assert frozen["elapsed_seconds"] == pytest.approx(12)
    assert frozen["question_elapsed_seconds"] == pytest.approx(12)
    duplicate = await database.set_interview_paused(created["id"], True)
    assert duplicate is not None and duplicate["deadline_at"] == original_deadline
    with pytest.raises(AppError) as blocked:
        await engine.answer(created["id"], "暂停时不应接受回答。")
    assert blocked.value.code == "INTERVIEW_PAUSED"

    resumed = await database.set_interview_paused(created["id"], False)
    assert resumed is not None and resumed["paused"] is False
    assert resumed["deadline_at"] == pytest.approx(original_deadline + 50)
    assert resumed["paused_total_seconds"] == pytest.approx(50)
    assert resumed["elapsed_seconds"] == pytest.approx(12)
    clock[0] += 4
    result = await engine.answer(
        created["id"],
        "我负责订单链路和事务边界。",
        input_mode="voice",
        answer_duration_seconds=2,
    )
    assert result.turn.answer_duration_seconds == pytest.approx(16)
    assert result.turn.speech_rate_cpm == pytest.approx(
        len("我负责订单链路和事务边界。") * 30
    )

    assert await database.finish_interview(created["id"], "manual")
    clock[0] += 30
    ended = await database.get_interview(created["id"])
    assert ended is not None
    assert ended["elapsed_seconds"] == pytest.approx(16)

    unlimited = await engine.create(
        InterviewCreate(
            client_id="timing-client-unlimited",
            company="meituan",
            duration_minutes=None,
            resume=ResumeData(),
        )
    )
    await database.start_interview(unlimited["id"])
    clock[0] += 7
    unlimited_running = await database.get_interview(unlimited["id"])
    assert unlimited_running is not None
    assert unlimited_running["elapsed_seconds"] == pytest.approx(7)
    await database.set_interview_paused(unlimited["id"], True)
    clock[0] += 30
    unlimited_paused = await database.get_interview(unlimited["id"])
    assert unlimited_paused is not None
    assert unlimited_paused["elapsed_seconds"] == pytest.approx(7)
    await database.set_interview_paused(unlimited["id"], False)
    clock[0] += 3
    unlimited_running = await database.get_interview(unlimited["id"])
    assert unlimited_running is not None
    assert unlimited_running["remaining_seconds"] is None
    assert unlimited_running["elapsed_seconds"] == pytest.approx(10)


def test_voice_capture_clock_excludes_explicit_pause() -> None:
    session = object.__new__(BrowserVoiceSession)
    session.answer_capture_active = True
    session._answer_boundary_started_at = 10.0
    session._candidate_speaking = True
    session._speech_started_at = 12.0

    session.shift_answer_clock(7.5)

    assert session._answer_boundary_started_at == pytest.approx(17.5)
    assert session._speech_started_at == pytest.approx(19.5)
