"""C-10.8: the findings of the three reviews of ae3455a1, each as a test.

Reviews 20260927-184331-recheck-review-a (correctness), -184332-recheck-review-b
(contract and mutation) and -184332-recheck-review-c (the live roster simulated)
found ways the automatic re-check could restore a lane it should not, or tell the
operator the wrong thing. Each test here reproduces one and holds the fix.
"""
from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from subfleet import recheck, scheduler
from subfleet.adapters.base import AdapterError
from subfleet.adapters.registry import register
from subfleet.contracts import LaneInfo
from subfleet.daemon import Daemon, _automatic_identity_refusal
from subfleet.timers import Timers
from tests.fake.test_auth_recheck import (  # noqa: F401 - fixtures and helpers
    OTHER, REF, Account, Codex, core, due, enroll, events, kill, lanes_json, listing, log, results, rig, row,
    until_due,
)


def restore(daemon, clock, lane_id):
    until_due(daemon, clock, lane_id)
    record = daemon.timers.auth_recheck_cycle()
    assert record["result"] == "restored", record
    return record["successor"]


# --- review A, finding 1: a flapping credential cannot reset its own backoff --------


def test_c10_8_a_restore_that_does_not_hold_backs_off_like_a_failure(rig):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)
    gaps, previous = [], None
    for round_ in range(4):
        lane_id = restore(daemon, clock, lane_id)
        started = recheck._instant(events(daemon, recheck.RECHECK_EVENT)[-1]["at"])
        if previous is not None:
            gaps.append((started - previous).total_seconds() / 3600)
        previous = started
        clock.advance(minutes=1)
        kill(daemon, lane_id, clock)                       # its first run disables it again
        assert row(daemon, lane_id)["auth_recheck"]["failures"] == round_ + 1
    # 6 h, then 12, 24, 24 (plus under 10 % jitter): at most one restore a day.
    for gap, hours in zip(gaps, [12, 24, 24]):
        assert hours <= gap <= hours * (1 + recheck.JITTER_FRACTION) + 0.1


def test_c10_8_a_restore_that_held_a_day_starts_the_successor_afresh(rig):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)
    successor = restore(daemon, clock, lane_id)
    clock.advance(days=1, minutes=1)
    kill(daemon, successor, clock)
    assert row(daemon, successor)["auth_recheck"]["failures"] == 0


# --- review A, finding 2: a replaced setup token waits for an operator --------------


def test_c10_8_a_replaced_setup_token_is_not_restored_automatically(rig):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]                     # enrolled with token A
    kill(daemon, lane_id, clock)
    Account.token = "setup-token-of-another-account"
    clock.advance(days=1)
    record = daemon.timers.auth_recheck_cycle()
    assert record["result"] == "failed" and "different token" in record["detail"]
    assert not daemon.store.query("SELECT 1 FROM lanes WHERE enabled=1 AND credential_ref=?", (REF,))
    Account.token = "setup-token-A"                          # the enrolled token authenticates again
    until_due(daemon, clock, lane_id)
    assert daemon.timers.auth_recheck_cycle()["result"] == "restored"


def test_c10_8_a_lane_enrolled_before_fingerprints_keeps_the_name_and_scope_check(rig):
    """Lanes enrolled before fingerprints were recorded (claude-11 to -17) have
    only the reference name and the profile's scope refusal to go on, as an
    operator's `lanes enroll` does; the restore records the fingerprint from then on."""
    daemon, root, clock = rig
    Account.token = None                                     # nothing recorded at enrolment
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)
    Account.token = "setup-token-A"
    clock.advance(days=1)
    successor = daemon.timers.auth_recheck_cycle()["successor"]
    assert daemon._enrolled_fingerprint(successor)
    assert not daemon._enrolled_fingerprint(lane_id)


