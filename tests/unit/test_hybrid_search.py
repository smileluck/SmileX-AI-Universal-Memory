"""Unit tests for hybrid search (L1.13).

验证:
- rrf_fusion: Reciprocal Rank Fusion 算法正确性
- hybrid_memory_search: 多策略融合(temporal/graph/causal)
- HybridQuery: 不同字段触发不同策略
- scope_filter 集成
- top_k 截断
"""

from __future__ import annotations

import pytest

from smilex.memory.storage.queries import (
    HybridQuery,
    hybrid_memory_search,
    project_scope,
    rrf_fusion,
)
from smilex.memory.storage.sqlite_engine import SQLiteEngine

# ---------- rrf_fusion 单元测试 ----------

def test_rrf_fusion_empty():
    """空输入应返回空."""
    assert rrf_fusion([]) == []


def test_rrf_fusion_single_ranking():
    """单排序:分数应该按 1/(k+rank) 降序."""
    result = rrf_fusion([["a", "b", "c"]], k=60)
    assert result[0] == ("a", 1.0 / 61)
    assert result[1] == ("b", 1.0 / 62)
    assert result[2] == ("c", 1.0 / 63)


def test_rrf_fusion_two_rankings():
    """两排序:出现在两边的 ID 应得更高分."""
    result = rrf_fusion([["a", "b", "c"], ["b", "a", "d"]], k=60)
    scores = dict(result)
    # b 在两边都靠前(rank 1 + rank 2 → 对称,a 也是 rank 1 + rank 2)
    # 所以 a 和 b 分数相同,并列第一
    top_score = result[0][1]
    assert scores["a"] == top_score or scores["b"] == top_score
    # a 和 b 都比 c/d 分数高(只在一边出现)
    assert scores["a"] > scores["c"]
    assert scores["b"] > scores["d"]


def test_rrf_fusion_custom_k():
    """k 越小,头部权重越高."""
    result_k60 = rrf_fusion([["a", "b", "c"]], k=60)
    result_k1 = rrf_fusion([["a", "b", "c"]], k=1)
    # k=1 时,a 的分数 = 1/2 = 0.5;k=60 时 = 1/61 ≈ 0.016
    assert dict(result_k1)["a"] > dict(result_k60)["a"]


def test_rrf_fusion_unique_ids_union():
    """多个排序的并集都应出现在结果中."""
    result = rrf_fusion([["a", "b"], ["c", "d"], ["e"]])
    ids = {doc_id for doc_id, _ in result}
    assert ids == {"a", "b", "c", "d", "e"}


def test_rrf_fusion_score_descending():
    """结果应按分数降序."""
    result = rrf_fusion([["a", "b", "c"], ["a", "d"]])
    scores = [s for _, s in result]
    assert scores == sorted(scores, reverse=True)


# ---------- hybrid_memory_search 集成测试 ----------

