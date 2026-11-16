"""Tests for project_context and diagnostics."""
from __future__ import annotations

import asyncio
import json

import pytest

from personal_files_mcp import diagnostics, project_context
from personal_files_mcp.config import AppSettings


def test_extract_conversation_id_valid():
    headers = {"X-Conversation-Id": "f7e4f9d5-350c-4402-944b-f77ae2baf61c"}
    conv_id, status = project_context.extract_conversation_id(headers)
    assert status == "valid"
    assert conv_id == "f7e4f9d5-350c-4402-944b-f77ae2baf61c"


def test_extract_conversation_id_case_insensitive():
    headers = {"x-conversation-id": "conv-12345-67890"}
    conv_id, status = project_context.extract_conversation_id(headers)
    assert status == "valid"
    assert conv_id == "conv-12345-67890"


def test_extract_conversation_id_missing():
    headers = {"X-Other": "value"}
    conv_id, status = project_context.extract_conversation_id(headers)
    assert status == "missing"
    assert conv_id is None

    conv_id2, status2 = project_context.extract_conversation_id(None)
    assert status2 == "missing"
    assert conv_id2 is None


def test_extract_conversation_id_empty():
    headers = {"X-Conversation-Id": "   "}
    conv_id, status = project_context.extract_conversation_id(headers)
    assert status == "empty"
    assert conv_id is None


def test_extract_conversation_id_literal_placeholder():
    headers = {"X-Conversation-Id": "{{LIBRECHAT_BODY_CONVERSATIONID}}"}
    conv_id, status = project_context.extract_conversation_id(headers)
    assert status == "placeholder"
    assert conv_id is None


def test_extract_conversation_id_invalid_chars():
    headers = {"X-Conversation-Id": "../../../etc/passwd"}
    conv_id, status = project_context.extract_conversation_id(headers)
    assert status == "invalid"
    assert conv_id is None


def test_extract_conversation_id_too_long():
    headers = {"X-Conversation-Id": "a" * 129}
    conv_id, status = project_context.extract_conversation_id(headers)
    assert status == "invalid"
    assert conv_id is None


@pytest.mark.asyncio
async def test_request_context_concurrency_isolation():
    async def worker(idx: int):
        data = project_context.RequestContextData(
            user_id=f"user_{idx}",
            conversation_id=f"conv_{idx}",
            request_id=f"req_{idx}",
        )
        token = project_context.set_request_context(data)
        try:
            await asyncio.sleep(0.01)
            active = project_context.get_request_context()
            assert active is not None
            assert active.user_id == f"user_{idx}"
            assert active.conversation_id == f"conv_{idx}"
            assert active.request_id == f"req_{idx}"
        finally:
            project_context.reset_request_context(token)

    tasks = [worker(i) for i in range(20)]
    await asyncio.gather(*tasks)


def test_diagnostics_logging_sanitizes_secrets(monkeypatch):
    settings = AppSettings(
        personal_files_log_level="INFO",
        personal_files_context_diagnostics=True,
    )

    logged = []

    def mock_info(msg):
        logged.append(msg)

    monkeypatch.setattr(diagnostics.logger, "info", mock_info)

    diagnostics.log_context_event(
        settings,
        event="test.event",
        request_id="req123",
        conversation_id="conv-123",
        jwt_secret="super-secret",
        password="secret-password",
        mongo_uri="mongodb://secret@localhost",
    )

    assert len(logged) == 1
    data = json.loads(logged[0])
    assert data["event"] == "test.event"
    assert data["request_id"] == "req123"
    assert data["conversation_id"] == "conv-123"
    assert "jwt_secret" not in data
    assert "password" not in data
    assert "mongo_uri" not in data


def test_diagnostics_disabled_when_flag_false(monkeypatch):
    settings = AppSettings(
        personal_files_log_level="INFO",
        personal_files_context_diagnostics=False,
    )

    logged = []
    monkeypatch.setattr(diagnostics.logger, "info", lambda msg: logged.append(msg))

    diagnostics.log_context_event(
        settings,
        event="test.event",
        request_id="req123",
        conversation_id="conv-123",
    )

    assert len(logged) == 0
