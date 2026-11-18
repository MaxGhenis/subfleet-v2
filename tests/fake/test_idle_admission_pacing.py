"""D-ADMIT-IDLE1: detached duty cycle and semantic wakes, with a fake clock."""

from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401


class InlinePool:
    def submit(self, function, *args):
        result = Future()
        try:
            result.set_result(function(*args))
        except BaseException as exc:
            result.set_exception(exc)
        return result


@pytest.fixture
def paced(routing_state, monkeypatch):  # noqa: F811
    service, harness = routing_state
    clock = SimpleNamespace(now=1000., duration=.1, placed=0, error=False, during=None,
                            starts=[], ends=[], turns=[])
    origin = datetime(2026, 11, 7, tzinfo=timezone.utc)
    real_time = daemon_module.time
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(
        **{key: getattr(real_time, key) for key in dir(real_time) if key not in ("monotonic", "time")},
        monotonic=lambda: clock.now, time=lambda: origin.timestamp() + clock.now))
    monkeypatch.setattr(daemon_module, "utcnow", lambda: (
        origin + timedelta(seconds=clock.now)).isoformat(timespec="seconds").replace("+00:00", "Z"))
    monkeypatch.setattr(service, "_run_worker", lambda function, *args: function(*args))
    monkeypatch.setattr(service, "_note_admission", lambda *args: None)

    def pass_once(holds, tally, *, kind):
        if kind == "turn":
            clock.turns.append(clock.now)
            return
        clock.starts.append(clock.now)
        clock.now += clock.duration
        if clock.during:
            clock.during()
        if clock.error:
            raise RuntimeError("fixture pass failed")
        tally["placed"] = clock.placed
        clock.ends.append(clock.now)

    monkeypatch.setattr(service, "_admit_pass", pass_once)

    def offer(key="admission"):
        fn = service._admit if key == "admission" else service._admit_turns
        service._schedule(key, fn, paced=True, pool=InlinePool())

    clock.offer = offer
    yield service, harness, clock


@pytest.mark.parametrize("duration", [.01, .8, 5.])
def test_idle_pass_rests_from_its_end_for_at_least_four_durations(paced, duration):
    """I1: even a slow pass occupies at most a fifth of an unchanged idle loop."""
    service, _, clock = paced
    clock.duration = duration
    for _ in range(3):
        clock.offer()
        end = clock.ends[-1]
        rest = max(daemon_module.ADMISSION_IDLE_PACE_S, 4 * duration)
        count = len(clock.starts)
        clock.now = end + rest - .001
        clock.offer()
        assert len(clock.starts) == count
        clock.now = end + rest + .001
    clock.offer()
    assert len(clock.starts) == 4
    for start, end in zip(clock.starts[1:], clock.ends):
        assert start - end >= max(2, 4 * duration)


def test_reservation_keeps_next_tick_and_turns_keep_their_cadence(paced, monkeypatch):
    """I3: a reservation rests for no tick, and the independent turn key never rests."""
    service, _, clock = paced
    clock.placed = 1
    clock.offer()
    clock.now += .05
    clock.offer()
    assert len(clock.starts) == 2
    clock.placed = 0
    clock.offer()
    monkeypatch.setattr(service, "_admit_turns", lambda: clock.turns.append(clock.now))
    for _ in range(5):
        clock.now += .05
        clock.offer("admission:turns")
        clock.offer()
    assert len(clock.turns) == 5
    assert len(clock.starts) == 3


def test_failed_pass_has_only_worker_failure_pacing(paced):
    service, _, clock = paced
    clock.error = True
    clock.offer()
    retry = service._worker_retry_at["admission"]
    assert service._admission_rest_until == 0
    clock.now = retry + .001
    clock.error = False
    clock.offer()
    assert len(clock.starts) == 2


WAKE_SOURCES = ("submit", "retry", "task-release", "lease-release", "closure-add",
                "closure-release", "closure-expiry", "lane-enable", "lane-disable",
                "lane-enroll", "lane-remove", "policy-hash", "pilot-answer")


