"""Unit tests for Layer 3c 5 类核心任务(§8.2 / §14.3 P0 退出标准).

覆盖 P0 三项退出标准:
1. 整合任务自动 L1→L2 流转(test_consolidation_l1_to_l2_flow)
2. 长任务可被抢占 + 断点续传(test_consolidation_preempt_and_resume)
3. 自适应规则触发冷却正确(test_adaptive_cooldown_exit_criterion)

另含遗忘/摘要/因果/语义任务的功能测试与默认装配测试.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from smilex.memory.scheduler import (
    CORE_TASK_NAMES,
    CoreTaskConfig,
    MemoryTaskScheduler,
    SchedulerConfig,
    TaskPriority,
    TaskStatus,
    register_core_tasks,
)
from smilex.memory.storage.storage_engine import StorageEngine

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
    """注册 5 类核心任务、关闭默认触发器的调度器(手动 submit 驱动)."""
    sched = MemoryTaskScheduler(engine, SchedulerConfig(tick_interval=0.01, grace_timeout=0.5))
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


async def _add_fragment(
    conn,
    fid: str,
    content: str,
    *,
    entities: tuple[str, ...] = (),
    scope: str = SCOPE,
    layer: str = "L1",
    importance: float = 0.5,
    time_start: str = T0,
    time_end: str | None = None,
    updated_at: str = T0,
) -> None:
    await conn.execute(
        "INSERT INTO temporal_fragments(id, fragment_id, time_start, time_end, content, "
        "entities, relations, scope, layer, importance, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)",
        (
            fid,
            fid,
            time_start,
            time_end,
            content,
            json.dumps(list(entities)),
            scope,
            layer,
            importance,
            time_start,
            updated_at,
        ),
    )
    await conn.commit()


async def _add_triple(
    conn,
    tid: str,
    subject_id: str,
    predicate: str,
    *,
    object_id: str | None = None,
    object_value: str | None = None,
    predecessor_id: str | None = None,
    confidence: float = 1.0,
    scope: str = SCOPE,
) -> None:
    await conn.execute(
        "INSERT INTO triples(id, triple_id, subject_id, predicate, object_id, object_value, "
        "scope, valid_from, predecessor_id, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, tid, subject_id, predicate, object_id, object_value, scope, T0, predecessor_id,
         confidence),
    )
    await conn.commit()


async def _add_entity(conn, eid: str, name: str, *, scope: str = SCOPE) -> None:
    await conn.execute(
        "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
        "VALUES (?, ?, 'concept', ?, ?, ?)",
        (eid, eid, name, scope, T0),
    )
    await conn.commit()


async def _count(conn, where: str, params=()) -> int:
    cur = await conn.execute(f"SELECT COUNT(*) AS c FROM temporal_fragments WHERE {where}", params)
    return int((await cur.fetchone())["c"])


# ---------- 默认装配 ----------


async def test_register_core_tasks_all_registered(scheduler):
    """5 类任务全部注册:可中断、优先级符合 §8.2(整合/因果 HIGH,遗忘 LOW)."""
    for name in CORE_TASK_NAMES:
        assert scheduler.has_task(name)
    definitions = scheduler._definitions
    assert all(definitions[n].interruptible for n in CORE_TASK_NAMES)
    assert definitions["consolidate"].priority is TaskPriority.HIGH
    assert definitions["causal"].priority is TaskPriority.HIGH
    assert definitions["forget"].priority is TaskPriority.LOW


async def test_default_triggers_registered(engine):
    """默认装配:时间/事件/自适应三重触发器按 config 注入."""
    sched = MemoryTaskScheduler(engine, SchedulerConfig(tick_interval=0.01))
    register_core_tasks(
        sched,
        engine,
        config=CoreTaskConfig(l1_pressure=lambda: False, potential_causal_links=lambda: False),
    )
    assert len(sched._time_triggers) == 5  # 整合/摘要/语义/遗忘 + db_integrity 巡检
    for event in ("memory_full", "episode_end", "session_end", "causal_inference"):
        assert sched._event_trigger.knows(event)
    assert {r.name for r in sched._adaptive_trigger.rules} == {
        "memory_pressure",
        "causal_potential",
    }


# ---------- 退出标准 1: 整合任务自动 L1→L2 流转 ----------


async def test_consolidation_l1_to_l2_flow(engine, scheduler):
    """同实体 L1 片段 ≥ min_group → 聚合为一条 L2,源片段删除(§7.3 L1→L2 流转)."""
    conn = engine.conn
    await _add_fragment(conn, "c-01", "用户偏好暗色主题", entities=("e1",))
    await _add_fragment(conn, "c-02", "用户常用 vim 键位", entities=("e1",))
    await _add_fragment(conn, "c-03", "用户周末活跃", entities=("e1",), importance=0.8)

    task_id = await scheduler.submit("consolidate")
    await scheduler.wait_idle(timeout=3.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED

    # L1 源已流转删除,L2 生成 1 条聚合记忆
    assert await _count(conn, "layer = 'L1'") == 0
    assert await _count(conn, "layer = 'L2'") == 1
    cur = await conn.execute("SELECT * FROM temporal_fragments WHERE layer = 'L2'")
    row = await cur.fetchone()
    assert row["content"].startswith("【整合】")
    assert "3 条记忆聚合" in row["content"]
    assert json.loads(row["entities"]) == ["e1"]
    assert row["importance"] == pytest.approx(0.8)  # 取组内最大
    assert row["scope"] == SCOPE


async def test_consolidation_grouping_and_min_group(engine, scheduler):
    """分组规则: 同实体聚合;不足 min_group 的片段保留在 L1;多组各成一条 L2."""
    conn = engine.conn
    await _add_fragment(conn, "g-01", "实体 A 记忆一", entities=("eA",))
    await _add_fragment(conn, "g-02", "实体 A 记忆二", entities=("eA",))
    await _add_fragment(conn, "g-03", "实体 B 记忆一", entities=("eB",))
    await _add_fragment(conn, "g-04", "实体 B 记忆二", entities=("eB",))
    await _add_fragment(conn, "g-05", "孤零零的一条", entities=("eC",))  # 不足 min_group

    task_id = await scheduler.submit("consolidate", payload={"min_group": 2})
    await scheduler.wait_idle(timeout=3.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED

    assert await _count(conn, "layer = 'L2'") == 2  # eA / eB 两组
    assert await _count(conn, "layer = 'L1'") == 1  # eC 单条保留
    cur = await conn.execute("SELECT entities FROM temporal_fragments WHERE layer = 'L1'")
    assert json.loads((await cur.fetchone())["entities"]) == ["eC"]

    # 幂等: 再跑一次不再产生新 L2(eC 仍不足 min_group)
    await scheduler.submit("consolidate", payload={"min_group": 2})
    await scheduler.wait_idle(timeout=3.0)
    assert await _count(conn, "layer = 'L2'") == 2


# ---------- 退出标准 2: 长任务抢占 + checkpoint 落库 + 断点续传 ----------


async def test_consolidation_preempt_and_resume(engine, scheduler):
    """多批整合运行中被 GRACEFUL 抢占 → PAUSED + checkpoint 落库 → resume 从断点继续,结果正确."""
    conn = engine.conn
    # 4 组 × 3 条 = 12 条 L1;batch_size=1 + step_delay 保证运行窗口足够长
    for group in ("e1", "e2", "e3", "e4"):
        for i in range(3):
            await _add_fragment(conn, f"f-{group}-{i}", f"{group} 的记忆 {i}", entities=(group,))

    task_id = await scheduler.submit(
        "consolidate", payload={"batch_size": 1, "step_delay": 0.05, "min_group": 2}
    )
    # 等到第 3 批检查点落库后发起 GRACEFUL 抢占
    store = scheduler._store

    async def _cp_step():
        cp = await store.load_latest(task_id)
        return cp.step if cp else 0

    loop = asyncio.get_event_loop()
    deadline = loop.time() + 3.0
    while await _cp_step() < 3:
        assert loop.time() < deadline, "任务未产生足够检查点"
        await asyncio.sleep(0.005)

    preempted = await scheduler.preempt(task_id)
    assert preempted is True

    task = scheduler.get_status(task_id)
    assert task.status is TaskStatus.PAUSED  # 抢占后断点已保存
    cp = await store.load_latest(task_id)
    assert cp is not None  # checkpoint 落库(checkpoints 表)
    assert 3 <= cp.step < 13
    assert cp.cursor is not None  # 续传游标 = 最后扫描的 fragment id
    scanned_at_pause = cp.state["stats"]["scanned"]
    assert 0 < scanned_at_pause < 12

    # 断点续传: 从 cursor 继续,不重复不遗漏
    assert await scheduler.resume(task_id) is True
    await scheduler.wait_idle(timeout=3.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED

    assert await _count(conn, "layer = 'L2'") == 4  # 每组恰好聚合一次
    assert await _count(conn, "layer = 'L1'") == 0
    assert await store.load_latest(task_id) is None  # 完成后检查点清理


# ---------- 退出标准 3: 自适应规则触发冷却正确 ----------


async def test_adaptive_cooldown_exit_criterion(engine):
    """条件持续为真时,两次触发间隔 ≥ cooldown,冷却内不重复触发(§8.3)."""
    cooldown = 0.4
    sched = MemoryTaskScheduler(engine, SchedulerConfig(tick_interval=0.01, grace_timeout=0.5))
    register_core_tasks(
        sched,
        engine,
        config=CoreTaskConfig(
            enable_time_triggers=False,
            enable_event_mappings=False,
            l1_pressure=lambda: True,  # 条件持续为真
            memory_pressure_cooldown=cooldown,
        ),
    )
    await sched.start()
    conn = engine.conn
    try:
        # 第一批可整合数据 → 首次自适应触发
        await _add_fragment(conn, "p-01", "压力记忆一", entities=("e1",))
        await _add_fragment(conn, "p-02", "压力记忆二", entities=("e1",))
        loop = asyncio.get_event_loop()

        async def _l2_count():
            return await _count(conn, "layer = 'L2'")

        deadline = loop.time() + 3.0
        while await _l2_count() < 1:
            assert loop.time() < deadline, "首次自适应触发未发生"
            await asyncio.sleep(0.005)
        t1 = loop.time()

        # 冷却期内补充新数据: 不应触发第二次整合
        await _add_fragment(conn, "p-03", "压力记忆三", entities=("e2",))
        await _add_fragment(conn, "p-04", "压力记忆四", entities=("e2",))
        await asyncio.sleep(0.2)  # < cooldown,仍在冷却内
        assert await _l2_count() == 1, "冷却期内重复触发了"

        # 冷却结束后第二次触发,且与首次间隔 ≥ cooldown
        deadline = loop.time() + 3.0
        while await _l2_count() < 2:
            assert loop.time() < deadline, "冷却结束后未再次触发"
            await asyncio.sleep(0.005)
        t2 = loop.time()
        assert t2 - t1 >= cooldown - 0.05  # 容忍 tick/执行耗时
    finally:
        await sched.stop()


# ---------- 遗忘任务 ----------


async def test_forgetting_rules(engine, scheduler):
    """遗忘: 过期(time_end 已过)淘汰;低保留分(importance × 时间衰减)淘汰;高分保留."""
    conn = engine.conn
    now_s = "2026-08-21T00:00:00.000000Z"  # 晚于所有测试时间
    old = "2026-01-01T00:00:00.000000Z"  # ~232 天前
    await _add_fragment(conn, "f-expired", "已过期记忆", time_end="2025-06-01T00:00:00.000000Z",
                        updated_at=now_s, importance=0.9)
    await _add_fragment(conn, "f-decayed", "陈旧低值记忆", updated_at=old, importance=0.2)
    await _add_fragment(conn, "f-hot", "新鲜重要记忆", updated_at=now_s, importance=0.9)

    task_id = await scheduler.submit("forget")
    await scheduler.wait_idle(timeout=3.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED

    cur = await conn.execute("SELECT id FROM temporal_fragments")
    remaining = {r["id"] for r in await cur.fetchall()}
    assert remaining == {"f-hot"}


async def test_forgetting_demote_mode(engine, scheduler):
    """遗忘降权模式: 低分记忆不删除,importance 降为衰减后分值."""
    conn = engine.conn
    old = "2026-02-01T00:00:00.000000Z"  # ~201 天前,score = 0.4 × 0.5^6.7 ≈ 0.004
    await _add_fragment(conn, "f-mid", "中等记忆", updated_at=old, importance=0.4)

    task_id = await scheduler.submit("forget", payload={"demote": True, "threshold": 0.1})
    await scheduler.wait_idle(timeout=3.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED

    cur = await conn.execute("SELECT importance FROM temporal_fragments WHERE id = 'f-mid'")
    row = await cur.fetchone()
    assert row is not None  # 降权而非删除
    assert row["importance"] < 0.1


# ---------- 摘要任务 ----------


async def test_summarization(engine, scheduler):
    """摘要: 长 fragment 生成「【摘要】」记忆(fragment_id 后缀幂等),短片段不动."""
    conn = engine.conn
    long_content = "第一段内容。" * 30 + "第二段内容。" * 30  # 360 字
    await _add_fragment(conn, "s-long", long_content, entities=("e1",))
    await _add_fragment(conn, "s-short", "短内容")

    payload = {"max_length": 100, "summary_length": 60}
    task_id = await scheduler.submit("summarize", payload=payload)
    await scheduler.wait_idle(timeout=3.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED

    cur = await conn.execute(
        "SELECT * FROM temporal_fragments WHERE fragment_id = 's-long:summary'"
    )
    summary = await cur.fetchone()
    assert summary is not None
    assert summary["content"].startswith("【摘要】")
    assert "原文共" in summary["content"]
    assert len(summary["content"]) < len(long_content)
    assert json.loads(summary["entities"]) == ["e1"]  # 继承源实体
    # 短片段无摘要
    assert await _count(conn, "fragment_id = 's-short:summary'") == 0

    # 幂等: 重跑不产生重复摘要
    await scheduler.submit("summarize", payload=payload)
    await scheduler.wait_idle(timeout=3.0)
    assert await _count(conn, "fragment_id = 's-long:summary'") == 1


# ---------- 因果任务 ----------


async def test_causal_chain_maintenance(engine, scheduler):
    """因果: predecessor_id 链 → causal_chains 表(chain_id 确定性,重跑幂等)."""
    conn = engine.conn
    await _add_triple(conn, "t1", "e1", "导致", object_value="构建失败", confidence=0.9)
    await _add_triple(conn, "t2", "e1", "导致", object_value="测试红灯",
                      predecessor_id="t1", confidence=0.8)
    await _add_triple(conn, "t3", "e1", "导致", object_value="发布延期",
                      predecessor_id="t2", confidence=0.6)
    await _add_triple(conn, "t4", "e2", "无关", object_value="孤立事实")  # 无链

    task_id = await scheduler.submit("causal")
    await scheduler.wait_idle(timeout=3.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED

    cur = await conn.execute("SELECT * FROM causal_chains")
    rows = await cur.fetchall()
    assert len(rows) == 1
    chain = rows[0]
    assert chain["chain_id"] == "chain:t3"  # 链尾确定性 ID
    assert json.loads(chain["node_ids"]) == ["t1", "t2", "t3"]  # 根 → 尾
    assert chain["support_count"] == 3
    assert chain["confidence"] == pytest.approx(0.6)  # 链上最小 confidence

    # 幂等: upsert by chain_id,重跑不膨胀
    await scheduler.submit("causal")
    await scheduler.wait_idle(timeout=3.0)
    cur = await conn.execute("SELECT COUNT(*) AS c FROM causal_chains")
    assert (await cur.fetchone())["c"] == 1


# ---------- 语义任务 ----------


async def test_semantic_community_cache(engine, scheduler):
    """语义: NetworkX 连通分量 → L3 社区缓存;孤立实体不成社区;重跑整批刷新不膨胀."""
    conn = engine.conn
    for eid, name in [("e1", "编译器"), ("e2", "链接器"), ("e3", "构建系统"), ("e4", "孤岛")]:
        await _add_entity(conn, eid, name)
    await _add_triple(conn, "r1", "e1", "依赖", object_id="e2")
    await _add_triple(conn, "r2", "e2", "属于", object_id="e3")

    task_id = await scheduler.submit("semantic")
    await scheduler.wait_idle(timeout=3.0)
    assert scheduler.get_status(task_id).status is TaskStatus.COMPLETED

    cur = await conn.execute("SELECT * FROM temporal_fragments WHERE layer = 'L3'")
    rows = await cur.fetchall()
    assert len(rows) == 1  # e1-e2-e3 一个社区;e4 孤立排除
    row = rows[0]
    assert row["fragment_id"].startswith("semantic:community:")
    assert row["content"].startswith("【语义社区】")
    assert "3 个实体 / 2 条关系" in row["content"]
    assert json.loads(row["entities"]) == ["e1", "e2", "e3"]

    # 幂等刷新: 重跑仍只有 1 条 L3 缓存
    await scheduler.submit("semantic")
    await scheduler.wait_idle(timeout=3.0)
    assert await _count(conn, "layer = 'L3'") == 1
