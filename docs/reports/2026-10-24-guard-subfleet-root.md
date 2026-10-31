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
zsh's `=cmd`, pipes into a shell, `"$@"` scripts, `eval $(…)` and `<&0`. Rounds r6 and r7 (2026-10-28) close the
findings of the hub's reviews r5 and r6, rounds r8 and r9 (2026-10-29) those of reviews r7 and r8, round r10 (2026-10-30) those of review r9, and rounds r11 and r12 (2026-10-30) those of reviews r10 and r11 (below). Nothing here touches the live guard; the hub installs it with the steps below.

## What changed

**The hook** (`private/guard/never-rules-hook.sh`, SHA-256 `e5b387c4…62d7` after r12; r11's was `7bba4822…e102`, r10's `c42f05ef…865f`, r9's `9eb0db09…e7e2`, r8's `740728e3…20e9`, r7's `89b0efe1…aa5d`, r6's `6671496b…aea3`, r5's `0b00cc61…789b`, r4v's `1d8bc1e5…e58c`, r4's `4b382c29…79ec`, r3c's `7a5ad79d…1a1a`, r3b's
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

**TRUST** (`c91e21b4…8fd7` after r12; r11's was `4e270e5b…14f8`, r10's `54cdded8…0432`, r9's `339ca3f0…a2c4`, r8's `f5e5268e…c073`, r7's `b749db5a…e9eb`, r6's `955bc7fa…3d2f`, r5's `09ed48d3…17a8`, r4v's `3c7c8af8…f9bb`, r4's `807f0e8b…7fb4`, r3c's `68936faf…c91a`, r3b's `0d5dac0f…54d8`, r3's `12b5f31a…58a2`, r2's `aa09f53d…a2f0`). It is built on the installed TRUST of 2026-09-29 (`b3193b97…`, the
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

## r6: review r5's findings closed (2026-10-28)

The hub's review r5 (REQUEST_CHANGES at `e02a22fb6`) found two P2s and seven P3s that needed code (an
eighth, two unpinned r5 readings, it pinned itself in `9487a0f59`, `test_review_r5_pins_r5s_unpinned_readings`,
which stays and passes). r6 builds on `809346708` (that test plus the hub's merge of `release/217`). Every pin is
review r5's input or the brief's, judged through the whole hook in the fixture (`test_r6_*`), and each command
pin and control also runs in real `/bin/zsh -f` and `/bin/bash` 3.2 over the fixture with stub searchers and
the fixture's HOME (`test_r6_command_pins_agree_with_real_shells`; r6's find stub runs the real `/usr/bin/find`
only when every start path, and the cwd, is a folder inside the fixture, and `git`, `python3` and `gh` are
no-ops).

**Decisions as built.**

1. **An unquoted heredoc body's substitutions are judged.** When the delimiter word holds no quote and no
   backslash (`<<EOF`, `<<-EOF`), `prep()` runs `spans()` over the body (`HDB`), so each `$(…)` and backtick
   span in it that names a searcher is judged as a command; the rest of the body stays data. `<<'EOF'`,
   `<<"EOF"`, `<<E"O"F` and `<<\EOF` keep the whole body data. (`HDW` is saved across the nested reading,
   which reuses the heredoc globals.)
2. **xargs into a shell whose `-c` script names a searcher.** `simple()` sets `xsh` when xargs runs `sh`,
   `bash`, `zsh`, `dash` (or `ksh`, shstep's set) with `-c` and the script names a searcher as `pw_named` reads
   it (`pwn()`, its four tests in awk); the prep then ends with `\003X\003`, and the xargs rule judges the
   whole command (`pw_xsh`): a root anywhere, a bounded producer's roots included (`bound()` keeps them), and a
   root right before an xargs, quoted or not. The rule's edit is outside the path-word blocks (`R6_TEXT`).
3. **Any producer's heredoc or here-string piped into a shell is its script:** `simple()`'s generic producer
   branch passes on its `hstr` and `hd` as the `cat` branch does.
4. **An arithmetic `<<` starts no heredoc.** The prep keeps the open `((` it has read (by its own `(` and `)`
   separators) and, only at a `<<` it would read as a heredoc, `arith()` asks whether one of them, innermost
   first, is arithmetic: its inner `(` closing right before its outer one, as the shells read them. A `<<`
   whose body never ends disables only itself (r5's `hf` is gone; `hz` and `HZ` remember a line end or a
   delimiter already not found). *Deviation from a literal "skip `$((…))`":* skipping the text whole would
   hide a `$(…)` inside an arithmetic expression from the prep, so only the `<<` is reinterpreted, lazily;
   an eager look at every `((` cost about 10 s more than r5 on 2,000 `(`.
5. **A `$(` inside double quotes ends where a heredoc-aware `parend` says.** `parend()` reads heredocs (the
   lines after the line holding a `<<`, to its delimiter line; a body that never ends disables only its
   `<<`), and `dqhend()` is `dqend()` with each `$(` inside the quotes run to `parend()`'s `)`; the prep's
   double quotes, `eol()` and `simple()`'s tokenizer use it.
6. **zsh's `=cmd` after a backtick:** the prefilter's sixth term and `pw_named`'s `=cmd` test take a
   backtick before the `=` (the prefilter edit is outside the path-word blocks: `R6_TEXT`).
7. **The markdown code span over-block is intended.** r5's behaviour is kept and pinned as denied
   (`test_r6_a_markdown_code_span_in_a_quoted_body_is_an_intended_over_block`). *Counterexample to review r5's
   rescoping edit:* without the backticks the span is still denied, and so it is with the path quoted, since
   the text rules read `rg --files ~/.subfleet` in the body (the installed hook, which does not know
   `~/.subfleet`, allows all three). What rescopes it: naming the root outside the searcher's span
   (`` Run `rg --files` in `~/.subfleet` ``), or a scoped path. Both pinned.
8. **Hostile words fail closed fast.** A command that names a searcher and holds more than 4,096 of any one
   of `$`, `#` or `\` is denied before the prep, as too complex to judge (one awk pass counts the three).
9. **Process substitutions and loops.** What a `<(…)` writes (`subout()`, the pipe model of r5's decision 11)
   goes to the command it is a word of: the script of a shell given no script (`bash <(…)`,
   `bash < <(…)`) or of `source`/`.` (run in this shell), and what `cat` or any producer passes on (*not true at
   r6:* the prep splits the command at the `(` of its `<(…)`, so no pipe carried that pass-on; review r6's P3-4, and
   r7's decision 3 makes it fire). A `for`
   loop variable stands for its words, and a variable a piped `while read` reads for the roots of what the
   pipe carries (find: its roots, `.` when it names none; any other command: its words), as `{}` does for
   `-exec`: a searcher operand that is only `$V`, `"$V"`, `${V}` or `"${V}"` is read as them. Any other
   loop shape stays unjudged. (This awk allows 50 arguments and locals per call, so `simple()` keeps the
   new state in `st["ps"]` and `st["pr"]` and reuses `w`.)
10. **The 300-path cost test measures child CPU** (`resource.getrusage(RUSAGE_CHILDREN)` deltas around each
    run, interleaved, the same 2× bound on the median); it reports the wall times beside.

**The step budget (a deviation shared by decisions 4, 5 and 9).** A dense run of `$(` or `((` made every
`parend()` read walk the rest of the command; r5's own hook already ran past 90 s on 2,000 `$(` inside
double quotes (23.8 s on 500), and Codex's hook timeout is 60 s. Every `parend()` call now shares one budget
of 20,000 steps, and a special character is taken without the 512-character window match (which costs its
whole window when it fails); past the budget the prep stops, its last line is `\003C\003`, and the hook
denies the command as too complex to judge right after the prep (the no-path pass fails closed on it too).
`test_r6_dense_substitutions_fail_closed_fast` pins three dense forms.

**The hub's decisions 11 to 13.** "Known differences" in `docs/private-guard-overlay.md` lists every form of
review r5's check 8, each marked fixed at this head with its decision or left open with its reason, sorted
into overlay-only, shared or unchecked against snapshot-5, and stricter. `test_review_r4_pins_r4s_unpinned_readings`
and `test_review_r5_pins_r5s_unpinned_readings` stay and pass. TRUST changes only `hook_sha256` (no decision
changes what the provenance says); step 0 of the install lists the installed pair and the new one, and the
rollback pair stays `a60d1c51…`/`b3193b97…`.

**Red runs at `809346708`** (the probe: the fixture seam, home `/Users/tester`, four at a time, load ~217): of
the 90 r6 pins, 35 must-deny inputs were allowed and one control I first wrote (the span without backticks)
was denied, which is decision 7's counterexample above; the three hostile rows each ran past 30 s and were
stopped.

**Holds** (one pytest each, home `/Users/tester`, `-k` selections of `test_guard_subfleet_root.py` unless named):

| Hold | Admitted (slot, load) | Selection | Result, pytest time |
| --- | --- | --- | --- |
| H1 | 07:22:25Z (B, 83) | `test_r6_*`, review r5's test | 12 passed, 1 failed (wall time on one dense row, below), 372 s |
| H4 | 07:59:39Z (A, 119) | r4's decision tests, hostile rows and 300-path cost; review r4's test; the dense rows again | 13 passed, 337 s |
| H2 | 08:07:06Z (B, 63) | r5's decision tests and timing | 10 passed, 1 failed (r5's timing row, wall time, below), 199 s |
| H3 | 08:26:08Z (B, 84) | r5's shells; r5's timing rerun once | 2 passed, 206 s |
| HA | 08:43:27Z (B, 64) | `test_guard_overlay.py`, `test_guard_trust.py` and the three guards | 189 passed, 1 failed (the overlay file's stale pin, fixed in `9deacad70`), 356 s |
| HB | outside the lock, 09:12:57Z (load 181) | r1/r2, TRUST, the fence, env spellings, known limits; the overlay pin | 16 passed, 3 failed (two the hook's mode, one decision 9's closed limit; both fixed, below), 444 s |

- **H1's failure was wall time:** `test_r6_dense_substitutions_fail_closed_fast`'s third row took 3.5 s against
  the installed hook's 1.5 s at load 110. Interleaved right after, the three hooks took 0.19 (r6), 0.21 (r5)
  and 0.15 s (installed) of child CPU on it, and wall time for the same CPU swung from 1.3 to 12.5 s. The test is
  this round's own (the budget is a deviation), so `8b35ed657` bounds it as decision 10 bounds the 300-path test:
  under 3 s of wall time or within twice the installed hook's child CPU. H4 ran it again: all three rows passed
  (CPU 0.19/0.17, 0.16/0.17 and 0.17/0.16 s).
- **H2's failure was load:** r5's timing row `'60,000 x', scoped` took 3.38 s against 1.09 s at load 94.
  Interleaved, r6 and r5 took 0.33 and 0.31-0.36 s of CPU on it and the installed hook 0.23-0.28 s, and r5's own
  hook once took 13.9 s of wall time. Rerun once in H3 at load 78, every row passed (below). The bound was not
  loosened.
- **HB ran outside the lock:** its waiter sat 34 minutes unadmitted (both slots held, load over 120), so, as the
  lane rules allow, I stopped my own waiter and ran it directly. Its failures: this job's checkout wrote the hook
  644 and `92f19a5fd` recorded it, so `load_guard` refused it (`36eda0c38` restores 100755; no mode differs from
  `809346708` now); and r2's quoted for-loop limit is closed by decision 9 (`1f3ee53c5` moves it to
  `R6_CLOSED_LIMITS`).

**Timing rows** (one run each, the new hook then the installed one, `hooks["old"]`):

| Row | Load | This head | Installed |
| --- | --- | --- | --- |
| 60,000 `x` beside `rg --files ~/.subfleet` (denied) | 78.7 | 1.20 s | 0.74 s |
| 60,000 `x` beside `rg foo src` | 78.7 | 1.19 s | 0.84 s |
| 65,000 `x`, scoped | 78.7 | 0.71 s | 0.39 s |
| 30,000 `x` beside a crawl (denied) | 78.7 | 0.50 s | 0.38 s |
| `'60,000 x'`, scoped | 78.7 | 0.54 s | 0.30 s |
| `"60,000 x"`, scoped | 77.5 | 0.48 s | 0.34 s |
| `` `echo 60,000 x` ``, scoped | 77.5 | 0.69 s | 0.36 s |
| 4,000 words in a commit message, scoped | 77.5 | 0.95 s | 0.44 s |
| a 70,000-character word beside a crawl (denied, too long) | 77.5 | 0.62 s | 1.00 s |
| 60,000 `$` beside a crawl (denied, too complex) | 118.5 | 2.78 s | 1.25 s |
| 60,000 `#` beside a crawl (denied, too complex) | 116.6 | 3.98 s | 4.69 s |
| 30,000 backslash pairs beside a crawl (denied, too complex) | 110.8 | 2.28 s | 1.63 s |

The first nine are H3's rerun, the last three H1's. **Decision 10's 300-path test** (H4, load 107.5): median
child CPU 0.302 s against the installed hook's 0.195 s, **1.55×**; wall in the same run 2.02 s against 0.57 s
(3.5×), the load the new measure steps around. r4's hostile rows in H4: 1,000 `{` 0.62 s, `/[`×1,000 1.40 s,
10,000 commas 1.21 s, 2,040 components 1.12 s; the cap edge (reported, not bounded) 10.96 s and 24.92 s at load
~107 (review r5's t1 measured 9.4 s for the second at load ~100; r6 does not touch the path-word pass that judges
it, but this round could not separate its cost from the load).

**The real shells.** r6's 68 command runs (H1): the hook denies 37; zsh walked a root in 33 and bash in 27 (both in
27), neither in 35, and every run agrees but the listed exceptions (decision 7's three spans and decision 4's
unterminated heredoc, denied by design). r5's 104 runs (H3) agree too: denied 70; zsh 62, bash 62, both 55,
neither 35.

**Must-allow cases.** `test_r6_scoped_searches_stay_allowed` holds 198 (176 distinct): r5's list (130), r5's
allowed forms, the print-and-exit forms from the home, `/` and the scoped cwd, and 17 lane commands (an 18th, the
markdown span, is decision 7's denial); each decision's test holds 31 allowed forms more, and decision 8's 5
controls. None comes near decision 8's cap: the most of any one of `$`, `#` or `\` in a must-allow case is 2
(``x=$(cat <<'EOF' …``, the PR-body heredoc's `##`, `` git commit -m "use \`rg\` / here" ``).

**Mutants** (`tools/guard_subfleet_root_mutations.py`, one `git archive` of HEAD each, the named test and the
fence, under the lock): M1 admitted 09:14:50Z (A, load 192), M2b 10:02:10Z (A, load 80). Each control passed every
named test and the fence.

| Decision | Mutant | Killing test |
| --- | --- | --- |
| 1 | `r6-body-unread` | `test_r6_an_unquoted_heredoc_bodys_substitutions_are_judged` |
| 2 | `r6-xargs-shell-unread` | `test_r6_xargs_into_a_shell_whose_script_names_a_searcher` |
| 3 | `r6-filter-heredoc-dropped` | `test_r6_any_producers_heredoc_piped_into_a_shell_is_its_script` |
| 4 | `r6-arith-unread`; `r6-unterminated-disables-rest` (survived M1, killed in M2b after `cba2d56e7` pinned the form across lines) | `test_r6_an_arithmetic_shift_starts_no_heredoc` |
| 5 | `r6-dq-parend-plain` | `test_r6_a_substitution_in_double_quotes_reads_its_heredoc` |
| 6 | `r6-equals-backtick-dropped` | `test_r6_zsh_equals_cmd_after_a_backtick_is_read` |
| 7 | `r6-span-backtick-dropped` (root_end is a fenced line, so the fence fails too) | `test_r6_a_markdown_code_span_in_a_quoted_body_is_an_intended_over_block` |
| 8 | `r6-cap-dropped` | `test_r6_hostile_words_fail_closed_fast` |
| 9 | `r6-psub-unread`, `r6-loop-words-dropped` | `test_r6_process_substitutions_and_loops_are_judged` |
| budget | `r6-budget-dropped` | `test_r6_dense_substitutions_fail_closed_fast` |
| review r5 | `rv5-execdir-dropped`, `rv5-wrapper-equals-dropped` | `test_review_r5_pins_r5s_unpinned_readings` |
| r5 (retargeted) | `r5-heredoc-unread`, `r5-bound-hides-producer` | `test_r5_heredoc_bodies_are_data`, `test_r5_a_depth_bound_covers_only_its_own_walk` |

**Unrun at this head:**
- r5's other 14 mutants (`r5-base-quadratic`, `r5-backtick-space-dropped`, `r5-dq-substitution-unread`,
  `r5-pipe-stages-dropped`, `r5-too-long-allowed`, `r5-brace-root-unread`, `r5-voldots-glob-dropped`,
  `r5-dotdot-parent-dropped`, `r5-alias-unfolded`, `r5-doc-duplicated` and review r4's four): their batch could
  not finish inside the job's wall once the lock had held it 16 minutes. Each one's edit still matches the hook
  exactly once, and each named test passed at this head (H2-H4, HB, HC).
- Holds: the r3, r3b and r3c examples, shells, mount and folded-code-point tests; the three properties (r3, r3b,
  r3c); S1, S1-r3, S2, S3 ×2, S5 and the cwd list. The lock admitted my holds 7 times in 2 h 45 m at load 63-192
  (two more ran outside it after 34 and 40 minutes queued); these are what I would run first.
- Not runnable here: S4's two items (they need the operator's Claude hook) and `tests/frontend/test_core_live.py`.

## r7: review r6's findings closed (2026-10-28)

The hub's review r6 (REQUEST_CHANGES at `2bd5b3199`, all P3) found seven P3s; its own test-only commit
`016a256be` (`test_review_r6_pins_r6s_unpinned_readings`: 26 denied forms, 8 allowed forms and the step budget's
edge) stays and passes. r7 builds on `016a256be`. Every pin is review r6's input or the brief's, judged through the
whole hook in the fixture (`test_r7_*`), and each command pin and control also runs in real `/bin/zsh -f` and
`/bin/bash` 3.2 over the fixture with r6's stubs and the fixture's HOME (`test_r7_command_pins_agree_with_real_shells`).

**Decisions as built.**

1. **`parend` reads arithmetic** (review r6's P3-1, a loosening against r5, and P3-2). At a `((`, `arend()` reads
   the span in `parend`'s new arithmetic mode (no heredocs; parentheses counted; quotes, `$(…)` and backticks
   skipped as they are elsewhere) and calls it arithmetic when its inner `(` closes right before its outer one, as
   bash and zsh decide between `$((…))` and `$( (…) )`. An arithmetic span is passed over whole, so a `<<` in it is
   a shift and the span may cross lines; a `$(` in it is read in command mode, its heredocs included. A `$[…]` is
   read the same way to its `]`. The prep's `arith()` asks `arend()` (r6's two `parend` calls read the span as
   commands, so a multi-line span never found its `))`), and a `<<` inside an open `$[…]` (`abe`) is a shift too.
   `let '…'` is a quoted word, which neither `parend` nor the prep reads for heredocs; it is pinned.
2. **Heredoc body lines count against the step budget** (P3-3). `parend` reads a body a line at a time: each line's
   end is found 512 bytes at a time (`lnend`, each window past the first a step) and kept in `LE` for the next
   delimiter's read, so no read copies the rest of the command, and each line read is a step of the shared
   20,000-step budget. The registration's pre-check (`index(substr(t, j), HDD)`, another copy of the rest per
   delimiter) is gone: the scan decides, and `PF` still keeps a delimiter not found. The edge of review r6's
   `$(:)` shape holds no body line, so it is unchanged and still exact (19,998 steps read, 20,001 too complex); the
   body-line edges are pinned beside it (`R7_STEP_EDGE`: inside `"$(…)"` the prep reads a body three times, 18 steps
   and 3 a line, so 6,660 lines are read and 6,661 are too complex; at the top of the command once, 1 and 1 a line,
   so 19,999 are read and 20,000 are too complex).
   - *Deviation, an addition:* the prep's own `hdbody()` counts its lines too. At the top of the command, 300
     delimiters that are never found each read every later line (r6's decision 4 made an unterminated `<<` disable
     only itself): r6 took 4.93 s of CPU (35.9 s of wall at load 64) on `rg foo src; cat <<N0 … <<N299` + 20,000
     lines, against r5's 1.55 s and the installed hook's 0.55 s. It is now denied as too complex in 0.36 s.
   - *Deviation in how:* the brief's "compute line starts once, as `prep` does with `LN`/`LP`" is built as line
     ends found once per `parend` call, lazily, from bounded windows, not as a `split()` of the rest. `parend` runs
     once per `$(` and often on a substring (a segment, a double-quoted string), so a split per call would cost
     the rest of the command per `$(`, which a dense run of `$(cat <<A⏎A⏎)` would multiply; the windows cost each
     line read at most its own length. Both meet the decision's intent (no copy of the rest, each line a step);
     this is reasoning, not a measured counterexample: the split form was not built.
3. **A filter passes its `<(…)` down a pipe** (P3-4), the brief's first choice. The prep splits a command at the
   `(` of its `<(…)`, so r6 handed the substitution only to the part of the command before the `(` and no pipe
   carried it. Now, at that split, a command that passes on what its process substitution writes (`cat`, `tee`,
   `sort`, any filter: `simple()` reports it as `lastps`, from `st["lp"]`) leaves its production, with what came
   into it, in `oc`; each later `<(…)` of the same command adds to it, and the pipe after the command's words
   (after the substitution's `)`, `oe`) carries it into the next stage. The pass-on lines of r6 (`:2040`, `:2048`)
   are what `lastps` reads, so they are live now and review r6's `rv6-psub-passon-dropped` is killed. *Review r6
   names three P3-4 inputs* ("all four hooks" in it are the hooks compared); r7 pins them and a fourth of its own,
   `sort <(…) | bash`, with a second `<(…)`, an earlier stage, a later filter and a nested `<(…)`.
4. **"Known differences" names three more forms**, each with its reason and pinned open
   (`test_r7_known_differences_name_the_open_forms`: allowed here, walked by a real shell, named by the doc): zsh's
   `=(…)` (a temporary file name; no script reading follows a file name), `while read d; …; done < <(find …)` (the
   `read` comes before the redirection that feeds it, after `done`), and nested loops (the inner `for` list is
   taken as written). Decision 3's hand-off does not catch the fed loop: its `<(…)` belongs to `done`.
5. **A loop variable reassigned after its loop** (P3-5), built in `LVAR` with two small counters rather than a new
   map: the prep sets `vtop` before each simple command (the top of the command: no subshell, substitution or
   backtick, no case pattern's `)` seen; after `;`, a newline, `&` or nothing, before `;`, a newline, `&&` or `||`),
   and `simple()` counts the compound commands open at the top (`ld`: `for`, `select`, `case`, `while`, `until`,
   `if`, `{` against `done`, `esac`, `fi`, `}`). A command made only of assignments, with no keyword before it,
   `vtop` set and `ld` zero, ends a loop variable's reading: a literal value (no `$`, no backtick) is read in its
   place, so `d=src` passes and `d=~/.subfleet` is denied; any other assignment leaves the loop's words. The
   brief's three pins hold. The over-blocks left (an assignment in an `if` or a brace group or a nested script,
   after a case pattern without its leading `(`, or of a value holding `$`) are listed under "stricter" and pinned
   (`R7_LOOPVAR_STRICTER`, denied, walked by neither shell). `simple()` was at 49 of this awk's 50 arguments and
   locals, so the new state is in `st` (`vt`, `ka`, `lp`) and `op` is reused.
6. **`test_r4_hostile_words_fail_closed_fast` measures child CPU**, best of three, the same 2.0 s, with wall time,
   the cap edge's CPU and the load reported.
7. **TRUST and docs.** TRUST changes only `hook_sha256`. The overlay doc gains r7's paragraph, its "Known
   differences" read after r7 (arithmetic `<<` and the filter's `<(…)` marked fixed by decisions 1 and 3, the body-line
   counts and decision 5's over-blocks under stricter). In r6's section above, decision 9's sentence on `cat` and
   other producers was false at r6 (review r6's P3-4); it is true from r7.

**Nothing is looser than r6 but decision 5's narrowing.** Over S1's generated corpus (528 pairs), the fixed corpus,
every r4 to r7 command list in the test and 48 probes of mine (1,108 pairs), this head against r6: 15 newly denied
(all r7 pins; r8 recounts 14 over r7's pin lists, 16 with the two body-line edges, see r8's decision 7) and 5 newly
allowed, each one of decision 5's `R7_LOOPVAR_ALLOWED`, which neither shell walks.

**Red runs at `016a256be`** (the fixture seam, home `/Users/tester`, load 64-79). The probe over the brief's pins: the
three decision 1 inputs, the four decision 3 inputs and decision 5's first pin were allowed (8 of 14 pins; the controls
were allowed), and review r6's 300-delimiter row was denied by reading (not as too complex) in 1.15 s of CPU. Then the
new tests, called directly against `016a256be`'s hook with this round's test file: `test_r7_an_arithmetic_shift_crosses_lines`,
`…_heredoc_bodies_count_against_the_step_budget`, `…_a_filter_passes_on_its_process_substitution`,
`…_known_differences_name_the_open_forms`, `…_a_loop_variable_reassigned_after_its_loop_is_read_as_assigned` and
`test_r7_scoped_searches_stay_allowed` (decision 5's first pin) each failed.

**Holds** (one pytest each, `-k` selections of `test_guard_subfleet_root.py` unless named, home `/Users/tester`; a
bounded copy of `suite_hold.sh`, the same locks and log plus a wait deadline, kept each call under the tool's cap):

| Hold | Admitted (slot, load) | Selection | Result, pytest time |
| --- | --- | --- | --- |
| H1 | 19:08:01Z (A, 103) | `test_r7_*`, r6's decision tests, review r6's test | 17 passed, 42 s |
| H2 | 19:09:08Z (A, 106) | r6's shells, hostile and dense rows; r5's timing; r4's hostile rows and 300-path cost | 6 passed, 57 s |
| H3 | 19:11:10Z (B, 61) | r4's and r5's decision tests, reviews r4 and r5 | 21 passed, 22 s |
| H4 | 19:12:53Z (B, 57) | r4's and r5's shells, r4's words | 3 passed, 108 s |
| H5 | 19:15:48Z (B, 17) | r1/r2, TRUST, the fence, env spellings, known limits | 18 passed, 236 s |
| H6 | about 19:22Z (B, queued at 67) | S1 | 1 passed, 771 s |
| HA | 19:39:30Z (A, 53) | `test_guard_overlay.py`, `test_guard_trust.py` | 127 passed, 312 s |
| HB | 19:52:23Z (B, 109) | the three guards | 63 passed, 159 s |

S1 at this head: 779 commands (531 generated, 253 fixed, 308 env twins), the old hook denies 119 and this one 313;
the 194 newly denied are all r3's intended moves, none unexplained.

**Timing rows** (one run each, child CPU in seconds, at load 60-69; the first three are decision 2's):

| Row | This head | r6 | r5 | Installed | Load |
| --- | --- | --- | --- | --- | --- |
| 300 delimiters, 20,000 lines (denied; too complex here) | 0.46 | 1.09 | 0.78 | 0.54 (allowed) | 66-69 |
| 40 delimiters, 8,000 lines (denied; too complex here) | 0.27 | 0.47 | 0.39 | 0.31 (allowed) | 66 |
| 300 missing delimiters, 20,000 lines, top level (denied; too complex here) | 0.36 | 4.93 | 1.55 | 0.55 (allowed) | 63-66 |
| 60,000 `x` beside a crawl | 0.35 | | 0.41 | 0.28 | 63 |
| 60,000 `x` beside `rg foo src` | 0.37 | | 0.34 | 0.30 | 63-64 |
| 65,000 `x`, scoped | 0.36 | | 0.30 | 0.26 | 64 |
| 30,000 `x` beside a crawl | 0.30 | | 0.28 | 0.19 | 64 |
| `'60,000 x'`, scoped | 0.35 | | 0.30 | 0.25 | 64 |
| `"60,000 x"`, scoped | 0.30 | | 0.29 | 0.25 | 62 |
| `` `echo 60,000 x` ``, scoped | 0.35 | | 0.33 | 0.28 | 62 |
| 4,000 words in a commit message, scoped | 0.31 | | 0.25 | 0.25 | 62 |
| a 70,000-character word beside a crawl (too long) | 0.19 | | 0.17 | 0.34 | 61-62 |
| 60,000 `$` / 60,000 `#` / 30,000 backslash pairs (too complex) | 0.21 / 0.24 / 0.24 | | not run | 0.30 / 0.30 / 0.31 | 61 |
| 2,000 `$(` in `"…"` / 2,000 `(` before `<<` / 1,000 `((` (too complex) | 0.18 / 0.20 / 0.19 | | not run | 0.18 / 0.17 / 0.17 | 59-61 |
| r4's 1,000 `{` / `/[`×1,000 / 10,000 commas / 2,040 components | 0.25 / 0.25 / 0.27 / 0.28 | | 0.25 / 0.23 / 0.25 / 0.27 | 0.18 / 0.17 / 0.18 / 0.20 | 59-62 |
| r4's cap edge, 64 `{` / 64 `/[` in 4 KB (reported, not bounded) | 0.35 / 0.72 | | 0.32 / 0.68 | 0.20 / 0.19 | 60-62 |

r5 was not run on r6's hostile and dense rows (r5 has no cap or budget for them; r6's red run stopped it past 30 s).
In the same rows wall time was 0.33-4.51 s here, and r6's top-level row took 35.9 s of wall. The holds' own measures
(H2, load ~100-109): the 300-path median child CPU 0.295 s against the installed hook's 0.203 s, **1.46×**; r4's
hostile rows 0.22-0.24 s of CPU, best of three (decision 6's measure), the cap edge 0.30 and 0.65 s; decision 2's
rows in H1 (load 94) 0.52, 0.28 and 0.38 s against the installed hook's 0.66, 0.33 and 0.61 s.

**The real shells.** r7's 46 command runs (H1): the hook denies 31; zsh walked a root in 25 and bash in 26 (both in
25), neither in 20, and every run agrees but decision 5's five listed over-blocks (denied by design, walked by neither).
r6's 69 runs (H2) agree too: denied 38; zsh 33, bash 27, both 27, neither 36. Decision 4's four open forms: zsh walks
all four, bash the two loops (bash reads `=(` as a syntax error).

**Must-allow cases.** `test_r7_scoped_searches_stay_allowed` holds r6's 198 cases, review r6's 8 allowed forms and
r7's 15 allowed forms; each decision's test holds its own controls. The routine lane commit and r6's 17 allowed lane
commands are in it; the 18th lane command is still decision 7's denial.

**Mutants** (`tools/guard_subfleet_root_mutations.py`, one `git archive` of HEAD each, the named test and the fence,
under the lock; each batch's control passed every named test and the fence):

| Decision | Mutant | Killing test | Batch (admitted, load) |
| --- | --- | --- | --- |
| 1 | `r7-arith-plain` | `test_r7_an_arithmetic_shift_crosses_lines` | M1 (19:55:59Z, 30) |
| 2 | `r7-scan-uncounted` | `test_r7_heredoc_bodies_count_against_the_step_budget` | M2 (20:27:29Z, 58) |
| 3 | `r7-passon-dropped` | `test_r7_a_filter_passes_on_its_process_substitution` | M1 |
| 4 | `r7-known-form-unlisted` (the overlay doc's `=(…)` names dropped) | `test_r7_known_differences_name_the_open_forms` | M1 |
| 5 | `r7-loopvar-unscoped`, `r7-loopvar-anywhere` | `test_r7_a_loop_variable_reassigned_after_its_loop_is_read_as_assigned` | M3 (20:47:12Z, 105) |
| review r6 | `rv6-psub-passon-dropped` (rebuilt on r7's lines: the `cat` and filter pass-on dropped) | `test_r7_a_filter_passes_on_its_process_substitution` | M1 |
| r6 4 | `r6-arith-unread` (retargeted), `r6-unterminated-disables-rest` | `test_r6_an_arithmetic_shift_starts_no_heredoc` | M4 (20:53:25Z, 102) |
| r6 5 | `r6-dq-parend-plain` | `test_r6_a_substitution_in_double_quotes_reads_its_heredoc` | M4 |
| r6 1 | `r6-body-unread` | `test_r6_an_unquoted_heredoc_bodys_substitutions_are_judged` | M5 (21:03:37Z, 120) |
| r6 2 | `r6-xargs-shell-unread` | `test_r6_xargs_into_a_shell_whose_script_names_a_searcher` | M5 |
| r6 3 | `r6-filter-heredoc-dropped` | `test_r6_any_producers_heredoc_piped_into_a_shell_is_its_script` | M5 |
| r6 6 | `r6-equals-backtick-dropped` | `test_r6_zsh_equals_cmd_after_a_backtick_is_read` | D (21:47Z, 77) |
| r6 7 | `r6-span-backtick-dropped` | `test_r6_a_markdown_code_span_in_a_quoted_body_is_an_intended_over_block` | D |
| r6 9 | `r6-psub-unread`, `r6-loop-words-dropped` | `test_r6_process_substitutions_and_loops_are_judged` | D |
| r6 budget | `r6-budget-dropped` (retargeted) | `test_r6_dense_substitutions_fail_closed_fast` | D (21:48Z, 66) |
| r6 8 | `r6-cap-dropped` | `test_r6_hostile_words_fail_closed_fast` fails on it, but with a `JSONDecodeError`, not an assertion: the mutant allows `rg foo src` + 4,097 `$`, and the test parses the empty stdout | D (21:49Z, 80) |

All thirteen run under the lock are killed (r7 first wrote "eleven"; r8's decision 7). D is outside the lock: my waiters went unadmitted for 30 minutes (21:16:49Z
to 21:47Z, five bounded attempts at load 98-137), so, as the lane rules allow, I ran r6's last six directly
(`mutdirect.py` in scratch: the driver's own `mutate()` on a small scratch tree, the named test function called in the
fixture seam, a kill only on `AssertionError`), each after its control passed. Five are killed. `r6-cap-dropped`'s
named test fails on it, which pytest reports as a failure (the driver would count it killed), but my direct runner
counts only an assertion, so I list it as caught, not killed; an `assert rule(...) is not None` before the
`json.loads` would make it an assertion. r7 retargets six older edits whose lines it touched, each to the same edit on its new
text (`r5-heredoc-unread`, `r6-arith-unread`, `r6-budget-dropped`, `r4-keywords-dropped`, `r5-bound-hides-producer`,
`rv4-function-keyword-dropped`; the tool commit's subject says five), and all 78 of the driver's edits match their file
exactly once.

**Unrun at this head:**
- r6's last six mutants ran outside the lock (above), not under it.
- Of `test_guard_subfleet_root.py`'s 128 items, 66 ran (H1-H6, all passed). Unrun: S1-protection, S2, S3 ×2, the cwd
  list, S5, S1-r3, and r3's, r3b's and r3c's tests (examples, the three properties, shells, mounts, folded code
  points). r7 changes only the command prep (`parend`, `arend`, `lnend`, `hdbody`, `prep`, `simple`), not the
  path-word pass those tests judge; review r6 ran all of them at `2bd5b3199` and they passed. Beside S1, this round's
  differential covers the corpus they read: over S1's 528 generated pairs, the fixed corpus and every r4-r7 list, this
  head and r6 differ only on r7's pins and decision 5's five narrowed forms.
- Not runnable here: S4's two items (they need the operator's Claude hook) and `tests/frontend/test_core_live.py`.

**Disclosures.**
- Four runs passed the tool's 10-minute cap and were moved to its background mode, against this round's brief: the
  first head-against-r6 differential (stopped and rerun in bounded chunks with a verdict cache), H6 (S1, a single item
  that took 771 s at load 100-144, so it also passed the brief's 9-minute line; polled to completion in the
  foreground) and the mutant batches M1 and M5 (polled to completion; each one's slow part was removing its exports).
  No process of mine outlived its own run.
- Outside the lock besides the six mutants, all probes: the red runs, the direct calls of r7's tests while building, the
  differential, the step counts (an instrumented copy of the prep's awk in scratch that prints its step count) and the
  timing rows.
- The 18 lane commands are r6's reconstruction; review r5's list is not on disk.

## r8: review r7's findings closed (2026-10-29)

The hub's review r7 (REQUEST_CHANGES at `7447e9dce`, all P3) found six P3s and three report nits. Its three test-only
commits (`cecca6e25`, `0f337c7bf` and `c83be9c89`: `test_review_r7_pins_r7s_unpinned_readings`, 29 denied and 9 allowed
forms, and the `assert` that makes `r6-cap-dropped` fail on an assertion) stay and pass. r8 builds on `c83be9c89`.
Every pin is review r7's input or the brief's, judged through the whole hook in the fixture (`test_r8_*`), and each
command pin and control also runs in real `/bin/zsh -f` and `/bin/bash` 3.2 over the fixture with r6's stubs and the
fixture's HOME (`test_r8_command_pins_agree_with_real_shells`).

**Decisions as built.**

1. **A loop variable keeps its root wherever the shells keep it** (review r7's P3-1, review r7's measured variant).
   - A newline right after `&&`, `||`, `|` or `|&` (blanks and a comment between) continues the line: the prep skips
     it as a separator, so the operator stays `pv`.
   - `time` or `time -p` before a compound command (`if`, `while`, `until`, `for`, `select`, `case`, `repeat`,
     `foreach`, `coproc`, `!`, `{`) is a keyword, so `ld` counts the command it times. zsh's `repeat` and
     `foreach` open one too. A `foreach` closes on `end`, which the prep doesn't count, so it stays open, and so
     does a `repeat` with no `do … done`.
   - A `name()` (any empty `()`) or the word `function` anywhere in the command sets `FN`, which the prep reads once
     over the whole command, and `FN` turns the reset off.
   - Only a plain word (no `$`, backtick, quote, backslash or blank) replaces the loop's words.
   - The hook denies review r7's fifteen inputs. It still allows the sixteenth (`…; d=src; read d <<< ~/.subfleet;
     rg foo "$d"`), which the doc names under "a root held in any variable" (`R8_KNOWN_OPEN`), and r7's five
     intended forms.
   - *Deviation, the smallest change that keeps the intent:* a value that is not a plain word joins the loop's words
     instead of leaving them alone. The counterexample: r7 denies `for d in src; do :; done; d='/'; rg foo "$d"`
     (and `d=\/`, `d='/Users/tester/.subfleet'`), and both shells walk the root. If a quoted value only left the
     loop's words (`src`), the hook would allow all three. With the join, `d='src /'` keeps the loop's root and `d='/'`
     still reads `/`. The join also denies a `$` value that spells a root after a scoped loop (`d=$HOME`,
     `d="$HOME"`), which both shells walk and r7 allowed.
   - *How:* `time` before a simple command stays a wrapper (`WRAP` reads its `-o` and `-f` values), so `time -o f rg
     …` reads as before. A timed simple command is never an assignment-only command, so it never resets.
   - *Addition:* the continuation also keeps the pipe's state, so the command on the next line is still in the
     pipe. That closes `echo 'rg --files ~/.subfleet' |⏎bash`, `find ~/.subfleet -maxdepth 1 |⏎while read d; …` and
     `cat <(…) |⏎⏎bash`. r7 and the installed hook allow all three, and both shells walk them. From the home, it also
     allows `echo x |⏎rg foo` and `git log |⏎⏎rg foo`. r7 denied them: the newline ended the pipe, so rg's stdin
     looked empty and rg looked as if it walked `.`. The installed hook allows them, and in both shells rg reads the
     pipe and walks nothing. The hook still denies `rg foo |⏎cat` from the home.
2. **The 4,096 cap counts `'` and backtick** (P3-2). r6 decision 8's one-pass count also counts them, and the
   message names them. The hook denies review r7's `''`×30,000 row as too complex in 0.22 s of CPU, against the
   installed hook's 0.46 s and r7's 4.05 s. The tests pin the edges: the prep reads 4,096 of either beside a scoped
   search, 4,098 is too complex, and the hook denies a file-writing heredoc with 4,099 quotes that names a searcher.
   That heredoc is the trade-off, which the doc lists under "stricter".
3. **In `$((`, a `<<` is a shift whenever the span ends in `))`** (P3-3). `arend` reads a `$((` span as arithmetic
   when it ends in `))`, whatever its inner `(` closes on. Review r7's `echo $((1) + (1<<X⏎))⏎/usr/bin/find / -name
   x⏎X` is now denied (bash walks it; zsh doesn't). The hook still allows r7's six arithmetic controls, and the
   command form `((1) + (1<<X⏎))…`, which walks in neither shell. The overlay doc's r7 sentence now names this
   reading.
4. **A piped `while read` takes the roots of what a filter passes on** (P3-4). When r7's pass-on puts a filter's
   `<(…)` output on the pipe (`nextin`), its words also join `nextroots`. The hook denies review r7's two inputs and
   a two-`<(…)` form. It allows the brief's control (`cat <(git ls-files) | while read f; do wc -l "$f"; done`), and
   the same loop with `rg -n foo "$f"`.
5. **The hook still denies `tee <(echo 'rg --files ~/.subfleet') | bash`** (P3-5). The doc lists it under
   "stricter", and `R8_STRICTER` pins it: denied, walked by neither shell, named by the doc.
6. **The doc lists the joined productions open** (P3-6), and `R8_KNOWN_OPEN` pins each: allowed here, walked by a
   real shell, named by the doc.
   - `cat <(printf 'rg --files ') <(echo ~/.subfleet) | bash` and `paste -d' ' <(echo 'rg --files') <(echo
     ~/.subfleet) | bash`: the prep joins the two `<(…)` outputs with a line break and judges each line on its own.
   - `{ printf 'rg --files '; echo ~/.subfleet; } | bash`: a pipe carries what the simple command right before it
     writes, and only the group's closing `}` stands there.
   - *Addition:* this round's probes found two more of the last kind, `{ echo 'rg --files ~/.subfleet'; } | bash`
     and `(echo 'rg --files ~/.subfleet') | bash`. r7 and the installed hook allow both, and both shells walk them.
     The doc lists them, and the test pins them, beside review r7's three.
7. **Report nits.**
   - The driver holds 19 r6 and r7 mutants: r7's seven and r6's twelve. r7's final answer said 20.
   - r7 retargeted six edits. r7's tool commit says five; r7's section above already says six.
   - Over r7's own pin lists (57 commands), r7 newly denies 14 walk pins against r6: five arithmetic, eight
     pass-on and one loop variable. With the two body-line budget edges (too complex at r7, read at r6) it is 16.
     That agrees with review r7's 14; r7's section said 15, and now carries this recount.
   - r7's section also said "all eleven run under the lock", but its table lists thirteen there (M1-M5); the
     sentence now says thirteen.
8. **TRUST and docs.**
   - TRUST changes only `hook_sha256`.
   - The overlay doc gains r8's paragraph. Its "Known differences" now reads after r8: the open forms of decisions 1
     and 6, the stricter forms of decisions 1, 2 and 5, and the `while read` sentence for decision 4.
   - The doc's r7 sentences on arithmetic and on the loop variable now say what r8 changed.

**Against r7: 33 newly denied, and two corrected over-blocks newly allowed** (corrected in r9; see the note after the
list). I compared this head with r7 and the installed hook over 1,401 distinct pairs: S1's 779 (its generated and fixed
corpus), the 594 pairs of every r4–r8 command list in the test (92 lists), and 79 probes of mine, 36 of them in no list.
This head newly denies 33 and newly allows two, the corrected over-blocks below. No rule changed, and the installed
hook denies nothing this head allows. The 33:
- 19 are decision 1's pins;
- 4 are decision 2's;
- 1 is decision 3's;
- 3 are decision 4's;
- 2 are the `$HOME` values after a scoped loop, which both shells walk;
- 4 are the function pre-scan's over-blocks: `echo "f()"`, a commit message holding "function", an uncalled
  `function g { :; }` and a comment holding "function". Neither shell walks them, and the doc lists them under
  "stricter".

The two corrected over-blocks are decision 1's pipe continuations from the home (`echo x |⏎rg foo`, `git log
|⏎⏎rg foo`), which a probe of mine found from that cwd: neither shell walks them, and the installed hook allows them
too.

*r9's correction (review r8's P3-3).* This paragraph first said "newly allows none", which its own two corrected
over-blocks contradict, and its heading said nothing here is looser than r7. Review r8's probes found ten inputs this
head allows and r7 denies, none of them among the 1,401 pairs: after a scoped loop, an assignment of a root that this
head drops where the reset doesn't apply (review r8's P3-1, closed in r9). r9's section recounts this head against r7
over the lists.

**Red runs at `c83be9c89`** (the fixture seam, home `/Users/tester`; the probe at 02:04Z, load 24).
- **The probe over the brief's pins:** the hook allowed every decision 1-4 pin: decision 1's fifteen, the
  `''`×30,000 row, decision 3's input and decision 4's two. It already denied the tee form, and it allowed the three
  joined productions, the sixteenth input and every control.
- **The new tests, under the lock** (queued 02:12:24Z, slot B, done 02:14:54Z at load 35): six of the seven failed.
  The must-allow test passed: `c83be9c89` denies no new control.

**Holds** (one pytest each, `-k` selections of `test_guard_subfleet_root.py` unless named, home `/Users/tester`).
Every hold ran under the lock, and each was admitted within a minute of being queued.

| Hold | Admitted (slot, load) | Selection | Result, pytest time |
| --- | --- | --- | --- |
| red | queued 02:12:24Z (B, 35 at the end) | `test_r8_*` at `c83be9c89` | 6 failed, 1 passed, 117 s |
| T | 02:44:33Z (B, 19) | TRUST, the fence, the doc pins, both known-differences tests, the overlay pair | 8 passed, 17 s |
| H1 | 02:49:01Z (B, 24) | `test_r8_*`, `test_r7_*`, review r7's test | 15 passed, 184 s |
| H9 | 02:53:48Z (A, 20) | S1 | 1 passed, 361 s |
| H10 | 03:00:13Z (B, 26) | S1-protection, S2, S3 ×2, the cwd list, S5, S4 ×2, S1-r3, known limits ×2 | 9 passed, 2 skipped (S4), 260 s |
| H2 | 03:04:46Z (B, 15) | r5's and r6's tests, reviews r5 and r6 | 26 passed, 112 s |
| H3 | 03:07:02Z (A, 23) | r4's tests, review r4 | 14 passed, 69 s |
| H4 | 03:08:15Z (A, 23) | r3c's property | 1 passed, 190 s |
| H5 | 03:11:42Z (A, 28) | r3's examples, settings and real-shell globs | 26 passed, 117 s |
| H6 | 03:13:49Z (A, 26) | r3's property | 1 passed, 130 s |
| H7 | 03:16:10Z (A, 11) | r3b's property | 1 passed, 46 s |
| H8 | 03:16:57Z (A, 20) | r3b's and r3c's examples, shells, mounts and folded code points | 24 passed, 59 s |
| H11 | 03:18:07Z (A, 15) | the rest (r1/r2, TRUST, the fence, env spellings, logging) | 16 passed, 27 s |
| HA | 03:18:35Z (A, 11) | `test_guard_overlay.py`, `test_guard_trust.py` | 127 passed, 70 s |
| HB | 03:20:03Z (A, 9) | the three guards | 63 passed, 22 s |

H1 to H11 cover all 136 items of `test_guard_subfleet_root.py` exactly once (checked with `--collect-only`): 134
passed and S4's 2 skipped. H9 wrote the corpus verdicts to a fresh `GUARD_DIFF_CACHE`, and H10 read them, as the
cache is meant to be read across holds. S1 at this head has 779 commands; the old hook denies 119 and this one
313, and the 194 newly denied are r3's intended moves, as at r7.

**Timing rows** (child CPU in seconds, one run each, this head / r7 / the installed hook, load 9-10 at 03:20Z):

| Row | This head | r7 | Installed |
| --- | --- | --- | --- |
| `cat <<A ` + `''`×30,000 (pinned; too complex here) | 0.22 | 4.05 | 0.46 |
| `rg foo src; cat ` + `<<'' `×12,000 (too complex) | 0.16 | 1.82 | 0.28 |
| `rg foo src; echo ` + `''`×30,000 (too complex) | 0.13 | 2.92 | 0.43 |
| `rg foo src; echo ` + `` `:` ``×16,000 (too complex) | 0.13 | 1.44 | 0.15 |
| `` `rg` ``×13,000 (too complex) | 0.13 | 1.48 | 0.05 |
| 300 delimiters, 20,000 lines / 40 and 8,000 / 300 missing (too complex) | 0.43 / 0.18 / 0.34 | 0.43 / 0.17 / 0.33 | 0.52 / 0.21 / 0.50 |
| r5's nine long-word rows | 0.08-0.23 | 0.10-0.25 | 0.11-0.19 |
| r6's 60,000 `$` / 60,000 `#` / 30,000 backslash pairs (too complex) | 0.13 / 0.18 / 0.21 | 0.14 / 0.21 / 0.17 | 0.21 / 0.24 / 0.27 |
| r6's three dense rows (too complex) | 0.12 | 0.11-0.16 | 0.13 |
| r4's four hostile rows | 0.17-0.20 | 0.16-0.18 | 0.10-0.14 |
| r4's cap edge, 64 `{` / 64 `/[` in 4 KB (reported, not bounded) | 0.28 / 0.50 | 0.25 / 0.53 | 0.13 / 0.13 |

The installed hook allows every row; "too complex" and "too long" mark this head's denials, and r7 allows review r7's
five rows and denies the rest as this head does.

Best of three on review r7's five rows (load 8-9): 0.17, 0.11, 0.12, 0.15 and 0.17 here, against 3.69, 1.75, 2.65,
1.71 and 1.90 at r7 and 0.45, 0.33, 0.55, 0.19 and 0.04 for the installed hook. The test bounds the pinned row at
twice the installed hook (`test_r8_quotes_and_backticks_count_against_the_cap`): in H1, at load 23, it took 0.21 s against
the installed hook's 0.53 s.

The `` `rg` ``×13,000 row runs above twice the installed hook: 0.13 s against 0.05 s single, and 0.17 s against
0.04 s best of three.
- The installed hook spends on it what it spends on a command that names no searcher (`` `xy` ``×13,000: 0.05 s),
  so it most likely never reads a searcher in `` `rg` ``.
- A stage split of this head (scratch copies with an `exit` inserted, best of three, load 10.5): 0.086 s before
  `pw_named`, then 0.034 s for `pw_named`, 0.016 s for decision 2's count and 0.031 s for the deny.
- Decision 2 costs 0.016 s of that and takes r7's 1.48-1.90 s down to the cap's floor, which every capped command
  pays (r6's hostile rows cost 0.13-0.21 s here). So this round ships it, and the row's bound stays the brief's: the
  pinned row only.

The 300-path cost test (`test_r4_absolute_paths_cost_at_most_twice_the_installed_hook`, H3, load 23) gave a median
child CPU of 0.237 s against the installed hook's 0.150 s, **1.58×**. r4's hostile rows took 0.17-0.23 s best of
three, and the cap edge 0.24 and 0.55 s.

**The real shells.** r8 runs 56 commands (H1): the hook denies 29; zsh walked a root in 29 and bash in 31 (both in
26), and neither walked in 22.
- Every run agrees but the listed forms: the tee form (denied, walked by neither) and the six open forms (allowed,
  walked by both).
- bash alone walks `…; echo x |⏎d=src⏎…`, `d='src /'; rg foo $d`, `time -p while …` and decision 3's two inputs.
- zsh alone walks `f(); d=src`, `f()⏎d=src` and `repeat 0 do … done`.
- r7's 46 runs and r6's 69 (H1, H2) agree as before.

**Must-allow cases.** `test_r8_scoped_searches_stay_allowed` holds 248 cases (220 distinct):
- r6's 198 (with review r4's allowed forms, the print-and-exit forms from three cwds, the routine `git commit -F -
  <<'EOF'⏎It's fixed⏎EOF` and r6's 17 allowed lane commands);
- review r6's 8 and review r7's 9 allowed forms;
- r7's 15 allowed forms;
- r8's 18: r7's five loop forms again, six continuation and `time` controls, three cap edges, two pass-on
  controls and two pipes from the home.

The hook allows all of them, and r7's and r6's own must-allow tests pass too (H1, H2).

**Mutants** (`tools/guard_subfleet_root_mutations.py`, one `git archive` of HEAD each, the named test and the fence,
all under the lock; each batch's control passed every named test and the fence):

| Decision | Mutant | Killing test | Batch (admitted, load) |
| --- | --- | --- | --- |
| 1, continuation | `r8-continuation-dropped` | `test_r8_a_loop_variable_keeps_its_root_where_the_shells_keep_it` | M1 (03:23:10Z, 9) |
| 1, `time` | `r8-time-keyword-dropped` | the same | M1 |
| 1, function pre-scan | `r8-function-prescan-dropped` | the same | M1 |
| 1, plain word | `r8-plain-word-dropped` | the same | M1 |
| 1, deviation (the join) | `r8-value-joins-dropped` | the same | M1 |
| 1, zsh's `repeat` | `r8-repeat-uncounted` | the same | M1 |
| 2 | `r8-cap-quotes-dropped` | `test_r8_quotes_and_backticks_count_against_the_cap` | M1 |
| 3 | `r8-dollar-arith-dropped` | `test_r8_a_dollar_double_paren_span_ending_in_two_closes_is_arithmetic` | M1 |
| 4 | `r8-passon-roots-dropped` | `test_r8_a_piped_while_read_takes_the_roots_a_filter_passes_on` | M1 |
| 5, 6 | `r8-known-form-unlisted` (the paste form's name dropped from the doc) | `test_r8_known_differences_name_the_stricter_and_open_forms` | M1b (03:24:30Z, 10) |
| review r7 | `rv7-bto-dropped` (`!bto` dropped from both `vtop` lines) | `test_review_r7_pins_r7s_unpinned_readings` | M1b |
| r7 1-5, review r6 | r7's seven (`r7-arith-plain`, `r7-scan-uncounted`, `r7-passon-dropped`, `rv6-psub-passon-dropped`, `r7-known-form-unlisted`, `r7-loopvar-unscoped`, `r7-loopvar-anywhere`) | each one's named test | M2 (03:25:07Z, 9) |
| r6 1-9, budget | r6's twelve (`r6-body-unread` … `r6-budget-dropped`) | each one's named test; `r6-span-backtick-dropped` also fails the fence | M3 (03:25:47Z, 10) |

The tests kill all 30. I rebuilt `rv7-bto-dropped` from review r7's description (its scratch is gone). r8 rewrote
two lines older mutants edit (`r7-passon-dropped`, `r7-loopvar-unscoped`); I retargeted each to the same edit on its
new text, and all 95 of the driver's edits match their file exactly once. M1's first run stopped at
`r8-known-form-unlisted`, whose edit predated the doc bullet's final wording. I retargeted it (`6e26da6ea`) and reran
it with `rv7-bto-dropped` as M1b.

**Unrun at this head:**
- S4's two items. They need the operator's Claude hook, so they skip here.
- `tests/frontend/test_core_live.py`: it can't run on a lane. CI runs both.

**Disclosures.**
- The head-against-r7 differential ran longer than the tool's 10-minute limit. The tool moved it to its background
  mode, as this round's brief allows, and I polled it to completion; no process of mine outlived its run.
- Outside the lock, all probes: the red probe, the decision probes while building, the differential, the timing rows,
  the best-of-three rows, the stage split and the shell probes. Every test and mutant run ran under the lock.
- Review r7 writes the fourth row as `` echo `:` ``×16,000, which names no searcher, so no hook would prep it. I
  rebuilt it as `rg foo src; echo ` + `` `:` ``×16,000. The other four rows follow review r7's text.

## r9: review r8's findings closed (2026-10-29)

The hub's review r8 (REQUEST_CHANGES at `daf7f508c`, all P3) found three P3s. Its two commits stay and pass:
`5868fe119` adds `test_review_r8_pins_r8s_unpinned_readings` (22 denied and 7 allowed forms, and the cap's 4,096/4,097
edges), and `37f706335` adds three probe mutants. r9 builds on `37f706335`. Every pin is review r8's input or this
round's probe, judged through the whole hook in the fixture (`test_r9_*`). Each command pin and control also runs in
real `/bin/zsh -f` and `/bin/bash` 3.2 over the fixture with r6's stubs and the fixture's HOME
(`test_r9_command_pins_agree_with_real_shells`).

**Decisions as built.**

1. **Where the reset doesn't apply, a later value joins the loop's words** (review r8's P3-1, its measured variant).
   - *Before:* r8 skipped the whole update when `FN` was set, a compound command was open (`ld`), a keyword came
     before the assignment (`st["ka"]`) or the assignment might not last (`st["vt"]`). So after a scoped loop it
     dropped an assigned root and kept only the loop's words.
   - *Now:* every assignment-only command at the top of the command updates a loop variable it names. Where the reset
     applies (`vrp`, the old condition) and the value is a plain word, the word replaces the loop's words, as in r7 and
     r8. Anywhere else the value joins them. `vrp` is a local of `simple()`.
   - The hook denies review r8's sixteen inputs. The first ten (`R9_LOOPVAR_DENIED`) r7 denied and r8 allowed: a `()`
     or the word `function` anywhere (in prose, a heredoc body, a commit message or `$()`), a defined function, and
     zsh's `repeat` and `foreach`. The other six (`R9_LOOPVAR_MORE_DENIED`) r7 let through too: an `if`, a brace group,
     a timed group, `&&`, a `foreach` before the assignment, and `d=$HOME` beside a `()`. Both shells walk the root in
     fourteen; zsh alone walks the two `foreach` forms, where bash stops at a syntax error.
   - *Addition:* this round's probes found `for d in src; do d=/; done; rg foo "$d"`, an assignment in the loop's own
     body, where the loop is still open. r8, r7 and the installed hook allow it, both shells walk `/`, and the hook
     now denies it (`R9_LOOPVAR_MORE_DENIED`).
   - A joined depth bound disarms nothing: after a loop over `~/.subfleet/*`, `if false; then d=--max-depth=1; fi`
     and `false && d=--max-depth=1` leave `rg foo "$d"` denied (`R9_BOUND_JOIN_DENIED`), and both shells walk the
     root.
   - Nine controls stay allowed (`R9_LOOPVAR_ALLOWED`): a scoped value joining a scoped loop's words in each of those
     places.
   - *Over-block, listed "stricter":* a root assigned where the shells never run the assignment joins too.
     `for d in src; do :; done; false && d=/; rg foo "$d"`, and the same with `if false; then d=~/.subfleet; fi` or
     `{ :; } || d=/`, are denied, and neither shell walks a root (`R9_STRICTER`). r8 allowed all three.
   - The change moves no verdict among S1's 779 commands or the 665 pairs of every r4–r8 and review list in the test.
     No corpus command holds more than 4,096 `"`, so on that corpus the P3-1-only hook gives head's verdicts.
2. **The 4,096 cap counts `"`** (review r8's P3-2). The one-pass count adds `s = $0; f += gsub(/"/, "", s)` and
   `|| f > 4096`, and the message names `"`.
   - The hook denies review r8's four rows as too complex to judge (`R9_DQ_ROWS`): 60,000 and 30,000 `"` beside a
     scoped search, a heredoc opened before 30,000, and every other capped character at 4,096 with 30,000 `"`.
   - The edges (`R9_DQ_DENIED`, `R9_DQ_ALLOWED`): the hook reads 4,096 `"` beside a scoped search and denies 4,097 as
     too complex. It also reads 4,096 `"` with every other capped character at 4,096 at once.
   - The trade-off, which the doc lists beside the one for `'` and backtick: the hook denies a heredoc that names a
     searcher and carries more than 4,096 double quotes (`say "hi"`×2,049 in a file-writing heredoc), and reads the
     same heredoc with 4,096.
   - The change moves no verdict among the 1,444 pairs of S1 and the lists, none of which holds more than 4,096 `"`.
3. **The recount and the doc** (review r8's P3-3).
   - r8's section said "newly allows none", and its heading said nothing there is looser than r7. Both were wrong, and
     that section now carries the correction.
   - Over the r4–r8 lists (632 pairs at `daf7f508c`), r8 newly denies 31 against r7 and newly allows 2: `echo x
     |⏎rg foo` and `git log |⏎⏎rg foo` from the home, the corrected over-blocks. That agrees with review r8's 31
     and 2. Review r8's own lists (33 more pairs) add 21 newly denied and 2 more corrected over-blocks newly allowed
     from the home, `echo x | # note⏎rg foo` and `cat <<X |⏎hi⏎X⏎rg foo`, which neither shell walks. S1 moves no
     command. r8's 33 newly denied also counted 79 probes of its own, and review r8's probes found P3-1's ten newly
     allowed.
   - The doc's loop-variable bullet under "stricter" now says that the value of every assignment that doesn't end the
     reading joins the loop's words, and it names decision 1's over-block. Its "let through" list marks review r8's
     P3-1 fixed at this head, and its variables bullet names r9's join.
4. **TRUST and the hashes.** TRUST changes only `hook_sha256`, to the r9 hook's `9eb0db09…e7e2`; TRUST is
   `339ca3f0…a2c4`. The overlay doc's table, install step 0, the overlay test, the subfleet-root test and the mutant
   driver name the new pair. The rollback stays `a60d1c51…`/`b3193b97…`.
5. **Mutants.** `r9-join-dropped` (where the reset is off, the value is dropped again) and `r9-cap-dquote-dropped`
   (`"` counted but not capped) join the driver. r9 rewrote the assignment line and the cap's count line, so six older
   edits are retargeted to the same edit on the new text: `r7-loopvar-unscoped`, `r8-function-prescan-dropped`,
   `r8-plain-word-dropped`, `r8-value-joins-dropped`, `r8-cap-quotes-dropped` and `rv8-cap-edge-moved`. The driver
   holds 102 mutants, and all 100 of its edits match their file exactly once.

**The recount at this head** (1,484 pairs: S1's 779, the 665 pairs of every r4–r8 and review list in the test, and
r9's 40). Nothing is looser than r8 or r7, and the installed hook denies nothing this head allows.

| Against | S1 (779) | Lists (665) | r9's pins (40) |
| --- | --- | --- | --- |
| r8 (`daf7f508c`) | no move | no move | 26 newly denied, none newly allowed |
| r7 (`7447e9dce`) | no move | 52 newly denied, 4 newly allowed (the four home pipes above) | 16 newly denied, none newly allowed |
| the installed hook | 194 newly denied, and the two apply_patch cases S3 labels | 379 newly denied | 28 newly denied |

- Against r8, the 26 are decision 1's seventeen pins, its three stricter forms and decision 2's six denied rows and
  edges.
- Against r7, the 16 are the seven pins r7 let through, the three stricter forms and the six cap rows and edges. r7
  already denied review r8's first ten and the two depth-bound forms.
- S1: the installed hook denies 119 commands and this head 313, as at r8.

**The real shells.** r9 runs 31 commands (H3): the hook denies 22; zsh walked a root in 19 and bash in 17 (both in
17), and neither walked in 12.
- Every run agrees but the three stricter forms (denied, walked by neither).
- zsh alone walks the two `foreach` forms.
- r8's 56 runs and r7's 46 (H3) agree as before, and so do r4's to r6's (H4).

**Must-allow cases.** `test_r8_scoped_searches_stay_allowed` (248 cases) and `test_r9_scoped_searches_stay_allowed`
(14: the nine controls, the three cap edges read, a commit message with double quotes and a heredoc of quoted prose
beside a scoped search) pass (H3).

**Holds** (one pytest or driver each, `-k` selections of `test_guard_subfleet_root.py` unless named, home
`/Users/tester`). Every hold ran under the lock, and each was admitted within seven minutes of being queued.

| Hold | Admitted (slot, load) | Selection | Result, time |
| --- | --- | --- | --- |
| H1 | 11:38:59Z (B, 22.8) | S1 | 1 passed, 183 s |
| H2 | 11:44:34Z (B, 12.3) | S1's protection, S1-r3, S2, S3 ×2, S4 ×2, S5, the cwd list, known limits ×2 | 9 passed, 2 skipped (S4), 115 s |
| H3 | 11:48:13Z (A, 11.2) | `test_r7_*`, `test_r8_*`, `test_r9_*`, reviews r7 and r8 | 21 passed, 50 s |
| H4 | 11:51:13Z (B, 24.5) | r4's to r6's tests, reviews r4 to r6, r3c's property | 41 passed, 294 s |
| H5 | 11:56:30Z (B, 23.4) | r3's, r3b's and r3c's other tests, and the rest (r1/r2, TRUST, the fence, env spellings, logging) | 68 passed, 298 s |
| HA | 12:03:40Z (B, 14.1) | `test_guard_overlay.py`, `test_guard_trust.py` | 127 passed, 75 s |
| HB | 12:05:04Z (B, 10.9) | the three guards | 63 passed, 52 s |
| M1-M6 | 12:09:22Z to 12:49:28Z (A then B, 15.1-32.4) | the 102 mutants | below |

H1 to H5 cover all 142 items of `test_guard_subfleet_root.py` exactly once (checked with `--collect-only`): 140 passed
and S4's 2 skipped. H1 wrote the corpus verdicts to a fresh `GUARD_DIFF_CACHE`, and H2 read them.

**Timing rows** (child CPU in seconds, three runs each, interleaved with the installed hook, load 13.6-18.8 at 12:56Z):

| Row | This head | Installed | Median ratio |
| --- | --- | --- | --- |
| `rg foo src; echo ` + `""`×30,000 (60,000 `"`; too complex here) | 0.241-0.278 | 0.635-0.692 | 0.37× |
| `rg foo src; echo ` + `""`×15,000 (30,000 `"`; too complex) | 0.103-0.179 | 0.230-0.338 | 0.51× |
| `cat <<A ` + 30,000 `"` (too complex) | 0.112-0.183 | 0.284-0.353 | 0.50× |
| every other capped character at 4,096, and 30,000 `"` (too complex) | 0.232-0.260 | 0.460-0.650 | 0.39× |
| 4,096 `"` beside a scoped search (read) | 0.192-0.247 | 0.123-0.174 | 1.24× |
| every capped character at 4,096 at once (read; reported, not bounded) | 1.722-1.903 | 0.315-0.435 | 5.41× |
| 300 absolute paths | 0.270-0.325 | 0.160-0.232 | 1.49× |

The installed hook allows every row. The 300-path test itself (`test_r4_absolute_paths_cost_at_most_twice_the_installed_hook`,
H4, load 33) gave a median of 0.207 s against 0.129 s, **1.61×**. In H3, at load 12, r8's pinned `''` row took 0.24 s
against the installed hook's 0.63 s.

**The residual, reported and not fixed.** A command that holds 4,096 of every capped character at once is read, and it
costs 1.72-1.90 s against the installed hook's 0.32-0.44 s, 5.4×. The row is `rg foo src; echo ` followed by
`''`×2,048, `` `:` ``×2,048, `\\`×2,048, `${#x}`×4,096 and `""`×2,048 (38,933 characters). Review r8 measured 0.58 s
against 0.17 s (3.4×) on a row of its own whose text it doesn't give. A cap on the characters' sum would bound it, as
review r8 says; this round leaves that to the hub.

**Mutants** (`tools/guard_subfleet_root_mutations.py`, one `git archive` of HEAD each, the named test and the fence,
all under the lock; each batch's control passed every named test and the fence):

| Mutants | Killing test | Batch (admitted, slot, load) |
| --- | --- | --- |
| `r9-join-dropped` | `test_r9_a_later_value_joins_the_loop_words_where_the_reset_does_not_apply` | M1 (12:09:22Z, A, 16.9) |
| `r9-cap-dquote-dropped` | `test_r9_double_quotes_count_against_the_cap` | M1 |
| review r8's three (`rv8-continuation-top-only`, `rv8-time-brace-dropped`, `rv8-cap-edge-moved`), review r7's `rv7-bto-dropped`, r8's ten | each one's named test | M1 (16 killed) |
| r7's seven (review r6's included), review r5's two, r6's first five | each one's named test | M2 (12:20:05Z, B, 19.5; 14 killed) |
| r6's other seven, r5's first eleven, review r4's first two | each one's named test | M3 (12:24:41Z, B, 15.1; 20 killed) |
| review r4's other two, r5's twelfth, r4's nine, r3c's first eight | each one's named test | M4 (12:31:58Z, B, 17.3; 20 killed) |
| r3c's other four, r3b's eleven, r3's first | each one's named test | M5 (12:40:10Z, B, 30.7; 16 killed, 1 survived) |
| r3's other nine, r1's and r2's seven | each one's named test | M6 (12:49:28Z, B, 32.4; 16 killed) |

The tests kill 101 of the 102. `stall-mount-listed` (r3b) survives, as it has since r3c: r4's section above and review r4
record it as equivalent. r3c's mount branch comes first, so only `/Volumes` reaches the listing guard with `pw_stall`
true, and `pw_holds` is always true there. r9 doesn't touch that code: its diff against `37f706335` changes only
`simple()`'s locals, the loop-variable update and the cap's lines.

**An observation, not built.** After a scoped loop, an assignment by a builtin is let through by this head, r8, r7 and
the installed hook alike, and both shells walk the root: `export d=/`, `declare d=/`, `typeset d=/`, `readonly d=/`,
`eval d=/` and `printf -v d /` before `rg foo "$d"`. The prep reads only assignment-only commands, so these fall under
the doc's existing "a root held in any variable".

**Unrun at this head:**
- S4's two items, which need the operator's Claude hook, so they skip here;
- `tests/frontend/test_core_live.py`, which can't run on a lane. CI runs both.

**Disclosures.**
- Two mutant batches, M1 and M5, ran past the tool's 10-minute limit. The tool moved each to its background mode, as
  this round's brief allows, and I polled each to completion; no process of mine outlived its run.
- Outside the lock, all probes: the sixteen-form and candidate probes, the recount (this head, r8, r7 and the installed
  hook over 1,484 pairs), the shell probes and the timing rows. Every test and mutant ran under the lock.

## r10: review r9's findings closed (2026-10-30)

The hub's review r9 (REQUEST_CHANGES at `b229383fc`, all P3) found three P3s, none a regression. The hub committed its
test as `8cbedc93d` (`test_review_r9_pins_r9s_unpinned_readings`: 13 walked readings, five stricter forms, six
controls and the cap's message), and r10 builds on it. Every pin is review r9's input or this round's probe, judged
through the whole hook in the fixture (`test_r10_*`). Each command pin and control also runs in real `/bin/zsh -f`
and `/bin/bash` 3.2 over the fixture with r6's stubs and the fixture's HOME
(`test_r10_command_pins_agree_with_real_shells`). Below, `Ls` is `for d in src; do :; done` and `R` is
`rg foo "$d"`.

**Decisions as built.**

1. **The join reaches substitutions and nested scripts** (review r9's P3-1, its measured fix).
   - *Before:* the assignment block ran only at depth 0 (`if (mode == "prep" && !depth && t > nt)`), while the `for`
     reading had no depth gate. So inside a substitution or a nested script r9 read the loop but dropped a later
     assignment.
   - *Now:* `!depth` is gone from that line. Inside a substitution `st["vt"]` is 0 (vtop's first term is `!depth`), so
     `vrp` is 0 there, the value always joins, and the reset still never applies.
   - The hook denies review r9's seven inputs (`R10_NESTED_DENIED`): `echo "$(Ls; d=/; R)"`, the same with the
     assignment in an `if`, `Ls; echo "$(d=/; R)"`, `bash -c 'Ls; d=/; R'`, `eval 'Ls; d=/; R'`, a heredoc script into
     `bash`, and `echo "$(for e in x; do Ls; done; d=/; R)"`. r9, r8 and the installed hook allow each (review r9
     found r7 allows them too), and both shells walk `/` in each.
   - *Addition:* this round's probes found that `eval d=/` after a scoped loop is now denied too
     (`R10_EVAL_DENIED`: `Ls; eval d=/; R`, `Ls; eval 'd=/'; R` and `Ls; eval d=~/.subfleet; R`). The prep reads
     eval's argument as a nested script, so the value joins. r9 and the installed hook allow each, and both shells
     walk the root, since eval runs in the shell itself. Review r9 and decision 3 list `eval` among the
     builtins let through; see decision 3 for how the doc says this.
   - A joined depth bound disarms nothing there: `echo "$(for d in ~/.subfleet/*; do :; done; false &&
     d=--max-depth=1; R)"` is denied, and both shells walk the root (`R10_NESTED_BOUND_DENIED`).
   - Controls stay allowed (`R10_NESTED_ALLOWED`): the hub's `Ls; echo "$(d=/)"; R` (the substitution names no
     searcher, so the prep never reads it, and its subshell keeps the value), `echo "$(Ls; d=src; R)"` and
     `bash -c 'Ls; d=lib; R'`. No shell walks a root on them.
   - *Over-blocks, listed "stricter":* a root assigned only in a nested script or a substitution, which never reaches
     the search after it, now joins too. `Ls; bash -c 'd=/'; R` and `Ls; echo "$(d=/; rg foo src)"; R` are denied,
     and neither shell walks a root (`R10_STRICTER`). r9 allowed both.
2. **A function body written before its loop joins** (review r9's P3-2, its measured two-line change).
   - *Before:* the assignment block updated only a name already in `LVAR`, and the `for` reading created the entry.
     So a function body written before the loop, and called after it, lost its value.
   - *Now:* in a command that defines a function (`FN`), a value assigned where the reset doesn't apply
     (`st["ka"] || ld || !st["vt"]`) to a name not yet in `LVAR` is kept in `FA`. A later `for` over that name joins
     it to the loop's words.
   - The hook denies review r9's four inputs (`R10_FUNCTION_DENIED`): `f() { d=/; }; Ls; f; R`, the same with
     `function f { … }`, with `d=~/.subfleet`, and with the assignment in an `if`. r9, r8 and the installed hook allow
     each (review r9: every hook it ran), and both shells walk the root.
   - A depth bound a function body joins disarms nothing: `f() { d=--max-depth=1; }; for d in ~/.subfleet/*; do :;
     done; false && f; R` is denied, and both shells walk the root (`R10_FUNCTION_BOUND_DENIED`).
   - The hub's two controls stay allowed (`R10_FUNCTION_ALLOWED`): `d=/; f() { :; }; Ls; R` (an assignment at the top
     where the reset applies goes to no `FA`, and the loop replaces it) and `f() { d=lib; }; Ls; f; R`.
   - *Over-blocks, listed "stricter":* the uncalled body, `f() { d=/; }; Ls; R`, beside r9's never-run assignment; and,
     this round's probes found, a root the loop replaces, `f() { :; }; if true; then d=/; fi; Ls; R`. Both are denied,
     and neither shell walks a root (`R10_STRICTER`). r9 allowed both.
3. **The doc** (review r9's P3-3, its corrected lines in their form with P3-1's fix).
   - r9's paragraph (`docs/private-guard-overlay.md:250`) now says "Wherever a plain `name=value` at the top of the
     command does not end …", which is what r9 built. The parenthesis review r9 proposed ("not yet inside a quoted
     `$(…)` or a nested script") is dropped. A new r10 paragraph says the join reaches substitutions and nested scripts
     and a function body written before its loop.
   - "Let through" (the variables bullet) names an assignment by `+=` or by a builtin (`export`, `declare`, `typeset`,
     `readonly`, `local`, `eval`, `printf -v`, `read`) and a value a command substitution writes. It gives examples
     the prep doesn't read and both shells walk (`R10_KNOWN_OPEN`, 12 forms). Modeling the builtins is a follow-up.
   - *Deviation, smallest change:* decision 1 closes `eval d=/` (above), so a bare "`eval` is let through" would be
     false at this head. The doc keeps the hub's list of eight builtins and says that of `eval`, only a prefix
     assignment (`d=/ eval 'rg foo "$d"'`) or a builtin it runs (`eval export d=/`) passes. `R10_KNOWN_OPEN` pins
     both, and `R10_EVAL_DENIED` pins the closed form.
   - "Stricter" (the loop-variable bullet, `:412-414` before) now says a root assigned where the shells never run the
     assignment, run it only in a subshell, a substitution or a nested script, or replace it later in the same command
     is denied too. That is review r9's sentence, with "a substitution or a nested script" added for decision 1's
     over-blocks. It lists review r9's five over-blocks (`Ls; (d=/); R`, `Ls; ( d=/ ) && R`,
     `Ls; if true; then d=/ d=src; fi; R`, `f() { :; }; Ls; d=/; d=src; R` and `Ls; f() { d=/; }; R`), decision 1's two
     and decision 2's two, the uncalled body among them.
   - `test_r10_known_differences_name_the_over_blocks_and_what_is_let_through` holds each named form to its verdict,
     to the shells and to the doc's text.
4. **TRUST and the hashes.** TRUST changes only `hook_sha256`, to the r10 hook's `c42f05ef…865f`; TRUST is
   `54cdded8…0432`. The new pair goes into TRUST, the overlay doc's table, install steps 0 and 2 and this report. The
   pins that check those carry it too: the overlay test, the subfleet-root test's `NEW_HOOK_SHA256` and the mutant
   driver's. The rollback stays `a60d1c51…`/`b3193b97…`.
5. **Mutants.**
   - New: `r10-depth-gate-restored` (`!depth` back on the assignment block) and `r10-function-body-join-dropped` (the
     `for` reading no longer joins `FA`), one per fix. Review r9's `rv9-message-dquote-dropped` (the cap's deny message
     drops `"`), which its committed test now kills, joins the driver too.
   - Retargeted: `r7-loopvar-unscoped` deletes the update line, and r10 put an `else` under it. The edit now drops both
     lines, so the awk still parses and no later value is read.
   - The driver holds 105 mutants, and each of its 103 edits matches its file exactly once.

**The recount at this head** (1,677 pairs: S1's 779; the 859 pairs of every r2–review r9 list in the test, taking each
command that names a searcher, with the cwd its pair gives or the scoped cwd, deduplicated against S1; and r10's 39).
Review r9's 1,060 list pairs came from a rule it doesn't state, so this count uses the rule above. The P3-1-only hook is
r9's with decision 1 alone (a scratch copy).

| From → to | S1 (779) | Earlier lists (859) | r10's pins (39) |
| --- | --- | --- | --- |
| r9 (`9eb0db09…`) → decision 1 alone | no move | no move | 12 newly denied (the seven, the three `eval` forms, two stricter), none newly allowed |
| decision 1 alone → this head | no move | no move | 6 newly denied (the four, two stricter), none newly allowed |
| the installed hook (`a60d1c51…`) → this head | 194 newly denied, and the two apply_patch cases S3 labels | 547 newly denied, none newly allowed | 20 newly denied, none newly allowed |

- S1: the installed hook denies 119 commands and this head 313, as at r9 (H1, H1b).
- No list pair the installed hook denies is allowed here.

**The real shells.** r10 runs 37 commands (H3): the hook denies 20; zsh walked a root in 28 and bash in 28 (both in 28),
and neither walked in 9.
- Every run agrees but the four stricter forms (denied, walked by neither) and the twelve open forms (allowed, walked
  by both).
- Both shells walk review r9's eleven inputs and the three `eval` forms.
- r9's 31 runs, r8's 56, r7's 46 and review r9's (H3) agree as before, and so do r4's to r6's (H4a, H4b).

**Must-allow cases.** `test_r10_scoped_searches_stay_allowed` (11: r10's five controls and review r9's six) passes, as do
r8's (248 cases) and r9's (14) (H3).

**Holds** (one pytest or driver each, `-k` selections of `test_guard_subfleet_root.py` unless named, home
`/Users/tester`). Every hold ran under the lock; H1 was admitted 61 s after being queued, and every other hold within
two seconds. H1 ran at
`bcd7f2d84`, whose hook bytes are this head's. Every other hold ran at `9d58ca0fb`, whose tests are this head's.

| Hold | Admitted (slot, load) | Selection | Result, time |
| --- | --- | --- | --- |
| H1 | 03:24:39Z (B, 20.8) | S1 | 1 passed, 238 s (a fresh `GUARD_DIFF_CACHE`) |
| H1b | 04:40:57Z (A, 17.7) | S1 again, at `9d58ca0fb` | 1 passed, 8 s (H1's cache: same hook bytes) |
| H2a | 03:33:25Z (B, 19.5) | S1's protection, S1-r3, S2, S3 ×2, S4 ×2, S5, the cwd list, known limits ×2 | 9 passed, 2 skipped (S4), 170 s |
| H2b | 03:36:22Z (B, 23.2) | the other tests outside the rounds (r1/r2, TRUST, the fence, env spellings, logging) | 16 passed, 54 s |
| H3 | 03:37:24Z (B, 24.7) | `test_r7_*` to `test_r10_*`, reviews r7 to r9 | 27 passed, 97 s |
| H4a | 03:39:08Z (A, 20.6) | r4's tests, review r4 | 14 passed, 40 s |
| H4b | 03:39:58Z (A, 19.9) | r5's and r6's tests, reviews r5 and r6 | 26 passed, 105 s |
| H5a | 03:41:49Z (A, 15.8) | r3's tests | 27 passed, 219 s |
| H5b | 03:45:34Z (A, 16.6) | r3b's and r3c's tests | 26 passed, 213 s |
| HA | 03:49:18Z (A, 10.6) | `test_guard_overlay.py`, `test_guard_trust.py` | 127 passed, 69 s |
| HB | 03:50:31Z (A, 16.4) | the three guards | 63 passed, 27 s |
| M1-M9 | 03:51:30Z to 04:21:30Z (A, 12.5-27.6) | the 105 mutants | below |

H1 and H2a to H5b cover all 148 items of `test_guard_subfleet_root.py` exactly once (checked against `--collect-only`):
146 passed and S4's 2 skipped.

**Timing** (the 300-path command, child CPU in seconds, three runs of five interleaved pairs with the installed hook,
04:25:41-47Z):

| Run | Load | This head | Installed | Median ratio |
| --- | --- | --- | --- | --- |
| 1 | 19.6 | 0.226-0.239 | 0.148-0.159 | 1.52× |
| 2 | 19.1 | 0.214-0.234 | 0.130-0.155 | 1.63× |
| 3 | 19.1 | 0.219-0.236 | 0.138-0.154 | 1.56× |

The installed hook allows the command, and so does this head. The 300-path test itself
(`test_r4_absolute_paths_cost_at_most_twice_the_installed_hook`, H4a, load 22.8) gave a median of 0.239 s against
0.153 s, **1.57×**.

**Mutants** (`tools/guard_subfleet_root_mutations.py`, one `git archive` of HEAD each, the named test and the fence,
all under the lock; each batch's control passed every named test and the fence):

| Mutants | Killing test | Batch (admitted, slot, load) |
| --- | --- | --- |
| `r10-depth-gate-restored` | `test_r10_a_value_joins_inside_a_substitution_or_a_nested_script` | M1 (03:51:30Z, A, 12.5) |
| `r10-function-body-join-dropped` | `test_r10_a_function_body_written_before_its_loop_joins` | M1 |
| `rv9-message-dquote-dropped` | `test_review_r9_pins_r9s_unpinned_readings` | M1 |
| review r8's three, r9's two, review r7's `rv7-bto-dropped`, r8's first two | each one's named test | M1 (11 killed) |
| r8's other eight, r7's first three, review r6's | each one's named test | M2 (03:54:34Z, A, 21.5; 12 killed) |
| r7's other three (`r7-loopvar-unscoped` retargeted), review r5's two, r6's first seven | each one's named test | M3 (03:56:37Z, A, 15.8; 12 killed) |
| r6's other five, r5's first seven | each one's named test | M4 (03:58:25Z, A, 15.5; 12 killed) |
| r5's other five, review r4's four, r4's first three | each one's named test | M5 (04:01:20Z, A, 20.4; 12 killed) |
| r4's other six, r3c's first six | each one's named test | M6 (04:05:06Z, A, 15.3; 12 killed) |
| r3c's other six, r3b's first six | each one's named test | M7 (04:10:09Z, A, 18.6; 12 killed) |
| r3b's other five, r3's first seven | each one's named test | M8 (04:16:45Z, A, 27.3; 11 killed, 1 survived) |
| r3's other three, r1's and r2's seven | each one's named test | M9 (04:21:30Z, A, 27.6; 10 killed) |

The tests kill 104 of the 105. `stall-mount-listed` (r3b) survives, as it has since r3c, where it became equivalent
(r4's section and review r4). r10 doesn't touch that code: its hook diff against `8cbedc93d` changes only the assignment
block's gate, its comment, the `FA` line and the `for` reading's join. The fence fails, as designed, on the six mutants
whose edit is a line the fence itself undoes: r1's and r2's root edits (`new-roots-removed`, `child-form-dropped`,
`prefix-match`, `trailing-slash-missed`, `env-spellings-dropped`), and `r6-span-backtick-dropped`, which edits r5's
`root_end` line in `R5_TEXT`. It passes on the other 99 and on every control. A kill counts only the named test.

**Unrun at this head:**
- S4's two items, which need the operator's Claude hook, so they skip here;
- `tests/frontend/test_core_live.py`, which can't run on a lane. CI runs both.

**Disclosures.**
- Batches M6 and M7 ran in one tool call, which passed the tool's 10-minute limit and moved to its background mode, as
  this round's brief allows. I polled it to completion. No process of mine outlived its run.
- Outside the lock, all probes: the candidate and over-block probes, the recount (r9, decision 1 alone, this head and
  the installed hook over 1,677 pairs), the shell probes and the timing rows. Every test and mutant ran under the lock.

## r11: review r10's findings closed (2026-10-30)

The hub's review r10 (REQUEST_CHANGES at `0117418c5`) found one P2 and five P3s. The hub committed its test as
`15fb54b04` (`test_review_r10_pins_r10s_unpinned_readings`) and merged `release/217` at `a093fb4b6` (`ab9ed1b0e`), and
r11 builds on that. Every pin is review r10's input or this round's probe, judged through the whole hook in the fixture
(`test_r11_*`). Each command pin and control also runs in real `/bin/zsh -f` and `/bin/bash` 3.2 over the fixture with
r6's stubs and the fixture's HOME (`test_r11_command_pins_agree_with_real_shells`). Below, `Ls` is `for d in src; do :;
done`, `L` is `for d in ~/.subfleet/*; do :; done` and `R` is `rg foo "$d"`.

**Decisions as built.**

1. **Loop-variable words cost steps of the shared budget** (review r10's P2-1, its V4).
   - *Before:* a searcher operand read through a loop variable (`:2023` at r10) became every word `LVAR` held, and
     nothing charged them. r10's joins fed `LVAR` by new routes, so review r10's 7.9 KB command ran past the hook's
     60 s timeout, and r6's route cost 26 s at 6.3 KB.
   - *Now (V1):* each word the operand is read as costs a step of the one 20,000-step budget the prep shares (`pst`).
     Past it the prep stops and the command is too complex to judge. No second budget.
   - *Deviation, smallest change:* a count of words alone let one long word through. `for d in /<4,000 a>; do :; done;`
     followed by 2,000 `rg x "$d";` (26 KB) is charged 2,000 steps, and V1 took 25 s of CPU on it (40 s of wall at load
     25) before allowing it; 1,000 searches took 10 s. Each 16 bytes of the words is a step too, so every 16 bytes the
     prep copies into the command is counted. The row is now denied as too complex in 0.15 s. At 64 bytes a step, 100
     words of 64 bytes read 100 times were still allowed after 2.45 s; at 16 the same command is too complex.
   - *Now (V4):* the path-word judgment lists and judges each distinct path word once, at its first place. `FK` keeps that
     place by everything `pw_word` reads (context, quoting, cd reading, form and text), and every later place takes its
     verdict. Without it, 100 path words read 99 times cost 8.1 s of CPU; with it, 0.42-0.48 s.
   - The "too complex" message names loop variables ("… or loop variables standing for more text, than the guard
     reads").
   - Every row of review r10's cost table, and the long word, is denied as too complex in at most 1 s of child CPU
     (`R11_COST`, bound `R11_COST_CPU_S`). In H3, at load 15, the eight rows took 0.11-0.15 s; the installed hook took
     0.10-0.16 s on them. Review r10 measured r10 at more than 75 s on the first and 9.4-25.9 s on its other rows.
   - The budget's edges are exact (`R11_STEP_EDGE`): 100 words `a` (199 bytes, 112 steps a search) read 178 times are
     19,936 steps, read, and 179 times 20,048, too complex; one 1,600-byte word (101 steps) read 198 times is 19,998
     steps, and 199 times 20,099.
   - *Worst under the budget this round found:* 100 path words of 15 bytes read 99 times on one line, 0.77-1.10 s of
     CPU; the same a line each, 0.42-0.48 s; 63 words of 63 bytes read 63 times, 0.69-1.04 s (`R11_UNDER`, each allowed
     in at most 3 s; H3 at load 15 and probes at load 18-21). One 4,000-byte word read 79 times, at the byte edge, cost
     0.9 s.
   - *Trade-off, listed "stricter":* review r10's batch script, `bash <<'EOF2'` with `Ls` and lines of `d=src/mN; rg -n
     foo "$d"`, is too complex from 162 lines (161 are read; review r10 measured V1 alone at 200). Each value joins the
     loop's words in the nested script, so each search reads all of them. Neither shell walks a root.
   - *The whole test file on this build* (H1-H5b): 153 passed and S4's 2 skipped. No earlier test moves.
2. **A function body's value joins a piped `while read` loop** (review r10's P3-1, its V2, one line).
   - The `read` reading joins `FA` as the `for` reading does. The hook denies review r10's input,
     `f() { d=/; }; echo src | while read d; do :; done; f; R`, and four spellings (`function f`, a `~/.subfleet` value,
     an `if` in the body, `read -r`; `R11_READ_FUNCTION_DENIED`). r10 and the installed hook allow each, and both
     shells walk the root.
   - Controls stay allowed: a scoped value in the body and a root assigned to another name (`R11_READ_FUNCTION_ALLOWED`).
   - *Over-block, listed "stricter":* `f() { d=/; }; echo src | while read d; do f; done; R`, of the listed kind (bash
     runs the loop in a subshell, and zsh's `read` replaces the value at the end of its input).
3. **An inner-scope loop joins an earlier loop's words** (review r10's P3-4, its V3).
   - A `for` loop read where the reset would not apply (`st["ka"] || ld > 1 || !st["vt"] || FN`: `ld` counts the loop
     itself) joins the words its variable stood for, and every `read` joins.
   - The hook denies review r10's seven inputs (`R11_INNER_DENIED`): after `L`, a loop over `src` in a `bash -c` script,
     an `x=$(…)`, a `<(…)`, a function body, a subshell, a quoted `$(…)` that searches, and a piped `while read`, each
     before `R`. r10 and the installed hook allow each (review r10: r9 and r8 too). Both shells walk ~/.subfleet's
     children in the first six, and bash in the seventh.
   - Two forms read by one term alone are pinned too (`R11_INNER_TERMS_DENIED`): a brace group a pipe runs in a subshell
     (`ld > 1`; both shells walk) and zsh's `coproc for …` (`st["ka"]`; zsh walks, and bash 3.2 has no `coproc`).
   - A loop at the top of the command still replaces the words, and scoped loops stay allowed (`R11_INNER_ALLOWED`, 8).
   - *Deviation, smallest change:* the words a function body assigned (`FA`) join once, where the variable is first
     read, not again at each joining loop. `FA` stops growing once a loop reads the name, so the words are the same.
     Review r10's form copied `FA` into every later loop: 3.2 s of CPU against 0.75 s (r10: 0.79 s) on a 50 KB command
     with 1,000 loops after a 3,000-value body. The reading adds no local, since `simple()` is at the awk's limit of 50
     arguments and locals (a 51st made the prep fail).
   - *Over-blocks, listed "stricter":* review r10 counted five; this round's probes found 13 of two kinds, each denied,
     and neither shell walks a root (`R11_STRICTER`):
     - a search in an inner scope that reuses a root loop's name (seven): `for d in ~/.subfleet/*; do echo "$d"; done;
       ls src | while read d; do R; done`, and the same with the inner loop in a `bash -c` script, a subshell, an
       `x=$(…)`, a quoted `$(…)`, a function body or a `<(…)`;
     - a loop the shells run in place where the prep reads it as joining (six): `L; if true; then Ls; fi; R`, and the
       loop after `!` or `time`, in a brace group, nested in another loop, or in a command that defines a function.
   - *Cost, inside decision 1's budget:* every join is read only through a substitution, which decision 1 charges.
     Joins that no search reads cost what r10's did: a loop over 9,000 words in four nested quoted `$(…)`, 4.4 s of CPU
     against r10's 3.6 s; 2,000 loops in three, 2.5 s against 2.5 s; 2,500 loops in `bash -c`, 1.9 s against 1.8 s.
4. **P3-5, listed, not modeled.** A search in a function body written before its loop, `f() { R; }; for d in
   ~/.subfleet/*; do f; done` and `f() { R; }; Ls; d=/; f`, is under "let through" in the overlay doc and pinned open in
   `R11_KNOWN_OPEN`. Every hook allows both, and both shells walk the root. The prep reads in textual order; modeling
   it needs a second reading of a command that defines a function, which the hub records as a follow-up.
   - *Found this round, listed, not modeled:* a loop a pipe or `&` runs in a subshell, over an earlier root loop's
     name: `L; Ls | cat; R` and `L; Ls & wait; R`. Every hook allows both, and both shells walk the root. The loop sits at
     the top of the command, where V3's reading replaces. Pinned open in `R11_KNOWN_OPEN`, and under "let through".
5. **Mutants.**
   - Review r10's two, killed by its committed test: `rv10-fa-ld-dropped` (the `ld` term of r10's `FA` gate) and
     `rv10-depth-reset` (the reset applying at depth).
   - One per term: decision 1's word charge, byte charge, judging each path word once and the message
     (`r11-word-charge-dropped`, `r11-byte-charge-dropped`, `r11-judged-at-each-place`, `r11-message-loop-unnamed`);
     decision 2's join (`r11-read-function-join-dropped`); decision 3's four terms and `read`'s join
     (`r11-join-ka-dropped`, `-ld-`, `-vt-`, `-fn-`, `r11-read-join-dropped`). `r11-join-fn-dropped` is killed by the
     doc-pin test's stricter form: the term reads no form the shells walk that another term misses, only the
     over-block of a loop in a command that defines a function.
   - Retargeted: `r6-loop-words-dropped` drops the substitution line, now after the charge, and
     `r10-function-body-join-dropped` drops `FA` from the `for` reading's replacing branch.
   - The driver holds 117 mutants, and each of its 109 edits matches its file exactly once.
6. **The text** (review r10's P3-3). The overlay doc's eval sentence is review r10's corrected one: "of `eval`, a prefix
   assignment, a builtin it runs, or a `+=` or substitution value it assigns passes, since r10 decision 1 reads `eval
   d=/` as a nested script", with its three forms pinned open (`R11_KNOWN_OPEN`). An r11 paragraph covers decisions
   1-3, "let through" names P3-5 and the pipe and `&` form, and "stricter" names decision 1's batch script and
   decisions 2 and 3's over-blocks. `test_r11_known_differences_name_the_over_blocks_and_what_is_let_through` holds each
   named form to its verdict and to the doc's text.
7. **TRUST and the hashes.** TRUST changes only `hook_sha256`, to the r11 hook's `7bba4822…e102`; TRUST is
   `4e270e5b…14f8`. The new pair goes into TRUST, the overlay doc's table, install steps 0 and 2 and this report. The
   pins that check those carry it too: the overlay test, the subfleet-root test's `NEW_HOOK_SHA256` and the mutant
   driver's. The rollback stays `a60d1c51…`/`b3193b97…`.

**The recount at this head** (outside the lock, from the scoped cwd): over the 977 commands that name a searcher in every
R2-R10 and review R2-R10 list constant of the test, r10 (`c42f05ef…`) and this head give the same verdict on each. Over
r11's 46 pinned commands, 29 are newly denied (decision 2's five, decision 3's nine and the 15 stricter forms) and none
is newly allowed. S1: the installed hook denies 119 of 779 commands and this head 313, as at r10 (H1).

**The real shells.** r11 runs 47 commands (H3): the hook denies 29; zsh walked a root in 20 and bash in 20 (both in 19),
and neither walked in 26.
- Every run agrees but the 15 stricter forms (denied, walked by neither) and the seven open forms (allowed, walked by
  both).
- Both shells walk decision 2's five inputs and the first six of decision 3's.
- Earlier rounds' shell tests agree as before (H3, H4).

**Holds** (one pytest or driver each, `-k` selections of `test_guard_subfleet_root.py` unless named, home
`/Users/tester`). Every hold ran under the lock; H1 was admitted 57 s after being queued, H4 124 s and H5a 196 s, and
every other hold within 21 s. H1 ran at `ce688792c`, whose hook bytes and S1 test are this head's; every other hold ran
at `446a307ef`, whose tests and hook are this head's.

| Hold | Admitted (slot, load) | Selection | Result, time |
| --- | --- | --- | --- |
| H1 | 14:41:24Z (A, 18.4) | S1 | 1 passed, 223 s |
| H2 | 14:58:50Z (B, 13.4) | everything outside S1 and the rounds | 25 passed, 2 skipped (S4), 352 s |
| H3 | 14:56:58Z (A, 28.1) | `test_r7_*` to `test_r11_*`, reviews r7 to r10 | 34 passed, 102 s |
| H4 | 15:07:31Z (B, 15.2) | r4's to r6's tests, reviews r4 to r6 | 40 passed, 224 s |
| H5a | 15:15:01Z (B, 35.7) | r3's tests | 27 passed, 356 s |
| H5b | 15:21:33Z (B, 20.8) | r3b's and r3c's tests | 26 passed, 235 s |
| HA | 15:25:38Z (B, 27.9) | `test_guard_overlay.py`, `test_guard_trust.py` | 127 passed, 79 s |
| HB | 15:27:00Z (A, 25.4) | the three guards | 63 passed, 34 s |
| M1-M14 | 15:28:03Z to 16:16:24Z (A, 19.6-38.7) | the 117 mutants | below |

H1 to H5b cover all 155 items of `test_guard_subfleet_root.py` exactly once (checked against `--collect-only`): 153
passed and S4's 2 skipped.

**Timing** (the 300-path command, child CPU in seconds, three runs of five interleaved pairs with the installed hook,
outside the lock, 16:18:57-19:11Z):

| Run | Load | This head | Installed | Median ratio |
| --- | --- | --- | --- | --- |
| 1 | 23.3 | 0.229-0.353 | 0.157-0.210 | 1.52× |
| 2 | 22.8 | 0.255-0.305 | 0.175-0.218 | 1.40× |
| 3 | 22.4 | 0.184-0.355 | 0.118-0.224 | 1.75× |

Both hooks allow the command. The 300-path test itself (`test_r4_absolute_paths_cost_at_most_twice_the_installed_hook`,
H4, load 25.6) gave a median of 0.279 s against 0.175 s, **1.59×**. The cost pins are decision 1's above.

**Mutants** (`tools/guard_subfleet_root_mutations.py`, one `git archive` of HEAD each, the named test and the fence,
all under the lock; each batch's control passed every named test and the fence):

| Mutants | Killing test | Batch (admitted, slot, load) |
| --- | --- | --- |
| `rv10-fa-ld-dropped`, `rv10-depth-reset` | `test_review_r10_pins_r10s_unpinned_readings` | M1 (15:28:03Z, A, 25.0) |
| `r11-word-charge-dropped`, `r11-byte-charge-dropped`, `r11-message-loop-unnamed` | `test_r11_loop_variable_words_cost_steps_of_the_shared_budget` | M1 |
| `r11-judged-at-each-place` | `test_r11_each_path_word_is_judged_once` | M1 |
| `r11-read-function-join-dropped` | `test_r11_a_function_body_joins_a_piped_while_read_loop` | M1 |
| `r11-join-ka-dropped`, `-ld-`, `-vt-`, `r11-read-join-dropped` | `test_r11_a_loop_in_an_inner_scope_joins_an_earlier_loops_words` | M1 |
| `r11-join-fn-dropped` | `test_r11_known_differences_name_the_over_blocks_and_what_is_let_through` | M1 (12 killed) |
| r10's two (`r10-function-body-join-dropped` retargeted), review r9's, review r8's three, r9's two, review r7's, r8's first three | each one's named test | M2 (15:33:57Z, A, 23.5; 12 killed) |
| r8's other seven, r7's first | each one's named test | M3 (15:42:55Z, A, 38.7; 8 killed) |
| r7's other five, review r6's, review r5's two | each one's named test | M4 (15:48:42Z, A, 31.0; 8 killed) |
| r6's first eight | each one's named test | M5 (15:51:38Z, A, 26.0; 8 killed) |
| r6's other four (`r6-loop-words-dropped` retargeted), r5's first four | each one's named test | M6 (15:53:45Z, A, 31.7; 8 killed) |
| r5's next seven, review r4's first | each one's named test | M7 (15:57:52Z, A, 38.4; 8 killed) |
| review r4's other three, r5's last, r4's first four | each one's named test | M8 (15:59:48Z, A, 26.1; 8 killed) |
| r4's other five, r3c's first three | each one's named test | M9 (16:01:54Z, A, 25.1; 8 killed) |
| r3c's next eight | each one's named test | M10 (16:05:42Z, A, 29.3; 8 killed) |
| r3c's last, r3b's first seven | each one's named test | M11 (16:08:01Z, A, 21.1; 8 killed) |
| r3b's other four, r3's first four | each one's named test | M12 (16:10:39Z, A, 19.6; 7 killed, 1 survived) |
| r3's other six, r1's first two | each one's named test | M13 (16:12:44Z, A, 20.4; 8 killed) |
| r1's other two, the two TRUST mutants, r2's | each one's named test | M14 (16:16:24Z, A, 26.6; 5 killed) |

The tests kill 116 of the 117. `stall-mount-listed` (r3b) survives, as it has since r3c, where it became equivalent;
r11 doesn't touch its code. The fence fails, as designed, on the six mutants whose edit is a line the fence itself
undoes (r1's and r2's five root edits and `r6-span-backtick-dropped`), and passes on the other 111 and on every
control. A kill counts only the named test.

**Unrun at this head:**
- S4's two items, which need the operator's Claude hook, so they skip here;
- `tests/frontend/test_core_live.py`, which can't run on a lane. CI runs both.

**Disclosures.**
- Outside the lock, all probes: the cost and edge probes, the variant hooks (V1 by words alone, three byte
  granularities, judging at each place, review r10's V3 form), the shell and over-block probes, the recount (r10 and
  this head over 977 list commands and 46 pins) and the timing rows. Every test and mutant ran under the lock.
- No Bash call moved to the background, and no process of mine outlived its run.

## r12: review r11's findings closed (2026-10-30)

The hub's review r11 (REQUEST_CHANGES at `c134c83bf`) found one P2 and three P3s. The hub committed its test and two
mutants as `0d2401f07`, and an earlier job for this round committed decision 1's code as `82df89ded` before it stopped.
A second job (r12b) committed the rest of the code, its pins, the mutants, TRUST and the doc (`27ad3e5d7` to
`44995a5d1`), and a cold boot ended it while its holds ran. A third job (r12c) checked that head against the hub's
decisions, reran every hold, mutant and timing run at `44995a5d1` (whose hook, tests and driver are this head's) and
wrote this section. Every pin is review r11's table or a row the hub named, judged through the whole hook in the
fixture (`test_r12_*`). The hub's decisions forbade a search for other places and new cost inputs, so this round built
none beyond the two door edges below, which decision 6's mutants need.

**Decisions as built.**

1. **What find's `{}` stands for costs steps of the shared budget** (review r11's P2-1).
   - *Before:* `execs` made each `{}` all of find's roots, and a `{}` inside a word the first root, and nothing charged
     either. Review r11 measured 150 roots and 150 `{}` (1.1 KB) at 2.6 s of CPU, 300 of each at 26 s, and 400 past
     120 s of wall time; `find /<3,000 a> -exec rg x b{}…×1000 \;` took 10.4 s.
   - *Now:* `execs` spends what each `{}` stands for (`spend`, decision 2's helper: a step for each word and each 16
     bytes, on the one 20,000-step budget). Past the budget the prep stops, and the command is too complex to judge.
     The message names find's `{}` ("… or find's {} standing for more paths …").
   - Every row of review r11's table is denied as too complex in at most 1 s of child CPU (`R12_BRACES`, bound
     `R11_COST_CPU_S`): in H3, at load 31.8, 0.10-0.13 s, where the installed hook took 0.11-0.13 s and allows each.
   - The budget's edges are exact (`R12_BRACE_EDGE`): 100 roots `a` (199 bytes, 112 steps a `{}`) read 178 times are
     19,936 steps, read, and 179 times 20,048, too complex; one 1,600-byte root (101 steps) read 198 times is 19,998
     steps, and 199 times 20,099.
   - 100 roots and 100 `{}` stay read (0.67 s of CPU in H3; bound `R11_UNDER_CPU_S`). Routine forms don't move:
     `-exec grep -l foo {} +`, `-exec sh -c 'rg -n TODO "$1"' _ {} \;` and `-exec rg -l foo {} +` over four roots are
     allowed by this hook and the installed one, and `find ~/.subfleet … -exec rg foo {} …` is denied as a root, not as
     too complex (`R12_BRACES_ALLOWED`, `R12_BRACES_DENIED`).
   - Review r11's test loses only its strict expected-failure mark, and passes (H3).
2. **printf's repeat and the script a shell, `eval` or `source` reads cost steps too.**
   - *Before:* `pfmt` repeated a printf format while words remained (up to 64 rounds), and a pipe into a shell carried
     every earlier stage's text into each later shell; nothing charged either. The hub ran four of the rows below at
     `82df89ded` (all but the 63 KB row and the two `<(…)` rows): each ran past 40 s, at 29-40 s of CPU when stopped.
   - *Now:* one helper, `spend(s)`, charges `s` to the budget as the loop variables' charge does (words plus
     `length / 16`). `pfmt` spends its format on each round after the first; `eval` spends its script; `source <(…)`
     spends what the substitution writes; a shell spends what a pipe or a substitution gives it, and the words a `-c`
     script that runs its arguments runs. `execs` uses it too. The loop substitution's own line stays as it was, since
     its mutants name that text. The message names printf's repeat and text piped into a shell ("… or more text from
     printf's repeated format or piped into a shell …").
   - *Deviation, smallest change:* a shell doesn't spend its own here-string or heredoc. The hub's trial spent whatever
     script a shell reads. On a scratch copy with the trial's door, that moves two commands beyond the one the hub
     named, both executed this round: r11's pinned batch row, `bash <<'EOF2'` with `for d in src; do :; done` and 161
     lines of `d=src/mN; rg -n foo "$d"` (`R11_BUDGET_READ`, which r11's tests hold read, since no shell walks a root),
     goes from read to too complex, and so does `bash <<'EOF'` with 4,000 lines of `rg -n foo src/x` (64 KB). Both are
     text of the command, whose heredoc lines the budget already counts. The door reads `(st["ps"] == "" && hstr hd !=
     "") || spend(scr)`, with no new local in `simple()`. At this head the 161-line row is read (0.44 s, probe) and the
     162-line row too complex, as at r11, and the 4,000-line heredoc is read (0.99 s, probe).
   - Each of the hub's rows is denied as too complex (`R12_PIPED`), in at most 3 s of child CPU, or 10 s for the two
     64 KB rows (`R12_PIPED_CPU_S`, `R12_LONG_CPU_S`). In H3, at load 32.3-32.6; the installed hook allows each:

     | Row | Bytes | This head | Installed |
     | --- | --- | --- | --- |
     | `printf` 16 KB format, 64 rounds, `\| bash` | 16,664 | 0.32 s | 0.05 s |
     | the same in `eval "$(…)"` | 16,667 | 0.34 s | 0.04 s |
     | `source <(…)` of the same | 16,667 | 0.28 s | 0.03 s |
     | `bash <(…)` of the same | 16,665 | 0.31 s | 0.05 s |
     | `echo` 8 KB through 1,000 shells | 15,199 | 0.39 s | 0.14 s |
     | `printf` 63 KB format, 64 rounds, `\| bash` | 63,280 | 1.53 s | 0.07 s |
     | `echo` 32 KB through 4,600 shells | 64,975 | 0.69 s | 0.20 s |

     Most of the 63 KB row's cost comes before any charge, in reading its 31,500 short words: the same format as one
     63 KB word is denied in 0.25 s (probe), and review r11 measured 64 KB of short words with no printf at 2.47 s (its
     listed difference 3).
   - *Deviation for decision 6, the door edges:* on the rows above, `pfmt`'s charge alone spends the budget, so neither
     the `eval` door's charge nor the `source` door's decides one of them. Executed on scratch copies: with `eval`'s
     charge removed, the `eval` row is still too complex (0.29 s), and with `source`'s removed, the `source` row is too
     (0.33 s). So each door's mutant needs an input of its own, and the test pins each door's exact edge
     (`R12_DOOR_EDGE`). `eval 'rg x'` with K words `a` is K + 2 words in 2K + 4 bytes: K = 17,776 is 20,000 steps, read,
     and 17,777 is 20,001, too complex, so nothing else in the command costs a step. What `source` reads from `<(echo
     'rg x …')` is 2K + 3 words in 4K + 7 bytes, after 4 steps spent before it (traced on a scratch copy: 17,775 words
     and 35,551 bytes at K = 8,886), so K = 8,886 is 20,000 steps, read, and 8,887 is 20,003. Each copy without its
     door's charge allows that door's 20,001- or 20,003-step edge. The installed hook and r11 allow all four edges.
   - These stay read (`R12_PIPED_READ`): `echo 'rg -n foo src' | bash`, `printf 'rg foo %s\n' src tests | sh`, `cat
     <<'EOF' | bash` with one line, `bash <<'EOF'` with 3,333 lines (53 KB), `bash -c` and `eval` with a 60 KB script,
     1 KB through 10 shells, a printf format of about 100 bytes, and 2,500 lines (40 KB) through `cat <<'EOF' | bash`.
   - *The one move, intended:* `cat <<'EOF' | bash` with 4,000 lines of `rg -n foo src/x` (64 KB, the hub's "60 KB"
     row), which r11 reads (0.91 s) and the installed hook allows, is too complex to judge, because the pipe's text now
     costs steps on top of the heredoc's lines (`R12_PIPED_MOVED`). Its only search is `rg -n foo src/x`. The overlay
     doc lists it under "stricter".
   - `echo 'rg foo ~/.subfleet' | bash` is still denied as a root, not as too complex (`R12_PIPED_ROOT`).
3. **Nothing else in this class.** This round looked for no other place that turns one word into many, and built no
   cost input beyond the hub's rows and the two door edges. The doc's budget line names the charged places: find's `{}`,
   each round of a printf format after the first, the script `eval` or `source` runs, and the script a shell runs from
   a pipe, a substitution or its arguments.
   - *Met while reading the shell doors, not executed and not charged* (decision 3 and the stop rule): a shell under
     xargs whose `-c` script runs its arguments, or a `-c` with no script, runs what xargs reads (`script(pin, …)` after
     the `-c` arguments door), and nothing spends it. It is listed for review r12.
4. **The P3s need no code.** P3-1 closed with `0d2401f07` (review r11's test and its two mutants, killed in MB1). P3-2:
   the overlay doc's list of loops the prep reads as joining takes review r11's corrected kinds (an `if`, a `case` arm
   or a brace group; after `!`, `time`, `&&` or `||`; an `eval` script; another loop; a command that defines a
   function; the top of a nested script or substitution after a root loop over the same name there), and "a `for` loop
   at the top of the command still replaces the words" now says where the top is: where a plain assignment would last
   (r7 decision 5), with no other compound command open, no keyword before the loop and no function defined in the
   command. That is the code's condition (`st["ka"] || ld > 1 || !st["vt"] || FN` joins; `ld` counts the loop itself).
   The forms are pinned in `REVIEW_R11_STRICTER`. P3-3 is the PR body.
5. **Listed differences.** The overlay doc's "let through" takes review r11's lines: a redirection, a heredoc operator
   or a `$((…))` between a searcher and its root (`find 2>&1 / -name x`), and a loop whose list expands to nothing
   after a root loop over its name. Its budget line takes the third: a 64 KB command of short words, distinct path words
   or globs costs 2.5-11 s of CPU (installed 0.3 s). r11's subshell-loop gap now names review r11's spellings: `|&` and
   `&|` (zsh only), `&!`, `2>&1 | cat`, `& R`, a pipe continued on a new line and `| while read x`. None is modeled;
   all but `&|` are pinned open in `REVIEW_R11_OPEN`.
6. **Mutants.**
   - One per term of `spend` (`r12-word-charge-dropped`, `r12-byte-charge-dropped`) and `execs`' charge
     (`r12-execs-uncharged`), each named against the `{}` test.
   - One per door (`r12-eval-door-uncharged`, `r12-shell-door-uncharged`, `r12-source-door-uncharged`), `pfmt`'s charge
     (`r12-pfmt-uncharged`) and the message (`r12-message-unnamed`, which drops both of r12's clauses), each named
     against the printf and piped-text test. The shell door's mutant drops the charge where a shell reads a pipe or a
     substitution; the `-c` arguments door's charge has no row of its own among the hub's, and no mutant names it.
   - Retargeted to this round's lines: `r3c-eval-unwrap-dropped` (eval's line now spends first) and
     `r11-message-loop-unnamed` (it drops the loop clause from the longer message).
   - The driver holds 127 mutants, and each of its 119 edits matches its file exactly once.
7. **TRUST and the hashes.** TRUST changes only `hook_sha256`, to the r12 hook's `e5b387c4…62d7`; TRUST is
   `c91e21b4…8fd7`. The new pair goes into TRUST, the overlay doc's table, install steps 0 and 2 and this report, and
   the pins that check those carry it too: the overlay test, the subfleet-root test's `NEW_HOOK_SHA256` and the mutant
   driver's. The rollback stays `a60d1c51…`/`b3193b97…`.

**The recount at this head** (outside the lock, from the scoped cwd, in the fixture): over the 1,127 commands that name
a searcher (the hook's searcher pattern) in the test's 187 R2-R11 and review R2-R11 constants, the r11 hook
(`0d2401f07`, `7bba4822…`) denies 697 and this head 699. The two that move are review r11's own P2 rows
(`REVIEW_R11_BRACES`), from allowed to too complex. Over r12's 38 pinned commands, this head denies 20, 17 of them as
too complex, and the installed hook allows every one of the hub's rows and edges. S1: the installed hook denies 119 of
779 commands and this head 313, as at r11 (H1).

**Holds** (one pytest or driver each, `-k` selections of `test_guard_subfleet_root.py` unless named, home
`/Users/tester`, all at `44995a5d1`). r12c started at 02:35Z on 10-31 and queued its first hold at 02:51Z; recent holds
had waited 12-30 minutes for the lock. H4's and H3's waiters (queued 03:24:50Z and 03:34:16Z) were not admitted within
30 minutes: the lock admits whichever waiter polls first, and other holds, among them this job's H2b, H5b, MB3, HAB and
MB2, took the slots that came free. So each was stopped with no test run under it, and its selection ran directly,
outside the lock (the lane rules' fallback). H5a's first try (03:38:35Z, slot B) ran no test, because its `-k` list held
parametrized ids, which pytest refuses (exit 4). It was queued again with the tests' names. Every other hold ran under
the lock.

| Hold | Admitted (slot, load) | Selection | Result, time |
| --- | --- | --- | --- |
| H1 | 03:08:11Z (A, 33.0) | S1 | 1 passed, 174 s |
| H2a | 03:20:56Z (B, 44.1) | the review's inputs, r2's tests, TRUST's and the fence | 16 passed, 53 s |
| H2b | 03:29:58Z (A, 41.5) | S1's second test, S2, S3, the cwd list, the known limits, S5, S4 | 8 passed, 2 skipped (S4), 250 s |
| H3 | outside the lock, 04:05:12Z (load 37.8) | r7's to r12's tests, reviews r7 to r11 | 38 passed, 188 s |
| H4 | outside the lock, 03:57:45Z (load 35.4) | r4's to r6's tests, reviews r4 to r6 | 40 passed, 125 s |
| H5a | 04:16:14Z (A, 26.7) | r3's tests | 28 passed, 246 s |
| H5b | 03:52:09Z (A, 40.6) | r3b's and r3c's tests | 26 passed, 425 s |
| HAB | 03:59:42Z (B, 36.3) | `test_guard_overlay.py` (22), `test_guard_trust.py` (105) and the three guards (37, 1, 25) | 190 passed, 128 s |
| MB1-MB6 | 03:59:19Z to 04:36:34Z (A and B, load 32.7-43.3) | the 127 mutants | below |

H1 to H5b cover all 159 items of `test_guard_subfleet_root.py` exactly once (checked against `--collect-only`): 157
passed and S4's 2 skipped. The overlay and TRUST tests passed 127, and the three guards 63.

**Timing** (the 300-path command, child CPU in seconds, three runs of five interleaved pairs with the installed hook,
outside the lock, 03:45:30-03:45:45Z):

| Run | Load | This head | Installed | Median ratio |
| --- | --- | --- | --- | --- |
| 1 | 35.0 | 0.219-0.245 | 0.137-0.153 | 1.52× |
| 2 | 34.0 | 0.203-0.248 | 0.135-0.166 | 1.57× |
| 3 | 33.6 | 0.229-0.246 | 0.147-0.157 | 1.57× |

Both hooks allow the command. The 300-path test itself (`test_r4_absolute_paths_cost_at_most_twice_the_installed_hook`,
H4, load 37.9) gave a median of 0.227 s against 0.153 s, **1.48×**. The cost pins are decisions 1's and 2's above.

**Mutants** (`tools/guard_subfleet_root_mutations.py`, one `git archive` of HEAD each, the named test and the fence,
all under the lock; each batch's control passed every named test and the fence):

| Mutants | Killing test | Batch |
| --- | --- | --- |
| `rv11-key-ctx-dropped`, `rv11-key-cd-dropped` | `test_review_r11_pins_r11s_unpinned_readings` | MB1 |
| `r12-word-charge-dropped`, `r12-byte-charge-dropped`, `r12-execs-uncharged` | `test_r12_find_exec_braces_cost_steps_of_the_shared_budget` | MB1 |
| `r12-eval-door-uncharged`, `r12-shell-door-uncharged`, `r12-source-door-uncharged`, `r12-pfmt-uncharged`, `r12-message-unnamed` | `test_r12_printf_repeats_and_piped_text_cost_steps_of_the_shared_budget` | MB1 |
| `r11-message-loop-unnamed` (retargeted), `r11-word-charge-dropped`, `r11-byte-charge-dropped` | `test_r11_loop_variable_words_cost_steps_of_the_shared_budget` | MB1 |
| `r3c-eval-unwrap-dropped` (retargeted) | `test_r4_every_shell_and_eval_script_is_judged` | MB5 |
| review r10's two and r11's other seven | each one's named test | MB1 |

| Batch (admitted, slot, load) | Mutants | Result |
| --- | --- | --- |
| MB1 (04:20:39Z, A, 32.7) | review r11's two, r12's eight, review r10's two, r11's ten | 22 killed |
| MB2 (04:01:57Z, B, 34.3) | r10's two, review r9's, review r8's three, r9's two, review r7's, r8's ten, r7's first three | 22 killed |
| MB3 (03:59:19Z, A, 37.3) | review r6's, r7's other three, review r5's two, r6's twelve, r5's first four | 22 killed |
| MB4 (04:36:34Z, A, 43.3) | r5's other eight, review r4's four, r4's nine, r3c's first | 22 killed |
| MB5 (04:09:57Z, B, 36.6) | r3c's other eleven, r3b's eleven | 21 killed, 1 survived |
| MB6 (04:10:39Z, A, 37.9) | r3's ten, r1's four, the two TRUST mutants, r2's | 17 killed |

The tests kill 126 of the 127. `stall-mount-listed` (r3b) survives, as it has since r3c, where it became
equivalent; r12 doesn't touch its code. The fence fails, as designed, on the six mutants whose edit is a line the fence
itself undoes (r1's and r2's five root edits and `r6-span-backtick-dropped`), and passes on the other 121 and on every
control. A kill counts only the named test.

**Unrun at this head:**
- S4's two items, which need the operator's Claude hook, so they skip here;
- `tests/frontend/test_core_live.py`, which can't run on a lane. CI runs both.
- This round made no GitHub calls, so it read no CI.

**Disclosures.**
- Outside the lock: H3 and H4 (above), and all probes: the hub's rows through this head, r11 and the installed hook;
  the scratch copies (the trial's shell door, each door without its charge, the `source` trace); the PR body's
  examples; the recount; and the timing rows.
- MB4 held the lock for 21 min 48 s (04:36:34Z-04:58:22Z), past the lane rules' 20 minutes: its last mutant finished at
  about 04:56:30Z, and the driver's removal of its exports ran in disk wait until 04:58:22Z. Every other hold stayed
  under 20 minutes; MB1, at 16 min, was the next longest.
- The tool's 600 s limit moved six Bash calls to its background: H1, H2a, the three chains of holds and H5a's second
  try. Each was polled to completion in the foreground, and no process of mine outlived its run.

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
  - shell structure: a for-loop variable used inside a word or after a `cd` (`for d in
    "$HOME/.subfleet"; do find $d/x …; done` passes; r6 decision 9 took the whole-operand form,
    quoted list or not, and `R6_CLOSED_LIMITS` holds the quoted pair r2 listed);
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
   shasum -a 256 private/guard/never-rules-hook.sh private/guard/TRUST   # the reviewed pair (r12)
   # e5b387c4cf19f1bb6333ae9306e9657f5904a8b13f3d9c0f7d282d45b0a062d7  private/guard/never-rules-hook.sh
   # c91e21b4d571f0453ead3ed49a6ad8afe931d74617de5be38fd6948418a48fd7  private/guard/TRUST
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
   shasum -a 256 ~/.subfleet/guard/never-rules-hook.sh ~/.subfleet/guard/TRUST   # e5b387c4…62d7, c91e21b4…8fd7
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
