"""#221 review r3c (D-SH1 round 9): one judgment decides gh on every path.

Round 8's fallback and run-time paths refused only the gh verbs they could
name, so three things gh itself accepts passed them: its built-in aliases (`pr
new`, `cs`), a flag before the verb (`gh pr -R o/r comment`), and the hidden
`gh auth git-credential`. Now, once a call reaches one of gh's commands as
gh's own parser finds it, every path refuses it unless `gh_read` accepts it.

The invariants, for every argument list `a` given to gh:
1. One judgment: wherever `a` reaches a gh command (or reads, or prints the
   credential), the direct path, the known wrappers, the fallback and the
   run-time words give the same verdict, and it is `gh_read`'s.
2. Fail closed: such an `a` that `gh_read` doesn't accept is refused on every path.
3. Reads pass: every call in `GH_READS`, with `-R`/`--repo` before the command,
   before the verb or after it, is allowed on every path.
4. Credentials: `auth token`, `auth git-credential` and `auth status -t` or
   `--show-token` are refused everywhere with the `credential` wording.

gh is always the recording stand-in, except offline `--help` reads of real gh
2.100.0 with a scratch config and no credential; no test makes a network call.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from hypothesis import given, settings, strategies as st
import pytest

from subfleet.publish_rule import (GH_COMMAND_ALIASES, GH_READS, GH_SUBCOMMANDS, _gh_path, command_refusal,
                                   denial, gh_credential, gh_read)
from tests.unit.test_publish_rule_round7 import fill, round7_world
from tests.unit.test_review_publish_guard import PROVIDER_SHELLS, SHELLS, hooks_deny, protected, provider_hooks, \
    recorded, run, zsh_environment

# Review r3c's executed table and its sweep table, each input as it ran.
R3C_TABLE = [
    'arch -arm64 gh pr new --title t --body b',
    '$(command -v gh) pr new --title t --body b',
    '{g,}h pr new -t t -b b',
    'arch -arm64 gh pr -R o/r comment 7 --body x',
    '$(command -v gh) pr -R o/r comment 7 --body x',
    'gh pr new --title t --body b',
    '{brew}/g? pr -R o/r comment 7 --body x',
    'g=gh; $g pr -R o/r comment 7 --body x',
    '$(command -v gh) cs delete --all',
    'arch -arm64 gh pr --repo=o/r comment 7 --body x',
    'arch -arm64 gh pr comment 7 -b x',
]
R3C_CREDENTIAL = ['arch -arm64 gh auth git-credential get', 'gh auth git-credential get']
#: The aliases review r3c read from gh 2.100.0's offline `--help`, as `<command> <alias>`.
R3C_ALIASES = ['pr new', 'issue new', 'release new', 'repo new', 'gist new', 'secret remove', 'variable remove',
               'extension uninstall', 'skill add', 'cs delete', 'ext remove', 'rs list', 'at verify',
               'agent create', 'skills install']
#: Aliases a full read of every command's and subcommand's `--help` adds (2026-10-22), and
#: the hidden commands found by name. `co` is the alias gh writes into a new config.
MORE_ALIASES = ['agents create', 'agent-tasks create', 'extensions install', 'skill show', 'pr co', 'co 7',
                'cs ls', 'repo credits', 'repo garden', 'credits', 'help pr']
#: Real gh 2.100.0, offline: each runs the verb after a flag that comes before it.
FLAG_FIRST = ['--body x pr comment 7', '-b x pr comment 7', 'pr --body x comment 7', 'pr -b x comment 7',
              '--method POST api repos/o/r', '-X POST api repos/o/r', 'issue -R o/r new', 'pr -R o/r -R o/r merge 7',
              'pr -Rx --repo=y merge 7', '--repo o/r cs delete --all', '- pr comment 7 -b x', '"" pr comment 7 -b x']
#: How each finding's form is spelled on each path (`{a}` is the argument list, quoted).
DIRECT = ['gh {a}', '/opt/homebrew/bin/gh {a}', '=gh {a}', 'env gh {a}', 'env X=1 gh {a}', 'command gh {a}',
          'nohup gh {a}', 'sudo -u x gh {a}', 'timeout 5 gh {a}', 'nice -n 5 gh {a}', 'exec gh {a}', '- gh {a}',
          'noglob gh {a}', '>/dev/null gh {a}', 'cd . && gh {a}', 'true; gh {a}']
FALLBACK = ['arch -arm64 gh {a}', '/usr/bin/lockf -k /tmp/l gh {a}', 'taskpolicy -b gh {a}',
            'uv run --no-project gh {a}', 'caffeinate -i lockf -k f gh {a}', 'arch -arm64 /opt/homebrew/bin/gh {a}',
            'ssh host gh {a}', 'docker run --rm img gh {a}']
RUNTIME = ['$(command -v gh) {a}', '"$(command -v gh)" {a}', '`command -v gh` {a}', '"$GH" {a}', '${GH:-gh} {a}',
           '{gh,} {a}', '{g,}h {a}', '/opt/homebrew/bin/g[h] {a}', '/opt/homebrew/bin/g? {a}', 'g=gh; $g {a}',
           'arch -arm64 $(command -v gh) {a}', 'taskpolicy -b "$GH" {a}']
SPELLINGS = DIRECT + FALLBACK + RUNTIME


def spell(template: str, args) -> str:
    return template.replace('{a}', shlex.join(args))


def verdict(command):
    refused = command_refusal(command)
    return None if refused is None else refused.kind


def expected(args):
    return None if gh_read(args) else ('credential' if gh_credential(args) else 'publish')


def reaches_a_command(args) -> bool:
    """Whether gh's parser would reach one of its commands: the first word not a flag or a flag's value."""
    path, _ = _gh_path(args)
    return bool(path) and path[0] in GH_SUBCOMMANDS


@pytest.mark.parametrize('template', R3C_TABLE)
def test_each_input_review_r3c_executed_is_refused(template):
    command = fill(template, {'brew': '/opt/homebrew/bin', 'work': '/w'})
    refused = command_refusal(command)
    assert refused and refused.kind == 'publish', command


@pytest.mark.parametrize('command', R3C_CREDENTIAL)
def test_gh_auth_git_credential_is_refused_with_the_credential_wording(command):
    refused = command_refusal(command)
    assert refused and refused.kind == 'credential', command
    reason = denial(refused)['hookSpecificOutput']['permissionDecisionReason']
    assert 'git-credential' in reason and 'credential' in reason and "verdict is its deliverable" not in reason


@pytest.mark.parametrize('call', R3C_ALIASES + MORE_ALIASES)
@pytest.mark.parametrize('template', SPELLINGS)
def test_gh_aliases_and_hidden_verbs_are_refused_on_every_path(template, call):
    command = spell(template, call.split())
    assert verdict(command) == 'publish', command


@pytest.mark.parametrize('call', FLAG_FIRST)
@pytest.mark.parametrize('template', SPELLINGS)
def test_a_flag_before_the_verb_is_refused_on_every_path(template, call):
    command = spell(template, shlex.split(call))
    assert verdict(command) == 'publish', command


# P3: gh reads `gh pr -R o/r view 7` as `pr view`. So are the other global-flag positions gh accepts.
READ_FORMS = ['pr -R o/r view 7', 'pr --repo o/r view 7', 'pr --repo=o/r view 7', 'pr -Ro/r view 7',
              '-R o/r pr view 7', 'pr -R o/r -R o/r view', 'pr -R o/r checks 7', 'issue --repo o/r view 1',
              'run -R o/r view 1 --log-failed', 'pr ls', 'pr -R o/r ls', 'issue ls', 'run ls', 'workflow ls',
              'release ls', 'label ls', '-R o/r label ls', 'search -R o/r prs q']


@pytest.mark.parametrize('call', READ_FORMS)
@pytest.mark.parametrize('template', SPELLINGS)
def test_reads_with_a_flag_before_the_verb_pass_on_every_path(template, call):
    args = call.split()
    assert gh_read(args), args
    command = spell(template, args)
    assert command_refusal(command) is None, command


def test_ls_is_a_read_wherever_gh_has_it_for_list():
    for name, verbs in GH_READS.items():
        assert ('ls' in verbs) == ('list' in verbs), name
        assert 'ls' not in verbs or 'ls' in GH_SUBCOMMANDS[name], name


def test_every_alias_is_in_gh_subcommands_and_each_command_alias_has_its_commands_verbs():
    for alias, name in GH_COMMAND_ALIASES.items():
        assert GH_SUBCOMMANDS[alias] == GH_SUBCOMMANDS[name], alias
    for call in R3C_ALIASES + MORE_ALIASES:
        command, *rest = call.split()
        assert command in GH_SUBCOMMANDS, call
        if rest and GH_SUBCOMMANDS[command]:
            assert rest[0] in GH_SUBCOMMANDS[command], call


# Invariant 4: credentials, whatever flags come before the verb (real gh 2.100.0
# ran `auth token` for `gh auth -h h token`, `gh auth -u x token` and `gh auth
# --hostname h token`; gh's `-h` is `--hostname`, never help).
CREDENTIAL_CALLS = ['auth token', 'auth git-credential get', 'auth git-credential', 'auth git-credential store',
                    'auth status -t', 'auth status --show-token', 'auth status -at', 'auth status -h h -t',
                    'auth -h h token', 'auth -u x token', 'auth --hostname h token', 'auth --user=x token',
                    'auth --hostname h git-credential get', '-R o/r auth token', 'pr view 7 --show-token',
                    'auth --hostname h status -t', '--help auth token', 'auth token --help']


@pytest.mark.parametrize('call', CREDENTIAL_CALLS)
@pytest.mark.parametrize('template', SPELLINGS)
def test_credential_calls_are_refused_everywhere_with_the_credential_wording(template, call):
    command = spell(template, call.split())
    assert verdict(command) == 'credential', command


# Invariants 1 to 3 over gh's whole command tree. Bounded and derandomized.
COMMANDS = sorted(GH_SUBCOMMANDS)
UNKNOWN = ['frobnicate', 'pr2', 'new2', 'view2']
GLOBALS = [['-R', 'o/r'], ['--repo', 'o/r'], ['--repo=o/r'], ['-Ro/r'], ['--hostname', 'h'], ['--hostname=h'],
           ['-R', 'localhost:9/o/r']]
#: Flags gh's verbs define, which gh also accepts before the verb, and flags gh reads as switches.
VERB_FLAGS = [['--body', 'x'], ['-b', 'x'], ['--web'], ['--json', 'title'], ['-X', 'POST'], ['--method', 'GET'],
              ['-f', 'q=x'], ['-t'], ['--show-token'], ['-h', 'h'], ['-u', 'x'], ['--help'], ['--version'], ['--']]
OPERANDS = ['7', 'x', 'v1', 'repos/o/r', 'get', '--comments', '-L', '5']


@st.composite
def gh_argument_lists(draw):
    command = draw(st.sampled_from(COMMANDS + UNKNOWN))
    verbs = sorted(GH_SUBCOMMANDS.get(command, frozenset()) | GH_READS.get(command, frozenset()))
    words = [command]
    if verbs or draw(st.booleans()):
        words.append(draw(st.sampled_from(verbs + UNKNOWN)))
    words += draw(st.lists(st.sampled_from(OPERANDS), max_size=3))
    for flag in draw(st.lists(st.sampled_from(GLOBALS + VERB_FLAGS), max_size=3)):
        at = draw(st.integers(0, len(words)))
        words[at:at] = flag
    return words


#: `-R`/`--repo`, which gh accepts before a read's command, before its verb and after it (`--hostname` it refuses there).
READ_GLOBALS = [['-R', 'o/r'], ['--repo', 'o/r'], ['--repo=o/r'], ['-Ro/r'], ['-R', 'localhost:9/o/r']]


@st.composite
def gh_reads(draw):
    command = draw(st.sampled_from(sorted(GH_READS)))
    units = [[command], [draw(st.sampled_from(sorted(GH_READS[command])))],
             *draw(st.lists(st.sampled_from([['7'], ['x'], ['--comments'], ['--web'], ['-L', '5'], ['--json', 'title']]),
                            max_size=3))]
    # Between any two words that are not a flag and its value: before the command, before the verb, or after it.
    for flag in draw(st.lists(st.sampled_from(READ_GLOBALS), max_size=3)):
        units.insert(draw(st.integers(0, len(units))), flag)
    return [word for unit in units for word in unit]


PROPERTY = settings(max_examples=300, derandomize=True, database=None, deadline=None)


@PROPERTY
@given(args=gh_argument_lists(), template=st.sampled_from(SPELLINGS))
def test_one_judgment_decides_gh_on_every_path(args, template):
    """Invariant 1: where `args` reaches a gh command, reads, or prints the credential."""
    if not (reaches_a_command(args) or gh_read(args) or gh_credential(args)):
        return
    want = expected(args)
    assert verdict(spell(template, args)) == want, (template, args)
    assert verdict(['gh', *args]) == want and verdict(['arch', '-arm64', 'gh', *args]) == want, args


@PROPERTY
@given(args=gh_argument_lists(), template=st.sampled_from(SPELLINGS))
def test_every_gh_call_gh_read_does_not_accept_is_refused(args, template):
    """Invariant 2: fail closed. A first word gh doesn't know as a command is refused on the direct path."""
    if gh_read(args):
        return
    assert command_refusal(spell('gh {a}', args)), args
    if reaches_a_command(args) or gh_credential(args):
        assert command_refusal(spell(template, args)), (template, args)


