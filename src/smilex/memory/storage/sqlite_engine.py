"""SQLiteEngine — aiosqlite + WAL pragmas + sqlite-vec + schema migrations.

Per main doc §19.1 + §15.1:
- WAL + synchronous=NORMAL + busy_timeout → 写入吞吐 3x
- cachebox 热缓存 → 读取命中率 > 70%
- sqlite-vec HNSW → 向量 P99 < 30ms

线程模型:
- aiosqlite 在独立 worker 线程跑 sqlite3.Connection
- 主线程访问 _conn 会触发 SQLite 线程亲和性错误
- 解决:用 `await conn._execute(fn, *args)` 把同步函数调度到 worker 线程
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .schema import (
    MIGRATIONS,
    get_user_version,
)


def _quote_sql_literal(value: str) -> str:
    """SQL 单引号字面量内容转义(' 加倍);调用方负责包裹外层引号.

    PRAGMA 语句不支持 ``?`` 绑定,密钥等字符串只能以字面量进入语句,
    转义防引号截断/拼接。
    """
    return value.replace("'", "''")


def _setup_sqlite_vec(raw_conn: sqlite3.Connection) -> None:
    """同步函数: 在 sqlite3.Connection 上加载 sqlite-vec 扩展.

    必须通过 aiosqlite.Connection._execute 调度到 worker 线程执行,
    否则会触发 "SQLite objects created in a thread can only be used in that same thread" 错误.
    """
    import sqlite_vec

    raw_conn.enable_load_extension(True)
    sqlite_vec.load(raw_conn)
    raw_conn.enable_load_extension(False)


def _apply_migrations_sync(
    raw_conn: sqlite3.Connection,
    *,
    vec_already_loaded: bool = True,
) -> int:
    """同步函数: 应用 schema migrations.

    通过 aiosqlite.Connection._execute 调度到 worker 线程执行.
    与 schema.apply_migrations() 等价,但避免重复加载 sqlite-vec.

    Note:
        010 曾在无扩展环境下被跳过(user_version 已越过 10 但虚拟表不存在)时,
        扩展已加载则按表存在性补建 010,不回退 user_version。
    """
    from .schema import _vec_tables_exist, get_schema_dir

    current = get_user_version(raw_conn)
    for version, filename in MIGRATIONS:
        if version == 10:
            if not vec_already_loaded:
                # 需要先加载 sqlite-vec — 这里不允许,应该由调用方先 load
                continue
            if version <= current and _vec_tables_exist(raw_conn):
                continue  # 010 已应用过
            sql = (get_schema_dir() / filename).read_text(encoding="utf-8")
            raw_conn.executescript(sql)
            if version > get_user_version(raw_conn):
                raw_conn.execute(f"PRAGMA user_version = {version}")
            current = max(current, get_user_version(raw_conn))
            continue
        if version <= current:
            continue
        sql = (get_schema_dir() / filename).read_text(encoding="utf-8")
        raw_conn.executescript(sql)
        raw_conn.execute(f"PRAGMA user_version = {version}")
    return get_user_version(raw_conn)


class SQLiteEngine:
    """SQLite 异步引擎 + sqlite-vec + schema migrations.

    使用方法:
        engine = SQLiteEngine("~/.smilex/data.db")
        await engine.initialize()
        async with engine.transaction() as conn:
            await conn.execute("INSERT INTO ...")
        await engine.close()

    线程安全:
        - 单实例在单事件循环内使用
        - 底层 aiosqlite.Connection 自带 worker 线程,自动串行化 SQL 调用
        - 跨进程并发靠 SQLite WAL 模式支持
    """

    DEFAULT_PRAGMAS: dict[str, Any] = {
        "journal_mode": "WAL",  # 并发读 + 单写者,写入吞吐 3x
        "synchronous": "NORMAL",  # 平衡安全与性能(WAL 下不会丢数据)
        "temp_store": "MEMORY",  # 临时表/排序在内存
        "mmap_size": 268435456,  # 256MB 内存映射
        "cache_size": -64000,  # 64MB 页缓存
        "foreign_keys": 1,  # 启用外键约束
        "busy_timeout": 5000,  # 5s 锁等待
    }

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        pragmas: dict[str, Any] | None = None,
        load_vec: bool = True,
        encryption_key: str | None = None,
    ) -> None:
        self.db_path: str = str(db_path)
        self.pragmas: dict[str, Any] = {**self.DEFAULT_PRAGMAS, **(pragmas or {})}
        self.load_vec: bool = load_vec
        # SQLCipher 密钥(§15.4,实验性): 必须是连接上的第一条语句,
        # 且要求解释器的 sqlite3 为 SQLCipher 构建(见 storage/sqlcipher.py)
        self.encryption_key: str | None = encryption_key
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """已初始化的 aiosqlite 连接(未初始化时 raise)."""
        if self._conn is None:
            raise RuntimeError("SQLiteEngine 未初始化,请先调用 await initialize()")
        return self._conn

    @property
    def is_initialized(self) -> bool:
        return self._conn is not None

    async def initialize(self) -> None:
        """连接 + 应用 pragmas + 加载 sqlite-vec + 应用 schema migrations.

        幂等: 重复调用会 raise(避免并发初始化).
        """
        if self._conn is not None:
            raise RuntimeError("SQLiteEngine 已初始化")

        if self.encryption_key is not None and self.db_path == ":memory:":
            raise ValueError("内存库不支持加密(加密仅对文件库有意义)")

        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        # 0. SQLCipher 密钥(必须是第一条语句,先于任何会读库头的 pragma;
        #    随即校验 cipher_version — 香草 sqlite 会静默忽略 PRAGMA key,
        #    不校验就会写出明文库,这是安全网)
        if self.encryption_key is not None:
            await self._conn.execute(
                f"PRAGMA key = '{_quote_sql_literal(self.encryption_key)}'"
            )
            cursor = await self._conn.execute("PRAGMA cipher_version")
            row = await cursor.fetchone()
            version = str(row[0]).strip() if row is not None and row[0] else ""
            if not version:
                raise RuntimeError(
                    "配置了 db_encryption_key,但当前解释器的 sqlite3 不是 "
                    "SQLCipher 构建(PRAGMA key 被静默忽略,继续将写出明文库,已中止). "
                    "解决: 安装 extra `pip install 'smilex-ai-memory[encryption]'` "
                    "并确保系统有 libsqlcipher(brew install sqlcipher / "
                    "apt install libsqlcipher-dev),详见 README「库文件加密」小节."
                )

        # 1. 应用 PRAGMAs(字符串值安全引用,防运算符配置里的引号/分号破坏语句)
        for key, value in self.pragmas.items():
            await self._conn.execute(
                f"PRAGMA {key}="
                + (f"'{_quote_sql_literal(value)}'" if isinstance(value, str)
                   else str(value))
            )

        # 2. 加载 sqlite-vec(必须在 worker 线程执行)
        if self.load_vec:
            await self._conn._execute(_setup_sqlite_vec, self._conn._conn)

        # 3. 应用 schema migrations(也在 worker 线程执行)
        await self._conn._execute(
            _apply_migrations_sync,
            self._conn._conn,
            vec_already_loaded=self.load_vec,
        )

    async def close(self) -> None:
        """关闭连接(幂等)."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def get_user_version(self) -> int:
        """当前 schema 版本."""
        cursor = await self.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        return int(row[0])

    async def list_tables(self) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [row[0] for row in await cursor.fetchall()]

    async def list_indexes(self) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [row[0] for row in await cursor.fetchall()]

    async def list_triggers(self) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
        )
        return [row[0] for row in await cursor.fetchall()]

    @asynccontextmanager
    async def transaction(self):
        """显式事务上下文.

        退出时:
        - 正常 → COMMIT
        - 异常 → ROLLBACK 并重新抛出
        """
        conn = self.conn
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


__all__ = [
    "SQLiteEngine",
    "setup_sqlite_vec",
    "apply_migrations_sync",
]


# Public aliases (no leading underscore) for ad-hoc usage
async def setup_sqlite_vec(conn: aiosqlite.Connection) -> None:
    """Utility: 在 aiosqlite 连接上加载 sqlite-vec."""
    await conn._execute(_setup_sqlite_vec, conn._conn)


async def apply_migrations_sync(
    conn: aiosqlite.Connection,
    *,
    vec_already_loaded: bool = True,
) -> int:
    """Utility: 在 aiosqlite 连接上应用 schema migrations."""
    return await conn._execute(
        _apply_migrations_sync, conn._conn, vec_already_loaded=vec_already_loaded
    )
