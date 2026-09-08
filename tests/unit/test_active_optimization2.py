"""Unit tests for 记忆主动优化二期: superseded 显式化 + 容量治理/删除守卫/FK 修复."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from smilex.memory.contracts import RecallRequest, TripleInput, WriteRequest
from smilex.memory.models import MemoryScope, ScopeFilter
from smilex.memory.scheduler import (
    MemoryTaskScheduler,
    SchedulerConfig,
    TaskStatus,
)
from smilex.memory.scheduler.checkpoint import CheckpointStore, InterruptContext
from smilex.memory.scheduler.tasks import (
    CoreTaskConfig,
    register_core_tasks,
)
from smilex.memory.scheduler.tasks.consolidate import consolidate
from smilex.memory.scheduler.tasks.forget import forget
from smilex.memory.storage.storage_engine import StorageEngine
from smilex.middlewares.memory import MemoryMiddleware
from smilex.utils.timeutil import now_utc, to_iso

T0 = "2025-01-01T00:00:00.000000Z"
SCOPE = "project:proj_a"


@pytest.fixture
async def engine():
    eng = StorageEngine(":memory:", load_vec=False)
    await eng.initialize()
    yield eng
    await eng.close()


@pytest.fixture
async def scheduler(engine):
    sched = MemoryTaskScheduler(
        engine, SchedulerConfig(tick_interval=0.01, grace_timeout=1.0)
    )
    register_core_tasks(
        sched,
        engine,
        config=CoreTaskConfig(
            enable_time_triggers=False,
            enable_event_mappings=False,
            enable_adaptive_rules=False,
        ),
    )
    await sched.start()
    yield sched
    await sched.stop()


def _ctx(engine) -> InterruptContext:
    return InterruptContext("task-direct", CheckpointStore(engine))


# ---------- A. superseded 显式化 ----------

async def _write_relation(mw: MemoryMiddleware, obj: str):
    return await mw.write(
        WriteRequest(
            scope=MemoryScope.PROJECT,
            content=f"配置值更新为 {obj}",
            relations=[
                TripleInput(
                    subject_name="部署环境", predicate="region", object_value=obj
                )
            ],
        ),
        session_id="s",
        scope_id="proj_a",
    )


async def test_lww_supersedes_with_bitemporal_trail(tmp_path):
    """同键不同值: 旧行闭合(valid_to=新行 valid_from),新行记 predecessor_id."""
    mw = MemoryMiddleware(tmp_path / "sup.db")
    await mw.initialize()
    try:
        await _write_relation(mw, "cn-east-1")
        await _write_relation(mw, "cn-north-2")
        conn = mw.engine.conn
        cur = await conn.execute(
            "SELECT id, valid_to, predecessor_id, object_value FROM triples "
            "ORDER BY valid_from"
        )
        rows = list(await cur.fetchall())
        assert len(rows) == 2
        old, new = rows
        assert old["object_value"] == "cn-east-1"
        # 精确断言: 旧行 valid_to == 新行 valid_from;新行 predecessor = 旧行 id
        assert old["valid_to"] is not None
        cur2 = await conn.execute(
            "SELECT old.valid_to = (SELECT valid_from FROM triples t2 "
            "WHERE t2.id = ?) AS aligned FROM triples old WHERE old.id = ?",
            [new["id"], old["id"]],
        )
        assert int((await cur2.fetchone())["aligned"]) == 1
        assert new["predecessor_id"] == old["id"]
        assert new["valid_to"] is None  # 新行当前有效

        # 第三次写入 → 链到第二行,第二行被闭合
        await _write_relation(mw, "ap-south-1")
        cur3 = await conn.execute(
            "SELECT id, predecessor_id, valid_to FROM triples "
            "ORDER BY valid_from"
        )
        chain = list(await cur3.fetchall())
        assert len(chain) == 3
        assert chain[2]["predecessor_id"] == chain[1]["id"]
        assert chain[0]["valid_to"] is not None
        assert chain[1]["valid_to"] is not None
        assert chain[2]["valid_to"] is None
    finally:
        await mw.close()


async def test_identical_reassertion_does_not_supersede(tmp_path):
    """相同值重复断言不构成冲突: 不闭合旧行、不记 predecessor."""
    mw = MemoryMiddleware(tmp_path / "same.db")
    await mw.initialize()
    try:
        await _write_relation(mw, "cn-east-1")
        await _write_relation(mw, "cn-east-1")
        conn = mw.engine.conn
        cur = await conn.execute(
            "SELECT valid_to, predecessor_id FROM triples"
        )
        rows = list(await cur.fetchall())
        assert len(rows) == 2  # 两行都保留(重复断言不覆盖)
        assert all(r["valid_to"] is None and r["predecessor_id"] is None for r in rows)
    finally:
        await mw.close()


async def test_graph_strategy_excludes_superseded(tmp_path):
    """闭合后的旧三元组不再进入召回的图策略结果(valid_to 过滤)."""
    mw = MemoryMiddleware(tmp_path / "graph.db")
    await mw.initialize()
    try:
        await _write_relation(mw, "cn-east-1")
        await _write_relation(mw, "cn-north-2")
        # entity_filter 触发 L2 图策略
        conn = mw.engine.conn
        cur = await conn.execute(
            "SELECT id FROM entities WHERE name = '部署环境'"
        )
        row = await cur.fetchone()
        assert row is not None  # triples.subject_id 存 entities 主键(ULID)
        # session_id=None 跳过 L0(旧写入的 L0 内容不含 cn-east-1 干扰),
        # 只验证 L2 图策略通道的 valid_to 过滤
        resp = await mw.recall(
            RecallRequest(
                query="部署环境 region",
                entity_filter=row["id"],
                scope_filter=ScopeFilter(include_project="proj_a"),
            ),
        )
        snippets = " ".join(r.snippet for r in resp.sources)
        assert "cn-north-2" in snippets  # 正向对照: 图策略确实命中当前值
        assert "cn-east-1" not in snippets  # 被覆盖的旧值不进上下文
    finally:
        await mw.close()


# ---------- B1. FK 修复(forget / consolidate 清向量) ----------

async def _seed_with_vector(conn, fid: str, *, importance=0.5, updated_at=T0,
                            entities=("e1",), scope=SCOPE):
    await conn.execute(
        "INSERT INTO temporal_fragments(id, fragment_id, time_start, content, "
        "entities, relations, scope, layer, importance, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '[]', ?, 'L1', ?, ?, ?)",
        (fid, fid, T0, f"{fid} 内容", json.dumps(list(entities)), scope,
         importance, T0, updated_at),
    )
    await conn.execute(
        "INSERT INTO vector_links(id, vector_db, vector_id, fragment_id, "
        "dimension, created_at) VALUES (?, 'sqlite_vec', ?, ?, 1024, ?)",
        (f"vl-{fid}", abs(hash(fid)) % 10000, fid, T0),
    )
    await conn.commit()


async def test_forget_cleans_vectors_before_delete(engine):
    """带 vector_links 的 fragment 被 forget 删除: FK 不再违约,向量被清."""
    conn = engine.conn
    await _seed_with_vector(conn, "fk-1", importance=0.05,
                            updated_at=to_iso(now_utc() - timedelta(days=400)))
    stats = await forget(engine, _ctx(engine), {"threshold": 0.5})
    assert stats["forgotten"] == 1
    cur = await conn.execute("SELECT COUNT(*) AS c FROM temporal_fragments")
    assert int((await cur.fetchone())["c"]) == 0
    cur = await conn.execute("SELECT COUNT(*) AS c FROM vector_links")
    assert int((await cur.fetchone())["c"]) == 0


async def test_consolidate_cleans_vectors_before_delete(engine):
    """consolidate 删除 L1 源行前清向量(FK)."""
    conn = engine.conn
    await _seed_with_vector(conn, "c-1", entities=("e9",))
    await _seed_with_vector(conn, "c-2", entities=("e9",))
    stats = await consolidate(engine, _ctx(engine), {"min_group": 2})
    assert stats["consolidated"] == 2
    cur = await conn.execute("SELECT COUNT(*) AS c FROM vector_links")
    assert int((await cur.fetchone())["c"]) == 0


# ---------- B2. 删除守卫 ----------

async def test_delete_guard_protects_high_importance(engine):
    """importance ≥ 0.9 的行: 极旧极低分也不删;confirm_protected=True 后才删."""
    conn = engine.conn
    ancient = to_iso(now_utc() - timedelta(days=400))
    await _seed_with_vector(conn, "p-1", importance=0.95, updated_at=ancient)
    stats = await forget(engine, _ctx(engine), {"threshold": 0.5})
    assert stats["protected"] == 1 and stats["forgotten"] == 0
    cur = await conn.execute("SELECT COUNT(*) AS c FROM temporal_fragments")
    assert int((await cur.fetchone())["c"]) == 1

    stats2 = await forget(
        engine, _ctx(engine), {"threshold": 0.5, "confirm_protected": True}
    )
    assert stats2["forgotten"] == 1
    cur = await conn.execute("SELECT COUNT(*) AS c FROM temporal_fragments")
    assert int((await cur.fetchone())["c"]) == 0


# ---------- B3. 容量治理 ----------

async def _seed_plain(conn, fid: str, *, importance=0.5, scope=SCOPE,
                      updated_at=None, content=None):
    await conn.execute(
        "INSERT INTO temporal_fragments(id, fragment_id, time_start, content, "
        "entities, relations, scope, layer, importance, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, '[]', '[]', ?, 'L1', ?, ?, ?)",
        (fid, fid, T0, content or f"{fid} 内容", scope, importance, T0,
         updated_at or to_iso(now_utc())),
    )
    await conn.commit()


async def test_capacity_pass_prunes_lowest_score(engine):
    """超配额 scope 修剪到配额: 保分最高者存活,受保护行豁免(可超配额)."""
    conn = engine.conn
    # scope-a: 4 行新鲜(衰减 pass 全部存活,threshold=0.5),其中 1 行受保护
    await _seed_plain(conn, "k-a1", importance=0.95, scope="project:a")  # 受保护
    await _seed_plain(conn, "k-a2", importance=0.8, scope="project:a")
    await _seed_plain(conn, "k-a3", importance=0.7, scope="project:a")
    await _seed_plain(conn, "k-a4", importance=0.7, scope="project:a")
    # scope-b: 1 行不受影响
    await _seed_plain(conn, "k-b1", importance=0.5, scope="project:b")

    stats = await forget(
        engine, _ctx(engine), {"max_per_scope": 2, "threshold": 0.4}
    )
    assert stats["forgotten"] == 0  # 衰减 pass 不删(全部分数 ≥ 阈值/受保护)
    assert stats["capacity_pruned"] == 2  # 超配额修剪非保护最低分两条
    cur = await conn.execute(
        "SELECT id FROM temporal_fragments WHERE scope = 'project:a'"
    )
    remaining = {r["id"] for r in await cur.fetchall()}
    assert remaining == {"k-a1", "k-a2"}
    cur = await conn.execute(
        "SELECT COUNT(*) AS c FROM temporal_fragments WHERE scope = 'project:b'"
    )
    assert int((await cur.fetchone())["c"]) == 1


async def test_core_task_config_defaults_reach_scheduled_runs(engine):
    """CoreTaskConfig 的容量/守卫默认值经 runner 注入空 payload 的调度运行."""
    conn = engine.conn
    await _seed_plain(conn, "d-1", importance=0.95, scope="project:d",
                      updated_at=to_iso(now_utc() - timedelta(days=400)))
    sched = MemoryTaskScheduler(
        engine, SchedulerConfig(tick_interval=0.01, grace_timeout=1.0)
    )
    register_core_tasks(
        sched,
        engine,
        config=CoreTaskConfig(
            enable_time_triggers=False,
            enable_event_mappings=False,
            enable_adaptive_rules=False,
            forget_protect_importance=0.9,
        ),
    )
    await sched.start()
    try:
        task_id = await sched.submit("forget", payload={"threshold": 0.5})
        await sched.wait_idle(timeout=3.0)
        assert sched.get_status(task_id).status is TaskStatus.COMPLETED
        cur = await conn.execute("SELECT COUNT(*) AS c FROM temporal_fragments")
        assert int((await cur.fetchone())["c"]) == 1  # 0.95 受保护存活
    finally:
        await sched.stop()


# ---------- B4. dedup 保护对跳过 ----------

async def test_dedup_skips_protected_pairs(engine, scheduler):
    """合并双方任一 importance ≥ 守卫门槛 → 跳过(不因相似合并高价值内容)."""
    conn = engine.conn
    base = "受保护的近重复条目内容甲版本足够长"
    near = "受保护的近重复条目内容甲版本足够"  # 高 Jaccard
    await _seed_plain(conn, "pp-1", importance=0.95, content=base)
    await _seed_plain(conn, "pp-2", importance=0.5, content=near)

    task_id = await scheduler.submit("dedup")
    await scheduler.wait_idle(timeout=5.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED
    cur = await conn.execute("SELECT COUNT(*) AS c FROM temporal_fragments")
    assert int((await cur.fetchone())["c"]) == 2  # 一方受保护 → 不合并
