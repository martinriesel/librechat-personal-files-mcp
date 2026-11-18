"""Health endpoint implementation (briefing §5 + D005)."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from . import __version__
from .app import AppContext

logger = logging.getLogger(__name__)


async def healthz_handler(ctx: AppContext) -> dict:
    storage_status = "ok"
    try:
        Path(ctx.settings.storage_root).resolve(strict=False)
    except OSError as exc:
        logger.warning("storage_root not resolvable: %s", exc)
        storage_status = "error"

    rag_status = await _check_rag(ctx)

    jwt_ok = ctx.settings.jwt_secret_usable()
    overall = "ok" if (storage_status == "ok" and jwt_ok) else "degraded"

    return {
        "status": overall,
        "rag": rag_status,
        "storage": storage_status,
        "jwt_configured": jwt_ok,
        "version": __version__,
    }


async def _check_rag(ctx: AppContext) -> str:
    if not ctx.settings.jwt_secret_usable():
        return "disabled"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{ctx.settings.rag_api_url.rstrip('/')}/health")
        if resp.status_code == 200:
            return "available"
        return "unreachable"
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("rag_api health check failed: %s", exc)
        return "unreachable"
