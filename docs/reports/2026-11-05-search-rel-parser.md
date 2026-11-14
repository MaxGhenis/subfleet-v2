# D-SEARCH-REL1: the Codex lane hook resolves relative search roots against the cwd

Build report for job `20261104-210909-search-rel-parser-build`, written 2026-11-07 by attempt a3.
Attempts a1 and a2 (2026-11-04 to 2026-11-06) wrote the hook change, the TRUST pin, the docs, the
tests and the mutation harness; a3 committed a2's last two edits, ran every check below and wrote
this report. Base: `release/217` at `a8624a489`. Every test here ran at `2d73365a0`, the last commit
that touches code. The data behind each table is in
[`2026-11-05-search-rel-parser-data/`](2026-11-05-search-rel-parser-data/).

## Summary

- The lane hook (`private/guard/never-rules-hook.sh`) now carries the session hook's parser,
  `unscoped_search.py`, byte for byte (SHA-256 `0ec2f7aa2cbdc1718e1424c18c8f46dac2bdafdc70faa16c9e03c7d079fdb207`,
  the hub's copy) as the body of a quoted heredoc. The hook runs the parser after the regex
  judgment, when the rule's prefilter found a searcher and the regex judgment passed. The hook
  blocks when either judgment blocks. With no interpreter, a nonzero exit, or no answer within 5 s,
  the regex judgment decides alone, as before.
- On the hub's 17 rows the hook now gives the parser's verdict under zsh and under bash. The 8 rows
  that passed before now block, and the other 9 keep their verdicts (2 blocks, 7 passes).
- Over 1,370 corpus pairs, every block the regex judgment makes keeps its output byte for byte, and
  the parser adds 24 blocks. With no interpreter, the hook's output equals the previous hook's on
  every pair.
- A command with no searcher costs nothing new: −5.3 ms and +1.8 ms median wall time, which is
  noise. A command the parser judges costs 56–85 ms more child CPU (median) and 57–156 ms more wall
  time, at load 15–20.
- All seven mutants die: the brief's six, plus one that removes the loader's alarm.
- The whole suite's 12-shard run and its counts are under [Tests](#tests).

## Files changed

| File | Change |
| --- | --- |
| `private/guard/never-rules-hook.sh` | The parser block, inside the unscoped-search region after the regex judgment: the heredoc (the parser, 2,414 lines), the `-c` loader, the interpreter choice, the call and the block message. Also the `us_ask` flag in the prefilter's branch and the header's paragraph on D-SEARCH-REL1. |
| `private/guard/TRUST` | `hook_sha256` moves from `e869dd4c…` to `af5e7239…`; nothing else changes. |
| `docs/private-guard-overlay.md` | A new section on what judges the rule now, the fallback, why the parser is not run as `python3 /dev/fd/3`, and the rollback pair, with the pair table moved to the new hashes. |
| `tests/unit/test_guard_search_rel.py` | New: the rows, the corpus comparison, the fallback, the alarm, the drift check, the cost measurement, the bytes and the arguments. |
| `tests/unit/test_guard_subfleet_root.py` | Its hook runs name no interpreter (`CODEX_GUARD_TEST_PYTHON` set to a path that does not exist), so the regex judgment decides alone, as these tests state it. Its old-hook rebuilds remove the parser block and undo the header lines. |
| `tests/unit/test_guard_overlay.py` | The reviewed pair's pinned hashes move to the new pair. |
| `tools/guard_search_rel_mutations.py` | New: the mutation harness for this change. |
| `tools/guard_subfleet_root_mutations.py` | Its pin of the current hook moves to the new hash. |
| `docs/reports/2026-11-05-search-rel-parser.md` and its `-data/` folder | This report, and the JSON it cites. |

This change leaves `docs/acceptance-contract.md` alone, because no clause in it describes the
overlay's unscoped-search rule. C-14.1 covers the pair's pinning, staging and validation, not what
the rule judges. The rule's earlier revisions (D-LANE-SCOPE1 and its rounds to r12d) left the contract
unchanged too. The one hook commit that touched it, `4c9c12cf2` (2026-09-21), moved the pair under
`private/` and rewrote C-14.1 to say so.

## The gap and the change

### The gap

The rule's regex judgment reads the command's words. It resolves an absolute root, and `.` only
when the cwd itself is one of the broad roots its `case` lists (the home folder, three of its
children, `/tmp`, `/private/tmp`, `/` and `/Users`). It never resolves a relative root against the
cwd. From a job worktree, `<home>/.subfleet/worktrees/<job>`, `find ..` walks every job worktree
and the rule let it run. On 11/04 one lane's `find ..` did that for 38 minutes.

### The change

The session hook calls `unscoped_search.py`, which splits the command as the shell does, follows
`cd` and resolves every root against the cwd. The lane hook now carries those bytes and calls them
as the session hook does, with one difference in how the bytes reach the interpreter:

- **Where.** A block marked `# >>> unscoped-search: parser begin >>>` and
  `# <<< unscoped-search: parser end <<<` sits inside the region, after the regex judgment's last
  statement and before `# <<< unscoped-search: shared region end <<<`. The function `us_parse`
  holds the parser as the body of a heredoc whose delimiter, `__UNSCOPED_SEARCH_PY__`, sits in
  quotes, so bash expands nothing in it. TRUST's `hook_sha256` therefore covers the parser's bytes, and
  staging gains no file.
- **When.** The rule's prefilter `pw_named` sets `us_ask=1` when it finds a searcher word: `find`,
  `rg`, `grep`, `egrep`, `fgrep`, `rgrep` or `ugrep`. It also counts such a word with a quote or a
  backslash inside it, zsh's `=rg`, and a `$'…'` escape that may spell one. The block runs the
  parser only then, and only when the regex judgment did not block, because `block` exits the
  hook. A command that names no rule keyword at all leaves at the hook's first prefilter and never
  reaches the region.
- **How.** The interpreter is the session hook's: `/opt/homebrew/bin/python3` when it is
  executable, else `/usr/bin/python3`. The call is
  `"$us_py" -I -S -c "$us_boot" "$cwd" "$HOME" "${SHELL:-}" 3<<'__UNSCOPED_SEARCH_PY__'`, with the
  command on stdin. The loader `$us_boot` sets SIGALRM to its default action, arms a 5 s alarm,
  reads descriptor 3 and runs the bytes as `__main__`.
- **Fallback.** With no executable interpreter, a nonzero exit (the alarm's kill included) or an
  empty answer, nothing blocks here, so the regex judgment's pass stands.
- **Message.** The parser's three lines (searcher, root as written, root resolved) fill the rule's
  existing message: `[unscoped-search] Whole-tree <searcher> over a broad root (<the roots>) — … Matched broad root: '<root as written> (<root resolved>)'`.
  The resolved root is shown only when it differs from the root as written.
- **Test seam.** `CODEX_GUARD_TEST_PYTHON` names the interpreter instead, and a path that is not
  executable counts as no interpreter. The daemon's launch environment, an exact allowlist in
  `subfleet/launch_env.py`, never passes the name to a lane, and
  `test_r5_the_test_seam_never_reaches_a_lane` checks that.

**Why a loader, not `python3 /dev/fd/3`.** The brief preferred
`python3 -I -S /dev/fd/3 "$cwd" "$HOME" "${SHELL:-}"`. Apple's Python 3.9.6 (`/usr/bin/python3`)
run that way ran nothing and exited 0 whenever descriptor 3 was a file of 22 bytes or more. Bash
3.2 writes a heredoc to a temporary file, so with the parser every command would have passed in
silence. I reproduced this on 2026-11-07:

- A two-line program in a bash 3.2 heredoc printed nothing, with status 0.
- Through `exec 3<file`, programs of 18–21 bytes ran, and programs of 22 and 23 bytes ran nothing.
- Reading a fresh open of `/dev/fd/3` moved descriptor 3's offset to the end of the file.
- Homebrew's Python 3.14.7 ran every case.
- The loader ran the two-line program under both interpreters.

The 22-byte threshold is the size of a zip end-of-central-directory record. That fits the account
attempt a1 recorded in the doc and the hook's comment: Python 3.9 probes the script path as a zip
archive, which seeks to 22 bytes before the end and reads to it, and the open of `/dev/fd/3` shares
the descriptor's offset, so the script read that follows starts at the end. I did not trace the
probe itself.

## The 17 rows

Each row ran on disk: a real home under the test's temp folder, with
`<home>/.subfleet/worktrees/20261104-210909-search-rel-parser-build/subfleet` built, and the hook
fed the Codex stdin JSON. **Before** is the r12d hook (this hook with the parser block and its
other edits removed, which hashes to the pinned `e869dd4c…`). **After** is this hook with Homebrew's
Python 3.14.7. The verdicts and messages agree under `SHELL=/bin/zsh` and `/bin/bash`. With no
interpreter, the hook's output equals the before column byte for byte on every row (`rows.json`).

| # | cwd | Command | Parser (hub) | Before | After | Matched root after |
| --- | --- | --- | --- | --- | --- | --- |
| 01 | `<wt>` | `find <home>/.subfleet/worktrees -name AGENTS.md` | block | block | block | `<home>/.subfleet/worktrees` (the regex judgment's block, unchanged) |
| 02 | `<wt>` | `find <wt>/.. -name AGENTS.md` | block | block | block | `<wt>/..` (the regex judgment's block, unchanged) |
| 03 | `<wt>` | `find .. -name AGENTS.md` | block | pass | **block** | `.. (<home>/.subfleet/worktrees)` |
| 04 | `<wt>` | `cd .. && find . -name AGENTS.md` | block | pass | **block** | `. (<home>/.subfleet/worktrees)` |
| 05 | `<wt>` | `rg --files -g AGENTS.md ..` | block | pass | **block** | `.. (<home>/.subfleet/worktrees)` |
| 06 | `<wt>` | `grep -rl AGENTS ..` | block | pass | **block** | `.. (<home>/.subfleet/worktrees)` |
| 07 | `<wt>` | `find ../.. -name x` | block | pass | **block** | `../.. (<home>/.subfleet)` |
| 08 | `<wt>` | `find ../../.. -name x` | block | pass | **block** | `../../.. (<home>)` |
| 09 | `<wt>` | `find . -name AGENTS.md` | pass | pass | pass | |
| 10 | `<wt>` | `rg -n foo subfleet` | pass | pass | pass | |
| 11 | `<wt>` | `grep -rn foo .` | pass | pass | pass | |
| 12 | `<wt>` | `cd subfleet && find .. -name x` | pass | pass | pass | |
| 13 | `<wt>/subfleet` | `find .. -name x` | pass | pass | pass | |
| 14 | `<wt>/subfleet` | `find ../.. -name x` | block | pass | **block** | `../.. (<home>/.subfleet/worktrees)` |
| 15 | `<wt>` | `find .. -maxdepth 1 -name x` | pass | pass | pass | |
| 16 | `<wt>` | `rg --files ../..` | block | pass | **block** | `../.. (<home>/.subfleet)` |
| 17 | `<wt>` | `cd .. && rg --files` | block | pass | **block** | `. (implicit) (<home>/.subfleet/worktrees)` |

The before column matches the hub's `codex_hook_today` column on all 17 rows, and the after column
matches its `parser` column on all 17. The embedded parser, run on its own as the session hook runs
it, gives the same verdict on every row under both shells (`test_r1_the_embedded_parser_gives_each_row_its_verdict`).

## Corpus comparison

The corpus is every command string the base module (`tests/unit/test_guard_subfleet_root.py`)
runs through this hook. That covers its generated corpus at scale 1, its fixed lists, the examples
in the hook's comments, the other rules' commands in real git repositories, and each row of its 76
`*_DENIED` lists, from the scoped cwd and from the row's own absolute path where it names one. That
comes to 1,370 distinct (command, cwd) pairs. `tests/unit/test_guard_overlay.py` holds no rule rows
of its own beyond the fake guard's `--fixture-deny`, which the corpus includes. No other test file
runs this hook.

Both judgments see the base module's fixture file system. The hook's regex judgment reads it
through its seam (`CODEX_GUARD_TEST_FSROOT`). The parser reads it through `FixtureOS`, a stand-in
for its `os` module that the test gives it, both inside the hook (through the interpreter seam) and
when the test loads it in process. Nothing reads a real home.

| | Pairs |
| --- | --- |
| Corpus pairs | 1,370 |
| Blocked by the regex judgment as `[unscoped-search]` | 847 |
| Blocked by the parser (in process) | 498 |
| The regex judgment blocks: the hook's output stays byte for byte | all of them |
| hf-dest denials relabelled `[unscoped-search]` (the one allowed change) | 0 |
| **Parser blocks, regex passes, prefilter found a searcher: now blocked** | 24 |
| **Parser blocks, regex passes, prefilter found none: still pass** | 6 |
| **Regex blocks, parser passes: still blocked** | 389 |
| No interpreter: output equals the r12d hook's | all 1,370 |

### The parser blocks and the regex judgment passes (30)

The hook now blocks these 24, because the prefilter found a searcher, so the hook asked the parser:

| cwd | Command | Parser: searcher, root, resolved |
| --- | --- | --- |
| `/tmp/work` | `env -C / find . -name x` | find, `.`, `/` |
| `/Users/tester/PolicyEngine/policyengine-us/policyengine_us` | `find ../../.. -name x` | find, `../../..`, `/Users/tester` |
| `/tmp/work` | `grep -d recurse foo /Users` | grep, `/Users`, `/Users` |
| `/tmp/work` | `D=~/PolicyEngine; grep -rn foo $D` | grep, `$D`, `/Users/tester/PolicyEngine` |
| `/tmp/work` | `W="$HOME/TheAxiomFoundation"; cd $W && rg foo` | rg, `. (implicit)`, `/Users/tester/TheAxiomFoundation` |
| `/tmp/work` | `export R=/; find $R -name x` | find, `$R`, `/` |
| `/Users/tester/.subfleet` | `rg --files .` | rg, `.`, `/Users/tester/.subfleet` |
| `/Users/tester/.subfleet` | `find . -name AGENTS.md` | find, `.`, `/Users/tester/.subfleet` |
| `/Users/tester/.subfleet` | `grep -r x .` | grep, `.`, `/Users/tester/.subfleet` |
| `/Users/tester/.subfleet/jobs` | `rg --files .` | rg, `.`, `/Users/tester/.subfleet/jobs` |
| `/Users/tester/.subfleet/jobs` | `find . -name AGENTS.md` | find, `.`, `/Users/tester/.subfleet/jobs` |
| `/Users/tester/.subfleet/jobs` | `grep -r x .` | grep, `.`, `/Users/tester/.subfleet/jobs` |
| `/Users/tester/.subfleet/worktrees/20261025-033945-pr257-fix-r2` | `rg --files -g AGENTS.md ..` | rg, `..`, `/Users/tester/.subfleet/worktrees` |
| `/Users/tester/PolicyEngine/policyengine-us` | `rg --files -g AGENTS.md ..` | rg, `..`, `/Users/tester/PolicyEngine` |
| `/Users/tester/.subfleet/worktrees/20261025-033945-pr257-fix-r2` | `find ../.. -name AGENTS.md` | find, `../..`, `/Users/tester/.subfleet` |
| `/Users/tester/PolicyEngine/policyengine-us/policyengine_us` | `find ../.. -name AGENTS.md` | find, `../..`, `/Users/tester/PolicyEngine` |
| `/Users/tester/.subfleet/worktrees/20261025-033945-pr257-fix-r2` | `rg --files ../../jobs` | rg, `../../jobs`, `/Users/tester/.subfleet/jobs` |
| `/Users/tester/PolicyEngine/policyengine-us` | `rg --files ../../PolicyEngine` | rg, `../../PolicyEngine`, `/Users/tester/PolicyEngine` |
| `/Users/tester/work/repo` | `rg --files $'/Users/tester/.subfleet'` | rg, `/Users/tester/.subfleet`, same |
| `/Users/tester/work/repo` | `rg --files $'/Users/tester'` | rg, `/Users/tester`, same |
| `/Users/tester/work/repo` | `env -C ~/.subfleet rg --files` | rg, `. (implicit)`, `/Users/tester/.subfleet` |
| `/Users/tester/work/repo` | `env -C ~ rg --files` | rg, `. (implicit)`, `/Users/tester` |
| `/Users/tester/work/repo` | `rg --files ${SUBFLEET_ROOT:-~/.subfleet}` | rg, `${SUBFLEET_ROOT:-~/.subfleet}`, `/Users/tester/.subfleet` |
| `/Users/tester/work/repo` | `rg --files ${HOME:-/Users/tester}` | rg, `${HOME:-/Users/tester}`, `/Users/tester` |

These 6 still pass, before and after, because the prefilter knows no `bfs` or `fd`, so neither
judgment runs (see [Open questions](#open-questions)):

| cwd | Command | Parser |
| --- | --- | --- |
| `/tmp/work` | `bfs / -name x` | bfs, `/`, `/` |
| `/tmp/work` | `fd x /` | fd, `/`, `/` |
| `/Users/tester/work/repo` | `fd -H AGENTS.md ~/.subfleet` | fd, `~/.subfleet`, `/Users/tester/.subfleet` |
| `/Users/tester/work/repo` | `fd -H AGENTS.md ~` | fd, `~`, `/Users/tester` |
| `/Users/tester/work/repo` | `bfs ~/.subfleet -name AGENTS.md` | bfs, `~/.subfleet`, `/Users/tester/.subfleet` |
| `/Users/tester/work/repo` | `bfs ~ -name AGENTS.md` | bfs, `~`, `/Users/tester` |

### The regex judgment blocks and the parser passes (389, all still blocked)

The hook keeps every one of these blocks, so none changes verdict. `corpus-disagreements.json`
lists all 389 (`regex_blocks_parser_passes`). This table groups them by the root the
regex judgment's message names, which is a rough grouping, with examples:

| Group | Pairs | Examples (cwd `/Users/tester/work/repo` unless shown) |
| --- | --- | --- |
| Paths fed by xargs, quoted words in `sh -c` scripts, `~` inside quotes, and the like | 144 | `echo '/Users/tester/PolicyEngine/' \| xargs rg foo`; `bash -c 'find -L "~/.cache/" -name x'`; `grep -rn x "~/.subfleet"` |
| A variable or expansion word | 92 | `rg -t py foo '$HOME'`; `grep -rn x "$SUBFLEET_ROOT"`; `rg -l foo '$HOME/Library/Caches' \| head -n 5` |
| A glob (the message names it with `(glob)`) | 92 | `rg foo ~/.subfleet/jobs/?*`; `rg foo $SUBFLEET_ROOT/worktrees/*`; `cd ~/.subfleet/jobs/* && rg foo` |
| A depth-bounded `find` feeding a searcher | 26 | `find ~/.subfleet -maxdepth 1 -type d \| xargs rg foo`; `find ~/.subfleet -maxdepth 1 -exec rg foo {} \;` |
| After a `cd` | 23 | `cd /Users/maxghenis && rg foo` (that home does not exist in the fixture); `cd $SUBFLEET_ROOT && find . -name AGENTS.md` |
| Too complex to judge (more than 4,096 of one of `$ # \ ' " ` and a searcher named) | 10 | `rg foo src $$$$…`; `rg foo src; echo '""""…` |
| The cwd rule | 2 | cwd `/Users/tester`: `cd ~/PolicyEngine/policyengine-us && rg foo` |

In these groups the regex judgment reads the text and blocks on a broad root that the shell would
not walk in that command: a single-quoted `'$HOME'`, `~` inside double quotes, or a path the
command echoes into xargs. The parser follows the shell's reading and passes them. Keeping the
regex judgment's blocks is what the brief asks for.

## Cost

`test_cost_before_and_after` (gated by `SEARCH_REL_TIMING`) ran the r12d hook and this hook
alternately, 20 runs each, on each row from the row's cwd and on three commands with no walk. Those
are two with no searcher (`ls -la` and `git push origin x`) and a grep filter (`ps aux | grep foo`,
which names a searcher). The interpreter was `/opt/homebrew/bin/python3` (3.14.7), and the
one-minute load was 15–20. Wall time is the hook's whole run as a subprocess. CPU is the child's
user plus system time. Times are in ms.

| cwd | Command | Before median | Before worst | After median | After worst | Added wall (median) | Added CPU (median) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `<wt>` | `ls -la` | 113.2 | 181.5 | 107.9 | 180.4 | −5.3 | 0.4 |
| `<wt>` | `git push origin x` | 232.0 | 683.5 | 233.8 | 375.7 | 1.8 | −3.3 |
| `<wt>` | `ps aux \| grep foo` | 244.7 | 368.5 | 396.5 | 475.5 | 151.8 | 60.1 |
| `<wt>` | 01 `find <home>/.subfleet/worktrees -name AGENTS.md` | 276.3 | 372.2 | 296.9 | 454.3 | 20.6 | 0.9 |
| `<wt>` | 02 `find <wt>/.. -name AGENTS.md` | 362.4 | 489.1 | 345.9 | 507.2 | −16.5 | −0.9 |
| `<wt>` | 03 `find .. -name AGENTS.md` | 296.1 | 447.4 | 452.3 | 628.9 | 156.2 | 80.8 |
| `<wt>` | 04 `cd .. && find . -name AGENTS.md` | 299.7 | 444.0 | 442.6 | 562.8 | 142.9 | 77.2 |
| `<wt>` | 05 `rg --files -g AGENTS.md ..` | 211.5 | 425.7 | 367.4 | 580.9 | 155.9 | 75.0 |
| `<wt>` | 06 `grep -rl AGENTS ..` | 150.3 | 225.8 | 238.7 | 274.7 | 88.4 | 56.1 |
| `<wt>` | 07 `find ../.. -name x` | 175.5 | 334.2 | 311.3 | 512.3 | 135.8 | 79.9 |
| `<wt>` | 08 `find ../../.. -name x` | 204.9 | 282.5 | 295.0 | 363.7 | 90.1 | 77.6 |
| `<wt>` | 09 `find . -name AGENTS.md` | 199.4 | 245.8 | 273.0 | 392.2 | 73.6 | 64.8 |
| `<wt>` | 10 `rg -n foo subfleet` | 176.3 | 281.9 | 233.6 | 420.7 | 57.3 | 56.0 |
| `<wt>` | 11 `grep -rn foo .` | 217.3 | 337.5 | 335.2 | 450.5 | 117.9 | 61.3 |
| `<wt>` | 12 `cd subfleet && find .. -name x` | 154.5 | 319.4 | 263.9 | 417.7 | 109.4 | 59.0 |
| `<wt>/subfleet` | 13 `find .. -name x` | 291.8 | 421.8 | 390.1 | 593.6 | 98.3 | 60.5 |
| `<wt>/subfleet` | 14 `find ../.. -name x` | 321.3 | 404.7 | 455.6 | 644.5 | 134.3 | 84.8 |
| `<wt>` | 15 `find .. -maxdepth 1 -name x` | 225.3 | 304.5 | 304.5 | 373.8 | 79.2 | 60.0 |
| `<wt>` | 16 `rg --files ../..` | 209.9 | 341.7 | 316.1 | 454.9 | 106.2 | 72.3 |
| `<wt>` | 17 `cd .. && rg --files` | 155.4 | 188.4 | 233.1 | 341.8 | 77.7 | 65.5 |

The added cost per command:

- **No searcher** (`ls -la`, `git push origin x`): none. The medians move by −5.3 and +1.8 ms of
  wall time and +0.4 and −3.3 ms of CPU, which is noise. The interpreter never starts
  (`test_r4_the_interpreter_starts_only_when_a_searcher_is_named_and_the_regex_passed` counts
  starts). Bash still parses the heredoc for a command that reaches the region.
- **The regex judgment blocks first** (rows 01 and 02): none (+20.6 and −16.5 ms wall, +0.9 and
  −0.9 ms CPU). The parser never runs.
- **The parser runs** (rows 03 to 17, and the grep filter): 56–85 ms more child CPU and 57–156 ms
  more wall time, median. The worst wall time after the change was 644.5 ms (row 14), against
  404.7 ms before. On this host, a bare `python3 -I -S -c pass` takes a median 32 ms under
  Homebrew's 3.14.7 (99 ms under Apple's 3.9.6), and compiling the 108 KB parser takes a median
  35 ms under 3.14.7 (17.6 ms under 3.9.6). Together the start and the compile take about 67 ms of
  wall time, close to the added CPU.

## Mutants

`tools/guard_search_rel_mutations.py --first-kill --one-export` applies each mutant to one
`git archive` export of HEAD under the scratch folder, never to this checkout, and restores and
checks the hook's bytes after each. It then runs the mutant's named tests, quickest killer first,
stopping at the first failure. The unmutated control first ran all 10 named tests, and all 10
passed. A kill is a failed test. An error in a fixture, a skip or a pass is no kill.

| Mutant | Edit | Killed by (first kill) |
| --- | --- | --- |
| (a) the hook never calls the parser | `if [ -n "${us_ask-}" ]` becomes `if [ -n "" ]` | `test_fallback_a_nonzero_exit_is_no_answer` |
| (b) the hook ignores the parser's answer | `&& [ -n "$us_hit" ]` becomes `&& [ -n "" ]` | `test_fallback_a_nonzero_exit_is_no_answer` |
| (c) the hook skips the regex judgment whenever the parser runs | the regex block's `if [ -n "$hit" ]` also requires that the command named no searcher or that no interpreter is executable | `test_r4_the_interpreter_starts_only_when_a_searcher_is_named_and_the_regex_passed` |
| (d) the parser runs on every command | `if [ -n "${us_ask-}" ]` becomes `if :` | `test_r4_the_interpreter_starts_only_when_a_searcher_is_named_and_the_regex_passed` |
| (e) no fallback: with no interpreter, nothing blocks | the regex block's `if [ -n "$hit" ]` also requires an executable interpreter | `test_fallback_an_interpreter_that_cannot_run_is_none` |
| (f) one byte of the embedded parser changed | `analyzer` becomes `analyser` in its docstring | `test_r5_the_heredoc_holds_the_hub_parser_byte_for_byte` |
| (g) (this change's own) the loader arms no alarm | `signal.alarm(5)` removed | `test_fallback_no_answer_within_5_s_is_no_answer` |

The output is in `mutants.jsonl`. With `--first-kill`, each mutant's later named tests did not run
against it. Those are the rows test for (a) to (c), the argument, overlay-pin and TRUST-pin tests
for (f), and the alarm test for (g).

**(c) and the corpus.** On the 17 rows, (c) changes no verdict. Rows 01 and 02, the only rows the
regex judgment blocks, are rows the parser blocks too, so they still block, but with the parser's
message instead of the regex judgment's. The rows test catches that difference, because it asserts
that a row the regex judgment blocks keeps its output byte for byte. In the corpus, (c) does change
verdicts. `mutant-c-corpus.json` ran the 389 pairs that the regex judgment blocks and the parser
passes through the (c) mutant, with the fixture interpreter. The mutant passes 377 of them, where
the unmutated hook blocks all 389. Of the other 12, 10 are "too complex to judge" blocks, which
come before the parser block. The other 2 are a `cd … && apply_patch` heredoc that never ends,
which the hf-dest rule denies once the region lets it through. The corpus tests themselves cannot
kill (c) or (e) in this harness, because those edits sit outside the parser block. The r12d rebuild
those tests use then no longer hashes to its pin, and the fixture errors rather than fails, so the
harness names tests that do not rebuild r12d.

## Tests

The commands below ran in the job worktree. Each ran with `PYTHONPATH` unset, and `TMPDIR` and
`UV_CACHE_DIR` inside this attempt's temp folder. `uv sync --group dev` came first.

| Command | Result |
| --- | --- |
| `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -p no:cacheprovider -n 4 --basetemp=… tests/unit/test_guard_search_rel.py -k 'not corpus and not r3_with and not r2_'` | 79 passed, 2 skipped (drift: `SUBFLEET_CLAUDE_UNSCOPED_SEARCH` unset; cost: `SEARCH_REL_TIMING` unset), 245 s at load 85–95 |
| `GUARD_DIFF_CACHE=… SEARCH_REL_REPORT=… pytest -q -p no:cacheprovider --basetemp=… tests/unit/test_guard_search_rel.py -k 'corpus or r3_with or r2_' --timeout=560` | 3 passed, 248 s. Two earlier runs stopped at their 580 s and 560 s timeouts while the corpus fixture filled the verdict cache, which keeps each verdict as it lands. |
| `SEARCH_REL_TIMING=… pytest … -k test_cost_before_and_after` | 1 passed, 224 s (the table above) |
| `SUBFLEET_CLAUDE_UNSCOPED_SEARCH=<file> pytest … -k test_drift`, with the file set to the hub's copy, a copy with one byte changed, and a path that does not exist | passed, failed, skipped |
| `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -p no:cacheprovider -n 4 --basetemp=… tests/unit/test_guard_trust.py tests/unit/test_doctor.py tests/unit/test_guard_overlay.py` | 174 passed (105 + 47 + 22), 40 s |
| `GUARD_DIFF_CACHE=… uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -p no:cacheprovider -n 4 --basetemp=… tests/unit/test_guard_subfleet_root.py` | 158 passed, 2 skipped (S4: the operator's parser path unset), 399 s. An earlier run reached 92% before a 570 s deadline stopped it. |
| `python tools/guard_search_rel_mutations.py --scratch <…/sf-a3/mut> --first-kill --one-export --only a b c d e f g` (full names) | control 10 of 10 passed; 7 of 7 mutants killed, exit 0, 141 s |
| `mutant_c_corpus.py` (scratch script; output in `mutant-c-corpus.json`) | 389 pairs: the unmutated hook blocks 389, mutant (c) passes 377 |

`tests/unit/test_guard_search_rel.py` thus has 84 tests: 82 passed and 2 gated. Both gated tests
also ran with their variables set (cost: passed; drift: passed, failed on a changed byte, skipped
on an absent file).

### The whole suite in 12 shards

Pending at this commit: the shards run next, and a later commit records their counts here.

## Deviations from the brief

1. **The loader.** The parser runs as `python3 -I -S -c <loader> "$cwd" "$HOME" "${SHELL:-}"`,
   with the parser on descriptor 3 from a quoted heredoc and the command on stdin, not as
   `python3 -I -S /dev/fd/3 …`. Apple's Python 3.9.6 runs nothing for a program of 22 bytes or more
   on a file-backed descriptor 3, as above. The hook still embeds the parser, so the trust pin still
   covers it, and staging gains no file.
2. **The 5 s bound is an alarm inside the interpreter.** The loader arms `signal.alarm(5)` before
   it reads the parser, and the alarm's default action ends the process, a nonzero exit, so the
   regex judgment decides. macOS ships no `timeout` command. The interpreter's start before the
   loader runs falls outside the 5 s.
3. **The drift test names its file.** `test_drift_the_embedded_parser_is_the_session_hooks`
   compares the embedded bytes with the file that `SUBFLEET_CLAUDE_UNSCOPED_SEARCH` names (for
   example `~/.claude/hooks/unscoped_search.py`). It skips when the variable is unset or the file
   is absent. It does not probe `~/.claude/hooks/unscoped_search.py` itself: that file exists on
   this host, so such a test would run, and read `~/.claude`, in a lane, while the brief asks it to
   stay skipped there and forbids reading `~/.claude`. The base module's S4 uses the same variable.
4. **An existing test module changed.** `tests/unit/test_guard_subfleet_root.py` judges in a fixture
   file system that the parser cannot see. Its hook runs now name no interpreter, so the regex
   judgment decides alone, as its tests state it. The new module's corpus tests judge both halves
   together, with `FixtureOS` giving the parser the same fixture.
5. **Extras.** The cost runs include a third command with no walk (a grep filter), and the
   mutation set includes (g), the alarm.
6. **History.** Commit `40275e6b0`, from an earlier attempt, is a `fixup!` of `7cd4d56e1` that adds
   `tests/unit/test_guard_search_rel.py`, and its message has no body. The brief forbids rewriting
   history, so it stays, and the hub may squash it at merge.
7. **Long runs were split.** The headless lane caps a command at 600 s, so the corpus tests and the
   base module ran more than once, resuming from `GUARD_DIFF_CACHE`. A small wrapper in the scratch
   folder stopped a command's process group at a deadline. The whole suite ran one shard per
   command (see above).

## Open questions

1. **`bfs` and `fd`.** The rule's prefilter knows `find`, `rg` and the grep family, and the parser
   also judges `bfs`, `fd`, `fdfind` and `gfind`. The 6 corpus pairs above (`bfs / -name x`,
   `fd x /`, …) pass the hook before and after this change. Widening the prefilter to the parser's
   searcher words would close them, at the cost of a parser start for every command that names one.
   The brief limited the parser to the prefilter's commands, so this build does not widen it.
2. **grep filters start the parser.** Any command the regex judgment passes and that names grep
   (`ps aux | grep foo`) starts the interpreter: +60 ms CPU and +152 ms wall time here. The session
   hook skips its parser when grep is the only searcher and no flag could make it recursive
   (`session-hook-unscoped-block.sh.txt`, its fast path). Taking that fast path into the lane hook
   would remove the cost. The brief asked for the rule's prefilter, so this build does not take it.
3. **The regex judgment's over-blocks.** 389 corpus pairs block only because of the regex judgment,
   for example a single-quoted `'$HOME'`, `~` inside double quotes, or a heredoc that never ends.
   Whether the parser's pass should overrule some of those classes is a policy call. This build
   keeps every regex block.
4. **Parser start cost.** Most of the added CPU is the interpreter's start and the compile of the
   108 KB source on every run. The loader could cache compiled bytes, but that would add a file the
   trust pin does not cover. This build leaves it.
