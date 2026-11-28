"""Adjacent empty deny ACEs must both be removed by one ACL repair."""
from __future__ import annotations

import ctypes
import errno
import grp
import os
from pathlib import Path
import pwd
import subprocess

import pytest

from subfleet import retention_fs as rfs
from tests.unit.rte_acl import add_acl, restored  # noqa: F401 - restored: the fixture
from tests.unit.test_rte_tmpdir_acl import scratch_in

pytestmark = pytest.mark.skipif(not rfs.ACL_AVAILABLE, reason="needs libSystem's ACL calls (macOS)")


def acl_shape(fd):
    """Return tags and permission masks only, keeping runtime identities private."""
    ctypes.set_errno(0)
    acl = rfs._acl_get_fd_np(fd, rfs._ACL_TYPE_EXTENDED)
    if not acl:
        assert ctypes.get_errno() in (0, errno.ENOENT), "could not read the test entry's ACL"
        return []
    try:
        shape = []
        position = rfs._ACL_FIRST_ENTRY
        entry = ctypes.c_void_p()
        while rfs._acl_get_entry(acl, position, ctypes.byref(entry)) == 0:
            position = rfs._ACL_NEXT_ENTRY
            tag = ctypes.c_int()
            mask = ctypes.c_uint64()
            assert rfs._acl_get_tag_type(entry, ctypes.byref(tag)) == 0
            assert rfs._acl_get_permset_mask_np(entry, ctypes.byref(mask)) == 0
            shape.append((tag.value, mask.value))
        return shape
    finally:
        rfs._acl_free(acl)


def add_runtime_principal_denials(path):
    """Resolve two distinct principals at runtime; never print their identities."""
    principals = ("user:" + pwd.getpwuid(os.getuid()).pw_name,
                  "group:" + grp.getgrgid(os.getgid()).gr_name)
    for principal in principals:
        try:
            add_acl(path, f"{principal} deny delete")
        except subprocess.CalledProcessError:
            pytest.fail("could not add a runtime principal's deny-delete ACE", pytrace=False)


def test_adjacent_delete_only_denials_are_both_removed_in_one_pass(restored, monkeypatch):
    """Deleting one empty ACE must not skip its now-adjacent deny-delete ACE."""
    scratch = scratch_in(restored)
    record = scratch.create("job/a1")
    entry = Path(record["path"]) / "locked"
    entry.write_text("synthetic payload")
    add_runtime_principal_denials(entry)
    inode = os.lstat(entry).st_ino
    fd = os.open(entry, rfs.O_FILE)
    try:
        assert acl_shape(fd) == [(rfs._ACL_EXTENDED_DENY, rfs._ACL_DELETE)] * 2
    finally:
        os.close(fd)
    with pytest.raises(PermissionError):
        os.unlink(entry)

    repairs = []
    real = rfs.clear_extended_acl

    def observed(fd, **kwargs):
        target = os.fstat(fd).st_ino == inode
        before = len(acl_shape(fd)) if target else None
        changed = real(fd, **kwargs)
        if target:
            repairs.append((before, len(acl_shape(fd))))
        return changed

    monkeypatch.setattr(rfs, "clear_extended_acl", observed)
    assert scratch._remove(record) is True
    assert repairs == [(2, 0)]
    assert scratch.presence(record) == "absent"
