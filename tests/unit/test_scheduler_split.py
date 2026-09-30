"""C-6.3: `scheduler.evaluate`, split into `prepare`, `judge_lane` and `rank_key`
so one lane can be judged alone inside a reservation (`route_check`), decides
exactly what the evaluate it replaced decided, error for error.
`tests/reference_scheduler.py` is that one, verbatim from e053b2c.
"""

from __future__ import annotations

import dataclasses

from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import scheduler
from tests.reference_scheduler import reference_evaluate
from tests.routing_strategies import NOW, event, policies, route_jobs, stores, view_of

SETTINGS = settings(max_examples=1500, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                                           HealthCheck.filter_too_much])


def outcome(fn, *args):
    """A decision as data, or the error it raised, so two evaluations compare whole."""
    try:
        return "decision", dataclasses.asdict(fn(*args))
    except Exception as exc:  # noqa: BLE001 - the error is the outcome being compared
        return "error", (type(exc).__name__, str(exc))


@st.composite
def cases(draw):
    store = draw(stores())
    return draw(policies()), store, draw(route_jobs(store))


# --- the split evaluate is the evaluate it replaced ------------------------------------------------

@SETTINGS
@given(cases())
def test_c6_3_the_split_evaluate_decides_what_the_whole_one_did(case):
    policy, store, job = case
    view = view_of(store, NOW)
    expected = outcome(reference_evaluate, policy, view, job)
    event(f"reference: {expected[0]}, chose {'a lane' if expected[0] == 'decision' and expected[1]['chosen_lane'] else 'none'}")
    assert outcome(scheduler.evaluate, policy, view, job) == expected


def test_c6_3_evaluate_reads_only_the_lane_facts_of_a_lane(monkeypatch):
    """`LANE_FACTS` is every lane field `prepare` and `judge_lane` read, so a lane whose
    values of them did not change is judged as it was (`route_check.lane_facts`)."""
    read: set[str] = set()

    class Recording(dict):
        def __getitem__(self, key):
            read.add(key)
            return super().__getitem__(key)

        def get(self, key, default=None):
            read.add(key)
            return super().get(key, default)

    @SETTINGS
    @given(cases())
    def run(case):
        policy, store, job = case
        view = view_of(store, NOW)
        view["lanes"] = [Recording(lane) for lane in view["lanes"]]
        monkeypatch.setattr(scheduler, "_row", lambda value: value if isinstance(value, Recording)
                            else dataclasses.asdict(value) if dataclasses.is_dataclass(value) else dict(value))
        try:
            scheduler.evaluate(policy, view, job)
        except Exception:  # noqa: BLE001 - what was read before an error counts too
            pass
    run()
    assert read and read <= set(scheduler.LANE_FACTS), read - set(scheduler.LANE_FACTS)


# --- turn caps: the split evaluate and the reference agree where the caps decide -------------------

TURN_CAP_VALUES = st.sampled_from([None, 0, 1, 2, 3])


@st.composite
def turn_cases(draw):
    """C-26.9: fleets where turn caps bind. The general strategies above rarely reach one:
    a lane holding as many turns as its cap, with no closure, probe or parent block."""
    import copy

    from tests.unit.test_scheduler import attempt, closure, lane, reading, view
    from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
    policy = load_policy(DEFAULT_POLICY_PATH)
    if draw(st.booleans()):
        policy["reserve"] = {**policy.get("reserve", {}), "models": []}
    policy = copy.deepcopy(policy)
    policy["conversations"].update(max_active_turns=draw(TURN_CAP_VALUES), turn_slots_per_lane=draw(TURN_CAP_VALUES))
    identities = [f"claude-{n}" for n in range(1, 1 + draw(st.integers(1, 4)))]
    lanes = [lane(identity, desktop=draw(st.integers(0, 5)) == 0, enabled=draw(st.integers(0, 5)) > 0)
             for identity in identities]
    rows = [reading(identity, draw(st.sampled_from([.1, .5, .9, .99]))) for identity in identities]
    closures = [closure(identity) for identity in identities if draw(st.integers(0, 4)) == 0]
    attempts, jobs = [], []
    for identity in identities:
        for kind, most in (("turn", 3), ("dispatch", 2)):
            for n in range(draw(st.integers(0, most))):
                job_id = f"{identity}-{kind}-{n}"
                attempts.append(attempt(identity, job_id))
                jobs.append({"job_id": job_id, "kind": kind, "state": "running"})
    snapshot = view(lanes, rows, closures=closures, attempts=attempts, jobs=jobs)
    kind = draw(st.sampled_from(["turn", "turn", "dispatch"]))
    job = {"task": None, "tier": None, "sandbox": "read-only", "pinned_model": "opus", "kind": kind}
    if draw(st.booleans()):
        job["affinity_lane"] = draw(st.sampled_from(identities))
    return policy, snapshot, job


@settings(max_examples=1500, deadline=None, derandomize=True, suppress_health_check=[HealthCheck.too_slow])
@given(turn_cases())
def test_c26_9_the_split_evaluate_and_the_reference_agree_on_turn_caps(case):
    policy, view, job = case
    expected = outcome(reference_evaluate, policy, view, job)
    caps = policy["conversations"]
    event(f"turn caps: fleet {caps['max_active_turns']}, lane {caps['turn_slots_per_lane']}; "
          f"{job['kind']} {'placed' if expected[0] == 'decision' and expected[1]['chosen_lane'] else 'held'}")
    assert outcome(scheduler.evaluate, policy, view, job) == expected
