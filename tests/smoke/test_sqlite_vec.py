"""Smoke test: sqlite-vec extension load + KNN query.

This is a BLOCKING dependency for MVP (per decision D1).
"""

import sqlite3

import sqlite_vec


def test_sqlite_vec_load():
    """验证 sqlite-vec C 扩展能在 Windows 上加载."""
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    cursor = conn.execute("SELECT vec_version()")
    row = cursor.fetchone()
    assert row is not None, "vec_version() 应该返回非空"
    assert row[0]


def test_sqlite_vec_knn():
    """验证 vec0 虚拟表 + KNN 查询."""
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute(
        "CREATE VIRTUAL TABLE vec_test USING vec0(embedding float[4])"
    )

    # 插入 5 个向量
    vectors = [
        "[1.0, 0.0, 0.0, 0.0]",
        "[0.0, 1.0, 0.0, 0.0]",
        "[0.0, 0.0, 1.0, 0.0]",
        "[0.9, 0.1, 0.0, 0.0]",  # 接近第 1 个
        "[0.0, 0.0, 0.0, 1.0]",
    ]
    for i, v in enumerate(vectors, start=1):
        conn.execute("INSERT INTO vec_test(rowid, embedding) VALUES (?, ?)", (i, v))

    # 查询最接近 [1.0, 0.0, 0.0, 0.0] 的 Top-2
    rows = conn.execute(
        "SELECT rowid, distance FROM vec_test "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT 2",
        ("[1.0, 0.0, 0.0, 0.0]",),
    ).fetchall()

    assert len(rows) == 2
    # rowid=1 ([1,0,0,0]) 距离应该最近(为 0)
    assert rows[0][0] == 1
    # rowid=4 ([0.9,0.1,0,0]) 应该是第二近
    assert rows[1][0] == 4


def test_sqlite_vec_version_string():
    """版本号可读,便于调试."""
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    version = conn.execute("SELECT vec_version()").fetchone()[0]
    assert isinstance(version, str)
    assert len(version) > 0
