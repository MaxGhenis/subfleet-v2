VERDICT #272: REQUEST_CHANGES

Reviewed `e4b20c9e54a8261613ee5a78a72a5f58b489f4c3`, the whole branch against `release/217` at `1ff1fa982`. First recorded clock: 2026-10-29 13:31:48 UTC. Correctness and measurement holds began by 13:37:02 UTC, within the first hour. The requested Darwin scratch directory was denied; isolated scratch is `/private/tmp/sf-r272r3`. No live store, real provider, production CLI, reviewed-repository fetch/merge/push, external application write or sub-agent is used. Fixture Git transport uses only local bare remotes. The two permitted GitHub reads were made once each. Product source and existing review files are unchanged.

Archive fixtures later use a second task-owned scratch directory under the explicitly writable Darwin attempt directory, because the literal `tmp` component in `/private/tmp` defeats that file's non-scratch fixture. The execution ledger records this path-only adjustment and the five affected nodes' successful retries; environment setup is not repeated.

## Findings, ranked

**P2, R3-F1 — resolving a repository alias before the alternates read hides an ancestor symlink swap.** `subfleet/dependencies.py:138` discards the original `.git` spelling before entering `_storage`. The final pathname check at `:246` therefore checks the old resolved repository, not the path Git will resolve from the borrower's folder.

Executed a borrower whose `.git` symlink initially names an outside repository. Immediately after the first real `readline` of that repository's alternates, retarget the symlink to a repository inside an allocated owner's worktree. The old alternates file remains present with exactly the same device, inode, size, nanosecond mtime and nanosecond ctime. Discovery returns only the old Git directory and no `incomplete`. Immediately afterward, real Git in the borrower successfully reads the owner's distinct commit and its `owner-only.txt` content. Maintenance nevertheless returns `pruned: ['owner']` and removes the tree. Both a running borrower and a terminal borrower holding an `out:` lease execute this failure. This happens during the same discovery call and requires no write inside a timestamp tick. The final red/marked/trial holds use this strengthened, owner-only-commit fixture; earlier exploratory holds are retained separately.

Fix: retain the original repository aliases alongside their resolved targets and revalidate them after the storage traversal, outside transactions. A changed or disappeared alias must record incomplete discovery and invoke the existing global allocated-worktree pin. Cover `.git` and relevant ancestor/pointer resolution, rather than checking only a pathname whose alias has already been erased. An in-memory trial checks the original `.git` target after traversal; its results are recorded below. No product fix is applied in this review.

Regression: `tests/fake/test_retention_dependency_review_r3.py::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live]` and `[exporting]`. Both fail with `--runxfail`; both are strict expected failures naming R3-F1 in the delivered file. The assertions establishing unchanged old-file metadata and successful real Git object access pass before the retirement assertion fails.

**P2, R3-F2 — recovery can quarantine before the legacy incomplete-discovery census.** `subfleet/retention.py:539` calls `_recover()` before filling `self.legacy` and `self.ctx.legacy` at `:543`. Recovery of a `locked` journal calls `Retirement.quarantine()` and its dependency check with an empty legacy census. Recorded incomplete evidence is already available from the database, whereas legacy evidence is not discovered until after that move.

Executed a legacy job that predates both passes, first with a valid outside alternates file. Crash immediately before the owner's quarantine, leaving its journal `locked` and its allocated worktree present. Make the legacy alternates malformed between passes. On resumption, the tree is absent immediately after recovery's quarantine (`observations == [False]`). The later census discovers the error and the pass rolls back: the tree is present at return and `pruned` is empty. This finding is a transient loss of the live path, not a claim of permanent deletion. It occurs both while the legacy job is running and while it is terminal with an `out:` lease. Paired modern recorded-incomplete controls prevent the move entirely (`observations == [True]`).

Fix: populate the off-transaction legacy census, including terminal exporters, before recovery can quarantine anything, or conservatively defer those recovery moves until it exists. Carry that same census into both the pass and archive context. The in-memory trial moves the existing census ahead of `_recover`; no product change is applied. The regression `test_c8_4_recovery_discovers_incomplete_before_quarantine` has two legacy cases marked strict expected failures naming R3-F2 and two passing recorded-evidence controls.

## Checks 1–6

