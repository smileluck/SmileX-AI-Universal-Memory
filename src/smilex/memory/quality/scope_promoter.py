"""跨项目知识提升 — Layer 5 知识质量(主文档 §11.3 / 06-layer5 §3.4).

规则版(零 LLM / 零向量):HashEmbedder 无语义相似度,共现判定不用向量,
一律用确定性归一化键(见下),保证嵌入式环境零依赖可跑.

共现判定(§11.3 detect_cross_project_patterns 的规则版):
- 模式键 = (subject_key, predicate, object_key)
- subject_key: 主体实体的归一化 entity_id(如 "tech:redis");
  无实体行时回退原始 subject_id 字符串
- object_key: object_id 引用实体时取其归一化 entity_id;
  字面值 object_value 归一化(去首尾空白 + 折叠内部空白 + 小写)
  后加 "value:" 前缀,与实体引用区分
- 同一模式键在 >= min_projects(默认 3,§14.5 退出标准)个不同
  project scope 的现行三元组(valid_to IS NULL)中出现 → 可提升
- 状态跟踪类 relation_type(project_state/task_status/config,§6.3)
  默认不参与:它们是「项目当前状态」而非可共享知识,提升会污染 global

提升动作(promote_to_global 的规则版):
- 在 global scope 写一份副本三元组;主体/客体实体在 global 缺失时
  按 entity_id 去重补建(新 ULID,source_closet 回指源实体)
- 溯源: 提升三元组的 source_closet 回指代表性源三元组 id(取 confidence
  最高者,并列取 id 最小者,确定性);完整来源 scope 列表见
  PromotionReport / CrossProjectPattern.source_scopes(检测是确定性的,
  可由重跑 detect_patterns 复算)
- 幂等: 提升前按同一模式键检查 global 现有三元组,已存在则跳过
  (重复运行不重复提升)

偏离说明: 06-layer5 §3.4 的 MIN_SUPPORT=5 / MIN_CONFIDENCE=0.8(置信度 =
1 - 反例比例)依赖反例统计,规则版无可靠反例定义,简化为 min_projects +
min_support(默认 1)双阈值;LLM 置信度评估留待语义层接入.

调度器挂接: run(ctx, payload) 符合 InterruptibleRun 签名,可经
register_scope_promotion_task 注册为调度任务(每模式一个检查点,
cursor = 最后处理的模式键,断点续传);同步 API 为 detect_and_promote().

退出标准(§14.5): 跨 3 项目共现自动提升准确率 > 90%,
见 tests/unit/test_cross_project_promotion.py 标注集.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ...utils.ids import generate_id
from ...utils.timeutil import now_utc, to_iso

if TYPE_CHECKING:
    from ..scheduler.checkpoint import InterruptContext
    from ..scheduler.scheduler import MemoryTaskScheduler
    from ..storage.storage_engine import StorageEngine

# 调度器任务名(注册键)
TASK_SCOPE_PROMOTION = "scope_promotion"

# 默认共现项目数阈值(§11.3: >= 3 个项目)
DEFAULT_MIN_PROJECTS = 3

# 状态跟踪类 relation_type(§6.3)— 项目当前状态,默认不参与跨项目提升
STATE_RELATION_TYPES = frozenset({"project_state", "task_status", "config"})

_WS_RE = re.compile(r"\s+")


def normalize_value(value: str) -> str:
    """字面值归一化: 去首尾空白 + 内部空白折叠 + 小写(规则版相似判定)."""
    return _WS_RE.sub(" ", value.strip()).lower()


@dataclass
class CrossProjectPattern:
    """跨项目共现模式(可提升候选,§11.3 Pattern)."""

    subject_key: str  # 归一化主体键(实体 entity_id 或原始 subject_id)
    predicate: str
    object_key: str  # 归一化客体键(实体 entity_id 或 "value:{归一化字面值}")
    object_value: str | None  # 字面值(实体引用模式为 None)
    relation_type: str
    project_count: int  # 共现项目数(DISTINCT scope)
    support: int  # 总出现次数
    source_scopes: list[str] = field(default_factory=list)  # 排序后的来源 scope
    source_triple_ids: list[str] = field(default_factory=list)  # 来源三元组 id

    @property
    def key(self) -> str:
        """模式键(确定性,断点 cursor / 日志用)."""
        return f"{self.subject_key}|{self.predicate}|{self.object_key}"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "subject_key": self.subject_key,
            "predicate": self.predicate,
            "object_key": self.object_key,
            "object_value": self.object_value,
            "relation_type": self.relation_type,
            "project_count": self.project_count,
            "support": self.support,
            "source_scopes": list(self.source_scopes),
            "source_triple_ids": list(self.source_triple_ids),
        }


@dataclass
class PromotionReport:
    """一次 detect_and_promote 的提升报告(§11.3)."""

    scanned_triples: int = 0  # 参与共现统计的现行 project 三元组数
    patterns: list[CrossProjectPattern] = field(default_factory=list)  # 检出的模式
    promoted: list[CrossProjectPattern] = field(default_factory=list)  # 本次新提升
    skipped_existing: int = 0  # 已在 global 存在而跳过的模式数
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "scanned_triples": self.scanned_triples,
            "patterns": len(self.patterns),
            "promoted": len(self.promoted),
            "skipped_existing": self.skipped_existing,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class _TripleRow:
    """共现扫描加载的三元组轻量视图(含端点实体归一化键)."""

    id: str
    scope: str
    subject_id: str
    subject_key: str
    predicate: str
    object_id: str | None
    object_key: str
    object_value: str | None
    relation_type: str
    confidence: float
    certainty: str


class ScopePromoter:
    """跨项目知识提升器(§11.3 ScopePromoter 的规则版).

    Args:
        storage: Layer 1 StorageEngine(已 initialize)
        min_projects: 共现项目数阈值(默认 3,§14.5)
        min_support: 总出现次数阈值(默认 1;06-layer5 §3.4 置信度机制的简化)
        exclude_relation_types: 不参与提升的 relation_type
            (None 时默认排除状态跟踪类 STATE_RELATION_TYPES)
    """

    def __init__(
        self,
        storage: StorageEngine,
        *,
        min_projects: int = DEFAULT_MIN_PROJECTS,
        min_support: int = 1,
        exclude_relation_types: frozenset[str] | None = None,
    ) -> None:
        if min_projects < 2:
            raise ValueError(f"min_projects 必须 >= 2,得到 {min_projects}")
        self._storage = storage
        self._min_projects = min_projects
        self._min_support = min_support
        self._exclude_relation_types = (
            exclude_relation_types
            if exclude_relation_types is not None
            else STATE_RELATION_TYPES
        )

    # ==================== 共现检测 ====================

    async def detect_patterns(self) -> list[CrossProjectPattern]:
        """检测可提升的跨项目共现模式(06-layer5 §2.3 精确计数,按模式键排序).

        只统计 project scope 的现行三元组(valid_to IS NULL);
        global / tenant scope 不参与共现计数.
        """
        patterns, _ = await self._detect()
        return patterns

    async def _detect(self) -> tuple[list[CrossProjectPattern], int]:
        """共现检测主流程,返回 (模式列表, 参与统计的三元组数)."""
        rows = await self._load_project_triples()
        groups: dict[str, list[_TripleRow]] = {}
        for row in rows:
            key = f"{row.subject_key}|{row.predicate}|{row.object_key}"
            groups.setdefault(key, []).append(row)

        patterns: list[CrossProjectPattern] = []
        for members in groups.values():
            scopes = sorted({m.scope for m in members})
            if len(scopes) < self._min_projects or len(members) < self._min_support:
                continue
            first = members[0]
            patterns.append(
                CrossProjectPattern(
                    subject_key=first.subject_key,
                    predicate=first.predicate,
                    object_key=first.object_key,
                    object_value=first.object_value,
                    relation_type=first.relation_type,
                    project_count=len(scopes),
                    support=len(members),
                    source_scopes=scopes,
                    source_triple_ids=sorted(m.id for m in members),
                )
            )
        patterns.sort(key=lambda p: p.key)
        return patterns, len(rows)

    async def _load_project_triples(self) -> list[_TripleRow]:
        """加载全部 project scope 现行三元组 + 端点实体归一化键(两次 LEFT JOIN)."""
        cursor = await self._storage.conn.execute(
            "SELECT t.id, t.scope, t.subject_id, t.predicate, t.object_id, "
            "t.object_value, t.relation_type, t.confidence, t.certainty, "
            "s.entity_id AS subject_entity_id, o.entity_id AS object_entity_id "
            "FROM triples t "
            "LEFT JOIN entities s ON s.id = t.subject_id "
            "LEFT JOIN entities o ON o.id = t.object_id "
            "WHERE t.scope LIKE 'project:%' AND t.valid_to IS NULL"
        )
        rows: list[_TripleRow] = []
        for r in await cursor.fetchall():
            if r["relation_type"] in self._exclude_relation_types:
                continue
            rows.append(
                _TripleRow(
                    id=str(r["id"]),
                    scope=str(r["scope"]),
                    subject_id=str(r["subject_id"]),
                    subject_key=str(r["subject_entity_id"] or r["subject_id"]),
                    predicate=str(r["predicate"]),
                    object_id=r["object_id"],
                    object_key=(
                        str(r["object_entity_id"] or r["object_id"])
                        if r["object_id"]
                        else f"value:{normalize_value(str(r['object_value']))}"
                    ),
                    object_value=r["object_value"],
                    relation_type=str(r["relation_type"]),
                    confidence=float(r["confidence"]),
                    certainty=str(r["certainty"]),
                )
            )
        return rows

    # ==================== 提升到 global ====================

    async def detect_and_promote(self) -> PromotionReport:
        """检测共现模式并提升到 global(幂等),返回提升报告.

        同步 API;调度器挂接用 run()(同一逻辑的 InterruptibleRun 包装).
        """
        report, _ = await self._detect_and_promote()
        return report

    async def _detect_and_promote(
        self, ctx: InterruptContext | None = None
    ) -> tuple[PromotionReport, int]:
        """detect + promote 主流程;ctx 非空时每模式落一个检查点(断点续传).

        Returns:
            (report, next_step) — next_step 供调度任务继续计数
        """
        start = time.monotonic()
        patterns, scanned = await self._detect()
        report = PromotionReport(scanned_triples=scanned, patterns=patterns)

        existing_keys = await self._global_pattern_keys()
        # 断点续传: cursor = 最后处理的模式键,跳过已处理部分
        cursor = ""
        step = 0
        if ctx is not None:
            step = ctx.step
            if isinstance(ctx.cursor, str):
                cursor = ctx.cursor
        total = len(patterns)
        for i, pattern in enumerate(patterns):
            if pattern.key <= cursor:
                continue
            if pattern.key in existing_keys:
                report.skipped_existing += 1
            else:
                await self.promote_to_global(pattern)
                existing_keys.add(pattern.key)  # 同事务内后续模式可见
                report.promoted.append(pattern)
            step += 1
            if ctx is not None:
                await ctx.checkpoint(
                    step=step,
                    progress=(i + 1) / total if total else 1.0,
                    state={"stats": report.to_dict()},
                    cursor=pattern.key,
                )
        report.elapsed_ms = int((time.monotonic() - start) * 1000)
        return report, step

    async def promote_to_global(self, pattern: CrossProjectPattern) -> str:
        """把一个共现模式复制到 global scope,返回新三元组 id.

        - 主体/客体实体在 global 按 entity_id 去重补建(source_closet 回指源实体)
        - 提升三元组 source_closet 回指代表性源三元组(confidence 最高,
          并列取 id 最小,确定性)
        - 调用方负责幂等(模式键查重见 _detect_and_promote)
        """
        representative = await self._representative_source(pattern)
        subject_id = await self._ensure_global_entity(pattern.subject_key)
        object_id = None
        if pattern.object_value is None:
            object_id = await self._ensure_global_entity(pattern.object_key)

        triple_id = generate_id()
        now = to_iso(now_utc())
        await self._storage.conn.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, object_id, "
            "object_value, scope, valid_from, valid_to, predecessor_id, "
            "causal_level, confidence, certainty, relation_type, source_closet) "
            "VALUES (?, ?, ?, ?, ?, ?, 'global', ?, NULL, NULL, 0, ?, ?, ?, ?)",
            (
                triple_id,
                f"{pattern.subject_key}|{pattern.predicate}|{pattern.object_key}",
                subject_id or pattern.subject_key,
                pattern.predicate,
                object_id,
                pattern.object_value,
                now,
                representative["confidence"] if representative else 1.0,
                representative["certainty"] if representative else "exact",
                pattern.relation_type,
                representative["id"] if representative else None,
            ),
        )
        await self._storage.conn.commit()
        return triple_id

    async def _representative_source(self, pattern: CrossProjectPattern) -> dict | None:
        """代表性源三元组: confidence 最高,并列取 id 最小(确定性溯源指针)."""
        placeholders = ",".join("?" for _ in pattern.source_triple_ids)
        cursor = await self._storage.conn.execute(
            f"SELECT id, confidence, certainty FROM triples WHERE id IN ({placeholders}) "
            "ORDER BY confidence DESC, id ASC LIMIT 1",
            list(pattern.source_triple_ids),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def _ensure_global_entity(self, entity_key: str) -> str | None:
        """确保 global scope 存在 entity_id = entity_key 的实体(按 entity_id 去重).

        从任一 project 源实体复制(名称/类型/归一化 ID),新 ULID,
        source_closet 回指源实体 id;无源实体行(裸 subject_id)时返回 None.
        """
        conn = self._storage.conn
        cursor = await conn.execute(
            "SELECT id FROM entities WHERE scope = 'global' AND entity_id = ?",
            [entity_key],
        )
        row = await cursor.fetchone()
        if row is not None:
            return str(row["id"])
        cursor = await conn.execute(
            "SELECT id, entity_type, name, valid_from FROM entities "
            "WHERE entity_id = ? AND scope LIKE 'project:%' ORDER BY id LIMIT 1",
            [entity_key],
        )
        source = await cursor.fetchone()
        if source is None:
            return None
        new_id = generate_id()
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, "
            "valid_from, valid_to, embedding, source_closet) "
            "VALUES (?, ?, ?, ?, 'global', ?, NULL, NULL, ?)",
            (
                new_id,
                entity_key,
                source["entity_type"],
                source["name"],
                source["valid_from"],
                source["id"],
            ),
        )
        return new_id

    async def _global_pattern_keys(self) -> set[str]:
        """global 现有三元组的模式键集合(幂等查重,与检测同一键规则)."""
        cursor = await self._storage.conn.execute(
            "SELECT t.subject_id, t.predicate, t.object_id, t.object_value, "
            "s.entity_id AS subject_entity_id, o.entity_id AS object_entity_id "
            "FROM triples t "
            "LEFT JOIN entities s ON s.id = t.subject_id "
            "LEFT JOIN entities o ON o.id = t.object_id "
            "WHERE t.scope = 'global'"
        )
        keys: set[str] = set()
        for r in await cursor.fetchall():
            subject_key = str(r["subject_entity_id"] or r["subject_id"])
            object_key = (
                str(r["object_entity_id"] or r["object_id"])
                if r["object_id"]
                else f"value:{normalize_value(str(r['object_value']))}"
            )
            keys.add(f"{subject_key}|{r['predicate']}|{object_key}")
        return keys

    # ==================== 手动提升 ====================

    async def manual_promote(self, triple_id: str) -> str | None:
        """手动提升单条三元组到 global(§11.3 manual_promote).

        按同一模式键规则幂等: global 已存在同键三元组时返回 None.
        """
        triple = await self._storage.get_triple(triple_id)
        if triple is None:
            raise ValueError(f"三元组不存在: {triple_id}")
        cursor = await self._storage.conn.execute(
            "SELECT t.id, t.scope, t.subject_id, t.predicate, t.object_id, "
            "t.object_value, t.relation_type, t.confidence, t.certainty, "
            "s.entity_id AS subject_entity_id, o.entity_id AS object_entity_id "
            "FROM triples t "
            "LEFT JOIN entities s ON s.id = t.subject_id "
            "LEFT JOIN entities o ON o.id = t.object_id WHERE t.id = ?",
            [triple_id],
        )
        r = await cursor.fetchone()
        subject_key = str(r["subject_entity_id"] or r["subject_id"])
        object_key = (
            str(r["object_entity_id"] or r["object_id"])
            if r["object_id"]
            else f"value:{normalize_value(str(r['object_value']))}"
        )
        key = f"{subject_key}|{r['predicate']}|{object_key}"
        if key in await self._global_pattern_keys():
            return None
        pattern = CrossProjectPattern(
            subject_key=subject_key,
            predicate=str(r["predicate"]),
            object_key=object_key,
            object_value=r["object_value"],
            relation_type=str(r["relation_type"]),
            project_count=1,
            support=1,
            source_scopes=[str(r["scope"])],
            source_triple_ids=[str(r["id"])],
        )
        return await self.promote_to_global(pattern)

    # ==================== 调度器挂接 ====================

    async def run(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
        """调度任务执行函数(InterruptibleRun 签名): 检测 + 提升,断点续传.

        阈值在 ScopePromoter 构造时注入(payload 不覆盖,保持任务无状态).

        Returns:
            统计 dict(见 PromotionReport.to_dict)
        """
        report, _ = await self._detect_and_promote(ctx)
        return report.to_dict()


def register_scope_promotion_task(
    scheduler: MemoryTaskScheduler,
    storage: StorageEngine,
    *,
    promoter: ScopePromoter | None = None,
    interval_seconds: float | None = None,
) -> ScopePromoter:
    """把跨项目提升注册为调度器任务(复用 scheduler,不改框架).

    Args:
        scheduler: 已创建的 MemoryTaskScheduler
        storage: Layer 1 StorageEngine(已 initialize)
        promoter: 可选自定义 ScopePromoter(阈值/排除项)
        interval_seconds: 非 None 时追加定时触发器(默认不自动触发,
            由宿主 submit / 事件触发)

    Returns:
        ScopePromoter(便于测试直接调用同步 API)
    """
    from ..scheduler.models import TaskDefinition, TaskPriority  # 延迟导入: 避免循环

    promoter = promoter or ScopePromoter(storage)
    scheduler.register(
        TaskDefinition(
            name=TASK_SCOPE_PROMOTION,
            run=promoter.run,
            priority=TaskPriority.LOW,
            interruptible=True,
            description="跨项目共现知识提升到 global(规则版,§11.3)",
        )
    )
    if interval_seconds is not None:
        scheduler.add_time_trigger(
            TASK_SCOPE_PROMOTION, interval_seconds, priority=TaskPriority.LOW
        )
    return promoter


__all__ = [
    "DEFAULT_MIN_PROJECTS",
    "STATE_RELATION_TYPES",
    "TASK_SCOPE_PROMOTION",
    "CrossProjectPattern",
    "PromotionReport",
    "ScopePromoter",
    "normalize_value",
    "register_scope_promotion_task",
]
