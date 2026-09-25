"""C-18.3: idle Codex weekly clocks are started by the daemon, not left to slide.

A ChatGPT-subscription Codex account's weekly window starts at its first real
request after a reset. Until then the usage endpoint reports 0% with a reset
that slides to (probe time + 7 days) on every read, so each idle day moves that
lane's next reset a day later (incident: codex-4 redeemed its banked reset on
2026-09-22 at 20:09 ET and sat unstarted until a manual touch at 2026-09-23
15:52 ET, about 20 hours of clock lost). `Wham` below is that endpoint.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path

import pytest

from subfleet import capacity, scheduler
from subfleet.adapters.base import AdapterError
from subfleet.contracts import (CLOCK_TOUCH_MODEL_ID, CLOCK_UNSTARTED_TOLERANCE_S, Closure, ClosureReason,
                                ClockSource, Credential, Lane, LaneOwner, Outcome, OutcomeClass, Reading,
                                ReadingLabel)
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy, touch_model
from subfleet.store import Store
from subfleet.timers import Timers, iso

WEEK = timedelta(days=7)


class Clock:
    def __init__(self):
        self.at = datetime(2026, 9, 22, 20, 9, tzinfo=timezone.utc)

    def __call__(self):
        return self.at

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)


class Wham:
    """The usage endpoint as it reads a Codex weekly window (C-18.3).

    Unstarted: 0% and a reset that slides with the read. Started by a request
    at T: the reset is fixed at T + 7 days. `lag_s` keeps a just-started window
    reading as unstarted for that long, as the endpoint does for a few minutes.
    `starts` is False for a turn that is metered elsewhere (Spark).
    """

    def __init__(self, clock):
        self.clock, self.started, self.calls = clock, {}, []
        self.lag_s, self.starts, self.overrides = 0, True, {}

    def start(self, lane_id):
        if self.starts:
            self.started.setdefault(lane_id, self.clock())

    def probe_status(self, lane, env):
        self.calls.append(lane.lane_id)
        if lane.lane_id in self.overrides:
            return self.overrides[lane.lane_id]
        now = self.clock()
        start = self.started.get(lane.lane_id)
        if start is None or (now - start).total_seconds() < self.lag_s:
            weekly, reset, five = 0.0, now + WEEK, 0.0
        else:
            weekly, reset, five = .01, start + WEEK, .02
        return {"status": "ok", "limit_reached": False, "allowed": True, "account_key": lane.account_key,
                "readings": (
                    Reading(lane.lane_id, "account", "five_hour", five, iso(now + timedelta(hours=5)),
                            ReadingLabel.PROVIDER, "wham", iso(now)),
                    Reading(lane.lane_id, "account", "seven_day", weekly, iso(reset),
                            ReadingLabel.PROVIDER, "wham", iso(now)))}


class Recorder(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    clock = Clock()
    wham = Wham(clock)
    policy = {"models": {"haiku": {"provider": "claude", "id": "claude-haiku-4-5-20251001"},
                         "luna": {"provider": "codex", "id": "gpt-5.6-luna"}},
              "timers": {"probe_interval_s": 60, "keepalive_interval_s": 18300},
              "reset_credits": {"enabled": False}, "alerts": {}, "caps": {}}
    turns, notices = [], []
    log = logging.getLogger(f"test.touch.{id(tmp_path)}")
    recorder = Recorder()
    log.addHandler(recorder)
    log.setLevel(logging.INFO)

    def turn(lane, purpose, holder, *, cancel, deadline):
        assert store.one("SELECT holder FROM leases WHERE lease_key=?", (f"lane:{lane.lane_id}:slot:0",))["holder"] == holder
        turns.append((lane.lane_id, purpose))
        wham.start(lane.lane_id)
        return Outcome(OutcomeClass.OK, "Reply with exactly OK", evidence={"requested_at": iso(clock()), "rc": 0})

    with Store(tmp_path / "state.sqlite3") as store:
        timer = Timers(store, tmp_path, policy, adapter_factory=lambda _: wham, now=clock, turn=turn,
                       deliver=lambda notice: notices.append(notice) or True, log=log)

        def enroll(identity="codex-4", *, enabled=True, owner=LaneOwner.V2, desktop=False):
            home = tmp_path / identity
            home.mkdir(exist_ok=True)
            (home / "auth.json").write_text(json.dumps({"last_refresh": "first"}))
            lane = Lane(identity, "codex", "codex:" + identity, Credential("codex", str(home), "home"),
                        str(home), owner, desktop, enabled)
            store.put_lane(lane)
            return lane

        yield timer, store, clock, wham, enroll, turns, notices, recorder
        timer.stop()


def touches(store, lane_id=None):
    return [json.loads(row["data_json"]) for row in store.query(
        "SELECT lane_id,data_json FROM events WHERE kind='timer.touch' ORDER BY event_id")
            if json.loads(row["data_json"]) and (lane_id is None or row["lane_id"] == lane_id)]


def weekly(now, *, utilization=0.0, offset_s=0.0, window="seven_day", label="provider", age_s=0.0,
           scope="account", resets=True):
    observed = now - timedelta(seconds=age_s)
    length = capacity.window_seconds(window)
    return {"lane_id": "codex-4", "scope": scope, "window": window, "utilization": utilization,
            "resets_at": iso(observed + timedelta(seconds=length + offset_s)) if resets else None,
            "label": label, "source": "wham", "observed_at": iso(observed)}


# --- detection (C-18.3) --------------------------------------------------------

@pytest.mark.parametrize("offset_s", [0, 599, -599])
def test_a_window_at_zero_with_a_sliding_reset_has_not_started(offset_s):
    """C-18.3: 0% and a reset within 600 s of observed_at + 7 d reads as an unstarted clock."""
    now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    evidence = capacity.clock_unstarted([weekly(now, offset_s=offset_s),
                                         weekly(now, window="five_hour")], now=now)
    assert evidence and evidence["window"] == "seven_day"
    assert abs(evidence["offset_s"] - offset_s) < 1


def test_detection_reads_dataclass_readings_and_any_long_window_key():
    """C-9.7, C-18.3: a window is classified by its length, so a 14-day minute key counts too."""
    now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    reading = Reading("codex-4", "account", "seven_day", 0.0, iso(now + WEEK), ReadingLabel.PROVIDER, "wham", iso(now))
    assert capacity.clock_unstarted([reading], now=now)
    assert capacity.clock_unstarted([weekly(now, window="20160")], now=now)
    assert capacity.window_seconds("admission") is None
    assert capacity.window_seconds("seven_day") == 604800


@pytest.mark.parametrize("change", [
    {"utilization": .01},                         # a request was metered: started
    {"offset_s": -2 * 3600},                      # reset fixed two hours ago: started
    {"offset_s": CLOCK_UNSTARTED_TOLERANCE_S + 1},
    {"label": "stale-provider"},
    {"label": "admission-observed"},
    {"age_s": 121},                               # older than the reading TTL
    {"resets": False},
    {"scope": "gpt-5.6-luna"},                    # a model-scoped window is not the account's clock
    {"window": "five_hour"},                      # a five-hour window is not the weekly clock
])
def test_started_stale_or_unread_windows_are_not_unstarted(change):
    """C-18.3: detection reads fresh provider evidence only and never infers an unstarted clock."""
    now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    assert capacity.clock_unstarted([weekly(now, **change)], now=now) is None


def test_one_started_long_window_is_enough_to_say_started():
    """C-18.3: every long window must slide at 0%; one that does not means the clock runs."""
    now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    assert capacity.clock_unstarted([weekly(now), weekly(now, window="20160", utilization=.2)], now=now) is None
    assert capacity.clock_unstarted([], now=now) is None


# --- the automatic touch (C-18.3) ----------------------------------------------

def test_probe_cycle_touches_an_unstarted_lane_once_and_reprobes(rig):
    """C-18.3, C-8.4: one supervised Luna turn starts the clock; the re-probe is what publishes."""
    timer, store, clock, wham, enroll, turns, notices, recorder = rig
    lane = enroll()
    snapshot = timer.probe_cycle()
    assert turns == [(lane.lane_id, "touch")]
    assert wham.calls == [lane.lane_id, lane.lane_id]              # probe, then the re-probe
    [started, done] = touches(store, lane.lane_id)
    assert started["status"] == "touching" and started["mode"] == "auto"
    assert done["status"] == "ok" and done["model"] == CLOCK_TOUCH_MODEL_ID
    assert done["requested_at"] == iso(clock()) and done["resets_at"] == iso(clock() + WEEK)
    assert done["before"]["weekly_clock"] == "not-started"
    row = next(row for row in snapshot["lanes"] if row["lane_id"] == lane.lane_id)
    assert row["weekly_clock"] is None and row["clock_alert"] is None
    [notice] = [n for n in notices if n.get("key") == "codex-clock-started"]
    assert notice["subject"] == "codex: weekly clock started on 1 lane(s)"
    assert "codex-4 (weekly reset now " + iso(clock() + WEEK) + ")" in notice["body"]
    assert not [n for n in notices if n.get("key", "").startswith("codex-clock:")]
    assert len([line for line in recorder.lines if line.startswith("lane touch ")]) == 1
    assert "lane=codex-4 mode=auto model=gpt-5.6-luna status=ok" in recorder.lines[0]
    assert store.list_jobs() == [] and store.list_leases() == []
    status = json.loads((timer.root / "status.json").read_bytes())
    assert status["codex"]["homes"][0]["window_unstarted"] is False
    assert timer.status()["touch"]["last_run"] == iso(clock())
    clock.advance(24 * 3600)
    timer.probe_cycle()
    assert turns == [(lane.lane_id, "touch")]                     # started clocks are left alone
    latest = max((r for r in store.list_readings(lane.lane_id) if r["window"] == "seven_day"),
                 key=lambda r: (r["observed_at"], r["reading_id"]))
    assert latest["observed_at"] == iso(clock()) and latest["resets_at"] == iso(clock() - timedelta(days=1) + WEEK)


def test_a_lane_already_started_or_busy_is_not_touched(rig):
    """C-18.3: a running clock needs no touch; a live attempt is itself the first request."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    started, busy = enroll("codex-1"), enroll("codex-2")
    wham.started[started.lane_id] = clock() - timedelta(days=2)
    store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                  state="running", workdir=str(timer.root), prompt_path="/prompt", sandbox="read-only")
    store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id=busy.lane_id,
                      model_requested="gpt-6-astra", state="running", started_at=iso(clock()))
    timer.probe_cycle()
    assert turns == [] and touches(store) == []
    assert not [n for n in notices if n.get("key", "").startswith("codex-clock")]


