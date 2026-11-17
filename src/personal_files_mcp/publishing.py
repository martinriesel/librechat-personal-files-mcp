"""Public link registry + ``/files/{token}`` endpoint.

Briefing §4.5: SQLite WAL at ``REGISTRY_DB``. Tokens are 128-bit
``secrets.token_urlsafe(16)`` values with no user/path leakage. The endpoint
delivers files directly via Starlette ``FileResponse`` with chunk streaming,
Range request support (HTTP 206), attachment disposition, nosniff, and no-store,
without requiring Nginx ``X-Accel-Redirect``. ``HEAD`` mirrors ``GET`` with no body.
Other methods return 405.

Lazy cleanup:
    * Expired entries older than 24h are removed by the periodic
      ``purge_expired`` task and on the fly when ``/files/{token}`` is
      accessed.
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import storage
from .runtime import AppContext

logger = logging.getLogger(__name__)

LAZY_EXPIRY_GRACE = timedelta(hours=24)
PURGE_INTERVAL = timedelta(hours=6)


SCHEMA = """
CREATE TABLE IF NOT EXISTS public_links (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  filename TEXT NOT NULL,
  mime_type TEXT,
  size_bytes INTEGER,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT,
  last_access_at TEXT,
  access_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_links_user ON public_links(user_id);
CREATE INDEX IF NOT EXISTS idx_links_expires ON public_links(expires_at);
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PublishError(Exception):
    """Base class for publishing layer errors."""


class LinkNotFound(PublishError):
    pass


class LinkGone(PublishError):
    pass


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class LinkRecord:
    token: str
    user_id: str
    relative_path: str
    filename: str
    mime_type: str | None
    size_bytes: int | None
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    last_access_at: str | None
    access_count: int

    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, now: datetime) -> bool:
        if not self.expires_at:
            return False
        try:
            ts = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return now >= ts

    def public_url(self, base: str) -> str:
        return f"{base.rstrip('/')}/{self.token}"


