"""#221 review r4 (D-SH1 round 10): the publish rule refuses every gh command it can't show is a read.

Review r4 found two older gaps, each executed through the installed hook:
- P2-1: the fallback judged gh only when its first word was a known gh command, so a
  word known only at run time there (`arch -arm64 gh $v comment`, `gh ${=a}`) got
  through, and so did gh fed by xargs behind a runner (`… | xargs arch -arm64 gh`).
- P2-2: `env`'s option clusters (`-vS`, `-iS`) and `-S`'s own escapes (`\\_`) hid
  the split string, and `csh` and `tcsh` were not shells to the rule.

The invariants, for every argument list `a` given to gh:
1. One judgment: the direct path, the known wrappers, the fallback and the run-time
   words give the same verdict, now also when a run-time word comes before gh's verb
   is settled and when xargs or parallel feeds gh.
2. Fail closed: a gh call whose command or subcommand isn't spelled literally (or
   whose arguments come from xargs) is refused on every path, and so is any literal
   list `gh_read` doesn't accept that reaches a gh command.
3. Reads pass: every literal `GH_READS` call passes on every path, including with
   run-time words after the settled verb.
4. Runners are seen through: a gh write is refused through `env` in any option
   cluster and through every shell in `SHELLS` given `-c` in any cluster or its
   input on a pipe, a heredoc or a here-string; an `-S` value with an escape is refused.

gh is always the recording stand-in; no test makes a network call.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess

from hypothesis import given, settings, strategies as st
import pytest

from subfleet.publish_rule import C_SHELLS, GH_READS, GH_SUBCOMMANDS, NOT_SHELL_VALUES, SHELLS as RULE_SHELLS
from subfleet.publish_rule import _gh_unsettled, command_refusal, gh_credential, gh_read
from tests.unit.test_publish_rule_round9 import SPELLINGS, gh_argument_lists, gh_reads, hook_reasons, spell, verdict
from tests.unit.test_review_publish_guard import protected, recorded, zsh_environment

# Review r4's two executed tables, each input as it ran. `allowed` is the installed
# hook's verdict at e60509af1; each allowed row is a regression this round fixes.
R4_P2_1 = [
    ('v=pr; gh $v comment 7 -b x', False),
    ('v=pr; arch -arm64 gh $v comment 7 -b x', True),
    ('v=pr; arch -arm64 gh "$v" comment 7 -b x', True),
    ('arch -arm64 gh $(echo pr) comment 7 -b x', True),
    ("a='pr comment 7 -b x'; arch -arm64 gh ${=a}", True),
    ("printf '%s\\n' pr comment 7 | xargs arch -arm64 gh", True),
    ("printf '%s\\n' pr comment 7 | xargs gh", False),
]
R4_P2_2 = [
    ("env -vS 'gh pr comment 7 -b x'", True),
    ("env -S 'gh\\_pr\\_comment\\_7\\_-b\\_x'", True),
    ("env -iS 'gh pr comment 7 -b x'", True),
    ("tcsh -fc 'gh pr comment 7 -b x'", True),
    ("csh -fc 'gh pr comment 7 -b x'", True),
    ("echo 'gh pr comment 7 -b x' | tcsh -f", True),
    ("env -S 'gh pr comment 7 -b x'", False),
    ("tcsh -c 'gh pr comment 7 -b x'", False),
]
R4_ROWS = R4_P2_1 + R4_P2_2


@pytest.mark.parametrize('command,allowed_before', R4_ROWS, ids=[row for row, _ in R4_ROWS])
def test_each_row_review_r4_executed_is_refused_with_the_publish_wording(command, allowed_before):
    refused = command_refusal(command)
    assert refused and refused.kind == 'publish', command


# The same gaps in the forms next to them (found this round, not in the review).
P2_1_NEIGHBOURS = [
    'arch -arm64 gh `echo pr` comment 7', 'arch -arm64 gh $((1)) comment 7', 'arch -arm64 gh <(echo pr) comment 7',
    'arch -arm64 gh p? comment 7', 'arch -arm64 gh {pr,} comment 7', "a='pr comment 7'; arch -arm64 gh $=a",
    'arch -arm64 gh pr $v 7 -b x', 'arch -arm64 gh -R o/r $v comment 7', 'arch -arm64 gh -R "$r" pr comment 7',
    'arch -arm64 gh --repo=$r pr view 7', 'gh -R "$r" pr view 7', 'gh pr --repo "$r" view 7', 'gh pr $v 7',
    '{g,}h $v comment 7', '/opt/homebrew/bin/g? $v comment 7', 'arch -arm64 $(command -v gh) $v comment 7',
    'taskpolicy -b "$GH" $v comment 7', 'g=gh; arch -arm64 $g $v comment 7', 'for g in gh; do arch -arm64 $g $v x; done',
    "printf '%s\\n' pr | xargs -0 arch -arm64 gh", "printf '%s\\n' pr | xargs -I{} arch -arm64 gh {} comment 7",
    "printf 7 | xargs gh pr view", "printf 7 | xargs -n1 /opt/homebrew/bin/gh pr view", 'printf x | arch -arm64 xargs gh',
    "printf 7 | xargs {g,}h pr view", "printf 7 | xargs \"$GH\" pr view", 'parallel gh api repos/o/r/pulls/1/merge ::: -XPUT',
    "printf 7 | parallel gh pr view", 'parallel gh pr view ::: 1 2',
]
P2_2_NEIGHBOURS = [
    "env -0vS 'gh pr comment 7'", "env -iS'gh pr comment 7'", "env -uFOO -S 'gh pr comment 7'", "env -vuFOO -S'gh pr comment 7'",
    "env -P/bin -S 'gh pr comment 7'", "env -S'-v gh pr comment 7'", "env --split-string='gh pr comment 7'",
    "env --split 'gh pr comment 7'", "env -S 'gh pr\\tcomment'", "env -S 'gh pr comment\\c'", "env -S '${GH} pr comment 7'",
    "env -S 'gh \"pr\" comment 7 \\#x'", "x='gh\\_pr\\_comment'; env -S \"$x\"", "arch -arm64 env -vS 'gh pr comment 7'",
    "tcsh -cf 'gh pr comment 7'", "tcsh -xc 'gh pr comment 7'", "csh -ec 'gh pr comment 7'", "tcsh -fsc 'gh pr comment 7'",
    "echo 'gh pr comment 7' | csh -f", "tcsh -f <<< 'gh pr comment 7'", "tcsh <<EOF\ngh pr comment 7\nEOF",
    "tcsh -f < <(echo 'gh pr comment 7')", "/bin/tcsh -fc 'gh pr comment 7'", "TCSH -fc 'gh pr comment 7'",
    "bash -eo pipefail -ec 'gh pr comment 7'", "zsh -o errexit -xc 'gh pr comment 7'", "ksh -oc 'gh pr comment 7'",
    "sh -euo pipefail -fc 'gh pr comment 7'", "arch -arm64 bash -ec 'gh pr comment 7'", "arch -arm64 tcsh -fc 'gh pr comment 7'",
    "echo 'gh pr comment 7' | arch -arm64 bash", "echo 'gh pr comment 7' | arch -arm64 tcsh -f",
    "arch -arm64 bash <<< 'gh pr comment 7'", "parallel <<< 'gh pr comment 7'", "fish -c'gh pr comment 7'",
    "fish --command='gh pr comment 7'", "fish -C 'gh pr comment 7'", "printf 'gh pr comment 7' | xargs -0 arch -arm64 sh -c",
]


@pytest.mark.parametrize('command', P2_1_NEIGHBOURS + P2_2_NEIGHBOURS)
def test_the_same_gaps_in_neighbouring_forms_are_refused(command):
    refused = command_refusal(command)
    assert refused and refused.kind == 'publish', command


# What keeps working: text tools, reads with run-time words after the settled verb,
# and run-time words that cannot be gh.
STILL_PASSES = [
    'grep gh "$f"', 'xargs grep gh', 'printf x | xargs grep -n gh', 'rg -n gh "$dir" "$other"', 'git log --grep gh "$rev"',
    'gh pr view "$n"', 'gh pr view "$n" -R "$REPO"', 'gh pr checks $(cat n)', 'gh run view "$id" --log-failed',
    'gh api "repos/$r/pulls/$n"', 'gh search prs "$q"', 'arch -arm64 gh pr view "$n"', '{g,}h pr view $n',
    '"$GH" pr view "$n"', '$(command -v gh) pr diff "$n"', 'echo "$(gh pr view 1 -q .t)" "$(gh pr view 2 -q .t)"',
    'diff "$a" "$b" | grep gh', '"$PY" "$script"', '$PYTHON -m pytest tests/unit', 'git diff "$BASE" "$HEAD"',
    'ls ./* "$x"', 'echo "$a" "$b"; gh pr view 7', 'for n in 1 2; do gh pr view "$n"; done', 'env -vS "gh pr view 7"',
    "env -iS 'PATH=/usr/bin git status'", "bash -euo pipefail -c 'git status'", "tcsh -fc 'echo hi'", 'echo hi | tcsh -f',
    "csh -fc 'gh pr view 7'", 'ps aux | grep bash', 'which bash', 'find . -name "*.py" -exec wc -l {} +',
    'cat "$a" "$b"', 'gh pr list --json number -q ".[].number"', 'rg -n "gh pr comment" docs; which bash',
    'grep -rn "gh pr comment" tests | head -3; ls -la /bin/sh /bin/tcsh', "echo bash -c 'gh pr view 7'",
    'gh run watch "$id"', 'arch -arm64 gh run watch "$id"',
]


@pytest.mark.parametrize('command', STILL_PASSES)
def test_text_tools_reads_and_other_run_time_words_still_pass(command):
    assert command_refusal(command) is None, command


def test_the_text_tools_are_the_ones_named():
    assert {'grep', 'rg', 'sed', 'awk', 'jq', 'git'} <= NOT_SHELL_VALUES
    assert {'csh', 'tcsh'} <= RULE_SHELLS and C_SHELLS <= RULE_SHELLS


# Invariants 1 and 2 with run-time words where gh reads its command or subcommand.
class Raw(str):
    """A word written into the command text as is: the shell expands it at run time."""


def render(args) -> str:
    return ' '.join(word if isinstance(word, Raw) else shlex.quote(word) for word in args)


def spell_raw(template: str, args) -> str:
    return template.replace('{a}', render(args))


RUNTIME_WORDS = ['$v', '"$v"', '${v}', '${v:-pr}', '"$@"', '$(echo pr)', '"$(echo pr)"', '`echo pr`', '${=a}', '$=a',
                 'p?', 'p[r]', '{pr,}', '{p,}r', '$((1))', '<(echo pr)']
COMMANDS = sorted(GH_SUBCOMMANDS)
WITH_SUBCOMMANDS = sorted(name for name, verbs in GH_SUBCOMMANDS.items() if verbs)


@st.composite
def gh_runtime_lists(draw):
    """gh argument lists with a run-time word as the command, the subcommand, or a flag's value before them."""
    command = draw(st.sampled_from(COMMANDS))
    verbs = sorted(GH_SUBCOMMANDS[command] | GH_READS.get(command, frozenset()))
    units = [[command], *([[draw(st.sampled_from(verbs))]] if verbs else []),
             *draw(st.lists(st.sampled_from([['7'], ['x'], ['--comments'], ['-L', '5'], ['--body', 'x']]), max_size=2))]
    word = Raw(draw(st.sampled_from(RUNTIME_WORDS)))
    spot = draw(st.sampled_from(['command', 'verb', 'value'] if verbs else ['command', 'value']))
    if spot == 'command':
        units[0] = [word]
    elif spot == 'verb':
        units[1] = [word]
    else:
        flag = draw(st.sampled_from([['-R', word], ['--repo', word], [Raw('--repo=' + word)], ['--hostname', word]]))
        units.insert(draw(st.integers(0, 1 if verbs else 0)), flag)
    for flag in draw(st.lists(st.sampled_from([['-R', 'o/r'], ['--repo=o/r']]), max_size=1)):
        units.insert(0, flag)
    return [item for unit in units for item in unit]


