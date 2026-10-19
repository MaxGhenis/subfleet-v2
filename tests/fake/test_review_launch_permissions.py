"""D-SH1 round 2: reviews can test in their worktree, but cannot publish."""
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import cli, protocol
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Sandbox
from subfleet.daemon import after, utcnow
from tests.fake.test_review_needs_shell import shell_state, review  # noqa: F401
from tests.fake.test_state_contract import launched, receipt_fixture, state_daemon  # noqa: F401
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
    launched(service, attempt)
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
    launched(service, attempt)
    attempt, = service.store.list_attempts(job)
    assert attempt['capability'] == capability
    adir = service.root / 'jobs' / job / 'a1'
    adir.mkdir()
    service._finalize(receipt_fixture(service, attempt, adir))
    service._export(job)
    shown = service.dispatch('show', {'job_id': job})
    assert shown['attempts'][0]['capability'] == capability
    assert shown['job']['needs_shell'] is False
    assert shown['attempts'][0]['publish_denied'] is True
    waited = service.dispatch('wait', {'job_ids': [job], 'deadline_s': 0})
    assert waited['jobs'][0]['attempt']['publish_denied'] is True
    notice, = [n for n in service.store.list_notices() if n['job_id'] == job]
    header, *_, capability_line = notice['text'].splitlines()
    assert header == (f"{job}: succeeded; rc=0; deliverable={adir / 'deliverable.md'}; "
                      f"out={output}")
    assert capability_line.startswith(f"ran read-only on {attempt['lane_id']} ({model})")
    assert words in capability_line
    assert capability_line.endswith('; publish: denied')
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
    from subfleet.offline import Offline
    # P3-2: reserved attempts have not launched, so they claim no capability and count for nothing.
    assert service.dispatch('daemon.status', {})['restricted_runs_24h'] == []
    assert Offline(service.root).status()['restricted_runs_24h'] == []
    for job in jobs:
        launched(service, service.store.list_attempts(job['job_id'])[0])
    first = service.store.list_attempts(jobs[0]['job_id'])[0]
    service.store.add_attempt(attempt_id=jobs[0]['job_id'] + '/a2', job_id=jobs[0]['job_id'], seq=2,
        lane_id=first['lane_id'], model_requested=first['model_requested'], capability='commands')
    assert set(Offline(service.root).status()['restricted_runs_24h']) == {j['job_id'] for j in jobs}
    view = service.dispatch('daemon.status', {})
    assert set(view['restricted_runs_24h']) == {j['job_id'] for j in jobs}
    assert all(j['needs_shell'] is False for j in view['jobs'])
    assert all(j['needs_shell'] is False for j in service.dispatch('list', {})['jobs'])
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


# #221 review r1b, P2 (D-SH1 round 6): a policy review in its own worktree used to
# review HEAD, not what its caller had in front of them. The review's probe: in a
# committed repository, edit tracked.txt and add an untracked new_module.py.
def dirty_checkout(service, harness):
    from tests.fake.test_workspace_contract import git
    repo = repository(service, harness)
    (repo / 'tracked.txt').write_text('edited by the caller\n')
    (repo / 'new_module.py').write_text('VALUE = 1\n')
    (repo / '.git' / 'info' / 'exclude').write_text('ignored.log\n')
    (repo / 'ignored.log').write_text('never copied\n')
    status = git(repo, 'status', '--porcelain')
    return repo, status


def policy_review(service, harness, **changes):
    service.policy['permissions']['review'] = 'worktree-write'
    service.policy.pop('requires', None)
    return service.dispatch('submit', harness.submit_args(task='review', tier='hard', pinned_model='sol61',
                                                          sandbox=protocol.POLICY_SANDBOX, **changes))['job_id']


def finished_notice(service, job):
    attempt, = service.store.list_attempts(job)
    adir = service.root / 'jobs' / job / 'a1'
    adir.mkdir(exist_ok=True)
    service._finalize(receipt_fixture(service, attempt, adir))
    notice, = [n for n in service.store.list_notices() if n['job_id'] == job]
    return notice['text'].splitlines()


def test_policy_review_worktree_sees_the_callers_uncommitted_and_untracked_files(shell_state):
    from tests.fake.test_workspace_contract import git
    service, harness = shell_state
    repo, status = dirty_checkout(service, harness)
    job = policy_review(service, harness)
    assert service.store.get_job(job)['sandbox'] == 'workspace-write'
    service._admit()
    worktree = Path(service.store.get_job(job)['worktree'])
    assert worktree != repo and worktree.parent == service.root / 'worktrees'
    assert (worktree / 'tracked.txt').read_text() == 'edited by the caller\n'
    assert (worktree / 'new_module.py').read_text() == 'VALUE = 1\n'
    assert not (worktree / 'ignored.log').exists()
    # The caller's checkout is untouched, index included.
    assert git(repo, 'status', '--porcelain') == status
    record = {'files': 2, 'tracked': 1, 'untracked': 1}
    shown = service.dispatch('show', {'job_id': job})
    assert {key: shown['uncommitted'][key] for key in record} == record
    from subfleet.offline import Offline
    assert Offline(service.root).show_job(job)['uncommitted'] == shown['uncommitted']
    words = "the worktree included the caller's uncommitted changes as of submit: 2 files (1 tracked, 1 untracked)"
    assert words in cli._format_job(shown)
    prepared = (service.root / 'jobs' / job / 'prompt.prepared.md').read_text()
    assert "plus the caller's uncommitted changes and untracked files as of submit (2 files)" in prepared
    assert 'none of the caller\'s uncommitted' not in prepared
    lines = finished_notice(service, job)
    assert lines[-2:] == [words, 'publish: denied']


