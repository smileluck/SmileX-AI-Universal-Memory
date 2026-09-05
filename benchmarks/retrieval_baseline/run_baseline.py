"""检索基线对比 — 裸 BGE-M3 向量 KNN vs smilex 记忆管线 recall().

同一份数据、同一种切块,对比两条检索通道的 R@10:
- raw: 切块 → BGE-M3 向量 → 纯余弦 KNN 取 top-k(naive RAG 基线)
- smilex: 切块 → MemoryMiddleware.write(L1 + FTS5)→ recall()
  (向量 KNN + BM25 关键词通道 RRF 融合)

命中判定:
- LongMemEval: gold 为 answer_session_ids;检索结果含任一 gold session 的块即命中
- LoCoMo: gold 为 evidence(dia_id 如 "D1:3");检索结果含任一 evidence 轮次的
  块即命中(adversarial 题语义为"不可回答",不计入检索基线)

用法:
    uv run python benchmarks/retrieval_baseline/run_baseline.py --dataset longmemeval --limit 50
    uv run python benchmarks/retrieval_baseline/run_baseline.py --dataset locomo --limit 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from smilex.memory.lifecycle.embedder import EmbedderConfig, get_embedder
from smilex.memory.models import MemoryScope
from smilex.middlewares import MemoryMiddleware, RecallRequest, WriteRequest

BENCH_DIR = Path(__file__).parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from locomo.ingest import iter_sessions  # noqa: E402
from longmemeval.ingest import chunk_session as chunk_session_lme  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"
LOCOMO_CATEGORIES = {1: "multi_hop", 2: "temporal", 3: "open_domain", 4: "single_hop"}
MAX_CHUNK_CHARS = 2000  # 与两个 benchmark 的切法一致
# 用于把 recall 上下文映射回切块的指纹长度(去掉 [date: ...] 前缀后取首 N 字符)
FINGERPRINT_LEN = 120


# ---------- 数据加载 ----------

def load_lme(limit: int | None) -> list[dict]:
    path = BENCH_DIR / "longmemeval" / "data" / "longmemeval_s_test.jsonl"
    if not path.exists():
        raise SystemExit(f"数据集未下载: {path}\n先运行 longmemeval/download_dataset.py")
    rows = [json.loads(line) for line in path.open(encoding="utf-8")]
    return rows[:limit] if limit else rows


def load_locomo(limit: int | None) -> list[dict]:
    path = BENCH_DIR / "locomo" / "data" / "locomo10.jsonl"
    if not path.exists():
        raise SystemExit(f"数据集未下载: {path}\n先运行 locomo/download_dataset.py")
    rows = [json.loads(line) for line in path.open(encoding="utf-8")]
    return rows[:limit] if limit else rows


# ---------- 切块(与各自 benchmark 完全一致的切法) ----------

def chunks_lme(sample: dict) -> list[tuple[str, set]]:
    """LongMemEval 样本 → [(chunk 文本, 归属 session id 集合)]."""
    out: list[tuple[str, set]] = []
    sessions = sample.get("haystack_sessions") or []
    sids = sample.get("haystack_session_ids") or []
    dates = sample.get("haystack_dates") or []
    for i, session in enumerate(sessions):
        sid = {sids[i]} if i < len(sids) else {f"session_{i}"}
        date = dates[i] if i < len(dates) else None
        for chunk in chunk_session_lme(session, date=date):
            out.append((chunk, sid))
    return out


def _split_turns(turns: list[dict], date: str | None) -> list[tuple[str, set]]:
    """单次遍历同时产出 (块文本, 块内 dia_id 集合),切分阈值与 ingest 一致."""
    chunks: list[tuple[str, set]] = []
    lines: list[str] = []
    ids: list[str] = []
    size = 0
    for turn in turns:
        speaker = turn.get("speaker", "speaker")
        text = str(turn.get("text", "")).strip()
        if not text:
            continue
        line = f"{speaker}: {text}"
        if lines and size + len(line) > MAX_CHUNK_CHARS:
            chunks.append(("\n".join(lines), set(ids)))
            lines, ids, size = [], [], 0
        lines.append(line)
        ids.append(str(turn.get("dia_id", "")))
        size += len(line) + 1
    if lines:
        chunks.append(("\n".join(lines), set(ids)))
    if date and chunks:
        text0, ids0 = chunks[0]
        chunks[0] = (f"[date: {date}]\n{text0}", ids0)
    return chunks


def chunks_locomo(sample: dict) -> list[tuple[str, set]]:
    """LoCoMo 样本 → [(chunk 文本, 块内 dia_id 集合)]."""
    out: list[tuple[str, set]] = []
    for turns, raw_date in iter_sessions(sample["conversation"]):
        m = re.search(r"on (.+)$", raw_date) if raw_date else None
        date = m.group(1).strip() if m else (raw_date or None)
        out.extend(_split_turns(turns, date))
    return out


# ---------- 两条检索通道 ----------

def fingerprint(chunk: str) -> str:
    """块文本指纹(去掉 [date:] 前缀),用于把 recall 结果映射回块."""
    body = re.sub(r"^\[date: [^\]]*\]\n", "", chunk)
    return body[:FINGERPRINT_LEN]


def recall_hits(resp, chunks: list[tuple[str, set]], k: int | None) -> set[int]:
    """按融合排名序取 recall 的前 k 个 source(k=None 取全部返回),反查块下标."""
    hits: set[int] = set()
    for src in (resp.sources if k is None else resp.sources[:k]):
        snippet = (src.snippet or "").strip()
        if len(snippet) < 40:
            continue
        probe = snippet[:60]
        for i, (chunk, _ids) in enumerate(chunks):
            if probe in chunk:
                hits.add(i)
                break
    return hits


async def write_all(mw: MemoryMiddleware, chunks: list[tuple[str, set]]) -> None:
    for chunk, _ids in chunks:
        await mw.write(
            WriteRequest(scope=MemoryScope.GLOBAL, content=chunk, importance=0.6),
            session_id="retrieval_baseline",
            detect_conflicts=False,
        )


def embed_chunks(embedder, chunk_texts: list[str], batch_size: int = 32) -> np.ndarray:
    """块向量矩阵(每样本算一次,所有题复用).

    分小批嵌入: 数百条长文本一次性送 MPS 会触发 Metal 命令挂起
    (实测 10 样本连跑时第 10 个样本卡死在 waitUntilCompleted).
    """
    vecs: list[np.ndarray] = []
    for i in range(0, len(chunk_texts), batch_size):
        batch = embedder.embed_batch(chunk_texts[i:i + batch_size])
        vecs.extend(np.asarray(v) for v in batch)
    return np.stack(vecs)


def raw_knn(embedder, vecs: np.ndarray, query: str, k: int) -> list[int]:
    """裸 BGE-M3 余弦 KNN. 返回 top-k 块下标."""
    q = np.asarray(embedder.embed(query))
    sims = vecs @ q / (np.linalg.norm(vecs, axis=1) * np.linalg.norm(q) + 1e-12)
    return np.argsort(-sims)[:k].tolist()


# ---------- 主流程 ----------

async def run(args: argparse.Namespace) -> int:
    dataset = args.dataset
    device = args.device
    print(f"加载 embedder({args.embedder}, BGE-M3, device={device or 'auto'})...")
    if device:
        from smilex.memory.lifecycle.embedder import SentenceTransformerEmbedder
        embedder = SentenceTransformerEmbedder(device=device)
    else:
        embedder = get_embedder(EmbedderConfig(backend=args.embedder))

    samples = load_lme(args.limit) if dataset == "longmemeval" else load_locomo(args.limit)

    reranker = None
    if args.rerank:
        from smilex.memory.lifecycle.reranker import CrossEncoderReranker

        reranker = CrossEncoderReranker(device=args.device)
        print("cross-encoder 精排已启用(BAAI/bge-reranker-v2-m3,首次运行需下载 ~2.3GB)")

    # 统计: {channel: {qtype: [bool]}}
    stats: dict[str, dict[str, list[bool]]] = {
        "raw_bge_m3": defaultdict(list),
        "smilex_recall": defaultdict(list),
        "smilex_recall_all": defaultdict(list),
    }
    n_q = 0

    start_all = time.perf_counter()
    for s_idx, sample in enumerate(samples, 1):
        # 组装 (块, gold 判定函数, 问题列表)
        if dataset == "longmemeval":
            chunks = chunks_lme(sample)
            questions = [{
                "q": sample["question"],
                "gold": set(sample.get("answer_session_ids") or []),
                "qtype": sample.get("question_type", "unknown"),
            }]
        else:
            chunks = chunks_locomo(sample)
            questions = [{
                "q": qa["question"],
                "gold": {e for e in qa.get("evidence", []) if e},
                "qtype": LOCOMO_CATEGORIES.get(qa.get("category"), "other"),
            } for qa in sample["qa"] if qa.get("category") in LOCOMO_CATEGORIES]
        if not chunks or not questions:
            continue
        n_q += len(questions)
        chunk_texts = [c for c, _ in chunks]

        # 通道 1: 裸 BGE-M3 KNN(块向量每样本算一次,所有题复用)
        vecs = None if args.skip_raw else embed_chunks(embedder, chunk_texts)

        # 通道 2: smilex 管线(每样本灌一次,问所有题)
        mw = None
        tmp = tempfile.TemporaryDirectory(prefix="rbl_")
        try:
            for qd in questions:
                gold = qd["gold"]
                if not gold:
                    continue
                # raw 通道
                if not args.skip_raw:
                    top = raw_knn(embedder, vecs, qd["q"], args.top_k)
                    hit = any((chunks[i][1] & gold) for i in top)
                    stats["raw_bge_m3"][qd["qtype"]].append(hit)
                # smilex 通道
                if mw is None:
                    mw = MemoryMiddleware(
                        Path(tmp.name) / "bench.db", embedder=embedder,
                        promotion_threshold=0, reranker=reranker,
                    )
                    await mw.initialize()
                    await write_all(mw, chunks)
                resp = await mw.recall(
                    RecallRequest(
                        query=qd["q"], top_k=args.top_k,
                        token_budget=1_000_000,  # 不让预算截断影响 top-k 命中
                    ),
                    session_id=None,
                )
                hits = recall_hits(resp, chunks, args.top_k)
                hit = any((chunks[i][1] & gold) for i in hits)
                stats["smilex_recall"][qd["qtype"]].append(hit)
                hits_all = recall_hits(resp, chunks, None)
                hit_all = any((chunks[i][1] & gold) for i in hits_all)
                stats["smilex_recall_all"][qd["qtype"]].append(hit_all)
        finally:
            if mw is not None:
                await mw.close()
            tmp.cleanup()
        print(f"[{s_idx}/{len(samples)}] {len(questions)} 题完成(共 {len(chunks)} 块)")

    return write_report(args, stats, n_q, time.perf_counter() - start_all)


def write_report(
    args: argparse.Namespace,
    stats: dict[str, dict[str, list[bool]]],
    n_q: int,
    elapsed: float,
) -> int:
    lines = [
        f"=== 检索基线对比 R@{args.top_k}({args.dataset}) — raw BGE-M3 KNN vs smilex recall ===",
        f"题目数: {n_q}(adversarial 不计入)",
        "smilex_recall     = 融合排名前 top-k 个 source(与 raw 同口径)",
        f"smilex_recall_all = recall 实际返回的全部 source(双通道去重后 ~2x{args.top_k})",
    ]
    for channel in ("raw_bge_m3", "smilex_recall", "smilex_recall_all"):
        rows = stats[channel]
        total = [v for flags in rows.values() for v in flags]
        if not total:
            lines.append(f"{channel:<14} (skipped)")
            continue
        r = sum(total) / len(total) * 100
        lines.append(f"{channel:<14} Overall {sum(total)}/{len(total)} = {r:.1f} %")
        for qtype in sorted(rows):
            flags = rows[qtype]
            lines.append(
                f"    {qtype:<24} {sum(flags)}/{len(flags)} = "
                f"{sum(flags)/len(flags)*100:.1f} %"
            )
    lines.append(f"Total Time       {elapsed / 60:.1f} min")
    report = "\n".join(lines)
    print("\n" + report)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{args.dataset}_r{args.top_k}_{args.embedder}_{time.strftime('%Y%m%d_%H%M%S')}"
    out = RESULTS_DIR / f"{stem}.md"
    out.write_text(f"```\n{report}\n```\n", encoding="utf-8")
    print(f"\n结果已保存: {out}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="检索基线对比(raw BGE-M3 vs smilex recall)")
    parser.add_argument("--dataset", choices=["longmemeval", "locomo"], required=True)
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个样本")
    parser.add_argument("--embedder", default="sentence-transformers",
                        choices=["sentence-transformers", "hash"])
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--skip-raw", action="store_true",
                        help="只跑 smilex 通道(对照数字已有 raw 结果时)")
    parser.add_argument("--rerank", action="store_true",
                        help="启用 cross-encoder 精排(bge-reranker-v2-m3,需下载 ~2.3GB)")
    parser.add_argument("--device", default=None,
                        help="embedding 设备(如 cpu;默认自动。MPS 连跑挂起时可指定 cpu)")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
