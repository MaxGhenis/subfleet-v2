"""C-11.11: generated fill laws and complete decisions against release/217.

The frozen scheduler is the exact file at 266c3ab8b0be. Its module and the two
ranking functions have pinned SHA-256s, checked without consulting git. Keeping
the entire evaluator catches accidental changes to rejection detail or reasons
that a comparator-only reference would share with the implementation under test.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import importlib.util
import math
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import scheduler
from subfleet.policy import DEFAULT_POLICY_PATH, admission_settings, load_policy
from tests.routing_strategies import NOW as ROUTE_NOW, policies, route_jobs, stores, view_of
from tests.unit.test_scheduler_weekly import (
    CLOCK, PROVIDERS, admission_fleets, fleet, heterogeneous_fleets, reset,
)


LAWS = settings(max_examples=200, deadline=None, derandomize=True)
PACKING = settings(max_examples=100, deadline=None, derandomize=True)
FILL_FIELDS = {"lane_order", "fill_weekly_used", "fill_weekly_reset",
               "fill_five_hour_used", "fill_expiring"}
FROZEN = Path(__file__).resolve().parents[1] / "fixtures/fill_first/scheduler_266c3ab8b.py"
BASE_SHA256 = "f6c9ad0cae11fd8b9d624f856b0782ddb7e155cbc4dfca1ab85ce141d1da7d54"
FUNCTION_SHA256 = {
    "rank_key": "1e100b7a9681f7bf7dc19ebf5d8e701785ce3fb8d5d0d57d5041e45f88bc6a54",
    "ranking_usage": "ade25eea6e4bdbed6f569d7812bafbd64c1c3a093bbf87341d01d338083a7e21",
}


def frozen_scheduler():
    data = FROZEN.read_bytes()
    assert hashlib.sha256(data).hexdigest() == BASE_SHA256
    lines = data.decode().splitlines(keepends=True)
    found = {}
    for node in ast.parse(data).body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTION_SHA256:
            source = "".join(lines[node.lineno - 1:node.end_lineno]).encode()
            found[node.name] = hashlib.sha256(source).hexdigest()
    assert found == FUNCTION_SHA256
    name = "subfleet._fill_first_frozen_scheduler"
    spec = importlib.util.spec_from_file_location(name, FROZEN)
    module = importlib.util.module_from_spec(spec)
    # dataclass resolves annotations through the module table while loading.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BASE = frozen_scheduler()


def fill_case(case, *, expiring=21600):
    policy, view, job = copy.deepcopy(case)
    policy["admission"].update(lane_order={"claude": "fill", "codex": "fill"}, expiring_s=expiring)
    # These laws synthesize and mutate reading rows directly. Pipeline laws in
    # test_fill_evidence cover build_view's separately preserved raw evidence.
    view.pop("fill_readings", None)
    return policy, view, job


def evaluation(case):
    return scheduler.evaluate(*case).evaluations[0]


def candidates(case):
    return evaluation(case)["candidates"]


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(st.data())
def test_fill_and_reset_have_identical_candidates_and_refusals(provider, model, data):
    """Invariant 1: floors, freshness, model reserves, slots and refusals agree."""
    case = data.draw(admission_fleets(provider))
    old, new = evaluation(case), evaluation(fill_case(case))
    assert set(old["candidates"]) == set(new["candidates"])
    assert old["capacity_blocks"] == new["capacity_blocks"]
    assert old["rejections"] == [
        {key: value for key, value in row.items() if key not in FILL_FIELDS}
        for row in new["rejections"]]
    assert old["candidate_details"] == {
        identity: {key: value for key, value in detail.items() if key not in FILL_FIELDS}
        for identity, detail in new["candidate_details"].items()}


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(st.data())
def test_fill_keeps_the_same_probe_predicate_for_each_pinned_lane(provider, model, data):
    """Invariant 1's C-11.4 freshness guard still asks the same probe per lane."""
    case = data.draw(admission_fleets(provider))
    case[2]["pinned_lane"] = data.draw(st.sampled_from(case[1]["lanes"]))["lane_id"]
    case[2]["sandbox"] = data.draw(st.sampled_from(["read-only", "workspace-write"]))
    before, after = scheduler.evaluate(*case), scheduler.evaluate(*fill_case(case))
    assert scheduler.probe_required(before, case[2]) == scheduler.probe_required(after, case[2])


