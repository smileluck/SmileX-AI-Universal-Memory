"""smilex-memory CLI — 服务化入口(server 层).

命令:
- serve:   全局常驻 HTTP 服务(MCP streamable-http + Web 面板)
- mcp:     stdio MCP 模式(项目独立库场景)
- init:    一键注入 MCP 配置到 Agent 工具的项目(Kimi Code / Claude Code)
- doctor:  环境自检(配置 / db / 端口 / 服务可达性)

安装: pip install 'smilex-ai-memory[server]'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from .server.config import DEFAULT_CONFIG_PATH, load_config, write_config_template

# 注入到各工具项目的 MCP server 名(工具内显示为 mcp__smilex-memory__*)
MCP_SERVER_NAME = "smilex-memory"

# AGENTS.md / CLAUDE.md 注入的记忆使用约定(幂等标记)
_GUIDE_MARKER = "<!-- smilex-memory-guide -->"
_GUIDE_TEXT = f"""
{_GUIDE_MARKER}
## 长期记忆(SmileX Memory MCP)

本项目已接入 SmileX 记忆服务,工具前缀 `mcp__{MCP_SERVER_NAME}__`:

- **回答涉及项目事实、历史决策、个人偏好的问题前**,先调 `memory_recall(query)` 获取上下文
- **任务完成或得到新结论后**,调 `memory_write(content, entities?, relations?)` 沉淀
- 首次接触本项目时调 `memory_init_project(name)` 完成冷启动
- 同一对话内保持相同 session_id(默认 "default")
""".strip()


# ==================== 工具配置注入(适配器) ====================


class ToolAdapter:
    """Agent 工具 MCP 配置适配器协议(新增工具 = 加子类 + 注册到 ADAPTERS)."""

    name: str = ""
    config_rel_path: str = ""  # 相对项目根目录的配置文件路径

    def config_path(self, project_dir: Path) -> Path:
        return project_dir / self.config_rel_path

    def http_entry(self, url: str) -> dict:
        return {"url": url}

    def stdio_entry(self, db_path: Path) -> dict:
        return {
            "command": "smilex-memory",
            "args": ["mcp", "--db", str(db_path)],
        }

    def guide_files(self, project_dir: Path) -> list[Path]:
        """--guide 时要写入使用约定的文件."""
        return [project_dir / "AGENTS.md"]


class KimiCodeAdapter(ToolAdapter):
    name = "kimi"
    config_rel_path = ".kimi-code/mcp.json"


class ClaudeCodeAdapter(ToolAdapter):
    name = "claude"
    config_rel_path = ".mcp.json"

    def guide_files(self, project_dir: Path) -> list[Path]:
        return [project_dir / "CLAUDE.md", project_dir / "AGENTS.md"]


ADAPTERS: dict[str, ToolAdapter] = {
    a.name: a for a in (KimiCodeAdapter(), ClaudeCodeAdapter())
}


def inject_tool_config(
    adapter: ToolAdapter,
    project_dir: Path,
    *,
    url: str,
    stdio: bool = False,
) -> tuple[Path, str]:
    """非破坏性合并写入工具的 MCP 配置,返回 (配置文件路径, 动作描述).

    - 文件不存在: 新建
    - 已有其他 server: 合并保留
    - 同名 server 已存在且内容一致: 幂等跳过;不一致: 备份 .bak 后覆盖
    """
    config_path = adapter.config_path(project_dir)
    entry = (
        adapter.stdio_entry(project_dir / ".smilex" / "memory.db")
        if stdio
        else adapter.http_entry(url)
    )

    data: dict = {"mcpServers": {}}
    action = "新建"
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        data.setdefault("mcpServers", {})
        existing = data["mcpServers"].get(MCP_SERVER_NAME)
        if existing == entry:
            return config_path, "已存在(幂等跳过)"
        if existing is not None:
            backup = config_path.with_suffix(config_path.suffix + ".bak")
            shutil.copy2(config_path, backup)
            action = f"覆盖(旧配置备份到 {backup.name})"
        else:
            action = "合并"

    data["mcpServers"][MCP_SERVER_NAME] = entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return config_path, action


def inject_guide(project_dir: Path, files: list[Path]) -> list[Path]:
    """向 AGENTS.md/CLAUDE.md 追加记忆使用约定(幂等),返回实际写入的文件."""
    written: list[Path] = []
    for path in files:
        if path.exists() and _GUIDE_MARKER in path.read_text(encoding="utf-8"):
            continue
        with path.open("a", encoding="utf-8") as f:
            f.write("\n\n" + _GUIDE_TEXT + "\n")
        written.append(path)
    return written


# ==================== 子命令 ====================


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server.app import serve

    config_path = write_config_template()  # 首次运行生成配置模板
    config = load_config(db_path=args.db, host=args.host, port=args.port)
    config.resolved_db_path().parent.mkdir(parents=True, exist_ok=True)
    print(f"配置: {config_path}")
    print(f"数据库: {config.resolved_db_path()}")
    print(f"MCP 地址(配置到各工具): {config.mcp_url}")
    print(f"Web 面板: http://{config.host}:{config.port}/")
    print("一键注入到项目: smilex-memory init <project-dir>")
    asyncio.run(serve(config))
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from .server.mcp_server import run_stdio

    config = load_config(db_path=args.db)
    config.resolved_db_path().parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_stdio(config))
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(f"错误: 项目目录不存在: {project_dir}", file=sys.stderr)
        return 1

    config = load_config()
    url = args.url or config.mcp_url
    tools = list(ADAPTERS) if args.tool == "all" else [args.tool]

    for name in tools:
        adapter = ADAPTERS[name]
        path, action = inject_tool_config(
            adapter, project_dir, url=url, stdio=args.stdio
        )
        print(f"[{name}] {action}: {path}")

    if args.guide:
        for name in tools:
            for path in inject_guide(project_dir, ADAPTERS[name].guide_files(project_dir)):
                print(f"[{name}] 已追加使用约定: {path}")

    if not args.stdio:
        print(f"\n指向全局服务: {url}(先启动 smilex-memory serve)")
    else:
        print(f"\n使用项目独立库: {project_dir / '.smilex' / 'memory.db'}(stdio 模式)")
    print("提示: Kimi Code 首次打开需在信任提示中选择 Trust; "
          "Claude Code 需在项目内确认启用 MCP server。")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    config = load_config()

    print(f"配置文件: {DEFAULT_CONFIG_PATH} "
          f"({'存在' if DEFAULT_CONFIG_PATH.exists() else '不存在(将用默认值)'})")
    db = config.resolved_db_path()
    print(f"数据库: {db} ({'存在' if db.exists() else '不存在(首次启动自动创建)'})")

    import importlib.util

    if all(importlib.util.find_spec(m) for m in ("fastapi", "mcp", "uvicorn")):
        print("server extras: 已安装(mcp/fastapi/uvicorn)")
    else:
        print("server extras: 未安装 → pip install 'smilex-ai-memory[server]'")
        ok = False

    import socket

    with socket.socket() as sock:
        in_use = sock.connect_ex((config.host, config.port)) == 0
    print(f"端口 {config.host}:{config.port}: {'已被占用(服务在运行?)' if in_use else '空闲'}")
    return 0 if ok else 1


# ==================== 入口 ====================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smilex-memory",
        description="SmileX Agent 记忆系统 — MCP 服务 / 面板 / 项目注入",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动全局常驻 HTTP 服务(MCP + Web 面板)")
    p_serve.add_argument("--db", help="数据库路径(覆盖配置文件)")
    p_serve.add_argument("--host", help="监听地址(默认 127.0.0.1)")
    p_serve.add_argument("--port", type=int, help="监听端口(默认 8765)")
    p_serve.set_defaults(func=_cmd_serve)

    p_mcp = sub.add_parser("mcp", help="stdio MCP 模式(项目独立库场景)")
    p_mcp.add_argument("--db", help="数据库路径(默认配置文件值)")
    p_mcp.set_defaults(func=_cmd_mcp)

    p_init = sub.add_parser("init", help="注入 MCP 配置到 Agent 工具的项目")
    p_init.add_argument("project_dir", help="目标项目目录")
    p_init.add_argument(
        "--tool", choices=[*ADAPTERS, "all"], default="all", help="目标工具(默认 all)"
    )
    p_init.add_argument("--url", help="MCP 服务地址(默认取配置 host:port)")
    p_init.add_argument(
        "--stdio", action="store_true", help="注入 stdio 本地库条目(默认 HTTP 全局服务)"
    )
    p_init.add_argument(
        "--guide", action="store_true", help="同时向 AGENTS.md/CLAUDE.md 追加使用约定"
    )
    p_init.set_defaults(func=_cmd_init)

    p_doctor = sub.add_parser("doctor", help="环境自检")
    p_doctor.set_defaults(func=_cmd_doctor)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