@pytest.fixture
async def engine_with_hybrid_data():
    """引擎 + 混合检索测试数据.

    数据设计:
    - alice 是核心实体(图谱中心)
    - t1 (alice knows bob) 时间靠前(2025-01)
    - t2 (alice knows carol) 时间靠后(2025-06)
    - t3 (alice reviewed PR) 因果链中点
    - t4 (alice merged) 因果链后续( predecessor=t3)
    """
    eng = SQLiteEngine(":memory:", load_vec=False)
    await eng.initialize()
    conn = eng.conn

    entities = [
        ("e1", "person:alice", "person", "Alice", "global"),
        ("e2", "person:bob", "person", "Bob", "global"),
        ("e3", "person:carol", "person", "Carol", "global"),
    ]
    for eid, ename, etype, name, scope in entities:
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
            "VALUES (?, ?, ?, ?, ?, '2025-01-01T00:00:00.000000Z')",
            (eid, ename, etype, name, scope),
        )

    triples = [
        # t1: alice → bob (Jan, 已失效 1 月底,用于测试时间过滤)
        ("t1", "tri_t1", "e1", "knows", "e2", None,
         "global", "2025-01-15T00:00:00.000000Z", "2025-01-31T23:59:59.000000Z",
         None, "semantic"),
        # t2: alice → carol (Jun, current)
        ("t2", "tri_t2", "e1", "knows", "e3", None,
         "global", "2025-06-01T00:00:00.000000Z", None, None, "semantic"),
        # t3: alice reviewed PR (Mar, causal middle)
        ("t3", "tri_t3", "e1", "reviewed", None, "PR-1",
         "global", "2025-03-01T00:00:00.000000Z", None, None, "semantic"),
        # t4: alice merged (Apr, causal follow-up of t3)
        ("t4", "tri_t4", "e1", "merged", None, "PR-1",
         "global", "2025-04-01T00:00:00.000000Z", None, "t3", "causal"),
    ]
    for tid, tri_id, sid, pred, oid, oval, scope, vf, vt, pred_id, rt in triples:
        await conn.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, "
            "object_id, object_value, scope, valid_from, valid_to, "
            "predecessor_id, relation_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, tri_id, sid, pred, oid, oval, scope, vf, vt, pred_id, rt),
        )
    await conn.commit()
    yield eng
    await eng.close()


@pytest.mark.asyncio
async def test_hybrid_no_strategies(engine_with_hybrid_data):
    """HybridQuery 全空 → 无策略触发,返回空."""
    conn = engine_with_hybrid_data.conn
    query = HybridQuery()
    result = await hybrid_memory_search(conn, query)
    assert result == []


@pytest.mark.asyncio
async def test_hybrid_temporal_only(engine_with_hybrid_data):
    """仅 time_range → 只触发 temporal 策略."""
    conn = engine_with_hybrid_data.conn
    query = HybridQuery(
        time_range=("2025-02-01T00:00:00.000000Z", "2025-12-31T00:00:00.000000Z"),
    )
    result = await hybrid_memory_search(conn, query, top_k=10)
    # 应该返回 t2, t3, t4(t1 时间太早不在范围)
    ids = {r["id"] for r in result}
    assert "t2" in ids
    assert "t3" in ids
    assert "t4" in ids
    assert "t1" not in ids
    # 所有结果都只命中 temporal 策略
    assert all(r["strategies_hit"] == ["temporal"] for r in result)


@pytest.mark.asyncio
async def test_hybrid_graph_only(engine_with_hybrid_data):
    """仅 entity_id → 只触发 graph 策略."""
    conn = engine_with_hybrid_data.conn
    query = HybridQuery(entity_id="e1")  # Alice
    result = await hybrid_memory_search(conn, query, top_k=10)
    # alice 涉及的当前有效边: t2, t3, t4
    # (t1 已闭合 valid_to — § 主动优化二期图策略只取当前边)
    ids = {r["id"] for r in result}
    assert ids == {"t2", "t3", "t4"}
    assert all(r["strategies_hit"] == ["graph"] for r in result)


@pytest.mark.asyncio
async def test_hybrid_causal_only(engine_with_hybrid_data):
    """仅 causal_triple_id → 只触发 causal 策略."""
    conn = engine_with_hybrid_data.conn
    query = HybridQuery(causal_triple_id="t4")
    result = await hybrid_memory_search(conn, query, top_k=10)
    # t4 向后追溯: t4 → t3
    ids = {r["id"] for r in result}
    assert "t4" in ids
    assert "t3" in ids


