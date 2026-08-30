from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.content import (
    COMPANIES,
    load_english_question_bank,
    load_hr_question_bank,
    load_interview_skill,
    load_style_card,
)
from app.db import Database
from app.interview_engine import InterviewEngine
from app.prompt_engine import (
    build_system_prompt,
    enforce_project_drill,
    initial_question,
    select_questions,
    select_server_questions,
)
from app.report_engine import REPORT_SYSTEM_PROMPT_EN, ReportEngine
from app.schemas import InterviewCreate, Project, ResumeData
from app.voice import EdgeTTS
from app.voice_session import _spoken_control_prompt


SUPPORTED = {
    "bytedance": "字节跳动",
    "meituan": "美团",
    "tencent": "腾讯",
    "alibaba": "阿里巴巴",
    "baidu": "百度",
    "huawei": "华为",
}
EVIDENCE_LEVELS = {
    "bytedance": "high",
    "meituan": "high",
    "tencent": "high",
    "alibaba": "medium",
    "baidu": "medium",
    "huawei": "medium",
}
ROOT = Path(__file__).resolve().parents[1]


def sample_resume() -> ResumeData:
    return ResumeData(
        项目=[
            Project(
                name="Campus Order Service",
                role="Backend developer",
                technologies=["Java", "Redis", "MySQL"],
                highlights=["Implemented cache-aside and idempotent order creation"],
                metrics=["p99 latency reduced by 35%"],
            )
        ],
        技能=["Java", "Redis", "MySQL"],
    )


def test_six_company_registry_and_versioned_skills_are_complete() -> None:
    assert COMPANIES == SUPPORTED
    required = {
        "version",
        "company",
        "display_name",
        "evidence_level",
        "flow",
        "tone",
        "topic_weights",
        "difficulty_ladder",
        "project_followup_rules",
        "pressure_policy",
        "hr_focus",
        "language_profiles",
        "source_refs",
    }
    for company, label in SUPPORTED.items():
        card = load_style_card(company)
        skill = load_interview_skill(company)
        assert skill["display_name"] == label
        assert required <= set(skill)
        assert skill["evidence_level"] == EVIDENCE_LEVELS[company]
        assert skill["language_profiles"]["en"]
        assert skill["project_followup_rules"]
        assert card["project_weight"] + card["fundamentals_weight"] == pytest.approx(1)


def test_company_schema_is_extensible_but_rejects_unknown_slugs() -> None:
    request = InterviewCreate(
        client_id="english-company-client",
        company="ALIBABA",
        language_mode="en",
        resume=sample_resume(),
    )
    assert request.company == "alibaba"
    assert request.language_mode == "en"
    with pytest.raises(ValidationError):
        InterviewCreate(
            client_id="unknown-company-client",
            company="unverified-company",
            resume=sample_resume(),
        )


def test_home_and_report_preserve_six_companies_and_english_mode() -> None:
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    home = (ROOT / "public" / "js" / "home.js").read_text(encoding="utf-8")
    common = (ROOT / "public" / "js" / "common.js").read_text(encoding="utf-8")
    report = (ROOT / "public" / "js" / "report.js").read_text(encoding="utf-8")
    for company, label in SUPPORTED.items():
        assert f'name="company" value="{company}"' in html
        assert label in common
    assert 'name="language_mode" value="en"' in html
    assert "['zh', 'bilingual', 'en'].includes" in home
    assert "report.languageMode === 'en' ? 'Pure English'" in report


def test_pure_english_prompt_applies_company_skill_and_hides_provenance() -> None:
    questions = load_english_question_bank()
    assert len(questions) == 108
    assert all(item["language"] == "en" for item in questions)
    assert {item["license"] for item in questions} <= {"Apache-2.0", "MIT"}
    technical = select_questions(
        "alibaba", [], 15, "通用后端", language_mode="en"
    )
    assert not any(item["category"] in {"Behavioral", "AI Engineering"} for item in technical)
    combined = select_questions(
        "alibaba", [], 15, "通用后端", language_mode="en", interview_type="technical_hr"
    )
    assert any(item["category"] == "Behavioral" for item in combined)
    prompt = build_system_prompt(
        company="alibaba",
        resume=sample_resume(),
        duration_minutes=15,
        weak_topics=[],
        language_mode="en",
        interview_type="technical_hr",
        stress_level=2,
    )
    assert "PURE ENGLISH MODE" in prompt
    assert "company-specific interview skill" not in prompt.lower()
    assert "【公司针对性 interview skill】" in prompt
    assert "one_hundred_times_traffic" in prompt
    selected = select_server_questions(
        "alibaba",
        [],
        15,
        "通用后端",
        language_mode="en",
        interview_type="technical_hr",
    )
    assert all(item["question"] in prompt for item in selected)
    assert "source_url" not in prompt
    assert "source_title" not in prompt
    assert "source_path" not in prompt
    assert "Apache-2.0" not in prompt
    assert "github.com/donnemartin" not in prompt


