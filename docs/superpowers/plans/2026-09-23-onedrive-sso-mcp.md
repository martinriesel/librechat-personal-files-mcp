# OneDrive SSO MCP Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicitly enabled OneDrive AppFolder provider, OAuth SSO, OneDrive MCP tools, and an operator configuration guide while preserving existing local tools.

**Architecture:** Implement OneDrive-specific auth, Graph client, storage, index, and publishing modules without refactoring local storage. Gate OneDrive tools on validated `STORAGE_PROVIDERS`, bind OAuth callbacks with one-time PKCE transactions, and use the existing rag_api with origin-aware document IDs.

**Tech Stack:** Python 3.12, FastAPI, FastMCP, httpx, cryptography/Fernet, SQLite, pytest, respx, Ruff, Microsoft Graph API.

**Spec:** `docs/superpowers/specs/2026-09-23-onedrive-sso-mcp-design.md`

## Global Constraints

- Default provider set is `local`; OneDrive tools are opt-in through `STORAGE_PROVIDERS`.
- OneDrive operations must remain inside Microsoft Graph `approot` and use delegated AppFolder permissions.
- `X-User-Id` remains the sole LibreChat identity source; never accept user ID from an MCP argument or OAuth callback parameter.
- OAuth uses Authorization Code + PKCE, signed state, short-lived SQLite transaction, and atomic single-use consumption.
- Token encryption uses dedicated `ONEDRIVE_TOKEN_ENCRYPTION_KEY`, separate from `JWT_SECRET`.
- OneDrive `memory-index.json` is authoritative and updated with eTag/`If-Match`.
- RAG remains on `rag_api` and distinguishes local and OneDrive document origins.
- Sharing is explicit and organization-only; the HTML viewer is authenticated and separate from sharing.
- Uploads above 4 MB use resumable Graph upload sessions and obey a configurable maximum size.
- Do not modify `/opt/LibreChat/docker-compose.override.yml`, active Nginx configuration, or containers.
- Tests must not make live Microsoft calls; run pytest and Ruff locally.

---

## File Map

- Modify `src/personal_files_mcp/config.py`: provider selection and Azure/OneDrive settings with safe local-only defaults.
- Modify `src/personal_files_mcp/runtime.py`: optional OneDrive service/runtime wiring.
- Add `src/personal_files_mcp/onedrive_auth.py`: OAuth URL, PKCE, state transactions, encrypted credentials, refresh/logout/status.
- Add `src/personal_files_mcp/onedrive_client.py`: async Microsoft identity and Graph HTTP client, retry/paging/upload-session support.
- Add `src/personal_files_mcp/onedrive_storage.py`: safe AppFolder path resolution and cloud CRUD/list/stat operations.
- Add `src/personal_files_mcp/onedrive_index.py`: eTag-aware remote memory index.
- Add `src/personal_files_mcp/onedrive_publishing.py`: organization-only links, revocation, and viewer token registry.
- Modify `src/personal_files_mcp/app.py`: optional route registration and conditional MCP tool registration.
- Modify `src/personal_files_mcp/tools.py` only where shared RAG APIs need origin-aware IDs; avoid broad rewrites.
- Add tests under `tests/test_onedrive_*.py` for each module and route/tool integration.
- Modify `pyproject.toml`: crypto dependency if not already available transitively; pin a direct supported dependency.
- Add `docs/onedrive-configuration.md`: Entra app setup, secrets, Compose variables, Nginx routes, rollout and smoke checks.
- Update `README.md`, `docker-compose.snippet.yml`, and `DECISIONS.md` with the opt-in provider and implementation decisions. Snippets are documentation only; do not edit the live override.

---

### Task 1: Provider configuration and safe tool gating

**Files:**
- Modify: `src/personal_files_mcp/config.py`
- Modify: `src/personal_files_mcp/runtime.py`
- Modify: `src/personal_files_mcp/app.py`
- Test: `tests/test_onedrive_config.py`
- Test: `tests/test_app.py`

