"""Person-only operations (C-25.6, design D-8).

Approving a tool call, resolving an ambiguous delivery, widening a permission
policy, setting `allow_main` and unblocking an unfinished turn are a person's
decisions. The daemon reads the caller's pid from the socket
(`LOCAL_PEERPID`) and refuses when the caller, or any ancestor, is a process
Subfleet launched (a guardian's descendant, or one carrying the C-5.1
markers), and accepts the installed app's executable, a process with a
controlling terminal, or a child of the exact owner-chat gateway (C-31.1).
Any other process of the same user is outside this
check, as it is outside `daemon.sock`'s: it is a boundary against the agents
Subfleet runs and headless agents, not against the user.
"""

from __future__ import annotations

import ctypes
import os
import shlex
import socket
import subprocess
from dataclasses import dataclass
from typing import Callable

MARKERS = ("SUBFLEET_ATTEMPT=", "SUBFLEET_JOB=")
APP_EXECUTABLES = ("/Applications/Subfleet.app/Contents/MacOS/Subfleet",)


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


def peer_pid(conn: socket.socket) -> int | None:
    try:
        return conn.getsockopt(0, 2)          # SOL_LOCAL, LOCAL_PEERPID (macOS)
    except OSError:
        return None


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


def judge(pid: int | None, *, chain: Callable[[int], list[Proc]] = process_chain,
          app_executables: tuple[str, ...] = APP_EXECUTABLES, root: str | None = None,
          executable: Callable[[int], str | None] = executable_path,
          gateway_script: str | None = None) -> Verdict:
    if pid is None:
        return Verdict(False, "the caller's process could not be identified", None)
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
    # C-31.1: the one owner-filtering gateway is a person surface even under
    # launchd. Check its actual parent position and script argument, never a
    # substring or a client-supplied flag. The agent ancestry refusals above
    # still apply. This is the same-user boundary, not a sandbox against Max.
    if gateway_script and len(procs) > 1 and procs[1].pid == caller.ppid:
        parent = procs[1]
        try:
            # ps -E appends unquoted environment values. Only parse the two
            # argv fields needed here; a quote in a later value is not part of
            # the interpreter or script name and must not reject the gateway.
            lexer = shlex.shlex(parent.command, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            argv = [lexer.get_token(), lexer.get_token()]
        except ValueError:
            argv = []
        parent_executable = executable(parent.pid)
        if (len(argv) > 1 and all(argv) and os.path.basename(argv[0]).lower().startswith("python")
                and parent_executable and os.path.basename(parent_executable).lower().startswith("python")
                and os.path.isabs(argv[1]) and os.path.realpath(argv[1]) == os.path.realpath(gateway_script)):
            return Verdict(True, "the owner-filtering Telegram gateway", pid)
    path = executable(caller.pid)
    allowed = {os.path.realpath(p) for p in app_executables}
    if path and (path in app_executables or os.path.realpath(path) in allowed):
        return Verdict(True, "the Subfleet app", pid)
    if caller.tty not in ("??", "-", ""):
        return Verdict(True, f"a terminal ({caller.tty})", pid)
    return Verdict(False, "only the Subfleet app or a terminal may do this", pid)
