"""Unix-socket client for the subfleet daemon.

One request line, one response line (C-16.1). The client is the only place in
the CLI that knows about sockets; every verb either gets a result dict back or
sees `DaemonUnavailable`, which the CLI maps to exit 69 (C-17.3).

Liveness has two sources of truth. The socket answering is the strong one. The
weak one is `daemon.lock`, whose recorded identity the CLI treats as no daemon
when the recorded process is provably gone (C-5.8): a stale socket file left by
a killed daemon otherwise looks like a daemon that is merely slow. The identity
check is `ps -p <pid> -o state=,lstart=` against the recorded `proc_start` and
`kern.bootsessionuuid` against the recorded `boot_id` (C-5.3), with conservative
support for older wall-clock boot timestamps. Process inspection is pinned to
`LC_ALL=C` and `TZ=UTC` because `lstart` is rendered in the reader's locale and
whoever recorded the value rendered it in theirs. It is kept small and local here
so the CLI does not import the daemon-side `procs` module.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from . import boot_identity

from .contracts import Exit
from .protocol import (PROTOCOL_VERSION, ProtocolError, Request, Response,
                        decode_response, encode)

TABLE_CODES = {int(code) for code in Exit}          # C-17.3

SOCKET_NAME = "daemon.sock"
LOCK_NAME = "daemon.lock"
LOG_NAME = "daemon.log"
DEFAULT_TIMEOUT_S = 15.0
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_STATE_ROOT = "~/.subfleet"
START_DAEMON_FIX = "subfleet daemon start"


def state_root(env: dict[str, str] | None = None) -> Path:
    """`$SUBFLEET_HOME`, default `~/.subfleet/`, always absolute (C-2.1).

    A relative `SUBFLEET_HOME` would otherwise mean a different directory for
    every caller's cwd, and a file URI cannot express one at all.
    """
    env = os.environ if env is None else env
    root = Path(env.get("SUBFLEET_HOME") or DEFAULT_STATE_ROOT).expanduser()
    return root if root.is_absolute() else Path.cwd() / root


class DaemonUnavailable(Exception):
    """No daemon is listening; the CLI falls back to offline mode or exits 69."""

    code = Exit.DAEMON_UNAVAILABLE

    def __init__(self, message: str, fix: str = START_DAEMON_FIX):
        super().__init__(message)
        self.fix = fix


class DaemonError(Exception):
    """An `ok: false` response (C-16.1); carries the daemon's exit code."""

    def __init__(self, code: int, message: str, fix: str | None = None):
        super().__init__(message)
        self.code = code
        self.fix = fix


# --- Process identity (C-5.3), kept local to the client ----------------------

