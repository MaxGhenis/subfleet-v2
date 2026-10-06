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
from tests.caps import capped
from tests.fake.test_routing_end_to_end import routing_state


@pytest.fixture
def fleet(routing_state):
    """One measured Codex lane that serves both astra and terra, two slots."""
    service, harness = routing_state
    # C-6.9's hold-back needs a count to hold for: the caps of before 2026-09-27
    # (tests/caps.py). With none, the default since, no job waits behind another.
    capped(service.policy)
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


def test_c6_16_a_waiting_priority_job_holds_back_competing_jobs_of_every_tier(fleet):
    """C-6.16 review of PR #147: a waiting hard-tier priority job held back only
    hard-tier jobs, so later standard and trivial jobs for its model took the
    slots it waited for. It holds back every competing detached job now,
    priority or not, and still nothing it does not compete with."""
    service, harness = fleet
    service.policy["admission"]["priority_callers"] = ["CHOSEN"]
    older = submit(service, harness, pinned_model="astra", tier="hard", caller_session="chosen")
    trivial = submit(service, harness, pinned_model="astra", tier="trivial", caller_session="other")
    standard = submit(service, harness, pinned_model="astra", tier="standard", caller_session="chosen")
    terra = submit(service, harness, pinned_model="terra", tier="trivial", caller_session="other")
    wait_on_capacity(service, older)
    service._admit()
    assert admitted(service, terra)
    for held in (trivial, standard):
        assert not service.store.list_attempts(held)
        assert service._holds[held]["reason"] == "behind-older-job"
        assert service._holds[held]["behind"] == older


def test_c6_16_a_job_passing_a_priority_waiter_of_another_tier_leaves_it_the_last_slot(fleet):
    """C-6.16 review of PR #147: with a cap, a later trivial job took the last
    slot a waiting hard-tier priority job needed. It keeps that slot now."""
    service, harness = fleet
    service.policy["caps"]["max_active_attempts"] = 2
    service.policy["admission"]["priority_callers"] = ["chosen"]
    running = submit(service, harness, pinned_model="terra", tier="trivial")
    service._admit()
    assert admitted(service, running)
    older = submit(service, harness, pinned_model="astra", tier="hard", caller_session="CHOSEN")
    passer = submit(service, harness, pinned_model="terra", tier="trivial", caller_session="other")
    wait_on_capacity(service, older)
    service._admit()
    assert not service.store.list_attempts(passer)                 # 1 live + the kept slot = the cap of 2
    assert service.store.get_job(passer)["state"] == "waiting"
    service.store.update_job(older, state="cancelled")
    service.store.update_job(passer, next_check_at=utcnow())
    service._admit()
    assert admitted(service, passer)


def test_c6_16_a_detached_tier_named_like_a_turn_queue_is_still_held(fleet):
    """C-6.16 review r2 of PR #147: a valid detached tier spelled `…#turn` was
    skipped by a name check and passed a waiting priority job."""
    service, harness = fleet
    service.policy["tiers"] = [*service.policy["tiers"], "trivial#turn"]
    service.policy["admission"]["priority_callers"] = ["chosen"]
    older = submit(service, harness, pinned_model="astra", tier="hard", caller_session="chosen")
    oddly = submit(service, harness, pinned_model="astra", tier="trivial#turn", caller_session="other")
    wait_on_capacity(service, older)
    service._admit()
    assert not service.store.list_attempts(oddly)
    assert service._holds[oddly]["behind"] == older


def test_c6_16_only_a_priority_waiter_crosses_tiers(fleet):
    """C-4.1 a waiter that is not priority still holds back only its own tier:
    the pass looks at a trivial waiter first, and it holds no later hard job."""
    service, harness = fleet
    service.policy["admission"]["priority_callers"] = ["chosen"]
    older = submit(service, harness, pinned_model="astra", tier="trivial", caller_session="other")
    hard = submit(service, harness, pinned_model="astra", tier="hard", caller_session="other")
    wait_on_capacity(service, older)
    service._admit()
    assert admitted(service, hard)


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


@pytest.mark.parametrize("case", ["same-lane", "newer-unpinned", "older-unpinned", "newer-unknown-pin"])
def test_c6_9_lane_pins_that_could_share_a_lane_still_compete(pinned_fleet, case):
    """C-6.9 only two different pins are disjoint; a free choice or a shared pin competes.

    A pin that names no lane no longer waits behind the older job (intended,
    C-11.8): no lane can ever admit it, so it is held `pin-unadmittable`
    before it is compared with anyone, and it holds nobody back either."""
    service, harness = pinned_fleet
    service.policy["caps"]["max_active_attempts"] = 4               # C-6.9 holds back only in a capped pool
    older_pin, newer_pin = {"same-lane": ("claude-a", "claude-a"), "newer-unpinned": ("claude-a", None),
                            "older-unpinned": (None, "claude-b"), "newer-unknown-pin": ("claude-a", None)}[case]
    older = submit(service, harness, pinned_model="fable", pinned_lane=older_pin)
    newer = submit(service, harness, pinned_model="fable", pinned_lane=newer_pin)
    if case == "newer-unknown-pin":
        service.store.update_job(newer, pinned_lane="nobody@example.invalid")   # a pin the roster cannot resolve
    wait_on_capacity(service, older)
    service._admit()
    assert not service.store.list_attempts(newer)
    if case == "newer-unknown-pin":
        hold = service._holds[newer]
        assert (hold["reason"], hold["reasons"]) == ("pin-unadmittable", ["unknown"])
        assert service.store.get_job(newer)["state"] == "waiting"
    else:
        assert service._holds[newer] == {"reason": "behind-older-job", "behind": older, "tier": "standard"}
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
