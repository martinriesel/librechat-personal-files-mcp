"""RAG API client (rag_api) with JWT HS256 auth and post-indexing canary.

Matches the *actual* running rag_api (ghcr.io/danny-avila/librechat-rag-api-dev):
    * ``POST /embed``            — multipart/form-data: ``file_id``, ``file``
                                   (binary), optional ``entity_id``.
    * ``POST /query``            — JSON ``{query, file_id (required), k}``;
                                   owner-scoped (metadata ``user_id``).
    * ``POST /query_multiple``   — JSON ``{query, file_ids[], k}``; NOT
                                   owner-scoped — caller MUST pass only the
                                   user's own file_ids.
    * ``DELETE /documents``      — JSON array of file_ids.
    * ``GET  /health``           — readiness.

Auth (briefing §4.4): the MCP issues its own HS256 JWT with the shared
``JWT_SECRET``.  Payload: ``id``, ``sub`` (both = userId), ``email``, ``name``,
``iat``, ``exp`` (300 s).  The rag_api middleware decodes the JWT and sets
``request.state.user``; ownership resolves to ``entity_id`` if provided, else
``request.state.user["id"]``, else ``"public"``.

Response shape (both /query and /query_multiple):
    ``[[{page_content, metadata:{file_id,user_id,...}}, distance], ...]``
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from typing import Any

import httpx
import jwt

from .config import AppSettings

logger = logging.getLogger(__name__)

JWT_EXPIRY_SECONDS = 300
CANARY_RETRIES = 3
CANARY_BACKOFF = (2, 4)  # seconds between retries

RE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
RE_MARKDOWN_BOLD = re.compile(r"\*\*(.+?)\*\*")
RE_MARKDOWN_ITALIC = re.compile(r"\*(.+?)\*")
RE_MARKDOWN_CODE = re.compile(r"`(.+?)`")
RE_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s*")
RE_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _strip_markdown(text: str) -> str:
    """Remove common markdown markers so the canary matches the rag_api loader's
    normalized output (which strips ``**bold**``, ``*italic*``, headings,
    backticks and link syntax)."""
    text = RE_MARKDOWN_BOLD.sub(r"\1", text)
    text = RE_MARKDOWN_ITALIC.sub(r"\1", text)
    text = RE_MARKDOWN_CODE.sub(r"\1", text)
    text = RE_MARKDOWN_LINK.sub(r"\1", text)
    text = RE_MARKDOWN_HEADING.sub("", text)
    return text.strip()


def _clean_for_canary(text: str) -> str:
    """Extract the first meaningful non-heading line for use as a canary phrase."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        cleaned = RE_CONTROL.sub("", stripped)
        cleaned = _strip_markdown(cleaned)
        if cleaned:
            return cleaned[:120].strip()
    return ""


def _canary_phrase(text: str) -> str:
    phrase = _clean_for_canary(text)
    if not phrase:
        return "MCP_CANARY_NO_CONTENT"
    return phrase[:80]


class RagError(Exception):
    pass