**Interfaces:**
- `AppSettings.storage_providers: frozenset[str]` defaults to `{"local"}`.
- Add `onedrive_enabled` and typed Azure fields for tenant, client ID, client secret, redirect URI, scopes, encryption key, max upload size, and viewer base URL.
- `AppSettings.validate_onedrive_config()` raises a clear `ValueError` only when OneDrive is explicitly enabled but required settings are invalid.
- `AppContext` may hold an optional OneDrive auth/provider service; local-only construction remains backwards-compatible.

- [ ] **Step 1: Write tests proving local-only defaults and validation rules**

Test omitted `STORAGE_PROVIDERS` yields only `local`; `local,onedrive` enables the cloud provider; unknown provider names and enabled-but-missing Azure configuration are rejected; local-only settings do not require Azure secrets.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `.venv/bin/pytest tests/test_onedrive_config.py tests/test_app.py -q`
Expected: new config tests fail because the settings fields/validation do not yet exist.

- [ ] **Step 3: Implement provider parsing and settings validation**

Parse comma-separated provider names after trimming/lowercasing. Accept only `local` and `onedrive`. Validate Azure requirements only if OneDrive is selected. Parse max upload size as a positive integer and require a dedicated encryption secret when enabled.

- [ ] **Step 4: Run focused tests and full current suite**

Run: `.venv/bin/pytest tests/test_onedrive_config.py tests/test_app.py -q`
Run: `.venv/bin/pytest -q`
Expected: all pass; local-only registration and behavior remain unchanged.

---

### Task 2: One-time OAuth transactions and encrypted tokens

**Files:**
- Add: `src/personal_files_mcp/onedrive_auth.py`
- Modify: `src/personal_files_mcp/config.py`
- Test: `tests/test_onedrive_auth.py`

**Interfaces:**
- `OAuthTransactionStore.create(user_id: str, state_hash: str, pkce_verifier: str, expires_at: datetime) -> None`
- `OAuthTransactionStore.consume(state_hash: str, now: datetime) -> OAuthTransaction | None`; consumption is atomic and one-time.
- `OneDriveAuth.create_authorization(user_id: str) -> AuthorizationStart(url: str, state: str)`.
- `OneDriveAuth.handle_callback(code: str, state: str) -> AuthResult` validates state, consumes transaction, exchanges code with PKCE, validates tenant/issuer, and stores tokens.
- `OneDriveAuth.get_valid_access_token(user_id: str) -> str` refreshes before expiry and persists rotated refresh tokens.
- `OneDriveAuth.status(user_id: str) -> AuthStatus`; `logout(user_id: str) -> None` deletes local token data and attempts remote revocation best-effort.
- Token files are per user under the user root and encrypted with the dedicated Fernet key; state/token data never appears in logs or tool output.

- [ ] **Step 1: Write tests for transaction expiry, single-use, PKCE binding, and token isolation**

Cover mismatched/expired/replayed state, successful consumption once, Fernet round trip, wrong key failure, and distinct user token files.

- [ ] **Step 2: Run focused tests to verify failure**

Run: `.venv/bin/pytest tests/test_onedrive_auth.py -q`
Expected: FAIL because auth/store modules are not implemented.

- [ ] **Step 3: Implement SQLite transaction store and token encryption**

Use bound SQL parameters, hashed state as the lookup key, a short transaction TTL, and a delete/return transaction under one SQLite write transaction. Create token files through safe user-root resolution and atomic replacement.

- [ ] **Step 4: Add OAuth authorization, callback exchange, refresh, status, and logout behavior**

Use injected async HTTP transport/client factories so tests can mock Microsoft endpoints. Validate callback transaction before exchanging any code. Encrypt the complete token response, including refresh-token rotation, before persistence.

- [ ] **Step 5: Run auth tests and Ruff**

Run: `.venv/bin/pytest tests/test_onedrive_auth.py -q`
Run: `.venv/bin/ruff check src/personal_files_mcp/onedrive_auth.py tests/test_onedrive_auth.py`
Expected: pass without network calls.

---

### Task 3: Microsoft Graph client, retries, pagination, and upload sessions

**Files:**
- Add: `src/personal_files_mcp/onedrive_client.py`
- Modify: `pyproject.toml`
- Test: `tests/test_onedrive_client.py`

