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


# --- fixed after: one verdict, one row, whatever kind of wait came between (C-6.10) ----------------

def test_c6_10_a_wait_of_another_kind_does_not_record_the_same_verdict_again(routing_state):  # noqa: F811
    """C-6.10 (P4): "a recheck that reaches the verdict it reached last time adds no decision row". A
    terra job whose output path another job holds waits `lease-held` while the fleet has room and
    `slot-kept` while it does not: an older job waiting for its own output path keeps the last slot
    (C-6.9, which also says it holds no job back). Each slot-kept look reaches one verdict. The wait was
    keyed on its kind as well as its verdict, so each return to `slot-kept` recorded that verdict again:
    three rows for three alternations. A lease look records nothing (what holds the job is the lease), and
    the row now follows the verdict last recorded. The count still follows the wait's kind (C-6.10 gives
    the lease wait its own doubling), so each alternation starts it again."""
    service, harness = routing_state
    service.policy["caps"]["max_active_attempts"] = 3
    service.store.put_lane(lane("codex-2", "codex"))
    for lane_id in ("codex-1", "codex-2"):
        measured(service, lane_id)
    kept_out, out = str(harness.root / "kept.md"), str(harness.root / "report.md")
    keeper = submit(service, harness, pinned_model="terra", out_path=kept_out)
    job = submit(service, harness, pinned_model="terra", out_path=out)
    assert service.store.acquire_lease(f"out:{kept_out}", "another-job")
    assert service.store.acquire_lease(f"out:{out}", "another-job")
    fillers = [submit(service, harness, pinned_model="terra", tier="hard") for _ in range(2)]
    service._admit()                                        # both lease-held; the two fillers start: 2 live
    assert service._holds[job]["reason"] == "lease-held" and rows(service, job) == []
    for _ in range(3):
        due(service, keeper), due(service, job)
        service._admit()                                    # 2 live: the cap less the slot kept for `keeper`
        assert service._holds[job]["reason"] == "slot-kept" and service._holds[job]["kept_for"] == keeper
        assert service._capacity_waits[job]["rechecks"] == 0
        end(service, fillers.pop())
        fillers.append(submit(service, harness, pinned_model="terra", tier="hard"))
        due(service, keeper), due(service, job)
        service._admit()                                    # 1 live at its look; the new filler starts after
        assert service._holds[job]["reason"] == "lease-held"
        assert service._capacity_waits[job]["rechecks"] == 0
    signatures = {scheduler.verdict_signature(json.loads(row["decision_json"])) for row in rows(service, job)}
    assert len(signatures) == 1
    assert len(rows(service, job)) == 1


def test_c6_10_neither_a_probes_turn_nor_its_answer_records_a_verdict_again(routing_state, monkeypatch):  # noqa: F811
    """C-6.10: "a probe that ends the same way adds no second decision row", and nor does any look that
    reaches the verdict last recorded. A job that must probe (C-11.4) is held for the slot an older lease
    waiter keeps (C-6.9) with one verdict, then for its probe's slot (another holder has `slot:0`), then
    by an inconclusive probe of the same lane. Each kind of wait was keyed apart, so each return to a
    verdict-keyed kind recorded the verdict again: four rows here. Each kind still keeps its own count."""
    from subfleet import daemon as daemon_module
    service, harness = routing_state
    service.policy["caps"]["max_active_attempts"] = 3
    measured(service, "codex-1")
    kept_out = str(harness.root / "kept.md")
    keeper = submit(service, harness, pinned_model="terra", out_path=kept_out)
    assert service.store.acquire_lease(f"out:{kept_out}", "another-job")
    fillers = [submit(service, harness, pinned_model="terra", tier="hard") for _ in range(2)]
    service._admit()                                        # `keeper` waits on its path; 2 live
    assert [placed_on(service, filler) for filler in fillers] == ["codex-1", "codex-1"]
    probing = submit(service, harness, pinned_model="terra")
    real = daemon_module.scheduler.probe_required
    monkeypatch.setattr(daemon_module.scheduler, "probe_required",
                        lambda decision, job: bool(decision.chosen_lane) if job["job_id"] == probing
                        else real(decision, job))
    answers = []
    service._execute_probe = lambda job, lane_row, model, holder: Outcome(answers.pop(0), "fixture",
                                                                          {"rc": 0, "signal": None})

    def look(answer=None, held=None):
        if answer:
            answers.append(answer)
        if held:
            assert service.store.acquire_lease("lane:codex-1:slot:0", held)
        due(service, keeper), due(service, probing)
        service._admit()
        if held:
            service.store.release_leases(held)
        return service._holds[probing], service._capacity_waits[probing]

    hold, wait = look(OutcomeClass.OK)                      # probed, admitted; the last slot is kept
    assert hold["reason"] == "slot-kept" and hold["kept_for"] == keeper
    first = rows(service, probing)
    assert len(first) == 1
    verdict = scheduler.verdict_signature(json.loads(first[0]["decision_json"]))
    hold, wait = look(held="another-job/a1")               # its probe's slot is held: no verdict of its own
    assert (hold["reason"], wait["signature"]) == ("probe-pending", "probe-pending") and "behind" not in hold
    hold, wait = look(OutcomeClass.OK)
    assert hold["reason"] == "slot-kept" and (wait["signature"], wait["rechecks"]) == (verdict, 0)
    hold, wait = look(OutcomeClass.UNKNOWN)                 # the probe says nothing: its own 60 s wait
    assert (hold["reason"], wait["signature"]) == ("probe-pending", "probe-wait:" + verdict)
    hold, wait = look(OutcomeClass.UNKNOWN)
    assert wait["rechecks"] == 1
    hold, wait = look(OutcomeClass.OK)
    assert hold["reason"] == "slot-kept" and (wait["signature"], wait["rechecks"]) == (verdict, 0)
    assert rows(service, probing) == first


