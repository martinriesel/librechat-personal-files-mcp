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
import dataclasses
import datetime as _dt
import hashlib
import json
import logging
import uuid
from dataclasses import asdict
from typing import Any

from . import docindex, project_context, storage

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


def _is_protected_catalog_path(path: str) -> bool:
    """Return True if path points to a memory-index.json catalog or its lock."""
    cleaned = path.replace("\\", "/").strip("/")
    filename = cleaned.rsplit("/", 1)[-1]
    if filename in (
        docindex.INDEX_FILENAME,
        f".{docindex.INDEX_FILENAME}.lock",
        f"{docindex.INDEX_FILENAME}.lock",
    ):
        return True
    return filename.endswith("memory-index.json") or filename.endswith("memory-index.json.lock")


def _is_modified_since_indexing(
    cfg: storage.StorageConfig, user_id: str, entry: docindex.DocumentEntry
) -> bool:
    if not entry.rag_indexed or not entry.indexed_sha256:
        return False
    try:
        data = storage.read(cfg, user_id, entry.file)
        return _hash_bytes(data) != entry.indexed_sha256
    except Exception:
        return False


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
    if _is_protected_catalog_path(path):
        return _err(
            "protected_file",
            "Direct modification of memory-index.json is not allowed. Use documentation tools.",
        )
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
    if _is_protected_catalog_path(path):
        return _err(
            "protected_file",
            "Direct modification of memory-index.json is not allowed. Use documentation tools.",
        )
    if not isinstance(content, str):
        return _err("invalid_content", "content must be a UTF-8 string")
    try:
        info = storage.update(cfg, user_id, path, content.encode("utf-8"))
    except storage.StorageError as exc:
        return _err("update_failed", str(exc))
    doc_updated = _maybe_register_doc(cfg, user_id, path)
    return _ok({"file": asdict(info), "documentation_updated": doc_updated})


def delete_file(cfg: storage.StorageConfig, user_id: str, path: str) -> str:
    if _is_protected_catalog_path(path):
        return _err(
            "protected_file",
            "Direct modification of memory-index.json is not allowed. Use documentation tools.",
        )
    try:
        storage.delete(cfg, user_id, path)
    except storage.StorageError as exc:
        return _err("delete_failed", str(exc))
    with contextlib.suppress(Exception):
        entry, cat = docindex.find_document_across_catalogs(cfg, user_id, path)
        if entry and cat:
            cat.remove(entry.file)
    return _ok({"deleted": path})


def move_file(
    cfg: storage.StorageConfig, user_id: str, src: str, dst: str
) -> str:
    if _is_protected_catalog_path(src) or _is_protected_catalog_path(dst):
        return _err(
            "protected_file",
            "Direct modification of memory-index.json is not allowed. Use documentation tools.",
        )
    try:
        storage.move(cfg, user_id, src, dst)
    except storage.StorageError as exc:
        return _err("move_failed", str(exc))
    with contextlib.suppress(Exception):
        entry, src_cat = docindex.find_document_across_catalogs(cfg, user_id, src)
        if entry and src_cat:
            dst_cat = docindex.get_catalog_for_path(cfg, user_id, dst)
            src_cat.remove(entry.file)
            updated = dataclasses.replace(
                entry,
                file=dst,
                updated_at=docindex._utc_now_iso(),
            )
            dst_cat.upsert(updated)
    return _ok({"moved": {"src": src, "dst": dst}})


