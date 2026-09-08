"""Unit tests for 记忆主动优化一期: 检索反馈闭环 + 近重复合并(§ 主动优化)."""

from __future__ import annotations

import json

import pytest

from smilex.memory.contracts import RecallRequest, TripleInput, WriteRequest
from smilex.memory.models import MemoryScope, ScopeFilter
from smilex.memory.scheduler import (
    MemoryTaskScheduler,
    SchedulerConfig,
    TaskStatus,
)
from smilex.memory.scheduler.checkpoint import CheckpointStore, InterruptContext
from smilex.memory.scheduler.tasks import CoreTaskConfig, register_core_tasks
from smilex.memory.scheduler.tasks.dedup import (
    _bigrams,
    _normalize,
    jaccard,
)
from smilex.memory.storage.storage_engine import StorageEngine
from smilex.middlewares.memory import MemoryMiddleware

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


def _bare_ctx(engine) -> InterruptContext:
    return InterruptContext("task-direct", CheckpointStore(engine))


# ---------- 度量函数 ----------

def test_normalize_and_bigrams():
    assert _normalize("Hello, 世界! 2026") == "hello世界2026"
    assert _bigrams("ab") == frozenset({"ab"})
    assert _bigrams("abc") == frozenset({"ab", "bc"})
    assert _bigrams("a") == frozenset({"a"})


def test_jaccard_cases():
    a = _bigrams(_normalize("客户张三偏好暗色主题界面"))
    same = _bigrams(_normalize("客户张三偏好暗色主题界面"))
    near = _bigrams(_normalize("客户张三偏好暗色主题"))
    far = _bigrams(_normalize("服务器磁盘空间不足需要清理"))
    assert jaccard(a, same) == 1.0
    assert jaccard(a, near) > 0.5
    assert jaccard(a, far) < 0.2
    assert jaccard(frozenset(), frozenset()) == 0.0


# ---------- 检索反馈闭环(recall → access_count) ----------

async def _write_and_recall(mw: MemoryMiddleware, content: str, query: str):
    resp = await mw.write(
        WriteRequest(scope=MemoryScope.PROJECT, content=content),
        session_id="s",
        scope_id="proj_a",
    )
    recalled = await mw.recall(
        RecallRequest(query=query, scope_filter=ScopeFilter(include_project="proj_a")),
        session_id="s",
    )
    return resp, recalled


