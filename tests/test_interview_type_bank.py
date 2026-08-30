from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import app.content as content_module
from app.config import get_settings
from app.content import (
    SPECIALIZATION_FALLBACKS,
    company_question_rank,
    load_hr_question_bank,
    load_interview_skill,
    load_specialization_catalog,
    load_style_card,
)
from app.db import Database
from app.interview_engine import InterviewEngine
from app.practice import PracticeService, PracticeSessionCreate
from app.prompt_engine import (
    build_system_prompt,
    select_questions,
    select_server_questions,
)
from app.schemas import InterviewCreate, ResumeData, TurnAssessment, TurnDecision


def test_bank_phase_pairs_reviewed_questions_with_one_safe_followup() -> None:
    technical = [
        InterviewEngine._bank_phase(
            "technical",
            completed_turns=completed,
            drill_target=4,
            combined_hr_stages=0,
        )
        for completed in range(5, 9)
    ]
    assert [
        (phase.next_track, phase.next_index, phase.next_followup)
        for phase in technical
    ] == [
        ("technical", 0, False),
        ("technical", 0, True),
        ("technical", 1, False),
        ("technical", 1, True),
    ]

    combined = [
        InterviewEngine._bank_phase(
            "technical_hr",
            completed_turns=completed,
            drill_target=3,
            combined_hr_stages=3,
        )
        for completed in range(4, 13)
    ]
    assert [
        (phase.next_track, phase.next_index, phase.next_followup)
        for phase in combined
    ] == [
        ("technical", 0, False),
        ("technical", 0, True),
        ("hr", 0, False),
        ("hr", 0, True),
        ("hr", 1, False),
        ("hr", 1, True),
        ("hr", 2, False),
        ("hr", 2, True),
        ("technical", 1, False),
    ]

    unrelated = InterviewEngine._anchored_bank_followup(
        "请解释另一个无关的数据库问题。",
        answer="我会用 Redis Lua 保证原子扣减。",
        anchor="Redis",
        bank_item={"topic": "Redis"},
        track="technical",
        vague=False,
        language_mode="zh",
    )
    assert "Redis" in unrelated
    assert "边界条件" in unrelated
    off_topic_evidence = InterviewEngine._anchored_bank_followup(
        "你提到 MySQL B+ 树，请继续说它的叶子节点。",
        answer="我不太清楚 Redis 淘汰，只熟悉 MySQL B+ 树。",
        anchor="MySQL",
        bank_item={
            "topic": "Redis / 淘汰策略",
            "category": "Redis",
            "question": "Redis 有哪些内存淘汰策略？",
        },
        track="technical",
        vague=False,
        language_mode="zh",
    )
    assert "Redis" in off_topic_evidence
    assert "淘汰策略" in off_topic_evidence
    assert "MySQL" in off_topic_evidence
    assert "叶子节点" not in off_topic_evidence
    hr_without_evidence = InterviewEngine._anchored_bank_followup(
        "你为什么选择后端？",
        answer="我选择后端是因为喜欢系统问题。",
        anchor="后端",
        bank_item={"topic": "人生规划与选择"},
        track="hr",
        vague=False,
        language_mode="zh",
    )
    assert "后端" in hr_without_evidence
    assert "具体行动" in hr_without_evidence
    assert "证据" in hr_without_evidence
    bilingual_english = InterviewEngine._anchored_bank_followup(
        "",
        answer="I used p99 latency to verify the change.",
        anchor="p99",
        bank_item={
            "topic": "Networking",
            "language": "en",
            "question": "How would you diagnose a latency regression?",
        },
        track="technical",
        vague=False,
        language_mode="bilingual",
    )
    assert "p99" in bilingual_english
    assert "evidence" in bilingual_english
    assert not any("\u4e00" <= char <= "\u9fff" for char in bilingual_english)
    assert InterviewEngine._recommended_seconds_for_item(
        {"suggested_seconds": 135}, "普通问题"
    ) == 135
    assert InterviewEngine.recommended_answer_seconds(
        "请用一分钟介绍你做过的项目。"
    ) == 60
    assert InterviewEngine.recommended_answer_seconds(
        "In one minute, describe the project you are most proud of."
    ) == 60


def test_pressure_silence_and_english_expression_signals_are_evidence_gated() -> None:
    assert InterviewEngine._pressure_action(2, 2, "silence") == "silence"
    assert InterviewEngine._pressure_action(3, 4, "silence") == "silence"
    assert InterviewEngine._pressure_action(1, 2, "silence") == "chain"
    assert InterviewEngine._pressure_action(3, 2, "interrupt") == "chain"
    assert not InterviewEngine._has_expression_problem(
        "I think the cache should expire after the write completes.", []
    )
    tangled = (
        "Um, uh, I think maybe the cache changes first, you know, but I mean, "
        "um, I am not reaching a clear conclusion."
    )
    assert InterviewEngine._has_expression_problem(tangled, [])
    assert (
        InterviewEngine._pressure_action(
            3,
            2,
            "interrupt",
            expression_problem=InterviewEngine._has_expression_problem(tangled, []),
        )
        == "interrupt"
    )


