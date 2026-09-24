"""C-23.16: a reset credit is spent only for a job waiting on exactly that lane.

Incident, 2026-09-22 (ET): OpenAI banked one reset on each of six ChatGPT accounts
between 16:32 and 17:02. The timer spent all six in 4.5 hours, the first with no job
queued or running, because it fired whenever no lane was dispatchable, and a lane
running one job at its unmeasured slot cap looked exactly like an exhausted one.
These cases cover each part of the rule that replaced that trigger, (a) to (f).
The daemon-level replay is `tests/fake/test_reset_credit_incident.py`.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from subfleet.actions import (CAPACITY, LIMITED, RESERVATION_S, UNUSABLE, ResetCredits, demand_verdict,
                              lane_condition, route_lane_state)
from subfleet.adapters.codex import CodexAdapter
from subfleet.scheduler import evaluate
from subfleet.timers import Timers
from tests.unit.test_scheduler import attempt, closure, job, lane, policy, reading, view  # noqa: F401  (fixture)
from tests.unit.test_timers_reset_credits import (GIFT, HTTP, NOW, STAMP, component, limit_again, snapshot,
                                                  store, wants)  # noqa: F401  (fixture)
from tests.unit.test_timers_reset_credits import lane as limited_lane

SCHEDULER_NOW = "2026-09-05T10:33:00Z"


def posts(http):
    return sum(request.get_method() == "POST" for request, _ in http.calls)


def lists(http):
    return sum(request.get_method() == "GET" for request, _ in http.calls)


# --- (b) what a lane is to a waiting job ---------------------------------------

def codex_fleet(*states):
    """Codex lanes codex-1..n: `limited`, `open` (measured, room), `unmeasured`, or `busy`."""
    lanes, readings, closures, attempts = [], [], [], []
    for number, state in enumerate(states, 1):
        identity = f"codex-{number}"
        lanes.append(lane(identity))
        if state == "limited":
            readings.append(reading(identity, 1.))
            closures.append(closure(identity))
        elif state in ("open", "busy-measured"):
            readings.append(reading(identity, .4))
        if state.startswith("busy"):
            cap = 2 if state == "busy-measured" else 1
            attempts.extend(attempt(identity, f"filler-{identity}-{n}") for n in range(cap))
    return lanes, readings, closures, attempts


def astra_job(**changes):
    return job(task=None, tier="hard", pinned_model="astra", **changes)


def test_every_lane_limited_is_demand_on_exactly_those_lanes(policy):
    """C-23.16 (a), (b): a job no lane can take is demand, and names the lanes a credit could reopen."""
    lanes, readings, closures, _ = codex_fleet(*["limited"] * 6)
    verdict = demand_verdict(evaluate(policy, view(lanes, readings, closures), astra_job()))
    assert verdict == {"verdict": "codex-demand", "capacity_lanes": [],
                       "limited_lanes": [f"codex-{n}" for n in range(1, 7)]}


@pytest.mark.parametrize("state", ["open", "unmeasured", "busy-measured", "busy-unmeasured"])
def test_one_lane_with_room_or_a_slot_coming_means_the_job_waits(policy, state):
    """C-23.16 (b): busy, unmeasured, or measured with headroom, one such lane is capacity."""
    lanes, readings, closures, attempts = codex_fleet(*["limited"] * 5, state)
    verdict = demand_verdict(evaluate(policy, view(lanes, readings, closures, attempts), astra_job()))
    if state.startswith("busy"):
        assert verdict == {"verdict": "lane-has-capacity", "capacity_lanes": ["codex-6"],
                           "limited_lanes": [f"codex-{n}" for n in range(1, 6)]}
    else:
        assert verdict["verdict"] == "placeable" and verdict["capacity_lanes"] == ["codex-6"]


@pytest.mark.parametrize("holder", ["reset-reserved:another-job", "probe:abc"])
def test_a_lane_kept_for_another_job_or_held_by_a_probe_is_capacity(policy, holder):
    """C-23.16 (b), (c): a slot that frees itself is room coming, not a reason to spend."""
    lanes, readings, closures, _ = codex_fleet(*["limited"] * 5, "unmeasured")
    snapshot_ = {**view(lanes, readings, closures), "unavailable_lanes": {"codex-6": holder}}
    verdict = demand_verdict(evaluate(policy, snapshot_, astra_job()))
    assert verdict["verdict"] == "lane-has-capacity" and verdict["capacity_lanes"] == ["codex-6"]


def test_a_full_fleet_is_capacity(policy):
    """C-23.16 (b), C-6.4: at the fleet cap every lane waits for a slot; resetting one adds none."""
    lanes, readings, closures, _ = codex_fleet(*["limited"] * 5, "unmeasured")
    fillers = [attempt("codex-1", f"filler-{n}") for n in range(policy["caps"]["max_active_attempts"])]
    verdict = demand_verdict(evaluate(policy, view(lanes, readings, closures, fillers), astra_job()))
    assert verdict["verdict"] == "lane-has-capacity"


@pytest.mark.parametrize("unusable", ["excluded", "disabled", "owner-v1", "identity-mismatch",
                                      "credential-latched", "auth-dead", "operator-hold"])
def test_a_lane_the_job_cannot_use_is_neither_room_nor_demand(policy, unusable):
    """C-23.16 (b): a lane no quota would open for this job never holds it back or earns a credit."""
    lanes, readings, closures, _ = codex_fleet(*["limited"] * 5, "unmeasured")
    changes, snapshot_extra, spec = {}, {}, astra_job()
    if unusable == "excluded":
        spec = astra_job(exclusions=["codex-6"])
    elif unusable == "disabled":
        changes = {"enabled": False}
    elif unusable == "owner-v1":
        changes = {"owner": "v1"}
    elif unusable == "identity-mismatch":
        changes = {"identity_status": "mismatch"}
    elif unusable == "credential-latched":
        snapshot_extra = {"unavailable_lanes": {"codex-6": "credential-latched"}}
    else:
        closures.append(closure("codex-6", reason=unusable))
    lanes[-1].update(changes)
    verdict = demand_verdict(evaluate(policy, {**view(lanes, readings, closures), **snapshot_extra}, spec))
    assert verdict == {"verdict": "codex-demand", "capacity_lanes": [],
                       "limited_lanes": [f"codex-{n}" for n in range(1, 6)]}
    rejection = next(row for evaluation in evaluate(policy, {**view(lanes, readings, closures), **snapshot_extra},
                                                    spec).evaluations
                     for row in evaluation["rejections"] if row["lane_id"] == "codex-6")
    scoped = [row for row in closures if row["lane_id"] == "codex-6"]
    assert route_lane_state(rejection, scoped) == UNUSABLE


def test_nothing_a_credit_can_fix_is_not_demand(policy):
    """C-23.16 (a): every Codex lane out of the job's reach for other reasons: no credit helps."""
    lanes, readings, closures, _ = codex_fleet("unmeasured", "unmeasured")
    for row in lanes:
        row["enabled"] = False
    verdict = demand_verdict(evaluate(policy, view(lanes, readings, closures), astra_job()))
    assert verdict == {"verdict": "no-codex-demand", "capacity_lanes": [], "limited_lanes": []}


