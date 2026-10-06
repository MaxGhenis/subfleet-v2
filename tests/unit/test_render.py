"""Evidence and routing presentation (C-9.1, C-11.3, C-11.5)."""

import json
from dataclasses import asdict

import pytest

from subfleet.capacity import build_view
from subfleet.contracts import Decision
from subfleet.render import attempt_usage, reading_text, status, why

NOW = "2026-09-05T10:33:00Z"


def lane(identity="codex-1", **values):
    return {"lane_id": identity, "provider": identity.split("-")[0],
            "account_key": "codex:account", "owner": "v2", "desktop": False, **values}


def reading(**values):
    return {"lane_id": "codex-1", "scope": "account", "window": "seven_day", "utilization": 0.4,
            "resets_at": "2026-09-06T00:00:00Z", "label": "provider", "source": "wham",
            "observed_at": NOW, **values}


@pytest.mark.parametrize("label", ["admission-observed", "local-backoff", "unknown"])
def test_no_percentage_without_provider_reading(label):
    """C-9.1, plan amendment 15: admission and backoff are words even with stray numbers."""
    evidence = reading(label=label, utilization=0.97)
    result = status(build_view([lane()], [evidence], now=NOW))
    assert "%" not in result
    assert label in result
    assert "source=wham" in result


def test_empty_evidence_is_unknown_without_invented_percentage():
    """C-9.1: absence of provider evidence never renders zero or unlimited capacity."""
    result = status(build_view([lane()], now=NOW))
    assert "unknown" in result
    assert "%" not in result


def test_stale_provider_percentage_is_marked_with_source_and_age():
    """C-9.1: stale numbers remain visible with their evidence label, source, and age."""
    result = status(build_view([lane()], [reading(observed_at="2026-09-05T10:30:59Z")], now=NOW))
    assert "40% used" in result
    assert "stale-provider" in result
    assert "; stale]" in result
    assert "age=121s" in result
    assert "source=wham" in result


@pytest.mark.parametrize("utilization", [None, True, float("inf"), float("nan"), -0.2])
def test_invalid_provider_number_is_never_formatted_as_percentage(utilization):
    """C-9.1, C-9.8: malformed utilization cannot recreate v1's invented capacity ratios."""
    assert "%" not in reading_text(reading(utilization=utilization))


def test_provider_over_cap_evidence_remains_visible():
    """C-9.1: real provider over-cap utilization is preserved rather than estimated or clamped."""
    view = build_view([lane()], [reading(utilization=1.47)], now=NOW)
    assert view["lanes"][0]["measured"]
    assert "147% used [provider;" in status(view)


def test_provider_percentage_and_model_closure_both_visible():
    """C-9.1, C-9.6: a window reading does not hide a model-scoped closure."""
    closure = {"lane_id": "claude-1", "scope": "claude-fable-5-1", "until_at": "2026-09-06T00:00:00Z",
               "reason": "credits", "clock_source": "reported", "source_event": "event-1"}
    result = status(build_view([lane("claude-1", owner="v1", desktop=True)],
                              [reading(lane_id="claude-1")], [closure], now=NOW))
    assert "40% used [provider;" in result
    assert "claude-fable-5-1 until 2026-09-06T00:00:00Z" in result
    assert "event=event-1" in result
    assert "reported clock" in result
    assert "desktop" in result and "v1" in result


def test_status_codex_weekly_reset_waterfall_is_primary_order():
    """C-11.3: a fuller lane resetting sooner prints first regardless of in-flight."""
    rows = [reading(lane_id="codex-1", utilization=0.4, resets_at="2026-09-11T00:00:00Z"),
            reading(lane_id="codex-2", utilization=0.6, resets_at="2026-09-06T00:00:00Z")]
    view = build_view([lane("codex-1"), lane("codex-2"), lane("codex-3")], rows,
                      attempts=[{"attempt_id": "research/a1", "job_id": "research", "lane_id": "codex-2",
                                 "model_requested": "gpt-6-astra", "state": "running"}], now=NOW)
    result = status(view)
    assert result.index("codex-2") < result.index("codex-1") < result.index("codex-3")
    assert "Codex order: weekly reset ascending" in result
    assert "Running jobs" in result
    assert "research/a1" in result and "gpt-6-astra" in result


