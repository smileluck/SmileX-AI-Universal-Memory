"""Unit tests for Layer 3b GRACEFUL 抢占 + Checkpoint(§9).

验证:
- Checkpoint 落库/读取/清理(checkpoints 表,cursor 各编码往返)
- GRACEFUL 抢占: 长任务在检查点协作让出 → PAUSED → resume 断点续传
- IMMEDIATE 抢占 / GRACEFUL 超时强制 / 无检查点 resume 拒绝
- CRITICAL 任务提交自动抢占当前任务(§8.2)
"""

from __future__ import annotations

import asyncio

import pytest

from smilex.memory.models.enums import PreemptionPolicy
from smilex.memory.scheduler import (
    Checkpoint,
    CheckpointStore,
    MemoryTaskScheduler,
    SchedulerConfig,
    TaskDefinition,
    TaskPriority,
    TaskStatus,
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
        engine, SchedulerConfig(tick_interval=0.01, grace_timeout=0.5)
    )
    await sched.start()
    yield sched
    await sched.stop()


class FakeBatchTask:
    """可分步的假任务: 每步 sleep + 落检查点,支持断点续传."""

    def __init__(self, total: int = 10, step_delay: float = 0.02) -> None:
        self.total = total
        self.step_delay = step_delay
        self.processed: list[int] = []
        self.resumed_from: int | None = None

    async def run(self, ctx, payload):
        start = ctx.step  # resume 时为断点步数
        if ctx.resumed:
            self.resumed_from = start
        for i in range(start, self.total):
            await asyncio.sleep(self.step_delay)
            self.processed.append(i)
            await ctx.checkpoint(
                step=i + 1,
                progress=(i + 1) / self.total,
                state={"last": i},
                cursor={"next": i + 1},
            )


async def _wait_steps(task: FakeBatchTask, n: int, timeout: float = 2.0) -> None:
    """等待假任务处理满 n 步."""
    deadline = asyncio.get_event_loop().time() + timeout
    while len(task.processed) < n:
        assert asyncio.get_event_loop().time() < deadline, f"任务未在超时内处理满 {n} 步"
        await asyncio.sleep(0.005)


# ---------- Checkpoint 存取 ----------

async def test_checkpoint_store_roundtrip(engine):
    """Checkpoint 落库 → 读回(progress/step/state/cursor 一致),删除后为空."""
    store = CheckpointStore(engine)
    cp = Checkpoint(task_id="t1", progress=0.4, step=4, state={"k": "值"}, cursor={"next": 4})
    cp_id = await store.save(cp)
    loaded = await store.load_latest("t1")
    assert loaded is not None
    assert loaded.id == cp_id
    assert loaded.progress == pytest.approx(0.4)
    assert loaded.step == 4
    assert loaded.state == {"k": "值"}
    assert loaded.cursor == {"next": 4}
    await store.delete("t1")
    assert await store.load_latest("t1") is None


async def test_checkpoint_store_overwrites_same_task(engine):
    """同 task_id 重复保存只保留最新一条."""
    store = CheckpointStore(engine)
    await store.save(Checkpoint(task_id="t1", step=1, state={}))
    await store.save(Checkpoint(task_id="t1", step=2, state={}))
    loaded = await store.load_latest("t1")
    assert loaded.step == 2
    cursor = await engine.conn.execute(
        "SELECT COUNT(*) AS c FROM checkpoints WHERE task_id = 't1'"
    )
    row = await cursor.fetchone()
    assert row["c"] == 1


@pytest.mark.parametrize("cursor", [None, "row:42", b"\x00\x01bin", {"next": 7}, [1, 2], 3.5])
async def test_checkpoint_cursor_encodings(engine, cursor):
    """cursor 列(BLOB)各类型编码往返."""
    store = CheckpointStore(engine)
    await store.save(Checkpoint(task_id="t1", step=1, state={}, cursor=cursor))
    loaded = await store.load_latest("t1")
    assert loaded.cursor == cursor


# ---------- GRACEFUL 抢占 + 续传 ----------

async def test_graceful_preempt_and_resume(scheduler, engine):
    """长任务被 GRACEFUL 抢占 → checkpoint 落库 → resume 从断点继续,步骤不重不漏."""
    fake = FakeBatchTask(total=10, step_delay=0.02)
    scheduler.register(TaskDefinition(name="pattern", run=fake.run))
    task_id = await scheduler.submit("pattern", priority=TaskPriority.LOW)

    await _wait_steps(fake, 3)
    ok = await scheduler.preempt(task_id, PreemptionPolicy.GRACEFUL)
    assert ok is True

    # 状态机: PAUSED,断点已落库
    task = scheduler.get_status(task_id)
    assert task.status is TaskStatus.PAUSED
    store = CheckpointStore(engine)
    cp = await store.load_latest(task_id)
    assert cp is not None
    assert 0 < cp.step < fake.total
    assert cp.progress == pytest.approx(cp.step / fake.total)
    assert cp.cursor == {"next": cp.step}

    # 断点续传: 从 cp.step 继续,不重跑已完成步骤
    assert await scheduler.resume(task_id) is True
    await scheduler.wait_idle(timeout=2.0)
    assert fake.resumed_from == cp.step
    assert fake.processed == list(range(fake.total))  # 不重不漏
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED
    assert await store.load_latest(task_id) is None  # 完成后清理断点


