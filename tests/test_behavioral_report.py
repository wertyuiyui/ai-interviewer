import pytest

from app.db import Database
from app.report_engine import ReportEngine
from app.schemas import InterviewTurn
from app.topics import canonical_topic


def _turn(
    ordinal: int,
    *,
    question: str,
    topic: str,
    score: float | None,
    answer: str = "我先核对事实，再和团队说明取舍，最终按周复盘结果。",
    input_mode: str = "text",
    duration: float | None = None,
    recommended_seconds: int = 60,
) -> InterviewTurn:
    return InterviewTurn(
        ordinal=ordinal,
        question=question,
        answer=answer,
        category="communication",
        topic=topic,
        score=score,
        scorable=score is not None,
        score_source="mock" if score is not None else "unavailable",
        input_mode=input_mode,
        answer_duration_seconds=duration,
        recommended_answer_seconds=recommended_seconds,
    )


def test_hr_report_uses_observed_behavioral_dimensions_without_default_scores() -> None:
    analysis = ReportEngine._behavioral_analysis(
        {"interview_type": "hr"},
        [
            _turn(
                1,
                question="讲一次价值观取舍，它和目标公司怎样契合？",
                topic="综合面·价值观与公司契合",
                score=7.5,
            ),
            _turn(
                2,
                question="未来两三年如何规划？",
                topic="综合面·人生规划与选择",
                score=6.5,
            ),
            _turn(
                3,
                question="薪酬、导师和方向如何排序？",
                topic="综合面·薪酬期待",
                score=8.0,
            ),
        ],
    )

    assert analysis.company_fit.score == 7.5
    assert analysis.career_planning.score == 6.5
    assert analysis.compensation_communication.score == 8.0
    assert analysis.collaboration.score is None
    assert analysis.collaboration.scorable is False


def test_technical_report_does_not_invent_behavioral_observations() -> None:
    analysis = ReportEngine._behavioral_analysis(
        {"interview_type": "technical"},
        [_turn(1, question="介绍 Redis", topic="Redis", score=9.0)],
    )

    assert analysis.company_fit.score is None
    assert analysis.career_planning.score is None
    assert analysis.compensation_communication.score is None


def test_english_behavioral_analysis_has_no_chinese_server_narrative() -> None:
    analysis = ReportEngine._behavioral_analysis(
        {"interview_type": "technical_hr"},
        [
            _turn(
                1,
                question="Tell me about a values trade-off and company fit.",
                topic="Values and company fit",
                score=8.0,
                answer="I compared two delivery options, shared the evidence, and reviewed the result with my team.",
            ),
            _turn(
                2,
                question="How are you planning your next three years?",
                topic="Career planning and choices",
                score=5.0,
                answer="I want to become a stronger backend engineer.",
            ),
            _turn(
                3,
                question="How did you handle a conflict with a teammate?",
                topic="Collaboration and conflict handling",
                score=6.0,
                answer="I listened to my teammate and we agreed on one option.",
            ),
            _turn(
                4,
                question="How would you communicate compensation expectations?",
                topic="Compensation communication",
                score=8.0,
                answer="I would explain my market references, priorities, and negotiable boundaries.",
            ),
        ],
        language_mode="en",
    )

    dimensions = (
        analysis.company_fit,
        analysis.career_planning,
        analysis.collaboration,
        analysis.compensation_communication,
    )
    assert any(item.strengths for item in dimensions)
    assert any(item.weaknesses for item in dimensions)
    assert all(item.suggestions for item in dimensions)
    assert all(
        not any("\u3400" <= char <= "\u9fff" for char in narrative)
        for item in dimensions
        for narrative in [
            *item.evidence,
            *item.strengths,
            *item.weaknesses,
            *item.suggestions,
        ]
    )
    assert analysis.company_fit.evidence[0].startswith("Question 1:")


def test_hr_topics_remain_distinct_in_scores_practice_and_memory_aliases() -> None:
    turns = [
        _turn(
            1,
            question="讲一次价值观取舍。",
            topic="综合面·价值观与公司契合",
            score=7.0,
        ),
        _turn(
            2,
            question="未来两三年如何规划？",
            topic="综合面·人生规划与选择",
            score=5.5,
        ),
        _turn(
            3,
            question="讲一次团队冲突。",
            topic="Collaboration and conflict handling",
            score=6.0,
        ),
        _turn(
            4,
            question="如何沟通薪酬预期？",
            topic="Compensation expectations",
            score=4.5,
        ),
    ]

    topic_scores = ReportEngine._topic_scores(turns)
    assert topic_scores == {
        "价值观与公司契合": 7.0,
        "职业规划与选择": 5.5,
        "协作与冲突处理": 6.0,
        "薪酬沟通": 4.5,
    }
    assert "表达逻辑" not in topic_scores

    reporter = object.__new__(ReportEngine)
    practice = reporter._practice_items(topic_scores, turns, language_mode="zh")
    assert [item.topic for item in practice] == [
        "薪酬沟通",
        "职业规划与选择",
        "协作与冲突处理",
    ]
    assert all("综合面主题" in item.reason for item in practice)

    # Database weak-topic memory canonicalizes stored report keys with the same
    # helper, including the English display aliases used by English reports.
    assert canonical_topic("values and company fit", "communication") == "价值观与公司契合"
    assert canonical_topic("career planning and choices", "communication") == "职业规划与选择"
    assert canonical_topic("collaboration and conflict handling", "communication") == "协作与冲突处理"
    assert canonical_topic("compensation communication", "communication") == "薪酬沟通"


