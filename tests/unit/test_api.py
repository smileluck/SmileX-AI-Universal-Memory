"""Unit tests for Web 面板只读 API(FastAPI TestClient)."""

from __future__ import annotations

import pytest

from smilex.server.config import ServerConfig

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from smilex.server.app import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    config = ServerConfig(
        db_path=tmp_path / "panel_test.db", enable_scheduler=False
    )
    app = create_app(config)
    with TestClient(app) as c:
        yield c


def _insert_fragment(db, frag_id: str, content: str, layer: str = "L1") -> None:
    """直接落库一条 fragment(触发器自动同步 FTS 索引)."""
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO temporal_fragments(id, fragment_id, time_start, content, "
            "entities, relations, scope, layer, importance, created_at, updated_at) "
            "VALUES (?, ?, '2025-01-01T00:00:00Z', ?, '[]', '[]', 'project:demo', "
            "?, 0.5, '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')",
            (frag_id, frag_id, content, layer),
        )


def test_index_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "SmileX Memory" in resp.text


def test_stats_empty(client):
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["entities"] == 0
    assert "fragments_by_layer" in data


def test_memories_browse_and_detail(client, tmp_path):
    """写入一条 fragment(直接落库)后,浏览/详情端点可见."""
    _insert_fragment(tmp_path / "panel_test.db", "frag1", "面板浏览测试内容")

    resp = client.get("/api/memories", params={"q": "面板浏览"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["kind"] == "fragment" and rows[0]["id"] == "frag1"

    detail = client.get("/api/memory/frag1").json()
    assert detail["table"] == "temporal_fragments"
    assert detail["row"]["content"] == "面板浏览测试内容"


def test_memory_detail_not_found(client):
    resp = client.get("/api/memory/nonexistent-id")
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_recall_test_endpoint(client):
    resp = client.post("/api/recall-test", json={"query": "测试查询"})
    assert resp.status_code == 200
    data = resp.json()
    assert "context" in data and "elapsed_ms" in data


def test_recall_test_focus_params(client):
    """聚焦参数(entity/时间对)与 MCP memory_recall 同语义,未知实体安全降级."""
    resp = client.post(
        "/api/recall-test",
        json={
            "query": "测试查询",
            "entity": "不存在的实体",
            "time_start": "2025-01-01T00:00:00Z",
            "time_end": "2025-12-31T00:00:00Z",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "context" in data and "elapsed_ms" in data


def test_tasks_endpoint(client):
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_health(client, tmp_path):
    data = client.get("/api/health").json()
    assert data["status"] == "ok"
    assert data["uptime_s"] >= 0
    assert "started_at" in data
    cfg = data["config"]
    assert cfg["embedder"] == "hash"
    assert cfg["enable_scheduler"] is False
    assert str(tmp_path / "panel_test.db") in cfg["db_path"]


def test_stats_extended_keys(client):
    data = client.get("/api/stats").json()
    assert data["l0_snapshots"] == 0
    assert data["db_size_bytes"] > 0


def test_memories_short_query_likes_fallback(client, tmp_path):
    """<3 字符的关键词不走 FTS(trigram 需 ≥3 字符),LIKE 回退仍可命中."""
    _insert_fragment(tmp_path / "panel_test.db", "frag2", "短词回退验证")
    rows = client.get("/api/memories", params={"q": "短词"}).json()
    assert len(rows) == 1 and rows[0]["id"] == "frag2"


def test_memories_query_with_quotes(client):
    """关键词含双引号: MATCH 短语转义后不抛语法错."""
    resp = client.get("/api/memories", params={"q": '面板"浏览'})
    assert resp.status_code == 200
    assert resp.json() == []


def test_memories_limit(client, tmp_path):
    db = tmp_path / "panel_test.db"
    for i in range(3):
        _insert_fragment(db, f"lim{i}", f"限量测试内容 {i}")
    rows = client.get("/api/memories", params={"limit": 2}).json()
    assert len(rows) == 2


def test_memories_kind_filter(client, tmp_path):
    _insert_fragment(tmp_path / "panel_test.db", "kf1", "类型过滤的片段内容")
    rows = client.get("/api/memories", params={"kind": "triple"}).json()
    assert rows == []
    rows = client.get("/api/memories", params={"kind": "fragment"}).json()
    assert [r["id"] for r in rows] == ["kf1"]


def test_app_lifespan_registers_quality_tasks(tmp_path):
    """lifespan 装配: 核心任务 + archiver + scope_promotion 默认注册(2026-09 接线)."""
    config = ServerConfig(db_path=tmp_path / "sched.db", enable_scheduler=True)
    app = create_app(config)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        scheduler = app.state.service.scheduler
        assert scheduler is not None
        from smilex.memory.scheduler import CORE_TASK_NAMES

        for name in CORE_TASK_NAMES:
            assert scheduler.has_task(name), name
        assert scheduler.has_task("archive")  # Layer 5 归档(每日)
        assert scheduler.has_task("scope_promotion")  # 跨项目提升(每日)


def test_errors_endpoint(client, tmp_path):
    """GET /api/errors: count 降序 + has_lesson 状态 + message 截断."""
    import sqlite3

    db = tmp_path / "panel_test.db"
    with sqlite3.connect(db) as conn:
        for fp, cnt, lesson in [
            ("fp-major", 7, None), ("fp-lesson", 3, "lesson-id-1"),
        ]:
            conn.execute(
                "INSERT INTO error_fingerprints(fingerprint, count, first_seen, "
                "last_seen, sample_code, sample_message, lesson_id) "
                "VALUES (?, ?, '2026-01-01T00:00:00Z', '2026-09-01T00:00:00Z', "
                "'E_TEST', ?, ?)",
                (fp, cnt, "m" * 200, lesson),
            )
    resp = client.get("/api/errors")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["fingerprint"] for r in rows] == ["fp-major", "fp-lesson"]
    assert rows[0]["count"] == 7 and rows[0]["has_lesson"] is False
    assert rows[1]["has_lesson"] is True
    assert len(rows[0]["sample_message"]) == 120  # 截断


def test_recall_test_entity_resolved_field(client):
    """recall-test 响应带 entity_resolved(None=未聚焦 / False=解析失败降级)."""
    resp = client.post(
        "/api/recall-test",
        json={"query": "x", "entity": "不存在的实体"},
    )
    assert resp.status_code == 200
    assert resp.json()["entity_resolved"] is False

    resp = client.post("/api/recall-test", json={"query": "x"})
    assert resp.json()["entity_resolved"] is None