**Interfaces:**
- `GraphClient.request(method, path_or_url, *, params=None, json=None, content=None, headers=None) -> httpx.Response` supplies a fresh bearer token and bounded retries.
- `GraphClient.iter_pages(path, params=None) -> AsyncIterator[dict]` follows validated Graph `@odata.nextLink` URLs.
- `GraphClient.upload_file(path, content, max_bytes) -> dict` uses direct upload through 4 MiB and upload session chunks above 4 MiB, rejecting files above `max_bytes`.
- `GraphClient.download_file(path, max_bytes) -> bytes` enforces response-size limit.

- [ ] **Step 1: Write mocked tests for auth header, 429 Retry-After, pagination, and upload session chunks**

Assert chunk boundaries are multiples of 320 KiB, final chunk handling, upload URL is treated as a secret, and configured max bytes reject before upload.

- [ ] **Step 2: Run focused test to confirm failure**

Run: `.venv/bin/pytest tests/test_onedrive_client.py -q`
Expected: FAIL because client module is absent.

- [ ] **Step 3: Implement bounded Graph request/retry behavior and pagination**

Retry 429 using `Retry-After`; retry transient 5xx with bounded exponential delay/jitter. Do not retry unsafe writes unless the Graph operation/session semantics make retry safe. Prevent arbitrary pagination URL hosts outside Microsoft Graph.

- [ ] **Step 4: Implement bounded downloads and resumable upload sessions**

Use a configurable max upload size. Create session for payloads >4 MiB; chunk size obeys Graph alignment, and failures report resumability without leaking the preauthenticated upload URL.

- [ ] **Step 5: Add direct cryptography dependency and run client tests**

Run: `.venv/bin/pytest tests/test_onedrive_client.py -q`
Run: `.venv/bin/ruff check src/personal_files_mcp/onedrive_client.py tests/test_onedrive_client.py`
Expected: pass with respx only.

---

### Task 4: Secure AppFolder storage provider

**Files:**
- Add: `src/personal_files_mcp/onedrive_storage.py`
- Test: `tests/test_onedrive_storage.py`

**Interfaces:**
- `OneDriveStorage(client: GraphClient, max_file_bytes: int)`.
- Methods: `list(path="", recursive=False, pattern=None)`, `read(path, max_bytes=2*1024*1024)`, `write(path, content)`, `update(path, content)`, `edit(path, edits)`, `delete(path)`, `move(src, dst)`, `stat(path)`.
- Every method resolves only relative paths beneath Graph `approot`; reject absolute paths, `..`, control chars, empty invalid segments, and provider-root escapes.

- [ ] **Step 1: Write tests for valid paths, traversal rejection, AppFolder scoping, and storage operations**

Mock Graph endpoints; assert every request is rooted under `approot` and directory creation is idempotent.

- [ ] **Step 2: Run tests and confirm missing-module failure**

Run: `.venv/bin/pytest tests/test_onedrive_storage.py -q`
Expected: FAIL because provider module is absent.

- [ ] **Step 3: Implement path normalization and Graph item resolution**

Represent paths as validated POSIX segments and resolve folders from the AppFolder root; never interpolate unvalidated path text into Graph URLs.

- [ ] **Step 4: Implement CRUD/list/stat/edit using GraphClient**

Preserve local tool semantics: update requires existing file, edit requires each oldText to match exactly once, deletes use Graph trash behavior, and move/rename stays inside approot.

- [ ] **Step 5: Run provider tests and Ruff**

Run: `.venv/bin/pytest tests/test_onedrive_storage.py -q`
Run: `.venv/bin/ruff check src/personal_files_mcp/onedrive_storage.py tests/test_onedrive_storage.py`
Expected: pass.

---

### Task 5: Remote memory index and origin-aware RAG

