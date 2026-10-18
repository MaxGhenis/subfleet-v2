"""C-6.16: priority callers, with the previous scheduler as a differential oracle."""

import copy

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import scheduler
from subfleet.policy import MACHINE_GUARD_PROPOSAL
from tests import priority_callers_reference as previous

PROPERTIES = settings(max_examples=500, deadline=None, derandomize=True)
TIERS = ["trivial", "standard", "hard"]
LIVE = scheduler.Liveness(sessions=frozenset({"live"}), jobs=frozenset({"j0", "j1"}))
CALLER = "92ec8be9-0000-4000-8000-00000000abcd"


def policy(callers=None):
    return {"tiers": TIERS, "admission": {"priority_callers": callers,
                                         "machine_guard": copy.deepcopy(MACHINE_GUARD_PROPOSAL)}}


@st.composite
def queues(draw):
    rows = draw(st.lists(st.fixed_dictionaries({
        "kind": st.sampled_from(["job", "gate-review", "turn"]),
        "caller_session": st.sampled_from([None, "fav", "FAV", "live", "gone"]),
        "tier": st.sampled_from([None, "", *TIERS, "unknown"]),
        "created_at": st.sampled_from([None, "", "2026-10-06T11:00:00Z", "2026-10-06T12:00:00Z"]),
        "parent_job_id": st.sampled_from([None, "missing", "j0", "j1"]),
    }), max_size=25))
    return [{**row, "job_id": f"j{index}"} for index, row in enumerate(rows)]


@PROPERTIES
@given(queues(), st.permutations(TIERS), st.one_of(st.none(), st.just(LIVE)))
def test_priority_precedes_other_detached_jobs_and_turn_order_is_unchanged(jobs, tiers, live):
    chosen = policy(["fav"])
    chosen["tiers"] = tiers
    ordered = scheduler.ordered_jobs(chosen, iter(jobs), live)
    family = {job["job_id"]: job for job in jobs}
    classes = [scheduler.priority_class(job, live, policy=chosen, jobs=family) for job in ordered]
    priority = [i for i, klass in enumerate(classes) if klass == "priority"]
    others = [i for i, klass in enumerate(classes) if klass in ("session", "background")]
    assert all(a < b for a in priority for b in others)
    assert all(i < p for i, klass in enumerate(classes) if klass == "attended" for p in priority)
    old = previous.ordered_jobs(chosen, jobs, live)
    assert [row for row in ordered if row["kind"] == "turn"] == [row for row in old if row["kind"] == "turn"]
    for row in ordered:
        tier = row["tier"] or "standard"
        assert scheduler.waiter_class(row, tier) == (f"{tier}#turn" if row["kind"] == "turn" else tier)
    expected_fifo = sorted((job for job in jobs if scheduler.priority_class(
        job, live, policy=chosen, jobs=family) == "priority"), key=lambda job: job["created_at"] or "")
    assert [row for row in ordered if scheduler.priority_class(
        row, live, policy=chosen, jobs=family) == "priority"] == expected_fifo


@PROPERTIES
@given(queues(), st.permutations(TIERS), st.one_of(st.none(), st.just(LIVE)),
       st.sampled_from([None, []]))
def test_null_priority_callers_order_is_identical_to_previous_scheduler(jobs, tiers, live, callers):
    unchanged = policy(callers)
    unchanged["tiers"] = tiers
    assert scheduler.ordered_jobs(unchanged, iter(jobs), live) == previous.ordered_jobs(unchanged, jobs, live)
    del unchanged["admission"]["priority_callers"]
    assert scheduler.ordered_jobs(unchanged, jobs, live) == previous.ordered_jobs(unchanged, jobs, live)


READINGS = st.one_of(st.none(), st.fixed_dictionaries({
    "load1": st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True)),
    "load5": st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True)),
    "cpus": st.one_of(st.none(), st.integers(-1, 256)),
    "memory_pressure": st.one_of(st.none(), st.integers(-1, 10)),
}))


@PROPERTIES
@given(READINGS)
def test_priority_never_held_and_other_classes_have_identical_guard_holds(reading):
    chosen = policy(["fav"])
    assert scheduler.machine_hold(chosen, reading, "priority") is None
    # Defence in depth: even an unvalidated policy cannot hold priority work.
    chosen["admission"]["machine_guard"]["priority"] = {"load_per_cpu": .01, "memory_pressure": "warn"}
    assert scheduler.machine_hold(chosen, reading, "priority") is None
    for klass in ("attended", "session", "background"):
        assert scheduler.machine_hold(chosen, reading, klass) == previous.machine_hold(chosen, reading, klass)


@PROPERTIES
@given(st.integers(0, 5), st.integers(-1, 5), st.integers(-1, 5),
       st.sampled_from([None, "turn", "job", "gate-review"]))
def test_ancestor_inheritance_up_to_depth_five_and_cycles_terminate(depth, chosen_at, cycle_at, kind):
    chain = [{"job_id": f"j{i}", "kind": "job", "caller_session": CALLER.upper() if i == chosen_at else "other",
              "parent_job_id": f"j{i + 1}" if i < depth else None} for i in range(depth + 1)]
    if 0 <= cycle_at <= depth:
        chain[-1]["parent_job_id"] = f"j{cycle_at}"
    chain[0]["kind"] = kind
    family = {job["job_id"]: job for job in chain}
    actual = scheduler.priority_class(chain[0], LIVE, policy=policy([CALLER]), jobs=family)
    expected = ("attended" if kind == "turn" else "priority" if 0 <= chosen_at <= depth else
                previous.priority_class(chain[0], LIVE))
    assert actual == expected
    # Ancestors do not need to be in the queue for a descendant to inherit.
    older = {"job_id": "older", "kind": "gate-review", "created_at": ""}
    ordered = scheduler.ordered_jobs(policy([CALLER]), [older, chain[0]], LIVE, ancestors=family)
    assert ordered == ([chain[0], older] if expected in ("attended", "priority") else [older, chain[0]])


@PROPERTIES
@given(st.uuids(), st.booleans(), st.booleans())
def test_priority_caller_matching_is_case_insensitive(session, upper_policy, upper_job):
    session = str(session)
    configured = session.upper() if upper_policy else session.lower()
    recorded = session.upper() if upper_job else session.lower()
    job = {"kind": "job", "caller_session": recorded}
    assert scheduler.priority_class(job, LIVE, policy=policy([configured])) == "priority"


def test_priority_hard_job_goes_before_its_younger_trivial_job():
    jobs = [{"job_id": "cheap", "tier": "trivial", "caller_session": "fav", "created_at": "2"},
            {"job_id": "hard", "tier": "hard", "caller_session": "fav", "created_at": "1"}]
    assert [job["job_id"] for job in scheduler.ordered_jobs(policy(["fav"]), jobs, LIVE)] == ["hard", "cheap"]


@pytest.mark.parametrize("live", [None, LIVE])
def test_uppercase_config_matches_lowercase_incident_caller_even_without_liveness(live):
    job = {"caller_session": CALLER}
    assert scheduler.priority_class(job, live, policy=policy([CALLER.upper()])) == "priority"


def test_missing_parent_ends_inheritance_walk():
    job = {"caller_session": "other", "parent_job_id": "missing"}
    assert scheduler.priority_class(job, LIVE, policy=policy([CALLER]), jobs={}) == "background"
