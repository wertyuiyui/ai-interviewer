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
    "hr",
}


def test_every_fake_resume_has_grounded_web_answers() -> None:
    resumes = json.loads(
        (ROOT / "testdata/fake_resumes.json").read_text(encoding="utf-8")
    )["resumes"]
    fixture = json.loads(
        (ROOT / "testdata/fake_interview_answers.json").read_text(encoding="utf-8")
    )
    profiles = {item["slug"]: item for item in fixture["profiles"]}

    assert set(profiles) == {item["slug"] for item in resumes}
    for resume in resumes:
        profile = profiles[resume["slug"]]
        assert profile["name"] == resume["name"]
        assert profile["target"] == resume["target"]
        assert (ROOT / profile["source_pdf"]).is_file()
        answers = {item["intent"]: item["answer"] for item in profile["answers"]}
        assert set(answers) == REQUIRED_INTENTS
        assert all(len(answer) >= 20 for answer in answers.values())

    assert profiles["05_weak_project_candidate"]["test_mode"] == "pressure_early_stop"
