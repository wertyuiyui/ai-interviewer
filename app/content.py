from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
import re
from typing import Any
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
    return dict(value)


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
    """Return company-specific behavioral questions for combined interviews.

    These prompts are intentionally written for undergraduate internship
    candidates.  They look for concrete choices and self-awareness rather
    than importing experienced-hire assumptions about management scope.
    """

    if company not in COMPANIES:
        raise AppError("INVALID_COMPANY", "暂不支持该公司", status_code=422)
    path = ROOT_DIR / "questions" / "hr_internship.json"
    if not path.exists():
        return []
    value = _load_json(str(path))
    companies = value.get("companies") if isinstance(value, dict) else None
    if not isinstance(companies, dict):
        raise RuntimeError(f"综合面题库格式错误：{path}")
    if language_mode == "en":
        return _generic_hr_questions(company, english=True)
    questions = _normalize_questions(companies.get(company, []), path)
    return questions or _generic_hr_questions(company, english=False)


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

    path = ROOT_DIR / "questions" / "real_practice_bank.json"
    if not path.exists():
        return []
    value = _load_json(str(path))
    records = value.get("questions") if isinstance(value, dict) else None
    if not isinstance(records, list):
        raise RuntimeError(f"英文题库格式错误：{path}")
    categories = {
        "ai_engineering": "AI Engineering",
        "behavioral": "Behavioral",
        "system_design": "System Design",
        "technical": "Backend Fundamentals",
    }
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
        "English behavioral": "Behavioral",
    }
    result: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        prompt = raw.get("prompt")
        question = str(prompt.get("en") or "").strip() if isinstance(prompt, dict) else ""
        if (
            not question
            or raw.get("status") != "approved"
            or raw.get("authenticity") != "licensed_bank"
            or not all(
                str(raw.get(key) or "").strip()
                for key in ("source_id", "source_path", "revision", "license")
            )
        ):
            continue
        topics = [str(item).strip() for item in (raw.get("topics") or []) if str(item).strip()]
        kind = str(raw.get("kind") or "technical")
        category = categories.get(kind, "Backend Fundamentals")
        if kind == "technical":
            category = next(
                (topic_categories[topic] for topic in topics if topic in topic_categories),
                category,
            )
        result.append(
            {
                "id": f"{raw.get('id')}-en",
                "language": "en",
                "category": category,
                "topic": " / ".join(topics) or categories.get(kind, "Backend Fundamentals"),
                "question": question,
                "followups": [],
                "difficulty": str(raw.get("difficulty") or "medium"),
                "source_id": raw["source_id"],
                "source_path": raw["source_path"],
                "revision": raw["revision"],
                "license": raw["license"],
                "authenticity": raw["authenticity"],
                "status": raw["status"],
            }
        )
    return result


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
