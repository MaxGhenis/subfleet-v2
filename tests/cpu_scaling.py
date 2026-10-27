"""CPU growth measurements shared by the scrubber's bounded child probes.

They detect super-linear growth (a quadratic regression), not a constant-factor
slowdown: see `best_cpu_samples`, and `tests/unit/test_cpu_scaling.py`, which
pins that scope.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

#: The growth criterion: `ratio` below this for inputs four times apart. Linear
#: work grows about 4×, quadratic about 16×; 8 sits between them.
GROWTH_RATIO_MAX = 8
#: The constant-factor bound against release/217's own scrub on one input
#: (`best_cpu_ratio`, C-23.14). Since #239 round 8 every mode runs release's pipeline
#: first and the head's on what it leaves, so the head's scrub does release's work and
#: then its own: 2.5 leaves the head's own pass up to 1.5 times release's, and catches
#: a pass that does several times that.
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
