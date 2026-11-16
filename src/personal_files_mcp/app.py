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
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastmcp import Context, FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import diagnostics, identity, project_context, project_resolver, publishing, storage, tools
from .config import AppSettings
from .runtime import AppContext

logger = logging.getLogger(__name__)


def build_context() -> AppContext:
    settings = AppSettings.from_env()
    diagnostics.configure_logging(settings)
    storage_cfg = storage.StorageConfig(
        storage_root=Path(settings.storage_root),
        max_file_bytes=settings.max_file_size_mb * 1024 * 1024,
        max_binary_bytes=settings.max_binary_file_mb * 1024 * 1024,
    )
    mongo_client = None
    if settings.librechat_mongo_uri:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient

            mongo_client = AsyncIOMotorClient(
                settings.librechat_mongo_uri,
                serverSelectionTimeoutMS=settings.librechat_mongo_timeout_ms,
            )
        except Exception as exc:
            logger.warning("failed to initialize mongo client: %s", exc)

    resolver = project_resolver.ProjectResolver(
        mongo_client=mongo_client,
        db_name=settings.librechat_mongo_db,
        timeout_ms=settings.librechat_mongo_timeout_ms,
    )
    return AppContext(
        settings=settings,
        storage_cfg=storage_cfg,
        project_resolver=resolver,
    )


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
        # Attach AppContext to the FastMCP instance for tool access.
        mcp._app_context = ctx
        app.state.app_context = ctx
        # Run the MCP sub-app lifespan (required for StreamableHTTPSessionManager).
        async with mcp_app.lifespan(app):
            yield
        # Graceful shutdown of project resolver resources
        if ctx.project_resolver and getattr(ctx.project_resolver, "_client", None):
            with suppress(Exception):
                ctx.project_resolver._client.close()

    app = FastAPI(
        title="librechat-personal-files-mcp",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.app_context = ctx
    app.add_middleware(
        IdentityMiddleware,
        header_name=ctx.settings.user_header,
        conversation_header=ctx.settings.conversation_header,
    )

    # Register user-facing routes BEFORE the catch-all MCP mount so they take
    # precedence.
    register_health(app)
    register_public_endpoint(app)
    register_binary_endpoint(app)
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

    def __init__(
        self,
        app,
        header_name: str = "X-User-Id",
        conversation_header: str = "X-Conversation-Id",
    ) -> None:
        super().__init__(app)
        self._config = identity.IdentityConfig(header_name=header_name)
        self._conversation_header = conversation_header

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/healthz" or path.startswith("/files"):
            return await call_next(request)
        try:
            user_id = identity.extract_user_id(request.headers, self._config)
        except identity.IdentityError as exc:
            return JSONResponse(
                status_code=403,
                content={"error": exc.code},
            )
        request.state.user_id = user_id

        # Extract and validate conversation ID
        request_id = secrets.token_hex(8)
        request.state.request_id = request_id

        conv_id, conv_status = project_context.extract_conversation_id(
            request.headers, self._conversation_header
        )
        request.state.conversation_id = conv_id
        request.state.conversation_status = conv_status

        app_ctx = getattr(request.app.state, "app_context", None)
        if app_ctx:
            if conv_status == "valid":
                diagnostics.log_context_event(
                    app_ctx.settings,
                    "context.received",
                    request_id=request_id,
                    identity_present=True,
                    conversation_header_present=True,
                    conversation_id_valid=True,
                    conversation_id=conv_id,
                )
            elif conv_status in ("placeholder", "invalid"):
                diagnostics.log_context_event(
                    app_ctx.settings,
                    "context.invalid",
                    request_id=request_id,
                    identity_present=True,
                    conversation_header_present=True,
                    conversation_id_valid=False,
                    error_code=conv_status,
                )
            elif conv_status == "missing":
                diagnostics.log_context_event(
                    app_ctx.settings,
                    "context.discovery_without_conversation",
                    request_id=request_id,
                    identity_present=True,
                    conversation_header_present=False,
                )

        token = project_context.set_request_context(
            project_context.RequestContextData(
                user_id=user_id,
                conversation_id=conv_id,
                request_id=request_id,
            )
        )
        try:
            return await call_next(request)
        finally:
            project_context.reset_request_context(token)


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


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


def _get_app_ctx(ctx: Context) -> AppContext:
    """Get the AppContext from the FastMCP instance."""
    return ctx.fastmcp._app_context


def _get_user_id(ctx: Context) -> str:
    """Extract and validate user_id from request headers in the FastMCP context."""
    request = ctx.request_context.request
    raw = request.headers.get("X-User-Id", "").strip()
    try:
        return identity.validate_user_id(raw)
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

    # --- Project context ---

    @mcp.tool()
    async def get_project_context(ctx: Context) -> str:
        """Resolve and return active LibreChat project context for the current conversation.

        Identifies the project associated with the current conversation.
        Returns a structured JSON response with status ('project', 'no_project',
        'conversation_unavailable', 'context_missing', etc.).
        """
        app_ctx = _get_app_ctx(ctx)
        uid = _get_user_id(ctx)

        request = ctx.request_context.request
        req_ctx = project_context.get_request_context()

        conv_id = getattr(request.state, "conversation_id", None)
        conv_status = getattr(request.state, "conversation_status", "valid")
        request_id = getattr(request.state, "request_id", "")

        if req_ctx:
            conv_id = conv_id or req_ctx.conversation_id
            request_id = request_id or req_ctx.request_id

        return await tools.get_project_context(
            app_ctx,
            user_id=uid,
            conversation_id=conv_id,
            conv_status=conv_status,
            request_id=request_id,
        )


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
