"""Stall points for the relay's stop bound (C-26.4, #236 review r1).

`StallingOs` stands in for the `os` module that `subfleet.relay` calls. Every
name is the real one except `write`, `fsync` and `unlink`, which hold the call
chosen until the test releases it. A call is chosen by what it touches, so
nothing else waits:
- a journal record: a regular file, matched by the record's `kind` and `seq`;
- the stdin pipe: a FIFO;
- the socket file, for `unlink`.

`StallingServer` stands in for the relay's listening socket: its `close` is held
(`server-close`), or the `close` of each connection it accepts (`conn-close`).

A `Gate` holds threads of this process; a `FileGate` holds a guardian in a
child process, through files the test creates.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

# Where a call can be held. `pipe-write`, `journal-write` and `journal-fsync` are
# in the relay thread's exchange; `conn-close` ends that thread's connection;
# `server-close` and `unlink` are stop's own cleanup.
OS_POINTS = ("pipe-write", "journal-write", "journal-fsync", "unlink")
SOCKET_POINTS = ("server-close", "conn-close")
POINTS = OS_POINTS + SOCKET_POINTS


class Gate:
    """Holds each call that reaches it until `release`; `reached` is set by the first."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.released = threading.Event()

    def hold(self) -> None:
        self.reached.set()
        self.released.wait()

    def release(self) -> None:
        self.released.set()


class FileGate:
    """A gate across processes: `reached` is a file this side creates; it holds
    until the test creates `release`."""

    def __init__(self, reached: str | Path, release: str | Path) -> None:
        self.reached_path = Path(reached)
        self.release_path = Path(release)

    def hold(self) -> None:
        self.reached_path.touch()
        while not self.release_path.exists():
            time.sleep(0.01)


def _record(data) -> dict | None:
    try:
        record = json.loads(bytes(data))
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


class StallingOs:
    """`os`, with one kind of call held at `gate`.

    `record` picks the journal record to hold: `intent`, `failed`, `written`, or
    `outcome` (either of the last two); `seq` narrows it to one frame."""

    def __init__(self, gate, *, point: str, record: str = "outcome", seq: int | None = None) -> None:
        if point not in OS_POINTS:
            raise ValueError(point)
        self.gate = gate
        self.point = point
        self.record = record
        self.seq = seq
        self._last: dict[int, dict | None] = {}     # each journal descriptor's record, for its fsync

    def __getattr__(self, name):
        return getattr(os, name)

    def _chosen(self, record: dict | None) -> bool:
        if record is None or (self.seq is not None and record.get("seq") != self.seq):
            return False
        kind = record.get("kind")
        return kind in ("written", "failed") if self.record == "outcome" else kind == self.record

    def write(self, fd: int, data) -> int:
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):
            record = _record(data)
            self._last[fd] = record
            if self.point == "journal-write" and self._chosen(record):
                self.gate.hold()
        elif stat.S_ISFIFO(mode) and self.point == "pipe-write":
            self.gate.hold()
        return os.write(fd, data)

    def fsync(self, fd: int) -> None:
        if self.point == "journal-fsync" and self._chosen(self._last.get(fd)):
            self.gate.hold()
        os.fsync(fd)

    def unlink(self, path, *args, **kwargs) -> None:
        if self.point == "unlink":
            self.gate.hold()
        os.unlink(path, *args, **kwargs)


class StallingConnection:
    """An accepted connection whose `close` is held at `gate`."""

    def __init__(self, conn, gate) -> None:
        self._conn = conn
        self._gate = gate

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self._gate.hold()
        self._conn.close()


class StallingServer:
    """The relay's listening socket with `close` held (`server-close`), or the
    close of each connection it accepts (`conn-close`)."""

    def __init__(self, server, gate, *, point: str) -> None:
        if point not in SOCKET_POINTS:
            raise ValueError(point)
        self._server = server
        self._gate = gate
        self._point = point

    def __getattr__(self, name):
        return getattr(self._server, name)

    def accept(self):
        conn, address = self._server.accept()
        if self._point == "conn-close":
            return StallingConnection(conn, self._gate), address
        return conn, address

    def close(self) -> None:
        if self._point == "server-close":
            self._gate.hold()
        self._server.close()
