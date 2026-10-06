"""C-6.9, C-26.9: a job waiting for a lease holds no later job back; it keeps the last slot.

Review of fenced-turn-precheck (0812d5c5), P3-4, measured 2026-10-06: with a pool
cap set (`conversations.max_active_turns` 5, `caps.max_active_attempts` 5), an older
job waiting `lease-held` (on retention's fence, a detached writer's checkout, its
conversation, a native session, an output path) held a later job that competes with
it `behind-older-job` on every look, with 0 or 1 of 5 slots live, for as long as the
lease was held. C-6.9's FIFO exists so a later job cannot take the slot an older one
waits for; a job waiting for a lease waits for no slot. Its lease keeps its order
through C-26.9's queue, and in a pool with a fleet count a job that passes it keeps
that count's last slot for it, as for any older waiter, so it starts the moment its
lease is let go unless a per-lane or parent count, which keeps no slot, then refuses
it (property 3 counts neither; `tests/fake/test_admission_lease_waiters.py` pins both
as intended).

Properties, for every input the strategies draw: the routing strategies' lanes,
readings, closures and pins, a pool cap or none, and lease waits laid on random jobs,
looked at on their clock, by retention's fence look or by the reserving transaction,
some of whose keys a later job holds itself. They run on the pure pass model
(`tests/admission_model.py`), and the oracles read only outcomes and counts, never
the model's waiter lists:

1. No job is held `behind-older-job` behind a job waiting for a lease.
2. The kept slot: in a pool with a fleet count c, a job that passes a lease waiter of
   its tier, holding none of its keys, is placed only while that leaves at most c - 1
   attempts live in its pool.
3. Liveness after let-go: a lease waiter that found room in its pool when it was
   looked at, passed only by jobs of its own tier, whose lease is then let go, and
   which is first in the next pass's order, is not held there for a count
   (`fleet-full`, `slot-kept`, `behind-older-job`). A job of another tier keeps no
   slot for it, as for any waiter (C-6.9: tiers never hold each other); the first
   counterexample Hypothesis found was that, a `standard` job taking the last slot
   past a `trivial` lease waiter. "Room" counts what `evaluate` counts against the
   fleet cap: a detached pool's attempts and its admission probes (C-11.4). The
   property is stated with no probe running in a detached pool: the kept slot counts
   attempts only, so a probe can hold the slot kept for an older waiter, any waiter,
   until it ends (`probe_timeout_s`); that gap predates this change.
4. Confined to lease waits: with none in the queue, a pass is outcome for outcome the
   pass of the rule before (`lease_holdback=True`).
5. Intended, and pinned by an example: the new rule does not place a superset of the
   old rule's jobs. A job no longer held behind a lease waiter can turn out to be a
   slot waiter itself, and it then holds back its own later competitors (C-6.9), which
   the lease waiter's hold had masked.

The daemon is checked against the model in `tests/fake/test_admission_lease_waiters.py`.
"""

from __future__ import annotations

import copy
from datetime import timedelta

from hypothesis import HealthCheck, assume, event, given, settings, strategies as st

from subfleet import scheduler
from tests.admission_model import _live, _pool_cap, _tier, run_pass
from tests.routing_strategies import BASE_POLICY, NOW, policies, route_jobs, stamp, stores, view_of

SETTINGS = settings(max_examples=300, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                                           HealthCheck.filter_too_much])
KEYS = ("conversation:shared", "worktree:/checkout", "out:/out.md", "native:codex:session")
LOOKS = ("clock", "fence", "transaction")
HELD_FOR_A_COUNT = ("fleet-full", "slot-kept", "behind-older-job")


