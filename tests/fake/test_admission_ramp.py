"""C-6.18: committed placements, fake clocks, durable rows, and the real daemon pass.

R1 bounds each window; R2 empties a free queue on the window clock; R3 keeps
attended work outside that clock; R4 keeps the existing priority/FIFO order;
R5 reconstructs attempts across a restart, including completed retries; R6
compares disabled/absent policy with the frozen pre-change admission method.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from types import MethodType

from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from subfleet import capacity as capacity_module, daemon as daemon_module, scheduler, store as store_module
from subfleet.contracts import Outcome, OutcomeClass
from subfleet.disk import GB, epoch
from tests.machine_guard_reference import LEGACY_GUARD
from tests.fake.test_admission_latency import add_codex_lanes, fleet_daemon, measure, submit_turn
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.unit.test_disk_admission import stamp


@pytest.fixture(autouse=True)
def non_git_workspace(monkeypatch):
    monkeypatch.setattr(daemon_module, "git_head", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon_module, "git_toplevel", lambda *args, **kwargs: None)


def fake_clock(patch, initial=0):
    clock = [initial]

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime.fromisoformat(stamp(clock[0]).replace("Z", "+00:00"))
            return instant.astimezone(tz) if tz is not None else instant.astimezone().replace(tzinfo=None)

    # Routing builds and rechecks its view with datetime.now directly. Keep
    # that clock aligned with the placement ledger and generated readings.
    patch.setattr(daemon_module, "datetime", ClockDatetime)
    # So do a capacity wait's next look (`scheduler.waiting_metadata` without
    # `now`) and the store's own stamps (`Store.add_decision`'s `evaluated_at`,
    # `created_at`). On the wall clock, two runs that straddle a second differ.
    for module in (scheduler, store_module, capacity_module):
        patch.setattr(module, "datetime", ClockDatetime)
    patch.setattr(daemon_module, "utcnow", lambda: stamp(clock[0]))
    patch.setattr(daemon_module, "after", lambda seconds: stamp(clock[0] + seconds))
    patch.setattr(daemon_module, "age", lambda timestamp:
                  epoch(stamp(clock[0])) - epoch(timestamp) if timestamp else 0)
    # These helpers imported the clock functions by name before the patch.
    patch.setattr("tests.fake.test_admission_latency.utcnow", lambda: stamp(clock[0]))
    patch.setattr("tests.fake.test_admission_latency.after", lambda seconds: stamp(clock[0] + seconds))
    return clock


def enable(service, patch, per_minute=12, window_s=60):
    service.policy["admission"]["ramp"] = {
        "enabled": True, "per_minute": per_minute, "window_s": window_s}
    patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))


def save_policy(service):
    """Write the in-memory policy for a restart as an operator's file would hold it.

    The loader adds `_policy_hash` and `_policy_path`, the state root's own path.
    C-11.1 hashes the file's bytes, so writing that metadata back would give each
    temporary root its own policy hash: a fixture difference, not an admission one.
    """
    (service.root / "policy.json").write_text(json.dumps(
        {key: value for key, value in service.policy.items() if not key.startswith("_")}))


def submit(service, harness, **changes):
    return service.dispatch("submit", harness.submit_args(**changes))["job_id"]


def placed(service):
    return service.store.query("SELECT * FROM attempts ORDER BY rowid")


def finish(service, row, now, *, retry=False):
    with service.store.transaction("test.ramp_attempt_ended") as tx:
        tx.execute("UPDATE attempts SET state=?,finished_at=?,outcome_class=? WHERE attempt_id=?",
                   ("failed" if retry else "succeeded", now, "transient" if retry else "ok", row["attempt_id"]))
        tx.execute("UPDATE jobs SET state=?,wait_reason=NULL,next_check_at=NULL WHERE job_id=?",
                   ("queued" if retry else "succeeded", row["job_id"]))
        tx.execute("DELETE FROM leases WHERE holder=?", (row["attempt_id"],))


@pytest.mark.parametrize("ramped", [False, True], ids=["before-56", "after-12"])
def test_disk_reopening_56_jobs_on_10_lanes(state_daemon, monkeypatch, ramped):
    """14:00Z replay: a latched disk hold at 41.5 GB releases at 135.9 GB.

    Before: 56 reservations in one pass. After: 12, 24, 36, 48, 56 at
    0, 60, 120, 180, 240 seconds; no other capacity limit changes the result.
    """
    service, harness = state_daemon
    clock = fake_clock(monkeypatch)
    add_codex_lanes(service, *(f"codex-{n}" for n in range(2, 11)))
    assert len(service.store.list_lanes()) == 10
    assert all(service.policy["caps"][key] is None for key in
               ("max_active_attempts", "max_in_flight_per_lane", "max_in_flight_unmeasured"))
    reading = [41.5]
    monkeypatch.setattr("subfleet.disk.free_bytes", lambda path: int(reading[0] * GB))
    service.policy["admission"]["disk"]["enabled"] = True
    # The incident's queue was already held: its hysteresis needs 45 GB to reopen.
    service._disk.holding = True
    enable(service, monkeypatch)
    service.policy["admission"]["ramp"]["enabled"] = ramped
    jobs = [submit(service, harness, request_id=f"incident-{n}") for n in range(56)]
    service._admit()
    assert placed(service) == []
    assert service._admission["reasons"] == {"disk": 56}
    reading[0] = 135.9
    service._admit()
    if not ramped:
        assert [row["job_id"] for row in placed(service)] == jobs
        return
    assert len(placed(service)) == 12
    assert service._admission["reasons"] == {"ramp": 44}
    for seconds in (1, 30, 59):
        clock[0] = seconds
        service._admit()
        assert len(placed(service)) == 12
    for seconds, count in ((60, 24), (120, 36), (180, 48), (240, 56)):
        clock[0] = seconds
        service._admit()
        assert len(placed(service)) == count
    assert [row["job_id"] for row in placed(service)] == jobs
    assert service._holds == {}


def test_ramp_holds_before_workspace_lease_or_capacity_backoff_and_says_when(state_daemon, monkeypatch):
    service, harness = state_daemon
    clock = fake_clock(monkeypatch)
    enable(service, monkeypatch, per_minute=2)
    for _ in range(2):
        submit(service, harness)
    service._admit()
    held = submit(service, harness)
    before = service.store.get_job(held)
    leases = service.store.list_leases()

    def forbidden(*args, **kwargs):
        raise AssertionError("a ramp hold must precede workspace preparation")

    monkeypatch.setattr(service, "_workspace", forbidden)
    service._admit()
    assert len(placed(service)) == 2
    assert service.store.list_leases() == leases
    after = service.store.get_job(held)
    assert (after["state"], after["wait_reason"], after["next_check_at"]) == (
        before["state"], before["wait_reason"], before["next_check_at"])
    assert held not in service._capacity_waits
    assert not service.store.query("SELECT 1 FROM decisions WHERE job_id=?", (held,))
    hold = service._holds[held]
    assert (hold["reason"], hold["used"], hold["limit"], hold["window_s"], hold["next_slot_at"]) == (
        "ramp", 2, 2, 60, stamp(60))
    phrase = f"placement ramp: 2 of 2 starts used in the last 60 s; next slot at {stamp(60)}"
    assert phrase in service.dispatch("why", {"job_id": held})["text"]
    status = service.dispatch("daemon.status", {})
    assert status["status"].count("placement ramp:") == 1
    assert phrase in status["status"]
    from subfleet.cli import format_status
    assert format_status(status).count("placement ramp:") == 1
    clock[0] = 60
    monkeypatch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
    service._admit()
    assert placed(service)[-1]["job_id"] == held


def test_committed_placements_this_pass_fill_window_and_rows_read_once(state_daemon, monkeypatch):
    """Kills the mutation that omits this pass's reservations from its count."""
    service, harness = state_daemon
    clock = fake_clock(monkeypatch)
    enable(service, monkeypatch, per_minute=3)
    original = service._ramp_rows
    reads = []

    def rows(since, *, read=None):
        reads.append((since, read is not None))
        return original(since, read=read)

    monkeypatch.setattr(service, "_ramp_rows", rows)
    boundary = service._boundary

    def counted_commit(stage, job_id, attempt_id):
        if stage == "reserved":
            # Immediate post-commit publication must include this placement,
            # even before a later job's census could hide a missing reserve().
            assert service._ramp.snapshot["used"] == len(placed(service))
        return boundary(stage, job_id, attempt_id)

    monkeypatch.setattr(service, "_boundary", counted_commit)
    jobs = [submit(service, harness) for _ in range(7)]
    service._admit()
    assert [transaction for _, transaction in reads] == [False, True, True, True]
    assert [row["job_id"] for row in placed(service)] == jobs[:3]
    assert service._ramp.snapshot["used"] == 3
    clock[0] = 59
    service._admit()
    assert [transaction for _, transaction in reads] == [False, True, True, True, False]
    assert len(placed(service)) == 3


