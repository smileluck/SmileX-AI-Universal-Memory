"""Unit tests for temporal queries (L1.8).

验证:
- query_at_time: 时间点查询(含未失效记录 NULL valid_to)
- query_in_range: 时间范围重叠查询
- build_entity_timeline: 实体时间线(UNION ALL subject + object)
- ScopeFilter 集成(global/project/tenant/all_projects)
- 表选择(triples vs entities)
"""

from __future__ import annotations

import pytest

from smilex.memory.models import ScopeFilter
from smilex.memory.storage.queries import (
    build_entity_timeline,
    query_at_time,
    query_in_range,
)
from smilex.memory.storage.sqlite_engine import SQLiteEngine

# ---------- Fixtures ----------

@pytest.fixture
async def engine_with_data():
    """引擎 + 预置测试数据: 3 个实体 + 5 条 triples(含时序变化)."""
    eng = SQLiteEngine(":memory:", load_vec=False)
    await eng.initialize()
    conn = eng.conn

    # 3 个实体
    entities = [
        # entity_id, entity_type, name, scope
        ("e1", "person:alice", "person", "Alice", "global"),
        ("e2", "person:bob", "person", "Bob", "global"),
        ("e3", "person:carol", "person", "Carol", "project:proj_a"),
    ]
    for eid, ename, etype, name, scope in entities:
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (eid, ename, etype, name, scope, "2025-01-01T00:00:00.000000Z"),
        )

    # 5 条 triples,体现时序变化
    triples = [
        # Alice knows Bob (always valid)
        ("t1", "alice_knows_bob", "e1", "knows", "e2", None,
         "global", "2025-01-01T00:00:00.000000Z", None, "semantic"),

        # Alice works at X (2025-Q1)
        ("t2", "alice_works_q1", "e1", "works_at", None, "CompanyX",
         "global", "2025-01-01T00:00:00.000000Z",
         "2025-03-31T23:59:59.000000Z", "semantic"),

        # Alice works at Y (2025-Q2 onwards)
        ("t3", "alice_works_q2", "e1", "works_at", None, "CompanyY",
         "global", "2025-04-01T00:00:00.000000Z", None, "semantic"),

        # Bob reviewed PR (project scope, specific date)
        ("t4", "bob_pr_review", "e2", "reviewed", None, "PR-123",
         "project:proj_a", "2025-06-15T10:00:00.000000Z", None, "semantic"),

        # Carol status active (project_state trigger would fire)
        ("t5", "carol_status", "e3", "status", None, "active",
         "project:proj_a", "2025-06-01T00:00:00.000000Z", None, "project_state"),
    ]
    for tid, tri_id, sid, pred, oid, oval, scope, vf, vt, rt in triples:
        await conn.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, "
            "object_id, object_value, scope, valid_from, valid_to, relation_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, tri_id, sid, pred, oid, oval, scope, vf, vt, rt),
        )

    await conn.commit()
    yield eng
    await eng.close()


# ---------- query_at_time ----------

@pytest.mark.asyncio
async def test_query_at_time_returns_active_records(engine_with_data):
    """时间点查询: 找出在时间 t 有效的记录."""
    conn = engine_with_data.conn
    # 时间点 2025-02-01: Alice knows Bob + Alice works at X
    rows = await query_at_time(conn, "2025-02-01T00:00:00.000000Z")
    predicates = {r["predicate"] for r in rows}
    assert "knows" in predicates
    assert "works_at" in predicates
    # 但 works_at 应该只有 CompanyX,不是 CompanyY
    works = [r for r in rows if r["predicate"] == "works_at"]
    assert len(works) == 1
    assert works[0]["object_value"] == "CompanyX"


@pytest.mark.asyncio
async def test_query_at_time_after_valid_to(engine_with_data):
    """valid_to 已失效的记录不应返回."""
    conn = engine_with_data.conn
    # 2025-06-01 之后,Alice works_at X 已失效
    rows = await query_at_time(conn, "2025-06-01T00:00:00.000000Z")
    works = [r for r in rows if r["predicate"] == "works_at"]
    assert len(works) == 1
    assert works[0]["object_value"] == "CompanyY"


