"""D-LANE-SCOPE1: the lane guard refuses whole-tree searches of ~/.subfleet.

At 15:32Z on 2026-10-24 a Codex lane ran ``rg --files -g AGENTS.md
/Users/maxghenis/.subfleet`` for 4 minutes with 12 threads at load 190-220,
walking 14,121 job folders and 1,765 job worktrees: the never-rules hook's
unscoped-search rule did not list ~/.subfleet. The hub's decision adds
~/.subfleet and each one path component directly under it to the broad roots,
spelled ``~``, ``$HOME``, ``${HOME}`` or ``/Users/<user>``, with or without a
trailing slash, as for the existing home children. A path two or more levels
under it stays scoped, and any -maxdepth / --max-depth still disarms the rule.
Its r2 (2026-10-25, the hub's review r1, P2-3) adds the names lanes have for
that folder: ``$SUBFLEET_ROOT`` (the daemon sets it on every attempt) and
``$SUBFLEET_HOME`` (the launch allowlist passes it through), each also as
``${…}``, are spellings of ``$HOME/.subfleet`` under the same depth rule.

These tests run the reviewed overlay's hook (``private/guard/never-rules-hook.sh``)
and the hook it replaces. The old hook is rebuilt from the new one by undoing
the decision's edits (``OLD_TEXT``, ``NEW_SED`` and the ``broad_re`` line) and
must hash to the original reviewed pin, so the differential always compares
against exactly the hook that was installed, and any edit outside the roots,
their ``${…}`` normalization, the header and the comments that describe them
fails ``test_only_the_roots_the_header_and_the_message_changed``.

Invariants, each checked below for every command of the corpus:

S1 (monotone) The new hook denies every command the old hook denies, and the
   two decisions differ only where the command's search root is one of the new
   roots: in a searcher's path position, as a cd/pushd target, in a for-loop
   list feeding find, or anywhere in a command where xargs runs a searcher
   (that rule's documented over-approximation). The corpus is every command
   string in this repository's guard tests that reaches this hook (this file;
   the never-rules test set, ``git grep -l -e never-rules -e never_rules -e
   guard_hook -- tests``, is 13 paths at e82e45bff, 9 of them test modules:
   this file, ``tests/unit/test_guard_overlay.py`` and 7 more, and 17 paths and
   12 modules after the release/217 merge at 8f1c65f69; only this file and the
   overlay test read ``private/guard/``, and the rest drive the synthetic
   ``tests/fake/guard.py`` hook, whose one command is ``--fixture-deny``),
   every backticked command in the hook's own unscoped-search comments, the
   Claude hook's example table, the reviews' inputs, commands that hit each
   other rule, and a seeded generator of find/rg/grep commands over a root
   pool: each root in every spelling (the env spellings as twins, see S5),
   with and without a depth bound, unquoted, double- and single-quoted, plain,
   in bash -c / sh -c / eval / a subshell, after cd/pushd, in a for loop, and
   through xargs (as an argument and as fed input).
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
   list) and the new roots inherit exactly those. The env-spelled twins (S5)
   meet the same rule against the same command over ``$HOME/.subfleet``
   (``${HOME}`` for a braced twin), which the shell expands as it does the
   twin in every quoting, and each half gives a twin that command's verdict.
S5 (env spellings are spellings) For every generated command over
   ``<home>/.subfleet`` or anything under it, swapping that prefix for an env
   spelling (``$SUBFLEET_ROOT``, ``${SUBFLEET_ROOT}``, ``$SUBFLEET_HOME``,
   ``${SUBFLEET_HOME}``) leaves the new hook's decision unchanged: the twin gets
   the decision of the same command over ``$HOME/.subfleet`` (``${HOME}`` for a
   braced twin) and of its source in the source's home spelling. For the
   near-miss names ``.subfleetx`` and ``.subfleet-old`` the braced spellings
   (the ones the shell reads as that path) do the same. Scale 1 swaps in every
   env spelling; scale N >= 2 one per command, cycling over the home spellings,
   so each (root, context) cell still meets all four.

r3 (2026-10-25, the hub's decisions 1-5) judges path words on disk before the text
rules: a root by name that names an existing non-directory is not broad (decision 1,
reversing r2's decision 6); a root followed by a trailing run of bare ``*`` / ``.*``
components, or by ``**/``, is that root (decision 2); a glob word whose literal folder can
hold a root by name (/, /Users, /private, a home, its Library or .subfleet) is broad iff
one of its first 20,000 matches is a broad root, and allowed when nothing matches
(decision 3); every direct child of ~/.subfleet stays a root (decision 4); a denial for
decision 2 or 3 names the word as written plus ``(glob)`` (decision 5). Every hook run here
judges paths in a fixture filesystem through the hook's test seam (FS_SEAM, FS_LAYOUT), so
no test stats, globs or reads a real home. S1 counts a command holding such a word
(``r3_labels``) as an intended difference, and S1-r3 holds each decision to its direction
against the r2 hook (r3's base, rebuilt from this one). The property test (Hypothesis,
PROPERTY_SEED, PROPERTY_EXAMPLES) states its oracle in its docstring: a plain word is broad
iff its resolved path is not an existing non-directory, always with a trailing /*; a glob
word is broad iff some real bash match is a broad root, and not when nothing matches. The
shell differential expands every glob case in real /bin/zsh -f and /bin/bash.

r3b (2026-10-26, the hub's decisions 1-6, for parity with snapshot-5 of the Claude half, read from
its frozen .ref copy) judges every path word: the roots are snapshot-5's (BROAD_RE, HOME_SUBDIRS and
HOME_PARENTS, matched case-insensitively after ``normalize`` folds //, . and .., the data volume and
/private/{tmp,var,etc}); a written path is judged where its links lead, except under a mount that may
stall (/Volumes, /Network, /net, /home), which is never resolved or tested; a glob is judged component
by component (``glob_broad``), and a folder is listed only when a name in it can be a root
(``holds_root``, computed). R3B_DENIED and R3B_ALLOWED are the brief's cases, R3B_FLIPPED the r3
verdicts r3b changes, and the r3b tests port snapshot-5's root, alias, case, glob, mount, plain-name
and link vectors (each names its source test; R3B_UNPORTED lists the rest). REFERENCE_SH is an
independent bash reference of decisions 1-4, written from the brief: the property test
(R3B_PROPERTY_SEED, R3B_PROPERTY_CASES) holds the hook's functions to it, and S1's labels come from it.
The mount test wraps the hook's five disk functions and records every listing and stat.

Also: the new hook keeps the reviews' allowed commands allowed and names the
matched root; the old hook passed every review input (so these roots are the
change); TRUST pins the new bytes while its identity hash and override stay put;
a denial is logged like any other.

GUARD_DIFF_SCALE (default 1) widens the generator: 1 is a representative root
pool crossed with every spelling and context; N >= 2 is the full pool with N - 1
seeded draws per cell. GUARD_DIFF_REPORT names a file for the S1/S4/S5 counts.
GUARD_DIFF_CACHE names a JSON-lines file of hook verdicts keyed by the hook's
SHA-256 (with the fixture filesystem's layout), cwd and command, read before and appended during a run, so a long
scale-N run can resume across suite holds. Only cwds that do not exist are
cached: there the verdict depends on nothing but the hook's bytes and the
command (HOME, the log and the environment are fixed here), while an existing
cwd can carry the git state other rules read.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
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
import threading

import pytest

from subfleet.guard import preflight as guard


REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "private/guard/never-rules-hook.sh"
TRUST = REPO / "private/guard/TRUST"
#: The original reviewed pair (2026-09-05): the hook these tests compare against, and the rollback.
OLD_HOOK_SHA256 = "a60d1c514d3a3bcec68c246a33c849c11fd37e1bdf60d886650cd9d6651390db"
#: The installed TRUST this revision is built on (2026-09-29: the codex-cli 0.159.0 pin and the
#: 0.158.0/0.159.0 provenance). The repository's copy at abe117e67 (0b9d688e…) still pinned 0.153.3;
#: installing a TRUST built on it would have refused every Codex lane at preflight (VERSION).
OLD_TRUST_SHA256 = "b3193b97d712bda6a5cc0f2447907e674f7ed90911a82d7709de8298b42aaa06"
#: hooks_trust_hash of TRUST's reference path, before and after: the identity, not the bytes.
HOOKS_TRUST_HASH = "sha256:f97fa95f2f4d43e1f8d35cd92da4d7f3f1861497abd86916680e0189e0ec34b1"
#: The overlay's hook after r2 (the env spellings), which r3 is built on.
R2_HOOK_SHA256 = "dbe21e1d9a2ed3f57a7675fd09d3f8cacb2f73a6bfec7fd22e800da3691a041c"
#: The overlay's hook after r3 (path words judged on disk): TRUST's hook_sha256.
NEW_HOOK_SHA256 = "2c3fbc1a6d563ee28e47d172343508d0613a9e7bb75567ec82a60e702980d7cf"

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
#     190-220. D-LANE-SCOPE1 r2 (2026-10-25) added $SUBFLEET_ROOT (set on
#     every attempt) and $SUBFLEET_HOME (passed through to lanes), both
#     ~/.subfleet by default, as spellings of it. Change a root on both sides
#     in one reviewed step: here, and in unscoped_search.py (BROAD_RE,
#     HOME_SUBDIRS, expand()).
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
#              scoped), `$SUBFLEET_ROOT` and `$SUBFLEET_HOME` as spellings of
#              ~/.subfleet (alone or with one component under them),
#              `/tmp`, `/private/tmp` — each with or without a trailing slash.
""", """#              `/tmp`, `/private/tmp` — each with or without a trailing slash.
"""),
    # r2: the ${…} normalization's two comments
    ("""#              does) and `${HOME}`, `${SUBFLEET_ROOT}` and `${SUBFLEET_HOME}`
#              lose their braces; then collapse_quotes
""", """#              does) and `${HOME}` is spelled `$HOME`; then collapse_quotes
"""),
    ("""  # the original port-back contract specifies, then `${HOME}` spelled `$HOME`
  # (and `${SUBFLEET_ROOT}`/`${SUBFLEET_HOME}` without braces too).
""", """  # the original port-back contract specifies, then `${HOME}` spelled `$HOME`.
"""),
    ("(/, /Users, ~, ~/TheAxiomFoundation, ~/PolicyEngine, ~/.subfleet, ~/.subfleet/<child>, /tmp, caches)",
     "(/, /Users, ~, ~/TheAxiomFoundation, ~/PolicyEngine, /tmp, caches)"),
]
#: The behavioural edits: the broad_re line, new and original ...
NEW_BROAD_RE = ("  broad_re=\"(/Users/[^/[:space:]'\\\"]+|~|\\\\\\$HOME)(/(TheAxiomFoundation|RulesFoundation|"
                "PolicyEngine|Library/Caches|\\.cache|\\.local|\\.subfleet(/[^/[:space:]'\\\"]+)?))?|"
                "\\\\\\$SUBFLEET_(ROOT|HOME)(/[^/[:space:]'\\\"]+)?|(/private)?/tmp\"")
OLD_BROAD_RE = ("  broad_re=\"(/Users/[^/[:space:]'\\\"]+|~|\\\\\\$HOME)(/(TheAxiomFoundation|RulesFoundation|"
                "PolicyEngine|Library/Caches|\\.cache|\\.local))?|(/private)?/tmp\"")
#: ... and r2's ${…} normalization, on both `judged` lines (new, original).
NEW_SED = (r"sed 's/\$[{]HOME[}]/$HOME/g; s/\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; "
           r"s/\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g'")
OLD_SED = r"sed 's/\$[{]HOME[}]/$HOME/g'"
OLD_TRUST_TEXT = [
    (NEW_HOOK_SHA256, OLD_HOOK_SHA256),
]
OLD_PROVENANCE_END = "Previous pin kept at TRUST.pre-0.159.0.bak."


def undo(text: str, edits) -> str:
    for new, old in edits:
        assert text.count(new) == 1, f"expected exactly one {new[:70]!r}"
        text = text.replace(new, old)
    return text


#: r3 (2026-10-25, decisions 1-3): the three path-word blocks, each from its begin marker line
#: through its end marker line, and the comments that name them, as (r3 text, r2 text).
R3_BLOCKS = [("# >>> r3 path words: begin >>>\n", "# <<< r3 path words: end <<<\n"),
             ("  # >>> r3 path words: judge >>>\n", "  # <<< r3 path words: judge <<<\n"),
             ("    # >>> r3 path words: name >>>\n", "    # <<< r3 path words: name <<<\n")]
R3_TEXT = [
    ("""#     ~/.subfleet by default, as spellings of it. D-LANE-SCOPE1 r3
#     (2026-10-25) judged path words on disk (the "r3 path words" blocks): a
#     file is never a broad root, a trailing `/*` after a root is that root,
#     and a glob word that can match a root is judged by its matches. Change
#     a root on both sides in one reviewed step: here, and in
#     unscoped_search.py (BROAD_RE, HOME_SUBDIRS, expand()).
""", """#     ~/.subfleet by default, as spellings of it. Change a root on both sides
#     in one reviewed step: here, and in unscoped_search.py (BROAD_RE,
#     HOME_SUBDIRS, expand()).
"""),
    ("""#              r3: a root by name that is an existing non-directory is not
#              broad, a root before a trailing `/*` run or a `**/` is that
#              root, and a glob word is judged by its matches ("Path words").
""", ""),
    ("""#              comment, nothing in the command text lets a crawl through
#              (r3's two test seams are names no lane's environment carries).
""", """#              comment, nothing in the command text lets a crawl through.
"""),
]


def rebuild_r2(text: str) -> str:
    """The r2 hook from the overlay's: each r3 block removed whole, whatever it now says (so
    a mutant inside one still has the true r2 beside it), and r3's comments undone."""
    for begin, end in R3_BLOCKS:
        if begin in text:
            start = text.index(begin)
            text = text[:start] + text[text.index(end, start) + len(end):]
    for new, old in R3_TEXT:
        if text.count(new) == 1:
            text = text.replace(new, old)
    return text


def rebuild_old(text: str) -> bytes:
    """The original reviewed hook from the overlay's: r3 undone (``rebuild_r2``), then the
    header, comment and message edits and the ${…} normalization undone where present and
    the broad_re line restored, whatever it now says (so a mutant of the roots still has the
    true original beside it)."""
    text = rebuild_r2(text)
    for new, old in OLD_TEXT:
        if text.count(new) == 1:
            text = text.replace(new, old)
    text = text.replace(NEW_SED, OLD_SED)
    lines = text.split("\n")
    at = [i for i, line in enumerate(lines) if line.startswith('  broad_re="')]
    assert len(at) == 1
    lines[at[0]] = OLD_BROAD_RE
    return "\n".join(lines).encode("utf-8")


@pytest.fixture(scope="module")
def hooks(tmp_path_factory) -> dict[str, Path]:
    """{"new": the overlay's hook, "old": the original reviewed hook rebuilt from it,
    "r2": the r2 hook (r3's base) rebuilt from it}."""
    text = HOOK.read_text(encoding="utf-8")
    old_bytes = rebuild_old(text)
    assert hashlib.sha256(old_bytes).hexdigest() == OLD_HOOK_SHA256
    r2_bytes = rebuild_r2(text).encode("utf-8")
    assert hashlib.sha256(r2_bytes).hexdigest() == R2_HOOK_SHA256
    built = {}
    for name, data in (("old", old_bytes), ("r2", r2_bytes)):
        path = tmp_path_factory.mktemp(f"{name}-guard") / "never-rules-hook.sh"
        path.write_bytes(data)
        path.chmod(0o755)
        built[name] = path
    return {"new": HOOK, **built}


# ---------------------------------------------------------------- r3: the fixture filesystem

