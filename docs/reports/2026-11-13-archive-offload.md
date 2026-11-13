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
