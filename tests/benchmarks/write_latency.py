"""Write latency benchmark — L1.15 验收(执行计划 §5.3).

SLA(主文档 §16 / §14.2): 写入 P99 < 50ms(单写者,无并发).

方法:
- 文件数据库 + 生产默认配置(WAL + synchronous=NORMAL + sqlite-vec)
- 预热 100 次后采样 1000 次 write_triple(每次独立事务)
- 输出 P50 / P95 / P99;P99 >= 50ms 时退出码 1

用法:
    uv run python tests/benchmarks/write_latency.py
"""

from __future__ import annotations

import asyncio
import math
import sys
import tempfile
import time
from pathlib import Path

from smilex.memory.models import MemoryScope, Triple
from smilex.memory.storage.storage_engine import StorageEngine

SAMPLES = 1000
WARMUP = 100
P99_SLA_MS = 50.0


def _percentile(sorted_ms: list[float], p: float) -> float:
    """Nearest-rank 百分位(sorted_ms 必须已升序排序)."""
    rank = max(1, math.ceil(p / 100.0 * len(sorted_ms)))
    return sorted_ms[rank - 1]


async def run_benchmark() -> list[float]:
    """预热 + 采样 1000 次 write_triple,返回每次耗时(ms)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = StorageEngine(Path(tmpdir) / "bench.db")
        await engine.initialize()
        try:
            samples: list[float] = []
            for i in range(WARMUP + SAMPLES):
                triple = Triple(
                    triple_id=f"bench_{i}",
                    subject_id="bench_subject",
                    predicate="bench_write",
                    object_value=f"value_{i}",
                    scope=MemoryScope.GLOBAL,
                    relation_type="semantic",
                )
                start = time.perf_counter()
                await engine.write_triple(triple)
                if i >= WARMUP:
                    samples.append((time.perf_counter() - start) * 1000.0)
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

    print("=== Layer 1 写入延迟 benchmark(L1.15)===")
    print(f"样本数:    {len(samples)}(预热 {WARMUP} 次)")
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
