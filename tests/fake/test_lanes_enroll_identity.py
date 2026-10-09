"""C-10.6, C-10.8 at `lanes enroll`: the moment an operator is watching (D-ID1).

The daemon's enrolment, with an adapter that answers as a setup token does:
its organization from its own response header, its label from the keychain
item's name. A token that is another enabled lane's account is refused; one whose
label a profile fact contradicts is refused; and a lane that is already refused
work for its identity can be re-enrolled with a token of its own, which disables
it and keeps only the operator's holds when the account changed.
"""

from __future__ import annotations

import pytest

from subfleet import daemon as daemon_module
from subfleet import protocol
from subfleet.adapters.registry import register
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, LaneInfo, Lane, LaneOwner,
                                Reading, ReadingLabel)
from subfleet.daemon import Daemon
from subfleet.procs import Containment
from tests.fake.conftest import Harness

SHARED = "5ba7ed00-1111-4000-8000-00000000d1d1"
OWN = "a7a7a7a7-0000-4000-8000-000000000007"


class SetupTokens:
    """Each keychain item's token belongs to the organization `ORGS` names."""

    ORGS: dict[str, str] = {}

    def enroll(self, credential):
        email = credential.ref.removeprefix("claude-quota-")
        org = self.ORGS[credential.ref]
        return LaneInfo(f"claude:{email}", None, None,
                        (Reading("", "account", "seven_day", .4, "2026-10-12T14:00:00Z", ReadingLabel.PROVIDER,
                                 "rate_limit_event", "2026-10-09T12:00:00Z"),),
                        identity=f"org:{org}", identity_status="enrolled", label=email)


