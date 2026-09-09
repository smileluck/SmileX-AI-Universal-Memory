"""maintain CLI 子命令冒烟(2026-09): stdio 项目库的一次性维护入口."""

from __future__ import annotations

import asyncio

from smilex.cli import build_parser
from smilex.memory.contracts import WriteRequest
from smilex.memory.models import MemoryScope
from smilex.middlewares import MemoryMiddleware


def _seed(db) -> None:
    async def _run() -> None:
        async with MemoryMiddleware(db) as mw:
            for i in range(3):
                await mw.write(
                    WriteRequest(scope=MemoryScope.GLOBAL, content=f"维护前记忆 {i}"),
                    session_id="seed",
                )

    asyncio.run(_run())


def test_maintain_runs_tasks(tmp_path, capsys):
    db = tmp_path / "maintain.db"
    _seed(db)

    args = build_parser().parse_args(
        ["maintain", "--db", str(db), "--tasks", "forget,dedup"]
    )
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "[forget] completed" in out
    assert "[dedup] completed" in out


def test_maintain_rejects_unknown_task(tmp_path, capsys):
    db = tmp_path / "maintain.db"
    _seed(db)
    args = build_parser().parse_args(
        ["maintain", "--db", str(db), "--tasks", "bogus"]
    )
    assert args.func(args) == 1
    assert "错误" in capsys.readouterr().err


def test_maintain_missing_db(tmp_path):
    args = build_parser().parse_args(
        ["maintain", "--db", str(tmp_path / "nope.db")]
    )
    assert args.func(args) == 1
