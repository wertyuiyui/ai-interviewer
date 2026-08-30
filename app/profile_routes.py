from __future__ import annotations

import asyncio
import hmac
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, File, Form, Header, Request, UploadFile
from pydantic import ValidationError

from .config import Settings
from .errors import AppError
from .profile import (
    MAX_DIRECT_FILE_BYTES,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_ITEMS,
    ProfileGitHubProjectCreate,
    ProfileProjectAnalysisRequest,
    ProfileProjectCreate,
    ProfileProjectSelection,
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
            if raw_total > MAX_UPLOAD_BYTES:
                raise AppError(
                    "PROJECT_UPLOAD_TOO_LARGE",
                    f"一次上传总大小不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
                    status_code=413,
                )
            uploads.append(upload)
        try:
            project_request = ProfileProjectCreate(client_id=client_id, name=name)
        except ValidationError as exc:
            raise _model_validation_error(exc) from exc
        project = await service_provider().create_uploaded_project(project_request, uploads)
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
