"""MCP tool implementations (storage, documentation, publishing, RAG).

All public functions in this module follow D003:
    * success returns a JSON string
    * failure returns a string starting with ``ERROR: <code> — <reason>``

Each function takes ``cfg`` and ``user_id`` explicitly. The MCP layer
(``app.py``) injects ``user_id`` from the validated request; tests inject it
directly. The tools never accept a user_id as a free argument from the LLM.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import datetime as _dt
import hashlib
import json
import logging
import uuid
from dataclasses import asdict
from typing import Any

from . import docindex, storage

logger = logging.getLogger(__name__)

MAX_TEXT_DISPLAY_BYTES = 2 * 1024 * 1024
DOCUMENTATION_SUBDIR = "docs"


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def _ok(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _err(code: str, reason: str, **extra: Any) -> str:
    body = {"error": code, "reason": reason}
    body.update(extra)
    return "ERROR: " + json.dumps(body, ensure_ascii=False)


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# AppContext binding (used by publishing + RAG tools)
# ---------------------------------------------------------------------------


def bind_publish_ctx(ctx) -> None:
    """Bind the live AppContext so publishing/RAG tools can find it."""
    global _PUBLISH_CTX
    _PUBLISH_CTX = ctx


def _resolve_publish_ctx(cfg: storage.StorageConfig):
    if _PUBLISH_CTX is None:
        raise RuntimeError("publishing context not configured")
    return _PUBLISH_CTX


_PUBLISH_CTX = None  # type: ignore[assignment]


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _run(coro):
    return asyncio.run(coro)


def _run_rag(ctx, coro_factory):
    """Run a RAG operation in a single event loop with a single client.

    The ``httpx.AsyncClient`` inside ``RagClient`` is bound to the event loop it
    was created in. Reusing it across multiple ``asyncio.run()`` calls raises
    ``RuntimeError: Event loop is closed``. This helper creates the client,
    runs the factory-provided coroutine, and closes the client within one loop.
    """
    from . import rag_client

    async def wrapper():
        client = rag_client.RagClient(ctx.settings)
        try:
            return await coro_factory(client)
        finally:
            await client.close()

    return asyncio.run(wrapper())


# ---------------------------------------------------------------------------
# Storage tools
# ---------------------------------------------------------------------------


def list_files(
    cfg: storage.StorageConfig,
    user_id: str,
    path: str = "",
    recursive: bool = False,
    pattern: str | None = None,
) -> str:
    try:
        items = storage.list(cfg, user_id, path, recursive=recursive, pattern=pattern)
    except storage.StorageError as exc:
        return _err("invalid_path", str(exc))
    return _ok(
        {
            "path": path,
            "recursive": recursive,
            "pattern": pattern,
            "count": len(items),
            "files": [asdict(i) for i in items],
        }
    )


def read_file(cfg: storage.StorageConfig, user_id: str, path: str) -> str:
    try:
        info = storage.stat(cfg, user_id, path)
    except storage.StorageError as exc:
        return _err("not_found", str(exc))
    if info.is_directory:
        return _err("is_directory", "path is a directory")
    try:
        data = storage.read(cfg, user_id, path)
    except storage.StorageError as exc:
        return _err("read_failed", str(exc))

    sha = _hash_bytes(data)
    if info.size_bytes > MAX_TEXT_DISPLAY_BYTES:
        return _err(
            "too_large",
            f"file is {info.size_bytes} bytes (> {MAX_TEXT_DISPLAY_BYTES})",
            sha256=sha,
            size_bytes=info.size_bytes,
        )
    try:
        text = data.decode("utf-8")
        return _ok(
            {
                "content": text,
                "is_binary": False,
                "size_bytes": info.size_bytes,
                "sha256": sha,
            }
        )
    except UnicodeDecodeError:
        return _ok(
            {
                "content": None,
                "is_binary": True,
                "size_bytes": info.size_bytes,
                "sha256": sha,
                "preview_hex": data[:64].hex(),
            }
        )


def write_file(
    cfg: storage.StorageConfig, user_id: str, path: str, content: str
) -> str:
    if not isinstance(content, str):
        return _err("invalid_content", "content must be a UTF-8 string")
    try:
        info = storage.write(cfg, user_id, path, content.encode("utf-8"))
    except storage.StorageError as exc:
        return _err("write_failed", str(exc))
    doc_updated = _maybe_register_doc(cfg, user_id, path)
    return _ok({"file": asdict(info), "documentation_updated": doc_updated})


def update_file(
    cfg: storage.StorageConfig, user_id: str, path: str, content: str
) -> str:
    if not isinstance(content, str):
        return _err("invalid_content", "content must be a UTF-8 string")
    try:
        info = storage.update(cfg, user_id, path, content.encode("utf-8"))
    except storage.StorageError as exc:
        return _err("update_failed", str(exc))
    doc_updated = _maybe_register_doc(cfg, user_id, path)
    return _ok({"file": asdict(info), "documentation_updated": doc_updated})


def delete_file(cfg: storage.StorageConfig, user_id: str, path: str) -> str:
    try:
        storage.delete(cfg, user_id, path)
    except storage.StorageError as exc:
        return _err("delete_failed", str(exc))
    return _ok({"deleted": path})


def move_file(
    cfg: storage.StorageConfig, user_id: str, src: str, dst: str
) -> str:
    try:
        storage.move(cfg, user_id, src, dst)
    except storage.StorageError as exc:
        return _err("move_failed", str(exc))
    return _ok({"moved": {"src": src, "dst": dst}})


def get_file_info(cfg: storage.StorageConfig, user_id: str, path: str) -> str:
    try:
        info = storage.stat(cfg, user_id, path)
    except storage.StorageError as exc:
        return _err("not_found", str(exc))
    out: dict[str, Any] = asdict(info)
    if not info.is_directory and info.name.endswith((".md", ".txt")):
        try:
            entry = docindex.new_docindex(cfg, user_id).get(path)
            if entry is not None:
                out["documentation"] = entry.to_dict()
        except storage.StorageError:
            pass
    return _ok(out)


def read_multiple_files(
    cfg: storage.StorageConfig, user_id: str, paths: list[str]
) -> str:
    """Read several text files at once.

    Returns a per-path result. Each entry is either ``content`` (text) or an
    ``error`` string. Binary/oversized files are flagged, not dumped raw.
    """
    if not isinstance(paths, (list, tuple)):
        return _err("invalid_paths", "paths must be a list of relative paths")
    results: dict[str, Any] = {}
    for path in paths:
        if not isinstance(path, str):
            results[str(path)] = {"error": "path must be a string"}
            continue
        try:
            info = storage.stat(cfg, user_id, path)
        except storage.StorageError as exc:
            results[path] = {"error": str(exc)}
            continue
        if info.is_directory:
            results[path] = {"error": "is a directory"}
            continue
        try:
            data = storage.read(cfg, user_id, path)
        except storage.StorageError as exc:
            results[path] = {"error": str(exc)}
            continue
        if info.size_bytes > MAX_TEXT_DISPLAY_BYTES:
            results[path] = {
                "error": f"file too large ({info.size_bytes} bytes)",
                "sha256": _hash_bytes(data),
            }
            continue
        try:
            results[path] = {
                "content": data.decode("utf-8"),
                "is_binary": False,
                "size_bytes": info.size_bytes,
            }
        except UnicodeDecodeError:
            results[path] = {
                "content": None,
                "is_binary": True,
                "size_bytes": info.size_bytes,
                "sha256": _hash_bytes(data),
                "preview_hex": data[:64].hex(),
            }
    return _ok({"files": results})


def edit_file(
    cfg: storage.StorageConfig,
    user_id: str,
    path: str,
    edits: list[dict[str, str]],
) -> str:
    """Apply find/replace edits to a text file.

    Each edit is ``{"oldText": ..., "newText": ...}``. Every ``oldText`` must
    match exactly once in the file (like the filesystem MCP); ambiguous or
    missing matches are rejected before any write happens.
    """
    if not isinstance(edits, (list, tuple)) or not edits:
        return _err("invalid_edits", "edits must be a non-empty list of {oldText, newText}")
    normalized: list[tuple[str, str]] = []
    for edit in edits:
        if not isinstance(edit, dict):
            return _err("invalid_edits", "each edit must be {oldText, newText}")
        old = edit.get("oldText")
        new = edit.get("newText")
        if not isinstance(old, str) or not isinstance(new, str):
            return _err("invalid_edits", "oldText and newText must be strings")
        if old == "":
            return _err("invalid_edits", "oldText cannot be empty")
        normalized.append((old, new))

    try:
        data = storage.read(cfg, user_id, path)
    except storage.StorageError as exc:
        return _err("not_found", str(exc))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _err("not_text", "file is not UTF-8 decodable")

    # Validate every match before writing anything (atomic semantics).
    for old, _new in normalized:
        count = text.count(old)
        if count == 0:
            return _err("no_match", f"oldText not found: {old[:80]!r}")
        if count > 1:
            return _err(
                "ambiguous_match",
                f"oldText matched {count} times (must be unique): {old[:80]!r}",
            )

    updated = text
    for old, new in normalized:
        updated = updated.replace(old, new, 1)

    try:
        info = storage.update(cfg, user_id, path, updated.encode("utf-8"))
    except storage.StorageError as exc:
        return _err("update_failed", str(exc))
    _maybe_register_doc(cfg, user_id, path)
    return _ok({"file": asdict(info), "edits_applied": len(normalized)})


# ---------------------------------------------------------------------------
# Binary file tools
# ---------------------------------------------------------------------------


def write_binary_file(
    cfg: storage.StorageConfig, user_id: str, path: str, content_base64: str
) -> str:
    """Write a binary file (base64 payload) to the user's private storage.

    Uses the binary size limit (``MAX_BINARY_FILE_MB``). Does not touch the
    documentation index (binary files are not RAG-able).
    """
    if not isinstance(content_base64, str):
        return _err("invalid_content", "content_base64 must be a base64 string")
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (ValueError, binascii.Error):
        return _err("invalid_base64", "content is not valid base64")
    if not data:
        return _err("invalid_content", "content is empty")
    try:
        info = storage.write(
            cfg,
            user_id,
            path,
            data,
            max_bytes=cfg.max_binary_bytes or storage.MAX_BINARY_BYTES,
        )
    except storage.StorageError as exc:
        return _err("write_failed", str(exc))
    return _ok(
        {
            "file": asdict(info),
            "size_bytes": len(data),
            "sha256": _hash_bytes(data),
            "indexed": False,
        }
    )


def read_binary_file(
    cfg: storage.StorageConfig, user_id: str, path: str
) -> str:
    """Read any file as base64 (works for binary and text alike).

    Unlike ``read_file`` there is no UTF-8 requirement and no 2 MB display cap.
    """
    try:
        info = storage.stat(cfg, user_id, path)
    except storage.StorageError as exc:
        return _err("not_found", str(exc))
    if info.is_directory:
        return _err("is_directory", "path is a directory")
    try:
        data = storage.read(cfg, user_id, path)
    except storage.StorageError as exc:
        return _err("read_failed", str(exc))
    return _ok(
        {
            "content_base64": base64.b64encode(data).decode("ascii"),
            "is_binary": True,
            "size_bytes": len(data),
            "sha256": _hash_bytes(data),
            "mime_type": info.mime_type,
        }
    )


# ---------------------------------------------------------------------------
# Documentation tools
# ---------------------------------------------------------------------------


def _sanitize_doc_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename:
        raise storage.StorageError("filename is required")
    cleaned = filename.replace("\\", "/")
    if cleaned.startswith("/") or any(part == ".." for part in cleaned.split("/")):
        raise storage.StorageError("invalid documentation filename")
    base = cleaned.split("/")[-1]
    if not base or base in (".", ".."):
        raise storage.StorageError("invalid documentation filename")
    if base.startswith("."):
        raise storage.StorageError("hidden files are not allowed")
    return base


def save_documentation(
    cfg: storage.StorageConfig,
    user_id: str,
    filename: str,
    content: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    topics: list[str] | None = None,
) -> str:
    if not isinstance(content, str):
        return _err("invalid_content", "content must be a UTF-8 string")
    try:
        safe = _sanitize_doc_filename(filename)
    except storage.StorageError as exc:
        return _err("invalid_filename", str(exc))
    rel = f"{DOCUMENTATION_SUBDIR}/{safe}"
    try:
        storage.write(cfg, user_id, rel, content.encode("utf-8"))
    except storage.StorageError as exc:
        return _err("write_failed", str(exc))

    entry = docindex.DocumentEntry(
        file=rel,
        title=title or "",
        description=description or "",
        tags=list(tags or []),
        topics=list(topics or []),
    )
    docindex.new_docindex(cfg, user_id).upsert(entry)
    info = storage.stat(cfg, user_id, rel)
    return _ok(
        {
            "file": rel,
            "saved": True,
            "indexed": False,
            "published": False,
            "suggestion": "Call index_document to add to knowledge base",
            "stat": asdict(info),
        }
    )


def update_documentation(
    cfg: storage.StorageConfig,
    user_id: str,
    filename: str,
    content: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    topics: list[str] | None = None,
) -> str:
    if not isinstance(content, str):
        return _err("invalid_content", "content must be a UTF-8 string")
    try:
        safe = _sanitize_doc_filename(filename)
    except storage.StorageError as exc:
        return _err("invalid_filename", str(exc))
    rel = f"{DOCUMENTATION_SUBDIR}/{safe}"
    try:
        storage.stat(cfg, user_id, rel)
    except storage.StorageError:
        return _err("not_found", "documentation file does not exist")
    try:
        storage.update(cfg, user_id, rel, content.encode("utf-8"))
    except storage.StorageError as exc:
        return _err("update_failed", str(exc))

    existing = docindex.new_docindex(cfg, user_id).get(rel) or docindex.DocumentEntry(file=rel)
    entry = docindex.DocumentEntry(
        file=rel,
        title=title if title is not None else existing.title,
        description=description if description is not None else existing.description,
        tags=list(tags) if tags is not None else existing.tags,
        topics=list(topics) if topics is not None else existing.topics,
        priority=existing.priority,
        language=existing.language,
        type=existing.type,
        rag_indexed=existing.rag_indexed,
        rag_file_id=existing.rag_file_id,
        indexing_status=existing.indexing_status,
        public=existing.public,
        public_token=existing.public_token,
    )
    docindex.new_docindex(cfg, user_id).upsert(entry)
    return _ok(
        {
            "file": rel,
            "updated": True,
            "suggestion": "Call index_document to refresh knowledge base",
        }
    )


def get_document_metadata(
    cfg: storage.StorageConfig, user_id: str, filename: str
) -> str:
    try:
        safe = _sanitize_doc_filename(filename)
    except storage.StorageError as exc:
        return _err("invalid_filename", str(exc))
    rel = f"{DOCUMENTATION_SUBDIR}/{safe}"
    entry = docindex.new_docindex(cfg, user_id).get(rel)
    if entry is None:
        return _err("not_found", "documentation entry not present in index")
    return _ok(entry.to_dict())


# ---------------------------------------------------------------------------
# Publishing tools (Fase B)
# ---------------------------------------------------------------------------


def publish_file(
    cfg: storage.StorageConfig,
    user_id: str,
    path: str,
    expires_in_days: int | None = None,
) -> str:
    from . import publishing

    try:
        ctx = _resolve_publish_ctx(cfg)
    except RuntimeError as exc:
        return _err("publishing_unavailable", str(exc))
    try:
        record = publishing.publish(ctx, user_id, path, expires_in_days=expires_in_days)
    except publishing.PublishError as exc:
        return _err("publish_failed", str(exc))
    except storage.StorageError as exc:
        return _err("not_found", str(exc))
    return _ok(
        {
            "token": record.token,
            "url": publishing.public_url(ctx, record),
            "relative_path": record.relative_path,
            "expires_at": record.expires_at,
            "revoked": False,
        }
    )


def unpublish_file(
    cfg: storage.StorageConfig, user_id: str, path_or_token: str
) -> str:
    from . import publishing

    try:
        ctx = _resolve_publish_ctx(cfg)
    except RuntimeError as exc:
        return _err("publishing_unavailable", str(exc))
    if not publishing.unpublish(ctx, user_id, path_or_token):
        return _err("not_found", "no active link for that path or token")
    return _ok({"revoked": path_or_token})


def get_public_link(
    cfg: storage.StorageConfig, user_id: str, path: str
) -> str:
    from . import publishing

    try:
        ctx = _resolve_publish_ctx(cfg)
    except RuntimeError as exc:
        return _err("publishing_unavailable", str(exc))
    record = publishing.get_link(ctx, user_id, path)
    if record is None or record.is_revoked() or record.is_expired(_now()):
        return _err("not_found", "no active public link for this path")
    return _ok(
        {
            "url": publishing.public_url(ctx, record),
            "token": record.token,
            "expires_at": record.expires_at,
            "created_at": record.created_at,
        }
    )


def list_public_links(
    cfg: storage.StorageConfig, user_id: str
) -> str:
    from . import publishing

    try:
        ctx = _resolve_publish_ctx(cfg)
    except RuntimeError as exc:
        return _err("publishing_unavailable", str(exc))
    records = publishing.list_links(ctx, user_id)
    return _ok(
        {
            "count": len(records),
            "links": [
                {
                    "token": r.token,
                    "url": publishing.public_url(ctx, r),
                    "relative_path": r.relative_path,
                    "expires_at": r.expires_at,
                    "revoked": r.is_revoked(),
                    "access_count": r.access_count,
                }
                for r in records
            ],
        }
    )


# ---------------------------------------------------------------------------
# RAG tools (Fase C)
# ---------------------------------------------------------------------------


def search_knowledge(
    cfg: storage.StorageConfig,
    user_id: str,
    query: str,
    limit: int = 8,
) -> str:
    from . import rag_client

    if not isinstance(query, str) or not query.strip():
        return _err("invalid_query", "query must be a non-empty string")
    if not isinstance(limit, int) or limit < 1 or limit > 50:
        return _err("invalid_limit", "limit must be 1..50")
    try:
        ctx = _resolve_publish_ctx(cfg)
    except RuntimeError:
        return _err("rag_unavailable", "AppContext not configured")
    if not ctx.settings.jwt_secret_usable():
        return _err("rag_disabled", "JWT_SECRET not configured or too short")
    index = docindex.new_docindex(cfg, user_id)
    file_ids = [
        d.rag_file_id
        for d in index.list_documents()
        if d.rag_file_id and d.indexing_status == "indexed"
    ]
    if not file_ids:
        return _ok(
            {
                "query": query,
                "limit": limit,
                "count": 0,
                "results": [],
                "note": "no indexed documents",
            }
        )

    async def flow(client):
        return await client.query_multiple(user_id, query, file_ids, limit)

    try:
        results = _run_rag(ctx, flow)
    except rag_client.RagError as exc:
        return _err("rag_failed", str(exc))
    return _ok(
        {
            "query": query,
            "limit": limit,
            "count": len(results),
            "results": results,
        }
    )


def index_document(
    cfg: storage.StorageConfig, user_id: str, path: str
) -> str:
    from . import rag_client

    try:
        ctx = _resolve_publish_ctx(cfg)
    except RuntimeError:
        return _err("rag_unavailable", "AppContext not configured")
    if not ctx.settings.jwt_secret_usable():
        return _err("rag_disabled", "JWT_SECRET not configured or too short")
    try:
        content = storage.read(cfg, user_id, path)
    except storage.StorageError as exc:
        return _err("not_found", str(exc))
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _err("not_text", "file is not UTF-8 decodable")

    file_id = str(uuid.uuid4())
    index = docindex.new_docindex(cfg, user_id)
    filename = path.rsplit("/", 1)[-1]

    async def flow(client):
        await client.embed(user_id, file_id, filename, text)
        return await client.canary(user_id, file_id, text)

    try:
        ok, reason = _run_rag(ctx, flow)
    except rag_client.RagError as exc:
        index.set_indexing_status(path, "indexing_failed", rag_file_id=file_id)
        return _err("rag_embed_failed", str(exc))
    if not ok:
        index.set_indexing_status(path, "indexing_failed", rag_file_id=file_id)

        async def cleanup(client):
            await client.delete_document(user_id, file_id)

        with contextlib.suppress(rag_client.RagError):
            _run_rag(ctx, cleanup)
        return _err("rag_canary_failed", reason or "canary check failed")
    index.set_indexing_status(path, "indexed", rag_file_id=file_id)
    return _ok({"indexed": True, "rag_file_id": file_id, "relative_path": path})


def remove_from_knowledge(
    cfg: storage.StorageConfig, user_id: str, path: str
) -> str:
    from . import rag_client

    try:
        ctx = _resolve_publish_ctx(cfg)
    except RuntimeError:
        return _err("rag_unavailable", "AppContext not configured")
    if not ctx.settings.jwt_secret_usable():
        return _err("rag_disabled", "JWT_SECRET not configured or too short")
    index = docindex.new_docindex(cfg, user_id)
    entry = index.get(path)
    if entry is None or not entry.rag_file_id:
        return _err("not_indexed", "document is not in the knowledge base")

    async def flow(client):
        await client.delete_document(user_id, entry.rag_file_id)

    try:
        _run_rag(ctx, flow)
    except rag_client.RagError as exc:
        return _err("rag_delete_failed", str(exc))
    index.set_indexing_status(path, "not_indexed", rag_file_id=None)
    return _ok({"removed": True, "relative_path": path, "rag_file_id": entry.rag_file_id})


def get_index_status(
    cfg: storage.StorageConfig, user_id: str
) -> str:
    from . import rag_client

    try:
        ctx = _resolve_publish_ctx(cfg)
    except RuntimeError:
        return _err("rag_unavailable", "AppContext not configured")
    if not ctx.settings.jwt_secret_usable():
        return _err("rag_disabled", "JWT_SECRET not configured or too short")
    docs = docindex.new_docindex(cfg, user_id).list_documents()
    total = len(docs)
    indexed = sum(1 for d in docs if d.indexing_status == "indexed")
    failed = sum(1 for d in docs if d.indexing_status == "indexing_failed")
    pending = total - indexed - failed

    async def flow(client):
        return await client.health()

    try:
        health = _run_rag(ctx, flow)
    except rag_client.RagError as exc:
        health = {"status": "unreachable", "error": str(exc)}
    return _ok(
        {
            "documents_total": total,
            "documents_indexed": indexed,
            "documents_pending": pending,
            "documents_failed": failed,
            "rag_health": health,
        }
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _maybe_register_doc(
    cfg: storage.StorageConfig, user_id: str, rel: str
) -> bool:
    if not rel.endswith((".md", ".txt")):
        return False
    if rel.startswith(f"{DOCUMENTATION_SUBDIR}/"):
        return False
    try:
        content = storage.read(cfg, user_id, rel).decode("utf-8", errors="replace")
    except storage.StorageError:
        return False
    title, description = _extract_title_description(content)
    entry = docindex.DocumentEntry(file=rel, title=title, description=description)
    try:
        docindex.new_docindex(cfg, user_id).upsert(entry)
        return True
    except storage.StorageError as exc:
        logger.warning("docindex upsert failed for %s: %s", rel, exc)
        return False


def _extract_title_description(content: str) -> tuple[str, str]:
    title = ""
    description = ""
    lines = content.splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        title = lines[0].lstrip()[2:].strip()
        for line in lines[1:]:
            if line.strip():
                description = line.strip()
                break
    return title, description
