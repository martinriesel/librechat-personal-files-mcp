# librechat-personal-files-mcp

MCP server providing per-user personal file storage, persistent documentation, RAG indexing/retrieval, and opaque public link publishing for LibreChat agents.

## Features

- **Per-user storage**: Private files under `/data/private/<userId>/` with full CRUD, list, move operations.
- **Documentation catalog**: `save_documentation` / `update_documentation` write to `docs/` and update `memory-index.json` (schema v2).
- **RAG integration**: `index_document` sends content to rag_api via `POST /embed` with canary verification; `search_knowledge` for semantic search; `remove_from_knowledge` for deletion.
- **Public publishing**: `publish_file` creates cryptographically random tokens (≥128 bits); files served via Nginx `X-Accel-Redirect` with `Content-Disposition: attachment`, `nosniff`, `no-store`.
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
                     │ Nginx        │
                     │ (X-Accel)    │
                     └──────────────┘
                            │
                            ▼
                     ┌──────────────┐
                     │ /data/private│  (read-only bind)
                     └──────────────┘
```

## Requirements

- A running [LibreChat](https://www.librechat.ai/) instance with `rag_api` enabled.
- A `JWT_SECRET` shared with LibreChat and `rag_api` (see below).
- Nginx as the reverse proxy in front of LibreChat (required for public links via `X-Accel-Redirect`).

## Installation

### 1. Get the Docker image

Pull the pre-built image from GHCR:

```bash
docker pull ghcr.io/martinriesel/librechat-personal-files-mcp:latest
```

Or build it yourself:

```bash
git clone https://github.com/martinriesel/librechat-personal-files-mcp.git
cd librechat-personal-files-mcp
docker build -t librechat-personal-files-mcp .
```

### 2. Prepare the host (run once, as root)

Create the data directory and a shared group. The container runs as UID `2000` / GID `2500`; Nginx (which serves published files) needs read access.

```bash
cd /path/to/your/librechat
groupadd -g 2500 lcfiles 2>/dev/null || true
mkdir -p ./data/private/_system
chown -R 2000:2500 ./data/private
chmod 2771 ./data/private          # group rwx + setgid; world executable so nginx can traverse
setfacl -R -m g:2500:rX,d:g:2500:rX ./data/private 2>/dev/null || \
  echo "ACL tools absent; using 644/755 fallback"
```

Ensure `JWT_SECRET` is set in your LibreChat `.env` (same value must be shared with `rag_api`):

```bash
grep -q '^JWT_SECRET=' .env || echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env
```

### 3. Add the service to your compose file

Add the blocks from `docker-compose.snippet.yml` to your `docker-compose.override.yml`:

- A `librechat-personal-files` service (image, environment, volumes, networks).
- An extra read-only volume `./data/private:/data/private:ro` on the `nginx` service.

Set `PUBLIC_BASE_URL` to your own public URL, e.g. `https://chat.example.com/files`.

Then start it:

```bash
docker compose up -d librechat-personal-files nginx
```

### 4. Configure the Nginx reverse proxy

Add these blocks to your existing Nginx server block (the same file LibreChat uses):

```nginx
limit_req_zone $binary_remote_addr zone=pubfiles:10m rate=5r/s;

location ^~ /files/ {
    limit_req zone=pubfiles burst=10 nodelay;
    limit_req_status 429;
    proxy_pass http://librechat-personal-files:8080/files/;
    proxy_set_header X-Original-URI $request_uri;
    proxy_set_header X-Real-IP $remote_addr;
}

location ^~ /_protected/ {
    internal;
    alias /data/private/;
}
```

Reload Nginx:

```bash
docker exec <nginx-container> nginx -s reload
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
```

Restart LibreChat. The `personal-files` server will be available to your agents.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USER_HEADER` | `X-User-Id` | Header carrying the user identity |
| `STORAGE_ROOT` | `/data/private` | Root directory for user data |
| `SHARE_ROOT` | `/data/share` | Legacy shared area (read/write during migration) |
| `RAG_API_URL` | `http://rag_api:8000` | rag_api endpoint |
| `JWT_SECRET` | *required* | HS256 secret shared with LibreChat/rag_api (≥32 chars) |
| `PUBLIC_BASE_URL` | `https://your-domain.example/files` | Base URL for public links |
| `MAX_FILE_SIZE_MB` | `20` | Max upload size |
| `REGISTRY_DB` | `/data/private/_system/links.db` | SQLite registry for public links |

## MCP Tools

### Storage

- `list_files(path="", recursive=false, pattern=null)` — list files/dirs
- `read_file(path)` — read UTF-8 text; error for binary or >2 MB
- `read_multiple_files(paths)` — read several text files at once
- `write_file(path, content)` — write text (UTF-8), creates parent dirs
- `update_file(path, content)` — update existing file
- `edit_file(path, edits)` — find/replace text (each `oldText` must match exactly once)
- `delete_file(path)` — delete file or directory
- `move_file(src, dst)` — move within user root
- `get_file_info(path)` — metadata + docindex + publishing status

### Documentation

- `save_documentation(filename, content, title?, description?, tags?, topics?)` — saves to `docs/`, updates index, **does not** publish or index
- `update_documentation(filename, content, ...)` — update existing doc
- `get_document_metadata(filename)` — full index entry

### RAG

- `search_knowledge(query, limit=8)` — semantic search (owner-scoped via JWT)
- `index_document(path)` — embed + canary verify, updates index status
- `remove_from_knowledge(path)` — delete from rag_api, clears index
- `get_index_status()` — counts + rag_api health

### Publishing

- `publish_file(path, expires_in_days?)` — create/reuse public link, returns token + URL
- `unpublish_file(path_or_token)` — revoke link (file stays private)
- `get_public_link(path)` — active link for path
- `list_public_links()` — all links for user

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
