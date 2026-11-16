from __future__ import annotations

import json

from personal_files_mcp.docindex import DocumentEntry
from personal_files_mcp.onedrive_index import OneDriveDocIndex


class FakeRemoteStorage:
    def __init__(self, initial=None):
        self.value = initial
        self.etag = '"1"'
        self.fail_next_if_match = False

    def read_index(self):
        if self.value is None:
            return None, None
        return self.value, self.etag

    def write_index(self, content, if_match, create_if_absent=False):
        if self.fail_next_if_match:
            self.fail_next_if_match = False
            self.value = json.dumps(
                {"version": 2, "documents": [DocumentEntry(file="docs/other.md").to_dict()]}
            )
            self.etag = '"2"'
            raise IndexConflict
        if if_match is None and self.value is not None:
            raise IndexConflict
        if if_match is not None and if_match != self.etag:
            raise IndexConflict
        self.value = content
        self.etag = '"next"'


class IndexConflict(Exception):
    pass


def test_remote_index_upsert_retries_after_etag_conflict():
    remote = FakeRemoteStorage()
    remote.fail_next_if_match = True
    index = OneDriveDocIndex(remote)

    index.upsert(DocumentEntry(file="docs/current.md", title="Current"))

    payload, _etag = remote.read_index()
    files = {entry["file"] for entry in json.loads(payload)["documents"]}
    assert files == {"docs/current.md", "docs/other.md"}


def test_remote_index_uses_existing_document_schema():
    remote = FakeRemoteStorage()
    index = OneDriveDocIndex(remote)
    entry = DocumentEntry(file="docs/note.md", title="Note")

    index.upsert(entry)

    stored = json.loads(remote.value)
    assert stored["version"] == 2
    assert stored["documents"][0]["file"] == "docs/note.md"


def test_remote_index_updates_and_removes_only_matching_document():
    remote = FakeRemoteStorage()
    index = OneDriveDocIndex(remote)
    index.upsert(DocumentEntry(file="docs/a.md", title="A"))
    index.upsert(DocumentEntry(file="docs/b.md", title="B"))

    assert index.set_indexing_status("docs/a.md", "indexed", "a-rag-id").rag_file_id == "a-rag-id"
    assert index.remove("docs/b.md") is True

    stored = json.loads(remote.value)
    assert [item["file"] for item in stored["documents"]] == ["docs/a.md"]
    assert stored["documents"][0]["rag_file_id"] == "a-rag-id"


def test_remote_index_stops_after_bounded_conflicts():
    class AlwaysConflict(FakeRemoteStorage):
        def write_index(self, content, if_match, create_if_absent=False):
            raise IndexConflict

    index = OneDriveDocIndex(AlwaysConflict(), max_retries=2)

    from personal_files_mcp.onedrive_index import OneDriveIndexError

    try:
        index.upsert(DocumentEntry(file="docs/a.md"))
    except OneDriveIndexError as exc:
        assert "too many times" in str(exc)
    else:
        raise AssertionError("conflicting writes must stop after configured retries")


def test_remote_index_can_find_entry_by_rag_id():
    remote = FakeRemoteStorage()
    index = OneDriveDocIndex(remote)
    index.upsert(
        DocumentEntry(file="docs/a.md", rag_file_id="onedrive-rag-id", indexing_status="indexed")
    )

    found = index.find_by_rag_id("onedrive-rag-id")

    assert found.file == "docs/a.md"
