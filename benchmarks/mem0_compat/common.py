"""mem0_compat 公共件 — cutoffs 切片 / 分层采样 / 指标 / db 全文读取.

mem0 协议核心: 一次检索 top-200,在 10/20/50/200 四个截断点各评一次
(答案生成 + 判卷独立),指标按 cutoff 独立计算,headline = 最大 cutoff.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

BENCH_DIR = Path(__file__).parent.parent
DEFAULT_CUTOFFS = [10, 20, 50, 200]
TOP_K = 200  # 检索深度(与 mem0 --top-k 200 一致)


def import_from(path: Path, name: str):
    """从指定文件加载模块(各 benchmark 目录同名模块多,不能共用 sys.path)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_llm_client():
    """复用 longmemeval 的 llm_client(GLM 兼容 / SMILEX_BENCH_* 环境变量)."""
    return import_from(BENCH_DIR / "longmemeval" / "llm_client.py", "m0c_llm_client")


# ---------- db 全文读取(MemoryRef.snippet 截断 120 字符不够作答) ----------

def anchor_times(db_path: Path, dated_chunks: list[tuple[str, str]]) -> int:
    """把块的时间锚定到真实 session 日期(mem0 同款时间序呈现所需).

    write() 路径中 time_start 取墙钟 created_at,WriteRequest.time_range 不会
    落到 L1 表,故灌入后按 content 精确匹配回填 time_start.
    dated_chunks: [(块文本, ISO 日期)],同 session 的所有块共享日期.
    """
    conn = sqlite3.connect(db_path)
    n = 0
    try:
        for chunk, iso in dated_chunks:
            cur = conn.execute(
                "UPDATE temporal_fragments SET time_start = ? WHERE content = ?",
                (iso, chunk),
            )
            n += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return n


def load_fragment_rows(db_path: Path) -> dict[str, dict]:
    """读取全部 L1 fragment(每题检索前调一次,按 id 索引)."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, fragment_id, content, created_at, time_start "
            "FROM temporal_fragments WHERE layer = 'L1'"
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for rid, frag_id, content, created_at, time_start in rows:
        rec = {
            "memory": content,
            "created_at": created_at or "",
            "time_start": time_start or "",
        }
        out[rid] = rec
        out.setdefault(frag_id, rec)
    return out


def ranked_memories(resp, rows: dict[str, dict]) -> list[dict]:
    """recall 响应 → 按融合排名序的 memory 记录列表({memory, created_at})."""
    out: list[dict] = []
    for src in resp.sources:
        rec = rows.get(src.id)
        if rec is not None:
            out.append(rec)
    return out


# ---------- 记忆格式化(mem0 同款: 时间序 / 不显示 score) ----------

def _to_human_date(iso_str: str) -> str:
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(
                iso_str[:26].rstrip("Z"), fmt.replace("%z", "")
            ).strftime("%A, %B %d, %Y")
        except ValueError:
            continue
    return iso_str[:10]


def format_memories_chronological(memories: list[dict]) -> str:
    """LoCoMo 用: 全部记忆按时间升序排列,行格式 '(date) memory'."""
    if not memories:
        return "(No relevant memories found)"
    lines = [
        "The following memories are presented in chronological order "
        "(oldest to newest).",
        "",
    ]
    for rec in sorted(
        memories, key=lambda x: x.get("time_start", "") or x.get("created_at", "")
    ):
        ts = rec.get("time_start", "") or rec.get("created_at", "")
        if ts:
            lines.append(f"({_to_human_date(ts)}) {rec['memory']}")
        else:
            lines.append(f"(unknown date) {rec['memory']}")
    return "\n".join(lines)


def format_memories_grouped_by_date(memories: list[dict]) -> str:
    """LongMemEval 用: 按日期分组(时间序取自 time_start/created_at)."""
    if not memories:
        return "(No relevant memories found)"
    lines: list[str] = []
    current_date = None
    for rec in sorted(memories, key=lambda x: x.get("time_start", "") or x.get("created_at", "")):
        ts = rec.get("time_start", "") or rec.get("created_at", "")
        if ts:
            date_str = _to_human_date(ts)
            if date_str != current_date:
                current_date = date_str
                lines.append(f"\n--- {date_str} ---")
            lines.append(f"- {rec['memory']}")
        else:
            lines.append(f"- {rec['memory']}")
    return "\n".join(lines).strip()


# ---------- 采样(mem0 同款: 分层 + 固定种子) ----------

def sample_questions_stratified(samples: list[dict], per_type: int, seed: int,
                                type_key: str = "question_type") -> list[dict]:
    """按题型分层采样: 每题型 per_type 题,random.Random(seed)."""
    by_type: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_type[s.get(type_key, "unknown")].append(s)
    rng = random.Random(seed)
    picked: list[dict] = []
    for qtype in sorted(by_type):
        pool = sorted(by_type[qtype], key=lambda x: x.get("question_id", str(x)))
        picked.extend(rng.sample(pool, min(per_type, len(pool))))
    return picked


# ---------- 指标(mem0 同款: 每 cutoff 独立,headline = 最大 cutoff) ----------

def compute_metrics_by_cutoff(
    records: list[dict], cutoffs: list[int], type_key: str
) -> dict[int, dict]:
    """records 每条含 {cutoffs: {c: bool}, type_key: 题型}."""
    out: dict[int, dict] = {}
    for c in cutoffs:
        flags_by_type: dict[str, list[bool]] = defaultdict(list)
        for rec in records:
            flags_by_type[rec[type_key]].append(bool(rec["cutoffs"].get(str(c), False)))
        total = [v for flags in flags_by_type.values() for v in flags]
        out[c] = {
            "n": len(total),
            "overall": sum(total) / len(total) * 100 if total else 0.0,
            "by_type": {
                t: {"n": len(f), "acc": sum(f) / len(f) * 100}
                for t, f in sorted(flags_by_type.items())
            },
        }
    return out


def render_report(title: str, metrics: dict[int, dict], cutoffs: list[int],
                  models_line: str, elapsed_s: float) -> str:
    lines = [title, models_line]
    for c in cutoffs:
        m = metrics[c]
        tag = " (headline)" if c == cutoffs[-1] else ""
        lines.append(
            f"\n== cutoff {c}{tag} — n={m['n']}, Overall={m['overall']:.1f} % =="
        )
        for t, tm in m["by_type"].items():
            lines.append(f"    {t:<24} {tm['n']:>3} 题  {tm['acc']:.1f} %")
    lines.append(f"\nTotal Time {elapsed_s / 60:.1f} min")
    return "\n".join(lines)


def save_results(stem_parts: list[str], report: str, records: list[dict]) -> Path:
    results = Path(__file__).parent / "results"
    results.mkdir(parents=True, exist_ok=True)
    import time
    stem = "_".join(stem_parts) + f"_{time.strftime('%Y%m%d_%H%M%S')}"
    (results / f"{stem}.json").write_text(
        json.dumps({"report": report, "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md = results / f"{stem}.md"
    md.write_text(f"```\n{report}\n```\n", encoding="utf-8")
    return md


def reopen_stdout() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
