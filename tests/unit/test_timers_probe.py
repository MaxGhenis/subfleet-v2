"""C-18.1 probe windows, authentication latches, and post-heal publication."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time

import pytest

from subfleet.contracts import Credential, Lane, LaneOwner, Outcome, OutcomeClass, Reading, ReadingLabel
from subfleet.store import Store
from subfleet.timers import Timers, iso


class Clock:
    def __init__(self):
        self.at = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.at

    def advance(self, seconds=300):
        self.at += timedelta(seconds=seconds)


class Probe:
    def __init__(self, clock):
        self.clock, self.calls, self.responses = clock, [], {}

    def probe_status(self, lane, env):
        self.calls.append(lane.lane_id)
        results = self.responses.get(lane.lane_id, [])
        if results:
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return {"status": "ok", "limit_reached": False, "readings": (
            Reading(lane.lane_id, "account", "five_hour", .2,
                    iso(self.clock() + timedelta(hours=5)), ReadingLabel.PROVIDER, "wham", iso(self.clock())),)}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    clock = Clock()
    adapter = Probe(clock)
    policy = {"models": {"haiku": {"id": "claude-haiku-4-5-20251001"}},
              "timers": {"probe_interval_s": 300, "keepalive_interval_s": 18300},
              "reset_credits": {"enabled": False}, "alerts": {}, "caps": {}}
    with Store(tmp_path / "state.sqlite3") as store:
        timer = Timers(store, tmp_path, policy, adapter_factory=lambda _: adapter, now=clock)

        def enroll(identity="codex-1", *, enabled=True, owner=LaneOwner.V2, desktop=False, account=None):
            home = tmp_path / identity
            home.mkdir(exist_ok=True)
            (home / "auth.json").write_text(json.dumps({"last_refresh": "first"}))
            lane = Lane(identity, "codex", account or "codex:" + identity, Credential("codex", str(home), "home"),
                        str(home), owner, desktop, enabled)
            store.put_lane(lane)
            return lane

        yield timer, store, clock, adapter, enroll
        timer.stop()


def events(store, kind, lane_id=None):
    return [json.loads(row["data_json"]) for row in store.query(
        "SELECT lane_id,data_json FROM events WHERE kind=? ORDER BY event_id", (kind,))
            if (lane_id is None or row["lane_id"] == lane_id) and json.loads(row["data_json"])]


def test_probe_once_per_idle_enabled_lane_per_window(rig):
    """C-18.1, C-8.4, C-9.1: each idle enabled lane writes readings once per window without jobs."""
    timer, store, clock, adapter, enroll = rig
    enroll("codex-1")
    enroll("codex-2")
    enroll("disabled", enabled=False)
    enroll("legacy", owner=LaneOwner.V1)
    timer.probe_cycle()
    timer.probe_cycle()
    assert sorted(adapter.calls) == ["codex-1", "codex-2"]
    assert len(store.list_readings()) == 2
    clock.advance()
    timer.probe_cycle()
    assert sorted(adapter.calls) == ["codex-1", "codex-1", "codex-2", "codex-2"]
    assert len(store.list_readings()) == 4
    assert store.list_jobs() == []
    assert (timer.root / "status.json").exists()


def test_auth_dead_lane_is_never_probed_again_even_after_restart(rig):
    """C-23.44, C-18.1: auth-dead disables immediately and no later cycle sends another request."""
    timer, store, clock, adapter, enroll = rig
    lane = enroll()
    adapter.responses[lane.lane_id] = [{"status": "auth-dead", "readings": ()}]
    timer.probe_cycle()
    assert not store.get_lane(lane.lane_id).enabled
    clock.advance(86400)
    timer.probe_cycle()
    restarted = Timers(store, timer.root, timer.policy, adapter_factory=lambda _: adapter, now=clock)
    try:
        restarted.probe_cycle()
    finally:
        restarted.stop()
    assert adapter.calls == [lane.lane_id]


def test_expired_token_has_one_heal_and_publishes_only_reprobe_verdict(rig):
    """C-23.47, C-23.27: one guardian heal precedes the cycle's sole persisted post-heal verdict."""
    timer, store, clock, adapter, enroll = rig
    lane = enroll()
    adapter.responses[lane.lane_id] = [{"status": "expired-token", "readings": ()}]
    turns = []

    def turn(lane, purpose, holder, *, cancel, deadline):
        assert store.list_readings(lane.lane_id) == []
        assert events(store, "timer.heal", lane.lane_id)
        assert store.one("SELECT holder FROM leases WHERE holder=?", (holder,))
        assert deadline > time.monotonic()
        turns.append(purpose)
        return Outcome(OutcomeClass.OK, "CLI refreshed token", evidence={"requested_at": iso(clock())})

    timer.turn = turn
    snapshot = timer.probe_cycle()
    assert turns == ["heal"]
    assert adapter.calls == [lane.lane_id, lane.lane_id]
    assert snapshot["lanes"][0]["verdict"] == "ok"
    assert [row["label"] for row in store.list_readings(lane.lane_id)] == ["provider"]
    assert [value["verdict"] for value in events(store, "timer.verdict", lane.lane_id)] == ["ok"]
    clock.advance(1200)
    adapter.responses[lane.lane_id] = [{"status": "expired-token", "readings": ()}]
    timer.probe_cycle()
    assert turns == ["heal"]


