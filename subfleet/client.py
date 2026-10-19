"""Unix-socket client for the subfleet daemon.

One request line, one response line (C-16.1). The client is the only place in
the CLI that knows about sockets; every verb either gets a result dict back or
sees `DaemonUnavailable`, which the CLI maps to exit 69 (C-17.3).

Connecting is the line between "not sent" and "not known" (C-16.3). A refused
connection means the request never reached a daemon, so it is
`DaemonUnavailable`. Anything that goes wrong after the connection is made (a
timeout, a reset, a close with no answer, a line that cannot be decoded) is
`ResponseLost`: the daemon may have committed the request and written its
answer after the client stopped reading. `Client.call_settled` sends an
idempotent request (`submit`, `kill`) once more with the same request id before
it will call the outcome unknown (incident: 2026-09-24, a `run --batch` entry
reported "NOT submitted" had been committed about a second before the 15 s
deadline, and the manual retry, under a fresh request id, created a duplicate
job).

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

import errno
import json
import os
import random
import re
import socket
import subprocess
import time
import uuid
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import boot_identity, ids

from .contracts import Exit
from .protocol import (PROTOCOL_VERSION, REFUSAL_MESSAGES, ListArgs, ProtocolError,
                        Request, Response,
                        correlation_id, decode_response, encode)

TABLE_CODES = {int(code) for code in Exit}          # C-17.3

SOCKET_NAME = "daemon.sock"
LOCK_NAME = "daemon.lock"
LOG_NAME = "daemon.log"
DEFAULT_TIMEOUT_S = 15.0
# C-16.3: the deadline of the one re-send of an unanswered `submit` or `kill`.
# Longer than the first on purpose: a daemon that did not answer in 15 s is busy
# (submit runs under one lock on the shared worker pool, C-16.4), and the re-send
# waits behind the request it repeats.
REQUERY_TIMEOUT_S = 60.0
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_STATE_ROOT = "~/.subfleet"
#: C-16.7: the errors of a request's send that met a socket the daemon had
#: already answered and closed (at its connection cap it answers `busy` before
#: reading): BrokenPipeError (EPIPE, ESHUTDOWN), ConnectionResetError, and, on
#: macOS when the close races the send, ENOTCONN (errno 57). Measured against a
#: unix-socket server that answers and closes at once, 8,000 sends under Python
#: 3.12 and 3.14: 510 met EPIPE and 6 ENOTCONN, and the answer was readable
#: after every one of them.
SEND_MET_CLOSE_ERRNOS = frozenset({errno.EPIPE, errno.ESHUTDOWN, errno.ECONNRESET,
                                   errno.ENOTCONN})
START_DAEMON_FIX = "subfleet daemon start"
#: C-16.7: the first pause before a busy answer's request is sent again.
BUSY_PAUSE_S = .05
#: C-5.13: a token bucket allows four fresh observations immediately, then
#: refills at one per interval, including failures and post-send diagnostics.
HOLDER_PROBE_INTERVAL_S = 1.0
HOLDER_PROBE_BURST = 4
HOLDER_PROBE_BUDGET_S = 5.0
#: Scope the entire observation, including UUID/legacy boot reads and ps, to
#: one absolute deadline. Context-local so simultaneous clients cannot share it.
_observation_deadline: ContextVar[float | None] = ContextVar('observation_deadline', default=None)
#: The clock and sleep `Client.call`'s busy retries use; tests replace these.
_clock = time.monotonic
_sleep = time.sleep


def silent_daemon_fix(pid: int) -> str:
    """What to do about a daemon that holds the lock and answers nothing.

    Only for a `pid` the identity check of C-5.3 has just verified as running:
    that is what makes "not a second daemon" true, because the flock of C-5.8 is
    held by a process that exists. A lock that is absent, unreadable, or whose
    holder cannot be verified says nothing about a second daemon, and the fix
    there is the ordinary `subfleet daemon start` (C-5.13).
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
    from .home_paths import home_path
    root = Path(home_path("SUBFLEET_HOME", lambda: DEFAULT_STATE_ROOT, env=env)).expanduser()
    return root if root.is_absolute() else Path.cwd() / root


