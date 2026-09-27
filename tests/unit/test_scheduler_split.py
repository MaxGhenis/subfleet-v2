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