@pytest.mark.parametrize('cause', ['too-many-bytes', 'too-many-files', 'nested-repository'])
def test_policy_review_falls_back_to_read_only_in_place_when_the_copy_fails(shell_state, monkeypatch, cause):
    from subfleet import uncommitted
    from tests.fake.test_workspace_contract import git
    service, harness = shell_state
    repo, _status = dirty_checkout(service, harness)
    if cause == 'too-many-bytes':
        monkeypatch.setattr(uncommitted, 'MAX_BYTES', 8)
        reason = 'more than 8 bytes'
    elif cause == 'too-many-files':
        monkeypatch.setattr(uncommitted, 'MAX_FILES', 1)
        reason = '2 files, more than 1'
    else:
        (repo / 'vendor').mkdir()
        git(repo / 'vendor', 'init')        # an untracked nested repository
        reason = 'untracked vendor/ is not a regular file or symbolic link'
    job = policy_review(service, harness)
    row = service.store.get_job(job)
    assert row['sandbox'] == 'read-only' and not row['in_place']
    service._admit()
    row = service.store.get_job(job)
    assert row['worktree'] is None and service._launch_dir(row, 'codex') == str(repo)
    attempt, = service.store.list_attempts(job)
    launched(service, attempt)
    attempt, = service.store.list_attempts(job)
    assert attempt['capability'] == 'commands'
    shown = service.dispatch('show', {'job_id': job})
    assert shown['uncommitted'] == {'fallback': reason}
    words = ("ran read-only in the caller's checkout: its uncommitted changes could not be copied "
             f"into a worktree ({reason})")
    assert words in cli._format_job(shown)
    assert words in finished_notice(service, job)


def test_explicit_workspace_write_review_keeps_todays_worktree(shell_state):
    service, harness = shell_state
    dirty_checkout(service, harness)
    service.policy.pop('requires', None)
    job = service.dispatch('submit', harness.submit_args(task='review', tier='hard', pinned_model='sol61',
                                                         sandbox='workspace-write'))['job_id']
    service._admit()
    worktree = Path(service.store.get_job(job)['worktree'])
    assert (worktree / 'tracked.txt').read_text() == 'baseline\n'
    assert not (worktree / 'new_module.py').exists()
    assert service.dispatch('show', {'job_id': job})['uncommitted'] is None
    assert not (service.root / 'jobs' / job / 'uncommitted').exists()


def test_clean_checkout_policy_review_copies_nothing(shell_state):
    service, harness = shell_state
    repository(service, harness)
    job = policy_review(service, harness)
    service._admit()
    assert service.store.get_job(job)['worktree']
    assert service.dispatch('show', {'job_id': job})['uncommitted'] is None
    prepared = (service.root / 'jobs' / job / 'prompt.prepared.md').read_text()
    assert "none of the caller's uncommitted, untracked or ignored files" in prepared


# P3-8: a writable review was told "Commit each coherent step ... Leave a concise
# report of changes and tests", the build preamble, which also implied that
# pushing to a non-main branch was allowed.
def test_writable_review_gets_a_review_preamble_and_a_build_keeps_its_own(shell_state):
    from subfleet.daemon import REVIEW_PREAMBLE, WRITE_PREAMBLE
    service, harness = shell_state
    repository(service, harness)
    review_job = policy_review(service, harness)
    build_job = service.dispatch('submit', harness.submit_args(task='build', pinned_model='sol61',
                                                               sandbox='workspace-write',
                                                               caller_session=None))['job_id']
    review_prompt = (service.root / 'jobs' / review_job / 'prompt.prepared.md').read_text()
    build_prompt = (service.root / 'jobs' / build_job / 'prompt.prepared.md').read_text()
    assert review_prompt.startswith(REVIEW_PREAMBLE) and 'Commit each coherent step' not in review_prompt
    assert 'may run tests and write scratch files' in review_prompt and 'Do not commit, push or publish' in review_prompt
    assert build_prompt.startswith(WRITE_PREAMBLE)



# P3-4: for all three probe cases the daemon gave `"publish_denied": true` and
# offline JSON gave `1`; offline `list` converted nothing at all.
@pytest.mark.parametrize('model,sandbox,capability', [
    ('opus', 'read-only', 'none'), ('sol61', 'read-only', 'commands'), ('sol61', 'workspace-write', 'writable')])
def test_offline_json_matches_the_daemon_for_launch_flags(shell_state, model, sandbox, capability):
    import json
    from subfleet.offline import Offline
    service, harness = shell_state
    service.policy.pop('requires', None)
    if sandbox == 'workspace-write':
        repository(service, harness)
    job = service.dispatch('submit', harness.submit_args(task='review', tier='hard', pinned_model=model,
                                                         sandbox=sandbox))['job_id']
    service._admit()
    attempt, = service.store.list_attempts(job)
    launched(service, attempt)
    offline = Offline(service.root)
    daemon_attempt = service.dispatch('show', {'job_id': job})['attempts'][0]
    offline_attempt = offline.show_job(job)['attempts'][0]
    assert daemon_attempt['capability'] == offline_attempt['capability'] == capability
    for key in ('publish_denied', 'has_shell'):
        assert json.dumps(offline_attempt[key]) == json.dumps(daemon_attempt[key]), key
    assert daemon_attempt['publish_denied'] is True
    daemon_job, = [row for row in service.dispatch('list', {})['jobs'] if row['job_id'] == job]
    offline_job, = [row for row in offline.list_jobs() if row['job_id'] == job]
    assert offline_job['needs_shell'] is daemon_job['needs_shell'] is False
    assert offline.show_job(job)['needs_shell'] is False
    for key in ('publish_denied', 'has_shell'):
        if key in offline_job and key in daemon_job:
            assert json.dumps(offline_job[key]) == json.dumps(daemon_job[key]), key
