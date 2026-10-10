"""C-6.17: production disk admission against an independent sequence oracle."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.disk import DiskAdmission, GB

from tests.disk_floor_model import BASE, Files, LOWER, RAISE, agent_rule, file, lowering, policy, stamp
POLICY = {"admission": {"disk": {"enabled": True}}}
CLASSES = ("background", "session", "priority", "attended", "probe")


@pytest.mark.parametrize("free,prior_hold,holding", [
    (39, False, True), (41, False, True), (44, True, True), (45, True, False), (46, False, False),
])
def test_recovery_rechecks_current_policy_latch_without_candidates(tmp_path, free, prior_hold, holding):
    gate = DiskAdmission(tmp_path, read_free=lambda path: free * GB,
                         holding=prior_hold, recheck_latch=True)
    gate.begin_pass(POLICY, [], stamp(0))
    assert gate.holding is holding
    assert gate.snapshot["holding"] is holding


def test_recovery_recheck_is_consumed_by_first_pass(tmp_path):
    reading = [39]
    gate = DiskAdmission(tmp_path, read_free=lambda path: reading[0] * GB, recheck_latch=True)
    gate.begin_pass(POLICY, [], stamp(0))
    assert gate.holding
    reading[0] = 46
    gate.begin_pass(POLICY, [], stamp(1))
    assert gate.holding  # Later unchanged policy passes retain candidate-driven hysteresis.
    assert gate.hold("background") is None


actions = st.lists(st.one_of(
    st.tuples(st.just("free"), st.integers(0, 160)),  # half-GB readings, including every boundary
    st.tuples(st.just("submit"), st.sampled_from(CLASSES)),
    st.tuples(st.just("end"), st.integers(0, 20)),
    st.tuples(st.just("clock"), st.integers(0, 1200)),
    st.tuples(st.just("restart"), st.just(0)),
    st.tuples(st.just("lower"), st.integers(20, 60)),
    st.tuples(st.just("raise"), st.integers(0, 80)),
), min_size=1, max_size=65)
SEQUENCES = settings(max_examples=120, deadline=None, derandomize=True,
                     suppress_health_check=[HealthCheck.function_scoped_fixture])


@pytest.mark.parametrize("invariant", ["I1-floor", "I2-progress", "I3-pacing", "I4-attended", "I5-reservations"])
@pytest.mark.parametrize("with_rulings", [False, True], ids=["policy", "timed-floor"])
@SEQUENCES
@given(actions)
def test_invariants_over_generated_sequences(tmp_path, invariant, with_rulings, sequence):
    # The model uses GB and explicit expiry times, independently of the
    # implementation's byte budgets, timestamp parsing and store evidence.
    now, free, latch, serial = 0, 39.0, False, 0
    waiting, attempts, budgets = [], [], {}
    reads = []
    from subfleet.policy import disk_settings
    files = Files()
    cfg = policy() if with_rulings else POLICY
    old_numbers = (40, 5)

    def read(path):
        reads.append(path)
        return int(free * GB)

    gate = DiskAdmission(tmp_path, read_free=read, read_ruling=files)
    for action, value in [("submit", "background"), *sequence]:
        if action == "free":
            free = value / 2
        elif action == "submit":
            waiting.append((serial, value))
            serial += 1
        elif action == "clock":
            now += value
        elif action == "lower" and with_rulings:
            files.files[LOWER] = file(lowering(value, now + 900), now)
        elif action == "raise" and with_rulings:
            files.files[RAISE] = file({"floor_gb": value, "until": stamp(now + 600)}, now)
        elif action == "end" and attempts:
            row = attempts[value % len(attempts)]
            row.update(state="finalizing", finished_at=stamp(now))
            budgets.pop(row["attempt_id"], None)
        elif action == "restart":
            old = dict(gate.reservations)
            gate = DiskAdmission(tmp_path, read_free=read, holding=gate.holding, read_ruling=files)
            gate.begin_pass(cfg, attempts, stamp(now))
            if with_rulings:
                old_numbers = (40, 5)  # A fresh gate begins with the policy defaults.
            if invariant == "I5-reservations":
                assert gate.reservations == old
        budgets = {aid: expiry for aid, expiry in budgets.items() if expiry > now}
        count_reads = len(reads)
        gate.begin_pass(cfg, attempts, stamp(now))
        assert len(reads) == count_reads + 1
        floor, margin, _, _ = agent_rule(disk_settings(cfg), files, now)
        effective = free - 1.5 * len(budgets)
        if old_numbers != (floor, margin):
            latch = (latch and effective < floor + margin) or effective - 1.5 < floor
        old_numbers = floor, margin
        limit = max(0, int((effective - floor) // 1.5))
        ready = any(klass not in ("attended", "probe") for _, klass in waiting)
        placed = 0
        remaining = []
        for jid, klass in waiting:
            exempt = klass in ("attended", "probe")
            if exempt:
                refused = False
            else:
                effective_now = free - 1.5 * len(budgets)
                refused = (latch and effective_now < floor + margin) or effective_now - 1.5 < floor
                latch = refused
            hold = gate.hold(klass)
            # This independent oracle also kills priority-exemption and
            # hysteresis mutations, even when their placements respect a floor.
            assert bool(hold) == refused
            if hold:
                remaining.append((jid, klass))
                continue
            if invariant == "I4-attended" and klass == "attended":
                assert not hold
            if not exempt:
                aid = f"job-{jid}/a1"
                attempts.append({"attempt_id": aid, "state": "reserved", "kind": "dispatch",
                                 "reserved_at": stamp(now), "finished_at": None})
                gate.reserve(aid, stamp(now))
                budgets[aid] = now + 600
                placed += 1
                if invariant == "I1-floor":
                    assert free * GB - gate.reserved_bytes >= floor * GB
        waiting = remaining
        if invariant == "I2-progress" and ready and effective >= floor + margin + 1.5:
            assert placed >= 1
        if invariant == "I3-pacing":
            assert placed <= limit
        if invariant == "I5-reservations":
            assert gate.reserved_bytes == 1.5 * GB * len(budgets) >= 0
            restarted = DiskAdmission(tmp_path, read_free=read, read_ruling=files)
            restarted.begin_pass(cfg, attempts, stamp(now))
            assert restarted.reservations == gate.reservations


def test_statvfs_uses_available_blocks_and_fragment_size(monkeypatch, tmp_path):
    from subfleet.disk import free_bytes
    monkeypatch.setattr("subfleet.disk.os.statvfs", lambda path: SimpleNamespace(
        f_bavail=123, f_bfree=999, f_frsize=4096, f_bsize=8192))
    assert free_bytes(tmp_path) == 123 * 4096


def test_hysteresis_refuses_between_floor_and_resume(tmp_path):
    free = 39
    gate = DiskAdmission(tmp_path, read_free=lambda path: free * GB)
    gate.begin_pass(POLICY, [], stamp(0))
    assert gate.hold("background")
    free = 44
    gate.begin_pass(POLICY, [], stamp(1))
    assert gate.hold("background")
    free = 45
    gate.begin_pass(POLICY, [], stamp(2))
    assert gate.hold("background") is None


@pytest.mark.parametrize("klass,held", [("priority", True), ("attended", False), ("probe", False)])
def test_exemptions_at_zero_free(tmp_path, klass, held):
    gate = DiskAdmission(tmp_path, read_free=lambda path: 0)
    gate.begin_pass(POLICY, [], stamp(0))
    assert bool(gate.hold(klass)) == held


def test_reservation_subtraction_limits_a_burst(tmp_path):
    gate = DiskAdmission(tmp_path, read_free=lambda path: 46 * GB)
    gate.begin_pass(POLICY, [], stamp(0))
    for index in range(4):
        assert gate.hold("background") is None
        gate.reserve(str(index), stamp(0))
    assert gate.hold("background")
    assert gate.reserved_bytes == 6 * GB


def test_restart_keeps_original_size_and_ttl_after_policy_change(tmp_path):
    import json
    gate = DiskAdmission(tmp_path, read_free=lambda path: 50 * GB)
    gate.begin_pass(POLICY, [], stamp(0))
    row = {"attempt_id": "a", "state": "running", "reserved_at": stamp(0),
           "evidence_json": json.dumps({"disk_reservation": gate.evidence()})}
    changed = {"admission": {"disk": {"enabled": True, "placement_reserve_gb": 3, "reserve_ttl_s": 20}}}
    gate.begin_pass(changed, [row], stamp(599))
    assert gate.reserved_bytes == 1.5 * GB
    gate.begin_pass(changed, [row], stamp(600))
    assert gate.reserved_bytes == 0


@SEQUENCES
@given(actions)
def test_I6_disabled_matches_absent_over_generated_sequences(tmp_path, sequence):
    # Exercise the actual scheduler pass too: apply the disabled gate to the
    # same submitted queue and compare with today's ungated pass model.
    from copy import deepcopy
    from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
    from tests.admission_model import run_pass
    from subfleet.capacity import build_view
    from tests.unit.test_scheduler import lane

    def forbidden(path):
        raise AssertionError("disabled disk admission must not read disk")

    policy = load_policy(DEFAULT_POLICY_PATH)
    policy["caps"]["max_active_attempts"] = len(sequence) % 4 or None
    policy["admission"]["priority_callers"] = ["chosen"]
    absent = deepcopy(policy)
    absent["admission"].pop("disk")
    policy["admission"]["disk"]["enabled"] = False
    gate = DiskAdmission(tmp_path, read_free=forbidden)
    jobs, history, attempts = [], [], []
    now = 0
    for index, (action, value) in enumerate(sequence):
        if action == "submit":
            job = {"job_id": f"job-{index}", "kind": "turn" if value == "attended" else "dispatch",
                   "caller_session": "chosen" if value == "priority" else "ordinary",
                   "tier": "standard", "pinned_model": "astra", "sandbox": "read-only",
                   "created_at": stamp(now)}
            jobs.append(job)
            history.append(job)
        elif action == "end" and attempts:
            attempts.pop(value % len(attempts))
        elif action == "clock":
            now += value
        elif action == "restart":
            gate = DiskAdmission(tmp_path, read_free=forbidden)
        gate.begin_pass(policy, attempts, stamp(now))
        assert all(gate.hold(klass) is None for klass in CLASSES)
        assert gate.reserved_bytes == 0
        view = build_view([lane("codex-1")], [], [], attempts, history, now=stamp(now))
        gated = run_pass(policy, view, [job for job in jobs if gate.hold("attended" if job["kind"] == "turn" else "background") is None])
        original = run_pass(absent, view, jobs)
        assert gated == original
        placed = {row.job_id for row in original.outcomes if row.placed}
        for row in original.outcomes:
            if row.placed:
                gate.reserve(f"{row.job_id}/model", stamp(now))
        assert gate.reserved_bytes == 0
        jobs = [job for job in jobs if job["job_id"] not in placed]
        attempts = original.view["attempts"]


FRACTIONS = st.fractions(min_value=1, max_value=40, max_denominator=9)


def _fractional_gate(tmp_path, free_bytes, floor, reserve, margin):
    gate = DiskAdmission(tmp_path, read_free=lambda path: free_bytes)
    gate.begin_pass({"admission": {"disk": {"enabled": True, "floor_gb": float(floor),
                                            "placement_reserve_gb": float(reserve),
                                            "resume_margin_gb": float(margin), "reserve_ttl_s": 600}}},
                    (), stamp(0))
    return gate


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(floor=FRACTIONS, reserve=FRACTIONS, free=st.integers(0, 400 * GB))
def test_I1_floor_is_exact_in_whole_bytes_for_fractional_settings(tmp_path, floor, reserve, free):
    """C-6.17 I1 (review of #161, P3): with fractional GB settings, every placement
    leaves measured free minus reservations at or above the floor, in whole bytes,
    with no rounding error; and the next placement is refused exactly when it would not."""
    gate = _fractional_gate(tmp_path, free, floor, reserve, 0)
    floor_b, reserve_b = round(float(floor) * GB), round(float(reserve) * GB)
    placed = 0
    while gate.hold("session") is None and placed < 500:
        gate.reserve(f"job/a{placed}", stamp(0))
        placed += 1
        assert free - gate.reserved_bytes >= floor_b
    assert gate.reserved_bytes == placed * reserve_b
    assert placed == max(0, (free - floor_b) // reserve_b)          # I3's bound is reached, never passed


def test_review_witnesses_for_fractional_settings(tmp_path):
    """The two cases the #161 review reproduced with float arithmetic."""
    # I1: floor 48/7, reserve 80/7, free 144 GB. Twelve float reservations undershot the floor.
    gate = _fractional_gate(tmp_path, 144 * GB, 48 / 7, 80 / 7, 0)
    placed = 0
    while gate.hold("session") is None:
        gate.reserve(f"w/a{placed}", stamp(0))
        placed += 1
    assert 144 * GB - gate.reserved_bytes >= round(48 / 7 * GB)
    # I2: floor 17/3, reserve 28/3, margin 0, free 15 GB is exactly the progress threshold.
    gate = _fractional_gate(tmp_path, 15 * GB, 17 / 3, 28 / 3, 0)
    assert gate.hold("session") is None


def test_a_daemon_built_without_init_has_the_rule_off(tmp_path):
    """C-6.17: a Daemon assembled by hand (as gate and legacy-hold unit tests do) has
    no disk state until first use, and then a disabled one: nothing is held or read."""
    from subfleet.daemon import Daemon
    bare = object.__new__(Daemon)
    assert bare._disk.settings["enabled"] is False
    assert bare._disk.hold("session") is None and bare._disk.hold("background") is None
    assert bare._disk_saved_hold is False
    assert bare._disk is bare._disk                                  # one state object, kept
