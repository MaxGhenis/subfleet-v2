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

That one `ps` reads the state column as well, so the third condition of C-5.11
costs nothing to notice: a holder that is alive but stopped holds the lock and
answers nothing until it is continued. It is refused before the socket is
touched, and asked again whenever the socket fails — a connect that is refused
or times out, or a read that times out — as `DaemonStopped`. The same read
decides the fix for a daemon that is silent for a reason `ps` cannot see: only a
holder verified as running makes "not a second daemon" true (C-5.8).
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


def silent_daemon_fix(pid: int) -> str:
    """What to do about a daemon that holds the lock and answers nothing.

    Only for a `pid` the identity check of C-5.3 has just verified as running:
    that is what makes "not a second daemon" true, because the flock of C-5.8 is
    held by a process that exists. A lock that is absent, unreadable, or whose
    holder cannot be verified says nothing about a second daemon, and the fix
    there is the ordinary `subfleet daemon start` (C-5.11).
    """
    return (f"`subfleet daemon logs -n 40`, then `subfleet daemon stop` if it is "
            f"wedged; pid {pid} holds {LOCK_NAME}, so starting a second daemon "
            f"would only fail the lock")


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


class DaemonStopped(DaemonUnavailable):
    """The lock holder is alive but stopped, so it will never answer (C-5.11).

    Unavailable in exactly the sense C-17.5 means: the read verbs fall back to
    the store. The fix is the signal that continues this daemon, never a second
    one, which would fail the flock of C-5.8.
    """

    def __init__(self, pid: int, state: str | None,
                 socket_path: Path | str | None = None):
        message, fix, command = stopped_report(pid, state, socket_path)
        super().__init__(message, fix)
        self.pid = pid
        self.state = state
        #: The one command `fix` tells the operator to run. `fail()` prints a
        #: fix bare (subfleet/cli.py) and `doctor`'s rows backtick the command
        #: they name, so the span each surface needs is kept apart from the prose.
        self.command = command


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
#: BSD `ps` prints the state letter with its flags attached (`T`, `T+`, `TN`),
#: so the letter is matched as a prefix. `T` is SIGSTOP, SIGTSTP, or a debugger.
STOPPED_STATES = ("T",)


def proc_status(pid: int) -> tuple[str | None, str | None]:
    """The `state` and `lstart` columns for `pid`, from one `ps`.

    `(None, None)`: `ps` could not answer, which is never evidence about the
    pid. `("", "")`: there is no such process. Otherwise the raw state string
    (`S`, `T+`, `Z`, …) and the start time as `ps` rendered it under `PS_ENV`.
    """
    rc, out = _run(["/bin/ps", "-p", str(int(pid)), "-o", "state=,lstart="])
    if rc < 0:
        return None, None                # ps itself did not run: unverifiable
    line = " ".join(out.split())
    if not line:
        # rc 1 with no output is "no such process"; anything else is unverifiable.
        return ("", "") if rc == 1 else (None, None)
    state, _, started = line.partition(" ")
    return state, started


def is_stopped(state: str | None) -> bool:
    """Does this `ps` state say the process is stopped rather than running?"""
    return bool(state) and state.startswith(STOPPED_STATES)


def proc_start(pid: int) -> str | None:
    """The `lstart` column for `pid`; "" when the pid is gone, None when unknown.

    A zombie is not a live process (C-5.5), so it reads as gone. Only `ps`
    saying "no such process" (rc 1 with no output) is death; any other failure
    is unverifiable, because a refused `ps` is not evidence that a pid is free.
    """
    return _live_start(*proc_status(pid))


def _live_start(state: str | None, started: str | None) -> str | None:
    """`proc_status`'s answer as `proc_start` reports it: the zombie rule."""
    if state is None:
        return None
    if state == "":
        return ""
    if state.startswith(ZOMBIE_STATES):
        return ""                        # exited, not yet reaped: not live
    return started


