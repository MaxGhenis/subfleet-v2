"""Exact synthetic read-only GraphQL boundary for six existing R5 cases.

No external gh process is permitted. Product and repository tests are unchanged.
"""

import json
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path('/private/tmp/sf-a2-fix1')
TEST_MODULE = Path('/Users/maxghenis/.subfleet/worktrees/20261115-180648-rv-a2-fix-r1b/tests/unit/test_review_r5_repro.py')
ARGV = ['gh', 'api', 'graphql', '--input', '-']
BODY = {'data': {'p0': {'pullRequest': None}}}


class NotSyntheticRead(Exception):
    pass


def synthetic_reply(argv, kwargs, body, rc=0, sleep_s=0.0):
    """Validate inert arguments and return the fixture's fixed response only."""
    if not isinstance(argv, (list, tuple)) or list(argv) != ARGV:
        raise NotSyntheticRead('not the exact synthetic GraphQL read argv')
    if body != BODY or rc != 0 or sleep_s != 0:
        raise NotSyntheticRead('not the fixed missing synthetic PR response')
    try:
        request = json.loads(kwargs.get('input', ''))
    except (ValueError, TypeError):
        raise NotSyntheticRead('not a synthetic JSON query') from None
    if not isinstance(request, dict) or set(request) != {'query'}:
        raise NotSyntheticRead('not a query-only synthetic request')
    query = request['query']
    if (not isinstance(query, str) or not query.startswith('query { p0: ')
            or re.search(r'\bmutation\b', query, re.IGNORECASE)
            or 'repository(owner:"o",name:"r")' not in query
            or 'pullRequest(number:1)' not in query
            or len(re.findall(r'\brepository\(', query)) != 1
            or len(re.findall(r'\bpullRequest\(', query)) != 1):
        raise NotSyntheticRead('not the fixed o/r#1 read-only query')
    return subprocess.CompletedProcess(list(argv), 0, json.dumps(BODY), '')


@pytest.fixture(autouse=True)
def r5_synthetic_graphql(request, monkeypatch):
    if Path(getattr(request.module, '__file__', '')).resolve() != TEST_MODULE:
        return
    names = {
        'test_ready_refusal_survives_hold_rearm_and_ready_to_accept_restart',
        'test_ready_refusal_acceptance_is_atomic_across_restart',
    }
    if getattr(request.node, 'originalname', request.node.name) not in names:
        return
    with (ROOT / 'graphql-callbacks.jsonl').open('a') as stream:
        stream.write(json.dumps({'nodeid': request.node.nodeid,
                                 'operation': 'exact fixture alias replacement activated',
                                 'external_commands_executed': 0}) + '\n')

    def fixture_callback(tmp_path, case_monkeypatch, body, rc=0, sleep_s=0.0):
        # Preserve the helper's return shape without writing an executable or
        # altering PATH. The current six cases use only this exact response.
        if body != BODY or rc != 0 or sleep_s != 0:
            raise NotSyntheticRead('unreviewed synthetic fixture response')
        calls = Path(tmp_path) / 'gh-calls'
        original_run = subprocess.run

        def run(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and list(argv) == ARGV:
                result = synthetic_reply(argv, kwargs, body, rc, sleep_s)
                with calls.open('a') as stream:
                    stream.write('call\n')
                with (ROOT / 'graphql-callbacks.jsonl').open('a') as stream:
                    stream.write(json.dumps({'nodeid': request.node.nodeid,
                                             'operation': 'synthetic read-only GraphQL callback',
                                             'external_commands_executed': 0}) + '\n')
                return result
            # All other operations retain the original strict audit guard.
            return original_run(argv, *args, **kwargs)

        case_monkeypatch.setattr(subprocess, 'run', run)
        return calls

    monkeypatch.setattr(request.module, 'fake_gh', fixture_callback)
