"""遗忘任务(§7.3 遗忘 / §11.5 历史保留)."""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any

from ....utils.timeutil import from_iso, now_utc, to_iso
from ...observability.logging import get_logger
from ._common import _scope_clause, drop_fragment_vectors

if TYPE_CHECKING:
    from ...storage.storage_engine import StorageEngine
    from ..checkpoint import InterruptContext

_logger = get_logger("forget")


async def forget(
    storage: StorageEngine, ctx: InterruptContext, payload: dict[str, Any]
) -> dict:
    """遗忘任务 — 淘汰或降权过期、低价值的记忆.

    保留分(规则版,§ 主动优化一期引入访问反馈):
        score = importance * 0.5 ** (age_days / half_life_days)
                      * min(3, 1 + log10(1 + access_count))
        age_days 取 max(updated_at, last_accessed_at) 距今天数 —
        被召回使用过的记忆从使用时刻重新衰减(使用即续命);
        访问计数给最多 3 倍存活加成(常用即升值)。
    判定:
    - time_end 已过(PromotionManager 将 expires_at 写入 time_end)→ 直接淘汰
    - score < threshold → 删除(默认)或降权(demote=True 时 importance=score)
    - 其余保留

    注: access_count/last_accessed_at 由 recall 命中时累加(schema 014,
    track_access 开关控制);未开启反馈时两列为默认值,公式退化为历史行为。

    删除守卫(§ 主动优化二期,对齐 DSH "importance=3 需 confirm"):
    importance >= protect_importance 的高价值行豁免一切删除(低分淘汰与过期
    清理都跳过),除非 confirm_protected=True 显式确认。

    容量治理(尾段 capacity pass): max_per_scope > 0 时,超配额 scope 按
    留存分升序修剪到配额(受保护行豁免,可超配额)。

    payload:
        scope / batch_size / step_delay: 同整合任务
        layers: 参与遗忘的层(默认 ["L1", "L2"])
        half_life_days: 衰减半衰期(默认 30 天)
        threshold: 保留分阈值(默认 0.1)
        forget_expired: 是否淘汰已过期记忆(默认 True)
        demote: True 时低分记忆降权而非删除(默认 False)
        protect_importance: 删除守卫的重要度门槛(默认 0.9)
        confirm_protected: True 时允许删除受保护行(默认 False)
        max_per_scope: 每 scope fragment 配额,0=不启用(默认 0)
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
    protect_importance = float(payload.get("protect_importance", 0.9))
    confirm_protected = bool(payload.get("confirm_protected", False))
    max_per_scope = int(payload.get("max_per_scope", 0))

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
        "protected": 0,
        "capacity_pruned": 0,
        **ctx.state.get("stats", {}),
    }
    step = ctx.step

    while True:
        cur = await conn.execute(
            "SELECT id, importance, time_end, updated_at, access_count, "
            "last_accessed_at FROM temporal_fragments "
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
            # 删除守卫: 高价值行豁免一切删除(除非显式确认)
            if (
                float(row["importance"]) >= protect_importance
                and not confirm_protected
            ):
                stats["protected"] += 1
                continue
            expired = row["time_end"] is not None and from_iso(row["time_end"]) < now
            # 使用即续命: 衰减锚点取更新时间与最近访问时间的较新者
            anchors = [from_iso(row["updated_at"])]
            if row["last_accessed_at"]:
                anchors.append(from_iso(row["last_accessed_at"]))
            age_days = max(
                0.0, (now - max(anchors)).total_seconds() / 86400
            )
            score = float(row["importance"]) * 0.5 ** (age_days / half_life_days)
            # 常用即升值: 访问计数给最多 3 倍存活加成
            score *= min(3.0, 1.0 + math.log10(1 + int(row["access_count"] or 0)))
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
            # vector_links.fragment_id 有 FK 引用热表: 先清向量再删行
            await drop_fragment_vectors(conn, delete_ids)
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
    # ---------- 容量治理 pass(§ 主动优化二期) ----------
    if max_per_scope > 0:
        await _capacity_pass(conn, ctx, stats, payload_state={
            "half_life_days": half_life_days,
            "protect_importance": protect_importance,
            "confirm_protected": confirm_protected,
            "max_per_scope": max_per_scope,
            "step": step,
        })
    return stats


async def _capacity_pass(
    conn: Any,
    ctx: InterruptContext,
    stats: dict[str, Any],
    *,
    payload_state: dict[str, Any],
) -> None:
    """超配额 scope 按留存分升序修剪到配额(受保护行豁免,可超配额).

    留存分与主衰减 pass 同公式(importance × 时间衰减 × 访问加成);
    确定性全量评分后取最低的 excess 条,逐 scope 提交 + checkpoint。
    """
    half_life_days = float(payload_state["half_life_days"])
    protect_importance = float(payload_state["protect_importance"])
    confirm_protected = bool(payload_state["confirm_protected"])
    max_per_scope = int(payload_state["max_per_scope"])
    step = int(payload_state["step"])
    now = now_utc()

    cursor = await conn.execute(
        "SELECT scope, COUNT(*) AS c FROM temporal_fragments "
        "GROUP BY scope HAVING c > ?",
        [max_per_scope],
    )
    over = [(str(r["scope"]), int(r["c"])) for r in await cursor.fetchall()]
    for scope_name, count in over:
        protect_clause = "" if confirm_protected else " AND importance < ?"
        params: list[Any] = [scope_name]
        if protect_clause:
            params.append(protect_importance)
        cur = await conn.execute(
            "SELECT id, importance, updated_at, access_count, last_accessed_at "
            f"FROM temporal_fragments WHERE scope = ?{protect_clause}",
            params,
        )
        candidates = list(await cur.fetchall())
        scored: list[tuple[float, str]] = []
        for row in candidates:
            anchors = [from_iso(row["updated_at"])]
            if row["last_accessed_at"]:
                anchors.append(from_iso(row["last_accessed_at"]))
            age_days = max(0.0, (now - max(anchors)).total_seconds() / 86400)
            score = float(row["importance"]) * 0.5 ** (age_days / half_life_days)
            score *= min(3.0, 1.0 + math.log10(1 + int(row["access_count"] or 0)))
            scored.append((score, str(row["id"])))
        scored.sort()
        excess = min(count - max_per_scope, len(scored))
        prune_ids = [fid for _, fid in scored[:excess]]
        if not prune_ids:
            continue  # 全部受保护,豁免超配额
        await drop_fragment_vectors(conn, prune_ids)
        ph = ",".join("?" for _ in prune_ids)
        await conn.execute(
            f"DELETE FROM temporal_fragments WHERE id IN ({ph})", prune_ids
        )
        await conn.commit()
        stats["capacity_pruned"] += len(prune_ids)
        _logger.warning(
            "capacity_pruned",
            scope=scope_name,
            pruned=len(prune_ids),
            cap=max_per_scope,
        )
        step += 1
        await ctx.checkpoint(
            step=step,
            state={"stats": stats},
        )
