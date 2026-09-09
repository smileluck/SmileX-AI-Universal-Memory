"""Unit tests for SQLiteEngine (L1.7).

验证:
- initialize() 流程(connect + pragmas + sqlite-vec + schema)
- 幂等性
- transaction() COMMIT / ROLLBACK
- list_tables/indexes/triggers
- 文件 DB 持久化
- 内存 DB 与文件 DB 行为一致
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smilex.memory.storage.schema import SCHEMA_VERSION
from smilex.memory.storage.sqlite_engine import SQLiteEngine


@pytest.fixture
async def engine():
    """内存引擎 + 完整初始化."""
    eng = SQLiteEngine(":memory:", load_vec=True)
    await eng.initialize()
    yield eng
    await eng.close()


# ---------- initialize() ----------

@pytest.mark.asyncio
async def test_engine_initialize_in_memory():
    """内存引擎初始化成功."""
    eng = SQLiteEngine(":memory:")
    assert not eng.is_initialized
    await eng.initialize()
    assert eng.is_initialized
    await eng.close()


@pytest.mark.asyncio
async def test_engine_initialize_idempotent_error(engine):
    """重复 initialize() 应 raise."""
    with pytest.raises(RuntimeError, match="已初始化"):
        await engine.initialize()


@pytest.mark.asyncio
async def test_engine_double_close_safe(engine):
    """double close 不报错."""
    await engine.close()
    await engine.close()  # 不应 raise
    assert not engine.is_initialized


@pytest.mark.asyncio
async def test_engine_conn_raises_before_init():
    eng = SQLiteEngine(":memory:")
    with pytest.raises(RuntimeError, match="未初始化"):
        _ = eng.conn
    await eng.close()


# ---------- Pragmas ----------

@pytest.mark.asyncio
async def test_pragma_wal_applied(engine):
    cursor = await engine.conn.execute("PRAGMA journal_mode")
    row = await cursor.fetchone()
    # :memory: 可能返回 memory,文件 DB 才是 wal
    assert row[0] in ("wal", "memory")


@pytest.mark.asyncio
async def test_pragma_foreign_keys_applied(engine):
    cursor = await engine.conn.execute("PRAGMA foreign_keys")
    row = await cursor.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_pragma_busy_timeout_applied(engine):
    cursor = await engine.conn.execute("PRAGMA busy_timeout")
    row = await cursor.fetchone()
    assert row[0] == 5000


@pytest.mark.asyncio
async def test_custom_pragmas_override_defaults():
    eng = SQLiteEngine(":memory:", pragmas={"busy_timeout": 9999})
    await eng.initialize()
    cursor = await eng.conn.execute("PRAGMA busy_timeout")
    row = await cursor.fetchone()
    assert row[0] == 9999
    await eng.close()


# ---------- Schema migrations ----------

@pytest.mark.asyncio
async def test_schema_user_version_set(engine):
    """初始化后 user_version > 0."""
    version = await engine.get_user_version()
    assert version >= 9  # 至少应用了 9 张表


@pytest.mark.asyncio
async def test_all_tables_present(engine):
    tables = set(await engine.list_tables())
    expected = {
        "entities",
        "triples",
        "locations",
        "temporal_fragments",
        "vector_links",
        "checkpoints",
        "project_current_state",
        "memory_l0_snapshot",
    }
    missing = expected - tables
    assert not missing, f"缺失表: {missing}"


@pytest.mark.asyncio
async def test_virtual_tables_present(engine):
    """sqlite-vec + R-tree 虚拟表创建."""
    cursor = await engine.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('memory_vectors', 'memory_locations_rtree')"
    )
    rows = await cursor.fetchall()
    names = {r[0] for r in rows}
    assert "memory_vectors" in names
    assert "memory_locations_rtree" in names


@pytest.mark.asyncio
async def test_trigger_present(engine):
    triggers = await engine.list_triggers()
    assert "trg_update_current_state" in triggers


# ---------- transaction() ----------

@pytest.mark.asyncio
async def test_transaction_commit(engine):
    async with engine.transaction() as conn:
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("e1", "person:alice", "person", "Alice", "global", "2026-01-01T00:00:00Z"),
        )

    cursor = await engine.conn.execute("SELECT count(*) FROM entities")
    row = await cursor.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_transaction_rollback_on_exception(engine):
    with pytest.raises(ValueError, match="rollback test"):
        async with engine.transaction() as conn:
            await conn.execute(
                "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("e1", "person:alice", "person", "Alice", "global", "2026-01-01T00:00:00Z"),
            )
            raise ValueError("rollback test")

    cursor = await engine.conn.execute("SELECT count(*) FROM entities")
    row = await cursor.fetchone()
    assert row[0] == 0


# ---------- File DB persistence ----------

@pytest.mark.asyncio
async def test_file_db_persists_schema_across_engines(tmp_path: Path):
    """文件 DB 的 schema 应在重新打开后仍存在."""
    db_file = tmp_path / "test.db"

    eng1 = SQLiteEngine(db_file)
    await eng1.initialize()
    version1 = await eng1.get_user_version()
    tables1 = await eng1.list_tables()
    await eng1.close()

    # 重新打开同一文件
    eng2 = SQLiteEngine(db_file)
    await eng2.initialize()
    version2 = await eng2.get_user_version()
    tables2 = await eng2.list_tables()
    await eng2.close()

    assert version1 == version2
    assert set(tables1) == set(tables2)


@pytest.mark.asyncio
async def test_file_db_persists_data(tmp_path: Path):
    """文件 DB 的数据应在重新打开后仍存在."""
    db_file = tmp_path / "test.db"

    eng1 = SQLiteEngine(db_file)
    await eng1.initialize()
    async with eng1.transaction() as conn:
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("e1", "person:alice", "person", "Alice", "global", "2026-01-01T00:00:00Z"),
        )
    await eng1.close()

    eng2 = SQLiteEngine(db_file)
    await eng2.initialize()
    cursor = await eng2.conn.execute("SELECT count(*) FROM entities")
    count = (await cursor.fetchone())[0]
    await eng2.close()

    assert count == 1


# ---------- load_vec=False path ----------

@pytest.mark.asyncio
async def test_engine_without_vec():
    """load_vec=False 也能初始化(010 被跳过,其余迁移含 011 仍应用)."""
    eng = SQLiteEngine(":memory:", load_vec=False)
    await eng.initialize()
    version = await eng.get_user_version()
    assert version == SCHEMA_VERSION  # 011 归档表不依赖 sqlite-vec

    # 虚拟表不应该存在
    cursor = await eng.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='memory_vectors'"
    )
    assert (await cursor.fetchone()) is None
    # 归档表应该存在
    cursor = await eng.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='triples_archive'"
    )
    assert (await cursor.fetchone()) is not None
    await eng.close()


# ---------- End-to-end sqlite-vec via engine ----------

@pytest.mark.asyncio
async def test_vec0_knn_via_engine(engine):
    """通过 engine 跑 KNN 查询(sqlite-vec 加载成功)."""
    # 插入 3 个向量(前 3 维有意义)
    v1 = str([1.0] + [0.0] * 1023)
    v2 = str([0.0, 1.0] + [0.0] * 1022)
    v3 = str([0.9, 0.1] + [0.0] * 1022)

    async with engine.transaction() as conn:
        await conn.execute(
            "INSERT INTO memory_vectors(vector_id, embedding) VALUES (?, ?)",
            (1, v1),
        )
        await conn.execute(
            "INSERT INTO memory_vectors(vector_id, embedding) VALUES (?, ?)",
            (2, v2),
        )
        await conn.execute(
            "INSERT INTO memory_vectors(vector_id, embedding) VALUES (?, ?)",
            (3, v3),
        )

    query = str([1.0] + [0.0] * 1023)
    cursor = await engine.conn.execute(
        "SELECT vector_id, distance FROM memory_vectors "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT 2",
        (query,),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 2
    assert rows[0]["vector_id"] == 1
    assert rows[1]["vector_id"] == 3
