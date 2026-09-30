"""C-11.8, C-6.9: a job pinned to a lane that can never admit it holds nobody back.

Incident, 2026-09-29 ~21:53Z: job 20260929-134236-salvage-r3-fix was submitted
with `-a claude-9`. An account switch made claude-9 the desktop login lane;
`subfleet why` said "no lane admits it (desktop)", the job stayed queued with no
word to its caller, and C-6.9's FIFO held 38 younger standard Opus jobs behind
it while claude-7 was open.

Each property is stated for every input the strategies draw (the lane states,
closures, latched credentials and pins of `tests/routing_strategies.py`, plus
closures far out and at the horizon), over `scheduler` itself and the pure pass
model in `tests/admission_model.py`. The oracle is written apart from the code
it checks: a job no lane can admit is one `evaluate` places nowhere even in the
best capacity there could be (every count cap lifted, every lane measured idle,
no attempt or probe in flight, every closure that ends within the horizon
ended). Nothing here reads `pin_unadmittable` to decide what it should say.

1. Sound: a pin it calls unadmittable is placed nowhere by `evaluate`, in the
   view at hand and in the best capacity there could be.
2. Exact: a pinned job it does not call unadmittable is placed on its lane in
   that best capacity: nothing it leaves out is standing.
3. `refused_for_good` agrees with the oracle for every job, pinned or not.
4. The property asked for: for any queue with arbitrary pins and lane states, no
   job waits behind a job no lane can admit, neither held behind it nor kept
   out of a slot kept for it.
5. Monotone in the horizon: a closure that makes a pin unadmittable at one
   horizon does at every shorter one.
"""

from __future__ import annotations

import copy
from datetime import timedelta

from hypothesis import HealthCheck, assume, event, given, settings, strategies as st

from subfleet import scheduler
from subfleet.capacity import _time, credential_gone, credential_latched
from subfleet.policy import admission_settings
from tests.admission_model import run_pass
from tests.routing_strategies import (BASE_POLICY, LANE_IDS, NOW, SCOPES, policies, route_jobs, stamp, stores,
                                      view_of)

SETTINGS = settings(max_examples=600, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                                           HealthCheck.filter_too_much])
FAR = admission_settings(BASE_POLICY)["pin_hold_far_s"]
RAISES = (ValueError, KeyError, TypeError, AttributeError, IndexError)
COUNT_CAPS = ("max_active_attempts", "max_in_flight_per_lane", "max_in_flight_unmeasured",
              "max_active_attempts_per_parent")


@st.composite
def pin_stores(draw) -> dict:
    """A store with closures near, at and past the horizon, as well as the usual ones."""
    store = draw(stores())
    lane_ids = [row["lane_id"] for row in store["lanes"]]
    # What the last probe found of each lane's credential, and its latch as a view marks
    # it (`Timers.enrich_view`): revoked or missing is a person's to fix; an expired
    # token the timers heal (C-23.47).
    for lane in store["lanes"]:
        lane["probe_status"] = draw(st.sampled_from([None] * 6 + ["ok", "revoked", "auth-revoked", "no-auth",
                                                                 "expired-token", "expired-token"]))
        if draw(st.integers(0, 9)) == 0:
            lane["revoked_epoch"] = 1
        if lane["probe_status"] == "expired-token" and draw(st.booleans()):
            lane["heal_spent"] = True               # the epoch's heal ran and left it expired
        if credential_latched(lane):
            store["unavailable"][lane["lane_id"]] = "credential-latched"
        elif store["unavailable"].get(lane["lane_id"]) == "credential-latched":
            del store["unavailable"][lane["lane_id"]]
    for n in range(draw(st.integers(0, 3))):
        seconds = draw(st.sampled_from([FAR - 1, FAR, FAR + 1, FAR + 3600, 30 * 86400,
                                        (_time("2099-12-31T00:00:00Z") - NOW).total_seconds()]))
        store["closures"].append({
            "closure_id": 100 + n, "lane_id": draw(st.sampled_from(lane_ids)),
            "scope": draw(st.sampled_from(SCOPES[:5] + ("gpt-6-astra", "gpt-6-terra"))),
            "until_at": stamp(NOW + timedelta(seconds=seconds)),
            "reason": draw(st.sampled_from(["operator-hold", "provider-limit", "auth-dead", "credits"])),
            "clock_source": "reported", "source_event": "fixture", "created_at": stamp(NOW),
            "released_at": draw(st.sampled_from([None, None, None, stamp(NOW)]))})
    return store


