"""Causal queries — trace causal chains via predecessor_id.

Per main doc §6.5 检索能力矩阵:
- trace_causal_chain(triple_id)   P99 < 50ms

图模型:
- triples.predecessor_id 形成因果链(每个 triple 至多 1 个前驱)
- 向后追溯(backward): 从某 triple 沿 predecessor_id 走到根因
- 向前追溯(forward):  从某 triple 找所有后继(影响分析)

实现方式:
- backward: 单条 SQL `WITH RECURSIVE` CTE 沿 predecessor_id 链走到根因(H4)
- forward:  按层批量 `IN (...)` 找所有 predecessor_id 指向当前层的 triples

scope 过滤策略:
- 起点检查(在 CTE 之前):起点 triple 必须在 scope 内,否则返回空
- CTE 内不再过滤(同一因果链通常同 scope,无需重复过滤)
"""

from __future__ import annotations

import aiosqlite

from ...models import ScopeFilter
from ._scope import build_scope_clause


async def _is_in_scope(
    conn: aiosqlite.Connection,
    triple_id: str,
    scope_clause: str,
    scope_params: list[str],
) -> bool:
    """检查 triple_id 是否存在且在 scope 范围内."""
    if not scope_clause:
        # 无 scope 过滤,只需检查存在
        sql = "SELECT 1 FROM triples WHERE id = ?"
        cursor = await conn.execute(sql, [triple_id])
    else:
        sql = f"SELECT 1 FROM triples WHERE id = ? AND ({scope_clause})"
        cursor = await conn.execute(sql, [triple_id, *scope_params])
    return (await cursor.fetchone()) is not None


async def _trace_backward(
    conn: aiosqlite.Connection,
    triple_id: str,
    max_depth: int,
) -> list[dict]:
    """向后追溯: triple → predecessor → predecessor → ... → root.

    单条 WITH RECURSIVE CTE 取回整链(H4),代替逐跳 SELECT 循环
    (max_depth=20 时最多 21 次串行往返)。深度上限保证有限终止;
    异常数据构成前驱环时按首次到达去重(与原 visited 语义一致)。
    """
    cursor = await conn.execute(
        "WITH RECURSIVE chain(id, predecessor_id, depth) AS ("
        "    SELECT id, predecessor_id, 0 FROM triples WHERE id = ?"
        "    UNION ALL"
        "    SELECT t.id, t.predecessor_id, c.depth + 1"
        "    FROM chain c JOIN triples t ON t.id = c.predecessor_id"
        "    WHERE c.depth < ?"
        ") "
        "SELECT id, depth FROM chain ORDER BY depth",
        [triple_id, max_depth],
    )
    rows: list[dict] = []
    seen: set[str] = set()
    for row in await cursor.fetchall():
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append({"id": row["id"], "depth": row["depth"]})
    return rows


async def _trace_forward(
    conn: aiosqlite.Connection,
    triple_id: str,
    max_depth: int,
) -> list[dict]:
    """向前追溯: triple → 所有后继 → 所有后继 → ..."""
    rows: list[dict] = []
    current_level = [triple_id]
    visited: set[str] = {triple_id}
    depth = 0

    while current_level and depth < max_depth:
        placeholders = ",".join(["?" for _ in current_level])
        cursor = await conn.execute(
            f"SELECT id FROM triples WHERE predecessor_id IN ({placeholders})",
            current_level,
        )
        next_level_rows = await cursor.fetchall()
        next_level = []
        for r in next_level_rows:
            if r["id"] not in visited:
                visited.add(r["id"])
                next_level.append(r["id"])
                rows.append({"id": r["id"], "depth": depth + 1})
        current_level = next_level
        depth += 1

    return rows


async def trace_causal_chain(
    conn: aiosqlite.Connection,
    triple_id: str,
    *,
    direction: str = "backward",
    max_depth: int = 20,
    scope_filter: ScopeFilter | None = None,
) -> dict:
    """追溯因果链.

    Args:
        conn: aiosqlite 连接
        triple_id: 起点 triple ID
        direction: 追溯方向
            - "backward": 沿 predecessor_id 向根因(默认,线性路径)
            - "forward":  找所有后继(树/DAG 扩展)
            - "both":     双向
        max_depth: 最大深度(防止无限递归)
        scope_filter: 作用域过滤(只检查起点是否在 scope 内)

    Returns:
        {"start": triple_id, "backward": [...], "forward": [...]}

        - "backward" 只在 direction="backward" 或 "both" 时存在
        - "forward"  只在 direction="forward" 或 "both" 时存在
        - 每个列表项是 {"id": str, "depth": int}
        - 起点本身在 backward 中(depth=0);forward 中不含起点

    Raises:
        ValueError: direction 不是三者之一
    """
    if direction not in ("backward", "forward", "both"):
        raise ValueError(f"direction 必须是 backward/forward/both,得到 {direction!r}")

    scope_clause, scope_params = build_scope_clause(scope_filter)
    result: dict = {"start": triple_id}

    # 起点必须存在且在 scope 内
    in_scope = await _is_in_scope(conn, triple_id, scope_clause, scope_params)
    if not in_scope:
        if direction in ("backward", "both"):
            result["backward"] = []
        if direction in ("forward", "both"):
            result["forward"] = []
        return result

    if direction in ("backward", "both"):
        result["backward"] = await _trace_backward(conn, triple_id, max_depth)

    if direction in ("forward", "both"):
        result["forward"] = await _trace_forward(conn, triple_id, max_depth)

    return result
