"""Spatial queries — radius search + hierarchical lookup.

Per main doc §6.5 检索能力矩阵:
- query_in_area(lng, lat, radius)   P99 < 20ms  (R-tree bbox + Haversine)
- query_in_location(path)           P99 < 10ms  (locations.path LIKE 'home.%')

依赖:
- locations 表(path + coordinates)
- memory_locations_rtree 虚拟表(R-tree 空间索引)
- locations.rowid ↔ memory_locations_rtree.id 映射
"""

from __future__ import annotations

import json
import math

import aiosqlite

from ...models import ScopeFilter
from ._scope import build_scope_clause

# 地球半径(米),用于 Haversine
EARTH_RADIUS_M = 6_371_000.0


def _haversine_m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """Haversine 公式计算两点之间的米距离."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _bbox_around(lng: float, lat: float, radius_m: float) -> tuple[float, float, float, float]:
    """计算 (lng, lat, radius_m) 对应的 bbox (min_lng, max_lng, min_lat, max_lat)."""
    lat_delta = radius_m / 111_000.0
    cos_lat = math.cos(math.radians(lat))
    # 高纬地区 cos(lat) 接近 0,加保护
    lng_delta = radius_m / (111_000.0 * max(cos_lat, 1e-6))
    return (
        lng - lng_delta,  # min_lng
        lng + lng_delta,  # max_lng
        lat - lat_delta,  # min_lat
        lat + lat_delta,  # max_lat
    )


async def upsert_location_with_rtree(
    conn: aiosqlite.Connection,
    *,
    id: str,
    location_id: str,
    name: str,
    location_type: str,
    path: str,
    coordinates: tuple[float, float] | None = None,
    parent_id: str | None = None,
    scope: str = "global",
    valid_from: str | None = None,
) -> int:
    """同时插入 locations 表 + R-tree 虚拟表.

    Returns:
        locations 表的 rowid(供后续 R-tree 更新使用)
    """
    coords_json = (
        json.dumps({"lng": coordinates[0], "lat": coordinates[1]})
        if coordinates
        else None
    )
    valid_from = valid_from or "1970-01-01T00:00:00.000000Z"

    # 1. 插入 locations(获取 rowid)
    cursor = await conn.execute(
        "INSERT INTO locations(id, location_id, name, location_type, parent_id, "
        "path, coordinates, scope, valid_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id, location_id, name, location_type, parent_id, path, coords_json, scope, valid_from),
    )
    rowid = cursor.lastrowid

    # 2. 插入 R-tree(如果有坐标)
    if coordinates and rowid is not None:
        lng, lat = coordinates
        await conn.execute(
            "INSERT INTO memory_locations_rtree(id, min_lng, max_lng, min_lat, max_lat) "
            "VALUES (?, ?, ?, ?, ?)",
            (rowid, lng, lng, lat, lat),  # point: min == max
        )

    return rowid or 0


async def query_in_area(
    conn: aiosqlite.Connection,
    lng: float,
    lat: float,
    radius_m: float,
    *,
    scope_filter: ScopeFilter | None = None,
) -> list[dict]:
    """空间半径查询 — 找出 (lng, lat) 周围 radius_m 米内的位置.

    算法(§6.5):
    1. 计算 bbox(lng±delta, lat±delta)
    2. R-tree 虚拟表粗过滤(候选集)
    3. JOIN locations 获取完整数据
    4. Haversine 精确过滤(< radius_m)

    Args:
        lng: 中心经度
        lat: 中心纬度
        radius_m: 搜索半径(米)
        scope_filter: 作用域过滤

    Returns:
        范围内的位置列表(dict 形式,每行额外带 _distance_m 字段,按距离升序)
    """
    min_lng, max_lng, min_lat, max_lat = _bbox_around(lng, lat, radius_m)

    # R-tree 粗过滤:row 的 bbox 与查询 bbox 相交
    # r.min_lng <= q.max_lng AND r.max_lng >= q.min_lng (同理 lat)
    sql = """
        SELECT l.*, r.id AS _rtree_id
        FROM locations l
        JOIN memory_locations_rtree r ON r.id = l.rowid
        WHERE r.min_lng <= ? AND r.max_lng >= ?
          AND r.min_lat <= ? AND r.max_lat >= ?
    """
    params: list = [max_lng, min_lng, max_lat, min_lat]

    scope_clause, scope_params = build_scope_clause(scope_filter)
    if scope_clause:
        sql += f" AND ({scope_clause})"
        params.extend(scope_params)

    cursor = await conn.execute(sql, params)
    candidates = list(await cursor.fetchall())

    # Haversine 精确过滤
    in_range: list[tuple[float, dict]] = []
    for row in candidates:
        coords_raw = row["coordinates"]
        if not coords_raw:
            continue
        try:
            coords = json.loads(coords_raw)
        except (json.JSONDecodeError, TypeError):
            continue
        row_lng = coords.get("lng")
        row_lat = coords.get("lat")
        if row_lng is None or row_lat is None:
            continue
        distance = _haversine_m(lng, lat, row_lng, row_lat)
        if distance <= radius_m:
            d = dict(row)
            d["_distance_m"] = distance
            in_range.append((distance, d))

    # 按距离升序
    in_range.sort(key=lambda x: x[0])
    return [d for _, d in in_range]


async def query_in_location(
    conn: aiosqlite.Connection,
    path: str,
    *,
    include_children: bool = True,
    scope_filter: ScopeFilter | None = None,
) -> list[aiosqlite.Row]:
    """空间层级查询 — 根据 path 查询位置(可选包含子层).

    语义(§6.5):
    - `query_in_location("home")` 返回 path="home" 自身 + path LIKE "home.%" 的所有子层
    - `include_children=False` 只返回精确匹配 path 的位置

    Args:
        path: 位置路径(如 "home", "home.living_room")
        include_children: 是否包含子层(默认 True)
        scope_filter: 作用域过滤

    Returns:
        匹配的位置列表(按 path 升序)
    """
    where_clauses: list[str] = []
    params: list[str] = []

    if include_children:
        # 区间扫描代替 LIKE 'path.%'(M4): LIKE 默认大小写不敏感,无法命中
        # idx_locations_path(BINARY)前缀索引;'.'(0x2E) 的下一字符是 '/'(0x2F),
        # 故 [path + '.', path + '/') 恰为全部子层。语义差异: 不再大小写折叠
        # (path 本身区分大小写,与索引排序一致)。
        where_clauses.append("(path = ? OR (path >= ? AND path < ?))")
        params.extend([path, path + ".", path + "/"])
    else:
        where_clauses.append("path = ?")
        params.append(path)

    scope_clause, scope_params = build_scope_clause(scope_filter)
    if scope_clause:
        where_clauses.append(f"({scope_clause})")
        params.extend(scope_params)

    sql = (
        "SELECT * FROM locations WHERE "
        + " AND ".join(where_clauses)
        + " ORDER BY path"
    )

    cursor = await conn.execute(sql, params)
    return list(await cursor.fetchall())
