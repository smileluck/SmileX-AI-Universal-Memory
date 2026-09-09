"""Unit tests for MCP 工具层(create_mcp_server 注册的工具函数闭环).

直接经 MCPServer.call_tool 调用(不拉起真实 stdio/http 传输).
"""

from __future__ import annotations

import json
from pathlib import Path

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
    assert resp["init"]["scope"].startswith("project:")
    assert resp["init"]["entity_count"] >= 1
    assert resp["project_path"] is None
    assert resp["skipped"] == ["扫描跳过: 未提供 project_path"]


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


async def test_session_end_promotes_l0(service):
    """memory_session_end: 会话 L0 短记忆全部晋升 L1(持久化闭环)."""
    server = create_mcp_server(service)
    written = await _call(
        server,
        "memory_write",
        {"content": "会话结束要持久的短记忆", "session_id": "se1",
         "scope": "global"},
    )
    assert written["status"] == "saved"

    resp = await _call(server, "memory_session_end", {"session_id": "se1"})
    assert resp == {"session_id": "se1", "promoted": 1}

    # 跨会话可召回(新 session_id 也能命中 → 已落 L1)
    recalled = await _call(
        server, "memory_recall", {"query": "短记忆", "session_id": "se2"}
    )
    assert "短记忆" in recalled["context"]


async def _seed_person_graph(service):
    """直接落库一张小图(工具层测试种子,绕开写入路径的实体解析细节).

    Alice ──knows──▶ Bob ──knows──▶ Carol(t2 带前驱 t1,供 causal 模式用)
    Alice ──works_at──▶ Company X
    """
    memory = await service.get()
    conn = memory.engine.conn
    for eid, ename, name in [
        ("e1", "person:alice", "Alice"),
        ("e2", "person:bob", "Bob"),
        ("e3", "person:carol", "Carol"),
    ]:
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from)"
            " VALUES (?, ?, 'person', ?, 'global', '2025-01-01T00:00:00.000000Z')",
            (eid, ename, name),
        )
    for tid, sid, pred, oid, pred_of in [
        ("t1", "e1", "knows", "e2", None),
        ("t2", "e2", "knows", "e3", "t1"),
        ("t3", "e1", "works_at", "e3", None),
    ]:
        await conn.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, object_id, "
            "scope, valid_from, relation_type, predecessor_id) "
            "VALUES (?, ?, ?, ?, ?, 'global', '2025-01-01T00:00:00.000000Z', "
            "'semantic', ?)",
            (tid, f"tri_{tid}", sid, pred, oid, pred_of),
        )
    await conn.commit()


async def test_recall_entity_focus(service):
    """recall 的 entity 参数: 实体名解析为 ID → 图谱 N 度策略聚焦相关三元组."""
    await _seed_person_graph(service)
    server = create_mcp_server(service)
    resp = await _call(
        server, "memory_recall", {"query": "人际关系", "entity": "Alice"}
    )
    assert "knows" in resp["context"]
    assert resp["layers_used"]


async def test_recall_entity_not_found(service):
    server = create_mcp_server(service)
    with pytest.raises(Exception, match="未找到实体"):
        await _call(
            server, "memory_recall", {"query": "x", "entity": "不存在的人"}
        )


async def test_graph_query_path_mode(service):
    """mode=path: 实体名解析 + 最短路径 + 节点名/边谓词装饰."""
    await _seed_person_graph(service)
    server = create_mcp_server(service)
    resp = await _call(
        server,
        "memory_graph_query",
        {"mode": "path", "src": "Alice", "dst": "Carol"},
    )
    assert resp["found"] is True
    assert resp["depth"] == 1  # Alice ──works_at──▶ Carol 直达
    assert [n["name"] for n in resp["nodes"]] == ["Alice", "Carol"]
    assert resp["edges"][0]["predicate"] == "works_at"

    # 限 semantic 之外的类型 → 无路可走
    resp = await _call(
        server,
        "memory_graph_query",
        {"mode": "path", "src": "Alice", "dst": "Carol",
         "relation_types": "causal"},
    )
    assert resp["found"] is False

    # 无路径的两点
    resp = await _call(
        server,
        "memory_graph_query",
        {"mode": "path", "src": "Carol", "dst": "Alice"},
    )
    assert resp["found"] is False


