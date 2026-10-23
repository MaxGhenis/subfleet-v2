## The restore recipe's fence survives any path, and a test session keeps inherited credentials out of its reports (D-PRR1)

Full report: `docs/reports/2026-10-21-prune-restore-recipe.md`. Fix round 1 answers review r1's P1 and merges `release/217` at `e345f0803`; its section is at the end of the report. The PR's title still says no test failure can print a credential, which round 1 shows is too strong; this heading is the suggested replacement.

### Cause

**The flake.** `test_restore_recipe_preserves_backup_and_displaced_files` failed under `--hypothesis-seed=18`. It shrinks to `suffix='```'`, and it reproduces at `2eacd5806`. The harness cut the recipe at the first triple backtick after the opening fence, which was inside the quoted backup path, so `/bin/sh` ran a script that ended in an open quote.

**The product bug.** `RESTORE_RECIPE` opened its shell block with a fixed three-backtick fence. A path holding a mid-line run ends that block for any reader that looks for the next triple backtick. A path holding a newline and then a line of three backticks ends it for a CommonMark renderer too.

**The leak.** The failing test passed `env={**os.environ, …}` to `subprocess.run`. pytest 9.1.1's long traceback is the default for a test's failure, and it prints each entry's function arguments, so `subprocess.run`'s `kwargs`, the copied environment among them, reached the report.

- That `kwargs` line is cut to 240 characters and four dict entries, which shows the first variables of the inherited environment.
- `-l` prints the same frame's locals as well.
- `-l -vv` pretty-prints them in full: every variable.
- `--tb=short`, `--tb=native`, `CalledProcessError`'s text and Hypothesis's falsifying-example report did not print it.

This was established with a dummy `SECRET7prr1` in minimal environments, with output in scratch only.

**Round 1: the session's own store printed one (review r1, P1).** `test_c20_6_this_session_holds_no_inherited_credential` bound `config._subfleet_removed_credentials`, the session's store of removed variables: a `dict` of name to value. Review r1 gave a child session an inherited dummy and set a second after configure. The test's sanity check then failed, and `-l` printed the store's local, value included. The round's audit found two more stores of the same kind:

- **The baseline snapshot.** `config._subfleet_original_home_env` held the inherited values of `CLAUDE_LANE_AGENT_SECRET` and `SUBFLEET_AGENT_SECRET`, two credential-shaped names it restores.
- **A `Mapping` store.** A `Mapping` with a redacting `repr`, the brief's suggestion, would still print values: pytest 9.1.1 diffs a failing `==` between two `collections.abc.Mapping`s item by item, values included (`_pytest/assertion/_compare_mapping.py`).

### Fix

- `subfleet/prune_decisions.py`: the script is its own template, `RESTORE_SCRIPT`. `_restore_fix` fences it with `code_fence`: a backtick run one longer than any inside the block, and never shorter than three. The migration guide's copy holds no backtick, so it keeps three and is unchanged.
- `tests/markdown_fences.py` reads backtick fences by CommonMark 0.31's section 4.5. `_run_recipe` and `test_prune_217` take the block through it. `_run_recipe` asserts that the block is exactly the template's script with `shlex.quote(path)` in it, and builds its child's environment from nothing: `PATH`, the session's `HOME`, `TMPDIR`, `LANG`/`LC_ALL` when set, and `state_root`.
- `subfleet/home_paths.py` adds `CREDENTIAL_ENV_PATTERN`, beside `SAFE_ENV_REMOVALS`. It matches names ending in `_TOKEN`, `_KEY`, `_SECRET`, `_PASSWORD` or `_COOKIE`, or holding `CREDENTIAL` or `_AUTH`, ignoring case, and never the session's markers.
- `tests/conftest.py` removes every matching variable before collection and restores each at unconfigure. While the session lives, none is in the test process's environment or an inheriting child's. A test that needs one sets a dummy.
- **Round 1:**
  - The session check binds the removed names only: `set(request.config._subfleet_removed_credentials)`.
  - `home_paths.RemovedCredentials` keeps the removed variables. Its `repr` and `str` are `<RemovedCredentials names=[…], values withheld>`, with the names sorted. pytest's traceback arguments, `-l` locals (cut or whole) and assertion operands and diffs, and Hypothesis's report, all print it through that `repr`.
  - It is deliberately not a `Mapping`. `keys()` and `[name]` still serve `os.environ.update` and `dict()`, so `restore_credentials` is unchanged.
  - The baseline snapshot records a credential-shaped name as absent, and the store puts its value back after the snapshot's restore. Cleanup restores exactly what it did before.
- `tests/unit/test_live_store_guard_round3.py`: its two checks that `host_push` drops `SSH_AUTH_SOCK` under pytest now set the socket themselves. The session removes an inherited one, so otherwise they would pass without reaching the drop.
- Contract: C-11.5 (the recipe's fence) and C-20.6 (the session's credential removal). Round 1 rewrites C-20.6's guarantee and limit, drops the absolute claim from the first change-log entry, and adds a second entry.

### What is guaranteed, and the limit

- Credential-shaped variables are absent from the test process's environment for the session. Each is put back exactly at cleanup.
- The removed-credentials store shows names only, and the baseline snapshot holds no credential-shaped value.
- Tests bind names only. The report's audit lists every local in this PR's tests that holds a value; none holds an inherited one.
- **The limit:** a test that copies a value into a local by hand (`dict(store)`, `store[name]`) can still print it. So can a test that prints a variable set after configure.

