"""C-11.11: fill explanations, conservative pick, and cached route clocks."""

from dataclasses import asdict
from datetime import timedelta
import json

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import capacity, cli, picker, route_check, scheduler
from tests.unit.test_scheduler_weekly import CLOCK, NOW, lane, policy, reading, reset


def rules(**changes):
    result = policy(spread=None)
    result["admission"].update(lane_order={"claude": "fill", "codex": "reset"}, **changes)
    return result


def test_fill_pick_and_why_show_the_same_fields_and_keep_freshness(monkeypatch, capsys):
    configured = rules(expiring_s=None)
    lanes = [lane(f"claude-{n}", label=f"fixture-{n}@example.invalid") for n in range(3)]
    rows = [reading("claude-0", .8, reset(500_000)),
            reading("claude-1", .3, reset(400_000)),
            reading("claude-2", .1, reset(400_000), age=3600)]
    snapshot = capacity.build_view(lanes, rows, now=NOW)
    result = picker.rank(configured, snapshot, family="claude", model="opus")
    assert [row["lane_id"] for row in result["ranked"]] == ["claude-1", "claude-0"]
    assert result["excluded"][0]["reasons"] == ["fresh-usage-required"]
    detail = result["ranked"][0]
    assert detail["lane_order"] == "fill"
    assert detail["fill_weekly_used"] == pytest.approx(.7)
    assert detail["fill_weekly_reset"] == reset(400_000)
    assert detail["fill_five_hour_used"] == 0
    assert detail["fill_expiring"] is False

    class Client:
        def call(self, op, args):
            return result

    monkeypatch.setattr(cli, "_client", lambda args: Client())
    assert cli.main(["pick", "claude", "--model", "opus", "--all"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "fixture-1@example.invalid\n"
    expected = (f"lane_order=fill fill_weekly_used=70.0% fill_weekly_reset={reset(400_000)} "
                "fill_five_hour_used=0.0% fill_expiring=false")
    assert expected in captured.err
    decision = scheduler.evaluate(configured, snapshot, {"pinned_model": "opus", "sandbox": "workspace-write"})
    # Admission still accepts the stale lane with its original probe predicate.
    assert decision.chosen_lane == "claude-2"
    reset_rules = rules(expiring_s=None)
    reset_rules["admission"]["lane_order"] = {"claude": "reset"}
    reset_job = {"pinned_model": "opus", "pinned_lane": "claude-2", "sandbox": "workspace-write"}
    baseline = scheduler.evaluate(reset_rules, snapshot, reset_job)
    assert scheduler.probe_required(decision, reset_job) == scheduler.probe_required(baseline, reset_job)
    assert scheduler.probe_required(decision, reset_job)
    assert "lane_order=fill fill_weekly_used=0.9" in decision.reason
    assert "fill_weekly_used=90.0%" in cli._format_decision(json.loads(json.dumps(asdict(decision))))


def test_fill_unknown_model_pick_binds_all_applicable_scopes():
    configured = rules(expiring_s=None)
    model_id = configured["models"]["fable"]["id"]
    lanes = [lane(f"claude-{n}", label=f"fixture-{n}@example.invalid") for n in range(2)]
    rows = [reading("claude-0", .8, reset(400_000)),
            reading("claude-0", .2, reset(500_000), scope=model_id, age=3600),
            reading("claude-1", .5, reset(400_000))]
    snapshot = capacity.build_view(lanes, rows, now=NOW)
    assert picker.rank(configured, snapshot, family="claude", model="opus")["ranked"][0]["lane_id"] == "claude-1"
    result = picker.rank(configured, snapshot, family="claude")
    assert result["ranked"][0]["lane_id"] == "claude-0"
    assert result["ranked"][0]["fill_weekly_used"] == pytest.approx(.8)
    assert result["ranked"][0]["fill_weekly_reset"] == reset(500_000)


@pytest.mark.parametrize("boundary", ["expiring", "reset"])
@settings(max_examples=100, deadline=None, derandomize=True)
@given(seconds=st.integers(1, 100), threshold=st.integers(1000, 100_000), age=st.integers(121, 500_000))
def test_fill_cached_route_matches_evaluate_at_old_window_boundaries(boundary, seconds, threshold, age):
    configured = rules(expiring_s=threshold if boundary == "expiring" else None)
    lanes = [lane("claude-0"), lane("claude-1")]
    moving = "claude-1" if boundary == "expiring" else "claude-0"
    rows = [reading("claude-0", .2, reset(600_000), age=age),
            reading("claude-1", .8, reset(600_000), age=age)]
    rows[1 if boundary == "expiring" else 0]["resets_at"] = reset(
        seconds + threshold if boundary == "expiring" else seconds)
    snapshot = capacity.build_view(lanes, rows, now=NOW)
    job = {"pinned_model": "opus", "sandbox": "read-only"}
    decision = scheduler.evaluate(configured, snapshot, job)
    assert decision.chosen_lane == "claude-0"
    clocks = capacity.lane_horizons(snapshot, policy=configured)
    assert clocks[moving] == CLOCK + timedelta(seconds=seconds)
    later = CLOCK + timedelta(seconds=seconds)
    verdict, judged, standing = route_check.still_stands(
        configured, job, decision, view=snapshot, candidates=rows, overridden=(), clocks=clocks,
        now=later, lanes=lanes, readings=(), closures=(), attempts=(), jobs=(), unavailable={}, reserved_probes=0)
    full = scheduler.evaluate(configured, capacity.build_view(lanes, rows, now=later), job)
    assert verdict is None
    assert judged == 1
    assert full.chosen_lane == "claude-1"
    assert asdict(standing) == asdict(full)


def test_fill_expiring_horizon_accepts_arbitrarily_large_whole_seconds():
    configured = rules(expiring_s=10**400)
    snapshot = capacity.build_view([lane("claude-0")],
        [reading("claude-0", .5, reset(600_000), age=1000)], now=NOW)
    assert capacity.lane_horizons(snapshot, policy=configured) == {"claude-0": CLOCK + timedelta(seconds=600_000)}


def test_per_provider_spread_maps_apply_to_each_ranking_order():
    configured = rules(expiring_s=None)
    configured["admission"]["lane_spread"] = {"claude": None, "codex": 2}
    lanes = [lane(f"{provider}-{n}") for provider in ("claude", "codex") for n in range(2)]
    rows = [reading("claude-0", .2, reset(600_000)), reading("claude-1", .8, reset(600_000)),
            reading("codex-0", .8, reset(600_000)), reading("codex-1", .2, reset(600_000))]
    snapshot = capacity.build_view(lanes, rows, now=NOW)
    snapshot["in_flight"] = {row["lane_id"]: 2 if row["lane_id"].endswith("-0") else 0 for row in lanes}
    assert scheduler.evaluate(configured, snapshot, {"pinned_model": "opus"}).chosen_lane == "claude-0"
    assert scheduler.evaluate(configured, snapshot, {"pinned_model": "astra"}).chosen_lane == "codex-1"
    configured["admission"]["lane_spread"] = {"claude": 2, "codex": None}
    assert scheduler.evaluate(configured, snapshot, {"pinned_model": "opus"}).chosen_lane == "claude-1"
    assert scheduler.evaluate(configured, snapshot, {"pinned_model": "astra"}).chosen_lane == "codex-0"


def test_simultaneous_fill_observations_compare_resets_without_float_precision_loss():
    configured = rules(expiring_s=None)
    rows = [reading("claude-0", .2, "9999-01-01T00:00:00.000002Z"),
            reading("claude-0", .2, "9999-01-01T00:00:00.000001Z"),
            reading("claude-0", .2, None)]
    for evidence in (rows, list(reversed(rows))):
        found = scheduler.fill_ranking_usage(evidence, now=CLOCK, admission=configured["admission"])
        assert found["fill_weekly_reset"] == "9999-01-01T00:00:00.000001Z"