def test_status_waiting_job_shows_capacity_reason_and_next_check():
    """C-4.1, plan amendment 11: waiting work explains its reason and next check."""
    result = status(build_view([lane()], jobs=[{"job_id": "research", "state": "waiting",
                         "wait_reason": "capacity", "next_check_at": "2026-09-05T10:34:00Z"}], now=NOW))
    assert "Waiting jobs" in result
    assert "capacity" in result and "2026-09-05T10:34:00Z" in result


def test_why_prints_recorded_promotion_and_every_rejection():
    """C-11.5, C-11.6: why exposes the stored Opus-to-Astra walk and its evidence."""
    promotion = "opus: no candidate lanes after exclusions; promoted"
    decision = Decision(("opus", "astra"), (
        {"model": "opus", "reason": "no candidate lanes after exclusions; promoted", "candidates": [],
         "rejections": [{"lane_id": "claude-1", "reason": "excluded", "reasons": ["excluded", "desktop"]},
                        {"lane_id": "claude-2", "reason": "no-slot"}]},
        {"model": "astra", "candidates": ["codex-1"], "rejections": [], "readings": [reading()],
         "candidate_details": {"codex-1": {"measured": True, "in_flight": 1,
                                 "seven_day_reset": "2026-09-06T00:00:00Z"}}},
    ), "codex-1", "astra", promotion, "abc123")
    result = why(decision)
    assert promotion in result
    assert "Walk: opus -> astra" in result
    assert "rejected claude-1: excluded, desktop" in result
    assert "rejected claude-2: no-slot" in result
    assert "Chosen: astra on codex-1" in result
    assert "Policy: abc123" in result
    assert "source=wham" in result
    assert why({"decision_json": json.dumps(asdict(decision))}) == result


def test_why_unmeasured_candidate_is_words_even_with_headroom():
    """C-9.1, C-11.3: a decision cannot turn an unmeasured candidate score into quota."""
    decision = Decision(("opus",), ({"model": "opus", "candidates": ["claude-1"],
        "candidate_details": {"claude-1": {"measured": False, "headroom": 0.99}}},),
        "claude-1", "opus", "eligible but unmeasured", "hash")
    result = why(decision)
    assert "candidate claude-1: eligible but unmeasured" in result
    assert "%" not in result


def test_why_no_lane_retains_earliest_reset_and_closure_evidence():
    """C-9.6, C-11.5, C-17.3: a failed pin explains its closure and earliest reset."""
    reset = "2026-09-06T00:00:00Z"
    decision = Decision(("opus",), ({"model": "opus", "candidates": [], "closures": [
        {"lane_id": "claude-1", "scope": "account", "until_at": reset,
         "reason": "provider-limit", "clock_source": "guessed", "source_event": "e"}]},),
        None, None, f"no candidate lanes; earliest reset {reset}", "hash")
    result = why(decision)
    assert "Chosen: no lane" in result
    assert f"earliest reset {reset}" in result
    assert "guessed clock" in result and "event=e" in result


def test_why_shows_additional_capacity_readings_without_duplicating_quota_evidence():
    """C-6.4, C-11.5: why includes evidence used only to grant lane concurrency."""
    quota = reading()
    capacity_only = reading(scope="gpt-5.6-terra", utilization=.95)
    decision = Decision(("astra",), ({"model": "astra", "candidates": ["codex-1"],
        "readings": [quota], "capacity_readings": [quota, capacity_only]},),
        "codex-1", "astra", "chosen", "hash")
    result = why(decision)
    assert result.count("account/seven_day") == 1
    assert "capacity reading codex-1: gpt-5.6-terra/seven_day 95% used [provider;" in result


def test_status_shows_quarantined_probe_without_counting_it_as_running_attempt():
    """C-5.7, C-11.4: a quarantined probe remains visible despite zero dispatch attempts."""
    view = build_view([lane(probe_state="quarantined")], now=NOW)
    assert view["in_flight"] == {"codex-1": 0}
    assert "probe=quarantined" in status(view)