async def test_resume_without_checkpoint_returns_false(scheduler):
    """无检查点时 resume 返回 False."""
    assert await scheduler.resume("no_such_task") is False


async def test_graceful_preempt_no_checkpoint_yet(scheduler, engine):
    """任务尚未落过检查点时被抢占: 在首个检查点让出,断点 step=1."""
    fake = FakeBatchTask(total=5, step_delay=0.05)
    scheduler.register(TaskDefinition(name="slow", run=fake.run))
    task_id = await scheduler.submit("slow")
    await asyncio.sleep(0.01)  # 任务已起步但未到首个检查点
    assert await scheduler.preempt(task_id, PreemptionPolicy.GRACEFUL) is True
    assert scheduler.get_status(task_id).status is TaskStatus.PAUSED
    cp = await CheckpointStore(engine).load_latest(task_id)
    assert cp.step == 1


async def test_preempt_non_running_returns_false(scheduler):
    """无执行中任务时抢占返回 False."""
    assert await scheduler.preempt(policy=PreemptionPolicy.GRACEFUL) is False


async def test_preempt_none_policy_noop(scheduler):
    """NONE 策略不动作."""
    fake = FakeBatchTask(total=5)
    scheduler.register(TaskDefinition(name="job", run=fake.run))
    task_id = await scheduler.submit("job")
    await _wait_steps(fake, 1)
    assert await scheduler.preempt(task_id, PreemptionPolicy.NONE) is False
    await scheduler.wait_idle(timeout=2.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED


# ---------- IMMEDIATE / 超时强制 ----------

async def test_immediate_preempt_cancels(scheduler, engine):
    """IMMEDIATE: 直接取消,状态 CANCELLED,断点清理."""
    fake = FakeBatchTask(total=10, step_delay=0.05)
    scheduler.register(TaskDefinition(name="batch", run=fake.run))
    task_id = await scheduler.submit("batch")
    await _wait_steps(fake, 1)
    assert await scheduler.preempt(task_id, PreemptionPolicy.IMMEDIATE) is True
    assert scheduler.get_status(task_id).status is TaskStatus.CANCELLED
    assert await CheckpointStore(engine).load_latest(task_id) is None


async def test_graceful_timeout_forces_cancel(scheduler):
    """GRACEFUL 超时: 任务不落检查点 → 超时强制中断(§9.1)."""
    never_yield = asyncio.Event()

    async def stubborn(ctx, payload):
        # 永不调用 ctx.checkpoint 的任务
        await never_yield.wait()

    scheduler.register(TaskDefinition(name="stubborn", run=stubborn))
    task_id = await scheduler.submit("stubborn")
    await asyncio.sleep(0.05)
    # grace_timeout=0.5s(fixture 配置),超时后强制取消
    assert await scheduler.preempt(task_id, PreemptionPolicy.GRACEFUL) is True
    assert scheduler.get_status(task_id).status is TaskStatus.CANCELLED


async def test_non_interruptible_task_cannot_be_preempted(scheduler):
    """interruptible=False 的任务拒绝抢占."""
    fake = FakeBatchTask(total=3, step_delay=0.02)
    scheduler.register(TaskDefinition(name="crit", run=fake.run, interruptible=False))
    task_id = await scheduler.submit("crit", priority=TaskPriority.CRITICAL)
    await asyncio.sleep(0.01)
    assert await scheduler.preempt(task_id, PreemptionPolicy.GRACEFUL) is False
    await scheduler.wait_idle(timeout=2.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED


# ---------- CRITICAL 自动抢占 ----------

async def test_critical_submit_auto_preempts(scheduler):
    """CRITICAL 任务提交自动 GRACEFUL 抢占当前低优任务(§8.2),随后可续传."""
    low = FakeBatchTask(total=10, step_delay=0.03)
    crit_log = []

    async def crit_run(ctx, payload):
        crit_log.append("crit")

    scheduler.register(TaskDefinition(name="low_job", run=low.run, priority=TaskPriority.LOW))
    scheduler.register(
        TaskDefinition(name="crit_job", run=crit_run, priority=TaskPriority.CRITICAL)
    )
    low_id = await scheduler.submit("low_job")
    await _wait_steps(low, 2)

    crit_id = await scheduler.submit("crit_job")
    await scheduler.wait_idle(timeout=2.0)

    assert scheduler.get_status(low_id).status is TaskStatus.PAUSED  # 被抢占
    assert scheduler.get_status(crit_id).status is TaskStatus.COMPLETED
    assert crit_log == ["crit"]

    # 续传低优任务至完成
    assert await scheduler.resume(low_id) is True
    await scheduler.wait_idle(timeout=2.0)
    assert low.processed == list(range(10))
