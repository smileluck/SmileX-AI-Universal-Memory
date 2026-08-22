"""server 层 — MCP server + Web 面板(可选 extras: [server])."""

from __future__ import annotations

from .config import ServerConfig, load_config

__all__ = [
    "ServerConfig",
    "load_config",
]
