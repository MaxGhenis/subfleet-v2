# Retention tree errors: D-SURVEY-EACCES1, D-RET-STALE-LOCK1, D-TMPDIR-ACL1

Two sessions built this. Both worked on a detached HEAD, because git refuses to check out `fix/retention-tree-errors-217` here while the caller's checkout has it. Subfleet keeps the commits under `refs/subfleet-salvage/`.

- **Session 1 (rte-build-r1), 2026-11-13 18:29–21:20 UTC.** It made the three fixes and their tests, and stopped at its 3-hour limit.
  - e89f202a9: walk maps the lstat refusal (change 1).
  - 96ad826ad: the bundle step removes git's lock (change 2).
  - 5169107e0: TMPDIR removal clears a blocking ACL (change 3).
  - 0f846b74f and a8583bee0: tests.
  - d767b2a8b: the first report.
- **Session 2 (rte-build-r2), 2026-11-14 23:40 UTC to 2026-11-15 02:22 UTC.**
  - 342d95001: merged release/217.
  - 4d97ecf2c: the hub's bundle-lock age ruling.
  - a0e02ffef: the change 4 test.
  - c0329e281: the contract amendments.
  - It also ran the mutants and the tests below, and wrote this report.

## Status

| Item | State |
|---|---|
| Merge of release/217 | Done (342d95001), one conflict resolved |
| Change 1: walk maps every permission refusal | Done; tests pass; mutant killed |
| Change 2: bundle lock, with the 10-minute ruling | Done; all four leftover cases and both sides of the line pass; 3 mutants killed |
| Change 3: TMPDIR removal clears a blocking ACL | Done; the ACL file passes (17 tests, property test included). `test_attempt_tmpdir_leaks.py` only partly ran |
| Change 4: Reclaim against a deny-delete file | Tested. The prediction holds, so Reclaim is unchanged |
| Contract (C-8.4, C-12.10) | Done; index and contract tests pass |
| Mutants (6 from the brief plus 2 for the age rule) | All 8 killed |
| Module suites | Partial; the host's load capped most runs (below) |

## The merge

