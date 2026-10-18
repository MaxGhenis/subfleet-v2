"""Person-only operations (C-25.6, design D-8).

Approving a tool call, resolving an ambiguous delivery, widening a permission
policy, setting `allow_main` and unblocking an unfinished turn are a person's
decisions. Immediately after accept, before any other connection work, the
daemon retains the socket's 32-byte audit token (`LOCAL_PEERTOKEN`), including
its pid and pid version. After the full request line is read, the person check
re-reads the token and refuses if either differs or the read fails. It also
refuses when the caller, or any ancestor, is a process Subfleet launched (a
guardian's descendant, or one carrying the C-5.1 markers), and requires the
installed app's executable (or, on a development state root, the development
build the daemon was started with, D-21).

A controlling terminal is not a person (D-PB1, 2026-10-11). The vetoes read
only the caller's ancestry as it is now: a process an agent started can drop
its markers, hold or open a pseudo-terminal, and outlive its parent, and once
launchd adopts it nothing above it is Subfleet's. Refusing only a caller whose
parent is launchd would not close that, because the orphan can fork a child
with a terminal of its own. An SSH login has a terminal and no markers too.
So the positive test is what the caller's image is, and a terminal is never
enough.

The kernel looks up the image through the saved token
(`proc_pidpath_audittoken`): an exec after that token was saved, an exited
process or a reused pid is refused. A missing token or a failed or unavailable
lookup is a refusal. The kernel's canonical path is compared verbatim with the
configured executables and their real paths, frozen when the daemon starts;
the caller's path is never resolved again. An update that moves the running
app bundle aside requires the app to be restarted: the token identifies the
process/image instance, not whether its moved path was an allowed app.

A same-user process that replaces its image with the app's executable before
the daemon reads its token can still be taken for the app, including for
request bytes written before exec. The kernel describes the process as it is
now; this image check cannot prove which program wrote those bytes. It
therefore separates the app from Subfleet's own launched processes only as
far as those processes do not replace their image with the app before the
first token read. The follow-up, an app-held credential that proves each
person-only request, is what closes this remaining case.

The chain is read with `ps`, which a loaded machine can slow. Each `ps` gets
`PS_TIMEOUT_S` and one more try when it does not answer, cannot start, or (the
process table) lists nothing, and the whole chain `CHAIN_BUDGET_S`. Past either
the chain is unreadable. A caller whose image is not the app is refused all the
same, since the vetoes could only refuse it too. The app itself is neither
accepted nor refused: the request fails closed with the cause named (the
service's `person-check-failed`, exit 1), nothing is done, and asking again is
safe. An ancestor that was not read is never taken to be free of the markers.
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

MARKERS = ("SUBFLEET_ATTEMPT=", "SUBFLEET_JOB=")
APP_EXECUTABLES = ("/Applications/Subfleet.app/Contents/MacOS/Subfleet",)
#: `ps -o tty` for a process with no controlling terminal.
NO_TERMINAL = ("??", "-", "")
PS = "/bin/ps"
#: Each `ps` answers within this, or is tried once more.
PS_TIMEOUT_S = 5.0
#: The whole chain is read within this, however long it is: well inside the
#: 15 s the app waits for a person-only op's answer (`DaemonOperation.timeout`),
#: so the app hears the cause instead of timing out, and no longer than this
#: is a request thread held by the check.
CHAIN_BUDGET_S = 10.0
#: The pause before a second try, for a `ps` that could not start (EAGAIN).
RETRY_PAUSE_S = 0.1
_PS_ENV = {"LC_ALL": "C", "LANG": "C", "PATH": "/usr/bin:/bin"}


class ChainUnreadable(Exception):
    """The caller's process chain could not be read in time (C-25.6)."""


@dataclass(frozen=True)
class AuditToken:
    """The saved audit_token_t; its socket lets person checks re-read the identity."""
    value: bytes
    conn: socket.socket | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if len(self.value) != 32:
            raise ValueError("an audit token must have eight uint32 words")

    @property
    def pid(self) -> int:
        return struct.unpack("=8I", self.value)[5]

    @property
    def pidversion(self) -> int:
        return struct.unpack("=8I", self.value)[7]


