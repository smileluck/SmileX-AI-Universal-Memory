"""Unit tests for Layer 4 冲突检测 + 解决 + middleware 集成(P1-a,主文档 §10.3).

覆盖:
- ConflictDetector: VERSION_STALE / WRITE_WRITE / READ_WRITE(内存版本追踪)
- ConflictDetector: CAUSAL_CYCLE / CAUSAL_CONTRADICTION / WRITE_WRITE(DB 层)
- ConflictResolver: AUTO_MERGE(三路合并)/ AUTO_LAST / ROLLBACK / MANUAL
- ConcurrencyController.guard_triple_write 放行/拒绝分支
- P1 退出标准「多任务链并发无 lost update」:
  乐观锁版本检查 + 合并 路径,与 middleware 并发写入路径
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from smilex.memory.concurrency import (
    ConcurrencyController,
    ConflictDetector,
    ConflictResolver,
    LockTimeoutError,
    MemoryOperation,
    ResolutionStrategy,
)
from smilex.memory.models import ConflictType, LockType, MemoryScope
from smilex.memory.storage.sqlite_engine import SQLiteEngine
from smilex.middlewares import (
    MemoryMiddleware,
    TripleInput,
    WriteRequest,
    WriteStatus,
)
from smilex.utils.timeutil import now_utc

RES = "entity:global:pref"


# ---------- VERSION_STALE / WRITE_WRITE / READ_WRITE(内存) ----------

@pytest.fixture
def detector() -> ConflictDetector:
    return ConflictDetector()


def _write_op(resource: str, chain: str, value, **kwargs) -> MemoryOperation:
    return MemoryOperation("write", resource, chain, value=value, **kwargs)


async def test_version_stale_detected(detector: ConflictDetector):
    """两条链基于同一版本读-改-写,后提交者检出 VERSION_STALE."""
    op_a = _write_op(RES, "chain-a", {"v": 1}, expected_version=0)
    assert detector.check_write(op_a) is None
    assert detector.commit_write(op_a) == 1

    op_b = _write_op(RES, "chain-b", {"v": 2}, expected_version=0)
    conflict = detector.check_write(op_b)
    assert conflict is not None
    assert conflict.conflict_type is ConflictType.VERSION_STALE
    assert conflict.other_chain_id == "chain-a"
    assert conflict.old_value == {"v": 1}
    assert conflict.new_value == {"v": 2}
    assert conflict.expected_version == 0
    assert conflict.current_version == 1


async def test_version_match_no_conflict(detector: ConflictDetector):
    """expected_version 与当前一致 → 无冲突."""
    detector.commit_write(_write_op(RES, "chain-a", "x"))
    op = _write_op(RES, "chain-b", "y", expected_version=1)
    assert detector.check_write(op) is None


async def test_write_write_via_base_version(detector: ConflictDetector):
    """未做版本校验的写(base_version 落后)→ WRITE_WRITE."""
    detector.commit_write(_write_op(RES, "chain-a", "x"))
    op = _write_op(RES, "chain-b", "y", base_version=0)
    conflict = detector.check_write(op)
    assert conflict is not None
    assert conflict.conflict_type is ConflictType.WRITE_WRITE
    assert conflict.other_chain_id == "chain-a"


async def test_version_stale_takes_precedence_over_write_write(detector: ConflictDetector):
    """expected_version 与 base_version 同时落后 → 优先报 VERSION_STALE."""
    detector.commit_write(_write_op(RES, "chain-a", "x"))
    op = _write_op(RES, "chain-b", "y", expected_version=0, base_version=0)
    conflict = detector.check_write(op)
    assert conflict is not None
    assert conflict.conflict_type is ConflictType.VERSION_STALE


async def test_read_write_conflict(detector: ConflictDetector):
    """读取后其他链写入 → READ_WRITE."""
    read_at = now_utc()
    detector.record_read(RES, "reader", at=read_at)
    await asyncio.sleep(0.001)  # 保证写入时间戳晚于读取
    detector.commit_write(_write_op(RES, "writer", "new"))
    conflict = detector.check_read_write(RES, "reader", since=read_at)
    assert conflict is not None
    assert conflict.conflict_type is ConflictType.READ_WRITE
    assert conflict.other_chain_id == "writer"


async def test_read_write_own_write_not_conflict(detector: ConflictDetector):
    """读取后自己写入不算冲突."""
    read_at = now_utc()
    detector.record_read(RES, "reader", at=read_at)
    detector.commit_write(_write_op(RES, "reader", "new"))
    assert detector.check_read_write(RES, "reader", since=read_at) is None


async def test_read_write_outside_window_not_conflict():
    """窗口期(0s)之外的写入不报 READ_WRITE."""
    detector = ConflictDetector(conflict_window=0.0)
    read_at = now_utc() - timedelta(seconds=10)
    detector.commit_write(_write_op(RES, "writer", "new"))
    assert detector.check_read_write(RES, "reader", since=read_at) is None


# ---------- 因果冲突(DB 层) ----------

@pytest.fixture
async def engine():
    eng = SQLiteEngine(":memory:", load_vec=False)
    await eng.initialize()
    conn = eng.conn
    for i in (1, 2, 3):
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
            "VALUES (?, ?, 'event', ?, 'global', '2025-01-01T00:00:00.000000Z')",
            (f"e{i}", f"event:e{i}", f"Event {i}"),
        )
    # 因果链 t1 ← t2 ← t3(predecessor 方向: t1 导致 t2 导致 t3)
    for tid, pred in (("t1", None), ("t2", "t1"), ("t3", "t2")):
        await conn.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
            "scope, valid_from, predecessor_id, relation_type) "
            "VALUES (?, ?, 'e1', 'caused', ?, 'global', "
            "'2025-01-01T00:00:00.000000Z', ?, 'causal')",
            (tid, f"tri_{tid}", f"{tid} happened", pred),
        )
    # 语义 triple(同键不同取值场景用)
    await conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
        "scope, valid_from, relation_type) "
        "VALUES ('s1', 'tri_s1', 'e1', 'prefers', 'coffee', 'global', "
        "'2025-01-01T00:00:00.000000Z', 'semantic')",
    )
    # 单值 causal triple(因果矛盾「重述同一事实」场景用)
    await conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
        "scope, valid_from, relation_type) "
        "VALUES ('c1', 'tri_c1', 'e2', 'leads_to', 'X', 'global', "
        "'2025-01-01T00:00:00.000000Z', 'causal')",
    )
    await conn.commit()
    yield eng
    await eng.close()


async def test_causal_cycle_detected(engine, detector: ConflictDetector):
    """t1 ← t2 ← t3 链上,给 t1 挂前驱 t3 → 因果环."""
    conflict = await detector.check_causal_cycle(
        engine.conn, triple_id="t1", predecessor_id="t3", chain_id="chain-x"
    )
    assert conflict is not None
    assert conflict.conflict_type is ConflictType.CAUSAL_CYCLE
    assert conflict.conflicting_memory_id == "t1"


async def test_causal_self_loop_detected(engine, detector: ConflictDetector):
    """前驱是自身 → 因果自环."""
    conflict = await detector.check_causal_cycle(
        engine.conn, triple_id="t1", predecessor_id="t1", chain_id="chain-x"
    )
    assert conflict is not None
    assert conflict.conflict_type is ConflictType.CAUSAL_CYCLE


async def test_causal_no_cycle(engine, detector: ConflictDetector):
    """延长链(新 triple 挂 t3 为前驱)不成环."""
    assert (
        await detector.check_causal_cycle(
            engine.conn, triple_id="t4", predecessor_id="t3", chain_id="chain-x"
        )
        is None
    )
    # 无前驱(None)直接放行
    assert (
        await detector.check_causal_cycle(
            engine.conn, triple_id="t5", predecessor_id=None, chain_id="chain-x"
        )
        is None
    )


async def test_causal_contradiction_detected(engine, detector: ConflictDetector):
    """causal 同键现行 triple 取值冲突 → CAUSAL_CONTRADICTION."""
    conflict = await detector.check_causal_contradiction(
        engine.conn,
        scope="global",
        subject_id="e1",
        predicate="caused",
        object_id=None,
        object_value="t1 happened",  # 与现行 t3("t3 happened")冲突
        chain_id="chain-x",
    )
    assert conflict is not None
    assert conflict.conflict_type is ConflictType.CAUSAL_CONTRADICTION
    assert conflict.conflicting_memory_id in {"t1", "t2", "t3"}


async def test_causal_contradiction_same_value_ok(engine, detector: ConflictDetector):
    """重述同一因果事实(取值相同)不算矛盾."""
    assert (
        await detector.check_causal_contradiction(
            engine.conn,
            scope="global",
            subject_id="e2",
            predicate="leads_to",
            object_id=None,
            object_value="X",
            chain_id="chain-x",
        )
        is None
    )


async def test_causal_contradiction_ignores_semantic(engine, detector: ConflictDetector):
    """非 causal 类型的同键取值差异不算因果矛盾."""
    assert (
        await detector.check_causal_contradiction(
            engine.conn,
            scope="global",
            subject_id="e1",
            predicate="prefers",
            object_id=None,
            object_value="tea",
            chain_id="chain-x",
        )
        is None
    )


async def test_triple_write_write_conflict(engine, detector: ConflictDetector):
    """同 (scope, subject, predicate) 现行取值不同 → WRITE_WRITE."""
    conflict = await detector.check_triple_write(
        engine.conn,
        scope="global",
        subject_id="e1",
        predicate="prefers",
        object_id=None,
        object_value="tea",
        chain_id="chain-x",
    )
    assert conflict is not None
    assert conflict.conflict_type is ConflictType.WRITE_WRITE
    assert conflict.conflicting_memory_id == "s1"
    assert conflict.old_value == "coffee"
    assert conflict.new_value == "tea"


async def test_triple_write_same_value_no_conflict(engine, detector: ConflictDetector):
    """重复断言同一事实 → 无冲突."""
    assert (
        await detector.check_triple_write(
            engine.conn,
            scope="global",
            subject_id="e1",
            predicate="prefers",
            object_id=None,
            object_value="coffee",
            chain_id="chain-x",
        )
        is None
    )


# ---------- ConflictResolver ----------

@pytest.fixture
def resolver() -> ConflictResolver:
    return ConflictResolver()


def test_default_strategies(resolver: ConflictResolver):
    """主文档 §10.3 默认策略表(VERSION_STALE 用可合并 dict,避免回退 LWW)."""
    expectations = {
        ConflictType.VERSION_STALE: ResolutionStrategy.AUTO_MERGE,
        ConflictType.WRITE_WRITE: ResolutionStrategy.AUTO_LAST,
        ConflictType.READ_WRITE: ResolutionStrategy.AUTO_LAST,
        ConflictType.CAUSAL_CYCLE: ResolutionStrategy.ROLLBACK,
        ConflictType.CAUSAL_CONTRADICTION: ResolutionStrategy.MANUAL,
    }
    for ctype, strategy in expectations.items():
        resolution = resolver.resolve(
            _conflict(ctype, old={"x": 1}, new={"x": 2}, base={"x": 1})
        )
        assert resolution.strategy is strategy


def _conflict(ctype: ConflictType, *, old, new, base=None):
    from smilex.memory.concurrency import Conflict

    return Conflict(
        conflict_type=ctype,
        resource_id=RES,
        chain_id="chain-b",
        other_chain_id="chain-a",
        base_value=base,
        old_value=old,
        new_value=new,
    )


def test_auto_merge_three_way(resolver: ConflictResolver):
    """三路合并: 双方各改不同字段 → 合并保留双方修改."""
    resolution = resolver.resolve(
        _conflict(
            ConflictType.VERSION_STALE,
            base={"theme": "dark", "font": "mono"},
            old={"theme": "light", "font": "mono"},  # 对方改了 theme
            new={"theme": "dark", "font": "sans"},  # 己方改了 font
        )
    )
    assert resolution.action == "merged"
    assert resolution.merged_value == {"theme": "light", "font": "sans"}
    assert resolution.requires_rewrite


def test_auto_merge_field_conflict_escalates_manual(resolver: ConflictResolver):
    """双方改同一字段且不一致 → 升级 MANUAL."""
    resolution = resolver.resolve(
        _conflict(
            ConflictType.VERSION_STALE,
            base={"theme": "dark"},
            old={"theme": "light"},
            new={"theme": "blue"},
        )
    )
    assert resolution.action == "manual"


def test_auto_merge_non_dict_falls_back_lww(resolver: ConflictResolver):
    """非 dict 值无法结构合并 → 回退 AUTO_LAST."""
    resolution = resolver.resolve(
        _conflict(ConflictType.VERSION_STALE, base="a", old="b", new="c")
    )
    assert resolution.strategy is ResolutionStrategy.AUTO_LAST
    assert resolution.action == "overwrite"
    assert resolution.winning_value == "c"


def test_auto_last_write_write(resolver: ConflictResolver):
    """WRITE_WRITE 默认 LWW: 新值获胜,放行写入."""
    resolution = resolver.resolve(
        _conflict(ConflictType.WRITE_WRITE, old="coffee", new="tea")
    )
    assert resolution.action == "overwrite"
    assert resolution.winning_value == "tea"


def test_rollback_causal_cycle(resolver: ConflictResolver):
    """CAUSAL_CYCLE 默认 ROLLBACK: 拒绝新操作."""
    resolution = resolver.resolve(
        _conflict(ConflictType.CAUSAL_CYCLE, old="t3", new="t1")
    )
    assert resolution.action == "reject"
    assert resolution.requires_rewrite


def test_manual_causal_contradiction(resolver: ConflictResolver):
    """CAUSAL_CONTRADICTION 默认 MANUAL: 不自动改写,透出上报."""
    resolution = resolver.resolve(
        _conflict(ConflictType.CAUSAL_CONTRADICTION, old="A→B", new="B→A")
    )
    assert resolution.action == "manual"


# ---------- ConcurrencyController ----------

async def test_guard_triple_write_no_conflict(engine):
    controller = ConcurrencyController()
    guard = await controller.guard_triple_write(
        engine.conn,
        scope="global",
        subject_id="e9",
        predicate="p",
        object_id=None,
        object_value="v",
        chain_id="chain-x",
    )
    assert guard is None
    assert controller.should_proceed(guard)


async def test_guard_triple_write_lww_proceeds(engine):
    """WRITE_WRITE 默认 AUTO_LAST → 放行."""
    controller = ConcurrencyController()
    guard = await controller.guard_triple_write(
        engine.conn,
        scope="global",
        subject_id="e1",
        predicate="prefers",
        object_id=None,
        object_value="tea",
        chain_id="chain-x",
    )
    assert guard is not None
    assert guard.conflict.conflict_type is ConflictType.WRITE_WRITE
    assert guard.resolution.action == "overwrite"
    assert controller.should_proceed(guard)


async def test_guard_triple_write_manual_blocks(engine):
    """策略覆盖为 MANUAL → 不放行."""
    controller = ConcurrencyController(
        strategies={ConflictType.WRITE_WRITE: ResolutionStrategy.MANUAL}
    )
    guard = await controller.guard_triple_write(
        engine.conn,
        scope="global",
        subject_id="e1",
        predicate="prefers",
        object_id=None,
        object_value="tea",
        chain_id="chain-x",
    )
    assert guard is not None
    assert not controller.should_proceed(guard)


# ---------- P1 退出标准: 多任务链并发无 lost update ----------

async def test_no_lost_update_via_version_check_and_merge(
    detector: ConflictDetector, resolver: ConflictResolver
):
    """两条并发链基于同一版本更新同一记忆: 后者检出 VERSION_STALE,
    AUTO_MERGE 三路合并保留双方更新(版本检查生效,无丢失更新).
    """
    base = {"theme": "dark", "font": "mono"}
    v0 = detector.current_version(RES)  # 两条链同时读到 v0
    assert v0 == 0

    # 链 A: 改 theme → v1
    op_a = _write_op(
        RES, "chain-a", {"theme": "light", "font": "mono"},
        base_value=base, base_version=v0, expected_version=v0,
    )
    assert detector.check_write(op_a) is None
    assert detector.commit_write(op_a) == 1

    # 链 B: 基于 v0 改 font,提交时版本过期
    op_b = _write_op(
        RES, "chain-b", {"theme": "dark", "font": "sans"},
        base_value=base, base_version=v0, expected_version=v0,
    )
    conflict = detector.check_write(op_b)
    assert conflict is not None
    assert conflict.conflict_type is ConflictType.VERSION_STALE

    # AUTO_MERGE: 双方修改都保留 → 无 lost update
    resolution = resolver.resolve(conflict)
    assert resolution.action == "merged"
    assert resolution.merged_value == {"theme": "light", "font": "sans"}

    # B 以合并值基于最新版本重写成功(base_version 失效,清掉以免误报 WRITE_WRITE)
    op_b_retry = replace(
        op_b, value=resolution.merged_value, base_version=None, expected_version=1
    )
    assert detector.check_write(op_b_retry) is None
    assert detector.commit_write(op_b_retry) == 2
    assert detector.current_version(RES) == 2


# ---------- middleware 集成 ----------

@pytest.fixture
async def mw(tmp_path):
    m = MemoryMiddleware(tmp_path / "l4.db")
    await m.initialize()
    yield m
    await m.close()


def _pref_write(value: str) -> WriteRequest:
    return WriteRequest(
        scope=MemoryScope.GLOBAL,
        content=f"Alice 偏好 {value}",
        relations=[
            TripleInput(subject_name="Alice", predicate="prefers", object_value=value)
        ],
    )


async def test_middleware_write_conflict_manual_reject(tmp_path):
    """策略 MANUAL 时,并发覆写返回 CONFLICT + ConflictInfo,第二条不落库."""
    controller = ConcurrencyController(
        strategies={ConflictType.WRITE_WRITE: ResolutionStrategy.MANUAL}
    )
    async with MemoryMiddleware(tmp_path / "x.db", concurrency=controller) as m:
        r1 = await m.write(_pref_write("coffee"), session_id="sess_a")
        assert r1.status is WriteStatus.SAVED
        first_triple_id = m.l0.get("sess_a", r1.memory_id).relations[0]

        r2 = await m.write(_pref_write("tea"), session_id="sess_b")
        assert r2.status is WriteStatus.CONFLICT
        assert r2.conflict is not None
        assert r2.conflict.conflict_type is ConflictType.WRITE_WRITE
        assert r2.conflict.conflicting_memory_id == first_triple_id

        # 第二条 triple 未落库;L0 记忆也未写入 sess_b
        cursor = await m.engine.conn.execute(
            "SELECT COUNT(*) FROM triples WHERE predicate = 'prefers'"
        )
        assert (await cursor.fetchone())[0] == 1
        assert m.l0.list("sess_b") == []


async def test_middleware_write_same_fact_no_conflict(mw: MemoryMiddleware):
    """重复写入同一事实(取值相同)不触发冲突."""
    r1 = await mw.write(_pref_write("coffee"), session_id="sess_a")
    r2 = await mw.write(_pref_write("coffee"), session_id="sess_b")
    assert r1.status is WriteStatus.SAVED
    assert r2.status is WriteStatus.SAVED


async def test_middleware_concurrent_writes_no_lost_update(mw: MemoryMiddleware):
    """P1 退出标准: 两条并发写入链(两会话)同时更新同一实体属性.

    EXCLUSIVE 锁序列化两条链;后者检出 WRITE_WRITE 由 AUTO_LAST 放行,
    两条 triple 均落库,无异常、无丢失更新。
    """
    ra, rb = await asyncio.gather(
        mw.write(_pref_write("coffee"), session_id="sess_a", chain_id="chain-a"),
        mw.write(_pref_write("tea"), session_id="sess_b", chain_id="chain-b"),
    )
    assert ra.status is WriteStatus.SAVED
    assert rb.status is WriteStatus.SAVED
    cursor = await mw.engine.conn.execute(
        "SELECT object_value FROM triples WHERE predicate = 'prefers' ORDER BY valid_from"
    )
    values = {row[0] for row in await cursor.fetchall()}
    assert values == {"coffee", "tea"}  # 双方写入都保留


async def test_middleware_write_lock_timeout(tmp_path):
    """锁被其他链持有且超时 → LockTimeoutError(死锁预防兜底)."""
    controller = ConcurrencyController(lock_timeout=0.05)
    async with MemoryMiddleware(tmp_path / "x.db", concurrency=controller) as m:
        handle = await controller.locks.acquire(
            "entity:global:Alice", LockType.EXCLUSIVE, holder="other-chain"
        )
        with pytest.raises(LockTimeoutError):
            await m.write(_pref_write("coffee"), session_id="sess_a")
        await handle.release()
        # 锁释放后写入恢复可用
        r = await m.write(_pref_write("coffee"), session_id="sess_a")
        assert r.status is WriteStatus.SAVED


async def test_middleware_write_detect_conflicts_disabled(mw: MemoryMiddleware):
    """detect_conflicts=False 跳过检测,覆写直接落库(向后兼容逃生门)."""
    r1 = await mw.write(_pref_write("coffee"), session_id="sess_a")
    r2 = await mw.write(
        _pref_write("tea"), session_id="sess_b", detect_conflicts=False
    )
    assert r1.status is WriteStatus.SAVED
    assert r2.status is WriteStatus.SAVED
