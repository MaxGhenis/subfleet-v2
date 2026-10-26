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
matches. Round r3b (2026-10-25, "r3b" below) makes those path words judged with the roots,
globs and mounts of the Claude half's snapshot-5 (its roots, normalized and in any letter case;
links followed except under a mount that may stall; globs component by component). Round r3c
(2026-10-26, "r3c" below) closes the walk-arounds of the hub's review r2 (REQUEST_CHANGES at
`674abdac7`): globs that match every entry, `/Volumes/<entry>` paths, braces and zsh's
alternation, links before `..`, searchers named by path or quote, and the other escapes. Nothing
here touches the live guard; the hub installs it with the steps below.

## What changed

**The hook** (`private/guard/never-rules-hook.sh`, SHA-256 `7a5ad79d…1a1a` after r3c; r3b's
was `f952fd3e…131e`, r3's `2c3fbc1a…d7cf`, r2's `dbe21e1d…041c`). Two alternations join the roots in `broad_re`:

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
`test_only_the_roots_the_header_and_the_message_changed` first removes the three path-word blocks
whole (`# >>> r3 path words: begin|judge|name >>>` to their end markers) and undoes the text pairs
r3, r3b and r3c made outside them (`R3C_TEXT`, then `R3_TEXT`: the header's sentences, the
region's header comments, and since r3c the prefilter's third term, `pw_cmd` in the detection, the
`judged` lines and the cwd rule, and the message's root list), which must give the r2 bytes
(`dbe21e1d…`); then it undoes D-LANE-SCOPE1's text edits, the two `sed` edits and the `broad_re`
line and gets the original reviewed bytes (`a60d1c51…`) back.

**TRUST** (`68936faf…c91a` after r3c; r3b's was `0d5dac0f…54d8`, r3's `12b5f31a…58a2`, r2's `aa09f53d…a2f0`). It is built on the installed TRUST of 2026-09-29 (`b3193b97…`, the
`codex-cli 0.159.0` pin), not on the repository's copy at `abe117e67` (`0b9d688e…`, which still
pins `codex-cli 0.153.3`; a TRUST built on it would refuse every Codex lane at preflight with
VERSION, `subfleet/guard/preflight.py:649-650`). It has the same keys. `hook_sha256` is
recomputed for the new bytes, and `provenance` gains four dated additions: D-LANE-SCOPE1's three
sentences, ending "Installed only after the hub's review and a live hooks/list preflight.",
r2's one sentence, naming the env spellings, r3's one sentence, naming the path words judged
on disk, and r3b's one sentence, naming snapshot-5's roots, links and globs, whose mount clause r3c
corrected (decision 7: "except under /Volumes, /Network, /net or /home: a written path under them
is never resolved or stat'ed; a link leading into one still is"). r3c added no sentence: its
decision 8 lets TRUST change only `hook_sha256` and that sentence. Nothing else moved. `hooks_trust_hash` (`sha256:f97fa95f…`) and
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

**r3's pair** (superseded by r3b's, below): hook `2c3fbc1a6d563ee28e47d172343508d0613a9e7bb75567ec82a60e702980d7cf`,
TRUST `12b5f31ae63c036f56f08d2fe9ab83154eab84bb1f6884d5d8b220a98ac758a2`.

## r3b: the roots, globs and mounts of the Claude half's snapshot-5 (2026-10-25)

**Why.** After r3, session 7f214082 froze snapshot-5 of the Claude half
(`~/.claude/hooks/unscoped_search.py`; the hub's read-only copy is
`reference-snapshot-5/unscoped_search.py.ref`, sha256 `0ec2f7aa…fdb207`) with two glob
refinements, and the hub found this overlay's root list far narrower than snapshot-5's. The
cross-half check (S4) would fail on every such difference. r3b ports snapshot-5's semantics
(`BROAD_RE`, `HOME_SUBDIRS`, `HOME_PARENTS`, `normalize`, `is_broad`, `ROOT_NAMES`,
`holds_root`, `glob_pattern`, `glob_list`, `glob_broad`, `STALL_RE`, `is_file`, `physical`,
`broad`) into the same three `r3 path words` blocks; the fence test still removes them and
gets r2's bytes.

**The decisions as built.**

1. **The roots are snapshot-5's.** Every path word is judged now, not only roots by name. A
   word is resolved as r3 resolves it (`~`, `$HOME`, `${HOME}`, `/…`; `$SUBFLEET_ROOT` and
   `$SUBFLEET_HOME` always as `$HOME/.subfleet`), then normalized as `normalize` does: `//`
   collapsed, `.` and `..` resolved by name, `/System/Volumes/Data` (and anything under it)
   folded to `/`, and `/private/tmp`, `/private/var` and `/private/etc` folded to `/tmp`,
   `/var` and `/etc`. It is broad by name, case-insensitively, as `is_broad` says (the table
   below); the home's rules apply wherever the home is. Added roots: `/Volumes`,
   `/Volumes/<entry>`, `/System`, `/System/Volumes`, `/private`, `/var`, `/var/folders`,
   `/opt`, `/opt/homebrew`, `/Library`, `/Applications`, `/usr` and `~/Library` itself (and
   with the folds `/private/var/folders`, `/System/Volumes/Data` and every letter case of
   every root).
2. **A written path is judged where its links lead** (`pw_physical`, `pw_broad`). Broad by
   name and not an existing non-directory: broad. Otherwise, if realpath (links followed, a
   missing part kept as written) leads to a different path that is broad by name and no
   file: broad, so `rg x ~/link-to-home` and `cd ~/link-to-home; rg x` are denied. A path
   under `/Volumes`, `/Network`, `/net` or `/home` (any letter case) is never resolved or
   tested: its file test reads "no file" and it leads nowhere else.
   `rg foo "/Volumes/Macintosh HD"` is broad by name and never stat'ed. This covers r3's
   file rule: the same `pw_broad` decides a file.
