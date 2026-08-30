from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import httpx
import pytest

import app.main as main_module
from app.config import get_settings
from app.db import Database
from app.errors import AppError, LLMError
from app.interview_engine import InterviewEngine
from app.practice import (
    PracticeAnswerCreate,
    PracticeHintCreate,
    PracticeService,
    PracticeSessionCreate,
    load_real_question_bank,
)
from app.schemas import InterviewCreate, InterviewTurn, ResumeData


def write_bank(path) -> None:
    path.mkdir()
    (path / "real.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "questions": [
                    {
                        "id": "real-zh-1",
                        "company": "bytedance",
                        "language": "zh",
                        "category": "Redis",
                        "topic": "缓存一致性",
                        "question": "缓存和数据库不一致时，你会如何定位并选择修复方案？",
                        "followups": ["先区分读路径和写路径会有什么帮助？"],
                        "difficulty": "hard",
                        "source_ids": ["licensed-bank-redis"],
                    },
                    {
                        "id": "real-en-1",
                        "language": "en",
                        "category": "System Design",
                        "topic": "backpressure",
                        "question": "How would you propagate backpressure through an API service?",
                        "difficulty": "medium",
                        "source_url": "https://example.com/licensed-bank",
                    },
                    {
                        "id": "untraceable-1",
                        "language": "zh",
                        "category": "MySQL",
                        "topic": "索引",
                        "question": "这道题没有来源，不应进入快刷题库。",
                        "difficulty": "easy",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_real_bank_quick_drill_hint_and_repeated_voice_answer(
    tmp_path,
) -> None:
    question_dir = tmp_path / "questions"
    write_bank(question_dir)
    bank = load_real_question_bank(question_dir)
    assert [item.id for item in bank] == ["real-zh-1", "real-en-1"]
    assert all(item.provenance for item in bank)

    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "practice.db",
    )
    database = Database(settings)
    await database.initialize()
    service = PracticeService(database, settings, question_dir=question_dir)
    await service.initialize()

    created = await service.create_session(
        PracticeSessionCreate(
            client_id="practice-client-001",
            mode="quick",
            company="bytedance",
            language_mode="zh",
            count=5,
        )
    )
    assert created["total_questions"] == 1
    question = created["current_question"]
    assert question["id"] == "real-zh-1"
    assert "source_ids" not in question
    assert "provenance" not in question
    assert "followups" not in question

    hint = await service.hint(
        created["id"],
        PracticeHintCreate(
            client_id="practice-client-001",
            question_id=question["id"],
        ),
    )
    assert "读路径" in hint["hint"]
    first = await service.submit_answer(
        created["id"],
        PracticeAnswerCreate(
            client_id="practice-client-001",
            question_id=question["id"],
            answer=(
                "我会先区分读写链路，核对缓存命中、数据库提交与删除缓存的时序，"
                "再通过 trace 和版本号定位窗口，按一致性要求选择延迟双删或消息补偿。"
            ),
            input_mode="text",
            answer_duration_seconds=48,
        ),
    )
    assert first["done"] is True
    assert first["assessment"]["status"] == "scored"
    assert first["hint_used"] is True

    repeated = await service.submit_answer(
        created["id"],
        PracticeAnswerCreate(
            client_id="practice-client-001",
            question_id=question["id"],
            answer=(
                "先用请求链路和版本号定位不一致窗口，再按业务容忍度选择旁路缓存失效、"
                "可靠消息补偿和定期对账，并用不一致率与修复延迟验证。"
            ),
            input_mode="voice",
            answer_duration_seconds=39,
            reattempt=True,
        ),
    )
    assert repeated["reattempt"] is True
    assert repeated["input_mode"] == "voice"

    state = await service.get_session(created["id"], "practice-client-001")
    assert state["answered_questions"] == 1
    assert state["attempt_count"] == 2
    assert state["hint_count"] == 1
    assert state["best_score"] is not None
    assert state["latest_score"] == repeated["assessment"]["score"]
    assert all("provenance" not in item["question"] for item in state["attempts"])

    with sqlite3.connect(settings.db_path) as connection:
        raw = connection.execute(
            "SELECT question_snapshot_json FROM practice_attempts LIMIT 1"
        ).fetchone()[0]
    persisted = json.loads(raw)
    assert persisted["provenance"]["source_ids"] == ["licensed-bank-redis"]


@pytest.mark.asyncio
async def test_quick_drill_english_filter_and_empty_bank_are_explicit(tmp_path) -> None:
    question_dir = tmp_path / "questions"
    write_bank(question_dir)
    settings = replace(
        get_settings(),
        mock_llm=True,
        db_path=tmp_path / "english.db",
    )
    database = Database(settings)
    await database.initialize()
    service = PracticeService(database, settings, question_dir=question_dir)
    await service.initialize()

    english = await service.create_session(
        PracticeSessionCreate(
            client_id="practice-english-001",
            language_mode="en",
            count=10,
        )
    )
    assert english["current_question"]["id"] == "real-en-1"
    assert english["current_question"]["language"] == "en"

    with pytest.raises(AppError) as caught:
        await service.create_session(
            PracticeSessionCreate(
                client_id="practice-empty-001",
                language_mode="zh",
                topic="不存在的主题",
            )
        )
    assert caught.value.code == "PRACTICE_QUESTIONS_EMPTY"
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_review_reuses_exact_interview_turn_and_checks_owner(tmp_path) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "review.db",
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    interview = await engine.create(
        InterviewCreate(
            client_id="review-owner-001",
            company="bytedance",
            language_mode="en",
            resume=ResumeData(),
        )
    )
    original_question = "请解释一次 Redis 缓存击穿的完整请求链路。"
    await database.append_turn(
        interview["id"],
        InterviewTurn(
            ordinal=1,
            question=original_question,
            answer="不知道。",
            category="fundamentals",
            topic="Redis",
            score=2.5,
            scorable=True,
            score_source="mock",
            deductions=["没有说明机制"],
            failed=True,
        ),
        "下一题",
    )
    await database.finish_interview(interview["id"], "manual")

    service = PracticeService(database, settings)
    await service.initialize()
    review = await service.create_session(
        PracticeSessionCreate(
            client_id="review-owner-001",
            mode="review",
            source_interview_id=interview["id"],
            review_ordinals=[1],
            count=1,
        )
    )
    assert review["current_question"]["question"] == original_question
    assert review["language_mode"] == "en"
    assert review["current_question"]["previous_score"] == 2.5
    assert review["current_question"]["previous_deductions"] == ["没有说明机制"]

    with pytest.raises(AppError) as caught:
        await service.get_session(review["id"], "different-owner-001")
    assert caught.value.code == "PRACTICE_FORBIDDEN"
    assert await service.owns_session(review["id"], "different-owner-001") is False
    assert await service.owns_session(review["id"], "review-owner-001") is True


class FailingClient:
    async def chat_json(self, *args, **kwargs):
        raise LLMError("synthetic outage")


@pytest.mark.asyncio
async def test_scoring_outage_is_unscored_never_default_five(tmp_path) -> None:
    question_dir = tmp_path / "questions"
    write_bank(question_dir)
    settings = replace(
        get_settings(),
        mock_llm=False,
        db_path=tmp_path / "unscored.db",
    )
    database = Database(settings)
    await database.initialize()
    service = PracticeService(
        database,
        settings,
        client=FailingClient(),  # type: ignore[arg-type]
        question_dir=question_dir,
    )
    await service.initialize()
    created = await service.create_session(
        PracticeSessionCreate(
            client_id="practice-unscored-001",
            company="bytedance",
            language_mode="zh",
            count=1,
        )
    )
    result = await service.submit_answer(
        created["id"],
        PracticeAnswerCreate(
            client_id="practice-unscored-001",
            question_id=created["current_question"]["id"],
            answer="这是一段已经被保存但评分服务暂时不可用的有效回答。",
        ),
    )
    assessment = result["assessment"]
    assert assessment["score"] is None
    assert assessment["scorable"] is False
    assert assessment["status"] == "unscored"
    assert assessment["evidence"] == []
    assert assessment["key_points"] == []


@pytest.mark.asyncio
async def test_practice_rest_api_contract(tmp_path, monkeypatch) -> None:
    question_dir = tmp_path / "questions"
    write_bank(question_dir)
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "practice-api.db",
    )
    database = Database(settings)
    await database.initialize()
    service = PracticeService(database, settings, question_dir=question_dir)
    await service.initialize()
    monkeypatch.setattr(main_module, "practice_service", service)

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        catalog = await client.get("/api/practice/catalog")
        assert catalog.status_code == 200
        assert catalog.json()["question_count"] == 2

        created = await client.post(
            "/api/practice/sessions",
            json={
                "client_id": "practice-api-client",
                "mode": "quick",
                "company": "bytedance",
                "language_mode": "zh",
                "count": 1,
            },
        )
        assert created.status_code == 201
        payload = created.json()
        question = payload["current_question"]
        assert set(question).isdisjoint({"source_ids", "source_url", "provenance"})

        hint = await client.post(
            f"/api/practice/sessions/{payload['id']}/hint",
            json={
                "client_id": "practice-api-client",
                "question_id": question["id"],
            },
        )
        assert hint.status_code == 200

        answer = await client.post(
            f"/api/practice/sessions/{payload['id']}/answers",
            json={
                "client_id": "practice-api-client",
                "question_id": question["id"],
                "answer": "我先定位读写链路，再分析一致性窗口、补偿机制与监控指标。",
                "input_mode": "voice",
                "answer_duration_seconds": 31.5,
            },
        )
        assert answer.status_code == 200
        assert answer.json()["input_mode"] == "voice"
        assert answer.json()["hint_used"] is True

        state = await client.get(
            f"/api/practice/sessions/{payload['id']}",
            params={"client_id": "practice-api-client"},
        )
        assert state.status_code == 200
        assert state.json()["status"] == "completed"
        history = await client.get(
            "/api/practice/history",
            params={"client_id": "practice-api-client"},
        )
        assert history.status_code == 200
        assert history.json()["items"][0]["id"] == payload["id"]


def test_practice_voice_route_requires_session_ownership() -> None:
    source = Path(__file__).resolve().parents[1] / "app" / "main.py"
    text = source.read_text(encoding="utf-8")
    assert '@app.websocket("/ws/practice/sessions/{session_id}")' in text
    assert "practice_service.owns_session" in text
    assert 'event.get("type") != "client.ready"' in text
    assert "PRACTICE_ANSWER_RATE_LIMIT" in text
