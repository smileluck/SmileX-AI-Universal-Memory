"""smilex-memory CLI — 服务化入口(server 层).

命令:
- serve:   全局常驻 HTTP 服务(MCP streamable-http + Web 面板)
- mcp:     stdio MCP 模式(项目独立库场景)
- init:    一键注入 MCP 配置到 Agent 工具(Kimi/Claude/Codex/Cursor/ZCode/Trae,
           WorkBuddy 输出手动接入指引);支持 --scope project|user;
           --scan 时冷启动并扫描项目(README/git 历史/markdown)生成初始记忆
- doctor:  环境自检(配置 / db / 端口 / 服务可达性)

安装: pip install 'smilex-ai-memory[server]'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import tomllib
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
- 首次接触本项目时调 `memory_init_project(name, project_path="<项目根>")` 完成冷启动:
  自动读 README + 导入 git 历史/markdown 文档为初始记忆(幂等;stdio 模式可省略 project_path)
- 同一对话内保持相同 session_id(默认 "default")
""".strip()


# ==================== 工具配置注入(适配器) ====================


class ToolAdapter:
    """Agent 工具 MCP 配置适配器协议(新增工具 = 加子类 + 注册到 ADAPTERS).

    - kind: "json"(默认,写入 servers_key 定位的表)/ "toml"(codex)/ "manual"(只打印指引)
    - servers_key: JSON 内 server 表的位置,支持嵌套元组(ZCode 为 ("mcp", "servers"))
    - user_config_path: 用户级(--scope user)配置绝对路径;None = 不支持用户级
    """

    name: str = ""
    config_rel_path: str = ""  # 相对项目根目录的配置文件路径
    servers_key: str | tuple[str, ...] = "mcpServers"
    kind: str = "json"

    def config_path(self, project_dir: Path) -> Path:
        return project_dir / self.config_rel_path

    def user_config_path(self) -> Path | None:
        return None

    def http_entry(self, url: str) -> dict:
        return {"url": url}

    def stdio_entry(self, db_path: Path) -> dict:
        # GUI 启动的工具(Cursor/Trae 等)没有 shell PATH,尽量解析绝对路径
        command = shutil.which(MCP_SERVER_NAME) or MCP_SERVER_NAME
        return {"command": command, "args": ["mcp", "--db", str(db_path)]}

    def guide_files(self, project_dir: Path) -> list[Path]:
        """--guide 时要写入使用约定的文件."""
        return [project_dir / "AGENTS.md"]


class KimiCodeAdapter(ToolAdapter):
    name = "kimi"
    config_rel_path = ".kimi-code/mcp.json"


class ClaudeCodeAdapter(ToolAdapter):
    name = "claude"
    config_rel_path = ".mcp.json"

    def user_config_path(self) -> Path | None:
        return Path.home() / ".claude.json"

    def guide_files(self, project_dir: Path) -> list[Path]:
        return [project_dir / "CLAUDE.md", project_dir / "AGENTS.md"]


class CodexAdapter(ToolAdapter):
    name = "codex"
    config_rel_path = ".codex/config.toml"
    kind = "toml"

    def user_config_path(self) -> Path | None:
        return Path.home() / ".codex" / "config.toml"


class CursorAdapter(ToolAdapter):
    name = "cursor"
    config_rel_path = ".cursor/mcp.json"

    def user_config_path(self) -> Path | None:
        return Path.home() / ".cursor" / "mcp.json"


class ZCodeAdapter(ToolAdapter):
    """ZCode 的 mcp.servers schema 严格:未知键会静默丢弃整台 server,条目保持最小."""

    name = "zcode"
    config_rel_path = ".zcode/config.json"
    servers_key = ("mcp", "servers")

    def user_config_path(self) -> Path | None:
        return Path.home() / ".zcode" / "cli" / "config.json"

    def http_entry(self, url: str) -> dict:
        return {"type": "http", "url": url}

    def stdio_entry(self, db_path: Path) -> dict:
        return {"type": "stdio", **super().stdio_entry(db_path)}


class TraeAdapter(ToolAdapter):
    name = "trae"
    config_rel_path = ".trae/mcp.json"

    def user_config_path(self) -> Path | None:
        # IDE 版全局配置在 Application Support 下,CN/国际目录取存在者
        user_dir = Path.home() / "Library" / "Application Support"
        for variant in ("Trae CN", "Trae"):
            if (user_dir / variant / "User").is_dir():
                return user_dir / variant / "User" / "mcp.json"
        return None


class WorkBuddyAdapter(ToolAdapter):
    """WorkBuddy 的 MCP 由客户端 UI 管理(连接器目录含加密凭据),只输出手动指引."""

    name = "workbuddy"
    kind = "manual"

    def manual_instructions(self, *, url: str, stdio: bool, db_path: Path | None) -> str:
        if stdio:
            entry = {"type": "stdio",
                     **super().stdio_entry(db_path or Path(".smilex/memory.db"))}
        else:
            entry = {"type": "streamableHttp", "url": url}
        snippet = json.dumps(
            {"mcpServers": {MCP_SERVER_NAME: entry}}, ensure_ascii=False, indent=2
        )
        return (
            "1. 打开 WorkBuddy 客户端 → 连接器 → 自定义连接器 / MCP 服务管理 → 添加\n"
            "2. 粘贴以下 JSON(HTTP 模式需先启动 smilex-memory serve):\n"
            f"{snippet}\n"
            "3. 保存后在工具列表确认出现 mcp__smilex-memory__ 前缀的工具"
        )


