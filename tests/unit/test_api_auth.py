"""Unit tests for HTTP API Key 鉴权中间件(§15.4)."""

from __future__ import annotations

import pytest

from smilex.server.config import ServerConfig

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from smilex.server.app import create_app  # noqa: E402


@pytest.fixture
def secured_client(tmp_path):
    config = ServerConfig(
        db_path=tmp_path / "auth.db",
        enable_scheduler=False,
        api_key="test-key-123",
    )
    with TestClient(create_app(config)) as c:
        yield c


@pytest.fixture
def open_client(tmp_path):
    """未配置 key: 鉴权零安装,行为与历史版本一致."""
    config = ServerConfig(db_path=tmp_path / "open.db", enable_scheduler=False)
    with TestClient(create_app(config)) as c:
        yield c


# ---------- 保护路径 ----------

def test_reject_without_key(secured_client):
    resp = secured_client.get("/api/stats")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["detail"]


def test_accept_x_api_key_header(secured_client):
    resp = secured_client.get("/api/stats", headers={"X-API-Key": "test-key-123"})
    assert resp.status_code == 200


def test_accept_bearer_authorization(secured_client):
    resp = secured_client.get("/metrics", headers={"Authorization": "Bearer test-key-123"})
    assert resp.status_code == 200
    assert resp.text.startswith("# HELP")


def test_reject_wrong_key(secured_client):
    for headers in (
        {"X-API-Key": "wrong"},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic test-key-123"},  # 非 Bearer 方案忽略
    ):
        assert secured_client.get("/api/stats", headers=headers).status_code == 401


def test_metrics_requires_key(secured_client):
    assert secured_client.get("/metrics").status_code == 401


def test_mcp_mount_requires_key(secured_client):
    """兜底 mount 的 MCP 子应用同样被外层中间件覆盖."""
    resp = secured_client.post("/mcp", json={})
    assert resp.status_code == 401


# ---------- 豁免路径 ----------

def test_health_exempt_but_trimmed(secured_client):
    """无 key: health 可达但只返回 daemon 契约字段(配置摘要不暴露)."""
    data = secured_client.get("/api/health").json()
    assert data["status"] == "ok"
    assert data["auth"] == "required"
    assert "config" not in data and "db" not in data
    # daemon status 依赖的三契约字段
    assert "started_at" in data and "uptime_s" in data


def test_health_full_with_key(secured_client):
    data = secured_client.get(
        "/api/health", headers={"X-API-Key": "test-key-123"}
    ).json()
    assert "config" in data and "db" in data and "scheduler" in data
    assert "auth" not in data


def test_panel_assets_exempt(secured_client):
    assert secured_client.get("/").status_code == 200
    assert secured_client.get("/static/app.js").status_code == 200


# ---------- 默认关闭 ----------

def test_disabled_when_no_key(open_client):
    """未配置 key: 任何路径无 header 直通(向后兼容)."""
    assert open_client.get("/api/stats").status_code == 200
    assert open_client.get("/metrics").status_code == 200
    health = open_client.get("/api/health").json()
    assert "config" in health and "auth" not in health


def test_env_var_beats_config_file(tmp_path, monkeypatch):
    """SMILEX_API_KEY 环境变量优先于配置文件 api_key 字段."""
    monkeypatch.setenv("SMILEX_API_KEY", "env-key-999")
    config = ServerConfig(
        db_path=tmp_path / "env.db", enable_scheduler=False, api_key="file-key"
    )
    assert config.effective_api_key == "env-key-999"
    with TestClient(create_app(config)) as c:
        assert c.get("/api/stats", headers={"X-API-Key": "file-key"}).status_code == 401
        assert c.get("/api/stats", headers={"X-API-Key": "env-key-999"}).status_code == 200


def test_authorized_requests_still_counted(secured_client):
    """鉴权通过后指标计数照常(401 不进计数)."""
    secured_client.get("/api/stats", headers={"X-API-Key": "test-key-123"})
    secured_client.get("/api/stats")  # 401,不计数
    text = secured_client.get("/metrics", headers={"X-API-Key": "test-key-123"}).text
    assert 'smilex_http_requests_total{method="GET",path="/api/stats"} 1' in text


def test_extract_api_key_helper():
    from smilex.server.auth import extract_api_key

    class _Headers(dict):
        def get(self, k, default=None):
            return super().get(k.lower(), default)

    class _Req:
        def __init__(self, headers):
            self.headers = _Headers(headers)

    assert extract_api_key(_Req({"authorization": "Bearer abc"})) == "abc"
    assert extract_api_key(_Req({"authorization": "bearer abc"})) == "abc"
    assert extract_api_key(_Req({"x-api-key": "xyz"})) == "xyz"
    assert extract_api_key(_Req({})) is None
    assert extract_api_key(_Req({"authorization": "Bearer  "})) is None
