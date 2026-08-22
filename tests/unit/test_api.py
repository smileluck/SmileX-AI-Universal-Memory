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
    import sqlite3

    db = tmp_path / "panel_test.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO temporal_fragments(id, fragment_id, time_start, content, "
            "entities, relations, scope, layer, importance, created_at, updated_at) "
            "VALUES ('frag1', 'frag1', '2025-01-01T00:00:00Z', "
            "'面板浏览测试内容', '[]', '[]', 'project:demo', 'L1', 0.5, "
            "'2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')"
        )

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


def test_tasks_endpoint(client):
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    assert resp.json() == []
