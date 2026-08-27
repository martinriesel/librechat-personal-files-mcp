# librechat-personal-files-mcp

MCP server providing per-user personal file storage, persistent documentation, RAG indexing/retrieval, and opaque public link publishing for LibreChat agents.

## Features

- **Per-user storage**: Private files under `/data/private/<userId>/` with full CRUD, list, move operations.
- **Documentation catalog**: `save_documentation` / `update_documentation` write to `docs/` and update `memory-index.json` (schema v2).
- **RAG integration**: `index_document` sends content to rag_api via `POST /embed` with canary verification (issue #305); `search_knowledge` for semantic search; `remove_from_knowledge` for deletion.
- **Public publishing**: `publish_file` creates cryptographically random tokens (≥128 bits); files served via Nginx `X-Accel-Redirect` with `Content-Disposition: attachment`, `nosniff`, `no-store`.
- **Strict security**: Fail-closed on missing/invalid `X-User-Id`; path traversal blocked; no absolute paths; no `..` segments; symlink escape detection; reserved segment protection; atomic index writes with cross-process locking.

## Architecture

```
┌──────────────┐     ┌────────────────────────┐     ┌─────────────┐
│ LibreChat    │────▶│ librechat-personal-files-mcp │──▶│ rag_api     │
│ (Agent)      │ MCP │ (stateless HTTP /mcp)  │     │ (vector DB) │
└──────────────┘     └────────────────────────┘     └─────────────┘
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

## Quick Start (Development)

```bash
cd /opt/LibreChat/mcp-personal-files
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
# Run server
STORAGE_ROOT=/tmp/pf-private JWT_SECRET=$(openssl rand -hex 32) .venv/bin/personal-files-mcp
```

## Production Deployment

### 1. Host preparation (run once as root)

```bash
cd /opt/LibreChat
groupadd -g 2500 lcfiles 2>/dev/null || true
mkdir -p ./data/private/_system
chown -R 2000:2500 ./data/private
chmod 2770 ./data/private
setfacl -R -m g:2500:rX,d:g:2500:rX ./data/private 2>/dev/null || \
  echo "ACL tools absent; using 644/755 fallback"
grep -q '^JWT_SECRET=' .env || echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env
```

### 2. Add to `docker-compose.override.yml`

Copy the blocks from `docker-compose.snippet.yml` into your existing override file.

### 3. Configure LibreChat Admin Panel

Add under **MCP Settings**:

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

Restart LibreChat.

### 4. Nginx configuration (add to existing server block)

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

Reload Nginx.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USER_HEADER` | `X-User-Id` | Header carrying the user identity |
| `STORAGE_ROOT` | `/data/private` | Root directory for user data |
| `SHARE_ROOT` | `/data/share` | Legacy shared area (read/write during migration) |
| `RAG_API_URL` | `http://rag_api:8000` | rag_api endpoint |
| `JWT_SECRET` | *required* | HS256 secret shared with LibreChat/rag_api (≥32 chars) |
| `PUBLIC_BASE_URL` | `https://gpt.riesel.com.br/files` | Base URL for public links |
| `MAX_FILE_SIZE_MB` | `20` | Max upload size |
| `REGISTRY_DB` | `/data/private/_system/links.db` | SQLite registry for public links |

## MCP Tools

### Storage
- `list_files(path="", recursive=false, pattern=null)` — list files/dirs
- `read_file(path)` — read UTF-8 text; error for binary or >2 MB
- `write_file(path, content)` — write text (UTF-8), creates parent dirs
- `update_file(path, content)` — update existing file
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
- **Isolation**: rag_api owner scope enforced by JWT `sub`/`id` = userId (PR #319 merged 2026-08-15).
- **Public links**: Opaque `secrets.token_urlsafe(16)` tokens; no user/path in URL; revocation via `410 Gone`; lazy cleanup of expired entries.

## Development

```bash
# Run tests
.venv/bin/pytest -q

# Lint
.venv/bin/ruff check .

# Type check (optional)
.venv/bin/mypy src/personal_files_mcp  # if mypy added to deps
```

## License

MIT