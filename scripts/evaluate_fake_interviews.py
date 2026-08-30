#!/usr/bin/env python3
"""Replay the five fake resumes against the real interviewer with a temp DB."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT_RULES = (
    ("request_flow", ("链路", "流程", "请求", "经过", "调用")),
    ("tradeoff", ("为什么", "选型", "取舍", "代价", "trade-off")),
    ("metrics", ("指标", "口径", "压测", "提升", "数据", "qps", "p95")),
    ("failure_boundary", ("故障", "失败", "异常", "一致性", "边界", "不成立")),
    ("scaling", ("十倍", "扩容", "容量", "流量扩大", "瓶颈")),
    ("project_overview", ("真实问题", "使用场景", "做什么", "背景", "解决")),
    ("ownership", ("负责", "职责", "亲自", "本人", "哪部分")),
)
STAGE_INTENTS = {
    "self_intro": ("self_intro",),
    "hr_fit": ("hr_fit", "hr_fit_followup"),
    "career_planning": ("career_planning", "career_followup"),
    "compensation": ("compensation", "compensation_followup"),
    "candidate_questions": ("candidate_questions",),
}
RUNS = {
    "01_java_ecommerce_backend": ("technical", "alibaba", "Java 业务后端", 0, 5),
    "02_go_high_concurrency_backend": ("technical_hr", "tencent", "Go 高并发后端", 1, 5),
    "03_cloud_native_infrastructure": ("hr", "huawei", "云原生平台后端", 0, 7),
    "04_python_ai_backend": ("technical", "baidu", "Python AI 工程后端", 1, 5),
    "05_weak_project_candidate": ("technical", "meituan", "通用后端", 3, 5),
}
LEAK_MARKERS = (
    "next_question",
    "anchor_keyword",
    "pressure_action",
    "system prompt",
    "interview_context",
    "服务端必须",
    "内部规则",
)


def _answer_map(profile: dict) -> dict[str, str]:
    return {item["intent"]: item["answer"] for item in profile["answers"]}


def choose_answer(
    profile: dict, stage: dict, question: str, used_intents: set[str]
) -> tuple[str, str]:
    answers = _answer_map(profile)
    stage_id = stage["current"]["id"]
    turn_count = int(stage["current"]["turn_count"])
    lowered = question.casefold()
    if stage_id == "project_deep_dive":
        ranked = sorted(
            (
                (sum(marker in lowered for marker in markers), -index, intent)
                for index, (intent, markers) in enumerate(PROJECT_RULES)
            ),
            reverse=True,
        )
        if ranked[0][0]:
            intent = ranked[0][2]
            if intent in used_intents:
                return "fallback_unknown", answers["fallback_unknown"]
            return intent, answers[intent]
        order = ("project_overview", "ownership", "request_flow", "tradeoff")
        intent = next((item for item in order if item not in used_intents), order[-1])
        return intent, answers[intent]
    if stage_id == "fundamentals":
        intent = "fundamentals_followup" if turn_count % 2 else "fundamentals"
        technical_tokens = {
            token.casefold()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+.#_-]{2,}", answers[intent])
        }
        if technical_tokens and not any(token in lowered for token in technical_tokens):
            intent = "fallback_unknown"
        return intent, answers[intent]
    if stage_id == "coding":
        answer = answers["coding"]
        keywords = re.findall(r"LRU|滑动窗口|二叉树|链表|数组", answer, re.I)
        intent = "coding" if any(item.casefold() in lowered for item in keywords) else "fallback_unknown"
        return intent, answers[intent]
    intents = STAGE_INTENTS[stage_id]
    intent = intents[min(turn_count, len(intents) - 1)]
    return intent, answers[intent]


def inspect_question(question: str, previous: list[str]) -> list[str]:
    issues = []
    lowered = question.casefold()
    if sum(question.count(mark) for mark in ("?", "？")) > 1:
        issues.append("multiple_questions")
    if any(marker in lowered for marker in LEAK_MARKERS):
        issues.append("internal_instruction_leak")
    if any(SequenceMatcher(None, question, old).ratio() >= 0.86 for old in previous):
        issues.append("near_duplicate_question")
    return issues


async def run(env_file: Path | None, output: Path, only_slug: str = "") -> int:
    if env_file:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=True)

    from app.config import get_settings
    from app.db import Database
    from app.interview_engine import InterviewEngine
    from app.resume import ResumeParser, extract_pdf_text
    from app.schemas import InterviewCreate

    fixture = json.loads((ROOT / "testdata/fake_interview_answers.json").read_text())
    profiles = {item["slug"]: item for item in fixture["profiles"]}
    records: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="ai-interviewer-skill-eval-") as temp_dir:
        settings = replace(
            get_settings(),
            db_path=Path(temp_dir) / "eval.db",
            mock_llm=False,
            voice_mode="L3",
        )
        if not settings.has_api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required for a real-model evaluation")
        database = Database(settings)
        await database.initialize()
        parser = ResumeParser(settings)
        engine = InterviewEngine(database, settings)

        for slug, (interview_type, company, specialization, stress, max_turns) in RUNS.items():
            if only_slug and slug != only_slug:
                continue
            profile = profiles[slug]
            pdf = ROOT / profile["source_pdf"]
            resume = await parser.parse(extract_pdf_text(pdf.read_bytes(), settings.max_pdf_mb))
            created = await engine.create(
                InterviewCreate(
                    client_id=f"skill-eval-{slug}",
                    resume=resume,
                    company=company,
                    interview_type=interview_type,
                    specialization=specialization,
                    language_mode="zh",
                    answer_mode="text",
                    stress_level=stress,
                    duration_minutes=None,
                    memory_enabled=False,
                )
            )
            await database.start_interview(created["id"])
            stage = created["stage"]
            question = created["initial_question"]
            previous: list[str] = []
            used_intents: set[str] = set()
            transcript = []
            for _ in range(max_turns):
                intent, answer = choose_answer(profile, stage, question, used_intents)
                used_intents.add(intent)
                result = await engine.answer(created["id"], answer, input_mode="text")
                issues = inspect_question(result.question, [*previous, question])
                if intent == "fallback_unknown" and (
                    result.turn.anchor_keyword
                    or "什么时候会不成立" in result.question
                    or "什么情况下会不成立" in result.question
                ):
                    issues.append("unknown_not_switched")
                if result.turn.score_source != "llm":
                    issues.append("model_fallback")
                if result.resume_selection_warning:
                    issues.append("unexpected_resume_warning")
                transcript.append(
                    {
                        "stage": stage["current"]["id"],
                        "question": question,
                        "answer_intent": intent,
                        "score": result.turn.score,
                        "failed": result.turn.failed,
                        "anchor": result.turn.anchor_keyword,
                        "next_stage": result.stage["current"]["id"],
                        "next_question": result.question,
                        "issues": issues,
                    }
                )
                previous.append(question)
                stage, question = result.stage, result.question
                if result.ended:
                    break
            records.append(
                {
                    "slug": slug,
                    "name": profile["name"],
                    "interview_type": interview_type,
                    "parsed": {
                        "education": len(resume.education),
                        "internships": len(resume.internships),
                        "projects": len(resume.projects),
                        "skills": len(resume.skills),
                    },
                    "transcript": transcript,
                }
            )

    issue_counts: dict[str, int] = {}
    for record in records:
        for turn in record["transcript"]:
            for issue in turn["issues"]:
                issue_counts[issue] = issue_counts.get(issue, 0) + 1
    payload = {
        "model": settings.qwen_text_model,
        "real_model": True,
        "profiles": len(records),
        "turns": sum(len(record["transcript"]) for record in records),
        "issues": issue_counts,
        "records": records,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("model", "profiles", "turns", "issues")}, ensure_ascii=False))
    hard_issues = {key: value for key, value in issue_counts.items() if key != "multiple_questions"}
    return 1 if hard_issues else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/tmp/fake-interview-eval.json"))
    parser.add_argument("--slug", choices=RUNS)
    args = parser.parse_args()
    return asyncio.run(run(args.env_file, args.output, args.slug or ""))


if __name__ == "__main__":
    raise SystemExit(main())
