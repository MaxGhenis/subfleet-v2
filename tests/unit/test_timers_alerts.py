"""Durable monitoring alerts: C-18.1, C-23.27, C-23.45–47, C-23.52."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from subfleet.alerts import Alerts, evaluate_conditions
from subfleet.store import Store

NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def lane(identity="one", **extra):
    return {"lane_id": identity, "provider": "codex", "account_key": f"codex:{identity}",
            "home": f"/homes/{identity}", "credential_ref": f"/homes/{identity}", "enabled": True,
            "owner": "v2", "verdict": "ok", "readings": [], "closures": [], **extra}


def snapshot(*lanes, **extra):
    return {"lanes": [lane("healthy-1"), lane("healthy-2"), *lanes], **extra}


@pytest.fixture
def alerts(tmp_path):
    """C-18.1: notices use an injectable ping delivery path and durable SQLite latches."""
    delivered = []
    with Store(tmp_path / "store.db") as store:
        yield Alerts(store, {}, delivered.append), delivered, store


def test_transition_realert_and_restart_honor_durable_latch(alerts):
    """C-18.1: alert on transition, then no sooner than six hours, including after restart."""
    monitor, delivered, store = alerts
    view = snapshot(lane(verdict="auth-dead", enabled=False))
    assert monitor.evaluate(view, now=NOW)["alerts_sent"] == ["codex-revoked:/homes/one"]
    monitor = Alerts(store, {}, delivered.append)
    assert not monitor.evaluate(view, now=NOW + timedelta(hours=5, minutes=59))["alerts_sent"]
    assert monitor.evaluate(view, now=NOW + timedelta(hours=6))["alerts_sent"] == ["codex-revoked:/homes/one"]
    assert len(delivered) == 2


def test_one_recovery_when_last_condition_for_home_clears(alerts):
    """C-23.52: clearing two conditions on one home emits one recovery, once."""
    monitor, delivered, _ = alerts
    bad = lane(verdict="auth-dead", closures=[{"scope": "model", "reason": "provider-limit", "until_at": "2026-09-06T12:00:00Z"}])
    monitor.evaluate(snapshot(bad), now=NOW)
    result = monitor.evaluate(snapshot(lane()), now=NOW + timedelta(minutes=1))
    assert result["recovered"] == ["/homes/one"]
    assert not monitor.evaluate(snapshot(lane()), now=NOW + timedelta(minutes=2))["recovered"]
    assert sum(bool(notice.get("recovery")) for notice in delivered) == 1


def test_condition_switch_is_not_recovery(alerts):
    """C-23.52: revoked→no-auth reports the new credential condition without an all-clear."""
    monitor, delivered, _ = alerts
    monitor.evaluate(snapshot(lane(verdict="auth-dead")), now=NOW)
    result = monitor.evaluate(snapshot(lane(verdict="no-auth")), now=NOW + timedelta(minutes=1))
    assert result["alerts_sent"] == ["codex-noauth:/homes/one"]
    assert not result["recovered"]
    assert not any(notice.get("recovery") for notice in delivered)


def test_offline_cycle_suppresses_all_alerts_and_recoveries(alerts):
    """C-23.27: all-network-error cycles are silent, including scoped limits and recoveries."""
    monitor, delivered, store = alerts
    monitor.evaluate(snapshot(lane(verdict="auth-dead")), now=NOW)
    before = len(delivered)
    view = {"lanes": [lane(probe_status="network-error"), lane("two", provider="claude",
            closures=[{"scope": "fable", "reason": "provider-limit", "until_at": "2026-09-06T12:00:00Z"}])]}
    result = monitor.evaluate(view, now=NOW + timedelta(hours=7))
    assert result["offline"]
    assert not result["alerts_sent"] and not result["recovered"]
    assert len(delivered) == before
    assert monitor.latches["codex-revoked:/homes/one"]["active"]
    assert store.query("SELECT * FROM events WHERE kind='monitoring.offline'")


@pytest.mark.parametrize("shape", ["row", "wrapped", "map", "latch"])
def test_imported_v1_latches_honored_on_first_cycle(alerts, shape):
    """C-18.1, C-23.27: imported v1 alert-latch events prevent immediate duplicate notices."""
    _, delivered, store = alerts
    key = "codex-revoked:/homes/one"
    state = {"active": True, "last_sent": NOW.isoformat()}
    data = ({"key": key, **state} if shape == "row" else {"latches": {key: state}} if shape == "wrapped"
            else {key: state} if shape == "map" else {"key": key, "latch": state})
    store.add_event("alert-latch", data=data)
    monitor = Alerts(store, {}, delivered.append)
    assert not monitor.evaluate(snapshot(lane(verdict="auth-dead")), now=NOW + timedelta(minutes=1))["alerts_sent"]
    assert not delivered


def test_expiring_capacity_daily_even_after_condition_reappears(alerts):
    """C-18.1: expiring capacity re-alerts at most daily, even across intervening clear cycles."""
    monitor, delivered, _ = alerts
    view = snapshot(capacity_expiry={"projected_unused_windows": 2, "earliest_reset_at": (NOW + timedelta(days=2)).isoformat()})
    monitor.evaluate(view, now=NOW)
    assert not monitor.evaluate(view, now=NOW + timedelta(hours=6))["alerts_sent"]
    monitor.evaluate(snapshot(), now=NOW + timedelta(hours=7))
    assert not monitor.evaluate(view, now=NOW + timedelta(hours=8))["alerts_sent"]
    assert monitor.evaluate(view, now=NOW + timedelta(hours=24))["alerts_sent"] == ["codex-capacity-expiring"]
    assert sum(notice["key"] == "codex-capacity-expiring" for notice in delivered) == 2


def test_credential_notices_have_exact_operator_commands(alerts):
    """C-23.44, C-23.47, C-23.52: every credential alert names the operator's login and re-enrolment commands."""
    monitor, delivered, _ = alerts
    monitor.evaluate(snapshot(lane(verdict="auth-dead", home="/homes/a space", credential_ref="/homes/a space"),
                              lane("claude", provider="claude", verdict="token-invalid", credential_ref="claude-quota-a@example.org")), now=NOW)
    credential = [notice for notice in delivered if "credentials:" in notice["subject"]]
    assert len(credential) == 2
    assert "CODEX_HOME='/homes/a space' codex login; subfleet lanes enroll '/homes/a space'" in credential[0]["body"]
    assert "claude setup-token; subfleet lanes enroll claude-quota-a@example.org" in credential[1]["body"]