@pytest.mark.asyncio
async def test_query_at_time_null_valid_to_treated_as_current(engine_with_data):
    """NULL valid_to 应视为"当前有效"."""
    conn = engine_with_data.conn
    # 遥远的未来时间点
    rows = await query_at_time(conn, "2099-12-31T00:00:00.000000Z")
    predicates = {r["predicate"] for r in rows}
    # NULL valid_to 的应该都在(knows + works_at CompanyY + reviewed + status)
    assert "knows" in predicates
    assert "works_at" in predicates


@pytest.mark.asyncio
async def test_query_at_time_with_scope_filter(engine_with_data):
    """时间点查询 + scope 过滤."""
    conn = engine_with_data.conn

    # 仅 global scope
    sf = ScopeFilter(include_global=True, include_project=None, include_all_projects=False)
    rows = await query_at_time(conn, "2025-06-15T00:00:00.000000Z", scope_filter=sf)
    # 应该只有 global 的 triples:t1, t3
    scopes = {r["scope"] for r in rows}
    assert all(s == "global" for s in scopes)


@pytest.mark.asyncio
async def test_query_at_time_project_scope(engine_with_data):
    """项目 scope 过滤."""
    conn = engine_with_data.conn

    sf = ScopeFilter(
        include_global=False,
        include_project="proj_a",
        include_all_projects=False,
    )
    rows = await query_at_time(conn, "2025-06-15T00:00:00.000000Z", scope_filter=sf)
    # 应该返回 project:proj_a 的 triples:t4, t5
    assert len(rows) >= 1
    assert all(r["scope"] == "project:proj_a" for r in rows)


@pytest.mark.asyncio
async def test_query_at_time_include_all_projects(engine_with_data):
    """include_all_projects 用 LIKE 'project:%'."""
    conn = engine_with_data.conn

    sf = ScopeFilter(
        include_global=False,
        include_project=None,
        include_all_projects=True,
    )
    rows = await query_at_time(conn, "2025-06-15T00:00:00.000000Z", scope_filter=sf)
    assert all(r["scope"].startswith("project:") for r in rows)


@pytest.mark.asyncio
async def test_query_at_time_entities_table(engine_with_data):
    """对 entities 表做时间点查询."""
    conn = engine_with_data.conn
    rows = await query_at_time(conn, "2025-06-01T00:00:00.000000Z", table="entities")
    # 3 个实体都应该有效(valid_to 都是 NULL)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_query_at_time_invalid_table(engine_with_data):
    """不支持的表应 raise."""
    conn = engine_with_data.conn
    with pytest.raises(ValueError, match="仅支持"):
        await query_at_time(conn, "2025-06-01T00:00:00.000000Z", table="checkpoints")


# ---------- query_in_range ----------

@pytest.mark.asyncio
async def test_query_in_range_overlap(engine_with_data):
    """时间范围重叠查询."""
    conn = engine_with_data.conn
    # 范围 2025-03-01 ~ 2025-05-01:
    # - t1 knows: 全程重叠 ✓
    # - t2 works_at X (2025-Q1): 与 03-01 边界相切 ✓
    # - t3 works_at Y (2025-04-01+): 重叠 ✓
    # - t4, t5: 在范围外 ✗
    rows = await query_in_range(
        conn,
        "2025-03-01T00:00:00.000000Z",
        "2025-05-01T00:00:00.000000Z",
    )
    predicates = {r["predicate"] for r in rows}
    assert "knows" in predicates
    assert "works_at" in predicates