def test_revoked_token_stays_latched_until_auth_epoch_changes(rig):
    """C-23.47, C-18.1: revoked credentials suppress probes until auth.json last_refresh changes."""
    timer, store, clock, adapter, enroll = rig
    lane = enroll()
    adapter.responses[lane.lane_id] = [{"status": "revoked", "readings": ()}]
    timer.probe_cycle()
    clock.advance(3600)
    timer.probe_cycle()
    assert adapter.calls == [lane.lane_id]
    assert timer.snapshot()["lanes"][0]["dispatchable"] is False
    (Path(lane.home) / "auth.json").write_text(json.dumps({"last_refresh": "second"}))
    timer.probe_cycle()
    assert adapter.calls == [lane.lane_id, lane.lane_id]
    assert timer.snapshot()["lanes"][0]["dispatchable"] is True
    assert store.get_lane(lane.lane_id).enabled


@pytest.mark.parametrize("busy", ["lease", "attempt", "desktop"])
def test_busy_or_desktop_lane_does_not_probe(rig, busy):
    """C-18.1, C-10.3: occupied and protected desktop lanes send no monitoring request."""
    timer, store, _, adapter, enroll = rig
    lane = enroll(desktop=busy == "desktop")
    if busy == "lease":
        store.acquire_lease(f"lane:{lane.lane_id}:slot:1", "active-probe")
    elif busy == "attempt":
        store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                      state="running", workdir=str(timer.root), prompt_path="/prompt", sandbox="read-only")
        store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id=lane.lane_id,
                          model_requested="gpt-6-astra", state="running")
    timer.probe_cycle()
    assert adapter.calls == []
    assert store.list_readings(lane.lane_id) == []


def test_probe_lane_reservation_covers_verdict_publication(rig, monkeypatch):
    """C-18.1, C-23.44: admission cannot acquire a lane between auth-dead response and publication."""
    timer, store, _, adapter, enroll = rig
    lane = enroll()
    adapter.responses[lane.lane_id] = [{"status": "auth-dead", "readings": ()}]
    original = timer._persist

    def persist(lane, result):
        assert store.one("SELECT 1 FROM leases WHERE lease_key LIKE ?", (f"lane:{lane.lane_id}:%",))
        original(lane, result)

    monkeypatch.setattr(timer, "_persist", persist)
    timer.probe_cycle()
    assert store.list_leases() == []


def test_tick_is_nonblocking_and_does_not_overlap_one_cycle(rig):
    """C-16.4, C-18.1: blocked provider work runs off the control loop without duplicate cycles."""
    timer, _, clock, adapter, enroll = rig
    enroll()
    entered, released = threading.Event(), threading.Event()
    original = adapter.probe_status

    def blocked(lane, env):
        entered.set()
        assert released.wait(2)
        return original(lane, env)

    adapter.probe_status = blocked
    timer.intervals["probe"] = .01
    timer.start()
    clock.advance(300)
    time.sleep(.015)
    before = time.monotonic()
    timer.tick()
    assert time.monotonic() - before < .2
    assert entered.wait(1)
    try:
        clock.advance(300)
        time.sleep(.015)
        timer.tick()
        assert timer.status()["probe"]["last_run"] is None
    finally:
        released.set()
    deadline = time.monotonic() + 2
    while timer.status()["probe"]["last_run"] is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert adapter.calls == ["codex-1"]
    assert timer.status()["probe"]["last_error_type"] is None
    assert timer.status()["probe"]["last_run"] is not None


def test_usage_timeout_does_not_block_cycle_or_shutdown(rig):
    """C-16.4, C-18.1: an uncooperative usage read cannot hold the cycle or shutdown past its deadline."""
    timer, _, _, adapter, enroll = rig
    enroll()
    timer.policy["caps"]["probe_timeout_s"] = .03
    entered, release = threading.Event(), threading.Event()

    def blocked(lane, env):
        entered.set()
        release.wait(2)
        return {"status": "ok", "readings": ()}

    adapter.probe_status = blocked
    try:
        before = time.monotonic()
        snapshot = timer.probe_cycle()
        assert entered.is_set()
        assert time.monotonic() - before < .4
        assert snapshot["offline"] is True
        assert snapshot["lanes"][0]["error_type"] == "TimeoutError"
        before = time.monotonic()
        timer.stop()
        assert time.monotonic() - before < .2
    finally:
        release.set()


