"""语义任务(NetworkX 图维护)."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from ....utils.ids import generate_id
from ....utils.timeutil import now_utc, to_iso
from ._common import SEMANTIC_COMMUNITY_ID, SEMANTIC_COMMUNITY_PREFIX, _scope_clause

if TYPE_CHECKING:
    from ...storage.storage_engine import StorageEngine
    from ..checkpoint import InterruptContext


async def semantic(
    storage: StorageEngine, ctx: InterruptContext, payload: dict[str, Any]
) -> dict:
    """语义任务 — NetworkX 重建实体关系图,预计算连通分量(语义社区)缓存.

    算法(规则版):
    1. 加载实体 + 当前有效(valid_to IS NULL)且指向实体的 triples,建无向图
    2. nx.connected_components 求连通分量;>= min_component 的为语义社区
    3. 社区预计算结果以 L3 fragment 缓存(fragment_id 前缀
       "semantic:community:",key 为成员 id 排序后的 md5,确定性):
       先按前缀整批删除旧缓存再重写,重跑幂等不膨胀
    4. 内容含社区规模与按 degree 排序的核心实体名

    payload:
        scope: 只统计该 scope,缓存 fragment 也落在该 scope(默认 None → global)
        min_component: 成社区最小实体数(默认 2)
        top_k: 内容中列出的核心实体数(默认 5)
    """
    import networkx as nx

    conn = storage.conn
    scope = payload.get("scope")
    min_component = int(payload.get("min_component", 2))
    top_k = int(payload.get("top_k", 5))
    cache_scope = scope or "global"

    # ---- 阶段 1: 建图 ----
    ent_where, ent_params = _scope_clause(scope, "1 = 1", [])
    cur = await conn.execute(f"SELECT id, name FROM entities WHERE {ent_where}", ent_params)
    names = {r["id"]: r["name"] for r in await cur.fetchall()}

    edge_where, edge_params = _scope_clause(
        scope, "object_id IS NOT NULL AND valid_to IS NULL", []
    )
    cur = await conn.execute(
        f"SELECT subject_id, object_id FROM triples WHERE {edge_where}", edge_params
    )
    edges = [(r["subject_id"], r["object_id"]) for r in await cur.fetchall()]

    graph = nx.Graph()
    graph.add_nodes_from(names)
    graph.add_edges_from(edges)
    components = [c for c in nx.connected_components(graph) if len(c) >= min_component]
    components.sort(key=lambda c: sorted(c))  # 确定性顺序
    stats = {
        "entities": len(names),
        "edges": len(edges),
        "communities": len(components),
        **ctx.state.get("stats", {}),
    }
    step = ctx.step + 1
    await ctx.checkpoint(step=step, progress=0.3, state={"stats": stats}, cursor=None)

    # ---- 阶段 2: 整批刷新 L3 缓存(先删后写,幂等) ----
    del_where, del_params = _scope_clause(
        scope,
        "layer = 'L3' AND fragment_id LIKE ?",
        [f"{SEMANTIC_COMMUNITY_ID}%"],
    )
    await conn.execute(f"DELETE FROM temporal_fragments WHERE {del_where}", del_params)

    degree = dict(graph.degree())
    for i, comp in enumerate(components):
        ids = sorted(comp)
        key = hashlib.md5(",".join(ids).encode()).hexdigest()[:12]  # 确定性分组键(非安全用途)
        top = sorted(comp, key=lambda e: (-degree.get(e, 0), e))[:top_k]
        top_names = [names.get(e, e) for e in top]
        # 社区内边数 = 成员度和 / 2,无需 graph.subgraph(comp) 拷贝(M9)
        n_edges = sum(degree.get(e, 0) for e in comp) // 2
        content = (
            f"{SEMANTIC_COMMUNITY_PREFIX}{len(comp)} 个实体 / {n_edges} 条关系;"
            f"核心实体: {', '.join(top_names)}"
        )
        now = to_iso(now_utc())
        new_id = generate_id()
        await conn.execute(
            "INSERT INTO temporal_fragments(id, fragment_id, time_start, content, "
            "entities, scope, layer, importance, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'L3', 0.5, ?, ?)",
            (
                new_id,
                f"{SEMANTIC_COMMUNITY_ID}{key}",
                now,
                content,
                json.dumps(ids, ensure_ascii=False),
                cache_scope,
                now,
                now,
            ),
        )
        # checkpoint 降频(M9): 每 50 个社区落一次断点(落库即隐式 commit),
        # 代替每社区一次 DELETE+INSERT+commit
        if (i + 1) % 50 == 0:
            step += 1
            await ctx.checkpoint(
                step=step,
                progress=0.3 + 0.7 * (i + 1) / len(components),
                state={"stats": stats},
                cursor=None,
            )
    await conn.commit()
    step += 1
    await ctx.checkpoint(
        step=step, progress=1.0, state={"stats": stats}, cursor=None
    )
    return stats