def test_frozen_release217_ranking_sources_have_verified_sha256():
    """The reference bytes and function hashes came from git show 266c3ab8b0be."""
    assert frozen_scheduler().rank_key.__module__ == "subfleet._fill_first_frozen_scheduler"


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(st.data())
def test_reset_decisions_equal_frozen_release217(provider, model, data):
    """Invariant 2: compare the whole Decision, including evidence and reasons."""
    case = data.draw(admission_fleets(provider))
    assert scheduler.evaluate(*case) == BASE.evaluate(*case)


@pytest.mark.parametrize("configuration", ["shipped", "omitted"])
@LAWS
@given(st.data())
def test_default_codex_decisions_equal_frozen_release217(configuration, data):
    """Invariant 2: shipped and omitted policy settings both leave Codex at reset."""
    case = data.draw(admission_fleets("codex"))
    if configuration == "shipped":
        case[0]["admission"]["lane_order"] = load_policy(DEFAULT_POLICY_PATH)["admission"]["lane_order"]
    else:
        case[0]["admission"].pop("lane_order", None)
    case[0]["admission"].pop("expiring_s", None)
    assert scheduler.evaluate(*case) == BASE.evaluate(*case)


@st.composite
def route_cases(draw):
    store = draw(stores())
    policy = draw(policies())
    policy["admission"]["lane_order"] = {"claude": "reset", "codex": "reset"}
    return policy, view_of(store, ROUTE_NOW), draw(route_jobs(store))


def outcome(function, case):
    try:
        return "decision", dataclasses.asdict(function(*case))
    except Exception as exc:  # the complete routing error is also an outcome
        return "error", (type(exc).__name__, str(exc))


@LAWS
@given(route_cases())
def test_reset_complete_chain_decisions_and_errors_equal_frozen_release217(case):
    """Invariant 2 also covers chains, pins, ancestors, mixed providers and turns."""
    assert outcome(scheduler.evaluate, case) == outcome(BASE.evaluate, case)


@LAWS
@given(route_cases())
def test_fill_complete_chain_has_identical_candidates_and_rejection_predicates(case):
    """Invariant 1 holds at every walked model, including promotion and pins."""
    before, after = outcome(scheduler.evaluate, case), outcome(scheduler.evaluate, fill_case(case))
    assert before[0] == after[0]
    if before[0] == "error":
        assert before == after
        return
    old, new = before[1], after[1]
    assert old["chain"] == new["chain"]
    for left, right in zip(old["evaluations"], new["evaluations"]):
        assert set(left["candidates"]) == set(right["candidates"])
        assert left["rejections"] == [
            {key: value for key, value in row.items() if key not in FILL_FIELDS}
            for row in right["rejections"]]


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(st.data())
def test_fill_desktop_last_and_turn_affinity_leads_its_class(provider, model, data):
    """Invariant 3: every non-desktop candidate leads every desktop candidate."""
    case, _ = data.draw(heterogeneous_fleets(provider))
    case = fill_case(case)
    result = evaluation(case)
    ordered, details = result["candidates"], result["candidate_details"]
    desktop = [bool(details[identity].get("desktop")) for identity in ordered]
    assert desktop == sorted(desktop)
    affinity = case[2].get("affinity_lane") if case[2].get("kind") == "turn" else None
    if affinity in ordered:
        same_class = [identity for identity in ordered
                      if bool(details[identity].get("desktop")) == bool(details[affinity].get("desktop"))]
        assert same_class[0] == affinity


