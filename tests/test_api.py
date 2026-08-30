from __future__ import annotations

import asyncio
from dataclasses import replace
import json
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
        assert config_payload["language_modes"] == [
            {"id": "zh", "name": "中文"},
            {"id": "en", "name": "English"},
        ]
        assert [item["id"] for item in config_payload["interview_types"]] == [
            "technical",
            "hr",
            "technical_hr",
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
        assert set(resume) == {"姓名", "教育", "实习经历", "项目", "技能"}

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
        assert unlimited_payload["interview_type"] == "technical"
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
        assert "学习" in created.json()["initial_question"]
        assert "项目" in created.json()["initial_question"]
        assert "不用展开" in created.json()["initial_question"]
        interview_id = created.json()["id"]
        await database.start_interview(interview_id)
        result = await main_module.interview_engine.answer(
            interview_id,
            "我负责 Redis Lua 库存预扣，入口经过网关，异步写入 MySQL，峰值 QPS 3000。",
        )
        assert result.turn.drill_depth == 0
        assert result.turn.category == "communication"
        assert result.turn.topic == "自我介绍·整体与学习情况"
        assert "单独聊一段经历" in result.question
        assert resume["项目"][0]["name"] in result.question

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
async def test_active_interview_can_be_discarded_without_history_or_quota(
    tmp_path, monkeypatch
) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "discard-interview.db",
        daily_interview_limit=20,
        client_daily_interview_limit=5,
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "interview_engine", engine)
    client_id = "discard-client-001"
    created = await engine.create(
        InterviewCreate(
            client_id=client_id,
            company="bytedance",
            resume=ResumeData(
                项目=[Project(name="订单服务", technologies=["Java", "MySQL"])]
            ),
        )
    )
    await database.start_interview(created["id"])
    await engine.answer(created["id"], "我负责订单链路和事务边界。")
    assert await database.interview_count_today(client_id) == 1

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        rejected = await client.delete(
            f"/api/history/{created['id']}",
            params={"client_id": "another-client-001"},
        )
        assert rejected.status_code == 404
        assert await database.get_interview(created["id"]) is not None
        discarded = await client.delete(
            f"/api/history/{created['id']}", params={"client_id": client_id}
        )
        assert discarded.status_code == 200
        assert discarded.json() == {"deleted": True}
        assert (await client.get(f"/api/interviews/{created['id']}")).status_code == 404
        history = await client.get("/api/history", params={"client_id": client_id})
        assert history.json() == {"items": [], "weak_topics": []}

    assert await database.get_interview(created["id"]) is None
    assert await database.list_turns(created["id"]) == []
    assert await database.interview_count_today(client_id) == 0


@pytest.mark.asyncio
async def test_interviews_are_not_limited_by_daily_counts(tmp_path, monkeypatch) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "unlimited-interviews.db",
        daily_interview_limit=0,
        client_daily_interview_limit=0,
    )
    database = Database(settings)
    await database.initialize()
    monkeypatch.setattr(main_module, "interview_engine", InterviewEngine(database, settings))

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {
            "client_id": "unlimited-client-001",
            "company": "bytedance",
            "resume": {"项目": [{"name": "订单服务", "technologies": ["Python"]}]},
        }
        assert (await client.post("/api/interviews", json=payload)).status_code == 201
        assert (await client.post("/api/interviews", json=payload)).status_code == 201


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


