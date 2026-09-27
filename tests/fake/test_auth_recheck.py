"""C-10.8 through the daemon: a lane disabled as auth-dead comes back by itself.

Each test drives the real `Daemon` (`core`, no provider process) with fake adapters
and a fake clock on the timers, and pins the host load, which on a busy machine
would otherwise defer every re-check:

- a lane disabled as auth-dead is re-enrolled once its credential authenticates
  again as the same account, through `_enroll_lane_locked`, with a daemon.log line,
  a notice, and a `lanes list` row that says so;
- a lane disabled for any other reason is never re-checked, however long it waits;
- the cadence bound, the backoff, the one-at-a-time bound, the load deferral;
- every automatic disable records its reason, so the re-check can tell them apart.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading

import pytest

from subfleet import recheck
from subfleet.adapters.base import AdapterError
from subfleet.adapters.registry import register
from subfleet.cli import _format_lanes
from subfleet.contracts import (
    Closure, ClosureReason, ClockSource, LaneInfo, Outcome, OutcomeClass,
)
from subfleet.store import LANE_DISABLED
from tests.fake.test_lanes_enroll import FakeClaude, core, lanes_json  # noqa: F401 - the fixture
from tests.fake.test_state_contract import state_daemon  # noqa: F401 - the fixture

REF = "claude-quota-max@axiom.org"
OTHER = "claude-quota-max@policybench.org"


class Clock:
    def __init__(self):
        self.at = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.at

    def advance(self, **delta):
        self.at += timedelta(**delta)


class Account(FakeClaude):
    """A setup-token lane's enrolment (C-10.6 `enrolled`), switchable per test."""
    mode = "ok"            # ok | dead | unverified
    calls: list[str] = []
    gate: threading.Event | None = None
    entered: threading.Event | None = None
    active = 0
    most = 0

    @classmethod
    def reset(cls):
        cls.mode, cls.calls, cls.gate, cls.entered, cls.active, cls.most = "ok", [], None, None, 0, 0

    def enroll(self, credential):
        cls = type(self)
        cls.calls.append(credential.ref)
        cls.active += 1
        cls.most = max(cls.most, cls.active)
        try:
            if cls.entered:
                cls.entered.set()
            if cls.gate:
                cls.gate.wait(10)
            if cls.mode == "dead":
                raise AdapterError("claude: the credential did not authenticate (the provider reported "
                                   "error oauth_org_not_allowed)", code=5, fix="renew the subscription")
            email = credential.ref.removeprefix("claude-quota-")
            status = "unverified" if cls.mode == "unverified" else "enrolled"
            return LaneInfo(f"claude:{email}", "max", None, (), identity=None, identity_status=status, label=email)
        finally:
            cls.active -= 1


class Codex:
    """A Codex adapter whose enrolment, like the real one, succeeds whatever the
    usage endpoint says; `usage` is what the endpoint says."""
    usage: dict = {"status": "auth-dead"}
    enrolls: list[str] = []

    def enroll(self, credential):
        type(self).enrolls.append(credential.ref)
        home = Path(credential.ref).resolve()
        return LaneInfo(f"codex:acct-{home.name}", "plus", str(home), ())

    def probe_status(self, lane, env):
        return dict(type(self).usage)


@pytest.fixture
def rig(core, monkeypatch):
    daemon, root = core
    clock = Clock()
    monkeypatch.setattr(daemon.timers, "now", clock)
    daemon.timers.load_per_cpu = lambda: 0.0
    Account.reset()
    register("claude", Account)
    return daemon, root, clock


def enroll(daemon, ref=REF):
    return daemon.dispatch("lanes", {"action": "enroll", "credential": str(ref)})["enrolled"]


def kill(daemon, lane_id, clock, *, reason="auth-dead", source="attempt"):
    """Disable as the daemon does, with the reason recorded at the fake clock's time."""
    daemon.store.disable_lane(lane_id, reason, source=source, at=recheck.iso(clock()))
    if reason == "auth-dead":
        daemon.timers.record_auth_dead(lane_id)


def events(daemon, kind, lane_id=None):
    """Payloads only: `add_event`'s transaction follows each with an empty audit twin."""
    return [data for row in daemon.store.query(
        "SELECT lane_id,data_json FROM events WHERE kind=? ORDER BY event_id", (kind,))
        if (lane_id is None or row["lane_id"] == lane_id) and (data := json.loads(row["data_json"]))]


