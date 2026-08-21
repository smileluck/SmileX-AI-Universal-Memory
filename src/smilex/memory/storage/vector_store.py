"""VectorStore — sqlite-vec 向量存储(决策 D1 + D5).

Per 主文档 §18 + 模块文档 02 §4.1(vector_store.py,ChromaDB 适配 → 决策 D1 改为 sqlite-vec):
- 文本经 Embedder(决策 D5)编码为 1024 维向量,写入 memory_vectors(vec0 虚拟表)
- vector_links 表记录 memory_vectors.rowid ↔ entities/triples/temporal_fragments 映射
- KNN 检索: vec0 `embedding MATCH ? AND k = ?`,scope 过滤通过 JOIN 业务表实现

线程模型与事务约定同 queries/ 模块:
- 所有函数接收 aiosqlite.Connection,由调用方管理事务(如 SQLiteEngine.transaction())
- 写入函数不主动 commit

使用方法:
    store = VectorStore()  # 默认 HashEmbedder(决策 D5)
    async with engine.transaction() as conn:
        vid = await store.add_text(conn, "alice works at X", entity_id="e1")
    hits = await store.knn_search(conn, "where does alice work", k=5)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiosqlite
import numpy as np
from sqlite_vec import serialize_float32

from ...utils.ids import generate_id
from ...utils.timeutil import now_utc, to_iso
from ..lifecycle.embedder import Embedder, get_embedder
from .queries._scope import build_scope_clause

if TYPE_CHECKING:
    from ..models import ScopeFilter

VECTOR_DB = "sqlite_vec"  # 决策 D1: vector_links.vector_db 固定值
# scope 过滤时的过采样倍数(vec0 只支持 KNN 后过滤,先取 k*倍数再按 scope 裁剪)
_SCOPE_OVERSAMPLE = 5


@dataclass
class VectorSearchResult:
    """KNN 单条命中结果.

    Attributes:
        vector_id: memory_vectors.rowid
        distance: sqlite-vec L2 距离(向量已 L2 归一化,越小越相似)
        link_id: vector_links.id
        entity_id / triple_id / fragment_id: 关联的业务记录 ID(恰有一个非空)
    """

    vector_id: int
    distance: float
    link_id: str
    entity_id: str | None = None
    triple_id: str | None = None
    fragment_id: str | None = None

    @property
    def memory_id(self) -> str:
        """关联的业务记忆 ID(entity_id / triple_id / fragment_id 中首个非空)."""
        for mid in (self.entity_id, self.triple_id, self.fragment_id):
            if mid is not None:
                return mid
        raise ValueError("vector_links 行缺少业务关联(CHECK 约束应阻止此情况)")


def _as_f32(vector: np.ndarray | list[float], dimension: int) -> np.ndarray:
    """校验并转换为一维 float32 向量."""
    vec = np.asarray(vector, dtype=np.float32)
    if vec.ndim != 1 or vec.shape[0] != dimension:
        raise ValueError(
            f"向量维度不匹配: 期望 ({dimension},),得到 shape {vec.shape}"
        )
    return vec


class VectorStore:
    """sqlite-vec 向量存储 — 写入 / KNN 检索 / 删除.

    Args:
        embedder: 文本编码器(决策 D5),None 时使用 get_embedder() 默认值
            (HashEmbedder,零依赖;仅确定性,无语义相似度保证)
    """

    def __init__(self, embedder: Embedder | None = None) -> None:
        self.embedder: Embedder = embedder or get_embedder()

    @property
    def dimension(self) -> int:
        """向量维度(与 embedder 一致,决策 D5 默认 1024)."""
        return self.embedder.dimension

    # ==================== 写入 ====================

    async def add_vector(
        self,
        conn: aiosqlite.Connection,
        vector: np.ndarray | list[float],
        *,
        entity_id: str | None = None,
        triple_id: str | None = None,
        fragment_id: str | None = None,
    ) -> int:
        """写入向量到 memory_vectors + vector_links 映射,返回 memory_vectors.rowid.

        Args:
            conn: aiosqlite 连接(事务由调用方管理)
            vector: shape == (self.dimension,) 的向量(应已 L2 归一化)
            entity_id / triple_id / fragment_id: 业务关联,至少给一个

        Raises:
            ValueError: 维度不匹配,或三个业务关联全为 None
        """
        if entity_id is None and triple_id is None and fragment_id is None:
            raise ValueError("entity_id / triple_id / fragment_id 至少给一个")
        vec = _as_f32(vector, self.dimension)
        blob = serialize_float32(vec.tolist())

        cursor = await conn.execute(
            "INSERT INTO memory_vectors(embedding) VALUES (?)",
            [blob],
        )
        vector_id = int(cursor.lastrowid)

        await conn.execute(
            "INSERT INTO vector_links(id, vector_db, vector_id, entity_id, "
            "triple_id, fragment_id, dimension, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                generate_id(),
                VECTOR_DB,
                vector_id,
                entity_id,
                triple_id,
                fragment_id,
                self.dimension,
                to_iso(now_utc()),
            ),
        )
        return vector_id

    async def add_text(
        self,
        conn: aiosqlite.Connection,
        text: str,
        *,
        entity_id: str | None = None,
        triple_id: str | None = None,
        fragment_id: str | None = None,
    ) -> int:
        """文本经 Embedder 编码后写入,返回 memory_vectors.rowid."""
        return await self.add_vector(
            conn,
            self.embedder.embed(text),
            entity_id=entity_id,
            triple_id=triple_id,
            fragment_id=fragment_id,
        )

    # ==================== 检索 ====================

    async def knn_search(
        self,
        conn: aiosqlite.Connection,
        query: str | np.ndarray | list[float],
        *,
        k: int = 10,
        scope_filter: ScopeFilter | None = None,
    ) -> list[VectorSearchResult]:
        """KNN 语义检索(§6.4): 返回按 distance 升序的 Top-k.

        Args:
            conn: aiosqlite 连接
            query: 查询文本(经 Embedder 编码)或已编码向量
            k: 返回条数上限
            scope_filter: 作用域过滤 — 通过 vector_links JOIN
                entities/triples/temporal_fragments 的 scope 列实现.
                注意: vec0 只支持「先 KNN 后过滤」,带 scope 过滤时会
                过采样 k * 5 再裁剪,结果可能少于 k 条.

        Returns:
            VectorSearchResult 列表,distance 升序(越小越相似)
        """
        if k <= 0:
            raise ValueError(f"k 必须为正整数,得到 {k!r}")
        if isinstance(query, str):
            vec = self.embedder.embed(query)
        else:
            vec = _as_f32(query, self.dimension)
        blob = serialize_float32(vec.tolist())

        scope_clause, scope_params = build_scope_clause(
            scope_filter,
            column="COALESCE(e.scope, t.scope, f.scope)",
        )
        scope_where = f"WHERE {scope_clause}" if scope_clause else ""
        k_eff = k * _SCOPE_OVERSAMPLE if scope_filter is not None else k

        cursor = await conn.execute(
            "SELECT knn.vector_id, knn.distance, "
            "l.id AS link_id, l.entity_id, l.triple_id, l.fragment_id "
            "FROM ("
            "    SELECT vector_id, distance FROM memory_vectors "
            "    WHERE embedding MATCH ? AND k = ?"
            ") knn "
            "JOIN vector_links l ON l.vector_id = knn.vector_id "
            "AND l.vector_db = ? "
            "LEFT JOIN entities e ON e.id = l.entity_id "
            "LEFT JOIN triples t ON t.id = l.triple_id "
            "LEFT JOIN temporal_fragments f ON f.id = l.fragment_id "
            f"{scope_where} "
            "ORDER BY knn.distance "
            "LIMIT ?",
            [blob, k_eff, VECTOR_DB, *scope_params, k],
        )
        rows = await cursor.fetchall()
        return [
            VectorSearchResult(
                vector_id=int(r["vector_id"]),
                distance=float(r["distance"]),
                link_id=r["link_id"],
                entity_id=r["entity_id"],
                triple_id=r["triple_id"],
                fragment_id=r["fragment_id"],
            )
            for r in rows
        ]

    # ==================== 删除 ====================

    async def delete(
        self,
        conn: aiosqlite.Connection,
        *,
        vector_id: int | None = None,
        entity_id: str | None = None,
        triple_id: str | None = None,
        fragment_id: str | None = None,
    ) -> int:
        """删除向量及映射(memory_vectors 行 + vector_links 行).

        四个条件恰好给一个;按业务 ID 删除时会清掉其全部向量.

        Returns:
            删除的 vector_links 行数(无匹配时 0)

        Raises:
            ValueError: 条件不是恰好一个
        """
        criteria = {
            "vector_id": vector_id,
            "entity_id": entity_id,
            "triple_id": triple_id,
            "fragment_id": fragment_id,
        }
        given = {k_: v for k_, v in criteria.items() if v is not None}
        if len(given) != 1:
            raise ValueError(
                "vector_id / entity_id / triple_id / fragment_id 恰好给一个"
            )
        column, value = next(iter(given.items()))

        cursor = await conn.execute(
            f"SELECT vector_id FROM vector_links WHERE vector_db = ? AND {column} = ?",
            [VECTOR_DB, value],
        )
        vector_ids = [int(r["vector_id"]) for r in await cursor.fetchall()]
        if not vector_ids:
            return 0

        placeholders = ",".join("?" for _ in vector_ids)
        await conn.execute(
            f"DELETE FROM vector_links WHERE vector_db = ? "
            f"AND vector_id IN ({placeholders})",
            [VECTOR_DB, *vector_ids],
        )
        await conn.execute(
            f"DELETE FROM memory_vectors WHERE vector_id IN ({placeholders})",
            vector_ids,
        )
        return len(vector_ids)


__all__ = [
    "VECTOR_DB",
    "VectorSearchResult",
    "VectorStore",
]
