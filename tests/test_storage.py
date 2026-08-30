"""Storage layer tests — security + round-trip."""
from __future__ import annotations

import pytest

from personal_files_mcp import storage

USER = "5f8d3a4b2c1e0d0001a2b3c4"


# ---------------------------------------------------------------------------
# resolve(): 7 cases rejected, 3 valid
# ---------------------------------------------------------------------------


def test_resolve_absolute_path_rejected(cfg):
    with pytest.raises(storage.StorageError):
        storage.resolve(cfg, USER, "/etc/passwd")


def test_resolve_traversal_simple_rejected(cfg):
    with pytest.raises(storage.StorageError):
        storage.resolve(cfg, USER, "..")


def test_resolve_traversal_nested_rejected(cfg):
    with pytest.raises(storage.StorageError):
        storage.resolve(cfg, USER, "docs/../../etc")


def test_resolve_traversal_in_segment_rejected(cfg):
    with pytest.raises(storage.StorageError):
        storage.resolve(cfg, USER, "a/../b")


def test_resolve_null_byte_rejected(cfg):
    with pytest.raises(storage.StorageError):
        storage.resolve(cfg, USER, "docs/\x00/file.txt")


def test_resolve_control_char_rejected(cfg):
    with pytest.raises(storage.StorageError):
        storage.resolve(cfg, USER, "docs/\x01/file.txt")


def test_reserve_git_segment_rejected(cfg):
    with pytest.raises(storage.StorageError):
        storage.resolve(cfg, USER, ".git/config")


def test_reserve_ssh_segment_rejected(cfg):
    with pytest.raises(storage.StorageError):
        storage.resolve(cfg, USER, ".ssh/keys")


def test_reserve_system_segment_rejected(cfg):
    with pytest.raises(storage.StorageError):
        storage.resolve(cfg, USER, "_system/links.db")


def test_name_too_long_rejected(cfg):
    long_name = "a" * 300
    with pytest.raises(storage.StorageError):
        storage.resolve(cfg, USER, long_name)


def test_resolve_valid_simple(cfg):
    p = storage.resolve(cfg, USER, "docs/file.md")
    assert p == cfg.storage_root / USER / "docs" / "file.md"


def test_resolve_valid_empty(cfg):
    p = storage.resolve(cfg, USER, "")
    assert p == cfg.storage_root / USER


def test_resolve_valid_nested_dirs(cfg):
    p = storage.resolve(cfg, USER, "reports/2026/q1/summary.md")
    assert p == cfg.storage_root / USER / "reports" / "2026" / "q1" / "summary.md"


def test_resolve_symlink_escape_rejected(cfg, tmp_root):
    # Create a real file outside the user root and a symlink inside the user root that points to it.
    external = tmp_root / "external.txt"
    external.write_text("leaked")
    user_root = tmp_root / USER
    user_root.mkdir()
    (user_root / "leak").symlink_to(external)
    with pytest.raises(storage.StorageError):
        storage.resolve(cfg, USER, "leak")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_write_read_roundtrip(cfg):
    storage.write(cfg, USER, "docs/note.md", "héllo wörld".encode())
    assert storage.read(cfg, USER, "docs/note.md") == "héllo wörld".encode()


def test_update_requires_existing(cfg):
    with pytest.raises(storage.StorageError):
        storage.update(cfg, USER, "missing.txt", b"data")
    storage.write(cfg, USER, "exists.txt", b"v1")
    storage.update(cfg, USER, "exists.txt", b"v2")
    assert storage.read(cfg, USER, "exists.txt") == b"v2"


def test_delete_file_and_dir(cfg):
    storage.write(cfg, USER, "del.txt", b"x")
    storage.delete(cfg, USER, "del.txt")
    with pytest.raises(storage.StorageError):
        storage.read(cfg, USER, "del.txt")
    storage.write(cfg, USER, "dir/a.txt", b"a")
    storage.delete(cfg, USER, "dir")
    with pytest.raises(storage.StorageError):
        storage.read(cfg, USER, "dir/a.txt")


def test_list_recursive_pattern(cfg):
    storage.write(cfg, USER, "a.md", b"# A")
    storage.write(cfg, USER, "docs/b.md", b"# B")
    storage.write(cfg, USER, "docs/sub/c.txt", b"c")
    all_items = storage.list(cfg, USER, "", recursive=True)
    rels = {i.relative_path for i in all_items}
    assert "a.md" in rels
    assert "docs/b.md" in rels
    assert "docs/sub/c.txt" in rels

    only_md = storage.list(cfg, USER, "", recursive=True, pattern="*.md")
    rels_md = {i.relative_path for i in only_md}
    assert "a.md" in rels_md
    assert "docs/b.md" in rels_md
    assert not any(r.endswith(".txt") for r in rels_md)


def test_move_within_user(cfg):
    storage.write(cfg, USER, "src.txt", b"hello")
    storage.move(cfg, USER, "src.txt", "dst/sub/moved.txt")
    assert storage.read(cfg, USER, "dst/sub/moved.txt") == b"hello"
    with pytest.raises(storage.StorageError):
        storage.read(cfg, USER, "src.txt")


def test_stat_returns_metadata(cfg):
    storage.write(cfg, USER, "info.md", b"hello")
    info = storage.stat(cfg, USER, "info.md")
    assert info.size_bytes == 5
    assert info.is_directory is False
    assert info.name == "info.md"
    assert info.mime_type is not None


def test_stat_office_mime_types(cfg):
    expected = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pdf": "application/pdf",
        "eml": "message/rfc822",
    }
    for ext, mime in expected.items():
        storage.write(cfg, USER, f"office/f.{ext}", b"data")
        info = storage.stat(cfg, USER, f"office/f.{ext}")
        assert info.mime_type == mime, ext


def test_stat_unknown_extension_falls_back(cfg):
    storage.write(cfg, USER, "f.zzz", b"data")
    info = storage.stat(cfg, USER, "f.zzz")
    assert info.mime_type == "application/octet-stream"


def test_compute_sha256(cfg):
    storage.write(cfg, USER, "h.txt", b"abc")
    assert (
        storage.compute_sha256(cfg, USER, "h.txt")
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_umask_applied_to_written_files(cfg, tmp_root):
    storage.write(cfg, USER, "perm.txt", b"x")
    mode = (tmp_root / USER / "perm.txt").stat().st_mode & 0o777
    # Default umask 0o002 => mode should be 0o664 (group-writable)
    assert mode == 0o664
