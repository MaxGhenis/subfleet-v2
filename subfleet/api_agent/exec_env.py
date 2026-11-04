"""Overwrite secrets in the environment block the kernel copied at exec (C-31.12).

`os.environ.pop` only unsets libc's pointer to a variable. The bytes stay in the
block the kernel wrote at exec, and any process of the same user reads that block
with `sysctl(KERN_PROCARGS2)`, as `ps -E` does. A tool command inside the shell's
seatbelt sandbox can too: seatbelt does not mediate that sysctl (the build tried
`sysctl-read` and `process-info*` rules against it; see
`docs/reports/credit-lane-harness-report.md`). So before the key leaves
`os.environ`, `overwrite` writes `*` over the value of every entry a predicate
names. Each string keeps its length, so the strings after it stay where a reader
of the block, Subfleet's own census among them (C-5.5), finds them.

Standard library only: `__main__` runs this before anything else is imported.
"""
from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable

FILLER = ord("*")


def _environ_array():
    """libc's current `char **environ`, as a pointer to addresses, or None."""
    try:
        libc = ctypes.CDLL(None)
    except OSError:
        return None
    if sys.platform == "darwin":
        try:
            get_environ = libc._NSGetEnviron
        except AttributeError:
            return None
        get_environ.restype = ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        get_environ.argtypes = []
        holder = get_environ()
        return holder.contents if holder else None
    try:
        return ctypes.POINTER(ctypes.c_void_p).in_dll(libc, "environ")
    except ValueError:
        return None


def overwrite(secret: Callable[[str], bool]) -> int:
    """Write `*` over the value of each environment entry whose name `secret`
    accepts; return how many entries it overwrote. Never raises."""
    array = _environ_array()
    if not array:
        return 0
    count = 0
    index = 0
    while True:
        address = array[index]
        if not address:
            return count
        index += 1
        entry = ctypes.string_at(address)
        name, equals, value = entry.partition(b"=")
        if not equals or not value:
            continue
        try:
            decoded = name.decode("ascii")
        except UnicodeDecodeError:
            decoded = name.decode("utf-8", "surrogateescape")
        if secret(decoded):
            ctypes.memset(address + len(name) + 1, FILLER, len(value))
            count += 1
