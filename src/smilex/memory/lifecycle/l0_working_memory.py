"""L0WorkingMemory — L0 工作记忆(cachebox LRUCache 进程内缓存).

Per main doc §7.2 + 决策 D4(EXECUTION_PLAN_GAPS.md):
- `cachebox.LRUCache(maxsize=1000)` — 纯 LRU,不启用 TTL
- key = session_id,value = {memory_id: FuzzyMemory}
- maxsize=1000 表示最多 1000 个活跃会话
- 会话过期由应用层主动清理(close_session → clear_session / clear)
- 持久化由 L0SnapshotStore(l0_snapshot.py)负责,本类只管热缓存

线程安全:
- cachebox 内部操作为原子(Rust 实现),单事件循环内无需额外锁
"""

from __future__ import annotations

from cachebox import LRUCache

from ..models import FuzzyMemory, MemoryLayer

DEFAULT_MAXSIZE = 1000


class L0WorkingMemory:
    """L0 会话级工作记忆 — 按 session_id 管理 FuzzyMemory 条目.

    使用方法:
        l0 = L0WorkingMemory()
        l0.put("sess_1", FuzzyMemory(content="用户偏好深色主题"))
        memories = l0.list("sess_1")
        l0.clear_session("sess_1")  # 会话结束时由上层主动调用
    """

    def __init__(self, *, maxsize: int = DEFAULT_MAXSIZE) -> None:
        self._cache: LRUCache = LRUCache(maxsize=maxsize)

    @property
    def maxsize(self) -> int:
        return self._cache.maxsize

    @property
    def session_count(self) -> int:
        """当前缓存的活跃会话数."""
        return len(self._cache)

    def session_ids(self) -> list[str]:
        """所有活跃会话 ID(按 LRU 顺序,最久未用在前)."""
        return list(self._cache.keys())

    # ==================== 条目级操作 ====================

    def put(self, session_id: str, memory: FuzzyMemory) -> str:
        """写入一条记忆到指定会话,返回 memory.id.

        相同 memory.id 重复 put 会覆盖.写入时把 layer 置为 L0,
        并更新 updated_at(touch).
        """
        session = self._cache.get(session_id)
        if session is None:
            session = {}
            self._cache.insert(session_id, session)
        else:
            # 刷新 LRU 热度
            self._cache.insert(session_id, session)
        memory.layer = MemoryLayer.L0_WORKING
        memory.touch()
        session[memory.id] = memory
        return memory.id

    def get(self, session_id: str, memory_id: str) -> FuzzyMemory | None:
        """按 (session_id, memory_id) 读取一条记忆."""
        session = self._cache.get(session_id)
        if session is None:
            return None
        return session.get(memory_id)

    def remove(self, session_id: str, memory_id: str) -> bool:
        """删除一条记忆,返回是否删除成功."""
        session = self._cache.get(session_id)
        if session is None or memory_id not in session:
            return False
        del session[memory_id]
        if not session:
            # 空会话直接清掉,避免占位
            self._cache.pop(session_id, None)
        return True

    # ==================== 会话级操作 ====================

    def list(self, session_id: str) -> list[FuzzyMemory]:
        """列出会话的全部记忆条目(无会话返回空列表)."""
        session = self._cache.get(session_id)
        if session is None:
            return []
        return list(session.values())

    def clear_session(self, session_id: str) -> None:
        """清空指定会话(会话结束/项目卸载时由上层主动调用,决策 D4)."""
        self._cache.pop(session_id, None)

    def clear(self) -> None:
        """清空所有会话(进程退出钩子)."""
        self._cache.clear()

    # ==================== 快照集成 ====================

    def export_session(self, session_id: str) -> list[FuzzyMemory]:
        """导出会话条目供 L0SnapshotStore.save_snapshot 使用."""
        return self.list(session_id)

    def import_session(self, session_id: str, memories: list[FuzzyMemory]) -> int:
        """从 L0SnapshotStore.load_snapshot 结果恢复会话,返回导入条数."""
        count = 0
        for memory in memories:
            self.put(session_id, memory)
            count += 1
        return count


__all__ = ["L0WorkingMemory"]
