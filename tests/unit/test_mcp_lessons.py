"""Unit tests for 错误指纹 + 教训闭环(§ 主动优化收官,DSH 自我纠错模式)."""

from __future__ import annotations

import json

import pytest

from smilex.server.config import ServerConfig
from smilex.server.lessons import (
    LESSON_IMPORTANCE,
    LESSON_PREFIX,
    error_fingerprint,
    normalize_error_text,
    prepare_lesson_write,
)
from smilex.server.mcp_server import MemoryService, create_mcp_server


@pytest.fixture
async def service(tmp_path):
    svc = MemoryService(ServerConfig(db_path=tmp_path / "lesson_test.db"))
    yield svc
    await svc.close()


async def _call(server, tool: str, args: dict) -> dict:
    result = await server.call_tool(tool, args)
    if hasattr(result, "content"):
        blocks = result.content
    elif isinstance(result, tuple):
        blocks = result[0]
    else:
        blocks = result
    return json.loads(blocks[0].text)


# ---------- 指纹函数(确定性) ----------

def test_normalize_strips_volatile_parts():
    assert normalize_error_text("FileNotFound: /tmp/x-42.py") == normalize_error_text(
        "filenotfound: /tmp/x-99.py"
    )
    assert normalize_error_text("A  B\t\nC") == "a b c"
    assert normalize_error_text("") == ""


def test_fingerprint_stable_and_scoped():
    a = error_fingerprint("project:p1", "TimeoutError", "request timed out after 30s")
    b = error_fingerprint("project:p1", "TimeoutError", "request timed out after 60s")
    c = error_fingerprint("project:p2", "TimeoutError", "request timed out after 30s")
    assert a == b  # 数字归一 → 同指纹
    assert a != c  # 跨 scope 分开计数


def test_prepare_lesson_write():
    text, importance = prepare_lesson_write("根因是 X", 0.5)
    assert text == f"{LESSON_PREFIX}根因是 X"
    assert importance == LESSON_IMPORTANCE
    # 已带前缀不重复;显式更高重要度保留
    text2, importance2 = prepare_lesson_write(f"{LESSON_PREFIX}已带前缀", 0.99)
    assert text2 == f"{LESSON_PREFIX}已带前缀"
    assert importance2 == 0.99


# ---------- 工具闭环 ----------

async def test_report_error_first_then_repeat(service):
    server = create_mcp_server(service)
    first = await _call(
        server, "memory_report_error",
        {"code": "BuildError", "message": "module not found in build-123",
         "scope": "global"},
    )
    assert first["count"] == 1
    assert first["lesson"] is None
    assert first["should_write_lesson"] is False
    assert first["fingerprint"]

    # 同错误再报(数字扰动归一) → count=2 + should_write_lesson
    second = await _call(
        server, "memory_report_error",
        {"code": "BuildError", "message": "module not found in build-456",
         "scope": "global"},
    )
    assert second["count"] == 2
    assert second["should_write_lesson"] is True
    assert "错误原因" in second["advice"]


async def test_lesson_write_links_and_returns(service):
    server = create_mcp_server(service)
    report = await _call(
        server, "memory_report_error",
        {"code": "ImportError", "message": "cannot import name 'X'", "scope": "global"},
    )
    fp = report["fingerprint"]

    written = await _call(
        server, "memory_write",
        {
            "content": "根因是循环导入;仅在 A 与 B 互相 import 顶层时复现,"
                       "延迟导入仍可行",
            "scope": "global",
            "error_fingerprint": fp,
        },
    )
    assert written["status"] == "saved"
    memory = await service.get()
    cur = await memory.engine.conn.execute(
        "SELECT content, importance FROM temporal_fragments WHERE id = ?",
        [written["memory_id"]],
    )
    row = await cur.fetchone()
    assert row["content"].startswith(LESSON_PREFIX)
    assert float(row["importance"]) == LESSON_IMPORTANCE  # ≥0.9 → 删除守卫保护

    # 再次上报(第 2 次 report;教训写入不计数) → 响应直接携带教训内容
    third = await _call(
        server, "memory_report_error",
        {"code": "ImportError", "message": "cannot import name 'X'", "scope": "global"},
    )
    assert third["count"] == 2
    assert third["lesson"] is not None
    assert LESSON_PREFIX in third["lesson"]
    assert third["should_write_lesson"] is False


async def test_rewritten_lesson_points_to_latest(service):
    server = create_mcp_server(service)
    report = await _call(
        server, "memory_report_error",
        {"code": "DBError", "message": "database is locked", "scope": "global"},
    )
    fp = report["fingerprint"]
    first = await _call(
        server, "memory_write",
        {"content": "第一版教训", "scope": "global", "error_fingerprint": fp},
    )
    second = await _call(
        server, "memory_write",
        {"content": "第二版教训(更新认知)", "scope": "global",
         "error_fingerprint": fp},
    )
    memory = await service.get()
    cur = await memory.engine.conn.execute(
        "SELECT lesson_id FROM error_fingerprints WHERE fingerprint = ?", [fp]
    )
    assert str((await cur.fetchone())["lesson_id"]) == second["memory_id"]
    # 旧教训 fragment 不删除(superseded 轻量版: 失链不删)
    cur = await memory.engine.conn.execute(
        "SELECT COUNT(*) AS c FROM temporal_fragments WHERE id = ?",
        [first["memory_id"]],
    )
    assert int((await cur.fetchone())["c"]) == 1


async def test_report_error_scope_fallback_without_project(service):
    """PROJECT 无 scope_id 且未初始化项目 → 回落 global,不报错."""
    server = create_mcp_server(service)
    resp = await _call(
        server, "memory_report_error",
        {"code": "AnyError", "message": "x", "scope": "project"},
    )
    assert resp["count"] == 1


# ---------- 治理联动 ----------

async def test_lesson_survives_forget(service, tmp_path):
    """教训(importance 0.95)被删除守卫保护,forget 不删."""
    from datetime import timedelta

    from smilex.memory.scheduler.checkpoint import CheckpointStore, InterruptContext
    from smilex.memory.scheduler.tasks.forget import forget
    from smilex.utils.timeutil import now_utc, to_iso

    server = create_mcp_server(service)
    report = await _call(
        server, "memory_report_error",
        {"code": "OldError", "message": "ancient failure", "scope": "global"},
    )
    written = await _call(
        server, "memory_write",
        {
            "content": "很久以前沉淀的教训",
            "scope": "global",
            "error_fingerprint": report["fingerprint"],
        },
    )
    memory = await service.get()
    # 把教训标记成极旧(直接改 updated_at),模拟陈旧化
    await memory.engine.conn.execute(
        "UPDATE temporal_fragments SET updated_at = ? WHERE id = ?",
        [to_iso(now_utc() - timedelta(days=400)), written["memory_id"]],
    )
    await memory.engine.conn.commit()
    stats = await forget(
        memory.engine,
        InterruptContext("t", CheckpointStore(memory.engine)),
        {"threshold": 0.5},
    )
    assert stats["protected"] >= 1 and stats["forgotten"] == 0