@pytest.mark.asyncio
async def test_legacy_english_hr_topics_stay_distinct_in_weak_memory() -> None:
    database = object.__new__(Database)

    async def history(*_args, **_kwargs):
        return [
            {
                "topic_scores": {
                    "values and company fit": 7.0,
                    "career planning and choices": 5.5,
                    "collaboration and conflict handling": 6.0,
                    "compensation communication": 4.5,
                }
            }
        ]

    database.history = history  # type: ignore[method-assign]
    assert await database.weak_topics("anonymous-client", limit=4) == [
        "薪酬沟通",
        "职业规划与选择",
        "协作与冲突处理",
        "价值观与公司契合",
    ]


def test_english_process_analysis_uses_words_and_english_discourse_markers() -> None:
    base = (
        "First, I state the conclusion because the cache boundary matters. "
        "Then I validate it with 3 production metrics and finally explain the trade-off. "
        + "Um, " * 12
    )
    ideal_answer = base + "evidence " * 122
    slow_answer = "I explain the cache result and its boundary with evidence."
    ideal = ReportEngine._process_analysis(
        [
            _turn(
                1,
                question="Explain the cache boundary.",
                topic="Redis",
                score=8.0,
                answer=ideal_answer,
                input_mode="voice",
                duration=60.0,
                recommended_seconds=90,
            )
        ],
        language_mode="en",
    )
    slow = ReportEngine._process_analysis(
        [
            _turn(
                1,
                question="Explain the cache boundary.",
                topic="Redis",
                score=8.0,
                answer=slow_answer,
                input_mode="voice",
                duration=60.0,
                recommended_seconds=90,
            )
        ],
        language_mode="en",
    )

    assert ideal.speech_rate.score == 9.0
    assert slow.speech_rate.score == 4.5
    assert ideal.speech_rate.score != slow.speech_rate.score
    assert "words per minute" in ideal.speech_rate.evidence[0]
    assert ideal.average_speech_rate_cpm is None
    assert ideal.wording.score == 10.0
    assert "English causal or layered transitions" in ideal.wording.evidence[0]
    assert "12 common English filler" in ideal.fluency.evidence[0]
    assert ideal.fluency.weaknesses
    assert all(
        not any("\u3400" <= char <= "\u9fff" for char in value)
        for analysis in (
            ideal.time_control,
            ideal.speech_rate,
            ideal.wording,
            ideal.fluency,
        )
        for value in [
            *analysis.evidence,
            *analysis.strengths,
            *analysis.weaknesses,
            *analysis.suggestions,
        ]
    )


def test_bilingual_process_analysis_keeps_rate_units_separate() -> None:
    chinese_answer = "首先说明结论，因为需要验证边界，最后给出取舍。" + "证据" * 90
    english_answer = (
        "First I explain the boundary, then I provide evidence, and finally I state the trade-off. "
        + "result " * 125
    )
    analysis = ReportEngine._process_analysis(
        [
            _turn(
                1,
                question="说明方案。",
                topic="项目深度",
                score=8.0,
                answer=chinese_answer,
                input_mode="voice",
                duration=60.0,
            ),
            _turn(
                2,
                question="Explain the boundary.",
                topic="Redis",
                score=8.0,
                answer=english_answer,
                input_mode="voice",
                duration=60.0,
            ),
        ],
        language_mode="bilingual",
    )

    assert analysis.speech_rate.score == 9.0
    assert any("字/分钟" in item for item in analysis.speech_rate.evidence)
    assert any("词/分钟" in item for item in analysis.speech_rate.evidence)
    assert any("不混算" in item for item in analysis.speech_rate.evidence)
    assert analysis.average_speech_rate_cpm is None
    assert analysis.wording.score is not None and analysis.wording.score > 4.0


def test_text_answer_boundaries_count_for_timing_but_not_speech_rate() -> None:
    analysis = ReportEngine._process_analysis(
        [
            _turn(
                1,
                question="请说明方案。",
                topic="项目深度",
                score=7.0,
                answer="首先给出结论，然后说明依据。",
                input_mode="text",
                duration=45.0,
                recommended_seconds=60,
            )
        ],
        language_mode="zh",
    )

    assert analysis.time_control.score == 9.0
    assert analysis.time_control.scorable is True
    assert "文字" in analysis.time_control.evidence[0]
    assert analysis.average_answer_seconds == 45.0
    assert analysis.speech_rate.score is None
    assert analysis.speech_rate.scorable is False
    assert analysis.average_speech_rate_cpm is None
    assert analysis.fluency.score is None
    assert analysis.fluency.scorable is False