def test_rollback_next_pass_counts_future_completed_reservation(state_daemon, monkeypatch):
    """Review R1 reproduction 1: t=100 then a new admission pass at t=90."""
    service, harness = state_daemon
    clock = fake_clock(monkeypatch, 100)
    enable(service, monkeypatch, per_minute=1)
    first = submit(service, harness)
    service._admit()
    finish(service, placed(service)[0], stamp(100))
    clock[0] = 90
    waiting = submit(service, harness)
    service._admit()
    assert [row["job_id"] for row in placed(service)] == [first]
    assert service._holds[waiting]["reason"] == "ramp"
    assert service._holds[waiting]["next_slot_at"] == stamp(160)
    clock[0] = 100
    assert service.dispatch("daemon.status", {})["ramp"]["used"] == 1
    clock[0] = 160
    service._admit()
    assert [row["job_id"] for row in placed(service)] == [first, waiting]


def test_rollback_real_restart_counts_future_durable_reservation(state_daemon, monkeypatch):
    """Review R1 reproduction 2: the actual daemon restarts at t=90."""
    service, harness = state_daemon
    clock = fake_clock(monkeypatch, 100)
    enable(service, monkeypatch, per_minute=1)
    first = submit(service, harness)
    service._admit()
    finish(service, placed(service)[0], stamp(100))
    waiting = submit(service, harness)
    save_policy(service)
    service.close()
    clock[0] = 90
    restarted = daemon_module.Daemon(harness.root)
    try:
        monkeypatch.setattr(restarted, "_workspace", lambda job: (job["workdir"], None, None, []))
        restarted._admit()
        assert [row["job_id"] for row in placed(restarted)] == [first]
        assert restarted.dispatch("daemon.status", {})["ramp"]["used"] == 1
        assert restarted._holds[waiting]["reason"] == "ramp"
        assert restarted._holds[waiting]["next_slot_at"] == stamp(160)
        clock[0] = 100
        assert restarted.dispatch("daemon.status", {})["ramp"]["used"] == 1
        clock[0] = 160
        restarted._admit()
        assert [row["job_id"] for row in placed(restarted)] == [first, waiting]
    finally:
        restarted.close()


