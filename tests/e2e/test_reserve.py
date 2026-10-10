"""C-9.9 and C-11.7 end to end: the usage sensor feeds the reserve rule in a real daemon."""

from __future__ import annotations

import json

import pytest

FABLE = "claude-fable-5-1"


def usage_rows(e2e):
    return e2e.rows("SELECT lane_id, scope, utilization FROM readings WHERE source='oauth-usage' "
                    "AND window='seven_day' ORDER BY lane_id, scope")


def test_c11_7_opus_lands_on_the_lane_with_slack_and_the_reserved_lane_is_named(e2e):
    """claude-1: shared 94, Fable 49 (the incident's account); claude-2: shared 60, Fable 97."""
    e2e.enable_reserve()
    e2e.start(scenario="success-allowed", env={"SUBFLEET_FAKE_USAGE": "1=94/49,2=60/97"})
    e2e.until(lambda: {(r["lane_id"], r["scope"]) for r in usage_rows(e2e)} >= {
        ("claude-1", FABLE), ("claude-2", FABLE)}, timeout=30)
    result = e2e.cli(*e2e.run_args("opus", "--wait"))
    assert result.rc == 0, result
    job_id = result.stdout.strip().splitlines()[0].strip()
    attempt, = e2e.attempts(job_id)
    assert attempt["lane_id"] == "claude-2" and attempt["state"] == "succeeded"
    decision = json.loads(e2e.rows("SELECT decision_json FROM decisions WHERE job_id=? ORDER BY decision_id",
                                   (job_id,))[-1]["decision_json"])
    opus = next(e for e in decision["evaluations"] if e["model"] == "opus")
    reserved = next(r for r in opus["rejections"] if r["lane_id"] == "claude-1")
    assert "reserve:fable:reserved" in reserved["reasons"]
    assert reserved["reserve"]["slack"] < 0 and reserved["reserve"]["reserved_remaining"] == pytest.approx(.51)
    assert opus["candidate_details"]["claude-2"]["reserve"]["state"] == "slack"


def test_c11_7_no_slack_anywhere_holds_non_fable_work_and_says_why(e2e):
    e2e.enable_reserve()
    e2e.start(scenario="success-allowed", env={"SUBFLEET_FAKE_USAGE": "1=94/49,2=90/40"})
    e2e.until(lambda: {(r["lane_id"], r["scope"]) for r in usage_rows(e2e)} >= {
        ("claude-1", "account"), ("claude-1", FABLE),
        ("claude-2", "account"), ("claude-2", FABLE)}, timeout=30)
    result = e2e.cli(*e2e.run_args("opus", "--dry-run", "--why"))
    assert result.rc == 0, result
    decision = json.loads(result.stdout)
    opus = next(e for e in decision["evaluations"] if e["model"] == "opus")
    assert opus["candidates"] == []
    assert all("reserve:fable:reserved" in r["reasons"] for r in opus["rejections"])


def test_c11_7_setup_token_lanes_stay_unmeasured_for_opus_while_fable_still_runs(e2e):
    """Every enrolled lane answered 403 on 2026-09-06: no usage scope, so no slack can be shown."""
    e2e.enable_reserve()
    e2e.start(scenario="success-allowed")  # SUBFLEET_FAKE_USAGE unset: every bearer gets 403
    e2e.until(lambda: any(json.loads(row["data_json"]).get("probe_status") == "no-scope"
                          for row in e2e.rows("SELECT data_json FROM events WHERE kind='timer.verdict'")), timeout=30)
    assert usage_rows(e2e) == []
    held = e2e.cli(*e2e.run_args("opus", "--dry-run", "--why"))
    decision = json.loads(held.stdout)
    opus = next(e for e in decision["evaluations"] if e["model"] == "opus")
    assert opus["candidates"] == [] and all("reserve:fable:unmeasured" in r["reasons"] for r in opus["rejections"])
    fable = e2e.cli(*e2e.run_args("fable", "--wait"))
    assert fable.rc == 0, fable
    attempt, = e2e.attempts(fable.stdout.strip().splitlines()[0].strip())
    assert attempt["state"] == "succeeded"


def test_c9_9_a_rate_limited_lane_is_left_alone_until_retry_after(e2e):
    e2e.enable_reserve()
    e2e.start(scenario="success-allowed", env={"SUBFLEET_FAKE_USAGE": "1=429,2=60/97"})
    # claude-2's readings can commit before claude-1's verdict in the same cycle.
    verdicts = e2e.until(lambda: e2e.rows(
        "SELECT data_json FROM events WHERE kind='timer.verdict' AND lane_id='claude-1' "
        "ORDER BY event_id"), timeout=30)
    verdict = json.loads(verdicts[-1]["data_json"])
    assert verdict["probe_status"] == "rate-limited" and verdict["retry_after_s"] == 3035
    assert not e2e.rows("SELECT 1 FROM closures WHERE lane_id='claude-1'")
    assert not e2e.rows("SELECT 1 FROM readings WHERE lane_id='claude-1' AND source='oauth-usage'")