def test_c10_8_the_automatic_identity_rule_itself():
    enrolled = {"provider": "claude", "identity": None, "identity_status": "enrolled"}
    ok = LaneInfo("claude:a", None, None, (), identity=None, identity_status="enrolled", credential_fingerprint="f1")
    assert _automatic_identity_refusal(enrolled, ok, "f1") is None
    assert _automatic_identity_refusal(enrolled, ok, None) is None
    assert "different token" in _automatic_identity_refusal(enrolled, ok, "f2")
    named = LaneInfo("claude:a", None, None, (), identity="acct:org", identity_status="enrolled")
    assert _automatic_identity_refusal(enrolled, named, None)             # inconsistent: refuse
    verified = {"provider": "claude", "identity": "acct:org", "identity_status": "verified"}
    assert _automatic_identity_refusal(verified, LaneInfo("k", None, None, (), identity="acct:org",
                                                          identity_status="verified"), None) is None
    assert _automatic_identity_refusal(verified, LaneInfo("k", None, None, (), identity="acct:other",
                                                          identity_status="verified"), None)
    assert _automatic_identity_refusal({**verified, "identity_status": "mismatch"},
                                       LaneInfo("k", None, None, (), identity="acct:org",
                                                identity_status="verified"), None)
    codex = {"provider": "codex", "identity": None, "identity_status": None}
    for status, refused in (("ok", False), ("limited", False), ("network-error", True), ("expired-token", True),
                            ("unknown", True), (None, True)):
        info = LaneInfo("codex:a", None, None, (), usage_status=status)
        assert bool(_automatic_identity_refusal(codex, info, None)) is refused, status


# --- review A finding 4, review B finding 1: history that disqualified stays --------


def test_c10_8_a_duplicate_whose_running_attempt_went_auth_dead_stays_off(rig):
    daemon, root, clock = rig
    first, duplicate = enroll(daemon), enroll(daemon, OTHER)
    daemon.store._update("lanes", "lane_id", duplicate["lane_id"], {"account_key": first["account_key"]})
    daemon.timers._identities()                               # the duplicate is turned off
    kill(daemon, duplicate["lane_id"], clock)                 # then its running attempt finalizes auth-dead
    kill(daemon, first["lane_id"], clock)                     # and the canonical lane goes too
    Account.calls = []
    clock.advance(days=3)
    for _ in range(3):
        daemon.timers.auth_recheck_cycle()
        clock.advance(hours=1)
    assert duplicate["lane_id"] not in [REF] and results(daemon, duplicate["lane_id"]) == []
    assert row(daemon, duplicate["lane_id"])["auth_recheck"]["why"] == "non-canonical"


def test_c10_8_a_duplicate_verdict_from_before_reasons_were_recorded_survives_a_restart(rig):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    daemon.store.update_lane(lane_id, enabled=0)                          # the code before C-10.8
    daemon.store.add_event("timer.verdict", lane_id=lane_id, data={"verdict": "duplicate"})
    daemon.store.add_event("timer.verdict", lane_id=lane_id, data={"verdict": "auth-dead"})  # a late attempt
    restarted = Timers(daemon.store, root, daemon.policy, reenroll=daemon._auth_recheck, now=clock)
    try:
        assert restarted.metadata[lane_id]["verdict"] == "auth-dead"
        assert restarted.disqualified[lane_id] == "non-canonical"
        standing = restarted.recheck_standings()[0][lane_id]
        assert not standing.eligible and standing.why == "non-canonical"
    finally:
        restarted.stop()


# --- review A finding 5, review B finding 3: the roster's values ---------------------


@pytest.mark.parametrize("value", [0, None, "false", "no"])
def test_c10_8_any_off_value_in_the_roster_is_the_operators_choice(rig, value):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)
    rows = [dict(r, enabled=value) if r["lane_id"] == lane_id else r for r in lanes_json(root)]
    (root / "lanes.json").write_text(json.dumps({"lanes": rows}))
    Account.calls = []
    clock.advance(days=2)
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == [] and row(daemon, lane_id)["auth_recheck"]["why"] == "operator"


