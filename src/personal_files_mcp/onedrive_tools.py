"""OneDrive MCP tool wrappers."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from typing import Any

from fastmcp import Context

from . import docindex, identity, tools
from .onedrive_index import OneDriveDocIndex, new_index_document
from .onedrive_storage import OneDriveStorage


def register_onedrive_tools(mcp) -> None:
    """Register all OneDrive tools. Identity is always sourced from request headers."""

    @mcp.tool()
    def onedrive_login(ctx: Context) -> str:
        uid, app = _context(ctx)
        try:
            started = app.onedrive_auth.begin_authorization(uid)
            return _ok(
                {
                    "authorization_url": started.url,
                    "state": started.state,
                    "callback_url": app.settings.azure_redirect_uri,
                    "expires_in_seconds": 600,
                }
            )
        except Exception as exc:
            return _error("onedrive_login_failed", str(exc))

    @mcp.tool()
    def onedrive_auth_status(ctx: Context) -> str:
        uid, app = _context(ctx)
        status = app.onedrive_auth.status(uid)
        return _ok({
            "connected": status.connected,
            "expires_at": status.expires_at,
            "display_name": status.display_name,
            "email": status.email,
        })

    @mcp.tool()
    def onedrive_logout(ctx: Context) -> str:
        uid, app = _context(ctx)
        app.onedrive_auth.logout(uid)
        return _ok({"logged_out": True})

    @mcp.tool()
    def onedrive_list_files(
        ctx: Context, path: str = "", recursive: bool = False, pattern: str | None = None
    ) -> str:
        uid, app, provider = _provider(ctx)
        try:
            files = provider.list(path, recursive, pattern)
            return _ok({"path": path, "count": len(files), "files": files})
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_list_files_page(
        ctx: Context, path: str = "", page_token: str | None = None, page_size: int = 100
    ) -> str:
        _uid, _app, provider = _provider(ctx)
        try:
            return _ok(provider.list_page(path, page_token, page_size))
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_read_file(ctx: Context, path: str) -> str:
        _uid, _app, provider = _provider(ctx)
        try:
            data = provider.read(path)
            return _ok({
                "content": data.decode("utf-8"),
                "is_binary": False,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        except UnicodeDecodeError:
            return _error("onedrive_binary_file", "file is not UTF-8 text; use onedrive_read_binary_file")
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_read_multiple_files(ctx: Context, paths: list[str]) -> str:
        _uid, _app, provider = _provider(ctx)
        results = {}
        for path in paths:
            try:
                data = provider.read(path)
                results[path] = {"content": data.decode("utf-8"), "size_bytes": len(data)}
            except Exception as exc:
                results[path] = {"error": str(exc)}
        return _ok({"files": results})

    @mcp.tool()
    def onedrive_read_binary_file(ctx: Context, path: str) -> str:
        _uid, _app, provider = _provider(ctx)
        try:
            data = provider.read(path, max_bytes=50 * 1024 * 1024)
            return _ok({"content_base64": base64.b64encode(data).decode("ascii"), "size_bytes": len(data)})
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_write_file(ctx: Context, path: str, content: str) -> str:
        _uid, _app, provider = _provider(ctx)
        try:
            item = provider.write(path, content.encode("utf-8"))
            return _ok({"path": path, "item": item})
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_write_binary_file(ctx: Context, path: str, content_base64: str) -> str:
        _uid, _app, provider = _provider(ctx)
        try:
            data = base64.b64decode(content_base64, validate=True)
            item = provider.write(path, data)
            return _ok({"path": path, "item": item})
        except (ValueError, base64.binascii.Error):
            return _error("invalid_base64", "content_base64 must be valid base64")
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_resume_upload(ctx: Context, upload_id: str) -> str:
        _uid, _app, provider = _provider(ctx)
        try:
            result = provider.resume_upload(upload_id)
            return _ok(result)
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_update_file(ctx: Context, path: str, content: str) -> str:
        _uid, _app, provider = _provider(ctx)
        try:
            return _ok({"path": path, "item": provider.update(path, content.encode("utf-8"))})
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_edit_file(ctx: Context, path: str, edits: list[dict[str, str]]) -> str:
        _uid, _app, provider = _provider(ctx)
        try:
            return _ok({"path": path, "item": provider.edit(path, edits)})
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_delete_file(ctx: Context, path: str) -> str:
        _uid, _app, provider = _provider(ctx)
        try:
            provider.delete(path)
            return _ok({"deleted": path})
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_move_file(ctx: Context, src: str, dst: str) -> str:
        _uid, _app, provider = _provider(ctx)
        try:
            return _ok({"moved": provider.move(src, dst)})
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_get_file_info(ctx: Context, path: str) -> str:
        _uid, _app, provider = _provider(ctx)
        try:
            info = provider.stat(path)
            if not info["is_directory"]:
                entry = OneDriveDocIndex(provider).get(path)
                if entry:
                    info["documentation"] = entry.to_dict()
            return _ok(info)
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_save_documentation(
        ctx: Context, filename: str, content: str, title: str | None = None,
        description: str | None = None, tags: list[str] | None = None,
        topics: list[str] | None = None,
    ) -> str:
        uid, _app, provider, index = _provider_index(ctx)
        try:
            safe = tools._sanitize_doc_filename(filename)
            path = f"docs/{safe}"
            provider.write(path, content.encode("utf-8"))
            entry = new_index_document(
                file=path, title=title or safe.rsplit(".", 1)[0],
                description=description or "", tags=tags or [], topics=topics or [],
            )
            index.upsert(entry)
            return _ok({"saved": path, "indexed": False, "suggested_next_tool": "onedrive_index_document"})
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_update_documentation(
        ctx: Context, filename: str, content: str, title: str | None = None,
        description: str | None = None, tags: list[str] | None = None,
        topics: list[str] | None = None,
    ) -> str:
        uid, _app, provider, index = _provider_index(ctx)
        try:
            safe = tools._sanitize_doc_filename(filename)
            path = f"docs/{safe}"
            existing = index.get(path)
            if existing is None:
                return _error("onedrive_not_found", "documentation entry does not exist")
            provider.update(path, content.encode("utf-8"))
            entry = docindex.DocumentEntry(
                file=path,
                title=title if title is not None else existing.title,
                description=description if description is not None else existing.description,
                tags=tags if tags is not None else existing.tags,
                topics=topics if topics is not None else existing.topics,
                rag_file_id=existing.rag_file_id,
                indexing_status=existing.indexing_status,
            )
            index.upsert(entry)
            return _ok({"updated": path})
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_get_document_metadata(ctx: Context, filename: str) -> str:
        _uid, _app, _provider, index = _provider_index(ctx)
        try:
            path = f"docs/{tools._sanitize_doc_filename(filename)}"
            entry = index.get(path)
            return _ok(entry.to_dict()) if entry else _error("onedrive_not_found", "metadata not found")
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_index_document(ctx: Context, path: str) -> str:
        uid, app, provider, index = _provider_index(ctx)
        try:
            text = provider.read(path).decode("utf-8")
        except UnicodeDecodeError:
            return _error("onedrive_binary_file", "RAG accepts UTF-8 text files only")
        except Exception as exc:
            return _exception(exc)
        existing = index.get(path)
        file_id = existing.rag_file_id if existing and existing.rag_file_id else str(uuid.uuid4())
        async def index_with_canary(client):
            await client.embed(uid, file_id, path.rsplit("/", 1)[-1], text)
            return await client.canary(uid, file_id, text)

        try:
            ok, reason = _run_rag(app.settings, index_with_canary)
            index.set_indexing_status(path, "indexed" if ok else "indexing_failed", file_id)
            return _ok({"indexed": ok, "rag_file_id": file_id, "reason": reason})
        except Exception as exc:
            index.set_indexing_status(path, "indexing_failed", file_id)
            return _error("rag_index_failed", str(exc))

    @mcp.tool()
    def onedrive_remove_from_knowledge(ctx: Context, path: str) -> str:
        uid, app, _provider, index = _provider_index(ctx)
        entry = index.get(path)
        if not entry or not entry.rag_file_id:
            return _error("onedrive_not_indexed", "file is not indexed")
        try:
            _run_rag(app.settings, lambda client: client.delete_document(uid, entry.rag_file_id))
            index.set_indexing_status(path, "not_indexed")
            return _ok({"removed": True, "path": path})
        except Exception as exc:
            return _error("rag_remove_failed", str(exc))

    @mcp.tool()
    def onedrive_search_knowledge(ctx: Context, query: str, limit: int = 8) -> str:
        uid, app, _provider, index = _provider_index(ctx)
        entries = [
            entry for entry in index.list_documents()
            if entry.rag_file_id and entry.indexing_status == "indexed"
        ]
        file_ids = [entry.rag_file_id for entry in entries]
        if not file_ids:
            return _ok({"results": [], "count": 0})
        try:
            result = _run_rag(
                app.settings,
                lambda client: client.query_multiple(
                    uid, query, file_ids, min(max(limit, 1), 50)
                ),
            )
            return _ok({"results": result, "count": len(result)})
        except Exception as exc:
            return _error("rag_unavailable", str(exc))

    @mcp.tool()
    def onedrive_get_index_status(ctx: Context) -> str:
        _uid, _app, _provider, index = _provider_index(ctx)
        entries = index.list_documents()
        return _ok({
            "storage_provider": "onedrive",
            "documents": len(entries),
            "indexed": sum(entry.indexing_status == "indexed" for entry in entries),
            "failed": sum(entry.indexing_status == "indexing_failed" for entry in entries),
        })

    @mcp.tool()
    def onedrive_get_direct_link(ctx: Context, path: str) -> str:
        uid, app, provider = _provider(ctx)
        try:
            item = provider.resolve_item(path)
            token = app.onedrive_publishing.create_viewer_token(uid, item["id"])
            return _ok({
                "url": f"{app.settings.onedrive_viewer_base_url.rstrip('/')}/{token}",
                "expires_in_seconds": 300,
                "requires_authenticated_user": True,
            })
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_get_download_link(ctx: Context, path: str) -> str:
        """Return an authenticated temporary viewer URL for the requested file."""
        return onedrive_get_direct_link(ctx, path)

    @mcp.tool()
    def onedrive_publish_file(ctx: Context, path: str) -> str:
        uid, app, provider = _provider(ctx)
        try:
            item = provider.resolve_item(path)
            result = app.onedrive_publishing.publish_file(uid, item["id"])
            return _ok(result)
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_unpublish_file(ctx: Context, path: str) -> str:
        uid, app, provider = _provider(ctx)
        try:
            item = provider.resolve_item(path)
            return _ok({"revoked": app.onedrive_publishing.unpublish_file(uid, item["id"])})
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_get_public_link(ctx: Context, path: str) -> str:
        uid, app, provider = _provider(ctx)
        try:
            item = provider.resolve_item(path)
            link = app.onedrive_publishing.get_public_link(uid, item["id"])
            return _ok(link) if link else _error("onedrive_not_found", "no active organization link")
        except Exception as exc:
            return _exception(exc)

    @mcp.tool()
    def onedrive_list_public_links(ctx: Context) -> str:
        uid, app = _context(ctx)
        return _ok({"links": app.onedrive_publishing.list_public_links(uid)})


def _context(ctx: Context):
    request = ctx.request_context.request
    app = ctx.fastmcp._app_context
    try:
        uid = identity.extract_user_id(
            request.headers, identity.IdentityConfig(app.settings.user_header)
        )
    except identity.IdentityError as exc:
        raise PermissionError(exc.code) from None
    return uid, app


def _provider(ctx: Context):
    uid, app = _context(ctx)
    if not app.settings.onedrive_enabled:
        raise PermissionError("onedrive_disabled")
    provider = _user_provider(app, uid)
    return uid, app, provider


def _provider_index(ctx: Context):
    uid, app, provider = _provider(ctx)
    return uid, app, provider, OneDriveDocIndex(provider)


def _user_provider(app, user_id: str) -> OneDriveStorage:
    return app.onedrive_storage.for_user(user_id)


def _run_rag(settings, coro_factory):
    from concurrent.futures import ThreadPoolExecutor

    async def run():
        from .rag_client import RagClient
        client = RagClient(settings)
        try:
            return await coro_factory(client)
        finally:
            await client.close()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run())
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, run()).result()


def _ok(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _error(code: str, reason: str) -> str:
    return "ERROR: " + json.dumps({"error": code, "reason": reason}, ensure_ascii=False)


def _exception(exc: Exception) -> str:
    code = getattr(exc, "code", "onedrive_operation_failed")
    return _error(code, str(exc))
