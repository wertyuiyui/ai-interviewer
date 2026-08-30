from __future__ import annotations

import re
from pathlib import Path


PUBLIC = Path(__file__).resolve().parents[1] / "public"
NAV_PAGES = ("index.html", "practice.html", "project.html", "coding.html", "report.html")


def _top_navigation(page: str) -> tuple[list[str], list[str]]:
    match = re.search(
        r'<nav class="header-actions"[^>]*>(.*?)</nav>',
        page,
        flags=re.DOTALL,
    )
    assert match is not None
    navigation = match.group(1)
    links = re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', navigation, re.DOTALL)
    hrefs = [href for href, _ in links]
    labels = [re.sub(r"<[^>]+>", "", label).strip() for _, label in links]
    return hrefs, labels


def test_every_primary_page_has_only_the_three_global_navigation_options() -> None:
    for filename in NAV_PAGES:
        page = (PUBLIC / filename).read_text(encoding="utf-8")
        hrefs, labels = _top_navigation(page)
        assert hrefs == ["/", "/#profilePanel", "/report?view=history"], filename
        assert labels == ["首页", "个人档案", "历史报告"], filename


def test_profile_navigation_opens_existing_profile_panel_without_new_data_flow() -> None:
    page = (PUBLIC / "index.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "home.js").read_text(encoding="utf-8")

    assert 'id="profilePanel"' in page
    assert "function openProfileFromHash" in script
    assert "profilePanel.open = true" in script
    assert "window.addEventListener('hashchange', openProfileFromHash)" in script


def test_report_view_switch_remains_available_below_global_navigation() -> None:
    page = (PUBLIC / "report.html").read_text(encoding="utf-8")
    _, labels = _top_navigation(page)
    header_end = page.index("</header>")

    assert labels == ["首页", "个人档案", "历史报告"]
    assert page.index('id="currentTab"') > header_end
    assert page.index('id="historyTab"') > header_end
    assert 'class="report-view-tabs"' in page
