"""C-26.2, C-26.9: how admission treats attended conversation turns."""

from __future__ import annotations

from subfleet.scheduler import evaluate, ordered_jobs, probe_required, waiter_class
from tests.unit.test_scheduler import (  # noqa: F401  (fixtures)
    NOW, TOMORROW, closure, job, lane, policy, reading, reserve_policy, view,
)


def test_turns_go_first_within_their_tier(policy):
    """C-26.9 a turn sorts ahead of older detached jobs of its tier, and tiers still order."""
    jobs = [dict(job(), job_id="old", created_at="2026-09-24T10:00:00Z"),
            dict(job(), job_id="turn", kind="turn", created_at="2026-09-24T11:00:00Z"),
            dict(job(tier="trivial"), job_id="trivial", created_at="2026-09-24T12:00:00Z")]
    assert [j["job_id"] for j in ordered_jobs(policy, jobs)] == ["trivial", "turn", "old"]


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
