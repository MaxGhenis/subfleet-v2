"""#221 review r2, P2 and P3-5 (D-SH1 round 7): the publish rule covers ordinary shell.

Round 6's rule judged a `gh` call only where it could name the command: an
unnamed runner (`arch`, `lockf`, a shell function), a command kept in a
variable, a here-string or process substitution read as a script, and the
commands git runs from a value all reached gh. The review executed each one
through both providers' real command paths; these tests rebuild its inputs the
same way: the PreToolUse hooks a real review launch carries, then (where they
allow) the command through Codex `zsh -c` and `zsh -lc` and Claude `zsh -c`,
`zsh -lc` and its snapshot shape, with a recording gh at the Homebrew position.

gh is always the recording stand-in, git pushes only to a local bare repo, and
no test reads a credential: `SECRET7` values are dummies.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from hypothesis import assume, given, settings, strategies as st
import pytest

from subfleet.publish_rule import command_refusal, denial, gh_read
from tests.unit.test_review_publish_guard import (PROVIDER_SHELLS, SHELLS, hooks_deny, local_remote, protected,
                                                  recorded, zsh_environment)

WRITE = 'gh pr comment 7 --body published'

# Every spelling review r2 saw reach gh past round 6's rule, as it ran them.
# `reach` names the shells where this host's harness reaches the recorder once
# the hooks are skipped (which reproduces the finding): every shell, a subset
# (as measured here: uv reached gh only under `zsh -lc`, and `- gh` only in the
# snapshot shape, as review r2 saw), or None where the runner can't run here
# (`watch` needs a terminal, GNU parallel isn't installed): those are judged by
# the rule and the hooks only, as the review did.
ALL = ('zsh -c', 'zsh -lc', 'claude snapshot')
SPELLINGS = [
    ('function runner', f'r() {{ "$@"; }}; r {WRITE}', ALL),
    ('variable with eval', f"c='{WRITE}'; eval \"$c\"", ALL),
    ('variable with sh -c', f"c='{WRITE}'; sh -c \"$c\"", ALL),
    ('variable split by zsh', f"c='{WRITE}'; ${{=c}}", ALL),
    ('process substitution to bash', f"bash <(echo '{WRITE}')", ALL),
    ('process substitution to source', f"source <(echo '{WRITE}')", ALL),
    ('here-string into bash', f"bash <<< '{WRITE}'", ALL),
    ('xargs into sh -c', f"echo '{WRITE}' | xargs -I{{}} sh -c '{{}}'", ALL),
    ('arch', f'arch -arm64 {WRITE}', ALL),
    ('lockf', f'/usr/bin/lockf -k {{work}}/probe.lock {WRITE}', ALL),
    ('taskpolicy', f'taskpolicy -b {WRITE}', ALL),
    ('env -S', f"env -S '{WRITE}'", ALL),
    ('uv run', f'UV_OFFLINE=1 uv run --no-project {WRITE}', ('zsh -lc',)),
    ('repeat', f'repeat 1 {WRITE}', ALL),
    ('glob command word', '{brew}/g[h] pr comment 7 --body published', ALL),
    ('leading redirection', f'>/dev/null {WRITE}', ALL),
    ('zsh - precommand', f'- {WRITE}', ('claude snapshot',)),
    ('run-time word then a global flag', '$(command -v gh) -R o/r pr comment 7 --body published', ALL),
    ('gh linked under another name', 'ln -sf {brew}/gh ./g && ./g pr comment 7 --body published', ALL),
    ('GIT_EDITOR value', f"GIT_EDITOR='{WRITE}' git commit --allow-empty", ALL),
    ('git alias value', f"git config alias.pc '!{WRITE}' && git pc", ALL),
    ('watch -e', f'watch -n 1 -e {WRITE}', None),
    ('parallel arguments', f"parallel ::: '{WRITE}'", None),
    ('run-time word, cache delete', '$(command -v gh) cache delete --all', ALL),
    ('run-time word, auth token', '$(command -v gh) auth token', ALL),
]
#: Forms of each item of the hub's decision that the review did not list.
RULE_ONLY = [
    f'bash < <(echo "{WRITE}")', f'. <(echo "{WRITE}")', f'zsh =(echo "{WRITE}")',
    f'sh <<<"{WRITE}"', f'bash -s <<< "{WRITE}"', f'source /dev/stdin <<< "{WRITE}"',
    f'c="{WRITE}"; zsh -c "$c"', f'c="{WRITE}"; bash -lc "$c"', 'c=$(printf "%s" "x"); gh=1; eval "$c"',
    f'c=\'{WRITE}\'; "$c"', f'c=\'{WRITE}\'; eval $c', 'c="pr comment 7"; gh ${=c}',
    f'2>&1 {WRITE}', f'</dev/null {WRITE}', f'{{fd}}>/dev/null {WRITE}', f'cat <<< x\n{WRITE}',
    f'env -S"{WRITE}"', f"env --split-string='{WRITE}'", f"env -u X -S '{WRITE}'",
    f'repeat 3 do {WRITE}; done', f'- - {WRITE}', '{gh,} pr comment 7', '/opt/homebrew/bin/g? pr merge 7',
    '/opt/*/bin/gh pr merge 7', '$(command -v gh) --repo o/r pr merge 7', '$(command -v gh) --repo=o/r issue close 1',
    '$(command -v gh) --hostname h api -XPOST repos/o/r/issues', '"$GH" -R o/r pr review 7 --approve',
    'ln -s "$(command -v gh)" ./g', 'cp /opt/homebrew/bin/gh /tmp/g', 'install -m 755 gh ./g', 'mv ./gh ./g',
    'cat /opt/homebrew/bin/gh > ./g', 'dd if=/opt/homebrew/bin/gh of=./g',
    f"GIT_SEQUENCE_EDITOR='{WRITE}' git rebase -i HEAD~1", f"export EDITOR='{WRITE}'",
    f"git -c core.editor='{WRITE}' commit", f"git config --global core.pager '{WRITE}'",
    "git config alias.x '!gh' && git x pr comment 7", f"git -c 'alias.pc=!{WRITE}' pc",
    f'nice -n 5 arch -arm64 {WRITE}', f'caffeinate -i lockf -k f {WRITE}', f'r() {{ "$@"; }}\nr {WRITE}',
    f'function r {{ $@ }}; r {WRITE}', f'docker run --rm img {WRITE}', f'ssh host {WRITE}',
    f"watch '{WRITE}'", f'parallel {WRITE} ::: 1', "parallel 'gh pr comment {}' ::: 7",
    f"echo '{WRITE}' | parallel", f"printf '%s\\n' '{WRITE}' | xargs -0 bash -c",
    f"uv run sh -c '{WRITE}'", f"fd -x sh -c '{WRITE}'", f"find . -exec sh -c '{WRITE}' \\;",
    '$(command -v gh) api -XPOST repos/o/r/issues/1/comments', 'g=gh; $g api --method PATCH repos/o/r',
    'arch -arm64 gh api -f body=x repos/o/r/issues/1/comments', 'taskpolicy -b gh auth token',
]


def fill(template: str, world: dict) -> str:
    return (template.replace('{brew}', str(world['brew'])).replace('{work}', str(world['work']))
            .replace('{remote}', world.get('remote', '')))


def static(template: str) -> str:
    return fill(template, {'brew': '/opt/homebrew/bin', 'work': '/w', 'remote': 'file:///r.git'})


def round7_world(tmp_path, provider):
    """A review launch, its zsh environment, and a committed repository at its cwd."""
    launch = protected(tmp_path, provider=provider)
    env, log, snapshot = zsh_environment(tmp_path, launch)
    git = shutil.which('git')
    brew = tmp_path / 'homebrew' / 'bin'
    # The review's Homebrew-position git sat ahead of the PATH wrapper.
    (brew / 'git').symlink_to(git)
    work = Path(launch.cwd)
    subprocess.run([git, 'init', '-q', str(work)], check=True)
    subprocess.run([git, '-C', str(work), '-c', 'user.name=Fixture', '-c', 'user.email=fake@example.org',
                    'commit', '-q', '--allow-empty', '-m', 'baseline'], check=True)
    env.update(GIT_AUTHOR_NAME='Fixture', GIT_COMMITTER_NAME='Fixture', GIT_AUTHOR_EMAIL='fake@example.org',
               GIT_COMMITTER_EMAIL='fake@example.org', UV_CACHE_DIR=str(tmp_path / 'uv-cache'))
    return launch, env, log, snapshot, {'brew': brew, 'work': work}


def shell_has(shell, env, snapshot, tool):
    found = subprocess.run(SHELLS[shell](f'command -v {tool}', snapshot), env=env, capture_output=True,
                           text=True, timeout=30)
    return found.returncode == 0 and bool(found.stdout.strip())


@pytest.mark.parametrize('name,template,reach', SPELLINGS, ids=[name for name, _, _ in SPELLINGS])
def test_each_spelling_review_r2_saw_reach_gh_is_refused_by_the_rule(name, template, reach):
    assert command_refusal(static(template)), template


@pytest.mark.parametrize('command', RULE_ONLY)
def test_the_decisions_other_forms_are_refused_by_the_rule(command):
    assert command_refusal(static(command)), command


@pytest.mark.parametrize('provider,shell', PROVIDER_SHELLS, ids=lambda value: str(value))
def test_each_spelling_is_refused_in_each_providers_real_shell(tmp_path, provider, shell):
    launch, env, log, snapshot, world = round7_world(tmp_path, provider)
    for name, template, reach in SPELLINGS:
        command = fill(template, world)
        assert hooks_deny(launch, provider, command, env), (provider, shell, name)
        if reach is None or shell not in reach:
            continue
        tool = command.split()[1] if command.startswith('UV_OFFLINE') else None
        if tool and not shell_has(shell, env, snapshot, 'uv'):
            continue
        # The harness reproduces the finding: past the hooks, this shell runs gh.
        before = len(recorded(log))
        # 180 s (#221 review r6, P3-1): a login zsh's `uv run` took over 60 s at load ~124.
        subprocess.run(SHELLS[shell](command, snapshot), cwd=launch.cwd, env=env, capture_output=True,
                       text=True, timeout=180)
        assert len(recorded(log)) > before, (shell, name)


# #221 review r2, P2 item 4: credential exposure is refused on the same path,
# with its own reason and wording. These never ran a real `gh auth token`.
CREDENTIAL = ['gh auth token', 'gh auth token --hostname github.com', 'gh -R o/r auth token', 'gh auth status -t',
              'gh auth status --show-token', 'gh auth status -at', 'gh auth status -h github.com -t',
              'gh pr view 7 --show-token', 'arch -arm64 gh auth token', '$(command -v gh) auth token',
              'taskpolicy -b gh auth status --show-token', 'gh auth status --json hosts']


@pytest.mark.parametrize('command', CREDENTIAL)
def test_credential_calls_are_refused_with_their_own_reason_and_wording(command):
    refused = command_refusal(command)
    assert refused and refused.kind == 'credential', command
    reason = denial(refused)['hookSpecificOutput']['permissionDecisionReason']
    assert f'denied ({refused})' in reason and 'credential' in reason and 'token' in reason
    assert "verdict is its deliverable" not in reason
    publish = denial(command_refusal(WRITE))['hookSpecificOutput']['permissionDecisionReason']
    assert command_refusal(WRITE).kind == 'publish' and "verdict is its deliverable" in publish


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_gh_auth_token_through_the_hook_answers_the_credential_denial(tmp_path, provider):
    """The real hook: a recording gh would have printed `SECRET7…`; the hook denies first."""
    launch = protected(tmp_path, provider=provider)
    env, log, _snapshot = zsh_environment(tmp_path, launch)
    for command in ('gh auth token', 'gh auth status --show-token'):
        assert hooks_deny(launch, provider, command, env), command
    assert hooks_deny(launch, provider, 'gh auth status', env) is False
    assert recorded(log) == []


# P3-5: reads that round 6 refused. Real gh 2.100.0 against a closed localhost
# port (dummy token) sent GET for each: `-X GET -f q=…` and `--method GET
# --raw-field` put the fields in the query string; `-iX GET`, `-X=GET` and
# `-Xget` are GET; `-H…` and `-q…` take the rest of their word as a value.
P3_5_READS = [
    ['api', '-X', 'GET', '-f', 'q=repo:o/r', 'search/issues'], ['api', '--method', 'GET', '-F', 'q=x', 'search/issues'],
    ['api', '--method=GET', '--raw-field', 'q=x', 'search/issues'], ['api', '-X', 'GET', '-fq=x', 'search/issues'],
    ['api', '-iX', 'GET', '-f', 'q=x', 'search/issues'], ['api', '-X=GET', 'repos/o/r'], ['api', '-Xget', 'repos/o/r'],
    ['--version'], ['version'], ['auth', 'status'], ['auth', 'status', '-a'], ['auth', 'status', '--hostname', 'h'],
    ['auth', 'status', '-hgithub.com'], ['api', '-HX-GitHub-Api-Version:2022-11-28', 'repos/o/r'],
    ['api', '-H', 'Accept: application/vnd.github+json', 'repos/o/r'], ['api', '-iHAccept:x', 'repos/o/r'],
    ['api', '-q.foo', 'repos/o/r'], ['api', '-q', '.foo', 'repos/o/r'], ['api', '--jq=.foo', 'repos/o/r'],
    ['api', '--jq', '.[] | .body', 'repos/o/r/pulls'], ['api', '-tx{{.}}', 'repos/o/r'],
]
#: `-f`/`-F` without an explicit GET still means POST (real gh: `-fq=x` sent POST), and the last -X wins.
P3_5_WRITES = [
    ['api', '-fq=x', 'repos/o/r'], ['api', '-F', 'q=x', 'search/issues'], ['api', '--raw-field', 'q=x', 'search/issues'],
    ['api', '-X', 'GET', '-X', 'POST', 'repos/o/r'], ['api', '-XGET', '-iXPOST', 'repos/o/r'],
    ['api', '-X', 'GET', '--input', 'body.json', 'repos/o/r'], ['api', '-X', 'GET', 'graphql', '-f', 'query=x'],
    ['api', '-Z', 'repos/o/r'], ['api', '--unknown', 'repos/o/r'], ['api', '-X'], ['auth', 'status', '--json', 'hosts'],
    ['auth', 'login'], ['--version', 'pr', 'comment', '7'],
]
#: The brief's 8 reads, which reached gh in every shell after round 6 too.
BRIEF_READS = ['gh pr view 7', 'gh pr checks 7', 'gh run view 1 --log-failed', 'gh issue view 7', 'gh pr list',
               'gh api repos/o/r/pulls/1', 'gh api -XGET repos/o/r', 'gh api -X GET repos/o/r']


@pytest.mark.parametrize('args', P3_5_READS, ids=lambda args: ' '.join(args))
def test_p3_5_reads_are_reads(args):
    assert gh_read(args), args
    assert command_refusal(['gh', *args]) is None
    assert command_refusal(shlex.join(['gh', *args])) is None


@pytest.mark.parametrize('args', P3_5_WRITES, ids=lambda args: ' '.join(args))
def test_p3_5_fields_without_get_still_write(args):
    assert not gh_read(args), args
    assert command_refusal(shlex.join(['gh', *args]))


@pytest.mark.parametrize('provider,shell', PROVIDER_SHELLS, ids=lambda value: str(value))
def test_reads_reach_gh_in_each_providers_real_shell(tmp_path, provider, shell):
    launch, env, log, snapshot, _world = round7_world(tmp_path, provider)
    for command in BRIEF_READS + [shlex.join(['gh', *args]) for args in P3_5_READS]:
        assert not hooks_deny(launch, provider, command, env), (provider, command)
        before = len(recorded(log))
        result = subprocess.run(SHELLS[shell](command, snapshot), cwd=launch.cwd, env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (command, result.stderr)
        assert len(recorded(log)) == before + 1, (shell, command)


# Over-refusal: ordinary review commands, and words that only look like gh.
CORPUS = [
    'git log --oneline -20', 'git log -p -3 -- subfleet/publish_rule.py', 'git diff release/217...HEAD --stat',
    'git diff HEAD~1 -- docs/', 'git show HEAD:subfleet/daemon.py | sed -n 1,40p', 'git show --stat HEAD',
    'git status --porcelain', 'git blame -L 300,340 subfleet/publish_rule.py', 'git rev-parse HEAD',
    'git branch --show-current', "git log --all --grep='D-SH1' --oneline", 'git ls-files | wc -l',
    "rg -n 'gh pr comment' subfleet tests", 'rg -n "def command_refusal" subfleet/', 'rg --files | head',
    'grep -rn GIT_ALLOW_PROTOCOL subfleet', 'grep gh file', 'ls ghost', 'echo gh', 'cat README.md | head -50',
    "sed -n '330,420p' subfleet/publish_rule.py", 'head -100 docs/acceptance-contract.md', 'wc -l subfleet/*.py',
    "find tests -name 'test_publish*' -print", "find . -name '*.py' -newer pyproject.toml | head",
    'uv run pytest -q tests/unit/test_publish_rule_round7.py', "uv run pytest -q -k 'publish and not slow' tests/unit",
    'uv run --frozen pytest tests/unit/test_hooks.py -x -q 2>&1 | tail -20',
    'python3 -m pytest -q tests/unit/test_review_publish_guard.py', 'UV_CACHE_DIR=/tmp/uv uv sync --frozen',
    "uv run python -c 'import subfleet; print(subfleet.__version__)'", 'gh pr view 221',
    'gh pr view 221 --json title,body,headRefOid -q .headRefOid', 'gh pr checks 221', 'gh pr diff 221 | head -200',
    'gh pr diff 221 --name-only', 'gh run view 123456 --log-failed', 'gh run list --branch fix/x --limit 5',
    'gh api repos/MaxGhenis/subfleet-v2/pulls/221', "gh api repos/o/r/pulls/221/comments --paginate -q '.[].body'",
    "gh api -X GET search/issues -f q='repo:o/r is:pr'", "gh api repos/o/r/commits/abc/check-runs -q '.check_runs[]'",
    'gh issue view 7 --comments', 'gh pr list --state open --author @me', 'cd subfleet && git log --oneline -5',
    'for f in subfleet/publish_rule.py subfleet/review_publish.py; do wc -l "$f"; done',
    'git diff --name-only release/217 | xargs wc -l', "jq '.hooks' launch.json",
    'diff <(git show HEAD~1:subfleet/publish_rule.py) subfleet/publish_rule.py | head', '[ -f pyproject.toml ] && echo yes',
    'GH_PAGER=cat gh pr view 221 --comments', 'command -v gh', 'which gh git', 'gh run view 123 --log | grep -n FAIL',
    '"$VIRTUAL_ENV/bin/python" -m pytest -q', '$(git rev-parse --show-toplevel)/bin/check && gh pr checks 7',
    'git log --grep gh --oneline', "git grep -n -e 'gh pr comment' -- subfleet", "sed -e 's/gh pr comment/X/' f",
    "grep -e 'gh pr comment' -e 'git push' tests/x.py", "pytest -k 'gh and not comment'", 'ls gh/ ghost/ my-gh',
    "python3 -c 'print(\"gh pr comment\")'", 'cat <<EOF\ngh pr comment 7\nEOF', "printf '%s\\n' 'gh pr comment 7'",
    'git log --format="%h -> %s" -5', 'gh --version', 'gh auth status', 'echo $((1 + 2)) gh',
]


@pytest.mark.parametrize('command', CORPUS)
def test_ordinary_review_commands_pass_the_rule(command):
    assert command_refusal(command) is None, command


def test_ordinary_review_commands_pass_the_real_hooks(tmp_path):
    for provider in ('codex', 'claude'):
        (tmp_path / provider).mkdir()
        launch = protected(tmp_path / provider, provider=provider)
        env, _log, _snapshot = zsh_environment(tmp_path / provider, launch)
        for command in CORPUS:
            assert not hooks_deny(launch, provider, command, env), (provider, command)


# The hub asked that `git push` stay refused in every new form, as round 2
# checked with its 10 forms against a local bare repo. The rule now refuses
# all but the two it cannot see (another language, a script on disk); the
# git environment refuses every one of the 10 once the hooks are skipped.
PUSH = 'git push {remote} HEAD:refs/heads/probe'
GIT_PUSH_FORMS = [
    ('function runner', f'r() {{ "$@"; }}; r {PUSH}', True),
    ('here-string', f"bash <<< '{PUSH}'", True),
    ('process substitution', f"bash <(echo '{PUSH}')", True),
    ('arch', f'arch -arm64 {PUSH}', True),
    ('brace command word', '{git,} push {remote} HEAD:refs/heads/probe', True),
    ('leading redirection', f'>/dev/null {PUSH}', True),
    ('env -S', f"env -S '{PUSH}'", True),
    ('variable with eval', f"c='{PUSH}'; eval \"$c\"", True),
    ('python', "python3 -c 'import subprocess,sys; sys.exit(subprocess.run([\"git\", \"push\", \"{remote}\", "
               "\"HEAD:refs/heads/probe\"]).returncode)'", False),
    ('script on disk', f"printf '%s\\n' '{PUSH}' > p.sh; sh p.sh", False),
]


@pytest.mark.parametrize('provider,shell', PROVIDER_SHELLS, ids=lambda value: str(value))
def test_git_push_stays_refused_in_every_new_form(tmp_path, provider, shell):
    remote = local_remote(tmp_path)
    (tmp_path / 'world').mkdir()
    launch, env, _log, snapshot, world = round7_world(tmp_path / 'world', provider)
    world['remote'] = remote
    git = shutil.which('git')
    subprocess.run([git, '-C', launch.cwd, 'commit', '-q', '--allow-empty', '-m', 'second'], check=True, env=env)
    for name, template, hook_sees in GIT_PUSH_FORMS:
        command = fill(template, world)
        assert hooks_deny(launch, provider, command, env) is hook_sees, (provider, name)
        # 120 s: under load 150 to 250 a `zsh -lc` push form took over 60 s (#221 review r3c P3).
        subprocess.run(SHELLS[shell](command, snapshot), cwd=launch.cwd, env=env, capture_output=True,
                       text=True, timeout=120)
        landed = subprocess.run([git, 'ls-remote', remote], capture_output=True, text=True).stdout
        assert landed == '', (shell, name, landed)
    # Review r2's one push: the environment discarded and the hook routed
    # around by a function runner. The rule now sees the git word.
    command = fill(f'unset GIT_ALLOW_PROTOCOL GIT_CONFIG_PARAMETERS; r() {{ "$@"; }}; r {PUSH}', world)
    assert hooks_deny(launch, provider, command, env), provider


# Property: any gh write embedded through one of the covered forms is refused,
# and the rule never raises on arbitrary text. Bounded and derandomized.
GH_WRITES = [WRITE, 'gh pr merge 7 --squash', 'gh issue close 1', 'gh api -XPOST repos/o/r/issues/1/comments',
             'gh api -f body=x repos/o/r/issues/1/comments', 'gh release create v1', 'gh auth token',
             'gh -R o/r pr review 7 --approve', 'gh repo delete o/r --yes', 'gh workflow run x.yml']
FORMS = [
    '{w}', 'r() {{ "$@"; }}; r {w}', "c='{w}'; eval \"$c\"", "c='{w}'; sh -c \"$c\"", "c='{w}'; ${{=c}}",
    "c='{w}'; bash -c \"$c\"", "bash <(echo '{w}')", "source <(echo '{w}')", ". <(echo '{w}')", "bash <<< '{w}'",
    "zsh <<<'{w}'", "echo '{w}' | xargs -I{{}} sh -c '{{}}'", 'arch -arm64 {w}', 'lockf -k /tmp/l {w}',
    'taskpolicy -b {w}', "env -S '{w}'", 'uv run --no-project {w}', 'repeat 2 {w}', '>/dev/null {w}', '2>&1 {w}',
    '- {w}', 'watch -n 1 {w}', "watch '{w}'", "parallel ::: '{w}'", "GIT_EDITOR='{w}' git commit",
    "git config alias.pc '!{w}' && git pc", "git -c core.editor='{w}' commit", 'nohup {w}', 'timeout 5 {w}',
    'env X=1 {w}', 'command {w}', 'exec {w}', 'sudo -u x {w}', "sh -c '{w}'", "zsh -lc '{w}'",
    'echo "$({w})"', 'cd /tmp && {w}', '{w} | cat', 'true; {w}', 'if true; then {w}; fi',
]
GLOBBED = ['{w}', '/opt/homebrew/bin/g[h]{rest}', '{{gh,}}{rest}', '$(command -v gh) -R o/r{rest}',
           '$(command -v gh){rest}', 'g=gh; $g{rest}', '"$GH" --repo o/r{rest}']


@settings(max_examples=400, derandomize=True, database=None, deadline=None)
@given(write=st.sampled_from(GH_WRITES), outer=st.sampled_from(FORMS), inner=st.sampled_from(FORMS),
       word=st.sampled_from(GLOBBED))
def test_any_gh_write_through_a_covered_form_is_refused(write, outer, inner, word):
    rest = write[len('gh'):]
    spelled = word.format(w=write, rest=rest) if '{w}' not in word else write
    nested = inner.format(w=spelled)
    # `env -S` splits its value into words and runs them: it expands no
    # substitution, glob, `$VAR` or shell syntax, so a run-time word or a
    # compound command inside it never runs gh. Those draws embed no write.
    assume('env -S' not in inner + outer or spelled == write)
    assert command_refusal(outer.format(w=spelled)), outer.format(w=spelled)
    if "'" not in nested and ('env -S' not in outer or nested == write):
        assert command_refusal(outer.format(w=nested)), outer.format(w=nested)
    assert command_refusal(nested), nested


@settings(max_examples=400, derandomize=True, database=None, deadline=None)
@given(st.text(alphabet=st.sampled_from(list('gh pr comment$(){}<>|&;\'"`\\=!-/[]*?.\n\t ce')), max_size=120))
def test_the_rule_never_raises_on_arbitrary_text(text):
    command_refusal(text)
    command_refusal(text.split())
    json.dumps(denial(command_refusal(text) or 'x'))


def test_the_rule_never_raises_on_deep_nesting():
    # Past MAX_DEPTH the text is judged by what it names, so a write stays refused.
    command = 'echo "$(' * 1500 + WRITE + ')"' * 1500
    assert len(command) > 9000 and command_refusal(command)
    assert command_refusal("bash -c 'bash -c \"zsh -c \\\"" + WRITE + "\\\"\"'")
    assert command_refusal('$(' * 3000 + ')' * 3000) is None
    assert command_refusal(f"r() {{ \"$@\"; }}; r {'x ' * 3000}") is None



# P3-3: the git denial and the marker are pinned in every review launch:
# Codex in `shell_environment_policy.set` (applied after the lane's inherit
# and excludes), Claude in its launch environment.
def codex_pins(argv):
    import tomllib
    argv = list(argv)
    pins = [argv[at + 1] for at, arg in enumerate(argv[:-1])
            if arg == '-c' and argv[at + 1].startswith('shell_environment_policy.set.')]
    return pins, {pin.split('=', 1)[0].rsplit('.', 1)[1]: tomllib.loads('v=' + pin.split('=', 1)[1])['v']
                  for pin in pins}


@pytest.mark.parametrize('kind', ['dispatch', 'resume'])
@pytest.mark.parametrize('sandbox', ['read-only', 'workspace-write'])
def test_every_codex_review_launch_pins_the_git_denial_in_its_shell_environment(tmp_path, kind, sandbox):
    from subfleet.review_publish import GIT_DENIAL
    launch = protected(tmp_path, provider='codex', sandbox=sandbox, kind=kind)
    _pins, values = codex_pins(launch.argv)
    assert values == {**GIT_DENIAL, 'SUBFLEET_PUBLISH_DENIED': '1'}
    assert {name: launch.env_add[name] for name in values} == values


def test_claude_review_launches_and_builds_pin_as_before(tmp_path):
    from subfleet.review_publish import GIT_DENIAL
    claude = protected(tmp_path, provider='claude')
    assert {name: claude.env_add[name] for name in (*GIT_DENIAL, 'SUBFLEET_PUBLISH_DENIED')} == {
        **GIT_DENIAL, 'SUBFLEET_PUBLISH_DENIED': '1'}
    (tmp_path / 'build').mkdir()
    build = protected(tmp_path / 'build', provider='codex', task='build')
    assert codex_pins(build.argv) == ([], {}) and 'GIT_ALLOW_PROTOCOL' not in build.env_add


CODEX = shutil.which('codex')


@pytest.mark.skipif(not CODEX, reason='the real Codex CLI is not installed here')
def test_real_codex_sandbox_keeps_the_git_denial_under_inherit_core(tmp_path):
    """Review r2's P3-3 case, with real `codex sandbox` 0.159.0 and a scratch
    CODEX_HOME: under a lane's `shell_environment_policy.inherit="core"` the
    three variables were absent and `git push` to a local bare repo landed.
    With the launch's own `-c` pins the push is refused."""
    version = subprocess.run([CODEX, '--version'], capture_output=True, text=True, timeout=60).stdout
    if '0.159' not in version:
        pytest.skip(f'this case was measured on codex-cli 0.159; installed: {version.strip()}')
    launch = protected(tmp_path, provider='codex')
    pins, _values = codex_pins(launch.argv)
    git = shutil.which('git')
    work = Path(launch.cwd)
    subprocess.run([git, 'init', '-q', str(work)], check=True)
    subprocess.run([git, '-C', str(work), '-c', 'user.name=Fixture', '-c', 'user.email=fake@example.org',
                    'commit', '-q', '--allow-empty', '-m', 'baseline'], check=True)
    bare = work / 'remote.git'
    subprocess.run([git, 'init', '-q', '--bare', str(bare)], check=True)
    (tmp_path / 'codex-home').mkdir()
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'CODEX_HOME': str(tmp_path / 'codex-home'),
           **{name: value for name, value in launch.env_add.items() if name not in ('PATH', 'CODEX_HOME')}}
    base = [CODEX, 'sandbox', '-c', 'sandbox_mode="workspace-write"', '-c', 'shell_environment_policy.inherit="core"']
    push = ['--', '/bin/sh', '-c', f'{shlex.quote(git)} push {shlex.quote(str(bare))} HEAD:refs/heads/probe']

    def landed():
        return subprocess.run([git, 'ls-remote', str(bare)], capture_output=True, text=True).stdout

    subprocess.run(base + push, cwd=work, env=env, capture_output=True, text=True, timeout=120)
    assert 'refs/heads/probe' in landed(), 'the harness reproduces review r2: the policy dropped the denial'
    subprocess.run([git, '-C', str(bare), 'update-ref', '-d', 'refs/heads/probe'], check=True)
    pinned = subprocess.run(base + [word for pin in pins for word in ('-c', pin)] + push, cwd=work, env=env,
                            capture_output=True, text=True, timeout=120)
    # Either layer of the denial: the push URL rewrite's transport, or the empty allowlist.
    assert re.search(r"transport '(subfleet-publish-denied|file)' not allowed", pinned.stderr), pinned.stderr[-2000:]
    assert landed() == ''