class _AuditToken(ctypes.Structure):
    _fields_ = [("val", ctypes.c_uint32 * 8)]


def allowed_executable_paths(executables: tuple[str, ...]) -> frozenset[str]:
    """Freeze configured spellings and their canonical paths at daemon startup."""
    return frozenset((*executables, *(os.path.realpath(p) for p in executables)))


DEFAULT_ALLOWED_PATHS = allowed_executable_paths(APP_EXECUTABLES)


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    tty: str              # read for the refusal's wording only; it never admits (D-PB1)
    command: str          # full command line, with `ps -E` environment appended when read


@dataclass(frozen=True)
class Verdict:
    person: bool
    reason: str
    pid: int | None
    unreadable: bool = False      # the app's chain could not be read: the check failed, not the caller


def peer_token(conn: socket.socket) -> AuditToken | None:
    try:
        return AuditToken(conn.getsockopt(0, 6, 32), conn)   # SOL_LOCAL, LOCAL_PEERTOKEN
    except (OSError, ValueError):
        return None


def peer_pid(conn: socket.socket) -> int | None:
    token = peer_token(conn)
    return token.pid if token else None


def _seconds(value: float) -> str:
    return f"{value:.1f}".removesuffix(".0")


class PsReader:
    """`ps` for one chain read: each call is tried at most twice, and every try
    ends by one deadline, `budget_s` from when the reader is made."""

    def __init__(self, budget_s: float | None = None, *, run: Callable = subprocess.run,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.budget_s = CHAIN_BUDGET_S if budget_s is None else budget_s
        self.run, self.clock, self.sleep = run, clock, sleep
        self.deadline = clock() + self.budget_s

    def __call__(self, args: list[str], *, rows_required: bool = False) -> str:
        """`ps <args>`'s output. `rows_required`: output with no rows is a failed
        try (the process table is never empty). Raises `ChainUnreadable`."""
        shown = "`ps " + " ".join(args) + "`"
        failures: list[str] = []
        for attempt in range(2):
            if attempt:
                self.sleep(max(0.0, min(RETRY_PAUSE_S, self.deadline - self.clock())))
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                break
            timeout = min(PS_TIMEOUT_S, remaining)
            try:
                done = self.run([PS, *args], capture_output=True, text=True, timeout=timeout, env=_PS_ENV,
                                check=False)
            except subprocess.TimeoutExpired:
                failures.append(f"did not answer within {_seconds(timeout)} s")
                continue
            except OSError as exc:
                failures.append(f"could not start ({exc.strerror or exc})")
                continue
            if rows_required and not (done.stdout or "").strip():
                failures.append(f"listed no processes (exit {done.returncode})")
                continue
            return done.stdout or ""
        budget = f"the check's {_seconds(self.budget_s)} s"
        if not failures:
            raise ChainUnreadable(f"{budget} ran out before {shown} could run")
        if len(failures) == 1:
            raise ChainUnreadable(f"{shown} {failures[0]}, and {budget} ran out before a second try")
        if failures[0] == failures[1]:
            raise ChainUnreadable(f"{shown} {failures[0]}, twice")
        raise ChainUnreadable(f"{shown} {failures[0]}, then {failures[1]}")


def executable_path(pid: int) -> str | None:
    """The caller's executable as the kernel records it (`proc_pidpath`), which
    keeps a path with spaces whole; `ps -o command` cannot be split to recover
    one (`/Applications/Subfleet Dev.app/...` reads as `.../Subfleet`)."""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        buffer = ctypes.create_string_buffer(4096)          # PROC_PIDPATHINFO_MAXSIZE
        length = libproc.proc_pidpath(int(pid), buffer, ctypes.c_uint32(4096))
    except (OSError, AttributeError, ValueError):
        return None
    return buffer.value.decode("utf-8", "replace") if length > 0 else None


def executable_path_for_token(token: AuditToken) -> str | None:
    """Read only this process/image instance; never fall back to a bare pid."""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lookup = libproc.proc_pidpath_audittoken
        lookup.argtypes = (ctypes.POINTER(_AuditToken), ctypes.c_void_p, ctypes.c_uint32)
        lookup.restype = ctypes.c_int
        audit = _AuditToken.from_buffer_copy(token.value)
        buffer = ctypes.create_string_buffer(4096)     # PROC_PIDPATHINFO_MAXSIZE
        length = lookup(ctypes.byref(audit), buffer, len(buffer))
    except (OSError, AttributeError, ValueError):
        return None
    return buffer.value.decode("utf-8", "replace") if length > 0 else None


def process_chain(pid: int, *, read: Callable[..., str] | None = None) -> list[Proc]:
    """The caller and its ancestors, each with its environment (read in memory,
    never stored). Raises `ChainUnreadable` when `ps` cannot answer in time:
    part of a chain says nothing about the ancestors not read."""
    read = read or PsReader()
    table: dict[int, tuple[int, str]] = {}
    for line in read(["-axo", "pid=,ppid=,tty="], rows_required=True).splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2])
    chain: list[Proc] = []
    seen: set[int] = set()
    current = pid
    while current and current not in seen and current in table and len(chain) < 64:
        seen.add(current)
        ppid, tty = table[current]
        command = read(["-Ewwp", str(current), "-o", "command="]).strip()
        chain.append(Proc(current, ppid, tty, command))
        if current == 1:
            break
        current = ppid
    return chain


