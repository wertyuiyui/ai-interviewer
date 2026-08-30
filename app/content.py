from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import ROOT_DIR
from .errors import AppError


COMPANIES = {
    "bytedance": "字节跳动",
    "meituan": "美团",
    "tencent": "腾讯",
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
        "company": COMPANIES[company],
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
    else:
        common.update(
            project_weight=0.6,
            fundamentals_weight=0.4,
            followup_preferences=["温和循循善诱", "从现象逐层追问原理"],
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