def test_rollback_during_workspace_rechecks_committed_budget_without_leases(state_daemon, monkeypatch):
    """Review R1 reproduction 3: check at 160, prepare at 150, budget is spent."""
    service, harness = state_daemon
    clock = fake_clock(monkeypatch, 100)
    enable(service, monkeypatch, per_minute=1)
    first = submit(service, harness)
    service._admit()
    finish(service, placed(service)[0], stamp(100))
    older, newer = [submit(service, harness) for _ in range(2)]
    before_leases = service.store.query("SELECT * FROM leases ORDER BY lease_key")
    prepared = []

    def rollback(job):
        prepared.append(job["job_id"])
        clock[0] = 150
        return job["workdir"], None, None, []

    monkeypatch.setattr(service, "_workspace", rollback)
    clock[0] = 160
    service._admit()
    assert [row["job_id"] for row in placed(service)] == [first]
    assert prepared == [older]  # The failed commit check holds later ordinary jobs too.
    assert service._ramp.snapshot["used"] == 1
    for waiting in (older, newer):
        assert service._holds[waiting]["reason"] == "ramp"
        assert service._holds[waiting]["next_slot_at"] == stamp(160)
        assert service.store.get_job(waiting)["state"] == "queued"
        assert waiting not in service._capacity_waits
        assert service.store.get_job(waiting)["next_check_at"] is None
    assert service.store.query("SELECT * FROM leases ORDER BY lease_key") == before_leases
    assert service._disk.reservations == {}
    clock[0] = 160
    enable(service, monkeypatch, per_minute=1)
    service._admit()
    assert [row["job_id"] for row in placed(service)] == [first, older]


def test_priority_burst_is_exempt_from_holds_but_slows_following_jobs(state_daemon, monkeypatch):
    service, harness = state_daemon
    clock = fake_clock(monkeypatch)
    enable(service, monkeypatch, per_minute=2)
    service.policy["admission"]["priority_callers"] = ["CHOSEN"]
    background = submit(service, harness, caller_session="ordinary")
    priority = [submit(service, harness, caller_session="chosen") for _ in range(3)]
    service._admit()
    assert [row["job_id"] for row in placed(service)] == priority
    assert service._ramp.snapshot["used"] == 3
    assert service._holds[background]["reason"] == "ramp"
    clock[0] = 60
    service._admit()
    assert [row["job_id"] for row in placed(service)] == priority + [background]