ADAPTERS: dict[str, ToolAdapter] = {
    a.name: a
    for a in (
        KimiCodeAdapter(),
        ClaudeCodeAdapter(),
        CodexAdapter(),
        CursorAdapter(),
        ZCodeAdapter(),
        TraeAdapter(),
        WorkBuddyAdapter(),
    )
}


def _key_path(servers_key: str | tuple[str, ...]) -> tuple[str, ...]:
    return (servers_key,) if isinstance(servers_key, str) else servers_key


def _toml_value(value: object) -> str:
    """序列化注入条目所需的受限 TOML 值(str / list[str] / bool)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise ValueError(f"不支持的 TOML 值类型: {type(value).__name__}")


def _toml_section(entry: dict) -> str:
    lines = [f"[mcp_servers.{MCP_SERVER_NAME}]"]
    for key, value in entry.items():
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def _strip_toml_section(text: str) -> str:
    # 删除目标段(段头允许行尾注释/空白;兼容裸键与引号键两种写法)
    name = re.escape(MCP_SERVER_NAME)
    pattern = re.compile(
        rf"(?ms)^\[mcp_servers\.(?:{name}|\"{name}\"|'{name}')[^\n]*\n(.*?)(?=^\[|\Z)"
    )
    return pattern.sub("", text)


def _inject_toml_config(config_path: Path, entry: dict) -> tuple[Path, str]:
    """codex 用 TOML 配置:文本级手术,保留其余内容与注释 byte-for-byte."""
    text = ""
    action = "新建"
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8")
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"现有 {config_path} 不是合法 TOML,请先修复: {exc}") from exc
        existing = parsed.get("mcp_servers", {}).get(MCP_SERVER_NAME)
        if existing == entry:
            return config_path, "已存在(幂等跳过)"
        if existing is not None:
            backup = config_path.with_suffix(config_path.suffix + ".bak")
            shutil.copy2(config_path, backup)
            action = f"覆盖(旧配置备份到 {backup.name})"
            text = _strip_toml_section(text)
        else:
            action = "合并"

    section = _toml_section(entry)
    if text.strip():
        if not text.endswith("\n"):
            text += "\n"
        text += "\n" + section  # 空行分隔,与已有内容合并
    else:
        text = section
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")
    return config_path, action


def inject_tool_config(
    adapter: ToolAdapter,
    project_dir: Path | None,
    *,
    url: str,
    stdio: bool = False,
    scope: str = "project",
) -> tuple[Path, str]:
    """非破坏性合并写入工具的 MCP 配置,返回 (配置文件路径, 动作描述).

    - scope="project" 写项目内配置;scope="user" 写 user_config_path()(仅 HTTP)
    - 文件不存在: 新建;已有其他 server: 合并保留
    - 同名 server 已存在且内容一致: 幂等跳过;不一致: 备份 .bak 后覆盖
    - TOML 适配器(codex)做文本级手术,其余内容 byte-for-byte 保留
    """
    if scope == "user":
        config_path = adapter.user_config_path()
        if config_path is None:
            raise ValueError(
                f"{adapter.name} 不支持用户级配置"
                f"(用项目级: init <项目目录> --tool {adapter.name})"
            )
        if stdio:
            raise ValueError("用户级仅支持 HTTP 模式(指向全局 serve,无项目独立库)")
    else:
        if project_dir is None:
            raise ValueError("项目级注入需要提供项目目录")
        config_path = adapter.config_path(project_dir)

    entry = (
        adapter.stdio_entry(project_dir / ".smilex" / "memory.db")
        if stdio
        else adapter.http_entry(url)
    )
    if adapter.kind == "toml":
        return _inject_toml_config(config_path, entry)

    data: dict = {}
    file_existed = config_path.exists()
    if file_existed:
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"现有 {config_path} 不是合法 JSON,请先修复: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"现有 {config_path} 顶层不是 JSON 对象")

    servers: dict = data
    for key in _key_path(adapter.servers_key):
        node = servers.get(key)
        if node is None:
            node = servers[key] = {}
        if not isinstance(node, dict):
            raise ValueError(f"现有 {config_path} 中 {key!r} 不是对象,无法合并")
        servers = node

    existing = servers.get(MCP_SERVER_NAME)
    if existing == entry:
        return config_path, "已存在(幂等跳过)"
    if existing is not None:
        backup = config_path.with_suffix(config_path.suffix + ".bak")
        shutil.copy2(config_path, backup)
        action = f"覆盖(旧配置备份到 {backup.name})"
    else:
        action = "合并" if file_existed else "新建"

    servers[MCP_SERVER_NAME] = entry
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


def _run_scan(project_dir: Path, *, stdio: bool) -> int:
    """冷启动 + 扫描导入(README/git 历史/markdown → 初始记忆).

    db 选择与注入的 MCP 条目一致: stdio 模式写项目内 ``<dir>/.smilex/memory.db``,
    HTTP 模式直接写全局库(跨进程写入,建议服务空闲时执行)。
    """
    from .server.mcp_server import build_middleware

    config = load_config()
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

    config = load_config()
    url = args.url or config.mcp_url
    tools = list(ADAPTERS) if "all" in args.tool else list(dict.fromkeys(args.tool))
    handled: list[str] = []

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
                adapter, project_dir, url=url, stdio=args.stdio, scope=args.scope
            )
        except ValueError as exc:
            print(f"[{name}] 跳过: {exc}", file=sys.stderr)
            continue
        print(f"[{name}] {action}: {path}")
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
        _run_scan(project_dir, stdio=args.stdio)

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

    p_init = sub.add_parser(
        "init", help="注入 MCP 配置到 Agent 工具(项目级 / 用户级全局)"
    )
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
        help="冷启动并扫描项目(README/git 历史/markdown)生成初始记忆",
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
