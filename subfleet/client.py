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
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from . import boot_identity

from .contracts import Exit
from .protocol import (PROTOCOL_VERSION, ListArgs, ProtocolError, Request, Response,
                        decode_response, encode)

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
#: The clock and sleep `Client.call`'s busy retries use; tests replace these.
_clock = time.monotonic
_sleep = time.sleep


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

    @property
    def busy(self) -> bool:
        """C-16.7: the daemon was at its connection cap and answered before
        reading the request. It sends 69 over the socket for nothing else, so
        the request did nothing and may be sent again as it is."""
        return self.code == Exit.DAEMON_UNAVAILABLE


def busy_pause(streak: int) -> float:
    """C-16.7: the wait after the `streak`th busy answer in a row: 50 ms doubling
    to 1 s, less up to half at random so refused clients do not return together."""
    return min(1.0, BUSY_PAUSE_S * 2 ** min(streak - 1, 10)) * (1 - random.random() / 2)


class ResponseLost(ProtocolError):
    """The request was sent and no complete answer came back (C-16.3).

    Raised for every failure after `connect` succeeded: a timeout while sending
    or reading, a reset or broken pipe (even a `sendall` that fails part-way may
    have delivered the whole line), a close with no line, a line past
    `MAX_RESPONSE_BYTES`, and a line that does not decode. The daemon may have
    done the work, so the outcome is unknown, never "not done". It is a
    `ProtocolError` with exit 1 and the message the CLI has always printed, so
    every handler written before it still reports it.
    """

    def __init__(self, message: str, *, op: str = "", request_id: str = ""):
        super().__init__(message, Exit.OPERATIONAL)
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
        self._checked = not verify_lock

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
        """Send one request, read one response, return its `result` (C-16.1).

        `DaemonUnavailable` before the request is sent, `ResponseLost` after it
        was sent and before a complete, decodable answer was read (C-16.3).
        A busy answer (C-16.7) is not an outcome: the daemon read nothing, so the
        same request is sent again after `busy_pause`. Retries happen only in the
        first half of the deadline, so a retry the daemon does read still has at
        least half the deadline to answer; one admitted with seconds left could
        time out and turn a clean "busy" into an unknown outcome (C-16.3). The
        first try has the whole deadline, and every message names it.
        """
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
                return self._call_once(op, args, request_id=request_id,
                                       timeout=deadline - elapsed if busy else deadline,
                                       stated=deadline)
            except DaemonError as exc:
                if not exc.busy or not self.retry_busy:
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
        self.check_available()
        deadline = timeout
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
                line = _read_line(conn, time.monotonic() + deadline)
            except TimeoutError as exc:
                raise ResponseLost(f"no response from the daemon within {stated:g}s",
                                   op=op, request_id=request_id) from exc
            except ProtocolError as exc:          # a line past MAX_RESPONSE_BYTES
                raise ResponseLost(str(exc), op=op, request_id=request_id) from exc
            except OSError as exc:
                raise ResponseLost(f"daemon connection failed: {exc}",
                                   op=op, request_id=request_id) from exc
        finally:
            conn.close()
        if not line.strip():
            raise ResponseLost("the daemon closed the connection without a response",
                               op=op, request_id=request_id)
        try:
            response: Response = decode_response(line)
        except ProtocolError as exc:              # not JSON: "malformed response: ..."
            raise ResponseLost(str(exc), op=op, request_id=request_id) from exc
        except (AttributeError, TypeError, ValueError) as exc:
            raise ResponseLost(f"malformed response: {exc}", op=op,
                               request_id=request_id) from exc
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
        except ResponseLost as exc:
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
