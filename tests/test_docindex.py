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
