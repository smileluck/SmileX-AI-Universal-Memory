"""tasks 包公共件 — 任务名注册键 / 产物前缀约定 / scope 过滤拼接 / 向量清理."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import Any

# ---------- 任务名(注册键) ----------
TASK_CONSOLIDATE = "consolidate"
TASK_FORGET = "forget"
TASK_SUMMARIZE = "summarize"
TASK_SEMANTIC = "semantic"
TASK_DB_INTEGRITY = "db_integrity"
TASK_DEDUP = "dedup"
CORE_TASK_NAMES = (
    TASK_CONSOLIDATE,
    TASK_FORGET,
    TASK_SUMMARIZE,
    TASK_SEMANTIC,
    TASK_DB_INTEGRITY,
    TASK_DEDUP,
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


async def drop_fragment_vectors(conn: Any, fragment_ids: Iterable[str]) -> None:
    """删除 fragment 关联向量(vector_links + memory_vectors).

    vector_links.fragment_id 有 FK 引用 temporal_fragments(无 CASCADE,
    foreign_keys=1),删除热表行前必须先清向量 — forget/consolidate/dedup
    共用;memory_vectors 虚拟表在 vec 扩展未加载的库里不存在,缺表时只清
    links(Archiver._drop_vectors 同模式)。
    """
    ids = list(fragment_ids)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with contextlib.suppress(Exception):  # noqa: BLE001 — 无 vec 表时跳过
        await conn.execute(
            "DELETE FROM memory_vectors WHERE vector_id IN ("
            f"SELECT vector_id FROM vector_links WHERE fragment_id IN ({placeholders}))",
            ids,
        )
    await conn.execute(
        f"DELETE FROM vector_links WHERE fragment_id IN ({placeholders})", ids
    )
