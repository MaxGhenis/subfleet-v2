"""C-18.7 invariants, stated over every input Hypothesis can build.

- Detection never calls a clock unstarted when any fresh account-wide window
  shows use, nor without the weekly window read, nor on long-window evidence
  that is stale, unread, or not the provider's.
- No touch runs on a clock work reached after the plan read it idle.
- Whatever holds, closes or occupies a lane (an operator's hold, including one
  only its `lane.held` event still shows, an `auth-dead` or other closure on
  the account or the touch model, a disabled, desktop or v1 lane, a job's or
  a probe's lease, a running attempt), no touch turn runs on it: not from a
  probe cycle, not from an operator naming it. No touch creates a job, redeems
  a reset credit, or leaves a lease or a holder behind; and a free, unstarted
  lane is touched, so the property is not vacuous.
- Automatic touches of one lane that reach a verdict start at least
  `timers.touch_spacing_s` apart, whatever the turns return.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import shutil
import tempfile
from unittest import mock

from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import capacity
from subfleet.contracts import (CLOCK_TOUCH_MODEL_ID, CLOCK_UNSTARTED_TOLERANCE_S, ClockSource, Closure,
                                ClosureReason, Credential, Lane, LaneOwner, Outcome, OutcomeClass)
from subfleet.store import Store
from subfleet.timers import Timers, iso
from tests.unit.test_timers_touch import Clock, Wham, hold, touches

SPACING = 3600
PROPERTY = settings(max_examples=40, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


# --- detection: nonzero use is never an unstarted clock --------------------------

WINDOWS = st.sampled_from(["five_hour", "seven_day", "1440", "4320", "60"])
READING = st.fixed_dictionaries({
    "window": WINDOWS,
    "scope": st.sampled_from(["account", "account", CLOCK_TOUCH_MODEL_ID]),
    "utilization": st.sampled_from([0.0, 0.0, 0.0, 1e-6, .01, .5, 1.0]),
    "label": st.sampled_from(["provider", "provider", "stale-provider", "admission-observed"]),
    "age_s": st.integers(0, 300),
    "offset_s": st.integers(-3 * 3600, 3 * 3600) | st.integers(-CLOCK_UNSTARTED_TOLERANCE_S, CLOCK_UNSTARTED_TOLERANCE_S),
    "resets": st.booleans() | st.just(True),
})


def as_row(now, spec):
    observed = now - timedelta(seconds=spec["age_s"])
    length = capacity.window_seconds(spec["window"])
    return {"lane_id": "codex-4", "scope": spec["scope"], "window": spec["window"],
            "utilization": spec["utilization"], "label": spec["label"], "source": "wham",
            "observed_at": iso(observed),
            "resets_at": iso(observed + timedelta(seconds=length + spec["offset_s"])) if spec["resets"] else None}


def disqualified(rows, now):
    """A reading that rules an unstarted clock out whatever else is read: fresh account-wide use
    in any window, or an account-wide long window that is not fresh provider evidence at 0%
    sliding a window ahead."""
    for row in rows:
        if row["scope"] != "account":
            continue
        fresh = capacity.fresh_provider(row, now=now)
        if fresh and row["utilization"] != 0:
            return True
        if capacity.window_seconds(row["window"]) >= 86400:
            if not fresh or not row["resets_at"] or row["utilization"] != 0:
                return True
            length = capacity.window_seconds(row["window"])
            drift = (capacity._time(row["resets_at"]) - capacity._time(row["observed_at"])).total_seconds() - length
            if abs(drift) > CLOCK_UNSTARTED_TOLERANCE_S:
                return True
    return False


@settings(max_examples=500, deadline=None)
@given(specs=st.lists(READING, max_size=6), extra=READING)
def test_property_detection_is_false_on_any_lane_with_nonzero_use(specs, extra):
    """C-18.7: an unstarted verdict needs the account-wide weekly window read, it and every
    other account-wide long window fresh from the provider at exactly 0% with a reset sliding
    a window ahead, and no fresh account-wide use in any window. The verdict names the weekly
    window. Once a reading disqualifies the lane, no further reading makes it unstarted (a
    lane not yet disqualified, a lone day-long window, say, can become so)."""
    now = Clock()()
    rows = [as_row(now, spec) for spec in specs]
    found = capacity.clock_unstarted(rows, now=now)
    weekly = capacity.long_windows(rows)
    if found is not None:
        assert capacity.window_seconds(found["window"]) == capacity.WEEK_S
        assert not disqualified(rows, now)
        for row in weekly:
            assert row["utilization"] == 0 and row["label"] == "provider" and row["resets_at"]
            assert capacity.fresh_provider(row, now=now)
    else:
        assert disqualified(rows, now) or not any(
            capacity.window_seconds(row["window"]) == capacity.WEEK_S for row in weekly)
    used = any(row["scope"] == "account" and row["utilization"] != 0 and capacity.fresh_provider(row, now=now)
               for row in rows)
    assert not (used and found is not None)
    if disqualified(rows, now):
        assert capacity.clock_unstarted([*rows, as_row(now, extra)], now=now) is None


# --- a rig per example (no function-scoped fixture under @given) -------------------

class CreditWham(Wham):
    """The usage endpoint, recording any reset credit someone tries to redeem."""

    def __init__(self, clock):
        super().__init__(clock)
        self.consumed = []

    def consume_reset_credit(self, lane, credit, redeem_request_id, credential_env=None, *, timeout=None):
        self.consumed.append(lane.lane_id)
        raise AssertionError("a touch must never redeem a reset credit")


@contextmanager
def make_rig(*, outcomes=None):
    base = Path(tempfile.mkdtemp(prefix="touch-prop-"))
    clock = Clock()
    wham = CreditWham(clock)
    turns = []
    policy = {"models": {"luna": {"provider": "codex", "id": CLOCK_TOUCH_MODEL_ID}},
              "timers": {"probe_interval_s": 60, "keepalive_interval_s": 18300, "touch_spacing_s": SPACING},
              "reset_credits": {"enabled": False}, "alerts": {}, "caps": {}}

    def turn(lane, purpose, holder, *, cancel, deadline):
        lease = store.one("SELECT holder FROM leases WHERE lease_key=?", (f"lane:{lane.lane_id}:slot:0",))
        assert lease and lease["holder"] == holder                # a touch runs only under its own lease
        turns.append((lane.lane_id, purpose, iso(clock())))
        kind = (outcomes or {}).get(lane.lane_id, lambda: "ok")()
        evidence = {"requested_at": iso(clock()), "rc": 0}
        if kind == "ok":
            wham.start(lane.lane_id)
            return Outcome(OutcomeClass.OK, "OK", evidence=evidence)
        if kind == "ineffective":                                  # reached the provider, started nothing
            return Outcome(OutcomeClass.OK, "OK", evidence=evidence)
        if kind == "timeout":
            return Outcome(OutcomeClass.UNKNOWN, "timed out", evidence={**evidence, "timed_out": True})
        return Outcome(OutcomeClass.UNKNOWN, "transport error", evidence=evidence)

    try:
        with mock.patch.object(Path, "home", lambda: base), Store(base / "state.sqlite3") as store:
            timer = Timers(store, base, policy, adapter_factory=lambda _: wham, now=clock, turn=turn,
                           deliver=lambda notice: True)

            def enroll(identity, *, enabled=True, owner=LaneOwner.V2, desktop=False):
                home = base / identity
                home.mkdir(exist_ok=True)
                (home / "auth.json").write_text(json.dumps({"last_refresh": "first"}))
                lane = Lane(identity, "codex", "codex:" + identity, Credential("codex", str(home), "home"),
                            str(home), owner, desktop, enabled)
                store.put_lane(lane)
                return lane

            try:
                yield timer, store, clock, wham, enroll, turns
            finally:
                timer.stop()
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- no touch on a held, closed or occupied lane -----------------------------------

#: What may hold, close or occupy a lane; `free` holds nothing.
CONDITIONS = ("free", "operator-hold", "hidden-hold", "auth-dead", "account-limit", "touch-model-limit",
              "disabled", "desktop", "owner-v1", "job-lease", "probe-lease", "running-attempt")


def impose(store, clock, wham, enroll, lane_id, condition):
    """Put `lane_id` in `condition`, the way the daemon records each (returns the lane). A
    lane at its account limit reads `limited`: a probe that reads it `ok` below 100%
    releases the limit, as it should (`Timers._persist`)."""
    lane = enroll(lane_id, enabled=condition != "disabled", desktop=condition == "desktop",
                  owner=LaneOwner.V1 if condition == "owner-v1" else LaneOwner.V2)
    later = clock() + timedelta(days=1)                 # outlasts every clock step below
    if condition == "operator-hold":
        hold(store, lane_id, later)
    elif condition == "hidden-hold":
        # A hold under a later provider limit leaves no row; the limit's release leaves none.
        store.add_closure(Closure(lane_id, "account", iso(clock() + timedelta(days=2)),
                                  ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "wham"))
        hold(store, lane_id, later)
        with store.transaction("closure.reset") as tx:
            tx.execute("UPDATE closures SET released_at=? WHERE lane_id=?", (iso(clock()), lane_id))
    elif condition in ("auth-dead", "account-limit", "touch-model-limit"):
        scope = CLOCK_TOUCH_MODEL_ID if condition == "touch-model-limit" else "account"
        reason = ClosureReason.AUTH_DEAD if condition == "auth-dead" else ClosureReason.PROVIDER_LIMIT
        store.add_closure(Closure(lane_id, scope, iso(later), reason, ClockSource.REPORTED, "wham"))
        if condition == "account-limit":
            wham.overrides[lane_id] = {"status": "limited", "limit_reached": True, "allowed": False,
                                       "account_key": lane.account_key, "readings": ()}
    elif condition == "job-lease":
        store.acquire_lease(f"lane:{lane_id}:slot:1", f"job-{lane_id}/a1")
    elif condition == "probe-lease":
        store.acquire_lease(f"lane:{lane_id}:slot:0", f"probe:admission:{lane_id}")
    elif condition == "running-attempt":
        store.add_job(job_id=f"job-{lane_id}", request_id=f"request-{lane_id}", payload_digest="digest",
                      kind="dispatch", state="running", workdir="/work", prompt_path="/prompt", sandbox="read-only")
        store.add_attempt(attempt_id=f"job-{lane_id}/a1", job_id=f"job-{lane_id}", seq=1, lane_id=lane_id,
                          model_requested="gpt-6-astra", state="running", started_at=iso(clock() - timedelta(days=1)))
    return lane


@PROPERTY
@given(conditions=st.lists(st.sampled_from(CONDITIONS), min_size=1, max_size=5))
def test_property_no_touch_ever_runs_on_a_held_closed_or_occupied_lane(conditions):
    """C-18.7, C-9.6, C-18.3, C-8.4: for any mix of lanes, a cycle and an operator naming
    each lane touch only the free ones; nothing creates a job, redeems a credit, or
    leaves a touch's lease or holder."""
    with make_rig() as (timer, store, clock, wham, enroll, turns):
        lanes = {f"codex-{n}": impose(store, clock, wham, enroll, f"codex-{n}", condition)
                 for n, condition in enumerate(conditions, start=1)}
        condition = {f"codex-{n}": value for n, value in enumerate(conditions, start=1)}
        jobs, leases = store.list_jobs(), store.list_leases()
        timer.probe_cycle()
        touched = {lane_id for lane_id, purpose, _ in turns if purpose == "touch"}
        assert touched == {lane_id for lane_id, value in condition.items() if value == "free"}, (condition, turns)
        # An operator names every lane: the plan refuses the held ones, and a lane the
        # plan let through is still stopped at the reservation (no 30 s wait here).
        clock.advance(2 * SPACING)
        wham.started.clear()
        view = timer.snapshot()
        for lane_id, lane in lanes.items():
            [entry] = timer.touch_plan(view, target=lane_id) or [{"action": "skip"}]
            forced = {"lane_id": lane_id, "reason": "forced", "weekly_clock": entry.get("weekly_clock")}
            item = timer._touch_lane(lane, forced, mode="operator", request_id=f"r-{lane_id}", wait_s=0)
            timer._publish_touch(item, "operator")
            if condition[lane_id] != "free":
                assert entry["action"] == "skip" or condition[lane_id] in ("job-lease", "probe-lease"), entry
        touched = {lane_id for lane_id, purpose, _ in turns if purpose == "touch"}
        assert touched == {lane_id for lane_id, value in condition.items() if value == "free"}, (condition, turns)
        assert store.list_jobs() == jobs and store.list_leases() == leases and not timer.active_holders
        assert wham.consumed == []
        assert all(record.get("status") != "touching" for record in timer.touches.values())


