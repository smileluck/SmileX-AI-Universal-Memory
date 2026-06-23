"""Unit tests for causal queries (L1.11).

测试因果链示意(predecessor_id 链):

    t1 (root cause)
     └─ t2 (predecessor: t1)
         └─ t3 (predecessor: t2)
             └─ t4 (predecessor: t3)

    另一条链:
    t5 (root)
     ├─ t6 (predecessor: t5)
     └─ t7 (predecessor: t5)

    孤立的 t8(无前驱,无后继)
"""

from __future__ import annotations

import pytest

from smilex.memory.models import ScopeFilter
from smilex.memory.storage.queries import trace_causal_chain
from smilex.memory.storage.sqlite_engine import SQLiteEngine


@pytest.fixture
async def engine_with_causal():
    """引擎 + 预置因果链数据."""
    eng = SQLiteEngine(":memory:", load_vec=False)
    await eng.initialize()
    conn = eng.conn

    # 准备实体
    for i in range(1, 10):
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
            "VALUES (?, ?, 'event', ?, 'global', '2025-01-01T00:00:00.000000Z')",
            (f"e{i}", f"event:e{i}", f"Event {i}"),
        )

    # t1 → t2 → t3 → t4 (线性链)
    # t5 ← t6, t7 (分支)
    # t8 孤立
    triples = [
        # id, triple_id, subject, pred, object, predecessor, scope
        ("t1", "tri_t1", "e1", "caused", None, "Event 1 happened", None, "global"),
        ("t2", "tri_t2", "e2", "caused", None, "Event 2 happened", "t1", "global"),
        ("t3", "tri_t3", "e3", "caused", None, "Event 3 happened", "t2", "global"),
        ("t4", "tri_t4", "e4", "caused", None, "Event 4 happened", "t3", "global"),
        ("t5", "tri_t5", "e5", "caused", None, "Root 5", None, "global"),
        ("t6", "tri_t6", "e6", "caused", None, "Branch 6", "t5", "global"),
        ("t7", "tri_t7", "e7", "caused", None, "Branch 7", "t5", "global"),
        ("t8", "tri_t8", "e8", "isolated", None, "Lone event", None, "global"),
        # 项目 scope 的边
        ("t_proj1", "tri_tp1", "e9", "caused", None, "Project event", None, "project:proj_a"),
        ("t_proj2", "tri_tp2", "e9", "caused_by", None, "Extends project",
         "t_proj1", "project:proj_a"),
    ]
    for tid, tri_id, sid, pred, oid, oval, pred_id, scope in triples:
        await conn.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, "
            "object_id, object_value, scope, valid_from, predecessor_id, relation_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '2025-01-01T00:00:00.000000Z', ?, 'causal')",
            (tid, tri_id, sid, pred, oid, oval, scope, pred_id),
        )
    await conn.commit()
    yield eng
    await eng.close()


# ---------- direction=backward ----------

@pytest.mark.asyncio
async def test_backward_from_leaf(engine_with_causal):
    """从叶子 t4 向后追溯到根 t1."""
    conn = engine_with_causal.conn
    result = await trace_causal_chain(conn, "t4", direction="backward")
    assert result["start"] == "t4"
    chain = result["backward"]
    # t4 (depth 0) → t3 (1) → t2 (2) → t1 (3)
    ids = [c["id"] for c in chain]
    depths = [c["depth"] for c in chain]
    assert ids == ["t4", "t3", "t2", "t1"]
    assert depths == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_backward_from_root(engine_with_causal):
    """从根 t1 向后追溯应只有自己."""
    conn = engine_with_causal.conn
    result = await trace_causal_chain(conn, "t1", direction="backward")
    assert len(result["backward"]) == 1
    assert result["backward"][0]["id"] == "t1"
    assert result["backward"][0]["depth"] == 0


@pytest.mark.asyncio
async def test_backward_from_middle(engine_with_causal):
    """从中间 t2 向后追溯应得到 t2 → t1."""
    conn = engine_with_causal.conn
    result = await trace_causal_chain(conn, "t2", direction="backward")
    ids = [c["id"] for c in result["backward"]]
    assert ids == ["t2", "t1"]


@pytest.mark.asyncio
async def test_backward_isolated_triple(engine_with_causal):
    """孤立 t8 的 backward 应只有自己."""
    conn = engine_with_causal.conn
    result = await trace_causal_chain(conn, "t8", direction="backward")
    assert len(result["backward"]) == 1
    assert result["backward"][0]["id"] == "t8"


# ---------- direction=forward ----------

