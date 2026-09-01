"""LoCoMo benchmark 主入口 — 对标 mem0 的记忆质量评测.

流程(每个对话样本):
    独立临时 db → ingest 一次(write, BGE-M3 + FTS5)→ 对该对话的每道题:
    recall 构建 4K token 上下文 → LLM 生成答案 → LLM-as-judge 对比 gold

判分口径(mem0 同款做法):
    全题型 LLM-as-judge 语义等价判定;adversarial 题(category 5)语义为
    "对话中不可回答",gold 视作 "I don't know.",拒答才得分.

输出: Overall Accuracy + 各题型准确率 + Avg Context Tokens + Recall Latency p50

用法:
    export OPENAI_API_KEY=...            # 可选 OPENAI_BASE_URL / SMILEX_BENCH_*_MODEL
    uv run python benchmarks/locomo/download_dataset.py
    uv run python benchmarks/locomo/run_benchmark.py --limit 1   # 先冒烟
    uv run python benchmarks/locomo/run_benchmark.py --resume    # 全量+续跑
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
from ingest import ingest_conversation
from judge import judge_answer
from llm_client import get_client

RESULTS_DIR = Path(__file__).parent / "results"

# LoCoMo category 编号 → 题型名(以 mem0 实现为准,与论文编号不同:
# 各 category 题量 1:282 2:321 3:96 4:841 5:446 与此映射吻合)
CATEGORY_NAMES = {
    1: "multi_hop",
    2: "temporal",
    3: "open_domain",
    4: "single_hop",
    5: "adversarial",
}


def load_samples(limit: int | None) -> list[dict]:
    path = dataset_path()
    if not path.exists():
        raise SystemExit(
            f"数据集未下载: {path}\n"
            "先运行: uv run python benchmarks/locomo/download_dataset.py"
        )
    samples = [json.loads(line) for line in path.open(encoding="utf-8")]
    return samples[:limit] if limit else samples


async def run(args: argparse.Namespace) -> int:
    from openai import AsyncOpenAI

    print(f"加载 embedder({args.embedder})... 首次加载 BGE-M3 需要几分钟")
    embedder = get_embedder(EmbedderConfig(backend=args.embedder))
    client: AsyncOpenAI = get_client()

    samples = load_samples(args.limit)
    total_q = sum(len(s["qa"]) for s in samples)
    print(f"对话数: {len(samples)}(共 {total_q} 题)")

    # checkpoint: 每题增量落盘,键 (conv_idx, q_idx),中断后 --resume 跳过已完成题
    ckpt_path = RESULTS_DIR / f"ckpt_locomo10_{args.embedder}.jsonl"
    done: dict[tuple[int, int], dict] = {}
    if args.resume and ckpt_path.exists():
        for line in ckpt_path.open(encoding="utf-8"):
            rec = json.loads(line)
            done[(rec["conv_idx"], rec["q_idx"])] = rec
        print(f"resume: 已完成 {len(done)} 题({ckpt_path.name})")

    records: list[dict] = []
    recall_latencies: list[float] = []
    ingest_seconds: list[float] = []
    n_answered = 0
    n_correct = 0
    by_cat: dict[str, list[bool]] = defaultdict(list)

    def _accumulate(rec: dict) -> None:
        nonlocal n_answered, n_correct
        n_answered += 1
        n_correct += rec["correct"]
        by_cat[rec["category_name"]].append(rec["correct"])
        recall_latencies.append(rec["recall_s"])
        ingest_seconds.append(rec["ingest_s"])
        records.append(rec)

    start_all = time.perf_counter()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with ckpt_path.open("a", encoding="utf-8") as ckpt:
        for conv_idx, sample in enumerate(samples, 1):
            qa_list = sample["qa"][: args.max_qa] if args.max_qa else sample["qa"]
            conv_done = sum(1 for i in range(len(qa_list)) if (conv_idx, i) in done)
            print(
                f"--- 对话 {conv_idx}/{len(samples)}(sample_id={sample['sample_id']}, "
                f"{len(qa_list)} 题, 已完成 {conv_done}) ---"
            )

            # 已全部完成的对话跳过灌入(省时)
            mw = None
            n_chunks, ingest_s = 0, 0.0
            tmp_ctx = None
            try:
                for q_idx, qa in enumerate(qa_list):
                    if (conv_idx, q_idx) in done:
                        _accumulate(done[(conv_idx, q_idx)])
                        continue

                    question = qa["question"]
                    cat = qa.get("category", 0)
                    cat_name = CATEGORY_NAMES.get(cat, f"cat_{cat}")
                    if cat == 5:
                        # adversarial: 对话中不可回答,拒答("I don't know.")才得分
                        gold = "I don't know."
                    else:
                        gold = qa["answer"]

                    try:
                        if mw is None:  # 该对话首次作答 → 灌入一次
                            tmp_ctx = tempfile.TemporaryDirectory(prefix="locomo_")
                            mw, n_chunks, ingest_s = await ingest_conversation(
                                sample, embedder, Path(tmp_ctx.name) / "bench.db"
                            )
                        result = await answer_question(
                            mw, question, client,
                            token_budget=args.token_budget, top_k=args.top_k,
                        )
                        correct, verdict_raw, judge_tokens = await judge_answer(
                            client, question, gold, result["answer"]
                        )
                        recall_s = result["recall_s"]
                        context_tokens = result["context_tokens"]
                    except Exception as exc:  # 单题失败不崩整个 run
                        print(f"  [{q_idx}] 样本失败: {type(exc).__name__}: {exc}")
                        correct, verdict_raw, judge_tokens = False, f"error: {exc}", 0
                        result = {"answer": "", "context_tokens": 0, "recall_s": 0.0}
                        recall_s, context_tokens = 0.0, 0

                    rec = {
                        "conv_idx": conv_idx,
                        "q_idx": q_idx,
                        "sample_id": sample["sample_id"],
                        "category": cat,
                        "category_name": cat_name,
                        "question": question,
                        "gold": gold,
                        "prediction": result["answer"],
                        "correct": correct,
                        "verdict_raw": verdict_raw,
                        "context_tokens": context_tokens,
                        "recall_s": round(recall_s, 4),
                        "llm_tokens": result.get("llm_tokens", 0),
                        "judge_tokens": judge_tokens,
                        "n_chunks": n_chunks,
                        "ingest_s": round(ingest_s, 2),
                    }
                    _accumulate(rec)
                    ckpt.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    ckpt.flush()

                    acc = n_correct / n_answered * 100
                    mark = "✓" if correct else "✗"
                    print(
                        f"  [{conv_idx}-{q_idx}] {mark} acc={acc:.1f}% | {cat_name} | "
                        f"ctx={context_tokens}tok recall={recall_s:.2f}s"
                    )
                    if args.dump_context:
                        print(f"    --- context ---\n{result.get('context', '')}\n    ---------------")
            finally:
                if mw is not None:
                    await mw.close()
                if tmp_ctx is not None:
                    tmp_ctx.cleanup()

    elapsed = time.perf_counter() - start_all
    return write_report(args, records, by_cat, recall_latencies,
                        ingest_seconds, elapsed)


def write_report(
    args: argparse.Namespace,
    records: list[dict],
    by_cat: dict[str, list[bool]],
    recall_latencies: list[float],
    ingest_seconds: list[float],
    elapsed: float,
) -> int:
    n = len(records)
    if n == 0:
        print("无记录")
        return 1
    overall = sum(r["correct"] for r in records) / n * 100
    avg_ctx = sum(r["context_tokens"] for r in records) / n
    p50 = median(recall_latencies) if recall_latencies else 0.0
    avg_ingest = sum(ingest_seconds) / len(ingest_seconds) if ingest_seconds else 0.0

    lines = [
        f"=== LoCoMo(locomo10) — smilex-ai-memory(embedder={args.embedder}) ===",
        f"题目数:            {n}",
        f"Overall Accuracy:  {overall:.1f} %",
    ]
    for cat_name in sorted(by_cat):
        flags = by_cat[cat_name]
        lines.append(f"  {cat_name:<16} {sum(flags)}/{len(flags)} = {sum(flags)/len(flags)*100:.1f} %")
    lines += [
        f"Avg Context Tokens {avg_ctx / 1000:.1f} K",
        f"Recall Latency p50 {p50:.2f} s",
        f"Avg Ingest /对话    {avg_ingest:.1f} s",
        f"Total Time         {elapsed / 60:.1f} min",
    ]
    report = "\n".join(lines)
    print("\n" + report)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"locomo10_n{n}_{args.embedder}_{time.strftime('%Y%m%d_%H%M%S')}"
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
    parser = argparse.ArgumentParser(description="LoCoMo 记忆质量 benchmark")
    parser.add_argument("--limit", type=int, default=None,
                        help="只跑前 N 个对话(试跑用;全量 10 个)")
    parser.add_argument("--max-qa", type=int, default=None,
                        help="每个对话最多跑 N 题(冒烟用)")
    parser.add_argument("--embedder", default="sentence-transformers",
                        choices=["sentence-transformers", "hash"])
    parser.add_argument("--token-budget", type=int, default=4000)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--dump-context", action="store_true", help="打印每题检索上下文")
    parser.add_argument("--resume", action="store_true",
                        help="从 checkpoint 续跑(跳过已完成题,复用其结果)")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