def test_touches_are_spaced_an_hour_apart_per_lane(rig):
    """C-18.3: at most one automatic touch per lane per `timers.touch_spacing_s` (3600 s)."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    wham.starts = False                            # a turn metered elsewhere starts nothing
    timer.probe_cycle()
    assert turns == [(lane.lane_id, "touch")]
    for _ in range(59):
        clock.advance(60)
        timer.probe_cycle()
    assert turns == [(lane.lane_id, "touch")]
    clock.advance(60)
    timer.probe_cycle()
    assert turns == [(lane.lane_id, "touch")] * 2
    assert touches(store, lane.lane_id)[-1]["ineffective"] == 1
    # The first touch "succeeded" but the clock did not start: say so once; the
    # repeat is not shown as `touched`, so the warning does not clear and re-fire hourly.
    row = next(row for row in timer.snapshot()["lanes"] if row["lane_id"] == lane.lane_id)
    assert row["weekly_clock"] == "not-started" and row["clock_alert"] == "touch-ineffective"
    alerts = [n for n in notices if n.get("key", "").startswith("codex-clock:")]
    assert len(alerts) == 1 and "succeeded, but the usage endpoint still reads 0%" in alerts[0]["body"]
    assert "subfleet lanes touch codex-4" in alerts[0]["body"]
    assert not [n for n in notices if n.get("recovery")]
    assert [n["subject"] for n in notices if n.get("key") == "codex-clock-started"] == [
        "codex: weekly clock started on 1 lane(s)"]                      # a repeat is not news


def test_a_touch_from_an_earlier_week_does_not_make_this_weeks_touch_a_repeat(rig):
    """C-18.3: `ineffective` counts touches within one idle stretch; last week's touch started
    last week's clock, so this week's first touch is announced and reads `touched`."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    wham.lag_s = 300
    timer.probe_cycle()
    clock.advance(8 * 86400)                       # the week ran out; a new window waits unstarted
    wham.started.clear()
    notices.clear()
    snapshot = timer.probe_cycle()
    assert turns == [(lane.lane_id, "touch")] * 2
    assert "ineffective" not in touches(store, lane.lane_id)[-1]
    row = next(row for row in snapshot["lanes"] if row["lane_id"] == lane.lane_id)
    assert row["weekly_clock"] == "touched" and row["clock_alert"] is None
    assert [n["subject"] for n in notices if n.get("key") == "codex-clock-started"] == [
        "codex: weekly clock started on 1 lane(s)"]


