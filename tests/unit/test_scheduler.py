"""Unit tests for Layer 3a 三重触发调度器(§8).

验证:
- 定时/事件/自适应三种触发各至少一例
- 自适应规则冷却: 触发后冷却期内不再触发(§8.3)
- 优先级调度顺序(CRITICAL > HIGH > LOW)
- 串行执行(单写者约束): 并发度恒为 1
- 同名任务去重 / 未注册任务报错 / 失败任务状态收敛
"""

from __future__ import annotations

import asyncio

import pytest

from smilex.memory.scheduler import (
    AdaptiveRule,
    AdaptiveTrigger,
    MemoryTaskScheduler,
    SchedulerConfig,
    TaskDefinition,
    TaskPriority,
    TaskStatus,
    TriggerType,
)
from smilex.memory.storage.storage_engine import StorageEngine


@pytest.fixture
async def engine():
    eng = StorageEngine(":memory:", load_vec=False)
    await eng.initialize()
    yield eng
    await eng.close()


@pytest.fixture
async def scheduler(engine):
    sched = MemoryTaskScheduler(
        engine, SchedulerConfig(tick_interval=0.01, grace_timeout=1.0)
    )
    await sched.start()
    yield sched
    await sched.stop()


def _recorder(log: list, name: str):
    """生成一个记录调用的任务执行函数."""

    async def run(ctx, payload):
        log.append((name, payload))

    return run


