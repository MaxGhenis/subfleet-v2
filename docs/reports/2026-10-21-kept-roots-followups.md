# Kept roots follow-ups: final decision audit and verification (D-KR1)

2026-10-22. Continuation workspace: `20261022-065122-kr1-build-b`; starting commit **`ad9604d37566`**.
The four follow-ups from #238 review r1 are **implemented**. Their focused tests pass and all four required
mutants are caught. Full-suite verification is **partial**: 6,173 passed, 15 skipped, two failed because
this sandbox denies process/kernel inspection, and 9,120 collected cases remain unrun.

The earlier report and PR-body drafts were uncommitted in the caller's checkout and therefore absent from
this detached snapshot. They were recovered read-only into this workspace. Neither draft certified green
files individually: its test section was unfinished. **No file was omitted on the strength of an earlier run.**
All `tests/unit` and `tests/fake` cases were selected in the new full-suite invocation.

## Decision audit

The audit covers the six follow-up commits after #238's merge, `c0759bd65`, through the starting lane head.
No decision needed further implementation changes in this continuation.

| Decision | Status | Implementation and evidence |
|---|---|---|
| 1. Keep evidence when teardown raises | **Done** | `2faf7c66d`: nested `finally` in both harnesses; copies after `close()`; preserves its error. Four focused cases pass; moving the copy before close kills two named daemon cases. |
| 2. Pin the shared layout and upload | **Done** | `6eb25a32d`: AST/source scan, keeper discovery and CI path check. Four cases pass; the stray-literal mutant is caught. |
| 3. Publish complete cached probes atomically | **Done** | `acd1e8805`: same-folder temporary copy and `os.replace`, with cleanup. Four cases pass; direct copying to the final name kills all four. Readers use neither size nor digest checks. |
| 4. Prune old roots of gone processes | **Done** | `21fa297b0`: session-start pruning plus the documented 24-hour rule. Eleven cases pass; treating a live pid as gone kills three named cases. |

There are **no partly implemented or unstarted decisions**. Remaining work, as the build ended, was the baseline rerun and the
unrun suite cases listed file by file below, plus verification of the two inspection failures in a capable environment.

## Commits and release integration

| Commit | What |
|---|---|
| `66995d255` | Tests first: teardown, layout, cache and pruning regressions. |
| `2faf7c66d` | Preserve a failed test's root even when fake/e2e teardown raises. |
| `6eb25a32d` | Pin helper use, the absence of stray literals, and the workflow upload path. |
| `acd1e8805` | Publish cached probes with an atomic rename. |
| `21fa297b0` | Prune departed processes' old roots; document the rule. |
| `ad9604d37` | Strengthen the pruning model and the copy-after-close assertion. |
| `df751af86` | Merge the already cached `origin/release/217` at `43f42d83d` (including #232). |
| this commit | Finalize this report and the PR body with actual results and remaining coverage. |

The cached release reference was not an ancestor of the starting head. It was merged without fetching,
conflicts, rebasing or discarding either parent's changes. The contract retains **all 255 clause identifiers
from the lane parent and all 257 from the release parent**, including letter-suffixed clauses, plus **all 19
and 26 version-3 change-log paragraphs**, respectively. The check compared both merge parents directly
with the resulting document. `git diff --check` passes.

The KR1-specific implementation changes are under `tests/` and `docs/`. The merge also imports release's
existing production changes. `.github/workflows/tests.yml` remains unchanged.

The build's commits were made in workspace-local `.git-local` metadata, since its sandbox could not write the shared
index. The hub then salvaged them onto `fix/kept-roots-followups-217`, merged release `4f5983f4c` (`cb1803cf6`),
pushed the branch and opened #247. The section "After review r1" at the end records what followed.

## Invariants

Each is tested, and each test fails against a mutant that breaks it (section 6):

1. **A failed test's root is kept whether or not teardown raises.**
   - The copy is taken after `close()` has run, whether `close()` returned or raised.
   - `close()`'s error is still the teardown's error. A copy that fails never replaces it.

2. **Only `tests/failed_keep.py` builds the path, and CI uploads its `ROOT`.**
   - No string or bytes literal in `tests/` code names the folder outside that file. Docstrings and comments may.
   - No non-Python file under `tests/` names it at all.
   - Every harness that keeps roots imports and calls `failed_keep_dir`.
   - The workflow's one kept-roots upload has `path:` equal to `failed_keep.ROOT`.

3. **The probe cache never exposes a partial file under its final name.**
   - After every chunk of every copy, a file under a digest name is the whole probe.
   - A fill that fails leaves nothing behind.

4. **Pruning never removes a live process's folder, nor one younger than 24 hours.**
   - It never removes the session's own folder, nor anything but a directory whose name is a pid.
   - It does remove every folder the rule names, each exactly once, even with several pruners at once.

## 1. Evidence survives a failing teardown (finding 2)

**Cause.** In `tests/fake/conftest.py`'s `daemon` and `tests/e2e/conftest.py`'s `e2e`, `harness.close()` ran first in
the `finally`, and the keep code ran after it in the same block. When `close()` raised, the copy never ran, and
`TemporaryDirectory` deleted the root. r1 saw this with `TimeoutExpired` stopping `tests.fake.run_daemon`.
`test_failed_keep_teardown.py` shows it at the base: a test that fails while `close()` raises keeps nothing, under
either harness.

**Fix.** Both fixtures now run the keep in a nested `finally` after `close()`, as `tests/frontend/test_core_live.py`'s
`dev_daemon` already did. The keep condition is: the call failed, or `close()` raised. The second half matches
`dev_daemon`, which keeps when `not (started and closed)`.

- A daemon that would not stop, or a C-15.1 notice mismatch raised from `close()`, is evidence too. The
  root is kept in that case even when the test's call passed, matching the frontend harness.

- The copy is wrapped in `except OSError`, and a failed copy is printed to stderr ("could not keep the state root"),
  as `keep_failed_root` already does. So `close()`'s error, or the test's, is what pytest reports. Before, a copy
  failure raised in teardown.

**Tests.** The cases run the real fixtures in a scratch pytest session (pytester, in-process), with `ROOT` moved to the
test's `tmp_path`:

- `test_a_failed_test_keeps_its_root_whether_or_not_close_raises[daemon|e2e]` covers every pairing of a test that fails
  or passes with a `close()` that raises or returns. It checks which roots are kept, that each holds `close()`'s
  marker, and that the two raising teardowns report `TimeoutExpired` naming `tests.fake.run_daemon`.

- `test_a_copy_that_fails_never_replaces_close_s_error[daemon|e2e]` puts a file where the kept-roots folder should be.
  The teardown error is still `TimeoutExpired`, and the copy failure is on stderr.

## 2. The layout is pinned (finding 5)

`tests/unit/test_failed_keep_layout.py` parses every Python file under `tests/` once and walks each tree once. It
searches for `failed_keep.ROOT.name`, so it holds no copy of the path itself.

- **No stray literal.** No string or bytes constant outside `tests/failed_keep.py` names the folder; f-string parts are
  constants too. Docstrings are exempt, and comments never reach the tree. Every other file under `tests/` (scripts,
  Swift probes, fixtures; `.md` and `.pyc` aside) must not contain the name at all. The helper's own literal must be
  found, so the scan can't pass vacuously.

- **Every keeper asks the helper.** A harness that keeps roots is a file that reads a phase report (`"rep_…"` or
  `.rep_…`) and calls `copytree`. Discovery must find the three known harnesses. Every keeper must import
  `failed_keep_dir` from `tests.failed_keep` and call it.

- **CI uploads `ROOT`.** A small reader of the workflow's steps finds the `actions/upload-artifact` steps. Exactly one
  has an artifact name starting `sf-failed-`, and its `path:` must equal `str(failed_keep.ROOT)`. Trailing comments and
  quotes are stripped. The reader has its own test on a workflow-shaped sample.

- **The one stray literal at the base** was `tests/unit/test_failed_keep.py:18`, `Path("/tmp/sf-failed")`. That pin is
  now the CI-path test, so the line went.

## 3. The probe cache can't hand out a truncated binary (finding 3)

**Cause.** `tests/frontend/swift.py` filled `build/frontend-probes/<digest>` with `shutil.copy2(binary, cached)`. That is
a copy straight to the final name, and readers take any file there (`cached.is_file()`). r1 measured `is_file()` true
at 0 bytes during a 64 MiB copy.

**Fix.** The probe is copied to a `tempfile.mkstemp` name in the same folder (`.<digest>.XXXX.partial`, never a digest)
and `os.replace`d into place. A `finally` removes the temporary file if the copy fails. `copy2`'s `copystat` sets the
mode from the compiled binary, so the cached probe stays executable.

**Readers check neither size nor digest; they don't need to.**

- A digest name is now only ever created by that rename of a complete file, and `rename(2)` is atomic within one
  file system.

- A reader that opened the file before a second writer's rename keeps reading the whole inode it opened.

- One case this does not cover: a *pre-fix* writer whose copy failed may have left a truncated file under a digest
  name in a long-lived checkout's `build/`, whether it was killed mid-copy or hit a write error (#247 review r1 saw
  an EFBIG failure leave 1 MiB under the final name at the base). Fresh Subfleet worktrees and CI runners have no `build/`. If a frontend
  probe fails to run in an old checkout, delete `build/frontend-probes` once.

