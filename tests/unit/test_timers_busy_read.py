"""C-18.3: busy Claude and Codex lanes are read beside their attempts, holding nothing.

Incident, 2026-09-30: codex-4's operator hold was released at 21:11:00Z with a
fresh week from a reset credit spent at 20:41:57Z. Admission placed work on it
back to back from 21:11:15Z, one attempt at a time ("eligible but unmeasured").
`Timers._reserve` refuses a lane with an attempt in flight or a lane lease, so
none of the 77 probe cycles that followed read it, the credit's confirmed
override was never settled, and the lane stayed unmeasured at one attempt while
every other Codex lane was at its floor.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet.adapters.codex import CodexAdapter, WHAM_USAGE_URL
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Outcome,
                                OutcomeClass, Reading, ReadingLabel)
from subfleet.daemon import Daemon
from subfleet.retention import maintenance
from subfleet.store import Store
from subfleet.timers import BUSY, Timers, iso
from tests.unit.test_codex_probe import SAVED_WHAM, _auth

NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
#: Statuses a busy read may publish, and every other answer `probe_status` gives.
USAGE = ("ok", "limited")
WITHHELD = ("auth-dead", "revoked", "expired-token", "no-auth", "network-error", "http-error",
            "invalid-response", "unknown")
LATCHES = ("auth-dead", "revoked", "auth-revoked", "expired-token", "no-auth")


class Clock:
    def __init__(self):
        self.at = NOW

    def __call__(self):
        return self.at

    def advance(self, seconds=60):
        self.at += timedelta(seconds=seconds)


def answer(lane, clock, status="ok", *, utilization=.1, limit_reached=None, allowed=True, account=...):
    """What `CodexAdapter.probe_status` returns for a wham read of `lane`."""
    at = iso(clock())
    readings = () if status not in USAGE else (
        Reading(lane.lane_id, "account", "seven_day", utilization, iso(clock() + timedelta(days=6)),
                ReadingLabel.PROVIDER, "wham", at),
        Reading(lane.lane_id, "account", "five_hour", utilization / 2, iso(clock() + timedelta(hours=4)),
                ReadingLabel.PROVIDER, "wham", at))
    return {"status": status, "readings": readings, "checked_at": at, "allowed": allowed,
            "limit_reached": (status == "limited") if limit_reached is None else limit_reached,
            "account_key": lane.account_key if account is ... else account}


class Wham:
    """A scripted usage read. `during[lane]` runs inside the read, on the reader's thread."""

    def __init__(self, clock, store):
        self.clock, self.store = clock, store
        self.calls, self.responses, self.during, self.leases_seen = [], {}, {}, []

    def probe_status(self, lane, env):
        self.calls.append(lane.lane_id)
        self.leases_seen.append(leases(self.store))
        for action in self.during.pop(lane.lane_id, ()):
            action()
        scripted = self.responses.get(lane.lane_id)
        result = scripted.pop(0) if scripted else answer(lane, self.clock)
        if isinstance(result, BaseException):
            raise result
        return result

    def list_reset_credits(self, *args, **kwargs):
        raise AssertionError("no reset credit is listed in these tests")


def leases(store):
    return sorted((row["lease_key"], row["holder"]) for row in store.list_leases())


@contextmanager
def make_rig(root: Path, **caps):
    clock = Clock()
    policy = {"models": {"haiku": {"id": "claude-haiku-4-5-20251001"}},
              "timers": {"probe_interval_s": 60, "keepalive_interval_s": 18300},
              "reset_credits": {"enabled": False}, "alerts": {}, "reserve": {"usage_spacing_s": 0},
              # Main's defaults spelled out, so the release line's uncapped defaults
              # do not change what "the measured cap" means here.
              "caps": {"max_in_flight_per_lane": 2, "max_in_flight_unmeasured": 1, **caps}}
    with Store(root / "state.sqlite3") as store:
        wham = Wham(clock, store)
        timer = Timers(store, root, policy, adapter_factory=lambda _: wham, now=clock)
        turns = []
        timer.turn = lambda lane, purpose, *args, **kwargs: turns.append((lane.lane_id, purpose))
        try:
            yield Rig(timer, store, clock, wham, root, turns)
        finally:
            timer.stop()


class Rig:
    def __init__(self, timer, store, clock, wham, root, turns):
        self.timer, self.store, self.clock, self.wham, self.root, self.turns = timer, store, clock, wham, root, turns

    def enroll(self, lane_id="codex-4", *, provider="codex", desktop=False):
        home = self.root / lane_id
        home.mkdir(exist_ok=True)
        (home / "auth.json").write_text(json.dumps({"last_refresh": "first"}))
        lane = Lane(lane_id, provider, f"{provider}:{lane_id}", Credential(provider, str(home), "home"),
                    str(home), LaneOwner.V2, desktop, True)
        self.store.put_lane(lane)
        return lane

    def occupy(self, lane, how="attempt", n=1):
        """Work on the lane as admission leaves it: attempt rows and their slot leases."""
        if how == "probe":                   # an admission probe (C-11.4) holds `slot:0`
            self.store.acquire_lease(f"lane:{lane.lane_id}:slot:0", "probe:0123456789abcdef")
            return
        for index in range(n):
            job = f"job-{lane.lane_id}-{index}"
            attempt = f"{job}/a1"
            if how in ("attempt", "both", "turn"):       # a conversation turn is a job of kind `turn`
                self.store.add_job(job_id=job, request_id=job, payload_digest="d",
                                   kind="turn" if how == "turn" else "dispatch",
                                   state="running", workdir=str(self.root), prompt_path="/p", sandbox="read-only")
                self.store.add_attempt(attempt_id=attempt, job_id=job, seq=1, lane_id=lane.lane_id,
                                       model_requested="gpt-6.1-sol", state="running")
            if how in ("lease", "both"):
                self.store.acquire_lease(f"lane:{lane.lane_id}:slot:{index + 1}", attempt)
            if how == "turn-lease":                      # and, on the release line, its lease is `slot:turn-<n>`
                self.store.acquire_lease(f"lane:{lane.lane_id}:slot:turn-{index}", attempt)

    def ends_limited(self, lane, until, *, attempt=None):
        """What `_finalize` does for an attempt that ends `limited`: its outcome, then its closure."""
        if attempt is None:                              # one reserved and ended within the read
            attempt = f"job-{lane.lane_id}-late/a1"
            self.store.add_job(job_id=attempt[:-3], request_id=attempt, payload_digest="d", kind="dispatch",
                               state="failed", workdir=str(self.root), prompt_path="/p", sandbox="read-only")
            self.store.add_attempt(attempt_id=attempt, job_id=attempt[:-3], seq=1, lane_id=lane.lane_id,
                                   model_requested="gpt-6.1-sol", state="running")
        with self.store.transaction("attempt.accepted", attempt_id=attempt) as tx:
            tx.execute("UPDATE attempts SET state='failed',outcome_class='limited' WHERE attempt_id=?", (attempt,))
            self.store.add_closure(Closure(lane.lane_id, "account", until, ClosureReason.PROVIDER_LIMIT,
                                           ClockSource.REPORTED, attempt))
            tx.execute("DELETE FROM leases WHERE holder=?", (attempt,))

    def prune(self, job):
        """What retention does to a finished job: its attempts and the job row go."""
        with self.store.transaction("retention.pruned", job_id=job) as tx:
            tx.execute("DELETE FROM attempts WHERE job_id=?", (job,))
            tx.execute("DELETE FROM jobs WHERE job_id=?", (job,))

    def override(self, lane, *, ago=60):
        """A confirmed reset-credit consume on the lane (C-23.16), not yet settled."""
        at = iso(self.clock() - timedelta(seconds=ago))
        self.store.add_action(action_id=f"credit-{lane.lane_id}", kind="reset-credit",
                              op_key=f"{lane.account_key}:gift-1", subject=lane.lane_id, state="confirmed",
                              request_json=json.dumps({"lane_id": lane.lane_id, "account_key": lane.account_key}),
                              result_json=json.dumps({"windows_reset": 1}), created_at=at, updated_at=at)
        assert self.timer.actions.confirmed_override(lane.lane_id, now=self.clock())

    def wham_readings(self, lane):
        return self.store.query("SELECT * FROM readings WHERE lane_id=? AND source='wham' ORDER BY reading_id",
                                (lane.lane_id,))

    def open_closures(self, lane):
        return {(row["scope"], row["reason"], row["until_at"]) for row in self.store.query(
            "SELECT * FROM closures WHERE lane_id=? AND released_at IS NULL", (lane.lane_id,))}

    def row(self, lane):
        return next(row for row in self.timer.snapshot()["lanes"] if row["lane_id"] == lane.lane_id)

    def cycle_event(self):
        # Each `add_event` is also its transaction's event; the one with the data is the cycle's.
        return next(data for row in self.store.query("SELECT data_json FROM events WHERE kind='timer.cycle' "
                                                     "ORDER BY event_id DESC")
                    if "lanes" in (data := json.loads(row["data_json"])))

    def events(self, kind, lane):
        return self.store.query("SELECT * FROM events WHERE kind=? AND lane_id=?", (kind, lane.lane_id))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with make_rig(tmp_path) as value:
        yield value


