"""MCP server — 把 MemoryMiddleware 暴露为 MCP 工具(server 层).

两种传输模式复用同一工具集:
- stdio: ``smilex-memory mcp``(项目独立库场景,每工具进程独立)
- streamable HTTP: ``smilex-memory serve`` 挂载到 FastAPI(全局常驻,推荐)

工具约定(返回 JSON 文本):
- memory_recall: 检索记忆上下文(回答涉及项目事实/历史决策前先调);
  可选 entity(实体名/ID,触发图谱策略)与 time_start/time_end(触发时序策略)
- memory_write: 沉淀新事实/结论(任务完成、得到新决策时调;带
  error_fingerprint 时按教训协议写【教训】并回链错误指纹)
- memory_report_error: 错误指纹登记(出错时调;同指纹第 2 次起提示沉淀
  教训,已有教训则直接回传内容)
- memory_init_project: 新项目冷启动 + 扫描生成初始记忆(README/git 历史/
  markdown 文档/源码结构;project_path 缺省时 stdio 模式按 db 路径推断项目根)
- memory_graph_query: 图谱结构查询(实体最短路径 / N 度关系 / 因果链追溯),
  memory_recall 语义召回之外的图遍历出口
- memory_stats: 各层记忆计数(调试/面板)

session_id: 同一对话会话内保持一致可获得 L0 工作记忆加速;缺省 "default"。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..memory.embedder import EmbedderConfig, get_embedder
from ..memory.extractor import ExtractorConfig, get_extractor
from ..memory.models import MemoryScope, ScopeFilter, TimeRange
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
from ..utils.timeutil import from_iso
from .config import ServerConfig
from .lessons import (
    build_advice,
    link_lesson,
    load_lesson_content,
    prepare_lesson_write,
    register_error,
    resolve_scope_str,
)
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


async def resolve_entity_ref(
    conn: Any, ref: str, *, project: str | None = None
) -> str | None:
    """实体名 / 归一化 entity_id / ULID → entities.id(recall 图谱策略与图查询共用).

    只读解析,不自动建实体(写入路径的 get-or-create 在 _write_path)。
    同名实体跨 scope 歧义时优先指定 project 的 scope,其次 global。
    """
    if not ref:
        return None
    cursor = await conn.execute(
        "SELECT id, scope FROM entities WHERE id = ? OR entity_id = ? OR name = ? "
        "LIMIT 25",
        [ref, ref, ref],
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    if not rows:
        return None
    project_scope = f"project:{project}" if project else None

    def _rank(row: dict) -> tuple[int, str]:
        if project_scope and row["scope"] == project_scope:
            return (0, "")
        if row["scope"] == "global":
            return (1, "")
        return (2, str(row["scope"]))

    return str(min(rows, key=_rank)["id"])


def parse_time_range(time_start: str | None, time_end: str | None) -> TimeRange | None:
    """ISO 时间字符串对 → TimeRange(approx 模式;单边缺省由检索层补 now)."""
    if not time_start and not time_end:
        return None
    return TimeRange(
        approx_start=from_iso(time_start) if time_start else None,
        approx_end=from_iso(time_end) if time_end else None,
    )


async def _entity_names(conn: Any, ids: list[str]) -> dict[str, str]:
    """entities.id 列表 → {id: name}(查无的 id 不进结果,调用方回退显示原 id)."""
    unique = list(dict.fromkeys(i for i in ids if i))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    cursor = await conn.execute(
        f"SELECT id, name FROM entities WHERE id IN ({placeholders})", unique
    )
    return {str(r["id"]): str(r["name"]) for r in await cursor.fetchall()}


async def _triple_summaries(conn: Any, ids: list[str]) -> list[dict]:
    """triples.id 列表 → 摘要列表(端点尽量换实体名,便于直接阅读)."""
    unique = list(dict.fromkeys(i for i in ids if i))
    if not unique:
        return []
    placeholders = ",".join("?" for _ in unique)
    cursor = await conn.execute(
        "SELECT id, subject_id, predicate, object_id, object_value, relation_type "
        f"FROM triples WHERE id IN ({placeholders})",
        unique,
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    endpoint_ids = [str(r["subject_id"]) for r in rows]
    endpoint_ids += [str(r["object_id"]) for r in rows if r["object_id"]]
    names = await _entity_names(conn, endpoint_ids)
    return [
        {
            "id": str(r["id"]),
            "subject": names.get(str(r["subject_id"]), str(r["subject_id"])),
            "predicate": str(r["predicate"]),
            "object": (
                names.get(str(r["object_id"]), str(r["object_id"]))
                if r["object_id"]
                else str(r["object_value"] or "?")
            ),
            "relation_type": str(r["relation_type"]),
        }
        for r in rows
    ]


def create_mcp_server(service: MemoryService) -> MCPServer:
    """创建 MCP server 并注册记忆工具(stdio / streamable-http 复用)."""
    _require_mcp()
    from mcp.server.mcpserver import MCPServer

    server: MCPServer = MCPServer(
        name="smilex-memory",
        description="SmileX Agent 长期记忆: 时序/图谱/因果/语义多路召回",
        instructions=(
            "涉及项目事实、历史决策、个人偏好时先调 memory_recall 获取上下文"
            "(可传 entity 实体名聚焦图谱检索、time_start/time_end 聚焦时序检索);"
            "需要结构化图遍历(两实体间路径/实体 N 度关系/因果链追溯)时调"
            " memory_graph_query。"
            "任务完成或得到新结论后调 memory_write 沉淀。"
            "遇到报错/失败时调 memory_report_error(code, message):"
            "响应含 lesson 则直接遵循;提示 should_write_lesson 则排查后用"
            " memory_write(error_fingerprint=...) 沉淀教训 — 写清错误原因/"
            "失败条件与适用边界/可保留做法,禁止把条件性失败记成绝对结论。"
            "同一对话保持相同 session_id。"
        ),
    )

    @server.tool(
        description="检索长期记忆: 多路召回(L0 工作记忆 + 向量 KNN + 时序/图谱/因果混合)"
        "并按 token 预算裁剪。回答涉及项目事实/历史/偏好前先调用。"
        "可选聚焦参数: entity(实体名或 ID,触发图谱 N 度扩展策略)、"
        "time_start/time_end(ISO 时间,触发时序策略)。"
    )
    async def memory_recall(
        query: str,
        session_id: str = DEFAULT_SESSION_ID,
        project: str | None = None,
        top_k: int = 10,
        token_budget: int | None = None,
        entity: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
    ) -> str:
        """检索记忆,返回拼接好的上下文与来源明细(JSON)."""
        memory = await service.get()
        entity_filter: str | None = None
        if entity:
            entity_filter = await resolve_entity_ref(
                memory.engine.conn, entity, project=project
            )
            if entity_filter is None:
                raise ValueError(
                    f"未找到实体 {entity!r}(接受实体名/归一化 ID/ULID;"
                    "实体随 memory_write 的 entities/relations 建立)"
                )
        resp = await memory.recall(
            RecallRequest(
                query=query,
                scope_filter=ScopeFilter(include_project=project),
                time_range=parse_time_range(time_start, time_end),
                entity_filter=entity_filter,
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
        "带 error_fingerprint(memory_report_error 返回的指纹)时按教训协议:"
        "内容加【教训】前缀、重要度提升至删除守卫保护线,写入后回链指纹 —"
        "内容需含 错误原因/失败条件与适用边界/可保留做法,"
        "禁止把条件性失败记成绝对结论。"
    )
    async def memory_write(
        content: str,
        session_id: str = DEFAULT_SESSION_ID,
        scope: str = "project",
        scope_id: str | None = None,
        entities: list[str] | None = None,
        relations: list[dict] | None = None,
        importance: float = 0.5,
        error_fingerprint: str | None = None,
    ) -> str:
        """写入一条记忆,返回 memory_id 与涉及层(JSON)."""
        memory = await service.get()
        if error_fingerprint:
            content, importance = prepare_lesson_write(content, importance)
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
        if error_fingerprint and resp.memory_id:
            # 教训必须持久: 短内容默认留 L0(会话内存),显式晋升 L1 落库
            await memory.promote_memory(
                resp.memory_id, session_id=session_id, scope_id=scope_id
            )
            await link_lesson(memory.engine.conn, error_fingerprint, resp.memory_id)
        return json.dumps(resp.to_dict(), ensure_ascii=False)

    @server.tool(
        description="错误指纹登记(出错/任务失败时调用): 同一错误归一到指纹"
        "计数。响应含已有教训(lesson)则直接遵循;should_write_lesson=true 时"
        "用 memory_write(error_fingerprint=指纹) 沉淀教训(三段式: 错误原因/"
        "失败条件与适用边界/可保留做法,禁止把条件性失败记成绝对结论)。"
    )
    async def memory_report_error(
        code: str,
        message: str,
        session_id: str = DEFAULT_SESSION_ID,
        scope: str = "project",
        scope_id: str | None = None,
    ) -> str:
        """登记错误指纹,返回计数/教训闭环状态(JSON)."""
        memory = await service.get()
        scope_str = resolve_scope_str(memory, scope, scope_id)
        status = await register_error(
            memory.engine.conn, scope_str=scope_str, code=code, message=message
        )
        lesson_content: str | None = None
        if status["lesson_id"]:
            lesson_content = await load_lesson_content(
                memory.engine.conn, str(status["lesson_id"])
            )
        payload = build_advice(status, lesson_content)
        return json.dumps(payload, ensure_ascii=False)

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

    @server.tool(
        description="图谱结构查询(memory_recall 语义召回之外的图遍历出口): "
        "mode=path 求两实体间最短路径(src/dst);mode=neighbors 求实体 N 度关系"
        "(entity);mode=causal 沿因果链追溯(triple_id,direction=backward 根因/"
        "forward 影响范围/both 双向)。实体参数接受实体名/归一化 ID/ULID;"
        "relation_types 逗号分隔限定边类型(causal/spatial/temporal/semantic/"
        "project_state/task_status/config,仅 path/neighbors 生效 — 因果链沿"
        "predecessor_id 结构遍历,边类型恒为 causal)。返回带实体名与谓词摘要的 JSON。"
    )
    async def memory_graph_query(
        mode: str,
        src: str | None = None,
        dst: str | None = None,
        entity: str | None = None,
        triple_id: str | None = None,
        direction: str = "both",
        max_depth: int | None = None,
        relation_types: str | None = None,
        project: str | None = None,
    ) -> str:
        """图遍历查询: 实体路径 / N 度关系 / 因果链(JSON)."""
        memory = await service.get()
        conn = memory.engine.conn
        scope_filter = ScopeFilter(include_project=project)
        types = [t.strip() for t in (relation_types or "").split(",") if t.strip()] or None

        async def _resolve(ref: str | None, what: str) -> str:
            if not ref:
                raise ValueError(f"mode={mode} 需要 {what} 参数")
            resolved = await resolve_entity_ref(conn, ref, project=project)
            if resolved is None:
                raise ValueError(f"未找到实体 {ref!r}")
            return resolved

        if mode == "path":
            src_id = await _resolve(src, "src")
            dst_id = await _resolve(dst, "dst")
            found = await memory.engine.find_path(
                src_id,
                dst_id,
                max_depth=max_depth or 5,
                scope_filter=scope_filter,
                relation_types=types,
            )
            if found is None:
                payload: dict = {"found": False}
            else:
                names = await _entity_names(conn, found["nodes"])
                payload = {
                    "found": True,
                    "depth": found["depth"],
                    "nodes": [
                        {"id": nid, "name": names.get(nid, nid)}
                        for nid in found["nodes"]
                    ],
                    "edges": await _triple_summaries(conn, found["edges"]),
                }
        elif mode == "neighbors":
            eid = await _resolve(entity, "entity")
            relations = await memory.engine.find_n_degree_relations(
                eid,
                max_depth=max_depth or 2,
                scope_filter=scope_filter,
                relation_types=types,
            )
            lookup_ids = [eid] + [r["entity_id"] for r in relations]
            for r in relations:
                lookup_ids.extend(r.get("path_nodes", []))
            names = await _entity_names(conn, lookup_ids)
            decorated = []
            for r in relations:
                item: dict = {
                    "entity_id": r["entity_id"],
                    "name": names.get(r["entity_id"], r["entity_id"]),
                    "depth": r["depth"],
                }
                if "path_nodes" in r:
                    item["path"] = [names.get(n, n) for n in r["path_nodes"]]
                decorated.append(item)
            payload = {
                "entity": {"id": eid, "name": names.get(eid, eid)},
                "relations": decorated,
            }
        elif mode == "causal":
            if not triple_id:
                raise ValueError("mode=causal 需要 triple_id 参数")
            chain = await memory.engine.trace_causal_chain(
                triple_id,
                direction=direction,
                max_depth=max_depth or 20,
                scope_filter=scope_filter,
            )
            chain_ids = [chain["start"]]
            for key in ("backward", "forward"):
                chain_ids.extend(item["id"] for item in chain.get(key, []))
            summaries = {s["id"]: s for s in await _triple_summaries(conn, chain_ids)}
            payload = {
                "start": summaries.get(chain["start"], {"id": chain["start"]})
            }
            for key in ("backward", "forward"):
                if key in chain:
                    payload[key] = [
                        {
                            "depth": item["depth"],
                            "triple": summaries.get(
                                item["id"], {"id": item["id"]}
                            ),
                        }
                        for item in chain[key]
                    ]
        else:
            raise ValueError(f"mode 必须是 path/neighbors/causal,得到 {mode!r}")
        return json.dumps(payload, ensure_ascii=False)

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