class DaemonUnavailable(Exception):
    """No daemon is listening; the CLI falls back to offline mode or exits 69."""

    code = Exit.DAEMON_UNAVAILABLE

    def __init__(self, message: str, fix: str = START_DAEMON_FIX):
        super().__init__(message)
        self.fix = fix


class DaemonStopped(DaemonUnavailable):
    """The lock holder is alive but stopped, so it will never answer (C-5.13).

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
        # An enum selected by exact template validation, never diagnostic prose.
        # Suggestion CLI errors reconstruct templates from this enum, validated
        # structural fields and the exit code; arbitrary daemon prose is excluded.
        from .conversations.suggestions import response_details, response_reason
        self.suggestion_reason = response_reason(message)
        self.suggestion_details = response_details(message)

    @property
    def busy(self) -> bool:
        """C-16.7: the daemon was at its connection cap and answered before
        reading the request. It sends 69 over the socket for nothing else, so
        the request did nothing and may be sent again as it is."""
        return self.code == Exit.DAEMON_UNAVAILABLE


#: C-16.7: how long a loop with no deadline of its own keeps taking refused
#: connects after a busy answer as busy while `daemon.lock` cannot say whether its
#: holder lives (no lock, no usable pid, `ps` blocked by a sandbox); past it the
#: daemon is reported absent (review of 4fc5b49, P2).
REFUSED_UNVERIFIED_MAX_S = 60.0


def refused_while_busy(client: Any, exc: BaseException, *,
                       deadline_at: float | None = None) -> bool | None:
    """C-16.7: whether a `DaemonUnavailable` that followed a busy answer is that busy
    daemon's full listen backlog. True: a refused connect (ECONNREFUSED) while
    `daemon.lock` names a living daemon, so busy. None: refused, but the lock
    cannot say whether its holder lives, which a caller with no deadline of its own
    takes as busy for at most `REFUSED_UNVERIFIED_MAX_S` (a busy daemon must not
    send a sandboxed caller, which cannot run `ps`, offline). False: a socket gone
    (the daemon stopped and unlinked it), a holder that is dead, or any other
    failure, so absent, and no loop asks for ever (reviews of eac0706 and
    4fc5b49). The lock check costs a `ps` and a `sysctl`, paid only on this path."""
    if isinstance(exc, DaemonStopped):
        return False
    if not isinstance(getattr(exc, "__cause__", None), ConnectionRefusedError):
        return False
    alive = getattr(client, "lock_holder_alive", None)
    if alive is None:
        return None
    try:
        verdict = alive() if deadline_at is None else alive(deadline_at=deadline_at)
    except Exception:                                   # noqa: BLE001 - unverifiable is not dead
        return None
    return None if verdict is None else bool(verdict)


def busy_pause(streak: int) -> float:
    """C-16.7: the wait after the `streak`th busy answer in a row: 50 ms doubling
    to 1 s, less up to half at random so refused clients do not return together."""
    return min(1.0, BUSY_PAUSE_S * 2 ** min(streak - 1, 10)) * (1 - random.random() / 2)


class RequestTimeout(ProtocolError):
    """The request budget ended before sending; no outcome needs settling."""

    def __init__(self):
        super().__init__("request deadline elapsed before sending to the daemon",
                         Exit.OPERATIONAL)


class ResponseLost(ProtocolError):
    """The request was sent and no complete answer came back (C-16.3).

    Raised for every failure after `connect` succeeded: a timeout while sending
    or reading, a reset or broken pipe (even a `sendall` that fails part-way may
    have delivered the whole line), a close with no line, a line past
    `MAX_RESPONSE_BYTES`, or a complete line that does not decode. The daemon may have
    done the work, so the outcome is unknown, never "not done". It is a
    `ProtocolError` with exit 1 and the message the CLI has always printed, so
    every handler written before it still reports it.
    """

    def __init__(self, message: str, *, op: str = "", request_id: str = "",
                 fix: str | None = None):
        super().__init__(message, Exit.OPERATIONAL, fix)
        self.op = op
        self.request_id = request_id


class OutcomeUnknown(ResponseLost):
    """An idempotent request sent twice and answered neither time (C-16.3).

    `reasons` holds why each try went unanswered, in order. The caller reports
    the outcome as unknown and names the request id and the command that
    settles it; it never reports the request as refused or not done.
    """

    def __init__(self, op: str, request_id: str, reasons: Iterable[str]):
        self.reasons = tuple(str(reason) for reason in reasons)
        super().__init__(f"the outcome of {op} is unknown: " + "; then ".join(self.reasons),
                         op=op, request_id=request_id)


# --- Process identity (C-5.3), kept local to the client ----------------------

# `ps` renders lstart in the caller's locale and timezone, and the recorded
# value was rendered by whoever wrote it. Both sides must pin the rendering or
# two views of one live process compare unequal (C-5.3).
PS_ENV = {"LC_ALL": "C", "LANG": "C", "TZ": "UTC",
          "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _run(argv: list[str], timeout: float = 5.0) -> tuple[int, str]:
    deadline = _observation_deadline.get()
    if deadline is not None:
        timeout = min(timeout, deadline - _clock())
        if timeout <= 0:
            return -1, ""             # exhausted observation is unverifiable
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=PS_ENV)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    if deadline is not None and _clock() > deadline:
        return -1, ""                 # no late observation establishes identity
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
    if not out.strip():
        # rc 1 with no output is "no such process"; anything else is unverifiable.
        return ("", "") if rc == 1 else (None, None)
    if rc != 0:
        return None, None                # failed stdout establishes no identity
    rows = out.strip().splitlines()
    if len(rows) != 1:
        return None, None
    fields = rows[0].split()
    if (len(fields) != 6 or not re.fullmatch(r"[DIRSTUWXZt][+<NELVWXslt]*", fields[0])
            or fields[1] not in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
            or fields[2] not in (months := "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())
            or not re.fullmatch(r"\d{1,2}", fields[3])
            or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", fields[4])
            or not re.fullmatch(r"\d{4}", fields[5])):
        return None, None
    try:
        datetime(int(fields[5]), months.index(fields[2]) + 1, int(fields[3]),
                 *(int(part) for part in fields[4].split(":")))
    except ValueError:
        return None, None
    return fields[0], " ".join(fields[1:])


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
    reported even for a verdict that did not depend on it, because C-5.13 asks
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


# --- Who stopped it (C-5.13) --------------------------------------------------

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
    a pauser writes may turn the diagnosis into a traceback (C-5.13).
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
    """Message, fix, and the command the fix names, for a stopped holder (C-5.13).

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
        remaining = deadline_at - _clock()
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
                 timeout: float | None = None, verify_lock: bool = True, retry_busy: bool = True):
        self.root = Path(root).expanduser() if root is not None else state_root()
        # Read at construction, not at definition, so a test can shorten it.
        self.timeout = DEFAULT_TIMEOUT_S if timeout is None else timeout
        # C-16.7: False for a caller with a faster answer than waiting, such as
        # a prompt hook that reads the store offline when the daemon is busy.
        self.retry_busy = retry_busy
        # C-15.6: a hook passes False. The lock check costs a `ps` and a `sysctl`
        # per process, and a hook, run on every Bash call of every session, says
        # nothing whichever way the daemon is down; a refused connect says it.
        self._verify_lock = verify_lock
        # Virtual time for a full token bucket: at most BURST observations
        # immediately, with one token refilled every INTERVAL seconds.
        self._holder_probe_next = _clock()

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

    def holder_report(self, *, deadline_at: float | None = None
                      ) -> tuple[int | None, bool | None, str, str | None]:
        """The recorded holder's pid, liveness, reason, and `ps` state (C-5.8).

        One `ps` per call, the same one the identity check makes, so the
        stopped-holder question of C-5.13 is answered at no extra cost. A reused
        client takes a token from its four-observation bucket, including for a failed read;
        it never sends on the strength of a cached running observation. Slot
        waits consume the request's budget. An already available diagnostic
        may finish after that budget. Every stage shares one five-second
        observation deadline, also capped at the call deadline plus five
        seconds. Preflight and silence diagnosis share that absolute ceiling.
        """
        info = self.lock_info()
        if info is None:
            return None, None, f"there is no {self.lock_path}", None
        pid = info.get("pid")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None, None, f"{self.lock_path} records no usable pid", None
        due = self._holder_probe_next - (HOLDER_PROBE_BURST - 1) * HOLDER_PROBE_INTERVAL_S
        waited = due > _clock()
        while (remaining := due - _clock()) > 0:
            if deadline_at is not None:
                left = deadline_at - _clock()
                if left <= 0:
                    raise RequestTimeout()
                remaining = min(remaining, left)
            _sleep(remaining)
        if waited and deadline_at is not None and _clock() >= deadline_at:
            raise RequestTimeout()
        self._holder_probe_next = max(_clock(), self._holder_probe_next) + HOLDER_PROBE_INTERVAL_S
        observation_end = _clock() + HOLDER_PROBE_BUDGET_S
        if deadline_at is not None:
            observation_end = min(observation_end, deadline_at + HOLDER_PROBE_BUDGET_S)
        token = _observation_deadline.set(observation_end)
        try:
            alive, reason, state = identity_status(pid, info.get("boot_id"),
                                                    info.get("proc_start"))
        finally:
            _observation_deadline.reset(token)
        return pid, alive, reason, state

    def lock_report(self, *, deadline_at: float | None = None) -> tuple[bool | None, str]:
        """Liveness of the recorded lock holder, with the reason (C-5.8)."""
        return self.holder_report(deadline_at=deadline_at)[1:3]

    def lock_holder_alive(self, *, deadline_at: float | None = None) -> bool | None:
        """Tri-state liveness of the recorded lock holder (C-5.8)."""
        return self.holder_report(deadline_at=deadline_at)[1]

    def stopped_holder(self, *, deadline_at: float | None = None) -> DaemonStopped | None:
        """`DaemonStopped` when the verified lock holder is stopped (C-5.13).

        Only a holder whose identity checks out is diagnosed: a state read for
        a pid that is not provably the recorded daemon says nothing about it.
        """
        return self.silence_report(deadline_at=deadline_at)[0]

    def silence_report(self, *, deadline_at: float | None = None
                       ) -> tuple[DaemonStopped | None, int | None]:
        """Why a daemon that did not answer did not, from one `ps` (C-5.13).

        `DaemonStopped` for a verified holder that is stopped; otherwise the pid
        of a holder this check verified as running, or None when the lock is
        absent, unreadable, or its holder unverifiable. That pid is what makes
        "do not start a second daemon" a true thing to say, because the flock of
        C-5.8 is then held by a process that exists; without one the ordinary
        `subfleet daemon start` is the fix after all.
        """
        try:
            pid, alive, _reason, state = self.holder_report(deadline_at=deadline_at)
        except RequestTimeout:
            # A diagnostic never waits past the request for a probe slot. A
            # slot already available may cost one final bounded observation.
            return None, None
        if alive and is_stopped(state):
            return DaemonStopped(pid, state, self.socket_path), pid
        return None, pid if alive else None

    def unreachable(self, why: str, *, deadline_at: float | None = None) -> DaemonUnavailable:
        """The exception for a socket that could not be reached (C-5.13).

        A refused connect is not only "nobody is listening". A stopped daemon
        accepts connections into its listen backlog, which the daemon sets to 64
        (subfleet/daemon.py), and once that is full every further connect is
        refused at once — measured on macOS, where a full AF_UNIX backlog raises
        `ConnectionRefusedError` rather than timing out, and where closing a
        queued client does not give the slot back. So the lock holder decides
        what a failed connect means, and `subfleet daemon start` is the answer
        only when there is no verified holder to fail the flock against.
        """
        stopped, holder = self.silence_report(deadline_at=deadline_at)
        if stopped is not None:
            return stopped
        if holder is None:
            return DaemonUnavailable(f"no daemon at {self.socket_path}: {why}")
        return DaemonUnavailable(
            f"pid {holder} holds {self.lock_path} but {self.socket_path} cannot "
            f"be reached: {why}", silent_daemon_fix(holder))

    def silent(self, why: str, *, op: str = "", request_id: str = "",
               deadline_at: float | None = None) -> ResponseLost:
        """The exception for a request that was sent and not answered (C-5.13, C-16.3).

        It is `ResponseLost` whatever the holder is doing, because the request
        was sent: a daemon stopped mid-call still has it in its buffer and acts
        on it when it is continued, so the outcome is unknown, never "not sent".
        The holder is asked again rather than remembered from `check_available`,
        since it may have been stopped mid-call, and the answer decides what the
        message names and what the fix is. A stopped holder is named with its
        own fix, and enables preflight even for a client that opted out, so its
        next call (the re-send of C-16.3 included) is refused as `DaemonStopped`
        before anything more is written into the paused buffer.
        """
        stopped, holder = self.silence_report(deadline_at=deadline_at)
        if stopped is not None:
            self._verify_lock = True
            return ResponseLost(f"{why}: {stopped}", op=op, request_id=request_id,
                                fix=stopped.fix)
        return ResponseLost(why, op=op, request_id=request_id,
                            fix=silent_daemon_fix(holder) if holder else START_DAEMON_FIX)

    def check_available(self, *, deadline_at: float | None = None) -> None:
        """Raise `DaemonUnavailable` when the lock says the daemon cannot answer.

        Every send gets one fresh state/identity observation, at most one `ps`.
        `holder_report` permits a burst of four, then one observation per second,
        so wait/watch/settlement loops cannot spin process probes. Waiting for
        that slot before sending also closes the cached-running race: a stopped
        holder accepts into its backlog without answering (C-5.13). Hooks can
        still explicitly opt out of preflight (C-15.6).
        """
        if not self._verify_lock:
            return
        pid, alive, reason, state = self.holder_report(deadline_at=deadline_at)
        if alive is False:
            raise DaemonUnavailable(f"{self.lock_path} is stale: {reason}")
        if alive and is_stopped(state):
            raise DaemonStopped(pid, state, self.socket_path)

    # --- the wire ------------------------------------------------------------

    def call(self, op: str, args: dict[str, Any] | None = None, *,
             request_id: str = "", timeout: float | None = None,
             retry_busy: bool | None = None) -> dict[str, Any]:
        """Send one request, read one response, return its `result` (C-16.1).

        `DaemonUnavailable` before the request is sent, `ResponseLost` after it
        was sent and before a complete, decodable answer was read (C-16.3).
        A busy answer (C-16.7) is not an outcome: the daemon read nothing, so the
        same request is sent again after `busy_pause`. Retries happen only in the
        first half of the deadline, so a retry the daemon does read still has at
        least half the deadline to answer; one admitted with seconds left could
        time out and turn a clean "busy" into an unknown outcome (C-16.3). The
        first try has the whole deadline, and every message names it.
        `retry_busy` overrides the client's own setting for this call: a long
        poll that loops takes busy as an empty poll instead, so its next poll
        has the whole deadline. Once a try was answered busy, a later try whose
        connect is refused reports busy too: a full listen backlog behind a busy
        daemon is not an absent one, and must not send a caller offline.
        """
        retry = self.retry_busy if retry_busy is None else retry_busy
        deadline = self.timeout if timeout is None else timeout
        started = _clock()
        busy: DaemonError | None = None
        streak = 0
        while True:
            elapsed = _clock() - started
            if busy is not None and elapsed > deadline / 2:
                # Checked when the retry would start, not predicted before the
                # pause: a slow machine can overrun the sleep or the last try.
                raise busy
            try:
                result = self._call_once(op, args, request_id=request_id,
                                         timeout=deadline - elapsed, stated=deadline)
                if _clock() > started + deadline:
                    raise ResponseLost(f"no response from the daemon within {deadline:g}s",
                                       op=op, request_id=request_id)
                return result
            except DaemonUnavailable as exc:
                # An unverifiable lock counts as busy here: the half-deadline bounds it.
                if busy is None or refused_while_busy(self, exc, deadline_at=started + deadline) is False:
                    raise
                raise busy from None
            except DaemonError as exc:
                if not exc.busy or not retry:
                    raise
                streak += 1
                pause = busy_pause(streak)
                if _clock() - started + pause > deadline / 2:
                    raise
                busy = exc
                _sleep(pause)

    def _call_once(self, op: str, args: dict[str, Any] | None, *,
                   request_id: str, timeout: float, stated: float) -> dict[str, Any]:
        """One connection and one request; `timeout` bounds it, `stated` is the
        caller's deadline, which a lost answer's message names."""
        deadline_at = _clock() + timeout
        if _clock() >= deadline_at:
            raise RequestTimeout()
        wire_id = request_id
        if op == "submit" and request_id:
            ids.request_id(request_id)
            try:
                correlation_id(request_id)
            except ProtocolError:
                # C-6.2 keys can contain punctuation or Unicode; the reflected
                # protocol id has its own alphabet and never changes that key.
                wire_id = str(uuid.uuid4())
        self.check_available(deadline_at=deadline_at)
        remaining = deadline_at - _clock()
        if remaining <= 0:
            raise RequestTimeout()
        request = Request(op=op, args=args or {}, id=wire_id)
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(remaining)
        try:
            try:
                conn.connect(str(self.socket_path))
            except OSError as exc:
                raise self.unreachable(str(exc), deadline_at=deadline_at) from exc
            remaining = deadline_at - _clock()
            if remaining <= 0:
                raise RequestTimeout()
            conn.settimeout(remaining)
            # From here on the daemon may have the request (C-16.3).
            try:
                try:
                    conn.sendall(encode(request))
                except OSError as exc:
                    # C-16.7: a daemon at its connection cap answers at once and
                    # closes before reading the request. Its answer says why, so
                    # a send that met that close is read past, whichever errno
                    # the kernel gave it; anything else is a lost answer.
                    if exc.errno not in SEND_MET_CLOSE_ERRNOS:
                        raise
                line = _read_line(conn, deadline_at)
            except TimeoutError as exc:
                raise self.silent(f"no response from the daemon within {stated:g}s",
                                  op=op, request_id=request_id, deadline_at=deadline_at) from exc
            except ProtocolError as exc:          # a line past MAX_RESPONSE_BYTES
                raise ResponseLost(str(exc), op=op, request_id=request_id) from exc
            except OSError as exc:
                raise ResponseLost(f"daemon connection failed: {exc}",
                                   op=op, request_id=request_id) from exc
        finally:
            conn.close()
        if _clock() > deadline_at:
            raise ResponseLost(f"no response from the daemon within {stated:g}s",
                               op=op, request_id=request_id)
        if not line.strip():
            raise ResponseLost("the daemon closed the connection without a response",
                               op=op, request_id=request_id)
        try:
            response: Response = decode_response(line)
        except (ProtocolError, AttributeError, TypeError, ValueError):
            # An undecodable reply settles nothing (C-16.3). Keep its contents
            # and parser exceptions out of the diagnostic used for the retry.
            raise ResponseLost(REFUSAL_MESSAGES["malformed-response"],
                               op=op, request_id=request_id) from None
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

    # --- settling a lost answer (C-16.3) -------------------------------------

    def call_settled(self, op: str, args: dict[str, Any] | None = None, *,
                     request_id: str = "", minted: bool = False,
                     requery_timeout: float | None = None,
                     on_lost: Callable[[ResponseLost], None] | None = None) -> dict[str, Any]:
        """`call` for an idempotent op whose answer must not be guessed (C-16.3).

        `submit` is idempotent per request id (C-6.2) and `kill` per job
        (C-7.1), so when the first answer is lost the identical request (same
        op, same arguments, same request id) is sent once more with the longer
        `REQUERY_TIMEOUT_S` deadline, and its answer is the outcome. The result
        then carries `requeried: True`; for `submit`, `created: false` there
        means the first, unanswered request (or, for a caller-supplied id, an
        earlier one) created the job. Answered neither time, it raises
        `OutcomeUnknown`.

        Three edges of the re-send:

        * The daemon is gone (`DaemonUnavailable`). The first request may have
          committed, so a `submit` is `OutcomeUnknown`. Anything else (`kill`)
          re-raises, and the CLI falls back to offline mode (C-17.5), which is
          what the operator asked for.
        * The daemon answers the re-send "busy" (C-16.7). It read nothing, so
          that says nothing about the first request: `call` sends it again,
          with `busy_pause` between tries, until the re-send's deadline, and a
          daemon still busy then leaves the outcome unknown, never refused.
        * The re-sent `submit` is refused. That alone does not prove the first
          created nothing: `submit` validates before it looks up the request id,
          and a checkout whose HEAD moved between the two is "a different
          payload" (C-6.2). So the request id is looked up. A job found is this
          invocation's own when `minted` (the caller made the id up for this
          call, so nothing else can hold it) and is returned as the answer, with
          the refusal as `refused`; for a supplied id the refusal stands and
          names that job. Found nowhere, the refusal stands; if the lookup
          itself fails, the outcome is unknown.

        `on_lost` is called once, with the first failure, before the re-send, so
        a CLI can say on stderr why it is asking again.
        """
        try:
            return self.call(op, args, request_id=request_id)
        except ResponseLost as exc:
            first = exc
        if on_lost is not None:
            on_lost(first)
        deadline = (max(REQUERY_TIMEOUT_S, self.timeout) if requery_timeout is None
                    else requery_timeout)
        try:
            result = self.call(op, args, request_id=request_id, timeout=deadline)
        except (ResponseLost, RequestTimeout) as exc:
            raise OutcomeUnknown(op, request_id, (str(first), str(exc))) from exc
        except DaemonUnavailable as exc:
            if op != "submit":
                raise
            raise OutcomeUnknown(op, request_id, (
                str(first), f"the daemon went away before the re-sent request: {exc}")) from exc
        except DaemonError as exc:
            if exc.busy:
                return self._settle_busy_resend(op, exc, request_id, minted=minted, first=first)
            if op != "submit" or not request_id:
                raise
            return self._settle_refused_submit(exc, request_id, minted=minted, first=first)
        return {**result, "requeried": True}

    def _settle_busy_resend(self, op: str, busy: DaemonError, request_id: str, *,
                            minted: bool, first: ResponseLost) -> dict[str, Any]:
        """The re-send met only busy answers (C-16.7): it was never read, so it
        settles nothing, and the first request's outcome is still unknown (C-16.3).

        A submit under a request id this call minted is answered by a job that
        carries the id, as for a refused re-send. Anything else is reported as
        unknown, never as busy: exit 69 would read as "nothing was sent".
        """
        reasons = [str(first), f"the re-sent request was not read: {busy}"]
        if op == "submit" and request_id:
            try:
                job = self.find_request(request_id)
            except (DaemonUnavailable, DaemonError, ProtocolError) as exc:
                raise OutcomeUnknown(op, request_id, (
                    *reasons, f"its request id could not be looked up: {exc}")) from exc
            if job is not None and minted:
                return {"job_id": job.get("job_id"), "request_id": request_id, "created": False,
                        "state": job.get("state"), "requeried": True, "busy": str(busy)}
            if job is not None:
                reasons.append(f"job {job.get('job_id')} carries request id {request_id}")
        raise OutcomeUnknown(op, request_id, reasons) from busy

    def _settle_refused_submit(self, refusal: DaemonError, request_id: str, *,
                               minted: bool, first: ResponseLost) -> dict[str, Any]:
        """The re-sent submit was refused: does a job carry its request id? (C-16.3)"""
        try:
            job = self.find_request(request_id)
        except (DaemonUnavailable, DaemonError, ProtocolError) as exc:
            raise OutcomeUnknown("submit", request_id, (
                str(first), f"the re-sent request was refused ({refusal})",
                f"its request id could not be looked up: {exc}")) from exc
        if job is None:
            raise refusal
        job_id = job.get("job_id")
        if minted:
            return {"job_id": job_id, "request_id": request_id, "created": False,
                    "state": job.get("state"), "requeried": True, "refused": str(refusal)}
        message = str(refusal)
        if not job_id or str(job_id) not in message:
            message = f"{message}; job {job_id} carries request id {request_id}"
        raise DaemonError(refusal.code, message,
                          refusal.fix or f"subfleet runs show {job_id}") from refusal

    def find_request(self, request_id: str) -> dict[str, Any] | None:
        """The job row carrying `request_id`, or None (C-6.2, C-16.3).

        `list` filters on `request_id`; a daemon older than that filter ignores
        the field and lists every job (C-16.2), so the rows are filtered here too.
        """
        args = asdict(ListArgs(request_id=request_id))
        # C-25.1: `kind` and `include_turns` go only to a daemon advertising
        # `jobs.kind.v1`; a request id names one job whatever its kind, and the
        # daemon's request-id filter ignores the turn default (C-26.12).
        del args["kind"], args["include_turns"]
        result = self.call("list", args)
        rows = result.get("jobs")
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("request_id") == request_id:
                return row
        return None