OCCUPANCY = ("attempt", "lease", "both", "probe", "turn", "turn-lease")


@pytest.mark.parametrize("how", OCCUPANCY)
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_c18_3_a_busy_lane_is_read_on_its_first_cycle(rig, how, provider):
    """C-18.3: work on the lane (an attempt, a slot lease, an admission probe's `slot:0`, a conversation
    turn or its `slot:turn-<n>` lease) no longer hides it, and none of it makes the read take `slot:0`."""
    lane = rig.enroll(provider=provider)
    rig.occupy(lane, how)
    before = leases(rig.store)
    rig.timer.probe_cycle()
    assert rig.wham.calls == [lane.lane_id]
    assert [(row["window"], row["label"], row["attempt_id"]) for row in rig.wham_readings(lane)] == [
        ("seven_day", "provider", None), ("five_hour", "provider", None)]
    assert leases(rig.store) == before
    assert rig.events("timer.reservation", lane) == []
    assert rig.timer.metadata[lane.lane_id]["verdict"] == "ok"
    event = rig.cycle_event()
    assert event["lanes"] == event["busy"] == [lane.lane_id] and event["deferred"] == {}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_c18_3_an_always_busy_lane_is_read_on_every_tick(rig, monkeypatch, provider):
    """C-18.3, C-18.1: the 2026-09-30 starvation. A lane never idle at a cycle is still read every cycle."""
    lane = rig.enroll(provider=provider)
    rig.occupy(lane, "both")
    monkeypatch.setattr("subfleet.timers.time.monotonic", lambda: rig.clock().timestamp())
    pending = []
    monkeypatch.setattr(rig.timer._cycles, "submit", lambda fn, *args: pending.append((fn, args)))
    rig.timer.start()
    for tick in range(1, 4):
        rig.clock.advance(rig.timer.intervals["probe"])
        rig.timer.tick()
        while pending:
            fn, args = pending.pop(0)
            fn(*args)
        assert rig.wham.calls == [lane.lane_id] * tick
        assert len([row for row in rig.wham_readings(lane) if row["window"] == "seven_day"]) == tick
        assert rig.row(lane)["measured"]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_c18_1_busy_lanes_are_read_on_consecutive_requested_cycles(rig, provider):
    """C-18.1: the preceding probe and fresh attempt evidence never skip a requested cycle."""
    lane = rig.enroll(provider=provider)
    rig.occupy(lane, "both")
    rig.store.add_reading(Reading(lane.lane_id, "account", "seven_day", .7,
                                  iso(rig.clock() + timedelta(days=2)), ReadingLabel.PROVIDER,
                                  "rate-limit-event", iso(rig.clock()), attempt_id=f"job-{lane.lane_id}-0/a1"))
    rig.timer.probe_cycle()
    rig.timer.probe_cycle()
    assert rig.wham.calls == [lane.lane_id, lane.lane_id]
    assert len(rig.wham_readings(lane)) == 4
    assert rig.events("timer.reservation", lane) == []


