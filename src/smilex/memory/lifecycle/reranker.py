"""兼容 shim — reranker 已上移至 ``smilex.memory.reranker``(2026-09)."""

from ..reranker import (
    DEFAULT_RERANK_MODEL,
    RERANKER_BACKENDS,
    CrossEncoderReranker,
    NoopReranker,
    Reranker,
    RerankerConfig,
    get_reranker,
)

__all__ = [
    "DEFAULT_RERANK_MODEL",
    "RERANKER_BACKENDS",
    "CrossEncoderReranker",
    "NoopReranker",
    "Reranker",
    "RerankerConfig",
    "get_reranker",
]