@st.composite
def queues(draw, *, leases: bool = True):
    """A pass in a pool that usually has a count cap (C-6.9 holds back only then): a
    store, and a queue of jobs, some waiting for leases and some holding them."""
    policy = draw(policies())
    policy["caps"]["max_active_attempts"] = draw(st.sampled_from([None, 1, 2, 3, 6]))
    policy["conversations"]["max_active_turns"] = draw(st.sampled_from([None, 1, 2, 3]))
    store = draw(stores())
    count = draw(st.integers(2, 8))
    minutes = draw(st.lists(st.integers(0, 59), min_size=count, max_size=count, unique=True))
    jobs = []
    for index, minute in enumerate(minutes):
        job = draw(route_jobs(store))
        job.update(job_id=f"q-{index}", created_at=f"2026-09-26T11:{minute:02d}:00Z", caller_session=None,
                   parent_job_id=None)
        if leases and draw(st.integers(0, 2)) == 0:
            job["lease_wait"] = frozenset(draw(st.lists(st.sampled_from(KEYS), min_size=1, max_size=2)))
            job["lease_look"] = draw(st.sampled_from(LOOKS))
        others = [key for key in KEYS if key not in job.get("lease_wait", ())]   # never a key it waits for
        if leases and draw(st.integers(0, 5)) == 0:
            job["own_leases"] = frozenset(draw(st.lists(st.sampled_from(others), min_size=1, max_size=1)))
        jobs.append(job)
    return policy, store, jobs


def _pool(job: dict) -> str:
    return "turn" if job.get("kind") == "turn" else "detached"


def _waits_on_a_lease(policy: dict, view: dict, job: dict, outcome) -> bool:
    """Whether the pass left `job` waiting for a lease and among the waiters: held
    `lease-held`, unless, looked at on its clock, no lane could ever admit it (C-11.8)."""
    return outcome.hold == "lease-held" and not (job.get("lease_look") == "clock"
                                                 and scheduler.unadmittable(policy, view, job))


# --- 1: no job waits behind a job waiting for a lease -----------------------------------------

@SETTINGS
@given(queues())
def test_no_job_is_held_behind_a_job_waiting_for_a_lease(case):
    policy, store, jobs = case
    view = view_of(store, NOW)
    result = run_pass(policy, view, jobs)
    before = run_pass(policy, view, jobs, lease_holdback=True)
    outcomes = result.by_job()
    for row in result.outcomes:
        if row.hold == "behind-older-job":
            assert outcomes[row.waits_for].hold != "lease-held", (
                f"{row.job_id} is held behind {row.waits_for}, which waits for {outcomes[row.waits_for].leases}")
    # Not vacuous: how often the rule before did hold a job so.
    held_before = [row for row in before.outcomes if row.hold == "behind-older-job"
                   and before.by_job()[row.waits_for].hold == "lease-held"]
    event(f"held behind a lease waiter by the rule before: {min(len(held_before), 2)}"
          f"{'+' if len(held_before) >= 2 else ''}")


# --- 2: the last slot is kept for a lease waiter ----------------------------------------------

@SETTINGS
@given(queues())
def test_a_job_that_passes_a_lease_waiter_keeps_the_last_slot_for_it(case):
    policy, store, jobs = case
    view = view_of(store, NOW)
    result = run_pass(policy, view, jobs)
    by_id = {job["job_id"]: job for job in jobs}
    live = {"turn": _live(view, True), "detached": _live(view, False)}
    lease_waiters: dict[str, list[dict]] = {}
    checked = 0
    for row in result.outcomes:
        job = by_id[row.job_id]
        tier, pool = _tier(policy, job), _pool(job)
        if _waits_on_a_lease(policy, view, job, row):
            lease_waiters.setdefault(tier, []).append(job)
        if not row.placed:
            continue
        live[pool] += 1
        cap = _pool_cap(policy, job)
        own = frozenset(job.get("own_leases") or ())
        passed = [older for older in lease_waiters.get(tier, ()) if not frozenset(older["lease_wait"]) & own]
        if cap is not None and passed:
            checked += 1
            assert live[pool] <= cap - 1, (
                f"{row.job_id} passed {passed[0]['job_id']}, which waits for a lease, and left {live[pool]} of "
                f"{cap} live: no slot kept")
    event(f"placements past a lease waiter in a capped pool: {min(checked, 2)}{'+' if checked >= 2 else ''}")


# --- 3: the waiter starts once its lease is let go ---------------------------------------------

