"""向量标量量化工具 — float32 → int8(06-layer5 §2.5 / §3.6 算法 1).

设计取舍(P2,为何不直接量化 memory_vectors):
- sqlite-vec vec0 虚拟表类型为 FLOAT[1024],无法原位存 int8;
  改 vec0 列类型/另建 int8 影子表 + 双写会破坏现有 KNN 正确性与查询路径,
  风险大于收益
- 实际体积优化由归档路径承担: 归档记录不保留向量(召回走非向量通道,
  见 quality/archiver.py),热数据向量保持 float32 保证 KNN 精度无损
- 本模块提供经过往返测试的 int8 标量量化编解码(4x 压缩),
  供后续阶段(PQ / 辅助向量量化副本)直接复用

算法(06-layer5 §3.6 算法 1,逐向量 min-max 归一到 [0,255]):
    quantized = round((v - vmin) * 255 / (vmax - vmin))  → uint8
    dequantized = quantized * (vmax - vmin) / 255 + vmin
单向量最大误差 <= (vmax - vmin) / 510(往返半量化步长);
零方差向量(vmin == vmax)退化为全零码,反量化还原为常量向量.
"""

from __future__ import annotations

import numpy as np

# 量化位宽(uint8 一档 = (vmax - vmin) / 255)
_LEVELS = 255


def quantize_int8(vector: np.ndarray | list[float]) -> tuple[bytes, float, float]:
    """float32 向量 → int8(uint8 语义)标量量化.

    Args:
        vector: 一维向量(任意长度;HashEmbedder 为 1024 维)

    Returns:
        (codes, vmin, vmax) — codes 为 len(vector) 字节的 bytes,
        vmin/vmax 为反量化所需的原始范围(float)

    Raises:
        ValueError: 空向量或非一维
    """
    vec = np.asarray(vector, dtype=np.float32)
    if vec.ndim != 1 or vec.shape[0] == 0:
        raise ValueError(f"向量必须是非空一维,得到 shape {vec.shape}")
    vmin = float(vec.min())
    vmax = float(vec.max())
    if vmax == vmin:
        # 零方差: 全零码,反量化时由 vmin/vmax 还原常量
        return bytes(vec.shape[0]), vmin, vmax
    scale = _LEVELS / (vmax - vmin)
    codes = np.round((vec - vmin) * scale).astype(np.uint8)
    return codes.tobytes(), vmin, vmax


def dequantize_int8(codes: bytes | np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    """int8 标量量化 → float32 向量(quantize_int8 的逆变换).

    Args:
        codes: quantize_int8 返回的字节串(或 uint8 数组)
        vmin / vmax: 量化时记录的原始范围

    Returns:
        float32 一维向量,长度与 codes 一致
    """
    q = np.frombuffer(codes, dtype=np.uint8) if isinstance(codes, bytes) else codes
    if vmax == vmin:
        return np.full(q.shape[0], vmin, dtype=np.float32)
    scale = (vmax - vmin) / _LEVELS
    return (q.astype(np.float32) * scale + vmin).astype(np.float32)


__all__ = [
    "dequantize_int8",
    "quantize_int8",
]
