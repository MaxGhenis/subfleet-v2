"""C-6.4, C-6.9, C-6.11, C-6.13, C-10.3, C-11.3: admission with no count caps,
placed by priority (Max, 2026-09-27: "we should uncap everything and instead use
prioritization").

Each property is stated for every input the strategies draw, over the pure pass
model in `tests/admission_model.py` and `scheduler.evaluate` itself:

1. nothing waits for a count that is not there;
2. jobs are considered in priority order (class, tier, oldest first);
3. decisions are deterministic, whatever order the rows arrive in;
4. raising or removing a cap never places fewer jobs, nor removes a candidate;
5. the desktop lane: out while Claude Code uses it, otherwise last;
6. load bands spread work, and with a cap equal to the band nothing changes;
7. the machine guard never holds a turn and is monotone in the load.
"""

from __future__ import annotations

import copy
import random

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from subfleet import scheduler
from subfleet.policy import DEFAULT_POLICY_PATH, MACHINE_GUARD_PROPOSAL, load_policy
from tests.admission_model import run_pass
from tests.caps import capped
from tests.routing_strategies import BASE_POLICY, NOW, event, policies, route_jobs, stores, view_of

SETTINGS = settings(max_examples=400, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                                           HealthCheck.filter_too_much])
CAP_HOLDS = {"fleet-full", "slot-kept", "parent-cap", "behind-older-job"}
COUNT_CAPS = ("max_active_attempts", "max_in_flight_per_lane", "max_in_flight_unmeasured",
              "max_active_attempts_per_parent")
LIVE = scheduler.Liveness(sessions=frozenset({"live-session"}),
                          jobs=frozenset({"job-0", "job-1"}))


def uncapped(policy: dict) -> dict:
    """Every count cap null (the shipped default), detached and turn alike."""
    policy = copy.deepcopy(policy)
    policy["caps"].update(dict.fromkeys(COUNT_CAPS))
    policy.setdefault("conversations", {}).update(max_active_turns=None, turn_slots_per_lane=None)
    return policy


def quiet(policy: dict) -> dict:
    policy = copy.deepcopy(policy)
    policy.setdefault("admission", {})["machine_guard"] = None
    return policy


def guarded() -> dict:
    """The shipped policy with the machine guard on at its proposed thresholds
    (C-6.13; off by default since 2026-09-28)."""
    policy = load_policy(DEFAULT_POLICY_PATH)
    policy["admission"]["machine_guard"] = copy.deepcopy(MACHINE_GUARD_PROPOSAL)
    return policy


@st.composite
def pass_jobs(draw, store: dict) -> list[dict]:
    """Several jobs one pass looks at, with callers some of which are live."""
    count = draw(st.integers(1, 7))
    minutes = draw(st.lists(st.integers(0, 59), min_size=count, max_size=count, unique=True))
    jobs = []
    for index, minute in enumerate(minutes):
        job = draw(route_jobs(store))
        job.update(job_id=f"pass-{index}", created_at=f"2026-09-26T11:{minute:02d}:00Z",
                   caller_session=draw(st.sampled_from([None, "live-session", "gone-session"])),
                   caller_pid=draw(st.sampled_from([None, 101, 202])),
                   parent_job_id=draw(st.sampled_from([None, None, "job-0", "job-2", *(row["job_id"] for row in jobs)])))
        if draw(st.integers(0, 5)) == 0:
            job["kind"] = "gate-review"
        jobs.append(job)
    return jobs


@st.composite
def passes(draw):
    store = draw(stores())
    return draw(policies()), store, draw(pass_jobs(store))


# --- 1. nothing waits for a count that is not there ------------------------------------------

