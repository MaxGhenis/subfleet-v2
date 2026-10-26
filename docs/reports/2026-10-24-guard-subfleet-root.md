# The lane guard refuses whole-tree searches of ~/.subfleet (D-LANE-SCOPE1), 2026-10-24

At 15:32Z on 2026-10-24 a Codex lane ran `rg --files -g AGENTS.md /Users/maxghenis/.subfleet`
for 4 minutes with 12 threads at load 190–220. It walked every job folder (14,121) and job
worktree (1,765). The never-rules hook's `unscoped-search` rule refuses find/rg/grep -r over
`/`, `/Users`, `~`/`$HOME` and some of their children and `/tmp`, but `~/.subfleet` was not
one of them.

Base: `release/217` at `abe117e67`. Round 2 (2026-10-25) merged `release/217` at `8f1c65f69`
and answers the hub's review r1 (REQUEST_CHANGES; P2-1 to P2-4, P3-1 to P3-7); the hub then
merged `release/217` at `0ab7cebaf` (contract wording only, `docs/acceptance-contract.md`).
Round r3 (2026-10-25, "r3" below) judges path words on disk: a file is never a broad root, a
trailing `/*` after a root is that root, and a glob that can match a root is judged by what it
matches. Nothing here touches the live guard; the hub installs it with the steps below.

## What changed

**The hook** (`private/guard/never-rules-hook.sh`, SHA-256 `2c3fbc1a…d7cf` after r3; r2's
was `dbe21e1d…041c`). Two alternations join the roots in `broad_re`:

```
…|Library/Caches|\.cache|\.local|\.subfleet(/[^/[:space:]'\"]+)?))?|\\\$SUBFLEET_(ROOT|HOME)(/[^/[:space:]'\"]+)?|(/private)?/tmp
```

So `~/.subfleet` and any one path component directly under it are broad roots, in every
spelling the existing home children have: `~`, `$HOME`, `${HOME}` (judged as `$HOME`) and
`/Users/<user>`, with or without a trailing slash, quoted wholly or partly or not at all. Two
or more levels down (`~/.subfleet/jobs/<id>`, `…/<id>/a1/deliverable.md`) stays scoped, and
any `-maxdepth`/`--max-depth` still disarms the rule. The block message's root list gains
`~/.subfleet, ~/.subfleet/<child>`.

**r2: the names lanes have for that folder** (review r1, P2-3). The daemon sets
`SUBFLEET_ROOT` to its root on every attempt (`subfleet/daemon.py`, the attempt environment),
and `SUBFLEET_HOME` is on the launch allowlist (`subfleet/launch_env.py:23`); the review found
both equal to `$HOME/.subfleet` in its lane. `$SUBFLEET_ROOT` and `$SUBFLEET_HOME` are now
spellings of `$HOME/.subfleet` under the same depth rule: the variable alone, or with one
component after it, is a root; two or more components stay scoped; any depth bound disarms it.
The `judged` view drops the braces of `${SUBFLEET_ROOT}` and `${SUBFLEET_HOME}` exactly as it
does for `${HOME}` (one `sed` on both `judged` lines), and the existing quote handling unquotes
`"$SUBFLEET_ROOT"`, `"$SUBFLEET_ROOT/jobs"` and `"$SUBFLEET_ROOT"/jobs` as it does the `$HOME`
forms. The hook reads the name, not the value: `rg --files "$SUBFLEET_HOME"` is denied
whatever folder the variable holds, which for a lane is the daemon's state root.

The region's own `roots` and `judged` comments and the header say so; the header's sync
paragraph records that the region is edited in this file on hub decisions (the Claude hook's
2026-10-20 rewrite moved its rule to `unscoped_search.py`, so the byte-identical contract and
its skip-gated DriftTests are historical). Nothing else changed:
`test_only_the_roots_the_header_and_the_message_changed` undoes exactly these edits (the text
edits, the two `sed` edits and the `broad_re` line) and gets the original reviewed bytes
(`a60d1c51…`) back.

**TRUST** (`12b5f31a…58a2` after r3; r2's was `aa09f53d…a2f0`). It is built on the installed TRUST of 2026-09-29 (`b3193b97…`, the
`codex-cli 0.159.0` pin), not on the repository's copy at `abe117e67` (`0b9d688e…`, which still
pins `codex-cli 0.153.3`; a TRUST built on it would refuse every Codex lane at preflight with
VERSION, `subfleet/guard/preflight.py:649-650`). It has the same keys. `hook_sha256` is
recomputed for the new bytes, and `provenance` gains three dated additions: D-LANE-SCOPE1's three
sentences, ending "Installed only after the hub's review and a live hooks/list preflight.",
r2's one sentence, naming the env spellings, and r3's one sentence, naming the path words judged
on disk. Nothing else moved. `hooks_trust_hash` (`sha256:f97fa95f…`) and
`override` are unchanged: `subfleet/guard/preflight.py:hooks_trust_hash` hashes the hook's
identity (event, matcher, type, command path, timeout, async flag, status message), not its
bytes. Checked three ways: `hooks_trust_hash(reference_hook_path)` still equals the pin; the
old and the new bytes written at one path give one hash and one override string; and the
installed path's identity, `sha256:29adec11fc23b970487de36f80282b1656783a8470f561d4da3c6d1eccbae1f6`
(computed from the path string, the file was not read), is what the live preflight below must
see as hooks/list's `currentHash`, before and after the install.

