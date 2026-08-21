"""三重触发器 — Layer 3a(§8.1 / analyse memory-task-scheduler §3).

- TimeTrigger: 固定间隔触发(到期后按 interval_seconds 周期出队任务名)
- EventTrigger: emit(event_type) 即触发一次映射的任务
- AdaptiveTrigger: 规则条件满足时触发,带 cooldown 冷却(§8.3)

三个触发器均为「拉取式」: 调度循环每个 tick 调用 drain() 取出到期任务,
不自建后台协程,避免与调度器主循环产生竞态.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .models import SchedulerTask, TaskPriority, TriggerType


def _make_task(
    name: str,
    priority: TaskPriority,
    trigger_type: TriggerType,
    payload: dict[str, Any] | None = None,
) -> SchedulerTask:
    return SchedulerTask(
        name=name,
        priority=priority,
        trigger_type=trigger_type,
        payload=payload or {},
    )


class TimeTrigger:
    """定时触发器 — 每 interval_seconds 触发一次指定任务(§8.1).

    Args:
        task_name: 到期出队的任务名(须已 register)
        interval_seconds: 触发间隔(秒)
        priority: 出队任务优先级
        fire_immediately: True 时首次 poll 即触发;False 时先等一个间隔
    """

    def __init__(
        self,
        task_name: str,
        interval_seconds: float,
        *,
        priority: TaskPriority = TaskPriority.MEDIUM,
        fire_immediately: bool = False,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须 > 0")
        self.task_name = task_name
        self.interval_seconds = interval_seconds
        self.priority = priority
        self._next_fire = time.monotonic() + (interval_seconds if not fire_immediately else 0.0)

    def drain(self, now: float | None = None) -> list[SchedulerTask]:
        """取出到期的任务(可能因 tick 稀疏一次补多个)."""
        now = time.monotonic() if now is None else now
        tasks: list[SchedulerTask] = []
        while now >= self._next_fire:
            tasks.append(_make_task(self.task_name, self.priority, TriggerType.TIME))
            self._next_fire += self.interval_seconds
        return tasks


class EventTrigger:
    """事件触发器 — emit(event_type) 命中映射即出队一个任务(§8.1).

    Args:
        mapping: event_type → (task_name, priority)
    """

    def __init__(self, mapping: dict[str, tuple[str, TaskPriority]]) -> None:
        self._mapping = dict(mapping)

    def add_mapping(self, event_type: str, task_name: str, priority: TaskPriority) -> None:
        """注册事件 → 任务映射."""
        self._mapping[event_type] = (task_name, priority)

    def handle(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> SchedulerTask | None:
        """处理一个事件;命中映射返回待入队任务,未命中返回 None."""
        hit = self._mapping.get(event_type)
        if hit is None:
            return None
        task_name, priority = hit
        return _make_task(
            task_name,
            priority,
            TriggerType.EVENT,
            {"event_type": event_type, **(payload or {})},
        )

    def knows(self, event_type: str) -> bool:
        """该事件类型是否有映射."""
        return event_type in self._mapping


@dataclass
class AdaptiveRule:
    """自适应规则(§8.3)— 条件满足即触发,触发后进入冷却.

    Attributes:
        name: 规则名(如 "memory_pressure")
        task_name: 触发任务名(须已 register)
        condition: 条件函数(同步,返回 bool)
        cooldown_seconds: 冷却时长(秒);冷却期内 condition 不再评估
        priority: 触发任务优先级
    """

    name: str
    task_name: str
    condition: Any  # Callable[[], bool]
    cooldown_seconds: float
    priority: TaskPriority = TaskPriority.MEDIUM
    _last_fired: float = field(default=float("-inf"), init=False, repr=False)

    def try_fire(self, now: float | None = None) -> SchedulerTask | None:
        """冷却外且条件满足 → 触发并进入冷却;否则返回 None."""
        now = time.monotonic() if now is None else now
        if now - self._last_fired < self.cooldown_seconds:
            return None
        if not self.condition():
            return None
        self._last_fired = now
        return _make_task(
            self.task_name,
            self.priority,
            TriggerType.ADAPTIVE,
            {"rule": self.name},
        )


class AdaptiveTrigger:
    """自适应触发器 — 一组 AdaptiveRule,每个 tick 逐个尝试触发."""

    def __init__(self, rules: list[AdaptiveRule] | None = None) -> None:
        self.rules: list[AdaptiveRule] = list(rules or [])

    def add_rule(self, rule: AdaptiveRule) -> None:
        """注册规则."""
        self.rules.append(rule)

    def drain(self, now: float | None = None) -> list[SchedulerTask]:
        """评估全部规则,取出本轮触发的任务."""
        now = time.monotonic() if now is None else now
        tasks: list[SchedulerTask] = []
        for rule in self.rules:
            task = rule.try_fire(now)
            if task is not None:
                tasks.append(task)
        return tasks
