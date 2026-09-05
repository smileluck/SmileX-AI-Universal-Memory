"""ServerConfig — 服务化配置(server 层).

配置文件: ``~/.smilex/config.toml``(不存在时 load 返回默认值,
``smilex-memory serve`` 首次运行自动生成模板)。CLI 参数优先级最高。
``--config`` 可指定任意路径,按扩展名识别格式: ``.toml``(tomllib)或
``.yaml``/``.yml``(yaml.safe_load,PyYAML 已是核心依赖)。

示例 config.toml::

    db_path = "~/.smilex/memory.db"
    host = "127.0.0.1"
    port = 8765
    embedder = "hash"            # 或 "sentence-transformers"(需 [embedding] extras)
    reranker = "noop"            # 或 "cross-encoder"(需 [rerank] extras)
    fact_extractor = "passthrough"  # 或 "llm"(需 [llm] extras + SMILEX_EXTRACT_* 环境变量)
    token_budget = 4000
    enable_scheduler = true      # serve 进程内跑 5 类核心调度任务

等价 config.yaml(扁平键,字段名相同)::

    db_path: ~/.smilex/memory.db
    host: 127.0.0.1
    port: 8765
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

SMILEX_HOME = Path.home() / ".smilex"
DEFAULT_CONFIG_PATH = SMILEX_HOME / "config.toml"
DEFAULT_DB_PATH = SMILEX_HOME / "memory.db"
DEFAULT_MCP_URL_PATH = "/mcp"
DEFAULT_PORT = 8765


class ServerConfig(BaseModel):
    """常驻服务 / stdio MCP 的共享配置."""

    db_path: Path = DEFAULT_DB_PATH
    host: str = "127.0.0.1"
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    embedder: str = "hash"  # hash | sentence-transformers
    reranker: str = "noop"  # noop | cross-encoder(需 [rerank] extras)
    # passthrough | llm(需 [llm] extras + SMILEX_EXTRACT_* 环境变量)
    fact_extractor: str = "passthrough"
    token_budget: int = Field(default=4000, gt=0)
    enable_scheduler: bool = True

    @property
    def mcp_url(self) -> str:
        """全局 HTTP 模式下各工具应配置的 MCP 地址."""
        return f"http://{self.host}:{self.port}{DEFAULT_MCP_URL_PATH}"

    def resolved_db_path(self) -> Path:
        """db_path 展开 ~ 并转绝对路径."""
        return Path(self.db_path).expanduser().resolve()


def load_config(
    path: Path | None = None,
    *,
    db_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> ServerConfig:
    """加载配置: TOML/YAML 文件 → CLI 覆盖(优先级递增).

    Args:
        path: 配置文件路径(None 用默认 ~/.smilex/config.toml;不存在视为空);
            按 ``.yaml``/``.yml`` 后缀走 YAML,其余走 TOML
        db_path / host / port: CLI 覆盖项(非 None 时生效)
    """
    path = (path or DEFAULT_CONFIG_PATH).expanduser()
    data: dict = {}
    if path.exists():
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        else:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
    config = ServerConfig(**data)
    overrides = {
        k: v for k, v in {"db_path": db_path, "host": host, "port": port}.items()
        if v is not None
    }
    if overrides:
        config = config.model_copy(update=overrides)
    return config


def write_config_template(path: Path | None = None) -> Path:
    """生成默认配置模板(已存在则不动),返回路径."""
    path = (path or DEFAULT_CONFIG_PATH).expanduser()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# SmileX-AI-Memory 服务配置(smilex-memory serve / mcp)\n"
        f'db_path = "{DEFAULT_DB_PATH.as_posix()}"\n'
        'host = "127.0.0.1"\n'
        "port = 8765\n"
        '# 语义检索改 "sentence-transformers"(需 [embedding] extras)\n'
        'embedder = "hash"\n'
        "token_budget = 4000\n"
        "enable_scheduler = true\n",
        encoding="utf-8",
    )
    return path
