from __future__ import annotations

import pytest

from personal_files_mcp.onedrive_client import GraphError
from personal_files_mcp.onedrive_storage import OneDriveStorage, OneDriveStorageError


class FakeGraph:
    def __init__(self):
        self.calls = []
        self.items = {"": {"id": "approot", "folder": {}}}

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/me/drive/special/approot"):
            return _Response(self.items[""])
        if path == "/me/drive/items/approot/children":
            return _Response({"value": [{"id": "folder-1", "name": "docs", "folder": {}}]})
        if path == "/me/drive/items/folder-1/children":
            return _Response({"value": [{"id": "file-1", "name": "note.md", "file": {}}]})
        raise GraphError("onedrive_not_found", "not found", 404)

    async def iter_pages(self, path, params=None, user_id=None):
        self.calls.append(("PAGES", path, {"user_id": user_id}))
        if path.endswith("/approot/children") or path.endswith("/items/approot/children"):
            yield {"value": [{"id": "folder-1", "name": "docs", "folder": {}}]}
        else:
            yield {"value": [{"id": "file-1", "name": "note.md", "file": {}}]}


class _Response:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_storage_resolves_empty_path_to_approot():
    graph = FakeGraph()
    storage = OneDriveStorage(graph)

    root = storage.resolve_item("")

    assert root["id"] == "approot"
    assert graph.calls[0][1] == "/me/drive/special/approot"


