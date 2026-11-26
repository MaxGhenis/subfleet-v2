"""Helpers for the retention tree-error tests (D-SURVEY-EACCES1, D-TMPDIR-ACL1).

Every tree that gets an ACL, an immutable flag, or a mode without owner read,
write or search is restored by `restore` (`chmod -R -N`, `chflags -R nouchg`,
owner rwx on directories), so pytest's basetemp cleanup and later runs can
remove it. `restored` is the fixture: its finalizer restores the case folder
even when the test failed, then asserts the folder removes cleanly.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from subfleet import retention_fs as rfs

DENY_DELETE = "everyone deny delete"
DENY_DELETE_CHILD = "everyone deny delete_child"
#: An ACL that refuses no unlink: removal must leave such an entry's ACL alone.
DENY_WRITEATTR = "everyone deny writeattr"


def add_acl(path: Path, rule: str = DENY_DELETE) -> None:
    """`chmod -h +a`: on the entry itself, never a symlink's target."""
    subprocess.run(["/bin/chmod", "-h", "+a", rule, str(path)], check=True, capture_output=True)


def has_acl(path: Path) -> bool:
    """Whether the entry itself (a symlink itself) carries an extended ACL."""
    st = os.lstat(path)
    flags = (os.O_RDONLY | os.O_SYMLINK) if stat.S_ISLNK(st.st_mode) else (rfs.O_DIR if stat.S_ISDIR(st.st_mode)
                                                                           else rfs.O_FILE)
    try:
        fd = os.open(path, flags)
    except PermissionError:
        listing = subprocess.run(["/bin/ls", "-led", str(path)], capture_output=True, text=True).stdout
        return "+" in listing.split()[0] if listing else False
    try:
        return rfs.has_extended_acl(fd)
    finally:
        os.close(fd)


def restore(path: Path) -> None:
    """Make a test tree removable again, whatever the test left in it."""
    if not os.path.lexists(path):
        return
    # Traversable first (pre-order), so the passes below reach every entry.
    subprocess.run(["/bin/chmod", "-R", "-P", "u+rwX", str(path)], capture_output=True)
    # The flag before the ACLs: an immutable entry's ACL cannot be changed.
    subprocess.run(["/usr/bin/chflags", "-R", "-P", "nouchg", str(path)], capture_output=True)
    subprocess.run(["/bin/chmod", "-R", "-P", "-N", str(path)], capture_output=True)
    subprocess.run(["/bin/chmod", "-R", "-P", "u+rwX", str(path)], capture_output=True)
    for directory, dirnames, filenames in os.walk(path):
        for name in [*dirnames, *filenames]:
            full = os.path.join(directory, name)
            if os.path.islink(full):
                # `chmod -R -N` acts on a link's target, never the link's own ACL.
                subprocess.run(["/bin/chmod", "-h", "-N", full], capture_output=True)
            elif name in dirnames:
                os.chmod(full, 0o700)


def removes_cleanly(path: Path) -> bool:
    restore(path)
    shutil.rmtree(path)
    return not os.path.lexists(path)


@pytest.fixture
def restored(tmp_path):
    """A case folder restored in teardown even when the test fails; the
    finalizer asserts it then removes cleanly."""
    case = tmp_path / "case"
    case.mkdir()
    yield case
    assert removes_cleanly(case), f"{case} did not remove cleanly"


def outside_state(path: Path) -> dict:
    """Mode, ACL, flags, content and names of everything under `path` (never followed)."""
    state = {}
    for directory, dirnames, filenames in os.walk(path):
        for name in [*dirnames, *filenames, "."]:
            full = os.path.normpath(os.path.join(directory, name))
            st = os.lstat(full)
            content = None
            if stat.S_ISREG(st.st_mode):
                content = Path(full).read_bytes() if st.st_mode & 0o400 else ("unreadable", st.st_size)
            elif stat.S_ISLNK(st.st_mode):
                content = os.readlink(full)
            state[os.path.relpath(full, path)] = (st.st_mode, st.st_flags, st.st_ino, has_acl(Path(full)), content)
    return state
