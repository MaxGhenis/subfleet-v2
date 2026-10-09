"""C-10.6, C-10.8: every path that judges or turns a lane carries its identity (review of #159, findings 4, 6, 10, 11, 14).

Written by the independent review of PR #159 (Subfleet job
20261009-100051-pr159-review-r1, GPT-6.1 Sol), each a failing reproduction of one
finding before its fix; kept as regressions. Finding numbers are the review's.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from subfleet import lane_identity, scheduler
from subfleet.alerts import evaluate_conditions
from subfleet.capacity import DesktopIdentity, build_view
from subfleet.contracts import Credential, Lane, LaneOwner, Outcome, OutcomeClass
from subfleet.daemon import Daemon
from subfleet.store import Store
from subfleet.timers import Timers


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
POLICY = {
    "models": {"haiku": {"id": "claude-haiku-4-5-20251001", "provider": "claude"}},
    "timers": {}, "reset_credits": {"enabled": False}, "alerts": {},
    "sessions": {"mirror_interval_s": 0},
}


def put_lane(store, lane_id="claude-2", *, identity="org:original", label=None, account_key=None):
    label = label or f"{lane_id}@example.com"
    lane = Lane(lane_id, "claude", account_key or f"claude:{label}",
                Credential("claude", f"UNUSED_REVIEW_TOKEN_{lane_id}", "env"),
                None, LaneOwner.V2, False, True, identity, label)
    store.put_lane(lane, identity_status="enrolled")
    return lane


@pytest.fixture
def rig(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        timer = Timers(store, tmp_path, POLICY, now=lambda: NOW)
        try:
            yield store, timer
        finally:
            timer.stop()


def test_idle_identity_change_and_its_event_rollback_together(rig, monkeypatch):
    """C-10.6 an identity-event insert failure must not leave an unaudited lane change (finding 6)."""
    store, timer = rig
    lane = put_lane(store, identity=None)
    real_add_event = store.add_event

    def fail_identity_event(kind, **kwargs):
        if kind == lane_identity.IDENTITY_EVENT:
            raise RuntimeError("injected identity event failure")
        return real_add_event(kind, **kwargs)

    monkeypatch.setattr(store, "add_event", fail_identity_event)
    probe = {"status": "ok", "readings": (), "identity": {
        "status": "identity-enrolled", "observed": "org:learned", "source": "models"}}
    with pytest.raises(RuntimeError, match="injected identity event failure"):
        timer._persist(lane, probe)

    assert store.get_lane(lane.lane_id).identity is None
    assert store.query("SELECT 1 FROM events WHERE kind='lane.identity'") == []


def test_idle_identity_learning_cannot_overwrite_a_concurrent_first_binding(rig, monkeypatch):
    """C-10.6 a finalized attempt learns the lane while an idle read holds an old row (finding 6)."""
    store, timer = rig
    lane = put_lane(store, identity=None)
    real_one = store.one
    interleaved = False

    def commit_attempt_between_record_read_and_write(sql, params=()):
        nonlocal interleaved
        row = real_one(sql, params)
        if (sql.startswith("SELECT identity,label,identity_status FROM lanes")
                and not interleaved and not store._holds_writer()):
            interleaved = True
            with store.transaction("review.concurrent_attempt"):
                lane_identity.record(store, lane.lane_id, {
                    "status": "identity-enrolled", "observed": "org:first", "source": "models"})
            assert store.get_lane(lane.lane_id).identity == "org:first"
        return row

    monkeypatch.setattr(store, "one", commit_attempt_between_record_read_and_write)
    timer._persist(lane, {"status": "ok", "readings": (), "identity": {
        "status": "identity-enrolled", "observed": "org:second", "source": "models"}})
    assert store.get_lane(lane.lane_id).identity == ("org:first" if interleaved else "org:second")


@pytest.mark.parametrize("finding, expected_identity, expected_status", [
    ({"status": "identity-enrolled", "observed": "org:learned", "source": "models"},
     "org:learned", "enrolled"),
    ({"status": "identity-mismatch", "observed": "org:other", "source": "models"},
     None, "mismatch"),
])
def test_keepalive_records_every_identity_finding(rig, finding, expected_identity, expected_status):
    """C-10.6 a model turn's finding is kept even without a later answering usage GET (finding 4)."""
    store, timer = rig
    lane = put_lane(store, identity=None)
    timer.turn = lambda *args, **kwargs: Outcome(OutcomeClass.OK, "answered", evidence={
        "identity": finding, "requested_at": "2026-10-09T12:00:00Z"})
    timer._keepalive_lane(lane)

    row = store.one("SELECT identity,identity_status FROM lanes WHERE lane_id=?", (lane.lane_id,))
    assert row == {"identity": expected_identity, "identity_status": expected_status}
    if expected_status == "mismatch":
        assert store.query("SELECT 1 FROM readings WHERE label='admission-observed'") == []


