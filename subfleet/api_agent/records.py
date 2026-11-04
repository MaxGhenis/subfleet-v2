"""The harness's records in the attempt folder (C-31.9).

`stream.jsonl` holds one event per line, each with `"v": 1`, `"type"` and `"ts"`;
`messages.jsonl` holds the conversation, one message per line, append-only;
`last.md` holds the final assistant text or nothing. Every line passes through the
`Redactor` on its way to disk, so neither the key nor any 12-character run of it
reaches a record (C-31.12).
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

STREAM = "stream.jsonl"
MESSAGES = "messages.jsonl"
LAST = "last.md"
VERSION = 1
#: The run length the key check uses, as `claude-credits`' own test does.
RUN = 12
REDACTED = "[redacted]"
_APPEND = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Redactor:
    """Replaces a secret, and every `RUN`-character run of it, with `[redacted]`.

    The replacement holds no character a key can hold next to a run, so replacing
    never creates a new run. Its text never shows the secret.
    """

    __slots__ = ("_runs",)

    def __init__(self, secret: str | None) -> None:
        runs: set[str] = set()
        if secret:
            if len(secret) <= RUN:
                runs.add(secret)
            else:
                runs.update(secret[index:index + RUN] for index in range(len(secret) - RUN + 1))
        self._runs = tuple(sorted(runs))

    def __call__(self, text: str) -> str:
        if not self._runs or not text:
            return text
        while True:
            found = [run for run in self._runs if run in text]
            if not found:
                return text
            for run in found:
                text = text.replace(run, REDACTED)

    def __repr__(self) -> str:
        return f"<Redactor runs={len(self._runs)}, secret withheld>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("Redactor cannot be pickled: it would carry the secret")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


class HistoryError(ValueError):
    """`messages.jsonl` holds something that is not a conversation."""


class Records:
    """The open record files of one attempt. Writes are appended and synced."""

    def __init__(self, attempt_dir: Path, redact: Redactor) -> None:
        self.dir = attempt_dir
        self._redact = redact
        self._stream = os.open(attempt_dir / STREAM, _APPEND, 0o600)
        self._messages = os.open(attempt_dir / MESSAGES, _APPEND, 0o600)
        self.write_last("")

    def close(self) -> None:
        for fd in (self._stream, self._messages):
            try:
                os.close(fd)
            except OSError:
                pass

    def _line(self, fd: int, record: object) -> None:
        # ASCII JSON: a lone surrogate in a tool's output stays a valid escape.
        text = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        _write_all(fd, (self._redact(text) + "\n").encode("ascii"))
        os.fsync(fd)

    def event(self, kind: str, **fields: object) -> dict:
        record = {"v": VERSION, "type": kind, "ts": utc_now(), **fields}
        self._line(self._stream, record)
        return record

    def append_message(self, message: dict) -> None:
        self._line(self._messages, message)

    def write_last(self, text: str) -> None:
        fd = os.open(self.dir / LAST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            _write_all(fd, self._redact(text).encode("utf-8", "replace"))
            os.fsync(fd)
        finally:
            os.close(fd)


def load_history(path: Path) -> list[dict]:
    """The conversation in `messages.jsonl`, for `--resume`.

    A last line without its newline is a write the previous process never finished:
    the file is cut back to the end of the last whole line, which removes nothing a
    request ever carried. Any other line that is not a message raises.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return []
    try:
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read()
        whole = data.rfind(b"\n") + 1
        if whole < len(data):
            os.ftruncate(fd, whole)
            os.fsync(fd)
        messages: list[dict] = []
        for number, raw in enumerate(data[:whole].splitlines(), start=1):
            try:
                message = json.loads(raw)
            except ValueError as exc:
                raise HistoryError(f"messages.jsonl line {number} is not JSON") from exc
            if (not isinstance(message, dict) or message.get("role") not in ("user", "assistant")
                    or not isinstance(message.get("content"), (str, list))):
                raise HistoryError(f"messages.jsonl line {number} is not a message")
            messages.append(message)
        return messages
    finally:
        os.close(fd)


def text_of(content: Iterable[dict] | str) -> str:
    """The text blocks of one message, joined."""
    if isinstance(content, str):
        return content
    return "\n\n".join(block.get("text", "") for block in content
                       if isinstance(block, dict) and block.get("type") == "text" and block.get("text"))
