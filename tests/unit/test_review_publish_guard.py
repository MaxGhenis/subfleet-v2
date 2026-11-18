"""Run real Git only against local file:// repositories; gh is always fake."""
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tomllib

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
    override = review_override(tmp_path, publish=task == 'review' or kind == 'gate-review')
    if kind == 'resume':
        return adapter.resume_launch(job, 'job/a1', tmp_path / 'attempt', lane, {},
                                     'fixture-thread', prompt, override, model_id='fixture-model')
    return adapter.build_launch(job, 'job/a1', tmp_path / 'attempt', lane, {},
                                'fixture-model', None, prompt, override)


def review_override(tmp_path, publish=True):
    """The `-c hooks=` override the daemon gives a review's Codex launch: the
    guard (here an allow-all stand-in) and the publish hook, as the preflight
    verifies them. Before #221 r1b's fix there was no publish hook to add."""
    guard = tmp_path / 'guard.sh'
    guard.write_text('#!/bin/sh\ncat >/dev/null\nexit 0\n')
    guard.chmod(0o755)
    try:
        from subfleet.guard.preflight import override_string
        from subfleet.review_publish import publish_hook
        if not publish:
            return override_string(guard)
        return override_string(guard, publish_command=publish_hook(tmp_path / 'state'))
    except (ImportError, TypeError):
        return 'hooks={}'


