"""C-4.1, C-6.9–C-6.12: the admission properties' counterexamples, each reduced to one scenario.

`test_admission_stateful.py` searches for violations of four properties of the
admission pass (P1 totality, P2 non-interference, P3 bounded progress, P4 recheck
cost). Every counterexample it or its reviewers found is pinned here as the
smallest scenario that shows it:

- fixed in this change, or already by release/217's design, as a regression test;
- not fixed, as a strict `xfail` that states what the contract (or the property)
  requires. Each fails today, at its last assertion (`raises=AssertionError` keeps
  any other error from hiding behind the marker). When a fix lands it passes,
  `strict` turns that into a failure, and the marker must come off with the fix.

C-6.9's hold-back and slot-keeping exist only in a pool with a count cap (C-6.4's
default is none), so the scenarios about them set one.

Found with the stateful search and the adversarial review of 2026-09-24
(`~/reviews/formal-verification-2026-09-24/verify-s7-subfleet.md`, M1).
"""

import json

import pytest

from subfleet import scheduler
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner,
                                Outcome, OutcomeClass, Reading, ReadingLabel)
from subfleet.daemon import Unroutable, after, utcnow
from subfleet.procs import Containment
from subfleet import protocol
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)

REASON = "Operator authorizes this job on this lane; the reserve is unmeasured and quota unverified."


def lane(lane_id, provider="claude"):
    return Lane(lane_id, provider, f"{provider}:{lane_id}@example.invalid",
                Credential(provider, f"/fake/{lane_id}", "home"), f"/fake/{lane_id}", LaneOwner.V2, False)


def measured(service, lane_id, utilization=.2):
    service.store.add_reading(Reading(lane_id, "account", "seven_day", utilization, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))


def closed(service, lane_id, scope="account", seconds=6 * 3600):
    service.store.add_closure(Closure(lane_id, scope, after(seconds), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))


def submit(service, harness, **changes):
    return service.dispatch("submit", harness.submit_args(**changes))["job_id"]


def waiting(service, job_id, seconds=30):
    service.store.update_job(job_id, state="waiting", wait_reason="capacity", next_check_at=after(seconds))


def due(service, job_id):
    service.store.update_job(job_id, next_check_at=utcnow())


def placed_on(service, job_id):
    attempts = [row for row in service.store.list_attempts(job_id) if row["state"] == "reserved"]
    return attempts[-1]["lane_id"] if attempts else None


def end(service, job_id):
    """The job's attempt ends and it leaves the queue (only its leases and rows matter here)."""
    attempt = service.store.list_attempts(job_id)[-1]["attempt_id"]
    with service.store.transaction("fixture.attempt_ended") as tx:
        tx.execute("UPDATE attempts SET state='failed' WHERE attempt_id=?", (attempt,))
        tx.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (job_id,))
        tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (attempt, job_id))


def rows(service, job_id):
    return [row for row in service.store.list_decisions(job_id) if row["attempt_id"] is None]


@pytest.fixture
def fleet(routing_state):  # noqa: F811
    """codex-1 and claude-a, both measured, in a pool with a count cap far above what runs here."""
    service, harness = routing_state
    service.policy["caps"]["max_active_attempts"] = 10
    service.store.put_lane(lane("claude-a"))
    for lane_id in ("codex-1", "claude-a"):
        measured(service, lane_id)
    return service, harness


# --- fixed here -------------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["pinned-task-waits", "pinned-task-arrives", "same-model"])
def test_c6_9_a_lane_pinned_task_competes_only_on_the_model_it_walks(fleet, case):
    """C-6.9, C-11.2 (P2): "a task job's [models] are its chain from its tier upward, exactly the chain
    routing walks", and a lane-pinned job "evaluates one model ... the first model of its task's chain
    from its tier". `demand_models` gave a review job pinned to claude-a its whole chain, {opus, astra},
    so it held back, and was held back by, Astra work it can never share a model with."""
    service, harness = fleet
    pinned = dict(pinned_model=None, task="review", tier="standard", pinned_lane="claude-a")   # walks opus
    other = dict(pinned_model="opus" if case == "same-model" else "astra")
    older = submit(service, harness, **(other if case == "pinned-task-arrives" else pinned))
    newer = submit(service, harness, **(pinned if case == "pinned-task-arrives" else other))
    lane_pinned = newer if case == "pinned-task-arrives" else older
    assert scheduler.demand_models(service.policy, service.store.get_job(lane_pinned)) == frozenset({"opus"})
    waiting(service, older)
    service._admit()
    if case == "same-model":
        # An Opus job still competes with it: the pin narrows the models, not the rule.
        assert service._holds[newer] == {"reason": "behind-older-job", "behind": older, "tier": "standard"}
    else:
        assert placed_on(service, newer) == ("claude-a" if case == "pinned-task-arrives" else "codex-1")


