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
with C-level work as with Python's, by the work's own shape, and repeats. They count
in a fresh child (`_counted_in_child`), as the scrubber's probes do: the count is the
whole process's, and in pytest's own two counts of one call differed by 3.8%.
"""

from __future__ import annotations

import json
import subprocess
import sys
from itertools import repeat
from pathlib import Path

import pytest

from tests.cpu_scaling import COUNT_GROWTH_MAX, GROWTH_RATIO_MAX, best_cpu_samples, counts_instructions

#: The instruction count where the CPU's counter is exposed to the kernel: not in a
#: virtual machine that hides it, as CI's `macos-15` runners do (run 40416019815 skipped
#: these three tests on both interpreters).
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


def _counted_in_child(script: str):
    """`script`'s JSON output, run in a fresh interpreter with `fewest_instructions`,
    `SIZES`, `linear` and `quadratic` in scope. The count is the whole process's, so it
    is taken where no other thread works and no test has run: two counts of one call
    in pytest's own process differed by 3.8% (host load 46 over a minute, 147 over
    five), while a fresh child's differed by at most 0.051% over 40 pairs (#266 review
    r3, P2-1)."""
    source = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
              "from tests.cpu_scaling import fewest_instructions\n"
              "from tests.unit.test_cpu_scaling import SIZES, linear, quadratic\n" + script)
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@INSTRUCTIONS
def test_the_instruction_count_grows_with_c_level_work():
    """What the count adds to a count of regex reads and Python lines: work done in C.
    One `str.count` over four times the characters retires four times the
    instructions, and the count is the CPU's own (more than one a character)."""
    small, large = _counted_in_child('''
import json
print(json.dumps([fewest_instructions(lambda text="x" * size: text.count("y")) for size in (1 << 20, 1 << 22)]))
''')
    assert small > 1 << 20, (small, large)
    assert 3.9 < large / small < 4.1, (small, large)


@INSTRUCTIONS
def test_the_instruction_count_tells_linear_work_from_quadratic():
    """C-23.14, C-25.5: the count criterion's scope, as above for CPU time. Linear work
    passes `COUNT_GROWTH_MAX` at the scaling tests' sizes, and so does the same work
    done twice; work quadratic in the input fails it."""
    counts = _counted_in_child('''
import json
shapes = {"once": linear, "twice": lambda size: linear(size, times=2), "square": quadratic}
print(json.dumps({name: [fewest_instructions(make(size)) for size in SIZES] for name, make in shapes.items()}))
''')
    once, twice, square = counts["once"], counts["twice"], counts["square"]
    assert once[1] / once[0] < COUNT_GROWTH_MAX, counts
    assert twice[1] / twice[0] < COUNT_GROWTH_MAX, counts
    assert square[1] / square[0] >= COUNT_GROWTH_MAX, counts


@INSTRUCTIONS
def test_the_instruction_count_repeats():
    """Why the count replaces timing: in a fresh child, the same call retires the same
    instructions, the fewest of ten runs twice over within 1% (CPU time's ratio reached
    9x for 4x on CI)."""
    first, second = _counted_in_child('''
import json
call = linear(SIZES[1])
print(json.dumps([fewest_instructions(call, runs=10), fewest_instructions(call, runs=10)]))
''')
    assert abs(first - second) < first / 100, (first, second)
