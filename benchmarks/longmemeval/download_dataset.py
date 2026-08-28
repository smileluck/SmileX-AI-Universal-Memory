"""下载 LongMemEval 数据集并缓存为 JSONL.

数据集: HuggingFace `xiaowu0162/longmemeval`(config longmemeval_s,split test).
每个样本字段:
- context: list[list[{role, content}]] — 多轮多 session 对话历史
- question / answer / question_type / question_date
- haystack_session_ids / answer_session_ids

用法:
    uv run python benchmarks/longmemeval/download_dataset.py [--config longmemeval_s]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_CONFIG = "longmemeval_s"
DEFAULT_SPLIT = "test"


def dataset_path(config: str = DEFAULT_CONFIG, split: str = DEFAULT_SPLIT) -> Path:
    return DATA_DIR / f"{config}_{split}.jsonl"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="下载 LongMemEval 数据集")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="数据集配置(默认 longmemeval_s)")
    parser.add_argument("--split", default=DEFAULT_SPLIT, help="数据集 split(默认 test)")
    parser.add_argument("--force", action="store_true", help="已存在时重新下载")
    args = parser.parse_args()

    out = dataset_path(args.config, args.split)
    if out.exists() and not args.force:
        n = sum(1 for _ in out.open(encoding="utf-8"))
        print(f"已缓存: {out}({n} 条样本,--force 重新下载)")
        return 0

    from huggingface_hub import hf_hub_download  # 延迟导入: 仅 benchmark extras 需要

    # 仓库根目录为无后缀的原始 JSON 数组文件(datasets 库解析不了,直接按文件下载)
    remote = args.config
    print(f"下载 xiaowu0162/longmemeval/{remote}(~270MB) ...")
    src = Path(hf_hub_download(
        "xiaowu0162/longmemeval", remote, repo_type="dataset",
    ))

    # 原始文件是单个 JSON 数组 → 转成 JSONL 便于流式处理
    rows = json.loads(src.read_text(encoding="utf-8"))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"完成: {out}({len(rows)} 条样本)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
