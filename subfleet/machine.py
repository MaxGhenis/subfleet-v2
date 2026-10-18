"""How busy this machine is, for the machine guard (C-6.13).

Admission holds detached jobs of a class at the door while the machine is
saturated. The load came from what jobs run, not from the providers: on
2026-09-27, at a load average near 120 on 18 logical CPUs, Claude's processes
together used about 31% of one CPU, while one job's `bfs` search used 453% and
test suites and `uv` builds about 430%.

`read` costs two system calls and no subprocess, so a pass can afford it. Every
field it cannot read is None, and a guard never holds on a None.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import time
from typing import Any

_libc: Any = None


def _sysctl_int(name: str) -> int | None:
    """One integer sysctl by name (`sysctlbyname`), or None."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if _libc.sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, ctypes.c_size_t(0)):
            return None
        return int(value.value)
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def read() -> dict[str, Any]:
    """The load averages, the logical CPU count and macOS memory pressure.

    `memory_pressure` is `kern.memorystatus_vm_pressure_level` as the kernel
    reports it: 1 normal, 2 warn, 4 critical; None where there is no such sysctl.
    """
    try:
        load1, load5, _ = os.getloadavg()
    except OSError:
        load1 = load5 = None
    level = _sysctl_int("kern.memorystatus_vm_pressure_level")
    return {"load1": load1, "load5": load5, "cpus": os.cpu_count(),
            "memory_pressure": level if level in (1, 2, 4) else None,
            "observed_at": time.time()}
