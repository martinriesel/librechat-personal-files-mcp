"""Personal Files MCP server entry point.

Wires together:
    * ``IdentityMiddleware`` (validates ``X-User-Id`` per request).
    * FastMCP server mounted at ``/mcp`` in stateless HTTP mode.
    * Public file endpoint at ``GET/HEAD /files/{token}`` (delegated to
      ``publishing.public_file_handler``; the route is registered here so the
      module is optional in tests).
    * Health endpoint at ``GET /healthz`` (bypasses identity).

The MCP tools are registered in ``register_tools``. They share a single
``AppContext`` (config + RAG client + publishing registry) that is built once
during FastAPI lifespan and exposed to tools via ``app.state``.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastmcp import Context, FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from . import identity, publishing, storage, tools
from .config import AppSettings
from .runtime import AppContext

logger = logging.getLogger(__name__)


def build_context() -> AppContext:
    settings = AppSettings.from_env()
    storage_cfg = storage.StorageConfig(
        storage_root=Path(settings.storage_root),
        max_file_bytes=settings.max_file_size_mb * 1024 * 1024,
        max_binary_bytes=settings.max_binary_file_mb * 1024 * 1024,
    )
    ctx = AppContext(settings=settings, storage_cfg=storage_cfg)
    if settings.onedrive_enabled:
        _build_onedrive_services(ctx)
    return ctx


def create_app(context: AppContext | None = None) -> FastAPI:
    ctx = context or build_context()

    # Build the MCP sub-app first so we can forward its lifespan to FastAPI.
    # ``path="/mcp"`` puts the MCP route at ``/mcp`` inside the sub-app; mounting
    # the sub-app at ``/`` makes it reachable at ``/mcp`` with NO trailing-slash
    # redirect (which would otherwise break the LibreChat streamable-http client).
    mcp = FastMCP("personal-files")
    register_mcp_tools(mcp)
    mcp_app = mcp.http_app(path="/mcp", stateless_http=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Ensure storage root exists; never fail closed here, log instead.
        Path(ctx.settings.storage_root).mkdir(parents=True, exist_ok=True)
        # Bind the AppContext so publishing/RAG tools can resolve it.
        tools.bind_publish_ctx(ctx)
        # Initialize publishing registry eagerly so SQLite is ready.
        publishing.get_registry(ctx)
        if ctx.settings.onedrive_enabled and ctx.onedrive_publishing is None:
            _build_onedrive_services(ctx)
        # Attach AppContext to the FastMCP instance for tool access.
        mcp._app_context = ctx
        if ctx.settings.onedrive_enabled:
            from .onedrive_tools import register_onedrive_tools

            register_onedrive_tools(mcp)
        app.state.app_context = ctx
        # Run the MCP sub-app lifespan (required for StreamableHTTPSessionManager).
        async with mcp_app.lifespan(app):
            yield

    app = FastAPI(
        title="librechat-personal-files-mcp",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.app_context = ctx
    app.add_middleware(IdentityMiddleware, header_name=ctx.settings.user_header)

    # Register user-facing routes BEFORE the catch-all MCP mount so they take
    # precedence.
    register_health(app)
    register_public_endpoint(app)
    register_binary_endpoint(app)
    if ctx.settings.onedrive_enabled:
        register_onedrive_auth_routes(app)
        register_onedrive_view_route(app)
    app.mount("/", mcp_app)

    return app


# ---------------------------------------------------------------------------
# Identity middleware
# ---------------------------------------------------------------------------


class IdentityMiddleware(BaseHTTPMiddleware):
    """Validate the configured identity header on every request that needs it.

    Public endpoints (``/healthz``, ``/files/*``) are excluded because they
    either are not user-scoped (health) or carry their own token-based
    authorization (public file download).
    """

    def __init__(self, app, header_name: str = "X-User-Id") -> None:
        super().__init__(app)
        self._config = identity.IdentityConfig(header_name=header_name)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            path == "/healthz"
            or path.startswith("/files")
            or path == "/auth/azure/callback"
        ):
            return await call_next(request)
        try:
            user_id = identity.extract_user_id(request.headers, self._config)
        except identity.IdentityError as exc:
            return JSONResponse(
                status_code=403,
                content={"error": exc.code},
            )
        request.state.user_id = user_id
        return await call_next(request)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register_health(app: FastAPI) -> None:
    from .health import healthz_handler

    @app.get("/healthz")
    async def _healthz() -> dict:
        ctx: AppContext = app.state.app_context
        return await healthz_handler(ctx)


def register_public_endpoint(app: FastAPI) -> None:
    """Mount GET/HEAD /files/{token} via publishing.py (lazy import)."""
    try:
        from . import publishing
    except ImportError:  # pragma: no cover
        publishing = None  # type: ignore

    if publishing is None:
        @app.get("/files/{token}")
        async def _placeholder(token: str):  # pragma: no cover
            return {"error": "publishing_not_initialized"}

        return

    @app.get("/files/{token}")
    async def _get(token: str, request: Request):
        ctx: AppContext = app.state.app_context
        return await publishing.handle_get(ctx, token, request)

    @app.head("/files/{token}")
    async def _head(token: str, request: Request):
        ctx: AppContext = app.state.app_context
        return await publishing.handle_head(ctx, token, request)

    @app.api_route(
        "/files/{token}",
        methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def _method_not_allowed(token: str) -> JSONResponse:
        return JSONResponse(
            status_code=405,
            content={"error": "method_not_allowed"},
            headers={"Allow": "GET, HEAD"},
        )


def register_binary_endpoint(app: FastAPI) -> None:
    """Mount ``POST /binary`` for service-to-service binary ingestion.

    Authentication is two-layered:
        1. ``IdentityMiddleware`` validates ``X-User-Id`` (fail-closed) and
           stores it in ``request.state.user_id``.
        2. The handler checks ``X-Service-Token`` against ``SERVICE_TOKEN`` so
           only known services (e.g. the office-documents fork) can write on
           behalf of a user. Without a configured token the endpoint refuses
           (503).
    """

    @app.post("/binary")
    async def _binary_upload(
        request: Request,
        file: UploadFile = File(...),  # noqa: B008 - FastAPI dependency marker
        path: str = Form(...),  # noqa: B008 - FastAPI dependency marker
    ) -> JSONResponse:
        ctx: AppContext = app.state.app_context

        if not ctx.settings.service_token_usable():
            return JSONResponse(
                status_code=503,
                content={"error": "service_token_not_configured"},
            )
        if not _service_token_matches(request, ctx.settings.service_token):
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_service_token"},
            )

        user_id: str = request.state.user_id

        # Enforce the binary size limit BEFORE buffering the whole payload, so a
        # client cannot exhaust memory by streaming an arbitrarily large file.
        limit = ctx.storage_cfg.max_binary_bytes or storage.MAX_BINARY_BYTES
        declared = _content_length(request)
        if declared is not None and declared > limit:
            return JSONResponse(
                status_code=413,
                content={"error": "file_too_large"},
            )

        data = await file.read(limit + 1)
        if not data:
            return JSONResponse(
                status_code=400,
                content={"error": "empty_file"},
            )
        if len(data) > limit:
            return JSONResponse(
                status_code=413,
                content={"error": "file_too_large"},
            )
        try:
            info = storage.write(
                ctx.storage_cfg,
                user_id,
                path,
                data,
                max_bytes=limit,
            )
        except storage.StorageError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

        import hashlib

        return JSONResponse(
            {
                "path": info.relative_path,
                "filename": info.name,
                "mime_type": info.mime_type,
                "size_bytes": info.size_bytes,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _service_token_matches(request: Request, expected: str) -> bool:
    import secrets

    supplied = request.headers.get("X-Service-Token", "")
    return secrets.compare_digest(supplied, expected)


def _build_onedrive_services(ctx: AppContext) -> None:
    from .onedrive_auth import OneDriveAuth
    from .onedrive_client import GraphClient
    from .onedrive_publishing import OneDrivePublishing
    from .onedrive_storage import OneDriveStorage

    settings = ctx.settings
    Path(settings.storage_root, "_system").mkdir(parents=True, exist_ok=True)
    transaction_db = (
        Path(settings.onedrive_oauth_db)
        if settings.onedrive_oauth_db
        else Path(settings.storage_root) / "_system" / "onedrive-oauth.db"
    )
    auth = OneDriveAuth(
        settings,
        transaction_db,
    )
    graph = GraphClient(auth, "")
    ctx.onedrive_auth = auth
    ctx.onedrive_storage = OneDriveStorage(
        graph, settings.onedrive_max_file_size_mb * 1024 * 1024
    )
    ctx.onedrive_publishing = OneDrivePublishing(
        Path(settings.onedrive_publishing_db)
        if settings.onedrive_publishing_db
        else Path(settings.storage_root) / "_system" / "onedrive-publishing.db",
        graph,
    )


def register_onedrive_auth_routes(app: FastAPI) -> None:
    """Register browser OAuth endpoints; callback identity comes from one-use state."""

    @app.get("/auth/azure/login")
    async def _azure_login(request: Request, state: str):
        ctx: AppContext = app.state.app_context
        try:
            user_id = _get_request_user_id(request, ctx.settings.user_header)
            started = ctx.onedrive_auth.authorization_for_state(user_id, state)
        except identity.IdentityError as exc:
            return JSONResponse(status_code=403, content={"error": exc.code})
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "invalid_oauth_state"})
        return RedirectResponse(started.url, status_code=302)

    @app.get("/auth/azure/callback")
    async def _azure_callback(code: str | None = None, state: str | None = None, error: str | None = None):
        ctx: AppContext = app.state.app_context
        if error or not code or not state:
            return HTMLResponse("<p>OneDrive authorization was not completed.</p>", status_code=400)
        try:
            ctx.onedrive_auth.handle_callback(code, state)
        except (ValueError, PermissionError) as exc:
            return HTMLResponse(f"<p>{_html_text(exc)}</p>", status_code=400)
        return HTMLResponse(
            "<p>OneDrive connected. Return to LibreChat.</p>",
            headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"},
        )


def register_onedrive_view_route(app: FastAPI) -> None:
    @app.get("/onedrive/view/{token}")
    async def _view_onedrive_file(token: str, request: Request):
        ctx: AppContext = app.state.app_context
        try:
            user_id = _get_request_user_id(request, ctx.settings.user_header)
            viewer = ctx.onedrive_publishing.viewer_item(token, user_id)
        except identity.IdentityError:
            return JSONResponse(status_code=403, content={"error": "missing_user_identity"})
        except Exception:
            return JSONResponse(status_code=404, content={"error": "viewer_not_found_or_expired"})
        async def body_stream():
            import asyncio
            from concurrent.futures import ThreadPoolExecutor

            def fetch():
                return ctx.onedrive_storage.client.download(
                    f"/me/drive/items/{viewer['item_id']}/content",
                    max_bytes=5 * 1024 * 1024,
                    user_id=user_id,
                )

            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=1) as executor:
                yield await loop.run_in_executor(executor, fetch)

        return StreamingResponse(
            body_stream(),
            media_type=viewer["mime_type"],
            headers={
                "Content-Disposition": "inline",
                "X-Frame-Options": "SAMEORIGIN",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "default-src 'self' data: https:; object-src 'none'; frame-ancestors 'self'",
            },
        )


def _get_request_user_id(request: Request, header_name: str) -> str:
    return identity.extract_user_id(request.headers, identity.IdentityConfig(header_name))


def _html_text(error: Exception) -> str:
    from html import escape

    return escape(str(error))




# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


def _get_app_ctx(ctx: Context) -> AppContext:
    """Get the AppContext from the FastMCP instance."""
    return ctx.fastmcp._app_context


def _get_user_id(ctx: Context) -> str:
    """Extract and validate user_id from request headers in the FastMCP context."""
    request = ctx.request_context.request
    try:
        return identity.extract_user_id(
            request.headers,
            identity.IdentityConfig(ctx.fastmcp._app_context.settings.user_header),
        )
    except identity.IdentityError as exc:
        raise identity.IdentityError(exc.code) from None


def register_mcp_tools(mcp: FastMCP) -> None:
    """Attach all storage + documentation tools to a FastMCP instance."""

    @mcp.tool()
    def list_files(
        ctx: Context, path: str = "", recursive: bool = False, pattern: str | None = None
    ) -> str:
        """List files under the user's personal storage. Path is relative.

        Args:
            path: relative subdirectory (empty = root).
            recursive: include subdirectory contents.
            pattern: optional glob filter (e.g. ``*.md``).
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.list_files(app_ctx.storage_cfg, uid, path, recursive, pattern)

    @mcp.tool()
    def read_file(ctx: Context, path: str) -> str:
        """Read a file as UTF-8 text. Returns ERROR for binary or >2MB files.

        Args:
            path: relative file path inside the user root.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.read_file(app_ctx.storage_cfg, uid, path)

    @mcp.tool()
    def write_file(ctx: Context, path: str, content: str) -> str:
        """Write UTF-8 text to a file, creating parent directories.

        Args:
            path: relative file path.
            content: UTF-8 string payload.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.write_file(app_ctx.storage_cfg, uid, path, content)

    @mcp.tool()
    def update_file(ctx: Context, path: str, content: str) -> str:
        """Update an existing file. Fails if the file does not exist.

        Args:
            path: relative file path.
            content: UTF-8 string payload.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.update_file(app_ctx.storage_cfg, uid, path, content)

    @mcp.tool()
    def delete_file(ctx: Context, path: str) -> str:
        """Delete a file or directory recursively.

        Args:
            path: relative file or directory path.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.delete_file(app_ctx.storage_cfg, uid, path)

    @mcp.tool()
    def move_file(ctx: Context, src: str, dst: str) -> str:
        """Move a file or directory within the same user root.

        Args:
            src: relative source path.
            dst: relative destination path (must not exist).
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.move_file(app_ctx.storage_cfg, uid, src, dst)

    @mcp.tool()
    def get_file_info(ctx: Context, path: str) -> str:
        """Return metadata for a file. Includes documentation and publishing state when relevant.

        Args:
            path: relative file path.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.get_file_info(app_ctx.storage_cfg, uid, path)

    @mcp.tool()
    def read_multiple_files(ctx: Context, paths: list[str]) -> str:
        """Read several text files at once.

        Args:
            paths: list of relative file paths inside the user root.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.read_multiple_files(app_ctx.storage_cfg, uid, paths)

    @mcp.tool()
    def edit_file(ctx: Context, path: str, edits: list[dict[str, str]]) -> str:
        """Apply find/replace edits to a text file.

        Each edit is {"oldText": ..., "newText": ...}. Each oldText must match
        exactly once; ambiguous or missing matches are rejected before writing.

        Args:
            path: relative file path.
            edits: list of {"oldText", "newText"} replacements.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.edit_file(app_ctx.storage_cfg, uid, path, edits)

    @mcp.tool()
    def write_binary_file(ctx: Context, path: str, content_base64: str) -> str:
        """Write a binary file (base64 payload) to personal storage.

        Used for Office documents and other non-UTF-8 content. Does not index.

        Args:
            path: relative file path (e.g. ``office/relatorio.docx``).
            content_base64: base64-encoded file contents.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.write_binary_file(app_ctx.storage_cfg, uid, path, content_base64)

    @mcp.tool()
    def read_binary_file(ctx: Context, path: str) -> str:
        """Read any file as base64, including binary files.

        Args:
            path: relative file path.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.read_binary_file(app_ctx.storage_cfg, uid, path)

    # --- Documentation ---

    @mcp.tool()
    def save_documentation(
        ctx: Context,
        filename: str,
        content: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        topics: list[str] | None = None,
    ) -> str:
        """Save a documentation file under ``docs/`` and update the user index.

        Does not publish or index automatically; suggests ``index_document`` as
        the follow-up.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.save_documentation(
            app_ctx.storage_cfg, uid, filename, content, title, description, tags, topics
        )

    @mcp.tool()
    def update_documentation(
        ctx: Context,
        filename: str,
        content: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        topics: list[str] | None = None,
    ) -> str:
        """Update an existing documentation file and refresh its index entry.

        Fails if the file does not exist.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.update_documentation(
            app_ctx.storage_cfg, uid, filename, content, title, description, tags, topics
        )

    @mcp.tool()
    def get_document_metadata(ctx: Context, filename: str) -> str:
        """Return the full documentation index entry for a filename."""
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.get_document_metadata(app_ctx.storage_cfg, uid, filename)

    # --- Publishing ---

    @mcp.tool()
    def publish_file(
        ctx: Context, path: str, expires_in_days: int | None = None
    ) -> str:
        """Create (or reuse) a public link for a file.

        Args:
            path: relative file path.
            expires_in_days: optional TTL in days; None means no expiry.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.publish_file(app_ctx.storage_cfg, uid, path, expires_in_days)

    @mcp.tool()
    def unpublish_file(ctx: Context, path_or_token: str) -> str:
        """Revoke a public link by path or token. The file remains private.

        Args:
            path_or_token: relative file path or token string.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.unpublish_file(app_ctx.storage_cfg, uid, path_or_token)

    @mcp.tool()
    def get_public_link(ctx: Context, path: str) -> str:
        """Return the active public link for a path, if any.

        Args:
            path: relative file path.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.get_public_link(app_ctx.storage_cfg, uid, path)

    @mcp.tool()
    def list_public_links(ctx: Context) -> str:
        """List all public links owned by the current user (including revoked)."""
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.list_public_links(app_ctx.storage_cfg, uid)

    # --- RAG ---

    @mcp.tool()
    def search_knowledge(ctx: Context, query: str, limit: int = 8) -> str:
        """Semantic search over the user's indexed documents.

        Args:
            query: free-text question.
            limit: number of results to return (1..50).
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.search_knowledge(app_ctx.storage_cfg, uid, query, limit)

    @mcp.tool()
    def index_document(ctx: Context, path: str) -> str:
        """Send a document to the rag_api and confirm via canary query.

        Args:
            path: relative file path (UTF-8 text).
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.index_document(app_ctx.storage_cfg, uid, path)

    @mcp.tool()
    def remove_from_knowledge(ctx: Context, path: str) -> str:
        """Delete a document from the knowledge base by path.

        Args:
            path: relative file path.
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.remove_from_knowledge(app_ctx.storage_cfg, uid, path)

    @mcp.tool()
    def get_index_status(ctx: Context) -> str:
        """Return counts of indexed/pending/failed documents and rag_api health."""
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)
        return tools.get_index_status(app_ctx.storage_cfg, uid)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
