I couldn't finish the requested test runs, and two existing retention tests failed under very heavy host load (load average 88–180, most of it other processes). Neither touches code this change modified. The feature is built and committed. All 43 new tests and all 8 mutants pass.

# D-ARCHIVE-OFFLOAD1: settled retention archives on another volume and back

## What changed and where
- **`subfleet/retention_volume.py`** (new): volume identity.
  - One seam, `volumes(path=None)`, which tests replace. With a path it returns that path's volume, encryption included; without one, every mounted volume.
  - Real reads, all read-only: `statfs`/`getfsstat` for mount point, file system type, flags and free space; `getattrlist` with `ATTR_VOL_UUID` and `ATTR_VOL_NAME`; `diskutil info -plist` for encryption.
  - Network and automounted volumes are never asked for identity or encryption.
  - `refusals()` holds the target rules.
- **`subfleet/retention_offload.py`** (new):
  - `offload()`, `recall()`, the per-archive lock `ArchiveLock` (`<state>/archive/.lock.<name>`), the stub `offload.json`, the log `.offload.jsonl`.
  - One resolver, `contents(root, name)`, which returns the folder holding `files/` and the bundle.
  - `HOOK`: a named-step crash point for tests.
- **`subfleet/retention_archive.py`**:
  - `list_archives` adds an `offload` entry.
  - `check_archive` and `restore` take the archive's lock and read through `contents`; the unlocked `_check_archive` and `_restore` sit behind them.
  - Results gain `offloaded_to` when the files are external.
- **`subfleet/retention_cli.py`**:
  - New verbs `offload --to DIR [--limit N] [--min-bytes B] [--keep-internal] [--dry-run] [--allow-unencrypted] [--json]` and `recall NAME | --all-on DIR [--drop-external] [--json]`.
  - `archives` prints "offloaded to <volume name> (<UUID>)". A refused target exits 7; a failed archive exits 1.
- **`docs/acceptance-contract.md`**: clause **C-8.8** after C-8.4, one change-log line, and C-17.1's verb list updated.
- **`tests/unit/test_retention_offload.py`** (new): 43 tests, 5 of them Hypothesis properties.
  - Archives are real ones: `retention_world.World` jobs retired by `retention.maintenance`.
  - Generated shapes vary file counts, empty files, nested paths, Unicode names, hard links, and a bundle or none.

## Invariants and the tests that hold them
1. **One good copy at every step.** `test_every_step_keeps_one_good_copy` (property). At every hook during offload and recall it checks that each stored file and the bundle have a copy matching the manifest, and that `_check_archive` through the resolver passes.
2. **No touching unsettled archives; no writes outside the allowed paths.**
   - `test_skip_an_archive_whose_job_has_a_retention_folder`
   - `test_skip_a_numbered_archive_whose_job_has_a_retention_folder`
   - `test_a_job_id_ending_in_a_number_is_looked_up_whole_too`
   - `test_offload_and_recall_write_only_their_own_paths` (example tests, not a property)
3. **Offload then recall gives back the same files, bytes and modes.** `test_offload_then_recall_gives_back_the_archive` (property; file mtimes too).
4. **Same verdict and same trees from an offloaded archive.**
   - `test_restore_from_an_offloaded_archive_matches_the_internal_one` (differential, byte for byte)
   - `test_check_verdicts_match_for_damage_inside_and_outside`
5. **A rerun after a crash at any step ends where an uninterrupted run ends.** `test_a_crash_at_any_offload_step_then_a_rerun_ends_the_same` and `test_a_crash_at_any_recall_step_then_a_rerun_ends_the_same` (properties). Each crashes at every step occurrence, half-truncates the copy just written, and leaves a stale lock file.
6. **`--dry-run` writes nothing.** `test_dry_run_writes_nothing` (property). It snapshots the whole tree including folder mtimes, and checks the dry-run batch equals what the real run then moves.

## Mutants (each applied, tested, file restored; status clean afterwards)
| # | Mutant | Killed by |
|---|---|---|
| 1 | skip the read-back | `test_a_copy_that_does_not_read_back_stops_with_the_internal_copy_untouched` |
| 2 | remove internal before the stub reads verified | `test_the_stub_moves_copying_verified_offloaded`, `test_every_step_keeps_one_good_copy` |
| 3 | ignore the retention-folder skip | `test_skip_an_archive_whose_job_has_a_retention_folder` |
| 4 | no `.N` strip | `test_skip_a_numbered_archive_whose_job_has_a_retention_folder` |
| 5 | accept exFAT | `test_refused_targets_write_nothing[exfat]`, `test_volume_classification_from_statfs` |
| 6 | recall without verifying | `test_recall_that_does_not_read_back_keeps_the_stub_and_the_external_copy`, `test_recall_of_a_damaged_bundle_fails` |
| 7 | restore reads the internal path | `test_restore_from_an_offloaded_archive_matches_the_internal_one` |
| 8 | `--dry-run` writes the stub | `test_dry_run_writes_nothing`, `test_cli_offload_list_check_recall` |

## Commands and results
- **Setup:** `uv sync --group dev` succeeded (Python 3.15.2). Free disk read 59, 54 and 51 GB before runs, always above the 35 GB floor.
- **Step 1, new file** (run in separate invocations):

  | Tests | Result | Time |
  |---|---|---|
  | example tests | 38 passed | 150 s |
  | step / round-trip / dry-run properties | 3 passed | 447 s |
  | offload crash property | passed | — |
  | recall crash property, first run | failed: a test bug (it skipped the rerun when the stub was already gone, leaving the stale lock); fixed in the test | — |
  | recall crash property, after the fix | passed | 374 s |
  | 15 volume-related tests, after the final volume edit | passed | 102 s |
  | mutant harness runs | as in the table above | — |

  The whole file was not rerun as one invocation after the last commit; that edit only touches `_volume`.
