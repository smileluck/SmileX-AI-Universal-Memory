"""Unit tests for Layer 1 schema (DDL + loader).

Verifies:
- All 9 base tables + 2 virtual tables can be created
- Indexes and triggers are properly registered
- Schema versioning via PRAGMA user_version works
- Migrations are idempotent
- Triple CHECK constraints enforce Layer 0 validation
- project_current_state trigger auto-updates on relevant triples
"""

from __future__ import annotations

import sqlite3

import pytest

from smilex.memory.storage.schema import (
    SCHEMA_VERSION,
    apply_migrations,
    get_user_version,
    list_indexes,
    list_tables,
    list_triggers,
)


@pytest.fixture
def db_with_schema():
    """内存 SQLite + 完整 schema(含 sqlite-vec)."""
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, load_vec_extension=True)
    yield conn
    conn.close()


@pytest.fixture
def db_no_vec():
    """内存 SQLite + 不含 010 虚拟表(schema 版本只到 9)."""
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, load_vec_extension=False)
    yield conn
    conn.close()


# ---------- Schema versioning ----------

def test_initial_user_version_zero():
    """新数据库 user_version 应该是 0."""
    conn = sqlite3.connect(":memory:")
    assert get_user_version(conn) == 0
    conn.close()


def test_apply_migrations_sets_user_version(db_with_schema):
    """应用 migration 后 user_version = SCHEMA_VERSION(10)."""
    assert get_user_version(db_with_schema) == SCHEMA_VERSION


def test_apply_migrations_idempotent():
    """多次调用 apply_migrations 不应有副作用."""
    conn = sqlite3.connect(":memory:")
    v1 = apply_migrations(conn, load_vec_extension=True)
    v2 = apply_migrations(conn, load_vec_extension=True)
    assert v1 == v2 == SCHEMA_VERSION
    # 表数也应该一致
    n1 = len(list_tables(conn))
    apply_migrations(conn, load_vec_extension=True)
    n2 = len(list_tables(conn))
    assert n1 == n2
    conn.close()


# ---------- Tables ----------

def test_all_base_tables_created(db_with_schema):
    """9 张基础表全部创建."""
    tables = set(list_tables(db_with_schema))
    expected = {
        "entities",
        "triples",
        "locations",
        "temporal_fragments",
        "causal_chains",
        "vector_links",
        "checkpoints",
        "project_current_state",
        "memory_l0_snapshot",
    }
    missing = expected - tables
    assert not missing, f"缺失表: {missing}"


def test_virtual_tables_created(db_with_schema):
    """2 张虚拟表(memory_vectors + memory_locations_rtree)创建."""
    # 虚拟表也会出现在 sqlite_master 中
    cursor = db_with_schema.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('memory_vectors', 'memory_locations_rtree')"
    )
    virtual = {row[0] for row in cursor.fetchall()}
    assert "memory_vectors" in virtual
    assert "memory_locations_rtree" in virtual


def test_skip_virtual_tables_without_extension():
    """load_vec_extension=False 应该跳过 010,但其余迁移(含 011)仍应用."""
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, load_vec_extension=False)
    tables = set(list_tables(conn))
    assert "entities" in tables
    assert "triples" in tables
    assert "triples_archive" in tables  # 011 归档表不依赖 sqlite-vec
    # memory_vectors 不应该存在
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='memory_vectors'"
    )
    assert cursor.fetchone() is None
    # user_version 越过 010 到最新(011 不依赖虚拟表,见 schema loader docstring)
    assert get_user_version(conn) == SCHEMA_VERSION
    conn.close()


def test_vec_tables_backfilled_after_skip():
    """无扩展建库(跳过 010)后,以带扩展方式重开应补建 010 虚拟表(P2 修复)."""
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, load_vec_extension=False)
    assert get_user_version(conn) == SCHEMA_VERSION
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE name='memory_vectors'")
    assert cursor.fetchone() is None

    # 重开(带 sqlite-vec): 010 被跳过过一次,按表存在性补建
    apply_migrations(conn, load_vec_extension=True)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE name='memory_vectors'")
    assert cursor.fetchone() is not None
    # user_version 不回退、不重复推进
    assert get_user_version(conn) == SCHEMA_VERSION
    conn.close()


