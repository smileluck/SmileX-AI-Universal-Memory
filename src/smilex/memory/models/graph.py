"""Graph models — Entity / Triple / CausalChain.

Per main doc §5.2:
- Triple 验证: object_id 或 object_value 至少一个非空
- Triple.relation_type: causal/spatial/temporal/semantic
- source_closet: 双向指针(用于知识质量层追溯)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from ...utils.ids import generate_id
from ...utils.timeutil import from_iso, now_utc, to_iso
from .enums import CertaintyLevel, MemoryScope
from .fuzzy import FuzzyLocation


@dataclass
class Entity:
    """实体节点 — 图谱中的节点."""

    id: str = field(default_factory=generate_id)
    entity_id: str = ""  # 归一化 ID(如 "person:drchen")
    entity_type: str = ""  # person/location/object/concept/event
    name: str = ""
    scope: MemoryScope = MemoryScope.PROJECT
    valid_from: datetime = field(default_factory=now_utc)
    valid_to: datetime | None = None
    location: FuzzyLocation | None = None
    embedding: np.ndarray | None = None
    source_closet: str | None = None  # 双向指针

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "name": self.name,
            "scope": str(self.scope),
            "valid_from": to_iso(self.valid_from),
            "valid_to": to_iso(self.valid_to) if self.valid_to else None,
            "location": self.location.to_dict() if self.location else None,
            "embedding": self.embedding.tolist() if self.embedding is not None else None,
            "source_closet": self.source_closet,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Entity:
        emb = d.get("embedding")
        return cls(
            id=d["id"],
            entity_id=d.get("entity_id", ""),
            entity_type=d.get("entity_type", ""),
            name=d.get("name", ""),
            scope=MemoryScope(d.get("scope", "project")),
            valid_from=from_iso(d["valid_from"]) if d.get("valid_from") else now_utc(),
            valid_to=from_iso(d["valid_to"]) if d.get("valid_to") else None,
            location=FuzzyLocation.from_dict(d["location"]) if d.get("location") else None,
            embedding=np.array(emb, dtype=np.float32) if emb else None,
            source_closet=d.get("source_closet"),
        )


@dataclass
class Triple:
    """三元组边 + 因果链 — 图谱中的边.

    验证规则:
    - object_id 或 object_value 至少一个非空
    - causal_level >= 0
    - 0 <= confidence <= 1
    """

    id: str = field(default_factory=generate_id)
    triple_id: str = ""  # 归一化 ID(用于去重)
    subject_id: str = ""
    predicate: str = ""
    object_id: str | None = None
    object_value: str | None = None
    scope: MemoryScope = MemoryScope.PROJECT
    valid_from: datetime = field(default_factory=now_utc)
    valid_to: datetime | None = None
    predecessor_id: str | None = None  # 因果链前驱
    causal_level: int = 0
    confidence: float = 1.0
    certainty: CertaintyLevel = CertaintyLevel.EXACT
    relation_type: str = "semantic"  # causal/spatial/temporal/semantic
    source_closet: str | None = None

    def __post_init__(self) -> None:
        if not self.object_id and not self.object_value:
            raise ValueError("Triple: object_id 或 object_value 至少一个非空")
        if self.causal_level < 0:
            raise ValueError(f"Triple.causal_level 必须 >= 0,得到 {self.causal_level}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"Triple.confidence 必须在 [0,1],得到 {self.confidence}")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "triple_id": self.triple_id,
            "subject_id": self.subject_id,
            "predicate": self.predicate,
            "object_id": self.object_id,
            "object_value": self.object_value,
            "scope": str(self.scope),
            "valid_from": to_iso(self.valid_from),
            "valid_to": to_iso(self.valid_to) if self.valid_to else None,
            "predecessor_id": self.predecessor_id,
            "causal_level": self.causal_level,
            "confidence": self.confidence,
            "certainty": str(self.certainty),
            "relation_type": self.relation_type,
            "source_closet": self.source_closet,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Triple:
        return cls(
            id=d["id"],
            triple_id=d.get("triple_id", ""),
            subject_id=d.get("subject_id", ""),
            predicate=d.get("predicate", ""),
            object_id=d.get("object_id"),
            object_value=d.get("object_value"),
            scope=MemoryScope(d.get("scope", "project")),
            valid_from=from_iso(d["valid_from"]) if d.get("valid_from") else now_utc(),
            valid_to=from_iso(d["valid_to"]) if d.get("valid_to") else None,
            predecessor_id=d.get("predecessor_id"),
            causal_level=int(d.get("causal_level", 0)),
            confidence=float(d.get("confidence", 1.0)),
            certainty=CertaintyLevel(d.get("certainty", "exact")),
            relation_type=d.get("relation_type", "semantic"),
            source_closet=d.get("source_closet"),
        )


@dataclass
class CausalChain:
    """因果链 — 多个 Triple 串联形成的链.

    node_ids 是 Triple.id 的有序列表,表示因果顺序.
    """

    id: str = field(default_factory=generate_id)
    chain_id: str = ""  # 归一化 ID
    node_ids: list[str] = field(default_factory=list)
    chain_type: str = "causal"
    confidence: float = 1.0
    support_count: int = 0
    scope: MemoryScope = MemoryScope.PROJECT
    created_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"CausalChain.confidence 必须在 [0,1],得到 {self.confidence}")
        if self.support_count < 0:
            raise ValueError(f"CausalChain.support_count 必须 >= 0,得到 {self.support_count}")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "chain_id": self.chain_id,
            "node_ids": list(self.node_ids),
            "chain_type": self.chain_type,
            "confidence": self.confidence,
            "support_count": self.support_count,
            "scope": str(self.scope),
            "created_at": to_iso(self.created_at),
        }

    @classmethod
    def from_dict(cls, d: dict) -> CausalChain:
        return cls(
            id=d["id"],
            chain_id=d.get("chain_id", ""),
            node_ids=list(d.get("node_ids", [])),
            chain_type=d.get("chain_type", "causal"),
            confidence=float(d.get("confidence", 1.0)),
            support_count=int(d.get("support_count", 0)),
            scope=MemoryScope(d.get("scope", "project")),
            created_at=from_iso(d["created_at"]) if d.get("created_at") else now_utc(),
        )
