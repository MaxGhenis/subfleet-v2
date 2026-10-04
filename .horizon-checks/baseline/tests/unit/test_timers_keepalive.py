"""C-23.19 and C-23.29 bounded, request-timestamped Claude keepalive passes."""

from datetime import datetime, timedelta, timezone
import json
import threading
import time

import pytest

from subfleet.contracts import Credential, Lane, LaneOwner, Outcome, OutcomeClass
from subfleet.store import Store
from subfleet.timers import Timers, iso


@pytest.fixture
def rig(tmp_path):
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    policy = {"models": {"haiku": {"id": "claude-haiku-4-5-20251001"}},
              "timers": {"probe_interval_s": 300, "keepalive_interval_s": 18300},
              "reset_credits": {"enabled": False}, "alerts": {}, "caps": {}}
    calls = []

    def turn(lane, purpose, holder, *, cancel, deadline):
        calls.append((lane.lane_id, purpose, deadline))
        return Outcome(OutcomeClass.OK, "ok", evidence={"requested_at": iso(now)}, native_session_id="session")

    with Store(tmp_path / "state.sqlite3") as store:
        timer = Timers(store, tmp_path, policy, turn=turn, now=lambda: now)

        def enroll(identity="claude-1", *, provider="claude", enabled=True, desktop=False):
            home = tmp_path / identity
            home.mkdir(exist_ok=True)
            lane = Lane(identity, provider, provider + ":" + identity, Credential(provider, str(home), "home"),
                        str(home), LaneOwner.V2, desktop, enabled)
            store.put_lane(lane)
            return lane

        yield timer, store, now, calls, enroll
        timer.stop()


def attempt(store, root, lane, started, *, state="succeeded", rc=0, session="session"):
    store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                  state="running" if state == "running" else "succeeded", workdir=str(root),
                  prompt_path="/prompt", sandbox="read-only")
    store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id=lane.lane_id,
                      model_requested="claude-haiku-4-5-20251001", state=state, started_at=iso(started),
                      native_session_id=session, rc=rc)


def keepalive_events(store):
    return [json.loads(row["data_json"]) for row in store.query(
        "SELECT data_json FROM events WHERE kind='timer.keepalive' ORDER BY event_id")
            if json.loads(row["data_json"])]


@pytest.mark.parametrize("state", ["succeeded", "running"])
def test_recent_completed_or_running_attempt_skips_keepalive(rig, state):
    """C-23.19: a request in the last five hours counts even while its attempt is still running."""
    timer, store, now, calls, enroll = rig
    lane = enroll()
    attempt(store, timer.root, lane, now - timedelta(hours=4), state=state)
    assert timer.keepalive_cycle() == ["skipped-open"]
    assert calls == []
    assert keepalive_events(store)[-1]["status"] == "skipped-open"
    assert store.list_readings(lane.lane_id) == []


@pytest.mark.parametrize("session,expected_calls", [(None, 1), ("provider-session", 0)])
def test_rc5_only_opens_window_when_a_provider_session_was_recorded(rig, session, expected_calls):
    """C-23.19: rc 5 without a provider session did not open a five-hour request window."""
    timer, store, now, calls, enroll = rig
    lane = enroll()
    attempt(store, timer.root, lane, now - timedelta(minutes=1), state="failed", rc=5, session=session)
    timer.keepalive_cycle()
    assert len(calls) == expected_calls


def test_keepalive_uses_request_timestamp_and_does_not_reset_window(rig):
    """C-23.19, C-9.1, C-8.4: admission starts at send time, with no quota numbers or reset clock."""
    timer, store, now, calls, enroll = rig
    lane = enroll()
    sent = iso(now - timedelta(seconds=12))

    def turn(lane, purpose, holder, *, cancel, deadline):
        assert purpose == "keepalive"
        assert store.one("SELECT holder FROM leases WHERE holder=?", (holder,))
        return Outcome(OutcomeClass.OK, "ok", evidence={"requested_at": sent}, native_session_id="session")

    timer.turn = turn
    assert timer.keepalive_cycle() == ["ok"]
    readings = store.list_readings(lane.lane_id)
    assert len(readings) == 1
    assert readings[0]["observed_at"] == sent
    assert readings[0]["window"] == "admission"
    assert readings[0]["scope"] == timer.policy["models"]["haiku"]["id"]
    assert readings[0]["label"] == "admission-observed"
    assert readings[0]["utilization"] is None
    assert readings[0]["resets_at"] is None
    assert store.list_jobs() == []
    assert timer.keepalive_cycle() == ["skipped-open"]


@pytest.mark.parametrize("minutes,expected", [(299, 0), (300, 1), (301, 1)])
def test_request_window_expires_at_five_hours(rig, minutes, expected):
    """C-23.19: only requests strictly inside the past five hours suppress a keepalive."""
    timer, store, now, calls, enroll = rig
    lane = enroll()
    attempt(store, timer.root, lane, now - timedelta(minutes=minutes))
    timer.keepalive_cycle()
    assert len(calls) == expected