def get_file_info(cfg: storage.StorageConfig, user_id: str, path: str) -> str:
    try:
        info = storage.stat(cfg, user_id, path)
    except storage.StorageError as exc:
        return _err("not_found", str(exc))
    out: dict[str, Any] = asdict(info)
    if not info.is_directory and info.name.endswith((".md", ".txt")):
        try:
            entry, _ = docindex.find_document_across_catalogs(cfg, user_id, path)
            if entry is not None:
                doc_dict = entry.to_dict()
                doc_dict["modified_since_indexing"] = _is_modified_since_indexing(
                    cfg, user_id, entry
                )
                out["documentation"] = doc_dict
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
    if _is_protected_catalog_path(path):
        return _err(
            "protected_file",
            "Direct modification of memory-index.json is not allowed. Use documentation tools.",
        )
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
    if _is_protected_catalog_path(path):
        return _err(
            "protected_file",
            "Direct modification of memory-index.json is not allowed. Use documentation tools.",
        )
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
    project_id: str | None = None,
    project_name: str | None = None,
    conversation_id: str | None = None,
    resolver_status: str | None = None,
) -> str:
    if not isinstance(content, str):
        return _err("invalid_content", "content must be a UTF-8 string")
    if resolver_status in (
        "conversation_unavailable",
        "resolver_unavailable",
        "project_unavailable",
        "context_invalid",
    ):
        return _err(
            "project_resolution_failed",
            f"Project resolution status '{resolver_status}': destination scope cannot be determined safely.",
            resolver_status=resolver_status,
        )
    try:
        safe = _sanitize_doc_filename(filename)
    except storage.StorageError as exc:
        return _err("invalid_filename", str(exc))

    req_ctx = project_context.get_request_context()
    resolved_proj_id = project_id or (req_ctx.project_id if req_ctx else None)
    resolved_proj_name = project_name or (req_ctx.project_name if req_ctx else None)
    resolved_conv_id = conversation_id or (req_ctx.conversation_id if req_ctx else None)

    if resolved_proj_id:
        target_dir = f"projects/{resolved_proj_id}"
        cat = docindex.get_project_catalog(cfg, user_id, resolved_proj_id)
    else:
        target_dir = DOCUMENTATION_SUBDIR
        cat = docindex.get_general_catalog(cfg, user_id)

    rel = f"{target_dir}/{safe}"
    try:
        storage.write(cfg, user_id, rel, content.encode("utf-8"))
    except storage.StorageError as exc:
        return _err("write_failed", str(exc))

    existing = cat.get(rel)
    now = docindex._utc_now_iso()
    if existing:
        entry = dataclasses.replace(
            existing,
            title=title or existing.title or "",
            description=description or existing.description or "",
            tags=list(tags or existing.tags),
            topics=list(topics or existing.topics),
            project_id=resolved_proj_id,
            project_name=resolved_proj_name,
            conversation_id=resolved_conv_id,
            updated_at=now,
        )
    else:
        entry = docindex.DocumentEntry(
            file=rel,
            title=title or "",
            description=description or "",
            tags=list(tags or []),
            topics=list(topics or []),
            project_id=resolved_proj_id,
            project_name=resolved_proj_name,
            conversation_id=resolved_conv_id,
            created_at=now,
            updated_at=now,
        )
    cat.upsert(entry)
    info = storage.stat(cfg, user_id, rel)
    payload: dict[str, Any] = {
        "doc_id": entry.doc_id,
        "file": rel,
        "saved": True,
        "indexed": False,
        "published": False,
        "suggestion": "Call index_document to add to knowledge base",
        "stat": asdict(info),
    }
    if resolved_proj_id:
        payload["project_id"] = resolved_proj_id
    if resolved_proj_name:
        payload["project_name"] = resolved_proj_name
    return _ok(payload)


