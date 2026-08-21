"""跨项目克隆(§9.5.1 机制 6 / 模块 11-project-export 模式 B)— 项目记忆复制/导出/导入.

把一个 scope 的记忆(实体 / 三元组 / 时序片段)经 CloneFilter 过滤后克隆到
新 scope(GAPS §3.4 补全 CloneFilter / CloneResult):

- ID 全部重新生成(新 ULID),避免跨 scope ID 冲突;实体/三元组/片段间的
  引用(subject_id / object_id / predecessor_id / fragments.entities /
  fragments.relations)经统一映射表改写,被过滤掉的端点引用丢弃
- 溯源: 克隆出的实体/三元组 source_closet 回指源记录 id
- 幂等: 重复克隆到同一目标 scope 不增数据 — 实体按 entity_id 去重、
  三元组按 (subject, predicate, object) 全键去重、片段按 fragment_id 去重
  (与 ProjectBootstrap.apply_seeds / BulkImporter 同一去重约定)
- 过滤: CloneFilter 按实体类型 / 片段层 / 时间(实体 valid_from、
  三元组 valid_from、片段 time_start)过滤;端点实体被过滤的三元组跳过

导出/导入(模块 11 模式 B 快照的轻量版):
- export_package: 导出为单个 JSON 包(不含向量,ADR-022;向量可由
  VectorStore 重建),CloneFilter 同样生效
- import_package: 从 JSON 包导入到目标 scope,复用克隆的 ID 重映射与去重
  写入路径,不重复造轮子(YAML 种子包仍归 seed_injector.load_seed_pack)

注: 本模块是行级复制(保留 confidence/certainty/valid_from/predecessor 等),
与种子级的 apply_seeds(按名称解析、只建骨架)互补,不复用.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ....utils.ids import generate_id
from ....utils.timeutil import from_iso, now_utc, to_iso
from ...models import MemoryScope
from ...storage.storage_engine import parse_scope_path

if TYPE_CHECKING:
    from ...storage.storage_engine import StorageEngine

# 快照包格式版本(模块 11 §4.1)
PACKAGE_VERSION = "1.0"


@dataclass
class CloneFilter:
    """跨项目克隆过滤器(GAPS §3.4 补全).

    Attributes:
        entity_types: 仅克隆这些实体类型(person/location/object/concept/event;
            None = 全部)
        layers: 仅克隆这些层的时序片段(L0/L1/L2/L3;None = 全部)
        since: 仅克隆不早于此时间的记录(实体/三元组按 valid_from,
            片段按 time_start;None = 不限)
        until: 仅克隆不晚于此时间的记录(None = 不限)
        include_entities / include_triples / include_fragments: 三类数据开关
        current_only: True 时仅克隆现行记录(valid_to IS NULL),
            False 连历史一起克隆
    """

    entity_types: list[str] | None = None
    layers: list[str] | None = None
    since: datetime | None = None
    until: datetime | None = None
    include_entities: bool = True
    include_triples: bool = True
    include_fragments: bool = True
    current_only: bool = True


@dataclass
class CloneResult:
    """克隆/导出/导入结果(GAPS §3.4 补全)."""

    source_scope: str  # 来源 scope(导入时为包内记录的源 scope)
    target_scope: str  # 目标 scope(导出时为包路径字符串)
    entity_count: int = 0
    triple_count: int = 0
    fragment_count: int = 0
    skipped_count: int = 0  # 已存在去重 / 端点被过滤而跳过
    elapsed_ms: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_scope": self.source_scope,
            "target_scope": self.target_scope,
            "entity_count": self.entity_count,
            "triple_count": self.triple_count,
            "fragment_count": self.fragment_count,
            "skipped_count": self.skipped_count,
            "elapsed_ms": self.elapsed_ms,
            "errors": list(self.errors),
        }


class CrossProjectCloner:
    """跨项目克隆器(§9.5.1 机制 6).

    Args:
        storage: Layer 1 StorageEngine(已 initialize)

    使用方法:
        cloner = CrossProjectCloner(storage)
        result = await cloner.clone_to("project:a", "project:b", CloneFilter())
    """

    def __init__(self, storage: StorageEngine) -> None:
        if not storage.is_initialized:
            raise RuntimeError("StorageEngine 未初始化,请先调用 await initialize()")
        self._storage = storage

    # ==================== 克隆 ====================

    async def clone_to(
        self,
        source_scope: str,
        target_scope: str,
        clone_filter: CloneFilter | None = None,
    ) -> CloneResult:
        """把 source_scope 的记忆克隆到 target_scope(新 ULID + 溯源 + 幂等).

        目标 scope 必须是 project/tenant(克隆到 global 会污染通用知识,
        通用化走 Layer 5 ScopePromoter).
        """
        scope_enum, _ = parse_scope_path(target_scope)
        if scope_enum is MemoryScope.GLOBAL:
            raise ValueError("克隆目标只能是 project/tenant scope")
        package = await self._load_scope(source_scope, clone_filter or CloneFilter())
        return await self._write_package(package, target_scope, source_scope=source_scope)

    # ==================== 导出 / 导入(JSON 包) ====================

    async def export_package(
        self,
        source_scope: str,
        path: str | Path,
        clone_filter: CloneFilter | None = None,
    ) -> CloneResult:
        """导出 scope 记忆为 JSON 包(模块 11 模式 B;不含向量,ADR-022)."""
        start = time.monotonic()
        package = await self._load_scope(source_scope, clone_filter or CloneFilter())
        payload = {
            "version": PACKAGE_VERSION,
            "generated_at": to_iso(now_utc()),
            "scope": source_scope,
            "entities": package["entities"],
            "triples": package["triples"],
            "fragments": package["fragments"],
        }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return CloneResult(
            source_scope=source_scope,
            target_scope=str(p),
            entity_count=len(package["entities"]),
            triple_count=len(package["triples"]),
            fragment_count=len(package["fragments"]),
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )

    async def import_package(
        self, path: str | Path, target_scope: str
    ) -> CloneResult:
        """从 JSON 包导入到目标 scope(复用克隆的 ID 重映射 + 去重写入)."""
        scope_enum, _ = parse_scope_path(target_scope)
        if scope_enum is MemoryScope.GLOBAL:
            raise ValueError("导入目标只能是 project/tenant scope")
        p = Path(path)
        if not p.is_file():
            raise ValueError(f"快照包不存在: {p}")
        payload = json.loads(p.read_text(encoding="utf-8"))
        if str(payload.get("version")) != PACKAGE_VERSION:
            raise ValueError(
                f"不兼容的快照包版本: {payload.get('version')!r}(支持 {PACKAGE_VERSION})"
            )
        package = {
            "entities": list(payload.get("entities", [])),
            "triples": list(payload.get("triples", [])),
            "fragments": list(payload.get("fragments", [])),
        }
        return await self._write_package(
            package, target_scope, source_scope=str(payload.get("scope", ""))
        )

    # ==================== 加载(源 scope → 包) ====================

    async def _load_scope(self, scope: str, f: CloneFilter) -> dict:
        """加载一个 scope 的实体/三元组/片段为 dict 行(过滤生效,不含向量)."""
        conn = self._storage.conn
        package: dict[str, list[dict]] = {"entities": [], "triples": [], "fragments": []}

        if f.include_entities:
            where = "scope = ?"
            params: list = [scope]
            if f.current_only:
                where += " AND valid_to IS NULL"
            cursor = await conn.execute(
                "SELECT id, entity_id, entity_type, name, valid_from, valid_to, "
                f"source_closet FROM entities WHERE {where}", params
            )
            for r in await cursor.fetchall():
                row = dict(r)
                if f.entity_types is not None and row["entity_type"] not in f.entity_types:
                    continue
                if not self._in_time_range(row["valid_from"], f):
                    continue
                package["entities"].append(row)

        if f.include_triples:
            where = "scope = ?"
            params = [scope]
            if f.current_only:
                where += " AND valid_to IS NULL"
            cursor = await conn.execute(
                "SELECT id, triple_id, subject_id, predicate, object_id, object_value, "
                "valid_from, valid_to, predecessor_id, causal_level, confidence, "
                f"certainty, relation_type, source_closet FROM triples WHERE {where}",
                params,
            )
            for r in await cursor.fetchall():
                row = dict(r)
                if not self._in_time_range(row["valid_from"], f):
                    continue
                package["triples"].append(row)

        if f.include_fragments:
            where = "scope = ?"
            params = [scope]
            cursor = await conn.execute(
                "SELECT id, fragment_id, time_start, time_end, location_id, content, "
                "entities, relations, layer, importance, created_at, updated_at "
                f"FROM temporal_fragments WHERE {where}",
                params,
            )
            for r in await cursor.fetchall():
                row = dict(r)
                if f.layers is not None and row["layer"] not in f.layers:
                    continue
                if not self._in_time_range(row["time_start"], f):
                    continue
                package["fragments"].append(row)

        return package

    @staticmethod
    def _in_time_range(iso: str | None, f: CloneFilter) -> bool:
        """时间过滤: ISO 字符串落在 [since, until] 内(None 端不限)."""
        if iso is None:
            return f.since is None
        dt = from_iso(iso)
        if f.since is not None and dt < f.since:
            return False
        return not (f.until is not None and dt > f.until)

    # ==================== 写入(包 → 目标 scope) ====================

    async def _write_package(
        self, package: dict, target_scope: str, *, source_scope: str | None = None
    ) -> CloneResult:
        """把包写入目标 scope: ID 重映射 + 引用改写 + 去重 + 溯源."""
        start = time.monotonic()
        conn = self._storage.conn
        result = CloneResult(
            source_scope=source_scope or "",
            target_scope=target_scope,
        )

        # ---- 目标 scope 现状(幂等去重键) ----
        cursor = await conn.execute(
            "SELECT entity_id FROM entities WHERE scope = ?", [target_scope]
        )
        existing_entity_keys = {r["entity_id"] for r in await cursor.fetchall()}
        cursor = await conn.execute(
            "SELECT subject_id, predicate, object_id, object_value FROM triples "
            "WHERE scope = ?",
            [target_scope],
        )
        existing_triple_keys = {
            (r["subject_id"], r["predicate"], r["object_id"], r["object_value"])
            for r in await cursor.fetchall()
        }
        cursor = await conn.execute(
            "SELECT fragment_id FROM temporal_fragments WHERE scope = ?", [target_scope]
        )
        existing_fragment_ids = {r["fragment_id"] for r in await cursor.fetchall()}

        # ---- 1. 实体: 新 ULID + entity_id 去重 + source_closet 溯源 ----
        id_map: dict[str, str] = {}  # 源 id → 目标 id(实体)
        for row in package["entities"]:
            if row["entity_id"] in existing_entity_keys:
                result.skipped_count += 1
                continue
            new_id = generate_id()
            id_map[row["id"]] = new_id
            existing_entity_keys.add(row["entity_id"])
            await conn.execute(
                "INSERT INTO entities(id, entity_id, entity_type, name, scope, "
                "valid_from, valid_to, embedding, source_closet) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    new_id,
                    row["entity_id"],
                    row["entity_type"],
                    row["name"],
                    target_scope,
                    row["valid_from"],
                    row["valid_to"],
                    row["source_closet"] or row["id"],  # 溯源: 回指源实体 id
                ),
            )
            result.entity_count += 1

        # ---- 2. 三元组: 端点重映射,端点缺失跳过;前驱两阶段改写(见下) ----
        triple_rows: list[tuple[str, dict]] = []  # (新 id, 源行)
        for row in package["triples"]:
            subject_id = id_map.get(row["subject_id"])
            if subject_id is None:
                result.skipped_count += 1
                continue
            object_id = None
            if row["object_id"] is not None:
                object_id = id_map.get(row["object_id"])
                if object_id is None:
                    result.skipped_count += 1
                    continue
            key = (subject_id, row["predicate"], object_id, row["object_value"])
            if key in existing_triple_keys:
                result.skipped_count += 1
                continue
            existing_triple_keys.add(key)
            new_id = generate_id()
            id_map[row["id"]] = new_id
            triple_rows.append((new_id, row))
            await conn.execute(
                "INSERT INTO triples(id, triple_id, subject_id, predicate, object_id, "
                "object_value, scope, valid_from, valid_to, predecessor_id, "
                "causal_level, confidence, certainty, relation_type, source_closet) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (
                    new_id,
                    f"{subject_id}|{row['predicate']}|{object_id or row['object_value']}",
                    subject_id,
                    row["predicate"],
                    object_id,
                    row["object_value"],
                    target_scope,
                    row["valid_from"],
                    row["valid_to"],
                    row["causal_level"],
                    row["confidence"],
                    row["certainty"],
                    row["relation_type"],
                    row["source_closet"] or row["id"],  # 溯源: 回指源三元组 id
                ),
            )
            result.triple_count += 1
        # 前驱指针第二遍改写: 与源行顺序解耦(前驱可能在后处理);
        # 前驱被过滤/未克隆 → 保持 NULL
        for new_id, row in triple_rows:
            if row["predecessor_id"] is None:
                continue
            mapped = id_map.get(row["predecessor_id"])
            if mapped is not None:
                await conn.execute(
                    "UPDATE triples SET predecessor_id = ? WHERE id = ?",
                    [mapped, new_id],
                )

        # ---- 3. 时序片段: fragment_id 去重 + entities/relations 引用改写 ----
        for row in package["fragments"]:
            if row["fragment_id"] in existing_fragment_ids:
                result.skipped_count += 1
                continue
            existing_fragment_ids.add(row["fragment_id"])
            new_id = generate_id()
            entities = [
                mapped
                for eid in json.loads(row["entities"])
                if (mapped := id_map.get(eid)) is not None
            ]
            relations = [
                mapped
                for tid in json.loads(row["relations"])
                if (mapped := id_map.get(tid)) is not None
            ]
            await conn.execute(
                "INSERT INTO temporal_fragments(id, fragment_id, time_start, time_end, "
                "location_id, content, embedding, entities, relations, scope, layer, "
                "importance, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id,
                    row["fragment_id"],
                    row["time_start"],
                    row["time_end"],
                    row["content"],
                    json.dumps(entities, ensure_ascii=False),
                    json.dumps(relations, ensure_ascii=False),
                    target_scope,
                    row["layer"],
                    row["importance"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
            result.fragment_count += 1

        await conn.commit()
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
        return result


__all__ = [
    "PACKAGE_VERSION",
    "CloneFilter",
    "CloneResult",
    "CrossProjectCloner",
]
