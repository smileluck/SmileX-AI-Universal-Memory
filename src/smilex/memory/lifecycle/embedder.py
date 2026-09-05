"""兼容 shim — embedder 已上移至 ``smilex.memory.embedder``(2026-09).

Protocol+默认实现+可选重后端属于基础设施客户端,非生命周期逻辑;
上移同时修复 storage→lifecycle 的倒挂依赖。旧路径保留 re-export。
"""

from ..embedder import (
    DEFAULT_MODEL_NAME,
    EMBEDDING_DIM,
    Embedder,
    EmbedderConfig,
    HashEmbedder,
    SentenceTransformerEmbedder,
    get_embedder,
)

__all__ = [
    "DEFAULT_MODEL_NAME",
    "EMBEDDING_DIM",
    "Embedder",
    "EmbedderConfig",
    "HashEmbedder",
    "SentenceTransformerEmbedder",
    "get_embedder",
]
