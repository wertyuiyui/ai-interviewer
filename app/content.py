from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import ROOT_DIR
from .errors import AppError


COMPANIES = {
    "bytedance": "字节跳动",
    "meituan": "美团",
    "tencent": "腾讯",
}


@lru_cache(maxsize=12)
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


def load_question_bank(company: str) -> list[dict[str, Any]]:
    if company not in COMPANIES:
        raise AppError("INVALID_COMPANY", "暂不支持该公司", status_code=422)
    path = ROOT_DIR / "questions" / f"{company}_backend.json"
    if not path.exists():
        return _fallback_questions()
    value = _load_json(str(path))
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
