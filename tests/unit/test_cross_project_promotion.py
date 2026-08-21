"""Unit tests for P2 Layer 5 跨项目自动提升 — ScopePromoter(§11.3 / §14.5).

验证:
- 3 项目共现 → 自动提升 global;2 项目共现 → 不提升
- 幂等(重复运行不重复提升)/ 阈值可配 / 手动提升
- 调度器任务挂接(register_scope_promotion_task + submit)
- 退出标准(§14.5): 标注数据集(24 样本,12 正例 + 12 负例)准确率 > 90%
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from smilex.memory.models import Entity, MemoryScope, Triple
from smilex.memory.quality import (
    TASK_SCOPE_PROMOTION,
    ScopePromoter,
    normalize_value,
    register_scope_promotion_task,
)
from smilex.memory.scheduler import MemoryTaskScheduler, SchedulerConfig
from smilex.memory.scheduler.bootstrap.seeds import make_entity_id
from smilex.memory.storage.storage_engine import StorageEngine, parse_scope_path

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def engine():
    eng = StorageEngine(":memory:", load_vec=False)
    await eng.initialize()
    yield eng
    await eng.close()


async def _ensure_entity(
    engine: StorageEngine, scope: str, name: str, etype: str = "concept"
) -> str:
    """按名称确保实体存在于 scope,返回实体 id(名称可能大小写不同但 entity_id 相同)."""
    scope_enum, scope_id = parse_scope_path(scope)
    entity_id = make_entity_id(etype, name)
    cursor = await engine.conn.execute(
        "SELECT id FROM entities WHERE scope = ? AND entity_id = ?",
        [scope, entity_id],
    )
    row = await cursor.fetchone()
    if row is not None:
        return str(row["id"])
    entity = Entity(
        entity_id=entity_id, entity_type=etype, name=name, scope=scope_enum
    )
    await engine.write_entity(entity, scope_id=scope_id)
    return entity.id


async def _add_fact(
    engine: StorageEngine,
    scope: str,
    subject: str,
    predicate: str,
    obj: str,
    *,
    kind: str = "value",
    rt: str = "semantic",
    etype: str = "concept",
    raw: bool = False,
    vt: datetime | None = None,
) -> str:
    """写入一条事实三元组(kind="ref" 时 obj 为客体实体名),返回 triple id."""
    scope_enum, scope_id = parse_scope_path(scope)
    subject_id = subject if raw else await _ensure_entity(engine, scope, subject, etype)
    object_id = None
    object_value = None
    if kind == "ref":
        object_id = await _ensure_entity(engine, scope, obj, etype)
    else:
        object_value = obj
    triple = Triple(
        triple_id=f"{subject_id}|{predicate}|{object_id or object_value}",
        subject_id=subject_id,
        predicate=predicate,
        object_id=object_id,
        object_value=object_value,
        scope=scope_enum,
        valid_from=_BASE,
        valid_to=vt,
        relation_type=rt,
    )
    return await engine.write_triple(triple, scope_id=scope_id)


def _key(subject: str, predicate: str, obj: str, *, kind: str = "value",
         etype: str = "concept", raw: bool = False) -> str:
    """测试侧模式键(与 ScopePromoter 归一化规则一致)."""
    subject_key = subject if raw else make_entity_id(etype, subject)
    object_key = (
        make_entity_id(etype, obj) if kind == "ref" else f"value:{normalize_value(obj)}"
    )
    return f"{subject_key}|{predicate}|{object_key}"


# ---------- 功能测试 ----------


async def test_three_projects_cooccurrence_promoted(engine):
    """3 项目共现 → 自动提升 global(实体补建 + 溯源回指)."""
    for pid in ("proj_a", "proj_b", "proj_c"):
        await _add_fact(engine, f"project:{pid}", "Redis", "has_failure_mode", "缓存雪崩")
    promoter = ScopePromoter(engine)
    report = await promoter.detect_and_promote()

    assert len(report.patterns) == 1
    assert report.patterns[0].project_count == 3
    assert sorted(report.patterns[0].source_scopes) == [
        "project:proj_a",
        "project:proj_b",
        "project:proj_c",
    ]
    assert len(report.promoted) == 1
    assert report.scanned_triples == 3

    # global 副本: scope 正确 + 主体实体已补建 + source_closet 回指源三元组
    cursor = await engine.conn.execute(
        "SELECT * FROM triples WHERE scope = 'global'"
    )
    global_triples = await cursor.fetchall()
    assert len(global_triples) == 1
    row = global_triples[0]
    assert row["predicate"] == "has_failure_mode"
    assert row["object_value"] == "缓存雪崩"
    src = await engine.get_triple(row["source_closet"])
    assert src is not None  # 溯源指针有效

    cursor = await engine.conn.execute(
        "SELECT * FROM entities WHERE scope = 'global'"
    )
    global_entities = await cursor.fetchall()
    assert len(global_entities) == 1
    assert global_entities[0]["entity_id"] == "concept:redis"
    assert global_entities[0]["source_closet"] is not None  # 实体溯源
    # global 三元组的 subject 指向 global 实体而非项目实体
    assert row["subject_id"] == global_entities[0]["id"]


async def test_two_projects_not_promoted(engine):
    """2 项目共现 → 不提升."""
    for pid in ("proj_a", "proj_b"):
        await _add_fact(engine, f"project:{pid}", "Redis", "has_failure_mode", "缓存雪崩")
    promoter = ScopePromoter(engine)
    report = await promoter.detect_and_promote()
    assert report.patterns == []
    assert report.promoted == []
    cursor = await engine.conn.execute(
        "SELECT COUNT(*) AS c FROM triples WHERE scope = 'global'"
    )
    assert (await cursor.fetchone())["c"] == 0


async def test_idempotent_rerun(engine):
    """幂等: 重复运行不重复提升."""
    for pid in ("proj_a", "proj_b", "proj_c"):
        await _add_fact(engine, f"project:{pid}", "Redis", "has_failure_mode", "缓存雪崩")
    promoter = ScopePromoter(engine)
    first = await promoter.detect_and_promote()
    second = await promoter.detect_and_promote()
    assert len(first.promoted) == 1
    assert second.promoted == []
    assert second.skipped_existing == 1
    cursor = await engine.conn.execute(
        "SELECT COUNT(*) AS c FROM triples WHERE scope = 'global'"
    )
    assert (await cursor.fetchone())["c"] == 1
    # 新共现出现后,再次运行只提升新模式
    for pid in ("proj_a", "proj_b", "proj_c"):
        await _add_fact(engine, f"project:{pid}", "Kafka", "has_failure_mode", "消息堆积")
    third = await promoter.detect_and_promote()
    assert len(third.promoted) == 1
    assert third.skipped_existing == 1


async def test_min_projects_configurable(engine):
    """阈值可配: min_projects=2 时 2 项目共现即提升."""
    for pid in ("proj_a", "proj_b"):
        await _add_fact(engine, f"project:{pid}", "Redis", "has_failure_mode", "缓存雪崩")
    promoter = ScopePromoter(engine, min_projects=2)
    report = await promoter.detect_and_promote()
    assert len(report.promoted) == 1


async def test_manual_promote(engine):
    """手动提升单条三元组(§11.3 manual_promote),按模式键幂等."""
    tid = await _add_fact(engine, "project:proj_a", "Redis", "has_default_port", "6379")
    promoter = ScopePromoter(engine)
    promoted_id = await promoter.manual_promote(tid)
    assert promoted_id is not None
    promoted = await engine.get_triple(promoted_id)
    assert promoted is not None
    assert promoted.scope is MemoryScope.GLOBAL
    assert promoted.source_closet == tid  # 溯源回指源三元组
    # 重复手动提升同键 → 幂等返回 None
    assert await promoter.manual_promote(tid) is None


async def test_ref_object_promoted_with_both_entities(engine):
    """实体引用三元组提升: 主体 + 客体实体都补建到 global."""
    for pid in ("proj_a", "proj_b", "proj_c"):
        await _add_fact(
            engine, f"project:{pid}", "FastAPI", "depends_on", "Starlette", kind="ref"
        )
    promoter = ScopePromoter(engine)
    report = await promoter.detect_and_promote()
    assert len(report.promoted) == 1
    cursor = await engine.conn.execute(
        "SELECT entity_id FROM entities WHERE scope = 'global' ORDER BY entity_id"
    )
    assert [r["entity_id"] for r in await cursor.fetchall()] == [
        "concept:fastapi",
        "concept:starlette",
    ]
    cursor = await engine.conn.execute(
        "SELECT subject_id, object_id FROM triples WHERE scope = 'global'"
    )
    row = (await cursor.fetchall())[0]
    assert row["object_id"] is not None  # 引用而非字面值


async def test_scheduler_task_hookup(engine):
    """调度器挂接: register_scope_promotion_task + submit 驱动提升."""
    for pid in ("proj_a", "proj_b", "proj_c"):
        await _add_fact(engine, f"project:{pid}", "Redis", "has_failure_mode", "缓存雪崩")
    sched = MemoryTaskScheduler(engine, SchedulerConfig(tick_interval=0.01))
    register_scope_promotion_task(sched, engine)
    assert sched.has_task(TASK_SCOPE_PROMOTION)
    await sched.start()
    try:
        await sched.submit(TASK_SCOPE_PROMOTION)
        await sched.wait_idle(timeout=5.0)
    finally:
        await sched.stop()
    cursor = await engine.conn.execute(
        "SELECT COUNT(*) AS c FROM triples WHERE scope = 'global'"
    )
    assert (await cursor.fetchone())["c"] == 1


# ---------- 标注数据集(12 正例 + 12 负例) ----------
# 每条: (用例名, 事实列表, 期望提升的模式键集合)
# 事实: (scope, subject, predicate, object) + 可选 opts dict
#   opts: kind("value"/"ref") / rt(relation_type) / etype(实体类型) /
#         raw(无实体行的裸 subject_id) / vt(valid_to)
# 注: 各用例的实体/谓词/取值命名互不相同,模式键天然隔离,scope 复用不串扰

_P3 = ["project:acc_a", "project:acc_b", "project:acc_c"]  # 标准 3 项目


def _spread(subject: str, predicate: str, obj: str, scopes: list[str], **opts) -> list:
    """同一事实铺到多个 scope(正例便捷构造)."""
    return [(s, subject, predicate, obj, opts) for s in scopes]


LABELED_SAMPLES: list[tuple[str, list[tuple], set[str]]] = [
    # ===== 正例: 应提升 =====
    ("pos_exact_3",
     _spread("Redis", "has_failure_mode", "缓存雪崩", _P3),
     {_key("Redis", "has_failure_mode", "缓存雪崩")}),
    ("pos_five_projects",
     _spread("PostgreSQL", "supports", "jsonb",
             ["project:acc_a", "project:acc_b", "project:acc_c",
              "project:acc_d", "project:acc_e"]),
     {_key("PostgreSQL", "supports", "jsonb")}),
    ("pos_object_case_variant", [
        ("project:acc_a", "Nginx", "has_role", "Reverse Proxy", {}),
        ("project:acc_b", "Nginx", "has_role", "reverse proxy", {}),
        ("project:acc_c", "Nginx", "has_role", "REVERSE  PROXY ", {}),
     ], {_key("Nginx", "has_role", "reverse proxy")}),
    ("pos_subject_case_variant", [
        ("project:acc_a", "Kafka", "has_failure_mode", "消息堆积", {}),
        ("project:acc_b", "kafka", "has_failure_mode", "消息堆积", {}),
        ("project:acc_c", "KAFKA", "has_failure_mode", "消息堆积", {}),
     ], {_key("Kafka", "has_failure_mode", "消息堆积")}),
    ("pos_ref_object",
     _spread("FastAPI", "depends_on", "Starlette", _P3, kind="ref"),
     {_key("FastAPI", "depends_on", "Starlette", kind="ref")}),
    ("pos_extra_support", [
        ("project:acc_a", "Docker", "has_command", "compose", {}),
        ("project:acc_a", "Docker", "has_command", "compose", {}),  # 同项目重复计 support
        ("project:acc_b", "Docker", "has_command", "compose", {}),
        ("project:acc_c", "Docker", "has_command", "compose", {}),
     ], {_key("Docker", "has_command", "compose")}),
    ("pos_raw_subject",
     _spread("shared-lib-raw", "has_option", "retry=3", _P3, raw=True),
     {_key("shared-lib-raw", "has_option", "retry=3", raw=True)}),
    ("pos_chinese",
     _spread("缓存", "最佳实践", "设置过期时间", _P3),
     {_key("缓存", "最佳实践", "设置过期时间")}),
    ("pos_temporal_rt",
     _spread("OrderService", "depends_on", "Database", _P3, rt="temporal"),
     {_key("OrderService", "depends_on", "Database")}),
    ("pos_numeric_value",
     _spread("Team", "size", "5 人", _P3),
     {_key("Team", "size", "5 人")}),
    ("pos_spatial_rt",
     _spread("Office", "located_in", "上海", _P3, rt="spatial"),
     {_key("Office", "located_in", "上海")}),
    ("pos_protocol",
     _spread("Service-Mesh", "uses_protocol", "gRPC", _P3),
     {_key("Service-Mesh", "uses_protocol", "gRPC")}),
    # ===== 负例: 不应提升 =====
    ("neg_two_projects",
     _spread("RabbitMQ", "has_failure_mode", "队列阻塞", ["project:acc_a", "project:acc_b"]),
     set()),
    ("neg_one_project_repeat", [
        ("project:acc_a", "Memcached", "has_failure_mode", "击穿", {}),
        ("project:acc_a", "Memcached", "has_failure_mode", "击穿", {}),
        ("project:acc_a", "Memcached", "has_failure_mode", "击穿", {}),
     ], set()),
    ("neg_diff_predicate", [
        ("project:acc_a", "Envoy", "uses_protocol", "HTTP2", {}),
        ("project:acc_b", "Envoy", "supports_protocol", "HTTP2", {}),
        ("project:acc_c", "Envoy", "prefers_protocol", "HTTP2", {}),
     ], set()),
    ("neg_diff_object", [
        ("project:acc_a", "MySQL", "default_engine", "InnoDB", {}),
        ("project:acc_b", "MySQL", "default_engine", "MyISAM", {}),
        ("project:acc_c", "MySQL", "default_engine", "Aria", {}),
     ], set()),
    ("neg_diff_subject", [
        ("project:acc_a", "Frontend", "uses_tech", "React", {}),
        ("project:acc_b", "Backend", "uses_tech", "React", {}),
        ("project:acc_c", "Mobile", "uses_tech", "React", {}),
     ], set()),
    ("neg_state_excluded",
     _spread("Pipeline", "deploy_env", "prod", _P3, rt="project_state"),
     set()),
    ("neg_historical_excluded", [
        ("project:acc_a", "Vault", "stores", "secrets", {"vt": datetime(2026, 2, 1, tzinfo=UTC)}),
        ("project:acc_b", "Vault", "stores", "secrets", {"vt": datetime(2026, 3, 1, tzinfo=UTC)}),
        ("project:acc_c", "Vault", "stores", "secrets", {"vt": datetime(2026, 4, 1, tzinfo=UTC)}),
     ], set()),
    ("neg_ref_diff_entities", [
        ("project:acc_a", "Gateway", "depends_on", "AuthService", {"kind": "ref"}),
        ("project:acc_b", "Gateway", "depends_on", "UserService", {"kind": "ref"}),
        ("project:acc_c", "Gateway", "depends_on", "OrderService", {"kind": "ref"}),
     ], set()),
    ("neg_global_not_counted", [
        ("global", "Etcd", "has_role", "选主", {}),
        ("project:acc_a", "Etcd", "has_role", "选主", {}),
        ("project:acc_b", "Etcd", "has_role", "选主", {}),
     ], set()),
    ("neg_tenant_not_counted", [
        ("project:acc_a", "Consul", "has_role", "服务发现", {}),
        ("project:acc_b", "Consul", "has_role", "服务发现", {}),
        ("tenant:acc_t1", "Consul", "has_role", "服务发现", {}),
     ], set()),
    ("neg_entity_type_diff", [
        ("project:acc_a", "Migration", "has_risk", "锁表", {"etype": "concept"}),
        ("project:acc_b", "Migration", "has_risk", "锁表", {"etype": "event"}),
        ("project:acc_c", "Migration", "has_risk", "锁表", {"etype": "object"}),
     ], set()),
    ("neg_value_vs_ref", [
        ("project:acc_a", "App", "depends_on", "Database", {"kind": "ref"}),
        ("project:acc_b", "App", "depends_on", "Database", {"kind": "ref"}),
        ("project:acc_c", "App", "depends_on", "Database", {"kind": "value"}),
     ], set()),
]


async def test_promotion_accuracy_above_90_percent(engine):
    """退出标准(§14.5): 标注数据集跨项目提升准确率 > 90%."""
    candidate_keys: list[tuple[str, set[str]]] = []  # 每用例可能产出的模式键
    for name, facts, _ in LABELED_SAMPLES:
        keys = set()
        for fact in facts:
            scope, subject, predicate, obj, *rest = fact
            opts = rest[0] if rest else {}
            keys.add(_key(subject, predicate, obj, **{
                k: v for k, v in opts.items() if k in ("kind", "etype", "raw")
            }))
            await _add_fact(engine, scope, subject, predicate, obj, **opts)
        candidate_keys.append((name, keys))

    promoter = ScopePromoter(engine)
    report = await promoter.detect_and_promote()
    detected = {p.key for p in report.patterns}

    failures: list[str] = []
    for (name, keys), (_, _, expected) in zip(candidate_keys, LABELED_SAMPLES, strict=True):
        actual = detected & keys
        if actual != expected:
            failures.append(f"{name}: 期望 {sorted(expected)},实得 {sorted(actual)}")
    accuracy = (len(LABELED_SAMPLES) - len(failures)) / len(LABELED_SAMPLES)
    assert accuracy > 0.9, f"准确率 {accuracy:.1%} 未达标;失败用例:\n" + "\n".join(failures)
