"""Tests for Phase 3 Evolution: Multi-scope catalogs, protection, transfer, reconcile, and batch indexing."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_files_mcp import docindex, storage, tools

USER = "5f8d3a4b2c1e0d0001a2b3c4"


def _ok_payload(s: str) -> dict:
    assert not s.startswith("ERROR:"), s
    return json.loads(s)


def _err_payload(s: str) -> dict:
    assert s.startswith("ERROR:"), s
    return json.loads(s[len("ERROR: ") :])


# ---------------------------------------------------------------------------
# 1. Catalog Protection Tests
# ---------------------------------------------------------------------------


def test_generic_tools_reject_catalog_modification(cfg):
    # write_file
    res = tools.write_file(cfg, USER, "memory-index.json", "{}")
    assert _err_payload(res)["error"] == "protected_file"

    res = tools.write_file(cfg, USER, "docs/memory-index.json", "{}")
    assert _err_payload(res)["error"] == "protected_file"

    res = tools.write_file(cfg, USER, "projects/p1/memory-index.json", "{}")
    assert _err_payload(res)["error"] == "protected_file"

    # update_file
    res = tools.update_file(cfg, USER, "memory-index.json", "{}")
    assert _err_payload(res)["error"] == "protected_file"

    # edit_file
    res = tools.edit_file(cfg, USER, "memory-index.json", [{"oldText": "a", "newText": "b"}])
    assert _err_payload(res)["error"] == "protected_file"

    # delete_file
    res = tools.delete_file(cfg, USER, "memory-index.json")
    assert _err_payload(res)["error"] == "protected_file"

    res = tools.delete_file(cfg, USER, "projects/p1/memory-index.json")
    assert _err_payload(res)["error"] == "protected_file"

    # write_binary_file
    res = tools.write_binary_file(cfg, USER, "memory-index.json", "e30=")
    assert _err_payload(res)["error"] == "protected_file"

    # move_file (src or dst)
    res = tools.move_file(cfg, USER, "memory-index.json", "backup.json")
    assert _err_payload(res)["error"] == "protected_file"

    res = tools.move_file(cfg, USER, "some.json", "docs/memory-index.json")
    assert _err_payload(res)["error"] == "protected_file"


# ---------------------------------------------------------------------------
# 2. Scope Routing and Fail-Closed Behavior
# ---------------------------------------------------------------------------


def test_save_documentation_scope_routing_and_fail_closed(cfg):
    # 1. Project scope
    res = tools.save_documentation(
        cfg,
        USER,
        "spec.md",
        "# Project Spec",
        project_id="proj_alpha",
        project_name="Alpha Project",
    )
    p = _ok_payload(res)
    assert p["saved"] is True
    assert p["file"] == "projects/proj_alpha/spec.md"
    assert p["project_id"] == "proj_alpha"
    assert p["doc_id"].startswith("doc_")

    # Catalog is in projects/proj_alpha/
    p_cat = docindex.get_project_catalog(cfg, USER, "proj_alpha")
    doc = p_cat.get("spec.md")
    assert doc is not None
    assert doc.doc_id == p["doc_id"]
    assert doc.file == "projects/proj_alpha/spec.md"

    # 2. General scope (proven without project)
    res_gen = tools.save_documentation(
        cfg,
        USER,
        "general.md",
        "# General Knowledge",
    )
    p_gen = _ok_payload(res_gen)
    assert p_gen["file"] == "docs/general.md"
    g_cat = docindex.get_general_catalog(cfg, USER)
    assert g_cat.get("general.md") is not None

    # 3. Fail-closed when resolver fails
    for err_status in ("conversation_unavailable", "resolver_unavailable", "context_invalid"):
        res_fail = tools.save_documentation(
            cfg,
            USER,
            "blocked.md",
            "# Blocked",
            resolver_status=err_status,
        )
        assert _err_payload(res_fail)["error"] == "project_resolution_failed"


def test_save_documentation_preserves_doc_id_on_overwrite(cfg):
    res1 = tools.save_documentation(cfg, USER, "note.md", "# V1", title="V1")
    p1 = _ok_payload(res1)
    doc_id1 = p1["doc_id"]

    res2 = tools.save_documentation(cfg, USER, "note.md", "# V2", title="V2")
    p2 = _ok_payload(res2)
    assert p2["doc_id"] == doc_id1


# ---------------------------------------------------------------------------
# 3. Update Documentation Preserves Existing Association
# ---------------------------------------------------------------------------


def test_update_documentation_preserves_association(cfg):
    # Create in project p1
    tools.save_documentation(
        cfg, USER, "guide.md", "# V1", project_id="proj_1", project_name="P1"
    )

    # Update without specifying project
    res_up = tools.update_documentation(cfg, USER, "guide.md", "# V2 Updated")
    p_up = _ok_payload(res_up)
    assert p_up["file"] == "projects/proj_1/guide.md"
    assert p_up["project_id"] == "proj_1"

    doc, cat = docindex.find_document_across_catalogs(cfg, USER, "guide.md")
    assert doc is not None
    assert doc.project_id == "proj_1"
    assert cat.path == docindex.get_project_catalog(cfg, USER, "proj_1").path


# ---------------------------------------------------------------------------
# 4. Register Documentation Tool
# ---------------------------------------------------------------------------


def test_register_documentation(cfg):
    # Write file on disk outside documentation tools
    storage.write(cfg, USER, "docs/existing.md", b"# Extracted Title\nSome content description.")

    res = tools.register_documentation(cfg, USER, "docs/existing.md")
    p = _ok_payload(res)
    assert p["registered"] is True
    assert p["file"] == "docs/existing.md"
    assert p["title"] == "Extracted Title"
    assert p["doc_id"].startswith("doc_")

    # Content on disk was not modified or rewritten
    data = storage.read(cfg, USER, "docs/existing.md").decode("utf-8")
    assert data == "# Extracted Title\nSome content description."

    # Non-existent or non-markdown rejects
    err1 = tools.register_documentation(cfg, USER, "docs/nonexistent.md")
    assert _err_payload(err1)["error"] == "not_found"

    err2 = tools.register_documentation(cfg, USER, "image.png")
    assert _err_payload(err2)["error"] == "invalid_type"


# ---------------------------------------------------------------------------
# 5. Transfer Documentation Tool
# ---------------------------------------------------------------------------


def test_transfer_documentation_dry_run_and_execution(cfg):
    # Setup document in proj_1
    tools.save_documentation(
        cfg, USER, "runbook.md", "# Runbook Content", project_id="proj_1"
    )

    # Dry run to proj_2
    res_dry = tools.transfer_documentation(
        cfg, USER, "runbook.md", target_project_id="proj_2", dry_run=True
    )
    p_dry = _ok_payload(res_dry)
    assert p_dry["dry_run"] is True
    assert p_dry["can_transfer"] is True
    assert p_dry["source_file"] == "projects/proj_1/runbook.md"
    assert p_dry["target_file"] == "projects/proj_2/runbook.md"

    # File still in proj_1
    doc1, _ = docindex.find_document_across_catalogs(cfg, USER, "projects/proj_1/runbook.md")
    assert doc1 is not None

    # Actual transfer to proj_2
    res_act = tools.transfer_documentation(
        cfg, USER, "runbook.md", target_project_id="proj_2", dry_run=False
    )
    p_act = _ok_payload(res_act)
    assert p_act["transferred"] is True
    assert p_act["source_file"] == "projects/proj_1/runbook.md"
    assert p_act["target_file"] == "projects/proj_2/runbook.md"
    assert p_act["doc_id"] == doc1.doc_id

    # Old location removed from catalog and disk
    old_doc, _ = docindex.find_document_across_catalogs(cfg, USER, "projects/proj_1/runbook.md")
    assert old_doc is None

    # New location exists in catalog and disk
    new_doc, _ = docindex.find_document_across_catalogs(cfg, USER, "projects/proj_2/runbook.md")
    assert new_doc is not None
    assert new_doc.project_id == "proj_2"

    # Collision test: target already exists
    storage.write(cfg, USER, "projects/proj_1/runbook.md", b"already here")
    res_coll = tools.transfer_documentation(
        cfg, USER, "projects/proj_2/runbook.md", target_project_id="proj_1"
    )
    assert _err_payload(res_coll)["error"] == "collision_detected"

    # Same destination test
    res_same = tools.transfer_documentation(
        cfg, USER, "projects/proj_2/runbook.md", target_project_id="proj_2"
    )
    assert _err_payload(res_same)["error"] == "same_destination"


def test_transfer_preserves_public_link(cfg):
    from personal_files_mcp import app, publishing

    settings = app.AppSettings(
        storage_root=str(cfg.storage_root),
        registry_db=str(cfg.storage_root / "public_links.db"),
        public_base_url="https://gpt.riesel.com.br",
    )
    app_ctx = app.AppContext(settings=settings, storage_cfg=cfg)
    tools.bind_publish_ctx(app_ctx)

    # Save and publish document in docs/
    tools.save_documentation(cfg, USER, "public_doc.md", "# Public Doc")
    pub_res = tools.publish_file(cfg, USER, "docs/public_doc.md")
    token = _ok_payload(pub_res)["token"]

    # Transfer to project
    res_tr = tools.transfer_documentation(
        cfg, USER, "docs/public_doc.md", target_project_id="proj_99"
    )
    assert _ok_payload(res_tr)["transferred"] is True

    # Check publishing link still resolves with the same token but new path!
    reg = publishing.get_registry(app_ctx)
    record = reg.get(token)
    assert record is not None
    assert record.relative_path == "projects/proj_99/public_doc.md"


# ---------------------------------------------------------------------------
# 6. Reconcile Documentation Tool
# ---------------------------------------------------------------------------


def test_reconcile_documentation(cfg):
    # 1. Create unregistered file
    storage.write(cfg, USER, "docs/unreg.md", b"# Unregistered")

    # 2. Create orphaned catalog entry
    cat = docindex.get_general_catalog(cfg, USER)
    cat.upsert(docindex.DocumentEntry(file="docs/ghost.md", title="Ghost"))

    # Reconcile without repair
    rep_dry = tools.reconcile_documentation(cfg, USER, repair=False)
    p_dry = _ok_payload(rep_dry)
    assert p_dry["unregistered_count"] == 1
    assert "docs/unreg.md" in p_dry["unregistered_files"]
    assert p_dry["orphaned_count"] == 1
    assert p_dry["repaired_unregistered"] == 0
    assert p_dry["repaired_orphans"] == 0

    # Reconcile with repair
    rep_act = tools.reconcile_documentation(cfg, USER, repair=True)
    p_act = _ok_payload(rep_act)
    assert p_act["repaired_unregistered"] == 1
    assert p_act["repaired_orphans"] == 1

    # Verify ghost removed and unreg registered
    assert cat.get("ghost.md") is None
    assert cat.get("unreg.md") is not None


# ---------------------------------------------------------------------------
# 7. Post-Index Modification Detection & Batch Indexing
# ---------------------------------------------------------------------------


def test_modified_since_indexing_and_batch_indexing(cfg, monkeypatch):
    from personal_files_mcp import app

    settings = app.AppSettings(
        storage_root=str(cfg.storage_root),
        jwt_secret="a-very-long-secret-key-that-is-at-least-32-chars-long",
    )
    app_ctx = app.AppContext(settings=settings, storage_cfg=cfg)
    tools.bind_publish_ctx(app_ctx)

    # Mock RAG client
    def mock_run_rag(ctx, coro_factory):
        mock_client = MagicMock()
        mock_client.embed = AsyncMock(return_value=None)
        mock_client.canary = AsyncMock(return_value=(True, None))
        mock_client.health = AsyncMock(return_value={"status": "ok"})

        import asyncio

        return asyncio.run(coro_factory(mock_client))

    monkeypatch.setattr(tools, "_run_rag", mock_run_rag)

    # Save document and index it
    tools.save_documentation(cfg, USER, "doc_rag.md", "# Original Content")
    tools.index_document(cfg, USER, "docs/doc_rag.md")

    # Status: not modified
    meta = _ok_payload(tools.get_document_metadata(cfg, USER, "doc_rag.md"))
    assert meta["rag_indexed"] is True
    assert meta["modified_since_indexing"] is False

    # Modify file on disk
    storage.update(cfg, USER, "docs/doc_rag.md", b"# Modified Content That Changed")

    # Metadata and index status report modified
    meta_mod = _ok_payload(tools.get_document_metadata(cfg, USER, "doc_rag.md"))
    assert meta_mod["modified_since_indexing"] is True

    status_mod = _ok_payload(tools.get_index_status(cfg, USER))
    assert status_mod["documents_modified_since_indexing"] == 1

    # Batch index only modified files
    batch_res = tools.index_batch(cfg, USER, only_modified=True)
    b_payload = _ok_payload(batch_res)
    assert b_payload["total_candidates"] == 1
    assert b_payload["successful_count"] == 1
    assert b_payload["results"][0]["status"] == "indexed"

    # Status back to not modified
    meta_reindexed = _ok_payload(tools.get_document_metadata(cfg, USER, "doc_rag.md"))
    assert meta_reindexed["modified_since_indexing"] is False


# ---------------------------------------------------------------------------
# 8. List Projects Tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_projects(cfg):
    from personal_files_mcp import app

    settings = app.AppSettings(storage_root=str(cfg.storage_root))
    resolver = MagicMock()

    async def fake_list_user_projects(s, uid):
        return [
            {"id": "mongo_proj_1", "name": "Locaweb", "description": "Locaweb Cloud"},
        ]

    resolver.list_user_projects = AsyncMock(side_effect=fake_list_user_projects)

    app_ctx = app.AppContext(
        settings=settings, storage_cfg=cfg, project_resolver=resolver
    )

    # Save a document into mongo_proj_1
    tools.save_documentation(cfg, USER, "plan.md", "# Plan", project_id="mongo_proj_1")

    # Also create an on-disk project directory
    tools.save_documentation(cfg, USER, "disk_doc.md", "# Disk", project_id="disk_proj_2")

    res = await tools.list_projects(app_ctx, USER)
    payload = _ok_payload(res)

    assert payload["count"] == 2
    proj_map = {p["id"]: p for p in payload["projects"]}

    assert "mongo_proj_1" in proj_map
    assert proj_map["mongo_proj_1"]["name"] == "Locaweb"
    assert proj_map["mongo_proj_1"]["documents_count"] == 1

    assert "disk_proj_2" in proj_map
    assert proj_map["disk_proj_2"]["documents_count"] == 1
