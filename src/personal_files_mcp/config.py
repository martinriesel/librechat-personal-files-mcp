"""Runtime configuration loaded from environment variables.

The MCP server reads everything via ``AppSettings.from_env()``. Tests build
settings explicitly, bypassing the environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


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
    max_binary_file_mb: int = 50
    service_token: str = ""
    registry_db: str = "/data/private/_system/links.db"
    storage_providers: frozenset[str] = frozenset({"local"})
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_scopes: tuple[str, ...] = ("Files.ReadWrite.AppFolder", "User.Read", "offline_access")
    azure_redirect_uri: str = ""
    onedrive_token_encryption_key: str = ""
    onedrive_max_file_size_mb: int = 100
    onedrive_viewer_base_url: str = ""
    onedrive_oauth_db: str = ""
    onedrive_publishing_db: str = ""

    def service_token_usable(self) -> bool:
        return bool(self.service_token) and len(self.service_token) >= 16

    @classmethod
    def from_env(cls) -> AppSettings:
        raw_providers = _env_str("STORAGE_PROVIDERS", "local")
        providers = frozenset(
            value.strip().lower() for value in raw_providers.split(",") if value.strip()
        )
        unsupported = providers - {"local", "onedrive"}
        if unsupported:
            raise ValueError(
                "unsupported storage provider: " + ", ".join(sorted(unsupported))
            )
        raw_scopes = _env_str(
            "AZURE_SCOPES", "Files.ReadWrite.AppFolder User.Read offline_access"
        )
        settings = cls(
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
            storage_providers=providers or frozenset({"local"}),
            azure_tenant_id=_env_str("AZURE_TENANT_ID", ""),
            azure_client_id=_env_str("AZURE_CLIENT_ID", ""),
            azure_client_secret=_env_str("AZURE_CLIENT_SECRET", ""),
            azure_scopes=tuple(raw_scopes.split()),
            azure_redirect_uri=_env_str("AZURE_REDIRECT_URI", ""),
            onedrive_token_encryption_key=_env_str("ONEDRIVE_TOKEN_ENCRYPTION_KEY", ""),
            onedrive_max_file_size_mb=_env_int("ONEDRIVE_MAX_FILE_SIZE_MB", 100),
            onedrive_viewer_base_url=_env_str("ONEDRIVE_VIEWER_BASE_URL", ""),
            onedrive_oauth_db=_env_str("ONEDRIVE_OAUTH_DB", ""),
            onedrive_publishing_db=_env_str("ONEDRIVE_PUBLISHING_DB", ""),
        )
        settings.validate_onedrive_config()
        return settings

    @property
    def onedrive_enabled(self) -> bool:
        return "onedrive" in self.storage_providers

    def validate_onedrive_config(self) -> None:
        """Require complete, safe configuration only when OneDrive is enabled."""
        if not self.onedrive_enabled:
            return
        required = (
            ("AZURE_TENANT_ID", self.azure_tenant_id),
            ("AZURE_CLIENT_ID", self.azure_client_id),
            ("AZURE_CLIENT_SECRET", self.azure_client_secret),
            ("AZURE_REDIRECT_URI", self.azure_redirect_uri),
            ("ONEDRIVE_TOKEN_ENCRYPTION_KEY", self.onedrive_token_encryption_key),
        )
        for name, value in required:
            if not value.strip():
                raise ValueError(f"{name} is required when OneDrive is enabled")
        redirect = urlparse(self.azure_redirect_uri)
        if redirect.scheme != "https" or not redirect.netloc:
            raise ValueError("AZURE_REDIRECT_URI must be an absolute HTTPS URL")
        if redirect.query or redirect.fragment:
            raise ValueError("AZURE_REDIRECT_URI must not contain query or fragment")
        if not self.azure_scopes or "offline_access" not in self.azure_scopes:
            raise ValueError("AZURE_SCOPES must include offline_access")
        if "Files.ReadWrite.AppFolder" not in self.azure_scopes:
            raise ValueError("AZURE_SCOPES must include Files.ReadWrite.AppFolder")
        if (
            self.jwt_secret
            and self.onedrive_token_encryption_key == self.jwt_secret
        ):
            raise ValueError(
                "ONEDRIVE_TOKEN_ENCRYPTION_KEY must be distinct from JWT_SECRET"
            )
        if len(self.onedrive_token_encryption_key.encode("utf-8")) < 32:
            raise ValueError(
                "ONEDRIVE_TOKEN_ENCRYPTION_KEY must contain at least 32 bytes"
            )
        if self.onedrive_max_file_size_mb < 1:
            raise ValueError("ONEDRIVE_MAX_FILE_SIZE_MB must be a positive integer")
        if not self.onedrive_viewer_base_url:
            raise ValueError("ONEDRIVE_VIEWER_BASE_URL is required when OneDrive is enabled")
        viewer = urlparse(self.onedrive_viewer_base_url)
        if viewer.scheme != "https" or not viewer.netloc:
            raise ValueError("ONEDRIVE_VIEWER_BASE_URL must be an absolute HTTPS URL")
        if viewer.query or viewer.fragment:
            raise ValueError("ONEDRIVE_VIEWER_BASE_URL must not contain query or fragment")

    def jwt_secret_usable(self) -> bool:
        return bool(self.jwt_secret) and len(self.jwt_secret) >= 32
