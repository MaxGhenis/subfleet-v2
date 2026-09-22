"""C-6.9: FIFO within a tier holds among jobs that compete for a model, and no further.

Incident, 2026-09-20: an Opus review with no admissible lane (every Claude lane
`reserve:fable:unmeasured`, Astra closed until the next day) sat at the head of
the `standard` tier. `_admit` held the whole tier behind it, so three
Fable-pinned handoff jobs stayed queued for hours beside eleven free Fable
lanes, and nothing at all was admitted for more than three hours.
"""

import pytest

from subfleet import scheduler
from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from tests.fake.test_routing_end_to_end import routing_state


@pytest.fixture
def fleet(routing_state):
    """One measured Codex lane that serves both astra and terra, two slots."""
    service, harness = routing_state
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    return service, harness


def submit(service, harness, **changes):
    return service.dispatch("submit", harness.submit_args(**changes))["job_id"]


def wait_on_capacity(service, job_id, seconds=30):
    service.store.update_job(job_id, state="waiting", wait_reason="capacity", next_check_at=after(seconds))


def admitted(service, job_id):
    return [row["state"] for row in service.store.list_attempts(job_id)] == ["reserved"]


def test_c6_9_a_waiter_holds_back_only_the_jobs_that_compete_with_it(fleet):
    """C-6.9 the incident: the astra job waits, a later astra job waits behind it, a terra job does not."""
    service, harness = fleet
    older = submit(service, harness, pinned_model="astra")
    same = submit(service, harness, pinned_model="astra")
    other = submit(service, harness, pinned_model="terra")
    wait_on_capacity(service, older)
    service._admit()
    assert admitted(service, other)
    assert not service.store.list_attempts(same) and not service.store.list_attempts(older)
    assert service.store.get_job(same)["state"] == "queued"      # held, not failed and not re-queued behind anyone


def test_c6_9_a_chain_competes_with_every_model_it_could_promote_to(fleet):
    """C-6.9 research/standard may promote to astra, so a later astra pin waits behind it; terra does not."""
    service, harness = fleet
    older = submit(service, harness, pinned_model=None, task="research", tier="standard")
    astra = submit(service, harness, pinned_model="astra")
    terra = submit(service, harness, pinned_model="terra")
    wait_on_capacity(service, older)
    service._admit()
    assert admitted(service, terra) and not service.store.list_attempts(astra)


def test_c6_9_a_job_whose_models_cannot_be_told_competes_with_everything(fleet):
    """C-6.9 a lane pin with no model is held behind any older waiter of its tier."""
    service, harness = fleet
    older = submit(service, harness, pinned_model="astra")
    unknown = submit(service, harness, pinned_model=None, pinned_lane="codex-1")
    wait_on_capacity(service, older)
    service._admit()
    assert not service.store.list_attempts(unknown)


def test_c6_9_tiers_stay_independent(fleet):
    """C-4.1 a waiter in one tier never held another tier, and still does not."""
    service, harness = fleet
    older = submit(service, harness, pinned_model=None, task="research", tier="hard")      # astra
    lower = submit(service, harness, pinned_model=None, task="sweep", tier="easy")         # terra, then astra
    wait_on_capacity(service, older)
    service._admit()
    assert admitted(service, lower)


def test_c6_9_a_job_that_passes_a_waiter_leaves_it_a_slot(fleet):
    """C-6.9 passing an older job never costs it its start: one active slot stays free while it waits."""
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 2
    running = submit(service, harness, pinned_model="terra")
    service._admit()
    assert admitted(service, running)
    older = submit(service, harness, pinned_model="astra")
    passer = submit(service, harness, pinned_model="terra")
    wait_on_capacity(service, older)
    service._admit()
    assert not service.store.list_attempts(passer)                 # 1 live + the reserved slot = the cap of 2
    assert service.store.get_job(passer)["state"] == "waiting"
    # With nobody older waiting, the same job takes the second slot.
    service.store.update_job(older, state="cancelled")
    service.store.update_job(passer, next_check_at=utcnow())
    service._admit()
    assert admitted(service, passer)


