"""MemoryMiddleware DTO — 公共 API 契约的数据传输对象.

Per main doc §3.2 + EXECUTION_PLAN_GAPS.md §2.1/§3.4:
- 内部 dataclass(与 Layer 0 一致),边界验证用 __post_init__(Pydantic v2 留到
  HTTP/MCP 服务暴露层,见 ADR-013)
- WriteStatus / BootstrapStage 为 middleware 级 StrEnum(不入 Layer 0 enums.py)
- 所有 DTO 提供 to_dict/from_dict 往返序列化
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ..memory.models import (
    CertaintyLevel,
    ConflictType,
    FuzzyLocation,
    MemoryLayer,
    MemoryScope,
    ScopeFilter,
    TimeRange,
)
from ..utils.timeutil import from_iso, now_utc, to_iso


class WriteStatus(StrEnum):
    """写入结果状态 — §2.1 WriteResponse."""

    SAVED = "saved"
    CONFLICT = "conflict"
    PARTIAL = "partial"


class BootstrapStage(StrEnum):
    """项目冷启动阶段 — §9.5 冷启动流程."""

    ONBOARDING = "onboarding"
    TEMPLATE = "template"
    ACTIVE_LEARNING = "active_learning"
    READY = "ready"


# 允许的项目模板(§9.5.1)
PROJECT_TEMPLATES: tuple[str, ...] = ("web", "cli", "data", "agent")


@dataclass
class TripleInput:
    """用户输入版 Triple(省略服务端填充字段,见 GAPS §2.1).

    subject_id/subject_name/object_id 可空,允许服务端从 content 提取;
    但 object_id/object_value 至少一个非空(与 Layer 0 Triple 语义一致).
    """

    subject_id: str | None = None
    subject_name: str | None = None
    predicate: str = ""
    object_id: str | None = None
    object_value: str | None = None
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM

    def __post_init__(self) -> None:
        if not self.predicate:
            raise ValueError("TripleInput.predicate 不能为空")
        if not self.object_id and not self.object_value:
            raise ValueError("TripleInput: object_id 或 object_value 至少一个非空")

    def to_dict(self) -> dict:
        return {
            "subject_id": self.subject_id,
            "subject_name": self.subject_name,
            "predicate": self.predicate,
            "object_id": self.object_id,
            "object_value": self.object_value,
            "certainty": str(self.certainty),
        }

    @classmethod
    def from_dict(cls, d: dict) -> TripleInput:
        return cls(
            subject_id=d.get("subject_id"),
            subject_name=d.get("subject_name"),
            predicate=d.get("predicate", ""),
            object_id=d.get("object_id"),
            object_value=d.get("object_value"),
            certainty=CertaintyLevel(d.get("certainty", "medium")),
        )


@dataclass
class ConflictInfo:
    """写入冲突信息(由 Layer 4 冲突检测在 write 检出冲突时填充,见 GAPS §2.2)."""

    conflict_type: ConflictType = ConflictType.WRITE_WRITE
    conflicting_memory_id: str | None = None  # 已存在的冲突记忆 ID
    message: str | None = None

    def to_dict(self) -> dict:
        return {
            "conflict_type": str(self.conflict_type),
            "conflicting_memory_id": self.conflicting_memory_id,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ConflictInfo:
        return cls(
            conflict_type=ConflictType(d.get("conflict_type", "write_write")),
            conflicting_memory_id=d.get("conflicting_memory_id"),
            message=d.get("message"),
        )


@dataclass
class WriteRequest:
    """写入记忆事件(§3.2)."""

    scope: MemoryScope
    content: str
    entities: list[str] = field(default_factory=list)
    relations: list[TripleInput] = field(default_factory=list)
    time_range: TimeRange | None = None
    location: FuzzyLocation | None = None
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM
    importance: float = 0.5

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("WriteRequest.content 不能为空")
        if not (0.0 <= self.importance <= 1.0):
            raise ValueError(f"WriteRequest.importance 必须在 [0,1],得到 {self.importance}")

    def to_dict(self) -> dict:
        return {
            "scope": str(self.scope),
            "content": self.content,
            "entities": list(self.entities),
            "relations": [r.to_dict() for r in self.relations],
            "time_range": self.time_range.to_dict() if self.time_range else None,
            "location": self.location.to_dict() if self.location else None,
            "certainty": str(self.certainty),
            "importance": self.importance,
        }

    @classmethod
    def from_dict(cls, d: dict) -> WriteRequest:
        return cls(
            scope=MemoryScope(d.get("scope", "project")),
            content=d.get("content", ""),
            entities=list(d.get("entities", [])),
            relations=[TripleInput.from_dict(r) for r in d.get("relations", [])],
            time_range=TimeRange.from_dict(d["time_range"]) if d.get("time_range") else None,
            location=FuzzyLocation.from_dict(d["location"]) if d.get("location") else None,
            certainty=CertaintyLevel(d.get("certainty", "medium")),
            importance=float(d.get("importance", 0.5)),
        )


@dataclass
class WriteResponse:
    """写入结果(GAPS §2.1)."""

    memory_id: str
    status: WriteStatus = WriteStatus.SAVED
    layers_affected: list[MemoryLayer] = field(default_factory=list)
    conflict: ConflictInfo | None = None
    written_at: datetime = field(default_factory=now_utc)

    def to_dict(self) -> dict:
        return {
            "memory_id": self.memory_id,
            "status": str(self.status),
            "layers_affected": [str(layer) for layer in self.layers_affected],
            "conflict": self.conflict.to_dict() if self.conflict else None,
            "written_at": to_iso(self.written_at),
        }

    @classmethod
    def from_dict(cls, d: dict) -> WriteResponse:
        return cls(
            memory_id=d["memory_id"],
            status=WriteStatus(d.get("status", "saved")),
            layers_affected=[MemoryLayer(layer) for layer in d.get("layers_affected", [])],
            conflict=ConflictInfo.from_dict(d["conflict"]) if d.get("conflict") else None,
            written_at=from_iso(d["written_at"]) if d.get("written_at") else now_utc(),
        )


@dataclass
class MemoryRef:
    """检索结果的记忆引用(GAPS §2.1)."""

    id: str
    layer: MemoryLayer
    score: float
    scope: MemoryScope = MemoryScope.PROJECT
    snippet: str | None = None  # 检索结果片段

    def __post_init__(self) -> None:
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"MemoryRef.score 必须在 [0,1],得到 {self.score}")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "layer": str(self.layer),
            "score": self.score,
            "scope": str(self.scope),
            "snippet": self.snippet,
        }

    @classmethod
    def from_dict(cls, d: dict) -> MemoryRef:
        return cls(
            id=d["id"],
            layer=MemoryLayer(d["layer"]),
            score=float(d["score"]),
            scope=MemoryScope(d.get("scope", "project")),
            snippet=d.get("snippet"),
        )


@dataclass
class RecallRequest:
    """检索记忆(§3.2)."""

    query: str
    scope_filter: ScopeFilter = field(default_factory=ScopeFilter)
    time_range: TimeRange | None = None
    location: FuzzyLocation | None = None
    entity_filter: str | None = None
    top_k: int = 10
    token_budget: int = 4000

    def __post_init__(self) -> None:
        if not self.query:
            raise ValueError("RecallRequest.query 不能为空")
        if self.top_k <= 0:
            raise ValueError(f"RecallRequest.top_k 必须 > 0,得到 {self.top_k}")
        if self.token_budget <= 0:
            raise ValueError(f"RecallRequest.token_budget 必须 > 0,得到 {self.token_budget}")

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "scope_filter": {
                "include_project": self.scope_filter.include_project,
                "include_tenant": self.scope_filter.include_tenant,
                "include_global": self.scope_filter.include_global,
                "include_all_projects": self.scope_filter.include_all_projects,
            },
            "time_range": self.time_range.to_dict() if self.time_range else None,
            "location": self.location.to_dict() if self.location else None,
            "entity_filter": self.entity_filter,
            "top_k": self.top_k,
            "token_budget": self.token_budget,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RecallRequest:
        sf = d.get("scope_filter") or {}
        return cls(
            query=d.get("query", ""),
            scope_filter=ScopeFilter(
                include_project=sf.get("include_project"),
                include_tenant=sf.get("include_tenant"),
                include_global=sf.get("include_global", True),
                include_all_projects=sf.get("include_all_projects", False),
            ),
            time_range=TimeRange.from_dict(d["time_range"]) if d.get("time_range") else None,
            location=FuzzyLocation.from_dict(d["location"]) if d.get("location") else None,
            entity_filter=d.get("entity_filter"),
            top_k=int(d.get("top_k", 10)),
            token_budget=int(d.get("token_budget", 4000)),
        )


@dataclass
class RecallResponse:
    """检索结果(§3.2)."""

    context: str  # 压缩到预算内的上下文
    sources: list[MemoryRef] = field(default_factory=list)  # 引用的记忆
    layers_used: list[MemoryLayer] = field(default_factory=list)  # 命中的层级
    token_count: int = 0  # 实际 token 数

    def to_dict(self) -> dict:
        return {
            "context": self.context,
            "sources": [s.to_dict() for s in self.sources],
            "layers_used": [str(layer) for layer in self.layers_used],
            "token_count": self.token_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RecallResponse:
        return cls(
            context=d.get("context", ""),
            sources=[MemoryRef.from_dict(s) for s in d.get("sources", [])],
            layers_used=[MemoryLayer(layer) for layer in d.get("layers_used", [])],
            token_count=int(d.get("token_count", 0)),
        )


@dataclass
class ProjectInitRequest:
    """新项目冷启动初始化请求(§3.2)."""

    name: str
    description: str = ""
    tech_stack: list[str] = field(default_factory=list)
    readme_content: str | None = None
    key_files: dict[str, str] = field(default_factory=dict)
    template: str | None = None  # web/cli/data/agent

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ProjectInitRequest.name 不能为空")
        if self.template is not None and self.template not in PROJECT_TEMPLATES:
            msg = f"template 必须是 {PROJECT_TEMPLATES} 之一,得到 {self.template!r}"
            raise ValueError(msg)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "tech_stack": list(self.tech_stack),
            "readme_content": self.readme_content,
            "key_files": dict(self.key_files),
            "template": self.template,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ProjectInitRequest:
        return cls(
            name=d.get("name", ""),
            description=d.get("description", ""),
            tech_stack=list(d.get("tech_stack", [])),
            readme_content=d.get("readme_content"),
            key_files=dict(d.get("key_files", {})),
            template=d.get("template"),
        )


@dataclass
class ProjectInitResponse:
    """新项目冷启动初始化结果(GAPS §2.1)."""

    scope: str  # 全路径,如 "project:{id}"(见决策 D6)
    entity_count: int = 0
    triple_count: int = 0
    stage: BootstrapStage = BootstrapStage.ONBOARDING
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "entity_count": self.entity_count,
            "triple_count": self.triple_count,
            "stage": str(self.stage),
            "elapsed_ms": self.elapsed_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ProjectInitResponse:
        return cls(
            scope=d.get("scope", "global"),
            entity_count=int(d.get("entity_count", 0)),
            triple_count=int(d.get("triple_count", 0)),
            stage=BootstrapStage(d.get("stage", "onboarding")),
            elapsed_ms=int(d.get("elapsed_ms", 0)),
        )


@dataclass
class ProjectContext:
    """项目上下文 — ProjectBootstrap.initialize 返回值(§9.5.3 / GAPS §3.4)."""

    scope: str  # 全路径,如 "project:{id}"
    name: str
    stage: BootstrapStage = BootstrapStage.ONBOARDING
    tech_stack: list[str] = field(default_factory=list)
    entity_count: int = 0
    triple_count: int = 0
    created_at: datetime = field(default_factory=now_utc)

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "name": self.name,
            "stage": str(self.stage),
            "tech_stack": list(self.tech_stack),
            "entity_count": self.entity_count,
            "triple_count": self.triple_count,
            "created_at": to_iso(self.created_at),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ProjectContext:
        return cls(
            scope=d.get("scope", "global"),
            name=d.get("name", ""),
            stage=BootstrapStage(d.get("stage", "onboarding")),
            tech_stack=list(d.get("tech_stack", [])),
            entity_count=int(d.get("entity_count", 0)),
            triple_count=int(d.get("triple_count", 0)),
            created_at=from_iso(d["created_at"]) if d.get("created_at") else now_utc(),
        )