@pytest.mark.asyncio
async def test_silence_result_is_reachable_without_forcing_an_interruption(
    tmp_path, monkeypatch
) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        db_path=tmp_path / "pressure-silence.db",
        daily_interview_limit=20,
        client_daily_interview_limit=10,
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="pressure-silence-client",
            resume=ResumeData(),
            company="bytedance",
            language_mode="zh",
            stress_level=2,
        )
    )
    await database.start_interview(created["id"])

    async def decide_with_pause(*_args, **_kwargs) -> TurnDecision:
        return TurnDecision(
            next_question="请继续说请求链路的边界条件。",
            assessment=TurnAssessment(
                score=7,
                scorable=True,
                score_source="mock",
                failed=False,
                dimension="project_depth",
                topic="请求链路",
            ),
            pressure_action="silence",
            drill_dimension="请求链路",
            drill_depth=1,
            anchor_keyword="Redis",
        )

    monkeypatch.setattr(engine, "_decide", decide_with_pause)
    first = await engine.answer(created["id"], "我的项目用 Redis 缓存商品信息。")
    assert first.pressure_action == "none"
    second = await engine.answer(
        created["id"],
        "请求先查 Redis，未命中时回源数据库并回填。",
    )
    assert second.pressure_action == "silence"
    assert second.silence_seconds == 10
    assert "打断" not in second.question


@pytest.mark.asyncio
async def test_off_topic_answer_cannot_relabel_or_redirect_reviewed_bank_pair(
    tmp_path,
) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        db_path=tmp_path / "bank-anchor.db",
        daily_interview_limit=20,
        client_daily_interview_limit=10,
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="bank-anchor-client-001",
            resume=ResumeData(),
            company="tencent",
            language_mode="zh",
            stress_level=0,
        )
    )
    await database.start_interview(created["id"])
    reviewed = select_server_questions(
        "tencent",
        [],
        15,
        "通用后端",
        selection_seed=created["id"],
        language_mode="zh",
        interview_type="technical",
    )
    bank_item = reviewed[0]

    # Intro plus four project-depth answers brings the server-owned main bank
    # question onto the wire.
    project_answers = [
        "我是计算机专业大三学生，正在学习后端开发。",
        "项目服务于校园商品查询，我负责服务端实现。",
        "请求先进入网关，再查缓存和数据库。",
        "我选择 Redis 是为了降低热点读请求的延迟。",
        "故障时先用指标确认影响范围，再查日志和链路。",
    ]
    result = None
    for project_answer in project_answers:
        result = await engine.answer(created["id"], project_answer)
    assert result is not None
    assert result.question == bank_item["question"]

    redirected = await engine.answer(
        created["id"],
        "这个我不太清楚，但我想改谈 MySQL B+ 树的叶子节点。",
    )
    expected_topic = str(bank_item.get("topic") or bank_item.get("category"))
    assert expected_topic.split("/")[-1].strip() in redirected.question
    assert "MySQL" in redirected.question
    await engine.answer(
        created["id"],
        "MySQL 和这个原问题没有直接关系，我需要回到原理和边界回答。",
    )

    bank_turns = (await database.list_turns(created["id"]))[-2:]
    expected_category = (
        "coding_thought" if bank_item.get("kind") == "coding" else "fundamentals"
    )
    assert [turn.category for turn in bank_turns] == [
        expected_category,
        expected_category,
    ]
    expected_turn_topic = (
        f"手撕思路·{expected_topic}"
        if bank_item.get("kind") == "coding"
        else expected_topic
    )
    assert [turn.topic for turn in bank_turns] == [
        expected_turn_topic,
        expected_turn_topic,
    ]


