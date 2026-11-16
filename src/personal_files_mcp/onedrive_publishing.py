"""Organization-scoped OneDrive sharing and authenticated viewer tokens."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .onedrive_client import GraphError


class OneDrivePublishingError(Exception):
    """Safe link creation, revocation, or viewer failure."""


class OneDrivePublishing:
    def __init__(self, db_path: str | Path, graph, now=None) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._graph = graph
        self._now = now or (lambda: datetime.now(UTC))
        with self._connect() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS onedrive_permissions (
                       user_id TEXT NOT NULL, item_id TEXT NOT NULL,
                       permission_id TEXT NOT NULL, web_url TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       PRIMARY KEY(user_id, item_id, permission_id)
                   );
                   CREATE TABLE IF NOT EXISTS onedrive_viewer_tokens (
                       token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                       item_id TEXT NOT NULL, expires_at TEXT NOT NULL,
                       used_at TEXT
                   );"""
            )

    def _connect(self):
        conn = sqlite3.connect(str(self._path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def publish_file(self, user_id: str, item_id: str) -> dict:
        self._assert_item_in_approot(user_id, item_id)
        try:
            response = self._graph.request(
                "POST",
                f"/me/drive/items/{item_id}/createLink",
                json={"type": "view", "scope": "organization"},
            )
            data = response.json()
        except GraphError as exc:
            raise OneDrivePublishingError("unable to create organizational link") from exc
        link = data.get("link") or {}
        if link.get("scope") != "organization" or not link.get("webUrl"):
            raise OneDrivePublishingError("Graph did not return an organization-scoped link")
        permission_id = data.get("id")
        if not permission_id:
            raise OneDrivePublishingError("Graph returned a link without permission ID")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO onedrive_permissions VALUES(?,?,?,?,?)",
                (user_id, item_id, permission_id, link["webUrl"], _iso(self._now())),
            )
        return {"permission_id": permission_id, "web_url": link["webUrl"], "scope": "organization"}

    def unpublish_file(self, user_id: str, item_id: str) -> bool:
        self._assert_item_in_approot(user_id, item_id)
        with self._connect() as conn:
            records = conn.execute(
                "SELECT permission_id FROM onedrive_permissions WHERE user_id=? AND item_id=?",
                (user_id, item_id),
            ).fetchall()
        for record in records:
            try:
                self._graph.request(
                    "DELETE",
                    f"/me/drive/items/{item_id}/permissions/{record['permission_id']}",
                )
            except GraphError as exc:
                if exc.status_code != 404:
                    raise OneDrivePublishingError("unable to revoke organizational link") from exc
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM onedrive_permissions WHERE user_id=? AND item_id=?",
                (user_id, item_id),
            )
        return bool(records)

    def add_viewer_token_to_url(self, base_url: str, token: str) -> str:
        return f"{base_url.rstrip('/')}/{token}"

    def list_public_links(self, user_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_id,permission_id,web_url,created_at FROM onedrive_permissions "
                "WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_public_link(self, user_id: str, item_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT item_id,permission_id,web_url,created_at FROM onedrive_permissions "
                "WHERE user_id=? AND item_id=? ORDER BY created_at DESC LIMIT 1",
                (user_id, item_id),
            ).fetchone()
        return dict(row) if row else None

    def create_viewer_token(
        self, user_id: str, item_id: str, ttl: timedelta = timedelta(minutes=5)
    ) -> str:
        if ttl <= timedelta(0) or ttl > timedelta(minutes=10):
            raise OneDrivePublishingError("viewer token TTL must be between 0 and 10 minutes")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO onedrive_viewer_tokens(token_hash,user_id,item_id,expires_at) "
                "VALUES(?,?,?,?)",
                (token_hash, user_id, item_id, _iso(self._now() + ttl)),
            )
        return token

    def resolve_viewer_token(self, token: str, authenticated_user_id: str) -> dict:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM onedrive_viewer_tokens WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise OneDrivePublishingError("viewer token not found")
            if row["user_id"] != authenticated_user_id:
                conn.rollback()
                raise OneDrivePublishingError("viewer identity does not match token")
            if datetime.fromisoformat(row["expires_at"]) <= self._now():
                conn.execute("DELETE FROM onedrive_viewer_tokens WHERE token_hash=?", (token_hash,))
                conn.commit()
                raise OneDrivePublishingError("viewer token expired")
            if row["used_at"]:
                conn.rollback()
                raise OneDrivePublishingError("viewer token already used")
            conn.execute(
                "UPDATE onedrive_viewer_tokens SET used_at=? WHERE token_hash=?",
                (_iso(self._now()), token_hash),
            )
            conn.commit()
            return {"user_id": row["user_id"], "item_id": row["item_id"]}
        finally:
            conn.close()

    def viewer_item(self, token: str, authenticated_user_id: str) -> dict:
        record = self.resolve_viewer_token(token, authenticated_user_id)
        try:
            access_token = self._graph._auth.get_valid_access_token(authenticated_user_id)
        except Exception as exc:
            raise OneDrivePublishingError("OneDrive authorization is unavailable") from exc
        if not access_token:
            raise OneDrivePublishingError("OneDrive authorization is unavailable")
        try:
            item = self._graph.request(
                "GET", f"/me/drive/items/{record['item_id']}"
            ).json()
        except GraphError as exc:
            raise OneDrivePublishingError("unable to retrieve viewer file") from exc
        mime = (item.get("file") or {}).get("mimeType", "")
        if mime not in ("text/html", "application/xhtml+xml"):
            raise OneDrivePublishingError("viewer only serves HTML files")
        if not _is_inside_appfolder(self._graph, authenticated_user_id, item):
            raise OneDrivePublishingError("viewer item is outside the application folder")
        return {"item_id": record["item_id"], "mime_type": mime}

    def serve_viewer(self, token: str, authenticated_user_id: str) -> tuple[bytes, str]:
        item = self.viewer_item(token, authenticated_user_id)
        try:
            content = self._graph.download(
                f"/me/drive/items/{item['item_id']}/content",
                max_bytes=5 * 1024 * 1024,
                user_id=authenticated_user_id,
            )
        except GraphError as exc:
            raise OneDrivePublishingError("unable to retrieve viewer file") from exc
        return content, item["mime_type"]

    def get_item_id_by_path(self, user_id: str, storage, path: str) -> str:
        try:
            return str(storage.resolve_item(path)["id"])
        except Exception as exc:
            raise OneDrivePublishingError("OneDrive item not found") from exc

    def remove_viewer_token(self, token: str) -> None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM onedrive_viewer_tokens WHERE token_hash=?", (token_hash,)
            )

    def _assert_item_in_approot(self, user_id: str, item_id: str) -> None:
        try:
            item = self._graph.request(
                "GET", f"/me/drive/items/{item_id}"
            ).json()
        except GraphError as exc:
            raise OneDrivePublishingError("OneDrive item not found") from exc
        if not _is_inside_appfolder(self._graph, user_id, item):
            raise OneDrivePublishingError("sharing is limited to the application folder")


def _is_inside_appfolder(graph, user_id: str, item: dict) -> bool:
    parent = item.get("parentReference") or {}
    parent_id = parent.get("id")
    if parent_id == "approot" or parent.get("path", "").endswith("/approot"):
        return True
    for _ in range(64):
        if not parent_id:
            return False
        try:
            current = graph.request(
                "GET", f"/me/drive/items/{parent_id}", user_id=user_id
            ).json()
        except Exception:
            return False
        parent = current.get("parentReference") or {}
        parent_id = parent.get("id")
        if parent_id == "approot" or parent.get("path", "").endswith("/approot"):
            return True
    return False


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
