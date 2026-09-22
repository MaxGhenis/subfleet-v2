"""Provider 529 placeholders are error evidence, not a served-model downgrade."""

from copy import deepcopy
import json

import pytest

from subfleet.adapters.claude_stream import is_synthetic_api_error
from subfleet.contracts import Attestation, OutcomeClass
from tests.conftest import exit_info, make_launch
from tests.unit.test_claude_attest import OPUS, FABLE, _adapter, _assistant_row, _write_transcript

SESSION = "synthetic-error-session"
ERROR = "API Error: 529 Overloaded. This is a server-side issue, usually temporary."


def synthetic_error(marker="is_api_error_message"):
    """Sanitized shape observed in the 2026-09-21 NZ attempt (Claude Code)."""
    return {
        "type": "assistant", "session_id": SESSION, "sessionId": SESSION,
        "error": "server_error", marker: True, "parent_tool_use_id": None,
        "message": {
            "id": "fixture-error-uuid", "type": "message", "role": "assistant",
            "model": "<synthetic>", "content": [{"type": "text", "text": ERROR}],
            "stop_reason": "stop_sequence", "stop_sequence": "", "container": None,
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                      "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
                      "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}},
        },
    }


def exercise(tmp_path, models, errors):
    projects, attempt = tmp_path / "projects", tmp_path / "a1"
    attempt.mkdir()
    real = [_assistant_row(SESSION, model, "Actual model response", i) for i, model in enumerate(models)]
    rows = real + errors
    _write_transcript(projects, SESSION, rows)
    # Even a terminal result claiming Opus exclusively must not erase unknown
    # or mixed real assistant-model evidence earlier in the stream/transcript.
    stream = [{"type": "system", "subtype": "init", "session_id": SESSION, "model": OPUS},
              *rows, {"type": "result", "subtype": "success", "is_error": True,
                      "session_id": SESSION, "result": ERROR,
                      "modelUsage": {OPUS: {"inputTokens": 926, "outputTokens": 1000}}}]
    (attempt / "stream.jsonl").write_text("".join(json.dumps(row) + "\n" for row in stream))
    (attempt / "stderr").write_text("")
    launch = make_launch(attempt, session_id=SESSION, model_id=OPUS, projects_dir=projects,
                         identity=None, label=None)
    adapter = _adapter(projects)
    summary = adapter.stream_summary(attempt, launch)
    outcome = adapter.classify(attempt, launch, exit_info(1))
    attestation = adapter.attest(attempt, launch, outcome, OPUS)
    return summary, outcome, attestation


@pytest.mark.parametrize("marker", ["is_api_error_message", "isApiErrorMessage"])
def test_real_opus_work_then_529_placeholder_remains_transient_and_opus_attested(tmp_path, marker):
    frame = synthetic_error(marker)
    summary, outcome, attestation = exercise(tmp_path, [OPUS] * 183, [frame, deepcopy(frame)])
    assert len(summary.assistants) == 185  # no frame or failure evidence is discarded
    assert summary.assistant_models == (OPUS,) * 183
    assert summary.error_kinds == ("server_error", "server_error")
    assert summary.texts().count(ERROR) == 3  # two assistant errors and final result
    assert outcome.cls is OutcomeClass.TRANSIENT and outcome.served_model == OPUS
    assert "server_error" in outcome.detail and "529" in outcome.detail
    assert attestation.status is Attestation.ATTESTED and attestation.served_model == OPUS
    assert "183 assistant turn(s)" in attestation.evidence
    assert "ignored 2 provider synthetic API-error frame(s)" in attestation.evidence


def test_only_provider_error_placeholders_cannot_attest_any_model(tmp_path):
    summary, outcome, attestation = exercise(tmp_path, [], [synthetic_error()])
    assert summary.assistant_models == () and summary.error_kinds == ("server_error",)
    assert outcome.cls is OutcomeClass.TRANSIENT and outcome.served_model is None
    assert attestation.status is Attestation.UNATTESTED and attestation.served_model is None


@pytest.mark.parametrize("model", [FABLE, "unknown-real-model", "<unrecognized>"])
def test_synthetic_error_does_not_hide_another_real_or_unknown_model(tmp_path, model):
    _, outcome, attestation = exercise(tmp_path, [OPUS, model, OPUS], [synthetic_error()])
    assert outcome.cls is OutcomeClass.TRANSIENT and outcome.served_model is None
    assert attestation.status is Attestation.MISMATCH and attestation.served_model == model


