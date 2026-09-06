"""审计日志 — 变更事件 JSONL 追加(observability 子系统,§15.3).

设计:
- 记录 who/what/when: session_id(who)+ memory_ids/status(what)+ ts(when);
  channel(mcp/api/cli)作为 source 维度(正常写入路径不产生 source_closet)
- **不落原文**: 只记 content_len 与脱敏后内容的 sha256 摘要 — 审计文件
  自身不得成为 PII 泄漏面(与 §15.4 配套)
- 默认只记变更事件(write/init/import/clone/session_close);
  audit_reads=True 时补记 recall(读事件量大,默认关)
- 追加失败只降级不抛(审计不阻塞业务;stderr 记 warning)
- 无句柄生命周期: 每行 open-append-close,单写者进程下量级(写入 QPS)
  开销可忽略,换取零清理成本

文件位置: 默认库文件旁 ``<db_dir>/audit-<db_stem>.jsonl``;":memory:"
库无文件系统语义,默认禁用(可 audit_path 显式指定开启)。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ...utils.timeutil import now_utc, to_iso
from .logging import get_logger

_logger = get_logger("audit")

# 审计记录的变更事件名(固定枚举,文档与测试用)
WRITE_EVENTS = ("write", "init", "import", "clone", "session_close")


def default_audit_path(db_path: str | Path) -> Path | None:
    """库文件路径 → 默认审计文件路径(":memory:" 返回 None)."""
    db = Path(db_path)
    if str(db) == ":memory:":
        return None
    return db.parent / f"audit-{db.stem}.jsonl"


def content_digest(text: str) -> str:
    """内容指纹(sha256 前 16 hex): 用于等值对账,不可逆推原文."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class AuditLogger:
    """JSONL 审计追加器(线程模型: 事件循环内同步调用)."""

    def __init__(self, path: Path | None, *, audit_reads: bool = False) -> None:
        self._path = path
        self.audit_reads = audit_reads

    @property
    def enabled(self) -> bool:
        return self._path is not None

    @property
    def path(self) -> Path | None:
        return self._path

    def log(self, event: str, **fields: Any) -> None:
        """追加一条审计事件;失败降级(stderr warning),不抛.

        通用字段由调用方传入(session_id/scope/memory_ids/status/
        elapsed_ms/content_len/content_sha256/channel 等),本方法只
        补时间戳并保证单行 JSON。
        """
        if not self.enabled:
            return
        record = {"ts": to_iso(now_utc()), "event": event, **fields}
        try:
            assert self._path is not None
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            # 注意: structlog 首位参数名即 event,日志 kwargs 不能再用 event=
            _logger.warning("audit_append_failed", error=str(exc), audit_event=event)