def test_c6_10_a_repeated_verdict_is_not_recorded_again_as_the_fleet_fills_and_empties(fleet):
    """C-6.10 (P4): "a recheck that reaches the verdict it reached last time adds no decision row", and
    the verdict is `scheduler.verdict_signature`. The wait was keyed on the signature plus whether the
    fleet was at its limit, so a job no lane admits got a new row, and its clock went back to 1 s, each
    time other work filled or emptied the fleet: 48 to 480 rows an hour per waiting job."""
    service, harness = fleet
    service.store.update_lane("claude-a", enabled=0)        # no Claude lane: an Opus job has no candidate
    service.policy["caps"]["max_active_attempts"] = 2
    stuck = submit(service, harness, pinned_model="opus")
    fillers = [submit(service, harness, pinned_model="terra", tier="hard") for _ in range(2)]
    service._admit()                                        # stuck looks at an empty fleet; the fillers start
    assert all(placed_on(service, job) for job in fillers)
    for _ in range(3):
        due(service, stuck)
        service._admit()                                    # at the fleet's limit
        end(service, fillers.pop())
        fillers.append(submit(service, harness, pinned_model="terra", tier="hard"))
        due(service, stuck)
        service._admit()                                    # below it (the new filler starts after the look)
    assert len(rows(service, stuck)) == 1
    assert service._capacity_waits[stuck]["rechecks"] == 6


def test_c4_1_a_probe_that_quarantines_leaves_its_job_uncertain_and_holding_nobody(routing_state):  # noqa: F811
    """C-4.1 "Only `capacity` holds back the jobs behind it in its tier"; C-6.11 "A wait that is a
    person's or a retry's to end (`approval`, `uncertain`, `workspace`) is reported as that". On the
    pass whose probe could not be contained, the job, already `uncertain`, was still registered as a
    waiter and reported `probe-pending`, so a later job that could run elsewhere waited a pass."""
    service, harness = routing_state
    service.store.put_lane(lane("codex-2", "codex"))
    measured(service, "codex-2")                            # codex-1 is unmeasured: hard work there probes
    service.term_grace_s = 0
    service._probe_census = lambda record: Containment(unverifiable=True)

    def quarantined(job, lane_row, model, holder):          # what `_execute_probe` returns when it cannot contain
        assert not service._contain_probe(service._probe_record(holder))
        return Outcome(OutcomeClass.UNKNOWN, "probe containment is quarantined", evidence={"probe_quarantined": True})
    service._execute_probe = quarantined
    probing = submit(service, harness, pinned_model=None, task="review", tier="hard", exclusions=["codex-2"])
    later = submit(service, harness, pinned_model="astra", tier="hard")
    service._admit()
    assert service.store.get_job(probing)["wait_reason"] == "uncertain"
    assert service._holds[probing] == {"reason": "uncertain"}
    assert placed_on(service, later) == "codex-2"
    assert service._admission["reasons"].get("probe-pending") is None


@pytest.mark.parametrize("exclusions", [[1], [None], [True], "codex-1"])
def test_c6_12_submit_refuses_exclusions_that_are_not_lane_names(routing_state, exclusions):  # noqa: F811
    """C-6.12: admission merges a job's exclusions with lane ids. Submit sorted them only after its
    validation, so a one-element list of anything was stored and a longer mixed one raised there."""
    service, harness = routing_state
    with pytest.raises(protocol.ProtocolError) as refused:
        service.dispatch("submit", harness.submit_args(exclusions=exclusions))
    assert refused.value.code == 2 and service.store.list_jobs() == []


def test_c6_12_a_stored_exclusion_that_is_not_a_lane_name_is_its_jobs_problem(routing_state):  # noqa: F811
    """C-6.12 (P1): "A job whose route cannot be evaluated is that job's problem and never the pass's."
    A row holding `[1]` (an older submit stored one) raised TypeError from the retry-exclusion merge
    inside the reserving transaction, which isolated only route evaluation, so every pass ended there.
    It now waits on `route` like any other error that is not the job's own, backing off 5 s, 10 s, 20 s:
    the merge is checked where the route is prepared, so a failure is never counted from a reset."""
    service, harness = routing_state
    service.store.put_lane(lane("codex-2", "codex"))
    for lane_id in ("codex-1", "codex-2"):
        measured(service, lane_id)
    stuck = submit(service, harness, pinned_model="astra")
    service.store.update_job(stuck, exclusions="[1]")
    service.store.add_attempt(attempt_id=f"{stuck}/a1", job_id=stuck, seq=1, lane_id="codex-1",
                              model_requested=service.policy["models"]["astra"]["id"], state="failed",
                              outcome_class="limited")        # its retry excludes codex-1
    later = submit(service, harness, pinned_model="terra", tier="hard")
    deferrals = []
    for _ in range(3):
        service._admit()                                    # raised TypeError before the fix
        hold = service._holds[stuck]
        assert hold["reason"] == "route" and hold["error_type"] == "TypeError"
        deferrals.append(hold["deferrals"])
        due(service, stuck)
    assert deferrals == [1, 2, 3]
    assert placed_on(service, later)
    with pytest.raises(Unroutable):
        service._merged_exclusions(service.store.get_job(stuck), ("codex-1",))


