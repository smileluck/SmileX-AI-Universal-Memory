"""Unit tests for graph models (Entity/Triple/CausalChain)."""

from datetime import UTC, datetime

import numpy as np
import pytest

from smilex.memory.models import (
    CausalChain,
    CertaintyLevel,
    Entity,
    MemoryScope,
    Triple,
)
from smilex.utils.ids import is_valid_id

# ---------- Entity ----------

def test_entity_defaults():
    e = Entity()
    assert is_valid_id(e.id)
    assert e.entity_id == ""
    assert e.entity_type == ""
    assert e.scope is MemoryScope.PROJECT
    assert e.valid_to is None
    assert e.location is None
    assert e.embedding is None
    assert e.source_closet is None


def test_entity_with_fields():
    e = Entity(
        entity_id="person:alice",
        entity_type="person",
        name="Alice",
        scope=MemoryScope.GLOBAL,
    )
    assert e.entity_id == "person:alice"
    assert e.entity_type == "person"
    assert e.name == "Alice"
    assert e.scope is MemoryScope.GLOBAL


def test_entity_roundtrip():
    e = Entity(
        entity_id="concept:redis",
        entity_type="concept",
        name="Redis",
        scope=MemoryScope.GLOBAL,
        location=None,
        embedding=np.array([0.5, 0.5], dtype=np.float32),
        source_closet="src:abc",
    )
    d = e.to_dict()
    e2 = Entity.from_dict(d)
    assert e2.id == e.id
    assert e2.entity_id == e.entity_id
    assert e2.name == e.name
    assert e2.source_closet == e.source_closet
    assert e2.embedding is not None


# ---------- Triple ----------

def test_triple_requires_object():
    """§5.2: Triple 必须有 object_id 或 object_value."""
    with pytest.raises(ValueError, match="object_id 或 object_value"):
        Triple(subject_id="a", predicate="knows")


def test_triple_with_object_id():
    t = Triple(subject_id="a", predicate="knows", object_id="b")
    assert t.object_id == "b"
    assert t.object_value is None


def test_triple_with_object_value():
    t = Triple(subject_id="a", predicate="age", object_value="30")
    assert t.object_value == "30"
    assert t.object_id is None


def test_triple_confidence_validation():
    with pytest.raises(ValueError, match="confidence"):
        Triple(subject_id="a", predicate="p", object_value="x", confidence=1.5)
    with pytest.raises(ValueError, match="confidence"):
        Triple(subject_id="a", predicate="p", object_value="x", confidence=-0.1)


def test_triple_causal_level_validation():
    with pytest.raises(ValueError, match="causal_level"):
        Triple(subject_id="a", predicate="p", object_value="x", causal_level=-1)


def test_triple_defaults():
    t = Triple(subject_id="a", predicate="p", object_value="x")
    assert t.relation_type == "semantic"
    assert t.causal_level == 0
    assert t.confidence == 1.0
    assert t.certainty is CertaintyLevel.EXACT


def test_triple_roundtrip():
    t = Triple(
        triple_id="triple:1",
        subject_id="person:alice",
        predicate="knows",
        object_id="person:bob",
        scope=MemoryScope.GLOBAL,
        predecessor_id="triple:0",
        causal_level=2,
        confidence=0.85,
        certainty=CertaintyLevel.HIGH,
        relation_type="causal",
        source_closet="src:def",
    )
    d = t.to_dict()
    t2 = Triple.from_dict(d)
    assert t2.id == t.id
    assert t2.triple_id == t.triple_id
    assert t2.subject_id == t.subject_id
    assert t2.predecessor_id == t.predecessor_id
    assert t2.causal_level == t.causal_level
    assert t2.confidence == t.confidence
    assert t2.relation_type == t.relation_type


def test_triple_valid_from_defaults_to_now():
    """Triple.valid_from 默认为当前 UTC 时间."""
    before = datetime.now(UTC)
    t = Triple(subject_id="a", predicate="p", object_value="x")
    after = datetime.now(UTC)
    assert before <= t.valid_from <= after


# ---------- CausalChain ----------

def test_causal_chain_defaults():
    c = CausalChain()
    assert is_valid_id(c.id)
    assert c.node_ids == []
    assert c.chain_type == "causal"
    assert c.confidence == 1.0
    assert c.support_count == 0


def test_causal_chain_with_nodes():
    c = CausalChain(
        chain_id="chain:1",
        node_ids=["triple:1", "triple:2", "triple:3"],
        confidence=0.7,
        support_count=3,
    )
    assert len(c.node_ids) == 3


def test_causal_chain_confidence_validation():
    with pytest.raises(ValueError, match="confidence"):
        CausalChain(confidence=2.0)


def test_causal_chain_support_count_validation():
    with pytest.raises(ValueError, match="support_count"):
        CausalChain(support_count=-5)


def test_causal_chain_roundtrip():
    c = CausalChain(
        chain_id="chain:1",
        node_ids=["t1", "t2", "t3"],
        chain_type="causal",
        confidence=0.9,
        support_count=5,
        scope=MemoryScope.GLOBAL,
    )
    d = c.to_dict()
    c2 = CausalChain.from_dict(d)
    assert c2.id == c.id
    assert c2.chain_id == c.chain_id
    assert c2.node_ids == c.node_ids
    assert c2.confidence == c.confidence
