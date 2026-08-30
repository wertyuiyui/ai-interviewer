from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Education(BaseModel):
    model_config = ConfigDict(extra="ignore")

    school: str = ""
    degree: str = ""
    major: str = ""
    period: str = ""
    details: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company: str = ""
    role: str = ""
    period: str = ""
    highlights: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    role: str = ""
    technologies: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)


class ResumeData(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    education: list[Education] = Field(default_factory=list, alias="教育")
    internships: list[Experience] = Field(default_factory=list, alias="实习经历")
    projects: list[Project] = Field(default_factory=list, alias="项目")
    skills: list[str] = Field(default_factory=list, alias="技能")


Company = Literal["bytedance", "meituan", "tencent"]


class InterviewCreate(BaseModel):
    client_id: str = Field(min_length=8, max_length=128)
    resume: ResumeData
    company: Company
    role: Literal["backend"] = "backend"
    specialization: str = Field(default="通用后端", min_length=1, max_length=80)
    language_mode: Literal["zh", "bilingual"] = "bilingual"
    # ``stress`` is retained as a compatibility alias for older clients. New
    # clients should send stress_level; when both are present, stress_level wins.
    stress: bool = False
    stress_level: int = Field(default=0, ge=0, le=3, strict=True)
    duration_minutes: int | None = Field(default=15, ge=1, le=180, strict=True)
    # Reports are always available. This switch only controls whether this
    # interview reads and contributes weak topics for later interview scripts.
    memory_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_stress(cls, raw: Any) -> Any:
        if not isinstance(raw, dict) or "stress_level" in raw:
            return raw
        values = dict(raw)
        legacy = values.get("stress", False)
        enabled = legacy is True or legacy == 1 or (
            isinstance(legacy, str)
            and legacy.strip().lower() in {"true", "1", "yes", "on"}
        )
        values["stress_level"] = 2 if enabled else 0
        return values

    @model_validator(mode="after")
    def synchronize_legacy_stress(self) -> "InterviewCreate":
        self.stress = self.stress_level > 0
        return self

    @field_validator("client_id")
    @classmethod
    def clean_client_id(cls, value: str) -> str:
        value = value.strip()
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not value or any(char not in allowed for char in value):
            raise ValueError("client_id 格式不正确")
        return value

    @field_validator("specialization")
    @classmethod
    def clean_specialization(cls, value: str) -> str:
        value = " ".join(value.strip().split())
        if not value:
            raise ValueError("岗位细分方向不能为空")
        return value


class InterviewFinish(BaseModel):
    reason: Literal["manual", "time"] = "manual"


class InterviewRetry(BaseModel):
    client_id: str = Field(min_length=8, max_length=128)

    @field_validator("client_id")
    @classmethod
    def clean_client_id(cls, value: str) -> str:
        value = value.strip()
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not value or any(char not in allowed for char in value):
            raise ValueError("client_id 格式不正确")
        return value


class TurnAssessment(BaseModel):
    score: float = Field(default=5, ge=0, le=10)
    failed: bool = False
    dimension: Literal[
        "project_depth", "fundamentals", "coding_thought", "communication"
    ] = "communication"
    topic: str = "表达逻辑"
    deductions: list[str] = Field(default_factory=list)


class TurnDecision(BaseModel):
    next_question: str
    assessment: TurnAssessment = Field(default_factory=TurnAssessment)
    pressure_action: Literal[
        "none", "chain", "challenge", "interrupt", "silence"
    ] = "none"
    drill_dimension: Literal[
        "业务背景",
        "个人职责",
        "请求链路",
        "技术选型理由",
        "难点与故障",
        "数据指标口径",
        "边界与trade-off",
        "基础知识",
        "手撕思路",
    ] = "基础知识"
    drill_depth: int = Field(default=0, ge=0, le=7)
    anchor_keyword: str = ""
    should_end: bool = False


class InterviewTurn(BaseModel):
    ordinal: int
    question: str
    answer: str
    category: str
    topic: str
    score: float = Field(ge=0, le=10)
    deductions: list[str] = Field(default_factory=list)
    failed: bool = False
    drill_dimension: str = ""
    drill_depth: int = 0
    anchor_keyword: str = ""
    created_at: str = Field(default_factory=utc_now_iso)


class RubricDimension(BaseModel):
    score: float = Field(ge=0, le=10)
    weight: float = Field(ge=0, le=1)
    deductions: list[str] = Field(default_factory=list)


class Rubric(BaseModel):
    project_depth: RubricDimension
    fundamentals: RubricDimension
    coding_thought: RubricDimension
    communication: RubricDimension


class QuestionFeedback(BaseModel):
    question: str
    answer: str
    category: str
    score: float = Field(ge=0, le=10)
    deductions: list[str] = Field(min_length=1)
    better_answer: str = Field(min_length=1)


class PracticeItem(BaseModel):
    topic: str
    reason: str
    resource_title: Literal["JavaGuide", "CodeTop"]
    resource_url: str


class HintEvent(BaseModel):
    ordinal: int = Field(ge=1)
    question: str
    hint: str
    created_at: str = Field(default_factory=utc_now_iso)


class InterviewReport(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: str
    interview_id: str
    generated_at: str = Field(default_factory=utc_now_iso)
    company: Company
    # A report can exist before the candidate has produced any usable answer.
    # Keep that state distinct from a genuine numeric score so API consumers do
    # not mistake a placeholder rubric for performance evidence.
    scored: bool = True
    score_status: Literal["scored", "insufficient_data"] = "scored"
    overall_score: float = Field(ge=0, le=10)
    rubric: Rubric
    question_feedback: list[QuestionFeedback]
    topic_scores: dict[str, float]
    must_practice: list[PracticeItem]
    summary: str
    next_focus: list[str]
    comparison: dict[str, Any] = Field(default_factory=dict)
    memory_enabled: bool = True
    hint_count: int = Field(default=0, ge=0)
    hint_events: list[HintEvent] = Field(default_factory=list)


class ApiError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
