"""Graph queries — path finding + N-degree relations.

Per main doc §6.5 检索能力矩阵:
- find_path(src, dst, max_depth)       P99 < 50ms
- find_n_degree_relations(entity, n)   P99 < 100ms

实现方式: SQL `WITH RECURSIVE` CTE(递归 SELECT 内 `ORDER BY depth`
启用 BFS 优先队列,M1)+ 逗号拼接路径字符串做循环检测(`instr` 判重,
代替逐边 `json_each` 解析 JSON 数组)。

图模型:
- triples 表形成有向图(subject → predicate → object)
- 只遍历 valid_to IS NULL 的"当前有效"边
- max_depth 限制递归深度(防止指数爆炸)
- 用路径字符串 instr 检测循环(entity/triple ID 为 ULID,不含逗号)

BFS 说明: SQLite recursive CTE 默认用栈(DFS);递归 SELECT 内加
`ORDER BY depth` 后改用优先队列按层扩展,find_path 的第一个命中即最短路径,
外层无需再 ORDER BY 全量物化(LIMIT 1 可提前终止 CTE 生成)。
"""

from __future__ import annotations

import aiosqlite

from ...models import ScopeFilter
from ._scope import build_scope_clause


def _split_path(path: str) -> list[str]:
    """',a,b,' 逗号路径字符串 → ['a', 'b']."""
    return [p for p in path.split(",") if p]


def _relation_type_clause(relation_types: list[str] | None) -> tuple[str, list[str]]:
    """relation_types 列表 → JOIN 用的 ` AND t.relation_type IN (...)` 片段."""
    if not relation_types:
        return "", []
    placeholders = ",".join("?" for _ in relation_types)
    return f" AND t.relation_type IN ({placeholders})", list(relation_types)


async def find_path(
    conn: aiosqlite.Connection,
    src_id: str,
    dst_id: str,
    *,
    max_depth: int = 5,
    scope_filter: ScopeFilter | None = None,
    relation_types: list[str] | None = None,
) -> dict | None:
    """查找从 src_id 到 dst_id 的最短路径(BFS via recursive CTE).

    Args:
        conn: aiosqlite 连接
        src_id: 起点实体 ID
        dst_id: 终点实体 ID
        max_depth: 最大深度(默认 5)
        scope_filter: 作用域过滤
        relation_types: 只遍历这些 relation_type 的边
            (causal/spatial/temporal/semantic/project_state/task_status/config);
            None 遍历全部

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
    type_where, type_params = _relation_type_clause(relation_types)

    sql = f"""
        WITH RECURSIVE bfs(node, depth, path_nodes, path_edges) AS (
            SELECT
                ? AS node,
                0 AS depth,
                ',' || ? || ',' AS path_nodes,
                ',' AS path_edges
            UNION ALL
            SELECT
                t.object_id AS node,
                b.depth + 1 AS depth,
                b.path_nodes || t.object_id || ',' AS path_nodes,
                b.path_edges || t.id || ',' AS path_edges
            FROM bfs b
            JOIN triples t ON t.subject_id = b.node{scope_where}{type_where}
            WHERE b.depth < ?
              AND t.valid_to IS NULL
              AND t.object_id IS NOT NULL
              AND instr(b.path_nodes, ',' || t.object_id || ',') = 0
            ORDER BY depth
        )
        SELECT path_nodes, path_edges, depth
        FROM bfs
        WHERE node = ?
        LIMIT 1
    """

    # 参数顺序按 SQL 中 ? 出现的位置:
    # 1. SELECT ? AS node            → src_id
    # 2. ',' || ? || ','             → src_id
    # 3. JOIN ... scope_where (?s)   → scope_params
    # 4. JOIN ... type_where (?s)    → type_params
    # 5. WHERE b.depth < ?           → max_depth
    # 6. WHERE node = ?              → dst_id
    params: list = [src_id, src_id, *scope_params, *type_params, max_depth, dst_id]

    cursor = await conn.execute(sql, params)
    row = await cursor.fetchone()

    if row is None:
        return None

    return {
        "nodes": _split_path(row["path_nodes"]),
        "edges": _split_path(row["path_edges"]),
        "depth": row["depth"],
    }


async def find_n_degree_relations(
    conn: aiosqlite.Connection,
    entity_id: str,
    *,
    max_depth: int = 2,
    scope_filter: ScopeFilter | None = None,
    include_paths: bool = True,
    relation_types: list[str] | None = None,
) -> list[dict]:
    """查找 entity_id 的 N 度关系(BFS 到 depth N).

    Args:
        conn: aiosqlite 连接
        entity_id: 起点实体 ID
        max_depth: 最大深度(默认 2,即"朋友的朋友")
        scope_filter: 作用域过滤
        include_paths: 是否返回路径明细(M8)。False 时只返回
            entity_id/depth(hybrid 检索只需 ID 集合,跳过路径构建与解析)
        relation_types: 只遍历这些 relation_type 的边;None 遍历全部

    Returns:
        列表,每项 {"entity_id": str, "depth": int, "path_nodes": list, "path_edges": list}
        (include_paths=False 时无 path_nodes/path_edges 键)
        按 depth 升序,同一 depth 内按 entity_id 字母序.
        起点本身不在结果中.每个 entity 只出现一次(取最短路径).
    """
    scope_clause, scope_params = build_scope_clause(scope_filter)
    scope_where = f" AND ({scope_clause})" if scope_clause else ""
    type_where, type_params = _relation_type_clause(relation_types)

    if include_paths:
        # MIN(depth) 聚合: SQLite 保证裸列(path_nodes/path_edges)取自
        # 产生最小 depth 的行 —— 即该节点的最短路径,去重下推到 SQL(M1)。
        select_cols = "path_nodes, path_edges"
    else:
        # 内部仍需 path_nodes 做路径内循环检测;path_edges 不构建、不返回
        select_cols = "NULL AS path_nodes, NULL AS path_edges"

    sql = f"""
        WITH RECURSIVE bfs(node, depth, path_nodes, path_edges) AS (
            SELECT
                ? AS node,
                0 AS depth,
                ',' || ? || ',' AS path_nodes,
                ',' AS path_edges
            UNION ALL
            SELECT
                t.object_id AS node,
                b.depth + 1 AS depth,
                b.path_nodes || t.object_id || ',' AS path_nodes,
                b.path_edges || {("t.id || ','") if include_paths else "''"} AS path_edges
            FROM bfs b
            JOIN triples t ON t.subject_id = b.node{scope_where}{type_where}
            WHERE b.depth < ?
              AND t.valid_to IS NULL
              AND t.object_id IS NOT NULL
              AND instr(b.path_nodes, ',' || t.object_id || ',') = 0
            ORDER BY depth
        )
        SELECT node AS entity_id, MIN(depth) AS depth, {select_cols}
        FROM bfs
        WHERE depth > 0
        GROUP BY node
        ORDER BY depth, node
    """

    params: list = [entity_id, entity_id, *scope_params, *type_params, max_depth]

    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()

    result: list[dict] = []
    for row in rows:
        item: dict = {"entity_id": row["entity_id"], "depth": row["depth"]}
        if include_paths:
            item["path_nodes"] = _split_path(row["path_nodes"])
            item["path_edges"] = _split_path(row["path_edges"])
        result.append(item)
    return result
