from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from .config import ROOT_DIR
from .errors import AppError


_BUILTIN_COMPANIES = {
    "bytedance": "字节跳动",
    "meituan": "美团",
    "tencent": "腾讯",
    "alibaba": "阿里巴巴",
    "baidu": "百度",
    "huawei": "华为",
}


def _discover_companies() -> dict[str, str]:
    """Build the company registry from versioned style cards.

    Keeping a small built-in registry preserves deterministic ordering and
    lets a newly-added card become selectable without another schema change.
    A card filename is also treated as untrusted input: only simple slugs are
    admitted and malformed JSON never prevents the app from starting.
    """

    companies = dict(_BUILTIN_COMPANIES)
    card_dir = ROOT_DIR / "cards"
    for path in sorted(card_dir.glob("*_backend.json")):
        company = path.name.removesuffix("_backend.json")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", company):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        label = str(
            value.get("company_name")
            or value.get("display_name")
            or companies.get(company)
            or company
        ).strip()
        if label:
            companies[company] = label[:40]
    return companies


COMPANIES = _discover_companies()
COMPANY_ENGLISH_NAMES = {
    "bytedance": "ByteDance",
    "meituan": "Meituan",
    "tencent": "Tencent",
    "alibaba": "Alibaba",
    "baidu": "Baidu",
    "huawei": "Huawei",
}

SPECIALIZATIONS = [
    "通用后端",
    "Java 后端",
    "Go 后端",
    "C++ 后端",
    "Python 后端",
    "基础架构",
    "云原生与微服务",
    "数据库与存储",
    "消息队列与中间件",
    "分布式系统",
    "AI 工程后端 / LLM Infra",
]

# Compatibility fallback for deployments where the reviewed bank is missing
# or temporarily unreadable.  The API catalog itself is derived at request
# time from the checked-in bank via ``load_specialization_catalog``.
SPECIALIZATION_FALLBACKS = tuple(SPECIALIZATIONS)

AI_SPECIALIZATION_KEYWORDS = (
    "ai后端",
    "ai 后端",
    "ai应用",
    "ai 应用",
    "ai工程",
    "ai 工程",
    "llm",
    "大模型",
    "模型服务",
    "推理服务",
    "模型网关",
    "agent",
    "智能体",
    "ai infra",
    "ai-infra",
    "inference",
    "推理系统",
)


@lru_cache(maxsize=24)
def _load_json(path: str) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _is_reviewed_real_question(raw: Any) -> bool:
    return bool(
        isinstance(raw, dict)
        and raw.get("status") == "approved"
        and raw.get("authenticity") == "licensed_bank"
        and all(
            str(raw.get(key) or "").strip()
            for key in ("id", "source_id", "source_path", "revision", "license")
        )
    )


