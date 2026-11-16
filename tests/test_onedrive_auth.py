from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from personal_files_mcp.config import AppSettings
from personal_files_mcp.onedrive_auth import (
    OAuthTransactionStore,
    OneDriveAuth,
    TokenVault,
)


def test_oauth_transaction_is_consumed_once(tmp_path):
    store = OAuthTransactionStore(tmp_path / "oauth.db")
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    store.create("user_123456", "state-hash", "pkce-verifier", expiry)

    first = store.consume("state-hash", datetime.now(UTC))
    second = store.consume("state-hash", datetime.now(UTC))

    assert first is not None
    assert first.user_id == "user_123456"
    assert first.pkce_verifier == "pkce-verifier"
    assert second is None


def test_expired_oauth_transaction_cannot_be_consumed(tmp_path):
    store = OAuthTransactionStore(tmp_path / "oauth.db")
    expiry = datetime.now(UTC) - timedelta(seconds=1)
    store.create("user_123456", "expired-state", "verifier", expiry)

    assert store.consume("expired-state", datetime.now(UTC)) is None


def test_token_vault_encrypts_tokens_per_user(tmp_path):
    vault = TokenVault(tmp_path, Fernet.generate_key())
    token = {"access_token": "secret-access", "refresh_token": "secret-refresh"}

    vault.save("user_123456", token)

    token_path = tmp_path / "user_123456" / ".azure_tokens.enc"
    assert b"secret-access" not in token_path.read_bytes()
    assert vault.load("user_123456") == token
    assert vault.load("user_234567") is None


def test_token_vault_rejects_wrong_key(tmp_path):
    first = TokenVault(tmp_path, Fernet.generate_key())
    first.save("user_123456", {"access_token": "secret"})
    second = TokenVault(tmp_path, Fernet.generate_key())

    with pytest.raises(ValueError, match="cannot decrypt"):
        second.load("user_123456")


def test_authorization_url_contains_pkce_and_signed_state(tmp_path):
    settings = _settings(tmp_path)
    auth = OneDriveAuth(settings, tmp_path / "oauth.db")

    started = auth.create_authorization("user_123456")

    from urllib.parse import parse_qs, urlparse

    params = parse_qs(urlparse(started.url).query)
    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [started.state]
    assert started.state.count(".") == 2


def test_authorization_for_state_does_not_create_or_replace_transaction(tmp_path):
    auth = OneDriveAuth(_settings(tmp_path), tmp_path / "oauth.db")
    original = auth.begin_authorization("user_123456")
    from urllib.parse import parse_qs, urlparse

    params = parse_qs(urlparse(original.url).query)
    verifier_tx = auth._transactions.get(
        __import__("hashlib").sha256(original.state.encode()).hexdigest(), datetime.now(UTC)
    )

    started = auth.authorization_for_state("user_123456", original.state)
    verifier_after = auth._transactions.get(
        __import__("hashlib").sha256(original.state.encode()).hexdigest(), datetime.now(UTC)
    )

    assert parse_qs(urlparse(started.url).query)["code_challenge"] == params["code_challenge"]
    assert verifier_after == verifier_tx


