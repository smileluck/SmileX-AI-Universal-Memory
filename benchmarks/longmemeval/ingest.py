"""LongMemEval 对话历史灌入 — 走 MemoryMiddleware 公开 write() API.

策略(与 mem0 的 raw ingestion 对齐):
- 每个样本独立临时 SQLite db,互不污染
- 每个 session 渲染为带角色标签的文本("user: ..."/"assistant: ..."),
  过长时按轮次切块(~MAX_CHUNK_CHARS),逐块 write 到 GLOBAL scope
- write 走 L0 → >800 tokens 自动晋升 L1 + BGE-M3 向量(生产真实路径)
"""

from __future__ import annotations

import time
from pathlib import Path

from smilex.memory.lifecycle.embedder import Embedder
from smilex.memory.models import MemoryScope
from smilex.middlewares import MemoryMiddleware, WriteRequest

# 单次写入的 session 文本块上限(字符);实测 2000 在 LongMemEval 上
# 检索粒度与上下文多样性平衡最好(10 sources / ~1.8K tokens)
MAX_CHUNK_CHARS = 2000


def render_session(session: list[dict]) -> str:
    """一个 session(list[{role, content}])→ 带角色标签的纯文本."""
    lines = []
    for turn in session:
        role = turn.get("role", "user")
        content = str(turn.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def chunk_session(session: list[dict], date: str | None = None) -> list[str]:
    """按轮次把 session 切成不超过 MAX_CHUNK_CHARS 的文本块.

    date(haystack_dates 对应项)作为块首前缀写入,供 temporal-reasoning 检索.
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for turn in session:
        line = f"{turn.get('role', 'user')}: {str(turn.get('content', '')).strip()}"
        if current and size + len(line) > MAX_CHUNK_CHARS:
            chunks.append("\n".join(current))
            current, size = [], 0
        if not line.strip().endswith(":"):
            current.append(line)
            size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    if date and chunks:
        chunks[0] = f"[date: {date}]\n{chunks[0]}"
    return chunks


async def ingest_sample(
    sample: dict, embedder: Embedder, db_path: Path
) -> tuple[MemoryMiddleware, int, float]:
    """把一个 LongMemEval 样本的对话历史灌入独立 db.

    Returns:
        (已初始化的 MemoryMiddleware, 写入块数, 灌入耗时秒)
    """
    mw = MemoryMiddleware(db_path, embedder=embedder, promotion_threshold=0)
    await mw.initialize()
    start = time.perf_counter()
    n_chunks = 0
    try:
        sessions = sample.get("haystack_sessions") or sample.get("context") or []
        dates = sample.get("haystack_dates") or []
        for i, session in enumerate(sessions):
            date = dates[i] if i < len(dates) else None
            for chunk in chunk_session(session, date=date):
                await mw.write(
                    WriteRequest(
                        scope=MemoryScope.GLOBAL,
                        content=chunk,
                        importance=0.6,
                    ),
                    session_id="longmemeval",
                    detect_conflicts=False,
                )
                n_chunks += 1
    except Exception:
        await mw.close()
        raise
    return mw, n_chunks, time.perf_counter() - start
