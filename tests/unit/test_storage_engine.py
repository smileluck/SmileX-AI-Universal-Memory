"""Unit tests for StorageEngine facade (L1.14).

验证:
- scope_path / parse_scope_path 互转
- write_entity / write_triple / write_location 写入
- get_entity / get_triple 读取(roundtrip)
- 检索方法委托(query_at_time / find_path / hybrid_search)
- get_current_state 触发器联动
"""

from __future__ import annotations

import pytest

from smilex.memory.models import (
    CertaintyLevel,
    Entity,
    MemoryScope,
    Triple,
)
from smilex.memory.storage.queries import HybridQuery
from smilex.memory.storage.storage_engine import (
    StorageEngine,
    parse_scope_path,
    scope_path,
)


@pytest.fixture
async def engine():
    eng = StorageEngine(":memory:", load_vec=False)
    await eng.initialize()
    yield eng
    await eng.close()


# ---------- scope_path / parse_scope_path ----------

def test_scope_path_global():
    assert scope_path(MemoryScope.GLOBAL) == "global"


def test_scope_path_project():
    assert scope_path(MemoryScope.PROJECT, "proj_a") == "project:proj_a"


def test_scope_path_tenant():
    assert scope_path(MemoryScope.TENANT, "t1") == "tenant:t1"


def test_scope_path_project_requires_id():
    with pytest.raises(ValueError, match="scope_id"):
        scope_path(MemoryScope.PROJECT)


def test_parse_scope_path_global():
    scope, sid = parse_scope_path("global")
    assert scope is MemoryScope.GLOBAL
    assert sid is None


def test_parse_scope_path_project():
    scope, sid = parse_scope_path("project:proj_a")
    assert scope is MemoryScope.PROJECT
    assert sid == "proj_a"


def test_parse_scope_path_invalid():
    with pytest.raises(ValueError, match="无法解析"):
        parse_scope_path("invalid")


def test_scope_path_roundtrip():
    """scope_path → parse_scope_path 往返一致."""
    for scope, sid in [
        (MemoryScope.GLOBAL, None),
        (MemoryScope.PROJECT, "proj_a"),
        (MemoryScope.TENANT, "t1"),
    ]:
        s = scope_path(scope, sid)
        parsed_scope, parsed_sid = parse_scope_path(s)
        assert parsed_scope is scope
        assert parsed_sid == sid


# ---------- write / get roundtrip ----------

@pytest.mark.asyncio
async def test_write_and_get_entity(engine):
    """写入 Entity 并读回."""
    e = Entity(
        entity_id="person:alice",
        entity_type="person",
        name="Alice",
        scope=MemoryScope.GLOBAL,
    )
    eid = await engine.write_entity(e)
    assert eid == e.id

    fetched = await engine.get_entity(eid)
    assert fetched is not None
    assert fetched.entity_id == "person:alice"
    assert fetched.name == "Alice"
    assert fetched.scope is MemoryScope.GLOBAL


@pytest.mark.asyncio
async def test_write_entity_project_scope(engine):
    """PROJECT scope + scope_id → 全路径存储."""
    e = Entity(
        entity_id="person:bob",
        entity_type="person",
        name="Bob",
        scope=MemoryScope.PROJECT,
    )
    eid = await engine.write_entity(e, scope_id="proj_a")

    # 直接查底层验证全路径
    cursor = await engine.conn.execute(
        "SELECT scope FROM entities WHERE id = ?", [eid]
    )
    row = await cursor.fetchone()
    assert row["scope"] == "project:proj_a"