@pytest.mark.parametrize("content", ["", "{}", '{"lanes": {}}'])
def test_c10_8_an_emptied_or_misshapen_roster_stops_every_recheck(rig, content):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)
    (root / "lanes.json").write_text(content)
    Account.calls = []
    clock.advance(days=2)
    daemon.timers.auth_recheck_cycle()
    assert Account.calls == [] and row(daemon, lane_id)["auth_recheck"]["why"] == "roster-unreadable"


# --- review A finding 6, B finding 2, C finding 1: a veto while the provider answers --


def test_c10_8_an_operator_veto_during_the_enrolment_stops_the_restore(rig):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)

    class Vetoed(Account):
        def enroll(self, credential):
            rows = [dict(r, enabled=False) if r["lane_id"] == lane_id else r for r in lanes_json(root)]
            (root / "lanes.json").write_text(json.dumps({"lanes": rows}))
            return super().enroll(credential)
    register("claude", Vetoed)
    clock.advance(days=1)
    record = daemon.timers.auth_recheck_cycle()
    assert record["result"] == "failed" and "may no longer come back: operator" in record["detail"]
    assert not daemon.store.query("SELECT 1 FROM lanes WHERE enabled=1 AND credential_ref=?", (REF,))
    assert not daemon.store.list_leases()


def test_c10_8_a_veto_or_a_stop_after_the_choice_asks_no_provider(rig):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)
    clock.advance(days=1)
    Account.calls = []
    rows = [dict(r, enabled=False) if r["lane_id"] == lane_id else r for r in lanes_json(root)]
    (root / "lanes.json").write_text(json.dumps({"lanes": rows}))
    assert daemon._auth_recheck(lane_id) is None
    rows = [dict(r, enabled=True) if r["lane_id"] == lane_id else r for r in lanes_json(root)]
    (root / "lanes.json").write_text(json.dumps({"lanes": rows}))
    daemon.timers.cancel.set()
    assert daemon._auth_recheck(lane_id) is None
    assert Account.calls == [] and results(daemon, lane_id) == []


# --- review B finding 4: capacity is checked where the slot is taken ----------------


def test_c10_8_a_daemon_filled_after_the_choice_defers_without_a_trace(rig, monkeypatch):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)
    clock.advance(days=1)
    answers = iter([None, "4 of 4 attempt slots in use"])        # free at the choice, full at the reservation
    monkeypatch.setattr(daemon.timers, "recheck_busy", lambda: next(answers))
    Account.calls = []
    assert daemon.timers.auth_recheck_cycle() is None
    assert Account.calls == [] and results(daemon, lane_id) == [] and not daemon.store.list_leases()
    assert daemon.timers.recheck_deferral.why == "4 of 4 attempt slots in use"


def test_c10_8_live_detached_attempts_fill_the_slots_and_turns_do_not(rig):
    daemon, root, clock = rig
    daemon.policy["caps"]["max_active_attempts"] = None       # no cap, the default: always a slot
    assert daemon.timers.recheck_busy() is None
    daemon.policy["caps"]["max_active_attempts"] = cap = 4
    for n in range(cap):
        daemon.store.add_job(job_id=f"t{n}", request_id=f"rt{n}", payload_digest="d", kind="turn",
                             workdir=str(root), prompt_path="/fake", sandbox="read-only")
        daemon.store.add_attempt(attempt_id=f"t{n}/a1", job_id=f"t{n}", seq=1, lane_id="codex-1",
                                 model_requested="haiku", state="running")
    assert daemon.timers.recheck_busy() is None
    for n in range(cap):
        daemon.store.add_job(job_id=f"j{n}", request_id=f"rj{n}", payload_digest="d", kind="dispatch",
                             workdir=str(root), prompt_path="/fake", sandbox="read-only")
        daemon.store.add_attempt(attempt_id=f"j{n}/a1", job_id=f"j{n}", seq=1, lane_id="codex-1",
                                 model_requested="haiku", state="running")
    assert daemon.timers.recheck_busy() == f"{cap} of {cap} attempt slots in use"


# --- review C finding 2: a roster that cannot be written is not an auth failure -----


