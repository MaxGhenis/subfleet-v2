"""C-6.10: a capacity wait that keeps reaching one verdict backs off and adds no rows.

Incident, 2026-09-20: three jobs no lane would admit were each re-evaluated once
a second for hours. Every look prepared the workspace (git), scored every lane,
and wrote a 22 KB decision row: 1,628 rows in ten minutes with zero attempts
reserved, 681 MB of a 709 MB store, and a daemon at a full core doing it.
"""

from datetime import datetime, timezone

import pytest

from subfleet import scheduler
from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)


@pytest.fixture
def fleet(routing_state):  # noqa: F811
    """One measured Codex lane, two slots; no Claude lane, so an Opus pin can never be placed."""
    service, harness = routing_state
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    return service, harness


def submit(service, harness, **changes):
    return service.dispatch("submit", harness.submit_args(**changes))["job_id"]


def make_due(service, job_id):
    service.store.update_job(job_id, next_check_at=utcnow())


def seconds_until(timestamp):
    due = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (due - datetime.now(timezone.utc)).total_seconds()


def decisions(service, job_id):
    return len(service.store.list_decisions(job_id))


def test_c6_10_the_incident_a_standing_verdict_adds_one_row_however_often_it_is_checked(fleet):
    """C-6.10 one hundred looks at a job nothing admits leave one decision row, not one hundred."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    for _ in range(100):
        service._admit()
        assert service.store.get_job(stuck)["state"] == "waiting"
        make_due(service, stuck)
    assert decisions(service, stuck) == 1
    assert service._capacity_waits[stuck]["rechecks"] == 99


def test_c6_10_the_recheck_clock_doubles_to_a_ceiling(fleet):
    """C-6.10 1 s, 2 s, 4 s ... 30 s: the wait is never rechecked sooner than the last time."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    waits = []
    for _ in range(8):
        service._admit()
        waits.append(seconds_until(service.store.get_job(stuck)["next_check_at"]))
        make_due(service, stuck)
    for found, expected in zip(waits, (1, 2, 4, 8, 16, 30, 30, 30)):
        assert expected - 1.5 <= found <= expected + .5           # timestamps are whole seconds
    assert waits[-1] > 25                                          # the incident's cadence was 1 s, forever


def test_c6_10_a_wait_that_is_not_due_is_not_looked_at(fleet):
    """C-6.10 between rechecks a pass costs the job nothing: no workspace, no evaluation, no write."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    make_due(service, stuck)
    service._admit()                                              # next check is now 2 s out
    looked = []
    real = service._workspace
    service._workspace = lambda job: looked.append(job["job_id"]) or real(job)
    before = service.store.one("SELECT count(*) n FROM events")["n"]
    for _ in range(20):
        service._admit()
    assert looked == [] and service.store.one("SELECT count(*) n FROM events")["n"] == before


def test_c6_10_a_new_verdict_is_recorded_and_restarts_the_clock(fleet):
    """C-6.10 the row that is skipped is a repeat; a verdict that changes is evidence and is kept."""
    from tests.fake.test_routing_end_to_end import claude_lane
    from subfleet.contracts import ClockSource, Closure, ClosureReason
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    for _ in range(5):
        service._admit()
        make_due(service, stuck)
    assert decisions(service, stuck) == 1 and service._capacity_waits[stuck]["rechecks"] == 4
    service.store.put_lane(claude_lane("claude-2"))               # a lane appears, closed for Opus
    service.store.add_closure(Closure("claude-2", "claude-opus-5", after(3600),
                                      ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    service._admit()
    assert decisions(service, stuck) == 2
    assert service._capacity_waits[stuck]["rechecks"] == 0
    assert seconds_until(service.store.get_job(stuck)["next_check_at"]) <= 1.5


def test_c6_10_a_known_reset_sooner_than_the_backoff_is_checked_on_time(fleet):
    """C-6.10, plan amendment 11: backing off never sleeps through a closure's own clock."""
    from tests.fake.test_routing_end_to_end import claude_lane
    from subfleet.contracts import ClockSource, Closure, ClosureReason
    service, harness = fleet
    service.store.put_lane(claude_lane("claude-2"))
    service.store.add_closure(Closure("claude-2", "claude-opus-5", after(5),
                                      ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    stuck = submit(service, harness, pinned_model="opus")
    for _ in range(6):                                            # backed off to 30 s
        service._admit()
        assert seconds_until(service.store.get_job(stuck)["next_check_at"]) <= 5.5
        make_due(service, stuck)


def test_c6_10_capacity_that_comes_free_is_seen_on_the_next_pass(fleet):
    """C-6.10 a backed-off job does not wait out its clock once an attempt ends and a slot frees."""
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 1
    first = submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    service._admit()
    for _ in range(6):                                            # `second` backs off to 30 s behind a full fleet
        make_due(service, second)
        service._admit()
    assert service.store.get_job(second)["state"] == "waiting"
    assert seconds_until(service.store.get_job(second)["next_check_at"]) > 25
    attempt = service.store.list_attempts(first)[0]["attempt_id"]
    with service.store.transaction("fixture.attempt_ended") as tx:
        tx.execute("UPDATE attempts SET state='failed' WHERE attempt_id=?", (attempt,))
        tx.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (first,))
        tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (attempt, first))
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(second)] == ["reserved"]
    assert second not in service._capacity_waits