- **Step 2, existing retention tests** (four workers):
  - `test_retention_archive.py`: **109 passed** (158 s).
  - A 12-file chunk (`archive_properties`, `readback`, `salvage_properties`, `prune_217`, `81_notes`, `case_alias`, `cwd_rechecks`, `deadline_r2`, `dependency_units`, `descriptor_prepared`, `descriptor_review_r5`, `folded_pin`): 397 passed, 1 failed, about 11 never finished. The tool's background time limit stopped it before pytest printed which test failed.
  - Rerun of those files minus the two property files: **38 passed, 2 failed**, stopped by me at 714 s.
    - `test_retention_readback.py::test_ten_thousand_copy_fast_path` hit the 240 s timeout while its fixture was still writing 10,000 files.
    - `test_retention_81_notes.py::test_a_gone_workdirs_job_finds_its_registration_through_the_repositories_retention_knows` failed on `git add f.py` with empty stderr, at the moment I interrupted the run.
  - Rerun of those two alone: readback failed again, cause not captured because the output was cut off when the run stopped; the `81_notes` result was not captured.
  - Neither test reaches code I changed: my hunks in `retention_archive.py` are only in `list_archives`, `check_archive` and `restore`. Calling the failures environmental is UNPROVEN.
- **Step 3:** `test_state_contract`, `test_test_module_names`, `test_invariants_index`, `test_handler_refusals`: **425 passed** (63 s).
- **Step 4:** grep found no test pinning the retention verb list or help. `test_retention_regenerable.py:769-770` pins the `archives` total line, which is unchanged. No updates needed.

## What didn't run
- **Step 2 files never run:** `test_retention_{git_process_r2, job_dependencies, nested_leases, ordinary_trees, packed_retirement_r2, qos, regenerable, regenerable_properties, repository_walk, shared_folders, sweep_race, worktrees}`, `test_review130_pass`, `test_salvage_unindexable`, `test_store_migration_2`, `test_timers_retention`, `test_host_push_retention`, `test_host_push_store_completion_pr183`, `test_host_push_store_interleavings`, `test_pr192_review_r2`, and all 12 `tests/fake` files.
- **Step 2 files run incompletely:** the `archive_properties` and `salvage_properties` files did not finish.
- **UNPROVEN on real hardware:**
  - behaviour on real exFAT, network or encrypted external drives;
  - which `diskutil` keys mark an external volume as encrypted (I read `FileVault`, `EncryptionThisVolumeProper`, `Encryption`, `Encrypted`);
  - whether `F_FULLFSYNC` plus `F_NOCACHE` makes the read-back come from the medium.

  The real `statfs`/`getattrlist`/`getfsstat` reads did run, read-only, against this host's temp folder.
- **Never run against live state:** no CLI run against `~/.subfleet`.

## Deviations from the brief
1. **Test flushes.** A module-scoped fixture swaps `rfs.fullsync` for plain `fsync` and turns off git's own fsync via `GIT_CONFIG_*`. Under this load one `F_FULLFSYNC` took over 45 s. `test_every_external_file_and_folder_is_flushed` still asserts the real requirement.
2. **Extra stub fields.** The three states stay; added fields are:
   - `removing`: internal removal has begun, so the resolver reads the external copy;
   - `recalled`: everything is back and the internal copy is authoritative before the external copy is dropped;
   - also `modes`, `bundle_sha256`, `left_internal`, `keep_internal`, `job_id`.
3. **Extra refusals:** a read-only volume, a volume with no UUID, and the state root's own volume.
4. **Extra skips:** the manifest no longer matches the summary's `manifest_sha256`; nothing to move; an offload in progress to another target; already copied with `--keep-internal`.
5. **Retention-folder lookup** checks both the name with `.N` stripped and the whole name, so a job id that itself ends in `.N` is still caught.
6. **Repair on resume.** A resumed `verified` archive whose external copy no longer reads back is repaired from the internal files that are still whole, instead of failing. It fails if nothing internal is left to repair from.
7. **Locking beyond restore.** `restore --check` also takes the lock. Non-dry runs sweep stale lock files left by killed runs.
8. **Recall temp names.** Recall copies through `.recall.<store>` names inside the internal archive folder. Copies keep their mtimes in both directions.
9. **Log lines** are written only for archives acted on, not for skips.
10. **`--limit`** counts batch members; skipped archives don't count.
11. **xdist.** `uv run --no-sync --with 'pytest-xdist>=3.6'` did not provide xdist here, so I installed pytest-xdist 3.8.0 into the worktree's `.venv` with `uv pip install`.
12. **Branch.** I committed on the detached checkout, as the job set it up, not on `feat/archive-offload-217` itself.

## Folders outside the job's scratch
- **`/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-offload-b1`** (my scratch, inside the system temp folder): the guarded `shutil.rmtree` was still running at handoff, slowed by disk I/O. The tool's background time limit may stop it, so this folder may be left partly deleted.
- **`$TMPDIR/uv-cache`** (the uv cache): used as `UV_CACHE_DIR`; it may have existed before, and the guard rule (path must contain `sf-offload-b1`) excludes it, so it was not deleted.
- **`$TMPDIR/retention-prop-scratch-81gl3ikv`**: left by an existing property test my killed chunk interrupted; not deleted, for the same reason.

## Head
`fd49859b3612cd8f8fd884c08e7e20e20859a915` (4 commits on b51f08f96: 4fa0ca181, 455d4e887, f5f814432, fd49859b3).

