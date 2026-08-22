"""FastAPI app 工厂(server 层)— MCP streamable-http + 只读面板.

路由布局(顺序即优先级):
1. /api/*  只读 JSON API(api.py)
2. /static/* + /  静态面板(panel/)
3. Mount("/")  MCP streamable-http 子应用(内部路径 /mcp)— 兜底挂载

生命周期: 启动时初始化 MemoryService(单写者)+ 可选调度器(5 类核心任务),
并级联 MCP 子应用的 lifespan(session manager 任务组)。
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from .api import create_api_router
from .config import ServerConfig
from .mcp_server import MemoryService, _require_mcp, create_mcp_server

if TYPE_CHECKING:
    from fastapi import FastAPI

PANEL_DIR = Path(__file__).parent / "panel"


def create_app(config: ServerConfig) -> FastAPI:
    """创建 HTTP 服务应用(MCP + 面板 + API)."""
    _require_mcp()
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    service = MemoryService(config)
    mcp_server = create_mcp_server(service)
    # stateless: 记忆状态在 MemoryService/SQLite,不依赖 MCP 会话状态
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            await service.get()  # 提前初始化,启动即暴露 db 错误
            if config.enable_scheduler:
                from ..memory.scheduler.scheduler import MemoryTaskScheduler
                from ..memory.scheduler.tasks import register_core_tasks

                memory = await service.get()
                scheduler = MemoryTaskScheduler(memory.engine)
                register_core_tasks(scheduler, memory.engine)
                await scheduler.start()
                stack.push_async_callback(scheduler.stop)
            # 级联 MCP 子应用 lifespan(session manager)
            await stack.enter_async_context(
                mcp_app.router.lifespan_context(mcp_app)
            )
            stack.push_async_callback(service.close)
            yield

    app = FastAPI(title="SmileX Memory Server", lifespan=lifespan)
    app.include_router(create_api_router(service))

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(PANEL_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=PANEL_DIR), name="static")
    # 兜底挂载 MCP 子应用(内部路由 /mcp);必须在 API/静态路由之后
    app.mount("/", mcp_app)
    return app


async def serve(config: ServerConfig) -> None:
    """启动 uvicorn(smilex-memory serve)."""
    try:
        import uvicorn
    except ImportError as e:
        raise ImportError(
            "serve 需要 uvicorn,安装: pip install 'smilex-ai-memory[server]'"
        ) from e
    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_level="info")
    )
    await server.serve()