def judge(peer: AuditToken | None, *, chain: Callable[[int], list[Proc]] = process_chain,
          allowed_paths: frozenset[str] = DEFAULT_ALLOWED_PATHS, root: str | None = None,
          executable: Callable[[AuditToken], str | None] = executable_path_for_token) -> Verdict:
    if not isinstance(peer, AuditToken) or peer.pid <= 0:
        return Verdict(False, "the caller's connection-time audit token could not be identified; "
                              "reconnect from the Subfleet app", None)
    pid = peer.pid
    # _person calls judge only after the full request has been read, before
    # acting. Compare the process/image version, not mutable credential words.
    if peer.conn is not None:
        current = peer_token(peer.conn)
        if current is None:
            return Verdict(False, "the caller's audit token could not be re-read after the request; "
                                  "reconnect from the Subfleet app", pid)
        if (current.pid, current.pidversion) != (peer.pid, peer.pidversion):
            return Verdict(False, "the caller's process/image changed between accept and the person-only request "
                                  "(audit token pid or pid version differs); "
                                  "reconnect from the Subfleet app", pid)
    try:
        procs = chain(pid)
    except ChainUnreadable as exc:
        # The vetoes could only refuse, so a caller that is not the app is
        # refused whatever its chain holds; only the app's own request waits on it.
        path = executable(peer)
        if not path:
            return _unverified_image(pid)
        if path not in allowed_paths:
            return _not_the_app(pid)
        return Verdict(False, f"the caller's process chain could not be read: {exc}", pid, unreadable=True)
    if not procs:
        return Verdict(False, "the caller's process could not be inspected", pid)
    root_marker = f"SUBFLEET_ROOT={root}" if root else None
    for proc in procs:
        if "subfleet.guardian" in proc.command:
            return Verdict(False, "the caller runs under a Subfleet guardian", pid)
        if any(marker in proc.command for marker in MARKERS) or (root_marker and root_marker in proc.command):
            return Verdict(False, "the caller carries Subfleet's attempt markers", pid)
    caller = procs[0]
    path = executable(peer)
    if not path:
        return _unverified_image(pid)
    if path in allowed_paths:
        return Verdict(True, "the Subfleet app", pid)
    if caller.tty not in NO_TERMINAL:
        return Verdict(False, f"this needs the Subfleet app; a terminal ({caller.tty}) is not enough, "
                              "because a process an agent started can hold one; "
                              "if an update moved the running app bundle, restart the app", pid)
    return _not_the_app(pid)


def _unverified_image(pid: int) -> Verdict:
    return Verdict(False, "the caller's connection-time image could not be verified through its audit token "
                          "(lookup failed or unavailable; process may have exec'd, exited or its pid been reused); "
                          "reconnect from the Subfleet app", pid)


def _not_the_app(pid: int) -> Verdict:
    return Verdict(False, "this needs the Subfleet app; if an update moved its running bundle, restart the app", pid)