def test_c9_9_a_busy_claude_lane_uses_the_no_turn_oauth_sensor(rig, monkeypatch):
    """C-9.9, C-18.3: a busy Claude lane reads all OAuth windows, with Claude pacing and no turn."""
    from subfleet.adapters.claude import ClaudeAdapter, IdentityCheck, IdentityStatus, OAUTH_USAGE_URL
    from tests.unit.test_claude_usage import PAYLOAD

    lane = rig.enroll("claude-4", provider="claude")
    rig.occupy(lane, "both")
    requests, paced = [], []

    def opener(request, timeout):
        requests.append(request.full_url)
        return 200, json.dumps(PAYLOAD).encode()

    real = ClaudeAdapter(usage_opener=opener, now=rig.clock)
    real.lane_identity_check = lambda lane, env: IdentityCheck(
        IdentityStatus.VERIFIED, "ok", "x", "y", None, None, None, "t")
    real.probe_with_model = lambda *args, **kwargs: pytest.fail("a model turn was spent")
    rig.timer.adapter_factory = lambda provider: real if provider == "claude" else pytest.fail(provider)
    monkeypatch.setattr("subfleet.timers.resolve_credential", lambda _: {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"})
    monkeypatch.setattr(rig.timer, "_pace_usage", lambda: paced.append(True))
    before = leases(rig.store)
    rig.timer.probe_cycle()

    assert requests == [OAUTH_USAGE_URL] and paced == [True]
    readings = rig.store.list_readings(lane.lane_id)
    assert {(r["scope"], r["window"], r["source"]) for r in readings} == {
        ("account", "five_hour", "oauth-usage"), ("account", "seven_day", "oauth-usage"),
        ("claude-fable-5-1", "seven_day", "oauth-usage")}
    assert leases(rig.store) == before
    assert rig.turns == [] and rig.events("timer.reservation", lane) == []


def test_c9_9_busy_claude_retry_after_keeps_usage_and_its_age(rig):
    """C-9.9, C-18.3: a busy Claude 429 keeps old usage, honours Retry-After, and spends no heal."""
    lane = rig.enroll("claude-4", provider="claude")
    rig.occupy(lane, "both")
    rig.timer.probe_cycle()
    previous = rig.wham_readings(lane)
    rig.clock.advance()
    rig.wham.responses[lane.lane_id] = [{"status": "rate-limited", "readings": (), "retry_after_s": 120}]
    rig.timer.probe_cycle()
    assert rig.wham_readings(lane) == previous
    assert rig.timer.metadata[lane.lane_id]["retry_after_until"] == iso(rig.clock() + timedelta(seconds=120))
    rig.clock.advance()
    rig.timer.probe_cycle()
    assert rig.wham.calls == [lane.lane_id, lane.lane_id]
    rig.clock.advance()
    rig.timer.probe_cycle()
    assert rig.wham.calls == [lane.lane_id] * 3
    assert rig.turns == [] and rig.events("timer.reservation", lane) == []


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_c18_1_an_idle_failed_read_keeps_usage_and_its_observation_time(rig, provider):
    """C-18.1, C-9.1: a failed idle read keeps prior usage and lets its age become stale honestly."""
    lane = rig.enroll(provider=provider)
    rig.timer.probe_cycle()
    previous = rig.wham_readings(lane)
    rig.clock.advance(180)
    rig.wham.responses[lane.lane_id] = [OSError("network is down")]
    rig.timer.probe_cycle()
    assert rig.wham_readings(lane) == previous
    assert all(reading["label"] == "stale-provider" for reading in rig.row(lane)["readings"]
               if reading["window"] in ("seven_day", "five_hour"))


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_c18_3_the_read_holds_no_slot_and_a_job_can_take_one_during_it(rig, provider):
    """C-18.3, C-6.10: no lease, no reserved probe, no `probe:` holder during the read; admission is free to place."""
    lane = rig.enroll(provider=provider)
    rig.occupy(lane, "both")
    before = leases(rig.store)
    seen = {}

    def admission_places_another():
        seen["holders"] = set(rig.timer.active_holders)
        seen["probe_leases"] = rig.store.query("SELECT 1 FROM leases WHERE holder LIKE 'probe:%'")
        assert rig.store.acquire_lease(f"lane:{lane.lane_id}:slot:2", "job-late/a1")

    rig.wham.during[lane.lane_id] = [admission_places_another]
    rig.timer.probe_cycle()
    assert rig.wham.leases_seen == [before]
    assert seen == {"holders": set(), "probe_leases": []}
    assert leases(rig.store) == sorted(before + [(f"lane:{lane.lane_id}:slot:2", "job-late/a1")])


@pytest.mark.parametrize("status", WITHHELD + ("raise-oserror", "raise-valueerror", "timeout"))
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_c18_3_a_busy_read_publishes_no_credential_verdict_and_runs_no_heal(rig, status, provider):
    """C-18.3, C-23.44, C-23.47: a busy lane's attempts renew its token and report a dead one themselves."""
    lane = rig.enroll(provider=provider)
    rig.timer.probe_cycle()                                   # idle: the verdict to keep
    published = dict(rig.timer.metadata[lane.lane_id])
    readings = rig.wham_readings(lane)
    rig.occupy(lane, "both")
    rig.clock.advance()
    if status == "timeout":
        rig.timer.policy["caps"]["probe_timeout_s"] = .03
        release = threading.Event()
        rig.wham.during[lane.lane_id] = [lambda: release.wait(2)]
    else:
        rig.wham.responses[lane.lane_id] = [OSError("reset by peer") if status == "raise-oserror" else
                                            ValueError("bad body") if status == "raise-valueerror" else
                                            answer(lane, rig.clock, status)]
    try:
        snapshot = rig.timer.probe_cycle()
    finally:
        if status == "timeout":
            release.set()
    expected = {"raise-oserror": "network-error", "raise-valueerror": "unknown", "timeout": "network-error"}
    assert rig.cycle_event()["deferred"] == {lane.lane_id: expected.get(status, status)}
    assert rig.store.get_lane(lane.lane_id).enabled
    assert rig.timer.metadata[lane.lane_id] == published
    assert rig.wham_readings(lane) == readings  # values, observation time and age are unchanged
    assert rig.turns == [] and rig.events("timer.heal", lane) == []
    assert snapshot["offline"] is (provider == "codex" and
                                   status in ("network-error", "raise-oserror", "timeout"))


def test_c18_3_a_withheld_busy_read_is_read_again_next_cycle(rig):
    """C-18.3: nothing withheld moves the lane's debounce; the next cycle asks again."""
    lane = rig.enroll()
    rig.occupy(lane)
    rig.wham.responses[lane.lane_id] = [answer(lane, rig.clock, "auth-dead")]
    rig.timer.probe_cycle()
    rig.clock.advance()
    rig.timer.probe_cycle()
    assert rig.wham.calls == [lane.lane_id, lane.lane_id]
    assert rig.timer.metadata[lane.lane_id]["verdict"] == "ok"


def test_c18_3_a_busy_read_that_overruns_fences_the_next_until_it_ends(rig):
    """C-18.3: the per-lane read fence holds for a busy read as for an idle one."""
    lane = rig.enroll()
    rig.occupy(lane)
    rig.timer.policy["caps"]["probe_timeout_s"] = .03
    release = threading.Event()
    rig.wham.during[lane.lane_id] = [lambda: release.wait(2)]
    try:
        rig.timer.probe_cycle()
        rig.clock.advance()
        rig.timer.probe_cycle()
        assert rig.wham.calls == [lane.lane_id]
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while lane.lane_id in rig.timer._io_busy and time.monotonic() < deadline:
        time.sleep(.01)
    rig.clock.advance()
    rig.timer.probe_cycle()
    assert rig.wham.calls == [lane.lane_id, lane.lane_id]
    assert rig.wham_readings(lane)


@pytest.mark.parametrize("status", USAGE + ("network-error", "auth-dead"))
def test_c18_3_a_busy_read_for_another_account_writes_nothing_of_it_and_disables_the_lane(rig, status):
    """C-18.3: no reading, settlement or closure change for the wrong account; the mismatch is published.
    The account is the home's `auth.json`'s, so a read whose request failed still names it, as on the
    idle path."""
    lane = rig.enroll()
    rig.store.put_closure(Closure(lane.lane_id, "account", iso(rig.clock() + timedelta(hours=1)),
                                  ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "wham"))
    rig.override(lane)
    rig.occupy(lane)
    closures = rig.open_closures(lane)
    rig.wham.responses[lane.lane_id] = [answer(lane, rig.clock, status, account="codex:someone-else")]
    rig.timer.probe_cycle()
    assert rig.wham_readings(lane) == []
    assert not rig.store.get_lane(lane.lane_id).enabled
    meta = rig.timer.metadata[lane.lane_id]
    assert meta["identity_status"] == "mismatch" and meta["observed_account_key"] == "codex:someone-else"
    assert rig.open_closures(lane) == closures
    assert rig.events("action.reconciled", lane) == []
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock())


def test_c18_3_the_real_adapter_reading_another_accounts_login_writes_no_readings(tmp_path, monkeypatch):
    """C-18.3, C-1.4: the account comes from the lane's own `auth.json`, read by `CodexAdapter.probe_status`."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return 200, SAVED_WHAM

    with make_rig(tmp_path) as rig:
        adapter = CodexAdapter(opener=opener, now=rig.clock)
        rig.timer.adapter_factory = lambda _: adapter
        lane = rig.enroll()
        (Path(lane.home) / "auth.json").write_text(json.dumps(_auth(account_id="another-account")))
        rig.occupy(lane, "both")
        rig.timer.probe_cycle()
        assert requests == [WHAM_USAGE_URL]
        assert rig.wham_readings(lane) == []
        assert rig.timer.metadata[lane.lane_id]["observed_account_key"] == "codex:another-account"
        assert not rig.store.get_lane(lane.lane_id).enabled


def test_c18_3_a_busy_read_settles_the_override_and_the_lane_takes_its_measured_cap(rig):
    """C-18.3, C-23.17, C-6.4: the live case. A credit's override held the busy lane unmeasured at one attempt;
    one busy read of the reopened account settles it, and the lane may take a second attempt."""
    lane = rig.enroll()
    rig.store.add_reading(Reading(lane.lane_id, "account", "seven_day", 1., iso(rig.clock() + timedelta(days=3)),
                                  ReadingLabel.PROVIDER, "wham", iso(rig.clock() - timedelta(hours=33))))
    rig.override(lane)
    rig.occupy(lane, "both")
    row = rig.row(lane)
    assert row["verdict"] == "admission-observed" and row["in_flight"] == 1
    assert row["dispatchable"] is False                       # unmeasured cap 1, one attempt in flight
    rig.timer.probe_cycle()
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock()) is None
    assert [data["action_id"] for event in rig.events("action.reconciled", lane)
            if "action_id" in (data := json.loads(event["data_json"]))] == [f"credit-{lane.lane_id}"]
    row = rig.row(lane)
    assert row["verdict"] == "ok" and row["in_flight"] == 1
    assert row["dispatchable"] is True                        # measured cap 2


@pytest.mark.parametrize("change", ["recorded", "extended"])
def test_c18_3_an_older_busy_read_never_releases_a_newer_limit(rig, change):
    """C-18.3, C-9.6: a closure an attempt records or extends during the read survives its publication,
    and so does the override; the readings and verdict are still published, and the next read settles."""
    lane = rig.enroll()
    rig.override(lane)
    rig.occupy(lane)
    soon, later = iso(rig.clock() + timedelta(minutes=30)), iso(rig.clock() + timedelta(hours=2))
    if change == "extended":
        rig.store.put_closure(Closure(lane.lane_id, "account", soon, ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.GUESSED, "attempt"))
    rig.wham.during[lane.lane_id] = [lambda: rig.store.put_closure(Closure(
        lane.lane_id, "account", later, ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "attempt"))]
    rig.timer.probe_cycle()
    assert rig.open_closures(lane) == {("account", "provider-limit", later)}
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock())
    assert len(rig.wham_readings(lane)) == 2 and rig.timer.metadata[lane.lane_id]["verdict"] == "ok"
    rig.clock.advance()
    rig.timer.probe_cycle()                                   # nothing new during this read
    assert rig.open_closures(lane) == set()
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock()) is None


@pytest.mark.parametrize("until", ["same", "earlier"])
@pytest.mark.parametrize("override", [False, True])
def test_c18_3_a_limit_an_attempt_reports_again_during_the_read_is_kept(rig, until, override):
    """C-18.3, C-9.6 (review of PR #96): a limit that ends no later than the open row's changes nothing
    in that row. The report's `closure.recorded` event is its trace, and the older read releases nothing."""
    lane = rig.enroll()
    reset = rig.clock() + timedelta(hours=2)
    rig.store.put_closure(Closure(lane.lane_id, "account", iso(reset), ClosureReason.PROVIDER_LIMIT,
                                  ClockSource.REPORTED, "attempt-1"))
    if override:
        rig.override(lane)
    rig.occupy(lane, "both", n=2)
    closures = rig.open_closures(lane)
    again = iso(reset if until == "same" else reset - timedelta(minutes=5))
    rig.wham.during[lane.lane_id] = [lambda: rig.ends_limited(lane, again, attempt=f"job-{lane.lane_id}-1/a1")]
    rig.wham.responses[lane.lane_id] = [answer(lane, rig.clock, "ok", utilization=.5, limit_reached=False)]
    rig.timer.probe_cycle()
    assert rig.open_closures(lane) == closures
    assert len(rig.wham_readings(lane)) == 2 and rig.events("action.reconciled", lane) == []
    event = rig.cycle_event()
    assert event["fenced"] == [lane.lane_id] and event["deferred"] == {}     # published, nothing released
    if override:
        assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock())


@pytest.mark.parametrize("until", ["same", "earlier"])
def test_c18_3_a_limit_an_admission_probe_reports_again_during_the_read_is_kept(rig, until):
    """C-18.3, C-11.4 (reviews of PR #96 and #97): an admission probe has no attempt row. The real
    `Daemon._finish_probe` records its `limited` outcome's closure, and that report's event is the trace."""
    lane = rig.enroll()
    reset = rig.clock() + timedelta(hours=2)
    rig.store.put_closure(Closure(lane.lane_id, "account", iso(reset), ClosureReason.PROVIDER_LIMIT,
                                  ClockSource.REPORTED, "attempt-1"))
    rig.override(lane)
    rig.occupy(lane, "both")
    rig.occupy(lane, "probe")
    closures = rig.open_closures(lane)
    again = Closure(lane.lane_id, "account", iso(reset if until == "same" else reset - timedelta(minutes=5)),
                    ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "probe")
    record = {"holder": "probe:0123456789abcdef", "job_id": f"job-{lane.lane_id}-0", "lane_id": lane.lane_id,
              "model_id": "gpt-6.1-sol", "directory": str(rig.root / "probe"), "state": "running"}
    daemon = object.__new__(Daemon)
    daemon.store, daemon.timers = rig.store, rig.timer
    rig.wham.during[lane.lane_id] = [lambda: daemon._finish_probe(
        record, Outcome(OutcomeClass.LIMITED, "provider rejected again", closure=again))]
    rig.wham.responses[lane.lane_id] = [answer(lane, rig.clock, "ok", utilization=.5, limit_reached=False)]
    rig.timer.probe_cycle()
    assert record["state"] == "completed" and rig.cycle_event()["fenced"] == [lane.lane_id]
    assert rig.open_closures(lane) == closures and len(rig.wham_readings(lane)) == 2
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock())
    rig.clock.advance()
    rig.timer.probe_cycle()                                   # nothing reported during this read
    assert rig.open_closures(lane) == set() and rig.cycle_event()["fenced"] == []
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock()) is None


