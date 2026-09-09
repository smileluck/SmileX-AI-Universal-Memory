"""共享统计 — /api/stats 与 MCP memory_stats 的单一实现(server 层).

原先两处(面板 API 与 MCP 工具)各自维护重复的 COUNT SQL,口径漂移
风险高;统一为 collect_stats(conn, db_path):
- 各核心表 COUNT(scope 可选过滤 entities/triples/temporal_fragments)
- l0 快照 / 因果链计数 + 库文件体积
- fragments 分层分布 + entities scope 分布
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


async def collect_stats(
    conn: Any, db_path: Path | None, *, scope: str | None = None
) -> dict[str, Any]:
    """聚合库统计(只读;db_path 为 None 或不存在时体积记 0)."""
    where, params = (" WHERE scope = ?", [scope]) if scope else ("", [])
    result: dict[str, Any] = {}
    for table in ("entities", "triples", "temporal_fragments"):
        cur = await conn.execute(f"SELECT COUNT(*) FROM {table}{where}", params)
        result[table] = int((await cur.fetchone())[0])
    cur = await conn.execute("SELECT COUNT(*) FROM vector_links")
    result["vector_links"] = int((await cur.fetchone())[0])
    for table, key in (
        ("memory_l0_snapshot", "l0_snapshots"),
        ):
        cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
        result[key] = int((await cur.fetchone())[0])
    result["db_size_bytes"] = (
        db_path.stat().st_size if db_path is not None and db_path.exists() else 0
    )
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
