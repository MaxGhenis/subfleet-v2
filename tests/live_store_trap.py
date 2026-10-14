"""Fail a test before it touches a path it must never reach (D-ST1).

`untouchable` refuses, before the call happens, any stat, lstat, readlink,
listing, `os.open`, `open` or directory or file change at or beneath the given
roots, by any case and through `..` segments. `Path.resolve`, `Path.samefile`,
`os.path.realpath` and `Path.mkdir` go through these; C code that opens a path
itself, such as `sqlite3.connect`, does not. The refusal is `pytest.fail`, a
BaseException, so a consumer's `except Exception` cannot swallow it and carry
on to the next access.
"""

from __future__ import annotations

import builtins
import contextlib
import os
import pwd
from pathlib import Path

import pytest

#: The person's live stores, from the real account entry, never from HOME.
PERSONAL = Path(pwd.getpwuid(os.getuid()).pw_dir)
LIVE_STORES = tuple(PERSONAL / relative for relative in (
    ".subfleet", ".claude", ".codex", ".claude.json", "chief-of-staff/state",
    "Library/Application Support/Claude",
))


@contextlib.contextmanager
def untouchable(patch, *roots):
    forbidden = [os.path.normpath(os.fspath(root)).casefold() for root in roots]
    touched = []

    def reaches(path):
        # Every directory the lookup walks, so `root/..` counts as well.
        walked = ""
        for part in path.casefold().split(os.sep):
            walked = os.path.normpath(os.path.join(walked or os.sep, part)) if part else walked
            if any(walked == root or walked.startswith(root + os.sep) for root in forbidden):
                return True
        return False

    def guarded(function):
        def call(*args, **kwargs):
            path = args[0] if args else kwargs.get("path", kwargs.get("file", "."))
            if not isinstance(path, int):
                spelled = os.fsdecode(path)
                if reaches(os.path.join(os.getcwd(), spelled)):
                    touched.append(spelled)
                    pytest.fail(f"touched {spelled}")
            return function(*args, **kwargs)
        return call

    with patch.context() as inner:
        for name in ("stat", "lstat", "readlink", "listdir", "scandir", "open", "mkdir", "rmdir",
                     "unlink", "rename", "replace", "chmod", "utime"):
            inner.setattr(os, name, guarded(getattr(os, name)))
        inner.setattr(builtins, "open", guarded(builtins.open))
        yield touched
