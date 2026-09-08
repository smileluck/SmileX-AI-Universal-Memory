"""Unit tests for SQLCipher 加密管道(§15.4,实验性).

真实加解密 roundtrip 需 pysqlcipher3 + 系统 libsqlcipher(importorskip 跳过);
无依赖环境下覆盖: 引号转义、cipher_version fail-fast、shim 探测、内存库拒绝。
"""

from __future__ import annotations

import pytest

from smilex.memory.storage.sqlite_engine import SQLiteEngine, _quote_sql_literal
from smilex.memory.storage.storage_engine import StorageEngine


def test_quote_sql_literal():
    assert _quote_sql_literal("plain") == "plain"
    assert _quote_sql_literal("it's") == "it''s"
    assert _quote_sql_literal("a'; DROP TABLE x; --") == "a''; DROP TABLE x; --"
    assert _quote_sql_literal("") == ""


async def test_memory_db_rejects_encryption():
    with pytest.raises(ValueError, match="内存库不支持加密"):
        engine = SQLiteEngine(":memory:", encryption_key="k", load_vec=False)
        await engine.initialize()


async def test_fail_fast_without_sqlcipher_build(tmp_path):
    """香草 sqlite3 下 PRAGMA key 被静默忽略 → cipher_version 校验必须中止."""
    engine = SQLiteEngine(
        tmp_path / "plain.db", encryption_key="k", load_vec=False
    )
    with pytest.raises(RuntimeError, match="SQLCipher 构建"):
        await engine.initialize()
    await engine.close()


async def test_plaintext_engine_unaffected(tmp_path):
    """未配 key 的引擎照常初始化(回归: cipher 检查不误伤明文路径)."""
    engine = StorageEngine(tmp_path / "plain2.db", load_vec=False)
    await engine.initialize()
    assert engine.is_initialized
    await engine.close()


async def test_pragma_string_values_quoted(tmp_path, monkeypatch):
    """pragma 字符串值走单引号包裹转义(防引号/分号破坏语句)."""
    import aiosqlite

    executed: list[str] = []
    real_connect = aiosqlite.connect

    async def recording_connect(path, **kw):
        conn = await real_connect(path, **kw)
        orig_execute = conn.execute

        async def wrapped(sql, params=None):
            executed.append(sql)
            return await orig_execute(sql, params)

        conn.execute = wrapped
        return conn

    monkeypatch.setattr(aiosqlite, "connect", recording_connect)
    engine = SQLiteEngine(
        tmp_path / "quote.db",
        pragmas={"busy_timeout": 5000, "custom_text": "v'x; DROP--"},
        load_vec=False,
    )
    await engine.initialize()
    await engine.close()
    assert any("PRAGMA custom_text='v''x; DROP--'" in sql for sql in executed), executed


def test_shim_probe_without_dependency():
    """未装 pysqlcipher3 时 sqlcipher_available() 返回 False 且不抛."""
    from smilex.memory.storage import sqlcipher

    # 测试环境未装 encryption extra → False;装了则 True(两种都合法,不抛即可)
    assert isinstance(sqlcipher.sqlcipher_available(), bool)


async def test_real_roundtrip_if_available(tmp_path):
    """pysqlcipher3 在场时的端到端加解密(无依赖环境跳过)."""
    pytest.importorskip("pysqlcipher3")
    from smilex.memory.storage.sqlcipher import install_shim

    assert install_shim()
    db = tmp_path / "enc.db"
    engine = SQLiteEngine(db, encryption_key="s3cret", load_vec=False)
    await engine.initialize()
    await engine.conn.execute("CREATE TABLE t(x TEXT)")
    await engine.conn.execute("INSERT INTO t VALUES ('机密')")
    await engine.conn.commit()
    await engine.close()

    # 无 key 打开应读不到明文 schema(文件已加密)
    import sqlite3

    raw = sqlite3.connect(db)
    try:
        row = raw.execute("SELECT count(*) FROM sqlite_master").fetchone()
        assert row[0] == 0  # 香草 sqlite 读加密文件得到空,不返回明文 schema
    except sqlite3.DatabaseError:
        pass  # 部分构建直接报 file is not a database,同样通过
    finally:
        raw.close()

    # 带 key 重开可读回
    engine2 = SQLiteEngine(db, encryption_key="s3cret", load_vec=False)
    await engine2.initialize()
    cur = await engine2.conn.execute("SELECT x FROM t")
    assert (await cur.fetchone())[0] == "机密"
    await engine2.close()
