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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    val = raw.strip().lower()
    if val in ("true", "1", "yes", "on"):
        return True
    if val in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean (got {raw!r})")


@dataclass(frozen=True)
class AppSettings:
    user_header: str = "X-User-Id"
    storage_root: str = "/data/private"
    share_root: str = "/data/share"
    rag_api_url: str = "http://rag_api:8000"
    jwt_secret: str = ""
    public_base_url: str = "https://your-domain.example/files"
    max_file_size_mb: int = 20
    max_binary_file_mb: int = 50
    service_token: str = ""
    registry_db: str = "/data/private/_system/links.db"
    conversation_header: str = "X-Conversation-Id"
    librechat_mongo_uri: str = ""
    librechat_mongo_db: str = "LibreChat"
    librechat_mongo_timeout_ms: int = 2000
    personal_files_log_level: str = "INFO"
    personal_files_context_diagnostics: bool = False

    def service_token_usable(self) -> bool:
        return bool(self.service_token) and len(self.service_token) >= 16

    @classmethod
    def from_env(cls) -> AppSettings:
        log_level = _env_str(
            "PERSONAL_FILES_LOG_LEVEL", cls.personal_files_log_level
        ).upper()
        allowed_log_levels = {"OFF", "ERROR", "WARNING", "INFO", "DEBUG"}
        if log_level not in allowed_log_levels:
            raise ValueError(
                f"PERSONAL_FILES_LOG_LEVEL must be one of {sorted(allowed_log_levels)} (got {log_level!r})"
            )

        return cls(
            user_header=_env_str("USER_HEADER", cls.user_header),
            storage_root=_env_str("STORAGE_ROOT", cls.storage_root),
            share_root=_env_str("SHARE_ROOT", cls.share_root),
            rag_api_url=_env_str("RAG_API_URL", cls.rag_api_url),
            jwt_secret=_env_str("JWT_SECRET", cls.jwt_secret),
            public_base_url=_env_str("PUBLIC_BASE_URL", cls.public_base_url),
            max_file_size_mb=_env_int("MAX_FILE_SIZE_MB", cls.max_file_size_mb),
            max_binary_file_mb=_env_int("MAX_BINARY_FILE_MB", cls.max_binary_file_mb),
            service_token=_env_str("SERVICE_TOKEN", cls.service_token),
            registry_db=_env_str("REGISTRY_DB", cls.registry_db),
            conversation_header=_env_str("CONVERSATION_HEADER", cls.conversation_header),
            librechat_mongo_uri=_env_str("LIBRECHAT_MONGO_URI", cls.librechat_mongo_uri),
            librechat_mongo_db=_env_str("LIBRECHAT_MONGO_DB", cls.librechat_mongo_db),
            librechat_mongo_timeout_ms=_env_int(
                "LIBRECHAT_MONGO_TIMEOUT_MS", cls.librechat_mongo_timeout_ms
            ),
            personal_files_log_level=log_level,
            personal_files_context_diagnostics=_env_bool(
                "PERSONAL_FILES_CONTEXT_DIAGNOSTICS", cls.personal_files_context_diagnostics
            ),
        )

    def jwt_secret_usable(self) -> bool:
        return bool(self.jwt_secret) and len(self.jwt_secret) >= 32
