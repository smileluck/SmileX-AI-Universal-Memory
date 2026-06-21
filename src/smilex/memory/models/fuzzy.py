"""Fuzzy data models — UncertainValue / TimeRange / FuzzyLocation / FuzzyMemory.

Per main doc §5.2:
- All fields optional → 强制渐进式存储(「不完美的记忆 > 没有记忆」)
- StrEnum serialization
- ULID default for id
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from ...utils.ids import generate_id
from ...utils.timeutil import from_iso, now_utc, to_iso
from .enums import CertaintyLevel, MemoryLayer, MemoryScope


@dataclass
class UncertainValue:
    """带确定性级别的值."""

    value: Any
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "certainty": str(self.certainty),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> UncertainValue:
        return cls(
            value=d.get("value"),
            certainty=CertaintyLevel(d.get("certainty", "medium")),
            note=d.get("note"),
        )


@dataclass
class TimeRange:
    """时间范围 — 三种模式: 精确 / 近似 / 相对.

    只能设置 exact 或 (approx_start + approx_end) 之一,relative 独立.
    """

    exact: datetime | None = None
    approx_start: datetime | None = None
    approx_end: datetime | None = None
    relative: str | None = None  # "昨天下午"、"刚才"
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM

    def __post_init__(self) -> None:
        has_approx = self.approx_start is not None or self.approx_end is not None
        if self.exact is not None and has_approx:
            raise ValueError("TimeRange: 不能同时设置 exact 和 approx_start/approx_end")

    def to_dict(self) -> dict:
        return {
            "exact": to_iso(self.exact) if self.exact else None,
            "approx_start": to_iso(self.approx_start) if self.approx_start else None,
            "approx_end": to_iso(self.approx_end) if self.approx_end else None,
            "relative": self.relative,
            "certainty": str(self.certainty),
        }

    @classmethod
    def from_dict(cls, d: dict) -> TimeRange:
        return cls(
            exact=from_iso(d["exact"]) if d.get("exact") else None,
            approx_start=from_iso(d["approx_start"]) if d.get("approx_start") else None,
            approx_end=from_iso(d["approx_end"]) if d.get("approx_end") else None,
            relative=d.get("relative"),
            certainty=CertaintyLevel(d.get("certainty", "medium")),
        )


@dataclass
class FuzzyLocation:
    """位置 — 三种模式: 精确坐标 / 层级 / 区域."""

    exact_coords: tuple[float, float] | None = None
    hierarchy: list[str] = field(default_factory=list)  # ["家", "客厅"]
    region: str | None = None
    location_id: str | None = None
    certainty: CertaintyLevel = CertaintyLevel.MEDIUM

    def to_dict(self) -> dict:
        return {
            "exact_coords": list(self.exact_coords) if self.exact_coords else None,
            "hierarchy": list(self.hierarchy),
            "region": self.region,
            "location_id": self.location_id,
            "certainty": str(self.certainty),
        }

    @classmethod
    def from_dict(cls, d: dict) -> FuzzyLocation:
        coords = d.get("exact_coords")
        return cls(
            exact_coords=tuple(coords) if coords else None,
            hierarchy=list(d.get("hierarchy", [])),
            region=d.get("region"),
            location_id=d.get("location_id"),
            certainty=CertaintyLevel(d.get("certainty", "medium")),
        )


@dataclass
class FuzzyMemory:
    """核心记忆单元 — 所有字段可选,强制渐进式存储."""

    id: str = field(default_factory=generate_id)
    content: str | None = None
    content_embedding: np.ndarray | None = None
    time_range: TimeRange | None = None
    location: FuzzyLocation | None = None
    entities: list[str] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    scope: MemoryScope = MemoryScope.PROJECT
    importance: float = 0.5
    emotion_weight: float = 0.0
    access_count: int = 0
    layer: MemoryLayer = MemoryLayer.L1_SHORT
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)
    expires_at: datetime | None = None

    def touch(self) -> None:
        """更新 updated_at 时间戳(用于写入后调用)."""
        self.updated_at = now_utc()

    def increment_access(self) -> None:
        """访问计数 +1,同时更新 updated_at."""
        self.access_count += 1
        self.touch()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "content_embedding": (
                self.content_embedding.tolist() if self.content_embedding is not None else None
            ),
            "time_range": self.time_range.to_dict() if self.time_range else None,
            "location": self.location.to_dict() if self.location else None,
            "entities": list(self.entities),
            "relations": list(self.relations),
            "scope": str(self.scope),
            "importance": self.importance,
            "emotion_weight": self.emotion_weight,
            "access_count": self.access_count,
            "layer": str(self.layer),
            "created_at": to_iso(self.created_at),
            "updated_at": to_iso(self.updated_at),
            "expires_at": to_iso(self.expires_at) if self.expires_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> FuzzyMemory:
        emb = d.get("content_embedding")
        return cls(
            id=d["id"],
            content=d.get("content"),
            content_embedding=np.array(emb, dtype=np.float32) if emb else None,
            time_range=TimeRange.from_dict(d["time_range"]) if d.get("time_range") else None,
            location=FuzzyLocation.from_dict(d["location"]) if d.get("location") else None,
            entities=list(d.get("entities", [])),
            relations=list(d.get("relations", [])),
            scope=MemoryScope(d.get("scope", "project")),
            importance=float(d.get("importance", 0.5)),
            emotion_weight=float(d.get("emotion_weight", 0.0)),
            access_count=int(d.get("access_count", 0)),
            layer=MemoryLayer(d.get("layer", "L1")),
            created_at=from_iso(d["created_at"]) if d.get("created_at") else now_utc(),
            updated_at=from_iso(d["updated_at"]) if d.get("updated_at") else now_utc(),
            expires_at=from_iso(d["expires_at"]) if d.get("expires_at") else None,
        )