@SETTINGS
@given(passes())
def test_uncapped_every_admissible_job_is_placed_and_none_waits_for_a_count(case):
    policy, store, jobs = case
    policy = quiet(uncapped(policy))
    result = run_pass(policy, view_of(store, NOW), jobs, live=LIVE)
    event(f"{len(result.placed)} of {len(jobs)} placed")
    for outcome in result.outcomes:
        assert outcome.hold not in CAP_HOLDS, outcome
        if outcome.placed:
            continue
        # Held: an evaluation made at that point in the pass found no lane.
        assert outcome.hold == "route" or outcome.decision.chosen_lane is None
        for evaluation in (outcome.decision.evaluations if outcome.decision else ()):
            assert evaluation["capacity_blocks"] == []
            for row in evaluation["rejections"]:
                if "no-slot" in row["reasons"]:
                    assert row.get("slot_block"), row          # only a probe or a latched credential


@SETTINGS
@given(passes())
def test_uncapped_evaluate_never_says_no_slot_for_want_of_a_count(case):
    policy, store, jobs = case
    policy = uncapped(policy)
    view = view_of(store, NOW)
    for job in jobs:
        try:
            decision = scheduler.evaluate(policy, view, job)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            continue
        for evaluation in decision.evaluations:
            assert evaluation["capacity_blocks"] == []
            assert all(row.get("slot_block") for row in evaluation["rejections"] if "no-slot" in row["reasons"])


# --- 2. priority order -----------------------------------------------------------------------

@SETTINGS
@given(passes())
def test_jobs_are_considered_by_class_then_tier_then_age(case):
    policy, _, jobs = case
    ordered = scheduler.ordered_jobs(policy, jobs, LIVE)
    assert sorted(row["job_id"] for row in ordered) == sorted(row["job_id"] for row in jobs)
    tiers = {tier: index for index, tier in enumerate(policy["tiers"])}
    classes = {name: index for index, name in enumerate(scheduler.PRIORITY_CLASSES)}

    def key(job):
        return (classes[scheduler.priority_class(job, LIVE)], tiers[job.get("tier") or "standard"],
                job["created_at"])
    keys = [key(job) for job in ordered]
    assert keys == sorted(keys)
    result = run_pass(quiet(policy), view_of(case[1], NOW), jobs, live=LIVE)
    assert [row.job_id for row in result.outcomes] == [row["job_id"] for row in ordered]


@pytest.mark.parametrize("job,expected", [
    ({"kind": "turn"}, "attended"),
    ({"kind": "turn", "caller_session": "gone"}, "attended"),
    ({"kind": "gate-review"}, "session"),
    ({"kind": "dispatch", "caller_session": "LIVE-SESSION"}, "session"),          # a UUID in either case
    ({"kind": "dispatch", "caller_pid": 101}, "background"),                    # a pid alone proves nothing
    ({"kind": "dispatch", "parent_job_id": "job-1"}, "session"),
    ({"kind": "dispatch", "caller_session": "gone", "caller_pid": 202}, "background"),
    ({"kind": "dispatch"}, "background"),
    ({"kind": "revive", "caller_session": "gone"}, "background"),
    ({"kind": "resume", "caller_session": "live-session"}, "session"),
])
def test_priority_classes(job, expected):
    """C-6.9: a turn is attended; a gate round and a job whose caller or parent is
    live is waited on; anything else is background. Without liveness every
    detached job is `session`, which orders exactly as before."""
    live = scheduler.Liveness(sessions=frozenset({"live-session"}),
                              jobs=frozenset({"job-1"}))
    assert scheduler.priority_class(job, live) == expected
    assert scheduler.priority_class(job) == ("attended" if job["kind"] == "turn" else "session")


def test_a_live_callers_job_goes_before_older_background_work_of_its_tier():
    policy = load_policy(DEFAULT_POLICY_PATH)
    jobs = [{"job_id": "old-background", "kind": "dispatch", "tier": "standard", "created_at": "2026-09-27T10:00:00Z"},
            {"job_id": "hard-live", "kind": "dispatch", "tier": "hard", "caller_session": "s-live",
             "created_at": "2026-09-27T12:00:00Z"},
            {"job_id": "live", "kind": "dispatch", "tier": "standard", "caller_session": "S-LIVE",
             "created_at": "2026-09-27T11:00:00Z"},
            {"job_id": "turn", "kind": "turn", "tier": "hard", "created_at": "2026-09-27T13:00:00Z"}]
    live = scheduler.Liveness(sessions=frozenset({"s-live"}))
    assert [row["job_id"] for row in scheduler.ordered_jobs(policy, jobs, live)] == [
        "turn", "live", "hard-live", "old-background"]


