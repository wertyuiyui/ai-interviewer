from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, File, Form, Header, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from .config import Settings
from .errors import AppError
from .profile import (
    MAX_DIRECT_FILE_BYTES,
    MAX_PAPER_PDF_BYTES,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_ITEMS,
    ProfileGitHubProjectCreate,
    ProfileLinkedProjectCreate,
    ProfileProjectAnalysisRequest,
    ProfileProjectCreate,
    ProfileProjectQuestionsRequest,
    ProfileProjectSelection,
    ProfileProjectUpdate,
    ProfileResumeCreate,
    ProfileService,
    ProjectUpload,
    clean_client_id,
    read_upload_limited,
)


class _ProfileLimiter:
    """Small in-process limiter for upload, fetch and paid analysis endpoints."""

    def __init__(self) -> None:
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str, limit: int, window_seconds: int = 3600) -> bool:
        now = time.monotonic()
        async with self._lock:
            bucket = self._entries[key]
            while bucket and bucket[0] <= now - window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True


def _host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _model_validation_error(exc: ValidationError) -> AppError:
    return AppError(
        "VALIDATION_ERROR",
        "匿名档案请求参数不正确",
        status_code=422,
        details={"errors": exc.errors(include_input=False, include_url=False)},
    )


def _verify_profile_key(profile_key: str, client_id: str | None = None) -> str:
    try:
        normalized = clean_client_id(profile_key)
    except ValueError as exc:
        raise AppError(
            "INVALID_PROFILE_KEY",
            "匿名档案密钥格式不正确",
            status_code=422,
        ) from exc
    if len(normalized) < 24 or len(normalized) > 128:
        raise AppError(
            "INVALID_PROFILE_KEY",
            "匿名档案密钥格式不正确",
            status_code=422,
        )
    if client_id is not None and not hmac.compare_digest(normalized, client_id):
        raise AppError(
            "PROFILE_KEY_MISMATCH",
            "匿名档案密钥不匹配",
            status_code=403,
        )
    return normalized