def test_a_just_touched_window_that_still_slides_reads_touched_not_unstarted(rig):
    """C-18.3: within the tolerance after an accepted touch the endpoint may still slide; that is `touched`."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    wham.lag_s = 300
    snapshot = timer.probe_cycle()
    row = next(row for row in snapshot["lanes"] if row["lane_id"] == lane.lane_id)
    assert row["weekly_clock"] == "touched" and row["clock_alert"] is None
    assert not [n for n in notices if n.get("key", "").startswith("codex-clock:")]
    clock.advance(301)
    snapshot = timer.probe_cycle()
    row = next(row for row in snapshot["lanes"] if row["lane_id"] == lane.lane_id)
    assert row["weekly_clock"] is None
    assert turns == [(lane.lane_id, "touch")]


@pytest.mark.parametrize("failure", ["unknown", "limited", "refused", "timed-out"])
def test_a_touch_that_fails_is_recorded_spaced_and_alerted(rig, failure):
    """C-18.3, C-9.4, C-14.2: a failed touch releases its lease, keeps its spacing, and raises a warning."""
    timer, store, clock, wham, enroll, turns, notices, recorder = rig
    lane = enroll()

    def turn(lane, purpose, holder, *, cancel, deadline):
        turns.append((lane.lane_id, purpose))
        if failure == "refused":
            raise AdapterError("guard trust preflight failed", code=7, fix="rerun subfleet doctor")
        if failure == "timed-out":
            raise TimeoutError("touch deadline")
        if failure == "limited":
            return Outcome(OutcomeClass.LIMITED, "usage limit reached", evidence={"rc": 1},
                           closure=Closure(lane.lane_id, CLOCK_TOUCH_MODEL_ID, iso(clock() + timedelta(hours=1)),
                                           ClosureReason.PROVIDER_LIMIT, ClockSource.GUESSED, None))
        return Outcome(OutcomeClass.UNKNOWN, "probe ended without an exit receipt")

    timer.turn = turn
    timer.probe_cycle()
    [_, done] = touches(store, lane.lane_id)
    assert done["status"] == failure
    if failure == "refused":
        assert done["code"] == 7 and done["fix"] == "rerun subfleet doctor"
    if failure == "limited":
        assert store.query("SELECT scope FROM closures WHERE lane_id=?", (lane.lane_id,))[0]["scope"] == CLOCK_TOUCH_MODEL_ID
    assert store.list_leases() == []
    assert f"status={failure}" in recorder.lines[-1]
    alerts = [n for n in notices if n.get("key", "").startswith("codex-clock:")]
    assert len(alerts) == 1 and f"ended {failure}" in alerts[0]["body"]
    assert not [n for n in notices if n.get("key") == "codex-clock-started"]
    clock.advance(1800)
    timer.probe_cycle()
    assert len(turns) == 1


def test_auto_touch_can_be_switched_off_and_then_warns_instead(rig):
    """C-18.3: `timers.touch_unstarted: false` touches nothing and says why the clock is idle."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    timer.policy["timers"]["touch_unstarted"] = False
    timer.probe_cycle()
    assert turns == [] and touches(store) == []
    [alert] = [n for n in notices if n.get("key", "").startswith("codex-clock:")]
    assert "automatic touching is off" in alert["body"]


