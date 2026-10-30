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

r3b (2026-10-25, the hub's decisions 1-6, for parity with snapshot-5 of the Claude half, read from
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

r4 (2026-10-26, the hub's decisions on review r3) closes review r3's walk-arounds: a .. anywhere after a glob
is folded, and the word is broad when either reading is; the trailing-run rule also runs before a **/;
every sh/bash/zsh/dash/ksh and eval script is judged (option values, --, a quoted shell name, eval's joined
arguments, a here-string or an echo/printf/cat pipe into a shell with no script, ANSI-C escapes); searchers
are found after keywords, function bodies and wrapper option values (a table), under wrappers named by
basename, and with quotes or backslashes inside the name; ${...} is no brace group and a starred word whose
group holds / or .. is broad; a path word over the scan cap (64 { or [, 4 KB) is broad unscanned and the
brace and bracket scans are one pass; rg with a device or a closed stdin searches .; print-and-exit forms
walk nothing; ~$USER, $"..." and /Volumes/<entry>/.. are read as the shell and the volume read them. Its
pins are review r3's inputs, run through the whole hook; the command pins and must-allow forms are also run
by real /bin/zsh -f and /bin/bash over the fixture with stub searchers (``r4_shell_walks``), and decision
2's words by the word oracle (``r4_real``). Two timing tests pin decision 6's cost.

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
import itertools
import json
import os
from pathlib import Path
import random
import re
import resource
import shlex
import subprocess
import threading
import time

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
#: The overlay's hook after r3 (path words judged on disk), which r3b replaces.
R3_HOOK_SHA256 = "2c3fbc1a6d563ee28e47d172343508d0613a9e7bb75567ec82a60e702980d7cf"
#: The overlay's hook after r3b (snapshot-5's roots, links and globs), which r3c replaces.
R3B_HOOK_SHA256 = "f952fd3e9648df1783cec96327b66a84a21082ba63469ef56d41749d23a5131e"
#: The overlay's hook after r3c (review r2's walk-arounds closed), which r4 replaces.
R3C_HOOK_SHA256 = "7a5ad79d3f7db4c3c08bf07b8455344a4bdae3e66224218094b398cdf1091a1a"
#: The overlay's hook after r10 (review r9's findings closed; 9eb0db09... at r9's end): TRUST's hook_sha256.
NEW_HOOK_SHA256 = "c42f05ef3b4513ec4972192e5bceeaf298df7fc523db532c6057fb7819fd865f"

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


#: r3 (2026-10-25, decisions 1-3) and r3b (2026-10-25): the three path-word blocks, each from its
#: begin marker line through its end marker line, and the comments that name them, as (r3b text,
#: r2 text).
R3_BLOCKS = [("# >>> r3 path words: begin >>>\n", "# <<< r3 path words: end <<<\n"),
             ("  # >>> r3 path words: judge >>>\n", "  # <<< r3 path words: judge <<<\n"),
             ("    # >>> r3 path words: name >>>\n", "    # <<< r3 path words: name <<<\n")]
#: r3c (2026-10-26): the edits outside the path-word blocks, as (r3c text, r3b text): the header's
#: r3c sentence, the prefilter's third term, the region's header, pw_cmd in the detection, the judged
#: lines and the cwd rule, and the message's root list. Undone first, so the r3 and r3b pairs below
#: find their text.
R3C_TEXT = [
    (r"""#     stall, and globs listed component by component where a root can be.
#     r3c (2026-10-26) closed the walk-arounds of the hub's review r2: globs
#     that match every entry, /Volumes/<entry> paths, braces and zsh's
#     alternation, links before .., searchers named by path or quote, shell
#     escapes, nested scripts and a depth bound outside its own command.
""",
     r"""#     stall, and globs listed component by component where a root can be.
"""),
    (r"""# codex addition that lets the fenced hf-dest heredoc scan below run; the
# third (D-LANE-SCOPE1 r3c) lets a searcher named by a path, after a quote or
# a backslash, through $(which …), or as egrep, fgrep, rgrep or ugrep reach the
# unscoped-search rule.
""",
     r"""# codex addition that lets the fenced hf-dest heredoc scan below run.
"""),
    (r"""  || printf '%s' "$command" | grep -qE '\*\*\* Begin Patch' \
  || printf '%s' "$command" | grep -qE "(^|[[:space:];&|(/\\\\'\"\`])(find|rg|[efru]?grep)[\`'\")]?([[:space:]]|\$)" \
""",
     r"""  || printf '%s' "$command" | grep -qE '\*\*\* Begin Patch' \
"""),
    (r"""#              r3c: also after a quote or a backslash, and named by basename:
#              a path, a backslash or a quote before the name, egrep, fgrep,
#              rgrep and ugrep, $(which X) and `which X` are read as the
#              searcher first (pw_cmd, "the command as the rules read it").
#   escape     a `-maxdepth` / `--max-depth` / `-d N` that is an unquoted
#              argument of the searcher's own simple command exempts that
#              command (r3c; before, one anywhere in the command disarmed the
#              rule, a comment or a commit message included).
""",
     r"""#   escape     any `-maxdepth` / `--max-depth` in the command disarms the rule
#              (a depth-bounded search is fine).
"""),
    (r"""#              and a glob component by component ("Path words").
#              r3c: a glob matching every entry is its folder, a glob directly
#              in a folder of ~/.subfleet is that folder, /Volumes/<entry>/x
#              is judged as /x, braces and zsh's (a|b) by their alternatives,
#              a searcher's .. after a link physically, and backslashes, ***,
#              a / inside brackets, ~user, $'…' and APFS's folded letters as
#              the shell and the volume read them ("Path words").
""",
     r"""#              and a glob component by component ("Path words").
"""),
    (r"""#              while any searcher is present (a bare `cd` is `cd ~`, r3c);
#              and `.`/`./` when the cwd itself is a broad root, as is a
#              recursive searcher given no path (r3c). A sh/bash/zsh/dash/ksh
#              -c or eval script holding a searcher is judged again as its own
#              line, to depth 4; a searcher deeper is denied (r3c). A root in
#              another command of the pipeline
""",
     r"""#              while any searcher is present; and `.`/`./` when the cwd itself
#              is a broad root. A root in another command of the pipeline
"""),
    (r"""if printf '%s' "$pw_cmd" | grep -qE "(^|[[:space:];&|('\"\\\\])find[[:space:]]"; then searcher="find"
elif printf '%s' "$pw_cmd" | grep -qE "(^|[[:space:];&|('\"\\\\])rg[[:space:]]"; then searcher="rg"
elif printf '%s' "$pw_cmd" | grep -qE "(^|[[:space:];&|('\"\\\\])grep[[:space:]]" \
  && printf '%s' "$pw_cmd" | grep -qE '(^|[[:space:]])(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--dereference-recursive)([[:space:]]|$)'; then searcher="recursive grep"
""",
     r"""if printf '%s' "$command" | grep -qE '(^|[[:space:];&|(])find[[:space:]]'; then searcher="find"
elif printf '%s' "$command" | grep -qE '(^|[[:space:];&|(])rg[[:space:]]'; then searcher="rg"
elif printf '%s' "$command" | grep -qE '(^|[[:space:];&|(])grep[[:space:]]' \
  && printf '%s' "$command" | grep -qE '(^|[[:space:]])(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--dereference-recursive)([[:space:]]|$)'; then searcher="recursive grep"
"""),
    (r"""if [ -n "$searcher" ]; then
""",
     r"""if [ -n "$searcher" ] && ! printf '%s' "$command" | grep -qE -- '--?max-?depth'; then
"""),
    (r"""  # (and `${SUBFLEET_ROOT}`/`${SUBFLEET_HOME}` without braces too). Since r3c it
  # starts from pw_cmd (searcher names, depth bounds and a bare cd read, see
  # "the command as the rules read it"), as do the searcher detection above and
  # the cwd rule below.
  judged=$(printf '%s' "$pw_cmd" | LC_ALL=C awk '{ if (sub(/\\$/, "")) printf "%s", $0; else print }' 2>/dev/null | LC_ALL=C sed 's/\$[{]HOME[}]/$HOME/g; s/\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; s/\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g' 2>/dev/null) || judged=""
  [ -n "$judged" ] || judged=$pw_cmd
""",
     r"""  # (and `${SUBFLEET_ROOT}`/`${SUBFLEET_HOME}` without braces too).
  # The searcher detection above and the cwd rule below keep looking at the raw
  # command.
  judged=$(printf '%s' "$command" | LC_ALL=C awk '{ if (sub(/\\$/, "")) printf "%s", $0; else print }' 2>/dev/null | LC_ALL=C sed 's/\$[{]HOME[}]/$HOME/g; s/\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; s/\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g' 2>/dev/null) || judged=""
  [ -n "$judged" ] || judged=$command
"""),
    (r"""  if printf '%s' "$pw_cmd" | LC_ALL=C awk 'match($0, /\\+$/) && RLENGTH % 2 == 0 { even=1 } END { exit !even }' 2>/dev/null; then
    judged=$(printf '%s' "$pw_cmd" | LC_ALL=C awk '{ if (match($0, /\\+$/) && RLENGTH % 2 == 1) printf "%s", substr($0, 1, length($0) - 1); else print }' 2>/dev/null | LC_ALL=C sed 's/\$[{]HOME[}]/$HOME/g; s/\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; s/\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g' 2>/dev/null) || judged=""
    [ -n "$judged" ] || judged=$pw_cmd
""",
     r"""  if printf '%s' "$command" | LC_ALL=C awk 'match($0, /\\+$/) && RLENGTH % 2 == 0 { even=1 } END { exit !even }' 2>/dev/null; then
    judged=$(printf '%s' "$command" | LC_ALL=C awk '{ if (match($0, /\\+$/) && RLENGTH % 2 == 1) printf "%s", substr($0, 1, length($0) - 1); else print }' 2>/dev/null | LC_ALL=C sed 's/\$[{]HOME[}]/$HOME/g; s/\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; s/\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g' 2>/dev/null) || judged=""
    [ -n "$judged" ] || judged=$command
"""),
    (r"""  # cwd rule: `find .` / `rg pat .` executed while sitting IN a broad root, and
  # (r3c) a recursive searcher given no path, which walks "." (pw_nopath).
""",
     r"""  # cwd rule: `find .` / `rg pat .` executed while sitting IN a broad root.
"""),
    (r"""        if printf '%s' "$pw_cmd" | grep -qE "(^|[[:space:];&|(])(find|rg|grep)[[:space:]]+([^|;&]*[[:space:]])?\.(/)?([[:space:]]|\$)" || pw_nopath "$pw_cmd"; then hit=1; matched=". (cwd $cwd)"; fi ;;
""",
     r"""        if printf '%s' "$command" | grep -qE "(^|[[:space:];&|(])(find|rg|grep)[[:space:]]+([^|;&]*[[:space:]])?\.(/)?([[:space:]]|\$)"; then hit=1; matched=". (cwd $cwd)"; fi ;;
"""),
    (r"""    block "[unscoped-search] Whole-tree ${searcher} over a broad root (${pw_roots_msg}) — 8 of these ran in parallel on 8/8 and drove load to 47, and ~22 sol-lane finds on 8/18 held load at 57 for hours, beachballing the Mac. Scope it: exact subdirectory, -maxdepth/--max-depth, 'git ls-files' inside a repo, 'axiom-locate' (engine|release|pypkg|corpus-file) for Axiom artifacts, or the interpreter for python package paths (python -c 'import x; print(x.__file__)'). Matched broad root: '${matched}'."
""",
     r"""    block "[unscoped-search] Whole-tree ${searcher} over a broad root (/, /Users, ~, ~/TheAxiomFoundation, ~/PolicyEngine, ~/.subfleet, ~/.subfleet/<child>, /tmp, caches) — 8 of these ran in parallel on 8/8 and drove load to 47, and ~22 sol-lane finds on 8/18 held load at 57 for hours, beachballing the Mac. Scope it: exact subdirectory, -maxdepth/--max-depth, 'git ls-files' inside a repo, 'axiom-locate' (engine|release|pypkg|corpus-file) for Axiom artifacts, or the interpreter for python package paths (python -c 'import x; print(x.__file__)'). Matched broad root: '${matched}'."
"""),
]
R3_TEXT = [
    ("""#     ~/.subfleet by default, as spellings of it. D-LANE-SCOPE1 r3
#     (2026-10-25) judged path words on disk (the "r3 path words" blocks): a
#     file is never a broad root, a trailing `/*` after a root is that root,
#     and a glob word that can match a root is judged by its matches. r3b
#     (2026-10-25) made those blocks judge the roots, globs and mounts of the
#     Claude half's snapshot-5: its BROAD_RE and home children (normalized,
#     in any letter case), links followed except under a mount that may
#     stall, and globs listed component by component where a root can be.
#     Change a root on both sides in one reviewed step: here, and in
#     unscoped_search.py (BROAD_RE, HOME_SUBDIRS, expand()).
""", """#     ~/.subfleet by default, as spellings of it. Change a root on both sides
#     in one reviewed step: here, and in unscoped_search.py (BROAD_RE,
#     HOME_SUBDIRS, expand()).
"""),
    ("""#              r3: a root by name that is an existing non-directory is not
#              broad, a root before a trailing `/*` run or a `**/` is that
#              root, and a glob word is judged by its matches ("Path words").
#              r3b: every path word is judged against snapshot-5's roots
#              (also /usr, /Library, /opt/homebrew, /Volumes/<entry> and the
#              rest, in any letter case, aliases folded), through its links,
#              and a glob component by component ("Path words").
""", ""),
    ("""#              comment, nothing in the command text lets a crawl through
#              (r3's two test seams are names no lane's environment carries).
""", """#              comment, nothing in the command text lets a crawl through.
"""),
]
#: r4 (2026-10-26): the edits outside the path-word blocks, as (r4 text, r3c text): the header's r4
#: sentence, the region's searcher and position notes, and the prefilter's fourth and fifth terms (a name
#: with a quote or a backslash inside it, and a $'...' holding an escape). Undone before R3C_TEXT.
R4_TEXT = [
    ("""#     escapes, nested scripts and a depth bound outside its own command.
#     r4 (2026-10-26) closed those of review r3: a .. after a glob, a **/
#     after a wildcard, every script a shell or eval runs, searchers after
#     keywords, wrapper options and quotes, brace groups across a / or a ..,
#     hostile words' cost, a device on stdin, print-and-exit forms, ~$USER,
#     $"..." and /Volumes/<entry>/.. .
""", """#     escapes, nested scripts and a depth bound outside its own command.
"""),
    ("""#              searcher first (pw_cmd, "the command as the rules read it").
#              r4: also after a keyword, a function body's {, a wrapper's
#              option values (a table) or a wrapper named by basename, with a
#              quote or a backslash inside the name, and in every script a
#              shell or eval runs (pw_cmd adds each as its own lines); a
#              print-and-exit form (rg --version, find --version) walks nothing.
""", """#              searcher first (pw_cmd, "the command as the rules read it").
"""),
    ("""#              line, to depth 4; a searcher deeper is denied (r3c); r4: also
#              with option values or -- before the script, a quoted shell name,
#              eval's joined arguments, and a here-string or an echo, printf or
#              cat pipe into a shell given no script. rg reading a device or a
#              closed stdin, with no path, searches . (r4). A root in
#              another command of the pipeline
""", """#              line, to depth 4; a searcher deeper is denied (r3c). A root in
#              another command of the pipeline
"""),
    (r"""# unscoped-search rule; the fourth and fifth (r4) one with a quote or a
# backslash inside its name (r''g, f\ind, $'rg'), and a $'...' holding an
# escape, which may spell one.
""", r"""# unscoped-search rule.
"""),
    (r"""  || printf '%s' "$command" | LC_ALL=C tr -d "\\\\'\"\$" | grep -qE "(^|[[:space:];&|(/\`])(find|rg|[efru]?grep)[\`)]?([[:space:]]|\$)" \
  || printf '%s' "$command" | grep -qE "\\\$'[^']*\\\\[0-7xX]" \
""", ""),
]
R3_TEXT = R4_TEXT + R3C_TEXT + R3_TEXT
#: r5 (2026-10-27): the edits outside the path-word blocks, as (r5 text, r4 text): the header's r5 sentence, the
#: prefilter's sixth term (zsh's =cmd) with its comment, and root_end's closing backtick. Undone before R4_TEXT.
R5_TEXT = [
    ("""#     $"..." and /Volumes/<entry>/.. .
#     r5 (2026-10-27) closed those of review r4: a long word's cost,
#     heredoc bodies, a depth bound beside xargs or -exec, a root before a
#     closing backtick, bash's .* and .?, the data volume's alias before a
#     mount, commands too long to read, ~{...}, ~$NAME and $(...), zsh's
#     =cmd, pipes into a shell, "$@" scripts, eval $(...) and <&0.
""", """#     $"..." and /Volumes/<entry>/.. .
"""),
    ("""  # quote, or the end; r5: also a closing backtick.
  root_core="/|/\\\\*|/Users/?|(${broad_re})/?"
  root_re="(${root_core})"
  root_end="['\\"]?([[:space:]|;&)\\`]|\\$)"
""", """  # quote, or the end.
  root_core="/|/\\\\*|/Users/?|(${broad_re})/?"
  root_re="(${root_core})"
  root_end="['\\"]?([[:space:]|;&)]|\\$)"
"""),
]
R5_TEXT += [
    ("""# escape, which may spell one; the sixth (r5) zsh's =rg, =find and =grep.
""", """# escape, which may spell one.
"""),
    ("""  || printf '%s' "$command" | grep -qE "(^|[[:space:];&|(])=(find|rg|[efru]?grep)([[:space:]]|\\$)" \\
""", ""),
]
R3_TEXT = R5_TEXT + R3_TEXT
#: r6 (2026-10-28): the edits outside the path-word blocks, as (r6 text, r5 text): the header's r6 sentence, the
#: prefilter's sixth term also after a backtick (decision 6) with its comment, and the xargs rule reading pw_xsh
#: (decision 2). Undone before R5_TEXT.
R6_TEXT = [
    ("""#     =cmd, pipes into a shell, "$@" scripts, eval $(...) and <&0.
#     r6 (2026-10-28) closed those of review r5: substitutions in an
#     unquoted heredoc body, xargs into a shell -c script, any producer's
#     heredoc into a shell, an arithmetic <<, the commit idiom's lone ",
#     =cmd after a backtick, hostile $ # \\ words and dense substitutions,
#     <(...) scripts and loop variables.
""", """#     =cmd, pipes into a shell, "$@" scripts, eval $(...) and <&0.
"""),
    ("""  # r6: so does xargs running a shell whose -c script names a searcher
  # (pw_xsh, from the command prep), a root right before an xargs included.
  xfeed_re=$xargs_feed_re
  [ -z "${pw_xsh-}" ] || xfeed_re="(^|[[:space:]])${root_re}['\\"]?[[:space:]]*\\|[[:space:]]*xargs[[:space:]]"
  if [ -n "${pw_xsh-}" ] || printf '%s' "$stripped" | grep -qE "$xargs_re"; then
    if printf '%s' "$stripped" | grep -qE "$xfeed_re"; then
      hit=1; matched=$(first_match "$stripped" "$xfeed_re" | sed 's/[[:space:]]*$//')
""", """  if printf '%s' "$stripped" | grep -qE "$xargs_re"; then
    if printf '%s' "$stripped" | grep -qE "$xargs_feed_re"; then
      hit=1; matched=$(first_match "$stripped" "$xargs_feed_re" | sed 's/[[:space:]]*$//')
"""),
    ("""# escape, which may spell one; the sixth (r5) zsh's =rg, =find and =grep,
# also right after a backtick (r6).
""", """# escape, which may spell one; the sixth (r5) zsh's =rg, =find and =grep.
"""),
    ("""  || printf '%s' "$command" | grep -qE "(^|[[:space:];&|(\\`])=(find|rg|[efru]?grep)([[:space:]]|\\$)" \\
""", """  || printf '%s' "$command" | grep -qE "(^|[[:space:];&|(])=(find|rg|[efru]?grep)([[:space:]]|\\$)" \\
"""),
]
R3_TEXT = R6_TEXT + R3_TEXT


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
    """Each env spelling alone or with one component, with or without a trailing slash, bare or
    quoted wholly or partly, is a root (a file is not, r3 and r3b); two components down stays
    scoped; other variables, other names and the name as a pattern or in prose stay allowed."""
    searches = ("rg -l foo {}", "find {} -type f", "grep -r x {}", "rg --files {}")
    denied, allowed = [], list(ENV_NEAR_MISSES)
    for i, env in enumerate(ENV_SPELLINGS):
        forms = [(env, True), (env + "/", True), ('"' + env + '"', True), (env + "/jobs", True),
                 # r3b decision 1: normalize drops the slash before the file test, and subfleetd.log
                 # is a file in the fixture (R3B_FLIPPED; r3 judged it by name)
                 (env + "/subfleetd.log/", False), ('"' + env + '/worktrees"', True),
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
    # r3b: a resolvable child is judged by the path-word pass against snapshot-5's roots; a child the
    # shell names only at run time ($CHILD) is the text rules' alone, so it keeps their child form honest.
    for j, spelling in enumerate(HOME_SPELLINGS):
        command = searches[j % len(searches)].format(f"{spelling}/.subfleet/$CHILD")
        expected[command] = f"{spelling.replace('${HOME}', '$HOME')}/.subfleet/$CHILD"
    for command, out in judged(HOOK, list(expected)).items():
        assert rule(out) == "unscoped-search", command
        assert matched_root(out) == expected[command]


TRAILING = ["~/.subfleet/", "$HOME/.subfleet/", "${HOME}/.subfleet/", "/Users/maxghenis/.subfleet/",
            "~/.subfleet/jobs/", "$HOME/.subfleet/worktrees/", "/Users/maxghenis/.subfleet/archive/",
            "~/.subfleet/$CHILD/"]   # r3b: the text rules' alone (see test_each_direct_child_is_a_root)


def test_trailing_slash_spellings_are_denied():
    commands = [search.format(path) for path in TRAILING
                for search in ("rg --files {}", "find {} -name x", "grep -r x {}", 'rg foo "{}"')]
    for command, out in judged(HOOK, commands).items():
        assert rule(out) == "unscoped-search", command


DEEP = ["~/.subfleet/jobs/20261024-115712-x", "~/.subfleet/jobs/20261024-115712-x/",
        "$HOME/.subfleet/worktrees/20261024-115939-guardscope-build",
        "/Users/maxghenis/.subfleet/jobs/20261024-115712-x/a1",
        "~/.subfleet/jobs/20261024-115712-x/a1/deliverable.md", "${HOME}/.subfleet/archive/2026-10/x",
        "~/.subfleet/jobs/$ID", "$HOME/.subfleet/worktrees/$NAME/x"]   # r3b: the text rules' alone


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
    "rg foo ~/.subfleet/jobs/$ID",      # r3b: two levels down, judged by the text rules alone
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
    assert "live hooks/list preflight. 2026-10-25, D-LANE-SCOPE1 r3b: the path words are judged with " \
           "the roots, links and globs of the Claude half's snapshot-5" in trust["provenance"]
    # r3c (the hub's decision 7): the mount sentence says what the code does
    assert "except under /Volumes, /Network, /net or /home: a written path under them is never resolved " \
           "or stat'ed; a link leading into one still is;" in trust["provenance"]
    assert "nothing under /Volumes" not in trust["provenance"]
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
                                              twin_of=case["command"], source_spelling=spelling,
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
    fixed += [(cmd, cwd) for cwd, cmd in R2_OVER_APPROXIMATIONS + R3_RESOLVED + R3C_CLOSED_LIMITS + R3C_STILL_ALLOWED
              + R6_CLOSED_LIMITS]
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
              "r3_intended_loosened": 0, "r3_intended_tightened": 0, "r3_labelled": 0, "r3c_labelled": 0}
    seen = set()

    def r3(key, o, n):
        r3c = r3c_labels(*key)
        labelled = bool(r3_labels(key[0]) or r3c)
        if key not in seen:
            seen.add(key)
            counts["r3_labelled"] += labelled
            counts["r3c_labelled"] += bool(r3c)
            if labelled and o != n:
                counts["r3_intended"] += 1
                counts["r3_intended_loosened" if o else "r3_intended_tightened"] += 1
            # r3c: a command whose only labels deny more never loosens
            if r3c and r3c <= R3C_DENY_ONLY and not r3_labels(key[0]) and o and not n:
                loosened.append(key)
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
    for command, out in judged(hooks["old"], allowed).items():
        assert rule(out) is None, ("old", command)
    # r3c decision 5: a searcher right after a quote is found now, and the script it starts is judged
    # as its own line (decision 6), so the new hook denies all three.
    for command, out in judged(hooks["new"], allowed).items():
        assert rule(out) == "unscoped-search", ("new", command)
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
    ((S_, "env -C ~/.subfleet rg --files"), (S_, "env -C ~ rg --files")),
    ((S_, "rg --files ${SUBFLEET_ROOT:-~/.subfleet}"), (S_, "rg --files ${HOME:-/Users/tester}")),
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
#: r2's known limits r3c closes (the hub's decisions on review r2): ~user (decision 6), a glob directly in
#: a folder of ~/.subfleet (1(c)) and a script run by bash -c (decisions 5 and 6). The old hook allows
#: each; the new one denies each. ``rg -l foo ~/*/src`` (a bare * between two plain names under a folder
#: that is listed) stays allowed by both: its matches are judged, and none is a root.
R3C_CLOSED_LIMITS = [(S_, "rg --files ~tester/.subfleet"), (S_, "rg --files ~tester/PolicyEngine"),
                     (S_, "rg -l foo ~/.subfleet/jobs/*/a1"), (S_, "bash -c 'rg --files $SUBFLEET_ROOT'"),
                     (S_, "bash -c 'rg --files ~'")]
R3C_STILL_ALLOWED = [(S_, "rg -l foo ~/*/src")]
#: r6 decision 9 closed P3-3's quoted loop list (a for loop variable that is find's whole operand stands for its
#: words): denied by the new hook, allowed by the installed one, for the new root and the existing one alike.
R6_CLOSED_LIMITS = [(S_, 'for d in "$SUBFLEET_ROOT"; do find $d -name x; done'), (S_, 'for d in "$HOME"; do find $d -name x; done')]
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
    closed = [(cmd, cwd) for cwd, cmd in R3C_CLOSED_LIMITS + R6_CLOSED_LIMITS]
    new, old = judge_all(hooks["new"], closed), judge_all(hooks["old"], closed)
    for case in closed:
        assert rule(new[case]) == "unscoped-search", case
        assert rule(old[case]) is None, case
    for which in ("old", "new"):
        outs = judge_all(hooks[which], [(cmd, cwd) for cwd, cmd in R3C_STILL_ALLOWED])
        assert all(rule(out) is None for out in outs.values()), which
    # r3 decision 1 flipped P3-2's find -newer case: the r2 hook denied it, the new one allows it.
    resolved = [(cmd, cwd) for cwd, cmd in R3_RESOLVED]
    new, r2 = judge_all(hooks["new"], resolved), judge_all(hooks["r2"], resolved)
    for case in resolved:
        assert rule(new[case]) is None, case
        assert rule(r2[case]) == "unscoped-search", case


# ---------------------------------------------------------------- S5

def same_on_disk(suffix: str) -> bool:
    """Whether <FAKE_HOME>/<suffix> and /Users/maxghenis/<suffix> are the same kind of thing in the
    fixture (a directory, a file, or nothing)."""
    def kind(path: Path) -> str:
        return "dir" if path.is_dir() else "file" if path.exists() else "none"
    return kind(fs_path(FAKE_HOME + "/" + suffix)) == kind(fs_path("/Users/maxghenis/" + suffix))


def test_s5_env_spellings_decide_as_their_home_expansions(hooks, corpus):
    """S5 over the generator: each env-spelled twin gets exactly the decision of its
    ``$HOME/.subfleet`` expansion (``home_ref``: the same draw over ``$HOME/.subfleet``, or
    ``${HOME}/.subfleet`` for a braced twin) and of its source command (the same draw over
    <home>/.subfleet[/…] in the source's home spelling); the twin shares its context, searcher,
    quoting, slash and depth bound. A source spelled /Users/maxghenis names another home than the
    hook's $HOME, so where the two homes differ on disk (r3 and r3b judge files there) it is
    compared with nothing (``other_home_sources``)."""
    twins = [case for case in corpus["generated"] if "twin_of" in case]
    outs = judge_all(hooks["new"], [(c["command"], c["cwd"]) for c in twins]
                     + [(c["home_ref"], c["cwd"]) for c in twins]
                     + [(c["twin_of"], c["cwd"]) for c in twins])
    differ = []
    counts = {"twins": len(twins), "home_refs": len({c["home_ref"] for c in twins}),
              "sources": len({c["twin_of"] for c in twins}), "denied": 0, "allowed": 0,
              "by_spelling": dict.fromkeys(ENV_SPELLINGS, 0), "scale": scale()}
    counts["other_home_sources"] = 0
    for case in twins:
        twin = rule(outs[(case["command"], case["cwd"])])
        ref = rule(outs[(case["home_ref"], case["cwd"])])
        source = rule(outs[(case["twin_of"], case["cwd"])])
        # A source spelled /Users/maxghenis names another home than the hook's $HOME (FAKE_HOME). Since
        # r3 judges paths on disk, it decides as its own path where the two homes differ there
        # (.subfleet/subfleetd.log is a file only under FAKE_HOME), so it is compared where they agree.
        other = case["source_spelling"] not in ("~", "$HOME", "${HOME}", FAKE_HOME) and not same_on_disk(case["suffix"])
        counts["other_home_sources"] += other
        if not (twin == ref and (other or ref == source)):
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


#: r3c's forms (the hub's decisions on review r2), each read from the command text, not from the hook.
R3C_NAME = re.compile(r"""(^|[\s;&|(])(["'\\]|[^\s;&|()<>]*/)(find|rg|grep|egrep|fgrep|rgrep|ugrep)(["']|\s|$)"""
                      r"""|(^|[\s;&|(])(egrep|fgrep|rgrep|ugrep)(\s|$)|\$\((which|command -v)\s|`(which|command -v)\s""")
R3C_NEST = re.compile(r"""(^|[\s;&|(/])((sh|bash|zsh|dash|ksh)(\s+-\S+)*\s+-[A-Za-z]*c[A-Za-z]*|eval)\s+\S*["']""")
R3C_BOUND = re.compile(r"""-max-?depth|(^|\s)-d\s*[0-9]""")
R3C_BARE_CD = re.compile(r"""(^|[\s;&|(])cd\s*(--\s*)?($|[;&|)\n])""")
R3C_WORD = re.compile(r"""[{}\\()|]|\$'|^~[A-Za-z0-9._-]|\*\*\*|(^|/)\.\.(/|$)|\u017f|\u212a|^/volumes/[^/]+/""", re.I)
R3C_DENY_ONLY = {"name", "nest", "bound", "cd", "nopath"}


def r3c_labels(command: str, cwd: str = SCOPED_CWD, home: str = FAKE_HOME) -> set[str]:
    """The r3c forms a command holds (S1's and S1-r3's intended differences): a searcher named by a path,
    a quote, a backslash, $(which ...) or as egrep/fgrep/rgrep/ugrep ("name", decision 5); a sh -c or
    eval script ("nest", decision 6: judged as its own line); a depth bound ("bound": it exempts only its
    own command now); a bare cd ("cd"); a searcher run from a cwd the cwd rule lists ("nopath": a
    searcher given no path walks it); and a path word holding a brace, a parenthesis, a backslash, $'...',
    ~user, ***, .., a folded code point or a /Volumes/<entry>/ prefix ("word", decisions 2-4 and 6).
    Each but "word" can only deny more."""
    labels = set()
    if R3C_NAME.search(command):
        labels.add("name")
    if R3C_NEST.search(command):
        labels.add("nest")
    if R3C_BOUND.search(command):
        labels.add("bound")
    if R3C_BARE_CD.search(command):
        labels.add("cd")
    cwds = {home, home + "/TheAxiomFoundation", home + "/RulesFoundation", home + "/PolicyEngine", "/tmp",
            "/private/tmp", "/", "/Users"}
    if cwd in cwds and re.search(r"(find|rg|grep)", command):
        labels.add("nopath")
    if (any(R3C_WORD.search(word) for word, _ in command_path_words(command))
            or re.search(r"""(^|\s)\{\S*[,.]|(^|[\s'"=])~[A-Za-z0-9._-]""", command)):
        labels.add("word")
    return labels


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
#: r3 allowed ``rg x ~/.subfleet/jobs/2026*`` (jobs/ cannot hold a root by name); r3c's 1(c) makes a glob
#: directly in a folder of ~/.subfleet that folder (R3C_WILD_DENIED pins it), so the r2 hook and this one
#: differ on it now.
R3_CORPUS = (R3_FILE_ALLOWED + [c for c, _ in R3_NAME_DENIED] + R3_GLOB_ALLOWED
             + [c for c, _ in R3_STAR_DENIED] + [c for c, _ in R3_GLOB_DENIED] + ["rg x ~/.subfleet/jobs/2026*"])
#: Of those, the ones whose verdict r3 moves (the r2 hook gives the other verdict).
R3_MOVED = (set(R3_FILE_ALLOWED) | {"grep -rn x ~/.subfleet/*.nomatch", "rg x ~/.subfleet/x*",
                                     "grep -rn x ~/.subfleet/*.json"}
            | {c for c, _ in R3_STAR_DENIED} - {"rg foo ~/.subfleet/*"}
            | {c for c, _ in R3_GLOB_DENIED} - {"rg foo ~/.subfleet/j*", "rg foo ~/.subfleet/[a-z]*"}
            | {"rg x ~/.subfleet/jobs/2026*"})


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
    wrong, counts = [], {"labelled": 0, "moved": 0, "file": 0, "root": 0, "glob": 0, "r3c": 0}
    for key in dict.fromkeys(pairs):
        labels = r3_labels(key[0])
        r3c = r3c_labels(*key)
        n, o = rule(new[key]), rule(r2[key])
        counts["r3c"] += bool(r3c)
        if r3c and not labels:
            # r3c's forms: the deny-only ones never allow more than r2 did
            if r3c <= R3C_DENY_ONLY and o is not None and n is None:
                wrong.append(("r3c allowed more", key, o, n))
            counts["moved"] += n != o
            continue
        labels |= r3c
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
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
    judged the same way. No match: not broad. Since r3c it is ``r3c_real``: decision 1 also makes a
    trailing run of wildcard-only components (after the trailing /, /. and <glob>/.. go) its folder, and a
    glob directly in a folder of a .subfleet that folder; the matches are still the real shell's."""
    return r3c_real(shell, word, home)


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
      - a glob word is broad iff some real match is a broad root (``real_broad`` in bash or zsh,
        r3c); no match, allowed.
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
        # r3c: a glob word is broad when some real match in either shell is (zsh walks **/, bash reads */)
        broad = (plain_broad(case[0], case[3], case[4]) if family == "plain"
                 else real_broad("bash", glob_word(*case)) or real_broad("zsh", glob_word(*case)))
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
    """The differential: every glob case (PROP_GLOBS, and r3's and r3b's brief cases in the tester
    home) is expanded by real /bin/zsh -f and /bin/bash in the fixture, and the hook denies iff a
    real match is broad as a written path (``real_broad``, by REFERENCE_SH), in each shell. Two forms differ between them by
    design, and both shells still agree on the verdict: zsh walks a ``**/`` (bash 3.2 reads it as
    ``*``), which decision 2 judges by the part before it; bash's ``.*`` matches . and .., which
    no case here has."""
    words = [glob_word(*case) for case in PROP_GLOBS]
    commands = [prop_command(word, i) for i, word in enumerate(words)]
    verdicts = judge_prop(HOOK, commands)
    brief = {"~/.subfleet/*.json": False, "~/Pol*/policyengine-us": False, "~/pol*": False, "~/*ubfleet": False,
             "~/.subfleet/*.nomatch": False, "~/.subfleet/x*": False, "~/.subfleet/j*": True, "~/.sub*": True,
             "~/Pol*": True, "~/P?licyEngine": True, "~/[P]olicyEngine": True, "~/Lib*/Cach?s": True,
             "~/Pol*/**/*.py": True, "~/.subfleet/[a-z]*": True,
             # r3b's glob words (the brief's), over the fixture's /, /Users and /Volumes; ~/s*/link-up is
             # left out: a real match is a link to /, which no folder that cannot hold a root lists
             "/U*/tester": True, "/Users/*/PolicyEngine": True, "/Volumes/Ma*": True, "/Volumes/Zz*nosuch": False,
             "/Volumes/*/x*": False, "/U*/nosuch": False, "/*/foo": False, "/*/*.py": False, "/*/*/x": False,
             "/p*/var/f*": True, "/S*/V*": True, "/o*/h*": True}
    brief_out = judge_prop(HOOK, ["rg foo " + w for w in brief], home=FAKE_HOME)
    differ, counts = [], {"cases": len(words) + len(brief), "denied": 0, "zsh_bash_agree": 0}
    for word, command in [*zip(words, commands), *(((w, "rg foo " + w) for w in brief))]:
        home = PROP_HOME if command in verdicts else FAKE_HOME
        out = verdicts.get(command) if home == PROP_HOME else brief_out[command]
        denied = rule(out) == "unscoped-search"
        shells = {shell: real_broad(shell, word, home) for shell in ("bash", "zsh")}
        counts["denied"] += denied
        counts["zsh_bash_agree"] += shells["bash"] == shells["zsh"]
        # r3c: denied exactly when some expansion in either shell names a root (bash reads **/ as */,
        # which r3's oracle read as zsh's walk in both shells)
        if home == FAKE_HOME and any(shells.values()) != brief[word]:
            differ.append(("brief", word, brief[word], shells))
        if denied != any(shells.values()):
            differ.append((command, denied, shells))
    report("shells", counts)
    assert not differ, differ[:10]


# ---------------------------------------------------------------- r3b: snapshot-5's roots, globs and mounts

# Round r3b (2026-10-25, the hub's decisions 1-6, for parity with snapshot-5 of the Claude half,
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
    ("rg -l foo $SUBFLEET_ROOT/subfleetd.log/", None),
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
           "/Users/me/Library/../.subfleet/guard", "/Users/me/.subfleet/leases",
           # r3c decision 2: /Volumes/<entry>/<rest> is /<rest> by name (r3b had this one as no root)
           "/Volumes/Macintosh HD/Users", "/Volumes/Macintosh HD/Users/me/.subfleet", "/Volumes/Backup/tmp",
           "/Volumes/Backup/Users/x/.subfleet/jobs", "/Volumes/a/Volumes/b/Users", "/volumes/X/private/var",
           # r3c decision 6: the code points APFS folds to an ASCII letter here (U+017F, U+212A)
           "/U\u017fers", "/Users/me/.\u017fubfleet", "/Users/me/.subfleet/wor\u212atrees"],
    False: ["/etc", "/private/etc", "/usr/local", "/usr/bin", "/opt/homebrew/Cellar", "/var/folders/zz",
            "/Library/Caches", "/Applications/x.app", "/System/Library", "/System/Volumes/Data/x",
            "/Volumes/Macintosh HD/src", "/Volumes/Backup/Users/me/src", "/Users/me/src", "/Users/me/.subfleet/jobs/x",
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
    assert "$pw_fs" not in code
    for needle in (r"(?<![\w-])readlink\b", r"\[ -[eLdfhrwxs] ", r"(?<![\w-])test -", r"(?<![\w-])ls\b"):
        assert not re.search(needle, code), needle
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


#: r5 decision 7: the data volume's alias, in any letter case, folded before a path is placed.
DATA_ALIAS = re.compile(r"^(/system/volumes/data(?=/|$))+", re.I)


def inside_a_mount(path: str) -> bool:
    """Whether ``path`` (as a disk function gets it) is /Volumes/<entry> or under it, or /Network,
    /net, /home or under them: /Volumes itself is the one stall folder that may be listed. r5: the
    data volume's alias is folded first, so /System/Volumes/Data/Volumes/<entry> is inside a mount."""
    path = DATA_ALIAS.sub("", path) or "/"
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
    # r3c decision 2 (this overlay only; snapshot-5 does not): /Volumes/<entry>/<rest> is judged as /<rest>.
    while re.match(r"/volumes/[^/]+/.", q):
        q = "/" + q.split("/", 3)[3]
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
    ("rg foo ~/[!P]olicyEngine", False), ("rg foo ~/.subfleet/jobs/2026*", True), ("rg foo ~/.sub*/nosuch", False),
    ("rg foo ~/[.]subfleet/nosuch/..", False), ("rg foo ~/L*/Caches/nosuch", False),
    ("rg foo ~/[[:digit:]]olicyEngine", False), ("rg foo ~/[[:nosuch:]]olicyEngine", False), ("rg foo ~/Pol[", False),
    # r3c 1(c): a glob directly in a folder of ~/.subfleet is that folder (snapshot-5 lists nothing there)
    ("rg foo src/*.py", False), ("rg foo ~/src/link-u*", False), ("rg foo ~/.subfleet/jobs/link-u*", True),
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
ME=$(/usr/bin/id -un)
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
# r3c: the two code points APFS folds to an ASCII letter on this volume
fold() { local f=$1; f=${f//$'\xc5\xbf'/s}; R=${f//$'\xe2\x84\xaa'/k}; }
normalize() {
  local p a l
  lexnorm "$1"; fold "$R"; p=$R
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
  local l sub n p=$1
  lc "$p"; l=$R
  # r3c decision 2: /Volumes/<entry>/<rest> is judged by name as /<rest>
  while [ "${l#/volumes/*/}" != "$l" ] && [ -n "${l#/volumes/*/}" ]; do
    p="/${p#/*/*/}"; normalize "$p"; p=$R; lc "$p"; l=$R
  done
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
inmount() { lc "$1"; [ "$R" != /volumes ] && stall "$1"; }
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
# r3c decision 4: each .. applied after the links before it are resolved
physdots() {
  local IFS=/ part path=""
  set -f; local -a parts=( $1 ); set +f
  for part in ${parts[@]+"${parts[@]}"}; do
    case "$part" in ''|.) ;; ..) physical "${path:-/}"; path=${R%/*} ;; *) path="$path/$part" ;; esac
  done
  R=${path:-/}
}
broad() {
  local r
  if isbroad "$1"; then if isfile "$1"; then return 1; fi; return 0; fi
  physical "$1"; r=$R
  [ "$r" != "$1" ] && isbroad "$r" && ! isfile "$r"
}
NAMES="TheAxiomFoundation RulesFoundation PolicyEngine Library Caches .cache .local .subfleet Users Volumes System private tmp var folders opt homebrew Applications usr Data any-name-at-all"
holds() {
  local n
  if inmount "$1"; then return 1; fi
  for n in $NAMES; do normalize "$1/$n"; if isbroad "$R"; then return 0; fi; done; return 1
}
# r3c 1(c): a broad root directly in a .subfleet
subchild() { local d=${1%/*}; lc "${d##*/}"; [ "$R" = .subfleet ] && isbroad "$1"; }
abs_of() {
  local n=""
  case "$1" in
    '~'|'$HOME') R=$HOME_L ;;
    '~/'*) R="$HOME_L/${1#\~/}" ;;
    '~'*) n=${1#\~}; n=${n%%/*}
      if [ "$n" = "$ME" ]; then R="$HOME_L${1#\~$n}"; else R="/Users/$n${1#\~$n}"; fi ;;
    '$HOME/'*) R="$HOME_L/${1#\$HOME/}" ;;
    '$SUBFLEET_ROOT'|'$SUBFLEET_HOME') R=$HOME_L/.subfleet ;;
    '$SUBFLEET_ROOT/'*|'$SUBFLEET_HOME/'*) R="$HOME_L/.subfleet/${1#*/}" ;;
    *) R=$1 ;;
  esac
}
BUDGET=0
globbroad() {
  local IFS=/ c="" first=-1 k=0 n=0 prefix="" text="" isglob="" p="" q="" name="" kind="" i=0 last="" lastup=-1
  local -a cs cand kinds next nkinds
  set -f; local -a raw=( $1 ); set +f
  IFS=$' \t\n'
  for c in ${raw[@]+"${raw[@]}"}; do [ -z "$c" ] || { cs[$n]=$c; [ "$c" != .. ] || lastup=$n; n=$((n + 1)); }; done
  while [ "$k" -lt "$n" ]; do
    case "${cs[$k]}" in *[*?[]*) first=$k; break ;; esac
    prefix="$prefix/${cs[$k]}"; k=$((k + 1))
  done
  [ "$first" -ge 0 ] || return 1
  physdots "${prefix:-/}"; normalize "$R"; prefix=$R
  isbroad "$prefix" || return 1
  cand=( "$prefix" ); kinds=( plain )
  k=$first
  while [ "$k" -lt "$n" ]; do
    text=${cs[$k]//$'\a'/\/}; isglob=""; last=""
    case "$text" in *[*?[]*) isglob=1 ;; esac
    [ "$k" -ne $((n - 1)) ] || last=1
    next=(); nkinds=()
    for p in "${cand[@]}"; do
      if [ -n "$isglob" ] && subchild "$p" && ! isfile "$p"; then return 0; fi
      if inmount "$p"; then
        if [ -z "$isglob" ]; then normalize "$p/$text"; next[${#next[@]}]=$R; nkinds[${#nkinds[@]}]=plain
        else
          for name in $NAMES; do
            if [ "$name" != any-name-at-all ]; then
              case "$name" in .*) case "$text" in .*) ;; *) continue ;; esac ;; esac
              [[ $name == $text ]] || continue
            fi
            normalize "$p/$name"; next[${#next[@]}]=$R; nkinds[${#nkinds[@]}]=plain
          done
        fi
      elif [ -z "$isglob" ] && ! stall "$p"; then
        if [ "$text" = .. ]; then physical "$p"; q=${R%/*}; normalize "${q:-/}"; else normalize "$p/$text"; fi
        q=$R
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
      if [ -n "$last" ] || { isbroad "${next[$i]}" && ! isfile "${next[$i]}"; } \
         || { [ "$k" -lt "$lastup" ] && ! isfile "${next[$i]}"; }; then
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
# r3c 1(b): a component made only of *, ? and bracket expressions
WILD_RE='^(\*|\?|\[[!^]?\]?([^]]|\[:[a-z]+:\])*\])+$'
BRACE_RE='^(.*[^$])?[{]([^{}]*,[^{}]*)[}](.*)$'
ALT_RE='^([^(]*)[(]([^()]*)[)](.*)$'
PLAIN_RE='^[A-Za-z0-9._@%+=:,|-]*$'
# r3c decision 3: brace words (comma groups, 64 at most; a nested group, {x..y} or more reads each as *)
# and zsh alternation (plain names, else the component as *); the words go to ALTS, STARRED is set
# when a group was read as *.
braces() {
  local w=$1 todo=() x="" i=0 j=0 d=0 c="" open=-1 cnt=1 bad="" groups=0 out="" t=""
  i=0; d=0
  while [ "$i" -lt "${#w}" ]; do
    c=${w:i:1}
    if [ "$c" = "{" ] && { [ "$i" -eq 0 ] || [ "${w:i-1:1}" != '$' ]; }; then
      if [ "$d" -eq 0 ]; then open=$i; t=""; else bad=1; fi
      d=$((d + 1))
    elif [ "$c" = "}" ] && [ "$d" -gt 0 ]; then
      d=$((d - 1))
      if [ "$d" -eq 0 ]; then
        t=${w:open+1:i-open-1}
        case "$t" in *[{]*) ;; *,*) c=${t//[!,]/}; cnt=$((cnt * (${#c} + 1))); groups=$((groups + 1)) ;; *..*) bad=1 ;; esac
      fi
    fi
    i=$((i + 1))
  done
  [ "$cnt" -le 64 ] || bad=1
  STARRED=""
  if [ -n "$bad" ]; then
    out=""; i=0; d=0
    while [ "$i" -lt "${#w}" ]; do
      c=${w:i:1}
      if [ "$c" = "{" ] && { [ "$i" -eq 0 ] || [ "${w:i-1:1}" != '$' ]; }; then
        [ "$d" -ne 0 ] || open=${#out}
        d=$((d + 1)); out="$out$c"
      elif [ "$c" = "}" ] && [ "$d" -gt 0 ]; then
        d=$((d - 1)); out="$out$c"
        if [ "$d" -eq 0 ]; then
          t=${out:open}
          case "$t" in *,*|*..*) out="${out:0:open}*" ;; esac
        fi
      else out="$out$c"
      fi
      i=$((i + 1))
    done
    ALTS=( "$out" ); STARRED=1; return 0
  fi
  ALTS=(); todo=( "$w" )
  while [ "${#todo[@]}" -gt 0 ]; do
    x=${todo[${#todo[@]}-1]}; unset "todo[${#todo[@]}-1]"; todo=( ${todo[@]+"${todo[@]}"} )
    if [[ $x =~ $BRACE_RE ]]; then
      local pre=${BASH_REMATCH[1]} mid="${BASH_REMATCH[2]}," post=${BASH_REMATCH[3]}
      while [ -n "$mid" ]; do todo[${#todo[@]}]="$pre${mid%%,*}$post"; mid=${mid#*,}; done
    else
      ALTS[${#ALTS[@]}]=$x
    fi
  done
}
alternation() {
  local x="" todo=() out=() pre="" mid="" post="" a=""
  todo=( "${ALTS[@]}" )
  while [ "${#todo[@]}" -gt 0 ]; do
    x=${todo[0]}; todo=( ${todo[@]+"${todo[@]:1}"} )
    if [[ $x =~ $ALT_RE ]]; then
      pre=${BASH_REMATCH[1]}; mid=${BASH_REMATCH[2]}; post=${BASH_REMATCH[3]}
      if [[ $mid == *'|'* ]] && [[ $mid =~ $PLAIN_RE ]] && [ $(( ${#todo[@]} + ${#out[@]} )) -lt 60 ]; then
        mid="$mid|"
        while [ -n "$mid" ]; do todo[${#todo[@]}]="$pre${mid%%|*}$post"; mid=${mid#*|}; done
      else
        case "$pre" in */*) pre="${pre%/*}/" ;; *) pre="" ;; esac
        case "$post" in */*) post="/${post#*/}" ;; *) post="" ;; esac
        todo[${#todo[@]}]="$pre*$post"; STARRED=1
      fi
    else
      out[${#out[@]}]=$x
    fi
  done
  ALTS=( "${out[@]}" )
}
# r4 decision 9: a written path (absolute) is broad when its physical reading is, or, climbing out of a
# /Volumes entry with .., when the rest read from / is
written() {
  local p=$1 l="" rest=""
  physdots "$p"; normalize "$R"; broad "$R" && return 0
  lc "$p"; l=$R
  case "$l" in
    /volumes/*/*) rest=${p#/*/*/}
      case "/$rest/" in */../*) normalize "/$rest"; broad "$R" && return 0 ;; esac ;;
  esac
  return 1
}
# r4 decision 1: the word with each <component>/.. after its first glob component folded away by name
dotfold() {
  local IFS=/ part="" g="" pre="" n=0 i=0 out=""
  local -a st
  set -f; local -a parts=( $1 ); set +f
  for part in ${parts[@]+"${parts[@]}"}; do
    if [ -z "$g" ]; then
      case "$part" in *[*?[]*) g=1; st[0]=$part; n=1 ;; *) pre="$pre$part/" ;; esac
      continue
    fi
    case "$part" in
      ''|.) ;;
      ..) if [ "$n" -gt 0 ] && [ "${st[$((n - 1))]}" != .. ]; then n=$((n - 1)); else st[$n]=..; n=$((n + 1)); fi ;;
      *) st[$n]=$part; n=$((n + 1)) ;;
    esac
  done
  out=$pre
  while [ "$i" -lt "$n" ]; do out="$out${st[$i]}/"; i=$((i + 1)); done
  out=${out%/}
  R=${out:-/}
}
# r5 decision 6: every reading of a word in which each component bash 3.2 matches against .. (a literal leading
# dot, then a pattern [[ .. == c ]] takes) is itself, . or .., into MIXES
dotdot_mixes() {
  local w=$1 lead="" tail="" part="" r="" i=0
  local IFS=/
  local -a parts acc nacc
  case "$w" in /*) lead=/; w=${w#/} ;; esac
  case "$w" in */) tail=/; w=${w%/} ;; esac
  set -f; parts=( $w ); set +f
  acc=( "$lead" )
  for part in ${parts[@]+"${parts[@]}"}; do
    nacc=()
    for r in "${acc[@]}"; do
      [ "$i" -eq 0 ] || r="$r/"
      nacc[${#nacc[@]}]="$r$part"
      if [[ $part == .* && $part == *[*?[]* && .. == $part ]]; then nacc[${#nacc[@]}]="$r."; nacc[${#nacc[@]}]="$r.."; fi
    done
    acc=( "${nacc[@]}" ); i=$((i + 1))
  done
  MIXES=()
  for r in "${acc[@]}"; do MIXES[${#MIXES[@]}]="$r$tail"; done
}
judge_alt() {
  local w=$1 b="" f="" m="" l="" rest=""
  case "$w" in
    *[*?[]*) ;;
    *) abs_of "$w"; case "$R" in *'$'*) return 1 ;; esac
       written "$R"; return ;;
  esac
  b="/$w/"
  while [ "${b//\/\*\*\*\//}" != "$b" ]; do b=${b//\/\*\*\*\//\/**\/}; done
  b=${b#/}; b=${b%/}
  b=${b//\[\^\/\]/[^$'\a']}; b=${b//\[\!\/\]/[!$'\a']}
  judge_glob "$b" && return 0
  dotfold "$b"; f=$R
  [ "$f" != "$b" ] && judge_glob "$f" && return 0
  # r5 decision 10: a glob word that climbs out of a /Volumes entry with .. is read from / as well
  abs_of "$b"
  case "$R" in
    /*) l=$R; lc "$l"
      case "$R" in /volumes/*/*) rest=${l#/*/*/}; case "/$rest/" in */../*) normalize "/$rest"; judge_glob "$R" && return 0 ;; esac ;; esac ;;
  esac
  # r5 decision 6: a component bash matches against .. reads as its folder and its parent too
  dotdot_mixes "$b"
  for m in "${MIXES[@]}"; do
    [ "$m" != "$b" ] || continue
    judge_glob "$m" && return 0
    dotfold "$m"; [ "$R" != "$m" ] && judge_glob "$R" && return 0
  done
  return 1
}
judge_glob() {
  local b=$1 walk="" c="" x=""
  case "/$b" in */\*\*/*) b=${b%%/\*\*/*}; walk=1 ;; esac
  # r4 decision 2: the trailing run is read before a **/ too
  if :; then
    while :; do
      c=${b##*/}
      case "$b" in
        /|'') break ;;
        */) b=${b%/} ;;
        */.) b=${b%/.} ;;
        */..) x=${b%/..}; c=${x##*/}; case "$c" in *[*?[]*) b=${x%/*}; walk=1 ;; *) break ;; esac ;;
        */*) if [ "$c" = '.*' ] || [ "$c" = '**' ] || [[ $c =~ $WILD_RE ]]; then b=${b%/*}; walk=1; else break; fi ;;
        *) break ;;
      esac
      [ -n "$b" ] || b=/
    done
  fi
  [ -n "$b" ] || b=/
  case "$b" in
    *[*?[]*) abs_of "$b"; case "$R" in *'$'*) return 1 ;; esac; globbroad "$R" ;;
    *) abs_of "$b"; case "$R" in *'$'*) return 1 ;; esac; written "$R" ;;
  esac
}
judge() {
  local w=$1 a="" e=""
  BUDGET=$CAP
  # r3c decision 6: an unquoted backslash escapes the next character
  while [[ $w == *'\'* ]]; do e="$e${w%%\\*}"; w=${w#*\\}; e="$e${w:0:1}"; w=${w:1}; done
  w="$e$w"
  braces "$w"
  alternation
  for a in "${ALTS[@]}"; do
    BUDGET=$CAP
    if judge_alt "$a"; then return 0; fi
  done
  return 1
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


# ---------------------------------------------------------------- r3c: review r2's walk-arounds closed

# Round r3c (2026-10-26, the hub's decisions 1-8 on review r2 at 674abdac7):
#   1. a glob that matches every entry is the folder it sits in: (a) trailing /, /. and <glob>/.. are
#      normalized first; (b) a last component made only of wildcards (?, *, brackets) under a broad root
#      is that root; (c) any glob directly in a folder of ~/.subfleet (jobs, worktrees, ...) is that folder;
#   2. /Volumes/<entry>/<rest> is judged by name as /<rest>, never stat'ed or resolved;
#   3. comma braces are expanded (64 at most) and zsh's (a|b) judged by its alternatives; a nested group,
#      a {x..y} sequence or more than 64 reads each group as *;
#   4. a searcher's .. after a link is resolved physically, as find and rg resolve it (cd's stays logical);
#   5. searchers are found by basename (a path, a backslash or a quote before the name, egrep, fgrep, rgrep,
#      ugrep, $(which X) and `which X`);
#   6. backslash escapes, ***, a / inside brackets, APFS's folded code points, $'...', ~user, a bare cd and a
#      searcher with no path, nesting to depth 4, and a depth bound only for its own command;
#   7-8. the sentences, the message's root list, TRUST and the install steps.
#: r3c's fixture entries: links whose .. leads into ~/.subfleet (decision 4), and a link standing in for
#: /Volumes/Macintosh HD, which is a link to / on this host (decision 2's differential).
def nest(command: str, depth: int, shell: str = "sh") -> str:
    """``command`` run through ``depth`` levels of ``shell -c``, each quoting the next."""
    for _ in range(depth):
        command = f"{shell} -c {shlex.quote(command)}"
    return command


R3C_LAYOUT = [
    ("Users/tester/work/jl", "link", f"../.subfleet/jobs/{JOB}"),
    ("Users/tester/work/repo/wt", "link", "../../.subfleet/worktrees/20261024-115939-guardscope-build"),
    ("Volumes/Boot", "link", ".."),
]
FS_LAYOUT += R3C_LAYOUT
FS_DIGEST = hashlib.sha256(json.dumps(FS_LAYOUT).encode()).hexdigest()
LANE_W = FAKE_HOME + "/.subfleet/worktrees/20261024-115939-guardscope-build"
MAC = "/Volumes/Macintosh HD"

#: Review r2's P2-1: globs that match every entry, and the trailing //, /./, /. and /.. forms
#: (decision 1). (command, the root the message names).
R3C_WILD_DENIED = [
    ("rg --files ~/.subfleet/jobs/?*", "~/.subfleet/jobs/?* (glob)"),
    ("rg --files ~/.subfleet/jobs/*?", "~/.subfleet/jobs/*? (glob)"),
    ("rg --files ~/.subfleet/jobs/[!.]*", "~/.subfleet/jobs/[!.]* (glob)"),
    ("rg --files ~/.subfleet/jobs/[0-9]*", "~/.subfleet/jobs/[0-9]* (glob)"),
    ("find ~/.subfleet/jobs/?* -name AGENTS.md", "~/.subfleet/jobs/?* (glob)"),
    ("rg --files ~/.subfleet/worktrees/?*", "~/.subfleet/worktrees/?* (glob)"),
    ("rg --files $SUBFLEET_ROOT/jobs/?*", "$SUBFLEET_ROOT/jobs/?* (glob)"),
    ("rg --files ~/PolicyEngine/?*", "~/PolicyEngine/?* (glob)"),
    ("rg --files /tmp/?*", "/tmp/?* (glob)"),
    ("rg --files /usr/?*", "/usr/?* (glob)"),
    ("rg --files ~/.subfleet/jobs/*//", "~/.subfleet/jobs/*// (glob)"),
    ("rg --files ~/.subfleet/jobs/*/./", "~/.subfleet/jobs/*/./ (glob)"),
    ("rg --files ~/.subfleet/jobs/*/.", "~/.subfleet/jobs/*/. (glob)"),
    ("rg --files ~/.subfleet/jobs/*/..", "~/.subfleet/jobs/*/.. (glob)"),
    ("grep -r x ~/.subfleet/jobs/*/..", "~/.subfleet/jobs/*/.. (glob)"),
    ("rg --files ~/PolicyEngine/*/.", "~/PolicyEngine/*/. (glob)"),
    # 1(c): a glob directly in a folder of ~/.subfleet is that folder (reverses r3's jobs/2026*)
    ("rg x ~/.subfleet/jobs/2026*", "~/.subfleet/jobs/2026* (glob)"),
    ("rg -l foo ~/.subfleet/jobs/*/a1", "~/.subfleet/jobs/*/a1 (glob)"),
    ("rg foo ~/.subfleet/jobs/link-u*", "~/.subfleet/jobs/link-u* (glob)"),
    # decision 6: a / inside a bracket expression does not split components (zsh)
    ("rg --files /[^/]*", "/[^/]* (glob)"),
    ("find /[!/]* -name x", "/[!/]* (glob)"),
    ("rg --files ~/[!/]*", "~/[!/]* (glob)"),
    ("rg --files ~/.subfleet/[^/]*", "~/.subfleet/[^/]* (glob)"),
]
#: Review r2's P2-2: the boot volume's alias, and an external disk's Users (decision 2).
R3C_VOLUMES_DENIED = [
    (f'rg --files -g AGENTS.md "{MAC}/Users/tester/.subfleet"', f"{MAC}/Users/tester/.subfleet"),
    (f'find "{MAC}/Users" -name AGENTS.md', f"{MAC}/Users"),
    (f'rg --files "{MAC}/Users/tester"', f"{MAC}/Users/tester"),
    (f'rg --files "{MAC}/tmp"', f"{MAC}/tmp"),
    (f'rg --files "{MAC}/private/tmp"', f"{MAC}/private/tmp"),
    (f'rg --files "{MAC}/Users/"*/.subfleet', f"{MAC}/Users/*/.subfleet (glob)"),
    (f'rg --files "{MAC}/Users/tester/.subfleet/jobs/"2026*', f"{MAC}/Users/tester/.subfleet/jobs/2026* (glob)"),
    ("rg --files /Volumes/Ext/Users", "/Volumes/Ext/Users"),
    ("find /Volumes/Ext/Users/someone/PolicyEngine -name x", "/Volumes/Ext/Users/someone/PolicyEngine"),
    (f'rg --files "{MAC}/"U*', f"{MAC}/U* (glob)"),
    (f'rg --files "{MAC}/Users/"t*', f"{MAC}/Users/t* (glob)"),
]
#: Review r2's P2-3: braces and zsh's alternation (decision 3).
R3C_BRACES_DENIED = [
    ("find ~/{PolicyEngine,TheAxiomFoundation} -name x", "~/{PolicyEngine,TheAxiomFoundation}"),
    ("rg -l foo ~/{PolicyEngine,TheAxiomFoundation}", "~/{PolicyEngine,TheAxiomFoundation}"),
    ("rg -l foo /Users/tester/{PolicyEngine,.subfleet}", "/Users/tester/{PolicyEngine,.subfleet}"),
    ("rg -l foo ~/.{subfleet,cache}", "~/.{subfleet,cache}"),
    ("rg --files ~/.sub{fleet,x}", "~/.sub{fleet,x}"),
    ("rg --files /U{sers,x}", "/U{sers,x}"),
    ("rg --files /{tmp,x}", "/{tmp,x}"),
    ("rg --files ~/(PolicyEngine|TheAxiomFoundation)", "~/(PolicyEngine|TheAxiomFoundation)"),
    ("find ~/(.subfleet|x) -name AGENTS.md", "~/(.subfleet|x)"),
    # other groups read as *: nested, a sequence, more than 64 results, an alternation that is no plain name
    ("rg --files ~/{PolicyEngine,{a,b}}", "~/{PolicyEngine,{a,b}} (glob)"),
    ("rg --files ~/{a..z}*", "~/{a..z}* (glob)"),
    ("rg --files ~/{a,b,c,d,e,f,g,h,i}{1,2,3,4,5,6,7,8}", "~/{a,b,c,d,e,f,g,h,i}{1,2,3,4,5,6,7,8} (glob)"),
    ("rg --files ~/(Pol*|x)", "~/(Pol*|x) (glob)"),
    ("rg --files {~,/tmp/x}/.subfleet", "{~,/tmp/x}/.subfleet"),
    ("rg --files ~/.subfleet/{jobs,src}", "~/.subfleet/{jobs,src}"),
]
#: Review r2's P3-1: a link followed by .. (decision 4), from the fixture's ~/work.
R3C_DOTS_DENIED = [
    ("rg --files ~/work/jl/..", "~/work/jl/.."),
    ("rg --files ~/work/repo/wt/..", "~/work/repo/wt/.."),
    ("find /Users/tester/work/jl/.. -name x", "/Users/tester/work/jl/.."),
]
#: Review r2's P3-2: searchers the detection missed (decision 5).
R3C_SEARCHERS_DENIED = [
    "/usr/bin/find / -name x", "/usr/bin/grep -r x ~", "/opt/homebrew/bin/rg --files ~/.subfleet",
    "\\rg --files ~/.subfleet", '"rg" --files ~/.subfleet', "'rg' --files ~/.subfleet",
    "$(which rg) --files ~/.subfleet", "$(command -v rg) --files ~/.subfleet", "`which rg` --files ~/.subfleet",
    "egrep -r x ~/.subfleet", "fgrep -r x ~/.subfleet", "rgrep x ~/.subfleet", "ugrep -r x ~/.subfleet",
    '"/usr/bin/find" ~/.subfleet -name x', "env /usr/bin/rg --files ~/.subfleet",
    # a searcher right after a quote (r2's known limit, now detected)
    "bash -c 'rg --files ~/.subfleet'", "bash -c 'find / -name x'", "eval 'grep -r x ~/.subfleet/jobs'",
]
#: Review r2's P3-3: the other escapes (decision 6). (command, cwd).
R3C_FORMS_DENIED = [
    ("rg --files /Users/tester/.sub\\fleet", S_), ("rg --files ~/.s\\ubfleet/jobs", S_), ("rg --files /\\Users", S_),
    ('rg --files "$HOME/.subfleet"\\/jobs', S_), ("rg --files ~/***/x", S_), ("rg --files ~/.subfleet/***/x", S_),
    ("rg --files ~/.ſubfleet", S_), ("rg --files /Uſers", S_), ("rg --files ~/.subfleet/worKtrees", S_),
    ("rg --files ~/$'.subfleet'", S_), ("rg --files ~tester/.subfleet", S_), ("rg --files ~tester/PolicyEngine", S_),
    ("cd && rg --files", S_), ("cd; rg --files .", S_), ("cd -- && find . -name x", S_),
    ("rg foo", FAKE_HOME), ("grep -r foo", FAKE_HOME), ("rg --files | head", FAKE_HOME), ("find -name x", "/"),
    ("ps | grep -r foo", FAKE_HOME),
    ("bash -c 'bash -c \"true; rg --files ~/.subfleet\"'", S_),
    (nest("true; rg --files /tmp", 4), S_),
    ("rg --files ~/.subfleet # --max-depth", S_),
    ("git commit -m 'drop --max-depth' && rg --files ~/.subfleet", S_),
    ("rg --max-depth 1 foo ~ && rg --files ~/.subfleet", S_),
]
#: Five levels of sh -c, each quoting the next: a searcher deeper than 4 is denied, even a scoped one.
R3C_DEEP = nest("true; rg --files src", 5)
#: Review r2's check 3 (25 scoped searches inside a lane worktree and scratch, and roots in pattern
#: position) and the brief's must-stay-allowed cases: (command, cwd).
R3C_ALLOWED = [
    ("rg foo .", LANE_W), ("rg foo src", LANE_W), ("rg foo", LANE_W), ("rg --files", LANE_W),
    ("rg foo " + LANE_W, S_), ('rg foo "' + LANE_W + '"', S_), ("rg foo ~/.subfleet/worktrees/20261024-115939-guardscope-build", S_),
    ("rg foo $HOME/.subfleet/worktrees/20261024-115939-guardscope-build/", S_),
    ("rg foo $SUBFLEET_ROOT/worktrees/20261024-115939-guardscope-build", S_),
    ("rg foo " + LANE_W + "/*", S_), ("grep -r foo " + LANE_W + "/**/*.py", S_), ("cd " + LANE_W + " && rg foo", S_),
    ("rg foo /var/folders/9l/x/T/sf-lane", S_), ("rg foo /private/var/folders/9l/x/T/sf-lane", S_),
    ("rg foo /tmp/lane", S_), ("rg foo /tmp/sf-*", S_), ("rg foo $(getconf DARWIN_USER_TEMP_DIR)sf-*", S_),
    ('rg foo "$(getconf DARWIN_USER_TEMP_DIR)"sf-*', S_),
    (f"rg foo ~/.subfleet/jobs/{JOB}/a1/out.md", S_), (f"rg foo ~/.subfleet/jobs/{JOB}/a1/*", S_),
    (f"grep -rn x ~/.subfleet/jobs/{JOB}/a1", S_), ("rg -n ERROR ~/.subfleet/daemon.log", S_),
    ("rg '/usr' src", LANE_W), ("rg -g '/Library/*' foo src", LANE_W), ("rg -e '/usr' src", LANE_W),
    # decision 3: braces and words that are no path words, in a lane worktree
    ("rg foo src/{a,b}.py", LANE_W), ("rg foo {src,tests}", LANE_W),
    # decision 4: cd's .. stays logical; decision 2: a scoped path under a mount entry
    ("cd ~/work/jl/.. && rg foo", S_), (f'rg foo "{MAC}/Users/tester/work/repo"', S_), ("rg foo /Volumes/Ext/src/x", S_),
    # decision 6: in double quotes only \$, \`, \", \\ and a backslash-newline are escapes: this names .sub\fleet
    ('rg --files "$HOME/.sub\\fleet"', S_),
    # decision 5: a searcher named through a variable stays a known limit
    ("RG=rg; $RG --files ~/.subfleet", S_),
    # decision 6: rg reading a pipe or a file, and a no-path search outside a root
    ("ps aux | rg foo", FAKE_HOME), ("rg foo < log.txt", FAKE_HOME), ("rg foo", S_), ("grep -r foo", S_),
    # the depth bound in the searcher's own command still exempts it, nested too
    ("rg --max-depth 1 foo ~/.subfleet", S_), ("bash -c 'find ~ -maxdepth 1 -name x'", S_),
    ("cd ~ && rg --max-depth 1 foo", S_), ("echo ~ | xargs rg --max-depth 1 foo", S_),
    (nest("rg --files src", 4), S_), (nest("rg --files src", 3, "bash"), S_),
    # decision 1: a glob below a job, and words that list nothing
    ("rg x /*/foo", S_), ("rg x /*/*.py", S_), ("rg foo /Volumes/*/x*", S_), ("bash -c 'rg foo /**/*.py'", S_),
]


def test_r3c_globs_that_match_every_entry_are_their_folder(hooks):
    """Decision 1 on review r2's P2-1 list: wildcard-only last components, the trailing //, /./, /.
    and <glob>/.. forms, a glob directly in a folder of ~/.subfleet, and a / inside brackets."""
    for command, root in R3C_WILD_DENIED:
        out = run_hook(hooks["new"], command)
        assert rule(out) == "unscoped-search", command
        assert matched_root(out) == root, (command, matched_root(out))


def test_r3c_volumes_entries_are_judged_as_the_rest_of_the_path(hooks):
    """Decision 2 on review r2's P2-2 list: /Volumes/<entry>/<rest> is judged by name as /<rest>."""
    for command, root in R3C_VOLUMES_DENIED:
        out = run_hook(hooks["new"], command)
        assert rule(out) == "unscoped-search", command
        assert matched_root(out) == root, (command, matched_root(out))


def test_r3c_braces_and_alternation_are_judged_by_their_alternatives(hooks):
    """Decision 3 on review r2's P2-3 list (the test file's own Claude example included)."""
    for command, root in R3C_BRACES_DENIED:
        out = run_hook(hooks["new"], command, "/tmp/work")
        assert rule(out) == "unscoped-search", command
        assert matched_root(out) == root, (command, matched_root(out))


def test_r3c_dotdot_after_a_link_is_physical(hooks):
    """Decision 4 on review r2's P3-1: jl leads to a job folder and wt to a worktree, so .. is jobs and
    worktrees, as find and rg resolve it."""
    assert fs_path(FAKE_HOME + "/work/jl").is_symlink() and fs_path(FAKE_HOME + "/work/repo/wt").is_symlink()
    for command, root in R3C_DOTS_DENIED:
        out = run_hook(hooks["new"], command)
        assert rule(out) == "unscoped-search", command
        assert matched_root(out) == root, (command, matched_root(out))


def test_r3c_searchers_are_found_by_basename(hooks):
    """Decision 5 on review r2's P3-2 list, and a searcher right after a quote."""
    for command in R3C_SEARCHERS_DENIED:
        assert rule(run_hook(hooks["new"], command)) == "unscoped-search", command


def test_r3c_the_other_escapes_are_closed(hooks):
    """Decision 6 on review r2's P3-3 list: backslashes, ***, the folded code points, $'...', ~user, a
    bare cd and a searcher with no path, double nesting, depth past 4, and a depth bound elsewhere."""
    for command, cwd in R3C_FORMS_DENIED + [(R3C_DEEP, S_)]:
        assert rule(run_hook(hooks["new"], command, cwd)) == "unscoped-search", (command, cwd)


def test_r3c_scoped_searches_stay_allowed(hooks):
    """Review r2's check 3 and the brief's must-stay-allowed cases."""
    for command, cwd in R3C_ALLOWED:
        assert rule(run_hook(hooks["new"], command, cwd)) is None, (command, cwd)


# ---------------------------------------------------------------- r3c: the property, the shells, the mounts

R3C_PROPERTY_SEED = 20261027
R3C_PROPERTY_CASES = 2400
#: r3c's additions to the generator: components made only of wildcards (with a / inside brackets and
#: ***), the trailing //, /., /./, /.. and <glob>/.. tails, the /Volumes/Macintosh HD prefix (and
#: /Volumes/Boot, the fixture's link to /, standing in for it on this host), comma braces, zsh
#: alternation, ~user, backslashes and the folded code points.
R3C_WILD = ["?*", "*?", "[!.]*", "[0-9]*", "?", "[a-z]*", "*", "**", "***", ".*", "[^/]*", "[!/]*", "[[:alpha:]]*"]
R3C_TAILS = ["", "", "", "", "/", "//", "/.", "/./", "/..", "/*/..", "/*//", "/*/./", "/?*"]
R3C_NAMES = [n for n in PROP_NAMES if " " not in n]
R3C_PREFIXES = PROP_PREFIXES + ["/Volumes/Boot", "/Volumes/Boot/Users/prop", "/Volumes/Macintosh HD/Users",
                                "/Volumes/Macintosh HD", "/Volumes/Ext/Users/x", "~prop", "~other", "/VOLUMES/Boot/private"]


def r3c_component(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.05:
        return rng.choice([".", "..", ""])
    if roll < 0.17:
        return rng.choice(R3C_WILD)
    if roll < 0.25:
        a, b = rng.sample(R3C_NAMES, 2)
        return "{%s,%s}" % (a, b)
    if roll < 0.28:
        return rng.choice(["{a..c}*", "{%s,{x,y}}" % rng.choice(R3C_NAMES), "{,%s}" % rng.choice(R3C_NAMES)])
    if roll < 0.35:
        a, b = rng.sample(R3C_NAMES, 2)
        return "(%s|%s)" % (a, b)
    if roll < 0.37:
        return "(%s*|zz)" % rng.choice(R3C_NAMES)[:2]
    name = rng.choice(PROP_NAMES)
    if rng.random() < 0.15:
        name = "".join(ch.swapcase() if rng.random() < 0.5 else ch for ch in name)
    if rng.random() < 0.25:
        name = glob_component(rng, name)
    if rng.random() < 0.1 and ("s" in name or "k" in name):
        name = name.replace("s", "ſ", 1) if "s" in name else name.replace("k", "K", 1)
    return name


def r3c_property_words(rng: random.Random, n: int) -> list[str]:
    words = []
    while len(words) < n:
        comps = [r3c_component(rng) for _ in range(rng.randint(0, 3))]
        prefix = rng.choice(R3C_PREFIXES)
        word = (prefix.rstrip("/") + ("/" + "/".join(comps) if comps else "")) or "/"
        word += rng.choice(R3C_TAILS)
        if rng.random() < 0.06:
            # an unquoted backslash before a letter (decision 6; never before a metacharacter)
            at = [i for i, ch in enumerate(word) if ch.isalpha() and i > len(prefix)]
            if at:
                i = rng.choice(at)
                word = word[:i] + "\\" + word[i:]
        if word != "/*":
            words.append(word)
    return words


def test_r3c_property_the_hook_agrees_with_an_independent_reference(hooks):
    """R3C_PROPERTY_CASES seeded words (R3C_PROPERTY_SEED): r3b's generator plus wildcard-only and bracket
    components, the //, /., /./, /.. and <glob>/.. tails, the /Volumes/Macintosh HD prefix (and
    /Volumes/Boot, a link to / in the fixture), comma braces and zsh alternation, ~user, backslashes, ***
    and the folded code points. REFERENCE_SH (rewritten from the r3c brief, not from the hook) gives the
    verdict. Every word without a backslash goes through the hook's functions (pw_word in context z, the
    text rule by name for a word it leaves); every word with one, and 200 more, go through the whole hook
    as ``rg -n foo <word>`` (a space escaped), where the shell form is the hook's own reading."""
    rng = random.Random(R3C_PROPERTY_SEED)
    words = r3c_property_words(rng, R3C_PROPERTY_CASES)
    ref = reference_verdicts(words, PROP_HOME)
    verdict = dict(zip(words, ref))
    plain = list(dict.fromkeys(w for w in words if "\\" not in w))
    got = dict(zip(plain, hook_word_verdicts(plain, PROP_HOME)))
    differ = [(w, got[w], verdict[w]) for w in plain if got[w] != verdict[w]]
    escaped = list(dict.fromkeys(w for w in words if "\\" in w))
    sample = escaped + plain[:: max(1, len(plain) // 200)][:200]
    commands = {w: "rg -n foo " + w.replace(" ", "\\ ") for w in sample}
    outs = judge_prop(hooks["new"], list(commands.values()), home=PROP_HOME)
    whole = [(w, verdict[w]) for w, c in commands.items() if (rule(outs[c]) == "unscoped-search") != verdict[w]]
    report("property-r3c", {"seed": R3C_PROPERTY_SEED, "cases": len(words), "distinct": len(set(words)),
                            "broad": sum(ref), "escaped": len(escaped), "differ": len(differ),
                            "whole_hook": len(sample), "whole_hook_differ": len(whole),
                            "braces": sum("{" in w for w in words), "alternation": sum("(" in w for w in words),
                            "volumes": sum(w.lower().startswith("/volumes/") for w in words)})
    assert differ == [], differ[:10]
    assert whole == [], whole[:10]
    assert len(escaped) > 50 and 0.2 < sum(ref) / len(ref) < 0.8


def r3c_split(word: str, shell: str) -> list[str]:
    """The word's components as the shell splits them: zsh keeps a / inside a bracket expression."""
    if shell == "zsh":
        word = re.sub(r"\[([!^]?)([^]]*)\]", lambda m: "[" + m[1] + m[2].replace("/", "\a") + "]", word)
    return [c.replace("\a", "/") for c in word.split("/")]


WILD_ONLY = re.compile(r"(\*|\?|\[[!^]?\]?([^]]|\[:[a-z]+:\])*\])+")


def r3c_noglob(shell: str, word: str, home: str) -> list[str]:
    """The words the shell makes of ``word`` before globbing (braces and ~), seam-relative."""
    fs = str(_FS["root"])
    text = fs + word if word.startswith("/") else word
    env = {"PATH": os.environ["PATH"], "HOME": fs + home, "SUBFLEET_ROOT": fs + home + "/.subfleet",
           "SUBFLEET_HOME": fs + home + "/.subfleet"}
    if shell == "bash":
        argv = ["/bin/bash", "--noprofile", "--norc", "-c", "set -f; for m in " + text + "; do printf '%s\\n' \"$m\"; done"]
    else:
        argv = ["/bin/zsh", "-f", "-c", "setopt NO_GLOB; for m in " + text + "; do print -r -- $m; done"]
    out = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=fs, timeout=60)
    return [m[len(fs):] if m.startswith(fs) else m for m in out.stdout.splitlines()] if out.returncode == 0 else []


def r3c_real(shell: str, word: str, home: str = FAKE_HOME) -> bool:
    """The oracle: the word as the real shell expands it, over the fixture, names a root, reading the
    hub's decision 1 as written: a trailing run of wildcard-only (or .*, **) components after the
    trailing /, /. and <glob>/.. are taken off is the folder they sit in (zsh walks **/ and ***/), and a
    glob directly in a folder of a .subfleet is that folder. Every match and folder is judged by
    REFERENCE_SH as a written path (where links lead, .. after them, /Volumes/<entry>/x as /x)."""
    fs = str(_FS["root"])
    for w in (r3c_noglob(shell, word, home) if ("{" in word or "~" in word) else [word]):
        comps = r3c_split(w, shell)
        if shell == "zsh":
            comps = ["**" if c == "***" else c for c in comps]
            if "**" in comps[:-1]:
                k = comps.index("**")
                prefix = "/".join(comps[:k]) or "/"
                if r3c_paths(shell, prefix, home):
                    return True
                continue
        else:
            comps = ["*" if c in ("**", "***") else c for c in comps]
        walk = False
        while len(comps) > 1:
            last = comps[-1]
            if last in ("", "."):
                comps.pop()
            elif last == ".." and len(comps) > 2 and GLOB_CHAR.search(comps[-2]):
                comps = comps[:-2]; walk = True
            elif last in (".*", "**") or WILD_ONLY.fullmatch(last):
                comps.pop(); walk = True
            else:
                break
        prefix = "/".join(comps) or "/"
        if walk:
            if r3c_paths(shell, prefix, home):
                return True
            continue
        for k, comp in enumerate(comps):
            if k and GLOB_CHAR.search(comp):
                for parent in r3c_matches(shell, "/".join(comps[:k]) or "/", home):
                    d = parent.rstrip("/")
                    if ("/" + d).rsplit("/", 2)[-2].lower() == ".subfleet" and reference_cached([d], home)[0]:
                        return True
        if r3c_paths(shell, prefix, home):
            return True
    return False


def r3c_matches(shell: str, word: str, home: str) -> list[str]:
    if not GLOB_CHAR.search(word) and "(" not in word:
        return [spelled_abs(word, home) or word]
    fs = str(_FS["root"])
    return [m[len(fs):] for m in shell_matches(shell, word, home) if m.startswith(fs + "/")]


def r3c_paths(shell: str, word: str, home: str) -> bool:
    return any(reference_cached(r3c_matches(shell, word, home), home))


#: (word, the hook's verdict, the shells' verdict when it differs by design): the r3c glob, brace,
#: alternation, bracket, *** and /Volumes/<entry> cases, in the tester home.
R3C_SHELL_CASES = [
    *((w, None) for w in (
        "~/.subfleet/jobs/?*", "~/.subfleet/jobs/*?", "~/.subfleet/jobs/[!.]*", "~/.subfleet/jobs/[0-9]*",
        "~/.subfleet/jobs/2026*", "~/.subfleet/jobs/*/a1", "~/.subfleet/jobs/*//", "~/.subfleet/jobs/*/./",
        "~/.subfleet/jobs/*/.", "~/.subfleet/jobs/*/..", "~/PolicyEngine/?*", "~/PolicyEngine/*/.", "/tmp/?*",
        "/usr/?*", "/tmp/sf-*", f"~/.subfleet/jobs/{JOB}/a1/*", "~/.subfleet/w*/2026*", "~/Pol*/?*",
        "~/{PolicyEngine,TheAxiomFoundation}", "~/.{subfleet,cache}", "~/.sub{fleet,x}", "/U{sers,x}",
        "/{tmp,x}", "~/{src,work}/repo", "~/{PolicyEngine,{a,b}}", "~/{a..z}*", "/Users/tester/{PolicyEngine,.subfleet}",
        "~/{work,src}/*.py", "~/(PolicyEngine|TheAxiomFoundation)", "~/(.subfleet|x)", "~/(src|work)",
        "~/(Pol*|x)", "/[^/]*", "~/[!/]*", "~/.subfleet/[^/]*", "~/***/x", "~/.subfleet/***/x",
        "/Volumes/Boot/Users/tester/.subfleet/jobs/?*", "/Volumes/Boot/Users/*/.subfleet", "/Volumes/Boot/tmp/?*",
        "/Volumes/Boot/U*", "/Volumes/Boot/Users/tester/work/repo/*.py", "/Volumes/Boot/Users/tester/Rul*")),
    # by name inside a mount entry (decision 2): the hook lists nothing there, so a name a root can have
    # counts whether or not it is there; the shells, following the fixture's link, find no such entry
    ("/Volumes/Boot/Users/zz*", True), ("/Volumes/Boot/Users/glob/The*", True),
]


def test_r3c_globs_braces_and_alternation_agree_with_real_shells(hooks):
    """The real-shell differential (r3c): each case is denied exactly when some expansion of it in
    /bin/zsh -f or /bin/bash, over the fixture, names a root (``r3c_real``); alternation and a / inside
    brackets are zsh's alone (bash refuses the first and splits the second). The documented exceptions
    are the entry budget (none here) and decision 2's by-name reading inside a mount entry, listed with
    the hook's verdict."""
    commands = {w: "rg foo " + w.replace(" ", "\\ ") for w, _ in R3C_SHELL_CASES}
    outs = judge_prop(hooks["new"], list(commands.values()), home=FAKE_HOME)
    differ, counts = [], {"cases": len(R3C_SHELL_CASES), "denied": 0, "by_name": 0}
    for word, by_name in R3C_SHELL_CASES:
        denied = rule(outs[commands[word]]) == "unscoped-search"
        shells = {"zsh": r3c_real("zsh", word)}
        if "(" not in word:
            shells["bash"] = r3c_real("bash", word)
        counts["denied"] += denied
        if by_name is not None:
            counts["by_name"] += 1
            if denied != by_name or any(shells.values()):
                differ.append(("by name", word, denied, shells))
        elif denied != any(shells.values()):
            differ.append((word, denied, shells))
    report("shells-r3c", counts)
    assert not differ, differ[:10]


R3C_MOUNT_WORDS = [
    "/Volumes/Macintosh HD/Users/tester/.subfleet", "/Volumes/Macintosh HD/Users/*/.subfleet",
    "/Volumes/Macintosh HD/Users/tester/.subfleet/jobs/2026*", "/Volumes/*/Users/*/x", "/V*/Macintosh HD/U*",
    "/Volumes/Backup/Users/{a,b}", "/Volumes/Ma*/Users/tester/..", "/net/host/x/..", "/home/x/(a|b)",
    "/Volumes/Backup/***/x", "/Volumes/Backup/?*", "/Volumes/Backup/inside/[!.]*", "/Network/*/..",
    "/Volumes/Macintosh HD/inside/x", "/Volumes/Boot/Users", "/home/*/.subfleet/jobs/?*",
    # r5 decision 7 (review r4's P3-4): a mount reached through the data volume's alias, in any letter case,
    # with a later glob too
    "/System/Volumes/Data/Volumes/Backup/x/..", "/System/Volumes/Data/net/a/b/..", "/System/Volumes/Data/home/a/b/..",
    "/System/Volumes/Data/Network/a/b/..", "/SYSTEM/VOLUMES/DATA/Volumes/Backup/x/..", "/system/volumes/data/NET/a/b/..",
    "/System/Volumes/Data/Volumes/Backup/x/../*", "/System/Volumes/Data/home/a/b/../*.py",
    "/System/volumes/Data/Network/a/b/../x*", "/System/Volumes/Data/Volumes/B*/x/..",
]


def test_r3c_nothing_inside_a_mount_is_read(tmp_path):
    """r2's harness, the five disk functions wrapped: no word that names or globs a mount (r3c's
    /Volumes/<entry> words by name, braces, alternation, .. and *** under a mount) lists or stats
    anything inside /Volumes/<entry>, /Network, /net or /home. The fixture holds something in each."""
    verdicts, calls = traced(R3C_MOUNT_WORDS + MOUNT_WORDS, tmp_path / "trace", fs=None, home=FAKE_HOME)
    assert [c for c in calls if inside_a_mount(c[1])] == []
    got = dict(zip(R3C_MOUNT_WORDS, verdicts))
    assert got["/Volumes/Macintosh HD/Users/tester/.subfleet"] and got["/Volumes/Macintosh HD/Users/*/.subfleet"]
    assert got["/Volumes/Macintosh HD/Users/tester/.subfleet/jobs/2026*"] and got["/V*/Macintosh HD/U*"]
    assert got["/Volumes/Backup/?*"] and not got["/Volumes/Macintosh HD/inside/x"] and not got["/net/host/x/.."]


#: The code points whose casefold is one ASCII letter while they are none (decision 6), each checked
#: with os.path.samefile on this volume in round r3c: U+017F (long s) and U+212A (Kelvin sign).
R3C_FOLDED = {"ſ": "s", "K": "k"}


def test_r3c_folded_code_points_are_exactly_the_confirmed_set(tmp_path):
    """Every code point whose casefold() is a single ASCII letter while it is not one is in R3C_FOLDED;
    on a case-insensitive volume each names the same entry as its letter (os.path.samefile); and the
    hook's pw_normalize folds exactly these, so ~/.ſubfleet and /Uſers are roots by name."""
    found = {}
    for cp in range(0x110000):
        ch = chr(cp)
        if 0xD800 <= cp <= 0xDFFF:
            continue
        folded = ch.casefold()
        if len(folded) == 1 and folded.isascii() and folded.isalpha() and not (ch.isascii() and ch.isalpha()):
            found[ch] = folded
    assert found == R3C_FOLDED
    (tmp_path / "Ab").mkdir()
    if (tmp_path / "aB").exists() and os.path.samefile(tmp_path / "Ab", tmp_path / "aB"):
        for ch, letter in found.items():
            (tmp_path / f"x{letter}y").mkdir()
            assert os.path.samefile(tmp_path / f"x{letter}y", tmp_path / f"x{ch}y"), hex(ord(ch))
    paths = [f"/Users/me/x{ch}y" for ch in found] + ["/Uſers", "/Users/me/.ſubfleet", "/Ušers"]
    body = 'while IFS= read -r p; do pw_normalize "$p"; printf "%s\\n" "$pw_n"; done'
    out = run_r3_functions(body, "/Users/me", fs="/nonexistent-r3c-seam", stdin="".join(p + "\n" for p in paths))
    assert out.stdout.splitlines() == ["/Users/me/xsy", "/Users/me/xky", "/Users", "/Users/me/.subfleet", "/Ušers"]


# ---------------------------------------------------------------- r4: review r3's walk-arounds

# Round r4 (2026-10-26) builds the hub's decisions on review r3 (REQUEST_CHANGES at d9d4f355e). Each
# pin below is one of review r3's inputs (or the brief's), judged through the whole hook in the fixture:
#   1. a .. anywhere after a glob is folded before judging, and the word is broad when either the folded
#      or the unfolded reading is (a literal component's .. resolved physically);
#   2. a **/ after a wildcard-only component: the trailing-run rule runs on the part before **/ too;
#   3. every shell and eval script is judged: -o/+o/-O/+O values, option clusters ending in o or O,
#      --rcfile, -- and a quoted shell name; eval's joined arguments; a here-string, or an echo, printf
#      or cat pipe, into a shell given no script; \xHH, \NNN and \' in a $'...' script;
#   4. searchers after a keyword or a function body's {, after a wrapper's option values (a table), under
#      a wrapper named by path or as doas/gtimeout, and with a quote or a backslash inside the name;
#   5. a ${...} is no brace group, and a starred word whose group holds / or .. is broad (fails closed);
#   6. a path word with more than 64 { or [, or over 4 KB, is broad unscanned; the scans are one pass;
#   7. rg with a device or a closed stdin and no path searches ., so it is judged as .;
#   8. print-and-exit forms (rg --version, -V, --help, -h, --type-list, find --version) walk nothing;
#   9. ~$USER, ~${USER} and ~$(whoami) are the home, $"..." is "...", and /Volumes/<entry>/.. is / as
#      well as its by-name reading.
R4_LAYOUT = [
    ("Users/tester/work/repo/src/a.py", "file", None),
    ("Users/tester/work/repo/tests/unit/t.py", "file", None),
    ("Users/tester/PolicyEngine/policyengine-us/pkg/sub/deep.py", "file", None),
    ("Users/tester/notes.txt", "file", None),
    ("tmp/x/y/x", "file", None),
]
FS_LAYOUT += R4_LAYOUT
FS_DIGEST = hashlib.sha256(json.dumps(FS_LAYOUT).encode()).hexdigest()
H_ = FAKE_HOME

#: Decision 1 (review r3's P2-1, and a link standing for a literal component after the glob).
R4_FOLD_DENIED = [
    f"find ~/.sub*/jobs/{JOB}/.. -name AGENTS.md", f"rg --files ~/.subfleet/jobs/{JOB}/a*/../..",
    "rg --files ~/work/r*/../../.subfleet", "rg --files ~/*/policyengine-us/..",
    "rg --files ~/w*/jl/..", "rg --files ~/.subfleet/jobs/*/../..",
]
#: Decision 2 (review r3's P2-2).
R4_TREE_DENIED = [
    "rg foo ~/PolicyEngine/*/**/*.py", "grep -rn foo ~/PolicyEngine/*/**/*.py", "rg foo ~/PolicyEngine/?*/**/*.py",
    "rg foo /tmp/*/**/x", "rg foo ~/PolicyEngine/[!.]*/**/*.py",
]
#: Decision 3 (review r3's P2-3, and the brief's +o, +O, --rcfile, printf and cat forms).
R4_SCRIPTS_DENIED = [
    "bash -o pipefail -c 'rg --files ~/.subfleet'", "bash -euo pipefail -c 'rg --files ~/.subfleet'",
    "bash -O extglob -c 'rg --files ~/.subfleet'", "zsh -o extendedglob -c 'rg --files ~/.subfleet'",
    "bash +o posix -c 'rg --files ~/.subfleet'", "bash +O extglob -c 'rg --files ~/.subfleet'",
    "bash --rcfile /dev/null -c 'rg --files ~/.subfleet'", "bash -c -- 'rg --files ~/.subfleet'",
    "\"bash\" -c 'rg --files ~/.subfleet'", "'bash' -c 'rg --files ~/.subfleet'", "/bin/b\\ash -c 'rg --files ~/.subfleet'",
    "eval 'rg' --files ~/.subfleet", "eval \\rg --files ~/.subfleet", "eval -- 'rg --files ~/.subfleet'",
    "eval /usr/bin/find / -name x", "eval 'rg --files ~/.subfleet'",
    "echo 'rg --files ~/.subfleet' | bash", "printf '%s\\n' 'rg --files ~/.subfleet' | sh",
    "cat <<< 'rg --files ~/.subfleet' | zsh", "bash <<< 'rg --files ~/.subfleet'", "bash -s <<< 'rg --files ~/.subfleet'",
    "echo -n 'rg --files ~/.subfleet' | bash -s", "echo 'rg --files ~/.subfleet' | sudo -u root bash",
    "bash -c $'rg --files \\x7e/.subfleet'", "bash -c $'rg --files \\176/.subfleet'",
    "bash -c $'bash -c \\'rg --files ~/.subfleet\\''",
]
#: Decision 4 (review r3's P3-1; nice -n 5 is review r3's surviving mutant's pin).
R4_NAMES_DENIED = [
    "if true; then /usr/bin/find / -name x; fi", "if true; then \\rg --files ~/.subfleet; fi",
    "{ /usr/bin/find / -name x; }", "! /usr/bin/find / -name x", "while /usr/bin/find / -name x; do break; done",
    "for i in 1; do /usr/bin/find / -name x; done", "f() { /usr/bin/find / -name x; }; f",
    "sudo -u root /usr/bin/find / -name x", "timeout -s KILL 60 /usr/bin/find / -name x",
    "exec -a foo /usr/bin/find / -name x", "env -u FOO /usr/bin/find / -name x",
    "echo / | xargs -I {} /usr/bin/find {} -name x", "doas /usr/bin/find / -name x", "gtimeout 5 /usr/bin/find / -name x",
    "/usr/bin/env /usr/bin/find / -name x", "r''g --files ~/.subfleet", 'r""g --files ~/.subfleet',
    "r\\g --files ~/.subfleet", '"r"g --files ~/.subfleet', "$'rg' --files ~/.subfleet", "f\\ind / -name x",
    "fi''nd / -name x", "gr\\ep -r x ~/.subfleet", "nice -n 5 /usr/bin/find / -name x",
    "until /usr/bin/find / -name x; do :; done", "if ! \\rg --files ~/.subfleet; then :; fi",
]
#: Decision 5 (review r3's P3-2; the unclosed brace is review r3's surviving mutant's pin).
R4_BRACES_DENIED = [
    "rg --files /{Users/{tester,x},y}/.subfleet", "rg --files /{Users/tester,x}/.{s..s}ubfleet",
    "rg --files /{Users/tester,x}/.subfleet{,}{,}{,}{,}{,}{,}", "rg --files ~/work/{..,x}{,}{,}{,}{,}{,}{,}/.subfleet",
    "rg --files {/Users/${USER},x}/.subfleet", "rg --files ~/.subfleet ~/{a",
]
#: Decision 7 (review r3's P3-4), from the home.
R4_DEVICE_DENIED = ["rg foo </dev/null", "rg foo < /dev/null", "rg foo <&-", "rg -l foo 0</dev/null",
                    "rg foo </dev/zero", "rg foo 0<&-"]
#: Decision 8 (review r3's P3-5): from the home and from /, each prints and exits.
R4_META = ["rg --version", "rg -V", "rg --help", "rg -h", "rg --type-list", "find --version"]
#: ... and the forms whose option is a value, so the walk stays.
R4_META_DENIED = [("rg -e --version", H_), ("rg -g --version foo", H_), ("find -name --version", "/"),
                  ("rg -- --version", H_)]
#: Decision 9 (review r3's P3-6).
R4_FORMS_DENIED = [
    "rg --files ~$USER/.subfleet", "rg --files ~${USER}/.subfleet", "rg --files ~$(whoami)/.subfleet",
    'rg --files $"/Users/tester/.subfleet"', 'bash -c $"rg --files /Users/tester/.subfleet"',
    f'rg --files "{MAC}/../Users/tester/.subfleet"',
]
#: Review r3's check 3, kept: scoped searches from a lane worktree, scratch and /tmp (each of these 21
#: in each of the three, 63 in all) ...
R4_SCOPED_CWDS = [LANE_W, "/var/folders/9l/x/T/sf-lane", "/tmp/lane"]
R4_SCOPED = [
    "rg foo", "rg foo .", "rg foo src", "rg --files", "rg -n x tests/unit", "find . -name '*.py'", "grep -rn foo src",
    "rg foo src/{a,b}.py", "rg foo {src,tests}", "git grep foo", "rg -l foo src/*/x.py", "find src -maxdepth 2",
    "rg foo </dev/null", "echo 'rg foo src' | bash", "bash -c 'rg foo src'", "eval 'rg foo src'",
    "if true; then rg foo src; fi", "timeout -s KILL 60 rg foo src", "rg foo src/*/**/*.py", "rg --version",
    "bash -euo pipefail -c 'rg -n foo src'",
]
#: ... roots in pattern position (12) ...
R4_PATTERN_ROOTS = [
    "rg '/usr' src", "rg -e '/usr' src", "rg -g '/Library/*' foo src", "rg '~/.subfleet' src", "rg '/' src",
    "rg -F '/tmp' src", "grep -rn '/Users' src", "rg -w '/opt' src", "rg '$HOME' src", 'rg "~" src',
    "rg '/System' src", "grep -r -e '/var' src",
]
#: ... and the must-allow cases (13), with prose and depth bounds: (command, cwd).
R4_MUST_ALLOW = [
    ("rg foo src/{a,b}.py", LANE_W), ("rg foo {src,tests}", LANE_W), ("git grep foo", H_), ("rg -n x tests/unit", LANE_W),
    ("find . -name '*.py'", LANE_W), ("rg x /*/*.py", S_), ("rg foo /Volumes/*/x*", S_),
    ('git commit -m "find / -name x"', S_), ('echo "rg --files ~/.subfleet" >> notes.md', S_),
    ('git commit -m "eval rg --files ~/.subfleet; bash -euo pipefail -c \'find / -name x\'"', S_),
    ("rg --max-depth 1 foo ~/.subfleet", S_), ("find ~/.subfleet -maxdepth 1 -name x", S_),
    ("RG=rg; $RG --files ~/.subfleet", S_),
]
#: The forms each decision must leave alone: (command, cwd).
R4_ALLOWED = [
    # 1 and 2: no root on either reading
    (f"rg foo {LANE_W}/*/**/*.py", S_), ("rg foo ~/work/*/**/*.py", S_), ("rg foo src/*/**/*.py", LANE_W),
    ("rg foo ~/work/r*/src/..", S_), (f"rg -l foo ~/.subfleet/jobs/{JOB}/a*/..", S_),
    # 3: a script that is scoped, a script file, and a pipe into something that is no shell
    ("echo 'rg foo src' | bash", S_), ("bash script.sh", S_), ("echo hi | bash -c 'rg foo src'", S_),
    ("echo 'rg --files ~/.subfleet' | cat", S_), ('eval "$(ssh-agent -s)"', S_), ("bash -o pipefail -c 'rg foo src'", S_),
    # 4: scoped searchers after a keyword or a wrapper
    ("if [ -d src ]; then rg foo src; fi", S_), ("sudo -u root rg foo src", S_), ("doas /usr/bin/rg foo src", S_),
    ("f() { rg foo src; }; f", S_), ("echo src | xargs -I {} rg foo {}", S_),
    # 5: brace groups that hold no / or ..
    ("rg foo src/{a,b}/{c,d}.py", LANE_W), ("rg foo src/{1..3}.py", LANE_W), ("rg foo src/${X}/y", LANE_W),
    # 7: a device from a scoped cwd, a file and a pipe from the home
    ("rg foo </dev/null", LANE_W), ("rg foo < notes.txt", H_), ("cat notes.txt | rg foo", H_), ("rg foo <<< x", H_),
    # 8: print and exit, from the home and /
    *((c, cwd) for cwd in (H_, "/") for c in R4_META),
    # 9: the forms read as their homes, scoped
    ("rg foo ~$USER/work/repo/src", S_), ('rg foo $"src"', LANE_W),
]


def r4_denied(commands, cwd: str = S_) -> list:
    """The commands the hook does not deny as [unscoped-search] (empty when every one is)."""
    outs = judged(HOOK, commands, cwd)
    return [c for c in commands if rule(outs[c]) != "unscoped-search"]


def test_r4_dotdot_after_a_glob_is_folded():
    """Decision 1: each <component>/.. after the first glob is folded, and either reading denies."""
    assert r4_denied(R4_FOLD_DENIED) == []


def test_r4_a_tree_glob_after_a_wildcard_component_is_its_folder():
    """Decision 2: the trailing-run rule runs on the part before a **/ (zsh walks every match)."""
    assert r4_denied(R4_TREE_DENIED) == []


def test_r4_every_shell_and_eval_script_is_judged():
    """Decision 3: shell options and their values, --, quoted shell names, eval's joined arguments,
    stdin scripts and ANSI-C scripts; the 4-deep bound still holds (R3C_DEEP)."""
    assert r4_denied(R4_SCRIPTS_DENIED + [R3C_DEEP]) == []
    assert rule(run_hook(HOOK, nest("true; rg --files src", 4))) is None


def test_r4_searchers_after_keywords_wrappers_and_quotes_are_found():
    """Decision 4: keywords, a function body's {, wrapper option values, wrappers by basename, quoted names."""
    assert r4_denied(R4_NAMES_DENIED) == []


def test_r4_brace_groups_that_span_a_slash_or_climb_fail_closed():
    """Decision 5: ${...} is no group; a starred word whose group holds / or .. is broad."""
    assert r4_denied(R4_BRACES_DENIED) == []


def test_r4_a_device_or_a_closed_stdin_is_no_pipe():
    """Decision 7: rg with </dev/null, <&- or another /dev/ target and no path searches the cwd."""
    assert r4_denied(R4_DEVICE_DENIED, H_) == []


def test_r4_print_and_exit_forms_walk_nothing():
    """Decision 8: allowed from any cwd; an option that is another option's value is no such form."""
    for cwd in (H_, "/", S_):
        outs = judged(HOOK, R4_META, cwd)
        assert [c for c in R4_META if rule(outs[c]) is not None] == [], cwd
    assert [(c, cwd) for c, cwd in R4_META_DENIED if rule(run_hook(HOOK, c, cwd)) != "unscoped-search"] == []


def test_r4_the_unlisted_forms_are_judged():
    """Decision 9: ~$USER, ~${USER}, ~$(whoami), $"..." and /Volumes/<entry>/.. ."""
    assert r4_denied(R4_FORMS_DENIED) == []


def test_r4_scoped_searches_stay_allowed():
    """Review r3's check 3 (scoped searches in a lane worktree, scratch and /tmp, roots in pattern
    position, the must-allow cases) and each decision's must-allow forms."""
    cases = ([(c, cwd) for cwd in R4_SCOPED_CWDS for c in R4_SCOPED] + [(c, LANE_W) for c in R4_PATTERN_ROOTS]
             + R4_MUST_ALLOW + R4_ALLOWED)
    assert len(R4_SCOPED) * len(R4_SCOPED_CWDS) >= 47 and len(R4_PATTERN_ROOTS) == 12 and len(R4_MUST_ALLOW) == 13
    outs = judge_all(HOOK, [(c, cwd) for c, cwd in cases])
    assert [case for case in cases if rule(outs[case]) is not None] == []


# ---------------------------------------------------------------- r4: cost (decision 6)

#: Hostile words beside a crawl: each must finish in R4_HOSTILE_S and stay denied. The time is the best
#: of three runs (the host's load is not the hook's cost; the bound is not loosened). r7 decision 6: the time is
#: child CPU (hook_cpu, r6 decision 10's measure), the same 2.0 s: review r6 saw 3.39, 3.72 and 2.21 s of wall at
#: load 44-52 on rows that took 0.23-0.28 s of CPU.
R4_HOSTILE_S = 2.0
R4_HOSTILE = {
    "1,000 unclosed {": "rg --files ~/.subfleet ~/" + "{" * 1000,
    "/[ x1,000": "rg --files ~/.subfleet " + "/[" * 1000,
    "10,000 commas": "rg --files ~/.subfleet ~/{" + "," * 10000 + "}",
    # under 4 KB, but 2,040 components: each cost the folder walks a pass over the word (7 s in r3c's
    # loop), so more than 256 components is capped too (the smallest change that keeps decision 6's intent)
    "2,040 components in 4 KB": "rg --files ~/.subfleet " + "/a" * 2040,
}
#: The cap's scanned edge (64 { or [ in just under 4 KB): judged, and denied beside the crawl; its time is
#: reported, not bounded (2.4 s and 5.1 s best of three at load ~100 in round r4: the residual cost).
R4_CAP_EDGE = {
    "64 unclosed { in 4 KB": "rg --files ~/.subfleet ~/" + ("{" + "a" * 62) * 64,
    "64 /[ in 4 KB": "rg --files ~/.subfleet " + ("/[" + "a" * 61) * 64,
}
#: ... and capped words alone: broad unscanned (fail closed), whatever they hold.
R4_CAPPED = {
    "65 {": f"rg foo {S_}/src/" + "{a}" * 65, "65 [": f"rg foo {S_}/src/" + "[a]" * 65,
    "4 KB": f"rg foo {S_}/src/" + "a" * 4097, "257 components": f"rg foo {S_}/src" + "/a" * 257,
}


def hook_seconds(hook: Path, command: str, cwd: str = S_) -> tuple[float, str]:
    started = time.perf_counter()
    out = run_hook(hook, command, cwd)
    return time.perf_counter() - started, out


def test_r4_hostile_words_fail_closed_fast():
    """Decision 6: 1,000 unclosed {, /[ x1,000, 10,000 commas and 2,040 components each finish under 2 s
    beside a crawl and stay denied; the scanned edge (64 { or [ in 4 KB) stays denied, its time reported; a
    word past the cap (more than 64 { or [, more than 256 components, or over 4 KB) is broad on its own. r7
    decision 6: the 2 s is child CPU (best of three), with wall time and the load reported."""
    rows, walls = {}, {}
    for name, command in R4_HOSTILE.items():
        runs = [hook_cpu(HOOK, command) for _ in range(3)]
        assert all(rule(out) == "unscoped-search" for _, _, out in runs), name
        rows[name] = min(cpu for cpu, _, _ in runs)
        walls[name] = min(wall for _, wall, _ in runs)
    edge = {}
    for name, command in R4_CAP_EDGE.items():
        cpu, wall, out = hook_cpu(HOOK, command)
        assert rule(out) == "unscoped-search", name
        edge[name] = {"cpu": cpu, "wall": wall}
    report("hostile-r4", {"cpu": rows, "wall": walls, "cap edge (not bounded)": edge,
                          "load": round(os.getloadavg()[0], 1)})
    assert {name: t for name, t in rows.items() if t >= R4_HOSTILE_S} == {}
    for name, command in R4_CAPPED.items():
        assert rule(run_hook(HOOK, command, LANE_W)) == "unscoped-search", name
    assert rule(run_hook(HOOK, f"rg foo {S_}/src/" + "{a}" * 64, LANE_W)) is None
    assert rule(run_hook(HOOK, f"rg foo {S_}/src/" + "a" * 4000, LANE_W)) is None
    assert rule(run_hook(HOOK, f"rg foo {S_}/src" + "/a" * 200, LANE_W)) is None


def hook_cpu(hook: Path, command: str, cwd: str = S_) -> tuple[float, float, str]:
    """(child CPU seconds, wall seconds, stdout) of one hook run: the RUSAGE_CHILDREN user and system time
    the run added (r6 decision 10: the host's load moves wall time, not the hook's own work)."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    out = run_hook(hook, command, cwd)
    wall = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime), wall, out


def test_r4_absolute_paths_cost_at_most_twice_the_installed_hook(hooks):
    """Decision 6: 300 absolute paths, judged five times by each hook, interleaved: the new hook's median
    is at most twice the installed hook's (the installed hook, a60d1c51..., rebuilt as hooks["old"]). r6
    decision 10: the cost is child CPU (RUSAGE_CHILDREN deltas), the same 2x bound on a steadier measure;
    wall time is reported."""
    command = "rg foo " + " ".join(f"{S_}/src/f{i}.py" for i in range(300))
    new, old, new_wall, old_wall = [], [], [], []
    for _ in range(5):
        cpu, wall, out = hook_cpu(HOOK, command)
        assert rule(out) is None
        new.append(cpu)
        new_wall.append(wall)
        cpu, wall, _ = hook_cpu(hooks["old"], command)
        old.append(cpu)
        old_wall.append(wall)
    report("abs300-r4", {"cpu": {"new": sorted(new), "old": sorted(old),
                                 "ratio": round(sorted(new)[2] / sorted(old)[2], 2)},
                         "wall": {"new": sorted(new_wall), "old": sorted(old_wall)},
                         "load": round(os.getloadavg()[0], 1)})
    assert sorted(new)[2] <= 2 * sorted(old)[2], (new, old)


# ---------------------------------------------------------------- r4: the real shells

#: Stub searchers: each run records its name, its cwd, what its stdin is and its argv (NUL-separated, one
#: file per run) and exits 0; the wrappers this host lacks (timeout, gtimeout, doas) and sudo skip their
#: options and run the rest.
R4_STUB = r"""#!/bin/sh
if [ -p /dev/stdin ]; then s=pipe; elif [ -f /dev/stdin ]; then s=file; elif [ -e /dev/stdin ]; then s=dev
else s=closed; fi
f=$(/usr/bin/mktemp "$R4_CALLS/call.XXXXXX")
printf '%s\0' "${0##*/}" "$PWD" "$s" "$@" > "$f"
"""
R4_WRAP = r"""#!/bin/sh
case "${0##*/}" in
  sudo|doas) while [ $# -gt 0 ]; do case "$1" in -u|-g|-C|-D|-h|-p|-r|-t|-U|-T) shift 2 ;; -*) shift ;; *) break ;; esac; done ;;
  *) while [ $# -gt 0 ]; do case "$1" in -s|-k) shift 2 ;; -*) shift ;; *) break ;; esac; done; shift ;;
esac
exec "$@"
"""
R4_RUNS = itertools.count()
R4_SEARCHERS = ("rg", "find", "grep", "egrep", "fgrep", "rgrep", "ugrep")
R4_RG_VALUE = set("ABCEMTdefgjmrt")
R4_RG_LONG = {"--after-context", "--before-context", "--context", "--color", "--colors", "--context-separator",
              "--encoding", "--engine", "--file", "--glob", "--iglob", "--ignore-file", "--max-columns",
              "--max-count", "--max-filesize", "--path-separator", "--pre", "--pre-glob", "--regexp", "--replace",
              "--sort", "--sortr", "--threads", "--type", "--type-not", "--type-add", "--type-clear"}


def r4_walked_paths(name: str, cwd: str, stdin: str, args: list[str]) -> list[str]:
    """What one stub run would have walked had it been the real searcher (snapshot-5's models, cut down
    to the options the pins use): find's start paths, rg's and grep -r's path operands, or the cwd."""
    if name == "find":
        if args[:1] and args[0] in ("--version", "-version", "--help", "-help"):
            return []
        paths = []
        for a in args:
            if a.startswith("-") or a in ("(", "!"):
                break
            paths.append(a)
        return paths or [cwd]
    grep = name != "rg"
    pos, files, has_e, rec, meta, k = [], False, False, name == "rgrep", False, 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            pos += args[k:]
            break
        if a.startswith("--"):
            files |= a == "--files"
            has_e |= a.split("=")[0] in ("--regexp", "--file")
            rec |= a in ("--recursive", "--dereference-recursive")
            meta |= a in ("--version", "--help", "--type-list")
            if "=" not in a and a in R4_RG_LONG:
                k += 1
            continue
        if a.startswith("-") and len(a) > 1:
            rec |= grep and bool(re.search(r"[rR]", a))
            meta |= (not grep and a in ("-V", "-h")) or (grep and a == "-V")
            has_e |= a[-1] in "ef"
            if not grep and a[-1] in R4_RG_VALUE or grep and a[-1] in "ABCDefm":
                k += 1
            continue
        pos.append(a)
    if meta or (grep and not rec):
        return []
    paths = pos if (files or has_e) else pos[1:]
    if paths:
        return paths
    if not grep and not files and stdin in ("pipe", "file"):
        return []
    return [cwd]


def r4_canonical(path: str, cwd: str) -> str:
    """A walked path inside the fixture, links followed as the kernel follows them, seam-relative; one
    outside it by name (/Volumes/Macintosh HD is a link to / on this host: review r3's os.readlink)."""
    fs, rfs = str(_FS["root"]), os.path.realpath(str(_FS["root"]))
    full = path if path.startswith("/") else cwd.rstrip("/") + "/" + path
    if any(full == f or full.startswith(f + "/") for f in (fs, rfs)):
        real = os.path.realpath(full)
        return (real[len(rfs):] or "/") if real == rfs or real.startswith(rfs + "/") else real
    if full.startswith(MAC + "/") or full == MAC:
        full = "/" + full[len(MAC):]
    return norm(full)


def r4_shell_walks(shell: str, command: str, cwd: str, tmp: Path) -> bool:
    """Whether ``command``, run by the real shell over the fixture with stub searchers, would have walked a
    root: some stub run's walked path is broad by REFERENCE_SH."""
    fs = str(_FS["root"])
    stubs = tmp / "stubs"
    if not stubs.exists():
        stubs.mkdir()
        for name in R4_SEARCHERS:
            (stubs / name).write_text(R4_STUB)
        for name in ("timeout", "gtimeout", "doas", "sudo"):
            (stubs / name).write_text(R4_WRAP)
        for p in stubs.iterdir():
            p.chmod(0o755)
    calls = tmp / f"calls-{shell}-{next(R4_RUNS)}"
    calls.mkdir()
    text = command.replace("/usr/bin/find", f"{stubs}/find").replace("/usr/bin/grep", f"{stubs}/grep")
    text = text.replace("/opt/homebrew/bin/rg", f"{stubs}/rg").replace("/usr/bin/rg", f"{stubs}/rg")
    text = re.sub(r"(?<![\w./~-])/Users/tester", fs + "/Users/tester", text)
    text = re.sub(r"(?<![\w./~-])/tmp/", fs + "/tmp/", text)
    text = re.sub(r"(?<=\s)/(?=\s|$)", fs + "/", text)
    # the session's PATH after the stubs (the test session's keychain guard refuses a shell whose PATH
    # reaches a system folder before its recorder)
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": fs + FAKE_HOME, "R4_CALLS": str(calls),
           "USER": os.environ.get("USER", "nobody"), "LC_ALL": "C", "TMPDIR": str(tmp)}
    argv = ["/bin/bash", "--noprofile", "--norc", "-c", text] if shell == "bash" else ["/bin/zsh", "-f", "-c", text]
    with open(os.devnull) as devnull:
        subprocess.run(argv, stdin=devnull, capture_output=True, env=env, cwd=fs + cwd, timeout=60)
    walked = []
    for f in sorted(calls.iterdir()):
        name, at, stdin, *args = f.read_bytes().decode().split("\0")[:-1]
        walked += [r4_canonical(p, at) for p in r4_walked_paths(name, at, stdin, args)]
    return any(reference_cached(walked, FAKE_HOME)) if walked else False


#: The decisions' command pins whose shell run a stub can watch: (command, cwd). Decision 2's walks are the
#: shell's own (zsh expands **/ itself), so they are in R4_SHELL_WORDS, judged by the word oracle.
R4_SHELL_COMMANDS = ([(c, S_) for c in R4_FOLD_DENIED + R4_SCRIPTS_DENIED + R4_NAMES_DENIED + R4_BRACES_DENIED
                      + R4_FORMS_DENIED] + [(c, H_) for c in R4_DEVICE_DENIED]
                     + [(c, cwd) for c, cwd in R4_META_DENIED] + [(c, H_) for c in R4_META]
                     + [case for case in R4_ALLOWED if not case[0].startswith(("rg foo src/{", "bash script", "eval \"$("))])
#: The shell runs the hook judges by design, not by what the stub saw: a by-name reading the real file
#: system cannot show, and the stubs' own limits. (command, the hook's verdict).
R4_SHELL_EXCEPTIONS = {
    # the fixture has no link to / to stand for /Volumes/Macintosh HD; its walk is r4_canonical's by-name /
    f'rg --files "{MAC}/../Users/tester/.subfleet"': True,
}


def test_r4_command_pins_agree_with_real_shells(tmp_path):
    """Each command pin and must-allow form, run by /bin/zsh -f and /bin/bash over the fixture with stub
    searchers: the hook denies it exactly when either shell's run would have walked a root."""
    outs = judge_all(HOOK, R4_SHELL_COMMANDS)
    rows, differ = [], []
    for command, cwd in R4_SHELL_COMMANDS:
        denied = rule(outs[(command, cwd)]) == "unscoped-search"
        shells = {sh: r4_shell_walks(sh, command, cwd, tmp_path) for sh in ("zsh", "bash")}
        rows.append((command, cwd, denied, shells))
        if command in R4_SHELL_EXCEPTIONS:
            if denied != R4_SHELL_EXCEPTIONS[command]:
                differ.append(("exception", command, denied, shells))
        elif denied != any(shells.values()):
            differ.append((command, cwd, denied, shells))
    report("shells-r4", {"cases": len(rows), "denied": sum(r[2] for r in rows),
                         "zsh": sum(r[3]["zsh"] for r in rows), "bash": sum(r[3]["bash"] for r in rows)})
    assert differ == [], differ[:10]


#: Decision 2's (and 1's and 5's) words, for the word oracle: zsh expands **/ itself.
R4_SHELL_WORDS = ["~/PolicyEngine/*/**/*.py", "~/PolicyEngine/?*/**/*.py", "/tmp/*/**/x", "~/PolicyEngine/[!.]*/**/*.py",
                  "~/work/*/**/*.py", f"{LANE_W}/*/**/*.py", f"~/.sub*/jobs/{JOB}/..", "~/work/r*/../../.subfleet",
                  "~/*/policyengine-us/..", f"~/.subfleet/jobs/{JOB}/a*/../..", "~/work/r*/src/..",
                  "/{Users/{tester,x},y}/.subfleet", "/{Users/tester,x}/.{s..s}ubfleet"]


def r4_real(shell: str, word: str, home: str = FAKE_HOME) -> bool:
    """r3c_real, with decision 2 as written: in zsh the part before a **/ component goes through the
    trailing-run rule (a wildcard-only run is its folder) before its matches are judged."""
    if shell == "zsh":
        for w in (r3c_noglob(shell, word, home) if ("{" in word or "~" in word) else [word]):
            comps = ["**" if c == "***" else c for c in r3c_split(w, shell)]
            if "**" in comps[:-1]:
                prefix = comps[:comps.index("**")]
                while len(prefix) > 1 and (prefix[-1] in ("", ".", ".*") or WILD_ONLY.fullmatch(prefix[-1])):
                    prefix.pop()
                if r3c_paths(shell, "/".join(prefix) or "/", home):
                    return True
    return r3c_real(shell, word, home)


def test_r4_words_agree_with_real_shells():
    """The word differential (r3c's, with r4_real): each word is denied exactly when some expansion in
    /bin/zsh -f or /bin/bash, over the fixture, names a root."""
    commands = {w: "rg foo " + w for w in R4_SHELL_WORDS}
    outs = judge_prop(HOOK, list(commands.values()), home=FAKE_HOME)
    differ = [(w, rule(outs[commands[w]]), r4_real("zsh", w), r4_real("bash", w)) for w in R4_SHELL_WORDS
              if (rule(outs[commands[w]]) == "unscoped-search") != (r4_real("zsh", w) or r4_real("bash", w))]
    assert differ == []


# ---------------------------------------------------------------- review r4: r4's unpinned readings

#: Four r4 readings no named test pinned (review r4's mutants rv4-otherfd-is-stdin, rv4-grep-meta-walks,
#: rv4-backtick-space-dropped and rv4-function-keyword-dropped each survived its decision's test): a
#: redirect on another fd leaves rg's stdin alone (decision 7's deviation), grep's print-and-exit forms
#: walk nothing (decision 8's deviation), a searcher right after an opening backtick is read (decision 4's
#: prep), and a `function name {` body is past its keyword (decision 4).
REVIEW_R4_DENIED = [("rg foo 3<notes.txt", H_), ("rg foo 3</dev/null", H_),
                    ("echo `\\rg --files ~/.subfleet | head`", S_), ("function f { /usr/bin/find / -name x; }; f", S_)]
REVIEW_R4_ALLOWED = [("grep -r -V x", H_), ("grep -r --version x", H_), ("grep -r --help x", H_),
                     ("grep -rV x", "/")]


def test_review_r4_pins_r4s_unpinned_readings():
    """Each reading's own input: denied ones walk a root (both shells ran the stub with it), allowed ones
    print and exit."""
    outs = judge_all(HOOK, REVIEW_R4_DENIED + REVIEW_R4_ALLOWED)
    assert [case for case in REVIEW_R4_DENIED if rule(outs[case]) != "unscoped-search"] == []
    assert [case for case in REVIEW_R4_ALLOWED if rule(outs[case]) is not None] == []


# ---------------------------------------------------------------- r5: review r4's findings

# Round r5 (2026-10-27) builds the hub's decisions on review r4 (REQUEST_CHANGES at 6afb5ce19). Each pin below is
# one of review r4's inputs (or the brief's), judged through the whole hook in the fixture:
#   1. awk's base() is one split, so a long word with no slash costs what a short one does;
#   2. heredoc bodies are data: the command prep keeps them out of its quote and separator tracking, and a body
#      fed to a shell given no script is that shell's script;
#   3. a depth bound covers only its own command's walk: the xargs rule reads a bounded producer's words, and the
#      command find's -exec, -execdir, -ok or -okdir runs is judged on its own, {} standing for find's roots;
#   4. the overlay doc holds one copy of its body;
#   5. a closing backtick ends a root;
#   6. a glob component bash can match against .. reads as its folder and that folder's parent;
#   7. no disk call is made inside a stall mount reached through the data volume's alias (R3C_MOUNT_WORDS);
#   8. a command too long to prep that names a searcher is denied;
#   9. a root spelling right before a brace group is a path word;
#  10. ~ followed by $ or a backtick is the home, and /Volumes/<glob>/.. is / as well;
#  11. zsh's =cmd, a pipe into a shell through any stage, a positional -c script, eval of a command
#      substitution, and <&0 are judged.
R5_PRE = "cat > /dev/null <<'EOF'\nit's\nEOF\n"
#: Decision 2 (review r4's P2-2): each was allowed because an apostrophe or a lone " in a heredoc body hid
#: the rest from the prep. (command, cwd).
R5_HEREDOC_DENIED = [
    (R5_PRE + "/usr/bin/find / -name x", S_), (R5_PRE + "\\rg --files ~/.subfleet", S_),
    (R5_PRE + "sudo -u root /usr/bin/find / -name x", S_), (R5_PRE + "echo 'rg --files ~/.subfleet' | bash", S_),
    (R5_PRE + "bash <<< 'rg --files ~/.subfleet'", S_), (R5_PRE + "eval 'rg' --files ~/.subfleet", S_),
    (R5_PRE + "bash -c $'rg --files \\x7e/.subfleet'", S_),
    ("git commit -F - <<'EOF'\nIt's fixed\nEOF\ntimeout -s KILL 60 /usr/bin/find / -name x", S_),
    ("cat <<'EOF' > x.md\nsay \"hi\nEOF\n/usr/bin/find / -name x", S_),
    ("python3 - <<'EOF'\nx = 'a\nEOF\n/opt/homebrew/bin/rg --files ~/.subfleet", S_),
    ("cat <<EOF > x.md\ndon't\nEOF\nif true; then \\rg --files ~/.subfleet; fi", S_),
    ("cat > notes.md <<'EOF'\nit's\nEOF\nrg foo", H_),
    # a heredoc into a shell given no script is its script (<<- strips the tabs); two on a line, in order
    ("bash <<'EOF'\n\\rg --files ~/.subfleet\nEOF", S_), ("sh -s <<'EOF'\necho \"it's\"\n/usr/bin/find / -name x\nEOF", S_),
    ("zsh <<EOF\n\\rg --files ~/.subfleet\nEOF", S_), ("bash <<-EOF\n\t\\rg --files ~/.subfleet\n\tEOF", S_),
    ("cat <<A; bash <<B\nit's\nA\n\\rg --files ~/.subfleet\nB", S_), ("cat <<'EOF' | bash\n\\rg --files ~/.subfleet\nEOF", S_),
]
#: ... the control pair's other half and a plain find after the apostrophe (denied before r5 too) ...
R5_HEREDOC_CONTROLS = [("cat > /dev/null <<'EOF'\nits\nEOF\n/usr/bin/find / -name x", S_), (R5_PRE + "find / -name x", S_)]
#: ... and what stays allowed: a routine lane commit, a scoped search after a body, a scoped shell body, a body
#: no shell runs (data), and an unterminated heredoc, whose rest is read as commands as before.
R5_HEREDOC_ALLOWED = [
    ("git commit -F - <<'EOF'\nIt's fixed\nEOF", S_), ("git commit -F - <<'EOF'\nIt's fixed\nEOF\nrg foo src", S_),
    ("bash <<'EOF'\nrg foo src\nEOF", S_), ("cat > /dev/null <<'EOF'\nit's\nEOF\nrg foo src", H_),
    ("cat > /dev/null <<'EOF'\nrg foo\nEOF", H_), ("cat <<'EOF'\nrg foo src", S_),
]
#: Decision 3 (review r4's P2-3 and the installed hook's form).
R5_BOUND_DENIED = [
    "find ~/.subfleet -maxdepth 1 -type d | xargs rg foo", "find ~/.subfleet -maxdepth 0 | xargs rg --files",
    "find ~ -maxdepth 1 -name .subfleet | xargs rg --files", "find ~/.subfleet -maxdepth 1 | xargs -I{} rg --files {}",
    "find . -maxdepth 1 -exec rg foo ~ \\;", "find ~/.subfleet -maxdepth 1 -exec rg foo {} \\;",
    "find ~/PolicyEngine -maxdepth 1 -type d | xargs rg foo",
]
R5_BOUND_ALLOWED = [
    ("find src -maxdepth 1 -type d | xargs rg foo", S_), ("find src -maxdepth 1 -exec rg foo {} \\;", S_),
    ("find ~/.subfleet -maxdepth 1 -name x", S_), ("find ~/.subfleet -maxdepth 1 | xargs rg --max-depth 1 foo", S_),
    ("find . -name '*.py' -exec grep -l / {} +", LANE_W), ("find . -maxdepth 2 -name '*.py' | xargs rg -l foo", LANE_W),
]
#: Decision 5 (review r4's P3-2): a root right before a closing backtick ...
R5_BACKTICK_DENIED = ["echo `rg --files ~/.subfleet`", "echo `find ~/.subfleet`", "echo `rg foo /`", "x=`grep -r foo ~`",
                      "echo `rg foo ~/PolicyEngine`", 'echo "`rg --files ~/.subfleet`"',
                      # a root only the path-word pass knows, which the space before the backtick lets it read
                      "echo `rg --files /usr`", 'echo "$(rg --files /usr)"']
#: ... and its two denied controls.
R5_BACKTICK_CONTROLS = ["echo `rg --files ~/.subfleet | head`", "$(rg --files ~/.subfleet)"]
R5_BACKTICK_ALLOWED = [("echo `rg foo src`", S_), ('echo "`git rev-parse HEAD`"', S_), ('git commit -m "use \\`rg\\` / here"', S_)]
#: Decision 6 (review r4's P3-3, and the brief's bash -c form).
R5_DOTDOT_DENIED = [
    "rg foo ~/work/.*", f"rg --files ~/.subfleet/jobs/{JOB}/.*", f"rg --files ~/.subfleet/jobs/{JOB}/.?",
    "rg --files ~/work/.?/.subfleet", "find ~/work/.* -name x", "bash -c 'rg --files ~/work/.*'",
    "rg --files ~/work/.[.]/.subfleet", "rg --files ~/work/.[!a]/.subfleet",
]
#: ... and components that cannot match .., or whose parent is no root.
R5_DOTDOT_ALLOWED = [("rg foo .*rc", S_), ("rg foo ~/work/.*rc", S_), ("rg foo ~/work/repo/src/.*", S_),
                     ("rg foo ~/work/repo/.?", S_), ("rg foo ~/work/[.]*/.subfleet", S_)]
#: Decision 8 (review r4's P3-5): over 64 KB, the prep is skipped.
R5_LONG = "x" * 65600
R5_TOO_LONG_DENIED = [f"echo {R5_LONG}; \\rg --files ~/.subfleet", f"echo {R5_LONG}; /usr/bin/find / -name x"]
#: Decision 9 (review r4's P3-6).
R5_BRACE_DENIED = ["rg --files ~{/.subfleet,/x}", "rg --files ~{,/.subfleet}", "rg --files $HOME{,}/.subfleet",
                   "find ~{,} -name x", "rg --files ~{,}"]
R5_BRACE_ALLOWED = [("rg foo src/{a,b}.py", LANE_W), ("rg foo {src,tests}", LANE_W), ("rg foo ~{/work/repo/src,/work/repo/tests}", S_)]
#: Decision 10 (review r4's P3-7).
R5_FAMILY_DENIED = ["rg --files ~$USERNAME/.subfleet", "rg --files ~$LOGNAME/.subfleet", "rg --files ~${LOGNAME}/.subfleet",
                    "rg --files ~$(id -un)/.subfleet", "rg --files ~`whoami`/.subfleet",
                    "rg --files /Volumes/M*/../Users/tester/.subfleet"]
R5_FAMILY_ALLOWED = [("rg foo ~$LOGNAME/work/repo/src", S_), ("rg foo ~$(id -un)/work/repo/src", S_)]
#: Decision 11 (review r4's P3-8). (command, cwd).
R5_FORMS_DENIED = [
    ("=rg --files ~/.subfleet", S_), ("=find / -name x", S_),
    ("echo 'rg --files ~/.subfleet' | cat | bash", S_), ("echo 'rg --files ~/.subfleet' | tee /dev/null | bash", S_),
    ("printf 'rg --files %s\\n' ~/.subfleet | bash", S_), ("printf 'rg --files ~/.subfleet' | xargs -0 bash -c", S_),
    ("sh -c '\"$@\"' sh \\rg --files ~/.subfleet", S_), ("bash -c '$@' sh rg --files ~/.subfleet", S_),
    ("eval \"$(echo 'rg --files ~/.subfleet')\"", S_), ("rg foo <&0", H_), ("rg foo 0<&0", H_),
]
R5_FORMS_ALLOWED = [
    ("echo 'rg foo src' | cat | bash", S_), ("printf 'rg foo %s\\n' src | bash", S_), ("sh -c '\"$@\"' sh rg foo src", S_),
    ("echo src | xargs -0 bash -c 'rg foo \"$1\"' sh", S_), ('eval "$(ssh-agent -s)"', S_), ("rg foo <&0", S_),
    ("rg foo <&3", H_), ("ls -la =(echo x)", S_),
]


def r5_denied(cases) -> list:
    """The (command, cwd) cases the hook does not deny as [unscoped-search] (empty when every one is)."""
    outs = judge_all(HOOK, cases)
    return [case for case in cases if rule(outs[case]) != "unscoped-search"]


def r5_allowed(cases) -> list:
    """The (command, cwd) cases the hook does not allow (empty when every one is)."""
    outs = judge_all(HOOK, cases)
    return [case for case in cases if rule(outs[case]) is not None]


def test_r5_heredoc_bodies_are_data():
    """Decision 2: review r4's P2-2 inputs and the control pair are denied; a routine commit, a body no shell
    runs and an unterminated heredoc are allowed."""
    assert r5_denied(R5_HEREDOC_DENIED + R5_HEREDOC_CONTROLS) == []
    assert r5_allowed(R5_HEREDOC_ALLOWED) == []


def test_r5_a_depth_bound_covers_only_its_own_walk():
    """Decision 3: a bounded producer's roots feed its xargs consumer, and -exec's command is judged on its own
    with {} as find's roots; a bound on the consumer, or a scoped producer, stays allowed."""
    assert r5_denied([(c, S_) for c in R5_BOUND_DENIED]) == []
    assert r5_allowed(R5_BOUND_ALLOWED) == []


def test_r5_a_closing_backtick_ends_a_root():
    """Decision 5: review r4's six forms and its two controls are denied; prose with escaped backticks is not."""
    assert r5_denied([(c, S_) for c in R5_BACKTICK_DENIED + R5_BACKTICK_CONTROLS]) == []
    assert r5_allowed(R5_BACKTICK_ALLOWED) == []


def test_r5_a_component_bash_can_match_against_dotdot_names_its_parent():
    """Decision 6: .*, .?, .[.] and .[!a] read as their folder and its parent, in every context; .*rc, a
    bracket first and a parent that is no root stay allowed."""
    assert r5_denied([(c, S_) for c in R5_DOTDOT_DENIED]) == []
    assert r5_allowed(R5_DOTDOT_ALLOWED) == []


def test_r5_a_command_too_long_to_prep_fails_closed():
    """Decision 8: over 64 KB and naming a searcher, the command is denied and the message says why; a long
    command that names none is allowed."""
    outs = judge_all(HOOK, [(c, S_) for c in R5_TOO_LONG_DENIED])
    for c in R5_TOO_LONG_DENIED:
        assert rule(outs[(c, S_)]) == "unscoped-search"
        assert "too long to judge" in json.loads(outs[(c, S_)])["hookSpecificOutput"]["permissionDecisionReason"]
    assert rule(run_hook(HOOK, f"echo {R5_LONG}")) is None


def test_r5_a_root_spelling_before_a_brace_is_a_path_word():
    """Decision 9: ~, $HOME and the rest right before { are path words; groups after no root spelling are not."""
    assert r5_denied([(c, S_) for c in R5_BRACE_DENIED]) == []
    assert r5_allowed(R5_BRACE_ALLOWED) == []


def test_r5_the_wider_user_and_volume_family():
    """Decision 10: ~ followed by $, ${, $( or a backtick is the home; /Volumes/<glob>/.. is / as well."""
    assert r5_denied([(c, S_) for c in R5_FAMILY_DENIED]) == []
    assert r5_allowed(R5_FAMILY_ALLOWED) == []


def test_r5_the_unjudged_forms_are_judged():
    """Decision 11: =cmd, pipes into a shell through any stage (printf's %s in order), positional -c scripts,
    eval of a command substitution and <&0 from the home; their scoped forms stay allowed."""
    assert r5_denied(R5_FORMS_DENIED) == []
    assert r5_allowed(R5_FORMS_ALLOWED) == []


def test_r5_scoped_searches_stay_allowed():
    """Review r4's check 3, rerun: r3's 47 scoped searches in a lane worktree, scratch and /tmp, the 12 roots in
    pattern position, the 13 must-allow cases, r4's must-allow forms, the print-and-exit forms, and a routine
    lane commit with an apostrophe in its message."""
    cases = ([(c, cwd) for cwd in R4_SCOPED_CWDS for c in R4_SCOPED] + [(c, LANE_W) for c in R4_PATTERN_ROOTS]
             + R4_MUST_ALLOW + R4_ALLOWED + REVIEW_R4_ALLOWED + [("git commit -F - <<'EOF'\nIt's fixed\nEOF", LANE_W)])
    assert len(R4_SCOPED) * len(R4_SCOPED_CWDS) >= 47 and len(R4_PATTERN_ROOTS) == 12 and len(R4_MUST_ALLOW) == 13
    assert r5_allowed(cases) == []


# ---------------------------------------------------------------- r5: cost (decision 1)

#: A 60,000-character word with no slash beside a crawl and beside a scoped search, then review r4's seven timing
#: rows: (name, command, denied). Each must finish under R5_TIMING_S, or within twice the installed hook's time.
R5_TIMING_S = 3.0
R5_X = "x" * 60000
R5_TIMING = [
    ("60,000 x beside a crawl", f"echo {R5_X}; rg --files ~/.subfleet", True),
    ("60,000 x beside rg foo src", f"echo {R5_X}; rg foo src", False),
    ("65,000 x, scoped", f"echo {'x' * 65000}; rg foo src", False),
    ("30,000 x beside a crawl", f"echo {'x' * 30000}; rg --files ~/.subfleet", True),
    ("'60,000 x', scoped", f"echo '{R5_X}'; rg foo src", False),
    ('"60,000 x", scoped', f'echo "{R5_X}"; rg foo src', False),
    ("`echo 60,000 x`, scoped", f"echo `echo {R5_X}`; rg foo src", False),
    ("4,000 words in a commit message, scoped", 'git commit -m "' + "word " * 4000 + '" && rg foo src', False),
    ("a 70,000-character word beside a crawl", "rg --files ~/.subfleet " + "x" * 70000, True),
]


def test_r5_a_long_word_without_a_slash_costs_what_the_installed_hook_does(hooks):
    """Decision 1: each row keeps its verdict and finishes under 3 s, or within twice the installed hook's time
    (hooks["old"], a60d1c51...); one run each, the new hook first, at the host's load (reported)."""
    rows = {}
    for name, command, denied in R5_TIMING:
        t, out = hook_seconds(HOOK, command)
        assert (rule(out) == "unscoped-search") == denied, name
        old = hook_seconds(hooks["old"], command)[0]
        rows[name] = {"new": round(t, 2), "installed": round(old, 2), "load": round(os.getloadavg()[0], 1)}
        assert t < R5_TIMING_S or t <= 2 * old, (name, rows[name])
    report("timing-r5", rows)


# ---------------------------------------------------------------- r5: the overlay doc (decision 4)

def test_r5_the_overlay_doc_holds_one_copy_of_its_body():
    """Decision 4: every heading of docs/private-guard-overlay.md is there once, the sentence r4's decision 4
    removed is nowhere, and the reviewed pair's table (this hook's hash) sits once, in the policy revision."""
    text = (REPO / "docs/private-guard-overlay.md").read_text(encoding="utf-8")
    heads = [line for line in text.splitlines() if line.startswith("#")]
    assert len(heads) == len(set(heads)), [h for h in heads if heads.count(h) > 1]
    flat = " ".join(text.split())
    assert "searcher's name after a quote, a backslash or a path is detected anywhere in the text" not in flat
    assert text.count(NEW_HOOK_SHA256) == 1
    revision = text.index("\n## Policy revision")
    assert revision < text.index(NEW_HOOK_SHA256) < text.index("\n## ", revision + 1)


# ---------------------------------------------------------------- r5: the real shells

#: r5's stubs: find records its argv and runs the real /usr/bin/find (over the fixture), so a bounded producer's
#: output reaches its consumer; git and python3 do nothing.
R5_FIND = r"""#!/bin/sh
f=$(/usr/bin/mktemp "$R4_CALLS/call.XXXXXX")
printf '%s\0' find "$PWD" none "$@" > "$f"
exec /usr/bin/find "$@"
"""
R5_NOOP = "#!/bin/sh\nexit 0\n"


def r5_walked_paths(name: str, cwd: str, stdin: str, args: list[str]) -> list[str]:
    """r4_walked_paths, and a depth bound in the searcher's own arguments (find's before an -exec) walks
    nothing past it: the rule's escape, so the stubs count only unbounded walks."""
    own = args
    if name == "find":
        cut = [i for i, a in enumerate(args) if a in ("-exec", "-execdir", "-ok", "-okdir")]
        own = args[:cut[0]] if cut else args
        if "-maxdepth" in own:
            return []
    elif any(re.fullmatch(r"--?max-?depth(=.*)?|-d[0-9]+", a) for a in own):
        return []
    return r4_walked_paths(name, cwd, stdin, args)


def r5_shell_walks(shell: str, command: str, cwd: str, tmp: Path) -> bool:
    """Whether ``command``, run by the real shell over the fixture with r5's stubs (r4's searchers and wrappers,
    R5_FIND, git and python3 as no-ops), would have walked a root. /Volumes is the fixture's too, and any file a
    pin writes in its cwd is removed afterwards."""
    fs = str(_FS["root"])
    stubs = tmp / "stubs-r5"
    if not stubs.exists():
        stubs.mkdir()
        for name in R4_SEARCHERS:
            (stubs / name).write_text(R5_FIND if name == "find" else R4_STUB)
        for name in ("timeout", "gtimeout", "doas", "sudo"):
            (stubs / name).write_text(R4_WRAP)
        for name in ("git", "python3"):
            (stubs / name).write_text(R5_NOOP)
        for p in stubs.iterdir():
            p.chmod(0o755)
    calls = tmp / f"calls-r5-{shell}-{next(R4_RUNS)}"
    calls.mkdir()
    text = command.replace("/usr/bin/find", f"{stubs}/find").replace("/usr/bin/grep", f"{stubs}/grep")
    text = text.replace("/opt/homebrew/bin/rg", f"{stubs}/rg").replace("/usr/bin/rg", f"{stubs}/rg")
    text = re.sub(r"(?<![\w./~-])/Users/tester", fs + "/Users/tester", text)
    text = re.sub(r"(?<![\w./~-])/(tmp|Volumes)/", fs + r"/\1/", text)
    text = re.sub(r"(?<=\s)/(?=\s|$)", fs + "/", text)
    # (LOGNAME as a lane carries it: review r4 found the ~$LOGNAME forms unwalked only for its absence)
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": fs + FAKE_HOME, "R4_CALLS": str(calls),
           "USER": os.environ.get("USER", "nobody"), "LOGNAME": os.environ.get("USER", "nobody"), "LC_ALL": "C",
           "TMPDIR": str(tmp)}
    argv = ["/bin/bash", "--noprofile", "--norc", "-c", text] if shell == "bash" else ["/bin/zsh", "-f", "-c", text]
    here = Path(fs + cwd)
    before = set(os.listdir(here))
    try:
        with open(os.devnull) as devnull:
            subprocess.run(argv, stdin=devnull, capture_output=True, env=env, cwd=here, timeout=120)
    finally:
        for new in set(os.listdir(here)) - before:
            if (here / new).is_file() and not (here / new).is_symlink():
                (here / new).unlink()
    walked = []
    for f in sorted(calls.iterdir()):
        name, at, stdin, *args = f.read_bytes().decode().split("\0")[:-1]
        walked += [r4_canonical(p, at) for p in r5_walked_paths(name, at, stdin, args)]
    # (a path holding a newline, as xargs -0 hands on echo's, names no root, and REFERENCE_SH reads by line)
    walked = [p for p in walked if "\n" not in p]
    return any(reference_cached(walked, FAKE_HOME)) if walked else False


#: Every r5 command pin and must-allow form a shell can run: (command, cwd).
R5_SHELL_COMMANDS = (R5_HEREDOC_DENIED + R5_HEREDOC_CONTROLS + R5_HEREDOC_ALLOWED
                     + [(c, S_) for c in R5_BOUND_DENIED] + R5_BOUND_ALLOWED
                     + [(c, S_) for c in R5_BACKTICK_DENIED + R5_BACKTICK_CONTROLS] + R5_BACKTICK_ALLOWED
                     + [(c, S_) for c in R5_DOTDOT_DENIED] + R5_DOTDOT_ALLOWED
                     + [(c, S_) for c in R5_TOO_LONG_DENIED] + [(f"echo {R5_LONG}", S_)]
                     + [(c, S_) for c in R5_BRACE_DENIED] + R5_BRACE_ALLOWED
                     + [(c, S_) for c in R5_FAMILY_DENIED] + R5_FAMILY_ALLOWED
                     + R5_FORMS_DENIED + [case for case in R5_FORMS_ALLOWED if "ssh-agent" not in case[0]]
                     + [(R5_TIMING[0][1], S_), (R5_TIMING[1][1], S_)])
#: The runs the hook judges by design, not by what the stubs saw: (command, the hook's verdict).
R5_SHELL_EXCEPTIONS = {
    # the fixture's /Volumes/Macintosh HD is a folder, not the boot volume's link to /: by name, its .. is /
    "rg --files /Volumes/M*/../Users/tester/.subfleet": True,
}


def test_r5_command_pins_agree_with_real_shells(tmp_path):
    """Each r5 command pin and must-allow form, run by /bin/zsh -f and /bin/bash over the fixture with r5's stubs:
    the hook denies it exactly when either shell's run would have walked a root."""
    outs = judge_all(HOOK, R5_SHELL_COMMANDS)
    rows, differ = [], []
    for command, cwd in R5_SHELL_COMMANDS:
        denied = rule(outs[(command, cwd)]) == "unscoped-search"
        shells = {sh: r5_shell_walks(sh, command, cwd, tmp_path) for sh in ("zsh", "bash")}
        rows.append((command, cwd, denied, shells))
        if command in R5_SHELL_EXCEPTIONS:
            if denied != R5_SHELL_EXCEPTIONS[command]:
                differ.append(("exception", command, denied, shells))
        elif denied != any(shells.values()):
            differ.append((command[:200], cwd, denied, shells))
    report("shells-r5", {"cases": len(rows), "denied": sum(r[2] for r in rows),
                         "zsh": sum(r[3]["zsh"] for r in rows), "bash": sum(r[3]["bash"] for r in rows),
                         "both": sum(r[3]["zsh"] and r[3]["bash"] for r in rows),
                         "neither": sum(not r[3]["zsh"] and not r[3]["bash"] for r in rows)})
    assert differ == [], differ[:10]


# ---------------------------------------------------------------- review r5: r5's unpinned readings

#: r5 readings no named test pinned (review r5's probe mutants rv5-execdir-dropped and rv5-wrapper-equals-dropped
#: each flipped only these inputs and passed their decision's test): -execdir, -ok and -okdir end find's own
#: arguments like -exec, a `+` terminator and a {} inside a word are read, every root of find stands for {}, and
#: zsh's =cmd is read after a wrapper as well as first (decisions 3 and 11).
REVIEW_R5_DENIED = [
    r"find . -maxdepth 1 -execdir rg foo ~ \;", r"find . -maxdepth 1 -ok rg foo ~ \;",
    r"find . -maxdepth 1 -okdir rg foo ~ \;", "find . -maxdepth 1 -exec rg foo ~ {} +",
    r"find ~/.subfleet -maxdepth 1 -exec rg foo {}/ \;", r"find src ~/.subfleet -maxdepth 1 -exec rg foo {} \;",
    r"find -L ~/.subfleet -maxdepth 1 -exec rg foo {} \;", "nice =rg --files ~/.subfleet", "sudo =find / -name x",
]


def test_review_r5_pins_r5s_unpinned_readings():
    """Each reading's own input is denied. In review r5's probe both real shells walked a root for the -execdir,
    `+`, {}/, two-root and -L forms; -ok and -okdir ask first, and =cmd is zsh's."""
    assert r5_denied([(c, S_) for c in REVIEW_R5_DENIED]) == []


# ---------------------------------------------------------------- r6: review r5's findings

# Round r6 (2026-10-28) builds the hub's decisions on review r5 (REQUEST_CHANGES at e02a22fb6). Each pin below is
# one of review r5's inputs (or the brief's), judged through the whole hook in the fixture:
#   1. the $(...) and backtick spans of a heredoc body whose delimiter word holds no quote and no backslash run,
#      so each is judged as a command; a quoted or escaped delimiter keeps the whole body data;
#   2. xargs running sh, bash, zsh or dash with a -c script that names a searcher is judged by the xargs rule;
#   3. any producer's heredoc or here-string piped into a shell given no script is that shell's script;
#   4. a << inside $((...)) or ((...)) is a shift, and a << whose body never ends disables only itself;
#   5. a $( inside double quotes ends where a heredoc-aware parend says;
#   6. zsh's =cmd right after a backtick passes the prefilter and pw_named;
#   7. a searcher in a markdown code span in a body no shell runs stays denied: an intended over-block;
#   8. a searcher beside more than 4,096 of one of $, # or \ is denied unread, fast;
#   9. a <(...) read as a shell's script, and a for or while-read loop variable as a searcher's path operand.
R6_DQ_HD = "git commit -m \"$(cat <<'EOF'\nsay \"hi\nEOF\n)\""
#: Decision 1 (review r5's P2-1): an unquoted delimiter, so each substitution in the body runs. (command, cwd).
R6_BODY_DENIED = [
    "cat > x.md <<EOF\n$(/usr/bin/find / -name x)\nEOF", "cat > x.md <<EOF\n$(\\rg --files ~/.subfleet)\nEOF",
    "cat > x.md <<EOF\n`/usr/bin/find / -name x`\nEOF", "git commit -F - <<EOF\nfix: $(sudo /usr/bin/find / -name x)\nEOF",
    "x=$(cat <<EOF\n$(/usr/bin/find / -name x)\nEOF\n)", "cat <<EOF > x.md\n${HOME}\n$(timeout 60 /usr/bin/find / -name x)\nEOF",
]
#: ... <<- with tabs is unquoted too ...
R6_BODY_MORE_DENIED = ["cat > x.md <<-EOF\n\t$(/usr/bin/find / -name x)\n\tEOF"]
#: ... and a quoted or escaped delimiter keeps the body data: each of the six with <<'EOF', the other quotings, and
#: an unquoted body holding only parameter expansions or a substitution that names no searcher.
R6_BODY_ALLOWED = ([c.replace("<<EOF", "<<'EOF'") for c in R6_BODY_DENIED]
                   + ["cat > x.md <<\"EOF\"\n$(/usr/bin/find / -name x)\nEOF",
                      "cat > x.md <<\\EOF\n$(/usr/bin/find / -name x)\nEOF",
                      "cat > x.md <<E\"O\"F\n$(/usr/bin/find / -name x)\nEOF",
                      "cat > x.md <<EOF\nit's $HOME and ${USER}\nEOF",
                      "git commit -F - <<EOF\nfix: $(git rev-parse --short HEAD)\nEOF",
                      "cat > x.md <<EOF\nit's $(git rev-parse HEAD)\nEOF\nrg foo src"])
#: Decision 2 (review r5's P2-2), and a root right before the xargs in quotes.
R6_XARGS_DENIED = [
    "find ~/.subfleet/jobs -maxdepth 1 -type d | xargs -I{} sh -c 'rg -l foo {}'",
    "find ~/.subfleet/jobs -maxdepth 1 -type d | xargs -I{} bash -c 'cd {} && rg foo'",
    "echo ~/.subfleet | xargs -I{} sh -c 'rg --files \"$1\"' _ {}",
    "find ~/.subfleet -maxdepth 1 -type d -print0 | xargs -0 -n1 sh -c 'rg foo \"$0\"'",
    "echo \"$HOME/.subfleet\" | xargs -I{} zsh -c 'rg --files {}'",
    "printf '%s\\n' ~/.subfleet | xargs -n1 dash -c 'find \"$0\" -name x'",
]
R6_XARGS_ALLOWED = ["find src -type f | xargs -I{} sh -c 'rg -l foo \"$1\"' _ {}",
                    "find src -type f | xargs -I{} sh -c 'rg -l foo {}'",
                    "git ls-files | xargs -n1 sh -c 'echo \"$0\"'"]
#: Decision 3 (review r5's P3-1).
R6_FILTER_DENIED = ["tee <<'EOF' | bash\n/usr/bin/find / -name x\nEOF", "sed -n p <<'EOF' | bash\n\\rg --files ~/.subfleet\nEOF",
                    "tee <<< '/usr/bin/find / -name x' | bash"]
R6_FILTER_ALLOWED = ["tee <<'EOF' | bash\nrg foo src\nEOF", "sed -n p <<'EOF' | sort\n\\rg --files ~/.subfleet\nEOF"]
#: Decision 4 (review r5's P3-2), a shift whose right operand is also a later line, and the (( )) command.
R6_ARITH_PRE = "cat > /dev/null <<'EOF'\nit's\nEOF\n/usr/bin/find / -name x"
R6_ARITH_DENIED = ["echo $((1<<2)); " + R6_ARITH_PRE, "x=$((1 << 3)); " + R6_ARITH_PRE, "((y = 1<<2)); " + R6_ARITH_PRE,
                   "echo $((1<<X))\n\\rg --files ~/.subfleet\nX"]
R6_ARITH_ALLOWED = ["echo $((1<<2))", "echo $((1<<2)); rg foo src", "x=$((1 << 3)); " + R6_ARITH_PRE.replace("/usr/bin/find /", "find src")]
#: ... and decision 4's second half: a << whose body never ends disables only itself, so a later heredoc is still
#: read. (No shell walks this one: the unterminated body swallows the rest, so nothing after it runs. Reading the
#: rest as commands is r5's fail-closed reading of an unterminated heredoc.)
R6_UNTERMINATED_DENIED = ["cat <<NOPE; " + R6_ARITH_PRE, "cat <<NOPE\n" + R6_ARITH_PRE]
#: Decision 5 (review r5's P3-3): the standard commit idiom with a lone " in its body, then a crawl; and a crawl
#: inside the substitution after its heredoc.
R6_DQ_DENIED = [R6_DQ_HD + "\n/usr/bin/find / -name x",
                "echo \"$(cat <<'EOF'\nsay \"hi\nEOF\n\\rg --files ~/.subfleet)\""]
R6_DQ_ALLOWED = [R6_DQ_HD, "git commit -m \"$(cat <<'EOF'\nsay \"quoted\" word\nEOF\n)\"", R6_DQ_HD + "\nrg foo src",
                 "git commit -m \"$(cat <<'EOF'\nsay \"quoted\" word\nEOF\n)\"\nrg foo src"]
#: Decision 6 (review r5's P3-4).
R6_EQ_DENIED = ["echo `=rg --files ~/.subfleet`", "x=`=find / -name x`"]
#: Decision 7 (review r5's P3-5): an INTENDED over-block. Neither shell runs a body under a quoted delimiter, but a
#: searcher in a markdown code span there is read by the text rules, and r5's decision 5 ends its root at the
#: closing backtick. Review r5's rescoping edit does not rescope it: without the backticks (or with the path quoted)
#: the text rules still read rg --files ~/.subfleet in the body. What does: the root outside the searcher's span.
R6_SPAN_DENIED = ["cat > notes.md <<'EOF'\nRun `cd repo && rg --files ~/.subfleet` to see\nEOF"]
R6_SPAN_STILL_DENIED = ["cat > notes.md <<'EOF'\nRun cd repo && rg --files ~/.subfleet to see\nEOF",
                        "cat > notes.md <<'EOF'\nRun `cd repo && rg --files '~/.subfleet'` to see\nEOF"]
R6_SPAN_ALLOWED = ["cat > notes.md <<'EOF'\nRun `rg --files` in `~/.subfleet` to see\nEOF"]
#: Decision 8 (review r5's P3-6): beside a crawl, each is denied unread, fast; the cap's edges and two controls.
R6_HOSTILE = {
    "60,000 $": "rg --files ~/.subfleet " + "$" * 60000,
    "60,000 #": "rg --files ~/.subfleet x" + "#" * 60000,
    "30,000 backslash pairs": "rg --files ~/.subfleet " + "\\\\" * 30000,
}
R6_CAP_DENIED = ["rg foo src " + "$" * 4097, "rg foo src x" + "#" * 4097, "rg foo src " + "\\" * 4097]
R6_CAP_ALLOWED = ["echo " + "$" * 5000, "rg '" + "\\" * 200 + "' src", "rg foo src " + "$" * 4096,
                  "rg foo src x" + "#" * 4096, "rg '" + "\\" * 4096 + "' src"]
#: Decision 9 (review r5's P3-7): a process substitution a shell or source reads ...
R6_PSUB_DENIED = ["bash <(echo 'rg --files ~/.subfleet')", "source <(echo '/usr/bin/find / -name x')",
                  "sh <(echo 'rg --files ~/.subfleet')", ". <(printf '%s\\n' '/usr/bin/find / -name x')",
                  "bash < <(echo 'rg --files ~/.subfleet')"]
R6_PSUB_ALLOWED = ["bash <(echo 'rg foo src')", "diff <(rg foo src) <(rg foo tests)", "source <(echo 'export X=1')"]
#: ... and a loop variable that is a searcher's path operand.
R6_LOOP_DENIED = ["for d in ~/.subfleet/*; do rg foo \"$d\"; done",
                  "find ~/.subfleet -maxdepth 1 | while read d; do rg foo \"$d\"; done",
                  "for d in ~/.subfleet/jobs; do rg --files ${d}; done",
                  "echo ~/.subfleet | while read -r d; do grep -r x $d; done"]
R6_LOOP_ALLOWED = ["for f in src/*.py; do rg -n foo \"$f\"; done",
                   "find src -name '*.py' | while read f; do rg -n foo \"$f\"; done",
                   "for d in src tests; do rg --files \"$d\"; done"]
#: Review r5's check 3 kept 18 lane commands (17 allowed); its list went with its scratch folder, so these are 18
#: of the same kinds, from a lane worktree: commits and a PR body by heredoc, cat > file <<EOF then a scoped rg,
#: find ... -exec rg -l ... +, xargs, loops and arithmetic. The one denial is decision 7's intended over-block.
R6_LANE_ALLOWED = [
    "git status --short && git log --oneline -5",
    "git commit -F - <<'EOF'\nfix: guard heredoc reading\n\nIt's the r6 round.\nEOF",
    "git commit -m \"$(cat <<'EOF'\nfix: read \"quoted\" words\nEOF\n)\"",
    "gh pr create --title 'guard r6' --body \"$(cat <<'EOF'\n## Summary\n- it's the r6 round\nEOF\n)\"",
    "cat > notes.md <<EOF\nHead: $(git rev-parse --short HEAD)\nEOF\nrg -n heredoc src",
    "cat > plan.md <<'EOF'\nit's done\nEOF\nrg -l foo subfleet tests",
    "find . -name '*.py' -exec rg -l pw_named {} +",
    "rg -n 'def test_r6' tests/unit/test_guard_subfleet_root.py",
    "grep -rn hdbody private/guard/",
    "find tests -maxdepth 2 -name '*.py' | xargs rg -l guard",
    "git ls-files | xargs rg -l never-rules",
    "uv run --no-sync pytest -q tests/unit/test_guard_trust.py",
    "for f in tests/unit/test_guard_*.py; do rg -c def \"$f\"; done",
    "git diff --stat origin/main...HEAD | tail -1",
    "rg --files subfleet | head -20",
    "echo $((3 + 4)); rg -n 'x <<' private/guard/never-rules-hook.sh",
    "python3 - <<'EOF'\nimport json\nprint(json.dumps({'a': 1}))\nEOF",
]
R6_LANE_DENIED = R6_SPAN_DENIED


def test_r6_an_unquoted_heredoc_bodys_substitutions_are_judged():
    """Decision 1: review r5's six P2-1 inputs (and <<-) are denied; with a quoted or escaped delimiter, or with
    only parameter expansions or a substitution that names no searcher, the body is data and they are allowed."""
    assert r5_denied([(c, S_) for c in R6_BODY_DENIED + R6_BODY_MORE_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R6_BODY_ALLOWED]) == []


def test_r6_xargs_into_a_shell_whose_script_names_a_searcher():
    """Decision 2: the xargs rule judges the whole command, a bounded producer's roots included; scoped producers
    and a script that names no searcher stay allowed."""
    assert r5_denied([(c, S_) for c in R6_XARGS_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R6_XARGS_ALLOWED]) == []


def test_r6_any_producers_heredoc_piped_into_a_shell_is_its_script():
    """Decision 3: tee's and sed's heredoc (and a here-string) piped into bash are bash's script."""
    assert r5_denied([(c, S_) for c in R6_FILTER_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R6_FILTER_ALLOWED]) == []


def test_r6_an_arithmetic_shift_starts_no_heredoc():
    """Decision 4: a << inside $((...)) or ((...)) is a shift, so a later heredoc is still read (and a shift's
    operand that matches a later line hides nothing); a scoped search beside one stays allowed."""
    assert r5_denied([(c, S_) for c in R6_ARITH_DENIED + R6_UNTERMINATED_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R6_ARITH_ALLOWED]) == []


def test_r6_a_substitution_in_double_quotes_reads_its_heredoc():
    """Decision 5: the commit idiom's body may hold a lone \" without hiding what follows."""
    assert r5_denied([(c, S_) for c in R6_DQ_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R6_DQ_ALLOWED]) == []


def test_r6_zsh_equals_cmd_after_a_backtick_is_read():
    """Decision 6: =rg and =find right after an opening backtick."""
    assert r5_denied([(c, S_) for c in R6_EQ_DENIED]) == []


def test_r6_a_markdown_code_span_in_a_quoted_body_is_an_intended_over_block():
    """Decision 7: INTENDED. No shell runs the body, but the text rules read the span's searcher and its root ends
    at the closing backtick (r5's decision 5), so it is denied, as it is without the backticks or with the path
    quoted (the text rules read the body); with the root outside the searcher's span it is allowed."""
    assert r5_denied([(c, S_) for c in R6_SPAN_DENIED + R6_SPAN_STILL_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R6_SPAN_ALLOWED]) == []


def test_r6_hostile_words_fail_closed_fast(hooks):
    """Decision 8: each hostile word beside a crawl is denied as too complex to judge in under 3 s, or within twice
    the installed hook's time (one run each, at the host's load, reported); 4,097 of one character beside a scoped
    search is denied too, and 4,096, a 5,000-$ echo and a 200-backslash pattern are allowed."""
    outs = judge_all(HOOK, [(c, S_) for c in R6_CAP_DENIED + R6_CAP_ALLOWED])
    for c in R6_CAP_DENIED:
        # (review r7: an allow fails here as an assertion, so r6-cap-dropped is a clean kill, not a JSONDecodeError)
        assert rule(outs[(c, S_)]) == "unscoped-search", c[:40]
        assert "too complex to judge" in json.loads(outs[(c, S_)])["hookSpecificOutput"]["permissionDecisionReason"]
    assert [c[:40] for c in R6_CAP_ALLOWED if rule(outs[(c, S_)]) is not None] == []
    rows = {}
    for name, command in R6_HOSTILE.items():
        t, out = hook_seconds(HOOK, command)
        assert rule(out) == "unscoped-search", name
        assert "too complex to judge" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"], name
        old = hook_seconds(hooks["old"], command)[0]
        rows[name] = {"new": round(t, 2), "installed": round(old, 2), "load": round(os.getloadavg()[0], 1)}
        assert t < R5_TIMING_S or t <= 2 * old, (name, rows[name])
    report("hostile-r6", rows)


#: The step budget decisions 4 and 5 share (a deviation, see the report): a dense run of $( or (( made each read
#: walk the rest of the command (r5's own hook passed 90 s on 2,000 $( in double quotes). Each is denied as too
#: complex to judge, beside a scoped search, in under 3 s of wall time or within twice the installed hook's child
#: CPU (decision 10's measure: in round r6's hold H1 one row took 3.5 s of wall against the installed hook's 1.5 s
#: at load 110, while r6, r5 and the installed hook took 0.19, 0.21 and 0.15 s of CPU on it).
R6_DENSE = {
    "2,000 $( in double quotes": "rg foo src; echo \"" + "$(" * 2000 + "\"",
    "2,000 ( before a <<": "(" * 2000 + " <<x; rg foo src",
    "1,000 (( after a search": "rg foo src; " + "((" * 1000 + " <<x",
}


def test_r6_dense_substitutions_fail_closed_fast(hooks):
    """The shared step budget: each dense form is denied as too complex to judge, fast: under 3 s of wall time or
    within twice the installed hook's child CPU (one run each, both reported)."""
    rows = {}
    for name, command in R6_DENSE.items():
        cpu, wall, out = hook_cpu(HOOK, command)
        assert rule(out) == "unscoped-search", name
        assert "too complex to judge" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"], name
        old_cpu, old_wall, _ = hook_cpu(hooks["old"], command)
        rows[name] = {"cpu": [round(cpu, 2), round(old_cpu, 2)], "wall": [round(wall, 2), round(old_wall, 2)],
                      "load": round(os.getloadavg()[0], 1)}
        assert wall < R5_TIMING_S or cpu <= 2 * old_cpu, (name, rows[name])
    report("dense-r6", rows)


def test_r6_process_substitutions_and_loops_are_judged():
    """Decision 9: a <(...) a shell, source or . reads is judged as its script; a for loop's words and a piped
    while-read's producer roots stand for the loop variable as a searcher's operand; scoped forms stay allowed."""
    assert r5_denied([(c, S_) for c in R6_PSUB_DENIED + R6_LOOP_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R6_PSUB_ALLOWED + R6_LOOP_ALLOWED]) == []


def test_r6_scoped_searches_stay_allowed():
    """The must-allow lists (r5's, with review r4's allowed forms and the routine commit), each r6 decision's allowed
    forms, the print-and-exit forms from any cwd, and 18 lane commands from a lane worktree (17 allowed; the one
    denial is decision 7's intended over-block)."""
    cases = ([(c, cwd) for cwd in R4_SCOPED_CWDS for c in R4_SCOPED] + [(c, LANE_W) for c in R4_PATTERN_ROOTS]
             + R4_MUST_ALLOW + R4_ALLOWED + REVIEW_R4_ALLOWED + [("git commit -F - <<'EOF'\nIt's fixed\nEOF", LANE_W)]
             + R5_HEREDOC_ALLOWED + R5_BOUND_ALLOWED + R5_BACKTICK_ALLOWED + R5_DOTDOT_ALLOWED + R5_BRACE_ALLOWED
             + R5_FAMILY_ALLOWED + R5_FORMS_ALLOWED + [(c, cwd) for cwd in (H_, "/", S_) for c in R4_META]
             + [(c, LANE_W) for c in R6_LANE_ALLOWED])
    assert len(R6_LANE_ALLOWED) + len(R6_LANE_DENIED) == 18
    assert r5_allowed(cases) == []
    assert r5_denied([(c, LANE_W) for c in R6_LANE_DENIED]) == []


# ---------------------------------------------------------------- r6: the real shells

#: r6's find stub: it records its argv and runs the real /usr/bin/find only when every start path is a folder inside
#: the fixture (R6_FS), so no run lists a real folder; git and python3 do nothing.
R6_FIND = r"""#!/bin/sh
f=$(/usr/bin/mktemp "$R4_CALLS/call.XXXXXX")
printf '%s\0' find "$PWD" none "$@" > "$f"
lead=1
for a in "$@"; do
  if [ "$lead" = 1 ]; then case "$a" in -[HLPEXdsx]|-O*) continue ;; esac; lead=0; fi
  case "$a" in -*|'('|'!') break ;; esac
  r=$(cd "$a" 2>/dev/null && pwd -P) || exit 0
  case "$r" in "$R6_FS"|"$R6_FS"/*) ;; *) exit 0 ;; esac
done
r=$(pwd -P)
case "$r" in "$R6_FS"|"$R6_FS"/*) ;; *) exit 0 ;; esac
exec /usr/bin/find "$@"
"""


def r6_shell_walks(shell: str, command: str, cwd: str, tmp: Path) -> bool:
    """r5_shell_walks with R6_FIND: whether ``command``, run by the real shell over the fixture with HOME the
    fixture's home, would have walked a root."""
    fs = str(_FS["root"])
    stubs = tmp / "stubs-r6"
    if not stubs.exists():
        stubs.mkdir()
        for name in R4_SEARCHERS:
            (stubs / name).write_text(R6_FIND if name == "find" else R4_STUB)
        for name in ("timeout", "gtimeout", "doas", "sudo"):
            (stubs / name).write_text(R4_WRAP)
        for name in ("git", "python3", "gh"):
            (stubs / name).write_text(R5_NOOP)
        for p in stubs.iterdir():
            p.chmod(0o755)
    calls = tmp / f"calls-r6-{shell}-{next(R4_RUNS)}"
    calls.mkdir()
    text = command.replace("/usr/bin/find", f"{stubs}/find").replace("/usr/bin/grep", f"{stubs}/grep")
    text = text.replace("/opt/homebrew/bin/rg", f"{stubs}/rg").replace("/usr/bin/rg", f"{stubs}/rg")
    text = re.sub(r"(?<![\w./~-])/Users/tester", fs + "/Users/tester", text)
    text = re.sub(r"(?<![\w./~-])/(tmp|Volumes)/", fs + r"/\1/", text)
    text = re.sub(r"(?<=\s)/(?=\s|$)", fs + "/", text)
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": fs + FAKE_HOME, "R4_CALLS": str(calls),
           "R6_FS": os.path.realpath(fs), "USER": os.environ.get("USER", "nobody"),
           "LOGNAME": os.environ.get("USER", "nobody"), "LC_ALL": "C", "TMPDIR": str(tmp)}
    argv = ["/bin/bash", "--noprofile", "--norc", "-c", text] if shell == "bash" else ["/bin/zsh", "-f", "-c", text]
    here = Path(fs + cwd)
    before = set(os.listdir(here))
    try:
        with open(os.devnull) as devnull:
            subprocess.run(argv, stdin=devnull, capture_output=True, env=env, cwd=here, timeout=120)
    finally:
        for new in set(os.listdir(here)) - before:
            if (here / new).is_file() and not (here / new).is_symlink():
                (here / new).unlink()
    walked = []
    for f in sorted(calls.iterdir()):
        name, at, stdin, *args = f.read_bytes().decode().split("\0")[:-1]
        walked += [r4_canonical(p, at) for p in r5_walked_paths(name, at, stdin, args)]
    walked = [p for p in walked if "\n" not in p]
    return any(reference_cached(walked, FAKE_HOME)) if walked else False


#: Every r6 command pin and control a shell can run (the hostile words and the cap's edges are left out: their
#: verdicts are the cost cap's, not a walk's), from the scoped cwd.
R6_SHELL_COMMANDS = [(c, S_) for c in (
    R6_BODY_DENIED + R6_BODY_MORE_DENIED + R6_BODY_ALLOWED + R6_XARGS_DENIED + R6_XARGS_ALLOWED + R6_FILTER_DENIED
    + R6_FILTER_ALLOWED + R6_ARITH_DENIED + R6_UNTERMINATED_DENIED + R6_ARITH_ALLOWED + R6_DQ_DENIED + R6_DQ_ALLOWED + R6_EQ_DENIED
    + R6_SPAN_DENIED + R6_SPAN_STILL_DENIED + R6_SPAN_ALLOWED + R6_PSUB_DENIED + R6_PSUB_ALLOWED + R6_LOOP_DENIED + R6_LOOP_ALLOWED)]
#: The runs the hook judges by design, not by what the shells walk: (command, the hook's verdict).
R6_SHELL_EXCEPTIONS = {
    # decision 7: no shell runs a body under a quoted delimiter; the text rules' reading denies it (intended)
    R6_SPAN_DENIED[0]: True, R6_SPAN_STILL_DENIED[0]: True, R6_SPAN_STILL_DENIED[1]: True,
    # decision 4's second half: the unterminated heredoc swallows the rest in both shells; the rest is read as
    # commands (r5's fail-closed reading)
    R6_UNTERMINATED_DENIED[0]: True, R6_UNTERMINATED_DENIED[1]: True,
}


def test_r6_command_pins_agree_with_real_shells(tmp_path):
    """Each r6 command pin and control, run by /bin/zsh -f and /bin/bash over the fixture with r6's stubs and the
    fixture's HOME: the hook denies it exactly when either shell's run would have walked a root."""
    outs = judge_all(HOOK, R6_SHELL_COMMANDS)
    rows, differ = [], []
    for command, cwd in R6_SHELL_COMMANDS:
        denied = rule(outs[(command, cwd)]) == "unscoped-search"
        shells = {sh: r6_shell_walks(sh, command, cwd, tmp_path) for sh in ("zsh", "bash")}
        rows.append((command, cwd, denied, shells))
        if command in R6_SHELL_EXCEPTIONS:
            if denied != R6_SHELL_EXCEPTIONS[command]:
                differ.append(("exception", command, denied, shells))
        elif denied != any(shells.values()):
            differ.append((command[:200], cwd, denied, shells))
    report("shells-r6", {"cases": len(rows), "denied": sum(r[2] for r in rows),
                         "zsh": sum(r[3]["zsh"] for r in rows), "bash": sum(r[3]["bash"] for r in rows),
                         "both": sum(r[3]["zsh"] and r[3]["bash"] for r in rows),
                         "neither": sum(not r[3]["zsh"] and not r[3]["bash"] for r in rows),
                         "rows": [(c[:120], d, s) for c, _, d, s in rows]})
    assert differ == [], differ[:10]


# ---------------------------------------------------------------- review r6: r6's unpinned readings

#: r6 readings no test pinned, from review r6's checks 2 and 5. In review r6's real-shell run over the fixture
#: (/bin/zsh -f and /bin/bash 3.2, r6's stubs) each denied form walked a root in one shell or both, and each allowed
#: form walked nothing. Decision 1: an unquoted body's substitution nested, holding a backtick, after an apostrophe,
#: inside single or double quotes, under a delimiter word holding a $, as a second heredoc of a line, and around a
#: heredoc of its own; $'EOF' is a quoted delimiter and \$( no substitution. Decision 2: xargs -J, -I%, -P, env and
#: an ls or printf producer. Decision 4: $[...], let, for ((...)), nested $((...)) and (( and ( ( subshells.
#: Decision 9: bash <(printf ...), bash -s < <(...), a piped while IFS= read -r -d '' and a loop after sort.
REVIEW_R6_DENIED = [
    "cat > x.md <<EOF\n$(echo $(/usr/bin/find / -name x))\nEOF",
    "cat > x.md <<EOF\n$(echo `\\rg --files ~/.subfleet`)\nEOF",
    "cat > x.md <<EOF\nit's\n$(/usr/bin/find / -name x)\nit's\nEOF",
    "cat > x.md <<EOF\nrun '$(/usr/bin/find / -name x)'\nEOF",
    "cat > x.md <<EOF\nrun \"$(/usr/bin/find / -name x)\n\nEOF",
    "cat > x.md <<E$X\n$(/usr/bin/find / -name x)\nE$X",
    "cat > x.md <<${D}\n$(/usr/bin/find / -name x)\n${D}",
    "cat <<A > a.md; cat <<B > b.md\nfirst\nA\n$(/usr/bin/find / -name x)\nB",
    "cat <<'A' > a.md; cat <<B > b.md\n$(echo hi)\nA\n$(/usr/bin/find / -name x)\nB",
    "cat > x.md <<EOF\n$(cat <<'X'\nit's\nX\n/usr/bin/find / -name x)\nEOF",
    "find ~/.subfleet/jobs -maxdepth 1 -type d | xargs -J % sh -c 'rg -l foo \"$0\"' %",
    "find ~/.subfleet/jobs -maxdepth 1 -type d | xargs -I% sh -c 'rg -l foo %'",
    "find ~/.subfleet -maxdepth 1 -type d | xargs -P4 -n1 bash -c 'rg foo \"$1\"' _",
    "ls -d ~/.subfleet/* | xargs -n1 sh -c 'rg foo \"$0\"'",
    "find ~/.subfleet -maxdepth 1 | xargs -n1 env sh -c 'rg foo \"$0\"'",
    "printf '%s\\n' \"$HOME/.subfleet\" | xargs -I{} sh -c 'rg --files {}'",
    "echo $[1<<2]; cat > /dev/null <<'EOF'\nit's\nEOF\n/usr/bin/find / -name x",
    "let 'x=1<<2'; cat > /dev/null <<'EOF'\nit's\nEOF\n/usr/bin/find / -name x",
    "for ((i=1<<1; i<2; i++)); do :; done; cat > /dev/null <<'EOF'\nit's\nEOF\n/usr/bin/find / -name x",
    "echo $(( (1<<2) + $((3<<1)) )); cat > /dev/null <<'EOF'\nit's\nEOF\n/usr/bin/find / -name x",
    "( (cat <<EOF\nhi\nEOF\n) ); /usr/bin/find / -name x",
    "((cat <<EOF\nhi\nEOF\n) ); /usr/bin/find / -name x",
    "bash <(printf '%s\\n' 'rg --files ~/.subfleet')",
    "bash -s < <(echo 'rg --files ~/.subfleet')",
    "find ~/.subfleet -maxdepth 1 -print0 | while IFS= read -r -d '' d; do rg foo \"$d\"; done",
    "find ~/.subfleet -maxdepth 1 | sort | while read -r d; do rg -l foo \"$d\"; done",
]
REVIEW_R6_ALLOWED = [
    "cat > x.md <<$'EOF'\n$(/usr/bin/find / -name x)\nEOF",
    "cat > x.md <<EOF\n\\$(/usr/bin/find / -name x)\nEOF",
    "cat > x.md <<EOF\n$(rg -c foo src)\nEOF",
    "cat > notes.md <<EOF\nsee ~/.subfleet and $HOME/.subfleet\nEOF\nrg -n foo src",
    "git ls-files -z | xargs -0 sh -c 'rg -l foo \"$@\"' _",
    "cd src && git ls-files | xargs -n1 sh -c 'rg -c foo \"$0\"'",
    "git ls-files | while read -r f; do rg -n foo \"$f\"; done",
    "n=$((1<<4)); echo \"$n\"; rg -c foo src",
]
#: The shared step budget's edge: each $(:) inside double quotes costs 9 parend steps (three reads of 3), and an
#: empty $() 6, so 2,222 of the first are 19,998 steps, read, and 2,221 with two of the second 20,001, too complex.
REVIEW_R6_STEP_EDGE = {"read": "rg foo src; echo \"" + "$(:)" * 2222 + "\"",
                       "unread": "rg foo src; echo \"" + "$(:)" * 2221 + "$()" * 2 + "\""}


def test_review_r6_pins_r6s_unpinned_readings():
    """Each reading's own input: the denied forms are denied, the allowed ones allowed, and the step budget's edge
    reads 19,998 steps and denies 20,001 as too complex to judge."""
    assert r5_denied([(c, S_) for c in REVIEW_R6_DENIED]) == []
    assert r5_allowed([(c, S_) for c in REVIEW_R6_ALLOWED]) == []
    outs = judged(HOOK, list(REVIEW_R6_STEP_EDGE.values()), S_)
    assert rule(outs[REVIEW_R6_STEP_EDGE["read"]]) is None
    unread = json.loads(outs[REVIEW_R6_STEP_EDGE["unread"]])["hookSpecificOutput"]["permissionDecisionReason"]
    assert "too complex to judge" in unread


# ---------------------------------------------------------------- r7: review r6's findings

# Round r7 (2026-10-28) builds the hub's decisions on review r6 (REQUEST_CHANGES at 2bd5b3199, all P3). Each pin is
# review r6's input or the brief's, judged through the whole hook in the fixture:
#   1. parend reads arithmetic: in $((...)), ((...)), $[...] and let '...' a << is a shift, never a heredoc, and the
#      span may cross lines, while a $( or backtick in it is still a command (review r6's P3-1 and P3-2);
#   2. a heredoc body is read a line at a time with no copy of the rest of the command, each line read a step of
#      the shared 20,000-step budget (P3-3);
#   3. what a <(...) writes, a cat, tee or other filter it is a word of passes down the pipe after it, into a shell
#      given no script (P3-4);
#   4. "Known differences" names zsh's =(...), while ... done < <(...) and nested loops, open (P3-6);
#   5. a plain assignment that lasts, after a loop's done, ends the loop variable's reading (P3-5);
#   6. test_r4_hostile_words_fail_closed_fast measures child CPU (P3-7).
R7_F = "/usr/bin/find / -name x"
#: Decision 1: review r6's P3-1 (a loosening against r5) and P3-2 inputs; a shift inside a substitution, inside double
#: quotes, in $[...] and in let '...'; and a $( inside arithmetic, still a command whose heredoc is read.
R7_ARITH_DENIED = [
    "echo \"$((1<<X\n))\"\n" + R7_F + "\nX\ncase x in x) : ;; y) : ;; esac; echo \"a\"",
    "echo $((1<<X\n))\n" + R7_F + "\nX",
    "((y = 1<<X\n))\n" + R7_F + "\nX",
    "x=$(echo $((1<<X\n))\n" + R7_F + "\nX\n)",
    "echo \"$(echo $((1<<X\n)))\"\n" + R7_F + "\nX",
    "echo $[1<<X\n]\n" + R7_F + "\nX",
    "let 'y=1<<X\n'\n" + R7_F + "\nX",
    "echo $(( $(cat <<'EOF'\n1\nEOF\n) << 1 ))\n" + R7_F,
]
R7_ARITH_ALLOWED = ["n=$((1<<4)); echo \"$n\"; rg -c foo src", "echo $((1<<2))", "echo $((1<<X\n))\nrg foo src\nX",
                    "echo $[1<<2]; rg foo src", "x=$(( (1<<2) + $(cat <<EOF\n3\nEOF\n) ))\nrg foo src",
                    "git commit -m \"$(cat <<'EOF'\nfix $((1<<2)) here\nEOF\n)\""]


def r7_heredocs(delimiters: int, lines: int) -> str:
    """Review r6's P3-3 shape: delimiters heredocs on one line inside "$(...)", the first one's body lines, each
    delimiter on its own line after, then a crawl."""
    return ("rg foo src; echo \"$(cat " + " ".join(f"<<D{i}" for i in range(delimiters)) + "\n" + "a\n" * lines
            + "".join(f"D{i}\n" for i in range(delimiters)) + ")\"; " + R7_F)


#: Decision 2's rows: review r6's (43,431 characters; r6 took 17.4 s of CPU on it in review r6, r5 0.5 s), its
#: smaller shape, and the same cost at the top level, where each of 300 delimiters that is never found read every
#: later line (r6 4.8 s of CPU against r5's 1.5 s and the installed hook's 0.55 s, in this round's probe at load 54).
#: Each is denied as too complex to judge, in at most R7_SCAN_CPU_S of child CPU (twice r5's 0.5 s).
R7_SCAN = {
    "300 delimiters, 20,000 lines": r7_heredocs(300, 20000),
    "40 delimiters, 8,000 lines": r7_heredocs(40, 8000),
    "300 missing delimiters, 20,000 lines, top level": ("rg foo src; cat " + " ".join(f"<<N{i}" for i in range(300))
                                                       + "\n" + "a\n" * 20000 + R7_F),
}
R7_SCAN_CPU_S = 1.0
#: ... and the budget's edges for body lines: inside "$(...)" the prep reads a body three times (18 steps and 3 a
#: line), so 6,660 lines are 19,998 steps, read, and 6,661 are 20,001, too complex; at the top level hdbody reads it
#: once (1 step and 1 a line), so 19,999 lines are 20,000 steps, read, and 20,000 are 20,001.
R7_STEP_EDGE = {
    "read": ["rg foo src; echo \"$(cat <<'A'\n" + "a\n" * 6660 + "A\n)\"", "rg foo src; cat <<'A'\n" + "a\n" * 19999 + "A\n"],
    "unread": ["rg foo src; echo \"$(cat <<'A'\n" + "a\n" * 6661 + "A\n)\"", "rg foo src; cat <<'A'\n" + "a\n" * 20000 + "A\n"],
}
#: Decision 3: review r6's three P3-4 inputs and r7's fourth (sort; review r6 names three forms), a second <(...),
#: an earlier stage, a later filter, and a nested <(...).
R7_PSUB_DENIED = [
    "cat <(echo 'rg --files ~/.subfleet') | bash", "cat <(printf '%s\\n' '" + R7_F + "') | sh",
    "tee < <(echo 'rg --files ~/.subfleet') | bash", "sort <(echo 'rg --files ~/.subfleet') | bash",
    "cat <(echo hi) <(echo 'rg --files ~/.subfleet') | bash", "echo x | cat <(echo '" + R7_F + "') | sh",
    "cat <(echo 'rg --files ~/.subfleet') | sort | bash", "cat <(cat <(echo 'rg --files ~/.subfleet')) | bash",
]
R7_PSUB_ALLOWED = ["cat <(git ls-files) | wc -l", "cat <(echo 'rg foo src') | bash",
                   "diff <(rg foo src) <(rg foo tests) | head", "paste <(echo a) <(echo b) | wc -l"]
#: Decision 4: forms the shells walk and this head lets through, listed open in "Known differences" (each with the
#: text the doc names it by): zsh's =(...) (a temporary file, not a <(...)); a while-read loop fed by < <(...) after
#: its done (the read comes before the redirection that feeds it); nested loops (a for list built from a while-read
#: variable).
R7_KNOWN_OPEN = {
    "source =(echo 'rg --files ~/.subfleet')": "source =(echo 'rg --files ~/.subfleet')",
    "zsh =(echo 'rg --files ~/.subfleet')": "zsh =(…)",
    "while read d; do rg foo \"$d\"; done < <(find ~/.subfleet -maxdepth 1)": "done < <(find ~/.subfleet -maxdepth 1)",
    "find ~/.subfleet -maxdepth 1 | while read d; do for f in \"$d\"/*; do rg foo \"$f\"; done; done":
        "for f in \"$d\"/*; do rg foo \"$f\"; done; done",
}
#: Decision 5: review r6's P3-5 over-block, allowed, and where an assignment lasts: a newline, &&, an if around the
#: loop, and a piped while-read's variable ...
R7_LOOPVAR_ALLOWED = [
    "for d in ~/.subfleet/*; do echo \"$d\"; done; d=src; rg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done\nd=src\nrg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done; d=src && rg foo \"$d\"",
    "if true; then for d in ~/.subfleet/*; do :; done; fi; d=src; rg foo \"$d\"",
    "find ~/.subfleet -maxdepth 1 | while read d; do :; done; d=src; rg foo \"$d\"",
]
#: ... and the brief's two denied pins (no assignment; assigned back to a root), a root assigned after a scoped loop,
#: and assignments that do not last or do not come first: after &&, in a subshell or a substitution, before a pipe,
#: before & and as a prefix of the search itself.
R7_LOOPVAR_DENIED = [
    "for d in ~/.subfleet/*; do echo \"$d\"; done; rg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done; rg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done; d=src; rg foo \"$d\"; d=~/.subfleet; rg foo \"$d\"",
    "for d in src; do :; done; d=~/.subfleet; rg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done; false && d=src; rg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done; (d=src); rg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done; x=$(d=src); rg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done; d=src | cat; rg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done; d=src & rg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done; d=src rg foo \"$d\"",
]
#: Decision 5's remaining over-blocks, denied though no shell walks a root, listed "stricter" in "Known differences":
#: an assignment inside a compound command (if, a brace group), after a case statement, of a value holding $, and
#: inside a nested script.
R7_LOOPVAR_STRICTER = [
    "for d in ~/.subfleet/*; do if true; then d=src; fi; done; rg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done; { d=src; }; rg foo \"$d\"",
    "case x in x) ;; esac; for d in ~/.subfleet/*; do :; done; d=src; rg foo \"$d\"",
    "for d in ~/.subfleet/*; do :; done; d=$PWD/src; rg foo \"$d\"",
    "bash -c 'for d in ~/.subfleet/*; do :; done; d=src; rg foo \"$d\"'",
]


def test_r7_an_arithmetic_shift_crosses_lines():
    """Decision 1: a << inside $((...)), ((...)), $[...] or let '...' is a shift even when the span crosses lines, so
    review r6's P3-1 and P3-2 inputs (and the same shapes in a substitution, double quotes, $[...] and let) are
    denied; a $( inside arithmetic is still a command, its heredoc read; scoped forms stay allowed."""
    assert r5_denied([(c, S_) for c in R7_ARITH_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R7_ARITH_ALLOWED]) == []


def test_r7_heredoc_bodies_count_against_the_step_budget(hooks):
    """Decision 2: each row is denied as too complex to judge in at most R7_SCAN_CPU_S of child CPU (decision 10's
    measure; one run each, the installed hook's CPU and the load reported), and the budget's body-line edges are
    exact: read below, too complex above."""
    rows = {}
    for name, command in R7_SCAN.items():
        cpu, wall, out = hook_cpu(HOOK, command)
        assert rule(out) == "unscoped-search", name
        assert "too complex to judge" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"], name
        old_cpu, old_wall, _ = hook_cpu(hooks["old"], command)
        rows[name] = {"cpu": [round(cpu, 2), round(old_cpu, 2)], "wall": [round(wall, 2), round(old_wall, 2)],
                      "load": round(os.getloadavg()[0], 1)}
        assert cpu <= R7_SCAN_CPU_S, (name, rows[name])
    report("scan-r7", rows)
    outs = judged(HOOK, R7_STEP_EDGE["read"] + R7_STEP_EDGE["unread"], S_)
    assert [c[:40] for c in R7_STEP_EDGE["read"] if rule(outs[c]) is not None] == []
    for c in R7_STEP_EDGE["unread"]:
        assert "too complex to judge" in json.loads(outs[c])["hookSpecificOutput"]["permissionDecisionReason"], c[:40]


def test_r7_a_filter_passes_on_its_process_substitution():
    """Decision 3: what a <(...) writes goes down the pipe after the cat, tee, sort or other filter it is a word of,
    into a shell given no script; a pipe into no shell, and a script that names no root, stay allowed."""
    assert r5_denied([(c, S_) for c in R7_PSUB_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R7_PSUB_ALLOWED]) == []


def test_r7_known_differences_name_the_open_forms(tmp_path):
    """Decision 4: each open form is allowed at this head, a real shell walks a root on it, and the overlay doc's
    "Known differences" names it (its text with runs of blanks and line breaks read as one space)."""
    doc = (REPO / "docs/private-guard-overlay.md").read_text(encoding="utf-8")
    start = doc.index("Known differences that remain")
    known = " ".join(doc[start:doc.index("| File | SHA-256 |", start)].split())
    outs = judged(HOOK, list(R7_KNOWN_OPEN), S_)
    for command, named in R7_KNOWN_OPEN.items():
        assert rule(outs[command]) is None, command
        assert any(r6_shell_walks(sh, command, S_, tmp_path) for sh in ("zsh", "bash")), command
        assert named in known, named


def test_r7_a_loop_variable_reassigned_after_its_loop_is_read_as_assigned():
    """Decision 5: a plain assignment that lasts (the top of the command, after ;, a newline, & or nothing and
    before ;, a newline, && or ||, no compound command open) ends a loop variable's reading, and a literal value is
    read in its place; any other assignment leaves the loop's words; the remaining over-blocks are listed."""
    assert r5_allowed([(c, S_) for c in R7_LOOPVAR_ALLOWED]) == []
    assert r5_denied([(c, S_) for c in R7_LOOPVAR_DENIED + R7_LOOPVAR_STRICTER]) == []


def test_r7_scoped_searches_stay_allowed():
    """The must-allow lists (r6's test's cases, with review r4's allowed forms, the print-and-exit forms, the routine
    commit and r6's 17 allowed lane commands), review r6's allowed forms, and each r7 decision's allowed forms."""
    cases = ([(c, cwd) for cwd in R4_SCOPED_CWDS for c in R4_SCOPED] + [(c, LANE_W) for c in R4_PATTERN_ROOTS]
             + R4_MUST_ALLOW + R4_ALLOWED + REVIEW_R4_ALLOWED + [("git commit -F - <<'EOF'\nIt's fixed\nEOF", LANE_W)]
             + R5_HEREDOC_ALLOWED + R5_BOUND_ALLOWED + R5_BACKTICK_ALLOWED + R5_DOTDOT_ALLOWED + R5_BRACE_ALLOWED
             + R5_FAMILY_ALLOWED + R5_FORMS_ALLOWED + [(c, cwd) for cwd in (H_, "/", S_) for c in R4_META]
             + [(c, LANE_W) for c in R6_LANE_ALLOWED] + [(c, S_) for c in REVIEW_R6_ALLOWED]
             + [(c, S_) for c in R7_ARITH_ALLOWED + R7_PSUB_ALLOWED + R7_LOOPVAR_ALLOWED])
    assert r5_allowed(cases) == []


# ---------------------------------------------------------------- r7: the real shells

#: Every r7 command pin and control a shell can run (the cost rows and the budget's edges are left out: their verdicts
#: are the budget's), from the scoped cwd; decision 5's listed over-blocks are denied by design.
R7_SHELL_COMMANDS = [(c, S_) for c in (R7_ARITH_DENIED + R7_ARITH_ALLOWED + R7_PSUB_DENIED + R7_PSUB_ALLOWED
                                      + R7_LOOPVAR_ALLOWED + R7_LOOPVAR_DENIED + R7_LOOPVAR_STRICTER)]
R7_SHELL_EXCEPTIONS = {c: True for c in R7_LOOPVAR_STRICTER}


def test_r7_command_pins_agree_with_real_shells(tmp_path):
    """Each r7 command pin and control, run by /bin/zsh -f and /bin/bash over the fixture with r6's stubs and the
    fixture's HOME: the hook denies it exactly when either shell's run would have walked a root, but for decision
    5's listed over-blocks (denied, and no shell walks)."""
    outs = judge_all(HOOK, R7_SHELL_COMMANDS)
    rows, differ = [], []
    for command, cwd in R7_SHELL_COMMANDS:
        denied = rule(outs[(command, cwd)]) == "unscoped-search"
        shells = {sh: r6_shell_walks(sh, command, cwd, tmp_path) for sh in ("zsh", "bash")}
        rows.append((command, cwd, denied, shells))
        if command in R7_SHELL_EXCEPTIONS:
            if denied != R7_SHELL_EXCEPTIONS[command] or any(shells.values()):
                differ.append(("exception", command, denied, shells))
        elif denied != any(shells.values()):
            differ.append((command[:200], cwd, denied, shells))
    report("shells-r7", {"cases": len(rows), "denied": sum(r[2] for r in rows),
                         "zsh": sum(r[3]["zsh"] for r in rows), "bash": sum(r[3]["bash"] for r in rows),
                         "both": sum(r[3]["zsh"] and r[3]["bash"] for r in rows),
                         "neither": sum(not r[3]["zsh"] and not r[3]["bash"] for r in rows),
                         "rows": [(c[:120], d, s) for c, _, d, s in rows]})
    assert differ == [], differ[:10]


# ---------------------------------------------------------------- review r7: r7's unpinned readings

#: r7 readings no test pinned, from review r7's check 2. In review r7's real-shell run over the fixture (/bin/zsh -f
#: and /bin/bash 3.2, r6's stubs, the fixture's HOME) each denied form walked a root in one shell or both, and each
#: allowed form walked nothing. Decision 1: $[...] inside $((...)) and $((...)) inside $[...] across lines, let with a
#: double-quoted shift, a substitution's heredoc inside ((...)), an arithmetic << on the line of a real heredoc, <<= and
#: a << ending a line inside $((...)); an unquoted let's << is a heredoc. Decision 3: the pass-on through tee's stdin,
#: a later tee or head, xargs -0 sh -c, cat - with a here-string, and zsh's >/dev/null | (MULTIOS). Decision 5: an
#: assignment after ||, in a subshell or a substitution, in a function body never called, in a loop that never runs
#: or in a case arm, a literal root value (/, \/, /*), and two assignments in one command, the last a root, all leave
#: the loop's reading.
R7V_L = "for d in ~/.subfleet/*; do :; done"
R7V_R = "rg foo \"$d\""
REVIEW_R7_DENIED = [
    "echo $(( $[1<<X\n] + 1 ))\n" + R7_F + "\nX",
    "echo $[ $((1<<X\n)) + 1 ]\n" + R7_F + "\nX",
    "let \"y=1<<X\n\"\n" + R7_F + "\nX",
    "(( x = $(cat <<EOF\n1\nEOF\n) ))\n" + R7_F,
    "cat <<Y; echo $((1<<X\n))\nY\n" + R7_F + "\nX",
    "echo $((1<<X\n)) <<Y\nY\n" + R7_F + "\nX",
    "(( x <<= 1 ))\n" + R7_F,
    "echo $(( 1 <<\nX ))\n" + R7_F + "\nX",
    "tee /dev/null < <(echo 'rg --files ~/.subfleet') | bash",
    "cat <(echo 'rg --files ~/.subfleet') | tee /dev/null | bash",
    "cat <(echo 'rg --files ~/.subfleet') | head -1 | sh",
    "cat <(echo 'rg --files ~/.subfleet') | xargs -0 sh -c",
    "cat - <(echo 'rg --files ~/.subfleet') <<< 'echo a' | bash",
    "cat <(echo 'rg --files ~/.subfleet') >/dev/null | bash",
    "cat <(echo ~/.subfleet) | xargs -I{} sh -c 'rg --files {}'",
    R7V_L + "; true || d=src; " + R7V_R,
    R7V_L + "; (d=src); " + R7V_R,
    R7V_L + "; ( :; d=src ); " + R7V_R,
    R7V_L + "; ( :; d=src; : ); " + R7V_R,
    R7V_L + "; echo \"$(d=src; :)\"; " + R7V_R,
    R7V_L + "; x=`d=src; :`; " + R7V_R,
    R7V_L + "; x=`:; d=src; :`; " + R7V_R,
    R7V_L + "; f() { d=src; }; " + R7V_R,
    R7V_L + "; while false; do\nd=src\ndone; " + R7V_R,
    R7V_L + "\ncase x in\n(y)\nd=src\n;;\nesac\n" + R7V_R,
    R7V_L + "; d=/; " + R7V_R,
    R7V_L + "; d=\\/; " + R7V_R,
    R7V_L + "; d=/*; rg foo $d",
    R7V_L + "; d=src d=~/.subfleet; " + R7V_R,
]
REVIEW_R7_ALLOWED = [
    "let y=1<<X\n" + R7_F + "\nX",
    "let y=1<<X z=2\n" + R7_F + "\nX\nrg foo src",
    "let 'y=1<<2' <<X\n" + R7_F + "\nX",
    "echo $(( $[1<<2] + 1 )) <<X\n" + R7_F + "\nX",
    "cat <<EOF; echo $((1<<2))\n" + R7_F + "\nEOF",
    "cat <<Y; echo $((1<<X\n))\n" + R7_F + "\nY\nX",
    "((x=1<<2)) <<Y\n" + R7_F + "\nY",
    "cat <(git ls-files) | xargs rg -l foo",
    R7V_L + "; d=src || true; " + R7V_R,
]


def test_review_r7_pins_r7s_unpinned_readings():
    """Each reading's own input: the denied forms are denied and the allowed ones allowed."""
    assert r5_denied([(c, S_) for c in REVIEW_R7_DENIED]) == []
    assert r5_allowed([(c, S_) for c in REVIEW_R7_ALLOWED]) == []


# ---------------------------------------------------------------- r8: review r7's findings

# Round r8 (2026-10-29) builds the hub's decisions on review r7 (REQUEST_CHANGES at 7447e9dce, all P3). Each pin is
# review r7's input or the brief's, judged through the whole hook in the fixture:
#   1. a loop variable keeps its root wherever the shells keep it: a newline right after &&, ||, | or |& continues
#      the line (the operator stays pv, and a pipe carries on); time and time -p before a compound command are a
#      keyword, and zsh's repeat and foreach open one; a name() or the word function anywhere in the command turns
#      the reset off; only a plain word replaces the loop's words, and any other value joins them (P3-1);
#   2. the 4,096 cap counts ' and backtick too (P3-2);
#   3. in $((...)) a << is a shift whenever the span ends in )) (P3-3);
#   4. a piped while read takes the roots of what a filter passes on from its <(...) (P3-4);
#   5. tee writing into a <(...) piped to a shell stays denied, listed "stricter" (P3-5);
#   6. the joined productions are listed open (P3-6).
#: Decision 1: review r7's P3-1 inputs (R7V_L and R7V_R as review r7 wrote them): a newline after &&, || and |; time
#: before if, while, for and case, and before a while inside an if; function bodies run out of textual order; zsh's
#: f() whose body is the assignment and repeat; and bash's word splitting of a quoted value with a blank.
R8_LOOPVAR_DENIED = [
    R7V_L + "; false &&\nd=src\n" + R7V_R,
    R7V_L + "; true ||\nd=src\n" + R7V_R,
    R7V_L + "; echo x |\nd=src\n" + R7V_R,
    R7V_L + "\ntime if false; then\nd=src\nfi\n" + R7V_R,
    R7V_L + "\ntime while false; do\nd=src\ndone\n" + R7V_R,
    R7V_L + "\ntime for x in; do\nd=src\ndone\n" + R7V_R,
    R7V_L + "\ntime case x in\n(y)\nd=src\n;;\nesac\n" + R7V_R,
    R7V_L + "\nif false; then time while false; do :; done; d=src; fi; " + R7V_R,
    "f() { for d in ~/.subfleet/*; do :; done; }; d=src; f; " + R7V_R,
    "f() {\nfor d in ~/.subfleet/*; do :; done\n}\nd=src\nf\n" + R7V_R,
    R7V_L + "; d=src; f() { d=~/.subfleet; }; f; " + R7V_R,
    R7V_L + "; f(); d=src; " + R7V_R,
    R7V_L + "\nf()\nd=src\n" + R7V_R,
    R7V_L + "\nrepeat 0 do\nd=src\ndone\n" + R7V_R,
    R7V_L + "; d='src /'; rg foo $d",
]
#: ... a quoted, escaped or blank-holding value after a scoped loop is still read (r7 denies these, and the shells walk
#: each root: a value that only left the loop's words in place would let them through), a time -p, and a pipe
#: continued on the next line into a shell or a while read ...
R8_LOOPVAR_MORE_DENIED = [
    "for d in src; do :; done; d='/'; rg foo \"$d\"",
    "for d in src; do :; done; d='/Users/tester/.subfleet'; rg foo \"$d\"",
    "for d in src; do :; done; d=\\/; rg foo \"$d\"",
    R7V_L + "\ntime -p while false; do\nd=src\ndone\n" + R7V_R,
    "echo 'rg --files ~/.subfleet' |\nbash",
    "find ~/.subfleet -maxdepth 1 |\nwhile read d; do rg foo \"$d\"; done",
    "cat <(echo 'rg --files ~/.subfleet') |\n\nbash",
]
#: ... while r7's five intended forms stay allowed, with a time before a plain command, a pipe continued on the next
#: line into a searcher that reads it, and && continued on the next line. From the home, where rg with no path walks
#: it, rg on the line after a | reads the pipe in both shells (r7 read its stdin as empty and denied it), and a
#: search before the | still walks.
R8_PIPE_HOME_ALLOWED = ["echo x |\nrg foo", "git log |\n\nrg foo"]
R8_PIPE_HOME_DENIED = ["rg foo |\ncat"]
R8_LOOPVAR_ALLOWED = R7_LOOPVAR_ALLOWED + [
    "time -p rg -n foo src",
    "for d in ~/.subfleet/*; do :; done\ntime -p true\nd=src\nrg foo \"$d\"",
    "git log --oneline |\nhead -5",
    "find src -name '*.py' |\nxargs rg -l foo",
    "git status --short &&\nrg -n foo src",
    "echo x |\nrg foo",
]
#: Decision 2: review r7's five rows past the step budget's reach (the first is the pinned row; the 16,000 `:` row is
#: rebuilt here beside a scoped search, as a row with no searcher is never prepped), each denied as too complex to
#: judge, the pinned row in at most twice the installed hook's child CPU ...
R8_CAP_PINNED = "cat <<A, 30,000 ''"
R8_CAP_ROWS = {
    R8_CAP_PINNED: "cat <<A " + "''" * 30000 + "\nA\nrg foo src",
    "12,000 <<''": "rg foo src; cat " + "<<'' " * 12000 + "\nx",
    "echo, 30,000 ''": "rg foo src; echo " + "''" * 30000,
    "16,000 `:`": "rg foo src; echo " + "`:`" * 16000,
    "13,000 `rg`": "`rg`" * 13000,
}
#: ... and the cap's edges: 4,097 or more of ' or of backtick beside a scoped search is too complex to judge, 4,096 is
#: read; the listed trade-off, a file-writing heredoc past the cap that names a searcher, is denied.
R8_CAP_DENIED = ["rg foo src " + "''" * 2049, "rg foo src; echo " + "`:`" * 2049,
                 "cat > notes.md <<'EOF'\n" + "it's\n" * 4097 + "EOF\nrg foo src"]
R8_CAP_ALLOWED = ["rg foo src " + "''" * 2048, "rg foo src; echo " + "`:`" * 2048,
                  "cat > notes.md <<'EOF'\n" + "it's\n" * 4094 + "EOF\nrg foo src"]
#: Decision 3: review r7's P3-3 input (bash reads $((1) + (...)) as arithmetic, its span ending in ))); r7's controls
#: stay allowed (R7_ARITH_ALLOWED).
R8_ARITH_DENIED = ["echo $((1) + (1<<X\n))\n" + R7_F + "\nX", "x=\"$((1) + (1<<X\n))\"\n" + R7_F + "\nX"]
#: Decision 4: review r7's P3-4 inputs and the brief's control.
R8_PSUB_LOOP_DENIED = ["cat <(find ~/.subfleet -maxdepth 1) | while read d; do rg foo \"$d\"; done",
                       "cat <(echo ~/.subfleet) | while read d; do rg foo \"$d\"; done",
                       "cat <(echo src) <(echo ~/.subfleet) | while read -r d; do grep -r x $d; done"]
R8_PSUB_LOOP_ALLOWED = ["cat <(git ls-files) | while read f; do wc -l \"$f\"; done",
                        "cat <(git ls-files) | while read f; do rg -n foo \"$f\"; done"]
#: Decisions 5, 6 and decision 1's sixteenth input: forms "Known differences" names, each with the text it is named by.
#: Stricter: denied though no shell walks (tee writes into the substitution, and nothing reaches bash) ...
R8_STRICTER = {"tee <(echo 'rg --files ~/.subfleet') | bash": "tee <(echo 'rg --files ~/.subfleet') | bash"}
#: ... and open: allowed here though a real shell walks a root (each line a pipe joins is judged on its own, and a
#: pipe after a brace group or a subshell carries nothing its commands write, which this round's probes found beside
#: review r7's three; a read from a here-string is a root held in a variable).
R8_KNOWN_OPEN = {
    "cat <(printf 'rg --files ') <(echo ~/.subfleet) | bash": "cat <(printf 'rg --files ') <(echo ~/.subfleet) | bash",
    "paste -d' ' <(echo 'rg --files') <(echo ~/.subfleet) | bash":
        "paste -d' ' <(echo 'rg --files') <(echo ~/.subfleet) | bash",
    "{ printf 'rg --files '; echo ~/.subfleet; } | bash": "{ printf 'rg --files '; echo ~/.subfleet; } | bash",
    "{ echo 'rg --files ~/.subfleet'; } | bash": "{ echo 'rg --files ~/.subfleet'; } | bash",
    "(echo 'rg --files ~/.subfleet') | bash": "(echo 'rg --files ~/.subfleet') | bash",
    R7V_L + "; d=src; read d <<< ~/.subfleet; " + R7V_R: "d=src; read d <<< ~/.subfleet",
}


def known_differences() -> str:
    """The overlay doc's "Known differences" section, runs of blanks and line breaks read as one space."""
    doc = (REPO / "docs/private-guard-overlay.md").read_text(encoding="utf-8")
    start = doc.index("Known differences that remain")
    return " ".join(doc[start:doc.index("| File | SHA-256 |", start)].split())


def test_r8_a_loop_variable_keeps_its_root_where_the_shells_keep_it():
    """Decision 1: review r7's fifteen P3-1 inputs are denied, and so are a quoted, escaped or blank-holding root value
    after a scoped loop, time -p before a compound command and a pipe continued on the next line; r7's five intended
    forms, a time before a plain command and the continued forms that walk nothing stay allowed."""
    assert r5_denied([(c, S_) for c in R8_LOOPVAR_DENIED + R8_LOOPVAR_MORE_DENIED]
                     + [(c, H_) for c in R8_PIPE_HOME_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R8_LOOPVAR_ALLOWED] + [(c, H_) for c in R8_PIPE_HOME_ALLOWED]) == []


def test_r8_quotes_and_backticks_count_against_the_cap(hooks):
    """Decision 2: more than 4,096 of ' or of backtick beside a searcher is too complex to judge, 4,096 is read; review
    r7's five rows are denied as too complex, the pinned row in at most twice the installed hook's child CPU (decision 6
    of r7's measure; one run each, the installed hook's CPU and the load reported)."""
    outs = judge_all(HOOK, [(c, S_) for c in R8_CAP_DENIED + R8_CAP_ALLOWED])
    for c in R8_CAP_DENIED:
        assert rule(outs[(c, S_)]) == "unscoped-search", c[:40]
        assert "too complex to judge" in json.loads(outs[(c, S_)])["hookSpecificOutput"]["permissionDecisionReason"]
    assert [c[:40] for c in R8_CAP_ALLOWED if rule(outs[(c, S_)]) is not None] == []
    rows = {}
    for name, command in R8_CAP_ROWS.items():
        cpu, wall, out = hook_cpu(HOOK, command)
        assert rule(out) == "unscoped-search", name
        assert "too complex to judge" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"], name
        old_cpu, old_wall, _ = hook_cpu(hooks["old"], command)
        rows[name] = {"cpu": [round(cpu, 2), round(old_cpu, 2)], "wall": [round(wall, 2), round(old_wall, 2)],
                      "load": round(os.getloadavg()[0], 1)}
    report("cap-r8", rows)
    assert rows[R8_CAP_PINNED]["cpu"][0] <= 2 * rows[R8_CAP_PINNED]["cpu"][1], rows[R8_CAP_PINNED]


def test_r8_a_dollar_double_paren_span_ending_in_two_closes_is_arithmetic():
    """Decision 3: a << in $((...)) whose span ends in )) is a shift, so the lines after it are commands; r7's
    arithmetic controls stay allowed."""
    assert r5_denied([(c, S_) for c in R8_ARITH_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R7_ARITH_ALLOWED]) == []


def test_r8_a_piped_while_read_takes_the_roots_a_filter_passes_on():
    """Decision 4: what a cat passes on from its <(...) is the roots a piped while read reads; a scoped production
    stays allowed."""
    assert r5_denied([(c, S_) for c in R8_PSUB_LOOP_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R8_PSUB_LOOP_ALLOWED]) == []


def test_r8_known_differences_name_the_stricter_and_open_forms(tmp_path):
    """Decisions 5 and 6, and decision 1's sixteenth input: each stricter form is denied and no real shell walks a root
    on it; each open form is allowed and a real shell walks one; the overlay doc's "Known differences" names each.
    A change that closes an open form has to move its pin."""
    known = known_differences()
    outs = judged(HOOK, list(R8_STRICTER) + list(R8_KNOWN_OPEN), S_)
    for command, named in R8_STRICTER.items():
        assert rule(outs[command]) == "unscoped-search", command
        assert not any(r6_shell_walks(sh, command, S_, tmp_path) for sh in ("zsh", "bash")), command
        assert named in known, named
    for command, named in R8_KNOWN_OPEN.items():
        assert rule(outs[command]) is None, command
        assert any(r6_shell_walks(sh, command, S_, tmp_path) for sh in ("zsh", "bash")), command
        assert named in known, named


def test_r8_scoped_searches_stay_allowed():
    """The must-allow lists (r6's test's cases, review r4's allowed forms, the print-and-exit forms, the routine commit
    and r6's 17 allowed lane commands), review r6's and review r7's allowed forms, r7's allowed forms, and each r8
    decision's allowed forms."""
    cases = ([(c, cwd) for cwd in R4_SCOPED_CWDS for c in R4_SCOPED] + [(c, LANE_W) for c in R4_PATTERN_ROOTS]
             + R4_MUST_ALLOW + R4_ALLOWED + REVIEW_R4_ALLOWED + [("git commit -F - <<'EOF'\nIt's fixed\nEOF", LANE_W)]
             + R5_HEREDOC_ALLOWED + R5_BOUND_ALLOWED + R5_BACKTICK_ALLOWED + R5_DOTDOT_ALLOWED + R5_BRACE_ALLOWED
             + R5_FAMILY_ALLOWED + R5_FORMS_ALLOWED + [(c, cwd) for cwd in (H_, "/", S_) for c in R4_META]
             + [(c, LANE_W) for c in R6_LANE_ALLOWED] + [(c, S_) for c in REVIEW_R6_ALLOWED + REVIEW_R7_ALLOWED]
             + [(c, S_) for c in R7_ARITH_ALLOWED + R7_PSUB_ALLOWED + R7_LOOPVAR_ALLOWED]
             + [(c, S_) for c in R8_LOOPVAR_ALLOWED + R8_CAP_ALLOWED + R8_PSUB_LOOP_ALLOWED]
             + [(c, H_) for c in R8_PIPE_HOME_ALLOWED])
    assert r5_allowed(cases) == []


#: Every r8 command pin and control a shell can run (the cap's rows and edges are left out: their verdicts are the
#: cap's), from the scoped cwd: the stricter forms are denied though no shell walks, and the open forms allowed
#: though one does.
R8_SHELL_COMMANDS = ([(c, S_) for c in (R8_LOOPVAR_DENIED + R8_LOOPVAR_MORE_DENIED + R8_LOOPVAR_ALLOWED
                                       + R8_ARITH_DENIED + R7_ARITH_ALLOWED + R8_PSUB_LOOP_DENIED
                                       + R8_PSUB_LOOP_ALLOWED + list(R8_STRICTER) + list(R8_KNOWN_OPEN))]
                     + [(c, H_) for c in R8_PIPE_HOME_ALLOWED + R8_PIPE_HOME_DENIED])
R8_SHELL_EXCEPTIONS = {**{c: True for c in R8_STRICTER}, **{c: False for c in R8_KNOWN_OPEN}}


def test_r8_command_pins_agree_with_real_shells(tmp_path):
    """Each r8 command pin and control, run by /bin/zsh -f and /bin/bash over the fixture with r6's stubs and the
    fixture's HOME: the hook denies it exactly when either shell's run would have walked a root, but for the listed
    stricter forms (denied, no shell walks) and open forms (allowed, a shell walks)."""
    outs = judge_all(HOOK, R8_SHELL_COMMANDS)
    rows, differ = [], []
    for command, cwd in R8_SHELL_COMMANDS:
        denied = rule(outs[(command, cwd)]) == "unscoped-search"
        shells = {sh: r6_shell_walks(sh, command, cwd, tmp_path) for sh in ("zsh", "bash")}
        rows.append((command, cwd, denied, shells))
        if command in R8_SHELL_EXCEPTIONS:
            if denied != R8_SHELL_EXCEPTIONS[command] or any(shells.values()) == denied:
                differ.append(("exception", command, denied, shells))
        elif denied != any(shells.values()):
            differ.append((command[:200], cwd, denied, shells))
    report("shells-r8", {"cases": len(rows), "denied": sum(r[2] for r in rows),
                         "zsh": sum(r[3]["zsh"] for r in rows), "bash": sum(r[3]["bash"] for r in rows),
                         "both": sum(r[3]["zsh"] and r[3]["bash"] for r in rows),
                         "neither": sum(not r[3]["zsh"] and not r[3]["bash"] for r in rows),
                         "rows": [(c[:120], d, s) for c, _, d, s in rows]})
    assert differ == [], differ[:10]


# ---------------------------------------------------------------- review r8: r8's unpinned readings

#: Review r8 (2026-10-29) pins readings r8's new code gets right that no list above holds, each found by a mutant that
#: passes every r4-r8 list and S1: a continuation inside $(...) (a !depth on the continuation passes every list), time
#: before a brace group inside an if (dropping { from time's keywords passes them), and the cap's edges at 4,096 and
#: 4,097 of one character (the lists pin 4,098). With them, the rest of review r8's check 2: the continuation inside a
#: backtick and a <(...), after a comment and with |&, a heredoc pending at the continued newline; ! time; time before a
#: subshell; a piped while read behind two filters; a $(( span that is a command in both shells; a $ value after a
#: scoped loop. Each denied form walks a root in a real shell, and no allowed one does.
RV8_E = "echo 'rg --files ~/.subfleet'"
REVIEW_R8_DENIED = [
    'x="$(' + RV8_E + ' |\nbash)"',
    "x=`" + RV8_E + " |\nbash`",
    "cat <(" + RV8_E + " |\nbash)",
    'x="$(find ~/.subfleet -maxdepth 1 |\nwhile read d; do rg foo "$d"; done)"',
    'x=`find ~/.subfleet -maxdepth 1 |\nwhile read d; do rg foo "$d"; done`',
    'cat <(find ~/.subfleet -maxdepth 1 |\nwhile read d; do rg foo "$d"; done)',
    R7V_L + "; false && # note\nd=src\n" + R7V_R,
    RV8_E + " | # note\nbash",
    RV8_E + " |\n# note\n\nbash",
    RV8_E + " |&\nbash",
    R7V_L + "; cat <<X ||\nbody\nX\nd=src\n" + R7V_R,
    R7V_L + "\n! time while false; do\nd=src\ndone\n" + R7V_R,
    R7V_L + "\nif false; then time { :; }; d=src; fi; " + R7V_R,
    R7V_L + "\ntime { false && d=src; }\n" + R7V_R,
    R7V_L + "\ntime ( d=src )\n" + R7V_R,
    "cat <(find ~/.subfleet -maxdepth 1) | sort | while read d; do rg foo \"$d\"; done",
    "sort <(echo ~/.subfleet) | uniq | while read d; do rg foo \"$d\"; done",
    "cat <(echo ~/.subfleet) | head -1 | tail -1 | while read d; do rg foo \"$d\"; done",
    "cat <(echo src) | cat - <(echo ~/.subfleet) | while read d; do rg foo \"$d\"; done",
    "echo $((rg --files ~/.subfleet) | (cat))",
    "for d in src; do :; done; d=$HOME; " + R7V_R,
    "for d in src; do :; done; d=\"${HOME}\"; " + R7V_R,
]
REVIEW_R8_ALLOWED = [
    ("echo x | # note\nrg foo", H_),
    ("cat <<X |\nhi\nX\nrg foo", H_),
    ("time -o f rg -n foo src", S_),
    ("cat <(git ls-files) | sort | while read f; do rg -n foo \"$f\"; done", S_),
    ("echo $((1) + (2))", S_),
    (R7V_L + "\nif true; then time { :; }; fi; d=src; " + R7V_R, S_),
    ("git commit -F - <<'EOF'\nAdd f() and a function\nEOF", LANE_W),
]
REVIEW_R8_CAP_DENIED = ["rg foo src; echo \"" + "'" * 4097 + "\"", "rg foo src; echo '" + "`" * 4097 + "'"]
REVIEW_R8_CAP_ALLOWED = ["rg foo src; echo \"" + "'" * 4096 + "\"", "rg foo src; echo '" + "`" * 4096 + "'"]


def test_review_r8_pins_r8s_unpinned_readings(tmp_path):
    """Each reading's own input: the denied forms are denied and walk a root in /bin/zsh -f or /bin/bash over the
    fixture (r6's stubs, the fixture's HOME); the allowed ones are allowed and walk none; 4,097 of ' or of backtick
    beside a scoped search is too complex to judge, and 4,096 is read."""
    assert r5_denied([(c, S_) for c in REVIEW_R8_DENIED]) == []
    assert r5_allowed(REVIEW_R8_ALLOWED) == []
    outs = judged(HOOK, REVIEW_R8_CAP_DENIED + REVIEW_R8_CAP_ALLOWED, S_)
    for command in REVIEW_R8_CAP_DENIED:
        assert rule(outs[command]) == "unscoped-search", command[:40]
        assert "too complex to judge" in json.loads(outs[command])["hookSpecificOutput"]["permissionDecisionReason"]
    assert [c[:40] for c in REVIEW_R8_CAP_ALLOWED if rule(outs[c]) is not None] == []
    unwalked = [c for c in REVIEW_R8_DENIED if not any(r6_shell_walks(sh, c, S_, tmp_path) for sh in ("zsh", "bash"))]
    walked = [c for c, cwd in REVIEW_R8_ALLOWED if any(r6_shell_walks(sh, c, cwd, tmp_path) for sh in ("zsh", "bash"))]
    assert (unwalked, walked) == ([], [])


# ---------------------------------------------------------------- r9: review r8's findings

# Round r9 (2026-10-29) builds the hub's decisions on review r8 (REQUEST_CHANGES at daf7f508c, all P3). Each pin is
# review r8's input or this round's probe, judged through the whole hook in the fixture:
#   1. where the reset of a loop variable does not apply (a compound command open, a keyword before the assignment,
#      an assignment that may not last, or a command that defines a function), the value assigned joins the loop's
#      words instead of being dropped (P3-1);
#   2. the 4,096 cap counts " too (P3-2);
#   3. the report's recount and the doc's lists say what r8 and r9 do (P3-3).
R9_LS = "for d in src; do :; done"
#: Decision 1: review r8's ten P3-1 inputs, which r7 denied and r8 allowed: after a scoped loop, a root assigned in a
#: command holding a () or the word function (prose, a heredoc body, a commit message and $() included), a defined
#: function, or zsh's repeat or foreach. Both shells walk the root in the first nine, zsh in the tenth.
R9_LOOPVAR_DENIED = [
    R9_LS + "; d=~/.subfleet; " + R7V_R + "; echo '()'",
    R9_LS + "; d=/; " + R7V_R + " # function",
    R9_LS + "; d='/'; " + R7V_R + "; python3 -c 'print()'",
    "cat <<'EOF'\nfunction\nEOF\n" + R9_LS + "; d=~/.subfleet; " + R7V_R,
    "git commit -m \"add f()\"; " + R9_LS + "; d=~/.subfleet; " + R7V_R,
    "f() { :; }; " + R9_LS + "; d=~/.subfleet; f; " + R7V_R,
    "x=$(); " + R9_LS + "; d=~/.subfleet; " + R7V_R,
    R9_LS + "\nrepeat 1 { : }\nd=~/.subfleet\n" + R7V_R,
    R9_LS + "; repeat 2 echo hi; d=/; " + R7V_R,
    R9_LS + "\nforeach x (a b)\n:\nend\nd=~/.subfleet\n" + R7V_R,
]
#: ... review r8's six that r7 and r8 both let through (an if, a brace group, a timed group, an assignment after &&, a
#: zsh foreach before it, a $ value in a command holding a ()), and a root assigned in the loop's own body, which this
#: round's probes found (r8, r7 and the installed hook allow it; both shells walk /) ...
R9_LOOPVAR_MORE_DENIED = [
    R9_LS + "; if true; then d=~/.subfleet; fi; " + R7V_R,
    R9_LS + "; { d=~/.subfleet; }; " + R7V_R,
    R9_LS + "\ntime { d=~/.subfleet; }\n" + R7V_R,
    R9_LS + "; true && d=~/.subfleet; " + R7V_R,
    R9_LS + "; foreach x (a) { : }; d=~/.subfleet; " + R7V_R,
    R9_LS + "; d=$HOME; " + R7V_R + "; echo '()'",
    "for d in src; do d=/; done; " + R7V_R,
]
#: ... a depth bound that joins disarms nothing (review r8's probes: after if false and after false &&) ...
R9_BOUND_JOIN_DENIED = [R7V_L + "; if false; then d=--max-depth=1; fi; " + R7V_R,
                        R7V_L + "; false && d=--max-depth=1; " + R7V_R]
#: ... while a scoped value that joins a scoped loop's words stays allowed in each of those places.
R9_LOOPVAR_ALLOWED = [
    R9_LS + "; if true; then d=lib; fi; " + R7V_R,
    R9_LS + "; { d=lib; }; " + R7V_R,
    R9_LS + "; true && d=lib; " + R7V_R,
    "f() { :; }; " + R9_LS + "; d=lib; f; " + R7V_R,
    R9_LS + "\nrepeat 1 { : }\nd=lib\n" + R7V_R,
    R9_LS + "\nforeach x (a b)\n:\nend\nd=lib\n" + R7V_R,
    R9_LS + "\ntime { d=lib; }\n" + R7V_R,
    "git commit -m \"add f()\"; " + R9_LS + "; d=lib; " + R7V_R,
    "for d in src; do d=lib; done; " + R7V_R,
]


def test_r9_a_later_value_joins_the_loop_words_where_the_reset_does_not_apply():
    """Decision 1: review r8's sixteen P3-1 inputs and the loop-body form are denied, and so are a joined depth bound's
    two forms; a scoped value that joins a scoped loop's words stays allowed."""
    assert r5_denied([(c, S_) for c in R9_LOOPVAR_DENIED + R9_LOOPVAR_MORE_DENIED + R9_BOUND_JOIN_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R9_LOOPVAR_ALLOWED]) == []
#: Decision 2: review r8's double-quote rows, each denied as too complex to judge: 60,000 and 30,000 " beside a scoped
#: search, a heredoc opened before 30,000 of them, and every other capped character at exactly 4,096 with 30,000 " ...
R9_AT_CAP = ("''" * 2048 + " " + "`:`" * 2048 + " " + "\\\\" * 2048 + " " + "${#x}" * 4096)
R9_DQ_ROWS = {
    "echo, 60,000 \"": "rg foo src; echo " + '""' * 30000,
    "echo, 30,000 \"": "rg foo src; echo " + '""' * 15000,
    "cat <<A, 30,000 \"": "cat <<A " + '""' * 15000 + "\nA\nrg foo src",
    "every capped character at 4,096, and 30,000 \"": "rg foo src; echo " + R9_AT_CAP + " " + '""' * 15000,
}
#: ... and the edges: 4,097 " beside a scoped search is too complex to judge and 4,096 is read, also with every other
#: capped character at 4,096 at once (review r8's residual row, read); the listed trade-off, a file-writing heredoc
#: past the cap that names a searcher, is denied, and the same heredoc at the cap is read.
R9_DQ_DENIED = ["rg foo src; echo '" + '"' * 4097 + "'",
                "cat > notes.md <<'EOF'\n" + 'say "hi"\n' * 2049 + "EOF\nrg foo src"]
R9_DQ_ALLOWED = ["rg foo src; echo '" + '"' * 4096 + "'",
                 "rg foo src; echo " + R9_AT_CAP + " " + '""' * 2048,
                 "cat > notes.md <<'EOF'\n" + 'say "hi"\n' * 2048 + "EOF\nrg foo src"]


def test_r9_double_quotes_count_against_the_cap():
    """Decision 2: more than 4,096 " beside a searcher is too complex to judge, and 4,096 is read, alone and with every
    other capped character at 4,096; review r8's four rows are denied as too complex."""
    outs = judged(HOOK, list(R9_DQ_ROWS.values()) + R9_DQ_DENIED + R9_DQ_ALLOWED, S_)
    for command in list(R9_DQ_ROWS.values()) + R9_DQ_DENIED:
        assert rule(outs[command]) == "unscoped-search", command[:40]
        assert "too complex to judge" in json.loads(outs[command])["hookSpecificOutput"]["permissionDecisionReason"]
    assert [c[:40] for c in R9_DQ_ALLOWED if rule(outs[c]) is not None] == []
#: Decision 1's over-blocks, listed "stricter": after a scoped loop, a root assigned where the shells never run the
#: assignment (after false &&, in an if false, after a group that succeeds and ||) joins the loop's words and is
#: denied, though no shell walks a root. Each with the text "Known differences" names it by.
R9_STRICTER = {
    R9_LS + "; false && d=/; " + R7V_R: R9_LS + "; false && d=/; " + R7V_R,
    R9_LS + "; if false; then d=~/.subfleet; fi; " + R7V_R: R9_LS + "; false && d=/; " + R7V_R,
    R9_LS + "; { :; } || d=/; " + R7V_R: R9_LS + "; false && d=/; " + R7V_R,
}
#: Every r9 command pin and control a shell can run (the cap's rows and edges are left out: their verdicts are the
#: cap's), from the scoped cwd: the stricter forms are denied though no shell walks.
R9_SHELL_COMMANDS = [(c, S_) for c in (R9_LOOPVAR_DENIED + R9_LOOPVAR_MORE_DENIED + R9_BOUND_JOIN_DENIED
                                       + R9_LOOPVAR_ALLOWED + list(R9_STRICTER))]


def test_r9_command_pins_agree_with_real_shells(tmp_path):
    """Each r9 command pin and control, run by /bin/zsh -f and /bin/bash over the fixture with r6's stubs and the
    fixture's HOME: the hook denies it exactly when either shell's run would have walked a root, but for the listed
    stricter forms (denied, no shell walks). Each of review r8's sixteen inputs walks a root in at least one shell."""
    outs = judge_all(HOOK, R9_SHELL_COMMANDS)
    rows, differ = [], []
    for command, cwd in R9_SHELL_COMMANDS:
        denied = rule(outs[(command, cwd)]) == "unscoped-search"
        shells = {sh: r6_shell_walks(sh, command, cwd, tmp_path) for sh in ("zsh", "bash")}
        rows.append((command, cwd, denied, shells))
        if command in R9_STRICTER:
            if not denied or any(shells.values()):
                differ.append(("stricter", command, denied, shells))
        elif denied != any(shells.values()):
            differ.append((command[:200], cwd, denied, shells))
    report("shells-r9", {"cases": len(rows), "denied": sum(r[2] for r in rows),
                         "zsh": sum(r[3]["zsh"] for r in rows), "bash": sum(r[3]["bash"] for r in rows),
                         "both": sum(r[3]["zsh"] and r[3]["bash"] for r in rows),
                         "neither": sum(not r[3]["zsh"] and not r[3]["bash"] for r in rows),
                         "rows": [(c[:120], d, s) for c, _, d, s in rows]})
    assert differ == [], differ[:10]
    assert [c for c, _, _, s in rows if c in R9_LOOPVAR_DENIED + R9_LOOPVAR_MORE_DENIED[:6]
            and not any(s.values())] == []
#: Decision 2's trade-off as "Known differences" names it.
R9_DQ_TRADE_OFF = "carries more than 4,096 double quotes"


def test_r9_known_differences_name_the_stricter_forms_and_the_double_quote_cap(tmp_path):
    """Decisions 1 and 2: each stricter form is denied and no real shell walks a root on it, and the overlay doc's "Known
    differences" names it and the cap's trade-off for "."""
    known = known_differences()
    outs = judged(HOOK, list(R9_STRICTER), S_)
    for command, named in R9_STRICTER.items():
        assert rule(outs[command]) == "unscoped-search", command
        assert not any(r6_shell_walks(sh, command, S_, tmp_path) for sh in ("zsh", "bash")), command
        assert named in known, named
    assert R9_DQ_TRADE_OFF in known


def test_r9_scoped_searches_stay_allowed():
    """r9's controls (a scoped value joining a scoped loop's words, each where the reset does not apply) and the cap's
    edges at 4,096 ", beside the routine commit and a heredoc of quoted prose; r8's must-allow test still holds every
    earlier list."""
    cases = ([(c, S_) for c in R9_LOOPVAR_ALLOWED + R9_DQ_ALLOWED]
             + [("git commit -F - <<'EOF'\nQuote \"the guard\" and \"the hook\"\nEOF", LANE_W),
                ("cat > notes.md <<'EOF'\n" + 'He said "rg foo src" twice.\n' * 100 + "EOF\nrg -n foo src", S_)])
    assert r5_allowed(cases) == []


# ---------------------------------------------------------------- review r9: r9's unpinned readings

#: Review r9 (at b229383fc) pins r9 readings no r9 test holds. Decision 1's join, through the places it reaches beyond
#: r9's pins: an assignment in a nested loop or a while-read body, in a subshell, in an unquoted $(...), a backtick
#: span or a <(...) read at the top of the command, and in a function body after its loop. r8 allowed each, and both
#: shells walk / in each (r6's stubs, the fixture's HOME).
REVIEW_R9_DENIED = [
    "for e in x; do for d in src; do :; done; d=/; done; " + R7V_R,
    "for e in x; do for d in src; do d=/; done; done; " + R7V_R,
    "for d in src; do for e in x; do d=/; done; done; " + R7V_R,
    "for e in x; do " + R9_LS + "; done; if true; then d=/; fi; " + R7V_R,
    "echo src | while read d; do d=/; " + R7V_R + "; done",
    "echo src | while read d; do if true; then d=/; fi; " + R7V_R + "; done",
    "(" + R9_LS + "; d=/; " + R7V_R + ")",
    R9_LS + "; (d=/; " + R7V_R + ")",
    "x=$(" + R9_LS + "; d=/; " + R7V_R + ")",
    "echo `" + R9_LS + "; d=/; " + R7V_R + "`",
    "cat <(" + R9_LS + "; d=/; " + R7V_R + ")",
    "f() { " + R9_LS + "; d=/; " + R7V_R + "; }; f",
    R9_LS + "; f() { d=/; }; f; " + R7V_R,
]
#: ... the same join's over-blocks of the listed "stricter" kind beyond r9's three: a root assigned in a subshell, one a
#: later assignment replaces (in an if, or in a command that defines a function), and one in a function never called.
#: Each is denied, and neither shell walks a root.
REVIEW_R9_STRICTER = [
    R9_LS + "; (d=/); " + R7V_R,
    R9_LS + "; ( d=/ ) && " + R7V_R,
    R9_LS + "; if true; then d=/ d=src; fi; " + R7V_R,
    "f() { :; }; " + R9_LS + "; d=/; d=src; " + R7V_R,
    R9_LS + "; f() { d=/; }; " + R7V_R,
]
#: ... and controls that stay allowed, none walked: a scoped value through the same places, a prefix assignment (it
#: lasts for its own command only) and an assignment inside a quoted $(...) that names no searcher.
REVIEW_R9_ALLOWED = [
    "for d in src; do for e in x; do d=lib; done; done; " + R7V_R,
    "echo src | while read d; do d=lib; " + R7V_R + "; done",
    "(" + R9_LS + "; d=lib; " + R7V_R + ")",
    R9_LS + "; d=/ :; " + R7V_R,
    R9_LS + "; d=/ true; " + R7V_R,
    R9_LS + "; echo \"$(d=/)\"; " + R7V_R,
]
#: Decision 2's deny message names " beside the other capped characters.
REVIEW_R9_CAP_MESSAGE = "of one of $, #, \\, ', \" or `"


def test_review_r9_pins_r9s_unpinned_readings(tmp_path):
    """Each reading's own input: the denied forms are denied and walk a root in /bin/zsh -f or /bin/bash over the
    fixture (r6's stubs, the fixture's HOME); the stricter forms are denied and walk none; the controls are allowed and
    walk none; and the cap's message for 4,097 " names "."""
    assert r5_denied([(c, S_) for c in REVIEW_R9_DENIED + REVIEW_R9_STRICTER]) == []
    assert r5_allowed([(c, S_) for c in REVIEW_R9_ALLOWED]) == []
    walks = {c: any(r6_shell_walks(sh, c, S_, tmp_path) for sh in ("zsh", "bash"))
             for c in REVIEW_R9_DENIED + REVIEW_R9_STRICTER + REVIEW_R9_ALLOWED}
    assert ([c for c in REVIEW_R9_DENIED if not walks[c]],
            [c for c in REVIEW_R9_STRICTER + REVIEW_R9_ALLOWED if walks[c]]) == ([], [])
    out = judged(HOOK, [R9_DQ_DENIED[0]], S_)[R9_DQ_DENIED[0]]
    assert REVIEW_R9_CAP_MESSAGE in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------- r10: review r9's findings

# Round r10 (2026-10-30) builds the hub's decisions on review r9 (REQUEST_CHANGES at b229383fc, all P3). Each pin is
# review r9's input or this round's probe, judged through the whole hook in the fixture:
#   1. the join reaches inside a substitution or a nested script: there a value assigned to a loop variable joins the
#      loop's words too (P3-1, the depth gate dropped);
#   2. in a command that defines a function, a value a function body assigns before the loop joins the words of a
#      later loop over that name (P3-2);
#   3. the doc says where the value joins and lists the over-blocks (P3-3).
#: Decision 1: review r9's seven P3-1 inputs: after a scoped loop, a root assigned inside a quoted $(...), a bash -c
#: or eval script, a heredoc script, or after a loop inside a substitution. r9, r8, r7 and the installed hook allow
#: each, and both shells walk / in each ...
R10_NESTED_DENIED = [
    'echo "$(' + R9_LS + '; d=/; ' + R7V_R + ')"',
    'echo "$(' + R9_LS + '; if true; then d=/; fi; ' + R7V_R + ')"',
    R9_LS + '; echo "$(d=/; ' + R7V_R + ')"',
    "bash -c '" + R9_LS + "; d=/; " + R7V_R + "'",
    "eval '" + R9_LS + "; d=/; " + R7V_R + "'",
    "bash <<'EOF'\n" + R9_LS + "\nd=/\n" + R7V_R + "\nEOF",
    'echo "$(for e in x; do ' + R9_LS + '; done; d=/; ' + R7V_R + ')"',
]
#: ... and, this round's probes found, an eval of a plain assignment, whose argument the prep reads as a nested
#: script: r9, r8, r7 and the installed hook allow each, and both shells walk the root (eval runs it in the shell
#: itself) ...
R10_EVAL_DENIED = [R9_LS + "; eval d=/; " + R7V_R, R9_LS + "; eval 'd=/'; " + R7V_R,
                   R9_LS + "; eval d=~/.subfleet; " + R7V_R]
#: ... a depth bound that joins there disarms nothing (it never runs, so both shells walk the loop's root) ...
R10_NESTED_BOUND_DENIED = ['echo "$(' + R7V_L + '; false && d=--max-depth=1; ' + R7V_R + ')"']
#: ... while an assignment in a quoted $(...) that names no searcher stays allowed (the hub's control: the subshell
#: keeps the value, and neither shell walks a root), and so does a scoped value assigned in a nested script.
R10_NESTED_ALLOWED = [
    R9_LS + '; echo "$(d=/)"; ' + R7V_R,
    'echo "$(' + R9_LS + '; d=src; ' + R7V_R + ')"',
    "bash -c '" + R9_LS + "; d=lib; " + R7V_R + "'",
]


def test_r10_a_value_joins_inside_a_substitution_or_a_nested_script():
    """Decision 1: review r9's seven P3-1 inputs, an eval of a plain assignment and a joined depth bound in a
    substitution are denied; an assignment in a quoted $(...) that names no searcher, and a scoped value in a nested
    script, stay allowed."""
    assert r5_denied([(c, S_) for c in R10_NESTED_DENIED + R10_EVAL_DENIED + R10_NESTED_BOUND_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R10_NESTED_ALLOWED]) == []
#: Decision 2: review r9's four P3-2 inputs: a function body written before a scoped loop and called after it assigns
#: a root (by name(), by the word function, a ~/.subfleet value, an if in the body). r9, r8, r7 and the installed hook
#: allow each, and both shells walk the root in each ...
R10_FUNCTION_DENIED = [
    "f() { d=/; }; " + R9_LS + "; f; " + R7V_R,
    "function f { d=/; }; " + R9_LS + "; f; " + R7V_R,
    "f() { d=~/.subfleet; }; " + R9_LS + "; f; " + R7V_R,
    "f() { if true; then d=/; fi; }; " + R9_LS + "; f; " + R7V_R,
]
#: ... a depth bound a function body joins disarms nothing (the function is never called, so both shells walk the
#: loop's root) ...
R10_FUNCTION_BOUND_DENIED = ["f() { d=--max-depth=1; }; " + R7V_L + "; false && f; " + R7V_R]
#: ... while the hub's two controls stay allowed: a root assigned at the top before the definition (the loop replaces
#: it) and a scoped value in the body; neither shell walks a root.
R10_FUNCTION_ALLOWED = [
    "d=/; f() { :; }; " + R9_LS + "; " + R7V_R,
    "f() { d=lib; }; " + R9_LS + "; f; " + R7V_R,
]


def test_r10_a_function_body_written_before_its_loop_joins():
    """Decision 2: review r9's four P3-2 inputs and a joined depth bound are denied; the hub's two controls stay
    allowed."""
    assert r5_denied([(c, S_) for c in R10_FUNCTION_DENIED + R10_FUNCTION_BOUND_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R10_FUNCTION_ALLOWED]) == []
#: The over-blocks, listed "stricter" (decision 3): each is denied, and neither shell walks a root. Decision 1's: a root
#: assigned only in a nested script or a substitution, which never reaches the search after it; decision 2's: a root a
#: function body assigns where no shell runs it (the function is never called, beside r9's never-run assignment) or a
#: root assigned before the loop, which the loop replaces. Each with the text "Known differences" names it by.
R10_STRICTER = {
    R9_LS + "; bash -c 'd=/'; " + R7V_R: R9_LS + "; bash -c 'd=/'; " + R7V_R,
    R9_LS + '; echo "$(d=/; rg foo src)"; ' + R7V_R: R9_LS + '; echo "$(d=/; rg foo src)"; ' + R7V_R,
    "f() { d=/; }; " + R9_LS + "; " + R7V_R: "f() { d=/; }; " + R9_LS + "; " + R7V_R,
    "f() { :; }; if true; then d=/; fi; " + R9_LS + "; " + R7V_R: "f() { :; }; if true; then d=/; fi; " + R9_LS,
}
#: Review r9's five over-blocks of r9's join (REVIEW_R9_STRICTER, pinned there), each by the text that names it.
R10_REVIEW_R9_STRICTER_NAMED = {
    REVIEW_R9_STRICTER[0]: '(d=/); rg foo "$d"',
    REVIEW_R9_STRICTER[1]: '( d=/ ) && rg foo "$d"',
    REVIEW_R9_STRICTER[2]: "if true; then d=/ d=src; fi",
    REVIEW_R9_STRICTER[3]: "f() { :; }; for d in src; do :; done; d=/; d=src",
    REVIEW_R9_STRICTER[4]: 'f() { d=/; }; rg foo "$d"',
}
#: What the prep does not read, named under "let through" (decision 3): after a scoped loop, a root assigned by +=, by
#: a builtin or by a command substitution's output. Each is allowed, and a real shell walks the root. Of eval, only a
#: prefix assignment is let through (R10_EVAL_DENIED: decision 1 reads eval d=/ itself). Modeling these is a
#: follow-up of its own; a change that closes one has to move its pin.
R10_BUILTINS = "`export`, `declare`, `typeset`, `readonly`, `local`, `eval`, `printf -v`, `read`"
R10_KNOWN_OPEN = {
    "for d in /U; do :; done; d+=sers; " + R7V_R: "by `+=`",
    R9_LS + "; d=; d+=/; " + R7V_R: "by `+=`",
    R9_LS + "; export d=/; " + R7V_R: R10_BUILTINS,
    R9_LS + "; declare d=/; " + R7V_R: R10_BUILTINS,
    R9_LS + "; typeset d=/; " + R7V_R: R10_BUILTINS,
    R9_LS + "; readonly d=/; " + R7V_R: R10_BUILTINS,
    R9_LS + "; eval export d=/; " + R7V_R: R10_BUILTINS,
    R9_LS + "; printf -v d /; " + R7V_R: R10_BUILTINS,
    R9_LS + "; read d <<< /; " + R7V_R: R10_BUILTINS,
    R9_LS + "; f() { local d=/; " + R7V_R + "; }; f": R10_BUILTINS,
    R9_LS + "; d=/ eval '" + R7V_R + "'": R10_BUILTINS,
    R9_LS + "; d=$(echo /); " + R7V_R: "a value a command substitution writes",
}
#: Every r10 command pin and control a shell can run, from the scoped cwd: the stricter forms are denied though no
#: shell walks, and the open forms allowed though one does.
R10_SHELL_COMMANDS = [(c, S_) for c in (R10_NESTED_DENIED + R10_EVAL_DENIED + R10_NESTED_BOUND_DENIED
                                        + R10_NESTED_ALLOWED + R10_FUNCTION_DENIED + R10_FUNCTION_BOUND_DENIED
                                        + R10_FUNCTION_ALLOWED + list(R10_STRICTER) + list(R10_KNOWN_OPEN))]


def test_r10_command_pins_agree_with_real_shells(tmp_path):
    """Each r10 command pin and control, run by /bin/zsh -f and /bin/bash over the fixture with r6's stubs and the
    fixture's HOME: the hook denies it exactly when either shell's run would have walked a root, but for the listed
    stricter forms (denied, no shell walks) and open forms (allowed, a shell walks). Both shells walk the root in each
    of review r9's eleven inputs and in each eval of a plain assignment."""
    outs = judge_all(HOOK, R10_SHELL_COMMANDS)
    rows, differ = [], []
    for command, cwd in R10_SHELL_COMMANDS:
        denied = rule(outs[(command, cwd)]) == "unscoped-search"
        shells = {sh: r6_shell_walks(sh, command, cwd, tmp_path) for sh in ("zsh", "bash")}
        rows.append((command, cwd, denied, shells))
        if command in R10_STRICTER:
            if not denied or any(shells.values()):
                differ.append(("stricter", command, denied, shells))
        elif command in R10_KNOWN_OPEN:
            if denied or not any(shells.values()):
                differ.append(("open", command, denied, shells))
        elif denied != any(shells.values()):
            differ.append((command[:200], cwd, denied, shells))
    report("shells-r10", {"cases": len(rows), "denied": sum(r[2] for r in rows),
                          "zsh": sum(r[3]["zsh"] for r in rows), "bash": sum(r[3]["bash"] for r in rows),
                          "both": sum(r[3]["zsh"] and r[3]["bash"] for r in rows),
                          "neither": sum(not r[3]["zsh"] and not r[3]["bash"] for r in rows),
                          "rows": [(c[:120], d, s) for c, _, d, s in rows]})
    assert differ == [], differ[:10]
    assert [c for c, _, _, s in rows if c in R10_NESTED_DENIED + R10_EVAL_DENIED + R10_FUNCTION_DENIED
            and not all(s.values())] == []


def test_r10_known_differences_name_the_over_blocks_and_what_is_let_through(tmp_path):
    """Decision 3: each stricter form (r10's and review r9's five) is denied and no real shell walks a root on it; each
    open form is allowed and a real shell walks one; the overlay doc's "Known differences" names each, and says where
    the value joins."""
    known = known_differences()
    stricter = {**R10_STRICTER, **R10_REVIEW_R9_STRICTER_NAMED}
    outs = judged(HOOK, list(stricter) + list(R10_KNOWN_OPEN), S_)
    for command, named in stricter.items():
        assert rule(outs[command]) == "unscoped-search", command
        assert not any(r6_shell_walks(sh, command, S_, tmp_path) for sh in ("zsh", "bash")), command
        assert named in known, named
    for command, named in R10_KNOWN_OPEN.items():
        assert rule(outs[command]) is None, command
        assert any(r6_shell_walks(sh, command, S_, tmp_path) for sh in ("zsh", "bash")), command
        assert named in known, named
    assert "run it only in a subshell, a substitution or a nested script, or replace it later" in known


def test_r10_scoped_searches_stay_allowed():
    """r10's controls (an assignment in a quoted $(...) that names no searcher, a scoped value in a nested script or a
    function body, a root assigned before a function's definition and replaced by the loop) beside review r9's
    controls; r8's and r9's must-allow tests still hold every earlier list."""
    assert r5_allowed([(c, S_) for c in R10_NESTED_ALLOWED + R10_FUNCTION_ALLOWED + REVIEW_R9_ALLOWED]) == []


# ---------------------------------------------------------------- review r10: r10's unpinned readings

#: Review r10 (at 0117418c5) pins r10 readings no r10 test holds. Decision 1's join, through the places it reaches
#: beyond r10's pins: sh -c and zsh -c, a double-quoted bash -c or eval script, a heredoc script into sh or piped into
#: bash, a nested substitution, a script inside a script, a search inside the nested script, a ~/.subfleet value, a
#: root after a scoped value, eval of a substitution's output, and the eval spellings decision 1 reads. r9 allowed each,
#: and both shells walk the root in each (r6's stubs, the fixture's HOME).
REVIEW_R10_NESTED_DENIED = [
    "sh -c '" + R9_LS + "; d=/; " + R7V_R + "'",
    "zsh -c '" + R9_LS + "; d=/; " + R7V_R + "'",
    'bash -c "' + R9_LS + '; d=/; rg foo \\"\\$d\\""',
    'eval "' + R9_LS + '; d=/; rg foo \\"\\$d\\""',
    "sh <<'EOF'\n" + R9_LS + "\nd=/\n" + R7V_R + "\nEOF",
    "cat <<'EOF' | bash\n" + R9_LS + "\nd=/\n" + R7V_R + "\nEOF",
    'echo "$(echo "$(' + R9_LS + '; d=/; ' + R7V_R + ')")"',
    "bash -c 'bash -c '\"'\"'" + R9_LS + "; d=/; " + R7V_R + "'\"'\"''",
    R9_LS + "; bash -c 'd=/; " + R7V_R + "'",
    'echo "$(' + R9_LS + '; d=~/.subfleet; ' + R7V_R + ')"',
    'echo "$(' + R9_LS + '; d=src; d=/; ' + R7V_R + ')"',
    "eval \"$(echo '" + R9_LS + "; d=/; " + R7V_R + "')\"",
    R9_LS + '; echo "$(d=/; ' + R7V_R + ')"; d=lib; ' + R7V_R,
    R9_LS + '; eval "d=/"; ' + R7V_R,
    R9_LS + "; eval eval d=/; " + R7V_R,
    R9_LS + "; builtin eval d=/; " + R7V_R,
    R9_LS + "; eval -- d=/; " + R7V_R,
    R9_LS + '; eval "$(echo d=/)"; ' + R7V_R,
    R9_LS + "; eval `echo d=/`; " + R7V_R,
    R9_LS + "; eval 'd=src; d=/'; " + R7V_R,
    R9_LS + "; eval 'if true; then d=/; fi'; " + R7V_R,
]
#: ... decision 2's, beyond r10's pins: a body called twice, a body whose last value is the root, a function a body
#: defines, `function f()`, a body another function calls, bodies over several lines, a body called inside the loop
#: (after it or before the search), and the same forms inside a nested script or a substitution ...
REVIEW_R10_FUNCTION_DENIED = [
    "f() { d=/; }; " + R9_LS + "; f; f; " + R7V_R,
    "f() { d=src; d=/; }; " + R9_LS + "; f; " + R7V_R,
    "f() { g() { d=/; }; }; " + R9_LS + "; f; g; " + R7V_R,
    "function f() { d=/; }; " + R9_LS + "; f; " + R7V_R,
    "f() { d=/; }; g() { f; }; " + R9_LS + "; g; " + R7V_R,
    "f() { d=/; }\n" + R9_LS + "\nf\n" + R7V_R,
    "f() {\n  d=/\n}\n" + R9_LS + "\nf\n" + R7V_R,
    "f() { d=/; }; for d in src; do f; done; " + R7V_R,
    "f() { d=/; }; for d in src; do f; " + R7V_R + "; done",
    "bash -c 'f() { d=/; }; " + R9_LS + "; f; " + R7V_R + "'",
    'echo "$(f() { d=/; }; ' + R9_LS + '; f; ' + R7V_R + ')"',
]
#: ... and the loop's root words kept through an assignment in an inner scope (r10 decision 1 joins there rather than
#: replaces): after a loop over ~/.subfleet's children, a scoped value assigned in a nested script, a quoted $(...) that
#: names no searcher, an unquoted $(...), a subshell or a function body leaves the shell's own variable at the loop's
#: last root. Every hook since r8 denies each, and both shells walk the root.
REVIEW_R10_KEPT_DENIED = [
    R7V_L + "; bash -c 'd=src'; " + R7V_R,
    R7V_L + '; echo "$(d=src)"; ' + R7V_R,
    R7V_L + "; x=$(d=src; echo); " + R7V_R,
    R7V_L + "; bash -c 'd=src; " + R7V_R + "'; " + R7V_R,
    R7V_L + "; (d=src); " + R7V_R,
    R7V_L + "; f() { d=src; }; " + R7V_R,
    "f() { d=src; }; " + R7V_L + "; " + R7V_R,
]
#: The over-blocks of r10's joins, each of a kind "Known differences" lists under "stricter" (a value replaced later, a
#: root the loop replaces, a function body never called, a subshell's assignment): each is denied, and neither shell
#: walks a root.
REVIEW_R10_STRICTER = [
    'echo "$(' + R9_LS + '; d=/; d=src; ' + R7V_R + ')"',
    "f() { d=/; }; f; " + R9_LS + "; " + R7V_R,
    "f() { d=/; d=src; }; " + R9_LS + "; f; " + R7V_R,
    "f() { g() { d=/; }; }; " + R9_LS + "; f; " + R7V_R,
    "f() ( d=/ ); " + R9_LS + "; f; " + R7V_R,
    "f() { d=/; }; bash -c '" + R9_LS + "; " + R7V_R + "'",
    "f() { d=/; }; " + R9_LS + "; f; d=src; " + R7V_R,
    R9_LS + "; eval d=/ d=src; " + R7V_R,
    "echo '()'; if true; then d=/; fi; " + R9_LS + "; " + R7V_R,
    R7V_L + "; eval 'd=src'; " + R7V_R,
]
#: ... and controls that stay allowed, none walked: an assignment in a quoted $(...) that names no searcher inside a
#: substitution, a prefix assignment in a nested script and through eval, a scoped value after a nested root, a body
#: assigning another name or an empty value, and lane-style uses of a scoped value in a nested script, a substitution, a
#: function's local loop and a body before the loop.
REVIEW_R10_ALLOWED = [
    'echo "$(' + R9_LS + '; echo "$(d=/)"; ' + R7V_R + ')"',
    "bash -c '" + R9_LS + "; d=/ :; " + R7V_R + "'",
    R9_LS + "; eval 'd=/ :'; " + R7V_R,
    R9_LS + "; bash -c 'd=/'; d=src; " + R7V_R,
    "f() { e=/; }; " + R9_LS + "; f; " + R7V_R,
    "f() { d=; }; " + R9_LS + "; f; " + R7V_R,
    R9_LS + '; echo "$(d=lib; ' + R7V_R + ')"; ' + R7V_R,
    R9_LS + "; bash -c 'd=lib; " + R7V_R + "'; " + R7V_R,
    'main() { local d; for d in src lib; do ' + R7V_R + '; done; }; main',
    "f() { d=src; }; for d in lib; do :; done; f; " + R7V_R,
]


def test_review_r10_pins_r10s_unpinned_readings(tmp_path):
    """Each reading's own input: the denied forms are denied and walk a root in both /bin/zsh -f and /bin/bash over
    the fixture (r6's stubs, the fixture's HOME); the stricter forms are denied and walk none; the controls are allowed
    and walk none."""
    denied = REVIEW_R10_NESTED_DENIED + REVIEW_R10_FUNCTION_DENIED + REVIEW_R10_KEPT_DENIED
    assert r5_denied([(c, S_) for c in denied + REVIEW_R10_STRICTER]) == []
    assert r5_allowed([(c, S_) for c in REVIEW_R10_ALLOWED]) == []
    walks = {c: [r6_shell_walks(sh, c, S_, tmp_path) for sh in ("zsh", "bash")]
             for c in denied + REVIEW_R10_STRICTER + REVIEW_R10_ALLOWED}
    assert ([c for c in denied if not all(walks[c])],
            [c for c in REVIEW_R10_STRICTER + REVIEW_R10_ALLOWED if any(walks[c])]) == ([], [])


# ---------------------------------------------------------------- r11: review r10's findings

# Round r11 (2026-10-30) builds the hub's decisions on review r10 (REQUEST_CHANGES at 0117418c5: one P2, five P3s).
# Below, R9_LS is a scoped loop, R7V_L a loop over ~/.subfleet's children and R7V_R the search of its variable:
#   1. P2-1: each word a searcher operand is read as through a loop variable, and each 16 bytes of them, costs a step
#      of the shared 20,000-step budget, and the path-word judgment judges each distinct word once;
#   2. P3-1: a function body's value joins a piped while-read loop's words, as it does a for loop's;
#   3. P3-4: a loop read where the reset would not apply joins the words its variable stood for, and read always joins;
#   4. P3-5: a search in a function body written before its loop is listed as let through.


def r11_subst(n: int, wrap: str) -> str:
    """Review r10's P2-1 shape: after a scoped loop, n plain values of d and n searches of it, in wrap."""
    body = R9_LS + "; " + "; ".join(f"d=a{i}" for i in range(n)) + "; " + "; ".join(['rg x "$d"'] * n)
    return wrap.replace("BODY", body)


def r11_loop(words, searches: int, sep: str = "; ") -> str:
    """A loop over ``words``, then ``searches`` searches of its variable joined by ``sep``."""
    return "for d in " + " ".join(words) + "; do :; done" + sep + sep.join([R7V_R] * searches)


#: Decision 1: review r10's cost table, each row denied as too complex in at most R11_COST_CPU_S of child CPU (r10
#: took more than 75 s on the first, and 9.4-25.9 s on the others; review r10 measured V1 and V4 at 0.10-0.15 s) ...
R11_COST = {
    "420 values in a quoted $(...), 7.9 KB": r11_subst(420, 'echo "$(BODY)"'),
    "300 values in a quoted $(...)": r11_subst(300, 'echo "$(BODY)"'),
    "300 values in bash -c": r11_subst(300, "bash -c 'BODY'"),
    "300 values in a function body": ("f() { " + "; ".join(f"d=a{i}" for i in range(300)) + "; }; " + R9_LS
                                      + "; f; " + "; ".join([R7V_R] * 300)),
    "300 values in an if (r9's route)": (R9_LS + "; if true; then " + "; ".join(f"d=a{i}" for i in range(300))
                                         + "; fi; " + "; ".join([R7V_R] * 300)),
    "400 words x 400 searches, one line (r6's route)": r11_loop([f"a{i}" for i in range(400)], 400),
    "200 path words x 200 searches, a line each": r11_loop([f"{S_}/src/p{i}" for i in range(200)], 200, "\n"),
    #: ... and this round's: one long word read in many places, which a count of words alone let through (25 s of
    #: CPU, 40 s of wall time at load 25, on the word count alone)
    "a 4,000-byte word x 2,000 searches": "for d in /" + "a" * 4000 + "; do :; done; " + 'rg x "$d"; ' * 2000,
}
R11_COST_CPU_S = 1.0
#: ... and the budget's edges, exact: a loop over 100 words "a" (199 bytes: 100 + 12 steps a search) read 178 times is
#: 19,936 steps, read, and 179 times 20,048, too complex; one word of 1,600 bytes (1 + 100 steps) read 198 times is
#: 19,998, and 199 times 20,099. Nothing else in these commands costs a step.
R11_STEP_EDGE = {
    "read": [r11_loop(["a"] * 100, 178), r11_loop(["b" * 1600], 198)],
    "unread": [r11_loop(["a"] * 100, 179), r11_loop(["b" * 1600], 199)],
}
#: ... and, under the budget, the worst this round found: 100 path words of 15 bytes, read 99 times, a line each and
#: on one line (each path word judged once: 8.1-8.2 s of CPU when each place is judged), and 63 words of 63 bytes read
#: 63 times. Each is allowed, in at most R11_UNDER_CPU_S of child CPU (0.4-1.1 s at load 21).
R11_UNDER = {
    "100 path words x 99, a line each": r11_loop([f"/u/aaaaaaa/p{i:03d}" for i in range(100)], 99, "\n"),
    "100 path words x 99, one line": r11_loop([f"/u/aaaaaaa/p{i:03d}" for i in range(100)], 99),
    "63 words of 63 bytes x 63": r11_loop([f"/u/{i:03d}" + "c" * 56 for i in range(63)], 63),
}
R11_UNDER_CPU_S = 3.0
#: The text the "too complex" message names loop variables by.
R11_COMPLEX_LOOP = "or loop variables standing for more text"


def test_r11_loop_variable_words_cost_steps_of_the_shared_budget(hooks):
    """Decision 1: each row of review r10's cost table, and one long word read in many places, is denied as too complex
    to judge in at most R11_COST_CPU_S of child CPU, and the message names loop variables (one run each; the installed
    hook's CPU and the load reported); the budget's word and byte edges are exact."""
    rows = {}
    for name, command in R11_COST.items():
        cpu, wall, out = hook_cpu(HOOK, command)
        assert rule(out) == "unscoped-search", name
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        assert "too complex to judge" in reason and R11_COMPLEX_LOOP in reason, name
        old_cpu, old_wall, _ = hook_cpu(hooks["old"], command)
        rows[name] = {"cpu": [round(cpu, 2), round(old_cpu, 2)], "wall": [round(wall, 2), round(old_wall, 2)],
                      "load": round(os.getloadavg()[0], 1)}
        assert cpu <= R11_COST_CPU_S, (name, rows[name])
    report("cost-r11", rows)
    outs = judged(HOOK, R11_STEP_EDGE["read"] + R11_STEP_EDGE["unread"], S_)
    assert [c[:40] for c in R11_STEP_EDGE["read"] if rule(outs[c]) is not None] == []
    for c in R11_STEP_EDGE["unread"]:
        assert rule(outs[c]) == "unscoped-search", c[:40]
        assert R11_COMPLEX_LOOP in json.loads(outs[c])["hookSpecificOutput"]["permissionDecisionReason"], c[:40]


def test_r11_each_path_word_is_judged_once():
    """Decision 1 (V4): under the budget, the worst rows this round found are allowed in at most R11_UNDER_CPU_S of
    child CPU (one run each, reported); a root among repeated scoped words, in any place, is still denied."""
    rows = {}
    for name, command in R11_UNDER.items():
        cpu, wall, out = hook_cpu(HOOK, command)
        assert rule(out) is None, name
        rows[name] = {"cpu": round(cpu, 2), "wall": round(wall, 2), "load": round(os.getloadavg()[0], 1)}
        assert cpu <= R11_UNDER_CPU_S, (name, rows[name])
    report("under-r11", rows)
    scoped = " ".join([f"{S_}/src"] * 50)
    assert r5_denied([(c, S_) for c in (f"rg foo {scoped} ~/.subfleet", f"rg foo ~/.subfleet {scoped} ~/.subfleet",
                                        f"rg foo {scoped}; rg foo {scoped} /", f"rg foo / {scoped}; rg foo /")]) == []
    assert r5_allowed([(f"rg foo {scoped}; rg foo {scoped}", S_)]) == []


#: Decision 2: review r10's P3-1 input, a function body written before a piped while-read loop over a scoped producer
#: and called after it assigns a root, and its spellings (the word function, a ~/.subfleet value, an if in the body,
#: read -r). r10, r9 and the installed hook allow each, and both shells walk the root in each ...
R11_READ_FUNCTION_DENIED = [
    "f() { d=/; }; echo src | while read d; do :; done; f; " + R7V_R,
    "function f { d=/; }; echo src | while read d; do :; done; f; " + R7V_R,
    "f() { d=~/.subfleet; }; ls src | while read d; do :; done; f; " + R7V_R,
    "f() { if true; then d=/; fi; }; echo src | while read d; do :; done; f; " + R7V_R,
    "f() { d=/; }; echo src | while read -r d; do :; done; f; " + R7V_R,
]
#: ... while a scoped value in the body, or a root assigned to another name, stays allowed; neither shell walks a root.
R11_READ_FUNCTION_ALLOWED = [
    "f() { d=lib; }; echo src | while read d; do :; done; f; " + R7V_R,
    "f() { e=/; }; echo src | while read d; do :; done; f; " + R7V_R,
]


def test_r11_a_function_body_joins_a_piped_while_read_loop():
    """Decision 2: review r10's P3-1 input and its spellings are denied; the two controls stay allowed."""
    assert r5_denied([(c, S_) for c in R11_READ_FUNCTION_DENIED]) == []
    assert r5_allowed([(c, S_) for c in R11_READ_FUNCTION_ALLOWED]) == []
