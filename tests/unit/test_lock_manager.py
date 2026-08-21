"""Unit tests for Layer 4 LockManager(P1-a,主文档 §10.1/§10.2).

覆盖:
- 兼容性矩阵: SHARED 共享并发 / EXCLUSIVE 互斥 / UPDATE 互斥
- 超时回退(死锁预防兜底)
- FIFO 唤醒 + 连续 SHARED 批量放行
- UPDATE → EXCLUSIVE 升级(UPDATE 与其他锁均不兼容,持有者为唯一持有者,
  因此升级总是立即授予;非法升级抛错)
- 锁排序 acquire_many(失败回滚已获取锁)
- release_all / 重入 / 非持有者释放
- P1 退出标准: 锁序列化下两条并发链无 lost update
"""

from __future__ import annotations

import asyncio

import pytest

from smilex.memory.concurrency import (
    LockError,
    LockHandle,
    LockManager,
    LockTimeoutError,
    is_compatible,
)
from smilex.memory.models import LockType

RES = "entity:global:e1"
TIMEOUT = 0.05  # 测试用短超时


@pytest.fixture
def lm() -> LockManager:
    return LockManager()


# ---------- 兼容性矩阵 ----------

def test_compatibility_matrix():
    """仅 SHARED+SHARED 兼容(模块文档 05 §3.1)."""
    assert is_compatible(LockType.SHARED, LockType.SHARED)
    for held in LockType:
        for req in LockType:
            if (held, req) != (LockType.SHARED, LockType.SHARED):
                assert not is_compatible(held, req)


async def test_shared_locks_concurrent(lm: LockManager):
    """多个 SHARED 可同时持有."""
    async with lm.shared(RES, holder="a"), lm.shared(RES, holder="b"):
        assert lm.holders_of(RES) == {
            "a": LockType.SHARED,
            "b": LockType.SHARED,
        }
    assert not lm.is_locked(RES)  # 全部释放后状态回收


async def test_exclusive_blocks_shared(lm: LockManager):
    """EXCLUSIVE 持有期间 SHARED 获取超时."""
    async with lm.exclusive(RES, holder="a"):
        with pytest.raises(LockTimeoutError):
            await lm.acquire(RES, LockType.SHARED, holder="b", timeout=TIMEOUT)


async def test_shared_blocks_exclusive(lm: LockManager):
    """SHARED 持有期间 EXCLUSIVE 获取超时."""
    async with lm.shared(RES, holder="a"):
        with pytest.raises(LockTimeoutError):
            await lm.acquire(RES, LockType.EXCLUSIVE, holder="b", timeout=TIMEOUT)


async def test_exclusive_mutual_exclusion(lm: LockManager):
    """两个 EXCLUSIVE 互斥;后者超时抛 LockTimeoutError 并携带上下文."""
    async with lm.exclusive(RES, holder="a"):
        with pytest.raises(LockTimeoutError) as exc_info:
            await lm.acquire(RES, LockType.EXCLUSIVE, holder="b", timeout=TIMEOUT)
    assert exc_info.value.resource == RES
    assert exc_info.value.holder == "b"


async def test_update_locks_mutual_exclusion(lm: LockManager):
    """UPDATE 互斥(防双 UPDATE 互等死锁,模块文档 05 §3.2)."""
    async with lm.update(RES, holder="a"):
        with pytest.raises(LockTimeoutError):
            await lm.acquire(RES, LockType.UPDATE, holder="b", timeout=TIMEOUT)


async def test_update_blocks_shared(lm: LockManager):
    """UPDATE 与 SHARED 不兼容."""
    async with lm.update(RES, holder="a"):
        with pytest.raises(LockTimeoutError):
            await lm.acquire(RES, LockType.SHARED, holder="b", timeout=TIMEOUT)


# ---------- 释放 / 唤醒 ----------

async def test_release_wakes_waiter_fifo(lm: LockManager):
    """释放后按 FIFO 顺序唤醒等待者."""
    order: list[str] = []

    async def waiter(holder: str):
        async with lm.exclusive(RES, holder=holder):
            order.append(holder)

    t_a = asyncio.create_task(waiter("a"))
    await asyncio.sleep(0)  # 让 a 先拿到锁
    t_b = asyncio.create_task(waiter("b"))
    t_c = asyncio.create_task(waiter("c"))
    await asyncio.gather(t_a, t_b, t_c)
    assert order == ["a", "b", "c"]


async def test_release_wakes_consecutive_shared_batch(lm: LockManager):
    """EXCLUSIVE 释放后,队首连续 SHARED 批量放行."""
    async with lm.exclusive(RES, holder="x"):
        t1 = asyncio.create_task(lm.acquire(RES, LockType.SHARED, holder="s1"))
        t2 = asyncio.create_task(lm.acquire(RES, LockType.SHARED, holder="s2"))
        await asyncio.sleep(0)  # 两个等待者入队
    h1, h2 = await asyncio.gather(t1, t2)
    assert lm.holders_of(RES) == {"s1": LockType.SHARED, "s2": LockType.SHARED}
    await h1.release()
    await h2.release()