def results(daemon, lane_id):
    return [e["result"] for e in events(daemon, recheck.RECHECK_EVENT, lane_id)]


def due(daemon, lane_id):
    standing = daemon.timers.recheck_standings()[0][lane_id]
    return recheck._instant(standing.next_at)


def until_due(daemon, clock, lane_id):
    clock.at = max(clock.at, due(daemon, lane_id))


def log(root):
    return (root / "daemon.log").read_text()


def row(daemon, lane_id):
    return next(r for r in daemon.dispatch("lanes", {"action": "list"})["lanes"] if r["lane_id"] == lane_id)


def listing(daemon, lane_id):
    text = _format_lanes(daemon.dispatch("lanes", {"action": "list"}))
    return next(line for line in text.splitlines() if line.startswith(lane_id + " "))


# --- the way back ---------------------------------------------------------------


def test_c10_8_an_auth_dead_lane_comes_back_when_its_credential_authenticates_again(rig):
    daemon, root, clock = rig
    first = enroll(daemon)
    kill(daemon, first["lane_id"], clock)
    Account.mode, Account.calls = "dead", []
    assert "never re-checked; next" in listing(daemon, first["lane_id"])

    daemon.timers.auth_recheck_cycle()
    assert Account.calls == []                                   # not due for six hours
    until_due(daemon, clock, first["lane_id"])
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == [REF]
    assert not daemon.store.get_lane(first["lane_id"]).enabled
    assert results(daemon, first["lane_id"]) == ["started", "failed"]
    assert "still does not authenticate" in log(root) and first["lane_id"] in log(root)
    assert "failed (1 failed in a row); next" in listing(daemon, first["lane_id"])

    Account.mode = "ok"
    until_due(daemon, clock, first["lane_id"])
    record = daemon.timers.auth_recheck_cycle()
    successor = record["successor"]
    assert successor != first["lane_id"] and results(daemon, first["lane_id"])[-1] == "restored"
    back = daemon.store.one("SELECT * FROM lanes WHERE lane_id=?", (successor,))
    assert back["enabled"] and back["account_key"] == first["account_key"]
    assert back["credential_ref"] == REF and back["credential_epoch"] == first["credential_epoch"] + 1
    assert back["identity_status"] == "enrolled" and back["label"] == first["label"]
    assert not daemon.store.get_lane(first["lane_id"]).enabled            # C-1.3: a new id, the old one kept
    enrolled = events(daemon, "lane.enrolled", successor)[-1]
    assert enrolled["automatic"] == "auth-recheck" and enrolled["supersedes"] == first["lane_id"]
    assert f"re-enrolled as {successor}" in log(root)
    notices = daemon.store.query("SELECT text FROM service_notices")
    assert any(f"is back as {successor}" in n["text"] and first["lane_id"] in n["text"] for n in notices)
    assert f"back as {successor}" in listing(daemon, first["lane_id"])
    assert any(r["lane_id"] == successor for r in lanes_json(root))
    assert not daemon.store.list_leases()
    # And it is not re-checked again: it has a successor.
    Account.calls = []
    clock.advance(days=30)
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == []


def test_c10_8_it_runs_through_the_timer_on_its_own_worker(rig):
    daemon, root, clock = rig
    assert daemon.timers.intervals["auth_recheck"] == 300
    assert daemon.timers.reenroll == daemon._auth_recheck
    assert daemon.timers._rechecks._max_workers == 1
    lane = enroll(daemon)
    kill(daemon, lane["lane_id"], clock)
    until_due(daemon, clock, lane["lane_id"])
    daemon.timers.start()
    daemon.timers._due["auth_recheck"] = 0
    daemon.timers.tick()
    daemon.timers._rechecks.submit(lambda: None).result(timeout=10)      # the worker has drained
    assert results(daemon, lane["lane_id"]) == ["started", "restored"]
    assert daemon.timers.status()["auth_recheck"]["last_run"]


# --- never for another reason -------------------------------------------------------