def test_a_claude_lane_with_a_slot_coming_in_the_route_means_the_job_waits(policy):
    """C-23.16 (b): a chain that promotes Opus to Astra waits for a busy Opus lane before any reset."""
    lanes, readings, closures, _ = codex_fleet("limited", "limited")
    lanes.append(lane("claude-1"))
    spec = job(task="review", tier="standard")                   # opus, then astra
    busy = view(lanes, readings, closures, [attempt("claude-1", "opus-filler")])
    assert demand_verdict(evaluate(policy, busy, spec)) == {
        "verdict": "lane-has-capacity", "capacity_lanes": ["claude-1"], "limited_lanes": ["codex-1", "codex-2"]}
    closed = view(lanes, readings, [*closures, closure("claude-1")])
    assert demand_verdict(evaluate(policy, closed, spec))["verdict"] == "codex-demand"


def test_below_the_floor_counts_only_on_a_fresh_reading(policy):
    """C-23.16 (b): a lane is limited by the floor only on a fresh measured reading."""
    lanes, readings, closures, _ = codex_fleet("limited")
    lanes.append(lane("codex-2"))
    fresh = view(lanes, [*readings, reading("codex-2", .9)], closures)
    assert demand_verdict(evaluate(policy, fresh, astra_job()))["limited_lanes"] == ["codex-1", "codex-2"]
    stale = view(lanes, [*readings, reading("codex-2", .9, observed_at="2026-09-05T09:00:00Z")], closures)
    assert demand_verdict(evaluate(policy, stale, astra_job()))["verdict"] == "placeable"


