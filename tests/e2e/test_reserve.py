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
    e2e.until(lambda: len(usage_rows(e2e)) >= 4, timeout=30)
    result = e2e.cli(*e2e.run_args("opus", "--dry-run", "--why"))
    assert result.rc == 0, result
    decision = json.loads(result.stdout)
    opus = next(e for e in decision["evaluations"] if e["model"] == "opus")
    assert opus["candidates"] == []
    assert all("reserve:fable:reserved" in r["reasons"] for r in opus["rejections"])


def test_c11_7_setup_token_lanes_stay_unmeasured_and_a_fable_pin_cannot_escape(e2e):
    """Every enrolled lane answered 403 on 2026-09-06: no usage scope, so no slack can be shown.

    Until Fable's retirement (2026-09-27) the reserved model itself still ran here. A
    policy that still lists Fable no longer lets `-m fable` reach it: the CLI dispatches
    the successor, which the reserve holds like any other Opus job (C-17.2)."""
    e2e.enable_reserve()
    e2e.start(scenario="success-allowed")  # SUBFLEET_FAKE_USAGE unset: every bearer gets 403
    e2e.until(lambda: any(json.loads(row["data_json"]).get("probe_status") == "no-scope"
                          for row in e2e.rows("SELECT data_json FROM events WHERE kind='timer.verdict'")), timeout=30)
    assert usage_rows(e2e) == []
    held = e2e.cli(*e2e.run_args("opus", "--dry-run", "--why"))
    decision = json.loads(held.stdout)
    opus = next(e for e in decision["evaluations"] if e["model"] == "opus")
    assert opus["candidates"] == [] and all("reserve:fable:unmeasured" in r["reasons"] for r in opus["rejections"])
    retired = e2e.cli(*e2e.run_args("fable", "--dry-run", "--why"))
    assert "-m fable is retired; using opus" in retired.stderr, retired
    decision = json.loads(retired.stdout)
    assert [evaluation["model"] for evaluation in decision["evaluations"]] == ["opus"]
    assert decision["evaluations"][0]["candidates"] == []
    assert not e2e.rows("SELECT 1 FROM attempts WHERE model_requested=?", (FABLE,))


def test_c9_9_a_rate_limited_lane_is_left_alone_until_retry_after(e2e):
    e2e.enable_reserve()
    e2e.start(scenario="success-allowed", env={"SUBFLEET_FAKE_USAGE": "1=429,2=60/97"})
    e2e.until(lambda: len(usage_rows(e2e)) >= 2, timeout=30)
    verdicts = [json.loads(row["data_json"]) for row in e2e.rows(
        "SELECT data_json FROM events WHERE kind='timer.verdict' AND lane_id='claude-1'")]
    assert verdicts and verdicts[-1]["probe_status"] == "rate-limited" and verdicts[-1]["retry_after_s"] == 3035
    assert not e2e.rows("SELECT 1 FROM closures WHERE lane_id='claude-1'")
    assert not e2e.rows("SELECT 1 FROM readings WHERE lane_id='claude-1' AND source='oauth-usage'")