def seed(daemon, root, row_extra):
    """A lane the operator's `lanes.json` names, seeded as `_seed_lanes` does."""
    rows = lanes_json(root)
    rows.append({"lane_id": "claude-15", "provider": "claude", "account_key": "claude:max@rulesatlas.org",
                 "credential_ref": "claude-quota-max@rulesatlas.org", "credential_kind": "keychain-token",
                 "credential_epoch": 1, "home": None, "desktop": False, "label": "max@rulesatlas.org",
                 **row_extra})
    (root / "lanes.json").write_text(json.dumps({"lanes": rows}))
    daemon._seed_lanes()
    return "claude-15"


OTHER_REASONS = ["identity-mismatch-probe", "identity-mismatch-recorded", "mismatch-then-auth-dead",
                 "non-canonical", "claude-15",
                 "operator-roster", "unverified-identity", "seeded-without-record", "owner-v1", "desktop",
                 "legacy-duplicate"]


@pytest.mark.parametrize("why", OTHER_REASONS)
def test_c10_8_a_lane_disabled_for_any_other_reason_is_never_rechecked(rig, why):
    daemon, root, clock = rig
    if why == "claude-15":
        lane_id = seed(daemon, root, {"enabled": False, "identity_status": "unverified"})
    elif why == "seeded-without-record":
        lane_id = seed(daemon, root, {"enabled": False, "identity_status": "enrolled"})
    else:
        lane_id = enroll(daemon)["lane_id"]
        lane = daemon.store.get_lane(lane_id)
        if why == "identity-mismatch-probe":
            daemon.timers._persist(lane, {"status": "ok", "account_key": "claude:someone-else", "readings": ()})
        elif why == "identity-mismatch-recorded":
            kill(daemon, lane_id, clock, reason="identity-mismatch", source="timer-probe")
        elif why == "mismatch-then-auth-dead":
            kill(daemon, lane_id, clock, reason="identity-mismatch", source="timer-probe")
            kill(daemon, lane_id, clock)                         # an attempt still running finalizes auth-dead
        elif why == "non-canonical":
            enroll(daemon, OTHER)
            daemon.store.update_lane("claude-2", enabled=1)
            daemon.store._update("lanes", "lane_id", "claude-2", {"account_key": lane.account_key})
            daemon.timers._identities()
            lane_id = "claude-2"
        elif why == "operator-roster":
            kill(daemon, lane_id, clock)
            rows = [dict(r, enabled=False) if r["lane_id"] == lane_id else r for r in lanes_json(root)]
            (root / "lanes.json").write_text(json.dumps({"lanes": rows}))
        elif why == "unverified-identity":
            kill(daemon, lane_id, clock)
            daemon.store.update_lane(lane_id, identity_status="unverified")
        elif why == "owner-v1":
            kill(daemon, lane_id, clock)
            daemon.store.update_lane(lane_id, owner="v1")
        elif why == "desktop":
            kill(daemon, lane_id, clock)
            daemon.store.update_lane(lane_id, desktop=1)
        elif why == "legacy-duplicate":
            daemon.store.update_lane(lane_id, enabled=0)          # as the code before C-10.8 left it
            daemon.timers.metadata[lane_id] = {"verdict": "duplicate"}
    assert not daemon.store.get_lane(lane_id).enabled
    Account.calls = []
    for _ in range(40):
        clock.advance(hours=7)
        daemon.timers.auth_recheck_cycle()
    assert Account.calls == [] and results(daemon, lane_id) == []
    info = row(daemon, lane_id)["auth_recheck"]
    assert not info["eligible"] and info["next_at"] is None
    assert "not re-checked" in listing(daemon, lane_id)


def test_c10_8_claude_15_reads_as_the_operators_choice(rig):
    daemon, root, clock = rig
    lane_id = seed(daemon, root, {"enabled": False, "identity_status": "unverified"})
    assert "not re-checked: operator (lanes.json)" in listing(daemon, lane_id)


def test_c10_8_a_lane_disabled_before_reasons_were_recorded_is_judged_by_its_verdict(rig):
    daemon, root, clock = rig
    lane = enroll(daemon)
    daemon.store.update_lane(lane["lane_id"], enabled=0)               # the code before C-10.8
    daemon.timers.record_auth_dead(lane["lane_id"])
    assert not events(daemon, LANE_DISABLED, lane["lane_id"])
    clock.advance(days=2)
    daemon.timers.auth_recheck_cycle()
    assert results(daemon, lane["lane_id"]) == ["started", "restored"]


