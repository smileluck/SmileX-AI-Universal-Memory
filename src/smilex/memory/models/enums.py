"""Layer 0 enums — 6 StrEnums per main doc §5.1.

StrEnum gives JSON-friendly serialization: `json.dumps(scope)` → `"project"`.
"""

from __future__ import annotations

from enum import StrEnum


class CertaintyLevel(StrEnum):
    """确定性级别 — 所有模糊数据的基础."""

    EXACT = "exact"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class MemoryLayer(StrEnum):
    """记忆层级 — L0/L1/L2/L3 四层."""

    L0_WORKING = "L0"
    L1_SHORT = "L1"
    L2_LONG = "L2"
    L3_SEMANTIC = "L3"


class MemoryScope(StrEnum):
    """记忆作用域 — 解决项目隔离与跨项目共享."""

    PROJECT = "project"  # 项目专属
    TENANT = "tenant"  # 租户专属
    GLOBAL = "global"  # 全局通用


class LockType(StrEnum):
    """锁类型 — Layer 4 并发控制使用."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"
    UPDATE = "update"


class ConflictType(StrEnum):
    """冲突类型 — Layer 4 并发控制使用."""

    VERSION_STALE = "version_stale"
    WRITE_WRITE = "write_write"
    READ_WRITE = "read_write"
    CAUSAL_CYCLE = "causal_cycle"
    CAUSAL_CONTRADICTION = "causal_contradiction"


class PreemptionPolicy(StrEnum):
    """抢占策略 — Layer 3 调度使用."""

    NONE = "none"
    IMMEDIATE = "immediate"
    GRACEFUL = "graceful"  # 默认
    COOPERATIVE = "cooperative"
    PRIORITY_INHERITANCE = "priority_inheritance"
