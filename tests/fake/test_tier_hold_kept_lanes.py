"""C-6.9: what an older waiter pinned to a lane keeps, and when a later job it kept is looked at again.

These cover the gaps the 2026-09-22 review of the pinned-waiter fix found: a
later job whose pin names several lanes (refused at admission, C-6.12, not held),
an older job whose pin names no lane (it can use none, so it keeps none), the
look brought forward only when the older job stops waiting, a later job never
probing a kept lane, a transient retry confined to one lane, and `why` saying
which lane is kept.
"""

import json
import subprocess
from datetime import datetime

import pytest

from subfleet import render, scheduler
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Outcome,
                                OutcomeClass, Reading, ReadingLabel)
from subfleet.daemon import after, utcnow
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)
from tests.fake.test_tier_hold_by_demand import (  # noqa: F401  (fixtures)
    close, fleet, pinned_fleet, put_claude_lane, rejections, reserve_fable, submit, wait_on_capacity)


def attempts_on(service, job_id):
    return [row["lane_id"] for row in service.store.list_attempts(job_id)]


def hold_of(service, job_id):
    hold = service._holds[job_id]
    return {key: hold[key] for key in ("reason", "behind", "tier") if key in hold}


# --- a pin that names no lane -----------------------------------------------------------------

def test_c6_9_a_later_pin_that_names_no_lane_says_so(pinned_fleet):
    """C-6.9, C-11.2 it can use no lane, so it is not held behind anyone: it waits and says its pin is unknown."""
    service, harness = pinned_fleet
    older = submit(service, harness, pinned_model="opus")
    later = submit(service, harness, pinned_model="opus", pinned_lane="claude-a")
    service.store.update_job(later, pinned_lane="nobody@example.invalid")
    wait_on_capacity(service, older, seconds=3600)
    service._admit()
    assert service.store.get_job(later)["state"] == "waiting"
    assert service._holds[later]["reason"] == "no-lanes"
    decision = json.loads(service.store.list_decisions(later)[-1]["decision_json"])
    assert "pinned lane 'nobody@example.invalid' is unknown" in decision["reason"]


# --- the look brought forward when an older job stops waiting (C-6.9, C-6.10) --------------------

def looked_at(service):
    """The jobs whose workspace a pass prepares: the ones it looks at."""
    looked = []
    real = service._workspace
    service._workspace = lambda job: looked.append(job["job_id"]) or real(job)
    return looked


def kept_only_on_claude_a(service, harness):
    close(service, "claude-b")
    older = submit(service, harness, pinned_model="opus", pinned_lane="claude-a")
    newer = submit(service, harness, pinned_model="opus")
    wait_on_capacity(service, older, seconds=3600)
    service._admit()
    assert hold_of(service, newer) == {"reason": "behind-older-job", "behind": older, "tier": "standard"}
    return older, newer


def test_c6_9_a_kept_wait_is_not_brought_forward_while_the_older_job_waits_on_its_clock(pinned_fleet):
    """C-6.10 while the older job still waits, the later job keeps its own clock: no git, no scoring."""
    service, harness = pinned_fleet
    older, newer = kept_only_on_claude_a(service, harness)
    far = after(3600)
    service.store.update_job(newer, next_check_at=far)
    checked = service._capacity_waits[newer]["checked_at"]
    looked = looked_at(service)
    for _ in range(5):
        service._admit()
    assert looked == []
    assert service.store.get_job(newer)["next_check_at"] == far
    assert service._capacity_waits[newer]["checked_at"] == checked
    assert hold_of(service, newer)["behind"] == older


