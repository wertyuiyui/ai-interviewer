from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_unscored_report_frontend_contract() -> None:
    report_html = (ROOT / "public" / "report.html").read_text(encoding="utf-8")
    report_js = (ROOT / "public" / "js" / "report.js").read_text(encoding="utf-8")

    assert 'id="overallScoreUnit"' in report_html
    assert "hasOverallScore ? report.overall.toFixed(1) : '—'" in report_js
    assert "['insufficient_data', 'unscorable', 'not_scorable'].includes" in report_js
    assert "有效回答不足，暂不评分" in report_js
    assert "if (!report?.scored || !report.rubric.every" in report_js
    assert "historyReports.filter((report) => report.scored)" in report_js
    assert "数据不足 · 不写入弱项记忆" in report_js


def test_extended_evidence_report_and_source_visibility_contract() -> None:
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    report_html = (ROOT / "public" / "report.html").read_text(encoding="utf-8")
    report_js = (ROOT / "public" / "js" / "report.js").read_text(encoding="utf-8")

    for element_id in (
        "scoreCoverage",
        "holisticRadarCanvas",
        "holisticDimensionList",
        "resumeAnalysisDetail",
        "processAnalysisDetail",
        "roleFitDetail",
        "companyCitationList",
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
    assert "report_takeaway" in report_js
    assert "!Number.isFinite(dimensions[index].score)" in report_js
    assert "JavaGuide" in report_js and "CodeTop" in report_js
    assert "ARIS-in-AI-Offer" not in index_html
