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
    """C-6.9 a shared pin, a newer free choice that would take the older job's lane (claude-a ranks first), an
    older free choice, or a pin nobody can resolve: the newer job waits."""
    service, harness = pinned_fleet
    older_pin, newer_pin = {"same-lane": ("claude-a", "claude-a"), "newer-unpinned": ("claude-a", None),
                            "older-unpinned": (None, "claude-b"), "newer-unknown-pin": ("claude-a", None)}[case]
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


# --- C-6.9 (2026-09-30): held only behind a job that could run where this one would ---------------
#
# 2026-09-30, 15:55Z: `20260930-115551-pb-gpt61sol-rejudge3` could run on the desktop lane
# (claude-9, open); `why` said it was held behind `20260930-114555-pr87-review-2`, an older
# standard job that could never run there. `competes` compared models and lane pins only, and an
# unpinned job "could run on every lane".

from subfleet.contracts import (DESKTOP_EXCLUSION, ClockSource, Closure, ClosureReason)
from subfleet.daemon import Daemon


def close_astra(service, lane_id):
    service.store.add_closure(Closure(lane_id, "gpt-6-astra", after(3600), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))


@pytest.fixture
def three_codex(fleet):
    """codex-1 (the fleet's), codex-2 and codex-3, each measured with room for astra and terra."""
    service, harness = fleet
    for lane_id in ("codex-2", "codex-3"):
        home = harness.root / lane_id
        home.mkdir()
        service.store.put_lane(Lane(lane_id, "codex", f"codex:fixture-{lane_id}", Credential("codex", str(home), "home"),
                                    str(home), LaneOwner.V2, False))
        service.store.add_reading(Reading(lane_id, "account", "seven_day", .2, after(86400),
                                          ReadingLabel.PROVIDER, "fixture", utcnow()))
    return service, harness


def lane_of(service, job_id):
    return [row["lane_id"] for row in service.store.list_attempts(job_id)]


def test_c6_9_a_newer_job_takes_a_lane_the_older_waiter_cannot_use(three_codex):
    """C-6.9 (a): the older astra job excludes codex-2 and codex-3, codex-1 is closed; the newer job runs on codex-2."""
    service, harness = three_codex
    close_astra(service, "codex-1")
    older = submit(service, harness, pinned_model="astra", exclusions=["codex-2", "codex-3"])
    newer = submit(service, harness, pinned_model="astra")
    wait_on_capacity(service, older)
    service._admit()
    assert lane_of(service, newer) == ["codex-2"]
    assert not service.store.list_attempts(older)


def test_c6_9_the_fairness_guarantee_holds_where_both_could_run(three_codex):
    """C-6.9 (b): codex-1 closed; the older job may use codex-2, which the newer would take: the newer waits,
    behind it by name, and the older takes codex-2 when it is next looked at."""
    service, harness = three_codex
    close_astra(service, "codex-1")
    older = submit(service, harness, pinned_model="astra", exclusions=["codex-3"])
    newer = submit(service, harness, pinned_model="astra")
    wait_on_capacity(service, older)
    service._admit()
    assert not service.store.list_attempts(newer)
    assert service._holds[newer]["reason"] == "behind-older-job" and service._holds[newer]["behind"] == older
    service.store.update_job(older, next_check_at=None)
    service._admit()
    assert lane_of(service, older) == ["codex-2"]


def test_c6_9_identical_jobs_are_held_without_asking_where_the_newer_would_run(fleet, monkeypatch):
    """C-6.9: where the older job's own facts allow all the newer one's, no view is built and no route evaluated."""
    service, harness = fleet
    monkeypatch.setattr(service, "_hold_view", lambda desktop: pytest.fail("evaluated a held job's lane"))
    older = submit(service, harness, pinned_model="astra")
    newer = submit(service, harness, pinned_model="astra")
    wait_on_capacity(service, older)
    service._admit()
    assert service._holds[newer] == {"reason": "behind-older-job", "behind": older, "tier": "standard"}


