"""Unix-socket client for the subfleet daemon.

One request line, one response line (C-16.1). The client is the only place in
the CLI that knows about sockets; every verb either gets a result dict back or
sees `DaemonUnavailable`, which the CLI maps to exit 69 (C-17.3).

Liveness has two sources of truth. The socket answering is the strong one. The
weak one is `daemon.lock`, whose recorded identity the CLI treats as no daemon
when the recorded process is provably gone (C-5.8): a stale socket file left by
a killed daemon otherwise looks like a daemon that is merely slow. The identity
check is `ps -p <pid> -o lstart=` against the recorded `proc_start` and
`sysctl -n kern.boottime` against the recorded `boot_id` (C-5.3). It is kept
small and local here so the CLI does not import the daemon-side `procs` module.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Any

from .contracts import Exit
from .protocol import ProtocolError, Request, Response, decode_response, encode

SOCKET_NAME = "daemon.sock"
LOCK_NAME = "daemon.lock"
LOG_NAME = "daemon.log"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_STATE_ROOT = "~/.subfleet"
START_DAEMON_FIX = "subfleet daemon start"


def state_root(env: dict[str, str] | None = None) -> Path:
    """`$SUBFLEET_HOME`, default `~/.subfleet/` (C-2.1)."""
    env = os.environ if env is None else env
    return Path((env.get("SUBFLEET_HOME") or DEFAULT_STATE_ROOT)).expanduser()


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

def _run(argv: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return done.returncode, done.stdout


_BOOT_ID: list[str | None] = []          # boot time cannot change under us


def boot_id() -> str | None:
    """`kern.boottime` seconds as a string, or None when it cannot be read."""
    if _BOOT_ID:
        return _BOOT_ID[0]
    value = _read_boot_id()
    _BOOT_ID.append(value)
    return value


def _read_boot_id() -> str | None:
    rc, out = _run(["sysctl", "-n", "kern.boottime"])
    if rc != 0 or not out.strip():
        return None
    match = re.search(r"sec\s*=\s*(\d+)", out)
    if match:
        return match.group(1)
    digits = re.findall(r"\d+", out)
    return digits[0] if digits else None


def proc_start(pid: int) -> str | None:
    """The `lstart` column for `pid`; "" when the pid is gone, None when unknown."""
    rc, out = _run(["ps", "-p", str(int(pid)), "-o", "lstart="])
    if rc < 0:
        return None                      # ps itself did not run: unverifiable
    return " ".join(out.split())         # "" means no such process


def same_process(pid: int | None, recorded_boot: str | None,
                 recorded_start: str | None) -> bool | None:
    """Tri-state identity check (C-5.3).

    True: the recorded process is running. False: it is provably gone (no such
    pid, a different start time, or a reboot since the record). None: the check
    could not be made, which is never treated as death.
    """
    if not pid:
        return None
    if recorded_boot:
        current = boot_id()
        if current is not None and str(current) != str(recorded_boot):
            return False
    start = proc_start(int(pid))
    if start is None:
        return None
    if start == "":
        return False
    if not recorded_start:
        return None                      # alive, but identity unconfirmed
    return " ".join(str(recorded_start).split()) == start


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

    def lock_holder_alive(self) -> bool | None:
        """Tri-state liveness of the recorded lock holder (C-5.8)."""
        info = self.lock_info()
        if info is None:
            return None
        pid = info.get("pid")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        return same_process(pid, info.get("boot_id"), info.get("proc_start"))

    def check_available(self) -> None:
        """Raise `DaemonUnavailable` when the lock says the daemon is dead.

        Checked once per client: the check costs a `ps` and a `sysctl`, and a
        daemon that dies mid-conversation shows up as a refused connection
        anyway, which is the stronger signal.
        """
        if self._checked:
            return
        if self.lock_holder_alive() is False:
            info = self.lock_info() or {}
            raise DaemonUnavailable(
                f"daemon.lock records pid {info.get('pid')} which is no longer running")
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
                conn.sendall(encode(request))
                with conn.makefile("rb") as stream:
                    line = stream.readline()
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
        response: Response = decode_response(line)
        if response.error is not None or not response.ok:
            error = response.error
            code = getattr(error, "code", None)
            message = getattr(error, "message", None) or "the daemon reported a failure"
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = int(Exit.OPERATIONAL)
            raise DaemonError(code, message, getattr(error, "fix", None))
        result = response.result
        return result if isinstance(result, dict) else {}