@pytest.mark.parametrize("source", WAKE_SOURCES)
def test_each_semantic_wake_starts_detached_on_the_next_tick(paced, source):
    """I2: exercise committed source changes, not a direct call to the wake helper."""
    service, harness, clock = paced
    job = service.dispatch("submit", harness.submit_args())["job_id"]
    closure = service.store.add_closure(Closure(
        "codex-1", "account", daemon_module.after(3600), ClosureReason.OPERATOR_HOLD,
        ClockSource.REPORTED, "fixture"))
    service.store.acquire_lease("out:/fixture", "fixture")
    # Expiry is nearer than the long rest made by this slow fake pass.
    expiry = daemon_module.utcnow()
    if source == "closure-expiry":
        expiry = (datetime.fromisoformat(expiry.replace("Z", "+00:00")) + timedelta(seconds=4)).isoformat()
        with service.store.transaction("fixture.expiry") as tx:
            tx.execute("UPDATE closures SET until_at=? WHERE closure_id=?", (expiry, closure))
    if source in ("retry", "task-release"):
        service.store.update_job(job, state="running" if source == "retry" else "waiting",
                                 wait_reason=None if source == "retry" else "task-held")
    if source == "lane-enable":
        service.store.update_lane("codex-1", enabled=False)
    if source == "lane-remove":
        service.store.put_lane(Lane("codex-2", "codex", "fixture:2", Credential("codex", "/fixture/2", "home"),
                                   "/fixture/2", LaneOwner.V2, False))
    clock.duration = 1.
    clock.offer()
    assert not service._admission_ready()
    clock.now += .1
    if source == "submit":
        service.dispatch("submit", harness.submit_args())
    elif source == "retry":
        service.store.update_job(job, state="waiting", wait_reason="transient")
    elif source == "task-release":
        service.store.update_job(job, wait_reason="capacity")
    elif source == "lease-release":
        service.store.release_leases("fixture")
    elif source == "closure-add":
        service.store.add_closure(Closure("codex-1", "model", daemon_module.after(3600),
            ClosureReason.OPERATOR_HOLD, ClockSource.REPORTED, "fixture"))
    elif source == "closure-release":
        with service.store.transaction("lane.released") as tx:
            tx.execute("UPDATE closures SET released_at=? WHERE closure_id=?", (daemon_module.utcnow(), closure))
    elif source == "closure-expiry":
        clock.now = service._admission_closure_due
    elif source in ("lane-enable", "lane-disable"):
        service.store.update_lane("codex-1", enabled=source == "lane-enable")
    elif source == "lane-enroll":
        service.store.put_lane(Lane("codex-2", "codex", "fixture:2", Credential("codex", "/fixture/2", "home"),
                                   "/fixture/2", LaneOwner.V2, False))
    elif source == "lane-remove":
        with service.store.transaction("lane.removed") as tx:
            tx.execute("DELETE FROM lanes WHERE lane_id='codex-2'")
    elif source == "policy-hash":
        service.policy_digest = "changed-policy"
    elif source == "pilot-answer":
        service.policy["admission"]["prove_idle_s"] = 60
        service._record_answer("codex-1", "fixture")
    woke_at = clock.now
    clock.now += .05
    clock.offer()
    assert len(clock.starts) == 2, source
    assert clock.starts[-1] - woke_at == pytest.approx(.05)


def test_reading_and_due_jobs_do_not_end_the_rest(paced):
    """A reading-only change waits at most one rest, and staggered due jobs cannot spin it."""
    from subfleet.contracts import Reading, ReadingLabel
    service, harness, clock = paced
    job = service.dispatch("submit", harness.submit_args())["job_id"]
    clock.offer()
    end = clock.ends[-1]
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .1,
        daemon_module.after(3600), ReadingLabel.PROVIDER, "fixture", daemon_module.utcnow()))
    service.store.update_job(job, next_check_at=daemon_module.utcnow())
    for _ in range(39):
        clock.now += .05
        clock.offer()
    assert len(clock.starts) == 1
    clock.now = end + 2.001
    clock.offer()
    assert len(clock.starts) == 2


def test_a_wake_during_a_pass_survives_its_completion(paced):
    service, harness, clock = paced
    # A different daemon thread commits the submit while this pass is running.
    import threading
    def during():
        thread = threading.Thread(target=lambda: service.dispatch("submit", harness.submit_args()))
        thread.start()
        thread.join(30)
        assert not thread.is_alive()
    clock.during = during
    clock.offer()
    assert service._admission_rest_until == 0
    clock.during = None
    clock.now += .05
    clock.offer()
    assert len(clock.starts) == 2


def test_closure_expiring_during_a_slow_pass_survives_its_completion(paced):
    service, _, clock = paced
    until = (datetime.fromisoformat(daemon_module.utcnow().replace("Z", "+00:00"))
             + timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
    service.store.add_closure(Closure("codex-1", "account", until, ClosureReason.OPERATOR_HOLD,
                                     ClockSource.REPORTED, "fixture"))
    clock.offer()
    clock.now = clock.ends[-1] + 2.001
    clock.duration = 2.
    clock.offer()
    assert len(clock.starts) == 2
    assert service._admission_rest_until == 0
    clock.now += .05
    clock.offer()
    assert len(clock.starts) == 3


def test_closure_expiring_during_the_first_pass_is_not_lost(paced):
    service, _, clock = paced
    until = (datetime.fromisoformat(daemon_module.utcnow().replace("Z", "+00:00"))
             + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    service.store.add_closure(Closure("codex-1", "account", until, ClosureReason.OPERATOR_HOLD,
                                     ClockSource.REPORTED, "fixture"))
    clock.duration = 2.
    clock.offer()
    assert service._admission_rest_until == 0
    clock.now += .05
    clock.offer()
    assert len(clock.starts) == 2


def test_a_malformed_closure_does_not_hide_a_valid_expiry_wake(paced):
    service, _, clock = paced
    now = datetime.fromisoformat(daemon_module.utcnow().replace("Z", "+00:00"))
    until = (now + timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
    broken = (now + timedelta(seconds=2)).isoformat().replace("+00:00", "Zbad")
    service.store.add_closure(Closure("codex-1", "account", until, ClosureReason.OPERATOR_HOLD,
                                     ClockSource.REPORTED, "fixture"))
    service.store.add_closure(Closure("codex-1", "broken", broken, ClosureReason.OPERATOR_HOLD,
                                     ClockSource.REPORTED, "fixture"))
    clock.duration = 1.
    clock.offer()
    assert len(clock.ends) == 1
    assert service._admission_closure_due == pytest.approx(1003.)
    clock.now = 1003.05
    clock.offer()
    assert len(clock.starts) == 2
