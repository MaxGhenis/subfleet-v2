"""D-ADMIT-IDLE1: event-only capacity waits and changes that end them."""

from datetime import datetime, timedelta, timezone

import pytest

from subfleet import daemon as daemon_module, scheduler
from subfleet.contracts import ClockSource, Closure, ClosureReason, Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from tests.fake.test_capacity_wait_backoff import backoff, make_due, submit
from tests.fake.test_routing_end_to_end import claude_lane, routing_state  # noqa: F401


@pytest.fixture
def clock(monkeypatch):
    instant = [datetime.now(timezone.utc).replace(microsecond=0)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant[0].astimezone(tz) if tz else instant[0].replace(tzinfo=None)

    monkeypatch.setattr(daemon_module, "datetime", Clock)
    monkeypatch.setattr(scheduler, "datetime", Clock)

    def advance(seconds):
        instant[0] += timedelta(seconds=seconds)

    return advance


@pytest.fixture
def held_fleet(routing_state, clock):  # noqa: F811
    service, harness = routing_state
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    closure = service.store.add_closure(Closure("codex-1", "account", after(7200),
                                               ClosureReason.OPERATOR_HOLD, ClockSource.REPORTED, "fixture"))
    return service, harness, closure, clock


def release(service, closure):
    with service.store.transaction("fixture.release") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE closure_id=?", (utcnow(), closure))


def long_wait(service, harness, **changes):
    job = submit(service, harness, pinned_model="terra", **changes)
    service._admit()
    service._capacity_waits[job] = {**service._capacity_waits[job], "rechecks": 9}
    make_due(service, job)
    service._admit()
    return job


@pytest.mark.parametrize("reason,event_only", [
    ("disabled", True), ("needs a shell: terra runs workspace-write jobs without one", True),
    ("excluded", False), ("desktop", False), ("config-dir", False), ("owner-v1", False),
    ("identity-mismatch", False), ("credential-refused", False), ("no-slot", False),
    ("below-floor", False), ("reserve:fable:unmeasured", False), ("reserve:fable:reserved", False),
    ("reserve:fable:probe-required", False), ("credential-latched", False),
    ("auth-dead", False), ("provider-cap", False), ("fleet-full", False),
    ("parent-cap", False), ("slot-kept", False), ("provider-slot-kept", False),
    ("unknown", False), ("no-lanes", False), ("future-rejection", False), (None, False),
])
def test_c6_10_every_rejection_reason_has_a_conservative_classification(reason, event_only):
    assert scheduler.capacity_reason_event_only(reason) is event_only


@pytest.mark.parametrize("closure_reason", list(ClosureReason))
def test_c6_10_only_an_operator_closure_is_event_only(closure_reason):
    closure = {"lane_id": "codex-1", "scope": "account", "until_at": "2030-01-01T00:00:00Z",
               "reason": closure_reason.value}
    reason = "closed:account:2030-01-01T00:00:00Z"
    assert scheduler.capacity_reason_event_only(reason, closures=[closure], lane_id="codex-1") is (
        closure_reason == ClosureReason.OPERATOR_HOLD)
    assert scheduler.capacity_reason_event_only(reason) is False


def test_c6_10_every_lane_and_every_reason_must_be_event_only():
    def verdict(rows):
        return {"chosen_lane": None, "evaluations": [{"rejections": rows, "candidates": []}]}

    disabled = {"lane_id": "c", "reasons": ["disabled"]}
    assert scheduler.capacity_recheck_ceiling(verdict([disabled])) == 600
    for reason in ("no-slot", "below-floor", "future-rejection"):
        mixed = {"lane_id": "c", "reasons": ["disabled", reason]}
        assert scheduler.capacity_recheck_ceiling(verdict([mixed])) == 30
        assert scheduler.capacity_recheck_ceiling(verdict([disabled, {"reasons": [reason]}])) == 30
    assert scheduler.capacity_recheck_ceiling(verdict([])) == 30
    assert scheduler.capacity_recheck_ceiling({"chosen_lane": "c", "evaluations": []}) == 30


def test_c6_10_a_provider_limit_cannot_borrow_an_operator_holds_deadline():
    closure = {"lane_id": "c", "scope": "account", "until_at": "2030-01-01T00:00:00Z",
               "reason": "operator-hold"}
    provider = {**closure, "reason": "provider-limit"}
    assert not scheduler.capacity_reason_event_only("closed:account:2030-01-01T00:00:00Z",
                                                    closures=[closure, provider], lane_id="c")


def test_c6_10_unchanged_operator_wait_reaches_600_seconds(held_fleet):
    service, harness, _, _ = held_fleet
    job = long_wait(service, harness)
    assert backoff(service, job) == 600


@pytest.mark.parametrize("reason", [ClosureReason.PROVIDER_LIMIT, ClosureReason.CREDITS,
                                    ClosureReason.AUTH_DEAD, ClosureReason.COOLDOWN])
def test_c6_10_reading_or_time_verdicts_keep_the_30_second_ceiling(held_fleet, reason):
    service, harness, closure, _ = held_fleet
    with service.store.transaction("fixture.reason") as tx:
        tx.execute("UPDATE closures SET reason=? WHERE closure_id=?", (reason.value, closure))
    job = long_wait(service, harness)
    assert backoff(service, job) == 30


@pytest.mark.parametrize("change", ["released", "expired", "enabled", "disabled", "enrolled", "removed", "policy"])
def test_c6_10_capacity_events_bring_every_eligible_wait_forward(held_fleet, monkeypatch, change):
    service, harness, closure, advance = held_fleet
    if change == "removed":
        service.store.put_lane(claude_lane("unused-lane"))
    jobs = [submit(service, harness, pinned_model="terra") for _ in range(3)]
    service._admit()
    probe = submit(service, harness, pinned_model="astra")
    service.store.update_job(probe, state="waiting", wait_reason="capacity", next_check_at=after(8000))
    service._capacity_wait(probe, "probe-wait:x", {"reason": "probe-pending"}, expedite=False)
    for job in jobs:
        service.store.update_job(job, next_check_at=after(8000))
    looked = []
    real = service._workspace
    monkeypatch.setattr(service, "_workspace", lambda job: looked.append(job["job_id"]) or real(job))
    if change == "released":
        release(service, closure)
    elif change == "expired":
        advance(7201)
    elif change in ("enabled", "disabled"):
        service.store.update_lane("codex-1", enabled=change == "disabled")
        service._admit()  # establish the opposite state without spending any future clock
        looked.clear()
        for job in jobs:
            service.store.update_job(job, next_check_at=after(8000))
        service.store.update_lane("codex-1", enabled=change == "enabled")
    elif change == "enrolled":
        service.store.put_lane(claude_lane("new-lane"))
    elif change == "removed":
        with service.store.transaction("fixture.remove") as tx:
            tx.execute("DELETE FROM lanes WHERE lane_id='unused-lane'")
    else:
        service.policy_digest = "changed-policy"
    service._admit()
    assert looked == jobs
    assert probe not in looked  # an inconclusive probe keeps its own clock


def test_c6_10_no_change_never_brings_waits_forward(held_fleet, monkeypatch):
    service, harness, _, _ = held_fleet
    job = long_wait(service, harness)
    checked = service._capacity_waits[job]["checked_at"]
    monkeypatch.setattr(service, "_workspace", lambda job: pytest.fail("unchanged capacity was looked at"))
    service._admit()
    assert service._capacity_waits[job]["checked_at"] == checked


def test_c6_10_a_failed_pass_keeps_capacity_news_for_every_wait_on_retry(held_fleet, monkeypatch):
    import sqlite3

    service, harness, closure, _ = held_fleet
    jobs = [submit(service, harness, pinned_model="terra") for _ in range(3)]
    service._admit()
    for job in jobs:
        service._capacity_waits[job] = {**service._capacity_waits[job], "rechecks": 9}
        make_due(service, job)
    service._admit()
    assert all(backoff(service, job) == 600 for job in jobs)
    release(service, closure)
    real = service._workspace

    def fail_before_every_job_is_reached(job):
        if job["job_id"] == jobs[1]:
            raise sqlite3.OperationalError("fixture store unavailable")
        return real(job)

    with monkeypatch.context() as patch:
        patch.setattr(service, "_workspace", fail_before_every_job_is_reached)
        with pytest.raises(sqlite3.OperationalError, match="fixture store unavailable"):
            service._admit()
    assert service.store.list_attempts(jobs[0])
    assert not service.store.list_attempts(jobs[1]) and not service.store.list_attempts(jobs[2])
    service._admit()
    assert all([row["state"] for row in service.store.list_attempts(job)] == ["reserved"] for job in jobs)
    assert not service._capacity_change_retry


@pytest.mark.parametrize("condition", ["closure", "lane"])
def test_c6_10_a_condition_seen_only_after_the_census_still_brings_waits_forward(held_fleet, monkeypatch,
                                                                               condition):
    """A route can observe a condition absent from both consecutive censuses."""
    service, harness, original_closure, _ = held_fleet
    release(service, original_closure)
    census = service._capacity_change_news
    inserted = []

    def change_after_census(kind):
        changed = census(kind)
        if kind == "detached" and not inserted:
            if condition == "closure":
                inserted.append(service.store.add_closure(Closure(
                    "codex-1", "account", after(7200), ClosureReason.OPERATOR_HOLD,
                    ClockSource.REPORTED, "fixture")))
            else:
                service.store.update_lane("codex-1", enabled=False)
                inserted.append("codex-1")
        return changed

    monkeypatch.setattr(service, "_capacity_change_news", change_after_census)
    job = submit(service, harness, pinned_model="terra")
    service._admit()
    assert service.store.get_job(job)["state"] == "waiting"
    service.store.update_job(job, next_check_at=after(8000))
    if condition == "closure":
        release(service, inserted[0])
    else:
        service.store.update_lane("codex-1", enabled=True)
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(job)] == ["reserved"]