def test_an_offline_cycle_touches_nothing(rig):
    """C-23.27, C-18.3: a cycle whose every Codex probe failed on the network acts on nothing."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    wham.overrides[lane.lane_id] = {"status": "network-error", "readings": (), "error_type": "URLError"}
    timer.probe_cycle()
    assert turns == [] and touches(store) == []


def test_disabled_v1_and_desktop_lanes_are_never_probed_or_touched(rig):
    """C-18.3, C-10.3, C-10.4: only enabled v2 lanes that are not the desktop login are touched."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    enroll("codex-1", enabled=False)
    enroll("codex-2", owner=LaneOwner.V1)
    enroll("codex-3", desktop=True)
    timer.probe_cycle()
    assert turns == [] and touches(store) == []


# --- the plan: who may be touched, and why not (C-18.3) --------------------------

def view_row(now, lane_id="codex-4", **extra):
    row = {"lane_id": lane_id, "provider": "codex", "account_key": "codex:" + lane_id, "owner": "v2",
           "enabled": True, "desktop": False, "home": "/h/" + lane_id, "credential_ref": "/h/" + lane_id,
           "readings": [weekly(now)], "closures": [], "in_flight": 0}
    row.update(extra)
    return row


@pytest.mark.parametrize("extra,reason", [
    ({"owner": "v1"}, "owner-v1"),
    ({"enabled": False}, "disabled"),
    ({"desktop": True}, "desktop"),
    ({"identity_status": "mismatch"}, "identity-mismatch"),
    ({"probe_status": "revoked", "revoked_epoch": "old"}, "credential-latched"),
    ({"probe_status": "expired-token"}, "credential-latched"),
    ({"probe_status": "limited", "limit_reached": True}, "limited"),
    ({"allowed": False}, "limited"),
    ({"in_flight": 1}, "busy"),
])
def test_blocked_lanes_are_skipped_even_when_forced(rig, extra, reason):
    """C-18.3: no touch uses another owner's, a disabled, desktop, mismatched, latched, limited, or busy lane."""
    timer, store, clock, *_ = rig
    view = {"lanes": [view_row(clock(), **extra)]}
    [entry] = timer.touch_plan(view)
    assert (entry["action"], entry["reason"]) == ("skip", reason)
    [forced] = timer.touch_plan({"lanes": [view_row(clock(), **extra)]}, target="codex-4")
    assert (forced["action"], forced["reason"]) == ("skip", reason)


@pytest.mark.parametrize("scope,blocked", [("account", True), (CLOCK_TOUCH_MODEL_ID, True), ("gpt-6-astra", False)])
def test_a_closure_blocks_a_touch_only_for_the_account_or_the_touch_model(rig, scope, blocked):
    """C-18.3, C-9.6: an Astra-only closure leaves Luna, and so the touch, free."""
    timer, store, clock, *_ = rig
    closure = {"lane_id": "codex-4", "scope": scope, "until_at": iso(clock() + timedelta(hours=2)),
               "reason": "provider-limit"}
    [entry] = timer.touch_plan({"lanes": [view_row(clock(), closures=[closure])]})
    assert entry["action"] == ("skip" if blocked else "touch")
    assert entry["reason"].startswith("closed:") if blocked else entry["reason"] == "not-started"


