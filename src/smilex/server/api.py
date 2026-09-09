"""面板 JSON API(server 层)— 只读监控 + 召回调试.

端点:
- GET  /api/health       运行状态(uptime + 配置摘要 + db/调度器/告警,
                         面板状态区轮询;status/started_at/uptime_s 三键
                         是 daemon status 的契约字段,保持不变)
- GET  /api/stats        各层/各 scope 计数 + L0 会话/因果链/库体积
- GET  /api/memories     浏览/关键词搜索(entities/triples/fragments 联合,
                         fragments 关键词走 FTS5 BM25,trigram 支持中文子串)
- GET  /api/memory/{id}  单条详情
- POST /api/recall-test  召回调试(返回上下文 + 来源 + 耗时 + entity_resolved)
- GET  /api/errors       错误指纹列表(count 降序,教训闭环可见性)
- GET  /api/tasks        调度任务断点(最近运行)列表

不提供写操作: 写入统一走 MCP 工具(memory_write),面板保持只读。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..memory.observability import get_integrity_report
from ..middlewares.dto import RecallRequest
from .stats import collect_stats

if TYPE_CHECKING:
    from fastapi import APIRouter

    from .mcp_server import MemoryService

# Request 注解需模块级可见(FastAPI 经 get_type_hints 解析延迟注解);
# 经 Any 变量中转避免"类名被二次赋值"的 mypy 冲突
_Request: Any = Any
try:
    from fastapi import Request as _FastAPIRequest

    _Request = _FastAPIRequest
except ImportError:  # 核心(server extras 未装)场景: 仅影响注解,不触发导入
    pass

# §15.3 告警阈值(本地自包含告警;Prometheus 规则样例见 docs/observability-alerts.yaml)
ALERT_QUEUE_DEPTH = 1000
ALERT_DB_BYTES = 10 * 1024**3  # 10GB
ALERT_FAILURE_RATE = 0.05
ALERT_FAILURE_MIN_SAMPLES = 20  # 样本不足不判失败率



class RecallTestBody(BaseModel):
    """POST /api/recall-test 请求体(模块级定义: FastAPI 需经模块命名空间解析注解)."""

    query: str
    session_id: str = "panel-debug"
    top_k: int = 10
    token_budget: int | None = None
    entity: str | None = None  # 实体名/归一化 ID/ULID → 图谱策略聚焦
    time_start: str | None = None  # ISO 时间对 → 时序策略聚焦
    time_end: str | None = None


def _fts_phrase(q: str) -> str:
    """用户输入转 FTS5 短语: 双引号包裹,内部引号加倍,防 MATCH 语法错误."""
    return '"' + q.replace('"', '""') + '"'


async def _fetch_fragments(
    conn: Any, layer: str | None, scope: str | None, q: str | None, limit: int
) -> list[Any]:
    """fragments 检索: 关键词 ≥3 字符走 FTS5 trigram(BM25 排序,中文子串可命中),
    短词或 FTS 不可用(旧库)时回退 LIKE,浏览模式按更新时间倒序."""
    frag_where, frag_params = "1=1", []
    if layer:
        frag_where += " AND tf.layer = ?"
        frag_params.append(layer)
    if scope:
        frag_where += " AND tf.scope = ?"
        frag_params.append(scope)
    if q and len(q) >= 3:
        try:
            cur = await conn.execute(
                "SELECT tf.id, tf.content, tf.scope, tf.layer, tf.importance, "
                "tf.updated_at FROM temporal_fragments tf "
                "JOIN fts_fragments fts ON fts.rowid = tf.rowid "
                f"WHERE fts.fts_fragments MATCH ? AND {frag_where} "
                "ORDER BY fts.rank LIMIT ?",
                [_fts_phrase(q), *frag_params, limit],
            )
            return list(await cur.fetchall())
        except sqlite3.OperationalError:
            pass  # FTS 不可用 → LIKE 回退
    like_where, like_params = frag_where, list(frag_params)
    if q:
        like_where += " AND tf.content LIKE ?"
        like_params.append(f"%{q}%")
    cur = await conn.execute(
        "SELECT tf.id, tf.content, tf.scope, tf.layer, tf.importance, tf.updated_at "
        f"FROM temporal_fragments tf WHERE {like_where} "
        "ORDER BY tf.updated_at DESC LIMIT ?",
        [*like_params, limit],
    )
    return list(await cur.fetchall())


def _require_fastapi() -> None:
    try:
        import fastapi  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "Web 面板需要 fastapi/uvicorn,安装: pip install 'smilex-ai-memory[server]'"
        ) from e


def _db_size(config: Any) -> int:
    db_file = config.resolved_db_path()
    return db_file.stat().st_size if db_file.exists() else 0


def _collect_alerts(service: MemoryService) -> list[dict[str, str]]:
    """本地自包含告警(§15.3 阈值): 队列积压 / 库体积 / 任务失败率."""
    alerts: list[dict[str, str]] = []
    scheduler = service.scheduler
    if scheduler is not None:
        depth = scheduler.queue_depth
        if depth > ALERT_QUEUE_DEPTH:
            alerts.append({
                "name": "queue_backlog",
                "severity": "warning",
                "detail": f"调度队列深度 {depth} > {ALERT_QUEUE_DEPTH}",
            })
        finished = [
            t for t in scheduler.recent_tasks
            if t.status.value in ("completed", "failed")
        ]
        if len(finished) >= ALERT_FAILURE_MIN_SAMPLES:
            failed = sum(1 for t in finished if t.status.value == "failed")
            rate = failed / len(finished)
            if rate > ALERT_FAILURE_RATE:
                alerts.append({
                    "name": "task_failure_rate",
                    "severity": "warning",
                    "detail": f"近 {len(finished)} 个任务失败率 "
                              f"{rate:.0%} > {ALERT_FAILURE_RATE:.0%}",
                })
    db_size = _db_size(service.config)
    if db_size > ALERT_DB_BYTES:
        alerts.append({
            "name": "db_size",
            "severity": "warning",
            "detail": f"库体积 {db_size / 1024**3:.1f}GB > 10GB",
        })
    report = get_integrity_report()
    if report is not None and not report.ok:
        alerts.append({
            "name": "db_integrity",
            "severity": "critical",
            "detail": f"PRAGMA quick_check 异常: {report.detail}",
        })
    return alerts


def create_api_router(service: MemoryService) -> APIRouter:
    """创建只读 API 路由(挂在 FastAPI app 的 /api 前缀下)."""
    _require_fastapi()
    from fastapi import APIRouter, Query

    router = APIRouter(prefix="/api")
    started_at = time.time()

    @router.get("/health")
    async def health(request: _Request) -> dict[str, Any]:
        # 鉴权豁免但条件裁剪: 未带有效 key 时只返回 daemon 契约字段,
        # 配置摘要(db 路径等)不暴露(§15.4)
        auth_required = service.config.effective_api_key is not None
        if auth_required and not getattr(request.state, "auth_ok", False):
            return {
                "status": "ok",
                "started_at": datetime.fromtimestamp(started_at, tz=UTC).isoformat(),
                "uptime_s": round(time.time() - started_at, 1),
                "auth": "required",
            }
        c = service.config
        scheduler = service.scheduler
        report = get_integrity_report()
        return {
            # ---- daemon status 契约字段(不可改动形状) ----
            "status": "ok",
            "started_at": datetime.fromtimestamp(started_at, tz=UTC).isoformat(),
            "uptime_s": round(time.time() - started_at, 1),
            "config": {
                "db_path": str(c.resolved_db_path()),
                "embedder": c.embedder,
                "reranker": c.reranker,
                "fact_extractor": c.fact_extractor,
                "enable_scheduler": c.enable_scheduler,
                "token_budget": c.token_budget,
            },
            # ---- §15.3 扩展: db / 调度器 / 告警 ----
            "db": {
                "connected": service.initialized,
                "size_bytes": _db_size(c),
                "integrity": (
                    None
                    if report is None
                    else {
                        "ok": report.ok,
                        "checked_at": report.checked_at,
                        "detail": report.detail,
                    }
                ),
            },
            "scheduler": {
                "enabled": c.enable_scheduler,
                "queue_depth": scheduler.queue_depth if scheduler else None,
                "current": (
                    scheduler.current_task.name if scheduler and scheduler.current_task else None
                ),
            },
            "alerts": _collect_alerts(service),
        }

    @router.get("/stats")
    async def stats(scope: str | None = None) -> dict[str, Any]:
        memory = await service.get()
        return await collect_stats(
            memory.engine.conn, service.config.resolved_db_path(), scope=scope
        )

    @router.get("/memories")
    async def memories(
        layer: str | None = None,
        scope: str | None = None,
        q: str | None = None,
        kind: str | None = Query(default=None, pattern="^(fragment|entity|triple)$"),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        """联合浏览: fragments(按 layer)+ entities + triples.

        kind 可选过滤(fragment/entity/triple);关键词 q: fragments 走 FTS5
        trigram(BM25 相关度排序,中文子串可命中),<3 字符或 FTS 不可用时回退
        LIKE;entities/triples 始终 LIKE。带 q 时结果保持相关度分组顺序,
        无 q 时按更新时间倒序。
        """
        memory = await service.get()
        conn = memory.engine.conn
        items: list[dict[str, Any]] = []

        if kind in (None, "fragment"):
            for r in await _fetch_fragments(conn, layer, scope, q, limit):
                items.append({
                    "kind": "fragment",
                    "id": r["id"],
                    "content": r["content"],
                    "scope": r["scope"],
                    "layer": r["layer"],
                    "importance": r["importance"],
                    "updated_at": r["updated_at"],
                })

        if kind in (None, "triple") and layer in (None, "L2"):  # triples 属于 L2 语义层
            t_where, t_params = "1=1", []
            if scope:
                t_where += " AND t.scope = ?"
                t_params.append(scope)
            if q:
                t_where += " AND (t.predicate LIKE ? OR t.object_value LIKE ?)"
                t_params.extend([f"%{q}%", f"%{q}%"])
            cur = await conn.execute(
                "SELECT t.id, t.subject_id, t.predicate, t.object_id, t.object_value, "
                "t.scope, t.valid_from FROM triples t "
                f"WHERE {t_where} ORDER BY t.valid_from DESC LIMIT ?",
                [*t_params, limit],
            )
            for r in await cur.fetchall():
                obj = r["object_id"] or r["object_value"] or "?"
                items.append({
                    "kind": "triple",
                    "id": r["id"],
                    "content": f"{r['subject_id']} {r['predicate']} {obj}",
                    "scope": r["scope"],
                    "layer": "L2",
                    "importance": None,
                    "updated_at": r["valid_from"],
                })

        if kind in (None, "entity") and (q or layer in (None, "L1")):
            e_where, e_params = "1=1", []
            if scope:
                e_where += " AND scope = ?"
                e_params.append(scope)
            if q:
                e_where += " AND name LIKE ?"
                e_params.append(f"%{q}%")
            cur = await conn.execute(
                "SELECT id, entity_id, entity_type, name, scope, valid_from "
                f"FROM entities WHERE {e_where} ORDER BY valid_from DESC LIMIT ?",
                [*e_params, limit],
            )
            for r in await cur.fetchall():
                items.append({
                    "kind": "entity",
                    "id": r["id"],
                    "content": f"{r['name']} ({r['entity_type']})",
                    "scope": r["scope"],
                    "layer": "L1",
                    "importance": None,
                    "updated_at": r["valid_from"],
                })

        if not q:  # 搜索模式保持相关度顺序;浏览模式按时间倒序
            items.sort(key=lambda x: x["updated_at"] or "", reverse=True)
        return items[:limit]

    @router.get("/memory/{memory_id}")
    async def memory_detail(memory_id: str) -> dict[str, Any]:
        memory = await service.get()
        conn = memory.engine.conn
        for table, cols in (
            ("temporal_fragments", "*"),
            ("entities", "*"),
            ("triples", "*"),
        ):
            cur = await conn.execute(
                f"SELECT {cols} FROM {table} WHERE id = ?", [memory_id]
            )
            row = await cur.fetchone()
            if row is not None:
                # BLOB(如 embedding)不可 JSON 序列化,替换为占位标记
                clean = {
                    k: (f"<binary {len(v)} bytes>" if isinstance(v, bytes) else v)
                    for k, v in dict(row).items()
                }
                return {"table": table, "row": clean}
        return {"error": f"未找到记忆: {memory_id}"}

    @router.post("/recall-test")
    async def recall_test(body: RecallTestBody) -> dict[str, Any]:
        memory = await service.get()
        # 聚焦参数与 MCP memory_recall 同语义: entity 解析为实体 ID 触发图谱策略,
        # ISO 时间对触发时序策略(辅助函数与 mcp_server 共用,延迟导入避免硬依赖)
        from .mcp_server import parse_time_range, resolve_entity_ref

        entity_filter: str | None = None
        entity_resolved: bool | None = None  # None=未聚焦 / True/False=解析结果
        if body.entity:
            entity_filter = await resolve_entity_ref(
                memory.engine.conn, body.entity
            )
            entity_resolved = entity_filter is not None
        start = time.monotonic()
        resp = await memory.recall(
            RecallRequest(
                query=body.query,
                time_range=parse_time_range(body.time_start, body.time_end),
                entity_filter=entity_filter,
                top_k=body.top_k,
                token_budget=body.token_budget or service.config.token_budget,
            ),
            session_id=body.session_id,
        )
        return {
            **resp.to_dict(),
            "entity_resolved": entity_resolved,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
        }

    @router.get("/errors")
    async def errors(limit: int = 50) -> list[dict[str, Any]]:
        """错误指纹列表(count 降序;教训闭环可见性,面板「错误指纹」区数据源)."""
        memory = await service.get()
        cur = await memory.engine.conn.execute(
            "SELECT fingerprint, count, first_seen, last_seen, sample_code, "
            "sample_message, lesson_id FROM error_fingerprints "
            "ORDER BY count DESC, last_seen DESC LIMIT ?",
            [max(1, min(limit, 500))],
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for row in rows:
            row["has_lesson"] = row.pop("lesson_id") is not None
            msg = str(row.get("sample_message") or "")
            row["sample_message"] = msg[:120]
        return rows

    @router.get("/tasks")
    async def tasks() -> list[dict[str, Any]]:
        """调度任务断点列表(最近运行状态;checkpoints 表即运行记录)."""
        memory = await service.get()
        cur = await memory.engine.conn.execute(
            "SELECT task_id, progress, step, updated_at FROM checkpoints "
            "ORDER BY updated_at DESC LIMIT 50"
        )
        return [dict(r) for r in await cur.fetchall()]

    return router
