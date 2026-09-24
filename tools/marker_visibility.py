#!/usr/bin/env python3
"""Which executables the C-5.5 marker can see: live CS_RESTRICT and `ps -E` visibility, per executable.

Each executable is spawned suspended (posix_spawn with POSIX_SPAWN_START_SUSPENDED,
so none of it runs) with `SUBFLEET_ATTEMPT=<probe>` in its environment. The tool
reads the process's live code-signing flags with csops(CS_OPS_STATUS), asks
`ps -Eww` whether the marker is in its environment, then SIGKILLs and reaps it.
xnu has already committed the flags when posix_spawn returns (process_signature
runs before the task is resumed), and ps reads KERN_PROCARGS2, whose environment
xnu's sysctl_procargsx withholds for a CS_RESTRICT process (C-5.5).

A script is measured as the interpreter its #! line names, before any exec: a
`#!/usr/bin/env python3` script takes python's flags once env has exec'd it, and
an xcode-select shim in /usr/bin (git, python3, make, clang) takes the flags of
the Xcode tool it execs in the same pid, so shims are measured through
`xcrun -f <tool>` as well.

    uv run python tools/marker_visibility.py                  # the tools providers and git run
    uv run python tools/marker_visibility.py --dir /bin       # every executable in a directory

Sweeping a system directory spawns launch-constrained programs, which the kernel
kills at exec and which leave crash reports in ~/Library/Logs/DiagnosticReports;
/bin has none. setuid and setgid files are skipped.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

POSIX_SPAWN_START_SUSPENDED = 0x0080
CS_OPS_STATUS = 0
CS_RESTRICT = 0x00000800
CS_PLATFORM_BINARY = 0x04000000
MARKER = f"SUBFLEET_ATTEMPT=marker-visibility:{os.getpid()}"

# What a Claude Code or Codex attempt, and git, run most (C-5.5).
COMMON = ["/bin/sh", "/bin/bash", "/bin/zsh", "/bin/sleep", "/bin/cat", "/bin/ls", "/bin/cp", "/bin/mv",
          "/bin/rm", "/bin/mkdir", "/usr/bin/env", "/usr/bin/tail", "/usr/bin/head", "/usr/bin/grep",
          "/usr/bin/sed", "/usr/bin/awk", "/usr/bin/find", "/usr/bin/xargs", "/usr/bin/tee", "/usr/bin/sort",
          "/usr/bin/perl", "/usr/bin/ruby", "/usr/bin/ssh", "/usr/bin/curl", "/usr/bin/rsync", "/usr/bin/tar",
          "/usr/bin/nohup", "/usr/bin/caffeinate", "/usr/bin/osascript", "/usr/bin/sandbox-exec",
          "/usr/bin/git", "/usr/bin/python3", "/usr/bin/make", "/usr/bin/clang"]
INSTALLED = ["git", "node", "bun", "uv", "rg", "python3", "claude", "codex"]
SHIMS = ["git", "python3", "make", "clang"]

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def spawn_suspended(path: str) -> int:
    attr = ctypes.c_void_p()
    if libc.posix_spawnattr_init(ctypes.byref(attr)):
        raise OSError(ctypes.get_errno(), "posix_spawnattr_init")
    try:
        if libc.posix_spawnattr_setflags(ctypes.byref(attr), ctypes.c_short(POSIX_SPAWN_START_SUSPENDED)):
            raise OSError(ctypes.get_errno(), "posix_spawnattr_setflags")
        argv = (ctypes.c_char_p * 2)(path.encode(), None)
        env = [MARKER.encode(), b"PATH=/usr/bin:/bin"]
        envp = (ctypes.c_char_p * (len(env) + 1))(*env, None)
        pid = ctypes.c_int()
        error = libc.posix_spawn(ctypes.byref(pid), path.encode(), None, ctypes.byref(attr), argv, envp)
        if error:
            raise OSError(error, os.strerror(error), path)
        return pid.value
    finally:
        libc.posix_spawnattr_destroy(ctypes.byref(attr))


def flags(pid: int) -> int | None:
    value = ctypes.c_uint32()
    if libc.csops(pid, CS_OPS_STATUS, ctypes.byref(value), ctypes.sizeof(value)):
        return None
    return value.value


def marker_visible(pid: int) -> bool:
    shown = subprocess.run(["/bin/ps", "-Eww", "-o", "command=", "-p", str(pid)],
                           capture_output=True, text=True, check=False).stdout
    return MARKER in shown


def kind(path: str) -> str:
    try:
        with open(path, "rb") as stream:
            head = stream.read(4)
    except OSError:
        return "unreadable"
    if head[:2] == b"#!":
        return "script"
    if head in {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xce\xfa\xed\xfe"}:
        return "mach-o"
    return "other"


def measure(path: str) -> dict:
    row = {"path": path, "kind": kind(path)}
    info = os.stat(path)
    if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
        return {**row, "result": "skipped: setuid or setgid"}
    try:
        pid = spawn_suspended(path)
    except OSError as exc:
        return {**row, "result": f"not spawned: {exc.strerror or exc}"}
    try:
        value = flags(pid)
        visible = marker_visible(pid)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _, status = os.waitpid(pid, 0)
    if value is None:
        return {**row, "result": "killed at exec" if os.WIFSIGNALED(status) else "flags unreadable"}
    return {**row, "result": "measured", "restricted": bool(value & CS_RESTRICT),
            "platform": bool(value & CS_PLATFORM_BINARY), "marker_visible": visible}


def targets(args) -> list[str]:
    if args.dir:
        found = []
        for directory in args.dir:
            for entry in sorted(Path(directory).iterdir()):
                if entry.is_file() and os.access(entry, os.X_OK):
                    found.append(str(entry))
        return found
    found = [path for path in COMMON if os.path.exists(path)]
    for name in INSTALLED:
        for candidate in (shutil.which(name, path=os.environ.get("PATH", "")) or "",):
            if candidate:
                found.append(os.path.realpath(candidate))
    for name in SHIMS:
        tool = subprocess.run(["/usr/bin/xcrun", "-f", name], capture_output=True, text=True, check=False)
        if tool.returncode == 0 and tool.stdout.strip():
            found.append(tool.stdout.strip())
    return list(dict.fromkeys(found))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", action="append", help="measure every executable file in this directory")
    args = parser.parse_args()
    rows = [measure(path) for path in targets(args)]
    for row in rows:
        if row["result"] == "measured":
            state = (f"{'restricted' if row['restricted'] else 'not restricted'}, "
                     f"marker {'visible' if row['marker_visible'] else 'hidden'}")
            if row["restricted"] == row["marker_visible"]:
                state += "  <- restriction and visibility disagree"
        else:
            state = row["result"]
        print(f"{row['kind']:7} {row['path']}: {state}")
    measured = [row for row in rows if row["result"] == "measured"]
    restricted = sum(row["restricted"] for row in measured)
    agree = sum(row["restricted"] != row["marker_visible"] for row in measured)
    print(f"\n{len(rows)} executables, {len(measured)} measured: {restricted} restricted, "
          f"{len(measured) - restricted} not; marker hidden exactly when restricted in {agree} of {len(measured)}")
    return 0 if agree == len(measured) else 1


if __name__ == "__main__":
    sys.exit(main())