def test_revocation_discovered_by_heal_latches_without_second_usage_request(rig):
    """C-23.47: the CLI's revoked-refresh verdict latches that epoch without a further usage call."""
    timer, _, clock, adapter, enroll = rig
    lane = enroll()
    adapter.responses[lane.lane_id] = [{"status": "expired-token", "readings": ()}]
    turns = []

    def turn(lane, purpose, holder, *, cancel, deadline):
        turns.append(purpose)
        return Outcome(OutcomeClass.AUTH_DEAD, "refresh token was revoked")

    timer.turn = turn
    timer.probe_cycle()
    clock.advance(3600)
    timer.probe_cycle()
    assert turns == ["heal"]
    assert adapter.calls == [lane.lane_id]
    assert timer.snapshot()["lanes"][0]["verdict"] == "auth-revoked"


def test_duplicate_account_disables_later_binding_and_alerts_both_homes(rig):
    """C-23.45: probe-cycle identity checks keep one canonical lane and name both homes in an alert."""
    timer, store, _, adapter, enroll = rig
    first = enroll("codex-2", account="codex:shared")
    later = enroll("codex-1", account="codex:shared")
    notices = []
    timer.alerts.deliver = lambda notice: notices.append(notice) or True
    timer.probe_cycle()
    assert adapter.calls == [first.lane_id]
    assert store.get_lane(first.lane_id).enabled
    assert not store.get_lane(later.lane_id).enabled
    duplicate = timer.metadata[later.lane_id]
    assert duplicate["identity_status"] == "non-canonical"
    assert duplicate["duplicate_of"] == first.home
    assert any(notice["severity"] == "critical" and first.home in notice["body"]
               and later.home in notice["body"] for notice in notices)


def test_c9_9_a_claude_lane_is_probed_through_the_usage_endpoint_never_a_model_turn(rig, monkeypatch):
    timer, store, clock, adapter, enroll = rig
    monkeypatch.setenv("SF_TEST_TOKEN", "token-value")
    lane = Lane("claude-1", "claude", "claude:uuid-a:uuid-o", Credential("claude", "SF_TEST_TOKEN", "env"),
                None, LaneOwner.V2, False, True)
    store.put_lane(lane)
    timer.turn = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("a model turn was spent"))
    adapter.responses["claude-1"] = [{"status": "ok", "limit_reached": False, "readings": (
        Reading("claude-1", "account", "seven_day", .88, iso(clock() + timedelta(days=4)),
                ReadingLabel.PROVIDER, "oauth-usage", iso(clock())),
        Reading("claude-1", "claude-fable-5-1", "seven_day", .24, iso(clock() + timedelta(days=4)),
                ReadingLabel.PROVIDER, "oauth-usage", iso(clock())))}]
    timer.probe_cycle()
    assert adapter.calls == ["claude-1"]
    rows = store.query("SELECT scope, window, utilization, source FROM readings WHERE lane_id='claude-1' ORDER BY scope")
    assert [(r["scope"], r["window"], r["utilization"], r["source"]) for r in rows] == [
        ("account", "seven_day", .88, "oauth-usage"), ("claude-fable-5-1", "seven_day", .24, "oauth-usage")]


def test_c9_9_retry_after_holds_the_lane_until_the_server_said_so(rig, monkeypatch):
    timer, store, clock, adapter, enroll = rig
    monkeypatch.setenv("SF_TEST_TOKEN", "token-value")
    store.put_lane(Lane("claude-1", "claude", "claude:uuid-a:uuid-o", Credential("claude", "SF_TEST_TOKEN", "env"),
                        None, LaneOwner.V2, False, True))
    adapter.responses["claude-1"] = [{"status": "rate-limited", "readings": (), "retry_after_s": 3035}]
    timer.probe_cycle()
    assert adapter.calls == ["claude-1"]
    assert timer.metadata["claude-1"]["retry_after_until"] == iso(clock() + timedelta(seconds=3035))
    assert not store.query("SELECT 1 FROM closures WHERE lane_id='claude-1'")
    clock.advance(600)
    timer.probe_cycle()
    assert adapter.calls == ["claude-1"]          # inside Retry-After: not asked again
    clock.advance(3000)
    timer.probe_cycle()
    assert adapter.calls == ["claude-1", "claude-1"]
