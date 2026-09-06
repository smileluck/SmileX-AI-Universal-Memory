"""SQLite 完整性巡检任务(§15.3 SQLite 监控).

``PRAGMA quick_check``(integrity_check 的轻量版: 只查 B-tree 与
索引一致性,不验证 FK 与内容) + 库体积/页统计,结果写入进程级
IntegrityReport 持有者(observability),供 ``smilex_db_integrity_ok``
gauge 与 /api/health 读取;失败打 error 日志但不抛(巡检自身不能
成为故障源)。

quick_check 是原子操作,无法分批让出 — interruptible 照常注册,
但巡检期间不产生检查点(单库 MB 级耗时毫秒,可接受)。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ....utils.timeutil import now_utc, to_iso
from ...observability.logging import get_logger
from ...observability.telemetry import IntegrityReport, set_integrity_report

if TYPE_CHECKING:
    from ...storage.storage_engine import StorageEngine
    from ..checkpoint import InterruptContext

_logger = get_logger("integrity")


async def db_integrity(
    storage: StorageEngine, ctx: InterruptContext, payload: dict[str, Any]
) -> dict[str, Any]:
    """巡检: quick_check + 库体积/页统计 → IntegrityReport + 统计返回值.

    payload: 无(占位,保持任务签名统一)
    """
    conn = storage.conn
    cur = await conn.execute("PRAGMA quick_check")
    rows = await cur.fetchall()
    messages = [str(r[0]) for r in rows]
    ok = len(messages) == 1 and messages[0].lower() == "ok"

    cur = await conn.execute("PRAGMA page_count")
    row = await cur.fetchone()
    page_count = int(row[0]) if row is not None else 0
    cur = await conn.execute("PRAGMA freelist_count")
    row = await cur.fetchone()
    freelist = int(row[0]) if row is not None else 0

    db_path = Path(storage.db_path)
    db_size = (
        db_path.stat().st_size
        if str(db_path) != ":memory:" and db_path.exists()
        else 0
    )

    set_integrity_report(
        IntegrityReport(
            ok=ok,
            checked_at=to_iso(now_utc()),
            detail="; ".join(messages)[:500],
            db_size_bytes=db_size,
        )
    )
    if not ok:
        _logger.error("db_integrity_failed", detail="; ".join(messages)[:500])

    return {
        "quick_check": "ok" if ok else "failed",
        "db_size_bytes": db_size,
        "page_count": page_count,
        "freelist_count": freelist,
    }
