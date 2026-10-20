"""One subprocess-free machine reading per detached admission pass (C-6.13).

CPU idle is a delta of Mach per-CPU ticks, with a 60 s time-constant EWMA.
Disk busy is the busiest block device's read + write service seconds per wall
second, bounded at 1; concurrent requests can overlap, so it is a saturation
proxy, not a physical utilization percentage. Missing signals are always None.
"""

from __future__ import annotations

import ctypes
import math
import os
import sys
import time
from collections.abc import Callable
from typing import Any

_libc: Any = None
_iokit: Any = None
CPU_EWMA_S = 60.0
Ticks = tuple[tuple[int, ...], ...]
DiskCounters = dict[int, tuple[int, int]]


def _system() -> Any:
    """Explicit system path: find_library can spawn a tool on other platforms."""
    global _libc
    if sys.platform != "darwin":
        raise OSError("Mach signals require macOS")
    if _libc is None:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        uint = ctypes.c_uint32
        lib.mach_host_self.argtypes = []
        lib.mach_host_self.restype = uint
        lib.host_processor_info.argtypes = [uint, ctypes.c_int, ctypes.POINTER(uint),
                                           ctypes.POINTER(ctypes.POINTER(uint)), ctypes.POINTER(uint)]
        lib.host_processor_info.restype = ctypes.c_int
        lib.vm_deallocate.argtypes = [uint, ctypes.c_size_t, ctypes.c_size_t]
        lib.vm_deallocate.restype = ctypes.c_int
        lib.mach_port_deallocate.argtypes = [uint, uint]
        lib.mach_port_deallocate.restype = ctypes.c_int
        lib.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        lib.sysctlbyname.restype = ctypes.c_int
        _libc = lib
    return _libc


def _sysctl_int(name: str) -> int | None:
    """One integer sysctl by name, or None."""
    try:
        lib = _system()
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if lib.sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0):
            return None
        return int(value.value) if size.value == ctypes.sizeof(value) else None
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def _cpu_ticks() -> Ticks | None:
    """PROCESSOR_CPU_LOAD_INFO: unsigned user/system/idle/nice ticks per CPU.

    host_processor_info's flavor is PROCESSOR_CPU_LOAD_INFO (2), the per-CPU
    equivalent of host_statistics' aggregate HOST_CPU_LOAD_INFO (3). Release
    both the returned VM allocation and the host send right on every path.
    """
    try:
        lib = _system()
        task = ctypes.c_uint32.in_dll(lib, "mach_task_self_").value
        host = lib.mach_host_self()
        data = ctypes.POINTER(ctypes.c_uint32)()
        cpus, count = ctypes.c_uint32(), ctypes.c_uint32()
        try:
            if lib.host_processor_info(host, 2, ctypes.byref(cpus), ctypes.byref(data), ctypes.byref(count)):
                return None
            if not data or not 0 < cpus.value <= 1024 or count.value != cpus.value * 4:
                return None
            return tuple(tuple(int(data[i * 4 + state]) for state in range(4)) for i in range(cpus.value))
        finally:
            if data:
                lib.vm_deallocate(task, ctypes.cast(data, ctypes.c_void_p).value,
                                  count.value * ctypes.sizeof(ctypes.c_uint32))
            lib.mach_port_deallocate(task, host)
    except (OSError, AttributeError, TypeError, ValueError):
        return None


