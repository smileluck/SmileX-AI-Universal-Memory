"""Unit tests for P2 Layer 5 历史归档 — Archiver(§11.5/§11.6 / §14.5).

验证:
- 冷数据判定(三元组超龄 / 片段完结超龄 / 久未更新低重要度;当前状态永不归档)
- 归档动作: 热表 → 归档表迁移 + 向量清理 + predicate 字典编码
- 召回: query_archived 非向量通道;include_archived(middleware);restore 回迁
- 幂等(重复归档无副作用)/ 调度器挂接 + GRACEFUL 抢占断点续传
"""

from __future__ import annotations

import asyncio

import pytest

from smilex.memory.models import ScopeFilter
from smilex.memory.quality import (
    TASK_ARCHIVE,
    Archiver,
    RetentionPolicy,
    register_archive_task,
)
from smilex.memory.scheduler import MemoryTaskScheduler, SchedulerConfig, TaskStatus
from smilex.memory.storage.storage_engine import StorageEngine
from smilex.middlewares.dto import ProjectInitRequest, RecallRequest
from smilex.middlewares.memory import MemoryMiddleware

SCOPE = "project:proj_a"
OLD = "2024-01-01T00:00:00.000000Z"  # 远超默认 365/180 天阈值
RECENT = "2026-08-01T00:00:00.000000Z"  # 阈值内(相对当前 2026-08)


@pytest.fixture
async def engine():
    eng = StorageEngine(":memory:", load_vec=False)
    await eng.initialize()
    yield eng
    await eng.close()


async def _add_triple(
    engine: StorageEngine,
    tid: str,
    predicate: str,
    *,
    object_value: str = "v",
    scope: str = SCOPE,
    valid_from: str = OLD,
    valid_to: str | None = None,
    relation_type: str = "semantic",
) -> None:
    await engine.conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_value, "
        "scope, valid_from, valid_to, relation_type) "
        "VALUES (?, ?, 's1', ?, ?, ?, ?, ?, ?)",
        (tid, tid, predicate, object_value, scope, valid_from, valid_to, relation_type),
    )
    await engine.conn.commit()


async def _add_fragment(
    engine: StorageEngine,
    fid: str,
    content: str,
    *,
    scope: str = SCOPE,
    importance: float = 0.5,
    time_start: str = OLD,
    time_end: str | None = None,
    updated_at: str = OLD,
) -> None:
    await engine.conn.execute(
        "INSERT INTO temporal_fragments(id, fragment_id, time_start, time_end, content, "
        "entities, relations, scope, layer, importance, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '[]', '[]', ?, 'L1', ?, ?, ?)",
        (fid, fid, time_start, time_end, content, scope, importance, time_start, updated_at),
    )
    await engine.conn.commit()


async def _count(conn, table: str) -> int:
    cursor = await conn.execute(f"SELECT COUNT(*) AS c FROM {table}")
    return int((await cursor.fetchone())["c"])


# ---------- 冷数据判定(find_cold) ----------


async def test_find_cold_rules(engine):
    """冷数据判定: 超龄历史三元组 + 完结/低值超龄片段为冷;当前状态与新数据为热."""
    await _add_triple(engine, "t-cold", "uses", valid_from=OLD, valid_to="2024-06-01T00:00:00Z")
    await _add_triple(engine, "t-current", "uses", valid_from=RECENT)  # valid_to NULL 永不归档
    await _add_triple(engine, "t-recent-history", "uses",
                      valid_from=RECENT, valid_to="2026-08-10T00:00:00Z")  # 阈值内历史
    await _add_fragment(engine, "f-old-done", "已完结的旧片段", time_end="2024-06-01T00:00:00Z")
    await _add_fragment(engine, "f-old-low", "久未更新的低值片段", importance=0.1, updated_at=OLD)
    await _add_fragment(engine, "f-old-important", "久未更新的高值片段",
                        importance=0.9, updated_at=OLD)  # 高 importance 不归档
    await _add_fragment(engine, "f-recent", "新片段", time_start=RECENT, updated_at=RECENT)

    report = await Archiver(engine).find_cold()
    assert report.triples == 1  # 仅 t-cold
    assert report.fragments == 2  # f-old-done + f-old-low
    assert report.cutoff_triples and report.cutoff_fragments