def test_c6_9_a_full_fleet_stops_the_pass(fleet):
    """C-6.4 at `max_active_attempts` nothing later is evaluated, whatever it competes for."""
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 1
    first = submit(service, harness, pinned_model="terra")
    second = submit(service, harness, pinned_model="astra")
    third = submit(service, harness, pinned_model="terra")
    calls = []
    real = service._workspace
    service._workspace = lambda job: calls.append(job["job_id"]) or real(job)
    service._admit()
    assert admitted(service, first) and calls == [first, second]   # the second finds the fleet full; the third is not looked at
    assert not service.store.list_attempts(third)


@pytest.mark.parametrize("job,expected", [
    ({"pinned_model": "fable"}, {"fable"}),
    ({"task": "review", "tier": "standard"}, {"opus", "astra"}),
    ({"task": "review", "tier": None}, {"opus", "astra"}),                       # no tier is `standard`
    ({"task": "review", "tier": "trivial"}, {"haiku", "sonnet", "opus", "astra"}),
    ({"task": "authored-prose", "tier": "hard"}, {"fable"}),
    ({"pinned_lane": "claude-3"}, None),
    ({"task": "not-a-task", "tier": "standard"}, None),
])
def test_c6_9_demand_models_follow_the_chain_evaluate_walks(job, expected):
    """C-11.2 a pin is one model; a task is its chain from its tier upward."""
    import json
    from pathlib import Path
    policy = json.loads((Path(scheduler.__file__).with_name("default_policy.json")).read_text())
    found = scheduler.demand_models(policy, job)
    assert found == (None if expected is None else frozenset(expected))


def test_c6_9_competes_is_overlap_or_unknown():
    """C-6.9 disjoint model sets do not compete; an unknown set competes with all."""
    assert scheduler.competes(frozenset({"opus", "astra"}), frozenset({"astra"}))
    assert not scheduler.competes(frozenset({"opus", "astra"}), frozenset({"fable"}))
    assert scheduler.competes(None, frozenset({"fable"})) and scheduler.competes(frozenset({"fable"}), None)


# --- C-6.9: lane pins ----------------------------------------------------------------------

from subfleet.contracts import Credential, Lane, LaneOwner


@pytest.fixture
def pinned_fleet(fleet):
    """Two measured Claude lanes for lane-pinned Fable work."""
    service, harness = fleet
    for lane_id in ("claude-a", "claude-b"):
        service.store.put_lane(Lane(lane_id, "claude", f"claude:{lane_id}@example.invalid",
                                    Credential("claude", f"/fake/{lane_id}", "home"), f"/fake/{lane_id}",
                                    LaneOwner.V2, False))
        service.store.add_reading(Reading(lane_id, "account", "seven_day", .2, after(86400),
                                          ReadingLabel.PROVIDER, "fixture", utcnow()))
    return service, harness


def test_c6_9_a_waiter_pinned_to_one_lane_does_not_hold_a_job_pinned_to_another(pinned_fleet):
    """C-6.9 the 2026-09-22 gates: the older job can only use lane a, so it holds nothing pinned to lane b."""
    service, harness = pinned_fleet
    older = submit(service, harness, pinned_model="fable", pinned_lane="claude-a")
    other = submit(service, harness, pinned_model="fable", pinned_lane="claude-b")
    wait_on_capacity(service, older)
    service._admit()
    assert admitted(service, other) and not service.store.list_attempts(older)


@pytest.mark.parametrize("case", ["same-lane", "older-unpinned", "newer-unknown-pin"])
def test_c6_9_lane_pins_that_could_share_a_lane_still_compete(pinned_fleet, case):
    """C-6.9 a shared pin, an older free choice, or a newer unresolvable pin competes.

    A newer job with no pin behind an older pinned one is evaluated without the
    older job's lane instead: see the C-6.9 section on pinned waiters below.
    """
    service, harness = pinned_fleet
    older_pin, newer_pin = {"same-lane": ("claude-a", "claude-a"), "older-unpinned": (None, "claude-b"),
                            "newer-unknown-pin": ("claude-a", None)}[case]
    older = submit(service, harness, pinned_model="fable", pinned_lane=older_pin)
    newer = submit(service, harness, pinned_model="fable", pinned_lane=newer_pin)
    if case == "newer-unknown-pin":
        service.store.update_job(newer, pinned_lane="nobody@example.invalid")   # a pin the roster cannot resolve
    wait_on_capacity(service, older)
    service._admit()
    assert not service.store.list_attempts(newer)
    assert service.store.get_job(newer)["state"] == "queued"


