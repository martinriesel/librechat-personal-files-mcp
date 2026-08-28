"""Runtime configuration loaded from environment variables.

The MCP server reads everything via ``AppSettings.from_env()``. Tests build
settings explicitly, bypassing the environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer (got {raw!r})") from exc


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


@dataclass(frozen=True)
class AppSettings:
    user_header: str = "X-User-Id"
    storage_root: str = "/data/private"
    share_root: str = "/data/share"
    rag_api_url: str = "http://rag_api:8000"
    jwt_secret: str = ""
    public_base_url: str = "https://your-domain.example/files"
    max_file_size_mb: int = 20
    registry_db: str = "/data/private/_system/links.db"

    @classmethod
    def from_env(cls) -> AppSettings:
        return cls(
            user_header=_env_str("USER_HEADER", cls.user_header),
            storage_root=_env_str("STORAGE_ROOT", cls.storage_root),
            share_root=_env_str("SHARE_ROOT", cls.share_root),
            rag_api_url=_env_str("RAG_API_URL", cls.rag_api_url),
            jwt_secret=_env_str("JWT_SECRET", cls.jwt_secret),
            public_base_url=_env_str("PUBLIC_BASE_URL", cls.public_base_url),
            max_file_size_mb=_env_int("MAX_FILE_SIZE_MB", cls.max_file_size_mb),
            registry_db=_env_str("REGISTRY_DB", cls.registry_db),
        )

    def jwt_secret_usable(self) -> bool:
        return bool(self.jwt_secret) and len(self.jwt_secret) >= 32
