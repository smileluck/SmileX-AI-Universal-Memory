"""Unit tests for server.config(ServerConfig 加载/覆盖/模板)."""

from __future__ import annotations

from pathlib import Path

import pytest

from smilex.server.config import (
    ServerConfig,
    load_config,
    write_config_template,
)


def test_defaults():
    config = ServerConfig()
    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert config.embedder == "hash"
    assert config.enable_scheduler is True
    assert config.mcp_url == "http://127.0.0.1:8765/mcp"


def test_load_missing_file_returns_defaults(tmp_path):
    config = load_config(tmp_path / "nonexistent.toml")
    assert config.port == 8765


def test_load_from_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'db_path = "~/custom/memory.db"\nport = 9000\nembedder = "sentence-transformers"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.port == 9000
    assert config.embedder == "sentence-transformers"
    assert "custom" in str(config.resolved_db_path())


def test_cli_overrides_beat_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("port = 9000\n", encoding="utf-8")
    config = load_config(path, port=7777, host="0.0.0.0")
    assert config.port == 7777
    assert config.host == "0.0.0.0"
    assert config.mcp_url == "http://0.0.0.0:7777/mcp"


def test_write_config_template(tmp_path):
    path = tmp_path / "sub" / "config.toml"
    returned = write_config_template(path)
    assert returned == path and path.exists()
    config = load_config(path)
    assert config.port == 8765
    # 幂等: 已存在不覆盖
    path.write_text("port = 1234\n", encoding="utf-8")
    write_config_template(path)
    assert load_config(path).port == 1234


def test_load_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "db_path: ~/custom/memory.db\n"
        "port: 9001\n"
        "host: 0.0.0.0\n"
        "embedder: sentence-transformers\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.port == 9001
    assert config.host == "0.0.0.0"
    assert config.embedder == "sentence-transformers"


def test_load_from_yml_suffix(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("port: 9002\n", encoding="utf-8")
    assert load_config(path).port == 9002


def test_cli_overrides_beat_yaml_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("port: 9001\n", encoding="utf-8")
    config = load_config(path, port=7778)
    assert config.port == 7778


def test_load_explicit_path_expands_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / "config.toml").write_text("port = 9101\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    assert load_config(Path("~/config.toml")).port == 9101


def test_repo_example_yaml_is_valid():
    """仓库根 config.example.yaml 始终可加载且值与默认一致(防文档漂移)."""
    example = Path(__file__).resolve().parents[2] / "config.example.yaml"
    if not example.exists():  # wheel 安装场景无仓库根文件
        pytest.skip("config.example.yaml 不在仓库根")
    config = load_config(example)
    defaults = ServerConfig()
    assert config.host == defaults.host
    assert config.port == defaults.port
    # 示例用 ~ 字面量展示展开,比较 resolved 后的路径
    assert config.resolved_db_path() == defaults.resolved_db_path()
    assert config.embedder == "hash"
    assert config.reranker == "noop"
    assert config.fact_extractor == "passthrough"
    assert config.token_budget == defaults.token_budget
    assert config.enable_scheduler is True
