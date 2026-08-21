"""Unit tests for P1-b 种子注入(L3.7)— SeedInjector / inject_seeds.

验证:
- 模板 key 注入(复用 templates 加载机制)
- 外部 YAML 种子包注入(与模板同构)
- ExtractionResult 直接注入
- 幂等: 重复注入不增实体/三元组
- global scope 拒绝;未知来源类型报错
- MemoryMiddleware.inject_seeds 集成入口
"""

from __future__ import annotations

import pytest

from smilex.memory.scheduler.bootstrap import (
    EntitySeed,
    ExtractionResult,
    ProjectBootstrap,
    SeedInjector,
    TripleSeed,
    inject_seeds,
    load_seed_pack,
)
from smilex.memory.storage.storage_engine import StorageEngine
from smilex.middlewares.dto import ProjectInitRequest
from smilex.middlewares.memory import MemoryMiddleware


@pytest.fixture
async def engine():
    eng = StorageEngine(":memory:", load_vec=False)
    await eng.initialize()
    yield eng
    await eng.close()


@pytest.fixture
async def project(engine):
    bootstrap = ProjectBootstrap(engine)
    ctx = await bootstrap.initialize(ProjectInitRequest(name="demo", tech_stack=["Python"]))
    return bootstrap, ctx.scope


async def _entity_ids(engine, scope):
    cursor = await engine.conn.execute(
        "SELECT entity_id FROM entities WHERE scope = ?", [scope]
    )
    return {r["entity_id"] for r in await cursor.fetchall()}


SEED_PACK_YAML = """\
name: "Payment Domain"
description: "支付领域知识包"
skeleton_entities:
  - {type: concept, name: "Payment Gateway"}
  - {type: concept, name: "Ledger"}
skeleton_triples:
  - {subject: "Payment Gateway", predicate: "depends_on", object: "Ledger"}
  - {subject: "Ledger", predicate: "stores", object_value: "交易流水"}
"""


async def test_inject_template(project, engine):
    """模板 key 注入: web 骨架实体/三元组进入已有项目 scope."""
    bootstrap, scope = project
    injector = SeedInjector(bootstrap)
    entity_count, triple_count = await injector.inject(scope, "web")
    entity_ids = await _entity_ids(engine, scope)
    assert "concept:http-api" in entity_ids
    assert "concept:database" in entity_ids
    assert entity_count == len(entity_ids)
    assert triple_count > 0


async def test_inject_template_idempotent(project, engine):
    """重复注入同一模板: 总量不变(apply_seeds 幂等)."""
    bootstrap, scope = project
    injector = SeedInjector(bootstrap)
    first = await injector.inject(scope, "web")
    second = await injector.inject(scope, "web")
    assert second == first


async def test_inject_yaml_seed_pack(project, engine, tmp_path):
    """外部 YAML 种子包注入(路径 str / Path 均可)."""
    bootstrap, scope = project
    pack = tmp_path / "payment.yaml"
    pack.write_text(SEED_PACK_YAML, encoding="utf-8")

    injector = SeedInjector(bootstrap)
    await injector.inject(scope, str(pack))
    entity_ids = await _entity_ids(engine, scope)
    assert {"concept:payment-gateway", "concept:ledger"} <= entity_ids
    cursor = await engine.conn.execute(
        "SELECT predicate, object_value FROM triples WHERE scope = ?", [scope]
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    assert any(r["predicate"] == "depends_on" for r in rows)
    assert any(r["object_value"] == "交易流水" for r in rows)

    # Path 对象 + 幂等
    again = await injector.inject(scope, pack)
    once_more = await injector.inject(scope, pack)
    assert again == once_more


async def test_load_seed_pack_missing(tmp_path):
    with pytest.raises(ValueError, match="种子包不存在"):
        load_seed_pack(tmp_path / "nope.yaml")


async def test_inject_extraction_result(project, engine):
    """手工构造的 ExtractionResult 直接注入."""
    bootstrap, scope = project
    seeds = ExtractionResult(
        entities=[EntitySeed(name="Redis Cluster", entity_id="concept:redis-cluster")],
        triples=[
            TripleSeed(
                subject="demo", predicate="uses_tech", object_name="Redis Cluster"
            )
        ],
    )
    await inject_seeds(bootstrap, scope, seeds)
    assert "concept:redis-cluster" in await _entity_ids(engine, scope)


async def test_inject_rejects_global_scope(project):
    bootstrap, _ = project
    injector = SeedInjector(bootstrap)
    with pytest.raises(ValueError, match="scope"):
        await injector.inject("global", "web")


async def test_inject_unknown_type(project):
    bootstrap, scope = project
    injector = SeedInjector(bootstrap)
    with pytest.raises(TypeError, match="不支持的种子来源类型"):
        await injector.inject(scope, 123)


async def test_middleware_inject_seeds():
    """MemoryMiddleware.inject_seeds: 缺省 scope = 当前项目."""
    mw = MemoryMiddleware()
    await mw.initialize()
    try:
        resp = await mw.initialize_project(ProjectInitRequest(name="demo"))
        entity_count, triple_count = await mw.inject_seeds("web")
        cursor = await mw.engine.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE scope = ?", [resp.scope]
        )
        assert entity_count == int((await cursor.fetchone())[0])
        assert triple_count > 0
    finally:
        await mw.close()
