from __future__ import annotations

from typing import Any

from .errors import AppError
from .profile import ProfileProjectAnalysisRequest, ProfileService
from .prompt_engine import is_internal_interview_instruction
from .schemas import InterviewCreate, Project


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:limit]


def _safe_analysis_text(value: Any, limit: int) -> str:
    """Keep project facts while dropping prompt/skill prose from uploaded docs."""

    text = _clean(value, limit)
    return "" if is_internal_interview_instruction(text) else text


def _evidence_copy(item: dict[str, Any], limit: int = 4) -> str:
    raw_evidence = item.get("evidence")
    if not isinstance(raw_evidence, list):
        raw_evidence = []
    evidence = _unique(
        [str(value) for value in raw_evidence if str(value).strip()],
        240,
    )[:limit]
    return f"；证据引用：{', '.join(evidence)}" if evidence else "；代码证据待核实"


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
                "架构、个人职责和实际指标目前均缺少自动分析证据。"
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
    technologies = [
        value for value in technologies if not is_internal_interview_instruction(value)
    ]
    highlights: list[str] = []
    responsibility = _safe_analysis_text(project.get("responsibility"), 1200)
    if responsibility:
        highlights.append(f"候选人填写的负责范围（待面试核实）：{responsibility}")

    summary = _safe_analysis_text(analysis.get("project_summary"), 1800)
    if summary:
        highlights.append(f"项目材料摘要（个人职责仍需面试核实）：{summary}")

    for item in analysis.get("architecture", [])[:8]:
        if not isinstance(item, dict):
            continue
        name = _safe_analysis_text(item.get("name"), 120)
        component_purpose = _safe_analysis_text(item.get("responsibility"), 700)
        if name or component_purpose:
            highlights.append(
                f"架构模块（自动分析，需核实）：{name or '未命名模块'}；"
                f"作用：{component_purpose or '材料未说明'}{_evidence_copy(item)}"
            )

    request_steps: list[tuple[int, int, dict[str, Any]]] = []
    for source_index, item in enumerate(analysis.get("request_flow", [])[:20]):
        if not isinstance(item, dict):
            continue
        try:
            step = max(1, min(int(item.get("step")), 100))
        except (TypeError, ValueError):
            step = 100
        request_steps.append((step, source_index, item))
    request_steps.sort(key=lambda value: (value[0], value[1]))
    for step, _source_index, item in request_steps:
        component = _safe_analysis_text(item.get("component"), 160)
        action = _safe_analysis_text(item.get("action"), 700)
        if component or action:
            highlights.append(
                f"请求链路第 {step} 步（自动分析，需核实）："
                f"{component or '未命名组件'}；动作：{action or '材料未说明'}"
                f"{_evidence_copy(item)}"
            )

    review = analysis.get("request_flow_review")
    if isinstance(review, dict):
        status = str(review.get("status") or "needs_verification").strip()
        if status not in {"verified", "partial", "needs_verification"}:
            status = "needs_verification"
        summary_copy = _safe_analysis_text(review.get("summary"), 900)
        boundary_parts: list[str] = []
        for field, label in (
            ("issues", "已识别问题"),
            ("assumptions", "自动分析假设"),
            ("to_verify", "仍待核实"),
        ):
            raw_values = review.get(field)
            if not isinstance(raw_values, list):
                raw_values = []
            values = [
                safe
                for raw in raw_values[:5]
                if (safe := _safe_analysis_text(raw, 360))
            ]
            if values:
                boundary_parts.append(f"{label}：{'；'.join(values)}")
        review_copy = "；".join(
            value for value in (summary_copy, *boundary_parts) if value
        )
        if review_copy:
            highlights.append(
                f"请求链路证据边界（自动检查={status}，候选人尚未确认）："
                f"{review_copy}"
            )

    for item in analysis.get("technology_choices", [])[:8]:
        if not isinstance(item, dict):
            continue
        technology = _safe_analysis_text(item.get("technology"), 120)
        purpose = _safe_analysis_text(item.get("purpose"), 500)
        tradeoffs = _safe_analysis_text(item.get("tradeoffs"), 700)
        if technology or purpose or tradeoffs:
            highlights.append(
                f"技术选型材料：{technology or '未命名技术'}；用途：{purpose or '待核实'}；"
                f"取舍：{tradeoffs or '待核实'}{_evidence_copy(item)}"
            )

    for item in analysis.get("risks", [])[:5]:
        if not isinstance(item, dict):
            continue
        risk = _safe_analysis_text(item.get("risk"), 500)
        impact = _safe_analysis_text(item.get("impact"), 500)
        if risk or impact:
            highlights.append(
                f"风险材料（自动分析，需核实）：{risk or '未命名风险'}；"
                f"影响：{impact or '待核实'}{_evidence_copy(item)}"
            )

    profile_name = _safe_analysis_text(project.get("name"), 120) or "未命名项目"
    uploaded_project = Project(
        name=f"[匿名 Profile 项目] {profile_name}",
        role=responsibility or "负责范围待候选人当场核实",
        technologies=technologies,
        highlights=_fit_highlights(highlights),
        metrics=[],
    )
    resume = request.resume.model_copy(
        # An explicitly selected Profile project is the intended drill target,
        # so keep it ahead of projects parsed from the resume.
        update={"projects": [uploaded_project, *request.resume.projects]}
    )
    return request.model_copy(update={"resume": resume})
