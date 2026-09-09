"""MemoryMiddleware 写入路径(M.3)— write / relations 解析 / 冲突检测.

2026-09 从 memory.py 拆出: 方法体不变,经 _WritePathMixin 组合回 MemoryMiddleware;
共享状态(_engine/_concurrency/_promotion/_l0/_extractor/_vector_store/
_current_scope_id 与 _require_initialized)由宿主类提供.
"""

from __future__ import annotations

from dataclasses import replace

from ..memory.concurrency import ConcurrencyController
from ..memory.contracts import (
    ConflictInfo,
    WriteRequest,
    WriteResponse,
    WriteStatus,
)
from ..memory.extractor import PassThroughExtractor
from ..memory.models import (
    Entity,
    FuzzyMemory,
    LockType,
    MemoryLayer,
    MemoryScope,
    Triple,
)
from ..memory.observability import get_logger
from ..memory.pii import NoopPIIMasker, PIIMasker
from ..memory.storage.storage_engine import StorageEngine, scope_path
from ..utils.timeutil import to_iso

_logger = get_logger("write")

# 会话写满阈值: 单会话 L0 记忆数达到即 emit memory_full
# (调度器映射 memory_full → consolidate;阈值远低于 LRU maxsize=1000,
# 让整合在会话进行中就有机会跑,而不是等淘汰)
_L0_SESSION_FULL_THRESHOLD = 100


def _apply_pii(request: WriteRequest, masker: PIIMasker) -> WriteRequest:
    """写入前 PII 脱敏(§15.4): content/entities 名/三元组自由文本.

    NoopPIIMasker 直通时原样返回(零开销);脱敏幂等 — 替换产物不再
    匹配任何模式,重复调用结果不变。
    """
    if isinstance(masker, NoopPIIMasker):
        return request
    relations = [
        replace(
            rel,
            subject_name=masker.mask(rel.subject_name) if rel.subject_name else None,
            object_value=masker.mask(rel.object_value) if rel.object_value else None,
        )
        for rel in request.relations
    ]
    return replace(
        request,
        content=masker.mask(request.content),
        entities=[masker.mask(e) for e in request.entities],
        relations=relations,
    )


class _RelationConflictError(Exception):
    """relation 写入被 Layer 4 冲突策略拒绝(write 内部捕获 → CONFLICT 响应)."""

    def __init__(self, info: ConflictInfo) -> None:
        self.info = info
        super().__init__(info.message or "relation 写入冲突")


