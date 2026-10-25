"""D-LANE-SCOPE1: the lane guard refuses whole-tree searches of ~/.subfleet.

At 15:32Z on 2026-10-24 a Codex lane ran ``rg --files -g AGENTS.md
/Users/maxghenis/.subfleet`` for 4 minutes with 12 threads at load 190-220,
walking 14,121 job folders and 1,765 job worktrees: the never-rules hook's
unscoped-search rule did not list ~/.subfleet. The hub's decision adds
~/.subfleet and each one path component directly under it to the broad roots,
spelled ``~``, ``$HOME``, ``${HOME}`` or ``/Users/<user>``, with or without a
trailing slash, as for the existing home children. A path two or more levels
under it stays scoped, and any -maxdepth / --max-depth still disarms the rule.

These tests run the reviewed overlay's hook (``private/guard/never-rules-hook.sh``)
and the hook it replaces. The old hook is rebuilt from the new one by undoing
the decision's edits (``OLD_TEXT``) and must hash to the original reviewed pin,
so the differential always compares against exactly the hook that was
installed, and any edit outside the roots, the header and the block message's
root list fails ``test_only_the_roots_the_header_and_the_message_changed``.

Invariants, each checked below for every command of the corpus:

S1 (monotone) The new hook denies every command the old hook denies, and the
   two decisions differ only where the command's search root is one of the new
   roots: in a searcher's path position, as a cd/pushd target, in a for-loop
   list feeding find, or anywhere in a command where xargs runs a searcher
   (that rule's documented over-approximation). The corpus is every command
   string in this repository's guard tests (this file; ``tests/fake/guard.py``
   and ``tests/unit/test_guard_overlay.py`` carry none but ``--fixture-deny``),
   every backticked command in the hook's own unscoped-search comments, the
   Claude hook's example table, the review's inputs, commands that hit each
   other rule, and a seeded generator of find/rg/grep commands over a root
   pool: each root in every spelling, with and without a depth bound,
   unquoted, double- and single-quoted, plain, in bash -c / sh -c / eval /
   a subshell, after cd/pushd, in a for loop, and through xargs (as an
   argument and as fed input).
S2 (scoped stays allowed) Depth-bounded searches and searches two or more
   levels under ~/.subfleet are allowed, by the old hook and by the new.
S3 (no other rule moves) For every corpus command the old hook denies under a
   rule other than unscoped-search, the new hook's output is byte-identical and
   its denial-log line identical but for the timestamp. One case is intended
   and labelled: a Bash command carrying an apply_patch body (judged by the
   hf-dest heredoc scan, which runs after the unscoped-search region) and a
   new-root search is denied by both hooks, the new one as [unscoped-search].
S4 (the Claude half agrees) The patched Claude analyzer (``unscoped_search.py``;
   it lives in the operator's home, not here, so name it with
   SUBFLEET_CLAUDE_UNSCOPED_SEARCH or these tests skip) gives the Codex hook's
   verdict on every plain explicit-path new-root case, and on the generated
   new-root cases wherever it gives the Codex hook's verdict on the same
   command over the matching existing home child (``~/.cache`` for
   ``~/.subfleet``, ``~/Library/Caches`` for ``~/.subfleet/<child>``): the
   two designs differ in known ways (quoted ``~``, a searcher right after a
   quote in sh -c / eval, xargs input, cd plus a relative path, globs, the cwd
   list) and the new roots inherit exactly those.

Also: the new hook keeps the review's allowed commands allowed and names the
matched root; the old hook passed every review input (so these roots are the
change); TRUST pins the new bytes while its identity hash and override stay put;
a denial is logged like any other.

GUARD_DIFF_SCALE (default 1) widens the generator: 1 is a representative root
pool crossed with every spelling and context; N >= 2 is the full pool with N
seeded draws per cell. GUARD_DIFF_REPORT names a file for the S1/S4 counts.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import functools
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import shlex
import subprocess

import pytest

from subfleet.guard import preflight as guard


REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "private/guard/never-rules-hook.sh"
TRUST = REPO / "private/guard/TRUST"
#: The original reviewed pair (2026-09-05): the hook these tests compare against, and the rollback.
OLD_HOOK_SHA256 = "a60d1c514d3a3bcec68c246a33c849c11fd37e1bdf60d886650cd9d6651390db"
OLD_TRUST_SHA256 = "0b9d688e6b2ca3adefdb8ad465c0fad5e6e18dfa884bc2309ff4f506c2287bab"
#: hooks_trust_hash of TRUST's reference path, before and after: the identity, not the bytes.
HOOKS_TRUST_HASH = "sha256:f97fa95f2f4d43e1f8d35cd92da4d7f3f1861497abd86916680e0189e0ec34b1"

FAKE_HOME = "/Users/tester"
SCOPED_CWD = FAKE_HOME + "/work/repo"
WORKERS = 4
SEED = 20261024

pytestmark = pytest.mark.skipif(not guard._jq_available(),
                                reason="the never-rules hook needs jq (its preflight refuses without it)")

# (text in the new hook, text in the old hook): D-LANE-SCOPE1's edits, each exactly once.
OLD_TEXT = [
    ("""#   - The unscoped-search rule is the region between the marker lines
#         (# >>> unscoped-search: shared region begin … >>>)
#         (# <<< unscoped-search: shared region end <<<)
#     once intended byte-identical to the same region of the Claude hook.
#     That 2026-08-19 port-back (guard-portback) never landed, and the Claude
#     hook no longer carries the region: its 2026-10-20 rewrite moved the
#     rule to ~/.claude/hooks/unscoped_search.py (BROAD_RE, HOME_SUBDIRS), so
#     tests/test_guard.py::DriftTests stays skip-gated and the begin marker's
#     "byte-identical … edit the Claude hook first" note is historical. The
#     region is edited in THIS file, and only on a hub decision: D-LANE-SCOPE1
#     (2026-10-24) added ~/.subfleet and each direct child of it to the broad
#     roots after one lane's `rg --files -g AGENTS.md ~/.subfleet` walked
#     14,121 job folders and 1,765 job worktrees for 4 minutes at load
#     190-220. Change a root on both sides in one reviewed step: here, and in
#     unscoped_search.py's BROAD_RE and HOME_SUBDIRS.
""", """#   - The unscoped-search rule is ONE SHARED REGION between the marker lines
#         (# >>> unscoped-search: shared region begin … >>>)
#         (# <<< unscoped-search: shared region end <<<)
#     intended byte-identical to the same region of the Claude hook. The
#     Claude half of that 2026-08-19 port-back (guard-portback) is not yet
#     installed; tests/test_guard.py::DriftTests compares the regions
#     and is skip-gated until it lands. NEVER edit the region in this file.
"""),
    ("over broad roots (8/8, 8/18 and 10/24 incidents)", "over broad roots (8/8 and 8/18 incidents)"),
    ("""#              their .subfleet child and each one path component directly