# --- the bounds ------------------------------------------------------------------------


def test_c10_8_at_most_one_recheck_per_interval_and_a_failed_one_backs_off(rig):
    daemon, root, clock = rig
    lane = enroll(daemon)
    kill(daemon, lane["lane_id"], clock)
    Account.mode, Account.calls = "dead", []
    starts = []
    for _ in range(12 * 24 * 5):                                           # five days of 5-minute ticks
        clock.advance(minutes=5)
        before = len(Account.calls)
        daemon.timers.auth_recheck_cycle()
        if len(Account.calls) > before:
            starts.append(clock())
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(starts, starts[1:])]
    assert (starts[0] - (clock() - timedelta(days=5))).total_seconds() >= 6 * 3600
    assert len(gaps) >= 3
    for gap, expected in zip(gaps, [12, 24, 24, 24]):          # 6 h after the disable, then doubling
        assert expected <= gap <= expected * (1 + recheck.JITTER_FRACTION) + 5 / 60
    assert not daemon.store.get_lane(lane["lane_id"]).enabled
    assert [e["failures"] for e in events(daemon, recheck.RECHECK_EVENT, lane["lane_id"])
            if e["result"] == "failed"] == list(range(1, len(starts) + 1))


def test_c10_8_one_recheck_at_a_time_and_never_behind_an_enrollment(rig):
    daemon, root, clock = rig
    first, second = enroll(daemon), enroll(daemon, OTHER)
    for lane in (first, second):
        kill(daemon, lane["lane_id"], clock)
    clock.advance(days=1)
    Account.calls, Account.gate, Account.entered = [], threading.Event(), threading.Event()
    worker = threading.Thread(target=daemon._auth_recheck, args=(first["lane_id"],))
    worker.start()
    try:
        assert Account.entered.wait(10)
        # The other lane is due, but an enrollment holds the lock: it waits, and says why.
        assert daemon._auth_recheck(second["lane_id"]) is None
        assert daemon.timers.recheck_deferral.why == "an enrollment is running"
        # The timer's own pass is held by the spacing the first re-check began.
        assert daemon.timers.auth_recheck_cycle() is None
        assert daemon.timers.recheck_deferral.why == "spacing"
        assert "(waiting: spacing)" in listing(daemon, second["lane_id"])
    finally:
        Account.gate.set()
        worker.join(10)
    assert Account.most == 1 and Account.calls == [REF]
    assert results(daemon, second["lane_id"]) == []
    clock.advance(seconds=daemon.timers.recheck.spacing_s)
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == [REF, OTHER]


def test_c10_8_host_load_defers_until_a_lane_is_a_day_overdue(rig):
    daemon, root, clock = rig
    lane = enroll(daemon)
    kill(daemon, lane["lane_id"], clock)
    daemon.timers.load_per_cpu = lambda: 50.0
    Account.calls = []
    until_due(daemon, clock, lane["lane_id"])
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == [] and "(waiting: host load 50.0 per CPU)" in listing(daemon, lane["lane_id"])
    clock.advance(hours=23)
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == []
    clock.advance(hours=1)
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == [REF]


def test_c10_8_no_free_attempt_slot_always_defers(rig):
    daemon, root, clock = rig
    lane = enroll(daemon)
    kill(daemon, lane["lane_id"], clock)
    cap = daemon.policy["caps"]["max_active_attempts"]
    for n in range(cap):
        daemon.store.acquire_lease(f"lane:elsewhere:slot:{n}", f"probe:other:{n}")
    Account.calls = []
    clock.advance(days=5)
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == [] and daemon.timers.recheck_deferral.why == f"{cap} of {cap} attempt slots in use"
    daemon.store.release_leases("probe:other:0")
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == [REF]


def test_c10_8_a_crash_mid_recheck_still_counts_for_the_cadence(rig):
    daemon, root, clock = rig
    lane = enroll(daemon)
    kill(daemon, lane["lane_id"], clock)
    clock.advance(days=1)
    daemon.store.add_event(recheck.RECHECK_EVENT, lane_id=lane["lane_id"],
                           data={"at": recheck.iso(clock()), "result": "started", "failures": 0})
    Account.calls = []
    clock.advance(hours=5)
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == []
    until_due(daemon, clock, lane["lane_id"])
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == [REF]


