# OneDrive SSO MCP Extension — Design

## Goal

Extend `librechat-personal-files-mcp` with an explicitly enabled OneDrive storage provider and a complete family of OneDrive MCP tools while preserving all current local tools and behavior. Google Drive is a future provider and is not part of this implementation.

## Current system

- FastAPI hosts a stateless FastMCP HTTP server at `/mcp`.
- `X-User-Id` is validated by `IdentityMiddleware`; MCP tools obtain identity from the request context, never from LLM arguments.
- Local files live under `/data/private/<userId>` and current storage, document index, RAG, and publishing implementations are established and tested.
- `/opt/LibreChat/docker-compose.override.yml` builds the MCP from `./mcp-personal-files`. The active Compose/Nginx stack is managed by the user and is not to be changed or restarted as part of local implementation.
- Existing untracked architecture documents are user work and must remain untouched.

## Decisions

1. **Incremental provider integration.** Do not migrate or refactor local storage now. Add OneDrive-specific modules and tools; extract a broader common provider abstraction only when a second cloud provider is implemented.
2. **Explicit enablement.** `STORAGE_PROVIDERS` controls provider tool registration. Its default is `local`; OneDrive tools are registered only when explicitly enabled and all required Azure configuration is valid. Existing local tools remain registered.
3. **Least privilege.** OneDrive operations are confined to Microsoft Graph `approot` and the delegated `Files.ReadWrite.AppFolder` permission (plus only the configured identity/profile scope needed for login/status).
4. **OAuth security.** Use Authorization Code with PKCE. Bind signed state to a short-lived, single-use OAuth transaction stored in SQLite under `/data/private/_system`; consume the transaction atomically in the callback. Never trust callback-supplied user identity.
5. **Token protection.** Persist per-user refresh/access token material encrypted with `ONEDRIVE_TOKEN_ENCRYPTION_KEY`, a secret distinct from `JWT_SECRET`. Refresh tokens transparently; logout removes local credentials and attempts remote revocation best-effort.
6. **OneDrive status.** Return connected state, basic Microsoft identity, and access-token expiry. Do not include quota in the initial version.
7. **Remote document index.** OneDrive `memory-index.json` in AppFolder is authoritative for OneDrive document metadata and indexing state. Use Graph eTags and conditional writes to prevent lost concurrent updates. Keep RAG file IDs unique per user and provider; origin is tracked in the OneDrive index rather than assuming the rag_api supports arbitrary metadata fields.
8. **RAG ownership.** Continue using the existing `rag_api`; generate and persist independent UUID file IDs for OneDrive documents so same-named local and OneDrive files cannot collide. Query only IDs from the corresponding user's index and preserve canary verification.
9. **Sharing and viewing.** `onedrive_publish_file` creates only an organization-scoped link after explicit invocation. Anonymous links are not supported. HTML gateway access is authenticated and does not imply publication.
10. **Uploads.** Uploads above 4 MB use Microsoft Graph upload sessions. Enforce a configurable OneDrive maximum size and support retry/resume within Graph session validity.
11. **Operational boundary.** Implement and test locally with mocked Microsoft endpoints. Provide a separate operator guide covering Entra app registration, Compose environment, Nginx routes, and manual deployment/validation. Do not edit the live Compose override or restart containers.

## Architecture

Add dedicated OneDrive modules for OAuth/token persistence, Microsoft Graph HTTP calls, and AppFolder storage. `AppContext` receives optional OneDrive services based on validated settings. FastAPI owns `/auth/azure/login`, `/auth/azure/callback`, and an authenticated `/onedrive/view/{token}` route; these routes are registered before the MCP catch-all mount. OAuth callback is exempted from normal `X-User-Id` middleware because its one-time state is its authorization. The viewer route requires the authenticated user's validated `X-User-Id` plus a short-lived token bound to that user and item; it must not be treated as a public bearer URL. If the reverse-proxy flow cannot provide the user's validated identity, HTML viewing remains disabled until a secure browser authentication mechanism is configured.

The MCP registration keeps all local tools intact. When OneDrive is enabled, it registers `onedrive_login`, `onedrive_auth_status`, `onedrive_logout`, and OneDrive-prefixed counterparts for file CRUD/list/read, documentation, RAG, and publishing, plus the direct-link tool. OneDrive path validation rejects absolute paths and traversal and resolves paths only below `approot`.

The OneDrive client handles paging, token refresh, 429/5xx retries using `Retry-After` where supplied, and bounded response sizes. Simple uploads use direct content upload; larger uploads use resumable upload sessions. The remote index uses eTags/`If-Match` and retries conflict by rereading and applying the mutation to current data.

## Failure behavior

- If OneDrive is disabled, no `onedrive_*` tools or routes requiring OneDrive are exposed; local functionality is unaffected.
- If OneDrive is enabled but required settings are missing or malformed, application startup fails with a clear configuration error rather than advertising unusable tools.
- If no valid per-user connection exists or refresh is revoked, OneDrive tools return structured `onedrive_unauthorized` guidance; local tools continue normally.
- OAuth state mismatch, expired transaction, reused transaction, invalid PKCE verifier, or invalid callback is rejected without storing tokens.
- Graph 401 after refresh returns an actionable re-login error. 403, 404, 409/412, 429, and oversized payloads map to stable tool error codes without leaking tokens or raw stack traces.
- Viewer tokens are opaque, short-lived, user- and item-bound, and are insufficient without the matching authenticated user identity. Responses are inline only for approved HTML content types and include defensive headers; no arbitrary URL proxying is allowed.
- Organizational link creation fails closed if tenant policy cannot enforce organization scope; it must never silently fall back to anonymous access.

## Testing and acceptance

- Tests use `pytest`, `httpx`/`respx`, FastAPI `TestClient`, and temporary directories; no Microsoft network calls are required.
- Existing local tests remain green and local tool registration remains unchanged under default settings.
- Tests cover provider gating, OAuth state/PKCE single-use, encrypted token round trips and per-user isolation, token refresh/revocation, AppFolder path safety, Graph pagination, upload-session chunking/retries, eTag conflict handling, independent per-provider RAG IDs, organization-only sharing, and authenticated viewer access.
- `pytest -q` and `ruff check .` pass in `mcp-personal-files`.
- The operator guide documents required secrets and exact environment variables and provides Compose/Nginx examples without modifying `/opt/LibreChat/docker-compose.override.yml`.

## Scope exclusions

- Google Drive implementation.
- Broad migration of local storage to a generic provider interface.
- Anonymous OneDrive links.
- Live Entra/Nginx/Compose changes or container restarts by the agent.
- OneDrive quota reporting in `onedrive_auth_status`.
