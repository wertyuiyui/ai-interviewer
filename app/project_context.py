from __future__ import annotations

from typing import Any

from .errors import AppError
from .profile import ProfileProjectAnalysisRequest, ProfileService
from .schemas import InterviewCreate, Project


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:limit]


def _unique(values: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _clean(raw, limit)
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _fit_highlights(values: list[str], total_limit: int = 6000) -> list[str]:
    result: list[str] = []
    used = 0
    for value in _unique(values, 1600):
        remaining = total_limit - used
        if remaining <= 0:
            break
        fitted = value[:remaining]
        if fitted:
            result.append(fitted)
            used += len(fitted)
    return result


async def enrich_interview_with_profile_project(
    request: InterviewCreate,
    service: ProfileService,
) -> InterviewCreate:
    """Resolve and snapshot one selected project for prompts and later reports.

    Ownership is checked inside ``ProfileService``. The snapshot contains only
    structured analysis, never executable files or raw repository responses.
    It is persisted inside the interview's resume JSON so a later report remains
    reproducible even if the user manually deletes the Profile project.
    """

    project_id = request.profile_project_id
    if not project_id:
        return request

    project = await service.get_project(project_id, request.client_id)
    try:
        analyzed = await service.analyze_project(
            project_id,
            ProfileProjectAnalysisRequest(client_id=request.client_id, refresh=False),
        )
        analysis = analyzed.get("analysis")
        if not isinstance(analysis, dict):
            analysis = {}
    except AppError:
        # Project analysis is an optional enhancement. Ownership has already
        # been verified above, so provider/model/content failures degrade to a
        # metadata-only snapshot and never take down the interview baseline.
        paths = [
            _clean(item.get("path"), 240)
            for item in project.get("files", [])[:20]
            if isinstance(item, dict)
        ]
        analysis = {
            "project_summary": (
                f"候选人提交了项目资料；当前自动解读不可用。可用文件包括："
                f"{', '.join(path for path in paths if path) or '未提供可读取文件'}。"
                "面试中必须由候选人说明架构、个人职责和实际指标。"
            ),
            "architecture": [],
            "technology_choices": [],
            "interview_questions": [],
        }

    technologies = _unique(
        [
            str(item.get("technology") or "")
            for item in analysis.get("technology_choices", [])
            if isinstance(item, dict)
        ],
        160,
    )[:20]
    highlights: list[str] = []
    summary = _clean(analysis.get("project_summary"), 1800)
    if summary:
        highlights.append(f"项目材料摘要（个人职责仍需面试核实）：{summary}")

    for item in analysis.get("architecture", [])[:8]:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"), 120)
        responsibility = _clean(item.get("responsibility"), 700)
        if name or responsibility:
            highlights.append(f"架构模块：{name or '未命名模块'}；{responsibility}")

    for item in analysis.get("technology_choices", [])[:8]:
        if not isinstance(item, dict):
            continue
        technology = _clean(item.get("technology"), 120)
        purpose = _clean(item.get("purpose"), 500)
        tradeoffs = _clean(item.get("tradeoffs"), 700)
        if technology or purpose or tradeoffs:
            highlights.append(
                f"技术选型材料：{technology or '未命名技术'}；用途：{purpose or '待核实'}；"
                f"取舍：{tradeoffs or '待核实'}"
            )

    for item in analysis.get("interview_questions", [])[:6]:
        if not isinstance(item, dict):
            continue
        question = _clean(item.get("question"), 500)
        focus = _clean(item.get("focus"), 700)
        if question:
            highlights.append(
                f"项目材料建议追问：{question}；考察重点：{focus or '结合候选人真实贡献、证据与取舍核实'}"
            )

    uploaded_project = Project(
        name=f"[匿名 Profile 项目] {_clean(project.get('name'), 120) or '未命名项目'}",
        role="候选人提交的只读项目材料；AI 结论不是候选人自述，个人职责与指标必须当场核实",
        technologies=technologies,
        highlights=_fit_highlights(highlights),
        metrics=[],
    )
    resume = request.resume.model_copy(
        update={"projects": [*request.resume.projects, uploaded_project]}
    )
    return request.model_copy(update={"resume": resume})