async def _access_count(mw: MemoryMiddleware, memory_id: str) -> int:
    cur = await mw.engine.conn.execute(
        "SELECT access_count, last_accessed_at FROM temporal_fragments WHERE id = ?",
        [memory_id],
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["last_accessed_at"] is not None
    return int(row["access_count"])


async def test_recall_increments_access_count(tmp_path):
    mw = MemoryMiddleware(tmp_path / "fb.db", promotion_threshold=0)
    await mw.initialize()
    try:
        resp, recalled = await _write_and_recall(mw, "暗色主题偏好测试内容", "暗色主题")
        assert recalled.sources, "召回应命中刚写入的记忆"
        assert await _access_count(mw, resp.memory_id) == 1
        # 再召回一次 → 2
        await mw.recall(
            RecallRequest(query="暗色主题", scope_filter=ScopeFilter(include_project="proj_a")),
            session_id="s",
        )
        assert await _access_count(mw, resp.memory_id) == 2
    finally:
        await mw.close()


async def test_track_access_off_keeps_recall_readonly(tmp_path):
    mw = MemoryMiddleware(tmp_path / "off.db", promotion_threshold=0, track_access=False)
    await mw.initialize()
    try:
        resp, _ = await _write_and_recall(mw, "关闭反馈的内容", "关闭反馈")
        cur = await mw.engine.conn.execute(
            "SELECT access_count, last_accessed_at FROM temporal_fragments WHERE id = ?",
            [resp.memory_id],
        )
        row = await cur.fetchone()
        assert int(row["access_count"]) == 0 and row["last_accessed_at"] is None
    finally:
        await mw.close()


async def test_recall_with_non_fragment_ids_is_harmless(tmp_path):
    """source 里混入三元组/未晋升 L0 id: 过滤后 UPDATE 落空,不报错."""
    mw = MemoryMiddleware(tmp_path / "mix.db", promotion_threshold=0)
    await mw.initialize()
    try:
        # 带 relation 的写入产生三元组 id;L0 会话内容未晋升时仅在内存
        resp = await mw.write(
            WriteRequest(
                scope=MemoryScope.PROJECT,
                content="混合 id 召回计数",
                entities=["混合"],
                relations=[
                    TripleInput(subject_name="混合", predicate="测试", object_value="值")
                ],
            ),
            session_id="s",
            scope_id="proj_a",
        )
        assert resp.status.value == "saved"
        recalled = await mw.recall(
            RecallRequest(query="混合 id", scope_filter=ScopeFilter(include_project="proj_a")),
            session_id="s",
        )
        # 不抛即通过;fragment 计数正确
        if any(r.id == resp.memory_id for r in recalled.sources):
            assert await _access_count(mw, resp.memory_id) == 1
    finally:
        await mw.close()


# ---------- Forget: 使用即续命 + 常用即升值 ----------

async def _seed(conn, fid: str, *, importance=0.5, updated_at=T0,
                access_count=0, last_accessed=None, scope=SCOPE):
    await conn.execute(
        "INSERT INTO temporal_fragments(id, fragment_id, time_start, content, "
        "entities, relations, scope, layer, importance, created_at, updated_at, "
        "access_count, last_accessed_at) "
        "VALUES (?, ?, ?, ?, '[]', '[]', ?, 'L1', ?, ?, ?, ?, ?)",
        (fid, fid, T0, f"{fid} 内容", scope, importance, T0, updated_at,
         access_count, last_accessed),
    )
    await conn.commit()


async def test_forget_access_revives_stale_memory(engine, scheduler):
    """边界年龄记忆: 无访问 → 淘汰;高访问计数(升值 ×~2.7)→ 存活;最近访问(续命)→ 存活."""
    from datetime import timedelta

    from smilex.utils.timeutil import now_utc, to_iso

    now = now_utc()
    conn = engine.conn
    # 40 天前入库、importance 0.2: 裸分 0.2*0.5^(40/30) ≈ 0.079 < 0.1
    stale_iso = to_iso(now - timedelta(days=40))
    await _seed(conn, "g-plain", importance=0.2, updated_at=stale_iso)
    # 同条件 + 50 次访问: ×(1+log10(51)) ≈ ×2.7 → ≈0.21 > 0.1 存活
    await _seed(conn, "g-hot-count", importance=0.2, updated_at=stale_iso,
                access_count=50)
    # 极旧但 5 天前刚被召回: 衰减锚点重置 → 0.5*0.5^(5/30) ≈ 0.44 存活
    await _seed(conn, "g-recent-access", importance=0.5,
                last_accessed=to_iso(now - timedelta(days=5)))
    await conn.commit()

    task_id = await scheduler.submit("forget", payload={"threshold": 0.1})
    await scheduler.wait_idle(timeout=3.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED

    cur = await conn.execute("SELECT id FROM temporal_fragments")
    remaining = {r["id"] for r in await cur.fetchall()}
    assert "g-plain" not in remaining  # 边界分无访问 → 淘汰
    assert "g-hot-count" in remaining  # 访问加成跨过阈值 → 存活
    assert "g-recent-access" in remaining  # 使用即续命 → 存活


# ---------- Dedup: 近重复合并 ----------

async def _add_dup_fragment(conn, fid: str, content: str, *, entities=(),
                            access_count=0, created_at=T0, scope=SCOPE):
    await conn.execute(
        "INSERT INTO temporal_fragments(id, fragment_id, time_start, content, "
        "entities, relations, scope, layer, importance, created_at, updated_at, "
        "access_count) VALUES (?, ?, ?, ?, ?, '[]', ?, 'L1', 0.5, ?, ?, ?)",
        (fid, fid, T0, content, json.dumps(list(entities)), scope,
         created_at, created_at, access_count),
    )
    await conn.commit()


async def test_dedup_merges_near_duplicates(engine, scheduler):
    conn = engine.conn
    base = "客户张三偏好暗色主题的界面配色方案"
    near = "客户张三偏好暗色主题的界面配色"  # 去尾 → Jaccard 仍高
    other = "服务器磁盘空间不足需要立即清理日志"  # 不相关
    # dup 更晚创建(2025-06) → 幸存者应是 base(2025-01),访问计数求和
    await _add_dup_fragment(conn, "d-base", base, entities=("e1", "e2"),
                            access_count=3, created_at="2025-01-01T00:00:00Z")
    await _add_dup_fragment(conn, "d-dup", near, entities=("e2", "e3"),
                            access_count=4, created_at="2025-06-01T00:00:00Z")
    await _add_dup_fragment(conn, "d-other", other)

    task_id = await scheduler.submit("dedup")
    await scheduler.wait_idle(timeout=5.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED

    cur = await conn.execute("SELECT id FROM temporal_fragments")
    remaining = {r["id"] for r in await cur.fetchall()}
    assert remaining == {"d-base", "d-other"}

    cur = await conn.execute(
        "SELECT access_count, entities, importance FROM temporal_fragments "
        "WHERE id = 'd-base'"
    )
    row = await cur.fetchone()
    assert int(row["access_count"]) == 7  # 3 + 4 求和
    assert json.loads(row["entities"]) == ["e1", "e2", "e3"]  # 保序并集

    # 幂等: 重跑无新合并
    await scheduler.submit("dedup")
    await scheduler.wait_idle(timeout=5.0)
    cur = await conn.execute("SELECT COUNT(*) AS c FROM temporal_fragments")
    assert int((await cur.fetchone())["c"]) == 2


async def test_dedup_respects_scope_and_min_length(engine, scheduler):
    conn = engine.conn
    content = "完全相同的近重复内容应该只在同 scope 内合并"
    await _add_dup_fragment(conn, "s-a", content, scope="project:p1")
    await _add_dup_fragment(conn, "s-b", content, scope="project:p2")  # 跨 scope
    await _add_dup_fragment(conn, "s-tiny", "很短", scope="project:p1")  # < min_length

    task_id = await scheduler.submit("dedup")
    await scheduler.wait_idle(timeout=5.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED

    cur = await conn.execute("SELECT id FROM temporal_fragments")
    remaining = {r["id"] for r in await cur.fetchall()}
    assert remaining == {"s-a", "s-b", "s-tiny"}


async def test_dedup_cleans_vectors_of_duplicate(engine, scheduler):
    """重复行带向量时,合并须先清 vector_links(FK)再删行."""
    conn = engine.conn
    base = "带向量的近重复条目甲版本内容足够长"
    near = "带向量的近重复条目甲版本内容足够"  # 去尾,高 Jaccard
    await _add_dup_fragment(conn, "v-base", base)
    await _add_dup_fragment(conn, "v-dup", near,
                            created_at="2025-06-01T00:00:00Z")
    # 手工造 vector_links(FK 指向 fragments.id)模拟真实晋升产物
    await conn.execute(
        "INSERT INTO vector_links(id, vector_db, vector_id, fragment_id, "
        "dimension, created_at) VALUES ('vl-1', 'sqlite_vec', 1, 'v-dup', 1024, ?)",
        [T0],
    )
    await conn.commit()

    task_id = await scheduler.submit("dedup")
    await scheduler.wait_idle(timeout=5.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED
    cur = await conn.execute("SELECT id FROM temporal_fragments")
    assert {r["id"] for r in await cur.fetchall()} == {"v-base"}
    cur = await conn.execute("SELECT COUNT(*) AS c FROM vector_links")
    assert int((await cur.fetchone())["c"]) == 0