def test_duplicate_alert_names_both_homes_and_survives_noncanonical_disable(alerts):
    """C-23.45: a duplicate account raises a critical alert naming both homes after disabling the later binding."""
    monitor, delivered, _ = alerts
    view = snapshot(lane("first", account_key="codex:duplicate"), lane("second", account_key="codex:duplicate",
                    enabled=False, duplicate_of="/homes/first", identity_status="non-canonical"))
    monitor.evaluate(view, now=NOW)
    duplicate = next(notice for notice in delivered if notice["key"] == "codex-dup:duplicate")
    assert duplicate["severity"] == "critical"
    assert "/homes/first" in duplicate["body"] and "/homes/second" in duplicate["body"]


def test_post_heal_snapshot_controls_every_condition(alerts):
    """C-23.27: stale pre-heal probe errors cannot override a post-heal ok verdict."""
    monitor, delivered, _ = alerts
    view = snapshot(lane(verdict="ok", probe_status="ok", pre_heal_verdict="auth-suspect"))
    assert not monitor.evaluate(view, now=NOW)["alerts_sent"]
    assert not delivered


def test_delivery_failure_does_not_latch_or_lose_retry(alerts):
    """C-18.1, C-23.52: failed notice enqueue does not mark an alert or recovery delivered."""
    monitor, delivered, _ = alerts
    monitor.deliver = lambda notice: False
    view = snapshot(lane(verdict="auth-dead"))
    assert not monitor.evaluate(view, now=NOW)["alerts_sent"]
    assert not monitor.latches
    monitor.deliver = delivered.append
    assert monitor.evaluate(view, now=NOW + timedelta(seconds=1))["alerts_sent"]
    monitor.deliver = lambda notice: False
    assert not monitor.evaluate(snapshot(lane()), now=NOW + timedelta(seconds=2))["recovered"]
    monitor.deliver = delivered.append
    assert monitor.evaluate(snapshot(lane()), now=NOW + timedelta(seconds=3))["recovered"]


def test_shadowing_is_transition_only_and_no_recovery(alerts):
    """C-23.46: app shadowing alerts once per transition and leaves dispatchability intact."""
    monitor, delivered, _ = alerts
    view = snapshot(lane(app_shadowed=True))
    assert monitor.evaluate(view, now=NOW)["alerts_sent"] == ["codex-app-shadow:/homes/one"]
    assert not monitor.evaluate(view, now=NOW + timedelta(days=1))["alerts_sent"]
    assert not monitor.evaluate(snapshot(lane()), now=NOW + timedelta(days=2))["recovered"]
    assert len(delivered) == 1


def test_scoped_closure_never_exposes_unmeasured_percentage():
    """C-9.1, C-23.27: scoped limit notices report the closure without inventing quota percentages."""
    view = snapshot(lane("claude", provider="claude", closures=[{"scope": "fable", "reason": "provider-limit", "until_at": "2026-09-06T12:00:00Z"}]))
    conditions = evaluate_conditions(view, now=NOW)
    notice = next(condition for condition in conditions if "scoped-limit" in condition["key"])
    assert "%" not in json.dumps(notice)


def test_expiring_observed_weekly_capacity_alerts_daily_per_home(alerts):
    """C-9.1, C-18.1: measured unused weekly capacity expiring within 24h alerts daily for each home."""
    monitor, delivered, _ = alerts
    weekly = {"scope": "account", "window": "seven_day", "utilization": 0.25,
              "resets_at": (NOW + timedelta(hours=23)).isoformat(), "label": "provider"}
    view = snapshot(lane(readings=[weekly]), lane("two", readings=[weekly]),
                    lane("unknown", readings=[{**weekly, "label": "admission-observed"}]),
                    lane("stale", readings=[{**weekly, "label": "stale-provider"}]))
    result = monitor.evaluate(view, now=NOW)
    assert sorted(result["alerts_sent"]) == ["codex-capacity-expiring:/homes/one", "codex-capacity-expiring:/homes/two"]
    assert not monitor.evaluate(view, now=NOW + timedelta(hours=6))["alerts_sent"]
    assert all("provider-observed unused" in notice["body"] for notice in delivered)


def test_cycle_offline_verdict_overrides_old_lane_network_error(alerts):
    """C-23.27: an explicit current-cycle online verdict overrides persisted errors from earlier probes."""
    monitor, delivered, _ = alerts
    view = snapshot(lane(verdict="auth-dead", probe_status="network-error"), offline=False)
    assert monitor.evaluate(view, now=NOW)["alerts_sent"] == ["codex-revoked:/homes/one"]
    assert delivered
