"""C-10.2, C-10.8: enrolment under concurrent change (review of #159, findings 7, 8, 9).

Written by the independent review of PR #159 (Subfleet job
20261009-100051-pr159-review-r1, GPT-6.1 Sol), each a failing reproduction of one
finding before its fix; kept as regressions. Finding numbers are the review's.
"""

from __future__ import annotations

import json

import pytest

from subfleet import protocol
from subfleet.contracts import ClockSource, Closure, ClosureReason
from subfleet.store import Store
from tests.fake.test_lanes_enroll_identity import OWN, SHARED, SetupTokens, core, enroll, seed


REF = "claude-quota-repaired@example.test"
FUTURE = "2027-01-01T00:00:00Z"


def test_repaired_account_does_not_regain_old_accounts_limits_on_next_reenrollment(core):
    """C-10.2 an older binding of another account keeps its limits to itself (finding 7)."""
    daemon, _ = core
    seed(daemon, "claude-7", REF, SHARED, status="mismatch")
    daemon.store.add_closure(Closure("claude-7", "account", FUTURE, ClosureReason.PROVIDER_LIMIT,
                                     ClockSource.REPORTED, "old-account-limit"))
    SetupTokens.ORGS = {REF: OWN}
    first = enroll(daemon, REF)["enrolled"]
    assert daemon.store.list_closures(first["lane_id"]) == []

    # A credential revocation disables the repaired binding; reauthenticate it
    # again with the same account. The first binding remains historical.
    with daemon.store.transaction("review.disable"):
        daemon.store.update_lane(first["lane_id"], enabled=0)
    second = enroll(daemon, REF)["enrolled"]
    carried = daemon.store.list_closures(second["lane_id"])
    assert carried == [], f"unrelated account limit returned: {carried}"


def test_enabled_replaced_binding_is_disabled_in_rebuild_seed(core):
    """C-10.2, C-1.3 a replaced binding is disabled in the seed file too (finding 9)."""
    daemon, root = core
    seed(daemon, "claude-7", REF, SHARED, status="mismatch")
    daemon._append_lanes_json(daemon.store.get_lane("claude-7"))
    SetupTokens.ORGS = {REF: OWN}
    replacement = enroll(daemon, REF)["enrolled"]
    assert not daemon.store.get_lane("claude-7").enabled
    payload = json.loads((root / "lanes.json").read_text())
    rows = payload["lanes"] if isinstance(payload, dict) else payload
    replaced = next(row for row in rows if row["lane_id"] == "claude-7")
    rebuilt = Store(root / "rebuilt.sqlite3")
    live_store = daemon.store
    try:
        daemon.store = rebuilt
        daemon._seed_lanes()
        old_lane = rebuilt.get_lane("claude-7")
        assert not old_lane.enabled, (
            f"rebuild resurrects binding alongside {replacement['lane_id']}: {replaced}"
        )
    finally:
        daemon.store = live_store
        rebuilt.close()


def test_enrollment_rechecks_identity_clash_when_another_lane_learns_its_identity(core, monkeypatch):
    """C-10.8 the clash refusal holds inside the write that would break it (finding 8)."""
    daemon, _ = core
    seed(daemon, "claude-4", "claude-quota-other@example.test", OWN)
    # A pre-upgrade lane has not learned any identity yet.
    with daemon.store.transaction("review.unbound"):
        daemon.store.update_lane("claude-4", identity=None)
    SetupTokens.ORGS = {REF: SHARED}

    def facts_read_while_probe_publishes():
        # _judge_enrollment has already read potential clashes. A concurrent
        # offline usage probe publishes this lane's first organization answer
        # while enrollment reads its facts before the write transaction.
        with daemon.store.transaction("review.probe-published"):
            daemon.store.update_lane("claude-4", identity=f"org:{SHARED}")
        return ()

    monkeypatch.setattr(daemon.timers, "identity_facts", facts_read_while_probe_publishes)
    with pytest.raises(protocol.ProtocolError) as refused:
        enroll(daemon, REF)
    assert refused.value.code == 7
    assert "same account as claude-4" in str(refused.value)
    assert daemon.store.get_lane("claude-5") is None


def test_reenrollment_refuses_when_the_binding_stops_being_shared_during_probe(core, monkeypatch):
    """C-10.2 a binding that stops being shared during the enrolment turn is not replaced."""
    daemon, _ = core
    seed(daemon, "claude-4", "claude-quota-other@example.test", SHARED)
    seed(daemon, "claude-7", REF, SHARED)
    original = SetupTokens.enroll
    SetupTokens.ORGS = {REF: OWN}

    def peer_disabled_during_probe(adapter, credential):
        with daemon.store.transaction("review.peer-disabled"):
            daemon.store.update_lane("claude-4", enabled=0)
        return original(adapter, credential)

    monkeypatch.setattr(SetupTokens, "enroll", peer_disabled_during_probe)
    with pytest.raises(protocol.ProtocolError) as refused:
        enroll(daemon, REF)
    assert refused.value.code == 7
    assert "lane changed during re-enrollment" in str(refused.value)
    assert daemon.store.get_lane("claude-7").enabled
    assert daemon.store.get_lane("claude-8") is None