@PROPERTY
@given(args=gh_reads(), template=st.sampled_from(SPELLINGS))
def test_every_read_passes_with_global_flags_where_gh_accepts_them(args, template):
    """Invariant 3."""
    assert gh_read(args), args
    assert command_refusal(spell(template, args)) is None, (template, args)
    assert command_refusal(['gh', *args]) is None and command_refusal(['arch', '-arm64', 'gh', *args]) is None


@PROPERTY
@given(call=st.sampled_from(CREDENTIAL_CALLS), flags=st.lists(st.sampled_from(GLOBALS + [['-u', 'x'], ['--help']]),
                                                              max_size=2), at=st.sampled_from([0, 1]),
       template=st.sampled_from(SPELLINGS))
def test_credentials_are_refused_whatever_value_flags_come_first(call, flags, at, template):
    """Invariant 4: value flags, given with their values, before the command or before `auth`'s verb."""
    args = call.split()
    at = at if args[0] == 'auth' else 0
    for flag in flags:
        args[at:at] = flag
    assert gh_credential(args), args
    assert verdict(spell(template, args)) == 'credential', (template, args)


# The stated limit C-12.4 keeps: a first word gh doesn't know as a command (an
# alias or extension in gh's own config, or `file` in `grep gh file`) is not
# judged through a runner the rule doesn't know. The direct path refuses it.
@pytest.mark.documented_limitation
@pytest.mark.parametrize('command', ['arch -arm64 gh myalias 7', 'arch -arm64 gh frobnicate pr comment 7',
                                     'grep gh file', 'echo gh', 'ls gh/ ghost/', 'git log --grep gh --oneline',
                                     '{g,}h myextension --flag'])
