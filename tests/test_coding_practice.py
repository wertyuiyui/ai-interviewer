from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from app.coding_practice import (
    CodingHintRequest,
    CodingPracticeService,
    CodingReviewRequest,
)
from app.config import get_settings
from app.errors import AppError


ROOT = Path(__file__).resolve().parents[1]


def test_checked_in_coding_bank_is_curated_traceable_and_reviewable() -> None:
    bank = json.loads((ROOT / "questions" / "coding_practice_bank.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "resources" / "coding_source_manifest.json").read_text(encoding="utf-8"))
    questions = bank["questions"]
    source_ids = {source["id"] for source in manifest["sources"]}

    assert len(questions) >= 8
    assert len({question["id"] for question in questions}) == len(questions)
    assert source_ids >= {"grind75", "tih-rubric", "tih-techniques"}
    assert all(question["source_ref"] in source_ids for question in questions)
    assert all(question["examples"] and question["signatures"] for question in questions)
    assert all(question["rubric"]["key_points"] for question in questions)
    assert all(question["rubric"]["edge_cases"] for question in questions)
    assert "never scrapes" in manifest["policy"]["runtime_rule"]
    assert "not claimed as official" in manifest["policy"]["company_claim_rule"]


@pytest.mark.asyncio
async def test_catalog_exposes_workflow_but_keeps_private_rubric_and_hints_private() -> None:
    service = CodingPracticeService(replace(get_settings(), mock_llm=True))
    catalog = await service.catalog()

    assert catalog["question_count"] >= 8
    assert catalog["workflow"] == ["clarify", "approach", "code", "test", "review"]
    assert catalog["judge_mode"] == "static_review"
    assert catalog["languages"] == ["python", "java", "go", "javascript"]
    assert all("rubric" not in question and "hints" not in question for question in catalog["questions"])


@pytest.mark.asyncio
async def test_stage_hint_and_mock_review_follow_four_dimension_static_rubric() -> None:
    service = CodingPracticeService(replace(get_settings(), mock_llm=True))
    hint = await service.hint(CodingHintRequest(challenge_id="grind-two-sum", stage="clarify"))
    result = await service.review(CodingReviewRequest(
        challenge_id="grind-two-sum",
        language="python",
        assumptions="确认无解时返回空数组，并确认是否保证唯一解。",
        approach="单次遍历哈希表，先查 target-current，再记录当前值和下标，避免复用自身。",
        code="def two_sum(nums, target):\n    seen = {}\n    for i, value in enumerate(nums):\n        if target - value in seen:\n            return [seen[target - value], i]\n        seen[value] = i\n    return []",
        complexity="时间 O(n)，空间 O(n)。",
        test_cases=["[3,3], 6 -> [0,1]，覆盖重复值", "[], 9 -> []，覆盖空输入"],
    ))

    assert hint["stage"] == "clarify" and hint["hint"]
    assessment = result["assessment"]
    assert assessment["execution_status"] == "not_executed"
    assert 0 <= assessment["overall_score"] <= 10
    for dimension in ("communication", "problem_solving", "technical_competency", "testing"):
        assert 0 <= assessment[dimension]["score"] <= 10
        assert assessment[dimension]["feedback"]


@pytest.mark.asyncio
async def test_unknown_coding_challenge_is_rejected() -> None:
    service = CodingPracticeService(replace(get_settings(), mock_llm=True))
    with pytest.raises(AppError) as caught:
        await service.hint(CodingHintRequest(challenge_id="missing-question", stage="test"))
    assert caught.value.status_code == 404
