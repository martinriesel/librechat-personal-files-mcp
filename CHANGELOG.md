# Changelog

All notable changes to `librechat-personal-files-mcp` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [1.5.0-test] — 2026-11-18

Fixes found while validating 1.4.0-test from LibreChat.

### Validated in a LibreChat deployment
- `/healthz` reported `version: "1.5.0"`, `status: "ok"`, `rag: "available"`, `storage: "ok"`.
- `search_knowledge` with `scope="auto"` and no `project_id`: each conversation resolved its own
  project (two projects tested).
- **Bidirectional isolation** with both projects holding indexed documents: a search from each project
  returned its own document and never the other project's document, compared by `rag_file_id`, not only
  by result text.
- The `asyncio.run() cannot be called from a running event loop` error did not reappear.

Not covered by that validation (neither passed nor failed): explicit `project_id` searches,
`documents_by_catalog` comparison between `list_projects` and `get_project_context`, multi-document
`index_batch`, and opening the same public link after `transfer_documentation`. These are covered by the
automated test suite only.

### Known limitations (still open)
- `project_id` passed by the model to `save_documentation` / `transfer_documentation` is not checked
  against the user's LibreChat projects.
- Public downloads check the path and then reopen it (check-then-open window); no rate limiting is
  applied by the server itself; expired links are only purged lazily.
- `reconcile_documentation` scans only the top level of `docs/` and `projects/<id>/`.
- Moving or deleting a whole directory does not update catalog entries of the files inside it.
- MCP tools read the identity from `X-User-Id` regardless of `USER_HEADER`.
- RAG network errors (e.g. DNS) are surfaced as raw errors instead of `rag_failed`.

This release was validated functionally; it is not a security audit.

### Fixed
- **`search_knowledge` failed with `asyncio.run() cannot be called from a running event loop`.**
  The MCP tool is async but called a sync helper that used `asyncio.run()`. `tools.search_knowledge`
  is now `async` and awaits the new `_arun_rag()`. `_run_rag()` remains for sync tools (they run in a
  worker thread) and now raises a clear error if misused from a running loop. Covered by tests that
  call the tool through the real HTTP → FastMCP transport (D035).
- **`search_knowledge(scope="auto")` no longer widens to all documents when project resolution
  fails.** It only searches everything when the resolver confirms `no_project`; any other status
  (`resolver_unavailable`, `conversation_unavailable`, `project_unavailable`, `context_invalid`,
  `context_missing`) returns `project_resolution_failed` with `resolver_status` and
  `retry_recommended`, without querying `rag_api`. Explicit `all`, `personal` or
  `project` + `project_id` still work without the resolver (D036).
- **Project document counts were inconsistent** (`list_projects` 5 vs `get_project_context` 6 for the
  same project). Every tool now uses one membership rule (`docindex.effective_project_id`): explicit
  `project_id` metadata, otherwise the `projects/<id>/` catalog the entry lives in, across the project,
  `docs/` and legacy root catalogs (D037).
- **`publish_file` / `unpublish_file` updated the legacy root catalog** (creating it as a side effect)
  instead of the document's real catalog. The public flag is now synced in whichever catalog holds the
  entry; non-document files no longer create catalog entries.
- **`unpublish_file` could not revoke root-level files by path** (paths without `/` were treated only as
  tokens). Tokens are tried first, then paths.
- **`transfer_documentation` always reported `public_link_preserved: false`.** It now reports whether
  an active link was actually moved; the dry run reports `public_link` from the link registry.

### Added
- `list_documentation` entries include `catalog` (`project` / `general` / `legacy`) and
  `effective_project_id`.
- `list_projects` and `get_project_context` include `documents_by_catalog`; `get_project_context`
  documents include `doc_id` and `catalog`. Projects referenced only by catalog metadata are listed
  with `source: "catalog"`.
- `tests/test_v150_regressions.py` (24 tests).

### Changed
- `search_knowledge(scope="personal")` and `list_documentation(unassigned_only=true)` exclude
  documents linked to a project by metadata, including those stored in `docs/`.
- Version reported by `pyproject.toml`, FastAPI and `/healthz` is now `1.5.0` (was `1.0.0`).

### Documentation
- **`CHANGELOG.md`**, `docs/mongodb-permissions-dokploy.md`, `docs/diagnostics-and-logs.md`, and
  DECISIONS.md D028–D037.

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