async def test_find_cold_scope_filter(engine):
    """find_cold 支持 scope 过滤."""
    await _add_triple(engine, "t-a", "uses", scope="project:a",
                      valid_from=OLD, valid_to="2024-06-01T00:00:00Z")
    await _add_triple(engine, "t-b", "uses", scope="project:b",
                      valid_from=OLD, valid_to="2024-06-01T00:00:00Z")
    archiver = Archiver(engine)
    assert (await archiver.find_cold(scope="project:a")).triples == 1
    assert (await archiver.find_cold()).triples == 2


# ---------- 归档动作 ----------


async def test_archive_moves_cold_data(engine):
    """归档: 冷数据移入归档表,热表移除,当前状态保留."""
    await _add_triple(engine, "t-cold", "uses", valid_from=OLD, valid_to="2024-06-01T00:00:00Z")
    await _add_triple(engine, "t-current", "uses", valid_from=RECENT)
    await _add_fragment(engine, "f-cold", "旧片段", time_end="2024-06-01T00:00:00Z")
    await _add_fragment(engine, "f-hot", "新片段", time_start=RECENT, updated_at=RECENT)

    result = await Archiver(engine).archive_expired()
    assert result.triples_archived == 1
    assert result.fragments_archived == 1
    assert result.elapsed_ms >= 0

    conn = engine.conn
    assert await _count(conn, "triples") == 1  # t-current 保留
    assert await _count(conn, "temporal_fragments") == 1
    assert await _count(conn, "triples_archive") == 1
    assert await _count(conn, "temporal_fragments_archive") == 1

    cursor = await conn.execute("SELECT * FROM triples_archive WHERE id = 't-cold'")
    row = await cursor.fetchone()
    assert row is not None
    assert row["archived_at"] is not None
    assert row["predicate_code"] is not None  # 字典编码已补齐(§11.5)
    cursor = await conn.execute(
        "SELECT predicate FROM predicate_dict WHERE code = ?", [row["predicate_code"]]
    )
    assert (await cursor.fetchone())["predicate"] == "uses"

    cursor = await conn.execute(
        "SELECT embedding, archived_at FROM temporal_fragments_archive WHERE id = 'f-cold'"
    )
    row = await cursor.fetchone()
    assert row["embedding"] is None  # 归档不保留向量(见 archiver 模块 docstring)
    assert row["archived_at"] is not None


async def test_archive_idempotent(engine):
    """幂等: 重复归档第二次无动作."""
    await _add_triple(engine, "t-cold", "uses", valid_from=OLD, valid_to="2024-06-01T00:00:00Z")
    archiver = Archiver(engine)
    first = await archiver.archive_expired()
    second = await archiver.archive_expired()
    assert first.triples_archived == 1
    assert second.triples_archived == 0
    assert second.fragments_archived == 0
    assert await _count(engine.conn, "triples_archive") == 1


async def test_archive_drops_vectors():
    """归档清理向量: memory_vectors/vector_links 对应行删除(热向量不受影响)."""
    from smilex.memory.lifecycle.embedder import HashEmbedder
    from smilex.memory.storage.vector_store import VectorStore

    eng = StorageEngine(":memory:", load_vec=True)
    await eng.initialize()
    try:
        store = VectorStore(embedder=HashEmbedder())
        await _add_fragment(eng, "f-cold", "旧片段", time_end="2024-06-01T00:00:00Z")
        await _add_fragment(eng, "f-hot", "新片段", time_start=RECENT, updated_at=RECENT)
        async with eng._engine.transaction() as conn:
            await store.add_text(conn, "旧片段", fragment_id="f-cold")
            await store.add_text(conn, "新片段", fragment_id="f-hot")

        result = await Archiver(eng).archive_expired()
        assert result.fragments_archived == 1
        assert result.vectors_dropped == 1

        conn = eng.conn
        assert await _count(conn, "vector_links") == 1  # f-hot 的保留
        cursor = await conn.execute("SELECT COUNT(*) AS c FROM memory_vectors")
        assert int((await cursor.fetchone())["c"]) == 1
    finally:
        await eng.close()


async def test_archive_custom_policy(engine):
    """自定义保留策略: 缩短窗口使新数据也归档."""
    await _add_triple(engine, "t1", "uses", valid_from=RECENT, valid_to="2026-08-10T00:00:00Z")
    archiver = Archiver(engine)
    assert (await archiver.archive_expired()).triples_archived == 0
    policy = RetentionPolicy(triple_history_days=1)
    assert (await archiver.archive_expired(policy)).triples_archived == 1


