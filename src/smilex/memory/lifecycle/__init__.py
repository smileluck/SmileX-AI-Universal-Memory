"""Layer 2: 生命周期 — L0 cachebox / L1 向量 / L2 SQLite / L3 图谱."""

from .context_builder import (
    DEFAULT_TOKEN_BUDGET,
    SAFETY_MARGIN,
    BuiltContext,
    ContextBuilder,
    ContextSource,
)
from .embedder import (
    EMBEDDING_DIM,
    Embedder,
    EmbedderConfig,
    HashEmbedder,
    SentenceTransformerEmbedder,
    get_embedder,
)
from .l0_snapshot import L0SnapshotStore
from .l0_working_memory import L0WorkingMemory
from .promotion import L0_PROMOTION_THRESHOLD, PromotionManager
from .token_counter import TokenCounter

__all__ = [
    "DEFAULT_TOKEN_BUDGET",
    "EMBEDDING_DIM",
    "L0SnapshotStore",
    "L0WorkingMemory",
    "L0_PROMOTION_THRESHOLD",
    "SAFETY_MARGIN",
    "BuiltContext",
    "ContextBuilder",
    "ContextSource",
    "Embedder",
    "EmbedderConfig",
    "HashEmbedder",
    "PromotionManager",
    "SentenceTransformerEmbedder",
    "TokenCounter",
    "get_embedder",
]
