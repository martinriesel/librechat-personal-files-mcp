"""Logging configuration and structured diagnostics for Personal Files MCP.

Enforces:
    * Log level setting via ``PERSONAL_FILES_LOG_LEVEL`` (OFF, ERROR, WARNING, INFO, DEBUG).
    * Single-line JSON diagnostic events when ``PERSONAL_FILES_CONTEXT_DIAGNOSTICS=true``
      or when level is DEBUG.
    * Strict sanitization to prevent leaking credentials, tokens, connection strings,
      and payload bodies.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from .config import AppSettings

logger = logging.getLogger("personal_files_mcp")

# Keys that must NEVER appear in diagnostic logs
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "cookies",
        "token",
        "service_token",
        "x-service-token",
        "jwt_secret",
        "mongo_uri",
        "password",
        "secret",
        "key",
        "api_key",
    }
)

# Allowed fields in structured context diagnostic events (Fase 3 §8)
ALLOWED_EVENT_FIELDS = frozenset(
    {
        "event",
        "request_id",
        "tool_name",
        "transport_method",
        "identity_present",
        "conversation_header_present",
        "conversation_id_valid",
        "conversation_id",
        "project_id",
        "resolution_status",
        "elapsed_ms",
        "error_code",
    }
)


def configure_logging(settings: AppSettings) -> None:
    """Configure the root application logger according to AppSettings."""
    level_name = settings.personal_files_log_level.upper()

    if level_name == "OFF":
        logger.setLevel(logging.CRITICAL + 1)
        logger.disabled = True
        return

    level = getattr(logging, level_name, logging.INFO)
    logger.disabled = False
    logger.setLevel(level)

    # Avoid adding duplicate handlers if already configured
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def log_context_event(
    settings: AppSettings,
    event: str,
    request_id: str,
    **kwargs: Any,
) -> None:
    """Emit a structured single-line JSON diagnostic event if diagnostics enabled.

    Only logged when ``settings.personal_files_context_diagnostics`` is True
    or log level is DEBUG. Respects OFF/WARNING/ERROR.
    """
    if settings.personal_files_log_level in ("OFF", "WARNING", "ERROR"):
        return

    if not (settings.personal_files_context_diagnostics or settings.personal_files_log_level == "DEBUG"):
        return

    payload: dict[str, Any] = {
        "event": event,
        "request_id": request_id,
    }

    for key, value in kwargs.items():
        if key in SENSITIVE_KEYS:
            continue
        if key in ALLOWED_EVENT_FIELDS:
            # If diagnostics is not active, do not log full conversation_id
            if key == "conversation_id" and not settings.personal_files_context_diagnostics:
                continue
            payload[key] = value

    logger.info(json.dumps(payload, separators=(",", ":")))