class _WritePathMixin:
    """M.3 写入路径(组合进 MemoryMiddleware)."""

    # 宿主类(MemoryMiddleware)提供的共享状态注解(存量 mixin 模式,
    # 此处仅为类型可见性;赋值在宿主 __init__)
    _engine: StorageEngine
    # 宿主另提供 _emit_event(event_type, payload|None) 方法(事件转发)

    # ==================== M.3 写入 ====================

    async def write(
        self,
        request: WriteRequest,
        *,
        session_id: str,
        scope_id: str | None = None,
        chain_id: str | None = None,
        detect_conflicts: bool = True,
    ) -> WriteResponse:
        """写入记忆事件(§3.2): FuzzyMemory → L0(超阈值自动晋升 L1)+ relations.

        Args:
            request: 写入请求(content/entities/relations/...)
            session_id: L0 工作记忆会话 ID
            scope_id: 项目/租户 ID;None 时用最近一次 initialize_project 的项目
            chain_id: 任务链标识(Layer 4 锁持有者/冲突归因;None 时用 session_id)
            detect_conflicts: False 跳过 Layer 4 冲突检测(测试/迁移用)

        Returns:
            WriteResponse: 正常为 SAVED;冲突且策略为拒绝/人工时返回 CONFLICT
            + ConflictInfo,L0 记忆不落库(同一 request 内冲突 relation 之前
            的 relations 已写入者保留,之后的不再写入)

        Raises:
            LockTimeoutError: 并发写入同一实体等待锁超时(§10 死锁预防兜底)

        并发控制(P1/Layer 4,§10): relations 涉及的实体按 ID 排序加
        EXCLUSIVE 锁(锁排序防死锁),序列化多任务链对同一实体的并发写;
        每条 triple 写前经 ConflictDetector 检测 WRITE_WRITE,默认策略
        AUTO_LAST(LWW)放行,拒绝/人工策略返回 CONFLICT。
        """
        self._require_initialized()
        request = _apply_pii(request, self._pii)
        scope_id = scope_id or self._current_scope_id
        scope_str = scope_path(request.scope, scope_id)  # 提前校验 PROJECT/TENANT 必须有 scope_id
        holder = chain_id or session_id

        # 0. Layer 4: 按 relations 涉及的实体加锁(锁排序防死锁)+ 冲突检测
        lock_resources: list[str] = []
        if detect_conflicts:
            lock_resources = sorted(
                {
                    f"entity:{scope_str}:{rel.subject_id or rel.subject_name}"
                    for rel in request.relations
                    if rel.subject_id or rel.subject_name
                }
            )
        handles = await self._concurrency.locks.acquire_many(
            lock_resources, LockType.EXCLUSIVE, holder=holder
        )
        # 1. relations: TripleInput 解析(名称 → 实体)+ 冲突检测 + 写入存储层(L2)
        try:
            triple_ids = await self._write_relations(
                request, scope_id=scope_id, holder=holder, detect=detect_conflicts
            )
        except _RelationConflictError as exc:
            return WriteResponse(
                memory_id="",
                status=WriteStatus.CONFLICT,
                conflict=exc.info,
            )
        finally:
            for handle in handles:
                await handle.release()

        # 2. 事实抽取 → 每条事实一个 FuzzyMemory → L0(超阈值自动晋升 L1 + 向量)
        #    PassThrough(默认)下 facts == [request.content],行为与历史一致
        try:
            facts = await self._extractor.extract(request.content)
        except Exception as exc:  # noqa: BLE001 — 抽取失败降级回原文,不阻断写入
            _logger.warning("fact_extractor_failed", error=str(exc), fallback="content")
            facts = [request.content]
        if not facts:  # 防御: 抽取器返回空列表时退回原文
            facts = [request.content]
        bypass = self._facts_bypass_l0 and not isinstance(
            self._extractor, PassThroughExtractor
        )
        conn = self._engine.conn
        primary_id = ""
        promoted_any = False
        try:
            for fact in facts:
                memory = FuzzyMemory(
                    content=fact,
                    time_range=request.time_range,
                    location=request.location,
                    entities=list(request.entities),
                    relations=triple_ids if not primary_id else [],
                    scope=request.scope,
                    scope_id=scope_id,
                    importance=request.importance,
                    expires_at=request.expires_at,
                )
                if not primary_id:
                    primary_id = memory.id
                await self._promotion.put(
                    conn, session_id, memory, scope_id=scope_id,
                    auto_promote=not bypass,
                )
                if bypass:
                    # 抽取产出的事实(通常短于阈值)直送 L1,保证可检索
                    await self._promotion.promote(
                        conn, session_id, memory.id, scope_id=scope_id
                    )
                    promoted_any = True
                elif self._l0.get(session_id, memory.id) is None:
                    promoted_any = True
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        # 会话写满信号(调度器映射 memory_full → consolidate;_enqueue 同名
        # 去重,重复 emit 无害)
        if len(self._l0.list(session_id)) >= _L0_SESSION_FULL_THRESHOLD:
            self._emit_event("memory_full", {"session_id": session_id})

        layers = [MemoryLayer.L1_SHORT if promoted_any else MemoryLayer.L0_WORKING]
        if triple_ids:
            layers.append(MemoryLayer.L2_LONG)
        return WriteResponse(
            memory_id=primary_id or memory.id,
            status=WriteStatus.SAVED,
            layers_affected=layers,
        )

    async def _write_relations(
        self,
        request: WriteRequest,
        *,
        scope_id: str | None,
        holder: str,
        detect: bool,
    ) -> list[str]:
        """TripleInput 列表 → Triple 写入存储层,返回 triple.id 列表.

        detect 开启时每条 triple 写前经 Layer 4 检测 WRITE_WRITE(§10.3):
        自动解决(LWW)放行;拒绝/人工策略抛 _RelationConflictError 中止整笔写入。
        """
        scope_str = scope_path(request.scope, scope_id)
        # 批量解析名称引用的实体(H1): 一次 IN 查询代替逐条 SELECT
        name_map = await self._resolve_entity_names(request, scope_id=scope_id)
        triples: list[Triple] = []
        # superseded 显式化(§ 主动优化二期): LWW 覆盖时旧行闭合 + 新行记
        # predecessor_id,消除"两行都当前有效"的静默矛盾
        superseded: dict[str, str] = {}  # 旧 triple id → 闭合时刻(新行 valid_from)
        for rel in request.relations:
            subject_id = rel.subject_id or name_map[rel.subject_name or ""]
            predecessor_id: str | None = None
            if detect:
                guard = await self._concurrency.guard_triple_write(
                    self._engine.conn,
                    scope=scope_str,
                    subject_id=subject_id,
                    predicate=rel.predicate,
                    object_id=rel.object_id,
                    object_value=rel.object_value,
                    chain_id=holder,
                )
                if not ConcurrencyController.should_proceed(guard):
                    assert guard is not None
                    raise _RelationConflictError(
                        ConflictInfo(
                            conflict_type=guard.conflict.conflict_type,
                            conflicting_memory_id=guard.conflict.conflicting_memory_id,
                            message=guard.resolution.message,
                        )
                    )
                if (
                    guard is not None
                    and guard.resolution.action == "overwrite"
                    and guard.conflict.conflicting_memory_id
                ):
                    predecessor_id = guard.conflict.conflicting_memory_id
            # 矛盾检测(§11.2 写入时同步检测,2026-09 接线): LWW 只会静默
            # 覆盖同键旧行,四维检测(VALUE/NUMERIC/TEMPORAL/CAUSAL)在覆盖
            # 前留痕 — 数量超容差/时态重叠/反向因果这类"新值在否定旧值"
            # 的事件值得被看见;只告警不阻断(解决仍走 Layer 4 策略)
            if detect and (rel.object_value or rel.object_id):
                found = await self._detect_contradictions(
                    scope_str, subject_id, rel
                )
                if found:
                    _logger.warning(
                        "contradiction_detected",
                        scope=scope_str,
                        subject_id=subject_id,
                        predicate=rel.predicate,
                        kinds=[str(c.kind) for c in found],
                    )
            new_triple = Triple(
                triple_id=(
                    f"{subject_id}|{rel.predicate}|"
                    f"{rel.object_id or rel.object_value}"
                ),
                subject_id=subject_id,
                predicate=rel.predicate,
                object_id=rel.object_id,
                object_value=rel.object_value,
                scope=request.scope,
                certainty=rel.certainty,
                predecessor_id=predecessor_id,
            )
            if predecessor_id is not None:
                # 沿用 008 触发器约定: 旧行 valid_to = 新行 valid_from
                superseded[predecessor_id] = to_iso(new_triple.valid_from)
            triples.append(new_triple)
        if superseded:
            # 与批量 INSERT 同一隐式事务,随 write_triples 的 commit 原子落库;
            # 每行闭合时刻对齐其继任者的 valid_from;AND valid_to IS NULL
            # 防御并发下已被闭合的行
            await self._engine.conn.executemany(
                "UPDATE triples SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
                [(ts, old_id) for old_id, ts in superseded.items()],
            )
        # 单事务批量写入(H1): N 条 triple 只刷一次 WAL
        await self._engine.write_triples(triples, scope_id=scope_id)
        return [t.id for t in triples]

    async def _detect_contradictions(self, scope_str: str, subject_id: str, rel) -> list:
        """写入时矛盾检测(§11.2 同步轻量版),返回矛盾列表(可空).

        检测器懒构造并缓存在实例上;检测自身失败只降级不阻断写入
        (告警链路不能比写入更脆)。
        """
        detector = getattr(self, "_contradiction_detector", None)
        if detector is None:
            from ..memory.quality.contradiction import (  # 延迟导入: 按需
                ContradictionDetector,
            )

            detector = ContradictionDetector(self._engine)
            self._contradiction_detector = detector
        try:
            return await detector.check_new(
                scope=scope_str,
                subject_id=subject_id,
                predicate=rel.predicate,
                object_id=rel.object_id,
                object_value=rel.object_value,
            )
        except Exception as exc:  # noqa: BLE001 — 检测失败不阻断写入
            _logger.warning("contradiction_check_failed", error=str(exc)[:200])
            return []

    async def _resolve_entity_names(
        self,
        request: WriteRequest,
        *,
        scope_id: str | None,
    ) -> dict[str, str]:
        """批量解析 relations 中的 subject_name → entities.id(H1).

        一次 `name IN (...)` 查出已存在实体;缺失的名称走 _resolve_entity_ref
        的 get-or-create 路径补 concept 实体(同名去重,整笔请求只建一次)。
        """
        names = {
            rel.subject_name
            for rel in request.relations
            if not rel.subject_id and rel.subject_name
        }
        for rel in request.relations:
            if not rel.subject_id and not rel.subject_name:
                raise ValueError("TripleInput 需要 subject_id 或 subject_name 之一")
        if not names:
            return {}
        scope_str = scope_path(request.scope, scope_id)
        placeholders = ",".join("?" for _ in names)
        cursor = await self._engine.conn.execute(
            f"SELECT id, name FROM entities WHERE scope = ? AND name IN ({placeholders})",
            [scope_str, *names],
        )
        resolved = {str(row["name"]): str(row["id"]) for row in await cursor.fetchall()}
        for name in names - resolved.keys():
            resolved[name] = await self._resolve_entity_ref(
                request.scope, scope_id, entity_id=None, name=name
            )
        return resolved

    async def _resolve_entity_ref(
        self,
        scope: MemoryScope,
        scope_id: str | None,
        *,
        entity_id: str | None,
        name: str | None,
    ) -> str:
        """TripleInput 端点解析: id 直通;名称按 scope 查找,缺失自动补 concept 实体."""
        if entity_id:
            return entity_id
        if not name:
            raise ValueError("TripleInput 需要 subject_id 或 subject_name 之一")
        from ..memory.scheduler.bootstrap.seeds import (  # 延迟导入: 同上
            make_entity_id,
        )

        scope_str = scope_path(scope, scope_id)
        conn = self._engine.conn
        cursor = await conn.execute(
            "SELECT id FROM entities WHERE scope = ? AND name = ?",
            [scope_str, name],
        )
        row = await cursor.fetchone()
        if row is not None:
            return str(row["id"])
        entity = Entity(
            entity_id=make_entity_id("concept", name),
            entity_type="concept",
            name=name,
            scope=scope,
        )
        await self._engine.write_entity(entity, scope_id=scope_id)
        await self._vector_store.add_text(conn, entity.name, entity_id=entity.id)
        await conn.commit()
        return entity.id
