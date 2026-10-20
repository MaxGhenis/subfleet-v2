"""The CPU growth criterion's scope (C-23.14, C-25.5; #230 review r1).

The scrubber's linear-time tests pass while `tests.cpu_scaling.best_cpu_samples`
finds a call's CPU on an input four times larger under `GROWTH_RATIO_MAX` times
its CPU on the smaller one. A ratio cannot see a constant factor. These tests pin
that scope with work of known shape, at the scaling tests' own sizes: a call doing
twice the work at every size still passes (as #230's reviewer found by calling the
scrubber twice per input), and work quadratic in the input fails. They compare two
sizes of one call, as the scaling tests do, and set no machine-speed budget.
"""

from __future__ import annotations

from itertools import repeat

from tests.cpu_scaling import GROWTH_RATIO_MAX, best_cpu_samples

SIZES = (65536, 262144)                 # the scaling tests' sizes, four times apart


# Interpreter loops that allocate nothing, so the work is its iteration count.
def linear(size: int, *, times: int = 1):
    """A call doing `size // 64` iterations, `times` over."""
    steps = size // 64

    def call():
        for _ in repeat(None, times):
            for _ in repeat(None, steps):
                pass
    return call


def quadratic(size: int):
    """A call doing `(size // 1024)²` iterations: 64² and 256² at SIZES."""
    side = size // 1024

    def call():
        for _ in repeat(None, side):
            for _ in repeat(None, side):
                pass
    return call


def test_a_call_doing_twice_the_work_still_passes_the_growth_criterion():
    """C-23.14, C-25.5: the criterion sees growth, not a constant factor. Linear
    work passes, and so does the same work done twice at every size: doubling
    scales both samples alike and leaves the ratio about 4."""
    once = best_cpu_samples(linear, SIZES)
    twice = best_cpu_samples(lambda size: linear(size, times=2), SIZES)
    assert once["ratio"] < GROWTH_RATIO_MAX, once
    assert twice["ratio"] < GROWTH_RATIO_MAX, twice


def test_work_quadratic_in_the_input_fails_the_growth_criterion():
    """C-23.14, C-25.5: what the criterion is for. Work quadratic in the input
    grows about 16 times for an input four times larger, over the ceiling."""
    measured = best_cpu_samples(quadratic, SIZES)
    assert measured["ratio"] >= GROWTH_RATIO_MAX, measured