@pytest.fixture
def core(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "unit-test-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: False)
    monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
    SetupTokens.ORGS = {}
    register("claude", SetupTokens)
    daemon = Daemon(root)
    try:
        yield daemon, root
    finally:
        daemon.close()


def claude_lanes(daemon):
    return [row["lane_id"] for row in daemon.store.lane_rows() if row["provider"] == "claude"]


def enroll(daemon, ref):
    return daemon.dispatch("lanes", {"action": "enroll", "credential": ref})


def test_c10_8_a_token_of_another_enabled_lanes_account_is_refused_and_nothing_recorded(core):
    """C-10.8, D-ID1 the hivesight item holding maxghenis.com's token: refused at
    enrolment, naming the lane it would double-count, and no lane row written."""
    daemon, root = core
    SetupTokens.ORGS = {"claude-quota-max@maxghenis.com": SHARED, "claude-quota-max@hivesight.ai": SHARED}
    first = enroll(daemon, "claude-quota-max@maxghenis.com")["enrolled"]
    assert first["identity"] == f"org:{SHARED}" and first["identity_status"] == "enrolled"
    with pytest.raises(protocol.ProtocolError) as refused:
        enroll(daemon, "claude-quota-max@hivesight.ai")
    assert refused.value.code == 7
    message = str(refused.value)
    assert "same account as claude-1 (max@maxghenis.com)" in message and "C-10.8" in message
    assert "sign in to claude.ai as max@hivesight.ai" in refused.value.fix
    assert claude_lanes(daemon) == ["claude-1"]


def test_c10_6_a_label_a_profile_fact_contradicts_is_refused(core):
    """C-10.6 the desktop's own profile said this organization is
    max@maxghenis.com's personal one; a token of it stored as the hivesight item
    is refused, citing that fact."""
    daemon, root = core
    daemon.store.add_event("desktop.identity", data={"identity": f"acct-4:{SHARED}", "label": "max@maxghenis.com",
                                                     "organization_type": "claude_max"})
    SetupTokens.ORGS = {"claude-quota-max@hivesight.ai": SHARED}
    with pytest.raises(protocol.ProtocolError) as refused:
        enroll(daemon, "claude-quota-max@hivesight.ai")
    assert "max@maxghenis.com's account" in str(refused.value) and "per desktop" in str(refused.value)
    assert claude_lanes(daemon) == []


def test_c10_6_a_label_a_profile_fact_proves_is_enrolled_verified(core):
    daemon, root = core
    daemon.store.add_event("desktop.identity", data={"identity": f"acct-4:{SHARED}", "label": "max@maxghenis.com",
                                                     "organization_type": "claude_max"})
    SetupTokens.ORGS = {"claude-quota-max@maxghenis.com": SHARED}
    assert enroll(daemon, "claude-quota-max@maxghenis.com")["enrolled"]["identity_status"] == "verified"


def seed(daemon, lane_id, ref, org, *, status="enrolled"):
    """A lane as the periodic check leaves one: enabled, its organization learned."""
    email = ref.removeprefix("claude-quota-")
    daemon.store.put_lane(Lane(lane_id, "claude", f"claude:{email}", Credential("claude", ref, "keychain-token"),
                               None, LaneOwner.V2, False, True, f"org:{org}", email), identity_status=status)


def test_c10_8_a_shadowed_lane_is_re_enrolled_with_its_own_token(core):
    """C-10.6, C-10.8 the repair D-ID1 needs: claude-7 is enabled but refused work
    (its token is claude-4's account). With a token of its own account under the
    same item, re-enrolment is allowed, makes a new lane, disables claude-7, and
    carries only the operator's hold: the provider limit was the other
    account's."""
    daemon, root = core
    seed(daemon, "claude-4", "claude-quota-max@maxghenis.com", SHARED)
    seed(daemon, "claude-7", "claude-quota-max@hivesight.ai", SHARED)
    future = "2027-01-01T00:00:00Z"
    daemon.store.add_closure(Closure("claude-7", "account", future, ClosureReason.OPERATOR_HOLD,
                                     ClockSource.REPORTED, "hold-1"))
    daemon.store.add_closure(Closure("claude-7", "claude-opus-5-5", future, ClosureReason.PROVIDER_LIMIT,
                                     ClockSource.REPORTED, "limit-1"))
    SetupTokens.ORGS = {"claude-quota-max@hivesight.ai": OWN}
    result = enroll(daemon, "claude-quota-max@hivesight.ai")["enrolled"]
    assert result["lane_id"] == "claude-8" and result["identity"] == f"org:{OWN}"
    assert not daemon.store.get_lane("claude-7").enabled
    assert daemon.store.get_lane("claude-4").enabled
    carried = {row["reason"] for row in daemon.store.list_closures("claude-8")}
    assert carried == {"operator-hold"}
    event = daemon.store.query("SELECT data_json FROM events WHERE kind='lane.enrolled' AND lane_id='claude-8' "
                               "AND data_json LIKE '%supersedes%'")
    assert '"identity_changed":true' in event[0]["data_json"] and '"replaces":["claude-7"]' in event[0]["data_json"]


def test_c10_6_a_mismatched_lane_is_re_enrolled_and_one_of_the_same_account_keeps_its_limits(core):
    """C-10.6 "not a candidate until an operator re-enrols it": an enabled
    `mismatch` lane can be re-enrolled. When the new token is the account the lane
    recorded, its limits carry over as before."""
    daemon, root = core
    seed(daemon, "claude-7", "claude-quota-max@hivesight.ai", OWN, status="mismatch")
    future = "2027-01-01T00:00:00Z"
    daemon.store.add_closure(Closure("claude-7", "account", future, ClosureReason.PROVIDER_LIMIT,
                                     ClockSource.REPORTED, "limit-1"))
    SetupTokens.ORGS = {"claude-quota-max@hivesight.ai": OWN}
    result = enroll(daemon, "claude-quota-max@hivesight.ai")["enrolled"]
    assert result["lane_id"] == "claude-8" and not daemon.store.get_lane("claude-7").enabled
    assert {row["reason"] for row in daemon.store.list_closures("claude-8")} == {"provider-limit"}


def test_c10_2_an_enabled_lane_in_good_standing_is_still_not_enrolled_twice(core):
    daemon, root = core
    seed(daemon, "claude-9", "claude-quota-max@thesisinstitute.org", OWN)
    SetupTokens.ORGS = {"claude-quota-max@thesisinstitute.org": OWN}
    with pytest.raises(protocol.ProtocolError) as refused:
        enroll(daemon, "claude-quota-max@thesisinstitute.org")
    assert "already lane claude-9" in str(refused.value)


def test_c10_6_a_lane_its_own_profile_proved_cannot_be_re_enrolled_as_another_account(core):
    daemon, root = core
    daemon.store.put_lane(Lane("claude-3", "claude", f"claude:acct-3:{OWN}",
                               Credential("claude", "claude-quota-a@x.example", "keychain-token"),
                               None, LaneOwner.V2, False, False, f"acct-3:{OWN}", "a@x.example"),
                          identity_status="verified")
    SetupTokens.ORGS = {"claude-quota-a@x.example": SHARED}
    with pytest.raises(protocol.ProtocolError) as refused:
        enroll(daemon, "claude-quota-a@x.example")
    assert "could not verify the existing account identity" in str(refused.value)
