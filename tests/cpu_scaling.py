"""CPU growth measurements shared by the scrubber's bounded child probes.

They detect super-linear growth (a quadratic regression), not a constant-factor
slowdown: see `best_cpu_samples`, and `tests/unit/test_cpu_scaling.py`, which
pins that scope. `fewest_instructions` measures the same growth by the
instructions the CPU retires, a count the runner's load does not move.
"""

from __future__ import annotations

import ctypes
import functools
import math
import os
import time
from collections.abc import Callable

#: The growth criterion: `ratio` below this for inputs four times apart. Linear
#: work grows about 4×, quadratic about 16×; 8 sits between them.
GROWTH_RATIO_MAX = 8
#: The growth criterion for counts, which load does not move (`fewest_instructions`,
#: and the scrub's regex reads and lines, `tests.scrub_work`): linear work grows about
#: 4× (its fixed part makes it less), quadratic about 16×, and with no noise to allow
#: for the bound can sit near 4.
COUNT_GROWTH_MAX = 5


def best_cpu_samples(make_call: Callable[[int], Callable[[], None]],
                     sizes: tuple[int, int]) -> dict:
    """Best of five equal batches; the smaller batch costs at least 20 ms.

    Equal batch counts preserve the growth ratio. Calibrate on the best sample,
    rather than a possibly interrupted warmup, so even the fastest small sample
    is well above clock granularity. Thread CPU excludes time spent descheduled;
    taking minima also reduces interference from cache and frequency changes.
    The caller's subprocess deadline still bounds a quadratic regression.

    Scope: `ratio` is the growth from the smaller input to the larger one, so
    it detects super-linear growth (a quadratic regression) and cannot see a
    constant-factor slowdown, which scales both samples alike. A call that does
    twice the work at every size keeps its ratio and passes `GROWTH_RATIO_MAX`
    (#230 review r1 called the code under test twice per input; the tests still
    passed). That is deliberate: a machine-speed budget in CI is the timing
    flake class #230 removed (D-TM1), so no constant-factor gate is added here.
    The one bound on the per-call seconds returned is the wrapper probe's
    original 0.5 s at 64 KiB (`test_sessions_handoff_excerpts.py`), far above
    what that call costs, so it catches only a gross slowdown.
    """
    calls = {size: make_call(size) for size in sizes}

    def best(call, batch):
        samples = []
        for _ in range(5):
            start = time.thread_time()
            for _ in range(batch):
                call()
            samples.append(time.thread_time() - start)
        return min(samples)

    batch = 1
    small = best(calls[sizes[0]], batch)
    while small < 0.02:
        batch *= max(2, math.ceil(0.02 / max(small, 1e-9)))
        small = best(calls[sizes[0]], batch)
    large = best(calls[sizes[1]], batch)
    return {str(sizes[0]): small / batch, str(sizes[1]): large / batch,
            "batch": batch, "base_batch_cpu_s": small, "ratio": large / small}


class _RusageInfoV4(ctypes.Structure):
    """`struct rusage_info_v4` (<sys/resource.h>): the uuid, 29 counters, then
    `ri_instructions`, `ri_cycles` and four more."""
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16), ("ri_before", ctypes.c_uint64 * 29),
                ("ri_instructions", ctypes.c_uint64), ("ri_cycles", ctypes.c_uint64),
                ("ri_after", ctypes.c_uint64 * 4)]


@functools.cache
def _proc_pid_rusage():
    return ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).proc_pid_rusage


def retired_instructions() -> int:
    """The instructions this process has retired, as the kernel reads them from the
    CPU's own counter (`proc_pid_rusage`, RUSAGE_INFO_V4); 0 where the CPU's counters
    are hidden from it, as a virtual machine may hide them."""
    info = _RusageInfoV4()
    if _proc_pid_rusage()(os.getpid(), 4, ctypes.byref(info)) != 0:
        raise OSError(ctypes.get_errno(), "proc_pid_rusage")
    return info.ri_instructions


def counts_instructions() -> bool:
    """Whether `retired_instructions` reads a live counter here."""
    return retired_instructions() > 0


def fewest_instructions(call: Callable[[], object], runs: int = 3) -> int:
    """The fewest instructions one `call()` retires, over `runs` runs after a warm-up.

    Unlike CPU time, the count does not move with the runner's load, clock or
    caches: a scrub's count varied under 0.2% between runs at a host load of 100
    (#266). It counts every instruction the call retires, C-level `str` work and
    `re`'s engine included, so it sees work a count of regex reads or Python lines
    cannot. The count is the whole process's: measure where no other thread works,
    as in the scrubber's single-threaded child probes.
    """
    call()
    counts = []
    for _ in range(runs):
        before = retired_instructions()
        call()
        counts.append(retired_instructions() - before)
    return min(counts)
