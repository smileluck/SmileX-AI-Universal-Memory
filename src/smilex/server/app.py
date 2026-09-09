"""FastAPI app 工厂(server 层)— MCP streamable-http + 只读面板.

路由布局(顺序即优先级):
1. /api/*  只读 JSON API(api.py)
2. /metrics  Prometheus 文本格式指标(§15.3,config.metrics 开关)
3. /static/* + /  静态面板(panel/)
4. Mount("/")  MCP streamable-http 子应用(内部路径 /mcp)— 兜底挂载

生命周期: 启动时初始化 MemoryService(单写者)+ 可选调度器(核心任务,
含每日 SQLite 巡检,挂终态观察者做任务计数),并级联 MCP 子应用的
lifespan(session manager 任务组)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..memory.observability import (
    attach_runtime_gauges,
    make_scheduler_observer,
)
from .api import create_api_router
from .auth import install_auth_middleware
from .config import ServerConfig
from .mcp_server import MemoryService, _require_mcp, create_mcp_server

if TYPE_CHECKING:
    from fastapi import FastAPI

PANEL_DIR = Path(__file__).parent / "panel"


def create_app(config: ServerConfig) -> FastAPI:
    """创建 HTTP 服务应用(MCP + 面板 + API)."""
    _require_mcp()
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, PlainTextResponse, Response
    from fastapi.staticfiles import StaticFiles

    service = MemoryService(config)
    mcp_server = create_mcp_server(service)
    # stateless: 记忆状态在 MemoryService/SQLite,不依赖 MCP 会话状态
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await service.get()  # 提前初始化,启动即暴露 db 错误
            if config.enable_scheduler:
                from ..memory.scheduler.scheduler import MemoryTaskScheduler
                from ..memory.scheduler.tasks import register_core_tasks
                from ..memory.summarizer import SummarizerConfig, get_summarizer

                memory = await service.get()
                # 维护引擎: 调度任务/checkpoint 与 HTTP 业务写分离 —
                # CheckpointStore 的裸 commit 与业务多语句写在同一连接上
                # 交错时会把半成品事务提前刷盘;独立引擎(WAL + busy_timeout
                # 承载双连接写竞争)消除该窗口
                from ..memory.storage.storage_engine import StorageEngine

                db_key = config.effective_db_key
                if db_key is not None:
                    from ..memory.storage.sqlcipher import install_shim

                    install_shim()  # 进程级幂等,见 build_middleware
                maintenance_engine = StorageEngine(
                    config.resolved_db_path(), encryption_key=db_key
                )
                await maintenance_engine.initialize()
                stack.push_async_callback(maintenance_engine.close)
                scheduler = MemoryTaskScheduler(
                    maintenance_engine,
                    observer=make_scheduler_observer(service.telemetry),
                )
                from ..memory.scheduler.tasks import CoreTaskConfig

                register_core_tasks(
                    scheduler,
                    maintenance_engine,
                    config=CoreTaskConfig(
                        forget_max_per_scope=config.max_records_per_scope,
                        forget_protect_importance=config.protect_importance,
                    ),
                    summarizer=get_summarizer(
                        SummarizerConfig(backend=config.summarizer)
                    ),
                    # semantic 任务 L3 社区缓存经此进 KNN 通道
                    # (与写入路径共享 embedder/LRU;add_text 接收任务侧连接)
                    vector_store=memory.vector_store,
                )
                # Layer 5 质量任务默认接线(此前无宿主注册,生产部署下
                # 归档保留策略与跨项目提升永不发生):
                # - archiver: 历史 365 天/片段 180 天冷数据归档(每日)
                # - scope_promoter: 跨项目共现自动提升到 global(每日)
                from ..memory.quality.archiver import register_archive_task
                from ..memory.quality.scope_promoter import (
                    register_scope_promotion_task,
                )

                register_archive_task(
                    scheduler, maintenance_engine, interval_seconds=86400
                )
                register_scope_promotion_task(
                    scheduler, maintenance_engine, interval_seconds=86400
                )
                # 事件接线: middleware 的 session_end/memory_full 经调度器
                # 映射触发 forget/consolidate(事件触发从死配置变为活链路)
                memory.event_sink = scheduler.emit
                # 回填句柄供 /api/health 读队列深度;关停时清理
                service.scheduler = scheduler
                await scheduler.start()
                stack.push_async_callback(scheduler.stop)
            # 级联 MCP 子应用 lifespan(session manager)
            await stack.enter_async_context(
                mcp_app.router.lifespan_context(mcp_app)
            )
            stack.push_async_callback(service.close)
            yield

    app = FastAPI(title="SmileX Memory Server", lifespan=lifespan)
    app.state.service = service  # 测试/诊断可达(调度器句柄经 lifespan 回填)
    app.include_router(create_api_router(service))

    if config.metrics:
        # 运行态 gauge: 队列深度(scheduler 回填前回退 0)/ 库体积 / 巡检结果
        attach_runtime_gauges(
            service.telemetry,
            queue_depth=lambda: (
                service.scheduler.queue_depth if service.scheduler else 0
            ),
            db_size_bytes=lambda: _db_size(config),
        )
        http_total = service.telemetry.metrics.counter(
            "smilex_http_requests_total",
            "HTTP 请求计数(路径模板化,/api/memory/{id} 聚合)",
            ("method", "path"),
        )

        @app.get(
            "/metrics",
            response_class=PlainTextResponse,
            include_in_schema=False,
        )
        async def metrics() -> Response:
            return Response(
                service.telemetry.metrics.render_prometheus(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

        @app.middleware("http")
        async def count_requests(
            request: Any, call_next: Any
        ) -> Any:
            response = await call_next(request)
            path = request.scope.get("path", "")
            if path.startswith("/api"):
                http_total.inc(method=request.method, path=_template(path))
            return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(PANEL_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=PANEL_DIR), name="static")
    # 兜底挂载 MCP 子应用(内部路由 /mcp);必须在 API/静态/metrics 路由之后
    app.mount("/", mcp_app)
    # 鉴权中间件最后安装 = 包在最外层,先于指标计数执行(§15.4;
    # 覆盖 /api/*、/metrics 与兜底 mount 的 MCP;未配置 key 时零安装)
    install_auth_middleware(app, config)
    return app


def _db_size(config: ServerConfig) -> int:
    db_file = config.resolved_db_path()
    return db_file.stat().st_size if db_file.exists() else 0


_ULID_CHARS = set("0123456789ABCDEFGHJKMNPQRSTVWXYZabcdefghjkmnpqrstuvwxyz")


def _template(path: str) -> str:
    """/api 路径模板化: ID 形段(ULID/十六进制/长数字)聚合为 {id} 防基数爆炸."""
    parts = path.strip("/").split("/")
    if len(parts) == 3 and parts[0] == "api" and parts[1] == "memory":
        candidate = parts[2]
        if (
            len(candidate) >= 16
            and (_ULID_CHARS.issuperset(candidate) or candidate.isalnum())
        ):
            return "/api/memory/{id}"
    return path


async def serve(config: ServerConfig, log_level: str = "info") -> None:
    """启动 uvicorn(smilex-memory serve / start)."""
    try:
        import uvicorn
    except ImportError as e:
        raise ImportError(
            "serve 需要 uvicorn,安装: pip install 'smilex-ai-memory[server]'"
        ) from e
    from ..memory.observability import configure_logging

    # uvicorn 的访问/错误日志走自身 log_level;应用结构化日志同步提级
    configure_logging(log_level)
    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_level=log_level)
    )
    await server.serve()
