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


def test_the_cpu_ratio_sees_a_constant_factor_against_its_reference():
    """`best_cpu_ratio`'s scope: the same work as its reference reads about 1, and
    four times the work about 4, at one size; machine speed scales both alike."""
    from tests.cpu_scaling import best_cpu_ratio
    same = best_cpu_ratio(linear(SIZES[1]), linear(SIZES[1]))
    four = best_cpu_ratio(linear(SIZES[1], times=4), linear(SIZES[1]))
    assert same["ratio"] < 2, same
    assert four["ratio"] > 2, four


def test_the_cost_bound_admits_release_s_work_and_the_head_s_and_no_more():
    """C-23.14 (#239 round 8): `COST_RATIO_MAX`, the scrubber's cost guards' bound against
    release/217's own scrub, reads the composed shape (release's work, then the head's
    about as much again) under it, and four times the reference's work over it."""
    from tests.cpu_scaling import COST_RATIO_MAX, best_cpu_ratio
    composed = best_cpu_ratio(linear(SIZES[1], times=2), linear(SIZES[1]))
    four = best_cpu_ratio(linear(SIZES[1], times=4), linear(SIZES[1]))
    assert composed["ratio"] < COST_RATIO_MAX, composed
    assert four["ratio"] > COST_RATIO_MAX, four