`git merge --no-ff origin/release/217`. The hub named 85769b826, but the fetched release/217 had moved on to 82d3dd819 (#294, #298, #304), which contains it. I merged the current tip so the PR is mergeable against today's base, and say so in the merge commit.

- **One conflict, in `subfleet/retention_survey.py`.** release/217 moved the survey's per-job loop into `job_info()`, which `retention_delete`'s gate 7 now also calls. I took release/217's loop and moved this branch's job-directory walk error into `job_info`, at its end, with `setdefault`.
- **The ordering.** The job is still listed with `walk: job: <error>` when nothing else keeps it. A class issue (`remote-less-history`, `nested-linked-worktree`) still reads first, so gate 7's class check is unchanged. Placed before `_preflight`, as on the old branch, the walk error would have hidden `remote-less-history` (which `_preflight` sets with `setdefault`) and refused deletes gate 7 allows today.
- **Other files.** `sample()`'s walk-error lines merged cleanly, and so did `docs/acceptance-contract.md`. The contract's duplicate-id grep printed nothing.

## Change 1: walk maps every permission refusal (D-SURVEY-EACCES1)

**Cause.** `retention_fs.walk` opens and lists a directory, which needs only read permission. It then lstats each entry (`os.stat(name, dir_fd=fd, follow_symlinks=False)`), which needs search permission on the parent. A 0644 directory raised a bare `PermissionError`.

**Fix (e89f202a9).** In walk's per-entry `try`, `except PermissionError` raises `TreeError("unreadable", f"{path}: {exc.strerror}")`, as `open_dir` and `_list` already do. The docstring says so.

**Callers of `walk` and `tree_bytes`.** I checked these against the code in session 2.

- **Survey (`retention_survey.job_info` after the merge, via `_walk_sizes`).** The worktree's walk error was already the job's `issue`. The job directory's error now is too, last.
- **`sample`.** Walk errors for the worktree, job and admin trees set the job's `issue`, so the job lands in `kept_in_sample` and isn't counted as retirable.
- **`retention_archive._Builder._walk` (the manifest walk).** It maps `TreeError` to `Defer(reason, …, f"{label}: {detail}")`, for example `unreadable: worktree: blob/inner.bin: Permission denied`.
- **`Retirement._final_check` and `_late_admin`.** Both catch `TreeError`.
- **`retention_git.admin_objects`, via `_Builder._git`.** Before this change, an admin directory listed but not searched raised an `OSError`, which the pass catches. Now it raises `TreeError`, which the pass doesn't catch. `_git` maps it to `Defer(reason, …, "admin: …")` (test: `test_an_admin_directory_walk_refusal_is_a_deferral_not_an_escape`).
- **`AttemptTemp.size` and `retention_fs.record_complete`.** Both catch `(OSError, TreeError)`. No change.
- **`tree_bytes` and salvage.** `tree_bytes` has no callers in `subfleet/`, and `subfleet/salvage.py` doesn't call `walk`.

**Modes.** Nothing changes a job tree's modes. The deferral test asserts the 0644 directory is still 0644.

**Measuring.** `retention._size` uses `os.walk`, which also hits the refusal. Such a job counts as unmeasurable and is protected. The deferral test passes `measure_s=0` so the pass reaches the manifest walk.

## Change 2: the bundle step clears git's leftover lock (D-RET-STALE-LOCK1)

**Fix.** Before `create_bundle`, `_Builder._git` calls `_clear_bundle_leftovers(temporary)`.

- **Without a lock,** it removes `commits.bundle.tmp` as before.
- **With a lock** (from `lstat`), the age is `_wall_now() - st_mtime`.

**The ruling as built (4d97ecf2c).**

- **The constant.** `_Builder.BUNDLE_LOCK_STALE_S = 600` sits beside the method, with a comment giving the reason: a running git keeps the lock's time fresh.
- **A stale lock** (age ≥ 600 s) is unlinked; unlink never follows a link. A progress note records `{"k": "bundle-lock", "size", "age_s", "removed_at"}`.
- **A younger lock** raises `Defer("bundle lock in use", DEFER_BUSY_S, "commits.bundle.tmp.lock is N s old, under 600 s (B bytes): a git may still be writing it")` before anything is removed. A lock dated in the future counts as young.
  - The rollback keeps the cache, because the reason isn't in `_IN_USE_AGAIN`. So the lock and `commits.bundle.tmp` keep their bytes, mode and time.
  - No note is written in this case.
- **Logging.** Either way, one warning names the job, the lock's size and age, and whether the lock was kept or removed, for example `Retention keeps the bundle lock of job-y: 254 bytes, 599 s old (removed at 600 s)`.
- **The clock.** Wall time comes through `rarch._wall_now()`, which tests fix.

**Why the first case timed out at 280 s.** It wasn't the fixture: `World` is a two-file repository. Run alone in session 2, `[neither]` passed in 45.6 s using 2.9 s of CPU, and the whole file used 5.5 s of CPU over 7.6 minutes of wall time. The wait is the host: load averages ran from about 105 to 183 during this session. No fixture change was needed.

**Lease proof (session 1, at b51f08f96).** I reread the method and its call in session 2 but didn't recheck every line number after the merge.

1. `<state>/retention/<job>/archive/` is `Retirement.building`. Only `_Builder` writes content there, and only `Retirement.archive` constructs `_Builder`.
2. Only `_Pass.run` calls `archive`, for retirements from `_start`, which takes `retire:<job>` and the worktree fence for `retention:<job>` in one transaction, or from `_recover`, which rolls back a journal whose lease is missing.
3. `rollback`, `drop_cache`, `_reclaim` and `publish` also touch the folder, all in the same single-threaded pass. The daemon runs `_retention` under `_unrun_lock`.
4. Nothing else writes there. The daemon only `lexists` the folder, the CLI's `archives` and `restore` read published archives, and `survey` and `sample` only walk.
5. `rgit.run` starts git in its own session and, on a timeout or a cancel, kills and reaps the group. A hard-killed daemon can leave a git child running; the hub's ruling covers it with the 10-minute rule.

## Change 3: TMPDIR removal clears an extended ACL that blocks it (D-TMPDIR-ACL1)

All of this is in `subfleet/attempt_tmp.py`, with the ACL helpers in `retention_fs.py` (5169107e0). I reread it in session 2.

**`_delete(op, parent, name, parent_inside=…)`** wraps every unlink and rmdir in `_empty`, and the tombstone's own rmdir (`parent_inside=False`, so the temp root is never touched). On EACCES or EPERM it calls `_clear_blocking_acls` and retries once only if something was cleared; otherwise the refusal stands. It clears:

- **the entry's own ACL** (deny `delete`), only when the entry is this user's and, unless it is a directory, has `st_nlink == 1`. Session 1 added the link condition: another link could be named outside the allocation;
- **the parent directory's ACL** (deny `delete_child`), only when the parent is inside the tombstone, through the descriptor `_empty` holds.

**How `_clear_entry_acl` touches an entry.**

- It opens without following a link: `O_DIR`, `O_SYMLINK` for a symlink itself, or `O_NOFOLLOW` for regular files and FIFOs.
- Through the descriptor, it checks device, inode, uid, type and link count against the lstat before it clears.
- An entry no open admits is first set by name to exactly 0600 (0700 for a directory), never through a link, as `_empty` already does for directories.
- Sockets and devices keep their ACL.

**Mechanism: ctypes into libSystem** (`acl_init(0)` with `acl_set_fd_np(fd, acl, ACL_TYPE_EXTENDED)`; detection with `acl_get_fd_np` and `acl_get_entry`). I chose it over `/bin/chmod -h -N` because:

- it acts on a checked descriptor, so no path is resolved and no link is followed;
- it needs no subprocess per entry (the live folder has 14,718 entries);
- its errno is visible.

**Logging.** `sweep_unknown` passes the folder's name, so a sighting's line names it instead of `None`. The removal-deferral lines and the sweep line name the errno through `_why(type(exc).__name__, exc.errno)`, for example `PermissionError(EACCES)`, and never the refusal's words. TreeError codes are still logged only as `TREE_CODES` literals. Only `test_rte_tmpdir_acl.py` and `test_attempt_tmpdir_leaks.py` (lines 236, 712, 796, 848, 976, 1116) compare these lines. All six leaks pins match the new format; line 796 now expects `PermissionError(EPERM)`.

**Not covered.** A deny-`delete` ACL on the allocation folder itself refuses the rename to the tombstone, before any of this runs. That case still defers, as the brief scopes the rule to inside the tombstone.

## Change 4: Reclaim against a deny-delete file

The tests are in `tests/unit/test_rte_reclaim_acl.py` (a0e02ffef). What Reclaim does today:

1. **The file's unlink is refused** (EACCES).
2. **The set-aside rename of the file itself is refused too:** macOS checks delete on a rename's source. The file stays where it is, with `kept` entries `unlink-failed`, `in place`.
3. **Its directory goes aside whole.** The directory's `rmdir` fails (not empty), and the directory, which has no ACL, is renamed into `conflicts/<label>/`. The file and its ACL go with it. At the root, `run` sets the remainder aside the same way.
4. **`Reclaim.run` returns True.** End to end, the job is retired and reclaimed, the file and its ACL sit in `retention-conflicts/<job>/worktree/…`, and a `retention.conflict` event lists `notes/locked.txt` as kept.
5. **The archive's copy carries no ACL** (the test checks every stored file). So clearing an ACL before an unlink would lose nothing the archive keeps.

The prediction holds: Reclaim finishes. The brief's condition for changing Reclaim ("if Reclaim can't finish") isn't met, so Reclaim is unchanged and the tests stay.

The file's bytes stay in `retention-conflicts/` until someone removes them. Two side effects, both existing behaviour that I left alone:

- `_empty`'s three rounds report each refused file three times in `kept` and six times in `errors`;
- a failed set-aside leaves an empty `conflicts/<label>/<dir>` behind.

## Invariants and their tests

All of these pass on this head.

1. **walk raises no bare permission error.**
   - `test_walk_finishes_or_raises_tree_error_never_permission_error`, a Hypothesis test over generated trees: random depth, modes 0000–0777, symlinks inside, outside and dangling, and deny-delete and delete_child ACLs.
   - `test_walk_maps_the_lstat_refused_in_a_listed_but_unsearchable_directory` and `test_walk_still_maps_an_unopenable_and_an_unlistable_directory`.
2. **Removal is all or nothing inside, and nothing outside.**
   - `test_removal_is_all_or_nothing_inside_and_changes_nothing_outside`, a Hypothesis test that also pins an immutable file for the False branch. It passed in 59 s; session 1 never reran it after its harness fixes.
   - `test_a_symlink_whose_own_acl_denies_delete_goes_and_its_outside_target_keeps_its_acl` and `test_a_hard_linked_file_keeps_its_acl_and_removal_defers`.
3. **No retry loop on an ACL.**
   - `test_two_files_whose_acl_denies_delete_go_with_their_tmpdir_in_one_pass` and the directory delete and delete_child cases.
   - `test_sweep_unknown_removes_an_acl_locked_folder_and_a_second_sweep_records_nothing`.
4. **Locks don't block bundles,** for a lock at least 10 minutes old.
   - `test_the_bundle_step_succeeds_whatever_an_earlier_attempt_left[neither|tmp|lock|both]`, with the lock exactly 600 s old on a fixed clock.
   - `test_a_bundle_lock_under_ten_minutes_old_defers_and_changes_nothing`: at 599 s the pass defers, the leftovers are unchanged, there is no note and one log line. At 600 s the lock is removed and the job retires.
5. **Idempotence.** `test_a_second_removal_of_a_removed_allocation_is_a_no_op` and the second-sweep test above.

**The fixture rule.**

- `rte_acl.restored` restores the case folder and asserts it removes cleanly; `test_restore_leaves_every_blocking_fixture_removable` checks the rule.
- The tests in `test_rte_reclaim_acl.py` use the same fixture, or `restore(tmp_path)` in their world fixture.
- Session 1's scratch still held one file with `uchg` set (`test_restore_leaves_every_bloc0/blocking/d/e/f`), from a run before its restore-order fix. I cleared it with `chflags -R -P nouchg` and `chmod -R -N`, then a guarded `shutil.rmtree`.

## Mutants

Each was applied alone in the worktree, with its killer run, then restored from a pristine copy in scratch. The restored file's sha256 matched both the copy and `git show HEAD:<file>` after every one (`mutants.py`'s check).