# ---------- 召回: query_archived / restore ----------


async def test_query_archived(engine):
    """归档数据可经非向量通道召回(scope + 关键词过滤)."""
    await _add_triple(engine, "t-cold", "depends_on", object_value="redis",
                      valid_from=OLD, valid_to="2024-06-01T00:00:00Z")
    await _add_fragment(engine, "f-cold", "线上事故: 缓存雪崩导致超时",
                        time_end="2024-06-01T00:00:00Z")
    await _add_triple(engine, "t-other", "uses", object_value="kafka", scope="project:other",
                      valid_from=OLD, valid_to="2024-06-01T00:00:00Z")
    await Archiver(engine).archive_expired()

    archiver = Archiver(engine)
    rows = await archiver.query_archived(scope=SCOPE, keyword="redis")
    assert [r["id"] for r in rows] == ["t-cold"]

    rows = await archiver.query_archived(scope=SCOPE, keyword="缓存雪崩")
    assert [r["id"] for r in rows] == ["f-cold"]

    rows = await archiver.query_archived(keyword="kafka")  # 不限 scope
    assert [r["id"] for r in rows] == ["t-other"]

    with pytest.raises(ValueError, match="table"):
        await archiver.query_archived(table="bogus")


async def test_restore_fragments(engine):
    """回迁: 片段移回热表,归档表清空,幂等."""
    await _add_fragment(engine, "f-cold", "旧片段", time_end="2024-06-01T00:00:00Z")
    archiver = Archiver(engine)
    await archiver.archive_expired()
    assert await _count(engine.conn, "temporal_fragments") == 0

    result = await archiver.restore(table="fragments", ids=["f-cold"])
    assert result.fragments_restored == 1
    assert await _count(engine.conn, "temporal_fragments") == 1
    assert await _count(engine.conn, "temporal_fragments_archive") == 0

    again = await archiver.restore(table="fragments")  # 归档表已空
    assert again.fragments_restored == 0


async def test_restore_triples_keeps_current_state(engine):
    """三元组回迁不污染当前状态: §6.3 触发器临时摘除(LWW 不触发)."""
    conn = engine.conn
    # 历史状态(将归档)+ 当前状态
    await _add_triple(engine, "t-old", "status", object_value="active",
                      valid_from=OLD, valid_to="2025-01-01T00:00:00Z",
                      relation_type="project_state")
    await _add_triple(engine, "t-current", "status", object_value="completed",
                      valid_from=RECENT, relation_type="project_state")

    archiver = Archiver(engine)
    await archiver.archive_expired()
    assert await _count(conn, "triples_archive") == 1

    result = await archiver.restore(table="triples")
    assert result.triples_restored == 1
    assert await _count(conn, "triples") == 2
    assert await _count(conn, "triples_archive") == 0

    # 当前状态仍是 completed,不被回迁的历史 active 覆写
    cursor = await conn.execute(
        "SELECT object_value FROM project_current_state WHERE subject_id = 's1' "
        "AND predicate = 'status'"
    )
    assert (await cursor.fetchone())["object_value"] == "completed"
    # 现行三元组 valid_to 未被触发器改写
    cursor = await conn.execute("SELECT valid_to FROM triples WHERE id = 't-current'")
    assert (await cursor.fetchone())["valid_to"] is None
    # 触发器仍在(恢复后新写入正常触发)
    await _add_triple(engine, "t-new", "status", object_value="archived",
                      valid_from="2026-08-20T00:00:00Z", relation_type="project_state")
    cursor = await conn.execute(
        "SELECT object_value FROM project_current_state WHERE subject_id = 's1' "
        "AND predicate = 'status'"
    )
    assert (await cursor.fetchone())["object_value"] == "archived"


# ---------- 调度器挂接 + 断点续传 ----------


@pytest.fixture
async def scheduler(engine):
    sched = MemoryTaskScheduler(engine, SchedulerConfig(tick_interval=0.01, grace_timeout=0.5))
    register_archive_task(sched, engine)
    await sched.start()
    yield sched
    await sched.stop()