# ---------- Indexes ----------

def test_key_indexes_created(db_with_schema):
    """关键索引创建."""
    indexes = set(list_indexes(db_with_schema))
    expected_subsets = {
        "idx_entities_scope_id",
        "idx_entities_valid_from",
        "idx_triples_subject_pred",
        "idx_triples_predecessor",
        "idx_triples_scope_time",
        "idx_locations_path",
        "idx_temporal_start",
        "uq_vector_links_db_id",
        "idx_checkpoints_task",
    }
    missing = expected_subsets - indexes
    assert not missing, f"缺失索引: {missing}"


# ---------- Triggers ----------

def test_project_current_state_trigger_created(db_with_schema):
    """project_current_state 触发器创建."""
    triggers = set(list_triggers(db_with_schema))
    assert "trg_update_current_state" in triggers


def test_trigger_fires_on_project_state_insert(db_with_schema):
    """插入 project_state 类型的 triple 应该自动更新 project_current_state."""
    conn = db_with_schema
    # 准备: 插入一个 project scope 的 entity(scope 全路径)
    conn.execute(
        "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("e1", "project:proj_a", "concept", "Project A", "project:proj_a", "2026-01-01T00:00:00Z"),
    )
    # 插入一个 project_state triple(scope 以 'project:' 前缀)
    conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
        "scope, valid_from, relation_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("t1", "tid1", "e1", "status", "active", "project:proj_a",
         "2026-01-01T00:00:00Z", "project_state"),
    )
    conn.commit()

    # project_current_state 应该有一条记录
    cursor = conn.execute(
        "SELECT scope, subject_id, predicate, object_value FROM project_current_state"
    )
    rows = cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "project:proj_a"
    assert rows[0][3] == "active"


def test_trigger_skips_non_project_scope(db_with_schema):
    """非 project: 前缀的 scope 应该不触发(全局/租户)."""
    conn = db_with_schema
    conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
        "scope, valid_from, relation_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("t1", "tid1", "s1", "p", "v", "global",
         "2026-01-01T00:00:00Z", "project_state"),
    )
    conn.commit()

    cursor = conn.execute("SELECT count(*) FROM project_current_state")
    assert cursor.fetchone()[0] == 0


def test_trigger_skips_non_state_relation(db_with_schema):
    """relation_type 不是 project_state/task_status/config 的不触发."""
    conn = db_with_schema
    conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
        "scope, valid_from, relation_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("t1", "tid1", "s1", "knows", "x", "project:proj_a",
         "2026-01-01T00:00:00Z", "semantic"),
    )
    conn.commit()

    cursor = conn.execute("SELECT count(*) FROM project_current_state")
    assert cursor.fetchone()[0] == 0


def test_trigger_upserts_on_duplicate(db_with_schema):
    """同 subject+predicate 的新版本应该 UPSERT(不插入新行)."""
    conn = db_with_schema
    conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
        "scope, valid_from, relation_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("t1", "tid1", "s1", "status", "active", "project:proj_a",
         "2026-01-01T00:00:00Z", "project_state"),
    )
    conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
        "scope, valid_from, relation_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("t2", "tid2", "s1", "status", "completed", "project:proj_a",
         "2026-06-01T00:00:00Z", "project_state"),
    )
    conn.commit()

    # 只应该有 1 条当前状态(最新版本)
    cursor = conn.execute(
        "SELECT object_value FROM project_current_state WHERE subject_id=? AND predicate=?",
        ("s1", "status"),
    )
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "completed"


# ---------- CHECK constraints ----------

def test_triples_check_object_required(db_with_schema):
    """CHECK: object_id 或 object_value 至少一个非空."""
    with pytest.raises(sqlite3.IntegrityError):
        db_with_schema.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, scope, valid_from) "
            "VALUES ('t1', 'tid1', 's1', 'p', 'project', '2026-01-01T00:00:00Z')"
        )


