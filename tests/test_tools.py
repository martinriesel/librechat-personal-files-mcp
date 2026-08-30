"""Tool tests: storage + documentation, structured error contract."""
from __future__ import annotations

import json

from personal_files_mcp import storage, tools

USER = "5f8d3a4b2c1e0d0001a2b3c4"


def _ok_payload(s: str) -> dict:
    assert not s.startswith("ERROR:"), s
    return json.loads(s)


def test_list_files_empty(cfg):
    out = tools.list_files(cfg, USER)
    payload = _ok_payload(out)
    assert payload["count"] == 0


def test_write_and_read_file(cfg):
    out = tools.write_file(cfg, USER, "docs/note.md", "héllo wörld")
    payload = _ok_payload(out)
    assert payload["file"]["size_bytes"] == len("héllo wörld".encode())
    out = tools.read_file(cfg, USER, "docs/note.md")
    payload = _ok_payload(out)
    assert payload["content"] == "héllo wörld"
    assert payload["is_binary"] is False


def test_read_binary_returns_is_binary_true(cfg):
    from personal_files_mcp import storage
    storage.write(cfg, USER, "blob.bin", b"\x80\x81\x82\x83")
    out = tools.read_file(cfg, USER, "blob.bin")
    payload = _ok_payload(out)
    assert payload["is_binary"] is True
    assert payload["content"] is None


def test_update_requires_existing(cfg):
    out = tools.update_file(cfg, USER, "missing.txt", "data")
    assert out.startswith("ERROR:")
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "update_failed"


def test_delete_then_read(cfg):
    tools.write_file(cfg, USER, "del.txt", "x")
    out = tools.delete_file(cfg, USER, "del.txt")
    _ok_payload(out)
    out = tools.read_file(cfg, USER, "del.txt")
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "not_found"


def test_move_file(cfg):
    tools.write_file(cfg, USER, "src.txt", "hi")
    out = tools.move_file(cfg, USER, "src.txt", "sub/moved.txt")
    payload = _ok_payload(out)
    assert payload["moved"]["dst"] == "sub/moved.txt"
    out = tools.read_file(cfg, USER, "sub/moved.txt")
    payload = _ok_payload(out)
    assert payload["content"] == "hi"


def test_get_file_info_includes_documentation(cfg):
    tools.save_documentation(cfg, USER, "guide.md", "# Guide\nline2", title="Guide")
    out = tools.get_file_info(cfg, USER, "docs/guide.md")
    payload = _ok_payload(out)
    assert "documentation" in payload
    assert payload["documentation"]["title"] == "Guide"


def test_save_documentation_rejects_traversal(cfg):
    out = tools.save_documentation(cfg, USER, "../escape.md", "evil")
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "invalid_filename"


def test_save_documentation_rejects_hidden(cfg):
    out = tools.save_documentation(cfg, USER, ".hidden.md", "x")
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "invalid_filename"


def test_update_documentation_requires_existing(cfg):
    out = tools.update_documentation(cfg, USER, "absent.md", "x")
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "not_found"


def test_get_document_metadata_returns_entry(cfg):
    tools.save_documentation(cfg, USER, "guide.md", "# Guide", title="Guide", description="d")
    out = tools.get_document_metadata(cfg, USER, "guide.md")
    payload = _ok_payload(out)
    assert payload["title"] == "Guide"
    assert payload["description"] == "d"
    assert payload["indexing_status"] == "not_indexed"


def test_path_traversal_via_write_returns_error(cfg):
    out = tools.write_file(cfg, USER, "../escape.txt", "x")
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] in ("write_failed", "invalid_path")


def test_write_rejects_non_string_content(cfg):
    out = tools.write_file(cfg, USER, "x.txt", 12345)  # type: ignore[arg-type]
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "invalid_content"


def test_write_too_large_returns_error(cfg, monkeypatch):
    from personal_files_mcp import storage
    monkeypatch.setattr(storage, "MAX_FILE_BYTES", 5)
    out = tools.write_file(cfg, USER, "big.txt", "0123456789")
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "write_failed"


def test_edit_file_applies_replacements(cfg):
    tools.write_file(cfg, USER, "notes/edit.txt", "hello world\nsecond line\n")
    out = tools.edit_file(
        cfg,
        USER,
        "notes/edit.txt",
        [{"oldText": "hello", "newText": "hello changed"}],
    )
    payload = _ok_payload(out)
    assert payload["edits_applied"] == 1
    out = tools.read_file(cfg, USER, "notes/edit.txt")
    payload = _ok_payload(out)
    assert "hello changed world" in payload["content"]
    assert "hello world" not in payload["content"]


