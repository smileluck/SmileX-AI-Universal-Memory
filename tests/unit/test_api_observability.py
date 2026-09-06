"""Unit tests for 可观测性端点(§15.3): /metrics + /api/health 扩展字段."""

from __future__ import annotations

import pytest

from smilex.server.config import ServerConfig

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from smilex.server.app import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    config = ServerConfig(
        db_path=tmp_path / "obs.db", enable_scheduler=False
    )
    app = create_app(config)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_with_scheduler(tmp_path):
    config = ServerConfig(
        db_path=tmp_path / "obs_sched.db", enable_scheduler=True
    )
    app = create_app(config)
    with TestClient(app) as c:
        yield c


def test_metrics_endpoint_prometheus_text(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    # 一次 /api/health 已产生 HTTP 计数
    assert "smilex_http_requests_total" in text
    assert "# TYPE smilex_task_queue_depth gauge" in text
    assert "smilex_db_size_bytes" in text
    # 巡检未跑过 → -1
    assert "smilex_db_integrity_ok -1" in text


def test_metrics_disabled_when_config_off(tmp_path):
    config = ServerConfig(
        db_path=tmp_path / "nometrics.db", enable_scheduler=False, metrics=False
    )
    app = create_app(config)
    with TestClient(app) as c:
        # /metrics 未注册 → 落到 MCP 兜底挂载(404 或 MCP 404 响应)
        assert c.get("/metrics").status_code in (404, 400, 405)


def test_http_counter_path_templating(client):
    client.get("/api/memory/01HZZZZZZZZZZZZZZZZZZZZZZZZ")
    text = client.get("/metrics").text
    assert 'smilex_http_requests_total{method="GET",path="/api/memory/{id}"}' in text
    assert "/api/memory/01HZZ" not in text  # 原始 id 不进 label


def test_health_contract_fields_preserved(client):
    """daemon status 依赖的三契约字段不可破坏."""
    data = client.get("/api/health").json()
    assert data["status"] == "ok"
    assert "started_at" in data and "uptime_s" in data
    assert isinstance(data["uptime_s"], float)


def test_health_extended_fields(client):
    data = client.get("/api/health").json()
    assert data["db"]["connected"] is True  # lifespan 已初始化
    assert data["db"]["size_bytes"] > 0
    assert data["db"]["integrity"] is None  # 巡检未跑
    assert data["scheduler"]["enabled"] is False
    assert data["scheduler"]["queue_depth"] is None
    assert data["alerts"] == []


def test_health_with_scheduler(client_with_scheduler):
    data = client_with_scheduler.get("/api/health").json()
    assert data["scheduler"]["enabled"] is True
    assert data["scheduler"]["queue_depth"] == 0
    assert data["scheduler"]["current"] is None


def test_alerts_on_integrity_failure(client, monkeypatch):
    from smilex.memory.observability import set_integrity_report
    from smilex.memory.observability.telemetry import IntegrityReport

    set_integrity_report(
        IntegrityReport(ok=False, checked_at="2026-01-01T00:00:00Z", detail="bad page")
    )
    try:
        data = client.get("/api/health").json()
        names = [a["name"] for a in data["alerts"]]
        assert "db_integrity" in names
        crit = [a for a in data["alerts"] if a["name"] == "db_integrity"][0]
        assert crit["severity"] == "critical"
    finally:
        monkeypatch.setattr(
            "smilex.memory.observability.telemetry._last_integrity", None
        )


def test_recall_test_increments_metrics(client):
    client.post("/api/recall-test", json={"query": "可观测性"})
    text = client.get("/metrics").text
    assert "smilex_memory_recall_total 1" in text
    assert "smilex_memory_recall_latency_seconds_count 1" in text


def test_scheduler_observer_counts_terminal_tasks(tmp_path):
    """调度终态观察者: smilex_task_total{name,status} 计数."""
    from smilex.memory.observability import Telemetry, make_scheduler_observer
    from smilex.memory.scheduler import (
        MemoryTaskScheduler,
        SchedulerConfig,
        TaskDefinition,
        TaskPriority,
    )
    from smilex.memory.storage.storage_engine import StorageEngine

    async def run():
        engine = StorageEngine(":memory:", load_vec=False)
        await engine.initialize()
        try:
            telemetry = Telemetry.disabled()
            observer = make_scheduler_observer(telemetry)
            scheduler = MemoryTaskScheduler(
                engine,
                SchedulerConfig(tick_interval=0.01, grace_timeout=1.0),
                observer=observer,
            )
            scheduler.register(
                TaskDefinition(name="ok_task", run=_ok_run, priority=TaskPriority.LOW)
            )
            scheduler.register(
                TaskDefinition(name="boom", run=_boom_run, priority=TaskPriority.LOW)
            )
            await scheduler.start()
            try:
                await scheduler.submit("ok_task")
                await scheduler.submit("boom")
                import asyncio

                for _ in range(400):
                    if len(scheduler.recent_tasks) >= 2:
                        break
                    await asyncio.sleep(0.01)
                task_total = telemetry.metrics.counter(
                    "smilex_task_total", "h", ("name", "status")
                )
                assert task_total.value(name="ok_task", status="completed") == 1
                assert task_total.value(name="boom", status="failed") == 1
            finally:
                await scheduler.stop()
        finally:
            await engine.close()

    import asyncio

    asyncio.run(run())


async def _ok_run(ctx, payload):
    return {"ok": True}


async def _boom_run(ctx, payload):
    raise RuntimeError("boom")