| # | Mutant | Killer | Result |
|---|---|---|---|
| 1 | Drop the line-251 mapping | `test_walk_maps_the_lstat_refused_in_a_listed_but_unsearchable_directory` | Killed: bare `PermissionError: … 'inner.bin'` |
| 2 | Remove only `commits.bundle.tmp` | `test_the_bundle_step_succeeds_whatever_an_earlier_attempt_left[lock]`, `[both]` | Killed: both failed |
| 3 | Clear a symlink's ACL by a call that follows it (`chmod -N` on the path) | `test_a_symlink_whose_own_acl_denies_delete_goes_and_its_outside_target_keeps_its_acl` | Killed: `_remove` is False, because the link's own ACL stayed |
| 4 | Retry the unlink without clearing the ACL | `test_an_acl_refusal_is_retried_once_after_its_acl_is_removed`, `test_two_files_whose_acl_denies_delete_go_with_their_tmpdir_in_one_pass` | Killed: both failed |
| 5 | Clear ACLs on every entry before its unlink | `test_only_an_acl_that_refused_an_unlink_is_cleared` | Killed: two inodes cleared, not one |
| 6 | `sweep_unknown` logs `attempt_id` again | `test_sweep_unknown_logs_the_folder_s_name_and_the_errno_never_none` | Killed: `… deferred for None: PermissionError(EPERM)` |
| 7 | Age rule dropped (`stale = True`) | `test_a_bundle_lock_under_ten_minutes_old_defers_and_changes_nothing` | Killed (second try, below) |
| 7b | Boundary `>=` made `>` | `test_the_bundle_step_succeeds_whatever_an_earlier_attempt_left[lock]` | Killed (second try, below) |

