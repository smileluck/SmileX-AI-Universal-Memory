"""Layer 5: 知识质量(§11)— 矛盾检测 / 跨项目提升 / 历史归档 / int8 量化工具."""

from .archiver import (
    TASK_ARCHIVE,
    Archiver,
    ArchiveResult,
    ColdReport,
    RestoreResult,
    RetentionPolicy,
    register_archive_task,
)
from .contradiction import (
    CAUSE_PREDICATES,
    DEFAULT_ENUM_PREDICATES,
    SINGLE_VALUED_RELATION_TYPES,
    Contradiction,
    ContradictionDetector,
    ContradictionKind,
    ContradictionReport,
)
from .quantization import dequantize_int8, quantize_int8
from .scope_promoter import (
    DEFAULT_MIN_PROJECTS,
    TASK_SCOPE_PROMOTION,
    CrossProjectPattern,
    PromotionReport,
    ScopePromoter,
    normalize_value,
    register_scope_promotion_task,
)

__all__ = [
    "CAUSE_PREDICATES",
    "DEFAULT_ENUM_PREDICATES",
    "DEFAULT_MIN_PROJECTS",
    "SINGLE_VALUED_RELATION_TYPES",
    "TASK_ARCHIVE",
    "TASK_SCOPE_PROMOTION",
    "Archiver",
    "ArchiveResult",
    "ColdReport",
    "Contradiction",
    "ContradictionDetector",
    "ContradictionKind",
    "ContradictionReport",
    "CrossProjectPattern",
    "PromotionReport",
    "RestoreResult",
    "RetentionPolicy",
    "ScopePromoter",
    "dequantize_int8",
    "normalize_value",
    "quantize_int8",
    "register_archive_task",
    "register_scope_promotion_task",
]
