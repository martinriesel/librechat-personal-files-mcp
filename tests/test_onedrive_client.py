from __future__ import annotations

import httpx
import pytest

from personal_files_mcp.onedrive_client import GraphClient, GraphError


class StaticAuth:
    def get_valid_access_token(self, user_id):
        return "graph-access-token"


def test_graph_client_adds_bearer_auth(respx_mock):
    route = respx_mock.get("https://graph.microsoft.com/v1.0/me/drive/root").respond(
        200, json={"id": "root"}
    )
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=())

    result = graph.request("GET", "/me/drive/root")

    assert result.json()["id"] == "root"
    assert route.calls[0].request.headers["Authorization"] == "Bearer graph-access-token"


def test_graph_client_retries_429_using_retry_after(respx_mock):
    route = respx_mock.get("https://graph.microsoft.com/v1.0/me/drive/root")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"id": "root"}),
    ]
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=(0,))

    result = graph.request("GET", "/me/drive/root")

    assert result.json()["id"] == "root"
    assert route.call_count == 2


def test_graph_client_rejects_untrusted_next_link():
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=())

    with pytest.raises(GraphError, match="untrusted pagination URL"):
        graph.validate_next_link("https://attacker.example/steal")


def test_graph_client_rejects_graph_next_link_with_custom_port():
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=())

    with pytest.raises(GraphError, match="untrusted pagination URL"):
        graph.validate_next_link("https://graph.microsoft.com:444/v1.0/me/drive")


def test_graph_client_upload_session_chunks_at_graph_alignment(respx_mock):
    item_url = "https://graph.microsoft.com/v1.0/me/drive/special/approot:/large.bin"
    session_url = (
        "https://graph.microsoft.com/v1.0/me/drive/special/approot:/large.bin:/createUploadSession"
    )
    upload_url = "https://tenant.up.1drv.com/session-secret"
    payload = b"x" * (4 * 1024 * 1024 + 1)
    respx_mock.post(session_url).respond(200, json={"uploadUrl": upload_url})
    put = respx_mock.put(upload_url).mock(
        side_effect=[
            *[
                httpx.Response(
                    202,
                    json={
                        "nextExpectedRanges": [f"{min((index + 1) * 320 * 1024, len(payload))}-"],
                        **({"completed": True} if index == 10 else {}),
                    },
                )
                for index in range(11)
            ],
            httpx.Response(201, json={"id": "item-1"}),
        ]
    )
    graph = GraphClient(
        StaticAuth(), "user_123456", retry_delays=(), upload_chunk_bytes=320 * 1024
    )
    result = graph.upload_content(item_url, payload, max_bytes=5 * 1024 * 1024)

    assert result["resumable"] is True
    assert result["next_range"] == 320 * 1024
    requests = [call.request for call in put.calls]
    assert len(requests) == 1
    assert requests[0].headers["Content-Range"] == f"bytes 0-{320 * 1024 - 1}/{len(payload)}"


def test_graph_client_rejects_upload_over_configured_limit():
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=())

    with pytest.raises(GraphError, match="file_too_large"):
        graph.upload_content("/item", b"x" * 11, max_bytes=10)


def test_graph_client_download_enforces_response_limit(respx_mock):
    respx_mock.get("https://graph.microsoft.com/v1.0/item/content").respond(
        200, content=b"too large"
    )
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=())

    with pytest.raises(GraphError, match="response exceeds"):
        graph.download("/item/content", max_bytes=4)


def test_graph_client_returns_unauthorized_error_without_retry(respx_mock):
    route = respx_mock.get("https://graph.microsoft.com/v1.0/me/drive/root").respond(401)
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=(0,))

    with pytest.raises(GraphError) as error:
        graph.request("GET", "/me/drive/root")

    assert error.value.code == "onedrive_unauthorized"
    assert route.call_count == 2


def test_graph_client_upload_rejects_non_https_upload_url(respx_mock):
    session_url = (
        "https://graph.microsoft.com/v1.0/me/drive/special/approot:/large.bin:/createUploadSession"
    )
    create_session_url = session_url.removesuffix(":/createUploadSession") + ":/createUploadSession"
    respx_mock.post(create_session_url).respond(
        200, json={"uploadUrl": "http://attacker.test/u"}
    )
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=())

    with pytest.raises(GraphError, match="untrusted upload session"):
        graph.upload_content(
            session_url.removesuffix(":/createUploadSession"),
            b"x" * (4 * 1024 * 1024 + 1),
            5 * 1024 * 1024,
        )


def test_graph_client_upload_session_requires_https_on_subdomain(respx_mock):
    item_url = "https://graph.microsoft.com/v1.0/me/drive/special/approot:/large.bin"
    create_url = item_url + ":/createUploadSession"
    respx_mock.post(create_url).respond(
        200, json={"uploadUrl": "https://attacker.test/upload"}
    )
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=())

    with pytest.raises(GraphError, match="untrusted upload session"):
        graph.upload_content(item_url, b"x" * (4 * 1024 * 1024 + 1), 5 * 1024 * 1024)


def test_upload_session_rejects_arbitrary_external_https_host(respx_mock):
    item_url = "https://graph.microsoft.com/v1.0/me/drive/special/approot:/large.bin"
    respx_mock.post(item_url + ":/createUploadSession").respond(
        200, json={"uploadUrl": "https://attacker.test/upload"}
    )
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=())

    with pytest.raises(GraphError, match="untrusted upload session"):
        graph.upload_content(item_url, b"x" * (4 * 1024 * 1024 + 1), 5 * 1024 * 1024)