def test_c6_9_a_kept_wait_is_not_brought_forward_while_the_older_job_is_looked_at_and_still_waits(pinned_fleet):
    """C-6.10 an older job looked at every pass (a lease it needs is held) still waits, so it still keeps its lane."""
    service, harness = pinned_fleet
    close(service, "claude-b")
    out = str(harness.root / "older-report.md")
    older = submit(service, harness, pinned_model="opus", pinned_lane="claude-a", out_path=out)
    newer = submit(service, harness, pinned_model="opus")
    with service.store.transaction("fixture.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                   (f"out:{out}", "some-other-job", utcnow()))
    service._admit()
    assert service._holds[older]["reason"] == "lease-held"
    assert hold_of(service, newer)["behind"] == older
    far = after(3600)
    service.store.update_job(newer, next_check_at=far)
    looked = looked_at(service)
    for _ in range(5):
        service.store.update_job(older, next_check_at=utcnow())       # the older job is looked at every pass
        service._admit()
    assert looked == [older] * 5
    assert service.store.get_job(newer)["next_check_at"] == far
    assert not service.store.list_attempts(newer)


def test_c6_9_a_kept_wait_is_looked_at_on_the_pass_the_older_job_is_placed(pinned_fleet):
    """C-6.9 the older job takes its lane first, and the later job is looked at on that same pass."""
    service, harness = pinned_fleet
    older, newer = kept_only_on_claude_a(service, harness)
    service.store.update_job(newer, next_check_at=after(3600))
    service.store.update_job(older, next_check_at=utcnow())
    looked = looked_at(service)
    service._admit()
    assert looked == [older, newer]
    assert attempts_on(service, older) == ["claude-a"] and attempts_on(service, newer) == ["claude-a"]


def test_c6_10_a_repeated_kept_verdict_adds_no_second_decision_row(pinned_fleet):
    """C-6.10 a kept-lane wait backs off like any capacity wait and records its verdict once."""
    service, harness = pinned_fleet
    older, newer = kept_only_on_claude_a(service, harness)
    assert len(service.store.list_decisions(newer)) == 1

    def backoff():
        looked = datetime.fromisoformat(service._capacity_waits[newer]["checked_at"].replace("Z", "+00:00"))
        due = datetime.fromisoformat(service.store.get_job(newer)["next_check_at"].replace("Z", "+00:00"))
        return (due - looked).total_seconds()
    waits = [backoff()]
    for n in range(1, 5):
        service.store.update_job(newer, next_check_at=utcnow())       # due: this pass looks
        service._admit()
        assert service._capacity_waits[newer]["rechecks"] == n
        assert hold_of(service, newer)["behind"] == older
        waits.append(backoff())
    assert len(service.store.list_decisions(newer)) == 1
    assert all(abs(found - expected) <= 1 for found, expected in zip(waits, (1, 2, 4, 8, 16)))


def test_c6_9_two_pinned_waiters_keep_both_lanes_and_either_leaving_brings_the_later_job_forward(pinned_fleet):
    """C-6.9, C-6.11 the hold names the oldest keeper and every kept lane; any keeper leaving is a look."""
    service, harness = pinned_fleet
    first = submit(service, harness, pinned_model="opus", pinned_lane="claude-a")
    second = submit(service, harness, pinned_model="opus", pinned_lane="claude-b")
    newer = submit(service, harness, pinned_model="opus")
    for job_id in (first, second):
        wait_on_capacity(service, job_id, seconds=3600)
    service._admit()
    assert not service.store.list_attempts(newer)
    assert rejections(service, newer, "opus") == {"claude-a": [f"kept:{first}"], "claude-b": [f"kept:{second}"]}
    hold = service._holds[newer]
    assert (hold["reason"], hold["behind"], hold["kept"]) == (
        "behind-older-job", first, {"claude-a": first, "claude-b": second})
    assert service._admission["reasons"]["behind-older-job"] == 1
    text = service.dispatch("why", {"job_id": newer})["text"]
    assert f"held behind {first}: the only lanes that would take it are kept" in text
    assert f"claude-a for {first}, claude-b for {second}" in text
    service.store.update_job(second, state="cancelled")                # the keeper the hold does not name
    service.store.update_job(newer, next_check_at=after(3600))
    service._admit()
    assert attempts_on(service, newer) == ["claude-b"]


# --- a later job never probes, or takes, a kept lane -----------------------------------------------

@pytest.fixture
def unmeasured_fleet(fleet):
    """Two Claude lanes with no reading: an unmeasured lane has one slot, and a hard job probes it first."""
    service, harness = fleet
    for lane_id in ("claude-a", "claude-b"):
        put_claude_lane(service, lane_id)
    return service, harness


def install_probe(service, monkeypatch):
    calls = []

    def probe(job, lane, model, holder):
        calls.append((job["job_id"], lane.lane_id))
        return Outcome(OutcomeClass.OK, "admitted", {"rc": 0, "signal": None})
    monkeypatch.setattr(service, "_execute_probe", probe, raising=False)
    return calls


def writable_repo(harness):
    subprocess.run(["git", "init", "-q", str(harness.workdir)], check=True)
    subprocess.run(["git", "-C", str(harness.workdir), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)


@pytest.mark.parametrize("why", ["tier-hard", "workspace-write"])
def test_c6_9_a_kept_lane_is_never_probed_for_the_later_job(unmeasured_fleet, monkeypatch, why):
    """C-6.9 the route is prepared with the kept lanes too, so a probe never takes the older job's slot."""
    service, harness = unmeasured_fleet
    changes = {"tier": "hard"} if why == "tier-hard" else {"sandbox": "workspace-write"}
    if why == "workspace-write":
        writable_repo(harness)
    calls = install_probe(service, monkeypatch)
    older = submit(service, harness, pinned_model="opus", pinned_lane="claude-a", **changes)
    newer = submit(service, harness, pinned_model="opus", **changes,
                   **({"caller_session": "other-session"} if why == "workspace-write" else {}))
    wait_on_capacity(service, older, seconds=3600)
    service._admit()
    assert calls == [(newer, "claude-b")]
    assert attempts_on(service, newer) == ["claude-b"]
    assert not service.store.list_attempts(older)


def test_c6_9_a_later_job_whose_only_lane_is_kept_is_not_probed_at_all(unmeasured_fleet, monkeypatch):
    service, harness = unmeasured_fleet
    close(service, "claude-b")
    calls = install_probe(service, monkeypatch)
    older = submit(service, harness, pinned_model="opus", pinned_lane="claude-a", tier="hard")
    newer = submit(service, harness, pinned_model="opus", tier="hard")
    wait_on_capacity(service, older, seconds=3600)
    service._admit()
    assert calls == []
    assert not any(row["holder"].startswith("probe:") for row in service.store.list_leases())
    assert hold_of(service, newer)["behind"] == older


def test_c6_9_an_older_pinned_waiter_with_no_shared_model_keeps_nothing(pinned_fleet):
    """C-6.9 an Opus build pinned to a lane that refuses Opus keeps nothing from a later Fable job
    (the 2026-09-22 policy, Fable reserved)."""
    service, harness = pinned_fleet
    reserve_fable(service)
    service.store.add_reading(Reading("claude-b", "account", "seven_day", .6, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))   # claude-a has the headroom
    older = submit(service, harness, pinned_model="opus", pinned_lane="claude-a")
    newer = submit(service, harness, pinned_model="fable")
    service._admit()
    assert service.store.get_job(older)["state"] == "waiting"
    assert service._holds[older]["reason"] == "reserve:fable:unmeasured"
    assert attempts_on(service, newer) == ["claude-a"]
    decision = json.loads(service.store.list_decisions(newer)[-1]["decision_json"])
    assert "kept:" not in json.dumps(decision)


def test_c6_9_a_lane_pinned_task_job_competes_only_on_the_model_it_can_run(pinned_fleet):
    """C-6.9, C-11.2 a build pinned to a Claude lane runs Opus there, never Astra, so an older Astra job
    does not hold it."""
    service, harness = pinned_fleet
    older = submit(service, harness, pinned_model="astra")
    later = submit(service, harness, pinned_model=None, task="build", tier="standard", pinned_lane="claude-a")
    wait_on_capacity(service, older, seconds=3600)
    service._admit()
    [attempt] = service.store.list_attempts(later)
    assert (attempt["lane_id"], attempt["model_requested"]) == ("claude-a", service.policy["models"]["opus"]["id"])


# --- a transient retry is confined to its lane, and keeps only that lane (C-4.5, C-6.9) ----------

def transient(service, job_id, *, next_check_at):
    [first] = service.store.list_attempts(job_id)
    with service.store.transaction("fixture.transient", job_id=job_id) as tx:
        tx.execute("UPDATE attempts SET state='failed',outcome_class='transient' WHERE attempt_id=?",
                   (first["attempt_id"],))
        tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (first["attempt_id"], job_id))
        tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? WHERE job_id=?",
                   (next_check_at, job_id))
    return first