@st.composite
def pinned_jobs(draw, store: dict) -> dict:
    """A job with a lane pin: a lane id, an account name, or a name no lane answers to."""
    for _ in range(20):
        job = draw(route_jobs(store))
        if job["pinned_lane"]:
            return job
    job["pinned_lane"] = draw(st.sampled_from([row["lane_id"] for row in store["lanes"]] + ["claude-99"]))
    return job


def uncapped(policy: dict) -> dict:
    policy = copy.deepcopy(policy)
    policy["caps"].update(dict.fromkeys(COUNT_CAPS))
    policy.setdefault("conversations", {}).update(max_active_turns=None, turn_slots_per_lane=None)
    return policy


def best(store: dict, policy: dict) -> dict:
    """The best capacity the store's standing facts allow, as `Daemon._pick` would view it.

    Every lane measured idle by one fresh complete usage read of its account
    (so no floor, and no reserved window: all of it is slack), nothing in flight,
    no probe holding a lane, every closure that ends within the horizon ended, and
    every expired token healed. What is left is what a person must change: the
    lane rows, far closures, and credentials revoked or missing."""
    far = admission_settings(policy)["pin_hold_far_s"]
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
                          if row["released_at"] or (_time(row["until_at"]) - NOW).total_seconds() > far]
    return view_of(better, NOW)


def admissible(policy: dict, store: dict, job: dict) -> bool | None:
    """The oracle: does any lane take `job` in the best capacity? None when its route raises."""
    try:
        return scheduler.evaluate(uncapped(policy), best(store, policy), job).chosen_lane is not None
    except RAISES:
        return None


# --- 1, 2: pin_unadmittable is sound and exact ------------------------------------------------

@SETTINGS
@given(st.data())
def test_c11_8_a_pin_called_unadmittable_is_placed_nowhere_even_in_the_best_capacity(data):
    policy, store = data.draw(policies()), data.draw(pin_stores())
    job = data.draw(pinned_jobs(store))
    stuck = scheduler.pin_unadmittable(policy, view_of(store, NOW), job)
    assume(stuck is not None)
    assert stuck["reasons"], stuck
    assert admissible(policy, store, job) is False, stuck
    try:
        decision = scheduler.evaluate(policy, view_of(store, NOW), job)
    except RAISES:
        return
    assert decision.chosen_lane is None


@SETTINGS
@given(st.data())
def test_c11_8_a_pin_not_called_unadmittable_is_placed_on_its_lane_in_the_best_capacity(data):
    policy, store = data.draw(policies()), data.draw(pin_stores())
    job = data.draw(pinned_jobs(store))
    view = view_of(store, NOW)
    try:
        scheduler.prepare(policy, view, job)
    except RAISES:
        assume(False)                               # C-6.12's: `pin_unadmittable` says None, by design
    assume(scheduler.pin_unadmittable(policy, view, job) is None)
    decision = scheduler.evaluate(uncapped(policy), best(store, policy), job)
    assert decision.chosen_lane is not None, decision.reason
    assert decision.chosen_lane == scheduler.prepare(policy, view, job)["selected"]["lane_id"]