def test_the_plan_is_read_only_and_a_named_lane_ignores_detection_and_spacing(rig):
    """C-18.3: a dry run writes nothing; an explicit lane is forced past its readings and spacing."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    wham.started[lane.lane_id] = clock() - timedelta(days=1)
    timer.probe_cycle()
    before = store.one("SELECT count(*) n FROM events")["n"]
    view = timer.snapshot()
    [entry] = timer.touch_plan(view)
    assert (entry["action"], entry["reason"], entry["weekly_clock"]) == ("skip", "started", None)
    [forced] = timer.touch_plan(view, target=lane.lane_id)
    assert (forced["action"], forced["reason"]) == ("touch", "forced")
    assert store.one("SELECT count(*) n FROM events")["n"] == before       # planning writes nothing
    assert touches(store) == [] and turns == [] and store.list_leases() == []
    result = timer.touch(view, target=lane.lane_id, mode="operator", request_id="req-1")
    assert [r["status"] for r in result["results"]] == ["ok"] and turns == [(lane.lane_id, "touch")]
    assert touches(store)[-1]["request_id"] == "req-1" and touches(store)[-1]["mode"] == "operator"
    assert not [n for n in notices if n.get("key") == "codex-clock-started"]     # operators read the CLI
    # The forced touch now spaces the automatic one.
    [entry] = timer.touch_plan(timer.snapshot())
    assert entry["next_touch_at"] == iso(clock() + timedelta(hours=1))


def test_operator_request_runs_on_a_timer_worker_and_is_collected(rig):
    """C-18.3, C-16.4: `lanes touch` is queued, and `touch_status` long-polls its result."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    timer.start()
    timer.probe_cycle()                      # auto-touch starts it; a forced touch still runs
    scheduled = timer.request("touch", target=lane.lane_id, request_id="req-9")
    assert scheduled == {"status": "scheduled", "timer": "touch", "target": lane.lane_id, "request_id": "req-9"}
    status = timer.touch_status("req-9", wait_s=10)
    assert status["status"] == "done" and [r["status"] for r in status["results"]] == ["ok"]
    assert timer.touch_status("never-sent") == {"status": "unknown", "request_id": "never-sent", "results": []}


# --- the touch model (C-18.3) ----------------------------------------------------

def test_the_touch_model_is_luna_and_spark_is_refused(tmp_path):
    """C-18.3: only a Codex turn metered on the weekly window starts it; Spark meters elsewhere."""
    policy = load_policy(DEFAULT_POLICY_PATH)
    assert touch_model(policy) == {"short": "luna", "provider": "codex", "id": "gpt-5.6-luna"}
    assert policy["timers"]["touch_spacing_s"] == 3600 and policy["timers"]["touch_unstarted"] is True
    # A policy without `luna` (the installed one predates it) still touches with Luna's id.
    assert touch_model({"models": {"terra": {"provider": "codex", "id": "gpt-5.6-terra"}}})["id"] == "gpt-5.6-luna"
    assert touch_model({"models": {"spark": {"provider": "codex", "id": "gpt-5.6-spark"}},
                        "timers": {"touch_model": "spark"}})["id"] == "gpt-5.6-luna"
    raw = json.loads(DEFAULT_POLICY_PATH.read_text())
    for value, message in (("spark", "Spark meters on its own bucket"), ("opus", "must be a Codex model"),
                           ("nope", "unknown model"), ("", "must name a Codex model")):
        changed = json.loads(json.dumps(raw))
        changed["models"]["spark"] = {"provider": "codex", "id": "gpt-5.6-spark"}
        changed["timers"]["touch_model"] = value
        path = tmp_path / "policy.json"
        path.write_text(json.dumps(changed))
        with pytest.raises(PolicyError, match=message):
            load_policy(path)
    changed = json.loads(json.dumps(raw))
    changed["timers"]["touch_spacing_s"] = 0
    path.write_text(json.dumps(changed))
    with pytest.raises(PolicyError, match="touch_spacing_s"):
        load_policy(path)


# --- the scheduler interaction (C-11.3, C-18.3) --------------------------------

def test_an_unstarted_lane_ranks_last_and_only_the_touch_starts_its_clock(rig):
    """C-11.3, C-18.3 regression: routing orders Codex lanes by weekly reset, and an unstarted
    window always reads the latest possible reset, so nothing routed ever starts it; the
    daemon's touch does (2026-09-22 codex-4: 20 h of clock lost)."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    policy = load_policy(DEFAULT_POLICY_PATH)
    working, idle = enroll("codex-1"), enroll("codex-4")
    wham.started[working.lane_id] = clock() - timedelta(days=5)

    def decide():
        view = timer.snapshot()
        view["now"] = iso(clock())
        decision = scheduler.evaluate(policy, view, {"pinned_model": "astra", "exclusions": "[]"})
        details = decision.evaluations[0]["candidate_details"]
        return decision.chosen_lane, details[idle.lane_id]["seven_day_reset"]

    timer.policy["timers"]["touch_unstarted"] = False
    timer.probe_cycle()
    chosen, reset = decide()
    assert chosen == working.lane_id and reset == iso(clock() + WEEK)
    clock.advance(86400)
    timer.probe_cycle()
    chosen, slid = decide()
    assert chosen == working.lane_id and slid == iso(clock() + WEEK)   # a day idle: the reset slid a day
    assert turns == []
    timer.policy["timers"]["touch_unstarted"] = True
    clock.advance(60)
    timer.probe_cycle()
    assert turns == [(idle.lane_id, "touch")]
    touched_at = clock()
    clock.advance(86400)
    timer.probe_cycle()
    _, fixed = decide()
    assert fixed == iso(touched_at + WEEK)                             # the clock runs; the reset holds


# --- review regressions (C-18.3) ------------------------------------------------

def test_a_clock_a_job_just_started_is_not_touched_again(rig):
    """C-18.3: a short job's request fixes the reset minutes before the probe reads 0%; that is
    `touched` (by the job), so no second turn is spent and no "started" notice is sent."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    wham.lag_s = 600
    store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                  state="succeeded", workdir=str(timer.root), prompt_path="/prompt", sandbox="read-only")
    store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id=lane.lane_id, rc=0,
                      model_requested="gpt-5.6-luna", state="succeeded", started_at=iso(clock() - timedelta(minutes=4)),
                      native_session_id="thread")
    wham.started[lane.lane_id] = clock() - timedelta(minutes=4)
    snapshot = timer.probe_cycle()
    row = next(row for row in snapshot["lanes"] if row["lane_id"] == lane.lane_id)
    assert row["weekly_clock"] == "touched" and row["clock_request"]["source"] == "attempt"
    assert turns == [] and not [n for n in notices if n.get("key", "").startswith("codex-clock")]