def test_a_lane_is_judged_by_its_best_state_across_the_route(policy):
    """C-23.16 (b): closed for Astra but with room for Terra, a lane is capacity to a sweep that can use both."""
    lanes = [lane("codex-1"), lane("codex-2")]
    closures = [closure("codex-1"), closure("codex-2", scope="gpt-6-astra")]
    snapshot_ = view(lanes, [reading("codex-1", 1.)], closures, [attempt("codex-2", "terra-filler")])
    verdict = demand_verdict(evaluate(policy, snapshot_, job(task="sweep", tier="standard")))  # terra, astra
    assert verdict["verdict"] == "lane-has-capacity" and verdict["capacity_lanes"] == ["codex-2"]


# --- (d) a lane's own state after a reset --------------------------------------

def test_a_freshly_reset_lane_with_a_guessed_clock_and_no_reading_is_capacity():
    """C-23.16 (b), (d), C-23.17: a confirmed reset reopens the lane until a fresh reading limits it."""
    row = {"lane_id": "codex-1", "enabled": True, "owner": "v2", "closures": [], "readings": []}
    assert lane_condition(row, now=STAMP, override=True) == CAPACITY
    # The old window, relabelled or still fresh, cannot close it while the override stands.
    old = {"scope": "account", "window": "seven_day", "utilization": 1., "label": "provider",
           "observed_at": STAMP, "resets_at": "2026-09-08T12:00:00Z"}
    assert lane_condition({**row, "readings": [old]}, now=STAMP, override=True) == CAPACITY
    assert lane_condition({**row, "readings": [old]}, now=STAMP, override=False) == LIMITED
    assert lane_condition({**row, "readings": [{**old, "observed_at": "2026-09-05T11:00:00Z"}]},
                          now=STAMP) == CAPACITY           # stale: not shown limited
    limit = {"scope": "account", "reason": "provider-limit", "until_at": "2026-09-06T12:00:00Z"}
    assert lane_condition({**row, "closures": [limit]}, now=STAMP, override=True) == LIMITED
    assert lane_condition({**row, "closures": [{**limit, "scope": "gpt-6-astra"}]}, now=STAMP) == CAPACITY
    assert lane_condition({**row, "closures": [{**limit, "reason": "auth-dead"}]}, now=STAMP) == UNUSABLE
    assert lane_condition({**row, "probe_status": "revoked"}, now=STAMP) == UNUSABLE
    assert lane_condition({**row, "enabled": False}, now=STAMP) == UNUSABLE


# --- the timer: (a) demand, (d) one at a time ----------------------------------

def test_no_waiting_job_spends_nothing_with_every_lane_limited(store, tmp_path):
    """C-23.16 (a), the incident's first redemption: six limited lanes, six gifts, nobody waiting."""
    for number in range(1, 7):
        limited_lane(store, tmp_path, number, days=number)
    http = HTTP()
    resets = component(store, http, min_interval_min=0)
    for demand in (None, [], lambda: [], lambda: None):
        result = resets.evaluate(snapshot(store), now=NOW, demand=demand)
        assert result["status"] == "no-demand"
    assert not http.calls and not store.query("SELECT * FROM actions")