#: The test seams (r3). The hook makes every check on disk under CODEX_GUARD_TEST_FSROOT, a
#: folder standing in for /, so no test stats, globs or reads a real path (the corpus names
#: /Users/maxghenis/.subfleet); CODEX_GUARD_TEST_GLOB_CAP is the glob match cap.
FS_SEAM = "CODEX_GUARD_TEST_FSROOT"
CAP_SEAM = "CODEX_GUARD_TEST_GLOB_CAP"
JOB = "20261024-115712-x"
#: The fixture filesystem under the seam: (path under /, kind, link target). The home is
#: FAKE_HOME; /Users/nojobs is a home whose ~/.subfleet has no jobs/; /Users/prop holds the
#: property test's six kinds of child and the folders its globs list. Links are relative,
#: so they resolve inside the seam.
FS_LAYOUT = [
    *((f"Users/tester/{d}", "dir", None) for d in (
        "work/repo", "PolicyEngine/policyengine-us", "TheAxiomFoundation", "RulesFoundation",
        "Library/Caches", ".cache", ".local", f".subfleet/jobs/{JOB}/a1",
        ".subfleet/worktrees/20261024-115939-guardscope-build", ".subfleet/archive", ".subfleet/lanes",
        ".subfleet/conversations", ".subfleet/guard-cache", ".subfleet/leases", ".subfleet/guard",
        ".subfleet/state", ".subfleet/inbox", ".subfleet/bin", ".subfleet/docs")),
    *((f"Users/tester/{f}", "file", None) for f in (
        ".subfleet/policy.json", ".subfleet/daemon.log", ".subfleet/lanes.json", ".subfleet/subfleetd.log",
        f".subfleet/jobs/{JOB}/a1/out.md", "PolicyEngine/policyengine-us/x.py", ".subfleet/$OTHER")),
    ("Users/tester/.subfleet/policy-link.json", "link", "policy.json"),
    ("Users/tester/.subfleet/jobs-link", "link", "jobs"),
    ("Users/tester/.subfleet/dangling", "link", "no-such-target"),
    ("Users/nojobs/.subfleet/worktrees", "dir", None),
    ("Users/nojobs/.subfleet/policy.json", "file", None),
    *((f"Users/prop/{d}", "dir", None) for d in (
        ".subfleet/d", "PolicyEngine/policyengine-us", "Library/Caches", ".cache", "Pictures")),
    *((f"Users/prop/{f}", "file", None) for f in (
        ".subfleet/f.log", "notes.txt", "PolicyEngine/policyengine-us/x.py")),
    ("Users/prop/.subfleet/lf.log", "link", "f.log"),
    ("Users/prop/.subfleet/ld", "link", "d"),
    ("Users/prop/.subfleet/dl.log", "link", "no-such-target"),
    ("tmp", "dir", None), ("private/tmp", "dir", None),
]
#: Part of every cached verdict's key: a verdict depends on the hook's bytes and this layout.
FS_DIGEST = hashlib.sha256(json.dumps(FS_LAYOUT).encode()).hexdigest()
_FS: dict[str, Path] = {}


def build_fs(root: Path) -> Path:
    for rel, kind, target in FS_LAYOUT:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "dir":
            path.mkdir(parents=True, exist_ok=True)
        elif kind == "file":
            path.write_text("x\n")
        else:
            path.symlink_to(target)
    return root


@pytest.fixture(scope="module", autouse=True)
def fixture_fs(tmp_path_factory) -> Path:
    """The seam folder every hook run here judges paths in (under TMPDIR, never a real home)."""
    _FS["root"] = build_fs(tmp_path_factory.mktemp("fsroot"))
    yield _FS["root"]
    _FS.pop("root", None)


def fs_path(path: str) -> Path:
    """Where the hook looks for the absolute ``path`` on disk."""
    return _FS["root"] / path.lstrip("/")


def fs_str(path: str) -> str:
    """The same as a string, a trailing slash kept (``policy.json/`` must fail os.path.exists)."""
    return str(_FS["root"]) + path


@pytest.fixture(scope="module")
def log_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("guard-log")


def run_hook(hook: Path, command: str, cwd: str = SCOPED_CWD, *, tool: str = "Bash",
             log: Path | None = None, home: str = FAKE_HOME, extra: dict | None = None) -> str:
    """The hook's stdout for one PreToolUse payload: empty to allow, the deny JSON otherwise.
    Paths are judged in the fixture filesystem (the r3 seam); the environment's SUBFLEET_ROOT
    and SUBFLEET_HOME are dropped (the hook must never read them; one test sets them)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CARPOOL_")
           and k not in ("SUBFLEET_ROOT", "SUBFLEET_HOME", FS_SEAM, CAP_SEAM)}
    env.update(HOME=home, CODEX_GUARD_LOG=str(log) if log else os.devnull)
    env[FS_SEAM] = str(_FS["root"])
    env.update(extra or {})
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
#: GUARD_DIFF_CACHE's verdicts: (hook SHA-256, cwd, command) -> stdout.
_CACHED: dict[tuple[str, str, str], str] = {}
_CACHE_LOADED: set[str] = set()
_CACHE_LOCK = threading.Lock()


def _cache_file() -> Path | None:
    target = os.environ.get("GUARD_DIFF_CACHE")
    return Path(target) if target else None


def _cacheable(cwd: str) -> bool:
    """A cwd that does not exist carries no git state, so the verdict is the bytes' and the command's."""
    return not os.path.exists(cwd)


def _load_cache() -> None:
    path = _cache_file()
    if path is None or str(path) in _CACHE_LOADED:
        return
    _CACHE_LOADED.add(str(path))
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            key, out = (row["hook"], row["cwd"], row["command"]), row["stdout"]
        except (ValueError, KeyError, TypeError):
            continue            # a line an interrupted run cut short
        if _cacheable(key[1]):
            _CACHED[key] = out


def _remember(digest: str, case: tuple[str, str], out: str) -> None:
    path = _cache_file()
    if path is None or not _cacheable(case[1]):
        return
    line = json.dumps({"hook": digest, "cwd": case[1], "command": case[0], "stdout": out}) + "\n"
    with _CACHE_LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def judge_all(hook: Path, cases) -> dict[tuple[str, str], str]:
    """Run ``hook`` over (command, cwd) pairs, four at a time (memoized, and read from and
    written to GUARD_DIFF_CACHE when it is set); stdout per pair. Each verdict is kept as it
    lands, and an interrupt cancels what has not started, so a stopped run loses little."""
    wanted = list(dict.fromkeys(cases))
    digest = hashlib.sha256(Path(hook).read_bytes() + FS_DIGEST.encode()).hexdigest()
    _load_cache()
    missing = []
    for case in wanted:
        key = (str(hook), *case)
        if key in _JUDGED:
            continue
        if (digest, case[1], case[0]) in _CACHED:
            _JUDGED[key] = _CACHED[(digest, case[1], case[0])]
        else:
            missing.append(case)
    if missing:
        pool = ThreadPoolExecutor(max_workers=WORKERS)
        try:
            futures = {pool.submit(run_hook, hook, command, cwd): (command, cwd) for command, cwd in missing}
            for future in as_completed(futures):
                case, out = futures[future], future.result()
                _JUDGED[(str(hook), *case)] = out
                _remember(digest, case, out)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
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


# ---------------------------------------------------------------- r2: the env spellings

#: ~/.subfleet as lanes name it (the hub's review r1, P2-3): the daemon sets SUBFLEET_ROOT to its
#: root on every attempt (subfleet/daemon.py, the attempt environment) and the launch allowlist
#: passes SUBFLEET_HOME through (subfleet/launch_env.py); both are ~/.subfleet by default.
ENV_SPELLINGS = ["$SUBFLEET_ROOT", "${SUBFLEET_ROOT}", "$SUBFLEET_HOME", "${SUBFLEET_HOME}"]
#: The home spelling each env spelling sits beside (its ~/.cache analog keeps the braces).
ENV_HOME = {"$SUBFLEET_ROOT": "$HOME", "${SUBFLEET_ROOT}": "${HOME}",
            "$SUBFLEET_HOME": "$HOME", "${SUBFLEET_HOME}": "${HOME}"}
ENV_DENIED = [
    # (command, the root the block message must name: ${…} is judged without its braces)
    ('rg --files -g AGENTS.md "$SUBFLEET_ROOT"', "$SUBFLEET_ROOT"),
    ("rg --files -g AGENTS.md $SUBFLEET_ROOT/worktrees", "$SUBFLEET_ROOT/worktrees"),
    ('find "$SUBFLEET_HOME" -name AGENTS.md', "$SUBFLEET_HOME"),
    ("grep -r x ${SUBFLEET_ROOT}/jobs", "$SUBFLEET_ROOT/jobs"),
]
ENV_ALLOWED = [
    "rg foo $SUBFLEET_ROOT/jobs/20261024-115712-x/a1",
    'find "$SUBFLEET_HOME" -maxdepth 1 -name lanes.json',
    "rg 'SUBFLEET_ROOT' src/",
]


def test_env_spellings_of_subfleet_are_denied(hooks):
    """The review's four: each env spelling names ~/.subfleet, so a whole-tree search of it,
    or of one component under it, is the incident's crawl. The old hook passed all four."""
    new = judged(hooks["new"], [command for command, _ in ENV_DENIED])
    old = judged(hooks["old"], [command for command, _ in ENV_DENIED])
    for command, root in ENV_DENIED:
        assert rule(new[command]) == "unscoped-search", command
        assert matched_root(new[command]) == root
        assert rule(old[command]) is None, command


def test_env_spelling_scoped_inputs_stay_allowed(hooks):
    """Two levels under, depth-bounded, or the name in pattern position: not a crawl."""
    for which in ("new", "old"):
        for command, out in judged(hooks[which], ENV_ALLOWED).items():
            assert rule(out) is None, (which, command)


ENV_NEAR_MISSES = [
    "rg foo $SUBFLEET_ROOTS", "rg foo $SUBFLEET_ROOT_X/jobs", "rg foo ${SUBFLEET_ROOT_X}",
    "find $SUBFLEET_JOB -name x", "rg foo $SUBFLEET_HOMEDIR", "rg foo x$SUBFLEET_ROOT",
    "rg foo ${SUBFLEET_ROOT}x", "rg foo ${SUBFLEET_HOME}-old/jobs", "rg $SUBFLEET_ROOT src/",
    "rg -n 'SUBFLEET_HOME' src", "rg --max-depth 2 foo $SUBFLEET_ROOT",
    'echo "rg --files $SUBFLEET_ROOT" >> notes.md', "git commit -m 'guard: rg x ${SUBFLEET_HOME}'",
]


def test_every_env_spelling_is_a_root_one_level_down_and_no_deeper():
    """Each env spelling alone or with one component (a file too), with or without a trailing
    slash, bare or quoted wholly or partly, is a root; two components down stays scoped; other
    variables, other names and the name as a pattern or in prose stay allowed."""
    searches = ("rg -l foo {}", "find {} -type f", "grep -r x {}", "rg --files {}")
    denied, allowed = [], list(ENV_NEAR_MISSES)
    for i, env in enumerate(ENV_SPELLINGS):
        forms = [(env, True), (env + "/", True), ('"' + env + '"', True), (env + "/jobs", True),
                 (env + "/subfleetd.log/", True), ('"' + env + '/worktrees"', True),
                 ('"' + env + '"/archive/', True), (env + "/jobs/20261024-115712-x", False),
                 ('"' + env + '"/jobs/20261024-115712-x/a1/', False),
                 ('"' + env + '/worktrees/20261025-033945-x"', False)]
        for j, (form, root) in enumerate(forms):
            (denied if root else allowed).append(searches[(i + j) % len(searches)].format(form))
    outs = judged(HOOK, denied + allowed)
    for command in denied:
        assert rule(outs[command]) == "unscoped-search", command
    for command in allowed:
        assert rule(outs[command]) is None, command


HOME_SPELLINGS = ["~", "$HOME", "${HOME}", "/Users/maxghenis", FAKE_HOME]
CHILDREN = ["jobs", "worktrees", "archive", "lanes", "conversations", "guard-cache", "x_y-z.1"]


def test_every_spelling_of_subfleet_is_a_root():
    commands = [search.format(quote.format(spelling + "/.subfleet"))
                for spelling in HOME_SPELLINGS for quote in ("{}", '"{}"')
                for search in ("find {} -name AGENTS.md", "rg --files {}", "rg -l foo {}", "grep -rn x {}")]
    for command, out in judged(HOOK, commands).items():
        assert rule(out) == "unscoped-search", command


def test_each_direct_child_is_a_root():
    searches = ("rg -l foo {}", "find {} -type f", "grep -r x {}")
    expected = {}
    for i, child in enumerate(CHILDREN):
        for j, spelling in enumerate(HOME_SPELLINGS):
            command = searches[(i + j) % len(searches)].format(f"{spelling}/.subfleet/{child}")
            expected[command] = f"{spelling.replace('${HOME}', '$HOME')}/.subfleet/{child}"
    for command, out in judged(HOOK, list(expected)).items():
        assert rule(out) == "unscoped-search", command
        assert matched_root(out) == expected[command]


TRAILING = ["~/.subfleet/", "$HOME/.subfleet/", "${HOME}/.subfleet/", "/Users/maxghenis/.subfleet/",
            "~/.subfleet/jobs/", "$HOME/.subfleet/worktrees/", "/Users/maxghenis/.subfleet/archive/"]


def test_trailing_slash_spellings_are_denied():
    commands = [search.format(path) for path in TRAILING
                for search in ("rg --files {}", "find {} -name x", "grep -r x {}", 'rg foo "{}"')]
    for command, out in judged(HOOK, commands).items():
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


def test_near_misses_patterns_prose_and_bounded_searches_stay_allowed():
    for command, out in judged(HOOK, NEAR_MISSES).items():
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
    assert "Installed only after the hub's review and a live hooks/list preflight. 2026-10-25, " \
           "D-LANE-SCOPE1 r2: $SUBFLEET_ROOT and $SUBFLEET_HOME" in trust["provenance"]
    assert "live hooks/list preflight. 2026-10-25, D-LANE-SCOPE1 r3: path words are judged on " \
           "disk" in trust["provenance"]
    assert trust["provenance"].endswith(
        "installed only after the hub's review and a live hooks/list preflight.")


def test_trust_keeps_the_installed_lane_pin():
    """The hub's review of this revision: the build first derived TRUST from the
    repository's copy at abe117e67, which still pinned codex-cli 0.153.3 while the
    installed TRUST had pinned 0.159.0 since 2026-09-29. Installed, that TRUST would
    have refused every Codex lane at preflight (VERSION). This TRUST keeps the
    installed pin and its 0.158.0 and 0.159.0 provenance."""
    trust = json.loads(TRUST.read_text())
    assert trust["codex_version"] == "codex-cli 0.159.0"
    assert "lane pin moved 0.158.0 -> 0.159.0" in trust["provenance"]


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
    """Decision 2's fence, with r2's normalization and r3's blocks: undoing exactly R3_BLOCKS
    and R3_TEXT gives the r2 bytes, and then undoing OLD_TEXT, the two NEW_SED edits and the
    broad_re line gives the original reviewed bytes."""
    text = HOOK.read_text(encoding="utf-8")
    for begin, end in R3_BLOCKS:
        assert text.count(begin) == 1 and text.count(end) == 1 and text.index(begin) < text.index(end)
    for begin, end in R3_BLOCKS:
        start = text.index(begin)
        text = text[:start] + text[text.index(end, start) + len(end):]
    text = undo(text, R3_TEXT)
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == R2_HOOK_SHA256
    assert text.count(NEW_BROAD_RE + "\n") == 1
    assert text.count(NEW_SED) == 2
    old = undo(text, OLD_TEXT).replace(NEW_SED, OLD_SED)
    old = old.replace(NEW_BROAD_RE + "\n", OLD_BROAD_RE + "\n").encode("utf-8")
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
#: Appended to every OTHER_RULES command for the combination cases (r2: also an env spelling).
NEW_ROOT_SEARCH = "rg --files ~/.subfleet"
ENV_ROOT_SEARCH = "rg --files $SUBFLEET_ROOT"
COMBINED = (NEW_ROOT_SEARCH, ENV_ROOT_SEARCH)


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
    elif spelling in ENV_HOME and (suffix == ".subfleet" or suffix.startswith((".subfleet/", ".subfleetx",
                                                                             ".subfleet-"))):
        text = spelling + suffix[len(".subfleet"):]      # an env spelling is <home>/.subfleet
    elif spelling in ENV_HOME:
        text = ENV_HOME[spelling] + ("/" + suffix if suffix else "")
    else:
        text = spelling + ("/" + suffix if suffix else "")
    if slash and text not in ("/", "/*"):
        text += "/"
    if quote == "dq":
        return '"' + text + '"'
    if quote == "sq":
        return "'" + text + "'"
    return text


def twin_spellings(suffix: str, s_index: int, scale: int) -> list[str]:
    """S5: the env spellings a command over <home>/<suffix> is also judged in. Every one under
    ~/.subfleet; for .subfleetx / .subfleet-old only the braced ones, which the shell reads as
    that path (``$SUBFLEET_ROOTx`` is another variable). Scale 1: all of them; scale N >= 2:
    one, cycling with the home spelling, so each (root, context) cell meets all four."""
    if suffix == ".subfleet" or suffix.startswith(".subfleet/"):
        envs = ENV_SPELLINGS
    elif suffix in (".subfleetx", ".subfleet-old"):
        envs = [env for env in ENV_SPELLINGS if env.startswith("${")]
    else:
        return []
    return envs if scale <= 1 else [envs[s_index % len(envs)]]


def generate(scale: int) -> list[dict]:
    """The seeded generator. Scale 1: SMALL_POOL, every spelling plain and SMALL_CONTEXTS
    spread over the spellings; scale N >= 2: the full pool, every (root, spelling,
    context) cell, N - 1 seeded draws each (searcher, quoting, slash, depth bound).
    Each command over <home>/.subfleet[/…] gets env-spelled twins (``twin_of``, S5) that
    share its draw; S1-S4 judge the twins like any other case. A twin's ``home_ref`` is the
    same draw over ``$HOME/.subfleet`` (``${HOME}`` for a braced twin), which the shell
    expands exactly as the twin in every quoting; S4 compares twins with it."""
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
                    if homed:
                        for env in twin_spellings(suffix, s_index, scale):
                            cases.append(dict(case, command=case["command_as"](env), spelling=env,
                                              twin_of=case["command"],
                                              home_ref=case["command_as"](ENV_HOME[env])))
    for case in cases:
        case.pop("command_for", None)
        case.pop("command_as", None)
    return cases


