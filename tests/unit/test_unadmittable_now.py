"""C-6.20: `scheduler.unadmittable(..., far_s=0)` asks "would any lane take this job now,
given room?", which admission asks before it cuts a fresh job's tree.

With the default horizon (`admission.pin_hold_far_s`, 7 days) a closure counts only when
it ends that far out (C-11.8). With 0, every closure in force counts. The oracle is the
one `test_unadmittable_pin` uses, with only the closures that end within 0 s taken
away: every lane measured idle, nothing in flight, every expired token healed. What is
left is what keeps a lane from the job now whatever capacity does.
"""
from __future__ import annotations

import copy

from hypothesis import assume, given, strategies as st

from subfleet import scheduler
from subfleet.capacity import _time, credential_gone
from tests.routing_strategies import NOW, policies, route_jobs, stamp, view_of
from tests.unit.test_unadmittable_pin import RAISES, SETTINGS, pin_stores, uncapped
from datetime import timedelta


def best_now(store: dict) -> dict:
    """`test_unadmittable_pin.best` at a horizon of 0: every closure in force stays."""
    better = copy.deepcopy(store)
    better["readings"] = [{"reading_id": n + 1, "lane_id": row["lane_id"], "scope": "account", "window": "seven_day",
                           "utilization": 0.0, "resets_at": stamp(NOW + timedelta(days=3)), "label": "provider",
                           "source": "oauth-usage", "observed_at": stamp(NOW), "attempt_id": None}
                          for n, row in enumerate(store["lanes"])]
    better["attempts"] = []
    gone = {row["lane_id"] for row in store["lanes"] if credential_gone(row)}
    better["unavailable"] = {lane: holder for lane, holder in store["unavailable"].items()
                             if holder == "credential-latched" and lane in gone}
    better["overridden"] = set()
    better["closures"] = [row for row in store["closures"]
                          if row["released_at"] or (_time(row["until_at"]) - NOW).total_seconds() > 0]
    return view_of(better, NOW)


def admissible_now(policy: dict, store: dict, job: dict) -> bool | None:
    try:
        return scheduler.evaluate(uncapped(policy), best_now(store), job).chosen_lane is not None
    except RAISES:
        return None


@SETTINGS
@given(st.data())
def test_c6_20_refused_now_is_exactly_the_oracle_and_the_memo_changes_nothing(data):
    """Differential: `unadmittable(far_s=0)` names a refusal exactly when no lane would take
    the job now in the best capacity; a memo shared with the default horizon's answers
    changes neither."""
    policy, store = data.draw(policies()), data.draw(pin_stores())
    view = view_of(store, NOW)
    memo: dict = {}
    for job in [data.draw(route_jobs(store)) for _ in range(data.draw(st.integers(1, 4)))]:
        fresh = scheduler.unadmittable(policy, view, job, far_s=0)
        far = scheduler.unadmittable(policy, view, job)
        assert scheduler.unadmittable(policy, view, job, memo=memo) == far
        assert scheduler.unadmittable(policy, view, job, memo=memo, far_s=0) == fresh
        try:
            scheduler.prepare(policy, view, job)
        except RAISES:
            assert fresh is None
            continue
        assert (fresh is not None) == (admissible_now(policy, store, job) is False), (fresh, job)


@SETTINGS
@given(st.data())
def test_c6_20_a_job_refused_for_good_is_refused_now(data):
    """Monotone in the horizon: what keeps every lane from a job for days keeps it now."""
    policy, store = data.draw(policies()), data.draw(pin_stores())
    view = view_of(store, NOW)
    job = data.draw(route_jobs(store))
    for_good = scheduler.unadmittable(policy, view, job)
    assume(for_good is not None)
    assert scheduler.unadmittable(policy, view, job, far_s=0) is not None


@SETTINGS
@given(st.data())
def test_c6_20_a_job_refused_now_is_placed_nowhere_now(data):
    """Sound against the real evaluation: a job refused now gets no lane in the view itself."""
    policy, store = data.draw(policies()), data.draw(pin_stores())
    view = view_of(store, NOW)
    job = data.draw(route_jobs(store))
    assume(scheduler.unadmittable(policy, view, job, far_s=0) is not None)
    try:
        decision = scheduler.evaluate(policy, view, job)
    except RAISES:
        return
    assert decision.chosen_lane is None, decision.chosen_lane


def test_c6_20_refusal_label_groups_closures_by_scope():
    assert scheduler.refusal_label(["closed:account:2026-11-02T23:05:03Z", "closed:account:2026-11-04T06:35:44Z",
                                    "credential-latched"]) == "closed:account"
    assert scheduler.refusal_label(["credential-latched", "closed:gpt-6-astra:2026-11-02T23:00:00Z"]) == \
        "credential-latched"
    assert scheduler.refusal_label([]) == "not-evaluated"
