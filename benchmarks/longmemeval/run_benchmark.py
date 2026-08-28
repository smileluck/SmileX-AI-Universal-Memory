"""LongMemEval benchmark 主入口 — 对标 mem0 的记忆质量评测.

流程(每样本):
    独立临时 db → ingest 对话历史(write, BGE-M3)→ recall 构建 4K token 上下文
    → LLM 生成答案 → LLM-as-judge 对比 gold → 清理

输出(对标 mem0 报告口径):
    Overall Accuracy + 各题型准确率 + Avg Context Tokens + Recall Latency p50

用法:
    export OPENAI_API_KEY=...            # 可选 OPENAI_BASE_URL / SMILEX_BENCH_*_MODEL
    uv run python benchmarks/longmemeval/download_dataset.py
    uv run python benchmarks/longmemeval/run_benchmark.py --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from statistics import median

from smilex.memory.lifecycle.embedder import EmbedderConfig, get_embedder

from answer import answer_question
from download_dataset import dataset_path
from ingest import ingest_sample
from judge import judge_answer
from llm_client import get_client

RESULTS_DIR = Path(__file__).parent / "results"


def load_samples(config: str, split: str, limit: int | None) -> list[dict]:
    path = dataset_path(config, split)
    if not path.exists():
        raise SystemExit(
            f"数据集未下载: {path}\n"
            "先运行: uv run python benchmarks/longmemeval/download_dataset.py"
        )
    samples = [json.loads(line) for line in path.open(encoding="utf-8")]
    return samples[:limit] if limit else samples


async def run(args: argparse.Namespace) -> int:
    from openai import AsyncOpenAI

    print(f"加载 embedder({args.embedder})... 首次加载 BGE-M3 需要几分钟")
    embedder = get_embedder(EmbedderConfig(backend=args.embedder))
    client: AsyncOpenAI = get_client()

    samples = load_samples(args.config, args.split, args.limit)
    print(f"样本数: {len(samples)}(config={args.config}, split={args.split})")

    records: list[dict] = []
    correct_total = 0
    by_type: dict[str, list[bool]] = defaultdict(list)
    recall_latencies: list[float] = []
    ingest_seconds: list[float] = []

    start_all = time.perf_counter()
    for idx, sample in enumerate(samples, 1):
        question = sample["question"]
        gold = sample["answer"]
        qtype = sample.get("question_type", "unknown")

        with tempfile.TemporaryDirectory(prefix="lme_") as tmpdir:
            mw, n_chunks, ingest_s = await ingest_sample(
                sample, embedder, Path(tmpdir) / "bench.db"
            )
            ingest_seconds.append(ingest_s)
            try:
                result = await answer_question(
                    mw, question, client,
                    token_budget=args.token_budget, top_k=args.top_k,
                )
            finally:
                await mw.close()

        correct, verdict_raw, judge_tokens = await judge_answer(
            client, question, gold, result["answer"]
        )
        recall_latencies.append(result["recall_s"])
        correct_total += correct
        by_type[qtype].append(correct)
        records.append({
            "idx": idx,
            "question_type": qtype,
            "question": question,
            "gold": gold,
            "prediction": result["answer"],
            "correct": correct,
            "verdict_raw": verdict_raw,
            "context_tokens": result["context_tokens"],
            "recall_s": round(result["recall_s"], 4),
            "llm_tokens": result["llm_tokens"],
            "judge_tokens": judge_tokens,
            "n_chunks": n_chunks,
            "ingest_s": round(ingest_s, 2),
        })

        acc = correct_total / idx * 100
        mark = "✓" if correct else "✗"
        print(
            f"[{idx}/{len(samples)}] {mark} acc={acc:.1f}% | {qtype} | "
            f"ctx={result['context_tokens']}tok recall={result['recall_s']:.2f}s"
        )
        if args.dump_context:
            print(f"  --- context ---\n{result['context']}\n  ---------------")

    elapsed = time.perf_counter() - start_all
    return write_report(args, samples, records, by_type, recall_latencies,
                        ingest_seconds, elapsed)


def write_report(
    args: argparse.Namespace,
    samples: list[dict],
    records: list[dict],
    by_type: dict[str, list[bool]],
    recall_latencies: list[float],
    ingest_seconds: list[float],
    elapsed: float,
) -> int:
    n = len(records)
    overall = sum(r["correct"] for r in records) / n * 100
    avg_ctx = sum(r["context_tokens"] for r in records) / n
    p50 = median(recall_latencies) if recall_latencies else 0.0
    avg_ingest = sum(ingest_seconds) / len(ingest_seconds) if ingest_seconds else 0.0

    lines = [
        f"=== LongMemEval({args.config}) — smilex-ai-memory(embedder={args.embedder}) ===",
        f"样本数:            {n}",
        f"Overall Accuracy:  {overall:.1f} %",
    ]
    for qtype in sorted(by_type):
        flags = by_type[qtype]
        lines.append(f"  {qtype:<24} {sum(flags)}/{len(flags)} = {sum(flags)/len(flags)*100:.1f} %")
    lines += [
        f"Avg Context Tokens {avg_ctx / 1000:.1f} K",
        f"Recall Latency p50 {p50:.2f} s",
        f"Avg Ingest /样本    {avg_ingest:.1f} s",
        f"Total Time         {elapsed / 60:.1f} min",
    ]
    report = "\n".join(lines)
    print("\n" + report)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{args.config}_n{n}_{args.embedder}_{time.strftime('%Y%m%d_%H%M%S')}"
    json_path = RESULTS_DIR / f"{stem}.json"
    json_path.write_text(
        json.dumps({"report": report, "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path = RESULTS_DIR / f"{stem}.md"
    md_path.write_text(f"```\n{report}\n```\n", encoding="utf-8")
    print(f"\n结果已保存: {json_path}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="LongMemEval 记忆质量 benchmark")
    parser.add_argument("--config", default="longmemeval_s")
    parser.add_argument("--split", default="test")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条(试跑用)")
    parser.add_argument("--embedder", default="sentence-transformers",
                        choices=["sentence-transformers", "hash"])
    parser.add_argument("--token-budget", type=int, default=4000)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--dump-context", action="store_true", help="打印每题检索上下文")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
