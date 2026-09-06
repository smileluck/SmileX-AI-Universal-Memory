"""分布式追踪 — Tracer Protocol + noop 默认 + OpenTelemetry 可选后端.

复刻 reranker/extractor 的可选组件模式(§15.3 分布式追踪):
- NoopTracer(默认,零依赖): span() 为空上下文管理器,未装 OTel 零开销
- OTelTracer(可选 extra ``tracing``): opentelemetry-api+sdk 真实 span,
  覆盖 memory.write / memory.recall / memory.bootstrap 链路;宿主已自配
  TracerProvider(如 OTLP exporter)时直接复用,未配时接管为
  ConsoleSpanExporter(开箱可见,生产接 OTLP 见 README 示例)

span 属性约定: scope/session/status/facts/elapsed_ms(标量,避免高基数)。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

TRACING_BACKENDS = ("noop", "otel")


@runtime_checkable
class Tracer(Protocol):
    """追踪协议: ``with tracer.span(name, **attrs): ...``."""

    def span(self, name: str, **attributes: Any) -> AbstractContextManager[None]:
        """进入名为 name 的 span,attributes 作为 span 属性."""
        ...


class NoopTracer:
    """默认实现 — 无操作上下文管理器(未启用追踪时的零开销路径)."""

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[None]:
        yield None


class OTelTracer:
    """OpenTelemetry 后端 — 可选依赖(extra ``tracing``).

    安装: ``pip install "smilex-ai-memory[tracing]"``
    导出器约定: 宿主先用标准 OTel API 配置 TracerProvider(如 OTLP)则
    完全尊重;否则注册带 ConsoleSpanExporter 的默认 provider(span 打到
    stderr,便于快速验证)。span 内抛出的异常自动记录并置 ERROR 状态。
    """

    def __init__(self, service_name: str = "smilex-memory") -> None:
        try:
            # 可选依赖(extra `tracing`)未安装时缺 stub 属预期,忽略 mypy
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
            from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
            from opentelemetry.sdk.trace.export import (  # type: ignore[import-not-found]
                BatchSpanProcessor,
                ConsoleSpanExporter,
            )
            from opentelemetry.trace import ProxyTracerProvider
        except ImportError as e:
            raise ImportError(
                "OTelTracer 需要 opentelemetry,但未安装. "
                "安装方式: pip install 'smilex-ai-memory[tracing]'. "
                "无追踪需求请保持默认 NoopTracer."
            ) from e
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        # ProxyTracerProvider = 尚无 SDK provider 接管 → 允许我们注册默认;
        # 宿主已 set_tracer_provider(自配 OTLP 等)时 set 会告警并被忽略,
        # get_tracer 继续走宿主配置,行为正确
        if isinstance(trace.get_tracer_provider(), ProxyTracerProvider):
            trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer("smilex.memory")

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[None]:
        with self._tracer.start_as_current_span(name, attributes=attributes):
            yield None


@dataclass(frozen=True)
class TracingConfig:
    """追踪工厂配置(对齐 RerankerConfig 模式).

    Attributes:
        backend: "noop"(默认,零依赖)或 "otel"(可选 extra `tracing`)
        service_name: OTel Resource 的 service.name(多实例区分端口场景可改)
    """

    backend: str = "noop"
    service_name: str = "smilex-memory"


def get_tracer(config: TracingConfig | None = None) -> Tracer:
    """工厂: 按 config.backend 返回 Tracer 实例(默认 NoopTracer)."""
    config = config or TracingConfig()
    if config.backend == "noop":
        return NoopTracer()
    if config.backend == "otel":
        return OTelTracer(service_name=config.service_name)
    raise ValueError(
        f"未知 tracing backend {config.backend!r},可选: {TRACING_BACKENDS}"
    )
