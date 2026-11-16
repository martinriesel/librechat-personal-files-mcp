"""eTag-protected OneDrive memory-index.json operations."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from .docindex import DocumentEntry, IndexData

MAX_CONFLICT_RETRIES = 4


class OneDriveIndexError(Exception):
    """Remote index read/write failure."""


class OneDriveDocIndex:
    def __init__(self, storage, max_retries: int = MAX_CONFLICT_RETRIES) -> None:
        self._storage = storage
        self._max_retries = max_retries

    def _read(self) -> tuple[IndexData, str | None]:
        raw, etag = self._storage.read_index()
        if raw is None:
            return IndexData.empty(), None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("index must be an object")
            return IndexData.from_dict(data), etag
        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            raise OneDriveIndexError("OneDrive memory-index.json is invalid") from exc

    def _mutate(self, mutation) -> IndexData:
        for attempt in range(self._max_retries):
            data, etag = self._read()
            mutation(data)
            data.updated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            serialized = json.dumps(data.to_dict(), ensure_ascii=False, indent=2)
            try:
                self._storage.write_index(
                    serialized, if_match=etag, create_if_absent=etag is None
                )
                return data
            except Exception as exc:
                if not _is_precondition_conflict(exc) or attempt == self._max_retries - 1:
                    if _is_precondition_conflict(exc):
                        raise OneDriveIndexError("OneDrive index changed too many times") from exc
                    raise
        raise OneDriveIndexError("OneDrive index update failed")

    def list_documents(self) -> list[DocumentEntry]:
        return self._read()[0].documents

    def get(self, file_path: str) -> DocumentEntry | None:
        return next((entry for entry in self.list_documents() if entry.file == file_path), None)

    def upsert(self, entry: DocumentEntry) -> DocumentEntry:
        def apply(data: IndexData) -> None:
            for index, existing in enumerate(data.documents):
                if existing.file == entry.file:
                    data.documents[index] = entry
                    return
            data.documents.append(entry)

        self._mutate(apply)
        return entry

    def remove(self, file_path: str) -> bool:
        removed = False

        def apply(data: IndexData) -> None:
            nonlocal removed
            before = len(data.documents)
            data.documents[:] = [entry for entry in data.documents if entry.file != file_path]
            removed = len(data.documents) != before

        self._mutate(apply)
        return removed

    def find_by_rag_id(self, rag_file_id: str) -> DocumentEntry | None:
        return next(
            (entry for entry in self.list_documents() if entry.rag_file_id == rag_file_id),
            None,
        )

    def set_indexing_status(
        self, file_path: str, status: str, rag_file_id: str | None = None
    ) -> DocumentEntry | None:
        found: DocumentEntry | None = None

        def apply(data: IndexData) -> None:
            nonlocal found
            for entry in data.documents:
                if entry.file == file_path:
                    entry.indexing_status = status
                    entry.rag_indexed = status == "indexed"
                    if rag_file_id is not None:
                        entry.rag_file_id = rag_file_id
                    elif status == "not_indexed":
                        entry.rag_file_id = None
                    found = entry
                    break

        self._mutate(apply)
        return found


def new_index_document(file_path: str, **metadata) -> DocumentEntry:
    """Create a schema-compatible entry without coupling remote IO to local DocIndex."""
    return DocumentEntry(file=file_path, **metadata)


def _is_precondition_conflict(exc: Exception) -> bool:
    return (
        getattr(exc, "status_code", None) == 412
        or getattr(exc, "code", None) == "onedrive_index_conflict"
        or exc.__class__.__name__ in {
        "IndexConflict",
        "PreconditionFailed",
        }
    )
