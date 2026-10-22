## The restore recipe's fence survives any path, and no test failure can print a credential (D-PRR1)

Full report: `docs/reports/2026-10-21-prune-restore-recipe.md`.

### Cause

**The flake.** `test_restore_recipe_preserves_backup_and_displaced_files` failed under `--hypothesis-seed=18`. It shrinks to `suffix='```'`, and it reproduces at `2eacd5806`. The harness cut the recipe at the first triple backtick after the opening fence, which was inside the quoted backup path, so `/bin/sh` ran a script that ended in an open quote.

**The product bug.** `RESTORE_RECIPE` opened its shell block with a fixed three-backtick fence. A path holding a mid-line run ends that block for any reader that looks for the next triple backtick. A path holding a newline and then a line of three backticks ends it for a CommonMark renderer too.

**The leak.** The failing test passed `env={**os.environ, …}` to `subprocess.run`. pytest 9.1.1's long traceback is the default for a test's failure, and it prints each entry's function arguments, so `subprocess.run`'s `kwargs`, the copied environment among them, reached the report.

- That `kwargs` line is cut to 240 characters and four dict entries, which shows the first variables of the inherited environment.
- `-l` prints the same frame's locals as well.
- `-l -vv` pretty-prints them in full: every variable.
- `--tb=short`, `--tb=native`, `CalledProcessError`'s text and Hypothesis's falsifying-example report did not print it.

This was established with a dummy `SECRET7prr1` in minimal environments, with output in scratch only.

### Fix

- `subfleet/prune_decisions.py`: the script is its own template, `RESTORE_SCRIPT`. `_restore_fix` fences it with `code_fence`: a backtick run one longer than any inside the block, and never shorter than three. The migration guide's copy holds no backtick, so it keeps three and is unchanged.
- `tests/markdown_fences.py` reads backtick fences by CommonMark 0.31's section 4.5. `_run_recipe` and `test_prune_217` take the block through it. `_run_recipe` asserts that the block is exactly the template's script with `shlex.quote(path)` in it, and builds its child's environment from nothing: `PATH`, the session's `HOME`, `TMPDIR`, `LANG`/`LC_ALL` when set, and `state_root`.
- `subfleet/home_paths.py` adds `CREDENTIAL_ENV_PATTERN`, beside `SAFE_ENV_REMOVALS`. It matches names ending in `_TOKEN`, `_KEY`, `_SECRET`, `_PASSWORD` or `_COOKIE`, or holding `CREDENTIAL` or `_AUTH`, ignoring case, and never the session's markers.
- `tests/conftest.py` removes every matching variable before collection and restores each at unconfigure.
- So no test, no inheriting child and no traceback can print a credential the session inherited. A test that needs one sets a dummy.
- `tests/unit/test_live_store_guard_round3.py`: its two checks that `host_push` drops `SSH_AUTH_SOCK` under pytest now set the socket themselves. The session removes an inherited one, so otherwise they would pass without reaching the drop.
- Contract: C-11.5 (the recipe's fence) and C-20.6 (the session's credential removal), with one change-log entry.

### Invariants

1. **The block is whole.** For every backup path, the recipe's fenced block, read by CommonMark's rule, is exactly `RESTORE_SCRIPT` with `shlex.quote(path)` in it. Its fence is longer than every run inside and at least three, and the prose around it does not change.
2. **The restore still works.** For every drawn path and sidecar combination, the store equals the backup, the displaced files are byte-identical and together, and the backup is unchanged.
3. **No credential in test output.** A session started with dummy credential-shaped variables fails through a frame's arguments, `-l` locals, captured output, an assertion and a Hypothesis example. Under `-l --tb=long`, with and without `-vv`, its whole report holds no part of any dummy.
4. **Restored after the session.** `pytest_unconfigure` puts every removed variable back exactly, an empty value included.

### Tests

- **Regressions,** each failing at `2eacd5806`:
- seed 18's `suffix="```"` as an `@example`, with three more `@example`s: four backticks, a mixed run, and a run on its own line. It fails with seed 18's `CalledProcessError`.
- `test_restore_fix_fences_its_block_beyond_every_backtick_run_in_the_path`, on four paths with three- and four-backtick runs: `3 > 3`, `3 > 4`, and the block ending early.
- `test_c20_6_a_failing_session_prints_no_part_of_an_inherited_credential`, a child pytest in a minimal environment: all 8 non-empty dummies leak at base.
- **Properties:** invariant 1 over paths with backtick runs of 0–6, quotes, `$`, every line ending and non-ASCII (200 examples); removal and restore over arbitrary environments (100).
- **Mutations,** each failing a named test:

| Mutation | Fails |
| --- | --- |
| a fixed three-backtick fence | the fence test, the invariant 1 property, `test_run_recipe_hands_the_shell_the_whole_block` and the restore property |
| the harness splitting at the first triple backtick | `test_run_recipe_hands_the_shell_the_whole_block` and the restore property |
| `_run_recipe` copying `os.environ` | `test_run_recipe_gives_the_shell_only_the_variables_it_needs` |
| the session's removal dropped | `test_c20_6_this_session_holds_no_inherited_credential` and the child-session test |
| unconfigure not restoring | `test_c20_6_unconfigure_puts_back_exactly_what_configure_removed` and the child-session test |

Completed results (local; full file inventory in the report):

| Directory | Passed | Failed | Skipped | Xfailed | Complete files | Partial files | Unrun files | Unrun cases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `tests/unit` | 8,630 | 3 | 10 | 0 | 222 | 1 | 103 | 4,582 |
| `tests/fake` | 452 | 0 | 0 | 2 | 17 | 1 | 88 | 1,518 |

Final targeted check: `test_prune_restore.py` **20 passed** and `test_session_credential_env.py` **30 passed**, with `--hypothesis-profile=ci --hypothesis-seed=18`. The requested related restore, launch-environment, live-store guard, state-contract and module-name files also have completed results in the report.

Whole-directory coverage remains incomplete at the brief's cutoff: **191 files unrun, 2 partial, 6,100 cases without completed results**. The report lists every file and its counts. Three failures remain in unchanged sandbox-dependent tests: `test_daemon_qos_tools.py` and `test_hooks.py` cannot execute `/bin/ps`; `test_descriptors.py` cannot obtain the kernel ceiling through `sysctl`.

The three baseline regressions and all five required mutations were detected by named tests; details and exact probe options are in the report. In the final environment-copy audit, 46 launch-copy sites remain in 35 files (74 raw pattern matches in 42 files, including non-launch snapshots). Inherited credential-shaped names are removed before collection and restored at cleanup.

Each broad hold stayed below 20 minutes of pytest runtime and used the hub wrapper. The last hold was submitted at 09:03:40 UTC, before the 09:04 UTC cutoff. No later holds were started; no remote was contacted and nothing was pushed.

The shared Git index is read-only in this sandbox. The report and PR-body commit uses the authorized workspace-local metadata fallback, `.prr1-git/`, on `fix/prune-restore-recipe-217`, with history descended from checkpoint `8c2e9de04`.