**Tests.** `tests/unit/test_frontend_probe_cache.py` replaces the compiler with a stand-in, and `compile_probe`'s
`shutil` with one that copies 4 KiB at a time. After every chunk it reads every digest-named file in the cache and
requires the whole probe.

- Filling the cache: one whole probe under one final name, and nothing else in the folder.

- A reader takes the cached probe without compiling, and the result is executable.

- A fill that fails partway leaves the folder empty, and the next hold compiles and fills it.

- A Hypothesis property (40 derandomized examples) over probe sizes from 0 to 24 KiB and failure points.

## 4. Kept roots are pruned (finding 6)

**Rule** (`tests/failed_keep.py`'s `prune`, called by `tests/conftest.py`'s `pytest_sessionstart`, before collection).
`<ROOT>/<name>` is removed only when all of these hold:

- it is a directory: not a symlink, not a file;

- `name` matches `[1-9][0-9]*` and is at most 2³¹−1. So `0`, `007`, `-5`, Unicode digits and longer numbers are left
  alone, and `os.kill` is never called with 0 or a negative pid;

- the pid is not the session's own, and `os.kill(pid, 0)` raises `ProcessLookupError`. EPERM (another user's process)
  or any other error counts as alive;

- the folder's own mtime is more than 24 hours old. It changes whenever a test's root is added to it or removed from
  it, so it is the time of the last keep.

**Claiming.** Each folder is first renamed to `.pruning-<pid>-<pruner pid>-<random>` and then removed.

- When several pruners run at once (every xdist worker prunes at its own start), the rename lets exactly one of them
  have each folder.

- A new process that takes the pid after the check keeps its roots in a new folder.

- If the claimed folder's mtime changed, or its pid answers as alive afterwards, it is renamed back.