**Files:**
- Add: `src/personal_files_mcp/onedrive_index.py`
- Modify: `src/personal_files_mcp/tools.py`
- Modify: `src/personal_files_mcp/rag_client.py` only if its payload shape requires an explicit origin metadata field.
- Test: `tests/test_onedrive_index.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- `OneDriveDocIndex(storage: OneDriveStorage)` exposes `list_documents()`, `get(file_path)`, `upsert(entry)`, `remove(file_path)`, and `set_indexing_status(file_path, status, rag_file_id=None)`.
- `mutate` reads item content and eTag, applies a pure mutation, writes with `If-Match`, then rereads/reapplies on 412 with a bounded retry count.
- OneDrive uses its own persisted UUID `rag_file_id` per indexed document, independent of local storage. Do not assume rag_api accepts arbitrary origin metadata; filter query IDs exclusively from the current user's provider index.

- [ ] **Step 1: Write tests for eTag conflict/retry and distinct local/OneDrive RAG identity**

Prove conflicting updates preserve both independent document changes and a same-named local and OneDrive file cannot overwrite or remove one another's RAG record.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/pytest tests/test_onedrive_index.py -q`
Expected: FAIL because the remote index module is absent.

- [ ] **Step 3: Implement remote index serialization and conditional mutation**

Reuse the existing index schema and `DocumentEntry` semantics where practical, but keep cloud IO independent of local `docindex.py` filesystem writes.

- [ ] **Step 4: Add provider identity to OneDrive RAG operations**

Add OneDrive-specific index/remove/status helpers using `RagClient`; verify canary behavior remains intact and cleanup only removes the user-owned file ID for that origin.

- [ ] **Step 5: Run index/RAG tests and Ruff**

Run: `.venv/bin/pytest tests/test_onedrive_index.py tests/test_tools.py tests/test_rag_client.py -q`
Run: `.venv/bin/ruff check src/personal_files_mcp/onedrive_index.py src/personal_files_mcp/tools.py src/personal_files_mcp/rag_client.py`
Expected: pass.

---

### Task 6: OneDrive organizational sharing and authenticated viewer

**Files:**
- Add: `src/personal_files_mcp/onedrive_publishing.py`
- Modify: `src/personal_files_mcp/app.py`
- Test: `tests/test_onedrive_publishing.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Organization-only `create_link(item_id)` explicitly requests Graph link scope `organization`; reject any response that is anonymous or broader than configured tenant policy.
- `revoke_link(item_id, permission_id)` deletes only the stored permission created by this MCP.
- `create_viewer_token(user_id, item_id, ttl)` stores a cryptographically random, short-lived, user-bound token; `serve_viewer(token, authenticated_user_id)` requires both the matching validated `X-User-Id` and token, then streams only the bound OneDrive item.
- Routes: `/auth/azure/login`, `/auth/azure/callback`, and `/onedrive/view/{token}` are registered before the MCP catch-all. Callback uses OAuth state rather than `X-User-Id`; viewer requires the authenticated user's validated `X-User-Id` in addition to its opaque token. If proxy/browser cannot supply validated identity, do not expose the viewer route as a public bearer URL.

- [ ] **Step 1: Write tests for org-only link enforcement, token expiry/binding, viewer safety headers, and OAuth callback**

Assert anonymous link response is rejected, missing identity is rejected, a user cannot use another user's viewer token, expiry returns 410, and HTML is served with inline content type plus restrictive headers.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/pytest tests/test_onedrive_publishing.py tests/test_app.py -q`
Expected: new tests fail because routes/publishing module are absent.

- [ ] **Step 3: Implement sharing and viewer token registry**

Use SQLite in `/data/private/_system`, hash opaque tokens at rest, store user/item binding and expiry, and never persist preauthenticated download URLs. Viewer tokens are an additional factor, not a substitute for authenticated user identity.

- [ ] **Step 4: Register OAuth/view routes with correct middleware exceptions**

Keep all other user-scoped routes behind identity validation. Restrict OAuth login endpoint to signed, nonexpired state; callback verifies the stored transaction before code exchange.

- [ ] **Step 5: Run publishing/route tests and Ruff**

Run: `.venv/bin/pytest tests/test_onedrive_publishing.py tests/test_app.py -q`
Run: `.venv/bin/ruff check src/personal_files_mcp/onedrive_publishing.py src/personal_files_mcp/app.py tests/test_onedrive_publishing.py`
Expected: pass.

---

### Task 7: Conditional MCP tool catalog

