"""observability — 可观测性子系统(§15.3: 指标/审计/追踪/结构化日志).

模块:
- metrics:   零依赖 Prometheus 文本格式指标注册表(Counter/Gauge/Histogram)
- audit:     变更事件 JSONL 审计日志(不落原文,PII 安全)
- tracing:   Tracer Protocol + noop 默认 + OpenTelemetry 可选后端
- logging:   structlog 门面(库内统一结构化日志)
- telemetry: 三件套容器 + 标准指标 + 调度观察者 + 完整性报告持有者

消费方: middlewares/_telemetry.py(语义层包装)、server/app.py(/metrics
端点与 gauge)、scheduler/tasks/integrity.py(完整性报告)。
"""

from __future__ import annotations

from .audit import WRITE_EVENTS, AuditLogger, content_digest, default_audit_path
from .logging import configure_logging, get_logger
from .metrics import (
    DEFAULT_LATENCY_BUCKETS,
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
)
from .telemetry import (
    IntegrityReport,
    StandardMetrics,
    Telemetry,
    attach_runtime_gauges,
    get_integrity_report,
    make_scheduler_observer,
    set_integrity_report,
)
from .tracing import TRACING_BACKENDS, NoopTracer, OTelTracer, Tracer, TracingConfig, get_tracer

__all__ = [
    "WRITE_EVENTS",
    "AuditLogger",
    "Counter",
    "DEFAULT_LATENCY_BUCKETS",
    "Gauge",
    "Histogram",
    "IntegrityReport",
    "MetricsRegistry",
    "NoopTracer",
    "OTelTracer",
    "StandardMetrics",
    "TRACING_BACKENDS",
    "Telemetry",
    "Tracer",
    "TracingConfig",
    "attach_runtime_gauges",
    "configure_logging",
    "content_digest",
    "default_audit_path",
    "get_integrity_report",
    "get_logger",
    "get_tracer",
    "make_scheduler_observer",
    "set_integrity_report",
]
