"""The daemon's descriptor budget (C-16.6, C-16.7).

Every client connection holds a descriptor from `accept` until its reply is
written, and so does every pipe to a child, the store and its WAL, and the log.
launchd starts a job with a soft open-file limit of 256. On 2026-09-24 and again
on 2026-09-25 the daemon reached it: `accept` raised EMFILE and the uncaught
error ended the process while its clients waited on replies.

Two faults made the descriptors pile up, and both are fixed here and in
`Daemon.serve_forever`. A connection waited, descriptor open, in the reader
pool's queue whenever 32 earlier connections held every reader thread (a `wait`
holds one for up to a minute). And a request whose client had timed out and
hung up still ran when its turn came, so each 15-second retry added work as
well as a descriptor. This module keeps what that needs, with no daemon state:
the limit, the connection cap derived from it, the test for a client that has
gone, and the line framing that lets a reader give up on an idle client.
"""

from __future__ import annotations

import os
import plistlib
import resource
import socket
import subprocess
import sys
from pathlib import Path

#: C-16.6: the soft open-file limit the daemon asks for at start, and the value
#: `daemon install` writes into the launchd plist. The hard limit bounds it.
OPEN_FILES_WANTED = 65536
#: C-16.6: lower soft limits tried in turn when `setrlimit` refuses a higher one
#: (EINVAL or EPERM; macOS documents both for RLIMIT_NOFILE).
OPEN_FILES_FALLBACKS = (32768, 16384, 8192, 4096, 2048, 1024)

#: C-16.7: descriptors kept back from client connections for the daemon's own
#: work: the store with its WAL and shared memory, the log, the lock, the
#: listening socket, and the pipes and files of the children it starts.
DESCRIPTOR_RESERVE = 64
#: C-16.7: client connections held at once never exceed half of what the
#: reserve leaves, and never this many; each one has a reader thread waiting.
CONNECTIONS_CEILING = 512
CONNECTIONS_FLOOR = 4
#: C-16.7: a connection with no request outstanding that sends nothing for this
#: long is closed. The CLI sends its request as soon as it connects.
CONNECTION_IDLE_S = 60.0
#: C-16.1: one request line, newline included, is at most this many bytes.
MAX_REQUEST_BYTES = 1024 * 1024

#: C-16.7: ops whose only effect is their reply (and the observation caches a
#: read refreshes in passing). One whose client has hung up is not run.
READ_ONLY_OPS = frozenset({"list", "show", "wait", "readings", "why", "pick",
                           "daemon.status", "notice.pending"})


def open_file_limits() -> tuple[int, int]:
    """This process's (soft, hard) RLIMIT_NOFILE."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return soft, hard


def raise_open_file_limit(wanted: int = OPEN_FILES_WANTED) -> tuple[int, int, int]:
    """C-16.6: lift the soft open-file limit toward `wanted`; returns (before, after, hard).

    The soft limit is never lowered and never set above the hard limit. When
    `setrlimit` refuses a value, the next lower of `OPEN_FILES_FALLBACKS` is
    tried; a limit that cannot be raised at all is left as it was. `after` is
    what `getrlimit` reports once this returns. On macOS 26 a value above
    `kern.maxfilesperproc` is accepted, and that ceiling is enforced instead
    when descriptors are opened; 65536 is below it on the machines this runs on.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
    if soft == resource.RLIM_INFINITY or soft >= target:
        return soft, soft, hard
    for candidate in (target, *(value for value in OPEN_FILES_FALLBACKS if value < target)):
        if candidate <= soft:
            break
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (candidate, hard))
        except (ValueError, OSError):
            continue          # refused (EINVAL or EPERM); try lower
        break
    return soft, resource.getrlimit(resource.RLIMIT_NOFILE)[0], hard