def identity_status(pid: int | None, recorded_boot: str | None,
                    recorded_start: str | None) -> tuple[bool | None, str, str | None]:
    """`identity_report`, and the raw `ps` state the verdict was read from.

    The state is None whenever no `ps` was run or it could not answer; it is
    reported even for a verdict that did not depend on it, because C-5.11 asks
    a second question of the same process and this is the read that answers it.
    """
    if not pid:
        return None, "no pid was recorded", None
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
                           f"{recorded_boot} as recorded, so pid {pid} is gone"), None
    state, started = proc_status(int(pid))
    start = _live_start(state, started)
    if start is None:
        return None, f"ps could not report on pid {pid}", state
    if start == "":
        return False, f"there is no live process with pid {pid}", state
    if not recorded_start:
        return None, f"pid {pid} is alive but no start time was recorded", state
    if " ".join(str(recorded_start).split()) == start:
        if boot_match is None:
            return None, (f"pid {pid} has the recorded start time, but the legacy boot "
                          "timestamp changed or boot identity is unavailable; process death is unproven"), state
        return True, f"pid {pid} started at {start}, as recorded", state
    return False, (f"pid {pid} started at {start!r}, not {recorded_start!r} as "
                   f"recorded — either the pid was reused, or the two sides "
                   f"rendered the start time differently (C-5.3 wants "
                   f"LC_ALL=C and TZ=UTC on both)"), state


def identity_report(pid: int | None, recorded_boot: str | None,
                    recorded_start: str | None) -> tuple[bool | None, str]:
    """The identity verdict of C-5.3 with the reason it was reached.

    True: the recorded process is running. False: it is provably gone (no such
    pid, a different start time, or a reboot since the record). None: the check
    could not be made, which is never treated as death.
    """
    return identity_status(pid, recorded_boot, recorded_start)[:2]


def same_process(pid: int | None, recorded_boot: str | None,
                 recorded_start: str | None) -> bool | None:
    """Tri-state identity check (C-5.3); see `identity_report` for the reason."""
    return identity_report(pid, recorded_boot, recorded_start)[0]


# --- Who stopped it (C-5.11) --------------------------------------------------

#: Programs that stop other processes on purpose, each with the file it writes
#: the pids it paused into, one per line. A SIGSTOP leaves nothing on its target
#: that names the sender, so this table is the only way an attribution can be
#: made at all, and only for a pid the pauser's own file lists. Tests point
#: `marker` at a temp file by replacing this tuple.
KNOWN_PAUSERS: tuple[dict[str, str], ...] = (
    {"name": "clamshell-guard",
     "marker": "~/.local/state/clamshell-guard/paused.pids",
     "detail": "clamshell-guard paused it (it pauses heavy processes while the lid "
               "is closed on battery, and resumes them when the lid opens or power "
               "returns; see ~/bin/clamshell-guard-status)",
     # Not `kill -CONT`: the guard is the operator's thermal policy, and while
     # its condition holds it may stop the process again on a later pass.
     "fix": "open the lid or connect power; ~/bin/clamshell-guard-resume resumes "
            "everything it paused",
     "command": "~/bin/clamshell-guard-resume"},
)


def paused_by(pid: int) -> dict[str, str] | None:
    """The `KNOWN_PAUSERS` entry whose marker file lists `pid`, or None.

    A marker file belongs to another program and is not validated by anything
    here, so a file that cannot be read *or decoded* is simply no evidence;
    `read_text` decodes, and a `UnicodeDecodeError` is a `ValueError`. Nothing
    a pauser writes may turn the diagnosis into a traceback (C-5.11).
    """
    for pauser in KNOWN_PAUSERS:
        try:
            text = Path(pauser["marker"]).expanduser().read_text()
        except (OSError, ValueError):
            continue
        if any(line.strip() == str(pid) for line in text.splitlines()):
            return pauser
    return None