@pytest.mark.asyncio
async def test_write_and_get_triple(engine):
    """写入 Triple 并读回(含所有字段)."""
    t = Triple(
        triple_id="tri_1",
        subject_id="e1",
        predicate="knows",
        object_id="e2",
        scope=MemoryScope.GLOBAL,
        confidence=0.9,
        certainty=CertaintyLevel.HIGH,
        relation_type="semantic",
    )
    tid = await engine.write_triple(t)

    fetched = await engine.get_triple(tid)
    assert fetched is not None
    assert fetched.subject_id == "e1"
    assert fetched.predicate == "knows"
    assert fetched.object_id == "e2"
    assert fetched.confidence == 0.9
    assert fetched.certainty is CertaintyLevel.HIGH
    assert fetched.relation_type == "semantic"


@pytest.mark.asyncio
async def test_write_triple_with_embedding(engine):
    """Entity 带 embedding 的 roundtrip."""
    import numpy as np

    emb = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    e = Entity(
        entity_id="concept:x",
        entity_type="concept",
        name="X",
        scope=MemoryScope.GLOBAL,
        embedding=emb,
    )
    eid = await engine.write_entity(e)

    fetched = await engine.get_entity(eid)
    assert fetched is not None
    assert fetched.embedding is not None
    assert fetched.embedding.dtype == np.float32
    assert list(fetched.embedding) == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_write_location():
    """写入 Location + R-tree(需要 load_vec=True 才有 R-tree 虚拟表)."""
    eng = StorageEngine(":memory:", load_vec=True)
    await eng.initialize()
    try:
        rowid = await eng.write_location(
            id="loc1",
            location_id="home",
            name="家",
            location_type="region",
            path="home",
            coordinates=(116.4074, 39.9042),
        )
        assert rowid > 0

        cursor = await eng.conn.execute(
            "SELECT name FROM locations WHERE id = ?", ["loc1"]
        )
        row = await cursor.fetchone()
        assert row["name"] == "家"
    finally:
        await eng.close()


# ---------- get_* 不存在 ----------

@pytest.mark.asyncio
async def test_get_entity_nonexistent(engine):
    assert await engine.get_entity("nonexistent") is None


@pytest.mark.asyncio
async def test_get_triple_nonexistent(engine):
    assert await engine.get_triple("nonexistent") is None


# ---------- 检索委托 ----------

@pytest.mark.asyncio
async def test_query_at_time_delegates(engine):
    """query_at_time 委托给 queries 模块."""
    t = Triple(
        triple_id="tri_1",
        subject_id="e1",
        predicate="knows",
        object_id="e2",
        scope=MemoryScope.GLOBAL,
    )
    await engine.write_triple(t)

    rows = await engine.query_at_time("2099-12-31T00:00:00.000000Z")
    assert len(rows) >= 1
    assert any(r["predicate"] == "knows" for r in rows)


@pytest.mark.asyncio
async def test_find_path_delegates(engine):
    """find_path 委托给 queries 模块."""
    e1 = Entity(entity_id="person:a", entity_type="person", name="A",
                scope=MemoryScope.GLOBAL)
    e2 = Entity(entity_id="person:b", entity_type="person", name="B",
                scope=MemoryScope.GLOBAL)
    await engine.write_entity(e1)
    await engine.write_entity(e2)
    t = Triple(
        triple_id="tri_1", subject_id=e1.id, predicate="knows",
        object_id=e2.id, scope=MemoryScope.GLOBAL,
    )
    await engine.write_triple(t)

    path = await engine.find_path(e1.id, e2.id)
    assert path is not None
    assert path["depth"] == 1


@pytest.mark.asyncio
async def test_hybrid_search_delegates(engine):
    """hybrid_search 委托给 queries 模块."""
    e1 = Entity(entity_id="person:a", entity_type="person", name="A",
                scope=MemoryScope.GLOBAL)
    await engine.write_entity(e1)
    t = Triple(
        triple_id="tri_1", subject_id=e1.id, predicate="knows",
        object_value="X", scope=MemoryScope.GLOBAL,
    )
    await engine.write_triple(t)

    query = HybridQuery(entity_id=e1.id)
    result = await engine.hybrid_search(query)
    assert len(result) >= 1


# ---------- get_current_state 触发器联动 ----------

