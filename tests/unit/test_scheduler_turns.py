"""C-26.2, C-26.9: how admission treats attended conversation turns."""

from __future__ import annotations

import copy

from hypothesis import HealthCheck, given, settings, strategies as st
from subfleet.scheduler import evaluate, ordered_jobs, probe_required, waiter_class
from tests.unit.test_scheduler import (  # noqa: F401  (fixtures)
    attempt,
    NOW, TOMORROW, closure, job, lane, policy, reading, reserve_policy, view,
)


def test_turns_go_first_as_the_attended_class(policy):
    """C-6.9, C-26.9: a turn is the `attended` class, which sorts ahead of every
    detached job whatever its tier; among detached jobs tiers still order."""
    jobs = [dict(job(), job_id="old", created_at="2026-09-24T10:00:00Z"),
            dict(job(), job_id="turn", kind="turn", created_at="2026-09-24T11:00:00Z"),
            dict(job(tier="trivial"), job_id="trivial", created_at="2026-09-24T12:00:00Z")]
    assert [j["job_id"] for j in ordered_jobs(policy, jobs)] == ["turn", "trivial", "old"]


def test_turns_and_detached_jobs_wait_in_separate_queues():
    """C-26.9 a waiting turn never holds a detached job back, nor the reverse."""
    assert waiter_class({"kind": "turn"}, "standard") != waiter_class({"kind": "dispatch"}, "standard")
    assert waiter_class({"kind": "resume"}, "standard") == "standard"


def test_a_turn_refuses_a_lane_that_needs_a_probe(reserve_policy):
    """C-26.9 with Fable closed on the account the reserve demands a probe for Opus; a
    detached job takes the lane (and is probed), a turn does not."""
    lanes = [lane("claude-1")]
    rows = [reading("claude-1", .3, source="usage"), reading("claude-1", .3, scope="claude-opus-5-5", source="usage")]
    snapshot = view(lanes, rows, closures=[closure("claude-1", "claude-fable-5-1")])
    detached = evaluate(reserve_policy, snapshot, job(pinned_model="opus", task=None, tier=None))
    turn = evaluate(reserve_policy, snapshot, job(pinned_model="opus", task=None, tier=None, kind="turn"))
    assert detached.chosen_lane == "claude-1"
    assert probe_required(detached, job(pinned_model="opus", task=None, tier=None))
    assert turn.chosen_lane is None
    assert turn.evaluations[0]["rejections"][0]["reasons"][-1] == "reserve:fable:probe-required"
    assert not probe_required(turn, job(kind="turn"))


def test_a_home_lane_is_not_a_claude_turn_candidate(policy):
    """C-26.2 a Claude lane with its own config directory cannot resume the conversation."""
    snapshot = view([lane("claude-1", credential_kind="home"), lane("claude-2", credential_kind="keychain-token")],
                    [reading("claude-1", .1), reading("claude-2", .5)])
    turn = evaluate(policy, snapshot, job(pinned_model="opus", kind="turn"))
    assert turn.chosen_lane == "claude-2"
    assert evaluate(policy, snapshot, job(pinned_model="opus")).chosen_lane == "claude-1"


def test_a_conversation_keeps_its_account_while_it_is_a_candidate(policy):
    """C-26.2 affinity outranks headroom for turns; a closed affinity lane is left."""
    lanes = [lane("claude-1"), lane("claude-2")]
    rows = [reading("claude-1", .6), reading("claude-2", .1)]
    assert evaluate(policy, view(lanes, rows), job(pinned_model="opus", kind="turn")).chosen_lane == "claude-2"
    kept = evaluate(policy, view(lanes, rows), job(pinned_model="opus", kind="turn", affinity_lane="claude-1"))
    assert kept.chosen_lane == "claude-1"
    moved = evaluate(policy, view(lanes, rows, closures=[closure("claude-1")]),
                     job(pinned_model="opus", kind="turn", affinity_lane="claude-1"))
    assert moved.chosen_lane == "claude-2"
    # Affinity is a turn's only; a detached job routes on headroom.
    assert evaluate(policy, view(lanes, rows), job(pinned_model="opus", affinity_lane="claude-1")).chosen_lane == "claude-2"


def _running(lane_id, job_id, kind):
    return attempt(lane_id, job_id), {"job_id": job_id, "kind": kind, "state": "running"}


