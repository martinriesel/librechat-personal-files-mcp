"""Regression tests for 1.5.0-test.

Covers the issues found while validating 1.4.0-test from LibreChat:

1. ``search_knowledge`` failed with ``asyncio.run() cannot be called from a
   running event loop`` when called through the real MCP transport.
2. ``scope="auto"`` silently widened to ``all`` when project resolution failed.
3. ``list_projects`` and ``get_project_context`` counted project documents
   differently (project catalog only vs. project + legacy root catalog).
4. ``publish``/``unpublish`` synced the public flag into the legacy root
   catalog instead of the document's real catalog; root-level paths could not
   be unpublished by path.

The transport tests drive the full stack: HTTP -> IdentityMiddleware ->
FastMCP streamable-http (stateless) -> tool -> rag_api (mocked with respx).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from personal_files_mcp import docindex, publishing, storage, tools
from personal_files_mcp.app import AppContext, create_app
from personal_files_mcp.config import AppSettings
from personal_files_mcp.project_resolver import ProjectResolutionResult

USER = "5f8d3a4b2c1e0d0001a2b3c4"
OTHER_USER = "bbbbbbbbbbbbbbbbbbbbbbbb"
PROJ_A = "aaaaaaaaaaaaaaaaaaaaaa01"
PROJ_B = "aaaaaaaaaaaaaaaaaaaaaa02"
CONV = "00000000-0000-4000-8000-000000000001"
RAG = "http://rag.test"


def _ok(s: str) -> dict:
    assert not s.startswith("ERROR:"), s
    return json.loads(s)


def _err(s: str) -> dict:
    assert s.startswith("ERROR:"), s
    return json.loads(s[len("ERROR: ") :])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        storage_root=str(tmp_path / "private"),
        rag_api_url=RAG,
        jwt_secret="x" * 64,
        public_base_url="https://example.test/files",
        registry_db=str(tmp_path / "private" / "_system" / "links.db"),
    )


def _resolver(status: str, project_id: str | None = None, retry: bool = False):
    r = MagicMock()
    r.resolve = AsyncMock(
        return_value=ProjectResolutionResult(
            status=status,
            project_id=project_id,
            project_name="Project Alpha" if project_id else None,
            retry_recommended=retry,
        )
    )
    r.list_user_projects = AsyncMock(return_value=[])
    return r


def _make_ctx(settings: AppSettings, resolver=None) -> AppContext:
    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    return AppContext(
        settings=settings,
        storage_cfg=storage.StorageConfig(storage_root=Path(settings.storage_root)),
        project_resolver=resolver,
    )


def _seed(cfg: storage.StorageConfig) -> None:
    """Indexed docs: one per project, one general, one legacy-root linked to A."""
    storage.write(cfg, USER, f"projects/{PROJ_A}/a.md", b"# A\nalpha")
    docindex.get_project_catalog(cfg, USER, PROJ_A).upsert(
        docindex.DocumentEntry(
            file=f"projects/{PROJ_A}/a.md",
            project_id=PROJ_A,
            indexing_status="indexed",
            rag_file_id="rag-a",
        )
    )
    storage.write(cfg, USER, f"projects/{PROJ_B}/b.md", b"# B\nbeta")
    docindex.get_project_catalog(cfg, USER, PROJ_B).upsert(
        docindex.DocumentEntry(
            file=f"projects/{PROJ_B}/b.md",
            project_id=PROJ_B,
            indexing_status="indexed",
            rag_file_id="rag-b",
        )
    )
    storage.write(cfg, USER, "docs/g.md", b"# G\ngeneral")
    docindex.get_general_catalog(cfg, USER).upsert(
        docindex.DocumentEntry(
            file="docs/g.md", indexing_status="indexed", rag_file_id="rag-g"
        )
    )
    # Pre-routing document: lives in docs/ but registered in the legacy root
    # catalog with project A metadata (the "6th document" seen in production).
    storage.write(cfg, USER, "docs/legacy-a.md", b"# Legacy\nold")
    docindex.DocIndex(cfg, USER, scope_dir=None).upsert(
        docindex.DocumentEntry(
            file="docs/legacy-a.md",
            project_id=PROJ_A,
            indexing_status="indexed",
            rag_file_id="rag-legacy-a",
        )
    )


def _mcp_call(client: TestClient, name: str, arguments: dict, headers: dict) -> dict:
    resp = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **headers,
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    if "text/event-stream" in resp.headers.get("content-type", ""):
        data_lines = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
        body = data_lines[-1]
    return json.loads(body)["result"]


def _text(result: dict) -> str:
    return result["content"][0]["text"]


@pytest.fixture
def rag_mock():
    captured: list[dict] = []

    def _query_multiple(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=[[{"page_content": "hit", "metadata": {}}, 0.1]])

    with respx.mock(assert_all_called=False) as m:
        m.post(f"{RAG}/query_multiple").mock(side_effect=_query_multiple)
        m.route(host="testserver").pass_through()
        yield captured


def _headers(conv: str | None = CONV) -> dict:
    h = {"X-User-Id": USER}
    if conv:
        h["X-Conversation-Id"] = conv
    return h


# ---------------------------------------------------------------------------
# 1 + 2. search_knowledge through the real MCP transport
# ---------------------------------------------------------------------------


def test_search_via_transport_project_conversation(settings, rag_mock):
    ctx = _make_ctx(settings, _resolver("project", PROJ_A))
    _seed(ctx.storage_cfg)
    with TestClient(create_app(ctx)) as client:
        result = _mcp_call(client, "search_knowledge", {"query": "alpha"}, _headers())
    assert not result.get("isError"), result
    payload = _ok(_text(result))
    assert payload["scope"] == "project"
    assert payload["project_id"] == PROJ_A
    assert payload["count"] == 1
    # Project A docs from the project catalog AND the legacy root catalog only
    assert sorted(rag_mock[0]["file_ids"]) == ["rag-a", "rag-legacy-a"]


def test_search_via_transport_no_project_searches_all(settings, rag_mock):
    ctx = _make_ctx(settings, _resolver("no_project"))
    _seed(ctx.storage_cfg)
    with TestClient(create_app(ctx)) as client:
        result = _mcp_call(client, "search_knowledge", {"query": "x"}, _headers())
    payload = _ok(_text(result))
    assert payload["scope"] == "all"
    assert sorted(rag_mock[0]["file_ids"]) == ["rag-a", "rag-b", "rag-g", "rag-legacy-a"]


@pytest.mark.parametrize(
    "status",
    ["resolver_unavailable", "conversation_unavailable", "project_unavailable", "context_invalid"],
)
def test_search_auto_fails_closed_when_resolution_fails(settings, rag_mock, status):
    ctx = _make_ctx(settings, _resolver(status, retry=status == "conversation_unavailable"))
    _seed(ctx.storage_cfg)
    with TestClient(create_app(ctx)) as client:
        result = _mcp_call(client, "search_knowledge", {"query": "x"}, _headers())
    err = _err(_text(result))
    assert err["error"] == "project_resolution_failed"
    assert err["resolver_status"] == status
    assert rag_mock == []  # rag_api never queried


def test_search_auto_fails_closed_without_conversation_header(settings, rag_mock):
    resolver = _resolver("context_missing")
    ctx = _make_ctx(settings, resolver)
    _seed(ctx.storage_cfg)
    with TestClient(create_app(ctx)) as client:
        result = _mcp_call(client, "search_knowledge", {"query": "x"}, _headers(conv=None))
    assert _err(_text(result))["resolver_status"] == "context_missing"
    assert rag_mock == []


def test_search_auto_fails_closed_without_resolver(settings, rag_mock):
    ctx = _make_ctx(settings, resolver=None)
    _seed(ctx.storage_cfg)
    with TestClient(create_app(ctx)) as client:
        result = _mcp_call(client, "search_knowledge", {"query": "x"}, _headers())
    assert _err(_text(result))["resolver_status"] == "resolver_unavailable"
    assert rag_mock == []


def test_search_explicit_scopes_do_not_depend_on_resolver(settings, rag_mock):
    resolver = _resolver("resolver_unavailable")
    ctx = _make_ctx(settings, resolver)
    _seed(ctx.storage_cfg)
    with TestClient(create_app(ctx)) as client:
        all_res = _ok(_text(_mcp_call(client, "search_knowledge", {"query": "x", "scope": "all"}, _headers())))
        pers = _ok(_text(_mcp_call(client, "search_knowledge", {"query": "x", "scope": "personal"}, _headers())))
        proj_b = _ok(
            _text(
                _mcp_call(
                    client,
                    "search_knowledge",
                    {"query": "x", "scope": "project", "project_id": PROJ_B},
                    _headers(),
                )
            )
        )
    assert all_res["scope"] == "all"
    assert pers["scope"] == "personal"
    assert proj_b["project_id"] == PROJ_B
    assert sorted(rag_mock[0]["file_ids"]) == ["rag-a", "rag-b", "rag-g", "rag-legacy-a"]
    assert rag_mock[1]["file_ids"] == ["rag-g"]  # personal = no effective project
    assert rag_mock[2]["file_ids"] == ["rag-b"]
    # 'all'/'personal'/explicit project never consult the resolver
    resolver.resolve.assert_not_called()


def test_search_scope_project_with_failed_resolution(settings, rag_mock):
    ctx = _make_ctx(settings, _resolver("resolver_unavailable"))
    _seed(ctx.storage_cfg)
    with TestClient(create_app(ctx)) as client:
        result = _mcp_call(client, "search_knowledge", {"query": "x", "scope": "project"}, _headers())
    err = _err(_text(result))
    assert err["error"] == "missing_project"
    assert err["resolver_status"] == "resolver_unavailable"
    assert rag_mock == []


def test_search_rag_error_is_reported_not_swallowed(settings):
    ctx = _make_ctx(settings, _resolver("project", PROJ_A))
    _seed(ctx.storage_cfg)
    with respx.mock(assert_all_called=False) as m:
        m.post(f"{RAG}/query_multiple").mock(return_value=httpx.Response(401))
        m.route(host="testserver").pass_through()
        with TestClient(create_app(ctx)) as client:
            result = _mcp_call(client, "search_knowledge", {"query": "x"}, _headers())
    assert _err(_text(result))["error"] == "rag_failed"


def test_sync_rag_tool_still_works_via_transport(settings):
    """Sync tools run in a worker thread; their private event loop must work."""
    ctx = _make_ctx(settings, _resolver("project", PROJ_A))
    with respx.mock(assert_all_called=False) as m:
        m.get(f"{RAG}/health").mock(return_value=httpx.Response(200, json={"status": "UP"}))
        m.route(host="testserver").pass_through()
        with TestClient(create_app(ctx)) as client:
            result = _mcp_call(client, "get_index_status", {}, _headers())
    payload = _ok(_text(result))
    assert payload["rag_health"] == {"status": "UP"}


def test_run_rag_refuses_running_loop(settings):
    import asyncio

    ctx = _make_ctx(settings)

    async def inner():
        with pytest.raises(RuntimeError, match="_arun_rag"):
            tools._run_rag(ctx, AsyncMock())

    asyncio.run(inner())


# ---------------------------------------------------------------------------
# 3. Consistent project membership across tools
# ---------------------------------------------------------------------------


async def test_list_projects_and_context_counts_match(settings):
    resolver = _resolver("project", PROJ_A)
    resolver.list_user_projects = AsyncMock(
        return_value=[{"id": PROJ_A, "name": "Project Alpha", "description": ""}]
    )
    ctx = _make_ctx(settings, resolver)
    cfg = ctx.storage_cfg
    _seed(cfg)
    # A doc in docs/ catalog explicitly linked to project A
    storage.write(cfg, USER, "docs/linked-a.md", b"# L")
    docindex.get_general_catalog(cfg, USER).upsert(
        docindex.DocumentEntry(file="docs/linked-a.md", project_id=PROJ_A)
    )

    projects = _ok(await tools.list_projects(ctx, USER))
    proj_a = next(p for p in projects["projects"] if p["id"] == PROJ_A)
    context = _ok(await tools.get_project_context(ctx, USER, conversation_id=CONV))
    listed = _ok(tools.list_documentation(cfg, USER, project_id=PROJ_A))

    assert proj_a["documents_count"] == 3
    assert context["project"]["documents_count"] == 3
    assert listed["count"] == 3
    assert proj_a["documents_by_catalog"] == {"project": 1, "legacy": 1, "general": 1}
    assert context["project"]["documents_by_catalog"] == proj_a["documents_by_catalog"]
    catalogs = {d["file"]: d["catalog"] for d in context["project"]["documents"]}
    assert catalogs["docs/legacy-a.md"] == "legacy"
    assert catalogs["docs/linked-a.md"] == "general"

    # Linked docs are no longer reported as "personal"
    personal = _ok(tools.list_documentation(cfg, USER, unassigned_only=True))
    assert [d["file"] for d in personal["documents"]] == ["docs/g.md"]


async def test_list_projects_includes_catalog_only_projects(settings):
    ctx = _make_ctx(settings, _resolver("no_project"))
    cfg = ctx.storage_cfg
    storage.write(cfg, USER, "docs/x.md", b"# X")
    docindex.get_general_catalog(cfg, USER).upsert(
        docindex.DocumentEntry(file="docs/x.md", project_id="orphan_proj", project_name="Orphan")
    )
    projects = _ok(await tools.list_projects(ctx, USER))
    orphan = next(p for p in projects["projects"] if p["id"] == "orphan_proj")
    assert orphan["source"] == "catalog"
    assert orphan["name"] == "Orphan"
    assert orphan["documents_count"] == 1


def test_project_catalog_entry_without_project_id_belongs_to_folder(settings):
    ctx = _make_ctx(settings)
    cfg = ctx.storage_cfg
    storage.write(cfg, USER, f"projects/{PROJ_A}/manual.md", b"# M")
    docindex.get_project_catalog(cfg, USER, PROJ_A).upsert(
        docindex.DocumentEntry(file=f"projects/{PROJ_A}/manual.md")
    )
    assert [d.file for d in docindex.list_all_documents(cfg, USER, project_id=PROJ_A)] == [
        f"projects/{PROJ_A}/manual.md"
    ]
    assert docindex.list_all_documents(cfg, USER, unassigned_only=True) == []


# ---------------------------------------------------------------------------
# 4. Publishing syncs the right catalog
# ---------------------------------------------------------------------------


def _bound_ctx(settings) -> AppContext:
    ctx = _make_ctx(settings)
    tools.bind_publish_ctx(ctx)
    return ctx


def test_publish_marks_project_catalog_and_not_root(settings):
    ctx = _bound_ctx(settings)
    cfg = ctx.storage_cfg
    tools.save_documentation(cfg, USER, "spec.md", "# Spec", project_id=PROJ_A)
    rel = f"projects/{PROJ_A}/spec.md"

    token = _ok(tools.publish_file(cfg, USER, rel))["token"]
    entry = docindex.get_project_catalog(cfg, USER, PROJ_A).get(rel)
    assert entry.public is True
    assert entry.public_token == token
    # No legacy root catalog is created as a side effect
    assert not docindex.DocIndex(cfg, USER, scope_dir=None).path.exists()

    assert _ok(tools.unpublish_file(cfg, USER, rel))["revoked"] == rel
    entry = docindex.get_project_catalog(cfg, USER, PROJ_A).get(rel)
    assert entry.public is False
    assert entry.public_token is None


def test_unpublish_by_token_clears_general_catalog(settings):
    ctx = _bound_ctx(settings)
    cfg = ctx.storage_cfg
    tools.save_documentation(cfg, USER, "g.md", "# G")
    token = _ok(tools.publish_file(cfg, USER, "docs/g.md"))["token"]
    assert docindex.get_general_catalog(cfg, USER).get("docs/g.md").public is True
    _ok(tools.unpublish_file(cfg, USER, token))
    assert docindex.get_general_catalog(cfg, USER).get("docs/g.md").public is False


def test_publish_non_document_creates_no_catalog(settings):
    ctx = _bound_ctx(settings)
    cfg = ctx.storage_cfg
    storage.write(cfg, USER, "report.pdf", b"%PDF-1.4")
    _ok(tools.publish_file(cfg, USER, "report.pdf"))
    assert not docindex.DocIndex(cfg, USER, scope_dir=None).path.exists()
    # Root-level file can be unpublished by path
    assert _ok(tools.unpublish_file(cfg, USER, "report.pdf"))["revoked"] == "report.pdf"
    rec = publishing.get_registry(ctx).get_for_user(USER, "report.pdf")
    assert rec is None  # no active link left


def test_transfer_reports_link_preserved_and_keeps_flag(settings):
    ctx = _bound_ctx(settings)
    cfg = ctx.storage_cfg
    tools.save_documentation(cfg, USER, "pub.md", "# Pub")
    token = _ok(tools.publish_file(cfg, USER, "docs/pub.md"))["token"]

    dry = _ok(tools.transfer_documentation(cfg, USER, "docs/pub.md", PROJ_A, dry_run=True))
    assert dry["public_link"] is True

    res = _ok(tools.transfer_documentation(cfg, USER, "docs/pub.md", PROJ_A))
    assert res["public_link_preserved"] is True
    new_rel = f"projects/{PROJ_A}/pub.md"
    entry = docindex.get_project_catalog(cfg, USER, PROJ_A).get(new_rel)
    assert entry.public is True and entry.public_token == token
    rec = publishing.get_registry(ctx).get(token)
    assert rec.relative_path == new_rel and not rec.is_revoked()


def test_transfer_unpublished_reports_no_link(settings):
    ctx = _bound_ctx(settings)
    cfg = ctx.storage_cfg
    tools.save_documentation(cfg, USER, "priv.md", "# Priv")
    res = _ok(tools.transfer_documentation(cfg, USER, "docs/priv.md", PROJ_A))
    assert res["public_link_preserved"] is False


def test_unpublish_other_users_token_is_rejected(settings):
    ctx = _bound_ctx(settings)
    cfg = ctx.storage_cfg
    tools.save_documentation(cfg, USER, "mine.md", "# Mine")
    token = _ok(tools.publish_file(cfg, USER, "docs/mine.md"))["token"]
    assert _err(tools.unpublish_file(cfg, OTHER_USER, token))["error"] == "not_found"
    assert docindex.get_general_catalog(cfg, USER).get("docs/mine.md").public is True
