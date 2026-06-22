"""Unit tests for graph queries (L1.10).

测试图结构(典型社交网络):
    alice ──knows──▶ bob ──knows──▶ carol
                       │                 │
                       └──knows──▶ dave ◀┘

另外:
    alice ──works_at──▶ company_x
    dave  ──knows──▶ alice  (形成环 alice→bob→dave→alice)
"""

from __future__ import annotations

import pytest

from smilex.memory.models import ScopeFilter
from smilex.memory.storage.queries import find_n_degree_relations, find_path
from smilex.memory.storage.sqlite_engine import SQLiteEngine


@pytest.fixture
async def engine_with_graph():
    """引擎 + 预置社交图谱数据."""
    eng = SQLiteEngine(":memory:", load_vec=False)
    await eng.initialize()
    conn = eng.conn

    # 5 个实体
    entities = [
        ("e1", "person:alice", "person", "Alice", "global"),
        ("e2", "person:bob", "person", "Bob", "global"),
        ("e3", "person:carol", "person", "Carol", "global"),
        ("e4", "person:dave", "person", "Dave", "global"),
        # §5.2 entity_type: person/location/object/concept/event
        ("e5", "organization:x", "concept", "Company X", "global"),
    ]
    for eid, ename, etype, name, scope in entities:
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (eid, ename, etype, name, scope, "2025-01-01T00:00:00.000000Z"),
        )

    # 5 条 triples(有向边)
    # alice ──knows──▶ bob
    # bob ──knows──▶ carol
    # bob ──knows──▶ dave
    # carol ──knows──▶ dave
    # alice ──works_at──▶ company_x
    # dave ──knows──▶ alice (环)
    triples = [
        ("t1", "e1", "knows", "e2"),
        ("t2", "e2", "knows", "e3"),
        ("t3", "e2", "knows", "e4"),
        ("t4", "e3", "knows", "e4"),
        ("t5", "e1", "works_at", "e5"),
        ("t6", "e4", "knows", "e1"),  # 形成环
    ]
    for tid, sid, pred, oid in triples:
        await conn.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, object_id, "
            "scope, valid_from, relation_type) "
            "VALUES (?, ?, ?, ?, ?, 'global', '2025-01-01T00:00:00.000000Z', 'semantic')",
            (tid, f"tri_{tid}", sid, pred, oid),
        )
    await conn.commit()
    yield eng
    await eng.close()


# ---------- find_path ----------

@pytest.mark.asyncio
async def test_find_path_direct_edge(engine_with_graph):
    """直接相邻: alice → bob(1 跳)."""
    conn = engine_with_graph.conn
    result = await find_path(conn, "e1", "e2")
    assert result is not None
    assert result["depth"] == 1
    assert result["nodes"] == ["e1", "e2"]
    assert len(result["edges"]) == 1


@pytest.mark.asyncio
async def test_find_path_two_hops(engine_with_graph):
    """2 跳: alice → bob → carol."""
    conn = engine_with_graph.conn
    result = await find_path(conn, "e1", "e3")
    assert result is not None
    assert result["depth"] == 2
    assert result["nodes"][0] == "e1"
    assert result["nodes"][-1] == "e3"


@pytest.mark.asyncio
async def test_find_path_same_node(engine_with_graph):
    """src == dst 应返回空路径(depth 0)."""
    conn = engine_with_graph.conn
    result = await find_path(conn, "e1", "e1")
    assert result == {"nodes": ["e1"], "edges": [], "depth": 0}


@pytest.mark.asyncio
async def test_find_path_no_path(engine_with_graph):
    """无路径应返回 None(如 company_x 没有出边)."""
    conn = engine_with_graph.conn
    result = await find_path(conn, "e5", "e1")
    assert result is None


@pytest.mark.asyncio
async def test_find_path_handles_cycle(engine_with_graph):
    """存在环时不应死循环(alice→bob→dave→alice)."""
    conn = engine_with_graph.conn
    # alice → dave: 最短是 alice → bob → dave (2 跳)
    result = await find_path(conn, "e1", "e4")
    assert result is not None
    assert result["depth"] == 2
    assert result["nodes"] == ["e1", "e2", "e4"]


@pytest.mark.asyncio
async def test_find_path_respects_max_depth(engine_with_graph):
    """max_depth 限制递归深度."""
    conn = engine_with_graph.conn
    # alice → carol 需要 2 跳,max_depth=1 找不到
    result = await find_path(conn, "e1", "e3", max_depth=1)
    assert result is None

    # max_depth=2 能找到
    result = await find_path(conn, "e1", "e3", max_depth=2)
    assert result is not None
    assert result["depth"] == 2


@pytest.mark.asyncio
async def test_find_path_with_scope_filter(engine_with_graph):
    """scope 过滤限制遍历的 triples."""
    conn = engine_with_graph.conn
    # 添加一个 project scope 的边
    await conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_id, "
        "scope, valid_from, relation_type) "
        "VALUES ('t_proj', 'tri_proj', 'e1', 'knows', 'e3', "
        "'project:proj_a', '2025-01-01T00:00:00.000000Z', 'semantic')"
    )
    await conn.commit()

    # 只看 project:proj_a — alice → carol (1 跳, project scope)
    sf = ScopeFilter(include_global=False, include_project="proj_a")
    result = await find_path(conn, "e1", "e3", scope_filter=sf)
    assert result is not None
    assert result["depth"] == 1
    assert result["edges"] == ["t_proj"]


