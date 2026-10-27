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
alternation, links before `..`, searchers named by path or quote, and the other escapes. Round r4
(2026-10-26, "r4" below) closes those of the hub's review r3 (REQUEST_CHANGES at `d9d4f355e`): a `..`
after a glob, a `**/` after a wildcard, every script a shell or `eval` runs, searchers after keywords,
wrapper options and quotes, brace groups across a `/`, hostile words' cost, a device on stdin,
print-and-exit forms, `~$USER`, `$"…"` and `/Volumes/<entry>/..`. Round r5 (2026-10-27, "r5" below)
closes those of the hub's review r4 (REQUEST_CHANGES at `6afb5ce19`): a long word's cost, heredoc
bodies, a depth bound beside xargs or `-exec`, a root before a closing backtick, bash's `.*` and `.?`,
the data volume's alias before a mount, commands too long to read, `~{…}`, `~$NAME` and `~$(…)`,
zsh's `=cmd`, pipes into a shell, `"$@"` scripts, `eval $(…)` and `<&0`. Nothing here touches the
live guard; the hub installs it with the steps below.

## What changed

**The hook** (`private/guard/never-rules-hook.sh`, SHA-256 `0b00cc61…789b` after r5; r4v's was `1d8bc1e5…e58c`, r4's `4b382c29…79ec`, r3c's `7a5ad79d…1a1a`, r3b's
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

**TRUST** (`09ed48d3…17a8` after r5; r4v's was `3c7c8af8…f9bb`, r4's `807f0e8b…7fb4`, r3c's `68936faf…c91a`, r3b's `0d5dac0f…54d8`, r3's `12b5f31a…58a2`, r2's `aa09f53d…a2f0`). It is built on the installed TRUST of 2026-09-29 (`b3193b97…`, the
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
`674abdac7`: of the 96 red inputs, 94 were allowed there; the other two, `~/.subfleet/{jobs,src}`
and a Kelvin-sign `worKtrees`, were already denied as one child's name. The red hold (the seven
r3c tests at `a7ac35e20`, the hook still r3b's) gave 6 failed and 1 passed (the allowed list).

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
  (`R3C_ALLOWED`, 46 cases with the brief's must-stay-allowed ones), `rg -l foo ~/*/src`, and the
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

**r3c's holds** (each one pytest under the shared lock; the guard file in six `-k` parts, which
together select all 81 of its items, verdicts carried between them by `GUARD_DIFF_CACHE`):

| Hold | Admitted, load | Run | Result |
| --- | --- | --- | --- |
| red | 11:56Z, 24 | the seven r3c tests at `a7ac35e20` | 6 failed, 1 passed |
| A | 13:09Z, 45 | examples, TRUST, fence, known limits, r3c tests (43 items) | 43 passed in 340 s |
| B | 13:17Z, 48 | r3 and r3b examples, tables, mounts, shell states, ported vectors | 39 passed in 185 s |
| D | 13:20Z, 71 | S1, S1's protection, S2, the cwd list, S4 | 4 passed, 2 skipped (S4: no analyzer) |
| E | 13:24Z, 50 | S1-r3, S5, S3 | 4 passed in 174 s |
| F | 13:27Z, 53 | r3's property, r3's and r3c's shell differentials | 3 passed in 525 s |
| G | 13:38Z, 51 | r3b's and r3c's properties | 2 passed in 280 s |
| overlay | 14:13Z, 129 | `tests/unit/test_guard_overlay.py` | 22 passed in 7 s |
| guards | 14:22Z, 129 | the three contract guards | 63 passed in 22 s |
| mutants | 14:23Z, 123 | the driver, `--only` r3c's seven | control passed; 7 of 7 killed |

S1 at scale 1: 785 commands; the original hook denies 124 and this one 308; 186 differences are
labelled intended (r3, r3b or r3c forms), 184 of them tightening and two S3's apply_patch case
(denied by both, under another rule); none is allowed by this hook while the original denies it.
S1-r3 counts 602 commands with an r3c form, and no form that only denies more ever allows more
than the r2 hook. S2 checked 227 commands, S5 308 twins. The two holds queued at 13:47Z and 13:57Z
were stopped unadmitted after 590 s each (both slots busy, load over 120) and requeued.

| Mutant (decision) | Killing test | Named test | Fence |
| --- | --- | --- | --- |
| `r3c-wild-only-dropped` (1(b)) | `test_r3c_globs_that_match_every_entry_are_their_folder` | killed | passed |
| `r3c-subchild-dropped` (1(c)) | `test_r3c_globs_that_match_every_entry_are_their_folder` | killed | passed |
| `r3c-volumes-mapping-dropped` (2) | `test_r3c_volumes_entries_are_judged_as_the_rest_of_the_path` | killed | passed |
| `r3c-braces-dropped` (3) | `test_r3c_braces_and_alternation_are_judged_by_their_alternatives` | killed | passed |
| `r3c-physical-dots-dropped` (4) | `test_r3c_dotdot_after_a_link_is_physical` | killed | passed |
| `r3c-basename-dropped` (5) | `test_r3c_searchers_are_found_by_basename` | killed | passed |
| `r3c-unescape-dropped` (6) | `test_r3c_the_other_escapes_are_closed` | killed | passed |

r3's and r3b's 23 mutants were not run (the 17-mutant driver waits for the Budget hold to end); six
of their edits whose text r3c moved are retargeted at the new code, and every edit in the driver
matches the hook exactly once.

**r3c's pair** (superseded by r4's, below): hook
`7a5ad79d3f7db4c3c08bf07b8455344a4bdae3e66224218094b398cdf1091a1a`, TRUST
`68936fafa42f885c29fadd9b48c2de48ace5e1ccd29b9c0cee0a09ad9e1ac91a`. The installed pair step 0
expects, and the rollback: hook `a60d1c51…`, TRUST `b3193b97…` (unchanged by r3c).

## r4: review r3's walk-arounds closed (2026-10-26)

**Why.** The hub's review r3 (REQUEST_CHANGES at `d9d4f355e`) confirmed three more ways around the
rule, each through the whole hook and in real `/bin/zsh -f` and `/bin/bash`: a `..` after a glob
climbs out of the judged prefix (`find ~/.sub*/jobs/<id>/.. -name AGENTS.md` is the incident's
crawl), a `**/` after a wildcard-only component is not read as its folder
(`rg foo ~/PolicyEngine/*/**/*.py`), and shell and `eval` scripts the r3c unwrap missed
(`bash -euo pipefail -c 'rg --files ~/.subfleet'`, `eval 'rg' --files ~/.subfleet`); and seven
P3s. Every input below is review r3's; the pins run through the whole hook in the fixture seam
(home `/Users/tester`).

**The decisions as built** (the hub's numbering):

1. **A `..` anywhere after a glob is folded** (`pw_fold`). Each `<component>/..` after a word's
   first glob component is folded away by name; a `..` that reaches back into the literal part
   before it stays, and `pw_physdots` applies it physically (r3c's decision 4). The word is broad
   when the folded or the unfolded reading is. In the unfolded reading `pw_globbroad` now keeps a
   candidate that a later `..` may climb out of, broad or not, so that `..` is applied to it
   physically: `rg --files ~/w*/jl/..` (`jl` a link into a job folder) is denied. Review r3's four
   inputs are pinned, with `rg --files ~/.subfleet/jobs/*/../..`.
2. **A `**/` after a wildcard-only component.** The trailing-run loop (1(a)/1(b)) runs on the part
   before a `**/` whether or not the word walks, so `~/PolicyEngine/*/**/*.py`, `…/?*/**/*.py`,
   `…/[!.]*/**/*.py` and `/tmp/*/**/x` are their folders. `~/work/*/**/*.py` and a lane
   worktree's `W/*/**/*.py` pass.
3. **Every shell and `eval` script is judged.** The scripts moved from the judge pass to the
   command prep (`pw_prep_awk`), so the searcher detection and the cwd rule see them too: each
   script is added to `pw_cmd` as its own lines, each line led by `\003<context>\003` and a space
   (`b` in a script run by bash, sh, dash or ksh without globstar, `z` otherwise), and itself read
   the same way, to depth 4 (a deeper searcher still adds r3c's deny). A shell invocation is read
   word by word (`shstep`): `-o X`, `+o X`, `-O X`, `+O X`, a cluster ending in `o` or `O`
   (`-euo pipefail`), `--rcfile X` and `--init-file X` take the next word, `--` or `-` ends the
   options, the shell's name is read dequoted (`"bash"`, `'bash'`, `/bin/b\ash`), and the first
   other word is the `-c` script. `eval`'s arguments are joined (a leading `--` dropped). A shell
   given no script (or `-s`) reads its here-string, or what an `echo`, `print`, `printf` (its
   arguments joined by newlines, as snapshot-5 joins them) or `cat <<<` piped into it writes. A
   `$'…'` is decoded as bash decodes it (`\xHH`, `\NNN`, `\'`, `\n` and the rest), in the prep's
   reading of words and in the judge pass's script reading; the run regexes end a `$'…'` at its
   real closing quote. The prefilter gains a fifth term for a `$'…'` holding an escape. Review r3's
   inputs, `+o`, `+O`, `--rcfile`, `printf` and `cat <<<` forms, and `echo … | sudo -u root bash`
   are pinned; `nest(…, 4)` stays allowed and five deep stays denied.
4. **Searcher detection matches snapshot-5's.** In command position the prep skips shell keywords
   (snapshot-5's KEYWORDS: `if`, `then`, `else`, `elif`, `while`, `until`, `do`, `!`, `{`, `}`,
   `done`, `fi`, `esac`, `coproc`, and `function NAME`; a function body's `{` is the `{` after
   `f()`), assignments, redirections, and the wrappers, matched by basename (`/usr/bin/env`), with
   snapshot-5's WRAPPERS table of options that take a value (`sudo -u`, `timeout -s`/`-k`, `exec
   -a`, `env -u`/`-C`/`-S`, `xargs -I`/`-n`/…, `nice -n`, `time -o`/`-f`, `stdbuf`, `caffeinate`,
   `ionice`, `doas`, `gtimeout`, `chronic`, `unbuffer`, `noglob`, `nocorrect`) and timeout's
   duration. A name is read dequoted (`r''g`, `r\g`, `"r"g`, `$'rg'`, `f\ind`), and the prefilter's
   fourth term reads a dequoted copy of the command. The segment regexes see the canonical name
   wherever the prep writes it: the prep splits a segment at a backtick as at `(`, writes a space
   after a backtick or a `)`, and keeps `>&`, `<&` and `&>` inside their segment. Every input of
   review r3's P3-1 is pinned, with `nice -n 5 /usr/bin/find /` (review r3's surviving mutant), and
   the overlay doc's false "stricter" sentence is gone.
5. **Brace groups.** The groups are found in one pass with a stack (`pw_groups`); a `${…}` span is
   skipped, so `{/Users/${USER},x}/.subfleet` is a comma group of `/Users/${USER}` and `x`. A word
   whose group is read as `*` (nested, a sequence, or more than 64 words) and holds a `/` or a `..`
   component is denied (`pw_fc`); that fails closed on a rare form (`rg foo src/{a,b/c}{1..9}` in a
   worktree would be denied). Review r3's five inputs are pinned, with `rg --files ~/.subfleet ~/{a`.
6. **Cost on hostile words fails closed.** A path word with more than 64 `{` or `[`, or over 4 KB
   (bytes), is broad without being scanned (`pw_capped`; its message says so). The brace scan is
   one pass (`pw_groups`), and the bracket scans (`pw_brackets`, `pw_pattern`) stop where a bracket
   that never closed walked. **Deviation, with its counterexample:** a word of 2,040 components in
   4,080 bytes (`/a/a/…`, under the 4 KB cap) cost 7.4 s in `pw_physical` alone (r3c's loop as
   well), so a word with more than 256 components is capped too; it is then 0.08 s. Further cost
   fixes, none changing a verdict: counts and splits by field splitting (bash's `${x//…}` took 13 s
   on a 10 KB word, and `${x%%…}` is quadratic too), a word's folder resolved once and kept for the
   next word (`pw_pcd`, `pw_pcr`), a plain deep word in a folder already resolved with no link
   judged with one lstat, the plain absolute word sent straight to the written-path judgment, the
   stall check and the alias folding without option toggles, pass 1's output split in memory, and
   awk's `sform` and `value` returning a run with no quote, backslash or `$` as it is.
7. **A device or a closed stdin is no pipe.** rg's stdin is taken as its redirections leave it, in
   order: a here-string, a heredoc or a `<` from a file is readable, a `<` from any `/dev/` file
   (`</dev/null`, `< /dev/null`, `0</dev/null`, `</dev/zero`) or `<&-` is not, and `<&N` stays
   readable. **Deviation:** a redirection on another fd (`3<file`) leaves stdin as it was; r3c
   counted any `<` as stdin. A pipe and a file stay as r3c built them.
8. **Print-and-exit forms walk nothing.** A simple command whose searcher prints and exits is read
   as `:`, as a depth bound is: rg's `--help`, `-h`, `--version`, `-V`, `--type-list`,
   `--generate`, `--pcre2-version`; find's `-help`, `--help`, `-version`, `--version`; and grep's
   `--help`, `-V`, `--version` (snapshot-5's `grep_model`; an addition to the brief's list), each as
   an option of its own, not another option's value (`rg -e --version`, `rg -g --version foo`,
   `find -name --version`, `rg -- --version` stay denied from the home).
9. **The unlisted forms.** `~$USER`, `~${USER}` and `~$(whoami)` are read as the home; `$"…"` is
   read as `"…"`, in a path word and in a script; and a searcher's `/Volumes/<entry>/..` is also
   read with the entry as `/`, so `"/Volumes/Macintosh HD/../Users/me/.subfleet"` is the home's
   `.subfleet` (the by-name reading, `/Volumes/Users/…`, is still judged too). zsh's `:h`
   modifiers are named under "qualifiers are not judged" in the known limits.
10. **The overlay doc and the known limits** are rewritten from the code (below and in
    `docs/private-guard-overlay.md`).
11. **TRUST** changes only `hook_sha256`; no decision changes what the provenance says, so no
    sentence is added. Step 0 lists the installed pair and the reviewed pair; the rollback pair
    stays `a60d1c51…`/`b3193b97…`.

**Commits.** `c4667dc7e` red tests; `537b71e23` the hook, decisions 1–9 in one commit (the command
prep carries 3, 4, 7 and 8, and the word judge 1, 2, 5, 6 and 9, through the same functions), with
the fence's `R4_TEXT`; `e46212414` TRUST; then the cost fixes found by the hostile-word timing (the
256-component cap, the folder walk without recursion, the field splits and the awk fast paths), the
mutation driver, and the docs.

**Red at `d9d4f355e`.** Through the scratch probe (the fixture seam): of the 71 inputs that must be
denied, 68 were allowed and three denied (review r3's surviving-mutant pins: `eval 'rg --files
~/.subfleet'`, `nice -n 5 /usr/bin/find / -name x`, `rg --files ~/.subfleet ~/{a`), and the six
print-and-exit forms were denied from the home; the installed hook allowed all 77. All 13 r4 tests
failed (`c4667dc7e` against the r3c hook), including the 300-path cost (median 6.2 s against the
installed hook's 1.8 s) and a hostile word past the 120 s run limit.

**The real shells.** `test_r4_command_pins_agree_with_real_shells` runs each command pin and
must-allow form (119) in `/bin/zsh -f` and `/bin/bash` over the fixture, with stub `rg`, `find` and
grep-family searchers (and stub `sudo`, `doas`, `timeout`, `gtimeout` that run the rest) that record
their argv, cwd and stdin; a run walked a root when a path the stub would have walked (snapshot-5's
models, cut down) is broad by REFERENCE_SH. The hook denies a command exactly when either shell
walked a root:

| The shells walked a root | Commands | The hook |
| --- | --- | --- |
| both | 75 | denied |
| zsh only (`~$USER`, `~${USER}`, `~$(whoami)`: bash leaves them literal) | 3 | denied |
| bash only (`$"…"` and `bash -c $"…"`: zsh reads `$` and the string) | 2 | denied |
| neither (the must-allow forms, print-and-exit, scoped scripts and wrappers) | 39 | allowed |

`"/Volumes/Macintosh HD/../Users/tester/.subfleet"` is among the 75 by its documented by-name
reading (the fixture holds no link to `/` for the boot volume's alias). Decision 2's walks are the
shell's own, so `test_r4_words_agree_with_real_shells` checks 13 words with the word oracle
(`r4_real`: r3c's, with decision 2 as written); they agree.

**Timing** (best of three for the hostile words; medians of five interleaved runs for 300 paths):

| Run, load | 1,000 unclosed `{` | `/[` ×1,000 | 10,000 commas | 2,040 components | 64 `{` in 4 KB | 64 `/[` in 4 KB | 300 paths, new / installed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| red (r3c hook), ~50 | timed out (> 120 s) | | | | | | 6.2 s / 1.8 s |
| hold 1 (`e46212414`), 106 | 1.79 s | 3.61 s | 7.90 s | | 6.18 s | 5.98 s | 5.52 s / 4.76 s (1.16×) |
| direct, final cost fixes, 92–118 | 1.11 s | 1.64 s | 1.69 s | 1.16 s | 2.37 s | 5.09 s | 1.50 s / 1.03 s (1.46×) |
| r4v hold, `4b382c29…`, 23–28 | 0.82 s | 1.02 s | 0.68 s | 0.69 s | 1.58 s | 4.30 s | 0.72 s / 0.68 s (1.06×) |
| r4v hold, `4b382c29…`, 45→44 | 0.43 s | 0.37 s | 0.48 s | 0.43 s | 0.66 s | 0.98 s | 0.57 s / 0.57 s (1.01×) |
| r4v run 1, `1d8bc1e5…`, 51→49 (300 paths 49→52) | 0.49 s | 0.34 s | 0.39 s | 0.38 s | 0.86 s | 2.27 s | 0.68 s / 0.44 s (1.53×) |
| r4v run 2, `1d8bc1e5…`, 41→39 (300 paths 39→39) | 0.30 s | 0.32 s | 0.35 s | 0.32 s | 0.55 s | 1.51 s | 0.34 s / 0.25 s (1.37×) |
| r4v run 3, `1d8bc1e5…`, 51→48 (300 paths 48→48) | 0.45 s | 0.54 s | 0.53 s | 0.32 s | 0.54 s | 1.75 s | 0.53 s / 0.37 s (1.44×) |

At low load (32) the capped words took 0.21–0.35 s. The two scanned edge words (64 `{` or `[` in
just under 4 KB) are judged and denied, but are slower than 2 s at load ~100: the residual cost,
reported (`R4_CAP_EDGE`) and not bounded.

The r4v rows are inside holds, with the 1-minute load at each test's start and end. The first two
ran at r4's head; the three runs at the final hook (`1d8bc1e5…`) were each in their own hold. Every
bounded word stayed under `R4_HOSTILE_S` (2.0 s), and 300 paths stayed under twice the installed
hook. The 300-path ratio moves between runs (1.01× to 1.53×) because the installed hook's own
median moves (0.25 s to 0.68 s). The fix below doesn't explain it: through r4's hook and the final
one, interleaved 15 times each in one hold at load 27–31, the final hook's median was 0.95× r4's
(0.88 s against 0.93 s).

**r4's runs.** The 3-hour mark (21:38Z) fell after hold 3 was admitted, so three holds ran under
the shared lock, all at `e46212414` (hook `efa86362…`); the cost fixes and the hook's last two edits
came after, and the final head was checked by direct runs outside the lock (the fixture seam, four
workers at most), each named below with the hook it ran:

| Run | Started, load | Hook | Selection | Result |
| --- | --- | --- | --- | --- |
| hold 1 | 21:03Z slot B, 106 | `efa86362…` | the 13 r4 tests | 12 passed; the hostile timing failed (the table above) |
| hold 2 | 21:19Z slot A, 86 | `efa86362…` | `test_guard_overlay.py` and the three guards | 85 passed (22 and 63) in 340 s |
| hold 3 | 21:31Z slot B, 72 | `efa86362…` | r3c's ten items other than its property | 10 passed in 757 s (past the 10-minute split I meant to keep) |
| direct 1 | 21:57Z, ~100 | `c00b29ee…` | r4 (not the shells), r3c's examples, the fence, TRUST | 21 passed; the hostile timing failed |
| direct 2 | 22:21Z, 92–118 | `8bff5b29…` | the two timing tests | 300 paths passed (1.46×); the hostile test failed on the two cap-edge words, which then moved to `R4_CAP_EDGE` |
| direct 3 | 22:25Z, 88 | `8bff5b29…` | r4's pins and allowed lists, the overlay file, the pin tests | 34 passed |
| direct 4 | 22:30Z | `8bff5b29…` | r3b's and r3c's properties, the fence, TRUST | 4 passed; both properties failed (3 and 8 words, all a `..` after a glob or a `/Volumes/<entry>/..`: decisions 1 and 9, which REFERENCE_SH did not have yet) |
| direct 5 | 22:52Z, 89 | `8bff5b29…` | both properties, REFERENCE_SH extended from the r4 brief | r3b's passed (0 of 2,400); r3c's 1 (my reference reused the name `fold`) |
| direct 6 | 23:02Z | `8bff5b29…` | r3c's property | 0 of 2,400 at function level; 25 of 298 through the whole hook (a hook bug: the space the prep wrote after a `)` split zsh alternation words; fixed) |
| direct 7 | 23:12Z | `4b382c29…` (r4's final) | r3c's property | passed: 0 of 2,400, 0 of 298 |
| direct 8 | 23:20Z | `4b382c29…` (r4's final) | r4 but its two timing tests, the fence, TRUST, the overlay pin | 16 passed in 936 s, both real-shell tests among them (119 commands: 80 denied, zsh walked 78, bash 77; 13 words) |

**REFERENCE_SH for r4** (written from the r4 brief, not from the hook): a written path also reads a
`/Volumes/<entry>/..` from `/` (`written`); a glob word is judged as written and with each
`<component>/..` after its first glob component folded (`dotfold`), broad if either is; the trailing
run is read before a `**/` too; and between components a candidate a later `..` may climb out of
stays. The generator is unchanged.

**Counts per file at r4's head** (`4b382c29…`): `test_guard_subfleet_root.py` has 94 items (81
r3c, 13 r4). At r4's final hook: the 11 r4 tests but the two timing tests, the fence and the four TRUST
tests (direct 8), r3c's property (direct 7); at `8bff5b29…`, which differs from it only in the
backtick space: r3b's property and the timing tests (directs 5 and 2); at `efa86362…`: r3c's other
ten items (hold 3). `test_guard_overlay.py`: 22 passed at `efa86362…` (hold 2), the pin test again at
the final head (direct 8). The three guards: 63 passed (hold 2).

**r4v: the holds at the final head** (2026-10-27). Every run in this round was under the shared
lock: one pytest per hold, split with `-k` and run in the foreground, over the fixture seam's home
`/Users/tester`. The first holds ran at r4's head (`6f192c499`, hook `4b382c29…`):
- passed: the r1/r2 examples and pins (18), S1, S2, the cwd list and S4 (4 passed, 2 skipped), S3,
  S5 and S1-r3 (4), r3's examples (25), its property (1), its shells (1), and the timing pair (2);
- S3, S5 and S1-r3 took 741 s, past the 10-minute split, so they were split again below;
- the hold of r3b's examples failed `test_r3b_only_the_disk_functions_touch_the_file_system`.

The cause was decision 6's fast path in `pw_word`. It tested `! [ -L "$pw_fs$1" ]` itself, outside
the five disk functions, so the mount test's trace never saw that lstat. `da54c82de` calls
`pw_islink "$1"` there instead. That is the same test, so no verdict changes. `1edee8dc7` re-pins TRUST
(`hook_sha256` only, with no provenance sentence): hook `1d8bc1e5…`, TRUST `3c7c8af8…`. The rollback
pair stays `a60d1c51…`/`b3193b97…`. Every part then ran again at `1edee8dc7`:

| Hold | Admitted, slot, load | Selection | Result, pytest wall time |
| --- | --- | --- | --- |
| n1 | 03:00Z B, 18 | r1/r2 examples, TRUST, the fence and the known-limit pins (18); r3's examples (25); r3b's examples and ported vectors (14) and property (1); timing run 1 (2) | 60 passed, 378 s |
| n2 | 03:12Z A, 50 | S1, S2, the cwd list, S4 | 4 passed, 2 skipped (S4's two need the Claude hook's `unscoped_search.py`, in the operator's home), 246 s |
| n3 | 03:21Z A, 45 | S3 (2), S5, r3's real shells | 4 passed, 284 s |
| n4 | 03:30Z A, 66 | S1-r3, r3c's property, timing | cut off when that attempt ended (S1-r3 270 s, r3c's property 756 s at load up to 229, no JUnit); rerun as holds 5, 6 and 8 |
| 5 | 04:06Z B, 43 | r3's property; timing run 2 | 3 passed, 147 s |
| 6 | 04:09Z B, 39 | r3c's property | 1 passed (0 of 2,400 differ; 0 of 298 through the whole hook), 234 s |
| 7 | 04:19Z A, 50 | r3c's examples (7); its shells, mounts and code points (3) | 10 passed, 93 s |
| 8 | 04:24Z A, 62 | S1-r3; r4's examples (9) and real shells (2); timing run 3 | 14 passed, 231 s |
| 9 | 04:28Z A, 47 | `test_guard_overlay.py` (22), `test_guard_trust.py` (105), the three guards (37, 25 and 1) | 190 passed, 120 s |
| 10 | 04:30Z A, 61 | `test_daemon_launch` (9), `test_claude_adapter` (172), `test_codex_turn` (67) | 248 passed, 41 s |
| 11 | 04:33Z B, 60 | `test_doctor` (47), `test_hooks` (81), `test_live_store_guard` (324) | 452 passed, 54 s |

**Counts per file at the final head** (`1d8bc1e5…`):
- `test_guard_subfleet_root.py`: 94 items, 92 passed and 2 skipped (S4's). Each item ran in exactly one
  of holds n1–n3 and 5–8, checked against the collected node ids, and the timing pair ran three times.
- `test_guard_overlay.py`: 22 passed.
- The three guards: 63 passed.
- The seven never-rules modules: 805 passed, `test_guard_trust.py` included.
- `tests/frontend/test_core_live.py` can't run on a lane, so CI runs it.


**Mutants** (r4 named them but ran none, under the Budget hold; r4v ran all 49 at `1edee8dc7`, below).
Each edit is checked to match the hook exactly once; the driver now runs r4's and review r3's first.

| Mutant (decision) | Edit | Killing test |
| --- | --- | --- |
| `r4-fold-dropped` (1) | `pw_fold` returns the word unfolded | `test_r4_dotdot_after_a_glob_is_folded` |
| `r4-tree-loop-skipped` (2) | the trailing-run loop skipped after a `**/` cut | `test_r4_a_tree_glob_after_a_wildcard_component_is_its_folder` |
| `r4-scripts-dropped` (3) | no script is extracted | `test_r4_every_shell_and_eval_script_is_judged` |
| `r4-keywords-dropped` (4) | keywords are not skipped | `test_r4_searchers_after_keywords_wrappers_and_quotes_are_found` |
| `r4-starred-slash-allowed` (5) | a starred group with `/` or `..` passes | `test_r4_brace_groups_that_span_a_slash_or_climb_fail_closed` |
| `r4-cap-dropped` (6) | `pw_capped` never caps | `test_r4_hostile_words_fail_closed_fast` |
| `r4-device-is-stdin` (7) | any `<` is stdin again | `test_r4_a_device_or_a_closed_stdin_is_no_pipe` |
| `r4-meta-walks` (8) | print-and-exit forms walk | `test_r4_print_and_exit_forms_walk_nothing` |
| `r4-tilde-user-dropped` (9) | `~$USER` is not read as the home | `test_r4_the_unlisted_forms_are_judged` |
| `r3c-wrappers-env-only` (review r3) | only `env` is a wrapper | `test_r4_searchers_after_keywords_wrappers_and_quotes_are_found` |
| `r3c-unbalanced-brace-allows` (review r3) | an unclosed `{` exits 0 | `test_r4_brace_groups_that_span_a_slash_or_climb_fail_closed` |
| `r3c-physical-stall-guard-dropped` (review r3) | `pw_physical` resolves under a mount | `test_r3c_nothing_inside_a_mount_is_read` |
| `r3c-in-mount-glob-lists` (review r3) | a glob inside a mount entry lists it | `test_r3c_nothing_inside_a_mount_is_read` |
| `r3c-eval-unwrap-dropped` (review r3) | `eval`'s script is not judged | `test_r4_every_shell_and_eval_script_is_judged` |

Review r3's three survivors' pins are in the r4 tests: `nice -n 5 /usr/bin/find /`
(`R4_NAMES_DENIED`), `rg --files ~/.subfleet ~/{a` (`R4_BRACES_DENIED`) and `eval 'rg --files
~/.subfleet'` (`R4_SCRIPTS_DENIED`). Five earlier edits whose text r4 moved (`r3c-braces-dropped`, `slash-star-left-as-text`,
`r3c-wild-only-dropped`, `case-sensitive-roots`, `non-last-keeps-all`) are retargeted at the new
code; the driver holds 49 mutants.

**r4v's mutant runs.** `tools/guard_subfleet_root_mutations.py --only <group>` ran at `1edee8dc7`
under the lock, four groups in four holds. Each run exported HEAD, and its control passed every
named test of its group and the fence before any mutant ran. Then each mutant ran its named test
and the fence. Every edit still matches the final hook exactly once.

| Hold | Admitted, slot, load | Mutants | Control | Killed | Survived | Wall |
| --- | --- | --- | --- | --- | --- | --- |
| m1 | 04:35Z B, 32 | r4's 9 | 10 passed | 9 | 0 | 227 s |
| m2 | 04:40Z B, 37 | review r3's 5, r3c's 7 | 11 passed | 12 | 0 | 288 s |
| m3 | 04:45Z A, 43 | r3b's 11 | 6 passed | 10 | 1 | 361 s |
| m4 | 04:53Z A, 39 | r3's 10, r1's and r2's 7 | 13 passed | 17 | 0 | 596 s |

- **Killed, 48:**
  - r4's 9 and review r3's 5, each by the test the table above names.
  - r3c's 7, each by the test r3c named:
    - `r3c-unescape-dropped` by `test_r3c_the_other_escapes_are_closed`;
    - `r3c-basename-dropped` by `test_r3c_searchers_are_found_by_basename`;
    - `r3c-physical-dots-dropped` by `test_r3c_dotdot_after_a_link_is_physical`;
    - `r3c-braces-dropped` by `test_r3c_braces_and_alternation_are_judged_by_their_alternatives`;
    - `r3c-volumes-mapping-dropped` by `test_r3c_volumes_entries_are_judged_as_the_rest_of_the_path`;
    - `r3c-subchild-dropped` and `r3c-wild-only-dropped` by `test_r3c_globs_that_match_every_entry_are_their_folder`.
  - r3b's 10:
    - `file-test-on-stall` and `volumes-name-stated` by `test_r3b_nothing_inside_a_mount_is_read`;
    - `written-link-dropped`, `alias-folding-dropped`, `usr-dropped` and `volumes-not-listed` by
      `test_r3b_the_new_roots_aliases_case_links_and_globs_block`;
    - `non-last-keeps-all` and `plain-name-unchecked` by
      `test_r3b_scoped_paths_no_match_and_mount_contents_stay_allowed`;
    - `holds-root-fixed-list` by `test_r3b_listable_folders_are_computed`;
    - `case-sensitive-roots` by `test_r3b_root_table`.
  - r3's 10:
    - `state-not-restored` by `test_r3_glob_expansion_restores_every_setting`;
    - `slash-star-left-as-text` by `test_r3_a_bare_star_after_a_root_is_that_root`;
    - `case-insensitive`, `nomatch-judged-by-text`, `dot-rule-dropped` and `glob-as-single-file` by
      `test_r3_selective_globs_are_judged_by_what_they_match`;
    - `env-value-used` by `test_r3_env_values_are_never_read`;
    - `exists-dropped` and `not-dir-dropped` by `test_r3_a_directory_a_link_to_one_and_a_name_stay_roots`;
    - `file-exception-removed` by `test_r3_a_file_is_never_a_broad_root`.
  - r1's and r2's 7:
    - `new-roots-removed` and `child-form-dropped` by `test_each_direct_child_is_a_root`;
    - `prefix-match` by `test_near_misses_patterns_prose_and_bounded_searches_stay_allowed`;
    - `trailing-slash-missed` by `test_trailing_slash_spellings_are_denied`;
    - `trust-hash-stale` by `test_trust_pins_the_new_hook_bytes`;
    - `trust-built-on-repo-copy` by `test_trust_keeps_the_installed_lane_pin`;
    - `env-spellings-dropped` by `test_every_env_spelling_is_a_root_one_level_down_and_no_deeper`.
  - The fence failed too on the four root edits of r1 and r2 (`new-roots-removed`, `child-form-dropped`,
    `prefix-match`, `trailing-slash-missed`) and on `env-spellings-dropped`, and passed on the other 44.
- **Survived, 1: `stall-mount-listed` (r3b), and it is equivalent since r3c.** The edit turns the
  listing guard in `pw_globbroad` into `elif pw_holds "$p" || pw_stall "$p"; then`.
  - The added `pw_stall "$p"` is evaluated only when `pw_holds "$p"` is false, and `pw_stall` sets
    nothing.
  - r3c's mount branch comes first: `if pw_stall "$p" && ! pw_volumes "$p"`, where nothing is
    listed. So any `p` that reaches the guard with `pw_stall` true also has `pw_volumes` true, which
    means it is `/Volumes` in some letter case.
  - `pw_holds` is always true there. Its first name gives `/Volumes/TheAxiomFoundation`, which
    `pw_isbroad` takes as a `/volumes/<entry>` root under any home, and the depth rule skips `/V…`.
  - So no input reaches the added disjunct with a different outcome. At r3b's head (`674abdac7`)
    only the plain-name branch came before the guard. r3c (`8216108f8`) added the mount branch
    ahead of it, and that made this mutant equivalent.
  - Executed, in one hold (05:09Z, slot B, load 47), with HEAD and the mutant exported the driver's
    way (mutant hook `fdae3f5a…`), over the fixture with home `/Users/tester`:
    - the 20 `MOUNT_WORDS` plus 35 more mount words (`/Volumes/*`, `/vOlUmEs/*/*`, `/Volumes/*/..`,
      `/V*/../V*/*`, `/[Vv]olumes/*`, `/Volumes/**/x`, `/NET/*`, `/h*/*` and the rest): identical
      verdicts (29 denied) and identical disk-call traces, 112 calls in the batch and per word;
    - `pw_stall`, `pw_volumes` and `pw_holds` over 19 stall spellings under the homes `/Users/tester`,
      `/Users/me` and `/srv/me`: `pw_holds` is true for every spelling where `pw_stall` and
      `pw_volumes` both are (`/Volumes`, `/VOLUMES`, `/volumes`, `/vOlUmEs`,
      `/System/Volumes/Data/Volumes`, `/private/../Volumes`, `/Volumes/.` and `/Volumes/x/..`);
      every other stall spelling has `pw_volumes` false, so the mount branch takes it.
  - No test can kill it, so none was written. The driver still exits 1 on it until the hub retires
    or retargets it.

**Hashes for the install.** The new pair, after r4v's fix: hook
`1d8bc1e54f8e1838dd21b09026119beda53e0c293c0cafb905527b701e5fe58c`, TRUST
`3c7c8af80f34f0b8f8419355db66d654b730d08a37bc510f0bcacc8247f8f9bb`. r4's own pair (`4b382c29…`,
`807f0e8b…`) is superseded. The installed pair step 0 expects, and the rollback: hook `a60d1c51…`,
TRUST `b3193b97…` (unchanged by r4 and r4v).

## r5: review r4's findings closed (2026-10-27)

The hub's review r4 (REQUEST_CHANGES at `6afb5ce19`) found three P2s and nine P3s; it added one test
(`cb63c20a1`, `test_review_r4_pins_r4s_unpinned_readings`), which stays and passes. r5 builds the
hub's eleven decisions. Every pin is review r4's input or the brief's, judged through the whole hook
in the fixture (`test_r5_*`), and each command pin also runs in real `/bin/zsh -f` and `/bin/bash`
3.2 over the fixture with stub searchers (`test_r5_command_pins_agree_with_real_shells`; r5's stubs
add a `find` that records its argv and runs the real `/usr/bin/find` on the fixture, and no-op `git`
and `python3`).

**Decisions as built.**

1. **`base()` is one split** (`function base(v, n, a) { n = split(v, a, "/"); return a[n] }`). It
   was not the only quadratic piece: on a 60,000-character slash-free word the prep still took
   3.7 s and the no-path pass 2.5 s, and a double-quoted word 6.1 s, because this BSD awk takes a
   string's length on every `substr`, so every loop that read a long word a character at a time was
   quadratic (`simple()`'s tokenizer, `value()`, `ansic()`, `sqend()`, `dqend()`, and the judge
   pass's `sform()`). Each now takes a run of plain characters, or of blanks, with one anchored
   `match` on a 512-character window, and `lits()` maps a quoted string's `{ } , ( | )` with six
   `gsub`s. No awk `sub`, `gsub` or `match` in the hook starts with `.*` now; every unanchored one
   (`subst()`, the judge pass's run and quote matches, the gap's redirection `gsub`, `lits()`) starts
   with a literal or a one-character class, and the profiler below measured each pass.
2. **Heredoc bodies are data.** `prep()` finds each `<<WORD`, `<<-WORD`, `<<'WORD'` and `<<"WORD"`
   (several on a line, in order; a `<<<` is a here-string); the lines after the line that holds it,
   to its delimiter line (leading tabs stripped for `<<-`), are its body. The body is left out of
   quote and separator tracking and written out as it stands, so the text rules read it as the
   installed hook does while the prep's own readings skip it; it is handed to `simple()` as the stdin
   of its command, so a shell given no script judges it as its script and `cat` writes it to a pipe.
   `simple()` never receives body text, so its tokenizer needed no change. An unterminated heredoc
   leaves the rest read as commands.
3. **A depth bound covers only its own command's walk.** find's own arguments end at `-exec`,
   `-execdir`, `-ok` and `-okdir`, and only a bound among them exempts find's walk. The command each
   runs is judged on its own (`execs()`), `{}` standing for find's roots (`.` when it names none). A
   bounded command keeps its words for the xargs rule (`bound()`): its searcher names become `:` and
   find's roots are written as the shell reads them, so a bounded `find X | xargs <searcher>` is
   judged with X as the consumer's roots. A bound on the consumer still exempts it.
4. **One copy of the overlay doc.** Its lines 236-408 (the old body, from the original pair's table
   to r3c's list with the removed sentence) are gone, so the new pair's table follows r4's known
   differences; no heading and no three-line block repeats (`test_r5_the_overlay_doc_holds_one_copy_of_its_body`).
5. **A closing backtick ends a root:** the prep adds a space before it too, and `root_end` stops at a
   backtick. *Deviation:* `` echo "`rg --files ~/.subfleet`" `` stayed allowed with both, since the
   text rules collapse a double-quoted string as prose before reading a position (the same holds for
   `echo "$(rg --files ~/.subfleet)"`). A command substitution inside double quotes runs, so the prep
   now judges its command as its own lines (`spans()`). Two pins were added: `` echo `rg --files /usr` ``
   and `echo "$(rg --files /usr)"`, a root only the path-word pass knows.
6. **A component bash can match against `..` names its folder and its parent.** `pw_dotdot` asks
   bash's own `[[ .. == pattern ]]` of a component with a literal leading dot (a bracket never
   matches a leading dot in bash 3.2: `[.]*` and `?.` match nothing); `pw_alt` judges every reading
   with each such component as itself, `.` or `..` (folded too), in every context. *Deviation:* a word
   with more than three such components is denied unread (3^m readings otherwise). `rg foo .*rc` is
   unchanged.
7. **No disk call inside a stall mount.** `pw_mount` folds the data volume's alias (any letter case,
   repeated) before the stall test, at the top of `pw_physical` and for every step, and a step under a
   mount is taken by name. `R3C_MOUNT_WORDS` gains ten spellings (`…/Data/Volumes/Backup/x/..`,
   `…/Data/net`, `…/Data/home`, `…/Data/Network`, two letter cases, three later globs, a glob entry),
   and `inside_a_mount` folds the alias too; the wrapped `lstat` records no call inside a mount.
8. **A command too long to prep fails closed:** over 64 KB and naming a searcher, it is denied with
   "Command too long to judge"; a 65,600-character `echo` naming none is allowed.
9. **A root spelling right before `{` is a path word** (`~`, `~name`, `$HOME`, `${HOME}`,
   `$SUBFLEET_*`), so its brace expansion is judged; `rg foo src/{a,b}.py` and `rg foo {src,tests}`
   stay allowed.
10. **The wider user and volume family.** Any `~` followed by `$`, `${`, `$(` or a backtick is the
    home (`pw_tilde` drops the user part; r4's three spellings are now this one reading), and the prep
    reads a word that starts `~$(` or `` ~` `` from `~` on, judging the substitution's command on its
    own (a space inside it no longer splits the word). A glob word that climbs out of a `/Volumes`
    entry with `..` is also read from `/` (`pw_voldots` on the glob word), so
    `/Volumes/M*/../Users/tester/.subfleet` is judged like its written form.
11. **The unjudged forms.** `=rg`, `=find` and `=grep` pass the prefilter (a sixth term) and read as
    the searcher in command position, after a wrapper too; a pipe carries what every earlier stage
    writes into a shell given no script (printf's format takes its arguments in order, `%b` decoded;
    any other stage writes its words); a `-c` script that only runs its arguments (`"$@"`, `$@`, `"$1"`,
    `$*`) runs the words after its name operand, and under xargs such a script, or a `-c` with no
    script, runs what xargs reads; what a command substitution in `eval`'s arguments writes, quoted or
    not, is a script; `<&0` and `0<&0` leave rg's stdin as it was. No must-allow case moved, so
    nothing here went to the known limits.

**The hub's decisions 12 and 13.** `test_review_r4_pins_r4s_unpinned_readings` passes. TRUST changes
only `hook_sha256` (no decision changes what the provenance says); steps 0 and 2 of the install list
the installed pair and the new one, and the rollback pair stays `a60d1c51…`/`b3193b97…`.

**Red runs at `cb63c20a1`.** Through the probe (the fixture seam, home `/Users/tester`), 56 of the 68
fast must-deny inputs were allowed and every must-allow case was allowed; the long-word rows took
27-83 s (four at a time, load 32-39), and both 65,600-character forms were allowed. Under the lock
(admitted 12:44:24Z, load 54), `test_r5_*` and the mount test at the red-test commit: 12 failed (the
timing test by the 120 s hook timeout), 2 passed (`test_review_r4_pins_r4s_unpinned_readings` and
`test_r5_scoped_searches_stay_allowed`).

**Profiling the passes** (`prof.sh` in the build's scratch: the prep, the no-path pass and the judge
pass's pass 1, alone, one 60,000-character word beside `rg foo src`, at load 40-60):

| Word | `base()` split only: prep / no-path / pass 1 | Final: prep / no-path / pass 1 |
| --- | --- | --- |
| `x…` | 3.65 / 2.54 / 0.12 s | 0.19 / 0.38 / 0.40 s |
| `"x…"` | 3.97 / 5.91 / 8.61 s | 0.80 / 1.33 / 0.46 s |
| `'x…'` | — | 0.23 / 0.30 / 0.59 s |
| `$'x…'` | 4.06 / 2.99 / 1.23 s | 0.18 / 0.10 / 0.05 s |
| `` `echo x…` `` | — | 0.24 / 0.11 / 0.11 s |
| 60,000 blanks | — | 0.51 / 0.30 / 0.32 s |
| 4,000 × `word` | — | 0.22 / 0.28 / 0.27 s |

**Holds** (one pytest each, under the lock, in the foreground, home `/Users/tester`; the holds
before 14:37Z ran at `8a53286b2`, whose hook differs from the final one only in the text of decision
8's message, which `8484dfa94` changed after h4's first run found `ls` in it):

| Hold | Admitted (slot, load) | Selection | Result, pytest time |
| --- | --- | --- | --- |
| h1a | 14:12:49Z (B, 62) | `test_r5_` but the shells: the nine decision tests, timing, the doc | 11 passed, 177 s |
| h1b3 | 14:23:13Z (A, 31) | r5 shells (after two oracle fixes, below) | 1 passed, 90 s |
| h2 | 14:25:23Z (A, 38) | r1/r2 examples, TRUST, the fence, known-limit pins | 18 passed, 60 s |
| h3 | 14:26:40Z (A, 50) | r3 examples | 25 passed, 111 s |
| h4 | 14:37:24Z (A, 54) | r3b examples and property (final hook) | 15 passed, 412 s |
| h5 | 14:45:03Z (B, 14) | S1, S2, the cwd list, S4 | 4 passed, 2 skipped (S4), 632 s |
| h10b | 15:07:06Z (A, 94) | r4 examples, shells and timing | 12 passed, 1 failed, 356 s |
| h8 | 15:23:56Z (A, 147) | r3c property | 1 passed, 1,152 s |

- **h1b's two failures were the oracle's, not the hook's:** `echo src | xargs -0 bash -c 'rg foo "$1"' sh`
  hands rg `src` plus a newline, which broke REFERENCE_SH's line-by-line read (r5's shell oracle now
  drops a walked path holding a newline: no root name holds one); and `~$LOGNAME`/`~${LOGNAME}`
  were unwalked only because the oracle's environment had no LOGNAME, as review r4 noted (the oracle
  now passes it, as lanes do).
- **h10b's failure is load:** `test_r4_absolute_paths_cost_at_most_twice_the_installed_hook` measured
  the new hook's median at 1.68 s against the installed hook's 0.60 s (2.8×) at load ~95-115. An
  interleaved check right after, three runs each at load 117-146, gave the installed hook 1.08-1.28 s,
  `cb63c20a1`'s 0.98-1.09 s and this head's 0.89-1.52 s. It was not rerun under the lock.
- **Over 10 minutes:** h5's pytest took 632 s and h8's 1,152 s (a single test, which `-k` cannot
  split); both passed.
- **S1 at scale 1:** 777 commands, 185 newly denied, every one labelled; none loosened but r3's two
  labelled file cases. **r3c property:** 0 of 2,400 differ from REFERENCE_SH (decision 6's and
  decision 10's readings added to it, written from the brief), and 0 of 298 through the whole hook.

**Timing rows** (h1a, one run each, the new hook then the installed one, `hooks["old"]`):

| Row | Load | This head | Installed |
| --- | --- | --- | --- |
| 60,000 `x` beside `rg --files ~/.subfleet` (denied) | 70.7 | 2.05 s | 1.13 s |
| 60,000 `x` beside `rg foo src` | 70.7 | 1.50 s | 1.40 s |
| 65,000 `x`, scoped | 70.7 | 0.99 s | 0.39 s |
| 30,000 `x` beside a crawl (denied) | 70.2 | 0.50 s | 0.39 s |
| `'60,000 x'`, scoped | 70.2 | 0.64 s | 1.39 s |
| `"60,000 x"`, scoped | 68.5 | 1.81 s | 0.97 s |
| `` `echo 60,000 x` ``, scoped | 68.5 | 1.00 s | 0.68 s |
| 4,000 words in a commit message, scoped | 68.5 | 0.86 s | 1.69 s |
| a 70,000-character word beside a crawl (denied, too long) | 63.7 | 1.56 s | 3.08 s |

At `cb63c20a1` the first two rows took 82.7 s and 82.2 s (four at a time, load 32-39). r4's hostile
rows in h10b (load ~95): 1,000 `{` 0.79 s, `/[`×1,000 0.85 s, 10,000 commas 0.38 s, 2,040
components 0.56 s; the cap edge (reported, not bounded) 2.08 s and 7.49 s.

**The real shells.** r5's 104 command runs: the hook denies 70; zsh walked a root in 62 and bash in
62 (both in 55, zsh only in 7, bash only in 7), neither in 35, and the one listed exception
(`/Volumes/M*/../Users/tester/.subfleet`: the fixture's `Macintosh HD` is a folder, not the boot
volume's link, so its `..` is judged by name) is denied by design. r4's 119 runs in h10b agree too
(denied 80; zsh 78, bash 77).

**Counts per file:** `test_guard_subfleet_root.py` has 107 items (r4v's 94, review r4's one, r5's 12),
of which the holds above ran 90 (87 passed, 2 skipped, 1 failed on load); review r4's test passed in
h1b's first run and in the mutant control, and the rest are under "Unrun".

**Mutants** (`tools/guard_subfleet_root_mutations.py`, one `git archive` of HEAD each, the named test
and the fence). The lock admitted nothing in 30 minutes (load 108-120, slot B closed), so, as the lane
rules allow, the build stopped its own queued waiter and ran these 16 directly, 16:14-16:52Z at load
74-120. The control passed all 13 named tests and the fence at the final hook; every mutant was
killed by its named test, and the fence passed for each.

| Decision | Mutant | Killing test |
| --- | --- | --- |
| 1 | `r5-base-quadratic` (`base()` back to `sub(/.*\//, …)`) | `test_r5_a_long_word_without_a_slash_costs_what_the_installed_hook_does` |
| 2 | `r5-heredoc-unread` | `test_r5_heredoc_bodies_are_data` |
| 3 | `r5-bound-hides-producer` (the bounded command back to `:`) | `test_r5_a_depth_bound_covers_only_its_own_walk` |
| 4 | `r5-doc-duplicated` (a section's heading twice) | `test_r5_the_overlay_doc_holds_one_copy_of_its_body` |
| 5 | `r5-backtick-space-dropped`, `r5-dq-substitution-unread` | `test_r5_a_closing_backtick_ends_a_root` |
| 6 | `r5-dotdot-parent-dropped` | `test_r5_a_component_bash_can_match_against_dotdot_names_its_parent` |
| 7 | `r5-alias-unfolded` | `test_r3c_nothing_inside_a_mount_is_read` |
| 8 | `r5-too-long-allowed` (r4's skip) | `test_r5_a_command_too_long_to_prep_fails_closed` |
| 9 | `r5-brace-root-unread` | `test_r5_a_root_spelling_before_a_brace_is_a_path_word` |
| 10 | `r5-voldots-glob-dropped` | `test_r5_the_wider_user_and_volume_family` |
| 11 | `r5-pipe-stages-dropped` (only the stage right before the shell) | `test_r5_the_unjudged_forms_are_judged` |
| review r4 | `rv4-otherfd-is-stdin`, `rv4-grep-meta-walks`, `rv4-backtick-space-dropped`, `rv4-function-keyword-dropped` | `test_review_r4_pins_r4s_unpinned_readings` |

Two of r4's mutants were retargeted because r5 changed the code they edit: `r3c-physical-stall-guard-dropped`
now drops both stall guards of `pw_physical` (decision 7's per-step guard alone would make the old edit
equivalent), and `r4-tilde-user-dropped` drops the one line decision 10 made of r4's three. The driver
now holds 65 mutants; the other 49 were not run this round.

**Unrun at this head:** S3 ×2, S5 and the r3 shells; the r3 property; S1-r3; the r3c examples, shells
and folded code points (the r3c mount test ran in the mutant control); the overlay file
(`test_guard_overlay.py`) and the three guards; `test_guard_trust.py`; S4's two items (they need the
operator's Claude hook); and `tests/frontend/test_core_live.py`, which cannot run on a lane.

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
  unresolved; a `?` in a glob matches one byte of a non-ASCII name; and a script under
  `bash -O globstar -c` is read as zsh (its `**/` walks), which matches. (r4 took a `bash -c` script
  that spans lines: the prep reads it whole and adds it in its context.)
- **r3c's limits.** A glob inside a mount entry is judged by name (root names plus any other
  name) and never listed, so it over-approximates (`/Volumes/Ext/Users/zz*` is denied though
  nothing matches) and does not see a link there (`"/Volumes/Macintosh HD/Users/me/"link-to-*`);
  a link among the matches of a `<glob>/..` is followed since r4 only where the folder is listed;
  a bare `*` between two plain names under a folder that is listed is judged by its matches
  (`rg -l foo ~/*/src` passes; under a folder that is not, it is no match: `~/PolicyEngine/*/src`
  passes, as snapshot-5 has it); an unquoted depth-bound spelling used as a value still exempts
  its command (`rg -e --max-depth ~`); a nested script is judged in its shell's reading but
  without the outer command's cd; and a top-level word is read the zsh way, plus (r5 decision 6) bash's
  reading of a component bash 3.2 can match against `..`; the other bash readings known here
  (`(a|b)` refused, `/[^/]*` unmatched, `**` and `***` read as `*`) walk no more than zsh's.
- **r4's limits.** Shared with snapshot-5 and still open: zsh qualifiers and glob modifiers are not
  judged (`*(/)`, and `:h`: `rg foo ~/work/repo(:h:h)` walks the home and passes), nor are `<1-9>`
  and extendedglob forms; a glob in a folder that cannot hold a root is not listed, so a link inside
  it is not followed (`rg x src/link-u*`), also before a `..` (`~/work/r*/jl2/../..` with `jl2` a
  link into a job folder is read by name); a link inside a mount entry, and a link in a listed
  folder that points into a mount that may stall (still resolved); paths fed by xargs
  (`… | xargs -n1 find`); a script read from a file or a process substitution
  (`bash <(echo 'rg …')`, `bash script.sh`); and ag, `git grep --no-index`, tree, du and `ls -R`.
  This overlay's own: `~+` is not judged (the cwd, as a relative path) and `~-` is read as a user
  named `-`; the fold goes by name, so it over-approximates where the folded component does not
  exist (`…/link-to-hom*/lf.log/..` is denied with no `lf.log` there), as decision 1 has it; a brace
  group read as `*` that holds a `/` or a `..` component fails closed (`rg foo src/{a,b/c}{1..9}`
  in a worktree is denied); and the cap's scanned edge (64 `{` or `[` in just under 4 KB) costs
  seconds at load ~100. The differences from snapshot-5 in either direction are listed in
  `docs/private-guard-overlay.md`, "Known differences that remain after r5".
- **r5's limits.** Moved here rather than built, each measured on this head:
  - a heredoc body read by something other than a shell given no script gets no prep reading:
    `source /dev/stdin <<'EOF'⏎\rg --files ~/.subfleet⏎EOF` passes (`. /dev/stdin` and a
    `while read` loop that runs its lines are the same form; the body's text is still read by the
    text rules, so a plain `rg --files ~/.subfleet` in it is denied);
  - a tilde-prefix that holds an expansion after a name (`~x$USER/.subfleet`) is no path word, as
    before; decision 10 reads only `~` followed straight by `$` or a backtick;
  - what a command substitution in `eval`'s arguments writes is modeled for `echo`, `print`, `printf`
    and `cat`, and any other command contributes its words only;
  - a hostile word made of characters the prep reads one at a time (thousands of `$`, `#` or
    backslash pairs) still costs a pass per character; the brief's timing rows (slash-free `x` words,
    quoted, in backticks, a 4,000-word message) are under 3 s.
  The over-approximations r5 adds: a heredoc body no shell runs is still read by the text rules
  (`cat > notes.md <<'EOF'⏎find / -name x⏎EOF` is denied, as by the installed hook); a command over
  64 KB naming a searcher anywhere, prose included, is denied unread; a word with more than three
  components bash can match against `..` is denied unread; a pipe into a shell given no script reads
  every earlier stage's words as script lines; and, with xargs running a searcher, a bounded find's
  root counts like any other word of the command.
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
   shasum -a 256 private/guard/never-rules-hook.sh private/guard/TRUST   # the reviewed pair (r5)
   # 0b00cc6182af3cb19abdc78b74492b00c5a1b22c53e224c2ce65481e6744789b  private/guard/never-rules-hook.sh
   # 09ed48d336d868d32ac6612035fcad272f27ff5ac81c9616cfa11841d0df17a8  private/guard/TRUST
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
   shasum -a 256 ~/.subfleet/guard/never-rules-hook.sh ~/.subfleet/guard/TRUST   # 0b00cc61…789b, 09ed48d3…17a8
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
