"""C-6.12 and C-11.2: one job whose route cannot be evaluated never stops admission.

Incident, 2026-09-22: `worker admission failed: ValueError (128 in a row)`, and no
attempt was reserved for any session from 14:45:05Z to 18:02:45Z (10:45 to 14:03 EDT). Five queued jobs were pinned by
email (`-a max@axiom.org`, `-a max@thesisinstitute.org`) with a task and no model.
Submit resolved the email against the store's lane rows, where Codex lanes carry
no email, found one Claude lane, and kept the raw email as the pin. Admission
evaluates against the capacity view, where the usage probe has given each Codex
lane its email, so the same email named a Claude lane and a Codex lane and
`resolve_lane` raised "ambiguous lane". Nothing in `_admit_pass` caught it per
job, so every pass aborted at the first of them and every later job in every
tier starved; `why` caught it and printed "No decision recorded." for those five.

These tests build that roster: `claude-a` labelled `max@example.invalid`, and the
fixture's `codex-1` whose probe reported the same email.
"""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from subfleet import doctor, protocol, scheduler
from subfleet import daemon as daemon_module
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, Exit, Lane, LaneOwner,
                                Reading, ReadingLabel)
from subfleet.daemon import Daemon, after, utcnow
from tests.fake.test_admission_visibility import Inline
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)

EMAIL = "max@example.invalid"


def claude_lane(lane_id, label=EMAIL, *, enabled=True, credential=None):
    ref = credential or f"/fake/{lane_id}"
    return Lane(lane_id, "claude", f"claude:{label}", Credential("claude", ref, "home"), ref,
                LaneOwner.V2, False, enabled, None, label)