def stopped_report(pid: int, state: str | None,
                   socket_path: Path | str | None = None) -> tuple[str, str, str]:
    """Message, fix, and the command the fix names, for a stopped holder (C-5.11).

    The socket is named only when it is there: a holder that is stopped keeps
    whatever it had, and a daemon killed before it ever bound one leaves the
    lock alone. `daemon status` prints the socket's presence two lines above.
    """
    held = LOCK_NAME
    if socket_path is not None and Path(socket_path).exists():
        held = f"{LOCK_NAME} and {SOCKET_NAME}"
    message = (f"daemon pid {pid} is stopped (process state {state!r}: SIGSTOP or a "
               f"debugger); it holds {held} but cannot answer until it is continued")
    command = f"kill -CONT {pid}"
    pauser = paused_by(pid)
    if pauser is not None:
        return f"{message}; {pauser['detail']}", pauser["fix"], pauser["command"]
    return message, command, command


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

    def holder_report(self) -> tuple[int | None, bool | None, str, str | None]:
        """The recorded holder's pid, liveness, reason, and `ps` state (C-5.8).

        One `ps` per call, the same one the identity check makes, so the
        stopped-holder question of C-5.11 is answered at no extra cost.
        """
        info = self.lock_info()
        if info is None:
            return None, None, f"there is no {self.lock_path}", None
        pid = info.get("pid")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None, None, f"{self.lock_path} records no usable pid", None
        alive, reason, state = identity_status(pid, info.get("boot_id"),
                                               info.get("proc_start"))
        return pid, alive, reason, state

    def lock_report(self) -> tuple[bool | None, str]:
        """Liveness of the recorded lock holder, with the reason (C-5.8)."""
        return self.holder_report()[1:3]

    def lock_holder_alive(self) -> bool | None:
        """Tri-state liveness of the recorded lock holder (C-5.8)."""
        return self.holder_report()[1]

    def stopped_holder(self) -> DaemonStopped | None:
        """`DaemonStopped` when the verified lock holder is stopped (C-5.11).

        Only a holder whose identity checks out is diagnosed: a state read for
        a pid that is not provably the recorded daemon says nothing about it.
        """
        return self.silence_report()[0]

    def silence_report(self) -> tuple[DaemonStopped | None, int | None]:
        """Why a daemon that did not answer did not, from one `ps` (C-5.11).

        `DaemonStopped` for a verified holder that is stopped; otherwise the pid
        of a holder this check verified as running, or None when the lock is
        absent, unreadable, or its holder unverifiable. That pid is what makes
        "do not start a second daemon" a true thing to say, because the flock of
        C-5.8 is then held by a process that exists; without one the ordinary
        `subfleet daemon start` is the fix after all.
        """
        pid, alive, _reason, state = self.holder_report()
        if alive and is_stopped(state):
            return DaemonStopped(pid, state, self.socket_path), pid
        return None, pid if alive else None

    def unreachable(self, why: str) -> DaemonUnavailable:
        """The exception for a socket that could not be reached (C-5.11).

        A refused connect is not only "nobody is listening". A stopped daemon
        accepts connections into its listen backlog, which the daemon sets to 64
        (subfleet/daemon.py), and once that is full every further connect is
        refused at once — measured on macOS, where a full AF_UNIX backlog raises
        `ConnectionRefusedError` rather than timing out, and where closing a
        queued client does not give the slot back. So the lock holder decides
        what a failed connect means, and `subfleet daemon start` is the answer
        only when there is no verified holder to fail the flock against.
        """
        stopped, holder = self.silence_report()
        if stopped is not None:
            return stopped
        if holder is None:
            return DaemonUnavailable(f"no daemon at {self.socket_path}: {why}")
        return DaemonUnavailable(
            f"pid {holder} holds {self.lock_path} but {self.socket_path} cannot "
            f"be reached: {why}", silent_daemon_fix(holder))

    def check_available(self) -> None:
        """Raise `DaemonUnavailable` when the lock says the daemon cannot answer.

        Checked once per client: the check costs a `ps` and a `sysctl`, and a
        daemon that dies mid-conversation shows up as a refused connection
        anyway, which is the stronger signal. A stopped holder is caught here
        rather than on the wire, because it accepts the connection into its
        backlog and then answers nothing, so the whole timeout would be spent
        learning what this `ps` already said (C-5.11).
        """
        if self._checked:
            return
        pid, alive, reason, state = self.holder_report()
        if alive is False:
            raise DaemonUnavailable(f"{self.lock_path} is stale: {reason}")
        if alive and is_stopped(state):
            raise DaemonStopped(pid, state, self.socket_path)
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
            except OSError as exc:
                # Every way a connect can fail asks the holder, refused as well
                # as timed out: a full backlog is refused, and a backlog fills
                # precisely because nobody is accepting from it (C-5.11).
                raise self.unreachable(str(exc)) from exc
            try:
                conn.sendall(encode(request))
                line = _read_line(conn, time.monotonic() + deadline)
            except TimeoutError as exc:
                # The holder is asked again, not remembered from
                # `check_available`: it may have been stopped mid-call.
                stopped, holder = self.silence_report()
                if stopped is not None:
                    raise stopped from exc
                raise ProtocolError(
                    f"no response from the daemon within {deadline:g}s",
                    Exit.OPERATIONAL,
                    silent_daemon_fix(holder) if holder else START_DAEMON_FIX) from exc
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
