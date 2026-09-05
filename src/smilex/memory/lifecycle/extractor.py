"""兼容 shim — extractor 已上移至 ``smilex.memory.extractor``(2026-09)."""

from ..extractor import (
    DEFAULT_EXTRACT_MODEL,
    EXTRACTOR_BACKENDS,
    ExtractorConfig,
    FactExtractor,
    LLMFactExtractor,
    PassThroughExtractor,
    get_extractor,
)

__all__ = [
    "DEFAULT_EXTRACT_MODEL",
    "EXTRACTOR_BACKENDS",
    "ExtractorConfig",
    "FactExtractor",
    "LLMFactExtractor",
    "PassThroughExtractor",
    "get_extractor",
]