# `ps` renders lstart in the caller's locale and timezone, and the recorded
# value was rendered by whoever wrote it. Both sides must pin the rendering or
# two views of one live process compare unequal (C-5.3).
PS_ENV = {"LC_ALL": "C", "LANG": "C", "TZ": "UTC",
          "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _run(argv: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=PS_ENV)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return done.returncode, done.stdout


_BOOT_ID: list[str | None] = []          # only boot-session UUIDs are immutable


def boot_id() -> str | None:
    """The boot-session UUID, with legacy seconds only when UUID is unavailable."""
    if _BOOT_ID:
        return _BOOT_ID[0]
    value = _read_boot_id()
    if value and boot_identity.session_uuid(value):
        _BOOT_ID.append(value)
    return value


def _read_boot_id() -> str | None:
    return boot_identity.read_identity(_boot_read)


def _boot_read(argv: list[str]) -> str:
    rc, out = _run(argv)
    return out if rc == 0 else ""


ZOMBIE_STATES = ("Z",)


def proc_start(pid: int) -> str | None:
    """The `lstart` column for `pid`; "" when the pid is gone, None when unknown.

    A zombie is not a live process (C-5.5), so it reads as gone. Only `ps`
    saying "no such process" (rc 1 with no output) is death; any other failure
    is unverifiable, because a refused `ps` is not evidence that a pid is free.
    """
    rc, out = _run(["/bin/ps", "-p", str(int(pid)), "-o", "state=,lstart="])
    if rc < 0:
        return None                      # ps itself did not run: unverifiable
    line = " ".join(out.split())
    if not line:
        return "" if rc == 1 else None   # rc 1 with no output is "no such process"
    state, _, started = line.partition(" ")
    if state.startswith(ZOMBIE_STATES):
        return ""                        # exited, not yet reaped: not live
    return started


def identity_report(pid: int | None, recorded_boot: str | None,
                    recorded_start: str | None) -> tuple[bool | None, str]:
    """The identity verdict of C-5.3 with the reason it was reached.

    True: the recorded process is running. False: it is provably gone (no such
    pid, a different start time, or a reboot since the record). None: the check
    could not be made, which is never treated as death.
    """
    if not pid:
        return None, "no pid was recorded"
    boot_match = True
    if recorded_boot:
        current = boot_id()
        if current is None:
            boot_match = None
        else:
            boot_match = boot_identity.matches(str(recorded_boot), str(current),
                                               lambda: boot_identity.boot_seconds(_boot_read))
        if boot_match is False:
            return False, (f"the machine booted at {current}, not at "
                           f"{recorded_boot} as recorded, so pid {pid} is gone")
    start = proc_start(int(pid))
    if start is None:
        return None, f"ps could not report on pid {pid}"
    if start == "":
        return False, f"there is no live process with pid {pid}"
    if not recorded_start:
        return None, f"pid {pid} is alive but no start time was recorded"
    if " ".join(str(recorded_start).split()) == start:
        if boot_match is None:
            return None, (f"pid {pid} has the recorded start time, but the legacy boot "
                          "timestamp changed or boot identity is unavailable; process death is unproven")
        return True, f"pid {pid} started at {start}, as recorded"
    return False, (f"pid {pid} started at {start!r}, not {recorded_start!r} as "
                   f"recorded — either the pid was reused, or the two sides "
                   f"rendered the start time differently (C-5.3 wants "
                   f"LC_ALL=C and TZ=UTC on both)")


def same_process(pid: int | None, recorded_boot: str | None,
                 recorded_start: str | None) -> bool | None:
    """Tri-state identity check (C-5.3); see `identity_report` for the reason."""
    return identity_report(pid, recorded_boot, recorded_start)[0]


def _read_line(conn: socket.socket, deadline_at: float) -> bytes:
    """One newline-terminated response line, bounded in both time and size.

    `settimeout` bounds each recv, not the whole read, so a peer that trickles
    bytes without ever sending a newline would otherwise hold the CLI forever.
    """
    chunks: list[bytes] = []
    size = 0
    while True:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("no complete response line before the deadline")
        conn.settimeout(remaining)
        chunk = conn.recv(65536)
        if not chunk:
            break                        # peer closed
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.append(chunk[:newline + 1])
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ProtocolError(
                f"the daemon sent more than {MAX_RESPONSE_BYTES} bytes without a "
                f"newline", Exit.OPERATIONAL)
    return b"".join(chunks)


class Client:
    """Connects to `<state root>/daemon.sock` and speaks the C-16 protocol."""

    def __init__(self, root: Path | str | None = None, *,
                 timeout: float = DEFAULT_TIMEOUT_S):
        self.root = Path(root).expanduser() if root is not None else state_root()
        self.timeout = timeout
        self._checked = False

    # --- paths ---------------------------------------------------------------

    @property
    def socket_path(self) -> Path:
        return self.root / SOCKET_NAME

    @property
    def lock_path(self) -> Path:
        return self.root / LOCK_NAME

    @property
    def log_path(self) -> Path:
        return self.root / LOG_NAME

    # --- liveness ------------------------------------------------------------

    def lock_info(self) -> dict[str, Any] | None:
        """`daemon.lock` as a dict, or None when it is absent or unreadable."""
        try:
            text = self.lock_path.read_text()
        except OSError:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def lock_report(self) -> tuple[bool | None, str]:
        """Liveness of the recorded lock holder, with the reason (C-5.8)."""
        info = self.lock_info()
        if info is None:
            return None, f"there is no {self.lock_path}"
        pid = info.get("pid")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None, f"{self.lock_path} records no usable pid"
        return identity_report(pid, info.get("boot_id"), info.get("proc_start"))

    def lock_holder_alive(self) -> bool | None:
        """Tri-state liveness of the recorded lock holder (C-5.8)."""
        return self.lock_report()[0]

    def check_available(self) -> None:
        """Raise `DaemonUnavailable` when the lock says the daemon is dead.

        Checked once per client: the check costs a `ps` and a `sysctl`, and a
        daemon that dies mid-conversation shows up as a refused connection
        anyway, which is the stronger signal.
        """
        if self._checked:
            return
        alive, reason = self.lock_report()
        if alive is False:
            raise DaemonUnavailable(f"{self.lock_path} is stale: {reason}")
        self._checked = True

    # --- the wire ------------------------------------------------------------

    def call(self, op: str, args: dict[str, Any] | None = None, *,
             request_id: str = "", timeout: float | None = None) -> dict[str, Any]:
        """Send one request, read one response, return its `result` (C-16.1)."""
        self.check_available()
        deadline = self.timeout if timeout is None else timeout
        request = Request(op=op, args=args or {}, id=request_id)
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(deadline)
        try:
            try:
                conn.connect(str(self.socket_path))
            except (FileNotFoundError, ConnectionRefusedError, PermissionError,
                    NotADirectoryError) as exc:
                raise DaemonUnavailable(f"no daemon at {self.socket_path}: {exc}") from exc
            except OSError as exc:
                raise DaemonUnavailable(f"cannot reach {self.socket_path}: {exc}") from exc
            try:
                try:
                    conn.sendall(encode(request))
                except (BrokenPipeError, ConnectionResetError):
                    # C-16.6: a daemon at its connection cap answers at once and
                    # closes before reading the request. Its answer says why.
                    pass
                line = _read_line(conn, time.monotonic() + deadline)
            except TimeoutError as exc:
                raise ProtocolError(
                    f"no response from the daemon within {deadline:g}s",
                    Exit.OPERATIONAL) from exc
            except OSError as exc:
                raise ProtocolError(f"daemon connection failed: {exc}",
                                    Exit.OPERATIONAL) from exc
        finally:
            conn.close()
        if not line.strip():
            raise ProtocolError("the daemon closed the connection without a response",
                                Exit.OPERATIONAL)
        try:
            response: Response = decode_response(line)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProtocolError(f"malformed response: {exc}", Exit.OPERATIONAL) from exc
        if response.v != PROTOCOL_VERSION:
            raise ProtocolError(
                f"the daemon speaks protocol version {response.v}; this CLI speaks "
                f"{PROTOCOL_VERSION}", Exit.OPERATIONAL)
        if response.error is not None or not response.ok:
            error = response.error
            raw = getattr(error, "code", None)
            message = getattr(error, "message", None) or "the daemon reported a failure"
            try:
                code = int(raw)
            except (TypeError, ValueError):
                code = int(Exit.OPERATIONAL)
            if code not in TABLE_CODES or code == int(Exit.OK):
                # C-17.3 gives every code one meaning; a code outside the table
                # (or a "failure" numbered 0) must not become the exit status.
                message = f"{message} (daemon reported code {raw!r})"
                code = int(Exit.OPERATIONAL)
            raise DaemonError(code, message, getattr(error, "fix", None))
        result = response.result
        return result if isinstance(result, dict) else {}