def test_c26_12_status_lists_conversation_turns_under_their_own_heading():
    """C-26.12 (IR-19): a turn holds a lane slot, so it is shown, but never as a running or waiting job."""
    jobs = [{"job_id": "detached", "kind": "dispatch", "state": "running"},
            {"job_id": "turn-live", "kind": "turn", "state": "running"},
            {"job_id": "turn-waiting", "kind": "turn", "state": "waiting", "wait_reason": "capacity"},
            {"job_id": "turn-done", "kind": "turn", "state": "succeeded"}]
    attempts = [{"attempt_id": "detached/a1", "job_id": "detached", "lane_id": "codex-1",
                 "model_requested": "gpt-6-astra", "state": "running"},
                {"attempt_id": "turn-live/a1", "job_id": "turn-live", "lane_id": "codex-1",
                 "model_requested": "gpt-6-astra", "state": "running"}]
    result = status(build_view([lane()], attempts=attempts, jobs=jobs, now=NOW))
    running, turns = result.split("\nRunning jobs\n")[1].split("\nConversation turns\n")
    assert "detached/a1" in running and "turn-" not in running and "Waiting jobs" not in running
    assert "turn-live/a1" in turns and "turn-waiting" in turns and "waiting (capacity)" in turns
    assert "turn-done" not in result
    assert "Conversation turns" not in status(build_view([lane()], jobs=jobs[:1], now=NOW))


def test_c12_10_c17_8_attempt_usage_shows_provider_counters_and_requests():
    """C-12.10, C-17.8: each attempt shows normalized totals, raw fields, first and last request."""
    raw = {"usage": {"input_tokens": 10, "cache_read_input_tokens": 60,
                     "cache_creation_input_tokens": 30, "output_tokens": 12,
                     "cache_creation": {"ephemeral_1h_input_tokens": 30, "ephemeral_5m_input_tokens": 0}},
           "modelUsage": {"fixture-model": {"inputTokens": 10, "outputTokens": 12}}}
    usage = {"provider": "claude", "raw": raw,
             "normalized": {"prompt": 100, "cache_read": 60, "cache_write": 30,
                            "output": 12, "cache_ttl": "1h", "cache_hit_share": .6},
             "first_request": {"input": 4, "cache_read": 20, "cache_write": 10},
             "last_request": {"input": 6, "cache_read": 40, "cache_write": 20}}
    lines = attempt_usage(usage)
    assert lines[0] == ('usage: prompt=100; cache_read=60; cache_write=30; output=12; '
                        'cache_ttl="1h"; cache_hit_share=60%')
    assert json.loads(lines[1].removeprefix("first_request: ")) == usage["first_request"]
    assert json.loads(lines[2].removeprefix("last_request: ")) == usage["last_request"]
    assert json.loads(lines[3].removeprefix("usage raw: ")) == raw


def test_c12_10_c17_8_absent_usage_and_absent_fields_never_show_zero():
    """C-12.10, C-17.8: no stream report adds no record; an unreported field remains null."""
    assert attempt_usage(None) == []
    lines = attempt_usage({"provider": "codex", "raw": {"usage": {"output_tokens": 12}},
                           "normalized": {"output": 12}})
    assert lines[0] == ('usage: prompt=null; cache_read=null; cache_write=null; output=12; '
                        'cache_ttl=null; cache_hit_share=null')
    assert "=0" not in "\n".join(lines)


def test_c12_10_c17_8_cumulative_thread_usage_cannot_look_like_attempt_totals():
    """C-12.10, C-17.8: resumed exec totals are explicitly cumulative, with no inferred delta."""
    usage = {"provider": "codex", "cumulative_thread": True, "raw": {"usage": {"input_tokens": 100}},
             "normalized": {"prompt": 100, "cache_read": 80, "cache_write": None,
                            "output": 20, "cache_ttl": None, "cache_hit_share": .8}}
    assert attempt_usage(usage)[0].startswith("usage (cumulative thread totals): prompt=100;")