@pytest.mark.parametrize("unprepared", [False, True])
def test_c6_10_known_operator_reset_still_beats_the_long_clock(held_fleet, unprepared):
    service, harness, closure, _ = held_fleet
    if unprepared:
        from tests.unit.test_salvage import git
        git(harness.workdir, "init", "-b", "task/clock")
        git(harness.workdir, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--allow-empty", "-m", "baseline")
    until = after(123)
    with service.store.transaction("fixture.until") as tx:
        tx.execute("UPDATE closures SET until_at=? WHERE closure_id=?", (until, closure))
    changes = {"sandbox": "workspace-write"} if unprepared else {}
    job = long_wait(service, harness, **changes)
    assert service.store.get_job(job)["next_check_at"] == until


def test_c6_10_long_wait_commits_at_most_six_times_per_hour_per_job(held_fleet):
    service, harness, _, advance = held_fleet
    jobs = [submit(service, harness, pinned_model="terra") for _ in range(4)]
    service._admit()
    for job in jobs:
        service._capacity_waits[job] = {**service._capacity_waits[job], "rechecks": 9}
        make_due(service, job)
    service._admit()
    before = service.store.one("SELECT count(*) n FROM events")["n"]
    for _ in range(360):
        advance(10)
        service._admit()
    committed = service.store.one("SELECT count(*) n FROM events")["n"] - before
    assert committed == len(jobs) * 3600 / 600


@pytest.mark.parametrize("elapsed", [0, 29, 31, 299, 599])
def test_c23_16_demand_is_identical_between_old_and_long_capacity_looks(held_fleet, elapsed):
    service, harness, _, advance = held_fleet
    job = long_wait(service, harness)
    advance(elapsed)
    long = service._reset_demand()
    # The old cadence's latest cached look would be at most 29 seconds old.
    latest = utcnow()
    wait = service._capacity_waits[job]
    service._capacity_waits[job] = {**wait, "checked_at": latest}
    assert service._reset_demand() == long
    assert [row["job_id"] for row in long] == [job]


def test_c6_10_released_hold_places_the_old_rules_jobs_in_order_no_later(routing_state, clock, tmp_path,
                                                                     monkeypatch):  # noqa: F811
    """Differential: replay one fixture with the old 30s rule and with event bring-forward."""
    from tests.fake.conftest import Harness

    def run_case(old):
        root = tmp_path / ("old-rule" if old else "event-rule")
        root.mkdir()
        harness = Harness(root)
        service = daemon_module.Daemon(root)
        try:
            service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                              ReadingLabel.PROVIDER, "fixture", utcnow()))
            closure = service.store.add_closure(Closure("codex-1", "account", after(7200),
                                                       ClosureReason.OPERATOR_HOLD, ClockSource.REPORTED, "fixture"))
            jobs = [submit(service, harness, pinned_model="terra", name=f"job-{i}") for i in range(4)]
            with monkeypatch.context() as patch:
                if old:
                    patch.setattr(scheduler, "capacity_recheck_ceiling", lambda decision: 30)
                    patch.setattr(service, "_capacity_change_news", lambda kind: False)
                service._admit()
                for job in jobs:
                    service._capacity_waits[job] = {**service._capacity_waits[job], "rechecks": 9}
                    make_due(service, job)
                service._admit()
                clock(17)
                release(service, closure)
                service._admit()
                placed_at = 17
                if old:
                    assert not any(service.store.list_attempts(job) for job in jobs)
                    clock(13)
                    service._admit()
                    placed_at = 30
                order = [jobs.index(row["job_id"]) for row in service.store.query(
                    "SELECT job_id FROM attempts ORDER BY rowid")]
                return order, placed_at
        finally:
            service.close()

    old_order, old_at = run_case(True)
    new_order, new_at = run_case(False)
    assert new_order == old_order == list(range(4))
    assert new_at <= old_at
