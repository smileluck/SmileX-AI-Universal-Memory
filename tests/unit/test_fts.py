"""FTS5 BM25 关键词通道测试 — schema 013 + ContextBuilder 双通道融合."""

from __future__ import annotations

from pathlib import Path

import pytest

from smilex.memory.lifecycle import ContextBuilder, HashEmbedder, L0WorkingMemory
from smilex.memory.storage.queries.fts import bm25_fragment_search, build_fts_query
from smilex.memory.storage.sqlite_engine import SQLiteEngine
from smilex.memory.storage.vector_store import VectorStore

NOW = "2025-01-10T00:00:00.000000Z"


async def _insert_fragment(conn, fid: str, content: str, scope: str = "global") -> None:
    await conn.execute(
        "INSERT INTO temporal_fragments(id, fragment_id, time_start, content, "
        "scope, layer, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'L1', ?, ?)",
        (fid, f"frag_{fid}", NOW, content, scope, NOW, NOW),
    )
    await conn.commit()


@pytest.fixture
async def engine(tmp_path: Path) -> SQLiteEngine:
    eng = SQLiteEngine(tmp_path / "fts.db")
    await eng.initialize()
    try:
        yield eng
    finally:
        await eng.close()


class TestBuildFtsQuery:
    def test_basic_or_join(self) -> None:
        expr = build_fts_query("Where did I volunteer at the shelter dinner?")
        assert expr is not None
        assert " OR " in expr

    def test_short_words_dropped(self) -> None:
        assert build_fts_query("to be or") is None  # 全 <3 字符

    def test_long_query_truncated(self) -> None:
        expr = build_fts_query(" ".join(f"word{i}" for i in range(50)))
        assert expr is not None
        assert len(expr.split(" OR ")) <= 16

    def test_punctuation_stripped(self) -> None:
        # \w 分词天然去除引号/标点,杜绝 FTS 语法注入
        assert build_fts_query('say "hello" world') == '"say" OR "hello" OR "world"'


class TestTriggers:
    async def test_insert_sync(self, engine: SQLiteEngine) -> None:
        await _insert_fragment(engine.conn, "f1", "user volunteered at animal shelter dinner")
        ids = await bm25_fragment_search(engine.conn, "animal shelter dinner")
        assert ids == ["f1"]

    async def test_update_sync(self, engine: SQLiteEngine) -> None:
        await _insert_fragment(engine.conn, "f1", "user likes apples")
        await engine.conn.execute(
            "UPDATE temporal_fragments SET content = 'user likes bananas' WHERE id = 'f1'"
        )
        await engine.conn.commit()
        assert await bm25_fragment_search(engine.conn, "bananas") == ["f1"]
        assert await bm25_fragment_search(engine.conn, "apples") == []

    async def test_delete_sync(self, engine: SQLiteEngine) -> None:
        await _insert_fragment(engine.conn, "f1", "user likes apples")
        await engine.conn.execute("DELETE FROM temporal_fragments WHERE id = 'f1'")
        await engine.conn.commit()
        assert await bm25_fragment_search(engine.conn, "apples") == []

    async def test_chinese_content(self, engine: SQLiteEngine) -> None:
        await _insert_fragment(engine.conn, "f1", "用户在动物收容所做志愿者")
        await _insert_fragment(engine.conn, "f2", "用户喜欢打网球")
        ids = await bm25_fragment_search(engine.conn, "动物收容所 志愿者")
        assert "f1" in ids
        assert "f2" not in ids

    async def test_scope_filter(self, engine: SQLiteEngine) -> None:
        from smilex.memory.models import ScopeFilter

        await _insert_fragment(engine.conn, "f1", "secret project keyword", scope="project:p1")
        await _insert_fragment(engine.conn, "f2", "public keyword", scope="global")
        # 只查 project:p1(ScopeFilter 默认 include_global=True,需显式关闭)→ 仅 f1
        assert await bm25_fragment_search(
            engine.conn, "keyword",
            scope_filter=ScopeFilter(include_global=False, include_project="p1"),
        ) == ["f1"]
        # 只查 global → 仅 f2
        assert await bm25_fragment_search(
            engine.conn, "keyword", scope_filter=ScopeFilter(include_global=True)
        ) == ["f2"]


class TestContextBuilderFusion:
    async def test_keyword_only_hit_enters_context(self, engine: SQLiteEngine) -> None:
        """纯关键词命中(向量检索不到)也应进入上下文 — 修复 LongMemEval #9 型漏检."""
        store = VectorStore(embedder=HashEmbedder())
        conn = engine.conn
        # f1: 含目标关键词但向量为完全不同的语义(unrelated embedding 文本)
        await _insert_fragment(conn, "f1", "volunteered at the animal shelter fundraising dinner")
        # f2-f6: 干扰项,向量语义接近 query 词面
        for i in range(2, 7):
            await _insert_fragment(conn, f"f{i}", f"volunteer work summary number {i}")
        async with engine.transaction() as tconn:
            for i in range(1, 7):
                await store.add_text(
                    tconn,
                    f"filler vector text {i}",
                    fragment_id=f"f{i}",
                )
        builder = ContextBuilder(l0=L0WorkingMemory(), vector_store=store)
        ctx = await builder.build_context(
            conn, session_id=None,
            query_text="When did I volunteer at the local animal shelter's fundraising dinner?",
            token_budget=4000, l1_top_k=5, l2_top_k=5,
        )
        assert "animal shelter" in ctx.text

    async def test_scores_within_unit_range(self, engine: SQLiteEngine) -> None:
        store = VectorStore(embedder=HashEmbedder())
        for i in range(10):
            await _insert_fragment(engine.conn, f"f{i}", f"memory about topic {i}")
        async with engine.transaction() as tconn:
            for i in range(10):
                await store.add_text(tconn, f"memory about topic {i}", fragment_id=f"f{i}")
        builder = ContextBuilder(vector_store=store)
        ctx = await builder.build_context(
            engine.conn, session_id=None, query_text="memory about topic",
            token_budget=4000,
        )
        assert ctx.sources
        assert all(0.0 <= s.score <= 1.0 for s in ctx.sources)
