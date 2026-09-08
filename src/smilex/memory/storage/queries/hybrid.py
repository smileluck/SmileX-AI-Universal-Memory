"""Hybrid search — multi-strategy retrieval with Reciprocal Rank Fusion.

Per main doc §6.5 + §12.2:
- hybrid_memory_search(query, scope_filter, top_k)   P99 < 200ms

RRF 算法(§12.2):
    score(d) = sum over rankings i of 1 / (k + rank_i(d))
    where k = 60 (默认), rank_i(d) 是 d 在第 i 个排序列表中的位置(1-indexed)

策略:
- temporal: 时间范围查询 → triples
- graph: 实体 N 度关系 → triples(subject/object 命中)
- spatial: 位置附近 → locations(关联 triples 通过 location_id)
- causal: 因果链追溯 → triples
- semantic: 向量检索(L2 实现后接入,目前返回空)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from ...models import ScopeFilter
from .causal import trace_causal_chain
from .graph import find_n_degree_relations
from .spatial import query_in_area
from .temporal import query_in_range


@dataclass
class HybridQuery:
    """混合检索查询参数(所有字段可选).

    每个非空字段触发对应的检索策略.
    """

    text: str | None = None  # 语义检索(L2 接入,目前忽略)
    entity_id: str | None = None  # 图谱遍历起点
    time_range: tuple[datetime | str, datetime | str] | None = None  # (start, end)
    location: tuple[float, float, float] | None = None  # (lng, lat, radius_m)
    causal_triple_id: str | None = None  # 因果链起点
    top_k_per_strategy: int = 20  # 单策略返回的 ID 数上限


def rrf_fusion(
    rankings: list[list[str]],
    *,
    k: int = 60,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion 算法(§12.2).

    Args:
        rankings: 多个有序 ID 列表(每个列表按相关性降序)
        k: RRF 常数(默认 60,经验值)

    Returns:
        [(id, score), ...] 按 score 降序. score = sum of 1/(k + rank_i).

    示例:
        >>> rrf_fusion([["a", "b", "c"], ["b", "a", "d"]], k=60)
        [("b", ...), ("a", ...), ("c", ...), ("d", ...)]
    """
    if not rankings:
        return []

    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] += 1.0 / (k + rank)

    return sorted(scores.items(), key=lambda x: -x[1])


async def _run_temporal_strategy(
    conn: aiosqlite.Connection,
    query: HybridQuery,
    scope_filter: ScopeFilter | None,
) -> list[str]:
    """时序策略:返回 triples.id 列表(按 valid_from 升序,LIMIT 下推 SQL)."""
    if query.time_range is None:
        return []
    start, end = query.time_range
    rows = await query_in_range(
        conn,
        start,
        end,
        table="triples",
        scope_filter=scope_filter,
        limit=query.top_k_per_strategy,
    )
    return [r["id"] for r in rows]


async def _run_graph_strategy(
    conn: aiosqlite.Connection,
    query: HybridQuery,
    scope_filter: ScopeFilter | None,
) -> list[str]:
    """图谱策略:实体 N 度关系涉及的 triples(subject_id 或 object_id 命中)."""
    if query.entity_id is None:
        return []
    relations = await find_n_degree_relations(
        conn,
        query.entity_id,
        max_depth=2,
        scope_filter=scope_filter,
        include_paths=False,  # M8: 只需 ID 集合,跳过路径构建/解析
    )
    related_ids = {r["entity_id"] for r in relations}
    related_ids.add(query.entity_id)
    if not related_ids:
        return []

    from ._scope import build_scope_clause

    scope_clause, scope_params = build_scope_clause(scope_filter)
    scope_where = f" AND ({scope_clause})" if scope_clause else ""

    placeholders = ",".join(["?" for _ in related_ids])
    cursor = await conn.execute(
        f"SELECT id FROM triples WHERE (subject_id IN ({placeholders}) "
        # valid_to IS NULL: 只取当前有效边 — 被覆盖(superseded)的旧值
        # 不进入召回上下文;时间线/as-of 查询走 temporal 模块保持全版本
        f"OR object_id IN ({placeholders})){scope_where} "
        f"AND valid_to IS NULL "
        f"ORDER BY valid_from DESC LIMIT ?",
        [*related_ids, *related_ids, *scope_params, query.top_k_per_strategy],
    )
    rows = await cursor.fetchall()
    return [r["id"] for r in rows]


