"""因果任务(causal_chains 维护)."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from ....utils.ids import generate_id
from ....utils.timeutil import now_utc, to_iso
from ._common import _scope_clause

if TYPE_CHECKING:
    from ...storage.storage_engine import StorageEngine
    from ..checkpoint import InterruptContext


async def causal(
    storage: StorageEngine, ctx: InterruptContext, payload: dict[str, Any]
) -> dict:
    """因果任务 — 基于 triples.predecessor_id 维护 causal_chains 表.

    算法(规则版):
    1. 找「链尾」: 有前驱但不是任何 triple 前驱的 triple
       (predecessor 至多 1 个 → 因果链是线性的,一个链尾唯一确定一条链)
    2. 从链尾 trace_causal_chain(backward) 回溯到根因,得根→尾有序节点
    3. 链长 >= min_chain 时 upsert causal_chains:
       chain_id = "chain:{链尾 id}"(确定性,重跑幂等)
       confidence = 链上节点最小 confidence,support_count = 链长(支撑三元组数)

    payload:
        scope / batch_size / step_delay: 同整合任务
        min_chain: 成链最小长度(默认 2)
    """
    conn = storage.conn
    scope = payload.get("scope")
    batch_size = int(payload.get("batch_size", 50))
    min_chain = int(payload.get("min_chain", 2))
    step_delay = float(payload.get("step_delay", 0.0))

    where = (
        "predecessor_id IS NOT NULL AND id NOT IN "
        "(SELECT predecessor_id FROM triples WHERE predecessor_id IS NOT NULL)"
    )
    where, params = _scope_clause(scope, where, [])
    cursor = await conn.execute(f"SELECT COUNT(*) AS c FROM triples WHERE {where}", params)
    total = int((await cursor.fetchone())["c"])

    last_id = ctx.cursor if isinstance(ctx.cursor, str) else ""
    stats = {"scanned": 0, "chains": 0, "skipped": 0, **ctx.state.get("stats", {})}
    step = ctx.step

    while True:
        cur = await conn.execute(
            f"SELECT id, scope FROM triples WHERE {where} AND id > ? "
            "ORDER BY id LIMIT ?",
            [*params, last_id, batch_size],
        )
        rows = await cur.fetchall()
        if not rows:
            break
        for row in rows:
            stats["scanned"] += 1
            chain = await storage.trace_causal_chain(row["id"], direction="backward")
            node_ids = [e["id"] for e in reversed(chain["backward"])]  # 根 → 尾
            if len(node_ids) < min_chain:
                stats["skipped"] += 1
                continue
            placeholders = ",".join("?" for _ in node_ids)
            conf_cur = await conn.execute(
                f"SELECT MIN(confidence) AS c FROM triples WHERE id IN ({placeholders})",
                node_ids,
            )
            confidence = float((await conf_cur.fetchone())["c"] or 0.0)
            now = to_iso(now_utc())
            await conn.execute(
                "INSERT INTO causal_chains(id, chain_id, node_ids, chain_type, "
                "confidence, support_count, scope, created_at, updated_at) "
                "VALUES (?, ?, ?, 'causal', ?, ?, ?, ?, ?) "
                "ON CONFLICT(chain_id) DO UPDATE SET node_ids = excluded.node_ids, "
                "confidence = excluded.confidence, "
                "support_count = excluded.support_count, updated_at = excluded.updated_at",
                (
                    generate_id(),
                    f"chain:{row['id']}",
                    json.dumps(node_ids),
                    confidence,
                    len(node_ids),
                    row["scope"],
                    now,
                    now,
                ),
            )
            stats["chains"] += 1
        await conn.commit()
        last_id = rows[-1]["id"]
        step += 1
        if step_delay > 0:
            await asyncio.sleep(step_delay)
        await ctx.checkpoint(
            step=step,
            progress=min(0.95, stats["scanned"] / total) if total else 0.0,
            state={"stats": stats},
            cursor=last_id,
        )
    return stats