### Invariants

1. **The block is whole.** For every backup path, the recipe's fenced block, read by CommonMark's rule, is exactly `RESTORE_SCRIPT` with `shlex.quote(path)` in it. Its fence is longer than every run inside and at least three, and the prose around it does not change.
2. **The restore still works.** For every drawn path and sidecar combination, the store equals the backup, the displaced files are byte-identical and together, and the backup is unchanged.
3. **No credential in test output.** With dummy credential-shaped variables set before the session, and one set after configure, a failing test's whole report holds no part of either. That covers the plain traceback, `-l` locals cut and whole, `--tb=long`, `-vv` and Hypothesis's report, including when the test holds the store or the snapshot. Each channel is checked to have printed.
4. **Restored after the session.** `pytest_unconfigure` puts every removed variable back exactly, an empty value included.
5. **The store's text holds names only** (round 1). For every mapping of credential-shaped names to dummy values, `repr` and `str` of the store contain no value; eleven printers render it from its names alone.

### Tests

- **Regressions,** each failing at `2eacd5806` (the build):
  - seed 18's `suffix="```"` as an `@example`, with three more `@example`s: four backticks, a mixed run, and a run on its own line. It fails with seed 18's `CalledProcessError`.
  - `test_restore_fix_fences_its_block_beyond_every_backtick_run_in_the_path`, on four paths with three- and four-backtick runs: `3 > 3`, `3 > 4`, and the block ending early.
  - `test_c20_6_a_failing_session_prints_no_part_of_an_inherited_credential`, a child pytest in a minimal environment: the dummies leak at base.
- **Regressions,** each failing at the round's tests-first commit `5cf14fffc`, which is `e824c6a0b` plus the tests:
  - `test_c20_6_the_session_check_failing_prints_no_part_of_a_dummy`: review r1's case, a child session with an inherited dummy and a second set after configure, under `-q -l --tb=long -vvv` and `-l --tb=long`. Both leaked.
  - `test_c20_6_a_test_holding_the_store_prints_no_part_of_a_dummy`: `tests/credential_store_child.py`'s six tests hold the store or the baseline snapshot and fail through a local, a helper's argument, captured output, an assertion, a dict comparison and a Hypothesis draw. Both options leaked, and each channel is checked to have printed.
- **Properties:** invariant 1 over paths with backtick runs of 0–6, quotes, `$`, every line ending and non-ASCII (200 examples); removal and restore over arbitrary environments (100); round 1's invariant 5 over names with dummy, empty and part-of-a-name values, through eleven printers, against a twin store with the same names (100).
- **Mutations,** each failing a named test (the report has every count):

| Mutation | Fails |
| --- | --- |
| a fixed three-backtick fence | the fence test, the invariant 1 property, `test_run_recipe_hands_the_shell_the_whole_block` and the restore property |
| the harness splitting at the first triple backtick | `test_run_recipe_hands_the_shell_the_whole_block` and the restore property |
| `_run_recipe` copying `os.environ` | `test_run_recipe_gives_the_shell_only_the_variables_it_needs` |
| the session's removal dropped | `test_c20_6_this_session_holds_no_inherited_credential` and the store-holding child test |
| unconfigure not restoring | `test_c20_6_unconfigure_puts_back_exactly_what_configure_removed` and the store-holding child test |
| round 1: the session check binding `dict(store)` | both options of the review-r1 child test |
| round 1: the store's `__repr__` showing values | the invariant 5 property and both store-holding options |
| round 1: the store a `collections.abc.Mapping` | both store-holding options, through pytest's mapping diff |
| round 1: the baseline snapshot keeping values | the store-holding child test under `-q -l --tb=long -vvv` |

The session check binding the store itself (no `dict`) is an intended survivor: the store's `repr` covers it.

**Round 1 runs** (local, serial, each through the hub's suite wrapper and under 20 minutes; inherited credential-shaped variables unset, and a `SECRET7…` dummy set for the session to remove, which no log holds):

- Group 1: `test_session_credential_env.py` 35, `test_prune_restore.py` 20, `test_prune_decisions.py` 48, `test_prune_decision_backup_verification.py` 23, `test_prune_decision_fingerprints.py` 15, `test_prune_217.py` 9: all passed. One channel check of the new test was too specific under `-l` alone and was corrected in `e2ded7d12`; its leak check had passed.
- Group 2: the 14 `test_live_store_guard*.py` files (850) and 8 `test_launch_env*.py` files (329): all passed.
- Group 3: `test_state_contract.py` 37 and `test_test_module_names.py` 1: passed.
- Review r1's 95 unrun files: `test_suggestion_secret_properties.py` 7 passed, `test_record_secrecy.py` 42 passed before the hold's alarm, and the rest left to CI; the report has the list.

The build's whole-suite inventory (8,630 passed in `tests/unit` and 452 in `tests/fake`, with three sandbox-only failures) and review r1's further 3,267 cases stand as reported. The report lists which of review r1's 95 unrun files this round ran.

### Merge

`release/217` at `e345f0803` (#212, #223, #232, #238 and #243) is merged in `973857899`. The one conflict was the contract's change log, where both sides added entries; all are kept, newest first. Release changed neither C-11.5 nor C-20.6, nor any file this PR changes.

Nothing was pushed by the build or by round 1.
