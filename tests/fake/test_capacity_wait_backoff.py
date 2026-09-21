"""C-6.10: a capacity wait that keeps reaching one verdict backs off and adds no rows.

Incident, 2026-09-20: three jobs no lane would admit were each re-evaluated once
a second for hours. Every look prepared the workspace (git), scored every lane,
and wrote a 22 KB decision row: 1,628 rows in ten minutes with zero attempts
reserved, 681 MB of a 709 MB store, and a daemon at a full core doing it.
"""

from datetime import datetime

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


def backoff(service, job_id):
    """Seconds from the look to the next one. Both stamps come from one pass, so no wall clock is involved."""
    looked = datetime.fromisoformat(service._capacity_waits[job_id]["checked_at"].replace("Z", "+00:00"))
    due = datetime.fromisoformat(service.store.get_job(job_id)["next_check_at"].replace("Z", "+00:00"))
    return (due - looked).total_seconds()


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
        waits.append(backoff(service, stuck))
        make_due(service, stuck)
    for found, expected in zip(waits, (1, 2, 4, 8, 16, 30, 30, 30)):
        assert abs(found - expected) <= 1                          # stamps are whole seconds
    assert waits == sorted(waits) and waits[-1] >= 29              # the incident's cadence was 1 s, forever


def test_c6_10_a_wait_that_is_not_due_is_not_looked_at(fleet):
    """C-6.10 between rechecks a pass costs the job nothing: no workspace, no evaluation, no write."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    make_due(service, stuck)
    service._admit()
    service.store.update_job(stuck, next_check_at=after(3600))   # not due, however slow this machine is
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
    assert backoff(service, stuck) <= 2


def test_c6_10_a_known_reset_sooner_than_the_backoff_is_checked_on_time(fleet):
    """C-6.10, plan amendment 11: backing off never sleeps through a closure's own clock."""
    from tests.fake.test_routing_end_to_end import claude_lane
    from subfleet.contracts import ClockSource, Closure, ClosureReason
    service, harness = fleet
    until = after(20)
    service.store.put_lane(claude_lane("claude-2"))
    service.store.add_closure(Closure("claude-2", "claude-opus-5", until,
                                      ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    assert backoff(service, stuck) <= 2                           # 1 s: sooner than the closure
    # The same verdict for the tenth time would wait 30 s. Two passes, so this machine's speed plays no part.
    service._capacity_waits[stuck] = {**service._capacity_waits[stuck], "rechecks": 8}
    make_due(service, stuck)
    service._admit()
    assert service._capacity_waits[stuck]["rechecks"] == 9
    assert service.store.get_job(stuck)["next_check_at"] == until  # the closure's clock exactly, not 30 s


def test_c6_10_waiting_metadata_takes_the_sooner_of_the_backoff_and_the_reset():
    """C-6.10 the pure rule, with a fixed clock."""
    now = "2026-09-20T21:00:00Z"
    closes = lambda when: {"chain": ["opus"], "chosen_lane": None, "chosen_model": None, "policy_hash": "p",
                           "reason": "-", "evaluations": [{"model": "opus", "closures": [{"until_at": when}]}]}
    from subfleet.contracts import Decision
    as_decision = lambda value: Decision(tuple(value["chain"]), tuple(value["evaluations"]), None, None, "-", "p")
    sooner = scheduler.waiting_metadata(as_decision(closes("2026-09-20T21:00:05Z")), now, rechecks=9)
    later = scheduler.waiting_metadata(as_decision(closes("2026-09-20T22:20:00Z")), now, rechecks=9)
    first = scheduler.waiting_metadata(as_decision(closes("2026-09-20T22:20:00Z")), now)
    assert sooner["next_check_at"] == "2026-09-20T21:00:05Z"
    assert later["next_check_at"] == "2026-09-20T21:00:30Z"        # the incident: an hour off, rechecked every second
    assert first["next_check_at"] == "2026-09-20T21:00:01Z"


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
    assert backoff(service, second) >= 29
    service.store.update_job(second, next_check_at=after(3600))   # far off, however slow this machine is
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
    service.store.update_job(stuck, next_check_at=after(3600))   # not due, however slow this machine is
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


# --- review of cb83e1b --------------------------------------------------------------------------

def release_a_lease(service):
    """An unrelated lease appears on one pass and is gone on the next: freed capacity, as admission sees it."""
    with service.store.transaction("fixture.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES('out:/elsewhere','another-job',?)", (utcnow(),))
    service._admit()
    with service.store.transaction("fixture.release") as tx:
        tx.execute("DELETE FROM leases WHERE holder='another-job'")


def test_c6_10_freed_capacity_never_spends_a_workspace_retry(fleet):
    """C-6.10, C-6.8 (P1): a capacity wait whose workspace then failed kept its record, so every
    unrelated release brought its workspace retry forward; eight of them ended the job."""
    import errno
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    assert stuck in service._capacity_waits
    make_due(service, stuck)
    real = service._workspace
    tries = []

    def failing(job):
        tries.append(job["job_id"])
        raise OSError(errno.EAGAIN, "resource temporarily unavailable")

    service._workspace = failing
    service._admit()
    job = service.store.get_job(stuck)
    assert job["wait_reason"] == "workspace" and len(tries) == 1
    assert stuck not in service._capacity_waits                    # the wait is C-6.8's now
    for _ in range(9):                                            # nine releases: one more than `workspace_retry_max`
        release_a_lease(service)
        service._admit()
    assert len(tries) == 1 and service.store.get_job(stuck)["state"] == "waiting"
    service._workspace = real


def test_c6_10_a_route_that_cannot_be_prepared_backs_off_instead_of_spinning(fleet):
    """C-6.10: `_prepare_route` gave up without setting a clock (its probe's slot was held), so the
    job stayed `queued` and was prepared again, with a new probe directory, on every tick."""
    service, harness = fleet
    job_id = submit(service, harness, pinned_model="astra")
    service._prepare_route = lambda job, decision_job, exclusions: (None, service._desktop_identity())
    looked = []
    real = service._workspace
    service._workspace = lambda job: looked.append(job["job_id"]) or real(job)
    service._admit()
    job = service.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "capacity") and job["next_check_at"]
    assert service._holds[job_id]["reason"] == "probe-pending"
    service.store.update_job(job_id, next_check_at=after(3600))
    for _ in range(20):
        service._admit()
    assert looked == [job_id]                                     # once, not once a tick


def test_c6_10_a_probes_own_wait_is_never_brought_forward(fleet):
    """C-6.10: the 60 s wait after an inconclusive probe keeps its clock, or every release re-probes."""
    service, harness = fleet
    job_id = submit(service, harness, pinned_model="astra")
    service.store.update_job(job_id, state="waiting", wait_reason="capacity", next_check_at=after(3600))
    assert service._capacity_wait(job_id, "probe-wait:x", {"reason": "probe-pending"}, expedite=False) == 0
    assert service._capacity_wait(job_id, "probe-wait:x", {"reason": "probe-pending"}, expedite=False) == 1
    release_a_lease(service)
    looked = []
    real = service._workspace
    service._workspace = lambda job: looked.append(job["job_id"]) or real(job)
    service._admit()
    assert looked == []


def test_c6_10_a_lease_taken_and_lost_between_two_passes_is_still_a_release(fleet):
    """C-6.10: the snapshot is taken before the pass places anything, so an attempt that was placed
    and ended before the next pass was in neither snapshot and freed nothing."""
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 1
    first = submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="terra")
    service._admit()                                              # places `first`; `second` waits
    service.store.update_job(second, next_check_at=after(3600))
    attempt = service.store.list_attempts(first)[0]["attempt_id"]
    with service.store.transaction("fixture.attempt_ended") as tx:
        tx.execute("UPDATE attempts SET state='failed' WHERE attempt_id=?", (attempt,))
        tx.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (first,))
        tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (attempt, first))
    service._admit()                                              # the very next pass
    assert [row["state"] for row in service.store.list_attempts(second)] == ["reserved"]