@pytest.mark.asyncio
async def test_hybrid_multi_strategy_fusion(engine_with_hybrid_data):
    """多策略融合:同时给 time_range + entity_id."""
    conn = engine_with_hybrid_data.conn
    query = HybridQuery(
        entity_id="e1",  # graph 策略: t2, t3, t4(t1 已闭合被过滤)
        time_range=("2025-02-01T00:00:00.000000Z", "2025-12-31T00:00:00.000000Z"),
        # temporal 策略: t2, t3, t4
    )
    result = await hybrid_memory_search(conn, query, top_k=10)

    strategies_map = {r["id"]: set(r["strategies_hit"]) for r in result}

    # t2/t3/t4 命中 temporal + graph
    assert strategies_map["t2"] == {"graph", "temporal"}
    assert strategies_map["t3"] == {"graph", "temporal"}
    # t4 时间窗内? valid_from 2025-04 在窗内 → 双策略
    assert strategies_map["t4"] == {"graph", "temporal"}

    # 多策略命中的分数一致性: t2/t3 均高于仅 graph 的行(以集合完整性代替单行对照)
    assert set(strategies_map) == {"t2", "t3", "t4"}


@pytest.mark.asyncio
async def test_hybrid_top_k_limit(engine_with_hybrid_data):
    """top_k 限制返回数."""
    conn = engine_with_hybrid_data.conn
    query = HybridQuery(entity_id="e1")  # 4 个 triples
    result = await hybrid_memory_search(conn, query, top_k=2)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_hybrid_with_scope_filter(engine_with_hybrid_data):
    """scope 过滤 — 只匹配 global."""
    conn = engine_with_hybrid_data.conn
    # 先插入 project scope 的 triple
    await conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_id, "
        "scope, valid_from, relation_type) "
        "VALUES ('t_proj', 'tri_proj', 'e1', 'knows', 'e2', "
        "'project:proj_a', '2025-06-01T00:00:00.000000Z', 'semantic')"
    )
    await conn.commit()

    query = HybridQuery(entity_id="e1")
    sf = project_scope("proj_a", include_global=False)
    result = await hybrid_memory_search(conn, query, scope_filter=sf)
    # 只应该返回 project scope 的 triple
    ids = {r["id"] for r in result}
    assert "t_proj" in ids
    # global 的不应该在
    assert "t1" not in ids
    assert "t2" not in ids


@pytest.mark.asyncio
async def test_hybrid_strategies_hit_recorded(engine_with_hybrid_data):
    """strategies_hit 字段应正确记录命中的策略."""
    conn = engine_with_hybrid_data.conn
    query = HybridQuery(
        entity_id="e1",
        time_range=("2025-05-01T00:00:00.000000Z", "2025-12-31T00:00:00.000000Z"),
        # temporal 命中 t2;graph 命中 t2/t3/t4(t1 已闭合被过滤)
    )
    result = await hybrid_memory_search(conn, query)
    strategy_map = {r["id"]: set(r["strategies_hit"]) for r in result}
    # t2 命中两个策略
    assert "temporal" in strategy_map["t2"]
    assert "graph" in strategy_map["t2"]
    # t2/t3/t4 全部双命中(temporal 为重叠语义: 当前行 valid_to IS NULL,
    # valid_from ≤ 窗口末即入窗;原 t1 单 graph 对照行已被 current-only 过滤)
    for tid in ("t2", "t3", "t4"):
        assert strategy_map[tid] == {"graph", "temporal"}


@pytest.mark.asyncio
async def test_hybrid_score_ordering(engine_with_hybrid_data):
    """结果应按 score 降序."""
    conn = engine_with_hybrid_data.conn
    query = HybridQuery(
        entity_id="e1",
        time_range=("2025-02-01T00:00:00.000000Z", "2025-12-31T00:00:00.000000Z"),
    )
    result = await hybrid_memory_search(conn, query)
    scores = [r["score"] for r in result]
    assert scores == sorted(scores, reverse=True)


# ---------- 空 DB 行为 ----------

@pytest.mark.asyncio
async def test_hybrid_on_empty_db():
    eng = SQLiteEngine(":memory:", load_vec=False)
    await eng.initialize()
    conn = eng.conn

    query = HybridQuery(
        entity_id="any",
        time_range=("2025-01-01T00:00:00.000000Z", "2025-12-31T00:00:00.000000Z"),
    )
    result = await hybrid_memory_search(conn, query)
    assert result == []
    await eng.close()