def plain(args):
    """The words as the rule sees them in the text: a process substitution names a file."""
    return ['/dev/fd/63' if word.startswith('<(') else str(word) for word in args]


def expected_refusal(args):
    return 'credential' if gh_credential(plain(args)) else 'publish'


PROPERTY = settings(max_examples=300, derandomize=True, database=None, deadline=None)


@PROPERTY
@given(args=gh_runtime_lists(), template=st.sampled_from(SPELLINGS))
def test_one_judgment_with_a_run_time_word_before_the_verb(args, template):
    """Invariants 1 and 2: every path refuses, with the same wording. (An argument
    vector expands no word, so its run-time words are literal there.)"""
    assert _gh_unsettled(plain(args)) and not gh_read(plain(args)), args
    command = spell_raw(template, args)
    assert verdict(command) == expected_refusal(args), (template, command)


#: How xargs and GNU parallel feed gh, on the direct path, behind known wrappers and
#: unknown runners, and after run-time words that may be gh.
FED = ['printf x | xargs gh {a}', 'printf x | xargs -0 gh {a}', 'printf x | xargs -I{} gh {a}', 'printf x | xargs -n1 gh {a}',
       'printf x | xargs /opt/homebrew/bin/gh {a}', 'printf x | xargs sudo -u x gh {a}', 'printf x | xargs env X=1 gh {a}',
       'printf x | xargs nohup gh {a}', 'printf x | xargs arch -arm64 gh {a}', 'printf x | xargs -0 arch -arm64 gh {a}',
       'printf x | xargs -I{} arch -arm64 gh {a}', 'printf x | xargs lockf -k /tmp/l gh {a}', 'printf x | arch -arm64 xargs gh {a}',
       'printf x | xargs {g,}h {a}', 'printf x | xargs "$GH" {a}', 'printf x | xargs $(command -v gh) {a}',
       'printf x | xargs /opt/homebrew/bin/g? {a}', 'parallel gh {a} ::: 1', 'printf x | parallel gh {a}',
       'parallel arch -arm64 gh {a} ::: 1 2']