@SETTINGS
@given(queues(), st.data())
def test_a_lease_waiter_is_held_for_no_count_once_its_lease_is_let_go(case, data):
    policy, store, jobs = case
    view = view_of(store, NOW)
    first = run_pass(policy, view, jobs)
    by_id = {job["job_id"]: job for job in jobs}
    live = {"turn": _live(view, True), "detached": _live(view, False) + view.get("reserved_probes", 0)}
    candidates = []
    for row in first.outcomes:
        job = by_id[row.job_id]
        if _waits_on_a_lease(policy, view, job, row) and _pool_cap(policy, job) is not None \
                and live[_pool(job)] < _pool_cap(policy, job) \
                and (_pool(job) == "turn" or not view.get("reserved_probes")):
            candidates.append(row.job_id)                   # it found room when it was looked at
        if row.placed:
            live[_pool(job)] += 1
    assume(candidates)
    waiter = data.draw(st.sampled_from(candidates))
    keys = frozenset(by_id[waiter]["lease_wait"])
    # A job holding one of its keys keeps no slot for it, and is what it waits for.
    assume(not any(frozenset(by_id[row.job_id].get("own_leases") or ()) & keys for row in first.placed))
    after = [row.job_id for row in first.outcomes[[row.job_id for row in first.outcomes].index(waiter) + 1:]
             if row.placed]
    tier, pool = _tier(policy, by_id[waiter]), _pool(by_id[waiter])
    assume(all(_tier(policy, by_id[job_id]) == tier for job_id in after if _pool(by_id[job_id]) == pool))
    rest = [job for job in jobs if job["job_id"] not in {row.job_id for row in first.placed}]
    rest = [{**job, "lease_wait": None} if job["job_id"] == waiter else job for job in rest]
    order = scheduler.ordered_jobs(policy, rest, None)
    assume(order[0]["job_id"] == waiter)
    second = run_pass(policy, first.view, rest).by_job()[waiter]
    event(f"after let-go: {second.hold or 'placed'}")
    assert second.hold not in HELD_FOR_A_COUNT, (waiter, second.hold, second.waits_for)


# --- 4: nothing changes for a queue with no lease wait -------------------------------------------

@SETTINGS
@given(queues(leases=False))
def test_with_no_lease_wait_the_pass_is_the_rule_before(case):
    policy, store, jobs = case
    view = view_of(store, NOW)
    now = [(row.job_id, row.placed, row.hold, row.waits_for) for row in run_pass(policy, view, jobs).outcomes]
    before = [(row.job_id, row.placed, row.hold, row.waits_for)
              for row in run_pass(policy, view, jobs, lease_holdback=True).outcomes]
    assert now == before


# --- examples ----------------------------------------------------------------------------------

LANE = {"provider": "codex", "credential_kind": "home", "credential_epoch": 1, "owner": "v2", "enabled": 1,
        "plan": None, "identity": None, "identity_status": None, "desktop": 0,
        "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z"}
JOB = {"kind": "turn", "sandbox": "read-only", "task": None, "tier": None, "pinned_lane": None, "allow_desktop": 0,
       "exclusions": (), "parent_job_id": None, "policy_hash": "fixture", "unmeasured_reserve_reason": None,
       "caller_session": None}


def _world(*closed: str) -> tuple[dict, dict]:
    """Three measured Codex lanes, as the fake daemon's; `closed` are closed for an hour."""
    lanes = [{**LANE, "lane_id": lane_id, "account_key": f"codex:{lane_id}", "credential_ref": f"/h/{lane_id}",
              "home": f"/h/{lane_id}", "label": lane_id} for lane_id in ("codex-1", "codex-2", "codex-3")]
    readings = [{"reading_id": n + 1, "lane_id": lane["lane_id"], "scope": "account", "window": "seven_day",
                 "utilization": .2, "resets_at": stamp(NOW + timedelta(days=3)), "label": "provider",
                 "source": "fixture", "observed_at": stamp(NOW), "attempt_id": None} for n, lane in enumerate(lanes)]
    closures = [{"closure_id": n + 1, "lane_id": lane_id, "scope": "account",
                 "until_at": stamp(NOW + timedelta(hours=1)), "reason": "provider-limit", "clock_source": "reported",
                 "source_event": "fixture", "created_at": stamp(NOW), "released_at": None}
                for n, lane_id in enumerate(closed)]
    store = {"lanes": lanes, "readings": readings, "closures": closures, "jobs": [], "attempts": [],
             "unavailable": {}, "overridden": set(), "desktop_in_use": False}
    policy = copy.deepcopy(BASE_POLICY)
    return policy, view_of(store, NOW)