def measured(service, lane_id):
    service.store.add_reading(Reading(lane_id, "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))


def codex_probe_reports(service, email=EMAIL, lane_id="codex-1"):
    """What `Timers._persist` leaves after a Codex usage probe: the lane's email, in the view only."""
    service.timers.metadata[lane_id] = {**service.timers.metadata.get(lane_id, {}), "email": email}


@pytest.fixture
def fleet(routing_state):  # noqa: F811
    """claude-a (labelled EMAIL) and codex-1, both measured; codex-1's probe has not reported yet."""
    service, harness = routing_state
    service.store.put_lane(claude_lane("claude-a"))
    for lane_id in ("claude-a", "codex-1"):
        measured(service, lane_id)
    return service, harness


@pytest.fixture
def incident(fleet):
    """The 2026-09-22 roster: the email names claude-a in the store and codex-1 in the view."""
    service, harness = fleet
    codex_probe_reports(service)
    return service, harness


def submit(service, harness, **changes):
    return service.dispatch("submit", harness.submit_args(**changes))["job_id"]


def legacy(service, harness, pin, **changes):
    """A job accepted before this fix: submitted, then given the raw pin the old submit kept."""
    job_id = submit(service, harness, **{"pinned_lane": "claude-a", **changes})
    service.store.update_job(job_id, pinned_lane=pin)
    return job_id


def admitted(service, job_id):
    return [row["state"] for row in service.store.list_attempts(job_id)] == ["reserved"]


def log_text(service):
    service._log_handler.flush()
    return (service.root / "daemon.log").read_text()


# --- C-6.12: the pass survives one job it cannot route ----------------------------------------

def test_c6_12_the_incident_a_job_pinned_by_an_ambiguous_email_does_not_stop_the_pass(incident):
    """C-6.12 the incident exactly: before the fix `_admit` raised ValueError and placed nothing."""
    service, harness = incident
    stuck = legacy(service, harness, EMAIL, pinned_model=None, task="review", tier="standard")
    later = submit(service, harness, pinned_model="astra")
    service._admit()                                                  # raised "ambiguous lane" before
    assert admitted(service, later)
    # C-11.2: the job's own chain says Claude, so the email names one lane after all.
    assert admitted(service, stuck)
    assert service.store.list_attempts(stuck)[0]["lane_id"] == "claude-a"


def test_c6_12_the_incident_through_the_worker_pool_logs_no_failure(incident):
    """C-6.12, C-5.10 as the control loop runs it: no `worker admission failed`, no backoff."""
    service, harness = incident
    service.workers = Inline(service.workers)
    stuck = legacy(service, harness, EMAIL, pinned_model=None, task="build", tier="standard")
    later = submit(service, harness, pinned_model="terra", tier="hard")      # another tier starved too
    service._schedule("admission", service._admit, paced=True)
    assert "admission" not in service._worker_failures
    assert "worker admission failed" not in log_text(service)
    assert admitted(service, later) and admitted(service, stuck)


@pytest.mark.parametrize("later_changes", [
    {"pinned_model": "astra"},                                   # same tier, a model the stuck job could never use
    {"pinned_model": None, "task": "research", "tier": "standard"},
    {"pinned_model": "terra", "tier": "hard"},                   # another tier: the incident starved every tier
])
def test_c6_12_a_pin_that_names_two_lanes_is_refused_and_the_pass_goes_on(incident, later_changes):
    """C-6.12 a lane pin with no model whose name matches claude-a and codex-1 can never be routed:
    it fails with the message (exit 2, what submit would say) and the next job is placed."""
    service, harness = incident
    stuck = legacy(service, harness, EMAIL, pinned_model=None)
    later = submit(service, harness, **later_changes)
    service._admit()
    assert admitted(service, later)
    job = service.store.get_job(stuck)
    assert job["state"] == "failed" and job["rc"] == int(Exit.INVALID_INPUT)
    assert not service.store.list_attempts(stuck)
    notice = service.store.query("SELECT text FROM notices WHERE job_id=?", (stuck,))[0]["text"]
    assert "claude-a" in notice and "codex-1" in notice and EMAIL in notice
    event = service.store.query("SELECT data_json FROM events WHERE kind='job.route_refused' AND job_id=?", (stuck,))
    assert json.loads(event[0]["data_json"])["error_type"] == "RouteError"
    assert f"job {stuck} refused at admission" in log_text(service)
    assert stuck not in service._holds
    assert job["wait_reason"] is None and job["next_check_at"] is None               # a failed job waits on nothing
    answer = service.dispatch("why", {"job_id": stuck})
    assert answer["refused"].startswith("RouteError: pinned_lane") and answer["route_error"] is None
    assert "Refused at admission: RouteError" in answer["text"] and "codex-1" in answer["text"]
    assert "No decision recorded." not in answer["text"]


def test_c6_12_an_evaluation_error_holds_the_job_visibly_and_holds_nobody_else(fleet, monkeypatch):
    """C-6.12 an error that is not the job's own (bad capacity data, a policy edit) is never terminal:
    the job waits on `route` with a backoff clock, `why` names the error and `status` counts the hold,
    and it is no C-6.9 waiter, so later jobs of its tier and model still pass it."""
    service, harness = fleet
    service.policy["caps"]["max_in_flight_per_lane"] = 4          # all three astra jobs fit on codex-1
    broken = submit(service, harness, pinned_model="astra")
    later = submit(service, harness, pinned_model="astra")
    real = service._pick

    def pick(job, **options):
        if job["job_id"] == broken:
            raise KeyError("utilization")
        return real(job, **options)
    monkeypatch.setattr(service, "_pick", pick)
    service._admit()
    assert admitted(service, later)
    job = service.store.get_job(broken)
    assert job["state"] == "waiting" and job["wait_reason"] == "route" and job["next_check_at"] > utcnow()
    hold = service._holds[broken]
    assert hold["reason"] == "route" and hold["error_type"] == "KeyError" and "utilization" in hold["error"]
    answer = service.dispatch("why", {"job_id": broken})                               # why's own evaluation raises too
    assert "Decision: none; this job's route could not be evaluated: KeyError" in answer["text"]
    monkeypatch.setattr(service, "_pick", real)                                        # and when it does not
    answer = service.dispatch("why", {"job_id": broken})
    assert any("its route could not be evaluated (KeyError: 'utilization')" in line for line in answer["queue"])
    monkeypatch.setattr(service, "_pick", pick)
    assert service.dispatch("daemon.status", {})["admission"]["reasons"].get("route") == 1
    event = service.store.query("SELECT data_json FROM events WHERE kind='job.route_deferred' AND job_id=?", (broken,))
    assert json.loads(event[0]["data_json"])["error_type"] == "KeyError"
    # A pass that does not look at it (its clock is ahead) keeps the answer and holds no one.
    third = submit(service, harness, pinned_model="astra")
    service._admit()
    assert service._holds[broken]["reason"] == "route" and service._holds[broken]["error_type"] == "KeyError"
    assert admitted(service, third)
    # The error clears (a restart with the fix, a corrected policy): the job is routed, not lost.
    monkeypatch.setattr(service, "_pick", real)
    service.store.update_job(broken, next_check_at=utcnow())
    service._admit()
    assert admitted(service, broken)


def test_c6_12_route_waits_back_off_and_a_restart_looks_at_them_at_once(fleet, monkeypatch):
    """C-6.12 5 s doubling to 300 s, one log line per deferral; recovery makes the wait due."""
    service, harness = fleet
    broken = submit(service, harness, pinned_model="astra")
    monkeypatch.setattr(service, "_pick", lambda job, **options: (_ for _ in ()).throw(ValueError("bad reset")))
    clocks = []
    for _ in range(3):
        service.store.update_job(broken, next_check_at=utcnow())
        service._admit()
        clocks.append(service._route_deferrals[broken]["deferrals"])
    assert clocks == [1, 2, 3]
    assert log_text(service).count(f"job {broken} route could not be evaluated") == 3
    service._recover_capacity_waits()
    assert service.store.get_job(broken)["next_check_at"] <= utcnow()


def race(service):
    """A commit between admission's early evaluation and its reservation (C-6.3): the
    roster changed, so the reservation must evaluate again inside.

    Made on another thread, as a concurrent commit arrives: the early evaluation
    may run in a read snapshot, where this thread may not begin a transaction
    (C-3.7, `SnapshotWriteError`)."""
    failures = []

    def commit():
        try:
            with service.store.transaction("test.race") as tx:
                tx.execute("INSERT INTO leases VALUES ('test:race','test','t',NULL)")
                tx.execute("DELETE FROM leases WHERE lease_key='test:race'")
        except BaseException as exc:                                  # noqa: BLE001
            failures.append(exc)
    other = threading.Thread(target=commit, name="test-race")
    other.start()
    other.join(30)
    assert not other.is_alive() and not failures, failures


def test_c6_12_the_evaluation_inside_the_reservation_is_isolated_too(fleet, monkeypatch):
    """C-6.12 `_pick` runs before the reservation and, when the store moved, inside it
    too (C-6.3); the roster can change between them."""
    service, harness = fleet
    raced = submit(service, harness, pinned_model="astra")
    later = submit(service, harness, pinned_model="terra")
    real = service._pick

    def pick(job, **options):
        if job["job_id"] == raced:
            if service.store._holds_writer():                 # the evaluation inside the reservation
                raise scheduler.RouteError(f"pinned_lane: {EMAIL!r} names 2 lanes (claude-a, codex-1)")
            race(service)
        return real(job, **options)
    monkeypatch.setattr(service, "_pick", pick)
    service._admit()
    assert admitted(service, later)
    assert service.store.get_job(raced)["state"] == "failed"
    assert not service.store.list_attempts(raced)
    assert not service.store.query("SELECT 1 FROM leases WHERE holder LIKE ?", (raced + "%",))
    kinds = [row["kind"] for row in service.store.query("SELECT kind FROM events WHERE job_id=?", (raced,))]
    assert "attempt.reserved" not in kinds and "job.route_refused" in kinds          # rolled back, then settled


def test_c6_12_a_refusal_inside_the_reservation_rolls_back_what_it_wrote(fleet, monkeypatch):
    """C-6.12 the reservation writes a limited lane into `exclusions` before it evaluates; a refusal undoes it."""
    service, harness = fleet
    raced = submit(service, harness, pinned_model="astra")
    service.store.add_attempt(attempt_id=raced + "/a1", job_id=raced, seq=1, lane_id="claude-a",
                              model_requested="gpt-6-astra", state="failed", outcome_class="limited")
    before = service.store.get_job(raced)["exclusions"]
    real = service._pick

    def pick(job, **options):
        if service.store._holds_writer():
            raise scheduler.RouteError("pinned_lane: fixture")
        race(service)
        return real(job, **options)
    monkeypatch.setattr(service, "_pick", pick)
    service._admit()
    assert service.store.get_job(raced)["exclusions"] == before and service.store.get_job(raced)["state"] == "failed"


# --- C-6.3, C-3.7: the reservation takes the evaluation made before it, when nothing moved ----------

def test_c6_3_the_reservation_takes_the_early_evaluation_when_nothing_was_committed(fleet, monkeypatch):
    service, harness = fleet
    job_id = submit(service, harness, pinned_model="astra")
    real, inside = service._pick, []
    monkeypatch.setattr(service, "_pick", lambda job, **o: inside.append(service.store._holds_writer())
                        or real(job, **o))
    service._admit()
    assert admitted(service, job_id)
    assert True not in inside                                     # no evaluation held the store lock
    assert service._route_evaluations == {"reused": 1, "again": 0, "moved": 0, "old": 0, "failed": 0}


def test_c6_3_a_commit_after_the_early_evaluation_means_evaluating_again(fleet, monkeypatch):
    """The reservation rests on the rows it reads: after a commit it decides afresh,
    and what it decides then is what it reserves."""
    service, harness = fleet
    job_id = submit(service, harness, pinned_model="astra")
    real = service._pick
    chosen = []

    def pick(job, **options):
        decision = real(job, **options)
        if service.store._holds_writer():
            chosen.append(decision.chosen_lane)
        else:
            race(service)
        return decision
    monkeypatch.setattr(service, "_pick", pick)
    service._admit()
    assert service._route_evaluations == {"reused": 0, "again": 1, "moved": 1, "old": 0, "failed": 0}
    assert [row["lane_id"] for row in service.store.list_attempts(job_id)] == chosen


def test_c6_3_an_early_evaluation_older_than_the_bound_is_evaluated_again(fleet, monkeypatch):
    service, harness = fleet
    monkeypatch.setattr(daemon_module, "ROUTE_REUSE_S", -1.0)   # every early evaluation is too old
    job_id = submit(service, harness, pinned_model="astra")
    service._admit()
    assert admitted(service, job_id)
    assert service._route_evaluations == {"reused": 0, "again": 1, "moved": 0, "old": 1, "failed": 0}


def ageing_reading(service, fresh_for_s: float) -> datetime:
    """A codex-1 reading that stops being fresh `fresh_for_s` from now; returns when."""
    ttl = service.policy["caps"]["reading_ttl_s"]
    observed = (datetime.now(timezone.utc) - timedelta(seconds=ttl - fresh_for_s)).replace(microsecond=0)
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", observed.strftime("%Y-%m-%dT%H:%M:%SZ")))
    return observed + timedelta(seconds=ttl)


def busy_codex(service, harness):
    """One attempt of another job already running on codex-1: measured, the lane has
    a second slot; unmeasured, it has none (C-6.4's `max_in_flight_unmeasured`)."""
    running = submit(service, harness, pinned_model="astra")
    service.store.update_job(running, state="running")
    service.store.add_attempt(attempt_id=running + "/a1", job_id=running, seq=1, lane_id="codex-1",
                              model_requested="gpt-6-astra", state="running")


def test_c6_3_a_reading_that_ages_out_before_the_reservation_means_evaluating_again(routing_state, monkeypatch):  # noqa: F811
    """Review of 5841d8b: an early evaluation made while codex-1's reading was fresh
    was reserved on after the reading aged out, putting a second attempt on a lane
    that was unmeasured by then. The reservation now evaluates again, and the lane,
    unmeasured, has no second slot."""
    service, harness = routing_state
    busy_codex(service, harness)
    job_id = submit(service, harness, pinned_model="astra")
    real, early, stale = service._pick, [], []

    def pick(job, **options):
        if not stale:                                     # just before admission's first look
            stale.append(ageing_reading(service, fresh_for_s=5))
        decision = real(job, **options)
        if "horizon" in options and not service.store._holds_writer():
            early.append(decision.chosen_lane)
            assert options["horizon"]["fresh_until"] == stale[0]
            while datetime.now(timezone.utc) <= stale[0] + timedelta(seconds=.2):
                time.sleep(.05)                               # the reading ages out before the reservation
        return decision
    monkeypatch.setattr(service, "_pick", pick)
    service._admit()
    assert early == ["codex-1"]                                # measured then: a second slot
    assert service._route_evaluations == {"reused": 0, "again": 1, "moved": 0, "old": 1, "failed": 0}
    assert not service.store.list_attempts(job_id)             # no second attempt on an unmeasured lane
    assert service.store.get_job(job_id)["state"] == "waiting"


def test_c6_3_an_early_evaluation_whose_readings_stay_fresh_is_reserved_on(routing_state):  # noqa: F811
    """The same fleet, with the reading fresh for minutes: the early decision is reserved on."""
    service, harness = routing_state
    busy_codex(service, harness)
    ageing_reading(service, fresh_for_s=100)
    job_id = submit(service, harness, pinned_model="astra")
    service._admit()
    assert service._route_evaluations == {"reused": 1, "again": 0, "moved": 0, "old": 0, "failed": 0}
    assert [row["lane_id"] for row in service.store.list_attempts(job_id)] == ["codex-1"]


def test_c6_12_why_names_a_route_error_it_meets_itself(incident):
    """C-6.11, C-6.12 `why` evaluated the incident's jobs, caught the ValueError, and said nothing."""
    service, harness = incident
    stuck = legacy(service, harness, EMAIL, pinned_model=None)
    answer = service.dispatch("why", {"job_id": stuck})
    assert answer["decision"] is None
    assert "route could not be evaluated" in answer["text"] and "codex-1" in answer["text"]
    assert "No decision recorded." not in answer["text"]


# --- C-11.2: submit stores the lane id, resolved as admission resolves it ---------------------

def test_c11_2_submit_stores_the_lane_id_its_pin_names(fleet):
    service, harness = fleet
    job_id = submit(service, harness, pinned_model="opus", pinned_lane=EMAIL)
    assert service.store.get_job(job_id)["pinned_lane"] == "claude-a"
    manifest = json.loads((service.root / "jobs" / job_id / "manifest.json").read_text())
    assert manifest["job"]["pinned_lane"] == "claude-a"
    submitted = service.store.query("SELECT data_json FROM events WHERE kind='job.submitted' AND job_id=?", (job_id,))
    assert json.loads(submitted[0]["data_json"])["pin"] == {"requested": EMAIL, "lane_id": "claude-a"}


def test_c11_2_submit_resolves_with_the_identities_admission_uses(incident):
    """C-11.2 the store's rows did not have codex-1's email; the view does, so submit sees both lanes."""
    service, harness = incident
    with pytest.raises(protocol.ProtocolError) as refused:
        submit(service, harness, pinned_model=None, pinned_lane=EMAIL)
    message = str(refused.value)
    assert "claude-a" in message and "codex-1" in message and refused.value.code == Exit.INVALID_INPUT
    assert not service.store.query("SELECT 1 FROM jobs")


@pytest.mark.parametrize("changes,lane", [
    ({"pinned_model": "opus"}, "claude-a"),
    ({"pinned_model": "astra"}, "codex-1"),
    ({"pinned_model": None, "task": "review", "tier": "standard"}, "claude-a"),     # the incident's jobs
    ({"pinned_model": None, "task": "research", "tier": "hard"}, "codex-1"),
])
def test_c11_2_the_jobs_provider_narrows_a_pin_that_names_two_lanes(incident, changes, lane):
    """C-11.2 a pinned job evaluates one model; a lane of another provider could never take it."""
    service, harness = incident
    job_id = submit(service, harness, pinned_lane=EMAIL, **changes)
    assert service.store.get_job(job_id)["pinned_lane"] == lane
    service._admit()
    assert service.store.list_attempts(job_id)[0]["lane_id"] == lane


def test_c11_2_a_later_roster_change_cannot_make_an_accepted_pin_ambiguous(fleet):
    """C-11.2 the pin is resolved once, at submit; the probe reporting codex-1's email later changes nothing."""
    service, harness = fleet
    job_id = submit(service, harness, pinned_model=None, pinned_lane=EMAIL)
    codex_probe_reports(service)
    later = submit(service, harness, pinned_model="astra")
    service._admit()
    assert admitted(service, job_id) and admitted(service, later)
    assert service.store.list_attempts(job_id)[0]["lane_id"] == "claude-a"


def test_c11_2_a_retried_request_still_finds_its_job(fleet):
    """C-6.2 the digest keeps the pin as written, so a retry compares what the caller asked for."""
    service, harness = fleet
    args = harness.submit_args(pinned_model="opus", pinned_lane=EMAIL)
    first = service.dispatch("submit", args)
    again = service.dispatch("submit", args)
    assert again == {**first, "created": False}


def test_c11_2_a_dry_run_resolves_the_same_way(incident):
    service, harness = incident
    result = service.dispatch("submit", harness.submit_args(pinned_model=None, task="review", tier="standard",
                                                            pinned_lane=EMAIL, dry_run=True))
    assert result["decision"]["chosen_lane"] == "claude-a"


def test_c11_2_a_reenrolled_lane_is_named_by_its_successor(fleet):
    """C-11.2 a re-enrolment keeps the email on the disabled binding; the email names the live one."""
    service, harness = fleet
    service.store.update_lane("claude-a", enabled=0)
    service.store.put_lane(claude_lane("claude-b", credential="/fake/claude-a"))
    measured(service, "claude-b")
    job_id = submit(service, harness, pinned_model="opus", pinned_lane=EMAIL)
    assert service.store.get_job(job_id)["pinned_lane"] == "claude-b"


def test_c11_2_the_pin_roster_names_lanes_as_the_view_admission_evaluates(incident):
    """C-11.2 one identity set: every name a view lane answers to, the pin roster's lane answers to."""
    service, _ = incident
    service.store.put_lane(claude_lane("claude-old", enabled=False, credential="/fake/claude-a"))
    view = {lane["lane_id"]: lane for lane in service._capacity_view()["lanes"]}
    roster = {lane["lane_id"]: lane for lane in service._pin_roster()}
    assert set(view) == set(roster)
    for lane_id, lane in view.items():
        assert scheduler._identities(roster[lane_id]) == scheduler._identities(lane)
        for key in ("provider", "enabled", "home", "credential_ref"):
            assert roster[lane_id][key] == lane[key]
    assert EMAIL in scheduler._identities(roster["codex-1"])                         # the probe's email is there


# --- C-11.2: the resolver ---------------------------------------------------------------------

ROSTER = [
    {"lane_id": "claude-a", "provider": "claude", "account_key": f"claude:{EMAIL}", "label": EMAIL,
     "home": None, "credential_ref": "claude-quota-a", "enabled": 1},
    {"lane_id": "codex-1", "provider": "codex", "account_key": "codex:uuid-1", "email": EMAIL,
     "home": "/h/codex-1", "credential_ref": "/h/codex-1", "enabled": 1},
    {"lane_id": "claude-c", "provider": "claude", "account_key": "claude:c@example.invalid",
     "label": "c@example.invalid", "home": None, "credential_ref": "claude-quota-c", "enabled": 1},
]


def test_c11_2_resolve_lane_narrows_by_provider_and_refuses_what_is_left_ambiguous():
    assert scheduler.resolve_lane(ROSTER, "codex-1")["lane_id"] == "codex-1"          # a lane id is exact
    assert scheduler.resolve_lane(ROSTER, EMAIL, "claude")["lane_id"] == "claude-a"
    assert scheduler.resolve_lane(ROSTER, EMAIL, "codex")["lane_id"] == "codex-1"
    assert scheduler.resolve_lane(ROSTER, "c@example.invalid")["lane_id"] == "claude-c"
    assert scheduler.resolve_lane(ROSTER, "nobody@example.invalid") is None
    with pytest.raises(scheduler.RouteError, match=r"claude-a, codex-1"):
        scheduler.resolve_lane(ROSTER, EMAIL)
    twins = ROSTER + [{**ROSTER[0], "lane_id": "claude-z", "credential_ref": "claude-quota-z"}]
    with pytest.raises(scheduler.RouteError, match=r"claude-a, claude-z"):
        scheduler.resolve_lane(twins, EMAIL, "claude")                               # same provider: still two
    assert issubclass(scheduler.RouteError, ValueError)                             # old callers still catch it


def test_c11_2_a_superseded_binding_is_dropped_only_for_its_successor():
    old = {**ROSTER[0], "enabled": 0}
    new = {**ROSTER[0], "lane_id": "claude-b"}                                       # same credential, re-enrolled
    assert scheduler.resolve_lane([old, new], EMAIL, "claude")["lane_id"] == "claude-b"
    other = {**ROSTER[0], "lane_id": "claude-b", "credential_ref": "claude-quota-b"}
    with pytest.raises(scheduler.RouteError):
        scheduler.resolve_lane([old, other], EMAIL, "claude")                        # not a successor: ambiguous


def policy():
    from subfleet.policy import load_policy
    from pathlib import Path
    return load_policy(Path(scheduler.__file__).with_name("default_policy.json"))


@pytest.mark.parametrize("job,provider", [
    ({"pinned_model": "fable"}, "claude"),
    ({"pinned_model": "astra"}, "codex"),
    ({"task": "review", "tier": "standard"}, "claude"),
    ({"task": "research", "tier": "hard"}, "codex"),
    ({"task": "review"}, "claude"),                              # no tier is `standard`
    ({"pinned_lane": EMAIL}, None),                              # only the lane could say
    ({"task": "not-a-task"}, None),
])
def test_c11_2_pin_provider_is_the_first_model_evaluate_walks(job, provider):
    assert scheduler.pin_provider(policy(), job) == provider


def test_c11_2_evaluate_and_demand_lanes_narrow_the_same_way():
    view = {"lanes": ROSTER, "readings": [], "closures": [], "now": utcnow()}
    job = {"pinned_lane": EMAIL, "task": "review", "tier": "standard"}
    decision = scheduler.evaluate(policy(), view, job)
    assert [row["lane_id"] for row in decision.evaluations[0]["rejections"]
            + [{"lane_id": lane} for lane in decision.evaluations[0]["candidates"]]] == ["claude-a"]
    assert scheduler.demand_lanes(ROSTER, job, policy()) == frozenset({"claude-a"})
    assert scheduler.demand_lanes(ROSTER, {"pinned_lane": EMAIL}, policy()) is None    # unresolvable: any lane
    with pytest.raises(scheduler.RouteError):
        scheduler.evaluate(policy(), view, {"pinned_lane": EMAIL})


# --- C-11.2: the queue a restart finds --------------------------------------------------------

def test_c11_2_recovery_rewrites_a_queued_pin_to_the_lane_it_names(incident):
    """C-11.2 the one-time repair: a pin accepted before this fix is a lane id after the next start."""
    service, harness = incident
    review = legacy(service, harness, EMAIL, pinned_model=None, task="review", tier="standard")
    bare = legacy(service, harness, EMAIL, pinned_model=None)                         # names two lanes
    unknown = legacy(service, harness, "nobody@example.invalid", pinned_model="opus")
    exact = submit(service, harness, pinned_model="opus", pinned_lane="claude-a")
    done = legacy(service, harness, EMAIL, pinned_model="opus")
    service.store.update_job(done, state="succeeded")
    service._canonicalize_pins()
    pins = {job_id: service.store.get_job(job_id)["pinned_lane"] for job_id in (review, bare, unknown, exact, done)}
    assert pins == {review: "claude-a", bare: EMAIL, unknown: "nobody@example.invalid",
                    exact: "claude-a", done: EMAIL}
    moved = service.store.query("SELECT job_id,data_json FROM events WHERE kind='job.pin_canonicalized'")
    assert [(row["job_id"], json.loads(row["data_json"])) for row in moved] == [
        (review, {"from": EMAIL, "to": "claude-a"})]
    text = log_text(service)
    assert f"job {review} pin {EMAIL!r} is now claude-a" in text
    assert f"job {bare} pin {EMAIL!r} was left" in text
    service._canonicalize_pins()                                                      # a second start changes nothing
    assert len(service.store.query("SELECT 1 FROM events WHERE kind='job.pin_canonicalized'")) == 1


def test_c11_2_recovery_runs_the_repair_before_admission_starts(fleet, monkeypatch):
    service, _ = fleet
    order = []
    monkeypatch.setattr(service, "_canonicalize_pins", lambda: order.append("pins"))
    monkeypatch.setattr(service.timers, "start", lambda: order.append("timers"))
    service._recover_then_start_timers()
    assert order == ["pins", "timers"] and service._recovery_complete.is_set()


def test_c11_2_doctor_names_queued_jobs_whose_pin_is_not_a_lane_id(incident):
    service, harness = incident
    root = service.root
    assert doctor.check_queued_pins(root)["status"] == doctor.PASS
    stuck = legacy(service, harness, EMAIL, pinned_model=None)
    done = legacy(service, harness, "nobody@example.invalid", pinned_model="opus")
    service.store.update_job(done, state="failed")
    row = doctor.check_queued_pins(root)
    assert row["status"] == doctor.FAIL and stuck in row["detail"] and EMAIL in row["detail"]
    assert done not in row["detail"] and "lane id" in row["fix"]


def test_c11_2_doctor_without_a_store_is_not_a_failure(tmp_path):
    assert doctor.check_queued_pins(tmp_path)["status"] == doctor.UNKNOWN


# --- C-6.12: what a route failure settles to ----------------------------------------------------

@pytest.mark.parametrize("call", [1, 2])                            # 1: _prepare_route; 2: inside the reservation
def test_c6_12_an_authorization_the_probe_check_rejects_is_refused_and_the_pass_goes_on(fleet, monkeypatch, call):
    """C-6.12 `probe_required` checks an unmeasured-reserve authorization and is isolated like `evaluate`."""
    service, harness = fleet
    service.policy["caps"]["max_in_flight_per_lane"] = 4
    broken = submit(service, harness, pinned_model="astra")
    later = submit(service, harness, pinned_model="astra")
    real, calls = scheduler.probe_required, []

    def probe_required(decision, job):
        if job["job_id"] == broken:
            calls.append(job["job_id"])
            if len(calls) == call:
                raise scheduler.RouteError("unmeasured_reserve_reason: fixture")
        return real(decision, job)
    monkeypatch.setattr(daemon_module.scheduler, "probe_required", probe_required)
    service._admit()
    assert admitted(service, later)
    assert service.store.get_job(broken)["state"] == "failed" and not service.store.list_attempts(broken)


def test_c6_12_an_authorization_bound_to_a_label_is_refused(fleet):
    """C-11.7a an authorization names the lane id it was granted for; a label in its place is refused."""
    service, harness = fleet
    authorized = submit(service, harness, pinned_model="opus", pinned_lane="claude-a",
                        unmeasured_reserve_reason="fixture: operator accepts unknown reserve")
    service.store.update_job(authorized, pinned_lane=EMAIL)
    later = submit(service, harness, pinned_model="astra")
    service._admit()
    assert admitted(service, later)
    job = service.store.get_job(authorized)
    assert job["state"] == "failed" and job["rc"] == int(Exit.INVALID_INPUT)
    assert "canonical lane id" in service.dispatch("why", {"job_id": authorized})["refused"]


def test_c6_12_a_policy_edit_that_moves_a_tier_to_another_provider_waits_and_fails_nothing(fleet):
    """C-6.12 under a policy other than the one the job was accepted with, a provider conflict is the
    edit's, not the job's: it waits on `route`; under its own policy the same conflict is refused."""
    service, harness = fleet
    edited = submit(service, harness, pinned_model=None, task="review", tier="easy", pinned_lane="claude-a")
    service.policy["chains"]["review"][1] = "astra"                  # review/easy moves from Claude to Codex
    service.policy_digest = "edited-and-restarted"
    service._admit()
    job = service.store.get_job(edited)
    assert job["state"] == "waiting" and job["wait_reason"] == "route"
    assert "different providers" in service._holds[edited]["error"]
    same = submit(service, harness, pinned_model=None, task="review", tier="standard", pinned_lane="claude-a")
    service.store.update_job(same, pinned_lane="codex-1")            # a conflict under the job's own policy
    service._admit()
    assert service.store.get_job(same)["state"] == "failed"


def test_c6_12_a_refused_writable_job_leaves_no_worktree(incident, tmp_path):
    """C-6.12 admission cut `worktrees/<job>` before it evaluated the route; a refusal removes it."""
    from tests.unit.test_salvage import git
    service, harness = incident
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "task/example")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked.txt").write_text("baseline\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    stuck = legacy(service, harness, EMAIL, pinned_model=None, sandbox="workspace-write", workdir=str(repo))
    service._admit()
    assert service.store.get_job(stuck)["state"] == "failed"
    assert not (service.root / "worktrees" / stuck).exists()
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert stuck not in listed


def _deferral_clock(service, job_id):
    row = service.store.get_job(job_id)
    return (datetime.fromisoformat(row["next_check_at"].replace("Z", "+00:00"))
            - datetime.fromisoformat(utcnow().replace("Z", "+00:00"))).total_seconds()


def test_c6_12_route_waits_double_from_5_s_to_a_300_s_ceiling(fleet, monkeypatch):
    """C-6.12 the clock, the event, and a log line that names the type and never the message."""
    service, harness = fleet
    broken = submit(service, harness, pinned_model="astra")
    monkeypatch.setattr(service, "_pick", lambda job, **options: (_ for _ in ()).throw(ValueError("secret-ish detail")))
    clocks = []
    for _ in range(9):
        service.store.update_job(broken, next_check_at=utcnow())
        service._admit()
        clocks.append(_deferral_clock(service, broken))
    assert [round(clock / 5) * 5 for clock in clocks] == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    last = json.loads(service.store.query("SELECT data_json FROM events WHERE kind='job.route_deferred' AND job_id=? "
                                          "ORDER BY event_id DESC LIMIT 1", (broken,))[0]["data_json"])
    assert last["deferrals"] == 9 and last["next_check_at"] == service.store.get_job(broken)["next_check_at"]
    assert last["error"] == "secret-ish detail" and "secret-ish detail" not in log_text(service)


def test_c6_12_an_evaluation_that_succeeds_resets_the_route_count(fleet, monkeypatch):
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 1
    blocker = submit(service, harness, pinned_model="terra")
    service._admit()
    assert admitted(service, blocker)
    broken = submit(service, harness, pinned_model="astra")
    real = service._pick
    failing = lambda job, **options: (_ for _ in ()).throw(KeyError("utilization"))   # noqa: E731
    monkeypatch.setattr(service, "_pick", failing)
    for _ in range(3):
        service.store.update_job(broken, next_check_at=utcnow())
        service._admit()
    assert service._route_deferrals[broken]["deferrals"] == 3
    monkeypatch.setattr(service, "_pick", real)
    service.store.update_job(broken, next_check_at=utcnow())
    service._admit()                                                  # evaluated: held on the full fleet, not placed
    assert broken not in service._route_deferrals and not service.store.list_attempts(broken)
    assert service.store.get_job(broken)["wait_reason"] == "capacity"
    monkeypatch.setattr(service, "_pick", failing)
    service.store.update_job(broken, next_check_at=utcnow())
    service._admit()
    assert service._route_deferrals[broken]["deferrals"] == 1 and round(_deferral_clock(service, broken)) in (4, 5)


def test_c6_12_a_route_hold_outranks_other_holds_and_no_released_lease_hurries_it(fleet, monkeypatch):
    """C-6.11 the `route` hold is reported whatever else holds the job while its clock runs;
    C-6.10's early look on a released lease is for capacity waits only."""
    service, harness = fleet
    broken = submit(service, harness, pinned_model="astra")
    real, looks = service._pick, []

    def pick(job, **options):
        if job["job_id"] == broken:
            looks.append(job["job_id"])
            raise KeyError("utilization")
        return real(job, **options)
    monkeypatch.setattr(service, "_pick", pick)
    service._admit()
    assert service._holds[broken]["reason"] == "route" and len(looks) == 1
    # An older job of its tier and model now waits on capacity: C-6.9 would hold a capacity wait behind it.
    older = submit(service, harness, pinned_model="astra")
    service.store.update_job(older, created_at="2026-01-01T00:00:00Z", state="waiting", wait_reason="capacity",
                             next_check_at=after(600))
    service._admit()
    assert service._holds[broken]["reason"] == "route" and service._holds[broken]["error_type"] == "KeyError"
    clock = service.store.get_job(broken)["next_check_at"]
    with service.store.transaction("fixture.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES('out:/fixture','someone',?)", (utcnow(),))
    service._admit()
    with service.store.transaction("fixture.release") as tx:
        tx.execute("DELETE FROM leases WHERE lease_key='out:/fixture'")
    service._admit()                                                  # a lease was released: not a route wait's cue
    assert len(looks) == 1 and service.store.get_job(broken)["next_check_at"] == clock


def test_c6_12_a_fleet_held_only_by_route_waits_warns(fleet, monkeypatch):
    """C-6.11 a route wait is not ordinary queueing: beside an open lane the idle line is a warning."""
    from tests.fake.test_admission_visibility import age
    service, harness = fleet
    seen = []
    service.log.addHandler(type("Catch", (logging.Handler,), {"emit": lambda self, record: seen.append(record)})())
    submit(service, harness, pinned_model="astra")
    monkeypatch.setattr(service, "_pick", lambda job, **options: (_ for _ in ()).throw(KeyError("utilization")))
    service._admit()
    age(service, 61)
    service._admit()
    idle = [record for record in seen if record.getMessage().startswith("admission:")]
    assert [record.levelno for record in idle] == [logging.WARNING] and "route x1" in idle[0].getMessage()


def test_c6_12_a_pass_with_nothing_to_look_at_builds_no_capacity_view(fleet, monkeypatch):
    """C-6.10 an admission pass runs every tick; the pin roster must not cost a view (about 50 ms live)."""
    service, harness = fleet
    waiting = submit(service, harness, pinned_model="astra")
    service.store.update_job(waiting, state="waiting", wait_reason="capacity", next_check_at=after(600))
    monkeypatch.setattr(service, "_capacity_view", lambda *args, **kwargs: pytest.fail("built a capacity view"))
    service._admit()
    service.kill(protocol.KillArgs(waiting))
    service._admit()


def test_c6_12_an_empty_task_or_tier_is_none(fleet):
    """C-6.12 submit accepted tier '' and `evaluate` rejected it on every pass."""
    service, harness = fleet
    job_id = submit(service, harness, pinned_model=None, task="review", tier="", pinned_lane=EMAIL)
    job = service.store.get_job(job_id)
    assert job["tier"] is None and job["pinned_lane"] == "claude-a"
    service._admit()
    assert admitted(service, job_id)


# --- C-11.2: pins after submit ------------------------------------------------------------------

def test_c11_2_a_retry_after_the_probe_reports_the_email_still_finds_its_job(fleet):
    """C-6.2 a retry is answered from the request it repeats, though its name now names two lanes."""
    service, harness = fleet
    args = harness.submit_args(pinned_model=None, pinned_lane=EMAIL)
    first = service.dispatch("submit", args)
    codex_probe_reports(service)
    assert service.dispatch("submit", args) == {**first, "created": False}


def test_c11_2_a_retry_after_a_reenrolment_still_finds_its_job(fleet):
    """C-6.2 the digest keeps the name as typed, so a retry is not a new payload after a re-enrolment."""
    service, harness = fleet
    args = harness.submit_args(pinned_model="opus", pinned_lane=EMAIL)
    first = service.dispatch("submit", args)
    service.store.update_lane("claude-a", enabled=0)
    service.store.put_lane(claude_lane("claude-b", credential="/fake/claude-a"))
    assert service.dispatch("submit", args) == {**first, "created": False}


def test_c11_2_a_pin_follows_its_lane_through_a_reenrolment(fleet):
    """C-11.2, C-10.2 a job pinned before its lane was re-enrolled runs on the successor, the same
    account on the same credential; one carrying an authorization stays on the id it was granted for."""
    service, harness = fleet
    pinned = submit(service, harness, pinned_model="opus", pinned_lane=EMAIL)
    authorized = submit(service, harness, pinned_model="opus", pinned_lane="claude-a",
                        unmeasured_reserve_reason="fixture: operator accepts unknown reserve")
    service.store.update_lane("claude-a", enabled=0)
    service.store.put_lane(claude_lane("claude-b", credential="/fake/claude-a"))
    measured(service, "claude-b")
    service._admit()
    assert service.store.list_attempts(pinned)[0]["lane_id"] == "claude-b"
    assert not service.store.list_attempts(authorized)
    assert service.store.get_job(authorized)["state"] == "waiting"
    assert scheduler.resolve_lane(service._pin_roster(), "claude-a")["lane_id"] == "claude-b"
    assert scheduler.resolve_lane(service._pin_roster(), "claude-a", follow=False)["lane_id"] == "claude-a"


@pytest.mark.parametrize("flag,changes,outcome", [
    ("claude", {"pinned_model": None}, "claude-a"),                                  # bare -a EMAIL: the Claude account
    ("claude", {"pinned_model": "opus"}, "claude-a"),
    ("claude", {"pinned_model": "astra"}, "refused"),                                # -a names a Claude account
    ("claude", {"pinned_model": None, "task": "research", "tier": "hard"}, "refused"),
    ("codex", {"pinned_model": "astra"}, "codex-1"),                                 # -H with the email the probe reported
    (None, {"pinned_model": "astra"}, "codex-1"),                                    # no flag (a gate, an old client)
])
def test_c11_2_the_flag_names_the_provider_of_a_name(incident, flag, changes, outcome):
    """C-17.2 `-a EMAIL` pins a Claude account and `-H` a Codex home, as v1 had it; a lane id is its own provider."""
    service, harness = incident
    args = harness.submit_args(pinned_lane=EMAIL, pinned_provider=flag, **changes)
    if outcome == "refused":
        with pytest.raises(protocol.ProtocolError, match="was given as a claude lane"):
            service.dispatch("submit", args)
        return
    job_id = service.dispatch("submit", args)["job_id"]
    assert service.store.get_job(job_id)["pinned_lane"] == outcome
    exact = submit(service, harness, pinned_model="astra", pinned_lane="codex-1", pinned_provider="claude")
    assert service.store.get_job(exact)["pinned_lane"] == "codex-1"


def test_c11_2_a_probe_that_cannot_read_the_account_does_not_unname_it(incident):
    """C-11.2 a network error replaced the verdict and the Codex email with it, so one `-a` name
    resolved differently from one minute to the next."""
    service, _ = incident
    lane = service.store.get_lane("codex-1")
    service.timers._persist(lane, {"status": "network-error", "readings": (), "probed_at": utcnow(),
                                   "error_type": "TimeoutError"})
    assert service.timers.metadata["codex-1"]["email"] == EMAIL
    service.timers._persist(lane, {"status": "ok", "readings": (), "probed_at": utcnow(),
                                   "account_key": "codex:someone-else"})       # identity-mismatch: nothing carried
    assert "email" not in service.timers.metadata["codex-1"]


def test_c11_2_recovery_and_doctor_cover_running_jobs(incident):
    """C-11.2 a running job's pin routes its next attempt (a retry after `limited`)."""
    service, harness = incident
    running = legacy(service, harness, EMAIL, pinned_model=None, task="review", tier="standard")
    service.store.update_job(running, state="running")
    service._canonicalize_pins()
    assert service.store.get_job(running)["pinned_lane"] == "claude-a"
    service.store.update_job(running, pinned_lane=EMAIL)
    row = doctor.check_queued_pins(service.root)
    assert row["status"] == doctor.FAIL and f"{running} (running)" in row["detail"]


def test_c11_2_a_restart_repairs_pins_with_the_persisted_probe_email(incident):
    """C-11.2 the repair runs at a start, where the Codex email exists only in `timer.verdict` events."""
    service, harness = incident
    service.store.add_event("timer.verdict", lane_id="codex-1", data={"email": EMAIL, "probe_status": "ok"})
    job_id = legacy(service, harness, EMAIL, pinned_model=None, task="research", tier="hard", pinned_lane="codex-1")
    service.close()
    fresh = Daemon(service.root)
    try:
        fresh._canonicalize_pins()
        assert fresh.store.get_job(job_id)["pinned_lane"] == "codex-1"
    finally:
        fresh.close()


def test_c11_2_a_repair_that_raises_never_blocks_recovery(fleet, monkeypatch):
    service, _ = fleet
    monkeypatch.setattr(service, "_pin_roster", lambda: (_ for _ in ()).throw(KeyError("lane_id")))
    monkeypatch.setattr(service.timers, "start", lambda: None)
    service._recover_then_start_timers()
    assert service._recovery_complete.is_set() and "pin repair skipped: KeyError" in log_text(service)


def test_c6_12_a_transient_retry_on_a_model_id_the_policy_dropped_routes_the_job_itself(fleet):
    """C-6.12 the retry pins the last attempt's model id; after an id rename (claude-opus-5 -> -5-5 on
    2026-09-22) that pair is the daemon's, so the job is routed as submitted rather than stranded."""
    service, harness = fleet
    job_id = submit(service, harness, pinned_model=None, task="review", tier="standard")
    service.store.add_attempt(attempt_id=job_id + "/a1", job_id=job_id, seq=1, lane_id="claude-a",
                              model_requested="claude-opus-5", state="failed", outcome_class="transient")
    service._admit()
    attempts = service.store.list_attempts(job_id)
    assert [row["state"] for row in attempts] == ["failed", "reserved"]
    assert attempts[-1]["model_requested"] == service.policy["models"]["opus"]["id"]
    assert job_id not in service._route_deferrals


def test_c11_2_a_lane_that_proved_to_hold_another_account_does_not_answer_to_its_email(fleet):
    """C-10.6 a mismatched credential's probe reads the other account's email; the lane keeps it as
    `observed_email`, and a name both it and the account's own lane answer to names the account's lane."""
    service, harness = fleet
    service.store.put_lane(Lane("codex-2", "codex", "codex:bob-id", Credential("codex", "/fake/codex-2", "home"),
                                "/fake/codex-2", LaneOwner.V2, False))
    codex_probe_reports(service, "bob@example.invalid", "codex-2")
    service.timers._persist(service.store.get_lane("codex-1"), {
        "status": "ok", "readings": (), "probed_at": utcnow(), "account_key": "codex:bob-id",
        "email": "bob@example.invalid"})
    meta = service.timers.metadata["codex-1"]
    assert "email" not in meta and meta["observed_email"] == "bob@example.invalid"
    job_id = submit(service, harness, pinned_model="astra", pinned_lane="bob@example.invalid", pinned_provider="codex")
    assert service.store.get_job(job_id)["pinned_lane"] == "codex-2"
    stale = [{**lane, "email": "bob@example.invalid"} if lane["lane_id"] == "codex-1" else lane
             for lane in service._pin_roster()]                           # a verdict stored before this rule
    assert scheduler.resolve_lane(stale, "bob@example.invalid", "codex")["lane_id"] == "codex-2"


def test_c6_12_a_transient_retry_whose_id_is_retired_to_another_provider_routes_the_job_itself(fleet):
    """C-6.12 a `retired` alias may point at another provider's model; the retry pair then can never
    run on its lane, so the job routes as submitted instead of waiting until `max_wall_s`."""
    service, harness = fleet
    job_id = submit(service, harness, pinned_model=None, task="review", tier="standard")
    service.store.add_attempt(attempt_id=job_id + "/a1", job_id=job_id, seq=1, lane_id="claude-a",
                              model_requested="claude-old-haiku", state="failed", outcome_class="transient")
    service.policy["retired"]["claude-old-haiku"] = "astra"
    service.policy_digest = "edited-and-restarted"
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(job_id)] == ["failed", "reserved"]
    assert job_id not in service._route_deferrals


