"""Unit tests for P2 Layer 3 跨项目克隆 — CrossProjectCloner(§9.5.1 机制 6 / 模块 11).

验证:
- 端到端: 项目 A(实体/三元组/片段)→ 克隆到 B → B 数据完整、scope 正确、
  ID 全部重映射、A 不受影响、溯源回指
- CloneFilter: 实体类型 / 片段层 / 时间过滤生效;端点被过滤的三元组跳过
- 幂等: 重复克隆到同一目标不增数据
- 导出/导入 JSON 包往返(不含向量,ADR-022)
- middleware 薄封装 clone_project
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from smilex.memory.models import Entity, MemoryScope, Triple
from smilex.memory.scheduler.bootstrap import (
    CloneFilter,
    CloneResult,
    CrossProjectCloner,
)
from smilex.memory.storage.storage_engine import StorageEngine
from smilex.middlewares import MemoryMiddleware

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
SRC = "project:proj_a"
DST = "project:proj_b"


@pytest.fixture
async def engine():
    eng = StorageEngine(":memory:", load_vec=False)
    await eng.initialize()
    yield eng
    await eng.close()


async def _add_entity(
    engine: StorageEngine, scope: str, name: str, etype: str = "concept"
) -> str:
    scope_enum, scope_id = scope.rsplit(":", 1) if ":" in scope else (scope, None)
    enum = MemoryScope(scope_enum) if scope_enum in ("project", "tenant") else MemoryScope.GLOBAL
    entity = Entity(
        entity_id=f"{etype}:{name.lower()}",
        entity_type=etype,
        name=name,
        scope=enum,
        valid_from=_BASE,
    )
    await engine.write_entity(entity, scope_id=scope_id if enum is not MemoryScope.GLOBAL else None)
    return entity.id


async def _add_triple(
    engine: StorageEngine,
    scope: str,
    subject_id: str,
    predicate: str,
    *,
    object_id: str | None = None,
    object_value: str | None = None,
    predecessor_id: str | None = None,
    rt: str = "semantic",
) -> str:
    scope_enum, scope_id = scope.rsplit(":", 1) if ":" in scope else (scope, None)
    enum = MemoryScope(scope_enum) if scope_enum in ("project", "tenant") else MemoryScope.GLOBAL
    triple = Triple(
        triple_id=f"{subject_id}|{predicate}|{object_id or object_value}",
        subject_id=subject_id,
        predicate=predicate,
        object_id=object_id,
        object_value=object_value,
        scope=enum,
        valid_from=_BASE,
        predecessor_id=predecessor_id,
        relation_type=rt,
    )
    await engine.write_triple(
        triple, scope_id=scope_id if enum is not MemoryScope.GLOBAL else None
    )
    return triple.id


async def _add_fragment(
    engine: StorageEngine,
    scope: str,
    fid: str,
    content: str,
    *,
    layer: str = "L1",
    entities: tuple[str, ...] = (),
    relations: tuple[str, ...] = (),
    time_start: str = "2026-01-01T00:00:00.000000Z",
) -> None:
    await engine.conn.execute(
        "INSERT INTO temporal_fragments(id, fragment_id, time_start, time_end, content, "
        "entities, relations, scope, layer, importance, created_at, updated_at) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 0.5, ?, ?)",
        (
            fid,
            fid,
            time_start,
            content,
            json.dumps(list(entities)),
            json.dumps(list(relations)),
            scope,
            layer,
            time_start,
            time_start,
        ),
    )
    await engine.conn.commit()


async def _seed_source(engine: StorageEngine) -> dict[str, str]:
    """构造源项目: 3 实体(含 1 person)+ 3 三元组(含 1 条因果前驱)+ 2 片段."""
    ids: dict[str, str] = {}
    ids["e_redis"] = await _add_entity(engine, SRC, "redis")
    ids["e_cache"] = await _add_entity(engine, SRC, "cache")
    ids["e_alice"] = await _add_entity(engine, SRC, "alice", "person")
    ids["t1"] = await _add_triple(
        engine, SRC, ids["e_redis"], "has_failure_mode", object_value="缓存雪崩"
    )
    ids["t2"] = await _add_triple(
        engine, SRC, ids["e_cache"], "uses", object_id=ids["e_redis"]
    )
    ids["t3"] = await _add_triple(
        engine, SRC, ids["e_redis"], "mitigated_by",
        object_value="设置过期时间", predecessor_id=ids["t1"],
    )
    ids["t4"] = await _add_triple(
        engine, SRC, ids["e_alice"], "maintains", object_id=ids["e_cache"]
    )
    await _add_fragment(
        engine, SRC, "frag-1", "Redis 缓存雪崩复盘",
        layer="L1", entities=(ids["e_redis"],), relations=(ids["t1"],),
    )
    await _add_fragment(engine, SRC, "frag-2", "缓存架构整合记忆", layer="L2")
    return ids


async def _rows(engine: StorageEngine, table: str, scope: str) -> list[dict]:
    cursor = await engine.conn.execute(
        f"SELECT * FROM {table} WHERE scope = ?", [scope]
    )
    return [dict(r) for r in await cursor.fetchall()]


# ---------- 端到端 ----------


async def test_clone_end_to_end(engine):
    """端到端: A → B 数据完整 / scope 正确 / ID 重映射 / A 不受影响 / 溯源."""
    ids = await _seed_source(engine)
    cloner = CrossProjectCloner(engine)
    result = await cloner.clone_to(SRC, DST)

    assert isinstance(result, CloneResult)
    assert result.source_scope == SRC
    assert result.target_scope == DST
    assert result.entity_count == 3
    assert result.triple_count == 4
    assert result.fragment_count == 2
    assert result.errors == []

    # B: scope 全部正确 + ID 全部新生成
    b_entities = await _rows(engine, "entities", DST)
    b_triples = await _rows(engine, "triples", DST)
    b_fragments = await _rows(engine, "temporal_fragments", DST)
    assert len(b_entities) == 3
    assert len(b_triples) == 4
    assert len(b_fragments) == 2
    src_ids = set(ids.values())
    for row in [*b_entities, *b_triples]:
        assert row["id"] not in src_ids
    assert {f["id"] for f in b_fragments}.isdisjoint(src_ids)

    # 实体内容保留 + 溯源回指源实体
    by_entity_id = {e["entity_id"]: e for e in b_entities}
    assert set(by_entity_id) == {"concept:redis", "concept:cache", "person:alice"}
    assert by_entity_id["concept:redis"]["source_closet"] == ids["e_redis"]

    # 三元组引用重映射: uses 指向 B 的新实体 id;因果前驱指向 B 的新 t1
    b_triple_by_pred = {t["predicate"]: t for t in b_triples}
    uses = b_triple_by_pred["uses"]
    assert uses["subject_id"] == by_entity_id["concept:cache"]["id"]
    assert uses["object_id"] == by_entity_id["concept:redis"]["id"]
    mitigated = b_triple_by_pred["mitigated_by"]
    src_t1_in_b = b_triple_by_pred["has_failure_mode"]
    assert mitigated["predecessor_id"] == src_t1_in_b["id"]
    assert mitigated["source_closet"] == ids["t3"]  # 溯源

    # 片段: entities/relations 引用改写为 B 的新 id
    f1 = next(f for f in b_fragments if f["fragment_id"] == "frag-1")
    assert json.loads(f1["entities"]) == [by_entity_id["concept:redis"]["id"]]
    assert json.loads(f1["relations"]) == [src_t1_in_b["id"]]

    # A 不受影响
    assert len(await _rows(engine, "entities", SRC)) == 3
    assert len(await _rows(engine, "triples", SRC)) == 4
    assert len(await _rows(engine, "temporal_fragments", SRC)) == 2
    src_t3 = await engine.get_triple(ids["t3"])
    assert src_t3 is not None
    assert src_t3.predecessor_id == ids["t1"]


async def test_clone_idempotent(engine):
    """幂等: 重复克隆到同一目标不增数据."""
    await _seed_source(engine)
    cloner = CrossProjectCloner(engine)
    first = await cloner.clone_to(SRC, DST)
    second = await cloner.clone_to(SRC, DST)
    assert first.entity_count == 3
    assert second.entity_count == 0
    assert second.triple_count == 0
    assert second.fragment_count == 0
    assert second.skipped_count == 3 + 4 + 2
    assert len(await _rows(engine, "entities", DST)) == 3


# ---------- 过滤器 ----------


async def test_filter_entity_types(engine):
    """实体类型过滤: person 被排除,引用它的三元组跳过."""
    await _seed_source(engine)
    cloner = CrossProjectCloner(engine)
    result = await cloner.clone_to(SRC, DST, CloneFilter(entity_types=["concept"]))
    assert result.entity_count == 2  # alice(person) 被过滤
    assert result.triple_count == 3  # maintains(alice→cache) 端点缺失跳过
    assert result.skipped_count == 1
    b_triples = await _rows(engine, "triples", DST)
    assert {t["predicate"] for t in b_triples} == {
        "has_failure_mode",
        "uses",
        "mitigated_by",
    }


async def test_filter_layers(engine):
    """层过滤: 仅克隆 L2 片段."""
    await _seed_source(engine)
    cloner = CrossProjectCloner(engine)
    result = await cloner.clone_to(SRC, DST, CloneFilter(layers=["L2"]))
    assert result.fragment_count == 1
    b_fragments = await _rows(engine, "temporal_fragments", DST)
    assert [f["layer"] for f in b_fragments] == ["L2"]


async def test_filter_time_range(engine):
    """时间过滤: since 晚于源数据 valid_from/time_start → 不克隆."""
    await _seed_source(engine)
    cloner = CrossProjectCloner(engine)
    result = await cloner.clone_to(
        SRC, DST, CloneFilter(since=datetime(2026, 6, 1, tzinfo=UTC))
    )
    assert result.entity_count == 0
    assert result.triple_count == 0
    assert result.fragment_count == 0


async def test_clone_to_global_rejected(engine):
    """克隆目标不能是 global(通用化走 ScopePromoter)."""
    await _seed_source(engine)
    cloner = CrossProjectCloner(engine)
    with pytest.raises(ValueError, match="克隆目标"):
        await cloner.clone_to(SRC, "global")


# ---------- 导出 / 导入 ----------


async def test_export_import_roundtrip(engine, tmp_path):
    """导出 JSON 包(不含向量)→ 导入新 scope,数据与克隆一致."""
    await _seed_source(engine)
    cloner = CrossProjectCloner(engine)
    pkg = tmp_path / "snapshot.json"
    export_result = await cloner.export_package(SRC, pkg)
    assert export_result.entity_count == 3
    assert export_result.triple_count == 4
    assert export_result.fragment_count == 2

    payload = json.loads(pkg.read_text(encoding="utf-8"))
    assert payload["version"] == "1.0"
    assert payload["scope"] == SRC
    assert all("embedding" not in e for e in payload["entities"])  # ADR-022

    import_result = await cloner.import_package(pkg, DST)
    assert import_result.source_scope == SRC
    assert import_result.entity_count == 3
    assert import_result.triple_count == 4
    assert import_result.fragment_count == 2
    # 导入结果与直接克隆等价(再克隆到同目标全部去重跳过)
    clone_result = await cloner.clone_to(SRC, DST)
    assert clone_result.entity_count == 0
    assert clone_result.triple_count == 0
    assert clone_result.fragment_count == 0


async def test_import_package_version_check(engine, tmp_path):
    """不兼容版本 / 不存在的包报错."""
    cloner = CrossProjectCloner(engine)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": "9.9"}), encoding="utf-8")
    with pytest.raises(ValueError, match="版本"):
        await cloner.import_package(bad, DST)
    with pytest.raises(ValueError, match="不存在"):
        await cloner.import_package(tmp_path / "missing.json", DST)


# ---------- middleware 薄封装 ----------


async def test_middleware_clone_project():
    """MemoryMiddleware.clone_project: 新 scope + 项目实体种子 + 源不受影响."""
    mw = MemoryMiddleware()
    await mw.initialize()
    try:
        engine = mw.engine
        e_redis = await _add_entity(engine, SRC, "redis")
        await _add_triple(
            engine, SRC, e_redis, "has_failure_mode", object_value="缓存雪崩"
        )
        result = await mw.clone_project(SRC, "new-proj")
        assert result.target_scope.startswith("project:")
        assert result.target_scope != SRC
        assert result.entity_count == 1
        assert result.triple_count == 1

        # 目标 scope: 克隆实体 + 项目实体种子
        b_entities = await _rows(engine, "entities", result.target_scope)
        assert {e["entity_id"] for e in b_entities} == {
            "concept:redis",
            "project:new-proj",
        }
        # 源不受影响
        assert len(await _rows(engine, "entities", SRC)) == 1
    finally:
        await mw.close()
