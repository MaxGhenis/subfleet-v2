"""C-9.10 wiring: the card timer, status, `status.json`, alerts, the `cards` verb and policy."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import claude_cards as cc, cli, operations, protocol, render
from subfleet.alerts import card_condition, evaluate_conditions
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
from subfleet.status_json import build_status
from subfleet.timers import Timers
from tests.unit.test_claude_cards import Login, Wire, fixture, healthy
from tests.unit.test_timers_probe import rig, events  # noqa: F401 - fixtures


def claude_lane(store, lane_id, label, *, held_until=None):
    lane = Lane(lane_id, "claude", f"claude:{label}", Credential("claude", f"claude-quota-{label}", "keychain-token"),
                None, LaneOwner.V2, False, True, None, label)
    store.put_lane(lane)
    if held_until:
        store.add_closure(Closure(lane_id, "account", held_until, ClosureReason.OPERATOR_HOLD,
                                  ClockSource.REPORTED, "operator"))
    return lane


def install_sensor(timer, logins, wire):
    class Many:
        def read(self, home):
            return logins[home.name].read(home)

        def heal(self, home):
            return logins[home.name].heal(home)
    many = Many()
    timer._cards_sensor = cc.Sensor(login_reader=many.read, heal=many.heal, version=lambda: "2.1.286",
                                    opener=wire, now=timer.now)


def login_dirs(root, *names):
    for name in names:
        (root / "logins" / name).mkdir(parents=True, exist_ok=True)


def test_cycle_writes_snapshot_records_event_and_spares_held_accounts(rig, tmp_path):
    """C-9.10: one cycle reads every login, writes the snapshot, records what it did, and spends no
    turn on an account under an operator hold."""
    timer, store, clock, adapter, enroll = rig
    claude_lane(store, "claude-11", "max@ax.example")
    claude_lane(store, "claude-2", "max@pe.example", held_until="2099-12-31T00:00:00Z")
    login_dirs(tmp_path, "max@ax.example", "max@pe.example")
    logins = {"max@ax.example": Login(expires_in_s=-60, clock=clock), "max@pe.example": Login(expires_in_s=-60, clock=clock)}
    install_sensor(timer, logins, Wire(healthy()))
    summary = timer.claude_cards_cycle()
    assert summary["accounts"]["max@ax.example"] == {"status": "ok", "lanes": ["claude-11"], "unused": 1}
    assert summary["accounts"]["max@pe.example"]["status"] == "held"
    assert summary["heals"] == ["max@ax.example"] and logins["max@pe.example"].heals == []
    snap = cc.read_snapshot(tmp_path / cc.SNAPSHOT_FILE)
    assert [row["login"] for row in snap["accounts"]] == ["max@ax.example", "max@pe.example"]
    assert events(store, "timer.claude-cards")[-1]["read_at"] == snap["read_at"]
    assert "tok-secret" not in (tmp_path / cc.SNAPSHOT_FILE).read_text()


def test_probe_cycle_publishes_cards_and_raises_expiry_alert(rig, tmp_path):
    """C-9.10, C-18.4: every publication carries the cards; a card ending within five days is an alert."""
    timer, store, clock, adapter, enroll = rig
    enroll("codex-1")
    ends = (clock() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cc.write_snapshot(tmp_path / cc.SNAPSHOT_FILE, {"version": 1, "read_at": "2026-09-05T11:00:00Z", "accounts": [
        {"login": "max@ax.example", "lanes": ["claude-11"], "status": "ok", "read_at": "2026-09-05T11:00:00Z",
         "plan": {"organization_type": "claude_max", "subscription_status": "active"},
         "cards": {"eligible": True, "grants": [{"id": "g1", "resets_left": 1, "resets_total": 1, "ends_at": ends,
                                                  "usable_now": True}]},
         "credits": [], "cloud_credit_claim": None}]})
    timer.probe_cycle()
    status = json.loads((tmp_path / "status.json").read_text())
    [account] = status["claude"]["cards"]["accounts"]
    assert account["unused_cards"] == 1 and account["cards"][0]["id"] == "g1"
    assert "identity" not in account and "home" not in account
    # The cycle judged the condition; whether a notice is delivered is the daemon's callback.
    condition = timer.alerts._current["claude-card-expiring:max@ax.example:g1"]
    assert condition["severity"] == "warn" and "never redeems" in condition["body"]


def test_request_runs_on_its_own_worker_and_respects_disable(rig, tmp_path):
    """C-9.10: `cards --refresh` queues a read; a policy that switches the sensor off reads nothing."""
    timer, store, clock, adapter, enroll = rig
    login_dirs(tmp_path, "solo@example")
    install_sensor(timer, {"solo@example": Login(clock=clock)}, Wire(healthy()))
    timer.start()
    assert timer.request("claude_cards")["status"] == "scheduled"
    deadline = time.monotonic() + 30
    while not events(store, "timer.claude-cards") and time.monotonic() < deadline:
        time.sleep(.05)
    assert events(store, "timer.claude-cards")
    off = Timers(store, tmp_path / "off", {**timer.policy, "claude_cards": {"enabled": False}})
    try:
        off.start()
        assert "claude_cards" not in off.intervals
        assert off.request("claude_cards")["status"] == "disabled"
    finally:
        off.stop()


def test_cards_operation_reads_and_refreshes(rig, tmp_path):
    """C-9.10: the `cards` operation returns the view; `refresh` queues the timer; nothing else is a target."""
    timer, store, clock, adapter, enroll = rig
    service = SimpleNamespace(store=store, timers=timer)
    shown = operations.dispatch(service, protocol.OperationsArgs("cards"))
    assert shown["status"] == "ok" and shown["cards"]["accounts"] == []
    queued = operations.dispatch(service, protocol.OperationsArgs("cards", target="refresh"))
    assert queued["status"] == "recovering" and "cards" in queued          # timers not started yet
    with pytest.raises(protocol.ProtocolError):
        operations.dispatch(service, protocol.OperationsArgs("cards", target="redeem"))


def test_alert_conditions_and_text():
    """C-9.10: each warning becomes one daily alert per login; every body disclaims redemption."""
    at = datetime(2026, 10, 20, tzinfo=timezone.utc)
    warnings = [
        {"kind": "card-expiring", "key": "a:g", "login": "a", "lanes": ["claude-1"], "grant": "g",
         "at": "2026-10-22T16:00:00Z", "resets_left": 1},
        {"kind": "card-lapse-risk", "key": "a:g:lapse", "login": "a", "lanes": [], "grant": "g", "at": None,
         "resets_left": 1, "reasons": ["subscription past_due"]},
        {"kind": "credit-expiring", "key": "a:iguana_necktie", "login": "a", "lanes": [], "credit": "iguana_necktie",
         "label": "cloud-session credit", "at": "2026-11-05T07:59:00Z", "remaining_dollars": 250.0},
        {"kind": "credit-claimable", "key": "a:cloud_credit:claim", "login": "a", "lanes": [], "credit": "cloud_credit"},
        {"kind": "credit-lapse-risk", "key": "a:iguana_necktie:lapse", "login": "a", "lanes": [],
         "credit": "iguana_necktie", "label": "cloud-session credit", "remaining_dollars": 250.0,
         "reasons": ["plan ends 2026-10-22T00:00:00Z"]},
    ]
    conditions = [card_condition(row, at) for row in warnings]
    assert [c["key"] for c in conditions] == ["claude-card-expiring:a:g", "claude-card-lapse-risk:a:g:lapse",
                                              "claude-credit-expiring:a:iguana_necktie",
                                              "claude-credit-claimable:a:cloud_credit:claim",
                                              "claude-credit-lapse-risk:a:iguana_necktie:lapse"]
    assert all(c["daily"] and c["home"] == "claude-cards:a" and "never redeems" in c["body"] for c in conditions)
    # Used or lost is not a recovery: these latches clear with no "recovered" notice.
    assert all(c["recover"] is False for c in conditions)
    assert "in 2.7 days" in conditions[0]["body"] and "$250.00" in conditions[2]["body"]
    assert card_condition({"kind": "something-else"}, at) is None
    lost = card_condition({"kind": "card-lost", "key": "a:lost:g", "login": "a", "lanes": [], "grants": ["g"],
                           "at": "2026-10-19T00:00:00Z", "reason": "lapse"}, at)
    assert lost["once"] is True and "daily" not in lost and "lost with its plan" in lost["subject"]
    credit = card_condition({"kind": "credit-lost", "key": "a:credit-lost:c", "login": "a", "lanes": [],
                             "credits": [{"key": "c", "label": "cloud-session credit", "remaining_dollars": 250.0}],
                             "at": "2026-10-19T00:00:00Z", "reason": "lapse"}, at)
    assert "plan lapsed with promotional credit unspent" in credit["subject"] and "is gone" not in credit["body"]
    found = evaluate_conditions({"lanes": [], "claude_cards": {"warnings": warnings}}, now=at)
    assert len([row for row in found if row["key"].startswith("claude-")]) == 5


def test_watch_preview_includes_card_conditions(rig, tmp_path):
    """C-9.10: `watch --dry-run` previews the card and credit conditions the next cycle would raise."""
    timer, store, clock, adapter, enroll = rig
    ends = (clock() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cc.write_snapshot(tmp_path / cc.SNAPSHOT_FILE, {"version": 1, "read_at": "2026-09-05T11:00:00Z", "accounts": [
        {"login": "a", "lanes": [], "status": "ok", "read_at": "2026-09-05T11:00:00Z",
         "cards": {"eligible": True, "grants": [{"id": "g", "resets_left": 1, "ends_at": ends}]}, "credits": []}]})
    service = SimpleNamespace(store=store, timers=timer, _cached_desktop_identity=lambda: None,
                              _capacity_view=lambda desktop: timer.snapshot())
    result = operations.dispatch(service, protocol.OperationsArgs("watch", dry_run=True))
    assert "claude-card-expiring:a:g" in [row["key"] for row in result["conditions"]]


def test_status_text_lists_every_login():
    """C-9.10: `status` shows each login's cards and credits, or why they are unknown, and what to run."""
    now = datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc)
    snap = {"version": 1, "read_at": "2026-10-05T15:30:00Z", "accounts": [
        {"login": "max@ax.example", "lanes": ["claude-11", "claude-18"], "lanes_by": "label", "status": "ok",
         "read_at": "2026-10-05T15:30:00Z", "plan": {"organization_type": "claude_max", "subscription_status": "active"},
         "cards": cc.parse_cards(fixture("usage_unused_card_at_limit")["cedar_ember"]),
         "credits": cc.parse_credits(fixture("usage_unused_card_at_limit"))},
        {"login": "max@hs.example", "lanes": ["claude-7"], "status": "ok", "read_at": "2026-10-05T15:30:00Z",
         "cards": cc.parse_cards(fixture("usage_used_card")["cedar_ember"]), "credits": []},
        {"login": "gone@example", "lanes": [], "status": "lapsed",
         "plan": {"organization_type": "claude_free", "subscription_status": "canceled"}},
        {"login": "dead@example", "home": "/x/logins/dead@example", "lanes": ["claude-10"], "status": "login-dead",
         "detail": "the CLI could not renew this login"},
    ]}
    shown = cc.view(snap, now, warn_days=5)
    text = "\n".join(render.card_lines(shown))                    # `subfleet cards`: every login
    assert "max@ax.example [claude-11, claude-18 by name]: 1 unused reset card (opus55-launch-promax-20260921), " \
           "expires 2026-10-22T16:00:00Z, usable now, account at its limit" in text
    assert "cloud-session credit $250.00 of $250.00 left, expires 2026-11-05T07:59:00Z" in text
    assert "max@hs.example [claude-7]: reset card used" in text
    assert "gone@example: lapsed (claude_free, subscription canceled)" in text
    assert "CLAUDE_CONFIG_DIR=/x/logins/dead@example claude auth login" in text
    assert render.card_lines({}) == ["claude reset cards: not read yet (subfleet cards --refresh)"]
    status = cli.format_status({"lanes": [], "claude_cards": shown})  # `status`: what can be lost, and a tally
    assert "max@ax.example [claude-11, claude-18 by name]: 1 unused reset card" in status
    assert "  others: 1 card used, 1 lapsed, 1 login-dead (subfleet cards)" in status
    assert "gone@example" not in status and "dead@example" not in status


