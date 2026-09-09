"""PromotionManager — L0→L1 晋升规则(主文档 §7.3,L2.8).

Per 主文档 §7.3 记忆流转 + 模块文档 03 §3.3(L0 溢出策略):
- FuzzyMemory 超过 800 tokens 时晋升到 L1 短时记忆
- 晋升 = 写入 temporal_fragments(layer='L1')+ 向量(VectorStore),再从 L0 移除
- 提供显式 promote API + 自动检查钩子(put 时检查 / check_session 扫描)

事务约定同 queries/ 与 vector_store: 所有 async 方法接收 aiosqlite.Connection,
由调用方管理事务(如 SQLiteEngine.transaction()),本类不主动 commit.

使用方法:
    promo = PromotionManager(l0, vector_store=store)
    async with engine.transaction() as conn:
        mid = await promo.put(conn, "sess_1", memory, scope_id="proj_a")
    # 超阈值自动晋升;显式晋升:
    # await promo.promote(conn, "sess_1", mid, scope_id="proj_a")
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import aiosqlite

from ...utils.timeutil import to_iso
from ..models import FuzzyMemory, MemoryLayer
from ..storage.storage_engine import scope_path
from .token_counter import TokenCounter

if TYPE_CHECKING:
    from ..storage.vector_store import VectorStore
    from .l0_working_memory import L0WorkingMemory

# 模块文档 03 §3.3: L0 超 800 tokens 触发晋升(溢出)
L0_PROMOTION_THRESHOLD = 800


def _time_start_iso(memory: FuzzyMemory) -> str:
    """time_start 取值优先级: time_range.exact > approx_start > created_at.

    历史行为是 created_at 墙钟,导致写入方提供的 time_range(如历史对话
    的真实发生时间)在晋升后丢失;基准评测曾被迫按 content 文本匹配回填.
    """
    tr = memory.time_range
    anchor = None
    if tr is not None:
        anchor = tr.exact or tr.approx_start
    return to_iso(anchor or memory.created_at)


class PromotionManager:
    """L0→L1 晋升管理 — 阈值检查 + 写入存储/向量 + L0 移除.

    Args:
        l0: L0 工作记忆
        vector_store: 向量存储(None 时晋升只写 temporal_fragments,不写向量)
        token_counter: token 计数器(None 时新建默认实例)
        threshold: 晋升阈值(默认 800 tokens)
    """

    def __init__(
        self,
        l0: L0WorkingMemory,
        *,
        vector_store: VectorStore | None = None,
        token_counter: TokenCounter | None = None,
        threshold: int = L0_PROMOTION_THRESHOLD,
    ) -> None:
        self._l0 = l0
        self._vector_store = vector_store
        self._counter = token_counter or TokenCounter()
        self._threshold = threshold

    @property
    def threshold(self) -> int:
        return self._threshold

    # ==================== 检查 ====================

    def needs_promotion(self, memory: FuzzyMemory) -> bool:
        """判断一条记忆是否超过晋升阈值(content token 数 > threshold)."""
        return self._counter.count_memory(memory) > self._threshold

    # ==================== 写入钩子 ====================

    async def put(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        memory: FuzzyMemory,
        *,
        scope_id: str | None = None,
        auto_promote: bool = True,
    ) -> str:
        """写入 L0 并按阈值自动晋升(自动检查钩子),返回 memory.id.

        Args:
            conn: aiosqlite 连接(事务由调用方管理)
            session_id: 会话 ID
            memory: 待写入的 FuzzyMemory
            scope_id: 项目/租户 ID(scope 为 PROJECT/TENANT 时必需)
            auto_promote: 超过阈值时是否立即晋升(False 则只写入 L0)
        """
        self._l0.put(session_id, memory)
        if auto_promote and self.needs_promotion(memory):
            await self.promote(conn, session_id, memory.id, scope_id=scope_id)
        return memory.id

    # ==================== 晋升 ====================

    async def promote(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        memory_id: str,
        *,
        scope_id: str | None = None,
    ) -> bool:
        """显式晋升一条 L0 记忆到 L1,返回是否晋升成功.

        步骤(§7.3):
        1. 写入 temporal_fragments(layer='L1')
        2. 写入向量(VectorStore.add_text, fragment_id=memory.id)
        3. 从 L0 移除

        Args:
            conn: aiosqlite 连接(事务由调用方管理)
            session_id: 会话 ID
            memory_id: 待晋升的记忆 ID
            scope_id: 项目/租户 ID(scope 为 PROJECT/TENANT 时必需)

        Returns:
            False: L0 中不存在该记忆;True: 晋升完成

        Raises:
            ValueError: content 为空(无法检索的记忆不晋升)
        """
        memory = self._l0.get(session_id, memory_id)
        if memory is None:
            return False
        return await self._promote_memory(
            conn, session_id, memory, scope_id=scope_id
        )

    async def _promote_memory(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        memory: FuzzyMemory,
        *,
        scope_id: str | None = None,
        vector=None,
    ) -> bool:
        """晋升单条记忆(promote / check_session 共用).

        vector 为预计算 embedding(M6 批量晋升时由 embed_batch 一次算出),
        None 时经 VectorStore.add_text 单独编码。

        Raises:
            ValueError: content 为空(无法检索的记忆不晋升)
        """
        if memory.content is None:
            raise ValueError(f"记忆 {memory.id} content 为空,无法晋升到 L1")

        scope_str = scope_path(memory.scope, scope_id)
        memory.layer = MemoryLayer.L1_SHORT
        memory.touch()
        await conn.execute(
            "INSERT INTO temporal_fragments(id, fragment_id, time_start, time_end, "
            "content, entities, relations, scope, layer, importance, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'L1', ?, ?, ?)",
            (
                memory.id,
                memory.id,
                _time_start_iso(memory),
                to_iso(memory.expires_at) if memory.expires_at else None,
                memory.content,
                json.dumps(memory.entities),
                json.dumps(memory.relations),
                scope_str,
                memory.importance,
                to_iso(memory.created_at),
                to_iso(memory.updated_at),
            ),
        )
        if self._vector_store is not None:
            if vector is None:
                await self._vector_store.add_text(
                    conn, memory.content, fragment_id=memory.id
                )
            else:
                await self._vector_store.add_vector(
                    conn, vector, fragment_id=memory.id
                )
        # 存储写入成功后才从 L0 移除
        self._l0.remove(session_id, memory.id)
        return True

    async def check_session(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        *,
        scope_id: str | None = None,
    ) -> list[str]:
        """扫描会话中所有超过阈值的记忆并晋升,返回晋升的 memory_id 列表.

        多条待晋升时经 embed_batch 一次批量编码(M6),代替逐条 embed;
        编码卸载到线程执行(H3),不阻塞事件循环。
        """
        candidates = [
            m
            for m in self._l0.list(session_id)
            if m.content is not None and self.needs_promotion(m)
        ]
        return await self._promote_batch(conn, session_id, candidates, scope_id=scope_id)

    async def flush_session(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
    ) -> list[str]:
        """会话结束 flush: 晋升该会话**全部** L0 记忆(持久化闭环).

        与 check_session 的区别: 不做阈值过滤 — L0 里都是写入方显式
        沉淀的记忆,会话结束即持久是正确默认(短记忆否则会随进程消失)。
        scope_id 取各记忆自身携带的 scope_id(写入时记录),支持一个
        会话内混合 project/global 写入。
        """
        candidates = [m for m in self._l0.list(session_id) if m.content is not None]
        return await self._promote_batch(
            conn, session_id, candidates, scope_id=None, per_memory_scope=True
        )

    async def _promote_batch(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        candidates: list[FuzzyMemory],
        *,
        scope_id: str | None = None,
        per_memory_scope: bool = False,
    ) -> list[str]:
        """批量晋升(embed_batch 一次编码),check_session/flush_session 共用."""
        if not candidates:
            return []
        vectors: list | None = None
        if self._vector_store is not None:
            vectors = await asyncio.to_thread(
                self._vector_store.embedder.embed_batch,
                [m.content or "" for m in candidates],
            )
        promoted: list[str] = []
        for memory, vector in zip(
            candidates, vectors or [None] * len(candidates), strict=True
        ):
            effective = memory.scope_id if per_memory_scope else scope_id
            if await self._promote_memory(
                conn, session_id, memory, scope_id=effective, vector=vector
            ):
                promoted.append(memory.id)
        return promoted


__all__ = [
    "L0_PROMOTION_THRESHOLD",
    "PromotionManager",
]