def test_a_turn_has_its_own_capacity_while_detached_work_fills_the_fleet(policy):
    """C-26.9 detached jobs holding every lane slot and the whole fleet cap leave a
    turn its own slot: an attended turn never waits behind background work."""
    lanes = [lane("claude-1"), lane("claude-2")]
    rows = [reading("claude-1", .2), reading("claude-2", .3)]
    pairs = [_running("claude-1", f"d{i}", "dispatch") for i in range(2)] + \
            [_running("claude-2", f"e{i}", "dispatch") for i in range(2)]
    snapshot = view(lanes, rows, attempts=[a for a, _ in pairs], jobs=[j for _, j in pairs])
    assert sum(snapshot["in_flight"].values()) == policy["caps"]["max_active_attempts"]
    detached = evaluate(policy, snapshot, job(pinned_model="opus"))
    assert detached.chosen_lane is None
    turn = evaluate(policy, snapshot, job(pinned_model="opus", kind="turn"))
    assert turn.chosen_lane in ("claude-1", "claude-2")


def _capped(policy, *, fleet, per_lane):
    capped = copy.deepcopy(policy)
    capped["conversations"].update(max_active_turns=fleet, turn_slots_per_lane=per_lane)
    return capped


def test_turn_caps_set_in_policy_fill_their_own_slots_and_fleet_cap(policy):
    """C-26.9 with caps set, one turn per lane (`conversations.turn_slots_per_lane`) and
    at most `conversations.max_active_turns` across the fleet, counted apart from detached jobs."""
    policy = _capped(policy, fleet=3, per_lane=1)
    lanes = [lane("claude-1"), lane("claude-2"), lane("claude-3"), lane("claude-4")]
    rows = [reading(f"claude-{n}", .1 * n) for n in range(1, 5)]
    one = [_running("claude-1", "t1", "turn")]
    snapshot = view(lanes, rows, attempts=[a for a, _ in one], jobs=[j for _, j in one])
    turn = evaluate(policy, snapshot, job(pinned_model="opus", kind="turn", affinity_lane="claude-1"))
    assert turn.chosen_lane != "claude-1"
    reasons = {r["lane_id"]: r["reasons"] for r in turn.evaluations[0]["rejections"]}
    assert "no-slot" in reasons["claude-1"]
    # A detached job does not count the turn against the lane.
    assert evaluate(policy, snapshot, job(pinned_model="opus", pinned_lane="claude-1")).chosen_lane == "claude-1"
    full = [_running(f"claude-{n}", f"t{n}", "turn") for n in range(1, 1 + policy["conversations"]["max_active_turns"])]
    snapshot = view(lanes, rows, attempts=[a for a, _ in full], jobs=[j for _, j in full])
    assert evaluate(policy, snapshot, job(pinned_model="opus", kind="turn")).chosen_lane is None
    assert evaluate(policy, snapshot, job(pinned_model="opus")).chosen_lane is not None


def test_turns_are_uncapped_by_default(policy):
    """C-26.9 the shipped policy sets no turn cap: any number of turns may run on one
    lane and across the fleet, and a conversation keeps its lane however busy it is."""
    assert policy["conversations"]["max_active_turns"] is None
    assert policy["conversations"]["turn_slots_per_lane"] is None
    lanes = [lane("claude-1"), lane("claude-2")]
    rows = [reading("claude-1", .1), reading("claude-2", .2)]
    busy = [_running("claude-1", f"t{n}", "turn") for n in range(6)] + \
           [_running("claude-2", f"u{n}", "turn") for n in range(6)]
    snapshot = view(lanes, rows, attempts=[a for a, _ in busy], jobs=[j for _, j in busy])
    kept = evaluate(policy, snapshot, job(pinned_model="opus", kind="turn", affinity_lane="claude-1"))
    assert kept.chosen_lane == "claude-1"
    assert not kept.evaluations[0]["rejections"]
    assert evaluate(policy, snapshot, job(pinned_model="opus", kind="turn")).chosen_lane in ("claude-1", "claude-2")