def test_c6_10_a_restart_looks_at_every_capacity_wait_once(fleet):
    """C-6.10: the wait records are in memory; after a restart nothing could bring a persisted wait forward."""
    service, harness = fleet
    capacity_wait = submit(service, harness, pinned_model="opus")
    approval = submit(service, harness, pinned_model="astra")
    service.store.update_job(capacity_wait, state="waiting", wait_reason="capacity", next_check_at=after(3600))
    service.store.update_job(approval, state="waiting", wait_reason="approval", next_check_at=after(3600))
    service._recover_capacity_waits()
    assert service.store.get_job(capacity_wait)["next_check_at"] <= utcnow()
    assert service.store.get_job(approval)["next_check_at"] > utcnow()      # only capacity waits


# --- review of be171d5 --------------------------------------------------------------------------

def test_c6_10_a_pair_that_changed_after_its_probe_waits_on_a_clock(fleet, monkeypatch):
    """C-6.10: this branch recorded `probe-pending` and set no clock, so a lane whose state kept
    moving was prepared, and probed, on every tick."""
    from subfleet import daemon as daemon_module
    service, harness = fleet
    job_id = submit(service, harness, pinned_model="astra")
    monkeypatch.setattr(daemon_module.scheduler, "probe_required", lambda decision, job: True)
    service._prepare_route = lambda job, decision_job, exclusions: (set(), service._desktop_identity())
    looked = []
    real = service._workspace
    service._workspace = lambda job: looked.append(job["job_id"]) or real(job)
    service._admit()
    job = service.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "capacity") and job["next_check_at"]
    assert service._holds[job_id]["reason"] == "probe-pending" and service._holds[job_id]["next_check_at"]
    service.store.update_job(job_id, next_check_at=after(3600))
    for _ in range(20):
        service._admit()
    assert looked == [job_id]


