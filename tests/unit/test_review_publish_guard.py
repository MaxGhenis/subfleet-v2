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


def protected(tmp_path, provider='codex', sandbox='workspace-write', task='review', kind='dispatch'):
    work = tmp_path / 'work'
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
@pytest.mark.parametrize('task,kind', [('review', 'dispatch'), ('build', 'gate-review')])
def test_review_launch_real_git_push_local_file_is_denied(tmp_path, provider, sandbox, task, kind):
    remote = local_remote(tmp_path)
    launch = protected(tmp_path, provider, sandbox, task, kind)
    # Absolute git also inherits transport denial, even with a local file remote.
    for git in ['git', shutil.which('git')]:
        result = run([git, 'push', remote, 'HEAD:refs/heads/proof'], launch)
        assert result.returncode != 0, result.stdout
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
])
def test_review_gh_write_denied_reads_reach_fake(tmp_path, monkeypatch, args, allowed):
    fake = tmp_path / 'bin'
    fake.mkdir()
    gh = fake / 'gh'
    gh.write_text('#!/bin/sh\necho fake-gh "$@"\n')
    gh.chmod(0o755)
    monkeypatch.setenv('PATH', str(fake) + os.pathsep + os.environ['PATH'])
    launch = protected(tmp_path)
    result = run(['gh', *args], launch)
    assert (result.returncode == 0) is allowed
    assert ('fake-gh' in result.stdout) is allowed
    if not allowed:
        assert 'publish: denied' in result.stderr


@settings(max_examples=20, deadline=None, database=None)
@given(sandbox=st.sampled_from(['read-only', 'workspace-write']),
       kind=st.sampled_from(['dispatch', 'resume', 'gate-review']),
       options=st.sampled_from([[], ['--force'], ['--dry-run'], ['--mirror']]))
def test_review_launch_never_allows_push(sandbox, kind, options):
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        remote = local_remote(Path(directory))
        launch = protected(Path(directory), sandbox=sandbox, kind=kind)
        refs = [] if '--mirror' in options else ['HEAD:refs/heads/proof']
        result = run(['git', '-c', 'protocol.file.allow=always', 'push', *options, remote, *refs], launch)
        assert result.returncode != 0 and 'publish: denied' in result.stderr


def test_non_review_launch_is_unchanged(tmp_path):
    launch = protected(tmp_path, task='build')
    assert 'PATH' not in launch.env_add and not launch.notes.get('publish_denied')
