"""Resolver for LibreChat conversation-to-project association.

Consults LibreChat's MongoDB (read-only) to resolve:
    user + conversationId -> chatProjectId -> project details
with strict owner isolation, micro-retry on new conversation race conditions,
and no state leak between requests.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

try:
    from bson import ObjectId
except ImportError:  # pragma: no cover
    try:
        from bson.objectid import ObjectId  # type: ignore
    except ImportError:
        ObjectId = None  # type: ignore

from .config import AppSettings
from .diagnostics import log_context_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectResolutionResult:
    """Outcome of attempting to resolve a conversation's project."""

    status: str
    project_id: str | None = None
    project_name: str | None = None
    project_description: str | None = None
    error: str | None = None
    retry_recommended: bool = False

    def as_dict(self, conversation_id: str | None = None) -> dict[str, Any]:
        """Convert to clean JSON-serializable dictionary for tools."""
        res: dict[str, Any] = {"status": self.status}
        if conversation_id:
            res["conversationId"] = conversation_id

        if self.status == "project":
            res["project"] = {
                "id": self.project_id,
                "name": self.project_name or "",
            }
            if self.project_description:
                res["project"]["description"] = self.project_description
        elif self.status == "no_project":
            res["message"] = "This conversation is not linked to any LibreChat project."
        elif self.status == "conversation_unavailable":
            res["message"] = "Conversation context is pending database persistence."
            if self.retry_recommended:
                res["retry_recommended"] = True
        elif self.status == "context_missing":
            res["message"] = "No conversation identifier was supplied in request headers."
        elif self.status == "context_invalid":
            res["message"] = "Conversation header value is invalid or a literal placeholder."
        elif self.status == "project_unavailable":
            res["message"] = "Project is not accessible or does not belong to the user."
        elif self.status == "resolver_unavailable":
            res["message"] = "Project resolver database connection is not available."

        return res


