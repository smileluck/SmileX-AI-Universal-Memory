"""MCP server — 把 MemoryMiddleware 暴露为 MCP 工具(server 层).

两种传输模式复用同一工具集:
- stdio: ``smilex-memory mcp``(项目独立库场景,每工具进程独立)
- streamable HTTP: ``smilex-memory serve`` 挂载到 FastAPI(全局常驻,推荐)

工具约定(返回 JSON 文本):
- memory_recall: 检索记忆上下文(回答涉及项目事实/历史决策前先调)
- memory_write: 沉淀新事实/结论(任务完成、得到新决策时调)
- memory_init_project: 新项目冷启动 + 扫描生成初始记忆(README/git 历史/
  markdown 文档/源码结构;project_path 缺省时 stdio 模式按 db 路径推断项目根)
- memory_stats: 各层记忆计数(调试/面板)

session_id: 同一对话会话内保持一致可获得 L0 工作记忆加速;缺省 "default"。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..memory.embedder import EmbedderConfig, get_embedder
from ..memory.extractor import ExtractorConfig, get_extractor
from ..memory.models import MemoryScope, ScopeFilter
from ..memory.observability import Telemetry
from ..memory.pii import PIIConfig, get_pii_masker
from ..memory.reranker import RerankerConfig, get_reranker
from ..memory.storage.storage_engine import StorageEngine
from ..middlewares._telemetry import TelemetryMemoryMiddleware
from ..middlewares.dto import (
    RecallRequest,
    TripleInput,
    WriteRequest,
)
from ..middlewares.memory import MemoryMiddleware
from .config import ServerConfig
from .stats import collect_stats

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from ..memory.scheduler.scheduler import MemoryTaskScheduler

_MCP_IMPORT_HINT = (
    "MCP server 需要 mcp/fastapi/uvicorn,安装: pip install 'smilex-ai-memory[server]'"
)

DEFAULT_SESSION_ID = "default"


def _require_mcp() -> None:
    """server extras 未安装时给出清晰报错(延迟导入,核心库零依赖)."""
    try:
        import mcp  # noqa: F401
    except ImportError as e:
        raise ImportError(_MCP_IMPORT_HINT) from e


class MemoryService:
    """MemoryMiddleware 的懒初始化持有器(stdio/HTTP 模式共享).

    单进程单实例 = 单写者,与 SQLite 引擎假设一致;初始化并发由锁保护。
    """

    def __init__(self, config: ServerConfig, *, mode: str = "http") -> None:
        self._config = config
        # "stdio" = 项目独立库(smilex-memory mcp --db <项目>/.smilex/memory.db),
        # "http" = 全局常驻服务;影响 memory_init_project 的项目根推断
        self.mode = mode
        self._memory: MemoryMiddleware | None = None
        self._lock = asyncio.Lock()
        # 可观测性三件套(§15.3): /metrics 端点与调度观察者共用
        self._telemetry = Telemetry.from_config(
            audit=config.audit,
            audit_path=config.audit_path,
            audit_reads=config.audit_reads,
            tracing=config.tracing,
            db_path=config.resolved_db_path(),
        )
        # 调度器句柄(app lifespan 启动后回填;/api/health 读队列深度)
        self.scheduler: MemoryTaskScheduler | None = None

    @property
    def config(self) -> ServerConfig:
        return self._config

    @property
    def telemetry(self) -> Telemetry:
        """可观测性三件套(/metrics 端点 / 调度观察者 / 测试共用)."""
        return self._telemetry

    @property
    def initialized(self) -> bool:
        """MemoryMiddleware 是否已初始化(健康检查 db.connected)."""
        return self._memory is not None

    async def get(self) -> MemoryMiddleware:
        """取已初始化的 MemoryMiddleware(首次调用时初始化)."""
        if self._memory is not None:
            return self._memory
        async with self._lock:
            if self._memory is None:
                memory = build_middleware(self._config, telemetry=self._telemetry)
                await memory.initialize()
                self._memory = memory
        return self._memory

    async def close(self) -> None:
        if self._memory is not None:
            await self._memory.close()
            self._memory = None


def build_middleware(
    config: ServerConfig,
    db_path: Path | None = None,
    *,
    telemetry: Telemetry | None = None,
) -> MemoryMiddleware:
    """按 ServerConfig 组装 MemoryMiddleware(MemoryService 与 CLI --scan 共用).

    任一观测组件启用(audit/tracing)时返回 TelemetryMemoryMiddleware 包装
    (指标/审计/span);全 noop 时保持基类,行为与历史版本完全一致。
    配置了 db_encryption_key 时自建加密 StorageEngine 经 engine= 注入
    (含 pysqlcipher3 shim 尝试,见 storage/sqlcipher.py)。
    """
    embedder = get_embedder(EmbedderConfig(backend=config.embedder))
    reranker = get_reranker(RerankerConfig(backend=config.reranker))
    extractor = get_extractor(ExtractorConfig(backend=config.fact_extractor))
    pii_masker = get_pii_masker(PIIConfig(backend=config.pii_masker))
    resolved_db = db_path if db_path is not None else config.resolved_db_path()
    if telemetry is None:
        telemetry = Telemetry.from_config(
            audit=config.audit,
            audit_path=config.audit_path,
            audit_reads=config.audit_reads,
            tracing=config.tracing,
            db_path=resolved_db,
        )
    db_key = config.effective_db_key
    engine: StorageEngine | None = None
    if db_key is not None:
        from ..memory.storage.sqlcipher import install_shim

        # pysqlcipher3 在场则换掉 aiosqlite 的 sqlite3(须先于任何连接);
        # 不在场时仍构造加密引擎 — SQLCipher 构建的解释器无需 shim,
        # 香草解释器由 initialize() 的 cipher_version 校验兜底报错
        install_shim()
        engine = StorageEngine(resolved_db, encryption_key=db_key)
    middleware_cls: type[MemoryMiddleware] = (
        TelemetryMemoryMiddleware if telemetry.enabled else MemoryMiddleware
    )
    if middleware_cls is MemoryMiddleware:
        return MemoryMiddleware(
            resolved_db,
            engine=engine,
            embedder=embedder,
            reranker=reranker,
            fact_extractor=extractor,
            pii_masker=pii_masker,
            track_access=config.access_tracking,
        )
    wrapped = TelemetryMemoryMiddleware(
        resolved_db,
        engine=engine,
        embedder=embedder,
        reranker=reranker,
        fact_extractor=extractor,
        pii_masker=pii_masker,
        track_access=config.access_tracking,
        telemetry=telemetry,
    )
    wrapped.telemetry_channel = "mcp"
    return wrapped


def _infer_project_root(db_path: Path) -> Path | None:
    """stdio 模式从 db 路径 ``<项目根>/.smilex/memory.db`` 推断项目根.

    全局默认库 ``~/.smilex/memory.db`` 同样匹配该形状,显式排除(家目录
    不是项目根);推断结果必须真实存在。
    """
    if db_path.parent.name != ".smilex":
        return None
    root = db_path.parent.parent
    if root == Path.home() or not root.is_dir():
        return None
    return root


def _parse_scope(scope: str | None) -> MemoryScope:
    """MCP 工具字符串 scope 参数 → MemoryScope(非法值给清晰错误)."""
    if scope is None:
        return MemoryScope.PROJECT
    try:
        return MemoryScope(scope)
    except ValueError as e:
        raise ValueError(
            f"scope 必须是 project/tenant/global 之一,得到 {scope!r}"
        ) from e


def create_mcp_server(service: MemoryService) -> MCPServer:
    """创建 MCP server 并注册记忆工具(stdio / streamable-http 复用)."""
    _require_mcp()
    from mcp.server.mcpserver import MCPServer

    server: MCPServer = MCPServer(
        name="smilex-memory",
        description="SmileX Agent 长期记忆: 时序/图谱/因果/语义多路召回",
        instructions=(
            "涉及项目事实、历史决策、个人偏好时先调 memory_recall 获取上下文;"
            "任务完成或得到新结论后调 memory_write 沉淀。"
            "同一对话保持相同 session_id。"
        ),
    )

    @server.tool(
        description="检索长期记忆: 多路召回(L0 工作记忆 + 向量 KNN + 时序/图谱/因果混合)"
        "并按 token 预算裁剪。回答涉及项目事实/历史/偏好前先调用。"
    )
    async def memory_recall(
        query: str,
        session_id: str = DEFAULT_SESSION_ID,
        project: str | None = None,
        top_k: int = 10,
        token_budget: int | None = None,
    ) -> str:
        """检索记忆,返回拼接好的上下文与来源明细(JSON)."""
        memory = await service.get()
        resp = await memory.recall(
            RecallRequest(
                query=query,
                scope_filter=ScopeFilter(include_project=project),
                top_k=top_k,
                token_budget=token_budget or service.config.token_budget,
            ),
            session_id=session_id,
        )
        return json.dumps(resp.to_dict(), ensure_ascii=False)

    @server.tool(
        description="写入长期记忆: 沉淀事实/决策/结论。entities 为相关实体名,"
        "relations 为主谓宾三元组。任务完成或得到新结论时调用。"
        "scope=project 时需传 scope_id(项目名)或先调 memory_init_project。"
    )
    async def memory_write(
        content: str,
        session_id: str = DEFAULT_SESSION_ID,
        scope: str = "project",
        scope_id: str | None = None,
        entities: list[str] | None = None,
        relations: list[dict] | None = None,
        importance: float = 0.5,
    ) -> str:
        """写入一条记忆,返回 memory_id 与涉及层(JSON)."""
        memory = await service.get()
        resp = await memory.write(
            WriteRequest(
                scope=_parse_scope(scope),
                content=content,
                entities=list(entities or []),
                relations=[TripleInput(**r) for r in (relations or [])],
                importance=importance,
            ),
            session_id=session_id,
            scope_id=scope_id,
        )
        return json.dumps(resp.to_dict(), ensure_ascii=False)

    @server.tool(
        description="新项目冷启动 + 扫描生成初始记忆: 传入 project_path 自动读 README、"
        "导入 git 历史/markdown 文档/源码结构(.py AST 提取模块文档、顶层定义与依赖)"
        "为 L1 记忆并建立 project scope(幂等可重跑)。"
        "首次在某项目使用记忆服务时调用一次;stdio 模式 project_path 可省略。"
    )
    async def memory_init_project(
        name: str,
        description: str = "",
        tech_stack: list[str] | None = None,
        readme_content: str | None = None,
        project_path: str | None = None,
        scan_git: bool = True,
        scan_markdown: bool = True,
        scan_code: bool = True,
        max_commits: int | None = None,
    ) -> str:
        """冷启动 + 扫描导入,返回 scope、种子统计与各源导入结果(JSON).

        project_path 缺省时: stdio 模式按 db 路径(<项目根>/.smilex/memory.db)
        自动推断项目根;HTTP 全局模式不推断,需显式传参。
        """
        memory = await service.get()
        if project_path is None and service.mode == "stdio":
            inferred = _infer_project_root(service.config.resolved_db_path())
            if inferred is not None:
                project_path = str(inferred)
        result = await memory.bootstrap_project(
            name,
            project_path=project_path,
            description=description,
            tech_stack=list(tech_stack or []),
            readme_content=readme_content,
            scan_git=scan_git,
            scan_markdown=scan_markdown,
            scan_code=scan_code,
            max_commits=max_commits,
        )
        return json.dumps(result, ensure_ascii=False)

    @server.tool(description="记忆统计: 各层/各 scope 的条目计数(调试与面板用)。")
    async def memory_stats(scope: str | None = None) -> str:
        """返回实体/三元组/时序片段/向量等计数(JSON;与 /api/stats 同一实现)."""
        memory = await service.get()
        stats = await collect_stats(
            memory.engine.conn, service.config.resolved_db_path(), scope=scope
        )
        return json.dumps(stats, ensure_ascii=False)

    return server


async def run_stdio(config: ServerConfig) -> None:
    """stdio 模式入口(smilex-memory mcp).

    日志走 stderr(stdout 被 MCP 协议占用);默认 info 级 JSON。
    """
    from ..memory.observability import configure_logging

    configure_logging("info")
    service = MemoryService(config, mode="stdio")
    server = create_mcp_server(service)
    try:
        await server.run_stdio_async()
    finally:
        await service.close()