# P3-6: a writable Claude review loads only the user's settings and the hook.
@pytest.mark.parametrize('kind', ['dispatch', 'resume'])
def test_writable_claude_review_loads_only_user_settings_and_the_publish_hook(tmp_path, kind):
    launch = protected(tmp_path, provider='claude', kind=kind)
    argv = list(launch.argv)
    at = argv.index('--settings')
    assert argv[at - 2:at] == ['--setting-sources', 'user'] and argv.count('--setting-sources') == 1
    for name, changes in (('ro', {'sandbox': 'read-only'}), ('build', {'task': 'build'})):
        (tmp_path / name).mkdir()
        other = list(protected(tmp_path / name, provider='claude', kind=kind, **changes).argv)
        assert other.count('--settings') == 1                       # the lane settings alone (d1031)
        assert 'hooks' not in json.loads(other[other.index('--settings') + 1])
        assert other[other.index('--setting-sources') + 1] == '' if name == 'ro' else '--setting-sources' not in other


# P3-7: the launch self-test, on the hook `install_hook` writes and on broken ones.
def self_test_env(tmp_path):
    return {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'SUBFLEET_PUBLISH_DENIED': '1'}


def test_the_self_test_passes_the_installed_hook_and_caches_the_pass(tmp_path, monkeypatch):
    import sys
    from subfleet import review_publish
    path = review_publish.install_hook(tmp_path / 'hook')
    command = review_publish.hook_command(path)
    package = Path(review_publish.__file__).resolve().parent
    assert review_publish.hook_files(command) == [str(path), sys.executable, str(package / '__init__.py'),
                                                  str(package / 'publish_rule.py')]
    cache: dict = {}
    first = review_publish.hook_self_test(command, self_test_env(tmp_path), cwd=str(tmp_path), cache=cache)
    assert first.ok and not first.cached and first.elapsed_s > 0

    def runs_nothing(*args, **kwargs):
        raise AssertionError('a cached pass runs nothing')

    monkeypatch.setattr(review_publish.subprocess, 'run', runs_nothing)
    second = review_publish.hook_self_test(command, self_test_env(tmp_path), cwd=str(tmp_path), cache=cache)
    assert second.ok and second.cached
    monkeypatch.undo()
    # The same command over changed bytes is a new key: it runs again, and fails.
    path.chmod(0o755)
    path.write_bytes(b'#!/bin/sh\ncat >/dev/null\nexit 0\n')
    third = review_publish.hook_self_test(command, self_test_env(tmp_path), cwd=str(tmp_path), cache=cache)
    assert not third.ok and not third.cached and third.detail == 'exit 0, no deny'


