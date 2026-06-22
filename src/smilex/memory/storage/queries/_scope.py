"""SQL clause builders shared across query modules."""

from __future__ import annotations

from datetime import datetime

from ....utils.timeutil import to_iso as _dt_to_iso
from ...models import ScopeFilter


def to_iso(value: datetime | str) -> str:
    """把 datetime 或 ISO 字符串规范化为 ISO 8601 字符串."""
    if isinstance(value, datetime):
        return _dt_to_iso(value)
    return value


def build_scope_clause(
    scope_filter: ScopeFilter | None,
    *,
    column: str = "scope",
) -> tuple[str, list[str]]:
    """把 ScopeFilter 翻译为 SQL WHERE 子句(针对 scope 列).

    语义(对照 §5.3 典型场景):
    - include_global=True   →   `column = 'global'`
    - include_tenant=X      →   `column = 'tenant:X'`
    - include_project=X     →   `column = 'project:X'`
    - include_all_projects  →   `column LIKE 'project:%'`

    多个条件 OR 连接.

    Returns:
        (clause, params)
        - scope_filter 为 None → ("", [])  表示不过滤
        - 所有 include_* 都为 False → ("1=0", [])  表示匹配空集
    """
    if scope_filter is None:
        return "", []

    clauses: list[str] = []
    params: list[str] = []

    if scope_filter.include_global:
        clauses.append(f"{column} = ?")
        params.append("global")

    if scope_filter.include_tenant:
        clauses.append(f"{column} = ?")
        params.append(f"tenant:{scope_filter.include_tenant}")

    if scope_filter.include_all_projects:
        # 跨项目复盘 — 用 LIKE 匹配所有 project:%
        clauses.append(f"{column} LIKE 'project:%'")
    elif scope_filter.include_project:
        # 单项目精确匹配
        clauses.append(f"{column} = ?")
        params.append(f"project:{scope_filter.include_project}")

    if not clauses:
        return "1=0", []

    return " OR ".join(clauses), params
