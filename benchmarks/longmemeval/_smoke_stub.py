"""端到端冒烟测试 — 用 FakeLLM 替代 OpenAI(不发真实请求,不需要 API key)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _shared.llm_client as llm_client  # noqa: E402
import run_benchmark  # noqa: E402


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Usage:
    total_tokens = 10


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _Completions:
    async def create(self, **kw) -> _Resp:
        system = kw["messages"][0]["content"]
        if "grader" in system:
            return _Resp("yes")
        return _Resp("Business Administration")


class _Chat:
    completions = _Completions()


class FakeClient:
    chat = _Chat()


async def main() -> int:
    llm_client.get_client = lambda: FakeClient()
    run_benchmark.get_client = lambda: FakeClient()
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="longmemeval_s")
    parser.add_argument("--split", default="test")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--embedder", default="hash")
    parser.add_argument("--token-budget", type=int, default=4000)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--dump-context", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(["--limit", "2", "--embedder", "hash", "--resume"])
    return await run_benchmark.run(args)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