def _reviewed_real_records() -> list[dict[str, Any]]:
    """Return only record-level verified entries from the licensed bank."""

    question_dir = ROOT_DIR / "questions"
    paths = [
        question_dir / "real_practice_bank.json",
        question_dir / "real_practice_bank_extended.json",
    ]
    records_by_id: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            value = _load_json(str(path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        records = value.get("questions") if isinstance(value, dict) else None
        if not isinstance(records, list):
            continue
        for raw in records:
            if _is_reviewed_real_question(raw):
                records_by_id[str(raw["id"])] = dict(raw)
    return list(records_by_id.values())


def _real_question_category(
    raw: dict[str, Any], topics: list[str], language_mode: str
) -> str:
    kind = str(raw.get("kind") or "technical").strip().lower()
    if language_mode == "en":
        if kind == "behavioral":
            return "Behavioral"
        if kind == "coding":
            return "Coding Thought"
        if kind == "ai_engineering":
            return "AI Engineering"
        if kind == "system_design":
            return "System Design"
        topic_categories = {
            "Java": "Java",
            "MySQL": "MySQL",
            "Redis": "Redis",
            "Go": "Go",
            "并发": "Concurrency",
            "操作系统": "Operating Systems",
            "计算机网络": "Networking",
            "系统设计": "System Design",
            "AI 工程": "AI Engineering",
        }
        return next(
            (topic_categories[topic] for topic in topics if topic in topic_categories),
            "Backend Fundamentals",
        )
    if kind == "behavioral":
        return "综合面"
    if kind == "coding":
        return "手撕思路"
    if kind == "ai_engineering":
        return "AI工程"
    if kind == "system_design":
        return "系统设计"
    topic_categories = {
        "Java": "Java",
        "MySQL": "MySQL",
        "Redis": "Redis",
        "Go": "Go",
        "并发": "Java并发",
        "操作系统": "操作系统",
        "计算机网络": "计网",
        "系统设计": "系统设计",
        "AI 工程": "AI工程",
    }
    return next(
        (topic_categories[topic] for topic in topics if topic in topic_categories),
        "后端基础",
    )


def load_real_interview_question_bank(
    *,
    language_mode: str = "bilingual",
    company: str | None = None,
    kinds: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Normalize the reviewed practice bank for server-controlled interviews.

    Chinese and bilingual interviews use the reviewed Chinese wording; pure
    English interviews use the paired English wording.  No prompt is admitted
    unless it retains the approved status and complete pinned-source metadata.
    """

    language = "en" if language_mode == "en" else "zh"
    requested_kinds = {str(kind).strip().lower() for kind in kinds or set()}
    result: list[dict[str, Any]] = []
    for raw in _reviewed_real_records():
        kind = str(raw.get("kind") or "technical").strip().lower()
        if requested_kinds and kind not in requested_kinds:
            continue
        company_tags = {
            str(item).strip().lower()
            for item in (raw.get("company_tags") or [])
            if str(item).strip()
        }
        if company and company_tags and not (
            company.lower() in company_tags
            or company_tags.intersection({"all", "global", "global_tech", "overseas"})
        ):
            continue
        prompt = raw.get("prompt")
        question = (
            str(prompt.get(language) or "").strip()
            if isinstance(prompt, dict)
            else ""
        )
        if not question:
            continue
        topics = [
            str(item).strip()
            for item in (raw.get("topics") or [])
            if str(item).strip()
        ]
        result.append(
            {
                "id": f"{raw['id']}-{language}",
                "bank_id": str(raw["id"]),
                "kind": kind,
                "language": language,
                "category": _real_question_category(raw, topics, language_mode),
                "topic": " / ".join(topics)
                or ("Behavioral" if language == "en" else "综合面"),
                "topics": topics,
                "question": question,
                "followups": [],
                "difficulty": str(raw.get("difficulty") or "medium"),
                "suggested_seconds": int(raw.get("suggested_seconds") or 90),
                "company_tags": sorted(company_tags),
                "direction_tags": [
                    str(item).strip().lower()
                    for item in (raw.get("direction_tags") or [])
                    if str(item).strip()
                ],
                "scoring": raw.get("scoring") or {},
                "source_id": raw["source_id"],
                "source_path": raw["source_path"],
                "revision": raw["revision"],
                "license": raw["license"],
                "authenticity": raw["authenticity"],
                "status": raw["status"],
            }
        )
    return result


def load_specialization_catalog() -> list[str]:
    """Derive supported role directions from reviewed tag/topic coverage."""

    records = [
        raw
        for raw in _reviewed_real_records()
        if str(raw.get("kind") or "technical") != "behavioral"
    ]
    if not records:
        return list(SPECIALIZATION_FALLBACKS)
    covered_tags = {
        str(tag).strip().lower()
        for raw in records
        for tag in (raw.get("direction_tags") or [])
        if str(tag).strip()
    }
    covered_topics = {
        str(topic).strip().casefold()
        for raw in records
        for topic in (raw.get("topics") or [])
        if str(topic).strip()
    }
    # Labels are presentation-only.  Inclusion is entirely evidence-driven:
    # every returned preset has at least one reviewed direction tag or topic.
    descriptors = [
        ("通用后端", {"backend"}, set()),
        ("Java 后端", {"java", "jvm"}, {"java", "jvm"}),
        ("Go 后端", {"go"}, {"go"}),
        ("并发与高性能", {"concurrency", "performance"}, {"并发"}),
        ("基础架构", {"systems"}, {"操作系统"}),
        (
            "云原生与微服务",
            {"infrastructure", "protocol_design"},
            {"rpc", "反向代理"},
        ),
        ("数据库与存储", {"database"}, {"mysql"}),
        ("缓存与 Redis", {"cache"}, {"redis"}),
        ("网络与服务治理", {"network"}, {"计算机网络"}),
        (
            "消息队列与中间件",
            {"messaging"},
            {"消息队列"},
        ),
        (
            "可观测性与故障排查",
            {"observability", "reliability"},
            {"可观测性"},
        ),
        (
            "分布式系统",
            {"distributed_system", "system_design"},
            {"系统设计"},
        ),
        (
            "AI 工程后端 / LLM Infra",
            {"ai_engineering", "llm_infra", "rag"},
            {"ai 工程"},
        ),
    ]
    catalog = [
        label
        for label, tags, topics in descriptors
        if covered_tags.intersection(tags) or covered_topics.intersection(topics)
    ]
    return catalog or list(SPECIALIZATION_FALLBACKS)


def load_style_card(company: str) -> dict[str, Any]:
    if company not in COMPANIES:
        raise AppError("INVALID_COMPANY", "暂不支持该公司", status_code=422)
    path = ROOT_DIR / "cards" / f"{company}_backend.json"
    if not path.exists():
        return _fallback_card(company)
    value = dict(_load_json(str(path)))
    if not isinstance(value, dict):
        raise RuntimeError(f"风格卡格式错误：{path}")
    if "stage_ratios" in value:
        value.setdefault("stage_ratio", value["stage_ratios"])
    weights = value.get("project_vs_fundamentals_weights") or {}
    value.setdefault("project_weight", weights.get("project", 0.5))
    value.setdefault("fundamentals_weight", weights.get("fundamentals", 0.5))
    value.setdefault(
        "stress_default", value.get("default_pressure_enabled", False)
    )
    return value


@lru_cache(maxsize=1)
def _load_interviewer_core_skill() -> dict[str, Any]:
    """Load the company-neutral contract shared by every interviewer.

    Company skills express evidence-backed style preferences.  This core skill
    holds the invariants that must not vary by company, transport, or pressure
    level.  Keeping it nested also makes that boundary visible to the prompt
    without duplicating the same policy across six files.
    """

    path = ROOT_DIR / "interview_skills" / "interviewer_core.json"
    value = _load_json(str(path))
    required = {
        "schema_version",
        "name",
        "scope",
        "role_contract",
        "input_policy",
        "phase_invariants",
        "turn_policy",
        "adaptive_policy",
        "evidence_policy",
        "question_policy",
        "assessment_policy",
        "termination_policy",
        "fairness_policy",
        "modality_policy",
        "safety_policy",
        "provenance_policy",
    }
    if not isinstance(value, dict) or not required <= set(value):
        missing = sorted(required - set(value if isinstance(value, dict) else {}))
        raise RuntimeError(f"核心面试官 skill 格式错误：{path}，缺少 {missing}")
    if value.get("name") != "core-interviewer":
        raise RuntimeError(f"核心面试官 skill 标识不匹配：{path}")
    if value.get("scope") != "backend-intern-first-round-practice":
        raise RuntimeError(f"核心面试官 skill 范围不匹配：{path}")
    if value.get("schema_version") != "1.0":
        raise RuntimeError(f"核心面试官 skill 版本不支持：{path}")
    object_keys = required - {"schema_version", "name", "scope", "phase_invariants"}
    malformed = sorted(key for key in object_keys if not isinstance(value.get(key), dict))
    if not isinstance(value.get("phase_invariants"), list):
        malformed.append("phase_invariants")
    if malformed:
        raise RuntimeError(f"核心面试官 skill 字段类型错误：{path}，字段 {malformed}")
    nested_required = {
        "role_contract": {"goal", "candidate_level", "during_interview", "one_primary_intent_per_turn"},
        "turn_policy": {"sequence", "permitted_actions", "response_anchor_rule", "context_anchor_rule", "forbidden_output"},
        "assessment_policy": {"visibility", "evidence_required", "not_observed", "explicit_unknown", "separate_findings", "behavioral_boundary"},
        "termination_policy": {"server_authority", "signals", "rule"},
    }
    incomplete = sorted(
        key
        for key, fields in nested_required.items()
        if not fields <= set(value[key])
    )
    if incomplete:
        raise RuntimeError(f"核心面试官 skill 子结构错误：{path}，字段 {incomplete}")
    return dict(value)


def load_interviewer_core_skill() -> dict[str, Any]:
    """Return an isolated copy so callers cannot mutate another company skill."""

    return deepcopy(_load_interviewer_core_skill())


@lru_cache(maxsize=24)
def load_interview_skill(company: str) -> dict[str, Any]:
    """Load the versioned company behavior distilled from research material.

    Style cards remain the compatibility source for weights already consumed
    elsewhere.  Skills are narrower runtime instructions: they state how to
    sequence, follow up, apply pressure, and adapt language for one company.
    """

    if company not in COMPANIES:
        raise AppError("INVALID_COMPANY", "暂不支持该公司", status_code=422)
    path = ROOT_DIR / "interview_skills" / f"{company}_backend.json"
    if not path.exists():
        card = load_style_card(company)
        return {
            "version": "compat-card-1.0",
            "company": company,
            "display_name": COMPANIES[company],
            "evidence_level": "compatibility",
            "flow": list((card.get("stage_ratio") or {}).keys()),
            "tone": str(card.get("interviewer_persona") or "专业、自然、追问具体"),
            "topic_weights": {
                "project": float(card.get("project_weight", 0.55)),
                "fundamentals": float(card.get("fundamentals_weight", 0.45)),
            },
            "question_topic_priorities": [],
            "difficulty_ladder": ["事实", "原理", "边界", "变化条件"],
            "project_followup_rules": card.get("followup_preferences", []),
            "pressure_policy": card.get("pressure_profile", {}),
            "hr_focus": card.get("technical_hr_focus", []),
            "language_profiles": {
                "zh": "自然中文，技术术语可保留英文",
                "bilingual": "中文为主，并安排简短英文追问",
                "en": "所有候选人可见内容只使用自然英文",
            },
            "source_refs": [],
            "interviewer_core": load_interviewer_core_skill(),
        }
    value = _load_json(str(path))
    required = {
        "version",
        "company",
        "display_name",
        "evidence_level",
        "flow",
        "tone",
        "topic_weights",
        "question_topic_priorities",
        "difficulty_ladder",
        "project_followup_rules",
        "pressure_policy",
        "hr_focus",
        "language_profiles",
        "source_refs",
    }
    if not isinstance(value, dict) or not required <= set(value):
        missing = sorted(required - set(value if isinstance(value, dict) else {}))
        raise RuntimeError(f"公司面试 skill 格式错误：{path}，缺少 {missing}")
    if value.get("company") != company:
        raise RuntimeError(f"公司面试 skill 标识不匹配：{path}")
    compiled = dict(value)
    compiled["interviewer_core"] = load_interviewer_core_skill()
    return compiled


_QUESTION_TOPIC_ALIASES: dict[str, set[str]] = {
    "手撕思路": {"coding", "coding thought", "手撕思路", "algorithm", "算法"},
    "并发": {"concurrency", "performance", "并发", "java并发"},
    "计算机网络": {"network", "networking", "计算机网络", "计网"},
    "操作系统": {"operating systems", "operating_system", "操作系统"},
    "系统设计": {"system design", "system_design", "系统设计"},
    "分布式系统": {"distributed_system", "distributed system", "分布式系统"},
    "消息队列": {"messaging", "message queue", "消息队列", "mq"},
    "数据结构": {"data structure", "data_structure", "数据结构", "coding"},
    "可观测性": {"observability", "可观测性", "metrics", "logging", "trace"},
    "故障排查": {"reliability", "observability", "故障", "排查", "debugging"},
    "可靠性": {"reliability", "availability", "可靠性", "可用性", "故障"},
}


def company_question_rank(
    company: str | None, item: Mapping[str, Any]
) -> int:
    """Rank one common-bank record by a company's evidence-backed topic order.

    The question wording remains from the shared reviewed public bank.  Company
    skills only change ordering; they never relabel a common question as a
    company-exclusive interview question.
    """

    if not company:
        return 0
    priorities = load_interview_skill(company).get("question_topic_priorities") or []
    raw_signals = [
        item.get("kind", ""),
        item.get("category", ""),
        item.get("topic", ""),
        *(item.get("topics") or []),
        *(item.get("direction_tags") or []),
    ]
    signals = {
        str(value).strip().casefold()
        for value in raw_signals
        if str(value).strip()
    }
    haystack = " ".join(sorted(signals))
    for index, raw_priority in enumerate(priorities):
        priority = str(raw_priority).strip()
        if not priority:
            continue
        aliases = {
            priority.casefold(),
            *{
                alias.casefold()
                for alias in _QUESTION_TOPIC_ALIASES.get(priority, set())
            },
        }
        if aliases.intersection(signals) or any(
            alias
            and not (alias.isascii() and alias.isalnum() and len(alias) < 4)
            and alias in haystack
            for alias in aliases
        ):
            return index
    return len(priorities) + 1


def _normalize_questions(value: Any, path: Path) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("questions", [])
    if not isinstance(value, list):
        raise RuntimeError(f"题库格式错误：{path}")
    category_names = {
        "mysql": "MySQL",
        "redis": "Redis",
        "java_concurrency": "Java并发",
        "concurrency": "Java并发",
        "networking": "计网",
        "network": "计网",
        "coding_thought": "手撕思路",
        "coding": "手撕思路",
        "ai_backend": "AI工程",
        "ai_engineering": "AI工程",
    }
    normalized: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        raw_category = str(item.get("category", "其他"))
        item["category"] = category_names.get(raw_category, raw_category)
        normalized.append(item)
    return normalized


def load_question_bank(company: str) -> list[dict[str, Any]]:
    if company not in COMPANIES:
        raise AppError("INVALID_COMPANY", "暂不支持该公司", status_code=422)
    path = ROOT_DIR / "questions" / f"{company}_backend.json"
    if not path.exists():
        return _fallback_questions()
    return _normalize_questions(_load_json(str(path)), path)


def load_hr_question_bank(
    company: str, language_mode: str = "bilingual"
) -> list[dict[str, Any]]:
    """Return three HR stages made only from reviewed behavioral questions."""

    if company not in COMPANIES:
        raise AppError("INVALID_COMPANY", "暂不支持该公司", status_code=422)
    reviewed = load_real_interview_question_bank(
        language_mode=language_mode,
        company=company,
        kinds={"behavioral"},
    )
    by_bank_id = {str(item.get("bank_id")): item for item in reviewed}
    stages = [
        (
            "tih-behavior-001",
            "Values and company fit" if language_mode == "en" else "价值观与公司契合",
            ("tih-behavior-006", "tih-behavior-004"),
        ),
        (
            "tih-behavior-008",
            "Career planning and choices" if language_mode == "en" else "人生规划与选择",
            ("tih-behavior-005", "tih-behavior-002"),
        ),
        (
            "tih-behavior-007",
            "Compensation expectations" if language_mode == "en" else "薪酬期待",
            ("tih-behavior-003", "tih-behavior-001"),
        ),
    ]
    result: list[dict[str, Any]] = []
    for primary_id, topic, followup_ids in stages:
        primary = by_bank_id.get(primary_id)
        if not primary:
            continue
        item = dict(primary)
        item.update(
            category="Behavioral" if language_mode == "en" else "综合面",
            topic=topic,
            followups=[
                str(by_bank_id[followup_id]["question"])
                for followup_id in followup_ids
                if followup_id in by_bank_id
            ],
        )
        result.append(item)
    return result


def _generic_hr_questions(
    company: str, *, english: bool
) -> list[dict[str, Any]]:
    label = (
        COMPANY_ENGLISH_NAMES.get(company, company)
        if english
        else COMPANIES[company]
    )
    if english:
        seeds = [
            (
                "values",
                "Values and company fit",
                f"Tell me about a time you had to make a difficult trade-off under delivery pressure. What did you do, and how would that way of working fit a team at {label}?",
                [
                    "What evidence did you use to make the decision?",
                    "What would you change if the outcome fell short?",
                ],
            ),
            (
                "planning",
                "Career planning and choices",
                "Why are you pursuing a backend engineering internship now, and which capabilities do you want to build over the next two to three years?",
                [
                    "What are you doing this semester to make that plan measurable?",
                    "How would you choose between two internships in different technical directions?",
                ],
            ),
            (
                "compensation",
                "Compensation expectations",
                "What are your compensation expectations for this internship, and how would you rank compensation, mentorship, and role fit?",
                [
                    "What information did you use to form that expectation?",
                    "Which parts are negotiable, and how would you communicate that?",
                ],
            ),
        ]
    else:
        seeds = [
            (
                "values",
                "价值观与公司契合",
                f"讲一次你在交付压力下做艰难取舍的真实经历。你当时怎么判断，这种做事方式和{label}的团队有哪些契合或需要适应的地方？",
                ["你依据了哪些事实？", "如果结果不理想，你会怎样复盘？"],
            ),
            (
                "planning",
                "人生规划与选择",
                "你为什么在这个阶段选择后端实习，未来两三年最想形成哪些可验证的能力？",
                ["你这学期正在采取什么行动？", "两个方向不同的机会你会怎样选？"],
            ),
            (
                "compensation",
                "薪酬期待",
                "你对这段实习的薪酬有什么期待？薪酬、导师带教和方向匹配之间你会怎样排序？",
                ["你的预期参考了哪些信息？", "哪些条件可以沟通？"],
            ),
        ]
    return [
        {
            "id": f"{company}-hr-{suffix}-generic",
            "category": "Behavioral" if english else "综合面",
            "topic": topic,
            "question": question,
            "followups": followups,
            "difficulty": "medium",
            "language": "en" if english else "zh",
        }
        for suffix, topic, question, followups in seeds
    ]


def load_english_question_bank() -> list[dict[str, Any]]:
    """Load the English variants from the reviewed permissive-license bank.

    The research index also lists useful GPL and CC BY-SA repositories.  Those
    remain link-only study leads; runtime interview prompts are restricted to
    the same pinned Apache-2.0/MIT records used by quick practice.
    """

    return load_real_interview_question_bank(language_mode="en")


def is_ai_specialization(specialization: str) -> bool:
    compact = " ".join(str(specialization or "").strip().lower().split())
    return any(keyword in compact for keyword in AI_SPECIALIZATION_KEYWORDS)


def load_specialization_question_bank(specialization: str) -> list[dict[str, Any]]:
    if not is_ai_specialization(specialization):
        return []
    path = ROOT_DIR / "questions" / "aris_ai_backend.json"
    if not path.exists():
        return []
    return _normalize_questions(_load_json(str(path)), path)


def load_experience_question_bank(company: str) -> list[dict[str, Any]]:
    """Return short, rewritten prompts distilled from public interview reports.

    The source posts are never copied into the runtime prompt.  Each item only
    stores our own question wording and stable source identifiers that resolve
    through ``resources/source_catalog.json``.
    """

    if company not in COMPANIES:
        raise AppError("INVALID_COMPANY", "暂不支持该公司", status_code=422)
    path = ROOT_DIR / "questions" / "recent_experience_backend.json"
    if not path.exists():
        return []
    value = _load_json(str(path))
    companies = value.get("companies") if isinstance(value, dict) else None
    if not isinstance(companies, dict):
        raise RuntimeError(f"面经精选题库格式错误：{path}")
    return _normalize_questions(companies.get(company, []), path)


def load_current_research_question_bank(
    specialization: str,
) -> list[dict[str, Any]]:
    """Return discussion-level current research prompts for AI backend roles."""

    if not is_ai_specialization(specialization):
        return []
    path = ROOT_DIR / "questions" / "current_research_discussion.json"
    if not path.exists():
        return []
    return _normalize_questions(_load_json(str(path)), path)


def load_project_question_bank(specialization: str) -> list[dict[str, Any]]:
    """Return open-source-inspired scenarios relevant to the selected role."""

    path = ROOT_DIR / "questions" / "open_source_project_backend.json"
    if not path.exists():
        return []
    questions = _normalize_questions(_load_json(str(path)), path)
    normalized = " ".join(str(specialization or "通用后端").strip().lower().split())
    matches: list[dict[str, Any]] = []
    for item in questions:
        applicability = {
            " ".join(str(value).strip().lower().split())
            for value in item.get("applicability", [])
        }
        keywords = {
            str(value).strip().lower() for value in item.get("keywords", [])
        }
        if normalized in applicability or any(
            keyword and keyword in normalized for keyword in keywords
        ):
            matches.append(item)
    return matches


def load_source_catalog() -> dict[str, Any]:
    """Load the transparent, link-only provenance catalog for curated prompts."""

    path = ROOT_DIR / "resources" / "source_catalog.json"
    if not path.exists():
        return {"schema_version": "1.0", "sources": []}
    value = _load_json(str(path))
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
        raise RuntimeError(f"资料来源目录格式错误：{path}")
    allowed_kinds = {
        "question_bank",
        "github_project",
        "interview_experience",
        "research",
    }
    seen_ids: set[str] = set()
    for source in value["sources"]:
        if not isinstance(source, dict):
            raise RuntimeError(f"资料来源条目格式错误：{path}")
        source_id = str(source.get("id") or "").strip()
        source_url = str(source.get("url") or "").strip()
        if not source_id or source_id in seen_ids:
            raise RuntimeError(f"资料来源 ID 缺失或重复：{source_id or '<empty>'}")
        if source.get("kind") not in allowed_kinds:
            raise RuntimeError(f"资料来源类型不受支持：{source_id}")
        if urlparse(source_url).scheme != "https":
            raise RuntimeError(f"资料来源必须使用 HTTPS：{source_id}")
        if "license_spdx" not in source or not str(
            source.get("usage_mode") or ""
        ).strip():
            raise RuntimeError(f"资料来源缺少授权或使用策略：{source_id}")
        seen_ids.add(source_id)
    return value


def load_topic_links() -> dict[str, dict[str, str]]:
    path = ROOT_DIR / "resources" / "topic_links.json"
    if path.exists():
        value = _load_json(str(path))
        if isinstance(value, dict):
            topics = value.get("topics")
            if isinstance(topics, dict):
                flattened: dict[str, dict[str, str]] = {}
                for topic, resources in topics.items():
                    if not isinstance(resources, list) or not resources:
                        continue
                    resource = resources[0]
                    if not isinstance(resource, dict):
                        continue
                    source = str(resource.get("source", "JavaGuide"))
                    flattened[str(topic)] = {
                        "title": "CodeTop" if source == "CodeTop" else "JavaGuide",
                        "url": str(resource.get("url", "https://javaguide.cn/")),
                    }
                flattened["default"] = {
                    "title": "JavaGuide",
                    "url": "https://javaguide.cn/",
                }
                return flattened
            return value
    return {
        "default": {
            "title": "JavaGuide",
            "url": "https://javaguide.cn/",
        },
        "手撕思路": {"title": "CodeTop", "url": "https://codetop.cc/home"},
    }


def _fallback_card(company: str) -> dict[str, Any]:
    common = {
        "company": company,
        "company_name": COMPANIES[company],
        "role": "后端开发实习生",
        "stage_ratio": {
            "自我介绍": 0.08,
            "项目深挖": 0.38,
            "八股": 0.28,
            "手撕思路": 0.18,
            "反问": 0.08,
        },
        "project_weight": 0.55,
        "fundamentals_weight": 0.45,
        "coding_difficulty": "medium",
        "stress_default": False,
        "followup_preferences": [],
    }
    if company == "bytedance":
        common.update(
            project_weight=0.5,
            fundamentals_weight=0.5,
            coding_difficulty="medium-hard",
            stress_default=True,
            followup_preferences=["连环追问到底", "每轮必问手撕思路", "主动施压"],
        )
    elif company == "meituan":
        common.update(
            project_weight=0.65,
            fundamentals_weight=0.35,
            followup_preferences=["项目深挖为主", "八股从项目技术栈延伸"],
        )
    elif company == "tencent":
        common.update(
            project_weight=0.6,
            fundamentals_weight=0.4,
            followup_preferences=["温和循循善诱", "从现象逐层追问原理"],
        )
    else:
        common.update(
            followup_preferences=[
                "从候选人的项目技术选型切入",
                "追问个人贡献、量化证据、故障处理和系统边界",
            ],
        )
    return common


def _fallback_questions() -> list[dict[str, Any]]:
    seed = [
        ("MySQL", "索引", "B+ 树索引为什么适合范围查询？"),
        ("MySQL", "事务", "MySQL 的四种隔离级别分别解决什么问题？"),
        ("Redis", "缓存", "缓存穿透、击穿、雪崩分别怎么处理？"),
        ("Redis", "持久化", "RDB 和 AOF 如何取舍？"),
        ("Java并发", "线程池", "线程池核心参数如何根据业务设置？"),
        ("Java并发", "锁", "synchronized 与 ReentrantLock 有什么区别？"),
        ("计网", "TCP", "TCP 为什么需要四次挥手？"),
        ("计网", "HTTP", "HTTP/1.1、HTTP/2、HTTP/3 的关键差异是什么？"),
        ("手撕思路", "LRU", "口述实现一个线程安全 LRU 缓存的思路。"),
    ]
    return [
        {
            "id": f"fallback-{index}",
            "category": category,
            "topic": topic,
            "question": question,
            "followups": ["底层原理是什么？", "边界情况如何处理？"],
            "difficulty": "medium",
        }
        for index, (category, topic, question) in enumerate(seed, 1)
    ]