def update_documentation(
    cfg: storage.StorageConfig,
    user_id: str,
    filename: str,
    content: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    topics: list[str] | None = None,
    project_id: str | None = None,
    project_name: str | None = None,
    conversation_id: str | None = None,
) -> str:
    if not isinstance(content, str):
        return _err("invalid_content", "content must be a UTF-8 string")

    entry, cat = docindex.find_document_across_catalogs(cfg, user_id, filename)
    if entry is None or cat is None:
        return _err(
            "not_found",
            f"documentation file '{filename}' does not exist in any catalog",
        )

    try:
        storage.update(cfg, user_id, entry.file, content.encode("utf-8"))
    except storage.StorageError as exc:
        return _err("update_failed", str(exc))

    resolved_proj_id = project_id if project_id is not None else entry.project_id
    resolved_proj_name = project_name if project_name is not None else entry.project_name
    resolved_conv_id = conversation_id if conversation_id is not None else entry.conversation_id

    updated_entry = dataclasses.replace(
        entry,
        title=title if title is not None else entry.title,
        description=description if description is not None else entry.description,
        tags=list(tags) if tags is not None else entry.tags,
        topics=list(topics) if topics is not None else entry.topics,
        project_id=resolved_proj_id,
        project_name=resolved_proj_name,
        conversation_id=resolved_conv_id,
        updated_at=docindex._utc_now_iso(),
    )
    cat.upsert(updated_entry)
    payload: dict[str, Any] = {
        "doc_id": updated_entry.doc_id,
        "file": updated_entry.file,
        "updated": True,
        "suggestion": "Call index_document to refresh knowledge base",
    }
    if resolved_proj_id:
        payload["project_id"] = resolved_proj_id
    if resolved_proj_name:
        payload["project_name"] = resolved_proj_name
    return _ok(payload)


def list_documentation(
    cfg: storage.StorageConfig,
    user_id: str,
    project_id: str | None = None,
    tag: str | None = None,
    unassigned_only: bool = False,
) -> str:
    """List documentation catalog entries for the user, with optional filtering."""
    docs = docindex.list_all_documents(
        cfg, user_id, project_id=project_id, unassigned_only=unassigned_only
    )
    if tag:
        tag_lower = tag.lower()
        docs = [d for d in docs if any(t.lower() == tag_lower for t in d.tags)]

    doc_dicts = []
    for d in docs:
        item = d.to_dict()
        item["modified_since_indexing"] = _is_modified_since_indexing(cfg, user_id, d)
        doc_dicts.append(item)

    return _ok(
        {
            "count": len(doc_dicts),
            "project_id": project_id,
            "tag": tag,
            "documents": doc_dicts,
        }
    )


def get_document_metadata(
    cfg: storage.StorageConfig, user_id: str, filename: str
) -> str:
    entry, _cat = docindex.find_document_across_catalogs(cfg, user_id, filename)
    if entry is None:
        return _err(
            "not_found",
            f"documentation entry '{filename}' not found in any catalog",
        )
    out = entry.to_dict()
    out["modified_since_indexing"] = _is_modified_since_indexing(cfg, user_id, entry)
    return _ok(out)


def register_documentation(
    cfg: storage.StorageConfig,
    user_id: str,
    path: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    topics: list[str] | None = None,
    project_id: str | None = None,
    project_name: str | None = None,
) -> str:
    """Register an existing markdown or text file into the catalog without rewriting."""
    if not path.endswith((".md", ".txt")):
        return _err(
            "invalid_type",
            "only markdown (.md) or text (.txt) files can be registered as documentation",
        )
    try:
        info = storage.stat(cfg, user_id, path)
    except storage.StorageError as exc:
        return _err("not_found", str(exc))
    if info.is_directory:
        return _err("is_directory", "path is a directory")

    try:
        content_bytes = storage.read(cfg, user_id, path)
        text = content_bytes.decode("utf-8", errors="replace")
    except storage.StorageError as exc:
        return _err("read_failed", str(exc))

    auto_title, auto_desc = _extract_title_description(text)
    final_title = title if title is not None else (auto_title or info.name)
    final_desc = description if description is not None else auto_desc

    cat = docindex.get_catalog_for_path(cfg, user_id, path)
    existing = cat.get(path)

    req_ctx = project_context.get_request_context()
    resolved_proj_id = project_id or (req_ctx.project_id if req_ctx else None)
    resolved_proj_name = project_name or (req_ctx.project_name if req_ctx else None)

    if not resolved_proj_id and path.startswith("projects/"):
        parts = path.split("/")
        if len(parts) >= 2 and parts[1]:
            resolved_proj_id = parts[1]

    now = docindex._utc_now_iso()
    if existing:
        entry = dataclasses.replace(
            existing,
            title=final_title,
            description=final_desc,
            tags=list(tags) if tags is not None else existing.tags,
            topics=list(topics) if topics is not None else existing.topics,
            project_id=resolved_proj_id or existing.project_id,
            project_name=resolved_proj_name or existing.project_name,
            updated_at=now,
        )
    else:
        entry = docindex.DocumentEntry(
            file=path,
            title=final_title,
            description=final_desc,
            tags=list(tags or []),
            topics=list(topics or []),
            project_id=resolved_proj_id,
            project_name=resolved_proj_name,
            created_at=now,
            updated_at=now,
        )
    cat.upsert(entry)
    payload: dict[str, Any] = {
        "registered": True,
        "doc_id": entry.doc_id,
        "file": path,
        "title": entry.title,
        "project_id": entry.project_id,
        "project_name": entry.project_name,
        "suggestion": "Call index_document to add to knowledge base",
    }
    return _ok(payload)


