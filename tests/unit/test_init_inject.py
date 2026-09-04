"""Unit tests for CLI 注入: mcp.json 合并 + guide 幂等."""

from __future__ import annotations

import json
import sqlite3

from smilex.cli import (
    ADAPTERS,
    MCP_SERVER_NAME,
    inject_guide,
    inject_tool_config,
)


def test_inject_creates_new_config(tmp_path):
    path, action = inject_tool_config(
        ADAPTERS["kimi"], tmp_path, url="http://127.0.0.1:8765/mcp"
    )
    assert action == "新建"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "mcp.json" and path.parent.name == ".kimi-code"
    assert data["mcpServers"][MCP_SERVER_NAME] == {"url": "http://127.0.0.1:8765/mcp"}


def test_inject_merges_existing_servers(tmp_path):
    config_path = tmp_path / ".kimi-code" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"mcpServers": {"other": {"url": "http://x/y"}}}),
        encoding="utf-8",
    )
    _, action = inject_tool_config(
        ADAPTERS["kimi"], tmp_path, url="http://127.0.0.1:8765/mcp"
    )
    assert action == "合并"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "other" in data["mcpServers"]
    assert MCP_SERVER_NAME in data["mcpServers"]


def test_inject_idempotent(tmp_path):
    for _ in range(2):
        inject_tool_config(ADAPTERS["claude"], tmp_path, url="http://127.0.0.1:8765/mcp")
    _, action = inject_tool_config(
        ADAPTERS["claude"], tmp_path, url="http://127.0.0.1:8765/mcp"
    )
    assert "幂等" in action
    assert not (tmp_path / ".mcp.json.bak").exists()


def test_inject_overwrites_with_backup(tmp_path):
    inject_tool_config(ADAPTERS["claude"], tmp_path, url="http://old:1/mcp")
    _, action = inject_tool_config(
        ADAPTERS["claude"], tmp_path, url="http://new:2/mcp"
    )
    assert "覆盖" in action
    backup = json.loads((tmp_path / ".mcp.json.bak").read_text(encoding="utf-8"))
    assert backup["mcpServers"][MCP_SERVER_NAME]["url"] == "http://old:1/mcp"
    current = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert current["mcpServers"][MCP_SERVER_NAME]["url"] == "http://new:2/mcp"


def test_inject_stdio_entry(tmp_path):
    inject_tool_config(ADAPTERS["kimi"], tmp_path, url="http://x/mcp", stdio=True)
    data = json.loads(
        (tmp_path / ".kimi-code" / "mcp.json").read_text(encoding="utf-8")
    )
    entry = data["mcpServers"][MCP_SERVER_NAME]
    assert entry["command"] == "smilex-memory"
    assert entry["args"][0] == "mcp"
    assert ".smilex" in entry["args"][-1]


def test_guide_idempotent(tmp_path):
    files = [tmp_path / "AGENTS.md"]
    written = inject_guide(tmp_path, files)
    assert written == files
    assert "memory_recall" in files[0].read_text(encoding="utf-8")
    # 第二次: 幂等跳过,内容不重复
    again = inject_guide(tmp_path, files)
    assert again == []
    assert files[0].read_text(encoding="utf-8").count("smilex-memory-guide") == 1


def test_guide_claude_writes_both_files(tmp_path):
    files = ADAPTERS["claude"].guide_files(tmp_path)
    assert {f.name for f in files} == {"CLAUDE.md", "AGENTS.md"}


# ---------- init --scan(冷启动 + 扫描导入) ----------


def _md_fragments(db_path) -> int:
    con = sqlite3.connect(db_path)
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM temporal_fragments WHERE fragment_id LIKE 'md:%'"
            ).fetchone()[0]
        )
    finally:
        con.close()


def test_run_scan_stdio_creates_project_db(tmp_path, monkeypatch):
    """--scan(stdio 模式): 项目内 .smilex/memory.db 生成初始记忆,重复执行幂等."""
    from smilex.cli import _run_scan
    from smilex.server.config import ServerConfig

    (tmp_path / "README.md").write_text("# Demo\nUses FastAPI.\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\nUses Redis.\n", encoding="utf-8")
    # 隔离真实 ~/.smilex/config.toml(避免读到本机 embedder 配置)
    monkeypatch.setattr(
        "smilex.cli.load_config",
        lambda *a, **k: ServerConfig(db_path=tmp_path / "global.db"),
    )

    assert _run_scan(tmp_path, stdio=True) == 0
    db = tmp_path / ".smilex" / "memory.db"
    assert db.exists()
    assert _md_fragments(db) == 2  # README.md + docs/a.md

    _run_scan(tmp_path, stdio=True)  # 重复执行: 幂等不翻倍
    assert _md_fragments(db) == 2
