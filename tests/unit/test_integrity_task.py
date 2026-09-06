"""Unit tests for db_integrity 巡检任务(§15.3 SQLite 监控)."""

from __future__ import annotations

import pytest

import smilex.memory.observability.telemetry as telemetry_mod
from smilex.memory.observability.telemetry import get_integrity_report
from smilex.memory.scheduler.checkpoint import CheckpointStore, InterruptContext
from smilex.memory.scheduler.tasks import db_integrity
from smilex.memory.storage.storage_engine import StorageEngine


@pytest.fixture(autouse=True)
def _preserve_report():
    """巡检报告是进程级状态,测试间隔离(保存-恢复)."""
    saved = telemetry_mod._last_integrity
    telemetry_mod._last_integrity = None
    yield
    telemetry_mod._last_integrity = saved


@pytest.fixture
async def engine(tmp_path):
    eng = StorageEngine(tmp_path / "integ.db", load_vec=False)
    await eng.initialize()
    yield eng
    await eng.close()


async def test_quick_check_ok_writes_report(engine, tmp_path):
    ctx = InterruptContext("task-integ", CheckpointStore(engine))
    stats = await db_integrity(engine, ctx, {})
    assert stats["quick_check"] == "ok"
    assert stats["db_size_bytes"] > 0  # 文件库(tmp_path)非 :memory:
    assert stats["page_count"] > 0
    report = get_integrity_report()
    assert report is not None and report.ok
    assert report.checked_at.endswith("Z")
    assert report.db_size_bytes == stats["db_size_bytes"]


async def test_report_initially_absent():
    assert get_integrity_report() is None


async def test_registered_as_core_task(engine):
    from smilex.memory.scheduler import MemoryTaskScheduler, SchedulerConfig
    from smilex.memory.scheduler.tasks import CORE_TASK_NAMES, register_core_tasks

    assert "db_integrity" in CORE_TASK_NAMES
    scheduler = MemoryTaskScheduler(
        engine, SchedulerConfig(tick_interval=0.01, grace_timeout=1.0)
    )
    register_core_tasks(scheduler, engine)
    assert scheduler.has_task("db_integrity")
    await scheduler.stop()
