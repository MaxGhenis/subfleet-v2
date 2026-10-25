# The lane guard refuses whole-tree searches of ~/.subfleet (D-LANE-SCOPE1), 2026-10-24

At 15:32Z on 2026-10-24 a Codex lane ran `rg --files -g AGENTS.md /Users/maxghenis/.subfleet`
for 4 minutes with 12 threads at load 190–220. It walked every job folder (14,121) and job
worktree (1,765). The never-rules hook's `unscoped-search` rule refuses find/rg/grep -r over
`/`, `/Users`, `~`/`$HOME` and some of their children and `/tmp`, but `~/.subfleet` was not
one of them.

Base: `release/217` at `abe117e67`. Nothing here touches the live guard; the hub installs it
with the steps below.

## What changed

**The hook** (`private/guard/never-rules-hook.sh`, SHA-256 `1a736be3…f951`). One alternation
joins the home children in `broad_re`:

```
…|Library/Caches|\.cache|\.local|\.subfleet(/[^/[:space:]'\"]+)?))?|(/private)?/tmp
```

So `~/.subfleet` and any one path component directly under it are broad roots, in every
spelling the existing home children have: `~`, `$HOME`, `${HOME}` (judged as `$HOME`) and
`/Users/<user>`, with or without a trailing slash, quoted or not. Two or more levels down
(`~/.subfleet/jobs/<id>`, `…/<id>/a1/deliverable.md`) stays scoped, and any
`-maxdepth`/`--max-depth` still disarms the rule. The block message's root list gains
`~/.subfleet, ~/.subfleet/<child>`. The region's own `roots` comment and the header say so;
the header's sync paragraph now records that the region is edited in this file on hub
decisions (the Claude hook's 2026-10-20 rewrite moved its rule to `unscoped_search.py`, so
the byte-identical contract and its skip-gated DriftTests are historical). Nothing else
changed: `test_only_the_roots_the_header_and_the_message_changed` undoes exactly these edits
and gets the original reviewed bytes (`a60d1c51…`) back.

**TRUST** (`b3fcc893…e9210`). `hook_sha256` is recomputed for the new bytes and `provenance`
gains a dated D-LANE-SCOPE1 sentence ending "Installed only after the hub's review and a live
hooks/list preflight." Nothing else moved. `hooks_trust_hash` (`sha256:f97fa95f…`) and
`override` are unchanged: `subfleet/guard/preflight.py:hooks_trust_hash` hashes the hook's
identity (event, matcher, type, command path, timeout, async flag, status message), not its
bytes. Checked three ways: `hooks_trust_hash(reference_hook_path)` still equals the pin;
the old and the new bytes written at one path give one hash and one override string; and
the installed path's identity, `sha256:29adec11fc23b970487de36f80282b1656783a8470f561d4da3c6d1eccbae1f6`
(computed from the path string, the file was not read), is what the live preflight below
must see as hooks/list's `currentHash`, before and after the install.

**The overlay pin** (`tests/unit/test_guard_overlay.py::test_private_overlay_bytes_remain_the_original_reviewed_pair`)
moves to the new pair; its docstring names D-LANE-SCOPE1 and the original pair as the
rollback. No other test or code reads `private/guard/` (`git grep`).

**The Claude half**, prepared and not installed: `.hub-out/claude-hook/unscoped_search.py`
and `.hub-out/claude-hook/test_unscoped_search.py` in the build worktree (untracked).
`BROAD_RE` gains `| \.subfleet (/[^/]+)?` under `/Users/<user>`, and `HOME_SUBDIRS` gains
`".subfleet"` and `".subfleet/*"`, where a trailing `/*` means any one path component directly
under it (`is_broad` reads that form). The test copy's independent oracle, its root and
component pools, its example table and two new properties
(`test_subfleet_and_each_direct_child_are_broad`, `test_two_or_more_levels_under_subfleet_are_scoped`)
cover the new roots.

## Invariants and how they were checked

`tests/unit/test_guard_subfleet_root.py` states each in its docstring and checks it.