def max_connections(soft_limit: int) -> int:
    """C-16.7: how many client connections the daemon holds at once for this limit."""
    if soft_limit == resource.RLIM_INFINITY:
        return CONNECTIONS_CEILING
    return max(CONNECTIONS_FLOOR,
               min(CONNECTIONS_CEILING, (soft_limit - DESCRIPTOR_RESERVE) // 2))


def limit_for_display(value: int) -> int | None:
    """An rlimit for JSON: `None` is unlimited, never RLIM_INFINITY's 2**63 - 1."""
    return None if value == resource.RLIM_INFINITY else int(value)


def open_descriptors() -> int | None:
    """How many descriptors this process has open, or None when it cannot tell.

    Listing `/dev/fd` itself opens one, which is not counted. With none to
    spare the listing fails, and that is reported as None, not as zero.
    """
    try:
        return len(os.listdir("/dev/fd")) - 1
    except OSError:
        return None


def read_only(op: str, args: dict) -> bool:
    """C-16.7: whether a request's only effect is its reply."""
    if op in READ_ONLY_OPS:
        return True
    if op == "ping":
        return not args.get("text")            # a ping with text records a notice
    if op == "lanes":
        return args.get("action") in (None, "", "list")
    return False


def client_gone(conn: socket.socket) -> bool:
    """C-16.7: True only once the client has closed its whole socket.

    A client that shut down just its write half is still reading its reply and
    is not gone. On macOS `getpeername` tells the two apart: it fails with
    EINVAL once the peer's socket is closed, even while that client's request
    is still unread in ours, and succeeds after a half-close. Elsewhere this
    may never report a client gone, which leaves every request running.
    """
    try:
        conn.getpeername()
    except OSError:
        return True
    return False


class Oversized:
    """A request line longer than `MAX_REQUEST_BYTES`; its bytes are dropped."""

    def __repr__(self) -> str:
        return "OVERSIZED"


OVERSIZED = Oversized()


class LineFramer:
    """Request lines out of a byte stream, however `recv` happened to chunk it.

    Each line keeps its newline. A line whose length with the newline would
    exceed `limit` is reported once as `OVERSIZED` and the rest of it, up to
    and including its newline, is discarded, so its tail is never read as a
    request of its own. At end of stream a final line without a newline is
    returned as it is, as `readline` would return it.
    """

    def __init__(self, limit: int = MAX_REQUEST_BYTES):
        self.limit = limit
        self._buffer = bytearray()
        self._discarding = False

    def feed(self, data: bytes) -> list[bytes | Oversized]:
        lines: list[bytes | Oversized] = []
        start = 0
        while start < len(data):
            newline = data.find(b"\n", start)
            end = len(data) if newline < 0 else newline + 1
            piece = data[start:end]
            start = end
            if self._discarding:
                if newline >= 0:
                    self._discarding = False
                continue
            if len(self._buffer) + len(piece) > self.limit:
                self._buffer.clear()
                lines.append(OVERSIZED)
                self._discarding = newline < 0
                continue
            self._buffer += piece
            if newline >= 0:
                lines.append(bytes(self._buffer))
                self._buffer.clear()
        return lines

    def finish(self) -> list[bytes | Oversized]:
        """At end of stream: the unterminated last line, if there is one."""
        tail, self._buffer = bytes(self._buffer), bytearray()
        self._discarding = False
        return [tail] if tail else []


def kernel_open_files_ceiling() -> int | None:
    """macOS `kern.maxfilesperproc`, or None where it cannot be read."""
    if sys.platform != "darwin":
        return None
    try:
        done = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.maxfilesperproc"],
                              capture_output=True, text=True, timeout=5, check=False)
        return int(done.stdout.strip()) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def launchd_open_files(ceiling: int | None = None) -> int:
    """C-16.6: the `NumberOfFiles` soft limit `daemon install` writes into the plist.

    launchd applies it before the daemon runs, so a daemon that could not raise
    its own limit still starts with room. It is never above the kernel's
    per-process ceiling, since descriptors past that cannot be opened anyway.
    """
    ceiling = kernel_open_files_ceiling() if ceiling is None else ceiling
    return OPEN_FILES_WANTED if not ceiling else min(OPEN_FILES_WANTED, ceiling)


def plist_open_files(path: Path) -> int | None:
    """The soft `NumberOfFiles` an installed plist sets, or None when it sets none."""
    try:
        data = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    value = (data.get("SoftResourceLimits") or {}).get("NumberOfFiles") if isinstance(data, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None
