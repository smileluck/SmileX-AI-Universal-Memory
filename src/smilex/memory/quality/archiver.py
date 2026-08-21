"""历史归档(冷热分层)— Layer 5 知识质量(主文档 §11.5/§11.6 / 06-layer5 §2.4/§3.5).

规则版(零 LLM / 零依赖),同库归档表方案(偏离 06-layer5 §3.5 的 ATTACH 独立归档库):
- 同库 archive 表(schema 011: triples_archive / temporal_fragments_archive,
  与热表同构 + archived_at)而非 ATTACH 外部 .db — 嵌入式单库场景下召回路径
  最简单(同连接直查),无需管理第二个文件;数据量超单库上限时再演进为分库

冷数据判定(§11.6 历史保留策略的规则版):
- 三元组: valid_to 非空且早于 triple_history_days(默认 365 天);
  当前状态三元组(valid_to IS NULL)永不归档
- 时序片段: 已完结且超龄(time_end 早于 fragment_days,默认 180 天),
  或久未更新且低重要度(updated_at 超龄 且 importance <= fragment_max_importance
  (默认 0.3,对齐 §11.5 抽样遗忘阈值))
- 实体不归档(§11.6 无实体保留期;实体体量小且被现行/归档三元组引用,
  留在热表保证召回 JOIN 可用)

归档动作(每批一个事务,幂等 + 断点续传):
1. 清理向量: 归档记录不保留向量(删除 vector_links + memory_vectors 对应行,
   也是外键约束要求) — 召回走非向量通道(时间/关键词/scope),见 query_archived;
   热数据向量保持 float32 不动,KNN 正确性不受影响.
   注: sqlite-vec vec0 按 chunk 批量存储向量(实测 1024 维约 1000 条/chunk),
   碎片化删除不释放未清空的 chunk,向量体积收益需成批归档才体现
   (见 tests/benchmarks/storage_size.py 实测说明)
2. INSERT OR IGNORE 入归档表(triples 同时经 predicate_dict 补齐 predicate_code,
   §11.5 字典编码的收益落在归档表)
3. DELETE 热表对应行
幂等: 归档后主键不在热表,重跑自然跳过;INSERT OR IGNORE 防归档表重复.
断点续传: run(ctx, payload) 符合 InterruptibleRun,每批落检查点
(cursor = 批尾 id,state.phase = triples/fragments);被抢占后 resume
从断点续扫,已归档批次因热表行已删除不会重复处理.

召回(§14.5 退出标准「归档数据可按需召回」):
- query_archived(): 直接查归档表(scope/关键词/时间过滤),recall 默认
  只查热数据,middleware recall(include_archived=True) 会并入归档结果
- restore(): 显式回迁(unarchive),把归档行移回热表后普通召回即可查到;
  三元组回迁时临时摘除 trg_update_current_state 触发器,避免历史
  project_state 记录回迁触发 LWW 污染 project_current_state 当前状态

调度器挂接: register_archive_task(scheduler, storage)(复用 P0 框架,
不改调度器);建议 IDLE/LOW 优先级 + 每日触发(§8.1 IDLE 任务表).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from ...utils.timeutil import now_utc, to_iso

if TYPE_CHECKING:
    import aiosqlite

    from ..scheduler.checkpoint import InterruptContext
    from ..scheduler.scheduler import MemoryTaskScheduler
    from ..storage.storage_engine import StorageEngine

# 调度器任务名(注册键)
TASK_ARCHIVE = "archive"

# §6.3 当前状态触发器名(三元组回迁时临时摘除,见模块 docstring)
_CURRENT_STATE_TRIGGER = "trg_update_current_state"

# 热表 → 归档表的公共列(归档表额外有 archived_at)
_TRIPLE_COLS = (
    "id, triple_id, subject_id, predicate, predicate_code, object_id, object_value, "
    "scope, valid_from, valid_to, predecessor_id, causal_level, confidence, "
    "certainty, relation_type, source_closet"
)
# fragments 归档时 embedding 置 NULL(向量不保留,见模块 docstring)
_FRAGMENT_COLS = (
    "id, fragment_id, time_start, time_end, location_id, content, "
    "entities, relations, scope, layer, importance, created_at, updated_at"
)


@dataclass
class RetentionPolicy:
    """历史保留策略(§11.6).

    Attributes:
        triple_history_days: 历史三元组保留天数(默认 365,超龄归档)
        fragment_days: 时序片段保留天数(默认 180)
        fragment_max_importance: 片段「低重要度」阈值(默认 0.3,§11.5);
            仅对「久未更新」路径生效,已完结超龄片段不受 importance 限制
    """

    triple_history_days: int = 365
    fragment_days: int = 180
    fragment_max_importance: float = 0.3

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RetentionPolicy:
        """从调度 payload 构建(缺省项用默认值)."""
        return cls(
            triple_history_days=int(payload.get("triple_history_days", 365)),
            fragment_days=int(payload.get("fragment_days", 180)),
            fragment_max_importance=float(payload.get("fragment_max_importance", 0.3)),
        )


@dataclass
class ArchiveResult:
    """一次 archive_expired 的归档报告."""

    triples_archived: int = 0
    fragments_archived: int = 0
    vectors_dropped: int = 0  # 清理的 memory_vectors 行数
    batches: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "triples_archived": self.triples_archived,
            "fragments_archived": self.fragments_archived,
            "vectors_dropped": self.vectors_dropped,
            "batches": self.batches,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class RestoreResult:
    """一次 restore 的回迁报告."""

    triples_restored: int = 0
    fragments_restored: int = 0

    def to_dict(self) -> dict:
        return {
            "triples_restored": self.triples_restored,
            "fragments_restored": self.fragments_restored,
        }


@dataclass
class ColdReport:
    """find_cold 的冷数据预览(只统计,不动数据)."""

    triples: int = 0
    fragments: int = 0
    cutoff_triples: str = ""
    cutoff_fragments: str = ""

    def to_dict(self) -> dict:
        return {
            "triples": self.triples,
            "fragments": self.fragments,
            "cutoff_triples": self.cutoff_triples,
            "cutoff_fragments": self.cutoff_fragments,
        }


class Archiver:
    """历史归档器(§11.5/§11.6 的规则版同库实现).

    Args:
        storage: Layer 1 StorageEngine(已 initialize)
        policy: 默认保留策略(run/archive_expired 可另行覆盖)
    """

    def __init__(
        self,
        storage: StorageEngine,
        *,
        policy: RetentionPolicy | None = None,
    ) -> None:
        self._storage = storage
        self._policy = policy or RetentionPolicy()

    @property
    def _conn(self) -> aiosqlite.Connection:
        return self._storage.conn

    # ==================== 冷数据判定 ====================

    def _cutoffs(self, policy: RetentionPolicy) -> tuple[str, str]:
        """(三元组 cutoff, 片段 cutoff) ISO 字符串."""
        now = now_utc()
        return (
            to_iso(now - timedelta(days=policy.triple_history_days)),
            to_iso(now - timedelta(days=policy.fragment_days)),
        )

    @staticmethod
    def _triple_cold_where(scope: str | None) -> tuple[str, list[Any]]:
        """三元组冷数据 WHERE(不含 id 游标)."""
        where = "valid_to IS NOT NULL AND valid_to < ?"
        params: list[Any] = []  # cutoff 由调用方prepend
        if scope:
            where += " AND scope = ?"
            params.append(scope)
        return where, params

    @staticmethod
    def _fragment_cold_where(scope: str | None) -> tuple[str, list[Any]]:
        """片段冷数据 WHERE: 已完结超龄 或 久未更新且低重要度."""
        where = (
            "((time_end IS NOT NULL AND time_end < ?) "
            "OR (updated_at < ? AND importance <= ?))"
        )
        params: list[Any] = []
        if scope:
            where += " AND scope = ?"
            params.append(scope)
        return where, params

    async def find_cold(
        self,
        policy: RetentionPolicy | None = None,
        *,
        scope: str | None = None,
    ) -> ColdReport:
        """统计当前符合归档条件的冷数据量(只读预览)."""
        policy = policy or self._policy
        cutoff_t, cutoff_f = self._cutoffs(policy)
        conn = self._conn

        where, extra = self._triple_cold_where(scope)
        cursor = await conn.execute(
            f"SELECT COUNT(*) AS c FROM triples WHERE {where}",
            [cutoff_t, *extra],
        )
        n_triples = int((await cursor.fetchone())["c"])

        where, extra = self._fragment_cold_where(scope)
        cursor = await conn.execute(
            f"SELECT COUNT(*) AS c FROM temporal_fragments WHERE {where}",
            [cutoff_f, cutoff_f, policy.fragment_max_importance, *extra],
        )
        n_fragments = int((await cursor.fetchone())["c"])

        return ColdReport(
            triples=n_triples,
            fragments=n_fragments,
            cutoff_triples=cutoff_t,
            cutoff_fragments=cutoff_f,
        )

    # ==================== 归档 ====================

    async def archive_expired(
        self,
        policy: RetentionPolicy | None = None,
        *,
        scope: str | None = None,
        batch_size: int = 500,
    ) -> ArchiveResult:
        """归档过期数据(同步 API,幂等),返回归档报告.

        调度器挂接用 run()(同一逻辑的 InterruptibleRun 包装,带断点续传).
        """
        report, _ = await self._archive(
            policy or self._policy, scope=scope, batch_size=batch_size
        )
        return report

    async def _archive(
        self,
        policy: RetentionPolicy,
        *,
        scope: str | None,
        batch_size: int,
        ctx: InterruptContext | None = None,
        step_delay: float = 0.0,
    ) -> tuple[ArchiveResult, int]:
        """归档主流程(三元组 → 片段两阶段),返回 (report, next_step)."""
        start = time.monotonic()
        cutoff_t, cutoff_f = self._cutoffs(policy)

        stats: dict[str, int] = {
            "triples_archived": 0,
            "fragments_archived": 0,
            "vectors_dropped": 0,
        }
        step = 0
        # 断点恢复: phase/cursor/stats 来自上一检查点
        phase = "triples"
        cursor = ""
        if ctx is not None:
            step = ctx.step
            if isinstance(ctx.cursor, str):
                cursor = ctx.cursor
            phase = str(ctx.state.get("phase", "triples"))
            stats.update({k: int(v) for k, v in ctx.state.get("stats", {}).items()})

        if phase == "triples":
            step = await self._archive_table(
                table="triples",
                id_select=(
                    "SELECT id FROM triples WHERE valid_to IS NOT NULL AND valid_to < ?"
                    + (" AND scope = ?" if scope else "")
                    + " AND id > ? ORDER BY id LIMIT ?"
                ),
                select_params=[cutoff_t, *([scope] if scope else [])],
                stats=stats,
                batch_size=batch_size,
                ctx=ctx,
                step=step,
                cursor=cursor,
                step_delay=step_delay,
            )
            cursor = ""  # 进入下一阶段,游标归零
            phase = "fragments"

        step = await self._archive_table(
            table="fragments",
            id_select=(
                "SELECT id FROM temporal_fragments WHERE "
                "((time_end IS NOT NULL AND time_end < ?) "
                "OR (updated_at < ? AND importance <= ?))"
                + (" AND scope = ?" if scope else "")
                + " AND id > ? ORDER BY id LIMIT ?"
            ),
            select_params=[
                cutoff_f,
                cutoff_f,
                policy.fragment_max_importance,
                *([scope] if scope else []),
            ],
            stats=stats,
            batch_size=batch_size,
            ctx=ctx,
            step=step,
            cursor=cursor,
            step_delay=step_delay,
        )

        report = ArchiveResult(
            triples_archived=stats["triples_archived"],
            fragments_archived=stats["fragments_archived"],
            vectors_dropped=stats["vectors_dropped"],
            batches=step,
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
        return report, step

    async def _archive_table(
        self,
        *,
        table: str,
        id_select: str,
        select_params: list[Any],
        stats: dict[str, int],
        batch_size: int,
        ctx: InterruptContext | None,
        step: int,
        cursor: str,
        step_delay: float,
    ) -> int:
        """单表分批归档循环,返回累计 step."""
        conn = self._conn
        stat_key = f"{table}_archived"
        has_vectors = await self._has_vector_table()
        while True:
            cur = await conn.execute(
                id_select, [*select_params, cursor, batch_size]
            )
            ids = [str(r["id"]) for r in await cur.fetchall()]
            if not ids:
                break
            if has_vectors:
                stats["vectors_dropped"] += await self._drop_vectors(table, ids)
            if table == "triples":
                await self._archive_triple_batch(ids)
            else:
                await self._archive_fragment_batch(ids)
            stats[stat_key] += len(ids)
            await conn.commit()
            cursor = ids[-1]
            step += 1
            if step_delay > 0:
                await asyncio.sleep(step_delay)
            if ctx is not None:
                await ctx.checkpoint(
                    step=step,
                    progress=0.0,  # 总量随归档收缩,无法预估,progress 仅作心跳
                    state={"phase": table, "stats": stats},
                    cursor=cursor,
                )
        return step

    async def _archive_triple_batch(self, ids: list[str]) -> None:
        """一批三元组: 补 predicate 字典 → 入归档表 → 删热表(同事务)."""
        conn = self._conn
        placeholders = ",".join("?" for _ in ids)
        # predicate 字典编码(§11.5): 补齐字典后归档行带 code
        await conn.execute(
            f"INSERT OR IGNORE INTO predicate_dict(predicate) "
            f"SELECT DISTINCT predicate FROM triples WHERE id IN ({placeholders})",
            ids,
        )
        await conn.execute(
            f"INSERT OR IGNORE INTO triples_archive({_TRIPLE_COLS}, archived_at) "
            f"SELECT id, triple_id, subject_id, predicate, "
            f"COALESCE(predicate_code, (SELECT code FROM predicate_dict "
            f"WHERE predicate_dict.predicate = triples.predicate)), "
            f"object_id, object_value, scope, valid_from, valid_to, predecessor_id, "
            f"causal_level, confidence, certainty, relation_type, source_closet, ? "
            f"FROM triples WHERE id IN ({placeholders})",
            [to_iso(now_utc()), *ids],
        )
        await conn.execute(
            f"DELETE FROM triples WHERE id IN ({placeholders})", ids
        )

    async def _archive_fragment_batch(self, ids: list[str]) -> None:
        """一批时序片段: 入归档表(embedding 置 NULL)→ 删热表(同事务)."""
        conn = self._conn
        placeholders = ",".join("?" for _ in ids)
        await conn.execute(
            f"INSERT OR IGNORE INTO temporal_fragments_archive"
            f"({_FRAGMENT_COLS}, embedding, archived_at) "
            f"SELECT {_FRAGMENT_COLS}, NULL, ? FROM temporal_fragments "
            f"WHERE id IN ({placeholders})",
            [to_iso(now_utc()), *ids],
        )
        await conn.execute(
            f"DELETE FROM temporal_fragments WHERE id IN ({placeholders})", ids
        )

    async def _has_vector_table(self) -> bool:
        """memory_vectors 虚拟表是否存在(load_vec=False 环境跳过向量清理)."""
        cursor = await self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'memory_vectors'"
        )
        return await cursor.fetchone() is not None

    async def _drop_vectors(self, table: str, ids: list[str]) -> int:
        """删除一批归档 id 关联的向量(vector_links + memory_vectors).

        必须在热表行删除之前执行(vector_links 有外键引用热表).
        返回删除的 memory_vectors 行数.
        """
        conn = self._conn
        column = "triple_id" if table == "triples" else "fragment_id"
        placeholders = ",".join("?" for _ in ids)
        await conn.execute(
            f"DELETE FROM memory_vectors WHERE vector_id IN ("
            f"SELECT vector_id FROM vector_links WHERE {column} IN ({placeholders}))",
            ids,
        )
        cursor = await conn.execute(
            f"DELETE FROM vector_links WHERE {column} IN ({placeholders})", ids
        )
        return int(cursor.rowcount)

    # ==================== 召回: 归档查询 ====================

    async def query_archived(
        self,
        *,
        scope: str | None = None,
        keyword: str | None = None,
        table: str = "both",
        limit: int = 50,
    ) -> list[dict]:
        """查询归档数据(非向量通道: scope + 关键词 LIKE + 时间倒序).

        recall 默认只查热数据;需要归档数据时由调用方显式使用本接口
        (middleware recall(include_archived=True) 走这里),或用 restore 回迁.

        Args:
            scope: 作用域全路径过滤(None = 全部 scope)
            keyword: 关键词(片段匹配 content,三元组匹配 predicate/object_value)
            table: "triples" / "fragments" / "both"
            limit: 每类返回条数上限

        Returns:
            dict 列表,每项带 "table"("triples"/"fragments")标记与行字段
        """
        if table not in ("triples", "fragments", "both"):
            raise ValueError(f"table 必须是 triples/fragments/both,得到 {table!r}")
        conn = self._conn
        results: list[dict] = []

        if table in ("fragments", "both"):
            where, params = "1 = 1", []
            if scope:
                where += " AND scope = ?"
                params.append(scope)
            if keyword:
                where += " AND content LIKE ?"
                params.append(f"%{keyword}%")
            cursor = await conn.execute(
                "SELECT id, fragment_id, content, scope, layer, importance, "
                "time_start, time_end, archived_at "
                f"FROM temporal_fragments_archive WHERE {where} "
                "ORDER BY time_start DESC LIMIT ?",
                [*params, limit],
            )
            for row in await cursor.fetchall():
                results.append({"table": "fragments", **dict(row)})

        if table in ("triples", "both"):
            where, params = "1 = 1", []
            if scope:
                where += " AND scope = ?"
                params.append(scope)
            if keyword:
                where += " AND (predicate LIKE ? OR object_value LIKE ?)"
                params.extend([f"%{keyword}%", f"%{keyword}%"])
            cursor = await conn.execute(
                "SELECT id, triple_id, subject_id, predicate, object_id, "
                "object_value, scope, valid_from, valid_to, archived_at "
                f"FROM triples_archive WHERE {where} "
                "ORDER BY valid_from DESC LIMIT ?",
                [*params, limit],
            )
            for row in await cursor.fetchall():
                results.append({"table": "triples", **dict(row)})

        return results

    # ==================== 召回: 回迁(unarchive) ====================

    async def restore(
        self,
        *,
        table: str = "both",
        ids: list[str] | None = None,
        scope: str | None = None,
    ) -> RestoreResult:
        """把归档数据移回热表(显式 unarchive;之后普通召回即可查到).

        幂等: 热表已存在同 id 行时 INSERT OR IGNORE 跳过,归档副本仍删除
        (restore 语义是「移动」,归档表不应残留已在热表的行).

        三元组回迁在事务内临时摘除 trg_update_current_state 触发器:
        历史 project_state 记录回迁不应触发 LWW 覆写 project_current_state.

        Args:
            table: "triples" / "fragments" / "both"
            ids: 只回迁指定 id(None = 不过滤)
            scope: 只回迁指定 scope(None = 不过滤);ids/scope 都 None 时整表回迁
        """
        if table not in ("triples", "fragments", "both"):
            raise ValueError(f"table 必须是 triples/fragments/both,得到 {table!r}")
        result = RestoreResult()
        conn = self._conn

        if table in ("triples", "both"):
            result.triples_restored = await self._restore_triples(ids=ids, scope=scope)
            await conn.commit()
        if table in ("fragments", "both"):
            result.fragments_restored = await self._restore_fragments(ids=ids, scope=scope)
            await conn.commit()
        return result

    @staticmethod
    def _restore_filter(
        ids: list[str] | None, scope: str | None
    ) -> tuple[str, list[Any]]:
        """回迁过滤条件(作用于归档表)."""
        where, params = "1 = 1", []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            where += f" AND id IN ({placeholders})"
            params.extend(ids)
        if scope:
            where += " AND scope = ?"
            params.append(scope)
        return where, params

    async def _restore_triples(
        self, *, ids: list[str] | None, scope: str | None
    ) -> int:
        """三元组回迁(临时摘除当前状态触发器,见 restore docstring)."""
        conn = self._conn
        where, params = self._restore_filter(ids, scope)

        # 保存并摘除 §6.3 触发器(避免历史状态记录回迁触发 LWW)
        trigger_sql = await self._read_trigger_sql()
        if trigger_sql is not None:
            await conn.execute(f"DROP TRIGGER IF EXISTS {_CURRENT_STATE_TRIGGER}")
        try:
            await conn.execute(
                f"INSERT OR IGNORE INTO triples({_TRIPLE_COLS}) "
                f"SELECT {_TRIPLE_COLS} FROM triples_archive WHERE {where}",
                params,
            )
            cursor = await conn.execute(
                f"DELETE FROM triples_archive WHERE {where}", params
            )
            return int(cursor.rowcount)
        finally:
            if trigger_sql is not None:
                await conn.execute(trigger_sql)

    async def _restore_fragments(
        self, *, ids: list[str] | None, scope: str | None
    ) -> int:
        """片段回迁(embedding 为 NULL — 归档时向量已清理,召回走非向量通道)."""
        conn = self._conn
        where, params = self._restore_filter(ids, scope)
        await conn.execute(
            f"INSERT OR IGNORE INTO temporal_fragments({_FRAGMENT_COLS}, embedding) "
            f"SELECT {_FRAGMENT_COLS}, NULL FROM temporal_fragments_archive "
            f"WHERE {where}",
            params,
        )
        cursor = await conn.execute(
            f"DELETE FROM temporal_fragments_archive WHERE {where}", params
        )
        return int(cursor.rowcount)

    async def _read_trigger_sql(self) -> str | None:
        """读取当前状态触发器的定义 SQL(不存在返回 None,如无 vec 的轻量环境)."""
        cursor = await self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            [_CURRENT_STATE_TRIGGER],
        )
        row = await cursor.fetchone()
        return str(row["sql"]) if row is not None else None

    # ==================== 调度器挂接 ====================

    async def run(self, ctx: InterruptContext, payload: dict[str, Any]) -> dict:
        """调度任务执行函数(InterruptibleRun 签名): 归档过期数据,断点续传.

        payload:
            scope: 只归档该 scope 全路径(None = 全部)
            batch_size: 每批条数(默认 500)
            triple_history_days / fragment_days / fragment_max_importance:
                覆盖 RetentionPolicy 默认值
            step_delay: 每批处理后的 sleep 秒数(测试/演示限速用,默认 0)

        Returns:
            统计 dict(见 ArchiveResult.to_dict)
        """
        policy = RetentionPolicy.from_payload(payload)
        report, _ = await self._archive(
            policy,
            scope=payload.get("scope"),
            batch_size=int(payload.get("batch_size", 500)),
            ctx=ctx,
            step_delay=float(payload.get("step_delay", 0.0)),
        )
        return report.to_dict()


def register_archive_task(
    scheduler: MemoryTaskScheduler,
    storage: StorageEngine,
    *,
    archiver: Archiver | None = None,
    interval_seconds: float | None = None,
) -> Archiver:
    """把历史归档注册为调度器任务(复用 scheduler,不改框架).

    Args:
        scheduler: 已创建的 MemoryTaskScheduler
        storage: Layer 1 StorageEngine(已 initialize)
        archiver: 可选自定义 Archiver(保留策略)
        interval_seconds: 非 None 时追加定时触发器(默认不自动触发,
            建议 IDLE 时段每日一次,§8.1;由宿主 submit / 事件触发)

    Returns:
        Archiver(便于测试直接调用同步 API)
    """
    from ..scheduler.models import TaskDefinition, TaskPriority  # 延迟导入: 避免循环

    archiver = archiver or Archiver(storage)
    scheduler.register(
        TaskDefinition(
            name=TASK_ARCHIVE,
            run=archiver.run,
            priority=TaskPriority.LOW,
            interruptible=True,
            description="历史归档(冷热分层,规则版,§11.5/§11.6)",
        )
    )
    if interval_seconds is not None:
        scheduler.add_time_trigger(
            TASK_ARCHIVE, interval_seconds, priority=TaskPriority.LOW
        )
    return archiver


__all__ = [
    "TASK_ARCHIVE",
    "Archiver",
    "ArchiveResult",
    "ColdReport",
    "RestoreResult",
    "RetentionPolicy",
    "register_archive_task",
]