# --- 3. determinism --------------------------------------------------------------------------

@SETTINGS
@given(passes(), st.randoms(use_true_random=False))
def test_a_pass_is_deterministic_whatever_order_rows_arrive_in(case, rng: random.Random):
    policy, store, jobs = case
    view = view_of(store, NOW)
    first = run_pass(policy, view, jobs, live=LIVE)
    again = run_pass(policy, view, jobs, live=LIVE)
    shuffled_jobs = list(jobs)
    rng.shuffle(shuffled_jobs)
    shuffled_view = copy.deepcopy(view)
    rng.shuffle(shuffled_view["lanes"])
    other = run_pass(policy, shuffled_view, shuffled_jobs, live=LIVE)

    def summary(result):
        return [(row.job_id, row.placed, row.hold) for row in result.outcomes]
    assert summary(first) == summary(again) == summary(other)


# --- 4. monotone in the caps -----------------------------------------------------------------

def _raise(policy: dict, key: str, how: str) -> dict:
    """One cap raised by one, or removed."""
    policy = copy.deepcopy(policy)
    where = policy["conversations"] if key in ("max_active_turns", "turn_slots_per_lane") else policy["caps"]
    value = where.get(key)
    where[key] = None if how == "remove" or value is None else value + 1
    return policy


@SETTINGS
@given(passes(), st.sampled_from([*COUNT_CAPS, "max_active_turns", "turn_slots_per_lane"]),
       st.sampled_from(["raise", "remove"]))
def test_raising_or_removing_a_cap_never_places_fewer_jobs(case, key, how):
    policy, store, jobs = case
    policy = quiet(policy)
    view = view_of(store, NOW)
    lower = run_pass(policy, view, jobs, live=LIVE)
    higher = run_pass(_raise(policy, key, how), view, jobs, live=LIVE)
    free = run_pass(uncapped(policy), view, jobs, live=LIVE)
    event(f"{key} {how}: {len(lower.placed)} -> {len(higher.placed)} (uncapped {len(free.placed)})")
    assert len(higher.placed) >= len(lower.placed)
    assert len(free.placed) >= len(higher.placed)


@SETTINGS
@given(passes(), st.sampled_from([*COUNT_CAPS, "max_active_turns", "turn_slots_per_lane"]))
def test_raising_a_cap_never_removes_a_candidate(case, key):
    policy, store, jobs = case
    view = view_of(store, NOW)
    for job in jobs:
        try:
            before = scheduler.evaluate(policy, view, job)
            after = scheduler.evaluate(_raise(policy, key, "remove"), view, job)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            continue
        for low, high in zip(before.evaluations, after.evaluations):
            assert set(low["candidates"]) <= set(high["candidates"])
        if before.chosen_lane:
            assert after.chosen_lane


# --- 5. the desktop lane ---------------------------------------------------------------------

@SETTINGS
@given(passes())
def test_the_desktop_lane_is_out_while_in_use_and_last_otherwise(case):
    policy, store, jobs = case
    for in_use in (None, True, False):
        view = view_of({**store, "desktop_in_use": in_use}, NOW)
        desktop = {lane["lane_id"] for lane in view["lanes"] if lane.get("desktop")}
        for job in jobs:
            try:
                decision = scheduler.evaluate(policy, view, job)
            except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                continue
            if decision.chosen_lane not in desktop:
                continue
            event(f"desktop lane chosen, in use {in_use}")
            if in_use is not False:
                assert job.get("allow_desktop")            # None is judged in use
            evaluation = decision.evaluations[-1]
            assert set(evaluation["candidates"]) <= desktop  # no other candidate for that model