def test_window_expiring_mid_pass_does_not_let_newer_job_pass_older(state_daemon, monkeypatch):
    """R4: the first ramp hold stops ordinary placements for that whole pass."""
    service, harness = state_daemon
    clock = fake_clock(monkeypatch)
    enable(service, monkeypatch, per_minute=1)
    first = submit(service, harness)
    service._admit()
    older, newer = [submit(service, harness) for _ in range(2)]
    clock[0] = 59
    original = service._ramp.hold

    def hold_then_expire(klass, now):
        hold = original(klass, now)
        if hold:
            clock[0] = 60
        return hold

    monkeypatch.setattr(service._ramp, "hold", hold_then_expire)
    service._admit()
    assert [row["job_id"] for row in placed(service)] == [first]
    assert service._holds[older]["reason"] == service._holds[newer]["reason"] == "ramp"
    service._admit()
    assert [row["job_id"] for row in placed(service)] == [first, older]
    clock[0] = 120
    service._admit()
    assert [row["job_id"] for row in placed(service)] == [first, older, newer]


def test_gate_review_counts_holds_and_reconstructs_completed_start_after_restart(state_daemon, monkeypatch):
    """A detached gate review consumes the same window as dispatch work."""
    service, harness = state_daemon
    clock = fake_clock(monkeypatch)
    enable(service, monkeypatch, per_minute=1)

    def review(gate_id):
        return submit(service, harness, kind="gate-review", isolated_review=True,
                      review_root=str(harness.workdir), round_lease=f"gate:{gate_id}:round:1")

    first = review("ramp-first")
    service._admit()
    assert [row["job_id"] for row in placed(service)] == [first]
    assert service.store.get_job(first)["kind"] == "gate-review"
    assert service._ramp.snapshot["used"] == 1
    finish(service, placed(service)[0], stamp(1))
    clock[0] = 1
    waiting_review = review("ramp-second")
    waiting_dispatch = submit(service, harness)
    service._admit()
    assert len(placed(service)) == 1
    assert service._holds[waiting_review]["reason"] == "ramp"
    assert service._holds[waiting_dispatch]["reason"] == "ramp"
    assert waiting_review not in service._capacity_waits
    assert service.store.one("SELECT 1 FROM leases WHERE lease_key='gate:ramp-second:round:1'") is None
    save_policy(service)
    service.close()
    clock[0] = 2
    restarted = daemon_module.Daemon(harness.root)
    try:
        monkeypatch.setattr(restarted, "_workspace", lambda job: (job["workdir"], None, None, []))
        assert restarted.dispatch("daemon.status", {})["ramp"]["used"] == 1
        restarted._admit()
        assert len(placed(restarted)) == 1
        assert restarted._holds[waiting_review]["next_slot_at"] == stamp(60)
        clock[0] = 60
        restarted._admit()
        assert [row["job_id"] for row in placed(restarted)] == [first, waiting_review]
        assert restarted._holds[waiting_dispatch]["reason"] == "ramp"
        assert restarted._ramp.snapshot["used"] == 1
        clock[0] = 120
        restarted._admit()
        assert [row["job_id"] for row in placed(restarted)] == [first, waiting_review, waiting_dispatch]
    finally:
        restarted.close()


def test_real_restart_counts_completed_attempt_and_retry_until_each_expires(state_daemon, monkeypatch):
    """R5: two persisted starts leave zero slots at t=2, one at t=60."""
    service, harness = state_daemon
    clock = fake_clock(monkeypatch)
    enable(service, monkeypatch, per_minute=2)
    job = submit(service, harness)
    service._admit()
    first = placed(service)[0]
    finish(service, first, stamp(1), retry=True)
    clock[0] = 1
    service._admit()
    assert len(placed(service)) == 2 and placed(service)[1]["seq"] == 2
    finish(service, placed(service)[1], stamp(2))
    clock[0] = 2
    waiting = submit(service, harness)
    service._admit()
    assert service._holds[waiting]["reason"] == "ramp"
    save_policy(service)
    service.close()
    restarted = daemon_module.Daemon(harness.root)
    try:
        monkeypatch.setattr(restarted, "_workspace", lambda job: (job["workdir"], None, None, []))
        status = restarted.dispatch("daemon.status", {})
        assert status["ramp"]["used"] == 2
        assert status["status"].count("placement ramp:") == 1
        restarted._admit()
        assert len(placed(restarted)) == 2
        assert restarted._holds[waiting]["used"] == 2
        assert restarted._holds[waiting]["next_slot_at"] == stamp(60)
        clock[0] = 60
        restarted._admit()
        assert len(placed(restarted)) == 3
        assert placed(restarted)[-1]["job_id"] == waiting
        assert restarted._ramp.snapshot["used"] == 2
    finally:
        restarted.close()


