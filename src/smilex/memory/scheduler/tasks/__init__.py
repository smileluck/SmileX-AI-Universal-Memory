"""5 类核心任务 — Layer 3c(主文档 §8.2 / §14.3 P0 范围;analyse memory-task-scheduler §2).

整合(consolidate)/ 遗忘(forget)/ 摘要(summarize)/ 因果(causal)/ 语义(semantic),
全部规则版(无 LLM):HashEmbedder 无语义相似度,聚合/分组一律用确定性规则
(同实体 / 同 scope / 关键词),保证嵌入式环境零依赖可跑.

包结构: 每类任务一个模块(函数签名 `(storage, ctx, payload) -> dict`),
公共常量与 scope 拼接在 `_common`;本 `__init__` 组装 `CoreTaskRunner`
薄门面与 `register_core_tasks` 装配器,公共 API 与原单文件 `tasks.py` 一致.

共同约定(§9 抢占协议):
- interruptible=True,批处理循环中定期 `await ctx.checkpoint(...)`
- cursor = 最后处理的行 id(ULID 字符串有序,id > cursor 续扫)
- state 携带跨断点的累积状态(如整合的 pending 分组 / 统计计数)
- payload 均支持: scope(全路径,如 "project:proj_a")/ batch_size / step_delay(测试限速)

事务约定:直接用 StorageEngine.conn(同 CheckpointStore),每批写完 commit 一次;
schema 未改(复用 temporal_fragments / triples / causal_chains / checkpoints).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..models import TaskDefinition, TaskPriority
from ..triggers import AdaptiveRule
from ._common import (
    CONSOLIDATION_PREFIX,
    CORE_TASK_NAMES,
    SEMANTIC_COMMUNITY_ID,
    SEMANTIC_COMMUNITY_PREFIX,
    SUMMARY_ID_SUFFIX,
    SUMMARY_PREFIX,
    TASK_CAUSAL,
    TASK_CONSOLIDATE,
    TASK_FORGET,
    TASK_SEMANTIC,
    TASK_SUMMARIZE,
)
from .causal import causal
from .consolidate import consolidate
from .forget import forget
from .semantic import semantic
from .summarize import summarize

if TYPE_CHECKING:
    from ...storage.storage_engine import StorageEngine
    from ..checkpoint import InterruptContext
    from ..scheduler import MemoryTaskScheduler


class CoreTaskRunner:
    """5 类核心任务的执行函数集合(持有 StorageEngine,方法即 run 函数).

    每个方法签名符合 InterruptibleRun: `async (ctx, payload) -> dict(统计)`;
    实现委托至同名任务模块(consolidate/forget/summarize/causal/semantic).
    """

    def __init__(self, storage: StorageEngine) -> None:
        self._storage = storage

    async def consolidate(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
        return await consolidate(self._storage, ctx, payload)

    async def forget(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
        return await forget(self._storage, ctx, payload)

    async def summarize(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
        return await summarize(self._storage, ctx, payload)

    async def causal(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
        return await causal(self._storage, ctx, payload)

    async def semantic(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
        return await semantic(self._storage, ctx, payload)


@dataclass
class CoreTaskConfig:
    """5 类核心任务的默认装配参数(§8.1 三重触发 / §8.3 自适应冷却).

    嵌入式环境无法在同步条件函数里查库,自适应规则的条件由宿主应用以
    可调用对象注入(如内存压力表);None 表示不注册该条规则.
    """

    enable_time_triggers: bool = True
    enable_event_mappings: bool = True
    enable_adaptive_rules: bool = True

    consolidate_interval_seconds: float = 3600.0  # 每小时整合(§8.1 每日 02:00 的轻量替代)
    forget_interval_seconds: float = 86400.0  # 每日遗忘
    summarize_interval_seconds: float = 1800.0  # 每 30 分钟摘要
    semantic_interval_seconds: float = 7200.0  # 每 2 小时图维护

    l1_pressure: Any = None  # Callable[[], bool]: L1 使用率 > 80%(§8.3 记忆压力)
    memory_pressure_cooldown: float = 300.0  # 5 分钟
    potential_causal_links: Any = None  # Callable[[], bool]: 存在潜在因果链接
    causal_cooldown: float = 300.0  # 5 分钟


def register_core_tasks(
    scheduler: MemoryTaskScheduler,
    storage: StorageEngine,
    *,
    config: CoreTaskConfig | None = None,
) -> CoreTaskRunner:
    """把 5 类核心任务注册到调度器,并按 §8.1/§8.3 装配默认触发器.

    优先级(§8.2): 整合/因果 HIGH,摘要/语义 MEDIUM,遗忘 LOW;全部可中断.
    触发器默认:
    - 时间: 整合 1h / 摘要 30min / 语义 2h / 遗忘 1d(均不立即触发)
    - 事件: memory_full→整合 / episode_end→摘要 / session_end→遗忘 / causal_inference→因果
    - 自适应: 记忆压力→整合 / 潜在因果→因果(条件由 config 注入,带冷却)

    Returns:
        CoreTaskRunner(便于测试直接调用执行函数或自定义注册)
    """
    config = config or CoreTaskConfig()
    runner = CoreTaskRunner(storage)

    scheduler.register(
        TaskDefinition(
            name=TASK_CONSOLIDATE,
            run=runner.consolidate,
            priority=TaskPriority.HIGH,
            interruptible=True,
            description="L1→L2 记忆整合(规则版聚合,§7.3)",
        )
    )
    scheduler.register(
        TaskDefinition(
            name=TASK_FORGET,
            run=runner.forget,
            priority=TaskPriority.LOW,
            interruptible=True,
            description="遗忘:淘汰/降权过期低值记忆(importance × 时间衰减)",
        )
    )
    scheduler.register(
        TaskDefinition(
            name=TASK_SUMMARIZE,
            run=runner.summarize,
            priority=TaskPriority.MEDIUM,
            interruptible=True,
            description="长 fragment 规则版摘要(截取/模板,无 LLM)",
        )
    )
    scheduler.register(
        TaskDefinition(
            name=TASK_CAUSAL,
            run=runner.causal,
            priority=TaskPriority.HIGH,
            interruptible=True,
            description="因果链维护(predecessor_id → causal_chains)",
        )
    )
    scheduler.register(
        TaskDefinition(
            name=TASK_SEMANTIC,
            run=runner.semantic,
            priority=TaskPriority.MEDIUM,
            interruptible=True,
            description="语义图维护(NetworkX 连通分量 → L3 缓存)",
        )
    )

    if config.enable_time_triggers:
        scheduler.add_time_trigger(
            TASK_CONSOLIDATE,
            config.consolidate_interval_seconds,
            priority=TaskPriority.HIGH,
        )
        scheduler.add_time_trigger(
            TASK_SUMMARIZE,
            config.summarize_interval_seconds,
            priority=TaskPriority.MEDIUM,
        )
        scheduler.add_time_trigger(
            TASK_SEMANTIC,
            config.semantic_interval_seconds,
            priority=TaskPriority.LOW,
        )
        scheduler.add_time_trigger(
            TASK_FORGET,
            config.forget_interval_seconds,
            priority=TaskPriority.LOW,
        )

    if config.enable_event_mappings:
        scheduler.add_event_mapping("memory_full", TASK_CONSOLIDATE, TaskPriority.HIGH)
        scheduler.add_event_mapping("episode_end", TASK_SUMMARIZE, TaskPriority.MEDIUM)
        scheduler.add_event_mapping("session_end", TASK_FORGET, TaskPriority.LOW)
        scheduler.add_event_mapping("causal_inference", TASK_CAUSAL, TaskPriority.HIGH)

    if config.enable_adaptive_rules:
        if config.l1_pressure is not None:
            scheduler.add_adaptive_rule(
                AdaptiveRule(
                    name="memory_pressure",
                    task_name=TASK_CONSOLIDATE,
                    condition=config.l1_pressure,
                    cooldown_seconds=config.memory_pressure_cooldown,
                    priority=TaskPriority.HIGH,
                )
            )
        if config.potential_causal_links is not None:
            scheduler.add_adaptive_rule(
                AdaptiveRule(
                    name="causal_potential",
                    task_name=TASK_CAUSAL,
                    condition=config.potential_causal_links,
                    cooldown_seconds=config.causal_cooldown,
                    priority=TaskPriority.HIGH,
                )
            )

    return runner


__all__ = [
    "CONSOLIDATION_PREFIX",
    "CORE_TASK_NAMES",
    "CoreTaskConfig",
    "CoreTaskRunner",
    "SEMANTIC_COMMUNITY_ID",
    "SEMANTIC_COMMUNITY_PREFIX",
    "SUMMARY_ID_SUFFIX",
    "SUMMARY_PREFIX",
    "TASK_CAUSAL",
    "TASK_CONSOLIDATE",
    "TASK_FORGET",
    "TASK_SEMANTIC",
    "TASK_SUMMARIZE",
    "register_core_tasks",
]
