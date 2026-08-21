"""调度器任务模型 — Layer 3a(§8.2 / analyse memory-task-scheduler §2).

枚举沿用 Layer 0 风格(StrEnum,JSON 友好);
TaskPriority 数值越小优先级越高(CRITICAL=0 最先调度).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ...utils.ids import generate_id


class TaskPriority(StrEnum):
    """任务优先级(§8.2)— 数值越小越优先."""

    CRITICAL = "critical"  # 立即执行,不可被抢占
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    IDLE = "idle"

    @property
    def rank(self) -> int:
        """排序权重: 0(最高)~ 4(最低)."""
        return _PRIORITY_RANK[self]


_PRIORITY_RANK: dict[TaskPriority, int] = {
    TaskPriority.CRITICAL: 0,
    TaskPriority.HIGH: 1,
    TaskPriority.MEDIUM: 2,
    TaskPriority.LOW: 3,
    TaskPriority.IDLE: 4,
}


class TaskStatus(StrEnum):
    """任务状态(§9.2 状态机)."""

    QUEUED = "queued"  # 已入队,等待调度
    RUNNING = "running"  # 执行中
    PAUSED = "paused"  # 被抢占,Checkpoint 已保存,可 resume
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"  # 强制中断/取消,未保存断点


class TriggerType(StrEnum):
    """触发类型(§8.1 三重触发 + 手动)."""

    TIME = "time"  # 定时触发(固定间隔)
    EVENT = "event"  # 事件触发
    ADAPTIVE = "adaptive"  # 自适应规则触发(带冷却)
    MANUAL = "manual"  # 手动 submit


@dataclass
class TaskDefinition:
    """任务定义 — 「任务」注册用(5 类核心任务由此注册执行函数).

    执行函数签名:
        async def run(ctx: InterruptContext, payload: dict) -> Any
    InterruptContext 见 checkpoint.py(协作式检查点/让出钩子).
    """

    name: str  # 唯一任务名(注册键,如 "consolidate")
    run: Any = None  # async (InterruptContext, dict) -> Any
    priority: TaskPriority = TaskPriority.MEDIUM
    interruptible: bool = True  # 是否可被 GRACEFUL 抢占
    description: str = ""


@dataclass
class SchedulerTask:
    """一次任务执行实例(触发后入队的对象)."""

    id: str = field(default_factory=generate_id)
    name: str = ""  # 对应 TaskDefinition.name
    priority: TaskPriority = TaskPriority.MEDIUM
    trigger_type: TriggerType = TriggerType.MANUAL
    payload: dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.QUEUED
    resume_from: str | None = None  # 续传来源 Checkpoint.id
    error: str | None = None