def test_attended_turn_never_reads_waits_on_or_counts_against_ramp(state_daemon, monkeypatch):
    """R3, including durable reconstruction: an attended row leaves one free slot."""
    service, harness = state_daemon
    fake_clock(monkeypatch)
    enable(service, monkeypatch, per_minute=1)
    measure(service, "codex-1")
    monkeypatch.setattr(service.conversations, "admission_hold", lambda job: None)
    turn = submit_turn(service, harness, 1)

    def forbidden(*args, **kwargs):
        raise AssertionError("attended admission must never touch the placement ramp")

    with monkeypatch.context() as scoped:
        scoped.setattr(service, "_ramp_rows", forbidden)
        scoped.setattr(service._ramp, "hold", forbidden)
        scoped.setattr(service._ramp, "reserve", forbidden)
        service._admit_turns()
    assert [row["job_id"] for row in placed(service)] == [turn]
    assert turn not in service._holds
    background = submit(service, harness)
    service._admit()
    assert [row["job_id"] for row in placed(service)] == [turn, background]
    assert service._ramp.snapshot["used"] == 1


def test_timer_probe_never_reads_waits_on_or_counts_against_ramp(state_daemon, monkeypatch):
    service, harness = state_daemon
    fake_clock(monkeypatch)
    enable(service, monkeypatch, per_minute=1)
    outcome = Outcome(OutcomeClass.OK, "probe admitted", {"rc": 0, "signal": None})
    monkeypatch.setattr(service, "_execute_probe", lambda *args, **kwargs: outcome)

    def forbidden(*args, **kwargs):
        raise AssertionError("a timer probe must never touch the placement ramp")

    with monkeypatch.context() as scoped:
        scoped.setattr(service, "_ramp_rows", forbidden)
        scoped.setattr(service._ramp, "hold", forbidden)
        scoped.setattr(service._ramp, "reserve", forbidden)
        assert service._timer_turn(service.store.get_lane("codex-1"), "keepalive", "probe:timer:ramp",
                                   cancel=threading.Event(), deadline=time.monotonic() + 60).cls == OutcomeClass.OK
    assert placed(service) == []
    job = submit(service, harness)
    service._admit()
    assert [row["job_id"] for row in placed(service)] == [job]
    assert service._ramp.snapshot["used"] == 1


def test_control_tick_reopens_next_slot_without_external_event(state_daemon, monkeypatch):
    """R2 uses the existing periodic _control/_schedule admission timer."""
    service, harness = state_daemon
    clock = fake_clock(monkeypatch)
    enable(service, monkeypatch, per_minute=1)
    jobs = [submit(service, harness) for _ in range(2)]
    service._admit()
    assert service._holds[jobs[1]]["next_slot_at"] == stamp(60)
    clock[0] = 59
    service._recovery_complete.set()
    monkeypatch.setattr(service, "_has_work", lambda row: False)
    monkeypatch.setattr(service.timers, "tick", lambda: None)
    monkeypatch.setattr(service, "_notify", lambda: None)
    passes = []

    def schedule(key, fn, *args, **kwargs):
        if key == "admission":
            passes.append(clock[0])
            fn(*args)

    def wait(seconds):
        assert seconds == service.tick_s
        if clock[0] == 59:
            clock[0] = 60
        else:
            service.stopping.set()
        return False

    monkeypatch.setattr(service, "_schedule", schedule)
    monkeypatch.setattr(service.stopping, "wait", wait)
    service._control()
    assert passes == [59, 60]
    assert [row["job_id"] for row in placed(service)] == jobs


DIFFERENTIAL = settings(max_examples=30, deadline=None, derandomize=True,
                        suppress_health_check=[HealthCheck.too_slow])
QUEUES = st.lists(st.tuples(st.sampled_from(["ordinary", "chosen", "live"]),
                            st.sampled_from(["trivial", "standard", "hard"]),
                            st.sampled_from([None, "codex-1", "codex-2", "codex-3"])), max_size=8)


@settings(max_examples=30, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow])
@example(per_minute=1, steps=[(0, 0, False), (60, -10, False), (-60, 0, True)])
@given(per_minute=st.integers(1, 3), steps=st.lists(
    st.tuples(st.integers(-180, 180), st.integers(-180, 180), st.booleans()),
    min_size=1, max_size=12))
