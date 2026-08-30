from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import hmac
import json
import re
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .config import ROOT_DIR, Settings, get_settings
from .content import (
    COMPANIES,
    load_interview_skill,
    load_specialization_catalog,
    load_source_catalog,
    load_style_card,
)
from .db import Database
from .errors import AppError
from .hardware_test import HARDWARE_TEST_MAX_SECONDS, HardwareTranscriptionSession
from .interview_engine import InterviewEngine
from .practice import (
    PracticeAnswerCreate,
    PracticeHintCreate,
    PracticeService,
    PracticeSessionCreate,
)
from .profile import ProfileService
from .profile_routes import create_profile_router
from .project_context import enrich_interview_with_profile_project
from .report_engine import ReportEngine
from .resume import ResumeParser, extract_pdf_text
from .schemas import (
    InterviewCreate,
    InterviewFinish,
    InterviewRetry,
    TranscriptCorrection,
)
from .voice_session import BrowserVoiceSession


settings: Settings = get_settings()
db = Database(settings)
resume_parser = ResumeParser(settings)
interview_engine = InterviewEngine(db, settings)
report_engine = ReportEngine(db, settings)
practice_service = PracticeService(db, settings)
profile_service = ProfileService(db, settings, resume_parser=resume_parser)
background_tasks: set[asyncio.Task[Any]] = set()
session_locks: dict[str, asyncio.Lock] = {}
VOICE_END_DRAIN_TIMEOUT_SECONDS = 5.0
VOICE_END_ANNOUNCE_TIMEOUT_SECONDS = 12.0
WS_CONTROL_DRAIN_TIMEOUT_SECONDS = 9.0
VOICE_CLOSE_TIMEOUT_SECONDS = 8.0


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self.entries: dict[str, deque[float]] = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        async with self.lock:
            bucket = self.entries[key]
            while bucket and bucket[0] <= now - window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True


resume_limiter = SlidingWindowLimiter()
hardware_test_limiter = SlidingWindowLimiter()
practice_voice_limiter = SlidingWindowLimiter()
practice_answer_limiter = SlidingWindowLimiter()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await db.initialize()
    await practice_service.initialize()
    await profile_service.initialize()
    yield
    pending = list(background_tasks)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


app = FastAPI(
    title="AI 模拟面试官",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)
app.include_router(
    create_profile_router(lambda: profile_service, lambda: settings)
)

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Profile-Key"],
    )


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Any:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(self), geolocation=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self' ws: wss:; "
        "media-src 'self' blob: data:; worker-src 'self' blob:; frame-ancestors 'none'",
    )
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = [
        {
            key: value
            for key, value in error.items()
            if key not in {"input", "ctx", "url"}
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "请求参数不正确",
                "details": {"errors": errors},
            }
        },
    )


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def ready() -> dict[str, Any]:
    return {
        "status": "ready",
        "voice_mode": settings.voice_mode,
        "llm_configured": settings.has_api_key or settings.mock_llm,
    }


@app.get("/api/config")
async def config() -> dict[str, Any]:
    companies = []
    for key, label in COMPANIES.items():
        card = load_style_card(key)
        skill = load_interview_skill(key)
        companies.append(
            {
                "id": key,
                "name": label,
                "role": "后端开发实习生",
                "stress_default": bool(card.get("stress_default", False)),
                "preferences": card.get("followup_preferences", []),
                "tone": skill.get("tone", ""),
                "evidence_level": skill.get("evidence_level", "compatibility"),
            }
        )
    return {
        "app_name": settings.app_name,
        "voice_mode": settings.voice_mode,
        "voice_auto_fallback": settings.voice_auto_fallback,
        "llm_configured": settings.has_api_key or settings.mock_llm,
        "mock_mode": settings.mock_llm,
        "companies": companies,
        "specializations": load_specialization_catalog(),
        "custom_specialization": {"enabled": True, "max_length": 80},
        "durations": [10, 15, 25],
        "custom_duration": {"min": 1, "max": 180, "unlimited": True},
        "stress_levels": [
            {"level": 0, "name": "关闭"},
            {"level": 1, "name": "温和"},
            {"level": 2, "name": "标准"},
            {"level": 3, "name": "高压"},
        ],
        "language_modes": [
            {"id": "zh", "name": "全程中文"},
            {"id": "bilingual", "name": "中英双语"},
            {"id": "en", "name": "Pure English"},
        ],
        "interview_types": [
            {"id": "technical", "name": "技术面"},
            {"id": "hr", "name": "综合面（HR 面）"},
            {"id": "technical_hr", "name": "技术 / 综合（HR）面"},
        ],
        "daily_interview_limit": settings.daily_interview_limit,
    }