**Retries.** M7's first run hit the driver's 270 s cap under load; rerun with a 540 s cap, it was killed in 60 s. The driver's outer 575 s timeout stopped the first M7b run mid-test and left the mutant in `retention_archive.py`. I restored it from the pristine copy and checked its sha256 (`4e07fdcc…`, equal to HEAD's); `git status` was clean. The rerun killed it in 56 s.

## Commands and results

Environment: `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, PYTHONPATH unset, scratch `$TMPDIR/sf-rte-b2`, a new `--basetemp` for each run, Python 3.15.2.

- **Sync.** `uv sync --group dev --extra api` failed: the project defines no `api` extra. `uv sync --group dev` succeeded.
- **Merge.** One conflict, resolved. The contract's duplicate grep printed nothing.
- **Session 1 scratch.** `sf-rte-b1` cleared and removed with the guarded `rmtree`. The `probe.lock` had already gone.

Test runs, one file per command, at most four at once:

| Command | Result |
|---|---|
| `test_rte_walk_bundle.py -k 'bundle_step_succeeds and neither'` | 1 passed, 45.56 s |
| `test_rte_walk_bundle.py` (all 13, after the age change) | 13 passed, 436.31 s |
| `test_rte_reclaim_acl.py` | 3 passed, 62.67 s |
| `test_rte_tmpdir_acl.py -k removal_is_all` | 1 passed, 59.39 s |
| `test_rte_tmpdir_acl.py -k 'not removal_is_all'` | 16 passed, 226.96 s |
| `test_attempt_tmpdir_leaks.py` (197 collected) | 41 passed, no failure, then cut by the 570 s cap |
| `test_invariants_index.py` | 25 passed, 15.08 s |
| `test_test_module_names.py` | 1 passed, 48.80 s |
| `test_handler_refusals.py` | 372 passed, 440.16 s |
| `tests/fake/test_state_contract.py` | 37 passed, 197.88 s |
| `test_notice_keep.py` (export) | 49 passed, 257.44 s |
| `test_retention_walk_descriptors.py` (export) | 20 passed, 16.85 s |
| `test_retention_delete_apply.py` (export), first try | Didn't start: `-n` unknown, because xdist isn't installed in this venv |
| `test_retention_delete_apply.py`, second try | 1 passed, then cut at 570 s |
| `test_retention_delete_apply.py`, third try | 4 of 13 passed, then cut at 450 s. The stack dump was in the test harness's own `retention_world.snapshot` (`os.walk`), not product code |
| `test_daemon_log_sites.py` (export) | 68 passed and 5 skipped of about 84, then cut at 570 s |
| `test_daemon_log_sites.py -k 'lint or raw or caught_exception or stream'` | 39 passed and 2 skipped, then cut at 450 s |
| `test_no_daemon_module_logs_a_caught_exception_raw` (called directly over 122 modules) | Passed; raw sites `[]` |
| Mutant driver (8 mutants) | All killed; table above |

**The export.** The module suites from `test_notice_keep.py` down ran from `git archive HEAD` at c0329e281, extracted into `sf-rte-b2/suite-tree`, so the in-place mutants couldn't reach them. Its imports resolve to the export's own `subfleet/` and `tests/`. That `git archive` command ran past the tool's 120 s default, so the harness moved it to its background mode. It finished before any run used the export.

## What didn't run, and deviations

**Not run, or not finished.**

- **`test_attempt_tmpdir_leaks.py`.** 156 of its 197 cases didn't run, the T6 case at line 796 among them.
- **`test_retention_delete_apply.py`.** 9 of 13 tests didn't run.
- **`test_daemon_log_sites.py`.** It didn't finish, but its raw-site lint passed directly.
- **The other module suites.** These files import a changed module (`grep -lE 'retention_fs|attempt_tmp|retention_archive|retention_survey|Reclaim' -r tests/`, 73 files with helpers) and didn't run: `test_retention_archive.py`, `test_attempt_tmpdir_lifecycle.py`, `_retention.py`, `_mutations.py`, `_guardian.py`, `test_census_round4.py`, the `tests/fake` retention and TMPDIR files, and the remaining `test_retention_*` files.
- **Why.** Under this host's load (averages 105–183), single tests took 15–100 s of wall time on a few seconds of CPU. CI runs the whole suite.

**Deviations from the brief.**

- **Branch.** The work is on a detached HEAD, not the assigned branch.
- **Merge target.** I merged release/217 at 82d3dd819, not the 85769b826 named in the brief.
- **Scope.** The survey and `sample` now report walk errors they used to drop, and `_git` maps `TreeError`. Both go beyond the line-251 change, but the brief's caller check required them.
- **The link condition.** The nlink == 1 condition on clearing a file's ACL is session 1's addition.
- **Mutants beyond the brief.** M7 and M7b test the age rule; the continuation asked for one.
- **Folders.** Session 1 briefly created and deleted an empty `../rte-msg-2.txt` beside its worktree. Session 2 created nothing outside the worktree and `sf-rte-b2`.
  - The uv cache at `$TMPDIR/uv-cache` is the one the rules name, left in place.
  - `sf-rte-b2` (logs, basetemps, the export, the pristine copies and the driver `probe-scripts/mutants.py`) is left for the hub to read. Delete it with a guarded `shutil.rmtree` that asserts `sf-rte-b2` is in the path. It holds no ACL-locked or immutable entries, which the fixtures restore.

## PR body

**Retention tree errors: walk refusals, git's bundle lock, TMPDIR ACLs (D-SURVEY-EACCES1, D-RET-STALE-LOCK1, D-TMPDIR-ACL1)**

Three tree errors kept retention and TMPDIR cleanup from finishing on the host.

- **Walk (D-SURVEY-EACCES1).** `retention_fs.walk` maps the per-entry lstat's `PermissionError` (a directory listed but not searched, such as 0644) to `TreeError("unreadable")`.
  - The survey and `sample` finish and list the job with the reason.
  - A pass defers it naming the path, and nothing changes the tree's modes.
  - An admin directory's `TreeError` defers instead of escaping the pass.
- **Bundle lock (D-RET-STALE-LOCK1).** Before `git bundle create`, the bundle step removes `commits.bundle.tmp` and git's leftover `commits.bundle.tmp.lock`, the lock only once it is 10 minutes old (`_Builder.BUNDLE_LOCK_STALE_S`).
  - A younger lock defers the job with `bundle lock in use` and removes nothing.
  - Size and age are logged either way.
- **TMPDIR ACLs (D-TMPDIR-ACL1).** Inside a TMPDIR tombstone, an unlink or rmdir refused with EACCES or EPERM is retried once after removing the ACL that refused it, through a checked `O_NOFOLLOW` or `O_SYMLINK` descriptor (ctypes `acl_set_fd_np`).
  - That ACL is this user's single-link entry's deny `delete`, or its in-tombstone directory's deny `delete_child`.
  - Deferral lines name the errno, and `sweep_unknown` names the folder instead of `None`.
- **Reclaim.** Unchanged, with tests. A deny-delete file goes to `retention-conflicts/<job>/` with its directory, and `Reclaim.run` returns True.
- **Contract.** C-8.4 and C-12.10 are amended, with one change-log line.

Tests: new files `test_rte_walk_bundle.py`, `test_rte_tmpdir_acl.py` (with Hypothesis properties over generated trees) and `test_rte_reclaim_acl.py`. All 8 mutants are killed. The contract, index and handler-refusal tests pass. The module suites ran only in part on a loaded host; the report lists what didn't run. Full report: `docs/reports/2026-11-08-retention-tree-errors.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Head built: the commit that adds this version of the report, on c0329e281.

