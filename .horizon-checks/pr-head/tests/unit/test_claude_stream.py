"""C-9.8 and C-12.7: the stream-json parser, on every fixture and at its edges."""

from __future__ import annotations

import json

import pytest

from subfleet.adapters import claude_stream
from subfleet.adapters.claude_stream import parse_lines, parse_stream
from tests.conftest import FIXTURES, case_names, load_expected


@pytest.mark.parametrize("case", case_names())
def test_every_fixture_parses_to_its_recorded_shape(case):
    """C-12.7 every fixture's stream parses to the event counts `expected.json` records."""
    expected = load_expected(case)["stream"]
    summary = parse_stream((FIXTURES / case / "stdout").read_text(encoding="utf-8"))

    assert summary.has_init is expected["init"]
    assert len(summary.assistants) == expected["assistants"]
    assert list(summary.assistant_models) == expected["assistant_models"]
    assert len(summary.rate_limits) == expected["rate_limit_events"]
    assert (summary.result.subtype if summary.result else None) == expected["result_subtype"]
    assert summary.truncated_tail is expected["truncated_tail"]
    assert summary.bad_lines == expected["bad_lines"]


@pytest.mark.parametrize("case", case_names())
def test_no_fixture_stream_carries_a_secret(case):
    """C-12.7 fixtures are redacted: no token, cookie, or Authorization value."""
    for name in ("stdout", "stderr"):
        text = (FIXTURES / case / name).read_text(encoding="utf-8")
        lowered = text.lower()
        assert "sk-ant-" not in lowered
        assert "authorization" not in lowered
        assert "cookie" not in lowered
        assert "bearer " not in lowered


@pytest.mark.parametrize(
    "case,windows",
    [
        ("success-allowed", {"five_hour": (0.05, 1788624000), "seven_day": (0.25, 1789056000)}),
        ("allowed-out-of-credits-overage",
         {"five_hour": (0.29, 1788612600), "seven_day": (0.18, 1788613200)}),
        ("allowed-on-table-exhausted-lane",
         {"five_hour": (0.42, 1788612600), "seven_day": (0.33, 1788616800)}),
    ],
)
def test_experiment_zero_allowed_payloads_keep_their_fractions(case, windows):
    """C-9.8 `unifiedWindows.<window>.utilization` is a fraction and `resetsAt` epoch
    seconds; the parser changes neither."""
    summary = parse_stream((FIXTURES / case / "stdout").read_text(encoding="utf-8"))
    info = summary.rate_limit
    assert info is not None and info.allowed and not info.rejected
    assert set(info.windows) == set(windows)
    for key, (utilization, resets_at) in windows.items():
        assert info.windows[key].utilization == utilization
        assert 0.0 <= info.windows[key].utilization <= 1.0
        assert info.windows[key].resets_at == resets_at


def test_experiment_zero_rejected_payload_carries_the_credits_error_code():
    """C-9.8 a `rejected` event records `errorCode` and its own `resetsAt`."""
    summary = parse_stream(
        (FIXTURES / "rejected-credits-fable" / "stdout").read_text(encoding="utf-8")
    )
    info = summary.rate_limit
    assert info is not None
    assert info.rejected and not info.allowed
    assert info.error_code == "credits_required"
    assert info.resets_at == 1790812800
    assert info.windows == {}          # a rejection carries no window utilizations
    assert info.can_user_purchase_credits is True


def test_overage_is_parsed_and_kept_apart_from_admission():
    """C-9.8 `overageStatus` is parsed independently and is never admission evidence:
    an `overageStatus: rejected` event whose `status` is `allowed` stays allowed."""
    summary = parse_stream(
        (FIXTURES / "allowed-out-of-credits-overage" / "stdout").read_text(encoding="utf-8")
    )
    info = summary.rate_limit
    assert info is not None
    # The account has no credits left for overage and the overage lane is refused...
    assert info.overage_status == "rejected"
    assert info.overage_disabled_reason == "out_of_credits"
    assert info.is_using_overage is False
    # ...and the request itself was still admitted. Admission reads `status`, alone.
    assert info.status == "allowed" and info.allowed and not info.rejected

    axiom = parse_stream(
        (FIXTURES / "success-allowed" / "stdout").read_text(encoding="utf-8")
    ).rate_limit
    assert axiom is not None
    assert axiom.overage_status == "rejected"
    assert axiom.overage_disabled_reason == "org_level_disabled"
    assert axiom.allowed


