"""ConcurrencyController — Layer 4 组合门面(主文档 §10.1 三层防护).

组合 LockManager(锁)+ ConflictDetector(检测)+ ConflictResolver(解决),
供 MemoryMiddleware 写入路径及任务链调用方使用:

- 写入 triple 前: guard_triple_write 检测 WRITE_WRITE/CAUSAL_CONTRADICTION,
  自动解决(LWW/合并)则放行,拒绝/人工则由调用方透出 WriteStatus.CONFLICT
- 任务链「读-改-写」: with controller.locks.exclusive(...) 持锁 +
  detector.check_write/commit_write 乐观版本校验(无 lost update 的保证路径)
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from ..models import ConflictType
from .conflict_detection import Conflict, ConflictDetector
from .conflict_resolution import (
    PROCEED_ACTIONS,
    ConflictResolver,
    Resolution,
    ResolutionStrategy,
)
from .lock_manager import LockManager


@dataclass
class GuardResult:
    """guard_triple_write 检出冲突的结果: 冲突详情 + 解决结果."""

    conflict: Conflict
    resolution: Resolution


class ConcurrencyController:
    """Layer 4 三层防护组合门面.

    Args:
        lock_timeout: 锁默认超时秒数(透传 LockManager)
        strategies: 覆盖冲突解决默认策略表(透传 ConflictResolver)
        conflict_window: 冲突检测窗口秒数(透传 ConflictDetector)
    """

    def __init__(
        self,
        *,
        lock_timeout: float | None = LockManager.DEFAULT_TIMEOUT,
        strategies: dict[ConflictType, ResolutionStrategy] | None = None,
        conflict_window: float = 60.0,
    ) -> None:
        self.locks = LockManager(default_timeout=lock_timeout)
        self.detector = ConflictDetector(conflict_window=conflict_window)
        self.resolver = ConflictResolver(strategies)

    async def guard_triple_write(
        self,
        conn: aiosqlite.Connection,
        *,
        scope: str,
        subject_id: str,
        predicate: str,
        object_id: str | None,
        object_value: str | None,
        chain_id: str,
    ) -> GuardResult | None:
        """写入 triple 前的检测 + 解决.

        Returns:
            None: 无冲突,直接写入
            GuardResult: 检出冲突;resolution.action ∈ PROCEED_ACTIONS 表示
                自动解决(LWW/合并)放行,否则("reject"/"manual")调用方应
                中止写入并上报冲突
        """
        conflict = await self.detector.check_triple_write(
            conn,
            scope=scope,
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            object_value=object_value,
            chain_id=chain_id,
        )
        if conflict is None:
            return None
        return GuardResult(conflict=conflict, resolution=self.resolver.resolve(conflict))

    @staticmethod
    def should_proceed(guard: GuardResult | None) -> bool:
        """guard 结果是否放行写入(None 或自动解决 → True)."""
        return guard is None or guard.resolution.action in PROCEED_ACTIONS


__all__ = ["ConcurrencyController", "GuardResult"]

