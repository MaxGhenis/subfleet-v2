"""C-6.9, C-6.12: a job held behind an older one while its capacity clock runs still holds its competitors.

Stateful admission properties, findings 5 and 6 (2026-10-12,
`docs/reports/2026-10-12-admission-properties.md`): `Daemon._admit_pass` checked
`behind` before it registered a clocked capacity wait as a waiter, and a transient
retry whose pin a look let go was held behind an older job without registering. So
in a pool with a count cap a job held `behind-older-job` stopped holding back the
later jobs that compete only with it: a younger job that competed with it and not
with the job ahead of it was placed past it.

Properties, for every input the strategies draw: the routing strategies' lanes,
readings, closures, pins and caps, and capacity clocks laid on random jobs (C-6.10:
`waiting` on `capacity` with its clock ahead, so not evaluated on this pass). They
run on the pure pass model (`tests/admission_model.py`), and the oracles read only
outcomes and the jobs' own fields, never the model's waiter lists:

1. No pass past a waiter on its clock: in a pool with a count cap, a job is never
   placed after an older job of its tier and pool that it competes with and that
   waits on its capacity clock, whether or not that job is held behind another,
   unless no lane can ever admit that job (C-11.8).
2. No hold through a due job: a job held behind an older job that is itself held
   `behind-older-job` is held behind one on its capacity clock that some lane could
   admit. A held job gets no look and so no new clock: once its clock is due, the
   holds that ran through it end (the bound the fix's review asked for).
3. Confined: with no job on its capacity clock, a pass is outcome for outcome the
   pass of the rule before (`held_waiters=False`).
4. Not vacuous: how often the rule before placed a job past a held waiter on its
   clock, as a Hypothesis event.

The daemon is checked against the model in `tests/fake/test_admission_held_waiters.py`.
"""

from __future__ import annotations

import copy
from datetime import timedelta

from hypothesis import HealthCheck, event, given, settings, strategies as st

from subfleet import scheduler
from tests.admission_model import _tier, run_pass
from tests.routing_strategies import BASE_POLICY, NOW, policies, route_jobs, stamp, stores, view_of

SETTINGS = settings(max_examples=300, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                                           HealthCheck.filter_too_much])


@st.composite
def queues(draw, *, clocks: bool = True):
    """A pass in a pool that usually has a count cap (C-6.9 holds back only then): a store,
    and a queue of jobs, some on their capacity clocks."""
    policy = draw(policies())
    policy["caps"]["max_active_attempts"] = draw(st.sampled_from([None, 1, 2, 3, 6, 10]))
    policy["conversations"]["max_active_turns"] = draw(st.sampled_from([None, 1, 2, 3, 10]))
    store = draw(stores())
    count = draw(st.integers(2, 8))
    minutes = draw(st.lists(st.integers(0, 59), min_size=count, max_size=count, unique=True))
    jobs = []
    for index, minute in enumerate(minutes):
        job = draw(route_jobs(store))
        job.update(job_id=f"q-{index}", created_at=f"2026-09-26T11:{minute:02d}:00Z", caller_session=None,
                   parent_job_id=None)
        if clocks and draw(st.integers(0, 2)):
            job["capacity_look"] = "clock"
        jobs.append(job)
    return policy, store, jobs


def _pool(job: dict) -> str:
    return "turn" if job.get("kind") == "turn" else "detached"


def _competes(policy: dict, view: dict, one: dict, other: dict) -> bool:
    """C-6.9: some model could serve both, and some lane could serve both."""
    lanes = view.get("lanes", ())
    return scheduler.competes(scheduler.demand_models(policy, one), scheduler.demand_models(policy, other),
                              scheduler.demand_lanes(lanes, one, policy), scheduler.demand_lanes(lanes, other, policy))