def test_opening_and_forced_project_followups_are_english_only() -> None:
    opening = initial_question("huawei", "en")
    assert "academic background" in opening
    assert not re.search(r"[\u3400-\u9fff]", opening)

    question, _, depth = enforce_project_drill(
        "",
        completed_turns=2,
        anchor="Redis",
        resume=sample_resume(),
        vague=False,
        language_mode="en",
    )
    assert depth == 2
    assert "Redis" in question
    assert not re.search(r"[\u3400-\u9fff]", question)


@pytest.mark.asyncio
async def test_mock_combined_interview_keeps_every_candidate_question_english(
    tmp_path,
) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "english-flow.db",
        daily_interview_limit=100,
        client_daily_interview_limit=100,
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="english-flow-client",
            company="baidu",
            interview_type="technical_hr",
            language_mode="en",
            stress_level=2,
            resume=sample_resume(),
        )
    )
    assert not re.search(r"[\u3400-\u9fff]", created["initial_question"])
    await database.start_interview(created["id"])

    answers = [
        "I am a third-year computer science student. I have completed databases and operating systems and I am now focusing on backend reliability.",
        "I built the campus order service myself. It processes student orders and I owned the API, cache, database schema, and load tests.",
        "A request enters through the gateway, reaches the Java service, checks Redis, writes MySQL in a transaction, and publishes an event.",
        "I chose cache-aside because reads dominate. I compared database-only reads and a local cache but needed shared invalidation across instances.",
        "I would first inspect p99 latency by dependency, trace one slow request, and compare Redis misses, pool wait time, and database execution plans.",
        "I would define p99 over five-minute windows, compare it with the pre-release baseline, and validate the change under the same traffic distribution.",
        "If traffic grew tenfold, the database connection pool and hot keys would fail first, so I would add admission control, shard hot state, and degrade noncritical reads.",
        "I would state the delivery constraint, compare evidence with the team, run a small experiment, and own the result even if my first proposal was rejected.",
        "In five years I want to own reliable backend services, and this internship lets me test that plan through real delivery and feedback.",
        "This semester I track shipped milestones, load-test results, and mentor feedback, then revise the plan at the end of each month.",
    ]
    questions: list[str] = []
    for answer in answers:
        result = await engine.answer(created["id"], answer)
        questions.append(result.question)
        assert not re.search(r"[\u3400-\u9fff]", result.question)
        assert not result.ended
    assert [questions[index] for index in (5, 7, 9)] == [
        item["question"] for item in load_hr_question_bank("baidu", "en")
    ]
    assert any("compensation expectations" in question.lower() for question in questions)


def test_english_voice_and_report_instructions_are_not_chinese_fallbacks() -> None:
    spoken = _spoken_control_prompt(
        "Explain the consistency boundary of this cache.",
        startup=True,
        language_mode="en",
    )
    assert "natural, professional English" in spoken
    assert not re.search(r"[\u3400-\u9fff]", spoken)
    assert EdgeTTS(voice="en-US-GuyNeural").voice == "en-US-GuyNeural"
    assert "structured JSON report in natural professional English" in REPORT_SYSTEM_PROMPT_EN
    required = ReportEngine._report_requirements(
        {"language_mode": "en", "interview_type": "technical_hr"}
    )
    assert "Write every narrative field in English" in required
    assert not re.search(r"[\u3400-\u9fff]", required)


def test_new_company_report_advice_is_caveated_and_uses_traceable_links() -> None:
    insights = ReportEngine._company_insights("huawei")

    assert insights.recurring_patterns
    assert insights.interview_advice
    assert "不是该公司官方标准" in insights.sample_caveat
    assert len(insights.citations) >= 3
    assert all(item.url.startswith("https://www.nowcoder.com/") for item in insights.citations)