def test_c6_12_a_transient_retry_whose_lane_was_disabled_since_routes_the_job_itself(fleet):
    """C-4.5 an auth-dead lane is never re-enabled; a retry pinned to it would never be placed."""
    service, harness = fleet
    job_id = submit(service, harness, pinned_model=None, task="review", tier="standard")
    service.store.add_attempt(attempt_id=job_id + "/a1", job_id=job_id, seq=1, lane_id="claude-a",
                              model_requested=service.policy["models"]["opus"]["id"], state="failed",
                              outcome_class="transient")
    service.store.update_lane("claude-a", enabled=0)
    service.timers.record_auth_dead("claude-a")
    service.store.put_lane(claude_lane("claude-b", label="other@example.invalid"))
    measured(service, "claude-b")
    service._admit()
    assert service.store.list_attempts(job_id)[-1]["lane_id"] == "claude-b"


def test_c11_2_a_mismatched_lane_is_dropped_only_within_one_provider(incident):
    """C-11.2 across providers a name stays ambiguous: a provider-less legacy pin whose Claude lane is
    identity-blocked is left for admission to refuse, never moved to the Codex lane and its first model."""
    service, harness = incident
    job_id = legacy(service, harness, EMAIL, pinned_model=None)
    service.store.update_lane("claude-a", identity_status="mismatch")
    service._canonicalize_pins()
    assert service.store.get_job(job_id)["pinned_lane"] == EMAIL
    service._admit()
    assert service.store.get_job(job_id)["state"] == "failed" and not service.store.list_attempts(job_id)


