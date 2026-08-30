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


def test_practice_page_supports_unlimited_skip_finish_and_mistake_book() -> None:
    page = (PUBLIC / "practice.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "practice.js").read_text(encoding="utf-8")

    assert 'value="unlimited"' in page
    assert 'id="practiceSkip"' in page
    assert 'id="mistakeBookList"' in page
    assert "个人 Profile" in page
    assert "count: infinite ? null" in script
    assert "infinite," in script
    assert "/skip" in script
    assert "/finish" in script
    assert "/api/practice/mistakes" in script
    assert "method: 'DELETE'" in script
    assert "第 ${position} 题 · 无限模式" in script


def test_practice_question_ui_renders_safe_public_provenance_and_origin_badges() -> None:
    page = (PUBLIC / "practice.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "practice.js").read_text(encoding="utf-8")

    assert 'id="questionOrigin"' in page
    assert 'id="questionSource"' in page
    assert "【真题】" in page
    assert "AI出题" in script
    assert "source_label" in script
    assert "source_url" in script
    assert "['http:', 'https:']" in script
    assert "noopener noreferrer" in script


def test_home_and_report_link_to_quick_and_bad_question_practice() -> None:
    home = (PUBLIC / "index.html").read_text(encoding="utf-8")
    report_page = (PUBLIC / "report.html").read_text(encoding="utf-8")
    report_script = (PUBLIC / "js" / "report.js").read_text(encoding="utf-8")

    assert 'href="/practice"' in home and "<strong>快速刷题</strong>" in home
    assert 'id="retryQuestionsButton"' in report_page
    assert "/practice?review=" in report_script
    assert "ordinal=" in report_script
    assert "单独重答这题" in report_script


def test_quick_practice_is_an_interview_knowledge_drill_not_the_coding_workbench() -> None:
    page = (PUBLIC / "practice.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "practice.js").read_text(encoding="utf-8")
    style = (PUBLIC / "assets" / "practice.css").read_text(encoding="utf-8")
    home = (PUBLIC / "index.html").read_text(encoding="utf-8")

    assert "八股快速刷题" in page
    assert 'name="drill_type"' not in page
    assert 'id="codingNotice"' not in page
    assert "requestedDrillType" not in script
    assert "drill_type: drillType" not in script
    assert "is-coding-session" not in style
    assert 'href="/coding"' in home and "<strong>手撕代码</strong>" in home
    assert "/practice?drill=coding" not in home
