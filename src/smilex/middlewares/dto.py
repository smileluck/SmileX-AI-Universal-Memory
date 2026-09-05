"""兼容 shim — DTO 已下沉至 ``smilex.memory.contracts``(2026-09).

原位置 middlewares/dto.py 同时被 server 与 scheduler.bootstrap 引用,
形成 bootstrap → middlewares 反向依赖;下沉后本模块仅作路径兼容,
公共 API 面不变(经 middlewares/__init__ 与本路径 re-export)。
"""

from ..memory.contracts import (
    PROJECT_TEMPLATES,
    BootstrapStage,
    ConflictInfo,
    MemoryRef,
    ProjectContext,
    ProjectInitRequest,
    ProjectInitResponse,
    RecallRequest,
    RecallResponse,
    TripleInput,
    WriteRequest,
    WriteResponse,
    WriteStatus,
)

__all__ = [
    "PROJECT_TEMPLATES",
    "BootstrapStage",
    "ConflictInfo",
    "MemoryRef",
    "ProjectContext",
    "ProjectInitRequest",
    "ProjectInitResponse",
    "RecallRequest",
    "RecallResponse",
    "TripleInput",
    "WriteRequest",
    "WriteResponse",
    "WriteStatus",
]