def test_triples_check_confidence_range(db_with_schema):
    """CHECK: confidence 必须在 [0, 1]."""
    with pytest.raises(sqlite3.IntegrityError):
        db_with_schema.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
            "scope, valid_from, confidence) "
            "VALUES ('t1', 'tid1', 's1', 'p', 'v', 'project', '2026-01-01T00:00:00Z', 1.5)"
        )


def test_triples_check_causal_level_nonneg(db_with_schema):
    """CHECK: causal_level >= 0."""
    with pytest.raises(sqlite3.IntegrityError):
        db_with_schema.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
            "scope, valid_from, causal_level) "
            "VALUES ('t1', 'tid1', 's1', 'p', 'v', 'project', '2026-01-01T00:00:00Z', -1)"
        )


def test_triples_check_scope_enum(db_with_schema):
    """CHECK: scope 必须是 project/tenant/global."""
    with pytest.raises(sqlite3.IntegrityError):
        db_with_schema.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
            "scope, valid_from) "
            "VALUES ('t1', 'tid1', 's1', 'p', 'v', 'invalid_scope', '2026-01-01T00:00:00Z')"
        )


def test_causal_chains_check_json_valid(db_with_schema):
    """CHECK: node_ids 必须是有效 JSON."""
    with pytest.raises(sqlite3.IntegrityError):
        db_with_schema.execute(
            "INSERT INTO causal_chains(id, chain_id, node_ids, created_at, updated_at) "
            "VALUES ('c1', 'ch1', 'not-json', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )


def test_causal_chains_check_node_ids_nonempty(db_with_schema):
    """CHECK: node_ids 至少 1 个元素."""
    with pytest.raises(sqlite3.IntegrityError):
        db_with_schema.execute(
            "INSERT INTO causal_chains(id, chain_id, node_ids, created_at, updated_at) "
            "VALUES ('c1', 'ch1', '[]', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )


def test_vector_links_check_at_least_one_ref(db_with_schema):
    """CHECK: vector_links 至少有一个业务关联."""
    with pytest.raises(sqlite3.IntegrityError):
        db_with_schema.execute(
            "INSERT INTO vector_links(id, vector_id, dimension, created_at) "
            "VALUES ('v1', 1, 1024, '2026-01-01T00:00:00Z')"
        )


def test_checkpoints_check_progress_range(db_with_schema):
    """CHECK: progress 必须 [0, 1]."""
    with pytest.raises(sqlite3.IntegrityError):
        db_with_schema.execute(
            "INSERT INTO checkpoints(id, task_id, progress, step, state, created_at, updated_at) "
            "VALUES ('cp1', 'task1', 1.5, 0, '{}', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )


# ---------- sqlite-vec KNN smoke ----------

def test_virtual_table_vec0_knn(db_with_schema):
    """memory_vectors 虚拟表 KNN 查询可用(决策 D1 验证)."""
    conn = db_with_schema
    # 插入 3 个 1024 维向量(简化:前 3 维有意义,其余 0)
    v1 = [1.0] + [0.0] * 1023
    v2 = [0.0, 1.0] + [0.0] * 1022
    v3 = [0.9] + [0.1] + [0.0] * 1022  # 接近 v1

    conn.execute(
        "INSERT INTO memory_vectors(vector_id, embedding) VALUES (?, ?)",
        (1, str(v1)),
    )
    conn.execute(
        "INSERT INTO memory_vectors(vector_id, embedding) VALUES (?, ?)",
        (2, str(v2)),
    )
    conn.execute(
        "INSERT INTO memory_vectors(vector_id, embedding) VALUES (?, ?)",
        (3, str(v3)),
    )

    # KNN 查询接近 [1, 0, 0, ...] 的 Top-2
    query = [1.0] + [0.0] * 1023
    rows = conn.execute(
        "SELECT vector_id, distance FROM memory_vectors "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT 2",
        (str(query),),
    ).fetchall()

    assert len(rows) == 2
    assert rows[0][0] == 1  # vector_id=1 距离最近(应该为 0)
    assert rows[1][0] == 3  # vector_id=3 接近 v1
