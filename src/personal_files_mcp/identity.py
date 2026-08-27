"""User identity validation.

Source of truth for the per-request user id used by every other module. All
tools, storage operations, docindex and publishing MUST obtain the user id
through ``extract_user_id`` (or the FastAPI dependency declared in ``app.py``).
The id is **never** taken from LLM arguments.

Fail-closed contract (briefing §4.1):
    * header absent or empty -> ``IdentityError`` with code ``missing_user_identity``
    * header fails generic regex -> ``IdentityError`` with code ``invalid_user_identity``
    * header matches tolerant regex but not ObjectId -> accepted, WARNING logged

Accepted formats:
    * MongoDB ObjectId: ``^[a-f0-9]{24}$``
    * Tolerant generic: ``^[A-Za-z0-9_-]{8,64}$``
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

OBJECTID_RE = re.compile(r"^[a-f0-9]{24}$")
GENERIC_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class IdentityError(Exception):
    """Raised when the identity header is missing or invalid.

    Attributes:
        code: machine-readable error code ("missing_user_identity" or
            "invalid_user_identity").
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class IdentityConfig:
    """Runtime configuration for identity extraction."""

    header_name: str = "X-User-Id"


def validate_user_id(raw: str | None) -> str:
    """Validate a raw header value and return the normalized user id.

    Raises:
        IdentityError: if the value is missing, empty, or does not match the
            accepted generic regex.
    """
    if raw is None:
        logger.warning("identity header missing")
        raise IdentityError("missing_user_identity")

    user_id = raw.strip()
    if not user_id:
        logger.warning("identity header empty")
        raise IdentityError("missing_user_identity")

    if not GENERIC_RE.match(user_id):
        logger.warning("identity header failed generic validation: %r", user_id)
        raise IdentityError("invalid_user_identity")

    if not OBJECTID_RE.match(user_id):
        logger.warning(
            "identity header matches tolerant format but not ObjectId: %r", user_id
        )

    return user_id


def extract_user_id(
    headers: Mapping[str, str] | None,
    config: IdentityConfig | None = None,
) -> str:
    """Extract and validate a user id from a headers mapping.

    The lookup is case-insensitive (mirrors HTTP/ASGI semantics).
    """
    cfg = config or IdentityConfig()
    raw: str | None = None
    if headers is not None:
        target = cfg.header_name.lower()
        for key, value in headers.items():
            if key.lower() == target:
                raw = value
                break
    return validate_user_id(raw)