@pytest.mark.parametrize("detour", ["workspace", "route"])
def test_c6_10_a_verdict_recorded_before_a_detour_is_not_recorded_again_after_it(fleet, detour):
    """C-6.10: a wait that becomes C-6.8's (its workspace) or C-6.12's (its route) loses its record and
    keeps that clock. The record also held whether the verdict had been recorded, so the first look after
    the detour, which reached the verdict it reached before, recorded it again. What was recorded is kept
    beside the record now, for as long as the job waits."""
    import errno
    service, harness = fleet
    service.store.update_lane("claude-a", enabled=0)        # no Claude lane: an Opus job has no candidate
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    assert len(rows(service, stuck)) == 1
    label = service._holds[stuck]["reason"]                 # `disabled`: what keeps it out of every lane
    due(service, stuck)
    if detour == "workspace":
        real = service._workspace

        def failing(job):
            raise OSError(errno.EAGAIN, "resource temporarily unavailable")
        service._workspace = failing
    else:
        real = service._pick

        def failing(job, **options):
            raise ValueError("injected: bad reset clock")    # C-6.12: not the job's, so it waits on `route`
        service._pick = failing
    service._admit()
    assert service.store.get_job(stuck)["wait_reason"] == detour and stuck not in service._capacity_waits
    if detour == "workspace":
        service._workspace = real
    else:
        service._pick = real
    due(service, stuck)
    service._admit()
    assert service.store.get_job(stuck)["wait_reason"] == "capacity" and service._holds[stuck]["reason"] == label
    assert len(rows(service, stuck)) == 1


def test_c6_10_a_row_rolled_back_with_its_transaction_is_recorded_by_the_next_look(fleet, monkeypatch):
    """C-6.10, C-11.5: what was recorded is noted only once the transaction that recorded it commits. A
    waiting job's verdict changes, and a store error after its row (the pass's, C-5.10) rolls the row
    back. The row followed the count, which that look had already moved on, so the next look at the new
    verdict counted a repeat and recorded nothing: the job kept a verdict it no longer reached."""
    import sqlite3
    service, harness = fleet
    service.store.update_lane("claude-a", enabled=0)
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    assert len(rows(service, stuck)) == 1
    service.store.put_lane(lane("claude-b"))                 # the verdict changes: a lane appears, closed
    closed(service, "claude-b")
    real = service.store.add_decision

    def fails_after(job_id, decision, attempt_id=None):
        real(job_id, decision, attempt_id)
        raise sqlite3.OperationalError("injected: disk I/O error")
    monkeypatch.setattr(service.store, "add_decision", fails_after)
    due(service, stuck)
    with pytest.raises(sqlite3.OperationalError):
        service._admit()
    assert len(rows(service, stuck)) == 1                   # rolled back
    monkeypatch.setattr(service.store, "add_decision", real)
    due(service, stuck)
    service._admit()
    assert service._capacity_waits[stuck]["rechecks"] == 1  # the count went on; the row is not skipped
    recorded = rows(service, stuck)
    assert len(recorded) == 2
    assert scheduler.verdict_signature(json.loads(recorded[-1]["decision_json"])) == \
        service._capacity_waits[stuck]["signature"]


