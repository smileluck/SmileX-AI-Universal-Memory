"""Layer 4: 并发控制(P1-a)— 锁管理器 + 冲突检测 + 冲突解决(主文档 §10).

模块文档 05 §4.1 规划的文件名(conflict_detector/conflict_resolver)
落地为 conflict_detection/conflict_resolution,职责不变。
"""

from .conflict_detection import (
    DEFAULT_CYCLE_MAX_DEPTH,
    Conflict,
    ConflictDetector,
    MemoryOperation,
    OperationType,
)
from .conflict_resolution import (
    DEFAULT_STRATEGIES,
    PROCEED_ACTIONS,
    ConflictResolver,
    Resolution,
    ResolutionStrategy,
)
from .controller import ConcurrencyController, GuardResult
from .lock_manager import (
    LOCK_COMPATIBILITY,
    LockError,
    LockHandle,
    LockManager,
    LockTimeoutError,
    is_compatible,
)

__all__ = [
    "DEFAULT_CYCLE_MAX_DEPTH",
    "DEFAULT_STRATEGIES",
    "LOCK_COMPATIBILITY",
    "PROCEED_ACTIONS",
    "ConcurrencyController",
    "Conflict",
    "ConflictDetector",
    "ConflictResolver",
    "GuardResult",
    "LockError",
    "LockHandle",
    "LockManager",
    "LockTimeoutError",
    "MemoryOperation",
    "OperationType",
    "Resolution",
    "ResolutionStrategy",
    "is_compatible",
]
