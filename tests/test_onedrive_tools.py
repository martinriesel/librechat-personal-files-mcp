from __future__ import annotations

import asyncio
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from personal_files_mcp import storage
from personal_files_mcp.app import create_app, register_mcp_tools
from personal_files_mcp.config import AppSettings
from personal_files_mcp.onedrive_tools import register_onedrive_tools
from personal_files_mcp.runtime import AppContext


def test_local_catalog_has_no_onedrive_tools():
    mcp = FastMCP("test")
    register_mcp_tools(mcp)

    names = {tool.name for tool in asyncio.run(mcp.list_tools())}

    assert "list_files" in names
    assert not any(name.startswith("onedrive_") for name in names)


def test_onedrive_catalog_registers_full_tool_family():
    mcp = FastMCP("test")
    register_mcp_tools(mcp)
    register_onedrive_tools(mcp)

    names = {tool.name for tool in asyncio.run(mcp.list_tools())}

    assert {
        "onedrive_login",
        "onedrive_auth_status",
        "onedrive_logout",
        "onedrive_list_files",
        "onedrive_list_files_page",
        "onedrive_read_file",
        "onedrive_write_file",
        "onedrive_save_documentation",
        "onedrive_index_document",
        "onedrive_publish_file",
        "onedrive_get_direct_link",
        "onedrive_get_download_link",
    } <= names
    assert "list_files" in names


def test_complete_catalog_includes_all_local_storage_mirrors():
    mcp = FastMCP("test")
    register_onedrive_tools(mcp)

    names = {tool.name for tool in asyncio.run(mcp.list_tools())}

    assert {
        "onedrive_list_files",
        "onedrive_read_file",
        "onedrive_read_multiple_files",
        "onedrive_write_file",
        "onedrive_write_binary_file",
        "onedrive_resume_upload",
        "onedrive_read_binary_file",
        "onedrive_update_file",
        "onedrive_edit_file",
        "onedrive_delete_file",
        "onedrive_move_file",
        "onedrive_get_file_info",
        "onedrive_save_documentation",
        "onedrive_update_documentation",
        "onedrive_get_document_metadata",
        "onedrive_search_knowledge",
        "onedrive_index_document",
        "onedrive_remove_from_knowledge",
        "onedrive_get_index_status",
        "onedrive_publish_file",
        "onedrive_unpublish_file",
        "onedrive_get_public_link",
        "onedrive_list_public_links",
        "onedrive_get_direct_link",
    } <= names


def test_create_app_registers_onedrive_tools_only_when_enabled(tmp_path):
    settings = AppSettings(
        storage_root=str(tmp_path),
        registry_db=str(tmp_path / "_system" / "links.db"),
        storage_providers=frozenset({"local"}),
    )
    app = create_app(
        AppContext(settings, storage.StorageConfig(storage_root=Path(tmp_path)))
    )

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "X-User-Id": "user_123456",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert response.status_code == 200
    assert "onedrive_login" not in response.text


def test_create_app_registers_onedrive_catalog_when_enabled(tmp_path):
    settings = AppSettings(
        storage_root=str(tmp_path),
        registry_db=str(tmp_path / "_system" / "links.db"),
        jwt_secret="j" * 40,
        storage_providers=frozenset({"local", "onedrive"}),
        azure_tenant_id="tenant-guid",
        azure_client_id="client-guid",
        azure_client_secret="client-secret",
        azure_scopes=("Files.ReadWrite.AppFolder", "User.Read", "offline_access"),
        azure_redirect_uri="https://chat.example.test/auth/azure/callback",
        onedrive_token_encryption_key=Fernet.generate_key().decode("ascii"),
        onedrive_max_file_size_mb=10,
        onedrive_viewer_base_url="https://chat.example.test/onedrive/view",
    )
    app = create_app(
        AppContext(settings, storage.StorageConfig(storage_root=Path(tmp_path)))
    )

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "X-User-Id": "user_123456",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert response.status_code == 200
    assert "onedrive_login" in response.text
    assert "onedrive_get_direct_link" in response.text


def test_onedrive_tool_uses_request_user_identity():
    from types import SimpleNamespace

    from personal_files_mcp.onedrive_tools import _context

    expected = "5f8d3a4b2c1e0d0001a2b3c4"
    app_context = SimpleNamespace(settings=AppSettings(user_header="X-User-Id"))
    fake_context = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"X-User-Id": expected})
        ),
        fastmcp=SimpleNamespace(_app_context=app_context),
    )

    user_id, _ = _context(fake_context)

    assert user_id == expected
