"""Unit tests for TelemetryMemoryMiddleware(§15.3 语义层观测包装)."""

from __future__ import annotations

import json

import pytest

from smilex.memory.contracts import RecallRequest, WriteRequest
from smilex.memory.models import MemoryScope
from smilex.memory.observability import Telemetry
from smilex.middlewares._telemetry import TelemetryMemoryMiddleware


@pytest.fixture
async def mw(tmp_path):
    db = tmp_path / "tel.db"
    telemetry = Telemetry.from_config(
        audit=True, db_path=db, audit_reads=True
    )
    middleware = TelemetryMemoryMiddleware(db, telemetry=telemetry)
    middleware.telemetry_channel = "test"
    await middleware.initialize()
    yield middleware
    await middleware.close()


async def test_write_and_recall_produce_metrics(mw):
    resp = await mw.write(
        WriteRequest(scope=MemoryScope.GLOBAL, content="观测测试内容"), session_id="s1"
    )
    assert resp.status.value == "saved"
    await mw.recall(RecallRequest(query="观测"), session_id="s1")

    std = mw.telemetry.standard
    assert std.write_total.value(status="saved") == 1
    assert std.recall_total.value() == 1
    render = mw.telemetry.metrics.render_prometheus()
    assert "smilex_memory_write_latency_seconds_count 1" in render
    assert "smilex_memory_recall_latency_seconds_count 1" in render


async def test_audit_records_without_raw_content(mw, tmp_path):
    await mw.write(
        WriteRequest(scope=MemoryScope.GLOBAL, content="机密原文不得落审计"), session_id="s2"
    )
    audit = tmp_path / "audit-tel.jsonl"
    assert audit.exists()
    lines = audit.read_text(encoding="utf-8").strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    write_events = [e for e in events if e["event"] == "write"]
    assert len(write_events) == 1
    record = write_events[0]
    assert record["session_id"] == "s2"
    assert record["channel"] == "test"
    assert record["status"] == "saved"
    assert record["memory_ids"] == [record["memory_ids"][0]]
    # 不落原文: 只有长度 + 摘要
    assert "机密原文" not in audit.read_text(encoding="utf-8")
    assert record["content_len"] == len("机密原文不得落审计")
    assert len(record["content_sha256"]) == 16
    # audit_reads=True → recall 也记录
    await mw.recall(RecallRequest(query="机密"), session_id="s2")
    events2 = [
        json.loads(ln) for ln in audit.read_text(encoding="utf-8").strip().split("\n")
    ]
    assert any(e["event"] == "recall" for e in events2)


async def test_audit_reads_off_by_default(tmp_path):
    telemetry = Telemetry.from_config(audit=True, db_path=tmp_path / "ro.db")
    middleware = TelemetryMemoryMiddleware(tmp_path / "ro.db", telemetry=telemetry)
    await middleware.initialize()
    # 先写一条(变更事件始终记录)再检索,验证读事件默认不记录
    await middleware.write(
        WriteRequest(scope=MemoryScope.GLOBAL, content="w"), session_id="s"
    )
    await middleware.recall(RecallRequest(query="w"), session_id="s")
    await middleware.close()
    audit = tmp_path / "audit-ro.jsonl"
    events = [
        json.loads(ln) for ln in audit.read_text(encoding="utf-8").strip().split("\n")
    ]
    assert [e["event"] for e in events] == ["write"]


async def test_init_audited_via_initialize_project(mw, tmp_path):
    from smilex.memory.contracts import ProjectInitRequest

    await mw.initialize_project(ProjectInitRequest(name="观测项目"))
    audit = tmp_path / "audit-tel.jsonl"
    events = [
        json.loads(ln) for ln in audit.read_text(encoding="utf-8").strip().split("\n")
    ]
    inits = [e for e in events if e["event"] == "init"]
    assert len(inits) == 1
    assert inits[0]["project"] == "观测项目"


async def test_disabled_telemetry_behaves_like_base(tmp_path):
    """Telemetry.disabled() 下包装类行为与基类一致(指标计数照常,审计无文件)."""
    middleware = TelemetryMemoryMiddleware(tmp_path / "plain.db")
    await middleware.initialize()
    resp = await middleware.write(
        WriteRequest(scope=MemoryScope.GLOBAL, content="无观测模式"), session_id="s"
    )
    assert resp.status.value == "saved"
    assert not (tmp_path / "audit-plain.jsonl").exists()
    # 标准指标仍然计数(无人消费,开销可忽略)
    assert middleware.telemetry.standard.write_total.value(status="saved") == 1
    await middleware.close()


async def test_write_error_counted(mw):
    """PROJECT scope 缺 scope_id → 写路径最前校验抛错 → error 计数 + 审计."""
    with pytest.raises(ValueError):
        await mw.write(
            WriteRequest(scope=MemoryScope.PROJECT, content="x"), session_id="s3"
        )
    assert mw.telemetry.standard.write_total.value(status="error") == 1
