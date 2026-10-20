"""D-MG2 / C-6.13: bounded signal properties and legacy differential checks."""

import copy

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import scheduler
from subfleet.policy import MACHINE_GUARD_PROPOSAL
from tests.machine_guard_reference import LEGACY_GUARD, machine_hold as previous_hold

CI = settings(max_examples=40, deadline=None, derandomize=True, database=None)
FRACTION = st.floats(0, 1, allow_nan=False, allow_infinity=False)
BAD = st.sampled_from([None, True, False, -1, 2, float("nan"), float("inf"), "busy"])


def policy(**limits):
    return {"admission": {"machine_guard": {"background": limits}}}


def held(state, chosen, **reading):
    snapshot = state.advance(chosen, reading)
    return scheduler.machine_hold(chosen, snapshot, "background")


@CI
@given(FRACTION, FRACTION, FRACTION)
def test_lower_idle_never_releases_a_class_that_higher_idle_holds(a, b, threshold):
    chosen = policy(cpu_idle_min=threshold)
    low, high = sorted((a, b))
    for stateful in (False, True):
        left, right = {"cpu_idle": low}, {"cpu_idle": high}
        if stateful:
            lstate, rstate = scheduler.MachineGuard(), scheduler.MachineGuard()
            # Give both paths an identical held history before comparing recovery.
            lstate.advance(chosen, {"cpu_idle": 0})
            rstate.advance(chosen, {"cpu_idle": 0})
            left, right = lstate.advance(chosen, left), rstate.advance(chosen, right)
        if scheduler.machine_hold(chosen, right, "background"):
            assert scheduler.machine_hold(chosen, left, "background")


@CI
@given(FRACTION, FRACTION, st.sampled_from(["attended", "priority", "session", "unknown"]))
def test_unnamed_attended_and_priority_classes_are_never_held(idle, disk, klass):
    chosen = policy(cpu_idle_min=1, disk_busy_max=.01)
    if klass in ("attended", "priority"):
        chosen["admission"]["machine_guard"][klass] = {"cpu_idle_min": 1, "disk_busy_max": .01}
    reading = scheduler.MachineGuard().advance(chosen, {"cpu_idle": idle, "disk_busy": disk})
    assert scheduler.machine_hold(chosen, reading, klass) is None


@CI
@given(BAD, st.sampled_from([("cpu_idle", "cpu_idle_min", .15, 0),
                           ("disk_busy", "disk_busy_max", .8, 1)]))
def test_unreadable_signal_holds_nothing_and_clears_a_latch(value, signal):
    key, limit, threshold, busy = signal
    chosen, state = policy(**{limit: threshold}), scheduler.MachineGuard()
    assert held(state, chosen, **{key: busy})
    assert held(state, chosen, **{key: value}) is None
    assert scheduler.machine_hold(chosen, {key: value}, "background") is None


@CI
@given(st.floats(.01, .9, allow_nan=False), st.integers(1, 6))
def test_cpu_release_requires_three_point_margin_for_two_consecutive_passes(threshold, dips):
    chosen, state = policy(cpu_idle_min=threshold), scheduler.MachineGuard()
    assert held(state, chosen, cpu_idle=threshold / 2)
    for _ in range(dips):
        assert held(state, chosen, cpu_idle=threshold + .015)
    assert held(state, chosen, cpu_idle=threshold + .04)
    assert held(state, chosen, cpu_idle=threshold + .015)  # resets the two-pass count
    assert held(state, chosen, cpu_idle=threshold + .04)
    assert held(state, chosen, cpu_idle=threshold + .04) is None


@CI
@given(st.floats(.2, .99, allow_nan=False))
def test_disk_release_requires_ten_point_margin_for_two_passes(threshold):
    chosen, state = policy(disk_busy_max=threshold), scheduler.MachineGuard()
    assert held(state, chosen, disk_busy=1)
    assert held(state, chosen, disk_busy=threshold - .05)
    assert held(state, chosen, disk_busy=threshold - .11)
    assert held(state, chosen, disk_busy=threshold - .11) is None


