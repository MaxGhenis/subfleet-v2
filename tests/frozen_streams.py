"""Frozen-path objects shared by bounded properties and replay regressions."""

import os
from pathlib import Path
import socket
import tempfile
from contextlib import contextmanager


NONREGULAR_KINDS = ("directory", "fifo", "socket", "symlink", "dangling-symlink")
FROZEN_KINDS = ("empty", "regular", "hardlink", *NONREGULAR_KINDS)
FREEZE_STEPS = ("before-open", "after-open", "fstat", "read", "file-fsync",
                "rename", "directory-fsync", "after-freeze")


@contextmanager
def link_during_freeze(patch, candidate: Path, live: Path, step: str, kind: str):
    """Add a writable alias at one syscall boundary, retaining the source bytes."""
    from subfleet import daemon as module

    fired = []
    def link():
        if fired:
            return
        fired.append(step)
        if kind == "parent-alias":
            live.parent.symlink_to(candidate.parent, target_is_directory=True)
            return
        live.unlink(missing_ok=True)
        if kind == "hardlink":
            os.link(candidate, live)
        else:
            live.symlink_to(candidate)

    original_open, original_fstat = os.open, os.fstat
    original_fsync, original_rename = os.fsync, os.rename
    original_stream = module.open_stream
    source_fds = set()
    checks = 0
    def opening(path, *args, **kwargs):
        source = os.fspath(path) == str(candidate)
        if source and step == "before-open":
            link()
        fd = original_open(path, *args, **kwargs)
        if source:
            source_fds.add(fd)
            if step == "after-open":
                link()
        return fd
    def checking(fd):
        nonlocal checks
        info = original_fstat(fd)
        if fd in source_fds:
            checks += 1
            if checks == 2 and step == "fstat":
                link()  # Return the decision made before the new link.
        return info
    def syncing(fd):
        import stat
        directory = stat.S_ISDIR(original_fstat(fd).st_mode)
        if step == ("directory-fsync" if directory else "file-fsync"):
            link()
        return original_fsync(fd)
    def renaming(*args, **kwargs):
        if step == "rename":
            link()
        return original_rename(*args, **kwargs)
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, *args):
            if step == "read":
                link()
            return self.stream.read(*args)
    def opening_stream(path, *args, **kwargs):
        stream = original_stream(path, *args, **kwargs)
        return Reader(stream) if Path(path) == candidate else stream
    patch.setattr(os, "open", opening)
    patch.setattr(os, "fstat", checking)
    patch.setattr(os, "fsync", syncing)
    patch.setattr(os, "rename", renaming)
    patch.setattr(module, "open_stream", opening_stream)
    yield fired
    if step == "after-freeze":
        link()


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