## Fix round 1

Started **2026-11-15 23:13:27 UTC**, from the first command, `date -u`.
Input: review-r1.md, this report and `gh pr view 321 --repo MaxGhenis/subfleet-v2`, all read before implementation. The starting head was **661911f6bfb618d192573859174a19fbbbc72c63**, the hub's unverified WIP on PR head 3e0b0c36b. This section supersedes the earlier validation claims for this round.

### Findings and evidence

- **P1: four broken mutation witnesses.** The warning now uses `who`; two mutation patterns still used `record.get("attempt_id")`. The inherited WIP updates both patterns. Restoring the old table process-locally reproduced exactly the four review failures (4 failed, 3 production controls passed). The final changed suite passes all 85 cases in `test_attempt_tmpdir_mutations.py`, including those four.
- **P1: ACL removal broader than C-12.10.** Accepted and verified the WIP's permission-bit narrowing, with its exact documented limits. `clear_extended_acl` now removes only the selected deny `delete` or `delete_child` permission, drops an ACE only if it becomes empty, and preserves other permissions, allow entries, qualifiers, flags and order. The uid, inode, link-count and temp-root guards remain. Both immutable/unrelated-ACL cases and both mixed-ACE preservation cases failed with the pre-WIP modules; all four pass now. The contract and change log state the exact scope: all deny entries are examined without principal/inheritance matching; a refusal triggers repair but does not prove which condition caused it. Errno alone cannot identify a causal ACE, and this change introduces no ACL-principal evaluator. The chosen repair preserves unrelated rights and avoids whole-ACL removal; it can still remove an inapplicable delete-deny bit after an unrelated refusal.
- **P2: temp-root guard untested (R3).** The crash-left-tombstone test reaches the final rmdir without a rename first. With `parent_inside=True` at that call it fails because removal succeeds and the root ACL is changed. With production code it passes, preserving the root ACL and tombstone. Corrected the older test's name/docstring: its root ACL blocks the initial rename, so it was never evidence for the final-rmdir guard.
- **P2: uid guard untested (R1).** The entry test simulates a different retention uid while the real process still owns the file, exposing the check independently of kernel ownership enforcement. Dropping the entry uid guard makes it fail. An additional parent-uid test likewise fails when that guard is dropped. Both pass unmutated.
- **P2: future lock rule untested (R2).** The new lock dated 70 years ahead stays young, leaves both leftovers unchanged and writes no note. Replacing signed age with `abs(age)` makes the test fail because the lock is removed. It passes unmutated. These guard/coverage findings did not require changing the original guards; their before-fix witnesses are the requested mutants, not failures of already-correct production behavior.
- **P2: deny read+delete never repaired.** Accepted and verified the WIP's checked write-only descriptor fallback for regular files, with no write or truncation. The real ACL denies the read-only open; the pre-WIP remover returns False, while the current remover removes the allocation in one pass. The contract also describes the remaining limit when neither open admits the entry.
- **Cheap P3s fixed.** A 599.999-second lock now displays 599, rather than “600 s old, under 600 s.” A directory-shaped lock defers and preserves both leftovers, rather than logging removal and escaping with EPERM. Both tests fail against the pre-WIP bundle module. I found and fixed an additional mismatch: C-8.4 promised the directory-lock reason would include age and size, but the WIP omitted them. Strengthening that test failed before the added detail; it passes now. C-8.4 and its change-log line agree. The sizing nuance is documented in C-8.4 and the change log: an earlier sizing refusal protects an unmeasurable job; only a pass reaching the archive walk defers it with the path.
- **P3 retained: concurrent same-user hard link after fstat.** There is no atomic ACL-change-if-single-link operation in this descriptor route. The existing single-link check rejects an already-linked inode but cannot exclude that race; C-12.10 names it. Removing the race needs broader isolation work. A far-future lock intentionally defers until its signed age reaches 600 seconds, per the hub's rule; this is now tested rather than changed.
- **Importer-discovered stale oracle.** `test_walk_exit_exception_parity[permission-stat]` still expected parity with a frozen walk predating D-SURVEY-EACCES1. The importer run failed because production correctly returned `TreeError("unreadable")` instead of raw `PermissionError`. The revised assertion permits exactly that documented difference, retaining all other parity and descriptor checks. A process-local mutant restoring raw PermissionError fails the revised test; the full descriptor-review file passes afterward.

