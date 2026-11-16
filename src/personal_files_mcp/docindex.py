"""Per-user documentation index (``memory-index.json``).

Briefing §4.3: every user owns a JSON catalog at
``/data/private/<userId>/memory-index.json`` with the schema version 2 shown in
``research-notes.md`` §10. Writes are atomic (``.tmp`` + ``os.replace``) and
guarded by both a per-process ``threading.Lock`` and a cross-process
``filelock.FileLock``.

This module never touches raw paths: it goes through ``storage.resolve`` so
that user roots are validated uniformly.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import filelock

from . import storage

logger = logging.getLogger(__name__)

INDEX_FILENAME = "memory-index.json"
INDEX_VERSION = 3
INDEX_TMP_SUFFIX = ".tmp"
LOCK_TIMEOUT_SECONDS = 10.0

VALID_INDEXING_STATUS: frozenset[str] = frozenset(
    {"not_indexed", "indexed", "indexing_failed"}
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class DocumentEntry:
    file: str
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    language: str = "pt-BR"
    type: str = "note"
    priority: int = 5
    last_reviewed: str | None = None
    when_to_read: list[str] = field(default_factory=list)
    rag_indexed: bool = False
    rag_file_id: str | None = None
    indexing_status: str = "not_indexed"
    public: bool = False
    public_token: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    conversation_id: str | None = None

    def __post_init__(self) -> None:
        if self.indexing_status not in VALID_INDEXING_STATUS:
            raise ValueError(
                f"invalid indexing_status: {self.indexing_status!r}"
            )
        if not 0 <= self.priority <= 10:
            raise ValueError("priority out of range 0..10")
        if not self.file:
            raise ValueError("file is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "topics": list(self.topics),
            "language": self.language,
            "type": self.type,
            "priority": self.priority,
            "last_reviewed": self.last_reviewed,
            "when_to_read": list(self.when_to_read),
            "rag_indexed": self.rag_indexed,
            "rag_file_id": self.rag_file_id,
            "indexing_status": self.indexing_status,
            "public": self.public,
            "public_token": self.public_token,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "conversation_id": self.conversation_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocumentEntry:
        return cls(
            file=data["file"],
            title=data.get("title", "") or "",
            description=data.get("description", "") or "",
            tags=list(data.get("tags") or []),
            topics=list(data.get("topics") or []),
            language=data.get("language", "pt-BR"),
            type=data.get("type", "note"),
            priority=int(data.get("priority", 5)),
            last_reviewed=data.get("last_reviewed"),
            when_to_read=list(data.get("when_to_read") or []),
            rag_indexed=bool(data.get("rag_indexed", False)),
            rag_file_id=data.get("rag_file_id"),
            indexing_status=data.get("indexing_status", "not_indexed"),
            public=bool(data.get("public", False)),
            public_token=data.get("public_token"),
            project_id=data.get("project_id"),
            project_name=data.get("project_name"),
            conversation_id=data.get("conversation_id"),
        )


@dataclass
class IndexData:
    version: int
    updated_at: str
    documents: list[DocumentEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "documents": [d.to_dict() for d in self.documents],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IndexData:
        return cls(
            version=int(data.get("version", INDEX_VERSION)),
            updated_at=data.get("updated_at") or _utc_now_iso(),
            documents=[DocumentEntry.from_dict(d) for d in data.get("documents") or []],
        )

    @classmethod
    def empty(cls) -> IndexData:
        return cls(version=INDEX_VERSION, updated_at=_utc_now_iso(), documents=[])


class DocIndex:
    """Manager for a single user's ``memory-index.json``."""

    def __init__(self, cfg: storage.StorageConfig, user_id: str) -> None:
        # Validate that user_id can be rooted, raising StorageError early if not.
        self._index_path = storage.resolve(cfg, user_id) / INDEX_FILENAME
        self._lock_path = storage.resolve(cfg, user_id) / f".{INDEX_FILENAME}.lock"
        self._user_root = storage.resolve(cfg, user_id)
        self._cfg = cfg
        self._user_id = user_id
        self._thread_lock = threading.Lock()
        self._file_lock = filelock.FileLock(
            str(self._lock_path), timeout=LOCK_TIMEOUT_SECONDS
        )

    @property
    def path(self) -> Path:
        return self._index_path

    # ------------------------------------------------------------------ IO

    def _read(self) -> IndexData:
        if not self._index_path.exists():
            return IndexData.empty()
        try:
            with self._index_path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise storage.StorageError(
                f"memory-index.json is corrupted: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise storage.StorageError("memory-index.json is not an object")
        return IndexData.from_dict(raw)

    def _atomic_write(self, data: IndexData) -> None:
        self._user_root.mkdir(parents=True, exist_ok=True)
        data.version = INDEX_VERSION
        data.updated_at = _utc_now_iso()
        payload = json.dumps(data.to_dict(), ensure_ascii=False, indent=2)
        tmp = self._index_path.with_suffix(INDEX_TMP_SUFFIX)
        with tmp.open("w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._index_path)

    def _mutate(self, mutate: Callable[[IndexData], None]) -> IndexData:
        with self._thread_lock, self._file_lock:
            data = self._read()
            mutate(data)
            self._atomic_write(data)
            return data

    # -------------------------------------------------------------- queries

    def list_documents(
        self,
        project_id: str | None = None,
        unassigned_only: bool = False,
    ) -> list[DocumentEntry]:
        with self._thread_lock:
            docs = list(self._read().documents)
            if project_id is not None:
                return [d for d in docs if d.project_id == project_id]
            if unassigned_only:
                return [d for d in docs if not d.project_id]
            return docs

    def get(self, file_path: str) -> DocumentEntry | None:
        for doc in self.list_documents():
            if doc.file == file_path:
                return doc
        return None

    def find_by_rag_id(self, rag_file_id: str) -> DocumentEntry | None:
        for doc in self.list_documents():
            if doc.rag_file_id == rag_file_id:
                return doc
        return None

    # -------------------------------------------------------------- mutators

    def set_project(
        self,
        file_path: str,
        project_id: str | None,
        project_name: str | None = None,
        conversation_id: str | None = None,
    ) -> DocumentEntry | None:
        captured: dict[str, DocumentEntry | None] = {"entry": None}

        def _do(data: IndexData) -> None:
            for doc in data.documents:
                if doc.file == file_path:
                    doc.project_id = project_id
                    doc.project_name = project_name
                    if conversation_id is not None:
                        doc.conversation_id = conversation_id
                    captured["entry"] = doc
                    return

        self._mutate(_do)
        return captured["entry"]

    def upsert(self, entry: DocumentEntry) -> DocumentEntry:
        captured: dict[str, DocumentEntry] = {}

        def _do(data: IndexData) -> None:
            for i, existing in enumerate(data.documents):
                if existing.file == entry.file:
                    data.documents[i] = entry
                    captured["entry"] = entry
                    return
            data.documents.append(entry)
            captured["entry"] = entry

        self._mutate(_do)
        return captured.get("entry", entry)

    def remove(self, file_path: str) -> bool:
        removed = {"flag": False}

        def _do(data: IndexData) -> None:
            for i, doc in enumerate(data.documents):
                if doc.file == file_path:
                    del data.documents[i]
                    removed["flag"] = True
                    return

        self._mutate(_do)
        return removed["flag"]

    def set_public(
        self, file_path: str, public: bool, token: str | None
    ) -> DocumentEntry | None:
        captured: dict[str, DocumentEntry | None] = {"entry": None}

        def _do(data: IndexData) -> None:
            for doc in data.documents:
                if doc.file == file_path:
                    doc.public = public
                    doc.public_token = token
                    captured["entry"] = doc
                    return

        self._mutate(_do)
        return captured["entry"]

    def set_indexing_status(
        self,
        file_path: str,
        status: str,
        rag_file_id: str | None = None,
    ) -> DocumentEntry | None:
        if status not in VALID_INDEXING_STATUS:
            raise ValueError(f"invalid indexing_status: {status!r}")
        captured: dict[str, DocumentEntry | None] = {"entry": None}

        def _do(data: IndexData) -> None:
            for doc in data.documents:
                if doc.file == file_path:
                    doc.indexing_status = status
                    doc.rag_indexed = status == "indexed"
                    if rag_file_id is not None:
                        doc.rag_file_id = rag_file_id
                    elif status == "not_indexed":
                        doc.rag_file_id = None
                    captured["entry"] = doc
                    return

        self._mutate(_do)
        return captured["entry"]

    def clear_rag_id(self, rag_file_id: str) -> list[DocumentEntry]:
        """Reset RAG metadata for any document pointing to ``rag_file_id``."""
        captured: list[DocumentEntry] = []

        def _do(data: IndexData) -> None:
            for doc in data.documents:
                if doc.rag_file_id == rag_file_id:
                    doc.rag_file_id = None
                    doc.rag_indexed = False
                    doc.indexing_status = "not_indexed"
                    captured.append(doc)

        self._mutate(_do)
        return captured


def new_docindex(
    cfg: storage.StorageConfig, user_id: str
) -> DocIndex:
    return DocIndex(cfg, user_id)


def all_files(index: DocIndex) -> Iterable[str]:
    return (doc.file for doc in index.list_documents())
