"""Checkpoint + 协作协议 — Layer 3b(§9.3 / analyse task-preemption-recovery §2-§3).

三件套:
- Checkpoint: 任务执行状态快照(progress/step/state/cursor),落库 checkpoints 表
- CheckpointStore: checkpoints 表读写(复用现有 007_checkpoints.sql,不改 schema)
- InterruptContext: 任务协作协议 — 任务函数循环中调用
  `await ctx.checkpoint(...)`,GRACEFUL 抢占时在检查点抛 TaskInterruptedError 让出

cursor 列为 BLOB,序列化策略:
- None          → NULL
- bytes         → 原样
- str           → b"s:" + utf-8
- 其他 JSON 值   → b"j:" + json utf-8
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ...utils.ids import generate_id
from ...utils.timeutil import now_utc, to_iso

if TYPE_CHECKING:
    from ..storage.storage_engine import StorageEngine

_STR_PREFIX = b"s:"
_JSON_PREFIX = b"j:"


def _encode_cursor(cursor: Any) -> bytes | None:
    """cursor → BLOB(见模块 docstring 的编码约定)."""
    if cursor is None:
        return None
    if isinstance(cursor, bytes):
        return cursor
    if isinstance(cursor, str):
        return _STR_PREFIX + cursor.encode("utf-8")
    return _JSON_PREFIX + json.dumps(cursor, ensure_ascii=False).encode("utf-8")


def _decode_cursor(blob: bytes | None) -> Any:
    """BLOB → cursor(_encode_cursor 的逆变换)."""
    if blob is None:
        return None
    raw = bytes(blob)
    if raw.startswith(_STR_PREFIX):
        return raw[len(_STR_PREFIX) :].decode("utf-8")
    if raw.startswith(_JSON_PREFIX):
        return json.loads(raw[len(_JSON_PREFIX) :].decode("utf-8"))
    return raw


@dataclass
class Checkpoint:
    """任务检查点(§9.3)— progress/step/state/cursor 四元组."""

    task_id: str
    id: str = field(default_factory=generate_id)
    progress: float = 0.0  # 0.0 ~ 1.0
    step: int = 0  # 当前步数(续传游标语义由任务自定)
    state: dict[str, Any] = field(default_factory=dict)  # JSON 状态快照
    cursor: Any = None  # 游标(bytes/str/JSON 值)


class CheckpointStore:
    """checkpoints 表存取(§9.4)— 每任务保留最新一条(upsert by task_id).

    Args:
        storage: 已 initialize 的 StorageEngine(直接用其 conn,沿用单写者连接)
    """

    def __init__(self, storage: StorageEngine) -> None:
        self._storage = storage

    async def save(self, cp: Checkpoint) -> str:
        """保存检查点(同 task_id 覆盖旧记录),返回 checkpoint id."""
        now = to_iso(now_utc())
        state_json = json.dumps(cp.state, ensure_ascii=False)
        cursor_blob = _encode_cursor(cp.cursor)
        conn = self._storage.conn
        await conn.execute("DELETE FROM checkpoints WHERE task_id = ?", [cp.task_id])
        await conn.execute(
            "INSERT INTO checkpoints(id, task_id, progress, step, state, cursor, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [cp.id, cp.task_id, cp.progress, cp.step, state_json, cursor_blob, now, now],
        )
        await conn.commit()
        return cp.id

    async def load_latest(self, task_id: str) -> Checkpoint | None:
        """读取任务最新检查点(无则 None)."""
        cursor = await self._storage.conn.execute(
            "SELECT id, task_id, progress, step, state, cursor FROM checkpoints "
            "WHERE task_id = ? ORDER BY updated_at DESC LIMIT 1",
            [task_id],
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return Checkpoint(
            id=row["id"],
            task_id=row["task_id"],
            progress=float(row["progress"]),
            step=int(row["step"]),
            state=json.loads(row["state"]),
            cursor=_decode_cursor(row["cursor"]),
        )

    async def delete(self, task_id: str) -> None:
        """删除任务的全部检查点(任务完成/取消后清理)."""
        conn = self._storage.conn
        await conn.execute("DELETE FROM checkpoints WHERE task_id = ?", [task_id])
        await conn.commit()


class TaskInterruptedError(Exception):
    """协作式中断信号 — 在检查点抛出,携带已落库的 Checkpoint."""

    def __init__(self, reason: str, checkpoint: Checkpoint) -> None:
        self.reason = reason
        self.checkpoint = checkpoint
        super().__init__(f"任务在检查点被中断: {reason}")


class InterruptibleRun(Protocol):
    """可注册任务执行函数签名(供 5 类核心任务实现)."""

    async def __call__(self, ctx: InterruptContext, payload: dict[str, Any]) -> Any: ...


class InterruptContext:
    """任务协作协议上下文(§9.5).

    任务函数在执行循环中定期调用:
        async def run(ctx, payload):
            start = ctx.step  # resume 时为断点步数
            for i in range(start, total):
                await do_step(i)
                await ctx.checkpoint(step=i + 1, progress=(i + 1) / total)

    语义:
    - checkpoint(): 落库断点;若已被 GRACEFUL 抢占请求,抛 TaskInterruptedError 让出
    - should_yield: 只读检查(不落库),任务想自定义让出点时用
    """

    def __init__(
        self,
        task_id: str,
        store: CheckpointStore,
        *,
        resume_from: Checkpoint | None = None,
    ) -> None:
        self.task_id = task_id
        self._store = store
        self._preempt_requested = False
        self._resumed = resume_from is not None
        self.last_checkpoint: Checkpoint | None = resume_from

    # ---------- 断点信息(resume 时任务读取) ----------

    @property
    def resumed(self) -> bool:
        """本次执行是否从检查点续传."""
        return self._resumed

    @property
    def step(self) -> int:
        """断点步数(无断点为 0)— 任务据此跳过已完成步骤."""
        return self.last_checkpoint.step if self.last_checkpoint else 0

    @property
    def state(self) -> dict[str, Any]:
        """断点状态快照(无断点为空 dict)."""
        return dict(self.last_checkpoint.state) if self.last_checkpoint else {}

    @property
    def cursor(self) -> Any:
        """断点游标(无断点为 None)."""
        return self.last_checkpoint.cursor if self.last_checkpoint else None

    # ---------- 协作协议(任务主动调用) ----------

    def request_preempt(self) -> None:
        """请求抢占(由调度器调用,任务不直接调用)."""
        self._preempt_requested = True

    def should_yield(self) -> bool:
        """是否被请求让出(只读,不触发中断)."""
        return self._preempt_requested

    async def checkpoint(
        self,
        *,
        step: int,
        progress: float = 0.0,
        state: dict[str, Any] | None = None,
        cursor: Any = None,
    ) -> Checkpoint:
        """保存检查点;若已被请求抢占,保存后抛 TaskInterruptedError 协作让出."""
        cp = Checkpoint(
            task_id=self.task_id,
            progress=progress,
            step=step,
            state=state or {},
            cursor=cursor,
        )
        await self._store.save(cp)
        self.last_checkpoint = cp
        if self._preempt_requested:
            raise TaskInterruptedError("graceful_preempt", cp)
        return cp
