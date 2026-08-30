from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_unscored_report_frontend_contract() -> None:
    report_html = (ROOT / "public" / "report.html").read_text(encoding="utf-8")
    report_js = (ROOT / "public" / "js" / "report.js").read_text(encoding="utf-8")

    assert 'id="overallScoreUnit"' in report_html
    assert "report.scored ? report.overall.toFixed(1) : '—'" in report_js
    assert "scoreStatus !== 'insufficient_data'" in report_js
    assert "有效回答不足，暂不评分" in report_js
    assert "if (!report?.scored) return" in report_js
    assert "historyReports.filter((report) => report.scored)" in report_js
    assert "数据不足 · 不写入弱项记忆" in report_js