def test_an_unknown_first_word_after_an_unknown_runner_is_not_judged_documented_limitation(command):
    assert command_refusal(command) is None, command
    assert command_refusal('gh myalias 7') and command_refusal('gh frobnicate pr comment 7')


# Intended: a run-time word followed by a gh command word is judged as gh,
# whatever program it names. Spelling the program (`uv run`) passes.
@pytest.mark.parametrize('command,refused', [
    ('$HOME/.local/bin/uv run pytest -q', True), ('"$UV" run pytest', True), ('"$GIT" status', True),
    ('uv run pytest -q', False), ('git status', False), ('"$VIRTUAL_ENV/bin/python" -m pytest -q', False),
    ('$PYTHON -m pytest tests/unit', False), ('"$(git rev-parse --show-toplevel)"/bin/check --help', False),
    ('$(command -v gh) --version', False), ('"$GH" auth status', False),
])
def test_a_run_time_word_before_a_gh_command_word_is_judged_as_gh(command, refused):
    assert bool(command_refusal(command)) is refused, command


# The PATH wrapper (layer 3) uses the same predicate.
@pytest.mark.parametrize('args,allowed', [
    (['pr', '-R', 'o/r', 'view', '7'], True), (['pr', '--repo', 'o/r', 'view', '7'], True), (['pr', 'ls'], True),
    (['pr', 'new', '--title', 't'], False), (['pr', '-R', 'o/r', 'comment', '7', '-b', 'x'], False),
    (['cs', 'delete', '--all'], False), (['auth', 'git-credential', 'get'], False),
    (['--body', 'x', 'pr', 'comment', '7'], False), (['pr', 'view', '7', '--show-token'], False),
])
def test_the_path_wrapper_judges_gh_with_the_same_predicate(tmp_path, monkeypatch, args, allowed):
    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'gh').write_text('#!/bin/sh\necho fake-gh "$@"\n')
    (fake / 'gh').chmod(0o755)
    monkeypatch.setenv('PATH', str(fake) + os.pathsep + os.environ['PATH'])
    launch = protected(tmp_path)
    result = run(['gh', *args], launch)
    assert (result.returncode == 0 and 'fake-gh' in result.stdout) is allowed, (args, result.stderr)
    if not allowed:
        assert result.returncode == 7 and 'publish: denied' in result.stderr