@app.websocket("/ws/hardware-test")
async def hardware_test_socket(websocket: WebSocket) -> None:
    """Run a short ASR-only microphone check without creating an interview."""

    await websocket.accept()
    send_lock = asyncio.Lock()
    session: HardwareTranscriptionSession | None = None

    async def send(event_type: str, **payload: Any) -> None:
        async with send_lock:
            await websocket.send_json({"type": event_type, **payload})

    try:
        first = await asyncio.wait_for(websocket.receive(), timeout=10)
        if first.get("type") == "websocket.disconnect":
            return
        raw = first.get("text")
        if not raw:
            await send(
                "hardware.error",
                code="READY_REQUIRED",
                message="请先初始化麦克风测试",
                recoverable=False,
            )
            await websocket.close(code=4400)
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            event = {}
        if event.get("type") != "client.ready":
            await send(
                "hardware.error",
                code="READY_REQUIRED",
                message="请先初始化麦克风测试",
                recoverable=False,
            )
            await websocket.close(code=4400)
            return
        client_id = str(event.get("client_id") or "").strip()
        try:
            _validate_client_id(client_id)
        except AppError as exc:
            await send(
                "hardware.error",
                code=exc.code,
                message=exc.message,
                recoverable=False,
            )
            await websocket.close(code=4400)
            return
        client_host = websocket.client.host if websocket.client else "unknown"
        host_allowed = await hardware_test_limiter.allow(
            f"hardware-host:{client_host}", 20, 3600
        )
        client_allowed = await hardware_test_limiter.allow(
            f"hardware-client:{client_id}", 8, 3600
        )
        if not host_allowed or not client_allowed:
            await send(
                "hardware.error",
                code="HARDWARE_TEST_RATE_LIMIT",
                message="语音测试过于频繁，请稍后再试",
                recoverable=False,
            )
            await websocket.close(code=4408)
            return

        session = HardwareTranscriptionSession(settings, send)
        await session.start()
        deadline = time.monotonic() + HARDWARE_TEST_MAX_SECONDS
        while not session.stopped:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await session.stop(reason="limit")
                break
            try:
                message = await asyncio.wait_for(
                    websocket.receive(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                await session.stop(reason="limit")
                break
            if message.get("type") == "websocket.disconnect":
                break
            pcm = message.get("bytes")
            if pcm is not None:
                await session.handle_audio(pcm)
                continue
            raw = message.get("text")
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                await send(
                    "hardware.error",
                    code="INVALID_EVENT",
                    message="语音测试消息格式错误",
                    recoverable=True,
                )
                continue
            event_type = event.get("type")
            if event_type == "hardware.stop":
                await session.stop(reason="manual")
                break
            if event_type == "ping":
                await send("pong", timestamp=time.time())
                continue
            await send(
                "hardware.error",
                code="UNKNOWN_EVENT",
                message="不支持的语音测试消息",
                recoverable=True,
            )
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        with suppress(Exception):
            await send(
                "hardware.error",
                code="HARDWARE_TEST_FAILED",
                message="语音测试暂时不可用，请稍后重试",
                recoverable=False,
            )
    finally:
        if session:
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(session.close(), timeout=7)
        with suppress(Exception):
            await websocket.close()


@app.get("/api/practice/catalog")
async def practice_catalog() -> dict[str, Any]:
    return await practice_service.catalog()


@app.post("/api/practice/sessions", status_code=201)
async def create_practice_session(
    request: PracticeSessionCreate,
) -> dict[str, Any]:
    return await practice_service.create_session(request)


@app.get("/api/practice/sessions/{session_id}")
async def get_practice_session(
    session_id: str,
    client_id: str = Query(min_length=8, max_length=128),
) -> dict[str, Any]:
    return await practice_service.get_session(session_id, client_id)


@app.post("/api/practice/sessions/{session_id}/answers")
async def submit_practice_answer(
    session_id: str,
    request: PracticeAnswerCreate,
    http_request: Request,
) -> dict[str, Any]:
    client_host = http_request.client.host if http_request.client else "unknown"
    host_allowed = await practice_answer_limiter.allow(
        f"practice-answer-host:{client_host}", 120, 3600
    )
    client_allowed = await practice_answer_limiter.allow(
        f"practice-answer-client:{request.client_id}", 40, 3600
    )
    if not host_allowed or not client_allowed:
        raise AppError(
            "PRACTICE_ANSWER_RATE_LIMIT",
            "单题评分过于频繁，请稍后再试。你的回答仍可保留在输入框中。",
            status_code=429,
        )
    return await practice_service.submit_answer(session_id, request)


@app.post("/api/practice/sessions/{session_id}/hint")
async def get_practice_hint(
    session_id: str,
    request: PracticeHintCreate,
) -> dict[str, Any]:
    return await practice_service.hint(session_id, request)


@app.get("/api/practice/history")
async def practice_history(
    client_id: str = Query(min_length=8, max_length=128),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    return {"items": await practice_service.history(client_id, limit)}


@app.websocket("/ws/practice/sessions/{session_id}")
async def practice_transcription_socket(
    websocket: WebSocket, session_id: str
) -> None:
    """ASR-only transport for a single quick-drill answer.

    Scoring still goes through the same REST answer endpoint as typed input;
    this socket only turns 16 kHz PCM into an editable transcript.
    """

    await websocket.accept()
    send_lock = asyncio.Lock()
    transcription: HardwareTranscriptionSession | None = None
    final_segments: list[str] = []

    async def send(event_type: str, **payload: Any) -> None:
        async with send_lock:
            await websocket.send_json({"type": event_type, **payload})

    async def forward_hardware(event_type: str, **payload: Any) -> None:
        mapped = (
            f"practice.{event_type.removeprefix('hardware.')}"
            if event_type.startswith("hardware.")
            else event_type
        )
        if event_type in {
            "hardware.transcript.partial",
            "hardware.transcript.done",
        }:
            segment = str(payload.get("text") or "").strip()
            if event_type.endswith(".done") and segment:
                if not final_segments or final_segments[-1] != segment:
                    final_segments.append(segment)
                payload["text"] = " ".join(final_segments)
            elif segment:
                payload["text"] = " ".join([*final_segments, segment])
        await send(mapped, **payload)

    try:
        first = await asyncio.wait_for(websocket.receive(), timeout=10)
        raw = first.get("text")
        if first.get("type") == "websocket.disconnect":
            return
        try:
            event = json.loads(raw or "{}")
        except json.JSONDecodeError:
            event = {}
        client_id = str(event.get("client_id") or "").strip()
        if event.get("type") != "client.ready" or not await practice_service.owns_session(
            session_id, client_id
        ):
            await send(
                "practice.error",
                code="PRACTICE_FORBIDDEN",
                message="刷题语音会话校验失败",
                recoverable=False,
            )
            await websocket.close(code=4403)
            return
        client_host = websocket.client.host if websocket.client else "unknown"
        host_allowed = await practice_voice_limiter.allow(
            f"practice-host:{client_host}", 60, 3600
        )
        client_allowed = await practice_voice_limiter.allow(
            f"practice-client:{client_id}", 30, 3600
        )
        if not host_allowed or not client_allowed:
            await send(
                "practice.error",
                code="PRACTICE_VOICE_RATE_LIMIT",
                message="语音刷题过于频繁，请稍后再试或切换文字输入",
                recoverable=False,
            )
            await websocket.close(code=4408)
            return

        max_seconds = 180
        transcription = HardwareTranscriptionSession(
            settings,
            forward_hardware,
            max_seconds=max_seconds,
        )
        await transcription.start()
        deadline = time.monotonic() + max_seconds
        while not transcription.stopped:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await transcription.stop(reason="limit")
                break
            try:
                message = await asyncio.wait_for(
                    websocket.receive(), timeout=remaining
                )
            except asyncio.TimeoutError:
                await transcription.stop(reason="limit")
                break
            if message.get("type") == "websocket.disconnect":
                break
            pcm = message.get("bytes")
            if pcm is not None:
                await transcription.handle_audio(pcm)
                continue
            text = message.get("text")
            if not text:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                await send(
                    "practice.error",
                    code="INVALID_EVENT",
                    message="刷题语音消息格式错误",
                    recoverable=True,
                )
                continue
            if control.get("type") == "practice.stop":
                await transcription.stop(reason="manual")
                break
            if control.get("type") == "ping":
                await send("pong", timestamp=time.time())
                continue
            await send(
                "practice.error",
                code="UNKNOWN_EVENT",
                message="不支持的刷题语音消息",
                recoverable=True,
            )
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        with suppress(Exception):
            await send(
                "practice.error",
                code="PRACTICE_TRANSCRIPTION_FAILED",
                message="实时转写暂时不可用，可以切换文字作答",
                recoverable=False,
            )
    finally:
        if transcription:
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(transcription.close(), timeout=7)
        with suppress(Exception):
            await websocket.close()


@app.get("/api/resources/catalog")
async def resource_catalog() -> dict[str, Any]:
    """Expose the curated provenance catalog without copying source bodies."""

    return load_source_catalog()


@app.post("/api/resumes/parse")
async def parse_resume(
    request: Request,
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
) -> dict[str, Any]:
    client_host = request.client.host if request.client else "unknown"
    if not await resume_limiter.allow(f"resume:{client_host}", 12, 3600):
        raise AppError(
            "RESUME_RATE_LIMIT",
            "简历解析过于频繁，请一小时后再试。",
            status_code=429,
        )
    source = "text"
    raw_text = (text or "").strip()
    if file is not None and file.filename:
        source = "pdf"
        filename = file.filename.lower()
        if not filename.endswith(".pdf"):
            raise AppError("INVALID_FILE_TYPE", "只支持 PDF 文件", status_code=422)
        max_bytes = settings.max_pdf_mb * 1024 * 1024
        raw_text = extract_pdf_text(await file.read(max_bytes + 1), settings.max_pdf_mb)
    if not raw_text:
        raise AppError(
            "RESUME_REQUIRED", "请上传 PDF 或粘贴简历文本", status_code=422
        )
    parsed = await resume_parser.parse(raw_text)
    return {
        "source": source,
        "text_length": len(raw_text),
        "resume": parsed.model_dump(by_alias=True),
    }


@app.post("/api/interviews", status_code=201)
async def create_interview(
    request: InterviewCreate,
    profile_key: str | None = Header(default=None, alias="X-Profile-Key"),
) -> dict[str, Any]:
    if request.profile_project_id and (
        not profile_key
        or len(profile_key) < 24
        or not hmac.compare_digest(profile_key, request.client_id)
    ):
        raise AppError(
            "PROFILE_KEY_MISMATCH",
            "使用匿名 Profile 项目时需要校验当前浏览器密钥",
            status_code=403,
        )
    # Check quotas before an optional, potentially paid project-analysis call.
    # InterviewEngine.create checks again to close the race with concurrent
    # session creation.
    await interview_engine.ensure_budget_available(request.client_id)
    enriched = await enrich_interview_with_profile_project(request, profile_service)
    created = await interview_engine.create(enriched)
    if request.profile_project_id:
        created["profile_project_id"] = request.profile_project_id
    return created


@app.get("/api/interviews/{interview_id}")
async def get_interview(interview_id: str) -> dict[str, Any]:
    interview = await _require_interview(interview_id)
    return _public_interview(interview)


@app.post("/api/interviews/{interview_id}/hint")
async def get_interview_hint(interview_id: str) -> dict[str, Any]:
    return await interview_engine.hint(interview_id)


@app.post("/api/interviews/{interview_id}/retry", status_code=201)
async def retry_interview(
    interview_id: str, request: InterviewRetry
) -> dict[str, Any]:
    return await interview_engine.retry(interview_id, request.client_id)


@app.patch("/api/interviews/{interview_id}/turns/{ordinal}")
async def correct_interview_transcript(
    interview_id: str, ordinal: int, request: TranscriptCorrection
) -> dict[str, Any]:
    await _require_interview(interview_id)
    corrected = await interview_engine.correct_answer(
        interview_id, ordinal=ordinal, text=request.text
    )
    return {**corrected, "item_id": request.item_id}


@app.post("/api/interviews/{interview_id}/finish", status_code=202)
async def finish_interview(
    interview_id: str, request: InterviewFinish | None = None
) -> dict[str, Any]:
    interview = await _require_interview(interview_id)
    if interview["status"] in {"created", "active"}:
        await db.finish_interview(
            interview_id, request.reason if request else "manual"
        )
    _spawn_report(interview_id)
    return {"status": "reporting", "interview_id": interview_id}


@app.get("/api/interviews/{interview_id}/report")
async def get_report(interview_id: str) -> dict[str, Any]:
    interview = await _require_interview(interview_id)
    existing = await db.get_report(interview_id)
    if existing:
        return existing
    if interview["status"] in {"created", "active"}:
        raise AppError(
            "REPORT_NOT_READY", "面试尚未结束，暂时不能生成报告", status_code=409
        )
    report = await report_engine.generate(interview_id)
    return report.model_dump()


@app.get("/api/history")
async def history(
    client_id: str = Query(min_length=8, max_length=128),
) -> dict[str, Any]:
    _validate_client_id(client_id)
    reports = await db.history(client_id)
    return {"items": reports, "weak_topics": await db.weak_topics(client_id)}


@app.delete("/api/history/{interview_id}")
async def delete_history_item(
    interview_id: str,
    client_id: str = Query(min_length=8, max_length=128),
) -> dict[str, Any]:
    _validate_client_id(client_id)
    deleted = await db.delete_history_item(client_id, interview_id)
    if not deleted:
        raise AppError("HISTORY_NOT_FOUND", "历史记录不存在", status_code=404)
    return {"deleted": True}


@app.delete("/api/history")
async def clear_history(
    client_id: str = Query(min_length=8, max_length=128),
) -> dict[str, Any]:
    _validate_client_id(client_id)
    return {"deleted": await db.clear_history(client_id)}


@app.websocket("/ws/interviews/{interview_id}")
async def interview_socket(websocket: WebSocket, interview_id: str) -> None:
    await websocket.accept()
    send_lock = asyncio.Lock()
    ended = asyncio.Event()
    ending = asyncio.Event()
    terminal_committed = asyncio.Event()
    terminal_lock = asyncio.Lock()
    timer_task: asyncio.Task[None] | None = None
    voice_session: BrowserVoiceSession | None = None
    answer_tasks: set[asyncio.Task[None]] = set()
    control_tasks: set[asyncio.Task[None]] = set()
    ready = False
    requested_end_reason = "manual"
    browser_microphone_enabled = True
    text_answer_started_at: float | None = None
    text_answer_protocol_enabled = False

    async def send(event_type: str, **payload: Any) -> None:
        async with send_lock:
            await websocket.send_json({"type": event_type, **payload})

    def track(coroutine: Any, bucket: set[asyncio.Task[None]], name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        bucket.add(task)

        def consume_result(done: asyncio.Task[None]) -> None:
            bucket.discard(done)
            if done.cancelled():
                return
            # Background answer/control tasks report expected failures to the
            # browser themselves. Still retrieve the result so asyncio does
            # not emit a noisy "Task exception was never retrieved" warning.
            with suppress(Exception):
                done.exception()

        task.add_done_callback(consume_result)

    async def cancel_answers() -> None:
        current = asyncio.current_task()
        pending = [
            task
            for task in answer_tasks
            if task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def drain_answers(timeout: float = 20.0) -> None:
        """Let accepted answers commit before ending, then preserve on cancel."""

        current = asyncio.current_task()
        pending = [
            task
            for task in answer_tasks
            if task is not current and not task.done()
        ]
        if not pending:
            return
        _, unfinished = await asyncio.wait(pending, timeout=timeout)
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)

    async def commit_terminal(reason: str) -> bool:
        """Durably finish and enqueue reporting independently of the socket."""

        async with terminal_lock:
            if terminal_committed.is_set():
                return False
            normalized_reason = str(reason or "manual")
            await db.finish_interview(interview_id, normalized_reason)
            terminal_committed.set()
            _spawn_report(
                interview_id,
                websocket=websocket,
                send_lock=send_lock,
            )
            return True

    async def finalize(reason: str) -> None:
        async with terminal_lock:
            if terminal_committed.is_set():
                return
            ending.set()
            await cancel_answers()
        await commit_terminal(reason)
        ended.set()
        with suppress(Exception):
            await send("interview.ended", reason=reason)

    async def terminate(reason: str) -> None:
        nonlocal voice_session, requested_end_reason
        async with terminal_lock:
            if terminal_committed.is_set() or ending.is_set():
                return
            requested_end_reason = reason if reason in {"time", "manual"} else "manual"
            ending.set()
        # Do not hold terminal_lock while an accepted answer finishes: an
        # answer that independently triggers early-stop calls finalize(), which
        # also needs the lock.
        if voice_session:
            await voice_session.prepare_end(
                drain_timeout=VOICE_END_DRAIN_TIMEOUT_SECONDS
            )
        else:
            await drain_answers(timeout=VOICE_END_DRAIN_TIMEOUT_SECONDS)
        if not await commit_terminal(requested_end_reason):
            return
        closing_interview = await db.get_interview(interview_id)
        if closing_interview and closing_interview.get("language_mode") == "en":
            closing = (
                "We have reached the end of the scheduled time. That concludes today's interview. Thank you for your time."
                if requested_end_reason == "time"
                else "That concludes today's interview. Thank you for your time."
            )
        else:
            closing = (
                "时间到了，今天的面试就到这里，感谢你的时间。"
                if requested_end_reason == "time"
                else "好的，今天的面试就到这里，感谢你的时间。"
            )
        with suppress(Exception):
            await send("interviewer.text.done", text=closing)
        if voice_session:
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(
                    voice_session.announce(
                        closing,
                        cancel_current=True,
                        wait_for_playback=True,
                    ),
                    timeout=VOICE_END_ANNOUNCE_TIMEOUT_SECONDS,
                )
        ended.set()
        with suppress(Exception):
            await send("interview.ended", reason=requested_end_reason)

    async def voice_ended(reason: str) -> None:
        if reason == "time":
            await terminate("time")
        else:
            await finalize(reason)

    async def timer_loop() -> None:
        while not ended.is_set() and not ending.is_set():
            interview = await db.get_interview(interview_id)
            if not interview or interview["status"] in {"ended", "reporting", "reported"}:
                return
            remaining = interview.get("remaining_seconds")
            await send(
                "timer.sync",
                remaining_seconds=remaining,
                ends_at=interview.get("deadline_at"),
            )
            if interview.get("deadline_at") is not None and (
                remaining is None or int(remaining) <= 0
            ):
                await terminate("time")
                return
            try:
                await asyncio.wait_for(ended.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    try:
        interview = await db.get_interview(interview_id)
        if not interview:
            await send("error", code="INTERVIEW_NOT_FOUND", message="面试不存在")
            await websocket.close(code=4404)
            return
        while not ended.is_set():
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if ending.is_set():
                    continue
                if not voice_session or voice_session.actual_mode == "L3":
                    await send(
                        "error",
                        code="TEXT_MODE_ONLY",
                        message="当前为文字模式，请使用文字输入框。",
                        recoverable=True,
                    )
                else:
                    await voice_session.handle_audio(message["bytes"])
                continue

            raw = message.get("text")
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                await send("error", code="INVALID_EVENT", message="消息格式错误")
                continue
            event_type = event.get("type")
            if event_type == "audio.playback.done":
                if voice_session:
                    await voice_session.handle_playback_done(
                        str(event.get("announcement_id") or "")
                    )
                continue
            if event_type == "microphone.state":
                raw_enabled = event.get("enabled")
                if isinstance(raw_enabled, bool):
                    browser_microphone_enabled = raw_enabled
                else:
                    state = str(event.get("state") or "").strip().lower()
                    if state in {"on", "open", "enabled", "live", "active"}:
                        browser_microphone_enabled = True
                    elif state in {"off", "closed", "disabled", "muted", "ended"}:
                        browser_microphone_enabled = False
                    else:
                        await send(
                            "error",
                            code="INVALID_MICROPHONE_STATE",
                            message="麦克风状态参数不正确",
                            recoverable=True,
                        )
                        continue
                if voice_session:
                    await voice_session.handle_microphone_state(
                        browser_microphone_enabled
                    )
                await send(
                    "microphone.state.changed",
                    enabled=browser_microphone_enabled,
                    state=(
                        "enabled" if browser_microphone_enabled else "disabled"
                    ),
                )
                continue
            if event_type == "client.ready":
                if ready:
                    continue
                ready = True
                interview = await db.start_interview(interview_id)
                if not interview:
                    await send("error", code="INTERVIEW_NOT_FOUND", message="面试不存在")
                    break
                if interview["status"] in {"ended", "reporting", "reported"}:
                    await send(
                        "interview.ended",
                        reason=interview.get("end_reason") or "manual",
                    )
                    ended.set()
                    break
                await send(
                    "session.ready",
                    session=_public_interview(interview),
                    voice_mode=interview["voice_mode"],
                )
                await send(
                    "interviewer.text.done",
                    text=interview["last_question"],
                    recommended_answer_seconds=interview_engine.recommended_answer_seconds(
                        str(interview["last_question"])
                    ),
                )
                if interview["voice_mode"] != "L3":
                    voice_session = BrowserVoiceSession(
                        interview_id=interview_id,
                        interview=interview,
                        settings=settings,
                        db=db,
                        engine=interview_engine,
                        send=send,
                        on_end=voice_ended,
                    )
                    actual_mode = await voice_session.start(interview["last_question"])
                    await db.set_voice_mode(interview_id, actual_mode)
                    if not browser_microphone_enabled:
                        await voice_session.handle_microphone_state(False)
                await send("answer.state.changed", state="idle")
                timer_task = asyncio.create_task(timer_loop())
            elif event_type == "answer.start":
                if not ready:
                    await send("error", code="NOT_READY", message="请先开始面试")
                    continue
                if ending.is_set():
                    await send(
                        "error",
                        code="INTERVIEW_ENDING",
                        message="本场面试正在结束",
                        recoverable=False,
                    )
                    continue
                try:
                    if voice_session:
                        await voice_session.handle_answer_start()
                    else:
                        lock = session_locks.setdefault(interview_id, asyncio.Lock())
                        if any(not task.done() for task in answer_tasks) or lock.locked():
                            raise AppError(
                                "ANSWER_IN_PROGRESS",
                                "上一题仍在处理中，请等面试官问完下一题",
                                status_code=409,
                            )
                        text_answer_protocol_enabled = True
                        if text_answer_started_at is None:
                            text_answer_started_at = time.monotonic()
                        await send("answer.state.changed", state="answering")
                except AppError as exc:
                    await send(
                        "error",
                        code=exc.code,
                        message=exc.message,
                        recoverable=True,
                    )
            elif event_type == "answer.end":
                if not ready:
                    await send("error", code="NOT_READY", message="请先开始面试")
                    continue
                text = str(event.get("text") or "").strip()
                try:
                    if voice_session:
                        await voice_session.handle_answer_end(text)
                    else:
                        if (
                            not text_answer_protocol_enabled
                            or text_answer_started_at is None
                        ):
                            raise AppError(
                                "ANSWER_NOT_STARTED",
                                "请先点击“开始回答”",
                                status_code=409,
                            )
                        if not text:
                            # This attempt is over even though it contained no
                            # text.  A retry gets a fresh server-side clock
                            # instead of inheriting time from the empty attempt.
                            text_answer_started_at = None
                            raise AppError(
                                "EMPTY_ANSWER",
                                "请输入回答后再点击“结束回答”",
                                status_code=422,
                            )
                        duration = round(
                            max(
                                0.0,
                                min(
                                    time.monotonic() - text_answer_started_at,
                                    3600.0,
                                ),
                            ),
                            2,
                        )
                        text_answer_started_at = None
                        await send(
                            "answer.state.changed",
                            state="sealing",
                            elapsed_ms=int(duration * 1000),
                        )
                        track(
                            _handle_text_answer(
                                interview_id=interview_id,
                                answer=text,
                                answer_duration_seconds=duration,
                                send=send,
                                stop_event=ending,
                                on_end=finalize,
                            ),
                            answer_tasks,
                            f"text-answer-{interview_id}",
                        )
                except AppError as exc:
                    await send(
                        "error",
                        code=exc.code,
                        message=exc.message,
                        recoverable=exc.status_code < 500,
                    )
            elif event_type == "user.text":
                if not ready:
                    await send("error", code="NOT_READY", message="请先开始面试")
                    continue
                if ending.is_set():
                    await send(
                        "error",
                        code="INTERVIEW_ENDING",
                        message="本场面试正在结束",
                        recoverable=False,
                    )
                    continue
                text = str(event.get("text", "")).strip()
                legacy_duration = (
                    round(
                        max(
                            0.0,
                            min(time.monotonic() - text_answer_started_at, 3600.0),
                        ),
                        2,
                    )
                    if text_answer_started_at is not None
                    else None
                )
                text_answer_started_at = None
                if voice_session:
                    try:
                        await voice_session.handle_text(
                            text,
                            answer_duration_seconds=legacy_duration,
                        )
                    except AppError as exc:
                        await send(
                            "error",
                            code=exc.code,
                            message=exc.message,
                            recoverable=exc.code != "INTERVIEW_ENDED",
                        )
                else:
                    track(
                        _handle_text_answer(
                            interview_id=interview_id,
                            answer=text,
                            answer_duration_seconds=legacy_duration,
                            send=send,
                            stop_event=ending,
                            on_end=finalize,
                        ),
                        answer_tasks,
                        f"text-answer-{interview_id}",
                    )
            elif event_type == "candidate.transcript.correct":
                if not ready:
                    await send("error", code="NOT_READY", message="请先开始面试")
                    continue
                try:
                    correction = TranscriptCorrection.model_validate(event)
                    if voice_session:
                        await voice_session.handle_transcript_correction(
                            text=correction.text,
                            ordinal=correction.ordinal,
                            item_id=correction.item_id,
                        )
                    else:
                        if correction.ordinal is None:
                            raise AppError(
                                "TURN_ORDINAL_REQUIRED",
                                "文字模式修正转写时必须提供 ordinal",
                                status_code=422,
                            )
                        corrected = await interview_engine.correct_answer(
                            interview_id,
                            ordinal=correction.ordinal,
                            text=correction.text,
                        )
                        await send(
                            "candidate.transcript.corrected",
                            **corrected,
                            item_id=correction.item_id,
                        )
                except AppError as exc:
                    await send(
                        "error",
                        code=exc.code,
                        message=exc.message,
                        recoverable=exc.status_code < 500,
                    )
                except ValidationError:
                    await send(
                        "error",
                        code="INVALID_TRANSCRIPT_CORRECTION",
                        message="转写修正参数不正确",
                        recoverable=True,
                    )
            elif event_type == "interview.end":
                reason = str(event.get("reason") or "manual")
                if not ending.is_set() and not ended.is_set():
                    track(
                        terminate(reason),
                        control_tasks,
                        f"terminate-{interview_id}",
                    )
            elif event_type == "ping":
                await send("pong", timestamp=time.time())
            else:
                await send(
                    "error",
                    code="UNKNOWN_EVENT",
                    message=f"不支持的消息类型：{event_type}",
                    recoverable=True,
                )
    except WebSocketDisconnect:
        pass
    except AppError as exc:
        with suppress(Exception):
            await send("error", code=exc.code, message=exc.message)
    except Exception:
        with suppress(Exception):
            await send(
                "error", code="INTERNAL_ERROR", message="面试连接出现异常，请刷新后重试"
            )
    finally:
        if timer_task:
            timer_task.cancel()
            with suppress(asyncio.CancelledError):
                await timer_task
        pending_answers = [task for task in answer_tasks if not task.done()]
        for task in pending_answers:
            task.cancel()
        if pending_answers:
            await asyncio.gather(*pending_answers, return_exceptions=True)
        pending_controls = [task for task in control_tasks if not task.done()]
        if pending_controls:
            _, unfinished_controls = await asyncio.wait(
                pending_controls,
                timeout=WS_CONTROL_DRAIN_TIMEOUT_SECONDS,
            )
            # A disconnected browser cannot own the durable terminal commit.
            # The normal terminate path should reach this within its bounded
            # voice drain; this fallback covers an unexpectedly stuck provider.
            if ending.is_set() and not terminal_committed.is_set():
                with suppress(Exception):
                    await commit_terminal(requested_end_reason)
            for task in unfinished_controls:
                task.cancel()
            if unfinished_controls:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.gather(
                            *unfinished_controls,
                            return_exceptions=True,
                        ),
                        timeout=1,
                    )
        if voice_session:
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(
                    voice_session.close(),
                    timeout=VOICE_CLOSE_TIMEOUT_SECONDS,
                )


async def _handle_text_answer(
    *,
    interview_id: str,
    answer: str,
    answer_duration_seconds: float | None = None,
    send: Any,
    stop_event: asyncio.Event,
    on_end: Any,
) -> None:
    lock = session_locks.setdefault(interview_id, asyncio.Lock())
    if lock.locked():
        await send(
            "error", code="ANSWER_IN_PROGRESS", message="上一轮仍在生成，请稍候", recoverable=True
        )
        return
    async with lock:
        predicted_ordinal = len(await db.list_turns(interview_id)) + 1
        await send(
            "candidate.transcript.done",
            text=answer,
            source="text",
            ordinal=predicted_ordinal,
        )
        await send("interviewer.state", state="thinking")
        persisted = False
        try:
            result = await interview_engine.answer(
                interview_id,
                answer,
                input_mode="text",
                answer_duration_seconds=answer_duration_seconds,
            )
            persisted = True
        except asyncio.CancelledError:
            if not persisted:
                preserve_task = asyncio.create_task(
                    interview_engine.preserve_unscored_answer(
                        interview_id,
                        answer,
                        input_mode="text",
                        answer_duration_seconds=answer_duration_seconds,
                        ordinal=predicted_ordinal,
                    )
                )
                try:
                    await asyncio.shield(preserve_task)
                except asyncio.CancelledError:
                    # Finish the independent SQLite write before propagating
                    # cancellation; otherwise the report can race an empty DB.
                    with suppress(asyncio.CancelledError, Exception):
                        await asyncio.shield(preserve_task)
            raise
        except AppError as exc:
            if exc.code == "INTERVIEW_TIMEOUT":
                await on_end("time")
                return
            await send(
                "error",
                code=exc.code,
                message=exc.message,
                recoverable=exc.code != "INTERVIEW_ENDED",
            )
            if exc.code != "INTERVIEW_ENDED":
                await send("answer.state.changed", state="idle")
            return
        if stop_event.is_set():
            if result.ended:
                await on_end(result.end_reason or "poor_performance")
            return
        if result.silence_seconds:
            await send(
                "interviewer.state",
                state="silent",
                seconds=result.silence_seconds,
            )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=result.silence_seconds
                )
                return
            except asyncio.TimeoutError:
                pass
        await send(
            "interviewer.text.done",
            text=result.question,
            pressure_action=result.pressure_action,
            recommended_answer_seconds=result.recommended_answer_seconds,
        )
        if result.ended:
            await on_end(result.end_reason or "poor_performance")
        else:
            await send("answer.state.changed", state="idle")
            await send("interviewer.state", state="listening")


def _spawn_report(
    interview_id: str,
    *,
    websocket: WebSocket | None = None,
    send_lock: asyncio.Lock | None = None,
) -> None:
    async def work() -> None:
        if websocket and send_lock:
            with suppress(Exception):
                async with send_lock:
                    await websocket.send_json(
                        {"type": "report.generating", "interview_id": interview_id}
                    )
        try:
            report = await report_engine.generate(interview_id)
        except Exception:
            return
        if websocket and send_lock:
            with suppress(Exception):
                async with send_lock:
                    await websocket.send_json(
                        {
                            "type": "report.ready",
                            "interview_id": interview_id,
                            "report_id": report.report_id,
                        }
                    )

    task = asyncio.create_task(work())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def _require_interview(interview_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", interview_id):
        raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
    interview = await db.get_interview(interview_id)
    if not interview:
        raise AppError("INTERVIEW_NOT_FOUND", "面试不存在", status_code=404)
    return interview


def _public_interview(interview: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: interview.get(key)
        for key in (
            "id",
            "company",
            "role",
            "interview_type",
            "specialization",
            "language_mode",
            "stress",
            "stress_level",
            "duration_minutes",
            "memory_enabled",
            "voice_mode",
            "status",
            "weak_topics",
            "last_question",
            "hint_count",
            "hint_events",
            "created_at",
            "started_at",
            "deadline_at",
            "remaining_seconds",
            "ended_at",
            "end_reason",
        )
    }
    result["recommended_answer_seconds"] = interview_engine.recommended_answer_seconds(
        str(interview.get("last_question") or "")
    )
    return result


def _validate_client_id(client_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", client_id):
        raise AppError("INVALID_CLIENT_ID", "client_id 格式不正确", status_code=422)


PUBLIC_DIR = ROOT_DIR / "public"
for directory in ("assets", "js", "worklets"):
    path = PUBLIC_DIR / directory
    path.mkdir(parents=True, exist_ok=True)
    app.mount(f"/{directory}", StaticFiles(directory=path), name=directory)


@app.get("/", include_in_schema=False)
async def home_page() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "index.html")


@app.get("/interview", include_in_schema=False)
async def interview_page() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "interview.html")


@app.get("/report", include_in_schema=False)
async def report_page() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "report.html")


@app.get("/practice", include_in_schema=False)
async def practice_page() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "practice.html")


@app.get("/project", include_in_schema=False)
async def project_page() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "project.html")