def test_generated_R1_arbitrary_clocks_at_check_and_commit(tmp_path_factory, per_minute, steps):
    """R1 at reservation instants, with real elapsed time independent of backwards UTC.

    Positive clock advances consume real elapsed time; negative corrections do
    not. Exercise both pass-entry and workspace clocks, plus actual restarts.
    The clock never advances inside a reserving transaction here, so each
    commit's elapsed time is its reservation's: C-6.18 bounds reservation
    instants, not commit instants (review of #180 r2).
    """
    with fleet_daemon(tmp_path_factory.mktemp("ramp-clock") / "state") as (service, harness, patch):
        clock = fake_clock(patch, 100)
        enable(service, patch, per_minute=per_minute)
        service.policy["caps"]["reading_ttl_s"] = 86400
        for lane in service.store.list_lanes():
            measure(service, lane.lane_id)
        active, elapsed, commits = service, [0], []

        def advance(delta):
            clock[0] += delta
            elapsed[0] += max(0, delta)

        def check_commit(stage, job_id, attempt_id=None):
            if stage != "reserved":
                return
            row = active.store.one("SELECT reserved_at FROM attempts WHERE attempt_id=?", (attempt_id,))
            wall = epoch(row["reserved_at"]) - epoch(stamp(0))
            commits.append((wall, elapsed[0]))
            assert sum(real > elapsed[0] - 60 for _, real in commits) <= per_minute
            assert sum(when > wall - 60 for when, _ in commits) <= per_minute

        try:
            for index, (before_check, preparation, restart) in enumerate(steps):
                advance(before_check)
                if restart:
                    save_policy(active)
                    active.close()
                    active = daemon_module.Daemon(harness.root)
                rolled = [False]

                def workspace(job):
                    if not rolled[0]:
                        advance(preparation)
                        rolled[0] = True
                    return job["workdir"], None, None, []

                patch.setattr(active, "_workspace", workspace)
                patch.setattr(active, "_boundary", check_commit)
                submit(active, harness, request_id=f"clock-{index}")
                before = len(placed(active))
                active._admit()
                rows = placed(active)
                assert len(commits) == len(rows)
                for row in rows[before:]:
                    finish(active, row, stamp(clock[0]))
        finally:
            if active is not service:
                active.close()


@settings(max_examples=20, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow])
@given(queue=QUEUES.filter(bool), per_minute=st.integers(1, 6), initial=st.integers(0, 3600),
       reopen=st.booleans(), restart=st.booleans())
def test_generated_enabled_daemon_windows_liveness_order_and_restart(tmp_path_factory, queue, per_minute,
                                                                    initial, reopen, restart):
    """R1/R2/R4/R5 on the daemon, with mixed tiers and caller classes."""
    with fleet_daemon(tmp_path_factory.mktemp("ramp-enabled") / "state") as (service, harness, patch):
        clock = fake_clock(patch, initial)
        enable(service, patch, per_minute=per_minute)
        # Free capacity remains measured for every generated admission window.
        service.policy["caps"]["reading_ttl_s"] = 86400
        live = scheduler.Liveness(sessions=frozenset({"live"}), jobs=frozenset())
        patch.setattr(service, "_liveness", lambda jobs: live)
        service.policy["admission"]["priority_callers"] = ["chosen"]
        reading = [39 if reopen else 1000]
        patch.setattr("subfleet.disk.free_bytes", lambda path: int(reading[0] * GB))
        service.policy["admission"]["disk"]["enabled"] = reopen
        for lane in service.store.list_lanes():
            measure(service, lane.lane_id)
        jobs = [submit(service, harness, request_id=f"generated-{n}", caller_session=caller,
                       tier=tier, pinned_lane=lane) for n, (caller, tier, lane) in enumerate(queue)]
        rows = [service.store.get_job(job) for job in jobs]
        expected = [job["job_id"] for job in scheduler.ordered_jobs(service.policy, rows, live)]
        priority = {row["job_id"] for row in rows if row["caller_session"] == "chosen"}
        if reopen:
            service._admit()
            assert placed(service) == []
            assert set(service._holds) == set(jobs)
            reading[0] = 1000
        service._admit()
        first_count = len(placed(service))
        active = service
        if restart:
            save_policy(service)
            service.close()
            active = daemon_module.Daemon(harness.root)
            patch.setattr(active, "_workspace", lambda job: (job["workdir"], None, None, []))
            patch.setattr(active, "_liveness", lambda jobs: live)
            clock[0] = initial + 30
            active._admit()
            assert len(placed(active)) == first_count
        try:
            for minute in range(1, math.ceil(len(jobs) / per_minute) + 1):
                clock[0] = initial + minute * 60
                active._admit()
                if len(placed(active)) == len(jobs):
                    break
            attempts = placed(active)
            assert len(attempts) == len(jobs)  # R2: within ceil(n/rate) minutes plus one pass.
            assert [row["job_id"] for row in attempts] == expected  # R4.
            ordinary_starts = [epoch(row["reserved_at"]) for row in attempts if row["job_id"] not in priority]
            for end_at in ordinary_starts:
                assert sum(end_at - 60 < started <= end_at for started in ordinary_starts) <= per_minute  # R1.
        finally:
            if restart:
                active.close()


