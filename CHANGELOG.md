# Changelog

All notable changes to `librechat-personal-files-mcp` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- **`CHANGELOG.md`** — this file.
- **Diagnostic & deployment docs** — `docs/mongodb-permissions-dokploy.md`, `docs/diagnostics-and-logs.md`.
- **Technical decisions** — DECISIONS.md entries D028–D034 covering multi-scope catalogs, fail-closed routing, catalog protection, stable `doc_id`, transfer & reconciliation, batch indexing, and `list_projects`.

---

## [1.4.0-test] — 2026-11-17

### Added
- **Multi-scope documentation catalog** — general (`docs/`) and per-project (`projects/<project_id>/`) catalogs, each with its own `memory-index.json`.
- **Catalog protection** — LLM-facing tools (`write_file`, `update_file`, `delete_file`, `move_file`, `edit_file`, `write_binary_file`) reject operations on `memory-index.json` and `.lock` files with error code `protected_file`.
- **Stable document identifiers** — every catalog entry gets a persistent `doc_id` (`doc_<uuid_hex12>`) that survives overwrites and transfers.
- **`register_documentation`** — register existing `.md`/`.txt` files into the catalog without rewriting their contents.
- **`transfer_documentation`** — move a document between general and project scopes (or between projects) with `dry_run`, collision detection, `doc_id` preservation, public-link path update, and RAG status retention.
- **`reconcile_documentation`** — scan for unregistered files, orphaned catalog entries, and content modified since last indexing; repair only when `repair=true`.
- **`index_batch`** — batch-index multiple documents with `doc_ids`, `project_id`, `only_pending`, `only_modified` filters and per-file status reporting.
- **`list_projects`** — list authorized LibreChat projects from MongoDB + on-disk directories, with document counts and indexing stats.
- **`indexed_sha256` tracking** — `index_document` and `index_batch` record content SHA-256 at index time; `modified_since_indexing` is reported in metadata and list views.
- **`list_documentation` `unassigned_only` filter** — list documents not associated with any project.
- **`LinkRegistry.update_path()`** — update public-link registry path during transfer without breaking existing tokens/URLs.

### Changed
- **`save_documentation`** — auto-routes to `projects/<project_id>/` for project conversations, `docs/` for conversations with no project. **Fail-closed**: resolver errors return `project_resolution_failed` instead of silently falling back to `docs/`.
- **`update_documentation`** — searches across all catalogs via `find_document_across_catalogs`; preserves existing project association (never re-routes).
- **`get_document_metadata`** — searches across catalogs by `doc_id`, relative path, or basename.
- **`list_documentation`** — aggregates entries from all catalogs; reports `doc_id` and `modified_since_indexing`.
- **`index_document`** — records `indexed_sha256` for post-index change detection.
- **`search_knowledge`** — auto-resolves project context and scopes search to active project.
- **`get_project_context`** — includes project document catalog and `summary_file` from multi-scope catalogs.
- **`get_index_status`** — reports `documents_modified_since_indexing` count.
- **README.md** — updated with all new tools, multi-scope catalog description, MongoDB setup reference, `requestHeaders` configuration.
- **DECISIONS.md** — added D028–D034.

### Fixed
- `_maybe_register_doc` correctly routes to project catalog for files under `projects/`.
- `DocIndex.get()` now matches by basename when no path prefix is given.

---

## [1.3.0-test] — 2026-11-16

### Added
- **Multi-scope catalog architecture** — `docs/memory-index.json` for general docs, `projects/<project_id>/memory-index.json` per project.
- Fail-closed resolver logic in `app.py` and `tools.py`.
- 10 new test functions in `tests/test_evolution_phase3.py` (catalog protection, scope routing, transfer, reconciliation, batch indexing, list_projects).

### Changed
- `DocumentEntry` extended with `doc_id`, `indexed_sha256`, `created_at`, `updated_at`.
- `DocIndex.__init__` accepts `scope_dir` parameter.
- `tools.py` grew from ~1010 to ~1600 lines with multi-scope support.

---

## [1.2.0-test] — 2026-11-15

### Added
- **Project-scoped RAG isolation** (Fase 5) — `search_knowledge` restricts candidates to active project's indexed files in `scope=auto|project`.
- **Project memory catalog schema v3** (Fase 4) — `project_id`, `project_name`, `conversation_id` on `DocumentEntry`, backward-compatible loading.
- `list_documentation` MCP tool.

---

## [1.1.0-test] — 2026-11-14

### Fixed
- Removed unexpected method argument from `FileResponse` in publishing module.
- Ruff linting errors and import sorting.

### Added
- **Project context discovery** (Fase 3) — `get_project_context` tool, `ContextVar[RequestContextData]`, MongoDB resolver with micro-retry.
- Structured diagnostics via `PERSONAL_FILES_CONTEXT_DIAGNOSTICS`.

---

## [1.0.0] — Initial release

### Added
- Per-user private storage with CRUD, list, move, and binary support.
- Documentation catalog (`memory-index.json` schema v2).
- RAG integration with canary verification.
- Public link publishing with opaque tokens, streaming `FileResponse`, and HTTP Range support.
- `POST /binary` endpoint for service-to-service uploads.
- Stateless HTTP MCP at `/mcp`.
- Identity middleware with fail-closed `X-User-Id` enforcement.
- Path safety (no `..`, no absolute paths, symlink escape detection).