@PROPERTY
@given(args=gh_argument_lists(), template=st.sampled_from(FED))
def test_one_judgment_when_xargs_or_parallel_feeds_gh(args, template):
    """Invariants 1 and 2: gh's arguments come partly from its input, so no path calls it a read."""
    assert verdict(spell(template, args)) == ('credential' if gh_credential(args) else 'publish'), (template, args)


#: Run-time words after the settled verb, which only add operands and flags to a read.
AFTER_VERB = [Raw('"$n"'), Raw('$n'), Raw('$(cat n)'), Raw('"${n:-7}"'), Raw('${=extra}'), Raw('"$@"')]


@st.composite
def gh_reads_with_run_time_operands(draw):
    words = draw(gh_reads())
    command = next(word for word in words if word in GH_READS)
    verb_at = next(at for at, word in enumerate(words) if word in GH_READS[command] and at > words.index(command))
    at = draw(st.integers(verb_at + 1, len(words)))
    return words[:at] + [draw(st.sampled_from(AFTER_VERB))] + words[at:]


@PROPERTY
@given(args=gh_reads_with_run_time_operands(), template=st.sampled_from(SPELLINGS))
def test_reads_pass_with_run_time_words_after_the_settled_verb(args, template):
    """Invariant 3."""
    assert gh_read(plain(args)), args
    command = spell_raw(template, args)
    assert command_refusal(command) is None, (template, command)


