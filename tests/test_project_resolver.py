"""Tests for project_resolver."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from bson import ObjectId
except ImportError:
    class ObjectId:  # type: ignore
        def __init__(self, val=None):
            self.val = str(val or "609c1b2c3d4e5f60718293a4")

        def __str__(self):
            return self.val

        def __eq__(self, other):
            return str(self) == str(other)

from personal_files_mcp.config import AppSettings
from personal_files_mcp.project_resolver import ProjectResolver

USER_ID = "5f8d3a4b2c1e0d0001a2b3c4"
OTHER_USER = "609c1b2c3d4e5f60718293a4"
CONV_ID = "conv-uuid-1234"
PROJECT_OID = ObjectId()


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(
        personal_files_log_level="INFO",
        personal_files_context_diagnostics=True,
    )


def _build_mock_client(conv_doc=None, proj_doc=None):
    client = MagicMock()
    db = MagicMock()
    client.__getitem__.return_value = db

    conv_collection = MagicMock()
    conv_collection.find_one = AsyncMock(return_value=conv_doc)
    db.conversations = conv_collection

    proj_collection = MagicMock()
    proj_collection.find_one = AsyncMock(return_value=proj_doc)
    db.chatprojects = proj_collection

    return client


@pytest.mark.asyncio
async def test_resolve_success(settings):
    client = _build_mock_client(
        conv_doc={"chatProjectId": str(PROJECT_OID)},
        proj_doc={"_id": PROJECT_OID, "name": "Alpha Project", "description": "Core desc"},
    )
    resolver = ProjectResolver(client)
    res = await resolver.resolve(settings, user_id=USER_ID, conversation_id=CONV_ID)

    assert res.status == "project"
    assert res.project_id == str(PROJECT_OID)
    assert res.project_name == "Alpha Project"
    assert res.project_description == "Core desc"
    d = res.as_dict(CONV_ID)
    assert d["status"] == "project"
    assert d["project"]["id"] == str(PROJECT_OID)
    assert d["project"]["name"] == "Alpha Project"


@pytest.mark.asyncio
async def test_resolve_no_project(settings):
    client = _build_mock_client(
        conv_doc={"chatProjectId": None},
    )
    resolver = ProjectResolver(client)
    res = await resolver.resolve(settings, user_id=USER_ID, conversation_id=CONV_ID)

    assert res.status == "no_project"
    d = res.as_dict(CONV_ID)
    assert d["status"] == "no_project"


@pytest.mark.asyncio
async def test_resolve_race_condition_retry_success(settings):
    client = MagicMock()
    db = MagicMock()
    client.__getitem__.return_value = db

    conv_collection = MagicMock()
    # First attempt: None, second attempt: document
    conv_collection.find_one = AsyncMock(
        side_effect=[None, {"chatProjectId": str(PROJECT_OID)}]
    )
    db.conversations = conv_collection

    proj_collection = MagicMock()
    proj_collection.find_one = AsyncMock(
        return_value={"_id": PROJECT_OID, "name": "Retry Project"}
    )
    db.chatprojects = proj_collection

    resolver = ProjectResolver(client)
    res = await resolver.resolve(settings, user_id=USER_ID, conversation_id=CONV_ID)

    assert res.status == "project"
    assert res.project_name == "Retry Project"
    assert conv_collection.find_one.call_count == 2


@pytest.mark.asyncio
async def test_resolve_conversation_unavailable_after_retries(settings):
    client = _build_mock_client(conv_doc=None)
    resolver = ProjectResolver(client)
    res = await resolver.resolve(settings, user_id=USER_ID, conversation_id=CONV_ID)

    assert res.status == "conversation_unavailable"
    assert res.retry_recommended is True
    d = res.as_dict(CONV_ID)
    assert d["status"] == "conversation_unavailable"
    assert d["retry_recommended"] is True


@pytest.mark.asyncio
async def test_resolve_project_unavailable_when_project_missing(settings):
    client = _build_mock_client(
        conv_doc={"chatProjectId": str(PROJECT_OID)},
        proj_doc=None,
    )
    resolver = ProjectResolver(client)
    res = await resolver.resolve(settings, user_id=USER_ID, conversation_id=CONV_ID)

    assert res.status == "project_unavailable"


@pytest.mark.asyncio
async def test_resolve_project_unavailable_when_invalid_objectid(settings):
    client = _build_mock_client(
        conv_doc={"chatProjectId": "invalid-non-hex-id"},
    )
    resolver = ProjectResolver(client)
    res = await resolver.resolve(settings, user_id=USER_ID, conversation_id=CONV_ID)

    assert res.status == "project_unavailable"


@pytest.mark.asyncio
async def test_resolve_timeout(settings):
    client = MagicMock()
    db = MagicMock()
    client.__getitem__.return_value = db

    async def slow_find(*args, **kwargs):
        await asyncio.sleep(0.5)
        return None

    conv_collection = MagicMock()
    conv_collection.find_one = AsyncMock(side_effect=slow_find)
    db.conversations = conv_collection

    # Short timeout
    resolver = ProjectResolver(client, timeout_ms=50)
    res = await resolver.resolve(settings, user_id=USER_ID, conversation_id=CONV_ID)

    assert res.status == "resolver_unavailable"
    assert res.error == "database_timeout"


@pytest.mark.asyncio
async def test_resolve_client_none(settings):
    resolver = ProjectResolver(None)
    res = await resolver.resolve(settings, user_id=USER_ID, conversation_id=CONV_ID)

    assert res.status == "resolver_unavailable"
    assert res.error == "mongo_not_configured"


@pytest.mark.asyncio
async def test_resolve_context_missing(settings):
    resolver = ProjectResolver(MagicMock())
    res = await resolver.resolve(settings, user_id=USER_ID, conversation_id=None, conv_status="missing")

    assert res.status == "context_missing"


@pytest.mark.asyncio
async def test_resolve_context_invalid_placeholder(settings):
    resolver = ProjectResolver(MagicMock())
    res = await resolver.resolve(
        settings, user_id=USER_ID, conversation_id=None, conv_status="placeholder"
    )

    assert res.status == "context_invalid"
