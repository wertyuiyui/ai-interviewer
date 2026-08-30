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

    assert 'href="/practice">快速刷题' in home
    assert 'id="retryQuestionsButton"' in report_page
    assert "/practice?review=" in report_script
    assert "ordinal=" in report_script
    assert "单独重答这题" in report_script


def test_practice_page_has_real_bank_coding_drill_and_honest_static_review_ui() -> None:
    page = (PUBLIC / "practice.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "practice.js").read_text(encoding="utf-8")
    style = (PUBLIC / "assets" / "practice.css").read_text(encoding="utf-8")
    home = (PUBLIC / "index.html").read_text(encoding="utf-8")

    assert 'name="drill_type" value="coding"' in page
    assert 'id="codingNotice"' in page
    assert "真实题库专项" in page
    assert "静态讲评" in page
    assert "不会冒充在线编译判题" in page
    assert "/practice?drill=coding" in home
    assert "requestedDrillType" in script
    assert "drill_type: drillType" in script
    assert "response.drill_type === 'coding'" in script
    assert "url.searchParams.set('drill', 'coding')" in script
    assert "静态代码讲评，不执行或编译代码" in script
    assert ".is-coding-session .practice-answer-input textarea" in style
    assert 'font-family: "SFMono-Regular"' in style