def test_interview_types_alias_and_reviewed_hr_provenance() -> None:
    legacy = InterviewCreate(
        client_id="legacy-type-client",
        resume=ResumeData(),
        company="tencent",
        interview_type="tech_hr",  # type: ignore[arg-type]
    )
    assert legacy.interview_type == "technical_hr"
    assert (
        PracticeSessionCreate(
            client_id="legacy-practice-client",
            interview_type="tech_hr",  # type: ignore[arg-type]
        ).interview_type
        == "technical_hr"
    )
    assert InterviewCreate(
        client_id="standalone-hr-client",
        resume=ResumeData(),
        company="tencent",
        interview_type="hr",
    ).interview_type == "hr"

    root = Path(__file__).resolve().parents[1]
    raw_questions = json.loads(
        (root / "questions" / "real_practice_bank.json").read_text(encoding="utf-8")
    )["questions"]
    reviewed_behavioral_wording = {
        prompt
        for item in raw_questions
        if item["kind"] == "behavioral"
        for prompt in item["prompt"].values()
    }
    for language_mode in ("zh", "en"):
        questions = load_hr_question_bank("tencent", language_mode)
        assert len(questions) == 3
        assert all(item["kind"] == "behavioral" for item in questions)
        assert all(item["status"] == "approved" for item in questions)
        assert all(item["authenticity"] == "licensed_bank" for item in questions)
        assert all(item["question"] in reviewed_behavioral_wording for item in questions)
        assert all(
            followup in reviewed_behavioral_wording
            for item in questions
            for followup in item["followups"]
        )


def test_specialization_catalog_is_bank_derived_with_custom_and_fallback(
    tmp_path, monkeypatch
) -> None:
    catalog = load_specialization_catalog()
    assert catalog[0] == "通用后端"
    assert {"Java 后端", "Go 后端", "数据库与存储", "分布式系统"} <= set(catalog)
    assert "AI 工程后端 / LLM Infra" in catalog
    # There are no reviewed C++ or Python records, so those presets are not
    # advertised merely because an old static design list mentioned them.
    assert "C++ 后端" not in catalog
    assert "Python 后端" not in catalog
    java_questions = select_server_questions(
        "tencent", [], 15, "Java 后端", selection_seed="catalog-test"
    )
    assert java_questions
    assert all(
        {"java", "jvm"}.intersection(item["direction_tags"])
        for item in java_questions[:3]
    )
    assert select_server_questions(
        "tencent", [], 15, "推荐系统后端", selection_seed="custom-fallback"
    )
    assert InterviewCreate(
        client_id="custom-direction-client",
        resume=ResumeData(),
        company="tencent",
        specialization="推荐系统后端",
    ).specialization == "推荐系统后端"

    monkeypatch.setattr(content_module, "ROOT_DIR", tmp_path)
    assert load_specialization_catalog() == list(SPECIALIZATION_FALLBACKS)


def test_company_skills_only_rank_the_shared_reviewed_public_bank() -> None:
    companies = ("bytedance", "meituan", "tencent", "alibaba", "baidu", "huawei")
    early_sequences: set[tuple[str, ...]] = set()
    for company in companies:
        skill = load_interview_skill(company)
        assert skill["question_topic_priorities"]
        selected = select_server_questions(
            company,
            [],
            15,
            "通用后端",
            selection_seed="company-priority",
            language_mode="zh",
        )
        assert selected
        assert all(item["authenticity"] == "licensed_bank" for item in selected)
        assert all(item["source_id"] and item["source_path"] for item in selected)
        non_coding = [item for item in selected if item["kind"] != "coding"]
        assert company_question_rank(company, non_coding[0]) == min(
            company_question_rank(company, item) for item in non_coding
        )
        early_sequences.add(tuple(item["bank_id"] for item in selected[:3]))

        if load_style_card(company).get("coding_required_every_interview"):
            assert selected[0]["kind"] == "coding"
            assert all(item["kind"] != "coding" for item in selected[1:])
    # Skills change ordering of a common bank, without introducing synthetic
    # or falsely company-exclusive question records.
    assert len(early_sequences) >= 4
    assert company_question_rank(None, selected[0]) == 0


def test_ai_mix_bilingual_rounds_and_observability_aliases_are_deterministic() -> None:
    for company in ("bytedance", "meituan", "tencent", "alibaba", "baidu", "huawei"):
        ai_questions = select_server_questions(
            company,
            [],
            15,
            "AI 工程后端 / LLM Infra",
            selection_seed="ai-third",
            language_mode="zh",
        )
        assert len(ai_questions) == 18
        assert sum(item["kind"] == "ai_engineering" for item in ai_questions) == 6

    for interview_type in ("technical", "hr", "technical_hr"):
        bilingual = select_server_questions(
            "tencent",
            [],
            15,
            "通用后端",
            selection_seed="bilingual-rounds",
            language_mode="bilingual",
            interview_type=interview_type,
        )
        assert [item["language"] for item in bilingual[:6]] == [
            "zh",
            "en",
            "zh",
            "zh",
            "en",
            "zh",
        ]
    assert all(
        item["language"] == "en"
        for item in select_server_questions(
            "tencent", [], 15, language_mode="en", selection_seed="english-only"
        )
    )
    observability = select_server_questions(
        "tencent",
        [],
        15,
        "Observability platform",
        selection_seed="observability-alias",
        language_mode="zh",
    )
    assert "observability" in observability[0]["direction_tags"]
    reliability = select_server_questions(
        "tencent",
        [],
        15,
        "Reliability backend",
        selection_seed="reliability-alias",
        language_mode="zh",
    )
    assert "reliability" in reliability[0]["direction_tags"]


