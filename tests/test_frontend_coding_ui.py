from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"


def test_home_groups_simulation_and_three_single_practice_cards() -> None:
    page = (PUBLIC / "index.html").read_text(encoding="utf-8")

    assert 'id="simulationModuleTitle">模拟面试' in page
    assert 'id="practiceModuleTitle">单项练习' in page
    assert 'href="/practice"' in page and "八股题库" in page
    assert 'href="/coding"' in page and "手撕代码" in page
    assert 'href="/project"' in page and "项目解读" in page
    assert page.index('class="feature-module is-simulation"') < page.index('class="feature-module is-practice"')


def test_coding_page_uses_independent_interview_workflow_and_static_review() -> None:
    page = (PUBLIC / "coding.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "coding.js").read_text(encoding="utf-8")
    style = (PUBLIC / "assets" / "coding.css").read_text(encoding="utf-8")

    for label in ("澄清约束", "方案设计", "编码实现", "主动自测"):
        assert label in page
    for dimension in ("沟通与澄清", "解题与取舍", "技术实现", "主动测试"):
        assert dimension in script
    assert 'id="codingCode"' in page
    assert 'id="codingTests"' in page
    assert "不会在服务器执行" in page
    assert "没有编译或执行" in page
    assert "/api/coding/catalog" in script
    assert "/api/coding/hint" in script
    assert "/api/coding/review" in script
    assert "/api/practice/sessions" not in script
    assert "localStorage" in script
    assert ".coding-editor" in style
    assert 'id="codingRun"' not in page
