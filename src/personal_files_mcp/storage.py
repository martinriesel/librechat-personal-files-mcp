"""Secure storage layer — sole filesystem gateway for user data.

Briefing §4.2: every filesystem operation for user data MUST go through this
module. The rest of the codebase does not call ``os``/``pathlib``/``shutil``
against user data; doing so is a code-review failure.

Public API (all take ``StorageConfig`` + ``user_id`` + relative path strings):
    * ``resolve(cfg, user_id, relative_path) -> Path``
    * ``read(cfg, user_id, relative_path) -> bytes``
    * ``write(cfg, user_id, relative_path, content) -> FileInfo``
    * ``update(cfg, user_id, relative_path, content) -> FileInfo``
    * ``delete(cfg, user_id, relative_path) -> None``
    * ``list(cfg, user_id, relative_path="", recursive=False, pattern=None) -> list[FileInfo]``
    * ``move(cfg, user_id, src, dst) -> None``
    * ``stat(cfg, user_id, relative_path) -> FileInfo``

Security contract enforced by ``resolve`` (briefing §4.2):
    1. Reject absolute paths (``/etc/passwd``).
    2. Reject ``..`` in any segment.
    3. Reject null bytes and control characters (< 0x20).
    4. Reject symlinks that escape the user root.
    5. Reject reserved segments (``.git``, ``.ssh``, ``_system``) — configurable.
    6. Enforce 255 chars per name, 1024 chars total.
    7. Parent directories created on ``write`` are kept under the user root.
"""
from __future__ import annotations

import fnmatch
import hashlib
import logging
import mimetypes
import os
import shutil
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

RESERVED_SEGMENTS: frozenset[str] = frozenset({".git", ".ssh", "_system"})
MAX_NAME_LEN = 255
MAX_TOTAL_PATH_LEN = 1024
MAX_FILE_BYTES = 20 * 1024 * 1024  # default mirror of MAX_FILE_SIZE_MB; may be raised by config
MAX_BINARY_BYTES = 50 * 1024 * 1024  # default mirror of MAX_BINARY_FILE_MB

_DEFAULT_MIMETYPE = "application/octet-stream"

# Explicit MIME mapping for extensions the stdlib ``mimetypes`` module misses
# on slim containers (e.g. ``docx``/``xlsx``/``pptx`` → ``None``). Consulted
# before ``mimetypes.guess_type`` so published/read Office files carry a real
# Content-Type instead of falling back to ``application/octet-stream``.
_KNOWN_MIMETYPES: dict[str, str] = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".docm": "application/vnd.ms-word.document.macroEnabled.12",
    ".dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    ".potx": "application/vnd.openxmlformats-officedocument.presentationml.template",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".eml": "message/rfc822",
    ".pdf": "application/pdf",
}