def transfer_documentation(
    cfg: storage.StorageConfig,
    user_id: str,
    identifier: str,
    target_project_id: str | None = None,
    target_filename: str | None = None,
    dry_run: bool = False,
) -> str:
    """Transfer documentation file and index record between scopes.

    Preserves metadata, public links, and RAG status. Detects collisions and
    supports dry_run verification.
    """
    entry, src_cat = docindex.find_document_across_catalogs(cfg, user_id, identifier)
    if entry is None or src_cat is None:
        return _err("not_found", f"document '{identifier}' not found in any catalog")

    src_path = entry.file
    base_fn = target_filename or src_path.rsplit("/", 1)[-1]
    try:
        safe_fn = _sanitize_doc_filename(base_fn)
    except storage.StorageError as exc:
        return _err("invalid_filename", str(exc))

    if target_project_id:
        dst_dir = f"projects/{target_project_id}"
        dst_cat = docindex.get_project_catalog(cfg, user_id, target_project_id)
    else:
        dst_dir = DOCUMENTATION_SUBDIR
        dst_cat = docindex.get_general_catalog(cfg, user_id)

    dst_path = f"{dst_dir}/{safe_fn}"

    if src_path == dst_path:
        return _err(
            "same_destination",
            f"source and destination are identical: {src_path}",
        )

    # Collision detection: file on disk
    try:
        storage.stat(cfg, user_id, dst_path)
        return _err(
            "collision_detected",
            f"destination file already exists: {dst_path}",
        )
    except storage.StorageError:
        pass

    # Collision detection: entry in destination catalog
    if dst_cat.get(dst_path) is not None:
        return _err(
            "collision_detected",
            f"destination catalog already contains an entry for: {dst_path}",
        )

    if dry_run:
        return _ok(
            {
                "dry_run": True,
                "doc_id": entry.doc_id,
                "source_file": src_path,
                "target_file": dst_path,
                "source_project_id": entry.project_id,
                "target_project_id": target_project_id,
                "can_transfer": True,
                "public_link": entry.public,
                "rag_indexed": entry.rag_indexed,
            }
        )

    # Perform physical move
    try:
        storage.move(cfg, user_id, src_path, dst_path)
    except storage.StorageError as exc:
        return _err("move_failed", str(exc))

    # Update public link in publishing registry if present
    with contextlib.suppress(Exception):
        ctx = _resolve_publish_ctx(cfg)
        from . import publishing

        publishing.update_link_path(ctx, user_id, src_path, dst_path)

    # Update catalogs
    src_cat.remove(src_path)
    updated_entry = dataclasses.replace(
        entry,
        file=dst_path,
        project_id=target_project_id,
        project_name=None,
        updated_at=docindex._utc_now_iso(),
    )
    dst_cat.upsert(updated_entry)

    return _ok(
        {
            "transferred": True,
            "doc_id": updated_entry.doc_id,
            "source_file": src_path,
            "target_file": dst_path,
            "source_project_id": entry.project_id,
            "target_project_id": target_project_id,
            "public_link_preserved": updated_entry.public,
            "rag_preserved": updated_entry.rag_indexed,
        }
    )