@pytest.mark.parametrize("model", [None, "", 123])
def test_missing_model_among_real_turns_stays_unattested(tmp_path, model):
    _, outcome, attestation = exercise(tmp_path, [OPUS, model], [synthetic_error()])
    assert outcome.cls is OutcomeClass.TRANSIENT and outcome.served_model is None
    assert attestation.status is Attestation.UNATTESTED and attestation.served_model is None
    assert "without a model field" in attestation.evidence


@pytest.mark.parametrize("mutation", [
    "no-marker", "truthy-marker", "unknown-error", "wrong-model", "no-usage", "missing-token",
    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    "boolean-token", "string-token", "tool-use", "wrong-role", "wrong-message-type",
])
def test_unrecognized_synthetic_shape_remains_mismatching_evidence(tmp_path, mutation):
    frame = synthetic_error()
    message = frame["message"]
    if mutation == "no-marker":
        del frame["is_api_error_message"]
    elif mutation == "truthy-marker":
        frame["is_api_error_message"] = "true"
    elif mutation == "unknown-error":
        frame["error"] = "unrecognized-future-error"
    elif mutation == "wrong-model":
        message["model"] = "<unknown>"
    elif mutation == "no-usage":
        del message["usage"]
    elif mutation == "missing-token":
        del message["usage"]["input_tokens"]
    elif mutation in message["usage"]:
        message["usage"][mutation] = 1
    elif mutation == "boolean-token":
        message["usage"]["input_tokens"] = False
    elif mutation == "string-token":
        message["usage"]["input_tokens"] = "0"
    elif mutation == "tool-use":
        message["content"].append({"type": "tool_use", "name": "Bash", "input": {"command": "true"}})
    elif mutation == "wrong-role":
        message["role"] = "user"
    elif mutation == "wrong-message-type":
        message["type"] = "unknown"
    assert not is_synthetic_api_error(frame)
    summary, outcome, attestation = exercise(tmp_path, [OPUS], [frame])
    assert message["model"] in summary.assistant_models
    assert outcome.served_model is None
    assert attestation.status is Attestation.MISMATCH
    assert attestation.served_model == message["model"]


def test_api_error_marker_on_real_model_does_not_exempt_it(tmp_path):
    frame = synthetic_error()
    frame["message"]["model"] = FABLE
    _, _, attestation = exercise(tmp_path, [OPUS], [frame])
    assert attestation.status is Attestation.MISMATCH and attestation.served_model == FABLE


@pytest.mark.parametrize("field,counter", [
    ("cache_creation", "ephemeral_1h_input_tokens"), ("cache_creation", "ephemeral_5m_input_tokens"),
    ("server_tool_use", "web_search_requests"), ("server_tool_use", "web_fetch_requests"),
    ("output_tokens_details", "reasoning_tokens"), ("input_tokens_details", "cache_tokens"),
])
@pytest.mark.parametrize("value", [1, -1, "0", False, None])
def test_nonzero_or_malformed_nested_work_counters_are_not_exempt(tmp_path, field, counter, value):
    frame = synthetic_error()
    frame["message"]["usage"].setdefault(field, {})[counter] = value
    assert not is_synthetic_api_error(frame)
    _, _, attestation = exercise(tmp_path, [OPUS], [frame])
    assert attestation.status is Attestation.MISMATCH and attestation.served_model == "<synthetic>"


@pytest.mark.parametrize("details", [None, {}, {"reasoning_tokens": 0}, {"nested": {"tokens": 0}}])
def test_optional_empty_or_zero_details_do_not_invent_work(details):
    frame = synthetic_error()
    frame["message"]["usage"]["output_tokens_details"] = details
    assert is_synthetic_api_error(frame)


@pytest.mark.parametrize("details", [[], "0", 0, {"nested": {"tokens": 1}}])
def test_optional_details_must_be_objects_containing_only_zero_counters(details):
    frame = synthetic_error()
    frame["message"]["usage"]["output_tokens_details"] = details
    assert not is_synthetic_api_error(frame)
