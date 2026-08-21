"""ConflictResolver — Layer 4 第 3 层防护:冲突解决器(主文档 §10.3).

默认策略(§10.3 表格):
- VERSION_STALE → AUTO_MERGE(字段级三路合并,真字段冲突升级 MANUAL)
- WRITE_WRITE → AUTO_LAST(last-write-wins,新值获胜)
- READ_WRITE → AUTO_LAST(读者重读最新值)
- CAUSAL_CYCLE → ROLLBACK(拒绝新操作,保持已有因果链)
- CAUSAL_CONTRADICTION → MANUAL(上报人工,不自动改写)

合并算法(模块文档 05 §2.4): 以 base_value 为基准做字段级三路合并 —
一方未改的字段取另一方,双方都改且不一致时升级 MANUAL;
非 dict 值无法结构合并,回退 AUTO_LAST。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..models import ConflictType
from .conflict_detection import Conflict


class ResolutionStrategy(StrEnum):
    """冲突解决策略(§10.3 默认策略表 + RETRY 备选)."""

    AUTO_MERGE = "auto_merge"
    AUTO_LAST = "auto_last"
    RETRY = "retry"
    ROLLBACK = "rollback"
    MANUAL = "manual"


@dataclass
class Resolution:
    """解决结果.

    action 语义(供调用方分支):
    - "merged": 字段级合并成功,调用方应以 merged_value 重写(requires_rewrite=True)
    - "overwrite": LWW,新值获胜,调用方按新值继续写入
    - "retry": 调用方应重读最新值后重试
    - "reject": 拒绝新操作(回滚),调用方应中止本次写入
    - "manual": 无法自动解决,上报人工;调用方应中止并透出冲突信息
    """

    strategy: ResolutionStrategy
    action: str
    winning_value: Any = None
    merged_value: Any = None
    requires_rewrite: bool = False
    message: str = ""


#: 主文档 §10.3 默认策略表
DEFAULT_STRATEGIES: dict[ConflictType, ResolutionStrategy] = {
    ConflictType.VERSION_STALE: ResolutionStrategy.AUTO_MERGE,
    ConflictType.WRITE_WRITE: ResolutionStrategy.AUTO_LAST,
    ConflictType.READ_WRITE: ResolutionStrategy.AUTO_LAST,
    ConflictType.CAUSAL_CYCLE: ResolutionStrategy.ROLLBACK,
    ConflictType.CAUSAL_CONTRADICTION: ResolutionStrategy.MANUAL,
}

#: 解决后放行写入的 action(其余 action 调用方应中止并上报)
PROCEED_ACTIONS: frozenset[str] = frozenset({"merged", "overwrite"})


class ConflictResolver:
    """冲突解决器(§10.1 第 3 层防护).

    Args:
        strategies: 覆盖默认策略表(键: ConflictType,缺省用 DEFAULT_STRATEGIES)
    """

    def __init__(
        self, strategies: dict[ConflictType, ResolutionStrategy] | None = None
    ) -> None:
        self._strategies = {**DEFAULT_STRATEGIES, **(strategies or {})}

    def resolve(self, conflict: Conflict) -> Resolution:
        """按默认策略表解决冲突."""
        strategy = self._strategies.get(conflict.conflict_type, ResolutionStrategy.AUTO_LAST)
        return self.resolve_with(conflict, strategy)

    def resolve_with(self, conflict: Conflict, strategy: ResolutionStrategy) -> Resolution:
        """按指定策略解决冲突."""
        handlers = {
            ResolutionStrategy.AUTO_MERGE: self._auto_merge,
            ResolutionStrategy.AUTO_LAST: self._auto_last,
            ResolutionStrategy.RETRY: self._retry,
            ResolutionStrategy.ROLLBACK: self._rollback,
            ResolutionStrategy.MANUAL: self._manual,
        }
        return handlers[strategy](conflict)

    # ==================== 策略实现 ====================

    def _auto_merge(self, conflict: Conflict) -> Resolution:
        """字段级三路合并;无法合并回退 AUTO_LAST,真字段冲突升级 MANUAL."""
        merged = self._three_way_merge(
            conflict.base_value, conflict.old_value, conflict.new_value
        )
        if merged is _MERGE_CONFLICT:
            return Resolution(
                strategy=ResolutionStrategy.MANUAL,
                action="manual",
                winning_value=conflict.new_value,
                message=f"字段级合并存在真冲突,需人工解决: {conflict.message}",
            )
        if merged is _MERGE_UNSUPPORTED:
            return self._auto_last(conflict)
        return Resolution(
            strategy=ResolutionStrategy.AUTO_MERGE,
            action="merged",
            winning_value=merged,
            merged_value=merged,
            requires_rewrite=True,
            message=f"自动合并成功: {conflict.message}",
        )

    @staticmethod
    def _three_way_merge(base: Any, old: Any, new: Any) -> Any:
        """三路合并: 双方一致的字段直接取;一方未改(等于 base)取另一方."""
        if old == new:
            return old
        if not isinstance(old, dict) or not isinstance(new, dict):
            return _MERGE_UNSUPPORTED
        base_map = base if isinstance(base, dict) else {}
        merged: dict = {}
        for key in old.keys() | new.keys():
            in_old, in_new = key in old, key in new
            old_val, new_val = old.get(key), new.get(key)
            base_val = base_map.get(key)
            if in_old and in_new:
                if old_val == new_val:
                    merged[key] = old_val
                elif old_val == base_val:
                    merged[key] = new_val  # 旧方未改,取新方
                elif new_val == base_val:
                    merged[key] = old_val  # 新方未改,取旧方
                else:
                    return _MERGE_CONFLICT  # 双方都改且不一致
            elif in_new:
                if key in base_map and base_val != new_val:
                    return _MERGE_CONFLICT  # 旧方删除 vs 新方修改
                merged[key] = new_val  # 新增字段
            else:
                if key in base_map and base_val != old_val:
                    return _MERGE_CONFLICT  # 新方删除 vs 旧方修改
                # 双方一致删除(或仅旧方保留) → 不写入 merged
        return merged

    @staticmethod
    def _auto_last(conflict: Conflict) -> Resolution:
        """last-write-wins: 新操作时间戳晚,新值获胜."""
        return Resolution(
            strategy=ResolutionStrategy.AUTO_LAST,
            action="overwrite",
            winning_value=conflict.new_value,
            message=f"LWW 取新值: {conflict.message}",
        )

    @staticmethod
    def _retry(conflict: Conflict) -> Resolution:
        """重试: 调用方应重读最新值后重试(乐观锁退避由调用方实现)."""
        return Resolution(
            strategy=ResolutionStrategy.RETRY,
            action="retry",
            winning_value=conflict.old_value,
            message=f"应重读最新值后重试: {conflict.message}",
        )

    @staticmethod
    def _rollback(conflict: Conflict) -> Resolution:
        """回滚新操作: 保持现有状态,拒绝本次写入(因果环默认策略)."""
        return Resolution(
            strategy=ResolutionStrategy.ROLLBACK,
            action="reject",
            winning_value=conflict.old_value,
            requires_rewrite=True,
            message=f"拒绝新操作(回滚): {conflict.message}",
        )

    @staticmethod
    def _manual(conflict: Conflict) -> Resolution:
        """人工介入: 不自动改写,透出冲突信息上报."""
        return Resolution(
            strategy=ResolutionStrategy.MANUAL,
            action="manual",
            winning_value=conflict.old_value,
            message=f"需人工解决: {conflict.message}",
        )


class _MergeSentinel:
    """合并结果哨兵(区分"合并非 dict 值"与"真字段冲突")."""


_MERGE_UNSUPPORTED = _MergeSentinel()
_MERGE_CONFLICT = _MergeSentinel()


__all__ = [
    "DEFAULT_STRATEGIES",
    "PROCEED_ACTIONS",
    "ConflictResolver",
    "Resolution",
    "ResolutionStrategy",
]