def test_c18_3_a_reported_limit_survives_retention_pruning_its_attempt(rig):
    """C-18.3 (review of PR #97): the attempt that reported the limit again is pruned by the real
    retention pass before the publication. The report's event is not, so the older read releases nothing."""
    lane = rig.enroll()
    reset = iso(rig.clock() + timedelta(hours=2))
    rig.store.put_closure(Closure(lane.lane_id, "account", reset, ClosureReason.PROVIDER_LIMIT,
                                  ClockSource.REPORTED, "attempt-1"))
    rig.override(lane)
    rig.occupy(lane, "both", n=2)
    closures, oldest, newer = rig.open_closures(lane), f"job-{lane.lane_id}-0", f"job-{lane.lane_id}-1"
    with rig.store.transaction("fixture.order-jobs") as tx:
        tx.execute("UPDATE jobs SET created_at=?,max_attempts=1 WHERE job_id=?", (iso(rig.clock() - timedelta(days=1)), oldest))
        tx.execute("UPDATE jobs SET created_at=? WHERE job_id=?", (iso(rig.clock()), newer))

    def ends_limited_and_is_pruned():
        rig.ends_limited(lane, reset, attempt=f"{oldest}/a1")
        with rig.store.transaction("fixture.job-terminal") as tx:
            tx.execute("UPDATE jobs SET state='failed',finished_at=? WHERE job_id=?", (iso(rig.clock()), oldest))
            tx.execute("UPDATE attempts SET finished_at=? WHERE job_id=?", (iso(rig.clock()), oldest))
        assert maintenance(rig.store, rig.root, max_jobs=1)["pruned"] == [oldest]

    rig.wham.during[lane.lane_id] = [ends_limited_and_is_pruned]
    rig.wham.responses[lane.lane_id] = [answer(lane, rig.clock, "ok", utilization=.5, limit_reached=False)]
    rig.timer.probe_cycle()
    assert rig.store.get_attempt(f"{oldest}/a1") is None and rig.store.get_job(newer)["state"] == "running"
    assert rig.open_closures(lane) == closures
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock())


def test_c18_3_only_a_closure_report_fences_a_release(rig):
    """C-18.3 (review of PR #96): a busy lane writes events all the time. An attempt reserved on it, a
    reading and a lane update during the read are not limit reports, and the read still settles."""
    lane = rig.enroll()
    rig.store.put_closure(Closure(lane.lane_id, "account", iso(rig.clock() + timedelta(hours=1)),
                                  ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "wham"))
    rig.override(lane)
    rig.occupy(lane, "both")

    def the_lane_is_busy():
        rig.store.add_job(job_id="job-next", request_id="job-next", payload_digest="d", kind="dispatch",
                          state="running", workdir=str(rig.root), prompt_path="/p", sandbox="read-only")
        with rig.store.transaction("attempt.reserved", job_id="job-next", lane_id=lane.lane_id):
            rig.store.add_attempt(attempt_id="job-next/a1", job_id="job-next", seq=1, lane_id=lane.lane_id,
                                  model_requested="gpt-6.1-sol", state="reserved")
        rig.store.add_reading(Reading(lane.lane_id, "gpt-6.1-sol", "admission", None, None,
                                      ReadingLabel.ADMISSION_OBSERVED, "probe", iso(rig.clock())))
        rig.store.add_event("lane.noted", lane_id=lane.lane_id, data={"note": "not a limit"})
        rig.store.update_lane(lane.lane_id, plan="pro")

    rig.wham.during[lane.lane_id] = [the_lane_is_busy]
    rig.timer.probe_cycle()
    assert rig.store.query("SELECT 1 FROM events WHERE lane_id=? AND kind='attempt.reserved'", (lane.lane_id,))
    assert rig.store.query("SELECT 1 FROM readings WHERE lane_id=? AND label='admission-observed'", (lane.lane_id,))
    assert rig.open_closures(lane) == set() and rig.cycle_event()["fenced"] == []
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock()) is None


def test_c18_3_another_lanes_limit_fences_nothing_here(rig):
    """C-18.3: the mark is the store's newest event, and only this lane's reports after it count."""
    lane, other = rig.enroll("codex-4"), rig.enroll("codex-5")
    rig.store.put_closure(Closure(lane.lane_id, "account", iso(rig.clock() + timedelta(hours=1)),
                                  ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "wham"))
    rig.override(lane)
    for each in (lane, other):
        rig.occupy(each)
    rig.wham.during[lane.lane_id] = [lambda: rig.ends_limited(other, iso(rig.clock() + timedelta(hours=2)))]
    rig.timer.probe_cycle()
    assert rig.open_closures(lane) == set()
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock()) is None