def test_edit_file_no_match_returns_error(cfg):
    tools.write_file(cfg, USER, "notes/edit.txt", "some content")
    out = tools.edit_file(
        cfg, USER, "notes/edit.txt", [{"oldText": "does-not-exist", "newText": "x"}]
    )
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "no_match"


def test_edit_file_ambiguous_match_returns_error(cfg):
    tools.write_file(cfg, USER, "notes/edit.txt", "repeated repeated")
    out = tools.edit_file(
        cfg, USER, "notes/edit.txt", [{"oldText": "repeated", "newText": "x"}]
    )
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "ambiguous_match"


def test_edit_file_traversal_rejected(cfg):
    out = tools.edit_file(
        cfg, USER, "../escape.txt", [{"oldText": "a", "newText": "b"}]
    )
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "not_found"


def test_read_multiple_files(cfg):
    tools.write_file(cfg, USER, "a.txt", "conteudo A")
    tools.write_file(cfg, USER, "b.txt", "conteudo B")
    out = tools.read_multiple_files(cfg, USER, ["a.txt", "b.txt", "inexistente.txt"])
    payload = _ok_payload(out)
    assert payload["files"]["a.txt"]["content"] == "conteudo A"
    assert payload["files"]["b.txt"]["content"] == "conteudo B"
    assert "error" in payload["files"]["inexistente.txt"]


def test_read_multiple_files_traversal_rejected(cfg):
    out = tools.read_multiple_files(cfg, USER, ["../escape.txt"])
    payload = _ok_payload(out)
    assert "error" in payload["files"]["../escape.txt"]


# ---------------------------------------------------------------------------
# Binary tools
# ---------------------------------------------------------------------------


def test_write_binary_and_read_roundtrip(cfg):
    import base64

    payload = b"\x50\x4b\x03\x04" + b"\x00" * 100  # fake PKZIP/office bytes
    out = tools.write_binary_file(cfg, USER, "office/report.docx", base64.b64encode(payload).decode())
    info = _ok_payload(out)
    assert info["size_bytes"] == len(payload)
    assert info["file"]["relative_path"] == "office/report.docx"
    assert info["indexed"] is False

    out = tools.read_binary_file(cfg, USER, "office/report.docx")
    data = _ok_payload(out)
    assert base64.b64decode(data["content_base64"]) == payload
    assert data["is_binary"] is True
    assert data["size_bytes"] == len(payload)
    assert data["sha256"] == info["sha256"]


def test_read_binary_works_for_text_files(cfg):
    import base64

    tools.write_file(cfg, USER, "notes/hello.txt", "olá mundo")
    out = tools.read_binary_file(cfg, USER, "notes/hello.txt")
    data = _ok_payload(out)
    assert base64.b64decode(data["content_base64"]).decode() == "olá mundo"


def test_write_binary_rejects_invalid_base64(cfg):
    out = tools.write_binary_file(cfg, USER, "office/x.docx", "!!!not-base64!!!")
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "invalid_base64"


def test_write_binary_rejects_empty(cfg):
    out = tools.write_binary_file(cfg, USER, "office/x.docx", "")
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] in ("invalid_base64", "invalid_content")


def test_write_binary_too_large_uses_binary_limit(cfg):
    import base64

    small_cfg = storage.StorageConfig(
        storage_root=cfg.storage_root,
        max_binary_bytes=4,
    )
    out = tools.write_binary_file(
        small_cfg, USER, "office/big.docx", base64.b64encode(b"0123456789").decode()
    )
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "write_failed"


def test_write_binary_allows_larger_than_text_limit(cfg):
    import base64

    # 20 MB default text limit would reject this 100 KB binary; the binary
    # limit (default 50 MB) accepts it.
    small_text_cfg = storage.StorageConfig(
        storage_root=cfg.storage_root,
        max_file_bytes=1024,
        max_binary_bytes=2 * 1024 * 1024,
    )
    payload = b"x" * (2048)
    out = tools.write_binary_file(
        small_text_cfg, USER, "office/ok.docx", base64.b64encode(payload).decode()
    )
    assert not out.startswith("ERROR:"), out


def test_write_binary_traversal_rejected(cfg):
    import base64

    out = tools.write_binary_file(
        cfg, USER, "../escape.docx", base64.b64encode(b"x").decode()
    )
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] in ("write_failed", "invalid_path")


def test_read_binary_missing_file(cfg):
    out = tools.read_binary_file(cfg, USER, "office/nope.docx")
    body = json.loads(out[len("ERROR: "):])
    assert body["error"] == "not_found"