async def test_graph_query_neighbors_mode(service):
    """mode=neighbors: N 度关系带实体名装饰 + relation_types 过滤透传."""
    await _seed_person_graph(service)
    server = create_mcp_server(service)
    resp = await _call(
        server,
        "memory_graph_query",
        {"mode": "neighbors", "entity": "Alice", "max_depth": 2},
    )
    assert resp["entity"]["name"] == "Alice"
    by_depth: dict[int, set[str]] = {}
    for r in resp["relations"]:
        by_depth.setdefault(r["depth"], set()).add(r["name"])
    assert by_depth == {1: {"Bob", "Carol"}}

    resp = await _call(
        server,
        "memory_graph_query",
        {"mode": "neighbors", "entity": "Alice", "relation_types": "causal"},
    )
    assert resp["relations"] == []


async def test_graph_query_causal_mode(service):
    """mode=causal: 沿 predecessor 链向根因追溯,三元组带摘要装饰."""
    await _seed_person_graph(service)
    server = create_mcp_server(service)
    resp = await _call(
        server,
        "memory_graph_query",
        {"mode": "causal", "triple_id": "t2", "direction": "backward"},
    )
    assert [item["depth"] for item in resp["backward"]] == [0, 1]
    assert resp["backward"][0]["triple"]["predicate"] == "knows"
    assert resp["backward"][1]["triple"]["subject"] == "Alice"


async def test_graph_query_bad_mode(service):
    server = create_mcp_server(service)
    with pytest.raises(Exception, match="mode"):
        await _call(server, "memory_graph_query", {"mode": "bogus"})
    with pytest.raises(Exception, match="src"):
        await _call(server, "memory_graph_query", {"mode": "path"})
    with pytest.raises(Exception, match="未找到实体"):
        await _call(
            server,
            "memory_graph_query",
            {"mode": "neighbors", "entity": "Nobody"},
        )


async def test_bad_scope_rejected(service):
    server = create_mcp_server(service)
    with pytest.raises(Exception, match="scope"):
        await _call(
            server,
            "memory_write",
            {"content": "x", "scope": "invalid-scope", "session_id": "s3"},
        )


async def test_init_project_scan_with_path(service, tmp_path):
    """project_path → 服务端扫描: README 自动读取 + markdown/源码导入 + skipped 明细."""
    server = create_mcp_server(service)
    (tmp_path / "README.md").write_text("# Demo\nBuilt with FastAPI.\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\nUses Redis.\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        '"""入口."""\nimport sqlite3\n\n\ndef main():\n    pass\n', encoding="utf-8"
    )

    resp = await _call(
        server, "memory_init_project",
        {"name": "demo", "project_path": str(tmp_path)},
    )
    assert resp["project_path"] == str(tmp_path)
    assert resp["init"]["scope"].startswith("project:")
    assert resp["imports"]["markdown"]["memory_count"] == 2
    assert resp["imports"]["code"]["memory_count"] == 1
    assert any("无 .git" in s for s in resp["skipped"])


async def test_init_project_stdio_infers_root(tmp_path):
    """stdio 模式 + db 在 <项目根>/.smilex/memory.db → project_path 自动推断."""
    from smilex.server.mcp_server import _infer_project_root

    root = tmp_path / "proj"
    (root / ".smilex").mkdir(parents=True)
    (root / "README.md").write_text("# P\n", encoding="utf-8")

    svc = MemoryService(
        ServerConfig(db_path=root / ".smilex" / "memory.db"), mode="stdio"
    )
    try:
        server = create_mcp_server(svc)
        resp = await _call(server, "memory_init_project", {"name": "proj"})
        assert resp["project_path"] == str(root.resolve())
        assert resp["imports"]["markdown"]["memory_count"] == 1
    finally:
        await svc.close()

    # 形状守卫: 全局默认库(~/.smilex)与非 .smilex 形状不推断
    assert _infer_project_root(Path.home() / ".smilex" / "memory.db") is None
    assert _infer_project_root(Path("/tmp/x/memory.db")) is None