@pytest.mark.parametrize("report", ["new", "later", "same", "earlier"])
def test_c18_3_every_closure_report_leaves_its_event(rig, report):
    """C-18.3, C-3.2: `put_closure` records an event for a report that changes no row, and the row is as it was."""
    lane = rig.enroll()
    until = rig.clock() + timedelta(hours=2)
    if report != "new":
        rig.store.put_closure(Closure(lane.lane_id, "account", iso(until), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "first"))
    row = rig.store.query("SELECT * FROM closures WHERE lane_id=?", (lane.lane_id,))
    mark = rig.timer._mark()
    again = iso(until + {"later": timedelta(hours=1), "earlier": -timedelta(minutes=5)}.get(report, timedelta()))
    rig.store.put_closure(Closure(lane.lane_id, "account", again, ClosureReason.PROVIDER_LIMIT,
                                  ClockSource.GUESSED, "again"))
    assert rig.store.query("SELECT 1 FROM events WHERE kind='closure.recorded' AND lane_id=? AND event_id>?",
                           (lane.lane_id, mark))
    if report in ("same", "earlier"):
        assert rig.store.query("SELECT * FROM closures WHERE lane_id=?", (lane.lane_id,)) == row


def test_c18_3_a_limit_another_thread_records_waits_for_the_publication(rig, monkeypatch):
    """C-18.3 (reviews of PR #96 and #97): judged, then published outside one transaction, an attempt's
    limit recorded in between was released. The publication holds the store's write lock, so the
    finalizer's write lands after the commit and stays."""
    lane = rig.enroll()
    rig.occupy(lane, "both")
    reset = iso(rig.clock() + timedelta(hours=2))
    judge, done = rig.timer._publishable, threading.Event()

    def finalizer():
        rig.store.add_closure(Closure(lane.lane_id, "account", reset, ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "attempt-2"))
        done.set()

    def judged_then_raced(*args):
        verdict = judge(*args)
        threading.Thread(target=finalizer, daemon=True).start()
        assert not done.wait(.3)                              # it cannot write until the commit
        return verdict

    monkeypatch.setattr(rig.timer, "_publishable", judged_then_raced)
    rig.wham.responses[lane.lane_id] = [answer(lane, rig.clock, "ok", utilization=.5, limit_reached=False)]
    rig.timer.probe_cycle()
    assert done.wait(2)
    assert rig.open_closures(lane) == {("account", "provider-limit", reset)}


def test_c18_3_whatever_raises_before_the_read_is_a_withheld_read_not_a_failed_cycle(rig, monkeypatch):
    """C-18.3, C-18.1 (review of PR #96): an error reading the lane's limits is this lane's read failing.
    The cycle goes on, the idle lane is published and its `slot:0` is released."""
    busy, idle = rig.enroll("codex-4"), rig.enroll("codex-5")
    rig.occupy(busy, "both")
    held = leases(rig.store)
    monkeypatch.setattr(rig.timer, "_mark", lambda: (_ for _ in ()).throw(RuntimeError("database disk image is malformed")))
    rig.timer.probe_cycle()
    assert rig.cycle_event()["deferred"] == {busy.lane_id: "unknown"}
    assert rig.wham.calls == [idle.lane_id] and rig.wham_readings(idle)
    assert leases(rig.store) == held and rig.timer._probe_holders == {}


@pytest.mark.parametrize("change", [{"owner": "v1"}, {"desktop": 1}, "operator-hold", "auth-dead"])
def test_c18_3_nothing_is_published_for_a_lane_no_longer_read(rig, change):
    """C-18.3 (reviews of PR #96): publication asks exactly what `_claim` asks (`_never_read`). A lane
    transferred, made the desktop's, held or closed `auth-dead` during its read is not published."""
    lane = rig.enroll()
    rig.occupy(lane, "both")
    rig.wham.during[lane.lane_id] = [
        (lambda: rig.store.update_lane(lane.lane_id, **change)) if isinstance(change, dict) else
        (lambda: rig.store.put_closure(Closure(lane.lane_id, "account", "2099-12-31T00:00:00Z", ClosureReason(change),
                                               ClockSource.REPORTED, "operator")))]
    rig.timer.probe_cycle()
    assert rig.wham_readings(lane) == [] and lane.lane_id not in rig.timer.metadata
    assert rig.cycle_event()["deferred"] == {lane.lane_id: "ok"}


def test_c18_3_an_answer_that_is_not_a_mapping_is_a_withheld_read(rig):
    """C-18.3: a malformed adapter answer is a failed read, not a failed cycle."""
    lane = rig.enroll()
    rig.occupy(lane)
    rig.wham.responses[lane.lane_id] = [None]
    rig.timer.probe_cycle()
    assert rig.cycle_event()["deferred"] == {lane.lane_id: "unknown"}


def test_c18_3_nothing_is_published_for_a_lane_disabled_during_its_read(rig):
    """C-18.3, C-23.44: an attempt that ends `auth-dead` mid-read keeps the lane's verdict."""
    lane = rig.enroll()
    rig.occupy(lane)

    def attempt_finds_it_dead():
        rig.store.update_lane(lane.lane_id, enabled=0)
        rig.timer.record_auth_dead(lane.lane_id)

    rig.wham.during[lane.lane_id] = [attempt_finds_it_dead]
    rig.timer.probe_cycle()
    assert rig.wham_readings(lane) == []
    assert rig.timer.metadata[lane.lane_id] == {"verdict": "auth-dead", "probe_status": "auth-dead"}
    assert rig.cycle_event()["deferred"] == {lane.lane_id: "ok"}


def test_c18_3_a_busy_read_is_judged_and_published_in_one_transaction(rig, monkeypatch):
    """C-18.3: the store's write lock stands in for the slot. The judgement, the settlement and the
    publication share one transaction, so no attempt can record a closure between them."""
    lane = rig.enroll()
    rig.override(lane)
    rig.occupy(lane)
    seen = {}
    judge, settle, publish = rig.timer._publishable, rig.timer.actions.settle_by_usage, rig.timer._persist
    monkeypatch.setattr(rig.timer, "_publishable", lambda *a: seen.setdefault("judged", rig.store._depth) and judge(*a))
    monkeypatch.setattr(rig.timer.actions, "settle_by_usage",
                        lambda *a, **k: seen.setdefault("settled", rig.store._depth) and settle(*a, **k))
    monkeypatch.setattr(rig.timer, "_persist", lambda *a, **k: seen.setdefault("published", rig.store._depth) and publish(*a, **k))
    rig.timer.probe_cycle()
    assert seen == {"judged": 1, "published": 1, "settled": 1}
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock()) is None
    assert rig.store._depth == 0


@pytest.mark.parametrize("verdict", ["none-yet", "from-an-idle-read"])
@pytest.mark.parametrize("fails", ["writing-the-verdict", "after-the-verdict"])
def test_c18_3_a_publication_that_raises_leaves_the_lane_as_it_stood(rig, monkeypatch, fails, verdict):
    """C-18.3 (Astra's finding 2): no fence is released early because there is none; a busy read's
    publication that fails rolls back whole, the settlement with it, and the next cycle publishes.
    The verdict the lane had (its email, its reset-credit counts) is put back, not dropped."""
    lane = rig.enroll()
    if verdict == "from-an-idle-read":
        rig.wham.responses[lane.lane_id] = [{**answer(lane, rig.clock), "email": "lane@example.invalid"}]
        rig.timer.probe_cycle()
        rig.clock.advance()
    stood, read = rig.timer.metadata.get(lane.lane_id), len(rig.wham_readings(lane))
    rig.store.put_closure(Closure(lane.lane_id, "account", iso(rig.clock() + timedelta(hours=1)),
                                  ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "wham"))
    rig.override(lane)
    rig.occupy(lane)
    closures, held = rig.open_closures(lane), leases(rig.store)
    with monkeypatch.context() as patch:
        if fails == "writing-the-verdict":
            add_event = rig.store.add_event
            patch.setattr(rig.store, "add_event", lambda kind, **fields: (_ for _ in ()).throw(
                RuntimeError("disk I/O error")) if kind == "timer.verdict" else add_event(kind, **fields))
        else:                                 # the verdict is in `metadata`; the commit never happens
            publish = rig.timer._persist
            patch.setattr(rig.timer, "_persist", lambda *a, **k: (publish(*a, **k), (_ for _ in ()).throw(
                RuntimeError("disk I/O error"))))
        with pytest.raises(RuntimeError):
            rig.timer.probe_cycle()
    assert len(rig.wham_readings(lane)) == read and rig.open_closures(lane) == closures
    assert rig.events("action.reconciled", lane) == []
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock())
    assert rig.timer.metadata.get(lane.lane_id) == stood and rig.store.get_lane(lane.lane_id).enabled
    assert rig.store._depth == 0 and leases(rig.store) == held
    rig.clock.advance()
    rig.timer.probe_cycle()
    assert len(rig.wham_readings(lane)) == read + 2 and rig.open_closures(lane) == set()
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock()) is None