def test_c6_10_placing_the_job_starts_its_record_of_verdicts_again(fleet):
    """C-6.10: the record is "dropped when the job is placed or ends", and with it the verdict it last
    recorded: the attempt's own row is the job's decision then (C-11.5), and a wait after the attempt is a
    new one, whose first look records its verdict even when it is the one recorded before the attempt."""
    service, harness = fleet
    service.store.update_lane("claude-a", enabled=0)
    stuck = submit(service, harness, pinned_model="opus")
    service._admit()
    before = rows(service, stuck)
    assert len(before) == 1 and stuck in service._recorded_verdicts
    service.store.update_lane("claude-a", enabled=1)
    due(service, stuck)
    service._admit()
    assert placed_on(service, stuck) == "claude-a"
    assert stuck not in service._recorded_verdicts and stuck not in service._capacity_waits
    attempt = service.store.list_attempts(stuck)[-1]["attempt_id"]
    with service.store.transaction("fixture.attempt_ended") as tx:   # it ends with a fault of its own, and is retried
        tx.execute("UPDATE attempts SET state='failed',outcome_class='unknown' WHERE attempt_id=?", (attempt,))
        tx.execute("UPDATE jobs SET state='queued' WHERE job_id=?", (stuck,))
        tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (attempt, stuck))
    service.store.update_lane("claude-a", enabled=0)
    service._admit()
    after = rows(service, stuck)
    assert len(after) == 2
    assert {scheduler.verdict_signature(json.loads(row["decision_json"])) for row in after} == \
        {scheduler.verdict_signature(json.loads(before[0]["decision_json"]))}


def test_c6_10_a_pass_clears_the_other_kinds_jobs_only_once_they_end(fleet):
    """C-6.10, C-26.9 (review of #202): each pass cleared every table of each job it read as out of the
    queue, whatever its kind. A turn pass could read a detached job as running, the detached pass could
    then record the verdict of that job's retry, and the turn pass's clearing then dropped it: the next
    look recorded it again and backed off from 1 s. A pass clears its own kind's jobs once they leave the
    queue and the other kind's only once they have ended, which no write can follow."""
    service, harness = fleet
    service.store.update_lane("claude-a", enabled=0)
    running, ended = (submit(service, harness, pinned_model="opus") for _ in range(2))
    service._admit()
    assert {running, ended} <= set(service._recorded_verdicts) & set(service._capacity_waits)
    service.store.update_job(running, state="running")      # out of the queue while an attempt runs
    service.store.update_job(ended, state="failed")
    service._admit_pass({}, {"placed": 0}, kind="turn")
    assert running in service._recorded_verdicts and running in service._capacity_waits
    assert ended not in service._recorded_verdicts and ended not in service._capacity_waits
    service._admit_pass({}, {"placed": 0}, kind="detached")
    assert running not in service._recorded_verdicts and running not in service._capacity_waits


# --- found, not fixed: strict xfails ----------------------------------------------------------

@pytest.mark.xfail(strict=True, raises=AssertionError, reason="C-6.9: a capacity waiter held behind an older job is not a "
                   "waiter itself, so younger jobs that compete only with it pass it (not fixed here: "
                   "the fix adds holds and needs its own review)")
def test_c6_9_a_waiting_job_held_behind_an_older_one_still_holds_its_own_competitors(fleet):
    """C-6.9 (P2): "A job that cannot be placed on this pass (it is `waiting` on `capacity` with a
    `next_check_at` still ahead, ...) holds back the later jobs of its tier that compete with it". X waits
    on capacity with its clock ahead and is held behind W (they share Astra); Y competes with X (Opus)
    but not with W. Without W, Y is held behind X; with W, Y is placed past X, because `_admit_pass` checks
    `behind` before it registers a clocked capacity wait as a waiter."""
    service, harness = fleet
    closed(service, "codex-1")
    older = submit(service, harness, pinned_model="astra")
    waiting(service, older, 600)
    middle = submit(service, harness, pinned_model=None, task="review", tier="standard")   # opus, astra
    waiting(service, middle, 20)
    younger = submit(service, harness, pinned_model="opus")
    service._admit()
    assert service._holds[middle]["behind"] == older
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