@pytest.mark.asyncio
async def test_voice_end_disconnect_still_commits_and_queues_report(
    tmp_path, monkeypatch
) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L0",
        voice_auto_fallback=False,
        db_path=tmp_path / "voice-disconnect.db",
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="voice-disconnect-client",
            company="bytedance",
            resume=ResumeData(
                项目=[Project(name="缓存服务", technologies=["Redis", "Java"])]
            ),
        )
    )

    class FakeWebSocket:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[dict] = asyncio.Queue()
            self.sent: list[dict] = []

        async def accept(self) -> None:
            return None

        async def receive(self) -> dict:
            return await self.incoming.get()

        async def send_json(self, payload: dict) -> None:
            self.sent.append(payload)

        async def close(self, code: int = 1000) -> None:
            return None

        async def event(self, payload: dict) -> None:
            await self.incoming.put(
                {
                    "type": "websocket.receive",
                    "text": json.dumps(payload),
                }
            )

        async def disconnect(self) -> None:
            await self.incoming.put({"type": "websocket.disconnect"})

    class FakeVoiceSession:
        instances: list["FakeVoiceSession"] = []

        def __init__(self, **_kwargs) -> None:
            self.actual_mode = "L0"
            self.prepare_started = asyncio.Event()
            self.allow_prepare = asyncio.Event()
            self.announce_started = asyncio.Event()
            self.microphone_states: list[bool] = []
            self.closed = False
            self.__class__.instances.append(self)

        async def start(self, _question: str) -> str:
            return "L0"

        async def prepare_end(self, *, drain_timeout: float) -> None:
            assert drain_timeout == main_module.VOICE_END_DRAIN_TIMEOUT_SECONDS
            self.prepare_started.set()
            await self.allow_prepare.wait()

        async def announce(self, *_args, **_kwargs) -> None:
            self.announce_started.set()
            await asyncio.Event().wait()

        async def handle_microphone_state(self, enabled: bool) -> None:
            self.microphone_states.append(enabled)

        async def close(self) -> None:
            self.closed = True

    reports: list[str] = []

    def record_report(interview_id: str, **_kwargs) -> None:
        reports.append(interview_id)

    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "interview_engine", engine)
    monkeypatch.setattr(main_module, "BrowserVoiceSession", FakeVoiceSession)
    monkeypatch.setattr(main_module, "_spawn_report", record_report)
    monkeypatch.setattr(main_module, "WS_CONTROL_DRAIN_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(main_module, "VOICE_CLOSE_TIMEOUT_SECONDS", 0.1)

    websocket = FakeWebSocket()
    socket_task = asyncio.create_task(
        main_module.interview_socket(websocket, created["id"])
    )
    await websocket.event({"type": "client.ready"})

    async def wait_for(predicate) -> None:
        deadline = asyncio.get_running_loop().time() + 1
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("condition not reached")
            await asyncio.sleep(0.001)

    await wait_for(lambda: bool(FakeVoiceSession.instances))
    voice = FakeVoiceSession.instances[0]
    await websocket.event({"type": "microphone.state", "enabled": False})
    await wait_for(lambda: voice.microphone_states == [False])
    assert any(
        event.get("type") == "microphone.state.changed"
        and event.get("enabled") is False
        for event in websocket.sent
    )

    await websocket.event({"type": "interview.end", "reason": "manual"})
    await asyncio.wait_for(voice.prepare_started.wait(), timeout=1)
    # Reproduce the production race: the browser leaves while the accepted
    # final answer is still draining.
    await websocket.disconnect()
    voice.allow_prepare.set()
    await asyncio.wait_for(socket_task, timeout=1)

    state = await database.get_interview(created["id"])
    assert state is not None
    assert state["status"] == "ended"
    assert state["end_reason"] == "manual"
    assert reports == [created["id"]]
    assert voice.announce_started.is_set()
    assert voice.closed is True


@pytest.mark.asyncio
async def test_l3_websocket_answer_boundaries_record_server_elapsed_time(
    tmp_path, monkeypatch
) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "answer-boundary.db",
        daily_interview_limit=20,
        client_daily_interview_limit=5,
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="answer-boundary-client",
            company="tencent",
            interview_type="technical_hr",
            resume=ResumeData(
                项目=[Project(name="课程订单系统", technologies=["Java", "MySQL"])]
            ),
        )
    )

    class FakeWebSocket:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[dict] = asyncio.Queue()
            self.sent: list[dict] = []

        async def accept(self) -> None:
            return None

        async def receive(self) -> dict:
            return await self.incoming.get()

        async def send_json(self, payload: dict) -> None:
            self.sent.append(payload)

        async def close(self, code: int = 1000) -> None:
            return None

        async def event(self, payload: dict) -> None:
            await self.incoming.put(
                {"type": "websocket.receive", "text": json.dumps(payload)}
            )

        async def disconnect(self) -> None:
            await self.incoming.put({"type": "websocket.disconnect"})

    async def wait_for(predicate) -> None:
        deadline = asyncio.get_running_loop().time() + 1
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("condition not reached")
            await asyncio.sleep(0.001)

    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "interview_engine", engine)

    websocket = FakeWebSocket()
    socket_task = asyncio.create_task(
        main_module.interview_socket(websocket, created["id"])
    )
    await websocket.event({"type": "client.ready"})
    await wait_for(
        lambda: any(event.get("type") == "session.ready" for event in websocket.sent)
    )
    ready = next(event for event in websocket.sent if event["type"] == "session.ready")
    assert ready["session"]["interview_type"] == "technical_hr"

    await websocket.event({"type": "interview.pause", "paused": True})
    await wait_for(
        lambda: any(
            event.get("type") == "interview.pause.changed"
            and event.get("paused") is True
            for event in websocket.sent
        )
    )
    frozen = await database.get_interview(created["id"])
    assert frozen is not None and frozen["paused"] is True
    frozen_elapsed = frozen["question_elapsed_seconds"]
    await asyncio.sleep(0.02)
    still_frozen = await database.get_interview(created["id"])
    assert still_frozen is not None
    assert still_frozen["question_elapsed_seconds"] == pytest.approx(frozen_elapsed)
    await websocket.event({"type": "answer.start"})
    await wait_for(
        lambda: any(
            event.get("type") == "error"
            and event.get("code") == "INTERVIEW_PAUSED"
            for event in websocket.sent
        )
    )
    await websocket.event({"type": "interview.pause", "paused": False})
    await wait_for(
        lambda: any(
            event.get("type") == "interview.pause.changed"
            and event.get("paused") is False
            for event in websocket.sent
        )
    )
    await asyncio.sleep(0.02)

    await websocket.event({"type": "answer.start"})
    await wait_for(
        lambda: any(
            event.get("type") == "answer.state.changed"
            and event.get("state") == "answering"
            for event in websocket.sent
        )
    )
    await asyncio.sleep(0.01)
    await websocket.event(
        {
            "type": "answer.end",
            # The browser value is display-only. Durable time starts when the
            # question appears, includes thinking, and excludes explicit pause.
            "elapsed_ms": 999_999,
            "text": "我是计算机专业大三学生，学过数据结构、数据库和操作系统。",
        }
    )
    await wait_for(
        lambda: len(
            [event for event in websocket.sent if event.get("type") == "interviewer.text.done"]
        )
        >= 2
    )
    turns = await database.list_turns(created["id"])
    assert len(turns) == 1
    assert turns[0].input_mode == "text"
    assert turns[0].answer_duration_seconds is not None
    assert turns[0].answer_duration_seconds >= 0.02
    assert turns[0].answer_duration_seconds < 1
    assert turns[0].answer_duration_seconds != pytest.approx(999.999)
    assert turns[0].topic == "自我介绍·整体与学习情况"
    assert any(
        event.get("type") == "answer.state.changed"
        and event.get("state") == "sealing"
        for event in websocket.sent
    )
    assert sum(
        event.get("type") == "answer.state.changed"
        and event.get("state") == "idle"
        for event in websocket.sent
    ) >= 2

    await websocket.disconnect()
    await asyncio.wait_for(socket_task, timeout=1)
