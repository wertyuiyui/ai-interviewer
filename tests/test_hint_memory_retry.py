from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import app.main as main_module
from app.config import get_settings
from app.content import load_current_research_question_bank
from app.db import Database
from app.interview_engine import InterviewEngine
from app.report_engine import ReportEngine
from app.schemas import ResumeData


def test_hint_memory_retry_controls_are_wired_in_public_ui() -> None:
    public = Path(__file__).resolve().parents[1] / "public"
    home = (public / "index.html").read_text(encoding="utf-8")
    interview = (public / "interview.html").read_text(encoding="utf-8")
    report = (public / "report.html").read_text(encoding="utf-8")
    home_js = (public / "js" / "home.js").read_text(encoding="utf-8")
    interview_js = (public / "js" / "interview.js").read_text(encoding="utf-8")
    report_js = (public / "js" / "report.js").read_text(encoding="utf-8")

    assert 'id="memoryEnabled"' in home
    assert 'name="language_mode"' in home
    assert "memory_enabled: memory" in home_js
    assert "language_mode: languageMode" in home_js
    assert 'id="hintButton"' in interview and 'id="hintPanel"' in interview
    assert "/hint`" in interview_js
    assert 'id="retryWeakButton"' in report and 'id="hintUsageList"' in report
    assert "/retry`" in report_js


def resume_payload() -> dict:
    return {
        "教育": [],
        "实习经历": [],
        "项目": [
            {
                "name": "校园秒杀系统",
                "role": "后端负责人",
                "technologies": ["Java", "Redis", "MySQL"],
                "highlights": ["使用 Redis Lua 做库存预扣"],
                "metrics": ["峰值 QPS 3000"],
            }
        ],
        "技能": ["Java", "Redis", "MySQL"],
    }


def test_mock_research_gap_is_low_score_but_not_breakdown(tmp_path) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "research-gap.db",
    )
    engine = InterviewEngine(Database(settings), settings)
    specialization = "AI 工程后端 / LLM Infra"
    research_question = load_current_research_question_bank(specialization)[0][
        "question"
    ]
    interview = {
        "id": "research-seed-001",
        "company": "tencent",
        "specialization": specialization,
        "duration_minutes": 15,
        "weak_topics": [],
        "last_question": f"我对这个前提存疑，{research_question}",
    }
    resume = ResumeData.model_validate(resume_payload())

    research_gap = engine._mock_decision(
        interview,
        resume,
        [],
        "这篇论文我没读过，所以暂时不知道。",
    )
    assert research_gap.assessment.failed is False
    assert research_gap.assessment.score < 5
    assert research_gap.assessment.deductions

    interview["last_question"] = "请解释 Redis 缓存穿透。"
    ordinary_gap = engine._mock_decision(interview, resume, [], "不知道")
    assert ordinary_gap.assessment.failed is True


@pytest.mark.asyncio
async def test_hint_memory_opt_out_and_owned_retry(tmp_path, monkeypatch) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "hint-memory.db",
        daily_interview_limit=20,
        client_daily_interview_limit=10,
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    reports = ReportEngine(database, settings)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "interview_engine", engine)
    monkeypatch.setattr(main_module, "report_engine", reports)

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        created_response = await client.post(
            "/api/interviews",
            json={
                "client_id": "memory-client-001",
                "resume": resume_payload(),
                "company": "bytedance",
                "role": "backend",
                "memory_enabled": False,
                "duration_minutes": 15,
            },
        )
        assert created_response.status_code == 201
        created = created_response.json()
        interview_id = created["id"]
        assert created["memory_enabled"] is False
        assert created["language_mode"] == "bilingual"
        assert created["weak_topics"] == []

        before_start = await client.post(f"/api/interviews/{interview_id}/hint")
        assert before_start.status_code == 409
        assert before_start.json()["error"]["code"] == "INTERVIEW_NOT_STARTED"

        await database.start_interview(interview_id)
        first_hint, duplicate_hint = await asyncio.gather(
            client.post(f"/api/interviews/{interview_id}/hint"),
            client.post(f"/api/interviews/{interview_id}/hint"),
        )
        assert first_hint.status_code == duplicate_hint.status_code == 200
        assert first_hint.json()["created"] is True
        assert duplicate_hint.json()["created"] is False
        assert first_hint.json()["hint_count"] == duplicate_hint.json()["hint_count"] == 1
        assert first_hint.json()["hint"] == duplicate_hint.json()["hint"]

        result = await engine.answer(
            interview_id,
            "我负责 Redis Lua 库存预扣，请求经过网关后异步落 MySQL，峰值 QPS 3000。",
        )
        assert not result.ended
        second_hint = await client.post(f"/api/interviews/{interview_id}/hint")
        assert second_hint.status_code == 200
        assert second_hint.json()["ordinal"] == 2
        assert second_hint.json()["hint_count"] == 2

        state = (await client.get(f"/api/interviews/{interview_id}")).json()
        assert state["memory_enabled"] is False
        assert state["hint_count"] == 2
        assert [event["ordinal"] for event in state["hint_events"]] == [1, 2]

        await database.finish_interview(interview_id, "manual")
        report_response = await client.get(
            f"/api/interviews/{interview_id}/report"
        )
        assert report_response.status_code == 200
        report = report_response.json()
        assert report["memory_enabled"] is False
        assert report["hint_count"] == 2
        assert [event["ordinal"] for event in report["hint_events"]] == [1, 2]

        # An opted-out report remains visible but cannot affect implicit memory.
        assert await database.weak_topics("memory-client-001") == []
        fresh_response = await client.post(
            "/api/interviews",
            json={
                "client_id": "memory-client-001",
                "resume": resume_payload(),
                "company": "tencent",
                "role": "backend",
                "language_mode": "zh",
                "memory_enabled": True,
                "duration_minutes": 10,
            },
        )
        assert fresh_response.status_code == 201
        assert fresh_response.json()["weak_topics"] == []
        assert fresh_response.json()["language_mode"] == "zh"

        forbidden_retry = await client.post(
            f"/api/interviews/{interview_id}/retry",
            json={"client_id": "different-client-001"},
        )
        assert forbidden_retry.status_code == 404

        retry_response = await client.post(
            f"/api/interviews/{interview_id}/retry",
            json={"client_id": "memory-client-001"},
        )
        assert retry_response.status_code == 201
        retry = retry_response.json()
        assert retry["retry_of"] == interview_id
        assert retry["memory_enabled"] is True
        assert retry["language_mode"] == "bilingual"
        assert retry["weak_topics"]
        source_row = await database.get_interview(interview_id)
        retry_row = await database.get_interview(retry["id"])
        assert source_row is not None and retry_row is not None
        assert retry_row["resume"] == source_row["resume"]
        assert retry_row["client_id"] == source_row["client_id"]