def test_the_queue_is_not_read_unless_every_cheaper_gate_passes(store, tmp_path):
    """C-23.16 (a), (f): a disabled, held, or interval-blocked timer never evaluates the queue."""
    limited_lane(store, tmp_path)
    calls = []
    def demand():
        calls.append(1)
        return wants(store)
    assert component(store, HTTP(), enabled=False).evaluate(snapshot(store), now=NOW, demand=demand)["status"] == "disabled"
    held = component(store, HTTP())
    held.inhibit = tmp_path / "no-reset"
    held.inhibit.write_text("{}")
    assert held.evaluate(snapshot(store), now=NOW, demand=demand)["status"] == "inhibited"
    assert not calls
    resets = component(store, HTTP())
    assert resets.evaluate(snapshot(store), now=NOW, demand=demand)["status"] == "confirmed"
    assert resets.evaluate(snapshot(store), now=NOW, demand=demand)["status"] == "interval-blocked"
    assert calls == [1]


def test_a_lane_reset_this_week_with_room_blocks_the_next_spend_whatever_the_interval(store, tmp_path):
    """C-23.16 (c), (d): six limited lanes, one gift each, more jobs than slots: one reset at a time."""
    for number in range(1, 7):
        limited_lane(store, tmp_path, number, days=number)
    http = HTTP()
    resets = component(store, http, min_interval_min=0)
    first = resets.evaluate(snapshot(store), now=NOW, demand=wants(store))
    assert first["status"] == "confirmed" and first["lane_id"] == "codex-6"    # furthest weekly reset
    later = NOW
    for minute in range(1, 500, 7):
        later = NOW + timedelta(minutes=minute)
        # Busy at its slot cap, the reset lane is where every waiting job goes next.
        view_ = snapshot(store, **{"codex-6": {"in_flight": 1}})
        for row in view_["lanes"]:
            row["probe"]["checked_at"] = later.isoformat().replace("+00:00", "Z")
        result = resets.evaluate(view_, now=later, demand=wants(store, job=f"job-{minute}"))
        assert result["status"] == "reset-lane-open" and result["reset_lanes"] == ["codex-6"]
    assert posts(http) == 1 and lists(http) == 1
    # Used up again (an attempt came back limited): the next job may have the next lane.
    limit_again(store, "codex-6")
    view_ = snapshot(store)
    for row in view_["lanes"]:
        row["probe"]["checked_at"] = later.isoformat().replace("+00:00", "Z")
    second = resets.evaluate(view_, now=later, demand=wants(store, job="job-next"))
    assert second["status"] == "confirmed" and second["lane_id"] == "codex-5" and second["job_id"] == "job-next"
    assert posts(http) == 2


def test_the_first_waiting_job_a_credit_can_help_gets_it(store, tmp_path):
    """C-23.16 (c): a job whose lanes hold no gift does not block a later job whose lane does."""
    limited_lane(store, tmp_path, 1, days=6)
    limited_lane(store, tmp_path, 2, days=2)
    def opener(request, timeout):
        account = dict((key.lower(), value) for key, value in request.header_items())["chatgpt-account-id"]
        if account == "1":
            return 200, b'{"credits":[]}'
        return HTTP()(request, timeout)
    seen = []
    def counting(request, timeout):
        seen.append((request.get_method(), dict((k.lower(), v) for k, v in request.header_items())["chatgpt-account-id"]))
        return opener(request, timeout)
    resets = component(store, counting)
    demand = wants(store, "codex-1", job="pinned-to-one") + wants(store, "codex-1", "codex-2", job="either")
    result = resets.evaluate(snapshot(store), now=NOW, demand=demand)
    assert result["status"] == "confirmed" and result["lane_id"] == "codex-2" and result["job_id"] == "either"
    assert seen == [("GET", "1"), ("GET", "2"), ("POST", "2")]          # each lane listed once


def test_selection_is_furthest_weekly_reset_first_and_shadowed_last(store, tmp_path):
    """C-23.16 (c), C-23.38, C-23.46: among the job's limited lanes, furthest reset, unshadowed first."""
    for number, days in [(1, 6), (2, 5), (3, 2), (4, 4)]:
        limited_lane(store, tmp_path, number, days=days)
    view_ = snapshot(store, **{"codex-1": {"app_shadowed": True}})
    result = component(store, HTTP()).evaluate(view_, now=NOW, demand=wants(store, "codex-1", "codex-3", "codex-4"))
    assert result["lane_id"] == "codex-4"       # codex-2 is further out, but not a lane this job can use


