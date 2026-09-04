"""Unit tests for CLI 注入: mcp.json 合并 + guide 幂等 + 多工具适配器 + 双 scope."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tomllib

import pytest

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


def test_inject_stdio_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)  # PATH 里没有 → 回退字面量
    inject_tool_config(ADAPTERS["kimi"], tmp_path, url="http://x/mcp", stdio=True)
    data = json.loads(
        (tmp_path / ".kimi-code" / "mcp.json").read_text(encoding="utf-8")
    )
    entry = data["mcpServers"][MCP_SERVER_NAME]
    assert entry["command"] == "smilex-memory"
    assert entry["args"][0] == "mcp"
    assert ".smilex" in entry["args"][-1]


def test_stdio_entry_resolves_absolute(tmp_path, monkeypatch):
    """GUI 启动的工具没有 shell PATH,注入时应尽量写绝对路径."""
    monkeypatch.setattr(shutil, "which", lambda _: "/opt/bin/smilex-memory")
    entry = ADAPTERS["kimi"].stdio_entry(tmp_path / ".smilex" / "memory.db")
    assert entry["command"] == "/opt/bin/smilex-memory"


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


def _fragments_like(db_path, prefix: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM temporal_fragments WHERE fragment_id LIKE ?",
                [f"{prefix}%"],
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
    (tmp_path / "app.py").write_text(
        '"""应用."""\nimport fastapi\n\n\nclass App:\n    pass\n', encoding="utf-8"
    )
    # 隔离真实 ~/.smilex/config.toml(避免读到本机 embedder 配置)
    monkeypatch.setattr(
        "smilex.cli.load_config",
        lambda *a, **k: ServerConfig(db_path=tmp_path / "global.db"),
    )

    assert _run_scan(tmp_path, stdio=True) == 0
    db = tmp_path / ".smilex" / "memory.db"
    assert db.exists()
    assert _fragments_like(db, "md:") == 2  # README.md + docs/a.md
    assert _fragments_like(db, "code:") == 1  # app.py

    _run_scan(tmp_path, stdio=True)  # 重复执行: 幂等不翻倍
    assert _fragments_like(db, "md:") == 2
    assert _fragments_like(db, "code:") == 1


# ---------- 多工具适配器(codex / cursor / zcode / trae / workbuddy) ----------


def test_adapter_registry():
    assert set(ADAPTERS) == {
        "kimi", "claude", "codex", "cursor", "zcode", "trae", "workbuddy",
    }
    assert ADAPTERS["codex"].kind == "toml"
    assert ADAPTERS["workbuddy"].kind == "manual"


@pytest.mark.parametrize(
    "tool,rel",
    [("cursor", ".cursor/mcp.json"), ("trae", ".trae/mcp.json")],
)
def test_json_adapters_create_and_overwrite(tmp_path, tool, rel):
    path, action = inject_tool_config(
        ADAPTERS[tool], tmp_path, url="http://127.0.0.1:8765/mcp"
    )
    assert action == "新建"
    assert path == tmp_path / rel
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"][MCP_SERVER_NAME] == {"url": "http://127.0.0.1:8765/mcp"}

    _, action = inject_tool_config(ADAPTERS[tool], tmp_path, url="http://new:2/mcp")
    assert "覆盖" in action
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"][MCP_SERVER_NAME]["url"] == "http://new:2/mcp"
    backup = json.loads(
        path.with_suffix(".json.bak").read_text(encoding="utf-8")
    )
    assert backup["mcpServers"][MCP_SERVER_NAME]["url"] == "http://127.0.0.1:8765/mcp"


def test_zcode_nested_servers_key(tmp_path):
    """ZCode 写嵌套 mcp.servers(而非扁平 mcpServers),条目带显式 type."""
    path, action = inject_tool_config(
        ADAPTERS["zcode"], tmp_path, url="http://127.0.0.1:8765/mcp"
    )
    assert action == "新建"
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["mcp"]["servers"][MCP_SERVER_NAME]
    assert entry == {"type": "http", "url": "http://127.0.0.1:8765/mcp"}
    assert "mcpServers" not in data  # 不误建扁平键


def test_zcode_merge_and_idempotent(tmp_path):
    config_path = tmp_path / ".zcode" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {"mcp": {"servers": {"other": {"type": "stdio", "command": "x"}}}}
        ),
        encoding="utf-8",
    )
    _, action = inject_tool_config(
        ADAPTERS["zcode"], tmp_path, url="http://127.0.0.1:8765/mcp"
    )
    assert action == "合并"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "other" in data["mcp"]["servers"]
    assert MCP_SERVER_NAME in data["mcp"]["servers"]

    _, action = inject_tool_config(
        ADAPTERS["zcode"], tmp_path, url="http://127.0.0.1:8765/mcp"
    )
    assert "幂等" in action


def test_zcode_stdio_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    inject_tool_config(ADAPTERS["zcode"], tmp_path, url="http://x/mcp", stdio=True)
    data = json.loads(
        (tmp_path / ".zcode" / "config.json").read_text(encoding="utf-8")
    )
    entry = data["mcp"]["servers"][MCP_SERVER_NAME]
    assert entry["type"] == "stdio"
    assert entry["command"] == "smilex-memory"


# ---------- codex(TOML) ----------


def test_codex_toml_creates_new(tmp_path):
    path, action = inject_tool_config(
        ADAPTERS["codex"], tmp_path, url="http://127.0.0.1:8765/mcp"
    )
    assert action == "新建"
    assert path == tmp_path / ".codex" / "config.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["mcp_servers"][MCP_SERVER_NAME] == {"url": "http://127.0.0.1:8765/mcp"}


def test_codex_toml_preserves_other_config(tmp_path):
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '# codex 配置\nmodel_provider = "custom"\n\n[profiles.foo]\nmodel = "gpt-5"\n',
        encoding="utf-8",
    )
    _, action = inject_tool_config(
        ADAPTERS["codex"], tmp_path, url="http://127.0.0.1:8765/mcp"
    )
    assert action == "合并"
    text = config_path.read_text(encoding="utf-8")
    assert "# codex 配置" in text  # 注释保留
    data = tomllib.loads(text)
    assert data["model_provider"] == "custom"
    assert data["profiles"]["foo"]["model"] == "gpt-5"
    assert data["mcp_servers"][MCP_SERVER_NAME]["url"] == "http://127.0.0.1:8765/mcp"


def test_codex_toml_idempotent(tmp_path):
    for _ in range(2):
        inject_tool_config(ADAPTERS["codex"], tmp_path, url="http://127.0.0.1:8765/mcp")
    _, action = inject_tool_config(
        ADAPTERS["codex"], tmp_path, url="http://127.0.0.1:8765/mcp"
    )
    assert "幂等" in action
    assert not (tmp_path / ".codex" / "config.toml.bak").exists()


def test_codex_toml_overwrite_with_backup(tmp_path):
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('model_provider = "custom"\n', encoding="utf-8")
    inject_tool_config(ADAPTERS["codex"], tmp_path, url="http://old:1/mcp")
    _, action = inject_tool_config(
        ADAPTERS["codex"], tmp_path, url="http://new:2/mcp"
    )
    assert "覆盖" in action
    backup = tomllib.loads(
        (tmp_path / ".codex" / "config.toml.bak").read_text(encoding="utf-8")
    )
    assert backup["mcp_servers"][MCP_SERVER_NAME]["url"] == "http://old:1/mcp"
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["mcp_servers"][MCP_SERVER_NAME]["url"] == "http://new:2/mcp"
    assert data["model_provider"] == "custom"  # 其他配置保留


def test_codex_toml_stdio_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/bin/smilex-memory")
    inject_tool_config(ADAPTERS["codex"], tmp_path, url="http://x/mcp", stdio=True)
    data = tomllib.loads(
        (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    entry = data["mcp_servers"][MCP_SERVER_NAME]
    assert entry["command"] == "/fake/bin/smilex-memory"
    assert entry["args"][0] == "mcp"
    assert ".smilex" in entry["args"][-1]


# ---------- workbuddy(manual) ----------


def test_workbuddy_manual_instructions():
    text = ADAPTERS["workbuddy"].manual_instructions(
        url="http://127.0.0.1:8765/mcp", stdio=False, db_path=None
    )
    assert "mcpServers" in text
    assert "streamableHttp" in text
    assert "http://127.0.0.1:8765/mcp" in text
    assert "mcp__smilex-memory__" in text


# ---------- 用户级 scope(--scope user) ----------


def test_user_scope_writes_home_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    expected_rel = {
        "codex": ".codex/config.toml",
        "cursor": ".cursor/mcp.json",
        "zcode": ".zcode/cli/config.json",
        "claude": ".claude.json",
    }
    for tool, rel in expected_rel.items():
        path, action = inject_tool_config(
            ADAPTERS[tool], None, url="http://127.0.0.1:8765/mcp", scope="user"
        )
        assert action == "新建"
        assert path == tmp_path / rel


def test_user_scope_claude_preserves_other_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps({"projects": {"tmp": {}}, "mcpServers": {}}), encoding="utf-8"
    )
    inject_tool_config(
        ADAPTERS["claude"], None, url="http://127.0.0.1:8765/mcp", scope="user"
    )
    data = json.loads(claude_json.read_text(encoding="utf-8"))
    assert data["projects"] == {"tmp": {}}  # 运行时状态保留
    assert data["mcpServers"][MCP_SERVER_NAME]["url"] == "http://127.0.0.1:8765/mcp"


def test_user_scope_unsupported_or_stdio(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ValueError, match="不支持用户级"):
        inject_tool_config(ADAPTERS["kimi"], None, url="http://x/mcp", scope="user")
    with pytest.raises(ValueError, match="仅支持 HTTP"):
        inject_tool_config(
            ADAPTERS["codex"], None, url="http://x/mcp", stdio=True, scope="user"
        )


def test_trae_user_scope_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ADAPTERS["trae"].user_config_path() is None  # 未安装 → 不支持
    user_dir = tmp_path / "Library" / "Application Support" / "Trae CN" / "User"
    user_dir.mkdir(parents=True)
    assert ADAPTERS["trae"].user_config_path() == user_dir / "mcp.json"