- **S1, monotone.** The new hook denies every command the old one denies, and the decisions
  differ only where the command's search root is a new root (a searcher's path position, a
  cd/pushd target, a for-loop list feeding find, or anywhere in a command where xargs runs a
  searcher, that rule's documented over-approximation). The corpus: this file's examples,
  every backticked command in the hook's unscoped-search comments, the Claude hook's example
  table, the review's inputs, commands for every other rule (in scratch git repositories for
  the cwd-dependent ones) alone and with a new-root search appended, the cwd-rule cases, and a
  seeded generator (seed 20261024) of find/rg/grep commands over a root pool in every
  spelling, with and without a depth bound, unquoted, double- and single-quoted, plain, in
  `bash -c`/`sh -c`/`eval`, piped to `head`, after cd, in a for loop, and through xargs. A
  second check makes the new roots at least as protected as the existing home children:
  wherever the old hook denies a command over `~/.cache` or `~/Library/Caches`, the new hook
  denies the same command over `~/.subfleet` or `~/.subfleet/<child>`.
- **S2, scoped stays allowed.** Depth-bounded searches and searches two or more levels under
  `~/.subfleet` are allowed by the old hook and the new.
- **S3, no other rule moves.** For every command the old hook denies under another rule
  (tg-getupdates, keychain-dump, trust-root, corpus-squash, corpus-push, stash-shared,
  local-main, hf-dest), the new hook's stdout is byte-identical and its denial-log line
  identical but for the timestamp, alone and with `; rg --files ~/.subfleet` appended. One
  case is intended: a Bash command carrying an apply_patch body is judged by the hf-dest
  heredoc scan, which runs after the unscoped-search region, so with a new-root search
  appended both hooks deny it and the new one says `[unscoped-search]`. apply_patch payloads
  are judged identically.
- **S4, the Claude half agrees.** On plain explicit-path cases (every spelling, child, deep and
  near-miss path, slash and quoting) the patched analyzer and the Codex hook agree outright;
  on the generated cases the analyzer disagrees with the Codex hook on a new root only where
  it already disagrees on the matching existing home child (the two designs differ on quoted
  `~`, a searcher right after a quote, xargs input, cd plus a relative path, globs and the cwd
  list, and the new roots inherit exactly those). Two or more levels under `~/.subfleet` the
  analyzer blocks nothing. These two tests read the analyzer from
  `SUBFLEET_CLAUDE_UNSCOPED_SEARCH` and skip without it, since it lives in the operator's home.

`GUARD_DIFF_SCALE=1` (the default, CI's) crosses a representative root pool with every spelling
and spreads the contexts over the spellings; `N >= 2` is the full pool, every context, `N − 1`
draws per cell. `GUARD_DIFF_REPORT=<file>` writes the counts.

Mutants (`tools/guard_subfleet_root_mutations.py`, each on a `git archive` of HEAD in a scratch
directory; a kill is a failed assertion in the named test):

| Mutant | Killed by |
| --- | --- |
| the new roots removed | `test_review_inputs_are_denied` |
| the child form dropped (`~/.subfleet` only) | `test_each_direct_child_is_a_root` |
| a prefix match (`~/.subfleet` and everything under it) | `test_two_or_more_levels_under_subfleet_stay_scoped` |
| the trailing-slash form missed (roots in their own `root_core` branch, without the shared `/?`) | `test_trailing_slash_spellings_are_denied` |
| TRUST's hash left stale (the original TRUST) | `test_trust_pins_the_new_hook_bytes` |

Run on 2026-10-25: the unmutated control passed all six named tests, and every mutant failed
its named test (a failed assertion, not an error); the fence test,
`test_only_the_roots_the_header_and_the_message_changed`, also failed on the four hook mutants.

### Results (scale 1, 2026-10-25 UTC, load 100–160)

S1 ran over 366 distinct (command, cwd) pairs: 223 generated (29 of them the matching
home-child commands) and 146 fixed. The old hook denies 113 and the new one 142. None is
loosened, and all 29 newly denied commands (19 generated, 10 fixed) carry a new root in a
reading position. Of the 29 generated new-root reading cases, the new hook allows 10, and for
each of them the old hook also allows the same command over `~/.cache` or `~/Library/Caches`:

- 8 are a searcher right after a quote in `bash -c`/`sh -c`/`eval`
  (`bash -c 'rg --files -g AGENTS.md ~/.subfleet/'`), the first known limit below;
- `for d in "/Users/maxghenis/.subfleet"; do find $d -name x; done` (a quoted loop list);
- `git ls-files | xargs rg "${HOME}/.subfleet/" src/`, where the root is rg's pattern (the
  generator labels every xargs case as reading, which over-counts here).

