"""Retention's scheduling: below the operator's apps (final review of e50716e8, N9).

The daemon runs at the default QoS (launchd `ProcessType` `Interactive`,
docs/reports/2026-09-27-daemon-qos.md), and a thread's QoS reaches no child, so
retention's `lsof` listings, its git commands and the readers it keeps open
ran at the operator's priority, with sustained I/O during a backlog. Each child
now starts under a `taskpolicy -c <clamp>` (the guardian's mechanism, C-5.1),
which it and its own children stay under; the in-process part of a retirement
(clones, reads for hashing, unlinks) runs with its thread's disk I/O policy
lowered to match (`setiopolicy_np`, thread scope), restored afterwards. The
thread's CPU QoS is left alone: it takes the store and the interpreter's lock,
and a low-QoS holder of either stalls the whole daemon (the 2026-09-27 report).

The clamp is `utility` by default, the level agent work already runs at. Measured
2026-09-30 at load 55 to 80, a whole-machine `lsof` took 1.5 and 3.9 s under
`utility` and 47 and 8 s under `background`, and a cached `git rev-list` 0.14 s
against 9 and 1.3 s: `background` yields to every agent, so on this machine
retention would wait behind them indefinitely. `SUBFLEET_RETENTION_QOS` picks
`background` or `maintenance` instead, or `inherit` for no clamp.
"""
from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from .guardian import TASKPOLICY

QOS_ENV = "SUBFLEET_RETENTION_QOS"
DEFAULT_QOS = "utility"
CLAMPS = ("utility", "background", "maintenance")

# <sys/resource.h>
IOPOL_TYPE_DISK = 0
IOPOL_SCOPE_THREAD = 1
IOPOL_THROTTLE = 3
IOPOL_UTILITY = 4
#: The disk I/O policy that goes with each clamp.
IO_POLICY = {"utility": IOPOL_UTILITY, "background": IOPOL_THROTTLE, "maintenance": IOPOL_THROTTLE}

_libc = ctypes.CDLL(None, use_errno=True)
_setiopolicy = getattr(_libc, "setiopolicy_np", None)
_getiopolicy = getattr(_libc, "getiopolicy_np", None)
if _setiopolicy is not None and _getiopolicy is not None:
    _setiopolicy.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    _setiopolicy.restype = ctypes.c_int
    _getiopolicy.argtypes = [ctypes.c_int, ctypes.c_int]
    _getiopolicy.restype = ctypes.c_int
_local = threading.local()


def qos() -> str | None:
    """The clamp retention's children start under, or None (`inherit`, an
    unknown value's fallback is the default, and a host without taskpolicy(8)
    inherits: that is not macOS)."""
    value = os.environ.get(QOS_ENV, DEFAULT_QOS).strip().lower()
    if value == "inherit" or not os.access(TASKPOLICY, os.X_OK):
        return None
    return value if value in CLAMPS else DEFAULT_QOS


def argv(command: Sequence[str]) -> list[str]:
    """`command` as retention spawns it: under `taskpolicy -c <clamp>` when clamped."""
    clamp = qos()
    return [TASKPOLICY, "-c", clamp, *command] if clamp else list(command)


@contextmanager
def throttled_io() -> Iterator[None]:
    """Run the block with this thread's disk I/O policy lowered to the clamp's
    (`IO_POLICY`), then put back what it was. Nested uses keep the outer one."""
    clamp = qos()
    if clamp is None or _setiopolicy is None or _getiopolicy is None or getattr(_local, "depth", 0):
        _local.depth = getattr(_local, "depth", 0) + 1
        try:
            yield
        finally:
            _local.depth -= 1
        return
    before = _getiopolicy(IOPOL_TYPE_DISK, IOPOL_SCOPE_THREAD)
    lowered = before >= 0 and _setiopolicy(IOPOL_TYPE_DISK, IOPOL_SCOPE_THREAD, IO_POLICY[clamp]) == 0
    _local.depth = 1
    try:
        yield
    finally:
        _local.depth = 0
        if lowered:
            _setiopolicy(IOPOL_TYPE_DISK, IOPOL_SCOPE_THREAD, before)


def thread_io_policy() -> int | None:
    """This thread's disk I/O policy (for tests), or None where it cannot be read."""
    if _getiopolicy is None:
        return None
    value = _getiopolicy(IOPOL_TYPE_DISK, IOPOL_SCOPE_THREAD)
    return value if value >= 0 else None
