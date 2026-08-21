"""Unit tests for Embedder (L2.3 决策 D5 / L2.4).

验证:
- HashEmbedder: 确定性 / 1024 维 / L2 归一化 / batch 一致 / Protocol 兼容
- get_embedder 工厂: 默认 HashEmbedder,未知 backend 报错
- SentenceTransformerEmbedder: 未安装 sentence-transformers 时报错友好
  (不下载模型,不测真实语义)
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from smilex.memory.lifecycle.embedder import (
    EMBEDDING_DIM,
    Embedder,
    EmbedderConfig,
    HashEmbedder,
    SentenceTransformerEmbedder,
    get_embedder,
)

# ---------- HashEmbedder ----------


def test_hash_embedder_default_dimension():
    """默认维度 = EMBEDDING_DIM(1024,决策 D5)."""
    emb = HashEmbedder()
    assert emb.dimension == EMBEDDING_DIM == 1024
    vec = emb.embed("hello")
    assert vec.shape == (1024,)
    assert vec.dtype == np.float32


def test_hash_embedder_deterministic():
    """相同文本 → 相同向量(逐位一致)."""
    emb = HashEmbedder()
    v1 = emb.embed("alice works at CompanyX")
    v2 = emb.embed("alice works at CompanyX")
    np.testing.assert_array_equal(v1, v2)


def test_hash_embedder_l2_normalized():
    """输出向量 L2 范数 ≈ 1."""
    emb = HashEmbedder()
    for text in ["", "a", "中文文本测试", "x" * 10000]:
        vec = emb.embed(text)
        assert float(np.linalg.norm(vec)) == pytest.approx(1.0, abs=1e-5)


def test_hash_embedder_different_texts_differ():
    """不同文本 → 不同向量(哈希的雪崩效应)."""
    emb = HashEmbedder()
    v1 = emb.embed("text one")
    v2 = emb.embed("text two")
    assert not np.array_equal(v1, v2)


def test_hash_embedder_custom_dimension():
    """自定义维度(如测试用小维度)."""
    emb = HashEmbedder(dimension=32)
    vec = emb.embed("hello")
    assert vec.shape == (32,)
    assert float(np.linalg.norm(vec)) == pytest.approx(1.0, abs=1e-5)


def test_hash_embedder_invalid_dimension():
    with pytest.raises(ValueError, match="dimension"):
        HashEmbedder(dimension=0)


def test_hash_embedder_batch_matches_single():
    """embed_batch 与逐条 embed 结果一致."""
    emb = HashEmbedder()
    texts = ["alpha", "beta", "gamma"]
    batch = emb.embed_batch(texts)
    singles = [emb.embed(t) for t in texts]
    assert len(batch) == 3
    for b, s in zip(batch, singles, strict=True):
        np.testing.assert_array_equal(b, s)


def test_hash_embedder_satisfies_protocol():
    """HashEmbedder 结构兼容 Embedder Protocol."""
    assert isinstance(HashEmbedder(), Embedder)


# ---------- get_embedder 工厂 ----------


def test_get_embedder_default_is_hash():
    """工厂默认返回 HashEmbedder(零依赖,决策 D5)."""
    emb = get_embedder()
    assert isinstance(emb, HashEmbedder)
    assert emb.dimension == 1024


def test_get_embedder_sentence_transformers_backend():
    """backend='sentence-transformers' 返回懒加载实例(不触发模型加载)."""
    emb = get_embedder(EmbedderConfig(backend="sentence-transformers"))
    assert isinstance(emb, SentenceTransformerEmbedder)
    assert emb.dimension == 1024  # 未加载模型时按 BGE-M3 1024 维


def test_get_embedder_unknown_backend():
    with pytest.raises(ValueError, match="未知 embedder backend"):
        get_embedder(EmbedderConfig(backend="openai"))


# ---------- SentenceTransformerEmbedder(未安装时报错友好) ----------


def test_st_embedder_friendly_error_when_not_installed(monkeypatch):
    """模拟 sentence-transformers 未安装: embed 时 raise ImportError 带安装提示.

    不下载模型,只验证错误路径.
    """
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    emb = SentenceTransformerEmbedder()
    with pytest.raises(ImportError, match="smilex-ai-memory\\[embedding\\]"):
        emb.embed("hello")


def test_st_embedder_lazy_no_import_on_init():
    """构造时不 import sentence-transformers / 不加载模型(懒加载)."""
    emb = SentenceTransformerEmbedder(model_name="BAAI/bge-m3")
    assert emb._model is None
    assert emb.model_name == "BAAI/bge-m3"