def _transient_on(service, job_id, lane_id, model_id):
    service.store.add_attempt(attempt_id=job_id + "/a1", job_id=job_id, seq=1, lane_id=lane_id,
                              model_requested=model_id, state="failed", outcome_class="transient")


@pytest.mark.parametrize("refusal", ["closed", "desktop", "excluded"])
def test_c4_5_a_retry_whose_lane_refuses_it_for_more_than_a_slot_goes_to_the_next_candidate(fleet, refusal):
    """C-4.5 "same lane after 60 s, once, then next candidate": a closure, the desktop login, or the
    job's own exclusion is not ended by a slot, so the job routes as submitted onto the open lane."""
    service, harness = fleet
    service.store.put_lane(claude_lane("claude-b", label="other@example.invalid"))
    measured(service, "claude-b")
    exclusions = ["claude-a"] if refusal == "excluded" else []
    job_id = submit(service, harness, pinned_model=None, task="review", tier="standard", exclusions=exclusions)
    _transient_on(service, job_id, "claude-a", service.policy["models"]["opus"]["id"])
    if refusal == "closed":
        service.store.add_closure(Closure("claude-a", "account", after(3 * 86400), ClosureReason.PROVIDER_LIMIT,
                                          ClockSource.REPORTED, "fixture"))
    elif refusal == "desktop":
        service.store.update_lane("claude-a", desktop=1)
    service._admit()
    assert service.store.list_attempts(job_id)[-1]["lane_id"] == "claude-b"