#              under it (`~/.subfleet/jobs`; `~/.subfleet/jobs/<id>` is
#              scoped), `/tmp`, `/private/tmp` — each with or without a
#              trailing slash.
""", """#              `/tmp`, `/private/tmp` — each with or without a trailing slash.
"""),
    ("|\\.local|\\.subfleet(/[^/[:space:]'\\\"]+)?))?|(/private)?/tmp\"",
     "|\\.local))?|(/private)?/tmp\""),
    ("(/, /Users, ~, ~/TheAxiomFoundation, ~/PolicyEngine, ~/.subfleet, ~/.subfleet/<child>, /tmp, caches)",
     "(/, /Users, ~, ~/TheAxiomFoundation, ~/PolicyEngine, /tmp, caches)"),
]
OLD_TRUST_TEXT = [
    ("1a736be3c968e5c1dc6bb7d63019f540b06d1cc619913e6bb8e4e057e536f951", OLD_HOOK_SHA256),
]
OLD_PROVENANCE_END = "No live preflight was run to create this pin."


def undo(text: str, edits) -> str:
    for new, old in edits:
        assert text.count(new) == 1, f"expected exactly one {new[:70]!r}"
        text = text.replace(new, old)
    return text


@pytest.fixture(scope="module")
def hooks(tmp_path_factory) -> dict[str, Path]:
    """{"new": the overlay's hook, "old": the original reviewed hook rebuilt from it}."""
    old_bytes = undo(HOOK.read_text(encoding="utf-8"), OLD_TEXT).encode("utf-8")
    assert hashlib.sha256(old_bytes).hexdigest() == OLD_HOOK_SHA256
    old = tmp_path_factory.mktemp("old-guard") / "never-rules-hook.sh"
    old.write_bytes(old_bytes)
    old.chmod(0o755)
    return {"new": HOOK, "old": old}


@pytest.fixture(scope="module")
def log_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("guard-log")


def run_hook(hook: Path, command: str, cwd: str = SCOPED_CWD, *, tool: str = "Bash",
             log: Path | None = None, home: str = FAKE_HOME) -> str:
    """The hook's stdout for one PreToolUse payload: empty to allow, the deny JSON otherwise."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CARPOOL_")}
    env.update(HOME=home, CODEX_GUARD_LOG=str(log) if log else os.devnull)
    payload = json.dumps({"tool_name": tool, "cwd": cwd, "tool_input": {"command": command}})
    out = subprocess.run(["/bin/bash", str(hook)], input=payload, capture_output=True, text=True,
                         env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def rule(stdout: str) -> str | None:
    """The denying rule's tag (``unscoped-search``, ``local-main``, ...), or None for an allow."""
    if not stdout.strip():
        return None
    reason = json.loads(stdout)["hookSpecificOutput"]
    assert reason["permissionDecision"] == "deny"
    match = re.match(r"\[([a-z-]+)\]", reason["permissionDecisionReason"])
    assert match, reason
    return match[1]


def matched_root(stdout: str) -> str:
    return re.search(r"Matched broad root: '(.*)'\.$", json.loads(stdout)["hookSpecificOutput"]
                     ["permissionDecisionReason"])[1]


_JUDGED: dict[tuple[str, str, str], str] = {}


def judge_all(hook: Path, cases) -> dict[tuple[str, str], str]:
    """Run ``hook`` over (command, cwd) pairs, four at a time (memoized); stdout per pair."""
    wanted = list(dict.fromkeys(cases))
    missing = [case for case in wanted if (str(hook), *case) not in _JUDGED]
    if missing:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for case, out in zip(missing, pool.map(lambda c: run_hook(hook, c[0], c[1]), missing)):
                _JUDGED[(str(hook), *case)] = out
    return {case: _JUDGED[(str(hook), *case)] for case in wanted}


def judged(hook: Path, commands, cwd: str = SCOPED_CWD) -> dict[str, str]:
    """stdout per command, all judged from ``cwd``."""
    outs = judge_all(hook, [(command, cwd) for command in commands])
    return {command: outs[(command, cwd)] for command in commands}


# ---------------------------------------------------------------- the review's inputs

REVIEW_DENIED = [
    # (command, the root the block message must name)
    ("rg --files -g AGENTS.md /Users/maxghenis/.subfleet", "/Users/maxghenis/.subfleet"),
    ("rg -l foo ~/.subfleet/jobs", "~/.subfleet/jobs"),
    ("find $HOME/.subfleet -name AGENTS.md", "$HOME/.subfleet"),
    ('grep -r x "${HOME}/.subfleet/worktrees/"', "$HOME/.subfleet/worktrees/"),
    # the live deny test's probe (the head bounds the damage if the guard is not live)
    ("rg --files ~/.subfleet | head -n 1", "~/.subfleet"),
]
REVIEW_ALLOWED = [
    "find ~/.subfleet -maxdepth 1 -name lanes.json",
    "rg foo ~/.subfleet/jobs/20261024-115712-x/a1",
    'rg ".subfleet" src/',
]


def test_review_inputs_are_denied(hooks):
    new = judged(hooks["new"], [command for command, _ in REVIEW_DENIED])
    old = judged(hooks["old"], [command for command, _ in REVIEW_DENIED])
    for command, root in REVIEW_DENIED:
        assert rule(new[command]) == "unscoped-search", command
        assert matched_root(new[command]) == root
        assert "~/.subfleet, ~/.subfleet/<child>" in new[command]
        assert rule(old[command]) is None, "the old hook passed every review input"


def test_review_scoped_inputs_stay_allowed(hooks):
    for which in ("new", "old"):
        for command, out in judged(hooks[which], REVIEW_ALLOWED).items():
            assert rule(out) is None, (which, command)


HOME_SPELLINGS = ["~", "$HOME", "${HOME}", "/Users/maxghenis", FAKE_HOME]
CHILDREN = ["jobs", "worktrees", "archive", "lanes", "conversations", "guard-cache", "x_y-z.1"]


def test_every_spelling_of_subfleet_is_a_root(hooks):
    commands = [search.format(quote.format(spelling + "/.subfleet"))
                for spelling in HOME_SPELLINGS for quote in ("{}", '"{}"')
                for search in ("find {} -name AGENTS.md", "rg --files {}", "rg -l foo {}", "grep -rn x {}")]
    for command, out in judged(hooks["new"], commands).items():
        assert rule(out) == "unscoped-search", command


def test_each_direct_child_is_a_root(hooks):
    searches = ("rg -l foo {}", "find {} -type f", "grep -r x {}")
    expected = {}
    for i, child in enumerate(CHILDREN):
        for j, spelling in enumerate(HOME_SPELLINGS):
            command = searches[(i + j) % len(searches)].format(f"{spelling}/.subfleet/{child}")
            expected[command] = f"{spelling.replace('${HOME}', '$HOME')}/.subfleet/{child}"
    for command, out in judged(hooks["new"], list(expected)).items():
        assert rule(out) == "unscoped-search", command
        assert matched_root(out) == expected[command]


TRAILING = ["~/.subfleet/", "$HOME/.subfleet/", "${HOME}/.subfleet/", "/Users/maxghenis/.subfleet/",
            "~/.subfleet/jobs/", "$HOME/.subfleet/worktrees/", "/Users/maxghenis/.subfleet/archive/"]


def test_trailing_slash_spellings_are_denied(hooks):
    commands = [search.format(path) for path in TRAILING
                for search in ("rg --files {}", "find {} -name x", "grep -r x {}", 'rg foo "{}"')]
    for command, out in judged(hooks["new"], commands).items():
        assert rule(out) == "unscoped-search", command


DEEP = ["~/.subfleet/jobs/20261024-115712-x", "~/.subfleet/jobs/20261024-115712-x/",
        "$HOME/.subfleet/worktrees/20261024-115939-guardscope-build",
        "/Users/maxghenis/.subfleet/jobs/20261024-115712-x/a1",
        "~/.subfleet/jobs/20261024-115712-x/a1/deliverable.md", "${HOME}/.subfleet/archive/2026-10/x"]


def test_two_or_more_levels_under_subfleet_stay_scoped(hooks):
    """S2, by example: one job's folder is a scoped search."""
    commands = [search.format(path) for path in DEEP
                for search in ("rg --files {}", "find {} -name x", "grep -r x {}", "rg foo {} | head",
                               "cd {} && rg foo", "git ls-files | xargs rg foo {}",
                               "bash -c 'find {} -name x'")]
    for which in ("new", "old"):
        for command, out in judged(hooks[which], commands).items():
            assert rule(out) is None, (which, command)


NEAR_MISSES = [
    "rg foo ~/.subfleetx", "find ~/.subfleet-old -name x", "rg foo ~/src/.subfleet",
    "rg foo .subfleet/jobs", "find .subfleet -name x", "rg foo /Users/a/b/.subfleet",
    "rg ~/.subfleet src/", "grep -rn '~/.subfleet' src", "git log --grep .subfleet",
    "rg --max-depth 3 foo ~/.subfleet", "find ~/.subfleet/jobs -maxdepth 2 -name AGENTS.md",
    'echo "rg --files ~/.subfleet" >> notes.md', "git commit -m 'guard ~/.subfleet roots: rg x ~/.subfleet'",
]


def test_near_misses_patterns_prose_and_bounded_searches_stay_allowed(hooks):
    for command, out in judged(hooks["new"], NEAR_MISSES).items():
        assert rule(out) is None, command


def test_a_new_root_denial_is_logged_like_any_other(hooks, tmp_path):
    log = tmp_path / "guard-denials.log"
    command = "rg --files -g AGENTS.md /Users/maxghenis/.subfleet"
    assert rule(run_hook(hooks["new"], command, log=log)) == "unscoped-search"
    stamp, cwd, logged = log.read_text().rstrip("\n").split("\t")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4}", stamp)
    assert (cwd, logged) == (SCOPED_CWD, command)


