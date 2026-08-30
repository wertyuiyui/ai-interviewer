from __future__ import annotations

import asyncio
from collections import defaultdict, deque
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

from .config import ROOT_DIR, Settings, get_settings
from .content import COMPANIES, SPECIALIZATIONS, load_style_card
from .db import Database
from .errors import AppError
from .interview_engine import InterviewEngine
from .report_engine import ReportEngine
from .resume import ResumeParser, extract_pdf_text
from .schemas import InterviewCreate, InterviewFinish
from .voice_session import BrowserVoiceSession


settings: Settings = get_settings()
db = Database(settings)
resume_parser = ResumeParser(settings)
interview_engine = InterviewEngine(db, settings)
report_engine = ReportEngine(db, settings)
background_tasks: set[asyncio.Task[Any]] = set()
session_locks: dict[str, asyncio.Lock] = {}


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


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await db.initialize()
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

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
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
        companies.append(
            {
                "id": key,
                "name": label,
                "role": "后端开发实习生",
                "stress_default": bool(card.get("stress_default", False)),
                "preferences": card.get("followup_preferences", []),
            }
        )
    return {
        "app_name": settings.app_name,
        "voice_mode": settings.voice_mode,
        "voice_auto_fallback": settings.voice_auto_fallback,
        "llm_configured": settings.has_api_key or settings.mock_llm,
        "mock_mode": settings.mock_llm,
        "companies": companies,
        "specializations": SPECIALIZATIONS,
        "durations": [10, 15, 25],
        "custom_duration": {"min": 1, "max": 180, "unlimited": True},
        "stress_levels": [
            {"level": 0, "name": "关闭"},
            {"level": 1, "name": "温和"},
            {"level": 2, "name": "标准"},
            {"level": 3, "name": "高压"},
        ],
        "references": [
            {
                "name": "ARIS-in-AI-Offer",
                "url": "https://wanshuiyin.github.io/ARIS-in-AI-Offer/",
                "license": "MIT",
                "scope": "AI 工程后端 / LLM Infra 精选题",
            }
        ],
        "daily_interview_limit": settings.daily_interview_limit,
    }


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
async def create_interview(request: InterviewCreate) -> dict[str, Any]:
    return await interview_engine.create(request)


@app.get("/api/interviews/{interview_id}")
async def get_interview(interview_id: str) -> dict[str, Any]:
    interview = await _require_interview(interview_id)
    return _public_interview(interview)


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
    terminal_lock = asyncio.Lock()
    timer_task: asyncio.Task[None] | None = None
    voice_session: BrowserVoiceSession | None = None
    answer_tasks: set[asyncio.Task[None]] = set()
    control_tasks: set[asyncio.Task[None]] = set()
    ready = False

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

    async def finalize(reason: str) -> None:
        async with terminal_lock:
            if ended.is_set():
                return
            ending.set()
            await cancel_answers()
            ended.set()
            with suppress(Exception):
                await send("interview.ended", reason=reason)
            _spawn_report(interview_id, websocket=websocket, send_lock=send_lock)

    async def terminate(reason: str) -> None:
        nonlocal voice_session
        async with terminal_lock:
            if ended.is_set() or ending.is_set():
                return
            ending.set()
            await cancel_answers()
            if voice_session:
                await voice_session.prepare_end()
            reason = reason if reason in {"time", "manual"} else "manual"
            await db.finish_interview(interview_id, reason)
            closing = (
                "时间到了，今天的面试就到这里，感谢你的时间。"
                if reason == "time"
                else "好的，今天的面试就到这里，感谢你的时间。"
            )
            with suppress(Exception):
                await send("interviewer.text.done", text=closing)
            if voice_session:
                with suppress(Exception):
                    await voice_session.announce(
                        closing,
                        cancel_current=True,
                        wait_for_playback=True,
                    )
            ended.set()
            with suppress(Exception):
                await send("interview.ended", reason=reason)
            _spawn_report(interview_id, websocket=websocket, send_lock=send_lock)

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
                    "interviewer.text.done", text=interview["last_question"]
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
                timer_task = asyncio.create_task(timer_loop())
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
                if voice_session:
                    try:
                        await voice_session.handle_text(text)
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
                            send=send,
                            stop_event=ending,
                            on_end=finalize,
                        ),
                        answer_tasks,
                        f"text-answer-{interview_id}",
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
        ended.set()
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
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*pending_controls, return_exceptions=True),
                    timeout=15,
                )
        if voice_session:
            await voice_session.close()


async def _handle_text_answer(
    *,
    interview_id: str,
    answer: str,
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
        await send("candidate.transcript.done", text=answer)
        await send("interviewer.state", state="thinking")
        try:
            result = await interview_engine.answer(interview_id, answer)
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
        )
        if result.ended:
            await on_end(result.end_reason or "poor_performance")
        else:
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
    return {
        key: interview.get(key)
        for key in (
            "id",
            "company",
            "role",
            "specialization",
            "stress",
            "stress_level",
            "duration_minutes",
            "voice_mode",
            "status",
            "weak_topics",
            "created_at",
            "started_at",
            "deadline_at",
            "remaining_seconds",
            "ended_at",
            "end_reason",
        )
    }


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