@SETTINGS
@given(st.data())
def test_c11_8_refused_for_good_agrees_with_the_oracle_for_every_job(data):
    """3. For pinned and unpinned jobs alike: a decision with no lane is refused for
    good exactly when no lane would take the job in the best capacity."""
    policy, store = data.draw(policies()), data.draw(pin_stores())
    job = data.draw(route_jobs(store))
    try:
        decision = scheduler.evaluate(policy, view_of(store, NOW), job)
    except RAISES:
        assume(False)
    assume(decision.chosen_lane is None)
    for_good = scheduler.refused_for_good(policy, decision, job, view_of(store, NOW)["lanes"])
    assert (for_good is not None) == (admissible(policy, store, job) is False), (for_good, decision.reason)


@SETTINGS
@given(st.data())
def test_c11_8_unadmittable_is_exactly_the_oracle_for_every_job_and_the_memo_changes_nothing(data):
    """What admission asks of a job on its clock, pinned or not, is exactly "no lane
    would take it in the best capacity"; and one pass's memo, shared by a queue of
    jobs, answers each as a fresh look would."""
    policy, store = data.draw(policies()), data.draw(pin_stores())
    view = view_of(store, NOW)
    queue = [data.draw(route_jobs(store)) for _ in range(data.draw(st.integers(1, 4)))]
    memo: dict = {}
    for job in queue:
        fresh = scheduler.unadmittable(policy, view, job)
        assert scheduler.unadmittable(policy, view, job, memo=memo) == fresh
        try:
            scheduler.prepare(policy, view, job)
        except RAISES:
            assert fresh is None                    # C-6.12's to settle
            continue
        oracle = admissible(policy, store, job)
        assert (fresh is not None) == (oracle is False), (fresh, job)


# --- 4: no job waits behind a job no lane can admit -------------------------------------------

@st.composite
def queues(draw):
    """A pass: a policy with some count cap (C-6.9 holds back only then), a store, and a
    queue of jobs, many of them lane-pinned, some to lanes no one can use."""
    policy = draw(policies())
    policy["caps"]["max_active_attempts"] = draw(st.sampled_from([1, 2, 3, 6]))
    store = draw(pin_stores())
    count = draw(st.integers(2, 8))
    minutes = draw(st.lists(st.integers(0, 59), min_size=count, max_size=count, unique=True))
    jobs = []
    for index, minute in enumerate(minutes):
        job = draw(pinned_jobs(store)) if draw(st.booleans()) else draw(route_jobs(store))
        job.update(job_id=f"q-{index}", created_at=f"2026-09-26T11:{minute:02d}:00Z", caller_session=None,
                   parent_job_id=None, tier=job.get("tier") or draw(st.sampled_from([None, "standard"])))
        jobs.append(job)
    return policy, store, jobs


@settings(max_examples=500, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much])
@given(queues())
def test_c6_9_c11_8_no_job_waits_behind_a_job_no_lane_can_admit(case):
    policy, store, jobs = case
    result = run_pass(policy, view_of(store, NOW), jobs)
    by_id = {job["job_id"]: job for job in jobs}
    waited_on = [(row.job_id, row.hold, row.waits_for) for row in result.outcomes if row.waits_for]
    stuck = sum(admissible(policy, store, job) is False for job in jobs)
    event(f"jobs no lane can admit: {min(stuck, 3)}{'+' if stuck >= 3 else ''}")
    event(f"jobs held behind or kept out for another: {min(len(waited_on), 3)}{'+' if len(waited_on) >= 3 else ''}")
    if stuck and waited_on:
        event("both in one pass")
    for job_id, hold, older in waited_on:
        assert admissible(policy, store, by_id[older]) is not False, (
            f"{job_id} is held {hold} behind {older}, which no lane can admit")
    # And every pinned job the oracle finds unadmittable is held as such, never
    # compared with anyone (approval and uncertain waits are a person's: none here).
    holds = result.by_job()
    for job in jobs:
        if job["pinned_lane"] and admissible(policy, store, job) is False \
                and scheduler.pin_unadmittable(policy, view_of(store, NOW), job) is not None:
            assert holds[job["job_id"]].hold == "pin-unadmittable"