# ---------------------------------------------------------------- TRUST

def test_trust_pins_the_new_hook_bytes():
    trust = json.loads(TRUST.read_text())
    assert trust["hook_sha256"] == hashlib.sha256(HOOK.read_bytes()).hexdigest()
    guard.load_guard(hook_path=HOOK, trust_path=TRUST)
    assert "D-LANE-SCOPE1" in trust["provenance"]
    assert trust["provenance"].endswith(
        "Installed only after the hub's review and a live hooks/list preflight.")


def test_trust_changed_only_the_hook_hash_and_the_appended_provenance():
    text = undo(TRUST.read_text(encoding="utf-8"), OLD_TRUST_TEXT)
    head, sep, _ = text.partition(OLD_PROVENANCE_END)
    old = head + sep + '"\n}\n'
    assert hashlib.sha256(old.encode("utf-8")).hexdigest() == OLD_TRUST_SHA256


def test_identity_hash_and_override_do_not_depend_on_the_hook_bytes(hooks, tmp_path):
    """trusted_hash hashes the hook's identity (event, matcher, type, command path,
    timeout, async flag, status message), not its bytes: the same path keeps its
    hash across the byte change, so TRUST's hooks_trust_hash and override stay put."""
    trust = json.loads(TRUST.read_text())
    assert trust["hooks_trust_hash"] == HOOKS_TRUST_HASH
    assert guard.hooks_trust_hash(trust["reference_hook_path"]) == HOOKS_TRUST_HASH
    assert guard.override_string(trust["reference_hook_path"]) == trust["override"]
    installed = tmp_path / "guard/never-rules-hook.sh"
    installed.parent.mkdir()
    seen = set()
    for which in ("old", "new"):
        installed.write_bytes(hooks[which].read_bytes())
        seen.add((guard.hooks_trust_hash(installed), guard.override_string(installed)))
    assert len(seen) == 1