def test_c18_3_a_failed_publication_never_overwrites_a_verdict_an_attempt_recorded(rig, monkeypatch):
    """C-18.3 (reviews of PR #96 and #97): the lane's previous verdict is read under the store lock. An
    attempt that disables the lane while the publication waits for that lock keeps its `auth-dead`
    verdict when the publication then fails; an `ok` read before the lock would be put back over it."""
    lane = rig.enroll()
    rig.timer.probe_cycle()                                   # an `ok` verdict from an idle read
    rig.occupy(lane, "both")
    rig.clock.advance()
    real, waited = rig.store.transaction, []

    @contextmanager
    def an_attempt_finalizes_first(kind, **fields):
        if kind == "timer.busy-read" and not waited:
            waited.append(kind)                               # the publication has not got the lock yet
            rig.store.update_lane(lane.lane_id, enabled=0)
            rig.timer.record_auth_dead(lane.lane_id)
        with real(kind, **fields) as conn:
            yield conn
            if kind == "timer.busy-read":
                raise RuntimeError("disk I/O error")

    monkeypatch.setattr(rig.store, "transaction", an_attempt_finalizes_first)
    with pytest.raises(RuntimeError):
        rig.timer.probe_cycle()
    assert waited and rig.timer.metadata[lane.lane_id] == {"verdict": "auth-dead", "probe_status": "auth-dead"}


def test_c18_3_a_commit_that_fails_puts_the_old_verdict_back(rig, monkeypatch):
    """C-18.3: the publication's own statements succeed and its commit does not. Nothing is in the store,
    and the verdict in `metadata` is the one the lane had."""
    lane = rig.enroll()
    rig.wham.responses[lane.lane_id] = [{**answer(lane, rig.clock), "email": "lane@example.invalid"}]
    rig.timer.probe_cycle()
    stood, read = rig.timer.metadata[lane.lane_id], len(rig.wham_readings(lane))
    rig.occupy(lane, "both")
    rig.clock.advance()
    real = rig.store.transaction

    @contextmanager
    def commit_fails(kind, **fields):
        with real(kind, **fields) as conn:
            yield conn
            if kind == "timer.busy-read":
                raise RuntimeError("disk I/O error")

    with monkeypatch.context() as patch:
        patch.setattr(rig.store, "transaction", commit_fails)
        with pytest.raises(RuntimeError):
            rig.timer.probe_cycle()
    assert rig.timer.metadata[lane.lane_id] is stood and len(rig.wham_readings(lane)) == read


@pytest.mark.parametrize("fails", ["the-commit", "the-publication"])
def test_c18_3_a_verdict_an_attempt_records_after_the_rollback_stays(rig, monkeypatch, fails):
    """C-18.3 (reviews of PR #96 and #97): once a failed publication has let the store lock go, an attempt
    can record `auth-dead`. When the publication's own statements failed, the old verdict was already put
    back under the lock; when the commit failed, it is put back only if this publication's is still in place."""
    lane = rig.enroll()
    rig.timer.probe_cycle()
    rig.occupy(lane, "both")
    rig.clock.advance()
    real, publish = rig.store.transaction, rig.timer._persist

    @contextmanager
    def fails_then_an_attempt_finalizes(kind, **fields):
        try:
            with real(kind, **fields) as conn:
                yield conn
                if kind == "timer.busy-read" and fails == "the-commit":
                    raise RuntimeError("disk I/O error")
        except RuntimeError:
            if kind == "timer.busy-read":                     # rolled back, the lock released
                rig.store.update_lane(lane.lane_id, enabled=0)
                rig.timer.record_auth_dead(lane.lane_id)
            raise

    monkeypatch.setattr(rig.store, "transaction", fails_then_an_attempt_finalizes)
    if fails == "the-publication":
        monkeypatch.setattr(rig.timer, "_persist", lambda *a, **k: (publish(*a, **k), (_ for _ in ()).throw(
            RuntimeError("disk I/O error"))))
    with pytest.raises(RuntimeError):
        rig.timer.probe_cycle()
    assert rig.timer.metadata[lane.lane_id] == {"verdict": "auth-dead", "probe_status": "auth-dead"}


def test_c18_3_the_check_and_the_put_back_are_one_step(rig, monkeypatch):
    """C-18.3 (review of PR #97): after a failed commit, an attempt's verdict cannot land between the
    check that this publication's verdict is still in place and the old one being put back."""
    lane = rig.enroll()
    rig.timer.probe_cycle()
    rig.occupy(lane, "both")
    rig.clock.advance()
    real, put, landed, raced = rig.store.transaction, rig.timer._set_verdict, threading.Event(), []

    @contextmanager
    def commit_fails(kind, **fields):
        with real(kind, **fields) as conn:
            yield conn
            if kind == "timer.busy-read":
                raced.append("failed")
                raise RuntimeError("disk I/O error")

    def an_attempt_records_during_the_put_back(lane_id, verdict):
        if raced == ["failed"]:                               # the put-back after the failed commit
            raced.append("putting-back")
            threading.Thread(target=lambda: (put(lane_id, {"verdict": "auth-dead", "probe_status": "auth-dead"}),
                                             landed.set()), daemon=True).start()
            assert not landed.wait(.3)                        # it waits for the check and the put-back
        return put(lane_id, verdict)

    monkeypatch.setattr(rig.store, "transaction", commit_fails)
    monkeypatch.setattr(rig.timer, "_set_verdict", an_attempt_records_during_the_put_back)
    with pytest.raises(RuntimeError):
        rig.timer.probe_cycle()
    assert landed.wait(2) and raced == ["failed", "putting-back"]
    assert rig.timer.metadata[lane.lane_id] == {"verdict": "auth-dead", "probe_status": "auth-dead"}


class AfterRead(dict):
    """`Timers.metadata` whose next read of one lane, once armed, runs an action after the value is read."""

    armed = None

    def get(self, key, default=None):
        value = super().get(key, default)
        if self.armed and key == self.armed[0]:
            action, self.armed = self.armed[1], None
            action()
        return value


def test_c18_3_an_attempt_that_finalizes_right_after_the_check_keeps_its_verdict(rig, monkeypatch):
    """C-18.3 (review of PR #96): the commit fails, the check reads this publication's verdict, and an
    attempt that ends `auth-dead` finalizes at that instant, in its own transaction, on its own thread.
    The check is under the verdicts' lock too, and `record_auth_dead` writes through it, so the attempt's
    verdict lands after the put-back and stays."""
    lane = rig.enroll()
    rig.timer.probe_cycle()                                   # `ok` from an idle read
    rig.occupy(lane, "both")
    rig.clock.advance()
    rig.timer.metadata = AfterRead(rig.timer.metadata)
    real, landed = rig.store.transaction, threading.Event()

    def finalizer():                                          # `Daemon._finalize` for an `auth-dead` attempt
        with real("attempt.accepted", lane_id=lane.lane_id):
            rig.store.update_lane(lane.lane_id, enabled=0)
            rig.timer.record_auth_dead(lane.lane_id)
        landed.set()

    def race():
        threading.Thread(target=finalizer, daemon=True).start()
        landed.wait(.5)                                       # lands now unless the verdicts' lock holds it off

    @contextmanager
    def commit_fails(kind, **fields):
        try:
            with real(kind, **fields) as conn:
                yield conn
                if kind == "timer.busy-read":
                    raise RuntimeError("disk I/O error")
        except RuntimeError:
            if kind == "timer.busy-read":                     # rolled back, the store lock released
                rig.timer.metadata.armed = (lane.lane_id, race)   # the next read is the check
            raise

    monkeypatch.setattr(rig.store, "transaction", commit_fails)
    with pytest.raises(RuntimeError):
        rig.timer.probe_cycle()
    assert landed.wait(5)
    assert not rig.store.get_lane(lane.lane_id).enabled
    assert dict.get(rig.timer.metadata, lane.lane_id) == {"verdict": "auth-dead", "probe_status": "auth-dead"}