def _passed_clocked_waiters(policy: dict, view: dict, jobs: list[dict], result) -> list[tuple[str, str]]:
    """(placed job, older clocked waiter it passed) for every placement past an older job of its
    tier and pool that it competes with, that waits on its capacity clock and that some lane could
    admit, in a pool with a count cap: what property 1 forbids."""
    by_id = {job["job_id"]: job for job in jobs}
    found, seen = [], []
    for row in result.outcomes:
        job = by_id[row.job_id]
        if row.placed and scheduler.hold_scope(policy, job) == "pool":
            for older in seen:
                if (_tier(policy, older) == _tier(policy, job) and _pool(older) == _pool(job)
                        and _competes(policy, view, job, older)):
                    found.append((row.job_id, older["job_id"]))
        outcome = row.hold
        if (job.get("capacity_look") == "clock" and outcome in ("capacity", "behind-older-job")
                and not scheduler.unadmittable(policy, view, job)):
            seen.append(job)
    return found


# --- 1: no job passes a waiter on its clock ----------------------------------------------------

@SETTINGS
@given(queues())
def test_no_job_is_placed_past_a_waiter_on_its_clock_held_behind_another_or_not(case):
    policy, store, jobs = case
    view = view_of(store, NOW)
    result = run_pass(policy, view, jobs)
    passed = _passed_clocked_waiters(policy, view, jobs, result)
    assert not passed, f"placed past an older waiter on its clock that it competes with: {passed}"
    held = [row for row in result.outcomes if row.hold == "behind-older-job"
            and next(job for job in jobs if job["job_id"] == row.job_id).get("capacity_look") == "clock"]
    event(f"waiters on their clock held behind another: {min(len(held), 2)}{'+' if len(held) >= 2 else ''}")


# --- 2: no hold runs through a job whose clock is due -----------------------------------------

@SETTINGS
@given(queues())
def test_a_hold_runs_through_a_held_job_only_while_its_clock_runs(case):
    policy, store, jobs = case
    view = view_of(store, NOW)
    result = run_pass(policy, view, jobs)
    by_id, outcomes = {job["job_id"]: job for job in jobs}, result.by_job()
    through = 0
    for row in result.outcomes:
        if row.hold != "behind-older-job" or outcomes[row.waits_for].hold != "behind-older-job":
            continue
        through += 1
        middle = by_id[row.waits_for]
        assert middle.get("capacity_look") == "clock", (
            f"{row.job_id} is held behind {middle['job_id']}, which is held behind "
            f"{outcomes[middle['job_id']].waits_for} and is not on its clock")
        assert not scheduler.unadmittable(policy, view, middle), (
            f"{row.job_id} is held behind {middle['job_id']}, which no lane can admit (C-11.8)")
    event(f"holds through a held job: {min(through, 2)}{'+' if through >= 2 else ''}")


@SETTINGS
@given(queues())
def test_once_every_clock_is_due_no_hold_runs_through_a_held_job(case):
    """The same queue a clock later: every job held behind another is due now, and while it is still
    held behind one it holds nobody."""
    policy, store, jobs = case
    view = view_of(store, NOW)
    first = run_pass(policy, view, jobs).by_job()
    due = {job_id for job_id, row in first.items() if row.hold == "behind-older-job"}
    later = [{key: value for key, value in job.items() if key != "capacity_look"} if job["job_id"] in due else job
             for job in jobs]
    result = run_pass(policy, view, later)
    outcomes = result.by_job()
    for row in result.outcomes:
        if row.hold == "behind-older-job" and row.waits_for in due:
            assert outcomes[row.waits_for].hold != "behind-older-job", (
                f"{row.job_id} is held behind {row.waits_for}, which is due and itself held behind "
                f"{outcomes[row.waits_for].waits_for}")


# --- 3: confined to jobs on their clock -------------------------------------------------------

