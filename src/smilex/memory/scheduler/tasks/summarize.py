"""摘要任务(默认规则版,可注入 LLM 后端见 smilex.memory.summarizer)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ....utils.ids import generate_id
from ....utils.timeutil import now_utc, to_iso
from ...summarizer import RuleSummarizer, Summarizer
from ._common import SUMMARY_ID_SUFFIX, SUMMARY_PREFIX, _scope_clause

if TYPE_CHECKING:
    from ...storage.storage_engine import StorageEngine
    from ..checkpoint import InterruptContext


async def summarize(
    storage: StorageEngine,
    ctx: InterruptContext,
    payload: dict[str, Any],
    summarizer: Summarizer | None = None,
) -> dict:
    """摘要任务 — 为超长 fragment 生成摘要记忆(规则或 LLM).

    算法: content 长度 > max_length 的 fragment,生成一条摘要记忆:
    - fragment_id = 源 fragment_id + ":summary"(幂等:已存在则跳过)
    - content 带「【摘要】」前缀,entities/relations/importance 继承源
    - layer 与源一致(摘要是索引而非整合,层间流转归整合任务)

    summarizer: 摘要后端(None = RuleSummarizer;LLM 后端由 server 层按
    config.summarizer 注入,失败自降级规则版,任务永不因 LLM 故障失败)

    payload:
        scope / batch_size / step_delay: 同整合任务
        source_layer: 默认 "L1"
        max_length: 触发摘要的内容长度(默认 500)
        summary_length: 摘要截取长度(默认 120,仅规则后端使用)
    """
    conn = storage.conn
    scope = payload.get("scope")
    source_layer = payload.get("source_layer", "L1")
    batch_size = int(payload.get("batch_size", 50))
    max_length = int(payload.get("max_length", 500))
    summary_length = int(payload.get("summary_length", 120))
    step_delay = float(payload.get("step_delay", 0.0))
    # 注入后端优先;默认规则后端的截取长度沿用 payload(历史行为)
    summarizer = summarizer or RuleSummarizer(summary_length)

    where, params = _scope_clause(
        scope, "layer = ? AND LENGTH(content) > ?", [source_layer, max_length]
    )
    cursor = await conn.execute(
        f"SELECT COUNT(*) AS c FROM temporal_fragments WHERE {where}", params
    )
    total = int((await cursor.fetchone())["c"])

    last_id = ctx.cursor if isinstance(ctx.cursor, str) else ""
    stats = {"scanned": 0, "summarized": 0, "skipped": 0, **ctx.state.get("stats", {})}
    step = ctx.step

    while True:
        cur = await conn.execute(
            "SELECT id, fragment_id, content, entities, relations, scope, layer, "
            "importance, time_start, time_end FROM temporal_fragments "
            f"WHERE {where} AND id > ? ORDER BY id LIMIT ?",
            [*params, last_id, batch_size],
        )
        rows = await cur.fetchall()
        if not rows:
            break
        # 批内一次 IN 查询已有摘要(M9),代替逐行 SELECT 1 存在性检查
        fid_ph = ",".join("?" for _ in rows)
        exist_cur = await conn.execute(
            f"SELECT fragment_id FROM temporal_fragments WHERE fragment_id IN ({fid_ph})",
            [f"{row['fragment_id']}{SUMMARY_ID_SUFFIX}" for row in rows],
        )
        existing = {r["fragment_id"] for r in await exist_cur.fetchall()}
        insert_rows: list[tuple] = []
        for row in rows:
            stats["scanned"] += 1
            summary_fid = f"{row['fragment_id']}{SUMMARY_ID_SUFFIX}"
            if summary_fid in existing:
                stats["skipped"] += 1
                continue
            existing.add(summary_fid)  # 同批内重复 fragment_id 幂等(同原逐行语义)
            head = await summarizer.summarize(row["content"])
            now = to_iso(now_utc())
            new_id = generate_id()
            insert_rows.append(
                (
                    new_id,
                    summary_fid,
                    row["time_start"],
                    row["time_end"],
                    f"{SUMMARY_PREFIX}{head}",
                    row["entities"],
                    row["relations"],
                    row["scope"],
                    row["layer"],
                    row["importance"],
                    now,
                    now,
                )
            )
            stats["summarized"] += 1
        if insert_rows:
            await conn.executemany(
                "INSERT INTO temporal_fragments(id, fragment_id, time_start, time_end, "
                "content, entities, relations, scope, layer, importance, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                insert_rows,
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