def test_only_the_roots_the_header_and_the_message_changed():
    """Decision 2's fence: undoing exactly OLD_TEXT gives the original reviewed bytes."""
    old = undo(HOOK.read_text(encoding="utf-8"), OLD_TEXT).encode("utf-8")
    assert hashlib.sha256(old).hexdigest() == OLD_HOOK_SHA256


# ---------------------------------------------------------------- the corpus

def _hook_comment_examples() -> list[str]:
    """Every backticked command in the hook's unscoped-search comments."""
    text = HOOK.read_text(encoding="utf-8")
    region = text[text.index("\n# >>> unscoped-search: shared region begin"):
                  text.index("\n# <<< unscoped-search: shared region end <<<")]
    comments = " ".join(line.strip().lstrip("#").strip() for line in region.splitlines()
                        if line.strip().startswith("#"))
    found = []
    for span in re.findall(r"`([^`]+)`", comments):
        span = span.replace("…", "").replace("...", "").strip()
        if re.search(r"(^|[\s;&|(])(find|rg|grep|cd|pushd|xargs)\s", span + " "):
            found.append(span)
    return found


#: The Claude hook's example table (~/.claude/tests/test_unscoped_search.py, 2026-10-20), as (cwd, command).
CLAUDE_EXAMPLES = [
    ("/tmp/work", "find / -name x"), ("/tmp/work", "bfs / -name x"), ("/tmp/work", "fd x /"),
    ("/tmp/work", "rg foo /Users"), ("/tmp/work", "find / -maxdepth 2 -name x"),
    ("/tmp/work", "find ~/reviews/foo -name x"), ("/tmp/work", "ps | grep -oE 'x'"),
    (FAKE_HOME + "/PolicyEngine/policyengine-us/policyengine_us", "find . -name x"),
    ("/Applications", "find / -name review_r3_8916273ec.md -not -path */proc/*"),
    ("/Applications", "ps -Ao stat=,pid=,args= | awk '$1 ~ /^[RU]/' | sed -E "
                      "'s#^[^ ]+ +[0-9]+ ##' | grep -oE '(/\\.subfleet/worktrees/[^/ ]+|"
                      "_worktrees/[^/ ]+|subfleetd)' | sort | uniq -c | sort -rn | head -15"),
    ("/tmp/work", "find '/Volumes/Macintosh HD' -name x"),
    ("/tmp/work", "find /System/Volumes/Data/Users/tester/PolicyEngine -name x"),
    ("/tmp/work", "find -name x /"), ("/tmp/work", "find /* -name x"),
    ("/tmp/work", "find ~/{PolicyEngine,TheAxiomFoundation} -name x"), ("/tmp/work", "find $HOME -name x"),
    ("/tmp/work", "find ${HOME}/PolicyEngine/ -name x"), ("/tmp/work", "env -C / find . -name x"),
    ("/tmp/work", "find src -exec grep -r foo / \\;"), ("/tmp/work", "diff <(find / -name a) b"),
    ("/tmp/work", "bash -euo pipefail -c 'find / -name x'"), (FAKE_HOME, "rg foo"),
    (FAKE_HOME, "rg --files | head"), (FAKE_HOME, "grep -r foo"), (FAKE_HOME, "ps | grep -r foo"),
    (FAKE_HOME, "ps aux | rg foo"), (FAKE_HOME, "rg foo < log.txt"),
    (FAKE_HOME, "cd ~/PolicyEngine/policyengine-us && rg foo"),
    (FAKE_HOME, "(cd / && ls); cd src && find . -name x"),
    (FAKE_HOME + "/PolicyEngine/policyengine-us/policyengine_us", "find ../../.. -name x"),
    ("/tmp/work", "rg /opt src/"), ("/tmp/work", "grep -rn /var src"), ("/tmp/work", "grep -r -e foo /var"),
    ("/tmp/work", "grep -d recurse foo /Users"), ("/tmp/work", "grep foo /Users"),
    ("/tmp/work", "rg foo ~/*.md"), ("/tmp/work", 'find "$TMPDIR" -name x'), ("/tmp/work", "find '~' -name x"),
    ("/tmp/work", "xargs grep -r foo"), ("/tmp/work", "ssh devbox 'find / -name x'"),
    ("/tmp/work", "command -v find"), ("/tmp/work", "du -sh / ; find src -name x"),
    ("/tmp/work", "rg foo src | awk -F / '{print $1}'"),
    ("/tmp/work", "cat > n.md <<'EOF'\nfind / -name x\nEOF\nfind / -name y"),
    ("/tmp/work", "for d in /private/tmp ~/Library/Caches ~/.cache; do "
                  "find $d -xdev -type f -mmin -60 -size +200M; done"),
    ("/tmp/work", "D=~/PolicyEngine; grep -rn foo $D"), ("/tmp/work", 'W="$HOME/TheAxiomFoundation"; cd $W && rg foo'),
    ("/tmp/work", "W=~/PolicyEngine/policyengine-us; cd $W && rg foo"), ("/tmp/work", "export R=/; find $R -name x"),
    ("/tmp/work", "for d in ~/PolicyEngine/*; do grep -r foo $d; done"),
    ("/tmp/work", "for f in $(ls); do grep -r foo $f; done"), ("/tmp/work", "D=/ find src -name x"),
    ("/tmp/work", "D=/; while read -r D; do find $D -name x; done < list.txt"),
    ("/tmp/work", "eval 'cd /'; find . -name x"), ("/tmp/work", "bash -c 'cd /'; find . -name x"),
    (FAKE_HOME, "rg -n --no-heading -iE '12\\.5|outperform' ~/obsidian-vault/exports/public/ | head"),
    (FAKE_HOME, "rg -E utf-16le foo"), ("/tmp/work", "rg -iE foo /"),
]

