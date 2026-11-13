"""C-11.11: preserve fill evidence through store, view and reservation reuse."""

from __future__ import annotations

import copy
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import capacity, route_check, scheduler
from subfleet.contracts import Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.policy import DEFAULT_POLICY_PATH, admission_settings, load_policy
from subfleet.store import Store


CLOCK = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
JOB = {"pinned_model": "opus", "sandbox": "read-only"}


def stamp(seconds=0):
    return (CLOCK + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def policy():
    rules = load_policy(DEFAULT_POLICY_PATH)
    rules["reserve"]["models"] = []
    rules["headroom_floor"] = 0
    return rules


def lane(identity="claude-1"):
    return Lane(identity, "claude", identity, Credential("claude", identity, "keychain-token"),
                None, LaneOwner.V2, False)


def reading(used=.8, observed=-3600, reset=36000, label="provider", *, identity="claude-1", scope="account"):
    return Reading(identity, scope, "seven_day", used, stamp(reset) if reset is not None else None,
                   ReadingLabel(label), "fixture", stamp(observed))


def put(store, rows):
    store.put_lane(lane())
    for row in rows:
        store.add_reading(row)


def detail(decision, identity="claude-1"):
    evaluation = decision.evaluations[0]
    return evaluation["candidate_details"].get(identity) or next(
        row for row in evaluation["rejections"] if row["lane_id"] == identity)


@pytest.mark.parametrize("mask", [reading(.1, 1), reading(.1, -1, label="unknown"),
                                 reading(None, -1), reading(.1, -1, label="local-backoff")])
def test_store_view_preserves_latest_observed_numeric_provider_reading_behind_a_mask(tmp_path, mask):
    with Store(tmp_path / "state.sqlite3") as store:
        put(store, [reading(), mask])
        view = capacity.from_store(store, now=CLOCK)
        normal = store.latest_reading_candidates()
        assert len(normal) == 1 and normal[0]["observed_at"] == mask.observed_at
        assert detail(scheduler.evaluate(policy(), view, JOB))["fill_weekly_used"] == .8
        assert detail(scheduler.evaluate(policy(), view, JOB))["fill_weekly_reset"] == stamp(36000)
        # The extra ranking evidence cannot alter any reset decision field.
        reset_rules = policy()
        reset_rules["admission"]["lane_order"] = {"claude": "reset", "codex": "reset"}
        base_view = capacity.build_view(store.lane_rows(), normal, now=CLOCK)
        assert scheduler.evaluate(reset_rules, view, JOB) == scheduler.evaluate(reset_rules, base_view, JOB)


def test_equal_observation_ties_are_conservative_and_independent_of_reading_id(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        put(store, [reading(.8, -1, 40000), reading(.8, -1, 36000), reading(.2, -1, 30000)])
        assert capacity.from_store(store, now=CLOCK)["readings"][0]["utilization"] == .2
        expected = {"lane_order": "fill", "fill_weekly_used": .8, "fill_weekly_reset": stamp(36000),
                    "fill_five_hour_used": 0, "fill_expiring": False}
        for rows in (store.list_readings(), list(reversed(store.list_readings()))):
            view = capacity.build_view(store.lane_rows(), rows, now=CLOCK)
            found = detail(scheduler.evaluate(policy(), view, JOB))
            assert {key: found[key] for key in expected} == expected
        found = detail(scheduler.evaluate(policy(), capacity.from_store(store, now=CLOCK), JOB))
        assert {key: found[key] for key in expected} == expected


def test_latest_numeric_observation_with_no_open_window_does_not_revive_an_older_one(tmp_path):
    for reset in (None, 0, -1):
        with Store(tmp_path / f"state-{reset}.sqlite3") as store:
            put(store, [reading(), reading(.9, -1, reset)])
            found = detail(scheduler.evaluate(policy(), capacity.from_store(store, now=CLOCK), JOB))
            assert found["fill_weekly_used"] == 0
            assert found["fill_weekly_reset"] is None


@settings(max_examples=100, deadline=None, derandomize=True, database=None)
@given(st.lists(st.tuples(st.sampled_from([0, .2, .8, None]), st.integers(-5000, 50),
                         st.one_of(st.none(), st.integers(-1, 50000)),
                         st.sampled_from(["provider", "stale-provider", "unknown", "local-backoff"]),
                         st.sampled_from(["account", "claude-opus-5-5"])), min_size=1, max_size=12))
def test_bounded_store_candidates_match_all_raw_provider_history(specs):
    with tempfile.TemporaryDirectory(prefix="fill-evidence-") as directory:
        with Store(Path(directory) / "state.sqlite3") as store:
            put(store, [reading(used, observed, reset, label, scope=scope)
                        for used, observed, reset, label, scope in specs])
            all_rows, candidates = store.list_readings(), store.fill_reading_candidates(now=CLOCK)
            admission = admission_settings(policy())
            assert scheduler.fill_ranking_usage(candidates, now=CLOCK, admission=admission) == \
                scheduler.fill_ranking_usage(all_rows, now=CLOCK, admission=admission)


def test_store_fill_candidates_keep_noncanonical_times_and_all_future_rows(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        put(store, [reading(.1, -7200), reading(.5, -3600), reading(.2, 10), reading(.3, 20)])
        offset = reading(.8, -1)
        store.add_reading(Reading(**{**offset.__dict__, "observed_at": offset.observed_at.replace("Z", "+00:00")}))
        candidates = store.fill_reading_candidates(now=CLOCK)
        assert len(candidates) == 4              # old canonical .1 cannot bind again
        assert {row["utilization"] for row in candidates} == {.5, .8, .2, .3}
        for later in (CLOCK, CLOCK + timedelta(seconds=10), CLOCK + timedelta(seconds=20)):
            assert scheduler.fill_ranking_usage(candidates, now=later, admission=admission_settings(policy())) == \
                scheduler.fill_ranking_usage(store.list_readings(), now=later, admission=admission_settings(policy()))


@pytest.mark.parametrize("seconds,expected_judged", [(1, 0), (14400, 1), (36000, 1)])
def test_cached_fill_verdict_matches_full_evaluation_before_expiring_and_reset_clocks(tmp_path, seconds, expected_judged):
    with Store(tmp_path / "state.sqlite3") as store:
        put(store, [reading()])
        rules = policy()
        view = capacity.from_store(store, now=CLOCK)
        decision = scheduler.evaluate(rules, view, JOB)
        later = CLOCK + timedelta(seconds=seconds)
        verdict, judged, standing = route_check.still_stands(
            rules, JOB, decision, view=view, candidates=store.latest_reading_candidates(),
            fill_candidates=store.fill_reading_candidates(now=CLOCK), overridden=(),
            clocks=capacity.lane_horizons(view, policy=rules), now=later,
            lanes=store.lane_rows(), readings=(), closures=(), attempts=(), jobs=(),
            unavailable={}, reserved_probes=0)
        assert verdict is None and judged == expected_judged
        assert standing == scheduler.evaluate(rules, capacity.from_store(store, now=later), JOB)


def test_auxiliary_provider_window_restored_when_reset_override_ends(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        put(store, [reading()])
        rules = policy()
        view = capacity.from_store(store, now=CLOCK)
        normal, auxiliary = view["readings"], view["fill_readings"]
        view["readings"] = view["fill_readings"] = []
        decision = scheduler.evaluate(rules, view, JOB)
        verdict, judged, standing = route_check.still_stands(
            rules, JOB, decision, view=view, candidates=normal, fill_candidates=auxiliary,
            overridden=("claude-1",), holding=(), clocks={}, now=CLOCK,
            lanes=store.lane_rows(), readings=(), closures=(), attempts=(), jobs=(),
            unavailable={}, reserved_probes=0)
        assert verdict is None and judged == 1
        assert standing == scheduler.evaluate(rules, capacity.from_store(store, now=CLOCK), JOB)


def test_lane_order_defaults_do_not_share_mutable_maps(tmp_path):
    first = admission_settings({})
    first["lane_order"]["claude"] = "reset"
    assert admission_settings({})["lane_order"]["claude"] == "fill"
    data = copy.deepcopy(load_policy(DEFAULT_POLICY_PATH))
    data.pop("admission")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_policy(path)
    loaded["admission"]["lane_order"]["claude"] = "reset"
    assert admission_settings({})["lane_order"]["claude"] == "fill"