# Invariant 4: the runners.
WRITES = ['gh pr comment 7 -b x', 'gh pr -R o/r comment 7', 'gh api -XPOST repos/o/r/issues/1/comments', 'gh pr new -t t',
          'gh --body x pr comment 7', 'gh issue close 1', 'arch -arm64 gh pr merge 7', '/opt/homebrew/bin/gh pr ready 7']
ENV_BOOLEANS = '0iv'
ENV_VALUE_OPTIONS = [['-u', 'NAME'], ['-uNAME'], ['-P', '/usr/bin'], ['-P/usr/bin'], ['-C', '/tmp'], ['NAME=value']]


@st.composite
def env_calls(draw):
    """`env` with its options in any cluster, and the write in `-S`'s value."""
    write = draw(st.sampled_from(WRITES))
    before = [word for unit in draw(st.lists(st.sampled_from(ENV_VALUE_OPTIONS), max_size=2)) for word in unit]
    booleans = ''.join(draw(st.permutations(ENV_BOOLEANS))[:draw(st.integers(0, 3))])
    form = draw(st.sampled_from(['next', 'attached', 'long', 'long-next', 'split-options']))
    if form == 'next':
        tail = [f'-{booleans}S', write]
    elif form == 'attached':
        tail = [f'-{booleans}S{write}']
    elif form == 'long':
        tail = [*([f'-{booleans}'] if booleans else []), f'--split-string={write}']
    elif form == 'long-next':
        tail = [*([f'-{booleans}'] if booleans else []), '--split-string', write]
    else:
        tail = [f'-{booleans}S', f'-v NAME=x {write}']
    prefix = draw(st.sampled_from(['', 'arch -arm64 ', 'nohup ', 'X=1 ']))
    return prefix + shlex.join(['env', *before, *tail])


