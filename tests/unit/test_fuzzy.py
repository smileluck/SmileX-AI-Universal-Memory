"""Unit tests for fuzzy data models (UncertainValue/TimeRange/FuzzyLocation/FuzzyMemory)."""

from datetime import UTC, datetime

import numpy as np
import pytest

from smilex.memory.models import (
    CertaintyLevel,
    FuzzyLocation,
    FuzzyMemory,
    MemoryLayer,
    MemoryScope,
    TimeRange,
    UncertainValue,
)
from smilex.utils.ids import is_valid_id

# ---------- UncertainValue ----------

def test_uncertain_value_defaults():
    v = UncertainValue(value=42)
    assert v.value == 42
    assert v.certainty is CertaintyLevel.MEDIUM
    assert v.note is None


def test_uncertain_value_roundtrip():
    v = UncertainValue(value="hello", certainty=CertaintyLevel.HIGH, note="test")
    d = v.to_dict()
    v2 = UncertainValue.from_dict(d)
    assert v == v2


# ---------- TimeRange ----------

def test_time_range_exact():
    t = TimeRange(exact=datetime.now(UTC))
    assert t.exact is not None


def test_time_range_approx():
    t = TimeRange(approx_start=datetime.now(UTC), approx_end=datetime.now(UTC))
    assert t.approx_start is not None
    assert t.approx_end is not None


def test_time_range_relative():
    t = TimeRange(relative="昨天下午")
    assert t.relative == "昨天下午"


def test_time_range_rejects_exact_and_approx():
    """§5.2: exact 和 approx_start/approx_end 互斥."""
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="不能同时设置"):
        TimeRange(exact=now, approx_start=now)


def test_time_range_roundtrip_exact():
    t = TimeRange(exact=datetime.now(UTC), certainty=CertaintyLevel.HIGH)
    d = t.to_dict()
    assert isinstance(d["exact"], str)
    assert d["exact"].endswith("Z")
    t2 = TimeRange.from_dict(d)
    assert t == t2


def test_time_range_roundtrip_relative():
    t = TimeRange(relative="刚才", certainty=CertaintyLevel.LOW)
    d = t.to_dict()
    t2 = TimeRange.from_dict(d)
    assert t == t2


# ---------- FuzzyLocation ----------

def test_fuzzy_location_defaults():
    loc = FuzzyLocation()
    assert loc.exact_coords is None
    assert loc.hierarchy == []
    assert loc.certainty is CertaintyLevel.MEDIUM


def test_fuzzy_location_exact_coords():
    loc = FuzzyLocation(exact_coords=(116.397, 39.916))
    assert loc.exact_coords == (116.397, 39.916)


def test_fuzzy_location_hierarchy():
    loc = FuzzyLocation(hierarchy=["家", "客厅"])
    assert loc.hierarchy == ["家", "客厅"]


def test_fuzzy_location_roundtrip():
    loc = FuzzyLocation(
        exact_coords=(116.397, 39.916),
        hierarchy=["北京", "天安门"],
        region="北京",
        certainty=CertaintyLevel.HIGH,
    )
    d = loc.to_dict()
    # exact_coords 应该序列化为 list(msgpack/json 兼容)
    assert d["exact_coords"] == [116.397, 39.916]
    loc2 = FuzzyLocation.from_dict(d)
    assert loc2.exact_coords == (116.397, 39.916)
    assert loc2.hierarchy == ["北京", "天安门"]


# ---------- FuzzyMemory ----------

def test_fuzzy_memory_defaults():
    """§5.2: FuzzyMemory 所有字段可选,强制渐进式存储."""
    m = FuzzyMemory()
    assert is_valid_id(m.id)
    assert m.content is None
    assert m.content_embedding is None
    assert m.entities == []
    assert m.scope is MemoryScope.PROJECT
    assert m.importance == 0.5
    assert m.layer is MemoryLayer.L1_SHORT
    assert m.created_at <= m.updated_at


def test_fuzzy_memory_with_content():
    m = FuzzyMemory(
        content="今天见到了小明",
        entities=["person:小明"],
        importance=0.8,
    )
    assert m.content == "今天见到了小明"
    assert m.importance == 0.8


def test_fuzzy_memory_with_embedding():
    emb = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    m = FuzzyMemory(content="test", content_embedding=emb)
    assert m.content_embedding is not None
    assert m.content_embedding.dtype == np.float32


def test_fuzzy_memory_touch_updates_timestamp():
    import time as _time

    m = FuzzyMemory()
    old = m.updated_at
    _time.sleep(0.01)
    m.touch()
    assert m.updated_at > old


def test_fuzzy_memory_increment_access():
    m = FuzzyMemory()
    assert m.access_count == 0
    m.increment_access()
    m.increment_access()
    assert m.access_count == 2


def test_fuzzy_memory_roundtrip_minimal():
    m = FuzzyMemory(content="hello")
    d = m.to_dict()
    assert d["content"] == "hello"
    assert d["scope"] == "project"
    assert d["layer"] == "L1"
    m2 = FuzzyMemory.from_dict(d)
    assert m2.id == m.id
    assert m2.content == m.content
    assert m2.scope == m.scope


def test_fuzzy_memory_roundtrip_complete():
    """完整字段的 FuzzyMemory 往返一致."""
    m = FuzzyMemory(
        content="完整记忆",
        content_embedding=np.array([0.1, 0.2], dtype=np.float32),
        time_range=TimeRange(exact=datetime.now(UTC)),
        location=FuzzyLocation(hierarchy=["家"]),
        entities=["a", "b"],
        relations=["r1"],
        scope=MemoryScope.GLOBAL,
        importance=0.9,
        emotion_weight=0.3,
        access_count=5,
        layer=MemoryLayer.L2_LONG,
        expires_at=datetime.now(UTC),
    )
    d = m.to_dict()
    m2 = FuzzyMemory.from_dict(d)
    assert m2.content == m.content
    assert m2.scope == m.scope
    assert m2.importance == m.importance
    assert m2.layer == m.layer
    assert m2.access_count == m.access_count
    assert list(m2.entities) == m.entities
    assert m2.time_range is not None
    assert m2.location is not None
    assert m2.content_embedding is not None
    assert m2.expires_at is not None


def test_fuzzy_memory_id_is_ulid():
    """FuzzyMemory.id 默认是 ULID(26 字符)."""
    m = FuzzyMemory()
    assert len(m.id) == 26
    assert is_valid_id(m.id)