def test_c6_9_an_older_retry_waiting_for_its_busy_lane_holds_later_work_only_there(pinned_fleet):
    """C-4.5, C-6.9 the retry waits for a slot on claude-a; a later job runs on claude-b meanwhile."""
    service, harness = pinned_fleet
    service.policy["caps"]["max_in_flight_per_lane"] = 1
    older = submit(service, harness, pinned_model="opus")
    service._admit()
    assert attempts_on(service, older) == ["claude-a"]
    transient(service, older, next_check_at=after(3600))
    busy = submit(service, harness, pinned_model="opus", pinned_lane="claude-a", tier="hard")   # another tier fills claude-a
    service._admit()
    assert attempts_on(service, busy) == ["claude-a"]
    service.store.update_job(older, next_check_at=utcnow())
    newer = submit(service, harness, pinned_model="opus")
    service._admit()
    assert len(service.store.list_attempts(older)) == 1 and service._holds[older]["reason"] == "no-slot"
    assert attempts_on(service, newer) == ["claude-b"], service._holds.get(newer)
    assert rejections(service, newer, "opus")["claude-a"] == ["no-slot", f"kept:{older}"]


def test_c6_9_a_later_retry_pinned_to_a_kept_lane_waits_behind_the_older_job(pinned_fleet):
    """C-4.5, C-6.9 a later job's one same-lane retry does not take the lane an older job is pinned to."""
    service, harness = pinned_fleet
    service.store.add_reading(Reading("claude-b", "account", "seven_day", .6, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))   # claude-a has the headroom
    older = submit(service, harness, pinned_model="opus", pinned_lane="claude-a")
    newer = submit(service, harness, pinned_model="opus")
    service.store.update_job(older, state="waiting", wait_reason="approval")   # not a capacity waiter yet
    service._admit()
    assert attempts_on(service, newer) == ["claude-a"]
    transient(service, newer, next_check_at=utcnow())
    wait_on_capacity(service, older, seconds=3600)
    service._admit()
    assert len(service.store.list_attempts(newer)) == 1
    assert hold_of(service, newer) == {"reason": "behind-older-job", "behind": older, "tier": "standard"}
    assert json.loads(service.store.get_job(newer)["exclusions"]) == []
    # The pair is the daemon's choice, so the retry was looked at: its lane only lacks nothing but the
    # older job's turn, so it keeps the pin and waits behind that job on a capacity clock.
    assert service._retry_verdicts[newer][1] is True
    assert service.store.query("SELECT 1 FROM events WHERE kind='job.retry_held' AND job_id=?", (newer,))
    job = service.store.get_job(newer)
    assert (job["state"], job["wait_reason"]) == ("waiting", "capacity") and job["next_check_at"] > utcnow()
    # The older job leaves: the retry is looked at on that pass, whatever its clock, and takes its lane.
    service.store.update_job(older, state="cancelled")
    service.store.update_job(newer, next_check_at=after(3600))
    service._admit()
    assert attempts_on(service, newer) == ["claude-a", "claude-a"]


