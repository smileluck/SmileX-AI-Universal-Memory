"""Unit tests for spatial queries (L1.9).

验证:
- upsert_location_with_rtree: 同步写入 locations + R-tree
- query_in_area: 半径查询(R-tree 粗过滤 + Haversine 精确过滤)
- query_in_location: 路径层级查询(含/不含子层)
- ScopeFilter 集成
- 距离排序
"""

from __future__ import annotations

import pytest

from smilex.memory.models import ScopeFilter
from smilex.memory.storage.queries import (
    query_in_area,
    query_in_location,
    upsert_location_with_rtree,
)
from smilex.memory.storage.sqlite_engine import SQLiteEngine


@pytest.fixture
async def engine_with_locations():
    """引擎 + 预置位置数据(北京/上海/北京内子地点)."""
    eng = SQLiteEngine(":memory:", load_vec=True)
    await eng.initialize()
    conn = eng.conn

    locations_data = [
        # id, location_id, name, type, path, coords, parent, scope
        ("l1", "beijing", "北京", "region", "beijing", (116.4074, 39.9042), None, "global"),
        # 北京天安门(约 6.4 km 外)
        ("l2", "beijing.tiananmen", "天安门", "point", "beijing.tiananmen",
         (116.3974, 39.9162), "l1", "global"),
        # 北京海淀(约 11 km 外)
        ("l3", "beijing.haidian", "海淀", "area", "beijing.haidian",
         (116.3104, 39.9912), "l1", "global"),
        # 上海外滩(距离北京约 1067 km,不在 100km 范围内)
        ("l4", "shanghai", "上海", "region", "shanghai",
         (121.4737, 31.2304), None, "global"),
        # 项目专属地点(北京附近)
        ("l5", "proj_office", "项目办公室", "point", "beijing.office.proj_a",
         (116.4100, 39.9100), "l1", "project:proj_a"),
    ]
    for loc in locations_data:
        await upsert_location_with_rtree(
            conn,
            id=loc[0],
            location_id=loc[1],
            name=loc[2],
            location_type=loc[3],
            path=loc[4],
            coordinates=loc[5],
            parent_id=loc[6],
            scope=loc[7],
        )
    await conn.commit()
    yield eng
    await eng.close()


# ---------- upsert_location_with_rtree ----------

@pytest.mark.asyncio
async def test_upsert_inserts_into_both_tables():
    eng = SQLiteEngine(":memory:", load_vec=True)
    await eng.initialize()
    conn = eng.conn

    rowid = await upsert_location_with_rtree(
        conn,
        id="l1",
        location_id="home",
        name="家",
        location_type="region",
        path="home",
        coordinates=(116.4074, 39.9042),
    )
    await conn.commit()

    assert rowid > 0

    # locations 表有记录
    cursor = await conn.execute("SELECT count(*) FROM locations")
    assert (await cursor.fetchone())[0] == 1

    # R-tree 表有记录
    cursor = await conn.execute("SELECT count(*) FROM memory_locations_rtree")
    assert (await cursor.fetchone())[0] == 1

    await eng.close()


@pytest.mark.asyncio
async def test_upsert_without_coords_skips_rtree():
    """无坐标的位置不入 R-tree."""
    eng = SQLiteEngine(":memory:", load_vec=True)
    await eng.initialize()
    conn = eng.conn

    await upsert_location_with_rtree(
        conn,
        id="l1",
        location_id="concept_x",
        name="X 概念",
        location_type="region",
        path="concept.x",
        coordinates=None,
    )
    await conn.commit()

    cursor = await conn.execute("SELECT count(*) FROM locations")
    assert (await cursor.fetchone())[0] == 1

    cursor = await conn.execute("SELECT count(*) FROM memory_locations_rtree")
    assert (await cursor.fetchone())[0] == 0

    await eng.close()


# ---------- query_in_area ----------

@pytest.mark.asyncio
async def test_query_in_area_finds_nearby(engine_with_locations):
    """以北京为中心,10km 内应该有 3 个地点(北京/天安门/海淀/项目办公室)."""
    conn = engine_with_locations.conn
    rows = await query_in_area(conn, 116.4074, 39.9042, radius_m=15_000)
    # 北京 + 天安门(~6.4km) + 海淀(~11km) + 项目办公室(~0.6km)
    names = {r["name"] for r in rows}
    assert "北京" in names
    assert "天安门" in names
    assert "海淀" in names
    assert "项目办公室" in names
    assert "上海" not in names


@pytest.mark.asyncio
async def test_query_in_area_excludes_far(engine_with_locations):
    """上海不应出现在北京 100km 范围内."""
    conn = engine_with_locations.conn
    rows = await query_in_area(conn, 116.4074, 39.9042, radius_m=100_000)
    names = {r["name"] for r in rows}
    assert "上海" not in names


