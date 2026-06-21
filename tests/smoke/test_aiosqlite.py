"""Smoke test: aiosqlite async connection + PRAGMA + CRUD."""

import aiosqlite
import pytest


@pytest.mark.asyncio
async def test_aiosqlite_pragma_wal():
    async with aiosqlite.connect(":memory:") as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        cursor = await db.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        # :memory: 通常返回 memory,WAL 在文件库才生效,这里仅验证 PRAGMA 可执行
        assert row is not None


@pytest.mark.asyncio
async def test_aiosqlite_crud():
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        await db.execute("INSERT INTO t (name) VALUES (?)", ("alice",))
        await db.execute("INSERT INTO t (name) VALUES (?)", ("bob",))
        await db.commit()

        cursor = await db.execute("SELECT count(*) FROM t")
        row = await cursor.fetchone()
        assert row[0] == 2

        cursor = await db.execute("SELECT name FROM t WHERE id=?", (1,))
        row = await cursor.fetchone()
        assert row[0] == "alice"
