from __future__ import annotations

import pytest

from personal_files_mcp.config import AppSettings


def test_storage_providers_defaults_to_local(monkeypatch):
    monkeypatch.delenv("STORAGE_PROVIDERS", raising=False)

    settings = AppSettings.from_env()

    assert settings.storage_providers == frozenset({"local"})
    assert settings.onedrive_enabled is False


def test_storage_providers_can_enable_onedrive(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "local, onedrive")
    _set_valid_onedrive_env(monkeypatch)

    settings = AppSettings.from_env()

    assert settings.storage_providers == frozenset({"local", "onedrive"})
    assert settings.onedrive_enabled is True


def test_local_only_does_not_require_azure_configuration(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "local")
    monkeypatch.delenv("AZURE_TENANT_ID", raising=False)

    settings = AppSettings.from_env()
    settings.validate_onedrive_config()


def test_enabled_onedrive_requires_complete_configuration(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "local,onedrive")
    _set_valid_onedrive_env(monkeypatch)
    monkeypatch.delenv("AZURE_TENANT_ID", raising=False)

    with pytest.raises(ValueError, match="AZURE_TENANT_ID"):
        AppSettings.from_env()


def test_unknown_storage_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "local,gdrive")

    with pytest.raises(ValueError, match="unsupported storage provider"):
        AppSettings.from_env()


def test_onedrive_requires_encryption_key_distinct_from_jwt_secret(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "onedrive")
    _set_valid_onedrive_env(monkeypatch)
    monkeypatch.setenv("ONEDRIVE_TOKEN_ENCRYPTION_KEY", "same-secret")
    monkeypatch.setenv("JWT_SECRET", "same-secret")

    with pytest.raises(ValueError, match="must be distinct"):
        AppSettings.from_env()


def test_onedrive_requires_https_redirect_uri(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "onedrive")
    _set_valid_onedrive_env(monkeypatch)
    monkeypatch.setenv("AZURE_REDIRECT_URI", "http://chat.example.test/auth/azure/callback")

    with pytest.raises(ValueError, match="absolute HTTPS URL"):
        AppSettings.from_env()


def test_onedrive_requires_offline_access_for_refresh_tokens(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "onedrive")
    _set_valid_onedrive_env(monkeypatch)
    monkeypatch.setenv("AZURE_SCOPES", "Files.ReadWrite.AppFolder User.Read")

    with pytest.raises(ValueError, match="offline_access"):
        AppSettings.from_env()


def test_onedrive_rejects_nonpositive_upload_limit(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "onedrive")
    _set_valid_onedrive_env(monkeypatch)
    monkeypatch.setenv("ONEDRIVE_MAX_FILE_SIZE_MB", "0")

    with pytest.raises(ValueError, match="positive integer"):
        AppSettings.from_env()


def test_onedrive_requires_https_viewer_base(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "onedrive")
    _set_valid_onedrive_env(monkeypatch)
    monkeypatch.setenv("ONEDRIVE_VIEWER_BASE_URL", "http://chat.example.test")

    with pytest.raises(ValueError, match="ONEDRIVE_VIEWER_BASE_URL"):
        AppSettings.from_env()


def test_onedrive_encryption_key_must_be_at_least_32_bytes(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "onedrive")
    _set_valid_onedrive_env(monkeypatch)
    monkeypatch.setenv("ONEDRIVE_TOKEN_ENCRYPTION_KEY", "short")

    with pytest.raises(ValueError, match="at least 32 bytes"):
        AppSettings.from_env()


def test_viewer_base_cannot_contain_query_or_fragment(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "onedrive")
    _set_valid_onedrive_env(monkeypatch)
    monkeypatch.setenv(
        "ONEDRIVE_VIEWER_BASE_URL", "https://chat.example.test/onedrive/view?user=1"
    )

    with pytest.raises(ValueError, match="query or fragment"):
        AppSettings.from_env()


def test_redirect_uri_cannot_contain_query_or_fragment(monkeypatch):
    monkeypatch.setenv("STORAGE_PROVIDERS", "onedrive")
    _set_valid_onedrive_env(monkeypatch)
    monkeypatch.setenv(
        "AZURE_REDIRECT_URI", "https://chat.example.test/auth/azure/callback?user=1"
    )

    with pytest.raises(ValueError, match="query or fragment"):
        AppSettings.from_env()


def _set_valid_onedrive_env(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-guid")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-guid")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("AZURE_REDIRECT_URI", "https://chat.example.test/auth/azure/callback")
    monkeypatch.setenv("AZURE_SCOPES", "Files.ReadWrite.AppFolder User.Read offline_access")
    monkeypatch.setenv("ONEDRIVE_TOKEN_ENCRYPTION_KEY", "dedicated-fernet-key-123456789012345")
    monkeypatch.setenv("ONEDRIVE_VIEWER_BASE_URL", "https://chat.example.test/onedrive/view")