# --- no touch on a clock work reached after the plan -------------------------------

#: How an attempt ended: (rc, native session). C-23.19: rc 5 or 127 with no provider
#: session sent no request; every other started attempt did.
SENT = {"session": (0, "thread"), "no-session": (0, None), "rc5-session": (5, "thread"),
        "rc5": (5, None), "rc127": (127, None)}
WORK = st.fixed_dictionaries({
    "offset_s": st.integers(-3 * CLOCK_UNSTARTED_TOLERANCE_S, 3 * CLOCK_UNSTARTED_TOLERANCE_S),
    "sent": st.sampled_from(sorted(SENT)),
})


@PROPERTY
@given(work=st.lists(WORK, max_size=3), mode=st.sampled_from(["auto", "operator"]))
def test_property_no_touch_runs_on_a_clock_work_reached_after_the_plan(work, mode):
    """C-18.7: the plan reads a clock idle at `clock_read_at`; a job that wins the lane after
    the plan, sends a request, and leaves before the touch holds the lane stops the touch under
    its reservation (`skipped-started`), as does one that started within the tolerance before
    the read. Work that sent no request, or ran earlier, stops nothing, so a free lane is
    still touched; and an operator who names the lane touches it whatever ran."""
    with make_rig() as (timer, store, clock, wham, enroll, turns):
        lane = enroll("codex-1")
        timer._persist(lane, {**wham.probe_status(lane, {}), "probed_at": iso(clock())})
        [entry] = [item for item in timer.touch_plan(timer.snapshot()) if item["action"] == "touch"]
        read_at = capacity._time(entry["clock_read_at"])
        for n, spec in enumerate(work):
            rc, session = SENT[spec["sent"]]
            store.add_job(job_id=f"job-{n}", request_id=f"request-{n}", payload_digest="digest", kind="dispatch",
                          state="succeeded" if rc == 0 else "failed", workdir="/work", prompt_path="/prompt",
                          sandbox="read-only")
            store.add_attempt(attempt_id=f"job-{n}/a1", job_id=f"job-{n}", seq=1, lane_id=lane.lane_id, rc=rc,
                              model_requested=CLOCK_TOUCH_MODEL_ID, state="succeeded" if rc == 0 else "failed",
                              started_at=iso(read_at + timedelta(seconds=spec["offset_s"])),
                              native_session_id=session)
        clock.advance(3 * CLOCK_UNSTARTED_TOLERANCE_S + 1)        # every attempt has ended
        blocked = any(spec["offset_s"] >= -CLOCK_UNSTARTED_TOLERANCE_S and spec["sent"] not in ("rc5", "rc127")
                      for spec in work)
        item = timer._touch_lane(lane, entry, mode=mode, request_id="r-1", wait_s=0)
        timer._publish_touch(item, mode)
        assert bool(turns) is not blocked, (work, item["record"])
        if blocked:
            assert item["record"]["status"] == "skipped-started", item["record"]
            assert item["record"]["detail"].startswith("started:attempt:")
            forced = timer._touch_lane(lane, {**entry, "reason": "forced"}, mode="operator", request_id="r-2", wait_s=0)
            timer._publish_touch(forced, "operator")
            assert len(turns) == 1 and forced["record"]["status"] == "ok"
        assert store.list_leases() == [] and not timer.active_holders and wham.consumed == []


# --- spacing ----------------------------------------------------------------------

@PROPERTY
@given(steps=st.lists(st.tuples(st.integers(0, 2 * SPACING),
                                st.sampled_from(["ok", "ineffective", "timeout", "error"]),
                                st.booleans()),
                      min_size=1, max_size=12))
def test_property_automatic_touches_of_a_lane_are_spaced(steps):
    """C-18.7: whatever each turn returns and however the cycles fall, the automatic touches
    of one lane that reach a verdict start at least `touch_spacing_s` apart."""
    plan = iter(())
    with make_rig(outcomes={"codex-1": lambda: next(plan, "ok")}) as (timer, store, clock, wham, enroll, turns):
        enroll("codex-1")
        for advance, outcome, new_window in steps:
            plan = iter([outcome])
            clock.advance(advance)
            if new_window:
                wham.started.clear()                         # an early or global reset
            timer.probe_cycle()
        starts = [capacity._time(row["at"]) for row in touches(store, "codex-1")
                  if row.get("status") == "touching" and row.get("mode") == "auto"]
        assert all((later - earlier).total_seconds() >= SPACING for earlier, later in zip(starts, starts[1:])), starts
        assert store.list_leases() == [] and not timer.active_holders and store.list_jobs() == []