@pytest.mark.parametrize(
    "upload_url",
    [
        "https://user@tenant.up.1drv.com/session",
        "https://tenant.up.1drv.com:444/session",
    ],
)
def test_upload_session_rejects_url_credentials_or_custom_port(respx_mock, upload_url):
    item_url = "https://graph.microsoft.com/v1.0/me/drive/special/approot:/large.bin"
    respx_mock.post(item_url + ":/createUploadSession").respond(
        200, json={"uploadUrl": upload_url}
    )
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=())

    with pytest.raises(GraphError, match="untrusted upload session"):
        graph.upload_content(item_url, b"x" * (4 * 1024 * 1024 + 1), 5 * 1024 * 1024)


def test_upload_session_uses_server_reported_next_range(respx_mock):
    item_url = "https://graph.microsoft.com/v1.0/me/drive/special/approot:/large.bin"
    upload_url = "https://tenant.up.1drv.com/session"
    respx_mock.post(item_url + ":/createUploadSession").respond(
        200, json={"uploadUrl": upload_url}
    )
    respx_mock.put(upload_url).mock(
        side_effect=[
            httpx.Response(202, json={"nextExpectedRanges": ["327680-"]}),
            httpx.Response(201, json={"id": "done"}),
        ]
    )
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=(), upload_chunk_bytes=320 * 1024)
    payload = b"x" * (4 * 1024 * 1024 + 1)

    checkpoint = graph.upload_content(item_url, payload, 5 * 1024 * 1024)
    assert checkpoint["next_range"] == 327680


def test_large_upload_returns_resumable_checkpoint_without_exposing_in_error(respx_mock):
    item_url = "https://graph.microsoft.com/v1.0/me/drive/special/approot:/large.bin"
    upload_url = "https://tenant.up.1drv.com/session-secret"
    respx_mock.post(item_url + ":/createUploadSession").respond(
        200, json={"uploadUrl": upload_url}
    )
    respx_mock.put(upload_url).respond(
        202, json={"nextExpectedRanges": ["327680-"]}
    )
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=(), upload_chunk_bytes=320 * 1024)

    checkpoint = graph.upload_content(
        item_url, b"x" * (5 * 1024 * 1024), 6 * 1024 * 1024
    )

    assert checkpoint == {
        "resumable": True,
        "upload_url": upload_url,
        "next_range": 320 * 1024,
        "session_expires": None,
    }


def test_upload_session_can_resume_from_checkpoint(respx_mock):
    item_url = "https://graph.microsoft.com/v1.0/me/drive/special/approot:/large.bin"
    upload_url = "https://tenant.up.1drv.com/session-secret"
    resumed_put = respx_mock.put(upload_url).respond(201, json={"id": "finished"})
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=(), upload_chunk_bytes=320 * 1024)

    result = graph._upload_session(
        item_url,
        b"x" * (5 * 1024 * 1024),
        user_id="user_123456",
        resume_url=upload_url,
        next_range=320 * 1024,
    )

    assert result == {"id": "finished"}
    assert resumed_put.calls[0].request.headers["Content-Range"].startswith("bytes 327680-")


def test_graph_client_retries_once_with_refreshed_token_after_401(respx_mock):
    class RefreshingAuth:
        def __init__(self):
            self.calls = 0

        def get_valid_access_token(self, user_id):
            self.calls += 1
            return f"token-{self.calls}"

    auth = RefreshingAuth()
    route = respx_mock.get("https://graph.microsoft.com/v1.0/me/drive/root").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"id": "root"})]
    )
    graph = GraphClient(auth, "user_123456", retry_delays=())

    result = graph.request("GET", "/me/drive/root")

    assert result.json()["id"] == "root"
    assert auth.calls == 2
    assert route.calls[1].request.headers["Authorization"] == "Bearer token-2"


def test_graph_client_removes_authorization_from_upload_session_request(respx_mock):
    item_url = "https://graph.microsoft.com/v1.0/me/drive/special/approot:/large.bin"
    upload_url = "https://tenant.up.1drv.com/session-secret"
    respx_mock.post(item_url + ":/createUploadSession").respond(
        200, json={"uploadUrl": upload_url}
    )
    upload = respx_mock.put(upload_url).respond(201, json={"id": "file-1"})
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=())

    graph.upload_content(item_url, b"x" * (4 * 1024 * 1024 + 1), 5 * 1024 * 1024)

    assert "Authorization" not in upload.calls[0].request.headers


def test_graph_client_download_streams_and_stops_at_limit(respx_mock):
    route = respx_mock.get("https://graph.microsoft.com/v1.0/item/content").mock(
        return_value=httpx.Response(200, content=b"123456789")
    )
    graph = GraphClient(StaticAuth(), "user_123456", retry_delays=())

    with pytest.raises(GraphError, match="response exceeds"):
        graph.download("/item/content", max_bytes=4)

    assert route.call_count == 1


def test_graph_client_refreshes_token_when_download_returns_401(respx_mock):
    class RefreshingAuth:
        def __init__(self):
            self.calls = 0

        def get_valid_access_token(self, user_id):
            self.calls += 1
            return f"token-{self.calls}"

    auth = RefreshingAuth()
    route = respx_mock.get("https://graph.microsoft.com/v1.0/item/content").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, content=b"ok")]
    )
    graph = GraphClient(auth, "user_123456", retry_delays=())

    assert graph.download("/item/content", 10) == b"ok"
    assert auth.calls == 2
    assert route.calls[1].request.headers["Authorization"] == "Bearer token-2"
