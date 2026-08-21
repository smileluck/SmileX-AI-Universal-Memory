"""MemoryTaskScheduler — 三重触发调度器(§8 / 04-layer3 §2.1).

自研轻量 asyncio 调度(选型: 04-layer3 §2.1,零外部依赖,asyncio 原生):

- 三重触发(§8.1): TimeTrigger(间隔) / EventTrigger(emit) / AdaptiveTrigger(带冷却)
- 优先级队列(§8.2): CRITICAL > HIGH > MEDIUM > LOW > IDLE,同级 FIFO
- 串行执行: 单写者 SQLite 约束下任务逐个执行,避免写冲突
- GRACEFUL 抢占(§9): CRITICAL 任务提交时自动抢占当前可中断任务;
  被抢占任务在检查点协作让出,断点落库后可 resume 续传

协作协议见 checkpoint.InterruptContext — 任务函数循环中定期
`await ctx.checkpoint(...)`,抢占时在该点抛 TaskInterruptedError.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from ..models.enums import PreemptionPolicy
from ..storage.storage_engine import StorageEngine
from .checkpoint import Checkpoint, CheckpointStore, InterruptContext, TaskInterruptedError
from .models import SchedulerTask, TaskDefinition, TaskPriority, TaskStatus, TriggerType
from .triggers import AdaptiveRule, AdaptiveTrigger, EventTrigger, TimeTrigger


@dataclass
class SchedulerConfig:
    """调度器配置."""

    tick_interval: float = 0.1  # 调度周期(秒);触发器按此粒度评估
    grace_timeout: float = 30.0  # GRACEFUL 抢占等待检查点的上限(§9.1: 超时强制)
    history_size: int = 128  # 已结束任务的保留条数(供 get_status 查询)


@dataclass
class _Running:
    """当前执行句柄(抢占协议内部状态)."""

    task: SchedulerTask
    ctx: InterruptContext
    runner: asyncio.Task[None]
    interruptible: bool
    done: asyncio.Event = field(default_factory=asyncio.Event)


class MemoryTaskScheduler:
    """记忆系统任务调度器(§8.4 对外契约).

    使用方法:
        scheduler = MemoryTaskScheduler(storage)
        scheduler.register(TaskDefinition(name="consolidate", run=run_consolidate))
        scheduler.add_time_trigger("consolidate", interval_seconds=3600)
        await scheduler.start()
        task_id = await scheduler.submit("consolidate")
        await scheduler.wait_idle()
        await scheduler.stop()

    注意: CRITICAL 任务的提交会抢占当前任务,因此任务函数内部不应
    再 submit CRITICAL 任务(会自我等待死锁).
    """

    def __init__(self, storage: StorageEngine, config: SchedulerConfig | None = None) -> None:
        self._storage = storage
        self._store = CheckpointStore(storage)
        self.config = config or SchedulerConfig()

        self._definitions: dict[str, TaskDefinition] = {}
        self._queue: deque[SchedulerTask] = deque()
        self._time_triggers: list[TimeTrigger] = []
        self._event_trigger = EventTrigger({})
        self._adaptive_trigger = AdaptiveTrigger()

        self._current: _Running | None = None
        self._history: deque[SchedulerTask] = deque(maxlen=self.config.history_size)
        self._queued_ids: dict[str, SchedulerTask] = {}

        self._running = False
        self._tick_task: asyncio.Task[None] | None = None
        self._wakeup: asyncio.Event | None = None
        self._idle: asyncio.Event | None = None

    # ==================== 任务注册 ====================

    def register(self, definition: TaskDefinition) -> None:
        """注册任务定义(5 类核心任务由此接入)."""
        if definition.run is None:
            raise ValueError(f"任务 {definition.name!r} 缺少 run 执行函数")
        self._definitions[definition.name] = definition

    def has_task(self, name: str) -> bool:
        return name in self._definitions

    # ==================== 触发器注册(§8.1) ====================

    def add_time_trigger(
        self,
        task_name: str,
        interval_seconds: float,
        *,
        priority: TaskPriority = TaskPriority.MEDIUM,
        fire_immediately: bool = False,
    ) -> TimeTrigger:
        """注册定时触发: 每 interval_seconds 触发一次 task_name."""
        trigger = TimeTrigger(
            task_name,
            interval_seconds,
            priority=priority,
            fire_immediately=fire_immediately,
        )
        self._time_triggers.append(trigger)
        return trigger

    def add_event_mapping(
        self, event_type: str, task_name: str, priority: TaskPriority = TaskPriority.MEDIUM
    ) -> None:
        """注册事件触发: emit(event_type) 时触发 task_name."""
        self._event_trigger.add_mapping(event_type, task_name, priority)

    def add_adaptive_rule(self, rule: AdaptiveRule) -> None:
        """注册自适应规则(带冷却,§8.3)."""
        self._adaptive_trigger.add_rule(rule)

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        """启动调度循环(幂等)."""
        if self._running:
            return
        self._running = True
        self._wakeup = asyncio.Event()
        self._idle = asyncio.Event()
        if self.is_idle:
            self._idle.set()
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        """停止调度器;正在执行的任务强制中断(IMMEDIATE)."""
        self._running = False
        if self._wakeup is not None:
            self._wakeup.set()
        if self._current is not None:
            self._current.runner.cancel()
        if self._tick_task is not None:
            with suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None

    # ==================== 对外契约(§8.4) ====================

    async def submit(
        self,
        name: str,
        *,
        payload: dict[str, Any] | None = None,
        priority: TaskPriority | None = None,
    ) -> str:
        """提交任务(手动触发),返回 task_id.

        CRITICAL 任务会抢占当前正在执行的可中断任务(GRACEFUL)后立即执行.
        """
        definition = self._definitions.get(name)
        if definition is None:
            raise KeyError(f"未注册的任务: {name!r}")
        task = SchedulerTask(
            name=name,
            priority=priority or definition.priority,
            trigger_type=TriggerType.MANUAL,
            payload=payload or {},
        )
        await self._enqueue(task)
        return task.id

    async def preempt(
        self,
        task_id: str | None = None,
        policy: PreemptionPolicy = PreemptionPolicy.GRACEFUL,
    ) -> bool:
        """抢占当前正在执行的任务(§9.1).

        Args:
            task_id: 指定抢占目标任务;None 表示当前任务
            policy: GRACEFUL 等待检查点让出(超时强制中断);
                    COOPERATIVE 仅置标志由任务自行让出;
                    IMMEDIATE 直接取消;NONE 不动作

        Returns:
            是否实际发起了抢占
        """
        running = self._current
        if running is None or not running.interruptible:
            return False
        if task_id is not None and running.task.id != task_id:
            return False

        if policy is PreemptionPolicy.NONE:
            return False
        if policy is PreemptionPolicy.IMMEDIATE:
            running.runner.cancel()
            await running.done.wait()
            return True
        # GRACEFUL / COOPERATIVE: 通知任务在检查点协作让出
        running.ctx.request_preempt()
        if policy is PreemptionPolicy.COOPERATIVE:
            return True  # 不等待,任务自行决定让出时机
        try:
            await asyncio.wait_for(running.done.wait(), timeout=self.config.grace_timeout)
        except TimeoutError:
            running.runner.cancel()  # 超时强制(§9.1 GRACEFUL 兜底)
            await running.done.wait()
        return True

    async def resume(self, task_id: str) -> bool:
        """从最近检查点续传任务(断点续传,§9.2 PAUSED → RUNNING).

        调度器运行中 → 重新入队由调度循环执行(保持串行约束);
        调度器未启动 → 就地执行.

        Returns:
            False 表示无检查点或任务不在 PAUSED 状态
        """
        checkpoint = await self._store.load_latest(task_id)
        if checkpoint is None:
            return False
        task = self._find_finished(task_id)
        if task is None or task.status is not TaskStatus.PAUSED:
            return False
        definition = self._definitions[task.name]
        task.status = TaskStatus.QUEUED
        task.resume_from = checkpoint.id
        if self._running:
            self._queue.append(task)
            self._queued_ids[task.id] = task
            if self._idle is not None:
                self._idle.clear()
            if self._wakeup is not None:
                self._wakeup.set()
        else:
            await self._run(task, definition, resume_from=checkpoint)
        return True

    async def cancel(self, task_id: str) -> bool:
        """取消任务: 队列中直接移除;执行中等价 IMMEDIATE 抢占."""
        queued = self._queued_ids.pop(task_id, None)
        if queued is not None:
            with suppress(ValueError):
                self._queue.remove(queued)
            queued.status = TaskStatus.CANCELLED
            self._history.append(queued)
            return True
        return await self.preempt(task_id, PreemptionPolicy.IMMEDIATE)

    async def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> str | None:
        """发出事件(事件触发入口);命中映射返回 task_id,未命中返回 None."""
        task = self._event_trigger.handle(event_type, payload)
        if task is None:
            return None
        if not self.has_task(task.name):
            return None
        await self._enqueue(task)
        return task.id

    # ==================== 状态查询 ====================

    def get_status(self, task_id: str) -> SchedulerTask | None:
        """查询任务(队列中/执行中/已结束)."""
        if task_id in self._queued_ids:
            return self._queued_ids[task_id]
        if self._current is not None and self._current.task.id == task_id:
            return self._current.task
        return self._find_finished(task_id)

    @property
    def is_idle(self) -> bool:
        """队列空且无执行中任务."""
        return not self._queue and self._current is None

    async def wait_idle(self, timeout: float | None = None) -> None:
        """等待队列排空且当前任务结束(测试/关停用)."""
        if self._idle is None:
            return
        await asyncio.wait_for(self._idle.wait(), timeout=timeout)

    # ==================== 内部: 调度循环 ====================

    async def _enqueue(self, task: SchedulerTask) -> None:
        """入队(同名任务去重,避免触发器刷队列);CRITICAL 抢占当前任务."""
        # 同名去重: 队列中或执行中已有同名任务则跳过
        if any(t.name == task.name for t in self._queue):
            return
        if self._current is not None and self._current.task.name == task.name:
            return
        self._queue.append(task)
        self._queued_ids[task.id] = task
        if self._idle is not None:
            self._idle.clear()
        if (
            task.priority is TaskPriority.CRITICAL
            and self._current is not None
            and self._current.interruptible
        ):
            await self.preempt(policy=PreemptionPolicy.GRACEFUL)
        if self._wakeup is not None:
            self._wakeup.set()

    async def _tick_loop(self) -> None:
        """主调度循环: 拉取触发器 → 出队最高优先级任务 → 串行执行."""
        assert self._wakeup is not None and self._idle is not None
        while self._running:
            self._wakeup.clear()
            # 1. 拉取三重触发器的到期任务
            for trigger in self._time_triggers:
                for task in trigger.drain():
                    if self.has_task(task.name):
                        await self._enqueue(task)
            for task in self._adaptive_trigger.drain():
                if self.has_task(task.name):
                    await self._enqueue(task)
            # 2. 出队最高优先级任务,串行执行(单写者约束)
            if self._queue:
                task = min(self._queue, key=lambda t: (t.priority.rank, t.id))
                self._queue.remove(task)
                self._queued_ids.pop(task.id, None)
                definition = self._definitions[task.name]
                checkpoint = None
                if task.resume_from is not None:
                    checkpoint = await self._store.load_latest(task.id)
                await self._run(task, definition, resume_from=checkpoint)
            # 3. 空闲标记 + 等待下一周期(提交/事件可提前唤醒)
            if self.is_idle:
                self._idle.set()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.config.tick_interval)

    async def _run(
        self,
        task: SchedulerTask,
        definition: TaskDefinition,
        *,
        resume_from: Checkpoint | None = None,
    ) -> None:
        """串行执行一个任务(含抢占协议装配)."""
        ctx = InterruptContext(task.id, self._store, resume_from=resume_from)
        done = asyncio.Event()
        runner = asyncio.create_task(self._run_guarded(task, definition, ctx))
        self._current = _Running(task, ctx, runner, definition.interruptible, done)
        try:
            await runner
        except asyncio.CancelledError:
            # runner 被 cancel(stop/超时强制)时 _run_guarded 已收敛状态
            pass
        finally:
            done.set()
            self._current = None
            self._history.append(task)

    async def _run_guarded(
        self, task: SchedulerTask, definition: TaskDefinition, ctx: InterruptContext
    ) -> None:
        """任务执行 + 状态机收敛(§9.2)."""
        task.status = TaskStatus.RUNNING
        try:
            await definition.run(ctx, task.payload)
        except TaskInterruptedError:
            task.status = TaskStatus.PAUSED  # 检查点已落库,可 resume
        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            await self._store.delete(task.id)
            # 不 re-raise: cancel 来自 preempt 超时/stop,状态已收敛
        except Exception as exc:  # noqa: BLE001 — 任务失败不拖垮调度循环
            task.status = TaskStatus.FAILED
            task.error = f"{type(exc).__name__}: {exc}"
            await self._store.delete(task.id)
        else:
            task.status = TaskStatus.COMPLETED
            await self._store.delete(task.id)

    def _find_finished(self, task_id: str) -> SchedulerTask | None:
        for task in self._history:
            if task.id == task_id:
                return task
        return None
