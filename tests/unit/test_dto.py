"""Unit tests for MemoryMiddleware DTOs (src/smilex/middlewares/dto.py)."""

from datetime import UTC, datetime

import pytest

from smilex.memory.models import (
    CertaintyLevel,
    ConflictType,
    FuzzyLocation,
    MemoryLayer,
    MemoryScope,
    ScopeFilter,
    TimeRange,
)
from smilex.middlewares import (
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
from smilex.utils.timeutil import to_iso

# ---------- Enums ----------


def test_write_status_values():
    assert WriteStatus.SAVED == "saved"
    assert WriteStatus.CONFLICT == "conflict"
    assert WriteStatus.PARTIAL == "partial"


def test_bootstrap_stage_values():
    assert BootstrapStage.ONBOARDING == "onboarding"
    assert BootstrapStage.TEMPLATE == "template"
    assert BootstrapStage.ACTIVE_LEARNING == "active_learning"
    assert BootstrapStage.READY == "ready"


def test_project_templates():
    assert PROJECT_TEMPLATES == ("web", "cli", "data", "agent")


# ---------- TripleInput ----------


def test_triple_input_defaults():
    t = TripleInput(predicate="uses", object_value="SQLite")
    assert t.subject_id is None
    assert t.subject_name is None
    assert t.object_id is None
    assert t.certainty is CertaintyLevel.MEDIUM


def test_triple_input_requires_predicate():
    with pytest.raises(ValueError, match="predicate"):
        TripleInput(object_value="x")


def test_triple_input_requires_object():
    with pytest.raises(ValueError, match="object_id 或 object_value"):
        TripleInput(predicate="uses")


def test_triple_input_roundtrip():
    t = TripleInput(
        subject_name="本项目",
        predicate="uses",
        object_id="tech:sqlite",
        certainty=CertaintyLevel.HIGH,
    )
    assert TripleInput.from_dict(t.to_dict()) == t


# ---------- ConflictInfo ----------


def test_conflict_info_defaults():
    c = ConflictInfo()
    assert c.conflict_type is ConflictType.WRITE_WRITE
    assert c.conflicting_memory_id is None
    assert c.message is None


def test_conflict_info_roundtrip():
    c = ConflictInfo(
        conflict_type=ConflictType.VERSION_STALE,
        conflicting_memory_id="01J",
        message="版本过期",
    )
    assert ConflictInfo.from_dict(c.to_dict()) == c


# ---------- WriteRequest / WriteResponse ----------


def test_write_request_defaults():
    w = WriteRequest(scope=MemoryScope.PROJECT, content="使用 SQLite 存储")
    assert w.entities == []
    assert w.relations == []
    assert w.time_range is None
    assert w.location is None
    assert w.certainty is CertaintyLevel.MEDIUM
    assert w.importance == 0.5


def test_write_request_requires_content():
    with pytest.raises(ValueError, match="content"):
        WriteRequest(scope=MemoryScope.GLOBAL, content="")


def test_write_request_importance_range():
    with pytest.raises(ValueError, match="importance"):
        WriteRequest(scope=MemoryScope.GLOBAL, content="x", importance=1.5)


def test_write_request_roundtrip():
    w = WriteRequest(
        scope=MemoryScope.TENANT,
        content="FastAPI 依赖注入模式",
        entities=["FastAPI"],
        relations=[TripleInput(predicate="uses", object_value="di")],
        time_range=TimeRange(exact=datetime.now(UTC)),
        location=FuzzyLocation(region="华东"),
        certainty=CertaintyLevel.EXACT,
        importance=0.9,
    )
    assert WriteRequest.from_dict(w.to_dict()) == w


def test_write_response_defaults():
    r = WriteResponse(memory_id="01J")
    assert r.status is WriteStatus.SAVED
    assert r.layers_affected == []
    assert r.conflict is None
    assert r.written_at is not None


def test_write_response_roundtrip():
    r = WriteResponse(
        memory_id="01J",
        status=WriteStatus.CONFLICT,
        layers_affected=[MemoryLayer.L1_SHORT, MemoryLayer.L2_LONG],
        conflict=ConflictInfo(message="写冲突"),
    )
    assert WriteResponse.from_dict(r.to_dict()) == r


def test_write_response_written_at_serializes_utc():
    r = WriteResponse(memory_id="01J")
    assert to_iso(r.written_at).endswith("Z")


# ---------- MemoryRef ----------


def test_memory_ref_defaults():
    m = MemoryRef(id="01J", layer=MemoryLayer.L2_LONG, score=0.8)
    assert m.scope is MemoryScope.PROJECT
    assert m.snippet is None


def test_memory_ref_score_range():
    with pytest.raises(ValueError, match="score"):
        MemoryRef(id="01J", layer=MemoryLayer.L1_SHORT, score=1.2)


def test_memory_ref_roundtrip():
    m = MemoryRef(
        id="01J",
        layer=MemoryLayer.L1_SHORT,
        score=0.95,
        scope=MemoryScope.GLOBAL,
        snippet="SQLite WAL 模式...",
    )
    assert MemoryRef.from_dict(m.to_dict()) == m


# ---------- RecallRequest / RecallResponse ----------


def test_recall_request_defaults():
    r = RecallRequest(query="SQLite 怎么配置?")
    assert r.scope_filter == ScopeFilter()
    assert r.top_k == 10
    assert r.token_budget == 4000
    assert r.entity_filter is None


def test_recall_request_requires_query():
    with pytest.raises(ValueError, match="query"):
        RecallRequest(query="")


def test_recall_request_validates_top_k_and_budget():
    with pytest.raises(ValueError, match="top_k"):
        RecallRequest(query="q", top_k=0)
    with pytest.raises(ValueError, match="token_budget"):
        RecallRequest(query="q", token_budget=-1)


def test_recall_request_roundtrip():
    r = RecallRequest(
        query="认证方案",
        scope_filter=ScopeFilter(include_project="project:abc"),
        time_range=TimeRange(relative="昨天"),
        entity_filter="auth",
        top_k=5,
        token_budget=2000,
    )
    assert RecallRequest.from_dict(r.to_dict()) == r


def test_recall_response_defaults():
    r = RecallResponse(context="")
    assert r.sources == []
    assert r.layers_used == []
    assert r.token_count == 0


def test_recall_response_roundtrip():
    r = RecallResponse(
        context="上下文内容",
        sources=[MemoryRef(id="01J", layer=MemoryLayer.L0_WORKING, score=0.7)],
        layers_used=[MemoryLayer.L0_WORKING, MemoryLayer.L1_SHORT],
        token_count=1234,
    )
    assert RecallResponse.from_dict(r.to_dict()) == r


# ---------- ProjectInitRequest / ProjectInitResponse ----------


def test_project_init_request_defaults():
    r = ProjectInitRequest(name="smilex")
    assert r.description == ""
    assert r.tech_stack == []
    assert r.readme_content is None
    assert r.key_files == {}
    assert r.template is None


def test_project_init_request_requires_name():
    with pytest.raises(ValueError, match="name"):
        ProjectInitRequest(name="")


def test_project_init_request_validates_template():
    with pytest.raises(ValueError, match="template"):
        ProjectInitRequest(name="x", template="unknown")


def test_project_init_request_roundtrip():
    r = ProjectInitRequest(
        name="smilex",
        description="记忆系统",
        tech_stack=["python", "sqlite"],
        readme_content="# SmileX",
        key_files={"README.md": "..."},
        template="agent",
    )
    assert ProjectInitRequest.from_dict(r.to_dict()) == r


def test_project_init_response_defaults():
    r = ProjectInitResponse(scope="project:01J")
    assert r.entity_count == 0
    assert r.triple_count == 0
    assert r.stage is BootstrapStage.ONBOARDING
    assert r.elapsed_ms == 0


def test_project_init_response_roundtrip():
    r = ProjectInitResponse(
        scope="project:01J",
        entity_count=50,
        triple_count=200,
        stage=BootstrapStage.READY,
        elapsed_ms=1234,
    )
    assert ProjectInitResponse.from_dict(r.to_dict()) == r


# ---------- ProjectContext ----------


def test_project_context_defaults():
    c = ProjectContext(scope="project:01J", name="smilex")
    assert c.stage is BootstrapStage.ONBOARDING
    assert c.tech_stack == []
    assert c.entity_count == 0
    assert c.triple_count == 0
    assert c.created_at is not None


def test_project_context_roundtrip():
    c = ProjectContext(
        scope="project:01J",
        name="smilex",
        stage=BootstrapStage.ACTIVE_LEARNING,
        tech_stack=["python"],
        entity_count=10,
        triple_count=20,
    )
    assert ProjectContext.from_dict(c.to_dict()) == c
