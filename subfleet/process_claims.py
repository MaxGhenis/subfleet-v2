"""Serialization of guardian child receipts with daemon signal dispatch."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


@contextmanager
def receipt_claim_lock(receipt: Path, *, blocking: bool = False):
    """A stable sidecar inode; dispatch refuses a busy/unavailable publisher."""
    fd = None
    try:
        fd = os.open(receipt.with_name(".start-claims.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except OSError:
        if fd is not None:
            os.close(fd)
        if blocking:
            raise
        yield False
        return
    try:
        yield True
    finally:
        os.close(fd)