**The overlay pin** (`tests/unit/test_guard_overlay.py::test_private_overlay_bytes_remain_the_original_reviewed_pair`)
moves to the new pair. Its docstring names the rollback (hook `a60d1c51…` with the installed
TRUST `b3193b97…`) and says the 2026-09-05 TRUST (`0b9d688e…`) must never be staged. No other
code reads `private/guard/`, and of the never-rules test set (`git grep -l -e never-rules -e
never_rules -e guard_hook -- tests`: 13 paths at `e82e45bff`, which is 9 test modules, the
overlay test, the new file and 7 more; 17 paths and 12 modules after the release merge at
`8f1c65f69`, whose 3 new modules come with #221) only the overlay test and the new file read
it; the rest drive the synthetic `tests/fake/guard.py` hook.

**The Claude half is no longer this PR's** (the hub's note of 2026-10-25T17:16Z): session
7f214082 owns `~/.claude/hooks/unscoped_search.py` and folds these roots into its own rounds; the
copies below were built on its round 1 and must never be installed. The hub runs S4 against the
installed analyzer once 7f214082 says the matching round is live. As built in r1 and r2:
`.hub-out/claude-hook/unscoped_search.py`
and `.hub-out/claude-hook/test_unscoped_search.py` in the build worktree (untracked).
`BROAD_RE` gains `| \.subfleet (/[^/]+)?` under `/Users/<user>`, and `HOME_SUBDIRS` gains
`".subfleet"` and `".subfleet/*"`, where a trailing `/*` means any one path component directly
under it (`is_broad` reads that form). r2: `expand()` reads `$SUBFLEET_ROOT` and
`$SUBFLEET_HOME` (also braced, quoted or not) as `<home>/.subfleet` unless the command assigned
the variable first, and the parser-failure fallback does the same. The test copy gains the
review's four denials and three allows as examples, more examples (a prefix assignment, which
the shell expands after the word, an assigned value, pattern position, near-miss names), named
roots in `test_hit_names_the_root`, the property `test_env_spellings_decide_as_home_subfleet`,
env tokens in the totality strategy, and env-spelled paths in the strategy of
`test_hook_agrees_with_analyzer`, so the hub's run with the bash hook checks that its fast path
hands those commands to the analyzer.

## r3: path words judged on disk (2026-10-25)