def test_the_desktop_lane_takes_only_what_no_other_lane_can():
    policy = quiet(load_policy(DEFAULT_POLICY_PATH))
    policy["reserve"] = {**policy.get("reserve", {}), "models": []}        # C-11.7 is not the point here
    lanes = [{"lane_id": "claude-1", "provider": "claude", "account_key": "claude:a@example.invalid", "owner": "v2",
              "enabled": True, "desktop": False},
             {"lane_id": "claude-4", "provider": "claude", "account_key": "claude:d@example.invalid", "owner": "v2",
              "enabled": True, "desktop": True}]
    job = {"job_id": "j", "kind": "dispatch", "task": "review", "tier": "standard", "sandbox": "read-only",
           "exclusions": (), "allow_desktop": 0}
    from subfleet import capacity
    free = capacity.build_view(lanes, now=NOW, desktop_in_use=False)
    busy = capacity.build_view(lanes, now=NOW, desktop_in_use=True)
    assert scheduler.evaluate(policy, free, job).chosen_lane == "claude-1"
    closed = {"closure_id": 1, "lane_id": "claude-1", "scope": "account", "until_at": "2026-09-27T00:00:00Z",
              "released_at": None}
    free_closed = capacity.build_view(lanes, closures=[closed], now=NOW, desktop_in_use=False)
    busy_closed = capacity.build_view(lanes, closures=[closed], now=NOW, desktop_in_use=True)
    assert scheduler.evaluate(policy, free_closed, job).chosen_lane == "claude-4"
    assert scheduler.evaluate(policy, busy_closed, job).chosen_lane is None
    assert scheduler.evaluate(policy, busy, job).chosen_lane == "claude-1"
    unknown = capacity.build_view(lanes, closures=[closed], now=NOW)          # no signal: in use
    assert scheduler.evaluate(policy, unknown, job).chosen_lane is None


# --- 6. load bands ---------------------------------------------------------------------------

def _twin_lanes(count: int, provider: str) -> list[dict]:
    return [{"lane_id": f"{provider}-{n}", "provider": provider, "account_key": f"{provider}:l{n}@example.invalid",
             "owner": "v2", "enabled": True, "desktop": False, "credential_kind": "keychain-token"}
            for n in range(1, count + 1)]


@settings(max_examples=150, deadline=None, derandomize=True)
@given(st.integers(1, 6), st.integers(1, 30), st.sampled_from([1, 2, 3]), st.sampled_from(["claude", "codex"]),
       st.lists(st.sampled_from([0.1, 0.3, 0.5, 0.7]), min_size=6, max_size=6))
