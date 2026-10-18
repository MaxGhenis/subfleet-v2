"""Which processes still hold a retiring job's trees (design 5.4).

One ``lsof`` listing of every process serves a whole batch of jobs, so a pass
pays for two listings however many jobs it retires (review of ccc85387, finding
4: a listing took 46 s to 6 min under load). A process holds a tree if it has
its current or root directory in it, a text or memory mapping of a file in it,
any descriptor on a directory in it, or a descriptor open for writing on a file
in it. "In it" is a path prefix, or at the second check also one of the
(device, inode) pairs the archive recorded. A read-only descriptor on a file is
not a hold: it cannot change the file, and its reader keeps the unlinked inode.

The check fails closed: if ``lsof`` is missing, fails or times out, every job
in the batch is busy. Processes of other users are invisible to a non-root
``lsof``; job worktrees are mode 0700. A descriptor passed over a Unix socket
and not held by any process at the moment of the listing is invisible too;
Max's d635 ruling accepts both (documented, not engineered away).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from . import retention_qos as rqos

LSOF = "/usr/sbin/lsof"
MAPPED = {"txt", "mem", "mmap", "ltx", "mxx", "m86", "tr"}


class ScanFailed(Exception):
    pass


@dataclass
class Watch:
    """One job's watched paths (prefixes) and, at the second check, inodes."""
    prefixes: list[str]
    inodes: set[tuple[int, int]] = field(default_factory=set)


Holders = Callable[[Mapping[str, Watch]], dict[str, list[str]]]


def _under(name: str, prefixes: Iterable[str]) -> bool:
    return any(name == p or name.startswith(p.rstrip("/") + "/") for p in prefixes)


def parse(output: str, watches: Mapping[str, Watch], own_pid: int | None = None) -> dict[str, list[str]]:
    """Busy reasons per job from ``lsof -F pcftaDin`` output."""
    busy: dict[str, list[str]] = {}
    pid = command = None
    record: dict[str, str] = {}

    def settle() -> None:
        if not record or pid is None:
            return
        fd = record.get("f", "")
        name = record.get("n", "")
        kind = record.get("t", "")
        access = record.get("a", "").strip()
        try:
            device = int(record.get("D", ""), 16) if record.get("D") else None
            inode = int(record["i"]) if record.get("i", "").isdigit() else None
        except ValueError:
            device = inode = None
        for job, watch in watches.items():
            by_path = bool(name) and _under(name, watch.prefixes)
            by_inode = device is not None and inode is not None and (device, inode) in watch.inodes
            if not (by_path or by_inode):
                continue
            why = None
            if fd in ("cwd", "rtd"):
                why = fd
            elif fd in MAPPED:
                why = "mapped"
            elif kind == "DIR":
                why = "directory descriptor"
            elif access in ("w", "u"):
                why = "open for writing"
            if why:
                who = f"pid {pid} ({command or '?'}): {why} {name}"
                busy.setdefault(job, []).append(who + (" (our own process)" if own_pid == pid else ""))

    for line in output.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            settle()
            record = {}
            try:
                pid = int(value)
            except ValueError:
                pid = None
            command = None
        elif tag == "c":
            command = value
        elif tag == "f":
            settle()
            record = {"f": value}
        elif tag in "atDin":
            record[tag] = value
    settle()
    return busy


def lsof_holders(watches: Mapping[str, Watch], *, timeout: float = 900,
                 cancel: threading.Event | None = None) -> dict[str, list[str]]:
    """Run one listing and report the busy jobs; raise `ScanFailed` when it cannot."""
    if not watches:
        return {}
    binary = LSOF if os.access(LSOF, os.X_OK) else shutil.which("lsof")
    if not binary:
        raise ScanFailed("lsof is not installed")
    try:
        process = subprocess.Popen(rqos.argv([binary, "-n", "-P", "-w", "-F", "pcftaDin"]), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise ScanFailed(f"lsof could not start: {exc}") from exc
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = process.communicate(timeout=max(0.05, min(1.0, deadline - time.monotonic())))
            break
        except subprocess.TimeoutExpired:
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                process.kill()
                process.communicate()
                raise ScanFailed("lsof cancelled" if cancel is not None and cancel.is_set()
                                 else f"lsof did not finish in {timeout:g} s") from None
    if process.returncode != 0 or not out:
        raise ScanFailed(f"lsof exited {process.returncode}: {err.decode('utf-8', 'replace').strip()[-300:]}")
    return parse(out.decode("utf-8", "surrogateescape"), watches, own_pid=os.getpid())
