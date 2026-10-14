"""Frozen-path objects shared by bounded properties and replay regressions."""

import os
from pathlib import Path
import socket
import tempfile


NONREGULAR_KINDS = ("directory", "fifo", "socket", "symlink", "dangling-symlink")
FROZEN_KINDS = ("empty", "regular", "hardlink", *NONREGULAR_KINDS)


def make_frozen(path: Path, kind: str, contents: bytes = b"") -> None:
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        # Darwin's Unix socket path cap is shorter than pytest's temp paths.
        with tempfile.TemporaryDirectory(prefix="sf-sock-", dir="/private/tmp") as temporary:
            source = Path(temporary) / "socket"
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(source))
            source.rename(path)
    elif kind == "hardlink":
        source = path.parent / "stdout"
        source.write_bytes(contents)
        os.link(source, path)
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
