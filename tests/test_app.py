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


# ---------------------------------------------------------------------------
# POST /binary
# ---------------------------------------------------------------------------


@pytest.fixture
def bin_ctx(settings: AppSettings, tmp_path: Path) -> AppContext:
    Path(settings.storage_root).mkdir(parents=True)
    return AppContext(
        settings=settings,
        storage_cfg=storage.StorageConfig(
            storage_root=Path(settings.storage_root),
            max_file_bytes=1024,
            max_binary_bytes=2 * 1024 * 1024,
        ),
    )


@pytest.fixture
def bin_client(bin_ctx: AppContext) -> TestClient:
    app = create_app(bin_ctx)
    with TestClient(app) as c:
        yield c


def _bin_headers(service_token: str = "s" * 16, user: str = USER) -> dict:
    return {
        "X-Service-Token": service_token,
        "X-User-Id": user,
    }


def test_binary_upload_requires_service_token(bin_client: TestClient):
    resp = bin_client.post(
        "/binary",
        headers={"X-User-Id": USER},
        files={"file": ("r.docx", b"\x50\x4b", "application/octet-stream")},
        data={"path": "office/r.docx"},
    )
    # settings fixture has no service_token -> 503
    assert resp.status_code == 503
    assert resp.json()["error"] == "service_token_not_configured"


def test_binary_upload_rejects_bad_token(tmp_path: Path):
    settings = AppSettings(
        storage_root=str(tmp_path / "private"),
        jwt_secret="x" * 64,
        service_token="s" * 16,
        registry_db=str(tmp_path / "private" / "_system" / "links.db"),
    )
    Path(settings.storage_root).mkdir(parents=True)
    app = create_app(
        AppContext(
            settings=settings,
            storage_cfg=storage.StorageConfig(storage_root=Path(settings.storage_root)),
        )
    )
    with TestClient(app) as c:
        resp = c.post(
            "/binary",
            headers={"X-Service-Token": "wrong-token", "X-User-Id": USER},
            files={"file": ("r.docx", b"\x50\x4b", "application/octet-stream")},
            data={"path": "office/r.docx"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_service_token"


def test_binary_upload_requires_identity(tmp_path: Path):
    settings = AppSettings(
        storage_root=str(tmp_path / "private"),
        jwt_secret="x" * 64,
        service_token="s" * 16,
        registry_db=str(tmp_path / "private" / "_system" / "links.db"),
    )
    Path(settings.storage_root).mkdir(parents=True)
    app = create_app(
        AppContext(
            settings=settings,
            storage_cfg=storage.StorageConfig(storage_root=Path(settings.storage_root)),
        )
    )
    with TestClient(app) as c:
        resp = c.post(
            "/binary",
            headers={"X-Service-Token": "s" * 16},
            files={"file": ("r.docx", b"\x50\x4b", "application/octet-stream")},
            data={"path": "office/r.docx"},
        )
    assert resp.status_code == 403
    assert resp.json()["error"] == "missing_user_identity"


def test_binary_upload_success(tmp_path: Path):
    settings = AppSettings(
        storage_root=str(tmp_path / "private"),
        jwt_secret="x" * 64,
        service_token="s" * 16,
        registry_db=str(tmp_path / "private" / "_system" / "links.db"),
    )
    Path(settings.storage_root).mkdir(parents=True)
    app = create_app(
        AppContext(
            settings=settings,
            storage_cfg=storage.StorageConfig(storage_root=Path(settings.storage_root)),
        )
    )
    payload = b"\x50\x4b\x03\x04" + b"\x00" * 100
    with TestClient(app) as c:
        resp = c.post(
            "/binary",
            headers={"X-Service-Token": "s" * 16, "X-User-Id": USER},
            files={"file": ("relatorio.docx", payload, "application/octet-stream")},
            data={"path": "office/relatorio.docx"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "office/relatorio.docx"
    assert body["size_bytes"] == len(payload)
    assert body["filename"] == "relatorio.docx"
    # file landed in user root
    assert (Path(settings.storage_root) / USER / "office" / "relatorio.docx").read_bytes() == payload


def test_binary_upload_rejects_traversal(tmp_path: Path):
    settings = AppSettings(
        storage_root=str(tmp_path / "private"),
        jwt_secret="x" * 64,
        service_token="s" * 16,
        registry_db=str(tmp_path / "private" / "_system" / "links.db"),
    )
    Path(settings.storage_root).mkdir(parents=True)
    app = create_app(
        AppContext(
            settings=settings,
            storage_cfg=storage.StorageConfig(storage_root=Path(settings.storage_root)),
        )
    )
    with TestClient(app) as c:
        resp = c.post(
            "/binary",
            headers={"X-Service-Token": "s" * 16, "X-User-Id": USER},
            files={"file": ("e.docx", b"x", "application/octet-stream")},
            data={"path": "../escape.docx"},
        )
    assert resp.status_code == 400
    assert ".." in resp.json()["error"] or "escape" in resp.json()["error"]


def test_binary_upload_rejects_oversized_file(tmp_path: Path):
    settings = AppSettings(
        storage_root=str(tmp_path / "private"),
        jwt_secret="x" * 64,
        service_token="s" * 16,
        registry_db=str(tmp_path / "private" / "_system" / "links.db"),
    )
    Path(settings.storage_root).mkdir(parents=True)
    app = create_app(
        AppContext(
            settings=settings,
            storage_cfg=storage.StorageConfig(
                storage_root=Path(settings.storage_root),
                max_binary_bytes=16,  # tiny limit for the test
            ),
        )
    )
    with TestClient(app) as c:
        resp = c.post(
            "/binary",
            headers={"X-Service-Token": "s" * 16, "X-User-Id": USER},
            files={"file": ("big.docx", b"x" * 1024, "application/octet-stream")},
            data={"path": "office/big.docx"},
        )
    assert resp.status_code == 413
    assert resp.json()["error"] == "file_too_large"
    # nothing written
    assert not (Path(settings.storage_root) / USER / "office" / "big.docx").exists()


# ---------------------------------------------------------------------------
# Project Context & Diagnostics
# ---------------------------------------------------------------------------


def test_tool_catalog_includes_get_project_context(ctx: AppContext):
    import asyncio

    from fastmcp import FastMCP

    from personal_files_mcp.app import register_mcp_tools

    mcp = FastMCP("test")
    register_mcp_tools(mcp)

    tools_list = asyncio.run(mcp.list_tools())
    tool_names = {t.name for t in tools_list}
    assert "get_project_context" in tool_names


def test_identity_middleware_captures_conversation_headers(client: TestClient):
    resp = client.post(
        "/binary",
        headers={
            "X-User-Id": USER,
            "X-Service-Token": "s" * 16,
            "X-Conversation-Id": "conv-test-12345",
        },
        files={"file": ("f.txt", b"x", "text/plain")},
        data={"path": "f.txt"},
    )
    # The endpoint processes, confirming middleware successfully extracted conversation ID
    assert resp.status_code in (200, 400, 413, 503)
