# librechat-personal-files-mcp

MCP server providing per-user personal file storage, persistent documentation, RAG indexing/retrieval, and opaque public link publishing for LibreChat agents.

> **Related fork**: this server is designed to work with a fork of
> [ForLegalAI/mcp-ms-office-documents](https://github.com/ForLegalAI/mcp-ms-office-documents)
> that stores generated Office documents (`.docx`/`.xlsx`/`.pptx`/`.eml`/`.xml`)
> in the user's private storage here via the `POST /binary` endpoint. See the
> fork's documentation for the `PERSONAL_FILES` upload strategy.

## Features

- **Per-user storage**: Private files under `/data/private/<userId>/` with full CRUD, list, move operations.
- **Binary file support**: `write_binary_file` / `read_binary_file` (base64) MCP tools plus a `POST /binary` multipart endpoint (service-token authenticated) so other services — e.g. the Office-documents fork — can store generated `.docx`/`.xlsx`/`.pptx` files per user.
- **Multi-scope documentation catalog**: `save_documentation` auto-routes to `projects/<project_id>/` or `docs/` with fail-closed resolver; each scope maintains its own `memory-index.json` (schema v3). Catalog files are protected from direct LLM writes.
- **RAG integration**: `index_document` sends content to rag_api via `POST /embed` with canary verification; `search_knowledge` for semantic search; `remove_from_knowledge` for deletion.
- **Public publishing**: `publish_file` creates cryptographically random tokens (≥128 bits); files served directly via streaming `FileResponse` with `Content-Disposition: attachment`, `nosniff`, `no-store`, and HTTP Range support (compatible with Traefik/Dokploy and Nginx).
- **Strict security**: Fail-closed on missing/invalid `X-User-Id`; path traversal blocked; no absolute paths; no `..` segments; symlink escape detection; reserved segment protection; atomic index writes with cross-process locking.

## Architecture

```
┌──────────────┐     ┌────────────────────────────────┐     ┌─────────────┐
│ LibreChat    │────▶│ librechat-personal-files-mcp   │──▶  │ rag_api     │
│ (Agent)      │ MCP │ (stateless HTTP /mcp)          │     │ (vector DB) │
└──────────────┘     └────────────────────────────────┘     └─────────────┘
                            │
                            │ GET /files/{token}
                            ▼
                     ┌──────────────┐
                     │ Proxy/Router │  (Traefik / Nginx)
                     └──────────────┘
                            │
                            ▼
                     ┌──────────────┐
                     │ Public Client│  (Direct chunk streaming)
                     └──────────────┘
```

## Requirements

- A running [LibreChat](https://www.librechat.ai/) instance with `rag_api` enabled.
- A `JWT_SECRET` shared with LibreChat and `rag_api` (see below).
- A reverse proxy (Traefik in Dokploy, or Nginx) routing `/files/` to the MCP container.

## Installation

### 1. Get the Docker image

Pull a pre-built image from GHCR, pinning an explicit tag:

```bash
docker pull ghcr.io/martinriesel/librechat-personal-files-mcp:<tag>
```

Images are built by CI from `v*` git tags (published as the tag without the `v`, e.g. `v1.5.0-test` → `1.5.0-test`) and from `main` (`main` / `latest`). Check the [package page](https://github.com/martinriesel/librechat-personal-files-mcp/pkgs/container/librechat-personal-files-mcp) for the available tags. `latest` follows `main` and may lag behind the newest tagged version.

Or build it yourself:

```bash
git clone https://github.com/martinriesel/librechat-personal-files-mcp.git
cd librechat-personal-files-mcp
docker build -t librechat-personal-files-mcp .
```

### 2. Prepare storage

The container runs as UID `2000` / GID `2500`. Published files are streamed by the MCP container itself, so the reverse proxy does **not** need access to the data directory.

The compose snippet uses **named volumes**; Docker initializes them from the image with the correct ownership, so no host preparation is needed. If you prefer **bind mounts** instead, create the directories once (as root):

```bash
cd /path/to/your/librechat
groupadd -g 2500 lcfiles 2>/dev/null || true
mkdir -p ./data/private/_system
chown -R 2000:2500 ./data/private
chmod 2770 ./data/private          # group rwx + setgid; no world access needed
```

Ensure `JWT_SECRET` is set in your LibreChat `.env` (same value must be shared with `rag_api`):

```bash
grep -q '^JWT_SECRET=' .env || echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env
```

### 3. Add the service to your compose file

Add the service block from `docker-compose.snippet.yml` to your `docker-compose.override.yml` (or paste it into a Dokploy Compose application):

- A `librechat-personal-files` service (image, environment, volumes, networks).

Set `PUBLIC_BASE_URL` to your own public URL, e.g. `https://chat.example.com/files`, replace `<tag>` with an existing image tag and `<shared-network>` with a Docker network that also contains `rag_api` and MongoDB (and your reverse proxy), so their hostnames resolve from inside this container. Secrets (`JWT_SECRET`, `PERSONAL_FILES_SERVICE_TOKEN` → `SERVICE_TOKEN`, `LIBRECHAT_MONGO_URI`) come from your environment / `.env`, never from the compose file.

After deploying, check from inside the container that `rag_api` is reachable: `/healthz` should report `"rag": "available"`. A `Temporary failure in name resolution` error from the RAG tools means the containers do not share a network.

Then start it:

```bash
docker compose up -d librechat-personal-files
```

### 4. Configure the reverse proxy (Traefik or Nginx)

#### Option A: Traefik (Dokploy)
In the Dokploy dashboard, configure a Domain for `librechat-personal-files`:
- **Host**: `chat.example.com`
- **Path**: `/files/`
- **Internal Port**: `8080`
- **HTTPS**: enabled

See [docs/links-publicos-dokploy-traefik.md](docs/links-publicos-dokploy-traefik.md) for architecture details and verification steps.

#### Option B: Nginx
Add this location to your existing Nginx server block:

```nginx
location ^~ /files/ {
    proxy_pass http://librechat-personal-files:8080/files/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### 5. Activate the MCP server in LibreChat

In the LibreChat **Admin Panel → MCP Servers**, add (or in your `librechat.yaml`):

```yaml
mcpSettings:
  allowedAddresses:
    - 'librechat-personal-files:8080'

mcpServers:
  personal-files:
    type: streamable-http
    url: http://librechat-personal-files:8080/mcp
    timeout: 120000
    chatMenu: false
    headers:
      X-User-ID: '{{LIBRECHAT_USER_ID}}'
      X-User-Email: '{{LIBRECHAT_USER_EMAIL}}'
    requestHeaders:
      X-Conversation-Id: '{{LIBRECHAT_BODY_CONVERSATIONID}}'
```

Restart LibreChat. The `personal-files` server will be available to your agents.

See [docs/mongodb-permissions-dokploy.md](docs/mongodb-permissions-dokploy.md) for Docker shared network setup, MongoDB `mcp_project_reader` user creation, and connection parameters.

> [!IMPORTANT]
> Use `streamable-http` at `/mcp` only. SSE at `/mcp/sse` is **not supported** and should not be configured.

### Enabling Tools in a LibreChat Agent

After the MCP server is connected, enable specific tools in your agent configuration:

1. Go to **Admin Panel → Agents** (or edit your agent YAML).
2. Under MCP servers, select `personal-files`.
3. Enable the tools you want the agent to use. Key tools to enable:
   - `save_documentation`, `update_documentation`, `list_documentation` — document management.
   - `get_project_context`, `list_projects` — project discovery (read-only).
   - `search_knowledge`, `index_document`, `index_batch` — RAG operations.
   - `register_documentation`, `transfer_documentation`, `reconcile_documentation` — catalog maintenance.
   - `publish_file`, `get_public_link` — public link management.
4. Save and test the agent in a project conversation.

### 6. Service-to-service binary uploads (`POST /binary`)

Other services can store files in a user's private storage without speaking
MCP. Set `SERVICE_TOKEN` on this server and have the service send a multipart
request:

```
POST http://librechat-personal-files:8080/binary
X-Service-Token: <SERVICE_TOKEN>
X-User-Id: <userId>

form: file=<bytes>, path=office/report.docx
```

Both `SERVICE_TOKEN` and a valid `X-User-Id` are required (fail-closed); the
path goes through the same path-safety checks as every other tool. On success
it returns `{path, filename, mime_type, size_bytes, sha256}`.

This is how the [Office-documents fork](https://github.com/ForLegalAI/mcp-ms-office-documents)
stores generated `.docx`/`.xlsx`/`.pptx` files per user (see its `PERSONAL_FILES`
strategy).

### Volume Preservation & Deployment

> [!CAUTION]
> The volume mounted at `/data/private` contains all user files and documentation indexes. **Never delete or recreate this volume** during version upgrades.

- **Use published images**: Deploy stable tags from `ghcr.io/martinriesel/librechat-personal-files-mcp:<tag>`.
- **Avoid fixed `container_name`**: Let Dokploy manage container lifecycle for safe rolling updates and rollbacks.
- **Verify networks after changes**: After modifying network settings in Dokploy, verify effective networks with `docker inspect <container>` — saving the option or restarting may not be sufficient.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USER_HEADER` | `X-User-Id` | Header carrying the user identity |
| `STORAGE_ROOT` | `/data/private` | Root directory for user data |
| `SHARE_ROOT` | `/data/share` | Legacy shared area (read/write during migration) |
| `RAG_API_URL` | `http://rag_api:8000` | rag_api endpoint |
| `JWT_SECRET` | *required* | HS256 secret shared with LibreChat/rag_api (≥32 chars) |
| `PUBLIC_BASE_URL` | `https://your-domain.example/files` | Base URL for public links |
| `MAX_FILE_SIZE_MB` | `20` | Max upload size for text files |
| `MAX_BINARY_FILE_MB` | `50` | Max upload size for binary files (`/binary`, `write_binary_file`) |
| `SERVICE_TOKEN` | *(empty)* | Shared token required by the `POST /binary` endpoint; without it the endpoint returns 503 |
| `REGISTRY_DB` | `/data/private/_system/links.db` | SQLite registry for public links |
| `CONVERSATION_HEADER` | `X-Conversation-Id` | Header carrying the conversation identity |
| `LIBRECHAT_MONGO_URI` | *(empty)* | Read-only MongoDB connection URI for project resolution |
| `LIBRECHAT_MONGO_DB` | `LibreChat` | MongoDB database name |
| `LIBRECHAT_MONGO_TIMEOUT_MS` | `2000` | Timeout in ms for project resolution queries |
| `PERSONAL_FILES_LOG_LEVEL` | `INFO` | Application log level (`OFF`, `ERROR`, `WARNING`, `INFO`, `DEBUG`) |
| `PERSONAL_FILES_CONTEXT_DIAGNOSTICS` | `false` | Enable single-line JSON context diagnostic logging (see [docs/diagnostics-and-logs.md](docs/diagnostics-and-logs.md)) |

> [!TIP]
> Set `PERSONAL_FILES_CONTEXT_DIAGNOSTICS=false` in production after initial validation is complete. See [docs/diagnostics-and-logs.md](docs/diagnostics-and-logs.md) for troubleshooting guidance.

## MCP Tools

### Storage

- `list_files(path="", recursive=false, pattern=null)` — list files/dirs
- `read_file(path)` — read UTF-8 text; error for binary or >2 MB
- `read_multiple_files(paths)` — read several text files at once
- `write_file(path, content)` — write text (UTF-8), creates parent dirs (protected from modifying `memory-index.json`)
- `write_binary_file(path, content_base64)` — write a binary file (e.g. Office documents); uses the binary size limit, never indexes
- `read_binary_file(path)` — read any file as base64 (binary or text)
- `update_file(path, content)` — update existing file
- `edit_file(path, edits)` — find/replace text (each `oldText` must match exactly once)
- `delete_file(path)` — delete file or directory
- `move_file(src, dst)` — move within user root
- `get_file_info(path)` — metadata + docindex + publishing status

### Documentation (Multi-Scope Catalog)

Documentation files and catalogs follow a structured hierarchy:
- General scope: `docs/*.md` with catalog `docs/memory-index.json`
- Project scope: `projects/<project_id>/*.md` with catalog `projects/<project_id>/memory-index.json`
- Direct LLM writes/edits/deletions of any `memory-index.json` are rejected (`protected_file`).

Tools:
- `save_documentation(filename, content, title?, description?, tags?, topics?, project_id?, project_name?)` — auto-routes to `projects/<project_id>/` for project chats, or `docs/` for chats proven without a project. Fails closed (`project_resolution_failed`) if resolver errors.
- `update_documentation(filename, content, title?, description?, tags?, topics?, project_id?, project_name?)` — updates existing doc across catalogs, preserving existing project association.
- `get_document_metadata(filename)` — looks up document across catalogs by `doc_id`, relative path, or filename. Returns metadata including `modified_since_indexing`.
- `list_documentation(project_id?, tag?, unassigned_only=false)` — lists registered documentation across scopes or filtered by project/unassigned, reporting `doc_id` and indexing status.
- `register_documentation(path, title?, description?, tags?, topics?, project_id?, project_name?)` — registers an existing `.md`/`.txt` file on disk into the catalog without rewriting its contents.
- `transfer_documentation(identifier, target_project_id?, target_filename?, dry_run=false)` — transfers a document between general and project scopes or between projects, preserving `doc_id`, metadata, RAG status, and public link URLs. Supports collision detection and dry-run preview.
- `reconcile_documentation(project_id?, repair=false)` — scans for unregistered files on disk, orphaned catalog entries, and modified files after indexing. Repairs discrepancies only when `repair=true`.

### RAG & Batch Indexing

- `search_knowledge(query, limit=8, project_id?, scope="auto")` — semantic search with project scoping (`auto`, `project`, `personal`, `all`). In a project conversation, `auto` restricts candidate documents to the active project's indexed files. `auto` searches all documents only when the conversation is confirmed to have no project; if the project cannot be resolved it returns `project_resolution_failed` instead of widening the search. Project membership = explicit `project_id` metadata, or the `projects/<id>/` catalog the entry lives in (same rule as `list_projects` / `get_project_context`).
- `index_document(path)` — embed + canary verify, updates index status and records content SHA-256 (`indexed_sha256`) to track post-index modifications.
- `index_batch(doc_ids?, project_id?, only_pending=false, only_modified=false)` — batch indexes multiple candidate documents (selected, pending, or modified) with individual per-file status reporting.
- `remove_from_knowledge(path)` — delete from rag_api, clears index.
- `get_index_status()` — returns counts of total, indexed, pending, failed, and modified-after-indexing documents, plus rag_api health.

#### How automatic project search works (`scope="auto"`)

1. LibreChat sends the conversation ID in the `X-Conversation-Id` request header (see `requestHeaders` above).
2. The server looks up that conversation in LibreChat's MongoDB (read-only, filtered by the authenticated user) and reads its `chatProjectId`.
3. The search is restricted to the indexed documents that belong to that project. Only their `file_id`s are sent to `rag_api`, so documents from other projects never reach the model.
4. If the conversation is confirmed to have **no** project, `auto` searches all of the user's indexed documents.
5. If the project **cannot be determined** (database unreachable, conversation not yet saved, missing or invalid header), the tool returns `project_resolution_failed` and does not query `rag_api`. It never widens the search silently.

A document belongs to a project when its catalog entry has that `project_id`, or, if it has none, when it is catalogued in `projects/<project_id>/`. The same rule is used by `list_projects`, `get_project_context` and `list_documentation`.

Example result in a project conversation:

```json
{"query": "deployment checklist", "scope": "project", "project_id": "<project_id>", "count": 3, "results": ["..."]}
```

Explicit scopes do not need project resolution: `scope="project"` with `project_id`, `scope="personal"` (documents with no project) and `scope="all"`.

### Publishing

- `publish_file(path, expires_in_days?)` — create/reuse public link, returns token + URL
- `unpublish_file(path_or_token)` — revoke link by token or path, including root-level files (file stays private)
- `get_public_link(path)` — active link for path
- `list_public_links()` — all links for user

### Project Context & Management

- `get_project_context()` — resolves the active LibreChat project for the current conversation via read-only MongoDB query (`conversations` -> `chatprojects`). Returns structured JSON with status (`project`, `no_project`, `conversation_unavailable`, `context_missing`, etc.), project documents catalog, and `summary_file` if present.
- `list_projects()` — lists authorized LibreChat projects from MongoDB combined with on-disk project directories, including document counts and indexing status. See [docs/mongodb-permissions-dokploy.md](docs/mongodb-permissions-dokploy.md) for database user configuration.

## Security Model

- **Identity**: `X-User-Id` header injected by LibreChat (`{{LIBRECHAT_USER_ID}}`). Placeholder unresolved → empty string → **fail-closed**.
- **Fail-closed**: Missing/empty/invalid header → HTTP 403 `{"error":"missing_user_identity"}` or `{"error":"invalid_user_identity"}`.
- **Path safety**: All paths are relative; absolute paths and `..` rejected; symlink escape detected via `Path.resolve()` + prefix check.
- **Isolation**: rag_api owner scope enforced by JWT `sub`/`id` = userId.
- **Public links**: Opaque `secrets.token_urlsafe(16)` tokens; no user/path in URL; revocation via `410 Gone`; lazy cleanup of expired entries.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Run tests
.venv/bin/pytest -q

# Lint
.venv/bin/ruff check .
```

## License

MIT