def test_the_2026_09_27_turn_wait_no_longer_happens(policy):
    """C-26.9 incident, 2026-09-27: every Claude lane but two was closed, disabled or the
    desktop login, and each of the two ran another conversation's turn. With one turn
    per lane a third conversation's turn waited 12 minutes; with no cap it is placed."""
    lanes = [lane("claude-1"), lane("claude-2"), lane("claude-4", desktop=True),
             lane("claude-9"), lane("claude-11", enabled=False)]
    rows = [reading(identity, .3) for identity in ("claude-1", "claude-2", "claude-4", "claude-9", "claude-11")]
    others = [_running("claude-1", "turn-a", "turn"), _running("claude-9", "turn-b", "turn")]
    snapshot = view(lanes, rows, closures=[closure("claude-2")],
                    attempts=[a for a, _ in others], jobs=[j for _, j in others])
    turn = job(pinned_model="opus", kind="turn")
    assert evaluate(policy, snapshot, turn).chosen_lane in ("claude-1", "claude-9")
    # The rule it replaces held the same turn on every lane but the desktop login's, which refused it
    # then; since 2026-09-30 that lane takes it, the chain's last resort (C-10.3), and a turn is never
    # refused by its reserve.
    assert evaluate(_capped(policy, fleet=3, per_lane=1), snapshot, turn).chosen_lane == "claude-4"
    kept_off = {**turn, "exclusions": ["@desktop"]}
    assert evaluate(_capped(policy, fleet=3, per_lane=1), snapshot, kept_off).chosen_lane is None


TURN_CAPS = st.sampled_from([None, 0, 1, 2, 3])


def _at_most(low, high):
    """Cap `low` admits no more than cap `high` (None is no cap)."""
    return high is None or (low is not None and low <= high)


def _open_lanes(decision):
    rejected = {row["lane_id"] for evaluation in decision.evaluations for row in evaluation["rejections"]}
    return {f"claude-{n}" for n in range(1, 4)} - rejected


# `policy` is read, never changed: each example caps a deep copy of it.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(running=st.lists(st.integers(0, 4), min_size=3, max_size=3),
       detached=st.lists(st.integers(0, 2), min_size=3, max_size=3),
       fleet_caps=st.tuples(TURN_CAPS, TURN_CAPS), lane_caps=st.tuples(TURN_CAPS, TURN_CAPS),
       affinity=st.sampled_from([None, "claude-1", "claude-2", "claude-3"]))
def test_turn_cap_invariants(policy, running, detached, fleet_caps, lane_caps, affinity):
    """C-26.9, for every fleet of running turns and detached jobs and every pair of caps:
    (1) with no caps a turn is placed on an eligible lane and no lane refuses it for room;
    (2) raising a cap never refuses a turn a lane the lower cap admitted (monotone);
    (3) turn caps never change a detached job's decision."""
    lanes = [lane(f"claude-{n}") for n in range(1, 4)]
    rows = [reading(f"claude-{n}", .1 * n) for n in range(1, 4)]
    pairs = [_running(f"claude-{n + 1}", f"t{n}-{i}", "turn") for n, count in enumerate(running) for i in range(count)]
    pairs += [_running(f"claude-{n + 1}", f"d{n}-{i}", "dispatch") for n, count in enumerate(detached) for i in range(count)]
    snapshot = view(lanes, rows, attempts=[a for a, _ in pairs], jobs=[j for _, j in pairs])
    turn = job(pinned_model="opus", kind="turn", **({"affinity_lane": affinity} if affinity else {}))

    uncapped = evaluate(_capped(policy, fleet=None, per_lane=None), snapshot, turn)
    assert uncapped.chosen_lane is not None
    assert not any("no-slot" in row["reasons"] for row in uncapped.evaluations[0]["rejections"])
    if affinity:
        assert uncapped.chosen_lane == affinity

    looser = lambda cap: (cap is None, cap or 0)                  # noqa: E731  (None sorts last: no cap)
    fleet, per_lane = sorted(fleet_caps, key=looser), sorted(lane_caps, key=looser)
    lower = _capped(policy, fleet=fleet[0], per_lane=per_lane[0])
    higher = _capped(policy, fleet=fleet[1], per_lane=per_lane[1])
    assert _at_most(fleet[0], fleet[1]) and _at_most(per_lane[0], per_lane[1])
    admitted_low, admitted_high = (_open_lanes(evaluate(lower, snapshot, turn)),
                                   _open_lanes(evaluate(higher, snapshot, turn)))
    assert admitted_low <= admitted_high
    if evaluate(lower, snapshot, turn).chosen_lane is not None:
        assert evaluate(higher, snapshot, turn).chosen_lane is not None

    background = job(pinned_model="opus")
    assert evaluate(lower, snapshot, background).chosen_lane == evaluate(higher, snapshot, background).chosen_lane