@st.composite
def comparable_fill_pairs(draw):
    """Two lanes tie through the prefix, reserve classes and expiring tier."""
    reserve = draw(st.booleans())
    bounds = (981, 999) if reserve else (0, 979)
    first, second = draw(st.tuples(st.integers(*bounds), st.integers(*bounds)))
    five = draw(st.integers(0, 999))
    horizon = draw(st.sampled_from([None, 21600, 86400]))
    expiring = horizon is not None and draw(st.booleans())
    when = draw(st.integers(1, horizon)) if expiring else draw(st.integers(86401, 600000))
    spread = draw(st.sampled_from([None, 2, 4]))
    band = draw(st.integers(0, 5))
    loads = [draw(st.integers(0, 20) if spread is None else
                  st.integers(band * spread, (band + 1) * spread - 1)) for _ in range(2)]
    specs = [(1 - used / 1000, 1 - five / 1000, reset(when), load)
             for used, load in zip((first, second), loads)]
    case = fill_case(fleet("claude", specs, spread=spread), expiring=horizon)
    for row in case[1]["readings"]:
        row["observed_at"] = reset(-draw(st.sampled_from([0, 121, 86400, 500000])))
    return case, first, second


@LAWS
@given(comparable_fill_pairs())
def test_fill_more_weekly_use_never_ranks_after_less_in_equal_earlier_tiers(generated):
    """Invariant 4: descending use precedes both in-flight and lane id."""
    case, first, second = generated
    if first == second:
        return
    high, low = ("claude-0", "claude-1") if first > second else ("claude-1", "claude-0")
    ordered = candidates(case)
    assert ordered.index(high) < ordered.index(low)


@pytest.mark.parametrize("provider,model", PROVIDERS)
@pytest.mark.parametrize("weekly,five", [(.02, .10), (.25, .125), (.03, .4)])
@pytest.mark.parametrize("window", ["seven_day", "five_hour"])
def test_fill_reserve_complements_are_clear_at_equality_and_low_one_float_above(provider, model, weekly, five, window):
    """Both reserve boundaries compare reported use directly with the complement."""
    case = fill_case(fleet(provider, [(.4, .5, reset(200000), 0), (.5, .5, reset(200000), 0)]), expiring=None)
    case[0]["admission"].update(weekly_reserve=weekly, five_hour_reserve=five)
    target = next(row for row in case[1]["readings"]
                  if row["lane_id"] == f"{provider}-0" and row["window"] == window)
    target["utilization"] = 1 - (weekly if window == "seven_day" else five)
    assert candidates(case) == [f"{provider}-0", f"{provider}-1"]
    target["utilization"] = math.nextafter(target["utilization"], math.inf)
    assert candidates(case) == [f"{provider}-1", f"{provider}-0"]


@LAWS
@given(spread=st.sampled_from([2, 4]), band=st.integers(0, 10), when=st.integers(1, 21600))
def test_fill_retains_band_then_claude_stranding_before_usage(spread, band, when):
    """C-11.3's shared prefix still precedes every fill preference."""
    case = fill_case(fleet("claude", [(.9, .5, reset(100000), band * spread),
                                     (.2, .5, reset(when), (band + 1) * spread)], spread=spread))
    case[1]["closures"].append({"lane_id": "claude-1", "scope": "claude-fable-5-1",
                                 "until_at": reset(when), "reason": "provider-limit"})
    assert candidates(case) == ["claude-0", "claude-1"]
    case[1]["in_flight"]["claude-0"] = (band + 1) * spread
    # In the same band the stranded lane wins despite its weekly reserve.
    for row in case[1]["readings"]:
        if row["lane_id"] == "claude-1" and row["window"] == "seven_day":
            row["utilization"] = .99
    assert candidates(case) == ["claude-1", "claude-0"]


@st.composite
def reading_histories(draw):
    specs = draw(st.lists(st.tuples(
        st.sampled_from(["account", "claude-opus-5-0", "unrelated-model"]),
        st.sampled_from(["seven_day", "five_hour"]),
        st.sampled_from(["provider", "stale-provider", "estimate"]),
        st.one_of(st.none(), st.integers(0, 999)),
        st.one_of(st.none(), st.integers(-100000, 600000)),
        st.integers(-1000, 600000)), max_size=24))
    rows = []
    for index, (scope, window, label, used, when, age) in enumerate(specs):
        # Distinct clocks avoid giving unspecified identical-observation
        # evidence a special meaning in this independent latest-row oracle.
        row = {"lane_id": "claude-0", "scope": scope, "window": window,
               "utilization": used / 1000 if used is not None else None,
               "resets_at": reset(when) if when is not None else None,
               "observed_at": reset(-(age * 25 + index)), "label": label, "source": "usage"}
        rows.append(row)
    return rows


