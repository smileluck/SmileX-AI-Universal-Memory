"""structlog 门面 — 库内统一结构化日志(observability 子系统).

structlog 自 0.1.0 起就是核心依赖但从未使用,本模块将其启用:
- 库内代码一律 ``get_logger()`` 取 logger(命名空间 ``smilex.*``)
- import 时即应用安全默认配置(JSON → stderr,级别 WARNING):
  嵌入式场景默认安静(INFO 不输出),WARNING 及以上经 stdlib lastResort
  打到 stderr 可见;入口(cli serve/start/mcp)调用 ``configure_logging``
  提升到 info 后全部可见
- stdio MCP 模式日志必须走 stderr(协议占用 stdout),本模块固定输出 stderr

为何不用 stdlib logging 直连: 键值对结构化事件(session/task/memory_id 等)
是审计与排查的主载体,structlog 的 bind/merge 在事件循环单线程模型下零开销。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_ROOT_NAME = "smilex"

# stdlib 侧: smilex 命名空间无 handler 时不向 root 冒泡打印(trace 只在
# 显式配置时出现);WARNING+ 由 lastResort 输出 stderr(库默认可见下限)
_std_logger = logging.getLogger(_ROOT_NAME)
_std_logger.addHandler(logging.NullHandler())

_LEVELS = {"debug", "info", "warning", "error", "critical"}


def configure_logging(level: str = "info") -> None:
    """配置 structlog(JSON → stderr,按 level 过滤).

    Args:
        level: debug/info/warning/error/critical(非法值回退 info 并告警)
    """
    if level not in _LEVELS:
        structlog.get_logger(_ROOT_NAME).warning(
            "未知日志级别,回退 info", requested=level
        )
        level = "info"
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,  # 允许入口后续重新配置级别
    )


def get_logger(name: str | None = None) -> Any:
    """取 ``smilex[.name]`` 结构化 logger(未配置时返回安全默认实例)."""
    logger = structlog.get_logger(f"{_ROOT_NAME}.{name}" if name else _ROOT_NAME)
    return logger


# import 即应用默认配置: 保证未调用 configure_logging 的嵌入式场景下,
# 库内日志仍输出合法 JSON 到 stderr(而非 structlog 裸默认的 stdout KV)
configure_logging("warning")
