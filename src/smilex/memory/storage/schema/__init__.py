"""Schema loader — applies DDL files in order, tracks schema version via PRAGMA.

Per module doc 02 §6 Schema 版本化机制:
- PRAGMA user_version 存储当前 schema 版本
- migrations/001_*.sql ~ 012_*.sql 顺序应用
- idempotent(每次启动只应用未应用的版本)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Schema 版本号(每次新增 .sql 文件或修改结构时 +1)
SCHEMA_VERSION = 13

# DDL 文件清单(按版本号顺序)
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, "001_entities.sql"),
    (2, "002_triples.sql"),
    (3, "003_locations.sql"),
    (4, "004_temporal_fragments.sql"),
    (5, "005_causal_chains.sql"),
    (6, "006_vector_links.sql"),
    (7, "007_checkpoints.sql"),
    (8, "008_project_current_state.sql"),
    (9, "009_memory_l0_snapshot.sql"),
    (10, "010_virtual_tables.sql"),
    (11, "011_archive_and_predicate_dict.sql"),
    (12, "012_perf_indexes.sql"),
    (13, "013_fts5.sql"),
)


def get_schema_dir() -> Path:
    """返回 schema DDL 文件所在目录."""
    return Path(__file__).parent


def _read_sql(filename: str) -> str:
    """读取 DDL 文件内容."""
    schema_dir = get_schema_dir()
    return (schema_dir / filename).read_text(encoding="utf-8")


def get_user_version(conn: sqlite3.Connection) -> int:
    """读取当前 schema 版本(0 = 未初始化)."""
    cursor = conn.execute("PRAGMA user_version")
    return cursor.fetchone()[0]


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    """设置 schema 版本(PRAGMA 不支持参数化,version 必须为 int)."""
    if not isinstance(version, int) or version < 0:
        raise ValueError(f"version 必须非负整数,得到 {version!r}")
    conn.execute(f"PRAGMA user_version = {version}")


def _vec_tables_exist(conn: sqlite3.Connection) -> bool:
    """010 虚拟表(memory_vectors)是否已创建."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'memory_vectors'"
    ).fetchone()
    return row is not None


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    load_vec_extension: bool = True,
) -> int:
    """应用所有未应用的 DDL migrations.

    Args:
        conn: SQLite 连接(同步)
        load_vec_extension: 是否加载 sqlite-vec 扩展(010 虚拟表需要)
            — 默认 True;测试环境如果未安装 sqlite-vec 可设 False(会跳过 010)

    Returns:
        应用到的最终 schema 版本号

    Raises:
        sqlite3.OperationalError: DDL 执行失败
        ImportError: 010 文件应用但 sqlite-vec 未安装且 load_vec_extension=True

    Note:
        010 曾在无扩展环境下被跳过时(user_version 已越过 10 但虚拟表不存在),
        再次以 load_vec_extension=True 打开会补建 010(按表存在性判定,
        不回退 user_version)。
    """
    current = get_user_version(conn)

    for version, filename in MIGRATIONS:
        # 010_virtual_tables.sql 需要 sqlite-vec 扩展
        if version == 10:
            if load_vec_extension:
                try:
                    import sqlite_vec

                    conn.enable_load_extension(True)
                    sqlite_vec.load(conn)
                    conn.enable_load_extension(False)
                except (ImportError, sqlite3.OperationalError) as e:
                    raise ImportError(
                        f"应用 {filename} 需要 sqlite-vec 扩展,但加载失败: {e}. "
                        "请安装 sqlite-vec 或传入 load_vec_extension=False 跳过虚拟表."
                    ) from e
                if version <= current and _vec_tables_exist(conn):
                    continue  # 010 已应用过
                # 曾被跳过(无扩展建库)→ 补建,但不回退 user_version
                sql = _read_sql(filename)
                conn.executescript(sql)
                if version > get_user_version(conn):
                    set_user_version(conn, version)
                current = max(current, get_user_version(conn))
                continue
            else:
                # 跳过 010,版本号不更新(后续迁移不依赖 010)
                continue

        if version <= current:
            continue

        sql = _read_sql(filename)
        conn.executescript(sql)
        set_user_version(conn, version)

    return get_user_version(conn)


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """列出所有用户表(不含 sqlite_ 前缀)."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [row[0] for row in cursor.fetchall()]


def list_indexes(conn: sqlite3.Connection) -> list[str]:
    """列出所有用户索引(不含 sqlite_ 前缀)."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [row[0] for row in cursor.fetchall()]


def list_triggers(conn: sqlite3.Connection) -> list[str]:
    """列出所有触发器."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
    )
    return [row[0] for row in cursor.fetchall()]


__all__ = [
    "SCHEMA_VERSION",
    "MIGRATIONS",
    "apply_migrations",
    "get_schema_dir",
    "get_user_version",
    "set_user_version",
    "list_tables",
    "list_indexes",
    "list_triggers",
]