def draw(rng, suffix, kind, spelling, context) -> dict:
    slash = rng.random() < 0.4
    quote = rng.choice(["bare", "bare", "dq", "sq"])
    bounded = rng.random() < 0.25
    if context in ROOT_CONTEXTS:
        template = ROOT_CONTEXTS[context][bounded]
        position = "path"
        make = lambda sfx, sp: template.replace("{R}", spell(sfx, sp, slash, quote))  # noqa: E731
    else:
        name = rng.choice(sorted(SEARCHES))
        template, position = SEARCHES[name]
        if bounded:
            template = bound(template)
        wrap = search_context(context)
        make = lambda sfx, sp: wrap(template.replace("{R}", spell(sfx, sp, slash, quote)))  # noqa: E731
        if context == "xargs-arg":
            position = "xargs"      # a root anywhere counts when xargs runs a searcher
    command = make(suffix, spelling)
    return {"command": command, "cwd": SCOPED_CWD, "kind": kind, "suffix": suffix, "spelling": spelling,
            "context": context, "bounded": bounded, "position": position, "quote": quote, "slash": slash,
            "reads_root": position in ("path", "xargs") and not bounded,
            "command_for": lambda sfx: make(sfx, spelling), "command_as": lambda sp: make(suffix, sp)}


#: Implicit "." searches judged by the cwd rule, whose list D-LANE-SCOPE1 left alone (see the report).
CWD_CASES = [(cmd, cwd) for cwd in (FAKE_HOME, FAKE_HOME + "/PolicyEngine", FAKE_HOME + "/.subfleet",
                                    FAKE_HOME + "/.subfleet/jobs", FAKE_HOME + "/.subfleet/jobs/20261024-115712-x",
                                    SCOPED_CWD)
             for cmd in ("rg --files .", "find . -name AGENTS.md", "grep -r x .")]