def test_c6_9_demand_lanes_resolves_a_pin_to_its_lane_id():
    """C-11.2 an account label or a lane id names one lane; nothing, or an unknown label, is any lane."""
    roster = [{"lane_id": "claude-a", "account_key": "claude:a@example.invalid", "email": "a@example.invalid"},
              {"lane_id": "claude-b", "account_key": "claude:b@example.invalid", "email": "b@example.invalid"}]
    assert scheduler.demand_lanes(roster, {"pinned_lane": "claude-b"}) == frozenset({"claude-b"})
    assert scheduler.demand_lanes(roster, {"pinned_lane": "a@example.invalid"}) == frozenset({"claude-a"})
    assert scheduler.demand_lanes(roster, {"pinned_lane": None}) is None
    assert scheduler.demand_lanes(roster, {"pinned_lane": "nobody@example.invalid"}) is None


def test_c6_9_competes_needs_a_shared_model_and_a_shared_lane():
    """C-6.9 disjoint models or disjoint pins do not compete; an unknown side counts as overlap."""
    f, a, b = frozenset({"fable"}), frozenset({"claude-a"}), frozenset({"claude-b"})
    assert not scheduler.competes(f, f, a, b)
    assert scheduler.competes(f, f, a, a) and scheduler.competes(f, f, a, None) and scheduler.competes(f, f, None, b)
    assert not scheduler.competes(f, frozenset({"opus"}), a, a)
    assert scheduler.competes(None, f, a, b) is False                                  # lanes disjoint wins even with unknown models


# --- C-6.9: an older job pinned to a lane keeps that lane, not the fleet ------------------------
#
# Incident, 2026-09-22 (about 18:58Z): a standard build pinned to `claude-9` waited
# from 18:24:52Z, because `claude-9` refuses Opus as `reserve:fable:unmeasured` to
# every job without an unmeasured-reserve authorization. An unpinned job counted as
# competing with it on every lane, so every later unpinned standard job whose chain
# includes Opus, fresh reviews among them, was held `behind-older-job` (three at
# once) while `claude-11`, which would have taken them, stayed open.

import json

from subfleet.contracts import ClockSource, Closure, ClosureReason


def put_claude_lane(service, lane_id):
    service.store.put_lane(Lane(lane_id, "claude", f"claude:{lane_id}@example.invalid",
                                Credential("claude", f"/fake/{lane_id}", "home"), f"/fake/{lane_id}",
                                LaneOwner.V2, False))


@pytest.fixture
def incident_fleet(fleet):
    """`claude-9` has no usage read of the Fable window, so it refuses Opus; `claude-11` has slack."""
    service, harness = fleet
    service.policy["reserve"] = {"models": ["fable"], "cap_ratio": 2., "min_slack": .05}      # C-11.7, as in production
    for lane_id, source in (("claude-9", "fixture"), ("claude-11", "oauth-usage")):
        put_claude_lane(service, lane_id)
        service.store.add_reading(Reading(lane_id, "account", "seven_day", .2, after(86400),
                                          ReadingLabel.PROVIDER, source, utcnow()))
    return service, harness


