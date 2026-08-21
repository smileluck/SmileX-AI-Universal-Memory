"""Storage size benchmark — P2 退出标准「10 项目/年存储 < 3 GB」(§14.5 / §16.4).

方法(抽样外推,本脚本内说明):
- 实测量级: **1 个项目 × 1 个月** 的数据写入一个文件库,测 .db 文件体积
- 外推: 实测体积 × 120(10 项目 × 12 月),假设各项目/各月数据分布相同
  (线性外推忽略索引非线性,对 SQLite 在小数据量下偏保守/偏大都可能,
  结论以「量级判定」为准 — 与 §16.4 的估算口径一致)
- 数据配比假设(每项目每月,见 N_* 常量):
  500 实体 + 2000 三元组(30% 为已失效历史)+ 2000 时序片段
  (30% 久未更新低重要度;每条片段带 1024 维 float32 向量,决策 D5)
- 对比两组:
  ① 未优化基线: 全部数据留热表,float32 向量全保留
  ② P2 优化后: Archiver 归档冷数据(向量清理 + 片段 embedding 置 NULL,
     predicate 字典编码随写入路径生效)+ VACUUM 回收页
- 断言: 优化后外推体积 < 3 GB(退出标准);同时报告未优化外推供对照

用法:
    uv run python tests/benchmarks/storage_size.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from smilex.memory.lifecycle.embedder import HashEmbedder
from smilex.memory.models import Entity, MemoryScope, Triple
from smilex.memory.quality import Archiver
from smilex.memory.storage.storage_engine import StorageEngine
from smilex.memory.storage.vector_store import VectorStore
from smilex.utils.timeutil import from_iso

# ---- 采样规模(1 项目 × 1 月) ----
N_ENTITIES = 500
N_TRIPLES = 2000
N_FRAGMENTS = 2000
HISTORY_RATIO = 0.3  # 30% 历史三元组(valid_to 超龄)/ 冷片段

EXTRAPOLATE_FACTOR = 120  # 10 项目 × 12 月
LIMIT_GB = 3.0

OLD = "2024-06-01T00:00:00.000000Z"  # 超龄(默认阈值 365/180 天)
RECENT = "2026-08-01T00:00:00.000000Z"
OLD_DT = from_iso(OLD)
RECENT_DT = from_iso(RECENT)
HISTORY_END_DT = from_iso("2024-12-01T00:00:00.000000Z")


async def _seed(db_path: Path) -> None:
    """写入 1 项目 × 1 月的模拟数据(实体/三元组/片段+向量)."""
    engine = StorageEngine(db_path)
    await engine.initialize()
    try:
        store = VectorStore(embedder=HashEmbedder())
        conn = engine.conn
        n_history = int(N_TRIPLES * HISTORY_RATIO)
        n_cold_frag = int(N_FRAGMENTS * HISTORY_RATIO)

        for i in range(N_ENTITIES):
            await engine.write_entity(
                Entity(
                    entity_id=f"concept:e{i}",
                    entity_type="concept",
                    name=f"Entity {i}",
                    scope=MemoryScope.PROJECT,
                ),
                scope_id="bench",
            )
        for i in range(N_TRIPLES):
            is_history = i < n_history
            await engine.write_triple(
                Triple(
                    triple_id=f"tri_{i}",
                    subject_id=f"e{i % N_ENTITIES}",
                    # 少量谓词重复使用 — predicate 字典编码的收益场景
                    predicate=("uses", "depends_on", "knows", "located_in")[i % 4],
                    object_value=f"value {i} with some realistic payload text",
                    scope=MemoryScope.PROJECT,
                    valid_from=OLD_DT if is_history else RECENT_DT,
                    valid_to=HISTORY_END_DT if is_history else None,
                ),
                scope_id="bench",
            )
        for i in range(N_FRAGMENTS):
            is_cold = i < n_cold_frag
            ts = OLD if is_cold else RECENT
            await conn.execute(
                "INSERT INTO temporal_fragments(id, fragment_id, time_start, time_end, "
                "content, scope, layer, importance, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, ?, 'project:bench', 'L1', ?, ?, ?)",
                (
                    f"f{i}",
                    f"frag_{i}",
                    ts,
                    f"bench fragment {i}: agent decision log entry with content",
                    0.2 if is_cold else 0.8,
                    ts,
                    ts,
                ),
            )
        await conn.commit()
        async with engine._engine.transaction() as tconn:
            for i in range(N_FRAGMENTS):
                await store.add_text(
                    tconn, f"bench fragment {i} content", fragment_id=f"f{i}"
                )
    finally:
        await engine.close()


async def _archive_and_vacuum(db_path: Path) -> None:
    """P2 优化: 归档冷数据(清向量)+ VACUUM 回收页."""
    engine = StorageEngine(db_path)
    await engine.initialize()
    try:
        result = await Archiver(engine).archive_expired()
        print(
            f"  归档: triples={result.triples_archived} "
            f"fragments={result.fragments_archived} "
            f"vectors_dropped={result.vectors_dropped}"
        )
        await engine.conn.execute("VACUUM")
        await engine.conn.commit()
    finally:
        await engine.close()


def _db_size(db_path: Path) -> int:
    """主库文件字节数(WAL 已随 close checkpoint)."""
    return os.path.getsize(db_path)


async def run_benchmark() -> tuple[int, int]:
    """返回 (未优化字节, 优化后字节)(1 项目 × 1 月实测)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        baseline_path = Path(tmpdir) / "baseline.db"
        optimized_path = Path(tmpdir) / "optimized.db"

        await _seed(baseline_path)
        baseline = _db_size(baseline_path)

        await _seed(optimized_path)
        await _archive_and_vacuum(optimized_path)
        optimized = _db_size(optimized_path)
        return baseline, optimized


def _gib(n_bytes: float) -> float:
    return n_bytes / (1024**3)


def main() -> int:
    # Windows 控制台默认 GBK,强制 UTF-8 输出避免中文乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    baseline, optimized = asyncio.run(run_benchmark())

    base_ext = baseline * EXTRAPOLATE_FACTOR
    opt_ext = optimized * EXTRAPOLATE_FACTOR
    saving = 1.0 - optimized / baseline if baseline else 0.0

    print("=== 存储体积 benchmark(§14.5: 10 项目/年 < 3 GB)===")
    print(f"采样规模:  1 项目 × 1 月 = {N_ENTITIES} 实体 / "
          f"{N_TRIPLES} 三元组 / {N_FRAGMENTS} 片段(含 1024 维向量)")
    print(f"外推方法:  实测 × {EXTRAPOLATE_FACTOR}(10 项目 × 12 月,线性外推)")
    print(f"未优化:    {baseline / 1e6:.1f} MB → 外推 {_gib(base_ext):.2f} GiB")
    print(f"P2 优化后: {optimized / 1e6:.1f} MB → 外推 {_gib(opt_ext):.2f} GiB")
    print(f"实测节省:  {saving * 100:.1f}%(归档清向量 + VACUUM)")

    ok = _gib(opt_ext) < LIMIT_GB
    verdict = "PASS" if ok else "FAIL"
    print(f"退出标准:  外推 {_gib(opt_ext):.2f} GiB < {LIMIT_GB:.0f} GB → {verdict}")
    print(
        "说明: 归档清向量的体积收益受 sqlite-vec vec0 分块存储粒度限制"
        "(1024 维约 1000 条/chunk,碎片化删除不释放未清空 chunk),"
        "成批归档 + VACUUM 才能完整回收;int8 量化工具已就绪(4x),"
        "生产应用留待后续阶段(见 quality/quantization.py docstring)"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