def test_c6_9_the_lane_a_job_is_reserved_on_is_checked_against_the_waiters_again(three_codex, monkeypatch):
    """C-6.9: the shared view said codex-3 (the older job cannot use it); the job's own decision chose codex-2,
    opened since, where the older job could run: held there, inside the reservation, on a clock."""
    service, harness = three_codex
    close_astra(service, "codex-1")
    close_astra(service, "codex-2")
    stale = service._route_view(service._desktop_identity())[2]
    with service.store.transaction("fixture.reopen") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE lane_id='codex-2'", (utcnow(),))
    monkeypatch.setattr(service, "_hold_view", lambda desktop: stale)
    older = submit(service, harness, pinned_model="astra", exclusions=["codex-3"])
    newer = submit(service, harness, pinned_model="astra")
    wait_on_capacity(service, older)
    service._admit()
    assert not service.store.list_attempts(newer)
    hold = service._holds[newer]
    assert (hold["reason"], hold["behind"], hold["lane"]) == ("behind-older-job", older, "codex-2")
    job = service.store.get_job(newer)
    assert (job["state"], job["wait_reason"]) == ("waiting", "capacity") and job["next_check_at"]


def test_c6_9_a_newer_job_runs_on_the_desktop_login_an_older_no_desktop_job_keeps_off(fleet, monkeypatch):
    """C-6.9, C-10.3 the incident's shape: every other Claude lane closed for Opus, the older job says
    --no-desktop, the newer does not; Claude Code is active on the desktop login. The newer runs there."""
    service, harness = fleet
    for lane_id, email in (("claude-1", "max@optiqal.ai"), ("claude-9", "max@thesisinstitute.org")):
        service.store.put_lane(Lane(lane_id, "claude", f"claude:{email}", Credential("claude", f"/fake/{lane_id}", "home"),
                                    f"/fake/{lane_id}", LaneOwner.V2, False, label=email))
    service.store.add_closure(Closure("claude-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    monkeypatch.setattr("subfleet.daemon.capacity.read_desktop_account", lambda: "max@thesisinstitute.org")
    older = submit(service, harness, pinned_model="opus", exclusions=[DESKTOP_EXCLUSION])
    newer = submit(service, harness, pinned_model="opus")
    wait_on_capacity(service, older)
    service._admit()
    assert lane_of(service, newer) == ["claude-9"]
    assert not service.store.list_attempts(older)


# --- the design review of 2026-09-30 --------------------------------------------------------------

def test_c6_9_a_waiter_pinned_to_one_lane_does_not_hold_a_free_job_that_would_take_another(pinned_fleet):
    """C-6.9: the older job may only use claude-b; the newer, free, would take claude-a (it ranks first)."""
    service, harness = pinned_fleet
    older = submit(service, harness, pinned_model="fable", pinned_lane="claude-b")
    newer = submit(service, harness, pinned_model="fable")
    wait_on_capacity(service, older)
    service._admit()
    assert lane_of(service, newer) == ["claude-a"]


def test_c6_9_a_waiter_for_another_providers_model_does_not_hold_a_job_on_a_claude_lane(pinned_fleet):
    """C-6.9: the older job runs astra only (Codex); the newer research job would take Opus on a Claude
    lane, which the older could never take, although their chains share astra."""
    service, harness = pinned_fleet
    older = submit(service, harness, pinned_model="astra")
    newer = submit(service, harness, pinned_model=None, task="research", tier="standard")
    wait_on_capacity(service, older)
    service._admit()
    assert lane_of(service, newer)[0] in ("claude-a", "claude-b")


def test_c6_9_a_lane_a_waiters_retry_excludes_is_one_it_cannot_take(three_codex):
    """C-4.5, C-6.9: the older job's attempt was limited on codex-2, so its retry never runs there; with
    codex-1 closed and codex-3 excluded by it, the newer job takes codex-2."""
    service, harness = three_codex
    close_astra(service, "codex-1")
    older = submit(service, harness, pinned_model="astra", exclusions=["codex-3"])
    service.store.add_attempt(attempt_id=older + "/a1", job_id=older, seq=1, lane_id="codex-2",
                              model_requested="gpt-6-astra", state="failed")
    with service.store.transaction("fixture.limited") as tx:
        tx.execute("UPDATE attempts SET outcome_class='limited' WHERE attempt_id=?", (older + "/a1",))
    newer = submit(service, harness, pinned_model="astra")
    wait_on_capacity(service, older)
    service._admit()
    assert lane_of(service, newer) == ["codex-2"]


def family(service, harness, parent, **changes):
    return submit(service, harness, pinned_model="astra", parent_job_id=parent, **changes)


def test_c6_4_c6_9_a_sibling_holds_a_sibling_wherever_it_would_run_while_the_parent_cap_is_set(three_codex):
    """C-6.4, C-6.9: with a parent cap, two children of one parent share its count wherever each runs, so
    the older holds the newer even on a lane it could not use itself."""
    service, harness = three_codex
    service.policy["caps"]["max_active_attempts_per_parent"] = 2
    close_astra(service, "codex-1")
    parent = submit(service, harness, pinned_model="terra")
    older = family(service, harness, parent, exclusions=["codex-2", "codex-3"])
    newer = family(service, harness, parent)
    wait_on_capacity(service, older)
    service._admit()
    assert not service.store.list_attempts(newer)
    assert service._holds[newer]["behind"] == older


def test_c6_4_c6_9_a_waiter_whose_own_family_is_full_holds_no_other_familys_job(three_codex):
    """C-6.4, C-6.9: the older job waits for its parent's only slot, which a sibling holds; it could not be
    placed on any lane, so a job of no family is not held behind it, submitted the same way or not."""
    service, harness = three_codex
    service.policy["caps"]["max_active_attempts_per_parent"] = 1
    parent = submit(service, harness, pinned_model="terra")
    running = family(service, harness, parent)
    service._admit()
    assert lane_of(service, running)
    older = family(service, harness, parent)
    newer = submit(service, harness, pinned_model="astra")
    wait_on_capacity(service, older)
    service._admit()
    assert lane_of(service, newer)


def test_c6_9_c11_4_a_job_is_held_before_its_probe_where_a_waiter_could_run(three_codex, monkeypatch):
    """C-6.9, C-11.4: the shared view said codex-3 (closed since); the job's own decision chose codex-2,
    which needs a probe (hard, unmeasured). The older job could run there, so no probe runs and the job
    waits behind it."""
    service, harness = three_codex
    close_astra(service, "codex-1")
    close_astra(service, "codex-2")
    stale = service._route_view(service._desktop_identity())[2]
    with service.store.transaction("fixture.reopen") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE lane_id='codex-2'", (utcnow(),))
        tx.execute("DELETE FROM readings WHERE lane_id='codex-2'")          # unmeasured: a hard job probes first
    close_astra(service, "codex-3")
    monkeypatch.setattr(service, "_hold_view", lambda desktop: stale)
    probes = []
    monkeypatch.setattr(service, "_execute_probe", lambda *args: probes.append(args) or pytest.fail("probed"))
    older = submit(service, harness, pinned_model="astra", tier="hard", exclusions=["codex-3"])
    newer = submit(service, harness, pinned_model="astra", tier="hard")
    wait_on_capacity(service, older)
    service._admit()
    assert probes == [] and not service.store.list_attempts(newer)
    assert (service._holds[newer]["reason"], service._holds[newer]["lane"]) == ("behind-older-job", "codex-2")
    assert not service.store.one("SELECT 1 FROM leases WHERE lease_key='lane:codex-2:slot:0'")


# --- the code review of 2026-09-30 --------------------------------------------------------------

def test_c6_9_a_job_held_behind_an_older_one_still_holds_a_newer_one(three_codex):
    """C-6.9: W1 (oldest) can use only codex-1, closed; W2 may use codex-2; N, submitted as W2 was, would
    take codex-2. N does not pass W2 because W1 cannot take codex-2: W2, held behind W1, is still a waiter."""
    service, harness = three_codex
    close_astra(service, "codex-1")
    w1 = submit(service, harness, pinned_model="astra", exclusions=["codex-2", "codex-3"])
    w2 = submit(service, harness, pinned_model="astra", exclusions=["codex-3"])
    newer = submit(service, harness, pinned_model="astra", exclusions=["codex-3"])
    wait_on_capacity(service, w1)
    wait_on_capacity(service, w2)
    service._admit()
    assert not service.store.list_attempts(newer)
    assert service._holds[newer]["behind"] == w2


def test_c6_11_a_hold_found_on_a_lane_keeps_its_lane_between_looks(three_codex, monkeypatch):
    """C-6.11: the reservation's hold names codex-2; the next pass, which does not look (the job is on its
    clock), reports the same hold in full, and `why` says the older job could run there."""
    service, harness = three_codex
    close_astra(service, "codex-1")
    close_astra(service, "codex-2")
    stale = service._route_view(service._desktop_identity())[2]
    with service.store.transaction("fixture.reopen") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE lane_id='codex-2'", (utcnow(),))
    monkeypatch.setattr(service, "_hold_view", lambda desktop: stale)
    older = submit(service, harness, pinned_model="astra", exclusions=["codex-3"])
    newer = submit(service, harness, pinned_model="astra")
    wait_on_capacity(service, older)
    service._admit()
    assert service._holds[newer]["lane"] == "codex-2"
    service._admit()
    assert service._holds[newer]["lane"] == "codex-2"
    text = service.dispatch("why", {"job_id": newer})["text"]
    assert f"held behind {older}, an older standard job that is waiting and could run on codex-2, where this one would" in text