def test_c6_9_c11_8_the_incident_38_jobs_are_not_held_behind_the_desktop_pin():
    """The 2026-09-29 shape: an Opus job pinned to the desktop lane, then 38 standard
    Opus jobs, one other Claude lane open, a count cap (the hold-back needs one)."""
    policy = copy.deepcopy(BASE_POLICY)
    policy["caps"]["max_active_attempts"] = 40
    lane = {"provider": "claude", "credential_kind": "keychain-token", "credential_epoch": 1, "home": None,
            "owner": "v2", "enabled": 1, "plan": None, "identity": None, "identity_status": None,
            "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z"}
    store = {"lanes": [{**lane, "lane_id": "claude-9", "account_key": "claude:nine@example.invalid",
                        "credential_ref": "/c/9", "label": "nine@example.invalid", "desktop": 1},
                       {**lane, "lane_id": "claude-7", "account_key": "claude:seven@example.invalid",
                        "credential_ref": "/c/7", "label": "seven@example.invalid", "desktop": 0}],
             # claude-7 was open: measured, with room (C-11.7's reserve needs a complete usage read).
             "readings": [{"reading_id": 1, "lane_id": "claude-7", "scope": "account", "window": "seven_day",
                           "utilization": 0.2, "resets_at": stamp(NOW + timedelta(days=3)), "label": "provider",
                           "source": "oauth-usage", "observed_at": stamp(NOW), "attempt_id": None}],
             "closures": [], "jobs": [], "attempts": [], "unavailable": {}, "overridden": set(),
             "desktop_in_use": True}
    base = {"kind": "dispatch", "sandbox": "read-only", "pinned_model": None, "allow_desktop": 0,
            "exclusions": (), "parent_job_id": None, "policy_hash": "fixture", "unmeasured_reserve_reason": None,
            "caller_session": None}
    jobs = [{**base, "job_id": "salvage-r3-fix", "created_at": "2026-09-29T17:42:36Z", "pinned_lane": "claude-9",
             "task": "build", "tier": "standard"}]
    jobs += [{**base, "job_id": f"opus-{n:02d}", "created_at": f"2026-09-29T21:{n:02d}:00Z", "pinned_lane": None,
              "task": "review", "tier": "standard"} for n in range(38)]
    view = view_of(store, NOW)
    assert scheduler.pin_unadmittable(policy, view, jobs[0])["reasons"] == ["desktop"]
    result = run_pass(policy, view, jobs).by_job()
    assert result["salvage-r3-fix"].hold == "pin-unadmittable"
    assert all(result[f"opus-{n:02d}"].placed == "claude-7" for n in range(38))


# --- 5: the horizon ---------------------------------------------------------------------------

@SETTINGS
@given(st.data(), st.sampled_from([3600, 86400, 3 * 86400, FAR, 30 * 86400]),
       st.sampled_from([3600, 86400, 3 * 86400, FAR, 30 * 86400]))
def test_c11_8_a_shorter_horizon_never_frees_a_pin_a_longer_one_holds(data, one, other):
    shorter, longer = sorted((one, other))
    store = data.draw(pin_stores())
    job = data.draw(pinned_jobs(store))
    view = view_of(store, NOW)
    policy = copy.deepcopy(BASE_POLICY)
    policy["admission"]["pin_hold_far_s"] = longer
    held_long = scheduler.pin_unadmittable(policy, view, job)
    policy["admission"]["pin_hold_far_s"] = shorter
    held_short = scheduler.pin_unadmittable(policy, view, job)
    if held_long is not None:
        assert held_short is not None and set(held_long["reasons"]) <= set(held_short["reasons"])


# --- examples: each reason, and what is not one ------------------------------------------------

