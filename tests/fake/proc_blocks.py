"""Read-only views of processes a test started itself, for the credit key checks (C-31.17).

macOS only, through libproc and `sysctl`, as `ps` reads them; nothing here starts a
process or signals one. Callers pass pids they started (or descendants of those), so no
other process on the host is read: `children` asks the kernel for one parent's children
(`proc_listpids(PROC_PPID_ONLY)`), never for the whole table.

- `block(pid)`: the raw `KERN_PROCARGS2` buffer, the argv and the environment the kernel
  copied at the process's exec. Any process of the same user can read it, but macOS 26
  leaves the environment out for platform binaries (`environment`).
- `descendants(pid)`: every live process below `pid`, breadth first.
- `descriptors(pid)`: each open descriptor and its kind (`PIPE` for a pipe).
- `leaks(data, secret)`: how many `RUN`-character runs of `secret` appear in `data`.

Standard library only, so a copy runs against an older tree (the base runs of these
checks in docs/reports/credit-lane-integration-report.md).
"""
from __future__ import annotations

import ctypes
import errno
import os
import struct

RUN = 12
#: sys/proc_info.h
_PROC_PPID_ONLY = 6
_PROC_PIDLISTFDS = 1
PIPE = 6                       # PROX_FDTYPE_PIPE
_CTL_KERN, _KERN_PROCARGS2 = 1, 49

_libc = ctypes.CDLL(None, use_errno=True)
_libc.proc_listpids.restype = ctypes.c_int
_libc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
_libc.proc_pidinfo.restype = ctypes.c_int
_libc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
_libc.sysctl.restype = ctypes.c_int
_libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]


def block(pid: int) -> bytes | None:
    """`pid`'s exec argv and environment block, or None once it is gone."""
    size = ctypes.c_size_t(os.sysconf("SC_ARG_MAX"))
    buffer = ctypes.create_string_buffer(size.value)
    name = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    if _libc.sysctl(name, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    return buffer.raw[:size.value]


def strings(data: bytes) -> list[bytes]:
    """The NUL-separated strings of a block, empty ones dropped: the exec path, argv,
    then the environment."""
    return [part for part in data[4:].split(b"\0") if part]


def argv(data: bytes) -> list[bytes]:
    """The argv a block holds (its first word, `argc`, says how many strings)."""
    count = struct.unpack_from("=i", data)[0]
    return strings(data)[1:1 + count]


def environment(data: bytes) -> list[bytes]:
    """The environment a block holds. macOS 26 leaves it out of a platform binary's block
    (`/bin/sh`, `/bin/bash`, `/bin/sleep`): `KERN_PROCARGS2` then returns argv alone, to
    every reader, so a check reads such a process's environment some other way."""
    count = struct.unpack_from("=i", data)[0]
    return strings(data)[1 + count:]


def children(pid: int) -> list[int]:
    for _ in range(3):
        needed = _libc.proc_listpids(_PROC_PPID_ONLY, pid, None, 0)
        if needed <= 0:
            return []
        listed = (ctypes.c_int * (needed // ctypes.sizeof(ctypes.c_int) + 64))()
        got = _libc.proc_listpids(_PROC_PPID_ONLY, pid, listed, ctypes.sizeof(listed))
        if got < ctypes.sizeof(listed):
            return [child for child in listed[:max(got, 0) // ctypes.sizeof(ctypes.c_int)] if child > 0]
    raise OSError(errno.EOVERFLOW, f"the children of pid {pid} kept growing")


def descendants(pid: int) -> list[int]:
    found, pending = [], [pid]
    while pending:
        for child in children(pending.pop(0)):
            if child not in found:
                found.append(child)
                pending.append(child)
    return found


def descriptors(pid: int) -> list[tuple[int, int]]:
    """(descriptor, kind) for each descriptor `pid` holds; [] once it is gone."""
    needed = _libc.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, None, 0)
    if needed <= 0:
        return []
    buffer = ctypes.create_string_buffer(needed + 64 * 8)
    got = _libc.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, buffer, len(buffer))
    return [(fd, kind) for fd, kind in struct.iter_unpack("=iI", buffer.raw[:max(got, 0) // 8 * 8])]


def runs(secret: str | bytes) -> list[bytes]:
    data = secret.encode() if isinstance(secret, str) else secret
    return [data[index:index + RUN] for index in range(len(data) - RUN + 1)]


def leaks(data: bytes, secret: str | bytes) -> int:
    """How many `RUN`-character runs of `secret` are in `data` (never the runs themselves)."""
    return sum(run in data for run in runs(secret))