def test_c6_10_a_probe_reservation_coming_and_going_frees_nothing(fleet):
    """C-6.10 probes visit every idle lane each cycle (C-18.1); they are not freed capacity."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    make_due(service, stuck)
    service._admit()
    assert service.store.acquire_lease("lane:codex-1:slot:0", "probe:timer:fixture")
    service._admit()
    service.store.release_leases("probe:timer:fixture")
    looked = []
    real = service._workspace
    service._workspace = lambda job: looked.append(job["job_id"]) or real(job)
    service._admit()
    assert looked == []


def test_c6_10_a_held_lease_backs_off_too(fleet):
    """C-6.10 the lease wait was a fixed 1 s, for as long as another job held the path."""
    service, harness = fleet
    out = str(harness.root / "report.md")
    waiting = submit(service, harness, pinned_model="terra", out_path=out)
    with service.store.transaction("fixture.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                   (f"out:{out}", "some-other-job", utcnow()))
    for _ in range(6):
        service._admit()
        make_due(service, waiting)
    job = service.store.get_job(waiting)
    assert job["state"] == "waiting" and service._capacity_waits[waiting]["label"] == "lease-held"
    assert service._capacity_waits[waiting]["rechecks"] == 5
    assert not service.store.list_attempts(waiting)


def test_c6_10_a_placed_or_finished_job_leaves_no_wait_record(fleet):
    """C-6.10 the in-memory record lives exactly as long as the wait."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    assert stuck in service._capacity_waits
    service.kill(__import__("subfleet.protocol", fromlist=["KillArgs"]).KillArgs(stuck))
    service._admit()
    assert stuck not in service._capacity_waits


# --- the pure rules -----------------------------------------------------------------------------

def decision(rejections, *, evaluated_at="2026-09-20T21:00:00Z", readings=(), chosen=None):
    return {"chain": ["opus"], "chosen_lane": chosen, "chosen_model": "opus" if chosen else None,
            "policy_hash": "p", "reason": "-",
            "evaluations": [{"model": "opus", "candidates": [chosen] if chosen else [],
                             "rejections": rejections, "readings": list(readings),
                             "evaluated_at": evaluated_at}]}


def rejected(lane, *reasons, **detail):
    return {"lane_id": lane, "reason": reasons[0], "reasons": list(reasons), **detail}


def test_c6_10_a_signature_ignores_the_evidence_and_keeps_the_verdict():
    """C-6.10 fresh readings and a new timestamp are the same verdict; a new reason is not."""
    base = decision([rejected("claude-1", "reserve:fable:unmeasured"), rejected("claude-2", "below-floor")])
    later = decision([rejected("claude-2", "below-floor"), rejected("claude-1", "reserve:fable:unmeasured")],
                     evaluated_at="2026-09-20T21:00:01Z", readings=[{"lane_id": "claude-1", "utilization": .31}])
    assert scheduler.verdict_signature(base) == scheduler.verdict_signature(later)
    changed = decision([rejected("claude-1", "reserve:fable:unmeasured"), rejected("claude-2", "no-slot")])
    assert scheduler.verdict_signature(base) != scheduler.verdict_signature(changed)
    assert scheduler.verdict_signature(base) != scheduler.verdict_signature(decision([], chosen="claude-1"))


def test_c6_10_no_slot_on_a_lane_rejected_anyway_is_evidence_not_verdict():
    """C-6.10 slots fill and empty all day, and a probe's reservation counts toward the fleet cap,
    so each probe cycle marks every lane `no-slot` for a second. Replayed over the 80,996 rows the
    incident left, this rule keeps 74; counting `no-slot` kept 1,818 and reset the clock each time."""
    base = decision([rejected("claude-1", "reserve:fable:unmeasured"), rejected("codex-1", "closed:account:T")])
    probed = decision([rejected("claude-1", "reserve:fable:unmeasured", "no-slot", slot_block="probe:timer:abc"),
                       rejected("codex-1", "closed:account:T", "no-slot")])       # the fleet cap, for that second
    assert scheduler.verdict_signature(base) == scheduler.verdict_signature(probed)
    # A lane with no other reason is what the job is waiting for: that `no-slot` is the verdict.
    waiting_for_a_slot = decision([rejected("claude-1", "no-slot"), rejected("codex-1", "closed:account:T")])
    assert scheduler.verdict_signature(base) != scheduler.verdict_signature(waiting_for_a_slot)
    freed = decision([rejected("codex-1", "closed:account:T")], chosen="claude-1")
    assert scheduler.verdict_signature(waiting_for_a_slot) != scheduler.verdict_signature(freed)


@pytest.mark.parametrize("rechecks,expected", [(0, 1), (1, 2), (2, 4), (4, 16), (5, 30), (6, 30), (10_000, 30), (-3, 1)])
def test_c6_10_recheck_delay(rechecks, expected):
    assert scheduler.capacity_recheck_delay(rechecks) == expected


def test_c6_10_dominant_rejection_names_what_most_lanes_said():
    """C-6.11 the label `status` and the log group jobs by."""
    found = scheduler.dominant_rejection(decision([
        rejected("claude-1", "reserve:fable:unmeasured"), rejected("claude-2", "reserve:fable:unmeasured"),
        rejected("codex-1", "closed:account:2026-09-26T12:11:15Z", "below-floor"),
        rejected("codex-2", "closed:account:2026-09-21T07:05:20Z")]))
    assert found in ("reserve:fable:unmeasured", "closed:account")   # a tie, broken by name
    assert scheduler.dominant_rejection(decision([rejected("codex-1", "closed:account:2026-09-26T12:11:15Z"),
                                                  rejected("codex-2", "closed:account:2026-09-21T07:05:20Z"),
                                                  rejected("claude-1", "desktop")])) == "closed:account"
    assert scheduler.dominant_rejection(decision([])) == "no-lanes"
    assert scheduler.dominant_rejection(None) == "not-evaluated"
