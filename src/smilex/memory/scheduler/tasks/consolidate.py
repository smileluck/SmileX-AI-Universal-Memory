"""整合任务 — L1 → L2(§7.3 记忆流转)."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from ....utils.ids import generate_id
from ....utils.timeutil import now_utc, to_iso
from ._common import CONSOLIDATION_PREFIX, _scope_clause

if TYPE_CHECKING:
    from ...storage.storage_engine import StorageEngine
    from ..checkpoint import InterruptContext


def _pending_to_json(pending: dict[str, dict]) -> dict[str, dict]:
    """consolidate 内存分组 → checkpoint 可 JSON 序列化的形态(集合转排序列表)."""
    return {
        k: {
            **v,
            "entities": sorted(v["entities"]),
            "relations": sorted(v["relations"]),
        }
        for k, v in pending.items()
    }


async def consolidate(
    storage: StorageEngine, ctx: InterruptContext, payload: dict[str, Any]
) -> dict:
    """整合任务 — 扫描 L1 fragments,同实体/同 scope 聚合提炼为 L2 语义记忆.

    算法(规则版,§12 整合语义):
    1. 按 id 升序分批扫描 source_layer(默认 L1)fragments
    2. 分组键 = scope + 排序后的实体集合(无实体时按 scope 聚合)
    3. 扫描完成后,成员数 >= min_group 的组生成一条 L2 记忆:
       模板拼接内容、importance 取组内最大、实体/关系取并集、
       time_start/time_end 取组内范围;随后删除组内 L1 源片段
    4. 不足 min_group 的组保留在 L1,等待后续积累

    断点续传: 每批 checkpoint(cursor=最后扫描 id, state=pending 分组 + stats);
    被抢占后 resume 从 cursor 续扫,pending 分组不丢失;flush 完成后才落
    最终检查点,源片段只在 flush 阶段删除,中途抢占不会产生半整合状态.

    pending 分组为可增量聚合的瘦身摘要(H5): 只存 id 列表 + 实体/关系集合
    + max importance + 时间范围,不存 content(原文在 flush 阶段按 id 回查),
    避免 checkpoint state 随扫描量 O(n²) 膨胀。

    payload:
        scope: 只整合该 scope(None = 全部)
        source_layer / target_layer: 默认 "L1" → "L2"
        batch_size: 每批扫描条数(默认 100)
        min_group: 成组最小条数(默认 2)
        content_head: 拼接时每条源内容截取长度(默认 50)
        step_delay: 每批处理后的 sleep 秒数(测试/演示限速用,默认 0)
    """
    conn = storage.conn
    scope = payload.get("scope")
    source_layer = payload.get("source_layer", "L1")
    target_layer = payload.get("target_layer", "L2")
    batch_size = int(payload.get("batch_size", 100))
    min_group = int(payload.get("min_group", 2))
    content_head = int(payload.get("content_head", 50))
    step_delay = float(payload.get("step_delay", 0.0))

    where, params = _scope_clause(scope, "layer = ?", [source_layer])
    cursor = await conn.execute(
        f"SELECT COUNT(*) AS c FROM temporal_fragments WHERE {where}", params
    )
    total = int((await cursor.fetchone())["c"])

    # 断点恢复: cursor / pending / stats 全部来自上一检查点
    last_id = ctx.cursor if isinstance(ctx.cursor, str) else ""
    # pending 内存形态: key → {"ids": [...], "entities": set, "relations": set,
    # "importance": max, "time_start": min, "time_end": max|None}(H5 瘦身,
    # checkpoint 落库时集合转排序列表)
    pending: dict[str, dict] = {}
    for k, v in ctx.state.get("pending", {}).items():
        pending[k] = {
            **v,
            "entities": set(v["entities"]),
            "relations": set(v["relations"]),
        }
    stats = {
        "scanned": 0,
        "groups": 0,
        "consolidated": 0,
        "created": 0,
        **ctx.state.get("stats", {}),
    }
    step = ctx.step

    # ---- 阶段 1: 分批扫描 + 内存分组 ----
    while True:
        cur = await conn.execute(
            "SELECT id, fragment_id, content, entities, relations, scope, "
            "importance, time_start, time_end FROM temporal_fragments "
            f"WHERE {where} AND id > ? ORDER BY id LIMIT ?",
            [*params, last_id, batch_size],
        )
        rows = await cur.fetchall()
        if not rows:
            break
        for row in rows:
            entities = sorted(json.loads(row["entities"]))
            key = f"{row['scope']}|{','.join(entities)}"
            g = pending.get(key)
            if g is None:
                g = pending[key] = {
                    "ids": [],
                    "entities": set(),
                    "relations": set(),
                    "importance": 0.0,
                    "time_start": row["time_start"],
                    "time_end": None,
                }
            g["ids"].append(row["id"])
            g["entities"].update(entities)
            g["relations"].update(json.loads(row["relations"]))
            g["importance"] = max(g["importance"], float(row["importance"]))
            if row["time_start"] < g["time_start"]:
                g["time_start"] = row["time_start"]
            if row["time_end"] and (
                g["time_end"] is None or row["time_end"] > g["time_end"]
            ):
                g["time_end"] = row["time_end"]
            stats["scanned"] += 1
        last_id = rows[-1]["id"]
        step += 1
        if step_delay > 0:
            await asyncio.sleep(step_delay)
        await ctx.checkpoint(
            step=step,
            progress=min(0.9, stats["scanned"] / total) if total else 0.0,
            state={"pending": _pending_to_json(pending), "stats": stats},
            cursor=last_id,
        )

    # ---- 阶段 2: flush — 成组的生成 L2 并删除 L1 源 ----
    for key in sorted(pending):
        g = pending[key]
        if len(g["ids"]) < min_group:
            continue
        scope_str, _, _ = key.partition("|")
        await _flush_consolidation_group(
            conn, scope_str, g, target_layer, content_head, stats
        )
    step += 1
    await ctx.checkpoint(
        step=step,
        progress=1.0,
        state={"pending": {}, "stats": stats},
        cursor=last_id,
    )
    return stats


async def _flush_consolidation_group(
    conn,
    scope_str: str,
    g: dict,
    target_layer: str,
    content_head: int,
    stats: dict,
) -> None:
    """把一个分组的 L1 片段聚合成一条 L2 记忆,并删除源片段.

    g 为瘦身摘要(H5);content 在此按 id 回查(ORDER BY id 保持扫描序).
    """
    ids: list[str] = g["ids"]
    placeholders = ",".join("?" for _ in ids)
    cur = await conn.execute(
        f"SELECT content FROM temporal_fragments WHERE id IN ({placeholders}) "
        "ORDER BY id",
        ids,
    )
    contents = [r["content"] for r in await cur.fetchall()]

    entities = sorted(g["entities"])
    relations = sorted(g["relations"])
    label = ",".join(entities) if entities else scope_str
    body = "; ".join(c[:content_head] for c in contents)
    content = f"{CONSOLIDATION_PREFIX}{label}:{len(ids)} 条记忆聚合 — {body}"
    importance = g["importance"]
    time_start = g["time_start"]
    time_end = g["time_end"]

    now = to_iso(now_utc())
    new_id = generate_id()
    await conn.execute(
        "INSERT INTO temporal_fragments(id, fragment_id, time_start, time_end, "
        "content, entities, relations, scope, layer, importance, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id,
            new_id,
            time_start,
            time_end,
            content,
            json.dumps(entities, ensure_ascii=False),
            json.dumps(relations, ensure_ascii=False),
            scope_str,
            target_layer,
            importance,
            now,
            now,
        ),
    )
    await conn.execute(
        f"DELETE FROM temporal_fragments WHERE id IN ({placeholders})",
        ids,
    )
    await conn.commit()
    stats["groups"] += 1
    stats["created"] += 1
    stats["consolidated"] += len(ids)
