"""Recall latency benchmark — L2.9 验收(执行计划 §5.4).

SLA(主文档 §16 / §14.2): 检索 P99 < 500ms(token budget 4000).

方法:
- 文件数据库 + 生产默认配置(WAL + synchronous=NORMAL + sqlite-vec)
- 预置数据: 50 实体 / 200 triples / 100 fragments(含向量)+ L0 会话(5 条)
- 每次采样 = 一次 ContextBuilder.build_context(L0 + L1 KNN + L2 混合检索,
  token_budget=4000),预热 10 次后采样 100 次
- 输出 P50 / P95 / P99;P99 >= 500ms 时退出码 1

用法:
    uv run python tests/benchmarks/recall_latency.py
"""

from __future__ import annotations

import asyncio
import math
import sys
import tempfile
import time
from pathlib import Path

from smilex.memory.lifecycle import ContextBuilder, HashEmbedder, L0WorkingMemory
from smilex.memory.models import FuzzyMemory, MemoryScope
from smilex.memory.storage.sqlite_engine import SQLiteEngine
from smilex.memory.storage.vector_store import VectorStore

SAMPLES = 100
WARMUP = 10
P99_SLA_MS = 500.0
TOKEN_BUDGET = 4000

N_ENTITIES = 50
N_TRIPLES = 200
N_FRAGMENTS = 100
N_L0_MEMORIES = 5


def _percentile(sorted_ms: list[float], p: float) -> float:
    """Nearest-rank 百分位(sorted_ms 必须已升序排序)."""
    rank = max(1, math.ceil(p / 100.0 * len(sorted_ms)))
    return sorted_ms[rank - 1]


async def _seed(engine: SQLiteEngine, store: VectorStore) -> None:
    """预置实体 / triples / fragments(含向量)."""
    conn = engine.conn
    for i in range(N_ENTITIES):
        await conn.execute(
            "INSERT INTO entities(id, entity_id, entity_type, name, scope, valid_from) "
            "VALUES (?, ?, 'person', ?, 'global', '2025-01-01T00:00:00.000000Z')",
            (f"e{i}", f"person:user_{i}", f"User {i}"),
        )
    for i in range(N_TRIPLES):
        await conn.execute(
            "INSERT INTO triples(id, triple_id, subject_id, predicate, object_id, "
            "scope, valid_from, relation_type) "
            "VALUES (?, ?, ?, 'knows', ?, 'global', ?, 'semantic')",
            (
                f"t{i}",
                f"tri_{i}",
                f"e{i % N_ENTITIES}",
                f"e{(i + 1) % N_ENTITIES}",
                f"2025-{(i % 12) + 1:02d}-15T00:00:00.000000Z",
            ),
        )
    for i in range(N_FRAGMENTS):
        await conn.execute(
            "INSERT INTO temporal_fragments(id, fragment_id, time_start, content, "
            "scope, layer, created_at, updated_at) "
            "VALUES (?, ?, '2025-01-10T00:00:00.000000Z', ?, 'global', 'L1', "
            "'2025-01-10T00:00:00.000000Z', '2025-01-10T00:00:00.000000Z')",
            (f"f{i}", f"frag_{i}", f"user {i} prefers topic number {i}"),
        )
    await conn.commit()
    async with engine.transaction() as tconn:
        for i in range(N_FRAGMENTS):
            await store.add_text(
                tconn, f"user {i} prefers topic number {i}", fragment_id=f"f{i}"
            )


async def run_benchmark() -> list[float]:
    """预热 + 采样 100 次 build_context,返回每次耗时(ms)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = SQLiteEngine(Path(tmpdir) / "bench.db")
        await engine.initialize()
        try:
            store = VectorStore(embedder=HashEmbedder())
            await _seed(engine, store)

            l0 = L0WorkingMemory()
            for i in range(N_L0_MEMORIES):
                l0.put(
                    "bench_session",
                    FuzzyMemory(
                        content=f"bench working memory item {i}",
                        importance=0.5 + i * 0.1,
                        scope=MemoryScope.GLOBAL,
                    ),
                )

            builder = ContextBuilder(l0=l0, vector_store=store)
            samples: list[float] = []
            for i in range(WARMUP + SAMPLES):
                start = time.perf_counter()
                ctx = await builder.build_context(
                    engine.conn,
                    session_id="bench_session",
                    query_text=f"user {i % N_FRAGMENTS} prefers topic",
                    entity_id=f"e{i % N_ENTITIES}",
                    time_range=("2025-01-01T00:00:00.000000Z", "2025-12-31T23:59:59.000000Z"),
                    token_budget=TOKEN_BUDGET,
                )
                if i >= WARMUP:
                    samples.append((time.perf_counter() - start) * 1000.0)
            assert ctx.total_tokens <= ctx.budget
            return samples
        finally:
            await engine.close()


def main() -> int:
    # Windows 控制台默认 GBK,强制 UTF-8 输出避免中文乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    samples = sorted(asyncio.run(run_benchmark()))

    p50 = _percentile(samples, 50)
    p95 = _percentile(samples, 95)
    p99 = _percentile(samples, 99)

    print("=== Layer 2 检索延迟 benchmark(L2.9)===")
    print(f"样本数:    {len(samples)}(预热 {WARMUP} 次)")
    print(f"预算:      {TOKEN_BUDGET} tokens")
    print(f"P50:       {p50:.3f} ms")
    print(f"P95:       {p95:.3f} ms")
    print(f"P99:       {p99:.3f} ms")
    print(f"min/max:   {samples[0]:.3f} / {samples[-1]:.3f} ms")

    ok = p99 < P99_SLA_MS
    verdict = "PASS" if ok else "FAIL"
    print(f"SLA 校验:  P99 < {P99_SLA_MS:.0f}ms → {verdict}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