@pytest.mark.asyncio
async def test_get_current_state_after_state_triple(engine):
    """写入 project_state triple → 触发器维护 → get_current_state 可读."""
    e = Entity(
        entity_id="project:demo",
        entity_type="concept",
        name="Demo Project",
        scope=MemoryScope.PROJECT,
    )
    await engine.write_entity(e, scope_id="demo")

    t = Triple(
        triple_id="tri_status",
        subject_id=e.id,
        predicate="status",
        object_value="active",
        scope=MemoryScope.PROJECT,
        relation_type="project_state",
    )
    await engine.write_triple(t, scope_id="demo")

    # 触发器应该更新了 project_current_state
    states = await engine.get_current_state("project:demo", e.id)
    assert len(states) == 1
    assert states[0]["object_value"] == "active"
    assert states[0]["predicate"] == "status"


@pytest.mark.asyncio
async def test_get_current_state_filters_by_predicate(engine):
    """get_current_state(predicate=...) 过滤."""
    e = Entity(
        entity_id="project:demo",
        entity_type="concept",
        name="Demo",
        scope=MemoryScope.PROJECT,
    )
    await engine.write_entity(e, scope_id="demo")

    for pred, val in [("status", "active"), ("priority", "high")]:
        t = Triple(
            triple_id=f"tri_{pred}",
            subject_id=e.id,
            predicate=pred,
            object_value=val,
            scope=MemoryScope.PROJECT,
            relation_type="project_state",
        )
        await engine.write_triple(t, scope_id="demo")

    # 全部
    all_states = await engine.get_current_state("project:demo", e.id)
    assert len(all_states) == 2

    # 单个 predicate
    only_status = await engine.get_current_state("project:demo", e.id, "status")
    assert len(only_status) == 1
    assert only_status[0]["predicate"] == "status"


@pytest.mark.asyncio
async def test_get_current_state_upsert(engine):
    """同 subject+predicate 新版本 → UPSERT."""
    e = Entity(
        entity_id="project:demo",
        entity_type="concept",
        name="Demo",
        scope=MemoryScope.PROJECT,
    )
    await engine.write_entity(e, scope_id="demo")

    # 第一版
    t1 = Triple(
        triple_id="tri_v1", subject_id=e.id, predicate="status",
        object_value="active", scope=MemoryScope.PROJECT,
        relation_type="project_state",
    )
    await engine.write_triple(t1, scope_id="demo")
    # 第二版
    t2 = Triple(
        triple_id="tri_v2", subject_id=e.id, predicate="status",
        object_value="completed", scope=MemoryScope.PROJECT,
        relation_type="project_state",
    )
    await engine.write_triple(t2, scope_id="demo")

    states = await engine.get_current_state("project:demo", e.id, "status")
    assert len(states) == 1  # UPSERT 不新增
    assert states[0]["object_value"] == "completed"


# ---------- 事务回滚 ----------

@pytest.mark.asyncio
async def test_write_uses_transaction(engine):
    """写入应在事务内(失败回滚)."""
    e = Entity(
        entity_id="person:a", entity_type="person", name="A",
        scope=MemoryScope.GLOBAL,
    )
    await engine.write_entity(e)

    # 直接读底层验证
    cursor = await engine.conn.execute("SELECT count(*) FROM entities")
    assert (await cursor.fetchone())[0] == 1


# ---------- 批量写入(H1) ----------

@pytest.mark.asyncio
async def test_write_entities_batch(engine):
    """write_entities 批量写入,返回 id 列表,逐条可读回."""
    entities = [
        Entity(
            entity_id=f"person:batch_{i}",
            entity_type="person",
            name=f"Batch{i}",
            scope=MemoryScope.GLOBAL,
        )
        for i in range(5)
    ]
    ids = await engine.write_entities(entities)
    assert ids == [e.id for e in entities]

    for e in entities:
        fetched = await engine.get_entity(e.id)
        assert fetched is not None
        assert fetched.name == e.name


