"""Unit tests for ContextBuilder (L2.7).

验证:
- 多路召回聚合: L0 工作记忆 + L1 向量 KNN + L2 混合检索(RRF)
- 去重: 同一 memory 来自多路召回只保留一份(L0 优先)
- 排序: L0 按 importance/recency,L1 按向量距离,L2 按 RRF score
- Token Budget 贪心裁剪(默认 4000 × 0.7 安全边际),来源标记 [L0]/[L1]/[L2]

注意: HashEmbedder 无语义相似度,KNN 相关断言只用确定性行为
(相同文本距离为 0 / 唯一向量必命中),不断言语义相关性.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from smilex.memory.lifecycle import (
    ContextBuilder,
    HashEmbedder,
    L0WorkingMemory,
    TokenCounter,
)
from smilex.memory.models import FuzzyMemory, MemoryScope
from smilex.memory.storage.sqlite_engine import SQLiteEngine
from smilex.memory.storage.vector_store import VectorStore
from smilex.utils.timeutil import now_utc

# ---------- Fixtures ----------


@pytest.fixture
async def env(tmp_path):
    """引擎(load_vec)+ VectorStore(HashEmbedder)+ L0 + ContextBuilder + 测试数据.

    数据:
    - entities: e1 alice / e2 bob
    - triples: t1 (e1 knows e2, 2025-01) / t2 (e1 reviewed PR-1, 2025-03)
    - fragments: f1 "alice works at CompanyX"(含向量)
    """
    eng = SQLiteEngine(tmp_path / "ctx_test.db", load_vec=True)
    await eng.initialize()
    conn = eng.conn

    for eid, entity_id, name in [
        ("e1", "person:alice", "Alice"),
        ("e2", "person:bob", "Bob"),
    ]:
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
            "VALUES (?, ?, 'person', ?, 'global', '2025-01-01T00:00:00.000000Z')",
            (eid, entity_id, name),
        )
    for tid, tri_id, pred, oid, oval, vf in [
        ("t1", "tri_t1", "knows", "e2", None, "2025-01-15T00:00:00.000000Z"),
        ("t2", "tri_t2", "reviewed", None, "PR-1", "2025-03-01T00:00:00.000000Z"),
    ]:
        await conn.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, object_id, "
            "object_value, scope, valid_from, relation_type) "
            "VALUES (?, ?, 'e1', ?, ?, ?, 'global', ?, 'semantic')",
            (tid, tri_id, pred, oid, oval, vf),
        )
    await conn.execute(
        "INSERT INTO temporal_fragments(id, fragment_id, time_start, content, scope, "
        "layer, created_at, updated_at) "
        "VALUES ('f1', 'frag_f1', '2025-01-10T00:00:00.000000Z', "
        "'alice works at CompanyX', 'global', 'L1', "
        "'2025-01-10T00:00:00.000000Z', '2025-01-10T00:00:00.000000Z')"
    )
    await conn.commit()

    store = VectorStore(embedder=HashEmbedder())
    async with eng.transaction() as tconn:
        await store.add_text(tconn, "alice works at CompanyX", fragment_id="f1")

    l0 = L0WorkingMemory()
    builder = ContextBuilder(l0=l0, vector_store=store)
    yield builder, l0, store, eng
    await eng.close()


# ---------- 空输入 ----------


@pytest.mark.asyncio
async def test_build_context_empty(env):
    """无任何召回源 → 空上下文."""
    builder, _, _, eng = env
    ctx = await builder.build_context(eng.conn)
    assert ctx.text == ""
    assert ctx.sources == []
    assert ctx.total_tokens == 0
    assert ctx.truncated is False


# ---------- L0 工作记忆 ----------


@pytest.mark.asyncio
async def test_l0_full_load_with_marker(env):
    """L0 全量加载,每行带 [L0] 来源标记."""
    builder, l0, _, eng = env
    l0.put("s1", FuzzyMemory(content="用户偏好深色主题", scope=MemoryScope.GLOBAL))
    l0.put("s1", FuzzyMemory(content="当前在调试登录页", scope=MemoryScope.GLOBAL))

    ctx = await builder.build_context(eng.conn, session_id="s1")
    lines = ctx.text.split("\n")
    assert len(lines) == 2
    assert all(line.startswith("[L0] ") for line in lines)
    assert any("深色主题" in line for line in lines)
    assert any("登录页" in line for line in lines)
    assert ctx.total_tokens > 0
    assert ctx.total_tokens <= ctx.budget


@pytest.mark.asyncio
async def test_l0_sorted_by_importance_then_recency(env):
    """L0 层内按 importance 降序,同 importance 按 updated_at 降序(后写入在前)."""
    builder, l0, _, eng = env
    id1 = l0.put(
        "s1", FuzzyMemory(content="低重要性先写", importance=0.5, scope=MemoryScope.GLOBAL)
    )
    id2 = l0.put(
        "s1", FuzzyMemory(content="低重要性后写", importance=0.5, scope=MemoryScope.GLOBAL)
    )
    id3 = l0.put(
        "s1", FuzzyMemory(content="高重要性", importance=0.9, scope=MemoryScope.GLOBAL)
    )
    # 显式控制时间戳: 低精度时钟(Windows py<3.13 约 15.6ms tick)下连续 put 会同 tick,
    # 同 updated_at 时排序结果不稳定,测试必须固定 recency 顺序
    base = now_utc()
    l0.get("s1", id1).updated_at = base
    l0.get("s1", id2).updated_at = base + timedelta(seconds=1)
    l0.get("s1", id3).updated_at = base + timedelta(seconds=2)

    ctx = await builder.build_context(eng.conn, session_id="s1")
    lines = ctx.text.split("\n")
    assert "高重要性" in lines[0]
    # 同 importance 时后写入(updated_at 更新)在前
    assert "低重要性后写" in lines[1]
    assert "低重要性先写" in lines[2]


# ---------- L1 向量 KNN ----------


@pytest.mark.asyncio
async def test_l1_knn_hit_with_marker(env):
    """L1 KNN: 相同文本确定性命中(HashEmbedder 距离为 0),带 [L1] 标记."""
    builder, _, _, eng = env
    ctx = await builder.build_context(eng.conn, query_text="alice works at CompanyX")
    assert len(ctx.sources) == 1
    src = ctx.sources[0]
    assert src.layer == "L1"
    assert src.memory_id == "f1"
    assert src.score == pytest.approx(1.0)  # 1/(1+0)
    assert ctx.text == "[L1] alice works at CompanyX"


# ---------- L2 混合检索 ----------


@pytest.mark.asyncio
async def test_l2_hybrid_with_marker(env):
    """L2 混合检索: entity_id 触发 graph 策略,triples 渲染为文本,带 [L2] 标记."""
    builder, _, _, eng = env
    ctx = await builder.build_context(eng.conn, entity_id="e1")
    layers = {s.layer for s in ctx.sources}
    assert layers == {"L2"}
    ids = {s.memory_id for s in ctx.sources}
    assert ids == {"t1", "t2"}
    assert "[L2] e1 knows e2" in ctx.text
    assert "[L2] e1 reviewed PR-1" in ctx.text


# ---------- 多路聚合 + 去重 ----------


@pytest.mark.asyncio
async def test_multi_recall_aggregation(env):
    """L0 + L1 + L2 三路同时召回,按 L0 > L1 > L2 排序."""
    builder, l0, _, eng = env
    l0.put("s1", FuzzyMemory(content="工作记忆条目", scope=MemoryScope.GLOBAL))

    ctx = await builder.build_context(
        eng.conn,
        session_id="s1",
        query_text="alice works at CompanyX",
        entity_id="e1",
    )
    layers = [s.layer for s in ctx.sources]
    # L0 最前,其后 L1,最后 L2
    assert layers == ["L0", "L1", "L2", "L2"]
    assert ctx.total_tokens <= ctx.budget


@pytest.mark.asyncio
async def test_dedup_across_recall_paths(env):
    """同一 memory 来自 L0 和 L1 两路召回时只保留一份(L0 优先)."""
    builder, l0, store, eng = env
    # f1 同时存在于 L0(伪造同 id)与 L1(向量 KNN)
    l0.put("s1", FuzzyMemory(
        id="f1", content="alice works at CompanyX", scope=MemoryScope.GLOBAL,
    ))

    ctx = await builder.build_context(
        eng.conn, session_id="s1", query_text="alice works at CompanyX"
    )
    f1_sources = [s for s in ctx.sources if s.memory_id == "f1"]
    assert len(f1_sources) == 1
    assert f1_sources[0].layer == "L0"
    assert ctx.text.count("alice works at CompanyX") == 1


# ---------- Token Budget 裁剪 ----------


@pytest.mark.asyncio
async def test_budget_trimming(env):
    """预算不足时裁剪 L0 条目: total_tokens 不超预算,truncated=True."""
    builder, l0, _, eng = env
    for i in range(5):
        l0.put("s1", FuzzyMemory(
            content=f"这是第 {i} 条工作记忆,包含足够多的文本以消耗 token 预算",
            scope=MemoryScope.GLOBAL,
        ))

    counter = TokenCounter()
    single_line_tokens = counter.count("[L0] 这是第 0 条工作记忆,包含足够多的文本以消耗 token 预算")
    # 预算只够放 2 条(考虑 0.7 安全边际: token_budget * 0.7 ≈ 2 条)
    token_budget = int(single_line_tokens * 2 / 0.7) + 1

    ctx = await builder.build_context(eng.conn, session_id="s1", token_budget=token_budget)
    assert ctx.truncated is True
    assert len(ctx.sources) == 2
    assert ctx.total_tokens <= ctx.budget
    assert ctx.budget == int(token_budget * 0.7)


@pytest.mark.asyncio
async def test_layer_priority_under_tight_budget(env):
    """预算紧张时 L0 优先保留,L1/L2 被裁剪."""
    builder, l0, _, eng = env
    l0.put("s1", FuzzyMemory(content="重要工作记忆", scope=MemoryScope.GLOBAL))

    counter = TokenCounter()
    l0_line_tokens = counter.count("[L0] 重要工作记忆")
    token_budget = int(l0_line_tokens / 0.7) + 1

    ctx = await builder.build_context(
        eng.conn,
        session_id="s1",
        query_text="alice works at CompanyX",
        entity_id="e1",
        token_budget=token_budget,
    )
    assert [s.layer for s in ctx.sources] == ["L0"]
    assert ctx.truncated is True
    assert ctx.total_tokens <= ctx.budget


@pytest.mark.asyncio
async def test_default_budget(env):
    """默认 budget = 4000 × 0.7 = 2800(§7.4 + 模块文档 03 §3.1)."""
    builder, _, _, eng = env
    ctx = await builder.build_context(eng.conn, entity_id="e1")
    assert ctx.budget == 2800
