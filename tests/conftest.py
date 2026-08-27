"""Shared pytest fixtures."""
from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from personal_files_mcp import storage


@pytest.fixture
def tmp_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="pfmcp-") as tmp:
        yield Path(tmp)


@pytest.fixture
def cfg(tmp_root: Path) -> storage.StorageConfig:
    return storage.StorageConfig(storage_root=tmp_root)


VALID_OBJECTID = "5f8d3a4b2c1e0d0001a2b3c4"
VALID_GENERIC = "user_with_underscore-1"
