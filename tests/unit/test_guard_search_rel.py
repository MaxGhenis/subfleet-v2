"""D-SEARCH-REL1: the Codex lane hook resolves relative search roots against the cwd.

On 11/04 one Codex lane's ``find ..``, run from its job worktree (``~/.subfleet/worktrees/<job>``),
walked every job worktree for 38 minutes. The lane hook's unscoped-search rule
(``private/guard/never-rules-hook.sh``) judged the command's words with regexes: it resolved
absolute roots, and ``.`` when the cwd itself is a broad root, but never a relative root against
the cwd, so ``find ..`` passed. The session hook calls a parser, ``unscoped_search.py``, that splits
the command as the shell does, follows ``cd`` and resolves every root against the cwd. The lane
hook now embeds that parser byte for byte as a heredoc body and runs it after the regex judgment
whenever the rule's prefilter (``pw_named``) found a searcher; the hook blocks when either judgment
blocks, and with no interpreter, a nonzero exit or no answer within 5 s the regex judgment decides
alone, as before.

Invariants, each checked below:

R1 (the hub's rows) On each of the hub's 17 rows (``ROWS``, from rows.tsv: a cwd in a job worktree
   or its subfleet folder under a real temporary home, a command, the parser's verdict and the lane
   hook's verdict before this change) the hook gives the parser's verdict under zsh and bash, and the
   embedded parser run on its own gives it too. A block the regex judgment makes keeps its output
   byte for byte; one only the parser makes names the parser's answer in the rule's message format.
   With no interpreter each row keeps its verdict before this change, as the r12d hook gives it.
R2 (either judgment) On every corpus pair (``corpus_pairs``: the base module's corpus, which is
   every command string its guard tests run through this hook, plus each row of its ``*_DENIED``
   lists), the hook with the parser blocks iff the regex judgment blocks or the parser, asked,
   blocks. The parser's view is the base module's fixture file system (``FixtureOS`` gives it the
   regex judgment's seam). A pair the regex judgment blocks keeps its output byte for byte, except a
   deny by hf-dest, which runs after the region, of a command the parser also blocks (both deny).
R3 (fallback) With no interpreter, the hook's output on every corpus pair is the r12d hook's (the
   hook before this change, rebuilt from this one by removing the parser block and undoing its
   other edits, pinned by hash), byte for byte. A nonzero exit is no answer, and so is none within
   5 s (the loader's alarm).
R4 (cost) The interpreter starts once for a command in which the rule's prefilter finds a searcher
   and the regex judgment does not block, and never otherwise.
R5 (bytes) The heredoc's body is the hub's parser (``PARSER_SHA256``) and its opener appears once;
   the interpreter gets those bytes on descriptor 3, the command on stdin and ``-I -S -c <loader>
   <cwd> <home> <shell>`` as its arguments.

The disagreements between the two judgments over the corpus, in each direction, are written to
``$SEARCH_REL_REPORT`` when it is set (JSON). ``$SEARCH_REL_TIMING`` (a JSON path) runs the cost
measurement: each row and three commands with no walk, 20 runs each, before (r12d) and after.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import signal
import statistics
import subprocess
import sys
import time

import pytest

from subfleet.guard import preflight as guard
from tests.unit import test_guard_subfleet_root as base
# The base module's fixtures, provided here too: its fixture file system (autouse), the git repositories
# its other-rule commands run in, and its corpus.
from tests.unit.test_guard_subfleet_root import corpus, fixture_fs, repos  # noqa: F401

HOOK = base.HOOK
#: The hub's copy of the session hook's parser, read 2026-11-05 (~/reviews/subfleet-hub/search-rel-2026-11-05).
PARSER_SHA256 = "0ec2f7aa2cbdc1718e1424c18c8f46dac2bdafdc70faa16c9e03c7d079fdb207"
DELIM = "__UNSCOPED_SEARCH_PY__"
OPENER = f" 3<<'{DELIM}'\n"
PARSER_BLOCK = ("# >>> unscoped-search: parser begin >>>\n", "# <<< unscoped-search: parser end <<<\n")
#: The flag in r3's begin block, as (this hook's text, r12d's text).
FLAG_TEXT = [(
    'pw_cmd=$command pw_xsh="" us_ask=""\nif pw_named "$command"; then\n'
    '  us_ask=1     # D-SEARCH-REL1: the parser judges this command too (the "unscoped-search: parser" block)\n',
    'pw_cmd=$command pw_xsh=""\nif pw_named "$command"; then\n')]
PY_SEAM = base.PY_SEAM
NO_PYTHON = base.NO_PYTHON
JOB = "20261104-210909-search-rel-parser-build"
SHELLS = ["/bin/zsh", "/bin/bash"]
#: Commands with no walk for the cost checks: one the prefilter turns away, one that reaches the
#: region without a searcher, and a grep filter (a searcher name: the parser runs and passes it).
NO_WALK = ["ls -la", "git push origin x", "ps aux | grep foo"]

#: The hub's table (rows.tsv, read 2026-11-05): (cwd, command, the parser's verdict, the lane hook's
#: verdict before this change). <wt> is a job worktree, <home>/.subfleet/worktrees/<job>, which holds a
#: subfleet folder. The hub judged each half on the command text; here every row runs on disk.
ROWS = [
    ("<wt>", "find <home>/.subfleet/worktrees -name AGENTS.md", "block", "block"),
    ("<wt>", "find <wt>/.. -name AGENTS.md", "block", "block"),
    ("<wt>", "find .. -name AGENTS.md", "block", "pass"),
    ("<wt>", "cd .. && find . -name AGENTS.md", "block", "pass"),
    ("<wt>", "rg --files -g AGENTS.md ..", "block", "pass"),
    ("<wt>", "grep -rl AGENTS ..", "block", "pass"),
    ("<wt>", "find ../.. -name x", "block", "pass"),
    ("<wt>", "find ../../.. -name x", "block", "pass"),
    ("<wt>", "find . -name AGENTS.md", "pass", "pass"),
    ("<wt>", "rg -n foo subfleet", "pass", "pass"),
    ("<wt>", "grep -rn foo .", "pass", "pass"),
    ("<wt>", "cd subfleet && find .. -name x", "pass", "pass"),
    ("<wt>/subfleet", "find .. -name x", "pass", "pass"),
    ("<wt>/subfleet", "find ../.. -name x", "block", "pass"),
    ("<wt>", "find .. -maxdepth 1 -name x", "pass", "pass"),
    ("<wt>", "rg --files ../..", "block", "pass"),
    ("<wt>", "cd .. && rg --files", "block", "pass"),
]
ROW_IDS = [f"{i + 1:02d}" for i in range(len(ROWS))]

pytestmark = pytest.mark.skipif(not guard._jq_available(),
                                reason="the never-rules hook needs jq (its preflight refuses without it)")


def interpreter() -> str | None:
    """The hook's choice: /opt/homebrew/bin/python3 when it is executable, else /usr/bin/python3."""
    for path in ("/opt/homebrew/bin/python3", "/usr/bin/python3"):
        if os.access(path, os.X_OK):
            return path
    return None


