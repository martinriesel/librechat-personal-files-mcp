"""RAG client tests with respx mocks (actual rag_api contract)."""
from __future__ import annotations

import httpx
import pytest
import respx

from personal_files_mcp import rag_client
from personal_files_mcp.config import AppSettings


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(
        storage_root="/data/private",
        rag_api_url="http://rag_api.test:8000",
        jwt_secret="x" * 64,
    )


def test_jwt_payload_minimal(settings):
    client = rag_client.RagClient(settings)
    token = client._jwt("5f8d3a4b2c1e0d0001a2b3c4")
    import jwt as pyjwt
    decoded = pyjwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    assert decoded["id"] == "5f8d3a4b2c1e0d0001a2b3c4"
    assert decoded["sub"] == "5f8d3a4b2c1e0d0001a2b3c4"
    assert decoded["email"].startswith("mcp@")
    assert decoded["exp"] - decoded["iat"] == rag_client.JWT_EXPIRY_SECONDS


def test_jwt_disabled_when_secret_short():
    bad = AppSettings(jwt_secret="short")
    client = rag_client.RagClient(bad)
    with pytest.raises(rag_client.RagError):
        client._jwt("5f8d3a4b2c1e0d0001a2b3c4")


def test_canary_strips_markdown_formatting():
    content = (
        "# Title\n\n"
        "**Registration date:** 19 May 2026  \n"
        "**Language:** en-US\n\n"
        "## Objective\n\n"
        "Document body."
    )
    phrase = rag_client._canary_phrase(content)
    assert "**" not in phrase
    assert phrase == "Registration date: 19 May 2026"


def test_canary_skips_headings_and_empty_lines():
    content = "# Title\n\n## Subtitle\n\nReal text here for testing."
    phrase = rag_client._canary_phrase(content)
    assert phrase == "Real text here for testing."


@pytest.mark.asyncio
async def test_embed_sends_multipart_payload(settings):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://rag_api.test:8000/embed").mock(side_effect=handler)
        client = rag_client.RagClient(settings)
        await client.embed("5f8d3a4b2c1e0d0001a2b3c4", "uuid-1", "note.md", "hello content")
        await client.close()

    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["authorization"].startswith("Bearer ")
    assert headers["content-type"].startswith("multipart/form-data")
    body = captured["body"]
    assert b'file_id' in body
    assert b"uuid-1" in body
    assert b"note.md" in body
    assert b"hello content" in body


@pytest.mark.asyncio
async def test_canary_succeeds_when_phrase_found(settings):
    phrase = rag_client._canary_phrase("The quick brown fox jumps over the lazy dog")
    assert "quick brown fox" in phrase

    async def query_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                [
                    {"page_content": phrase, "metadata": {"file_id": "f1"}},
                    0.1,
                ]
            ],
        )

    async def embed_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://rag_api.test:8000/embed").mock(side_effect=embed_handler)
        mock.post("http://rag_api.test:8000/query").mock(side_effect=query_handler)
        client = rag_client.RagClient(settings)
        await client.embed("u", "f1", "x.md", "The quick brown fox jumps over the lazy dog")
        ok, reason = await client.canary("u", "f1", "The quick brown fox jumps over the lazy dog")
        await client.close()
    assert ok is True
    assert reason is None


@pytest.mark.asyncio
async def test_canary_fails_after_retries(settings):
    call_count = {"n": 0}

    async def query_handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=[])

    async def embed_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://rag_api.test:8000/embed").mock(side_effect=embed_handler)
        mock.post("http://rag_api.test:8000/query").mock(side_effect=query_handler)
        client = rag_client.RagClient(settings)
        await client.embed("u", "f1", "x.md", "Some unique content here")
        ok, reason = await client.canary("u", "f1", "Some unique content here")
        await client.close()
    assert ok is False
    assert "retries" in (reason or "")
    assert call_count["n"] == rag_client.CANARY_RETRIES


@pytest.mark.asyncio
async def test_query_file_parses_results(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                [
                    {"page_content": "alpha", "metadata": {"file_id": "f1"}},
                    0.3,
                ],
                [
                    {"page_content": "beta", "metadata": {"file_id": "f2"}},
                    0.5,
                ],
            ],
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://rag_api.test:8000/query").mock(side_effect=handler)
        client = rag_client.RagClient(settings)
        results = await client.query_file("u", "alpha", "f1", k=2)
        await client.close()
    assert len(results) == 2
    assert results[0]["content"] == "alpha"
    assert results[0]["file_id"] == "f1"
    assert results[0]["score"] == 0.3


@pytest.mark.asyncio
async def test_query_multiple_sends_file_ids(settings):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=[[{"page_content": "x", "metadata": {"file_id": "f1"}}, 0.2]],
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://rag_api.test:8000/query_multiple").mock(side_effect=handler)
        client = rag_client.RagClient(settings)
        results = await client.query_multiple("u", "alpha", ["f1", "f2"], k=3)
        await client.close()
    assert captured["body"]["file_ids"] == ["f1", "f2"]
    assert captured["body"]["query"] == "alpha"
    assert results[0]["file_id"] == "f1"


@pytest.mark.asyncio
async def test_delete_document_sends_array(settings):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=True) as mock:
        mock.request("DELETE", "http://rag_api.test:8000/documents").mock(side_effect=handler)
        client = rag_client.RagClient(settings)
        await client.delete_document("u", "f1")
        await client.close()
    assert captured["body"] == ["f1"]


@pytest.mark.asyncio
async def test_health_404_raises(settings):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rag_api.test:8000/health").mock(return_value=httpx.Response(404))
        client = rag_client.RagClient(settings)
        with pytest.raises(rag_client.RagError):
            await client.health()
        await client.close()


@pytest.mark.asyncio
async def test_health_200_returns_json(settings):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rag_api.test:8000/health").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        client = rag_client.RagClient(settings)
        result = await client.health()
        await client.close()
    assert result == {"status": "ok"}
