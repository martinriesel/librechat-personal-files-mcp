"""OneDrive AppFolder storage operations."""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import mimetypes
import os
import secrets
from urllib.parse import quote

from .onedrive_client import GraphError

APP_ROOT = "/me/drive/special/approot"
MAX_NAME_LENGTH = 255
MAX_PATH_LENGTH = 1024


class OneDriveStorageError(Exception):
    """Safe storage validation or Graph operation failure."""

    def __init__(self, message: str, code: str = "onedrive_storage_error") -> None:
        super().__init__(message)
        self.code = code


class OneDriveStorage:
    def __init__(
        self, client, max_file_bytes: int = 100 * 1024 * 1024, user_id: str = ""
    ) -> None:
        self.client = client
        self.max_file_bytes = max_file_bytes
        self.user_id = user_id
        self._upload_sessions: dict[str, dict] = {}

    def for_user(self, user_id: str) -> OneDriveStorage:
        return OneDriveStorage(self.client, self.max_file_bytes, user_id=user_id)

    def resolve_path(self, path: str) -> tuple[str, ...]:
        if not isinstance(path, str):
            raise OneDriveStorageError("path must be a relative string")
        if "\x00" in path or any(ord(char) < 0x20 for char in path):
            raise OneDriveStorageError("control character in path")
        normalized = path.replace("\\", "/")
        if normalized.startswith("/"):
            raise OneDriveStorageError("absolute path rejected")
        segments = tuple(segment for segment in normalized.split("/") if segment not in ("", "."))
        if any(segment == ".." for segment in segments):
            raise OneDriveStorageError("path traversal '..' rejected")
        if len(normalized) > MAX_PATH_LENGTH:
            raise OneDriveStorageError("path too long")
        if any(len(segment) > MAX_NAME_LENGTH for segment in segments):
            raise OneDriveStorageError("path segment too long")
        return segments

    def _item_path(self, path: str, *, content: bool = False) -> str:
        segments = self.resolve_path(path)
        if not segments:
            return APP_ROOT
        encoded = "/".join(quote(part, safe="") for part in segments)
        suffix = ":/content" if content else ""
        return f"{APP_ROOT}:/{encoded}:{suffix}"

    def resolve_item(self, path: str) -> dict:
        try:
            response = self.client.request("GET", self._item_path(path))
            return response.json()
        except GraphError as exc:
            raise _storage_error(exc) from exc

    def resolve_item_by_id(self, item_id: str) -> dict:
        if not isinstance(item_id, str) or not item_id or "/" in item_id:
            raise OneDriveStorageError("invalid item id")
        try:
            response = self.client.request(
                "GET", f"/me/drive/items/{quote(item_id, safe='')}", user_id=self.user_id
            )
            item = response.json()
            if not self._is_inside_approot(item):
                raise OneDriveStorageError("item is outside the application folder")
            return item
        except GraphError as exc:
            raise _storage_error(exc) from exc

    def _is_inside_approot(self, item: dict) -> bool:
        return _is_inside_approot(self.client, self.user_id, item)

    def list(self, path: str = "", recursive: bool = False, pattern: str | None = None) -> list[dict]:
        folder = self.resolve_item(path)
        queue = [(path, folder)]
        results: list[dict] = []
        while queue:
            parent_path, parent = queue.pop(0)
            item_id = parent.get("id")
            if not item_id:
                continue
            pages = self.client.iter_pages(
                f"/me/drive/items/{quote(item_id, safe='')}/children",
                user_id=self.user_id,
            )
            for page in _run_async(_collect_pages(pages)):
                for item in page.get("value", []):
                    rel = "/".join(filter(None, (parent_path, item.get("name", ""))))
                    is_folder = "folder" in item
                    if (
                        not pattern
                        or is_folder
                        or fnmatch.fnmatch(item.get("name", ""), pattern)
                    ):
                        results.append(_file_info(item, rel))
                    if recursive and is_folder:
                        queue.append((rel, item))
        return results

    def list_page(
        self, path: str = "", page_token: str | None = None, page_size: int = 100
    ) -> dict:
        folder = self.resolve_item(path)
        item_id = quote(folder.get("id", ""), safe="")
        params = {"$top": str(min(max(page_size, 1), 999))}
        if page_token:
            params["$skiptoken"] = page_token
        response = self.client.request(
            "GET",
            f"/me/drive/items/{item_id}/children",
            params=params,
            user_id=self.user_id,
        ).json()
        next_url = response.get("@odata.nextLink")
        next_token = None
        if next_url:
            self.client.validate_next_link(next_url)
            from urllib.parse import parse_qs, urlparse

            next_token = parse_qs(urlparse(next_url).query).get("$skiptoken", [None])[0]
        return {
            "files": [
                _file_info(item, "/".join(filter(None, (path, item.get("name", "")))))
                for item in response.get("value", [])
            ],
            "next_page_token": next_token,
        }

    def read(self, path: str, max_bytes: int = 2 * 1024 * 1024) -> bytes:
        try:
            if self.user_id:
                return self.client.download(
                    self._item_path(path, content=True), max_bytes, user_id=self.user_id
                )
            return self.client.download(self._item_path(path, content=True), max_bytes)
        except GraphError as exc:
            raise _storage_error(exc) from exc

    def write(self, path: str, content: bytes) -> dict:
        if len(content) > self.max_file_bytes:
            raise OneDriveStorageError("file_too_large")
        self._ensure_parent_folders(path)
        try:
            from .onedrive_client import SIMPLE_UPLOAD_LIMIT

            if len(content) > SIMPLE_UPLOAD_LIMIT:
                existing = self._find_upload_session(path)
                if existing:
                    return {"resumable": True, "upload_id": existing["upload_id"]}
            result = self.client.upload_content(
                self._item_path(path, content=True),
                content,
                self.max_file_bytes,
                user_id=self.user_id,
            )
            if result.get("resumable"):
                upload_hash = hashlib.sha256(
                    f"{self.user_id}\0{path}".encode()
                ).hexdigest()
                try:
                    self._save_upload_session(
                        upload_hash,
                        path,
                        content,
                        result["upload_url"],
                        result["next_range"],
                        result.get("session_expires"),
                    )
                except Exception as exc:
                    raise OneDriveStorageError(
                        "could not persist resumable upload state"
                    ) from exc
                return {"resumable": True, "upload_id": upload_hash}
            return result
        except GraphError as exc:
            raise _storage_error(exc) from exc

    def resume_upload(
        self, upload_id: str,
    ) -> dict:
        session = self._load_upload_session(upload_id)
        if session is None:
            raise OneDriveStorageError("upload session not found", code="onedrive_upload_not_found")
        try:
            result = self.client._upload_session(
                self._item_path(session["path"], content=True),
                session["content"],
                user_id=self.user_id,
                resume_url=session["upload_url"],
                next_range=session["next_range"],
                session_expires=session.get("session_expires"),
            )
            if result.get("resumable"):
                session["next_range"] = result["next_range"]
                self._save_upload_session(
                    upload_id,
                    session["path"],
                    session["content"],
                    session["upload_url"],
                    session["next_range"],
                    session.get("session_expires"),
                )
                return {"resumable": True, "upload_id": upload_id}
            self._delete_upload_session(upload_id)
            return result
        except GraphError as exc:
            raise _storage_error(exc) from exc

    def _upload_session_path(self, upload_id: str):
        if not upload_id or any(char not in "0123456789abcdef" for char in upload_id):
            raise OneDriveStorageError("invalid upload id")
        if not self.user_id:
            raise OneDriveStorageError("user identity is required for resumable uploads")
        root = self.client.auth._vault._config.storage_root
        return root / "_system" / self.user_id / f"od-upload-{upload_id}.enc"

    def _save_upload_session(
        self, upload_id: str, path: str, content: bytes, upload_url: str, next_range: int,
        session_expires: int | None = None,
    ) -> None:
        payload = {
            "user_id": self.user_id,
            "path": path,
            "content": base64.b64encode(content).decode("ascii"),
            "upload_url": upload_url,
            "next_range": next_range,
            "session_expires": session_expires,
        }
        vault = self.client.auth._vault
        # Use the token vault's Fernet key for the opaque preauthenticated upload URL.
        target = self._upload_session_path(upload_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        encrypted = vault._fernet.encrypt(json.dumps(payload).encode())
        temp = target.with_name(target.name + ".tmp-" + secrets.token_hex(8))
        try:
            descriptor = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)

    def _load_upload_session(self, upload_id: str) -> dict | None:
        try:
            target = self._upload_session_path(upload_id)
            if not target.exists():
                return None
            payload = json.loads(
                self.client.auth._vault._fernet.decrypt(target.read_bytes()).decode()
            )
            if payload.get("user_id") != self.user_id:
                return None
            payload["content"] = base64.b64decode(payload["content"])
            return payload
        except Exception:
            return None

    def _delete_upload_session(self, upload_id: str) -> None:
        self._upload_session_path(upload_id).unlink(missing_ok=True)

    def _find_upload_session(self, path: str) -> dict | None:
        upload_id = hashlib.sha256(f"{self.user_id}\0{path}".encode()).hexdigest()
        session = self._load_upload_session(upload_id)
        if session:
            session["upload_id"] = upload_id
        return session

    def update(self, path: str, content: bytes) -> dict:
        self.resolve_item(path)
        return self.write(path, content)

    def edit(self, path: str, edits: list[dict[str, str]]) -> dict:
        try:
            text = self.read(path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OneDriveStorageError("file is not UTF-8 text") from exc
        for edit in edits:
            old, new = edit.get("oldText"), edit.get("newText")
            if not isinstance(old, str) or not isinstance(new, str) or text.count(old) != 1:
                raise OneDriveStorageError("each oldText must match exactly once")
            text = text.replace(old, new, 1)
        return self.write(path, text.encode("utf-8"))

    def delete(self, path: str) -> None:
        item = self.resolve_item(path)
        try:
            self.client.request("DELETE", f"/me/drive/items/{quote(item['id'], safe='')}")
        except GraphError as exc:
            raise _storage_error(exc) from exc

    def move(self, src: str, dst: str) -> dict:
        source = self.resolve_item(src)
        dst_segments = self.resolve_path(dst)
        if not dst_segments:
            raise OneDriveStorageError("destination must name a file or folder")
        try:
            destination = self.resolve_item(dst)
        except OneDriveStorageError as exc:
            if exc.code != "onedrive_not_found":
                raise
            destination = None
        if destination is not None:
            raise OneDriveStorageError("destination already exists", code="onedrive_conflict")
        parent_path = "/".join(dst_segments[:-1])
        parent = self.resolve_item(parent_path)
        try:
            response = self.client.request(
                "PATCH",
                f"/me/drive/items/{quote(source['id'], safe='')}",
                json={
                    "name": dst_segments[-1],
                    "parentReference": {"id": parent["id"]},
                },
            )
            return response.json()
        except GraphError as exc:
            raise _storage_error(exc) from exc

    def stat(self, path: str) -> dict:
        item = self.resolve_item(path)
        info = _file_info(item, path)
        if "file" in item:
            info["sha1"] = item.get("file", {}).get("hashes", {}).get("sha1Hash")
            info["quickxor_hash"] = item.get("file", {}).get("hashes", {}).get("quickXorHash")
            info["web_url"] = item.get("webUrl")
        return info

    def read_index(self) -> tuple[str | None, str | None]:
        try:
            item = self.find_index_item()
            if item is None:
                return None, None
            if int(item.get("size", 0) or 0) > 2 * 1024 * 1024:
                raise OneDriveStorageError("OneDrive memory-index.json exceeds 2 MiB")
            content = self.client.download(
                self._item_path("memory-index.json", content=True),
                2 * 1024 * 1024,
                user_id=self.user_id,
            )
            return content.decode("utf-8"), item.get("eTag")
        except OneDriveStorageError:
            raise
        except GraphError as exc:
            if exc.status_code == 404:
                return None, None
            raise _storage_error(exc) from exc

    def find_index_item(self) -> dict | None:
        try:
            return self.resolve_item("memory-index.json")
        except OneDriveStorageError as exc:
            if exc.code == "onedrive_not_found":
                return None
            raise

    def write_index(
        self, content: str, if_match: str | None, create_if_absent: bool = False
    ) -> None:
        self._ensure_parent_folders("memory-index.json")
        headers = {"Content-Type": "application/json"}
        try:
            existing = self.find_index_item()
            if existing is None:
                if if_match is not None:
                    raise OneDriveStorageError(
                        "OneDrive index disappeared", code="onedrive_index_conflict"
                    )
                self.client.request(
                    "PUT",
                    self._item_path("memory-index.json", content=True),
                    content=content.encode("utf-8"),
                    headers=headers,
                )
                return
            if if_match is None:
                raise OneDriveStorageError(
                    "OneDrive index was created concurrently", code="onedrive_index_conflict"
                )
            headers["If-Match"] = if_match
            self.client.request(
                "PUT",
                f"/me/drive/items/{quote(existing['id'], safe='')}/content",
                content=content.encode("utf-8"),
                headers=headers,
            )
        except GraphError as exc:
            if exc.status_code == 412:
                exc.code = "onedrive_index_conflict"
            raise _storage_error(exc) from exc

    def _ensure_parent_folders(self, path: str) -> None:
        segments = self.resolve_path(path)
        parent = ""
        for segment in segments[:-1]:
            parent = "/".join(filter(None, (parent, segment)))
            if not parent:
                continue
            try:
                self.resolve_item(parent)
            except OneDriveStorageError as exc:
                if exc.code != "onedrive_not_found":
                    raise
                parent_segments = self.resolve_path(parent)
                parent_parent = self.resolve_item("/".join(parent_segments[:-1]))
                self.client.request(
                    "POST",
                    f"/me/drive/items/{quote(parent_parent['id'], safe='')}/children",
                    json={
                        "name": segment,
                        "folder": {},
                        "@microsoft.graph.conflictBehavior": "fail",
                    },
                )


def _file_info(item: dict, path: str) -> dict:
    return {
        "relative_path": path,
        "name": item.get("name", ""),
        "id": item.get("id"),
        "is_directory": "folder" in item,
        "size_bytes": int(item.get("size", 0) or 0),
        "mime_type": item.get("file", {}).get("mimeType")
        or mimetypes.guess_type(item.get("name", ""))[0],
        "modified_at": item.get("lastModifiedDateTime"),
        "e_tag": item.get("eTag"),
        "sha256": hashlib.sha256(item.get("id", "").encode()).hexdigest(),
    }


def _is_inside_approot(client, user_id: str, item: dict) -> bool:
    parent = item.get("parentReference") or {}
    parent_id = parent.get("id")
    if not parent_id:
        return False
    if parent_id == "approot":
        return True
    current_id = parent_id
    for _ in range(64):
        try:
            current = client.request(
                "GET", f"/me/drive/items/{quote(current_id, safe='')}", user_id=user_id
            ).json()
        except Exception:
            return False
        remote_parent = current.get("parentReference") or {}
        parent_id = remote_parent.get("id")
        if parent_id == "approot":
            return True
        if not parent_id or parent_id == current_id:
            return False
        current_id = parent_id
    return False


def _storage_error(exc: GraphError) -> OneDriveStorageError:
    return OneDriveStorageError(str(exc), code=exc.code)


async def _collect_pages(pages):
    return [page async for page in pages]


def _run_async(coroutine):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()
