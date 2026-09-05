"""遗忘任务(§7.3 遗忘 / §11.5 历史保留)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ....utils.timeutil import from_iso, now_utc, to_iso
from ._common import _scope_clause

if TYPE_CHECKING:
    from ...storage.storage_engine import StorageEngine
    from ..checkpoint import InterruptContext


async def forget(
    storage: StorageEngine, ctx: InterruptContext, payload: dict[str, Any]
) -> dict:
    """遗忘任务 — 淘汰或降权过期、低价值的记忆.

    保留分(规则版,importance + 时间衰减):
        score = importance * 0.5 ** (age_days / half_life_days)
        age_days 取 updated_at 距今天数
    判定:
    - time_end 已过(PromotionManager 将 expires_at 写入 time_end)→ 直接淘汰
    - score < threshold → 删除(默认)或降权(demote=True 时 importance=score)
    - 其余保留

    注: temporal_fragments 无 access_count 列(Layer 0 access_count 不落库),
    访问热度暂由 importance 承载,不改 schema.

    payload:
        scope / batch_size / step_delay: 同整合任务
        layers: 参与遗忘的层(默认 ["L1", "L2"])
        half_life_days: 衰减半衰期(默认 30 天)
        threshold: 保留分阈值(默认 0.1)
        forget_expired: 是否淘汰已过期记忆(默认 True)
        demote: True 时低分记忆降权而非删除(默认 False)
    """
    conn = storage.conn
    scope = payload.get("scope")
    layers = list(payload.get("layers", ["L1", "L2"]))
    batch_size = int(payload.get("batch_size", 100))
    half_life_days = float(payload.get("half_life_days", 30.0))
    threshold = float(payload.get("threshold", 0.1))
    forget_expired = bool(payload.get("forget_expired", True))
    demote = bool(payload.get("demote", False))
    step_delay = float(payload.get("step_delay", 0.0))

    placeholders = ",".join("?" for _ in layers)
    where, params = _scope_clause(scope, f"layer IN ({placeholders})", layers)
    cursor = await conn.execute(
        f"SELECT COUNT(*) AS c FROM temporal_fragments WHERE {where}", params
    )
    total = int((await cursor.fetchone())["c"])

    now = now_utc()
    last_id = ctx.cursor if isinstance(ctx.cursor, str) else ""
    stats = {
        "scanned": 0,
        "kept": 0,
        "forgotten": 0,
        "expired": 0,
        "demoted": 0,
        **ctx.state.get("stats", {}),
    }
    step = ctx.step

    while True:
        cur = await conn.execute(
            "SELECT id, importance, time_end, updated_at FROM temporal_fragments "
            f"WHERE {where} AND id > ? ORDER BY id LIMIT ?",
            [*params, last_id, batch_size],
        )
        rows = await cur.fetchall()
        if not rows:
            break
        # 批内收集后一次性写库(M9): 代替逐行 DELETE/UPDATE
        delete_ids: list[str] = []
        demote_rows: list[tuple[float, str, str]] = []
        for row in rows:
            stats["scanned"] += 1
            expired = row["time_end"] is not None and from_iso(row["time_end"]) < now
            age_days = max(0.0, (now - from_iso(row["updated_at"])).total_seconds() / 86400)
            score = float(row["importance"]) * 0.5 ** (age_days / half_life_days)
            if expired and forget_expired:
                delete_ids.append(row["id"])
                stats["expired"] += 1
            elif score < threshold:
                if demote:
                    demote_rows.append((score, to_iso(now), row["id"]))
                    stats["demoted"] += 1
                else:
                    delete_ids.append(row["id"])
                    stats["forgotten"] += 1
            else:
                stats["kept"] += 1
        if delete_ids:
            id_ph = ",".join("?" for _ in delete_ids)
            await conn.execute(
                f"DELETE FROM temporal_fragments WHERE id IN ({id_ph})",
                delete_ids,
            )
        if demote_rows:
            await conn.executemany(
                "UPDATE temporal_fragments SET importance = ?, updated_at = ? "
                "WHERE id = ?",
                demote_rows,
            )
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
