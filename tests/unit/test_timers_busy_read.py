"""C-18.3: a busy Codex lane's usage is read beside its attempts, holding nothing.

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
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Reading, ReadingLabel
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
        scripted = self.responses.get(lane.lane_id, [])
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
            if how in ("attempt", "both"):
                self.store.add_job(job_id=job, request_id=job, payload_digest="d", kind="dispatch",
                                   state="running", workdir=str(self.root), prompt_path="/p", sandbox="read-only")
                self.store.add_attempt(attempt_id=attempt, job_id=job, seq=1, lane_id=lane.lane_id,
                                       model_requested="gpt-6.1-sol", state="running")
            if how in ("lease", "both"):
                self.store.acquire_lease(f"lane:{lane.lane_id}:slot:{index + 1}", attempt)

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


@pytest.mark.parametrize("how", ["attempt", "lease", "both", "probe"])
def test_c18_3_a_busy_codex_lane_is_read_on_its_first_cycle(rig, how):
    """C-18.3: work on the lane (an attempt, a slot lease, an admission probe's `slot:0`) no longer hides it."""
    lane = rig.enroll()
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


def test_c18_3_an_always_busy_lane_is_read_on_every_tick(rig, monkeypatch):
    """C-18.3, C-18.1: the 2026-09-30 starvation. A lane never idle at a cycle is still read every cycle."""
    lane = rig.enroll()
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


def test_c18_3_the_read_holds_no_slot_and_a_job_can_take_one_during_it(rig):
    """C-18.3, C-6.10: no lease, no reserved probe, no `probe:` holder during the read; admission is free to place."""
    lane = rig.enroll()
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
def test_c18_3_a_busy_read_publishes_no_credential_verdict_and_runs_no_heal(rig, status):
    """C-18.3, C-23.44, C-23.47: a busy lane's attempts renew its token and report a dead one themselves."""
    lane = rig.enroll()
    rig.timer.probe_cycle()                                   # idle: the verdict to keep
    published = dict(rig.timer.metadata[lane.lane_id])
    readings = len(rig.wham_readings(lane))
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
    assert len(rig.wham_readings(lane)) == readings
    assert rig.turns == [] and rig.events("timer.heal", lane) == []
    assert snapshot["offline"] is (status in ("network-error", "raise-oserror", "timeout"))


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


@pytest.mark.parametrize("fails", ["writing-the-verdict", "after-the-verdict"])
def test_c18_3_a_publication_that_raises_leaves_the_lane_as_it_stood(rig, monkeypatch, fails):
    """C-18.3 (Astra's finding 2): no fence is released early because there is none; a busy read's
    publication that fails rolls back whole, the settlement with it, and the next cycle publishes."""
    lane = rig.enroll()
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
    assert rig.wham_readings(lane) == [] and rig.open_closures(lane) == closures
    assert rig.events("action.reconciled", lane) == []
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock())
    assert lane.lane_id not in rig.timer.metadata and rig.store.get_lane(lane.lane_id).enabled
    assert rig.store._depth == 0 and leases(rig.store) == held
    rig.clock.advance()
    rig.timer.probe_cycle()
    assert len(rig.wham_readings(lane)) == 2 and rig.open_closures(lane) == set()
    assert rig.timer.actions.confirmed_override(lane.lane_id, now=rig.clock()) is None


@pytest.mark.parametrize("case", ["operator-hold", "auth-dead", "desktop", "claude", "disabled"])
def test_c18_3_lanes_that_are_never_read_stay_unread_when_busy(rig, case):
    """C-18.1, C-18.3, C-10.3, C-9.8: held, dead, desktop and disabled lanes are not read; a busy Claude
    lane is measured by its own attempts and is not read."""
    lane = rig.enroll("claude-1" if case == "claude" else "codex-4", provider="claude" if case == "claude" else "codex",
                      desktop=case == "desktop")
    if case in ("operator-hold", "auth-dead"):
        rig.store.put_closure(Closure(lane.lane_id, "account", "2099-12-31T00:00:00Z", ClosureReason(case),
                                      ClockSource.REPORTED, "operator"))
    if case == "disabled":
        rig.store.update_lane(lane.lane_id, enabled=0)
    rig.occupy(lane, "both")
    assert rig.timer._claim(lane, "probe") is (BUSY if case == "claude" else None)
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

DURING = ("none", "closure", "extend", "disable", "admit")
RESPONSES = USAGE + WITHHELD + ("mismatch-ok", "mismatch-limited", "mismatch-network-error", "mismatch-auth-dead",
                                "no-account", "raise-oserror", "raise-valueerror")


@settings(max_examples=120, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(how=st.sampled_from(("attempt", "lease", "both", "probe")), n=st.integers(1, 3),
       response=st.sampled_from(RESPONSES), utilization=st.floats(0, .99),
       limit_reached=st.sampled_from((None, True, False)), allowed=st.sampled_from((True, False, None)),
       during=st.sampled_from(DURING), override=st.booleans(), stale_limit=st.booleans())
def test_c18_3_property_a_busy_read_takes_no_slot_and_publishes_only_what_it_may(
        tmp_path, monkeypatch, how, n, response, utilization, limit_reached, allowed, during, override, stale_limit):
    """C-18.3 for every occupancy, answer and mid-read event:

    - no slot: the lease table during the read is the table before it, and after
      it the table plus only what admission took meanwhile; no reservation, no
      heal turn;
    - account fence: no reading, settlement or closure release for another account;
    - no credential verdict: the lane is disabled only by a published mismatch or
      by its own attempt, and a busy read never sets a latch;
    - no older answer undoes a newer limit: a closure recorded or extended during
      the read is still open, as recorded, after the publication;
    - what is published equals what `_persist` publishes for an idle read.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory, make_rig(Path(directory)) as rig:
        lane = rig.enroll()
        if stale_limit or during == "extend":
            rig.store.put_closure(Closure(lane.lane_id, "account", iso(rig.clock() + timedelta(minutes=30)),
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
        published = (mismatch or (status in USAGE and not response.startswith("raise-"))) and not disabled
        releases = published and not mismatch and during not in ("closure", "extend")
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
        # Settlement: only an `ok`, open, allowed answer of the lane's own account, uncontradicted.
        settles = override and releases and status == "ok" and limit_flag is False and allowed is not False
        assert bool(rig.events("action.reconciled", lane)) is settles
        # The older account limit is released only by a published, uncontradicted, open answer, and
        # extended in place only by a published limit of the lane's own account outside an override.
        if stale_limit and during != "extend":
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