@SETTINGS
@given(queues(clocks=False))
def test_with_no_job_on_its_clock_the_pass_is_the_rule_before(case):
    policy, store, jobs = case
    view = view_of(store, NOW)
    new, old = run_pass(policy, view, jobs), run_pass(policy, view, jobs, held_waiters=False)
    assert ([(row.job_id, row.placed, row.hold, row.waits_for) for row in new.outcomes]
            == [(row.job_id, row.placed, row.hold, row.waits_for) for row in old.outcomes])


# --- 4: the rule before broke property 1 ------------------------------------------------------

@SETTINGS
@given(queues())
def test_the_rule_before_placed_jobs_past_held_waiters_on_their_clock(case):
    """Not vacuous: the strategies draw what property 1 is about, as often as the event says."""
    policy, store, jobs = case
    view = view_of(store, NOW)
    passed = _passed_clocked_waiters(policy, view, jobs, run_pass(policy, view, jobs, held_waiters=False))
    event(f"placements past a held waiter on its clock by the rule before: {min(len(passed), 2)}"
          f"{'+' if len(passed) >= 2 else ''}")


def _lane(lane_id: str) -> dict:
    provider = lane_id.split("-")[0]
    return {"lane_id": lane_id, "provider": provider, "account_key": f"{provider}:{lane_id}@example.invalid",
            "credential_ref": f"/credentials/{lane_id}", "credential_kind": "home", "credential_epoch": 1,
            "home": f"/homes/{lane_id}", "owner": "v2", "desktop": 0, "enabled": 1, "plan": None, "identity": None,
            "label": None, "identity_status": None, "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z"}


def test_the_rule_before_placed_the_younger_job_past_the_middle_one():
    """Example for property 4, finding 5's counterexample on the model: an older Astra job and a middle
    Opus-or-Astra job wait on their clocks; a younger Opus job competes only with the middle one. The
    rule before placed it; now it waits behind the middle job."""
    policy = copy.deepcopy(BASE_POLICY)
    policy["caps"]["max_active_attempts"] = 10
    readings = [{"reading_id": n, "lane_id": lane_id, "scope": "account", "window": "seven_day", "utilization": 0.2,
                 "resets_at": stamp(NOW + timedelta(days=3)), "label": "provider", "source": "oauth-usage",
                 "observed_at": stamp(NOW), "attempt_id": None}
                for n, lane_id in enumerate(("claude-1", "codex-1"), start=1)]
    store = {"lanes": [_lane("claude-1"), _lane("codex-1")], "readings": readings, "closures": [], "jobs": [],
             "attempts": [], "unavailable": {}, "overridden": set(), "desktop_in_use": False}
    view = view_of(store, NOW)
    base = {"kind": "dispatch", "sandbox": "read-only", "task": None, "tier": None, "pinned_model": None,
            "pinned_lane": None, "allow_desktop": 0, "parent_job_id": None, "policy_hash": "fixture",
            "unmeasured_reserve_reason": None, "exclusions": (), "caller_session": None}
    jobs = [dict(base, job_id="older", created_at="2026-09-26T11:00:00Z", pinned_model="astra", capacity_look="clock"),
            dict(base, job_id="middle", created_at="2026-09-26T11:01:00Z", task="review", tier="standard",
                 capacity_look="clock"),
            dict(base, job_id="younger", created_at="2026-09-26T11:02:00Z", pinned_model="opus")]
    new, old = run_pass(policy, view, jobs).by_job(), run_pass(policy, view, jobs, held_waiters=False).by_job()
    assert (new["middle"].hold, new["middle"].waits_for) == ("behind-older-job", "older")
    assert (new["younger"].hold, new["younger"].waits_for) == ("behind-older-job", "middle")
    assert old["younger"].placed == "claude-1"
    # A clock later the middle job is due and holds nobody: the younger job is placed.
    due = [{key: value for key, value in job.items() if key != "capacity_look"} if job["job_id"] == "middle" else job
           for job in jobs]
    later = run_pass(policy, view, due).by_job()
    assert later["middle"].hold == "behind-older-job" and later["younger"].placed == "claude-1"