async def test_archive_task_via_scheduler(engine, scheduler):
    """调度器执行归档任务: submit → COMPLETED,冷数据归档."""
    await _add_triple(engine, "t-cold", "uses", valid_from=OLD, valid_to="2024-06-01T00:00:00Z")
    task_id = await scheduler.submit(TASK_ARCHIVE, payload={})
    await scheduler.wait_idle(timeout=3.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED
    assert await _count(engine.conn, "triples_archive") == 1


async def test_archive_preempt_and_resume(engine, scheduler):
    """GRACEFUL 抢占 → PAUSED + checkpoint → resume 从断点续传,全部归档."""
    for i in range(6):
        await _add_triple(engine, f"t-cold-{i}", "uses",
                          valid_from=OLD, valid_to="2024-06-01T00:00:00Z")
    task_id = await scheduler.submit(
        TASK_ARCHIVE, payload={"batch_size": 1, "step_delay": 0.05}
    )
    store = scheduler._store

    async def _cp_step() -> int:
        cp = await store.load_latest(task_id)
        return cp.step if cp else 0

    loop = asyncio.get_event_loop()
    deadline = loop.time() + 3.0
    while await _cp_step() < 2:
        assert loop.time() < deadline, "任务未产生足够检查点"
        await asyncio.sleep(0.005)

    assert await scheduler.preempt(task_id) is True
    assert scheduler.get_status(task_id).status is TaskStatus.PAUSED
    cp = await store.load_latest(task_id)
    assert cp is not None and 2 <= cp.step < 6
    archived_so_far = await _count(engine.conn, "triples_archive")
    assert 1 <= archived_so_far < 6

    assert await scheduler.resume(task_id) is True
    await scheduler.wait_idle(timeout=3.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED
    assert await _count(engine.conn, "triples_archive") == 6
    assert await _count(engine.conn, "triples") == 0
    assert await store.load_latest(task_id) is None  # 完成后检查点清理


# ---------- middleware 薄封装 ----------


@pytest.fixture
async def mw():
    m = MemoryMiddleware(":memory:")
    await m.initialize()
    yield m
    await m.close()


async def test_middleware_recall_include_archived(mw):
    """退出标准「归档数据可按需召回」: 默认只查热数据,include_archived=True 并入归档."""
    resp = await mw.initialize_project(ProjectInitRequest(name="demo"))
    _, sid = resp.scope.split(":", 1)

    # 直接落一条归档级别的旧片段(久未更新 + 低重要度)
    await _add_fragment(mw.engine, "f-cold", "归档知识: 项目使用 redis 做缓存",
                        scope=resp.scope, importance=0.1, updated_at=OLD)
    result = await mw.archive_expired()
    assert result.fragments_archived == 1

    # 默认召回: 归档数据不出现
    req = RecallRequest(
        query="redis",
        scope_filter=ScopeFilter(include_project=sid),
        entity_filter=None,
    )
    plain = await mw.recall(req)
    assert "f-cold" not in [r.id for r in plain.sources]

    # include_archived=True: 归档片段并入,带 [归档] 前缀
    with_archived = await mw.recall(req, include_archived=True)
    assert "f-cold" in [r.id for r in with_archived.sources]
    assert "[归档]" in with_archived.context
    assert "redis" in with_archived.context

    # 回迁后默认召回路径即可查到(显式 unarchive)
    restored = await mw.restore_archived(table="fragments")
    assert restored.fragments_restored == 1
    cursor = await mw.engine.conn.execute(
        "SELECT COUNT(*) AS c FROM temporal_fragments WHERE id = 'f-cold'"
    )
    assert int((await cursor.fetchone())["c"]) == 1


async def test_middleware_archived_scope_default_current_project(mw):
    """include_archived 遵循 middleware 的 scope 缺省规则(当前项目)."""
    resp = await mw.initialize_project(ProjectInitRequest(name="demo"))
    _, sid = resp.scope.split(":", 1)
    await _add_fragment(mw.engine, "f-mine", "本项目归档 kafka 经验",
                        scope=resp.scope, importance=0.1, updated_at=OLD)
    await _add_fragment(mw.engine, "f-other", "别的项目归档 kafka 经验",
                        scope="project:other", importance=0.1, updated_at=OLD)
    await mw.archive_expired()

    req = RecallRequest(query="kafka", scope_filter=ScopeFilter(include_project=sid))
    result = await mw.recall(req, include_archived=True)
    ids = [r.id for r in result.sources]
    assert "f-mine" in ids
    assert "f-other" not in ids  # 未 include_all_projects,不越权