3. **Globs follow `glob_broad`, component by component** (`pw_globbroad`), in place of r3's
   whole-word bash expansion. The literal components before the first glob component are the
   prefix, normalized; a prefix that is no broad root is not judged (allowed). A plain
   component keeps `p/<name>` where something is there (`pw_lexists`); under `/Volumes`
   (the one stall folder that can hold a root) the name is matched against `/Volumes`'
   listing instead, never stat'ed; under any other stall path it is dropped. A glob
   component lists `p` only when `pw_holds p`, matching case-sensitively, a leading dot only by
   a leading dot. After every component but the last only broad roots that are no file go on.
   At the end a broad root that is no file is a match, and a non-root counts only when it is a
   listed symlink or came from a plain component, judged by where its links lead (decision 2).
   At most 20,000 directory entries are read per word. A POSIX class other than `alpha`,
   `digit`, `alnum`, `upper`, `lower`, `xdigit` and `space` is not judged (allowed); zsh
   qualifiers, `<1-9>` and extendedglob forms are not judged, as in r3. Listing is bash's
   expansion of a quoted folder and a bare `*` (`set +f`, `nullglob`, `dotglob`, `nocaseglob`,
   `extglob`, `failglob` and `GLOBIGNORE` set for it and put back); matching is a `[[ == ]]`
   against a pattern whose every literal character is escaped, so no pattern is an extglob and
   nothing in one runs. The r3 tests holding `$(touch …)` and a backtick still leave no file.
4. **Which folders get listed is computed** (`pw_holds`, as `holds_root`): a name from
   `ROOT_NAMES`, or a placeholder for any name, makes `normalize(d/n)` broad. The table below
   pins the set.
5. **Unchanged from r3:** a trailing run of bare `*` / `.*` (or a `**/`) after a root is that
   root; every folder directly in `.subfleet` stays a root; a denial names the word as written,
   plus ` (glob)` for a glob.
6. **The shared known limit:** a link inside a listed folder that points into a mount that may
   stall is still resolved, as a written path's links are. The Claude half has the same limit.

Only five functions touch the file system (`pw_islink`, `pw_lexists`, `pw_nondir`,
`pw_readlink`, `pw_ls`); `test_r3b_only_the_disk_functions_touch_the_file_system` pins that, and
the mount test wraps exactly those five.

**Deviations, each with its counterexample.**
- **A quoted string is part of its word.** r3's tokenizer split runs at whitespace inside
  quotes, so `rg foo "/V*/Macintosh HD"` reached the hook as `/V*/Macintosh` and `HD`, and the
  first matched nothing in `/Volumes` (the entry is `Macintosh HD`): allowed. Words are now runs
  of unquoted characters and whole quoted strings; a run that is no path word has the words of
  each of its quoted strings judged in turn (`bash -c 'cd ~/x && rg y'`, prose in `-m "…"`, as
  r3 judged them). So `rg --files ~/".subfleet"` is `~/.subfleet` now (denied; r2's known
  limits listed it), and a placeholder replaces the whole word inside its first quote mark.
- **A broad word the text rules already read as a root by name is left to them**, so their
  messages and positions stay as they were, unless a quote mark was part of it (they read a
  quoted part as `Q`, so `~/"PolicyEngine"` gets a placeholder).
- **`**` inside a `bash`/`sh`/`dash`/`ksh -c` script is `*`.** The brief allows
  `bash -c 'rg foo /**/*.py'`; r3's decision 2 walks `/` for a `**/` anywhere. A quoted string
  whose text before it ends in such a shell and a `-…c` option is a bash script (unless it says
  `globstar`); its words get context `b`. At the top level and under `zsh -c` a `**/` still
  walks (zsh's `**/`). As the hook never sees a searcher right after a quote,
  `bash -c 'rg foo /**/*.py'` was allowed already; `true; bash -c 'true; rg foo /**/*.py'` is the
  case that moves (r3 denied it).
- **r3's trailing-`/*` rule runs before `glob_broad`.** "A bare `/*` after a root falls out of
  decision 3 too" holds only for a root that can hold one (`~/*` lists the home); for `/tmp/*`,
  `~/PolicyEngine/*` and `~/.subfleet/jobs/*`, `glob_broad` lists nothing, and the verdict comes
  from r3's rule (snapshot-5's `path_of`). Run first, it also makes `rg x ~/Pol*/*` denied
  (`~/Pol*` is judged), where snapshot-5's `path_of` declines a glob before the trailing star
  and its `glob_broad` stops at `~/PolicyEngine`, which holds no root: allowed there. An
  intended difference, kept from r3 (the shell hands rg every entry of `~/PolicyEngine`).
- **The root before a trailing `/*` is judged as a written path** (decisions 1 and 2), so
  `rg foo ~/.subfleet/policy.json/*` is allowed now (r3: always that root); `policy.json/*`
  matches nothing.
- **The entry budget counts in bash's byte order**, not `scandir`'s directory order; the two
  differ only past 20,000 entries. A symlink loop is left unresolved (realpath returns a
  partly resolved path); a `?` matches one byte of a non-ASCII name, where fnmatch matches one
  character.
- **The cwd rule's list is unchanged.** The decisions are about written words; a `.` search
  sitting in `/usr` passes (snapshot-5 blocks it), as one sitting in `~/.subfleet` did.

**The root table** (decision 1; `test_r3b_root_table`, by name, home `/Users/me`):

