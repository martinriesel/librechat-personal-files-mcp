"""Identity validation tests."""
from __future__ import annotations

import pytest

from personal_files_mcp import identity


def test_extract_valid_objectid():
    uid = identity.extract_user_id({"X-User-Id": "5f8d3a4b2c1e0d0001a2b3c4"})
    assert uid == "5f8d3a4b2c1e0d0001a2b3c4"


def test_extract_valid_generic():
    uid = identity.extract_user_id({"X-User-Id": "user_with_underscore-1"})
    assert uid == "user_with_underscore-1"


def test_extract_strips_whitespace():
    uid = identity.extract_user_id({"X-User-Id": "  5f8d3a4b2c1e0d0001a2b3c4  "})
    assert uid == "5f8d3a4b2c1e0d0001a2b3c4"


def test_extract_case_insensitive_header():
    uid = identity.extract_user_id({"x-user-id": "5f8d3a4b2c1e0d0001a2b3c4"})
    assert uid == "5f8d3a4b2c1e0d0001a2b3c4"


def test_missing_header_raises():
    with pytest.raises(identity.IdentityError) as exc:
        identity.extract_user_id({})
    assert exc.value.code == "missing_user_identity"


def test_empty_header_raises():
    with pytest.raises(identity.IdentityError) as exc:
        identity.extract_user_id({"X-User-Id": "   "})
    assert exc.value.code == "missing_user_identity"


def test_invalid_short():
    with pytest.raises(identity.IdentityError) as exc:
        identity.extract_user_id({"X-User-Id": "short"})
    assert exc.value.code == "invalid_user_identity"


def test_invalid_too_long():
    with pytest.raises(identity.IdentityError) as exc:
        identity.extract_user_id({"X-User-Id": "a" * 200})
    assert exc.value.code == "invalid_user_identity"


def test_invalid_special_chars():
    with pytest.raises(identity.IdentityError) as exc:
        identity.extract_user_id({"X-User-Id": "user@host!"})
    assert exc.value.code == "invalid_user_identity"


def test_custom_header_name():
    cfg = identity.IdentityConfig(header_name="X-Auth-User")
    uid = identity.extract_user_id({"X-AUTH-USER": "5f8d3a4b2c1e0d0001a2b3c4"}, cfg)
    assert uid == "5f8d3a4b2c1e0d0001a2b3c4"


def test_none_headers_mapping():
    with pytest.raises(identity.IdentityError):
        identity.extract_user_id(None)
