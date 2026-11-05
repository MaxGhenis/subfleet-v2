"""D-CREDIT-MEM1: every test runs under a phys_footprint bound, on macOS.

On 2026-11-04 one pytest-xdist worker of the credit-lane build grew to about 127 GB,
126 GB of it compressed, in about six minutes, until the host's operator stopped it
(docs/reports/credit-lane-integration-report.md). macOS enforces no address-space limit
(`ulimit -v` and RLIMIT_AS do nothing there), and RSS leaves compressed and swapped pages
out, so neither bounds a test. This plugin reads `ri_phys_footprint` from
`proc_pid_rusage`, which is what `top`'s MEM column shows, compressed pages included,
for the process that runs the tests and for each process below it, every 0.1 s.

- **The bound.** `SUBFLEET_TEST_MEMORY_MB` (default 4096; 0 turns it off). When the
  test process's footprint passes it while a test's setup, call or teardown runs, that
  phase fails: SIGUSR2 to the main thread raises `pytest.fail` there, as pytest-timeout's
  signal method raises its own failure, so a blocking read or a `select` is interrupted
  too. The failure names the footprint and RSS and holds every thread's stack. It fires
  once; it fires again only after the footprint has come back under the bound.
- **Processes below it.** A process the test started that passes the bound is killed
  (SIGKILL), and the running test fails the same way. A process that left the tree (a
  guardian's own session after its parent exited, a double fork) is out of reach.
- **The hard bound.** The bound plus half of it, at least 512 MB more, and at most 7 GB
  when the bound allows, so it stays under the host's own 8 GB watchdog. Past it (the
  main thread is inside one long C call and cannot run the handler, or code swallowed
  the failure), every thread's stack goes to `memory-bound-<pid>.txt` in
  `SUBFLEET_TEST_MEMORY_LOG` (default: the temporary folder) and the process kills
  itself; xdist reports the crashed worker and the test it was running.
- **xdist.** The controller runs no tests and bounds nothing; each worker bounds itself.

Off the Mac, `proc_pid_rusage` is missing and the plugin installs nothing. Load it with
`install(config)` (tests/conftest.py) or `-p tests.memory_bound`.
"""
from __future__ import annotations

import ctypes
import faulthandler
import os
import signal
import struct
import sys
import tempfile
import threading
import time

import pytest

ENV = "SUBFLEET_TEST_MEMORY_MB"
LOG_ENV = "SUBFLEET_TEST_MEMORY_LOG"
DEFAULT_MB = 4096
#: The hard bound stays under the host's 8 GB watchdog whenever the bound leaves room.
HARD_CEILING = 7168 << 20
INTERVAL_S = 0.1
SIGNAL = signal.SIGUSR2
NAME = "subfleet-memory-bound"
MB = 1 << 20

_PROC_PPID_ONLY = 6
_RUSAGE_INFO_V0 = 0
#: struct rusage_info_v0: a 16-byte uuid, then 64-bit counters; ri_resident_size is the
#: 7th and ri_phys_footprint the 8th.
_RESIDENT_AT, _FOOTPRINT_AT = 16 + 6 * 8, 16 + 7 * 8

_libc = None
if sys.platform == "darwin":
    _libc = ctypes.CDLL(None, use_errno=True)
    _libc.proc_pid_rusage.restype = ctypes.c_int
    _libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    _libc.proc_listpids.restype = ctypes.c_int
    _libc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]


def footprint(pid: int) -> tuple[int, int] | None:
    """(phys_footprint, resident size) of `pid` in bytes, or None once it is gone or off
    the Mac. The footprint counts compressed and swapped pages; the resident size does not."""
    if _libc is None:
        return None
    buffer = ctypes.create_string_buffer(512)
    if _libc.proc_pid_rusage(pid, _RUSAGE_INFO_V0, buffer) != 0:
        return None
    return (struct.unpack_from("=Q", buffer.raw, _FOOTPRINT_AT)[0],
            struct.unpack_from("=Q", buffer.raw, _RESIDENT_AT)[0])