def test_a_same_size_rewrite_with_its_mtime_put_back_is_not_a_cached_pass(tmp_path):
    """C-12.4 (#221 review r3 P3): the digest cache keys on ctime too, which `utime` cannot set."""
    import os
    from subfleet import review_publish
    path = review_publish.install_hook(tmp_path / 'hook')
    command = review_publish.hook_command(path)
    files = review_publish.hook_files(command)
    cache: dict = {}
    assert review_publish.hook_self_test(command, self_test_env(tmp_path), cwd=str(tmp_path), cache=cache).ok
    before, original = os.stat(path), path.read_bytes()
    # Keep the first three lines, so the hook names the same files, and the size;
    # what follows reads the payload and allows everything.
    head = b'\n'.join(original.split(b'\n')[:3]) + b'\n'
    body = b'import sys\nsys.stdin.read()\nraise SystemExit(0)\n'
    padding = len(original) - len(head) - len(body) - 1
    assert padding >= 0
    rewritten = head + body + b'#' * padding + b'\n'
    assert len(rewritten) == len(original) and rewritten != original
    path.chmod(0o755)                                # install_hook leaves it read-only (0o555)
    with open(path, 'r+b') as stream:                # in place: the same inode
        stream.write(rewritten)
        stream.truncate()
    path.chmod(before.st_mode & 0o777)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(path)
    assert (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode) == \
        (before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode)
    assert review_publish.hook_files(command) == files
    verdict = review_publish.hook_self_test(command, self_test_env(tmp_path), cwd=str(tmp_path), cache=cache)
    assert not verdict.ok and not verdict.cached and verdict.detail == 'exit 0, no deny'


