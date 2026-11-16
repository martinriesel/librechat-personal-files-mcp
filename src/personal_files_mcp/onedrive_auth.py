"""OAuth transaction and encrypted-token primitives for OneDrive."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken

from . import storage

OAUTH_TRANSACTION_TTL = timedelta(minutes=10)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OAuthTransaction:
    user_id: str
    pkce_verifier: str
    expires_at: datetime


class OAuthTransactionStore:
    """SQLite store for short-lived, single-use OAuth transactions."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS oauth_transactions (
                    state_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    pkce_verifier TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def create(
        self,
        user_id: str,
        state_hash: str,
        pkce_verifier: str,
        expires_at: datetime,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM oauth_transactions WHERE expires_at <= ?",
                (_iso(datetime.now(UTC)),),
            )
            conn.execute(
                "INSERT INTO oauth_transactions(state_hash,user_id,pkce_verifier,expires_at) "
                "VALUES(?,?,?,?)",
                (state_hash, user_id, pkce_verifier, _iso(expires_at)),
            )

    def consume(self, state_hash: str, now: datetime) -> OAuthTransaction | None:
        """Atomically remove and return an unexpired transaction."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "DELETE FROM oauth_transactions WHERE state_hash = ? AND expires_at > ? "
                "RETURNING user_id, pkce_verifier, expires_at",
                (state_hash, _iso(now)),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if row is None:
            return None
        expiry = datetime.fromisoformat(row["expires_at"])
        return OAuthTransaction(row["user_id"], row["pkce_verifier"], expiry)

    def get(self, state_hash: str, now: datetime) -> OAuthTransaction | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id,pkce_verifier,expires_at FROM oauth_transactions "
                "WHERE state_hash=? AND expires_at > ?",
                (state_hash, _iso(now)),
            ).fetchone()
        if row is None:
            return None
        return OAuthTransaction(
            row["user_id"], row["pkce_verifier"], datetime.fromisoformat(row["expires_at"])
        )


class TokenVault:
    """Per-user encrypted token file vault."""

    def __init__(self, storage_root: str | Path, encryption_key: str | bytes) -> None:
        try:
            key = encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid ONEDRIVE_TOKEN_ENCRYPTION_KEY") from exc
        self._config = storage.StorageConfig(storage_root=Path(storage_root))

    def save(self, user_id: str, tokens: dict) -> None:
        target = storage.resolve(self._config, user_id, ".azure_tokens.enc")
        target.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self._fernet.encrypt(
            json.dumps(tokens, separators=(",", ":")).encode("utf-8")
        )
        temp = target.with_name(target.name + ".tmp-" + secrets.token_hex(8))
        try:
            temp.write_bytes(encrypted)
            temp.chmod(0o600)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)

    def load(self, user_id: str) -> dict | None:
        target = storage.resolve(self._config, user_id, ".azure_tokens.enc")
        if not target.exists():
            return None
        try:
            plaintext = self._fernet.decrypt(target.read_bytes())
            data = json.loads(plaintext.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("cannot decrypt OneDrive tokens") from exc
        if not isinstance(data, dict):
            raise ValueError("cannot decrypt OneDrive tokens")
        return data

    def delete(self, user_id: str) -> None:
        target = storage.resolve(self._config, user_id, ".azure_tokens.enc")
        target.unlink(missing_ok=True)


@dataclass(frozen=True)
class AuthorizationStart:
    url: str
    state: str


@dataclass(frozen=True)
class AuthResult:
    user_id: str


@dataclass(frozen=True)
class AuthStatus:
    connected: bool
    expires_at: datetime | None = None
    display_name: str | None = None
    email: str | None = None


class OneDriveAuth:
    """OAuth authorization-code + PKCE and per-user credential lifecycle."""

    def __init__(
        self,
        settings,
        transaction_db: str | Path,
        *,
        client_factory=httpx.AsyncClient,
        now=None,
    ) -> None:
        self._settings = settings
        self._transactions = OAuthTransactionStore(transaction_db)
        self._vault = TokenVault(
            settings.storage_root, settings.onedrive_token_encryption_key
        )
        self._client_factory = client_factory
        self._now = now or (lambda: datetime.now(UTC))

    def create_authorization(self, user_id: str) -> AuthorizationStart:
        return self.begin_authorization(user_id)

    def authorization_for_state(self, user_id: str, state: str) -> AuthorizationStart:
        expected_user = self._decode_state(state)
        if expected_user != user_id:
            raise ValueError("OAuth state belongs to another user")
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        transaction = self._transactions.get(state_hash, self._now())
        if transaction is None or transaction.user_id != user_id:
            raise ValueError("invalid or expired OAuth transaction")
        challenge = _b64url(hashlib.sha256(transaction.pkce_verifier.encode("ascii")).digest())
        return self._store_authorization(user_id, state, transaction.pkce_verifier, challenge, save=False)

    def begin_authorization(self, user_id: str) -> AuthorizationStart:
        """Create the signed state first so the public begin flow can redirect browser."""
        verifier, challenge = new_pkce_pair()
        state = self._sign_state(user_id)
        return self._store_authorization(user_id, state, verifier, challenge)

    def _store_authorization(
        self, user_id: str, state: str, verifier: str, challenge: str, *, save: bool = True
    ) -> AuthorizationStart:
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        if save:
            self._transactions.create(
                user_id, state_hash, verifier, self._now() + OAUTH_TRANSACTION_TTL
            )
        params = {
            "client_id": self._settings.azure_client_id,
            "response_type": "code",
            "redirect_uri": self._settings.azure_redirect_uri,
            "response_mode": "query",
            "scope": " ".join(self._settings.azure_scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = (
            f"https://login.microsoftonline.com/{self._settings.azure_tenant_id}"
            f"/oauth2/v2.0/authorize?{urlencode(params)}"
        )
        return AuthorizationStart(url=url, state=state)

    def cancel_authorization(self, state: str) -> None:
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        self._transactions.consume(state_hash, self._now() + OAUTH_TRANSACTION_TTL)

    def _sign_state(self, user_id: str) -> str:
        secret = self._settings.jwt_secret
        if not secret or len(secret) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters for OAuth state")
        now = int(self._now().timestamp())
        return jwt.encode(
            {"sub": user_id, "iat": now, "exp": now + int(OAUTH_TRANSACTION_TTL.total_seconds()),
             "jti": secrets.token_urlsafe(24)},
            secret,
            algorithm="HS256",
        )

    def _decode_state(self, state: str) -> str:
        try:
            payload = jwt.decode(
                state,
                self._settings.jwt_secret,
                algorithms=["HS256"],
                options={"verify_exp": False},
            )
            issued = int(payload["iat"])
            now = int(self._now().timestamp())
            if now - issued > int(OAUTH_TRANSACTION_TTL.total_seconds()):
                raise ValueError("expired OAuth state")
            return str(payload["sub"])
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid or expired OAuth state") from exc

    def handle_callback(self, code: str, state: str) -> AuthResult:
        expected_user = self._decode_state(state)
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        transaction = self._transactions.consume(state_hash, self._now())
        if transaction is None or transaction.user_id != expected_user:
            raise ValueError("invalid or expired OAuth transaction")
        tokens = _run_async(self._exchange_code(code, transaction.pkce_verifier))
        tokens["expires_at"] = int(self._now().timestamp()) + int(tokens.get("expires_in", 3600))
        self._vault.save(transaction.user_id, tokens)
        return AuthResult(user_id=transaction.user_id)

    async def _exchange_code(self, code: str, verifier: str) -> dict:
        async with self._client_factory(timeout=30.0) as client:
            response = await client.post(
                self._token_endpoint,
                data={
                    "client_id": self._settings.azure_client_id,
                    "client_secret": self._settings.azure_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._settings.azure_redirect_uri,
                    "code_verifier": verifier,
                    "scope": " ".join(self._settings.azure_scopes),
                },
            )
        if response.status_code >= 400:
            raise ValueError("Microsoft authorization code exchange failed")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise ValueError("Microsoft returned an invalid token response")
        if not payload.get("refresh_token"):
            raise ValueError("Microsoft did not return a refresh token; verify offline_access")
        return payload

    @property
    def _token_endpoint(self) -> str:
        return (
            f"https://login.microsoftonline.com/{self._settings.azure_tenant_id}"
            "/oauth2/v2.0/token"
        )

    def status(self, user_id: str) -> AuthStatus:
        tokens = self._vault.load(user_id)
        if not tokens:
            return AuthStatus(connected=False)
        expiry = datetime.fromtimestamp(int(tokens.get("expires_at", 0)), UTC)
        display_name = tokens.get("display_name")
        email = tokens.get("email")
        if display_name is None or email is None:
            try:
                profile = _run_async(self._fetch_profile(tokens["access_token"]))
                display_name = profile.get("displayName")
                email = profile.get("mail") or profile.get("userPrincipalName")
                tokens["display_name"] = display_name
                tokens["email"] = email
                self._vault.save(user_id, tokens)
            except Exception:
                pass
        return AuthStatus(
            connected=True,
            expires_at=expiry,
            display_name=display_name,
            email=email,
        )

    async def _fetch_profile(self, access_token: str) -> dict:
        async with self._client_factory(timeout=15.0) as client:
            response = await client.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        response.raise_for_status()
        return response.json()

    def get_valid_access_token(self, user_id: str) -> str:
        tokens = self._vault.load(user_id)
        if not tokens or not tokens.get("refresh_token"):
            raise PermissionError("onedrive_unauthorized — Execute onedrive_login primeiro.")
        if int(tokens.get("expires_at", 0)) > int(self._now().timestamp()) + 60:
            return str(tokens["access_token"])
        try:
            refreshed = _run_async(self._refresh(tokens["refresh_token"]))
        except PermissionError:
            self._vault.delete(user_id)
            raise
        refreshed.setdefault("refresh_token", tokens["refresh_token"])
        refreshed["expires_at"] = int(self._now().timestamp()) + int(
            refreshed.get("expires_in", 3600)
        )
        self._vault.save(user_id, refreshed)
        return str(refreshed["access_token"])

    async def _refresh(self, refresh_token: str) -> dict:
        async with self._client_factory(timeout=30.0) as client:
            response = await client.post(
                self._token_endpoint,
                data={
                    "client_id": self._settings.azure_client_id,
                    "client_secret": self._settings.azure_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": " ".join(self._settings.azure_scopes),
                },
            )
        if response.status_code in (400, 401):
            raise PermissionError("onedrive_unauthorized — Execute onedrive_login novamente.")
        if response.status_code >= 400:
            raise RuntimeError("Microsoft token refresh failed")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise RuntimeError("Microsoft returned an invalid refresh response")
        return payload

    def logout(self, user_id: str) -> None:
        tokens = self._vault.load(user_id)
        if tokens and tokens.get("refresh_token"):
            try:
                _run_async(self._revoke(tokens["refresh_token"]))
            except Exception:
                logger.info("OneDrive remote token revocation was unavailable")
        self._vault.delete(user_id)

    async def _revoke(self, refresh_token: str) -> None:
        # Microsoft Entra delegated refresh tokens do not expose a general
        # per-token revocation endpoint; logout always removes local credentials.
        return None


def new_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _b64url(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _run_async(coroutine):
    """Run network IO safely when a tool call is already inside an event loop."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()