1. **Changes required.** The original R2-F1 cases pass. Independent real replacement, rewrite, unlink while open, rename-over, truncate/rewrite to identical size, alternates-symlink swap and `info`-symlink swap all record incomplete evidence. Each runs with a live job and with a terminal pending exporter; each keeps two allocated trees, including one whose row has NULL worktree. Releasing the obligation permits both owners to retire. R3-F1 is a further parent-symlink counterexample. The timestamp-tick blind spot is not the only one. R3-F2 shows recovery can move an allocated tree before discovering legacy incomplete evidence. Filesystem discovery remains outside transactions; dependency rechecks use database reads and string operations.
2. **Pass for R2-F2's SQL and database recheck; changes required for recovery ordering (R3-F2).** `_DISCOVERING` appends an OR to `_LIVE`'s WHERE, so its predicates are live-job OR export-holder. The BINARY key interval is `out:` inclusive through `out;` exclusive. Executed `out:`, `out:/result`, a non-ASCII destination, and negative controls `out;`, `out9`, `outz:/result`. Terminal legacy jobs without `depends` enter discovery only for the three export controls. Existing database/full-pass/already-exporting tests cover transition after census and terminal legacy exporters. Once discovered, legacy incomplete evidence survives the same exporter lifetime as recorded incomplete evidence. R3-F2 is the earlier recovery interval before that census exists. The single scale run is described below.
3. **Pass.** Byte comparison against `7eadc72c7` proves that exactly the three authorized strict expected-failure decorators were removed, covering six cases; the unit review file, review r2 report and its data have no changes. The two fix-r2 and fourteen fix-r1 mutants, plus three focused coverage mutants, run against named killers only. The restored-cap mutant's textual anchor is adapted solely to retain the new before-read fstat; its mutation remains the same cap restoration.
4. **Pass.** Each merge has 255 unique contract clause IDs and no missing IDs. Every resulting clause equals one parent's complete clause. `ad4d612ee` retains branch C-6.5/C-6.11/C-8.3/C-8.4 and release C-23.14/C-25.7; `e4b20c9e5` retains the branch's four retention clauses and release C-26.4. No clause is partially reconstructed or duplicated.
5. **Changes required for observation bounds and recovery.** The descriptor checks and repaired exporter loop are described accurately. Original six-case red/green evidence and dated prior counts remain valid. The broad discovery-change description omits R3-F1, so it cannot imply that the timestamp-tick example is the sole during-discovery gap. Legacy incomplete evidence does not protect recovery's initial moves (R3-F2). Preserve C-8.4's intended fail-closed requirement and repair the implementation. Suggested precise descriptive text and PR replacements follow.
6. **CI pending.** The one allowed `gh pr checks 272` read returned exit 8: pass 1, pending 9, fail 0, skipping 0, cancel 0. Python 3.12 macOS shard 4/5 passed in 25m8s; Python 3.12 shards 1/2/3/5 and all five Python 3.14 shards were pending. This is consistent with the reported macOS congestion; the snapshot does not distinguish queued from running. Neither D-GLIDE-CI1 frontend failure appears. The snapshot is saved beside this report and was not refreshed.

## One measurement run

At 3,000 jobs / 100 live, the fixture has 2,900 terminal legacy exporters, each holding an output lease, 147,800 events, and one malformed alternates file. The legacy census includes all 3,000 jobs; the database recheck includes all 3,000 and carries exactly the census's one incomplete entry. One measurement only: legacy discovery 2,536.671 ms; database recheck 211.028 ms; `_pin_reasons` 255.783 ms. Start 13:37:02 UTC, load 24.33 / 22.67 / 24.41. The census uses a jobs scan and export-holder subquery; submission lookup uses `events_job`, and exporter jobs join through `sqlite_autoindex_jobs_1`. SQLite chooses a leases scan when every lease is an output lease.

The measurement's final assertion incorrectly required every exporter to display `dependency-incomplete`. Existing reason precedence uses the exporter's own lease reason first. That assertion fails; it is a scratch oracle error, not a product finding or a passing scale test. The census, evidence-equality assertions and three timing reads all completed before it. No measurement was repeated or tuned. Independent small-store probes establish global pins on otherwise unpinned owners.

## Text corrections

Apply this scope clarification to the incomplete-discovery descriptions at `docs/acceptance-contract.md:500`, `docs/desktop/retention-archive.md:631`, main report `:9`, and fix-r2 report `:7`/`:12`:

> Discovery compares each opened alternates descriptor's metadata before and after reading and checks the final identity of the resolved pathname it opened. These checks detect the six original review-r2 inputs. They do not revalidate an original `.git` alias resolved before traversal: retargeting that ancestor during the read can leave complete-looking stale evidence and permit retirement (R3-F1). A metadata-preserving write within one timestamp tick and dependencies introduced after discovery are additional observation bounds.

Add this recovery qualification to the desktop description and the current main/fix-r2 report sections until R3-F2 is fixed:

> Once the legacy census exists, rechecks preserve its incomplete evidence for live jobs and pending exporters. At this head, journal recovery runs first and can temporarily quarantine a tree before that census; the later pin restores it but does not prevent the initial move (R3-F2).

C-8.3's terminal-exporter SQL and evidence lifetime after discovery are accurate; its global allocated-worktree guarantee also requires the R3-F2 ordering repair. No additional exporter SQL/loop correction is needed. The fix-r1 report's dated claims and its statement that fix-r2 closes the six r2 inputs are true. Its observation-bounds reference should point to the corrected fix-r2 paragraph. The main report's six-case history and fix-r2's 452-node historical ledger remain dated evidence, rather than claims of coverage for the new parent-alias or recovery inputs.

