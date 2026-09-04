"""面板 JSON API(server 层)— 只读监控 + 召回调试.

端点:
- GET  /api/health       运行状态(uptime + 配置摘要,面板状态区轮询)
- GET  /api/stats        各层/各 scope 计数 + L0 会话/因果链/库体积
- GET  /api/memories     浏览/关键词搜索(entities/triples/fragments 联合,
                         fragments 关键词走 FTS5 BM25,trigram 支持中文子串)
- GET  /api/memory/{id}  单条详情
- POST /api/recall-test  召回调试(返回上下文 + 来源 + 耗时)
- GET  /api/tasks        调度任务断点(最近运行)列表

不提供写操作: 写入统一走 MCP 工具(memory_write),面板保持只读。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..middlewares.dto import RecallRequest

if TYPE_CHECKING:
    from fastapi import APIRouter

    from .mcp_server import MemoryService


class RecallTestBody(BaseModel):
    """POST /api/recall-test 请求体(模块级定义: FastAPI 需经模块命名空间解析注解)."""

    query: str
    session_id: str = "panel-debug"
    top_k: int = 10
    token_budget: int | None = None


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


def create_api_router(service: MemoryService) -> APIRouter:
    """创建只读 API 路由(挂在 FastAPI app 的 /api 前缀下)."""
    _require_fastapi()
    from fastapi import APIRouter, Query

    router = APIRouter(prefix="/api")
    started_at = time.time()

    @router.get("/health")
    async def health() -> dict[str, Any]:
        c = service.config
        return {
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
        }

    @router.get("/stats")
    async def stats(scope: str | None = None) -> dict[str, Any]:
        memory = await service.get()
        conn = memory.engine.conn
        where, params = (" WHERE scope = ?", [scope]) if scope else ("", [])
        result: dict[str, Any] = {}
        for table in ("entities", "triples", "temporal_fragments"):
            cur = await conn.execute(f"SELECT COUNT(*) FROM {table}{where}", params)
            result[table] = int((await cur.fetchone())[0])
        cur = await conn.execute("SELECT COUNT(*) FROM vector_links")
        result["vector_links"] = int((await cur.fetchone())[0])
        for table, key in (
            ("memory_l0_snapshot", "l0_snapshots"),
            ("causal_chains", "causal_chains"),
        ):
            cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
            result[key] = int((await cur.fetchone())[0])
        db_file = service.config.resolved_db_path()
        result["db_size_bytes"] = db_file.stat().st_size if db_file.exists() else 0
        # 分层计数(temporal_fragments.layer)
        cur = await conn.execute(
            f"SELECT layer, COUNT(*) AS c FROM temporal_fragments{where} GROUP BY layer",
            params,
        )
        result["fragments_by_layer"] = {
            str(r["layer"]): int(r["c"]) for r in await cur.fetchall()
        }
        # scope 分布
        cur = await conn.execute(
            "SELECT scope, COUNT(*) AS c FROM entities GROUP BY scope ORDER BY c DESC"
        )
        result["entity_scopes"] = {
            str(r["scope"]): int(r["c"]) for r in await cur.fetchall()
        }
        return result

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
        start = time.monotonic()
        resp = await memory.recall(
            RecallRequest(
                query=body.query,
                top_k=body.top_k,
                token_budget=body.token_budget or service.config.token_budget,
            ),
            session_id=body.session_id,
        )
        return {
            **resp.to_dict(),
            "elapsed_ms": int((time.monotonic() - start) * 1000),
        }

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