def lane_row(lane_id: str, **changes) -> dict:
    provider = lane_id.split("-")[0]
    return {"lane_id": lane_id, "provider": provider, "account_key": f"{provider}:{lane_id}@example.invalid",
            "credential_ref": f"/c/{lane_id}", "credential_kind": "keychain-token", "credential_epoch": 1,
            "home": None, "owner": "v2", "desktop": 0, "enabled": 1, "plan": None, "identity": None,
            "label": f"{lane_id}@example.invalid", "identity_status": None,
            "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z", **changes}


def one_lane(lane: dict, *, closures=(), latched=False, in_use=True) -> dict:
    """A view of one lane; `latched` marks its credential as a view does (`credential_latched`)."""
    return view_of({"lanes": [lane], "readings": [], "closures": list(closures), "jobs": [], "attempts": [],
                    "unavailable": {lane["lane_id"]: "credential-latched"} if latched else {},
                    "overridden": set(), "desktop_in_use": in_use}, NOW)


def pinned(lane_id: str = "claude-9", **changes) -> dict:
    return {"job_id": "j", "kind": "dispatch", "sandbox": "read-only", "task": "review", "tier": "standard",
            "pinned_model": None, "pinned_lane": lane_id, "allow_desktop": 0, "exclusions": (),
            "parent_job_id": None, "policy_hash": "fixture", "unmeasured_reserve_reason": None, **changes}


def closure(until_s: float, scope: str = "account", reason: str = "operator-hold") -> dict:
    return {"closure_id": 1, "lane_id": "claude-9", "scope": scope, "until_at": stamp(NOW + timedelta(seconds=until_s)),
            "reason": reason, "clock_source": "reported", "source_event": "fixture", "created_at": stamp(NOW),
            "released_at": None}


def reasons(view: dict, job: dict | None = None, policy: dict = BASE_POLICY) -> list[str] | None:
    found = scheduler.pin_unadmittable(policy, view, job or pinned())
    return None if found is None else found["reasons"]


def test_c11_8_each_standing_refusal_is_named():
    assert reasons(one_lane(lane_row("claude-9", desktop=1))) == ["desktop"]
    assert reasons(one_lane(lane_row("claude-9", enabled=0))) == ["disabled"]
    assert reasons(one_lane(lane_row("claude-9", owner="v1"))) == ["owner-v1"]
    assert reasons(one_lane(lane_row("claude-9", identity_status="mismatch"))) == ["identity-mismatch"]
    for status in ("revoked", "auth-revoked", "no-auth"):
        assert reasons(one_lane(lane_row("claude-9", probe_status=status), latched=True)) == ["credential-latched"]
    assert reasons(one_lane(lane_row("claude-9", revoked_epoch=3), latched=True)) == ["credential-latched"]
    # Once the epoch's heal has run and left it expired, only a new login ends it.
    assert reasons(one_lane(lane_row("claude-9", probe_status="expired-token", heal_spent=True),
                            latched=True)) == ["credential-latched"]
    assert reasons(one_lane(lane_row("claude-9")), pinned(exclusions=("claude-9",))) == ["excluded"]
    assert reasons(one_lane(lane_row("claude-9")), pinned("claude-99")) == ["unknown"]
    held = closure(10 ** 9)
    assert reasons(one_lane(lane_row("claude-9"), closures=[held])) == [f"closed:account:{held['until_at']}"]
    turn = pinned(kind="turn", task=None, tier=None, pinned_model="opus")
    assert reasons(one_lane(lane_row("claude-9", credential_kind="home")), turn) == ["config-dir"]


