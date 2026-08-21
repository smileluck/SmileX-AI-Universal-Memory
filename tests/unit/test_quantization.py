"""Unit tests for P2 向量量化工具 — quantize_int8/dequantize_int8(06-layer5 §3.6 算法 1).

验证: 往返正确性(误差上界)/ 零方差退化 / 压缩比 / 输入校验.
"""

from __future__ import annotations

import numpy as np
import pytest

from smilex.memory.quality import dequantize_int8, quantize_int8


def test_roundtrip_error_bound():
    """往返误差 <= 量化步长的一半((vmax - vmin) / 510)."""
    rng = np.random.default_rng(42)
    vec = rng.standard_normal(1024).astype(np.float32)
    codes, vmin, vmax = quantize_int8(vec)
    restored = dequantize_int8(codes, vmin, vmax)

    assert restored.shape == vec.shape
    assert restored.dtype == np.float32
    max_err = float(np.abs(restored - vec).max())
    assert max_err <= (vmax - vmin) / 510 + 1e-6


def test_roundtrip_normalized_embedding():
    """L2 归一化向量(HashEmbedder 输出形态)往返后余弦近似保持."""
    from smilex.memory.lifecycle.embedder import HashEmbedder

    vec = HashEmbedder().embed("SmileX 记忆系统向量量化")
    codes, vmin, vmax = quantize_int8(vec)
    restored = dequantize_int8(codes, vmin, vmax)
    cos = float(
        np.dot(vec, restored) / (np.linalg.norm(vec) * np.linalg.norm(restored))
    )
    assert cos > 0.99  # 06-layer5 §2.5: int8 精度损失 < 3%


def test_compression_ratio_4x():
    """压缩比 4x: float32 每维 4 字节 → int8 每维 1 字节."""
    vec = np.linspace(-1.0, 1.0, 1024, dtype=np.float32)
    codes, vmin, vmax = quantize_int8(vec)
    assert len(codes) == 1024
    assert vec.nbytes == 4096
    assert len(codes) * 4 == vec.nbytes
    assert isinstance(vmin, float) and isinstance(vmax, float)


def test_constant_vector_degenerate():
    """零方差向量(vmin == vmax): 全零码,反量化还原常量."""
    vec = np.full(16, 0.5, dtype=np.float32)
    codes, vmin, vmax = quantize_int8(vec)
    assert codes == bytes(16)
    restored = dequantize_int8(codes, vmin, vmax)
    np.testing.assert_allclose(restored, vec)


def test_invalid_input():
    """空向量 / 多维矩阵拒绝."""
    with pytest.raises(ValueError, match="一维"):
        quantize_int8(np.array([], dtype=np.float32))
    with pytest.raises(ValueError, match="一维"):
        quantize_int8(np.zeros((2, 4), dtype=np.float32))
