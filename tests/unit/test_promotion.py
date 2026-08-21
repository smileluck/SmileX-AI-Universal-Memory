"""Unit tests for PromotionManager (L2.8).

验证(主文档 §7.3 + 模块文档 03 §3.3):
- needs_promotion: 超过 800 tokens 阈值判定
- put 自动检查钩子: 超阈值自动晋升,未超保留 L0
- promote 显式晋升: temporal_fragments(layer='L1')+ 向量写入 + L0 移除
- check_session: 批量扫描晋升
- 晋升后可被 KNN 检索到(HashEmbedder 确定性: 相同文本距离为 0)
"""

from __future__ import annotations

import pytest

from smilex.memory.lifecycle import (
    L0_PROMOTION_THRESHOLD,
    HashEmbedder,
    L0WorkingMemory,
    PromotionManager,
    TokenCounter,
)
from smilex.memory.models import FuzzyMemory, MemoryLayer, MemoryScope
from smilex.memory.storage.sqlite_engine import SQLiteEngine
from smilex.memory.storage.vector_store import VectorStore

# 远超 800 tokens 的文本(任意编码下都成立)
LONG_TEXT = "lorem ipsum dolor sit amet " * 1000
SHORT_TEXT = "hello world"

assert TokenCounter().count(LONG_TEXT) > L0_PROMOTION_THRESHOLD
assert TokenCounter().count(SHORT_TEXT) <= L0_PROMOTION_THRESHOLD


# ---------- Fixtures ----------


@pytest.fixture
async def env(tmp_path):
    """引擎(load_vec)+ VectorStore + L0 + PromotionManager."""
    eng = SQLiteEngine(tmp_path / "promo_test.db", load_vec=True)
    await eng.initialize()
    store = VectorStore(embedder=HashEmbedder())
    l0 = L0WorkingMemory()
    promo = PromotionManager(l0, vector_store=store)
    yield promo, l0, store, eng
    await eng.close()


def _make_memory(content: str, **kwargs) -> FuzzyMemory:
    return FuzzyMemory(content=content, scope=MemoryScope.GLOBAL, **kwargs)


# ---------- needs_promotion ----------


def test_needs_promotion_threshold():
    promo = PromotionManager(L0WorkingMemory())
    assert promo.needs_promotion(_make_memory(LONG_TEXT)) is True
    assert promo.needs_promotion(_make_memory(SHORT_TEXT)) is False
    assert promo.needs_promotion(_make_memory(None)) is False
    assert promo.threshold == 800


# ---------- put 自动检查钩子 ----------


@pytest.mark.asyncio
async def test_put_under_threshold_stays_in_l0(env):
    """未超阈值: put 后留在 L0,不写存储."""
    promo, l0, _, eng = env
    async with eng.transaction() as conn:
        mid = await promo.put(conn, "s1", _make_memory(SHORT_TEXT))
    assert l0.get("s1", mid) is not None

    cursor = await eng.conn.execute("SELECT COUNT(*) AS n FROM temporal_fragments")
    assert (await cursor.fetchone())["n"] == 0


@pytest.mark.asyncio
async def test_put_over_threshold_auto_promotes(env):
    """超阈值: put 自动晋升 — 写 temporal_fragments(L1)+ 向量,从 L0 移除."""
    promo, l0, _, eng = env
    mem = _make_memory(LONG_TEXT, importance=0.8, entities=["e1"])
    async with eng.transaction() as conn:
        mid = await promo.put(conn, "s1", mem)

    # L0 已移除
    assert l0.get("s1", mid) is None

    # temporal_fragments 有 L1 行
    cursor = await eng.conn.execute(
        "SELECT * FROM temporal_fragments WHERE id = ?", (mid,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["layer"] == "L1"
    assert row["content"] == LONG_TEXT
    assert row["scope"] == "global"
    assert row["importance"] == 0.8
    assert row["entities"] == '["e1"]'

    # vector_links 有 fragment 关联
    cursor = await eng.conn.execute(
        "SELECT * FROM vector_links WHERE fragment_id = ?", (mid,)
    )
    assert await cursor.fetchone() is not None

    # memory 对象 layer 已更新
    assert mem.layer == MemoryLayer.L1_SHORT


@pytest.mark.asyncio
async def test_put_auto_promote_disabled(env):
    """auto_promote=False 时超阈值也只写 L0."""
    promo, l0, _, eng = env
    async with eng.transaction() as conn:
        mid = await promo.put(conn, "s1", _make_memory(LONG_TEXT), auto_promote=False)
    assert l0.get("s1", mid) is not None


# ---------- 显式 promote ----------


@pytest.mark.asyncio
async def test_promote_explicit(env):
    """显式 promote: 手动把 L0 条目晋升到 L1."""
    promo, l0, _, eng = env
    async with eng.transaction() as conn:
        mid = await promo.put(conn, "s1", _make_memory(SHORT_TEXT))
        assert await promo.promote(conn, "s1", mid) is True
    assert l0.get("s1", mid) is None

    cursor = await eng.conn.execute(
        "SELECT layer FROM temporal_fragments WHERE id = ?", (mid,)
    )
    assert (await cursor.fetchone())["layer"] == "L1"


@pytest.mark.asyncio
async def test_promote_missing_returns_false(env):
    """promote 不存在的记忆 → False,不报错."""
    promo, _, _, eng = env
    async with eng.transaction() as conn:
        assert await promo.promote(conn, "s1", "no_such_id") is False


@pytest.mark.asyncio
async def test_promote_empty_content_raises(env):
    """content 为空的记忆无法晋升(无法检索),raise ValueError."""
    promo, l0, _, eng = env
    async with eng.transaction() as conn:
        mid = await promo.put(conn, "s1", _make_memory(None))
        with pytest.raises(ValueError, match="content 为空"):
            await promo.promote(conn, "s1", mid)


@pytest.mark.asyncio
async def test_promote_project_scope_requires_scope_id(env):
    """PROJECT scope 缺 scope_id 时 raise(与 storage_engine 约定一致)."""
    promo, l0, _, eng = env
    async with eng.transaction() as conn:
        mid = await promo.put(conn, "s1", FuzzyMemory(content=SHORT_TEXT))
        with pytest.raises(ValueError, match="scope_id"):
            await promo.promote(conn, "s1", mid)


# ---------- check_session ----------


@pytest.mark.asyncio
async def test_check_session_promotes_all_over_threshold(env):
    """check_session 扫描: 超阈值全部晋升,未超保留."""
    promo, l0, _, eng = env
    async with eng.transaction() as conn:
        over1 = await promo.put(conn, "s1", _make_memory(LONG_TEXT), auto_promote=False)
        over2 = await promo.put(conn, "s1", _make_memory(LONG_TEXT + "x"), auto_promote=False)
        under = await promo.put(conn, "s1", _make_memory(SHORT_TEXT))
        promoted = await promo.check_session(conn, "s1")

    assert set(promoted) == {over1, over2}
    assert l0.get("s1", over1) is None
    assert l0.get("s1", over2) is None
    assert l0.get("s1", under) is not None


# ---------- 晋升后可检索(集成) ----------


@pytest.mark.asyncio
async def test_promoted_memory_searchable_via_knn(env):
    """晋升后的记忆可通过向量 KNN 检索(HashEmbedder: 相同文本距离为 0)."""
    promo, _, store, eng = env
    async with eng.transaction() as conn:
        mid = await promo.put(conn, "s1", _make_memory(LONG_TEXT))

    hits = await store.knn_search(eng.conn, LONG_TEXT, k=1)
    assert len(hits) == 1
    assert hits[0].memory_id == mid
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6)
