"""memory-index.json tests: atomicity, lock, schema."""
from __future__ import annotations

import json
import threading

import pytest

from personal_files_mcp import docindex, storage

USER = "5f8d3a4b2c1e0d0001a2b3c4"


def test_index_created_empty_when_missing(cfg):
    idx = docindex.new_docindex(cfg, USER)
    assert idx.list_documents() == []


def test_upsert_and_get(cfg):
    idx = docindex.new_docindex(cfg, USER)
    entry = docindex.DocumentEntry(file="docs/a.md", title="A")
    idx.upsert(entry)
    assert idx.get("docs/a.md").title == "A"
    assert len(idx.list_documents()) == 1


def test_upsert_updates_existing(cfg):
    idx = docindex.new_docindex(cfg, USER)
    idx.upsert(docindex.DocumentEntry(file="docs/a.md", title="A"))
    idx.upsert(docindex.DocumentEntry(file="docs/a.md", title="A2", description="d"))
    docs = idx.list_documents()
    assert len(docs) == 1
    assert docs[0].title == "A2"
    assert docs[0].description == "d"


def test_remove(cfg):
    idx = docindex.new_docindex(cfg, USER)
    idx.upsert(docindex.DocumentEntry(file="docs/a.md", title="A"))
    assert idx.remove("docs/a.md") is True
    assert idx.remove("docs/a.md") is False
    assert idx.list_documents() == []


def test_set_public_and_indexing_status(cfg):
    idx = docindex.new_docindex(cfg, USER)
    idx.upsert(docindex.DocumentEntry(file="docs/a.md", title="A"))
    idx.set_public("docs/a.md", True, "token123")
    entry = idx.get("docs/a.md")
    assert entry.public is True
    assert entry.public_token == "token123"

    idx.set_indexing_status("docs/a.md", "indexed", rag_file_id="abc")
    entry = idx.get("docs/a.md")
    assert entry.indexing_status == "indexed"
    assert entry.rag_indexed is True
    assert entry.rag_file_id == "abc"

    idx.set_indexing_status("docs/a.md", "not_indexed")
    entry = idx.get("docs/a.md")
    assert entry.indexing_status == "not_indexed"
    assert entry.rag_indexed is False
    assert entry.rag_file_id is None  # cleared


def test_atomic_write_no_partial_file_on_replace(cfg, tmp_root):
    """The .tmp file should not exist after a successful write."""
    idx = docindex.new_docindex(cfg, USER)
    idx.upsert(docindex.DocumentEntry(file="a.md", title="A"))
    tmp_file = tmp_root / USER / "memory-index.json.tmp"
    assert not tmp_file.exists()
    final = tmp_root / USER / "memory-index.json"
    assert final.exists()
    payload = json.loads(final.read_text())
    assert payload["version"] == docindex.INDEX_VERSION


def test_concurrent_upserts_no_data_loss(cfg):
    """Many threads upserting distinct files must all end up persisted."""
    idx = docindex.new_docindex(cfg, USER)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            for j in range(20):
                idx.upsert(
                    docindex.DocumentEntry(
                        file=f"docs/file_{i}_{j}.md", title=f"T{i}-{j}"
                    )
                )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    docs = idx.list_documents()
    assert len(docs) == 8 * 20


def test_indexing_status_validation(cfg):
    idx = docindex.new_docindex(cfg, USER)
    idx.upsert(docindex.DocumentEntry(file="a.md", title="A"))
    with pytest.raises(ValueError):
        idx.set_indexing_status("a.md", "bogus_status")


def test_corruption_surfaces_as_storage_error(cfg, tmp_root):
    user_root = tmp_root / USER
    user_root.mkdir(parents=True)
    (user_root / "memory-index.json").write_text("{not json")
    idx = docindex.new_docindex(cfg, USER)
    with pytest.raises(storage.StorageError):
        idx.list_documents()


def test_priority_bounds():
    with pytest.raises(ValueError):
        docindex.DocumentEntry(file="x.md", priority=11)
    with pytest.raises(ValueError):
        docindex.DocumentEntry(file="x.md", priority=-1)


def test_schema_v3_defaults_and_roundtrip(cfg, tmp_root):
    idx = docindex.new_docindex(cfg, USER)
    entry = docindex.DocumentEntry(
        file="docs/proj.md",
        title="Project Doc",
        project_id="proj_123",
        project_name="Alphaplan",
        conversation_id="conv_abc",
    )
    idx.upsert(entry)

    saved = idx.get("docs/proj.md")
    assert saved is not None
    assert saved.project_id == "proj_123"
    assert saved.project_name == "Alphaplan"
    assert saved.conversation_id == "conv_abc"

    raw = json.loads((tmp_root / USER / "memory-index.json").read_text())
    assert raw["version"] == 3
    doc_raw = raw["documents"][0]
    assert doc_raw["project_id"] == "proj_123"
    assert doc_raw["project_name"] == "Alphaplan"
    assert doc_raw["conversation_id"] == "conv_abc"


def test_backward_compatibility_loads_v2_without_project(cfg, tmp_root):
    user_root = tmp_root / USER
    user_root.mkdir(parents=True)
    v2_data = {
        "version": 2,
        "updated_at": "2026-11-01T00:00:00Z",
        "documents": [
            {
                "file": "legacy.md",
                "title": "Old Doc",
                "indexing_status": "indexed",
                "rag_file_id": "rag-123",
            }
        ],
    }
    (user_root / "memory-index.json").write_text(json.dumps(v2_data))
    idx = docindex.new_docindex(cfg, USER)
    docs = idx.list_documents()
    assert len(docs) == 1
    assert docs[0].title == "Old Doc"
    assert docs[0].project_id is None
    assert docs[0].project_name is None
    assert docs[0].conversation_id is None


def test_list_documents_filtering(cfg):
    idx = docindex.new_docindex(cfg, USER)
    idx.upsert(docindex.DocumentEntry(file="a.md", project_id="p1"))
    idx.upsert(docindex.DocumentEntry(file="b.md", project_id="p1"))
    idx.upsert(docindex.DocumentEntry(file="c.md", project_id="p2"))
    idx.upsert(docindex.DocumentEntry(file="general.md"))

    assert len(idx.list_documents()) == 4
    assert [d.file for d in idx.list_documents(project_id="p1")] == ["a.md", "b.md"]
    assert [d.file for d in idx.list_documents(project_id="p2")] == ["c.md"]
    assert [d.file for d in idx.list_documents(project_id="nonexistent")] == []
    assert [d.file for d in idx.list_documents(unassigned_only=True)] == ["general.md"]


def test_set_project(cfg):
    idx = docindex.new_docindex(cfg, USER)
    idx.upsert(docindex.DocumentEntry(file="note.md", title="Note"))
    updated = idx.set_project("note.md", "p1", "Project 1", "conv1")
    assert updated is not None
    assert updated.project_id == "p1"
    assert updated.project_name == "Project 1"
    assert updated.conversation_id == "conv1"

    # Verify retrieval
    got = idx.get("note.md")
    assert got.project_id == "p1"

