"""同步矛盾检测 — Layer 5 知识质量(主文档 §11.2 / 06-layer5 §3.2).

规则版(零 LLM,P1 范围 = 同步轻量;异步 LLM 兜底留 P2,见 06-layer5 §2.2):
- TEMPORAL 时态矛盾: 同 (scope, subject, predicate) 时间区间重叠且取值不同
  (至少一方有 valid_to;双现行取值冲突归入 VALUE/NUMERIC,不重复报)
- NUMERIC 数量矛盾: 同键双现行,双方取值可提取数字且相对差 > 容差(默认 10%,
  容差内视为同一数量的不同表述,不报)
- VALUE 属性/取值矛盾: 同键双现行取值不同,且谓词为单值语义 —
  relation_type ∈ {project_state, task_status, config}(§6.3 状态跟踪)
  或谓词在 enum_predicates(枚举互斥);
  多值谓词(如 uses_tech)不同取值不算矛盾.
  注: §6.3 触发器对状态跟踪类 triple 覆写时自动把旧值置 valid_to(LWW),
  故 scan 时状态类双现行冲突不会出现——状态覆写矛盾由写入时检测
  (check_new / Layer 4 WRITE_WRITE)在落库前拦截;scan 的 VALUE 主要来自
  枚举谓词(semantic 边不经过该触发器)
- CAUSAL 因果矛盾: causal 类型的同键双现行取值冲突(对齐 Layer 4
  CAUSAL_CONTRADICTION 语义),或反向因果 — (A 导致 B) 与 (B 导致 A) 并存

报告复用 Layer 0 ConflictType: VALUE/NUMERIC/TEMPORAL → WRITE_WRITE
(均为「同键两笔写入断言不同值」),CAUSAL → CAUSAL_CONTRADICTION;
细分维度由 Contradiction.kind 承载.

退出标准(§14.4): 标注数据集准确率 > 90%,见 tests/unit/test_contradiction.py.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from ...utils.timeutil import from_iso, now_utc
from ..models import ConflictType

if TYPE_CHECKING:
    from ..storage.storage_engine import StorageEngine

# 单值语义 relation_type(§6.3 触发器: 同键只保留最新值,双现行不同值即冲突)
SINGLE_VALUED_RELATION_TYPES = frozenset({"project_state", "task_status", "config"})

# 默认枚举互斥谓词(同键双现行不同值即冲突);可按领域扩展
DEFAULT_ENUM_PREDICATES = frozenset(
    {"color", "status", "state", "priority", "category", "颜色", "状态", "类别", "优先级"}
)

# 因果谓词(反向因果检测: 同谓词 (A→B) 与 (B→A) 并存即矛盾)
CAUSE_PREDICATES = frozenset({"导致", "causes", "leads_to"})

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


class ContradictionKind(StrEnum):
    """矛盾细分维度(06-layer5 §3.2 四维)."""

    VALUE = "value"  # 属性/取值矛盾
    NUMERIC = "numeric"  # 数量矛盾
    TEMPORAL = "temporal"  # 时态矛盾
    CAUSAL = "causal"  # 因果矛盾


# kind → Layer 0 ConflictType(复用,见模块 docstring)
_KIND_TO_CONFLICT_TYPE: dict[ContradictionKind, ConflictType] = {
    ContradictionKind.VALUE: ConflictType.WRITE_WRITE,
    ContradictionKind.NUMERIC: ConflictType.WRITE_WRITE,
    ContradictionKind.TEMPORAL: ConflictType.WRITE_WRITE,
    ContradictionKind.CAUSAL: ConflictType.CAUSAL_CONTRADICTION,
}


@dataclass
class Contradiction:
    """一对矛盾三元组(按 valid_from 排序,existing 为较早者)."""

    kind: ContradictionKind
    conflict_type: ConflictType
    scope: str
    subject_id: str
    predicate: str
    existing_triple_id: str
    new_triple_id: str
    existing_object: str
    new_object: str
    message: str = ""
    detected_at: datetime = field(default_factory=now_utc)


@dataclass
class ContradictionReport:
    """一次 scan 的矛盾报告(§11.2)."""

    scope: str
    scanned_triples: int = 0
    contradictions: list[Contradiction] = field(default_factory=list)
    elapsed_ms: int = 0

    def count_by_kind(self) -> dict[ContradictionKind, int]:
        """按维度统计矛盾数."""
        counts: dict[ContradictionKind, int] = {}
        for c in self.contradictions:
            counts[c.kind] = counts.get(c.kind, 0) + 1
        return counts


@dataclass
class _TripleRow:
    """scan 加载的三元组轻量视图."""

    id: str
    subject_id: str
    predicate: str
    object_value: str  # object_id or object_value(取值视图)
    relation_type: str
    valid_from: datetime
    valid_to: datetime | None

    @property
    def current(self) -> bool:
        return self.valid_to is None


class ContradictionDetector:
    """同步矛盾检测器(§11.2 detect_sync / detect_async 的规则版).

    Args:
        storage: Layer 1 StorageEngine(已 initialize)
        numeric_tolerance: 数量矛盾相对差容差(默认 0.1,06-layer5 §3.2)
        enum_predicates: 枚举互斥谓词集(None 时用 DEFAULT_ENUM_PREDICATES)
    """

    def __init__(
        self,
        storage: StorageEngine,
        *,
        numeric_tolerance: float = 0.1,
        enum_predicates: frozenset[str] | None = None,
    ) -> None:
        self._storage = storage
        self._numeric_tolerance = numeric_tolerance
        self._enum_predicates = (
            enum_predicates if enum_predicates is not None else DEFAULT_ENUM_PREDICATES
        )

    # ==================== 写入时同步检测 ====================

    async def check_new(
        self,
        *,
        scope: str,
        subject_id: str,
        predicate: str,
        object_id: str | None = None,
        object_value: str | None = None,
        relation_type: str = "semantic",
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> list[Contradiction]:
        """写入前对单条新 triple 做同步检测,返回与现行数据的矛盾列表."""
        cursor = await self._storage.conn.execute(
            "SELECT id, subject_id, predicate, object_id, object_value, "
            "relation_type, valid_from, valid_to FROM triples "
            "WHERE scope = ? AND subject_id = ? AND predicate = ?",
            [scope, subject_id, predicate],
        )
        existing = [self._to_row(r) for r in await cursor.fetchall()]
        new = _TripleRow(
            id="",
            subject_id=subject_id,
            predicate=predicate,
            object_value=object_id or object_value or "",
            relation_type=relation_type,
            valid_from=valid_from or now_utc(),
            valid_to=valid_to,
        )
        found = [
            c
            for old in existing
            if (c := self._classify_pair(scope, old, new)) is not None
        ]
        found.extend(await self._check_inverse_causal(scope, new))
        return found

    # ==================== 按需扫描 ====================

    async def scan(self, scope: str) -> ContradictionReport:
        """扫描指定 scope 全部三元组,输出矛盾报告(规则版 detect_async)."""
        start = time.monotonic()
        cursor = await self._storage.conn.execute(
            "SELECT id, subject_id, predicate, object_id, object_value, "
            "relation_type, valid_from, valid_to FROM triples WHERE scope = ?",
            [scope],
        )
        rows = [self._to_row(r) for r in await cursor.fetchall()]
        report = ContradictionReport(scope=scope, scanned_triples=len(rows))

        # 1. 同 (subject, predicate) 组内两两检测(时态/数量/取值/同键因果)
        groups: dict[tuple[str, str], list[_TripleRow]] = {}
        for row in rows:
            groups.setdefault((row.subject_id, row.predicate), []).append(row)
        for group in groups.values():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if (
                        c := self._classify_pair(scope, group[i], group[j])
                    ) is not None:
                        report.contradictions.append(c)

        # 2. 反向因果: 同谓词 (A→B) 与 (B→A) 并存
        forward: dict[tuple[str, str, str], _TripleRow] = {}
        seen_pairs: set[frozenset[str]] = set()
        for row in rows:
            if row.relation_type != "causal" or row.predicate not in CAUSE_PREDICATES:
                continue
            if not row.current:
                continue
            # object_value 视图此时承载 object_id(反向因果需要实体端点)
            key = (row.predicate, row.subject_id, row.object_value)
            forward[key] = row
        for (predicate, subject, obj), row in forward.items():
            reverse = forward.get((predicate, obj, subject))
            if reverse is None:
                continue
            pair = frozenset({row.id, reverse.id})
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            report.contradictions.append(
                self._make(
                    ContradictionKind.CAUSAL,
                    scope,
                    row,
                    reverse,
                    f"反向因果: ({subject} {predicate} {obj}) 与 "
                    f"({obj} {predicate} {subject}) 并存",
                )
            )

        report.elapsed_ms = int((time.monotonic() - start) * 1000)
        return report

    # ==================== 分类规则 ====================

    def _classify_pair(
        self, scope: str, a: _TripleRow, b: _TripleRow
    ) -> Contradiction | None:
        """同键两条 triple 的矛盾分类;无矛盾返回 None."""
        if a.object_value == b.object_value:
            return None  # 重述同一事实不算矛盾
        existing, new = (a, b) if a.valid_from <= b.valid_from else (b, a)

        # 因果矛盾: causal 同键双现行取值冲突
        if a.relation_type == b.relation_type == "causal" and a.current and b.current:
            return self._make(
                ContradictionKind.CAUSAL,
                scope,
                existing,
                new,
                f"因果矛盾: ({a.subject_id}, {a.predicate}) 现行值 "
                f"{existing.object_value!r} 与 {new.object_value!r} 冲突",
            )

        # 时态矛盾: 区间重叠且取值不同(至少一方已闭合;双现行由取值规则覆盖)
        if (a.valid_to is not None or b.valid_to is not None) and _overlaps(a, b):
            return self._make(
                ContradictionKind.TEMPORAL,
                scope,
                existing,
                new,
                f"时态矛盾: ({a.subject_id}, {a.predicate}) 在时间区间重叠段取值 "
                f"{existing.object_value!r} 与 {new.object_value!r} 不同",
            )

        if not (a.current and b.current):
            return None

        # 数量矛盾: 双方取值含数字且相对差超容差;容差内视为同一数量
        num_a, num_b = _extract_number(a.object_value), _extract_number(b.object_value)
        if num_a is not None and num_b is not None:
            if num_a == num_b:
                return None
            if abs(num_a - num_b) / max(abs(num_a), abs(num_b)) <= self._numeric_tolerance:
                return None
            return self._make(
                ContradictionKind.NUMERIC,
                scope,
                existing,
                new,
                f"数量矛盾: ({a.subject_id}, {a.predicate}) 取值 "
                f"{existing.object_value!r} 与 {new.object_value!r} 数量不一致",
            )

        # 取值矛盾: 仅单值语义谓词(状态跟踪 relation_type / 枚举互斥)
        if (
            a.relation_type in SINGLE_VALUED_RELATION_TYPES
            or b.relation_type in SINGLE_VALUED_RELATION_TYPES
            or a.predicate in self._enum_predicates
        ):
            return self._make(
                ContradictionKind.VALUE,
                scope,
                existing,
                new,
                f"取值矛盾: ({a.subject_id}, {a.predicate}) 现行值 "
                f"{existing.object_value!r} 与 {new.object_value!r} 冲突",
            )
        return None

    async def _check_inverse_causal(
        self, scope: str, new: _TripleRow
    ) -> list[Contradiction]:
        """写入时反向因果检测: 已存在同谓词 (B→A) 与新 (A→B) 冲突."""
        if (
            new.relation_type != "causal"
            or new.predicate not in CAUSE_PREDICATES
            or not new.object_value
        ):
            return []
        cursor = await self._storage.conn.execute(
            "SELECT id, subject_id, predicate, object_id, object_value, "
            "relation_type, valid_from, valid_to FROM triples "
            "WHERE scope = ? AND relation_type = 'causal' AND predicate = ? "
            "AND subject_id = ? AND object_id = ? AND valid_to IS NULL",
            [scope, new.predicate, new.object_value, new.subject_id],
        )
        return [
            self._make(
                ContradictionKind.CAUSAL,
                scope,
                self._to_row(r),
                new,
                f"反向因果: ({new.subject_id} {new.predicate} {new.object_value}) "
                f"与已有反向边冲突",
            )
            for r in await cursor.fetchall()
        ]

    # ==================== 工具 ====================

    @staticmethod
    def _to_row(row) -> _TripleRow:
        return _TripleRow(
            id=str(row["id"]),
            subject_id=str(row["subject_id"]),
            predicate=str(row["predicate"]),
            object_value=str(row["object_id"] or row["object_value"]),
            relation_type=str(row["relation_type"]),
            valid_from=from_iso(row["valid_from"]),
            valid_to=from_iso(row["valid_to"]) if row["valid_to"] else None,
        )

    @staticmethod
    def _make(
        kind: ContradictionKind,
        scope: str,
        existing: _TripleRow,
        new: _TripleRow,
        message: str,
    ) -> Contradiction:
        return Contradiction(
            kind=kind,
            conflict_type=_KIND_TO_CONFLICT_TYPE[kind],
            scope=scope,
            subject_id=existing.subject_id,
            predicate=existing.predicate,
            existing_triple_id=existing.id,
            new_triple_id=new.id,
            existing_object=existing.object_value,
            new_object=new.object_value,
            message=message,
        )


def _overlaps(a: _TripleRow, b: _TripleRow) -> bool:
    """半开区间 [valid_from, valid_to) 重叠检测(None = 开放端)."""
    a_end = a.valid_to or _FAR_FUTURE
    b_end = b.valid_to or _FAR_FUTURE
    return a.valid_from < b_end and b.valid_from < a_end


def _extract_number(value: str) -> float | None:
    """从取值文本提取首个数字("3 个人" → 3.0);无数字返回 None."""
    match = _NUMBER_RE.search(value)
    return float(match.group()) if match else None


__all__ = [
    "CAUSE_PREDICATES",
    "DEFAULT_ENUM_PREDICATES",
    "SINGLE_VALUED_RELATION_TYPES",
    "Contradiction",
    "ContradictionDetector",
    "ContradictionKind",
    "ContradictionReport",
]