class RagClient:
    """Async HTTP client for the rag_api with JWT auth and canary verification."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._settings.rag_api_url.rstrip("/"),
                timeout=60.0,
            )
        return self._client

    def _jwt(self, user_id: str) -> str:
        if not self._settings.jwt_secret_usable():
            raise RagError("JWT_SECRET not configured or too short")
        now = int(time.time())
        payload = {
            "id": user_id,
            "sub": user_id,
            "email": "mcp@personal-files.local",
            "name": "MCP Personal Files",
            "iat": now,
            "exp": now + JWT_EXPIRY_SECONDS,
        }
        return jwt.encode(payload, self._settings.jwt_secret, algorithm="HS256")

    def _auth_headers(self, user_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._jwt(user_id)}"}

    async def health(self) -> dict[str, Any]:
        try:
            client = await self._ensure_client()
            resp = await client.get("/health")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise RagError(f"health check failed: {exc}") from exc

    async def embed(
        self,
        user_id: str,
        file_id: str,
        filename: str,
        contents: str,
    ) -> None:
        """Index a document via ``POST /embed`` (multipart form-data).

        ``file`` carries the UTF-8 text as a binary upload so the rag_api can
        pick the right document loader from the filename/content-type.
        """
        client = await self._ensure_client()
        content_type = mimetypes.guess_type(filename)[0] or "text/plain"
        resp = await client.post(
            "/embed",
            data={"file_id": file_id},
            files={"file": (filename, contents.encode("utf-8"), content_type)},
            headers=self._auth_headers(user_id),
        )
        if resp.status_code == 401:
            raise RagError("rag_api authentication failed — check JWT_SECRET")
        if resp.status_code >= 400:
            raise RagError(
                f"rag_api /embed returned {resp.status_code}: {resp.text[:300]}"
            )
        # rag_api may return 200 even if storage fails downstream (issue #305);
        # the canary check is the authoritative signal.

    async def query_file(
        self,
        user_id: str,
        query: str,
        file_id: str,
        k: int = 4,
    ) -> list[dict[str, Any]]:
        """Semantic search within a single file (owner-scoped) via ``/query``."""
        client = await self._ensure_client()
        resp = await client.post(
            "/query",
            json={"query": query, "file_id": file_id, "k": k},
            headers=self._auth_headers(user_id),
        )
        if resp.status_code == 401:
            raise RagError("rag_api authentication failed")
        if resp.status_code >= 400:
            raise RagError(
                f"rag_api /query returned {resp.status_code}: {resp.text[:300]}"
            )
        return _parse_results(resp.json())

    async def query_multiple(
        self,
        user_id: str,
        query: str,
        file_ids: list[str],
        k: int = 4,
    ) -> list[dict[str, Any]]:
        """Semantic search across several file_ids via ``/query_multiple``.

        The endpoint does NOT enforce ownership; the caller must pass only the
        requesting user's own file_ids (from their docindex).
        """
        client = await self._ensure_client()
        resp = await client.post(
            "/query_multiple",
            json={"query": query, "file_ids": file_ids, "k": k},
            headers=self._auth_headers(user_id),
        )
        if resp.status_code == 401:
            raise RagError("rag_api authentication failed")
        if resp.status_code >= 400:
            raise RagError(
                f"rag_api /query_multiple returned {resp.status_code}: {resp.text[:300]}"
            )
        return _parse_results(resp.json())

    async def delete_document(self, user_id: str, file_id: str) -> None:
        """Delete a document by file_id via ``DELETE /documents`` (JSON array)."""
        client = await self._ensure_client()
        resp = await client.request(
            "DELETE",
            "/documents",
            json=[file_id],
            headers=self._auth_headers(user_id),
        )
        if resp.status_code == 401:
            raise RagError("rag_api authentication failed")
        if resp.status_code not in (200, 204, 404):
            raise RagError(
                f"rag_api /documents DELETE returned {resp.status_code}: {resp.text[:300]}"
            )

    async def canary(
        self, user_id: str, file_id: str, content: str
    ) -> tuple[bool, str | None]:
        """Verify content was indexed by probing with a canary phrase.

        Uses the owner-scoped ``/query`` for the single file_id.  Returns
        (True, None) on success or (False, reason) after retries.
        """
        phrase = _canary_phrase(content)
        if not phrase or phrase == "MCP_CANARY_NO_CONTENT":
            logger.warning("canary: no extractable phrase for %s", file_id)
            return True, None

        for attempt in range(CANARY_RETRIES):
            try:
                results = await self.query_file(user_id, phrase, file_id, k=5)
                for result in results:
                    if phrase[:40] in result.get("content", ""):
                        return True, None
                if attempt < CANARY_RETRIES - 1:
                    await asyncio.sleep(CANARY_BACKOFF[min(attempt, len(CANARY_BACKOFF) - 1)])
            except RagError as exc:
                logger.warning(
                    "canary probe %d/%d failed for %s: %s",
                    attempt + 1, CANARY_RETRIES, file_id, exc,
                )
                if attempt < CANARY_RETRIES - 1:
                    await asyncio.sleep(CANARY_BACKOFF[min(attempt, len(CANARY_BACKOFF) - 1)])
        return False, f"canary phrase not found after {CANARY_RETRIES} retries"

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _parse_results(data: Any) -> list[dict[str, Any]]:
    """Normalize ``[[doc, distance], ...]`` into a list of result dicts."""
    results: list[dict[str, Any]] = []
    if not isinstance(data, list):
        return results
    for item in data:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        doc = item[0]
        if not isinstance(doc, dict):
            continue
        metadata = doc.get("metadata") or {}
        results.append(
            {
                "content": doc.get("page_content", ""),
                "score": float(item[1]),
                "file_id": metadata.get("file_id", ""),
            }
        )
    return results
