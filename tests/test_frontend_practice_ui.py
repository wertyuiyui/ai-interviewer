from __future__ import annotations

from pathlib import Path


PUBLIC = Path(__file__).resolve().parents[1] / "public"


def test_practice_page_supports_quick_review_voice_text_and_english() -> None:
    page = (PUBLIC / "practice.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "practice.js").read_text(encoding="utf-8")

    assert 'id="practiceForm"' in page
    assert 'value="en"' in page
    assert 'id="voiceModeButton"' in page
    assert 'id="textModeButton"' in page
    assert 'id="recordButton"' in page
    assert 'id="practiceAnswer"' in page
    assert 'id="reattemptButton"' in page
    assert "/api/practice/sessions" in script
    assert "/ws/practice/sessions/" in script
    assert "practice.transcript.partial" in script
    assert "practice.transcript.done" in script
    assert "review_ordinals" in script
    assert "score.toFixed(1)" in script
    assert "Number.isFinite(score)" in script


def test_practice_question_ui_does_not_render_provenance() -> None:
    page = (PUBLIC / "practice.html").read_text(encoding="utf-8")

    assert "source_id" not in page
    assert "license_spdx" not in page
    assert "题目来源" not in page
    assert "许可证" not in page


def test_home_and_report_link_to_quick_and_bad_question_practice() -> None:
    home = (PUBLIC / "index.html").read_text(encoding="utf-8")
    report_page = (PUBLIC / "report.html").read_text(encoding="utf-8")
    report_script = (PUBLIC / "js" / "report.js").read_text(encoding="utf-8")

    assert 'href="/practice">快速刷题' in home
    assert 'id="retryQuestionsButton"' in report_page
    assert "/practice?review=" in report_script
    assert "ordinal=" in report_script
    assert "单独重答这题" in report_script