def test_no_reset_marker_refuses_automatic_and_operator_consumes(store, tmp_path):
    """C-23.16 (f): the marker's existence refuses every consume; a dangling link still does."""
    target = limited_lane(store, tmp_path)
    marker = tmp_path / "no-reset"
    for make in (lambda: marker.write_text('{"reason":"operator hold"}'),
                 lambda: os.symlink(tmp_path / "gone", marker)):
        make()
        http = HTTP()
        resets = component(store, http)
        resets.inhibit = marker
        automatic = resets.evaluate(snapshot(store), now=NOW, demand=wants(store))
        operator = resets.evaluate(snapshot(store), now=NOW, target_lane_id=target.lane_id)
        preview = resets.evaluate(snapshot(store), now=NOW, target_lane_id=target.lane_id, dry_run=True)
        for result in (automatic, operator, preview):
            assert result["status"] == "inhibited" and result["inhibited_by"] == str(marker)
        assert not http.calls and not store.query("SELECT * FROM actions")
        marker.unlink()


def test_a_marker_that_appears_after_listing_is_honored_before_the_action_is_written(store, tmp_path):
    """C-23.16 (f): the pending transaction checks the marker again; no credit is fenced by a refusal."""
    limited_lane(store, tmp_path)
    marker = tmp_path / "no-reset"
    http = HTTP()
    def opener(request, timeout):
        marker.write_text("{}")                  # the operator holds resets while the list is in flight
        return http(request, timeout)
    resets = component(store, opener)
    resets.inhibit = marker
    assert resets.evaluate(snapshot(store), now=NOW, demand=wants(store))["status"] == "inhibited"
    assert [request.get_method() for request, _ in http.calls] == ["GET"]
    assert not store.query("SELECT * FROM actions")


def test_a_marker_that_appears_after_the_claim_is_honored_at_the_consume_boundary(store, tmp_path, monkeypatch):
    """C-23.16 (f): the consume itself refuses; the action fails without a POST."""
    limited_lane(store, tmp_path)
    marker = tmp_path / "no-reset"
    http = HTTP()
    resets = component(store, http)
    resets.inhibit = marker
    claim = resets.actions.claim
    def claim_then_hold(*args, **kwargs):
        claimed = claim(*args, **kwargs)
        marker.write_text("{}")
        return claimed
    monkeypatch.setattr(resets.actions, "claim", claim_then_hold)
    result = resets.evaluate(snapshot(store), now=NOW, demand=wants(store))
    assert result["status"] == "failed"
    action = store.get_action(result["action_id"])
    assert json.loads(action["result_json"]) == {"status": "inhibited", "inhibited_by": str(marker)}
    assert posts(http) == 0 and store.list_closures(active_at=STAMP)


def test_a_job_that_left_the_queue_is_not_spent_for(store, tmp_path):
    """C-23.16 (a): the job must still be waiting when the action is written."""
    limited_lane(store, tmp_path)
    demand = wants(store)
    store.update_job("job-1", cancel_requested_at=STAMP)
    http = HTTP()
    result = component(store, http).evaluate(snapshot(store), now=NOW, demand=demand)
    assert result["status"] == "demand-gone" and result["job_id"] == "job-1"
    assert posts(http) == 0 and not store.query("SELECT * FROM actions")