def test_probe_reservation_rechecks_new_identity_shadow(rig, tmp_path):
    """C-10.8 another lane learns the account after selection, before the probe's reservation (finding 10)."""
    store, timer = rig
    put_lane(store, "claude-1", identity=None)
    put_lane(store, "claude-2", identity="org:shared")
    daemon = Daemon.__new__(Daemon)
    daemon.store, daemon.timers, daemon.root, daemon.policy = store, timer, tmp_path, POLICY
    daemon._probe_notes, daemon._early_routes = {}, {}
    daemon._desktop_identity = lambda: DesktopIdentity("unverified")
    daemon._desktop_in_use = lambda: False
    daemon._desktop_answer = lambda: False
    daemon._record_desktop_use = lambda: None
    job = {"job_id": "review-job", "state": "queued", "cancel_requested_at": None}
    daemon._job = lambda job_id: job
    decision = SimpleNamespace(chosen_lane="claude-2", chosen_model="haiku")
    selected = False

    def route(*args, **kwargs):
        nonlocal selected
        if not selected:
            # This lane was still a candidate when the route selected it.
            assert not lane_identity.shadowing(store.lane_rows()).get("claude-2")
            store.update_lane("claude-1", identity="org:shared")
            selected = True
        return decision

    daemon._route = route
    daemon._probe_choice = lambda job, decision_job, exclusions, desktop, decision, basis, busy: (decision, basis, None)
    daemon._needs_probe = lambda decision, job: True
    daemon._save_probe = lambda record: None
    daemon._identity_binds = lambda outcome: True
    probed = []

    def probe_candidate(job, decision, holder):
        probed.append(decision.chosen_lane)
        store.release_leases(holder)
        return Outcome(OutcomeClass.OK, "answered")

    daemon._probe_candidate = probe_candidate
    daemon._prepare_route(job, job, ())

    assert lane_identity.shadowing(store.lane_rows())["claude-2"]["shadowed_by"] == "claude-1"
    assert probed == []


def test_submit_pin_roster_resolves_shared_account_as_capacity_view_does(rig):
    """C-10.8, C-11.2 a home login and a setup-token lane may both carry one account's label (finding 11)."""
    store, timer = rig
    put_lane(store, "claude-1", identity="account:shared", label="shared@example.com",
             account_key="claude:account:shared")
    store.update_lane("claude-1", identity_status="verified")
    put_lane(store, "claude-2", identity="org:shared", label="shared@example.com")
    daemon = Daemon.__new__(Daemon)
    daemon.store, daemon.timers = store, timer
    view = build_view(store.lane_rows(), now=NOW)
    candidate = scheduler.resolve_lane(view["lanes"], "shared@example.com", "claude")
    assert candidate["lane_id"] == "claude-1"
    assert scheduler.resolve_lane(daemon._pin_roster(), "shared@example.com", "claude")["lane_id"] == candidate["lane_id"]


def test_shared_alert_names_both_duplicated_team_accounts():
    """C-10.8 two seats may share an organization without sharing an account (finding 14)."""
    lanes = [{"lane_id": f"claude-{index}", "provider": "claude", "owner": "v2",
              "enabled": True, "identity_status": "verified", "identity": f"{account}:team-org",
              "label": f"{account}@example.com", "account_key": f"claude:legacy-{index}@example.com"}
             for index, account in enumerate(("seat-a", "seat-a", "seat-b", "seat-b"), 1)]
    conditions = evaluate_conditions(build_view(lanes, now=NOW), now=NOW)
    shared_alerts = [row for row in conditions if row["key"].startswith("claude-identity-shared:")]
    body = "\n".join(row["body"] for row in shared_alerts)
    for lane in lanes:
        assert lane["lane_id"] + " (" in body