For the live PR body's first paragraph, replace the unrestricted discovery-change/bounds description and the claim that all text now matches with:

> Fix round r2 resolves review r2's six original inputs and preserves terminal legacy exporters' incomplete evidence once discovered. Review r3 at e4b20c9e5 requests changes for R3-F1, a `.git` ancestor symlink retargeted during discovery that permits retirement, and R3-F2, journal recovery quarantining before the legacy census. The current checks validate the opened descriptor and resolved pathname, not the original repository alias; the legacy census must precede recovery moves. Both repairs and updated descriptions are required. Retention remains dormant; approving review and completed CI remain merge gates.

Keep the original r2 red/green and historical 452-node evidence as dated evidence. Replace the stale second paragraph's “Review r2 ... reviews it next” with “Review r2 completed; its six regression inputs are fixed in round r2. Review r3 requests R3-F1 and R3-F2 repairs.” After this review, replace “Review r3 ... reviews it next” and the Next item with “Address review r3's R3-F1/R3-F2 and rerun its six cases, then obtain approval and complete CI.” The rest of the PR's earlier evidence is explicitly historical.

## Execution accounting

**567 of 568 requested existing nodes passed** across 19 files; the single real-lsof node is unrun under the no-live-process restriction. All 75 planned ordinary holds completed, with 2 additional archive retry holds after correcting the fixture location. The new review file adds **2 passing controls and 4 strict expected failures**; `--runxfail` executes those four failures, each with completed teardown. Separate small-store controls: 20 passed. Trial repairs: 2 alias cases and 4 recovery cases passed. These counts exclude development oracle mistakes and mutant failures.

All times below are UTC on 2026-10-29 and are observed after lock admission; load is the host 1/5/15-minute average. The shared wrapper cannot read sysctl in this sandbox, so the observer records `os.getloadavg()`. Every hold used the shared lock; no outside-lock fallback was needed. Each pytest has a 570-second whole-run alarm and uses normal property settings, with no xdist.

| Requested file | Passed / unrun | First start | Load (1/5/15) |
|---|---:|---|---|
| `tests/fake/test_retention_dependency_review_r2.py` | 21 / 0 | 13:39:44.410 | 18.85 / 22.27 / 24.04 |
| `tests/unit/test_retention_alternates_review_r2.py` | 15 / 0 | 13:40:33.312 | 23.69 / 22.73 / 24.09 |
| `tests/fake/test_retention_dependency_review_r1.py` | 18 / 0 | 13:40:52.027 | 23.29 / 22.72 / 24.05 |
| `tests/fake/test_retention_dependency_incomplete.py` | 19 / 0 | 13:41:14.214 | 20.65 / 22.16 / 23.82 |
| `tests/unit/test_retention_alternates.py` | 23 / 0 | 13:42:13.533 | 17.42 / 21.10 / 23.32 |
| `tests/unit/test_retention_job_dependencies.py` | 12 / 0 | 13:47:59.256 | 21.27 / 21.09 / 22.50 |
| `tests/fake/test_retention_dependencies.py` | 30 / 0 | 13:48:18.763 | 22.89 / 21.42 / 22.58 |
| `tests/fake/test_retention_dependency_spelling.py` | 16 / 0 | 13:51:25.226 | 25.94 / 23.49 / 23.22 |
| `tests/unit/test_retention_dependency_units.py` | 19 / 0 | 13:51:58.586 | 24.92 / 23.58 / 23.27 |
| `tests/unit/test_retention_folded_pin.py` | 38 / 0 | 13:53:32.487 | 16.49 / 21.14 / 22.36 |
| `tests/unit/test_retention_dependency_properties.py` | 4 / 0 | 13:57:26.380 | 17.42 / 19.90 / 21.50 |
| `tests/unit/test_retention_shared_folders.py` | 62 / 0 | 14:08:03.170 | 25.07 / 23.41 / 22.30 |
| `tests/unit/test_retention_archive.py` | 108 / 1 | 14:25:04.225 | 60.09 / 52.27 / 39.06 |
| `tests/unit/test_retention_turns.py` | 7 / 0 | 14:35:32.550 | 32.49 / 35.90 / 37.02 |
| `tests/fake/test_detached_retention_fence.py` | 89 / 0 | 14:35:45.074 | 33.01 / 35.93 / 37.02 |
| `tests/fake/test_writer_retention_fence.py` | 23 / 0 | 14:40:48.791 | 32.06 / 32.84 / 35.24 |
| `tests/fake/test_state_contract.py` | 37 / 0 | 14:41:56.138 | 23.72 / 30.26 / 34.09 |
| `tests/unit/test_test_module_names.py` | 1 / 0 | 14:42:57.795 | 15.41 / 26.60 / 32.44 |
| `tests/unit/test_invariants_index.py` | 25 / 0 | 14:43:03.917 | 15.45 / 26.43 / 32.34 |

