"""C-31.4: an attempt the harness ended `refused-by-kind` raises a critical alert.

The credit refused a request by its kind while a neutral check on the same key
succeeded. The lane stays open (no closure), so the alert is the operator's one
sign: the timers' view lays the latest such attempt of the last day on the
lane's row (`Timers.refused_by_kind`), and `alerts.evaluate_conditions` reads it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from subfleet import credit_lane
from subfleet.alerts import Alerts, evaluate_conditions
from subfleet.store import Store
from subfleet.timers import Timers

NOW = datetime(2026, 11, 10, 15, tzinfo=timezone.utc)
KEY = "claude-api-refused-by-kind:plan@example.org"
ALERT = {"key": credit_lane.REFUSED_BY_KIND_ALERT, "severity": "critical"}


def credit_lane_row(**extra):
    return {"lane_id": "claude-api-1", "provider": "claude-api", "account_key": "claude-api:plan@example.org",
            "home": None, "credential_ref": "claude-api-plan@example.org", "enabled": True, "owner": "v2",
            "verdict": "ok", "readings": [], "closures": [], **extra}


def test_a_refused_by_kind_attempt_raises_a_critical_alert():
    lane = credit_lane_row(credit_refused_by_kind_at="2026-11-10T14:00:00Z")
    conditions = {condition["key"]: condition for condition in evaluate_conditions({"lanes": [lane]}, now=NOW)}
    alert = conditions[KEY]
    assert alert["severity"] == "critical" and alert["home"] == "claude-api-plan@example.org"
    assert "2026-11-10T14:00:00Z" in alert["body"] and "stays open" in alert["body"]


def test_no_alert_without_a_refused_attempt():
    keys = {condition["key"] for condition in evaluate_conditions({"lanes": [credit_lane_row()]}, now=NOW)}
    assert not any(key.startswith("claude-api-refused-by-kind") for key in keys)


def test_the_alert_fires_once_then_recovers(tmp_path):
    delivered = []
    with Store(tmp_path / "store.db") as store:
        monitor = Alerts(store, {}, delivered.append)
        refused = {"lanes": [credit_lane_row(credit_refused_by_kind_at="2026-11-10T14:00:00Z")]}
        assert monitor.evaluate(refused, now=NOW)["alerts_sent"] == [KEY]
        assert monitor.evaluate(refused, now=NOW + timedelta(minutes=5))["alerts_sent"] == []
        cleared = monitor.evaluate({"lanes": [credit_lane_row()]}, now=NOW + timedelta(days=1))
        assert cleared["recovered"] == ["claude-api-plan@example.org"]
    assert [notice["key"] for notice in delivered if not notice.get("recovery")] == [KEY]


def _rows(store, attempts):
    with store.transaction("test.credit-alert") as tx:
        for lane_id, provider in (("claude-api-1", "claude-api"), ("claude-1", "claude")):
            tx.execute("INSERT INTO lanes(lane_id,provider,account_key,credential_kind,credential_ref,owner,desktop,"
                       "created_at,updated_at) VALUES (?,?,?,'keychain-token',?,'v2',0,'2026-11-01T00:00:00Z',"
                       "'2026-11-01T00:00:00Z')", (lane_id, provider, f"{provider}:x", f"ref-{lane_id}"))
        for index, (lane_id, finished_at, evidence) in enumerate(attempts):
            job = f"job-{index}"
            tx.execute("INSERT INTO jobs(job_id,request_id,kind,state,workdir,prompt_path,sandbox,payload_digest,"
                       "created_at) VALUES (?,?,'dispatch','failed','/w','/p','read-only','d','2026-11-09T00:00:00Z')",
                       (job, f"request-{job}"))
            tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at,"
                       "finished_at,evidence_json) VALUES (?,?,1,?,'claude-haiku-5-5','failed',"
                       "'2026-11-09T00:00:00Z',?,?)", (f"{job}/a1", job, lane_id, finished_at, json.dumps(evidence)))


def test_the_view_names_the_latest_refused_attempt_of_the_last_day(tmp_path):
    with Store(tmp_path / "store.db") as store:
        _rows(store, [
            ("claude-api-1", "2026-11-09T14:59:59Z", {"classification": {"alert": ALERT}}),   # over a day: out
            ("claude-api-1", "2026-11-10T09:00:00Z", {"classification": {"alert": ALERT}}),
            ("claude-api-1", "2026-11-10T12:00:00Z", {"classification": {"alert": ALERT}}),   # the latest
            ("claude-api-1", "2026-11-10T13:00:00Z", {"classification": {"end_outcome": "done"}}),
            ("claude-1", "2026-11-10T12:30:00Z", {"classification": {"alert": ALERT}}),       # not a credit lane
        ])
        timers = SimpleNamespace(store=store, now=lambda: NOW)
        assert Timers.refused_by_kind(timers) == {"claude-api-1": "2026-11-10T12:00:00Z"}
        later = SimpleNamespace(store=store, now=lambda: NOW + timedelta(days=1))
        assert Timers.refused_by_kind(later) == {}


def test_a_dead_credit_key_alert_names_the_credit_tools_not_claude_login():
    lane = credit_lane_row(verdict="auth-dead", enabled=False)
    alert = {c["key"]: c for c in evaluate_conditions({"lanes": [lane]}, now=NOW)}["claude-lane-auth:plan@example.org"]
    assert "claude-credits add plan@example.org; subfleet lanes enroll --api plan@example.org" in alert["body"]
    assert "setup-token" not in alert["body"]