@pytest.mark.asyncio
async def test_forward_from_root_branching(engine_with_causal):
    """从根 t5 向前应找到所有后继 t6, t7(分支)."""
    conn = engine_with_causal.conn
    result = await trace_causal_chain(conn, "t5", direction="forward")
    forward = result["forward"]
    ids = {c["id"] for c in forward}
    assert "t6" in ids
    assert "t7" in ids
    # t5 自己不应在 forward 中
    assert "t5" not in ids
    # 两个分支都应该是 depth=1
    depths = {c["id"]: c["depth"] for c in forward}
    assert depths["t6"] == 1
    assert depths["t7"] == 1


@pytest.mark.asyncio
async def test_forward_from_root_linear(engine_with_causal):
    """从根 t1 向前应找到 t2, t3, t4(线性)."""
    conn = engine_with_causal.conn
    result = await trace_causal_chain(conn, "t1", direction="forward")
    forward = result["forward"]
    ids_to_depth = {c["id"]: c["depth"] for c in forward}
    assert ids_to_depth == {"t2": 1, "t3": 2, "t4": 3}


@pytest.mark.asyncio
async def test_forward_from_leaf(engine_with_causal):
    """叶子 t4 没有后继,forward 应为空."""
    conn = engine_with_causal.conn
    result = await trace_causal_chain(conn, "t4", direction="forward")
    assert result["forward"] == []


@pytest.mark.asyncio
async def test_forward_from_middle(engine_with_causal):
    """从中间 t2 向前应找到 t3, t4."""
    conn = engine_with_causal.conn
    result = await trace_causal_chain(conn, "t2", direction="forward")
    ids_to_depth = {c["id"]: c["depth"] for c in result["forward"]}
    assert ids_to_depth == {"t3": 1, "t4": 2}


# ---------- direction=both ----------

@pytest.mark.asyncio
async def test_both_from_middle(engine_with_causal):
    """双向追溯: t2 既有前驱也有后继."""
    conn = engine_with_causal.conn
    result = await trace_causal_chain(conn, "t2", direction="both")
    backward_ids = [c["id"] for c in result["backward"]]
    forward_ids = {c["id"] for c in result["forward"]}
    assert backward_ids == ["t2", "t1"]
    assert forward_ids == {"t3", "t4"}


# ---------- max_depth ----------

@pytest.mark.asyncio
async def test_backward_max_depth(engine_with_causal):
    """max_depth 限制追溯深度."""
    conn = engine_with_causal.conn
    # t4 → t1 是 3 跳,max_depth=2 只能到 t2
    result = await trace_causal_chain(conn, "t4", direction="backward", max_depth=2)
    ids = [c["id"] for c in result["backward"]]
    assert ids == ["t4", "t3", "t2"]  # depth 0, 1, 2
    assert "t1" not in ids


# ---------- scope_filter ----------

@pytest.mark.asyncio
async def test_backward_with_scope_filter(engine_with_causal):
    """scope 过滤 — 只追溯匹配 scope 的 triples."""
    conn = engine_with_causal.conn
    # 从 t_proj2 向后追溯,project scope
    sf = ScopeFilter(include_global=False, include_project="proj_a")
    result = await trace_causal_chain(
        conn, "t_proj2", direction="backward", scope_filter=sf
    )
    ids = [c["id"] for c in result["backward"]]
    assert "t_proj2" in ids
    assert "t_proj1" in ids


@pytest.mark.asyncio
async def test_backward_scope_blocks_chain(engine_with_causal):
    """scope 过滤可能截断链(global scope 看不到 project 链)."""
    conn = engine_with_causal.conn
    sf = ScopeFilter(include_global=True, include_project=None)
    # 从 t_proj2 向后追溯,只看 global scope → 找不到 t_proj1
    result = await trace_causal_chain(
        conn, "t_proj2", direction="backward", scope_filter=sf
    )
    # 只看到自己(t_proj2 是 project scope,scope_clause 是 global,
    # 起点本身因为 scope 不匹配,可能根本查不到)
    # 这里观察行为:起点 scope 不匹配则 backward 为空
    assert len(result["backward"]) == 0


# ---------- 错误情况 ----------

@pytest.mark.asyncio
async def test_nonexistent_triple(engine_with_causal):
    """不存在的 triple 应返回空 backward/forward."""
    conn = engine_with_causal.conn
    result = await trace_causal_chain(conn, "nonexistent", direction="both")
    assert result["backward"] == []
    assert result["forward"] == []


@pytest.mark.asyncio
async def test_invalid_direction(engine_with_causal):
    """非法 direction 应 raise."""
    conn = engine_with_causal.conn
    with pytest.raises(ValueError, match="backward/forward/both"):
        await trace_causal_chain(conn, "t1", direction="sideways")


# ---------- 空 DB ----------

@pytest.mark.asyncio
async def test_causal_on_empty_db():
    eng = SQLiteEngine(":memory:", load_vec=False)
    await eng.initialize()
    conn = eng.conn
    result = await trace_causal_chain(conn, "any", direction="both")
    assert result["backward"] == []
    assert result["forward"] == []
    await eng.close()