def test_allowed_warning_counts_as_allowed():
    """C-9.8 `allowed_warning` is an admission, not a refusal."""
    line = json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "allowed_warning",
                            "unifiedWindows": {"five_hour": {"utilization": 0.92,
                                                             "resetsAt": 1788624000}}},
        "session_id": "s",
    })
    info = parse_stream(line).rate_limit
    assert info is not None and info.allowed and not info.rejected


def test_utilization_above_one_is_preserved():
    """C-9.8 `utilization` above 1 is legitimate (usage past a window's cap) and is
    never clamped: a clamp would understate how far past the cap a lane ran."""
    line = json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "allowed",
                            "unifiedWindows": {"five_hour": {"utilization": 1.47,
                                                             "resetsAt": 1788624000}}},
        "session_id": "s",
    })
    info = parse_stream(line).rate_limit
    assert info is not None and info.windows["five_hour"].utilization == 1.47


def test_truncated_last_line_is_recorded_not_raised():
    """C-9.5 a stream that stops mid-line parses, and says its tail was truncated."""
    summary = parse_stream(
        (FIXTURES / "stream-disconnect" / "stdout").read_text(encoding="utf-8")
    )
    assert summary.truncated_tail is True
    assert summary.bad_lines == 0
    assert summary.has_init
    assert len(summary.api_retries) == 2
    assert [r.error for r in summary.api_retries] == ["server_error", "overloaded"]
    assert summary.api_retries[1].error_status == 529
    assert summary.result is None


def test_interior_bad_line_is_counted_and_the_rest_still_parses():
    """C-9.5 a corrupt interior line is a bad line, not a truncated tail, and does not
    cost us the events around it."""
    text = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "s",
                    "model": "claude-opus-5"}),
        "{not json at all",
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "result": "done", "session_id": "s"}),
    ])
    summary = parse_stream(text)
    assert summary.bad_lines == 1
    assert summary.truncated_tail is False
    assert summary.has_init
    assert summary.result is not None and summary.result.text == "done"


def test_unknown_event_types_are_tolerated_and_named():
    """C-9.8 the parser survives event types it has never seen and records them, so a
    new CLI never silently drops the sensor."""
    text = "\n".join([
        json.dumps({"type": "system", "subtype": "hook_started", "session_id": "s"}),
        json.dumps({"type": "system", "subtype": "post_turn_summary", "session_id": "s"}),
        json.dumps({"type": "prompt_suggestion", "session_id": "s"}),
        json.dumps({"type": "rate_limit_event",
                    "rate_limit_info": {"status": "allowed",
                                        "unifiedWindows": {
                                            "five_hour": {"utilization": 0.1,
                                                          "resetsAt": 1788624000}}},
                    "session_id": "s"}),
    ])
    summary = parse_stream(text)
    assert summary.unknown_types == (
        "system/hook_started", "system/post_turn_summary", "prompt_suggestion",
    )
    assert summary.rate_limit is not None
    assert summary.rate_limit.windows["five_hour"].utilization == 0.1


def test_the_observed_event_sequence_of_experiment_zero_parses():
    """C-9.8 the real sequence experiment-0 recorded — init, two hook pairs, two
    assistant frames, the rate-limit event, a post-turn summary, and the result —
    yields one summary with the sensor intact."""
    sid = "281408bb-280e-43da-9455-b9f3ddebf275"
    rows = [
        {"type": "system", "subtype": "init", "session_id": sid, "model": "claude-haiku-4-5-20251001"},
        {"type": "system", "subtype": "hook_started", "session_id": sid},
        {"type": "system", "subtype": "hook_started", "session_id": sid},
        {"type": "system", "subtype": "hook_response", "session_id": sid},
        {"type": "system", "subtype": "hook_response", "session_id": sid},
        {"type": "assistant", "session_id": sid,
         "message": {"model": "claude-haiku-4-5-20251001",
                     "content": [{"type": "text", "text": "o"}]}},
        {"type": "assistant", "session_id": sid,
         "message": {"model": "claude-haiku-4-5-20251001",
                     "content": [{"type": "text", "text": "k"}]}},
        {"type": "rate_limit_event", "session_id": sid,
         "rate_limit_info": {"status": "allowed", "resetsAt": 1788624000,
                             "rateLimitType": "five_hour",
                             "unifiedWindows": {
                                 "five_hour": {"utilization": 0.05, "resetsAt": 1788624000},
                                 "seven_day": {"utilization": 0.25, "resetsAt": 1789056000}}}},
        {"type": "system", "subtype": "post_turn_summary", "session_id": sid},
        {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
         "session_id": sid},
    ]
    summary = parse_lines(json.dumps(r) for r in rows)
    assert summary.session_id == sid
    assert len(summary.assistants) == 2
    assert summary.final_text == "ok"
    assert summary.rate_limit is not None
    assert set(summary.rate_limit.windows) == {"five_hour", "seven_day"}
    assert summary.bad_lines == 0 and summary.truncated_tail is False


