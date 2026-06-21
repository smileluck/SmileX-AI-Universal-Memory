"""Unit tests for Layer 0 enums."""

import json

from smilex.memory.models import (
    CertaintyLevel,
    ConflictType,
    LockType,
    MemoryLayer,
    MemoryScope,
    PreemptionPolicy,
)


def test_certainty_level_values():
    assert CertaintyLevel.EXACT == "exact"
    assert CertaintyLevel.HIGH == "high"
    assert CertaintyLevel.MEDIUM == "medium"
    assert CertaintyLevel.LOW == "low"
    assert CertaintyLevel.UNKNOWN == "unknown"


def test_memory_layer_values():
    assert MemoryLayer.L0_WORKING == "L0"
    assert MemoryLayer.L1_SHORT == "L1"
    assert MemoryLayer.L2_LONG == "L2"
    assert MemoryLayer.L3_SEMANTIC == "L3"


def test_memory_scope_values():
    assert MemoryScope.PROJECT == "project"
    assert MemoryScope.TENANT == "tenant"
    assert MemoryScope.GLOBAL == "global"


def test_lock_type_values():
    assert LockType.SHARED == "shared"
    assert LockType.EXCLUSIVE == "exclusive"
    assert LockType.UPDATE == "update"


def test_conflict_type_values():
    assert len(list(ConflictType)) == 5
    assert ConflictType.CAUSAL_CONTRADICTION == "causal_contradiction"


def test_preemption_policy_values():
    assert len(list(PreemptionPolicy)) == 5
    assert PreemptionPolicy.GRACEFUL == "graceful"


def test_strenum_json_serializable():
    """StrEnum 应该作为字符串直接 JSON 序列化."""
    data = {
        "scope": MemoryScope.GLOBAL,
        "certainty": CertaintyLevel.HIGH,
    }
    s = json.dumps(data)
    assert '"global"' in s
    assert '"high"' in s
    # 反序列化
    parsed = json.loads(s)
    assert parsed["scope"] == "global"
    assert MemoryScope(parsed["scope"]) is MemoryScope.GLOBAL


def test_strenum_from_string():
    """从字符串构造 StrEnum."""
    assert CertaintyLevel("medium") is CertaintyLevel.MEDIUM
    assert MemoryLayer("L2") is MemoryLayer.L2_LONG
