"""C-18.5: provider usage semantics and refusal invariants on retained lines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet.adapters.claude_stream import parse_lines, parse_stream
from subfleet.usage import parse_usage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usage"


def fixture(name, provider, **kw):
    with (FIXTURES / name).open() as stream:
        return parse_usage(stream, provider, **kw)


def lines(*events):
    return [json.dumps(event) for event in events]


def claude(input=2, read=10, write=3, output=5, **extra):
    return {"type": "result", "usage": {"input_tokens": input, "cache_read_input_tokens": read,
                                         "cache_creation_input_tokens": write, "output_tokens": output, **extra}}


def codex(input=20, read=10, write=0, output=5):
    return {"type": "turn.completed", "usage": {"input_tokens": input, "cached_input_tokens": read,
                                                 "cache_write_input_tokens": write, "output_tokens": output}}


def app(input=20, read=10, write=0, output=5, *, turn="turn", last=None):
    usage = {"inputTokens": input, "cachedInputTokens": read, "cacheWriteInputTokens": write, "outputTokens": output}
    return {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread", "turnId": turn,
                                                                "tokenUsage": {"total": usage, "last": last or usage}}}


def assistant(mid, **usage):
    return {"type": "assistant", "parent_tool_use_id": None, "message": {"id": mid, "usage": usage}}


def test_c_12_10_claude_live_results_are_disjoint_model_usage_is_cumulative():
    """C-18.5: claude live results are disjoint model usage is cumulative."""
    usage = fixture("claude-multi-result.jsonl", "claude")
    normalized = usage["normalized"]
    assert normalized["prompt"] == 398 + 785346 + 99405827
    assert normalized["cache_read"] == 99405827
    assert normalized["cache_write"] == 785346
    assert normalized["output"] == 329370
    assert normalized["cache_ttl"] == "1h"
    assert usage["raw"]["modelUsage"]["claude-opus-5-5"]["outputTokens"] == 657781
    assert len(usage["raw"]["results"]) == 2
    assert usage["first_request"] == {"input": 2, "cache_read": 0, "cache_write": 188891}
    assert usage["last_request"] == {"input": 4, "cache_read": 784128, "cache_write": 1218}
    text = (FIXTURES / "claude-multi-result.jsonl").read_text()
    assert parse_stream(text).usage == usage == parse_lines(iter(text.splitlines())).usage


def test_c_12_10_codex_live_resume_preserves_thread_scope_and_subset_input():
    """C-18.5: codex live resume preserves thread scope and subset input."""
    original = fixture("codex-original.jsonl", "codex")
    resumed = fixture("codex-resumed.jsonl", "codex", resumed=True)
    assert original["normalized"]["prompt"] == 5979585
    assert resumed["normalized"]["prompt"] == 9898307
    assert resumed["normalized"]["cache_read"] == 9230080
    assert resumed["raw"]["usage"]["reasoning_output_tokens"] == 22203
    assert resumed["cumulative_thread"] is True
    assert original["cumulative_thread"] is False


def test_c_12_10_app_server_live_total_and_last_own_turn_only():
    """C-18.5: app server live total and last own turn only."""
    usage = fixture("codex-app-turn.jsonl", "codex", turn_id="turn")
    assert usage["normalized"]["prompt"] == 14734
    assert usage["normalized"]["cache_read"] == 12160
    assert usage["raw"]["total"] == usage["raw"]["last"]
    assert usage["cumulative_thread"] is False
    assert fixture("codex-app-turn.jsonl", "codex", turn_id="other") is None


def test_c_12_10_app_snapshots_replace_and_restored_thread_totals_are_labelled():
    """C-18.5: app snapshots replace and restored thread totals are labelled."""
    events = lines(app(input=20, output=5), app(input=45, output=9, last={"inputTokens": 25, "outputTokens": 4}),
                   app(input=999, turn="other"))
    usage = parse_usage(events, "codex", turn_id="turn")
    assert usage["normalized"]["prompt"] == 45
    assert usage["normalized"]["output"] == 9
    assert usage["cumulative_thread"] is False
    resumed = parse_usage(lines(app(input=45, output=9, last={"inputTokens": 25, "outputTokens": 4})),
                          "codex", turn_id="turn")
    assert resumed["cumulative_thread"] is True
    assert resumed["normalized"]["prompt"] == 45
    baseline = parse_usage(lines(app(input=20, turn="earlier"), app(input=45)), "codex", turn_id="turn")
    assert baseline["cumulative_thread"] is True


def test_c_12_10_app_turn_id_inferred_only_from_own_acknowledgement():
    """C-18.5: app turn id inferred only from own acknowledgement."""
    started = {"method": "turn/started", "params": {"turn": {"id": "turn"}}}
    assert parse_usage(lines(started, app()), "codex") == parse_usage(lines(app()), "codex", turn_id="turn")
    assert parse_usage(lines(app()), "codex") is None


def test_c_12_10_app_historical_baseline_is_scoped_to_the_own_thread():
    """C-18.5: foreign thread notifications cannot turn own usage into historical totals."""
    foreign = app(input=100, turn="foreign-turn")
    foreign["params"]["threadId"] = "foreign-thread"
    own = app(input=20)
    usage = parse_usage(lines(foreign, own), "codex", turn_id="turn")
    assert usage["cumulative_thread"] is False
    assert usage["normalized"]["prompt"] == 20
    # Absent ids cannot disprove historical scope, so no thread id is inferred.
    del foreign["params"]["threadId"]
    assert parse_usage(lines(foreign, own), "codex", turn_id="turn")["cumulative_thread"] is True
    del own["params"]["threadId"]
    assert parse_usage(lines(foreign, own), "codex", turn_id="turn")["cumulative_thread"] is True


def test_c_12_10_first_last_main_requests_deduplicate_and_ignore_children():
    """C-18.5: first last main requests deduplicate and ignore children."""
    first = assistant("first", input_tokens=2, cache_creation_input_tokens=3, cache_read_input_tokens=10)
    last = assistant("last", input_tokens=4, cache_creation_input_tokens=0, cache_read_input_tokens=15)
    child = {**assistant("child", input_tokens=999), "parent_tool_use_id": "tool"}
    usage = parse_usage(lines(first, first, child, last, last, claude()), "claude")
    assert usage["first_request"] == {"input": 2, "cache_read": 10, "cache_write": 3}
    assert usage["last_request"] == {"input": 4, "cache_read": 15, "cache_write": 0}
    pending = assistant("first")
    reordered = parse_usage(lines(pending, last, first, claude()), "claude")
    assert reordered["first_request"] == usage["first_request"]
    assert reordered["last_request"] == usage["last_request"]
    assert parse_usage(lines(pending), "claude") is None


@pytest.mark.parametrize("split,ttl", [((5, 0), "1h"), ((0, 5), "5m"), ((2, 3), "mixed"), ((0, 0), None), ((5, None), None)])
def test_c_12_10_ttl_is_reported_split_only(split, ttl):
    """C-18.5: ttl is reported split only."""
    creation = dict(zip(("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"), split))
    assert parse_usage(lines(claude(cache_creation=creation)), "claude")["normalized"]["cache_ttl"] == ttl


def test_c_12_10_missing_fields_stay_null_and_request_totals_are_not_estimated():
    """C-18.5: missing fields stay null and request totals are not estimated."""
    usage = parse_usage(lines({"type": "result", "usage": {"output_tokens": 5}}), "claude")
    assert usage["normalized"] == {"prompt": None, "cache_read": None, "cache_write": None,
                                    "output": 5, "cache_ttl": None, "cache_hit_share": None}
    request = parse_usage(lines(assistant("request", input_tokens=4)), "claude")
    assert request["normalized"]["prompt"] is None
    assert request["first_request"] == {"input": 4, "cache_read": None, "cache_write": None}
    for event in ({"type": "result", "usage": {"cache_creation": {"ephemeral_1h_input_tokens": 8}}},
                  {"type": "result", "modelUsage": {"model": {"inputTokens": 8}}}):
        record = parse_usage(lines(event), "claude")
        assert record is not None and record["normalized"]["prompt"] is None
    for event in (assistant("request", output_tokens=5),
                  assistant("request", cache_creation={"ephemeral_1h_input_tokens": 8})):
        record = parse_usage(lines(event), "claude")
        assert record is not None
        assert record["normalized"]["prompt"] is record["normalized"]["output"] is None
        assert "request" in record["raw"]["requests"]
        assert record["first_request"] == {"input": None, "cache_read": None, "cache_write": None}
    incomplete_segment = parse_usage(lines({"type": "result"}, claude()), "claude")
    assert incomplete_segment["normalized"]["prompt"] is None
    assert incomplete_segment["normalized"]["output"] is None
    assert len(incomplete_segment["raw"]["results"]) == 2
    assert parse_usage(lines({"type": "result"}), "claude") is None


@pytest.mark.parametrize("models", [{}, {"model": {}}, {"model": None}, {"model": {"outputTokens": None}},
                                     {"model": {"costUSD": 1, "contextWindow": 1000000}}])
def test_c_12_10_model_maps_without_token_counters_do_not_claim_usage(models):
    """C-18.5: empty, null-only and cost/capacity-only model maps report no measured tokens."""
    assert parse_usage(lines({"type": "result", "modelUsage": models}), "claude") is None
    observed = {**claude(), "modelUsage": models}
    assert parse_usage(lines(observed), "claude")["raw"]["modelUsage"] == models


@pytest.mark.parametrize("provider,event", [("codex", codex(input=5, read=6)), ("codex", codex(input=5, write=6)),
                                            ("codex", app(input=5, read=6)), ("claude", claude(input=-1)),
                                            ("claude", claude(cache_creation=[])), ("codex", codex(input=True))])
def test_c_12_10_invalid_usage_is_refused_never_clamped(provider, event):
    """C-18.5: invalid usage is refused never clamped."""
    assert parse_usage(lines(event), provider, turn_id="turn") is None


def test_c_12_10_duplicate_conflicts_and_invalid_earlier_snapshots_refuse_record():
    """C-18.5: duplicate conflicts and invalid earlier snapshots refuse record."""
    assert parse_usage(lines(assistant("same", input_tokens=4), assistant("same", input_tokens=5), claude()), "claude") is None
    assert parse_usage(lines(app(input=5, read=6), app()), "codex", turn_id="turn") is None
    repeated = {**claude(), "uuid": "result"}
    assert parse_usage(lines(repeated, repeated), "claude")["normalized"]["output"] == 5


def test_c_12_10_provider_adapters_attach_usage_to_outcome(adapter, tmp_path):
    """C-18.5: provider adapters attach usage to outcome."""
    from subfleet.adapters.codex import CodexAdapter
    from tests.conftest import exit_info, make_launch

    directory = tmp_path / "attempt"
    directory.mkdir()
    (directory / "stdout").write_text("\n".join(lines(claude())) + "\n")
    launch = make_launch(directory, session_id="session", model_id="opus", projects_dir=tmp_path / "projects")
    assert adapter.classify(directory, launch, exit_info(1)).usage == parse_usage(lines(claude()), "claude")
    (directory / "stdout").write_text("\n".join(lines(codex())) + "\n")
    assert CodexAdapter().classify(directory, launch, exit_info(1)).usage == parse_usage(lines(codex()), "codex")


def test_c_12_10_turn_drivers_keep_usage_after_completion_and_replay():
    """C-18.5: turn drivers keep usage after completion and replay."""
    from subfleet.conversations.claude_turn import ClaudeTurn
    from subfleet.conversations.codex_turn import CodexTurn
    from subfleet.conversations.turn import TurnSpec

    spec = TurnSpec(provider="claude", message_id="message", text="hello", model_id="opus", permission="ask", native_session_id=None)
    claude_driver = ClaudeTurn(spec, read_bytes=lambda image: b"")
    claude_driver.feed(json.dumps(claude()), 0)
    claude_driver.eof(1)
    assert claude_driver.outcome.usage == parse_usage(lines(claude()), "claude")
    trailing = claude(output=8)
    claude_driver.feed(json.dumps(trailing), 2)
    assert claude_driver.outcome.usage["normalized"]["output"] == 13

    codex_spec = TurnSpec(provider="codex", message_id="message", text="hello", model_id="gpt-6-astra", permission="ask", native_session_id=None)
    codex_driver = CodexTurn(codex_spec)
    codex_driver.turn_id = "turn"
    codex_driver.feed(json.dumps(app()), 0)
    codex_driver.eof(1)
    assert codex_driver.outcome.usage == parse_usage(lines(app()), "codex", turn_id="turn")
    codex_driver.feed(json.dumps(app(input=30)), 2)
    assert codex_driver.outcome.usage["normalized"]["prompt"] == 30


def test_c_12_10_turn_classifier_reparses_trailing_stream_instead_of_stale_record(tmp_path):
    """C-18.5: turn classifier reparses trailing stream instead of stale record."""
    from subfleet.conversations.classify import TurnAdapter
    from tests.conftest import exit_info, make_launch

    (tmp_path / "turn.json").write_text(json.dumps({"state": "complete", "turn_id": "turn", "usage": {"stale": True}}))
    (tmp_path / "stdout").write_text("\n".join(lines(app(input=20), app(input=30))) + "\n")
    launch = make_launch(tmp_path, session_id="session", model_id="gpt-6-astra", projects_dir=tmp_path / "projects")
    assert TurnAdapter("codex").classify(tmp_path, launch, exit_info(0)).usage["normalized"]["prompt"] == 30


def test_c_12_10_runner_omits_unreported_usage_and_adds_measurement_on_replay(tmp_path):
    """C-18.5: replay adds observed counters while preserving the recorded turn fate."""
    from contextlib import nullcontext
    from dataclasses import replace
    from types import SimpleNamespace
    from subfleet.conversations.runner import TurnRunner
    from subfleet.conversations.turn import Outcome, TurnSpec

    spec = TurnSpec(provider="claude", message_id="message", text="hello", model_id="opus", permission="ask", native_session_id=None)
    runner = SimpleNamespace(recorded=None, driver=SimpleNamespace(outcome=Outcome("complete")), spec=spec,
                             adir=tmp_path, store=SimpleNamespace(writing=nullcontext), served={}, stop_reason=None,
                             final_text="hello", relay_failed=False, sent={}, frame_refused=False, relay_version=1,
                             steer_facts=lambda: {})
    TurnRunner._write_outcome(runner)
    recorded = json.loads((tmp_path / "turn.json").read_text())
    assert "usage" not in recorded
    runner.recorded = recorded
    runner.driver.outcome = replace(runner.driver.outcome, state="failed", usage=parse_usage(lines(claude()), "claude"))
    TurnRunner._write_outcome(runner)
    updated = json.loads((tmp_path / "turn.json").read_text())
    assert updated["state"] == "complete"
    assert updated["usage"] == runner.driver.outcome.usage


@given(st.recursive(st.one_of(st.none(), st.booleans(), st.integers(), st.text()),
                    lambda children: st.one_of(st.lists(children, max_size=3),
                                               st.dictionaries(st.sampled_from(("id", "turn", "total", "last", "usage", "input_tokens")), children, max_size=3)),
                    max_leaves=10))
@settings(deadline=None)
def test_c_12_10_unfamiliar_container_shapes_do_not_raise(value):
    """C-18.5: unfamiliar provider field types give null or refused records, never a crash."""
    parse_usage(lines({"method": "turn/started", "params": value},
                      {"method": "thread/tokenUsage/updated", "params": value}), "codex")
    parse_usage(lines({"type": "result", "usage": value}), "claude")
    parse_usage(lines({"type": "turn.completed", "usage": value}), "codex")


@given(st.integers(min_value=0, max_value=10**12), st.integers(min_value=0, max_value=10**12),
       st.integers(min_value=0, max_value=10**12), st.integers(min_value=0, max_value=10**12))
@settings(deadline=None)
def test_c_12_10_claude_nonnegative_components_bound_cache_and_share(input, read, write, output):
    """C-18.5: claude nonnegative components bound cache and share."""
    record = parse_usage(lines(claude(input, read, write, output)), "claude")
    values = record["normalized"]
    assert values["prompt"] == input + read + write
    assert 0 <= values["cache_read"] <= values["prompt"]
    assert 0 <= values["cache_write"] <= values["prompt"]
    assert values["cache_hit_share"] is None or 0 <= values["cache_hit_share"] <= 1
    assert record == parse_usage(lines(claude(input, read, write, output)), "claude")


@given(st.integers(min_value=0, max_value=10**12), st.integers(min_value=0, max_value=10**12),
       st.integers(min_value=0, max_value=10**12))
@settings(deadline=None)
def test_c_12_10_codex_cache_subset_invariant_or_whole_record_refused(prompt, read, write):
    """C-18.5: codex cache subset invariant or whole record refused."""
    record = parse_usage(lines(codex(prompt, read, write)), "codex")
    if read > prompt or write > prompt:
        assert record is None
    else:
        values = record["normalized"]
        assert 0 <= values["cache_read"] <= values["prompt"]
        assert 0 <= values["cache_write"] <= values["prompt"]
        assert values["cache_hit_share"] is None or 0 <= values["cache_hit_share"] <= 1


@given(st.integers(min_value=0), st.integers(min_value=0), st.integers(min_value=0),
       st.permutations(("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
@settings(deadline=None)
def test_c_12_10_duplicate_message_usage_is_order_independent(input, read, write, order):
    """C-18.5: duplicate message usage is order independent."""
    counters = dict(input_tokens=input, cache_read_input_tokens=read, cache_creation_input_tokens=write)
    duplicate_frames = [assistant("same", **{key: counters[key]}) for key in order]
    expected = parse_usage(lines(assistant("same", **counters), claude()), "claude")
    assert parse_usage(lines(*duplicate_frames, claude()), "claude") == expected


@given(st.lists(st.text().filter(lambda text: '"usage"' not in text and '"modelUsage"' not in text)))
@settings(deadline=None)
def test_c_12_10_no_usage_never_produces_a_record(noise):
    """C-18.5: no usage never produces a record."""
    assert parse_usage(noise, "claude") is None
    assert parse_usage(noise, "codex") is None
