"""Unit tests for Reranker(精排,检索优化).

验证:
- NoopReranker: 返回空列表不干预
- CrossEncoderReranker: 懒加载不 import / max_candidates 校验
- get_reranker 工厂: 默认 Noop,未知 backend 报错
- ContextBuilder._rerank_candidates: 分数替换/截断保护/异常降级/L0 不动
"""

from __future__ import annotations

import pytest

from smilex.memory.lifecycle.context_builder import ContextBuilder, ContextSource
from smilex.memory.lifecycle.reranker import (
    CrossEncoderReranker,
    NoopReranker,
    RerankerConfig,
    get_reranker,
)

# ---------- NoopReranker ----------


@pytest.mark.asyncio
async def test_noop_reranker_returns_empty():
    """Noop 返回空列表 — 调用方据此保留原 RRF 分数."""
    r = NoopReranker()
    assert await r.rerank("q", ["a", "b"]) == []


# ---------- CrossEncoderReranker(懒加载) ----------


def test_cross_encoder_lazy_no_import_on_init():
    """构造时不 import sentence-transformers / 不加载模型."""
    emb = CrossEncoderReranker()
    assert emb._model is None
    assert emb.max_candidates == 50


def test_cross_encoder_invalid_max_candidates():
    with pytest.raises(ValueError, match="max_candidates"):
        CrossEncoderReranker(max_candidates=0)


@pytest.mark.asyncio
async def test_cross_encoder_empty_documents():
    """空输入直接返回,不触发模型加载."""
    r = CrossEncoderReranker()
    assert await r.rerank("q", []) == []


# ---------- 工厂 ----------


def test_get_reranker_default_is_noop():
    assert isinstance(get_reranker(), NoopReranker)
    assert isinstance(get_reranker(RerankerConfig()), NoopReranker)


def test_get_reranker_cross_encoder():
    r = get_reranker(RerankerConfig(backend="cross-encoder"))
    assert isinstance(r, CrossEncoderReranker)
    assert r._model_name == "BAAI/bge-reranker-v2-m3"


def test_get_reranker_unknown_backend():
    with pytest.raises(ValueError, match="未知 reranker backend"):
        get_reranker(RerankerConfig(backend="bogus"))


# ---------- ContextBuilder._rerank_candidates ----------


class _FixedReranker:
    """测试用假精排器: 按文档首字母打分(b=1.0, a=0.5, c=0.0)."""

    def __init__(self, error: bool = False):
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []
        self.max_candidates = 50

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if self.error:
            raise RuntimeError("rerank boom")
        self.calls.append((query, documents))
        table = {"b": 1.0, "a": 0.5, "c": 0.0}
        return [table.get(d[0], 0.5) for d in documents]


def _src(layer: str, content: str, score: float) -> ContextSource:
    return ContextSource(memory_id=content, layer=layer, content=content, score=score)


@pytest.mark.asyncio
async def test_rerank_candidates_reorders_within_layer():
    """L1 候选按精排分数重排;L0 不动且不送精排."""
    builder = ContextBuilder(reranker=_FixedReranker())
    cands = [
        _src("L0", "l0-item", 0.9),
        _src("L1", "alpha", 1.0),   # RRF 第一,精排 0.5
        _src("L1", "beta", 0.5),    # RRF 第二,精排 1.0
    ]
    out = await builder._rerank_candidates("q", cands)
    assert [c.layer for c in out] == ["L0", "L1", "L1"]
    assert out[1].content == "beta"  # 精排第一顶到 L1 组首
    assert out[1].score == 1.0
    assert out[2].score == 0.0
    # L0 不进精排文档列表
    docs = [d for _, ds in builder._reranker.calls for d in ds]  # type: ignore[attr-defined]
    assert "l0-item" not in docs


@pytest.mark.asyncio
async def test_rerank_candidates_noop_keeps_order():
    """Noop 时原样返回(含原分数),不重排."""
    builder = ContextBuilder()  # 默认 Noop
    cands = [_src("L1", "a", 0.3), _src("L1", "b", 0.9)]
    out = await builder._rerank_candidates("q", cands)
    assert [c.score for c in out] == [0.3, 0.9]


@pytest.mark.asyncio
async def test_rerank_candidates_error_degrades_gracefully():
    """精排抛异常时保留原分数,不崩(降级为无精排)."""
    builder = ContextBuilder(reranker=_FixedReranker(error=True))
    cands = [_src("L1", "a", 0.9), _src("L1", "b", 0.1)]
    out = await builder._rerank_candidates("q", cands)
    assert [c.content for c in out] == ["a", "b"]
    assert [c.score for c in out] == [0.9, 0.1]


@pytest.mark.asyncio
async def test_rerank_candidates_respects_max_candidates():
    """超出 max_candidates 的条目不送精排,且压到精排条目之后."""
    rr = _FixedReranker()
    rr.max_candidates = 2
    builder = ContextBuilder(reranker=rr)
    cands = [
        _src("L1", "c", 1.0),  # RRF 高但精排 0.0
        _src("L1", "a", 0.8),
        _src("L1", "b", 0.6),  # 第 3 条:不精排
    ]
    out = await builder._rerank_candidates("q", cands)
    assert len(rr.calls[0][1]) == 2  # 只送了前 2 条
    assert out[-1].content == "b"    # 未精排条目排层内最后
    assert out[-1].score <= 0.0


@pytest.mark.asyncio
async def test_rerank_candidates_without_query_is_noop():
    """query_text 为 None(纯实体/时间检索)时不精排."""
    builder = ContextBuilder(reranker=_FixedReranker())
    cands = [_src("L1", "a", 0.5)]
    out = await builder._rerank_candidates(None, cands)
    assert out[0].score == 0.5
    assert builder._reranker.calls == []  # type: ignore[attr-defined]