| Broad | Not broad |
| --- | --- |
| `/`, `/Users`, `/Users/<any>` and its `TheAxiomFoundation`, `RulesFoundation`, `PolicyEngine`, `Library`, `Library/Caches`, `.cache`, `.local`, `.subfleet`, `.subfleet/<any>` | `/etc`, `/private/etc`, `/usr/local`, `/usr/bin`, `/opt/homebrew/Cellar`, `/var/folders/zz` |
| `/Volumes`, `/Volumes/<any>` | `/Library/Caches`, `/Applications/x.app`, `/System/Library`, `/System/Volumes/Data/x` |
| `/System`, `/System/Volumes`, `/System/Volumes/Data` (folds to `/`) | `/Volumes/Macintosh HD/Users`, `/Network`, `/net`, `/home`, `/tmp/x` |
| `/private`, `/tmp`, `/private/tmp`, `/var`, `/private/var`, `/var/folders`, `/private/var/folders` | `~/src`, `~/.subfleet/jobs/x`, `~/Library/Caches/x`, `~/.subfleet-old`, `/Users/x/y/PolicyEngine` |
| `/opt`, `/opt/homebrew`, `/Library`, `/Applications`, `/usr` (also `/USR`, `/users/ME/policyengine`, `//Users//me/`, `/Users/./me/./Library`) | |
| the home and its children wherever it is (`/srv/me`, `/srv/me/library/CACHES`, `/srv/me/.subfleet/x` with that home) | the same paths with another home |

**The listable-folder table** (decision 4; `test_r3b_listable_folders_are_computed`):

| Listed (home `/Users/me`) | Never listed |
| --- | --- |
| `/`, `/Users`, `/Users/<any>`, `/Users/<any>/Library`, `/Users/<any>/.subfleet` | `/tmp`, `/var/folders`, `/opt/homebrew`, `/Library`, `/Applications`, `/usr` |
| `/Volumes`, `/System`, `/System/Volumes`, `/System/Volumes/Data` | the home's other children (`PolicyEngine`, `.cache`, `.local`, `TheAxiomFoundation`, `RulesFoundation`, `Library/Caches`) and the folders directly in `.subfleet` |
| `/private`, `/private/var`, `/var`, `/opt` | `/Volumes/<entry>` and anything under it, `/Network`, `/net`, `/home`, `/private/tmp`, `/private/etc`, `~/src` |
| a home outside `/Users` (`/srv/me`), its `Library` and its `.subfleet` | `/srv`, `/srv/me/PolicyEngine`, `/srv/me/.subfleet/jobs` |