def close(service, lane_id):
    service.store.add_closure(Closure(lane_id, "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))


def rejections(service, job_id, model):
    decision = json.loads(service.store.list_decisions(job_id)[-1]["decision_json"])
    [walked] = [row for row in decision["evaluations"] if row["model"] == model]
    return {row["lane_id"]: row["reasons"] for row in walked["rejections"]}


def test_c6_9_the_incident_a_waiter_pinned_to_a_lane_that_refuses_it_holds_nothing_elsewhere(incident_fleet):
    """C-6.9 the 2026-09-22 incident: later unpinned Opus builds and reviews run on `claude-11`, never `claude-9`."""
    service, harness = incident_fleet
    older = submit(service, harness, pinned_model=None, task="build", tier="standard", pinned_lane="claude-9")
    service._admit()
    assert service.store.get_job(older)["state"] == "waiting"
    assert service._holds[older]["reason"] == "reserve:fable:unmeasured"          # its own lane refuses it
    build = submit(service, harness, pinned_model=None, task="build", tier="standard")
    review = submit(service, harness, pinned_model=None, task="review", tier="standard")
    stored = {job_id: service.store.get_job(job_id)["exclusions"] for job_id in (build, review)}
    service._admit()
    for job_id in (build, review):
        [attempt] = service.store.list_attempts(job_id)
        assert (attempt["lane_id"], attempt["model_requested"]) == ("claude-11", "claude-opus-5-5")
        assert rejections(service, job_id, "opus")["claude-9"] == ["reserve:fable:unmeasured", f"kept:{older}"]
        assert service.store.get_job(job_id)["exclusions"] == stored[job_id]      # this pass only, never stored
    assert not service.store.list_attempts(older) and service._holds[older]["reason"] == "reserve:fable:unmeasured"
    assert "behind-older-job" not in service._admission["reasons"]


def test_c6_9_a_later_unpinned_job_never_takes_the_lane_an_older_waiter_is_pinned_to(pinned_fleet):
    """C-6.9 FIFO for the pinned lane's capacity: the later job waits behind the older one for it, then takes it."""
    service, harness = pinned_fleet
    close(service, "claude-b")                                  # the only lane that would take it is claude-a
    older = submit(service, harness, pinned_model="fable", pinned_lane="claude-a")
    newer = submit(service, harness, pinned_model="fable")
    wait_on_capacity(service, older, seconds=3600)
    service._admit()
    assert not service.store.list_attempts(newer)
    hold = service._holds[newer]
    assert {key: hold[key] for key in ("reason", "behind", "tier")} == {
        "reason": "behind-older-job", "behind": older, "tier": "standard"}
    assert rejections(service, newer, "fable")["claude-a"] == [f"kept:{older}"]
    answer = service.dispatch("why", {"job_id": newer})
    assert f"held behind {older}" in answer["text"] and f"rejected claude-a: kept:{older}" in answer["text"]
    service._admit()                                            # a pass that does not look repeats the hold
    assert service._holds[newer]["reason"] == "behind-older-job" and not service.store.list_attempts(newer)
    # The first pass on which the older job no longer waits looks at the later one, whatever its clock says.
    service.store.update_job(older, state="cancelled")
    service.store.update_job(newer, next_check_at=after(3600))
    service._admit()
    assert [row["lane_id"] for row in service.store.list_attempts(newer)] == ["claude-a"]


def test_c6_9_a_pinned_waiter_moves_later_work_off_its_lane_and_holds_later_work_pinned_there(pinned_fleet):
    """C-6.9 claude-a has the most headroom, so it is where the later job would go if nobody waited for it."""
    service, harness = pinned_fleet
    service.store.add_reading(Reading("claude-b", "account", "seven_day", .6, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    older = submit(service, harness, pinned_model="fable", pinned_lane="claude-a")
    unpinned = submit(service, harness, pinned_model="fable")
    same_pin = submit(service, harness, pinned_model="fable", pinned_lane="claude-a")
    wait_on_capacity(service, older)
    service._admit()
    assert [row["lane_id"] for row in service.store.list_attempts(unpinned)] == ["claude-b"]
    assert not service.store.list_attempts(same_pin) and service.store.get_job(same_pin)["state"] == "queued"
    assert service._holds[same_pin] == {"reason": "behind-older-job", "behind": older, "tier": "standard"}
    # With nobody waiting for claude-a, the same job goes where the headroom is.
    service.store.update_job(older, state="cancelled")
    service.store.update_job(same_pin, state="cancelled")
    control = submit(service, harness, pinned_model="fable")
    service._admit()
    assert [row["lane_id"] for row in service.store.list_attempts(control)] == ["claude-a"]


def test_c6_9_passing_a_pinned_waiter_still_leaves_it_the_last_slot(pinned_fleet):
    """C-6.9 `slot-kept`: a job that runs elsewhere than the older job's lane has still passed it."""
    service, harness = pinned_fleet
    service.policy["caps"]["max_active_attempts"] = 2
    running = submit(service, harness, pinned_model="terra")
    service._admit()
    assert admitted(service, running)
    older = submit(service, harness, pinned_model="fable", pinned_lane="claude-a")
    passer = submit(service, harness, pinned_model="fable")
    wait_on_capacity(service, older)
    service._admit()
    assert not service.store.list_attempts(passer)
    hold = service._holds[passer]
    assert (hold["reason"], hold["kept_for"], hold["live"]) == ("slot-kept", older, 1)


F, O = frozenset({"fable"}), frozenset({"opus"})
A, B = frozenset({"claude-a"}), frozenset({"claude-b"})


@pytest.mark.parametrize("later,pinned,waiters,expected", [
    ((F, None), False, [("w1", F, A)], (None, {"claude-a": "w1"})),                    # the incident
    ((F, A), True, [("w1", F, A)], ("w1", {"claude-a": "w1"})),                        # confined to the kept lane
    ((F, B), True, [("w1", F, A)], (None, {})),                                        # PR #23: different pins
    ((F, None), False, [("w1", F, None)], ("w1", {})),                                 # unpinned FIFO, unchanged
    ((F, None), False, [("w1", F, A), ("w2", F, None)], ("w2", {"claude-a": "w1"})),   # an unpinned waiter still holds
    ((F, None), False, [("w1", O, A)], (None, {})),                                    # no shared model: nothing kept
    ((F, None), True, [("w1", F, A)], ("w1", {})),                                     # an unresolvable pin: held
    ((None, None), False, [("w1", F, A)], (None, {"claude-a": "w1"})),                 # models unknown: lanes still kept
    ((F, None), False, [("w1", F, A), ("w2", F, A)], (None, {"claude-a": "w1"})),      # the oldest keeps it
    ((F, None), False, [("w1", F, A), ("w2", F, B)], (None, {"claude-a": "w1", "claude-b": "w2"})),
    ((F, B), True, [("w1", F, A), ("w2", F, B)], ("w2", {"claude-b": "w2"})),
    ((F, None), False, [], (None, {})),
])
def test_c6_9_tier_hold_keeps_only_the_lanes_older_pinned_waiters_are_pinned_to(later, pinned, waiters, expected):
    """C-6.9 an older pinned waiter keeps its lanes; only a job confined to them, or an older free choice, holds."""
    models, lanes = later
    assert scheduler.tier_hold(models, lanes, waiters, pinned=pinned) == expected


def test_c6_9_a_lane_kept_only_for_an_older_job_names_that_job():
    """C-6.9, C-6.11 a lane that would take the job but for an older waiter is the cause; one that refuses it is not."""
    def decision(rows, blocks=()):
        return {"evaluations": [{"rejections": [{"lane_id": lane, "reason": reasons[0], "reasons": reasons}
                                                for lane, reasons in rows], "capacity_blocks": list(blocks)}]}
    kept_only = [("claude-a", ["kept:w1"]), ("claude-b", ["reserve:fable:unmeasured"]),
                 ("claude-c", ["reserve:fable:unmeasured"])]
    assert scheduler.dominant_rejection(decision(kept_only)) == "kept:w1"
    assert scheduler.dominant_rejection(decision([("claude-a", ["no-slot", "kept:w1"])])) == "kept:w1"
    refused_anyway = [("claude-a", ["reserve:fable:unmeasured", "kept:w1"]), ("claude-b", ["reserve:fable:unmeasured"])]
    assert scheduler.dominant_rejection(decision(refused_anyway)) == "reserve:fable:unmeasured"
    room = [("claude-a", ["kept:w1"]), ("claude-b", ["no-slot"])]
    assert scheduler.dominant_rejection(decision(room, ["fleet"])) == "fleet-full"
