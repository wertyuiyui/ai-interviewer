from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlparse

import pymupdf as fitz
import pytest

from app.config import get_settings
from app.content import load_question_bank, load_style_card, load_topic_links
from app.db import Database
from app.errors import AppError
from app.interview_engine import InterviewEngine
from app.llm import parse_json_content
from app.prompt_engine import SEVEN_DRILL_DIMENSIONS, build_system_prompt, select_questions
from app.report_engine import ReportEngine
from app.resume import ResumeParser, extract_pdf_text
from app.schemas import InterviewCreate, Project, ResumeData
from app.topics import project_depth_target


def mock_settings(tmp_path, voice_mode: str = "L3"):
    return replace(
        get_settings(),
        mock_llm=True,
        voice_mode=voice_mode,
        db_path=tmp_path / "test.db",
    )


def sample_resume() -> ResumeData:
    return ResumeData(
        项目=[
            Project(
                name="校园秒杀系统",
                role="后端负责人",
                technologies=["Java", "Redis", "MySQL"],
                highlights=["用 Redis Lua 做库存预扣减"],
                metrics=["QPS 从 800 提升到 3000"],
            )
        ],
        技能=["Java", "Redis", "MySQL"],
    )


def test_static_cards_banks_and_resource_allowlist() -> None:
    expected_counts = {
        "MySQL": 8,
        "Redis": 8,
        "Java并发": 8,
        "计网": 8,
        "手撕思路": 4,
    }
    for company in ("bytedance", "meituan", "tencent"):
        card = load_style_card(company)
        assert sum(card["stage_ratio"].values()) == pytest.approx(1.0)
        assert card["project_weight"] + card["fundamentals_weight"] == pytest.approx(1.0)
        assert card["minimum_project_drill_depth"] >= 3
        bank = load_question_bank(company)
        assert len(bank) == 36
        assert len({item["id"] for item in bank}) == 36
        for category, count in expected_counts.items():
            assert sum(item["category"] == category for item in bank) == count
        assert all(len(item["followups"]) >= 2 for item in bank)

    for resource in load_topic_links().values():
        assert urlparse(resource["url"]).hostname in {"javaguide.cn", "codetop.cc"}
        assert resource["title"] in {"JavaGuide", "CodeTop"}


def test_prompt_contains_non_negotiable_interview_rules() -> None:
    prompt = build_system_prompt(
        company="bytedance",
        resume=sample_resume(),
        stress=True,
        duration_minutes=15,
        weak_topics=["Redis"],
    )
    assert all(dimension in prompt for dimension in SEVEN_DRILL_DIMENSIONS)
    assert "至少 3 层" in prompt
    assert "绝不点评" in prompt
    assert "沉默10秒" in prompt
    assert "qwen" not in prompt.lower()
    assert "QPS 从 800 提升到 3000" in prompt
    weighted = select_questions("bytedance", ["Redis"], 15)
    assert [item["category"] for item in weighted[:3]] == ["Redis"] * 3


def test_pdf_text_layer_and_scanned_pdf_error() -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (50, 80),
        "Backend Resume Java Redis MySQL Project internship education " * 4,
    )
    data = document.tobytes()
    document.close()
    assert "Backend Resume" in extract_pdf_text(data)

    scanned = fitz.open()
    scanned.new_page()
    scanned_data = scanned.tobytes()
    scanned.close()
    with pytest.raises(AppError) as exc_info:
        extract_pdf_text(scanned_data)
    assert exc_info.value.code == "SCANNED_PDF"


@pytest.mark.asyncio
async def test_mock_resume_parser_returns_required_chinese_schema(tmp_path) -> None:
    settings = mock_settings(tmp_path)
    parsed = await ResumeParser(settings).parse(
        "某大学计算机本科\n校园秒杀系统项目，使用 Java、Redis 和 MySQL，QPS 提升 50%。\n负责后端开发实习。"
    )
    payload = parsed.model_dump(by_alias=True)
    assert set(payload) == {"教育", "实习经历", "项目", "技能"}
    assert "Redis" in payload["技能"]
    assert payload["项目"]


