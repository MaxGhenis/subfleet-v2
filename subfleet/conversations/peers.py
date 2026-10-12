"""Person-only operations (C-25.6, design D-8).

Approving a tool call, resolving an ambiguous delivery, widening a permission
policy, setting `allow_main` and unblocking an unfinished turn are a person's
decisions. At connection time the daemon retains the socket's 32-byte audit
token (`LOCAL_PEERTOKEN`), including its pid and pid version. It refuses when
the caller, or any ancestor, is a process Subfleet launched (a guardian's descendant, or one carrying the C-5.1
markers), and accepts only the installed app's executable (or, on a
development state root, the development build the daemon was started with,
D-21). Any other process of the same user is outside this check, as it is
outside `daemon.sock`'s: it is a boundary against the agents Subfleet runs and
headless agents, not against the user.

A controlling terminal is not a person (D-PB1, 2026-10-11). The vetoes read
only the caller's ancestry as it is now: a process an agent started can drop
its markers, hold or open a pseudo-terminal, and outlive its parent, and once
launchd adopts it nothing above it is Subfleet's. Refusing only a caller whose
parent is launchd would not close that, because the orphan can fork a child
with a terminal of its own. An SSH login has a terminal and no markers too.
So the positive test is what the caller's image is, and a terminal is never
enough.

The kernel looks up the image through the saved token
(`proc_pidpath_audittoken`), so an exec, an exited process or a reused pid
cannot give an earlier connection a new identity. A missing token or a failed
or unavailable lookup is a refusal. The kernel's canonical path is compared
verbatim with the configured executables and their real paths, frozen when
the daemon starts; the caller's path is never resolved again. An update that
moves the running app bundle aside requires the app to be restarted: the
token proves the process instance, not that its moved path was an allowed app.
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct
import subprocess
from dataclasses import dataclass
from typing import Callable

MARKERS = ("SUBFLEET_ATTEMPT=", "SUBFLEET_JOB=")
APP_EXECUTABLES = ("/Applications/Subfleet.app/Contents/MacOS/Subfleet",)
#: `ps -o tty` for a process with no controlling terminal.
NO_TERMINAL = ("??", "-", "")


@dataclass(frozen=True)
class AuditToken:
    """A socket's connection-time audit_token_t, including pid (5) and version (7)."""
    value: bytes

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
        return AuditToken(conn.getsockopt(0, 6, 32))   # SOL_LOCAL, LOCAL_PEERTOKEN
    except (OSError, ValueError):
        return None


def peer_pid(conn: socket.socket) -> int | None:
    token = peer_token(conn)
    return token.pid if token else None


def _ps(argv: list[str]) -> str:
    env = {"LC_ALL": "C", "LANG": "C", "PATH": "/usr/bin:/bin"}
    return subprocess.run(argv, capture_output=True, text=True, timeout=5, env=env, check=False).stdout


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
