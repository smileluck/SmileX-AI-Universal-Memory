"""5 类核心任务 — Layer 3c(主文档 §8.2 / §14.3 P0 范围;analyse memory-task-scheduler §2).

整合(consolidate)/ 遗忘(forget)/ 摘要(summarize)/ 因果(causal)/ 语义(semantic),
全部规则版(无 LLM):HashEmbedder 无语义相似度,聚合/分组一律用确定性规则
(同实体 / 同 scope / 关键词),保证嵌入式环境零依赖可跑.

共同约定(§9 抢占协议):
- interruptible=True,批处理循环中定期 `await ctx.checkpoint(...)`
- cursor = 最后处理的行 id(ULID 字符串有序,id > cursor 续扫)
- state 携带跨断点的累积状态(如整合的 pending 分组 / 统计计数)
- payload 均支持: scope(全路径,如 "project:proj_a")/ batch_size / step_delay(测试限速)

事务约定:直接用 StorageEngine.conn(同 CheckpointStore),每批写完 commit 一次;
schema 未改(复用 temporal_fragments / triples / causal_chains / checkpoints).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...utils.ids import generate_id
from ...utils.timeutil import from_iso, now_utc, to_iso
from .models import TaskDefinition, TaskPriority
from .triggers import AdaptiveRule

if TYPE_CHECKING:
    from ..storage.storage_engine import StorageEngine
    from .checkpoint import InterruptContext
    from .scheduler import MemoryTaskScheduler

# ---------- 任务名(注册键) ----------
TASK_CONSOLIDATE = "consolidate"
TASK_FORGET = "forget"
TASK_SUMMARIZE = "summarize"
TASK_CAUSAL = "causal"
TASK_SEMANTIC = "semantic"
CORE_TASK_NAMES = (
    TASK_CONSOLIDATE,
    TASK_FORGET,
    TASK_SUMMARIZE,
    TASK_CAUSAL,
    TASK_SEMANTIC,
)

# 摘要/整合/语义产物的内容前缀与 fragment_id 约定(幂等判定用)
SUMMARY_PREFIX = "【摘要】"
SUMMARY_ID_SUFFIX = ":summary"
CONSOLIDATION_PREFIX = "【整合】"
SEMANTIC_COMMUNITY_PREFIX = "【语义社区】"
SEMANTIC_COMMUNITY_ID = "semantic:community:"


def _scope_clause(scope: str | None, where: str, params: list[Any]) -> tuple[str, list[Any]]:
    """可选 scope 过滤拼接到 WHERE 子句."""
    if scope:
        where += " AND scope = ?"
        params.append(scope)
    return where, params


def _rule_summary(content: str, summary_length: int) -> str:
    """规则版摘要:截取前 summary_length 字,优先在句末标点处截断."""
    if len(content) <= summary_length:
        return content
    head = content[:summary_length]
    cut = max(head.rfind(p) for p in ("。", "!", "?", "!", "?"))
    if cut > 0:
        head = head[: cut + 1]
    return f"{head}…(原文共 {len(content)} 字)"


class CoreTaskRunner:
    """5 类核心任务的执行函数集合(持有 StorageEngine,方法即 run 函数).

    每个方法签名符合 InterruptibleRun: `async (ctx, payload) -> dict(统计)`.
    """

    def __init__(self, storage: StorageEngine) -> None:
        self._storage = storage

    # ==================== 1. 整合任务:L1 → L2(§7.3 记忆流转) ====================

    async def consolidate(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
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

        payload:
            scope: 只整合该 scope(None = 全部)
            source_layer / target_layer: 默认 "L1" → "L2"
            batch_size: 每批扫描条数(默认 100)
            min_group: 成组最小条数(默认 2)
            content_head: 拼接时每条源内容截取长度(默认 50)
            step_delay: 每批处理后的 sleep 秒数(测试/演示限速用,默认 0)
        """
        conn = self._storage.conn
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
        pending: dict[str, list[dict]] = {
            k: list(v) for k, v in ctx.state.get("pending", {}).items()
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
                pending.setdefault(key, []).append(
                    {
                        "id": row["id"],
                        "content": row["content"],
                        "entities": entities,
                        "relations": json.loads(row["relations"]),
                        "importance": float(row["importance"]),
                        "time_start": row["time_start"],
                        "time_end": row["time_end"],
                    }
                )
                stats["scanned"] += 1
            last_id = rows[-1]["id"]
            step += 1
            if step_delay > 0:
                await asyncio.sleep(step_delay)
            await ctx.checkpoint(
                step=step,
                progress=min(0.9, stats["scanned"] / total) if total else 0.0,
                state={"pending": pending, "stats": stats},
                cursor=last_id,
            )

        # ---- 阶段 2: flush — 成组的生成 L2 并删除 L1 源 ----
        for key in sorted(pending):
            frags = pending[key]
            if len(frags) < min_group:
                continue
            scope_str, _, _ = key.partition("|")
            await self._flush_consolidation_group(
                conn, scope_str, frags, target_layer, content_head, stats
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
        self,
        conn,
        scope_str: str,
        frags: list[dict],
        target_layer: str,
        content_head: int,
        stats: dict,
    ) -> None:
        """把一个分组的 L1 片段聚合成一条 L2 记忆,并删除源片段."""
        entities = sorted({e for f in frags for e in f["entities"]})
        relations = sorted({r for f in frags for r in f["relations"]})
        label = ",".join(entities) if entities else scope_str
        body = "; ".join(f["content"][:content_head] for f in frags)
        content = f"{CONSOLIDATION_PREFIX}{label}:{len(frags)} 条记忆聚合 — {body}"
        importance = max(f["importance"] for f in frags)
        time_start = min(f["time_start"] for f in frags)
        ends = [f["time_end"] for f in frags if f["time_end"]]
        time_end = max(ends) if ends else None

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
        placeholders = ",".join("?" for _ in frags)
        await conn.execute(
            f"DELETE FROM temporal_fragments WHERE id IN ({placeholders})",
            [f["id"] for f in frags],
        )
        await conn.commit()
        stats["groups"] += 1
        stats["created"] += 1
        stats["consolidated"] += len(frags)

    # ==================== 2. 遗忘任务(§7.3 遗忘 / §11.5 历史保留) ====================

    async def forget(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
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
        conn = self._storage.conn
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
            for row in rows:
                stats["scanned"] += 1
                expired = row["time_end"] is not None and from_iso(row["time_end"]) < now
                age_days = max(0.0, (now - from_iso(row["updated_at"])).total_seconds() / 86400)
                score = float(row["importance"]) * 0.5 ** (age_days / half_life_days)
                if expired and forget_expired:
                    await conn.execute("DELETE FROM temporal_fragments WHERE id = ?", [row["id"]])
                    stats["expired"] += 1
                elif score < threshold:
                    if demote:
                        await conn.execute(
                            "UPDATE temporal_fragments SET importance = ?, updated_at = ? "
                            "WHERE id = ?",
                            [score, to_iso(now), row["id"]],
                        )
                        stats["demoted"] += 1
                    else:
                        await conn.execute(
                            "DELETE FROM temporal_fragments WHERE id = ?", [row["id"]]
                        )
                        stats["forgotten"] += 1
                else:
                    stats["kept"] += 1
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

    # ==================== 3. 摘要任务(规则版,无 LLM) ====================

    async def summarize(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
        """摘要任务 — 为超长 fragment 生成规则版摘要记忆.

        算法: content 长度 > max_length 的 fragment,截取前 summary_length 字
        (优先句末标点截断)生成一条摘要记忆:
        - fragment_id = 源 fragment_id + ":summary"(幂等:已存在则跳过)
        - content 带「【摘要】」前缀,entities/relations/importance 继承源
        - layer 与源一致(摘要是索引而非整合,层间流转归整合任务)

        payload:
            scope / batch_size / step_delay: 同整合任务
            source_layer: 默认 "L1"
            max_length: 触发摘要的内容长度(默认 500)
            summary_length: 摘要截取长度(默认 120)
        """
        conn = self._storage.conn
        scope = payload.get("scope")
        source_layer = payload.get("source_layer", "L1")
        batch_size = int(payload.get("batch_size", 50))
        max_length = int(payload.get("max_length", 500))
        summary_length = int(payload.get("summary_length", 120))
        step_delay = float(payload.get("step_delay", 0.0))

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
            for row in rows:
                stats["scanned"] += 1
                summary_fid = f"{row['fragment_id']}{SUMMARY_ID_SUFFIX}"
                exists = await conn.execute(
                    "SELECT 1 FROM temporal_fragments WHERE fragment_id = ? LIMIT 1",
                    [summary_fid],
                )
                if await exists.fetchone() is not None:
                    stats["skipped"] += 1
                    continue
                head = _rule_summary(row["content"], summary_length)
                now = to_iso(now_utc())
                new_id = generate_id()
                await conn.execute(
                    "INSERT INTO temporal_fragments(id, fragment_id, time_start, time_end, "
                    "content, entities, relations, scope, layer, importance, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    ),
                )
                stats["summarized"] += 1
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

    # ==================== 4. 因果任务(causal_chains 维护) ====================

    async def causal(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
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
        conn = self._storage.conn
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
                chain = await self._storage.trace_causal_chain(row["id"], direction="backward")
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

    # ==================== 5. 语义任务(NetworkX 图维护) ====================

    async def semantic(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
        """语义任务 — NetworkX 重建实体关系图,预计算连通分量(语义社区)缓存.

        算法(规则版):
        1. 加载实体 + 当前有效(valid_to IS NULL)且指向实体的 triples,建无向图
        2. nx.connected_components 求连通分量;>= min_component 的为语义社区
        3. 社区预计算结果以 L3 fragment 缓存(fragment_id 前缀
           "semantic:community:",key 为成员 id 排序后的 md5,确定性):
           先按前缀整批删除旧缓存再重写,重跑幂等不膨胀
        4. 内容含社区规模与按 degree 排序的核心实体名

        payload:
            scope: 只统计该 scope,缓存 fragment 也落在该 scope(默认 None → global)
            min_component: 成社区最小实体数(默认 2)
            top_k: 内容中列出的核心实体数(默认 5)
        """
        import networkx as nx

        conn = self._storage.conn
        scope = payload.get("scope")
        min_component = int(payload.get("min_component", 2))
        top_k = int(payload.get("top_k", 5))
        cache_scope = scope or "global"

        # ---- 阶段 1: 建图 ----
        ent_where, ent_params = _scope_clause(scope, "1 = 1", [])
        cur = await conn.execute(f"SELECT id, name FROM entities WHERE {ent_where}", ent_params)
        names = {r["id"]: r["name"] for r in await cur.fetchall()}

        edge_where, edge_params = _scope_clause(
            scope, "object_id IS NOT NULL AND valid_to IS NULL", []
        )
        cur = await conn.execute(
            f"SELECT subject_id, object_id FROM triples WHERE {edge_where}", edge_params
        )
        edges = [(r["subject_id"], r["object_id"]) for r in await cur.fetchall()]

        graph = nx.Graph()
        graph.add_nodes_from(names)
        graph.add_edges_from(edges)
        components = [c for c in nx.connected_components(graph) if len(c) >= min_component]
        components.sort(key=lambda c: sorted(c))  # 确定性顺序
        stats = {
            "entities": len(names),
            "edges": len(edges),
            "communities": len(components),
            **ctx.state.get("stats", {}),
        }
        step = ctx.step + 1
        await ctx.checkpoint(step=step, progress=0.3, state={"stats": stats}, cursor=None)

        # ---- 阶段 2: 整批刷新 L3 缓存(先删后写,幂等) ----
        del_where, del_params = _scope_clause(
            scope,
            "layer = 'L3' AND fragment_id LIKE ?",
            [f"{SEMANTIC_COMMUNITY_ID}%"],
        )
        await conn.execute(f"DELETE FROM temporal_fragments WHERE {del_where}", del_params)

        degree = dict(graph.degree())
        for i, comp in enumerate(components):
            ids = sorted(comp)
            key = hashlib.md5(",".join(ids).encode()).hexdigest()[:12]  # 确定性分组键(非安全用途)
            top = sorted(comp, key=lambda e: (-degree.get(e, 0), e))[:top_k]
            top_names = [names.get(e, e) for e in top]
            n_edges = graph.subgraph(comp).number_of_edges()
            content = (
                f"{SEMANTIC_COMMUNITY_PREFIX}{len(comp)} 个实体 / {n_edges} 条关系;"
                f"核心实体: {', '.join(top_names)}"
            )
            now = to_iso(now_utc())
            new_id = generate_id()
            await conn.execute(
                "INSERT INTO temporal_fragments(id, fragment_id, time_start, content, "
                "entities, scope, layer, importance, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'L3', 0.5, ?, ?)",
                (
                    new_id,
                    f"{SEMANTIC_COMMUNITY_ID}{key}",
                    now,
                    content,
                    json.dumps(ids, ensure_ascii=False),
                    cache_scope,
                    now,
                    now,
                ),
            )
            await conn.commit()
            step += 1
            await ctx.checkpoint(
                step=step,
                progress=0.3 + 0.7 * (i + 1) / len(components),
                state={"stats": stats},
                cursor=None,
            )
        return stats


@dataclass
class CoreTaskConfig:
    """5 类核心任务的默认装配参数(§8.1 三重触发 / §8.3 自适应冷却).

    嵌入式环境无法在同步条件函数里查库,自适应规则的条件由宿主应用以
    可调用对象注入(如内存压力表);None 表示不注册该条规则.
    """

    enable_time_triggers: bool = True
    enable_event_mappings: bool = True
    enable_adaptive_rules: bool = True

    consolidate_interval_seconds: float = 3600.0  # 每小时整合(§8.1 每日 02:00 的轻量替代)
    forget_interval_seconds: float = 86400.0  # 每日遗忘
    summarize_interval_seconds: float = 1800.0  # 每 30 分钟摘要
    semantic_interval_seconds: float = 7200.0  # 每 2 小时图维护

    l1_pressure: Any = None  # Callable[[], bool]: L1 使用率 > 80%(§8.3 记忆压力)
    memory_pressure_cooldown: float = 300.0  # 5 分钟
    potential_causal_links: Any = None  # Callable[[], bool]: 存在潜在因果链接
    causal_cooldown: float = 300.0  # 5 分钟


def register_core_tasks(
    scheduler: MemoryTaskScheduler,
    storage: StorageEngine,
    *,
    config: CoreTaskConfig | None = None,
) -> CoreTaskRunner:
    """把 5 类核心任务注册到调度器,并按 §8.1/§8.3 装配默认触发器.

    优先级(§8.2): 整合/因果 HIGH,摘要/语义 MEDIUM,遗忘 LOW;全部可中断.
    触发器默认:
    - 时间: 整合 1h / 摘要 30min / 语义 2h / 遗忘 1d(均不立即触发)
    - 事件: memory_full→整合 / episode_end→摘要 / session_end→遗忘 / causal_inference→因果
    - 自适应: 记忆压力→整合 / 潜在因果→因果(条件由 config 注入,带冷却)

    Returns:
        CoreTaskRunner(便于测试直接调用执行函数或自定义注册)
    """
    config = config or CoreTaskConfig()
    runner = CoreTaskRunner(storage)

    scheduler.register(
        TaskDefinition(
            name=TASK_CONSOLIDATE,
            run=runner.consolidate,
            priority=TaskPriority.HIGH,
            interruptible=True,
            description="L1→L2 记忆整合(规则版聚合,§7.3)",
        )
    )
    scheduler.register(
        TaskDefinition(
            name=TASK_FORGET,
            run=runner.forget,
            priority=TaskPriority.LOW,
            interruptible=True,
            description="遗忘:淘汰/降权过期低值记忆(importance × 时间衰减)",
        )
    )
    scheduler.register(
        TaskDefinition(
            name=TASK_SUMMARIZE,
            run=runner.summarize,
            priority=TaskPriority.MEDIUM,
            interruptible=True,
            description="长 fragment 规则版摘要(截取/模板,无 LLM)",
        )
    )
    scheduler.register(
        TaskDefinition(
            name=TASK_CAUSAL,
            run=runner.causal,
            priority=TaskPriority.HIGH,
            interruptible=True,
            description="因果链维护(predecessor_id → causal_chains)",
        )
    )
    scheduler.register(
        TaskDefinition(
            name=TASK_SEMANTIC,
            run=runner.semantic,
            priority=TaskPriority.MEDIUM,
            interruptible=True,
            description="语义图维护(NetworkX 连通分量 → L3 缓存)",
        )
    )

    if config.enable_time_triggers:
        scheduler.add_time_trigger(
            TASK_CONSOLIDATE,
            config.consolidate_interval_seconds,
            priority=TaskPriority.HIGH,
        )
        scheduler.add_time_trigger(
            TASK_SUMMARIZE,
            config.summarize_interval_seconds,
            priority=TaskPriority.MEDIUM,
        )
        scheduler.add_time_trigger(
            TASK_SEMANTIC,
            config.semantic_interval_seconds,
            priority=TaskPriority.LOW,
        )
        scheduler.add_time_trigger(
            TASK_FORGET,
            config.forget_interval_seconds,
            priority=TaskPriority.LOW,
        )

    if config.enable_event_mappings:
        scheduler.add_event_mapping("memory_full", TASK_CONSOLIDATE, TaskPriority.HIGH)
        scheduler.add_event_mapping("episode_end", TASK_SUMMARIZE, TaskPriority.MEDIUM)
        scheduler.add_event_mapping("session_end", TASK_FORGET, TaskPriority.LOW)
        scheduler.add_event_mapping("causal_inference", TASK_CAUSAL, TaskPriority.HIGH)

    if config.enable_adaptive_rules:
        if config.l1_pressure is not None:
            scheduler.add_adaptive_rule(
                AdaptiveRule(
                    name="memory_pressure",
                    task_name=TASK_CONSOLIDATE,
                    condition=config.l1_pressure,
                    cooldown_seconds=config.memory_pressure_cooldown,
                    priority=TaskPriority.HIGH,
                )
            )
        if config.potential_causal_links is not None:
            scheduler.add_adaptive_rule(
                AdaptiveRule(
                    name="causal_potential",
                    task_name=TASK_CAUSAL,
                    condition=config.potential_causal_links,
                    cooldown_seconds=config.causal_cooldown,
                    priority=TaskPriority.HIGH,
                )
            )

    return runner


__all__ = [
    "CONSOLIDATION_PREFIX",
    "CORE_TASK_NAMES",
    "CoreTaskConfig",
    "CoreTaskRunner",
    "SEMANTIC_COMMUNITY_ID",
    "SEMANTIC_COMMUNITY_PREFIX",
    "SUMMARY_ID_SUFFIX",
    "SUMMARY_PREFIX",
    "TASK_CAUSAL",
    "TASK_CONSOLIDATE",
    "TASK_FORGET",
    "TASK_SEMANTIC",
    "TASK_SUMMARIZE",
    "register_core_tasks",
]
