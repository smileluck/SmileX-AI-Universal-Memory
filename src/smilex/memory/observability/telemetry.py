"""Telemetry 捆绑 — metrics + audit + tracer 的组装层(observability 子系统).

职责:
- ``Telemetry``: 三件套容器 + ``from_config`` 工厂(ServerConfig 的
  metrics/audit/audit_path/audit_reads/tracing 键 → 各组件实例)
- ``StandardMetrics``: 语义层标准指标(write/recall 计数与延迟)按需
  装入 registry;由 TelemetryMemoryMiddleware 消费
- ``make_scheduler_observer``: 调度器终态回调 — 任务计数
  ``smilex_task_total{name,status}`` + 失败告警日志
- 进程级 ``IntegrityReport`` 持有者: db_integrity 任务写入,
  /api/health 与 ``smilex_db_integrity_ok`` gauge 读取

分层: 本模块不 import scheduler 的运行时符号(仅 TYPE_CHECKING 注解),
避免 observability ↔ scheduler 耦合。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .audit import AuditLogger, default_audit_path
from .logging import get_logger
from .metrics import Counter, Histogram, MetricsRegistry
from .tracing import NoopTracer, Tracer, TracingConfig, get_tracer

if TYPE_CHECKING:
    from ..scheduler.models import SchedulerTask

_logger = get_logger("telemetry")


# ==================== 进程级 SQLite 完整性报告 ====================


@dataclass
class IntegrityReport:
    """PRAGMA quick_check 结果(最近一次 db_integrity 任务产物)."""

    ok: bool
    checked_at: str  # ISO 时间
    detail: str = "ok"
    db_size_bytes: int = 0


# 进程内单持有者: 单写者进程模型下无竞争;health/gauge 与任务同循环访问
_last_integrity: IntegrityReport | None = None


def set_integrity_report(report: IntegrityReport) -> None:
    """记录最近一次完整性检查结果(db_integrity 任务调用)."""
    global _last_integrity
    _last_integrity = report


def get_integrity_report() -> IntegrityReport | None:
    """读最近一次完整性检查结果(未检查过返回 None)."""
    return _last_integrity


# ==================== 标准语义指标 ====================


@dataclass
class StandardMetrics:
    """write/recall 语义层标准指标(TelemetryMemoryMiddleware 消费)."""

    write_total: Counter
    write_latency: Histogram
    recall_total: Counter
    recall_latency: Histogram

    @classmethod
    def install(cls, registry: MetricsRegistry) -> StandardMetrics:
        """装入标准指标(注册表同名幂等,可重复调用)."""
        return cls(
            write_total=registry.counter(
                "smilex_memory_write_total",
                "记忆写入次数(按结果状态)",
                ("status",),
            ),
            write_latency=registry.histogram(
                "smilex_memory_write_latency_seconds",
                "记忆写入延迟分布(秒)",
            ),
            recall_total=registry.counter(
                "smilex_memory_recall_total", "记忆检索次数"
            ),
            recall_latency=registry.histogram(
                "smilex_memory_recall_latency_seconds",
                "记忆检索延迟分布(秒)",
            ),
        )


def make_scheduler_observer(telemetry: Telemetry) -> Callable[[SchedulerTask], None]:
    """构造调度器终态观察者: 任务计数 + 失败告警日志.

    挂在 MemoryTaskScheduler(observer=...) — 每个任务到达终态
    (COMPLETED/FAILED/CANCELLED/PAUSED)后调用一次。
    """

    task_total = telemetry.metrics.counter(
        "smilex_task_total", "调度任务终态计数(按任务名与状态)", ("name", "status")
    )

    def observe(task: SchedulerTask) -> None:
        # TaskStatus 为 StrEnum,value 小写(completed/failed/...)
        task_total.inc(name=task.name, status=task.status.value)
        if task.status.value == "failed":
            _logger.error(
                "scheduler_task_failed",
                task=task.name,
                task_id=task.id,
                error=task.error,
            )

    return observe


# ==================== Telemetry 容器 ====================


@dataclass
class Telemetry:
    """可观测性三件套容器(全部组件可独立禁用).

    Attributes:
        metrics: 指标注册表(禁用 = 空 registry,照常计数但无人消费,
            开销可忽略;保持容器形状统一比条件分支更简单)
        audit: 审计日志器(path=None 即禁用)
        tracer: 追踪器(NoopTracer 即禁用)
    """

    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    audit: AuditLogger = field(default_factory=lambda: AuditLogger(None))
    tracer: Tracer = field(default_factory=get_tracer)
    _standard: StandardMetrics | None = field(default=None, repr=False)

    @classmethod
    def disabled(cls) -> Telemetry:
        """全禁用实例(noop tracer + 禁用审计 + 无人消费的 registry)."""
        return cls()

    @classmethod
    def from_config(
        cls,
        *,
        audit: bool = True,
        audit_path: str | Path | None = None,
        audit_reads: bool = False,
        tracing: str = "noop",
        service_name: str = "smilex-memory",
        db_path: str | Path | None = None,
    ) -> Telemetry:
        """从 ServerConfig 形状的键组装.

        Args:
            audit: 是否启用审计日志
            audit_path: 审计文件显式路径;None 且 audit=True 时按 db_path
                推导默认路径(":memory:" 库推导为禁用)
            audit_reads: 是否审计 recall 读事件
            tracing: "noop" | "otel"
            db_path: 库文件路径(审计默认位置推导用)
        """
        path: Path | None = None
        if audit:
            if audit_path is not None:
                path = Path(audit_path).expanduser()
            elif db_path is not None:
                path = default_audit_path(db_path)
        return cls(
            metrics=MetricsRegistry(),
            audit=AuditLogger(path, audit_reads=audit_reads),
            tracer=get_tracer(
                TracingConfig(backend=tracing, service_name=service_name)
            ),
        )

    @property
    def standard(self) -> StandardMetrics:
        """标准语义指标(懒装入,首次访问后缓存)."""
        if self._standard is None:
            self._standard = StandardMetrics.install(self.metrics)
        return self._standard

    @property
    def enabled(self) -> bool:
        """任一组件处于启用形态(审计开启或追踪非 noop)."""
        return self.audit.enabled or not isinstance(self.tracer, NoopTracer)


def attach_runtime_gauges(
    telemetry: Telemetry,
    *,
    queue_depth: Callable[[], int] | None,
    db_size_bytes: Callable[[], int] | None,
) -> None:
    """挂回调型 gauge: 队列深度 / 库体积(scrape 时惰性求值).

    另注册 ``smilex_db_integrity_ok``: 读进程级完整性报告
    (1=ok / 0=fail / -1=未检查),与任务/健康检查共享同一状态源。
    """
    if queue_depth is not None:
        telemetry.metrics.gauge(
            "smilex_task_queue_depth",
            "调度器队列深度(排队 + 执行中)",
            callback=queue_depth,
        )
    if db_size_bytes is not None:
        telemetry.metrics.gauge(
            "smilex_db_size_bytes", "SQLite 库文件体积(字节)", callback=db_size_bytes
        )

    def _integrity_ok() -> int:
        report = get_integrity_report()
        if report is None:
            return -1
        return 1 if report.ok else 0

    telemetry.metrics.gauge(
        "smilex_db_integrity_ok",
        "SQLite 完整性检查结果(1=ok / 0=fail / -1=未检查)",
        callback=_integrity_ok,
    )
