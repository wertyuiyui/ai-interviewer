from __future__ import annotations


PROJECT_MARKERS = {
    "project_depth",
    "项目深度",
    "项目深挖",
    "业务背景",
    "个人职责",
    "请求链路",
    "技术选型理由",
    "难点与故障",
    "数据指标口径",
    "trade-off",
}


def canonical_topic(topic: str, category: str = "") -> str:
    """Map model/question-bank labels to stable cross-interview domains."""

    raw = f"{topic} {category}".strip()
    lowered = raw.lower()
    # Behavioral turns intentionally use the fixed technical rubric's
    # ``communication`` dimension, but that rubric category must not erase the
    # actual HR subject from cross-interview memory.  Keep these checks before
    # the generic communication alias and accept both report languages.
    if any(
        marker in lowered
        for marker in (
            "compensation",
            "salary",
            "薪酬",
            "待遇沟通",
        )
    ):
        return "薪酬沟通"
    if any(
        marker in lowered
        for marker in (
            "career planning",
            "career choice",
            "planning and choice",
            "职业规划",
            "人生规划",
            "规划与选择",
            "发展规划",
        )
    ):
        return "职业规划与选择"
    if any(
        marker in lowered
        for marker in (
            "collaboration",
            "teamwork",
            "conflict",
            "协作",
            "冲突处理",
            "团队合作",
            "分歧处理",
        )
    ):
        return "协作与冲突处理"
    if any(
        marker in lowered
        for marker in (
            "company fit",
            "values and",
            "value fit",
            "价值观",
            "公司契合",
            "文化契合",
        )
    ):
        return "价值观与公司契合"
    if "mysql" in lowered:
        return "MySQL"
    if "redis" in lowered:
        return "Redis"
    if any(
        marker in lowered
        for marker in (
            "java并发",
            "java_concurrency",
            "concurrency",
            "concurrent",
            "thread",
            "线程",
            "锁",
        )
    ):
        return "Java并发"
    if any(
        marker in lowered
        for marker in ("计网", "network", "tcp", "udp", "http", "https", "dns")
    ):
        return "计网"
    if any(
        marker in lowered
        for marker in ("coding_thought", "手撕", "算法", "lru", "复杂度")
    ):
        return "手撕思路"
    if any(marker in lowered for marker in PROJECT_MARKERS):
        return "项目深度"
    if any(marker in lowered for marker in ("communication", "表达", "逻辑")):
        return "表达逻辑"
    return topic.strip() or category.strip() or "综合基础"


def project_depth_target(weak_topics: list[str]) -> int:
    """Spend two extra drill layers when project depth was a recent weakness."""

    return 6 if any(canonical_topic(topic) == "项目深度" for topic in weak_topics) else 4
