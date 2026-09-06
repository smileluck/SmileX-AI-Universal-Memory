"""Unit tests for 审计日志(§15.3 audit)."""

from __future__ import annotations

import json

from smilex.memory.observability.audit import (
    AuditLogger,
    content_digest,
    default_audit_path,
)


def test_default_audit_path_beside_db(tmp_path):
    db = tmp_path / "proj" / "memory.db"
    assert default_audit_path(db) == tmp_path / "proj" / "audit-memory.jsonl"
    assert default_audit_path(":memory:") is None


def test_disabled_logger_writes_nothing(tmp_path):
    logger = AuditLogger(None)
    assert not logger.enabled
    logger.log("write", session_id="s1")  # 无害直通
    assert list(tmp_path.iterdir()) == []


def test_log_appends_jsonl_with_ts(tmp_path):
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path)
    logger.log("write", session_id="s1", status="saved", memory_ids=["m1"])
    logger.log("init", project="demo")
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "write"
    assert first["session_id"] == "s1"
    assert first["memory_ids"] == ["m1"]
    assert "ts" in first  # 自动补时间戳
    assert json.loads(lines[1])["project"] == "demo"


def test_log_failure_degrades_silently(tmp_path):
    """审计写失败不抛(父目录被文件占位等场景)."""
    (tmp_path / "audit.jsonl").write_text("not a dir", encoding="utf-8")
    # path.parent 链经过文件 audit.jsonl → mkdir 抛 OSError → 只降级
    logger = AuditLogger(tmp_path / "audit.jsonl" / "sub" / "x.jsonl")
    logger.log("write", session_id="s1")  # 不抛即通过


def test_content_digest_stable_and_short():
    assert content_digest("abc") == content_digest("abc")
    assert content_digest("abc") != content_digest("abd")
    assert len(content_digest("x")) == 16


def test_audit_reads_flag():
    logger = AuditLogger(None, audit_reads=True)
    assert logger.audit_reads