async def test_release_by_non_holder_raises(lm: LockManager):
    """资源有其他持有者时,未持有者释放抛 LockError(状态已整体回收则幂等)."""
    async with lm.shared(RES, holder="a"):
        with pytest.raises(LockError, match="未持有"):
            await lm.release(LockHandle(RES, "nobody", LockType.SHARED))
    # 状态已回收后再释放 → 幂等不报错(如升级后的旧句柄)
    await lm.release(LockHandle(RES, "nobody", LockType.SHARED))


async def test_reentrant_same_type(lm: LockManager):
    """同 holder 同类型重入直接返回;任一 release 完全释放,重复释放幂等."""
    h1 = await lm.acquire(RES, LockType.SHARED, holder="a")
    h2 = await lm.acquire(RES, LockType.SHARED, holder="a")
    await h1.release()
    assert not lm.is_locked(RES)
    await h2.release()  # 幂等,不报错


async def test_reentrant_incompatible_raises(lm: LockManager):
    """已持 SHARED 再请求 UPDATE → LockError."""
    async with lm.shared(RES, holder="a"):
        with pytest.raises(LockError, match="已持有"):
            await lm.acquire(RES, LockType.UPDATE, holder="a")


async def test_release_all(lm: LockManager):
    """release_all 释放 holder 的全部锁."""
    await lm.acquire("r1", LockType.EXCLUSIVE, holder="a")
    await lm.acquire("r2", LockType.SHARED, holder="a")
    await lm.release_all("a")
    assert not lm.is_locked("r1")
    assert not lm.is_locked("r2")


# ---------- 升级 ----------

async def test_upgrade_update_to_exclusive(lm: LockManager):
    """UPDATE 持有者为唯一持有者,升级立即授予."""
    handle = await lm.acquire(RES, LockType.UPDATE, holder="a")
    upgraded = await lm.upgrade(handle, LockType.EXCLUSIVE)
    assert upgraded.lock_type is LockType.EXCLUSIVE
    assert lm.holders_of(RES) == {"a": LockType.EXCLUSIVE}
    await upgraded.release()
    assert not lm.is_locked(RES)


async def test_upgrade_via_acquire_reentrant(lm: LockManager):
    """持有 UPDATE 时再 acquire EXCLUSIVE 等效 upgrade."""
    async with lm.update(RES, holder="a"):
        upgraded = await lm.acquire(RES, LockType.EXCLUSIVE, holder="a")
        assert upgraded.lock_type is LockType.EXCLUSIVE
        await upgraded.release()
        assert not lm.is_locked(RES)


async def test_upgrade_invalid_type_raises(lm: LockManager):
    """仅支持 UPDATE → EXCLUSIVE."""
    handle = await lm.acquire(RES, LockType.SHARED, holder="a")
    with pytest.raises(LockError, match="UPDATE"):
        await lm.upgrade(handle, LockType.EXCLUSIVE)
    await handle.release()


# ---------- 锁排序 ----------

async def test_acquire_many_sorted_and_rollback(lm: LockManager):
    """acquire_many 按字典序获取;中途超时释放已获取锁."""
    await lm.acquire("res-b", LockType.EXCLUSIVE, holder="other")
    with pytest.raises(LockTimeoutError):
        await lm.acquire_many(
            ["res-b", "res-a"], LockType.EXCLUSIVE, holder="a", timeout=TIMEOUT
        )
    # res-a 先获取(字典序),res-b 超时后 res-a 应已回滚
    assert not lm.is_locked("res-a")
    assert lm.holders_of("res-b") == {"other": LockType.EXCLUSIVE}
    await lm.release_all("other")


async def test_concurrent_acquire_many_no_deadlock(lm: LockManager):
    """两条链申请同一组锁: 锁排序保证无死锁,双方最终完成."""
    resources = ["r1", "r2", "r3"]

    async def worker(holder: str):
        handles = await lm.acquire_many(resources, LockType.EXCLUSIVE, holder=holder)
        for h in handles:
            await h.release()
        return holder

    done = await asyncio.gather(worker("a"), worker("b"))
    assert sorted(done) == ["a", "b"]


# ---------- P1 退出标准: 锁序列化无 lost update ----------

async def test_concurrent_chains_no_lost_update_via_lock_serialization(lm: LockManager):
    """两条并发链对同一资源读-改-写: EXCLUSIVE 锁序列化,无丢失更新.

    无锁时 `await asyncio.sleep(0)` 切换会让双方读到相同旧值导致丢失更新。
    """
    counter = {"value": 0}
    iterations = 50

    async def worker(chain: str):
        for _ in range(iterations):
            async with lm.exclusive("counter", holder=chain):
                value = counter["value"]
                await asyncio.sleep(0)  # 强制任务切换
                counter["value"] = value + 1

    await asyncio.gather(worker("chain-a"), worker("chain-b"))
    assert counter["value"] == iterations * 2