def expected_open_usage(rows, scope, window):
    """Select latest valid evidence before checking whether its window is open."""
    applicable = [row for row in rows if row["scope"] in ("account", scope)
                  and row["window"] == window and row["label"] in ("provider", "stale-provider")
                  and row["utilization"] is not None and row["observed_at"] <= reset(0)]
    latest = {}
    for row in applicable:
        if row["scope"] not in latest or row["observed_at"] > latest[row["scope"]]["observed_at"]:
            latest[row["scope"]] = row
    kept = [row for row in latest.values() if row["resets_at"] and row["resets_at"] > reset(0)]
    return min(kept, key=lambda row: (-row["utilization"], row["resets_at"], row["scope"]), default=None)


@LAWS
@given(reading_histories())
def test_fill_only_latest_applicable_open_windows_contribute_at_any_age(rows):
    """Invariant 5: expired/missing resets, future readings and other scopes never count."""
    case = fill_case(fleet("claude", [(.5, .5, reset(100000), 0)]))
    scope = case[0]["models"]["opus"]["id"]
    # The real model id is policy data, rather than a fixture assumption.
    rows = [{**row, "scope": scope if row["scope"] == "claude-opus-5-0" else row["scope"]}
            for row in rows]
    case[1]["readings"] = rows
    result = evaluation(case)
    detail = (result["candidate_details"].get("claude-0") or result["rejections"][0])
    weekly = expected_open_usage(rows, scope, "seven_day")
    five = expected_open_usage(rows, scope, "five_hour")
    assert detail["fill_weekly_used"] == (weekly["utilization"] if weekly else 0)
    assert detail["fill_weekly_reset"] == (weekly["resets_at"] if weekly else None)
    assert detail["fill_five_hour_used"] == (five["utilization"] if five else 0)


@pytest.mark.parametrize("age", [121, 86400, 500000])
def test_fill_keeps_old_open_provider_reading_and_ignores_expired_one(age):
    """Explicit witnesses for the TTL and expired-window mutation checks."""
    case = fill_case(fleet("claude", [(.2, .5, reset(200000), 0),
                                     (.7, .5, reset(200000), 0)]), expiring=None)
    for row in case[1]["readings"]:
        row["observed_at"] = reset(-age)
    assert candidates(case) == ["claude-0", "claude-1"]
    for row in case[1]["readings"]:
        if row["lane_id"] == "claude-0" and row["window"] == "seven_day":
            row["resets_at"] = reset(-1)
    result = evaluation(case)
    assert result["candidate_details"]["claude-0"]["fill_weekly_used"] == 0
    assert result["candidates"] == ["claude-1", "claude-0"]


@LAWS
@given(horizon=st.integers(2, 86400), used=st.integers(300, 800), age=st.integers(0, 600000))
def test_fill_expiring_precedes_nonexpiring_and_earlier_expiring_reset_wins(horizon, used, age):
    """Invariant 6: expiry wins against greater use, including the exact horizon."""
    case = fill_case(fleet("claude", [(.9, .5, reset(horizon - 1), 0),
                                     (.8, .5, reset(horizon), 0),
                                     (1 - used / 1000, .5, reset(horizon + 1), 0)]), expiring=horizon)
    for row in case[1]["readings"]:
        row["observed_at"] = reset(-age)
    result = evaluation(case)
    assert result["candidates"] == ["claude-0", "claude-1", "claude-2"]
    assert [result["candidate_details"][identity]["fill_expiring"]
            for identity in result["candidates"]] == [True, True, False]


@LAWS
@given(near=st.integers(1, 21600), far=st.integers(21601, 600000))
def test_fill_null_expiring_disables_the_tier(near, far):
    """Invariant 6: null makes every binding window non-expiring."""
    case = fill_case(fleet("claude", [(.8, .5, reset(near), 0), (.2, .5, reset(far), 0)]), expiring=None)
    result = evaluation(case)
    assert result["candidates"] == ["claude-1", "claude-0"]
    assert not any(detail["fill_expiring"] for detail in result["candidate_details"].values())


