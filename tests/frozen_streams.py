"""Frozen-path objects shared by bounded properties and replay regressions."""

import os
from pathlib import Path


FROZEN_KINDS = ("empty", "regular", "directory", "fifo", "symlink", "dangling-symlink")


def make_frozen(path: Path, kind: str, contents: bytes = b"") -> None:
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        path.symlink_to(path.parent / "stdout")
    elif kind == "dangling-symlink":
        path.symlink_to(path.parent / "missing-target")
    else:
        path.write_bytes(b"" if kind == "empty" else contents)


def frozen_identity(path: Path) -> tuple:
    """Inspect without opening a FIFO or following a link."""
    info = path.lstat()
    return info.st_ino, info.st_mode, path.readlink() if path.is_symlink() else None
