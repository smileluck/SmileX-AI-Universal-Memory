"""L0SnapshotStore — L0 工作记忆的 SQLite 快照持久化.

Per main doc §7.2 + schema 009_memory_l0_snapshot.sql:
- 会话状态用 msgpack(serialization.packb)序列化为 BLOB
- 写入 memory_l0_snapshot 表(session_id PRIMARY KEY,INSERT OR REPLACE 覆盖)
- expires_at 可选(决策 D4 不强用 TTL,过期快照在读取/清理时删除)
- 崩溃恢复: cachebox 丢失时从本表恢复(< 50ms)

使用方法:
    store = L0SnapshotStore(engine)  # SQLiteEngine 或 StorageEngine
    await store.save_snapshot("sess_1", l0.export_session("sess_1"))
    memories = await store.load_snapshot("sess_1")
    l0.import_session("sess_1", memories)
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ...utils.timeutil import now_utc, to_iso
from ..models import FuzzyMemory
from ..models.serialization import packb, unpackb

if TYPE_CHECKING:
    from ..storage.sqlite_engine import SQLiteEngine
    from ..storage.storage_engine import StorageEngine

_TABLE = "memory_l0_snapshot"


class L0SnapshotStore:
    """L0 快照持久化 — 通过 SQLiteEngine/StorageEngine 执行 SQL.

    只依赖 ``conn`` 属性(aiosqlite.Connection),两者都暴露该属性.
    """

    def __init__(self, engine: SQLiteEngine | StorageEngine) -> None:
        self._engine = engine

    # ==================== 写入 ====================

    async def save_snapshot(
        self,
        session_id: str,
        memories: list[FuzzyMemory],
        *,
        expires_at: datetime | None = None,
    ) -> None:
        """保存会话快照(msgpack BLOB,覆盖已有快照).

        Args:
            session_id: 会话 ID
            memories: 该会话的 FuzzyMemory 条目
            expires_at: 可选过期时间(None = 不过期)
        """
        payload = packb([m.to_dict() for m in memories])
        conn = self._engine.conn
        await conn.execute(
            f"INSERT OR REPLACE INTO {_TABLE}(session_id, data, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (
                session_id,
                payload,
                to_iso(now_utc()),
                to_iso(expires_at) if expires_at else None,
            ),
        )
        await conn.commit()

    # ==================== 读取 ====================

    async def load_snapshot(self, session_id: str) -> list[FuzzyMemory] | None:
        """加载会话快照.不存在或已过期返回 None(过期快照顺带删除)."""
        conn = self._engine.conn
        cursor = await conn.execute(
            f"SELECT data, expires_at FROM {_TABLE} WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] <= to_iso(now_utc()):
            await self.delete_snapshot(session_id)
            return None
        data: list[dict] = unpackb(row["data"])
        return [FuzzyMemory.from_dict(d) for d in data]

    # ==================== 删除 ====================

    async def delete_snapshot(self, session_id: str) -> bool:
        """删除会话快照,返回是否删除了行."""
        conn = self._engine.conn
        cursor = await conn.execute(
            f"DELETE FROM {_TABLE} WHERE session_id = ?",
            (session_id,),
        )
        await conn.commit()
        return cursor.rowcount > 0

    async def cleanup_expired(self) -> int:
        """删除所有已过期快照,返回删除行数."""
        conn = self._engine.conn
        cursor = await conn.execute(
            f"DELETE FROM {_TABLE} "
            "WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (to_iso(now_utc()),),
        )
        await conn.commit()
        return cursor.rowcount

    async def list_snapshots(self) -> list[str]:
        """列出所有快照的 session_id(含已过期的,由调用方决定处理)."""
        conn = self._engine.conn
        cursor = await conn.execute(f"SELECT session_id FROM {_TABLE} ORDER BY updated_at")
        return [row["session_id"] for row in await cursor.fetchall()]


__all__ = ["L0SnapshotStore"]