# The review's inputs through each provider's real command path: the hooks a
# review launch carries deny them, and past the hooks each shell runs gh
# (the recording stand-in at the Homebrew position), which reproduces the finding.
SHELL_WRITES = [
    ('alias through arch', 'arch -arm64 gh pr new --title t --body b'),
    ('alias after a substitution', '$(command -v gh) pr new --title t --body b'),
    # zsh parses a brace word in command position (`{g,}h pr new`) as a group and refuses it; the rule
    # refuses it too (above). A glob word is what zsh expands there.
    ('alias after a glob word', '{brew}/g[h] pr new -t t -b b'),
    ('flag before the verb through arch', 'arch -arm64 gh pr -R o/r comment 7 --body x'),
    ('flag before the verb after a substitution', '$(command -v gh) pr -R o/r comment 7 --body x'),
    ('flag before the verb after a glob', '{brew}/g? pr -R o/r comment 7 --body x'),
    ('flag before the verb after a variable', 'g=gh; $g pr -R o/r comment 7 --body x'),
    ('--repo= before the verb through arch', 'arch -arm64 gh pr --repo=o/r comment 7 --body x'),
    ('command alias through arch', 'arch -arm64 gh cs delete --all'),
    ('verb flag before the command through arch', 'arch -arm64 gh --body x pr comment 7'),
    ('git-credential through arch', 'arch -arm64 gh auth git-credential get'),
    ('git-credential', 'gh auth git-credential get'),
]
SHELL_READS = ['gh pr -R o/r view 7', 'gh pr --repo o/r view 7', 'arch -arm64 gh pr ls', '$(command -v gh) pr -R o/r view 7',
               'arch -arm64 gh -R o/r run ls']