def children(pid: int) -> list[int]:
    if _libc is None:
        return []
    needed = _libc.proc_listpids(_PROC_PPID_ONLY, pid, None, 0)
    if needed <= 0:
        return []
    listed = (ctypes.c_int * (needed // ctypes.sizeof(ctypes.c_int) + 64))()
    got = _libc.proc_listpids(_PROC_PPID_ONLY, pid, listed, ctypes.sizeof(listed))
    return [child for child in listed[:max(got, 0) // ctypes.sizeof(ctypes.c_int)] if child > 0]


def descendants(pid: int) -> list[int]:
    """Every live process below `pid`, read one parent at a time (never the whole table)."""
    found, pending = [], [pid]
    while pending:
        for child in children(pending.pop(0)):
            if child not in found:
                found.append(child)
                pending.append(child)
    return found


def bounds(limit_mb: int) -> tuple[int, int]:
    """(bound, hard bound) in bytes for a bound of `limit_mb`."""
    soft = limit_mb * MB
    hard = soft + max(soft // 2, 512 * MB)
    return soft, min(hard, max(HARD_CEILING, soft + 256 * MB))


def limit_mb(environ=os.environ) -> int:
    text = environ.get(ENV, "").strip()
    if not text:
        return DEFAULT_MB
    value = int(text)
    if value < 0:
        raise ValueError(f"{ENV} must be 0 (off) or a number of megabytes")
    return value


def _stacks() -> str:
    """Every thread's Python stack, as faulthandler writes it."""
    with tempfile.TemporaryFile() as dump:
        faulthandler.dump_traceback(file=dump, all_threads=True)
        dump.seek(0)
        return dump.read().decode("utf-8", "replace")


class MemoryBound:
    """The bound for one pytest process: a sampler thread and the phase window."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.soft, self.hard = bounds(limit)
        self.pid = os.getpid()
        self.main = threading.main_thread().ident
        self.nodeid: str | None = None
        self.in_phase = False
        self.armed = True
        self.pending: str | None = None
        self.peak = 0
        self.trips: list[dict] = []
        self.log_dir = os.environ.get(LOG_ENV) or tempfile.gettempdir()
        self.stop = threading.Event()
        signal.signal(SIGNAL, self._handler)
        self.thread = threading.Thread(target=self._watch, name=NAME, daemon=True)
        self.thread.start()

    # -- the main thread ------------------------------------------------------------------
    def _handler(self, signum, frame) -> None:
        message, self.pending = self.pending, None
        if message is not None and self.in_phase:
            pytest.fail(message, pytrace=True)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.nodeid = item.nodeid
        found = footprint(self.pid)
        if found is not None and found[0] <= self.soft:
            self.armed = True
        yield
        self.nodeid = None

    def _phase(self):
        self.in_phase = True
        try:
            yield
        finally:
            self.in_phase = False
            self.pending = None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item):
        yield from self._phase()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        yield from self._phase()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self, item, nextitem):
        yield from self._phase()

    def pytest_unconfigure(self, config) -> None:
        self.stop.set()
        self.thread.join(timeout=2)

    # -- the sampler ----------------------------------------------------------------------
    def _watch(self) -> None:
        while not self.stop.wait(INTERVAL_S):
            try:
                self._sample()
            except Exception:       # a sampler that dies would leave the run unbounded
                time.sleep(INTERVAL_S)

    def _sample(self) -> None:
        found = footprint(self.pid)
        if found is None:
            return
        size, resident = found
        self.peak = max(self.peak, size)
        if size > self.hard:
            self._die(size, resident)
        for pid in descendants(self.pid):
            below = footprint(pid)
            if below is not None and below[0] > self.soft:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    continue
                self._fail(f"memory bound (D-CREDIT-MEM1): process {pid}, which this test started, reached "
                           f"a phys_footprint of {below[0] // MB} MB (RSS {below[1] // MB} MB), over the "
                           f"{self.limit} MB bound ({ENV}), and was killed", rearm=True)
        if size > self.soft and self.armed:
            self._fail(f"memory bound (D-CREDIT-MEM1): this test's process reached a phys_footprint of "
                       f"{size // MB} MB (RSS {resident // MB} MB), over the {self.limit} MB bound ({ENV})",
                       rearm=False)

    def _fail(self, message: str, *, rearm: bool) -> None:
        """Fail the running phase from the main thread, once."""
        if not self.in_phase or signal.getsignal(SIGNAL) != self._handler:
            return
        self.armed = rearm
        self.trips.append({"test": self.nodeid, "message": message})
        self.pending = f"{message}.\nEvery thread's stack when it was seen:\n{_stacks()[-6000:]}"
        try:
            signal.pthread_kill(self.main, SIGNAL)
        except (OSError, ValueError):
            self.pending = None

    def _die(self, size: int, resident: int) -> None:
        """Past the hard bound: leave the stacks and end this process now."""
        try:
            path = os.path.join(self.log_dir, f"memory-bound-{self.pid}.txt")
            with open(path, "w", encoding="utf-8") as log:
                log.write(f"memory bound (D-CREDIT-MEM1): pid {self.pid} reached a phys_footprint of "
                          f"{size // MB} MB (RSS {resident // MB} MB), over the hard bound of {self.hard // MB} MB "
                          f"({ENV}={self.limit}), while running {self.nodeid}; killed\n")
                log.flush()
                faulthandler.dump_traceback(file=log, all_threads=True)
            print(f"memory bound: killed pid {self.pid} running {self.nodeid}; stacks in {path}",
                  file=sys.__stderr__, flush=True)
        finally:
            os.kill(self.pid, signal.SIGKILL)


def install(config) -> MemoryBound | None:
    """Bound this pytest process, once; none on an xdist controller, off the Mac, or at 0.
    A stand-in config without a plugin manager (a test calling `pytest_configure` itself)
    gets none either: the session already has its own."""
    manager = getattr(config, "pluginmanager", None)
    if manager is None or manager.has_plugin(NAME) or _libc is None:
        return None
    if not hasattr(config, "workerinput") and config.getoption("numprocesses", None):
        return None         # the xdist controller: its workers run the tests
    limit = limit_mb()
    if limit == 0:
        return None
    bound = MemoryBound(limit)
    config.pluginmanager.register(bound, NAME)
    return bound


def pytest_configure(config) -> None:
    install(config)