@pytest.mark.asyncio
async def test_query_in_area_returns_distance(engine_with_locations):
    """结果应带 _distance_m 字段,按距离升序."""
    conn = engine_with_locations.conn
    rows = await query_in_area(conn, 116.4074, 39.9042, radius_m=15_000)
    assert len(rows) >= 2

    # 第一个应该是北京自己(距离 0)
    assert rows[0]["name"] == "北京"
    assert rows[0]["_distance_m"] == 0

    # 距离应递增
    distances = [r["_distance_m"] for r in rows]
    assert distances == sorted(distances)


@pytest.mark.asyncio
async def test_query_in_area_tight_radius(engine_with_locations):
    """1km 范围内只有北京自己和项目办公室."""
    conn = engine_with_locations.conn
    rows = await query_in_area(conn, 116.4074, 39.9042, radius_m=1000)
    names = {r["name"] for r in rows}
    assert "北京" in names
    assert "项目办公室" in names
    assert "天安门" not in names  # ~6.4km 外
    assert "海淀" not in names  # ~11km 外


@pytest.mark.asyncio
async def test_query_in_area_with_scope_filter(engine_with_locations):
    """项目 scope 过滤 — 只查 project:proj_a 的地点."""
    conn = engine_with_locations.conn
    sf = ScopeFilter(include_global=False, include_project="proj_a")
    rows = await query_in_area(
        conn, 116.4074, 39.9042, radius_m=15_000, scope_filter=sf
    )
    # 应该只返回项目办公室(scope=project:proj_a)
    names = {r["name"] for r in rows}
    assert "项目办公室" in names
    assert "北京" not in names
    assert "天安门" not in names


@pytest.mark.asyncio
async def test_query_in_area_no_matches(engine_with_locations):
    """太平洋上的查询应返回空."""
    conn = engine_with_locations.conn
    rows = await query_in_area(conn, -150.0, 0.0, radius_m=1000)
    assert rows == []


# ---------- query_in_location ----------

@pytest.mark.asyncio
async def test_query_in_location_exact(engine_with_locations):
    """精确 path 查询(不含子层)."""
    conn = engine_with_locations.conn
    rows = await query_in_location(conn, "beijing", include_children=False)
    assert len(rows) == 1
    assert rows[0]["path"] == "beijing"


@pytest.mark.asyncio
async def test_query_in_location_with_children(engine_with_locations):
    """path 查询含子层 — beijing 及其所有子路径."""
    conn = engine_with_locations.conn
    rows = await query_in_location(conn, "beijing", include_children=True)
    paths = {r["path"] for r in rows}
    assert "beijing" in paths
    assert "beijing.tiananmen" in paths
    assert "beijing.haidian" in paths
    assert "beijing.office.proj_a" in paths
    assert "shanghai" not in paths


@pytest.mark.asyncio
async def test_query_in_location_partial_path(engine_with_locations):
    """中间层路径查询 — beijing.office 及其子."""
    conn = engine_with_locations.conn
    # 先插入一个中间层
    await upsert_location_with_rtree(
        conn,
        id="l6",
        location_id="beijing.office",
        name="北京办公室区",
        location_type="area",
        path="beijing.office",
        coordinates=(116.4080, 39.9050),
        parent_id="l1",
        scope="global",
    )
    await conn.commit()

    rows = await query_in_location(conn, "beijing.office", include_children=True)
    paths = {r["path"] for r in rows}
    assert "beijing.office" in paths
    assert "beijing.office.proj_a" in paths
    # 不应该包含其他 beijing 子层
    assert "beijing.tiananmen" not in paths


@pytest.mark.asyncio
async def test_query_in_location_nonexistent_path(engine_with_locations):
    """不存在的路径应返回空."""
    conn = engine_with_locations.conn
    rows = await query_in_location(conn, "nonexistent")
    assert rows == []


@pytest.mark.asyncio
async def test_query_in_location_with_scope_filter(engine_with_locations):
    """path 查询 + scope 过滤."""
    conn = engine_with_locations.conn
    sf = ScopeFilter(include_global=False, include_project="proj_a")
    rows = await query_in_location(conn, "beijing", scope_filter=sf)
    # 应该只返回 project:proj_a 的记录
    assert all(r["scope"] == "project:proj_a" for r in rows)
    paths = {r["path"] for r in rows}
    assert "beijing.office.proj_a" in paths


@pytest.mark.asyncio
async def test_query_in_location_ordering(engine_with_locations):
    """结果按 path 字母升序."""
    conn = engine_with_locations.conn
    rows = await query_in_location(conn, "beijing", include_children=True)
    paths = [r["path"] for r in rows]
    assert paths == sorted(paths)


# ---------- 空 DB 行为 ----------

@pytest.mark.asyncio
async def test_spatial_queries_on_empty_db():
    """空数据库上的 spatial 查询应返回空列表."""
    eng = SQLiteEngine(":memory:", load_vec=True)
    await eng.initialize()
    conn = eng.conn

    rows = await query_in_area(conn, 116.0, 39.0, radius_m=1000)
    assert rows == []

    rows = await query_in_location(conn, "any")
    assert rows == []

    await eng.close()
