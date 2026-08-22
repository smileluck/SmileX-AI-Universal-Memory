"""Unit tests for VectorStore (L2.5).

验证:
- 写入: add_text / add_text → memory_vectors + vector_links 映射
- KNN: knn_search 距离排序 / 相同文本距离为 0 / scope 过滤
- 删除: 按 entity_id / vector_id 删除后 KNN 不再命中
- 参数校验: 维度不匹配 / 无业务关联 / 删除条件不唯一
"""

from __future__ import annotations

import numpy as np
import pytest

from smilex.memory.lifecycle.embedder import HashEmbedder
from smilex.memory.models import ScopeFilter
from smilex.memory.storage.sqlite_engine import SQLiteEngine
from smilex.memory.storage.vector_store import VectorStore

# ---------- Fixtures ----------


@pytest.fixture
async def store_engine(tmp_path):
    """临时文件 db(加载 sqlite-vec)+ 3 个实体 + VectorStore(HashEmbedder)."""
    eng = SQLiteEngine(tmp_path / "vec_test.db", load_vec=True)
    await eng.initialize()
    conn = eng.conn

    entities = [
        ("e1", "person:alice", "person", "Alice", "global"),
        ("e2", "person:bob", "person", "Bob", "global"),
        ("e3", "proj:alpha", "concept", "Project Alpha", "project:proj_a"),
    ]
    for eid, entity_id, etype, name, scope in entities:
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (eid, entity_id, etype, name, scope, "2025-01-01T00:00:00.000000Z"),
        )
    await conn.commit()

    store = VectorStore(embedder=HashEmbedder())
    yield store, eng
    await eng.close()


# ---------- 写入 ----------

@pytest.mark.asyncio
async def test_add_text_writes_vector_and_link(store_engine):
    """add_text 写入 memory_vectors + vector_links,返回 rowid."""
    store, eng = store_engine
    async with eng.transaction() as conn:
        vid = await store.add_text(conn, "alice works at CompanyX", entity_id="e1")
    assert isinstance(vid, int) and vid > 0

    conn = eng.conn
    cursor = await conn.execute(
        "SELECT vector_id, entity_id, dimension, vector_db FROM vector_links "
        "WHERE entity_id = 'e1'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["vector_id"] == vid
    assert row["dimension"] == 1024
    assert row["vector_db"] == "sqlite_vec"


@pytest.mark.asyncio
async def test_add_vector_dimension_mismatch(store_engine):
    """维度不匹配的向量应 raise ValueError."""
    store, eng = store_engine
    async with eng.transaction() as conn:
        with pytest.raises(ValueError, match="维度不匹配"):
            await store.add_vector(conn, [0.1] * 512, entity_id="e1")


@pytest.mark.asyncio
async def test_add_vector_requires_business_link(store_engine):
    """三个业务关联全 None 应 raise ValueError."""
    store, eng = store_engine
    async with eng.transaction() as conn:
        with pytest.raises(ValueError, match="至少给一个"):
            await store.add_vector(conn, np.zeros(1024, dtype=np.float32))


# ---------- KNN ----------

@pytest.mark.asyncio
async def test_knn_search_same_text_distance_zero(store_engine):
    """HashEmbedder 确定性: 相同文本查询距离为 0,排在最前."""
    store, eng = store_engine
    async with eng.transaction() as conn:
        await store.add_text(conn, "alice works at CompanyX", entity_id="e1")
        await store.add_text(conn, "bob reviewed PR-123", entity_id="e2")

    hits = await store.knn_search(eng.conn, "alice works at CompanyX", k=2)
    assert len(hits) == 2
    assert hits[0].entity_id == "e1"
    assert hits[0].memory_id == "e1"
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6)
    # 距离升序
    assert hits[0].distance <= hits[1].distance


@pytest.mark.asyncio
async def test_knn_search_accepts_vector_query(store_engine):
    """query 也可直接传向量(供下游缓存复用)."""
    store, eng = store_engine
    async with eng.transaction() as conn:
        await store.add_text(conn, "alice works at CompanyX", entity_id="e1")

    vec = store.embedder.embed("alice works at CompanyX")
    hits = await store.knn_search(eng.conn, vec, k=1)
    assert len(hits) == 1
    assert hits[0].entity_id == "e1"


@pytest.mark.asyncio
async def test_knn_search_scope_filter(store_engine):
    """scope 过滤: include_global 只返回 global 实体的向量."""
    store, eng = store_engine
    async with eng.transaction() as conn:
        await store.add_text(conn, "global memory about alice", entity_id="e1")
        await store.add_text(conn, "project alpha kickoff notes", entity_id="e3")

    sf = ScopeFilter(include_global=True, include_project=None, include_all_projects=False)
    hits = await store.knn_search(eng.conn, "memory notes", k=10, scope_filter=sf)
    assert len(hits) == 1
    assert hits[0].entity_id == "e1"


