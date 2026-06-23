"""Unit tests for scope filter factories and helper (L1.12).

验证:
- 4 个工厂函数(project / cross_project / global_only / tenant)
- apply_scope_filter SQL 助手(WHERE 处理 / 不含 WHERE / None / 空集)
- build_scope_clause 在不同 filter 下的输出
"""

from __future__ import annotations

from smilex.memory.models import ScopeFilter
from smilex.memory.storage.queries import (
    apply_scope_filter,
    build_scope_clause,
    cross_project_scope,
    global_only_scope,
    project_scope,
    tenant_scope,
)

# ---------- 工厂函数 ----------

def test_project_scope_factory():
    """§5.3 场景 1: 项目内对话."""
    sf = project_scope("proj_a")
    assert sf.include_project == "proj_a"
    assert sf.include_global is True
    assert sf.include_all_projects is False
    assert sf.include_tenant is None


def test_project_scope_no_global():
    sf = project_scope("proj_a", include_global=False)
    assert sf.include_global is False
    assert sf.include_project == "proj_a"


def test_cross_project_scope_factory():
    """§5.3 场景 2: 跨项目复盘."""
    sf = cross_project_scope()
    assert sf.include_all_projects is True
    assert sf.include_global is True
    assert sf.include_project is None


def test_global_only_scope_factory():
    """§5.3 场景 3: 纯架构经验."""
    sf = global_only_scope()
    assert sf.include_global is True
    assert sf.include_project is None
    assert sf.include_all_projects is False
    assert sf.include_tenant is None


def test_tenant_scope_factory():
    """§5.3 场景 4: 多租户隔离."""
    sf = tenant_scope("tenant_x")
    assert sf.include_tenant == "tenant_x"
    assert sf.include_global is True
    assert sf.include_project is None


def test_tenant_scope_no_global():
    sf = tenant_scope("tenant_x", include_global=False)
    assert sf.include_global is False


# ---------- build_scope_clause ----------

def test_build_scope_clause_none():
    """scope_filter=None → 空子句."""
    clause, params = build_scope_clause(None)
    assert clause == ""
    assert params == []


def test_build_scope_clause_global_only():
    sf = global_only_scope()
    clause, params = build_scope_clause(sf)
    assert "scope = ?" in clause
    assert params == ["global"]


def test_build_scope_clause_project_with_global():
    sf = project_scope("proj_a")
    clause, params = build_scope_clause(sf)
    # 两段 OR: project:proj_a + global
    assert " OR " in clause
    assert "project:proj_a" in params
    assert "global" in params


def test_build_scope_clause_cross_project():
    sf = cross_project_scope()
    clause, params = build_scope_clause(sf)
    assert "LIKE 'project:%'" in clause
    assert "global" in params


def test_build_scope_clause_all_false():
    """所有 include=False → "1=0" 空集."""
    sf = ScopeFilter(include_global=False)
    clause, params = build_scope_clause(sf)
    assert clause == "1=0"
    assert params == []


def test_build_scope_clause_custom_column():
    sf = global_only_scope()
    clause, _ = build_scope_clause(sf, column="t.scope")
    assert "t.scope" in clause


# ---------- apply_scope_filter ----------

def test_apply_scope_filter_none():
    """scope_filter=None → 原样返回."""
    sql, params = apply_scope_filter(
        "SELECT * FROM t WHERE x = ?",
        ["val"],
        None,
    )
    assert sql == "SELECT * FROM t WHERE x = ?"
    assert params == ["val"]


def test_apply_scope_filter_with_existing_where():
    """已有 WHERE → 附加 AND."""
    sql, params = apply_scope_filter(
        "SELECT * FROM triples WHERE valid_from <= ?",
        ["2025-01-01"],
        project_scope("proj_a"),
    )
    assert "AND" in sql
    assert "(" in sql  # clause 被括号包裹
    assert sql.count("?") == 3  # 原 1 + scope 2
    assert params[0] == "2025-01-01"
    assert "project:proj_a" in params
    assert "global" in params


def test_apply_scope_filter_without_where():
    """无 WHERE → 新加 WHERE."""
    sql, params = apply_scope_filter(
        "SELECT * FROM triples",
        [],
        global_only_scope(),
    )
    assert "WHERE" in sql.upper()
    assert "scope = ?" in sql
    assert params == ["global"]


def test_apply_scope_filter_empty_result_set():
    """scope 全 False → "1=0" 强制空结果."""
    sf = ScopeFilter(include_global=False, include_project=None, include_all_projects=False)
    sql, params = apply_scope_filter("SELECT * FROM t", [], sf)
    assert "1=0" in sql


def test_apply_scope_filter_custom_column():
    sql, params = apply_scope_filter(
        "SELECT * FROM triples",
        [],
        global_only_scope(),
        column="t.scope",
    )
    assert "t.scope" in sql


def test_apply_scope_filter_preserves_params_order():
    """参数顺序: 原 params 在前,scope_params 在后."""
    sql, params = apply_scope_filter(
        "SELECT * FROM t WHERE a = ? AND b = ?",
        ["a_val", "b_val"],
        project_scope("proj_a"),
    )
    assert params[0] == "a_val"
    assert params[1] == "b_val"
    # 后面是 scope params
    assert "project:proj_a" in params[2:]
    assert "global" in params[2:]


def test_apply_scope_filter_tenant():
    """tenant scope 应用."""
    sql, params = apply_scope_filter(
        "SELECT * FROM entities",
        [],
        tenant_scope("tenant_x"),
    )
    assert "tenant:tenant_x" in params
    assert "global" in params


# ---------- 端到端: 工厂 + build + apply ----------

def test_factory_chain_works():
    """工厂 + build + apply 链式调用."""
    sf = project_scope("myproj")
    clause, params = build_scope_clause(sf)
    assert clause  # 非空

    sql, sql_params = apply_scope_filter(
        "SELECT * FROM triples WHERE predicate = ?",
        ["knows"],
        sf,
    )
    # 验证最终 SQL 与参数可被 SQLite 执行(语法层面)
    assert "predicate = ?" in sql
    assert "scope = ?" in sql
    assert "?" in sql
    assert len(sql_params) >= 3  # 1 (predicate) + 2 (scope)


def test_factory_chaining_no_global():
    """仅项目内,不含 global."""
    sf = project_scope("proj_a", include_global=False)
    sql, params = apply_scope_filter("SELECT * FROM t", [], sf)
    # 只有 project:proj_a,没有 global
    assert "project:proj_a" in params
    assert "global" not in params
