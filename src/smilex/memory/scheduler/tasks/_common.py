"""tasks 包公共件 — 任务名注册键 / 产物前缀约定 / scope 过滤拼接."""

from __future__ import annotations

from typing import Any

# ---------- 任务名(注册键) ----------
TASK_CONSOLIDATE = "consolidate"
TASK_FORGET = "forget"
TASK_SUMMARIZE = "summarize"
TASK_CAUSAL = "causal"
TASK_SEMANTIC = "semantic"
TASK_DB_INTEGRITY = "db_integrity"
CORE_TASK_NAMES = (
    TASK_CONSOLIDATE,
    TASK_FORGET,
    TASK_SUMMARIZE,
    TASK_CAUSAL,
    TASK_SEMANTIC,
    TASK_DB_INTEGRITY,
)

# 摘要/整合/语义产物的内容前缀与 fragment_id 约定(幂等判定用)
SUMMARY_PREFIX = "【摘要】"
SUMMARY_ID_SUFFIX = ":summary"
CONSOLIDATION_PREFIX = "【整合】"
SEMANTIC_COMMUNITY_PREFIX = "【语义社区】"
SEMANTIC_COMMUNITY_ID = "semantic:community:"


def _scope_clause(scope: str | None, where: str, params: list[Any]) -> tuple[str, list[Any]]:
    """可选 scope 过滤拼接到 WHERE 子句."""
    if scope:
        where += " AND scope = ?"
        params.append(scope)
    return where, params
