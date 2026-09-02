"""LongMemEval benchmark — mem0 兼容口径 runner.

与 mem0ai/memory-benchmarks benchmarks/longmemeval/run.py 逐项对齐:
- 采样: 默认每题型 5 题(seed=42,共 30 题);--all-questions 跑 500
- 数据源: --source original(默认,与历史结果连续)| cleaned
  (mem0 用的 xiaowu0162/longmemeval-cleaned 社区修正版)
- 每题独立 db(mem0 的 per-question user_id 隔离语义)
- 检索: recall top-200 一次取全,cutoffs 10/20/50/200 各评一次
- 作答: mem0 14 条规则 prompt(question_date 作 today,<mem_thinking> 隐藏推理)
- 判卷: anti-"no" 宽松 judge;拒答("The information provided is not enough")
  匹配 abstention gold
- 指标: 每 cutoff 独立,headline = 最大 cutoff(200)

用法:
    export OPENAI_API_KEY=...  # 同 longmemeval 约定
    uv run python benchmarks/mem0_compat/longmemeval.py --per-type 1      # 冒烟 6 题
    uv run python benchmarks/mem0_compat/longmemeval.py                   # mem0 默认 30 题
    uv run python benchmarks/mem0_compat/longmemeval.py --all-questions --source cleaned
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from smilex.memory.lifecycle.embedder import EmbedderConfig, get_embedder
from smilex.memory.models import MemoryScope
from smilex.memory.models.fuzzy import TimeRange
from smilex.middlewares import MemoryMiddleware, RecallRequest, WriteRequest

import mem0_prompts as mp
from common import (
    DEFAULT_CUTOFFS, TOP_K, compute_metrics_by_cutoff, format_memories_grouped_by_date,
    import_from, load_fragment_rows, load_llm_client, ranked_memories,
    reopen_stdout, render_report, sample_questions_stratified, save_results,
)

_lme_ingest = import_from(
    Path(__file__).parent.parent / "longmemeval" / "ingest.py", "m0c_lme_ingest"
)
chunk_session = _lme_ingest.chunk_session

RESULTS_DIR = Path(__file__).parent / "results"
DATA_DIR = Path(__file__).parent / "data"
CLEANED_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/"
    "longmemeval_s_cleaned.json"
)
ORIGINAL_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s"
)


def dataset_path(source: str) -> Path:
    return DATA_DIR / f"longmemeval_s_{source}.jsonl"


def load_samples(source: str) -> list[dict]:
    path = dataset_path(source)
    if not path.exists():
        # original 可复用 longmemeval benchmark 已下载的缓存
        if source == "original":
            shared = Path(__file__).parent.parent / "longmemeval" / "data" / "longmemeval_s_test.jsonl"
            if shared.exists():
                return [json.loads(line) for line in shared.open(encoding="utf-8")]
        url = CLEANED_URL if source == "cleaned" else ORIGINAL_URL
        print(f"下载数据集({url},原始版 ~270MB)...")
        with urllib.request.urlopen(url, timeout=600) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"完成: {path}({len(rows)} 题)")
    return [json.loads(line) for line in path.open(encoding="utf-8")]


def parse_lme_date(raw: str) -> datetime | None:
    """'2023/09/14 (Thu) 12:00' / ISO → datetime."""
    if not raw:
        return None
    for fmt in ("%Y/%m/%d (%a) %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def ingest_sample(
    sample: dict, embedder, db_path: Path,
    fact_extraction: bool = False, rerank: bool = False, device: str | None = None,
) -> MemoryMiddleware:
    """灌入该题 haystack_sessions,并回填 session 日期到 time_start.

    fact_extraction: 写入时 LLM 原子事实抽取(SMILEX_EXTRACT_* 环境变量);
    rerank: cross-encoder 精排(bge-reranker-v2-m3).
    """
    from common import anchor_times
    kwargs = {}
    if fact_extraction:
        from smilex.memory.lifecycle.extractor import LLMFactExtractor
        kwargs["fact_extractor"] = LLMFactExtractor()
    if rerank:
        from smilex.memory.lifecycle.reranker import CrossEncoderReranker
        kwargs["reranker"] = CrossEncoderReranker(device=device)
    mw = MemoryMiddleware(db_path, embedder=embedder, promotion_threshold=0, **kwargs)
    await mw.initialize()
    dated: list[tuple[str, str]] = []
    try:
        sessions = sample.get("haystack_sessions") or sample.get("context") or []
        dates = sample.get("haystack_dates") or []
        for i, session in enumerate(sessions):
            dt = parse_lme_date(dates[i]) if i < len(dates) else None
            tr = TimeRange(exact=dt) if dt else None
            iso = dt.isoformat() if dt else ""
            for chunk in chunk_session(session, date=dates[i] if i < len(dates) else None):
                await mw.write(
                    WriteRequest(
                        scope=MemoryScope.GLOBAL, content=chunk,
                        importance=0.6, time_range=tr,
                    ),
                    session_id="mem0_compat_lme",
                    detect_conflicts=False,
                )
                if iso:
                    dated.append((chunk, iso))
    except Exception:
        await mw.close()
        raise
    anchor_times(db_path, dated)
    return mw


_llm = None


async def _chat_answer(client, prompt: str) -> str:
    text, _ = await _llm.chat(client, _llm.answer_model(), "", prompt, max_tokens=4096)
    return text


async def _chat_judge(client, prompt: str) -> str:
    """判卷调用;输出里没有 yes/no 判定时(思考文本耗尽 max_tokens 被截断)
    用更大预算 + 简化 prompt 重试一次,避免误记 0 分."""
    import re as _re

    text, _ = await _llm.chat(
        client, _llm.judge_model(), "", prompt, max_tokens=512, disable_thinking=True
    )
    has_verdict = bool(_re.search(r"\b(yes|no)\b", text.lower()))
    if not has_verdict:
        terse = (
            prompt
            + "\n\nAnswer with exactly one word (yes or no) after your thinking. "
            "Keep the thinking under 100 words."
        )
        retry, _ = await _llm.chat(
            client, _llm.judge_model(), "", terse,
            max_tokens=2048, disable_thinking=True,
        )
        if _re.search(r"\b(yes|no)\b", retry.lower()):
            return retry
    return text


async def run(args: argparse.Namespace) -> int:
    global _llm
    _llm = load_llm_client()

    if args.device:
        from smilex.memory.lifecycle.embedder import SentenceTransformerEmbedder
        embedder = SentenceTransformerEmbedder(device=args.device)
    else:
        embedder = get_embedder(EmbedderConfig(backend=args.embedder))
    client = _llm.get_client()

    all_samples = load_samples(args.source)
    if args.all_questions:
        samples = all_samples
    else:
        samples = sample_questions_stratified(all_samples, args.per_type, args.seed)
    cutoffs = [int(x) for x in args.cutoffs.split(",")]
    print(f"样本数: {len(samples)}(source={args.source}, seed={args.seed if not args.all_questions else '-'})")

    variant = ("_fx" if args.fact_extraction else "") + ("_rr" if args.rerank else "")
    ckpt_path = RESULTS_DIR / f"ckpt_lme_mem0compat_{args.source}_{args.embedder}{variant}.jsonl"
    done: dict[str, dict] = {}
    if args.resume and ckpt_path.exists():
        for line in ckpt_path.open(encoding="utf-8"):
            rec = json.loads(line)
            done[rec["qid"]] = rec
        print(f"resume: 已完成 {len(done)} 题({ckpt_path.name})")

    records: list[dict] = []
    start_all = time.perf_counter()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with ckpt_path.open("a", encoding="utf-8") as ckpt:
        for idx, sample in enumerate(samples, 1):
            qid = str(sample.get("question_id", idx))
            if qid in done:
                records.append(done[qid])
                print(f"[{idx}/{len(samples)}] (skip, cached)")
                continue
            question = sample["question"]
            gold = str(sample["answer"])
            qtype = sample.get("question_type", "unknown")
            qdate = str(sample.get("question_date", ""))

            rec = {
                "qid": qid, "question_type": qtype, "question": question,
                "gold": gold, "cutoffs": {}, "results": {},
            }
            try:
                with tempfile.TemporaryDirectory(prefix="m0c_lme_") as td:
                    db = Path(td) / "bench.db"
                    mw = await ingest_sample(
                        sample, embedder, db,
                        fact_extraction=args.fact_extraction,
                        rerank=args.rerank, device=args.device,
                    )
                    try:
                        resp = await mw.recall(
                            RecallRequest(query=question, top_k=TOP_K, token_budget=10**9),
                            session_id=None,
                        )
                        rows = load_fragment_rows(db)
                    finally:
                        await mw.close()
                    ranked = ranked_memories(resp, rows)

                    for c in cutoffs:
                        prompt = mp.LME_ANSWER_PROMPT.format(
                            memories=format_memories_grouped_by_date(ranked[:c]),
                            question_date=qdate or "unknown date",
                            question=question,
                        )
                        try:
                            raw_answer = await _chat_answer(client, prompt)
                            visible = mp.strip_mem_thinking(raw_answer)
                            judge_prompt = mp.LME_JUDGE_PROMPT.format(
                                question=question, answer=gold, response=visible,
                            )
                            verdict = await _chat_judge(client, judge_prompt)
                            correct, label = mp.parse_lme_judge(verdict)
                        except Exception as exc:
                            print(f"[{idx}] cutoff {c} 失败: {exc}")
                            visible, correct, label = "", False, f"error: {exc}"
                        rec["cutoffs"][str(c)] = correct
                        rec["results"][str(c)] = {
                            "answer": visible, "correct": correct, "label": label,
                        }
            except Exception as exc:  # 灌入/检索失败: 该题全 cutoff 记 0
                print(f"[{idx}] 样本失败: {type(exc).__name__}: {exc}")
                for c in cutoffs:
                    rec["cutoffs"][str(c)] = False
                    rec["results"][str(c)] = {"answer": "", "correct": False,
                                              "label": f"error: {exc}"}

            records.append(rec)
            ckpt.write(json.dumps(rec, ensure_ascii=False) + "\n")
            ckpt.flush()
            marks = "".join("✓" if rec["cutoffs"][str(c)] else "✗" for c in cutoffs)
            print(
                f"[{idx}/{len(samples)}] {marks} ({','.join(map(str, cutoffs))}) | {qtype}",
                flush=True,
            )

    metrics = compute_metrics_by_cutoff(records, cutoffs, "question_type")
    report = render_report(
        f"=== LongMemEval mem0 兼容口径(source={args.source}) — "
        f"smilex-ai-memory(embedder={args.embedder}) ===",
        metrics, cutoffs,
        f"answerer={_llm.answer_model()} judge={_llm.judge_model()} "
        f"(mem0 官方为 gpt-5/gpt-5,比较时注意)",
        time.perf_counter() - start_all,
    )
    print("\n" + report)
    md = save_results(
        [f"lme_mem0compat_{args.source}_n{len(records)}", args.embedder], report, records
    )
    print(f"\n结果已保存: {md}")
    return 0


def main() -> int:
    reopen_stdout()
    parser = argparse.ArgumentParser(description="LongMemEval benchmark(mem0 兼容口径)")
    parser.add_argument("--source", choices=["original", "cleaned"], default="original",
                        help="original=官方版(默认);cleaned=mem0 用的社区修正版")
    parser.add_argument("--per-type", type=int, default=5,
                        help="每题型采样 N 题(mem0 默认 5)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--all-questions", action="store_true", help="跑全量 500 题")
    parser.add_argument("--cutoffs", default=",".join(map(str, DEFAULT_CUTOFFS)))
    parser.add_argument("--embedder", default="sentence-transformers",
                        choices=["sentence-transformers", "hash"])
    parser.add_argument("--device", default=None,
                        help="embedding 设备(如 cpu;MPS 挂起时用)")
    parser.add_argument("--fact-extraction", action="store_true",
                        help="写入时 LLM 原子事实抽取(需 SMILEX_EXTRACT_API_KEY)")
    parser.add_argument("--rerank", action="store_true",
                        help="cross-encoder 精排(bge-reranker-v2-m3,需下载 ~2.3GB)")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
