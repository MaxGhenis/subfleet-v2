"""The directory rename rules of the macOS releases subfleet runs on (D-TMP1-P3 r2).

macOS 26 renames a directory whatever its own mode, within one parent. macOS 15
refuses with EACCES when the directory lacks owner write, even within one
parent: CI's `macos-15` runners failed #258 so (run 40166524067), where this
host, on macOS 26, passed. Nothing here runs macOS 15, so `MACOS15` emulates it
by wrapping `os.rename` and `os.replace`, which `Path.rename`, `Path.replace`
and `shutil.move` call too. `HOST` leaves this host's rule as it is.

A test names its rule in its id (`host-rename`, `macos15-rename`). A child
process gets the same rule with `install(rule)` before anything renames.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat

HOST = "host-rename"
MACOS15 = "macos15-rename"
RULES = (HOST, MACOS15)

_stat = os.stat                  # the real one: a test may patch `os.stat` around the code it runs


def refused(src, src_dir_fd=None) -> bool:
    """Whether macOS 15 refuses to rename `src`: a directory (never through a
    symlink) whose mode lacks owner write."""
    try:
        found = _stat(src, dir_fd=src_dir_fd, follow_symlinks=False)
    except OSError:
        return False             # the real call reports what is wrong with it
    return stat.S_ISDIR(found.st_mode) and not found.st_mode & stat.S_IWUSR


def _macos15(real):
    def rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if refused(src, src_dir_fd):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), src, None, dst)
        return real(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
    rename.rename_rule = MACOS15
    return rename


def install(rule: str, patch=None) -> None:
    """Make `os.rename` and `os.replace` follow `rule`: through a pytest
    `MonkeyPatch` when given (undone with it), else for the rest of the process."""
    assert rule in RULES, rule
    if rule == HOST:
        return
    for name in ("rename", "replace"):
        wrapped = _macos15(getattr(os, name))
        if patch is not None:
            patch.setattr(os, name, wrapped)
        else:
            setattr(os, name, wrapped)


@contextlib.contextmanager
def applied(rule: str):
    """`rule` for the duration of the block (for Hypothesis examples, which a
    function-scoped `monkeypatch` cannot serve)."""
    import pytest
    with pytest.MonkeyPatch.context() as patch:
        install(rule, patch)
        yield


def move_dir(src, dst) -> None:
    """Rename a directory under either rule, as a provider renaming its own locked
    folder must on macOS 15: give it owner write, rename, put its mode back."""
    mode = stat.S_IMODE(os.lstat(src).st_mode)
    os.chmod(src, mode | stat.S_IWUSR)
    os.rename(src, dst)
    os.chmod(dst, mode)
