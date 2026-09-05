"""Agent 工具的 MCP 配置适配器(新增工具 = 加 ToolAdapter 子类 + 注册到 ADAPTERS).

2026-09 从 cli 单模块拆出;经 smilex.cli 包 re-export,公共导入路径不变.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

# 注入到各工具项目的 MCP server 名(工具内显示为 mcp__smilex-memory__*)
MCP_SERVER_NAME = "smilex-memory"


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