def test_the_measured_shape_a_fenced_turn_holds_no_later_turn_and_keeps_the_last_slot():
    """The review's probe as a pass: a turn cap of 5, an older turn on retention's fence
    and four later turns of its model. The rule before held all four
    `behind-older-job`; now four places allow at most 4 live, so three are placed and
    the fourth is held `slot-kept` for the fenced turn."""
    policy, view = _world()
    policy["conversations"]["max_active_turns"] = 5
    older = {**JOB, "job_id": "older", "created_at": "2026-09-26T11:00:00Z", "pinned_model": "astra",
             "lease_wait": frozenset({"worktree:/tree"}), "lease_look": "fence"}
    later = [{**JOB, "job_id": f"later-{n}", "created_at": f"2026-09-26T11:0{n + 1}:00Z", "pinned_model": "astra"}
             for n in range(4)]
    before = run_pass(policy, view, [older, *later], lease_holdback=True).by_job()
    now = run_pass(policy, view, [older, *later]).by_job()
    assert [before[job["job_id"]].hold for job in later] == ["behind-older-job"] * 4
    assert [bool(now[job["job_id"]].placed) for job in later] == [True] * 4
    assert now["older"].hold == "lease-held"
    policy["conversations"]["max_active_turns"] = 4
    tight = run_pass(policy, view, [older, *later]).by_job()
    assert [bool(tight[job["job_id"]].placed) for job in later] == [True, True, True, False]
    assert (tight["later-3"].hold, tight["later-3"].waits_for) == ("slot-kept", "older")


def test_intended_a_waiter_the_lease_hold_masked_now_holds_its_own_competitors():
    """Property 5, intended. A fenced Astra turn, then a turn whose chain holds Astra and
    Terra pinned to a closed lane, then a Terra turn. The rule before held the second
    behind the first (they compete on Astra), so it was no waiter, and the Terra turn,
    which does not compete with the fenced one, was placed. Now the second is looked at,
    waits for its closed lane, and as an older slot waiter holds the Terra turn, which
    could take a slot it needs (C-6.9). So the new rule places a job the old one did
    not hold, and holds one the old one placed: not a superset, by design."""
    policy, view = _world("codex-1")
    policy["conversations"]["max_active_turns"] = 3
    policy["chains"]["sweep"] = ["terra", "terra", "terra", "astra"]
    fenced = {**JOB, "job_id": "fenced", "created_at": "2026-09-26T11:00:00Z", "pinned_model": "astra",
              "lease_wait": frozenset({"worktree:/tree"}), "lease_look": "fence"}
    closed = {**JOB, "job_id": "closed", "created_at": "2026-09-26T11:01:00Z", "pinned_model": None,
              "task": "sweep", "tier": "standard", "pinned_lane": "codex-1"}
    terra = {**JOB, "job_id": "terra", "created_at": "2026-09-26T11:02:00Z", "pinned_model": "terra"}
    assert scheduler.competes(scheduler.demand_models(policy, fenced), scheduler.demand_models(policy, closed))
    assert not scheduler.competes(scheduler.demand_models(policy, fenced), scheduler.demand_models(policy, terra))
    before = run_pass(policy, view, [fenced, closed, terra], lease_holdback=True).by_job()
    now = run_pass(policy, view, [fenced, closed, terra]).by_job()
    assert (before["closed"].hold, before["closed"].waits_for) == ("behind-older-job", "fenced")
    assert before["terra"].placed
    assert now["closed"].hold.startswith("closed")
    assert (now["terra"].hold, now["terra"].waits_for) == ("behind-older-job", "closed")
