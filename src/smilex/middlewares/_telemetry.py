"""TelemetryMemoryMiddleware — 记忆语义层的可观测性包装(§15.3).

组合而非侵入: 覆写 write/recall/bootstrap 等入口方法,monotonic 计延迟、
写标准指标、追加审计事件、开 tracer span 后委托 super() — 核心写/检索
逻辑零改动,MemoryMiddleware 全部行为保持不变。

审计口径:
- 变更事件 write/init;读事件 recall 仅 audit_reads=True 时记录
- content_len/content_sha256 取 **脱敏后** 内容(NoopPIIMasker 即原文):
  审计文件不得成为 PII 泄漏面(§15.4 配套约定)
- who/what/when: session_id + channel(调用方标注)/ memory_ids + status / ts

server 层 build_middleware() 在任一观测组件启用时返回本包装实例;
库用户亦可自行组合::

    mw = TelemetryMemoryMiddleware(
        "app.db", telemetry=Telemetry.from_config(audit=True, db_path="app.db")
    )
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ..memory.observability import Telemetry, content_digest
from .memory import MemoryMiddleware

if TYPE_CHECKING:
    from ..memory.contracts import (
        ProjectInitRequest,
        ProjectInitResponse,
        RecallRequest,
        RecallResponse,
        WriteRequest,
        WriteResponse,
    )


class TelemetryMemoryMiddleware(MemoryMiddleware):
    """带指标/审计/追踪的 MemoryMiddleware(行为与基类完全一致)."""

    def __init__(
        self,
        *args: Any,
        telemetry: Telemetry | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._telemetry = telemetry or Telemetry.disabled()
        # channel 审计维度: 默认 "lib",server 层按入口标注(mcp/panel/cli)
        self.telemetry_channel = "lib"

    @property
    def telemetry(self) -> Telemetry:
        """观测三件套(metrics 端点 / 测试断言用)."""
        return self._telemetry

    # ==================== M.3 写入 ====================

    async def write(
        self,
        request: WriteRequest,
        *,
        session_id: str,
        scope_id: str | None = None,
        chain_id: str | None = None,
        detect_conflicts: bool = True,
    ) -> WriteResponse:
        std = self._telemetry.standard
        start = time.monotonic()
        with self._telemetry.tracer.span(
            "memory.write", session=session_id, scope=scope_id or ""
        ):
            try:
                resp = await super().write(
                    request,
                    session_id=session_id,
                    scope_id=scope_id,
                    chain_id=chain_id,
                    detect_conflicts=detect_conflicts,
                )
            except Exception:
                elapsed = time.monotonic() - start
                std.write_total.inc(status="error")
                std.write_latency.observe(elapsed)
                self._telemetry.audit.log(
                    "write",
                    session_id=session_id,
                    scope=scope_id or self._current_scope_id or "",
                    channel=self.telemetry_channel,
                    status="error",
                    elapsed_ms=int(elapsed * 1000),
                    content_len=len(request.content),
                    content_sha256=content_digest(
                        self._pii.mask(request.content)
                    ),
                )
                raise
        elapsed = time.monotonic() - start
        status = resp.status.value if hasattr(resp.status, "value") else str(resp.status)
        std.write_total.inc(status=status)
        std.write_latency.observe(elapsed)
        self._telemetry.audit.log(
            "write",
            session_id=session_id,
            scope=scope_id or self._current_scope_id or "",
            channel=self.telemetry_channel,
            memory_ids=[resp.memory_id] if resp.memory_id else [],
            status=status,
            elapsed_ms=int(elapsed * 1000),
            content_len=len(request.content),
            content_sha256=content_digest(self._pii.mask(request.content)),
        )
        return resp

    # ==================== M.4 检索 ====================

    async def recall(
        self,
        request: RecallRequest,
        *,
        session_id: str | None = None,
        include_archived: bool = False,
    ) -> RecallResponse:
        std = self._telemetry.standard
        start = time.monotonic()
        with self._telemetry.tracer.span(
            "memory.recall", session=session_id or ""
        ):
            try:
                resp = await super().recall(
                    request,
                    session_id=session_id,
                    include_archived=include_archived,
                )
            except Exception:
                std.recall_total.inc()
                std.recall_latency.observe(time.monotonic() - start)
                raise
        elapsed = time.monotonic() - start
        std.recall_total.inc()
        std.recall_latency.observe(elapsed)
        if self._telemetry.audit.audit_reads:
            self._telemetry.audit.log(
                "recall",
                session_id=session_id or "",
                channel=self.telemetry_channel,
                status="ok",
                elapsed_ms=int(elapsed * 1000),
                sources=len(resp.sources),
            )
        return resp

    # ==================== M.2 冷启动(审计 init 事件) ====================

    async def initialize_project(
        self, request: ProjectInitRequest, *, wizard_answers: dict[str, Any] | None = None
    ) -> ProjectInitResponse:
        start = time.monotonic()
        with self._telemetry.tracer.span("memory.bootstrap", project=request.name):
            resp = await super().initialize_project(
                request, wizard_answers=wizard_answers
            )
        self._telemetry.audit.log(
            "init",
            channel=self.telemetry_channel,
            project=request.name,
            scope=resp.scope,
            stage=resp.stage.value if hasattr(resp.stage, "value") else str(resp.stage),
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
        return resp

    async def bootstrap_project(
        self, name: str, **scan_kwargs: Any
    ) -> dict[str, Any]:
        start = time.monotonic()
        with self._telemetry.tracer.span("memory.bootstrap", project=name):
            result = await super().bootstrap_project(name, **scan_kwargs)
        self._telemetry.audit.log(
            "init",
            channel=self.telemetry_channel,
            project=name,
            scope=str(result.get("scope", "")),
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
        return result


__all__ = ["TelemetryMemoryMiddleware"]
