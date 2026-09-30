"""Person-only operations (C-25.6, design D-8).

Approving a tool call, resolving an ambiguous delivery, widening a permission
policy, setting `allow_main` and unblocking an unfinished turn are a person's
decisions. The daemon reads the caller's pid from the socket
(`LOCAL_PEERPID`) and refuses when the caller, or any ancestor, is a process
Subfleet launched (a guardian's descendant, or one carrying the C-5.1
markers), and accepts only the installed app's executable or a process with a
controlling terminal. Any other process of the same user is outside this
check, as it is outside `daemon.sock`'s: it is a boundary against the agents
Subfleet runs and headless agents, not against the user.

The chain is read with `ps`, which a loaded machine can slow. Each `ps` gets
`PS_TIMEOUT_S` and one more try if it does not answer or cannot start, and the
whole chain `CHAIN_BUDGET_S`. Past either the chain is unreadable, and the
caller is neither accepted nor refused: the request fails closed with the
cause named (the service's `person-check-failed`, exit 1), nothing is done,
and asking again is safe. An ancestor that was not read is never taken to be
free of the markers.
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

MARKERS = ("SUBFLEET_ATTEMPT=", "SUBFLEET_JOB=")
APP_EXECUTABLES = ("/Applications/Subfleet.app/Contents/MacOS/Subfleet",)
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
class Proc:
    pid: int
    ppid: int
    tty: str
    command: str          # full command line, with `ps -E` environment appended when read


@dataclass(frozen=True)
class Verdict:
    person: bool
    reason: str
    pid: int | None
    unreadable: bool = False      # the chain could not be read: the check failed, not the caller


def peer_pid(conn: socket.socket) -> int | None:
    try:
        return conn.getsockopt(0, 2)          # SOL_LOCAL, LOCAL_PEERPID (macOS)
    except OSError:
        return None


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


def judge(pid: int | None, *, chain: Callable[[int], list[Proc]] = process_chain,
          app_executables: tuple[str, ...] = APP_EXECUTABLES, root: str | None = None,
          executable: Callable[[int], str | None] = executable_path) -> Verdict:
    if pid is None:
        return Verdict(False, "the caller's process could not be identified", None)
    try:
        procs = chain(pid)
    except ChainUnreadable as exc:
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
    path = executable(caller.pid)
    allowed = {os.path.realpath(p) for p in app_executables}
    if path and (path in app_executables or os.path.realpath(path) in allowed):
        return Verdict(True, "the Subfleet app", pid)
    if caller.tty not in ("??", "-", ""):
        return Verdict(True, f"a terminal ({caller.tty})", pid)
    return Verdict(False, "only the Subfleet app or a terminal may do this", pid)