def test_keepalive_targets_only_idle_enabled_claude_lanes(rig):
    """C-18.1, C-23.44, C-10.3: disabled, busy, desktop, and Codex lanes receive no keepalive."""
    timer, store, _, calls, enroll = rig
    enroll("disabled", enabled=False)
    enroll("desktop", desktop=True)
    enroll("codex-1", provider="codex")
    busy = enroll("busy")
    store.acquire_lease(f"lane:{busy.lane_id}:slot:0", "active")
    ready = enroll("ready")
    timer.keepalive_cycle()
    assert [call[0] for call in calls] == [ready.lane_id]


def test_auth_dead_keepalive_disables_lane_for_later_passes(rig):
    """C-23.44: auth-dead during a keepalive disables the lane and forbids subsequent requests."""
    timer, store, _, calls, enroll = rig
    lane = enroll()

    def turn(lane, purpose, holder, *, cancel, deadline):
        calls.append(lane.lane_id)
        return Outcome(OutcomeClass.AUTH_DEAD, "organization blocked")

    timer.turn = turn
    assert timer.keepalive_cycle() == ["auth-dead"]
    assert not store.get_lane(lane.lane_id).enabled
    assert timer.keepalive_cycle() == []
    assert calls == [lane.lane_id]


def test_keepalive_concurrency_is_capped_at_four(rig):
    """C-23.29: a pass never has more than four provider turns in progress."""
    timer, _, now, _, enroll = rig
    for index in range(9):
        enroll(f"claude-{index}")
    lock = threading.Lock()
    reached_four = threading.Event()
    active = peak = calls = 0

    def turn(lane, purpose, holder, *, cancel, deadline):
        nonlocal active, peak, calls
        with lock:
            calls += 1
            active += 1
            peak = max(peak, active)
            if active == 4:
                reached_four.set()
        assert reached_four.wait(1)
        time.sleep(.015)
        with lock:
            active -= 1
        return Outcome(OutcomeClass.OK, "ok", evidence={"requested_at": iso(now)})

    timer.turn = turn
    assert timer.keepalive_cycle() == ["ok"] * 9
    assert peak == 4
    assert calls == 9


def test_keepalive_timeout_is_bounded_and_not_retried_in_same_pass(rig):
    """C-23.29, C-16.4: each lane receives one bounded attempt and a durable timeout verdict."""
    timer, store, _, _, enroll = rig
    timer.policy["caps"]["keepalive_timeout_s"] = .04
    for index in range(5):
        enroll(f"claude-{index}")
    calls = []

    def turn(lane, purpose, holder, *, cancel, deadline):
        assert 0 < deadline - time.monotonic() <= .04
        calls.append(lane.lane_id)
        while time.monotonic() < deadline and not cancel.is_set():
            time.sleep(.002)
        return Outcome(OutcomeClass.UNKNOWN, "timed out", evidence={"timed_out": True})

    timer.turn = turn
    started = time.monotonic()
    assert timer.keepalive_cycle() == ["timed-out"] * 5
    assert time.monotonic() - started < .5
    assert len(calls) == len(set(calls)) == 5
    assert [event["status"] for event in keepalive_events(store)] == ["timed-out"] * 5
    assert store.list_leases() == []


def test_keepalive_timeout_exception_records_lane_result(rig):
    """C-23.29: an injected transport timeout records a timed-out lane without retrying it."""
    timer, store, _, calls, enroll = rig
    enroll()

    def turn(lane, purpose, holder, *, cancel, deadline):
        calls.append(lane.lane_id)
        raise TimeoutError("fake transport timeout")

    timer.turn = turn
    assert timer.keepalive_cycle() == ["timed-out"]
    assert len(calls) == 1
    assert keepalive_events(store)[-1]["status"] == "timed-out"
    assert store.list_leases() == []


def test_shutdown_cancels_running_keepalive_and_releases_its_lane(rig):
    """C-16.4, C-23.29: shutdown cancels the active bounded turn and releases confirmed-empty ownership."""
    timer, store, _, _, enroll = rig
    enroll()
    entered = threading.Event()

    def turn(lane, purpose, holder, *, cancel, deadline):
        entered.set()
        assert cancel.wait(1)
        return Outcome(OutcomeClass.UNKNOWN, "cancelled", evidence={"timed_out": True})

    timer.turn = turn
    timer.intervals["keepalive"] = .01
    timer.start()
    time.sleep(.015)
    timer.tick()
    assert entered.wait(1)
    before = time.monotonic()
    timer.stop()
    assert time.monotonic() - before < .3
    assert store.list_leases() == []
    assert timer.status()["keepalive"]["last_run"] is not None
    assert timer.status()["keepalive"]["last_error_type"] is None
