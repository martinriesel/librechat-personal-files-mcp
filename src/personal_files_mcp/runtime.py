"""Shared runtime context used by app, tools, publishing and RAG.

Defined in a separate module to avoid circular imports between
``app`` (which imports ``publishing``) and ``publishing`` (which needs
the context dataclass).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import storage
from .config import AppSettings


@dataclass
class AppContext:
    settings: AppSettings
    storage_cfg: storage.StorageConfig
    onedrive_auth: object | None = None
    onedrive_storage: object | None = None
    onedrive_publishing: object | None = None
