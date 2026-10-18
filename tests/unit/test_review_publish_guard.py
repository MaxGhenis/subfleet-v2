"""Run real Git only against local file:// repositories; gh is always fake."""
from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from hypothesis import given, settings, strategies as st

from subfleet.contracts import Credential, Sandbox
from tests.conftest import make_job, make_lane


def protected(tmp_path, provider='codex', sandbox='workspace-write', task='review', kind='dispatch', workdir=None):
    work = Path(workdir) if workdir else tmp_path / 'work'
    work.mkdir(exist_ok=True)
    prompt = tmp_path / 'prompt'
    prompt.write_text('review')
    job = replace(make_job(str(work), str(prompt)), task=task, kind=kind, sandbox=Sandbox(sandbox))
    if provider == 'claude':
        from subfleet.adapters.claude import ClaudeAdapter
        adapter = ClaudeAdapter(claude_bin='fake-provider', projects_dir=tmp_path / 'projects')
        lane = make_lane()
    else:
        from subfleet.adapters.codex import CodexAdapter
        adapter = CodexAdapter(codex_bin='fake-provider')
        home = str(tmp_path / 'home')
        lane = replace(make_lane(), provider='codex', home=home, credential=Credential('codex', home, 'home'))
    if kind == 'resume':
        return adapter.resume_launch(job, 'job/a1', tmp_path / 'attempt', lane, {},
                                     'fixture-thread', prompt, 'hooks={}', model_id='fixture-model')
    return adapter.build_launch(job, 'job/a1', tmp_path / 'attempt', lane, {},
                                'fixture-model', None, prompt, 'hooks={}')


def run(args, launch):
    return subprocess.run(args, cwd=launch.cwd, env={**os.environ, **launch.env_add},
                          capture_output=True, text=True, timeout=10)


def local_remote(tmp_path):
    git = shutil.which('git')
    bare = tmp_path / 'remote.git'
    subprocess.run([git, 'init', '--bare', str(bare)], check=True, capture_output=True)
    work = tmp_path / 'work'
    work.mkdir(exist_ok=True)
    subprocess.run([git, 'init', str(work)], check=True, capture_output=True)
    subprocess.run([git, '-C', str(work), '-c', 'user.name=Fixture', '-c', 'user.email=fake@example.org',
                    'commit', '--allow-empty', '-m', 'baseline'], check=True, capture_output=True)
    return bare.as_uri()


@pytest.mark.parametrize('provider', ['codex', 'claude'])
@pytest.mark.parametrize('sandbox', ['read-only', 'workspace-write'])
@pytest.mark.parametrize('task,kind', [('review', 'dispatch'), ('review', 'resume'), ('build', 'gate-review')])
def test_review_launch_real_git_push_local_file_is_denied(tmp_path, provider, sandbox, task, kind):
    remote = local_remote(tmp_path)
    launch = protected(tmp_path, provider, sandbox, task, kind)
    # Absolute git also inherits transport denial, even with a local file remote.
    for git in ['git', shutil.which('git')]:
        result = run([git, 'push', remote, 'HEAD:refs/heads/proof'], launch)
        assert result.returncode != 0, result.stdout
    # A configured pushurl must not bypass the inherited restriction either.
    real_git = shutil.which('git')
    subprocess.run([real_git, '-C', launch.cwd, 'remote', 'add', 'fixture', remote], check=True)
    subprocess.run([real_git, '-C', launch.cwd, 'config', 'remote.fixture.pushurl', remote], check=True)
    assert run([real_git, '-c', 'protocol.file.allow=always', 'push', 'fixture', 'HEAD:refs/heads/proof'], launch).returncode != 0
    # Removing environment/config markers still cannot enable the wrapped git.
    assert run(['/usr/bin/env', '-u', 'GIT_ALLOW_PROTOCOL', '-u', 'GIT_CONFIG_COUNT',
                'git', '-c', 'protocol.file.allow=always', 'push', remote, 'HEAD'], launch).returncode != 0
    assert launch.notes['publish_denied'] is True
    assert run(['git', 'log', '-1', '--oneline'], launch).returncode == 0
    assert subprocess.run([shutil.which('git'), 'ls-remote', remote], capture_output=True).stdout == b''


@pytest.mark.parametrize('args,allowed', [
    (['pr', verb, '7'], False) for verb in ['create', 'merge', 'comment', 'review', 'edit', 'close', 'ready']
] + [(['issue', verb, '7'], False) for verb in ['create', 'comment']] + [
    (['api', 'repos/x/y', '-X', 'POST'], False),
    (['api', '--method=DELETE', 'repos/x/y'], False),
    (['api', 'repos/x/y', '-XPOST'], False),
    (['api', 'repos/x/y', '-f', 'a=b'], False),
    (['api', 'repos/x/y', '-Fname=value'], False),
    (['api', 'repos/x/y', '--field=name=value'], False),
    (['api', 'repos/x/y', '-XGET', '--raw-field=a=b'], False),
    (['pr', 'view', '7'], True), (['pr', 'diff', '7'], True), (['pr', 'checks', '7'], True),
    (['api', 'repos/x/y'], True), (['api', '-X', 'GET', 'repos/x/y'], True),
    (['--repo', 'x/y', 'pr', 'comment', '7'], False),
    (['alias', 'set', 'publish', 'pr create'], False),
    (['-Rx/y', 'pr', 'view', '7'], True),
    (['api', 'repos/x/y', '--input=body.json'], False),
    (['api', 'graphql'], False),
])
@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_review_gh_write_denied_reads_reach_fake(tmp_path, monkeypatch, args, allowed, provider):
    fake = tmp_path / 'bin'
    fake.mkdir()
    gh = fake / 'gh'
    gh.write_text('#!/bin/sh\necho fake-gh "$@"\n')
    gh.chmod(0o755)
    monkeypatch.setenv('PATH', str(fake) + os.pathsep + os.environ['PATH'])
    launch = protected(tmp_path, provider=provider)
    result = run(['gh', *args], launch)
    assert (result.returncode == 0) is allowed
    assert ('fake-gh' in result.stdout) is allowed
    if not allowed:
        assert 'publish: denied' in result.stderr