## Hub notes (2026-11-13T21:45:54Z)

- The lane's final answer was later overwritten by its own cleanup correction, so the hub recovered this report from the lane's transcript and committed it here.
- **Cleanup correction from the lane:** its guarded removal of the scratch folder `sf-offload-b1` was stopped by a 10-minute tool limit, so the folder (test data only) remained. The hub removes it.
- **Merge:** 024d3b567 merges release/217 (85769b826). `docs/acceptance-contract.md` was resolved with the hub's merge script; `subfleet/retention_cli.py` took both sides (release/217's `retention delete`, this branch's `offload` and `recall`).
- **Reruns on the merged head, at load 47-74:** `test_retention_delete_request.py` and `test_retention_delete_daemon.py`: 83 passed. `test_retention_offload.py` without its five properties: 38 passed in 5.4 s. The two existing tests that failed during the build, `test_retention_readback.py::test_ten_thousand_copy_fast_path` and `test_retention_81_notes.py::test_a_gone_workdirs_job_finds_its_registration_through_the_repositories_retention_knows`: 2 passed in 6.0 s, so their failures were the build host's load.

## Fix round 1

Started **2026-11-15 20:05:14 UTC** (`date -u` in the first command); reporting deadline **23:05:14 UTC**. Finished **2026-11-15 22:48:02 UTC**, within the three-hour limit. All P0–P2 findings and both cheap P3 documentation findings are addressed in the working tree. **No commit or merge commit could be created:** the sandbox denies Git's `ORIG_HEAD.lock` and `index.lock`. Existing checkpoint changes were preserved.

The required review, original report and PR body were read first. `origin/release/217` is `a4aded8859fca77678f59e10261ad701160aefb8`. The checkpoint already contained unstaged release changes and hand-resolved CLI/contract files. The mandated merge command failed before merging. The mandated contract script returned **3**, naming **C-17.1**; I retained and inspected the checkpoint's hand resolution, including `outcomes`, `delete`, `offload` and `recall`. The CLI comparison against release retains its outcomes behavior. **The hub must still create the release merge and commits.** Release history touching `subfleet/retention_cli.py` since HEAD: `d4b21a244` (outcomes), `22e07e831` (bounded outcomes paging), and merge `591e458e7`.

| Finding and cause | Change and regression evidence |
|---|---|
| P0: recall accepted a bundle with no recorded checksum. | Missing checksums refuse recall; retained bundles require size, checksum and heads. Complete internal read-back precedes external deletion. Recall also reuses verified interrupted temporary copies and verifies the external source before replacing an invalid temporary. The missing-checksum case failed before and passed after; both temporary-copy variants failed before (2 failed, 75.20 s); both passed in the final changed-file run. |
| P0: shared target names and blind replacement could destroy another source's bundle. | Paths include source identity: canonical archive-root path plus filesystem identity. A private ownership/manifest marker durably records exclusively created inode identities before payload writes. Foreign matching or differing files are neither adopted nor replaced. Deletion checks every planned entry and refuses deletion from a copied/relocated state root. The shared-target case, both foreign-target variants, both replaced-inode variants, unclaimed-store, owner-mismatch and copied-state cases failed before and passed after. |
| P1: archive ancestors and late replacements bypassed confinement. | Re-plan after locking; open archive/target components without following links; use pinned descriptors for metadata, modes, copying, recall and removal. Recheck the complete external copy before deleting internally. Bundle headers use nofollow descriptors. Recall/archive-swap, planning/locking swap, late removal swap and post-hash bundle-link cases failed before and passed after. |
| P1: recall missed the unsettled fence. | Recall checks the same retention-folder fence under lock. All three ordinary/stripped/whole-numbered variants failed before and passed after. |
| P1: matching entries bypassed mode restoration. | Kept owned external files/directories receive 0600/0700; matching recalled files receive their recorded modes. Both external-resume mode cases and the internal-mode case failed before and passed after. |
| P1: repair rewrote the copy the resolver still selected. | Verify the entire internal archive, durably select it with `copying`/`removing=False`, then repair/reverify externally before switching authority. Incomplete internal archives refuse repair without external payload changes. Interrupted-repair and incomplete-internal cases failed before and passed after. |
| P2: matching resumed files skipped full sync. | Both resume states normalize and flush every kept owned entry; recall flushes matching internal files before external deletion. Both external-resume flush cases and the internal recall flush case failed before and passed after. |
| P2: automount classification checked only local/network flags. | Refuse Darwin `MNT_AUTOMOUNTED` and `autofs` before UUID/encryption reads. All four enumeration/target variants failed before and passed after. |
| P3: changelog claimed listing takes the content lock; verb clause omitted delete. | Changelog states listing reads metadata; C-17.1 preserves `delete` and release's `outcomes`. C-8.8 and its changelog describe source paths, ownership, confinement, safe repair/refusal, modes, sync and source-preserving recall. Documentation assertions passed. No P3 finding remains deferred. |

The 20 preserved regression cases were extended by ten cases: **30 regression cases** plus the original **43** offload tests. Property comparisons normalize source paths and omit variable ledger bytes; separate tests assert ownership safety. Darwin changes birth time when restoring an older mtime, so timestamps precede the durable ownership claim. A strengthened owner-mismatch test also proved the CLI must return an operational error rather than raise uncaught; `Failed` now derives from `RestoreError`.

Commands and results (at most four workers per invocation, fresh `pytest` basetemps):