- `prune` never raises. A folder it cannot read or claim is left as it is; one it claims but cannot fully remove is put back under its pid name and not reported removed (#247 review r1, P3-1, fixed by the hub below).

**CI.** Pruning changes nothing there, and the upload is unchanged.

- Each `macos-15` job runs on a fresh GitHub-hosted runner with no `/tmp/sf-failed`.

- A known flake's retry (`tools/ci_retry_known_flakes.py`) is a second pytest process started as soon as the first
  ends, so the first attempt's folder is less than a day old and is uploaded beside the retry's.

**The rule is stated** in `docs/lanes/README.md`, with "copy a root elsewhere to keep it longer";
`docs/lanes/quarantine-flakes.md` points to it.

**Tests.** `tests/unit/test_failed_keep_prune.py` prunes scratch folders only, never `/tmp/sf-failed`.

- **With real processes:**
  - a reaped child's pid, 24 h 1 min old, is removed;
  - a live `sleep` child's, this process's and its parent's, 30 days old, are kept;
  - dead pids 1 minute and exactly 24 hours old are kept;
  - `notes`, `0`, `007`, `12x`, `-5`, `١٢`, 2³¹, an 11-digit number and a `.pruning-` name are kept, and so are a
    dead pid's symlink (its target untouched) and a dead pid's file.

- **A Hypothesis model of the rule** (40 derandomized examples plus an explicit one). Its inputs are generated entries
  (pid-shaped or not, this process's pid, dir/file/symlink, ages around the boundary) and a stand-in `kill` that
  answers alive, ESRCH, EPERM or EINVAL. The removed set must equal exactly what the rule names, with nothing removed
  twice, no claimed folder left behind, and a second prune removing nothing.

- **The claim's race guards:** a folder written between the check and the claim is put back, and a pid that answers
  alive after the claim keeps its folder.

- **Four pruners at once** (threads, behind a barrier) remove each of 40 folders exactly once.

- **Before any test runs:** the root conftest's `pytest_sessionstart`, in a scratch session, has removed an old dead
  pid's folder before the first test body runs, and has kept this process's.

**The real kept-roots folder in this continuation.** The actual pytest processes (8609 and 34059)
left no folders there. Fault-injection tests use scratch `ROOT`s. The mutant's session-start hook was also
wrapped to use scratch only, restoring `ROOT` before collection so the CI path test still checks the real
constant. The positive session logged no real-root pruning. No real kept-root folders were manually removed;
other runs' evidence was left alone.

## 5. Tests run in this continuation

CPython 3.14.7, GIL enabled; pytest 9.1.1 and Hypothesis 6.168.1 from the locked dev environment.
The mandated scratch venv was reused and synchronized to this checkout offline through `uv_sync_hold.sh`.
Initial isolated builds could not obtain hatchling from the empty scratch cache. Cached backend packages
were reconstructed locally, temporarily installed through the same setup wrapper, and the successful
`uv sync --locked --group dev --python /opt/homebrew/bin/python3.14 --no-build-isolation` removed those
extra build packages. No project dependency or lockfile changed and no network was used.

Every external pytest invocation went through `suite_hold.sh`, one at a time, with one worker. `TMPDIR`,
Hypothesis storage, logs and probes stayed under the prescribed `sf-kr1` scratch folder. The CI Hypothesis
profile was bounded to 40 examples and 30 stateful steps, with the focused properties' explicit bounds retained.
The lane markers stayed inherited; `SUBFLEET_RETENTION_DORMANT` was unset. Test outcomes were journaled by
node and phase so incomplete cases are not counted as passes.

| Hold | Outcome |
|---|---|
| Initial focused queue | Cancelled before acquiring a slot; replaced with one consolidated positive invocation. Zero tests ran in this queue. |
| `positive-01`, merged source, slot A from 14:18:58 UTC | All unit and fake cases, plus the selected e2e file, collected: **15,310**. Focused files and required harness/state/name files ran first. **6,173 passed, 15 skipped, 2 failed** before interruption; **9,120 unrun**. Pytest recorded 1,143.26 seconds and exit 2. |
| `mutations`, scratch archive of the merged source, slot B at 14:56:26 UTC | **10 expected failures, 15 passed**, exactly the four required mutants' expected failure set. Pytest: 34.18 s; entire slot command: 48.9 s. |
| `baseline`, scratch archive of `c0759bd65` with the new tests | Queued at 14:57:26 UTC, never acquired a slot, cancelled at the 15:41 UTC cutoff. **Not run**. Earlier red-proof notes below are historical, not a new result. |
| Remaining positive cases | Resume ledger prepared; no further hold started before the cutoff. **9,120 unrun** as detailed below. |

**Timing correction.** The first positive hold's 19-minute timer began after interpreter/plugin startup.
The slot therefore exceeded 20 minutes (at least 20 min 29 s); I interrupted only this task's command and
confirmed pytest pid 8609 had exited. This was an operational mistake. Subsequent commands use a separate
foreground parent supervisor that starts timing at slot entry and interrupts pytest after 17 min 30 s,
including startup. The mutation hold completed within a minute; the baseline never started. No new holds
were started after the 4 h 30 m cutoff (job start 11:11:37 UTC; cutoff 15:41:37 UTC).

**Focused and explicitly requested files** (these are included in the directory totals, not added again):

| File | Result |
|---|---|
| `tests/unit/test_failed_keep.py` | 2 passed |
| `tests/unit/test_failed_keep_layout.py` | 4 passed |
| `tests/unit/test_failed_keep_teardown.py` | 4 passed |
| `tests/unit/test_frontend_probe_cache.py` | 4 passed |
| `tests/unit/test_failed_keep_prune.py` | 11 passed |
| `tests/fake/test_resume_cli.py` | 1 skipped |
| `tests/e2e/test_guard_and_isolation.py` | 5 skipped |
| `tests/fake/test_state_contract.py` | 37 passed |
| `tests/unit/test_test_module_names.py` | 1 passed |

The five focused files account for **25 passing cases**. The selected fake and e2e files skipped because
this sandbox cannot inspect the real processes their fixtures require. The stand-in fixture regression tests
still exercised both actual teardown fixtures, including failing calls, raising close operations, and copy errors.

**Directory totals** (the e2e row covers only the requested selected file):

| Directory | Selected | Passed | Skipped | Failed | Unrun |
|---|---:|---:|---:|---:|---:|
| `tests/e2e` | 5 | 0 | 5 | 0 | 0 |
| `tests/fake` | 1972 | 37 | 1 | 0 | 1934 |
| `tests/unit` | 13333 | 6136 | 9 | 2 | 7186 |

**Two environment failures**, both outside the KR1 implementation:

- `tests/unit/test_daemon_qos_tools.py::test_thread_priorities_reads_every_thread_of_a_clamped_process`:
  `PermissionError: [Errno 1] Operation not permitted: '/bin/ps'`.

- `tests/unit/test_descriptors.py::test_c16_6_the_kernel_ceiling_is_read_on_macos`:
  the kernel ceiling is `None`; `kernel_open_files_ceiling()` reads `/usr/sbin/sysctl -n kern.maxfilesperproc`,
  which this sandbox denies. The assertion requires a positive integer.

These tests were not changed or marked green. The remaining **7,186 unit and 1,934 fake cases** were collected
but never executed. Section 7 records every file, including partial and entirely unrun files. The baseline rerun
also remains unrun. The fresh mutation failures are expected checks, separate from the two positive-run failures.

## 6. Mutations

**Fresh verification in this continuation:** all four required mutants ran together in a scratch archive
of `df751af86`. Nothing in the worktree was mutated. The exact expected set was **10 failed, 15 passed**:

| Required mutant | Named tests that caught it |
|---|---|
| Copy before `close()` without the nested `finally` in the fake fixture | `test_a_failed_test_keeps_its_root_whether_or_not_close_raises[daemon]`; `test_a_copy_that_fails_never_replaces_close_s_error[daemon]` |
| A stray kept-roots literal in `test_failed_keep.py` | `test_only_the_helper_names_the_kept_roots_folder` |
| `copy2` straight to the digest name | `test_filling_the_cache_never_shows_a_partial_probe`; `test_a_reader_takes_the_cached_probe_without_compiling`; `test_a_fill_that_fails_leaves_no_file_behind`; `test_no_fill_ever_shows_a_partial_probe` |
| A pid that answers `kill(pid, 0)` counts as gone | `test_a_live_process_folder_is_kept_however_old`; `test_pruning_removes_exactly_the_folders_the_rule_names`; `test_a_pid_that_comes_alive_while_its_folder_is_claimed_keeps_it` |

**Earlier build's mutation and regression development notes** (preserved from its recovered report):


Each mutant was applied to a `git archive` copy of HEAD in the scratch folder, never to the worktree. The four
decisions' mutants (five edits, in disjoint files, each caught only by its own test file) ran in one tree.

| Mutant | Caught by |
|---|---|
| keep before `close()` in the same `finally`, no nested `finally` (`tests/fake/conftest.py`) | `test_a_failed_test_keeps_its_root_whether_or_not_close_raises[daemon]` ("test_fails[close_raises]'s root was copied before close() ran") and `test_a_copy_that_fails_never_replaces_close_s_error[daemon]` (the teardown error became `NotADirectoryError`); the `[e2e]` cases pass |
| a stray `Path("/tmp/sf-failed")` (#238's pin, put back in `tests/unit/test_failed_keep.py`) | `test_only_the_helper_names_the_kept_roots_folder` |
| `keep_failed_root` builds `ROOT / pid / name` itself (`tests/frontend/test_core_live.py`) | `test_every_harness_that_keeps_roots_asks_the_helper_for_the_path` |
| `shutil.copy2(binary, cached)` straight to the final name | all four in `test_frontend_probe_cache.py` ("… holds 4096 of 262161 bytes") |
| a pid that answers `kill(pid, 0)` counts as gone | `test_a_live_process_folder_is_kept_however_old`, `test_pruning_removes_exactly_the_folders_the_rule_names`, `test_a_pid_that_comes_alive_while_its_folder_is_claimed_keeps_it` |

- **First round:** the five edits ran against the first version of the tests. All 10 named failures appeared, but the
  Hypothesis model passed against the live-pid mutant: derandomized draws had no old folder of a live pid.

- **Second round:** an explicit example with every `kill` answer, and ages weighted to the boundary, made the model
  catch it. The teardown test's checks were reordered so the keep-before-`close()` mutant fails on the missing marker
  (commit `ad9604d37`). The second round ran against the same mutant tree: 5 failed, 10 passed, exactly the expected
  set.

## 7. Every selected file: completed results and remaining cases

The table covers **332 unit files, 106 fake files and one selected e2e file**. **122 files** completed
with passes and no failures/errors. The only partial file is `tests/unit/test_host_push.py`: **102 of 118
cases passed, 16 remain unrun**.

These counts certify this continuation only, on the merged source. A complete row with passing cases and
no failures/errors has finished; skip-only rows do not verify the skipped behavior. Partial or unrun rows
need their remaining coverage, and the two failing files need a capable environment. An interrupted case
without a call outcome remains unrun. There were no teardown/setup errors in the positive invocation.

| File | Selected | Passed | Skipped | Failed | Unrun | Status |
|---|---:|---:|---:|---:|---:|---|
| `tests/e2e/test_guard_and_isolation.py` | 5 | 0 | 5 | 0 | 0 | skipped |
| `tests/fake/test_admission_disk.py` | 27 | 0 | 0 | 0 | 27 | unrun |
| `tests/fake/test_admission_held_waiters.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_admission_latency.py` | 14 | 0 | 0 | 0 | 14 | unrun |
| `tests/fake/test_admission_lease_waiters.py` | 16 | 0 | 0 | 0 | 16 | unrun |
| `tests/fake/test_admission_liveness.py` | 32 | 0 | 0 | 0 | 32 | unrun |
| `tests/fake/test_admission_priority.py` | 32 | 0 | 0 | 0 | 32 | unrun |
| `tests/fake/test_admission_properties.py` | 28 | 0 | 0 | 0 | 28 | unrun |
| `tests/fake/test_admission_ramp.py` | 19 | 0 | 0 | 0 | 19 | unrun |
| `tests/fake/test_admission_ramp_retention.py` | 11 | 0 | 0 | 0 | 11 | unrun |
| `tests/fake/test_admission_route_isolation.py` | 93 | 0 | 0 | 0 | 93 | unrun |
| `tests/fake/test_admission_stateful.py` | 16 | 0 | 0 | 0 | 16 | unrun |
| `tests/fake/test_admission_visibility.py` | 43 | 0 | 0 | 0 | 43 | unrun |
| `tests/fake/test_batch_label.py` | 9 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_canonical_identity.py` | 24 | 0 | 0 | 0 | 24 | unrun |
| `tests/fake/test_canonical_identity_fix2.py` | 21 | 0 | 0 | 0 | 21 | unrun |
| `tests/fake/test_canonical_identity_fix3.py` | 18 | 0 | 0 | 0 | 18 | unrun |
| `tests/fake/test_canonical_identity_fix4.py` | 11 | 0 | 0 | 0 | 11 | unrun |
| `tests/fake/test_canonical_identity_fix5.py` | 14 | 0 | 0 | 0 | 14 | unrun |
| `tests/fake/test_capacity_steer_daemon.py` | 54 | 0 | 0 | 0 | 54 | unrun |
| `tests/fake/test_capacity_wait_backoff.py` | 30 | 0 | 0 | 0 | 30 | unrun |
| `tests/fake/test_census_escapers_daemon.py` | 9 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_census_recycled_pid_daemon.py` | 29 | 0 | 0 | 0 | 29 | unrun |
| `tests/fake/test_codex_ca_bundle_fake.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_codex_turn_completed_fake.py` | 109 | 0 | 0 | 0 | 109 | unrun |
| `tests/fake/test_conversation_pools.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_conversation_wakes_fake.py` | 4 | 0 | 0 | 0 | 4 | unrun |
| `tests/fake/test_crash_matrix.py` | 9 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_daemon_contract.py` | 21 | 0 | 0 | 0 | 21 | unrun |
| `tests/fake/test_daemon_descriptors.py` | 13 | 0 | 0 | 0 | 13 | unrun |
| `tests/fake/test_daemon_launch.py` | 9 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_daemon_stacks.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_descriptor_exhaustion.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_detached_cwd_pins.py` | 18 | 0 | 0 | 0 | 18 | unrun |
| `tests/fake/test_detached_retention_fence.py` | 89 | 0 | 0 | 0 | 89 | unrun |
| `tests/fake/test_enrollment_guardian.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_fake_provider.py` | 13 | 0 | 0 | 0 | 13 | unrun |
| `tests/fake/test_finalization_replay.py` | 10 | 0 | 0 | 0 | 10 | unrun |
| `tests/fake/test_folder_spelling.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/fake/test_frozen_stream_nonregular.py` | 140 | 0 | 0 | 0 | 140 | unrun |
| `tests/fake/test_gate_end_to_end.py` | 11 | 0 | 0 | 0 | 11 | unrun |
| `tests/fake/test_identity_rollback.py` | 5 | 0 | 0 | 0 | 5 | unrun |
| `tests/fake/test_idle_cost.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_interactive_claude_titles.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_interactive_steer.py` | 13 | 0 | 0 | 0 | 13 | unrun |
| `tests/fake/test_lane_fault.py` | 20 | 0 | 0 | 0 | 20 | unrun |
| `tests/fake/test_lane_pilot.py` | 24 | 0 | 0 | 0 | 24 | unrun |
| `tests/fake/test_lanes_enroll.py` | 15 | 0 | 0 | 0 | 15 | unrun |
| `tests/fake/test_lanes_reenroll.py` | 19 | 0 | 0 | 0 | 19 | unrun |
| `tests/fake/test_lanes_reenroll_keychain.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_launch_env_daemon.py` | 49 | 0 | 0 | 0 | 49 | unrun |
| `tests/fake/test_launch_env_providers.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_launch_gates_above_stdio.py` | 9 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_lost_acknowledgement.py` | 3 | 0 | 0 | 0 | 3 | unrun |
| `tests/fake/test_main_refusal_write_dir.py` | 5 | 0 | 0 | 0 | 5 | unrun |
| `tests/fake/test_mcp_job_contract.py` | 12 | 0 | 0 | 0 | 12 | unrun |
| `tests/fake/test_native_maintenance_startup.py` | 41 | 0 | 0 | 0 | 41 | unrun |
| `tests/fake/test_notice_delivery.py` | 15 | 0 | 0 | 0 | 15 | unrun |
| `tests/fake/test_notice_header.py` | 23 | 0 | 0 | 0 | 23 | unrun |
| `tests/fake/test_picker_native.py` | 3 | 0 | 0 | 0 | 3 | unrun |
| `tests/fake/test_policy_sandbox.py` | 25 | 0 | 0 | 0 | 25 | unrun |
| `tests/fake/test_probe_cancel.py` | 24 | 0 | 0 | 0 | 24 | unrun |
| `tests/fake/test_probe_recovery.py` | 9 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_probe_turn.py` | 43 | 0 | 0 | 0 | 43 | unrun |
| `tests/fake/test_prompt_seam.py` | 8 | 0 | 0 | 0 | 8 | unrun |
| `tests/fake/test_provider_active_cap_fake.py` | 60 | 0 | 0 | 0 | 60 | unrun |
| `tests/fake/test_provider_cap_review_evidence.py` | 5 | 0 | 0 | 0 | 5 | unrun |
| `tests/fake/test_provider_qos.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_record_secrecy.py` | 44 | 0 | 0 | 0 | 44 | unrun |
| `tests/fake/test_request_routing.py` | 3 | 0 | 0 | 0 | 3 | unrun |
| `tests/fake/test_reserve_authorization.py` | 12 | 0 | 0 | 0 | 12 | unrun |
| `tests/fake/test_reserve_authorization_validation.py` | 11 | 0 | 0 | 0 | 11 | unrun |
| `tests/fake/test_reset_credit_incident.py` | 19 | 0 | 0 | 0 | 19 | unrun |
| `tests/fake/test_resume_cli.py` | 1 | 0 | 1 | 0 | 0 | skipped |
| `tests/fake/test_resume_contract.py` | 23 | 0 | 0 | 0 | 23 | unrun |
| `tests/fake/test_retention_dormant.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/fake/test_review130_ab.py` | 12 | 0 | 0 | 0 | 12 | unrun |
| `tests/fake/test_review_r185_adversarial.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_route_view.py` | 8 | 0 | 0 | 0 | 8 | unrun |
| `tests/fake/test_routing_end_to_end.py` | 16 | 0 | 0 | 0 | 16 | unrun |
| `tests/fake/test_routing_tier_preferences.py` | 5 | 0 | 0 | 0 | 5 | unrun |
| `tests/fake/test_salvage_adversarial.py` | 8 | 0 | 0 | 0 | 8 | unrun |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 | 0 | 0 | 47 | unrun |
| `tests/fake/test_sessions_end_to_end.py` | 33 | 0 | 0 | 0 | 33 | unrun |
| `tests/fake/test_state_contract.py` | 37 | 37 | 0 | 0 | 0 | complete; green |
| `tests/fake/test_state_fixture_processes.py` | 4 | 0 | 0 | 0 | 4 | unrun |
| `tests/fake/test_status_payload.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_suggestions_fake.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_tier_hold_by_demand.py` | 27 | 0 | 0 | 0 | 27 | unrun |
| `tests/fake/test_timers_busy_admission.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_timers_end_to_end.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/fake/test_turn_listing.py` | 9 | 0 | 0 | 0 | 9 | unrun |
| `tests/fake/test_turn_retention_cwd.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_turn_retention_cwd_properties.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_turn_retention_job_directory.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/fake/test_turn_retention_job_properties.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_turn_retention_quarantine.py` | 10 | 0 | 0 | 0 | 10 | unrun |
| `tests/fake/test_turn_trees.py` | 18 | 0 | 0 | 0 | 18 | unrun |
| `tests/fake/test_turn_wait_reasons.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/fake/test_unadmittable_pin_admission.py` | 24 | 0 | 0 | 0 | 24 | unrun |
| `tests/fake/test_unadmittable_pin_rereview.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/fake/test_unadmittable_pin_review.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/fake/test_workspace_contract.py` | 8 | 0 | 0 | 0 | 8 | unrun |
| `tests/fake/test_workspace_enospc.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/fake/test_workspace_transient.py` | 24 | 0 | 0 | 0 | 24 | unrun |
| `tests/fake/test_writable_fanout.py` | 28 | 0 | 0 | 0 | 28 | unrun |
| `tests/fake/test_writer_retention_fence.py` | 23 | 0 | 0 | 0 | 23 | unrun |
| `tests/unit/test_admission_uncapped.py` | 27 | 27 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_agent_secret_credentials.py` | 7 | 7 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_app_cutover_daemon.py` | 13 | 13 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_approval_commit.py` | 6 | 6 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_approval_resolution.py` | 33 | 33 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_boot_identity.py` | 10 | 10 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_capacity_desktop.py` | 20 | 20 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 10 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_capacity_steer_route.py` | 22 | 22 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_capacity_view.py` | 20 | 20 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_census_counted.py` | 5 | 5 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_census_identity_uncertainty.py` | 8 | 8 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_census_procargs.py` | 52 | 52 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_census_recycled_pid.py` | 25 | 25 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_census_responsibility_failures.py` | 20 | 20 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_census_responsibility_identity.py` | 17 | 17 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_census_responsibility_races.py` | 34 | 34 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_census_survivors_review.py` | 14 | 14 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_ci_retry_known_flakes.py` | 72 | 72 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_ci_shard_plugin.py` | 48 | 48 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_adapter.py` | 170 | 170 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_attest.py` | 19 | 19 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_cards.py` | 96 | 96 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_cards_wiring.py` | 15 | 15 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 8 | 8 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_identity.py` | 30 | 30 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_large_stream.py` | 30 | 30 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_mcp.py` | 32 | 32 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 11 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_stream.py` | 60 | 60 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_synthetic_error.py` | 63 | 63 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_turn.py` | 61 | 61 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_usage.py` | 28 | 28 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_claude_v1_parity.py` | 6 | 6 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_cli.py` | 247 | 247 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_cli_batch.py` | 18 | 18 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_cli_turns.py` | 23 | 23 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_client_availability.py` | 22 | 22 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_codex_adapter.py` | 172 | 172 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_codex_ca_bundle.py` | 46 | 46 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 6 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_codex_probe.py` | 55 | 55 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_codex_shim.py` | 31 | 31 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_codex_turn.py` | 58 | 58 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_codex_turn_completed.py` | 41 | 41 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_compat.py` | 1615 | 1615 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_continuation_r2.py` | 11 | 11 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_catalog.py` | 15 | 15 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 21 | 21 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_connections.py` | 9 | 9 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_diff.py` | 39 | 39 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_fifo_reads.py` | 26 | 26 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_handoff.py` | 48 | 48 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_handoff_briefs.py` | 28 | 28 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 9 | 9 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_history.py` | 22 | 22 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_peers_attachments.py` | 44 | 44 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_reconcile.py` | 36 | 36 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_redact.py` | 9 | 9 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_service.py` | 98 | 98 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_state_reads.py` | 18 | 18 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_status.py` | 9 | 9 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_steer_service.py` | 45 | 45 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_store.py` | 32 | 32 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_stream_assertion.py` | 13 | 13 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_titles.py` | 89 | 89 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_waits.py` | 9 | 9 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_conversation_wakes.py` | 22 | 22 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_cpu_scaling.py` | 2 | 2 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_daemon_accept.py` | 5 | 5 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_daemon_connections.py` | 31 | 31 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_daemon_fifo_reads.py` | 27 | 27 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_daemon_init_release.py` | 2 | 2 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 22 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_daemon_inspection_properties.py` | 4 | 4 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_daemon_integration.py` | 7 | 7 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_daemon_log_scrubbing.py` | 86 | 86 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_daemon_qos_tools.py` | 4 | 3 | 0 | 1 | 0 | complete; environment failure |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 8 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_daemon_settle.py` | 61 | 61 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_daemon_verbs.py` | 20 | 15 | 5 | 0 | 0 | complete; skips |
| `tests/unit/test_decision_horizon.py` | 7 | 7 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 56 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_descriptors.py` | 16 | 15 | 0 | 1 | 0 | complete; environment failure |
| `tests/unit/test_descriptors_desktop.py` | 8 | 8 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_desktop_account_cache.py` | 3 | 3 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_desktop_in_use.py` | 13 | 13 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_desktop_ledger.py` | 4 | 4 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_disk_admission.py` | 27 | 27 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_disk_floor.py` | 60 | 60 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_doctor.py` | 44 | 44 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_doctor_ca_bundle.py` | 16 | 16 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_doctor_identity.py` | 13 | 13 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_empty_provider_results.py` | 334 | 334 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_enrollment_runner.py` | 7 | 7 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_failed_keep.py` | 2 | 2 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_failed_keep_layout.py` | 4 | 4 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_failed_keep_prune.py` | 11 | 11 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_failed_keep_teardown.py` | 4 | 4 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_fake_codex_http.py` | 7 | 7 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_filesystem_identity_agreement.py` | 40 | 37 | 3 | 0 | 0 | complete; skips |
| `tests/unit/test_folders.py` | 23 | 23 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_forget_paced.py` | 2 | 2 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_fresh_until.py` | 1 | 1 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_frontend_probe_cache.py` | 4 | 4 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_frozen_provider_streams.py` | 217 | 217 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_gate_admission.py` | 24 | 24 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_gate_merge.py` | 59 | 59 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_gate_replay.py` | 23 | 23 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_gate_revision.py` | 16 | 16 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_gate_service.py` | 25 | 25 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_gate_verdict.py` | 45 | 45 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_guard_overlay.py` | 22 | 22 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_guard_trust.py` | 98 | 98 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_guardian.py` | 12 | 12 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_guardian_qos.py` | 14 | 14 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_handler_refusals.py` | 317 | 317 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_held_waiter_holdback.py` | 6 | 6 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_hidden_environment.py` | 7 | 7 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_hook_cost.py` | 5 | 5 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_hooks.py` | 73 | 72 | 1 | 0 | 0 | complete; skips |
| `tests/unit/test_hooks_native_guard.py` | 18 | 18 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_host_push.py` | 118 | 102 | 0 | 0 | 16 | partial |
| `tests/unit/test_host_push_admission.py` | 15 | 0 | 0 | 0 | 15 | unrun |
| `tests/unit/test_host_push_build.py` | 11 | 0 | 0 | 0 | 11 | unrun |
| `tests/unit/test_host_push_build_budget.py` | 15 | 0 | 0 | 0 | 15 | unrun |
| `tests/unit/test_host_push_cli.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_host_push_config_cost.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_host_push_copy.py` | 11 | 0 | 0 | 0 | 11 | unrun |
| `tests/unit/test_host_push_export.py` | 10 | 0 | 0 | 0 | 10 | unrun |
| `tests/unit/test_host_push_intake.py` | 13 | 0 | 0 | 0 | 13 | unrun |
| `tests/unit/test_host_push_metadata.py` | 33 | 0 | 0 | 0 | 33 | unrun |
| `tests/unit/test_host_push_mutations.py` | 24 | 0 | 0 | 0 | 24 | unrun |
| `tests/unit/test_host_push_names.py` | 13 | 0 | 0 | 0 | 13 | unrun |
| `tests/unit/test_host_push_quarantine_restart.py` | 8 | 0 | 0 | 0 | 8 | unrun |
| `tests/unit/test_host_push_restart.py` | 4 | 0 | 0 | 0 | 4 | unrun |
| `tests/unit/test_host_push_retention.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_host_push_storage_recovery.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_host_push_storage_retry_budget.py` | 60 | 0 | 0 | 0 | 60 | unrun |
| `tests/unit/test_host_push_store_completion_pr183.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_host_push_store_interleavings.py` | 26 | 0 | 0 | 0 | 26 | unrun |
| `tests/unit/test_host_push_trees.py` | 5 | 0 | 0 | 0 | 5 | unrun |
| `tests/unit/test_host_push_validated_top.py` | 8 | 0 | 0 | 0 | 8 | unrun |
| `tests/unit/test_importer.py` | 148 | 0 | 0 | 0 | 148 | unrun |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 | 0 | 25 | unrun |
| `tests/unit/test_keychain_exec_guard_properties.py` | 3 | 0 | 0 | 0 | 3 | unrun |
| `tests/unit/test_lane_answers.py` | 27 | 0 | 0 | 0 | 27 | unrun |
| `tests/unit/test_lanes_transfer.py` | 79 | 0 | 0 | 0 | 79 | unrun |
| `tests/unit/test_launch_env_allowlist.py` | 27 | 0 | 0 | 0 | 27 | unrun |
| `tests/unit/test_launch_env_helpers.py` | 20 | 0 | 0 | 0 | 20 | unrun |
| `tests/unit/test_launch_env_mutations.py` | 41 | 0 | 0 | 0 | 41 | unrun |
| `tests/unit/test_launch_env_policy.py` | 26 | 0 | 0 | 0 | 26 | unrun |
| `tests/unit/test_launch_env_round2.py` | 67 | 0 | 0 | 0 | 67 | unrun |
| `tests/unit/test_launch_env_round3.py` | 69 | 0 | 0 | 0 | 69 | unrun |
| `tests/unit/test_launch_env_round4.py` | 40 | 0 | 0 | 0 | 40 | unrun |
| `tests/unit/test_launch_env_round5.py` | 39 | 0 | 0 | 0 | 39 | unrun |
| `tests/unit/test_lease_waiter_selection.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/unit/test_legacy_hold.py` | 56 | 0 | 0 | 0 | 56 | unrun |
| `tests/unit/test_limit_from_model_text.py` | 95 | 0 | 0 | 0 | 95 | unrun |
| `tests/unit/test_live_store_guard.py` | 324 | 0 | 0 | 0 | 324 | unrun |
| `tests/unit/test_live_store_guard_round10.py` | 60 | 0 | 0 | 0 | 60 | unrun |
| `tests/unit/test_live_store_guard_round10_mutations.py` | 13 | 0 | 0 | 0 | 13 | unrun |
| `tests/unit/test_live_store_guard_round3.py` | 22 | 0 | 0 | 0 | 22 | unrun |
| `tests/unit/test_live_store_guard_round4.py` | 8 | 0 | 0 | 0 | 8 | unrun |
| `tests/unit/test_live_store_guard_round5.py` | 11 | 0 | 0 | 0 | 11 | unrun |
| `tests/unit/test_live_store_guard_round6.py` | 112 | 0 | 0 | 0 | 112 | unrun |
| `tests/unit/test_live_store_guard_round7.py` | 50 | 0 | 0 | 0 | 50 | unrun |
| `tests/unit/test_live_store_guard_round7_mutations.py` | 23 | 0 | 0 | 0 | 23 | unrun |
| `tests/unit/test_live_store_guard_round8.py` | 100 | 0 | 0 | 0 | 100 | unrun |
| `tests/unit/test_live_store_guard_round8_mutations.py` | 4 | 0 | 0 | 0 | 4 | unrun |
| `tests/unit/test_live_store_guard_round9.py` | 89 | 0 | 0 | 0 | 89 | unrun |
| `tests/unit/test_live_store_guard_round9_mutations.py` | 12 | 0 | 0 | 0 | 12 | unrun |
| `tests/unit/test_live_store_guard_state_root.py` | 22 | 0 | 0 | 0 | 22 | unrun |
| `tests/unit/test_lockwatch.py` | 20 | 0 | 0 | 0 | 20 | unrun |
| `tests/unit/test_mcp_cli_contract.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/unit/test_mcp_scheduler_contract.py` | 5 | 0 | 0 | 0 | 5 | unrun |
| `tests/unit/test_mirror_flags_faults.py` | 29 | 0 | 0 | 0 | 29 | unrun |
| `tests/unit/test_mirror_flags_model.py` | 12 | 0 | 0 | 0 | 12 | unrun |
| `tests/unit/test_mirror_flags_stateful.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_native_operations.py` | 28 | 0 | 0 | 0 | 28 | unrun |
| `tests/unit/test_notice_rendering.py` | 21 | 0 | 0 | 0 | 21 | unrun |
| `tests/unit/test_notify_push.py` | 31 | 0 | 0 | 0 | 31 | unrun |
| `tests/unit/test_offline.py` | 33 | 0 | 0 | 0 | 33 | unrun |
| `tests/unit/test_operator_hold_closures.py` | 15 | 0 | 0 | 0 | 15 | unrun |
| `tests/unit/test_operator_notices.py` | 79 | 0 | 0 | 0 | 79 | unrun |
| `tests/unit/test_outcome_unknown.py` | 58 | 0 | 0 | 0 | 58 | unrun |
| `tests/unit/test_person_boundary.py` | 97 | 0 | 0 | 0 | 97 | unrun |
| `tests/unit/test_person_boundary_identity.py` | 25 | 0 | 0 | 0 | 25 | unrun |
| `tests/unit/test_picker.py` | 39 | 0 | 0 | 0 | 39 | unrun |
| `tests/unit/test_placement_ramp.py` | 26 | 0 | 0 | 0 | 26 | unrun |
| `tests/unit/test_policy.py` | 178 | 0 | 0 | 0 | 178 | unrun |
| `tests/unit/test_policy_disk.py` | 57 | 0 | 0 | 0 | 57 | unrun |
| `tests/unit/test_policy_ramp.py` | 46 | 0 | 0 | 0 | 46 | unrun |
| `tests/unit/test_policy_support.py` | 14 | 0 | 0 | 0 | 14 | unrun |
| `tests/unit/test_policy_tier_preferences.py` | 15 | 0 | 0 | 0 | 15 | unrun |
| `tests/unit/test_pr124_fixes.py` | 10 | 0 | 0 | 0 | 10 | unrun |
| `tests/unit/test_pr124_review_r2_fixes.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/unit/test_pr192_review_r1.py` | 21 | 0 | 0 | 0 | 21 | unrun |
| `tests/unit/test_pr192_review_r2.py` | 9 | 0 | 0 | 0 | 9 | unrun |
| `tests/unit/test_procs.py` | 47 | 0 | 0 | 0 | 47 | unrun |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 | 0 | 0 | 322 | unrun |
| `tests/unit/test_provider_active_cap.py` | 50 | 0 | 0 | 0 | 50 | unrun |
| `tests/unit/test_prune_217.py` | 9 | 0 | 0 | 0 | 9 | unrun |
| `tests/unit/test_prune_decision_backup_verification.py` | 23 | 0 | 0 | 0 | 23 | unrun |
| `tests/unit/test_prune_decision_fingerprints.py` | 15 | 0 | 0 | 0 | 15 | unrun |
| `tests/unit/test_prune_decisions.py` | 48 | 0 | 0 | 0 | 48 | unrun |
| `tests/unit/test_prune_restore.py` | 4 | 0 | 0 | 0 | 4 | unrun |
| `tests/unit/test_quota_projection.py` | 28 | 0 | 0 | 0 | 28 | unrun |
| `tests/unit/test_rank_review_report.py` | 3 | 0 | 0 | 0 | 3 | unrun |
| `tests/unit/test_reap_liveness.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_rediscovery_git_trace.py` | 17 | 0 | 0 | 0 | 17 | unrun |
| `tests/unit/test_relay.py` | 24 | 0 | 0 | 0 | 24 | unrun |
| `tests/unit/test_render.py` | 20 | 0 | 0 | 0 | 20 | unrun |
| `tests/unit/test_render_quota_projection.py` | 23 | 0 | 0 | 0 | 23 | unrun |
| `tests/unit/test_reserve_authorization_plumbing.py` | 5 | 0 | 0 | 0 | 5 | unrun |
| `tests/unit/test_reset_credit_claim_timing.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_reset_credit_demand.py` | 68 | 0 | 0 | 0 | 68 | unrun |
| `tests/unit/test_reset_credit_properties.py` | 11 | 0 | 0 | 0 | 11 | unrun |
| `tests/unit/test_reset_credit_review_r1.py` | 22 | 0 | 0 | 0 | 22 | unrun |
| `tests/unit/test_reset_credit_review_r2.py` | 52 | 0 | 0 | 0 | 52 | unrun |
| `tests/unit/test_reset_credit_review_r3.py` | 23 | 0 | 0 | 0 | 23 | unrun |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | 12 | 0 | 0 | 0 | 12 | unrun |
| `tests/unit/test_resource_identity.py` | 10 | 0 | 0 | 0 | 10 | unrun |
| `tests/unit/test_retention_81_notes.py` | 38 | 0 | 0 | 0 | 38 | unrun |
| `tests/unit/test_retention_archive.py` | 109 | 0 | 0 | 0 | 109 | unrun |
| `tests/unit/test_retention_archive_properties.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_retention_case_alias.py` | 56 | 0 | 0 | 0 | 56 | unrun |
| `tests/unit/test_retention_nested_leases.py` | 27 | 0 | 0 | 0 | 27 | unrun |
| `tests/unit/test_retention_ordinary_trees.py` | 4 | 0 | 0 | 0 | 4 | unrun |
| `tests/unit/test_retention_qos.py` | 8 | 0 | 0 | 0 | 8 | unrun |
| `tests/unit/test_retention_readback.py` | 37 | 0 | 0 | 0 | 37 | unrun |
| `tests/unit/test_retention_rediscovery.py` | 17 | 0 | 0 | 0 | 17 | unrun |
| `tests/unit/test_retention_regenerable.py` | 26 | 0 | 0 | 0 | 26 | unrun |
| `tests/unit/test_retention_regenerable_properties.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/unit/test_retention_salvage_properties.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_retention_shared_folders.py` | 62 | 0 | 0 | 0 | 62 | unrun |
| `tests/unit/test_retention_sweep_race.py` | 13 | 0 | 0 | 0 | 13 | unrun |
| `tests/unit/test_retention_turn_contract.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_retention_turn_workdirs.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/unit/test_retention_turns.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_retention_worktrees.py` | 15 | 0 | 0 | 0 | 15 | unrun |
| `tests/unit/test_review130_pass.py` | 21 | 0 | 0 | 0 | 21 | unrun |
| `tests/unit/test_review_pr127_fixes.py` | 12 | 0 | 0 | 0 | 12 | unrun |
| `tests/unit/test_review_pr127_history.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/unit/test_review_pr127_pr_wakes.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_review_pr127_report.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/unit/test_review_pr127_requests.py` | 17 | 0 | 0 | 0 | 17 | unrun |
| `tests/unit/test_review_pr127_service.py` | 8 | 0 | 0 | 0 | 8 | unrun |
| `tests/unit/test_review_r2_repro.py` | 31 | 0 | 0 | 0 | 31 | unrun |
| `tests/unit/test_review_r3_measure.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/unit/test_review_r3_repro.py` | 12 | 0 | 0 | 0 | 12 | unrun |
| `tests/unit/test_review_r4_repro.py` | 27 | 0 | 0 | 0 | 27 | unrun |
| `tests/unit/test_review_r5_repro.py` | 17 | 0 | 0 | 0 | 17 | unrun |
| `tests/unit/test_review_r6_probes.py` | 30 | 0 | 0 | 0 | 30 | unrun |
| `tests/unit/test_review_r6_restart.py` | 5 | 0 | 0 | 0 | 5 | unrun |
| `tests/unit/test_round10_refusal_context.py` | 59 | 0 | 0 | 0 | 59 | unrun |
| `tests/unit/test_route_check.py` | 24 | 0 | 0 | 0 | 24 | unrun |
| `tests/unit/test_salvage.py` | 31 | 0 | 0 | 0 | 31 | unrun |
| `tests/unit/test_salvage_optional_transient.py` | 15 | 0 | 0 | 0 | 15 | unrun |
| `tests/unit/test_salvage_unindexable.py` | 86 | 0 | 0 | 0 | 86 | unrun |
| `tests/unit/test_scheduler.py` | 97 | 0 | 0 | 0 | 97 | unrun |
| `tests/unit/test_scheduler_priority_callers.py` | 9 | 0 | 0 | 0 | 9 | unrun |
| `tests/unit/test_scheduler_split.py` | 3 | 0 | 0 | 0 | 3 | unrun |
| `tests/unit/test_scheduler_tier_preferences.py` | 9 | 0 | 0 | 0 | 9 | unrun |
| `tests/unit/test_scheduler_turns.py` | 10 | 0 | 0 | 0 | 10 | unrun |
| `tests/unit/test_scheduler_weekly.py` | 33 | 0 | 0 | 0 | 33 | unrun |
| `tests/unit/test_scoped_stream_capacity.py` | 17 | 0 | 0 | 0 | 17 | unrun |
| `tests/unit/test_scrub_key_order.py` | 70 | 0 | 0 | 0 | 70 | unrun |
| `tests/unit/test_sessions_cli.py` | 142 | 0 | 0 | 0 | 142 | unrun |
| `tests/unit/test_sessions_handoff.py` | 117 | 0 | 0 | 0 | 117 | unrun |
| `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 0 | 0 | 0 | 153 | unrun |
| `tests/unit/test_sessions_headless_properties.py` | 5 | 0 | 0 | 0 | 5 | unrun |
| `tests/unit/test_sessions_mirror.py` | 41 | 0 | 0 | 0 | 41 | unrun |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | 4 | 0 | 0 | 0 | 4 | unrun |
| `tests/unit/test_sessions_mirror_fifo.py` | 49 | 0 | 0 | 0 | 49 | unrun |
| `tests/unit/test_sessions_mirror_hot_health.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/unit/test_sessions_mirror_hot_progress.py` | 16 | 0 | 0 | 0 | 16 | unrun |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | 22 | 0 | 0 | 0 | 22 | unrun |
| `tests/unit/test_sessions_mirror_inventory.py` | 8 | 0 | 0 | 0 | 8 | unrun |
| `tests/unit/test_sessions_mirror_load_gap.py` | 85 | 0 | 0 | 0 | 85 | unrun |
| `tests/unit/test_sessions_mirror_safety.py` | 28 | 0 | 0 | 0 | 28 | unrun |
| `tests/unit/test_sessions_mirror_write_churn.py` | 3 | 0 | 0 | 0 | 3 | unrun |
| `tests/unit/test_sessions_nudge.py` | 44 | 0 | 0 | 0 | 44 | unrun |
| `tests/unit/test_sessions_registry.py` | 24 | 0 | 0 | 0 | 24 | unrun |
| `tests/unit/test_sessions_revive.py` | 45 | 0 | 0 | 0 | 45 | unrun |
| `tests/unit/test_sessions_revive_reads.py` | 10 | 0 | 0 | 0 | 10 | unrun |
| `tests/unit/test_sessions_transcripts.py` | 71 | 0 | 0 | 0 | 71 | unrun |
| `tests/unit/test_settle_properties.py` | 129 | 0 | 0 | 0 | 129 | unrun |
| `tests/unit/test_soak_tools.py` | 49 | 0 | 0 | 0 | 49 | unrun |
| `tests/unit/test_status_json.py` | 56 | 0 | 0 | 0 | 56 | unrun |
| `tests/unit/test_steer_invariants.py` | 248 | 0 | 0 | 0 | 248 | unrun |
| `tests/unit/test_stop_alarm.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_stop_watchdog.py` | 29 | 0 | 0 | 0 | 29 | unrun |
| `tests/unit/test_stopped_daemon.py` | 72 | 0 | 0 | 0 | 72 | unrun |
| `tests/unit/test_store_contention_queries.py` | 212 | 0 | 0 | 0 | 212 | unrun |
| `tests/unit/test_store_ids.py` | 27 | 0 | 0 | 0 | 27 | unrun |
| `tests/unit/test_store_migration_2.py` | 10 | 0 | 0 | 0 | 10 | unrun |
| `tests/unit/test_store_properties.py` | 3 | 0 | 0 | 0 | 3 | unrun |
| `tests/unit/test_store_readers.py` | 26 | 0 | 0 | 0 | 26 | unrun |
| `tests/unit/test_store_snapshot_plans.py` | 4 | 0 | 0 | 0 | 4 | unrun |
| `tests/unit/test_suggest_cli.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_suggest_hint.py` | 10 | 0 | 0 | 0 | 10 | unrun |
| `tests/unit/test_suggestion_error_boundaries.py` | 22 | 0 | 0 | 0 | 22 | unrun |
| `tests/unit/test_suggestion_properties.py` | 1 | 0 | 0 | 0 | 1 | unrun |
| `tests/unit/test_suggestion_secret_properties.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_suggestion_structural_errors.py` | 40 | 0 | 0 | 0 | 40 | unrun |
| `tests/unit/test_suggestions.py` | 30 | 0 | 0 | 0 | 30 | unrun |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 30 | 0 | 0 | 0 | 30 | unrun |
| `tests/unit/test_suggestions_r3_channels.py` | 14 | 0 | 0 | 0 | 14 | unrun |
| `tests/unit/test_suggestions_review_boundaries.py` | 7 | 0 | 0 | 0 | 7 | unrun |
| `tests/unit/test_suggestions_review_multiline.py` | 6 | 0 | 0 | 0 | 6 | unrun |
| `tests/unit/test_suggestions_review_response.py` | 3 | 0 | 0 | 0 | 3 | unrun |
| `tests/unit/test_test_module_names.py` | 1 | 1 | 0 | 0 | 0 | complete; green |
| `tests/unit/test_tick_prefilter.py` | 30 | 0 | 0 | 0 | 30 | unrun |
| `tests/unit/test_timers_alerts.py` | 17 | 0 | 0 | 0 | 17 | unrun |
| `tests/unit/test_timers_busy_read.py` | 115 | 0 | 0 | 0 | 115 | unrun |
| `tests/unit/test_timers_daemon.py` | 4 | 0 | 0 | 0 | 4 | unrun |
| `tests/unit/test_timers_keepalive.py` | 14 | 0 | 0 | 0 | 14 | unrun |
| `tests/unit/test_timers_probe.py` | 35 | 0 | 0 | 0 | 35 | unrun |
| `tests/unit/test_timers_reset_credits.py` | 47 | 0 | 0 | 0 | 47 | unrun |
| `tests/unit/test_timers_retention.py` | 20 | 0 | 0 | 0 | 20 | unrun |
| `tests/unit/test_title_review_regressions.py` | 4 | 0 | 0 | 0 | 4 | unrun |
| `tests/unit/test_turn_attachment_replay.py` | 13 | 0 | 0 | 0 | 13 | unrun |
| `tests/unit/test_turn_overlap.py` | 10 | 0 | 0 | 0 | 10 | unrun |
| `tests/unit/test_turn_replay.py` | 33 | 0 | 0 | 0 | 33 | unrun |
| `tests/unit/test_turn_runner.py` | 76 | 0 | 0 | 0 | 76 | unrun |
| `tests/unit/test_ultracode_effort.py` | 39 | 0 | 0 | 0 | 39 | unrun |
| `tests/unit/test_unadmittable_pin.py` | 12 | 0 | 0 | 0 | 12 | unrun |
| `tests/unit/test_wait_hub.py` | 44 | 0 | 0 | 0 | 44 | unrun |
| `tests/unit/test_wake_cli.py` | 2 | 0 | 0 | 0 | 2 | unrun |
| `tests/unit/test_weekly_rank_vs_base.py` | 26 | 0 | 0 | 0 | 26 | unrun |

## 8. Delivery and cleanup

The report and PR body are committed with named-file staging only and the requested co-author trailer.
No logs, JSON evidence, bundles or scratch probes are staged. No remote operation or push was performed.
The prescribed scratch folder was removed with the guarded Python `shutil.rmtree` cleanup after its results
were transferred to this report. No real kept-roots folder was removed. Both actual pytest invocations exited;
the unstarted baseline queue was interrupted and returned exit 130. The required `pgrep -fl
'suite_hold.sh|pytest|lockf'` check was attempted, but this sandbox cannot read the process list
(`sysmond service not found`, exit 3). No unrelated process was signalled.

DONE

## After review r1 (hub, 2026-10-22)

Review r1 (`20261022-121038-pr247-review-r1`) approved `cb1803cf6`, with three P3s.
- **The baseline rerun is done:** the review ran it (its hold 3). Every new regression fails at `4f5983f4c`: teardown 4 of 4, pruning 11 of 11, cache 4 of 4, and the layout literal.
- **P3-1, fixed here.** `prune` renamed a folder it claimed but could not fully remove to `.pruning-*`, counted it as removed, and never looked at it again. Now:
  - a claimed path that is no longer a directory (a symlink swapped in) is put back unremoved;
  - so is a folder that `rmtree` leaves partly in place, under its pid name, for a later session to retry;
  - only a folder that is fully gone is reported removed;
  - a claimed `.pruning-<pid>-<pruner>-<hex>` folder left behind by an interrupted pruner is swept by the same rules: its pruner and pid gone, and older than a day.
  - **Tests,** in `tests/unit/test_failed_keep_prune.py`, one per executed input: an unreadable folder inside; a symlink swapped in between the check and the claim; and stale, young, live-pruner and oddly named claimed folders.
  - The docstring and `docs/lanes/README.md` now say exactly this.
- **P3-2, not here:** a test whose *setup* fails keeps nothing, since the keep condition reads only `rep_call`. It predates this PR, and it's a follow-up.
- **P3-3:** this report and the PR body no longer say the commits are unpushed, or that the baseline never ran.