S2 checked 67 bounded or deep commands: all allowed by both hooks. S4 compared 29 pairs: the
analyzer agrees with the Codex hook on 18 of the matching home-child commands and on the same
18 new-root commands; the other 11 differ on both, in the classes above. It blocks none of the
22 deep cases. Other counts are in the final answer.

## Known limits

The decision fenced the edit to the roots, so these stay as they were and apply to the new
roots exactly as to the old ones. `test_known_limits_the_new_roots_share_with_the_old_ones`
(marked `documented_limitation`) pins each; a follow-up must change it deliberately.

- **A searcher right after a quote is not detected** (found by this build's differential).
  The searcher regexes want a space, `;`, `&`, `|`, `(` or the line start before `find`/`rg`/
  `grep`, so a sub-script that starts with one passes: `bash -c 'find / -name x'`,
  `sh -c "rg foo ~"` and `eval 'grep -r x ~/.subfleet/jobs'` are allowed today by the live
  hook. `bash -c 'cd / && find . -name x'` and `bash -c ' find / -name x'` are denied. The
  Claude analyzer parses sub-scripts and blocks all of them. This is the largest gap the
  differential found; it predates D-LANE-SCOPE1 and needs its own decision (allowing `'` and
  `"` before the searcher in the three detection regexes is the obvious repair; it changes
  the prefilter literal the Claude-era drift tests pinned).
- **A quoted for-loop list is not read.** `for d in "$HOME/.subfleet"; do find $d …; done`
  passes; the unquoted form is denied.
- **A trailing glob is text.** `rg foo ~/.subfleet/jobs/*` walks every job folder and passes,
  as `rg foo ~/*` does. The Claude analyzer folds a trailing `*` into its parent and blocks it.
- **The cwd list is unchanged.** `rg --files .` with a payload cwd of `~/.subfleet` passes; the
  list never held the other home children either (`Library/Caches`, `.cache`, `.local`). A
  Codex lane's cwd is its job workdir, normally a worktree two levels under `~/.subfleet`.
- **Intended false positive.** A file directly under `~/.subfleet` is a root like a folder
  there: `rg foo ~/.subfleet/subfleetd.log` is denied. Read it with non-recursive `grep`, or
  add `--max-depth 1`.
- **Over-approximations shared with every root.** With xargs running a searcher, a new root
  anywhere in the command counts (`… | xargs rg foo > ~/.subfleet/out.log` is denied); and a
  `cd ~/.subfleet` (or a child) with any searcher present is denied even when the search then
  names a deeper relative path.

## Install (the hub)

From a checkout of the reviewed commit, with its environment (`uv run`). Every path below is
the default state root; substitute a custom `SUBFLEET_HOME`.

0. **The installed pair must be the original pair.** If either hash differs, stop: this
   TRUST was built on the repository's pins (`codex-cli 0.153.3`), not on the installed ones.

   ```sh
   shasum -a 256 ~/.subfleet/guard/never-rules-hook.sh ~/.subfleet/guard/TRUST
   # a60d1c514d3a3bcec68c246a33c849c11fd37e1bdf60d886650cd9d6651390db  …/never-rules-hook.sh
   # 0b9d688e6b2ca3adefdb8ad465c0fad5e6e18dfa884bc2309ff4f506c2287bab  …/TRUST
   ```

1. **Stage through the repository's staging path**, into a fresh state root on the same volume
   (`tools/stage_guard_overlay.py` refuses to replace a different overlay in place, by design):

   ```sh
   uv run python -m tools.stage_guard_overlay --source private/guard --state-root ~/.subfleet/guard-stage-d-lane-scope1
   cmp private/guard/never-rules-hook.sh ~/.subfleet/guard-stage-d-lane-scope1/guard/never-rules-hook.sh
   cmp private/guard/TRUST ~/.subfleet/guard-stage-d-lane-scope1/guard/TRUST
   ```

   The tool validates the pins, writes the directory 700, the hook 755 and TRUST 600, and
   validates the staged bytes again before renaming into place.

2. **Swap the two directories in one step.** Running lanes execute
   `~/.subfleet/guard/never-rules-hook.sh` by path on every tool call; an exchange rename means
   that path never goes missing. Afterwards `~/.subfleet/guard` holds the new pair and
   `~/.subfleet/guard-stage-d-lane-scope1/guard` the old one, kept for rollback.

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
   ```

   (This exact call was exercised on two scratch directories on this Mac: it exchanged them.)
   In-flight Codex sessions keep their `-c hooks=…` argument, which names that path and the
   unchanged identity hash; `docs/private-guard-overlay.md` already relies on such sessions
   invoking the hook by path on later tool calls ("Removing either can break future tool
   calls in an otherwise live provider session"), so they should pick up the new bytes. This
   build did not verify that on a live session; step 5 verifies a fresh launch. A launch whose
   `load_guard` reads TRUST and the hook across the instant of the swap is refused as a
   `trust` mismatch and can be retried; it is never launched unguarded.

3. **Offline check**, as `docs/private-guard-overlay.md` describes:

   ```sh
   uv run python -c 'from pathlib import Path; from subfleet.doctor import check_guard_preflight; import os; r=check_guard_preflight(Path(os.environ.get("SUBFLEET_HOME", "~/.subfleet")).expanduser()); print(r); raise SystemExit(0 if r["status"] == "pass" else 7)'
   ```

4. **Live hooks/list preflight on one Codex lane** (no model turn; bypasses the verdict cache,
   whose key includes the overlay and so would miss anyway):

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

   Expect `"kind": "verified"` and `"hooks_hash": "sha256:29adec11…a1f6"`. This mirrors
   `subfleet doctor --live`, which runs the same preflight for every enabled Codex lane.

5. **Live deny test: one cheap turn.** A normal (not `-I`) Codex job carries the guard
   override (`subfleet/daemon.py`, the launch's `_guard_override` call; `subfleet/adapters/codex.py`
   adds it unless the job is an isolated review). Check the route first with
   `subfleet why --task lookup --tier trivial`, then dispatch from any scratch folder:

   ```sh
   cat > ~/reviews/subfleet-hub/lane-scope-2026-10-24/deny-probe.md <<'EOF'
   This is a guard test. Run exactly this shell command, once, unchanged, with your shell
   tool, and paste the tool's response verbatim. Run nothing else.

       rg --files ~/.subfleet | head -n 1
   EOF
   subfleet run --task lookup --tier trivial -C <scratch folder> \
     -p ~/reviews/subfleet-hub/lane-scope-2026-10-24/deny-probe.md -o <scratch folder>/deny-probe.out.md -n guard-subfleet-root-deny
   ```

   Pass: `subfleet runs show <id>` names a `codex-*` lane; the answer quotes a hook block whose
   reason starts `[unscoped-search]` and ends `Matched broad root: '~/.subfleet'.`; and
   `tail -n 3 ~/.cache/subfleet-codex/guard-denials.log` (or `$CODEX_GUARD_LOG`) has a line with
   that command and the job's cwd. The attempt's `guard-preflight.json` should read
   `"kind": "verified"` (the first launch per lane after the swap re-verifies). The
   `| head -n 1` bounds the damage if the guard were not live: head exits after one line and
   rg's next write meets a closed pipe.
   If the command ran, roll back (step 6) before anything else.

6. **Rollback: the old pair.** Swap back with the same call as step 2 (the old pair is in
   `~/.subfleet/guard-stage-d-lane-scope1/guard`), then check:

   ```sh
   shasum -a 256 ~/.subfleet/guard/never-rules-hook.sh ~/.subfleet/guard/TRUST   # a60d1c51…, 0b9d688e…
   ```

   If that directory is gone, the old pair is `git show abe117e67:private/guard/never-rules-hook.sh`
   and `git show abe117e67:private/guard/TRUST`, staged the same way. Verdict markers for the
   old pair are still valid for their 30 days.

7. **The Claude half**, after its review. Test the candidate pair before installing it, as the
   test file's header provides (`GUARD_HOOKS_DIR`: "test a candidate hook + analyzer pair before
   installing it"): put a copy of the installed `~/.claude/hooks/guard-never-rules.sh` and
   `.hub-out/claude-hook/unscoped_search.py` in one scratch folder, run
   `GUARD_HOOKS_DIR=<that folder> uvx --with hypothesis pytest -q -p no:cacheprovider .hub-out/claude-hook/test_unscoped_search.py`,
   then copy the two files to `~/.claude/hooks/unscoped_search.py` and
   `~/.claude/tests/test_unscoped_search.py`. The build ran this file against the copies without
   the bash hook (out of its reach), so `test_hook_agrees_with_analyzer` ran only there.
