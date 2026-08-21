"""ConflictDetector — Layer 4 第 2 层防护:冲突检测器(主文档 §10.1/§10.3).

检测五类冲突(Layer 0 ConflictType):
- VERSION_STALE: 乐观锁版本过期 — 任务链基于旧版本「读-改-写」,
  提交时 expected_version 落后于当前版本
- WRITE_WRITE: 写写冲突 — 其他链在窗口期内已提交同资源更新
  (base_version 落后),或 DB 层同 (scope, subject, predicate)
  存在取值不同的现行 triple(覆写当前状态)
- READ_WRITE: 读写冲突 — 读操作之后有其他链写入,读到的数据已过期
- CAUSAL_CYCLE: 因果环 — 新 triple 的 predecessor_id 沿前驱链
  回溯可达自身(含自环)
- CAUSAL_CONTRADICTION: 因果矛盾 — causal 类型的同
  (scope, subject, predicate) 存在取值冲突的现行 triple

版本追踪说明:
    存储层(Layer 1)实体/三元组表无 version 列(MVP schema 未含),
    乐观锁版本由本检测器在应用层按资源 ID 维护(进程内单调递增)。
    与 LockManager 的关系: 锁防「执行期穿插」,版本检查防「跨锁周期的
    lost update」(读时未持锁、写时校验),两者互补(模块文档 05 §2.3 混合策略)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import aiosqlite

from ...utils.timeutil import now_utc
from ..models import ConflictType

OperationType = Literal["read", "write"]

# 因果环检测的默认最大回溯深度(防御脏数据形成的超长链)
DEFAULT_CYCLE_MAX_DEPTH = 100


@dataclass
class MemoryOperation:
    """一次记忆操作(冲突检测输入,分析文档 §4.1 MemoryOperation 简化版).

    Attributes:
        operation_type: "read" / "write"
        resource_id: 资源 ID(记忆/实体/三元组)
        chain_id: 任务链标识(会话/链/任务 ID)
        value: 写入的新值(read 时为 None)
        base_value: 读取时的基准值(三路合并的 base)
        base_version: 读取时观察到的版本(WRITE_WRITE 检测用)
        expected_version: 乐观锁期望版本(VERSION_STALE 检测用)
    """

    operation_type: OperationType
    resource_id: str
    chain_id: str
    value: Any = None
    base_value: Any = None
    base_version: int | None = None
    expected_version: int | None = None
    timestamp: datetime = field(default_factory=now_utc)


@dataclass
class Conflict:
    """检测到的冲突(分析文档 §4.1 Conflict 简化版).

    base_value/old_value/new_value 供 ConflictResolver 三路合并;
    conflicting_memory_id 映射 middlewares.dto.ConflictInfo 同名字段。
    """

    conflict_type: ConflictType
    resource_id: str
    chain_id: str  # 触发检测的新操作方
    message: str = ""
    other_chain_id: str | None = None  # 冲突另一方
    conflicting_memory_id: str | None = None  # 已存在的冲突记忆/triple ID
    base_value: Any = None
    old_value: Any = None  # 当前(对方)值
    new_value: Any = None  # 新(己方)值
    expected_version: int | None = None
    current_version: int | None = None
    detected_at: datetime = field(default_factory=now_utc)


@dataclass
class _ResourceVersion:
    """资源的应用层乐观锁版本 + 最近提交值."""

    version: int
    value: Any
    chain_id: str
    updated_at: datetime


class ConflictDetector:
    """冲突检测器(§10.1 第 2 层防护).

    Args:
        conflict_window: 操作日志窗口秒数,READ_WRITE/WRITE_WRITE
            只追溯窗口内的跨链操作(默认 60s)
    """

    def __init__(self, *, conflict_window: float = 60.0) -> None:
        self.conflict_window = conflict_window
        self._versions: dict[str, _ResourceVersion] = {}
        # resource_id → [(chain_id, 读时间)]
        self._reads: dict[str, list[tuple[str, datetime]]] = {}

    # ==================== 乐观锁版本 ====================

    def current_version(self, resource_id: str) -> int:
        """资源当前版本(未写入过为 0)."""
        state = self._versions.get(resource_id)
        return state.version if state else 0

    def check_write(self, op: MemoryOperation) -> Conflict | None:
        """写前同步检测(不修改状态): VERSION_STALE 优先于 WRITE_WRITE.

        - expected_version 提供且 != 当前版本 → VERSION_STALE
        - base_version 提供且 < 当前版本(其他链已提交) → WRITE_WRITE
        """
        state = self._versions.get(op.resource_id)
        current = state.version if state else 0
        if op.expected_version is not None and op.expected_version != current:
            return Conflict(
                conflict_type=ConflictType.VERSION_STALE,
                resource_id=op.resource_id,
                chain_id=op.chain_id,
                other_chain_id=state.chain_id if state else None,
                message=(
                    f"版本过期: 期望 v{op.expected_version},当前 v{current}"
                    + (f"(最近由 {state.chain_id} 提交)" if state else "")
                ),
                base_value=op.base_value,
                old_value=state.value if state else None,
                new_value=op.value,
                expected_version=op.expected_version,
                current_version=current,
            )
        if op.base_version is not None and op.base_version < current and state is not None:
            return Conflict(
                conflict_type=ConflictType.WRITE_WRITE,
                resource_id=op.resource_id,
                chain_id=op.chain_id,
                other_chain_id=state.chain_id,
                message=(
                    f"写写冲突: 基于 v{op.base_version} 的修改,"
                    f"{state.chain_id} 已提交至 v{current}"
                ),
                base_value=op.base_value,
                old_value=state.value,
                new_value=op.value,
                expected_version=op.base_version,
                current_version=current,
            )
        return None

    def commit_write(self, op: MemoryOperation) -> int:
        """检测通过后提交: 版本 +1 并记录值,返回新版本."""
        state = self._versions.get(op.resource_id)
        version = (state.version if state else 0) + 1
        self._versions[op.resource_id] = _ResourceVersion(
            version=version, value=op.value, chain_id=op.chain_id, updated_at=op.timestamp
        )
        return version

    # ==================== 读写冲突 ====================

    def record_read(self, resource_id: str, chain_id: str, *, at: datetime | None = None) -> None:
        """记录一次读操作(READ_WRITE 检测的事实来源)."""
        reads = self._reads.setdefault(resource_id, [])
        reads.append((chain_id, at or now_utc()))
        # 限制日志长度(窗口外清理在 check 时做,这里兜底防膨胀)
        if len(reads) > 100:
            del reads[:-100]

    def check_read_write(
        self, resource_id: str, chain_id: str, *, since: datetime
    ) -> Conflict | None:
        """读者自检: since(读取时刻)之后有其他链写入 → READ_WRITE."""
        state = self._versions.get(resource_id)
        if (
            state is not None
            and state.chain_id != chain_id
            and state.updated_at > since
            and (state.updated_at - since).total_seconds() <= self.conflict_window
        ):
            return Conflict(
                conflict_type=ConflictType.READ_WRITE,
                resource_id=resource_id,
                chain_id=chain_id,
                other_chain_id=state.chain_id,
                message=(
                    f"读写冲突: 读取后 {state.chain_id} 已写入 v{state.version},数据已过期"
                ),
                old_value=state.value,
                current_version=state.version,
            )
        return None

    # ==================== 因果冲突(DB 层) ====================

    async def check_causal_cycle(
        self,
        conn: aiosqlite.Connection,
        *,
        triple_id: str,
        predecessor_id: str | None,
        chain_id: str,
        max_depth: int = DEFAULT_CYCLE_MAX_DEPTH,
    ) -> Conflict | None:
        """写入前检测因果环: predecessor_id 沿前驱链回溯可达 triple_id 即成环.

        方向约定: triple.predecessor_id = P 表示 P 导致 triple。
        新增 triple_id → predecessor_id 边,当且仅当 triple_id 是
        predecessor_id 的祖先(沿 predecessor 链回溯命中)时成环(含自环)。
        """
        if predecessor_id is None:
            return None
        if predecessor_id == triple_id:
            return Conflict(
                conflict_type=ConflictType.CAUSAL_CYCLE,
                resource_id=triple_id,
                chain_id=chain_id,
                conflicting_memory_id=predecessor_id,
                message=f"因果自环: {triple_id} 的前驱是自身",
            )
        visited: set[str] = set()
        current: str | None = predecessor_id
        depth = 0
        while current is not None and depth <= max_depth and current not in visited:
            if current == triple_id:
                return Conflict(
                    conflict_type=ConflictType.CAUSAL_CYCLE,
                    resource_id=triple_id,
                    chain_id=chain_id,
                    conflicting_memory_id=current,
                    message=(
                        f"因果环: {triple_id} 经前驱链可达其前驱 {predecessor_id}"
                    ),
                )
            visited.add(current)
            cursor = await conn.execute(
                "SELECT predecessor_id FROM triples WHERE id = ?", [current]
            )
            row = await cursor.fetchone()
            current = row["predecessor_id"] if row else None
            depth += 1
        return None

    async def check_causal_contradiction(
        self,
        conn: aiosqlite.Connection,
        *,
        scope: str,
        subject_id: str,
        predicate: str,
        object_id: str | None,
        object_value: str | None,
        chain_id: str,
    ) -> Conflict | None:
        """因果矛盾: 现行 causal triple 同 (scope, subject, predicate) 但取值冲突.

        仅统计 relation_type='causal' 且 valid_to IS NULL(现行有效)的 triple;
        取值完全相同(重述事实)不算矛盾。
        """
        conflict = await self._find_conflicting_triple(
            conn,
            scope=scope,
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            object_value=object_value,
            relation_type="causal",
        )
        if conflict is None:
            return None
        existing_id, existing_object = conflict
        return Conflict(
            conflict_type=ConflictType.CAUSAL_CONTRADICTION,
            resource_id=f"{scope}:{subject_id}:{predicate}",
            chain_id=chain_id,
            conflicting_memory_id=existing_id,
            message=(
                f"因果矛盾: ({subject_id}, {predicate}) 现行值为 "
                f"{existing_object!r},新值 {object_id or object_value!r} 与之冲突"
            ),
            old_value=existing_object,
            new_value=object_id or object_value,
        )

    async def check_triple_write(
        self,
        conn: aiosqlite.Connection,
        *,
        scope: str,
        subject_id: str,
        predicate: str,
        object_id: str | None,
        object_value: str | None,
        chain_id: str,
    ) -> Conflict | None:
        """写入 triple 前检测 WRITE_WRITE: 现行同键 triple 取值不同(覆写当前状态).

        §6.3 触发器对同 (scope, subject, predicate) 只保留最新值,
        因此覆写视为写写冲突,交由 ConflictResolver 按默认 AUTO_LAST 解决;
        取值相同(重复断言同一事实)不算冲突。
        """
        conflict = await self._find_conflicting_triple(
            conn,
            scope=scope,
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            object_value=object_value,
            relation_type=None,
        )
        if conflict is None:
            return None
        existing_id, existing_object = conflict
        return Conflict(
            conflict_type=ConflictType.WRITE_WRITE,
            resource_id=f"{scope}:{subject_id}:{predicate}",
            chain_id=chain_id,
            conflicting_memory_id=existing_id,
            message=(
                f"写写冲突: ({subject_id}, {predicate}) 现行值为 "
                f"{existing_object!r},新值 {object_id or object_value!r} 将覆写"
            ),
            old_value=existing_object,
            new_value=object_id or object_value,
        )

    @staticmethod
    async def _find_conflicting_triple(
        conn: aiosqlite.Connection,
        *,
        scope: str,
        subject_id: str,
        predicate: str,
        object_id: str | None,
        object_value: str | None,
        relation_type: str | None,
    ) -> tuple[str, str] | None:
        """查找同键现行 triple 中取值不同者,返回 (id, 现有取值)."""
        sql = (
            "SELECT id, object_id, object_value FROM triples "
            "WHERE scope = ? AND subject_id = ? AND predicate = ? AND valid_to IS NULL"
        )
        params: list[str] = [scope, subject_id, predicate]
        if relation_type is not None:
            sql += " AND relation_type = ?"
            params.append(relation_type)
        cursor = await conn.execute(sql, params)
        new_object = object_id or object_value
        for row in await cursor.fetchall():
            existing_object = row["object_id"] or row["object_value"]
            if existing_object != new_object:
                return str(row["id"]), str(existing_object)
        return None


__all__ = [
    "DEFAULT_CYCLE_MAX_DEPTH",
    "Conflict",
    "ConflictDetector",
    "MemoryOperation",
    "OperationType",
]