def test_storage_paging_returns_opaque_skip_token():
    class PageGraph(FakeGraph):
        def validate_next_link(self, url):
            assert url.startswith("https://graph.microsoft.com/")

        def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return _Response({
                "value": [{"id": "file-1", "name": "a.md", "file": {}}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/drive/items/approot/children?$skiptoken=opaque-token",
            })

    provider = OneDriveStorage(PageGraph())
    page = provider.list_page(page_size=12)

    assert page["files"][0]["name"] == "a.md"
    assert page["next_page_token"] == "opaque-token"


@pytest.mark.parametrize("path", ["/etc/passwd", "../escape", "a/../b", "a\x00b", "a\nb"])
def test_storage_rejects_unsafe_paths_before_graph_call(path):
    graph = FakeGraph()
    storage = OneDriveStorage(graph)

    with pytest.raises(OneDriveStorageError):
        storage.resolve_path(path)

    assert graph.calls == []


def test_storage_encodes_segments_and_scopes_to_approot():
    graph = FakeGraph()
    storage = OneDriveStorage(graph)
    storage.resolve_path("space name/#file.txt")
    item_path = storage._item_path("space name/#file.txt")

    assert item_path.startswith("/me/drive/special/approot:/")
    assert "space%20name" in item_path
    assert "%23file.txt" in item_path
    assert not item_path.startswith("/me/drive/root")


def test_storage_edit_rejects_ambiguous_replace_before_write():
    class EditGraph(FakeGraph):
        def download(self, path, max_bytes, user_id=None):
            return b"same same"

    graph = EditGraph()
    storage = OneDriveStorage(graph)

    with pytest.raises(OneDriveStorageError, match="exactly once"):
        storage.edit("notes.txt", [{"oldText": "same", "newText": "new"}])

    assert not any(method == "PUT" for method, _, _ in graph.calls)


def test_storage_lists_paged_children_recursively():
    graph = FakeGraph()
    provider = OneDriveStorage(graph)

    files = provider.list(recursive=True, pattern="*.md")

    assert [item["relative_path"] for item in files] == ["docs", "docs/note.md"]


def test_storage_creates_parent_folders_before_upload():
    class WriteGraph(FakeGraph):
        def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            if path.endswith("/me/drive/special/approot"):
                return _Response({"id": "approot", "folder": {}})
            if path.endswith("/me/drive/items/approot/children"):
                if method == "POST":
                    return _Response({"id": "folder-docs", "folder": {}, "name": "docs"})
                return _Response({"value": []})
            if path.endswith("/me/drive/items/folder-docs/children"):
                return _Response({"value": []})
            raise GraphError("onedrive_not_found", "not found", 404)

        def upload_content(self, item_url, content, max_bytes, user_id=None):
            self.calls.append(("UPLOAD", item_url, {"bytes": len(content)}))
            return {"id": "file-1"}

    graph = WriteGraph()
    provider = OneDriveStorage(graph)

    result = provider.write("docs/note.md", b"content")

    assert result["id"] == "file-1"
    assert any(method == "POST" and path.endswith("/children") for method, path, _ in graph.calls)
    assert any(method == "UPLOAD" for method, _, _ in graph.calls)


def test_large_upload_returns_opaque_resume_id_and_hides_upload_url(tmp_path):
    class UploadGraph(FakeGraph):
        def request(self, method, path, **kwargs):
            if path.endswith(":/createUploadSession"):
                return _Response({"uploadUrl": "https://tenant.up.1drv.com/secret"})
            return _Response({})

        def upload_content(self, item_url, content, max_bytes, user_id=None):
            return {
                "resumable": True,
                "upload_url": "https://tenant.up.1drv.com/secret",
                "next_range": 320 * 1024,
                "session_expires": None,
            }

        def _upload_session(self, *args, **kwargs):
            return {"id": "complete"}

    graph = UploadGraph()
    from types import SimpleNamespace

    from cryptography.fernet import Fernet

    graph.auth = SimpleNamespace(
        _vault=SimpleNamespace(
            _config=SimpleNamespace(storage_root=tmp_path),
            _fernet=Fernet(Fernet.generate_key()),
        )
    )
    provider = OneDriveStorage(graph, max_file_bytes=10 * 1024 * 1024, user_id="user_123456")
    started = provider.write("large.bin", b"x" * (5 * 1024 * 1024))

    assert "upload_url" not in started
    assert started["resumable"] is True
    completed = provider.resume_upload(started["upload_id"])
    assert completed == {"id": "complete"}


def test_upload_resume_checkpoint_is_persisted_encrypted_per_user(tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from cryptography.fernet import Fernet

    class CheckpointGraph(FakeGraph):
        def __init__(self):
            super().__init__()
            self.auth = SimpleNamespace(
                _vault=SimpleNamespace(
                    _config=SimpleNamespace(storage_root=tmp_path),
                    _fernet=Fernet(Fernet.generate_key()),
                )
            )

        def upload_content(self, *args, **kwargs):
            return {
                "resumable": True,
                "upload_url": "https://tenant.up.1drv.com/private-upload-token",
                "next_range": 320 * 1024,
                "expiration_date_time": None,
            }

        def _upload_session(self, *args, **kwargs):
            return {"resumable": True, "next_range": 640 * 1024}

    provider = OneDriveStorage(
        CheckpointGraph(), max_file_bytes=8 * 1024 * 1024, user_id="user_123456"
    )
    result = provider.write("large.bin", b"x" * (5 * 1024 * 1024))
    state_path = next((tmp_path / "_system" / "user_123456").glob("*.enc"))

    assert b"private-upload-token" not in state_path.read_bytes()
    assert provider.resume_upload(result["upload_id"]) == {
        "resumable": True,
        "upload_id": result["upload_id"],
    }
    assert Path(state_path).exists()


def test_stored_upload_session_rejects_another_users_identity(tmp_path):
    from types import SimpleNamespace

    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    graph = FakeGraph()
    graph.auth = SimpleNamespace(
        _vault=SimpleNamespace(
            _config=SimpleNamespace(storage_root=tmp_path),
            _fernet=Fernet(key),
        )
    )
    first = OneDriveStorage(graph, user_id="user_123456")
    second = OneDriveStorage(graph, user_id="user_234567")
    first._save_upload_session(
        "a" * 64, "docs/a.bin", b"data", "https://tenant.up.1drv.com/secret", 0
    )

    assert second._load_upload_session("a" * 64) is None


def test_resolve_item_by_id_rejects_items_outside_appfolder():
    class ItemGraph(FakeGraph):
        def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return _Response({
                "id": "outside-item",
                "parentReference": {"id": "root"},
            })

        def _is_inside_approot(self, *args):
            return False

    provider = OneDriveStorage(ItemGraph())

    with pytest.raises(OneDriveStorageError, match="outside"):
        provider.resolve_item_by_id("outside-item")
