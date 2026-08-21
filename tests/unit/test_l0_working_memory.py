"""Unit tests for L0WorkingMemory (L2.1, cachebox LRUCache per 决策 D4).

验证:
- put/get/remove/list/clear_session 基本 API
- 会话隔离(不同 session_id 互不影响)
- 覆盖写入(同 memory.id)
- LRU 淘汰(maxsize 满时淘汰最久未用会话)
- export/import_session 快照集成
"""

from __future__ import annotations

import pytest

from smilex.memory.lifecycle import L0WorkingMemory
from smilex.memory.models import FuzzyMemory, MemoryLayer


@pytest.fixture
def l0():
    return L0WorkingMemory()


def _mem(content: str) -> FuzzyMemory:
    return FuzzyMemory(content=content)


# ---------- 基本 API ----------

def test_put_and_get(l0):
    m = _mem("用户偏好深色主题")
    mid = l0.put("sess_1", m)
    assert mid == m.id
    got = l0.get("sess_1", mid)
    assert got is m
    assert got.layer == MemoryLayer.L0_WORKING


def test_put_overwrites_same_id(l0):
    m = _mem("v1")
    l0.put("sess_1", m)
    m.content = "v2"
    l0.put("sess_1", m)
    assert len(l0.list("sess_1")) == 1
    assert l0.get("sess_1", m.id).content == "v2"


def test_get_missing_returns_none(l0):
    assert l0.get("no_such_session", "no_such_id") is None
    l0.put("sess_1", _mem("x"))
    assert l0.get("sess_1", "no_such_id") is None


def test_list_session(l0):
    l0.put("sess_1", _mem("a"))
    l0.put("sess_1", _mem("b"))
    assert len(l0.list("sess_1")) == 2
    assert l0.list("no_such_session") == []


def test_remove(l0):
    m = _mem("to be removed")
    l0.put("sess_1", m)
    assert l0.remove("sess_1", m.id) is True
    assert l0.get("sess_1", m.id) is None
    # 再删一次返回 False
    assert l0.remove("sess_1", m.id) is False


def test_remove_last_entry_clears_session(l0):
    m = _mem("only entry")
    l0.put("sess_1", m)
    l0.remove("sess_1", m.id)
    assert l0.session_count == 0
    assert l0.session_ids() == []


def test_session_isolation(l0):
    l0.put("sess_1", _mem("session 1 data"))
    l0.put("sess_2", _mem("session 2 data"))
    assert len(l0.list("sess_1")) == 1
    assert len(l0.list("sess_2")) == 1
    l0.clear_session("sess_1")
    assert l0.list("sess_1") == []
    assert len(l0.list("sess_2")) == 1
    assert l0.session_count == 1


def test_clear_all(l0):
    l0.put("sess_1", _mem("a"))
    l0.put("sess_2", _mem("b"))
    l0.clear()
    assert l0.session_count == 0


def test_clear_session_missing_is_noop(l0):
    l0.clear_session("no_such_session")  # 不应 raise


# ---------- LRU 淘汰 ----------

def test_lru_eviction():
    """maxsize 满时淘汰最久未访问的会话."""
    l0 = L0WorkingMemory(maxsize=3)
    for i in range(3):
        l0.put(f"sess_{i}", _mem(f"data {i}"))
    # 访问 sess_0 使其成为最近使用
    l0.list("sess_0")
    # 插入第 4 个会话 → 淘汰 sess_1(最久未用)
    l0.put("sess_3", _mem("data 3"))
    assert l0.session_count == 3
    assert "sess_1" not in l0.session_ids()
    assert "sess_0" in l0.session_ids()


# ---------- 快照集成 ----------

def test_export_import_roundtrip(l0):
    l0.put("sess_1", _mem("a"))
    l0.put("sess_1", _mem("b"))
    exported = l0.export_session("sess_1")
    assert len(exported) == 2

    l0_new = L0WorkingMemory()
    count = l0_new.import_session("sess_1", exported)
    assert count == 2
    assert {m.id for m in l0_new.list("sess_1")} == {m.id for m in exported}


def test_export_missing_session_returns_empty(l0):
    assert l0.export_session("no_such_session") == []