@pytest.mark.asyncio
async def test_knn_search_project_scope(store_engine):
    """scope 过滤: 单项目只返回 project:proj_a 的向量."""
    store, eng = store_engine
    async with eng.transaction() as conn:
        await store.add_text(conn, "global memory about alice", entity_id="e1")
        await store.add_text(conn, "project alpha kickoff notes", entity_id="e3")

    sf = ScopeFilter(
        include_global=False, include_project="proj_a", include_all_projects=False
    )
    hits = await store.knn_search(eng.conn, "memory notes", k=10, scope_filter=sf)
    assert len(hits) == 1
    assert hits[0].entity_id == "e3"


@pytest.mark.asyncio
async def test_knn_search_empty_db(store_engine):
    """空库 KNN 返回空列表(不报错)."""
    store, eng = store_engine
    hits = await store.knn_search(eng.conn, "anything", k=5)
    assert hits == []


@pytest.mark.asyncio
async def test_knn_search_invalid_k(store_engine):
    store, eng = store_engine
    with pytest.raises(ValueError, match="k 必须为正整数"):
        await store.knn_search(eng.conn, "x", k=0)


# ---------- 删除 ----------

@pytest.mark.asyncio
async def test_delete_by_entity(store_engine):
    """按 entity_id 删除: memory_vectors + vector_links 都清掉."""
    store, eng = store_engine
    async with eng.transaction() as conn:
        await store.add_text(conn, "alice works at CompanyX", entity_id="e1")
        await store.add_text(conn, "bob reviewed PR-123", entity_id="e2")

    async with eng.transaction() as conn:
        deleted = await store.delete(conn, entity_id="e1")
    assert deleted == 1

    hits = await store.knn_search(eng.conn, "alice works at CompanyX", k=10)
    assert {h.entity_id for h in hits} == {"e2"}

    # memory_vectors 中向量本体也被删除
    cursor = await eng.conn.execute("SELECT COUNT(*) AS n FROM memory_vectors")
    row = await cursor.fetchone()
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_delete_by_vector_id(store_engine):
    """按 vector_id 删除."""
    store, eng = store_engine
    async with eng.transaction() as conn:
        vid = await store.add_text(conn, "alice works at CompanyX", entity_id="e1")

    async with eng.transaction() as conn:
        deleted = await store.delete(conn, vector_id=vid)
    assert deleted == 1

    hits = await store.knn_search(eng.conn, "alice works at CompanyX", k=10)
    assert hits == []


@pytest.mark.asyncio
async def test_delete_no_match_returns_zero(store_engine):
    """无匹配时返回 0,不报错."""
    store, eng = store_engine
    async with eng.transaction() as conn:
        deleted = await store.delete(conn, entity_id="nonexistent")
    assert deleted == 0


@pytest.mark.asyncio
async def test_delete_requires_exactly_one_criterion(store_engine):
    """删除条件必须恰好一个."""
    store, eng = store_engine
    with pytest.raises(ValueError, match="恰好给一个"):
        await store.delete(eng.conn)
    with pytest.raises(ValueError, match="恰好给一个"):
        await store.delete(eng.conn, vector_id=1, entity_id="e1")


# ---------- query 向量 LRU 缓存(M7) ----------

class _SpyEmbedder(HashEmbedder):
    """记录 embed 调用次数的 HashEmbedder."""

    def __init__(self):
        super().__init__()
        self.embed_calls = 0

    def embed(self, text: str) -> np.ndarray:
        self.embed_calls += 1
        return super().embed(text)


@pytest.mark.asyncio
async def test_knn_search_caches_query_embedding(store_engine):
    """相同 query 第二次 knn_search 命中 LRU,不再调用 embedder."""
    store, eng = store_engine
    spy = _SpyEmbedder()
    store.embedder = spy
    store._query_cache.clear()

    async with eng.transaction() as conn:
        await store.add_text(conn, "alice works at CompanyX", entity_id="e1")
    baseline = spy.embed_calls  # add_text 自身的编码不计入

    await store.knn_search(eng.conn, "alice", k=5)
    assert spy.embed_calls == baseline + 1
    hits = await store.knn_search(eng.conn, "alice", k=5)
    assert spy.embed_calls == baseline + 1  # 缓存命中
    assert len(hits) == 1

    await store.knn_search(eng.conn, "bob", k=5)
    assert spy.embed_calls == baseline + 2  # 不同 query 正常编码