@settings(max_examples=20, deadline=None, database=None)
@given(provider=st.sampled_from(['claude', 'codex']), sandbox=st.sampled_from(['read-only', 'workspace-write']),
       kind=st.sampled_from(['dispatch', 'resume', 'gate-review']),
       options=st.sampled_from([[], ['--force'], ['--dry-run'], ['--mirror']]))
def test_review_launch_never_allows_push(provider, sandbox, kind, options):
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        remote = local_remote(Path(directory))
        launch = protected(Path(directory), provider=provider, sandbox=sandbox, kind=kind)
        refs = [] if '--mirror' in options else ['HEAD:refs/heads/proof']
        result = run(['git', '-c', 'protocol.file.allow=always', 'push', *options, remote, *refs], launch)
        assert result.returncode != 0 and 'publish: denied' in result.stderr


def test_non_review_launch_is_unchanged(tmp_path):
    launch = protected(tmp_path, task='build')
    assert 'PATH' not in launch.env_add and not launch.notes.get('publish_denied')


# #221 review r1b, P2: gh's flag parser lets a boolean flag lead a cluster, so
# `-iXPOST` is `-i -X POST` and `-ifbody=x` is `-i -f body=x`. Real gh 2.100.0
# sent POST for both (GH_DEBUG=api against a closed localhost port). Every
# input the review executed, plus the cluster shapes the rule names.
CLUSTER_WRITES = [
    ['api', '-ifbody=published', 'repos/o/r/issues/1/comments'],
    ['api', '-iXPOST', 'repos/o/r/issues/1/comments'],
    ['api', '-iX', 'POST', 'repos/o/r/pulls/1/merge'],
    ['api', '-iFbody=@file', 'repos/o/r/issues/1/comments'],
    ['api', '-iX', 'GET', 'repos/o/r'],             # only -XGET and -X GET are exempt
    ['api', '-Xget', 'repos/o/r'],
    ['api', '-HAccept:x', '-f', 'a=b', 'repos/o/r'],
    ['api', '--method', 'PATCH', 'repos/o/r'],
]


@pytest.mark.parametrize('args', CLUSTER_WRITES, ids=lambda args: ' '.join(args))
@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_gh_flag_clusters_that_real_gh_sends_as_writes_are_refused(tmp_path, monkeypatch, args, provider):
    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'gh').write_text('#!/bin/sh\necho fake-gh "$@"\n')
    (fake / 'gh').chmod(0o755)
    monkeypatch.setenv('PATH', str(fake) + os.pathsep + os.environ['PATH'])
    launch = protected(tmp_path, provider=provider)
    result = run(['gh', *args], launch)
    assert result.returncode == 7 and 'fake-gh' not in result.stdout, result.stdout
    assert 'publish: denied' in result.stderr


@pytest.mark.parametrize('args', [
    ['api', '-XGET', 'repos/o/r'], ['api', '-X', 'GET', 'repos/o/r'], ['api', '-i', 'repos/o/r'],
    ['api', '--method=GET', '-H', 'Accept: x', 'repos/o/r'], ['api', '--paginate', '-q', '.[]', 'repos/o/r/pulls'],
])
def test_gh_api_get_clusters_still_read(args):
    from subfleet.review_publish import gh_read
    assert gh_read(args)


# P3-7: the reads a review needs once the wrapper and hook refuse the rest.
REVIEW_READS = [
    ['run', 'view', '123', '--log-failed'], ['run', 'view', '123', '--log'], ['run', 'list'],
    ['run', 'watch', '123'], ['issue', 'view', '7'], ['issue', 'list'], ['pr', 'list'],
    ['pr', 'status'], ['repo', 'view'], ['release', 'list'], ['release', 'view', 'v1'],
    ['workflow', 'list'], ['workflow', 'view', 'tests.yml'], ['search', 'prs', 'q'],
    ['label', 'list'], ['pr', 'view', '7', '--comments'], ['-R', 'o/r', 'run', 'view', '1'],
]


@pytest.mark.parametrize('args', REVIEW_READS, ids=lambda args: ' '.join(args))
def test_gh_review_reads_reach_gh(tmp_path, monkeypatch, args):
    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'gh').write_text('#!/bin/sh\necho fake-gh "$@"\n')
    (fake / 'gh').chmod(0o755)
    monkeypatch.setenv('PATH', str(fake) + os.pathsep + os.environ['PATH'])
    launch = protected(tmp_path)
    result = run(['gh', *args], launch)
    assert result.returncode == 0 and 'fake-gh' in result.stdout, result.stderr


@pytest.mark.parametrize('args', [
    ['run', 'rerun', '1'], ['run', 'cancel', '1'], ['workflow', 'run', 'x'], ['issue', 'close', '1'],
    ['release', 'create', 'v1'], ['repo', 'edit'], ['label', 'create', 'x'], ['issue', 'edit', '1'],
    ['run', 'delete', '1'], ['secret', 'list'], ['gist', 'create', 'x'], ['auth', 'token'],
])
def test_gh_other_verbs_stay_refused(args):
    from subfleet.review_publish import gh_read
    assert not gh_read(args)