def test_c10_8_a_stop_mid_recheck_is_interrupted_not_a_failure(rig):
    daemon, root, clock = rig
    lane = enroll(daemon)
    kill(daemon, lane["lane_id"], clock)
    clock.advance(days=1)
    Account.mode, Account.gate, Account.entered = "dead", threading.Event(), threading.Event()
    worker = threading.Thread(target=daemon._auth_recheck, args=(lane["lane_id"],))
    worker.start()
    assert Account.entered.wait(10)
    daemon.timers.cancel.set()
    Account.gate.set()
    worker.join(10)
    record = events(daemon, recheck.RECHECK_EVENT, lane["lane_id"])[-1]
    assert record["result"] == "interrupted" and record["failures"] == 0
    assert "still does not authenticate" not in log(root)


# --- the identity it comes back as -----------------------------------------------------


def test_c10_8_a_setup_token_lane_must_be_refused_profile_scope_again(rig):
    """C-10.6: the automatic path restores an `enrolled` lane only when the profile
    endpoint answers as it did at enrolment; an unanswered profile leaves it off,
    where an operator's `lanes enroll`, watching, may still take it."""
    daemon, root, clock = rig
    lane = enroll(daemon)
    kill(daemon, lane["lane_id"], clock)
    Account.mode = "unverified"
    clock.advance(days=1)
    record = daemon.timers.auth_recheck_cycle()
    assert record["result"] == "failed" and record["code"] == 7
    assert "enrolled without profile scope" in record["detail"]
    assert not daemon.store.get_lane(lane["lane_id"]).enabled
    assert enroll(daemon)["enabled"]                     # the operator's path is unchanged


def test_c10_8_a_verified_lane_must_come_back_as_its_identity(rig):
    daemon, root, clock = rig
    register("claude", FakeClaude)
    lane = enroll(daemon, "claude-quota-max7@example.test")
    assert lane["identity_status"] == "verified"
    kill(daemon, lane["lane_id"], clock)

    class Moved(FakeClaude):
        def enroll(self, credential):
            info = super().enroll(credential)
            return LaneInfo(info.account_key, info.plan, info.home, (), identity="acct-9:org-9",
                            identity_status="verified", label=info.label)
    register("claude", Moved)
    clock.advance(days=1)
    record = daemon.timers.auth_recheck_cycle()
    assert record["result"] == "failed" and "could not verify" in record["detail"]
    register("claude", FakeClaude)
    until_due(daemon, clock, lane["lane_id"])
    assert daemon.timers.auth_recheck_cycle()["result"] == "restored"


def test_c10_8_a_codex_lane_needs_its_usage_endpoint_to_accept_the_same_account(rig, tmp_path):
    daemon, root, clock = rig
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    Codex.usage, Codex.enrolls = {"status": "auth-dead"}, []
    register("codex", Codex)
    lane = enroll(daemon, home)
    assert lane["provider"] == "codex"
    kill(daemon, lane["lane_id"], clock)
    clock.advance(days=1)
    record = daemon.timers.auth_recheck_cycle()
    assert record["result"] == "failed" and record["code"] == 5 and len(Codex.enrolls) == 1
    Codex.usage = {"status": "ok", "account_key": "codex:someone-else", "readings": ()}
    until_due(daemon, clock, lane["lane_id"])
    record = daemon.timers.auth_recheck_cycle()
    assert record["result"] == "failed" and "someone-else" in record["detail"] and len(Codex.enrolls) == 1
    Codex.usage = {"status": "limited", "account_key": lane["account_key"], "readings": ()}
    until_due(daemon, clock, lane["lane_id"])
    record = daemon.timers.auth_recheck_cycle()
    assert record["result"] == "restored" and len(Codex.enrolls) == 2
    assert daemon.store.get_lane(record["successor"]).enabled


def test_c10_8_another_enabled_lane_on_the_account_keeps_it_off(rig):
    daemon, root, clock = rig
    lane = enroll(daemon)
    kill(daemon, lane["lane_id"], clock)
    other = enroll(daemon, OTHER)
    daemon.store._update("lanes", "lane_id", other["lane_id"], {"account_key": lane["account_key"]})
    Account.calls = []
    clock.advance(days=3)
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == []
    assert "account enabled on another lane" in listing(daemon, lane["lane_id"])