# --- found, not fixed: strict xfails ----------------------------------------------------------

@pytest.mark.xfail(strict=True, raises=AssertionError, reason="C-6.9: a capacity waiter held behind an older job is not a "
                   "waiter itself, so younger jobs that compete only with it pass it (not fixed here: "
                   "the fix adds holds and needs its own review)")
def test_c6_9_a_waiting_job_held_behind_an_older_one_still_holds_its_own_competitors(fleet):
    """C-6.9 (P2): "A job that cannot be placed on this pass (it is `waiting` on `capacity` with a
    `next_check_at` still ahead, ...) holds back the later jobs of its tier that compete with it". X's
    own looks find no lane and set its clock; then a cap is set, a Claude lane opens, and X, its clock
    still ahead, is held behind W (they share Astra). Y competes with X (Opus) but not with W. Without W,
    Y is held behind X; with W, Y is placed past X, because `_admit_pass` checks `behind` before it
    registers a clocked capacity wait as a waiter."""
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = None    # no job holds another back while X looks
    closed(service, "codex-1")
    closed(service, "claude-a")
    older = submit(service, harness, pinned_model="astra")
    middle = submit(service, harness, pinned_model=None, task="review", tier="standard")   # opus, astra
    service._admit()
    for _ in range(3):                                      # X's verdict repeats, so its clock backs off (C-6.10)
        due(service, middle)
        service._admit()
    looked = service.store.get_job(middle)
    assert (looked["state"], looked["wait_reason"]) == ("waiting", "capacity") and len(rows(service, middle)) == 1
    assert service._holds[middle]["reason"] == "closed:account" and looked["next_check_at"] > after(5)
    service.policy["caps"]["max_active_attempts"] = 10     # the operator sets a cap
    with service.store.transaction("fixture.claude_a_opens") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE lane_id='claude-a'", (utcnow(),))
    younger = submit(service, harness, pinned_model="opus")
    service._admit()
    assert service.store.get_job(middle)["next_check_at"] == looked["next_check_at"]   # X was not looked at
    assert service._holds[middle] == {"reason": "behind-older-job", "behind": older, "tier": "standard"}
    assert service._holds.get(younger) == {"reason": "behind-older-job", "behind": middle, "tier": "standard"}


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="C-6.12: a retry that let its pin go and is held behind an older "
                   "job never becomes a waiter, so a younger competitor passes it (same root cause as "
                   "the test above)")
def test_c6_12_a_retry_that_let_its_pin_go_is_not_passed_by_a_younger_competitor(fleet):
    """C-6.12: "once a look has let the pin go, the job's demand is its own on that pass and while its
    clock runs, so it neither passes an older job it competes with nor is passed by a younger one"."""
    service, harness = fleet
    service.store.put_lane(lane("claude-b"))
    measured(service, "claude-b")
    closed(service, "codex-1")
    closed(service, "claude-a")                             # the retry's pair cannot run: its pin is let go
    older = submit(service, harness, pinned_model="astra")
    waiting(service, older, 600)
    retry = submit(service, harness, pinned_model=None, task="review", tier="standard")     # opus, astra
    service.store.add_attempt(attempt_id=f"{retry}/a1", job_id=retry, seq=1, lane_id="claude-a",
                              model_requested=service.policy["models"]["opus"]["id"], state="failed",
                              outcome_class="transient")
    younger = submit(service, harness, pinned_model="opus")
    service._admit()
    assert service._retry_verdicts[retry][1] is False and service._holds[retry]["behind"] == older
    assert not placed_on(service, younger)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="C-6.10: the wait is keyed on its kind as well as its verdict, so a "
                   "job that alternates between lease-held and slot-kept records one verdict again on "
                   "every alternation (not fixed here: the probe-pending key has no decision to key on)")
