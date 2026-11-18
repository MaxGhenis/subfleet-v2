"""C-6.13: subprocess-free, one-second machine samples and bounded calibration.

Disk busy is service milliseconds per operation on the state root's physical
device. In-flight requests are recorded separately, never capped.
"""

from __future__ import annotations

import ctypes
import math
import os
import sys
import stat
from pathlib import Path
import time
from collections.abc import Callable
from typing import Any

_libc: Any = None
_iokit: Any = None
CPU_EWMA_S = 60.0
DISK_EWMA_S = 60.0
SAMPLE_INTERVAL_S = 1.0
DEVICE_LIMIT = 1024
HISTORY_LIMIT = 1440
HISTORY_WINDOW_S = 86400.0
Ticks = tuple[tuple[int, ...], ...]
DiskCounters = dict[int, tuple[int, int, int, int]]


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
        lib.devname.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        lib.devname.restype = ctypes.c_char_p
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
    except Exception:
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
    except Exception:
        return None


class _Storage:
    """Cached bindings and one owned driver handle; CF snapshots stay per-read."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise OSError("IOKit signals require macOS")
        self.io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._selected: tuple[int, int] | None = None
        pointer, uint = ctypes.c_void_p, ctypes.c_uint32
        signatures = [
            (self.io.IOServiceMatching, [ctypes.c_char_p], pointer),
            (self.io.IOServiceGetMatchingServices, [uint, pointer, ctypes.POINTER(uint)], ctypes.c_int),
            (self.io.IOIteratorNext, [uint], uint),
            (self.io.IORegistryEntryIDMatching, [ctypes.c_uint64], pointer),
            (self.io.IOBSDNameMatching, [uint, uint, ctypes.c_char_p], pointer),
            (self.io.IOServiceGetMatchingService, [uint, pointer], uint),
            (self.io.IORegistryEntryGetParentEntry, [uint, ctypes.c_char_p, ctypes.POINTER(uint)], ctypes.c_int),
            (self.io.IOObjectConformsTo, [uint, ctypes.c_char_p], ctypes.c_bool),
            (self.io.IOObjectRelease, [uint], ctypes.c_int),
            (self.io.IORegistryEntryGetRegistryEntryID, [uint, ctypes.POINTER(ctypes.c_uint64)], ctypes.c_int),
            (self.io.IORegistryEntryInPlane, [uint, ctypes.c_char_p], ctypes.c_bool),
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

    def close(self) -> None:
        selected = getattr(self, '_selected', None)
        self._selected = None
        if selected is not None:
            self.io.IOObjectRelease(selected[1])

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass  # also safe after partial initialization or interpreter shutdown

    def device_id(self, root: Path) -> int | None:
        """Walk the state volume's IOService ancestry to its storage driver.

        devname resolves st_dev even for an APFS volume; its registry ancestry
        crosses the APFS container and partition to the physical storage driver.
        Only that driver's counters enter the guard. No busiest-device fallback.
        """
        io = self.io
        name = _system().devname(os.stat(root).st_dev, stat.S_IFBLK)
        if not name:
            return None
        matching = io.IOBSDNameMatching(0, 0, name)
        if not matching:
            return None
        device = io.IOServiceGetMatchingService(0, matching)  # consumes matching
        try:
            for _ in range(DEVICE_LIMIT):
                if not device:
                    return None
                if io.IOObjectConformsTo(device, b"IOBlockStorageDriver"):
                    identity = ctypes.c_uint64()
                    if not io.IORegistryEntryGetRegistryEntryID(device, ctypes.byref(identity)):
                        self.close()
                        self._selected = identity.value, device
                        device = 0  # transfer the owned send right to the cache
                        return identity.value
                    return None
                parent = ctypes.c_uint32()
                if io.IORegistryEntryGetParentEntry(device, b"IOService", ctypes.byref(parent)):
                    return None
                io.IOObjectRelease(device)
                device = parent.value
            return None
        finally:
            if device:
                io.IOObjectRelease(device)

    def read(self, device_id: int | None = None) -> DiskCounters | None:
        io, cf = self.io, self.cf
        keys = []
        iterator = ctypes.c_uint32()
        selected = 0
        selected_readable = False
        try:
            for name in (b"Statistics", b"Total Time (Read)", b"Total Time (Write)",
                         b"Operations (Read)", b"Operations (Write)"):
                key = cf.CFStringCreateWithCString(None, name, 0x08000100)  # UTF-8
                if not key:
                    return None
                keys.append(key)
            if device_id is None:  # bounded diagnostic enumeration
                matching = io.IOServiceMatching(b"IOBlockStorageDriver")
                if not matching:
                    return None
                # IOServiceGetMatchingServices consumes matching, including on failure.
                if io.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator)):
                    return None
            else:
                cached = getattr(self, '_selected', None)
                if cached is not None and cached[0] != device_id:
                    self.close()
                    cached = None
                if cached is None:
                    matching = io.IORegistryEntryIDMatching(device_id)
                    if not matching:
                        return None
                    selected = io.IOServiceGetMatchingService(0, matching)
                    if not selected:
                        return None
                    self._selected = device_id, selected
                else:
                    selected = cached[1]
                # A retained registry object can outlive attachment and still
                # expose stale properties. Never use those as a fresh sample.
                if not io.IORegistryEntryInPlane(selected, b"IOService"):
                    return None
            result = {}
            for _ in range(DEVICE_LIMIT if device_id is None else 1):
                device = selected if device_id is not None else io.IOIteratorNext(iterator.value)
                if not device:
                    return result or None
                stats = None
                try:
                    identity = ctypes.c_uint64()
                    if io.IORegistryEntryGetRegistryEntryID(device, ctypes.byref(identity)):
                        continue
                    if device_id is not None and identity.value != device_id:
                        continue
                    stats = io.IORegistryEntryCreateCFProperty(device, keys[0], None, 0)
                    if not stats or cf.CFGetTypeID(stats) != cf.CFDictionaryGetTypeID():
                        continue
                    counters = []
                    for key in keys[1:]:
                        number = cf.CFDictionaryGetValue(stats, key)
                        value = ctypes.c_int64()
                        if (not number or cf.CFGetTypeID(number) != cf.CFNumberGetTypeID()
                                or not cf.CFNumberGetValue(number, 4, ctypes.byref(value)) or value.value < 0):
                            break
                        counters.append(value.value)
                    if len(counters) == 4 and (device_id is None or
                            io.IORegistryEntryInPlane(device, b"IOService")):
                        result[identity.value] = tuple(counters)
                        selected_readable = True
                except Exception:
                    # A disappearing or unreadable neighbor cannot hide this SSD.
                    continue
                finally:
                    if stats:
                        cf.CFRelease(stats)
                    if device_id is None:
                        io.IOObjectRelease(device)
            return result or None
        finally:
            # A failed selected read is unknown. Drop only its native handle;
            # the sampler keeps counters/EWMA and retries by ID next sample.
            if selected and not selected_readable:
                self.close()
            if iterator.value:
                io.IOObjectRelease(iterator.value)
            for key in keys:
                cf.CFRelease(key)


def _disk_counters(device_id: int | None = None) -> DiskCounters | None:
    global _iokit
    try:
        if _iokit is None:
            _iokit = _Storage()
        return _iokit.read(device_id)
    except Exception:
        return None



def _root_device_id(root: Path) -> int:
    """Cheap per-sample identity check; registry ancestry is resolved on change."""
    return os.stat(root).st_dev


def _state_device_id(root: Path) -> int | None:
    global _iokit
    try:
        if _iokit is None:
            _iokit = _Storage()
        return _iokit.device_id(root)
    except Exception:
        return None


def _source(function: Callable, *args: Any) -> Any:
    """Every independent source fails open on Exception, never BaseException."""
    try:
        return function(*args)
    except Exception:
        return None


def _legacy_read() -> dict[str, Any]:
    """Legacy fields only: null/load/memory policies run no new native source."""
    started = time.perf_counter()
    loads = _source(os.getloadavg)
    level = _source(_sysctl_int, "kern.memorystatus_vm_pressure_level")
    return {"load1": loads[0] if loads else None, "load5": loads[1] if loads else None,
            "cpus": _source(os.cpu_count), "memory_pressure": level if level in (1, 2, 4) else None,
            "observed_at": time.time(), "read_cost_s": time.perf_counter() - started}


def read() -> dict[str, Any]:
    return _legacy_read()


class _EWMA:
    """Bias-corrected, elapsed-time EWMA; a short first sample has short weight."""
    def __init__(self, tau: float) -> None:
        self.tau, self.total, self.weight = tau, 0.0, 0.0

    def add(self, value: float, elapsed: float) -> float:
        alpha = -math.expm1(-elapsed / self.tau)
        self.total += alpha * (value - self.total)
        self.weight += alpha * (1.0 - self.weight)
        return self.total / self.weight


class Sampler:
    """Daemon-owned history; native sources run at most once per second.

    A lost/zero-tick read changes neither the valid baseline nor EWMA. The
    snapshot marks that source unreadable, so it cannot hold or recover a latch.
    Quiet baseline: p10 of up to 1,440 minute samples in the preceding 24 hours,
    on this device. The daemon restores those samples from its bounded events.
    Baseline is calibration evidence, not an automatically selected threshold.
    """
    def __init__(self, root: str | Path | None = None, *, clock: Callable[[], float] = time.monotonic,
                 history: Any = ()) -> None:
        self.root = Path(root or Path.cwd())
        self._clock = clock
        self.device_id: int | None = None
        self._root_device: int | None = None
        self._polled: float | None = None
        self._cpu_at: float | None = None
        self._disk_at: float | None = None
        self._ticks: Ticks | None = None
        self._disk: tuple[int, ...] | None = None
        self._disk_id: int | None = None
        self._cpu_ewma, self._disk_ewma = _EWMA(CPU_EWMA_S), _EWMA(DISK_EWMA_S)
        self._snapshot: dict[str, Any] = {}
        self._latest: dict[str, tuple[Any, float]] = {}
        self._history = list(history)[-HISTORY_LIMIT:]

    @property
    def baseline_ms(self) -> float | None:
        return self._baseline(time.time())[0]

    def _baseline(self, now: float) -> tuple[float | None, float | None]:
        identity = self.device_id
        eligible = [row for row in self._history
                    if self._baseline_eligible(row, now - HISTORY_WINDOW_S, identity)]
        values = sorted(row['disk_service_ms_raw'] for row in eligible)
        return (values[int((len(values)-1)*.1)] if values else None,
                max((row['observed_at'] for row in eligible), default=None))

    @staticmethod
    def _baseline_eligible(row: dict[str, Any], cutoff: float, identity: int | None) -> bool:
        value = row.get('disk_service_ms_raw')
        return (identity is not None and row.get('disk_device_id') == identity
                and row.get('observed_at', 0) >= cutoff
                and isinstance(value, (int, float)) and value > 0 and math.isfinite(value))

    def _select_device(self, identity: int | None, root_changed: bool = False) -> None:
        if identity is None:
            return  # unknown is not evidence of a device transition
        if root_changed or identity != self.device_id:
            # Even a reused registry ID cannot carry counters or smoothing from
            # another root device. Discard its calibration if reuse is observed.
            if root_changed and identity is not None and identity == self.device_id:
                self._history = [r for r in self._history if r.get('disk_device_id') != identity]
            self._disk = self._disk_at = self._disk_id = None
            self._disk_ewma = _EWMA(DISK_EWMA_S)
            for key in ('disk_busy', 'disk_in_flight', 'disk_baseline_ms'):
                self._latest.pop(key, None)
        self.device_id = identity

    def record(self, row: dict[str, Any]) -> None:
        self._history = (self._history + [dict(row)])[-HISTORY_LIMIT:]

    def prime(self) -> None:
        # Startup only, before admission is offered. The wait ensures its first
        # pass has a full sampling interval, while keeping reads <= 1 Hz.
        self.read()
        time.sleep(SAMPLE_INTERVAL_S)

    def read(self) -> dict[str, Any]:
        now = self._clock()
        if self._polled is not None and now - self._polled < SAMPLE_INTERVAL_S:
            return dict(self._snapshot)
        started = time.perf_counter()
        self._polled = now
        ticks = _source(_cpu_ticks)
        idle = disk = raw_disk = in_flight = None
        if ticks:
            if self._ticks and len(ticks) == len(self._ticks):
                deltas = [tuple((cur-old) & 0xFFFFFFFF for cur, old in zip(cur_cpu, old_cpu))
                          for cur_cpu, old_cpu in zip(ticks, self._ticks)]
                total = sum(map(sum, deltas))
                if total > 0 and all(d < 2**31 for cpu in deltas for d in cpu):
                    elapsed = now - self._cpu_at
                    if elapsed > 0:
                        idle = self._cpu_ewma.add(sum(cpu[2] for cpu in deltas)/total, elapsed)
                    self._ticks, self._cpu_at = ticks, now
                elif total > 0:  # reset; a zero delta retains the old baseline
                    self._ticks, self._cpu_at = ticks, now
            else:
                self._ticks, self._cpu_at = ticks, now
        root_device = _source(_root_device_id, self.root)
        current = None
        if root_device is not None:
            root_changed = self._root_device is not None and root_device != self._root_device
            resolved = root_device != self._root_device or self.device_id is None
            identity = _source(_state_device_id, self.root) if resolved else self.device_id
            if identity is not None:
                self._select_device(identity, root_changed)
                # Commit this key only after successful resolution. A failed
                # lookup on a changed root must be retried without reading the
                # old root's still-readable driver in the meantime.
                self._root_device = root_device
                disks = _source(_disk_counters, identity)
                current = (disks or {}).get(identity)
                if current is None and not resolved:
                    replacement = _source(_state_device_id, self.root)
                    self._select_device(replacement)
                    if replacement is not None and replacement != identity:
                        disks = _source(_disk_counters, replacement)
                        current = (disks or {}).get(replacement)
        if current is not None:
            if self._disk is not None and self._disk_id == self.device_id:
                changes = tuple(c-p for c,p in zip(current,self._disk))
                operations = sum(changes[2:])
                if all(d >= 0 for d in changes) and operations > 0:
                    elapsed = now - self._disk_at
                    raw_disk = sum(changes[:2]) / operations / 1e6
                    if elapsed > 0:
                        disk = self._disk_ewma.add(raw_disk,elapsed)
                        in_flight = sum(changes[:2]) / (elapsed * 1e9)
                    self._disk, self._disk_at = current, now
                elif any(d < 0 for d in changes):
                    self._disk_ewma = _EWMA(DISK_EWMA_S)
                    self._disk, self._disk_at = current, now
            else:
                self._disk, self._disk_at, self._disk_id = current, now, self.device_id
        row = _legacy_read()
        row.update(cpu_idle=idle, disk_busy=disk, disk_service_ms_raw=raw_disk,
                   disk_in_flight=in_flight, disk_device_id=self.device_id,
                   disk_baseline_ms=self.baseline_ms, sampled_at=now, observed_at=time.time())
        row['read_cost_s'] = time.perf_counter() - started
        self._snapshot = row
        for key in ('cpu_idle','disk_busy','disk_in_flight','disk_baseline_ms','read_cost_s'):
            if row[key] is not None:
                self._latest[key] = row[key], row['observed_at']
        return dict(row)

    def status(self) -> dict[str, Any]:
        now = time.time()
        units = {'cpu_idle':'fraction','disk_busy':'ms/op','disk_in_flight':'requests',
                 'disk_baseline_ms':'ms/op','read_cost_s':'seconds'}
        result = {}
        for key, unit in units.items():
            # Admission can evict an old device's entry while status is read.
            # Fetch its value and timestamp together, with no later key lookup.
            value, observed_at = self._latest.get(key, (None, None))
            result[key] = {'value': value,
                           'age_s': max(0, now-observed_at) if observed_at is not None else None,
                           'unit': unit, 'readable': self._snapshot.get(key) is not None}
        # Value and age use one eligible set at one instant, including at the
        # 24-hour boundary. Recomputing a percentile never refreshes its age.
        baseline, calibrated_at = self._baseline(now)
        result['disk_baseline_ms'].update(value=baseline, readable=baseline is not None,
            age_s=max(0, now-calibrated_at) if calibrated_at is not None else None)
        result.update(sample_interval_s=SAMPLE_INTERVAL_S, disk_device_id=self.device_id,
                      baseline_samples=len(self._history))
        return result
