"""smilex-memory CLI — 服务化入口(server 层).

命令:
- serve:   前台启动全局常驻 HTTP 服务(MCP streamable-http + Web 面板)
- start:   后台静默启动(脱离终端,日志 ~/.smilex/logs/,pidfile 管理)
- stop:    停止后台服务(SIGTERM → SIGKILL 兜底)
- status:  查看后台服务状态(pid / 健康 / 日志位置)
- mcp:     stdio MCP 模式(项目独立库场景)
- init:    一键注入 MCP 配置到 Agent 工具(Kimi/Claude/Codex/Cursor/ZCode/Trae,
           WorkBuddy 输出手动接入指引);支持 --scope project|user;
           --scan 时冷启动并扫描项目(README/git 历史/markdown/源码结构)生成初始记忆
- doctor:  环境自检(配置 / db / 端口 / 服务可达性)
- maintain: 一次性维护任务(stdio 项目独立库不享受后台调度,手动/脚本触发;
           默认 forget+dedup+consolidate+semantic,--tasks 可选子集)

通用旗标: --config 指定配置文件路径(.toml/.yaml 均可,默认 ~/.smilex/config.toml),
--host/--port 覆盖监听地址(CLI > 配置文件 > 默认 127.0.0.1:8765)。

2026-09 拆分: 工具适配器在 adapters.py、配置注入在 inject.py;
本模块保留子命令编排并 re-export 公共符号(ADAPTERS / inject_tool_config /
inject_guide / MCP_SERVER_NAME),入口点 smilex.cli:main 不变.

安装: pip install 'smilex-ai-memory[server]'
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ..server.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_PORT,
    ServerConfig,
    load_config,
    write_config_template,
)
from .adapters import ADAPTERS, MCP_SERVER_NAME, ToolAdapter
from .inject import inject_guide, inject_tool_config

__all__ = [
    "ADAPTERS",
    "MCP_SERVER_NAME",
    "ToolAdapter",
    "build_parser",
    "inject_guide",
    "inject_tool_config",
    "main",
]


# ==================== 子命令 ====================


def _config_path(args: argparse.Namespace) -> Path | None:
    """解析 --config(显式指定但不存在时直接终止)."""
    if not args.config:
        return None
    path = Path(args.config).expanduser()
    if not path.is_file():
        raise SystemExit(f"错误: 配置文件不存在: {path}")
    return path


def _cmd_serve(args: argparse.Namespace) -> int:
    from ..server.app import serve

    path = _config_path(args)
    config_path = write_config_template(path)  # 首次运行生成配置模板(显式路径已存在,幂等)
    config = load_config(path, db_path=args.db, host=args.host, port=args.port)
    config.resolved_db_path().parent.mkdir(parents=True, exist_ok=True)
    print(f"配置: {config_path}")
    print(f"数据库: {config.resolved_db_path()}")
    print(f"MCP 地址(配置到各工具): {config.mcp_url}")
    print(f"Web 面板: http://{config.host}:{config.port}/")
    print("一键注入到项目: smilex-memory init <project-dir>")
    asyncio.run(serve(config, log_level=args.log_level))
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    from ..server.daemon import start_daemon

    return start_daemon(
        config_path=_config_path(args),
        host=args.host,
        port=args.port,
        db_path=args.db,
        log_file=Path(args.log_file).expanduser() if args.log_file else None,
        log_level=args.log_level,
    )


def _cmd_stop(args: argparse.Namespace) -> int:
    from ..server.daemon import stop_daemon

    return stop_daemon(
        config_path=_config_path(args), host=args.host, port=args.port,
        force=args.force,
    )


def _cmd_status(args: argparse.Namespace) -> int:
    from ..server.daemon import status_daemon

    return status_daemon(
        config_path=_config_path(args), host=args.host, port=args.port
    )


def _cmd_mcp(args: argparse.Namespace) -> int:
    from ..server.mcp_server import run_stdio

    config = load_config(_config_path(args), db_path=args.db)
    config.resolved_db_path().parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_stdio(config))
    return 0


def _run_scan(project_dir: Path, *, stdio: bool, config: ServerConfig | None = None) -> int:
    """冷启动 + 扫描导入(README/git 历史/markdown/源码 → 初始记忆).

    db 选择与注入的 MCP 条目一致: stdio 模式写项目内 ``<dir>/.smilex/memory.db``,
    HTTP 模式直接写全局库(跨进程写入,建议服务空闲时执行)。
    """
    from ..server.mcp_server import build_middleware

    config = config or load_config()
    db_path = (
        project_dir / ".smilex" / "memory.db" if stdio else config.resolved_db_path()
    )
    if not stdio:
        print(f"\n注意: --scan(HTTP 模式)直接写入全局库 {db_path},建议服务空闲时执行")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async def _run() -> dict:
        mw = build_middleware(config, db_path=db_path)
        try:
            await mw.initialize()
            return await mw.bootstrap_project(project_dir.name, project_path=project_dir)
        finally:
            await mw.close()

    result = asyncio.run(_run())
    init = result["init"]
    print(f"\n项目冷启动: scope={init['scope']} 实体={init['entity_count']} "
          f"三元组={init['triple_count']} 阶段={init['stage']}")
    for reason in result["skipped"]:
        print(f"跳过: {reason}", file=sys.stderr)
    for kind, imp in result["imports"].items():
        print(f"[{kind}] 新记忆 {imp['memory_count']} / 跳过 {imp['skipped_count']}"
              f"(源条目 {imp['source_count']}),实体 {imp['entity_count']},"
              f"三元组 {imp['triple_count']},耗时 {imp['elapsed_ms']}ms")
        for err in imp["errors"]:
            print(f"[{kind}] 警告: {err}", file=sys.stderr)
    print(f"数据库: {db_path}")
    return 0


# 各工具注入后的针对性提示
_TOOL_HINTS = {
    "kimi": "Kimi Code 首次打开需在信任提示中选择 Trust",
    "claude": "Claude Code 需在项目内确认启用 MCP server",
    "codex": "Codex 需重启会话生效;项目级配置需先将项目加入信任列表",
    "cursor": "Cursor 需重启或 Reload Window 后在 MCP 面板确认已启用",
    "zcode": "ZCode 需重启会话;若 server 未加载,检查配置是否被未知字段拖累",
    "trae": "Trae 需在 MCP 面板中确认 smilex-memory 已启用",
    "workbuddy": "WorkBuddy 按上方指引在客户端 UI 中完成添加",
}


def _cmd_init(args: argparse.Namespace) -> int:
    if args.scope == "user":
        if args.stdio:
            print("错误: --scope user 仅支持 HTTP 模式(用户级无项目独立库)",
                  file=sys.stderr)
            return 1
        if args.scan:
            print("错误: --scope user 不支持 --scan(无项目目录)", file=sys.stderr)
            return 1
        project_dir = None
    else:
        if not args.project_dir:
            print("错误: 项目级注入需要提供 project_dir(--scope user 可省略)",
                  file=sys.stderr)
            return 1
        project_dir = Path(args.project_dir).resolve()
        if not project_dir.is_dir():
            print(f"错误: 项目目录不存在: {project_dir}", file=sys.stderr)
            return 1

    config = load_config(_config_path(args))
    url = args.url or config.mcp_url
    api_key = config.effective_api_key
    tools = list(ADAPTERS) if "all" in args.tool else list(dict.fromkeys(args.tool))
    handled: list[str] = []
    # §15.4 鉴权: 支持 headers 的适配器自动注入 X-API-Key,其余打印手动指引
    needs_manual_key: list[str] = []

    for name in tools:
        adapter = ADAPTERS[name]
        if adapter.kind == "manual":
            print(f"[{name}] 需手动接入(无可安全写入的配置文件):")
            for line in adapter.manual_instructions(
                url=url,
                stdio=args.stdio,
                db_path=project_dir / ".smilex" / "memory.db" if project_dir else None,
            ).splitlines():
                print(f"    {line}")
            handled.append(name)
            continue
        try:
            path, action = inject_tool_config(
                adapter,
                project_dir,
                url=url,
                stdio=args.stdio,
                scope=args.scope,
                api_key=api_key,
            )
        except ValueError as exc:
            print(f"[{name}] 跳过: {exc}", file=sys.stderr)
            continue
        print(f"[{name}] {action}: {path}")
        if api_key and not args.stdio and not adapter.supports_http_headers:
            needs_manual_key.append(name)
        handled.append(name)

    if args.guide:
        if project_dir is None:
            print("\n--scope user 不注入 AGENTS.md 使用约定(无项目目录)")
        else:
            for name in handled:
                if ADAPTERS[name].kind == "manual":
                    continue
                for path in inject_guide(
                    project_dir, ADAPTERS[name].guide_files(project_dir)
                ):
                    print(f"[{name}] 已追加使用约定: {path}")

    if args.scan:
        _run_scan(project_dir, stdio=args.stdio, config=config)

    if api_key and needs_manual_key:
        print(
            f"\n服务已启用 API Key 鉴权: {'/'.join(needs_manual_key)} 的配置格式"
            "不支持自动注入 headers,需手动给 MCP 条目加 "
            f'{{"headers": {{"X-API-Key": "<你的 key>"}}}}(示例见 README)'
        )

    if args.stdio:
        print(f"\n使用项目独立库: {project_dir / '.smilex' / 'memory.db'}(stdio 模式)")
    elif args.scope == "user":
        print(f"\n已写入用户级配置,指向全局服务: {url}(先启动 smilex-memory serve)")
    else:
        print(f"\n指向全局服务: {url}(先启动 smilex-memory serve)")
    for name in handled:
        hint = _TOOL_HINTS.get(name)
        if hint:
            print(f"提示[{name}]: {hint}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    path = _config_path(args)
    config = load_config(path)

    config_file = path or DEFAULT_CONFIG_PATH
    print(f"配置文件: {config_file} "
          f"({'存在' if config_file.exists() else '不存在(将用默认值)'})")
    db = config.resolved_db_path()
    print(f"数据库: {db} ({'存在' if db.exists() else '不存在(首次启动自动创建)'})")

    import importlib.util

    if all(importlib.util.find_spec(m) for m in ("fastapi", "mcp", "uvicorn")):
        print("server extras: 已安装(mcp/fastapi/uvicorn)")
    else:
        print("server extras: 未安装 → pip install 'smilex-ai-memory[server]'")
        ok = False

    if config.effective_api_key:
        print("API Key 认证: 已启用(未带 X-API-Key / Bearer 的请求将被拒绝)")
    else:
        print("API Key 认证: 未配置(本地回环监听可接受;对外暴露请设置 SMILEX_API_KEY)")
    if config.effective_db_key:
        print("库文件加密: 已启用(SQLCipher;需加密构建的解释器,见 README)")
    else:
        print("库文件加密: 未启用")

    from ..server.daemon import http_health, is_alive, read_pid

    pid = read_pid(config.port)
    if pid is not None and is_alive(pid):
        state = "daemon 运行中" + (
            "(健康)" if http_health(config) is not None else "(health 不可达)"
        )
    elif http_health(config) is not None:
        state = "运行中(非 daemon 启动)"
    else:
        state = "未运行"
    print(f"服务状态: {state}(端口 {config.port})")
    return 0 if ok else 1


_MAINTAIN_TASKS = ("consolidate", "forget", "dedup", "semantic", "summarize")


def _cmd_maintain(args: argparse.Namespace) -> int:
    """一次性维护(2026-09): 调度任务的手动/脚本入口.

    stdio 模式每工具进程短生命周期、不启动调度器 — 项目独立库的
    forget/dedup/consolidate/semantic 由此命令承接(可挂 cron/钩子)。
    """
    config = load_config(_config_path(args))
    db_path = Path(args.db).expanduser() if args.db else config.resolved_db_path()
    if not db_path.exists():
        print(f"错误: 数据库不存在: {db_path}", file=sys.stderr)
        return 1
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    unknown = [t for t in tasks if t not in _MAINTAIN_TASKS]
    if unknown or not tasks:
        print(
            f"错误: 任务需为 {', '.join(_MAINTAIN_TASKS)} 的非空子集",
            file=sys.stderr,
        )
        return 1

    async def _run() -> int:
        from ..memory.scheduler.scheduler import (
            MemoryTaskScheduler,
            SchedulerConfig,
        )
        from ..memory.scheduler.tasks import CoreTaskConfig, register_core_tasks
        from ..memory.summarizer import SummarizerConfig, get_summarizer
        from ..server.mcp_server import build_middleware

        mw = build_middleware(config, db_path=db_path)
        await mw.initialize()
        try:
            sched = MemoryTaskScheduler(
                mw.engine, SchedulerConfig(tick_interval=0.05, grace_timeout=30.0)
            )
            register_core_tasks(
                sched,
                mw.engine,
                config=CoreTaskConfig(
                    enable_time_triggers=False,
                    enable_event_mappings=False,
                    enable_adaptive_rules=False,
                    forget_max_per_scope=config.max_records_per_scope,
                    forget_protect_importance=config.protect_importance,
                ),
                summarizer=get_summarizer(
                    SummarizerConfig(backend=config.summarizer)
                ),
                vector_store=mw.vector_store,
            )
            await sched.start()
            try:
                task_ids = [await sched.submit(name) for name in tasks]
                await sched.wait_idle(timeout=600.0)
            finally:
                await sched.stop()
            ok = True
            for name, tid in zip(tasks, task_ids, strict=True):
                info = sched.get_status(tid)
                status = info.status.value if info else "unknown"
                error = f" — {info.error}" if info and info.error else ""
                print(f"[{name}] {status}{error}")
                ok = ok and info is not None and info.status.value == "completed"
            return 0 if ok else 1
        finally:
            await mw.close()

    return asyncio.run(_run())


# ==================== 入口 ====================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smilex-memory",
        description="SmileX Agent 记忆系统 — MCP 服务 / 面板 / 项目注入",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config",
            help="配置文件路径(.toml/.yaml 均可,默认 ~/.smilex/config.toml)",
        )

    p_serve = sub.add_parser("serve", help="前台启动全局 HTTP 服务(MCP + Web 面板)")
    _add_config(p_serve)
    p_serve.add_argument("--db", help="数据库路径(覆盖配置文件)")
    p_serve.add_argument("--host", help="监听地址(默认 127.0.0.1)")
    p_serve.add_argument("--port", type=int, help=f"监听端口(默认 {DEFAULT_PORT})")
    p_serve.add_argument(
        "--log-level", choices=["debug", "info", "warning", "error"],
        default="info", help="uvicorn 日志级别(默认 info)",
    )
    p_serve.set_defaults(func=_cmd_serve)

    p_start = sub.add_parser(
        "start", help="后台静默启动全局服务(脱离终端,日志落盘,pidfile 管理)"
    )
    _add_config(p_start)
    p_start.add_argument("--db", help="数据库路径(覆盖配置文件)")
    p_start.add_argument("--host", help="监听地址(默认 127.0.0.1)")
    p_start.add_argument("--port", type=int, help=f"监听端口(默认 {DEFAULT_PORT})")
    p_start.add_argument(
        "--log-file", help="日志文件(默认 ~/.smilex/logs/serve-{端口}.log)"
    )
    p_start.add_argument(
        "--log-level", choices=["debug", "info", "warning", "error"],
        default="info", help="日志级别(默认 info,仅写入日志文件)",
    )
    p_start.set_defaults(func=_cmd_start)

    p_stop = sub.add_parser("stop", help="停止后台服务(SIGTERM 优雅退出 → SIGKILL 兜底)")
    _add_config(p_stop)
    p_stop.add_argument("--host", help="监听地址(定位实例,默认取配置文件)")
    p_stop.add_argument("--port", type=int, help=f"监听端口(默认 {DEFAULT_PORT})")
    p_stop.add_argument(
        "--force", action="store_true", help="跳过优雅退出,直接强制结束"
    )
    p_stop.set_defaults(func=_cmd_stop)

    p_status = sub.add_parser(
        "status", help="查看后台服务状态(pid / 健康 / 日志位置;退出码 0=运行中 1=未运行)"
    )
    _add_config(p_status)
    p_status.add_argument("--host", help="监听地址(定位实例,默认取配置文件)")
    p_status.add_argument("--port", type=int, help=f"监听端口(默认 {DEFAULT_PORT})")
    p_status.set_defaults(func=_cmd_status)

    p_mcp = sub.add_parser("mcp", help="stdio MCP 模式(项目独立库场景)")
    _add_config(p_mcp)
    p_mcp.add_argument("--db", help="数据库路径(默认配置文件值)")
    p_mcp.set_defaults(func=_cmd_mcp)

    p_init = sub.add_parser(
        "init", help="注入 MCP 配置到 Agent 工具(项目级 / 用户级全局)"
    )
    _add_config(p_init)
    p_init.add_argument(
        "project_dir", nargs="?", help="目标项目目录(--scope user 时可省略)"
    )
    p_init.add_argument(
        "--tool", nargs="+", choices=[*ADAPTERS, "all"], default=["all"],
        help="目标工具(可多值,默认 all)",
    )
    p_init.add_argument("--url", help="MCP 服务地址(默认取配置 host:port)")
    p_init.add_argument(
        "--stdio", action="store_true", help="注入 stdio 本地库条目(默认 HTTP 全局服务)"
    )
    p_init.add_argument(
        "--scope", choices=["project", "user"], default="project",
        help="写入范围: 项目内配置 / 工具全局配置(默认 project)",
    )
    p_init.add_argument(
        "--guide", action="store_true", help="同时向 AGENTS.md/CLAUDE.md 追加使用约定"
    )
    p_init.add_argument(
        "--scan", action="store_true",
        help="冷启动并扫描项目(README/git 历史/markdown/源码结构)生成初始记忆",
    )
    p_init.set_defaults(func=_cmd_init)

    p_doctor = sub.add_parser("doctor", help="环境自检")
    _add_config(p_doctor)
    p_doctor.set_defaults(func=_cmd_doctor)

    p_maint = sub.add_parser(
        "maintain", help="一次性维护任务(默认 forget+dedup+consolidate+semantic)"
    )
    _add_config(p_maint)
    p_maint.add_argument(
        "--tasks",
        default="forget,dedup,consolidate,semantic",
        help=f"逗号分隔任务子集(可选: {', '.join(_MAINTAIN_TASKS)})",
    )
    p_maint.add_argument("--db", help="目标库路径(默认取配置 db_path)")
    p_maint.set_defaults(func=_cmd_maintain)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