def reconcile_documentation(
    cfg: storage.StorageConfig,
    user_id: str,
    project_id: str | None = None,
    repair: bool = False,
) -> str:
    """Detect unregistered files, orphaned catalog entries, and modified files.

    Repairs only when repair=True.
    """
    user_root = storage.resolve(cfg, user_id)
    if project_id:
        scan_catalogs = [docindex.get_project_catalog(cfg, user_id, project_id)]
        disk_dirs = [user_root / "projects" / project_id]
    else:
        scan_catalogs = docindex.get_all_catalogs(cfg, user_id)
        disk_dirs = [user_root / DOCUMENTATION_SUBDIR]
        p_dir = user_root / "projects"
        if p_dir.is_dir():
            for d in p_dir.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    disk_dirs.append(d)

    # 1. Unregistered files
    unregistered: list[str] = []
    for d in disk_dirs:
        if d.is_dir():
            for p in d.glob("*.md"):
                rel = str(p.relative_to(user_root)).replace("\\", "/")
                entry, _ = docindex.find_document_across_catalogs(cfg, user_id, rel)
                if entry is None:
                    unregistered.append(rel)
            for p in d.glob("*.txt"):
                rel = str(p.relative_to(user_root)).replace("\\", "/")
                entry, _ = docindex.find_document_across_catalogs(cfg, user_id, rel)
                if entry is None:
                    unregistered.append(rel)

    # 2. Orphaned catalog entries
    orphaned: list[dict[str, Any]] = []
    for cat in scan_catalogs:
        for doc in cat.list_documents():
            fpath = user_root / doc.file
            if not fpath.exists():
                cat_rel = (
                    str(cat.path.relative_to(user_root))
                    if cat.path.is_relative_to(user_root)
                    else cat.path.name
                )
                orphaned.append(
                    {
                        "doc_id": doc.doc_id,
                        "file": doc.file,
                        "catalog": cat_rel,
                    }
                )

    # 3. Modified files after indexing
    modified: list[dict[str, Any]] = []
    for cat in scan_catalogs:
        for doc in cat.list_documents():
            if _is_modified_since_indexing(cfg, user_id, doc):
                modified.append(
                    {
                        "doc_id": doc.doc_id,
                        "file": doc.file,
                        "indexed_sha256": doc.indexed_sha256,
                    }
                )

    # Execute repair if requested
    repaired_unregistered = 0
    repaired_orphans = 0
    if repair:
        for rel in unregistered:
            cat = docindex.get_catalog_for_path(cfg, user_id, rel)
            try:
                content = storage.read(cfg, user_id, rel).decode(
                    "utf-8", errors="replace"
                )
                auto_t, auto_d = _extract_title_description(content)
                e = docindex.DocumentEntry(
                    file=rel,
                    title=auto_t or rel.rsplit("/", 1)[-1],
                    description=auto_d,
                )
                cat.upsert(e)
                repaired_unregistered += 1
            except Exception as exc:
                logger.warning("failed to repair unregistered file %s: %s", rel, exc)

        for o in orphaned:
            for cat in scan_catalogs:
                if cat.remove(o["file"]):
                    repaired_orphans += 1
                    break

    return _ok(
        {
            "reconciled": True,
            "repair_mode": repair,
            "unregistered_files": unregistered,
            "unregistered_count": len(unregistered),
            "repaired_unregistered": repaired_unregistered,
            "orphaned_entries": orphaned,
            "orphaned_count": len(orphaned),
            "repaired_orphans": repaired_orphans,
            "modified_since_indexing": modified,
            "modified_count": len(modified),
        }
    )


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
    project_id: str | None = None,
    scope: str = "auto",
) -> str:
    from . import rag_client

    if not isinstance(query, str) or not query.strip():
        return _err("invalid_query", "query must be a non-empty string")
    if not isinstance(limit, int) or limit < 1 or limit > 50:
        return _err("invalid_limit", "limit must be 1..50")
    if scope not in ("auto", "project", "personal", "all"):
        return _err("invalid_scope", f"unknown scope: {scope!r}")
    try:
        ctx = _resolve_publish_ctx(cfg)
    except RuntimeError:
        return _err("rag_unavailable", "AppContext not configured")
    if not ctx.settings.jwt_secret_usable():
        return _err("rag_disabled", "JWT_SECRET not configured or too short")

    req_ctx = project_context.get_request_context()
    resolved_proj_id = project_id or (req_ctx.project_id if req_ctx else None)

    effective_scope = scope
    if effective_scope == "auto":
        effective_scope = "project" if resolved_proj_id else "all"

    if effective_scope == "project":
        if not resolved_proj_id:
            return _err(
                "missing_project",
                "project scope requires project_id or active project context",
            )
        docs = docindex.list_all_documents(
            cfg, user_id, project_id=resolved_proj_id
        )
    elif effective_scope == "personal":
        docs = docindex.list_all_documents(cfg, user_id, unassigned_only=True)
    else:  # "all"
        docs = docindex.list_all_documents(cfg, user_id)

    file_ids = [
        d.rag_file_id
        for d in docs
        if d.rag_file_id and d.indexing_status == "indexed"
    ]
    if not file_ids:
        note = (
            f"no indexed documents for project '{resolved_proj_id}'"
            if effective_scope == "project"
            else "no indexed documents"
        )
        return _ok(
            {
                "query": query,
                "limit": limit,
                "scope": effective_scope,
                "project_id": resolved_proj_id if effective_scope == "project" else None,
                "count": 0,
                "results": [],
                "note": note,
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
            "scope": effective_scope,
            "project_id": resolved_proj_id if effective_scope == "project" else None,
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

    entry, cat = docindex.find_document_across_catalogs(cfg, user_id, path)
    target_path = entry.file if entry else path
    if cat is None:
        cat = docindex.get_catalog_for_path(cfg, user_id, target_path)

    try:
        content = storage.read(cfg, user_id, target_path)
    except storage.StorageError as exc:
        return _err("not_found", str(exc))
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _err("not_text", "file is not UTF-8 decodable")

    file_id = str(uuid.uuid4())
    sha256 = _hash_bytes(content)
    filename = target_path.rsplit("/", 1)[-1]

    if entry is None:
        auto_title, auto_desc = _extract_title_description(text)
        entry = docindex.DocumentEntry(
            file=target_path,
            title=auto_title or filename,
            description=auto_desc,
        )
        cat.upsert(entry)

    async def flow(client):
        await client.embed(user_id, file_id, filename, text)
        return await client.canary(user_id, file_id, text)

    try:
        ok, reason = _run_rag(ctx, flow)
    except rag_client.RagError as exc:
        cat.set_indexing_status(entry.file, "indexing_failed", rag_file_id=file_id)
        return _err("rag_embed_failed", str(exc))
    if not ok:
        cat.set_indexing_status(entry.file, "indexing_failed", rag_file_id=file_id)

        async def cleanup(client):
            await client.delete_document(user_id, file_id)

        with contextlib.suppress(rag_client.RagError):
            _run_rag(ctx, cleanup)
        return _err("rag_canary_failed", reason or "canary check failed")
    cat.set_indexing_status(
        entry.file, "indexed", rag_file_id=file_id, content_sha256=sha256
    )
    return _ok(
        {
            "indexed": True,
            "doc_id": entry.doc_id,
            "rag_file_id": file_id,
            "relative_path": entry.file,
        }
    )


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
    entry, cat = docindex.find_document_across_catalogs(cfg, user_id, path)
    if entry is None or not entry.rag_file_id or cat is None:
        return _err("not_indexed", "document is not in the knowledge base")

    async def flow(client):
        await client.delete_document(user_id, entry.rag_file_id)

    try:
        _run_rag(ctx, flow)
    except rag_client.RagError as exc:
        return _err("rag_delete_failed", str(exc))
    cat.set_indexing_status(entry.file, "not_indexed", rag_file_id=None)
    return _ok(
        {
            "removed": True,
            "doc_id": entry.doc_id,
            "relative_path": entry.file,
            "rag_file_id": entry.rag_file_id,
        }
    )


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
    docs = docindex.list_all_documents(cfg, user_id)
    total = len(docs)
    indexed = sum(1 for d in docs if d.indexing_status == "indexed")
    failed = sum(1 for d in docs if d.indexing_status == "indexing_failed")
    pending = total - indexed - failed
    modified = sum(1 for d in docs if _is_modified_since_indexing(cfg, user_id, d))

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
            "documents_modified_since_indexing": modified,
            "rag_health": health,
        }
    )


