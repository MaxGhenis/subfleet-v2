"""Long successful attempts must not lose their terminal provider result."""
import json

import pytest

from subfleet.adapters.claude_stream import parse_lines, parse_stream
from subfleet.contracts import OutcomeClass
from tests.conftest import FIXTURES, case_names, exit_info, make_launch


def large_attempt(tmp_path, *, terminal='success', middle_model='claude-opus-5'):
    attempt = tmp_path / 'a1'
    attempt.mkdir()
    model, session = 'claude-opus-5', 'large-stream-session'
    final = 'The complete design is committed, with all sections verified.'
    path = attempt / 'stdout'
    with path.open('w') as handle:
        def event(value):
            handle.write(json.dumps({'session_id': session, **value}) + '\n')
        def assistant(text, served=model, **extra):
            event({'type': 'assistant', 'message': {'model': served,
                  'content': [{'type': 'text', 'text': text}]}, **extra})
        event({'type': 'system', 'subtype': 'init', 'model': model})
        assistant('The design is 1,500 lines; sections 529 and 543 are checked.')
        for index in range(20):
            event({'type': 'user', 'message': {'content': [{'type': 'tool_result',
                   'content': 'x' * 250_000}]}})
            if index == 9:
                assistant('Intermediate analysis.', middle_model)
        assistant(final)
        if terminal == 'limited':
            event({'type': 'rate_limit_event', 'rate_limit_info': {
                'status': 'rejected', 'rateLimitType': 'seven_day'}})
        event({'type': 'result', 'subtype': 'success' if terminal != 'error' else 'error_during_execution',
               'is_error': terminal == 'error', 'stop_reason': 'end_turn',
               'result': final if terminal != 'error' else '',
               'errors': ['Service unavailable'] if terminal == 'error' else []})
    assert path.stat().st_size > 4_000_000
    launch = make_launch(attempt, session_id=session, model_id=model,
                         projects_dir=tmp_path / 'projects', identity=None, label=None)
    return attempt, launch, final


def test_success_beyond_diagnostic_prefix_is_accepted_and_delivered(adapter, tmp_path):
    attempt, launch, final = large_attempt(tmp_path)
    # The old prefix omitted result and matched the innocent "1,500" as HTTP500.
    assert parse_stream(adapter.raw_stream_text(attempt, launch)).result is None
    outcome = adapter.classify(attempt, launch, exit_info(0))
    assert outcome.cls is OutcomeClass.OK, outcome.detail
    assert not outcome.evidence['stream_truncated_tail']
    assert adapter.deliverable(attempt, launch, outcome) == final.encode()


@pytest.mark.parametrize('terminal, expected', [('error', OutcomeClass.TRANSIENT),
                                               ('limited', OutcomeClass.LIMITED)])
def test_true_terminal_failure_after_large_body_is_not_hidden(adapter, tmp_path, terminal, expected):
    attempt, launch, _ = large_attempt(tmp_path, terminal=terminal)
    outcome = adapter.classify(attempt, launch, exit_info(0))
    assert outcome.cls is expected
    if terminal == 'limited':
        assert outcome.closure is not None
    else:
        assert outcome.closure is None


def test_complete_stream_keeps_middle_model_evidence(adapter, tmp_path):
    attempt, launch, _ = large_attempt(tmp_path, middle_model='claude-sonnet-4-6')
    summary = adapter.stream_summary(attempt, launch)
    assert summary.assistant_models == ('claude-opus-5', 'claude-sonnet-4-6', 'claude-opus-5')
    outcome = adapter.classify(attempt, launch, exit_info(0))
    assert outcome.cls is OutcomeClass.OK and outcome.served_model is None


@pytest.mark.parametrize('case', case_names())
def test_incremental_parser_matches_existing_provider_fixtures(case):
    path = FIXTURES / case / 'stdout'
    with path.open() as handle:
        assert parse_lines(handle) == parse_stream(path.read_text())


@pytest.mark.parametrize('text', ['bad\n{}\n', '{}\nbad\n\n', '[]\nnull\n',
                                 '\n', '{\n "type": "result",\n "is_error": false\n}\n',
                                 '{"type":"result",\n"subtype":"success","is_error":false,"result":"ok"}\n'])
def test_incremental_parser_keeps_malformed_and_legacy_json_semantics(text):
    assert parse_lines(iter(text.splitlines(keepends=True))) == parse_stream(text)