### Commands and results

The required setup, `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, and `mkdir -p "$TMPDIR/sf-rte-fix1"`, was attempted first. Both the scratch mkdir and cache access were denied by the sandbox. The successful fallback used `TMPDIR=/private/tmp/sf-rte-fix1` and `UV_CACHE_DIR=/private/tmp/sf-rte-fix1/uv-cache`. `uv sync --group dev` succeeded: Python 3.15.2, nine packages installed. No lockfile/dependency change was made.

The process-local baseline/mutant driver and final pytest isolation plugin are preserved in `docs/reports/2026-11-08-retention-tree-errors-fix-r1-data/`. Baseline source comes from `git show 3e0b0c36b:<module>` and is executed only in the test process; mutants likewise replace one decision in memory. These probes did not mutate production source on disk.

For the table, pytest defaults were `-q --timeout=300 --timeout-method=thread`, overridden where noted, and a distinct `--basetemp=/private/tmp/sf-rte-fix1/pytest-<run>`. Baseline/mutant commands used `uv run --no-sync python .../verify.py <mode> ...`. Suite commands used `uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest`, at most `-n 4`. Isolated suites used `-p sf_rte_safe`, with the plugin on the scratch PYTHONPATH. Importer/retry/final suites add `-rs` or `-ra`, and `--durations=15`. Retries use `--offline` and unbuffered Python.

| Run / command arguments | Result |
|---|---|
| `baseline-acl tests/unit/test_rte_tmpdir_acl.py -k 'immutable_refusal_preserves or clearing_delete_denials or denies_read_and_delete'`, `pytest-baseline-acl` | Expected 5 failed, 17 deselected; 23.11 s |
| First `baseline-patterns .../test_attempt_tmpdir_mutations.py -k 'no-top-chmod or no-pre-rename-chmod or pre_rename_chmod_mutant'`, `pytest-baseline-patterns` | 7 passed, 78 deselected; 4.63 s. Harness error: replaced an import different from pytest's collected module; this was not baseline evidence |
| Corrected same baseline-patterns command, `pytest-baseline-patterns-2` | Expected 4 failed, 3 passed, 78 deselected; 8.51 s |
| `baseline-bundle tests/unit/test_rte_walk_bundle.py -k 'just_below or folder_shaped'`, `pytest-baseline-bundle` | Expected 2 failed, 14 deselected; 11.01 s |
| `R1 tests/unit/test_rte_tmpdir_acl_guards.py -k entry_owned`, `pytest-R1` | Killed: 1 failed, 2 deselected; 6.92 s |
| `R1-parent tests/unit/test_rte_tmpdir_acl_guards.py -k parent_owned`, `pytest-R1-parent` | Killed: 1 failed, 2 deselected; 7.22 s |
| `R2 tests/unit/test_rte_walk_bundle.py -k seventy_years`, `pytest-R2` | Killed: 1 failed, 15 deselected; 6.49 s |
| `R3 tests/unit/test_rte_tmpdir_acl_guards.py -k crash_left`, `pytest-R3` | Killed: 1 failed, 2 deselected; 3.18 s |
| Changed files: `tests/unit/test_attempt_tmpdir_mutations.py tests/unit/test_rte_tmpdir_acl.py tests/unit/test_rte_tmpdir_acl_guards.py tests/unit/test_rte_walk_bundle.py -n 4`, `pytest-changed` | 126 passed; 125.85 s. This run used the original World fixture; deviation below |
| Same changed files, with local-fetch fixture replacement, `pytest-changed-safe` | 126 passed; 210.18 s |
| `-p sf_rte_safe tests/unit/test_rte_walk_bundle.py -k folder_shaped`, `pytest-directory-detail-before` | Expected 1 failed, 15 deselected; 8.31 s, missing directory-lock age/size |
| Final changed files with the then-current isolation plugin, `pytest-changed-final` | 126 passed; 134.35 s |
| First all-importer attempt, `pytest-importers` | Interrupted with exit 130 after a long silent startup/collection period; six progress marks, no final totals. No result claimed |
| All importer files, adding `--offline` and unbuffered Python, `pytest-importers-offline` | 9 failed, 1,799 passed, 84 skipped, 7 xfailed; 2,526.88 s. Failures and skips detailed below |
| Full `tests/unit/test_retention_archive.py tests/unit/test_retention_descriptor_review.py -n 4 --timeout=600 --timeout-method=signal`, `pytest-archive-review-retry` | 155 passed, 1 forbidden-command skip; 208.61 s |
| `walk-stat-raw -p sf_rte_safe 'tests/unit/test_retention_descriptor_review.py::test_walk_exit_exception_parity[permission-stat]' --timeout-method=signal`, `pytest-walk-stat-raw` | Killed: 1 failed; 1.36 s |
| `uv run --no-sync --with 'sortedcontainers==2.4.0' python -c 'import sortedcontainers; print("sortedcontainers 2.4.0 cache ready")'` | Exit 0; package import worked, but did not supply the offline index metadata |
| Full `tests/unit/test_retention_ordinary_trees.py -n 4 --timeout=600 --timeout-method=signal`, `pytest-ordinary-retry` | 4 cache-prerequisite skips; 5.07 s |
| `UV_DEFAULT_INDEX=https://pypi.org/simple uv venv /private/tmp/sf-rte-fix1/cache-prime-venv --python .venv/bin/python`, then `uv pip install --python /private/tmp/sf-rte-fix1/cache-prime-venv/bin/python 'sortedcontainers==2.4.0'` with the same index/cache | Both exit 0; resolved and installed one package into scratch |
| Full ordinary-tree file, same public index, `-n 3 --timeout=600 --timeout-method=signal`, `pytest-ordinary-public-cache` | 4 passed; 14.19 s |
| The two crashed property cases, `-n 0 --timeout=1200 --timeout-method=signal`, `pytest-long-properties-retry` | 2 passed; 227.15 s; unchanged Hypothesis example counts |
| Required final four files, `-n 4 --timeout=600 --timeout-method=signal`, `pytest-final` | 435 passed; 32.38 s |