def hook_reasons(launch, provider, command, env):
    payload = {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': launch.cwd}
    if provider == 'claude':
        payload.update(hook_event_name='PreToolUse', session_id='fixture-session')
    reasons = []
    for hook in provider_hooks(launch, provider):
        result = subprocess.run(['/bin/sh', '-c', hook], input=json.dumps(payload), env=env, capture_output=True,
                                text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        if result.stdout.strip():
            output = json.loads(result.stdout)['hookSpecificOutput']
            if output['permissionDecision'] == 'deny':
                reasons.append(output['permissionDecisionReason'])
    return reasons


@pytest.mark.parametrize('provider,shell', PROVIDER_SHELLS, ids=lambda value: str(value))
def test_each_input_is_denied_by_the_real_hooks_and_reaches_gh_past_them(tmp_path, provider, shell):
    launch, env, log, snapshot, world = round7_world(tmp_path, provider)
    for name, template in SHELL_WRITES:
        command = fill(template, world)
        [reason] = hook_reasons(launch, provider, command, env)
        wording = 'never prints a GitHub credential' if 'git-credential' in command else 'verdict is its deliverable'
        assert wording in reason, (provider, name, reason)
        before = len(recorded(log))
        subprocess.run(SHELLS[shell](command, snapshot), cwd=launch.cwd, env=env, capture_output=True, text=True,
                       timeout=120)
        assert len(recorded(log)) > before, (shell, name)
    for command in SHELL_READS:
        assert not hooks_deny(launch, provider, command, env), (provider, command)
        before = len(recorded(log))
        result = subprocess.run(SHELLS[shell](command, snapshot), cwd=launch.cwd, env=env, capture_output=True,
                                text=True, timeout=120)
        assert result.returncode == 0 and len(recorded(log)) == before + 1, (shell, command, result.stderr)


# Differential: the rule's reading of gh's command line against real gh
# 2.100.0's own, offline. `--help` makes gh name the command it found (its
# USAGE line, or the parent an unknown subcommand was looked up in) without a
# request; a scratch config and no token mean it reads no credential.
GH = shutil.which('gh')


def real_gh(tmp_path, args):
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'GH_CONFIG_DIR': str(tmp_path / 'gh-config'),
           'GH_NO_UPDATE_NOTIFIER': '1', 'GH_NO_EXTENSION_UPDATE_NOTIFIER': '1', 'GH_PROMPT_DISABLED': '1',
           'NO_COLOR': '1', 'XDG_DATA_HOME': str(tmp_path / 'data'), 'XDG_STATE_HOME': str(tmp_path / 'state'),
           'XDG_CONFIG_HOME': str(tmp_path / 'config')}
    return subprocess.run([GH, *args], env=env, capture_output=True, text=True, timeout=120)


@pytest.fixture
def gh_2_100(tmp_path):
    version = real_gh(tmp_path, ['--version']).stdout if GH else ''
    if 'gh version 2.100.0 ' not in version:
        pytest.skip(f'measured on gh 2.100.0; installed: {version.strip() or "none"}')
    return tmp_path


def found_by_real_gh(tmp_path, args):
    """The command path real gh found for `gh <args>`: [] when it found none."""
    result = real_gh(tmp_path, [*args, '--help'])
    text = result.stdout + result.stderr
    unknown = re.search(r'unknown command "[^"]*" for "gh((?: [\w-]+)*)"', text)
    if unknown:
        return unknown[1].split()
    usage = re.search(r'(?:USAGE\n|Usage:)\s+gh ((?:[\w-]+ ?)*)', text)
    assert usage, text[-2000:]
    return usage[1].split()


def found_by_the_rule(args):
    """`_gh_path`'s command path, each part kept only where gh has it, an alias named as gh names it."""
    path, _ = _gh_path(args)
    if not path or path[0] not in GH_SUBCOMMANDS:
        return []
    if path[0] == 'co':
        return ['pr', 'checkout']
    command = GH_COMMAND_ALIASES.get(path[0], path[0])
    return [command, path[1]] if len(path) > 1 and path[1] in GH_SUBCOMMANDS[command] else [command]


PARSE_CASES = [*(call.split() for call in R3C_ALIASES + MORE_ALIASES if not call.startswith('help')),
               *(shlex.split(call) for call in FLAG_FIRST), *(call.split() for call in READ_FORMS),
               *(call.split() for call in CREDENTIAL_CALLS if '--show-token' not in call),
               ['--web', 'pr', 'view', '7'], ['-h', 'pr', 'comment', '7'], ['--', 'pr', 'view', '7'],
               ['pr', '--', 'view', '7'], ['--help', 'pr', 'comment', '7'], ['--version', 'pr', 'comment', '7'],
               ['auth', '-t', 'status'], ['pr', '--web', 'view', '7'], ['--hostname', 'h', 'pr', 'view', '7']]


def test_the_rule_finds_the_command_real_gh_finds(gh_2_100):
    for args in PARSE_CASES:
        real, ours = found_by_real_gh(gh_2_100, args), found_by_the_rule(args)
        assert len(real) == len(ours) and real[:1] == ours[:1], (args, real, ours)
        # gh's USAGE line names a subcommand alias (`pr new`) by its subcommand (`pr create`).
        assert real[1:] == ours[1:] or {real[1], ours[1]} <= GH_SUBCOMMANDS[real[0]], (args, real, ours)


def help_sections(text):
    found, current = {}, None
    for line in text.splitlines():
        if re.fullmatch(r'[A-Z][A-Z /-]+', line.strip()) and not line.startswith(' '):
            current = found.setdefault(line.strip(), [])
        elif current is not None and line.strip():
            current.append(line.strip())
    return found


def listed_commands(text):
    return [re.match(r'([\w-]+):', line)[1] for title, lines in help_sections(text).items()
            if 'COMMANDS' in title and title != 'ALIAS COMMANDS' for line in lines if re.match(r'[\w-]+:\s', line)]


def listed_aliases(text):
    return [item.split()[-1] for line in help_sections(text).get('ALIASES', []) for item in line.split(',')]


def test_gh_subcommands_holds_every_command_subcommand_and_alias_real_gh_lists(gh_2_100):
    commands = listed_commands(real_gh(gh_2_100, ['--help']).stdout)
    assert {'pr', 'issue', 'api', 'auth', 'codespace'} <= set(commands), commands
    for command in commands:
        text = real_gh(gh_2_100, [command, '--help']).stdout
        assert command in GH_SUBCOMMANDS, command
        for alias in listed_aliases(text):
            assert GH_COMMAND_ALIASES.get(alias) == command, (command, alias)
        for verb in listed_commands(text):
            assert verb in GH_SUBCOMMANDS[command], (command, verb)
            for alias in listed_aliases(real_gh(gh_2_100, [command, verb, '--help']).stdout):
                assert alias in GH_SUBCOMMANDS[command] or GH_COMMAND_ALIASES.get(alias) == command, \
                    (command, verb, alias)


# Beyond review r3c: macOS's default APFS volume is case-insensitive, so the
# shell finds gh for `GH` and `Gh` (zsh and bash ran the recording gh for
# `GH pr comment 7` on this host), and git, the shells, `env` and the copiers
# the same way. The rule compares command names case-insensitively.
CASE_WRITES = ['GH pr comment 7 --body x', 'Gh pr merge 7', 'gH issue close 1', '/opt/homebrew/bin/GH pr comment 7',
               'arch -arm64 GH pr new --title t', 'ENV gh pr comment 7', 'env GH pr comment 7', 'command GH pr merge 7',
               "BASH <<< 'gh pr comment 7'", "ZSH -c 'gh pr comment 7'", "EVAL 'gh pr comment 7'",
               'GIT push origin HEAD', 'arch -arm64 GIT push origin HEAD', 'CP /opt/homebrew/bin/gh ./g',
               'cp /opt/homebrew/bin/GH ./g', 'LN -s "$(command -v gh)" ./g', "c='GH pr comment 7'; ${=c}",
               "c='GH pr comment 7'; eval \"$c\"", 'Find . -exec GH pr comment 7 \\;']
CASE_CREDENTIAL = ['GH auth token', 'arch -arm64 Gh auth git-credential get']
CASE_PASSES = ['GH pr view 7', 'arch -arm64 Gh pr ls', 'GH_PAGER=cat gh pr view 7', 'echo $GH_TOKEN_FILE', 'GREP -n gh x',
               'Git log --oneline -5', 'echo GH']


@pytest.mark.parametrize('command', CASE_WRITES)
def test_a_command_name_in_another_case_is_judged_as_the_command(command):
    refused = command_refusal(command)
    assert refused and refused.kind == 'publish', command


@pytest.mark.parametrize('command', CASE_CREDENTIAL)
def test_a_credential_call_in_another_case_keeps_the_credential_wording(command):
    refused = command_refusal(command)
    assert refused and refused.kind == 'credential', command


@pytest.mark.parametrize('command', CASE_PASSES)
def test_reads_and_other_words_in_another_case_pass(command):
    assert command_refusal(command) is None, command


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(name=st.sampled_from(['gh', 'Gh', 'gH', 'GH']), path=st.sampled_from(['', '/opt/homebrew/bin/', './']),
       call=st.sampled_from(R3C_ALIASES + FLAG_FIRST + ['pr comment 7 -b x', 'api -XPOST repos/o/r']),
       template=st.sampled_from(['{w}', 'arch -arm64 {w}', 'env {w}', "bash -c '{w}'", 'nohup {w}']))
def test_any_casing_of_gh_is_judged_as_gh(name, path, call, template):
    word = path + name
    command = template.replace('{w}', f'{word} {shlex.join(shlex.split(call))}')
    assert command_refusal(command), command


def case_insensitive(directory: Path) -> bool:
    probe = directory / 'case-probe'
    probe.write_text('')
    return (directory / 'CASE-PROBE').exists()


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_a_differently_cased_gh_runs_gh_here_and_the_hooks_deny_it(tmp_path, provider):
    if not case_insensitive(tmp_path):
        pytest.skip('this filesystem is case-sensitive: `GH` does not find gh')
    launch = protected(tmp_path, provider=provider)
    env, log, snapshot = zsh_environment(tmp_path, launch)
    for command in ('GH pr comment 7 --body published', 'Gh pr merge 7', "BASH <<< 'gh pr comment 7 --body x'"):
        assert hooks_deny(launch, provider, command, env), (provider, command)
        before = len(recorded(log))
        subprocess.run(SHELLS['zsh -c'](command, snapshot), cwd=launch.cwd, env=env, capture_output=True, text=True,
                       timeout=120)
        assert len(recorded(log)) > before, command