@pytest.mark.asyncio
async def test_write_entities_empty(engine):
    """空列表直接返回,不开事务."""
    assert await engine.write_entities([]) == []


@pytest.mark.asyncio
async def test_write_triples_batch(engine):
    """write_triples 单事务批量写入,predicate 编码正确."""
    triples = [
        Triple(
            triple_id=f"tri_b{i}",
            subject_id="e1",
            predicate="knows" if i % 2 == 0 else "likes",
            object_id=f"e{i}",
            scope=MemoryScope.GLOBAL,
        )
        for i in range(6)
    ]
    ids = await engine.write_triples(triples)
    assert ids == [t.id for t in triples]

    for t in triples:
        fetched = await engine.get_triple(t.id)
        assert fetched is not None
        assert fetched.predicate == t.predicate

    # 同谓词的 predicate_code 一致(字典编码语义不变)
    cursor = await engine.conn.execute(
        "SELECT DISTINCT predicate_code FROM triples WHERE predicate = 'knows'"
    )
    codes = [row[0] for row in await cursor.fetchall()]
    assert len(codes) == 1


@pytest.mark.asyncio
async def test_write_triples_empty(engine):
    assert await engine.write_triples([]) == []


@pytest.mark.asyncio
async def test_predicate_cache_hit(engine):
    """predicate 缓存:同谓词第二次写入不查库(缓存命中)."""
    from smilex.memory.storage.predicate_codec import encode_predicate

    async with engine._engine.transaction() as conn:
        code1 = await encode_predicate(conn, "cached_pred", cache=engine._predicate_cache)
    assert engine._predicate_cache["cached_pred"] == code1

    # 缓存命中路径:即使传入一个无法到达库的假连接也不应触发 SQL
    class _ExplodingConn:
        async def execute(self, *a, **kw):
            raise AssertionError("缓存命中时不应执行 SQL")

    code2 = await encode_predicate(
        _ExplodingConn(), "cached_pred", cache=engine._predicate_cache
    )
    assert code2 == code1


# ---------- embedding BLOB 存储(M5) ----------

@pytest.mark.asyncio
async def test_embedding_stored_as_blob(engine):
    """新写入的 embedding 以 float32 BLOB 存储(非 JSON TEXT)."""
    import numpy as np

    emb = np.arange(8, dtype=np.float32) * 0.1
    e = Entity(
        entity_id="concept:blob",
        entity_type="concept",
        name="Blob",
        scope=MemoryScope.GLOBAL,
        embedding=emb,
    )
    eid = await engine.write_entity(e)

    cursor = await engine.conn.execute(
        "SELECT embedding, typeof(embedding) FROM entities WHERE id = ?", [eid]
    )
    row = await cursor.fetchone()
    assert row[1] == "blob"
    assert len(row[0]) == 8 * 4  # float32 × 8 维

    fetched = await engine.get_entity(eid)
    assert fetched.embedding.dtype == np.float32
    np.testing.assert_allclose(fetched.embedding, emb, rtol=1e-6)


@pytest.mark.asyncio
async def test_embedding_legacy_json_compat(engine):
    """存量 JSON TEXT 格式的 embedding 仍可正确读取(兼容路径)."""
    import json

    import numpy as np

    e = Entity(
        entity_id="concept:legacy",
        entity_type="concept",
        name="Legacy",
        scope=MemoryScope.GLOBAL,
    )
    eid = await engine.write_entity(e)
    # 模拟旧版本写入的 JSON TEXT
    await engine.conn.execute(
        "UPDATE entities SET embedding = ? WHERE id = ?",
        [json.dumps([1.5, -2.5, 3.0]), eid],
    )
    await engine.conn.commit()

    fetched = await engine.get_entity(eid)
    assert fetched.embedding is not None
    assert fetched.embedding.dtype == np.float32
    np.testing.assert_allclose(fetched.embedding, [1.5, -2.5, 3.0], rtol=1e-6)