#: Commands that hit each other rule (cwd "plain"/"main"/"worktrees"/"corpus" names a fixture repo).
OTHER_RULES = [
    ("plain", "curl -s https://api.telegram.org/bot123:abc/getUpdates", "tg-getupdates"),
    ("plain", "python -c 'bot.get_updates()'", "tg-getupdates"),
    ("plain", "security dump-keychain -d", "keychain-dump"),
    ("plain", "gh variable set AXIOM_RELEASE_PUBLIC_KEY --org TheAxiomFoundation --body x", "trust-root"),
    ("plain", "gh pr merge 5 --squash -R TheAxiomFoundation/axiom-corpus", "corpus-squash"),
    ("corpus", "git push origin main", "corpus-push"),
    ("corpus", "git push", "corpus-push"),
    ("worktrees", "git stash", "stash-shared"),
    ("worktrees", "git stash push -m wip", "stash-shared"),
    ("main", "git checkout -b feature main", "local-main"),
    ("main", "git switch -c feature", "local-main"),
    ("plain", "cd policyengine-uk-data && apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: "
              "upload.py\n@@\n+repo_id = 'other/x'\n*** End Patch\nEOF", "hf-dest"),
    # near misses: allowed by both
    ("worktrees", "git stash list", None), ("main", "git checkout -b feature origin/main", None),
    ("plain", "gh variable list", None), ("plain", "security find-generic-password -s x", None),
]
#: Appended to every OTHER_RULES command for the combination cases.
NEW_ROOT_SEARCH = "rg --files ~/.subfleet"


@pytest.fixture(scope="module")
def repos(tmp_path_factory) -> dict[str, str]:
    """Scratch git repositories the cwd-dependent rules read (none touches a real repo)."""
    base = tmp_path_factory.mktemp("rule-repos")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def git(path, *args):
        subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True, env=env)

    def repo(name, origin=None):
        path = base / name
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        git(path, "commit", "-q", "--allow-empty", "-m", "init")
        git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
        if origin:
            git(path, "remote", "add", "origin", origin)
        return path

    plain = base / "plain"
    plain.mkdir()
    worktrees = repo("worktrees")
    git(worktrees, "worktree", "add", "-q", str(base / "worktrees-second"), "-b", "second")
    return {"plain": str(plain), "main": str(repo("main")), "worktrees": str(worktrees),
            "corpus": str(repo("corpus", "https://github.com/TheAxiomFoundation/axiom-corpus.git"))}


# ---------------------------------------------------------------- the generator

#: Home-relative roots: (path under home, kind). kind: "old" root, "new" root,
#: "deep" (two or more levels under ~/.subfleet), "none" (never a root).
HOME_ROOTS = [
    ("", "old"), ("TheAxiomFoundation", "old"), ("RulesFoundation", "old"), ("PolicyEngine", "old"),
    ("Library/Caches", "old"), (".cache", "old"), (".local", "old"),
    (".subfleet", "new"), (".subfleet/jobs", "new"), (".subfleet/worktrees", "new"),
    (".subfleet/archive", "new"), (".subfleet/lanes", "new"), (".subfleet/conversations", "new"),
    (".subfleet/guard-cache", "new"), (".subfleet/subfleetd.log", "new"),
    (".subfleet/jobs/20261024-115712-x", "deep"),
    (".subfleet/worktrees/20261024-115939-guardscope-build", "deep"),
    (".subfleet/jobs/20261024-115712-x/a1", "deep"),
    (".subfleet/jobs/20261024-115712-x/a1/deliverable.md", "deep"),
    ("PolicyEngine/policyengine-us", "none"), ("reviews/subfleet-hub", "none"), ("Library", "none"),
    (".subfleetx", "none"), (".subfleet-old", "none"), ("src/.subfleet", "none"), ("src/.subfleet/jobs", "none"),
]
ABS_ROOTS = [("/", "old"), ("/*", "old"), ("/Users", "old"), ("/tmp", "old"), ("/private/tmp", "old"),
             ("/opt/homebrew/Cellar/x", "none"), ("src", "none"), ("./tests", "none"),
             (".subfleet", "none"), (".subfleet/jobs", "none")]
#: GUARD_DIFF_SCALE=1: one root of each shape (home-relative, then "abs:" for ABS_ROOTS) ...
SMALL_POOL = {"", "PolicyEngine", "Library/Caches", ".cache", ".subfleet", ".subfleet/jobs",
              ".subfleet/subfleetd.log", ".subfleet/jobs/20261024-115712-x",
              ".subfleet/jobs/20261024-115712-x/a1", ".subfleetx", "src/.subfleet",
              "abs:/", "abs:/tmp", "abs:src", "abs:.subfleet"}
#: ... and these contexts (every one at GUARD_DIFF_SCALE >= 2).
SMALL_CONTEXTS = ["plain", "bash-c", "sh-c-dq", "eval", "head", "xargs-arg",
                  "cd-find", "cd-rg", "for", "xargs-feed"]
#: S4's analog: the existing home child each new root is compared with.
ANALOG = {".subfleet": ".cache"}
CHILD_ANALOG = "Library/Caches"

SEARCHES = {  # name -> (template, root position)
    "find-name": ("find {R} -name AGENTS.md", "path"),
    "find-type": ("find {R} -type f -name '*.md'", "path"),
    "find-L": ("find -L {R} -name x", "path"),
    "rg": ("rg foo {R}", "path"),
    "rg-l": ("rg -l foo {R}", "path"),
    "rg-e": ("rg -n -e foo {R}", "path"),
    "rg-files": ("rg --files {R}", "path"),
    "rg-files-g": ("rg --files -g AGENTS.md {R}", "path"),
    "rg-glob": ("rg -g '*.md' -i foo {R}", "path"),
    "rg-type": ("rg -t py foo {R}", "path"),
    "grep-r": ("grep -r x {R}", "path"),
    "grep-rn": ("grep -rn x {R}", "path"),
    "grep-R": ("grep -R --include=*.py x {R}", "path"),
    "grep-long": ("grep --recursive -l x {R}", "path"),
    "rg-pattern": ("rg {R} src/", "pattern"),
    "grep-pattern": ("grep -rn {R} src", "pattern"),
}
DEPTH = {"find": (" -maxdepth 2", "end"), "rg": ("--max-depth 2 ", "after"), "grep": ("--max-depth=2 ", "after")}


