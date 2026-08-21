"""Integration tests for Layer 1 storage engine (L1.15).

端到端场景(文件数据库 + tmp_path):
- 建库(schema migrations + WAL pragmas + sqlite-vec)
- 写入实体 / 三元组 / 时态 / 空间 / 因果 / project_state 数据
- 各类查询: temporal / spatial / graph / causal / scope filter
- hybrid_search(RRF 融合)
- get_current_state(触发器联动)
- 关闭后重开验证持久化
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from smilex.memory.models import Entity, MemoryScope, ScopeFilter, Triple
from smilex.memory.storage.queries import HybridQuery
from smilex.memory.storage.schema import SCHEMA_VERSION
from smilex.memory.storage.storage_engine import StorageEngine

# ---------- Fixtures ----------

# 时间常量(固定,便于时序断言)
T_JAN = datetime(2025, 1, 1, tzinfo=UTC)
T_FEB = datetime(2025, 2, 1, tzinfo=UTC)
T_MAR_END = datetime(2025, 3, 31, 23, 59, 59, tzinfo=UTC)
T_APR = datetime(2025, 4, 1, tzinfo=UTC)
T_MAY_1 = datetime(2025, 5, 1, tzinfo=UTC)
T_MAY_2 = datetime(2025, 5, 2, tzinfo=UTC)
T_MAY_3 = datetime(2025, 5, 3, tzinfo=UTC)
T_JUN = datetime(2025, 6, 1, tzinfo=UTC)
T_MID_JUN = datetime(2025, 6, 15, tzinfo=UTC)
T_DEC = datetime(2025, 12, 31, tzinfo=UTC)

# 北京坐标(用于空间查询)
HOME_LNG, HOME_LAT = 116.4074, 39.9042


@pytest.fixture
async def engine(tmp_path):
    """文件数据库引擎(含 sqlite-vec / R-tree 虚拟表)."""
    eng = StorageEngine(tmp_path / "integration.db", load_vec=True)
    await eng.initialize()
    yield eng
    await eng.close()


@pytest.fixture
async def populated(engine):
    """预置端到端数据集:

    - 3 实体: alice(global) / bob(global) / proj(project:proj_a)
    - 2 位置: home / home.living_room(含 R-tree)
    - 3 时态 triples: knows(恒有效) / works_at Q1(已失效) / works_at Q2(有效)
    - 3 因果 triples: c1 → c2 → c3(predecessor_id 链)
    - 1 project_state triple: proj.status = active(触发器维护)
    """
    data: dict[str, object] = {}

    # 实体
    alice = Entity(entity_id="person:alice", entity_type="person", name="Alice",
                   scope=MemoryScope.GLOBAL, valid_from=T_JAN)
    bob = Entity(entity_id="person:bob", entity_type="person", name="Bob",
                 scope=MemoryScope.GLOBAL, valid_from=T_JAN)
    proj = Entity(entity_id="project:demo", entity_type="concept", name="Demo",
                  scope=MemoryScope.PROJECT, valid_from=T_JAN)
    await engine.write_entity(alice)
    await engine.write_entity(bob)
    await engine.write_entity(proj, scope_id="proj_a")
    data.update(alice=alice, bob=bob, proj=proj)

    # 位置(家 + 子层客厅)
    await engine.write_location(
        id="loc_home", location_id="home", name="家", location_type="region",
        path="home", coordinates=(HOME_LNG, HOME_LAT),
    )
    await engine.write_location(
        id="loc_lr", location_id="home_living_room", name="客厅", location_type="area",
        path="home.living_room", coordinates=(HOME_LNG + 0.0001, HOME_LAT + 0.0001),
        parent_id="loc_home",
    )

    # 时态 triples
    t_knows = Triple(triple_id="tri_knows", subject_id=alice.id, predicate="knows",
                     object_id=bob.id, scope=MemoryScope.GLOBAL, valid_from=T_JAN)
    t_work_q1 = Triple(triple_id="tri_work_q1", subject_id=alice.id, predicate="works_at",
                       object_value="CompanyX", scope=MemoryScope.GLOBAL,
                       valid_from=T_JAN, valid_to=T_MAR_END)
    t_work_q2 = Triple(triple_id="tri_work_q2", subject_id=alice.id, predicate="works_at",
                       object_value="CompanyY", scope=MemoryScope.GLOBAL, valid_from=T_APR)
    for t in (t_knows, t_work_q1, t_work_q2):
        await engine.write_triple(t)
    data.update(t_knows=t_knows, t_work_q1=t_work_q1, t_work_q2=t_work_q2)

    # 因果链: 需求变更 → 代码重构 → 测试失败
    t_c1 = Triple(triple_id="tri_c1", subject_id=proj.id, predicate="causes",
                  object_value="需求变更", scope=MemoryScope.PROJECT,
                  valid_from=T_MAY_1, relation_type="causal")
    t_c2 = Triple(triple_id="tri_c2", subject_id=proj.id, predicate="causes",
                  object_value="代码重构", scope=MemoryScope.PROJECT,
                  valid_from=T_MAY_2, predecessor_id=t_c1.id, causal_level=1,
                  relation_type="causal")
    t_c3 = Triple(triple_id="tri_c3", subject_id=proj.id, predicate="causes",
                  object_value="测试失败", scope=MemoryScope.PROJECT,
                  valid_from=T_MAY_3, predecessor_id=t_c2.id, causal_level=2,
                  relation_type="causal")
    for t in (t_c1, t_c2, t_c3):
        await engine.write_triple(t, scope_id="proj_a")
    data.update(t_c1=t_c1, t_c2=t_c2, t_c3=t_c3)

    # project_state(触发器联动)
    t_state = Triple(triple_id="tri_status", subject_id=proj.id, predicate="status",
                     object_value="active", scope=MemoryScope.PROJECT,
                     valid_from=T_JUN, relation_type="project_state")
    await engine.write_triple(t_state, scope_id="proj_a")
    data["t_state"] = t_state

    return data


# ---------- 建库 ----------

@pytest.mark.asyncio
async def test_schema_initialized(engine):
    """文件库初始化: schema 版本到位 + WAL 模式生效."""
    cursor = await engine.conn.execute("PRAGMA user_version")
    assert (await cursor.fetchone())[0] == SCHEMA_VERSION

    cursor = await engine.conn.execute("PRAGMA journal_mode")
    assert (await cursor.fetchone())[0] == "wal"

    # 核心表 + 虚拟表均已创建
    cursor = await engine.conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table') "
        "AND name NOT LIKE 'sqlite_%'"
    )
    tables = {row[0] for row in await cursor.fetchall()}
    assert {"entities", "triples", "locations", "project_current_state"} <= tables
    assert "memory_locations_rtree" in tables


# ---------- 时态查询 ----------

@pytest.mark.asyncio
async def test_temporal_point_query(populated, engine):
    """时间点查询: 2 月只有 knows + works_at Q1 有效."""
    rows = await engine.query_at_time(T_FEB)
    predicates = {r["predicate"] for r in rows}
    assert "knows" in predicates
    assert "works_at" in predicates
    values = [r["object_value"] for r in rows if r["predicate"] == "works_at"]
    assert values == ["CompanyX"]  # Q2 尚未生效


@pytest.mark.asyncio
async def test_temporal_range_query(populated, engine):
    """范围查询: 3-4 月覆盖 works_at 两版 + knows."""
    rows = await engine.query_in_range(datetime(2025, 3, 1, tzinfo=UTC), T_APR)
    works = [r["object_value"] for r in rows if r["predicate"] == "works_at"]
    assert sorted(works) == ["CompanyX", "CompanyY"]


@pytest.mark.asyncio
async def test_entity_timeline(populated, engine):
    """实体时间线: alice 相关 triples 按 valid_from 升序."""
    alice = populated["alice"]
    rows = await engine.build_entity_timeline(alice.id)
    assert len(rows) == 3  # knows + works_at x2
    valid_froms = [r["valid_from"] for r in rows]
    assert valid_froms == sorted(valid_froms)


# ---------- 空间查询 ----------

@pytest.mark.asyncio
async def test_spatial_area_query(populated, engine):
    """半径查询: 家附近 200m 内命中 2 个位置,按距离升序."""
    rows = await engine.query_in_area(HOME_LNG, HOME_LAT, 200.0)
    assert len(rows) == 2
    assert rows[0]["path"] == "home"  # 最近的是圆心本身
    assert rows[0]["_distance_m"] < rows[1]["_distance_m"]


@pytest.mark.asyncio
async def test_spatial_location_hierarchy(populated, engine):
    """层级查询: path='home' 含子层;不含子层时只返回自身."""
    with_children = await engine.query_in_location("home")
    assert [r["path"] for r in with_children] == ["home", "home.living_room"]

    exact = await engine.query_in_location("home", include_children=False)
    assert [r["path"] for r in exact] == ["home"]


# ---------- 图查询 ----------

@pytest.mark.asyncio
async def test_graph_find_path(populated, engine):
    """最短路径: alice → knows → bob,depth=1."""
    path = await engine.find_path(populated["alice"].id, populated["bob"].id)
    assert path is not None
    assert path["depth"] == 1
    assert path["edges"] == [populated["t_knows"].id]


@pytest.mark.asyncio
async def test_graph_n_degree_relations(populated, engine):
    """N 度关系: alice 的 1 度关系是 bob."""
    relations = await engine.find_n_degree_relations(
        populated["alice"].id, max_depth=2
    )
    assert [(r["entity_id"], r["depth"]) for r in relations] == [
        (populated["bob"].id, 1)
    ]


# ---------- 因果查询 ----------

@pytest.mark.asyncio
async def test_causal_chain_backward(populated, engine):
    """向后追溯: 从 c3 走到根因 c1."""
    result = await engine.trace_causal_chain(populated["t_c3"].id, direction="backward")
    chain = result["backward"]
    assert [e["depth"] for e in chain] == [0, 1, 2]
    assert chain[0]["id"] == populated["t_c3"].id
    assert chain[-1]["id"] == populated["t_c1"].id


@pytest.mark.asyncio
async def test_causal_chain_forward(populated, engine):
    """向前追溯: 从 c1 找所有后继."""
    result = await engine.trace_causal_chain(populated["t_c1"].id, direction="forward")
    forward_ids = {e["id"] for e in result["forward"]}
    assert forward_ids == {populated["t_c2"].id, populated["t_c3"].id}


# ---------- 混合检索 ----------

@pytest.mark.asyncio
async def test_hybrid_search_e2e(populated, engine):
    """hybrid_search: graph + temporal 双策略 RRF 融合."""
    query = HybridQuery(
        entity_id=populated["alice"].id,
        time_range=(T_JAN, T_DEC),
    )
    results = await engine.hybrid_search(query, top_k=10)
    assert len(results) >= 1

    result_ids = {r["id"] for r in results}
    assert populated["t_knows"].id in result_ids

    # 双策略命中的文档 strategies_hit 应包含 temporal + graph
    knows_hit = next(r for r in results if r["id"] == populated["t_knows"].id)
    assert knows_hit["score"] > 0
    assert {"temporal", "graph"} <= set(knows_hit["strategies_hit"])


# ---------- 当前状态(触发器) ----------

@pytest.mark.asyncio
async def test_current_state_trigger(populated, engine):
    """project_state 写入 → 触发器维护 → get_current_state 可读."""
    states = await engine.get_current_state("project:proj_a", populated["proj"].id)
    assert len(states) == 1
    assert states[0]["predicate"] == "status"
    assert states[0]["object_value"] == "active"


# ---------- scope 过滤 ----------

@pytest.mark.asyncio
async def test_scope_filter_excludes_global(populated, engine):
    """ScopeFilter: 只看 proj_a 项目时排除 global triples."""
    sf = ScopeFilter(include_global=False, include_project="proj_a")
    rows = await engine.query_at_time(T_MID_JUN, scope_filter=sf)
    assert len(rows) >= 1
    assert all(r["scope"] == "project:proj_a" for r in rows)


@pytest.mark.asyncio
async def test_scope_filter_includes_both(populated, engine):
    """ScopeFilter: 默认(含 global)+ proj_a → 两类都召回."""
    sf = ScopeFilter(include_global=True, include_project="proj_a")
    rows = await engine.query_at_time(T_MID_JUN, scope_filter=sf)
    scopes = {r["scope"] for r in rows}
    assert scopes == {"global", "project:proj_a"}


# ---------- 持久化 ----------

@pytest.mark.asyncio
async def test_persistence_across_reopen(tmp_path):
    """关闭后重开同一文件库: 数据仍在."""
    db = tmp_path / "persist.db"

    eng = StorageEngine(db, load_vec=True)
    await eng.initialize()
    e = Entity(entity_id="person:carol", entity_type="person", name="Carol",
               scope=MemoryScope.GLOBAL)
    eid = await eng.write_entity(e)
    await eng.close()

    eng2 = StorageEngine(db, load_vec=True)
    await eng2.initialize()
    try:
        fetched = await eng2.get_entity(eid)
        assert fetched is not None
        assert fetched.name == "Carol"
    finally:
        await eng2.close()
