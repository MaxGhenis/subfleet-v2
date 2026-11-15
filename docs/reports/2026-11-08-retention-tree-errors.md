# Retention tree errors: D-SURVEY-EACCES1, D-RET-STALE-LOCK1, D-TMPDIR-ACL1

Two sessions built this. Both worked on a detached HEAD, because git refuses to check out `fix/retention-tree-errors-217` here while the caller's checkout has it. Subfleet keeps the commits under `refs/subfleet-salvage/`.

- **Session 1 (rte-build-r1), 2026-11-13 18:29–21:20 UTC.** It made the three fixes and their tests, and stopped at its 3-hour limit.
  - e89f202a9: walk maps the lstat refusal (change 1).
  - 96ad826ad: the bundle step removes git's lock (change 2).
  - 5169107e0: TMPDIR removal clears a blocking ACL (change 3).
  - 0f846b74f and a8583bee0: tests.
  - d767b2a8b: the first report.
- **Session 2 (rte-build-r2), 2026-11-14 23:40 UTC to 2026-11-15 02:30 UTC.**
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