@pytest.mark.parametrize("case", ["operator-hold", "auth-dead", "desktop", "disabled"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_c18_3_lanes_that_are_never_read_stay_unread_when_busy(rig, case, provider):
    """C-18.1, C-18.3, C-10.3: held, dead, desktop and disabled lanes of either provider stay unread."""
    lane = rig.enroll(f"{provider}-4", provider=provider,
                      desktop=case == "desktop")
    if case in ("operator-hold", "auth-dead"):
        rig.store.put_closure(Closure(lane.lane_id, "account", "2099-12-31T00:00:00Z", ClosureReason(case),
                                      ClockSource.REPORTED, "operator"))
    if case == "disabled":
        rig.store.update_lane(lane.lane_id, enabled=0)
    rig.occupy(lane, "both")
    assert rig.timer._claim(lane, "probe") is None
    rig.timer.probe_cycle()
    assert rig.wham.calls == []
    assert rig.store.query("SELECT 1 FROM readings WHERE lane_id=?", (lane.lane_id,)) == []


def test_c18_3_a_keepalive_still_waits_for_an_idle_lane(rig):
    """C-18.3: only the usage read goes beside the work; a timer turn still needs `slot:0`."""
    lane = rig.enroll("claude-1", provider="claude")
    rig.occupy(lane)
    assert rig.timer._reserve(lane, "keepalive") is None
    assert rig.timer._keepalive_lane(lane) == "skipped-busy"
    assert rig.turns == [] and rig.events("timer.reservation", lane) == []


def test_c18_3_busy_reads_count_toward_offline(rig, monkeypatch):
    """C-18.3: with every Codex lane busy and the network down, the cycle is offline and spends no credit."""
    lanes = [rig.enroll(f"codex-{n}") for n in (1, 2)]
    for lane in lanes:
        rig.occupy(lane)
        rig.wham.responses[lane.lane_id] = [OSError("network is unreachable")]
    monkeypatch.setattr(rig.timer.actions, "evaluate", lambda *a, **k: pytest.fail("evaluated while offline"))
    snapshot = rig.timer.probe_cycle()
    assert snapshot["offline"] is True
    assert rig.cycle_event()["deferred"] == {lane.lane_id: "network-error" for lane in lanes}


# --- properties ------------------------------------------------------------------

DURING = ("none", "closure", "extend", "relimit", "probe-relimit", "pruned-relimit", "hold", "disable", "admit")
RELIMITS = ("relimit", "probe-relimit", "pruned-relimit")
RESPONSES = USAGE + WITHHELD + ("mismatch-ok", "mismatch-limited", "mismatch-network-error", "mismatch-auth-dead",
                                "no-account", "raise-oserror", "raise-valueerror")


@settings(max_examples=120, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(how=st.sampled_from(OCCUPANCY), n=st.integers(1, 3),
       response=st.sampled_from(RESPONSES), utilization=st.floats(0, .99),
       limit_reached=st.sampled_from((None, True, False)), allowed=st.sampled_from((True, False, None)),
       during=st.sampled_from(DURING), override=st.booleans(), stale_limit=st.booleans())
def test_c18_3_property_a_busy_read_takes_no_slot_and_publishes_only_what_it_may(
        tmp_path, monkeypatch, how, n, response, utilization, limit_reached, allowed, during, override, stale_limit):
    """C-18.3 for every occupancy, answer and mid-read event:

    - no slot: the read itself acquires and releases no lease (here the lease table
      changes only by what the test's admission takes during it); no reservation,
      no heal turn;
    - account fence: no reading, settlement or closure release for another account;
    - no credential verdict: the lane is disabled only by a published mismatch or
      by its own attempt, and a busy read never sets a latch;
    - no older answer undoes a newer limit: a closure recorded or extended during
      the read, or reported again by an attempt that ended `limited`, is still
      open and ends no sooner after the publication;
    - what is published equals what `_persist` publishes for an idle read, when the
      lane is still one the timer reads and no limit was reported during the read.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory, make_rig(Path(directory)) as rig:
        lane = rig.enroll()
        stale_limit = stale_limit or during in RELIMITS
        older = iso(rig.clock() + timedelta(minutes=30))
        if stale_limit or during == "extend":
            rig.store.put_closure(Closure(lane.lane_id, "account", older,
                                          ClosureReason.PROVIDER_LIMIT, ClockSource.GUESSED, "attempt"))
        if override:
            rig.override(lane)
        rig.occupy(lane, how, n)
        before_leases = leases(rig.store)
        meta_before = dict(rig.timer.metadata.get(lane.lane_id, {}))
        newer = iso(rig.clock() + timedelta(hours=2))
        admitted = (f"lane:{lane.lane_id}:slot:9", "job-late/a1")
        actions = {
            "none": [], "admit": [lambda: rig.store.acquire_lease(*admitted)],
            "closure": [lambda: rig.store.put_closure(Closure(lane.lane_id, "five_hour-model", newer,
                                                              ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "attempt"))],
            "extend": [lambda: rig.store.put_closure(Closure(lane.lane_id, "account", newer,
                                                             ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "attempt"))],
            "disable": [lambda: (rig.store.update_lane(lane.lane_id, enabled=0), rig.timer.record_auth_dead(lane.lane_id))],
            # A limit already open is reported again: the closure's row is as it was. By an attempt that
            # ends `limited`; by an admission probe (`_finish_probe`), which has no attempt row; by an
            # attempt that retention prunes before the publication.
            "relimit": [lambda: rig.ends_limited(lane, older)],
            "probe-relimit": [lambda: rig.store.add_closure(Closure(lane.lane_id, "account", older, ClosureReason.PROVIDER_LIMIT,
                                                                    ClockSource.GUESSED, "probe"))],
            "pruned-relimit": [lambda: rig.ends_limited(lane, older), lambda: rig.prune(f"job-{lane.lane_id}-late")],
            # `lanes hold` records at scope `account`, the scope of a wham limit (#101).
            "hold": [lambda: rig.store.put_closure(Closure(lane.lane_id, "account", "2099-12-31T00:00:00Z",
                                                           ClosureReason.OPERATOR_HOLD, ClockSource.REPORTED, "operator"))],
        }
        rig.wham.during[lane.lane_id] = actions[during]
        status = response.removeprefix("mismatch-") if response.startswith("mismatch-") else (
            "ok" if response == "no-account" else response)
        if response.startswith("raise-"):
            scripted = OSError("down") if response == "raise-oserror" else ValueError("body")
        else:
            scripted = answer(lane, rig.clock, status, utilization=utilization, allowed=allowed,
                              limit_reached=limit_reached if status == "ok" else None,
                              account="codex:other" if response.startswith("mismatch-") else
                              None if response == "no-account" else ...)
        rig.wham.responses[lane.lane_id] = [scripted]
        open_before = rig.open_closures(lane)

        rig.timer.probe_cycle()

        limit_flag = None if isinstance(scripted, BaseException) else scripted["limit_reached"]
        mismatch = response.startswith("mismatch-")
        disabled = during == "disable"
        published = (mismatch or (status in USAGE and not response.startswith("raise-"))) and not disabled and during != "hold"
        releases = published and not mismatch and during not in ("closure", "extend") + RELIMITS
        # No slot.
        assert rig.wham.leases_seen == [before_leases]
        assert leases(rig.store) == sorted(before_leases + ([admitted] if during == "admit" else []))
        assert rig.events("timer.reservation", lane) == [] and rig.timer.active_holders == set()
        assert rig.turns == [] and rig.events("timer.heal", lane) == []
        # Account fence and publication.
        assert len(rig.wham_readings(lane)) == (2 if published and not mismatch else 0)
        assert bool(rig.store.get_lane(lane.lane_id).enabled) == (not disabled and not (published and mismatch))
        meta = rig.timer.metadata.get(lane.lane_id, {})
        if disabled:
            assert meta == {"verdict": "auth-dead", "probe_status": "auth-dead"}
        elif published:
            assert meta["probe_status"] == ("identity-mismatch" if mismatch else status)
        else:
            assert meta == meta_before
        assert disabled or meta.get("probe_status") not in LATCHES
        assert "revoked_epoch" not in meta
        # No older answer undoes a newer limit: it is still open, and no sooner (a limit the read itself
        # reports may only lengthen it).
        if during in ("closure", "extend"):
            scope = "five_hour-model" if during == "closure" else "account"
            assert any(c[:2] == (scope, "provider-limit") and c[2] >= newer for c in rig.open_closures(lane))
        fenced = published and during in ("closure", "extend") + RELIMITS   # a mismatch releases nothing anyway
        assert rig.cycle_event()["fenced"] == ([lane.lane_id] if fenced else [])
        # Settlement: only an `ok`, open, allowed answer of the lane's own account, uncontradicted.
        settles = override and releases and status == "ok" and limit_flag is False and allowed is not False
        assert bool(rig.events("action.reconciled", lane)) is settles
        # The older account limit is released only by a published, uncontradicted, open answer, and
        # extended in place only by a published limit of the lane's own account outside an override.
        if stale_limit and during not in ("extend", "hold"):
            stale, = (closure for closure in open_before if closure[0] == "account")
            released = releases and status == "ok" and limit_flag is False
            extended = published and not mismatch and (status == "limited" or limit_flag is True) and not override
            assert (stale in rig.open_closures(lane)) is (not released and not extended)


@settings(max_examples=60, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(status=st.sampled_from(USAGE), utilization=st.floats(0, .99), limit_reached=st.sampled_from((None, False, True)),
       allowed=st.sampled_from((True, False, None)), override=st.booleans(), stale_limit=st.booleans(),
       no_account=st.booleans())
def test_c18_3_differential_a_busy_read_publishes_what_an_idle_read_would(
        tmp_path, monkeypatch, status, utilization, limit_reached, allowed, override, stale_limit, no_account):
    """C-18.3: for an answer a busy read may publish, its readings, closures, settlement and verdict equal
    an idle read's of the same answer. Only the idle read's `slot:0` hold differs."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    published = []
    for busy in (False, True):
        with tempfile.TemporaryDirectory(dir=tmp_path) as directory, make_rig(Path(directory)) as rig:
            lane = rig.enroll()
            if stale_limit:
                rig.store.put_closure(Closure(lane.lane_id, "account", iso(rig.clock() + timedelta(minutes=30)),
                                              ClosureReason.PROVIDER_LIMIT, ClockSource.GUESSED, "attempt"))
            if override:
                rig.override(lane)
            if busy:
                rig.occupy(lane, "both")
            rig.wham.responses[lane.lane_id] = [answer(
                lane, rig.clock, status, utilization=utilization, allowed=allowed,
                limit_reached=limit_reached if status == "ok" else None, account=None if no_account else ...)]
            rig.timer.probe_cycle()
            assert rig.cycle_event()["busy"] == ([lane.lane_id] if busy else [])
            published.append({
                "readings": [(r["scope"], r["window"], r["utilization"], r["resets_at"], r["label"], r["source"],
                              r["observed_at"]) for r in rig.store.query(
                                  "SELECT * FROM readings WHERE lane_id=? ORDER BY reading_id", (lane.lane_id,))],
                "closures": sorted((c["scope"], c["reason"], c["until_at"], c["clock_source"], c["released_at"])
                                   for c in rig.store.query("SELECT * FROM closures WHERE lane_id=?", (lane.lane_id,))),
                "reconciled": [json.loads(e["data_json"]) for e in rig.events("action.reconciled", lane)],
                "metadata": rig.timer.metadata[lane.lane_id],
                "enabled": rig.store.get_lane(lane.lane_id).enabled,
            })
    idle, busy = published
    assert busy == idle


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('idle_first', [False, True])
def test_busy_claude_reads_start_after_idle_lanes_are_available(rig, provider, idle_first):
    """Eight paced busy reads cannot occupy workers/pacing while idle slots are held."""
    rig.timer.policy['reserve']['usage_spacing_s'] = .03
    idle = rig.enroll('idle-' + provider, provider=provider) if idle_first else None
    busy = [rig.enroll(f'claude-busy-{n}', provider='claude') for n in range(8)]
    for lane in busy:
        rig.occupy(lane)
    idle = idle or rig.enroll('idle-' + provider, provider=provider)
    seen = []
    def observe():
        view = rig.timer.snapshot()
        rig.timer.fence_probes(view)
        row = next(row for row in view['lanes'] if row['lane_id'] == idle.lane_id)
        seen.append((row['dispatchable'], idle.lane_id in rig.wham.calls,
                     bool(rig.wham_readings(idle)), idle.lane_id in view['unavailable_lanes']))
    for lane in busy:
        rig.wham.during[lane.lane_id] = [observe]
    rig.timer.probe_cycle()
    assert len(seen) == 8
    assert seen == [(True, True, True, False)] * 8
    assert not rig.timer._probe_holders
    assert not leases(rig.store)


def test_busy_pacing_debt_from_the_previous_cycle_is_paid_before_any_idle_lease(rig, monkeypatch):
    """A requested repeat cannot hold idle Claude/Codex slots while waiting for busy pacing."""
    rig.enroll('claude-idle', provider='claude')
    rig.enroll('codex-idle', provider='codex')
    rig.timer.policy['reserve']['usage_spacing_s'] = 3
    # The predecessor's last busy read owns the next usage start. Advance
    # that wait deterministically, inspecting the real leases at the wait.
    rig.timer._usage_next = time.monotonic() + 1000
    observed = []
    def wait(seconds):
        observed.append(leases(rig.store))
        rig.timer._usage_next = time.monotonic()
        return False
    monkeypatch.setattr(rig.timer.cancel, 'wait', wait)
    rig.timer.probe_cycle()
    assert observed
    assert observed[0] == []
    assert set(rig.wham.calls) == {'claude-idle', 'codex-idle'}


@pytest.mark.parametrize('busy', [False, True])
@pytest.mark.parametrize('status,retry_after', [('no-scope', None), ('identity-unbound', None),
    ('unavailable', None), ('network-error', None), ('rate-limited', 3600)])
def test_claude_missing_sensor_respects_cadence_and_retry_after_after_restart(rig, busy, status, retry_after):
    lane = rig.enroll('claude-1', provider='claude')
    rig.timer.probe_cycle()
    previous = rig.wham_readings(lane)
    rig.clock.advance(180)
    if busy:
        rig.occupy(lane)
    failure = {'status': status, 'readings': (), 'retry_after_s': retry_after}
    rig.wham.responses[lane.lane_id] = [dict(failure)]
    rig.timer.probe_cycle()
    assert rig.wham_readings(lane) == previous
    calls = len(rig.wham.calls)
    # Explicit cycles, including after a restart, must not bypass failure pacing.
    rig.timer.probe_cycle()
    assert len(rig.wham.calls) == calls
    rig.timer.stop()
    rig.timer = Timers(rig.store, rig.root, rig.timer.policy,
                       adapter_factory=lambda _: rig.wham, now=rig.clock)
    delay = retry_after or rig.timer.intervals['probe']
    rig.clock.advance(delay - 1)
    rig.timer.probe_cycle()
    assert len(rig.wham.calls) == calls
    assert rig.wham_readings(lane) == previous
    assert all(r['label'] == 'stale-provider' for r in rig.row(lane)['readings']
               if r['window'] in ('seven_day', 'five_hour'))
    rig.clock.advance(1)
    rig.timer.probe_cycle()
    assert len(rig.wham.calls) == calls + 1


@pytest.mark.parametrize('retry_after', [None, 3600])
def test_claude_missing_sensor_respects_fractional_backoff_and_retry_after(rig, retry_after):
    lane = rig.enroll('claude-1', provider='claude')
    rig.occupy(lane)
    rig.clock.advance(.4)
    rig.timer.intervals['probe'] = .5
    rig.wham.responses[lane.lane_id] = [{'status': 'rate-limited' if retry_after else 'no-scope',
                                       'readings': (), 'retry_after_s': retry_after}]
    rig.timer.probe_cycle()
    rig.timer.probe_cycle()
    assert rig.wham.calls == [lane.lane_id]
    rig.clock.advance((retry_after or .5) - .01)
    rig.timer.probe_cycle()
    assert rig.wham.calls == [lane.lane_id]
    rig.clock.advance(.01)
    rig.timer.probe_cycle()
    assert rig.wham.calls == [lane.lane_id] * 2