def index_batch(
    cfg: storage.StorageConfig,
    user_id: str,
    doc_ids: list[str] | None = None,
    project_id: str | None = None,
    only_pending: bool = False,
    only_modified: bool = False,
) -> str:
    """Batch index multiple documentation files, pending files, or modified files."""
    from . import rag_client

    try:
        ctx = _resolve_publish_ctx(cfg)
    except RuntimeError:
        return _err("rag_unavailable", "AppContext not configured")
    if not ctx.settings.jwt_secret_usable():
        return _err("rag_disabled", "JWT_SECRET not configured or too short")

    candidates: list[tuple[docindex.DocumentEntry, docindex.DocIndex]] = []
    if doc_ids:
        for did in doc_ids:
            entry, cat = docindex.find_document_across_catalogs(cfg, user_id, did)
            if entry and cat:
                candidates.append((entry, cat))
    else:
        all_docs = docindex.list_all_documents(cfg, user_id, project_id=project_id)
        for d in all_docs:
            is_mod = _is_modified_since_indexing(cfg, user_id, d)
            is_pend = d.indexing_status != "indexed"
            if only_modified and not is_mod:
                continue
            if only_pending and not is_pend:
                continue
            if not only_modified and not only_pending and not (is_pend or is_mod):
                continue
            _, cat = docindex.find_document_across_catalogs(cfg, user_id, d.doc_id)
            if cat:
                candidates.append((d, cat))

    results: list[dict[str, Any]] = []
    successful_count = 0
    failed_count = 0

    for entry, cat in candidates:
        try:
            content = storage.read(cfg, user_id, entry.file)
            text = content.decode("utf-8")
        except Exception as exc:
            results.append(
                {
                    "doc_id": entry.doc_id,
                    "file": entry.file,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            failed_count += 1
            continue

        file_id = str(uuid.uuid4())
        sha256 = _hash_bytes(content)
        filename = entry.file.rsplit("/", 1)[-1]

        def make_flow(fid: str, fn: str, txt: str):
            async def flow(client):
                await client.embed(user_id, fid, fn, txt)
                return await client.canary(user_id, fid, txt)

            return flow

        def make_cleanup(fid: str):
            async def cleanup(client):
                await client.delete_document(user_id, fid)

            return cleanup

        try:
            ok, reason = _run_rag(ctx, make_flow(file_id, filename, text))
        except rag_client.RagError as exc:
            cat.set_indexing_status(entry.file, "indexing_failed", rag_file_id=file_id)
            results.append(
                {
                    "doc_id": entry.doc_id,
                    "file": entry.file,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            failed_count += 1
            continue

        if not ok:
            cat.set_indexing_status(entry.file, "indexing_failed", rag_file_id=file_id)

            with contextlib.suppress(rag_client.RagError):
                _run_rag(ctx, make_cleanup(file_id))
            results.append(
                {
                    "doc_id": entry.doc_id,
                    "file": entry.file,
                    "status": "failed",
                    "error": reason or "canary check failed",
                }
            )
            failed_count += 1
        else:
            cat.set_indexing_status(
                entry.file, "indexed", rag_file_id=file_id, content_sha256=sha256
            )
            results.append(
                {
                    "doc_id": entry.doc_id,
                    "file": entry.file,
                    "status": "indexed",
                    "rag_file_id": file_id,
                }
            )
            successful_count += 1

    return _ok(
        {
            "total_candidates": len(candidates),
            "successful_count": successful_count,
            "failed_count": failed_count,
            "results": results,
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
    if rel.startswith(f"{DOCUMENTATION_SUBDIR}/") or rel.startswith("projects/"):
        return False
    try:
        content = storage.read(cfg, user_id, rel).decode("utf-8", errors="replace")
    except storage.StorageError:
        return False
    title, description = _extract_title_description(content)
    req_ctx = project_context.get_request_context()
    proj_id = req_ctx.project_id if req_ctx else None
    proj_name = req_ctx.project_name if req_ctx else None
    conv_id = req_ctx.conversation_id if req_ctx else None

    entry = docindex.DocumentEntry(
        file=rel,
        title=title,
        description=description,
        project_id=proj_id,
        project_name=proj_name,
        conversation_id=conv_id,
    )
    try:
        cat = docindex.get_catalog_for_path(cfg, user_id, rel)
        cat.upsert(entry)
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


# ---------------------------------------------------------------------------
# Project context & projects
# ---------------------------------------------------------------------------


async def list_projects(app_ctx: Any, user_id: str) -> str:
    """List all authorized projects for user with document counts."""
    projects_map: dict[str, dict[str, Any]] = {}

    resolver = getattr(app_ctx, "project_resolver", None)
    if resolver:
        mongo_projs = await resolver.list_user_projects(app_ctx.settings, user_id)
        for p in mongo_projs:
            pid = p["id"]
            projects_map[pid] = {
                "id": pid,
                "name": p.get("name", ""),
                "description": p.get("description", ""),
                "source": "librechat",
            }

    user_root = storage.resolve(app_ctx.storage_cfg, user_id)
    projects_dir = user_root / "projects"
    if projects_dir.is_dir():
        for p in projects_dir.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                pid = p.name
                if pid not in projects_map:
                    projects_map[pid] = {
                        "id": pid,
                        "name": pid,
                        "description": "",
                        "source": "storage",
                    }

    for pid, pdata in projects_map.items():
        p_cat = docindex.get_project_catalog(app_ctx.storage_cfg, user_id, pid)
        p_docs = p_cat.list_documents()
        pdata["documents_count"] = len(p_docs)
        pdata["indexed_count"] = sum(
            1 for d in p_docs if d.indexing_status == "indexed"
        )

    projs_list = list(projects_map.values())
    return _ok(
        {
            "count": len(projs_list),
            "projects": projs_list,
        }
    )


async def get_project_context(
    app_ctx: Any,
    user_id: str,
    conversation_id: str | None = None,
    conv_status: str = "valid",
    request_id: str = "",
) -> str:
    """Resolve and return active LibreChat project context."""
    resolver = getattr(app_ctx, "project_resolver", None)
    if resolver is None:
        return _ok(
            {
                "status": "resolver_unavailable",
                "message": "Project resolver is not configured on this server.",
            }
        )

    res = await resolver.resolve(
        app_ctx.settings,
        user_id=user_id,
        conversation_id=conversation_id,
        conv_status=conv_status,
        request_id=request_id,
    )
    res_dict = res.as_dict(conversation_id)
    if res.status == "project" and res.project_id:
        proj_docs = docindex.list_all_documents(
            app_ctx.storage_cfg, user_id, project_id=res.project_id
        )
        res_dict["project"]["documents_count"] = len(proj_docs)
        doc_list = []
        summary_file = None
        for d in proj_docs:
            doc_list.append(
                {
                    "file": d.file,
                    "title": d.title,
                    "description": d.description,
                    "tags": d.tags,
                    "rag_indexed": d.rag_indexed,
                    "public": d.public,
                }
            )
            if not summary_file and (
                d.file.endswith(("resumo.md", "summary.md"))
                or "resumo" in [t.lower() for t in d.tags]
                or "summary" in [t.lower() for t in d.tags]
            ):
                summary_file = d.file
        res_dict["project"]["documents"] = doc_list
        if summary_file:
            res_dict["project"]["summary_file"] = summary_file

    return _ok(res_dict)