**Why.** Session 7f214082 owns the Claude half of this rule. It replayed 115,165 real Claude
Bash commands (07/14–10/25) through its analyzer with the new roots: "46 newly block, and 13 of
those are recursive greps of one file, which walk nothing": `grep -rn -i 'retention'
~/.subfleet/policy.json`, from 12 different sessions, and one on `~/.subfleet/daemon.log`. The
hub reproduced that on this overlay, and its probe of the live analyzer found two more
differences: a bare `/*` after a broad root (`rg foo ~/.subfleet/jobs/*` hands rg every job
folder, and this overlay let it pass) and selective globs (`rg foo ~/Pol*` expands to a broad
root).

**The decisions as built.** Before the text rules, every path word of the command is judged
once: a run of characters other than whitespace and `| ; & ( ) < >`, its quote marks dropped,
that starts with `~`, `$HOME`, `$SUBFLEET_ROOT` or `$SUBFLEET_HOME` (alone or before a `/`), or
`/`. The words that move are rewritten, quote marks kept, so every existing rule (path
position, cd, for-loop, xargs, sub-scripts) reads the rewritten command. The code is three
blocks between `r3 path words` marker lines; the fence test removes them and gets r2's bytes.

1. **A file is never a broad root** (reversing r2's decision 6, P3-1). A root by name that
   resolves to an existing non-directory, `[ -e "$p" ] && [ ! -d "$p" ]` (both follow links),
   becomes the neutral placeholder `F`, and judging continues with the command's other words.
   `~`, `$HOME` and `${HOME}` resolve to the hook's `$HOME`; `$SUBFLEET_ROOT` and
   `$SUBFLEET_HOME` (and their `${…}` forms) to `$HOME/.subfleet`, never to the environment's
   values; `/Users/<user>` is itself. Judged by name, as before: a missing path, a dangling
   link, a path that cannot be checked, one still holding `$` or a backtick, and a trailing
   slash (`policy.json/` fails `-e`). It applies to every root the regex can match; in
   practice only the variable components can be files: `~/.subfleet/<child>`,
   `$SUBFLEET_*/<child>` (and `/Users/<name>`, where `/Users/.localized` is one).
2. **A trailing `/*` after a root is that root**, for every root: `~/*`, `~/PolicyEngine/*`,
   `~/.cache/*`, `/tmp/*`, `~/.subfleet/*`, `~/.subfleet/jobs/*`, `$SUBFLEET_ROOT/worktrees/*`.
   Built as the Claude analyzer's reference semantics (`path_of`, `tree_globs`) have it: a
   trailing run of bare `*` or `.*` components (or a last `**`), after one trailing slash, is
   the folder before it (`~/.subfleet/jobs/*/*` too), and a `**/` component makes the part
   before it the root (zsh walks it). Always broad when that part is a root by name, whatever
   it is on disk (`~/.subfleet/policy.json/*` is the trailing-slash case). `/*` itself stays the
   text rule's root; `~/.subfleet/jobs/<id>/*` stays scoped.
3. **A glob that can match a root is judged by what it matches.** A word with a glob character
   (`*`, `?`, `[`) whose literal folder (the part before the first globbed component) is `/`,
   `/Users`, `/private`, a home (any spelling, `/Users/<user>` included), its `Library` or its
   `.subfleet` is expanded by bash itself: the pattern is an unquoted variable expansion, which
   never runs a substitution, with `set +f`, `nullglob`, and `dotglob`, `nocaseglob`, `extglob`,
   `failglob` and `GLOBIGNORE` off, all restored afterwards. Each of the first 20,000 matches
   is taken as a written path (`//`, `.` and `..` resolved, the home spelled `~`) and is broad
   if it is a root by name that decision 1 does not clear. Broad: the word becomes a placeholder
   root. Not broad, or no match at all: it becomes `F` (zsh aborts on no match; bash passes the
   literal, which names no path). So `~/.subfleet/*.json` over files passes, and
   `~/.subfleet/x*` passes where the text rule would deny it; `~/.subfleet/j*`, `~/.sub*`,
   `~/Pol*`, `~/P?licyEngine`, `~/[P]olicyEngine`, `~/Lib*/Cach?s` and `~/Pol*/**/*.py` are
   denied. Any other glob word, and one holding `$`, a backtick or `{` (which the shell expands
   before it globs), is judged by its text, as before.
4. **Small folders stay roots.** Every direct child of `~/.subfleet` stays a root, `leases`,
   `guard`, `state`, `inbox`, `bin` and `docs` included: 7f214082's replay found 9 such blocks in
   three months, each one a lane rescopes with `--max-depth` or a deeper path, and a list of only
   the big folders would go stale as new ones appear.
5. **Messages.** A denial for decision 2 or 3 names the word as written plus ` (glob)`:
   `Matched broad root: '~/.subfleet/jobs/* (glob)'.`, `'~/Pol* (glob)'`, `'~/Pol* (glob) (after cd)'`.

**Test seams.** Two environment names, `CODEX_GUARD_TEST_FSROOT` (a folder that stands in for
`/` in every check made on disk) and `CODEX_GUARD_TEST_GLOB_CAP` (the match cap). The daemon's
launch environment is an exact allowlist (`subfleet/launch_env.py`) and carries neither to a
lane (`test_r3_test_seams_never_reach_a_lane`). Every hook run in the tests judges paths in a
fixture filesystem through the first, so no test stats, globs or reads a real home: the corpus
names `/Users/maxghenis/.subfleet`.

**Deviations from the brief, each with its counterexample.**
- The brief listed `rg x $OTHER/.subfleet/policy.json` as denied. Every hook allows it, the old,
  r2 and r3: `$OTHER` is no root spelling, so nothing in it is a root by name, and making it one
  would add a spelling no decision names. The case it stands for, a root by name whose path still
  holds `$` after resolving, is judged by name: `rg x ~/.subfleet/$OTHER` stays denied though a
  file named `$OTHER` exists there (`test_r3_an_unresolved_spelling_is_no_root_spelling`).
- Decision 2 is built as its reference semantics have it (a trailing run, `.*`, `**/`), not
  only a single bare `/*`: `rg foo ~/.subfleet/jobs/*/*` hands rg every attempt folder of every
  job, the incident's walk.
- Decision 3 leaves a glob word holding `$`, a backtick or `{` to the text rule: the shell
  expands those first, so expanding the literal would pass `rg x ~/.subfleet/{jobs,x}*`, which
  r2 denies.
- The cap counts matches judged. Bash expands the whole word before the hook reads a match; that
  is the work the shell does anyway when it runs the command, and the listed folders are small.

**Hashes for the install.** The new pair: hook
`2c3fbc1a6d563ee28e47d172343508d0613a9e7bb75567ec82a60e702980d7cf`, TRUST
`12b5f31ae63c036f56f08d2fe9ab83154eab84bb1f6884d5d8b220a98ac758a2`. The installed pair step 0
expects, and the rollback: hook `a60d1c51…`, TRUST `b3193b97…` (unchanged by r3).

## Invariants and how they were checked

`tests/unit/test_guard_subfleet_root.py` states each in its docstring and checks it.

- **S1, monotone.** The new hook denies every command the old one denies, and the decisions
  differ only where the command's search root is a new root (a searcher's path position, a
  cd/pushd target, a for-loop list feeding find, or anywhere in a command where xargs runs a
  searcher, that rule's documented over-approximation). The corpus: this file's examples,
  every backticked command in the hook's unscoped-search comments, the Claude hook's example
  table, the reviews' inputs, commands for every other rule (in scratch git repositories for
  the cwd-dependent ones) alone and with a new-root search appended in a home and an env
  spelling, the cwd-rule cases, the r2 known limits and over-approximations, and a seeded
  generator (seed 20261024) of find/rg/grep commands over a root pool in every spelling, with
  and without a depth bound, unquoted, double- and single-quoted, plain, in
  `bash -c`/`sh -c`/`eval`, piped to `head`, after cd, in a for loop, and through xargs. The
  env spellings enter the pool as S5's twins. A second check makes the new roots at least as
  protected as the existing home children: wherever the old hook denies a command over
  `~/.cache` or `~/Library/Caches`, the new hook denies the same command over `~/.subfleet` or
  `~/.subfleet/<child>`.
- **S2, scoped stays allowed.** Depth-bounded searches and searches two or more levels under
  `~/.subfleet`, in every spelling including the env ones, are allowed by the old hook and the
  new.
- **S3, no other rule moves.** For every command the old hook denies under another rule
  (tg-getupdates, keychain-dump, trust-root, corpus-squash, corpus-push, stash-shared,
  local-main, hf-dest), the new hook's stdout is byte-identical and its denial-log line
  identical but for the timestamp, alone and with `; rg --files ~/.subfleet` or
  `; rg --files $SUBFLEET_ROOT` appended. One case is intended: a Bash command carrying an
  apply_patch body is judged by the hf-dest heredoc scan, which runs after the unscoped-search
  region, so with a new-root search appended both hooks deny it and the new one says
  `[unscoped-search]`. apply_patch payloads are judged identically.
- **S4, the Claude half agrees.** On plain explicit-path cases (every spelling, the env ones
  included, child, deep and near-miss paths, slash and quoting) the patched analyzer and the
  Codex hook agree outright. On the generated cases the analyzer disagrees with the Codex hook
  on a new root only where it already disagrees on the matching existing home child (the two
  designs differ on quoted `~` and single-quoted `$…`, a searcher right after a quote, xargs
  input, cd plus a relative path, globs and the cwd list, and the new roots inherit exactly
  those). An env-spelled twin is compared with its reference, the same draw over
  `$HOME/.subfleet` (`${HOME}/.subfleet` for a braced twin), which the shell expands exactly as
  it does the twin in every quoting: each half must give the twin its reference's verdict, so
  the analyzer differs from the hook on a twin only where it does on that reference. (Round r2
  compared a twin with its source command instead, which may spell the root `~` or
  `/Users/<user>`; quoting changes those differently from a variable, and S4 failed on exactly
  that at scales 1 and 2: `grep -r x '$SUBFLEET_ROOT/'`, `cd '${SUBFLEET_HOME}/jobs' && rg foo`
  and `sh -c "rg --files \"$SUBFLEET_ROOT/jobs\""`.) Two or more levels under `~/.subfleet`
  the analyzer blocks nothing. These tests read the analyzer from
  `SUBFLEET_CLAUDE_UNSCOPED_SEARCH` and skip without it, since it lives in the operator's home.
- **r3 in S1.** A command holding a path word decisions 1-3 judge on disk (`r3_labels`) is an
  intended difference, counted apart (`r3_intended`, split into loosened and tightened), and
  S1-r3 holds each decision to its direction against the r2 hook, rebuilt from this one: a
  decision 1 word only allows more, a decision 2 word only denies more, and an unlabelled
  command gets r2's verdict exactly. The corpus now runs in a fixture filesystem with files,
  directories and links under `~/.subfleet`, so decision 1 meets real files.
- **r3's property** (Hypothesis, seed 20261025, 2,000 examples). Over the child's kind (file,
  directory, link to a file, link to a directory, dangling link, missing), its spelling (the
  eight forms of `~/.subfleet`), its quoting (none, single, double, partial), a trailing slash
  and a trailing `/*`, and over 180 glob words: a plain word is broad iff its resolved path is
  not an existing non-directory, always with a trailing `/*`; a glob word is broad iff some real
  bash match is a broad root, and allowed when nothing matches. The hook denies iff broad.
- **r3's shell differential.** Every glob case is expanded by real `/bin/zsh -f` and
  `/bin/bash` in the fixture home, and the hook denies iff a real match is a broad root, in each
  shell (a `**/` or trailing `/*` run judged by the part before it, as decision 2 says).
- **S5, env spellings are spellings** (r2). For every generated command over
  `<home>/.subfleet` or anything under it, swapping that prefix for `$SUBFLEET_ROOT`,
  `${SUBFLEET_ROOT}`, `$SUBFLEET_HOME` or `${SUBFLEET_HOME}` leaves the new hook's decision
  unchanged: the twin gets the decision of the same command over `$HOME/.subfleet`
  (`${HOME}/.subfleet` for a braced twin) and of its source, whatever home spelling the source
  used; for the near-miss names `.subfleetx` and `.subfleet-old` the braced spellings
  (the ones the shell reads as that path) do the same. The twin shares its source's context,
  searcher, quoting, slash and depth bound. Scale 1 swaps in every env spelling; scale N ≥ 2
  one per command, cycling with the home spelling, so every (root, context) cell meets all four.

`GUARD_DIFF_SCALE=1` (the default, CI's) crosses a representative root pool with every spelling
and spreads the contexts over the spellings; `N >= 2` is the full pool, every context, `N − 1`
draws per cell. `GUARD_DIFF_REPORT=<file>` writes the counts. `GUARD_DIFF_CACHE=<file>` keeps
each hook verdict (keyed by the hook's SHA-256, the cwd and the command, for cwds that do not
exist, which carry no git state) so a long run resumes across suite holds.

Mutants (`tools/guard_subfleet_root_mutations.py`, each on a `git archive` of HEAD in a scratch
directory; a kill is a failed assertion in the named test):

| Mutant | Killed by |
| --- | --- |
| the new roots removed | `test_review_inputs_are_denied` |
| the child form dropped (`~/.subfleet` only) | `test_each_direct_child_is_a_root` |
| a prefix match (`~/.subfleet` and everything under it) | `test_two_or_more_levels_under_subfleet_stay_scoped` |
| the trailing-slash form missed (roots in their own `root_core` branch, without the shared `/?`) | `test_trailing_slash_spellings_are_denied` |
| TRUST's hash left stale (the new TRUST with the old hook's hash) | `test_trust_pins_the_new_hook_bytes` |
| TRUST built on the repository's stale copy (`codex-cli 0.153.3`) | `test_trust_keeps_the_installed_lane_pin` |
| r2: the env spellings dropped (their `broad_re` alternative and their `${…}` normalization) | `test_env_spellings_of_subfleet_are_denied` |
| r3: the file exception removed | `test_r3_a_file_is_never_a_broad_root` |
| r3: `! -d` dropped (a link to a directory passes) | `test_r3_a_directory_a_link_to_one_and_a_name_stay_roots` |
| r3: `-e` dropped (a missing path passes) | `test_r3_a_directory_a_link_to_one_and_a_name_stay_roots` |
| r3: the environment's `$SUBFLEET_ROOT`/`$SUBFLEET_HOME` values used | `test_r3_env_values_are_never_read` |
| r3: a glob resolved as a single file | `test_r3_selective_globs_are_judged_by_what_they_match` |
| r3: the dot rule dropped (`dotglob` on) | `test_r3_selective_globs_are_judged_by_what_they_match` |
| r3: a no-match glob judged by its text | `test_r3_selective_globs_are_judged_by_what_they_match` |
| r3: case-insensitive matching (`nocaseglob` on) | `test_r3_selective_globs_are_judged_by_what_they_match` |
| r3: a trailing `/*` left as text | `test_r3_a_bare_star_after_a_root_is_that_root` |
| r3: the expansion state not restored | `test_r3_glob_expansion_restores_every_setting` |

Round r3 did not reach the driver: its hold was the third of five in the brief's order, and
the suite lock stayed contended (load 100-240, both slots taken by other sessions' holds of 15-50
minutes) until the round's 4-hour mark. The driver asserts that each r3 mutant's text occurs
exactly once in the hook; that check passed for all ten when they were written. The mutants are
unrun.

### Results

Round r3's holds, through `suite_hold.sh` (round r2b's runs were cut off by the 2026-10-25
network outage and are replaced by these):

| Hold | Run | Result |
| --- | --- | --- |
| A, 22:02:13Z, load 138 | red: the r3 cases at `dcc8dbbab` (the new tests beside the r2 hook) | 9 failed, 1 passed (the "stays a root" cases, which r3 does not move), 389 s |
| B, 00:20:16Z, load 119 | `tests/unit/test_guard_subfleet_root.py` at `e4d02bbf7`, scale 1, r3's tests ordered first | the first 30 of 53 items passed; the hold's 1,140 s alarm ended it in the property test |

The 30: the three TRUST tests, the identity-hash test, the fence, both known-limit pins, and
every r3 test but the property, the shell differential and S1-r3 (the shell-state test's 11
cases included). Unrun, for the hub: the property (seed 20261025, 2,000 examples), the zsh/bash
differential, 13 example tests (the review's and r2's inputs, spellings, children, trailing
slashes, depth, near misses, the log line, S3), the corpus tests at scale 1 (S1, S1's
protection, S1-r3, S2, the cwd list, S5; S4 skips without the analyzer), the overlay test (22
tests), the mutation driver, the 7 other never-rules modules and S1-S5 at scale 2. The verdict
cache of hold B (305 verdicts) resumes the scale-1 corpus.

## Known limits

The decision fenced the edit to the roots, so these stay as they were and apply to the new
roots exactly as to the old ones. `test_known_limits_the_new_roots_share_with_the_old_ones` and
`test_known_limits_r2_lists_are_pre_existing_for_every_root` (both marked
`documented_limitation`) pin them, each r2 entry beside an existing-root command of the same
shape that the old hook also allows; a follow-up must change them deliberately. The hub folds
the first three entries below into D-LANE-SCOPE2.

- **Relative paths from a lane's own worktree** (review r1, P2-4). Job worktrees live at
  `~/.subfleet/worktrees/<id>`, so from that cwd `rg --files -g AGENTS.md ..` walks every
  worktree, `find ../.. -name AGENTS.md` is the incident's walk and `rg --files ../../jobs`
  walks every job folder; all three pass. Pre-existing for every root: `rg --files -g AGENTS.md ..`
  from `~/PolicyEngine/policyengine-us`, `find ../.. -name AGENTS.md` from
  `~/PolicyEngine/policyengine-us/policyengine_us` and `rg --files ../../PolicyEngine` from
  `~/PolicyEngine/policyengine-us` pass the old hook (and `find ../../..` from `~/PolicyEngine/…`).
  The Claude analyzer resolves the cwd and blocks all three.
- **A searcher right after a quote is not detected** (found by the first build's differential).
  The searcher regexes want a space, `;`, `&`, `|`, `(` or the line start before `find`/`rg`/
  `grep`, so a sub-script that starts with one passes: `bash -c 'find / -name x'`,
  `sh -c "rg foo ~"`, `eval 'grep -r x ~/.subfleet/jobs'` and `bash -c 'rg --files $SUBFLEET_ROOT'`
  are allowed today by the live hook. `bash -c 'cd / && find . -name x'` and
  `bash -c ' find / -name x'` are denied. The Claude analyzer parses sub-scripts and blocks all
  of them. This is the largest gap the differential found; it predates D-LANE-SCOPE1 and needs
  its own decision (allowing `'` and `"` before the searcher in the three detection regexes is
  the obvious repair; it changes the prefilter literal the Claude-era drift tests pinned).
- **`xargs -n1 find`** (review r1, P3-3, seen at scale 2). `printf '%s\n' ~/.subfleet/ | xargs -n1 find`
  passes: a `find` that ends the command is not detected as a searcher (the detection and the
  prefilter want whitespace after it). Pre-existing: `printf '%s\n' ~/ | xargs -n1 find` passes
  the old hook.
- **Other gaps shared with every root** (review r1, P3-3), each pre-existing:
  - spellings the rule does not normalize: doubled or dotted slashes (`~//.subfleet`,
    `~/.subfleet//jobs`, `~/.subfleet/./jobs`, a trailing `/.` or `/..` after a child); quoting
    inside the path (`~/".subfleet"`, `$'…'`); other names for the folder (`~maxghenis/.subfleet`,
    `~/.Subfleet`, which APFS reads as the same folder, `/System/Volumes/Data/…`); a glob in a
    middle component under a folder that cannot hold a root by name (`…/jobs/*/a1`; r3's
    decision 2 took the trailing `/*` out of this list); `env -C ~/.subfleet rg --files`; and parameter
    forms of the env spellings other than `$NAME` and `${NAME}` (`${SUBFLEET_ROOT:-~/.subfleet}`,
    as `${HOME:-…}` today);
  - shell structure: a quoted for-loop list (`for d in "$HOME/.subfleet"; do find $d …; done`
    passes; the unquoted form is denied);
  - searchers the rule does not know: fd, bfs, ag, `git grep --no-index`, tree, du and `ls -R`.
- **The cwd list is unchanged.** `rg --files .` with a payload cwd of `~/.subfleet` passes; the
  list never held the other home children either (`Library/Caches`, `.cache`, `.local`). A
  Codex lane's cwd is its job workdir, normally a worktree two levels under `~/.subfleet`.
- **r3 reversed r2's decision 6: a file one component down is not a root.** r2 kept
  `rg -n ERROR ~/.subfleet/daemon.log`, `rg -n sol61 ~/.subfleet/policy.json` and
  `grep -rn codex ~/.subfleet/lanes.json` denied (review r1, P3-1); r3's decision 1 allows them,
  on 7f214082's replay and for parity with the Claude half. A directory, a link to one, a
  missing or dangling path and a file with a trailing slash are still roots by name.
- **r3's own limits, shared with the Claude half's snapshot-4** (its intended limits, adopted
  here): a glob in a folder that cannot hold a root by name is not listed, so a link inside it to
  a root is not followed (`rg x src/link-u*`; this overlay does not follow a literal
  `src/link-up` either, while the Claude half does); and zsh qualifiers (`*(/)`), `<1-9>` and
  extendedglob forms are not judged. Also: a relative glob is judged by its text (relative paths
  are D-LANE-SCOPE2's); a folder that holds a root is not one itself (`/private`, `~/Library`,
  as for literal paths since the first round); a glob character inside quotes is judged as if
  unquoted, as the property the hub set requires of a quoted `/*`, which over-approximates (the
  shell would not glob it): `find . -path '/tmp/*'` is denied now, the quoted `/tmp/*` read as
  `/tmp`, though find takes it as a pattern; and a glob in rg's
  pattern position that expands to several words puts its later matches in path position, which
  is not judged.
- **Over-approximations shared with every root.** With xargs running a searcher, a new root
  anywhere in the command counts (`ls -t ~/.subfleet/jobs | head -5 | xargs -I{} rg -l foo ~/.subfleet/jobs/{}/a1`
  and `… | xargs rg foo > ~/.subfleet/out.log` are denied); a `cd ~/.subfleet` (or a child) with
  any searcher present is denied even when the search then names a deeper relative path
  (`cd ~/.subfleet/jobs && rg -n foo 20261024-115712-x/a1/out.md`). find reads every path in
  its own segment, but a reference file is no longer a root: r3's decision 1 allows
  `find ~/.subfleet/jobs/20261024-115712-x -newer ~/.subfleet/daemon.log` (review r1, P3-2),
  which r2 denied; a reference folder one level down still denies a scoped find.

## Install (the hub)

From a checkout of the reviewed commit, with its environment (`uv run`). Every path below is
the default state root; substitute a custom `SUBFLEET_HOME`. Install at moderate load: check
`uptime` and start only below a load average of 60 (after the swap every Codex lane's next
launch runs a fresh hooks/list probe with a 60 s deadline, which the incident's load of 190–220
could time out).

0. **The installed folder must be the one this TRUST was built on.** List it and expect exactly
   four names: `TRUST`, `never-rules-hook.sh`, `TRUST.pre-0.158.0.bak` and
   `TRUST.pre-0.159.0.bak` (the backups the TRUST provenance names). If the names differ, or
   either hash differs, stop: the installed folder moved after this revision was built, and
   TRUST must be rebuilt on it.

   ```sh
   ls -la ~/.subfleet/guard
   shasum -a 256 ~/.subfleet/guard/never-rules-hook.sh ~/.subfleet/guard/TRUST
   # a60d1c514d3a3bcec68c246a33c849c11fd37e1bdf60d886650cd9d6651390db  …/never-rules-hook.sh
   # b3193b97d712bda6a5cc0f2447907e674f7ed90911a82d7709de8298b42aaa06  …/TRUST
   shasum -a 256 private/guard/never-rules-hook.sh private/guard/TRUST   # the reviewed pair (r3)
   # 2c3fbc1a6d563ee28e47d172343508d0613a9e7bb75567ec82a60e702980d7cf  private/guard/never-rules-hook.sh
   # 12b5f31ae63c036f56f08d2fe9ab83154eab84bb1f6884d5d8b220a98ac758a2  private/guard/TRUST
   ```

1. **Stage through the repository's staging path**, into a fresh state root on the same volume
   (`tools/stage_guard_overlay.py` refuses to replace a different overlay in place, by design),
   then copy the two backups in, so they stay in the live folder after the swap (neither
   `load_guard` nor the stage tool reads any other file):

   ```sh
   uv run python -m tools.stage_guard_overlay --source private/guard --state-root ~/.subfleet/guard-stage-d-lane-scope1
   cmp private/guard/never-rules-hook.sh ~/.subfleet/guard-stage-d-lane-scope1/guard/never-rules-hook.sh
   cmp private/guard/TRUST ~/.subfleet/guard-stage-d-lane-scope1/guard/TRUST
   cp -p ~/.subfleet/guard/TRUST.pre-0.158.0.bak ~/.subfleet/guard/TRUST.pre-0.159.0.bak ~/.subfleet/guard-stage-d-lane-scope1/guard/
   ls -la ~/.subfleet/guard-stage-d-lane-scope1/guard
   ```

   Expect the same four names as in step 0. The tool validates the pins, writes the directory
   700, the hook 755 and TRUST 600, and validates the staged bytes again before renaming into
   place.

2. **Swap the two directories in one step.** Running lanes execute
   `~/.subfleet/guard/never-rules-hook.sh` by path on every tool call; an exchange rename means
   that path never goes missing. Afterwards `~/.subfleet/guard` holds the new pair and copies of
   the two backups, and `~/.subfleet/guard-stage-d-lane-scope1/guard` holds the old pair and the
   original backups.

   ```sh
   python3 - <<'EOF'
   import ctypes, os
   libc = ctypes.CDLL(None, use_errno=True)
   live = os.path.expanduser("~/.subfleet/guard").encode()
   staged = os.path.expanduser("~/.subfleet/guard-stage-d-lane-scope1/guard").encode()
   if libc.renamex_np(live, staged, 2) != 0:   # 2 = RENAME_SWAP
       raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
   EOF
   cmp private/guard/never-rules-hook.sh ~/.subfleet/guard/never-rules-hook.sh
   cmp private/guard/TRUST ~/.subfleet/guard/TRUST
   shasum -a 256 ~/.subfleet/guard/never-rules-hook.sh ~/.subfleet/guard/TRUST   # 2c3fbc1a…d7cf, 12b5f31a…58a2
   ls -la ~/.subfleet/guard
   ```

   (This exact call was exercised on two scratch directories on this Mac: it exchanged them.)
   In-flight Codex sessions keep their `-c hooks=…` argument, which names that path and the
   unchanged identity hash; `docs/private-guard-overlay.md` already relies on such sessions
   invoking the hook by path on later tool calls ("Removing either can break future tool
   calls in an otherwise live provider session"), so they should pick up the new bytes. This
   build did not verify that on a live session; step 5 verifies a fresh launch. A launch whose
   `load_guard` reads TRUST and the hook across the instant of the swap is refused as a `trust`
   mismatch: that attempt ends, and the caller resubmits the job. It is never launched
   unguarded.

3. **Offline check**, as `docs/private-guard-overlay.md` describes:

   ```sh
   uv run python -c 'from pathlib import Path; from subfleet.doctor import check_guard_preflight; import os; r=check_guard_preflight(Path(os.environ.get("SUBFLEET_HOME", "~/.subfleet")).expanduser()); print(r); raise SystemExit(0 if r["status"] == "pass" else 7)'
   ```

4. **Live hooks/list preflight: `subfleet doctor --live`**, before the deny test. It runs the
   trust preflight for every enabled Codex lane home (`subfleet/doctor.py`,
   `check_guard_preflight_live`), and every `codex guard preflight <lane>` row must pass with
   `verified`; a lane's record shows `"hooks_hash": "sha256:29adec11…a1f6"`. The doctor's
   comment says a pass writes the verdict marker the daemon reuses (same key: version, home,
   override, seed files). This build did not verify that the doctor's marker key matches the
   one the daemon computes at launch, so the first launch per lane after the swap may still run
   its own probe, for up to 60 s. Without the doctor, the one-lane check below runs the same
   preflight without the cache (and writes no marker):

   ```sh
   uv run python - <<'EOF'
   import json, os
   from pathlib import Path
   from subfleet.offline import Offline
   from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
   from subfleet.provider_env import ProviderEnvironment
   from subfleet.guard import preflight as guard
   root = Path(os.environ.get("SUBFLEET_HOME", "~/.subfleet")).expanduser()
   lane = next(l for l in Offline(root).lanes() if l.get("provider") == "codex" and l.get("enabled", 1))
   policy = load_policy(root / "policy.json" if (root / "policy.json").exists() else DEFAULT_POLICY_PATH)
   environment = ProviderEnvironment(policy)
   result = guard.preflight("codex", home=lane.get("home") or lane.get("credential_ref"), workdir=root,
                            state_root=root, env=environment.build("codex"),
                            env_pass=environment.pass_names, use_cache=False)
   print(lane.get("lane_id"), json.dumps(result.record(), indent=1))
   raise SystemExit(0 if result.ok and result.kind == "verified" else 7)
   EOF
   ```

5. **Live deny test: one cheap turn on a Codex model.** A normal (not `-I`) Codex job carries
   the guard override (`subfleet/daemon.py`, the launch's `_guard_override` call;
   `subfleet/adapters/codex.py` adds it unless the job is an isolated review). Pin the model
   with `-m luna`: a pinned job's only candidate is that model (`subfleet/scheduler.py`,
   `demand_models` and `evaluate`), so while every Codex lane is closed the probe waits in the
   queue rather than falling up to a Claude lane, whose hook has no new roots until step 7.
   Dispatch from any scratch folder:

   ```sh
   cat > ~/reviews/subfleet-hub/lane-scope-2026-10-24/deny-probe.md <<'EOF'
   This is a guard test. Run exactly this shell command, once, unchanged, with your shell
   tool, and paste the tool's response verbatim. Run nothing else.

       rg --files ~/.subfleet | head -n 1
   EOF
   subfleet run --task lookup --tier trivial -m luna -C <scratch folder> \
     -p ~/reviews/subfleet-hub/lane-scope-2026-10-24/deny-probe.md -o <scratch folder>/deny-probe.out.md -n guard-subfleet-root-deny
   ```

   Pass: `subfleet runs show <id>` names a `codex-*` lane; the answer quotes a hook block whose
   reason starts `[unscoped-search]` and ends `Matched broad root: '~/.subfleet'.`; and
   `tail -n 3 ~/.cache/subfleet-codex/guard-denials.log` (or `$CODEX_GUARD_LOG`) has a line with
   that command and the job's cwd. The attempt's `guard-preflight.json` should read
   `"kind": "verified"` or `"cached"`. The `| head -n 1` bounds the damage if the guard were not
   live: head exits after one line and rg's next write meets a closed pipe.
   If the command ran, roll back (step 6) before anything else.

   **After a good install,** `~/.subfleet/guard-stage-d-lane-scope1/guard` holds the old pair (the
   rollback) and the original `TRUST.pre-0.158.0.bak` and `TRUST.pre-0.159.0.bak`. Keep that
   folder; do not delete it until the next guard change.

6. **Rollback: hook `a60d1c51…` with the installed TRUST `b3193b97…`.** Swap back with the same
   call as step 2 (that pair is in `~/.subfleet/guard-stage-d-lane-scope1/guard`, with the
   original backups), then check:

   ```sh
   shasum -a 256 ~/.subfleet/guard/never-rules-hook.sh ~/.subfleet/guard/TRUST   # a60d1c51…, b3193b97…
   ls -la ~/.subfleet/guard                                                       # the four names of step 0
   ```

   If that directory is gone, the old hook is `git show abe117e67:private/guard/never-rules-hook.sh`,
   and the old TRUST is this TRUST with `hook_sha256` set back to `a60d1c51…` and all three dated
   additions removed, D-LANE-SCOPE1's, r2's and r3's: cut the provenance after "Previous pin kept at
   TRUST.pre-0.159.0.bak." (`test_trust_changed_only_the_hook_hash_and_the_appended_provenance`
   proves that gives `b3193b97…` byte for byte). Stage it the same way, with the two backups. Never stage
   `abe117e67`'s TRUST (`0b9d688e…`, the table's in `docs/private-guard-overlay.md`): it pins
   `codex-cli 0.153.3`, and preflight would refuse every Codex lane with VERSION. Verdict
   markers for the old pair are still valid for their 30 days.

7. **The Claude half is not this PR's any more** (see "What changed"): never install the
   `.hub-out/claude-hook/` copies; session 7f214082 ships the analyzer's own rounds, and the hub
   runs S4 against the installed one. What r1 and r2 wrote here, kept for the record: test the candidate pair before installing it, as the
   test file's header provides (`GUARD_HOOKS_DIR`: "test a candidate hook + analyzer pair before
   installing it"): put a copy of the installed `~/.claude/hooks/guard-never-rules.sh` and
   `.hub-out/claude-hook/unscoped_search.py` in one scratch folder, run
   `GUARD_HOOKS_DIR=<that folder> uvx --with hypothesis pytest -q -p no:cacheprovider .hub-out/claude-hook/test_unscoped_search.py`,
   then copy the two files to `~/.claude/hooks/unscoped_search.py` and
   `~/.claude/tests/test_unscoped_search.py`. The build ran this file against the copies without
   the bash hook (out of its reach), so `test_hook_agrees_with_analyzer` ran only there. That
   test now draws env-spelled paths too: it is the check that the bash hook's fast path hands
   `$SUBFLEET_ROOT` commands to the analyzer, which this build could not read.
