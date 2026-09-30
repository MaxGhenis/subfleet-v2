"""Attachments by content (C-28.1).

`attachment.add` copies a person's pasted or chosen image into the state root
before any message names it: a regular file owned by the daemon's user,
opened without following symlinks, at most 20 MiB, PNG, JPEG, GIF or WebP by
magic bytes, re-hashed after the copy. An add whose stored copy is missing or
changed copies it again, and the copy's name is on disk before its row. The
daemon's copy is what a provider sees; the original can be deleted the moment
the receipt arrives.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import secrets
import stat
from pathlib import Path

from ..state_files import read_state
from .store import ConversationError, ConversationStore

MAX_BYTES = 20 * 1024 * 1024
TYPES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)


def sniff(head: bytes) -> tuple[str, str] | None:
    for magic, media, ext in TYPES:
        if head.startswith(magic):
            return media, ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _holds(target: Path, digest: str, size: int) -> bool:
    """Whether `target` is the daemon's own copy: a regular file of `size` bytes that
    hash to `digest`, owned by this user, with no second link and no group or other mode
    bits (an ACL is not read).
    Anything else is not holding: an add copies over it and the driver's check refuses
    it. That is no file, other bytes (disk damage, a stray write), a file this user
    cannot read, a symlink (not followed), a FIFO (opened without waiting for a
    writer), a hard link another name can write through, or a copy others can read.
    The size is checked before any byte is read, and at most one byte past it is read
    (which shows the file grew)."""
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        # No link but its name, or none: an add that renamed its identical copy over this
        # one after it was opened leaves it with no name at all, which no one writes through.
        if (not stat.S_ISREG(info.st_mode) or info.st_size != size or info.st_uid != os.getuid()
                or info.st_nlink > 1 or info.st_mode & 0o077):
            return False
        sha, total = hashlib.sha256(), 0
        while chunk := os.read(fd, min(1 << 20, size + 1 - total)):
            total += len(chunk)
            if total > size:                              # it grew while it was read
                return False
            sha.update(chunk)
        return sha.hexdigest() == digest
    except OSError:
        return False
    finally:
        os.close(fd)


def _sync_directory(directory: Path) -> None:
    """fsync `directory`, so a name renamed into it survives a crash, as `store._publish`
    does; not `_publish` itself, whose mkdir(parents=True) could make a removed state
    root again (review of #47)."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy(data: bytes, target: Path, *, mode: int = 0o600) -> None:
    """Write `data` to `target` through a temporary file of this call's own, renamed
    onto it. Two adds of the same bytes at once (the app resending after its request
    timed out while the first add still ran, or two clients) each rename a whole copy
    into place; through one shared name, the second open truncated the first's file
    and one rename found it gone. A copy that fails removes its temporary file, if the
    directory still lets it. The caller fsyncs the directory before it writes the row."""
    for _ in range(8):                                    # a name another add holds is drawn again
        tmp = target.with_name(f".{target.stem}.{secrets.token_hex(4)}.tmp")
        try:
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            break
        except FileExistsError as exc:
            taken = exc
    else:
        raise taken                                       # every draw taken: fail before the cleanup's try
    try:
        try:
            view = memoryview(data)
            while view:                                   # os.write may write less than asked
                written = os.write(out, view)
                if not written:                           # never loop without progress
                    raise OSError(errno.EIO, "the attachment copy made no progress")
                view = view[written:]
            os.fchmod(out, mode)
            os.fsync(out)
        finally:
            os.close(out)
        os.rename(tmp, target)                            # over an identical copy, if another add got there
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def add(store: ConversationStore, path: str, expected_sha256: str | None = None) -> dict:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ConversationError("bad-path", "attachment path must be absolute")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ConversationError("unreadable", f"cannot open attachment: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConversationError("not-a-file", "attachment must be a regular file")
        if info.st_uid != os.getuid():
            raise ConversationError("not-owned", "attachment must belong to this user", code=7)
        if not 0 < info.st_size <= MAX_BYTES:
            raise ConversationError("too-large", "attachment must be 1 byte to 20 MiB")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise ConversationError("too-large", "attachment grew past 20 MiB while it was read")
            chunks.append(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    kind = sniff(data[:16])
    if kind is None:
        raise ConversationError("not-an-image", "attachments must be PNG, JPEG, GIF or WebP")
    media, ext = kind
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ConversationError("hash-mismatch", "the file does not match the hash the app sent")
    directory = store.subdirectory("attachments")         # never the state root itself
    target = directory / f"{digest}.{ext}"
    if not _holds(target, digest, len(data)):             # missing or changed: copy it again
        _copy(data, target)
        if not _holds(target, digest, len(data)):
            raise ConversationError("copy-mismatch", "the stored copy does not match; try again", code=1)
    # Published as C-8.1 says, its name on disk before its row, whichever add made the
    # copy: one that found it in place may have found it before its maker synced.
    _sync_directory(directory)
    store.add_attachment(digest, media, len(data), str(target))
    return {"sha256": digest, "media_type": media, "bytes": len(data)}


def check(store: ConversationStore, sha256: str) -> tuple[str, str]:
    """Before a frame is built (C-28.1): the stored copy is still the daemon's own and
    hashes right, judged as an add judges it (`_holds`), so it never blocks on a FIFO or
    follows a symlink. `path.read_bytes()` did both: a FIFO at the name held the
    conversation tick that builds turns, and every tick after it, for good (review of
    1808f61). Adding the image again repairs the copy."""
    row = store.attachment(sha256)
    if row is None:
        raise ConversationError("attachment-missing", f"attachment {sha256} is not stored")
    if not _holds(Path(row["path"]), sha256, row["bytes"]):
        raise ConversationError("attachment-missing",
                                f"attachment {sha256} is not the daemon's own stored copy (gone, changed, or not private)",
                                fix="add the image again; that repairs the stored copy")
    return row["path"], row["media_type"]


def read_verified(store: ConversationStore, sha256: str) -> tuple[bytes, str]:
    """Resolve an image by content in the current root and verify its final read.

    The caller uses these exact bytes, never a second open of the checked name.
    An old manifest or attachment row may still name a previous state root.
    """
    row = store.attachment(sha256)
    extensions = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
    if row is None or row["media_type"] not in extensions:
        raise OSError("attachment is not stored")
    ext = extensions[row["media_type"]]
    path = store.root / "attachments" / f"{sha256}.{ext}"
    data = read_state(path, limit=MAX_BYTES, digest=sha256, size=row["bytes"], private=True)
    return data, ext