def bound(search: str) -> str:
    tool = search.split()[0]
    text, where = DEPTH[tool]
    return search + text if where == "end" else search.replace(tool + " ", tool + " " + text, 1)


def search_context(name):
    """Contexts that wrap one search command S."""
    return {
        "plain": lambda s: s,
        "bash-c": lambda s: "bash -c " + shlex.quote(s),
        "sh-c-dq": lambda s: 'sh -c "' + s.replace('"', '\\"') + '"',
        "zsh-lc": lambda s: "zsh -lc " + shlex.quote(s),
        "eval": lambda s: "eval " + shlex.quote(s),
        "subshell": lambda s: "(" + s + ")",
        "timeout": lambda s: "timeout 30 " + s,
        "head": lambda s: s + " | head -n 5",
        "after": lambda s: "ls; " + s,
        "xargs-arg": lambda s: "git ls-files | xargs " + s,
    }.get(name)


ROOT_CONTEXTS = {  # contexts built around the root itself: template, bounded template
    "cd-find": ("cd {R} && find . -name AGENTS.md", "cd {R} && find . -maxdepth 1 -name AGENTS.md"),
    "cd-rg": ("cd {R} && rg foo", "cd {R} && rg --max-depth 1 foo"),
    "pushd": ("pushd {R} && rg --files", "pushd {R} && rg --files --max-depth 1"),
    "for": ("for d in {R}; do find $d -name x; done", "for d in {R}; do find $d -maxdepth 1 -name x; done"),
    "xargs-feed": ("echo {R} | xargs rg foo", "echo {R} | xargs rg --max-depth 1 foo"),
    "xargs-feed-find": ("printf '%s\\n' {R} | xargs -n1 find", "printf '%s\\n' {R} | xargs -n1 find -maxdepth 1"),
}
CONTEXTS = ["plain", "bash-c", "sh-c-dq", "zsh-lc", "eval", "subshell", "timeout", "head", "after",
            "xargs-arg", *ROOT_CONTEXTS]


def spell(suffix: str, spelling: str | None, slash: bool, quote: str) -> str:
    if spelling is None:
        text = suffix
    else:
        text = spelling + ("/" + suffix if suffix else "")
    if slash and text not in ("/", "/*"):
        text += "/"
    if quote == "dq":
        return '"' + text + '"'
    if quote == "sq":
        return "'" + text + "'"
    return text