def test_bands_fill_lanes_evenly_with_no_cap(lanes, jobs, spread, provider, utilization):
    """C-11.3: uncapped, no lane is more than one band ahead of another that
    could take the same work, however their headroom differs."""
    from subfleet import capacity
    policy = quiet(uncapped(load_policy(DEFAULT_POLICY_PATH)))
    policy["admission"]["lane_spread"] = spread
    policy["reserve"] = {**policy.get("reserve", {}), "models": []}
    rows = _twin_lanes(lanes, provider)
    readings = [{"reading_id": n, "lane_id": row["lane_id"], "scope": "account", "window": "seven_day",
                 "utilization": utilization[n], "resets_at": f"2026-09-{27 + n % 3}T00:00:00Z",
                 "label": "provider", "source": "oauth-usage", "observed_at": "2026-09-26T11:59:30Z"}
                for n, row in enumerate(rows)]
    view = capacity.build_view(rows, readings, now=NOW, desktop_in_use=False)
    model = "opus" if provider == "claude" else "astra"
    batch = [{"job_id": f"j{n}", "kind": "dispatch", "pinned_model": model, "sandbox": "read-only",
              "exclusions": (), "allow_desktop": 0, "created_at": f"2026-09-26T11:00:{n:02d}Z"} for n in range(jobs)]
    result = run_pass(policy, view, batch)
    assert len(result.placed) == jobs
    bands = [result.view["in_flight"].get(row["lane_id"], 0) // spread for row in rows]
    assert max(bands) - min(bands) <= 1


@SETTINGS
@given(passes(), st.sampled_from([1, 2, 3]))
def test_a_band_as_wide_as_the_per_lane_cap_chooses_as_the_cap_did(case, width):
    """C-11.3: while every candidate is under a per-lane cap of `width`, a band of
    `width` puts them all in band 0, so the choice is the comparator's alone:
    with caps equal to the band, placement is what it was before bands."""
    policy, store, jobs = case
    policy = copy.deepcopy(policy)
    policy["caps"].update(max_in_flight_per_lane=width, max_in_flight_unmeasured=None)
    policy["conversations"].update(turn_slots_per_lane=width)
    banded, flat = copy.deepcopy(policy), copy.deepcopy(policy)
    banded["admission"]["lane_spread"], flat["admission"]["lane_spread"] = width, None
    view = view_of(store, NOW)
    for job in jobs:
        try:
            left, right = scheduler.evaluate(banded, view, job), scheduler.evaluate(flat, view, job)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            continue
        assert (left.chosen_lane, left.chosen_model) == (right.chosen_lane, right.chosen_model)


# --- 7. the machine guard --------------------------------------------------------------------

READINGS = st.fixed_dictionaries({
    "load1": st.one_of(st.none(), st.floats(0, 400, allow_nan=False)),
    "load5": st.one_of(st.none(), st.floats(0, 400, allow_nan=False)),
    "cpus": st.sampled_from([None, 1, 8, 18]),
    "memory_pressure": st.sampled_from([None, 1, 2, 4])})


@settings(max_examples=500, deadline=None, derandomize=True)
@given(READINGS, st.floats(0, 100, allow_nan=False), st.sampled_from([None, 1, 2, 4]))
def test_the_guard_never_holds_a_turn_and_holds_more_only_as_load_rises(reading, more_load, more_pressure):
    policy = guarded()
    assert scheduler.machine_hold(policy, reading, "attended") is None
    heavier = {**reading, "load1": None if reading["load1"] is None else reading["load1"] + more_load,
               "load5": None if reading["load5"] is None else reading["load5"] + more_load,
               "memory_pressure": max(filter(None, (reading["memory_pressure"], more_pressure)), default=None)}
    for klass in ("session", "background"):
        if scheduler.machine_hold(policy, reading, klass):
            assert scheduler.machine_hold(policy, heavier, klass)
    # A class held is held at every class below it, with the proposed thresholds.
    if scheduler.machine_hold(policy, reading, "session"):
        assert scheduler.machine_hold(policy, reading, "background")
    off = {**policy, "admission": {**policy["admission"], "machine_guard": None}}
    assert all(scheduler.machine_hold(off, heavier, klass) is None for klass in scheduler.PRIORITY_CLASSES)


def test_the_guard_at_the_2026_09_27_evening_load():
    """With the proposed guard on: at 20:26 EDT the averages were 93 and 112 on 18
    CPUs, pressure normal, so background work is held at the 5-minute average (6.2
    per CPU) and session work is not. Off (the default), nothing is held."""
    policy = guarded()
    evening = {"load1": 92.92, "load5": 111.54, "cpus": 18, "memory_pressure": 1}
    hold = scheduler.machine_hold(policy, evening, "background")
    assert hold == {"reason": "machine-busy", "class": "background", "load_per_cpu": 6.2, "load_threshold": 6.0}
    assert scheduler.machine_hold(policy, evening, "session") is None
    assert scheduler.machine_hold(policy, {**evening, "memory_pressure": 4}, "session")["memory_threshold"] == "critical"
    assert scheduler.machine_hold(policy, {"load1": None, "load5": None, "cpus": 18, "memory_pressure": None},
                                  "background") is None
    shipped = load_policy(DEFAULT_POLICY_PATH)
    assert shipped["admission"]["machine_guard"] is None
    assert all(scheduler.machine_hold(shipped, {**evening, "memory_pressure": 4}, klass) is None
               for klass in scheduler.PRIORITY_CLASSES)


@SETTINGS
@given(passes(), READINGS)
def test_the_guard_holds_detached_jobs_at_the_door_and_never_a_turn(case, reading):
    policy, store, jobs = case
    policy["admission"]["machine_guard"] = copy.deepcopy(MACHINE_GUARD_PROPOSAL)
    result = run_pass(policy, view_of(store, NOW), jobs, live=LIVE, machine=reading)
    for outcome in result.outcomes:
        if outcome.hold == "machine-busy":
            assert outcome.klass != "attended" and outcome.decision is None


# --- the pool rule ---------------------------------------------------------------------------

def test_a_pool_holds_back_only_while_it_has_a_count():
    policy = load_policy(DEFAULT_POLICY_PATH)
    assert not scheduler.pool_capped(policy, {"kind": "dispatch"})
    assert not scheduler.pool_capped(policy, {"kind": "turn"})
    assert not scheduler.pool_capped(policy, {"kind": "dispatch", "parent_job_id": "p"})
    assert scheduler.pool_capped(capped(copy.deepcopy(policy)), {"kind": "dispatch"})
    assert not scheduler.pool_capped(capped(copy.deepcopy(policy)), {"kind": "turn"})
    parent = copy.deepcopy(policy)
    parent["caps"]["max_active_attempts_per_parent"] = 2
    assert scheduler.pool_capped(parent, {"kind": "dispatch", "parent_job_id": "p"})
    assert not scheduler.pool_capped(parent, {"kind": "dispatch"})
    turns = copy.deepcopy(policy)
    turns["conversations"]["turn_slots_per_lane"] = 1
    assert scheduler.pool_capped(turns, {"kind": "turn"}) and not scheduler.pool_capped(turns, {"kind": "dispatch"})


def test_the_2026_09_27_jam_places_everything_uncapped():
    """20:26 EDT: four open Claude lanes each at 2, six Codex lanes each at 1, 35
    jobs behind one head job. With no cap every one of them has a lane."""
    from subfleet import capacity
    policy = quiet(load_policy(DEFAULT_POLICY_PATH))
    policy["reserve"] = {**policy.get("reserve", {}), "models": []}
    claude = [{"lane_id": f"claude-{n}", "provider": "claude", "account_key": f"claude:c{n}@example.invalid",
               "owner": "v2", "enabled": True, "desktop": False} for n in (1, 6, 7, 9)]
    codex = _twin_lanes(6, "codex")
    attempts = ([{"attempt_id": f"c{n}-{k}", "job_id": f"run-c{n}-{k}", "lane_id": f"claude-{n}", "state": "running"}
                 for n in (1, 6, 7, 9) for k in range(2)]
                + [{"attempt_id": f"x{n}", "job_id": f"run-x{n}", "lane_id": f"codex-{n}", "state": "running"}
                   for n in range(1, 7)])
    view = capacity.build_view(claude + codex, attempts=attempts, jobs=[{"job_id": row["job_id"], "kind": "dispatch"}
                                                                        for row in attempts], now=NOW)
    queue = [{"job_id": f"q{n}", "kind": "dispatch", "task": "review", "tier": "standard", "sandbox": "read-only",
              "exclusions": (), "allow_desktop": 0, "created_at": f"2026-09-26T11:{n:02d}:00Z"} for n in range(35)]
    assert run_pass(capped(copy.deepcopy(policy)), view, queue).placed == []
    free = run_pass(policy, view, queue)
    assert len(free.placed) == 35
    counts = [free.view["in_flight"][row["lane_id"]] for row in claude]
    assert max(counts) - min(counts) <= policy["admission"]["lane_spread"]