def _guess_mime(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    if mime is not None:
        return mime
    ext = Path(name).suffix.lower()
    return _KNOWN_MIMETYPES.get(ext, _DEFAULT_MIMETYPE)


class StorageError(Exception):
    """Raised for any storage-level failure (security, IO, configuration)."""


@dataclass
class FileInfo:
    """Metadata for a file or directory.

    ``sha256`` is intentionally optional and computed lazily by callers that
    need it (publishing, docindex sync).
    """

    user_id: str
    relative_path: str
    name: str
    is_directory: bool
    size_bytes: int = 0
    mime_type: str | None = None
    mtime: int = 0  # seconds since epoch
    sha256: str | None = field(default=None)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StorageConfig:
    """Runtime configuration for the storage layer."""

    storage_root: Path
    reserved_segments: frozenset[str] = RESERVED_SEGMENTS
    max_name_len: int = MAX_NAME_LEN
    max_total_path_len: int = MAX_TOTAL_PATH_LEN
    umask: int = 0o002
    follow_symlinks: bool = False
    max_file_bytes: int | None = None  # None -> MAX_FILE_BYTES
    max_binary_bytes: int | None = None  # None -> MAX_BINARY_BYTES

    def with_root(self, root: Path | str) -> StorageConfig:
        return StorageConfig(
            storage_root=Path(root),
            reserved_segments=self.reserved_segments,
            max_name_len=self.max_name_len,
            max_total_path_len=self.max_total_path_len,
            umask=self.umask,
            follow_symlinks=self.follow_symlinks,
            max_file_bytes=self.max_file_bytes,
            max_binary_bytes=self.max_binary_bytes,
        )


# ---------------------------------------------------------------------------
# resolve: the single chokepoint
# ---------------------------------------------------------------------------


def _is_reserved(name: str, reserved: Iterable[str]) -> bool:
    return name in reserved


def _check_chars(value: str, label: str) -> None:
    if "\x00" in value:
        raise StorageError(f"null byte in {label}")
    if any(ord(c) < 0x20 for c in value):
        raise StorageError(f"control character in {label}")


def _normalize_relative(relative_path: str) -> str:
    if not isinstance(relative_path, str):
        raise StorageError("relative_path must be a string")
    _check_chars(relative_path, "path")
    cleaned = relative_path.replace("\\", "/")
    if cleaned.startswith("/"):
        raise StorageError("absolute path rejected")
    # Reject any "..", even inside a segment like "..hidden"
    parts = [p for p in cleaned.split("/") if p != "" and p != "."]
    for p in parts:
        if p == "..":
            raise StorageError("path traversal '..' rejected")
    return "/".join(parts)


def resolve(
    cfg: StorageConfig, user_id: str, relative_path: str = ""
) -> Path:
    """Validate inputs and return the absolute, symlink-resolved path.

    The returned path is guaranteed to be inside ``cfg.storage_root / user_id``.
    It does NOT require the path to exist; use ``stat`` for that.
    """
    if not isinstance(user_id, str) or not user_id:
        raise StorageError("user_id is required")
    if any(ord(c) < 0x20 or c == "\x00" for c in user_id):
        raise StorageError("invalid user_id")
    if "/" in user_id or "\\" in user_id or user_id in (".", ".."):
        raise StorageError("invalid user_id")

    normalized = _normalize_relative(relative_path or "")
    parts = normalized.split("/") if normalized else []
    if len(parts) > 0 and sum(len(p) for p in parts) + len(parts) - 1 > cfg.max_total_path_len:
        raise StorageError("path too long")

    reserved = cfg.reserved_segments
    for part in parts:
        if len(part) > cfg.max_name_len:
            raise StorageError(f"name too long: {part[:32]}...")
        if _is_reserved(part, reserved):
            raise StorageError(f"reserved segment: {part}")

    user_root = (cfg.storage_root / user_id).resolve()
    base = (cfg.storage_root / user_id).resolve()
    if not str(user_root).startswith(str(cfg.storage_root.resolve())):
        raise StorageError("user_id escapes storage root")

    candidate = user_root
    for part in parts:
        candidate = candidate / part

    # Resolve symlinks / "..". On Python 3.12 Path.resolve(strict=False) handles missing leaves.
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise StorageError(f"cannot resolve path: {relative_path!r}: {exc}") from exc

    # The resolved path must remain inside the user root, even if symlinks exist on disk.
    base_with_sep = str(base) + os.sep
    if not (str(resolved) == str(base) or str(resolved).startswith(base_with_sep)):
        raise StorageError(f"path escapes user root: {relative_path!r}")

    return resolved


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def _ensure_parent(cfg: StorageConfig, user_id: str, target: Path) -> None:
    parent = target.parent
    # Defense in depth: even if the caller's relative_path was sanitized, double-check.
    if not str(parent.resolve(strict=False)).startswith(
        str((cfg.storage_root / user_id).resolve(strict=False)) + os.sep
    ) and parent.resolve(strict=False) != (cfg.storage_root / user_id).resolve(
        strict=False
    ):
        raise StorageError("parent directory escapes user root")
    parent.mkdir(parents=True, exist_ok=True)


def _apply_umask(mode: int, umask: int) -> int:
    return mode & ~umask


def _compute_sha256(path: Path, chunk: int = 65536) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _stat_info(cfg: StorageConfig, user_id: str, path: Path, rel: str) -> FileInfo:
    st = path.stat()
    is_dir = path.is_dir()
    size = 0 if is_dir else st.st_size
    mime = "inode/directory" if is_dir else _guess_mime(path.name)
    return FileInfo(
        user_id=user_id,
        relative_path=rel,
        name=path.name,
        is_directory=is_dir,
        size_bytes=size,
        mime_type=mime,
        mtime=int(st.st_mtime),
    )


def read(cfg: StorageConfig, user_id: str, relative_path: str) -> bytes:
    path = resolve(cfg, user_id, relative_path)
    if not path.exists() or path.is_dir():
        raise StorageError("file not found")
    return path.read_bytes()


def write(
    cfg: StorageConfig,
    user_id: str,
    relative_path: str,
    content: bytes,
    max_bytes: int | None = None,
) -> FileInfo:
    if not isinstance(content, (bytes, bytearray)):
        raise StorageError("content must be bytes")
    if max_bytes is not None:
        limit = max_bytes
    else:
        limit = cfg.max_file_bytes if cfg.max_file_bytes is not None else MAX_FILE_BYTES
    if len(content) > limit:
        raise StorageError("file too large")

    path = resolve(cfg, user_id, relative_path)
    _ensure_parent(cfg, user_id, path)
    path.write_bytes(content)
    os.chmod(path, _apply_umask(0o666, cfg.umask))
    return _stat_info(cfg, user_id, path, _rel(cfg, user_id, path))


def update(
    cfg: StorageConfig,
    user_id: str,
    relative_path: str,
    content: bytes,
    max_bytes: int | None = None,
) -> FileInfo:
    if not isinstance(content, (bytes, bytearray)):
        raise StorageError("content must be bytes")
    if max_bytes is not None:
        limit = max_bytes
    else:
        limit = cfg.max_file_bytes if cfg.max_file_bytes is not None else MAX_FILE_BYTES
    if len(content) > limit:
        raise StorageError("file too large")
    path = resolve(cfg, user_id, relative_path)
    if not path.exists() or path.is_dir():
        raise StorageError("file does not exist")
    path.write_bytes(content)
    os.chmod(path, _apply_umask(0o666, cfg.umask))
    return _stat_info(cfg, user_id, path, _rel(cfg, user_id, path))


def delete(cfg: StorageConfig, user_id: str, relative_path: str) -> None:
    path = resolve(cfg, user_id, relative_path)
    if not path.exists():
        raise StorageError("path does not exist")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def list(
    cfg: StorageConfig,
    user_id: str,
    relative_path: str = "",
    recursive: bool = False,
    pattern: str | None = None,
) -> list[FileInfo]:
    path = resolve(cfg, user_id, relative_path)
    if not path.exists() or not path.is_dir():
        return []
    entries: list[FileInfo] = []
    for child in sorted(path.iterdir()):
        if child.name.startswith("."):
            continue
        is_dir = child.is_dir()
        # Pattern filter only applies to files; directories are always traversed
        # so that nested matches are not lost.
        if pattern and not is_dir and not fnmatch.fnmatch(child.name, pattern):
            continue
        rel = _rel(cfg, user_id, child)
        entries.append(_stat_info(cfg, user_id, child, rel))
        if recursive and is_dir:
            entries.extend(
                list(cfg, user_id, rel, recursive=True, pattern=pattern)
            )
    return entries


def move(cfg: StorageConfig, user_id: str, src: str, dst: str) -> None:
    src_path = resolve(cfg, user_id, src)
    dst_path = resolve(cfg, user_id, dst)
    if not src_path.exists():
        raise StorageError("source does not exist")
    if dst_path.exists():
        raise StorageError("destination already exists")
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_path), str(dst_path))


def stat(cfg: StorageConfig, user_id: str, relative_path: str) -> FileInfo:
    path = resolve(cfg, user_id, relative_path)
    if not path.exists():
        raise StorageError("path does not exist")
    return _stat_info(cfg, user_id, path, _rel(cfg, user_id, path))


def compute_sha256(
    cfg: StorageConfig, user_id: str, relative_path: str
) -> str:
    path = resolve(cfg, user_id, relative_path)
    if not path.exists() or path.is_dir():
        raise StorageError("file not found")
    return _compute_sha256(path)


def _rel(cfg: StorageConfig, user_id: str, abs_path: Path) -> str:
    user_root = (cfg.storage_root / user_id).resolve(strict=False)
    try:
        rel = abs_path.relative_to(user_root)
    except ValueError as exc:
        raise StorageError("path outside user root") from exc
    return "" if rel == Path(".") else str(rel)
