"""The credit key's frame: how an API credit lane's key travels between processes (C-31.17).

Hub decision of 2026-11-04 (`docs/reports/credit-lane-integration-report.md`): no exec
environment block, argv or file of any process ever holds a credit key. The key travels
only over pipes, and only the harness keeps it, in memory.

- The daemon writes the launch gate's release byte and then one frame on the guardian's
  launch pipe (`--launch-fd`, with `--key-frame`).
- The guardian reads that frame, writes the same frame into a new pipe, and passes the
  pipe's read end to the harness, whose argv it extends with `--key-fd <n>`.
- The harness reads the frame once at start, closes the descriptor and keeps the key in
  memory only.

A frame is a 4-byte big-endian length, then that many bytes of key, 1 to `MAX_KEY_BYTES`.
The bound keeps a reader's buffer fixed and small: a frame always fits the pipe's buffer,
so no writer here ever blocks on a reader. A reader that meets end-of-file, a short read, a
zero length or a length over the bound raises `FrameError`, whose message never holds a
byte of the frame. Readers return a `bytearray`, so a holder that is done with the key
can overwrite it (`wipe`).

Standard library only: the guardian and the harness import this before anything else.
"""
from __future__ import annotations

import os

#: The largest key a frame carries, in bytes. A Console API key is 108 characters.
MAX_KEY_BYTES = 4096
#: The name credential resolution returns a credit key under (`credentials.resolve_credit_key`),
#: inside the daemon only: the adapter checks a credit launch has it, the daemon frames it, and
#: no environment, argv or file ever carries it under this name or any other.
CREDENTIAL_NAME = "ANTHROPIC_API_KEY"
#: The length prefix: an unsigned 32-bit big-endian count.
HEADER_BYTES = 4


class FrameError(ValueError):
    """A frame that is missing, cut short, empty or over the bound. Its message names
    which, and never holds a byte of the frame."""


def encode(key: str) -> bytearray:
    """The frame for `key`: printable ASCII of 1 to `MAX_KEY_BYTES` characters."""
    if not isinstance(key, str) or not key.isascii() or not key.isprintable():
        raise FrameError("a credit key frame carries printable ASCII only")
    return frame(key.encode("ascii"))


def frame(payload: bytes | bytearray) -> bytearray:
    """The frame for key bytes as a reader returned them: the guardian relays what it
    read without making a string of it."""
    if not 1 <= len(payload) <= MAX_KEY_BYTES:
        raise FrameError(f"a credit key frame carries 1 to {MAX_KEY_BYTES} bytes")
    framed = bytearray(len(payload).to_bytes(HEADER_BYTES, "big"))
    framed += payload
    return framed


def decode(payload: bytearray) -> str:
    """The key a frame's bytes carry, for the one holder that keeps it (the harness).
    Refuses anything but printable ASCII."""
    try:
        key = payload.decode("ascii")
    except UnicodeDecodeError:
        raise FrameError("the credit key frame holds bytes that are not ASCII") from None
    if not key.isprintable():
        raise FrameError("the credit key frame holds characters that are not printable")
    return key


def _read_exactly(fd: int, size: int, what: str) -> bytearray:
    buffer = bytearray(size)
    view = memoryview(buffer)
    got = 0
    try:
        while got < size:
            count = os.readv(fd, [view[got:]])
            if count == 0:
                wipe(buffer)
                raise FrameError(f"the credit key frame ended inside its {what}")
            got += count
    finally:
        view.release()
    return buffer


def read_frame(fd: int) -> bytearray:
    """Read one frame from `fd` and return its key bytes. Never reads past the frame."""
    header = _read_exactly(fd, HEADER_BYTES, "length")
    size = int.from_bytes(header, "big")
    if size == 0:
        raise FrameError("the credit key frame is empty")
    if size > MAX_KEY_BYTES:
        raise FrameError(f"the credit key frame is longer than {MAX_KEY_BYTES} bytes")
    return _read_exactly(fd, size, "key")


def write_all(fd: int, data: bytes | bytearray | memoryview) -> None:
    """Write every byte of `data` to `fd`, however many writes that takes."""
    view = memoryview(data)
    try:
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        view.release()


def wipe(buffer: bytearray) -> None:
    """Overwrite `buffer` with zero bytes in place."""
    if buffer:
        memoryview(buffer)[:] = bytes(len(buffer))
