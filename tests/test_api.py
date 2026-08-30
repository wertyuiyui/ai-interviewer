from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

import app.main as main_module
from app.config import get_settings
from app.db import Database
from app.interview_engine import InterviewEngine
from app.report_engine import ReportEngine
from app.resume import ResumeParser


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
        assert config.json()["voice_mode"] == "L3"
        assert config.headers["cache-control"] == "no-store"

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