def test_c6_10_a_reservation_that_does_not_happen_leaves_no_probe_directory(fleet, monkeypatch):
    """C-6.10: the directory was made before the reservation and every early return left it behind.
    The reviewer's case: a two-slot lane with slot 0 busy is still chosen, and the probe wants slot 0."""
    from subfleet import daemon as daemon_module
    service, harness = fleet
    job_id = submit(service, harness, pinned_model="astra")
    monkeypatch.setattr(daemon_module.scheduler, "probe_required", lambda decision, job: bool(decision.chosen_lane))
    assert service.store.acquire_lease("lane:codex-1:slot:0", "another-job/a1")
    job = service.store.get_job(job_id)
    approved, _ = service._prepare_route(job, job, ())
    assert approved is None
    probes = service.root / "lanes" / "codex-1" / "probes"
    assert not probes.exists() or list(probes.iterdir()) == []


def test_c6_11_a_hurried_look_that_ends_at_the_route_still_reports_itself(fleet):
    """C-6.11: a look brought forward found the route unprepared and the clock already set; the hold
    had no `next_check_at` and the record kept the previous look's label."""
    service, harness = fleet
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    assert service._capacity_waits[stuck]["label"] == "no-lanes"
    service.store.update_job(stuck, next_check_at=after(3600))
    service._prepare_route = lambda job, decision_job, exclusions: (None, service._desktop_identity())
    release_a_lease(service)
    service._admit()                                              # hurried by the release; the clock is an hour off
    hold = service._holds[stuck]
    assert hold["reason"] == "probe-pending" and hold["next_check_at"] == service.store.get_job(stuck)["next_check_at"]
    assert service._capacity_waits[stuck]["label"] == "probe-pending"
    assert service._capacity_waits[stuck]["signature"].endswith(":False")     # the verdict and its count are untouched


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


def test_c6_11_dominant_rejection_names_what_keeps_the_job_out():
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


def test_c6_11_the_cap_is_the_cause_only_when_a_lane_would_otherwise_take_the_job():
    """C-6.11 review of cb83e1b: a probe counts toward the fleet cap, so a job no lane admits anyway
    read `fleet-full` for the second each probe ran, which is ordinary queueing and hid the warning."""
    def capped(rejections, blocks):
        value = decision(rejections)
        value["evaluations"][0]["capacity_blocks"] = blocks
        return value
    no_lane_would = capped([rejected("claude-1", "no-slot", "reserve:fable:unmeasured"),
                            rejected("codex-1", "closed:account:T", "no-slot")], ["fleet"])
    assert scheduler.dominant_rejection(no_lane_would) in ("reserve:fable:unmeasured", "closed:account")
    a_lane_would = capped([rejected("claude-1", "no-slot", "reserve:fable:unmeasured"),
                           rejected("codex-1", "no-slot")], ["fleet"])
    assert scheduler.dominant_rejection(a_lane_would) == "fleet-full"
    assert scheduler.dominant_rejection(capped([rejected("codex-1", "no-slot")], ["parent:j"])) == "parent-cap"
    assert scheduler.dominant_rejection(capped([rejected("codex-1", "no-slot")], [])) == "no-slot"