def test_c10_8_off_in_policy(rig):
    daemon, root, clock = rig
    from subfleet.timers import Timers
    policy = {**daemon.policy, "timers": {**daemon.policy["timers"], "auth_recheck_interval_s": 0}}
    timers = Timers(daemon.store, root, policy, reenroll=daemon._auth_recheck, now=clock)
    try:
        assert "auth_recheck" not in timers.intervals
        lane = enroll(daemon)
        kill(daemon, lane["lane_id"], clock)
        clock.advance(days=5)
        Account.calls = []
        timers.auth_recheck_cycle()
        assert Account.calls == []
    finally:
        timers.stop()


# --- every disable says why -----------------------------------------------------------


def disabled_reason(daemon, lane_id):
    record = events(daemon, LANE_DISABLED, lane_id)[-1]
    return record["reason"], record["source"]


def test_c10_8_the_probe_cycle_records_why_it_disabled_a_lane(rig):
    daemon, root, clock = rig
    dead, mismatched = enroll(daemon), enroll(daemon, OTHER)
    daemon.timers._persist(daemon.store.get_lane(dead["lane_id"]), {"status": "auth-dead", "readings": ()})
    daemon.timers._persist(daemon.store.get_lane(mismatched["lane_id"]),
                           {"status": "ok", "account_key": "claude:someone-else", "readings": ()})
    assert disabled_reason(daemon, dead["lane_id"]) == ("auth-dead", "timer-probe")
    assert disabled_reason(daemon, mismatched["lane_id"]) == ("identity-mismatch", "timer-probe")


def test_c10_8_identities_record_the_closure_and_the_duplicate(rig):
    daemon, root, clock = rig
    closed, first, duplicate = enroll(daemon), enroll(daemon, OTHER), enroll(daemon, "claude-quota-max@logpile.ai")
    daemon.store.add_closure(Closure(closed["lane_id"], "account", "2099-01-01T00:00:00Z",
                                     ClosureReason.AUTH_DEAD, ClockSource.REPORTED, "test"))
    daemon.store._update("lanes", "lane_id", duplicate["lane_id"], {"account_key": first["account_key"]})
    daemon.timers._identities()
    assert disabled_reason(daemon, closed["lane_id"]) == ("auth-dead", "closure")
    assert disabled_reason(daemon, duplicate["lane_id"]) == ("non-canonical", "probe-cycle")


def test_c10_8_keepalive_and_the_prelaunch_probe_record_auth_dead(rig, tmp_path):
    daemon, root, clock = rig
    kept, probed = enroll(daemon), enroll(daemon, OTHER)
    daemon.timers.turn = lambda *args, **kwargs: Outcome(OutcomeClass.AUTH_DEAD, "auth-dead: account_on_hold")
    assert daemon.timers._keepalive_lane(daemon.store.get_lane(kept["lane_id"])) == "auth-dead"
    assert disabled_reason(daemon, kept["lane_id"]) == ("auth-dead", "keepalive")
    directory = tmp_path / "probe"
    directory.mkdir()
    daemon._finish_probe({"holder": "probe:test", "job_id": None, "lane_id": probed["lane_id"],
                          "model_id": "claude-haiku-4-5-20251001", "directory": str(directory),
                          "state": "running"}, Outcome(OutcomeClass.AUTH_DEAD, "auth-dead: oauth_org_not_allowed"))
    assert disabled_reason(daemon, probed["lane_id"]) == ("auth-dead", "probe")
    assert not daemon.store.get_lane(probed["lane_id"]).enabled


def test_c10_8_a_finalized_attempt_records_auth_dead(state_daemon, monkeypatch):
    from subfleet import daemon as module
    from tests.fake.test_state_contract import receipt_fixture, reserve
    from tests.fake_adapter import FakeAdapter

    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1)

    class Dead(FakeAdapter):
        def classify(self, *args):
            return Outcome(OutcomeClass.AUTH_DEAD, "auth-dead: the provider reported error account_on_hold")
    monkeypatch.setattr(module, "get_adapter", lambda _: Dead())
    daemon._finalize(receipt_fixture(daemon, attempt, adir, rc=1))
    assert disabled_reason(daemon, attempt["lane_id"]) == ("auth-dead", "attempt")
    assert not daemon.store.get_lane(attempt["lane_id"]).enabled
