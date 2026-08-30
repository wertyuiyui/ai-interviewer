from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_INTENTS = {
    "self_intro",
    "project_overview",
    "ownership",
    "request_flow",
    "tradeoff",
    "metrics",
    "failure_boundary",
    "scaling",
    "fundamentals",
    "fundamentals_followup",
    "coding",
    "hr_fit",
    "hr_fit_followup",
    "career_planning",
    "career_followup",
    "compensation",
    "compensation_followup",
    "candidate_questions",
    "fallback_unknown",
}
EXPECTED_FLOWS = {
    "technical": [
        "self_intro",
        "project_deep_dive",
        "fundamentals",
        "coding",
        "candidate_questions",
    ],
    "hr": [
        "self_intro",
        "hr_fit",
        "career_planning",
        "compensation",
        "candidate_questions",
    ],
    "technical_hr": [
        "self_intro",
        "project_deep_dive",
        "fundamentals",
        "coding",
        "hr_fit",
        "career_planning",
        "compensation",
        "candidate_questions",
    ],
}


def test_every_fake_resume_has_grounded_web_answers() -> None:
    resumes = json.loads(
        (ROOT / "testdata/fake_resumes.json").read_text(encoding="utf-8")
    )["resumes"]
    fixture = json.loads(
        (ROOT / "testdata/fake_interview_answers.json").read_text(encoding="utf-8")
    )
    profiles = {item["slug"]: item for item in fixture["profiles"]}
    flows = fixture["interview_flows"]

    assert set(profiles) == {item["slug"] for item in resumes}
    assert {
        flow: [stage["stage"] for stage in stages]
        for flow, stages in flows.items()
    } == EXPECTED_FLOWS
    for resume in resumes:
        profile = profiles[resume["slug"]]
        assert profile["name"] == resume["name"]
        assert profile["target"] == resume["target"]
        assert (ROOT / profile["source_pdf"]).is_file()
        answers = {item["intent"]: item["answer"] for item in profile["answers"]}
        assert set(answers) == REQUIRED_INTENTS
        assert all(len(answer) >= 20 for answer in answers.values())
        assert "项目" in answers["self_intro"] or "实习" in answers["self_intro"]
        assert all(
            intent in answers
            for stages in flows.values()
            for stage in stages
            for intent in stage["answer_intents"]
        )

    assert profiles["05_weak_project_candidate"]["test_mode"] == "pressure_early_stop"
