"""Shared runtime context used by app, tools, publishing and RAG.

Defined in a separate module to avoid circular imports between
``app`` (which imports ``publishing``) and ``publishing`` (which needs
the context dataclass).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import storage
from .config import AppSettings


@dataclass
class AppContext:
    settings: AppSettings
    storage_cfg: storage.StorageConfig
    project_resolver: Any | None = None
