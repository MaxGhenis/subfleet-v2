"""Bounded reads of state records and verified private payloads.

Native transcripts deliberately allow symlinks (`sessions.transcripts`). State
records must opt into a separate reader that never follows the final symlink.
All checks concern the descriptor whose bytes are returned.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from .sessions.transcripts import NotRegularFile, TooLarge


def _state_fd(path: str | Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(errno.EINVAL, "not a regular file", str(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_state(path: str | Path, mode: str = "rb", **kwargs: Any):
    """Stream an existing regular state file without following a symlink.

    Stdout and relay logs are streamed to completion, with their callers' own
    budgets. `open` owns and closes the checked descriptor even if building the
    stream fails, as `transcripts.open_regular` does for native files.
    """
    if mode not in ("r", "rb", "rt") or {"opener", "closefd"} & set(kwargs):
        raise ValueError(f"open_state reads only, with its own descriptor: mode {mode!r}, {sorted(kwargs)}")
    return open(path, mode, opener=lambda name, flags: _state_fd(name), **kwargs)


def read_state(path: str | Path, *, limit: int, digest: str | None = None,
               size: int | None = None, private: bool = False) -> bytes:
    """Read a regular, non-symlink file without waiting for a FIFO's writer.

    Refuse files larger than `limit` before reading, and read at most one byte
    past that limit (or the expected `size`) to catch growth. With `private`,
    require this user's own file with no second hard link or public mode bits,
    before and after reading. `digest` verifies the exact bytes returned.
    Refusals are OSError, as for any other unreadable file.
    """
    if limit < 0 or (size is not None and size < 0):
        raise ValueError("file limits and sizes must be nonnegative")
    fd = _state_fd(path)
    try:
        def check() -> None:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise NotRegularFile(errno.EINVAL, "not a regular file", str(path))
            if info.st_size > limit:
                raise TooLarge(errno.EFBIG, f"longer than {limit} bytes", str(path))
            if size is not None and info.st_size != size:
                raise OSError(errno.EIO, "file size changed", str(path))
            if private and (info.st_uid != os.getuid() or info.st_nlink > 1 or info.st_mode & 0o077):
                raise OSError(errno.EACCES, "not a private file owned by this user", str(path))

        check()
        cap = limit if size is None else min(limit, size)
        chunks, total = [], 0
        while chunk := os.read(fd, min(1 << 20, cap + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                raise TooLarge(errno.EFBIG, f"longer than {cap} bytes", str(path))
        check()
        data = b"".join(chunks)
        if size is not None and len(data) != size:
            raise OSError(errno.EIO, "file size changed while reading", str(path))
        if digest is not None and hashlib.sha256(data).hexdigest() != digest:
            raise OSError(errno.EIO, "file digest does not match", str(path))
        return data
    finally:
        os.close(fd)