def snapshot(service, jobs):
    """Compare every hold, persisted decision, and placement after naming jobs.

    Policy hashes stay raw: both runs load the same policy bytes (`save_policy`).
    """
    aliases = {job: f"job-{n}" for n, job in enumerate(jobs)}

    def normalize(value):
        text = json.dumps(value, sort_keys=True)
        # Generated ids can be prefixes of each other (astra, astra-1, astra-10).
        # Replace the longest first so naming one job cannot alter another's id.
        for job, alias in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
            text = text.replace(job, alias)
        text = text.replace(str(service.root), "<state>")
        # Canonicalize again after naming jobs: the original ids sorted the
        # hold-map keys, and that order can differ across the two daemon runs.
        return json.dumps(json.loads(text), sort_keys=True)

    return normalize({
        "holds": service._holds,
        "jobs": [{key: service.store.get_job(job)[key] for key in
                  ("job_id", "state", "wait_reason", "next_check_at", "started_at")}
                 for job in jobs],
        "attempts": service.store.query("SELECT attempt_id,job_id,seq,lane_id,model_requested,state,"
                                        "reserved_at,evidence_json FROM attempts ORDER BY rowid"),
        "decisions": service.store.query("SELECT job_id,attempt_id,evaluated_at,policy_hash,decision_json "
                                         "FROM decisions ORDER BY decision_id"),
    })


def first_difference(actual, previous, path="passes"):
    """Name the first differing field; inspect nested JSON only for diagnostics."""
    if actual == previous:
        return None
    if isinstance(actual, dict) and isinstance(previous, dict):
        for key in sorted(actual.keys() | previous.keys()):
            if key not in actual or key not in previous:
                return f"{path}.{key}: present only in {'current' if key in actual else 'previous'}"
            difference = first_difference(actual[key], previous[key], f"{path}.{key}")
            if difference:
                return difference
    if isinstance(actual, list) and isinstance(previous, list):
        if len(actual) != len(previous):
            return f"{path}: current length {len(actual)} != previous length {len(previous)}"
        for index, (now, before) in enumerate(zip(actual, previous)):
            difference = first_difference(now, before, f"{path}[{index}]")
            if difference:
                return difference
    if isinstance(actual, str) and isinstance(previous, str):
        try:
            now, before = json.loads(actual), json.loads(previous)
        except (ValueError, TypeError):
            pass
        else:
            if isinstance(now, (dict, list)) and isinstance(before, type(now)):
                difference = first_difference(now, before, f"{path} (JSON)")
                if difference:
                    return difference
    return f"{path}: current {repr(actual)[:240]} != previous {repr(previous)[:240]}"


@DIFFERENTIAL
@example(queue=[("ordinary", "trivial", "codex-1")], enabled=False, cap=None,
         initial=180, advance=0, gate=None, restart=False)
@example(queue=[("ordinary", "trivial", "codex-1")], enabled=False, cap=None,
         initial=0, advance=0, gate=None, restart=False)
@example(queue=[("ordinary", "hard", "codex-1")], enabled=False, cap=None,
         initial=2910, advance=58, gate="disk", restart=False)
@example(queue=[("ordinary", "trivial", None)] * 3, enabled=False, cap=None,
         initial=36, advance=0, gate="disk", restart=True)
@given(queue=QUEUES, enabled=st.sampled_from([False, None]), cap=st.sampled_from([None, 1, 3]),
       initial=st.integers(0, 3600), advance=st.integers(0, 120),
       gate=st.sampled_from([None, "disk", "machine"]), restart=st.booleans())
