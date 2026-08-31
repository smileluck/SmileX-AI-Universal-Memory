"""下载 LoCoMo 数据集并缓存为 JSONL.

数据集: GitHub `snap-research/locomo` 的 data/locomo10.json(论文官方数据,
共 10 个长对话样本). 每个样本字段:
- conversation: dict — speaker_a/speaker_b + session_N(list[{speaker, dia_id, text}])
  与 session_N_date_time(如 "1:56 pm on 8 May, 2023")成对出现
- qa: list[{question, answer, evidence, category}] — category 1-5:
  1=single-hop 2=multi-hop 3=temporal 4=open-domain 5=adversarial
  (adversarial 题无 answer 字段, 仅 adversarial_answer; 语义为"不可回答")
- event_summary / observation / session_summary / sample_id

用法:
    uv run python benchmarks/locomo/download_dataset.py
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
REMOTE_URL = (
    "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
)


def dataset_path() -> Path:
    return DATA_DIR / "locomo10.jsonl"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="下载 LoCoMo 数据集")
    parser.add_argument("--force", action="store_true", help="已存在时重新下载")
    args = parser.parse_args()

    out = dataset_path()
    if out.exists() and not args.force:
        n = sum(1 for _ in out.open(encoding="utf-8"))
        print(f"已缓存: {out}({n} 个对话,--force 重新下载)")
        return 0

    print(f"下载 {REMOTE_URL}(~2.7MB) ...")
    with urllib.request.urlopen(REMOTE_URL, timeout=120) as resp:
        rows = json.loads(resp.read().decode("utf-8"))

    # 原始文件是单个 JSON 数组 → 转成 JSONL(每行一个对话样本)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    n_qa = sum(len(r.get("qa", [])) for r in rows)
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"完成: {out}({len(rows)} 个对话, {n_qa} 题)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