@PROPERTY
@given(command=env_calls())
def test_a_write_through_env_in_any_option_cluster_is_refused(command):
    refused = command_refusal(command)
    assert refused and refused.kind == 'publish', command


ENV_ESCAPES = ['\\_', '\\t', '\\n', '\\r', '\\f', '\\v', '\\c', '\\#', '\\$', '\\\\', "\\'", '\\"', '${HOME}', '${GH}']


@PROPERTY
@given(text=st.sampled_from(['gh pr view 7', 'gh pr comment 7', 'git status', 'printf x']), escape=st.sampled_from(ENV_ESCAPES),
       at=st.integers(0, 12), cluster=st.sampled_from(['-S', '-vS', '-iS', '--split-string=']))
def test_an_env_split_string_with_an_escape_is_refused(text, escape, at, cluster):
    """Invariant 4: env applies its own escapes and environment to `-S`'s value, which the rule refuses."""
    value = text[:at] + escape + text[at:]
    command = shlex.join(['env', cluster + value] if cluster.endswith('=') else ['env', cluster, value])
    refused = command_refusal(command)
    assert refused and refused.kind == 'publish', command


#: Letters each shell accepts in a cluster beside `c` (bash and sh's `o` and `O` take a value).
SHELL_LETTERS = {'csh': 'efvxisn', 'tcsh': 'efvxisnm', 'fish': 'ilnP'}
POSIX_LETTERS = 'efvxuain'


@st.composite
def shell_calls(draw):
    """Each shell in SHELLS given the write by `-c` in a cluster, or as its input."""
    shell = draw(st.sampled_from(sorted(RULE_SHELLS)))
    write = draw(st.sampled_from(WRITES))
    letters = list(draw(st.lists(st.sampled_from(SHELL_LETTERS.get(shell, POSIX_LETTERS)), max_size=3, unique=True)))
    values = []
    if shell not in C_SHELLS and shell != 'fish' and draw(st.booleans()):
        letters.insert(draw(st.integers(0, len(letters))), draw(st.sampled_from('oO')))
        values = [draw(st.sampled_from(['pipefail', 'errexit', 'extglob']))]
    path = draw(st.sampled_from(['', '/bin/', '/usr/bin/']))
    runner = draw(st.sampled_from(['', 'arch -arm64 ', 'nohup ', 'env ']))
    how = draw(st.sampled_from(['-c', 'pipe', 'heredoc', 'herestring', 'process']))
    if how == '-c':
        letters.insert(draw(st.integers(0, len(letters))), 'c')
        return f"{runner}{path}{shell} -{''.join(letters)} {shlex.join(values)} {shlex.quote(write)}"
    cluster = f"-{''.join(letters)} {shlex.join(values)}" if letters else ''
    call = f'{runner}{path}{shell} {cluster}'.rstrip()
    if how == 'pipe':
        return f'echo {shlex.quote(write)} | {call}'
    if how == 'heredoc':
        return f'{call} <<EOF\n{write}\nEOF'
    if how == 'herestring':
        return f'{call} <<< {shlex.quote(write)}'
    return f'{call} < <(echo {shlex.quote(write)})'


@PROPERTY
@given(command=shell_calls())
def test_a_write_given_to_any_shell_by_a_cluster_or_its_input_is_refused(command):
    refused = command_refusal(command)
    assert refused and refused.kind == 'publish', command