class ProjectResolver:
    """Async resolver for LibreChat project associations."""

    def __init__(
        self,
        mongo_client: Any | None,
        db_name: str = "LibreChat",
        timeout_ms: int = 2000,
    ) -> None:
        self._client = mongo_client
        self._db_name = db_name
        self._timeout_sec = max(timeout_ms, 100) / 1000.0

    @property
    def is_available(self) -> bool:
        return self._client is not None

    async def resolve(
        self,
        settings: AppSettings,
        user_id: str,
        conversation_id: str | None,
        conv_status: str = "valid",
        request_id: str = "",
    ) -> ProjectResolutionResult:
        """Resolve project context for a user and conversation ID."""
        start_time = time.monotonic()

        log_context_event(
            settings,
            "project.lookup.started",
            request_id=request_id,
            conversation_id=conversation_id,
        )

        if conv_status == "placeholder" or conv_status == "invalid":
            elapsed = int((time.monotonic() - start_time) * 1000)
            log_context_event(
                settings,
                "project.lookup.completed",
                request_id=request_id,
                resolution_status="context_invalid",
                elapsed_ms=elapsed,
            )
            return ProjectResolutionResult(status="context_invalid")

        if not conversation_id or conv_status == "missing" or conv_status == "empty":
            elapsed = int((time.monotonic() - start_time) * 1000)
            log_context_event(
                settings,
                "project.lookup.completed",
                request_id=request_id,
                resolution_status="context_missing",
                elapsed_ms=elapsed,
            )
            return ProjectResolutionResult(status="context_missing")

        if not self._client:
            elapsed = int((time.monotonic() - start_time) * 1000)
            log_context_event(
                settings,
                "project.lookup.failed",
                request_id=request_id,
                resolution_status="resolver_unavailable",
                error_code="client_not_configured",
                elapsed_ms=elapsed,
            )
            return ProjectResolutionResult(
                status="resolver_unavailable", error="mongo_not_configured"
            )

        db = self._client[self._db_name]

        # 1. Fetch conversation with micro-retry for Turn 1 race conditions
        conv: dict[str, Any] | None = None
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                conv = await asyncio.wait_for(
                    db.conversations.find_one(
                        {"user": user_id, "conversationId": conversation_id},
                        projection={"chatProjectId": 1, "_id": 0},
                    ),
                    timeout=self._timeout_sec,
                )
                if conv is not None:
                    break
            except TimeoutError:
                elapsed = int((time.monotonic() - start_time) * 1000)
                log_context_event(
                    settings,
                    "project.lookup.failed",
                    request_id=request_id,
                    resolution_status="resolver_unavailable",
                    error_code="timeout",
                    elapsed_ms=elapsed,
                )
                return ProjectResolutionResult(
                    status="resolver_unavailable", error="database_timeout"
                )
            except Exception:
                elapsed = int((time.monotonic() - start_time) * 1000)
                log_context_event(
                    settings,
                    "project.lookup.failed",
                    request_id=request_id,
                    resolution_status="resolver_unavailable",
                    error_code="database_error",
                    elapsed_ms=elapsed,
                )
                return ProjectResolutionResult(
                    status="resolver_unavailable", error="database_error"
                )

            if attempt < max_retries:
                await asyncio.sleep(0.25)

        if conv is None:
            elapsed = int((time.monotonic() - start_time) * 1000)
            log_context_event(
                settings,
                "project.lookup.completed",
                request_id=request_id,
                resolution_status="conversation_unavailable",
                elapsed_ms=elapsed,
            )
            return ProjectResolutionResult(
                status="conversation_unavailable", retry_recommended=True
            )

        chat_project_id = conv.get("chatProjectId")
        if not chat_project_id:
            elapsed = int((time.monotonic() - start_time) * 1000)
            log_context_event(
                settings,
                "project.lookup.completed",
                request_id=request_id,
                resolution_status="no_project",
                elapsed_ms=elapsed,
            )
            return ProjectResolutionResult(status="no_project")

        # 2. Validate and fetch project
        try:
            if ObjectId is not None:
                proj_obj_id = ObjectId(str(chat_project_id))
            else:
                proj_obj_id = str(chat_project_id)
        except Exception:
            elapsed = int((time.monotonic() - start_time) * 1000)
            log_context_event(
                settings,
                "project.lookup.completed",
                request_id=request_id,
                resolution_status="project_unavailable",
                error_code="invalid_project_id",
                elapsed_ms=elapsed,
            )
            return ProjectResolutionResult(status="project_unavailable")

        try:
            proj = await asyncio.wait_for(
                db.chatprojects.find_one(
                    {"_id": proj_obj_id, "user": user_id},
                    projection={"_id": 1, "name": 1, "description": 1},
                ),
                timeout=self._timeout_sec,
            )
        except TimeoutError:
            elapsed = int((time.monotonic() - start_time) * 1000)
            log_context_event(
                settings,
                "project.lookup.failed",
                request_id=request_id,
                resolution_status="resolver_unavailable",
                error_code="timeout",
                elapsed_ms=elapsed,
            )
            return ProjectResolutionResult(
                status="resolver_unavailable", error="database_timeout"
            )
        except Exception:
            elapsed = int((time.monotonic() - start_time) * 1000)
            log_context_event(
                settings,
                "project.lookup.failed",
                request_id=request_id,
                resolution_status="resolver_unavailable",
                error_code="database_error",
                elapsed_ms=elapsed,
            )
            return ProjectResolutionResult(
                status="resolver_unavailable", error="database_error"
            )

        if not proj:
            elapsed = int((time.monotonic() - start_time) * 1000)
            log_context_event(
                settings,
                "project.lookup.completed",
                request_id=request_id,
                resolution_status="project_unavailable",
                elapsed_ms=elapsed,
            )
            return ProjectResolutionResult(status="project_unavailable")

        project_id_str = str(proj["_id"])
        elapsed = int((time.monotonic() - start_time) * 1000)
        log_context_event(
            settings,
            "project.lookup.completed",
            request_id=request_id,
            project_id=project_id_str,
            resolution_status="project",
            elapsed_ms=elapsed,
        )

        return ProjectResolutionResult(
            status="project",
            project_id=project_id_str,
            project_name=proj.get("name", ""),
            project_description=proj.get("description", ""),
        )

    async def get_project_name(
        self,
        settings: AppSettings,
        user_id: str,
        project_id: str,
    ) -> str | None:
        """Return the name of a project owned by ``user_id``, or ``None``.

        Read-only lookup in ``chatprojects`` filtered by ``_id`` AND ``user``.
        Returns ``None`` when the client is not configured, the ID is not an
        ObjectId (e.g. a legacy on-disk folder), the project does not belong to
        the user, or the database fails.
        """
        if not self._client or not project_id:
            return None
        try:
            proj_obj_id = ObjectId(str(project_id)) if ObjectId is not None else project_id
        except Exception:
            return None
        try:
            proj = await asyncio.wait_for(
                self._client[self._db_name].chatprojects.find_one(
                    {"_id": proj_obj_id, "user": user_id},
                    projection={"_id": 0, "name": 1},
                ),
                timeout=self._timeout_sec,
            )
        except Exception as exc:
            logger.warning("failed to look up project name: %s", exc)
            return None
        if not proj:
            return None
        return proj.get("name") or None

    async def list_user_projects(
        self,
        settings: AppSettings,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """List all projects belonging to user_id in LibreChat MongoDB."""
        if not self._client:
            return []
        db = self._client[self._db_name]
        try:
            cursor = db.chatprojects.find(
                {"user": user_id},
                projection={"_id": 1, "name": 1, "description": 1},
            )
            projects = await asyncio.wait_for(
                cursor.to_list(length=100), timeout=self._timeout_sec
            )
            return [
                {
                    "id": str(p["_id"]),
                    "name": p.get("name", ""),
                    "description": p.get("description", ""),
                }
                for p in projects
            ]
        except Exception as exc:
            logger.warning("failed to list user projects from mongo: %s", exc)
            return []