class _Storage:
    """Cached ctypes bindings; each reading owns and releases its CF/IO objects."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise OSError("IOKit signals require macOS")
        self.io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer, uint = ctypes.c_void_p, ctypes.c_uint32
        signatures = [
            (self.io.IOServiceMatching, [ctypes.c_char_p], pointer),
            (self.io.IOServiceGetMatchingServices, [uint, pointer, ctypes.POINTER(uint)], ctypes.c_int),
            (self.io.IOIteratorNext, [uint], uint),
            (self.io.IOObjectRelease, [uint], ctypes.c_int),
            (self.io.IORegistryEntryGetRegistryEntryID, [uint, ctypes.POINTER(ctypes.c_uint64)], ctypes.c_int),
            (self.io.IORegistryEntryCreateCFProperty, [uint, pointer, pointer, uint], pointer),
            (self.cf.CFStringCreateWithCString, [pointer, ctypes.c_char_p, uint], pointer),
            (self.cf.CFDictionaryGetValue, [pointer, pointer], pointer),
            (self.cf.CFNumberGetValue, [pointer, ctypes.c_int, pointer], ctypes.c_bool),
            (self.cf.CFGetTypeID, [pointer], ctypes.c_ulong),
            (self.cf.CFDictionaryGetTypeID, [], ctypes.c_ulong),
            (self.cf.CFNumberGetTypeID, [], ctypes.c_ulong),
            (self.cf.CFRelease, [pointer], None),
        ]
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result

    def read(self) -> DiskCounters | None:
        io, cf = self.io, self.cf
        keys = []
        iterator = ctypes.c_uint32()
        try:
            for name in (b"Statistics", b"Total Time (Read)", b"Total Time (Write)"):
                key = cf.CFStringCreateWithCString(None, name, 0x08000100)  # UTF-8
                if not key:
                    return None
                keys.append(key)
            matching = io.IOServiceMatching(b"IOBlockStorageDriver")
            if not matching:
                return None
            # IOServiceGetMatchingServices consumes matching, including on failure.
            if io.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator)):
                return None
            result = {}
            for _ in range(64):  # bound work even on an unexpectedly large registry
                device = io.IOIteratorNext(iterator.value)
                if not device:
                    return result or None
                stats = None
                try:
                    identity = ctypes.c_uint64()
                    if io.IORegistryEntryGetRegistryEntryID(device, ctypes.byref(identity)):
                        return None
                    stats = io.IORegistryEntryCreateCFProperty(device, keys[0], None, 0)
                    if not stats or cf.CFGetTypeID(stats) != cf.CFDictionaryGetTypeID():
                        return None
                    counters = []
                    for key in keys[1:]:
                        number = cf.CFDictionaryGetValue(stats, key)
                        value = ctypes.c_int64()
                        if (not number or cf.CFGetTypeID(number) != cf.CFNumberGetTypeID()
                                or not cf.CFNumberGetValue(number, 4, ctypes.byref(value)) or value.value < 0):
                            return None
                        counters.append(value.value)
                    result[identity.value] = (counters[0], counters[1])
                finally:
                    if stats:
                        cf.CFRelease(stats)
                    io.IOObjectRelease(device)
            return None
        finally:
            if iterator.value:
                io.IOObjectRelease(iterator.value)
            for key in keys:
                cf.CFRelease(key)


def _disk_counters() -> DiskCounters | None:
    global _iokit
    try:
        if _iokit is None:
            _iokit = _Storage()
        return _iokit.read()
    except (OSError, AttributeError, TypeError, ValueError):
        return None


class Sampler:
    """A sampling history, owned by the detached admission reader.

    First samples, missing data, changed device/CPU sets, reset counters and
    nonpositive intervals hold nothing. A lost CPU reading resets its EWMA;
    stale utilization is never carried across an unreadable interval.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._at: float | None = None
        self._ticks: Ticks | None = None
        self._disks: DiskCounters | None = None
        self._idle: float | None = None

    def read(self) -> dict[str, Any]:
        started = time.perf_counter()
        now = self._clock()
        ticks, disks = _cpu_ticks(), _disk_counters()
        elapsed = now - self._at if self._at is not None else 0
        idle = disk = None
        if elapsed > 0 and ticks and self._ticks and len(ticks) == len(self._ticks):
            deltas = [tuple((cur - old) & 0xFFFFFFFF for cur, old in zip(current, previous))
                      for current, previous in zip(ticks, self._ticks)]
            total = sum(sum(cpu) for cpu in deltas)
            # A backwards counter is a reset, except for a normal uint32 wrap.
            if total > 0 and all(delta < 2**31 for cpu in deltas for delta in cpu):
                raw = sum(cpu[2] for cpu in deltas) / total
                alpha = -math.expm1(-elapsed / CPU_EWMA_S)
                idle = raw if self._idle is None else self._idle + alpha * (raw - self._idle)
        if elapsed > 0 and disks and self._disks and disks.keys() == self._disks.keys():
            changes = [tuple(current - previous for current, previous in zip(values, self._disks[key]))
                       for key, values in disks.items()]
            if all(delta >= 0 for pair in changes for delta in pair):
                disk = min(1.0, max(sum(pair) for pair in changes) / (elapsed * 1e9))
        self._at, self._ticks, self._disks, self._idle = now, ticks, disks, idle
        try:
            load1, load5, _ = os.getloadavg()
        except OSError:
            load1 = load5 = None
        level = _sysctl_int("kern.memorystatus_vm_pressure_level")
        return {"load1": load1, "load5": load5, "cpus": os.cpu_count(),
                "memory_pressure": level if level in (1, 2, 4) else None,
                "cpu_idle": idle, "disk_busy": disk, "observed_at": time.time(),
                "read_cost_s": time.perf_counter() - started}


_sampler = Sampler()


def read() -> dict[str, Any]:
    """Read once per detached pass; no subprocess, including the first call."""
    return _sampler.read()
