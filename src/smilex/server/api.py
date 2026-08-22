"""面板 JSON API(server 层)— 只读监控 + 召回调试.

端点:
- GET  /api/stats        各层/各 scope 计数
- GET  /api/memories     浏览/关键词搜索(entities/triples/fragments 联合)
- GET  /api/memory/{id}  单条详情
- POST /api/recall-test  召回调试(返回上下文 + 来源 + 耗时)
- GET  /api/tasks        调度任务断点(最近运行)列表

不提供写操作: 写入统一走 MCP 工具(memory_write),面板保持只读。
"""

from __future__ import annotations

import time
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
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        """联合浏览: fragments(按 layer)+ entities + triples,关键词 LIKE 过滤."""
        memory = await service.get()
        conn = memory.engine.conn
        items: list[dict[str, Any]] = []

        frag_where, frag_params = "1=1", []
        if layer:
            frag_where += " AND layer = ?"
            frag_params.append(layer)
        if scope:
            frag_where += " AND scope = ?"
            frag_params.append(scope)
        if q:
            frag_where += " AND content LIKE ?"
            frag_params.append(f"%{q}%")
        cur = await conn.execute(
            "SELECT id, content, scope, layer, importance, updated_at "
            f"FROM temporal_fragments WHERE {frag_where} "
            "ORDER BY updated_at DESC LIMIT ?",
            [*frag_params, limit],
        )
        for r in await cur.fetchall():
            items.append({
                "kind": "fragment",
                "id": r["id"],
                "content": r["content"],
                "scope": r["scope"],
                "layer": r["layer"],
                "importance": r["importance"],
                "updated_at": r["updated_at"],
            })

        if layer in (None, "L2"):  # triples 属于 L2 语义层
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

        if q or layer in (None, "L1"):
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
                return {"table": table, "row": dict(row)}
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
