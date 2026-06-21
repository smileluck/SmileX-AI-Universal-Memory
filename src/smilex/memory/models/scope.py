"""Scope filter — query-time scope filtering per main doc §5.2."""

from __future__ import annotations

from dataclasses import dataclass

from .enums import MemoryScope


@dataclass
class ScopeFilter:
    """查询时的作用域过滤(见 §5.3 典型场景)."""

    include_project: str | None = None
    include_tenant: str | None = None
    include_global: bool = True  # 默认召回通用经验
    include_all_projects: bool = False  # 跨项目复盘场景

    def matches(self, scope: MemoryScope, scope_id: str | None = None) -> bool:
        """判断 (scope, scope_id) 是否匹配此过滤.

        Args:
            scope: 记忆的 MemoryScope 枚举值
            scope_id: 记忆的具体 scope 标识(如 "project:abc"),可空

        Returns:
            True if matches, False otherwise
        """
        if scope is MemoryScope.GLOBAL:
            return self.include_global
        if scope is MemoryScope.TENANT:
            if self.include_tenant is None:
                return False
            return scope_id == self.include_tenant or scope_id is None
        if scope is MemoryScope.PROJECT:
            if self.include_all_projects:
                return True
            if self.include_project is None:
                return False
            return scope_id == self.include_project or scope_id is None
        return False