def test_prompt_fixed_candidates_are_server_questions_only() -> None:
    server_questions = select_server_questions(
        "tencent",
        [],
        15,
        "AI 工程后端 / LLM Infra",
        selection_seed="fixed-candidate-only",
        language_mode="zh",
    )
    legacy_candidates = select_questions(
        "tencent",
        [],
        15,
        "AI 工程后端 / LLM Infra",
        selection_seed="fixed-candidate-only",
        language_mode="zh",
    )
    legacy_only = next(
        item
        for item in legacy_candidates
        if str(item.get("id", "")).startswith(
            ("experience-", "project-", "research-")
        )
    )
    prompt = build_system_prompt(
        company="tencent",
        resume=ResumeData(),
        duration_minutes=15,
        weak_topics=[],
        specialization="AI 工程后端 / LLM Infra",
        selection_seed="fixed-candidate-only",
        language_mode="zh",
    )
    assert all(item["question"] in prompt for item in server_questions)
    assert legacy_only["question"] not in prompt
    for hidden in ("source_id", "source_path", "revision", "license"):
        assert f'"{hidden}"' not in prompt


@pytest.mark.asyncio
async def test_standalone_hr_flow_is_server_owned_and_skips_project_drill(tmp_path) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        db_path=tmp_path / "standalone-hr.db",
        daily_interview_limit=20,
        client_daily_interview_limit=10,
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    created = await engine.create(
        InterviewCreate(
            client_id="hr-flow-client-001",
            resume=ResumeData(),
            company="tencent",
            interview_type="hr",
            language_mode="zh",
        )
    )
    stored = await database.get_interview(created["id"])
    assert stored is not None
    assert stored["interview_type"] == "hr"
    assert "独立 HR 面" in stored["system_prompt"]
    assert "不进入项目技术下钻" in stored["system_prompt"]

    await database.start_interview(created["id"])
    expected = load_hr_question_bank("tencent", "zh")
    answers = [
        "我是计算机专业大三学生，希望通过实习了解真实团队协作和工程交付。",
        "我会先调研岗位和产品，再结合自己的后端学习方向说明匹配点。",
        "我具体对比了三个岗位的业务和技术栈，最后选择更匹配的一项，投递反馈验证了判断。",
        "未来几年我想补齐并发和存储能力，这学期会通过项目和实习验证。",
        "我每周记录项目压测结果并复盘学习计划，月底按交付结果调整下一阶段目标。",
        "我会参考市场与城市给出范围，也会综合导师、方向和成长空间沟通。",
    ]
    next_questions = []
    for answer in answers:
        next_questions.append((await engine.answer(created["id"], answer)).question)
    assert next_questions[::2] == [item["question"] for item in expected]
    assert all(
        "具体行动" in next_questions[index] and "证据" in next_questions[index]
        for index in (1, 3, 5)
    )

    turns = await database.list_turns(created["id"])
    assert all(not turn.topic.startswith("项目深挖·") for turn in turns)
    assert [turn.topic for turn in turns[1:6]] == [
        f"综合面·{expected[0]['topic']}",
        f"综合面·{expected[0]['topic']}",
        f"综合面·{expected[1]['topic']}",
        f"综合面·{expected[1]['topic']}",
        f"综合面·{expected[2]['topic']}",
    ]
    assert [turn.drill_depth for turn in turns[1:6]] == [0, 1, 0, 1, 0]


@pytest.mark.asyncio
async def test_quick_practice_filters_and_mixes_interview_types(tmp_path) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        db_path=tmp_path / "practice-types.db",
    )
    database = Database(settings)
    await database.initialize()
    service = PracticeService(database, settings)
    await service.initialize()

    catalog = await service.catalog()
    assert [item["id"] for item in catalog["interview_types"]] == [
        "technical",
        "hr",
        "technical_hr",
    ]

    async def persisted_kinds(interview_type: str) -> list[str]:
        created = await service.create_session(
            PracticeSessionCreate(
                client_id=f"practice-{interview_type}-client",
                interview_type=interview_type,  # type: ignore[arg-type]
                language_mode="zh",
                count=5,
            )
        )
        session = await service._require_session(created["id"], created["client_id"])
        assert session["interview_type"] == interview_type
        return [str(item["kind"]) for item in session["questions"]]

    technical = await persisted_kinds("technical")
    hr = await persisted_kinds("hr")
    combined = await persisted_kinds("technical_hr")
    assert technical and all(kind != "behavioral" for kind in technical)
    assert hr and all(kind == "behavioral" for kind in hr)
    assert combined.count("behavioral") == 2
    assert len(combined) - combined.count("behavioral") == 3