`git grep -l -E '(attempt_tmp|retention_fs|retention_archive)' -- 'tests/**/test_*.py'` found 70 files. AST inspection found 66 direct importers, including multiline/local imports and aliases; all 70 conservative matches were included in the importer command. Both lists are preserved beside the drivers. The final four files are `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py`.

The nine importer failures comprised six archive assertions caused by the fallback path's lexical `tmp` component, the stale parity assertion above, and worker exits while running `test_any_detached_cwd_lifetime_interleaving_preserves_the_reservation` and `test_interleavings_with_case_aliases_never_delete_a_folder_a_row_names` under the 300-second thread timeout. All nine affected cases pass in the retries; the two properties ran serially with a 1,200-second signal timeout and every original example. The initial 84 skips were 32 routing-state cases that inspect unrelated processes, 48 cases stopped before forbidden Git commands, and four ordinary-tree cases missing their offline cache prerequisite. Those four pass after public-index cache priming. Seven existing expected failures remained expected. The remaining 80 unique importer skips were left for CI because of the brief's command/process restrictions; this is not a claim that every importer case executed.

Read-only inspection used `git status --short`, `git log`, `git show`, `git diff`, `git grep`, `rg`, `sed`, and small AST inspection scripts within the worktree/scratch. `AGENTS.md` and `tests/retention_harness.py` were absent; those reads exited 1. The specified external review was read directly; no search of personal state was used. `git diff --check` passed after the code changes.

