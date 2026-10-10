"""C-6.18: placement ramp invariants against an independent rolling oracle."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings, strategies as st

from subfleet.ramp import PlacementRamp

BASE = datetime(2026, 10, 10, 14, tzinfo=timezone.utc)
CLASSES = ("background", "session", "priority", "attended", "probe")
EXEMPT = {"attended", "probe"}
GENERATED = settings(max_examples=120, deadline=None, derandomize=True)


def stamp(seconds=0):
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def policy(per_minute=12, window_s=60, enabled=True):
    return {"admission": {"ramp": {"enabled": enabled, "per_minute": per_minute, "window_s": window_s}}}


def attempt(serial, when=0, *, kind="dispatch", state="running", **extra):
    return {"attempt_id": str(serial), "reserved_at": stamp(when), "kind": kind, "state": state, **extra}


def place(gate, serial, now=0, klass="background"):
    assert gate.hold(klass, stamp(now)) is None
    gate.reserve(str(serial), stamp(now), klass)


def test_this_pass_placements_consume_each_of_twelve_slots():
    gate = PlacementRamp()
    gate.begin_pass(policy(), [], stamp())
    for serial in range(12):
        place(gate, serial)
        assert gate.snapshot["used"] == serial + 1
    assert gate.hold("background", stamp()) == {
        "reason": "ramp", "enabled": True, "per_minute": 12, "window_s": 60,
        "used": 12, "limit": 12, "holding": True, "next_slot_at": stamp(60),
    }


def test_rolling_window_expires_at_exact_boundary_inside_one_pass():
    gate = PlacementRamp()
    gate.begin_pass(policy(1), [], stamp())
    place(gate, "first", 0)
    assert gate.hold("background", stamp(59.999999))
    assert gate.hold("background", stamp(60)) is None
    place(gate, "next", 60)
    assert gate.hold("session", stamp(119.999999))
    assert gate.hold("session", stamp(120)) is None


def test_fractional_quota_uses_the_literal_preplacement_comparison():
    gate = PlacementRamp()
    gate.begin_pass(policy(12, 1), [], stamp())
    assert gate.snapshot["limit"] == 0.2
    place(gate, "first")
    assert gate.snapshot["used"] == 1
    assert gate.hold("background", stamp())["next_slot_at"] == stamp(1)
    assert gate.hold("background", stamp(0.999999))
    assert gate.hold("background", stamp(1)) is None


def test_restart_reconstructs_completed_retries_and_ignores_other_kinds():
    rows = [attempt("old", -60), attempt("completed", -20, state="done", finished_at=stamp(-19)),
            attempt("retry", -10, state="failed", finished_at=stamp(-9)),
            *[attempt(kind, -5, kind=kind) for kind in ("turn", "probe", "revive")]]
    gate = PlacementRamp()
    gate.begin_pass(policy(3), rows, stamp())
    assert gate.snapshot["used"] == 2
    place(gate, "remaining")
    assert gate.hold("background", stamp())
    assert gate.snapshot["next_slot_at"] == stamp(40)


def test_attended_and_probes_never_read_clock_or_consume_slots():
    gate = PlacementRamp()
    gate.begin_pass(policy(1), [], stamp())
    place(gate, "first")
    before = dict(gate.snapshot)
    for klass in ("attended", "turn", "probe", "revive"):
        assert gate.hold(klass, "clock must not be read") is None
        gate.reserve(klass, "clock must not be read", klass)
        assert gate.snapshot == before
    gate.begin_pass(policy(1), [attempt("first"), *[
        {"attempt_id": kind, "reserved_at": "clock must not be read", "kind": kind}
        for kind in ("turn", "probe", "revive")]], stamp())
    assert gate.snapshot["used"] == 1


def test_priority_passes_but_burst_delays_until_enough_starts_expire():
    gate = PlacementRamp()
    gate.begin_pass(policy(2), [], stamp())
    place(gate, "first", 0)
    place(gate, "second", 10)
    for serial in range(3):
        place(gate, f"priority-{serial}", 20, "priority")
    assert gate.snapshot["used"] == 5
    assert gate.hold("background", stamp(60))["next_slot_at"] == stamp(80)
    assert gate.hold("session", stamp(79.999999))
    assert gate.hold("session", stamp(80)) is None


def test_status_reports_fresh_expiry_without_mutating_the_pass_snapshot():
    gate = PlacementRamp()
    gate.begin_pass(policy(1), [], stamp())
    place(gate, "first")
    before = dict(gate.snapshot)
    assert gate.status(stamp(60))["used"] == 0
    assert gate.status(stamp(60))["next_slot_at"] is None
    assert gate.snapshot == before


def test_repeated_reserve_counts_one_attempt_and_future_rows_wait_for_their_clock():
    gate = PlacementRamp()
    gate.begin_pass(policy(3), [attempt("future", 10)], stamp())
    assert gate.snapshot["used"] == 0
    place(gate, "same")
    gate.reserve("same", stamp())
    assert gate.snapshot["used"] == 1
    assert gate.status(stamp(10))["used"] == 2


def test_clock_rollback_restores_starts_reconstructed_in_this_pass():
    gate = PlacementRamp()
    gate.begin_pass(policy(1), [attempt("first")], stamp())
    assert gate.hold("background", stamp(60)) is None
    place(gate, "future", 60)
    assert gate.hold("background", stamp(20))
    assert gate.snapshot["used"] == 1
    assert gate.snapshot["next_slot_at"] == stamp(60)


@pytest.mark.parametrize("enabled", [False, True])
def test_large_integer_policy_does_not_overflow_reporting(enabled):
    gate = PlacementRamp()
    gate.begin_pass(policy(10 ** 400, 1, enabled), [], stamp())
    assert gate.snapshot["limit"] == f"{10 ** 400}/60"
    assert gate.hold("background", stamp()) is None


class ForbiddenAttempts:
    def __iter__(self):
        raise AssertionError("disabled ramp must not read attempt rows")


@pytest.mark.parametrize("cfg", [{}, policy(enabled=False)])
def test_disabled_and_absent_never_read_count_or_parse_clock(cfg):
    gate = PlacementRamp()
    gate.begin_pass(cfg, ForbiddenAttempts(), "clock must not be read")
    for klass in CLASSES:
        assert gate.hold(klass, "clock must not be read") is None
        gate.reserve(klass, "clock must not be read", klass)
    assert gate.status("clock must not be read") == {
        "enabled": False, "per_minute": 12, "window_s": 60, "used": 0,
        "limit": 12, "holding": False, "next_slot_at": None,
    }
    assert gate.reservations == {}


@pytest.mark.parametrize("cfg", [{}, policy(enabled=False)], ids=["absent", "disabled"])
def test_daemon_disabled_pass_resets_enabled_gate_without_reading_rows_or_clock(monkeypatch, cfg):
    from subfleet import daemon as daemon_module

    bare = object.__new__(daemon_module.Daemon)
    bare.policy = cfg
    bare._ramp = PlacementRamp()
    bare._ramp.begin_pass(policy(1), [attempt("previous-start")], stamp())
    assert bare._ramp.snapshot["enabled"] and bare._ramp.snapshot["holding"]
    active_gate = bare._ramp

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled daemon ramp must not read attempts or UTC clock")

    monkeypatch.setattr(daemon_module, "utcnow", forbidden)
    monkeypatch.setattr(bare, "_ramp_rows", forbidden)

    assert bare._begin_ramp_pass() == {"enabled": False, "per_minute": 12, "window_s": 60}
    assert bare._ramp is active_gate
    assert bare._ramp.snapshot["enabled"] is False
    assert bare._ramp.snapshot["used"] == 0
    assert bare._ramp.snapshot["holding"] is False
    assert bare._ramp.reservations == {}


# Integral quotas isolate the stated rolling-window invariant from the literal
# policy comparison's fractional-quota rounding. Include subminute and longer
# windows to exercise the formula, rather than assuming every window is 60 s.
QUOTAS = st.sampled_from([(per_minute, window_s)
                         for per_minute in range(1, 21)
                         for window_s in (5, 10, 15, 20, 30, 60, 90, 120)
                         if per_minute * window_s % 60 == 0])
ACTIONS = st.lists(st.one_of(
    st.tuples(st.just("submit"), st.sampled_from(CLASSES)),
    st.tuples(st.just("clock"), st.integers(0, 150)),
    st.tuples(st.just("restart"), st.just(0)),
    st.tuples(st.just("machine"), st.booleans()),
    st.tuples(st.just("disk"), st.booleans()),
), min_size=1, max_size=65)


@GENERATED
@given(quota=QUOTAS, actions=ACTIONS)
def test_R1_R3_R5_over_generated_queues_clocks_restarts_and_gate_reopenings(quota, actions):
    per_minute, window_s = quota
    limit = per_minute * window_s // 60
    cfg = policy(per_minute, window_s)
    gate = PlacementRamp()
    now, serial = 0, 0
    machine_open, disk_open = False, False
    pending, ledger, starts = [], [], []
    # Both reopenings and restart reconstruction occur in every example, even
    # when Hypothesis chooses a short sequence without those generated events.
    sequence = [("submit", "background"), ("submit", "attended"), *actions,
                ("machine", True), ("disk", True), ("restart", 0)]
    for action, value in sequence:
        if action == "submit":
            pending.append((serial, value))
            serial += 1
        elif action == "clock":
            now += value
        elif action == "machine":
            machine_open = value
        elif action == "disk":
            disk_open = value
        elif action == "restart":
            gate = PlacementRamp()

        gate.begin_pass(cfg, ledger, stamp(now))
        # Independent numeric oracle: completed attempts remain starts; turns
        # and probes never enter the count, regardless of queue or gate state.
        expected_used = sum(now - window_s < when <= now for _, when, klass in starts if klass not in EXEMPT)
        assert gate.snapshot["used"] == expected_used
        held = []
        for job_id, klass in sorted(pending, key=lambda row: row[1] != "priority"):
            if klass in EXEMPT:
                assert gate.hold(klass, stamp(now)) is None
                before = dict(gate.snapshot)
                gate.reserve(str(job_id), stamp(now), klass)
                assert gate.snapshot == before                         # R3
            elif not (machine_open and disk_open):
                held.append((job_id, klass))
                continue
            else:
                expected_used = sum(now - window_s < when <= now for _, when, kind in starts if kind not in EXEMPT)
                decision = gate.hold(klass, stamp(now))
                assert bool(decision) == (klass != "priority" and expected_used >= limit)
                if decision:
                    held.append((job_id, klass))
                    continue
                gate.reserve(str(job_id), stamp(now), klass)
            starts.append((job_id, now, klass))
            kind = "turn" if klass == "attended" else "probe" if klass == "probe" else "dispatch"
            # Every recorded start has already ended: retention, rather than
            # live-attempt filtering, is what makes restart reconstruction safe.
            ledger.append(attempt(job_id, now, kind=kind, state="done", finished_at=stamp(now)))
            ordinary = sum(now - window_s < when <= now for _, when, kind in starts
                           if kind not in EXEMPT and kind != "priority")
            assert ordinary <= limit                                  # R1
        pending = held
        expected_used = sum(now - window_s < when <= now for _, when, klass in starts if klass not in EXEMPT)
        assert gate.status(stamp(now))["used"] == expected_used         # R3/R5


@GENERATED
@given(n=st.integers(1, 90), per_minute=st.integers(1, 20),
       window_s=st.sampled_from((1, 5, 10, 15, 20, 30, 60, 90, 120, 300)),
       restart_flags=st.lists(st.booleans(), min_size=1, max_size=12))
def test_R2_free_capacity_drains_within_ceiling_minutes_plus_one_pass(n, per_minute, window_s, restart_flags):
    # Each window permits K=ceil(rate*window/60) starts under the literal
    # comparison. With an initial burst at zero, completion takes
    # floor((n-1)/K)*window <= (n-1)*60/rate < ceil(n/rate)*60 seconds.
    gate, ledger = PlacementRamp(), []
    now, placed = 0, 0
    minutes = math.ceil(n / per_minute)
    slots = (per_minute * window_s + 59) // 60
    waves = math.ceil(n / slots)
    for pass_no in range(waves + 1):
        if restart_flags[pass_no % len(restart_flags)]:
            gate = PlacementRamp()
        gate.begin_pass(policy(per_minute, window_s), ledger, stamp(now))
        while placed < n and gate.hold("background", stamp(now)) is None:
            gate.reserve(str(placed), stamp(now))
            ledger.append(attempt(placed, now))
            placed += 1
        if placed == n:
            break
        now += window_s
    assert placed == n
    assert now == (waves - 1) * window_s
    assert now <= minutes * 60                                          # R2


@GENERATED
@given(classes=st.lists(st.sampled_from(("background", "session", "priority")), min_size=1, max_size=90),
       per_minute=st.integers(1, 20), reopen_at=st.integers(0, 300))
def test_R4_ramp_drain_preserves_the_eligible_priority_and_fifo_order(classes, per_minute, reopen_at):
    # Admission's priority ordering happens before the ramp sees the queue.
    ungated = sorted(enumerate(classes), key=lambda row: row[1] != "priority")
    waiting, order, ledger = list(ungated), [], []
    now = reopen_at
    gate = PlacementRamp()
    for _ in range(len(classes) + 2):
        gate.begin_pass(policy(per_minute), ledger, stamp(now))
        held = []
        for serial, klass in waiting:
            if gate.hold(klass, stamp(now)):
                held.append((serial, klass))
                continue
            gate.reserve(str(serial), stamp(now), klass)
            ledger.append(attempt(serial, now))
            order.append((serial, klass))
        waiting = held
        if not waiting:
            break
        now += 60
        gate = PlacementRamp()                                         # R5 also crosses every burst
    assert not waiting
    assert order == ungated                                             # R4


@GENERATED
@given(per_minute=st.integers(1, 30), prior=st.integers(0, 30),
       restart_at=st.integers(1, 59), state=st.sampled_from(("reserved", "running", "done", "failed")))
def test_R5_restart_grants_only_the_remaining_allowance(per_minute, prior, restart_at, state):
    prior = min(prior, per_minute)
    ledger = [attempt(serial, state=state, finished_at=stamp(1) if state in ("done", "failed") else None)
              for serial in range(prior)]
    before = PlacementRamp()
    before.begin_pass(policy(per_minute), [], stamp())
    for serial in range(prior):
        place(before, serial)
    gate = PlacementRamp()
    gate.begin_pass(policy(per_minute), ledger, stamp(restart_at))
    assert gate.snapshot["used"] == prior
    placed = 0
    while gate.hold("background", stamp(restart_at)) is None:
        gate.reserve(f"after-restart-{placed}", stamp(restart_at))
        placed += 1
        assert placed <= per_minute
    assert placed == per_minute - prior                                 # R5