def test_the_last_rate_limit_event_wins():
    """C-9.8 the CLI re-emits the event whenever the numbers move; the state at exit
    is the last one."""
    text = "\n".join(
        json.dumps({"type": "rate_limit_event", "session_id": "s",
                    "rate_limit_info": {"status": "allowed",
                                        "unifiedWindows": {
                                            "five_hour": {"utilization": u,
                                                          "resetsAt": 1788624000}}}})
        for u in (0.10, 0.20, 0.31)
    )
    summary = parse_stream(text)
    assert len(summary.rate_limits) == 3
    assert summary.rate_limit.windows["five_hour"].utilization == 0.31


def test_a_single_json_object_is_accepted():
    """C-12.6 an attempt captured with `--output-format json` (v1's shape) still
    parses, so a v1 artifact is readable by the v2 classifier."""
    summary = parse_stream(json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": "hello",
         "session_id": "s"},
        indent=1,
    ))
    assert summary.result is not None and summary.result.text == "hello"


def test_empty_and_whitespace_input_parse_to_nothing():
    """C-9.5 an attempt that produced no stream at all is empty, not an exception."""
    for text in ("", "\n", "   \n\n"):
        summary = parse_stream(text)
        assert summary.init is None
        assert summary.rate_limit is None
        assert summary.result is None
        assert summary.lines_total == 0


def test_non_object_rows_are_counted_as_bad_lines():
    """C-9.5 a JSON array or scalar on a line is not an event."""
    summary = parse_stream('[1,2,3]\n"a string"\n42\n')
    assert summary.bad_lines == 3
    assert summary.init is None


def test_result_error_shape_carries_errors_not_result_text():
    """C-9.2 the `error_during_execution` result shape has an `errors` array and no
    `result` string; both are surfaced to the classifier's text corpus."""
    summary = parse_stream(json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "errors": ["API Error: 529 Overloaded"], "session_id": "s",
    }))
    assert summary.result is not None
    assert summary.result.text == ""
    assert summary.result.errors == ("API Error: 529 Overloaded",)
    assert "API Error: 529 Overloaded" in summary.texts()


def test_assistant_text_extraction_ignores_non_text_blocks():
    """C-12.6 a deliverable is the message's text blocks, exactly; tool_use and
    thinking blocks contribute nothing."""
    summary = parse_stream(json.dumps({
        "type": "assistant", "session_id": "s",
        "message": {"model": "claude-opus-5", "content": [
            {"type": "thinking", "thinking": "hidden"},
            {"type": "text", "text": "first "},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
            {"type": "text", "text": "second"},
        ]},
    }))
    assert summary.assistants[0].text == "first second"


def test_error_kinds_are_surfaced_from_assistant_and_retry_frames():
    """C-9.3, C-9.5 the CLI's closed `error` enum reaches the classifier from both
    the frames that carry it."""
    text = "\n".join([
        json.dumps({"type": "assistant", "session_id": "s", "error": "oauth_org_not_allowed",
                    "message": {"model": "claude-opus-5", "content": []}}),
        json.dumps({"type": "system", "subtype": "api_retry", "session_id": "s",
                    "attempt": 1, "max_retries": 3, "retry_delay_ms": 500,
                    "error_status": 500, "error": "server_error"}),
    ])
    summary = parse_stream(text)
    assert summary.error_kinds == ("oauth_org_not_allowed", "server_error")
    assert "oauth_org_not_allowed" in claude_stream.AUTH_ERROR_KINDS
    assert "server_error" in claude_stream.TRANSIENT_ERROR_KINDS


def test_final_text_falls_back_to_the_last_assistant_message():
    """C-12.6 with no result event, the last assistant text is the stream's rendering."""
    text = "\n".join([
        json.dumps({"type": "assistant", "session_id": "s",
                    "message": {"model": "m", "content": [{"type": "text", "text": "one"}]}}),
        json.dumps({"type": "assistant", "session_id": "s",
                    "message": {"model": "m", "content": [{"type": "text", "text": "two"}]}}),
    ])
    assert parse_stream(text).final_text == "two"


def test_the_parser_touches_nothing_outside_its_input():
    """C-12.1 the stream parser is pure: no filesystem, no subprocess, no network."""
    source = (
        __import__("pathlib").Path(claude_stream.__file__).read_text(encoding="utf-8")
    )
    for forbidden in ("subprocess", "urllib", "socket", "os.environ", "open("):
        assert forbidden not in source, f"claude_stream.py must not use {forbidden}"
