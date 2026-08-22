"""Temporal queries — time-point / time-range / entity timeline.

Per main doc §6.5 检索能力矩阵:
- query_at_time(t)            P99 < 5ms
- query_in_range(start, end)  P99 < 10ms
- build_entity_timeline(e)    P99 < 20ms

基于 triples / entities 表的 valid_from/valid_to 字段实现.
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from ...models import ScopeFilter
from ._scope import build_scope_clause, to_iso


async def query_at_time(
    conn: aiosqlite.Connection,
    t: datetime | str,
    *,
    table: str = "triples",
    scope_filter: ScopeFilter | None = None,
) -> list[aiosqlite.Row]:
    """时间点查询 — 找出在时间 t 有效的记录.

    语义: `valid_from <= t AND (valid_to IS NULL OR valid_to > t)`.

    Args:
        conn: 已初始化的 aiosqlite 连接
        t: 时间点(datetime 或 ISO 8601 字符串)
        table: 查询表,"triples" 或 "entities"(均使用 valid_from/valid_to)
        scope_filter: 作用域过滤,None 表示不过滤

    Returns:
        匹配的行列表(按 valid_from 升序)
    """
    if table not in ("triples", "entities"):
        raise ValueError(f"query_at_time 仅支持 triples/entities,得到 {table!r}")

    t_str = to_iso(t)
    where_clauses = ["valid_from <= ?", "(valid_to IS NULL OR valid_to > ?)"]
    params: list[str] = [t_str, t_str]

    scope_clause, scope_params = build_scope_clause(scope_filter)
    if scope_clause:
        where_clauses.append(f"({scope_clause})")
        params.extend(scope_params)

    sql = (
        f"SELECT * FROM {table} WHERE "
        + " AND ".join(where_clauses)
        + " ORDER BY valid_from"
    )

    cursor = await conn.execute(sql, params)
    return list(await cursor.fetchall())


async def query_in_range(
    conn: aiosqlite.Connection,
    start: datetime | str,
    end: datetime | str,
    *,
    table: str = "triples",
    scope_filter: ScopeFilter | None = None,
    limit: int | None = None,
) -> list[aiosqlite.Row]:
    """时间范围查询 — 找出与 [start, end] 有重叠的记录.

    语义(§6.5):
        valid_from <= end AND (valid_to IS NULL OR valid_to >= start)

    重叠定义: 记录的有效期 [valid_from, valid_to] 与查询范围 [start, end] 有交集.

    Args:
        start: 范围开始(含)
        end: 范围结束(含)
        table: 查询表,"triples" 或 "entities"
        scope_filter: 作用域过滤
        limit: 返回条数上限(M3: 下推 SQL LIMIT,避免大范围全量拉回后 Python 切片)

    Returns:
        匹配的行列表(按 valid_from 升序)
    """
    if table not in ("triples", "entities"):
        raise ValueError(f"query_in_range 仅支持 triples/entities,得到 {table!r}")

    start_str = to_iso(start)
    end_str = to_iso(end)
    where_clauses = ["valid_from <= ?", "(valid_to IS NULL OR valid_to >= ?)"]
    params: list = [end_str, start_str]

    scope_clause, scope_params = build_scope_clause(scope_filter)
    if scope_clause:
        where_clauses.append(f"({scope_clause})")
        params.extend(scope_params)

    sql = (
        f"SELECT * FROM {table} WHERE "
        + " AND ".join(where_clauses)
        + " ORDER BY valid_from"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    cursor = await conn.execute(sql, params)
    return list(await cursor.fetchall())


async def build_entity_timeline(
    conn: aiosqlite.Connection,
    entity_id: str,
    *,
    include_as_subject: bool = True,
    include_as_object: bool = True,
    scope_filter: ScopeFilter | None = None,
) -> list[aiosqlite.Row]:
    """构建实体时间线 — 所有以 entity_id 为主语或宾语的 triples,按时间排序.

    Args:
        entity_id: 实体 ID(对应 entities.id)
        include_as_subject: 是否包含 entity 作为 subject 的 triples
        include_as_object: 是否包含 entity 作为 object 的 triples
        scope_filter: 作用域过滤

    Returns:
        合并排序后的 triples 列表(按 valid_from 升序)

    Raises:
        ValueError: 两个 include 都为 False
    """
    if not include_as_subject and not include_as_object:
        raise ValueError("至少一个 include 必须为 True")

    scope_clause, scope_params = build_scope_clause(scope_filter)
    scope_where = f" AND ({scope_clause})" if scope_clause else ""

    # 用 UNION ALL 合并 subject + object 角色
    select_parts: list[str] = []
    params: list[str] = []
    for include, where_col in [
        (include_as_subject, "subject_id"),
        (include_as_object, "object_id"),
    ]:
        if include:
            select_parts.append(
                f"SELECT * FROM triples WHERE {where_col} = ?{scope_where}"
            )
            params.append(entity_id)
            if scope_clause:
                params.extend(scope_params)

    union_sql = " UNION ALL ".join(select_parts)
    sql = f"SELECT * FROM ({union_sql}) AS timeline ORDER BY valid_from"

    cursor = await conn.execute(sql, params)
    return list(await cursor.fetchall())
