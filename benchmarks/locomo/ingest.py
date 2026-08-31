"""LoCoMo 对话灌入 — 走 MemoryMiddleware 公开 write() API.

策略(与 longmemeval benchmark 对齐):
- 每个对话样本独立 SQLite db(同对话的所有题目共享,只灌入一次)
- 每个 session 渲染为 "speaker: text",session 日期作 [date: ...] 前缀,
  过长时按轮次切块(~MAX_CHUNK_CHARS),逐块 write 到 GLOBAL scope
- write 走 L0 → 晋升 L1 + BGE-M3 向量 + FTS5 索引(生产真实路径)
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from smilex.memory.lifecycle.embedder import Embedder
from smilex.memory.models import MemoryScope
from smilex.middlewares import MemoryMiddleware, WriteRequest

# 单次写入的 session 文本块上限(字符);与 longmemeval 保持一致
MAX_CHUNK_CHARS = 2000


def iter_sessions(conversation: dict):
    """按 session 编号顺序产出 (session_turns, date_str)."""
    n = 1
    while f"session_{n}" in conversation:
        turns = conversation[f"session_{n}"]
        date = conversation.get(f"session_{n}_date_time", "")
        yield turns, date
        n += 1


def _normalize_date(raw: str) -> str:
    """'1:56 pm on 8 May, 2023' → '8 May, 2023'(去掉时刻,保留日期)."""
    m = re.search(r"on (.+)$", raw)
    return m.group(1).strip() if m else raw.strip()


def chunk_session(turns: list[dict], date: str | None = None) -> list[str]:
    """按轮次把 session 切成不超过 MAX_CHUNK_CHARS 的文本块."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for turn in turns:
        speaker = turn.get("speaker", "speaker")
        text = str(turn.get("text", "")).strip()
        if not text:
            continue
        line = f"{speaker}: {text}"
        if current and size + len(line) > MAX_CHUNK_CHARS:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    if date and chunks:
        chunks[0] = f"[date: {date}]\n{chunks[0]}"
    return chunks


async def ingest_conversation(
    sample: dict, embedder: Embedder, db_path: Path
) -> tuple[MemoryMiddleware, int, float]:
    """把一个 LoCoMo 对话样本灌入独立 db.

    Returns:
        (已初始化的 MemoryMiddleware, 写入块数, 灌入耗时秒)
    """
    mw = MemoryMiddleware(db_path, embedder=embedder, promotion_threshold=0)
    await mw.initialize()
    start = time.perf_counter()
    n_chunks = 0
    try:
        for turns, raw_date in iter_sessions(sample["conversation"]):
            date = _normalize_date(raw_date) if raw_date else None
            for chunk in chunk_session(turns, date=date):
                await mw.write(
                    WriteRequest(
                        scope=MemoryScope.GLOBAL,
                        content=chunk,
                        importance=0.6,
                    ),
                    session_id="locomo",
                    detect_conflicts=False,
                )
                n_chunks += 1
    except Exception:
        await mw.close()
        raise
    return mw, n_chunks, time.perf_counter() - start
