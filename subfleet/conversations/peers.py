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
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct
from dataclasses import dataclass, field
from typing import Callable

from .. import supervisor

MARKERS = ("SUBFLEET_ATTEMPT=", "SUBFLEET_JOB=")
APP_EXECUTABLES = ("/Applications/Subfleet.app/Contents/MacOS/Subfleet",)
#: `ps -o tty` for a process with no controlling terminal.
NO_TERMINAL = ("??", "-", "")


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


def peer_token(conn: socket.socket) -> AuditToken | None:
    try:
        return AuditToken(conn.getsockopt(0, 6, 32), conn)   # SOL_LOCAL, LOCAL_PEERTOKEN
    except (OSError, ValueError):
        return None


def peer_pid(conn: socket.socket) -> int | None:
    token = peer_token(conn)
    return token.pid if token else None


def _ps(argv: list[str]) -> str:
    env = {"LC_ALL": "C", "LANG": "C", "PATH": "/usr/bin:/bin"}
    # Under a file op (a create's person check), supervised like every process it starts (C-25.3).
    return supervisor.run(argv, text=True, timeout=5, env=env).stdout


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


def process_chain(pid: int) -> list[Proc]:
    """The caller and its ancestors, each with its environment (read in memory,
    never stored)."""
    table: dict[int, tuple[int, str]] = {}
    for line in _ps(["/bin/ps", "-axo", "pid=,ppid=,tty="]).splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2])
    chain: list[Proc] = []
    seen: set[int] = set()
    current = pid
    while current and current not in seen and current in table and len(chain) < 64:
        seen.add(current)
        ppid, tty = table[current]
        command = _ps(["/bin/ps", "-Ewwp", str(current), "-o", "command="]).strip()
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
    procs = chain(pid)
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
        return Verdict(False, "the caller's connection-time image could not be verified through its audit token "
                              "(lookup failed or unavailable; process may have exec'd, exited or its pid been reused); "
                              "reconnect from the Subfleet app", pid)
    if path in allowed_paths:
        return Verdict(True, "the Subfleet app", pid)
    if caller.tty not in NO_TERMINAL:
        return Verdict(False, f"this needs the Subfleet app; a terminal ({caller.tty}) is not enough, "
                              "because a process an agent started can hold one; "
                              "if an update moved the running app bundle, restart the app", pid)
    return Verdict(False, "this needs the Subfleet app; if an update moved its running bundle, restart the app", pid)
