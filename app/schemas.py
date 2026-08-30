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


Company = str
InterviewType = Literal["technical", "hr", "technical_hr"]


def normalize_interview_type(value: Any) -> str:
    """Normalize the one legacy combined-interview spelling.

    ``tech_hr`` was used by an early client build.  Keep accepting it at API
    boundaries and when reading old SQLite rows, but expose and persist only
    the canonical ``technical_hr`` spelling.
    """

    normalized = str(value or "technical").strip().lower()
    if normalized == "tech_hr":
        return "technical_hr"
    return normalized


class InterviewCreate(BaseModel):
    client_id: str = Field(min_length=8, max_length=128)
    resume: ResumeData
    # Optional anonymous-Profile project selected on the home page. The HTTP
    # adapter resolves ownership and snapshots its structured analysis into the
    # resume before the interview engine persists the session.
    profile_project_id: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{32}$"
    )
    company: Company
    role: Literal["backend"] = "backend"
    # Keep the original technical interview as the compatibility default.
    interview_type: InterviewType = "technical"
    specialization: str = Field(default="通用后端", min_length=1, max_length=80)
    language_mode: Literal["zh", "bilingual", "en"] = "bilingual"
    # Candidate input is selected per interview. Text sessions deliberately
    # reuse the durable L3 path so reconnects never start microphone capture.
    answer_mode: Literal["voice", "text"] = "voice"
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

    @field_validator("interview_type", mode="before")
    @classmethod
    def migrate_legacy_interview_type(cls, value: Any) -> str:
        return normalize_interview_type(value)

    @field_validator("company")
    @classmethod
    def validate_company(cls, value: str) -> str:
        # Import lazily to keep the data-only schema module free of an eager
        # dependency cycle while still rejecting arbitrary filenames/slugs.
        from .content import COMPANIES

        normalized = str(value or "").strip().lower()
        if normalized not in COMPANIES:
            raise ValueError("暂不支持该公司")
        return normalized

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
    # A missing model score must fail schema validation and enter the explicit
    # evidence-based fallback.  A neutral-looking midpoint is not a safe
    # default because it is indistinguishable from a genuine 5/10 assessment.
    score: float | None = Field(ge=0, le=10)
    scorable: bool = True
    score_source: Literal["llm", "mock", "unavailable"] = "llm"
    failed: bool = False
    dimension: Literal[
        "project_depth", "fundamentals", "coding_thought", "communication"
    ] = "communication"
    topic: str = "表达逻辑"
    deductions: list[str] = Field(default_factory=list)


class TurnDecision(BaseModel):
    next_question: str
    assessment: TurnAssessment
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
    resume_consistency: Literal["supported", "uncertain", "mismatch"] = "supported"
    resume_mismatch_reason: str = Field(default="", max_length=600)
    resume_selection_warning: bool = False
    should_end: bool = False


class InterviewTurn(BaseModel):
    ordinal: int
    question: str
    answer: str
    category: str
    topic: str
    score: float | None = Field(default=None, ge=0, le=10)
    scorable: bool = True
    score_source: Literal["llm", "mock", "unavailable"] = "llm"
    deductions: list[str] = Field(default_factory=list)
    failed: bool = False
    drill_dimension: str = ""
    drill_depth: int = 0
    anchor_keyword: str = ""
    input_mode: Literal["voice", "text"] = "text"
    answer_duration_seconds: float | None = Field(default=None, ge=0, le=3600)
    speech_rate_cpm: float | None = Field(default=None, ge=0, le=2000)
    transcript_edited: bool = False
    original_answer: str = ""
    recommended_answer_seconds: int = Field(default=60, ge=15, le=600)
    created_at: str = Field(default_factory=utc_now_iso)


class RubricDimension(BaseModel):
    score: float | None = Field(default=None, ge=0, le=10)
    weight: float = Field(ge=0, le=1)
    scorable: bool = False
    status: Literal["scored", "not_observed"] = "not_observed"
    evidence: list[str] = Field(default_factory=list)
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
    score: float | None = Field(default=None, ge=0, le=10)
    scorable: bool = True
    status: Literal["scored", "not_scorable"] = "scored"
    evidence: list[str] = Field(default_factory=list)
    deductions: list[str] = Field(min_length=1)
    better_answer: str = Field(min_length=1)
    recommended_answer_seconds: int = Field(default=60, ge=15, le=600)
    answer_duration_seconds: float | None = Field(default=None, ge=0, le=3600)
    input_mode: Literal["voice", "text"] = "text"
    transcript_edited: bool = False
    original_answer: str = ""


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