@pytest.mark.parametrize("status", ["revoked", "expired-token", "limited", "no-auth"])
def test_a_cycle_whose_probe_is_not_ok_touches_nothing(rig, status):
    """C-18.3, C-23.47: only a lane whose probe this cycle answered `ok` is touched automatically."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    sliding = wham.probe_status(lane, {})
    wham.overrides[lane.lane_id] = {**sliding, "status": status, "limit_reached": status == "limited"}
    timer.probe_cycle()
    # An expired token gets its one heal turn (C-23.47); no touch follows it.
    assert [purpose for _, purpose in turns if purpose == "touch"] == [] and touches(store) == []


def test_an_automatic_pass_takes_one_round_and_releases_each_lane_as_it_finishes(rig):
    """C-18.3, C-6.4: at most `lane_workers` touches per cycle; a finished lane is not held for the rest."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lanes = [enroll(f"codex-{n}") for n in range(1, 7)]
    held = []

    def turn(lane, purpose, holder, *, cancel, deadline):
        held.append(len(store.query("SELECT 1 FROM leases WHERE holder LIKE 'probe:timer:%'")))
        turns.append((lane.lane_id, purpose))
        wham.start(lane.lane_id)
        return Outcome(OutcomeClass.OK, "OK", evidence={"requested_at": iso(clock()), "rc": 0})

    timer.turn = turn
    timer.probe_cycle()
    assert len(turns) == timer.lane_workers == 4 and max(held) <= 4
    assert store.list_leases() == []
    assert sum(int(n["subject"].split()[-2]) for n in notices if n.get("key") == "codex-clock-started") == 4
    clock.advance(60)
    timer.probe_cycle()
    assert sorted(lane_id for lane_id, _ in turns) == [lane.lane_id for lane in lanes]


def test_a_failure_while_publishing_one_touch_releases_every_lease_and_the_cycle_still_publishes(rig, monkeypatch):
    """C-18.3, C-23.27: a store error persisting one re-probe is that lane's; no lease is left behind."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    enroll("codex-1"), enroll("codex-2")
    persist, calls = timer._persist, []

    def failing(lane, probe):
        calls.append(lane.lane_id)
        if len(calls) == 3:                        # the first touch re-probe (after two cycle probes)
            import sqlite3
            raise sqlite3.OperationalError("database is locked")
        return persist(lane, probe)

    monkeypatch.setattr(timer, "_persist", failing)
    (timer.root / "status.json").unlink(missing_ok=True)
    timer.probe_cycle()
    assert store.list_leases() == [] and not timer.active_holders
    results = {t["lane_id"]: t for t in touches(store) if t["status"] != "touching"}
    assert sorted(results) == ["codex-1", "codex-2"]
    assert [t.get("error_type") for t in results.values()].count("OperationalError") == 1
    assert (timer.root / "status.json").exists()


def test_a_failure_right_after_the_reservation_releases_it(rig, monkeypatch):
    """C-18.3: everything after `_reserve` is inside `try`; the lease is always released."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    record, calls = timer._record_touch, []

    def flaky(lane_id, value):
        calls.append(value["status"])
        if len(calls) == 1:
            raise RuntimeError("store unavailable")
        return record(lane_id, value)

    monkeypatch.setattr(timer, "_record_touch", flaky)
    timer.probe_cycle()
    assert turns == [] and store.list_leases() == [] and not timer.active_holders
    assert touches(store, lane.lane_id)[-1]["status"] == "failed"
    assert touches(store, lane.lane_id)[-1]["error_type"] == "RuntimeError"