def test_c6_10_a_wait_of_another_kind_does_not_record_the_same_verdict_again(routing_state):  # noqa: F811
    """C-6.10 (P4): "a recheck that reaches the verdict it reached last time adds no decision row". A
    terra job whose output path another job holds waits `lease-held` while the fleet has room and
    `slot-kept` while it does not: an older job waiting for its own output path keeps the last slot
    (C-6.9, which also says it holds no job back). The job is pinned to codex-1, so every look chooses
    the same lane and reaches the same verdict (checked at each evaluation, not only in the rows); each
    return to `slot-kept` records it again."""
    service, harness = routing_state
    service.policy["caps"]["max_active_attempts"] = 3
    service.store.put_lane(lane("codex-2", "codex"))
    for lane_id in ("codex-1", "codex-2"):
        measured(service, lane_id)
    kept_out, out = str(harness.root / "kept.md"), str(harness.root / "report.md")
    keeper = submit(service, harness, pinned_model="terra", out_path=kept_out)
    job = submit(service, harness, pinned_model="terra", pinned_lane="codex-1", out_path=out)
    verdicts, pick = [], service._pick

    def recording(row, **kwargs):
        decision = pick(row, **kwargs)
        if row["job_id"] == job:
            verdicts.append(scheduler.verdict_signature(decision))
        return decision
    service._pick = recording
    assert service.store.acquire_lease(f"out:{kept_out}", "another-job")
    assert service.store.acquire_lease(f"out:{out}", "another-job")
    fillers = [submit(service, harness, pinned_model="terra", tier="hard") for _ in range(2)]
    service._admit()                                        # both lease-held; the two fillers start: 2 live
    for _ in range(3):
        due(service, keeper), due(service, job)
        service._admit()                                    # 2 live: the cap less the slot kept for `keeper`
        assert service._holds[job]["reason"] == "slot-kept" and service._holds[job]["kept_for"] == keeper
        end(service, fillers.pop())
        fillers.append(submit(service, harness, pinned_model="terra", tier="hard"))
        due(service, keeper), due(service, job)
        service._admit()                                    # 1 live at its look; the new filler starts after
        assert service._holds[job]["reason"] == "lease-held"
    assert len(verdicts) >= 7 and len(set(verdicts)) == 1  # one verdict at every look, on codex-1
    signatures = {scheduler.verdict_signature(json.loads(row["decision_json"])) for row in rows(service, job)}
    assert signatures == set(verdicts)
    assert len(rows(service, job)) == 1


def test_c6_9_a_slot_is_not_kept_for_a_job_no_lane_can_admit(routing_state):  # noqa: F811
    """P3 (C-6.9, C-11.8): slot-keeping (`max_active_attempts` minus one for a job that passes an older
    waiter) never leaves a slot unusable for good. On the line this was found on (main, 2026-09-27) an
    Opus job with no Claude lane kept the only slot of a cap of 1 for as long as it waited (never started,
    so `max_wall_s` never ended it), and a Terra job that does not compete with it never ran beside a free
    Codex lane. Here C-11.8 holds: "A job no lane can admit ... holds back none ... no later job is ...
    kept out of a slot for it"."""
    service, harness = routing_state
    measured(service, "codex-1")
    service.policy["caps"]["max_active_attempts"] = 1
    opus = submit(service, harness, pinned_model="opus")
    terra = submit(service, harness, pinned_model="terra")
    service._admit()
    assert service._holds[opus].get("for_good") == ["no-lanes"]
    assert placed_on(service, terra) == "codex-1"


def test_c6_9_a_job_that_must_probe_is_not_held_by_an_attempt_in_flight(routing_state):  # noqa: F811
    """P3 (C-6.9, C-11.4): the head of its class has an admissible lane with a free slot on every pass, so
    it is placed. On the line this was found on (main, 2026-09-27) an attempt took the lowest free slot
    from 0 and a probe always leased `slot:0`, so an authorized Opus job, which must probe first, waited
    `probe-pending` behind the Sonnet attempt on claude-r's slot 0 until it ended (up to `max_wall_s`)
    with slot 1 free. Here "a detached attempt takes the lowest free `lane:<lane id>:slot:<n>` from
    n = 1, so `slot:0` is the admission probe's alone"."""
    service, harness = routing_state
    service.store.put_lane(lane("claude-r"))
    measured(service, "claude-r")
    service._execute_probe = lambda job, lane_row, model, holder: Outcome(OutcomeClass.OK, "admitted",
                                                                          {"rc": 0, "signal": None})
    first = submit(service, harness, pinned_lane="claude-r", pinned_model="sonnet")
    service._admit()
    assert placed_on(service, first) == "claude-r"
    probing = submit(service, harness, pinned_lane="claude-r", pinned_model="opus", unmeasured_reserve_reason=REASON)
    for _ in range(6):
        due(service, probing)
        service._admit()
    assert service._pick(service.store.get_job(probing)).chosen_lane == "claude-r"
    assert placed_on(service, probing) == "claude-r"
