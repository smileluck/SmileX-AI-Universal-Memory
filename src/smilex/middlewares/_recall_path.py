"""MemoryMiddleware 检索路径(M.4)— recall 多路召回 / 归档并入 / scope 折算.

2026-09 从 memory.py 拆出: 方法体不变,经 _RecallPathMixin 组合回 MemoryMiddleware;
共享状态(_engine/_context_builder/_counter/_current_scope_id 与
_require_initialized)由宿主类提供.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ..memory.contracts import MemoryRef, RecallRequest, RecallResponse
from ..memory.models import MemoryLayer, MemoryScope, ScopeFilter
from ..memory.storage.storage_engine import scope_path
from ..utils.timeutil import now_utc, to_iso

if TYPE_CHECKING:
    from ..memory.storage.storage_engine import StorageEngine

# ContextSource.layer("L0"/"L1"/"L2") → MemoryLayer 枚举
_LAYER_MAP: dict[str, MemoryLayer] = {
    "L0": MemoryLayer.L0_WORKING,
    "L1": MemoryLayer.L1_SHORT,
    "L2": MemoryLayer.L2_LONG,
}

# MemoryRef.snippet 截断长度
_SNIPPET_MAX_CHARS = 120


def _time_range_tuple(request: RecallRequest) -> tuple[str, str] | None:
    """RecallRequest.time_range(TimeRange)→ build_context 的 (start, end) ISO 元组.

    relative 模式(如「昨天下午」)无法转换为区间,MVP 忽略(None).
    """
    tr = request.time_range
    if tr is None:
        return None
    if tr.exact is not None:
        iso = to_iso(tr.exact)
        return (iso, iso)
    if tr.approx_start is not None or tr.approx_end is not None:
        return (
            to_iso(tr.approx_start or now_utc()),
            to_iso(tr.approx_end or now_utc()),
        )
    return None


class _RecallPathMixin:
    """M.4 检索路径(组合进 MemoryMiddleware)."""

    # 宿主类(MemoryMiddleware)提供的共享状态注解(存量 mixin 模式,
    # 此处仅为类型可见性;赋值在宿主 __init__)
    _track_access: bool
    _engine: StorageEngine

    # ==================== M.4 检索 ====================

    async def recall(
        self,
        request: RecallRequest,
        *,
        session_id: str | None = None,
        include_archived: bool = False,
    ) -> RecallResponse:
        """检索记忆(§3.2): 多路召回(L0 全量 + L1 向量 KNN + L2 混合)+ 预算裁剪.

        Args:
            request: 检索请求(query/scope_filter/top_k/token_budget/...)
            session_id: L0 会话 ID(None 跳过 L0 通道)
            include_archived: True 时把归档数据(§11.6 冷热分层)并入结果 —
                走 Archiver.query_archived 非向量通道(scope + 关键词),
                以 ``[归档]`` 前缀追加到 context,MemoryRef 标记为 L2_LONG;
                默认 False 只查热数据(向后兼容)
        """
        self._require_initialized()
        scope_filter = self._effective_scope_filter(request.scope_filter)
        built = await self._context_builder.build_context(
            self._engine.conn,
            session_id=session_id,
            query_text=request.query,
            entity_id=request.entity_filter,
            time_range=_time_range_tuple(request),
            scope_filter=scope_filter,
            token_budget=request.token_budget,
            l1_top_k=request.top_k,
            l2_top_k=request.top_k,
        )
        sources = [
            MemoryRef(
                id=src.memory_id,
                layer=_LAYER_MAP[src.layer],
                score=min(max(src.score, 0.0), 1.0),
                snippet=src.content[:_SNIPPET_MAX_CHARS],
            )
            for src in built.sources
        ]
        context_text = built.text
        token_count = built.total_tokens
        if include_archived:
            archived_lines, archived_refs, archived_tokens = await self._recall_archived(
                request, scope_filter
            )
            if archived_lines:
                context_text = (
                    f"{context_text}\n{archived_lines}" if context_text else archived_lines
                )
                sources.extend(archived_refs)
                token_count += archived_tokens
        layers_used = list(dict.fromkeys(ref.layer for ref in sources))
        if self._track_access:
            await self._record_access([ref.id for ref in sources])
        return RecallResponse(
            context=context_text,
            sources=sources,
            layers_used=layers_used,
            token_count=token_count,
        )

    async def _record_access(self, source_ids: list[str]) -> None:
        """检索反馈闭环(§ 主动优化): 本次召回命中的 fragment 记一次访问.

        - source id 是混合命名空间(L1 向量通道含实体/三元组 id,L2 为三元组
          id,L0 为未晋升内存 id)——先 IN 过滤出真实 fragment 行,其余 UPDATE
          影响 0 行无害;L0 已晋升的 id 与 fragment id 相同,自然命中
        - 计数落库供 forget(续命+升值)与 dedup(合并时求和)消费;
          失败只降级不抛(检索路径不能因统计写失败而断)
        """
        unique_ids = list(dict.fromkeys(source_ids))
        if not unique_ids:
            return
        try:
            conn = self._engine.conn
            placeholders = ",".join("?" for _ in unique_ids)
            cur = await conn.execute(
                "SELECT id FROM temporal_fragments WHERE id IN "
                f"({placeholders})",
                unique_ids,
            )
            fragment_ids = [str(r["id"]) for r in await cur.fetchall()]
            if not fragment_ids:
                return
            ph = ",".join("?" for _ in fragment_ids)
            await conn.execute(
                "UPDATE temporal_fragments SET access_count = access_count + 1, "
                f"last_accessed_at = ? WHERE id IN ({ph})",
                [to_iso(now_utc()), *fragment_ids],
            )
            await conn.commit()
        except Exception as exc:  # noqa: BLE001 — 统计写失败不影响检索结果
            from ..memory.observability import get_logger

            get_logger("recall").warning(
                "access_tracking_failed", error=str(exc)
            )

    async def _recall_archived(
        self, request: RecallRequest, scope_filter: ScopeFilter
    ) -> tuple[str, list[MemoryRef], int]:
        """归档召回(include_archived=True 的辅助): 按有效 scope 查归档表.

        Returns:
            (context 追加行, MemoryRef 列表, 追加 token 数)
        """
        from ..memory.quality.archiver import Archiver  # 延迟导入: 按需

        scopes = self._archived_query_scopes(scope_filter)
        archiver = Archiver(self._engine)
        rows: list[dict] = []
        for scope in scopes:
            rows.extend(
                await archiver.query_archived(
                    scope=scope, keyword=request.query, limit=request.top_k
                )
            )
        lines: list[str] = []
        refs: list[MemoryRef] = []
        tokens = 0
        for row in rows[: request.top_k]:
            if row["table"] == "fragments":
                content = str(row["content"])
            else:
                obj = row["object_id"] or row["object_value"] or "?"
                content = f"{row['subject_id']} {row['predicate']} {obj}"
            line = f"[归档] {content}"
            lines.append(line)
            tokens += self._counter.count(line)
            refs.append(
                MemoryRef(
                    id=str(row["id"]),
                    layer=MemoryLayer.L2_LONG,
                    score=0.0,
                    snippet=content[:_SNIPPET_MAX_CHARS],
                )
            )
        return "\n".join(lines), refs, tokens

    def _archived_query_scopes(self, scope_filter: ScopeFilter) -> list[str | None]:
        """把有效 ScopeFilter 折算为归档查询的 scope 列表(None = 不过滤)."""
        if scope_filter.include_all_projects:
            return [None]
        scopes: list[str | None] = []
        if scope_filter.include_global:
            scopes.append("global")
        if scope_filter.include_project:
            scopes.append(scope_path(MemoryScope.PROJECT, scope_filter.include_project))
        if scope_filter.include_tenant:
            scopes.append(scope_path(MemoryScope.TENANT, scope_filter.include_tenant))
        return scopes or [None]

    def _effective_scope_filter(self, scope_filter: ScopeFilter) -> ScopeFilter:
        """scope_filter 未指定项目时,默认带上当前项目(§5.3 典型场景).

        存储层默认 ScopeFilter 只含 global;middleware 已 initialize_project
        时,未显式指定 include_project / include_all_projects 视为查当前项目.
        """
        if (
            scope_filter.include_project is None
            and not scope_filter.include_all_projects
            and self._current_scope_id is not None
        ):
            return replace(scope_filter, include_project=self._current_scope_id)
        return scope_filter
