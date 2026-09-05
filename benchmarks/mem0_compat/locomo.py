"""LoCoMo benchmark — mem0 兼容口径 runner.

与 mem0ai/memory-benchmarks benchmarks/locomo/run.py 逐项对齐:
- 题目范围: categories 1-4 全量(adversarial 不计分,分母与 mem0 一致)
- gold 预处理: category 3(open-domain)取分号前第一段
- 检索: recall top-200 一次取全,cutoffs 10/20/50/200 各评一次
- 记忆呈现: 时间序(不显示 score/rank),answerer 上限 200 条
- 作答/判卷: mem0 官方 prompt 逐字移植(mem0_prompts.py)
- 指标: 每 cutoff 独立,headline = 最大 cutoff(200)

与 mem0 的差异(报告注明):
- 记忆形成: mem0 灌入时 LLM 抽取事实卡片;本系统原文分层存储(BGE-M3+FTS5)
- 作答/判卷模型: 默认 GLM 环境变量约定(mem0 官方为 gpt-5)

用法:
    export OPENAI_API_KEY=...  # 同 longmemeval 约定
    uv run python benchmarks/mem0_compat/locomo.py --conversations 0 --max-qa 2  # 冒烟
    uv run python benchmarks/mem0_compat/locomo.py --resume                      # 全量
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import mem0_prompts as mp
from common import (
    DEFAULT_CUTOFFS,
    TOP_K,
    compute_metrics_by_cutoff,
    format_memories_chronological,
    load_fragment_rows,
    ranked_memories,
    render_report,
    reopen_stdout,
    save_results,
)

from smilex.memory.lifecycle.embedder import EmbedderConfig, get_embedder
from smilex.memory.models import MemoryScope
from smilex.memory.models.fuzzy import TimeRange
from smilex.middlewares import MemoryMiddleware, RecallRequest, WriteRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _shared.llm_client as _llm  # noqa: E402
from locomo.ingest import chunk_session, iter_sessions  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"


def parse_session_date(raw: str) -> datetime | None:
    """'1:56 pm on 8 May, 2023' → datetime(2023, 5, 8)."""
    import re
    m = re.search(r"on (.+)$", raw or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).strip(), "%d %B, %Y")
    except ValueError:
        return None


def load_samples() -> list[dict]:
    path = Path(__file__).parent.parent / "locomo" / "data" / "locomo10.jsonl"
    if not path.exists():
        raise SystemExit(
            f"数据集未下载: {path}\n先运行: uv run python benchmarks/locomo/download_dataset.py"
        )
    return [json.loads(line) for line in path.open(encoding="utf-8")]


async def ingest_conversation(
    sample: dict, embedder, db_path: Path, progress: bool = False,
    fact_extraction: bool = False, rerank: bool = False, device: str | None = None,
) -> tuple[MemoryMiddleware, int, float, list[tuple[str, str]]]:
    """灌入一个对话,并回填 session 日期到 time_start(供时间序呈现).

    fact_extraction: 写入时 LLM 原子事实抽取(SMILEX_EXTRACT_* 环境变量);
    rerank: cross-encoder 精排(bge-reranker-v2-m3).

    Returns:
        (mw, 写入块数, 耗时秒, [(块文本, ISO 日期)] 供 anchor_times)
    """
    kwargs = {}
    if fact_extraction:
        from smilex.memory.lifecycle.extractor import LLMFactExtractor
        kwargs["fact_extractor"] = LLMFactExtractor()
    if rerank:
        from smilex.memory.lifecycle.reranker import CrossEncoderReranker
        kwargs["reranker"] = CrossEncoderReranker(device=device)
    mw = MemoryMiddleware(db_path, embedder=embedder, promotion_threshold=0, **kwargs)
    await mw.initialize()
    start = time.perf_counter()
    n = 0
    dated: list[tuple[str, str]] = []
    try:
        for turns, raw_date in iter_sessions(sample["conversation"]):
            dt = parse_session_date(raw_date)
            tr = TimeRange(exact=dt) if dt else None
            iso = dt.isoformat() if dt else ""
            for chunk in chunk_session(turns, date=raw_date or None):
                await mw.write(
                    WriteRequest(
                        scope=MemoryScope.GLOBAL, content=chunk,
                        importance=0.6, time_range=tr,
                    ),
                    session_id="mem0_compat_locomo",
                    detect_conflicts=False,
                )
                n += 1
                if iso:
                    dated.append((chunk, iso))
                if progress and n % 100 == 0:
                    print(f"    write {n} t={time.perf_counter()-start:.0f}s", flush=True)
    except Exception:
        await mw.close()
        raise
    from common import anchor_times
    anchor_times(db_path, dated)
    return mw, n, time.perf_counter() - start, dated


async def answer_and_judge(
    client, question: str, memories: list[dict], reference_date: str, gold: str,
) -> dict[str, object]:
    """一个 cutoff 的完整评卷: 作答 → 判卷."""
    from common import format_memories_chronological as _f  # noqa: F401
    prompt = mp.LOCOMO_ANSWER_PROMPT.format(
        memories=format_memories_chronological(memories[:mp.LOCOMO_ANSWERER_MEMORY_LIMIT]),
        question=question, reference_date=reference_date,
    )
    answer, _tok = await _chat_answer(client, prompt)
    judge_prompt = mp.LOCOMO_JUDGE_PROMPT.format(
        question=question, answer=gold, response=answer,
    )
    verdict_raw, _jtok = await _chat_judge(client, mp.LOCOMO_JUDGE_SYSTEM, judge_prompt)
    correct, label = mp.parse_locomo_judge(verdict_raw)
    return {"answer": answer, "correct": correct, "label": label}


async def _chat_answer(client, prompt: str) -> tuple[str, int]:
    return await _llm.chat(
        client, _llm.answer_model(), "", prompt, max_tokens=4096
    )  # 保留思考模式(长上下文事实抽取)


async def _chat_judge(client, system: str, prompt: str) -> tuple[str, int]:
    return await _llm.chat(
        client, _llm.judge_model(), system, prompt,
        max_tokens=512, disable_thinking=True,
    )


async def run(args: argparse.Namespace) -> int:
    if args.device:
        from smilex.memory.lifecycle.embedder import SentenceTransformerEmbedder
        embedder = SentenceTransformerEmbedder(device=args.device)
    else:
        embedder = get_embedder(EmbedderConfig(backend=args.embedder))
    client = _llm.get_client()

    all_samples = load_samples()
    conv_ids = [int(x) for x in args.conversations.split(",") if x.strip() != ""]
    cats = {int(x) for x in args.categories.split(",")}
    cutoffs = [int(x) for x in args.cutoffs.split(",")]

    variant = ("_fx" if args.fact_extraction else "") + ("_rr" if args.rerank else "")
    ckpt_path = RESULTS_DIR / f"ckpt_locomo_mem0compat_{args.embedder}{variant}.jsonl"
    done: dict[tuple[int, int], dict] = {}
    if args.resume and ckpt_path.exists():
        for line in ckpt_path.open(encoding="utf-8"):
            rec = json.loads(line)
            done[(rec["conv_idx"], rec["q_idx"])] = rec
        print(f"resume: 已完成 {len(done)} 题({ckpt_path.name})")

    records: list[dict] = []
    start_all = time.perf_counter()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with ckpt_path.open("a", encoding="utf-8") as ckpt:
        for conv_idx in conv_ids:
            sample = all_samples[conv_idx]
            qa_list = [qa for qa in sample["qa"] if qa.get("category") in cats]
            if args.max_qa:
                qa_list = qa_list[: args.max_qa]

            # reference_date = 该对话最后一个 session 的日期(mem0 口径)
            ref_dt = None
            for _turns, raw_date in iter_sessions(sample["conversation"]):
                dt = parse_session_date(raw_date)
                if dt:
                    ref_dt = dt if ref_dt is None or dt > ref_dt else ref_dt
            reference_date = (
                ref_dt.strftime("%B %d, %Y") if ref_dt else "2023"
            )

            conv_done = sum(1 for i in range(len(qa_list)) if (conv_idx, i) in done)
            print(
                f"--- 对话 {conv_idx}(sample_id={sample['sample_id']}, "
                f"{len(qa_list)} 题, 已完成 {conv_done}, ref_date={reference_date}) ---",
                flush=True,
            )

            mw = None
            rows: dict[str, dict] = {}
            tmp = None
            try:
                for q_idx, qa in enumerate(qa_list):
                    if (conv_idx, q_idx) in done:
                        records.append(done[(conv_idx, q_idx)])
                        continue
                    question = qa["question"]
                    cat = qa["category"]
                    cat_name = mp.LOCOMO_CATEGORY_NAMES.get(cat, f"cat_{cat}")
                    gold = mp.locomo_preprocess_answer(cat, str(qa["answer"]))

                    if mw is None:
                        tmp = tempfile.TemporaryDirectory(prefix="m0c_locomo_")
                        mw, n_chunks, ingest_s, _dated = await ingest_conversation(
                            sample, embedder, Path(tmp.name) / "bench.db",
                            fact_extraction=args.fact_extraction,
                            rerank=args.rerank, device=args.device,
                        )
                        rows = load_fragment_rows(Path(tmp.name) / "bench.db")
                        print(f"    ingest: {n_chunks} chunks / {ingest_s:.0f}s", flush=True)

                    resp = await mw.recall(
                        RecallRequest(query=question, top_k=TOP_K, token_budget=10**9),
                        session_id=None,
                    )
                    ranked = ranked_memories(resp, rows)

                    cut_results: dict[str, object] = {}
                    cut_correct: dict[str, bool] = {}
                    for c in cutoffs:
                        try:
                            r = await answer_and_judge(
                                client, question, ranked[:c], reference_date, gold
                            )
                            cut_results[str(c)] = r
                            cut_correct[str(c)] = r["correct"]
                        except Exception as exc:
                            print(f"    [{q_idx}] cutoff {c} 失败: {exc}")
                            cut_results[str(c)] = {"answer": "", "correct": False,
                                                   "label": f"error: {exc}"}
                            cut_correct[str(c)] = False

                    rec = {
                        "conv_idx": conv_idx, "q_idx": q_idx,
                        "category": cat, "category_name": cat_name,
                        "question": question, "gold": gold,
                        "n_retrieved": len(ranked),
                        "cutoffs": cut_correct, "results": cut_results,
                    }
                    records.append(rec)
                    ckpt.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    ckpt.flush()
                    marks = "".join(
                        "✓" if cut_correct[str(c)] else "✗" for c in cutoffs
                    )
                    print(
                        f"    [{conv_idx}-{q_idx}] {marks} ({','.join(map(str, cutoffs))}) "
                        f"| {cat_name} | retrieved={len(ranked)}",
                        flush=True,
                    )
            finally:
                if mw is not None:
                    await mw.close()
                if tmp is not None:
                    tmp.cleanup()

    metrics = compute_metrics_by_cutoff(records, cutoffs, "category_name")
    report = render_report(
        f"=== LoCoMo mem0 兼容口径 — smilex-ai-memory(embedder={args.embedder}) ===",
        metrics, cutoffs,
        f"answerer={_llm.answer_model()} judge={_llm.judge_model()} "
        f"(mem0 官方为 gpt-5/gpt-5,比较时注意)",
        time.perf_counter() - start_all,
    )
    print("\n" + report)
    md = save_results(
        [f"locomo_mem0compat_n{len(records)}", args.embedder], report, records
    )
    print(f"\n结果已保存: {md}")
    return 0


def main() -> int:
    reopen_stdout()
    parser = argparse.ArgumentParser(description="LoCoMo benchmark(mem0 兼容口径)")
    parser.add_argument("--conversations", default="0,1,2,3,4,5,6,7,8,9",
                        help="跑哪些对话(默认全部 10 个)")
    parser.add_argument("--categories", default="1,2,3,4",
                        help="评哪些 category(mem0 口径默认 1-4,不含 5)")
    parser.add_argument("--max-qa", type=int, default=None, help="每对话最多 N 题(冒烟)")
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