New file `tests/fake/test_retention_dependency_review_r3.py`: **2 passed / 4 strict expected failures / 0 unrun**, final marked start 14:42:47.384 UTC, load 16.45 / 27.18 / 32.71. The final alias fixture uses a distinct owner-only commit and checks its content through Git; earlier exploratory holds remain in the ledger.

### Mutants

Every mutation matches once, loads exactly one changed module from the reviewed source and exits 1 with a named call assertion failing and teardown passing. The ordinary green runs validate those killers independently. No source file was modified. The three r3 mutants independently test write metadata, final pathname identity and inclusion of already-terminal legacy exporters.

| Mutant | Executed failing killer | Result | Start | Load |
|---|---|---|---|---|
| `git-storage-no-commondir` | `tests/unit/test_retention_dependency_units.py::test_d1_commondir_outside_the_worktrees_layout_is_followed` | KILLED | 13:46:41.565 | 22.33 / 21.20 / 22.67 |
| `git-storage-no-worktrees-fallback` | `tests/unit/test_retention_dependency_units.py::test_d1_storage_a_gitfile_names_counts_when_it_is_gone` | KILLED | 13:46:54.117 | 20.90 / 20.93 / 22.56 |
| `git-storage-no-alternates` | `tests/unit/test_retention_dependency_units.py::test_d1_alternates_are_followed` | KILLED | 13:46:56.296 | 19.87 / 20.72 / 22.47 |
| `git-storage-alternates-one-level` | `tests/unit/test_retention_dependency_units.py::test_d1_alternates_are_followed` | KILLED | 13:46:58.212 | 19.87 / 20.72 / 22.47 |
| `git-storage-gitfile-relative-to-cwd` | `tests/unit/test_retention_dependency_units.py::test_d1_a_separate_git_dir_and_a_relative_gitfile` | KILLED | 13:47:00.807 | 19.96 / 20.72 / 22.46 |
| `git-storage-nearest-ignores-gitfiles` | `tests/unit/test_retention_dependency_units.py::test_d1_a_linked_checkout_outside_names_the_repository_it_lives_in` | KILLED | 13:47:04.688 | 19.96 / 20.72 / 22.46 |
| `record-raw-spelling` | `tests/unit/test_retention_dependency_units.py::test_record_spells_each_place_once` | KILLED | 13:47:08.957 | 19.24 / 20.56 / 22.39 |
| `fences-not-retention-only` | `tests/unit/test_retention_dependency_units.py::test_d5_fences_reads_what_retiring_reads_place_by_place` | KILLED | 13:47:16.597 | 20.79 / 20.85 / 22.48 |
| `record-no-output` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[output]` | KILLED | 13:47:43.412 | 23.44 / 21.47 / 22.65 |
| `record-no-git` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[linked]` | KILLED | 13:47:50.381 | 22.77 / 21.37 / 22.61 |
| `review-quoted-decode-removed` | `tests/fake/test_retention_dependency_review_r1.py::test_c8_4_git_c_quoted_alternate_keeps_the_tree_it_reads[absolute]` | KILLED | 13:49:21.102 | 25.93 / 22.27 / 22.80 |
| `review-export-row-removed` | `tests/fake/test_retention_dependency_review_r1.py::test_c8_3_pending_export_keeps_its_actual_path_beside_its_lease_identity[legacy-dotless-i]` | KILLED | 13:53:12.212 | 17.85 / 21.70 / 22.58 |
| `review-alternates-cap-restored` | `tests/fake/test_retention_dependency_review_r1.py::test_c8_4_large_valid_alternates_file_keeps_the_tree_git_reads` | KILLED | 13:53:21.398 | 17.21 / 21.43 / 22.47 |
| `review-job-directory-pin-removed` | `tests/unit/test_retention_job_dependencies.py::test_c8_4_job_directory_dependencies_are_rechecked_atomically[census-out_path]` | KILLED | 13:53:28.033 | 16.79 / 21.28 / 22.41 |
| `r2-post-read-identity-check-dropped` | `tests/fake/test_retention_dependency_review_r2.py::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace]` | KILLED | 13:53:31.450 | 16.49 / 21.14 / 22.36 |
| `r2-terminal-legacy-incomplete-dropped` | `tests/fake/test_retention_dependency_review_r2.py::test_c8_4_legacy_incomplete_census_survives_terminal_export[database]` | KILLED | 13:53:37.456 | 16.61 / 21.09 / 22.33 |
| `r3-write-metadata-check-dropped` | `probes.py::test_changes[same-size-running]` | KILLED | 13:49:43.907 | 31.14 / 23.73 / 23.31 |
| `r3-final-path-check-dropped` | `probes.py::test_changes[symlink-info-running]` | KILLED | 13:49:59.767 | 26.28 / 23.03 / 23.06 |
| `r3-terminal-census-live-only` | `tests/fake/test_retention_dependency_review_r2.py::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting]` | KILLED | 13:50:19.369 | 26.46 / 23.18 / 23.11 |

