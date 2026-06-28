"""StorageEngine — Layer 1 对外契约(§6.6)的面向对象封装.

把 SQLiteEngine + queries 模块 + Layer 0 models 组合成统一 API:
- 写入: write_entity / write_triple / write_location
- 读取: get_entity / get_triple
- 检索: query_at_time / find_path / hybrid_search / ...(委托 queries 模块)
- 当前状态: get_current_state(读取 project_current_state 触发器维护的汇总)

scope 适配(决策 D6):
- Layer 0 MemoryScope 枚举 + scope_id → Layer 1 全路径字符串
- 用 scope_path() / parse_scope_path() 工具函数互转
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import numpy as np

from ...utils.timeutil import to_iso
from ..models import CertaintyLevel, Entity, MemoryScope, Triple
from . import queries
from .sqlite_engine import SQLiteEngine


def scope_path(scope: MemoryScope, scope_id: str | None = None) -> str:
    """Layer 0 (scope enum + scope_id) → Layer 1 全路径字符串.

    Examples:
        scope_path(MemoryScope.GLOBAL) → "global"
        scope_path(MemoryScope.PROJECT, "proj_a") → "project:proj_a"
        scope_path(MemoryScope.TENANT, "t1") → "tenant:t1"
    """
    if scope is MemoryScope.GLOBAL:
        return "global"
    if scope is MemoryScope.PROJECT:
        if not scope_id:
            raise ValueError("PROJECT scope 需要 scope_id")
        return f"project:{scope_id}"
    if scope is MemoryScope.TENANT:
        if not scope_id:
            raise ValueError("TENANT scope 需要 scope_id")
        return f"tenant:{scope_id}"
    raise ValueError(f"未知 MemoryScope: {scope!r}")


def parse_scope_path(s: str) -> tuple[MemoryScope, str | None]:
    """Layer 1 全路径字符串 → Layer 0 (scope enum, scope_id).

    Examples:
        parse_scope_path("global") → (MemoryScope.GLOBAL, None)
        parse_scope_path("project:proj_a") → (MemoryScope.PROJECT, "proj_a")
    """
    if s == "global":
        return MemoryScope.GLOBAL, None
    if s.startswith("project:"):
        return MemoryScope.PROJECT, s[len("project:"):]
    if s.startswith("tenant:"):
        return MemoryScope.TENANT, s[len("tenant:"):]
    raise ValueError(f"无法解析 scope 路径: {s!r}")


class StorageEngine:
    """Layer 1 对外契约 — 封装 SQLite + 查询模块,提供面向对象 API.

    使用方法:
        engine = StorageEngine("~/.smilex/data.db")
        await engine.initialize()
        eid = await engine.write_entity(entity, scope_id="proj_a")
        path = await engine.find_path("e1", "e2")
        await engine.close()

    线程安全: 同 SQLiteEngine(单实例在单事件循环内使用).
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        pragmas: dict | None = None,
        load_vec: bool = True,
    ) -> None:
        self._engine = SQLiteEngine(db_path, pragmas=pragmas, load_vec=load_vec)

    async def initialize(self) -> None:
        await self._engine.initialize()

    async def close(self) -> None:
        await self._engine.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        """底层 aiosqlite 连接(高级用户可直接执行 SQL)."""
        return self._engine.conn

    @property
    def is_initialized(self) -> bool:
        return self._engine.is_initialized

    # ==================== 写入 ====================

    async def write_entity(
        self,
        entity: Entity,
        *,
        scope_id: str | None = None,
    ) -> str:
        """写入 Entity,返回 id.

        Args:
            entity: Layer 0 Entity 实例
            scope_id: 项目/租户 ID(当 scope 是 PROJECT/TENANT 时必需)
        """
        scope_str = scope_path(entity.scope, scope_id)
        async with self._engine.transaction() as conn:
            await conn.execute(
                "INSERT INTO entities(id, entity_id, entity_type, name, scope, "
                "valid_from, valid_to, embedding, source_closet) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity.id,
                    entity.entity_id,
                    entity.entity_type,
                    entity.name,
                    scope_str,
                    to_iso(entity.valid_from),
                    to_iso(entity.valid_to) if entity.valid_to else None,
                    json.dumps(entity.embedding.tolist())
                    if entity.embedding is not None
                    else None,
                    entity.source_closet,
                ),
            )
        return entity.id

    async def write_triple(
        self,
        triple: Triple,
        *,
        scope_id: str | None = None,
    ) -> str:
        """写入 Triple,返回 id. 会触发 project_current_state 触发器(§6.3)."""
        scope_str = scope_path(triple.scope, scope_id)
        async with self._engine.transaction() as conn:
            await conn.execute(
                "INSERT INTO triples(id, triple_id, subject_id, predicate, "
                "object_id, object_value, scope, valid_from, valid_to, "
                "predecessor_id, causal_level, confidence, certainty, "
                "relation_type, source_closet) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    triple.id,
                    triple.triple_id,
                    triple.subject_id,
                    triple.predicate,
                    triple.object_id,
                    triple.object_value,
                    scope_str,
                    to_iso(triple.valid_from),
                    to_iso(triple.valid_to) if triple.valid_to else None,
                    triple.predecessor_id,
                    triple.causal_level,
                    triple.confidence,
                    str(triple.certainty),
                    triple.relation_type,
                    triple.source_closet,
                ),
            )
        return triple.id

    async def write_location(
        self,
        *,
        id: str,
        location_id: str,
        name: str,
        location_type: str,
        path: str,
        coordinates: tuple[float, float] | None = None,
        parent_id: str | None = None,
        scope: str = "global",
        valid_from: str | None = None,
    ) -> int:
        """写入 Location + R-tree(委托 queries.upsert_location_with_rtree)."""
        async with self._engine.transaction() as conn:
            rowid = await queries.upsert_location_with_rtree(
                conn,
                id=id,
                location_id=location_id,
                name=name,
                location_type=location_type,
                path=path,
                coordinates=coordinates,
                parent_id=parent_id,
                scope=scope,
                valid_from=valid_from,
            )
        return rowid

    # ==================== 读取 ====================

    async def get_entity(self, entity_id: str) -> Entity | None:
        """按 id 读取 Entity(返回 Layer 0 实例)."""
        cursor = await self.conn.execute(
            "SELECT * FROM entities WHERE id = ?",
            [entity_id],
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_entity(row)

    async def get_triple(self, triple_id: str) -> Triple | None:
        """按 id 读取 Triple(返回 Layer 0 实例)."""
        cursor = await self.conn.execute(
            "SELECT * FROM triples WHERE id = ?",
            [triple_id],
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_triple(row)

    def _row_to_entity(self, row) -> Entity:
        """aiosqlite.Row → Entity(读取时 scope_id 信息丢失,scope 退化为枚举)."""
        scope_enum, _ = parse_scope_path(row["scope"])
        emb = None
        if row["embedding"]:
            emb = np.array(json.loads(row["embedding"]), dtype=np.float32)
        return Entity(
            id=row["id"],
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            name=row["name"],
            scope=scope_enum,
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            embedding=emb,
            source_closet=row["source_closet"],
        )

    def _row_to_triple(self, row) -> Triple:
        """aiosqlite.Row → Triple."""
        scope_enum, _ = parse_scope_path(row["scope"])
        return Triple(
            id=row["id"],
            triple_id=row["triple_id"],
            subject_id=row["subject_id"],
            predicate=row["predicate"],
            object_id=row["object_id"],
            object_value=row["object_value"],
            scope=scope_enum,
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            predecessor_id=row["predecessor_id"],
            causal_level=row["causal_level"],
            confidence=row["confidence"],
            certainty=CertaintyLevel(row["certainty"]),
            relation_type=row["relation_type"],
            source_closet=row["source_closet"],
        )

    # ==================== 检索(委托 queries 模块) ====================

    async def query_at_time(self, t, *, table="triples", scope_filter=None):
        return await queries.query_at_time(self.conn, t, table=table, scope_filter=scope_filter)

    async def query_in_range(self, start, end, *, table="triples", scope_filter=None):
        return await queries.query_in_range(
            self.conn, start, end, table=table, scope_filter=scope_filter
        )

    async def build_entity_timeline(
        self, entity_id, *, include_as_subject=True, include_as_object=True,
        scope_filter=None,
    ):
        return await queries.build_entity_timeline(
            self.conn, entity_id,
            include_as_subject=include_as_subject,
            include_as_object=include_as_object,
            scope_filter=scope_filter,
        )

    async def query_in_area(self, lng, lat, radius_m, *, scope_filter=None):
        return await queries.query_in_area(
            self.conn, lng, lat, radius_m, scope_filter=scope_filter
        )

    async def query_in_location(self, path, *, include_children=True, scope_filter=None):
        return await queries.query_in_location(
            self.conn, path,
            include_children=include_children,
            scope_filter=scope_filter,
        )

    async def find_path(self, src, dst, *, max_depth=5, scope_filter=None):
        return await queries.find_path(
            self.conn, src, dst, max_depth=max_depth, scope_filter=scope_filter
        )

    async def find_n_degree_relations(
        self, entity_id, *, max_depth=2, scope_filter=None
    ):
        return await queries.find_n_degree_relations(
            self.conn, entity_id, max_depth=max_depth, scope_filter=scope_filter
        )

    async def trace_causal_chain(
        self, triple_id, *, direction="backward", max_depth=20, scope_filter=None
    ):
        return await queries.trace_causal_chain(
            self.conn, triple_id,
            direction=direction,
            max_depth=max_depth,
            scope_filter=scope_filter,
        )

    async def hybrid_search(self, query, *, scope_filter=None, top_k=10, rrf_k=60):
        return await queries.hybrid_memory_search(
            self.conn, query,
            scope_filter=scope_filter,
            top_k=top_k,
            rrf_k=rrf_k,
        )

    # ==================== 当前状态(§6.3 触发器维护) ====================

    async def get_current_state(
        self,
        scope: str,
        subject_id: str,
        predicate: str | None = None,
    ) -> list[dict]:
        """读取 project_current_state(触发器维护的当前状态汇总).

        Args:
            scope: 作用域全路径(如 "project:proj_a")
            subject_id: 主体 ID
            predicate: 谓词(None 返回该 subject 的所有当前状态)

        Returns:
            dict 列表,每项含 scope/subject_id/predicate/object_id/object_value/
            valid_from/confidence
        """
        if predicate is None:
            cursor = await self.conn.execute(
                "SELECT * FROM project_current_state "
                "WHERE scope = ? AND subject_id = ? ORDER BY predicate",
                [scope, subject_id],
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM project_current_state "
                "WHERE scope = ? AND subject_id = ? AND predicate = ?",
                [scope, subject_id, predicate],
            )
        return [dict(r) for r in await cursor.fetchall()]


__all__ = [
    "StorageEngine",
    "scope_path",
    "parse_scope_path",
]