### Isolation, deviations, cleanup and commits

- **Hidden forbidden command in the first changed-suite run.** `retention_world.World.__post_init__` calls its `push` helper, which executes `git push` to a local bare fixture repository. I missed that before the first 126-pass run. This breached the command-shape rule despite using only synthetic local repositories; it was disclosed during the session. Subsequent suite runs replace the helper process-locally with `git fetch` into the synthetic bare remote and a normal tracking-ref fetch, asserting both repositories are under this scratch. Remaining direct forbidden command attempts are skipped before subprocess execution by a Python audit hook, including git push/stash and the named gh mutations/helpers.
- **Shared pytest cleanup hook.** Earlier runs also used the repository's default `failed_keep.prune()` hook against `/tmp/sf-failed`. No prune was reported by those runs; I did not inspect that unrelated folder to infer more. The final plugin redirects `tests.failed_keep.ROOT` under this scratch before session start. It also skips the two routing-state TMPDIR daemon modules that would scan unrelated live processes, and blocks broad ps/lsof subprocess scans. Marked cwd/lsof tests restrict inspection to their own children and remain enabled.
- **Setup deviation.** The prescribed Darwin scratch/cache were denied. All successful test runs use the writable `/private/tmp/sf-rte-fix1` fallback; the first required-path setup attempt created no folder there.
- **Fallback-path test seam.** Beginning with `pytest-archive-review-retry`, the plugin removes only `/private/tmp/sf-rte-fix1` from the lexical path-component input to `retention_git.scratch_reason`, process-locally. The fallback's `tmp` ancestor otherwise forces every trusted synthetic repository to be classified as scratch, unlike the prescribed Darwin path. Real paths, temp-root/state-root checks, explicit scratch descendants and production files are unchanged. Earlier suite runs did not use this seam. The six affected archive assertions pass in the full-file retry.
- **Importer retry.** The first importer run was interrupted after producing no visible output for several minutes; its log later showed six progress marks. The full retry uses cached dependencies (`--offline`) and unbuffered Python so collection/startup is visible. No partial count from the interrupted run is included in validation totals.
- **Audit command rejected.** One AST scan/write for an optional deselection list was rejected by automatic approval review because its source contained forbidden command words as data. It executed nothing; `apply_patch` safely created the scratch list instead. The list was not used by the suite runner.
- **Commits denied.** Named-file staging and commits were attempted after the test-description step, the directory-lock fix and the stale-oracle correction. Staging and committing failed with `index.lock: Operation not permitted`. The final report/data commit attempt is recorded below. I did not bypass the sandbox, rewrite history, stash, or push this checkout. The hub must collect the final files. This remains the assigned detached checkout.
- **Cleanup / final checks.** The generated folders outside scratch were recorded here before deletion. All paths below are relative to `/Users/maxghenis/.subfleet/worktrees/20261115-180648-rte-fix-r1b/`: `.hypothesis`, `.pytest_cache`, `.venv`, `docs/reports/2026-11-08-retention-tree-errors-fix-r1-data/__pycache__`, `subfleet/__pycache__`, `subfleet/adapters/__pycache__`, `subfleet/conversations/__pycache__`, `subfleet/gate/__pycache__`, `subfleet/guard/__pycache__`, `subfleet/sessions/__pycache__`, `tests/__pycache__`, `tests/fake/__pycache__`, and `tests/unit/__pycache__`. `python3 .../cleanup.py` exited 0: inventoried all 13, checked that none contained a tracked file or was a symlink, moved them into the workspace's `sf-rte-fix1-generated-caches`, asserted `sf-rte-fix1` was in that path and called guarded `shutil.rmtree`. All 13 and the staging folder were removed. The inventory is preserved in `cleanup-paths.txt`. The report/data folder is an intentional deliverable, retained. `/private/tmp/sf-rte-fix1` remains for the hub's logs/cache; no scratch folder elsewhere was intentionally created. Final `git diff --check` passed.
- **Not run.** The whole suite, other Python versions and the earlier report's additional manual mutants were not rerun. The changed 85-case mutation suite and this round's guard mutants did run. The remaining importer skips are detailed above; prohibited operations were not authorized for those omitted cases.

Final report/data commit attempt: explicit `git add` of the 12 changed/deliverable files and `git commit -m 'fix: finish retention tree error review round one'` both refused creation of `index.lock` with “Operation not permitted.” The built result consists of the starting head plus the reported working-tree changes; the hub must collect them.

Finished: **2026-11-16 00:37:22 UTC**, within the three-hour limit (1 h 23 m 55 s).

Head built: **661911f6bfb618d192573859174a19fbbbc72c63**
