"""Scope filter factories + SQL helper.

Per main doc §5.3 典型场景:
- 项目内对话 → project_scope(project_id)
- 跨项目复盘 → cross_project_scope()
- 架构决策   → global_only_scope()
- 租户隔离   → tenant_scope(tenant_id)

提供:
- 4 个工厂函数快速构造 ScopeFilter
- apply_scope_filter(base_sql, params, scope_filter) — 把过滤应用到 SQL
"""

from __future__ import annotations

from ...models import ScopeFilter
from ._scope import build_scope_clause, to_iso


def project_scope(project_id: str, *, include_global: bool = True) -> ScopeFilter:
    """项目内对话 — §5.3 场景 1.

    召回: 本项目 + 通用经验(默认)
    用法: `recall(query, scope_filter=project_scope("proj_a"))`
    """
    return ScopeFilter(
        include_project=project_id,
        include_global=include_global,
        include_all_projects=False,
    )


def cross_project_scope(*, include_global: bool = True) -> ScopeFilter:
    """跨项目复盘 — §5.3 场景 2.

    召回: 所有项目 + 通用经验(默认)
    用法: 复盘多项目共性问题时使用
    """
    return ScopeFilter(
        include_project=None,
        include_global=include_global,
        include_all_projects=True,
    )


def global_only_scope() -> ScopeFilter:
    """纯架构经验 — §5.3 场景 3.

    召回: 仅全局通用经验,不含任何项目专属
    用法: 架构决策、通用模式查询
    """
    return ScopeFilter(
        include_project=None,
        include_global=True,
        include_all_projects=False,
    )


def tenant_scope(tenant_id: str, *, include_global: bool = True) -> ScopeFilter:
    """多租户隔离 — §5.3 场景 4.

    召回: 本租户 + 通用经验(默认)
    用法: 多租户 SaaS 场景
    """
    return ScopeFilter(
        include_tenant=tenant_id,
        include_global=include_global,
        include_all_projects=False,
    )


def apply_scope_filter(
    base_sql: str,
    params: list,
    scope_filter: ScopeFilter | None,
    *,
    column: str = "scope",
    connector: str = "AND",
) -> tuple[str, list]:
    """把 scope 过滤附加到已有 SQL 上.

    Args:
        base_sql: 基础 SQL 语句(可能已含 WHERE 子句)
        params: 基础 SQL 的参数列表
        scope_filter: ScopeFilter 对象,None 表示不过滤
        column: scope 列名(默认 "scope")
        connector: 连接词(默认 "AND",可选 "OR" / "AND NOT")

    Returns:
        (new_sql, new_params)

    语义:
        - scope_filter=None → 原样返回
        - scope_filter 全部 include=False → 附加 "AND 1=0"(保证空结果)
        - base_sql 已含 WHERE → 附加 "AND (...)"
        - base_sql 不含 WHERE → 改写为 "WHERE (...)"

    示例:
        >>> sql, params = apply_scope_filter(
        ...     "SELECT * FROM triples WHERE valid_from <= ?",
        ...     ["2025-01-01"],
        ...     project_scope("proj_a"),
        ... )
        >>> sql
        'SELECT * FROM triples WHERE valid_from <= ? AND (scope = ? OR scope = ?)'
        >>> params
        ['2025-01-01', 'project:proj_a', 'global']
    """
    clause, clause_params = build_scope_clause(scope_filter, column=column)
    if not clause:
        return base_sql, params

    upper = base_sql.upper()
    has_where = " WHERE " in upper
    new_sql = (
        f"{base_sql} {connector} ({clause})"
        if has_where
        else f"{base_sql} WHERE ({clause})"
    )

    return new_sql, [*params, *clause_params]


__all__ = [
    # Factories
    "project_scope",
    "cross_project_scope",
    "global_only_scope",
    "tenant_scope",
    # Helpers
    "apply_scope_filter",
    "build_scope_clause",
    "to_iso",
]