def test_disabled_absent_matches_frozen_pre_change_pass(tmp_path_factory, queue, enabled, cap,
                                                       initial, advance, gate, restart):
    """R6: generated queues, clocks, restarts and reopening disk/machine gates."""
    outcomes = []
    for previous in (False, True):
        with fleet_daemon(tmp_path_factory.mktemp("ramp-differential") / "state") as (service, harness, patch):
            clock = fake_clock(patch, initial)
            patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
            patch.setattr(service, "_liveness", lambda jobs: scheduler.Liveness(
                sessions=frozenset({"live"}), jobs=frozenset()))
            patch.setattr(service.conversations, "admission_hold", lambda job: None)
            service.policy["caps"]["max_active_attempts"] = cap
            service.policy["caps"]["reading_ttl_s"] = 86400
            service.policy["admission"]["priority_callers"] = ["chosen"]
            if enabled is None:
                service.policy["admission"].pop("ramp", None)
            else:
                service.policy["admission"]["ramp"] = {"enabled": False, "per_minute": 1, "window_s": 120}
            reading = [39]
            patch.setattr("subfleet.disk.free_bytes", lambda path: int(reading[0] * GB))
            machine = {"load1": 400, "load5": 400, "cpus": 18, "memory_pressure": 4}
            patch.setattr("subfleet.machine.read", lambda: dict(machine))
            if gate == "disk":
                service.policy["admission"]["disk"]["enabled"] = True
            elif gate == "machine":
                service.policy["admission"]["machine_guard"] = copy.deepcopy(LEGACY_GUARD)
            for lane in service.store.list_lanes():
                measure(service, lane.lane_id)
            jobs = [submit(service, harness, request_id=f"differential-{n}", caller_session=caller,
                           tier=tier, pinned_lane=lane) for n, (caller, tier, lane) in enumerate(queue)]

            def forbid(*args, **kwargs):
                raise AssertionError("disabled/absent ramp must not read attempts")

            def install(target):
                patch.setattr(target, "_workspace", lambda job: (job["workdir"], None, None, []))
                patch.setattr(target, "_liveness", lambda jobs: scheduler.Liveness(
                    sessions=frozenset({"live"}), jobs=frozenset()))
                patch.setattr(target, "_ramp_rows", forbid)
                if previous:
                    namespace = vars(daemon_module).copy()
                    source = Path(__file__).parents[1] / "fixtures" / "admission_ramp_before.py"
                    exec(compile(source.read_text(), str(source), "exec"), namespace)
                    patch.setattr(target, "_admit_pass", MethodType(namespace["_admit_pass"], target))

            checked = [0]

            def file_hashed(target):
                # C-11.1: each new decision names the hash of the loaded file's
                # exact bytes, the loader's own definition before and after C-6.18.
                digest = hashlib.sha256((target.root / "policy.json").read_bytes()).hexdigest()
                assert target.policy_digest == target.policy["_policy_hash"] == digest
                rows = target.store.query("SELECT policy_hash,evaluated_at FROM decisions ORDER BY decision_id")
                assert {row["policy_hash"] for row in rows[checked[0]:]} <= {digest}
                # Every decision this pass recorded, the store's included, reads
                # the fake clock: a wall-clock stamp fails here, not only when
                # the two runs happen to straddle a second.
                assert {row["evaluated_at"] for row in rows[checked[0]:]} <= {stamp(clock[0])}
                checked[0] = len(rows)

            install(service)
            observed = []
            service._admit()
            file_hashed(service)
            observed.append(snapshot(service, jobs))
            if restart:
                save_policy(service)
                service.close()
                active = daemon_module.Daemon(harness.root)
                install(active)
            else:
                active = service
            try:
                reading[0] = 1000
                machine.update(load1=0, load5=0, memory_pressure=1)
                clock[0] += advance
                active._admit()
                file_hashed(active)
                observed.append(snapshot(active, jobs))
                clock[0] += 60
                active._admit()
                file_hashed(active)
                observed.append(snapshot(active, jobs))
                outcomes.append(observed)
            finally:
                if restart:
                    active.close()
    assert outcomes[0] == outcomes[1], first_difference(
        [json.loads(value) for value in outcomes[0]], [json.loads(value) for value in outcomes[1]])


@pytest.mark.parametrize("enabled", [None, False], ids=["absent", "disabled"])
def test_status_omits_ramp_immediately_after_policy_disable_with_cached_full_gate(
        state_daemon, monkeypatch, enabled):
    service, harness = state_daemon
    fake_clock(monkeypatch)
    enable(service, monkeypatch, per_minute=1)
    submit(service, harness)
    service._admit()
    assert service._ramp.snapshot["enabled"] is True
    assert service._ramp.snapshot["holding"] is True
    assert service._ramp.snapshot["used"] == 1

    if enabled is None:
        service.policy["admission"].pop("ramp")
    else:
        service.policy["admission"]["ramp"]["enabled"] = False

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled status must not read or count placement ramp starts")

    monkeypatch.setattr(service._ramp, "status", forbidden)
    monkeypatch.setattr(service, "_ramp_rows", forbidden)
    status = service.dispatch("daemon.status", {})
    assert "ramp" not in status
    assert "placement ramp:" not in status["status"]
    from subfleet.cli import format_status
    assert "placement ramp:" not in format_status(status)
