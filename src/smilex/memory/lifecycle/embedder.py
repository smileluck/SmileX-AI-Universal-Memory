"""Embedder — 文本向量化(决策 D5: 1024 维,Hash 默认 + sentence-transformers 可选).

Per 12-mvp-execution-plan.md §7 决策 D5:
- 维度统一 1024(BGE-M3),schema 010_virtual_tables.sql 已按 FLOAT[1024] 建表
- 架构: Embedder Protocol + 两个后端
    - HashEmbedder(默认,零依赖): 确定性哈希 embedding,MVP 默认/测试/无模型环境
    - SentenceTransformerEmbedder(可选 extra `embedding`): BGE-M3 语义向量
- 工厂: get_embedder() / EmbedderConfig,默认返回 HashEmbedder
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import numpy as np

# 决策 D5: 维度统一 1024(BGE-M3;主文档 §6.4 原文 1536 已废弃)
EMBEDDING_DIM = 1024

DEFAULT_MODEL_NAME = "BAAI/bge-m3"

# HashEmbedder 每个 SHA-256 块(32 字节)展开成的 float32 个数
_HASH_FLOATS_PER_BLOCK = 8


@runtime_checkable
class Embedder(Protocol):
    """文本向量化协议(决策 D5).

    实现约定:
    - embed() 返回 shape == (dimension,) 的 float32 向量
    - 向量应 L2 归一化(sqlite-vec 默认 L2 距离,归一化后等价余弦排序)
    """

    @property
    def dimension(self) -> int:
        """输出向量维度."""
        ...

    def embed(self, text: str) -> np.ndarray:
        """把单条文本编码为向量."""
        ...

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        """批量编码(默认逐条,实现可覆盖为真正的批量推理)."""
        ...


class HashEmbedder:
    """确定性哈希 embedding — 零依赖默认实现(决策 D5).

    实现: 对 ``f"{text}\\x00{block}"`` 做 SHA-256,每 32 字节摘要展开为 8 个
    float32(uint32 → [0,1) → [-1,1)),填满 dimension 维后 L2 归一化.

    ⚠️ 重要限制: 该实现只保证**确定性**(相同文本 → 相同向量),
    **不保证语义相似度**(语义相近的文本,向量距离不一定近).
    仅用于 MVP 默认、单元测试与无模型环境;生产语义检索请改用
    SentenceTransformerEmbedder(BGE-M3).
    """

    def __init__(self, dimension: int = EMBEDDING_DIM) -> None:
        if dimension <= 0:
            raise ValueError(f"dimension 必须为正整数,得到 {dimension!r}")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, text: str) -> np.ndarray:
        """确定性哈希编码: 相同 text 恒得相同 L2 归一化向量."""
        n_blocks = math.ceil(self._dimension / _HASH_FLOATS_PER_BLOCK)
        values = np.empty(n_blocks * _HASH_FLOATS_PER_BLOCK, dtype=np.float32)
        for block in range(n_blocks):
            digest = hashlib.sha256(f"{text}\x00{block}".encode()).digest()
            for i in range(_HASH_FLOATS_PER_BLOCK):
                (u,) = struct.unpack_from("<I", digest, i * 4)
                values[block * _HASH_FLOATS_PER_BLOCK + i] = (u / 2**32) * 2.0 - 1.0
        vec = values[: self._dimension]
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return vec.astype(np.float32)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed(t) for t in texts]


class SentenceTransformerEmbedder:
    """sentence-transformers 后端(BGE-M3,1024 维)— 可选依赖.

    安装: ``pip install "smilex-ai-memory[embedding]"``
    懒加载: 首次 embed()/embed_batch() 时才 import 并加载模型
    (BGE-M3 约 2GB,首次下载后缓存到 ``~/.cache/``).

    sentence-transformers 未安装时,首次 embed 会 raise ImportError
    并附带安装提示.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        *,
        device: str | None = None,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._model: Any | None = None  # 懒加载

    def _load_model(self) -> Any:
        """懒加载模型;未安装 sentence-transformers 时给出清晰错误."""
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ImportError(
                "SentenceTransformerEmbedder 需要 sentence-transformers,但未安装. "
                "安装方式: pip install 'smilex-ai-memory[embedding]' "
                "(或 uv add --optional embedding sentence-transformers). "
                "无模型环境请改用 HashEmbedder."
            ) from e
        self._model = SentenceTransformer(self._model_name, device=self._device)
        return self._model

    @property
    def dimension(self) -> int:
        """输出维度.模型未加载时按 BGE-M3 的 1024 维返回(决策 D5)."""
        if self._model is not None:
            # sentence-transformers v5 起 get_sentence_embedding_dimension 改名
            # get_embedding_dimension;旧名仅作兼容回退
            get_dim = getattr(self._model, "get_embedding_dimension", None)
            if get_dim is None:
                get_dim = self._model.get_sentence_embedding_dimension
            return int(get_dim())
        return EMBEDDING_DIM

    @property
    def model_name(self) -> str:
        return self._model_name

    # 语料里可能出现 "<|endoftext|>" 等特殊 token 字面量(如 LongMemEval 部分样本),
    # HF tokenizer 默认拒绝编码;去掉尖括号按普通文本处理
    _SPECIAL_TOKEN_RE = re.compile(r"<\|[^|<>]{0,32}\|>")

    @classmethod
    def _sanitize(cls, text: str) -> str:
        return cls._SPECIAL_TOKEN_RE.sub(lambda m: m.group(0).replace("<", "").replace(">", ""), text)

    def embed(self, text: str) -> np.ndarray:
        model = self._load_model()
        vec = model.encode(self._sanitize(text), normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        model = self._load_model()
        vecs = model.encode([self._sanitize(t) for t in texts], normalize_embeddings=True)
        return [np.asarray(v, dtype=np.float32) for v in vecs]


@dataclass(frozen=True)
class EmbedderConfig:
    """Embedder 工厂配置(决策 D5).

    Attributes:
        backend: "hash"(默认,零依赖)或 "sentence-transformers"(可选 extra)
        model_name: sentence-transformers 模型名(默认 BGE-M3)
        dimension: 向量维度(默认 1024,仅 HashEmbedder 可自定义)
    """

    backend: str = "hash"
    model_name: str = DEFAULT_MODEL_NAME
    dimension: int = EMBEDDING_DIM


def get_embedder(config: EmbedderConfig | None = None) -> Embedder:
    """Embedder 工厂 — 默认返回 HashEmbedder(零依赖,决策 D5).

    Args:
        config: None 时使用默认配置(HashEmbedder, 1024 维)

    Raises:
        ValueError: 未知 backend
    """
    config = config or EmbedderConfig()
    if config.backend == "hash":
        return HashEmbedder(dimension=config.dimension)
    if config.backend in ("sentence-transformers", "bge-m3"):
        return SentenceTransformerEmbedder(model_name=config.model_name)
    raise ValueError(
        f"未知 embedder backend: {config.backend!r}"
        "(支持: 'hash' / 'sentence-transformers')"
    )


__all__ = [
    "EMBEDDING_DIM",
    "Embedder",
    "EmbedderConfig",
    "HashEmbedder",
    "SentenceTransformerEmbedder",
    "get_embedder",
]
