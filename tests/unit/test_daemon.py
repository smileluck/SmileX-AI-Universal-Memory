"""Unit + smoke tests for server.daemon(start/stop/status 后台控制).

纯单元部分不 spawn 子进程;冒烟部分真实拉起 serve 子进程走
start → status → stop 全链路(需 server extras,缺则跳过)。
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from smilex.server import daemon

# 冒烟测试的子进程需要能 import smilex(未安装到解释器时兜底走源码树)
_SRC_ROOT = Path(daemon.__file__).resolve().parents[2]
_DEAD_PID = 2**22 + 1  # 超出常规 pid_max,几乎不可能存在


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def iso_home(tmp_path, monkeypatch):
    """把 daemon 的 ~/.smilex 隔离到 tmp_path(pidfile/日志不落真实家目录)."""
    monkeypatch.setattr(daemon, "SMILEX_HOME", tmp_path)
    return tmp_path


# ==================== 纯单元 ====================


def test_pidfile_roundtrip(iso_home):
    assert daemon.read_pid(8123) is None
    daemon.write_pid(8123, 4242)
    assert daemon.read_pid(8123) == 4242
    daemon.remove_pidfile(8123)
    assert daemon.read_pid(8123) is None


def test_read_pid_garbage(iso_home):
    (iso_home / "serve-1.pid").write_text("not-a-pid\n", encoding="utf-8")
    assert daemon.read_pid(1) is None


def test_is_alive(iso_home):
    assert daemon.is_alive(os.getpid())
    assert not daemon.is_alive(_DEAD_PID)


def test_default_log_path(iso_home):
    assert daemon.default_log_path(9000) == iso_home / "logs" / "serve-9000.log"


def test_build_serve_command():
    cmd = daemon.build_serve_command(port=9000, host="0.0.0.0", log_level="warning")
    assert cmd[:4] == [sys.executable, "-m", "smilex.cli", "serve"]
    assert cmd[cmd.index("--log-level") + 1] == "warning"
    assert cmd[cmd.index("--port") + 1] == "9000"
    # 未显式给定的旗标不拼接,交给配置文件
    assert "--config" not in cmd and "--db" not in cmd

    cmd2 = daemon.build_serve_command(config_path=Path("/x/y.yaml"), db_path="/d.db")
    assert cmd2[cmd2.index("--config") + 1] == "/x/y.yaml"
    assert cmd2[cmd2.index("--db") + 1] == "/d.db"


def test_start_missing_config_errors(iso_home, capsys):
    assert daemon.start_daemon(config_path=Path("/nonexistent/x.yaml")) == 1
    assert "不存在" in capsys.readouterr().err


def test_status_not_running_cleans_stale_pidfile(iso_home, capsys):
    daemon.write_pid(8221, _DEAD_PID)
    assert daemon.status_daemon(port=8221) == 1
    assert "未运行" in capsys.readouterr().out
    assert not (iso_home / "serve-8221.pid").exists()


def test_stop_not_running(iso_home, capsys):
    assert daemon.stop_daemon(port=8222) == 0
    assert "未运行" in capsys.readouterr().out


def test_http_health_down(iso_home):
    from smilex.server.config import ServerConfig

    assert daemon.http_health(ServerConfig(port=8765)) is None


# ==================== CLI 解析 ====================


def test_parser_accepts_daemon_flags():
    from smilex.cli import build_parser

    args = build_parser().parse_args(["start", "--config", "c.yaml", "--port", "9000"])
    assert args.config == "c.yaml" and args.port == 9000

    args = build_parser().parse_args(["stop", "--port", "9000", "--force"])
    assert args.force is True and args.port == 9000

    args = build_parser().parse_args(["status"])
    assert args.config is None

    args = build_parser().parse_args(["serve", "--log-level", "warning"])
    assert args.log_level == "warning"

    args = build_parser().parse_args(["mcp", "--config", "c.toml"])
    assert args.config == "c.toml"


def test_config_path_missing_file_exits():
    from smilex.cli import _config_path, build_parser

    args = build_parser().parse_args(["serve", "--config", "/nope/x.toml"])
    with pytest.raises(SystemExit):
        _config_path(args)


# ==================== 冒烟: 真实后台进程全链路 ====================


def test_start_status_stop_roundtrip(iso_home, tmp_path, monkeypatch, capsys):
    pytest.importorskip("fastapi")
    pytest.importorskip("mcp")
    pytest.importorskip("uvicorn")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(_SRC_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )

    port = _free_port()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"db_path: {tmp_path / 'mem.db'}\n"
        "host: 127.0.0.1\n"
        f"port: {port}\n"
        "embedder: hash\n"
        "enable_scheduler: false\n",
        encoding="utf-8",
    )

    try:
        assert daemon.start_daemon(config_path=config_path) == 0
        assert capsys.readouterr().out  # 启动横幅

        assert daemon.status_daemon(config_path=config_path) == 0
        assert (iso_home / f"serve-{port}.pid").exists()
        assert (iso_home / "logs" / f"serve-{port}.log").exists()

        # 重复 start 幂等
        assert daemon.start_daemon(config_path=config_path) == 0
        assert "已在运行" in capsys.readouterr().out
    finally:
        assert daemon.stop_daemon(config_path=config_path) == 0
        assert "已停止" in capsys.readouterr().out

    assert daemon.status_daemon(config_path=config_path) == 1
    assert not (iso_home / f"serve-{port}.pid").exists()