def test_a_touch_cut_short_by_shutdown_or_a_crash_neither_spaces_nor_warns(rig):
    """C-18.3: stopping the daemon mid-turn is no verdict on the lane; neither is a crash."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()

    def stopping(lane, purpose, holder, *, cancel, deadline):
        turns.append((lane.lane_id, purpose))
        timer.cancel.set()
        return Outcome(OutcomeClass.UNKNOWN, "timer cancelled", evidence={"timed_out": True})

    timer.turn = stopping
    timer.probe_cycle()
    assert touches(store, lane.lane_id)[-1]["status"] == "cancelled"
    restarted = Timers(store, timer.root, timer.policy, adapter_factory=lambda _: wham, now=clock,
                       turn=rig[0].turn, deliver=lambda notice: notices.append(notice) or True)
    try:
        [entry] = restarted.touch_plan(restarted.snapshot())
        assert entry["next_touch_at"] is None
        row = next(row for row in restarted.snapshot()["lanes"] if row["lane_id"] == lane.lane_id)
        assert row["clock_alert"] is None
        # A crash leaves `touching`; once it can no longer be running it is interrupted, not failed.
        restarted._record_touch(lane.lane_id, {"lane_id": lane.lane_id, "at": iso(clock()), "mode": "auto",
                                               "status": "touching"})
        [entry] = restarted.touch_plan(restarted.snapshot())
        assert entry["reason"] == "spaced"                # still possibly running: keep out
        clock.advance(120 + 301)
        view = restarted.snapshot()
        [entry] = restarted.touch_plan(view)
        row = next(row for row in view["lanes"] if row["lane_id"] == lane.lane_id)
        assert row["clock_touch"]["status"] == "interrupted" and row["clock_alert"] is None
        assert entry["next_touch_at"] is None
    finally:
        restarted.stop()


def test_a_touch_that_waited_behind_another_rechecks_spacing_after_reserving(rig):
    """C-18.3: an automatic touch queued behind an operator's touch of the same lane does not run."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    timer.probe_cycle()
    assert len(turns) == 1
    stale_entry = {"lane_id": lane.lane_id, "reason": "not-started", "weekly_clock": "not-started"}
    item = timer._touch_lane(lane, stale_entry, mode="auto", request_id=None, wait_s=0)
    assert item["record"]["status"] == "skipped-spaced" and item["holder"]
    timer._publish_touch(item, "auto")
    assert len(turns) == 1 and store.list_leases() == []
    forced = timer._touch_lane(lane, {**stale_entry, "reason": "forced"}, mode="operator", request_id="r", wait_s=0)
    timer._publish_touch(forced, "operator")
    assert len(turns) == 2 and forced["record"]["status"] == "ok"


def test_an_automatic_pass_skips_a_lane_a_probe_reservation_holds_and_an_operator_waits(rig):
    """C-18.3: a quarantined probe keeps its lease; the automatic pass says `held` instead of churning."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    timer.probe_cycle()
    clock.advance(3601)
    wham.started.clear()
    timer._persist(lane, {**wham.probe_status(lane, {}), "probed_at": iso(clock())})
    store.acquire_lease(f"lane:{lane.lane_id}:slot:0", "probe:timer:stuck")
    [auto] = timer.touch_plan(timer.snapshot(held=True), auto=True)
    assert (auto["action"], auto["reason"]) == ("skip", "held")
    [operator] = timer.touch_plan(timer.snapshot(held=True))
    assert operator["action"] == "touch"


def test_a_superseded_lane_is_refused_as_superseded(rig):
    """C-18.3, C-10.2: a re-enrolled lane's old binding names its successor, not a login."""
    timer, store, clock, *_ = rig
    [entry] = timer.touch_plan({"lanes": [view_row(clock(), enabled=False, superseded_by="codex-9")]},
                               target="codex-4")
    assert entry["reason"] == "superseded"


def test_a_retired_alias_is_not_a_touch_model(tmp_path):
    """C-18.3: `sol` resolves to Astra at ultra effort; the touch names the model it means."""
    raw = json.loads(DEFAULT_POLICY_PATH.read_text())
    raw["timers"]["touch_model"] = "sol"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(PolicyError, match="retired alias"):
        load_policy(path)


# --- review round 2 (C-18.3) ------------------------------------------------------

def test_an_operator_touch_in_flight_does_not_clear_a_standing_warning(rig):
    """C-18.3, C-23.52: while a touch runs the lane stands on its last settled touch, so a cycle
    in between neither sends `recovered:` nor re-raises the warning after."""
    import threading
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    timer.turn = lambda lane, purpose, holder, **_: Outcome(OutcomeClass.UNKNOWN, "no receipt", evidence={"rc": None})
    timer.probe_cycle()
    assert [n["key"] for n in notices if n.get("key", "").startswith("codex-clock:")]
    entered, gate = threading.Event(), threading.Event()

    def slow(lane, purpose, holder, *, cancel, deadline):
        entered.set()
        gate.wait(5)
        return Outcome(OutcomeClass.UNKNOWN, "no receipt", evidence={"rc": None})

    timer.turn = slow
    worker = threading.Thread(target=timer.touch, kwargs={"target": lane.lane_id, "mode": "operator",
                                                          "request_id": "r"})
    worker.start()
    try:
        assert entered.wait(5)
        clock.advance(60)
        snapshot = timer.probe_cycle()
        row = next(row for row in snapshot["lanes"] if row["lane_id"] == lane.lane_id)
        assert row["clock_touch"]["status"] == "touching" and row["clock_alert"] == "touch-failed"
        assert not [n for n in notices if n.get("recovery")]
    finally:
        gate.set()
        worker.join(5)
    clock.advance(60)
    timer.probe_cycle()
    assert len([n for n in notices if n.get("key", "").startswith("codex-clock:")]) == 1