def test_multiple_jobs_share_one_pass_and_policy_edits_clear_latches():
    state, chosen = scheduler.MachineGuard(), policy(cpu_idle_min=.15)
    assert held(state, chosen, cpu_idle=0)
    reading = state.advance(chosen, {"cpu_idle": .5})
    for _ in range(30):
        assert scheduler.machine_hold(chosen, reading, "background")
    assert held(state, chosen, cpu_idle=.5) is None
    assert held(state, chosen, cpu_idle=0)
    assert held(state, policy(cpu_idle_min=.01), cpu_idle=.02) is None
    assert state.advance({"admission": {"machine_guard": None}}, {"cpu_idle": 0})
    assert held(state, chosen, cpu_idle=.16) is None


def test_independent_signals_are_or_conditions_and_unreadable_does_not_mask_another():
    chosen, state = policy(cpu_idle_min=.15, disk_busy_max=.8, memory_pressure="warn"), scheduler.MachineGuard()
    assert held(state, chosen, cpu_idle=None, disk_busy=.9)["disk_busy"] == .9
    hold = held(state, chosen, cpu_idle=.01, disk_busy=None)
    assert hold["cpu_idle"] == .01 and "disk_busy" not in hold
    assert held(state, chosen, cpu_idle=None, disk_busy=None, memory_pressure=2)["memory_pressure"] == 2


LEGACY_READINGS = st.one_of(st.none(), st.fixed_dictionaries({
    "load1": st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True)),
    "load5": st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True)),
    "cpus": st.one_of(st.none(), st.integers(-1, 256)),
    "memory_pressure": st.one_of(st.none(), st.integers(-1, 10)),
    "cpu_idle": st.one_of(BAD, FRACTION), "disk_busy": st.one_of(BAD, FRACTION),
}))


@CI
@given(st.lists(LEGACY_READINGS, min_size=1, max_size=12),
       st.sampled_from([None, {}, LEGACY_GUARD,
                       {"background": {"load_per_cpu": .01}},
                       {"session": {"memory_pressure": "warn"}}]))
def test_legacy_only_guard_matches_473fe2941_on_every_generated_reading(readings, guard):
    chosen, state = {"admission": {"machine_guard": copy.deepcopy(guard)}}, scheduler.MachineGuard()
    for reading in readings:
        snapshot = state.advance(chosen, reading)
        for klass in ("attended", "priority", "session", "background", "unknown"):
            expected = previous_hold(chosen, reading, klass)
            assert scheduler.machine_hold(chosen, reading, klass) == expected
            assert scheduler.machine_hold(chosen, snapshot, klass) == expected


@pytest.mark.parametrize("disk", [None, .95])
def test_october_11_high_load_with_idle_headroom_never_holds_by_cpu(disk):
    chosen = {"admission": {"machine_guard": MACHINE_GUARD_PROPOSAL}}
    reading = {"load1": 398, "load5": 316, "cpus": 18, "cpu_idle": .32, "disk_busy": disk}
    for klass in ("background", "session"):
        hold = scheduler.machine_hold(chosen, reading, klass)
        assert hold is None or ("disk_busy" in hold and "cpu_idle" not in hold and "load_per_cpu" not in hold)


def test_october_20_saturated_cpu_holds_session_under_proposal():
    chosen = {"admission": {"machine_guard": MACHINE_GUARD_PROPOSAL}}
    hold = scheduler.machine_hold(chosen, {"load1": 225, "cpus": 18, "cpu_idle": .01}, "session")
    assert hold["cpu_idle"] == .01 and hold["cpu_idle_min"] == .05
    assert MACHINE_GUARD_PROPOSAL["background"]["cpu_idle_min"] == .15


def test_new_hold_text_names_readings_thresholds_and_release_progress():
    from subfleet.render import _machine
    state, chosen = scheduler.MachineGuard(), policy(cpu_idle_min=.15, disk_busy_max=.8)
    hold = held(state, chosen, cpu_idle=.01, disk_busy=.95)
    text = _machine(hold)
    assert "CPU idle 1.0%" in text and "15.0%" in text
    assert "disk busy 0.950" in text and "0.800" in text
    hold = held(state, chosen, cpu_idle=.5, disk_busy=.1)
    assert "1/2" in _machine(hold)
