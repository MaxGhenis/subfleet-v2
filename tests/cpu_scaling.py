"""CPU growth measurements shared by the scrubber's bounded child probes."""

from __future__ import annotations

import math
import time
from collections.abc import Callable


def best_cpu_samples(make_call: Callable[[int], Callable[[], None]],
                     sizes: tuple[int, int]) -> dict:
    """Best of five equal batches; the smaller batch costs at least 50 ms.

    Equal batch counts preserve the growth ratio. Calibrate on the best sample,
    rather than a possibly interrupted warmup, so even the fastest small sample
    is well above clock granularity. Thread CPU excludes time spent descheduled;
    taking minima also reduces interference from cache and frequency changes.
    The caller's subprocess deadline still bounds a quadratic regression.
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
    while small < 0.05:
        batch *= max(2, math.ceil(0.05 / max(small, 1e-9)))
        small = best(calls[sizes[0]], batch)
    large = best(calls[sizes[1]], batch)
    return {str(sizes[0]): small / batch, str(sizes[1]): large / batch,
            "batch": batch, "base_batch_cpu_s": small, "ratio": large / small}