NEW_ROOT_TOKEN = re.compile(r"""(^|[\s'"=(])((~|\$HOME|\$\{HOME\}|/Users/[^/\s'"]+)/\.subfleet"""
                            r"""|\$SUBFLEET_(ROOT|HOME)|\$\{SUBFLEET_(ROOT|HOME)\})"""
                            r"""(/[^/\s'"|;&)<>]+)?/?(?=['"]?([\s|;&)]|$))""")


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
    fixed += [(cmd + "; " + search, repos[where]) for where, cmd, _ in OTHER_RULES for search in COMBINED]
    fixed += [(c, SCOPED_CWD) for c, _ in ENV_DENIED] + [(c, SCOPED_CWD) for c in ENV_ALLOWED]
    fixed += [(cmd, cwd) for pairs in R2_LIMITS for cwd, cmd in pairs]
    fixed += [(cmd, cwd) for cwd, cmd in R2_OVER_APPROXIMATIONS + R3_RESOLVED]
    fixed += [(c, SCOPED_CWD) for c in R3_CORPUS]
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
    """Also r3: a command holding a path word decisions 1-3 judge on disk (``r3_labels``) is an
    intended difference, counted apart (``r3_intended``) and held to its direction by
    ``test_s1_r3_moves_each_decision_only_its_own_way``."""
    old, new = verdicts["old"], verdicts["new"]
    prime_labels([case["command"] for case in corpus["generated"]] + [c for c, _ in corpus["fixed"]])
    loosened, stray = [], []
    counts = {"commands": 0, "old_denied": 0, "new_denied": 0, "newly_denied": 0,
              "newly_denied_generated": 0, "newly_denied_fixed": 0, "r3_intended": 0,
              "r3_intended_loosened": 0, "r3_intended_tightened": 0, "r3_labelled": 0}
    seen = set()

    def r3(key, o, n):
        labelled = bool(r3_labels(key[0]))
        if key not in seen:
            seen.add(key)
            counts["r3_labelled"] += labelled
            if labelled and o != n:
                counts["r3_intended"] += 1
                counts["r3_intended_loosened" if o else "r3_intended_tightened"] += 1
        return labelled

    for case in corpus["generated"]:
        key = (case["command"], case["cwd"])
        o, n = rule(old[key]), rule(new[key])
        if r3(key, o, n):
            continue
        if o and n != o:
            loosened.append(key)
        if o != n and not (case["kind"] == "new" and case["reads_root"] and n == "unscoped-search"):
            stray.append((key, case["kind"], case["position"], case["bounded"]))
        counts["newly_denied_generated"] += o is None and n is not None
    for key in corpus["fixed"]:
        o, n = rule(old[key]), rule(new[key])
        if r3(key, o, n):
            continue
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
    counts["env_twins"] = sum("twin_of" in case for case in corpus["generated"])
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
    prime_labels([case["command"] for case in corpus["generated"]])
    by_command = {case["command"]: case for case in corpus["generated"]}
    missing, allowed_new = [], []
    for case in corpus["generated"]:
        if "analog_of" not in case:
            continue
        target = by_command[case["analog_of"]]
        # r3, decision 1: a child that is a file in the fixture (subfleetd.log) is not broad.
        if r3_labels(target["command"]) == {"file"}:
            continue
        if rule(old[(case["command"], case["cwd"])]) and not rule(new[(target["command"], target["cwd"])]):
            missing.append(target["command"])
    for case in corpus["generated"]:
        if (case["kind"] == "new" and case["reads_root"] and "twin_of" not in case
                and not r3_labels(case["command"]) and not rule(new[(case["command"], case["cwd"])])):
            allowed_new.append(case["command"])
    report("S1-protection", {"new_root_reading_cases": sum(c["kind"] == "new" and c["reads_root"]
                                                          and "twin_of" not in c for c in corpus["generated"]),
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
    for index, (where, command, expected) in enumerate(OTHER_RULES):
        for suffix, text in enumerate((command, *(command + "; " + search for search in COMBINED))):
            logs = {which: tmp_path / f"{which}-{index}-{suffix}.log" for which in ("old", "new")}
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
    # Every rule's own command is compared; a combination is compared when the old hook
    # still denies it under that rule (`security dump-keychain -d; rg …` is not, in either
    # spelling: the keychain rule wants a space or the end after -d, so only the new root
    # denies it).
    assert compared >= (1 + len(COMBINED)) * sum(1 for _, _, expected in OTHER_RULES if expected) - len(COMBINED)


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
    - The cwd list is unchanged: ``rg --files .`` sitting in ~/.subfleet passes.

    r3 flipped two pins of this list (the hub's decisions, 2026-10-25):
    - decision 2: a trailing ``/*`` after a root is that root, so ``rg foo ~/.subfleet/jobs/*``
      and ``rg foo ~/*`` are denied now; the old and r2 hooks pass both;
    - decision 1 (reversing r2's decision 6): a child of ~/.subfleet that is a file
      (``rg foo ~/.subfleet/subfleetd.log``) is not broad, so it is allowed now; r2 denied it.
    """
    allowed = ["bash -c 'find / -name x'", "bash -c 'rg --files ~/.subfleet'",
               "eval 'grep -r x ~/.subfleet/jobs'"]
    for which in ("old", "new"):
        for command, out in judged(hooks[which], allowed).items():
            assert rule(out) is None, (which, command)
    assert rule(run_hook(hooks["new"], "rg --files .", FAKE_HOME + "/.subfleet")) is None
    for command in ("rg foo ~/.subfleet/jobs/*", "rg foo ~/*"):
        # r3 decision 2: was allowed by every hook before r3; now that root.
        assert rule(run_hook(hooks["new"], command)) == "unscoped-search", command
        assert rule(run_hook(hooks["r2"], command)) is None, command
        assert rule(run_hook(hooks["old"], command)) is None, command
    # r3 decision 1: a file is never a broad root (r2 denied it; the old hook did not know the root).
    assert fs_path(FAKE_HOME + "/.subfleet/subfleetd.log").is_file()
    assert rule(run_hook(hooks["new"], "rg foo ~/.subfleet/subfleetd.log")) is None
    assert rule(run_hook(hooks["r2"], "rg foo ~/.subfleet/subfleetd.log")) == "unscoped-search"
    for command in ("grep foo ~/.subfleet/subfleetd.log", "rg --max-depth 1 foo ~/.subfleet/subfleetd.log"):
        assert rule(run_hook(hooks["new"], command)) is None, command


LANE_WORKTREE = FAKE_HOME + "/.subfleet/worktrees/20261025-033945-pr257-fix-r2"
PE_REPO = FAKE_HOME + "/PolicyEngine/policyengine-us"
S_ = SCOPED_CWD
#: The known limits r2 lists (the hub's review r1, P2-4 and P3-3), each new-root command beside an
#: existing-root command of the same shape: ((cwd, new root), (cwd, existing root)). The old and the
#: new hook allow all of them, so each is pre-existing for every root; D-LANE-SCOPE2 takes them up.
R2_LIMITS = [
    # P2-4: relative paths from a lane's own worktree (job worktrees are ~/.subfleet/worktrees/<id>)
    ((LANE_WORKTREE, "rg --files -g AGENTS.md .."), (PE_REPO, "rg --files -g AGENTS.md ..")),
    ((LANE_WORKTREE, "find ../.. -name AGENTS.md"), (PE_REPO + "/policyengine_us", "find ../.. -name AGENTS.md")),
    ((LANE_WORKTREE, "rg --files ../../jobs"), (PE_REPO, "rg --files ../../PolicyEngine")),
    # P3-3: a find that ends the command is not seen as a searcher
    ((S_, "printf '%s\\n' ~/.subfleet/ | xargs -n1 find"), (S_, "printf '%s\\n' ~/ | xargs -n1 find")),
    # P3-3: spellings the rule does not normalize (r3b normalizes //, /./, letter case, the data
    # volume and a quoted part, and R3B_FLIPPED holds those five pairs now)
    ((S_, "rg --files $'/Users/tester/.subfleet'"), (S_, "rg --files $'/Users/tester'")),
    ((S_, "rg --files ~tester/.subfleet"), (S_, "rg --files ~tester/PolicyEngine")),
    ((S_, "rg -l foo ~/.subfleet/jobs/*/a1"), (S_, "rg -l foo ~/*/src")),
    ((S_, "env -C ~/.subfleet rg --files"), (S_, "env -C ~ rg --files")),
    ((S_, "rg --files ${SUBFLEET_ROOT:-~/.subfleet}"), (S_, "rg --files ${HOME:-/Users/tester}")),
    # P3-3: shell structure the rule misses
    ((S_, "bash -c 'rg --files $SUBFLEET_ROOT'"), (S_, "bash -c 'rg --files ~'")),
    ((S_, 'for d in "$SUBFLEET_ROOT"; do find $d -name x; done'), (S_, 'for d in "$HOME"; do find $d -name x; done')),
    # P3-3: searchers the rule does not know
    ((S_, "fd -H AGENTS.md ~/.subfleet"), (S_, "fd -H AGENTS.md ~")),
    ((S_, "bfs ~/.subfleet -name AGENTS.md"), (S_, "bfs ~ -name AGENTS.md")),
    ((S_, "ag -l foo ~/.subfleet"), (S_, "ag -l foo ~")),
    ((S_, "git grep --no-index foo ~/.subfleet/jobs"), (S_, "git grep --no-index foo ~/PolicyEngine")),
    ((S_, "tree ~/.subfleet"), (S_, "tree ~")),
    ((S_, "du -sh ~/.subfleet/jobs"), (S_, "du -sh ~/PolicyEngine")),
    ((S_, "ls -R ~/.subfleet"), (S_, "ls -R ~")),
]
#: Scoped searches the new hook denies because a new root sits where the rule reads one (the
#: over-approximations every root has). The old hook allows them. P3-2's find -newer case left
#: this list in r3: ~/.subfleet/daemon.log is a file, which decision 1 never counts as broad
#: (``R3_RESOLVED``).
R3_RESOLVED = [(S_, "find ~/.subfleet/jobs/20261024-115712-x -newer ~/.subfleet/daemon.log")]
R2_OVER_APPROXIMATIONS = [
    (S_, "cd ~/.subfleet/jobs && rg -n foo 20261024-115712-x/a1/out.md"),
    (S_, "ls -t ~/.subfleet/jobs | head -5 | xargs -I{} rg -l foo ~/.subfleet/jobs/{}/a1"),
]


@pytest.mark.documented_limitation
def test_known_limits_r2_lists_are_pre_existing_for_every_root(hooks):
    """docs/reports/2026-10-24-guard-subfleet-root.md, "Known limits": relative paths from a
    lane's worktree, a trailing find under xargs, unnormalized spellings (env spellings
    included), sub-scripts and quoted loop lists, and searchers the rule does not know pass
    for the new roots exactly as for the existing ones; and the scoped commands the new roots
    over-approximate are denied. A follow-up (D-LANE-SCOPE2) must change this deliberately."""
    cases = [(cmd, cwd) for pairs in R2_LIMITS for cwd, cmd in pairs]
    for which in ("old", "new"):
        outs = judge_all(hooks[which], cases)
        for case in cases:
            assert rule(outs[case]) is None, (which, case)
    over = [(cmd, cwd) for cwd, cmd in R2_OVER_APPROXIMATIONS]
    new, old = judge_all(hooks["new"], over), judge_all(hooks["old"], over)
    for case in over:
        assert rule(new[case]) == "unscoped-search", case
        assert rule(old[case]) is None, case
    # r3 decision 1 flipped P3-2's find -newer case: the r2 hook denied it, the new one allows it.
    resolved = [(cmd, cwd) for cwd, cmd in R3_RESOLVED]
    new, r2 = judge_all(hooks["new"], resolved), judge_all(hooks["r2"], resolved)
    for case in resolved:
        assert rule(new[case]) is None, case
        assert rule(r2[case]) == "unscoped-search", case


# ---------------------------------------------------------------- S5

def test_s5_env_spellings_decide_as_their_home_expansions(hooks, corpus):
    """S5 over the generator: each env-spelled twin gets exactly the decision of its
    ``$HOME/.subfleet`` expansion (``home_ref``: the same draw over ``$HOME/.subfleet``, or
    ``${HOME}/.subfleet`` for a braced twin) and of its source command (the same draw over
    <home>/.subfleet[/…] in the source's home spelling); the twin shares its context, searcher,
    quoting, slash and depth bound."""
    twins = [case for case in corpus["generated"] if "twin_of" in case]
    outs = judge_all(hooks["new"], [(c["command"], c["cwd"]) for c in twins]
                     + [(c["home_ref"], c["cwd"]) for c in twins]
                     + [(c["twin_of"], c["cwd"]) for c in twins])
    differ = []
    counts = {"twins": len(twins), "home_refs": len({c["home_ref"] for c in twins}),
              "sources": len({c["twin_of"] for c in twins}), "denied": 0, "allowed": 0,
              "by_spelling": dict.fromkeys(ENV_SPELLINGS, 0), "scale": scale()}
    for case in twins:
        twin = rule(outs[(case["command"], case["cwd"])])
        ref = rule(outs[(case["home_ref"], case["cwd"])])
        source = rule(outs[(case["twin_of"], case["cwd"])])
        if not twin == ref == source:
            differ.append((case["command"], twin, case["home_ref"], ref, case["twin_of"], source))
        counts["denied" if twin else "allowed"] += 1
        counts["by_spelling"][case["spelling"]] += 1
    report("S5", counts)
    assert not differ, differ[:10]
    assert counts["denied"] and counts["allowed"] and all(counts["by_spelling"].values()), counts


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
    # r2: each env spelling, alone, one and two components down, one template each in turn
    for i, env in enumerate(ENV_SPELLINGS):
        for j, rest in enumerate(("", "/x_y-z.1", "/jobs/20261024-115712-x", "/", '"/jobs', '"/jobs/a/b')):
            root = '"' + env + rest[1:] + '"' if rest.startswith('"') else env + rest
            commands.append(templates[(i + j) % len(templates)].format(root))
    for command, out in judged(hooks["new"], commands).items():
        assert claude_blocks(command) == (rule(out) is not None), command


def test_s4_the_claude_half_differs_on_new_roots_only_where_it_differs_on_home_children(hooks, corpus, verdicts):
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
    # r2: an env-spelled twin, against the same draw over $HOME/.subfleet (``home_ref``). Not
    # against its source (``twin_of``): that may spell the root ~ or /Users/<user>, and quoting
    # changes those differently from a variable. A quoted ~ is a literal folder to the analyzer
    # while "$SUBFLEET_ROOT" is expanded, and a single-quoted '/Users/<user>/.subfleet' is a path
    # while '$SUBFLEET_ROOT' is literal; the Codex hook judges the text and gives every spelling
    # one verdict (S5). (Round r2 compared with the source and failed on exactly those, at scales 1 and 2:
    # `grep -r x '$SUBFLEET_ROOT/'`, `cd '${SUBFLEET_HOME}/jobs' && rg foo`,
    # `sh -c "rg --files \"$SUBFLEET_ROOT/jobs\""`.) Each half must give a twin its reference's
    # verdict, so the analyzer differs from the hook on a twin only where it does on that reference.
    twins = [case for case in corpus["generated"] if "twin_of" in case]
    refs = judge_all(hooks["new"], [(case["home_ref"], case["cwd"]) for case in twins])
    counts.update(twins=len(twins), twin_agree=0, ref_agree=0)
    asymmetric = []
    for case in twins:
        codex_twin = rule(new[(case["command"], case["cwd"])]) is not None
        codex_ref = rule(refs[(case["home_ref"], case["cwd"])]) is not None
        claude_twin, claude_ref = claude_blocks(case["command"]), claude_blocks(case["home_ref"])
        if (codex_twin, claude_twin) != (codex_ref, claude_ref):
            asymmetric.append((case["command"], case["home_ref"], (codex_twin, claude_twin), (codex_ref, claude_ref)))
        counts["twin_agree"] += claude_twin == codex_twin
        counts["ref_agree"] += claude_ref == codex_ref
        if claude_ref == codex_ref and claude_twin != codex_twin:
            regressions.append(case["command"])
    deep = [c for c in corpus["generated"] if c["kind"] == "deep" and not c["bounded"]]
    deep_blocked = [c["command"] for c in deep if claude_blocks(c["command"])]
    counts["deep_cases"] = len(deep)
    counts["deep_blocked_by_claude"] = len(deep_blocked)
    report("S4", counts)
    assert not asymmetric, asymmetric[:10]
    assert not regressions, regressions[:10]
    assert not deep_blocked, deep_blocked[:10]
    assert counts["pairs"] > 0


# ---------------------------------------------------------------- r3: path words judged on disk

#: The hook's path words (the "r3 path words" blocks): a run of characters other than whitespace
#: and | ; & ( ) < >, its quote marks dropped, that starts with a root spelling.
PATH_RUN = re.compile(r"[^\s|;&()<>]+")
SPELLED = re.compile(r"(~|\$HOME|\$SUBFLEET_(ROOT|HOME))(/|$)|/")
#: A root by name: the broad roots the region's comment lists, spelled as the text rules read
#: them (``${…}`` without braces), each with or without a trailing slash.
ROOT_NAME = re.compile(r"""(/|/\*|/Users/?|((/Users/[^/\s'"]+|~|\$HOME)(/(TheAxiomFoundation|"""
                       r"""RulesFoundation|PolicyEngine|Library/Caches|\.cache|\.local|"""
                       r"""\.subfleet(/[^/\s'"]+)?))?|\$SUBFLEET_(ROOT|HOME)(/[^/\s'"]+)?|"""
                       r"""(/private)?/tmp)/?)""")
GLOB_CHAR = re.compile(r"[*?[]")


def spelled_abs(word: str, home: str = FAKE_HOME) -> str | None:
    """The absolute path a root spelling names (``$SUBFLEET_*`` as <home>/.subfleet), or None."""
    for spelling, value in (("~", home), ("$HOME", home), ("$SUBFLEET_ROOT", home + "/.subfleet"),
                            ("$SUBFLEET_HOME", home + "/.subfleet")):
        if word == spelling or word.startswith(spelling + "/"):
            return value + word[len(spelling):]
    return word if word.startswith("/") else None


def norm(path: str) -> str:
    out: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            out = out[:-1]
        else:
            out.append(part)
    return "/" + "/".join(out)


def walk_prefix(word: str) -> tuple[str, bool]:
    """Decision 2: (the word before its first ``**/`` component or its trailing run of bare
    ``*`` / ``.*`` (or a last ``**``) components, one trailing slash dropped first; whether one was cut)."""
    at = word.find("/**/")
    if at >= 0:
        return word[:at] or "/", True
    b = word[:-1] if word.endswith("/") and word != "/" else word
    walk = False
    while re.search(r"/(\*|\.\*|\*\*)$", b):
        b, walk = b[:b.rindex("/")], True
    return b or "/", walk


def listed(directory: str, home: str) -> bool:
    """Decision 3: a folder that can hold a broad root by name."""
    if directory in ("/", "/Users", "/private", home, home + "/Library", home + "/.subfleet"):
        return True
    return re.fullmatch(r"/Users/[^/]+(/(Library|\.subfleet))?", directory) is not None


def is_file(path: str) -> bool:
    """Decision 1 on disk: an existing non-directory (links followed), as ``[ -e ] && [ ! -d ]``."""
    real = fs_str(path)
    return os.path.exists(real) and not os.path.isdir(real)


QUOTED_RUN = re.compile(r"""(?:[^\s|;&()<>"']|"[^"]*"|'[^']*')+""")
#: REFERENCE_SH's verdicts on written words: (home, word) -> broad.
_REF_CACHE: dict[tuple[str, str], bool] = {}


def reference_cached(words, home: str) -> list[bool]:
    """REFERENCE_SH's verdicts for ``words``, the ones not yet known taken in one run."""
    missing = sorted({w for w in words if (home, w) not in _REF_CACHE})
    if missing:
        for word, verdict in zip(missing, reference_verdicts(missing, home)):
            _REF_CACHE[(home, word)] = verdict
    return [_REF_CACHE[(home, w)] for w in words]


def command_path_words(command: str) -> list[tuple[str, bool]]:
    """(bare word, quoted) for every path word the hook may judge: r3's runs (split inside quotes)
    and r3b's (a quoted string part of its word), continuations joined, ``${…}`` unbraced."""
    text = command.replace("\\\n", "")
    for name in ("HOME", "SUBFLEET_ROOT", "SUBFLEET_HOME"):
        text = text.replace("${" + name + "}", "$" + name)
    found = []
    for line in text.split("\n"):
        for run in PATH_RUN.findall(line) + QUOTED_RUN.findall(line):
            bare = run.replace('"', "").replace("'", "")
            if SPELLED.match(bare):
                found.append((bare, bare != run))
    return list(dict.fromkeys(found))


def resolvable(word: str, home: str) -> bool:
    path = spelled_abs(word, home)
    return path is not None and "$" not in path and "`" not in path


def prime_labels(commands, home: str = FAKE_HOME) -> None:
    """REFERENCE_SH's verdicts for every plain, resolvable path word of ``commands``, in one run."""
    reference_cached([w for c in commands for w, _ in command_path_words(c)
                      if not GLOB_CHAR.search(w) and resolvable(w, home)], home)


def r3_word(bare: str, quoted: bool = False, home: str = FAKE_HOME) -> str | None:
    """How the path-word pass may move one word from the text rules' verdict (r3 and r3b):
    "glob" (decisions 2 and 3 judge it, either way), "root" (broad where the text rules would not
    see it by name: a new root, a letter case, an alias, a link, a quoted part; it only denies
    more), "file" (a root by name that is not broad: a file or a link to one; it only allows more),
    or None (the text rules' verdict stands). A plain word is judged by REFERENCE_SH."""
    if not SPELLED.match(bare):
        return None
    if GLOB_CHAR.search(bare):
        return None if bare == "/*" else "glob"
    if not resolvable(bare, home):
        return None
    broad, text = reference_cached([bare], home)[0], bool(ROOT_NAME.fullmatch(bare))
    if broad and (not text or quoted):
        return "root"
    if text and not broad:
        return "file"
    return None


def path_words(command: str) -> list[str]:
    """The command's path words as the hook reads them (continuations joined, ``${…}`` unbraced)."""
    return [word for word, _ in command_path_words(command)]


def r3_labels(command: str, home: str = FAKE_HOME) -> set[str]:
    """The ways the path-word pass may move some word of the command (S1's intended differences)."""
    return {label for word, quoted in command_path_words(command) if (label := r3_word(word, quoted, home))}


#: The brief's cases (the hub's decisions 1-5, round r3), in the fixture filesystem.
R3_FILE_ALLOWED = [            # decision 1: each names a file (or a link to one)
    "grep -rn -i 'retention' ~/.subfleet/policy.json",
    "rg -n ERROR ~/.subfleet/daemon.log",
    'grep -rn codex "$HOME/.subfleet/lanes.json"',
    "rg x ${SUBFLEET_ROOT}/policy.json",
    "rg x '/Users/tester/.subfleet/policy.json'",
    "rg x ~/.subfleet/policy-link.json",
]
R3_NAME_DENIED = [             # decision 1: judged by name, as before (command, the root named)
    ("rg x ~/.subfleet/jobs", "~/.subfleet/jobs"),
    ("rg x ~/.subfleet", "~/.subfleet"),
    ("rg x ~/.subfleet/jobs-link", "~/.subfleet/jobs-link"),           # a link to a directory
    ("rg x ~/.subfleet/dangling", "~/.subfleet/dangling"),             # a dangling link
    ("rg x ~/.subfleet/missing.json", "~/.subfleet/missing.json"),
    ("rg x ~/.subfleet/$OTHER", "~/.subfleet/$OTHER"),                 # unresolved: a file named so exists
    ("rg x ~/.subfleet/policy.json ~/.subfleet/jobs", "~/.subfleet/jobs"),  # the other word still counts
    # decision 4: every direct child stays a root, the small folders too
    ("rg x ~/.subfleet/leases", "~/.subfleet/leases"),
    ("rg x ~/.subfleet/guard", "~/.subfleet/guard"),
]
R3_GLOB_ALLOWED = [            # decision 3: no match is a broad root, or no match at all
    "grep -rn x ~/.subfleet/*.json",                      # policy.json, lanes.json, a link to a file
    f"rg foo ~/.subfleet/jobs/{JOB}/*",                    # decision 2 does not reach two levels down
    "rg foo ~/Pol*/policyengine-us",                      # two levels down
    "rg foo ~/pol*",                                      # case-sensitive: no match
    "rg foo ~/*ubfleet",                                  # a leading dot only by a leading dot
    "rg x ~/.subfleet/jobs/2026*",                        # jobs/ cannot hold a root by name: text
    "rg x src/*.py",                                      # relative: text
    "grep -rn x ~/.subfleet/*.nomatch",                   # no match
    "rg x ~/.subfleet/x*",                                # no match (the text rule would deny it)
]
R3_STAR_DENIED = [             # decision 2: a trailing /* after a root is that root
    ("rg foo ~/.subfleet/*", "~/.subfleet/* (glob)"),
    ("rg foo ~/.subfleet/jobs/*", "~/.subfleet/jobs/* (glob)"),
    ('rg foo "$SUBFLEET_HOME"/worktrees/*', "$SUBFLEET_HOME/worktrees/* (glob)"),
    ("rg foo $SUBFLEET_ROOT/worktrees/*", "$SUBFLEET_ROOT/worktrees/* (glob)"),
    ("rg foo ~/*", "~/* (glob)"),
    ("rg foo ~/PolicyEngine/*", "~/PolicyEngine/* (glob)"),
    ("rg foo ~/.cache/*", "~/.cache/* (glob)"),
    ("rg foo /tmp/*", "/tmp/* (glob)"),
    ("rg foo ~/.subfleet/jobs/*/*", "~/.subfleet/jobs/*/* (glob)"),
    ("cd ~/.subfleet/jobs/* && rg foo", "~/.subfleet/jobs/* (glob) (after cd)"),
    ("for d in ~/.subfleet/jobs/*; do find $d -name x; done", "~/.subfleet/jobs/* (glob)"),
]
R3_GLOB_DENIED = [             # decision 3: some match is a broad root
    ("rg foo ~/.subfleet/j*", "~/.subfleet/j* (glob)"),
    ("rg foo ~/.sub*", "~/.sub* (glob)"),
    ("rg foo ~/Pol*", "~/Pol* (glob)"),
    ("rg foo ~/P?licyEngine", "~/P?licyEngine (glob)"),
    ("rg foo ~/[P]olicyEngine", "~/[P]olicyEngine (glob)"),
    ("rg foo ~/Lib*/Cach?s", "~/Lib*/Cach?s (glob)"),
    ("grep -r x ~/Pol*/**/*.py", "~/Pol*/**/*.py (glob)"),
    ("rg foo ~/.subfleet/[a-z]*", "~/.subfleet/[a-z]* (glob)"),      # daemon.log first, then jobs
    ("find $HOME/Pol* -name x", "$HOME/Pol* (glob)"),
]
R3_CORPUS = (R3_FILE_ALLOWED + [c for c, _ in R3_NAME_DENIED] + R3_GLOB_ALLOWED
             + [c for c, _ in R3_STAR_DENIED] + [c for c, _ in R3_GLOB_DENIED])
#: Of those, the ones whose verdict r3 moves (the r2 hook gives the other verdict).
R3_MOVED = (set(R3_FILE_ALLOWED) | {"grep -rn x ~/.subfleet/*.nomatch", "rg x ~/.subfleet/x*",
                                     "grep -rn x ~/.subfleet/*.json"}
            | {c for c, _ in R3_STAR_DENIED} - {"rg foo ~/.subfleet/*"}
            | {c for c, _ in R3_GLOB_DENIED} - {"rg foo ~/.subfleet/j*", "rg foo ~/.subfleet/[a-z]*"})


def test_r3_a_file_is_never_a_broad_root(hooks):
    """Decision 1 (reversing r2's decision 6): a root by name that names an existing
    non-directory, or a link to one, is not broad, in every spelling, quoted or not."""
    for command in R3_FILE_ALLOWED:
        assert rule(run_hook(hooks["new"], command)) is None, command


def test_r3_a_directory_a_link_to_one_and_a_name_stay_roots(hooks):
    """Decision 1's other side, and decision 4: a directory, a link to one, a dangling link, a
    missing path, a file with a trailing slash and an unresolved name are judged by name."""
    for command, root in R3_NAME_DENIED:
        out = run_hook(hooks["new"], command)
        assert rule(out) == "unscoped-search", command
        assert matched_root(out) == root, command


def test_r3_a_bare_star_after_a_root_is_that_root(hooks):
    """Decision 2, for old roots and new: the shell hands the searcher every entry of the
    root; the message names the word as written plus (glob) (decision 5)."""
    for command, root in R3_STAR_DENIED:
        out = run_hook(hooks["new"], command)
        assert rule(out) == "unscoped-search", command
        assert matched_root(out) == root, command
    assert matched_root(run_hook(hooks["new"], "find /* -name x")) == "/*"     # unchanged


def test_r3_globs_that_match_a_root_are_denied(hooks):
    """Decision 3: a glob that can match a broad root is judged by what it matches."""
    for command, root in R3_GLOB_DENIED:
        out = run_hook(hooks["new"], command)
        assert rule(out) == "unscoped-search", command
        assert matched_root(out) == root, command


def test_r3_selective_globs_are_judged_by_what_they_match(hooks):
    """Decision 3's other side: files only, two levels down, case, the dot rule, folders that
    cannot hold a root (judged by text), and no match at all."""
    for command in R3_GLOB_ALLOWED:
        assert rule(run_hook(hooks["new"], command)) is None, command


def test_r3_moves_exactly_the_cases_the_decisions_name(hooks):
    """The red evidence, kept: the r2 hook (r3's base) gives the other verdict on exactly
    R3_MOVED, and the same verdict as r3 on the rest of the brief's cases."""
    new, r2 = judged(hooks["new"], R3_CORPUS), judged(hooks["r2"], R3_CORPUS)
    moved = {command for command in R3_CORPUS if rule(new[command]) != rule(r2[command])}
    assert moved == R3_MOVED


def test_r3_a_glob_with_nothing_to_match_is_not_judged(hooks):
    """No match: allowed, even where the text rule would deny the word. ~/.subfleet/j* in a
    home without jobs/ passes; ~/.subfleet/w* there still matches worktrees/."""
    assert not fs_path("/Users/nojobs/.subfleet/jobs").exists()
    assert rule(run_hook(hooks["new"], "rg x ~/.subfleet/j*", home="/Users/nojobs")) is None
    assert rule(run_hook(hooks["new"], "rg x ~/.subfleet/w*", home="/Users/nojobs")) == "unscoped-search"
    assert rule(run_hook(hooks["r2"], "rg x ~/.subfleet/j*", home="/Users/nojobs")) == "unscoped-search"


def test_r3_the_cap_bounds_what_is_judged(hooks):
    """The cap (a test-only seam): at 0 no match is judged, so ~/Pol* is not judged. r3b counts
    directory entries read, not matches (``test_r3b_ported_the_listing_is_capped``)."""
    assert rule(run_hook(hooks["new"], "rg foo ~/Pol*", extra={CAP_SEAM: "0"})) is None
    assert rule(run_hook(hooks["new"], "rg foo ~/Pol*")) == "unscoped-search"


def test_r3_env_values_are_never_read(hooks):
    """$SUBFLEET_ROOT and $SUBFLEET_HOME are spellings of $HOME/.subfleet whatever the
    environment says: set to a folder without policy.json, or to a file, they change nothing."""
    extra = {"SUBFLEET_ROOT": "/Users/nojobs/.subfleet/jobs", "SUBFLEET_HOME": FAKE_HOME + "/.subfleet/policy.json"}
    assert rule(run_hook(hooks["new"], "rg x ${SUBFLEET_ROOT}/policy.json", extra=extra)) is None
    assert rule(run_hook(hooks["new"], "rg --files $SUBFLEET_HOME", extra=extra)) == "unscoped-search"
    assert rule(run_hook(hooks["new"], "rg foo $SUBFLEET_ROOT/j*", extra=extra)) == "unscoped-search"


def test_r3_an_unresolved_spelling_is_no_root_spelling(hooks):
    """The brief listed ``rg x $OTHER/.subfleet/policy.json`` as denied; it is not a root
    spelling (only ~, $HOME, /Users/<user> and the two env names are), so every hook allows
    it. Its intent, a root by name whose path still holds $ after resolving, is judged by
    name: ``rg x ~/.subfleet/$OTHER`` (R3_NAME_DENIED) stays denied though a file named
    ``$OTHER`` exists."""
    assert is_file(FAKE_HOME + "/.subfleet/$OTHER")
    for which in ("new", "r2", "old"):
        assert rule(run_hook(hooks[which], "rg x $OTHER/.subfleet/policy.json")) is None, which


def test_r3_a_glob_never_runs_a_substitution(hooks, tmp_path):
    """Decision 3's listing is bash's pathname expansion of a quoted folder and a bare *, and its
    matching a [[ == ]] against a pattern whose every literal character is escaped: a pattern holding
    $( ) or a backtick runs nothing, through the hook and through pw_globbroad, pw_ls and pw_word."""
    marks = [tmp_path / name for name in ("pwned-dollar", "pwned-tick", "pwned-glob-dollar", "pwned-glob-tick")]
    for command in (f"rg x ~/.subfleet/$(touch {marks[0]})*", f"rg x ~/.subfleet/`touch {marks[1]}`*",
                    f"rg x ~/Pol*/$(touch {marks[0]})", f"grep -r x ~/[P]ol`touch {marks[1]}`*"):
        run_hook(hooks["new"], command)
    out = run_glob_harness(f'pw_globbroad "{FAKE_HOME}/\\$(touch {marks[2]})*"; '
                           f'pw_globbroad "{FAKE_HOME}/\\`touch {marks[3]}\\`*"; '
                           f'pw_ls "{FAKE_HOME}/\\$(touch {marks[2]})"; pw_word "~/[P]\\`touch {marks[3]}\\`*" z; echo done')
    assert out.stdout.strip() == "done", out
    assert [m for m in marks if m.exists()] == []


def r3_block() -> str:
    text = HOOK.read_text(encoding="utf-8")
    begin, end = R3_BLOCKS[0]
    return text[text.index(begin):text.index(end) + len(end)]


def run_glob_harness(body: str, prelude: str = "") -> subprocess.CompletedProcess:
    """``body`` run in /bin/bash after the hook's r3 functions (``run_r3_functions``), with this
    module's fixture as the seam and its home, ``prelude`` first."""
    return run_r3_functions(prelude + "\n" + body, FAKE_HOME)


#: Shell states a caller of the path-word functions may be in: each must come back unchanged, and
#: none may change what the listing and the matching find.
SHELL_STATES = ["", "set -f", "shopt -s nullglob", "shopt -s dotglob", "shopt -s nocaseglob",
                "shopt -s extglob", "shopt -s failglob", "IFS=:", "unset IFS", "GLOBIGNORE='*'",
                "shopt -s nocasematch", "export LC_ALL=en_US.UTF-8",
                "set -f; shopt -s nullglob dotglob nocaseglob extglob failglob nocasematch; IFS=/; GLOBIGNORE=x"]


@pytest.mark.parametrize("state", SHELL_STATES)
def test_r3_glob_expansion_restores_every_setting(state):
    """pw_ls turns globbing on, sets nullglob and dotglob, clears nocaseglob, extglob, failglob and
    GLOBIGNORE for its one expansion, pw_match clears nocasematch and extglob for its matching, and
    pw_normalize, pw_stall and pw_isbroad set nocasematch for theirs; each puts every one back as it
    was, and no answer depends on the caller's settings (r3b: the same probe through pw_word)."""
    probe = ("st() { printf '%s|%q|%q|' \"$-\" \"${IFS-unset}\" \"${GLOBIGNORE-unset}\"; "
             "shopt -p nullglob dotglob nocaseglob extglob failglob nocasematch | tr '\\n' ' '; }\n"
             "before=$(st)\n"
             "v() { pw_word \"$1\" z; case $pw_t in /Users/@sfglob*) printf 0 ;; *) printf 1 ;; esac; }\n"
             "r=$(v '~/P*'; v '~/p*'; v '~/*ubfleet'; v '~/.sub*'; v '/USERS/tester'; v '~/PolicyEngine.x')\n"
             "pw_ls " + FAKE_HOME + "; n=${#pw_ents[@]}\n"
             "after=$(st)\n"
             "printf '%s\\n%s\\n%s %s\\n' \"$before\" \"$after\" \"$r\" \"$n\"\n")
    out = run_glob_harness(probe, prelude=state)
    lines = out.stdout.splitlines()
    assert len(lines) == 3, out
    assert lines[0] == lines[1], (state, lines)
    entries = len(os.listdir(fs_path(FAKE_HOME)))
    # P*, .sub* and /USERS/tester are roots; p*, *ubfleet and a missing name are not
    assert lines[2] == f"011001 {entries}", (state, lines[2])


def test_r3_test_seams_never_reach_a_lane():
    """The seams are names the daemon's launch environment, an exact allowlist, never carries."""
    from subfleet import launch_env
    for name in (FS_SEAM, CAP_SEAM):
        assert not launch_env.inherited_name(name), name
        assert name not in launch_env.build_environment(environ={"PATH": "/usr/bin:/bin", name: "/x"})


def test_s1_r3_moves_each_decision_only_its_own_way(hooks, corpus):
    """Against the r2 hook (r3's base), on every corpus command the path-word pass labels: a "file"
    word only ever allows more (it becomes F), a "root" word only ever denies more (it becomes a
    placeholder root); a "glob" word may do either, as may a mix. Unlabelled commands get r2's
    verdict exactly. Labels come from REFERENCE_SH, not from the hook (r3b)."""
    pairs = [(case["command"], case["cwd"]) for case in corpus["generated"]] + corpus["fixed"]
    prime_labels([command for command, _ in pairs])
    new, r2 = judge_all(hooks["new"], pairs), judge_all(hooks["r2"], pairs)
    wrong, counts = [], {"labelled": 0, "moved": 0, "file": 0, "root": 0, "glob": 0}
    for key in dict.fromkeys(pairs):
        labels = r3_labels(key[0])
        n, o = rule(new[key]), rule(r2[key])
        for label in labels:
            counts[label] += 1
        counts["labelled"] += bool(labels)
        counts["moved"] += n != o
        if not labels and n != o:
            wrong.append(("unlabelled", key, o, n))
        elif labels == {"file"} and o is None and n is not None:
            wrong.append(("file denied more", key, o, n))
        elif labels == {"root"} and o is not None and n is None:
            wrong.append(("root allowed more", key, o, n))
    report("S1-r3", counts)
    assert not wrong, wrong[:10]
    assert counts["file"] and counts["root"] and counts["glob"], counts


# ---------------------------------------------------------------- r3: the property and the shells

PROP_HOME = "/Users/prop"
#: The six kinds of child, each a name in the fixture's /Users/prop/.subfleet.
KINDS = {"file": "f.log", "dir": "d", "link-to-file": "lf.log", "link-to-dir": "ld",
         "dangling": "dl.log", "missing": "m.log"}
#: The six spellings of ~/.subfleet (the env names in both forms).
PROP_SPELLINGS = ["~/.subfleet", "$HOME/.subfleet", "${HOME}/.subfleet", PROP_HOME + "/.subfleet",
                  "$SUBFLEET_ROOT", "${SUBFLEET_ROOT}", "$SUBFLEET_HOME", "${SUBFLEET_HOME}"]
QUOTINGS = ["none", "single", "double", "partial"]
PROP_SEARCHES = ["rg -n foo {}", "grep -rn x {}", "find {} -name x", "rg --files {}"]


def plain_word(kind: str, spelling: str, quoting: str, slash: bool, star: bool) -> str:
    tail = "/" + KINDS[kind] + ("/*" if star else "") + ("/" if slash else "")
    if quoting == "single":
        return "'" + spelling + tail + "'"
    if quoting == "double":
        return '"' + spelling + tail + '"'
    if quoting == "partial":
        return '"' + spelling + '"' + tail
    return spelling + tail


def plain_broad(kind: str, slash: bool, star: bool) -> bool:
    """The rule (r3b): broad iff the resolved path, normalized (a trailing slash dropped, as
    normalize drops it), is not an existing non-directory; before a trailing /* the path is judged
    the same way (r3's decision 2, where a file is no root by decisions 1 and 2)."""
    return not is_file(PROP_HOME + "/.subfleet/" + KINDS[kind])


#: Glob words over /Users/prop: (literal part, pattern component, tail).
PROP_GLOBS = [
    *((home, comp, tail) for home in ("~", "$HOME", "${HOME}", PROP_HOME)
      for comp in ("P*", "P?licyEngine", "[P]olicyEngine", "[!p]olicyEngine", "[^p]olicyEngine",
                   "[[:upper:]]olicyEngine", "pol*", "*ubfleet", ".sub*", ".c*", "Pic*", "*.txt", "x*")
      for tail in ("", "/policyengine-us", "/*", "/**/x.py")),
    *((home, comp, tail) for home in ("~", "$HOME") for comp in ("Lib*", "L?brary")
      for tail in ("", "/Cach?s", "/Caches", "/x*")),
    *((sub, comp, tail) for sub in ("~/.subfleet", "$SUBFLEET_ROOT", "${SUBFLEET_HOME}")
      for comp in ("*.log", "f*", "l?", "lf*", "ld*", "d*", "[f]*", "m*", "?.log", "[!d]*")
      for tail in ("", "/*")),
    *(("", comp, tail) for comp, tail in (("U*", ""), ("t*", ""), ("p*", ""), ("p*", "/tmp"),
                                          ("U*", "/prop"), ("x*", ""))),
    *(("/Users", comp, tail) for comp, tail in (("p*", ""), ("p*", "/Pictures"), ("pr?p", "/.subfleet"),
                                                ("[n]*", ""), ("q*", ""))),
    *(("~/Library", comp, "") for comp in ("C*", "Cach?s", "x*")),
]


def glob_word(literal: str, comp: str, tail: str) -> str:
    return literal + "/" + comp + tail


def shell_matches(shell: str, word: str, home: str) -> list[str]:
    """The real expansion of ``word`` in /bin/bash or /bin/zsh -f, nothing matched -> [], in the
    fixture: the home is the seam's, $SUBFLEET_* are its .subfleet, / is the seam folder."""
    fs = str(_FS["root"])
    text = fs + word if word.startswith("/") else word
    env = {"PATH": os.environ["PATH"], "HOME": fs + home, "SUBFLEET_ROOT": fs + home + "/.subfleet",
           "SUBFLEET_HOME": fs + home + "/.subfleet"}
    if shell == "bash":
        argv = ["/bin/bash", "--noprofile", "--norc", "-c",
                "shopt -s nullglob; for m in " + text + "; do printf '%s\\n' \"$m\"; done"]
    else:
        argv = ["/bin/zsh", "-f", "-c", "setopt NULL_GLOB; for m in " + text + "; do print -r -- $m; done"]
    out = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=fs, timeout=60)
    assert out.returncode == 0, (shell, word, out.stderr)
    return out.stdout.splitlines()


@functools.lru_cache(maxsize=None)
def real_broad(shell: str, word: str, home: str = PROP_HOME) -> bool:
    """The oracle (r3b): a glob word is broad iff some real match is broad as a written path, by
    REFERENCE_SH (a broad root by name that is no file, or a path that leads to one through links;
    the seam folder off and //, . and .. resolved first). A ``**/`` component or a trailing run of
    bare ``*`` / ``.*`` (decision 2) makes the part before it the root, written or expanded and
    judged the same way. No match: not broad."""
    fs = str(_FS["root"])
    prefix, walk = walk_prefix(word)
    if not GLOB_CHAR.search(prefix):
        return bool(walk and reference_cached([prefix], home)[0])
    paths = [norm(m[len(fs):]) for m in shell_matches(shell, prefix, home) if m.startswith(fs + "/")]
    return any(reference_cached(paths, home))


_PROP: dict[tuple[str, str], str] = {}


def judge_prop(hook: Path, commands, home: str = PROP_HOME) -> dict[str, str]:
    """The hook over commands in ``home``, four at a time, memoized, and read from and written
    to GUARD_DIFF_CACHE like ``judge_all`` (its cwd field names the home: ``<cwd>#home=<home>``)."""
    digest = hashlib.sha256(Path(hook).read_bytes() + FS_DIGEST.encode()).hexdigest()
    where = f"{SCOPED_CWD}#home={home}"
    _load_cache()
    missing = []
    for command in dict.fromkeys(commands):
        if (home, command) in _PROP:
            continue
        if (digest, where, command) in _CACHED:
            _PROP[(home, command)] = _CACHED[(digest, where, command)]
        else:
            missing.append(command)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(run_hook, hook, command, home=home): command for command in missing}
        for future in as_completed(futures):
            command, out = futures[future], future.result()
            _PROP[(home, command)] = out
            _remember(digest, (command, where), out)
    return {c: _PROP[(home, c)] for c in commands}


def prop_command(word: str, index: int) -> str:
    return PROP_SEARCHES[index % len(PROP_SEARCHES)].format(word)


PLAIN_SPACE = [(k, s, q, sl, st) for k in KINDS for s in PROP_SPELLINGS for q in QUOTINGS
               for sl in (False, True) for st in (False, True)]
PROPERTY_SEED = 20261025
PROPERTY_EXAMPLES = 2000


def test_r3_property_the_verdict_is_the_rule():
    """Hypothesis, seed PROPERTY_SEED, PROPERTY_EXAMPLES examples, over the child's kind (file,
    directory, link to a file, link to a directory, dangling link, missing), its spelling (the
    eight in PROP_SPELLINGS), its quoting (none, single, double, partial), a trailing slash or
    not and a trailing /* or not, and over the glob words of PROP_GLOBS. The oracle:
      - a plain word is broad iff its resolved path (the trailing slash kept) is not an
        existing non-directory; with a trailing /*, always broad;
      - a glob word is broad iff some real match is a broad root (``real_broad`` in bash,
        which the hook's expansion is); no match, allowed.
    The hook denies iff the word is broad. The space is finite (PLAIN_SPACE and PROP_GLOBS), so
    the hook's verdicts are taken for all of it first, four at a time; every drawn example is
    then checked against its verdict and the oracle."""
    from hypothesis import given, seed, settings, strategies as st, HealthCheck

    plain = [(case, prop_command(plain_word(*case), i)) for i, case in enumerate(PLAIN_SPACE)]
    globs = [(case, prop_command(glob_word(*case), i)) for i, case in enumerate(PROP_GLOBS)]
    verdicts = judge_prop(HOOK, [c for _, c in plain] + [c for _, c in globs])
    seen = {"plain": set(), "glob": set(), "denied": 0, "allowed": 0}

    @seed(PROPERTY_SEED)
    @settings(max_examples=PROPERTY_EXAMPLES, deadline=None, database=None, derandomize=False,
              suppress_health_check=list(HealthCheck))
    @given(st.one_of(st.sampled_from(plain).map(lambda x: ("plain",) + x),
                     st.sampled_from(globs).map(lambda x: ("glob",) + x)))
    def check(example):
        family, case, command = example
        broad = plain_broad(case[0], case[3], case[4]) if family == "plain" else real_broad("bash", glob_word(*case))
        denied = rule(verdicts[command]) == "unscoped-search"
        assert denied == broad, (command, case, broad)
        assert rule(verdicts[command]) in (None, "unscoped-search"), command
        seen[family].add(case)
        seen["denied" if denied else "allowed"] += 1

    check()
    report("property", {"seed": PROPERTY_SEED, "examples": PROPERTY_EXAMPLES, "plain_cases": len(seen["plain"]),
                        "glob_cases": len(seen["glob"]), "plain_space": len(PLAIN_SPACE), "glob_space": len(PROP_GLOBS),
                        "denied": seen["denied"], "allowed": seen["allowed"]})
    assert seen["denied"] and seen["allowed"]


def test_r3_globs_agree_with_real_zsh_and_bash():
    """The differential: every glob case (PROP_GLOBS, and the brief's in the tester home) is
    expanded by real /bin/zsh -f and /bin/bash in the fixture, and the hook denies iff a real
    match is a broad root (``real_broad``), in each shell. Two forms differ between them by
    design, and both shells still agree on the verdict: zsh walks a ``**/`` (bash 3.2 reads it as
    ``*``), which decision 2 judges by the part before it; bash's ``.*`` matches . and .., which
    no case here has."""
    words = [glob_word(*case) for case in PROP_GLOBS]
    commands = [prop_command(word, i) for i, word in enumerate(words)]
    verdicts = judge_prop(HOOK, commands)
    brief = {"~/.subfleet/*.json": False, "~/Pol*/policyengine-us": False, "~/pol*": False, "~/*ubfleet": False,
             "~/.subfleet/*.nomatch": False, "~/.subfleet/x*": False, "~/.subfleet/j*": True, "~/.sub*": True,
             "~/Pol*": True, "~/P?licyEngine": True, "~/[P]olicyEngine": True, "~/Lib*/Cach?s": True,
             "~/Pol*/**/*.py": True, "~/.subfleet/[a-z]*": True}
    brief_out = judge_prop(HOOK, ["rg foo " + w for w in brief], home=FAKE_HOME)
    differ, counts = [], {"cases": len(words) + len(brief), "denied": 0, "zsh_bash_agree": 0}
    for word, command in [*zip(words, commands), *(((w, "rg foo " + w) for w in brief))]:
        home = PROP_HOME if command in verdicts else FAKE_HOME
        out = verdicts.get(command) if home == PROP_HOME else brief_out[command]
        denied = rule(out) == "unscoped-search"
        shells = {shell: real_broad(shell, word, home) for shell in ("bash", "zsh")}
        counts["denied"] += denied
        counts["zsh_bash_agree"] += shells["bash"] == shells["zsh"]
        if home == FAKE_HOME and shells["bash"] != brief[word]:
            differ.append(("brief", word, brief[word], shells))
        if any(denied != broad for broad in shells.values()):
            differ.append((command, denied, shells))
    report("shells", counts)
    assert not differ, differ[:10]


# ---------------------------------------------------------------- r3b: snapshot-5's roots, globs and mounts

# Round r3b (2026-10-26, the hub's decisions 1-6, for parity with snapshot-5 of the Claude half,
# ~/.claude/hooks/unscoped_search.py, read from its frozen .ref copy only):
#   1. the roots are snapshot-5's: a resolved path is normalized (// . .. by name, the data volume
#      and /private/{tmp,var,etc} folded) and matched case-insensitively against BROAD_RE and the
#      home's children, wherever the home is;
#   2. a written path is judged where its links lead (realpath), except under a mount that may
#      stall (/Volumes, /Network, /net, /home), which is never resolved or tested;
#   3. a glob is judged component by component (glob_broad): a prefix that is no root is not judged,
#      a plain component keeps a path only where something is there, a glob component lists a folder
#      only when it can hold a root, only broad roots that are no file go on between components;
#   4. which folders are listed is computed (holds_root), never a fixed list;
#   5. r3's bare /* after a root, the .subfleet children and the (glob) message stand;
#   6. a link inside a listed folder that leads into a stall mount is still resolved (a shared limit).
R3B_HOME = "/Users/glob"
#: r3b's fixture entries (FS_LAYOUT takes them): the top-level roots snapshot-5 adds, mounts that may
#: stall with something inside each (no test may list or stat it), a second user, links to roots and
#: to a file, and the snapshot-5 glob home (``glob_home`` in test_unscoped_search.py.ref) at R3B_HOME.
#: Every link is relative, so it resolves inside the seam (the hook reads an absolute one inside
#: the seam too, but this module's own checks would follow it out).
R3B_LAYOUT = [
    *((d, "dir", None) for d in (
        "usr/include", "usr/local/x", "Library/Preferences", "Applications/x.app", "opt/homebrew/Cellar",
        "var/folders/zz", "private/var/folders/zz", "private/etc", "System/Volumes/Data", "System/Library",
        "Volumes/Macintosh HD/inside", "Volumes/Backup/inside", "Network/inside", "net/inside", "home/inside",
        "Users/other/PolicyEngine", "Users/Shared", "Users/tester/src", "Users/prop/src")),
    *((f, "file", None) for f in ("usr/include/stdio.h", "Volumes/Macintosh HD/x1", "Volumes/Backup/x2",
                                  "Network/n1", "net/n2", "home/h1")),
    ("Users/tester/link-to-home", "link", "."),
    ("Users/tester/link-to-pe", "link", "PolicyEngine"),
    ("Users/tester/link-to-usr", "link", "../../usr"),
    ("Users/tester/link-to-file", "link", ".subfleet/policy.json"),
    ("Users/tester/src/link-up", "link", "../../.."),
    ("Users/prop/link-to-home", "link", "."),
    ("Users/prop/link-to-pe", "link", "PolicyEngine"),
    ("Users/prop/link-to-usr", "link", "../../usr"),
    ("Users/prop/link-to-notes", "link", "notes.txt"),
    ("Users/prop/src/link-up", "link", "../../.."),
    # snapshot-5's glob_home
    *((f"Users/glob/{d}", "dir", None) for d in (
        "PolicyEngine/policyengine-us", "Library/Caches", ".subfleet/jobs/20261024-115712-x",
        ".subfleet/worktrees", "src", "Polaroid", ".cache")),
    *((f"Users/glob/{f}", "file", None) for f in (".subfleet/policy.json", ".subfleet/x.json", "notes.md",
                                                  "PolicyEngine.md")),
    ("Users/glob/link-to-root", "link", "../.."),
    ("Users/glob/link-to-file", "link", "notes.md"),
    ("Users/glob/src/link-up", "link", "../../.."),
    ("Users/glob/.subfleet/jobs/link-up", "link", "../../../.."),
]
FS_LAYOUT += R3B_LAYOUT
FS_DIGEST = hashlib.sha256(json.dumps(FS_LAYOUT).encode()).hexdigest()

#: The brief's cases (round r3b), in the fixture: (command, the root the message names).
R3B_DENIED = [
    ("rg foo /usr", "/usr"), ("rg foo /Library", "/Library"), ("rg foo /opt/homebrew", "/opt/homebrew"),
    ("rg foo /var/folders", "/var/folders"), ("rg foo /private", "/private"),
    ("rg foo /System/Volumes/Data", "/System/Volumes/Data"),
    ("rg foo /private/var/folders", "/private/var/folders"),            # folds to /var/folders
    ("rg foo ~/Library", "~/Library"), ("rg foo ~/policyengine", "~/policyengine"),     # case
    ("rg foo /users/tester", "/users/tester"),                                          # case
    ('rg foo "/Volumes/Macintosh HD"', "/Volumes/Macintosh HD"),         # by name, never stat'ed
    ("rg foo /Volumes/Ma*", "/Volumes/Ma* (glob)"),                      # the entry matched by name
    ('rg foo "/V*/Macintosh HD"', "/V*/Macintosh HD (glob)"),            # matched in /Volumes' listing
    ("rg x ~/link-to-home", "~/link-to-home"),                           # judged where the link leads
    ("rg x ~/link-to-pe", "~/link-to-pe"),                               # a link in the home to a root
    ("rg x ~/link-to-usr", "~/link-to-usr"),
    ("cd ~/link-to-home; rg x", "~/link-to-home (after cd)"),
    ("rg foo /U*/tester", "/U*/tester (glob)"),                          # the home through a glob
    ("rg foo /Users/*/PolicyEngine", "/Users/*/PolicyEngine (glob)"),    # another user folder has one
]
R3B_ALLOWED = [
    "rg foo /usr/include/stdio.h", "rg foo /usr/local/x",                # scoped
    "rg foo /Volumes/Zz*nosuch",                                         # no match
    "rg foo /Volumes/*/x*",                                              # nothing inside a mount is listed
    "rg x /U*/nosuch", "rg x /*/foo",                                    # no such names
    "rg x /*/*.py",                                                      # not a volume
    "bash -c 'rg foo /**/*.py'",                                         # bash: ** is *
    "true; bash -c 'true; rg foo /**/*.py'",                             # ... with the searcher seen
    "rg x ~/pol*",                                                       # glob case
    "rg x ~/*ubfleet",                                                   # the dot rule
    "rg x ~/s*/link-up",                                                 # only a broad folder goes on
]
#: r3 verdicts these decisions flip (each named in the report's "changed verdicts").
R3B_FLIPPED = [
    # decision 1: the trailing slash is dropped before a file is tested, as normalize does
    ("rg x ~/.subfleet/policy.json/", None),
    # decisions 1-2: the root before a trailing /* is judged as a written path, and a file is none
    ("rg foo ~/.subfleet/policy.json/*", None),
    # decision 1: spellings normalize used to miss (r2's known limits)
    ("rg --files ~//.subfleet", "unscoped-search"), ("rg --files ~/.subfleet/./jobs", "unscoped-search"),
    ("rg --files ~/.Subfleet/jobs", "unscoped-search"),
    ("rg --files /System/Volumes/Data/Users/tester/.subfleet", "unscoped-search"),
    ("rg --files ~//PolicyEngine", "unscoped-search"), ("rg --files ~/./PolicyEngine", "unscoped-search"),
    ("rg --files /System/Volumes/Data/Users/tester", "unscoped-search"),
    # a quoted string is part of its word: ~/".subfleet" is ~/.subfleet
    ('rg --files ~/".subfleet"', "unscoped-search"), ('rg --files ~/"PolicyEngine"', "unscoped-search"),
]


def test_r3b_the_new_roots_aliases_case_links_and_globs_block(hooks):
    """Decisions 1-4 on the brief's cases: each is denied and named as written ((glob) for a glob).
    The r3 hook (this branch before r3b) allowed every one but the three r3 already reached."""
    for command, root in R3B_DENIED:
        out = run_hook(hooks["new"], command)
        assert rule(out) == "unscoped-search", command
        assert matched_root(out) == root, (command, matched_root(out))


def test_r3b_scoped_paths_no_match_and_mount_contents_stay_allowed(hooks):
    """The brief's allowed cases: scoped paths, globs that match nothing (no such entry, or a name
    after a glob that is not there), nothing listed inside a mount, bash's ** and the r3 glob rules."""
    for command in R3B_ALLOWED:
        assert rule(run_hook(hooks["new"], command)) is None, command


def test_r3b_flips_exactly_these_r3_verdicts(hooks):
    """The verdicts r3b changes among r3's own pins and r2's known limits (R3B_FLIPPED)."""
    for command, want in R3B_FLIPPED:
        assert rule(run_hook(hooks["new"], command)) == want, command


# ---------------------------------------------------------------- r3b: the hook's functions

def hook_root_lines() -> str:
    """The hook's own broad_re and root_core assignments (the text rules' root regex)."""
    lines = [line for line in HOOK.read_text(encoding="utf-8").splitlines()
             if line.startswith('  broad_re="') or line.startswith('  root_core="')]
    assert len(lines) == 2
    return "\n".join(line.strip() for line in lines) + "\n"


def run_r3_functions(body: str, home: str = FAKE_HOME, fs: str | None = None, stdin: str = "",
                     timeout: int = 600) -> subprocess.CompletedProcess:
    """``body`` in /bin/bash after the hook's r3 functions (its begin block), the hook's root regex,
    and the state the judge block sets (pw_home, pw_root_re, pw_nt); ``fs`` is the seam ("" for the
    real file system; this module's fixture by default)."""
    script = (r3_block() + hook_root_lines() + 'pw_root_re="^(${root_core})$"\n'
              'pw_normalize "$HOME"; pw_home=$pw_n; pw_nt=0\n' + body + "\n")
    env = {"PATH": os.environ["PATH"], "HOME": home, FS_SEAM: str(_FS["root"]) if fs is None else fs,
           "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
    if fs == "":
        del env[FS_SEAM]
    return subprocess.run(["/bin/bash", "-c", script], input=stdin, capture_output=True, text=True, env=env,
                          timeout=timeout)


#: Reads one word per line; prints 1 when the hook's functions find it broad and 0 when not: a
#: placeholder root is broad, F is not, and a word left to the text rules is what they say by name.
WORD_VERDICTS = r"""
while IFS= read -r w; do
  pw_word "$w" "${CTX:-z}"
  case "$pw_t" in
    /Users/@sfglob*) echo 1 ;;
    F) echo 0 ;;
    *) if [[ $w =~ $pw_root_re ]]; then echo 1; else echo 0; fi ;;
  esac
done
"""


def hook_word_verdicts(words, home: str = FAKE_HOME, fs: str | None = None) -> list[bool]:
    out = run_r3_functions(WORD_VERDICTS, home, fs, stdin="".join(w + "\n" for w in words))
    assert out.returncode == 0 and not out.stderr, out.stderr[-2000:]
    lines = out.stdout.splitlines()
    assert len(lines) == len(words), (len(lines), len(words))
    return [line == "1" for line in lines]


#: The hub's tables (decisions 1 and 4), by name only: roots, and the folders a glob lists.
ROOT_TABLE_HOME = "/Users/me"
ROOT_TABLE = {
    True: ["/", "/Users", "/Users/me", "/Users/other", "/Users/other/PolicyEngine", "/Users/x/Library",
           "/Users/x/Library/Caches", "/Users/x/.cache", "/Users/x/.local", "/Users/x/.subfleet",
           "/Users/x/.subfleet/jobs", "/Users/x/TheAxiomFoundation", "/Users/x/RulesFoundation",
           "/Volumes", "/Volumes/Macintosh HD", "/Volumes/Backup", "/System", "/System/Volumes",
           "/System/Volumes/Data", "/private", "/tmp", "/private/tmp", "/var", "/private/var",
           "/var/folders", "/private/var/folders", "/opt", "/opt/homebrew", "/Library", "/Applications",
           "/usr", "/USR", "/users/ME/policyengine", "/SYSTEM/volumes/DATA/Users", "//Users//me/",
           "/Users/me/src/..", "/Users/./me/./Library", "/System/Volumes/Data/private/var/folders",
           "/Users/me/Library/../.subfleet/guard", "/Users/me/.subfleet/leases"],
    False: ["/etc", "/private/etc", "/usr/local", "/usr/bin", "/opt/homebrew/Cellar", "/var/folders/zz",
            "/Library/Caches", "/Applications/x.app", "/System/Library", "/System/Volumes/Data/x",
            "/Volumes/Macintosh HD/Users", "/Users/me/src", "/Users/me/.subfleet/jobs/x",
            "/Users/me/Library/Caches/x", "/Users/me/.subfleet-old", "/Network", "/net", "/home",
            "/tmp/x", "/Users/x/y/PolicyEngine", "/Users/me/Pictures"],
}
LISTED_IN = {   # (folder, home): whether a glob component lists it
    True: ["/", "/Users", "/Users/me", "/Users/other", "/Users/me/Library", "/Users/other/Library",
           "/Users/me/.subfleet", "/Users/other/.subfleet", "/Volumes", "/System", "/System/Volumes",
           "/System/Volumes/Data", "/private", "/private/var", "/var", "/opt"],
    False: ["/tmp", "/var/folders", "/opt/homebrew", "/Library", "/Applications", "/usr",
            "/Users/me/PolicyEngine", "/Users/me/.cache", "/Users/me/.local", "/Users/me/TheAxiomFoundation",
            "/Users/me/RulesFoundation", "/Users/me/Library/Caches", "/Users/me/.subfleet/jobs",
            "/Users/me/.subfleet/guard", "/Volumes/Macintosh HD", "/Volumes/Macintosh HD/Users", "/Network",
            "/net", "/home", "/Users/me/src", "/private/tmp", "/private/etc"],
}
OUTSIDE_HOME = "/srv/me"
LISTED_IN_OUTSIDE = {True: ["/srv/me", "/srv/me/Library", "/srv/me/.subfleet", "/", "/Users"],
                     False: ["/srv", "/srv/me/PolicyEngine", "/srv/me/.subfleet/jobs", "/srv/me/Library/Caches"]}


def by_name(function: str, paths, home: str) -> list[bool]:
    body = ('while IFS= read -r p; do pw_normalize "$p"; if %s "$pw_n"; then echo 1; else echo 0; fi; done'
            % function)
    out = run_r3_functions(body, home, fs="/nonexistent-r3b-seam", stdin="".join(p + "\n" for p in paths))
    assert out.returncode == 0 and not out.stderr, out.stderr
    return [line == "1" for line in out.stdout.splitlines()]


def test_r3b_root_table():
    """Decision 1's table, by name: each root (aliases, case, //, . and .. included) is broad and each
    near miss is not; the home's children count wherever the home is, outside /Users too."""
    for want, paths in ROOT_TABLE.items():
        assert by_name("pw_isbroad", paths, ROOT_TABLE_HOME) == [want] * len(paths), (want, paths)
    outside = ["/srv/me", "/srv/me/PolicyEngine", "/srv/me/library/CACHES", "/srv/me/.subfleet/x"]
    assert by_name("pw_isbroad", outside, OUTSIDE_HOME) == [True] * 4
    assert by_name("pw_isbroad", outside + ["/srv/me/src"], ROOT_TABLE_HOME) == [False] * 5


def test_r3b_listable_folders_are_computed():
    """Decision 4: pw_holds says yes exactly for the folders the hub's table lists on a host with the
    home at /Users/<me>, and also for a home outside /Users, its Library and its .subfleet."""
    for want, folders in LISTED_IN.items():
        got = by_name("pw_holds", folders, ROOT_TABLE_HOME)
        assert [f for f, g in zip(folders, got) if g != want] == [], want
    for want, folders in LISTED_IN_OUTSIDE.items():
        got = by_name("pw_holds", folders, OUTSIDE_HOME)
        assert [f for f, g in zip(folders, got) if g != want] == [], want


def test_r3b_only_the_disk_functions_touch_the_file_system():
    """The five pw_* disk functions (pw_islink, pw_lexists, pw_nondir, pw_readlink, pw_ls) are the
    only code in the path-word blocks that reads the file system: the seam folder, file tests,
    readlink and a pathname expansion appear nowhere else. So the mount test below, which wraps
    those five, sees every listing and stat."""
    disk = ("pw_islink", "pw_lexists", "pw_nondir", "pw_readlink", "pw_ls")
    text = HOOK.read_text(encoding="utf-8")
    blocks = [text[text.index(b):text.index(e) + len(e)] for b, e in R3_BLOCKS]
    bodies, top, name, lines = {}, [], None, []
    for line in blocks[0].splitlines():
        start = re.match(r"(pw_[a-z]+)\(\) \{(.*)$", line)
        if name is None and start and start[2].rstrip().endswith("}"):
            bodies[start[1]] = start[2]
        elif name is None and start:
            name, lines = start[1], [start[2]]
        elif name is not None and line == "}":
            bodies[name], name = "\n".join(lines), None
        elif name is not None:
            lines.append(line)
        else:
            top.append(line)
    assert set(disk) <= set(bodies), sorted(bodies)
    outside = "\n".join(body for name, body in bodies.items() if name not in disk)
    outside += "\n" + "\n".join(top)
    outside += "\n" + blocks[1] + "\n" + blocks[2]
    code = "\n".join(line for line in outside.splitlines() if not line.lstrip().startswith("#"))
    for needle in ("$pw_fs", "readlink", "[ -e", "[ -L", "[ -d", "[ -f", "[ -h", "test -", " ls "):
        assert needle not in code, needle
    assert not re.search(r"=\(\s*[^)]*\*", code), "a pathname expansion outside pw_ls"


#: Wraps the five disk functions so each call is recorded (function, path) before it runs.
TRACE_WRAP = r"""
for f in pw_ls pw_islink pw_lexists pw_nondir pw_readlink; do
  eval "orig_$(declare -f $f)"
  eval "$f() { printf '%s\t%s\n' $f \"\$1\" >> \"\$TRACE\"; orig_$f \"\$@\"; }"
done
"""
STALL = re.compile(r"/(Volumes|Network|net|home)(/|$)", re.I)


def traced(words, trace: Path, fs: str | None = None, home: str = FAKE_HOME) -> tuple[list[bool], list]:
    body = "TRACE=" + shlex.quote(str(trace)) + "\n: > \"$TRACE\"\n" + TRACE_WRAP + WORD_VERDICTS
    out = run_r3_functions(body, home, fs, stdin="".join(w + "\n" for w in words))
    assert out.returncode == 0 and not out.stderr, out.stderr[-2000:]
    calls = [tuple(line.split("\t", 1)) for line in trace.read_text().splitlines()]
    return [line == "1" for line in out.stdout.splitlines()], calls


def inside_a_mount(path: str) -> bool:
    """Whether ``path`` (as a disk function gets it) is /Volumes/<entry> or under it, or /Network,
    /net, /home or under them: /Volumes itself is the one stall folder that may be listed."""
    if not STALL.match(path):
        return False
    return path.rstrip("/").lower() != "/volumes"


MOUNT_WORDS = ["/*/*/x", "/Volumes/*/x*", "/V*/Macintosh HD", "/*/*.py", "/**/*.py", "/Volumes/Zz*nosuch",
               "/V*/Zz-nosuch", "/*/Zz-nosuch", "/U*/Zz-nosuch", "/U*/Zz-nosuch/.cache", "/net/*", "/home/*/x",
               "/Network/*", "/Volumes/Macintosh HD", "/Volumes/Macintosh HD/inside", "/V*/Backup/x2",
               "/Volumes/Ma*", "/VOLUMES/*", "/System/Volumes/Data/Volumes/*/x", "/private/../Volumes/*/x"]


def test_r3b_nothing_inside_a_mount_is_read(tmp_path):
    """Decisions 2 and 3: with every call of the five disk functions recorded, judging the brief's
    three words (and snapshot-5's mount cases) never lists or stats /Volumes/<entry>, anything under
    it, /Network, /net or /home, in the fixture, where each holds something; /Volumes itself is
    listed. /*/*/x and /Volumes/*/x* match nothing; /V*/Macintosh HD is matched in the listing."""
    verdicts, calls = traced(MOUNT_WORDS, tmp_path / "trace", fs=None, home=FAKE_HOME)
    assert [c for c in calls if inside_a_mount(c[1])] == []
    assert ("pw_ls", "/Volumes") in calls
    got = dict(zip(MOUNT_WORDS, verdicts))
    assert not got["/*/*/x"] and not got["/Volumes/*/x*"] and got["/V*/Macintosh HD"]
    assert got["/Volumes/Macintosh HD"] and got["/Volumes/Ma*"] and got["/VOLUMES/*"]
    assert not any(got[w] for w in ("/*/*.py", "/Volumes/Zz*nosuch", "/V*/Zz-nosuch", "/*/Zz-nosuch",
                                    "/U*/Zz-nosuch", "/U*/Zz-nosuch/.cache", "/net/*", "/home/*/x",
                                    "/Network/*", "/V*/Backup/x2"))


def test_r3b_nothing_inside_a_real_mount_is_read(tmp_path):
    """The same on this host's real file system, for the two words that list only / and /Volumes:
    no call reaches inside a mount. When /Volumes holds Macintosh HD, "/V*/Macintosh HD" blocks."""
    words = ["/Volumes/*/x*", "/V*/Macintosh HD"]
    verdicts, calls = traced(words, tmp_path / "trace", fs="", home="/nonexistent-r3b-home")
    assert [c for c in calls if inside_a_mount(c[1])] == []
    assert {c[1] for c in calls if c[0] == "pw_ls"} <= {"/", "/Volumes"}
    assert verdicts[0] is False
    if os.path.isdir("/Volumes") and "Macintosh HD" in os.listdir("/Volumes"):
        assert verdicts[1] is True


# ---------------------------------------------------------------- r3b: snapshot-5's vectors, ported

#: test_unscoped_search.py.ref's oracle_broad, ported (the hub's table, without BROAD_RE).
FIXED_BROAD = {"/", "/users", "/volumes", "/system", "/system/volumes", "/private", "/tmp", "/var",
               "/var/folders", "/opt", "/opt/homebrew", "/library", "/applications", "/usr"}
HOME_BROAD = {"theaxiomfoundation", "rulesfoundation", "policyengine", "library", "library/caches", ".cache",
              ".local", ".subfleet"}


def oracle_broad(canonical: str) -> bool:
    q = canonical.lower()
    data = "/system/volumes/data"
    if q == data or q.startswith(data + "/"):
        q = q[len(data):] or "/"
    for alias in ("/private/tmp", "/private/var", "/private/etc"):
        if q == alias or q.startswith(alias + "/"):
            q = q[len("/private"):]
    if q in FIXED_BROAD:
        return True
    parts = q.split("/")[1:]
    if parts[0] in ("users", "volumes") and len(parts) == 2:
        return True
    if parts[0] == "users" and len(parts) == 4 and parts[2] == ".subfleet":
        return True
    return parts[0] == "users" and len(parts) in (3, 4) and "/".join(parts[2:]) in HOME_BROAD


#: test_unscoped_search.py.ref's CANON_ROOTS and component pool (its HOME is /Users/tester).
CANON_ROOTS = ["/", "/Users", FAKE_HOME, "/Users/other", FAKE_HOME + "/PolicyEngine", FAKE_HOME + "/TheAxiomFoundation",
               FAKE_HOME + "/RulesFoundation", FAKE_HOME + "/Library", FAKE_HOME + "/Library/Caches",
               FAKE_HOME + "/.cache", FAKE_HOME + "/.local", FAKE_HOME + "/.subfleet", FAKE_HOME + "/.subfleet/jobs",
               "/Users/other/.subfleet/worktrees", "/Volumes", "/Volumes/Backup", "/System", "/System/Volumes",
               "/private", "/tmp", "/var", "/var/folders", "/opt", "/opt/homebrew", "/Library", "/Applications",
               "/usr"]
COMPONENTS = ["PolicyEngine", "TheAxiomFoundation", "Library", "Caches", ".cache", ".local", "homebrew", "folders",
              "Volumes", "Data", "src", "reviews", "policyengine-us", "Cellar", "claude-501", "T", ".subfleet", "jobs",
              "worktrees", ".subfleet-old", "a1", "x.y"]


def canonical_paths(rng: random.Random, n: int) -> list[str]:
    out = []
    for _ in range(n):
        root = rng.choice(CANON_ROOTS)
        comps = [rng.choice(COMPONENTS) for _ in range(rng.randint(0, 3))]
        out.append((root.rstrip("/") + "/" + "/".join(comps)).rstrip("/") or "/")
    return out


def spelling_variants(path: str) -> list[str]:
    """test_spelling_does_not_change_verdict's variants (trailing slash, //, /., /zz/.., letter case,
    the data volume and /private prefixes)."""
    variants = [path + "/", path.replace("/", "//"), path.rstrip("/") + "/.", path.rstrip("/") + "/zz/..",
                path.swapcase()]
    if not path.lower().startswith("/system"):
        variants.append("/System/Volumes/Data" + path.rstrip("/"))
    if path.startswith(("/tmp", "/var")):
        variants.append("/private" + path)
    return variants


def test_r3b_ported_explicit_root_blocked_iff_broad_and_spelling():
    """Ported from test_unscoped_search.py.ref: test_explicit_root_blocked_iff_broad (a canonical path
    is broad iff oracle_broad says so; none of them exists in the seam this runs over, so links
    and files play no part), test_spelling_does_not_change_verdict (every spelling variant keeps the
    verdict) and test_normalize_is_idempotent, over 600 seeded draws."""
    rng = random.Random(PROPERTY_SEED)
    paths = canonical_paths(rng, 600)
    words = paths + [v for p in paths for v in spelling_variants(p)]
    got = hook_word_verdicts(words, FAKE_HOME, fs="/nonexistent-r3b-seam")
    verdict = dict(zip(words, got))
    assert [p for p in paths if verdict[p] != oracle_broad(p)] == []
    assert [(p, v) for p in paths for v in spelling_variants(p) if verdict[v] != verdict[p]] == []
    texts = ["/" + "".join(rng.choice("/.abcXYZ_-") for _ in range(rng.randint(0, 40))) for _ in range(300)]
    body = 'while IFS= read -r p; do pw_normalize "$p"; a=$pw_n; pw_normalize "$a"; [ "$a" = "$pw_n" ] && echo 1 || echo 0; done'
    out = run_r3_functions(body, stdin="".join(t + "\n" for t in texts))
    assert out.stdout.split() == ["1"] * len(texts), out.stderr


def test_r3b_ported_roots_under_any_home():
    """Ported from test_roots_under_any_home: with HOME=/var/root, its listed folders are roots by
    the home's rule (/var/root/.subfleet/jobs/a1 is not), and none is for another home."""
    cases = [(s, True) for s in ("TheAxiomFoundation", "RulesFoundation", "PolicyEngine", "Library", "Library/Caches",
                                 ".cache", ".local", ".subfleet", ".subfleet/jobs", ".subfleet/x.y")]
    cases += [(s, False) for s in (".subfleet/jobs/a1", ".subfleet-old", "x/.subfleet", "src")]
    paths = ["/var/root/" + s for s, _ in cases]
    for home in ("/var/root", "/var/root/"):
        assert by_name("pw_isbroad", paths, home) == [want for _, want in cases], home
    assert by_name("pw_isbroad", paths, "/elsewhere") == [False] * len(paths)


def test_r3b_ported_root_names_cover_every_root():
    """Ported from test_root_names_cover_every_root: pw_holds says yes for the parent of every root
    (so no glob skips one), under /var/root too, and no for ~/.subfleet/jobs and ~/src."""
    parents = [posixpath_dirname(r) for r in CANON_ROOTS[1:]]
    assert by_name("pw_holds", parents, FAKE_HOME) == [True] * len(parents)
    subs = ["TheAxiomFoundation", "RulesFoundation", "PolicyEngine", "Library", "Library/Caches", ".cache", ".local",
            ".subfleet"]
    assert by_name("pw_holds", [posixpath_dirname("/var/root/" + s) for s in subs], "/var/root") == [True] * len(subs)
    assert by_name("pw_holds", [FAKE_HOME + "/.subfleet/jobs", FAKE_HOME + "/src"], FAKE_HOME) == [False, False]
    assert by_name("pw_holds", ["/Volumes", "/Network", "/net", "/home"], FAKE_HOME) == [True, False, False, False]


def posixpath_dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


#: test_unscoped_search.py.ref's GLOB_CASES, in its glob home (R3B_HOME): (command, a broad root is
#: walked). Its cases the overlay cannot take are in R3B_UNPORTED with the reason.
R3B_GLOB_CASES = [
    ("rg foo ~/Pol*", True), ("rg foo ~/P?licyEngine", True), ("rg foo ~/[P]olicyEngine", True),
    ("rg foo ~/[[:upper:]]olicyEngine", True), ("rg foo ~/[^Q]olicyEngine", True), ("rg foo ~/[!Q]olicyEngine", True),
    ("rg foo ~/.subfleet/j*", True), ("find ~/.subfleet/[jx]o* -name x", True), ("rg foo ~/.sub*", True),
    ("rg foo ~/.*ubfleet", True), ("rg foo ~/Lib*/Cach?s", True), ("rg foo ~/.sub*/jobs", True),
    ("rg foo ~/.sub*/worktrees/", True), ("rg foo ~/L*/Caches/..", True), ("rg foo ~/Pol*/..", True),
    ("rg foo ~/link-to-r*", True), ("grep -r foo ~/Pol*", True), ("grep -r foo ~/Pol*/**/*.py", True),
    ("rg foo ~/[P]olicyEngine/policyengine-us", False), ("rg foo ~/Pol*/policyengine-us", False),
    ("rg foo ~/.subfleet/*.json", False), ("rg foo ~/notes*", False), ("rg foo ~/link-to-f*", False),
    ("rg foo ~/src*", False), ("rg foo ~/pol*", False), ("rg foo ~/*ubfleet", False),
    ("rg foo ~/[!P]olicyEngine", False), ("rg foo ~/.subfleet/jobs/2026*", False), ("rg foo ~/.sub*/nosuch", False),
    ("rg foo ~/[.]subfleet/nosuch/..", False), ("rg foo ~/L*/Caches/nosuch", False),
    ("rg foo ~/[[:digit:]]olicyEngine", False), ("rg foo ~/[[:nosuch:]]olicyEngine", False), ("rg foo ~/Pol[", False),
    ("rg foo src/*.py", False), ("rg foo ~/src/link-u*", False), ("rg foo ~/.subfleet/jobs/link-u*", False),
    # test_a_file_is_never_a_root
    ("rg x ~/.subfleet/*.json", False), ("rg x ~/link-to-file", False),
    # the example table: aliases and the data volume
    ("find '/Volumes/Macintosh HD' -name x", True), ("find /System/Volumes/Data/Users/glob/PolicyEngine -name x", True),
    ("rg /opt src/", False), ("grep -rn /var src", False), ("grep -r -e foo /var", True), ("rg foo ~/*.md", False),
    ("for d in /private/tmp ~/Library/Caches ~/.cache; do find $d -xdev -type f -mmin -60; done", True),
    # test_symlinks_are_judged_where_they_lead (an absolute path to a link to /)
    ("rg x /Users/glob/link-to-root", True), ("cd /Users/glob/link-to-root; rg x", True),
    # test_hit_names_the_root's written form
    ("find ~/PolicyEngine/ -name x", True),
]
R3B_UNPORTED = {
    "rg foo ../Pol*": "relative words are not path words here (only ~, $HOME, $SUBFLEET_* and / are)",
    "bash -c 'rg foo ~/Pol*'": "a searcher right after a quote is never detected (a known limit)",
    "grep foo ~/Pol*/**/*.py": "a grep without -r is no searcher here; ported with -r",
    "rg foo '~/Pol*'": "a quoted glob is judged as if unquoted (r3's documented over-approximation)",
    "rg foo ~/'Pol*'": "the same",
    "noglob rg foo ~/Pol*": "noglob is not read",
    "test_glob_cases_match_the_shells": "kept as test_r3_globs_agree_with_real_zsh_and_bash over these words",
    "REAL_EXAMPLES / cd -P / CDPATH": "the overlay has no cd model past its cd rule",
}


def test_r3b_ported_glob_cases():
    """test_unscoped_search.py.ref's GLOB_CASES (and its file, alias and link examples) in its glob
    home: each is denied iff the reference walks a broad root. Unported cases: R3B_UNPORTED."""
    outs = judge_prop(HOOK, [c for c, _ in R3B_GLOB_CASES], home=R3B_HOME)
    wrong = [(c, want) for c, want in R3B_GLOB_CASES if (rule(outs[c]) == "unscoped-search") != want]
    assert wrong == []
    assert matched_root(outs["find ~/PolicyEngine/ -name x"]) == "~/PolicyEngine/"


def test_r3b_ported_a_glob_of_a_root_is_that_root():
    """Ported from test_a_glob_of_a_root_is_that_root (Hypothesis there; every case here): a ?, *,
    [c] or [!Z] in any position of a root's last component (a leading dot kept literal, a leading *
    skipped) matches that root, and the same glob plus "zz" matches nothing."""
    words, wants = [], []
    for root in ("PolicyEngine", "Library", "Library/Caches", ".cache", ".subfleet", ".subfleet/jobs",
                 ".subfleet/worktrees"):
        parent, _, name = root.rpartition("/")
        for form in ("?", "*", "[]", "[!]"):
            for k in range(name.startswith(".") + (form == "*"), len(name)):
                other = "Z" if name[k] != "Z" else "Y"
                glob = name[:k] + {"[]": "[%s]" % name[k], "[!]": "[!%s]" % other}.get(form, form) + (
                    name[k + 1:] if form != "*" else "")
                word = "~/" + (parent + "/" if parent else "") + glob
                words += [word, word + "zz"]
                wants += [True, False]
    got = hook_word_verdicts(words, R3B_HOME)
    assert [(w, want) for w, g, want in zip(words, got, wants) if g != want] == []


def test_r3b_ported_the_listing_is_capped(hooks):
    """Ported from test_glob_listing_is_capped: past the entry cap nothing is judged. The home's
    entries are listed in byte order; a cap that stops before PolicyEngine judges no match."""
    names = sorted(os.listdir(fs_path(FAKE_HOME)), key=lambda n: n.encode())
    before = names.index("PolicyEngine")
    for cap, want in ((0, None), (before, None), (before + 1, "unscoped-search"), (100000, "unscoped-search")):
        assert rule(run_hook(hooks["new"], "rg foo ~/Pol*", extra={CAP_SEAM: str(cap)})) == want, cap


# ---------------------------------------------------------------- r3b: the property, against a reference

#: An independent reference of the hub's decisions 1-4 (round r3b), written from the brief in bash,
#: not from the hook: it lowercases with its own table, normalizes by splitting on IFS=/, resolves
#: links by rewriting the whole path at the first link and starting again (the fixture's link
#: targets start with their .. parts, so a lexical join is exact there), lists with ls -A and
#: matches each glob component as a bash pattern as written. It reads one word per line and prints
#: 1 (broad) or 0. FS: the seam folder; HOME_L: the home; CAP: the entry budget per word.
REFERENCE_SH = r"""
set -u
export LC_ALL=C
UP=ABCDEFGHIJKLMNOPQRSTUVWXYZ; LO=abcdefghijklmnopqrstuvwxyz
lc() {
  local s=$1 o="" c="" i=0 pre=""
  while [ "$i" -lt "${#s}" ]; do
    c=${s:i:1}
    case "$c" in [A-Z]) pre=${UP%%"$c"*}; c=${LO:${#pre}:1} ;; esac
    o=$o$c; i=$((i + 1))
  done
  R=$o
}
lexnorm() {
  local IFS=/ part n=0 i=0 out=""
  local -a st
  set -f; local -a parts=( $1 ); set +f
  for part in ${parts[@]+"${parts[@]}"}; do
    case "$part" in ''|.) ;; ..) [ "$n" -eq 0 ] || n=$((n - 1)) ;; *) st[$n]=$part; n=$((n + 1)) ;; esac
  done
  while [ "$i" -lt "$n" ]; do out="$out/${st[$i]}"; i=$((i + 1)); done
  R=${out:-/}
}
normalize() {
  local p a l
  lexnorm "$1"; p=$R
  lc "$p"; l=$R
  if [ "$l" = /system/volumes/data ]; then p=/
  elif [ "${l#/system/volumes/data/}" != "$l" ]; then p=${p:20}; fi
  lc "$p"; l=$R
  for a in /private/tmp /private/var /private/etc; do
    if [ "$l" = "$a" ] || [ "${l#"$a"/}" != "$l" ]; then p=${p:8}; break; fi
  done
  R=$p
}
FIXED=" / /users /volumes /system /system/volumes /private /tmp /var /var/folders /opt /opt/homebrew /library /applications /usr "
HSUB=" theaxiomfoundation rulesfoundation policyengine library library/caches .cache .local .subfleet "
normalize "$HOME_L"; lc "$R"; LH=$R
isbroad() {
  local l sub n
  lc "$1"; l=$R
  case "$FIXED" in *" $l "*) return 0 ;; esac
  n=${l//[!\/]/}; n=${#n}
  case "$l" in
    /users/*|/volumes/*) [ "$n" -ne 2 ] || return 0 ;;
  esac
  case "$l" in
    /users/*/*) sub=${l#/*/*/}
      case "$HSUB" in *" $sub "*) return 0 ;; esac
      case "$sub" in .subfleet/*) case "${sub#.subfleet/}" in */*) ;; *) return 0 ;; esac ;; esac ;;
  esac
  if [ "$LH" != / ]; then
    [ "$l" != "$LH" ] || return 0
    case "$l" in
      "$LH"/*) sub=${l#"$LH"/}
        case "$HSUB" in *" $sub "*) return 0 ;; esac
        case "$sub" in .subfleet/*) case "${sub#.subfleet/}" in */*) ;; *) return 0 ;; esac ;; esac ;;
    esac
  fi
  return 1
}
stall() { lc "$1"; case "$R" in /volumes|/volumes/*|/network|/network/*|/net|/net/*|/home|/home/*) return 0 ;; esac; return 1; }
isfile() { if stall "$1"; then return 1; fi; [ -e "$FS$1" ] && [ ! -d "$FS$1" ]; }
physical() {
  local p=$1 round=0 prefix="" c="" rest="" t="" found=""
  if stall "$p"; then R=$p; return 0; fi
  while [ "$round" -lt 50 ]; do
    round=$((round + 1)); found=""; prefix=""; rest=${p#/}
    while [ -n "$rest" ]; do
      c=${rest%%/*}
      if [ "$c" = "$rest" ]; then rest=""; else rest=${rest#*/}; fi
      [ -n "$c" ] || continue
      if [ -h "$FS$prefix/$c" ]; then
        t=$(/usr/bin/readlink "$FS$prefix/$c")
        case "$t" in /*) lexnorm "$t${rest:+/$rest}" ;; *) lexnorm "$prefix/$t${rest:+/$rest}" ;; esac
        p=$R; found=1; break
      fi
      prefix="$prefix/$c"
    done
    [ -n "$found" ] || break
  done
  normalize "$p"
}
broad() {
  local r
  if isbroad "$1"; then if isfile "$1"; then return 1; fi; return 0; fi
  physical "$1"; r=$R
  [ "$r" != "$1" ] && isbroad "$r" && ! isfile "$r"
}
NAMES="TheAxiomFoundation RulesFoundation PolicyEngine Library Caches .cache .local .subfleet Users Volumes System private tmp var folders opt homebrew Applications usr Data any-name-at-all"
holds() { local n; for n in $NAMES; do normalize "$1/$n"; if isbroad "$R"; then return 0; fi; done; return 1; }
abs_of() {
  case "$1" in
    '~'|'$HOME') R=$HOME_L ;;
    '~/'*) R="$HOME_L/${1#\~/}" ;;
    '$HOME/'*) R="$HOME_L/${1#\$HOME/}" ;;
    '$SUBFLEET_ROOT'|'$SUBFLEET_HOME') R=$HOME_L/.subfleet ;;
    '$SUBFLEET_ROOT/'*|'$SUBFLEET_HOME/'*) R="$HOME_L/.subfleet/${1#*/}" ;;
    *) R=$1 ;;
  esac
}
BUDGET=0
globbroad() {
  local IFS=/ c="" first=-1 k=0 n=0 prefix="" text="" isglob="" p="" q="" name="" kind="" i=0 last=""
  local -a cs cand kinds next nkinds
  set -f; local -a raw=( $1 ); set +f
  IFS=$' \t\n'
  for c in ${raw[@]+"${raw[@]}"}; do [ -z "$c" ] || { cs[$n]=$c; n=$((n + 1)); }; done
  while [ "$k" -lt "$n" ]; do
    case "${cs[$k]}" in *[*?[]*) first=$k; break ;; esac
    prefix="$prefix/${cs[$k]}"; k=$((k + 1))
  done
  [ "$first" -ge 0 ] || return 1
  normalize "${prefix:-/}"
  isbroad "$R" || return 1
  cand=( "$R" ); kinds=( plain )
  k=$first
  while [ "$k" -lt "$n" ]; do
    text=${cs[$k]}; isglob=""; last=""
    case "$text" in *[*?[]*) isglob=1 ;; esac
    [ "$k" -ne $((n - 1)) ] || last=1
    next=(); nkinds=()
    for p in "${cand[@]}"; do
      if [ -z "$isglob" ] && ! stall "$p"; then
        normalize "$p/$text"; q=$R
        if [ -e "$FS$q" ] || [ -h "$FS$q" ]; then next[${#next[@]}]=$q; nkinds[${#nkinds[@]}]=written; fi
      elif holds "$p"; then
        while IFS= read -r name; do
          [ "$BUDGET" -gt 0 ] || break
          BUDGET=$((BUDGET - 1))
          if [ -n "$isglob" ]; then
            case "$name" in .*) case "$text" in .*) ;; *) continue ;; esac ;; esac
            [[ $name == $text ]] || continue
          else
            [ "$name" = "$text" ] || continue
          fi
          normalize "$p/$name"; q=$R
          kind=plain
          if ! stall "$p" && [ -h "$FS$p/$name" ]; then kind=link; fi
          next[${#next[@]}]=$q; nkinds[${#nkinds[@]}]=$kind
        done < <(/bin/ls -A1 "$FS$p" 2>/dev/null)
      fi
    done
    cand=(); kinds=(); i=0
    while [ "$i" -lt "${#next[@]}" ]; do
      if [ -n "$last" ] || { isbroad "${next[$i]}" && ! isfile "${next[$i]}"; }; then
        cand[${#cand[@]}]=${next[$i]}; kinds[${#kinds[@]}]=${nkinds[$i]}
      fi
      i=$((i + 1))
    done
    [ "${#cand[@]}" -gt 0 ] || return 1
    k=$((k + 1))
  done
  i=0
  while [ "$i" -lt "${#cand[@]}" ]; do
    q=${cand[$i]}
    if isbroad "$q"; then
      isfile "$q" || return 0
    elif [ "${kinds[$i]}" != plain ]; then
      physical "$q"
      [ "$R" = "$q" ] || { isbroad "$R" && ! isfile "$R" && return 0; }
    fi
    i=$((i + 1))
  done
  return 1
}
judge() {
  local w=$1 b="" walk=""
  BUDGET=$CAP
  case "$w" in
    *[*?[]*) ;;
    *) abs_of "$w"; normalize "$R"; broad "$R"; return ;;
  esac
  b=$w
  [ "$b" = / ] || b=${b%/}
  while :; do
    case "$b" in */\*|*/.\*|*/\*\*) b=${b%/*}; walk=1 ;; *) break ;; esac
  done
  [ -n "$b" ] || b=/
  case "$b" in
    *[*?[]*) abs_of "$b"; globbroad "$R" ;;
    *) abs_of "$b"; normalize "$R"; broad "$R" ;;
  esac
}
while IFS= read -r word; do
  if judge "$word"; then echo 1; else echo 0; fi
done
"""


def reference_verdicts(words, home: str) -> list[bool]:
    # The session's PATH, as run_hook passes it (the test session's keychain guard refuses a shell
    # whose PATH starts at a system folder); the script names its tools by absolute path.
    env = {"PATH": os.environ["PATH"], "FS": str(_FS["root"]), "HOME_L": home, "CAP": "20000", "LC_ALL": "C"}
    out = subprocess.run(["/bin/bash", "-c", REFERENCE_SH], input="".join(w + "\n" for w in words),
                         capture_output=True, text=True, env=env, timeout=900)
    assert out.returncode == 0 and not out.stderr, out.stderr[-2000:]
    lines = out.stdout.splitlines()
    assert len(lines) == len(words)
    return [line == "1" for line in lines]


R3B_PROPERTY_SEED = 20261026
R3B_PROPERTY_CASES = 2400
#: The generator's parts, over /Users/prop in the fixture.
PROP_PREFIXES = ["~", "$HOME", "$SUBFLEET_ROOT", "$SUBFLEET_HOME", PROP_HOME, "/USERS/PROP", "/users/prop",
                 "/System/Volumes/Data" + PROP_HOME, "/private/../Users/prop", "/", "/System/Volumes/Data",
                 "/private", "/PRIVATE", "/Users", "/Volumes", "/var", "/opt"]
PROP_NAMES = ["PolicyEngine", "Library", "Caches", ".cache", ".local", ".subfleet", "Users", "Volumes", "System",
              "private", "tmp", "var", "folders", "opt", "homebrew", "Applications", "usr", "Data", "prop", "other",
              "Shared", "tester", "d", "f.log", "ld", "lf.log", "dl.log", "link-to-home", "link-to-pe",
              "link-to-usr", "link-to-notes", "src", "link-up", "notes.txt", "Pictures", "policyengine-us",
              "include", "local", "x", "nosuch", "zz", "m.log", "Macintosh HD", "Backup", "inside"]


def glob_component(rng: random.Random, name: str) -> str:
    """One glob form of ``name``: a ? for a letter, a * for a tail or a head (a leading dot kept),
    a [c] or [!Z] for a letter, or [[:upper:]] for a capital."""
    lead = "." if name.startswith(".") else ""
    body = name[len(lead):]
    if len(body) < 2:
        return name + "*"
    k = rng.randrange(len(body))
    form = rng.choice(["?", "tail*", "*head", "[c]", "[!Z]", "upper"])
    if form == "?":
        body = body[:k] + "?" + body[k + 1:]
    elif form == "tail*":
        body = body[:k] + "*"
    elif form == "*head":
        body = "*" + body[k:]
    elif form == "[c]":
        body = body[:k] + "[" + body[k] + "]" + body[k + 1:]
    elif form == "[!Z]":
        body = body[:k] + "[!" + ("Z" if body[k] != "Z" else "Y") + "]" + body[k + 1:]
    else:
        caps = [i for i, ch in enumerate(body) if ch.isupper()]
        if not caps:
            return lead + body[:k] + "?" + body[k + 1:]
        k = rng.choice(caps)
        body = body[:k] + "[[:upper:]]" + body[k + 1:]
    return lead + body


def property_words(rng: random.Random, n: int) -> list[str]:
    words = []
    while len(words) < n:
        comps = []
        for _ in range(rng.randint(0, 4)):
            roll = rng.random()
            if roll < 0.06:
                comps.append(rng.choice([".", "..", ""]))
                continue
            name = rng.choice(PROP_NAMES)
            if rng.random() < 0.2:
                name = "".join(ch.swapcase() if rng.random() < 0.5 else ch for ch in name)
            if rng.random() < 0.35:
                name = glob_component(rng, name)
            comps.append(name)
        prefix = rng.choice(PROP_PREFIXES)
        word = prefix.rstrip("/") + ("/" + "/".join(comps) if comps else "")
        word = word or "/"
        tail = rng.random()
        if tail < 0.08:
            word += "/"
        elif tail < 0.16:
            word += "/*"
        if word not in ("/*",):
            words.append(word)
    return words


def test_r3b_property_the_hook_agrees_with_an_independent_reference(hooks):
    """R3B_PROPERTY_CASES seeded words (R3B_PROPERTY_SEED) built from root spellings (the four home
    spellings, /Users/prop in three letter cases, the data volume and /private prefixes, the other
    roots), names that exist in the fixture and names that do not, letter-case changes, ., .. and
    //, a glob character per component, and a trailing / or /*, over a home with links to itself,
    to a root, to /usr and to a file. The hook's functions (pw_word, then the text rule by name
    for a word it leaves) must give REFERENCE_SH's verdict on every word; 200 of them also go
    through the whole hook as `rg -n foo <word>`, which must deny iff the reference says broad."""
    rng = random.Random(R3B_PROPERTY_SEED)
    words = property_words(rng, R3B_PROPERTY_CASES)
    ref = reference_verdicts(words, PROP_HOME)
    got = hook_word_verdicts(words, PROP_HOME)
    differ = [(w, g, r) for w, g, r in zip(words, got, ref) if g != r]
    sample = words[:: max(1, len(words) // 200)][:200]
    commands = {w: "rg -n foo " + ('"%s"' % w if " " in w else w) for w in sample}
    outs = judge_prop(hooks["new"], list(commands.values()), home=PROP_HOME)
    verdict = dict(zip(words, ref))
    whole = [(w, outs[c]) for w, c in commands.items() if (rule(outs[c]) == "unscoped-search") != verdict[w]]
    report("property-r3b", {"seed": R3B_PROPERTY_SEED, "cases": len(words), "distinct": len(set(words)),
                            "broad": sum(ref), "glob_words": sum(bool(GLOB_CHAR.search(w)) for w in words),
                            "differ": len(differ), "whole_hook_sample": len(sample), "whole_hook_differ": len(whole)})
    assert differ == [], differ[:10]
    assert whole == [], whole[:5]
    assert 0.2 < sum(ref) / len(ref) < 0.8