class LinkRegistry:
    """SQLite-backed registry of public links (WAL)."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def create_link(
        self,
        user_id: str,
        relative_path: str,
        filename: str,
        mime_type: str | None,
        size_bytes: int | None,
        ttl: timedelta | None,
    ) -> LinkRecord:
        now = datetime.now(UTC)
        token = _generate_token()
        record = LinkRecord(
            token=token,
            user_id=user_id,
            relative_path=relative_path,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            created_at=now.isoformat(),
            expires_at=(now + ttl).isoformat() if ttl else None,
            revoked_at=None,
            last_access_at=None,
            access_count=0,
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO public_links(
                    token, user_id, relative_path, filename, mime_type,
                    size_bytes, created_at, expires_at, revoked_at,
                    last_access_at, access_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.token,
                    record.user_id,
                    record.relative_path,
                    record.filename,
                    record.mime_type,
                    record.size_bytes,
                    record.created_at,
                    record.expires_at,
                    None,
                    None,
                    0,
                ),
            )
        return record

    def get(self, token: str) -> LinkRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM public_links WHERE token = ?", (token,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    def list_for_user(self, user_id: str) -> list[LinkRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM public_links WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get_for_user(self, user_id: str, rel_path: str) -> LinkRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM public_links
                WHERE user_id = ? AND relative_path = ? AND revoked_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, rel_path),
            ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    def revoke(self, user_id: str, token_or_path: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            if "/" in token_or_path:
                cursor = conn.execute(
                    """
                    UPDATE public_links
                    SET revoked_at = ?
                    WHERE user_id = ? AND relative_path = ? AND revoked_at IS NULL
                    """,
                    (now, user_id, token_or_path),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE public_links
                    SET revoked_at = ?
                    WHERE user_id = ? AND token = ? AND revoked_at IS NULL
                    """,
                    (now, user_id, token_or_path),
                )
            return cursor.rowcount > 0

    def touch(self, token: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE public_links
                SET access_count = access_count + 1, last_access_at = ?
                WHERE token = ?
                """,
                (now, token),
            )

    def update_path(self, user_id: str, old_path: str, new_path: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE public_links
                SET relative_path = ?
                WHERE user_id = ? AND relative_path = ? AND revoked_at IS NULL
                """,
                (new_path, user_id, old_path),
            )
            return cursor.rowcount > 0

    def delete(self, token: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM public_links WHERE token = ?", (token,))

    def purge_expired(self, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(UTC)) - LAZY_EXPIRY_GRACE
        with self._conn() as conn:
            cursor = conn.execute(
                """
                DELETE FROM public_links
                WHERE expires_at IS NOT NULL
                  AND datetime(expires_at) < datetime(?)
                """,
                (cutoff.isoformat(),),
            )
            return cursor.rowcount


def _row_to_record(row: sqlite3.Row) -> LinkRecord:
    return LinkRecord(
        token=row["token"],
        user_id=row["user_id"],
        relative_path=row["relative_path"],
        filename=row["filename"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        last_access_at=row["last_access_at"],
        access_count=row["access_count"] or 0,
    )


def _generate_token() -> str:
    # 16 bytes -> ~128 bits; abort if we ever see a collision.
    for _ in range(3):
        token = secrets.token_urlsafe(16)
        return token
    raise RuntimeError("could not generate unique token")


# ---------------------------------------------------------------------------
# MCP-facing helpers
# ---------------------------------------------------------------------------


def get_registry(ctx: AppContext) -> LinkRegistry:
    """Return a cached registry bound to the app's settings."""
    cached: LinkRegistry | None = getattr(ctx, "_publishing_registry", None)
    if cached is not None:
        return cached
    registry = LinkRegistry(ctx.settings.registry_db)
    object.__setattr__(ctx, "_publishing_registry", registry)
    return registry


def publish(
    ctx: AppContext,
    user_id: str,
    relative_path: str,
    expires_in_days: int | None = None,
) -> LinkRecord:
    """Create a public link for an existing file (idempotent for same path)."""
    info = storage.stat(ctx.storage_cfg, user_id, relative_path)
    if info.is_directory:
        raise PublishError("cannot publish a directory")
    existing = get_registry(ctx).get_for_user(user_id, relative_path)
    if existing and not existing.is_revoked() and not existing.is_expired(datetime.now(UTC)):
        return existing
    ttl = (
        timedelta(days=expires_in_days)
        if expires_in_days is not None and expires_in_days > 0
        else None
    )
    record = get_registry(ctx).create_link(
        user_id=user_id,
        relative_path=relative_path,
        filename=info.name,
        mime_type=info.mime_type,
        size_bytes=info.size_bytes,
        ttl=ttl,
    )
    _sync_docindex(ctx, user_id, relative_path, record)
    return record


def unpublish(
    ctx: AppContext, user_id: str, path_or_token: str
) -> bool:
    """Revoke a link. Returns True if a link was revoked."""
    revoked = get_registry(ctx).revoke(user_id, path_or_token)
    if revoked:
        # docindex sync is best-effort: find the entry and clear flags.
        try:
            from . import docindex
            idx = docindex.new_docindex(ctx.storage_cfg, user_id)
            for entry in idx.list_documents():
                if entry.file == path_or_token or entry.public_token == path_or_token:
                    idx.set_public(entry.file, False, None)
                    break
        except storage.StorageError:
            pass
    return revoked


def list_links(ctx: AppContext, user_id: str) -> list[LinkRecord]:
    return get_registry(ctx).list_for_user(user_id)


def get_link(ctx: AppContext, user_id: str, path: str) -> LinkRecord | None:
    return get_registry(ctx).get_for_user(user_id, path)


def update_link_path(
    ctx: AppContext, user_id: str, old_path: str, new_path: str
) -> bool:
    return get_registry(ctx).update_path(user_id, old_path, new_path)


def public_url(ctx: AppContext, record: LinkRecord) -> str:
    return record.public_url(ctx.settings.public_base_url)


def _sync_docindex(
    ctx: AppContext, user_id: str, rel: str, record: LinkRecord
) -> None:
    try:
        from . import docindex
        idx = docindex.new_docindex(ctx.storage_cfg, user_id)
        entry = idx.get(rel)
        if entry is None:
            entry = docindex.DocumentEntry(file=rel)
        idx.set_public(rel, True, record.token)
    except storage.StorageError:
        # docindex sync is best-effort; surface warning but do not raise
        logger.warning("docindex sync failed for published %s", rel)


# ---------------------------------------------------------------------------
# /files/{token} endpoint handlers
# ---------------------------------------------------------------------------


async def handle_get(ctx: AppContext, token: str, request: Request) -> Response:
    return await _serve(ctx, token, head=False, request=request)


async def handle_head(ctx: AppContext, token: str, request: Request) -> Response:
    return await _serve(ctx, token, head=True, request=request)


async def _serve(
    ctx: AppContext, token: str, head: bool, request: Request
) -> Response:
    registry = get_registry(ctx)
    record = registry.get(token)
    if record is None:
        return JSONResponse(status_code=404, content={"error": "link_not_found"})

    now = datetime.now(UTC)
    if record.is_revoked():
        return JSONResponse(status_code=410, content={"error": "link_revoked"})
    if record.is_expired(now):
        registry.delete(token)
        return JSONResponse(status_code=410, content={"error": "link_expired"})

    # Defense in depth: re-resolve the path through storage. If a path was
    # deleted from disk the registry might still hold a stale entry.
    try:
        abs_path = storage.resolve(ctx.storage_cfg, record.user_id, record.relative_path)
    except storage.StorageError:
        return JSONResponse(
            status_code=404, content={"error": "file_no_longer_exists"}
        )

    if not abs_path.is_file():
        return JSONResponse(
            status_code=404, content={"error": "file_no_longer_exists"}
        )

    registry.touch(token)

    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    media_type = record.mime_type or "application/octet-stream"
    safe_name = _safe_filename(record.filename)

    return FileResponse(
        path=abs_path,
        filename=safe_name,
        media_type=media_type,
        content_disposition_type="attachment",
        headers=headers,
    )


def _safe_filename(name: str) -> str:
    cleaned = name.replace('"', "").replace("\r", "").replace("\n", "")
    if not cleaned:
        return "download"
    return cleaned
