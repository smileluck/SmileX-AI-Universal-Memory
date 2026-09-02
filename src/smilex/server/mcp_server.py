"""MCP server — 把 MemoryMiddleware 暴露为 MCP 工具(server 层).

两种传输模式复用同一工具集:
- stdio: ``smilex-memory mcp``(项目独立库场景,每工具进程独立)
- streamable HTTP: ``smilex-memory serve`` 挂载到 FastAPI(全局常驻,推荐)

工具约定(返回 JSON 文本):
- memory_recall: 检索记忆上下文(回答涉及项目事实/历史决策前先调)
- memory_write: 沉淀新事实/结论(任务完成、得到新决策时调)
- memory_init_project: 新项目冷启动(导入 README/git 历史 + 种子)
- memory_stats: 各层记忆计数(调试/面板)

session_id: 同一对话会话内保持一致可获得 L0 工作记忆加速;缺省 "default"。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from ..memory.lifecycle.embedder import EmbedderConfig, get_embedder
from ..memory.lifecycle.extractor import ExtractorConfig, get_extractor
from ..memory.lifecycle.reranker import RerankerConfig, get_reranker
from ..memory.models import MemoryScope, ScopeFilter
from ..middlewares.dto import (
    ProjectInitRequest,
    RecallRequest,
    TripleInput,
    WriteRequest,
)
from ..middlewares.memory import MemoryMiddleware
from .config import ServerConfig

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

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

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._memory: MemoryMiddleware | None = None
        self._lock = asyncio.Lock()

    @property
    def config(self) -> ServerConfig:
        return self._config

    async def get(self) -> MemoryMiddleware:
        """取已初始化的 MemoryMiddleware(首次调用时初始化)."""
        if self._memory is not None:
            return self._memory
        async with self._lock:
            if self._memory is None:
                embedder = get_embedder(EmbedderConfig(backend=self._config.embedder))
                reranker = get_reranker(RerankerConfig(backend=self._config.reranker))
                extractor = get_extractor(
                    ExtractorConfig(backend=self._config.fact_extractor)
                )
                memory = MemoryMiddleware(
                    self._config.resolved_db_path(),
                    embedder=embedder,
                    reranker=reranker,
                    fact_extractor=extractor,
                )
                await memory.initialize()
                self._memory = memory
        return self._memory

    async def close(self) -> None:
        if self._memory is not None:
            await self._memory.close()
            self._memory = None


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
        description="新项目冷启动: 导入 README 摘要/技术栈种子,建立 project scope。"
        "首次在某项目使用记忆服务时调用一次。"
    )
    async def memory_init_project(
        name: str,
        description: str = "",
        tech_stack: list[str] | None = None,
        readme_content: str | None = None,
    ) -> str:
        """冷启动初始化,返回 scope 与种子统计(JSON)."""
        memory = await service.get()
        resp = await memory.initialize_project(
            ProjectInitRequest(
                name=name,
                description=description,
                tech_stack=list(tech_stack or []),
                readme_content=readme_content,
            )
        )
        return json.dumps(resp.to_dict(), ensure_ascii=False)

    @server.tool(description="记忆统计: 各层/各 scope 的条目计数(调试与面板用)。")
    async def memory_stats(scope: str | None = None) -> str:
        """返回实体/三元组/时序片段/向量的计数(JSON)."""
        memory = await service.get()
        conn = memory.engine.conn
        stats: dict[str, int] = {}
        params: list[str] = []
        where = ""
        if scope:
            where = " WHERE scope = ?"
            params = [scope]
        for table in ("entities", "triples", "temporal_fragments"):
            cursor = await conn.execute(
                f"SELECT COUNT(*) FROM {table}{where}", params
            )
            stats[table] = int((await cursor.fetchone())[0])
        cursor = await conn.execute("SELECT COUNT(*) FROM vector_links")
        stats["vector_links"] = int((await cursor.fetchone())[0])
        return json.dumps(stats, ensure_ascii=False)

    return server


async def run_stdio(config: ServerConfig) -> None:
    """stdio 模式入口(smilex-memory mcp)."""
    service = MemoryService(config)
    server = create_mcp_server(service)
    try:
        await server.run_stdio_async()
    finally:
        await service.close()
