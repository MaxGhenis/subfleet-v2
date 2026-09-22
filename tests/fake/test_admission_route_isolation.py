"""C-6.12 and C-11.2: one job whose route cannot be evaluated never stops admission.

Incident, 2026-09-22 11:40–14:05 EDT: `worker admission failed: ValueError (128 in
a row)` and nothing was placed for any session. Five queued jobs were pinned by
email (`-a max@axiom.org`, `-a max@thesisinstitute.org`) with a task and no model.
Submit resolved the email against the store's lane rows, where Codex lanes carry
no email, found one Claude lane, and kept the raw email as the pin. Admission
evaluates against the capacity view, where the usage probe has given each Codex
lane its email, so the same email named a Claude lane and a Codex lane and
`resolve_lane` raised "ambiguous lane". Nothing in `_admit_pass` caught it per
job, so the pass aborted on every tick and every later job in every tier
starved; `why` caught it and printed "No decision recorded.". The same shape
produced a 16-failure burst at 09:41 that day.

These tests build that roster: `claude-a` labelled `max@example.invalid`, and the
fixture's `codex-1` whose probe reported the same email.
"""

import json

import pytest

from subfleet import doctor, protocol, scheduler
from subfleet import daemon as daemon_module
from subfleet.contracts import Credential, Exit, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.daemon import after, utcnow
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


def test_c6_12_an_evaluation_error_holds_the_job_visibly_and_holds_nobody_else(fleet, monkeypatch):
    """C-6.12 an error that is not the job's own (bad capacity data, a policy edit) is never terminal:
    the job waits on `route` with a backoff clock, `why` and `status` name the error, and it is no
    C-6.9 waiter, so later jobs of its tier and model still pass it."""
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
    answer = service.dispatch("why", {"job_id": broken})
    assert "route could not be evaluated" in answer["text"] and "KeyError" in answer["text"]
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


def test_c6_12_the_evaluation_inside_the_reservation_is_isolated_too(fleet, monkeypatch):
    """C-6.12 `_pick` runs twice per placement; the roster can change between them."""
    service, harness = fleet
    raced = submit(service, harness, pinned_model="astra")
    later = submit(service, harness, pinned_model="terra")
    real, calls = service._pick, {}

    def pick(job, **options):
        calls[job["job_id"]] = calls.get(job["job_id"], 0) + 1
        if job["job_id"] == raced and calls[raced] == 2:
            raise scheduler.RouteError(f"pinned_lane: {EMAIL!r} names 2 lanes (claude-a, codex-1)")
        return real(job, **options)
    monkeypatch.setattr(service, "_pick", pick)
    service._admit()
    assert admitted(service, later)
    assert service.store.get_job(raced)["state"] == "failed"
    assert not service.store.list_attempts(raced)
    assert not service.store.query("SELECT 1 FROM leases WHERE holder LIKE ?", (raced + "%",))


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
