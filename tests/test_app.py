"""App / middleware / health endpoint integration tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from personal_files_mcp import storage
from personal_files_mcp.app import AppContext, create_app
from personal_files_mcp.config import AppSettings

USER = "5f8d3a4b2c1e0d0001a2b3c4"


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        storage_root=str(tmp_path / "private"),
        rag_api_url="http://rag_api.test:8000",
        jwt_secret="x" * 64,
        registry_db=str(tmp_path / "private" / "_system" / "links.db"),
    )


@pytest.fixture
def ctx(settings: AppSettings, tmp_path: Path) -> AppContext:
    Path(settings.storage_root).mkdir(parents=True)
    return AppContext(
        settings=settings,
        storage_cfg=storage.StorageConfig(storage_root=Path(settings.storage_root)),
    )


@pytest.fixture
def client(ctx: AppContext) -> TestClient:
    app = create_app(ctx)
    with TestClient(app) as c:
        yield c


def test_healthz_bypasses_identity(client: TestClient, ctx):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert "rag" in body
    assert "storage" in body


def test_healthz_degraded_without_secret(tmp_path: Path):
    bad = AppSettings(
        storage_root=str(tmp_path / "private"),
        jwt_secret="",
        registry_db=str(tmp_path / "private" / "_system" / "links.db"),
    )
    Path(bad.storage_root).mkdir(parents=True)
    app = create_app(
        AppContext(
            settings=bad,
            storage_cfg=storage.StorageConfig(storage_root=Path(bad.storage_root)),
        )
    )
    with TestClient(app) as c:
        resp = c.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["jwt_configured"] is False


def test_mcp_route_requires_identity(client: TestClient):
    resp = client.get("/mcp")
    # No X-User-Id, so middleware returns 403
    assert resp.status_code == 403
    assert resp.json()["error"] == "missing_user_identity"


def test_mcp_route_rejects_invalid_identity(client: TestClient):
    resp = client.get("/mcp", headers={"X-User-Id": "bad"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "invalid_user_identity"


def test_files_route_bypasses_identity(client: TestClient):
    # Public endpoint — no identity required, but unknown token must 404
    resp = client.get("/files/nope")
    assert resp.status_code == 404
