"""Unit tests for ScopeFilter."""

from smilex.memory.models import MemoryScope, ScopeFilter


def test_scope_filter_defaults():
    """默认: include_global=True, 其余 False/None."""
    f = ScopeFilter()
    assert f.include_project is None
    assert f.include_tenant is None
    assert f.include_global is True
    assert f.include_all_projects is False


def test_matches_global():
    """全局 scope 默认匹配(include_global=True)."""
    f = ScopeFilter()
    assert f.matches(MemoryScope.GLOBAL) is True

    f_no_global = ScopeFilter(include_global=False)
    assert f_no_global.matches(MemoryScope.GLOBAL) is False


def test_matches_project_exact():
    """项目 scope 精确匹配."""
    f = ScopeFilter(include_project="proj_a")
    assert f.matches(MemoryScope.PROJECT, "proj_a") is True
    assert f.matches(MemoryScope.PROJECT, "proj_b") is False


def test_matches_project_all():
    """include_all_projects 匹配所有 project scope."""
    f = ScopeFilter(include_all_projects=True)
    assert f.matches(MemoryScope.PROJECT, "proj_a") is True
    assert f.matches(MemoryScope.PROJECT, "proj_b") is True
    # 但不匹配 tenant
    assert f.matches(MemoryScope.TENANT, "tenant_x") is False


def test_matches_tenant():
    """租户 scope 精确匹配."""
    f = ScopeFilter(include_tenant="tenant_x")
    assert f.matches(MemoryScope.TENANT, "tenant_x") is True
    assert f.matches(MemoryScope.TENANT, "tenant_y") is False


def test_matches_cross_project_review():
    """跨项目复盘场景 — §5.3."""
    # 包含所有项目 + 全局
    f = ScopeFilter(include_all_projects=True, include_global=True)
    assert f.matches(MemoryScope.PROJECT, "any_project") is True
    assert f.matches(MemoryScope.GLOBAL) is True


def test_matches_within_project_conversation():
    """项目内对话场景 — §5.3."""
    f = ScopeFilter(include_project="current", include_global=True)
    assert f.matches(MemoryScope.PROJECT, "current") is True
    assert f.matches(MemoryScope.GLOBAL) is True
    assert f.matches(MemoryScope.PROJECT, "other") is False
