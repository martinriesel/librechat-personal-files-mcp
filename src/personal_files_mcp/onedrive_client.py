"""Microsoft Graph HTTP client with bounded retries and upload support."""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import httpx

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_HOST = "graph.microsoft.com"
UPLOAD_ALIGNMENT = 320 * 1024
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024


class GraphError(Exception):
    """A safe, structured Microsoft Graph failure."""

    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class GraphClient:
    def __init__(
        self,
        auth,
        user_id: str,
        *,
        client: httpx.Client | None = None,
        retry_delays: tuple[float, ...] = (0.25, 0.75, 1.5),
        upload_chunk_bytes: int = 10 * UPLOAD_ALIGNMENT,
    ) -> None:
        if upload_chunk_bytes <= 0 or upload_chunk_bytes % UPLOAD_ALIGNMENT:
            raise ValueError("upload_chunk_bytes must be a positive multiple of 320 KiB")
        self._auth = auth
        self._user_id = user_id
        self._client = client or httpx.Client(timeout=60.0, follow_redirects=False)
        self._owns_client = client is None
        self._retry_delays = retry_delays
        self._chunk_bytes = upload_chunk_bytes

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def request(self, method: str, path_or_url: str, **kwargs) -> httpx.Response:
        url = self._resolve_url(path_or_url)
        request_user_id = kwargs.pop("user_id", None) or self._user_id
        headers = dict(kwargs.pop("headers", {}) or {})
        if _is_upload_url(url):
            headers.pop("Authorization", None)
        else:
            headers["Authorization"] = (
                f"Bearer {self._auth.get_valid_access_token(request_user_id)}"
            )
        for attempt in range(len(self._retry_delays) + 1):
            response = self._client.request(method, url, headers=headers, **kwargs)
            if response.status_code == 401 and not _is_upload_url(url) and attempt == 0:
                headers["Authorization"] = (
                    f"Bearer {self._auth.get_valid_access_token(request_user_id)}"
                )
                response = self._client.request(method, url, headers=headers, **kwargs)
            if response.status_code == 401:
                raise GraphError(
                    "onedrive_unauthorized",
                    "OneDrive authorization expired; execute onedrive_login again",
                    401,
                )
            if response.status_code == 429 and attempt < len(self._retry_delays):
                wait = _retry_after(response, self._retry_delays[attempt])
                asyncio.run(_sleep(wait))
                continue
            if response.status_code in (500, 502, 503, 504) and attempt < len(self._retry_delays):
                asyncio.run(_sleep(self._retry_delays[attempt] * random.uniform(0.8, 1.2)))
                continue
            if response.status_code >= 400:
                raise _graph_error(response)
            return response
        raise GraphError("onedrive_unavailable", "Microsoft Graph retries exhausted")

    async def iter_pages(
        self, path: str, params: dict | None = None, user_id: str | None = None
    ) -> AsyncIterator[dict]:
        next_url: str | None = self._resolve_url(path)
        first = True
        while next_url:
            self.validate_next_link(next_url)
            response = self.request(
                "GET", next_url, params=params if first else None, user_id=user_id
            )
            first = False
            data = response.json()
            if not isinstance(data, dict):
                raise GraphError("onedrive_invalid_response", "Invalid Graph page response")
            yield data
            candidate = data.get("@odata.nextLink")
            next_url = str(candidate) if candidate else None

    def validate_next_link(self, url: str) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != GRAPH_HOST
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise GraphError("onedrive_untrusted_pagination", "untrusted pagination URL")

    def download(self, path_or_url: str, max_bytes: int, user_id: str | None = None) -> bytes:
        url = self._resolve_url(path_or_url)
        headers = {
            "Authorization": f"Bearer {self._auth.get_valid_access_token(user_id or self._user_id)}"
        }
        for attempt in range(2):
            with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code == 401 and attempt == 0:
                    headers["Authorization"] = (
                        f"Bearer {self._auth.get_valid_access_token(user_id or self._user_id)}"
                    )
                    continue
                if response.status_code >= 400:
                    raise _graph_error(response)
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise GraphError(
                            "onedrive_too_large", "response exceeds configured size limit"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        raise GraphError("onedrive_unauthorized", "OneDrive authorization expired", 401)

    def upload_content(
        self, item_url: str, content: bytes, max_bytes: int, user_id: str | None = None
    ) -> dict:
        if len(content) > max_bytes:
            raise GraphError("onedrive_file_too_large", "file_too_large")
        if len(content) <= SIMPLE_UPLOAD_LIMIT:
            return self.request(
                "PUT", item_url, content=content,
                headers={"Content-Type": "application/octet-stream"}, user_id=user_id,
            ).json()
        return self._upload_session(item_url, content, user_id=user_id)

    def _upload_session(
        self,
        item_url: str,
        content: bytes,
        *,
        user_id: str | None,
        resume_url: str | None = None,
        next_range: int = 0,
        session_expires: int | None = None,
    ) -> dict:
        session = (
            self.request(
                "POST",
                item_url.rstrip("/") + ":/createUploadSession",
                json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
                user_id=user_id,
            ).json()
            if resume_url is None
            else {"uploadUrl": resume_url}
        )
        upload_url = session.get("uploadUrl")
        session_expiration = session_expires or _parse_expiration(
            session.get("expirationDateTime")
        )
        if session_expiration is not None and session_expiration <= int(time.time()):
            raise GraphError("onedrive_upload_session_expired", "upload session expired")
        parsed_upload = urlparse(upload_url) if isinstance(upload_url, str) else None
        if (
            parsed_upload is None
            or parsed_upload.scheme != "https"
            or not parsed_upload.hostname
            or parsed_upload.username is not None
            or parsed_upload.password is not None
            or parsed_upload.port not in (None, 443)
            or parsed_upload.hostname == GRAPH_HOST
            or parsed_upload.hostname.endswith(".graph.microsoft.com")
            or not _is_trusted_upload_host(parsed_upload.hostname)
        ):
            raise GraphError(
                "onedrive_invalid_response", "Graph returned an untrusted upload session"
            )
        total = len(content)
        start = next_range
        while start < total:
            expected = session.get("nextExpectedRanges") or []
            if expected:
                try:
                    start = int(str(expected[0]).split("-", 1)[0])
                except ValueError as exc:
                    raise GraphError(
                        "onedrive_invalid_response", "Graph returned an invalid upload range"
                    ) from exc
            end_exclusive = min(start + self._chunk_bytes, total)
            chunk = content[start:end_exclusive]
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end_exclusive - 1}/{total}",
                "Content-Type": "application/octet-stream",
            }
            response = self.request(
                "PUT", upload_url, content=chunk, headers=headers, user_id=user_id
            )
            if response.status_code in (200, 201):
                return response.json()
            if response.status_code != 202:
                raise _graph_error(response)
            if resume_url is None and response.json().get("completed"):
                return {"id": "completed"}
            next_ranges = response.json().get("nextExpectedRanges", [])
            if not next_ranges:
                raise GraphError(
                    "onedrive_invalid_response", "Graph omitted the next upload range"
                )
            try:
                next_start = int(str(next_ranges[0]).split("-", 1)[0])
            except ValueError as exc:
                raise GraphError(
                    "onedrive_invalid_response", "Graph returned an invalid upload range"
                ) from exc
            if next_start <= start or next_start > total:
                raise GraphError(
                    "onedrive_invalid_response", "Graph returned a non-progressing upload range"
                )
            start = next_start
            if start >= total:
                return {"id": "completed"}
            if resume_url is None:
                return {
                    "resumable": True,
                    "upload_url": upload_url,
                    "next_range": start,
                    "session_expires": session_expiration,
                }
        raise GraphError("onedrive_upload_incomplete", "Graph upload session did not complete")

    def _resolve_url(self, path_or_url: str) -> str:
        if path_or_url.startswith("https://"):
            parsed = urlparse(path_or_url)
            if parsed.hostname == GRAPH_HOST:
                self.validate_next_link(path_or_url)
            elif not _is_trusted_upload_host(parsed.hostname or "") or (
                parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
            ):
                raise GraphError("onedrive_untrusted_upload", "untrusted upload URL")
            return path_or_url
        return GRAPH_BASE_URL + "/" + path_or_url.lstrip("/")


def _is_upload_url(url: str) -> bool:
    return urlparse(url).hostname != GRAPH_HOST


def _is_trusted_upload_host(hostname: str) -> bool:
    return (
        hostname == "up.1drv.com"
        or hostname.endswith(".up.1drv.com")
        or hostname.endswith(".sharepoint.com")
    )


def _retry_after(response: httpx.Response, default: float) -> float:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return default
    try:
        return min(max(float(raw), 0.0), 30.0)
    except ValueError:
        return default


async def _sleep(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


def _graph_error(response: httpx.Response) -> GraphError:
    code = {
        401: "onedrive_unauthorized",
        403: "onedrive_forbidden",
        404: "onedrive_not_found",
        409: "onedrive_conflict",
        412: "onedrive_conflict",
        429: "onedrive_rate_limited",
    }.get(response.status_code, "onedrive_graph_error")
    return GraphError(code, f"Microsoft Graph returned HTTP {response.status_code}", response.status_code)


def _parse_expiration(value: str | None) -> int | None:
    if not value:
        return None
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp())
    except ValueError:
        return None