# --- a kept lane re-enrolled during the pass is still kept (C-6.9, C-11.2) --------------------------

def successor(service, lane_id, new_id):
    """A re-enrolment (C-10.2): the old lane disabled, the same credential bound to a new lane id."""
    service.store.update_lane(lane_id, enabled=0)
    service.store.put_lane(Lane(new_id, "claude", f"claude:{lane_id}@example.invalid",
                                Credential("claude", f"/fake/{lane_id}", "home"), f"/fake/{lane_id}",
                                LaneOwner.V2, False))
    service.store.add_reading(Reading(new_id, "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))


def test_c6_9_a_lane_re_enrolled_after_the_waiters_were_collected_is_still_kept(pinned_fleet):
    """C-6.9, C-11.2 the pass reads the roster once; the older job's pin follows its lane to a successor
    enrolled before the later job is evaluated, so the later job may not take that successor either."""
    service, harness = pinned_fleet
    close(service, "claude-b")
    older = submit(service, harness, pinned_model="opus", pinned_lane="claude-a")
    newer = submit(service, harness, pinned_model="opus")
    wait_on_capacity(service, older, seconds=3600)
    real = service._workspace

    def workspace(job):
        if job["job_id"] == newer and not service.store.get_lane("claude-a2"):
            successor(service, "claude-a", "claude-a2")         # between collecting waiters and evaluating
        return real(job)
    service._workspace = workspace
    service._admit()
    assert service.store.get_lane("claude-a2") and not service.store.list_attempts(newer), attempts_on(service, newer)
    hold = service._holds[newer]
    assert (hold["reason"], hold["behind"], hold["kept"]) == ("behind-older-job", older, {"claude-a2": older})
    assert scheduler.current_lane_id(service._pin_roster(), "claude-a") == "claude-a2"


def test_c6_9_evaluate_keeps_the_successor_of_a_kept_lane():
    policy = {"tiers": ["standard"], "chains": {}, "models": {"opus": {"provider": "claude", "id": "claude-opus-5-5"}},
              "caps": {"max_active_attempts": 4, "max_in_flight_per_lane": 2}}
    lanes = [{"lane_id": "claude-9", "provider": "claude", "home": "/h/9", "owner": "v2", "enabled": 0},
             {"lane_id": "claude-12", "provider": "claude", "home": "/h/9", "owner": "v2", "enabled": 1},
             {"lane_id": "claude-11", "provider": "claude", "home": "/h/11", "owner": "v2", "enabled": 1}]
    decision = scheduler.evaluate(policy, {"lanes": lanes, "now": utcnow()},
                                  {"pinned_model": "opus", "kept_lanes": {"claude-9": "w1"}})
    reasons = {row["lane_id"]: row["reasons"] for row in decision.evaluations[0]["rejections"]}
    assert reasons["claude-12"] == ["kept:w1"] and "kept:w1" in reasons["claude-9"]
    assert decision.chosen_lane == "claude-11"
    # An older job whose pin never follows (an unmeasured-reserve authorization, C-11.7) keeps only its own id.
    decision = scheduler.evaluate(policy, {"lanes": lanes, "now": utcnow()},
                                  {"pinned_model": "opus", "kept_lanes": {"claude-9": "w1"}, "kept_exact": ["claude-9"]})
    reasons = {row["lane_id"]: row["reasons"] for row in decision.evaluations[0]["rejections"]}
    assert "claude-12" not in reasons and "kept:w1" in reasons["claude-9"]
    assert decision.chosen_lane in ("claude-11", "claude-12")


def test_c6_9_an_older_authorized_pin_does_not_keep_its_lanes_successor(pinned_fleet):
    """C-6.9, C-11.7 an authorized job runs only on the lane id it was granted, never on a successor, so it
    keeps only that id: a later job takes the successor, pass after pass, rather than waiting on nothing."""
    service, harness = pinned_fleet
    close(service, "claude-b")
    older = submit(service, harness, pinned_model="opus", pinned_lane="claude-a")
    service.store.update_job(older, unmeasured_reserve_reason="fixture: a granted authorization")
    successor(service, "claude-a", "claude-a2")
    newer = submit(service, harness, pinned_model="opus")
    wait_on_capacity(service, older, seconds=3600)
    service._admit()
    assert attempts_on(service, newer) == ["claude-a2"], service._holds.get(newer)
    assert not service.store.list_attempts(older)


def test_c6_9_a_following_keeper_keeps_the_successor_though_an_older_bound_one_does_not(pinned_fleet):
    """C-6.9, C-11.7 an authorized Opus job and an ordinary Sonnet job both wait for claude-a (they do not
    compete with each other, so both are waiters); the later job's chain has both models. claude-a is
    re-enrolled mid-pass. The Sonnet job's pin follows to the successor, so the later job may not take it,
    though the oldest keeper's pin is bound."""
    service, harness = pinned_fleet
    for lane_id in ("claude-b", "codex-1"):
        close(service, lane_id)
    bound = submit(service, harness, pinned_model="opus", pinned_lane="claude-a", tier="easy")
    service.store.update_job(bound, unmeasured_reserve_reason="fixture: a granted authorization")
    follows = submit(service, harness, pinned_model="sonnet", pinned_lane="claude-a", tier="easy")
    newer = submit(service, harness, pinned_model=None, task="review", tier="easy")    # sonnet, opus, astra
    for job_id in (bound, follows):
        wait_on_capacity(service, job_id, seconds=3600)
    real = service._workspace

    def workspace(job):
        if job["job_id"] == newer and not service.store.get_lane("claude-a2"):
            successor(service, "claude-a", "claude-a2")         # between collecting waiters and evaluating
        return real(job)
    service._workspace = workspace
    service._admit()
    assert service.store.get_lane("claude-a2") and not service.store.list_attempts(newer), attempts_on(service, newer)
    assert service._holds[newer]["reason"] == "behind-older-job"


def test_c6_9_exact_lanes_are_those_only_bound_keepers_keep():
    a, b = frozenset({"claude-a"}), frozenset({"claude-b"})
    opus = frozenset({"opus"})
    waiters = [("w1", opus, a), ("w2", opus, a), ("w3", opus, b), ("w4", frozenset({"astra"}), b)]
    kept = {"claude-a": "w1", "claude-b": "w3"}
    assert scheduler.exact_lanes(opus, None, waiters, kept, {"w1"}) == frozenset()            # w2 follows to a's successor
    assert scheduler.exact_lanes(opus, None, waiters, kept, {"w1", "w2"}) == {"claude-a"}
    assert scheduler.exact_lanes(opus, None, waiters, kept, {"w3"}) == {"claude-b"}            # w4 does not compete
    assert scheduler.exact_lanes(opus, None, waiters, kept, set()) == frozenset()


# --- one rule, stated once (C-6.11) -----------------------------------------------------------------

def decision_of(rows, blocks=()):
    return {"evaluations": [{"rejections": [{"lane_id": lane, "reason": reasons[0], "reasons": reasons}
                                            for lane, reasons in rows], "capacity_blocks": list(blocks)}]}


def test_c6_11_room_on_another_lane_outranks_a_kept_lane_and_a_lane_that_refuses_anyway_is_not_kept():
    assert scheduler.dominant_rejection(decision_of([("claude-a", ["kept:w1"]), ("claude-b", ["no-slot"])])) == "no-slot"
    assert scheduler.dominant_rejection(decision_of([("claude-a", ["kept:w1"]), ("claude-b", ["kept:w2"])])) == "kept:w1"
    assert scheduler.dominant_rejection(decision_of([("claude-a", ["excluded", "kept:w1"])])) == "excluded"


def test_c6_9_kept_only_names_each_lane_a_decision_rejected_only_as_kept():
    rows = [("claude-a", ["kept:w1"]), ("claude-b", ["no-slot", "kept:w2"]), ("claude-c", ["excluded", "kept:w1"]),
            ("claude-d", ["no-slot"])]
    assert scheduler.kept_only(decision_of(rows)) == {"claude-a": "w1", "claude-b": "w2"}
    assert scheduler.kept_only(None) == {} and scheduler.kept_only(decision_of([])) == {}


def test_c6_11_why_says_which_lane_is_kept_and_for_whom():
    lines = render.why_queue({"job_id": "j", "state": "waiting", "wait_reason": "capacity",
                              "hold": {"reason": "behind-older-job", "behind": "w1", "tier": "standard",
                                       "kept": {"claude-9": "w1"}}})
    assert lines[1] == ("Held: held behind w1: the only lanes that would take it are kept for older standard "
                        "jobs pinned there (claude-9 for w1) (C-6.9)")
    fleet_wide = render.why_queue({"job_id": "j", "state": "queued",
                                   "hold": {"reason": "behind-older-job", "behind": "w1", "tier": "standard"}})
    assert "could run on the same model" in fleet_wide[1]
