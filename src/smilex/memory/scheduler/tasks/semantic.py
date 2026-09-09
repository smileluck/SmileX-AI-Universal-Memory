"""语义任务(NetworkX 图维护 — Louvain 社区检测轻量版,08 号文档修订落地).

2026-09 从连通分量升级为 networkx 内置 louvain_communities:
连通分量零分辨率,记忆图被整合任务的语义边连成巨分量后输出退化为
"全项目 1 个社区",模块度聚类(加权)才能切断弱桥。

设计要点(与旧版差异):
- scope 语义对齐检索: 按"宇宙"分别建图 — global 单独一组,每个
  project/tenant scope 与 global 联合一组(recall 默认查当前项目+global,
  社区按同一视角划分);旧版 payload 不传 scope 时全库混图,跨项目实体
  污染同一张图,缓存全落 global
- 加权无向图: 同一对实体间的多条三元组聚合为边权(条数);确定性前提
  是实体/边加载带 ORDER BY + Louvain 固定 seed(SQLite 无 ORDER BY 时
  行序不保证,同数据不同运行可能产出不同社区)
- diff 增量刷新: 社区 key = md5(成员 id 排序)[:12],与库内 L3 缓存按
  key diff — 只删消失的(先清向量)、只插新出现的、内容变化才 UPDATE;
  数据未变重跑 = 零写入(旧版整批删写,幂等只对条数成立不对行身份成立)
- 向量重建: 注入 vector_store 且 vec 表存在时,新社区/内容变更社区即时
  重嵌,L3 进入 KNN 通道(此前仅 FTS BM25 可命中);删除路径先清向量
  (vector_links.fragment_id FK 无 CASCADE)
- 内容为确定性规则版;LLM 摘要升级有意不做 — 非确定性内容会让 diff
  永远判定"有变化",破坏零写入幂等并放大社区向量重建 churn
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any

from ....utils.ids import generate_id
from ....utils.timeutil import now_utc, to_iso
from ._common import SEMANTIC_COMMUNITY_ID, SEMANTIC_COMMUNITY_PREFIX, drop_fragment_vectors

if TYPE_CHECKING:
    from ...storage.storage_engine import StorageEngine
    from ...storage.vector_store import VectorStore
    from ..checkpoint import InterruptContext

# Louvain 随机性固定 seed(确定性/幂等前提之一;payload 可覆盖供测试)
_LOUVAIN_SEED = 42


async def _universes(conn: Any, scope: str | None) -> list[tuple[str, list[str]]]:
    """payload scope → [(缓存 scope, 图 scope 集)] 列表.

    - None: global 单独 + 每个出现的非 global scope 各自 ∪ global
    - "global": 仅 global
    - "project:x" / "tenant:x": [x, global]
    """
    if scope == "global":
        return [("global", ["global"])]
    if scope:
        return [(scope, [scope, "global"])]
    cur = await conn.execute(
        "SELECT DISTINCT scope FROM entities WHERE scope != 'global' "
        "UNION SELECT DISTINCT scope FROM triples WHERE scope != 'global' "
        "ORDER BY scope"
    )
    anchors = [str(r["scope"]) for r in await cur.fetchall()]
    return [("global", ["global"])] + [(a, [a, "global"]) for a in anchors]


async def semantic(
    storage: StorageEngine,
    ctx: InterruptContext,
    payload: dict[str, Any],
    *,
    vector_store: VectorStore | None = None,
) -> dict:
    """语义任务 — Louvain 社区检测,产出 L3 社区缓存 fragment(diff 增量刷新).

    payload:
        scope: 限定宇宙(全路径;None → global + 每个 project/tenant 宇宙)
        min_component: 成社区最小实体数(默认 2)
        top_k: 内容中列出的核心实体数(默认 5)
        resolution: Louvain 分辨率(默认 1.0,越大社区越细)
        seed: Louvain 随机 seed(默认 42;确定性前提)

    返回统计: entities/edges/communities/universes + deleted/inserted/
    updated(diff 计数)+ vectors_rebuilt/vectors_missing。
    """
    import networkx as nx

    conn = storage.conn
    scope = payload.get("scope")
    min_component = int(payload.get("min_component", 2))
    top_k = int(payload.get("top_k", 5))
    resolution = float(payload.get("resolution", 1.0))
    seed = int(payload.get("seed", _LOUVAIN_SEED))

    stats: dict[str, Any] = {
        "entities": 0,
        "edges": 0,
        "communities": 0,
        "universes": 0,
        "deleted": 0,
        "inserted": 0,
        "updated": 0,
        "vectors_rebuilt": 0,
        "vectors_missing": 0,
        **ctx.state.get("stats", {}),
    }

    vec_available = False
    if vector_store is not None:
        with contextlib.suppress(sqlite3.OperationalError):
            await conn.execute("SELECT 1 FROM memory_vectors LIMIT 1")
            vec_available = True

    async def _rebuild_vector(text: str, *, fragment_id: str) -> None:
        if vec_available:
            await vector_store.add_text(conn, text, fragment_id=fragment_id)  # type: ignore[union-attr]
            stats["vectors_rebuilt"] += 1
        else:
            stats["vectors_missing"] += 1

    universes = await _universes(conn, scope)
    step = ctx.step + 1
    await ctx.checkpoint(
        step=step, progress=0.1, state={"stats": stats}, cursor=None
    )

    written = 0  # checkpoint 降频计数(M9: 落库即隐式 commit)

    for cache_scope, graph_scopes in universes:
        # ---- 建图(确定性: ORDER BY;边权 = 实体对间三元组条数) ----
        ph = ",".join("?" for _ in graph_scopes)
        cur = await conn.execute(
            f"SELECT id, name, scope FROM entities WHERE scope IN ({ph}) "
            "ORDER BY id",
            graph_scopes,
        )
        ent_rows = [dict(r) for r in await cur.fetchall()]
        names = {r["id"]: r["name"] for r in ent_rows}
        global_ids = {r["id"] for r in ent_rows if r["scope"] == "global"}

        cur = await conn.execute(
            "SELECT subject_id, object_id FROM triples "
            f"WHERE scope IN ({ph}) AND object_id IS NOT NULL AND valid_to IS NULL "
            "ORDER BY subject_id, object_id",
            graph_scopes,
        )
        edge_weight: dict[tuple[str, str], int] = {}
        n_edge_rows = 0
        for r in await cur.fetchall():
            s, o = str(r["subject_id"]), str(r["object_id"])
            # 宇宙外实体(如 global 三元组指向他项目实体)不进图,防污染
            if s not in names or o not in names:
                continue
            k = (s, o) if s <= o else (o, s)
            edge_weight[k] = edge_weight.get(k, 0) + 1
            n_edge_rows += 1

        graph = nx.Graph()
        graph.add_nodes_from(names)
        for (u, v), w in sorted(edge_weight.items()):
            graph.add_edge(u, v, weight=w)
        stats["entities"] += len(names)
        stats["edges"] += n_edge_rows
        stats["universes"] += 1

        # ---- Louvain(固定 seed + 加权模块度切弱桥) ----
        communities = [
            c
            for c in nx.community.louvain_communities(
                graph, weight="weight", resolution=resolution, seed=seed
            )
            if len(c) >= min_component
        ]
        # 非 global 宇宙跳过纯 global 成员社区(global 宇宙会单独缓存它们,
        # 避免同一社区在多个 scope 重复;宇宙外实体未加载,跨项目社区不可能)
        if cache_scope != "global":
            communities = [
                c for c in communities if not c <= global_ids
            ]
        communities.sort(key=lambda c: sorted(c))  # 确定性处理顺序

        # ---- 新社区集(key → (成员 ids, 内容)) ----
        degree = dict(graph.degree())
        fresh: dict[str, tuple[list[str], str]] = {}
        for comp in communities:
            ids = sorted(comp)
            key = hashlib.md5(",".join(ids).encode()).hexdigest()[:12]  # 确定性分组键(非安全用途)
            top = sorted(comp, key=lambda e: (-degree.get(e, 0), e))[:top_k]
            n_edges = sum(degree.get(e, 0) for e in comp) // 2
            content = (
                f"{SEMANTIC_COMMUNITY_PREFIX}{len(comp)} 个实体 / {n_edges} 条关系;"
                f"核心实体: {', '.join(names.get(e, e) for e in top)}"
            )
            fresh[key] = (ids, content)
        stats["communities"] += len(fresh)

        # ---- diff 增量刷新(对照本 cache scope 的旧 L3 缓存) ----
        cur = await conn.execute(
            "SELECT id, fragment_id, content FROM temporal_fragments "
            "WHERE scope = ? AND layer = 'L3' AND fragment_id LIKE ?",
            [cache_scope, f"{SEMANTIC_COMMUNITY_ID}%"],
        )
        prefix_len = len(SEMANTIC_COMMUNITY_ID)
        existing = {
            str(r["fragment_id"])[prefix_len:]: (str(r["id"]), str(r["content"] or ""))
            for r in await cur.fetchall()
        }

        # 1) 消失的社区: 先清向量(FK 无 CASCADE)再删行
        gone_ids = [existing[k][0] for k in existing.keys() - fresh.keys()]
        if gone_ids:
            await drop_fragment_vectors(conn, gone_ids)
            placeholders = ",".join("?" for _ in gone_ids)
            await conn.execute(
                f"DELETE FROM temporal_fragments WHERE id IN ({placeholders})",
                gone_ids,
            )
            stats["deleted"] += len(gone_ids)

        now = to_iso(now_utc())

        # 2) 新社区: 插入 + 重嵌向量
        for key in sorted(fresh.keys() - existing.keys()):
            ids, content = fresh[key]
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
            await _rebuild_vector(content, fragment_id=new_id)
            stats["inserted"] += 1
            written += 1
            # checkpoint 降频(M9): 每 50 次写落一次断点(落库即隐式 commit)
            if written % 50 == 0:
                step += 1
                await ctx.checkpoint(
                    step=step, progress=0.5, state={"stats": stats}, cursor=None
                )

        # 3) 成员相同(key 相同)但内容变化: 换内容 + 换向量(规模/degree 排序变)
        for key in sorted(existing.keys() & fresh.keys()):
            _, content = fresh[key]
            old_id, old_content = existing[key]
            if content == old_content:
                continue
            await drop_fragment_vectors(conn, [old_id])
            await conn.execute(
                "UPDATE temporal_fragments SET content = ?, updated_at = ? "
                "WHERE id = ?",
                (content, now, old_id),
            )
            await _rebuild_vector(content, fragment_id=old_id)
            stats["updated"] += 1
            written += 1
            if written % 50 == 0:
                step += 1
                await ctx.checkpoint(
                    step=step, progress=0.5, state={"stats": stats}, cursor=None
                )

        await conn.commit()

    step += 1
    await ctx.checkpoint(step=step, progress=1.0, state={"stats": stats}, cursor=None)
    return stats
