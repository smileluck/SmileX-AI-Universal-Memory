"""Smoke test: SQLite R-tree spatial index.

R-tree is built into SQLite (no extension load needed).
"""

import sqlite3


def test_rtree_create_and_query():
    """验证 R-tree 虚拟表创建 + bbox 空间查询."""
    conn = sqlite3.connect(":memory:")

    conn.execute(
        "CREATE VIRTUAL TABLE locations USING rtree("
        "id, min_lng, max_lng, min_lat, max_lat)"
    )

    # 插入几个 bbox(模拟位置区域)
    # 北京天安门(116.397, 39.916)附近
    conn.execute(
        "INSERT INTO locations VALUES (1, 116.39, 116.40, 39.91, 39.92)"
    )
    # 上海外滩(121.473, 31.231)附近
    conn.execute(
        "INSERT INTO locations VALUES (2, 121.47, 121.48, 31.22, 31.23)"
    )
    # 北京海淀(116.310, 39.991)
    conn.execute(
        "INSERT INTO locations VALUES (3, 116.30, 116.31, 39.98, 39.99)"
    )
    conn.commit()

    # 查询北京区域内的位置(经度 116.0-117.0,纬度 39.5-40.5)
    rows = conn.execute(
        "SELECT id FROM locations WHERE "
        "min_lng >= 116.0 AND max_lng <= 117.0 AND "
        "min_lat >= 39.5 AND max_lat <= 40.5"
    ).fetchall()

    ids = [r[0] for r in rows]
    assert 1 in ids, "北京天安门应该在查询范围内"
    assert 3 in ids, "北京海淀应该在查询范围内"
    assert 2 not in ids, "上海不应该在北京范围内"


def test_rtree_contains_query():
    """验证点包含查询(用 bbox 覆盖一个点)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE regions USING rtree("
        "id, min_x, max_x, min_y, max_y)"
    )
    conn.execute("INSERT INTO regions VALUES (1, 0.0, 10.0, 0.0, 10.0)")
    conn.commit()

    # 点 (5, 5) 是否落在任何 region 内
    rows = conn.execute(
        "SELECT id FROM regions WHERE "
        "min_x <= 5.0 AND max_x >= 5.0 AND "
        "min_y <= 5.0 AND max_y >= 5.0"
    ).fetchall()
    assert len(rows) == 1