class TranscriptCorrection(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    ordinal: int | None = Field(default=None, ge=1)
    item_id: str | None = Field(default=None, max_length=256)

    @field_validator("text")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = value.replace("\x00", "").strip()
        if not value:
            raise ValueError("修正后的转写不能为空")
        return value


class EvidenceAnalysis(BaseModel):
    score: float | None = Field(default=None, ge=0, le=10)
    scorable: bool = False
    evidence: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class ResumeAnalysis(BaseModel):
    overall: EvidenceAnalysis = Field(default_factory=EvidenceAnalysis)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    content_suggestions: list[str] = Field(default_factory=list)
    layout_suggestions: list[str] = Field(default_factory=list)
    layout_scorable: bool = False
    layout_evidence: list[str] = Field(default_factory=list)
    rewritten_examples: list[str] = Field(default_factory=list)


class ProcessAnalysis(BaseModel):
    time_control: EvidenceAnalysis = Field(default_factory=EvidenceAnalysis)
    speech_rate: EvidenceAnalysis = Field(default_factory=EvidenceAnalysis)
    wording: EvidenceAnalysis = Field(default_factory=EvidenceAnalysis)
    fluency: EvidenceAnalysis = Field(default_factory=EvidenceAnalysis)
    average_answer_seconds: float | None = Field(default=None, ge=0)
    average_speech_rate_cpm: float | None = Field(default=None, ge=0)


class RoleFitAnalysis(BaseModel):
    overall: EvidenceAnalysis = Field(default_factory=EvidenceAnalysis)
    matched_requirements: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    improvement_plan: list[str] = Field(default_factory=list)


class BehavioralAnalysis(BaseModel):
    """Evidence-backed dimensions for standalone or combined HR interviews.

    These dimensions intentionally sit beside the fixed technical rubric.  A
    behavioral interview should not pretend that unasked MySQL/coding topics
    were observed, while its values, planning and compensation evidence still
    needs a structured home in the report.
    """

    company_fit: EvidenceAnalysis = Field(default_factory=EvidenceAnalysis)
    career_planning: EvidenceAnalysis = Field(default_factory=EvidenceAnalysis)
    collaboration: EvidenceAnalysis = Field(default_factory=EvidenceAnalysis)
    compensation_communication: EvidenceAnalysis = Field(
        default_factory=EvidenceAnalysis
    )


class CompanyExperienceCitation(BaseModel):
    title: str
    url: str
    platform: str = ""
    published_at: str = ""
    round: str = ""
    report_takeaway: str = ""
    takeaways: list[str] = Field(default_factory=list)


class CompanyInsights(BaseModel):
    company_label: str = ""
    sample_caveat: str = ""
    recurring_patterns: list[str] = Field(default_factory=list)
    interview_advice: list[str] = Field(default_factory=list)
    citations: list[CompanyExperienceCitation] = Field(default_factory=list)


class RadarAxis(BaseModel):
    key: str
    label: str
    score: float | None = Field(default=None, ge=0, le=10)
    scorable: bool = False
    evidence: list[str] = Field(default_factory=list)


class InterviewReport(BaseModel):
    schema_version: Literal["1.0", "2.0"] = "2.0"
    report_id: str
    interview_id: str
    generated_at: str = Field(default_factory=utc_now_iso)
    company: Company
    # A report can exist before the candidate has produced any usable answer.
    # Keep that state distinct from a genuine numeric score so API consumers do
    # not mistake a placeholder rubric for performance evidence.
    scored: bool = True
    score_status: Literal["scored", "insufficient_data", "unscorable"] = "scored"
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
    scoring_coverage: float = Field(default=0, ge=0, le=1)
    resume_analysis: ResumeAnalysis = Field(default_factory=ResumeAnalysis)
    process_analysis: ProcessAnalysis = Field(default_factory=ProcessAnalysis)
    role_fit: RoleFitAnalysis = Field(default_factory=RoleFitAnalysis)
    behavioral_analysis: BehavioralAnalysis = Field(default_factory=BehavioralAnalysis)
    company_insights: CompanyInsights = Field(default_factory=CompanyInsights)
    radar: list[RadarAxis] = Field(default_factory=list)


class ApiError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
