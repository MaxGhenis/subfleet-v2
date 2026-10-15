"""D-SH1 round 2: reviews can test in their worktree, but cannot publish."""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import cli, protocol
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Sandbox
from subfleet.daemon import after, utcnow
from tests.fake.test_review_needs_shell import shell_state, review  # noqa: F401
from tests.fake.test_state_contract import receipt_fixture, state_daemon  # noqa: F401
from tests.fake.test_workspace_contract import repository


@pytest.mark.parametrize('model', ['sol61', 'opus'])
def test_policy_review_own_worktree_can_run_pytest(shell_state, model):
    service, harness = shell_state
    service.policy['permissions']['review'] = 'worktree-write'
    service.policy.pop('requires', None)
    repository(service, harness)
    job = service.dispatch(
        'submit', harness.submit_args(task='review', tier='hard', pinned_model=model,
                                     sandbox=protocol.POLICY_SANDBOX))['job_id']
    assert service.store.get_job(job)['sandbox'] == 'workspace-write'
    assert service.store.get_job(job)['needs_shell'] is False
    service._admit()
    attempt, = service.store.list_attempts(job)
    assert attempt['capability'] == 'writable'
    worktree = Path(service.store.get_job(job)['worktree'])
    (worktree / 'test_writes.py').write_text('def test_temp(tmp_path):\n    (tmp_path / "ok").write_text("ok")\n')
    # The outer pytest owns the sole shared-lock hold: exercise pytest in-process.
    assert pytest.main(['-q', '-p', 'no:cacheprovider', str(worktree / 'test_writes.py')]) == 0


@pytest.mark.parametrize('mode', ['non-git', 'in-place', 'isolated'])
@pytest.mark.parametrize('model,capability,words', [
    ('sol61', 'commands', 'temp files included, so pytest could not run'),
    ('opus', 'none', 'could not run any command'),
])
def test_read_only_policy_reviews_carry_capability_without_editing_output(shell_state, mode, model, capability, words):
    service, harness = shell_state
    service.policy['permissions']['review'] = 'worktree-write'
    service.policy.pop('requires', None)
    changes = {}
    if mode != 'non-git':
        repo = repository(service, harness)
        if mode == 'in-place':
            changes['in_place'] = True
        else:
            changes.update(isolated_review=True, review_root=str(repo))
    output = harness.root / 'result.md'
    job = service.dispatch('submit', harness.submit_args(task='review', tier='hard', pinned_model=model,
        sandbox=protocol.POLICY_SANDBOX, out_path=str(output), **changes))['job_id']
    assert service.store.get_job(job)['sandbox'] == 'read-only'
    service._admit()
    attempt, = service.store.list_attempts(job)
    assert attempt['capability'] == capability
    adir = service.root / 'jobs' / job / 'a1'
    adir.mkdir()
    service._finalize(receipt_fixture(service, attempt, adir))
    service._export(job)
    shown = service.dispatch('show', {'job_id': job})
    assert shown['attempts'][0]['capability'] == capability
    notice, = [n for n in service.store.list_notices() if n['job_id'] == job]
    assert words in notice['text'].splitlines()[0]
    assert 'publish: denied' in notice['text'].splitlines()[0]
    assert words in cli._wait_summary(shown)
    assert output.read_bytes() == (adir / 'deliverable.md').read_bytes() == b'fixture result\n'


def test_explicit_workspace_write_outside_git_still_refused(state_daemon):
    service, harness = state_daemon
    with pytest.raises(AdapterError) as refused:
        service.dispatch('submit', harness.submit_args(sandbox='workspace-write'))
    assert '-s read-only' in refused.value.fix


@settings(max_examples=80, deadline=None, database=None)
@given(task=st.sampled_from(['review', 'build', 'research', None]),
       source=st.sampled_from(['policy', 'read-only', 'workspace-write']),
       in_place=st.booleans(), git=st.booleans(), isolated=st.booleans(), gate=st.booleans())
def test_resolved_sandbox_table(task, source, in_place, git, isolated, gate):
    from subfleet.policy import resolve_sandbox
    policy = {'permissions': {'review': 'worktree-write', 'build': 'workspace-write', '*': 'read-only'}}
    if source != 'policy':
        expected = source
    elif isolated or gate:
        expected = 'read-only'
    elif task == 'review':
        expected = 'workspace-write' if git and not in_place else 'read-only'
    else:
        expected = 'workspace-write' if task == 'build' else 'read-only'
    assert resolve_sandbox(policy, source, task=task, in_place=in_place, git=git,
                           isolated=isolated, gate=gate) == expected


def test_status_counts_distinct_restricted_runs_last_24h(shell_state):
    service, harness = shell_state
    service.policy.pop('requires', None)
    for model in ['sol61', 'opus']:
        service.dispatch('submit', harness.submit_args(task='review', tier='hard', pinned_model=model))
    service._admit()
    jobs = service.store.list_jobs()
    view = service.dispatch('daemon.status', {})
    assert set(view['restricted_runs_24h']) == {j['job_id'] for j in jobs}
    text = cli.format_status(view)
    assert 'last 24h: 2 runs with no shell or no writes' in text
    assert all(j['job_id'] in text for j in jobs)
    with service.store.transaction('fixture.old') as tx:
        tx.execute('UPDATE attempts SET reserved_at=?', (after(-90000),))
    assert service.dispatch('daemon.status', {})['restricted_runs_24h'] == []
