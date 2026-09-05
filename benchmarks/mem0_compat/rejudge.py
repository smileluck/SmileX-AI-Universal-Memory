"""离线重判 mem0_compat 结果文件 — 不重新 ingest,只重跑 judge.

背景: 判卷模型偶尔在 <judge_thinking> 里耗尽 max_tokens,最终 yes/no
没吐出来被 parse 默认记 False(答案明明正确)。本工具读取结果 JSON,
对所有(或仅失败的)entry 重跑 judge,翻转误判并重算汇总。

用法:
    export OPENAI_API_KEY=...  # 同 runner 约定
    uv run python benchmarks/mem0_compat/rejudge.py <结果.json>            # 只重判失败项
    uv run python benchmarks/mem0_compat/rejudge.py <结果.json> --all      # 全部重判
    uv run python benchmarks/mem0_compat/rejudge.py <结果.json> --dry-run  # 只看会重判哪些
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import mem0_prompts as mp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _shared.llm_client as _llm  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"


async def rejudge(path: Path, *, all_entries: bool, dry_run: bool) -> int:
    client = _llm.get_client()

    data = json.loads(path.read_text(encoding="utf-8"))
    records: list[dict] = data["records"]
    is_lme = records and "cutoffs" in records[0] and "results" in records[0]

    n_rerun = n_flip = 0
    for rec in records:
        if is_lme:
            entries = [
                (str(c), rec["results"][str(c)]) for c in rec.get("cutoffs", {})
            ]
            prompt_tpl, parse = mp.LME_JUDGE_PROMPT, mp.parse_lme_judge
        else:  # locomo 结果(单层 answer 字段)
            entries = [("", rec)]
            prompt_tpl, parse = mp.LOCOMO_JUDGE_PROMPT, mp.parse_locomo_judge

        for cutoff, entry in entries:
            answer = entry.get("answer") or entry.get("prediction") or ""
            if not answer or (entry.get("correct") and not all_entries):
                continue
            if dry_run:
                print(f"[dry] {rec.get('qid', '?')} cutoff={cutoff or '-'}: 将重判")
                continue
            n_rerun += 1
            prompt = prompt_tpl.format(
                question=rec["question"], answer=rec["gold"], response=answer
            )
            verdict, _ = await _llm.chat(
                client, _llm.judge_model(), "", prompt,
                max_tokens=2048, disable_thinking=True,
            )
            correct, label = parse(verdict)
            if correct != entry.get("correct"):
                n_flip += 1
                mark = "✗→✓" if correct else "✓→✗"
                print(
                    f"[flip] {rec.get('qid', '?')} cutoff={cutoff or '-'} {mark} "
                    f"| {rec['question'][:60]}"
                )
            entry["correct"] = correct
            entry["label"] = label

    if dry_run:
        return 0

    # 重算 cutoffs 聚合 + 报告
    if is_lme:
        for rec in records:
            rec["cutoffs"] = {
                c: rec["results"][c]["correct"] for c in rec["results"]
            }
        cutoffs = sorted(records[0]["results"], key=int)
        lines = [
            f"=== rejudge 后({path.stem}) ===",
            f"重判 {n_rerun} 项,翻转 {n_flip} 项",
        ]
        for c in cutoffs:
            ok = sum(1 for r in records if r["cutoffs"][c])
            lines.append(f"cutoff {c}: {ok}/{len(records)} = {ok / len(records) * 100:.1f}%")
        report = "\n".join(lines)
        print("\n" + report)
        data["report"] = data.get("report", "") + "\n\n" + report

    out = path.with_name(path.stem + "_rejudged.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存: {out}(原文件不动)")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="mem0_compat 结果离线重判")
    parser.add_argument("result_json", help="results/ 下的结果 JSON 路径")
    parser.add_argument("--all", action="store_true", help="全部重判(默认只重判失败项)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    path = Path(args.result_json)
    # 带目录分隔符的相对路径按 cwd 解析;纯文件名才默认 results/ 下
    if not path.is_absolute() and "/" not in args.result_json:
        path = RESULTS_DIR / path
    if not path.exists():
        raise SystemExit(f"文件不存在: {path}")
    return asyncio.run(rejudge(path, all_entries=args.all, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
