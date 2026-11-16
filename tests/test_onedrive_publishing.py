from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from personal_files_mcp.onedrive_publishing import (
    OneDrivePublishing,
    OneDrivePublishingError,
)


class FakeGraph:
    def __init__(self, link_type="organization"):
        self.link_type = link_type
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/items/item-1"):
            return _Response({
                "id": "item-1",
                "parentReference": {"id": "approot"},
                "file": {"mimeType": "text/html"},
            })
        if path.endswith("/permissions/permission-1"):
            return _Response({})
        if path.endswith("/createLink"):
            return _Response({
                "id": "permission-1",
                "link": {"scope": self.link_type, "webUrl": "https://share.example/link"},
            })
        if path.endswith("/content"):
            return _Response({"file": {"mimeType": "text/html"}})
        if path.endswith("/items/item-1"):
            return _Response({"file": {"mimeType": "text/html"}})
        return _Response({"value": []})

    def download(self, path, max_bytes, user_id=None):
        return b"<html><body>report</body></html>"


class ViewerAuth:
    def get_valid_access_token(self, user_id):
        return "access-token"


class _Response:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_publish_file_requests_organization_link(tmp_path):
    graph = FakeGraph()
    publishing = OneDrivePublishing(tmp_path / "publish.db", graph)

    result = publishing.publish_file("user_123456", "item-1")

    method, path, kwargs = graph.calls[1]
    assert method == "POST"
    assert path.endswith("/items/item-1/createLink")
    assert kwargs["json"] == {"type": "view", "scope": "organization"}
    assert result["permission_id"] == "permission-1"


def test_publish_rejects_anonymous_scope(tmp_path):
    publishing = OneDrivePublishing(tmp_path / "publish.db", FakeGraph("anonymous"))

    with pytest.raises(OneDrivePublishingError, match="organization"):
        publishing.publish_file("user_123456", "item-1")


def test_publish_rejects_items_outside_appfolder(tmp_path):
    graph = FakeGraph()
    graph.request = lambda method, path, **kwargs: _Response({
        "id": "item-1",
        "parentReference": {"id": "root"},
    })
    publishing = OneDrivePublishing(tmp_path / "publish.db", graph)

    with pytest.raises(OneDrivePublishingError, match="application folder"):
        publishing.publish_file("user_123456", "item-1")


def test_viewer_token_requires_user_and_is_bound_to_item(tmp_path):
    publishing = OneDrivePublishing(tmp_path / "publish.db", FakeGraph())
    token = publishing.create_viewer_token(
        "user_123456", "item-1", timedelta(minutes=2)
    )

    assert publishing.resolve_viewer_token(token, "user_123456")["item_id"] == "item-1"
    with pytest.raises(OneDrivePublishingError, match="identity"):
        publishing.resolve_viewer_token(token, "user_234567")


def test_viewer_token_expires(tmp_path):
    now = datetime.now(UTC)
    publishing = OneDrivePublishing(tmp_path / "publish.db", FakeGraph(), now=lambda: now)
    token = publishing.create_viewer_token(
        "user_123456", "item-1", timedelta(seconds=1)
    )

    publishing._now = lambda: now + timedelta(seconds=2)
    with pytest.raises(OneDrivePublishingError, match="expired"):
        publishing.resolve_viewer_token(token, "user_123456")


def test_viewer_token_is_single_use(tmp_path):
    publishing = OneDrivePublishing(tmp_path / "publish.db", FakeGraph())
    token = publishing.create_viewer_token("user_123456", "item-1")

    publishing.resolve_viewer_token(token, "user_123456")
    with pytest.raises(OneDrivePublishingError, match="already used"):
        publishing.resolve_viewer_token(token, "user_123456")


def test_unpublish_revokes_only_recorded_org_permission(tmp_path):
    graph = FakeGraph()
    publishing = OneDrivePublishing(tmp_path / "publish.db", graph)
    publishing.publish_file("user_123456", "item-1")

    assert publishing.unpublish_file("user_123456", "item-1") is True
    assert graph.calls[-1][0:2] == (
        "DELETE",
        "/me/drive/items/item-1/permissions/permission-1",
    )
    assert publishing.list_public_links("user_123456") == []


def test_viewer_returns_only_html_content(tmp_path):
    graph = FakeGraph()
    graph._auth = ViewerAuth()
    publishing = OneDrivePublishing(tmp_path / "publish.db", graph)
    token = publishing.create_viewer_token("user_123456", "item-1")

    content, mime = publishing.serve_viewer(token, "user_123456")

    assert content.startswith(b"<html>")
    assert mime == "text/html"


def test_viewer_checks_type_before_downloading_content(tmp_path):
    class NonHtmlGraph(FakeGraph):
        _auth = ViewerAuth()

        def request(self, method, path, **kwargs):
            return _Response({
                "file": {"mimeType": "text/plain"},
                "parentReference": {"id": "approot"},
            })

        def download(self, path, max_bytes, user_id=None):
            raise AssertionError("content must not be downloaded before MIME validation")

    publishing = OneDrivePublishing(tmp_path / "publish.db", NonHtmlGraph())
    token = publishing.create_viewer_token("user_123456", "item-1")

    with pytest.raises(OneDrivePublishingError, match="only serves HTML"):
        publishing.serve_viewer(token, "user_123456")


def test_viewer_rejects_non_html_files(tmp_path):
    graph = FakeGraph()
    graph._auth = ViewerAuth()
    graph.request = lambda method, path, **kwargs: _Response({
        "file": {"mimeType": "text/plain"},
        "parentReference": {"id": "approot"},
    })
    publishing = OneDrivePublishing(tmp_path / "publish.db", graph)
    token = publishing.create_viewer_token("user_123456", "item-1")

    with pytest.raises(OneDrivePublishingError, match="only serves HTML"):
        publishing.serve_viewer(token, "user_123456")


def test_viewer_token_ttl_is_bounded(tmp_path):
    publishing = OneDrivePublishing(tmp_path / "publish.db", FakeGraph())

    with pytest.raises(OneDrivePublishingError, match="TTL"):
        publishing.create_viewer_token("user_123456", "item-1", timedelta(hours=1))


def test_create_link_does_not_create_viewer_token(tmp_path):
    publishing = OneDrivePublishing(tmp_path / "publish.db", FakeGraph())
    sharing = publishing.publish_file("user_123456", "item-1")

    assert "web_url" in sharing
    assert publishing.list_public_links("user_123456")
    assert not any(
        "viewer" in key for key in sharing
    )


def test_unpublish_does_not_remove_viewer_tokens(tmp_path):
    graph = FakeGraph()
    publishing = OneDrivePublishing(tmp_path / "publish.db", graph)
    publishing.publish_file("user_123456", "item-1")
    token = publishing.create_viewer_token("user_123456", "item-1")

    publishing.unpublish_file("user_123456", "item-1")

    assert publishing.resolve_viewer_token(token, "user_123456")["item_id"] == "item-1"


def test_second_user_does_not_receive_another_users_share_link(tmp_path):
    publishing = OneDrivePublishing(tmp_path / "publish.db", FakeGraph())
    publishing.publish_file("user_123456", "item-1")

    assert publishing.get_public_link("user_234567", "item-1") is None
    assert publishing.list_public_links("user_234567") == []
