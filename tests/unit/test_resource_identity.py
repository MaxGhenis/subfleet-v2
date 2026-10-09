"""Comparison keys are stable; opaque native ids retain their exact identity."""
from pathlib import Path
import errno
import tempfile
import unicodedata

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import folders, resource_leases
from subfleet.conversations.store import canonical_native


@settings(max_examples=80, deadline=None, derandomize=True, database=None)
@given(name=st.text(alphabet="abcABCéÉΐİß012-_", min_size=1, max_size=15),
       parent=st.booleans())
def test_path_identity_is_idempotent_and_deterministic_for_files_and_parents(name, parent):
    with tempfile.TemporaryDirectory(prefix="identity-key-") as directory:
        path = Path(directory) / name
        if parent:
            path.mkdir()
            path = path / "result.md"
        first = folders.identity(path)
        assert folders.identity(path) == first
        assert folders.identity(first) == first


def test_case_sensitive_volume_preserves_distinct_case_and_normalizes_unicode(tmp_path, monkeypatch):
    monkeypatch.setattr(folders, "_case_sensitive", lambda _: True)
    assert folders.identity(tmp_path / "Absent-A") != folders.identity(tmp_path / "Absent-a")
    assert folders.identity(tmp_path / "café") == folders.identity(tmp_path / unicodedata.normalize("NFD", "café"))


@pytest.mark.parametrize("left,right", [("a", "b"), ("café", "cafe"), ("A", "a"),
                                        ("café", "cafe\u0301"), ("a", "a/"), ("a", "x/../a")])
def test_opaque_native_ids_are_distinct_and_stable(left, right):
    # Case, normalization and path syntax are aliases only for their resource
    # kind: opaque session ids are not UUIDs and are not filesystem paths.
    assert canonical_native(left) == left != right == canonical_native(right)
    for value in (left, right):
        key = resource_leases.native_key("claude", value)
        assert resource_leases.canonical_native_key(key) == key
        assert canonical_native(canonical_native(value)) == value


@settings(max_examples=80, deadline=None, derandomize=True, database=None)
@given(value=st.uuids(), upper=st.booleans(), form=st.sampled_from(["hyphenated", "braced", "urn", "hex"]))
def test_native_keys_are_idempotent_and_provider_scoped(value, upper, form):
    session = value.hex if form == "hex" else str(value)
    session = session.upper() if upper else session
    if form == "braced":
        session = "{" + session + "}"
    elif form == "urn":
        session = "urn:uuid:" + session
    key = resource_leases.native_key("claude", session)
    assert key == resource_leases.native_key("claude", str(value))
    assert resource_leases.canonical_native_key(key) == key
    assert resource_leases.native_key("codex", session) != key


@settings(max_examples=32, deadline=None, derandomize=True, database=None)
@given(name=st.text(alphabet="abcABCéΐİßı\u200b\u0345012-_", min_size=1, max_size=12),
       code=st.sampled_from([errno.EACCES, errno.EIO, errno.ELOOP, errno.ETIMEDOUT]))
def test_unavailable_identity_never_raises_and_preserves_exact_string(name, code):
    with tempfile.TemporaryDirectory(prefix="identity-fallback-") as directory, pytest.MonkeyPatch.context() as patch:
        def unavailable(_):
            raise OSError(code, "fixture")
        patch.setattr(folders, "_case_sensitive", unavailable)
        path = str(Path(directory) / name)
        assert folders.identity(path) == path
        assert folders.identity(path) == folders.identity(path)
