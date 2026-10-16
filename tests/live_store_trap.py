"""Refuse forbidden filesystem accesses before the underlying call (D-ST1).

Check open/io.open/io.FileIO, chdir, path operands, aliases (including firmlinks),
and descriptor-relative paths.
Realpath itself runs through the lexical trap, so resolving an alias never stats
its forbidden target. Native libraries opening paths without Python APIs (e.g.
sqlite3.connect), and DirEntry.stat() on entries from scanning a parent, remain
outside this monkeypatch trap and require isolated inputs.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import os
import pwd
import sys
import threading
from pathlib import Path

import pytest

PERSONAL = Path(pwd.getpwuid(os.getuid()).pw_dir)
LIVE_STORES = tuple(PERSONAL / relative for relative in (
    ".subfleet", ".claude", ".codex", ".claude.json", "chief-of-staff/state",
    "Library/Application Support/Claude",
))


def _descriptor_path(fd):
    """Read the descriptor's name, never stat its target."""
    if sys.platform == "darwin":
        import fcntl
        raw = fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH", 50), bytes(1024))
        return os.fsdecode(raw.split(b"\0", 1)[0])
    return os.readlink(f"/proc/self/fd/{fd}")


@contextlib.contextmanager
def untouchable(patch, *roots):
    forbidden = [os.path.abspath(os.fspath(root)).casefold() for root in roots]
    touched = []
    resolving = threading.local()

    def reaches(path):
        # Check the walk before normalizing: reaching root/.. still touches root.
        walked = ""
        for part in path.casefold().split(os.sep):
            walked = os.path.normpath(os.path.join(walked or os.sep, part)) if part else walked
            if any(walked == root or walked.startswith(root + os.sep) for root in forbidden):
                return True
        return False

    def refuse(spelled):
        touched.append(spelled)
        pytest.fail(f"touched {spelled}")

    def check(path, dir_fd=None):
        try:
            if isinstance(path, int):
                spelled = _descriptor_path(path)
                absolute = spelled
            else:
                spelled = os.fsdecode(path)
                base = _descriptor_path(dir_fd) if dir_fd is not None and not os.path.isabs(spelled) else os.getcwd()
                absolute = os.path.join(base, spelled)
        except (OSError, TypeError, ValueError):
            return  # Invalid arguments are rejected by the original API.
        if reaches(absolute):
            refuse(spelled)
        if not getattr(resolving, "active", False):
            # realpath's lstat/readlink calls re-enter this wrapper. Those calls
            # do the lexical check before touching each component, including a
            # symlink's target. Only recursive canonicalization is suppressed.
            resolving.active = True
            try:
                canonical = os.path.realpath(absolute)
                if reaches(canonical):
                    refuse(spelled)
            finally:
                resolving.active = False

    def guarded(function, operands):
        def call(*args, **kwargs):
            for index, keyword, fd_keyword in operands:
                path = args[index] if len(args) > index else kwargs.get(keyword, ".")
                check(path, kwargs.get(fd_keyword))
            return function(*args, **kwargs)
        return call

    with patch.context() as inner:
        for name in ("chdir", "stat", "lstat", "readlink", "listdir", "scandir", "open", "mkdir", "rmdir",
                     "unlink", "remove", "access", "chmod", "chown", "utime", "truncate"):
            inner.setattr(os, name, guarded(getattr(os, name), [(0, "path", "dir_fd")]))
        for name in ("rename", "replace", "link"):
            inner.setattr(os, name, guarded(getattr(os, name),
                          [(0, "src", "src_dir_fd"), (1, "dst", "dst_dir_fd")]))
        inner.setattr(os, "symlink", guarded(os.symlink,
                      [(0, "src", "src_dir_fd"), (1, "dst", "dir_fd")]))
        inner.setattr(builtins, "open", guarded(builtins.open, [(0, "file", "dir_fd")]))
        inner.setattr(io, "open", guarded(io.open, [(0, "file", "dir_fd")]))
        inner.setattr(io, "FileIO", guarded(io.FileIO, [(0, "file", "dir_fd")]))
        # Canonicalize parent aliases with the lexical trap already installed;
        # never resolve or stat a forbidden root itself during setup.
        forbidden.extend(os.path.join(os.path.realpath(os.path.dirname(os.fspath(root))),
                                      os.path.basename(os.fspath(root))).casefold()
                         for root in roots)
        # Firmlinks are not symlinks; realpath cannot expose their other name.
        forbidden.extend("/system/volumes/data" + root for root in list(forbidden)
                         if not root.startswith("/system/volumes/data/"))
        yield touched
