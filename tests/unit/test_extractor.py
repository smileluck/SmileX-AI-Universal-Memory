"""Unit tests for FactExtractor(写入时事实抽取)与 write() 接线 + time_start 锚定.

验证:
- PassThroughExtractor: 原文整块返回
- LLMFactExtractor: 未配置 key 降级 / JSON 与纯文本解析 / 调用失败降级
- MemoryMiddleware.write: 多事实逐条入 L1(bypass)+ FTS 可检索
- promotion: time_range.exact 锚定 temporal_fragments.time_start
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from smilex.memory.lifecycle.extractor import (
    ExtractorConfig,
    LLMFactExtractor,
    PassThroughExtractor,
    get_extractor,
)
from smilex.memory.models import MemoryScope, TimeRange
from smilex.middlewares.dto import WriteRequest
from smilex.middlewares.memory import MemoryMiddleware

# ---------- PassThroughExtractor / 工厂 ----------


@pytest.mark.asyncio
async def test_passthrough_returns_content_as_single_fact():
    ext = PassThroughExtractor()
    assert await ext.extract("some long chunk") == ["some long chunk"]


def test_get_extractor_default_is_passthrough():
    assert isinstance(get_extractor(), PassThroughExtractor)
    assert isinstance(get_extractor(ExtractorConfig()), PassThroughExtractor)


def test_get_extractor_unknown_backend():
    with pytest.raises(ValueError, match="未知 extractor backend"):
        get_extractor(ExtractorConfig(backend="bogus"))


# ---------- LLMFactExtractor(不发真实请求) ----------


@pytest.mark.asyncio
async def test_llm_extractor_degrades_without_api_key():
    """未配置 key: 不 raise,降级返回原文(write 永远可用)."""
    ext = LLMFactExtractor(api_key=None)
    assert ext._api_key is None
    assert await ext.extract("chunk") == ["chunk"]


def test_llm_extractor_parse_json_array():
    text = (
        "Here you go:\n```json\n["
        '"The user needs to pick up dry cleaning on Oct 22.", '
        '"The user bought black jeans from Levi\'s."]\n```'
    )
    facts = LLMFactExtractor._parse_facts(text)
    assert facts == [
        "The user needs to pick up dry cleaning on Oct 22.",
        "The user bought black jeans from Levi's.",
    ]


def test_llm_extractor_parse_plain_lines_fallback():
    """非 JSON 输出退化为按行切分(去列表符号)."""
    text = "- fact one\n* fact two\n3. fact three"
    facts = LLMFactExtractor._parse_facts(text)
    assert facts == ["fact one", "fact two", "fact three"]


@pytest.mark.asyncio
async def test_llm_extractor_parse_empty_output_returns_empty():
    assert LLMFactExtractor._parse_facts("no facts here") == ["no facts here"]


# ---------- write() 接线: 多事实 bypass L0 ----------


class _ListExtractor:
    """固定返回两条事实的假抽取器."""

    def __init__(self, facts: list[str]):
        self._facts = facts

    async def extract(self, content: str) -> list[str]:
        return self._facts


@pytest.mark.asyncio
async def test_write_with_extractor_creates_one_memory_per_fact():
    """两条短事实各自成为独立记忆,且 bypass L0 直送 L1(可检索)."""
    mw = MemoryMiddleware(fact_extractor=_ListExtractor(["fact alpha", "fact beta"]))
    await mw.initialize()
    try:
        resp = await mw.write(
            WriteRequest(scope=MemoryScope.GLOBAL, content="whatever chunk"),
            session_id="s1",
        )
        assert resp.status.value == "saved"
        assert resp.layers_affected == ["L1"]
        cursor = await mw._engine.conn.execute(
            "SELECT content FROM temporal_fragments WHERE layer='L1' ORDER BY content"
        )
        rows = [r["content"] for r in await cursor.fetchall()]
        assert rows == ["fact alpha", "fact beta"]
        # FTS(v13 触发器)应已自动索引: BM25 能命中
        cursor = await mw._engine.conn.execute(
            "SELECT content FROM temporal_fragments "
            "WHERE rowid IN (SELECT rowid FROM fts_fragments "
            "WHERE fts_fragments MATCH 'alpha')"
        )
        fts_rows = [r["content"] for r in await cursor.fetchall()]
        assert fts_rows == ["fact alpha"]
    finally:
        await mw.close()


@pytest.mark.asyncio
async def test_write_with_extractor_error_falls_back_to_raw_content():
    """抽取器抛异常: 降级写原文整块,write 不崩."""

    class _Boom:
        async def extract(self, content: str) -> list[str]:
            raise RuntimeError("extractor boom")

    mw = MemoryMiddleware(fact_extractor=_Boom())
    await mw.initialize()
    try:
        resp = await mw.write(
            WriteRequest(scope=MemoryScope.GLOBAL, content="raw fallback chunk"),
            session_id="s1",
        )
        assert resp.status.value == "saved"
        cursor = await mw._engine.conn.execute(
            "SELECT content FROM temporal_fragments WHERE layer='L1'"
        )
        rows = [r["content"] for r in await cursor.fetchall()]
        assert rows == ["raw fallback chunk"]
    finally:
        await mw.close()


@pytest.mark.asyncio
async def test_write_passthrough_keeps_historical_behavior():
    """默认 PassThrough: 单条记忆、沿用 L0/L1 阈值语义(小记忆留 L0)."""
    mw = MemoryMiddleware()  # 无 extractor
    await mw.initialize()
    try:
        resp = await mw.write(
            WriteRequest(scope=MemoryScope.GLOBAL, content="tiny"),
            session_id="s1",
        )
        assert resp.layers_affected == ["L0"]
        cursor = await mw._engine.conn.execute(
            "SELECT COUNT(*) AS n FROM temporal_fragments"
        )
        assert (await cursor.fetchone())["n"] == 0
    finally:
        await mw.close()


# ---------- promotion: time_range 锚定 time_start ----------


@pytest.mark.asyncio
async def test_promotion_time_start_anchored_to_time_range():
    """WriteRequest.time_range.exact 应成为 temporal_fragments.time_start.

    回归: 历史行为是 created_at 墙钟,time_range 在晋升后丢失.
    """
    mw = MemoryMiddleware()
    await mw.initialize()
    try:
        tr = TimeRange(exact=datetime(2023, 10, 22, tzinfo=UTC))
        await mw.write(
            WriteRequest(
                scope=MemoryScope.GLOBAL, content="word " * 900, time_range=tr
            ),
            session_id="s1",
        )
        cursor = await mw._engine.conn.execute(
            "SELECT time_start, created_at FROM temporal_fragments LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row["time_start"].startswith("2023-10-22")
        assert not row["created_at"].startswith("2023-")  # created_at 仍是墙钟
    finally:
        await mw.close()


@pytest.mark.asyncio
async def test_promotion_time_start_approx_start_used():
    """无 exact 时用 approx_start;两者皆无回退 created_at."""
    mw = MemoryMiddleware()
    await mw.initialize()
    try:
        tr = TimeRange(approx_start=datetime(2024, 1, 15, tzinfo=UTC))
        await mw.write(
            WriteRequest(
                scope=MemoryScope.GLOBAL, content="word " * 900, time_range=tr
            ),
            session_id="s1",
        )
        cursor = await mw._engine.conn.execute(
            "SELECT time_start FROM temporal_fragments LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row["time_start"].startswith("2024-01-15")
    finally:
        await mw.close()
