"""Graph queries — path finding + N-degree relations.

Per main doc §6.5 检索能力矩阵:
- find_path(src, dst, max_depth)       P99 < 50ms
- find_n_degree_relations(entity, n)   P99 < 100ms

实现方式: SQL `WITH RECURSIVE` CTE + json_insert 循环检测.

图模型:
- triples 表形成有向图(subject → predicate → object)
- 只遍历 valid_to IS NULL 的"当前有效"边
- max_depth 限制递归深度(防止指数爆炸)
- 用 json_each 检测循环(NOT EXISTS 子查询)

注意: SQLite 的 `json_array() || json_array()` 是字符串拼接而非数组拼接,
      必须用 `json_insert(arr, '$[#]', x)` 追加元素.
"""

from __future__ import annotations

import json

import aiosqlite

from ...models import ScopeFilter
from ._scope import build_scope_clause


async def find_path(
    conn: aiosqlite.Connection,
    src_id: str,
    dst_id: str,
    *,
    max_depth: int = 5,
    scope_filter: ScopeFilter | None = None,
) -> dict | None:
    """查找从 src_id 到 dst_id 的最短路径(BFS via recursive CTE).

    Args:
        conn: aiosqlite 连接
        src_id: 起点实体 ID
        dst_id: 终点实体 ID
        max_depth: 最大深度(默认 5)
        scope_filter: 作用域过滤

    Returns:
        {"nodes": [...], "edges": [...], "depth": int} 或 None(无路径)

        - nodes: 路径上的 entity IDs(含起点终点)
        - edges: 路径上的 triple IDs
        - depth: 跳数(边数)
    """
    if src_id == dst_id:
        return {"nodes": [src_id], "edges": [], "depth": 0}

    scope_clause, scope_params = build_scope_clause(scope_filter)
    scope_where = f" AND ({scope_clause})" if scope_clause else ""

    sql = f"""
        WITH RECURSIVE bfs(node, depth, path_nodes, path_edges) AS (
            SELECT
                ? AS node,
                0 AS depth,
                json_array(?) AS path_nodes,
                json_array() AS path_edges
            UNION ALL
            SELECT
                t.object_id AS node,
                b.depth + 1 AS depth,
                json_insert(b.path_nodes, '$[#]', t.object_id) AS path_nodes,
                json_insert(b.path_edges, '$[#]', t.id) AS path_edges
            FROM bfs b
            JOIN triples t ON t.subject_id = b.node{scope_where}
            WHERE b.depth < ?
              AND t.valid_to IS NULL
              AND t.object_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM json_each(b.path_nodes) WHERE value = t.object_id
              )
        )
        SELECT path_nodes, path_edges, depth
        FROM bfs
        WHERE node = ?
        ORDER BY depth
        LIMIT 1
    """

    # 参数顺序按 SQL 中 ? 出现的位置:
    # 1. SELECT ? AS node          → src_id
    # 2. json_array(?)             → src_id
    # 3. JOIN ... scope_where (?s) → scope_params
    # 4. WHERE b.depth < ?         → max_depth
    # 5. WHERE node = ?            → dst_id
    params: list = [src_id, src_id, *scope_params, max_depth, dst_id]

    cursor = await conn.execute(sql, params)
    row = await cursor.fetchone()

    if row is None:
        return None

    return {
        "nodes": json.loads(row["path_nodes"]),
        "edges": json.loads(row["path_edges"]),
        "depth": row["depth"],
    }


async def find_n_degree_relations(
    conn: aiosqlite.Connection,
    entity_id: str,
    *,
    max_depth: int = 2,
    scope_filter: ScopeFilter | None = None,
) -> list[dict]:
    """查找 entity_id 的 N 度关系(BFS 到 depth N).

    Args:
        conn: aiosqlite 连接
        entity_id: 起点实体 ID
        max_depth: 最大深度(默认 2,即"朋友的朋友")
        scope_filter: 作用域过滤

    Returns:
        列表,每项 {"entity_id": str, "depth": int, "path_nodes": list, "path_edges": list}
        按 depth 升序,同一 depth 内按 entity_id 字母序.
        起点本身不在结果中.每个 entity 只出现一次(取最短路径).
    """
    scope_clause, scope_params = build_scope_clause(scope_filter)
    scope_where = f" AND ({scope_clause})" if scope_clause else ""

    sql = f"""
        WITH RECURSIVE bfs(node, depth, path_nodes, path_edges) AS (
            SELECT
                ? AS node,
                0 AS depth,
                json_array(?) AS path_nodes,
                json_array() AS path_edges
            UNION ALL
            SELECT
                t.object_id AS node,
                b.depth + 1 AS depth,
                json_insert(b.path_nodes, '$[#]', t.object_id) AS path_nodes,
                json_insert(b.path_edges, '$[#]', t.id) AS path_edges
            FROM bfs b
            JOIN triples t ON t.subject_id = b.node{scope_where}
            WHERE b.depth < ?
              AND t.valid_to IS NULL
              AND t.object_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM json_each(b.path_nodes) WHERE value = t.object_id
              )
        )
        SELECT node AS entity_id, depth, path_nodes, path_edges
        FROM bfs
        WHERE depth > 0
        ORDER BY depth, node
    """

    params: list = [entity_id, entity_id, *scope_params, max_depth]

    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()

    # 每个 entity_id 只取最短路径(去重)
    seen: set[str] = set()
    result: list[dict] = []
    for row in rows:
        eid = row["entity_id"]
        if eid in seen:
            continue
        seen.add(eid)
        result.append({
            "entity_id": eid,
            "depth": row["depth"],
            "path_nodes": json.loads(row["path_nodes"]),
            "path_edges": json.loads(row["path_edges"]),
        })
    return result