def test_c4_5_a_retry_whose_lane_is_only_full_keeps_its_lane(fleet):
    """C-4.5 a slot ends a full lane's refusal: the retry waits for its own lane, not the open one."""
    service, harness = fleet
    service.store.put_lane(claude_lane("claude-b", label="other@example.invalid"))
    measured(service, "claude-b")
    for _ in range(2):                                             # claude-a's two measured slots, taken
        busy = submit(service, harness, pinned_model="opus", pinned_lane="claude-a")
        service._admit()
        assert admitted(service, busy)
    job_id = submit(service, harness, pinned_model=None, task="review", tier="standard")
    _transient_on(service, job_id, "claude-a", service.policy["models"]["opus"]["id"])
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(job_id)] == ["failed"]
    assert service._holds[job_id]["reason"] == "no-slot"


def test_c6_9_a_pinned_retry_competes_only_for_its_own_pair(fleet):
    """C-6.9 while a retry is pinned to (claude-a, opus) it holds back nothing pinned elsewhere."""
    service, harness = fleet
    service.store.put_lane(claude_lane("claude-b", label="other@example.invalid"))
    measured(service, "claude-b")
    retry = submit(service, harness, pinned_model=None, task="review", tier="standard")
    _transient_on(service, retry, "claude-a", service.policy["models"]["opus"]["id"])
    service.store.update_job(retry, state="waiting", wait_reason="capacity", next_check_at=after(60))
    astra = submit(service, harness, pinned_model="astra")
    other_lane = submit(service, harness, pinned_model="opus", pinned_lane="claude-b")
    service._admit()
    assert admitted(service, astra) and admitted(service, other_lane)


