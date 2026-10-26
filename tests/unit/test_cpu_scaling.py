"""The CPU growth criterion's scope (C-23.14, C-25.5; #230 review r1).

The scrubber's linear-time tests pass while `tests.cpu_scaling.best_cpu_samples`
finds a call's CPU on an input four times larger under `GROWTH_RATIO_MAX` times
its CPU on the smaller one. A ratio cannot see a constant factor. These tests pin
that scope with work of known shape, at the scaling tests' own sizes: a call doing
twice the work at every size still passes (as #230's reviewer found by calling the
scrubber twice per input), and work quadratic in the input fails. They compare two
sizes of one call, as the scaling tests do, and set no machine-speed budget.

`fewest_instructions` counts the instructions a call retires instead of timing it
(#266, D-SCRUB-LINEAR). The tests at the end pin what that count is for: it grows
with C-level work as with Python's, by the work's own shape, and repeats.
"""

from __future__ import annotations

from itertools import repeat

import pytest

from tests.cpu_scaling import (COUNT_GROWTH_MAX, GROWTH_RATIO_MAX, best_cpu_samples, counts_instructions,
                               fewest_instructions)

#: The instruction count where the CPU's counter is exposed to the kernel: not in a
#: virtual machine that hides it, as CI's `macos-15` runners do (run 40399635625).
INSTRUCTIONS = pytest.mark.skipif(not counts_instructions(), reason="no CPU instruction counter here")

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


@INSTRUCTIONS
def test_the_instruction_count_grows_with_c_level_work():
    """What the count adds to a count of regex reads and Python lines: work done in C.
    One `str.count` over four times the characters retires four times the
    instructions, and the count is the CPU's own (more than one a character)."""
    sizes = (1 << 20, 1 << 22)
    counts = {size: fewest_instructions(lambda text="x" * size: text.count("y")) for size in sizes}
    assert counts[sizes[0]] > sizes[0], counts
    assert 3.9 < counts[sizes[1]] / counts[sizes[0]] < 4.1, counts


@INSTRUCTIONS
def test_the_instruction_count_tells_linear_work_from_quadratic():
    """C-23.14, C-25.5: the count criterion's scope, as above for CPU time. Linear work
    passes `COUNT_GROWTH_MAX` at the scaling tests' sizes, and so does the same work
    done twice; work quadratic in the input fails it."""
    once = [fewest_instructions(linear(size)) for size in SIZES]
    twice = [fewest_instructions(linear(size, times=2)) for size in SIZES]
    square = [fewest_instructions(quadratic(size)) for size in SIZES]
    assert once[1] / once[0] < COUNT_GROWTH_MAX, once
    assert twice[1] / twice[0] < COUNT_GROWTH_MAX, twice
    assert square[1] / square[0] >= COUNT_GROWTH_MAX, square


@INSTRUCTIONS
def test_the_instruction_count_repeats():
    """Why the count replaces timing: the same call retires the same instructions, to
    within 1%, under the host's load (CPU time's ratio reached 9x for 4x on CI)."""
    call = linear(SIZES[1])
    first, second = fewest_instructions(call), fewest_instructions(call)
    assert abs(first - second) < first / 100, (first, second)
