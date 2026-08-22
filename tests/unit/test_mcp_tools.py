"""Unit tests for MCP 工具层(create_mcp_server 注册的工具函数闭环).

直接经 MCPServer.call_tool 调用(不拉起真实 stdio/http 传输).
"""

from __future__ import annotations

import json

import pytest

from smilex.server.config import ServerConfig
from smilex.server.mcp_server import MemoryService, create_mcp_server


@pytest.fixture
async def service(tmp_path):
    svc = MemoryService(ServerConfig(db_path=tmp_path / "mcp_test.db"))
    yield svc
    await svc.close()


async def _call(server, tool: str, args: dict) -> dict:
    result = await server.call_tool(tool, args)
    # mcp>=1.27 返回 CallToolResult(.content 为内容块列表);兼容旧版 tuple/列表
    if hasattr(result, "content"):
        blocks = result.content
    elif isinstance(result, tuple):
        blocks = result[0]
    else:
        blocks = result
    return json.loads(blocks[0].text)


async def test_write_then_recall(service):
    server = create_mcp_server(service)
    written = await _call(
        server,
        "memory_write",
        {
            "content": "项目使用 FastAPI 作为主框架",
            "entities": ["FastAPI"],
            "session_id": "s1",
            "scope": "global",
        },
    )
    assert written["memory_id"]
    assert written["status"] == "saved"

    recalled = await _call(
        server, "memory_recall", {"query": "FastAPI 框架", "session_id": "s1"}
    )
    assert "FastAPI" in recalled["context"]
    assert recalled["layers_used"]


async def test_init_project(service):
    server = create_mcp_server(service)
    resp = await _call(
        server,
        "memory_init_project",
        {"name": "demo", "tech_stack": ["python", "sqlite"]},
    )
    assert resp["scope"].startswith("project:")
    assert resp["entity_count"] >= 1


async def test_stats(service):
    server = create_mcp_server(service)
    await _call(
        server,
        "memory_write",
        {"content": "stats 测试记忆", "session_id": "s2", "scope": "global"},
    )
    stats = await _call(server, "memory_stats", {})
    assert stats["temporal_fragments"] >= 0
    assert "vector_links" in stats


async def test_recall_empty_db(service):
    server = create_mcp_server(service)
    resp = await _call(server, "memory_recall", {"query": "不存在的记忆"})
    assert resp["context"] == "" or resp["token_count"] == 0


async def test_bad_scope_rejected(service):
    server = create_mcp_server(service)
    with pytest.raises(Exception, match="scope"):
        await _call(
            server,
            "memory_write",
            {"content": "x", "scope": "invalid-scope", "session_id": "s3"},
        )
