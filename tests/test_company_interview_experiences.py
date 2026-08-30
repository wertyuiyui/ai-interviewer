from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
REFERENCE_PATH = ROOT_DIR / "resources" / "company_interview_experiences.json"


def test_company_interview_experiences_are_report_only_and_traceable() -> None:
    payload = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "1.0"
    policy = payload["usage_policy"]
    assert policy["exposure"] == "report_only"
    assert policy["do_not_show_in_interview"] is True
    assert "不使用 RAG" in policy["runtime"]
    assert "不绕过登录" in policy["social_media"]

    companies = payload["companies"]
    assert set(companies) == {
        "bytedance",
        "meituan",
        "tencent",
        "alibaba",
        "baidu",
        "huawei",
    }

    all_ids: set[str] = set()
    for company in companies.values():
        assert len(company["trend_summary"]) >= 3
        assert len(company["report_advice"]) >= 3
        assert len(company["sources"]) >= 3

        for source in company["sources"]:
            source_id = source["id"]
            assert source_id not in all_ids
            all_ids.add(source_id)

            parsed = urlparse(source["url"])
            assert parsed.scheme == "https"
            assert parsed.netloc == "www.nowcoder.com"
            assert source["platform"] == "牛客网"
            assert source["provenance_type"] == "first_hand"
            assert date.fromisoformat(source["published_at"]) <= date.fromisoformat(
                payload["curated_at"]
            )
            assert source["round"] == "一面"
            assert source["signals"]
            assert source["report_takeaway"]
