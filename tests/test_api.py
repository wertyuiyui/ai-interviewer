from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import app.main as main_module
from app.config import get_settings
from app.db import Database
from app.interview_engine import InterviewEngine
from app.report_engine import ReportEngine
from app.resume import ResumeParser
from app.schemas import InterviewCreate, Project, ResumeData


@pytest.mark.asyncio
async def test_l3_rest_flow_resume_interview_report_and_history(
    tmp_path, monkeypatch
) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "api.db",
        daily_interview_limit=20,
        client_daily_interview_limit=5,
    )
    database = Database(settings)
    await database.initialize()
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "resume_parser", ResumeParser(settings))
    monkeypatch.setattr(
        main_module, "interview_engine", InterviewEngine(database, settings)
    )
    monkeypatch.setattr(
        main_module, "report_engine", ReportEngine(database, settings)
    )
    monkeypatch.setattr(main_module, "resume_limiter", main_module.SlidingWindowLimiter())

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        config = await client.get("/api/config")
        assert config.status_code == 200
        config_payload = config.json()
        assert config_payload["voice_mode"] == "L3"
        assert "AI 工程后端 / LLM Infra" in config_payload["specializations"]
        assert config_payload["custom_duration"] == {
            "min": 1,
            "max": 180,
            "unlimited": True,
        }
        assert [item["level"] for item in config_payload["stress_levels"]] == [
            0,
            1,
            2,
            3,
        ]
        assert "references" not in config_payload
        assert config.headers["cache-control"] == "no-store"

        catalog = await client.get("/api/resources/catalog")
        assert catalog.status_code == 200
        assert catalog.headers["cache-control"] == "no-store"
        catalog_payload = catalog.json()
        assert len(catalog_payload["sources"]) >= 15
        assert "不绕过登录" in catalog_payload["collection_policy"]["social_media"]

        home_source = (
            Path(main_module.__file__).resolve().parents[1] / "public" / "index.html"
        ).read_text(encoding="utf-8")
        assert "/sample-resumes/" not in home_source
        private_resume = await client.get(
            "/sample-resumes/01_java_ecommerce_backend.pdf"
        )
        assert private_resume.status_code == 404

        parsed = await client.post(
            "/api/resumes/parse",
            data={
                "text": (
                    "某大学计算机本科。校园秒杀项目使用 Java、Redis、MySQL，"
                    "本人负责库存链路，QPS 从 800 提升到 3000。"
                )
            },
        )
        assert parsed.status_code == 200
        resume = parsed.json()["resume"]
        assert set(resume) == {"教育", "实习经历", "项目", "技能"}

        unlimited = await client.post(
            "/api/interviews",
            json={
                "client_id": "api-unlimited-001",
                "resume": resume,
                "company": "meituan",
                "role": "backend",
                "specialization": "Go 微服务",
                "stress_level": 1,
                "duration_minutes": None,
            },
        )
        assert unlimited.status_code == 201
        unlimited_payload = unlimited.json()
        assert unlimited_payload["specialization"] == "Go 微服务"
        assert unlimited_payload["stress"] is True
        assert unlimited_payload["stress_level"] == 1
        assert unlimited_payload["duration_minutes"] is None
        await database.start_interview(unlimited_payload["id"])
        unlimited_state = await client.get(
            f"/api/interviews/{unlimited_payload['id']}"
        )
        assert unlimited_state.status_code == 200
        assert unlimited_state.json()["deadline_at"] is None
        assert unlimited_state.json()["remaining_seconds"] is None
        await database.finish_interview(unlimited_payload["id"], "manual")

        invalid = await client.post(
            "/api/interviews",
            json={
                "client_id": "含非法字符的设备号",
                "resume": resume,
                "company": "bytedance",
                "role": "backend",
                "stress": True,
                "duration_minutes": 15,
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"

        created = await client.post(
            "/api/interviews",
            json={
                "client_id": "api-browser-001",
                "resume": resume,
                "company": "bytedance",
                "role": "backend",
                "stress": True,
                "duration_minutes": 15,
            },
        )
        assert created.status_code == 201
        interview_id = created.json()["id"]
        await database.start_interview(interview_id)
        result = await main_module.interview_engine.answer(
            interview_id,
            "我负责 Redis Lua 库存预扣，入口经过网关，异步写入 MySQL，峰值 QPS 3000。",
        )
        assert result.turn.drill_depth == 0
        assert "业务" in result.question

        finished = await client.post(
            f"/api/interviews/{interview_id}/finish", json={"reason": "time"}
        )
        assert finished.status_code == 202
        state = await client.get(f"/api/interviews/{interview_id}")
        assert state.json()["end_reason"] == "time"
        report = await client.get(f"/api/interviews/{interview_id}/report")
        assert report.status_code == 200
        payload = report.json()
        assert payload["question_feedback"][0]["deductions"]
        assert payload["question_feedback"][0]["better_answer"]
        assert payload["rubric"]["project_depth"]["weight"] == 0.4

        history = await client.get(
            "/api/history", params={"client_id": "api-browser-001"}
        )
        assert history.status_code == 200
        assert history.json()["items"][0]["interview_id"] == interview_id


@pytest.mark.asyncio
async def test_cancelled_accepted_text_answer_is_preserved_without_fake_score(
    tmp_path, monkeypatch
) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "cancelled-answer.db",
    )
    database = Database(settings)
    await database.initialize()

    class SlowEngine(InterviewEngine):
        def __init__(self) -> None:
            super().__init__(database, settings)
            self.started = asyncio.Event()

        async def answer(self, *_args, **_kwargs):
            self.started.set()
            await asyncio.Event().wait()

    engine = SlowEngine()
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "interview_engine", engine)
    created = await engine.create(
        InterviewCreate(
            client_id="cancel-answer-client",
            company="bytedance",
            resume=ResumeData(
                项目=[Project(name="订单服务", technologies=["Java", "MySQL"])]
            ),
        )
    )
    await database.start_interview(created["id"])
    events: list[dict] = []

    async def send(event_type: str, **payload) -> None:
        events.append({"type": event_type, **payload})

    async def on_end(_reason: str) -> None:
        return None

    task = asyncio.create_task(
        main_module._handle_text_answer(
            interview_id=created["id"],
            answer="我会先说明订单链路和事务边界。",
            send=send,
            stop_event=asyncio.Event(),
            on_end=on_end,
        )
    )
    await asyncio.wait_for(engine.started.wait(), timeout=1)
    assert any(event["type"] == "candidate.transcript.done" for event in events)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    turns = await database.list_turns(created["id"])
    assert len(turns) == 1
    assert turns[0].answer == "我会先说明订单链路和事务边界。"
    assert turns[0].score is None
    assert turns[0].scorable is False
    assert turns[0].score_source == "unavailable"