# Each listed shell's real reading of `-c` in a cluster, on this host: the rule
# judges the script wherever the shell runs it. (fish, mksh: not installed.)
CLUSTERS = ['-c', '-ec', '-xc', '-fc', '-ce', '-cx', '-cf', '-sc', '-eo pipefail -c', '-euo pipefail -xc',
            '-o errexit -c', '+o errexit -c', '-oc', '-eO extglob -c', '-Oc']


@pytest.mark.parametrize('shell', sorted(name for name in RULE_SHELLS if shutil.which(name)))
def test_the_rule_judges_the_script_wherever_each_real_shell_runs_it(tmp_path, shell):
    ran = []
    for cluster in CLUSTERS:
        marker = tmp_path / 'ran'
        marker.unlink(missing_ok=True)
        script = f'touch {shlex.quote(str(marker))}'
        # The session's PATH keeps its recording `security` ahead of the system directories (the keychain guard).
        subprocess.run([shutil.which(shell), *shlex.split(cluster), script], stdin=subprocess.DEVNULL, cwd=tmp_path,
                       env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path)}, capture_output=True, timeout=60)
        if marker.exists():
            ran.append(cluster)
            command = f'{shell} {cluster} {shlex.quote("gh pr comment 7 -b x")}'
            assert command_refusal(command), (shell, cluster)
    assert '-c' in ran and '-fc' in ran, (shell, ran)


# Through the real command paths: the hooks a review launch carries deny each row,
# and past them the shell runs the recording gh (first on PATH), so each denial is
# what stops the call. zsh -f runs every row; csh -f and tcsh -f run the rows in
# their syntax. `env -iS` clears PATH, so gh runs only with a PATH given in -S.
SHELL_ROWS = [row for row, allowed in R4_ROWS if allowed] + [
    "env -iS 'PATH={brew} gh pr comment 7 -b x'", "printf 7 | xargs gh pr view", 'arch -arm64 gh -R "$r" pr comment 7 -b x',
    "echo 'gh pr comment 7 -b x' | csh -f", "bash -eo pipefail -ec 'gh pr comment 7 -b x'"]
NO_GH = {"env -iS 'gh pr comment 7 -b x'"}
CSH_SYNTAX = {"env -vS 'gh pr comment 7 -b x'", "env -S 'gh\\_pr\\_comment\\_7\\_-b\\_x'", "env -iS 'gh pr comment 7 -b x'",
              "tcsh -fc 'gh pr comment 7 -b x'", "csh -fc 'gh pr comment 7 -b x'", "echo 'gh pr comment 7 -b x' | tcsh -f",
              "printf '%s\\n' pr comment 7 | xargs arch -arm64 gh", "env -iS 'PATH={brew} gh pr comment 7 -b x'",
              "printf 7 | xargs gh pr view", "echo 'gh pr comment 7 -b x' | csh -f",
              "bash -eo pipefail -ec 'gh pr comment 7 -b x'"}
OUTER = {'zsh -f': ['/bin/zsh', '-f', '-c'], 'csh -f': ['/bin/csh', '-f', '-c'], 'tcsh -f': ['/bin/tcsh', '-f', '-c']}


@pytest.mark.parametrize('outer', sorted(OUTER))
@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_each_row_is_denied_by_the_real_hooks_and_reaches_gh_past_them(tmp_path, provider, outer):
    if not Path(OUTER[outer][0]).exists():
        pytest.skip(f'{OUTER[outer][0]} is not on this host')
    launch = protected(tmp_path, provider=provider)
    env, log, _snapshot = zsh_environment(tmp_path, launch)
    brew = tmp_path / 'homebrew' / 'bin'
    env['PATH'] = f'{brew}:{env["PATH"]}'       # zsh -f reads no startup file to put it first
    rows = SHELL_ROWS if outer == 'zsh -f' else [row for row in SHELL_ROWS if row in CSH_SYNTAX]
    for template in rows:
        command = template.replace('{brew}', str(brew))
        [reason] = hook_reasons(launch, provider, command, env)
        assert 'verdict is its deliverable' in reason, (provider, command, reason)
        before = len(recorded(log))
        subprocess.run([*OUTER[outer], command], cwd=launch.cwd, env=env, capture_output=True, text=True, timeout=120)
        if template in NO_GH:
            assert len(recorded(log)) == before, (outer, command)
        else:
            assert len(recorded(log)) > before, (outer, command)
