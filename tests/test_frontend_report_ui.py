from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_unscored_report_frontend_contract() -> None:
    report_html = (ROOT / "public" / "report.html").read_text(encoding="utf-8")
    report_js = (ROOT / "public" / "js" / "report.js").read_text(encoding="utf-8")

    assert 'id="overallScoreUnit"' in report_html
    assert "hasOverallScore ? report.overall.toFixed(1) : '—'" in report_js
    assert "['insufficient_data', 'unscorable', 'not_scorable', 'unscored', 'missing'].includes" in report_js
    assert "有效回答不足，暂不评分" in report_js
    assert "if (!report?.scored || !report.rubric.every" in report_js
    assert "historyReports.filter((report) => report.scored)" in report_js
    assert "数据不足 · 不写入弱项记忆" in report_js


def test_extended_evidence_report_and_source_visibility_contract() -> None:
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    report_html = (ROOT / "public" / "report.html").read_text(encoding="utf-8")
    report_js = (ROOT / "public" / "js" / "report.js").read_text(encoding="utf-8")
    common_js = (ROOT / "public" / "js" / "common.js").read_text(encoding="utf-8")

    for element_id in (
        "scoreCoverage",
        "holisticRadarCanvas",
        "holisticDimensionList",
        "resumeAnalysisDetail",
        "processAnalysisDetail",
        "roleFitDetail",
        "companyCitationList",
        "companyPersonalizedAdviceList",
    ):
        assert f'id="{element_id}"' in report_html

    assert "score10(entry, 0)" not in report_js
    assert "explicitlyBlocked" in report_js
    assert "scoring_coverage" in report_js
    assert "source.overall ?? source" in report_js
    assert "average_speech_rate_cpm" in report_js
    assert "matched_requirements" in report_js
    assert "recurring_patterns" in report_js
    assert "interview_advice" in report_js
    assert "personalized_advice" in report_js
    assert "report_takeaway" in report_js
    assert "!Number.isFinite(dimensions[index].score)" in report_js
    assert "JavaGuide" in report_js and "CodeTop" in report_js
    assert "ARIS-in-AI-Offer" not in index_html
    assert "mock_interview.report_cache.v2" in common_js
    assert "localStorage.removeItem(STORAGE.legacyReports)" in common_js
    assert "firstValue(metadata, ['score_status'], '')" in report_js
    assert "firstValue(metadata, ['scored'], undefined)" in report_js


def test_public_pages_hide_internal_source_and_voice_tier_copy() -> None:
    public = ROOT / "public"
    visible_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            public / "index.html",
            public / "interview.html",
            public / "report.html",
            public / "js" / "home.js",
            public / "js" / "interview.js",
            public / "js" / "report.js",
            public / "js" / "common.js",
        )
    )

    assert "AI 工程方向会混入约 1/3 的模型服务题" not in visible_source
    assert "精选改写自 ARIS-in-AI-Offer" not in visible_source
    assert "ARIS-in-AI-Offer" not in visible_source
    assert "L0 · 端到端语音" not in visible_source
    assert "实时语音" in visible_source
    assert "?v=20260830-profile-bank-v2" in visible_source