@pytest.mark.asyncio
async def test_three_layer_drill_early_end_report_and_memory(tmp_path) -> None:
    settings = mock_settings(tmp_path)
    db = Database(settings)
    await db.initialize()
    engine = InterviewEngine(db, settings)

    normal = await engine.create(
        InterviewCreate(
            client_id="browser-client-001",
            resume=sample_resume(),
            company="bytedance",
            stress=False,
            duration_minutes=15,
        )
    )
    await db.start_interview(normal["id"])
    answers = [
        "我负责校园秒杀系统，用 Redis Lua 预扣库存，QPS 从800提升到3000。",
        "业务目标是让社团抢票在高峰期不超卖，我负责库存和订单链路。",
        "请求先过网关，再查活动状态，Lua 原子扣 Redis 库存，然后 Kafka 异步落 MySQL。",
        "选择 Redis Lua 是为了原子性和低延迟，也比较过数据库悲观锁。",
    ]
    questions: list[str] = []
    for answer in answers:
        result = await engine.answer(normal["id"], answer)
        questions.append(result.question)
        assert not result.ended
    turns = await db.list_turns(normal["id"])
    assert [turn.drill_depth for turn in turns] == [0, 1, 2, 3]
    assert {turn.drill_dimension for turn in turns} >= {
        "业务背景",
        "个人职责",
        "请求链路",
    }
    assert any("Redis" in question or "QPS" in question for question in questions[1:])
    coding = await engine.answer(
        normal["id"],
        "选择 Redis Lua 是因为脚本原子执行，数据库悲观锁在峰值流量下竞争更重。",
    )
    assert "LRU" in coding.question and "O(1)" in coding.question
    turns = await db.list_turns(normal["id"])
    assert turns[-1].drill_depth == 4
    assert turns[-1].category == "project_depth"
    await db.finish_interview(normal["id"], "manual")
    report = await ReportEngine(db, settings).generate(normal["id"])
    assert len(report.question_feedback) == len(answers) + 1
    assert all(item.deductions and item.better_answer for item in report.question_feedback)
    assert report.rubric.project_depth.weight == 0.4
    assert report.rubric.fundamentals.weight == 0.3
    assert report.rubric.coding_thought.weight == 0.2
    assert report.rubric.communication.weight == 0.1

    second = await engine.create(
        InterviewCreate(
            client_id="browser-client-001",
            resume=sample_resume(),
            company="meituan",
            stress=False,
            duration_minutes=15,
        )
    )
    assert second["weak_topics"]
    assert set(second["weak_topics"]) <= {
        "MySQL", "Redis", "Java并发", "计网", "手撕思路", "项目深度", "表达逻辑"
    }
    assert project_depth_target(second["weak_topics"]) == 6
    second_row = await db.get_interview(second["id"])
    assert second_row is not None
    assert "完成 6 层项目下钻" in second_row["system_prompt"]
    if "Redis" in second["weak_topics"]:
        weighted = select_questions("meituan", second["weak_topics"], 15)
        baseline = select_questions("meituan", [], 15)
        assert [item["id"] for item in weighted] != [item["id"] for item in baseline]
        assert [item["category"] for item in weighted[:3]] == ["Redis"] * 3

    stress = await engine.create(
        InterviewCreate(
            client_id="browser-client-002",
            resume=sample_resume(),
            company="bytedance",
            stress=True,
            duration_minutes=15,
        )
    )
    await db.start_interview(stress["id"])
    first = await engine.answer(stress["id"], "我用 Redis 做了缓存和库存控制。")
    assert not first.ended
    assert first.pressure_action == "chain"
    failed_once = await engine.answer(stress["id"], "不知道")
    assert failed_once.breakdown_streak == 1
    assert failed_once.pressure_action == "challenge"
    assert failed_once.question.startswith("我对这个前提存疑")
    failed_twice = await engine.answer(stress["id"], "不会")
    assert failed_twice.ended
    assert failed_twice.pressure_action == "interrupt"
    assert failed_twice.end_reason == "poor_performance"
    assert "今天的面试就到这里" in failed_twice.question


def test_json_parser_handles_fenced_output() -> None:
    assert parse_json_content('```json\n{"ok": true}\n```') == {"ok": True}
