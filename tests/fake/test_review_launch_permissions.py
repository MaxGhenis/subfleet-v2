"""D-SH1 round 2: reviews can test in their worktree, but cannot publish."""
from pathlib import Path

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
def test_policy_review_own_worktree_can_run_pytest(shell_state, model, tmp_path, monkeypatch):
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
    (worktree / f'test_writes_{model}.py').write_text('def test_temp(tmp_path):\n    (tmp_path / "ok").write_text("ok")\n')
    from tests.unit.test_review_publish_guard import protected
    provider = service.policy['models'][model]['provider']
    launch = protected(tmp_path, provider=provider, workdir=worktree)
    assert launch.notes['capability'] == 'writable' and launch.notes['publish_denied']
    for name, value in launch.env_add.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(worktree)
    # The outer pytest owns the sole shared-lock hold: exercise pytest in-process.
    assert pytest.main(['-q', '-p', 'no:cacheprovider', '--import-mode=importlib', str(worktree / f'test_writes_{model}.py')]) == 0


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
    assert words in cli._format_job(shown) and 'publish: denied' in cli._format_job(shown)
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
    first = service.store.list_attempts(jobs[0]['job_id'])[0]
    service.store.add_attempt(attempt_id=jobs[0]['job_id'] + '/a2', job_id=jobs[0]['job_id'], seq=2,
        lane_id=first['lane_id'], model_requested=first['model_requested'], capability='commands')
    from subfleet.offline import Offline
    assert set(Offline(service.root).status()['restricted_runs_24h']) == {j['job_id'] for j in jobs}
    view = service.dispatch('daemon.status', {})
    assert set(view['restricted_runs_24h']) == {j['job_id'] for j in jobs}
    text = cli.format_status(view)
    assert 'last 24h: 2 runs with no shell or no writes' in text
    assert all(j['job_id'] in text for j in jobs)
    with service.store.transaction('fixture.old') as tx:
        tx.execute('UPDATE attempts SET reserved_at=?', (after(-90000),))
    assert service.dispatch('daemon.status', {})['restricted_runs_24h'] == []


@pytest.mark.parametrize('task,kind', [('review', 'dispatch'), ('build', 'gate-review')])
def test_reviews_refuse_explicit_host_push_before_any_remote_validation(state_daemon, task, kind):
    service, harness = state_daemon
    extra = {'isolated_review': True, 'review_root': str(harness.workdir), 'round_lease': 'gate:fixture:round:1'} if kind == 'gate-review' else {}
    with pytest.raises(AdapterError, match='reviews cannot publish'):
        service.dispatch('submit', harness.submit_args(task=task, kind=kind, push_branch='proof', **extra))


@pytest.mark.parametrize('task,kind', [('review', 'dispatch'), ('build', 'gate-review')])
def test_legacy_review_host_publish_is_denied_before_intake(state_daemon, task, kind):
    from subfleet.host_push import PushError
    service, harness = state_daemon
    with pytest.raises(PushError, match='publish: denied'):
        service._publish_bundle({'task': task, 'kind': kind})