### Complete hold ledger

P/F/S/X counts are completed call outcomes, with expected failures separated from skips. Collection has no calls. Initial scratch oracles and development probes are retained, without counting their failures as ordinary head failures.

| Hold | Start | Load (1/5/15) | P/F/S/X | Seconds |
|---|---|---|---|---:|
| `measurement` | 13:37:02.161 | 24.33 / 22.67 / 24.41 | 0/1/0/0 | 14.05 |
| `own-probes` | 13:37:20.397 | 28.41 / 23.65 / 24.73 | 6/15/0/0 | 135.02 |
| `collect` | 13:37:34.048 | 29.08 / 24.05 / 24.85 | 0/0/0/0 | 8.86 |
| `test_retention_dependency_review_r2-1` | 13:39:44.410 | 18.85 / 22.27 / 24.04 | 11/0/0/0 | 26.10 |
| `test_retention_dependency_review_r2-2` | 13:40:13.717 | 19.20 / 21.91 / 23.84 | 10/0/0/0 | 16.90 |
| `test_retention_alternates_review_r2-1` | 13:40:33.312 | 23.69 / 22.73 / 24.09 | 15/0/0/0 | 16.65 |
| `test_retention_dependency_review_r1-1` | 13:40:52.027 | 23.29 / 22.72 / 24.05 | 18/0/0/0 | 21.05 |
| `test_retention_dependency_incomplete-1` | 13:41:14.214 | 20.65 / 22.16 / 23.82 | 19/0/0/0 | 53.53 |
| `test_retention_alternates-1` | 13:42:13.533 | 17.42 / 21.10 / 23.32 | 16/0/0/0 | 8.00 |
| `own-controls` | 13:43:08.657 | 13.58 / 19.50 / 22.59 | 20/0/0/0 | 120.50 |
| `r3-red` | 13:45:15.230 | 21.79 / 21.22 / 22.87 | 0/2/0/0 | 13.23 |
| `r3-marked` | 13:45:29.622 | 20.69 / 21.01 / 22.76 | 0/0/0/2 | 13.12 |
| `mutant-trial-r3-alias-revalidation` | 13:46:21.050 | 21.25 / 20.90 / 22.60 | 2/0/0/0 | 9.07 |
| `mutant-git-storage-no-commondir` | 13:46:41.565 | 22.33 / 21.20 / 22.67 | 0/1/0/0 | 12.17 |
| `test_retention_alternates-2` | 13:46:51.354 | 20.90 / 20.93 / 22.56 | 1/0/0/0 | 47.35 |
| `mutant-git-storage-no-worktrees-fallback` | 13:46:54.117 | 20.90 / 20.93 / 22.56 | 0/1/0/0 | 1.80 |
| `mutant-git-storage-no-alternates` | 13:46:56.296 | 19.87 / 20.72 / 22.47 | 0/1/0/0 | 1.56 |
| `mutant-git-storage-alternates-one-level` | 13:46:58.212 | 19.87 / 20.72 / 22.47 | 0/1/0/0 | 2.14 |
| `mutant-git-storage-gitfile-relative-to-cwd` | 13:47:00.807 | 19.96 / 20.72 / 22.46 | 0/1/0/0 | 2.77 |
| `mutant-git-storage-nearest-ignores-gitfiles` | 13:47:04.688 | 19.96 / 20.72 / 22.46 | 0/1/0/0 | 3.15 |
| `mutant-record-raw-spelling` | 13:47:08.957 | 19.24 / 20.56 / 22.39 | 0/1/0/0 | 6.41 |
| `mutant-fences-not-retention-only` | 13:47:16.597 | 20.79 / 20.85 / 22.48 | 0/1/0/0 | 9.29 |
| `test_retention_alternates-3` | 13:47:39.111 | 23.74 / 21.50 / 22.67 | 6/0/0/0 | 3.61 |
| `mutant-record-no-output` | 13:47:43.412 | 23.44 / 21.47 / 22.65 | 0/1/0/0 | 6.12 |
| `mutant-record-no-git` | 13:47:50.381 | 22.77 / 21.37 / 22.61 | 0/1/0/0 | 8.23 |
| `test_retention_job_dependencies-1` | 13:47:59.256 | 21.27 / 21.09 / 22.50 | 12/0/0/0 | 16.93 |
| `test_retention_dependencies-1` | 13:48:18.763 | 22.89 / 21.42 / 22.58 | 20/0/0/0 | 56.36 |
| `mutant-review-quoted-decode-removed` | 13:49:21.102 | 25.93 / 22.27 / 22.80 | 0/1/0/0 | 21.27 |
| `mutant-r3-write-metadata-check-dropped` | 13:49:43.907 | 31.14 / 23.73 / 23.31 | 0/1/0/0 | 14.52 |
| `mutant-r3-final-path-check-dropped` | 13:49:59.767 | 26.28 / 23.03 / 23.06 | 0/1/0/0 | 18.77 |
| `mutant-r3-terminal-census-live-only` | 13:50:19.369 | 26.46 / 23.18 / 23.11 | 0/1/0/0 | 11.22 |
| `test_retention_dependencies-2` | 13:50:31.798 | 25.44 / 23.09 / 23.08 | 10/0/0/0 | 22.82 |
| `recovery-probe` | 13:50:57.878 | 23.80 / 22.92 / 23.02 | 2/2/0/0 | 25.11 |
| `test_retention_dependency_spelling-1` | 13:51:25.226 | 25.94 / 23.49 / 23.22 | 16/0/0/0 | 24.05 |
| `test_retention_dependency_units-1` | 13:51:58.586 | 24.92 / 23.58 / 23.27 | 6/0/0/0 | 16.53 |
| `test_retention_dependency_units-2` | 13:52:16.178 | 21.49 / 22.90 / 23.03 | 1/0/0/0 | 8.71 |
| `test_retention_dependency_units-3` | 13:52:25.084 | 21.93 / 22.96 / 23.05 | 1/0/0/0 | 18.69 |
| `test_retention_dependency_units-4` | 13:52:45.212 | 21.41 / 22.72 / 22.96 | 1/0/0/0 | 19.77 |
| `recovery-existing-legacy` | 13:52:52.239 | 19.74 / 22.32 / 22.81 | 2/2/0/0 | 16.52 |
| `test_retention_dependency_units-5` | 13:53:06.292 | 17.58 / 21.71 / 22.59 | 4/0/0/0 | 9.00 |
| `mutant-review-export-row-removed` | 13:53:12.212 | 17.85 / 21.70 / 22.58 | 2/1/0/0 | 8.71 |
| `test_retention_dependency_units-6` | 13:53:15.647 | 17.85 / 21.70 / 22.58 | 1/0/0/0 | 10.30 |
| `mutant-review-alternates-cap-restored` | 13:53:21.398 | 17.21 / 21.43 / 22.47 | 0/1/0/0 | 5.68 |
| `test_retention_dependency_units-7` | 13:53:26.177 | 16.79 / 21.28 / 22.41 | 5/0/0/0 | 5.62 |
| `mutant-review-job-directory-pin-removed` | 13:53:28.033 | 16.79 / 21.28 / 22.41 | 0/1/0/0 | 2.95 |
| `mutant-r2-post-read-identity-check-dropped` | 13:53:31.450 | 16.49 / 21.14 / 22.36 | 0/1/0/0 | 5.67 |
| `test_retention_folded_pin-1` | 13:53:32.487 | 16.49 / 21.14 / 22.36 | 30/0/0/0 | 3.00 |
| `test_retention_folded_pin-2` | 13:53:35.840 | 16.61 / 21.09 / 22.33 | 1/0/0/0 | 37.76 |
| `mutant-r2-terminal-legacy-incomplete-dropped` | 13:53:37.456 | 16.61 / 21.09 / 22.33 | 0/1/0/0 | 3.43 |
| `r3-recovery-red` | 13:54:06.196 | 15.19 / 20.32 / 22.01 | 2/2/0/0 | 38.00 |
| `test_retention_folded_pin-3` | 13:54:15.236 | 15.25 / 20.25 / 21.98 | 1/0/0/0 | 136.41 |
| `r3-all-marked` | 13:54:50.425 | 16.47 / 20.06 / 21.84 | 2/0/0/4 | 70.28 |
| `mutant-trial-r3-census-before-recovery` | 13:56:08.567 | 20.33 / 20.29 / 21.75 | 4/0/0/0 | 23.51 |
| `test_retention_folded_pin-4` | 13:56:33.780 | 23.06 / 20.83 / 21.90 | 5/0/0/0 | 30.75 |
| `test_retention_folded_pin-5` | 13:57:06.303 | 21.22 / 20.75 / 21.83 | 1/0/0/0 | 17.81 |
| `test_retention_dependency_properties-1` | 13:57:26.380 | 17.42 / 19.90 / 21.50 | 1/0/0/0 | 159.61 |
| `test_retention_dependency_properties-2` | 14:00:07.045 | 24.32 / 22.80 / 22.46 | 1/0/0/0 | 92.89 |
| `test_retention_dependency_properties-3` | 14:01:40.368 | 15.02 / 20.33 / 21.56 | 1/0/0/0 | 289.10 |
| `test_retention_dependency_properties-4` | 14:06:31.154 | 25.63 / 22.34 / 21.82 | 1/0/0/0 | 89.97 |
| `test_retention_shared_folders-1` | 14:08:03.170 | 25.07 / 23.41 / 22.30 | 16/0/0/0 | 55.91 |
| `test_retention_shared_folders-2` | 14:09:08.854 | 28.90 / 25.35 / 23.14 | 15/0/0/0 | 100.12 |
| `test_retention_shared_folders-3` | 14:11:07.157 | 34.27 / 29.66 / 25.22 | 1/0/0/0 | 18.85 |
| `test_retention_shared_folders-4` | 14:11:28.234 | 31.81 / 29.41 / 25.24 | 1/0/0/0 | 309.91 |
| `test_retention_shared_folders-5` | 14:16:39.450 | 39.78 / 36.09 / 29.91 | 3/0/0/0 | 16.94 |
| `test_retention_shared_folders-6` | 14:16:57.343 | 35.98 / 35.50 / 29.84 | 1/0/0/0 | 215.19 |
| `test_retention_shared_folders-7` | 14:20:35.592 | 31.48 / 28.28 / 27.65 | 12/0/0/0 | 151.74 |
| `test_retention_shared_folders-8` | 14:23:27.384 | 75.58 / 48.90 / 36.32 | 12/0/0/0 | 67.88 |
| `test_retention_shared_folders-9` | 14:24:39.156 | 67.07 / 52.77 / 38.83 | 1/0/0/0 | 23.17 |
| `test_retention_archive-1` | 14:25:04.225 | 60.09 / 52.27 / 39.06 | 4/2/0/0 | 50.82 |
| `test_retention_archive-2` | 14:25:58.952 | 64.70 / 55.36 / 41.06 | 5/3/0/0 | 26.82 |
| `test_retention_archive-3` | 14:26:26.673 | 51.79 / 53.07 / 40.72 | 6/0/0/0 | 35.01 |
| `test_retention_archive-4` | 14:27:07.263 | 42.17 / 50.85 / 40.50 | 6/0/0/0 | 63.44 |
| `test_retention_archive-retry-1` | 14:28:03.617 | 32.30 / 47.03 / 39.75 | 2/0/0/0 | 23.76 |
| `test_retention_archive-5` | 14:28:14.333 | 28.86 / 45.81 / 39.40 | 12/0/0/0 | 73.34 |
| `test_retention_archive-retry-2` | 14:28:28.528 | 36.48 / 46.73 / 39.85 | 3/0/0/0 | 12.51 |
| `test_retention_archive-6` | 14:29:30.458 | 37.30 / 44.68 / 39.52 | 23/0/0/0 | 45.52 |
| `test_retention_archive-7` | 14:30:20.463 | 28.93 / 41.29 / 38.57 | 7/0/0/0 | 37.13 |
| `test_retention_archive-8` | 14:31:02.240 | 32.38 / 40.62 / 38.47 | 6/0/0/0 | 49.75 |
| `test_retention_archive-9` | 14:31:56.084 | 36.75 / 40.88 / 38.70 | 6/0/0/0 | 37.41 |
| `test_retention_archive-10` | 14:32:35.852 | 37.07 / 40.04 / 38.47 | 6/0/0/0 | 26.99 |
| `test_retention_archive-11` | 14:33:07.295 | 33.20 / 38.97 / 38.16 | 6/0/0/0 | 36.23 |
| `test_retention_archive-12` | 14:33:45.802 | 38.33 / 39.64 / 38.43 | 6/0/0/0 | 24.56 |
| `test_retention_archive-13` | 14:34:14.316 | 27.92 / 36.99 / 37.52 | 6/0/0/0 | 41.05 |
| `test_retention_archive-14` | 14:35:01.886 | 26.28 / 35.29 / 36.86 | 4/0/0/0 | 28.64 |
| `test_retention_turns-1` | 14:35:32.550 | 32.49 / 35.90 / 37.02 | 7/0/0/0 | 11.43 |
| `test_detached_retention_fence-1` | 14:35:45.074 | 33.01 / 35.93 / 37.02 | 14/0/0/0 | 28.65 |
| `test_detached_retention_fence-2` | 14:36:16.132 | 25.18 / 33.78 / 36.20 | 16/0/0/0 | 37.46 |
| `test_detached_retention_fence-3` | 14:36:58.494 | 37.83 / 35.64 / 36.75 | 2/0/0/0 | 15.59 |
| `test_detached_retention_fence-4` | 14:37:15.008 | 37.57 / 35.62 / 36.71 | 1/0/0/0 | 5.50 |
| `test_detached_retention_fence-5` | 14:37:20.896 | 35.60 / 35.24 / 36.57 | 26/0/0/0 | 27.82 |
| `test_detached_retention_fence-6` | 14:38:03.419 | 34.47 / 35.23 / 36.51 | 1/0/0/0 | 34.53 |
| `test_detached_retention_fence-7` | 14:38:39.091 | 32.33 / 34.55 / 36.21 | 1/0/0/0 | 77.71 |
| `test_detached_retention_fence-8` | 14:40:07.996 | 25.31 / 32.02 / 35.09 | 10/0/0/0 | 9.89 |
| `test_detached_retention_fence-9` | 14:40:18.697 | 24.16 / 31.56 / 34.89 | 18/0/0/0 | 22.17 |
| `test_writer_retention_fence-1` | 14:40:48.791 | 32.06 / 32.84 / 35.24 | 14/0/0/0 | 28.33 |
| `test_writer_retention_fence-2` | 14:41:18.149 | 26.35 / 31.44 / 34.65 | 1/0/0/0 | 11.80 |
| `test_writer_retention_fence-3` | 14:41:30.695 | 25.25 / 31.03 / 34.47 | 8/0/0/0 | 22.25 |
| `test_state_contract-1` | 14:41:56.138 | 23.72 / 30.26 / 34.09 | 6/0/0/0 | 15.98 |
| `test_state_contract-2` | 14:42:12.921 | 20.42 / 29.09 / 33.59 | 10/0/0/0 | 5.65 |
| `test_state_contract-3` | 14:42:19.078 | 20.55 / 28.97 / 33.52 | 8/0/0/0 | 24.20 |
| `r3-final-red` | 14:42:29.468 | 19.15 / 28.40 / 33.26 | 2/4/0/0 | 16.92 |
| `test_state_contract-4` | 14:42:44.534 | 17.18 / 27.51 / 32.86 | 9/0/0/0 | 5.80 |
| `r3-final-marked` | 14:42:47.384 | 16.45 / 27.18 / 32.71 | 2/0/0/4 | 19.89 |
| `test_state_contract-5` | 14:42:50.746 | 16.45 / 27.18 / 32.71 | 4/0/0/0 | 6.30 |
| `test_test_module_names-1` | 14:42:57.795 | 15.41 / 26.60 / 32.44 | 1/0/0/0 | 5.16 |
| `test_invariants_index-1` | 14:43:03.917 | 15.45 / 26.43 / 32.34 | 6/0/0/0 | 4.30 |
| `test_invariants_index-2` | 14:43:08.639 | 15.26 / 26.20 / 32.23 | 6/0/0/0 | 5.17 |
| `mutant-trial-r3-alias-revalidation-final` | 14:43:10.143 | 15.26 / 26.20 / 32.23 | 2/0/0/0 | 7.77 |
| `test_invariants_index-3` | 14:43:14.772 | 15.48 / 26.07 / 32.15 | 6/0/0/0 | 3.78 |
| `test_invariants_index-4` | 14:43:19.265 | 15.92 / 25.98 / 32.08 | 6/0/0/0 | 5.09 |
| `test_invariants_index-5` | 14:43:25.585 | 16.40 / 25.92 / 32.02 | 1/0/0/0 | 4.62 |