async def _wait_until(pred, timeout: float = 2.0) -> None:
    """轮询等待条件满足(触发器按 tick 出队,需跨 tick 等待)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "条件未在超时内满足"
        await asyncio.sleep(0.005)


# ---------- 定时触发 ----------

async def test_time_trigger_fires(scheduler):
    """定时触发: fire_immediately 的任务在首个 tick 出队执行."""
    log = []
    scheduler.register(TaskDefinition(name="nightly", run=_recorder(log, "nightly")))
    scheduler.add_time_trigger(
        "nightly", interval_seconds=60.0, priority=TaskPriority.LOW, fire_immediately=True
    )
    await _wait_until(lambda: log)
    await scheduler.wait_idle(timeout=2.0)
    assert [n for n, _ in log] == ["nightly"]


async def test_time_trigger_interval_drain():
    """TimeTrigger.drain 按间隔补发到期的任务."""
    from smilex.memory.scheduler import TimeTrigger

    trigger = TimeTrigger("t", 10.0)
    assert trigger.drain(now=trigger._next_fire - 1) == []  # 未到期
    tasks = trigger.drain(now=trigger._next_fire + 15)  # 跨 1 个间隔 → 2 次
    assert len(tasks) == 2
    assert all(t.trigger_type is TriggerType.TIME for t in tasks)


# ---------- 事件触发 ----------

async def test_event_trigger_fires(scheduler):
    """事件触发: emit 命中映射即执行,事件载荷透传."""
    log = []
    scheduler.register(TaskDefinition(name="consolidate", run=_recorder(log, "consolidate")))
    scheduler.add_event_mapping("memory_full", "consolidate", TaskPriority.HIGH)
    task_id = await scheduler.emit("memory_full", {"usage": 0.9})
    assert task_id is not None
    await scheduler.wait_idle(timeout=2.0)
    assert log == [("consolidate", {"event_type": "memory_full", "usage": 0.9})]
    task = scheduler.get_status(task_id)
    assert task.status is TaskStatus.COMPLETED
    assert task.trigger_type is TriggerType.EVENT


async def test_event_trigger_unknown_event(scheduler):
    """未映射的事件不触发."""
    assert await scheduler.emit("no_such_event") is None


# ---------- 自适应触发 + 冷却 ----------

async def test_adaptive_trigger_fires(scheduler):
    """自适应触发: 条件满足时触发,载荷带规则名."""
    log = []
    fired = {"pressure": True}
    scheduler.register(TaskDefinition(name="consolidate", run=_recorder(log, "consolidate")))
    scheduler.add_adaptive_rule(
        AdaptiveRule(
            name="memory_pressure",
            task_name="consolidate",
            condition=lambda: fired["pressure"],
            cooldown_seconds=60.0,
            priority=TaskPriority.HIGH,
        )
    )
    await _wait_until(lambda: log)
    await scheduler.wait_idle(timeout=2.0)
    assert log == [("consolidate", {"rule": "memory_pressure"})]


async def test_adaptive_rule_cooldown():
    """冷却: 触发后冷却期内不再触发,冷却结束后可再次触发(§8.3)."""
    trigger = AdaptiveTrigger(
        [
            AdaptiveRule(
                name="idle_consolidate",
                task_name="consolidate",
                condition=lambda: True,
                cooldown_seconds=60.0,
            )
        ]
    )
    t0 = 1000.0
    first = trigger.drain(now=t0)
    assert len(first) == 1  # 首次触发
    assert trigger.drain(now=t0 + 30) == []  # 冷却期内不触发
    assert trigger.drain(now=t0 + 59.9) == []
    second = trigger.drain(now=t0 + 60.0)  # 冷却结束再次触发
    assert len(second) == 1


async def test_adaptive_rule_condition_false_no_fire():
    """条件不满足时不触发,且不进入冷却."""
    trigger = AdaptiveTrigger(
        [
            AdaptiveRule(
                name="r",
                task_name="t",
                condition=lambda: False,
                cooldown_seconds=60.0,
            )
        ]
    )
    assert trigger.drain(now=0.0) == []
    assert trigger.drain(now=1.0) == []


# ---------- 优先级与串行执行 ----------

async def test_priority_order(engine):
    """优先级: 同级 FIFO,CRITICAL 最先(启动前堆积的任务按优先级出队)."""
    log = []
    sched = MemoryTaskScheduler(engine, SchedulerConfig(tick_interval=0.01))
    for name, prio in [
        ("low_task", TaskPriority.LOW),
        ("high_task", TaskPriority.HIGH),
        ("critical_task", TaskPriority.CRITICAL),
    ]:
        sched.register(TaskDefinition(name=name, run=_recorder(log, name)))
        await sched.submit(name, priority=prio)
    await sched.start()
    await sched.wait_idle(timeout=2.0)
    await sched.stop()
    assert [n for n, _ in log] == ["critical_task", "high_task", "low_task"]


async def test_serial_execution_single_writer(scheduler):
    """串行执行: 单写者约束下任意时刻最多 1 个任务在跑."""
    running = 0
    max_running = 0

    async def run(ctx, payload):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.02)
        running -= 1

    for name in ("a", "b", "c"):
        scheduler.register(TaskDefinition(name=name, run=run))
        await scheduler.submit(name)
    await scheduler.wait_idle(timeout=2.0)
    assert max_running == 1


async def test_duplicate_name_deduped(scheduler):
    """同名任务在队列中已有时不重复入队(触发器防抖)."""
    log = []
    gate = asyncio.Event()

    async def slow(ctx, payload):
        await gate.wait()
        log.append("ran")

    scheduler.register(TaskDefinition(name="job", run=slow))
    first = await scheduler.submit("job")
    second = await scheduler.submit("job")  # 执行中同名 → 去重
    gate.set()
    await scheduler.wait_idle(timeout=2.0)
    assert scheduler.get_status(first).status is TaskStatus.COMPLETED
    assert scheduler.get_status(second) is None  # 未入队
    assert log == ["ran"]


# ---------- 错误处理 ----------

async def test_submit_unknown_task_raises(scheduler):
    with pytest.raises(KeyError, match="未注册的任务"):
        await scheduler.submit("ghost")


async def test_failed_task_status(scheduler):
    """任务异常 → FAILED + 错误信息,调度循环不受影响."""

    async def boom(ctx, payload):
        raise RuntimeError("磁盘满了")

    async def ok(ctx, payload):
        pass

    scheduler.register(TaskDefinition(name="bad", run=boom))
    scheduler.register(TaskDefinition(name="good", run=ok))
    bad_id = await scheduler.submit("bad")
    good_id = await scheduler.submit("good")
    await scheduler.wait_idle(timeout=2.0)
    bad = scheduler.get_status(bad_id)
    assert bad.status is TaskStatus.FAILED
    assert "RuntimeError" in bad.error
    assert scheduler.get_status(good_id).status is TaskStatus.COMPLETED