def test_fill_expiring_reset_order_keeps_fractional_seconds():
    """An earlier expiry wins even when both resets occur in one second."""
    case = fill_case(fleet("claude", [(.8, .5, reset(100), 0), (.2, .5, reset(100), 0)]))
    for row in case[1]["readings"]:
        if row["window"] == "seven_day":
            fraction = "100000" if row["lane_id"] == "claude-0" else "900000"
            row["resets_at"] = reset(100).replace("Z", f".{fraction}Z")
    result = evaluation(case)
    assert result["candidates"] == ["claude-0", "claude-1"]
    assert result["candidate_details"]["claude-0"]["fill_weekly_reset"].endswith(".100000Z")


@pytest.mark.parametrize("provider,model", PROVIDERS)
@LAWS
@given(st.data())
def test_fill_keys_are_unique_total_and_order_independent(provider, model, data):
    """Invariant 7: ids break all ties, and lane/reading permutations do not rank."""
    case, permutation = data.draw(heterogeneous_fleets(provider))
    case = fill_case(case)
    result = evaluation(case)
    setup = scheduler.prepare(*case)
    keys = [scheduler.rank_key(setup, model, identity, detail)
            for identity, detail in result["candidate_details"].items()]
    assert len(keys) == len(set(keys))
    for left in keys:
        for right in keys:
            assert (left < right) + (left == right) + (left > right) == 1
    permuted = {**case[1], "lanes": list(permutation), "readings": list(reversed(case[1]["readings"]))}
    again = evaluation((case[0], permuted, case[2]))
    assert result["candidates"] == again["candidates"]
    assert {identity: {key: detail[key] for key in FILL_FIELDS}
            for identity, detail in result["candidate_details"].items()} == {
        identity: {key: detail[key] for key in FILL_FIELDS}
        for identity, detail in again["candidate_details"].items()}


@LAWS
@given(used=st.integers(1, 900), when=st.integers(1, 600000))
def test_fill_equal_observation_ties_do_not_depend_on_reading_order(used, when):
    """Invariant 7 also covers conflicting rows with the same observation time."""
    rules = admission_settings(fill_case(fleet("claude", [(.5, .5, reset(when), 0)]))[0])
    rows = [{"scope": "account", "window": "seven_day", "label": "provider",
             "utilization": number / 1000, "observed_at": reset(-86400), "resets_at": reset(end)}
            for number, end in [(used, when), (used + 1, when + 1), (used + 1, when)]]
    expected = scheduler.fill_ranking_usage(rows, now=CLOCK, admission=rules)
    assert expected == scheduler.fill_ranking_usage(reversed(rows), now=CLOCK, admission=rules)


@PACKING
@given(start=st.lists(st.integers(0, 900), min_size=2, max_size=7),
       step=st.integers(1, 300), jobs=st.integers(1, 50))
def test_fill_packs_a_prefix_of_starting_use_when_expiry_and_bands_are_off(start, step, jobs):
    """Invariant 8's exact form: equal prefix, five-hour clear, expiry/band off.

    One completed job adds a fixed weekly step. At every simulation prefix the
    set ever chosen is an initial segment of (-starting use, lane id). A first
    use of a fresher lane occurs only after every more-used lane is refused or
    above its weekly reserve. No windows reset during this finite simulation.
    """
    case = fill_case(fleet("claude", [(1 - used / 1000, .5, reset(600000), 0)
                                     for used in start], spread=None), expiring=None)
    starting_order = sorted(range(len(start)), key=lambda index: (-start[index], f"claude-{index}"))
    current = list(start)
    seen = set()
    for _ in range(jobs):
        for row in case[1]["readings"]:
            if row["window"] == "seven_day":
                row["utilization"] = current[int(row["lane_id"].partition("-")[2])] / 1000
        result = evaluation(case)
        if not result["candidates"]:
            break
        chosen = int(result["candidates"][0].partition("-")[2])
        if chosen not in seen:
            for earlier in starting_order[:starting_order.index(chosen)]:
                identity = f"claude-{earlier}"
                assert identity not in result["candidates"] or current[earlier] / 1000 > .98
        seen.add(chosen)
        assert seen == set(starting_order[:len(seen)])
        current[chosen] = min(1000, current[chosen] + step)