Longest admitted pytest: **309.91 seconds**. Highest observed pytest RSS: **128.3 MiB**; fixture child RSS is not separately sampled.

The initial `own-probes` run has 14 erroneous post-release exact-set assertions (the finished borrower may retire too), one real R3-F1 failure and six passing SQL controls. Correcting the set assertion produces 20 passing controls. The initial recovery probe inserted a legacy-shaped job between passes; `recovery-existing-legacy` supersedes it with a legacy job predating both passes. Both show the same two legacy failures and two recorded-evidence passes. The final regression and trial use the latter timeline. The scale run retains its separate reason-precedence oracle error as explained above.

Initial archive holds under `/private/tmp` trigger the existing lexical `scratch path` check even though the fixture disables temporary-root classification. This makes expected blob omission and scratch-reason assertions fail. Archive fixture trees were moved to the allowed Darwin task scratch at `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261029-084118-pr272-review-r3-a1-04vmk40r/sf-r272r3`; no source, fixture, bound or assertion was patched. Only failed nodes were retried there; later archive holds use that directory from the start. The ledger retains initial environment failures and successful retries separately. TMPDIR and homes remain inside the two task-owned scratch roots, whose combined disk use stays below 1.5 GB.

Not run: the real-lsof archive node, the full suite, Python 3.12, CI execution and the person-only live frontend. The live frontend cannot run on a lane. No remaining requested local node is unrun beyond the explicitly excluded host process scan.

The regression, report and evidence are left uncommitted under the explicit review-only/no-commit instruction. No staging, Git metadata write, push, publication or PR comment was attempted. All owned run sessions completed. Guarded cleanup removed both task-owned scratch roots and verified their absence at 2026-10-29T14:46:04.367414+00:00. The archive interruption fixture left a 0555 directory; restoring owner write permission on task-owned directories allowed the guarded cleanup to finish. No bytecode, Hypothesis state, build directory or kept failure root from this review remains outside the preserved evidence. Combined scratch was observed at approximately 33 MB at its largest sampled point, below 1.5 GB. Final HEAD remains `e4b20c9e54a8`.


DONE
