"""Per-request context management and conversation header validation.

Captures request-scoped context (user_id, conversation_id, request_id)
using a Python ContextVar to ensure strict isolation across concurrent
async requests and background tasks.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Matches placeholders like {{LIBRECHAT_BODY_CONVERSATIONID}}
PLACEHOLDER_RE = re.compile(r"^\{\{.*\}\}$")

# Safe conversation ID: UUIDs or alfanumeric with hyphens/underscores (8 to 128 chars)
SAFE_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


@dataclass(frozen=True)
class RequestContextData:
    user_id: str
    conversation_id: str | None = None
    request_id: str = ""
    project_id: str | None = None
    project_name: str | None = None


_current_request_context: ContextVar[RequestContextData | None] = ContextVar(
    "current_request_context", default=None
)


def set_request_context(ctx_data: RequestContextData) -> Token:
    """Bind request context to the current async execution context."""
    return _current_request_context.set(ctx_data)


def reset_request_context(token: Token) -> None:
    """Restore the previous request context."""
    _current_request_context.reset(token)


def get_request_context() -> RequestContextData | None:
    """Return the active request context for the current async task."""
    return _current_request_context.get()


def extract_conversation_id(
    headers: Mapping[str, str] | None,
    header_name: str = "X-Conversation-Id",
) -> tuple[str | None, str]:
    """Extract and validate conversation ID from HTTP headers.

    Returns:
        tuple of (normalized_conversation_id or None, status_code)
        Status codes:
            * "valid": valid ID extracted.
            * "missing": header not present.
            * "empty": header present but empty or whitespace.
            * "placeholder": literal placeholder like {{LIBRECHAT_BODY_CONVERSATIONID}}.
            * "invalid": failed length or character safety checks.
    """
    if headers is None:
        return None, "missing"

    target = header_name.lower()
    raw: str | None = None
    for key, value in headers.items():
        if key.lower() == target:
            raw = value
            break

    if raw is None:
        return None, "missing"

    val = raw.strip()
    if not val:
        return None, "empty"

    if PLACEHOLDER_RE.match(val):
        logger.warning("conversation header received literal placeholder: %r", val)
        return None, "placeholder"

    if not SAFE_CONVERSATION_ID_RE.match(val):
        logger.warning("conversation header failed validation regex: %r", val)
        return None, "invalid"

    return val, "valid"