@pytest.mark.asyncio
async def test_find_path_invalid_scope_finds_nothing(engine_with_graph):
    """scope 不匹配应找不到路径."""
    conn = engine_with_graph.conn
    sf = ScopeFilter(include_global=False, include_project="nonexistent")
    result = await find_path(conn, "e1", "e2", scope_filter=sf)
    assert result is None


@pytest.mark.asyncio
async def test_find_path_nonexistent_entity(engine_with_graph):
    """不存在的实体应返回 None."""
    conn = engine_with_graph.conn
    assert await find_path(conn, "nonexistent", "e1") is None
    assert await find_path(conn, "e1", "nonexistent") is None


# ---------- find_n_degree_relations ----------

@pytest.mark.asyncio
async def test_find_n_degree_1_hop(engine_with_graph):
    """1 度关系 — alice 直接认识的人."""
    conn = engine_with_graph.conn
    relations = await find_n_degree_relations(conn, "e1", max_depth=1)
    # alice ──knows──▶ bob; alice ──works_at──▶ company_x
    related = {r["entity_id"] for r in relations}
    assert "e2" in related  # Bob
    assert "e5" in related  # Company X
    assert all(r["depth"] == 1 for r in relations)
    # alice 自己不应在结果中
    assert "e1" not in related


@pytest.mark.asyncio
async def test_find_n_degree_2_hops(engine_with_graph):
    """2 度关系 — alice 的朋友的朋友."""
    conn = engine_with_graph.conn
    relations = await find_n_degree_relations(conn, "e1", max_depth=2)
    related = {r["entity_id"]: r["depth"] for r in relations}

    # 1 度: Bob(e2), Company X(e5)
    assert related.get("e2") == 1
    assert related.get("e5") == 1
    # 2 度: Carol(e3, via Bob), Dave(e4, via Bob)
    assert related.get("e3") == 2
    assert related.get("e4") == 2


@pytest.mark.asyncio
async def test_find_n_degree_handles_cycle(engine_with_graph):
    """环存在时不重复访问同一节点."""
    conn = engine_with_graph.conn
    # 从 dave 出发: dave → alice (1 跳)
    # alice → bob → carol → dave (2,3,4 跳)
    # 但 dave 是起点,不应出现
    relations = await find_n_degree_relations(conn, "e4", max_depth=3)
    related = {r["entity_id"]: r["depth"] for r in relations}
    # dave → alice (1)
    assert related.get("e1") == 1
    # dave → alice → bob (2)
    assert related.get("e2") == 2
    # dave → alice → bob → carol (3)
    assert related.get("e3") == 3
    # dave 自己不应出现
    assert "e4" not in related


@pytest.mark.asyncio
async def test_find_n_degree_no_outgoing(engine_with_graph):
    """无出边的节点(如 e5 Company X)应返回空列表."""
    conn = engine_with_graph.conn
    relations = await find_n_degree_relations(conn, "e5", max_depth=3)
    assert relations == []


@pytest.mark.asyncio
async def test_find_n_degree_nonexistent_entity(engine_with_graph):
    """不存在的实体应返回空列表."""
    conn = engine_with_graph.conn
    relations = await find_n_degree_relations(conn, "nonexistent", max_depth=3)
    assert relations == []


@pytest.mark.asyncio
async def test_find_n_degree_with_scope_filter(engine_with_graph):
    """scope 过滤限制遍历的 triples."""
    conn = engine_with_graph.conn
    # 添加 project scope 边
    await conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_id, "
        "scope, valid_from, relation_type) "
        "VALUES ('t_proj2', 'tri_proj2', 'e1', 'knows', 'e4', "
        "'project:proj_a', '2025-01-01T00:00:00.000000Z', 'semantic')"
    )
    await conn.commit()

    sf = ScopeFilter(include_global=False, include_project="proj_a")
    relations = await find_n_degree_relations(conn, "e1", max_depth=2, scope_filter=sf)
    # 只有 project scope 的边: alice → dave (1 跳)
    related = {r["entity_id"]: r["depth"] for r in relations}
    assert related.get("e4") == 1
    # 不应该有 global scope 的 Bob/Company X
    assert "e2" not in related
    assert "e5" not in related


@pytest.mark.asyncio
async def test_find_n_degree_zero_depth(engine_with_graph):
    """max_depth=0 应返回空列表(起点本身不在结果中)."""
    conn = engine_with_graph.conn
    relations = await find_n_degree_relations(conn, "e1", max_depth=0)
    assert relations == []


# ---------- 空 DB 行为 ----------

@pytest.mark.asyncio
async def test_graph_queries_on_empty_db():
    """空数据库上查询应返回 None / 空."""
    eng = SQLiteEngine(":memory:", load_vec=False)
    await eng.initialize()
    conn = eng.conn

    assert await find_path(conn, "a", "b") is None
    assert await find_n_degree_relations(conn, "a", max_depth=2) == []

    await eng.close()
