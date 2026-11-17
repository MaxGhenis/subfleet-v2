"""Offline fill replay: comparator fidelity, account metrics and audit windows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tools.replay_weekly_ranking import Replay, Usage, monotonicity


def replay(rows, *, provider="claude", attempts=None, aliases=False):
    result = Replay.__new__(Replay)
    result.since, result.until = 0, 7200
    result.lanes = {
        f"{provider}-a": {"provider": provider, "desktop": False, "enabled": True, "account_key": "account-a"},
        f"{provider}-b": {"provider": provider, "desktop": False, "enabled": True, "account_key": "account-a" if aliases else "account-b"},
    }
    result.readings = sorted(rows, key=lambda row: (row.observed, row.reading_id))
    result.arrivals = attempts or [{"attempt_id": "a1", "lane_id": f"{provider}-a", "provider": provider,
                                   "arrival": 1000, "end": 4600, "kind": "dispatch", "model_requested": "model"}]
    result.attempts = result.arrivals
    result.snapshots = {a["attempt_id"]: {"candidates": list(result.lanes)} for a in result.arrivals}
    result.costs = {a["attempt_id"]: {} for a in result.arrivals}
    result.factual_increments = {}
    return result


def reading(lane, used, *, reset=50_000, observed=-1000, scope="account", window="seven_day", label="provider", identity=1):
    return Usage(lane, scope, window, used, reset, observed, identity, label)


def chosen(result):
    return next(iter(result["placements_by_lane"]))


def test_replay_refuses_live_directory_without_resolving_it(tmp_path, monkeypatch):
    original = Path.resolve

    def guarded_resolve(path, *args, **kwargs):
        assert ".subfleet" not in path.parts
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    with pytest.raises(ValueError, match="temporary SQLite backup"):
        Replay(tmp_path / ".subfleet" / "state.sqlite3")


def test_fill_replay_uses_old_stale_provider_reading():
    case = replay([reading("claude-a", .8, label="stale-provider"),
                   reading("claude-b", .1, observed=990, identity=2)])
    assert chosen(case.run("fill", lane_order="fill", expiring_s=None)) == "claude-a"
    assert chosen(case.run("reset")) == "claude-b"


@pytest.mark.parametrize("reset,observed", [(999, -1000), (None, -1000), (50_000, 1001)])
def test_fill_replay_counts_only_known_open_observed_windows(reset, observed):
    case = replay([reading("claude-a", .9, reset=reset, observed=observed),
                   reading("claude-b", .2, identity=2)])
    assert chosen(case.run("fill", lane_order="fill", expiring_s=None)) == "claude-b"


def test_fill_replay_expiring_precedes_use_and_can_be_disabled():
    case = replay([reading("claude-a", .8), reading("claude-b", .1, reset=2000, identity=2)])
    assert chosen(case.run("fill", lane_order="fill")) == "claude-b"
    assert chosen(case.run("fill", lane_order="fill", expiring_s=None)) == "claude-a"


def test_fill_replay_earlier_expiring_reset_precedes_more_use():
    case = replay([reading("claude-a", .8, reset=3000),
                   reading("claude-b", .1, reset=2000, identity=2)])
    assert chosen(case.run("fill", lane_order="fill")) == "claude-b"


@pytest.mark.parametrize("window,used", [("seven_day", .99), ("five_hour", .91)])
def test_fill_replay_reserves_precede_expiration(window, used):
    rows = [reading("claude-a", .8), reading("claude-b", .1, reset=2000, identity=2),
            reading("claude-b", used, reset=2000, window=window, identity=3)]
    assert chosen(replay(rows).run("fill", lane_order="fill")) == "claude-a"


def test_fill_replay_uses_requested_model_scope_and_latest_reading():
    case = replay([reading("claude-a", .9, identity=1),
                   reading("claude-a", .1, observed=-900, identity=2),
                   reading("claude-a", .7, scope="model", identity=3),
                   reading("claude-b", .5, identity=4)])
    assert chosen(case.run("fill", lane_order="fill", expiring_s=None)) == "claude-a"


@pytest.mark.parametrize("first_id", [1, 2])
@pytest.mark.parametrize("same_use", [False, True])
def test_fill_replay_simultaneous_evidence_matches_conservative_tie(first_id, same_use):
    rows = [reading("claude-a", .8, reset=2000, identity=first_id),
            reading("claude-a", .8 if same_use else .1, reset=50_000, identity=3 - first_id),
            reading("claude-b", .9 if same_use else .5, reset=10_000, identity=3)]
    assert chosen(replay(rows).run("fill", lane_order="fill")) == "claude-a"


def test_reset_replay_ancient_expired_row_does_not_demote_fresh_weekly():
    case = replay([reading("claude-a", .4, observed=990, reset=2000),
                   reading("claude-a", .9, observed=-1000, reset=500, window="five_hour", identity=2),
                   reading("claude-b", .3, observed=990, reset=3000, identity=3)])
    assert chosen(case.run("reset")) == "claude-a"


def test_replay_codex_keeps_reset_and_its_load_band():
    attempts = [{"attempt_id": f"a{n}", "lane_id": "codex-a", "provider": "codex",
                 "arrival": 1000 + n, "end": 4600, "kind": "dispatch", "model_requested": "model"}
                for n in range(3)]
    case = replay([reading("codex-a", .9, observed=990, reset=40_000),
                   reading("codex-b", .1, observed=990, reset=50_000, identity=2)], provider="codex", attempts=attempts)
    baseline = case.run("reset")
    assert baseline["placements_by_lane"] == {"codex-a": 2, "codex-b": 1}
    for spread in (2, 4, None):
        variant = case.run("fill", lane_order="fill", spread=spread)
        assert variant["placements_by_lane"] == baseline["placements_by_lane"]
        assert variant["by_provider"] == baseline["by_provider"]


def test_replay_account_aliases_are_counted_once_and_hours_are_clipped():
    attempts = [{"attempt_id": f"a{n}", "lane_id": f"claude-{lane}", "provider": "claude",
                 "arrival": 1000 + 2000 * n, "end": 10_000, "kind": "dispatch", "model_requested": "model"}
                for n, lane in enumerate(("a", "b"))]
    case = replay([reading("claude-a", .5), reading("claude-b", .5, identity=2)], attempts=attempts, aliases=True)
    result = case.run("fill", lane_order="fill")
    counts = result["by_provider"]["claude"]
    assert counts["accounts_taking_work"] == 1
    assert counts["top_1_attempt_hour_share"] == counts["top_2_attempt_hour_share"] == counts["top_3_attempt_hour_share"] == 1
    assert counts["attempt_hours"] == round((6200 + 4200) / 3600, 6)
    assert counts["hourly_account_pool"] == 1
    assert counts["hourly_samples"] == 2
    assert counts["hourly_mean_partial_accounts"] == 1
    assert counts["hourly_mean_whole_accounts"] == 0


def test_replay_partial_range_is_inclusive_and_missing_is_whole():
    case = replay([reading("claude-a", .1)])
    counts = case.run("fill", lane_order="fill")["by_provider"]["claude"]
    assert counts["hourly_mean_partial_accounts"] == 1
    assert counts["hourly_mean_whole_accounts"] == 1


def test_monotonicity_audit_requires_identical_open_reset():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE lanes(lane_id TEXT, provider TEXT)")
    db.execute("INSERT INTO lanes VALUES('claude-a','claude')")
    db.execute("""CREATE TABLE readings(reading_id INTEGER, lane_id TEXT, scope TEXT, window TEXT,
               utilization REAL, resets_at TEXT, observed_at TEXT, label TEXT)""")
    rows = [(.1, "1970-01-01T01:00:00Z", "1970-01-01T00:00:00Z"),
            (.2, "1970-01-01T01:00:00Z", "1970-01-01T00:01:00Z"),
            (.1, "1970-01-01T01:00:00Z", "1970-01-01T00:02:00Z"),
            (.0, "1970-01-01T02:00:00Z", "1970-01-01T00:03:00Z"),
            (.1, "1970-01-01T02:00:00Z", "1970-01-01T02:01:00Z")]
    db.executemany("INSERT INTO readings VALUES(?,'claude-a','account','seven_day',?,?,?,'provider')",
                   [(n, *row) for n, row in enumerate(rows)])
    result = monotonicity(db)
    assert result["consecutive_open_window_pairs"] == {"all": 2, "claude": 2, "claude:seven_day": 2}
    assert result["utilization_decreases"] == {"all": 1, "claude": 1, "claude:seven_day": 1}
    assert monotonicity(db, until=60)["readings"] == 2


def test_replay_report_has_all_six_deduplicated_experiments(tmp_path):
    case = replay([reading("claude-a", .5)])
    for attempt in case.arrivals:
        attempt.update(outcome_class="ok", seq=1)
    case.jobs = {}
    case.cost_details = {}
    case.monotonicity = case.monotonicity_through_until = {}
    source = tmp_path / "offline-copy.sqlite3"
    source.write_bytes(b"offline export")
    report = case.report(source)
    experiments = [(row["lane_order"]["claude"], row["lane_spread"]["claude"], row["expiring_s"])
                   for row in report["variants"]]
    assert experiments == [("reset", 2, 21600), ("fill", 2, 21600), ("fill", 4, 21600),
                           ("fill", None, 21600), ("fill", 2, None), ("fill", 2, 86400)]
