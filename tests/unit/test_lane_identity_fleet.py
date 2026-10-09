"""C-10.6, C-10.8, C-10.9 across the fleet: the D-ID1 incident end to end.

2026-10-09: claude-4 (max@maxghenis.com), claude-7 (max@hivesight.ai) and
claude-19 (max.ghenis@gmail.com) held tokens of one account. Here the real Claude
adapter reads three such tokens through the timers' probe cycle, with only the
network stood in: the profile endpoint refuses every setup token (403), the
organization header names one organization for all three, and the usage
endpoint answers. The fleet must learn each lane's organization, keep one lane
for the account, alert, publish it, and spend no keepalive on the others.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from subfleet import capacity, claude_cards, lane_identity, render
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.alerts import evaluate_conditions
from subfleet.capacity import DesktopIdentity, build_view
from subfleet.cli import _format_lanes
from subfleet.contracts import Credential, Lane, LaneOwner, Outcome, OutcomeClass
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.scheduler import STANDING_REFUSALS, evaluate
from subfleet.status_json import build_status, dispatchable
from subfleet.store import Store
from subfleet.timers import Timers
from tests.caps import capped
from tests.fake.profile import usage_body

SHARED = "5ba7ed00-1111-4000-8000-00000000d1d1"
OWN_7 = "a7a7a7a7-0000-4000-8000-000000000007"
OWN_9 = "0e9e0e9e-9999-4000-8000-000000000009"
LANES = {"claude-4": "max@maxghenis.com", "claude-7": "max@hivesight.ai",
         "claude-19": "max.ghenis@gmail.com", "claude-9": "max@thesisinstitute.org"}


class Clock:
    def __init__(self):
        self.at = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.at


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Four keychain-free lanes (environment credentials), each a token whose
    organization `orgs` decides; a real ClaudeAdapter per read, as the timers
    build one."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    orgs = {"tok-4": SHARED, "tok-7": SHARED, "tok-19": SHARED, "tok-9": OWN_9}
    org_calls = []

    def org_opener(request, timeout):
        token = request.headers.get("Authorization").partition(" ")[2]
        org_calls.append(token)
        return 200, orgs.get(token)

    def usage_opener(request, timeout):
        return 200, usage_body(40)

    clock = Clock()
    factory = lambda provider: ClaudeAdapter(  # noqa: E731
        now=clock, profile_opener=lambda request, timeout: (403, b""),
        org_opener=org_opener, usage_opener=usage_opener)
    policy = {**capped(load_policy(DEFAULT_POLICY_PATH)), "timers": {"probe_interval_s": 300, "keepalive_interval_s": 18300},
              "reset_credits": {"enabled": False, "headroom_floor_pct": 15, "min_interval_min": 60}}
    with Store(tmp_path / "state.sqlite3") as store:
        for index, (lane_id, label) in enumerate(LANES.items()):
            number = lane_id.split("-")[1]
            monkeypatch.setenv(f"SF_TOK_{number}", f"tok-{number}")
            store.put_lane(Lane(lane_id, "claude", f"claude:{label}", Credential("claude", f"SF_TOK_{number}", "env"),
                                None, LaneOwner.V2, False, True, None, label), identity_status="enrolled")
        notices = []
        timer = Timers(store, tmp_path, policy, adapter_factory=factory, now=clock,
                       deliver=lambda notice: notices.append(notice) or True)
        monkeypatch.setattr(timer, "_pace_usage", lambda: None)
        yield timer, store, orgs, notices, org_calls, tmp_path
        timer.stop()


def rows(store):
    return {row["lane_id"]: dict(row) for row in store.query(
        "SELECT lane_id,identity,identity_status,enabled FROM lanes ORDER BY lane_id")}


def test_c10_8_d_id1_one_cycle_finds_the_shared_account_and_keeps_one_lane(fleet):
    """C-10.6, C-10.8 one probe cycle: every lane learns the organization its own
    token names; claude-4, the earliest binding, takes the shared account's work;
    claude-7 and claude-19 are refused it; claude-9 is untouched. A critical alert
    names all three, status.json shows the two as not dispatchable, and no twin
    alert guesses at what the identities already say."""
    timer, store, orgs, notices, org_calls, root = fleet
    snapshot = timer.probe_cycle()
    learned = rows(store)
    assert {lane_id: row["identity"] for lane_id, row in learned.items()} == {
        "claude-4": f"org:{SHARED}", "claude-7": f"org:{SHARED}", "claude-19": f"org:{SHARED}",
        "claude-9": f"org:{OWN_9}"}
    assert {row["identity_status"] for row in learned.values()} == {"enrolled"}
    assert all(row["enabled"] for row in learned.values())       # refused work, not disabled
    by_id = {lane["lane_id"]: lane for lane in snapshot["lanes"]}
    assert by_id["claude-7"]["identity_shadowed_by"] == "claude-4"
    assert by_id["claude-19"]["identity_shadowed_by"] == "claude-4"
    assert by_id["claude-4"]["identity_shadowed_by"] is None
    assert "identity_shadowed_by" not in by_id["claude-9"]

    keys = [notice["key"] for notice in notices]
    assert f"claude-identity-shared:{SHARED}" in keys
    assert not any("reading-twins" in key for key in keys)
    shared = next(notice for notice in notices if notice["key"] == f"claude-identity-shared:{SHARED}")
    assert shared["severity"] == "critical"
    for lane_id, label in list(LANES.items())[:3]:
        assert f"{lane_id} ({label})" in shared["body"]
    assert "Only claude-4 takes its work" in shared["body"]

    status = json.loads((root / "status.json").read_text())
    accounts = {row["lane_id"]: row for row in status["claude"]["accounts"]}
    assert accounts["claude-7"]["dispatchable"] is False and accounts["claude-19"]["dispatchable"] is False
    assert accounts["claude-7"]["identity_shadowed_by"] == "claude-4"
    assert accounts["claude-4"]["identity_shared_with"] == ["claude-19", "claude-7"]


def test_c10_8_no_keepalive_is_spent_on_a_lane_whose_account_another_takes(fleet):
    """C-10.8 the shared account's keepalive is claude-4's; claude-7 and claude-19
    spend no turn of their own on it."""
    timer, store, orgs, notices, org_calls, root = fleet
    timer.probe_cycle()
    turned = []
    timer.turn = lambda lane, purpose, holder, **_: turned.append(lane.lane_id) or Outcome(OutcomeClass.OK, "ok")
    timer.keepalive_cycle()
    assert sorted(turned) == ["claude-4", "claude-9"]


def test_c10_6_an_organization_is_asked_once_per_token_not_once_per_read(fleet):
    """C-10.6, D-ID1 a token's organization never changes; the timers build an
    adapter per read, and the second cycle asks nothing again."""
    timer, store, orgs, notices, org_calls, root = fleet
    timer.probe_cycle()
    asked = len(org_calls)
    timer.probe_cycle()
    assert asked == 4 and len(org_calls) == asked


def test_c10_6_a_token_replaced_by_another_accounts_is_mismatch_and_leaves_the_group(fleet, monkeypatch):
    """C-10.6 once claude-7 recorded the shared organization, a token of its own
    account under the same credential is another account: `mismatch`, no
    readings of that cycle, and it no longer shares the account claude-4 takes."""
    timer, store, orgs, notices, org_calls, root = fleet
    timer.probe_cycle()
    before = store.query("SELECT COUNT(*) AS n FROM readings WHERE lane_id='claude-7' AND label='provider'")[0]["n"]
    monkeypatch.setenv("SF_TOK_7", "tok-7-own")
    orgs["tok-7-own"] = OWN_7
    snapshot = timer.probe_cycle()
    assert rows(store)["claude-7"]["identity_status"] == "mismatch"
    assert rows(store)["claude-7"]["identity"] == f"org:{SHARED}"           # learned once, never rewritten
    after = store.query("SELECT COUNT(*) AS n FROM readings WHERE lane_id='claude-7' AND label='provider'")[0]["n"]
    assert after == before
    by_id = {lane["lane_id"]: lane for lane in snapshot["lanes"]}
    assert "identity_shadowed_by" not in by_id["claude-7"]
    assert by_id["claude-19"]["identity_shadowed_by"] == "claude-4"


def test_c10_6_a_busy_lanes_read_still_records_what_its_credential_said(fleet, monkeypatch):
    """C-10.6, C-18.3 a busy lane's read publishes nothing a newer limit could
    contradict, but a credential that turned into another account's is kept on
    the row all the same."""
    timer, store, orgs, notices, org_calls, root = fleet
    timer.probe_cycle()
    monkeypatch.setenv("SF_TOK_7", "tok-7-own")
    orgs["tok-7-own"] = OWN_7
    store.acquire_lease("lane:claude-7:slot:1", "attempt-busy")
    timer.probe_cycle()
    assert rows(store)["claude-7"]["identity_status"] == "mismatch"


def test_c10_6_profile_facts_turn_an_unproven_label_into_a_verdict(fleet):
    """C-10.6 when a full login's profile names the shared organization as
    max@maxghenis.com's personal one, claude-4 is `verified` and the other two
    `mismatch`, by name, on the next cycle."""
    timer, store, orgs, notices, org_calls, root = fleet
    timer.probe_cycle()
    claude_cards.write_snapshot(root / claude_cards.SNAPSHOT_FILE, {
        "version": claude_cards.SNAPSHOT_VERSION, "read_at": "2026-10-09T12:00:00Z",
        "accounts": [{"login": "max@maxghenis.com", "identity": f"acct-4:{SHARED}", "email": "max@maxghenis.com",
                      "plan": {"organization_type": "claude_max"}, "read_at": "2026-10-09T12:00:00Z"}]})
    timer.probe_cycle()
    statuses = {lane_id: row["identity_status"] for lane_id, row in rows(store).items()}
    assert statuses == {"claude-4": "verified", "claude-7": "mismatch", "claude-19": "mismatch", "claude-9": "enrolled"}
    event = next(json.loads(row["data_json"]) for row in store.query(
        "SELECT data_json FROM events WHERE kind='lane.identity' AND lane_id='claude-7' ORDER BY event_id DESC")
        if json.loads(row["data_json"]).get("to") == "mismatch")
    assert event["fact"]["email"] == "max@maxghenis.com" and event["fact"]["source"] == "login:max@maxghenis.com"


# --- the surfaces --------------------------------------------------------------


NOW = "2026-10-09T12:00:00Z"


def view_lane(lane_id, identity, **extra):
    provider = lane_id.split("-")[0]
    return {"lane_id": lane_id, "provider": provider, "account_key": f"{provider}:{lane_id}@x.example",
            "label": f"{lane_id}@x.example", "owner": "v2", "enabled": True, "desktop": False,
            "home": None, "identity": identity, "identity_status": "enrolled",
            "created_at": f"2026-09-0{lane_id[-1]}T00:00:00Z", **extra}


def test_c10_8_the_scheduler_refuses_a_shadowed_lane_and_calls_it_standing():
    """C-10.8 a lane whose account another lane takes is refused as
    `identity-shared`, which no wait for capacity ends."""
    policy = capped(load_policy(DEFAULT_POLICY_PATH))
    policy["reserve"] = {**policy.get("reserve", {}), "models": []}
    view = build_view([view_lane("claude-1", f"org:{SHARED}"), view_lane("claude-2", f"org:{SHARED}")], now=NOW)
    decision = evaluate(policy, view, {"task": "research", "tier": "standard", "sandbox": "read-only",
                                       "pinned_model": "opus"})
    reasons = {row["lane_id"]: row["reasons"] for row in decision.evaluations[0]["rejections"]}
    assert "identity-shared" in reasons["claude-2"]
    assert "claude-1" not in reasons or "identity-shared" not in reasons["claude-1"]
    assert "identity-shared" in STANDING_REFUSALS
    assert "claude-2" not in capacity.open_lanes(view, policy["caps"])


def test_c10_8_status_json_never_counts_a_shadowed_lane_as_dispatchable_or_its_reset():
    later = (datetime(2026, 10, 9, 12, tzinfo=timezone.utc) + timedelta(days=2)).isoformat().replace("+00:00", "Z")
    sooner = (datetime(2026, 10, 9, 12, tzinfo=timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    readings = [{"lane_id": lane_id, "scope": "account", "window": "seven_day", "utilization": .4, "resets_at": at,
                 "observed_at": NOW, "label": "provider", "source": "oauth-usage"}
                for lane_id, at in (("claude-1", later), ("claude-2", sooner))]
    view = build_view([view_lane("claude-1", f"org:{SHARED}"), view_lane("claude-2", f"org:{SHARED}")],
                      readings, now=NOW)
    shadowed = next(lane for lane in view["lanes"] if lane["lane_id"] == "claude-2")
    assert not dispatchable(shadowed)
    status = build_status(view, now=NOW)
    assert status["claude"]["earliest_reset"] == later


def test_c10_3_a_setup_token_lane_is_the_desktops_when_its_organization_is():
    """C-10.3, C-10.7 the desktop's profile names an account; a lane that recorded
    only that account's organization is the desktop's login all the same."""
    desktop = DesktopIdentity("verified", f"acct-d:{SHARED}", "d@x.example")
    assert desktop.owns({"provider": "claude", "identity": f"org:{SHARED}"})
    assert not desktop.owns({"provider": "claude", "identity": f"org:{OWN_9}", "label": "d@x.example"})


def test_c9_10_a_login_backs_the_lanes_whose_organization_its_profile_names():
    """C-9.10, C-10.6 a login is bound to a setup-token lane by organization; a
    lane labelled with the login's name whose token is another account's is not
    backed by it (D-ID1: max@hivesight.ai's login does not back claude-7)."""
    lanes = [{"lane_id": "claude-4", "identity": f"org:{SHARED}", "label": "max@maxghenis.com"},
             {"lane_id": "claude-7", "identity": f"org:{SHARED}", "label": "max@hivesight.ai"},
             {"lane_id": "claude-8", "identity": None, "label": "max@hivesight.ai"}]
    bound, how = claude_cards.associate(lanes, f"acct-h:{OWN_7}", "max@hivesight.ai")
    assert [lane["lane_id"] for lane in bound] == ["claude-8"] and how == "label"
    bound, how = claude_cards.associate(lanes, f"acct-4:{SHARED}", "max@maxghenis.com")
    assert [lane["lane_id"] for lane in bound] == ["claude-4", "claude-7"] and how == "identity"


def test_c10_9_a_twin_alert_names_both_lanes_and_what_matched():
    """C-10.9 two Codex homes reporting one account's windows, which no identity
    explains: a warning naming both, the windows and the values."""
    lanes = [view_lane("codex-1", None, readings=[]), view_lane("codex-2", None, readings=[])]
    samples = []
    for lane_id in ("codex-1", "codex-2"):
        samples.append({"lane_id": lane_id, "scope": "account", "window": "seven_day", "utilization": .62,
                        "resets_at": "2026-10-12T09:00:00Z", "observed_at": NOW, "label": "provider"})
        samples.append({"lane_id": lane_id, "scope": "account", "window": "five_hour", "utilization": .3,
                        "resets_at": "2026-10-09T15:00:00Z", "observed_at": NOW, "label": "provider"})
    conditions = {row["key"]: row for row in evaluate_conditions({"lanes": lanes, "weekly_samples": samples},
                                                                 now=NOW)}
    twin = conditions["codex-reading-twins:codex-1+codex-2"]
    assert twin["severity"] == "warn"
    assert "codex-1 (codex-1@x.example)" in twin["body"] and "codex-2 (codex-2@x.example)" in twin["body"]
    assert "seven_day at 62%" in twin["body"] and "five_hour at 30%" in twin["body"]


def test_c10_8_the_lanes_table_and_status_flags_say_who_takes_the_work():
    lanes = [view_lane("claude-4", f"org:{SHARED}"), view_lane("claude-7", f"org:{SHARED}")]
    table = _format_lanes({"lanes": lanes})
    assert "org:5ba7ed00" in table
    assert "one account with claude-7; this lane takes its work" in table
    assert "one account with claude-4; claude-4 takes its work" in table
    text = render.status(build_view(lanes, now=NOW))
    assert "identity-shared=claude-4" in text