def test_c11_8_what_a_wait_ends_is_not_standing():
    """Capacity comes back by itself: a slot, a reading, the floor, a closure within the horizon."""
    assert reasons(one_lane(lane_row("claude-9"))) is None
    assert reasons(one_lane(lane_row("claude-9"), closures=[closure(FAR)])) is None             # at the horizon: a wait
    assert reasons(one_lane(lane_row("claude-9"), closures=[closure(3 * 86400, "claude-opus-5-5",
                                                                     "provider-limit")])) is None
    assert reasons(one_lane(lane_row("claude-9", desktop=1), in_use=False)) is None          # not in use: a candidate
    # An expired token latches the slot, but the heal the timers have yet to try may renew it (C-23.47).
    assert reasons(one_lane(lane_row("claude-9", probe_status="expired-token"), latched=True)) is None
    assert reasons(one_lane(lane_row("claude-9", desktop=1)), pinned(allow_desktop=1)) is None
    assert reasons(one_lane(lane_row("claude-9")), pinned(pinned_lane=None)) is None          # no pin, nothing to say
    # A closure for a model the job does not run is not its lane's refusal.
    assert reasons(one_lane(lane_row("claude-9"), closures=[closure(10 ** 9, "claude-fable-5-1")])) is None


def test_c11_8_a_pin_a_reenrolment_moved_follows_it():
    """C-11.2: a disabled lane whose credential an enabled lane now holds resolves to that
    lane, so its pin is judged there and is not stuck for being disabled."""
    old = lane_row("claude-9", enabled=0)
    new = lane_row("claude-12", credential_ref=old["credential_ref"])
    view = view_of({"lanes": [old, new], "readings": [], "closures": [], "jobs": [], "attempts": [],
                    "unavailable": {}, "overridden": set(), "desktop_in_use": True}, NOW)
    assert scheduler.pin_unadmittable(BASE_POLICY, view, pinned()) is None


def test_c11_8_a_route_that_cannot_be_evaluated_is_c6_12s_not_this():
    """A pin two lanes answer to, or a lane of the other provider, raises in `prepare`:
    C-6.12 refuses or defers it, and `pin_unadmittable` says nothing."""
    view = one_lane(lane_row("claude-9"))
    assert scheduler.pin_unadmittable(BASE_POLICY, view, pinned(pinned_model="astra")) is None
    both = view_of({"lanes": [lane_row("claude-9", label="x@example.invalid"),
                              lane_row("claude-8", label="x@example.invalid")],
                    "readings": [], "closures": [], "jobs": [], "attempts": [], "unavailable": {},
                    "overridden": set(), "desktop_in_use": True}, NOW)
    assert scheduler.pin_unadmittable(BASE_POLICY, both, pinned("x@example.invalid")) is None


def test_c11_8_the_notice_and_the_failure_say_what_and_what_to_do():
    from subfleet import render
    held = closure(10 ** 9)
    stuck = scheduler.pin_unadmittable(BASE_POLICY, one_lane(lane_row("claude-9", desktop=1), closures=[held]),
                                       pinned())
    notice = render.pin_notice("job-1", stuck, "2026-09-30T12:30:00Z")
    assert notice.startswith("job-1: waiting; its pinned lane claude-9 can never admit it: ")
    assert "Claude desktop app's login" in notice and f"until {held['until_at']}" in notice
    assert "(operator-hold)" in notice
    assert "resubmit it unpinned, or pinned to another lane" in notice and "subfleet kill job-1" in notice
    assert "fails with rc 3 at 2026-09-30T12:30:00Z" in notice
    assert "waits until that changes" in render.pin_notice("job-1", stuck, None)
    failure = render.pin_failure(stuck, "2026-09-30T12:00:00Z")
    assert failure.startswith("no lane: its pinned lane claude-9 could never admit it from 2026-09-30T12:00:00Z on")
    assert "resubmit it unpinned" in failure
    spent = render.pin_refusals({"lane_id": "codex-1", "reasons": ["credential-latched"], "probe_status": "expired-token"})
    assert "heal the timers allow for it ran" in spent and "only a new login" in spent
    for reason in (*scheduler.STANDING_REFUSALS, "credential-latched", "unknown", "no-lanes"):
        text = render.pin_refusals({"lane_id": "claude-9", "reasons": [reason]})
        assert reason not in ("desktop",) or "desktop" in text
        assert text and "{" not in text
