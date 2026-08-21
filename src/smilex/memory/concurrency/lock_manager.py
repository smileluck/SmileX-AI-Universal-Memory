"""LockManager — Layer 4 第 1 层防护:内存锁管理器(主文档 §10.1/§10.2).

设计依据:
- 锁类型: SHARED(读,可共享)/ UPDATE(更新,可升级为 EXCLUSIVE)/
  EXCLUSIVE(写,排他),兼容性矩阵见模块文档 05 §3.1(仅 SHARED+SHARED 兼容)
- 锁粒度: 按记忆/实体 ID(资源字符串,如 "entity:project:p1:e1")
- 死锁预防: 超时回退(默认 30s,模块文档 05 §2.2)+ 锁排序(acquire_many);
  等待图死锁检测留 P2
- 公平性: FIFO 等待队列,释放时按队首唤醒(连续 SHARED 可批量放行)

与 SQLite 单写者约束的关系:
    本锁是**应用层内存锁**(进程内、单事件循环),不是数据库锁。
    SQLite 的写串行化由 WAL + 单连接保证(Layer 1),本锁用于更上层的
    任务链级互斥(如「读-改-写」期间阻止其他链穿插),并配合
    ConflictDetector 的乐观版本检查覆盖跨链 lost update 场景。
    跨进程并发不适用于嵌入式单进程部署(模块文档 05 §2.1 选型)。

使用方法:
    lm = LockManager()
    async with lm.exclusive("entity:e1", holder="chain-a"):
        ...  # 读-改-写
    # 或手动:
    handle = await lm.acquire("entity:e1", LockType.UPDATE, holder="chain-a")
    handle = await lm.upgrade(handle, LockType.EXCLUSIVE)
    await lm.release(handle)
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

from ...utils.timeutil import now_utc
from ..models import LockType

# 锁兼容性矩阵(模块文档 05 §3.1): 仅 SHARED+SHARED 兼容
LOCK_COMPATIBILITY: dict[tuple[LockType, LockType], bool] = {
    (LockType.SHARED, LockType.SHARED): True,
}


def is_compatible(held: LockType, requested: LockType) -> bool:
    """检查已持有锁与请求锁是否兼容(默认不兼容)."""
    return LOCK_COMPATIBILITY.get((held, requested), False)


class LockError(Exception):
    """锁操作错误(如非法升级、重复获取不兼容锁)."""


class LockTimeoutError(LockError):
    """锁获取/升级超时(死锁预防兜底,模块文档 05 §2.2)."""

    def __init__(self, resource: str, holder: str, timeout: float) -> None:
        self.resource = resource
        self.holder = holder
        self.timeout = timeout
        super().__init__(
            f"获取锁超时: resource={resource!r} holder={holder!r} timeout={timeout}s"
        )


@dataclass
class LockHandle:
    """锁句柄 — acquire 返回值,release 凭据;支持 async with."""

    resource: str
    holder: str
    lock_type: LockType
    acquired_at: datetime = field(default_factory=now_utc)
    _manager: LockManager | None = field(default=None, repr=False, compare=False)
    _released: bool = field(default=False, repr=False, compare=False)

    async def release(self) -> None:
        """释放锁(幂等: 重复释放不报错)."""
        if self._released or self._manager is None:
            return
        self._released = True
        await self._manager.release(self)

    async def __aenter__(self) -> LockHandle:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.release()


@dataclass
class _Waiter:
    """等待队列元素(FIFO)."""

    holder: str
    lock_type: LockType
    future: asyncio.Future[None]


@dataclass
class _ResourceState:
    """单个资源的锁状态: 持有者表 + FIFO 等待队列."""

    holders: dict[str, LockType] = field(default_factory=dict)
    queue: deque[_Waiter] = field(default_factory=deque)


class LockManager:
    """内存锁管理器(§10.1 第 1 层防护).

    Args:
        default_timeout: 默认获取超时秒数(死锁预防兜底,None 表示不超时)
    """

    DEFAULT_TIMEOUT = 30.0

    def __init__(self, *, default_timeout: float | None = DEFAULT_TIMEOUT) -> None:
        self.default_timeout = default_timeout
        self._states: dict[str, _ResourceState] = {}

    # ==================== 获取 / 释放 ====================

    async def acquire(
        self,
        resource: str,
        lock_type: LockType,
        *,
        holder: str,
        timeout: float | None = None,
    ) -> LockHandle:
        """获取锁;不可立即授予时进入 FIFO 队列等待,超时抛 LockTimeoutError.

        重入语义(非计数): 同 holder 同类型重复获取直接返回新句柄,
        任一 release 即完全释放;持有 UPDATE 时请求 EXCLUSIVE 走 upgrade;
        其余「已持有 + 请求不兼容类型」抛 LockError。
        """
        timeout = self.default_timeout if timeout is None else timeout
        state = self._states.setdefault(resource, _ResourceState())

        held = state.holders.get(holder)
        if held is not None:
            if held == lock_type:
                return LockHandle(resource, holder, lock_type, _manager=self)
            if held is LockType.UPDATE and lock_type is LockType.EXCLUSIVE:
                handle = LockHandle(resource, holder, held, _manager=self)
                return await self.upgrade(handle, LockType.EXCLUSIVE, timeout=timeout)
            raise LockError(
                f"{holder} 已持有 {resource} 的 {held} 锁,无法再获取 {lock_type}"
            )

        if not state.queue and self._can_grant(state, lock_type):
            state.holders[holder] = lock_type
            return LockHandle(resource, holder, lock_type, _manager=self)

        return await self._enqueue_and_wait(state, resource, lock_type, holder, timeout)

    async def release(self, handle: LockHandle) -> None:
        """释放句柄对应锁并唤醒队首等待者.

        幂等: 资源状态已整体回收(如升级后旧句柄再释放)时不报错;
        资源仍有其他持有者而 holder 不在其中(从未持有)时抛 LockError。
        """
        state = self._states.get(handle.resource)
        if state is None:
            return
        if handle.holder not in state.holders:
            raise LockError(f"{handle.holder} 未持有 {handle.resource} 的锁")
        handle._released = True
        state.holders.pop(handle.holder)
        self._drain(state)
        self._cleanup(handle.resource, state)

    async def release_all(self, holder: str) -> None:
        """释放 holder 持有的全部锁(链结束/异常清理用)."""
        for resource, state in list(self._states.items()):
            if holder in state.holders:
                state.holders.pop(holder)
                self._drain(state)
            # 同时清掉 holder 的等待请求
            state.queue = deque(w for w in state.queue if w.holder != holder)
            self._cleanup(resource, state)

    async def acquire_many(
        self,
        resources: list[str],
        lock_type: LockType,
        *,
        holder: str,
        timeout: float | None = None,
    ) -> list[LockHandle]:
        """按字典序加锁(锁排序防死锁);任一失败时释放已获取锁再抛错."""
        handles: list[LockHandle] = []
        try:
            for resource in sorted(set(resources)):
                handles.append(
                    await self.acquire(resource, lock_type, holder=holder, timeout=timeout)
                )
        except BaseException:
            for handle in handles:
                await handle.release()
            raise
        return handles

    # ==================== 升级 ====================

    async def upgrade(
        self,
        handle: LockHandle,
        new_type: LockType,
        *,
        timeout: float | None = None,
    ) -> LockHandle:
        """UPDATE → EXCLUSIVE 升级(模块文档 05 §3.2).

        升级等待者插入队首(升级优先): UPDATE 互斥保证至多一个升级者,
        插队可避免「两个 UPDATE 互等对方释放 SHARED」类死锁与升级饥饿。
        超时后自动恢复原 UPDATE 持有,不丢锁。
        """
        timeout = self.default_timeout if timeout is None else timeout
        if handle.lock_type is not LockType.UPDATE or new_type is not LockType.EXCLUSIVE:
            raise LockError(f"仅支持 UPDATE → EXCLUSIVE 升级,得到 {handle.lock_type} → {new_type}")
        state = self._states.get(handle.resource)
        if state is None or state.holders.get(handle.holder) is not LockType.UPDATE:
            raise LockError(f"{handle.holder} 未持有 {handle.resource} 的 UPDATE 锁")

        holder = handle.holder
        state.holders.pop(holder)  # 暂退 UPDATE,等待其他持有者释放
        waiter = _Waiter(holder, LockType.EXCLUSIVE, asyncio.get_running_loop().create_future())
        state.queue.appendleft(waiter)
        self._drain(state)  # 无其他持有者时立即授予
        try:
            if timeout is None:
                await waiter.future
            else:
                await asyncio.wait_for(waiter.future, timeout)
        except TimeoutError:
            state.queue = deque(w for w in state.queue if w is not waiter)
            state.holders[holder] = LockType.UPDATE  # 恢复原 UPDATE
            raise LockTimeoutError(handle.resource, holder, timeout) from None
        except asyncio.CancelledError:
            state.queue = deque(w for w in state.queue if w is not waiter)
            state.holders[holder] = LockType.UPDATE  # 恢复原 UPDATE
            raise
        handle._released = True  # 旧句柄失效
        return LockHandle(handle.resource, holder, LockType.EXCLUSIVE, _manager=self)

    # ==================== 上下文管理器 ====================

    @asynccontextmanager
    async def shared(
        self, resource: str, *, holder: str, timeout: float | None = None
    ) -> AsyncIterator[LockHandle]:
        """SHARED 锁上下文(读)."""
        handle = await self.acquire(resource, LockType.SHARED, holder=holder, timeout=timeout)
        try:
            yield handle
        finally:
            await handle.release()

    @asynccontextmanager
    async def exclusive(
        self, resource: str, *, holder: str, timeout: float | None = None
    ) -> AsyncIterator[LockHandle]:
        """EXCLUSIVE 锁上下文(写)."""
        handle = await self.acquire(
            resource, LockType.EXCLUSIVE, holder=holder, timeout=timeout
        )
        try:
            yield handle
        finally:
            await handle.release()

    @asynccontextmanager
    async def update(
        self, resource: str, *, holder: str, timeout: float | None = None
    ) -> AsyncIterator[LockHandle]:
        """UPDATE 锁上下文(读-改-写,写前可 upgrade 为 EXCLUSIVE)."""
        handle = await self.acquire(resource, LockType.UPDATE, holder=holder, timeout=timeout)
        try:
            yield handle
        finally:
            await handle.release()

    # ==================== 状态查询 ====================

    def holders_of(self, resource: str) -> dict[str, LockType]:
        """资源的当前持有者表(诊断/测试用)."""
        state = self._states.get(resource)
        return dict(state.holders) if state else {}

    def is_locked(self, resource: str) -> bool:
        """资源是否有任何持有者."""
        state = self._states.get(resource)
        return bool(state and state.holders)

    # ==================== 内部 ====================

    @staticmethod
    def _can_grant(state: _ResourceState, lock_type: LockType) -> bool:
        """兼容性检查: 无持有者可授予;请求 SHARED 且现有全部 SHARED 可授予."""
        if not state.holders:
            return True
        return all(is_compatible(held, lock_type) for held in state.holders.values())

    async def _enqueue_and_wait(
        self,
        state: _ResourceState,
        resource: str,
        lock_type: LockType,
        holder: str,
        timeout: float | None,
    ) -> LockHandle:
        waiter = _Waiter(holder, lock_type, asyncio.get_running_loop().create_future())
        state.queue.append(waiter)
        try:
            if timeout is None:
                await waiter.future
            else:
                await asyncio.wait_for(waiter.future, timeout)
        except TimeoutError:
            state.queue = deque(w for w in state.queue if w is not waiter)
            self._cleanup(resource, state)
            raise LockTimeoutError(resource, holder, timeout) from None
        except asyncio.CancelledError:
            state.queue = deque(w for w in state.queue if w is not waiter)
            self._cleanup(resource, state)
            raise
        return LockHandle(resource, holder, lock_type, _manager=self)

    def _drain(self, state: _ResourceState) -> None:
        """释放后按 FIFO 唤醒: 队首与当前持有者兼容则授予,连续 SHARED 批量放行."""
        while state.queue and self._can_grant(state, state.queue[0].lock_type):
            waiter = state.queue.popleft()
            state.holders[waiter.holder] = waiter.lock_type
            waiter.future.set_result(None)

    def _cleanup(self, resource: str, state: _ResourceState) -> None:
        """空状态回收,避免内存泄漏."""
        if not state.holders and not state.queue:
            self._states.pop(resource, None)


__all__ = [
    "LOCK_COMPATIBILITY",
    "LockError",
    "LockHandle",
    "LockManager",
    "LockTimeoutError",
    "is_compatible",
]