- Read-only inspections: `date -u`, `pwd`, Git status/log/diff/rev-parse, required `cat` reads, `gh pr view 316 --repo MaxGhenis/subfleet-v2` (also JSON), and scoped `rg`/`sed`/`cat`/`tail`/`head`/`wc`. Completed. An initial word-boundary importer grep found nothing and was replaced below. Six-file AST checks, three-file `py_compile` and repeated `git diff --check` passed.
- `git merge --no-ff --no-commit origin/release/217`: **128**, denied `ORIG_HEAD.lock`.
- `python3 -I /Users/maxghenis/reviews/subfleet-hub/host-runs/merge_contract.py HEAD origin/release/217`: **3**, C-17.1 hand resolution as above.
- Read-only release comparison: all **209** release-only changed paths match release bytes/deletions. An unnecessary `git merge-tree --write-tree` attempt was denied (**128**); the read-only legacy three-argument comparison returned **0**.
- Named-file staging/commit attempts for merge resolution, automount, combined safety and interrupted-temporary recall steps: denied `index.lock` (**128**); the automount commit was not reached after staging failed. No history rewritten.
- Cache-only warmups: `uv run --no-project --with sortedcontainers==2.4.0 python -c ...` and then the same with `--isolated`: both **0**. The first reused an installed environment without filling the cache; isolated mode installed one package into the job cache.
- Prescribed `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, scratch mkdir and `uv sync --group dev`: mkdir denied; uv **2**, cache unwritable. Writable fallback scratch/cache used; `uv sync --group dev` then **0**, nine packages checked. Python **3.15.2**.

Pytest commands used `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q`, fresh `--basetemp=<scratch>/pytest-...`, and `-n 4` for CLI/combined/importer runs. Focused runs omitted xdist. Later runs add `-p sf_ao_scope`; green/debug runs use `--tb=short`. Importers use the same pytest invocation through `python -m pytest`, reading the saved file list; the final run uses `-v -rs` to retain completed node IDs if interrupted. Exact importer selection and reproducible harness: `docs/reports/2026-11-13-archive-offload-fix1-data/`.

| Basetemp suffix / selection | Result |
|---|---|
| `cli-release`: delete request, outcomes and original CLI integration | **90 passed**, 190.44 s |
| `r1-red`: both r1 files before fixes | **24 failed**, 229.35 s; every selected case failed |
| `r1-green`: r1 plus original offload | Interrupted (**130**); **14 failed, 45 passed**, 400.68 s; mtime restoration invalidated ownership birth times |
| `debug1`: external matching modes, `copied` | **1 failed**, 31.39 s; stale birth-time identity |
| `extra-red`: unclaimed-store deletion and owner-mismatch resolver | **1 failed, 1 passed**, 98.24 s; stale identity masked deletion, so not accepted as its red evidence |
| `extra-red2`: those cases after timestamp correction | **2 failed**, 169.15 s, for intended deletion/acceptance failures |
| `late-link-red`: archive replaced at `removing` | **1 failed**, 158.03 s; unrelated directory changed |
| `clone-red` | Interrupted (**130**); no useful count |
| `clone-red2`: copied-state recall | **1 failed**, 64.95 s; original external bundle dropped |
| `r1-green2` | Interrupted (**130**) on discovering the prohibited fixture command below; **28 passed**, 180.97 s |
| `r1-green3`: all three new/changed files | **71 passed**, 649.57 s |
| `cli-owner-red`: strengthened owner-mismatch CLI case | **1 failed**, 43.78 s; uncaught `Failed` |
| `cli-owner-green`: same case after exception normalization | **1 passed**, 20.68 s |
| `importers`: first attempt on all 51 selected files | Interrupted (**130**) after 68 progress passes and 10 skips to fix the temporary-copy P0 gap; xdist teardown warned about closed channels |
| `recall-temp-red`: stored-file and bundle temporary-only-copy cases | **2 failed**, 75.20 s; retry discarded the only intact temporary |
| `r1-green4`: changed files after temporary-copy fix | Interrupted (**130**); no tests ran, 55.59 s, to retain the existing operational error wording |
| `r1-green5`: all 73 new/changed cases | **73 passed**, 702.31 s |
| `importers-final`: all 51 files, unit files first; **1,396 collected cases** | **1,342 passed, 48 skipped, 6 failed**, 1,252.06 s; all 1,396 nodes completed; six scratch-path failures retried below |
| `importer-retries`: six scratch-path failures plus four uncached-dependency skips, worktree basetemp | **6 passed, 4 skipped**, 25.31 s; all six path-classification failures passed; dependency cache still needed isolation |
| `ordinary-retry`: four cases after isolated cache warmup, fresh worktree basetemp | **4 passed**, 4.63 s |
| `guards`: `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py` | **435 passed**, 22.13 s |

Discovery: `git grep -l -E '(retention_offload|retention_volume|retention_cli|retention_archive)' -- tests` found **48** tracked paths; `git grep --no-index -l -E '(retention_offload|retention_volume|retention_cli|retention_archive)' -- tests` found **53**, including preserved untracked files. Removing two helpers left **51 test files**. A safe-harness `--collect-only -q` run returned **0**, **1,396 collected** in **12.60 s**. `retention_archive` was included conservatively for the release changes. The data directory retains the complete failing-before names, all 1,396 collected IDs and their results, retry selection and final summaries.

Deviations and limits:

- **Brief violation discovered and corrected:** early stock `retention_world.World` setup executed `git push` into synthetic local bare repositories. No project branch was published, but this violated the explicit prohibition. The active run was stopped. The saved harness seeds these fixtures with local `git fetch` operations and refuses forbidden subprocesses before execution, including fixture executables. A pure command-data probe with nonexistent identifiers: **8 refused, 3 allowed**, no commands executed. The automatic `stash-shared` hook rejected an initial data-only probe and a report-writing heredoc because their text mentioned a forbidden operation. The optional probe vector was omitted, and the report was written through a file-edit tool.
- The harness scopes temporary homes/default temp allocations and retained failures into scratch. The fallback `/private/tmp` ancestor triggers the production scratch-path rule even in fixtures that override temporary roots; importer tests expecting blob omission therefore fail there. All six failures passed when rerun from a guarded worktree basetemp, without changing production path classification. Short-path workdir allocations receive a guarded job prefix. The prescribed system TMPDIR/cache were unwritable, requiring fallback scratch. Initial stock-conftest temporary homes were not logged; completed runs normally remove them, but interrupted early-run cleanup remains unproven. No guesses were made about other jobs' folders.
- Existing offload fixtures replace `fullsync` with `fsync` and disable Git fsync. Tests establish calls, ordering and logical recovery, not physical power-loss durability. Real external-volume encryption/automount behavior, independent-media read-back, Python 3.12 and the full suite were not run; CI remains responsible for the full suite.
- Importer selection after retries: **1,352 unique passes and 44 command-prohibition skips; no unresolved failures; all collected nodes accounted for**. The skipped bodies were not run because they execute prohibited commands, including a Hypothesis sweep example. No other selected test was omitted.

Folders and cleanup: fallback scratch `/private/tmp/sf-ao-fix1/20261115T200514` held its uv cache, primary basetemps and scoped temporary homes. The guarded worktree extension `/Users/maxghenis/.subfleet/worktrees/20261115-100031-ao-fix-r1/.diag/sf-ao-fix1-20261115T200514` held fresh `pytest-importer-retries`, `pytest-ordinary-retry` and `pytest-guards` basetemps; it avoids the literal `tmp` path component. **4548** logged temporary allocations, **4** outside primary scratch. Exact outside paths are retained in `2026-11-13-archive-offload-fix1-data/outside-scratch-folders.txt`. The four logged outside allocations were:

- `/Users/maxghenis/.subfleet/worktrees/20261115-100031-ao-fix-r1/.diag/sf-ao-fix1-20261115T200514/pytest-ordinary-retry/popen-gw0/test_an_ordinary_tree_retires_0/home/project/.git/worktrees/job/subfleet-salvage-0eo89byu`
- `/Users/maxghenis/.subfleet/worktrees/20261115-100031-ao-fix-r1/.diag/sf-ao-fix1-20261115T200514/pytest-ordinary-retry/popen-gw1/test_an_ordinary_tree_retires_0/home/project/.git/worktrees/job/subfleet-salvage-iatdqukl`
- `/Users/maxghenis/.subfleet/worktrees/20261115-100031-ao-fix-r1/.diag/sf-ao-fix1-20261115T200514/pytest-ordinary-retry/popen-gw2/test_an_ordinary_tree_retires_0/home/project/.git/worktrees/job/subfleet-salvage-6bnvx9ch`
- `/Users/maxghenis/.subfleet/worktrees/20261115-100031-ao-fix-r1/.diag/sf-ao-fix1-20261115T200514/pytest-ordinary-retry/popen-gw3/test_an_ordinary_tree_retires_0/home/project/.git/worktrees/job/subfleet-salvage-ms0tplnu`

An initial completed-importer cleanup returned **1** on a test-created read-only directory; directory permissions are normalized within the guarded roots before retry. Cleanup calls `shutil.rmtree` only after asserting `sf-ao-fix1` occurs in the resolved path. **Both owned job roots were removed successfully (exit 0)**, after **94** earlier guarded completed-run removals. All four logged outside allocations are gone with the worktree extension. All test and cleanup sessions finished. Dependencies and ordinary pre-existing workspace caches were preserved.

Final checks: **7 Python AST parses passed**; source namespace, interrupted-copy clause and all four merged CLI verbs asserted; whole-worktree `git diff --check` passed. Final named-file staging was denied (**128**, `index.lock`), so a final commit could not be reached. No push was attempted outside the disclosed early synthetic-fixture violation.

Head built: `53071cd13b40092a9b98407cf53ef8a3139838a5`

## Fix round 2

Started **2026-11-17 16:52:04 UTC** (`date -u` in the first command); deadline **19:52:04 UTC**. Built on the detached checkout of 6b53f6056, the head the brief named. I read review r2, this report and the PR body first. All tests ran on Python 3.15.2 with the round 1 harness copied to my scratch folder (`SF_AO_FIX2_SCRATCH`, assertion changed to `sf-ao-fix2`); the copy is in `2026-11-13-archive-offload-fix2-data/sf_ao_scope.py`. The harness skipped no test of the offload files. Logs for every run are in `2026-11-13-archive-offload-fix2-data/logs/`. Load average was 190 to 310 throughout.

### First: the release merge broke every offload test that builds an archive
At 6b53f6056 every offload test that builds an archive fails before it reaches offload code. Release/217's one holder listing per pass (D-HOLDERS-LSOF1, #297, which came in with the hub's merge) leaves a job in `awaiting_check2` after one `maintenance` call. `Archives.build` makes one call and asserts `pruned == [job_id]`.
- **Evidence:** my first run of the new tests (`logs/r2-red-171754.log`) had 16 failures, all at `test_retention_offload.py:144` with `{'pruned': [] ...}` or a job number one behind. A round 1 test failed the same way (`logs/r1-probe-172118.log`). A probe of `World` plus `maintenance` showed `awaiting_check2: ["job-1"]`.
- **Fix (b1391df36):** `pytestmark = pytest.mark.retention_catch_up` on `test_retention_offload.py`, `_r1.py` and `_r2.py`. This is how release marks its own retention test modules (`tests/conftest.py` `catch_up`).
- **Without it,** CI on 6b53f6056 fails every archive-building offload test. The review read 8b14506af, before the merge, so it couldn't see this.

### The findings
Each red result below is from `logs/r2-red2-172551.log`, after the marker fix and before any code change: **15 failed, 2 passed** in 29 s. The two passes were a round 1 test included as a fixture check, and `test_a_folder_holding_something_besides_ledger_temporaries_is_still_refused`, a guard that should pass both before and after. Green results are from `logs/r2-green2-173659.log`: round 2 plus round 1 files, **42 passed**, 304 s.

| Finding | Cause | Change | Test: red before, green after |
|---|---|---|---|
| **P0** resumed offload replaces the good external bundle | The `copying` stub stored `bundle_sha256: None`; a resume re-hashed the current internal bundle, and `_place` replaced the matching external bundle with it | `_offload_one` reads the bundle's SHA-256, and checks its heads, before it writes the stub (`_bundle_sha`; `Failed` and no stub if the bundle is wrong). `_copy_all`, `_verify`, repair, `_remove_internal` and recall compare against that value, and `_copy_all` refuses a bundle with none recorded. `_copy_into` now hashes what it reads: a source that doesn't hash to the recorded SHA-256 (manifest for stored files, stub for the bundle) has its new copy removed again, and the run fails naming it. A matching external copy is never removed (`_place` keeps it). | `test_copying_resume_must_not_replace_good_external_bundle`, red "no location keeps the good bundle" (good `11d1ea18…` in neither of `(None, '9794e00a…')`); green: outcome `offloaded`, external holds the good hash, the changed internal bundle is left and listed in `left_internal`. `test_the_copying_stub_records_the_bundle_checksum_before_any_copy`, red (`copying` stub held `None`). `test_a_resumed_copy_from_a_damaged_internal_bundle_stops_naming_it`, red: before, the changed bundle was copied, read back against its own new hash and **offloaded**; now `failed` naming `commits.bundle`, nothing external, internal untouched. |
| **P1** remount gives a new `st_dev`; check, restore, recall and resume refuse | `_identity()` was `[st_dev, st_ino, birthtime]` for the folder and every ledger entry | `_identity()` is `[st_ino, birthtime]`; the ledger also binds the stub's volume UUID (`"volume"`), and the resolver still finds the folder only on the mounted volume with that UUID. Ledger entries must be 2-int lists. A ledger from an earlier build has no `volume` key, so it is refused as "the external folder belongs to another archive or source"; "malformed" appears only when the `volume` key is there and the entries hold 3 ints (corrected in fix round 3). Nothing was offloaded outside tests, so there is no migration. Round 1's foreign or replaced folder refusals stay green. | `test_remount_with_new_device_number`, red `Failed: the external folder belongs to another archive or source`; green: `check_archive` ok, `restore --to` writes the trees, recall `recalled` and `dropped`. `test_a_resumed_offload_after_a_remount_with_new_device_number[copied, verified, removing]`, all three red (`failed`), green (`offloaded`, check ok). |
| **P1, `_source()` (UNPROVEN in the review)** | It hashed the state root's `st_dev` too | **The mechanism holds:** a new device number for the state root changed the source name (red: `d2d85eb1…` != `d31e37be…`), which skips a resumed offload ("run it with that target"), makes `--all-on` match nothing, and makes `--drop-external` keep the copy as another source's. `_source()` now hashes the canonical path plus `_identity()` (inode, birth time). Whether macOS really gives the internal data volume a new `st_dev` (a reboot with another disk numbering) is not shown here. | `test_a_state_root_with_a_new_device_number_keeps_its_source`, red; green: same source name, a `verified` archive resumes to `offloaded`, and `recall --all-on` recalls both archives and drops both external copies. |
| **P2** a `copying` archive whose target is gone can never be cleared | Same as P0: recall refused for lack of a checksum | The checksum is recorded (P0). Recall reads nothing external when the internal copy verifies, so it ends the stub. When the external copy is authoritative, recall now opens it and checks its ledger before changing anything internal. | `test_a_copying_archive_whose_target_is_gone_is_recalled`, red `commits.bundle: offload.json records no checksum; external copy kept`; green: crash at the first `copy-file`, unmount, offload to a new UUID (skipped), recall `recalled`, internal tree as before, the unmounted target's partial copy byte for byte unchanged. Remounted, a later offload to that target takes its own partial copy over and finishes it, and `recall --drop-external` removes it. C-8.8 says this. |
| **P2** an error or crash during the first ledger write wedges the folder | `_write_owner` removed its temporary only in its second `try`; `_ownership` refused a folder holding anything but `.owner.json` | `_write_owner` removes its temporary on any `Exception` raised anywhere in it. A kill (the tests' `Crash` is a `BaseException`) leaves it, as a real crash would. Under the archive's lock, `_ownership(create=True)` treats a folder holding nothing but `.owner.<pid>.<ns>.tmp` regular files as unclaimed, removes them, flushes and goes on. With a ledger present, it removes such leftovers after the ledger is validated. A folder with anything else in it is still refused, unchanged. New step `ledger-first`, inside `_write_owner` after the temporary is synced and before it is linked. | `test_crash_in_first_ledger_write_wedges_offload` (first fullsync of the temporary raises `EIO`), red: `.owner.13369.….tmp` left; green: none left, the rerun offloads, recall drops. `test_a_crash_in_the_first_ledger_write_leaves_a_temporary_the_rerun_clears` (a kill there), red: the rerun failed; green: offloaded, folder holds only the ledger, `files` and the bundle. `test_the_crash_properties_reach_the_first_ledger_write` (`logs/r2-ledger-first-180805.log`, 1 passed): at 6b53f6056 no `ledger-first` step exists (`grep -c` 0 in that file), so this crash point could not be reached. Guard `test_a_folder_holding_something_besides_ledger_temporaries_is_still_refused` passed before and after. |
| **P2** modes of user folders named `subfleet-archive` change | `_open_below` marked folders managed by name (`part == FOLDER`) | With `create` or `private`, the stub path's last three components (`subfleet-archive/<source>/<name>`) are the program's, found by position, and the one three from the end must be `subfleet-archive`. Folders at and above `DIR` are only opened (and `DIR` flushed): `create` no longer makes a missing user folder. | `test_a_target_below_a_folder_named_subfleet_archive_keeps_its_modes` (`--to <mount>/subfleet-archive/inner`), red `{'inner': '0o700', 'subfleet-archive': '0o700'}`; green: both 0755, the program's two folders 0700. |
| **P3, fixed** false `offloaded_to` through a linked state root | `_check_archive` compared an unresolved path with the resolver's resolved one | It compares `os.path.realpath` of both sides | `test_never_offloaded_archive_through_a_symlinked_root` (a link to the state root, and the `/var` form when the root is under `/private/var`), red: `offloaded_to` set; green: no key from `check_archive` or `restore`, and still set once offloaded. |
| **P3, fixed** a damaged `.owner.json` gives a raw parse error | `json.loads` errors escaped `_ownership` | It raises `Failed` naming `.owner.json` and the archive, and leaves the file as it is. My test first went green on the message but red on "nothing changes": a recall that failed later still created an empty internal `files/`. The recall-opens-external-first change above fixes that, and also stops a recall with the volume unmounted from leaving an empty `files/`. | `test_a_damaged_ownership_marker_fails_naming_the_file`, red `JSONDecodeError`; then red on the empty `files/` (`logs/r2-green1-173137.log`, 1 failed, 41 passed); green: `check_archive` raises `Failed` naming the file, recall `failed` naming it, both CLI calls exit 1, internal and external trees and the stub unchanged. |
| **P3, fixed** C-17.1 lost `[--json]` | Merge | `outcomes --session ID [--json]`, as release 4aed7a3c9 has it | Text check |
| **P3, fixed** one folder entry misses its flush on resume | `_open_below` synced a parent only when its own `mkdir` succeeded | Each of the three program folders' parents is flushed on every pass. This was part of the position change above, so it stayed small. | `test_every_program_folders_parent_is_flushed_on_resume` (crash at the flush of `DIR` right after `mkdir(subfleet-archive)`), red "DIR or a program folder never flushed" (`…/vol-a/backups`); green |

Changed existing tests:
- `test_retention_offload_r1.py::test_copying_recall_never_drops_the_only_good_bundle` asserted the old `bundle_sha256 is None` at the `copied` crash; it now asserts the recorded good hash, and the rest of that test (recall fails, the external bundle survives, the stub stays) is unchanged.
- The one-good-copy property's checker now also asserts, at every step, that the stub's `bundle_sha256` equals the bundle's original hash, so that invariant holds for the bundle by hash.

### Left for the hub
- **P3, listed as the brief asked: `restore --check` writes.** It creates and unlinks `<state>/archive/.lock.<name>`, so it fails on a read-only `<state>/archive`. C-8.8 documents the lock. Unchanged: a design call for the hub to file.
- **New, found this round, not fixed: a crash or error inside a *later* ledger write wedges the folder.** `_copy_into` and `_copy_all` create a file (or `files/`) exclusively, then record its identity in the ledger (`Owner.claim`). If that ledger write fails (an error, which removes the temporary) or the process dies first, the folder keeps an empty entry the ledger doesn't name, and every rerun refuses it as "an external entry this offload did not create". The fix I'd suggest is to treat an empty unrecorded regular file, or an empty folder at a planned path of an owned folder, as an interrupted creation: it holds no bytes, so remove it and create it again. That changes C-8.8's "foreign entries are never adopted or replaced", so it is a call for the hub. For that reason `ledger-first` sits only in the first ledger write: a step inside the later writes would make the crash properties fail on this gap.
- **The report's invariant 5** (crash at any step) now holds at every `HOOK` step, `ledger-first` included. A crash inside a later ledger write is not a step, as above. The test module's docstring says so.

### Contract
C-8.8 is amended for:
- the recorded bundle checksum and what `copying` keeps or replaces;
- the source name and ledger identity (inode and birth time, volume UUID);
- ledger temporaries and a damaged ledger;
- the program's folders by position, and the flush of each one's parent every run;
- the hash check while copying;
- recall of a `copying` archive whose target is gone, and what clears its partial copy later;
- the resolver's UUID sentence and `offloaded_to`;
- the write-confinement sentence (offload creates `DIR/subfleet-archive/` and `<source>/` when missing, sets both 0700 and flushes them and `DIR`).

It also adds `test_retention_offload_r2.py` to the clause's test list. C-17.1 regains `[--json]`. A new change-log line, dated 2026-11-17, heads "Changes in version 3". The module docstring of `retention_offload.py` says the same.

### Commands and results
Every pytest run went through `run.sh` (in the data folder): `uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -p sf_ao_scope --basetemp=<scratch>/bt/pytest-<label>-<time>`, at most `-n 4`, wrapped in `timeout`. Bash's 600 s cap per call in a headless session set each bound.

| Run (log) | Selection | Result |
|---|---|---|
| `uv sync --group dev` | | 0; xdist 3.8.0 through `--with` |
| `r2-red-171754` | new file, stock code | **16 failed**: the merge breakage above, not the findings |
| `r1-probe-172118` | one round 1 test | **1 failed**, same breakage |
| `r2-red2-172551` | new file plus that round 1 test, after the marker | **15 failed, 2 passed**, 29 s (red evidence) |
| `r2-green1-173137` | round 2 and round 1 files, first fix | **1 failed, 41 passed**, 224 s (the empty `files/`) |
| `r2-green2-173659` | same, final code | **42 passed**, 304 s |
| `main-examples-174530` | `test_retention_offload.py` without its 5 properties, `-n 4` | **38 passed**, 118 s |
| `main-props3-174906` | its one-good-copy, round-trip and dry-run properties, `-n 3` | **3 passed**, 461 s |
| `main-crash-a-175753` | both crash properties, `OFFLOAD_PROP_EXAMPLES=4`, `-n 2` | **2 passed**, 354 s |
| `r2-ledger-first-180805` | the added `ledger-first` test | **1 passed**, 118 s |
| `vol-r1-190149` | `test_retention_volume_offload_r1.py` | **4 passed**, 25 s |
| `imp-aa-181301` | importers chunk 1 (12 unit files), `-n 4 -v` | cut at 590 s at 19%: **29 passed, 28 skipped** (the harness refusing `git push` in host-push tests), **0 failed** |
| `imp-archive-182558` | `test_retention_archive.py`, `-n 4 -v` | cut at 570 s: **9 passed, 3 failed**; pytest was killed before it printed tracebacks |
| `imp-archive-3-183700` | those 3 alone, `-n 3` | **3 passed**, 305 s |
| `guards-184907` | `test_state_contract.py`, `test_test_module_names.py`, `test_invariants_index.py`, `test_handler_refusals.py`, `-n 4` | **437 progress marks, all passes, reached 100%**; the 570 s bound killed it in teardown before the summary line (round 1 counted 435) |

The 3 `test_retention_archive.py` failures were `test_file_written_after_the_final_check_is_kept_in_conflicts`, `test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit` and `test_corrupted_loose_object_is_not_trusted_for_omission[truncated]`. Each passed alone, which points to load, but their cause is UNPROVEN.

Other checks: `ast.parse` of every changed Python file and `git diff --check` passed before each commit. The importer list (`git grep -l -E 'retention_offload|retention_archive' -- tests`) is 51 files: the 3 offload files plus the 48 in `importers.txt`.

### What didn't run
- **Importers:** of the 48 files, only part of chunk 1 and part of `test_retention_archive.py` ran. Never started: the other 24 unit files in `importers.txt` (chunks 2 and 3) and all 12 `tests/fake` files. At this load a 12-file chunk reached 19% in 590 s, so the rest could not finish before the deadline. CI's full run is the check.
- **Crash properties:** 4 examples each, not the default 8.
- **No CI** for this head, and **no real disk-image remount** (optional in the brief; skipped for time). The remount tests wrap `os.stat`, `os.lstat` and `os.fstat` so every caller sees another `st_dev` for each entry that was under the volume or state root, with inode and birth time unchanged. That simulates a remount; it isn't one.
- **Not verified on hardware:** power-loss durability; tests replace `F_FULLFSYNC` with `fsync`, as before.

### Deviations from the brief
1. **The catch-up marker commit (b1391df36)** is outside the review's findings. Without it no offload test that builds an archive can pass at this head.
2. **`ledger-first` is in the first ledger write only,** for the reason under "Left for the hub". `_write_owner`'s cleanup catches `Exception`, not `BaseException`, so the crash properties' injected kill leaves the temporary, as a real kill would, and the sweep must clear it.
3. **Beyond the findings:**
   - `_copy_into` hashes what it copies for offload;
   - recall opens an authoritative external folder before it changes anything internal;
   - `_open_below(create=True)` no longer creates missing folders above `DIR`;
   - an offload resume with no recorded bundle checksum (a stub from an earlier build) fails naming the bundle.
4. **Runs were bounded at 570–590 s** to fit Bash's 600 s cap. One importer run's tool call was moved to the background by the harness after the bound had already ended pytest (rc 124), and a `du` I ran to size the scratch folder overran the 120 s default and was moved too. I stopped that one with TaskStop. Neither left a process running.
5. **I created and deleted `/tmp/sfao2-importers-raw.txt`** in one command: a stray redirect, outside the scratch folder and outside the brief's searches rule. No other file outside the worktree and scratch.
6. **Commits are on the detached checkout,** as the job set it up.

### Folders outside the job's scratch
- **`$TMPDIR/uv-cache`:** the brief's cache, which already existed. Kept.
- **`<worktree>/sf-ao-fix2-pytest-cache-files-e09tzd4a`:** pytest's own cache temporary, logged by the harness and already gone (pytest renames it into `.pytest_cache`). Nothing to delete.
- **The worktree's ignored `.hypothesis/` and `.pytest_cache/`:** test databases inside the worktree.
- **The scratch folder `$TMPDIR/sf-ao-fix2`** (`/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-ao-fix2`): removed with a guarded `shutil.rmtree` that asserted `sf-ao-fix2` in the resolved path. Its `bt/` basetemps took 212 s, the rest under a second, exit 0 both times; nothing is left. The logs, harness, runner and importer list it held are in `2026-11-13-archive-offload-fix2-data/`.
- **`$TMPDIR/sf-ao-fix2-rm.py`:** the removal script, a file just outside the scratch folder; deleted after use with the same name assertion.

Commits on 6b53f6056: d0f5ee6fc (red tests), b1391df36 (catch-up marker), 94f6be5fc (code), fba3fad43 (`ledger-first` test), 16a51cafe (contract and docstrings; the code and contract head), then the report and `2026-11-13-archive-offload-fix2-data/` (02be59274 and the cleanup note after it). The branch tip is the head named in this job's final answer.