def run(args, launch):
    # 60 s: under load 150 to 250 a wrapper call took over 10 s (#221 review r3c P3).
    return subprocess.run(args, cwd=launch.cwd, env={**os.environ, **launch.env_add},
                          capture_output=True, text=True, timeout=60)


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
    assert run(['/usr/bin/env', '-u', 'GIT_ALLOW_PROTOCOL', '-u', 'GIT_CONFIG_PARAMETERS',
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
    # #221 review r2 P3-5: with an explicit GET, real gh 2.100.0 sends the field in the query string.
    (['api', 'repos/x/y', '-XGET', '--raw-field=a=b'], True),
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
    # #221 review r2 P3-5: gh's flag parser reads these as GET (real gh 2.100.0 sent GET for each).
    ['api', '-iX', 'GET', 'repos/o/r'], ['api', '-Xget', 'repos/o/r'],
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



# #221 review r1b, P1: both providers run commands through zsh, and zsh startup
# puts Homebrew (or path_helper's /usr/local/bin and /usr/bin) ahead of the
# wrapper directory. The review put a recording gh at the Homebrew position via
# a scratch ZDOTDIR and saw `gh pr comment 7 --body published` reach it in 4 of
# 4 cases. These run each provider's real command path: the PreToolUse hooks
# its launch carries, then (when they allow) the command through zsh as the
# provider would run it.
SHELLS = {
    # Codex runs `zsh -lc` (seen in a rollout on this host); either provider may run `zsh -c`.
    'zsh -c': lambda command, snapshot: ['/bin/zsh', '-c', command],
    'zsh -lc': lambda command, snapshot: ['/bin/zsh', '-lc', command],
    # Claude's Bash tool sources its shell snapshot, then evals the command.
    'claude snapshot': lambda command, snapshot: [
        '/bin/zsh', '-c', f'source {shlex.quote(str(snapshot))} && setopt NO_EXTENDED_GLOB 2>/dev/null || true '
                          f'&& eval {shlex.quote(command)} < /dev/null'],
}
PROVIDER_SHELLS = [('codex', 'zsh -c'), ('codex', 'zsh -lc'),
                   ('claude', 'zsh -c'), ('claude', 'zsh -lc'), ('claude', 'claude snapshot')]
SHELL_WRITES = [
    'gh pr comment 7 --body published',             # the review's executed input
    'gh pr merge 7 --squash', 'gh pr review 7 --approve', 'gh issue comment 1 --body x',
    'gh api -iXPOST repos/o/r/issues/1/comments', 'gh api -ifbody=published repos/o/r/issues/1/comments',
    'cd . && gh pr comment 7 --body published', "zsh -lc 'gh pr comment 7 --body published'",
    'echo "$(gh pr comment 7 --body published)"', 'g=gh; $g pr comment 7 --body published',
]
SHELL_READS = ['gh pr view 7', 'gh pr checks 7', 'gh run view 1 --log-failed', 'gh api repos/o/r/pulls/1']


def provider_hooks(launch, provider):
    """The PreToolUse commands the provider would run before a Bash command."""
    argv = list(launch.argv)
    commands = []
    if provider == 'claude':
        for index, arg in enumerate(argv[:-1]):
            if arg == '--settings':
                for group in json.loads(argv[index + 1]).get('hooks', {}).get('PreToolUse', []):
                    if re.fullmatch(group.get('matcher') or '.*', 'Bash'):
                        commands += [hook['command'] for hook in group['hooks']]
        return commands
    for arg in argv:
        if arg.startswith('hooks='):
            table = tomllib.loads(arg)['hooks']
            state = table.get('state', {})
            for group_index, group in enumerate(table.get('PreToolUse', [])):
                for hook_index, hook in enumerate(group['hooks']):
                    key = f'/<session-flags>/config.toml:pre_tool_use:{group_index}:{hook_index}'
                    if re.fullmatch(group['matcher'], 'Bash') and state.get(key, {}).get('enabled'):
                        commands.append(hook['command'])
    return commands


def hooks_deny(launch, provider, command, env):
    payload = {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': launch.cwd}
    if provider == 'claude':
        payload.update(hook_event_name='PreToolUse', session_id='fixture-session')
    for hook in provider_hooks(launch, provider):
        result = subprocess.run(['/bin/sh', '-c', hook], input=json.dumps(payload), env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        if result.stdout.strip() and json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny':
            return True
    return False


def zsh_environment(tmp_path, launch):
    """The launch environment with startup files that put Homebrew first."""
    brew = tmp_path / 'homebrew' / 'bin'
    brew.mkdir(parents=True)
    log = tmp_path / 'recorded-gh.log'
    (brew / 'gh').write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(log))}\n')
    (brew / 'gh').chmod(0o755)
    zdotdir = tmp_path / 'zdotdir'
    zdotdir.mkdir()
    prepend = f'export PATH={shlex.quote(str(brew))}:$PATH\n'
    (zdotdir / '.zshenv').write_text(prepend)          # ~/.zshenv line 1
    (zdotdir / '.zprofile').write_text(prepend)        # `brew shellenv`, after /etc/zprofile's path_helper
    snapshot = tmp_path / 'snapshot-zsh.sh'
    snapshot.write_text(f'export PATH={shlex.quote(str(brew))}:/usr/bin:/bin:/usr/sbin:/sbin\n')
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    env = {name: value for name, value in {**os.environ, **launch.env_add}.items()
           if not name.startswith(('GH_', 'GITHUB_')) and name != 'PYTHONPATH'}
    env.update(ZDOTDIR=str(zdotdir), HOME=str(home), GH_CONFIG_DIR=str(tmp_path / 'gh-config'))
    return env, log, snapshot


def recorded(log):
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize('provider,shell', PROVIDER_SHELLS, ids=lambda value: str(value))
def test_gh_writes_are_refused_in_each_providers_real_shell(tmp_path, provider, shell):
    launch = protected(tmp_path, provider=provider)
    env, log, snapshot = zsh_environment(tmp_path, launch)
    for command in SHELL_WRITES:
        denied = hooks_deny(launch, provider, command, env)
        assert denied, (provider, shell, command)
        # The harness reproduces the finding: past the hook, this shell's startup
        # resolves gh to the Homebrew-position stand-in, not the PATH wrapper.
        before = len(recorded(log))
        subprocess.run(SHELLS[shell](command, snapshot), cwd=launch.cwd, env=env,
                       capture_output=True, text=True, timeout=30)
        assert len(recorded(log)) > before, (shell, command)
    for command in SHELL_READS:
        denied = hooks_deny(launch, provider, command, env)
        assert not denied, command
        before = len(recorded(log))
        result = subprocess.run(SHELLS[shell](command, snapshot), cwd=launch.cwd, env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert recorded(log)[before:] == [command.removeprefix('gh ')], (shell, command)


@pytest.mark.parametrize('provider', ['codex', 'claude'])
@pytest.mark.parametrize('command', ['git push origin HEAD', 'git -C . push', 'env -u GIT_ALLOW_PROTOCOL git push',
                                     'git -c alias.p=push p origin'])
def test_git_push_is_refused_by_the_hook_as_a_second_layer(tmp_path, provider, command):
    launch = protected(tmp_path, provider=provider)
    env, _log, _snapshot = zsh_environment(tmp_path, launch)
    denied = hooks_deny(launch, provider, command, env)
    assert denied, (provider, command)


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_non_review_launch_has_no_publish_hook(tmp_path, provider):
    launch = protected(tmp_path, provider=provider, task='build')
    env, _log, _snapshot = zsh_environment(tmp_path, launch)
    denied = hooks_deny(launch, provider, 'gh pr comment 7 --body published', env)
    assert not denied, provider


def test_writable_codex_review_without_the_publish_hook_is_refused(tmp_path):
    """Fail closed: a writable Codex review launch never starts without the hook rule."""
    from subfleet.adapters.base import AdapterError
    from subfleet.adapters.codex import CodexAdapter
    work = tmp_path / 'work'
    work.mkdir()
    prompt = tmp_path / 'prompt'
    prompt.write_text('review')
    job = replace(make_job(str(work), str(prompt)), task='review', sandbox=Sandbox.WORKSPACE_WRITE)
    home = str(tmp_path / 'home')
    lane = replace(make_lane(), provider='codex', home=home, credential=Credential('codex', home, 'home'))
    with pytest.raises(AdapterError) as refused:
        CodexAdapter(codex_bin='fake-provider').build_launch(
            job, 'job/a1', tmp_path / 'attempt', lane, {}, 'fixture-model', None, prompt,
            review_override(tmp_path, publish=False))
    assert refused.value.reason == 'codex-publish-hook-required'


@pytest.mark.parametrize('sandbox,task,expected', [
    ('workspace-write', 'review', True), ('read-only', 'review', False), ('workspace-write', 'build', False)])
def test_claude_publish_hook_rides_only_writable_review_launches(tmp_path, sandbox, task, expected):
    launch = protected(tmp_path, provider='claude', sandbox=sandbox, task=task)
    # Every Claude launch passes one `--settings` (d1031); the hook rides in it.
    assert launch.argv.count('--settings') == 1
    settings = json.loads(launch.argv[launch.argv.index('--settings') + 1])
    assert settings['env'] == {'CLAUDE_CODE_RETRY_WATCHDOG': '0'}
    assert ('hooks' in settings) is expected
    if expected:
        [group] = settings['hooks']['PreToolUse']
        assert 'matcher' not in group          # every tool whose input carries a command
        [hook] = group['hooks']
        assert Path(hook['command']).name == 'review-publish-hook' and os.access(hook['command'], os.X_OK)
        assert launch.env_add['SUBFLEET_PUBLISH_DENIED'] == '1'


# #221 review r1b, P3-1: Codex's `shell_environment_policy` documents default
# excludes, `*KEY*`, `*SECRET*` and `*TOKEN*` (case-insensitive), applied when
# `ignore_default_excludes=false`. With GIT_CONFIG_KEY_<n> stripped and
# GIT_CONFIG_COUNT kept, every git command failed (`missing config key
# GIT_CONFIG_KEY_0`, rc 128, `codex sandbox` 0.159). The wrapper re-added the
# variables, which hid it; absolute git, as here, does not go through it.
CODEX_DEFAULT_EXCLUDES = ('*KEY*', '*SECRET*', '*TOKEN*')


def codex_excluded(env):
    import fnmatch
    return {name: value for name, value in env.items()
            if not any(fnmatch.fnmatchcase(name.upper(), pattern) for pattern in CODEX_DEFAULT_EXCLUDES)}


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_git_denial_survives_codex_default_environment_excludes(tmp_path, provider):
    remote = local_remote(tmp_path)
    launch = protected(tmp_path, provider)
    env = codex_excluded({**os.environ, **launch.env_add})
    git = shutil.which('git')
    log = subprocess.run([git, 'log', '-1', '--oneline'], cwd=launch.cwd, env=env, capture_output=True, text=True)
    assert log.returncode == 0, log.stderr
    for shell_env in (env, {name: value for name, value in env.items() if name != 'GIT_ALLOW_PROTOCOL'}):
        # Without the allowlist the push URL rewrite alone still refuses, even with file:// allowed.
        push = subprocess.run([git, '-c', 'protocol.file.allow=always', 'push', remote, 'HEAD:refs/heads/proof'],
                              cwd=launch.cwd, env=shell_env, capture_output=True, text=True)
        assert push.returncode != 0
    assert subprocess.run([git, 'ls-remote', remote], capture_output=True).stdout == b''
    from subfleet.review_publish import GIT_DENIAL
    assert set(GIT_DENIAL) <= set(codex_excluded(GIT_DENIAL)), 'no denial variable may match an exclude'