def test_callback_consumes_state_and_stores_encrypted_tokens(tmp_path, respx_mock):
    settings = _settings(tmp_path)
    auth = OneDriveAuth(settings, tmp_path / "oauth.db")
    respx_mock.post(
        "https://login.microsoftonline.com/tenant-guid/oauth2/v2.0/token"
    ).respond(
        200,
        json={
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )

    started = auth.create_authorization("user_123456")
    result = auth.handle_callback("auth-code", started.state)

    assert result.user_id == "user_123456"
    assert auth.status("user_123456").connected is True
    token_file = tmp_path / "user_123456" / ".azure_tokens.enc"
    assert b"access-token" not in token_file.read_bytes()
    with pytest.raises(ValueError, match="invalid or expired"):
        auth.handle_callback("auth-code", started.state)


def test_expired_signed_state_is_rejected_before_token_exchange(tmp_path):
    now = datetime(2025, 1, 1, tzinfo=UTC)
    settings = _settings(tmp_path)
    auth = OneDriveAuth(settings, tmp_path / "oauth.db", now=lambda: now)
    state = auth._sign_state("user_123456")
    auth._now = lambda: now + timedelta(minutes=11)

    with pytest.raises(ValueError, match="invalid or expired"):
        auth.handle_callback("auth-code", state)


def test_token_refresh_rotates_refresh_token_encrypted(tmp_path, respx_mock):
    settings = _settings(tmp_path)
    auth = OneDriveAuth(settings, tmp_path / "oauth.db")
    vault = TokenVault(tmp_path, settings.onedrive_token_encryption_key)
    vault.save(
        "user_123456",
        {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 1,
        },
    )
    respx_mock.post(
        "https://login.microsoftonline.com/tenant-guid/oauth2/v2.0/token"
    ).respond(
        200,
        json={
            "access_token": "new-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 3600,
        },
    )

    assert auth.get_valid_access_token("user_123456") == "new-access"
    assert vault.load("user_123456")["refresh_token"] == "rotated-refresh"


def test_missing_connection_fails_closed(tmp_path):
    auth = OneDriveAuth(_settings(tmp_path), tmp_path / "oauth.db")

    with pytest.raises(PermissionError, match="onedrive_unauthorized"):
        auth.get_valid_access_token("user_123456")


def test_refresh_revocation_deletes_stale_local_credential(tmp_path, respx_mock):
    settings = _settings(tmp_path)
    auth = OneDriveAuth(settings, tmp_path / "oauth.db")
    vault = TokenVault(tmp_path, settings.onedrive_token_encryption_key)
    vault.save(
        "user_123456",
        {"access_token": "old", "refresh_token": "bad-refresh", "expires_at": 1},
    )
    respx_mock.post(
        "https://login.microsoftonline.com/tenant-guid/oauth2/v2.0/token"
    ).respond(400, json={"error": "invalid_grant"})

    with pytest.raises(PermissionError, match="onedrive_unauthorized"):
        auth.get_valid_access_token("user_123456")

    assert auth.status("user_123456").connected is False


def test_logout_removes_encrypted_local_tokens(tmp_path):
    settings = _settings(tmp_path)
    auth = OneDriveAuth(settings, tmp_path / "oauth.db")
    TokenVault(tmp_path, settings.onedrive_token_encryption_key).save(
        "user_123456", {"access_token": "access", "refresh_token": "refresh"}
    )

    auth.logout("user_123456")

    assert auth.status("user_123456").connected is False


def test_callback_rejects_token_response_with_wrong_tenant(tmp_path, respx_mock):
    settings = _settings(tmp_path)
    auth = OneDriveAuth(settings, tmp_path / "oauth.db")
    # Tenant-scoped authority is part of the token endpoint, so a response from
    # any other authority must never be used to establish the OneDrive link.
    respx_mock.post(
        "https://login.microsoftonline.com/tenant-guid/oauth2/v2.0/token"
    ).respond(400, json={"error": "invalid_grant"})
    started = auth.create_authorization("user_123456")

    with pytest.raises(ValueError, match="code exchange failed"):
        auth.handle_callback("bad-code", started.state)

    assert auth.status("user_123456").connected is False


def test_auth_status_reports_microsoft_profile(tmp_path, respx_mock):
    settings = _settings(tmp_path)
    auth = OneDriveAuth(settings, tmp_path / "oauth.db")
    vault = TokenVault(tmp_path, settings.onedrive_token_encryption_key)
    vault.save(
        "user_123456",
        {
            "access_token": "profile-token",
            "refresh_token": "refresh-token",
            "expires_at": int(datetime.now(UTC).timestamp()) + 3600,
        },
    )
    route = respx_mock.get("https://graph.microsoft.com/v1.0/me").respond(
        200,
        json={"displayName": "Alice", "mail": "alice@example.test"},
    )

    status = auth.status("user_123456")

    assert status.connected is True
    assert status.display_name == "Alice"
    assert status.email == "alice@example.test"
    assert route.calls[0].request.headers["Authorization"] == "Bearer profile-token"


def _settings(tmp_path):
    return AppSettings(
        storage_root=str(tmp_path),
        jwt_secret="j" * 40,
        storage_providers=frozenset({"local", "onedrive"}),
        azure_tenant_id="tenant-guid",
        azure_client_id="client-guid",
        azure_client_secret="client-secret",
        azure_scopes=("Files.ReadWrite.AppFolder", "User.Read", "offline_access"),
        azure_redirect_uri="https://chat.example.test/auth/azure/callback",
        onedrive_token_encryption_key=Fernet.generate_key().decode("ascii"),
        onedrive_viewer_base_url="https://chat.example.test/onedrive/view",
    )
