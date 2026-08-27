"""Publishing registry + /files/{token} endpoint tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from personal_files_mcp import publishing, storage
from personal_files_mcp.app import AppContext, create_app
from personal_files_mcp.config import AppSettings

USER_A = "5f8d3a4b2c1e0d0001a2b3c4"
USER_B = "aaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        storage_root=str(tmp_path / "private"),
        share_root=str(tmp_path / "share"),
        rag_api_url="http://rag_api:8000",
        jwt_secret="x" * 64,
        public_base_url="https://example.test/files",
        max_file_size_mb=20,
        registry_db=str(tmp_path / "private" / "_system" / "links.db"),
    )


@pytest.fixture
def ctx(settings: AppSettings, tmp_path: Path) -> AppContext:
    Path(settings.storage_root).mkdir(parents=True)
    Path(settings.registry_db).parent.mkdir(parents=True, exist_ok=True)
    return AppContext(
        settings=settings,
        storage_cfg=storage.StorageConfig(storage_root=Path(settings.storage_root)),
    )


@pytest.fixture
def client(ctx: AppContext) -> TestClient:
    app = create_app(ctx)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Registry unit tests
# ---------------------------------------------------------------------------


def test_registry_create_and_get(ctx):
    reg = publishing.get_registry(ctx)
    rec = reg.create_link(
        user_id=USER_A,
        relative_path="docs/a.md",
        filename="a.md",
        mime_type="text/markdown",
        size_bytes=10,
        ttl=timedelta(days=1),
    )
    assert rec.token
    assert rec.expires_at is not None
    fetched = reg.get(rec.token)
    assert fetched is not None
    assert fetched.user_id == USER_A


def test_registry_revoke_by_path(ctx):
    reg = publishing.get_registry(ctx)
    rec = reg.create_link(
        user_id=USER_A,
        relative_path="docs/a.md",
        filename="a.md",
        mime_type=None,
        size_bytes=None,
        ttl=None,
    )
    assert reg.revoke(USER_A, "docs/a.md") is True
    assert reg.get(rec.token).is_revoked() is True


def test_registry_purge_expired(ctx):
    reg = publishing.get_registry(ctx)
    rec_old = reg.create_link(
        user_id=USER_A,
        relative_path="old.md",
        filename="old.md",
        mime_type=None,
        size_bytes=None,
        ttl=timedelta(seconds=1),
    )
    # Backdate it so the grace period makes it purgeable.
    with reg._conn() as conn:
        past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        conn.execute(
            "UPDATE public_links SET expires_at = ? WHERE token = ?",
            (past, rec_old.token),
        )
    purged = reg.purge_expired()
    assert purged >= 1
    assert reg.get(rec_old.token) is None


def test_token_is_urlsafe_128bit(ctx):
    import base64
    reg = publishing.get_registry(ctx)
    rec = reg.create_link(
        user_id=USER_A,
        relative_path="x.md",
        filename="x.md",
        mime_type=None,
        size_bytes=None,
        ttl=None,
    )
    # 16 bytes -> 22 url-safe chars; allow padding buffer
    assert len(rec.token) >= 16
    # roundtrip via urlsafe b64 (16 bytes payload)
    raw = rec.token + "=" * (-len(rec.token) % 4)
    decoded = base64.urlsafe_b64decode(raw)
    assert len(decoded) == 16


# ---------------------------------------------------------------------------
# /files/{token} endpoint
# ---------------------------------------------------------------------------


def _write_file(ctx, name: str) -> None:
    ctx.storage_cfg.storage_root.mkdir(parents=True, exist_ok=True)
    storage.write(ctx.storage_cfg, USER_A, name, b"hello world")


def test_endpoint_404_when_unknown(client: TestClient):
    resp = client.get("/files/no-such-token")
    assert resp.status_code == 404
    assert resp.json()["error"] == "link_not_found"


def test_endpoint_returns_x_accel_redirect(client: TestClient, ctx):
    _write_file(ctx, "docs/a.md")
    rec = publishing.publish(ctx, USER_A, "docs/a.md", expires_in_days=1)
    resp = client.get(f"/files/{rec.token}")
    assert resp.status_code == 200
    assert resp.headers["X-Accel-Redirect"] == f"/_protected/{USER_A}/docs/a.md"
    assert "attachment" in resp.headers["Content-Disposition"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "private, no-store"


def test_endpoint_head_works(client: TestClient, ctx):
    _write_file(ctx, "docs/a.md")
    rec = publishing.publish(ctx, USER_A, "docs/a.md", expires_in_days=1)
    resp = client.head(f"/files/{rec.token}")
    assert resp.status_code == 200
    assert resp.headers["X-Accel-Redirect"] == f"/_protected/{USER_A}/docs/a.md"


def test_endpoint_post_returns_405(client: TestClient):
    resp = client.post("/files/anything", json={})
    assert resp.status_code == 405


def test_endpoint_revoked_returns_410(client: TestClient, ctx):
    _write_file(ctx, "docs/a.md")
    rec = publishing.publish(ctx, USER_A, "docs/a.md", expires_in_days=1)
    publishing.unpublish(ctx, USER_A, "docs/a.md")
    resp = client.get(f"/files/{rec.token}")
    assert resp.status_code == 410


def test_endpoint_expired_returns_410_and_cleans(client: TestClient, ctx):
    _write_file(ctx, "docs/a.md")
    rec = publishing.publish(ctx, USER_A, "docs/a.md", expires_in_days=1)
    # Force expiry
    reg = publishing.get_registry(ctx)
    past = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    with reg._conn() as conn:
        conn.execute(
            "UPDATE public_links SET expires_at = ? WHERE token = ?",
            (past, rec.token),
        )
    resp = client.get(f"/files/{rec.token}")
    assert resp.status_code == 410
    assert reg.get(rec.token) is None


def test_endpoint_increments_access_count(client: TestClient, ctx):
    _write_file(ctx, "docs/a.md")
    rec = publishing.publish(ctx, USER_A, "docs/a.md", expires_in_days=1)
    client.get(f"/files/{rec.token}")
    client.get(f"/files/{rec.token}")
    rec2 = publishing.get_registry(ctx).get(rec.token)
    assert rec2.access_count == 2
    assert rec2.last_access_at is not None


def test_publish_then_get_link(client: TestClient, ctx):
    _write_file(ctx, "docs/a.md")
    rec = publishing.publish(ctx, USER_A, "docs/a.md", expires_in_days=1)
    # get_link returns the same record
    out = publishing.get_link(ctx, USER_A, "docs/a.md")
    assert out is not None
    assert out.token == rec.token
    assert out.public_url(ctx.settings.public_base_url) == rec.public_url(
        ctx.settings.public_base_url
    )


def test_list_links_for_user(client: TestClient, ctx):
    _write_file(ctx, "a.md")
    _write_file(ctx, "b.md")
    publishing.publish(ctx, USER_A, "a.md")
    publishing.publish(ctx, USER_A, "b.md")
    records = publishing.list_links(ctx, USER_A)
    rels = {r.relative_path for r in records}
    assert rels == {"a.md", "b.md"}


def test_publish_directory_rejected(ctx):
    storage.write(ctx.storage_cfg, USER_A, "dir/.keep", b"x")
    import pytest
    with pytest.raises(publishing.PublishError):
        publishing.publish(ctx, USER_A, "dir", expires_in_days=1)


def test_publish_missing_file_rejected(ctx):
    from personal_files_mcp import storage as st
    with pytest.raises(st.StorageError):
        publishing.publish(ctx, USER_A, "missing.md", expires_in_days=1)
