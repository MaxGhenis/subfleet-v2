"""Attachments by content (C-28.1).

`attachment.add` copies a person's pasted or chosen image into the state root
before any message names it: a regular file owned by the daemon's user,
opened without following symlinks, at most 20 MiB, PNG, JPEG, GIF or WebP by
magic bytes, re-hashed after the copy. The daemon's copy is what a provider
sees; the original can be deleted the moment the receipt arrives.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import stat
from pathlib import Path

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


def _copy(data: bytes, target: Path) -> None:
    """Write `data` to `target` through a temporary file of this call's own, renamed
    onto it. Two adds of the same bytes at once (the app re-sending an image, a retry)
    each rename a whole copy into place; through one shared name, the second open
    truncated the first's file and one rename found it gone. A copy that fails removes
    its temporary file."""
    tmp = target.with_name(f".{target.stem}.{secrets.token_hex(4)}.tmp")
    out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:                                   # os.write may write less than asked
                view = view[os.write(out, view):]
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
    if not target.exists():
        _copy(data, target)
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise ConversationError("copy-mismatch", "the stored copy does not match; try again", code=1)
    store.add_attachment(digest, media, len(data), str(target))
    return {"sha256": digest, "media_type": media, "bytes": len(data)}


def check(store: ConversationStore, sha256: str) -> tuple[str, str]:
    """Before a frame is built (C-28.1): the stored copy exists and still hashes right."""
    row = store.attachment(sha256)
    if row is None:
        raise ConversationError("attachment-missing", f"attachment {sha256} is not stored")
    path = Path(row["path"])
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
            raise ConversationError("attachment-missing", f"attachment {sha256} changed on disk")
    except OSError as exc:
        raise ConversationError("attachment-missing", f"attachment {sha256} is gone") from exc
    return str(path), row["media_type"]