def test_a_failed_touch_between_two_ineffective_ones_keeps_the_count(rig):
    """C-18.3: ok, then a failure, then ok, with the clock never starting, is still ineffective:
    no "started" notice, no `touched`, no recovery."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    wham.starts = False
    outcomes = iter([Outcome(OutcomeClass.OK, "OK", evidence={"rc": 0}),
                     Outcome(OutcomeClass.TRANSIENT, "stream disconnected", evidence={"rc": 1}),
                     Outcome(OutcomeClass.OK, "OK", evidence={"rc": 0})])

    def turn(lane, purpose, holder, *, cancel, deadline):
        turns.append((lane.lane_id, purpose))
        outcome = next(outcomes)
        return Outcome(outcome.cls, outcome.detail, evidence={**outcome.evidence, "requested_at": iso(clock())})

    timer.turn = turn
    for cycle in range(3):
        if cycle:
            clock.advance(3601)
        timer.probe_cycle()
    assert [t["status"] for t in touches(store, lane.lane_id) if t["status"] != "touching"] == ["ok", "transient", "ok"]
    assert touches(store, lane.lane_id)[-1]["ineffective"] == 1
    assert len([n for n in notices if n.get("key") == "codex-clock-started"]) == 1
    assert not [n for n in notices if n.get("recovery")]
    clock.advance(700)                                  # past the tolerance, inside the spacing
    snapshot = timer.probe_cycle()
    assert len(turns) == 3
    row = next(row for row in snapshot["lanes"] if row["lane_id"] == lane.lane_id)
    assert row["weekly_clock"] == "not-started" and row["clock_alert"] == "touch-ineffective"
    assert not [n for n in notices if n.get("recovery")]


def test_an_operator_result_collected_after_a_restart_is_one_record_per_lane(rig):
    """C-18.3, C-17.3: the cold `touch-status` answer is the last record per lane, not `touching` too."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    timer.touch(target=lane.lane_id, mode="operator", request_id="req-cold")
    restarted = Timers(store, timer.root, timer.policy, adapter_factory=lambda _: wham, now=clock)
    try:
        status = restarted.touch_status("req-cold")
        assert status["status"] == "unknown" and [r["status"] for r in status["results"]] == ["ok"]
    finally:
        restarted.stop()


def test_a_touch_model_closure_warns_with_its_clock_and_points_at_status(rig):
    """C-18.3: with Luna closed on a lane the touch cannot start it; the warning says until when."""
    from subfleet.alerts import evaluate_conditions
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    until = iso(clock() + timedelta(hours=3))
    store.add_closure(Closure(lane.lane_id, CLOCK_TOUCH_MODEL_ID, until, ClosureReason.PROVIDER_LIMIT,
                              ClockSource.REPORTED, None))
    snapshot = timer.probe_cycle()
    assert turns == []
    [condition] = [c for c in evaluate_conditions(snapshot, now=clock()) if c["key"].startswith("codex-clock:")]
    assert f"the touch model is closed on it until {until}" in condition["body"]
    assert condition["body"].endswith("Inspect: subfleet status")


def test_a_forced_touch_of_a_running_clock_does_not_make_the_next_real_start_a_repeat(rig):
    """C-18.3: an operator may touch a lane whose clock runs; when that window resets soon after,
    the automatic touch that starts the new clock is announced and reads `touched`."""
    timer, store, clock, wham, enroll, turns, notices, _ = rig
    lane = enroll()
    wham.lag_s = 300
    wham.started[lane.lane_id] = clock() - timedelta(days=7) + timedelta(minutes=30)
    timer.probe_cycle()
    assert turns == []                                           # running: nothing to do
    timer.touch(target=lane.lane_id, mode="operator", request_id="forced")
    assert touches(store, lane.lane_id)[-1]["before"]["weekly_clock"] is None
    clock.advance(3601)                                          # the week ran out; unstarted again
    wham.started.clear()
    notices.clear()
    snapshot = timer.probe_cycle()
    assert len(turns) == 2 and "ineffective" not in touches(store, lane.lane_id)[-1]
    assert touches(store, lane.lane_id)[-1]["previous"]["unstarted"] is False
    row = next(row for row in snapshot["lanes"] if row["lane_id"] == lane.lane_id)
    assert row["weekly_clock"] == "touched" and row["clock_alert"] is None
    assert [n["subject"] for n in notices if n.get("key") == "codex-clock-started"] == [
        "codex: weekly clock started on 1 lane(s)"]