**Changed verdicts** (against r3, each pinned): every command over an added root, alias, letter
case or link to a root is denied now (`R3B_DENIED`: `/usr`, `/Library`, `/opt/homebrew`,
`/var/folders`, `/private`, `/System/Volumes/Data`, `/private/var/folders`, `~/Library`,
`~/policyengine`, `/users/<you>`, `"/Volumes/Macintosh HD"`, `/Volumes/Ma*`,
`"/V*/Macintosh HD"`, `~/link-to-home`, a link to `PolicyEngine`, a link to `/usr`,
`cd ~/link-to-home; rg x`; `/U*/<you>` and `/Users/*/PolicyEngine` were already denied by
r3's expansion); `true; bash -c 'true; rg foo /**/*.py'` is allowed; and `R3B_FLIPPED`:

| Command | r3 | r3b | Why |
| --- | --- | --- | --- |
| `rg x ~/.subfleet/policy.json/` | denied | allowed | normalize drops the slash before the file test (decision 1) |
| `rg -l foo $SUBFLEET_ROOT/subfleetd.log/` (each env spelling) | denied | allowed | the same, for an env spelling's file child |
| `rg foo ~/.subfleet/policy.json/*` | denied | allowed | the root before `/*` is judged as a written path |
| `rg --files ~//.subfleet`, `~//PolicyEngine` | allowed | denied | `//` collapsed |
| `rg --files ~/.subfleet/./jobs`, `~/./PolicyEngine` | allowed | denied | `.` resolved |
| `rg --files ~/.Subfleet/jobs` (and r2's `~/policyengine`) | allowed | denied | any letter case |
| `rg --files /System/Volumes/Data/Users/tester[/.subfleet]` | allowed | denied | the data volume folded |
| `rg --files ~/".subfleet"`, `~/"PolicyEngine"` | allowed | denied | a quoted string is part of its word |

The five r2 known-limit pairs these flip left `R2_LIMITS` for `R3B_FLIPPED`. Over-approximations
widen with the roots: `find . -path '/usr/*'` is denied now, as `find . -path '/tmp/*'` was in
r3 (a quoted glob judged as unquoted), and `cd /usr && rg -n x lib/foo.c` is denied, as `cd ~`
was.

**Ported vectors** (from `test_unscoped_search.py.ref`, each test naming its source):
`oracle_broad` with `test_explicit_root_blocked_iff_broad` over `CANON_ROOTS` and its component
pool (600 seeded draws), `test_spelling_does_not_change_verdict` (trailing slash, `//`, `/.`,
`/zz/..`, swapcase, the data volume and `/private` prefixes), `test_normalize_is_idempotent`
(300 strings), `test_roots_under_any_home`, `test_root_names_cover_every_root`, every case of
`GLOB_CASES` the overlay can take (36), `test_a_file_is_never_a_root`'s globs and links, the
example table's alias, data-volume, pattern-position and loop cases, the absolute form of
`test_symlinks_are_judged_where_they_lead`, `test_hit_names_the_root`'s written form,
`test_glob_never_lists_or_stats_inside_a_mount` (its eleven words and the stall-folder check),
`test_glob_listing_is_capped`, and `test_a_glob_of_a_root_is_that_root` (exhaustive over its
roots, forms and positions; Hypothesis there). Not ported, with the reason
(`R3B_UNPORTED`): `rg foo ../Pol*` (relative words are not path words here), `bash -c 'rg foo
~/Pol*'` (a searcher right after a quote is never detected), `grep foo ~/Pol*/**/*.py`
(a grep without `-r` is no searcher here; ported with `-r`), `rg foo '~/Pol*'` and
`rg foo ~/'Pol*'` (a quoted glob is judged as if unquoted, r3's over-approximation),
`noglob rg foo ~/Pol*` (noglob is not read), `test_glob_cases_match_the_shells` (kept as r3's
zsh/bash differential), and the cd/CDPATH/`cd -P` cases (this overlay has no cd model past its
cd rule).

**The property test** (`test_r3b_property_the_hook_agrees_with_an_independent_reference`, seed
20261026, 2,400 words): words built from the four home spellings, `/Users/prop` in three letter
cases, the data volume and `/private/..` prefixes and the other roots; names that exist in the
fixture and names that do not; letter-case changes; `.`, `..` and `//`; a glob character per
component (`?`, a `*` tail or head, `[c]`, `[!Z]`, `[[:upper:]]`); and a trailing `/` or `/*`,
over a home with links to itself, to `PolicyEngine`, to `/usr` and to a file. The oracle is
`REFERENCE_SH`, a bash reference of decisions 1-4 written from the brief, not from the hook (its
own lowercasing table, `IFS=/` splitting, a link resolver that rewrites the whole path at the
first link and starts again, `ls -A` listings, each glob component used as a bash pattern as
written). The hook's functions must give its verdict on every word, and 200 of the words also
go through the whole hook as `rg -n foo <word>`. S1's labels come from the same reference.

**The mount test** (`test_r3b_nothing_inside_a_mount_is_read`) sources the hook's functions,
wraps the five disk functions so each call is recorded before it runs (no environment-driven
behaviour in the hook), and judges `/*/*/x`, `/Volumes/*/x*`, `/V*/Macintosh HD` and seventeen
more mount words in a fixture where `/Volumes/<entry>`, `/Network`, `/net` and `/home` each hold
something: no call names any of them or anything under them; `/Volumes` itself is listed.
`test_r3b_nothing_inside_a_real_mount_is_read` repeats it on this host's real `/` and `/Volumes`
for the two words that list only those.

**Hashes for the install.** The new pair: hook
`f952fd3e9648df1783cec96327b66a84a21082ba63469ef56d41749d23a5131e`, TRUST
`0d5dac0fc9290eac8cfd1483b7f44cde9bb27e817657dd7fe4dc7b2ab07054d8`. The installed pair step 0
expects, and the rollback: hook `a60d1c51…`, TRUST `b3193b97…` (unchanged by r3b).


## r3c: review r2's walk-arounds closed (2026-10-26)

**Why.** The hub's review r2 (REQUEST_CHANGES at `674abdac7`) confirmed three ways around the rule
through the whole hook and in real `/bin/zsh -f` and `/bin/bash`: globs that match every entry
(`rg --files ~/.subfleet/jobs/?*`, `…/*//`, `…/*/..`), the boot volume's alias
(`"/Volumes/Macintosh HD/Users/<me>/.subfleet"`), and braces and zsh's `(a|b)`, plus four P3s
(a link before `..`, searcher names, other escapes, sentences). Every review input was allowed at
`674abdac7` (95 of the 97 red inputs; the other two, `~/.subfleet/{jobs,src}` and a Kelvin-sign
`worKtrees`, were already denied as one child's name).

**The decisions as built** (the hub's numbering):

1. **A glob that matches every entry is its folder.** In `pw_alt`, trailing `/`, `/.` and
   `<glob>/..` are read first; then a trailing run of components made only of wildcards (`*`,
   `?`, bracket expressions: `pw_wild`), or `.*` or `**`, is the folder they sit in, listed or not
   (`(glob)` in the message). In `pw_globbroad`, any glob component directly in a folder of a
   `.subfleet` (the home's, or any `/Users/<x>`'s, as BROAD_RE has them) makes the word that
   folder (`pw_subchild`), so r3's allowed `~/.subfleet/jobs/2026*` is denied; `jobs/<id>/a1/*`,
   `/tmp/sf-*`, `W/*` and `W/**/*.py` pass.
   *Deviation.* 1(b) as written covers a wildcard-only component in any position. Read that way
   it denies r3b's must-allow `rg x /*/*.py` (it names files one level down) and
   `rg foo /Volumes/*/x*`, and the r3b differential's `/*/*/x` (no real match is a root). So 1(b)
   is the last component (after 1(a)), at any root; a wildcard-only component followed by a plain
   name under a folder that is listed is judged by the listing, as r3b decided. Under a folder that
   is never listed because it is inside a mount entry, a glob component matches by name (below),
   which denies review r2's `"/Volumes/Macintosh HD/Users/"*/.subfleet`.
2. **`/Volumes/<entry>/<rest>` is `/<rest>` by name.** `pw_isbroad` maps the path (again for
   `/Volumes/<a>/Volumes/<b>/…`) before matching and never stats it; `pw_holds` is false inside a
   mount entry, so nothing there is listed. *Addition:* a glob component inside a mount entry
   matches the names a root can have (snapshot-5's ROOT_NAMES) plus a placeholder for any other
   name, judged by name only: `"/Volumes/Macintosh HD/"U*` is denied, `/Volumes/Ext/src/x*` passes.
   Without it, a selective glob under the alias (`…/Macintosh HD/U*`) would be a new walk-around.
3. **Braces and alternation.** `pw_expand` expands every unquoted comma group (64 words at most);
   a nested group, a `{x..y}` sequence or more than 64 words reads each group as `*`. Outside a
   bash/sh/dash/ksh `-c` script (zsh's reading; bash refuses the form), a `(a|b)` group of plain
   names is judged by its alternatives, and any other group reads its component as `*`. The judge
   pass now carries the word as the shell reads it, with a quoted or escaped `{ } , ( | )` kept
   literal (`\001`–`\006`), and a run that starts with a brace group is a path word too
   (`{~,/tmp/x}/.subfleet`). A word is broad when one of its words is; the message names it as
   written, plus `(glob)` when that word was judged as a glob.
4. **A searcher's `..` is physical.** `pw_physdots` resolves the links before each `..` (with
   `pw_physical`) for a plain word, a glob's prefix, and a plain `..` after a glob; inside a stall
   mount it stays by name. A `cd`/`pushd` target keeps the logical `..` (the judge pass marks it;
   `cd -P`'s is physical).
5. **Searchers by basename.** Before the detection, `pw_prep_awk` reads the command as the rules
   do (`pw_cmd`): in command position (the first word, or after `env`, `command`, `builtin`,
   `exec`, `nice`, `nohup`, `time`, `timeout`, `xargs`, `sudo`, `noglob`, `stdbuf` or `caffeinate`
   and their options), a searcher named by a path, a backslash or a quote is made its name,
   egrep/fgrep/ugrep `grep` and rgrep `grep -r`; `$(which X)`, `$(command -v X)` and `` `which X` ``
   become X. The detection regexes take a quote or a backslash before the name, and the prefilter
   gains a third term for these spellings. `RG=rg; $RG …` stays a known limit.
6. **The other escapes.** Backslash escapes are applied as the shell applies them (unquoted `\x` is
   `x`; in double quotes only `\$`, `` \` ``, `\"`, `\\` and backslash-newline). `***` is `**`. A
   `/` inside a bracket expression splits no component in zsh's reading (`/[^/]*` is every entry of
   `/`). The two folded code points below are folded in `pw_normalize`. Each `$'…'` in a path word
   is `*`. `~name` is `/Users/name`, or the home when `name` is this user (`id -un`). A bare `cd`
   or `cd --` is `cd ~`. A recursive searcher given no path walks `.`, and the cwd rule judges it
   (`pw_nopath`): find with an expression first, rg with no path and nothing on its stdin (no pipe
   into it, no `<` redirection; `--files` lists `.` whatever its stdin, as snapshot-5 reads rg),
   grep -r with none. A `sh`/`bash`/`zsh`/`dash`/`ksh -c` or `eval` script holding a searcher is
   added to the judged command as its own line (its words judged in the shell's reading), to depth
   4; a searcher deeper than that adds a placeholder root, so it is denied ("a searcher nested
   deeper than 4 shells"). A depth bound (`-maxdepth`, `--max-depth`, `-d N`) that is an unquoted
   argument of a simple command holding a searcher makes that command `:`, so it exempts only
   itself; the global "any max-depth disarms the rule" check is gone, and the header's "no
   override … no magic comment" sentence is true now.
7. **Sentences.** The mount sentence is corrected in TRUST and the overlay doc; this section's
   "What changed" paragraph lists the fence's three path-word blocks and counts four dated
   additions; `docs/invariants.md` says what the fence undoes; the overlay doc's "Known differences
   that remain" lists the open forms and where this overlay is stricter than snapshot-5; the block
   message's root list is built from the root table `pw_isbroad` reads (`pw_fixed`, `pw_kids`).
8. **TRUST and install.** TRUST changes only `hook_sha256` and that sentence; step 0 below expects
   the new pair.

**The folded code points** (decision 6): every code point whose Python `casefold()` is one ASCII
letter while it is none, each checked in scratch with `os.path.samefile` against the ASCII name on
this volume (APFS, case-insensitive). `test_r3c_folded_code_points_are_exactly_the_confirmed_set`
re-enumerates them and pins the hook's folding.

| Code point | Name | UTF-8 | Folds to | `samefile` here |
| --- | --- | --- | --- | --- |
| U+017F | LATIN SMALL LETTER LONG S | `c5 bf` | `s` | yes |
| U+212A | KELVIN SIGN | `e2 84 aa` | `k` | yes |

**Changed verdicts against `674abdac7`.**
- *Now denied:* every input of review r2's P2-1, P2-2 and P2-3 lists, P3-1's two links, P3-2's
  names and the P3-3 forms (`R3C_WILD_DENIED`, `R3C_VOLUMES_DENIED`, `R3C_BRACES_DENIED`,
  `R3C_DOTS_DENIED`, `R3C_SEARCHERS_DENIED`, `R3C_FORMS_DENIED`); r3's allowed
  `rg x ~/.subfleet/jobs/2026*` and snapshot-5's `jobs/2026*` and `jobs/link-u*` (1(c)); r2's known
  limits `~tester/.subfleet`, `~tester/PolicyEngine`, `~/.subfleet/jobs/*/a1`,
  `bash -c 'rg --files $SUBFLEET_ROOT'` and `bash -c 'rg --files ~'` (`R3C_CLOSED_LIMITS`); the
  known-limit pins `bash -c 'find / -name x'`, `bash -c 'rg --files ~/.subfleet'` and
  `eval 'grep -r x ~/.subfleet/jobs'`; the Claude example table's `rg foo`, `rg --files | head`,
  `grep -r foo` and `ps | grep -r foo` from the home (no path, so `.`); the root table's
  `/Volumes/Macintosh HD/Users` (decision 2).
- *Now allowed:* none. S1 finds no command the original hook denies that this one allows; its
  two "intended, loosened" rows are S3's labelled case, an apply_patch body plus a new-root search,
  denied by both hooks (`hf-dest` before, `unscoped-search` now).
- *Still allowed, by design:* review r2's 25 scoped searches and the roots in pattern position
  (`R3C_ALLOWED`, 47 cases with the brief's must-stay-allowed ones), `rg -l foo ~/*/src`, and the
  forms listed under "Known limits".

**The property** (`test_r3c_property_the_hook_agrees_with_an_independent_reference`, seed
20261027, 2,400 words, 1,986 distinct, 1,102 broad): r3b's generator plus wildcard-only and bracket
components (with a `/` inside brackets and `***`), the `//`, `/.`, `/./`, `/..` and `<glob>/..`
tails, the `/Volumes/Macintosh HD` and `/Volumes/Boot` prefixes (`/Volumes/Boot` is the fixture's
link to `/`, standing in for the alias), comma braces (360 words) and zsh alternation (306), `~user`,
98 words with a backslash, and the two folded code points. `REFERENCE_SH` was rewritten from the
r3c brief, not from the hook. Every plain word goes through `pw_word`; the 98 escaped words and 200
more go through the whole hook. Result: 0 disagreements. On the way it found one hook over-block,
minimized to `rg -n foo /VOLUMES/Boot/private/(link-to-usr|o\pt)//`: the judge pass's paren group
did not take a backslash escape, so the word split and its `//` tail read as `/`; fixed. It also
found a reference bug, minimized to `/Volumes/Macintosh HD/Users/x*`: the reference's glob walk
started from the path `isbroad` had overwritten (`R`, the mapped `/users`); fixed. r3b's property
(seed 20261026) still gives 0 of 2,400 against the rewritten reference, and r3's property (seed
20261025) passes against its updated oracle.

**The real-shell differential** (`test_r3c_globs_braces_and_alternation_agree_with_real_shells`,
45 words: the r3c glob, brace, alternation, bracket, `***` and `/Volumes/Boot` cases): each is
denied exactly when some expansion of it in `/bin/zsh -f` or `/bin/bash`, over the fixture, names a
root (`r3c_real`: the shells expand; decision 1 is read as written; every match is judged by
`REFERENCE_SH`). Alternation is zsh's alone. The documented exceptions are the entry budget (none
here) and decision 2's by-name reading inside a mount entry: `/Volumes/Boot/Users/zz*` and
`/Volumes/Boot/Users/glob/The*` are denied by name though no real entry matches. r3's differential
(324 words) takes the same either-shell rule now: bash reads `**/` as `*/`, which r3's oracle had
read as zsh's walk in both shells.

**The mount test** (`test_r3c_nothing_inside_a_mount_is_read`): r2's harness, the five disk
functions wrapped, over r3b's 20 mount words and 16 more (`/Volumes/<entry>/…` by name, braces,
alternation, `..` and `***` under a mount): no call lists or stats anything inside
`/Volumes/<entry>`, `/Network`, `/net` or `/home`.

**Hashes for the install.** The new pair: hook
`7a5ad79d3f7db4c3c08bf07b8455344a4bdae3e66224218094b398cdf1091a1a`, TRUST
`68936fafa42f885c29fadd9b48c2de48ace5e1ccd29b9c0cee0a09ad9e1ac91a`. The installed pair step 0
expects, and the rollback: hook `a60d1c51…`, TRUST `b3193b97…` (unchanged by r3c).

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
| the new roots removed | `test_each_direct_child_is_a_root` (r3b: its `$CHILD` cases, words the text rules judge alone) |
| the child form dropped (`~/.subfleet` only) | `test_each_direct_child_is_a_root` (r3b: its `$CHILD` cases) |
| a prefix match (`~/.subfleet` and everything under it) | `test_near_misses_patterns_prose_and_bounded_searches_stay_allowed` (r3b: its `~/.subfleet/jobs/$ID`) |
| the trailing-slash form missed (roots in their own `root_core` branch, without the shared `/?`) | `test_trailing_slash_spellings_are_denied` (r3b: its `~/.subfleet/$CHILD/`) |
| TRUST's hash left stale (the new TRUST with the old hook's hash) | `test_trust_pins_the_new_hook_bytes` |
| TRUST built on the repository's stale copy (`codex-cli 0.153.3`) | `test_trust_keeps_the_installed_lane_pin` |
| r2: the env spellings dropped (their `broad_re` alternative and their `${…}` normalization) | `test_every_env_spelling_is_a_root_one_level_down_and_no_deeper` |
| r3: the file exception removed (`pw_broad` no longer clears a file) | `test_r3_a_file_is_never_a_broad_root` |
| r3: `! -d` dropped in `pw_nondir` (a link to a directory passes) | `test_r3_a_directory_a_link_to_one_and_a_name_stay_roots` |
| r3: `-e` dropped in `pw_nondir` (a missing path passes) | `test_r3_a_directory_a_link_to_one_and_a_name_stay_roots` |
| r3: the environment's `$SUBFLEET_ROOT`/`$SUBFLEET_HOME` values used | `test_r3_env_values_are_never_read` |
| r3: a glob judged as a plain word | `test_r3_selective_globs_are_judged_by_what_they_match` |
| r3: the dot rule dropped (in `pw_match`) | `test_r3_selective_globs_are_judged_by_what_they_match` |
| r3: a no-match glob judged by its text | `test_r3_selective_globs_are_judged_by_what_they_match` |
| r3: case-insensitive matching (`nocasematch` on in `pw_match`) | `test_r3_selective_globs_are_judged_by_what_they_match` |
| r3: a trailing `/*` left as text | `test_r3_a_bare_star_after_a_root_is_that_root` |
| r3: the listing state not restored (`pw_ls`) | `test_r3_glob_expansion_restores_every_setting` |
| r3b: a stall mount listed inside (`pw_holds` or a stall path) | `test_r3b_nothing_inside_a_mount_is_read` |
| r3b: `/Volumes` not listed | `test_r3b_the_new_roots_aliases_case_links_and_globs_block` |
| r3b: a plain name after a glob counted without existing | `test_r3b_scoped_paths_no_match_and_mount_contents_stay_allowed` |
| r3b: a plain name under `/Volumes` stat'ed instead of matched against the listing | `test_r3b_nothing_inside_a_mount_is_read` |
| r3b: `/usr` dropped from the roots | `test_r3b_the_new_roots_aliases_case_links_and_globs_block` |
| r3b: root matching made case-sensitive (`pw_isbroad` without `nocasematch`) | `test_r3b_root_table` |
| r3b: alias folding dropped (`pw_normalize`) | `test_r3b_the_new_roots_aliases_case_links_and_globs_block` |
| r3b: `pw_holds` replaced by r3's fixed list | `test_r3b_listable_folders_are_computed` |
| r3b: a non-last component keeping non-broad matches | `test_r3b_scoped_paths_no_match_and_mount_contents_stay_allowed` |
| r3b: written-path link resolution dropped (`pw_broad`) | `test_r3b_the_new_roots_aliases_case_links_and_globs_block` |
| r3b: a file test run on a stall path (`pw_isfile` without `pw_stall`) | `test_r3b_nothing_inside_a_mount_is_read` |

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

### r3b's holds

Through `suite_hold.sh`, one pytest or driver per hold, each bounded by its own alarm (scratch:
`$(getconf DARWIN_USER_TEMP_DIR)sf-guardr3b/logs/`). Holds waited 8-55 minutes for a slot.

| Hold | Run | Result |
| --- | --- | --- |
| red, 05:09Z | the r3b cases at `52ea3f546` (the new tests beside r3's hook) | 28 failed, 1 passed (the substitution test, which r3's code already met), 213 s |
| check, 05:17Z | the r3 and r3b tests but the r3 property, the shell differential and S1-r3, at `7589d54b2` | 37 passed, 3 failed (two shell states: bash 3.2's `=~` obeys `nocaseglob`; the disk-function fence matched `pw_readlink`), both fixed in `ece09cd4d`; the r3b property (2,400 words) passed |
| file1, 05:47Z | `tests/unit/test_guard_subfleet_root.py` at `096b8574e`, scale 1 | 16 tests before the alarm, 1 failed: `$SUBFLEET_ROOT/subfleetd.log/` is a file now (pin flipped in `788add5fa`) |
| mut1, 06:47Z | the 11 r3b mutants at `9e1768212` | control passed; 11 of 11 killed |
| file2, 07:10Z | the file's corpus tests at `9e1768212`, resuming file1's verdict cache | 10 passed (S1, S1's protection, S1-r3, S2, S3 twice, the cwd list, both known-limit pins, the env spellings); S5 failed on a source spelled `/Users/maxghenis` (another home, where the fixture's file is absent), fixed in `ce6911afc`; S4 skipped (no analyzer) |
| modules, 07:43Z | `tests/unit/test_guard_overlay.py` and the 7 other never-rules modules at `9e1768212` (a whole-tree export) | 827 passed in 672 s: overlay 22, `test_daemon_launch` 9, `test_claude_adapter` 172, `test_codex_turn` 67, `test_doctor` 47, `test_guard_trust` 105, `test_hooks` 81, `test_live_store_guard` 324 |
| mut2, 07:54Z | r3's 10 mutants and r1's 7, from the worktree at `eb178c600` | control passed (13 named tests); r3's 10 of 10 killed; `child-form-dropped` killed; `new-roots-removed` not counted: its named test errored in the `hooks` fixture, whose r2 rebuild no longer hashes to `R2_HOOK_SHA256` after an edit outside the r3 blocks (latent since r3), so `8ba16fa4d` names it and the two other r1 root mutants by tests that read the hook directly; the hold's bound ended it there |

Unrun when the round's 4-hour mark came (08:11Z): file3 (queued 07:32Z, never given a slot),
which held `test_r3_property_the_verdict_is_the_rule`, `test_r3_globs_agree_with_real_zsh_and_bash`,
S5 with its fix, and a rerun of the r3 and r3b example tests and the r3b property at this hook
(they passed in the check hold, at `7589d54b2`, whose hook differs from this one only in
`pw_word` clearing `nocaseglob` and in comments); r1's five remaining mutants (`prefix-match`,
`trailing-slash-missed`, `trust-hash-stale`, `trust-built-on-repo-copy`, `env-spellings-dropped`)
and `new-roots-removed` under its new name; the near-miss case `8ba16fa4d` adds; and S1-S5 at
`GUARD_DIFF_SCALE=2` (3,718 generated commands, about three hours of hook runs at this load).

S1 at scale 1: 781 commands (531 generated, 308 of them env twins, 255 fixed); the old hook
denies 124 and the new 246; 245 carry a path word the pass may move, 39 of which move (all
tightened). S1-r3 against the r2 hook: 245 labelled (76 `file`, 136 `root`, 33 `glob`), 59 moved,
none the wrong way and none unlabelled. S2: 227 bounded or deep searches allowed by both hooks.
S5: 308 twins over 154 references and 84 sources, 52 denied and 256 allowed.

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
- **A searcher right after a quote** was not detected before r3c (`bash -c 'find / -name x'`,
  `eval 'grep -r x ~/.subfleet/jobs'`, `bash -c 'rg --files $SUBFLEET_ROOT'` are allowed by the
  live hook). r3c's decision 5 detects it and its decision 6 judges the script as its own line, so
  all of them are denied now; the Claude-era prefilter literal is kept byte for byte and a third
  term carries the new spellings.
- **`xargs -n1 find`** (review r1, P3-3, seen at scale 2). `printf '%s\n' ~/.subfleet/ | xargs -n1 find`
  passes: a `find` that ends the command is not detected as a searcher (the detection and the
  prefilter want whitespace after it). Pre-existing: `printf '%s\n' ~/ | xargs -n1 find` passes
  the old hook.
- **Other gaps shared with every root** (review r1, P3-3), each pre-existing:
  - spellings the rule does not normalize: a `$'…'` that starts a word (`$'/Users/me/.subfleet'`;
    r3c reads a `$'…'` inside a path word as `*`); `~+` and `~-`; `env -C ~/.subfleet rg --files`;
    and parameter forms of the env spellings other than `$NAME` and `${NAME}`
    (`${SUBFLEET_ROOT:-~/.subfleet}`, as `${HOME:-…}` today). r3's decision 2 took the trailing
    `/*` out of this list; r3b's normalization took `~//.subfleet`, `~/.subfleet/./jobs`, a
    trailing `/.` or `/..`, `~/".subfleet"`, `~/.Subfleet` and `/System/Volumes/Data/…`; r3c took
    `~user`, `…/jobs/*/a1` (1(c)), braces, zsh alternation, backslash escapes, `***`, a `/` inside
    brackets, U+017F and U+212A, and `/Volumes/<entry>/…`;
  - shell structure: a quoted for-loop list (`for d in "$HOME/.subfleet"; do find $d …; done`
    passes; the unquoted form is denied);
  - searchers the rule does not know: fd, bfs, gfind, ggrep, ag, `git grep --no-index`, tree, du
    and `ls -R`, and a searcher named through a variable (`RG=rg; $RG --files ~/.subfleet`; r3c
    finds one named by a path, a backslash, a quote or `$(which …)`, and egrep, fgrep, rgrep and
    ugrep).
- **The cwd list is unchanged.** `rg --files .` with a payload cwd of `~/.subfleet` passes; the
  list never held the other home children either (`Library/Caches`, `.cache`, `.local`). A
  Codex lane's cwd is its job workdir, normally a worktree two levels under `~/.subfleet`. Since
  r3c a recursive searcher given no path is judged by this list too (`rg foo` sitting in the
  home is denied).
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
  are D-LANE-SCOPE2's); a glob character inside quotes is judged as if
  unquoted, as the property the hub set requires of a quoted `/*`, which over-approximates (the
  shell would not glob it): `find . -path '/tmp/*'` is denied now, the quoted `/tmp/*` read as
  `/tmp`, though find takes it as a pattern; and a glob in rg's
  pattern position that expands to several words puts its later matches in path position, which
  is not judged.
- **r3b's limits.** The shared one (decision 6): a link inside a listed folder that points into a
  mount that may stall is still resolved, as a written path's links are; snapshot-5 has the same.
  Also: the cwd list is unchanged (a `.` search sitting in `/usr` passes); a symlink loop is left
  unresolved; a `?` in a glob matches one byte of a non-ASCII name; a script under
  `bash -O globstar -c` is read as zsh (its `**/` walks), which matches; and a `bash -c` script
  that spans lines loses its context (its `**/` walks).
- **r3c's limits.** A glob inside a mount entry is judged by name (root names plus any other
  name) and never listed, so it over-approximates (`/Volumes/Ext/Users/zz*` is denied though
  nothing matches) and does not see a link there (`"/Volumes/Macintosh HD/Users/me/"link-to-*`);
  a link among the matches of a `<glob>/..` is not followed (1(a) reads it as the folder);
  a bare `*` between two plain names under a folder that is listed is judged by its matches
  (`rg -l foo ~/*/src` passes; under a folder that is not, it is no match: `~/PolicyEngine/*/src`
  passes, as snapshot-5 has it); an unquoted depth-bound spelling used as a value still exempts
  its command (`rg -e --max-depth ~`); a nested script is judged in its shell's reading but
  without the outer command's cd; and a top-level word is read the zsh way only, which for every
  r3c form denies whatever bash's reading would (bash refuses `(a|b)`, leaves `/[^/]*` unmatched
  and reads `**` and `***` as `*`), so "deny if either shell denies" holds without a second pass.
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
   shasum -a 256 private/guard/never-rules-hook.sh private/guard/TRUST   # the reviewed pair (r3c)
   # 7a5ad79d3f7db4c3c08bf07b8455344a4bdae3e66224218094b398cdf1091a1a  private/guard/never-rules-hook.sh
   # 68936fafa42f885c29fadd9b48c2de48ace5e1ccd29b9c0cee0a09ad9e1ac91a  private/guard/TRUST
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
   shasum -a 256 ~/.subfleet/guard/never-rules-hook.sh ~/.subfleet/guard/TRUST   # 7a5ad79d…1a1a, 68936faf…c91a
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
   and the old TRUST is this TRUST with `hook_sha256` set back to `a60d1c51…` and all four dated
   additions removed, D-LANE-SCOPE1's, r2's, r3's and r3b's (with r3c's corrected clause): cut the provenance after "Previous pin kept at
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