async def _run_spatial_strategy(
    conn: aiosqlite.Connection,
    query: HybridQuery,
    scope_filter: ScopeFilter | None,
) -> list[str]:
    """空间策略:位置附近的 triples(通过 location 字段关联)."""
    if query.location is None:
        return []
    lng, lat, radius_m = query.location
    locations = await query_in_area(
        conn, lng, lat, radius_m, scope_filter=scope_filter
    )
    if not locations:
        return []
    # temporal_fragments 表有 location_id,但 MVP 阶段 triples 表没有 location 字段
    # 这里通过 entities.location 间接关联(简化版,实际可通过 entity_id 关联)
    # 暂时返回空,留给 L2 完善空间-语义联合检索
    return []


async def _run_causal_strategy(
    conn: aiosqlite.Connection,
    query: HybridQuery,
    scope_filter: ScopeFilter | None,
) -> list[str]:
    """因果策略:返回因果链上的 triples."""
    if query.causal_triple_id is None:
        return []
    result = await trace_causal_chain(
        conn,
        query.causal_triple_id,
        direction="both",
        scope_filter=scope_filter,
    )
    ids: list[str] = []
    for entry in result.get("backward", []):
        ids.append(entry["id"])
    for entry in result.get("forward", []):
        ids.append(entry["id"])
    return ids[: query.top_k_per_strategy]


async def hybrid_memory_search(
    conn: aiosqlite.Connection,
    query: HybridQuery,
    *,
    scope_filter: ScopeFilter | None = None,
    top_k: int = 10,
    rrf_k: int = 60,
) -> list[dict]:
    """混合检索 — 多策略 RRF 融合(§6.5).

    Args:
        conn: aiosqlite 连接
        query: HybridQuery 检索参数
        scope_filter: 作用域过滤
        top_k: 最终返回的结果数(默认 10)
        rrf_k: RRF 常数(默认 60)

    Returns:
        列表,每项 {"id": str, "score": float, "strategies_hit": int}
        按 score 降序,取 top_k.
    """
    strategies: list[tuple[str, list[str]]] = []

    temporal_ids = await _run_temporal_strategy(conn, query, scope_filter)
    if temporal_ids:
        strategies.append(("temporal", temporal_ids))

    graph_ids = await _run_graph_strategy(conn, query, scope_filter)
    if graph_ids:
        strategies.append(("graph", graph_ids))

    spatial_ids = await _run_spatial_strategy(conn, query, scope_filter)
    if spatial_ids:
        strategies.append(("spatial", spatial_ids))

    causal_ids = await _run_causal_strategy(conn, query, scope_filter)
    if causal_ids:
        strategies.append(("causal", causal_ids))

    # RRF 融合
    fused = rrf_fusion(
        [ids for _, ids in strategies],
        k=rrf_k,
    )

    # 计算每个 ID 命中的策略数
    strategy_map: dict[str, set[str]] = defaultdict(set)
    for name, ids in strategies:
        for doc_id in ids:
            strategy_map[doc_id].add(name)

    # 取 top_k
    top_results = fused[:top_k]
    return [
        {
            "id": doc_id,
            "score": score,
            "strategies_hit": list(strategy_map.get(doc_id, set())),
        }
        for doc_id, score in top_results
    ]


__all__ = [
    "HybridQuery",
    "hybrid_memory_search",
    "rrf_fusion",
]