needs_python = pytest.mark.skipif(interpreter() is None, reason="neither interpreter the hook takes is here")


def hook_text(hook: Path = HOOK) -> str:
    return hook.read_text(encoding="utf-8")


def embedded_parser(text: str | None = None) -> bytes:
    """The heredoc's body: the lines after its one opener, through the line before its delimiter line."""
    data = (hook_text() if text is None else text).encode("utf-8")
    opener, closer = OPENER.encode(), f"\n{DELIM}\n".encode()
    assert data.count(opener) == 1 and data.count(closer) == 1
    start = data.index(opener) + len(opener)
    return data[start:data.index(closer, start) + 1]


def loader() -> str:
    """The -c program the hook gives the interpreter."""
    found = re.findall(r"^  us_boot='([^']*)'$", hook_text(), re.M)
    assert len(found) == 1
    return found[0]


def rebuild_r12d(text: str) -> bytes:
    """The hook before D-SEARCH-REL1: the parser block removed whole, then its header and flag edits undone."""
    begin, end = PARSER_BLOCK
    start = text.index(begin)
    text = text[:start] + text[text.index(end, start) + len(end):]
    return base.undo(base.undo(text, base.SR_TEXT), FLAG_TEXT).encode("utf-8")


@pytest.fixture(scope="module")
def r12d_hook(tmp_path_factory) -> Path:
    data = rebuild_r12d(hook_text())
    assert hashlib.sha256(data).hexdigest() == base.R12D_HOOK_SHA256
    path = tmp_path_factory.mktemp("r12d-guard") / "never-rules-hook.sh"
    path.write_bytes(data)
    path.chmod(0o755)
    return path


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> dict[str, str]:
    """A real home holding a job worktree with a subfleet folder, as on a lane."""
    home = tmp_path_factory.mktemp("search-rel") / "home"
    worktree = home / ".subfleet" / "worktrees" / JOB
    (worktree / "subfleet").mkdir(parents=True)
    return {"home": str(home), "wt": str(worktree)}


