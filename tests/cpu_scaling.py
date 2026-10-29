"""CPU growth measurements shared by the scrubber's bounded child probes.

They detect super-linear growth (a quadratic regression), not a constant-factor
slowdown: see `best_cpu_samples`, and `tests/unit/test_cpu_scaling.py`, which
pins that scope. `fewest_instructions` measures the same growth by the
instructions the CPU retires, a count the runner's load barely moves.
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
#: The growth criterion for counts, which load moves little (`fewest_instructions`) or
#: not at all (the scrub's regex reads and lines, `tests.scrub_work`): linear work grows
#: about 4× (its fixed part makes it less), quadratic about 16×, and with so little
#: noise to allow for the bound can sit near 4.
COUNT_GROWTH_MAX = 5
#: The constant-factor bound against release/217's own scrub on one input
#: (`best_cpu_ratio`, C-23.14). Since #239 round 8 every mode runs release's pipeline
#: and then the head's, each on the original text, and hides what either hides
#: (`shown_by_both`), so the head's scrub does release's work and then its own: 2.5
#: leaves the head's own pass up to 1.5 times release's, and catches a pass that does
#: several times that.
COST_RATIO_MAX = 2.5


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


def best_cpu_ratio(call: Callable[[], None], reference: Callable[[], None], *,
                   rounds: int = 5) -> dict:
    """`call`'s CPU over `reference`'s, each the best of `rounds` equal batches,
    taken in turn so that load drifts over both alike; the reference's batch costs
    at least 20 ms.

    Scope: unlike `best_cpu_samples`, this does see a constant factor, but only
    against a reference measured alongside it in the same process on the same
    input (release/217's own rule, say), so machine speed cancels and no absolute
    budget is set (D-TM1). It catches a rule that does many times the reference's
    work at one size, such as #231's `Bearer` lookahead at the work bound.
    """
    batch = 1

    def timed(function: Callable[[], None]) -> float:
        start = time.thread_time()
        for _ in range(batch):
            function()
        return time.thread_time() - start

    while (calibration := timed(reference)) < 0.02:
        batch *= max(2, math.ceil(0.02 / max(calibration, 1e-9)))
    samples: dict[str, list[float]] = {"call": [], "reference": []}
    for _ in range(rounds):
        samples["call"].append(timed(call))
        samples["reference"].append(timed(reference))
    best = {name: min(values) for name, values in samples.items()}
    return {"call_s": best["call"] / batch, "reference_s": best["reference"] / batch, "batch": batch,
            "ratio": best["call"] / best["reference"]}


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

    Unlike CPU time, the count moves little with the runner's load, clock or
    caches. It counts every instruction the call retires, C-level `str` work and
    `re`'s engine included, so it sees work a count of regex reads or Python lines
    cannot. The kernel's work for the process counts too (page faults, system calls,
    interrupts): over ten runs at a host load of about 107, a scrub's count spread up
    to 1.5%, and the fewest of three came within 0.3% of the fewest of ten (#266
    review r2). The count is the whole process's: measure in a fresh child where no
    other thread works, as the scrubber's probes do. Inside pytest's own process two
    such counts of one call once differed by 3.8% (#266 review r3). It reads 0 where
    the CPU's counters are hidden, as on CI's virtual machines.
    """
    call()
    counts = []
    for _ in range(runs):
        before = retired_instructions()
        call()
        counts.append(retired_instructions() - before)
    return min(counts)
