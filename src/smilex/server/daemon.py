"""Daemon 控制(server 层)— smilex-memory start / stop / status.

后台静默运行: start 以 ``python -m smilex.cli serve ...`` 再执行子进程并脱离
终端(POSIX ``start_new_session`` / Windows ``DETACHED_PROCESS``),
stdout/stderr 落盘 ``~/.smilex/logs/serve-{端口}.log``,pid 记录在
``~/.smilex/serve-{端口}.pid``;status 探测 pid 活性 + GET /api/health;
stop 先 SIGTERM 优雅退出、超时 SIGKILL 兜底(uvicorn 自带 SIGTERM 处理,
FastAPI lifespan 会级联关停调度器与 MemoryService)。

多实例按端口隔离(pidfile/日志均含端口)。全 stdlib 实现,无新增依赖;
doctor 复用 read_pid/is_alive/http_health 做服务状态行。
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .config import DEFAULT_PORT, SMILEX_HOME, ServerConfig, load_config

STARTUP_TIMEOUT_S = 30.0  # 健康轮询总预算(sentence-transformers 加载可能较慢)
STOP_TIMEOUT_S = 10.0
PROBE_INTERVAL_S = 0.5


# ==================== pidfile / 探测原语 ====================


def pidfile_path(port: int) -> Path:
    return SMILEX_HOME / f"serve-{port}.pid"


def default_log_path(port: int) -> Path:
    return SMILEX_HOME / "logs" / f"serve-{port}.log"


def read_pid(port: int) -> int | None:
    try:
        return int(pidfile_path(port).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def write_pid(port: int, pid: int) -> None:
    SMILEX_HOME.mkdir(parents=True, exist_ok=True)
    pidfile_path(port).write_text(f"{pid}\n", encoding="utf-8")


def remove_pidfile(port: int) -> None:
    with contextlib.suppress(OSError):
        pidfile_path(port).unlink(missing_ok=True)


def is_alive(pid: int) -> bool:
    """进程是否存活(Windows 无 sig 0 语义,走 tasklist)."""
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但不属于当前用户
    return True


def http_health(config: ServerConfig, timeout: float = 1.0) -> dict | None:
    """GET /api/health,失败(未启动/超时/非本服务)返回 None."""
    host = config.host if config.host not in ("0.0.0.0", "::") else "127.0.0.1"
    try:
        with urllib.request.urlopen(
            f"http://{host}:{config.port}/api/health", timeout=timeout
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def build_serve_command(
    *,
    config_path: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    db_path: str | None = None,
    log_level: str = "info",
) -> list[str]:
    """拼后台子进程命令(只带用户显式给定的旗标,其余交给配置文件)."""
    cmd = [sys.executable, "-m", "smilex.cli", "serve", "--log-level", log_level]
    if config_path is not None:
        cmd += ["--config", str(config_path)]
    if host is not None:
        cmd += ["--host", host]
    if port is not None:
        cmd += ["--port", str(port)]
    if db_path is not None:
        cmd += ["--db", str(db_path)]
    return cmd


def _print_log_tail(log_path: Path, lines: int = 20) -> None:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in text[-lines:]:
        print(line, file=sys.stderr)


# ==================== 子命令实现 ====================


def start_daemon(
    *,
    config_path: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    db_path: str | None = None,
    log_file: Path | None = None,
    log_level: str = "info",
) -> int:
    """后台启动 serve 子进程并等待健康就绪(已运行则幂等返回 0)."""
    if config_path is not None and not config_path.is_file():
        print(f"错误: 配置文件不存在: {config_path}", file=sys.stderr)
        return 1
    config = load_config(config_path, db_path=db_path, host=host, port=port)
    log_path = (log_file or default_log_path(config.port)).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    pid = read_pid(config.port)
    if pid is not None and is_alive(pid):
        state = "已就绪" if http_health(config) is not None else "初始化中"
        print(f"已在运行({state}): pid {pid}, {config.mcp_url}")
        print(f"日志: {log_path}")
        return 0

    cmd = build_serve_command(
        config_path=config_path, host=host, port=port,
        db_path=db_path, log_level=log_level,
    )
    with open(log_path, "ab", buffering=0) as log_fh:
        popen_kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_fh,
            "stderr": subprocess.STDOUT,
            "cwd": str(Path.home()),
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **popen_kwargs)
    write_pid(config.port, proc.pid)
    print(f"启动中: pid {proc.pid} → {config.mcp_url}")
    print(f"日志: {log_path}")

    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"错误: 进程已退出(code {proc.returncode}),日志尾部:",
                  file=sys.stderr)
            _print_log_tail(log_path)
            remove_pidfile(config.port)
            return 1
        if http_health(config) is not None:
            print(f"已就绪: MCP {config.mcp_url} | "
                  f"面板 http://{config.host}:{config.port}/")
            stop_cmd = "smilex-memory stop"
            if config.port != DEFAULT_PORT:
                stop_cmd += f" --port {config.port}"
            print(f"停止: {stop_cmd}")
            return 0
        time.sleep(PROBE_INTERVAL_S)

    # 进程仍活但 30s 未就绪(典型: 加载嵌入模型)— 不终止,继续后台初始化
    print(f"仍在初始化(已 {int(STARTUP_TIMEOUT_S)}s 未就绪),进程继续后台运行,"
          f"稍后用 smilex-memory status 确认", file=sys.stderr)
    return 0


def stop_daemon(
    *,
    config_path: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    force: bool = False,
    timeout: float = STOP_TIMEOUT_S,
) -> int:
    """停止后台服务: SIGTERM → 超时 SIGKILL(--force 跳过优雅退出)."""
    config = load_config(config_path, host=host, port=port)
    pid = read_pid(config.port)

    if pid is None or not is_alive(pid):
        remove_pidfile(config.port)  # stale pidfile
        if http_health(config) is not None:
            print(f"端口 {config.port} 有服务在运行但无 pidfile(非 daemon 启动?),"
                  f"请手动结束该进程", file=sys.stderr)
            return 1
        print("未运行")
        return 0

    if sys.platform == "win32":  # Windows 无 SIGTERM 优雅语义
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, check=False,
        )
    elif force:
        os.kill(pid, signal.SIGKILL)
    else:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while is_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if is_alive(pid):
            os.kill(pid, signal.SIGKILL)
    remove_pidfile(config.port)
    print(f"已停止: pid {pid}({config.host}:{config.port})")
    return 0


def status_daemon(
    *,
    config_path: Path | None = None,
    host: str | None = None,
    port: int | None = None,
) -> int:
    """查看运行状态(供脚本判断: 0=运行中,1=未运行)."""
    config = load_config(config_path, host=host, port=port)
    pid = read_pid(config.port)
    alive = pid is not None and is_alive(pid)

    if alive:
        health = http_health(config)
        if health is None:
            print(f"进程存活(pid {pid})但 /api/health 不可达(初始化中?)")
            return 1
        print(f"运行中: pid {pid}, MCP {config.mcp_url}, "
              f"面板 http://{config.host}:{config.port}/")
        if health.get("uptime_s") is not None:
            print(f"已运行 {int(health['uptime_s'])}s(启动于 "
                  f"{health.get('started_at', '?')})")
        print(f"日志: {default_log_path(config.port)}")
        return 0

    if pid is not None:
        remove_pidfile(config.port)  # stale pidfile
    if http_health(config) is not None:
        print(f"运行中(非 daemon 启动,无 pidfile): {config.mcp_url}")
        return 0
    print("未运行")
    return 1