def place(world: dict[str, str], row) -> tuple[str, str]:
    def fill(text: str) -> str:
        return text.replace("<wt>", world["wt"]).replace("<home>", world["home"])
    return fill(row[0]), fill(row[1])


def hook_env(home: str, *, python: str | None = None, shell: str = "/bin/zsh", log: Path | None = None,
             extra: dict | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CARPOOL_", "CODEX_GUARD_TEST_"))
           and k not in ("SUBFLEET_ROOT", "SUBFLEET_HOME", "CDPATH")}
    env.update(HOME=home, SHELL=shell, CODEX_GUARD_LOG=str(log) if log else os.devnull)
    if python is not None:
        env[PY_SEAM] = python
    env.update(extra or {})
    return env


def run(command: str, cwd: str, home: str, *, hook: Path = HOOK, **kwargs) -> str:
    """The hook's stdout for one Codex PreToolUse payload, judged on the real file system."""
    payload = json.dumps({"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}})
    out = subprocess.run(["/bin/bash", str(hook)], input=payload, capture_output=True, text=True,
                         env=hook_env(home, **kwargs), timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def verdict(stdout: str) -> str:
    return "block" if stdout.strip() else "pass"


def reason(stdout: str) -> str:
    return json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def shape(text: str) -> str:
    """A rule message with its searcher and matched root taken out: the format both judgments share."""
    text = re.sub(r"^\[unscoped-search\] Whole-tree .+? over a broad root \(", "[unscoped-search] Whole-tree <s> "
                  "over a broad root (", text)
    return re.sub(r"Matched broad root: '.*'\.$", "Matched broad root: '<r>'.", text)


def matched(stdout: str) -> str:
    """The matched root a rule message names (before the codex-lane suffix an apply_patch body adds)."""
    found = re.search(r"Matched broad root: '(.*)'\.(?: \[codex lane: .*\])?$", reason(stdout))
    assert found, reason(stdout)
    return found[1]


def named(stdout: str) -> str:
    """What a block names: its matched root, or the head of a message that names none (too long, too complex)."""
    found = re.search(r"Matched broad root: '(.*)'\.(?: \[codex lane: .*\])?$", reason(stdout))
    return found[1] if found else reason(stdout)[:80]


def where(answer: list[str]) -> str:
    """The matched root the hook names for a parser answer: as written, then resolved when it differs."""
    _, shown, resolved = answer
    return shown if resolved == shown else f"{shown} ({resolved})"


@pytest.fixture(scope="module")
def parser_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("parser") / "unscoped_search.py"
    path.write_bytes(embedded_parser())
    return path


def parser_answer(parser_file: Path, command: str, cwd: str, home: str, shell: str = "/bin/zsh") -> list[str]:
    """The embedded parser's own answer, run as the session hook runs it: three lines, or none."""
    out = subprocess.run([interpreter(), "-I", "-S", str(parser_file), cwd, home, shell], input=command,
                         capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": home})
    assert out.returncode == 0, out.stderr
    return out.stdout.splitlines()


def script(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


# ---------------------------------------------------------------- R5: the bytes

def test_r5_the_heredoc_holds_the_hub_parser_byte_for_byte():
    """The body is the hub's parser, and the block sits after the regex judgment, inside the region."""
    assert hashlib.sha256(embedded_parser()).hexdigest() == PARSER_SHA256
    text = hook_text()
    begin, end = PARSER_BLOCK
    assert text.count(begin) == 1 and text.count(end) == 1
    order = [text.index("\n# >>> unscoped-search: shared region begin"), text.index('\nif [ -n "$searcher" ]; then'),
             text.index(begin), text.index(OPENER), text.index(end),
             text.index("\n# <<< unscoped-search: shared region end <<<")]
    assert order == sorted(order)
    assert subprocess.run(["/bin/bash", "-n", str(HOOK)]).returncode == 0


def test_r5_the_interpreter_gets_the_bytes_the_command_and_the_arguments(world, tmp_path):
    """A stand-in interpreter records descriptor 3, stdin and its arguments: bash's heredoc reproduces the
    parser exactly (no expansion), and the arguments are the session hook's, after the loader."""
    dump, args, stdin = tmp_path / "fd3", tmp_path / "args", tmp_path / "stdin"
    fake = script(tmp_path / "python3", '#!/bin/sh\ncat <&3 > "$SR_FD3"\ncat > "$SR_STDIN"\n'
                                         'printf "%s\\0" "$@" > "$SR_ARGS"\n')
    command = "find .. -name AGENTS.md"
    out = run(command, world["wt"], world["home"], python=fake, shell="/bin/bash",
              extra={"SR_FD3": str(dump), "SR_ARGS": str(args), "SR_STDIN": str(stdin)})
    assert out == ""                                        # an empty answer is a pass
    assert hashlib.sha256(dump.read_bytes()).hexdigest() == PARSER_SHA256
    assert stdin.read_text() == command
    assert args.read_text().split("\0")[:-1] == ["-I", "-S", "-c", loader(), world["wt"], world["home"], "/bin/bash"]


def test_r5_the_test_seam_never_reaches_a_lane():
    from subfleet import launch_env
    assert not launch_env.inherited_name(PY_SEAM)
    assert PY_SEAM not in launch_env.build_environment(environ={"PATH": "/usr/bin:/bin", PY_SEAM: "/x"})


def test_drift_the_embedded_parser_is_the_session_hooks():
    """Skip-gated, in the manner of v1's tests/test_guard.py::DriftTests: the session hook's parser lives
    in the operator's home, so name it with SUBFLEET_CLAUDE_UNSCOPED_SEARCH (as the base module's S4 does,
    e.g. ~/.claude/hooks/unscoped_search.py) to compare; skipped when unset or absent."""
    named = os.environ.get("SUBFLEET_CLAUDE_UNSCOPED_SEARCH")
    if not named:
        pytest.skip("set SUBFLEET_CLAUDE_UNSCOPED_SEARCH to the session hook's unscoped_search.py to compare")
    path = Path(named).expanduser()
    if not path.is_file():
        pytest.skip(f"{path} does not exist here")
    session = path.read_bytes()
    assert session == embedded_parser(), (
        f"the session hook's parser ({hashlib.sha256(session).hexdigest()}) is not the embedded one "
        f"({PARSER_SHA256}): copy it between the heredoc's lines in one reviewed step and move hook_sha256")


# ---------------------------------------------------------------- R1: the hub's rows

@needs_python
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("row", ROWS, ids=ROW_IDS)
def test_r1_the_hook_gives_each_row_the_parser_verdict(world, parser_file, row, shell, tmp_path):
    cwd, command = place(world, row)
    log = tmp_path / "denials.log"
    out = run(command, cwd, world["home"], shell=shell, log=log)
    assert verdict(out) == row[2], (command, out)
    if row[2] == "pass":
        return
    assert base.rule(out) == "unscoped-search"
    _, logged_cwd, logged = log.read_text().rstrip("\n").split("\t")
    assert (logged_cwd, logged) == (cwd, command)
    regex = run(command, cwd, world["home"], shell=shell, python=NO_PYTHON)
    if row[3] == "block":
        assert out == regex                                 # the regex judgment's block, as before
        return
    answer = parser_answer(parser_file, command, cwd, world["home"], shell)
    assert len(answer) == 3, answer
    assert matched(out) == where(answer)
    assert reason(out).startswith(f"[unscoped-search] Whole-tree {answer[0]} over a broad root (")
    first_cwd, first_command = place(world, ROWS[0])    # a block the regex judgment makes
    blocked_by_regex = run(first_command, first_cwd, world["home"], shell=shell)
    assert shape(reason(out)) == shape(reason(blocked_by_regex))    # the rule's message format
    assert regex == ""


@pytest.mark.parametrize("python", ["/opt/homebrew/bin/python3", "/usr/bin/python3"])
def test_r1_each_interpreter_the_hook_may_take_runs_the_parser(world, python):
    """Homebrew's and Apple's: Apple's Python 3.9 run as python3 /dev/fd/3 ran nothing (its zip probe left the
    shared offset at the end of bash 3.2's heredoc file), so the loader reads descriptor 3 itself."""
    if not os.access(python, os.X_OK):
        pytest.skip(f"{python} is not here")
    for row in ROWS:
        cwd, command = place(world, row)
        assert verdict(run(command, cwd, world["home"], python=python)) == row[2], (python, command)


@needs_python
@pytest.mark.parametrize("row", ROWS, ids=ROW_IDS)
def test_r1_the_embedded_parser_gives_each_row_its_verdict(world, parser_file, row):
    cwd, command = place(world, row)
    for shell in SHELLS:
        answer = parser_answer(parser_file, command, cwd, world["home"], shell)
        assert verdict("\n".join(answer)) == row[2], (shell, command, answer)
        assert len(answer) in (0, 3)


@pytest.mark.parametrize("row", ROWS, ids=ROW_IDS)
def test_r1_with_no_interpreter_each_row_keeps_its_verdict_before_this_change(world, r12d_hook, row):
    cwd, command = place(world, row)
    out = run(command, cwd, world["home"], python=NO_PYTHON)
    assert verdict(out) == row[3], (command, out)
    assert out == run(command, cwd, world["home"], hook=r12d_hook)


# ---------------------------------------------------------------- fallback

@pytest.mark.parametrize("status,want", [(3, "pass"), (0, "block")])
def test_fallback_a_nonzero_exit_is_no_answer(world, tmp_path, status, want):
    """An interpreter that prints a hit: with a nonzero status the hook ignores it and the regex judgment
    decides (it passes this relative root); with status 0 the hook blocks on it, naming its root."""
    fake = script(tmp_path / "python3", f'#!/bin/sh\ncat > /dev/null\nprintf "find\\n..\\n/elsewhere\\n"\nexit {status}\n')
    out = run("find .. -name AGENTS.md", world["wt"], world["home"], python=fake)
    assert verdict(out) == want
    if want == "block":
        assert matched(out) == ".. (/elsewhere)"


def test_fallback_an_interpreter_that_cannot_run_is_none(world, tmp_path):
    """A path that does not exist, a folder (executable, but no program: bash's 126) and a file that is not
    executable: the regex judgment decides each row, as before this change."""
    (tmp_path / "not-executable").write_text("#!/bin/sh\nexit 0\n")
    for python in (NO_PYTHON, str(tmp_path), str(tmp_path / "not-executable")):
        for row in ROWS:
            cwd, command = place(world, row)
            assert verdict(run(command, cwd, world["home"], python=python)) == row[3], (python, command)


@needs_python
def test_fallback_no_answer_within_5_s_is_no_answer(world, tmp_path):
    """The interpreter's stdin stays open for 9 s after the command: the loader's alarm ends the parser at
    5 s, so the regex judgment decides (it passes this relative root); without the alarm the parser would
    answer at 9 s and block."""
    slow = script(tmp_path / "python3", f'#!/bin/bash\n{{ cat; sleep 9; }} | "{interpreter()}" "$@"\n')
    started = time.monotonic()
    out = run("find .. -name AGENTS.md", world["wt"], world["home"], python=slow)
    took = time.monotonic() - started
    assert out == "", out
    assert 5 <= took < 60, took


@needs_python
def test_fallback_the_loader_arms_a_5_s_alarm(world, parser_file):
    """The loader on its own, the parser on descriptor 3 and a stdin that never ends: SIGALRM ends it."""
    started = time.monotonic()
    proc = subprocess.Popen(["/bin/bash", "-c", 'exec "$0" -I -S -c "$1" "$2" "$3" /bin/zsh 3<"$4"', interpreter(),
                             loader(), world["wt"], world["home"], str(parser_file)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        status = proc.wait(timeout=60)
    finally:
        proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    took = time.monotonic() - started
    assert status == -signal.SIGALRM, status
    assert 4.5 <= took < 60, took


# ---------------------------------------------------------------- R4: when the interpreter starts

@needs_python
def test_r4_the_interpreter_starts_only_when_a_searcher_is_named_and_the_regex_passed(world, tmp_path):
    starts = tmp_path / "starts"
    counting = script(tmp_path / "python3", f'#!/bin/sh\nprintf x >> "$SR_STARTS"\nexec "{interpreter()}" "$@"\n')
    cases = [(world["wt"], command, 0) for command in ("ls -la", "git push origin x", "git stash list",
                                                       "echo hello && pwd")]
    cases += [(world["wt"], "ps aux | grep foo", 1), (world["wt"], "rg -n foo subfleet", 1)]
    cases += [place(world, row) + ((1 if row[3] == "pass" else 0),) for row in ROWS]
    for cwd, command, want in cases:
        starts.unlink(missing_ok=True)
        run(command, cwd, world["home"], python=counting, extra={"SR_STARTS": str(starts)})
        got = len(starts.read_text()) if starts.exists() else 0
        assert got == want, (command, got)


# ---------------------------------------------------------------- R2 and R3: the corpus

#: The parser's ``os`` in the fixture world: every absolute path is read under the folder that stands in
#: for / (the regex judgment's FS_SEAM), so both judgments see one file system and nothing real is read.
FIXTURE_OS = r'''
import os


class FixturePath(object):
    def __init__(self, fs):
        self.fs = fs

    def isdir(self, path):
        return os.path.isdir(self.fs.at(path))

    def exists(self, path):
        return os.path.exists(self.fs.at(path))

    def lexists(self, path):
        return os.path.lexists(self.fs.at(path))

    def realpath(self, path):
        real = os.path.realpath(self.fs.at(path))
        if real == self.fs.real:
            return "/"
        return real[len(self.fs.real):] if real.startswith(self.fs.real + "/") else real


class FixtureOS(object):
    X_OK = os.X_OK

    def __init__(self, root, environ):
        self.root = root.rstrip("/")
        self.real = os.path.realpath(self.root)
        self.environ = environ
        self.path = FixturePath(self)

    def at(self, path):
        return self.root + path if path.startswith("/") else path

    def access(self, path, mode):
        return os.access(self.at(path), mode)

    def scandir(self, path):
        return os.scandir(self.at(path))
'''
#: The interpreter the corpus runs give the hook: the embedded parser, read from descriptor 3 as the loader
#: reads it, run with FixtureOS; each start is recorded.
FIXTURE_RUNNER = FIXTURE_OS + r'''
import sys

args = sys.argv[1:]
assert args[:3] == ["-I", "-S", "-c"] and len(args) == 7, args
with open(os.environ["SR_RECORD"], "a") as handle:
    handle.write("asked\n")
with os.fdopen(3, "rb") as handle:
    source = handle.read()
names = {"__name__": "embedded_unscoped_search"}
exec(compile(source, "unscoped_search.py", "exec"), names)
names["os"] = FixtureOS(os.environ["CODEX_GUARD_TEST_FSROOT"],
                        {k: v for k, v in os.environ.items() if k != "SR_RECORD"})
sys.exit(names["main"](["unscoped_search.py"] + args[4:]))
'''


def fixture_parser(environ: dict[str, str]) -> dict:
    """The embedded parser loaded in this process, judging in the fixture world."""
    names = {"__name__": "embedded_unscoped_search"}
    exec(compile(embedded_parser(), "unscoped_search.py", "exec"), names)
    fs: dict = {}
    exec(FIXTURE_OS, fs)
    names["os"] = fs["FixtureOS"](str(base._FS["root"]), environ)
    return names


def denied_rows() -> list[tuple[str, str]]:
    """Each row of the base module's *_DENIED lists, from its scoped cwd and, where a row's second item is
    an absolute path (a cwd in some lists, the root named in others), from there too."""
    rows = []
    for name in sorted(n for n in dir(base) if n.isupper() and "DENIED" in n):
        for item in getattr(base, name):
            command = item[0] if isinstance(item, tuple) else item
            assert isinstance(command, str), (name, item)
            rows.append((command, base.SCOPED_CWD))
            if isinstance(item, tuple) and item[1].startswith("/") and " (glob)" not in item[1]:
                rows.append((command, item[1]))
    return rows


@pytest.fixture(scope="module")
def corpus_pairs(corpus) -> list[tuple[str, str]]:
    pairs = [(case["command"], case["cwd"]) for case in corpus["generated"]] + corpus["fixed"] + denied_rows()
    return list(dict.fromkeys(pairs))


@pytest.fixture(scope="module")
def corpus_runs(corpus_pairs, r12d_hook, tmp_path_factory) -> dict:
    """Every corpus pair judged four ways in the base module's fixture world: by this hook with no
    interpreter ("regex"), by the r12d hook ("r12d"), by this hook with the fixture interpreter ("both",
    with "asked" when it started), and by the embedded parser in this process ("parser")."""
    scratch = tmp_path_factory.mktemp("sr-corpus")
    runner = scratch / "runner.py"
    runner.write_text(FIXTURE_RUNNER)
    python = script(scratch / "python3", f'#!/bin/sh\nexec "{sys.executable}" -I -S "{runner}" "$@"\n')
    regex = base.judge_all(HOOK, corpus_pairs)
    r12d = base.judge_all(r12d_hook, corpus_pairs)
    records = scratch / "records"
    records.mkdir()
    # Kept in GUARD_DIFF_CACHE like judge_all's verdicts, under a key of their own: the hook's bytes, the fixture's
    # layout and the runner (each verdict lands as it is made, so an interrupted run loses little).
    digest = hashlib.sha256(HOOK.read_bytes() + base.FS_DIGEST.encode() + b"\0both\0" + FIXTURE_RUNNER.encode()).hexdigest()
    base._load_cache()

    def both(index, command, cwd):
        cached = base._CACHED.get((digest, cwd, command))
        if cached is not None:
            out, asked = json.loads(cached)
            return out, asked
        record = records / str(index)
        out = base.run_hook(HOOK, command, cwd, extra={PY_SEAM: python, "SR_RECORD": str(record)})
        asked = record.exists()
        base._remember(digest, (command, cwd), json.dumps([out, asked]))
        return out, asked

    judged = {}
    pool = ThreadPoolExecutor(max_workers=base.WORKERS)
    try:
        futures = {pool.submit(both, index, *pair): pair for index, pair in enumerate(corpus_pairs)}
        for future in as_completed(futures):
            judged[futures[future]] = future.result()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    environ = {k: v for k, v in os.environ.items() if not k.startswith("CARPOOL_")
               and k not in ("SUBFLEET_ROOT", "SUBFLEET_HOME", base.FS_SEAM, base.CAP_SEAM, PY_SEAM)}
    environ.update(HOME=base.FAKE_HOME, CODEX_GUARD_LOG=os.devnull, PATH="/usr/bin:/bin:" + os.environ.get("PATH", ""))
    environ.update({base.FS_SEAM: str(base._FS["root"]), PY_SEAM: python})
    names = fixture_parser(environ)
    shell = os.environ.get("SHELL", "")
    parser = {pair: names["analyze"](pair[0], pair[1], base.FAKE_HOME, shell=shell) for pair in corpus_pairs}
    return {"regex": regex, "r12d": r12d, "both": {pair: judged[pair][0] for pair in corpus_pairs},
            "asked": {pair: judged[pair][1] for pair in corpus_pairs}, "parser": parser}


def test_r3_with_no_interpreter_the_hook_is_the_r12d_hook_on_every_corpus_pair(corpus_pairs, corpus_runs):
    differ = [pair for pair in corpus_pairs if corpus_runs["regex"][pair] != corpus_runs["r12d"][pair]]
    assert not differ, differ[:10]
    blocked = sum(bool(corpus_runs["r12d"][pair].strip()) for pair in corpus_pairs)
    assert blocked > 600, blocked                   # the denied rows and the corpus's blocks are in it


def test_r2_a_pair_the_regex_judgment_blocks_keeps_its_output(corpus_pairs, corpus_runs):
    """Every pair the regex judgment blocks still blocks, with its output byte for byte; the one labelled
    exception is a deny by hf-dest (which runs after the region) of a command the parser blocks too: the
    hook then denies it as [unscoped-search]."""
    changed, relabelled = [], []
    for pair in corpus_pairs:
        regex, both = corpus_runs["regex"][pair], corpus_runs["both"][pair]
        if not regex.strip():
            continue
        if both == regex:
            continue
        if base.rule(regex) == "hf-dest" and base.rule(both) == "unscoped-search" and corpus_runs["parser"][pair]:
            relabelled.append(pair)
            continue
        changed.append((pair, base.rule(regex), base.rule(both)))
    assert not changed, changed[:10]
    report_corpus(corpus_pairs, corpus_runs, relabelled)


def test_r2_the_hook_blocks_when_either_judgment_blocks(corpus_pairs, corpus_runs):
    wrong = []
    for pair in corpus_pairs:
        regex, both = corpus_runs["regex"][pair], corpus_runs["both"][pair]
        asked, hit = corpus_runs["asked"][pair], corpus_runs["parser"][pair]
        if asked and regex.strip():
            wrong.append((pair, "asked after a regex block"))
        expect = bool(regex.strip()) or bool(asked and hit)
        if bool(both.strip()) != expect:
            wrong.append((pair, regex[:60], both[:60], asked, hit))
        elif not regex.strip() and hit and asked and matched(both) != where(list(hit)):
            wrong.append((pair, "matched", matched(both), hit))
    assert not wrong, wrong[:10]


def report_corpus(corpus_pairs, corpus_runs, relabelled) -> None:
    """The two judgments' disagreements over the corpus, each direction, to $SEARCH_REL_REPORT."""
    target = os.environ.get("SEARCH_REL_REPORT")
    rows = {"pairs": len(corpus_pairs), "relabelled_hf_dest": [list(p) for p in relabelled],
            "parser_blocks_regex_passes": [], "regex_blocks_parser_passes": [], "parser_blocks_not_asked": []}
    for pair in corpus_pairs:
        regex, hit, asked = corpus_runs["regex"][pair], corpus_runs["parser"][pair], corpus_runs["asked"][pair]
        rule = base.rule(regex) if regex.strip() else None
        if hit and not regex.strip():
            rows["parser_blocks_regex_passes" if asked else "parser_blocks_not_asked"].append(
                {"cwd": pair[1], "command": pair[0], "parser": list(hit)})
        elif rule == "unscoped-search" and not hit:
            rows["regex_blocks_parser_passes"].append(
                {"cwd": pair[1], "command": pair[0], "regex": named(regex)})
    rows["counts"] = {key: len(value) for key, value in rows.items() if isinstance(value, list)}
    rows["counts"]["regex_blocks"] = sum(1 for p in corpus_pairs if corpus_runs["regex"][p].strip()
                                         and base.rule(corpus_runs["regex"][p]) == "unscoped-search")
    rows["counts"]["parser_blocks"] = sum(1 for p in corpus_pairs if corpus_runs["parser"][p])
    if target:
        Path(target).write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- the cost (measured on request)

def child_cpu_and_wall(command: str, cwd: str, home: str, hook: Path) -> tuple[float, float]:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    run(command, cwd, home, hook=hook)
    wall = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime), wall


@needs_python
def test_cost_before_and_after(world, r12d_hook):
    """$SEARCH_REL_TIMING (a JSON path): each row and each NO_WALK command, 20 runs of each hook,
    alternating, from the row's cwd: wall time (median, worst) and child CPU (median), in ms."""
    target = os.environ.get("SEARCH_REL_TIMING")
    if not target:
        pytest.skip("set SEARCH_REL_TIMING to a JSON path to measure the hook's cost before and after")
    cases = [place(world, row) for row in ROWS] + [(world["wt"], command) for command in NO_WALK]
    rows = []
    for cwd, command in cases:
        runs = {"before": [], "after": []}
        for _ in range(20):
            for which, hook in (("before", r12d_hook), ("after", HOOK)):
                runs[which].append(child_cpu_and_wall(command, cwd, world["home"], hook))
        row = {"command": command.replace(world["wt"], "<wt>").replace(world["home"], "<home>"),
               "cwd": cwd.replace(world["wt"], "<wt>")}
        for which, values in runs.items():
            walls = [wall * 1000 for _, wall in values]
            cpus = [cpu * 1000 for cpu, _ in values]
            row[which] = {"wall_median": round(statistics.median(walls), 1), "wall_worst": round(max(walls), 1),
                          "cpu_median": round(statistics.median(cpus), 1)}
        row["added_wall_median"] = round(row["after"]["wall_median"] - row["before"]["wall_median"], 1)
        row["added_cpu_median"] = round(row["after"]["cpu_median"] - row["before"]["cpu_median"], 1)
        rows.append(row)
    Path(target).write_text(json.dumps({"interpreter": interpreter(), "load": os.getloadavg(), "rows": rows},
                                       indent=1) + "\n", encoding="utf-8")