**Files:**
- Modify: `src/personal_files_mcp/app.py`
- Modify: `src/personal_files_mcp/runtime.py`
- Add: `src/personal_files_mcp/onedrive_tools.py`
- Test: `tests/test_onedrive_tools.py`
- Test: `tests/test_app.py`

**Interfaces:**
- `register_onedrive_tools(mcp: FastMCP) -> None` registers login/status/logout plus OneDrive file, documentation, RAG, sharing, and direct-link tools.
- Tools obtain `user_id` only from validated MCP request context; no tool accepts identity as an argument.
- Local `register_mcp_tools` behavior and tool names remain unchanged.

- [ ] **Step 1: Write tools/list tests for local-only and enabled-provider configurations**

Inspect FastMCP tool registry directly or issue MCP `tools/list`; assert no `onedrive_*` names by default and required names appear when enabled.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/pytest tests/test_onedrive_tools.py tests/test_app.py -q`
Expected: fail because conditional registration is not implemented.

- [ ] **Step 3: Implement OneDrive tool wrappers and structured error mapping**

Use concise JSON string responses and stable `onedrive_unauthorized`, `onedrive_not_found`, `onedrive_conflict`, `onedrive_rate_limited`, and provider-specific codes. Never leak tokens, upload URLs, or raw exceptions.

- [ ] **Step 4: Register only enabled and fully configured providers**

Build OneDrive services in application context/lifespan and register tools based on validated settings. Keep local tool registration unconditional.

- [ ] **Step 5: Run tool catalog tests and Ruff**

Run: `.venv/bin/pytest tests/test_onedrive_tools.py tests/test_app.py -q`
Run: `.venv/bin/ruff check src/personal_files_mcp/onedrive_tools.py src/personal_files_mcp/app.py`
Expected: pass.

---

### Task 8: Operator configuration guide and deployment snippets

**Files:**
- Add: `docs/onedrive-configuration.md`
- Modify: `docker-compose.snippet.yml`
- Modify: `README.md`
- Modify: `DECISIONS.md`
- Test/verify: documentation and Compose YAML parse

**Interfaces:**
- Guide documents Entra single-tenant app registration, delegated permissions, redirect URI, exact environment variable names, secret generation/storage, Compose provider toggle, Nginx proxy routes, restart/rollback, and local smoke checks.
- Compose snippet remains opt-in with `STORAGE_PROVIDERS=local` and commented/clearly disabled OneDrive configuration until operator sets required values.
- Do not put real secret values in docs or modify live `/opt/LibreChat/docker-compose.override.yml`.

- [ ] **Step 1: Write the operator guide from implemented settings and routes**

Include exact URLs/paths from implementation, the required scopes, expected callback and viewer route behavior, and explicit distinction between org links and authenticated viewer URLs.

- [ ] **Step 2: Update documented Compose snippet and README**

Add provider toggle and Azure secret variable references without enabling OneDrive by default. Record architecture decisions in `DECISIONS.md`.

- [ ] **Step 3: Validate YAML and doc consistency**

Run a YAML parse against `docker-compose.snippet.yml` using the available PyYAML dependency or `docker compose config` only if available without starting containers. Search the guide for each config field and route implementation.

- [ ] **Step 4: Run full test and lint suites**

Run: `.venv/bin/pytest -q`
Run: `.venv/bin/ruff check .`
Expected: all tests pass and Ruff reports no errors.

---

## Completion Criteria

- Default configuration exposes all existing local tools and no OneDrive tools.
- Explicit valid configuration exposes the complete OneDrive catalog.
- OAuth transaction is PKCE-bound, short-lived, atomic single-use; token storage is encrypted with a dedicated key.
- Every OneDrive path and Graph operation is confined to `approot`.
- Upload size bounds, chunking, paging, retries, and index eTag conflict behavior are tested without network access.
- RAG operations maintain user and provider origin isolation.
- Sharing links are organization-only; viewer tokens are authenticated, expiring, and separate from sharing.
- Operator guide is complete and matches code and Compose/Nginx snippets.
- `.venv/bin/pytest -q` and `.venv/bin/ruff check .` pass.
- Live Compose override, Nginx config, and containers remain untouched.