def test_a_reset_is_kept_for_its_job_until_placed_gone_or_expired(store, tmp_path):
    """C-23.16 (c): the lane is that job's for `RESERVATION_S`, and due at once."""
    target = limited_lane(store, tmp_path)
    resets = component(store, HTTP())
    result = resets.evaluate(snapshot(store), now=NOW, demand=wants(store))
    assert result["status"] == "confirmed"
    assert store.get_job("job-1")["next_check_at"] == STAMP
    assert result["reserved_until"] == "2026-09-05T12:15:00Z"
    events = store.query("SELECT job_id,lane_id,data_json FROM events WHERE kind='reset-credit.reserved' "
                         "AND data_json!='{}'")    # the store audits each event row with an empty twin
    assert [(row["job_id"], row["lane_id"]) for row in events] == [("job-1", target.lane_id)]
    assert json.loads(events[0]["data_json"]) == {"action_id": result["action_id"], "job_id": "job-1",
                                                  "lane_id": target.lane_id, "until": "2026-09-05T12:15:00Z"}
    assert resets.reservations(now=NOW) == {target.lane_id: "job-1"}
    assert resets.reservations(now=NOW + timedelta(seconds=RESERVATION_S)) == {target.lane_id: "job-1"}
    assert resets.reservations(now=NOW + timedelta(seconds=RESERVATION_S + 1)) == {}
    store.add_attempt(attempt_id="job-1/a1", job_id="job-1", seq=1, lane_id=target.lane_id,
                      model_requested="gpt-6-astra", reserved_at=STAMP)
    assert resets.reservations(now=NOW) == {}          # placed: the reset reached its job
    with store.transaction() as tx:
        tx.execute("DELETE FROM attempts")
    store.update_job("job-1", state="cancelled")
    assert resets.reservations(now=NOW) == {}          # gone


def test_an_operator_reset_reserves_nothing(store, tmp_path):
    """C-23.16 (e): an operator's lane is no job's."""
    target = limited_lane(store, tmp_path)
    resets = component(store, HTTP(), enabled=False)
    result = resets.evaluate(snapshot(store), now=NOW, target_lane_id=target.lane_id)
    assert result["status"] == "confirmed" and result["trigger_reason"] == "operator"
    assert resets.reservations(now=NOW) == {}
    assert not store.query("SELECT * FROM events WHERE kind='reset-credit.reserved'")


def test_the_default_policy_ships_automatic_redemption_off(store, tmp_path):
    """C-23.16 (f): with no setting, or the shipped one, nothing is spent automatically."""
    shipped = json.loads(Path("subfleet/default_policy.json").read_text())
    assert shipped["reset_credits"] == {"enabled": False, "min_interval_min": 30}
    limited_lane(store, tmp_path)
    http = HTTP()
    for policy_ in ({}, shipped):
        resets = ResetCredits(store, policy_, lambda lane_: CodexAdapter(opener=http, now=lambda: NOW))
        assert resets.evaluate(snapshot(store), now=NOW, demand=wants(store))["status"] == "disabled"
    assert not http.calls


def test_a_demand_reader_that_fails_is_no_demand(store, tmp_path):
    """C-23.16 (a): an unreadable queue never spends a credit; the failure is recorded, a preview's is not."""
    def broken():
        raise KeyError("queue")
    timers = Timers(store, tmp_path, {"reset_credits": {"enabled": True}}, demand=broken)
    try:
        assert timers.current_demand() is None
        assert timers.current_demand(record=False) is None
        errors = [json.loads(row["data_json"]) for row in store.query(
            "SELECT data_json FROM events WHERE kind='timer.error' AND data_json!='{}'")]
        assert errors == [{"timer": "reset_credits", "stage": "demand", "error_type": "KeyError"}]
        assert timers.actions.inhibit == tmp_path / "no-reset"
    finally:
        timers.stop()
    bare = Timers(store, tmp_path, {})
    try:
        assert bare.current_demand() is None
    finally:
        bare.stop()


def test_one_evaluation_at_a_time_holds_while_the_queue_is_read(store, tmp_path):
    """C-18.1: a second pass started while the first reads the queue spends nothing."""
    limited_lane(store, tmp_path)
    resets = component(store, HTTP())
    entered, release = threading.Event(), threading.Event()
    def slow():
        entered.set()
        release.wait(2)
        return []
    worker = threading.Thread(target=lambda: resets.evaluate(snapshot(store), now=NOW, demand=slow))
    worker.start()
    try:
        assert entered.wait(2)
        assert resets.evaluate(snapshot(store), now=NOW, demand=wants(store))["status"] == "evaluation-running"
    finally:
        release.set()
        worker.join(2)
