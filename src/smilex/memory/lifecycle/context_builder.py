"""ContextBuilder — Token Budget 上下文构建(主文档 §12.1 / §7.5,L2.7).

Per 主文档 §12.1 + 模块文档 03 §3.1:
- 多路召回聚合: L0 工作记忆(全量)+ L1 双通道(向量 KNN + FTS5 BM25,RRF 融合)
  + L2 混合检索(RRF)
- 去重: 同一 memory 来自多路召回只保留一份(优先级 L0 > L1 > L2)
- 排序: L0 按 importance/recency,L1 按 RRF 融合分,L2 按 RRF 相关度
- 贪心裁剪: 按 L0 > L1 > L2 优先级逐条填充,预算 = token_budget × 0.7(安全边际)
- 输出带来源标记([L0]/[L1]/[L2] 前缀),便于上层追溯

L3 语义层不在 MVP 范围(模块文档 03 §5 演进路径),此处不实现.

使用方法:
    builder = ContextBuilder(l0=l0, vector_store=store)
    ctx = await builder.build_context(
        engine.conn, session_id="sess_1", query_text="alice 的工作",
        entity_id="e1", token_budget=4000,
    )
    prompt = ctx.text  # 带来源标记的上下文字符串
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiosqlite

from ..storage.queries import HybridQuery, hybrid_memory_search
from ..storage.queries.fts import bm25_fragment_search
from ..storage.queries.hybrid import rrf_fusion
from .token_counter import TokenCounter

if TYPE_CHECKING:
    from ..models import ScopeFilter
    from ..storage.vector_store import VectorStore
    from .l0_working_memory import L0WorkingMemory

# 主文档 §7.4: 总预算 4000 tokens
DEFAULT_TOKEN_BUDGET = 4000
# 模块文档 03 §3.1: 实际预算 = 总预算 × 0.7(安全边际)
SAFETY_MARGIN = 0.7

# 各层召回的默认 Top-K(§12.1: L1 top_k=5, L2 top_k=10)
DEFAULT_L1_TOP_K = 5
DEFAULT_L2_TOP_K = 10

# L1 双通道(向量 + BM25)RRF 融合常数(与 L2 hybrid 一致取 60)
RRF_K = 60

# 层优先级(数值越小越优先填充)
_LAYER_PRIORITY = {"L0": 0, "L1": 1, "L2": 2}


@dataclass
class ContextSource:
    """一条进入上下文的记忆(带来源标记).

    Attributes:
        memory_id: 记忆 ID(FuzzyMemory.id / vector_links 业务 ID / triples.id)
        layer: 来源层 — "L0"(工作记忆)/ "L1"(向量 KNN)/ "L2"(混合检索)
        content: 渲染后的文本内容
        tokens: content 渲染行(``[层] content``)的 token 数;收集阶段不计算,
            仅入选条目由 _greedy_fill 回填(未入选或未填充时为 0)
        score: 层内排序分(L0: importance;L1: 1/(1+distance);L2: RRF score)
    """

    memory_id: str
    layer: str
    content: str
    tokens: int = 0
    score: float = 0.0


@dataclass
class BuiltContext:
    """build_context 输出.

    Attributes:
        text: 拼接好的上下文字符串(每行 ``[层] content``,含来源标记)
        sources: 实际入选的 ContextSource 列表(按填充顺序)
        total_tokens: text 的总 token 数
        budget: 实际生效预算(token_budget × SAFETY_MARGIN)
        truncated: 是否有候选因预算不足被裁剪
    """

    text: str
    sources: list[ContextSource] = field(default_factory=list)
    total_tokens: int = 0
    budget: int = 0
    truncated: bool = False


async def _load_content_map(
    conn: aiosqlite.Connection,
    memory_ids: list[str],
) -> dict[str, str]:
    """批量解析 memory_id → 文本内容.

    memory_id 可能指向 temporal_fragments / entities / triples 之一
    (vector_links 的三种业务关联 + hybrid 检索的 triples.id).
    """
    if not memory_ids:
        return {}
    contents: dict[str, str] = {}
    placeholders = ",".join("?" for _ in memory_ids)

    cursor = await conn.execute(
        f"SELECT id, content FROM temporal_fragments WHERE id IN ({placeholders})",
        memory_ids,
    )
    for row in await cursor.fetchall():
        contents[row["id"]] = row["content"]

    cursor = await conn.execute(
        f"SELECT id, name, entity_type FROM entities WHERE id IN ({placeholders})",
        memory_ids,
    )
    for row in await cursor.fetchall():
        contents[row["id"]] = f"{row['name']} ({row['entity_type']})"

    cursor = await conn.execute(
        f"SELECT id, subject_id, predicate, object_id, object_value "
        f"FROM triples WHERE id IN ({placeholders})",
        memory_ids,
    )
    for row in await cursor.fetchall():
        obj = row["object_id"] or row["object_value"] or "?"
        contents[row["id"]] = f"{row['subject_id']} {row['predicate']} {obj}"

    return contents


class ContextBuilder:
    """上下文构建器 — 聚合 L0/L1/L2 召回,按 Token Budget 贪心裁剪(§12.1).

    Args:
        l0: L0 工作记忆(None 时跳过 L0 层)
        vector_store: L1 向量存储(None 时跳过 L1 层)
        token_counter: token 计数器(None 时新建默认实例)
        safety_margin: 安全边际(默认 0.7,模块文档 03 §3.1)
    """

    def __init__(
        self,
        *,
        l0: L0WorkingMemory | None = None,
        vector_store: VectorStore | None = None,
        token_counter: TokenCounter | None = None,
        safety_margin: float = SAFETY_MARGIN,
    ) -> None:
        self._l0 = l0
        self._vector_store = vector_store
        self._counter = token_counter or TokenCounter()
        self._safety_margin = safety_margin

    async def build_context(
        self,
        conn: aiosqlite.Connection,
        *,
        session_id: str | None = None,
        query_text: str | None = None,
        entity_id: str | None = None,
        time_range: tuple[str, str] | None = None,
        causal_triple_id: str | None = None,
        scope_filter: ScopeFilter | None = None,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        l1_top_k: int = DEFAULT_L1_TOP_K,
        l2_top_k: int = DEFAULT_L2_TOP_K,
    ) -> BuiltContext:
        """构建带来源标记的上下文,裁剪到 token_budget × 安全边际内.

        Args:
            conn: aiosqlite 连接(L1/L2 检索用)
            session_id: L0 工作记忆会话 ID(None 跳过 L0)
            query_text: L1 向量 KNN 查询文本(None 跳过 L1)
            entity_id / time_range / causal_triple_id: L2 混合检索参数
                (HybridQuery;全空跳过 L2)
            scope_filter: 作用域过滤(作用于 L1/L2)
            token_budget: 总 token 预算(默认 4000,§7.4)
            l1_top_k / l2_top_k: L1/L2 召回条数上限

        Returns:
            BuiltContext(text 带来源标记,sources 记录入选明细)
        """
        budget = int(token_budget * self._safety_margin)
        candidates = await self._collect(
            conn,
            session_id=session_id,
            query_text=query_text,
            entity_id=entity_id,
            time_range=time_range,
            causal_triple_id=causal_triple_id,
            scope_filter=scope_filter,
            l1_top_k=l1_top_k,
            l2_top_k=l2_top_k,
        )
        return self._greedy_fill(candidates, budget)

    # ==================== 召回聚合 ====================

    async def _collect(
        self,
        conn: aiosqlite.Connection,
        *,
        session_id: str | None,
        query_text: str | None,
        entity_id: str | None,
        time_range: tuple[str, str] | None,
        causal_triple_id: str | None,
        scope_filter: ScopeFilter | None,
        l1_top_k: int,
        l2_top_k: int,
    ) -> list[ContextSource]:
        """多路召回 + 去重,按层优先级排序的候选列表."""
        seen: set[str] = set()
        candidates: list[ContextSource] = []

        # L0: 工作记忆全量(最高优先级,§12.1)
        if session_id is not None and self._l0 is not None:
            for src in self._collect_l0(session_id):
                if src.memory_id in seen:
                    continue
                seen.add(src.memory_id)
                candidates.append(src)

        # L1: 双通道 — 向量 KNN + FTS5 BM25 关键词,RRF 融合(schema 013)
        if query_text is not None and self._vector_store is not None:
            hits = await self._vector_store.knn_search(
                conn, query_text, k=l1_top_k, scope_filter=scope_filter
            )
            # 关键词通道: 精确词命中(向量近似检索不到的,如专有名词/日期)
            fts_ids: list[str] = []
            try:
                fts_ids = await bm25_fragment_search(
                    conn, query_text, scope_filter=scope_filter, top_k=l1_top_k * 2
                )
            except aiosqlite.OperationalError:
                fts_ids = []  # fts_fragments 不存在(老库未迁移)时降级纯向量
            rankings = [
                [h.memory_id for h in hits],
                fts_ids,
            ]
            fused = rrf_fusion(rankings, k=RRF_K)
            content_map = await _load_content_map(conn, [i for i, _ in fused])
            # RRF score 归一化到 [0,1](除以双通道理论最大值 2/(k+1)),
            # 保持 ContextSource.score 契约
            max_rrf = sum(1.0 / (RRF_K + 1) for _ in rankings if _)
            for memory_id, rrf_score in fused:
                if memory_id in seen or memory_id not in content_map:
                    continue
                seen.add(memory_id)
                candidates.append(
                    ContextSource(
                        memory_id=memory_id,
                        layer="L1",
                        content=content_map[memory_id],
                        score=rrf_score / max_rrf if max_rrf > 0 else 0.0,
                    )
                )

        # L2: 混合检索 RRF(§12.1 hybrid_memory_search top_k=10)
        if entity_id is not None or time_range is not None or causal_triple_id is not None:
            results = await hybrid_memory_search(
                conn,
                HybridQuery(
                    entity_id=entity_id,
                    time_range=time_range,
                    causal_triple_id=causal_triple_id,
                ),
                scope_filter=scope_filter,
                top_k=l2_top_k,
            )
            content_map = await _load_content_map(
                conn, [r["id"] for r in results]
            )
            for r in results:
                if r["id"] in seen or r["id"] not in content_map:
                    continue
                seen.add(r["id"])
                content = content_map[r["id"]]
                candidates.append(
                    ContextSource(
                        memory_id=r["id"],
                        layer="L2",
                        content=content,
                        score=r["score"],
                    )
                )

        # 层优先级 > 层内 score 降序
        candidates.sort(key=lambda s: (_LAYER_PRIORITY[s.layer], -s.score))
        return candidates

    def _collect_l0(self, session_id: str) -> list[ContextSource]:
        """L0 工作记忆全量加载,按 importance > recency(updated_at) 排序."""
        memories = [
            m for m in self._l0.list(session_id) if m.content is not None
        ]
        # 稳定排序两轮: 先 recency(updated_at 降序),再 importance 降序(主键)
        memories.sort(key=lambda m: m.updated_at, reverse=True)
        memories.sort(key=lambda m: m.importance, reverse=True)
        return [
            ContextSource(
                memory_id=m.id,
                layer="L0",
                content=m.content or "",
                score=m.importance,
            )
            for m in memories
        ]

    # ==================== 贪心裁剪 ====================

    def _greedy_fill(
        self,
        candidates: list[ContextSource],
        budget: int,
    ) -> BuiltContext:
        """按优先级贪心填充(模块文档 03 §3.1 greedy_trim).

        逐条检查:装得下就入选,装不下标记裁剪并继续尝试后续更小的条目.
        token 只在此处对渲染行计数一次(H2: 收集阶段不再预计算,避免双倍编码);
        入选条目的 tokens 字段在此回填。
        """
        selected: list[ContextSource] = []
        lines: list[str] = []
        used = 0
        truncated = False
        for src in candidates:
            line = f"[{src.layer}] {src.content}"
            line_tokens = self._counter.count(line)
            if used + line_tokens > budget:
                truncated = True
                continue
            src.tokens = line_tokens
            selected.append(src)
            lines.append(line)
            used += line_tokens

        return BuiltContext(
            text="\n".join(lines),
            sources=selected,
            total_tokens=used,
            budget=budget,
            truncated=truncated,
        )


__all__ = [
    "DEFAULT_TOKEN_BUDGET",
    "SAFETY_MARGIN",
    "BuiltContext",
    "ContextBuilder",
    "ContextSource",
]