def create_profile_router(
    service_provider: Callable[[], ProfileService],
    settings_provider: Callable[[], Settings],
) -> APIRouter:
    router = APIRouter(prefix="/api/profile", tags=["profile"])
    limiter = _ProfileLimiter()

    async def require_budget(
        request: Request,
        *,
        action: str,
        client_id: str,
        host_limit: int,
        client_limit: int,
    ) -> None:
        host_allowed = await limiter.allow(
            f"{action}:host:{_host(request)}", host_limit
        )
        client_allowed = await limiter.allow(
            f"{action}:client:{client_id}", client_limit
        )
        if not host_allowed or not client_allowed:
            raise AppError(
                "PROFILE_RATE_LIMIT",
                "匿名档案操作过于频繁，请稍后再试",
                status_code=429,
            )

    @router.get("")
    async def get_profile(
        http_request: Request,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, Any]:
        client_id = _verify_profile_key(profile_key)
        await require_budget(
            http_request,
            action="profile-read",
            client_id=client_id,
            host_limit=240,
            client_limit=120,
        )
        return await service_provider().get_profile(client_id)

    @router.post("/resumes", status_code=201)
    async def create_resume(
        http_request: Request,
        client_id: str = Form(min_length=8, max_length=128),
        name: str = Form(min_length=1, max_length=120),
        file: UploadFile = File(...),
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, Any]:
        _verify_profile_key(profile_key, client_id)
        await require_budget(
            http_request,
            action="profile-resume",
            client_id=client_id,
            host_limit=24,
            client_limit=20,
        )
        filename = str(file.filename or "")
        suffix = Path(filename).suffix.casefold()
        max_bytes = (
            settings_provider().max_pdf_mb * 1024 * 1024
            if suffix == ".pdf"
            else MAX_DIRECT_FILE_BYTES
        )
        content = await read_upload_limited(
            file,
            max_bytes=max_bytes,
            code="RESUME_FILE_TOO_LARGE",
            message="简历文件超过允许大小",
        )
        try:
            resume = await service_provider().create_resume_upload(
                client_id=client_id,
                name=name,
                filename=filename,
                content=content,
            )
        except ValidationError as exc:
            raise _model_validation_error(exc) from exc
        return {"resume": resume}

    @router.post("/resumes/text", status_code=201)
    async def create_text_resume(
        request: ProfileResumeCreate,
        http_request: Request,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, Any]:
        _verify_profile_key(profile_key, request.client_id)
        await require_budget(
            http_request,
            action="profile-resume",
            client_id=request.client_id,
            host_limit=24,
            client_limit=20,
        )
        if not request.text:
            raise AppError(
                "RESUME_TEXT_EMPTY",
                "请先粘贴简历文字",
                status_code=422,
            )
        normalized = ProfileResumeCreate(
            client_id=request.client_id,
            name=request.name,
            text=request.text,
            source_type="text",
        )
        return {"resume": await service_provider().create_resume(normalized)}

    @router.delete("/resumes/{resume_id}")
    async def delete_resume(
        resume_id: str,
        http_request: Request,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, bool]:
        client_id = _verify_profile_key(profile_key)
        await require_budget(
            http_request,
            action="profile-delete",
            client_id=client_id,
            host_limit=120,
            client_limit=60,
        )
        await service_provider().delete_resume(resume_id, client_id)
        return {"deleted": True}

    @router.post("/projects", status_code=201)
    async def create_uploaded_project(
        http_request: Request,
        client_id: str = Form(min_length=8, max_length=128),
        name: str = Form(min_length=1, max_length=120),
        project_type: str = Form(default="application"),
        responsibility_scope: str = Form(default="all"),
        responsibility: str = Form(default="", max_length=4_000),
        files: list[UploadFile] = File(...),
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, Any]:
        _verify_profile_key(profile_key, client_id)
        await require_budget(
            http_request,
            action="profile-project-upload",
            client_id=client_id,
            host_limit=30,
            client_limit=20,
        )
        if len(files) > MAX_UPLOAD_ITEMS:
            raise AppError(
                "PROJECT_UPLOAD_LIMIT",
                f"一次最多上传 {MAX_UPLOAD_ITEMS} 个文件",
                status_code=413,
            )
        uploads: list[ProjectUpload] = []
        raw_total = 0
        for item in files:
            upload = await ProjectUpload.from_async_upload(item)
            raw_total += len(upload.content)
            raw_limit = MAX_PAPER_PDF_BYTES if project_type == "paper" else MAX_UPLOAD_BYTES
            if raw_total > raw_limit:
                raise AppError(
                    "PROJECT_UPLOAD_TOO_LARGE",
                    f"一次上传总大小不能超过 {raw_limit // (1024 * 1024)} MB",
                    status_code=413,
                )
            uploads.append(upload)
        try:
            project_request = ProfileProjectCreate(
                client_id=client_id,
                name=name,
                project_type=project_type,
                responsibility_scope=responsibility_scope,
                responsibility=responsibility,
            )
        except ValidationError as exc:
            raise _model_validation_error(exc) from exc
        project = await service_provider().create_uploaded_project(project_request, uploads)
        return {"project": project}

    @router.post("/projects/links", status_code=201)
    async def create_linked_project(
        request: ProfileLinkedProjectCreate,
        http_request: Request,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, Any]:
        _verify_profile_key(profile_key, request.client_id)
        await require_budget(
            http_request,
            action="profile-linked-project",
            client_id=request.client_id,
            host_limit=16,
            client_limit=8,
        )
        project = await service_provider().create_linked_project(request)
        return {"project": project}

    @router.post("/projects/github", status_code=201)
    async def create_github_project(
        request: ProfileGitHubProjectCreate,
        http_request: Request,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, Any]:
        _verify_profile_key(profile_key, request.client_id)
        await require_budget(
            http_request,
            action="profile-github",
            client_id=request.client_id,
            host_limit=16,
            client_limit=8,
        )
        project = await service_provider().create_github_project(request, fetch=True)
        return {"project": project}

    @router.post("/projects/{project_id}/refresh")
    async def refresh_github_project(
        project_id: str,
        request: ProfileProjectAnalysisRequest,
        http_request: Request,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, Any]:
        _verify_profile_key(profile_key, request.client_id)
        await require_budget(
            http_request,
            action="profile-github",
            client_id=request.client_id,
            host_limit=16,
            client_limit=8,
        )
        project = await service_provider().refresh_github_project(project_id, request.client_id)
        return {"project": project}

    @router.patch("/projects/{project_id}/selection")
    async def select_project(
        project_id: str,
        request: ProfileProjectSelection,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, Any]:
        _verify_profile_key(profile_key, request.client_id)
        return await service_provider().select_project(project_id, request)

    @router.patch("/projects/{project_id}")
    async def update_project(
        project_id: str,
        request: ProfileProjectUpdate,
        http_request: Request,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, Any]:
        _verify_profile_key(profile_key, request.client_id)
        await require_budget(
            http_request,
            action="profile-project-update",
            client_id=request.client_id,
            host_limit=120,
            client_limit=60,
        )
        return {"project": await service_provider().update_project(project_id, request)}

    @router.post("/projects/{project_id}/analysis")
    async def analyze_project(
        project_id: str,
        request: ProfileProjectAnalysisRequest,
        http_request: Request,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, Any]:
        _verify_profile_key(profile_key, request.client_id)
        await require_budget(
            http_request,
            action="profile-analysis",
            client_id=request.client_id,
            host_limit=20,
            client_limit=10,
        )
        return await service_provider().analyze_project(project_id, request)

    @router.post("/projects/{project_id}/analysis/stream")
    async def analyze_project_stream(
        project_id: str,
        request: ProfileProjectAnalysisRequest,
        http_request: Request,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> StreamingResponse:
        _verify_profile_key(profile_key, request.client_id)
        await require_budget(
            http_request,
            action="profile-analysis",
            client_id=request.client_id,
            host_limit=20,
            client_limit=10,
        )

        async def events():
            queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=16)

            async def emit(event: dict[str, Any]) -> None:
                await queue.put(event)

            async def produce() -> None:
                try:
                    result = await service_provider().analyze_project(
                        project_id,
                        request,
                        progress=emit,
                    )
                except AppError as exc:
                    await queue.put(
                        {
                            "type": "error",
                            "error": {
                                "code": exc.code,
                                "message": exc.message,
                                "details": exc.details,
                            },
                        }
                    )
                except Exception:
                    await queue.put(
                        {
                            "type": "error",
                            "error": {
                                "code": "PROJECT_ANALYSIS_FAILED",
                                "message": "项目解读失败，请稍后重试",
                                "details": {},
                            },
                        }
                    )
                else:
                    await queue.put(
                        {
                            "type": "complete",
                            "progress": 100,
                            "result": result,
                        }
                    )
                finally:
                    await queue.put(None)

            task = asyncio.create_task(produce())
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            finally:
                # Do not cancel a paid model call merely because a browser tab
                # closed.  Let it finish and populate the cache for the retry.
                if task.done():
                    task.result()

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/projects/{project_id}/questions")
    async def generate_project_questions(
        project_id: str,
        request: ProfileProjectQuestionsRequest,
        http_request: Request,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, Any]:
        _verify_profile_key(profile_key, request.client_id)
        await require_budget(
            http_request,
            action="profile-project-questions",
            client_id=request.client_id,
            host_limit=20,
            client_limit=10,
        )
        return await service_provider().generate_project_questions(project_id, request)

    @router.delete("/projects/{project_id}")
    async def delete_project(
        project_id: str,
        http_request: Request,
        profile_key: str = Header(
            alias="X-Profile-Key", min_length=24, max_length=128
        ),
    ) -> dict[str, bool]:
        client_id = _verify_profile_key(profile_key)
        await require_budget(
            http_request,
            action="profile-delete",
            client_id=client_id,
            host_limit=120,
            client_limit=60,
        )
        await service_provider().delete_project(project_id, client_id)
        return {"deleted": True}

    return router