def test_c10_8_a_roster_write_failure_after_the_restore_is_not_a_failure(rig, monkeypatch):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)

    def refuse(lane):
        raise PermissionError(13, "Permission denied", str(root / "lanes.json"))
    monkeypatch.setattr(daemon, "_append_lanes_json", refuse)
    clock.advance(days=1)
    record = daemon.timers.auth_recheck_cycle()
    assert record["result"] == "restored" and "PermissionError" in record["detail"]
    assert daemon.store.get_lane(record["successor"]).enabled
    assert "lanes.json was not updated" in log(root)
    notices = [n["text"] for n in daemon.store.query("SELECT text FROM service_notices")]
    assert any("is back as" in n and "lanes.json was not updated" in n for n in notices)
    manual = daemon.dispatch("lanes", {"action": "enroll", "credential": OTHER})
    assert manual["enrolled"]["enabled"] and "PermissionError" in manual["roster_error"]


# --- review A finding 7: a crash between the reservation and the turn ----------------


def test_c10_8_a_crash_after_the_reservation_is_freed_by_the_startup_recovery(rig, tmp_path, monkeypatch):
    """The reviewer's reproduction ran `_recover_probes` alone; the daemon's start
    runs `_recover_then_start_timers`, which first frees every `probe:timer:` lease
    that no probe record holds, so the lane is reservable again at once."""
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)
    crash = tmp_path / "crash-state"
    crash.mkdir()

    class DiesAfterReserving(Account):
        def enroll(self, credential):
            assert daemon.store.list_leases() and all(daemon._probe_record(r["holder"]) is None
                                                      for r in daemon.store.list_leases())
            with sqlite3.connect(root / "state.sqlite3") as source, \
                    sqlite3.connect(crash / "state.sqlite3") as target:
                source.backup(target)
            for name in ("lanes.json", "policy.json"):
                shutil.copyfile(root / name, crash / name)
            raise AdapterError("the process died here", code=5)
    register("claude", DiesAfterReserving)
    clock.advance(days=1)
    daemon.timers.auth_recheck_cycle()
    register("claude", Account)
    recovered = Daemon(crash)
    try:
        monkeypatch.setattr(recovered.timers, "start", lambda: None)
        recovered.timers.now, recovered.timers.load_per_cpu = clock, (lambda: 0.0)
        recovered._recover_then_start_timers()
        assert not recovered.store.list_leases()
        until_due(recovered, clock, lane_id)
        assert recovered.timers.auth_recheck_cycle()["result"] == "restored"
    finally:
        recovered.close()


# --- review B: the mutants that survived ------------------------------------------


def test_c10_8_a_codex_enrolment_is_bounded_by_the_probe_timeout(rig, tmp_path):
    daemon, root, clock = rig
    daemon.policy["caps"]["probe_timeout_s"] = 2
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    Codex.usage, Codex.account, Codex.enrolls, Codex.timeouts = "ok", None, [], []
    register("codex", Codex)
    lane_id = enroll(daemon, home)["lane_id"]
    kill(daemon, lane_id, clock)
    clock.advance(days=1)
    assert daemon.timers.auth_recheck_cycle()["result"] == "restored"
    assert Codex.timeouts == [30, 2]                        # the operator's enrolment, then the re-check's


def test_c10_8_a_job_pinned_to_the_old_lane_follows_the_restored_one(rig):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)
    clock.advance(days=1)
    successor = daemon.timers.auth_recheck_cycle()["successor"]
    assert scheduler.resolve_lane(daemon._pin_roster(), lane_id)["lane_id"] == successor


def test_c10_8_lanes_list_keeps_the_last_recheck_of_a_lane_an_operator_brought_back(rig):
    daemon, root, clock = rig
    lane_id = enroll(daemon)["lane_id"]
    kill(daemon, lane_id, clock)
    Account.mode = "dead"
    clock.advance(days=1)
    daemon.timers.auth_recheck_cycle()
    Account.mode = "ok"
    successor = enroll(daemon)["lane_id"]                   # the operator's own `lanes enroll`
    line = listing(daemon, lane_id)
    assert f"superseded by {successor}; last re-check" in line and "failed" in line
