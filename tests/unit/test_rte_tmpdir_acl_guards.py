"""The ACL repair for C-12.10 stays inside this user's allocation.

Real deny-delete ACLs make the ownership and temp-root guard assertions
observable; the crash-left tombstone reaches the final rmdir without a
rename being refused first (review R1 and R3).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from subfleet import attempt_tmp
from subfleet import retention_fs as rfs
from tests.unit.rte_acl import (
    DENY_DELETE, DENY_DELETE_CHILD, add_acl, has_acl,
    restored,  # noqa: F401 - the fixture restores ACLs even after a failed assertion
)


pytestmark = pytest.mark.skipif(not rfs.ACL_AVAILABLE, reason="needs libSystem's ACL calls (macOS)")


def test_a_crash_left_tombstone_never_clears_the_temp_roots_delete_child_acl(restored):
    """R3: only the allocation's ACL can change at the final rmdir.

    A recorded tombstone left by a crash skips the rename, so the temp
    root's deny-delete-child ACL first refuses the tombstone's rmdir.
    """
    root = restored / "user-temp"
    root.mkdir()
    scratch = attempt_tmp.AttemptTemp(restored / "store", root=root)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    tombstone = Path(scratch.tombstone_path(record))
    folder.rename(tombstone)
    add_acl(root, DENY_DELETE_CHILD)

    assert scratch._remove(record) is False
    assert has_acl(root), "the temp root is outside the allocation"
    assert tombstone.is_dir(), "the refused deletion leaves the recorded tombstone"
    assert scratch.presence(record) == "tombstoned"


def test_an_entry_owned_by_another_uid_keeps_its_delete_acl(restored, monkeypatch):
    """R1: a privileged caller must still check the entry's uid explicitly.

    The real process owns this file and could clear its ACL. Simulating a
    different retention uid isolates the guard from the kernel's ownership
    refusal, which would otherwise hide a missing check on a non-root host.
    """
    entry = restored / "locked"
    entry.write_text("synthetic scratch")
    add_acl(entry, DENY_DELETE)
    uid = entry.lstat().st_uid
    parent = os.open(restored, rfs.O_DIR)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "getuid", lambda: uid + 1)
            assert attempt_tmp._clear_blocking_acls(parent, entry.name, parent_inside=False) is False
    finally:
        os.close(parent)
    assert has_acl(entry)
    with pytest.raises(PermissionError):
        entry.unlink()


def test_a_parent_owned_by_another_uid_keeps_its_delete_child_acl(restored, monkeypatch):
    """The parent repair has the same uid guard as the entry repair."""
    directory = restored / "parent"
    directory.mkdir()
    entry = directory / "plain"
    entry.write_text("synthetic scratch")
    add_acl(directory, DENY_DELETE_CHILD)
    uid = directory.lstat().st_uid
    parent = os.open(directory, rfs.O_DIR)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "getuid", lambda: uid + 1)
            assert attempt_tmp._clear_blocking_acls(parent, entry.name, parent_inside=True) is False
    finally:
        os.close(parent)
    assert has_acl(directory)
    with pytest.raises(PermissionError):
        entry.unlink()
