"""对 checkpoint 里的缓存预测重新判卷 — judge prompt/模型变更后免重灌入.

用法:
    uv run python benchmarks/longmemeval/rejudge.py            # 判后打印最新总分
    uv run python benchmarks/longmemeval/rejudge.py --dry-run  # 只看不改
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from judge import judge_answer
from llm_client import get_client

CKPT = Path(__file__).parent / "results" / "ckpt_longmemeval_s_test_sentence-transformers.jsonl"


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印新判定,不写回")
    args = parser.parse_args()

    if not CKPT.exists():
        raise SystemExit(f"checkpoint 不存在: {CKPT}")

    records = [json.loads(line) for line in CKPT.open(encoding="utf-8")]
    client = get_client()

    changed = 0
    for rec in records:
        if not rec["prediction"]:
            continue  # 空预测(被过滤/失败)无需重判
        correct, verdict_raw, tokens = await judge_answer(
            client, rec["question"], rec["gold"], rec["prediction"]
        )
        if correct != rec["correct"] or verdict_raw != rec["verdict_raw"]:
            changed += 1
            mark = "✓" if correct else "✗"
            print(
                f"[{rec['idx']}] {rec['correct']} → {mark} | "
                f"gold={rec['gold'][:40]!r} pred={rec['prediction'][:60]!r}"
            )
        if not args.dry_run:
            rec["correct"], rec["verdict_raw"], rec["judge_tokens"] = (
                correct, verdict_raw, tokens
            )

    by_type: dict[str, list[bool]] = defaultdict(list)
    for rec in records:
        by_type[rec["question_type"]].append(rec["correct"])
    overall = sum(r["correct"] for r in records) / len(records) * 100
    print(f"\nre-judge 完成: {changed} 条判定变化")
    print(f"Overall Accuracy: {overall:.1f} %")
    for qtype in sorted(by_type):
        flags = by_type[qtype]
        print(f"  {qtype:<24} {sum(flags)}/{len(flags)}")

    if not args.dry_run and changed:
        with CKPT.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"已写回 {CKPT.name}(下次 --resume 直接复用新判定)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