def test_c4_5_a_retry_whose_credential_is_latched_goes_to_the_next_candidate(fleet):
    """C-4.5 a revoked Codex login stays enabled but latched (`credential-latched`, a `no-slot` no slot ends)."""
    service, harness = fleet
    service.store.put_lane(Lane("codex-2", "codex", "codex:other", Credential("codex", "/fake/codex-2", "home"),
                                "/fake/codex-2", LaneOwner.V2, False))
    measured(service, "codex-2")
    job_id = submit(service, harness, pinned_model="astra")
    _transient_on(service, job_id, "codex-1", service.policy["models"]["astra"]["id"])
    service.timers.metadata["codex-1"] = {"probe_status": "revoked", "revoked_epoch": 1}
    service._admit()
    assert service.store.list_attempts(job_id)[-1]["lane_id"] == "codex-2"


def test_c6_9_a_retry_that_lets_its_pin_go_keeps_its_place_behind_older_jobs(fleet):
    """C-6.9 routed as submitted, a fallen-back retry competes as submitted: it does not pass an older
    waiter it competes with, and a younger job it competes with does not pass it."""
    service, harness = fleet
    service.store.put_lane(claude_lane("claude-b", label="other@example.invalid"))
    measured(service, "claude-b")
    older = submit(service, harness, pinned_model="opus", pinned_lane="claude-b")
    service.store.update_job(older, state="waiting", wait_reason="capacity", next_check_at=after(600))
    retry = submit(service, harness, pinned_model=None, task="review", tier="standard")
    _transient_on(service, retry, "claude-a", service.policy["models"]["opus"]["id"])
    service.store.add_closure(Closure("claude-a", "account", after(3 * 86400), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    looks = {"workspace": 0, "pick": 0}
    real_workspace, real_pick = service._workspace, service._pick
    service._workspace = lambda job: looks.__setitem__("workspace", looks["workspace"] + 1) or real_workspace(job)
    service._pick = lambda job, **options: looks.__setitem__("pick", looks["pick"] + 1) or real_pick(job, **options)
    service._admit()
    hold = service._holds[retry]
    assert {key: hold[key] for key in ("reason", "behind", "tier")} == {
        "reason": "behind-older-job", "behind": older, "tier": "standard"}
    assert [row["state"] for row in service.store.list_attempts(retry)] == ["failed"]
    # C-6.10: held after a look, so on a clock: the passes before it is due prepare nothing and score nothing.
    job = service.store.get_job(retry)
    assert job["state"] == "waiting" and job["wait_reason"] == "capacity" and job["next_check_at"] > utcnow()
    before = dict(looks)
    for _ in range(5):
        service._admit()
    assert looks == before and service._holds[retry]["reason"] == "behind-older-job"


def test_c6_9_a_younger_job_does_not_pass_a_retry_that_let_its_pin_go(fleet):
    service, harness = fleet
    service.store.put_lane(claude_lane("claude-b", label="other@example.invalid", enabled=False))
    measured(service, "claude-b")
    for lane_id in ("claude-a", "codex-1"):
        service.store.add_closure(Closure(lane_id, "account", after(3 * 86400), ClosureReason.PROVIDER_LIMIT,
                                          ClockSource.REPORTED, "fixture"))
    retry = submit(service, harness, pinned_model=None, task="review", tier="standard")
    _transient_on(service, retry, "claude-a", service.policy["models"]["opus"]["id"])
    service._admit()                                                  # falls back; nothing admits it as submitted
    assert service.store.get_job(retry)["state"] == "waiting" and service._retry_verdicts[retry][1] is False
    service.store.update_lane("claude-b", enabled=1)
    younger = submit(service, harness, pinned_model="opus", pinned_lane="claude-b")
    service._admit()                                                  # the retry's clock runs; the younger job waits
    assert service._holds[younger]["reason"] == "behind-older-job" and service._holds[younger]["behind"] == retry


def test_c4_5_a_retry_let_go_is_evaluated_again_when_it_is_next_due(fleet):
    """C-4.5 the one same-pair retry survives a refusal that ends: the next due look evaluates the pair,
    with or without a restart in between (the look's verdict is in memory)."""
    service, harness = fleet
    service.store.add_closure(Closure("codex-1", "account", after(3 * 86400), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    job_id = submit(service, harness, pinned_model=None, task="review", tier="standard")
    fable = service.policy["models"]["fable"]["id"]
    _transient_on(service, job_id, "claude-a", fable)                  # the pair: fable, not the chain's opus
    service.store.update_lane("claude-a", desktop=1)
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(job_id)] == ["failed"]
    assert service._retry_verdicts[job_id][1] is False
    service.store.update_lane("claude-a", desktop=0)
    service.store.update_job(job_id, next_check_at=utcnow())
    service._admit()
    last = service.store.list_attempts(job_id)[-1]
    assert (last["state"], last["lane_id"], last["model_requested"]) == ("reserved", "claude-a", fable)


def test_c6_9_a_due_retry_is_looked_at_as_its_pair_before_it_is_evaluated(fleet):
    """C-6.9 a due look evaluates the pair, so until then the job's demand is the pair: an older waiter
    pinned to another lane does not hold it, whatever the last look decided."""
    service, harness = fleet
    service.store.put_lane(claude_lane("claude-b", label="other@example.invalid"))
    measured(service, "claude-b")
    service.store.add_closure(Closure("codex-1", "account", after(3 * 86400), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    older = submit(service, harness, pinned_model="opus", pinned_lane="claude-b")
    service.store.update_job(older, state="waiting", wait_reason="capacity", next_check_at=after(600))
    job_id = submit(service, harness, pinned_model=None, task="review", tier="standard")
    fable = service.policy["models"]["fable"]["id"]
    _transient_on(service, job_id, "claude-a", fable)
    service._retry_verdicts[job_id] = (job_id + "/a1", False)         # a look let the pin go
    service._admit()
    last = service.store.list_attempts(job_id)[-1]
    assert (last["state"], last["lane_id"], last["model_requested"]) == ("reserved", "claude-a", fable)


def test_c4_5_a_retry_that_followed_a_reenrolment_counts_the_account_once(fleet):
    """C-4.5 "same lane, once, then next candidate": attempts on a lane and on its re-enrolled successor
    are attempts on one lane, so a second transient there excludes it and the job moves on."""
    service, harness = fleet
    service.store.put_lane(claude_lane("claude-b", label="other@example.invalid"))
    measured(service, "claude-b")
    job_id = submit(service, harness, pinned_model=None, task="review", tier="standard")
    opus = service.policy["models"]["opus"]["id"]
    _transient_on(service, job_id, "claude-a", opus)
    service.store.update_lane("claude-a", enabled=0)
    service.store.put_lane(claude_lane("claude-a2", credential="/fake/claude-a"))   # re-enrolled
    measured(service, "claude-a2")
    service._admit()
    second = service.store.list_attempts(job_id)[-1]
    assert second["lane_id"] == "claude-a2"                          # the one retry followed the lane
    service.store.update_attempt(second["attempt_id"], state="failed", outcome_class="transient")
    service.store.update_job(job_id, state="waiting", wait_reason="capacity", next_check_at=utcnow())
    with service.store.transaction("fixture.release") as tx:
        tx.execute("DELETE FROM leases WHERE holder=?", (second["attempt_id"],))
    service._admit()
    assert service.store.list_attempts(job_id)[-1]["lane_id"] == "claude-b"
    assert scheduler.current_lane_id(service._pin_roster(), "claude-a") == "claude-a2"


def test_c4_5_finalization_counts_transients_on_a_lane_and_its_successor_as_one(fleet):
    """C-4.5 a lane-pinned job gets one same-lane retry, also across a re-enrolment of its lane."""
    service, harness = fleet
    job_id = submit(service, harness, pinned_model="opus", pinned_lane="claude-a")
    opus = service.policy["models"]["opus"]["id"]
    _transient_on(service, job_id, "claude-a", opus)
    service.store.update_lane("claude-a", enabled=0)
    service.store.put_lane(claude_lane("claude-a2", credential="/fake/claude-a"))
    service.store.add_attempt(attempt_id=job_id + "/a2", job_id=job_id, seq=2, lane_id="claude-a2",
                              model_requested=opus, state="finalizing")
    second = service.store.get_attempt(job_id + "/a2")
    assert service._earlier_transients(service.store.connection, job_id, second) == 1
    service.store.put_lane(claude_lane("claude-z", label="z@example.invalid"))
    elsewhere = {**second, "lane_id": "claude-z"}
    assert service._earlier_transients(service.store.connection, job_id, elsewhere) == 0


@pytest.mark.parametrize("error", [AttributeError("'NoneType' object has no attribute 'get'"),
                                   IndexError("list index out of range")])
def test_c6_12_any_evaluation_error_of_one_job_is_that_jobs(fleet, monkeypatch, error):
    """C-6.12 an AttributeError or IndexError raised while evaluating one job's route (a malformed
    policy such as a `reserve` list, or a defect in evaluation or the capacity view) is handled like a
    KeyError: the job waits on `route` and the pass goes on."""
    service, harness = fleet
    broken = submit(service, harness, pinned_model="astra")
    later = submit(service, harness, pinned_model="terra")
    real = service._pick

    def pick(job, **options):
        if job["job_id"] == broken:
            raise error
        return real(job, **options)
    monkeypatch.setattr(service, "_pick", pick)
    service._admit()
    assert admitted(service, later)
    job = service.store.get_job(broken)
    assert job["state"] == "waiting" and job["wait_reason"] == "route"
    assert service._holds[broken]["error_type"] == type(error).__name__
