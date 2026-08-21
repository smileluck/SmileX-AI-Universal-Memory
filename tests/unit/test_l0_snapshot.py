"""Unit tests for L0SnapshotStore (L2.2, memory_l0_snapshot 表).

验证:
- save/load 往返(msgpack 序列化)
- 覆盖已有快照(INSERT OR REPLACE)
- delete_snapshot
- expires_at 过期快照: load 返回 None 并删除,cleanup_expired 清理
- 与 L0WorkingMemory 的崩溃恢复集成(export → save → load → import)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from smilex.memory.lifecycle import L0SnapshotStore, L0WorkingMemory
from smilex.memory.models import FuzzyMemory, TimeRange
from smilex.memory.storage.sqlite_engine import SQLiteEngine


@pytest.fixture
async def store(tmp_path):
    eng = SQLiteEngine(tmp_path / "test.db")
    await eng.initialize()
    yield L0SnapshotStore(eng)
    await eng.close()


def _mem(content: str) -> FuzzyMemory:
    return FuzzyMemory(content=content)


# ---------- save / load ----------

async def test_save_and_load_roundtrip(store):
    memories = [
        FuzzyMemory(content="hello", entities=["Alice"]),
        FuzzyMemory(
            content="世界",
            time_range=TimeRange(exact=datetime.now(UTC)),
        ),
    ]
    await store.save_snapshot("sess_1", memories)
    loaded = await store.load_snapshot("sess_1")
    assert loaded is not None
    assert len(loaded) == 2
    assert {m.id for m in loaded} == {m.id for m in memories}
    assert loaded[0].content == memories[0].content
    assert loaded[1].time_range.exact == memories[1].time_range.exact


async def test_load_missing_returns_none(store):
    assert await store.load_snapshot("no_such_session") is None


async def test_save_overwrites_existing(store):
    await store.save_snapshot("sess_1", [_mem("v1")])
    await store.save_snapshot("sess_1", [_mem("v2"), _mem("v3")])
    loaded = await store.load_snapshot("sess_1")
    assert len(loaded) == 2
    assert "v1" not in {m.content for m in loaded}


async def test_save_empty_session(store):
    await store.save_snapshot("sess_empty", [])
    loaded = await store.load_snapshot("sess_empty")
    assert loaded == []


# ---------- delete ----------

async def test_delete_snapshot(store):
    await store.save_snapshot("sess_1", [_mem("x")])
    assert await store.delete_snapshot("sess_1") is True
    assert await store.load_snapshot("sess_1") is None
    # 再删一次返回 False
    assert await store.delete_snapshot("sess_1") is False


async def test_list_snapshots(store):
    await store.save_snapshot("sess_a", [_mem("a")])
    await store.save_snapshot("sess_b", [_mem("b")])
    assert set(await store.list_snapshots()) == {"sess_a", "sess_b"}


# ---------- 过期处理 ----------

async def test_expired_snapshot_load_returns_none_and_deletes(store):
    past = datetime.now(UTC) - timedelta(seconds=1)
    await store.save_snapshot("sess_exp", [_mem("old")], expires_at=past)
    assert await store.load_snapshot("sess_exp") is None
    # 过期行已被顺带删除
    assert "sess_exp" not in await store.list_snapshots()


async def test_future_expiry_loads_normally(store):
    future = datetime.now(UTC) + timedelta(hours=1)
    await store.save_snapshot("sess_ok", [_mem("fresh")], expires_at=future)
    loaded = await store.load_snapshot("sess_ok")
    assert loaded is not None
    assert loaded[0].content == "fresh"


async def test_cleanup_expired(store):
    past = datetime.now(UTC) - timedelta(seconds=1)
    future = datetime.now(UTC) + timedelta(hours=1)
    await store.save_snapshot("sess_exp1", [_mem("a")], expires_at=past)
    await store.save_snapshot("sess_exp2", [_mem("b")], expires_at=past)
    await store.save_snapshot("sess_ok", [_mem("c")], expires_at=future)
    await store.save_snapshot("sess_forever", [_mem("d")])
    removed = await store.cleanup_expired()
    assert removed == 2
    assert set(await store.list_snapshots()) == {"sess_ok", "sess_forever"}


# ---------- 与 L0WorkingMemory 集成(崩溃恢复) ----------

async def test_crash_recovery_integration(store):
    """cachebox 丢失(新实例)时从快照恢复."""
    l0 = L0WorkingMemory()
    l0.put("sess_1", _mem("important context"))
    await store.save_snapshot("sess_1", l0.export_session("sess_1"))

    # 模拟进程重启: 新的 L0 实例,缓存为空
    l0_restarted = L0WorkingMemory()
    assert l0_restarted.session_count == 0
    memories = await store.load_snapshot("sess_1")
    count = l0_restarted.import_session("sess_1", memories)
    assert count == 1
    assert l0_restarted.list("sess_1")[0].content == "important context"