@pytest.mark.asyncio
async def test_query_in_range_no_overlap(engine_with_data):
    """完全在范围外的记录不应返回."""
    conn = engine_with_data.conn
    # 范围 2024-01 ~ 2024-06(早于所有数据)
    rows = await query_in_range(
        conn,
        "2024-01-01T00:00:00.000000Z",
        "2024-06-30T00:00:00.000000Z",
    )
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_query_in_range_null_valid_to_overlaps(engine_with_data):
    """NULL valid_to 应视为无穷大,任何未来范围都重叠."""
    conn = engine_with_data.conn
    # 远期范围
    rows = await query_in_range(
        conn,
        "2099-01-01T00:00:00.000000Z",
        "2099-12-31T00:00:00.000000Z",
    )
    # t1 knows (NULL valid_to), t3 works_at Y (NULL valid_to),
    # t4 reviewed (NULL), t5 status (NULL) — 都应该返回
    # 但 t2 (works_at X, valid_to=2025-03-31) 应该不在
    assert len(rows) >= 1
    assert all(r["valid_to"] is None for r in rows)


# ---------- build_entity_timeline ----------

@pytest.mark.asyncio
async def test_build_entity_timeline_subject_and_object(engine_with_data):
    """时间线: 既作为 subject 也作为 object."""
    conn = engine_with_data.conn
    # Alice (e1): t1, t2, t3 作为 subject; t1 作为 object(被 Bob knows? 没有)
    # 实际上 e1 在 t1 是 subject,e2 是 object
    timeline = await build_entity_timeline(conn, "e1")
    # e1 是 subject 的 triples: t1, t2, t3
    assert len(timeline) >= 3
    # 应该按 valid_from 升序
    valid_froms = [r["valid_from"] for r in timeline]
    assert valid_froms == sorted(valid_froms)


@pytest.mark.asyncio
async def test_build_entity_timeline_as_object(engine_with_data):
    """时间线: 仅作为 object."""
    conn = engine_with_data.conn
    # e2 (Bob) 是 t1 的 object
    timeline = await build_entity_timeline(conn, "e2")
    assert len(timeline) >= 1
    assert any(r["id"] == "t1" for r in timeline)


@pytest.mark.asyncio
async def test_build_entity_timeline_subject_only(engine_with_data):
    """时间线: 只查 subject 角色."""
    conn = engine_with_data.conn
    timeline = await build_entity_timeline(
        conn, "e2", include_as_subject=False, include_as_object=True
    )
    # e2 是 subject 的 triples: t4 (Bob reviewed PR)
    assert all(r["subject_id"] == "e2" or r["object_id"] == "e2" for r in timeline)
    # 实际上只该返回 object 角色
    assert all(r["object_id"] == "e2" for r in timeline)


@pytest.mark.asyncio
async def test_build_entity_timeline_object_only(engine_with_data):
    """时间线: 只查 subject 角色."""
    conn = engine_with_data.conn
    timeline = await build_entity_timeline(
        conn, "e1", include_as_subject=True, include_as_object=False
    )
    assert all(r["subject_id"] == "e1" for r in timeline)


@pytest.mark.asyncio
async def test_build_entity_timeline_invalid_args(engine_with_data):
    """两个 include 都 False 应 raise."""
    conn = engine_with_data.conn
    with pytest.raises(ValueError, match="至少一个"):
        await build_entity_timeline(
            conn, "e1", include_as_subject=False, include_as_object=False
        )


@pytest.mark.asyncio
async def test_build_entity_timeline_nonexistent_entity(engine_with_data):
    """不存在的实体应返回空列表."""
    conn = engine_with_data.conn
    timeline = await build_entity_timeline(conn, "nonexistent")
    assert timeline == []


# ---------- 空数据库行为 ----------

@pytest.mark.asyncio
async def test_queries_on_empty_db():
    """空数据库上查询应返回空列表(不报错)."""
    eng = SQLiteEngine(":memory:", load_vec=False)
    await eng.initialize()
    conn = eng.conn

    rows = await query_at_time(conn, "2025-01-01T00:00:00.000000Z")
    assert rows == []

    rows = await query_in_range(
        conn,
        "2025-01-01T00:00:00.000000Z",
        "2025-06-01T00:00:00.000000Z",
    )
    assert rows == []

    timeline = await build_entity_timeline(conn, "any")
    assert timeline == []

    await eng.close()