@pytest.mark.parametrize('script,detail', [
    (b'#!/bin/sh\ncat >/dev/null\nexit 0\n', 'exit 0, no deny'),
    (b'#!/bin/sh\necho \'{"hookSpecificOutput": {"permissionDecision": "allow"}}\'\n', 'exit 0, no deny'),
    (b'#!/bin/sh\necho not-json\n', 'exit 0, no deny'),
    (b'#!/bin/sh\nexit 2\n', 'exit 2, failed'),
    (b'#!/nonexistent/python3 -I\n', 'exit 126, failed'),
], ids=['silent', 'allow', 'garbage', 'exit-2', 'missing-interpreter'])
def test_the_self_test_refuses_a_hook_that_does_not_deny(tmp_path, script, detail):
    from subfleet import review_publish
    path = tmp_path / 'hook'
    path.write_bytes(script)
    path.chmod(0o755)
    cache: dict = {}
    verdict = review_publish.hook_self_test(review_publish.hook_command(path), self_test_env(tmp_path),
                                            cwd=str(tmp_path), cache=cache)
    assert not verdict.ok and verdict.detail.split(',')[1] == detail.split(',')[1] and cache == {}


def test_the_self_test_refuses_a_hook_that_hangs(tmp_path):
    from subfleet import review_publish
    path = tmp_path / 'hook'
    path.write_bytes(b'#!/bin/sh\nexec sleep 2\n')
    path.chmod(0o755)
    verdict = review_publish.hook_self_test(review_publish.hook_command(path), self_test_env(tmp_path),
                                            cwd=str(tmp_path), timeout_s=0.5)
    assert not verdict.ok and verdict.detail == 'TimeoutExpired'


def test_installed_publish_hooks_reads_both_providers_launches(tmp_path):
    from subfleet.review_publish import installed_publish_hooks
    claude = protected(tmp_path, provider='claude')
    [hook] = installed_publish_hooks(claude.argv)
    assert hook.endswith('/publish-bin/review-publish-hook')
    (tmp_path / 'codex').mkdir()
    codex = protected(tmp_path / 'codex', provider='codex')
    [hook] = installed_publish_hooks(codex.argv)          # the guard's own group is not the publish hook
    assert hook.endswith('/publish-guard/review-publish-hook')
    (tmp_path / 'build').mkdir()
    assert installed_publish_hooks(protected(tmp_path / 'build', provider='claude', task='build').argv) == []
