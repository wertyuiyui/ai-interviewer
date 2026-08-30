from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import httpx
import pytest
from pydantic import ValidationError

import app.main as main_module
from app.coding_practice import CodingHintRequest, CodingPracticeService
from app.config import get_settings
from app.db import Database
from app.errors import AppError, LLMError
from app.interview_engine import InterviewEngine
from app.practice import (
    PracticeAnswerCreate,
    PracticeAssessment,
    PracticeHintCreate,
    PracticeSessionAction,
    PracticeService,
    PracticeSessionCreate,
    PracticeSkipCreate,
    load_real_question_bank,
)
from app.report_engine import ReportEngine
from app.schemas import InterviewCreate, InterviewTurn, ResumeData


def write_bank(path) -> None:
    path.mkdir()
    (path / "real.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "questions": [
                    {
                        "id": "real-zh-1",
                        "company": "bytedance",
                        "language": "zh",
                        "category": "Redis",
                        "topic": "缓存一致性",
                        "question": "缓存和数据库不一致时，你会如何定位并选择修复方案？",
                        "followups": ["先区分读路径和写路径会有什么帮助？"],
                        "difficulty": "hard",
                        "source_ids": ["licensed-bank-redis"],
                    },
                    {
                        "id": "real-en-1",
                        "language": "en",
                        "category": "System Design",
                        "topic": "backpressure",
                        "question": "How would you propagate backpressure through an API service?",
                        "difficulty": "medium",
                        "source_url": "https://example.com/licensed-bank",
                    },
                    {
                        "id": "untraceable-1",
                        "language": "zh",
                        "category": "MySQL",
                        "topic": "索引",
                        "question": "这道题没有来源，不应进入快刷题库。",
                        "difficulty": "easy",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_real_bank_quick_drill_hint_and_repeated_voice_answer(
    tmp_path,
) -> None:
    question_dir = tmp_path / "questions"
    write_bank(question_dir)
    bank = load_real_question_bank(question_dir)
    assert [item.id for item in bank] == ["real-zh-1", "real-en-1"]
    assert all(item.provenance for item in bank)

    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "practice.db",
    )
    database = Database(settings)
    await database.initialize()
    service = PracticeService(database, settings, question_dir=question_dir)
    await service.initialize()

    created = await service.create_session(
        PracticeSessionCreate(
            client_id="practice-client-001",
            mode="quick",
            company="bytedance",
            language_mode="zh",
            count=5,
        )
    )
    assert created["total_questions"] == 1
    question = created["current_question"]
    assert question["id"] == "real-zh-1"
    assert "source_ids" not in question
    assert "provenance" not in question
    assert "followups" not in question
    assert question["origin_label"] == "真题"
    assert question["source_type"] == "real"

    hint = await service.hint(
        created["id"],
        PracticeHintCreate(
            client_id="practice-client-001",
            question_id=question["id"],
        ),
    )
    assert "读路径" in hint["hint"]
    first = await service.submit_answer(
        created["id"],
        PracticeAnswerCreate(
            client_id="practice-client-001",
            question_id=question["id"],
            answer=(
                "我会先区分读写链路，核对缓存命中、数据库提交与删除缓存的时序，"
                "再通过 trace 和版本号定位窗口，按一致性要求选择延迟双删或消息补偿。"
            ),
            input_mode="text",
            answer_duration_seconds=48,
        ),
    )
    assert first["done"] is True
    assert first["assessment"]["status"] == "scored"
    assert first["hint_used"] is True

    repeated = await service.submit_answer(
        created["id"],
        PracticeAnswerCreate(
            client_id="practice-client-001",
            question_id=question["id"],
            answer=(
                "先用请求链路和版本号定位不一致窗口，再按业务容忍度选择旁路缓存失效、"
                "可靠消息补偿和定期对账，并用不一致率与修复延迟验证。"
            ),
            input_mode="voice",
            answer_duration_seconds=39,
            reattempt=True,
        ),
    )
    assert repeated["reattempt"] is True
    assert repeated["input_mode"] == "voice"

    state = await service.get_session(created["id"], "practice-client-001")
    assert state["answered_questions"] == 1
    assert state["attempt_count"] == 2
    assert state["hint_count"] == 1
    assert state["best_score"] is not None
    assert state["latest_score"] == repeated["assessment"]["score"]
    assert all("provenance" not in item["question"] for item in state["attempts"])

    with sqlite3.connect(settings.db_path) as connection:
        raw = connection.execute(
            "SELECT question_snapshot_json FROM practice_attempts LIMIT 1"
        ).fetchone()[0]
    persisted = json.loads(raw)
    assert persisted["provenance"]["source_ids"] == ["licensed-bank-redis"]


@pytest.mark.asyncio
async def test_quick_drill_english_filter_and_empty_bank_are_explicit(tmp_path) -> None:
    question_dir = tmp_path / "questions"
    write_bank(question_dir)
    settings = replace(
        get_settings(),
        mock_llm=True,
        db_path=tmp_path / "english.db",
    )
    database = Database(settings)
    await database.initialize()
    service = PracticeService(database, settings, question_dir=question_dir)
    await service.initialize()

    english = await service.create_session(
        PracticeSessionCreate(
            client_id="practice-english-001",
            language_mode="en",
            count=10,
        )
    )
    assert english["current_question"]["id"] == "real-en-1"
    assert english["current_question"]["language"] == "en"

    with pytest.raises(AppError) as caught:
        await service.create_session(
            PracticeSessionCreate(
                client_id="practice-empty-001",
                language_mode="zh",
                topic="不存在的主题",
            )
        )
    assert caught.value.code == "PRACTICE_QUESTIONS_EMPTY"
    assert caught.value.status_code == 404


def test_production_quick_drill_company_changes_priority_without_claiming_exclusivity(
    tmp_path,
) -> None:
    settings = replace(get_settings(), mock_llm=True, db_path=tmp_path / "rank.db")
    service = PracticeService(Database(settings), settings)

    byte_questions = service._quick_questions(
        PracticeSessionCreate(
            client_id="practice-company-rank-001",
            company="bytedance",
            interview_type="technical",
            language_mode="zh",
            difficulty=None,
            count=6,
        ),
        "same-selection-seed",
    )
    meituan_questions = service._quick_questions(
        PracticeSessionCreate(
            client_id="practice-company-rank-001",
            company="meituan",
            interview_type="technical",
            language_mode="zh",
            difficulty=None,
            count=6,
        ),
        "same-selection-seed",
    )

    assert byte_questions[0]["kind"] == "coding"
    assert meituan_questions[0]["category"] == "MySQL"
    assert [item["id"] for item in byte_questions] != [
        item["id"] for item in meituan_questions
    ]


@pytest.mark.asyncio
async def test_coding_drill_uses_only_reviewed_real_questions_even_when_infinite(
    tmp_path,
) -> None:
    settings = replace(
        get_settings(), mock_llm=True, db_path=tmp_path / "coding-practice.db"
    )
    database = Database(settings)
    await database.initialize()
    service = PracticeService(database, settings)
    await service.initialize()

    catalog = await service.catalog()
    assert catalog["coding_question_count"] == 4
    coding_catalog = next(
        item for item in catalog["drill_types"] if item["id"] == "coding"
    )
    assert coding_catalog["question_count"] == 4
    assert coding_catalog["judge_mode"] == "review"

    request = PracticeSessionCreate(
        client_id="coding-drill-client-001",
        drill_type="coding",
        interview_type="technical",
        language_mode="bilingual",
        difficulty=None,
        count=None,
        infinite=True,
    )
    selected = service._quick_questions(request, "coding-selection-seed")
    assert len(selected) == 4
    assert all(item["kind"] == "coding" for item in selected)
    assert all(item["origin"] == "real" for item in selected)

    session = await service.create_session(request)
    assert session["drill_type"] == "coding"
    for _ in range(7):
        question = session["current_question"]
        assert question["kind"] == "coding"
        assert question["origin"] == "real"
        result = await service.submit_answer(
            session["id"],
            PracticeAnswerCreate(
                client_id="coding-drill-client-001",
                question_id=question["id"],
                answer=(
                    "def solve(value):\n"
                    "    # 展示核心状态更新、空输入边界，并在最后说明时间和空间复杂度\n"
                    "    return value\n"
                    "时间复杂度按题目要求分析，额外空间只保留必要状态。"
                ),
                input_mode="text",
            ),
        )
        assert result["assessment"]["status"] == "scored"
        session = await service.get_session(
            session["id"], "coding-drill-client-001"
        )

    assert all(
        attempt["question"]["kind"] == "coding"
        and attempt["question"]["origin"] == "real"
        for attempt in session["attempts"]
    )


def test_coding_drill_rejects_review_and_hr_modes() -> None:
    with pytest.raises(ValidationError, match="只支持快速刷题"):
        PracticeSessionCreate(
            client_id="coding-invalid-client-001",
            drill_type="coding",
            mode="review",
            source_interview_id="interview-001",
        )
    with pytest.raises(ValidationError, match="只支持技术面"):
        PracticeSessionCreate(
            client_id="coding-invalid-client-002",
            drill_type="coding",
            interview_type="hr",
        )


def test_combined_quick_drill_keeps_behavioral_share_with_technical_filters(
    tmp_path,
) -> None:
    settings = replace(get_settings(), mock_llm=True, db_path=tmp_path / "mixed.db")
    service = PracticeService(Database(settings), settings)
    questions = service._quick_questions(
        PracticeSessionCreate(
            client_id="practice-mixed-filter-001",
            company="bytedance",
            interview_type="technical_hr",
            language_mode="zh",
            topic="MySQL",
            difficulty="hard",
            count=5,
        ),
        "mixed-filter-seed",
    )

    assert len(questions) == 5
    assert any(item["kind"] == "behavioral" for item in questions)
    assert any(
        item["kind"] != "behavioral" and item["category"] == "MySQL"
        for item in questions
    )


@pytest.mark.asyncio
async def test_review_reuses_exact_interview_turn_and_checks_owner(tmp_path) -> None:
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "review.db",
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    interview = await engine.create(
        InterviewCreate(
            client_id="review-owner-001",
            company="bytedance",
            language_mode="en",
            resume=ResumeData(),
        )
    )
    original_question = "请解释一次 Redis 缓存击穿的完整请求链路。"
    await database.append_turn(
        interview["id"],
        InterviewTurn(
            ordinal=1,
            question=original_question,
            answer="不知道。",
            category="fundamentals",
            topic="Redis",
            score=2.5,
            scorable=True,
            score_source="mock",
            deductions=["没有说明机制"],
            failed=True,
        ),
        "下一题",
    )
    await database.finish_interview(interview["id"], "manual")

    service = PracticeService(database, settings)
    await service.initialize()
    review = await service.create_session(
        PracticeSessionCreate(
            client_id="review-owner-001",
            mode="review",
            source_interview_id=interview["id"],
            review_ordinals=[1],
            count=1,
        )
    )
    assert review["current_question"]["question"] == original_question
    assert review["language_mode"] == "en"
    assert review["current_question"]["previous_score"] == 2.5
    assert review["current_question"]["previous_deductions"] == ["没有说明机制"]

    with pytest.raises(AppError) as caught:
        await service.get_session(review["id"], "different-owner-001")
    assert caught.value.code == "PRACTICE_FORBIDDEN"
    assert await service.owns_session(review["id"], "different-owner-001") is False
    assert await service.owns_session(review["id"], "review-owner-001") is True


@pytest.mark.asyncio
async def test_report_adds_interview_mistakes_and_finite_modules_prioritize_them(
    tmp_path,
) -> None:
    question_dir = tmp_path / "questions"
    write_bank(question_dir)
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "interview-mistakes.db",
    )
    database = Database(settings)
    await database.initialize()
    engine = InterviewEngine(database, settings)
    interview = await engine.create(
        InterviewCreate(
            client_id="interview-mistake-client-001",
            company="bytedance",
            language_mode="zh",
            interview_type="technical_hr",
            resume=ResumeData.model_validate(
                {
                    "项目": [
                        {"name": "交易平台", "role": "参与开发"},
                        {"name": "校园交易平台", "role": "后端负责人"},
                    ]
                }
            ),
        )
    )
    turns = [
        InterviewTurn(
            ordinal=1,
            question="请解释 Redis 缓存击穿的完整请求链路。",
            answer="不知道。",
            category="fundamentals",
            topic="Redis",
            score=2.5,
            score_source="mock",
            deductions=["没有说明请求链路"],
            failed=True,
        ),
        InterviewTurn(
            ordinal=2,
            question="未来两年你会如何选择和验证自己的技术方向？",
            answer="还没想好。",
            category="communication",
            topic="综合面·职业规划与选择",
            score=4.0,
            score_source="mock",
            deductions=["缺少可验证行动"],
        ),
        InterviewTurn(
            ordinal=3,
            question="请说明两数之和的解法、复杂度和边界用例。",
            answer="只会暴力枚举。",
            category="coding_thought",
            topic="手撕思路·数组",
            score=5.0,
            score_source="mock",
            deductions=["没有说明目标复杂度"],
        ),
        InterviewTurn(
            ordinal=4,
            question="校园交易平台中，你如何定位并修复过一次一致性问题？",
            answer="没有形成完整复盘。",
            category="project_depth",
            topic="项目深挖·问题定位",
            score=6.0,
            score_source="mock",
            deductions=["缺少定位链路和验证结果"],
        ),
        InterviewTurn(
            ordinal=5,
            question="请解释 JVM 类加载的双亲委派。",
            answer="回答存在关键错误。",
            category="fundamentals",
            topic="JVM",
            score=7.5,
            score_source="mock",
            deductions=["核心结论错误"],
            failed=True,
        ),
        InterviewTurn(
            ordinal=6,
            question="请说明一次已经验证的性能优化。",
            answer="通过压测和监控验证了延迟下降。",
            category="fundamentals",
            topic="性能优化",
            score=8.0,
            score_source="mock",
            deductions=["可以补充更多数据"],
        ),
        InterviewTurn(
            ordinal=7,
            question="本轮评分服务不可用。",
            answer="这是一个有效回答。",
            category="fundamentals",
            topic="MySQL",
            score=None,
            scorable=False,
            score_source="unavailable",
            deductions=[],
        ),
        InterviewTurn(
            ordinal=8,
            question="你如何排序薪酬、成长空间和技术方向？",
            answer="主要看薪酬。",
            category="communication",
            topic="综合面·薪酬沟通",
            score=5.5,
            score_source="mock",
            deductions=["缺少排序依据和可协商边界"],
        ),
    ]
    for turn in turns:
        await database.append_turn(interview["id"], turn, "下一题")
    await database.finish_interview(interview["id"], "manual")

    reports = ReportEngine(database, settings)
    await reports.generate(interview["id"])
    await reports.generate(interview["id"])

    def simulate_pre_feature_report(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM practice_mistakes")
        connection.execute(
            "DELETE FROM schema_migrations WHERE name = 'interview_mistakes_v1'"
        )
        connection.commit()

    await database._run(simulate_pre_feature_report)
    await database.initialize()
    service = PracticeService(database, settings, question_dir=question_dir)
    await service.initialize()

    mistakes = await service.mistakes("interview-mistake-client-001")
    assert len(mistakes) == 6
    assert {item["question"]["kind"] for item in mistakes} == {
        "technical",
        "behavioral",
        "coding",
        "project",
    }
    assert all(item["attempt_count"] == 1 for item in mistakes)
    assert all(item["question"]["source_type"] == "interview" for item in mistakes)
    assert all(item["latest_deductions"] for item in mistakes)
    project_mistake = next(
        item for item in mistakes if item["question"]["kind"] == "project"
    )
    assert project_mistake["question"]["project_name"] == "校园交易平台"
    assert project_mistake["question"]["previous_better_answer"]
    assert any(
        item["latest_score"] == 7.5 and item["question"]["topic"] == "JVM"
        for item in mistakes
    )

    technical = await service.create_session(
        PracticeSessionCreate(
            client_id="interview-mistake-client-001",
            company="bytedance",
            topic="Redis",
            difficulty="medium",
            interview_type="technical",
            language_mode="zh",
            count=1,
        )
    )
    assert technical["current_question"]["from_mistake_book"] is True
    assert technical["current_question"]["topic"] == "Redis"
    assert technical["current_question"]["previous_score"] == 2.5

    hr = await service.create_session(
        PracticeSessionCreate(
            client_id="interview-mistake-client-001",
            company="bytedance",
            interview_type="hr",
            language_mode="zh",
            count=1,
        )
    )
    assert hr["current_question"]["kind"] == "behavioral"
    assert hr["current_question"]["from_mistake_book"] is True

    mixed = await service.create_session(
        PracticeSessionCreate(
            client_id="interview-mistake-client-001",
            company="bytedance",
            interview_type="technical_hr",
            language_mode="zh",
            count=4,
        )
    )
    stored_mixed = await service._require_session(
        mixed["id"], "interview-mistake-client-001"
    )
    assert sum(
        item.get("kind") == "behavioral" for item in stored_mixed["questions"]
    ) == 2
    assert all(
        item.get("kind") in {"technical", "behavioral"}
        for item in stored_mixed["questions"]
    )

    coding = await service.create_session(
        PracticeSessionCreate(
            client_id="interview-mistake-client-001",
            company="bytedance",
            topic="数组",
            interview_type="technical",
            drill_type="coding",
            language_mode="zh",
            count=1,
        )
    )
    assert coding["current_question"]["kind"] == "coding"
    assert coding["current_question"]["from_mistake_book"] is True

    coding_workbench = CodingPracticeService(settings, db=database)
    coding_catalog = await coding_workbench.catalog("interview-mistake-client-001")
    assert coding_catalog["mistake_count"] == 1
    assert coding_catalog["questions"][0]["from_mistake_book"] is True
    assert coding_catalog["questions"][0]["prompt"]["zh"] == turns[2].question
    coding_hint = await coding_workbench.hint(
        CodingHintRequest(
            challenge_id=coding_catalog["questions"][0]["id"],
            stage="approach",
            client_id="interview-mistake-client-001",
        )
    )
    assert coding_hint["hint"]

    await service.submit_answer(
        technical["id"],
        PracticeAnswerCreate(
            client_id="interview-mistake-client-001",
            question_id=technical["current_question"]["id"],
            answer="不知道。",
        ),
    )
    refreshed = await service.mistakes("interview-mistake-client-001")
    retried = next(
        item
        for item in refreshed
        if item["question"]["id"] == technical["current_question"]["id"]
    )
    assert retried["attempt_count"] == 2

    await service.delete_mistake(
        project_mistake["id"], "interview-mistake-client-001"
    )
    await database.initialize()
    assert all(
        item["question"]["kind"] != "project"
        for item in await service.mistakes("interview-mistake-client-001")
    )

    def corrupt_legacy_report(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE reports SET report_json = '{' WHERE interview_id = ?",
            (interview["id"],),
        )
        connection.execute(
            "DELETE FROM schema_migrations WHERE name = 'interview_mistakes_v1'"
        )
        connection.commit()

    await database._run(corrupt_legacy_report)
    await database.initialize()

    def migration_was_not_falsely_completed(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name = 'interview_mistakes_v1'"
        ).fetchone() is None

    assert await database._run(migration_was_not_falsely_completed)


class FailingClient:
    async def chat_json(self, *args, **kwargs):
        raise LLMError("synthetic outage")


class RecordingAssessmentClient:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, str]], dict]] = []

    async def chat_json(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {
            "score": 7.5,
            "scorable": True,
            "status": "scored",
            "evidence": ["回答给出了具体信息。"],
            "strengths": ["结构清楚。"],
            "deductions": [],
            "better_answer": "给出一个更具体、有证据的示范回答。",
            "key_points": ["证据"],
            "next_steps": ["补充细节"],
        }


@pytest.mark.asyncio
async def test_mixed_practice_uses_question_specific_behavioral_and_technical_rubrics(
    tmp_path,
) -> None:
    settings = replace(
        get_settings(),
        mock_llm=False,
        db_path=tmp_path / "rubric.db",
    )
    recorder = RecordingAssessmentClient()
    database = Database(settings)
    await database.initialize()

    def seed_profile(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE profile_projects (
                id TEXT PRIMARY KEY, client_id TEXT NOT NULL, name TEXT NOT NULL,
                responsibility TEXT NOT NULL, selected INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE profile_resumes (
                client_id TEXT NOT NULL, name TEXT NOT NULL,
                parsed_resume_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO profile_projects VALUES (?, ?, ?, ?, ?, ?)",
            (
                "profile-project-001",
                "practice-profile-001",
                "订单服务",
                "负责库存扣减链路和 Redis 缓存一致性",
                1,
                "2026-08-30T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO profile_resumes VALUES (?, ?, ?, ?)",
            (
                "practice-profile-001",
                "后端实习简历",
                json.dumps(
                    {
                        "项目": [
                            {
                                "name": "订单服务",
                                "role": "后端开发",
                                "technologies": ["Java", "Redis", "MySQL"],
                                "highlights": ["实现库存扣减与缓存失效"],
                                "metrics": [],
                            }
                        ],
                        "实习经历": [],
                        "技能": ["Java", "Redis"],
                    },
                    ensure_ascii=False,
                ),
                "2026-08-30T00:00:00Z",
            ),
        )
        connection.commit()

    await database._run(seed_profile)
    service = PracticeService(
        database, settings, client=recorder  # type: ignore[arg-type]
    )

    behavioral_assessment = await service._assess(
        question={
            "id": "behavioral-in-mixed",
            "kind": "behavioral",
            "category": "综合面",
            "topic": "职业规划",
            "question": "为什么选择这份实习？未来三年如何规划？",
        },
        answer="我会结合一次真实选择说明自己的依据、行动和结果。",
        language_mode="zh",
        company="bytedance",
        interview_type="technical_hr",
        client_id="practice-profile-001",
    )
    behavioral_messages, _ = recorder.calls[-1]
    behavioral_system = behavioral_messages[0]["content"]
    behavioral_payload = json.loads(behavioral_messages[1]["content"])
    assert behavioral_payload["assessment_mode"] == "behavioral"
    assert "STAR 证据的具体性" in behavioral_system
    assert "价值观、选择逻辑与目标公司/岗位契合度" in behavioral_system
    assert "不得因具体数值本身扣分" in behavioral_system
    assert "绝不虚构看似真实的技术实现" in behavioral_system
    assert "- 正确性 40%" not in behavioral_system
    assert behavioral_payload["profile_grounding"]["available"] is True
    assert behavioral_payload["profile_grounding"]["selected_project"] == {
        "name": "订单服务",
        "responsibility": "负责库存扣减链路和 Redis 缓存一致性",
    }
    assert behavioral_payload["profile_grounding"]["resumes"][0]["projects"][0][
        "technologies"
    ] == ["Java", "Redis", "MySQL"]
    assert behavioral_assessment.deductions == ["仍需具体补充：补充细节"]

    await service._assess(
        question={
            "id": "technical-in-mixed",
            "kind": "technical",
            "category": "MySQL",
            "topic": "索引",
            "question": "联合索引为什么要遵循最左匹配？",
        },
        answer="我会从 B+ 树有序键和查询边界说明最左匹配与失效场景。",
        language_mode="zh",
        company="bytedance",
        interview_type="technical_hr",
    )
    technical_messages, _ = recorder.calls[-1]
    technical_system = technical_messages[0]["content"]
    technical_payload = json.loads(technical_messages[1]["content"])
    assert technical_payload["assessment_mode"] == "technical"
    assert "- 正确性 40%" in technical_system
    assert "原理与深度 30%" in technical_system
    assert "STAR 证据的具体性" not in technical_system
    assert technical_payload["profile_grounding"] == {"available": False}

    # Category classification keeps legacy/persisted pure-HR questions on the
    # behavioral rubric even when they do not carry a `kind` field.
    await service._assess(
        question={
            "id": "legacy-pure-hr",
            "category": "HR",
            "topic": "薪酬期待",
            "question": "你的薪酬期待是什么？",
        },
        answer="我会说明市场信息、岗位匹配和成长机会的优先级。",
        language_mode="zh",
        interview_type="hr",
    )
    pure_hr_messages, _ = recorder.calls[-1]
    assert json.loads(pure_hr_messages[1]["content"])["assessment_mode"] == "behavioral"
    assert "STAR 证据的具体性" in pure_hr_messages[0]["content"]


@pytest.mark.asyncio
async def test_scoring_outage_is_unscored_never_default_five(tmp_path) -> None:
    question_dir = tmp_path / "questions"
    write_bank(question_dir)
    settings = replace(
        get_settings(),
        mock_llm=False,
        db_path=tmp_path / "unscored.db",
    )
    database = Database(settings)
    await database.initialize()
    service = PracticeService(
        database,
        settings,
        client=FailingClient(),  # type: ignore[arg-type]
        question_dir=question_dir,
    )
    await service.initialize()
    created = await service.create_session(
        PracticeSessionCreate(
            client_id="practice-unscored-001",
            company="bytedance",
            language_mode="zh",
            count=1,
        )
    )
    result = await service.submit_answer(
        created["id"],
        PracticeAnswerCreate(
            client_id="practice-unscored-001",
            question_id=created["current_question"]["id"],
            answer="这是一段已经被保存但评分服务暂时不可用的有效回答。",
        ),
    )
    assessment = result["assessment"]
    assert assessment["score"] is None
    assert assessment["scorable"] is False
    assert assessment["status"] == "unscored"
    assert assessment["evidence"] == []
    assert assessment["key_points"] == []
    assert await service.mistakes("practice-unscored-001") == []


def test_zero_score_moves_negative_strengths_to_deductions() -> None:
    assessment = PracticeAssessment.model_validate(
        {
            "score": 0,
            "scorable": True,
            "status": "scored",
            "evidence": [],
            "strengths": ["未完成核心机制说明", "没有给出任何边界条件"],
            "deductions": [],
            "better_answer": "先解释核心机制，再给出边界条件。",
            "key_points": ["核心机制"],
            "next_steps": [],
        }
    )

    assert assessment.strengths == []
    assert assessment.deductions == ["未完成核心机制说明", "没有给出任何边界条件"]


@pytest.mark.asyncio
async def test_infinite_skip_finish_and_mistake_book_lifecycle(tmp_path) -> None:
    question_dir = tmp_path / "questions"
    write_bank(question_dir)
    settings = replace(get_settings(), mock_llm=True, db_path=tmp_path / "infinite.db")
    database = Database(settings)
    await database.initialize()
    service = PracticeService(database, settings, question_dir=question_dir)
    await service.initialize()

    finite = await service.create_session(
        PracticeSessionCreate(
            client_id="practice-mistake-001",
            company="bytedance",
            language_mode="zh",
            count=1,
        )
    )
    low = await service.submit_answer(
        finite["id"],
        PracticeAnswerCreate(
            client_id="practice-mistake-001",
            question_id=finite["current_question"]["id"],
            answer="不知道。",
        ),
    )
    assert low["assessment"]["score"] <= 6
    mistakes = await service.mistakes("practice-mistake-001")
    assert len(mistakes) == 1
    assert mistakes[0]["latest_deductions"]

    infinite = await service.create_session(
        PracticeSessionCreate(
            client_id="practice-mistake-001",
            company="bytedance",
            language_mode="zh",
            count=None,
            infinite=True,
        )
    )
    assert infinite["infinite"] is True
    assert infinite["total_questions"] is None
    assert infinite["current_question"]["from_mistake_book"] is True
    skipped = await service.skip(
        infinite["id"],
        PracticeSkipCreate(
            client_id="practice-mistake-001",
            question_id=infinite["current_question"]["id"],
        ),
    )
    assert skipped["done"] is False
    assert skipped["next_question"] is not None
    assert len(await service.mistakes("practice-mistake-001")) == 1

    finished = await service.finish(
        infinite["id"], PracticeSessionAction(client_id="practice-mistake-001")
    )
    assert finished["status"] == "completed"
    # Manual finish is deliberately idempotent.
    assert (
        await service.finish(
            infinite["id"], PracticeSessionAction(client_id="practice-mistake-001")
        )
    )["status"] == "completed"
    with pytest.raises(AppError) as caught:
        await service.skip(
            infinite["id"],
            PracticeSkipCreate(
                client_id="practice-mistake-001",
                question_id=skipped["next_question"]["id"],
            ),
        )
    assert caught.value.code == "PRACTICE_FINISHED"

    await service.delete_mistake(mistakes[0]["id"], "practice-mistake-001")
    assert await service.mistakes("practice-mistake-001") == []


@pytest.mark.asyncio
async def test_infinite_mode_periodically_appends_explicit_ai_question(tmp_path) -> None:
    settings = replace(get_settings(), mock_llm=True, db_path=tmp_path / "infinite-ai.db")
    database = Database(settings)
    await database.initialize()
    service = PracticeService(database, settings)
    await service.initialize()
    session = await service.create_session(
        PracticeSessionCreate(
            client_id="practice-infinite-ai-001",
            language_mode="zh",
            count=1,
            infinite=True,
        )
    )
    question = session["current_question"]
    result = None
    for _ in range(4):
        result = await service.submit_answer(
            session["id"],
            PracticeAnswerCreate(
                client_id="practice-infinite-ai-001",
                question_id=question["id"],
                answer="我会说明核心机制、请求链路、故障边界、观测指标以及方案取舍。",
            ),
        )
        assert result["done"] is False
        question = result["next_question"]
    assert result is not None
    assert question["origin_label"] == "AI出题"
    assert question["source_type"] == "ai"
    assert question["source_label"] == "AI 仿真生成"
    assert "source_url" not in question


@pytest.mark.asyncio
async def test_practice_rest_api_contract(tmp_path, monkeypatch) -> None:
    question_dir = tmp_path / "questions"
    write_bank(question_dir)
    settings = replace(
        get_settings(),
        mock_llm=True,
        voice_mode="L3",
        db_path=tmp_path / "practice-api.db",
    )
    database = Database(settings)
    await database.initialize()
    service = PracticeService(database, settings, question_dir=question_dir)
    await service.initialize()
    monkeypatch.setattr(main_module, "practice_service", service)

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        catalog = await client.get("/api/practice/catalog")
        assert catalog.status_code == 200
        assert catalog.json()["question_count"] == 2

        created = await client.post(
            "/api/practice/sessions",
            json={
                "client_id": "practice-api-client",
                "mode": "quick",
                "company": "bytedance",
                "language_mode": "zh",
                "count": 1,
            },
        )
        assert created.status_code == 201
        payload = created.json()
        question = payload["current_question"]
        assert set(question).isdisjoint(
            {
                "source_ids",
                "source_id",
                "source_url",
                "source_path",
                "revision",
                "license",
                "provenance",
            }
        )

        hint = await client.post(
            f"/api/practice/sessions/{payload['id']}/hint",
            json={
                "client_id": "practice-api-client",
                "question_id": question["id"],
            },
        )
        assert hint.status_code == 200

        answer = await client.post(
            f"/api/practice/sessions/{payload['id']}/answers",
            json={
                "client_id": "practice-api-client",
                "question_id": question["id"],
                "answer": "我先定位读写链路，再分析一致性窗口、补偿机制与监控指标。",
                "input_mode": "voice",
                "answer_duration_seconds": 31.5,
            },
        )
        assert answer.status_code == 200
        assert answer.json()["input_mode"] == "voice"
        assert answer.json()["hint_used"] is True

        state = await client.get(
            f"/api/practice/sessions/{payload['id']}",
            params={"client_id": "practice-api-client"},
        )
        assert state.status_code == 200
        assert state.json()["status"] == "completed"
        history = await client.get(
            "/api/practice/history",
            params={"client_id": "practice-api-client"},
        )
        assert history.status_code == 200
        assert history.json()["items"][0]["id"] == payload["id"]

        mistakes = await client.get(
            "/api/practice/mistakes",
            params={"client_id": "practice-api-client"},
        )
        assert mistakes.status_code == 200
        assert len(mistakes.json()["items"]) == 1
        mistake_id = mistakes.json()["items"][0]["id"]
        deleted = await client.delete(
            f"/api/practice/mistakes/{mistake_id}",
            params={"client_id": "practice-api-client"},
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}


def test_practice_voice_route_requires_session_ownership() -> None:
    source = Path(__file__).resolve().parents[1] / "app" / "main.py"
    text = source.read_text(encoding="utf-8")
    assert '@app.websocket("/ws/practice/sessions/{session_id}")' in text
    assert "practice_service.owns_session" in text
    assert 'event.get("type") != "client.ready"' in text
    assert "PRACTICE_ANSWER_RATE_LIMIT" in text