def test_status_json_cards_section_is_always_present():
    """C-9.10: `claude.cards` is in every `status.json`, empty before the first read."""
    assert build_status({"lanes": []})["claude"]["cards"] == {"read_at": None, "disabled": False,
                                                              "accounts": [], "warnings": []}


def test_disabled_sensor_shows_and_alerts_nothing(rig, tmp_path):
    """C-9.10: with `claude_cards.enabled` false, an old snapshot raises no alert and status says the
    sensor is off."""
    timer, store, clock, adapter, enroll = rig
    ends = (clock() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cc.write_snapshot(tmp_path / cc.SNAPSHOT_FILE, {"version": 1, "read_at": "2026-09-05T11:00:00Z", "accounts": [
        {"login": "a", "lanes": [], "status": "ok", "read_at": "2026-09-05T11:00:00Z",
         "cards": {"eligible": True, "grants": [{"id": "g", "resets_left": 1, "ends_at": ends}]}, "credits": []}]})
    timer.policy["claude_cards"] = {"enabled": False}
    shown = timer.cards_view()
    assert shown["disabled"] and shown["warnings"] == [] and shown["accounts"] == []
    assert render.card_lines(shown) == ["claude reset cards: not read (claude_cards.enabled is false in the policy)"]


def test_hold_merged_into_a_provider_limit_still_holds(rig, tmp_path):
    """C-9.10: a hold whose closure `put_closure` merged into a longer provider limit on the same
    scope is still read from its `lane.held` event, so no heal is spent under it."""
    timer, store, clock, adapter, enroll = rig
    claude_lane(store, "claude-2", "max@pe.example")
    later = (clock() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.add_closure(Closure("claude-2", "account", later, ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "test"))
    soon = (clock() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with store.transaction("lane.held", lane_id="claude-2", data={"until": soon}):
        store.add_closure(Closure("claude-2", "account", soon, ClosureReason.OPERATOR_HOLD, ClockSource.REPORTED, "operator"))
    assert not store.query("SELECT 1 FROM closures WHERE reason='operator-hold'")      # merged away
    [lane] = timer._card_lanes()
    assert lane["held"] is True
    # Releasing it changes no row, so no `lane.released` event is written: the hold
    # is kept until its own end, which errs toward spending nothing.
    clock.advance(2 * 3600)
    [lane] = timer._card_lanes()
    assert lane["held"] is False


def test_released_hold_no_longer_holds(rig, tmp_path):
    """C-9.10: `lanes release` (an `operator-hold` row released, a `lane.released` event) ends the hold."""
    timer, store, clock, adapter, enroll = rig
    claude_lane(store, "claude-2", "max@pe.example", held_until="2099-12-31T00:00:00Z")
    with store.transaction("lane.held", lane_id="claude-2", data={"until": "2099-12-31T00:00:00Z"}):
        store.add_closure(Closure("claude-2", "account", "2099-12-31T00:00:00Z", ClosureReason.OPERATOR_HOLD,
                                  ClockSource.REPORTED, "operator"))
    assert timer._card_lanes()[0]["held"] is True
    with store.transaction("lane.released", lane_id="claude-2") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE lane_id=? AND reason='operator-hold' AND released_at IS NULL",
                   ("2026-09-05T12:00:00Z", "claude-2"))
    assert timer._card_lanes()[0]["held"] is False


def test_policy_defaults_and_validation(tmp_path):
    """C-9.10: a policy without `claude_cards` reads the defaults; a bad value names its key."""
    base = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    base.pop("claude_cards")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(base))
    assert load_policy(path)["claude_cards"] == cc.CLAUDE_CARDS_DEFAULTS
    for key, value in (("interval_min", 10), ("heal", "yes"), ("warn_days", -1), ("logins_dir", " "),
                       ("heal_interval_min", 5)):
        path.write_text(json.dumps({**base, "claude_cards": {key: value}}))
        with pytest.raises(PolicyError, match=f"claude_cards.{key}"):
            load_policy(path)


def test_offline_status_reads_the_last_snapshot(tmp_path):
    """C-9.10: with the daemon down, `status` still shows the last snapshot the daemon wrote."""
    from subfleet.offline import Offline
    from subfleet.store import Store
    with Store(tmp_path / "state.sqlite3"):
        pass
    cc.write_snapshot(tmp_path / cc.SNAPSHOT_FILE, {"version": 1, "read_at": "2026-10-05T15:30:00Z", "accounts": []})
    view = Offline(tmp_path).status()["claude_cards"]
    assert view["read_at"] == "2026-10-05T15:30:00Z" and view["accounts"] == []
