"""工具配置注入与使用约定注入(TOML 手术 / JSON 非破坏合并 / AGENTS.md 追加).

2026-09 从 cli 单模块拆出;经 smilex.cli 包 re-export,公共导入路径不变.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from pathlib import Path

from .adapters import MCP_SERVER_NAME, ToolAdapter

# AGENTS.md / CLAUDE.md 注入的记忆使用约定(幂等标记)
_GUIDE_MARKER = "<!-- smilex-memory-guide -->"
_GUIDE_TEXT = f"""
{_GUIDE_MARKER}
## 长期记忆(SmileX Memory MCP)

本项目已接入 SmileX 记忆服务,工具前缀 `mcp__{MCP_SERVER_NAME}__`:

- **回答涉及项目事实、历史决策、个人偏好的问题前**,先调 `memory_recall(query)` 获取上下文
- **任务完成或得到新结论后**,调 `memory_write(content, entities?, relations?)` 沉淀
- 首次接触本项目时调 `memory_init_project(name, project_path="<项目根>")` 完成冷启动:
  自动读 README + 导入 git 历史/markdown 文档/源码结构为初始记忆
  (幂等;stdio 模式可省略 project_path)
- 同一对话内保持相同 session_id(默认 "default")
""".strip()


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