def generate(scale: int) -> list[dict]:
    """The seeded generator. Scale 1: SMALL_POOL, every spelling plain and SMALL_CONTEXTS
    spread over the spellings; scale N >= 2: the full pool, every (root, spelling,
    context) cell, N - 1 seeded draws each (searcher, quoting, slash, depth bound)."""
    rng = random.Random(SEED)
    pool = [(suffix, kind, True) for suffix, kind in HOME_ROOTS]
    pool += [(suffix, kind, False) for suffix, kind in ABS_ROOTS]
    contexts = CONTEXTS
    if scale <= 1:
        pool = [(s, k, h) for s, k, h in pool if (s if h else "abs:" + s) in SMALL_POOL]
        contexts = SMALL_CONTEXTS
    cases = []
    for suffix, kind, homed in pool:
        spellings = HOME_SPELLINGS if homed else [None]
        for s_index, spelling in enumerate(spellings):
            chosen = contexts
            if scale <= 1 and homed:
                # Every spelling plain, and the other contexts spread over the spellings,
                # so each root still meets every context once.
                step = -(-(len(contexts) - 1) // len(spellings))
                chosen = ["plain", *contexts[1:][s_index * step:(s_index + 1) * step]]
            for context in chosen:
                for _ in range(max(1, scale - 1)):
                    case = draw(rng, suffix, kind, spelling, context)
                    cases.append(case)
                    if kind == "new" and case["reads_root"]:
                        analog = ANALOG.get(suffix, CHILD_ANALOG)
                        cases.append(dict(case, command=case["command_for"](analog),
                                          kind="old", analog_of=case["command"], suffix=analog))
    for case in cases:
        case.pop("command_for")
    return cases


def draw(rng, suffix, kind, spelling, context) -> dict:
    slash = rng.random() < 0.4
    quote = rng.choice(["bare", "bare", "dq", "sq"])
    bounded = rng.random() < 0.25
    if context in ROOT_CONTEXTS:
        template = ROOT_CONTEXTS[context][bounded]
        position = "path"
        build = lambda sfx: template.replace("{R}", spell(sfx, spelling, slash, quote))  # noqa: E731
    else:
        name = rng.choice(sorted(SEARCHES))
        template, position = SEARCHES[name]
        if bounded:
            template = bound(template)
        wrap = search_context(context)
        build = lambda sfx: wrap(template.replace("{R}", spell(sfx, spelling, slash, quote)))  # noqa: E731
        if context == "xargs-arg":
            position = "xargs"      # a root anywhere counts when xargs runs a searcher
    command = build(suffix)
    return {"command": command, "cwd": SCOPED_CWD, "kind": kind, "suffix": suffix, "spelling": spelling,
            "context": context, "bounded": bounded, "position": position, "quote": quote, "slash": slash,
            "reads_root": position in ("path", "xargs") and not bounded, "command_for": build}


#: Implicit "." searches judged by the cwd rule, whose list D-LANE-SCOPE1 left alone (see the report).
CWD_CASES = [(cmd, cwd) for cwd in (FAKE_HOME, FAKE_HOME + "/PolicyEngine", FAKE_HOME + "/.subfleet",
                                    FAKE_HOME + "/.subfleet/jobs", FAKE_HOME + "/.subfleet/jobs/20261024-115712-x",
                                    SCOPED_CWD)
             for cmd in ("rg --files .", "find . -name AGENTS.md", "grep -r x .")]

NEW_ROOT_TOKEN = re.compile(r"""(^|[\s'"=(])(~|\$HOME|\$\{HOME\}|/Users/[^/\s'"]+)/\.subfleet(/[^/\s'"|;&)<>]+)?/?(?=['"]?([\s|;&)]|$))""")


def mentions_new_root(command: str) -> bool:
    return bool(NEW_ROOT_TOKEN.search(command))


def scale() -> int:
    return max(1, int(os.environ.get("GUARD_DIFF_SCALE", "1")))


@pytest.fixture(scope="module")
def corpus(repos) -> dict:
    generated = generate(scale())
    fixed = [(c, SCOPED_CWD) for c, _ in REVIEW_DENIED] + [(c, SCOPED_CWD) for c in REVIEW_ALLOWED]
    fixed += [(c, SCOPED_CWD) for c in _hook_comment_examples()]
    fixed += [(c, SCOPED_CWD) for c in ("--fixture-deny",)]    # tests/fake/guard.py, test_guard_overlay.py
    fixed += [(cmd, cwd) for cwd, cmd in CLAUDE_EXAMPLES] + CWD_CASES
    fixed += [(cmd, repos[where]) for where, cmd, _ in OTHER_RULES]
    fixed += [(cmd + "; " + NEW_ROOT_SEARCH, repos[where]) for where, cmd, _ in OTHER_RULES]
    return {"generated": generated, "fixed": list(dict.fromkeys(fixed))}


@pytest.fixture(scope="module")
def verdicts(hooks, corpus) -> dict:
    pairs = [(case["command"], case["cwd"]) for case in corpus["generated"]] + corpus["fixed"]
    return {which: judge_all(hooks[which], pairs) for which in ("old", "new")}


def report(name: str, data: dict) -> None:
    target = os.environ.get("GUARD_DIFF_REPORT")
    if target:
        path = Path(target)
        current = json.loads(path.read_text()) if path.exists() else {}
        current[name] = data
        path.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")


# ---------------------------------------------------------------- S1, S2, S3

def test_s1_the_new_hook_is_monotone_and_moves_only_on_new_roots(corpus, verdicts):
    old, new = verdicts["old"], verdicts["new"]
    loosened, stray = [], []
    counts = {"commands": 0, "old_denied": 0, "new_denied": 0, "newly_denied": 0,
              "newly_denied_generated": 0, "newly_denied_fixed": 0}
    for case in corpus["generated"]:
        key = (case["command"], case["cwd"])
        o, n = rule(old[key]), rule(new[key])
        if o and n != o:
            loosened.append(key)
        if o != n and not (case["kind"] == "new" and case["reads_root"] and n == "unscoped-search"):
            stray.append((key, case["kind"], case["position"], case["bounded"]))
        counts["newly_denied_generated"] += o is None and n is not None
    for key in corpus["fixed"]:
        o, n = rule(old[key]), rule(new[key])
        if o and n is None:
            loosened.append(key)
        if o != n and not (n == "unscoped-search" and mentions_new_root(key[0])):
            stray.append((key, "fixed", "?", "?"))
        counts["newly_denied_fixed"] += o is None and n is not None
    keys = set(old)
    counts["commands"] = len(keys)
    counts["old_denied"] = sum(rule(old[k]) is not None for k in keys)
    counts["new_denied"] = sum(rule(new[k]) is not None for k in keys)
    counts["newly_denied"] = sum(rule(old[k]) is None and rule(new[k]) is not None for k in keys)
    counts["generated"] = len(corpus["generated"])
    counts["fixed"] = len(corpus["fixed"])
    counts["scale"] = scale()
    report("S1", counts)
    assert not loosened, loosened[:10]
    assert not stray, stray[:10]
    assert counts["newly_denied"] > 0


def test_s1_the_new_roots_get_the_existing_home_childrens_protection(corpus, verdicts):
    """Wherever the old hook denies a command over ~/.cache or ~/Library/Caches, the new
    hook denies the same command over ~/.subfleet or ~/.subfleet/<child>."""
    new, old = verdicts["new"], verdicts["old"]
    by_command = {case["command"]: case for case in corpus["generated"]}
    missing, allowed_new = [], []
    for case in corpus["generated"]:
        if "analog_of" not in case:
            continue
        target = by_command[case["analog_of"]]
        if rule(old[(case["command"], case["cwd"])]) and not rule(new[(target["command"], target["cwd"])]):
            missing.append(target["command"])
    for case in corpus["generated"]:
        if case["kind"] == "new" and case["reads_root"] and not rule(new[(case["command"], case["cwd"])]):
            allowed_new.append(case["command"])
    report("S1-protection", {"new_root_reading_cases": sum(c["kind"] == "new" and c["reads_root"]
                                                          for c in corpus["generated"]),
                             "allowed_by_new_hook": sorted(set(allowed_new))})
    assert not missing, missing[:10]


def test_s2_bounded_and_deep_searches_stay_allowed(corpus, verdicts):
    checked = 0
    for case in corpus["generated"]:
        if case["bounded"] or case["kind"] == "deep":
            key = (case["command"], case["cwd"])
            assert rule(verdicts["old"][key]) is None, key
            assert rule(verdicts["new"][key]) is None, key
            checked += 1
    assert checked > 0
    report("S2", {"checked": checked})


def test_s3_other_rules_give_identical_output(hooks, repos, tmp_path):
    compared = 0
    for where, command, expected in OTHER_RULES:
        for text in (command, command + "; " + NEW_ROOT_SEARCH):
            logs = {which: tmp_path / f"{which}-{compared}.log" for which in ("old", "new")}
            outs = {which: run_hook(hooks[which], text, repos[where], log=logs[which]) for which in logs}
            if text == command:
                assert rule(outs["old"]) == expected, (command, outs["old"])
            if rule(outs["old"]) in (None, "unscoped-search"):
                continue
            compared += 1
            if expected == "hf-dest" and text != command:
                # Intended: the hf-dest heredoc scan runs after the unscoped-search region.
                assert rule(outs["new"]) == "unscoped-search", text
                continue
            assert outs["new"] == outs["old"], text
            lines = {w: [line.split("\t", 1)[1] for line in p.read_text().splitlines()] for w, p in logs.items()}
            assert lines["new"] == lines["old"], text
    assert compared >= 2 * sum(1 for _, _, expected in OTHER_RULES if expected)


def test_s3_apply_patch_payloads_are_judged_identically(hooks, repos):
    patch = ("*** Begin Patch\n*** Update File: policyengine-uk-data/upload.py\n@@\n"
             "+repo_id = 'other/x'\n*** End Patch\n")
    benign = "*** Begin Patch\n*** Add File: notes.md\n+rg --files ~/.subfleet\n*** End Patch\n"
    for text, expected in ((patch, "hf-dest"), (benign, None)):
        outs = [run_hook(hooks[w], text, repos["plain"], tool="apply_patch") for w in ("old", "new")]
        assert outs[0] == outs[1] and rule(outs[0]) == expected


def test_the_cwd_list_is_unchanged(corpus, verdicts):
    """D-LANE-SCOPE1 changed the root patterns, not the cwd rule's list: a "."
    search sitting in ~/.subfleet is judged as before (a known gap, in the report)."""
    for key in CWD_CASES:
        assert rule(verdicts["new"][key]) == rule(verdicts["old"][key]), key
    assert rule(verdicts["new"][("rg --files .", FAKE_HOME)]) == "unscoped-search"
    assert rule(verdicts["new"][("rg --files .", FAKE_HOME + "/.subfleet")]) is None


@pytest.mark.documented_limitation
def test_known_limits_the_new_roots_share_with_the_old_ones(hooks):
    """Not D-LANE-SCOPE1's to change (decision 2 fences the edit to the roots); listed in
    docs/reports/2026-10-24-guard-subfleet-root.md for the hub.

    - A searcher right after a quote is never detected (the searcher regexes want a space,
      ;, &, |, ( or the line start before it), so a whole sh -c / eval sub-script that
      starts with it passes, for / as for ~/.subfleet.
    - A trailing glob is text, not the parent: ``~/.subfleet/jobs/*`` passes, as ``~/*`` does.
    - The cwd list is unchanged: ``rg --files .`` sitting in ~/.subfleet passes.
    - Intended: one path component under ~/.subfleet is a root even when it is a file
      (``rg foo ~/.subfleet/subfleetd.log``); non-recursive grep or --max-depth reads it.
    """
    allowed = ["bash -c 'find / -name x'", "bash -c 'rg --files ~/.subfleet'",
               "eval 'grep -r x ~/.subfleet/jobs'", "rg foo ~/.subfleet/jobs/*", "rg foo ~/*"]
    for which in ("old", "new"):
        for command, out in judged(hooks[which], allowed).items():
            assert rule(out) is None, (which, command)
    assert rule(run_hook(hooks["new"], "rg --files .", FAKE_HOME + "/.subfleet")) is None
    assert rule(run_hook(hooks["new"], "rg foo ~/.subfleet/subfleetd.log")) == "unscoped-search"
    for command in ("grep foo ~/.subfleet/subfleetd.log", "rg --max-depth 1 foo ~/.subfleet/subfleetd.log"):
        assert rule(run_hook(hooks["new"], command)) is None, command


# ---------------------------------------------------------------- S4

@functools.lru_cache(maxsize=1)
def claude_analyzer():
    path = os.environ.get("SUBFLEET_CLAUDE_UNSCOPED_SEARCH")
    if not path:
        pytest.skip("set SUBFLEET_CLAUDE_UNSCOPED_SEARCH to the Claude hook's unscoped_search.py "
                    "(operator home, not in this repository)")
    spec = importlib.util.spec_from_file_location("claude_unscoped_search", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def claude_blocks(command: str, cwd: str = SCOPED_CWD) -> bool:
    return claude_analyzer().analyze(command, cwd, FAKE_HOME) is not None


def test_s4_the_claude_half_agrees_on_plain_new_root_searches(hooks):
    claude_analyzer()
    templates = ("find {} -name AGENTS.md", "rg --files {}", "grep -rn x {}", "cd {} && rg foo",
                 "git ls-files | xargs rg foo {}", "rg --max-depth 2 foo {}")
    commands = []
    for spelling in HOME_SPELLINGS:
        for suffix in (".subfleet", ".subfleet/x_y-z.1", ".subfleet/jobs/20261024-115712-x", ".subfleetx"):
            for slash in ("", "/"):
                # A quoted ~ is a literal folder named "~": the Claude analyzer passes it and
                # the Codex hook, judging text, denies it, for every root alike.
                for quote in ("{}",) if spelling == "~" else ("{}", '"{}"'):
                    root = quote.format(f"{spelling}/{suffix}{slash}")
                    commands += [template.format(root) for template in templates]
    for command, out in judged(hooks["new"], commands).items():
        assert claude_blocks(command) == (rule(out) is not None), command


def test_s4_the_claude_half_differs_on_new_roots_only_where_it_differs_on_home_children(corpus, verdicts):
    new = verdicts["new"]
    by_command = {case["command"]: case for case in corpus["generated"]}
    counts = {"pairs": 0, "analog_agree": 0, "new_agree": 0, "both_differ": 0}
    regressions = []
    for case in corpus["generated"]:
        if "analog_of" not in case:
            continue
        target = by_command[case["analog_of"]]
        counts["pairs"] += 1
        analog_agree = claude_blocks(case["command"]) == (rule(new[(case["command"], case["cwd"])]) is not None)
        target_agree = claude_blocks(target["command"]) == (rule(new[(target["command"], target["cwd"])]) is not None)
        counts["analog_agree"] += analog_agree
        counts["new_agree"] += target_agree
        counts["both_differ"] += not analog_agree and not target_agree
        if analog_agree and not target_agree:
            regressions.append(target["command"])
    deep = [c for c in corpus["generated"] if c["kind"] == "deep" and not c["bounded"]]
    deep_blocked = [c["command"] for c in deep if claude_blocks(c["command"])]
    counts["deep_cases"] = len(deep)
    counts["deep_blocked_by_claude"] = len(deep_blocked)
    report("S4", counts)
    assert not regressions, regressions[:10]
    assert not deep_blocked, deep_blocked[:10]
    assert counts["pairs"] > 0
