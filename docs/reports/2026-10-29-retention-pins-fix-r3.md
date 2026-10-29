# Retention dependency pins: fix round r3, 2026-10-29

Base: `783123230e1e246aacbc61c9e5ff925357d9784b`, the hub's r3 evidence plus release/217 at `d827f5c99`. First recorded clock: **20:05:24 UTC**. First correctness hold: **20:06:43 UTC**. The four-hour new-hold cutoff is October 30 at 00:05 UTC; the six-hour wall is 02:05 UTC. Only the assigned worktree is edited. Shared Git metadata refused a branch write, so ordered commits use private metadata and delivery is a verified bundle based on `783123230`. No reviewed-repository fetch, merge, push or external message is performed; fixture Git transports use only local repositories and bundles.

## Decisions built

1. **R3-F1:** retain each original checkout and `.git` spelling beside its resolved target and device/inode, whether `.git` is a directory, symlink or `gitdir:` file. Retain the pointer text and pathname of `gitdir:`/`commondir`, their target paths, and every alternates entry's own spelling. After traversal, resolve the original paths again and reread pointer text, outside any transaction. A changed/vanished alias records incomplete evidence and invokes the existing global allocated-worktree pin. Original aliases are stored once per distinct spelling, so duplicate alternates lines do not accumulate duplicate observations. Fix: `84220acb5`; additional pointer coverage and storage deduplication: `66f7b2dcd`; repeated unreadable spellings also keep one error observation (`0455f054f` and `957bba602`).
2. **R3-F2:** build the legacy census, including terminal exporters, before `_recover()` can quarantine anything. Assign that exact census object to the pass and archive context. If construction raises, the pass never reaches recovery; no recovery move is made with empty evidence. Recorded-incomplete controls keep passing. No exporter SQL or evidence-lifetime loop change was needed. Fix: `24ea056b9`.
3. **Tests:** remove only the strict expected-failure marks covering r3's four repaired cases; every other byte of that review file is preserved. Add ten pointer/alias cases (including a stable control), two census-order/failure controls, and the two requested mutants. Rerun review r3's three and fix r2's two named mutants, each only against its named killer. The first r2 mutant is repeated after the small alias-error deduplication follow-up so the final seven outcomes all hash the final product source. The unchanged turn property is the regression test for decision 5. Mutant evidence commit: `1c9b11535`.
4. **Text:** update C-8.3/C-8.4, the changelog, desktop incomplete-discovery/recovery/I16 descriptions, the main report's “Review r3 and fix round r3” section and the fix-r2 description. Fix r1's observation-bounds reference points to that corrected r2 discussion. Preserve the original review reports and historical r2 red/green/452-node ledger as dated evidence. Current text says what is checked at this head; the original review's “does not revalidate” and “can temporarily quarantine” describe its old head.
5. **CI turn-property regression:** commit **`6f54e9ee6f412ce98a4a0ffe42193a10ea865bf4`**, “keep the canonical job-directory fence spelling for commit”, introduced `self.save(job_folder=folder)` in `_fence_job_folder`. That guard assumed a retirement work directory already existed. The release test's deliberately small retirement calls the real guard before creating its fake quarantine directory, so the save fails opening `.journal.json.<pid>.tmp`; `_start` defers retirement and returns None even after the live turn ends. Product repair `d01d43e0e` captures `job_folder` in `begin`'s normal initial journal. Older journals acquire it in memory; quarantine's existing save persists it before any rename. The original test is unchanged and still asserts both live-cwd preservation and retirement after the turn ends. Decision 2 reaches this same guard through recovery's `quarantine`, but only changes the ordering of dependency discovery; it neither introduced nor repairs this separate premature journal write.

No hub product decision required a deviation. The operational adjustments below concern writable scratch, private Git metadata and fixture scheduling only.

## Observation bounds

The descriptor checks compare device/inode, size, nanosecond mtime and ctime before/after reading, then the final identity of the resolved alternates pathname opened. The new check separately revalidates original path aliases and pointer contents after traversal. An alias **swapped and swapped back within one traversal** can escape those comparisons. A write that preserves size, mtime and ctime within one timestamp tick and dependencies introduced after discovery remain additional bounds. These checks are not an atomic filesystem snapshot.

Discovery stays outside transactions. Selecting, job-directory fencing and commit transactions use database reads and string operations, carrying the previously built census. Incomplete discovery still admits jobs; while the affected job is live or exporting, the existing global pin retains every allocated worktree and names the job/file. The new alias map grows with distinct original spellings, while the alternates line buffer retains its 1 MiB plus overflow-byte bound.

## Red and green

At the original `783123230` source, the whole r3 file with `--runxfail` gives **2 passed / 4 failed**. Both alias cases retire the owner; both legacy recovery cases observe its transient absence. At the repaired source, the complete file gives **6 passed**, with only the authorized marks removed. The two recorded-evidence controls pass in both runs.

At the original source, the complete unchanged turn-property file gives **1 failed**, with Hypothesis reporting that all **five explicit examples** fail on the missing journal temp file. After the product repair it gives **1 passed**, executing its five explicit and 40 generated examples. The remaining turn-retention files are included in the ordinary ledger below.

## Execution conditions

The locked dev group was synced once, using the prescribed sync wrapper and Python 3.14.7. Runs use `uv run --no-sync`, unset `SUBFLEET_RETENTION_DORMANT` and `PYTHONPATH`, disable pytest's cache and bytecode writes, and place TMPDIR, Hypothesis state, safe homes and uv files in task-owned scratch. The requested generic Darwin scratch location is outside the sandbox's writable roots; the environment lives in `/private/tmp/sf-fix272r3`. Archive fixtures use `sf-fix272r3` under this attempt's explicitly writable Darwin directory so non-scratch fixtures do not accidentally contain a literal `tmp` component. Initial ordinary fixtures also use that directory; later non-archive holds use shorter neutral paths in `/private/tmp/sf-fix272r3` to reduce pathname traversal work. Both roots are counted together for disk accounting.

The existing safe-home/provider/keychain guards remain active. No live store, real provider or production CLI is used, and no live host process data is read. The live-lsof archive case is explicitly deselected: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`.

Initial probe controls and two r2 output-pin controls reach the test keychain guard when `taskpolicy` wraps fixture Git. The observer initially reused TMPDIR as pytest basetemp, which pytest clears; this removed the scratch home marker. The observer now gives pytest a separate child directory, verifies the marker at session finish, and reruns the guards and affected controls with that layout. HOME remained a scratch path throughout, and `_guard_active` continues to enforce the keychain prohibition through its independent in-process pytest rule even without the marker. Those environment failures occur after the relevant dependency assertions; they are retained separately and rerun with the supported `SUBFLEET_RETENTION_QOS=inherit` setting. Subsequent holds use that scheduling setting, without changing product code, fixtures or assertions. There is no weakening of the keychain guard.

Ordinary pytest runs use the shared suite wrapper, one pytest per hold. The second mutant waiter received no admission for 30 minutes (queued 20:17:44 UTC); it was stopped through its tool session, and the six remaining named mutants run directly under the lane rules’ explicit fallback. Their JSON records mark `locked: false`; the wait log is retained. At most three owned holds are running or queued, and the shared lock admits at most two. No xdist, detached driver, load generator or sub-agent is used. Tool-managed background sessions are polled to completion. Every pytest has a 570-second whole-run alarm. Expensive properties run separately, including the sweep property apart from its examples. The observer records starts after lock admission and `os.getloadavg()` because this sandbox denies the wrapper's sysctl probes. Logs, phase outcomes, source hashes for mutants and replay scripts are preserved beside this report.

## PR-body lines for the hub

- **Status:** Review r2's original six inputs remain fixed. Review r3's R3-F1 and R3-F2 are now fixed; approving review and completed CI remain merge gates. Retention remains dormant.
- **Changes:** Revalidate original repository aliases and pointer targets after storage traversal outside transactions; changed or vanished aliases record incomplete evidence. Build the live/exporting legacy census before recovery and share it with the pass/archive context; a failed census prevents recovery moves. Persist canonical job-folder spelling in the normal journal lifecycle, repairing the unchanged live-turn cwd property.
- **Invariants:** C-6.5, C-6.11, C-8.3, C-8.4 and I16; filesystem discovery outside transactions, database-only rechecks and global allocated-worktree pins for incomplete evidence. An alias swapped and swapped back within one traversal, metadata-preserving writes inside one timestamp tick and later dependencies remain observation bounds.
- **Next:** Obtain approval for the repaired r3 inputs and complete CI before merge or re-enablement. Earlier measurements and r2's 452-node result remain dated evidence; no new CI snapshot was read or PR body written in this round.

## Test accounting

**682 distinct ordinary nodes passed**, including 680 repository nodes and two named review-r3 probe controls. No final failures, skips or expected failures. The adjacent `tests/unit/test_retention_alternates_review_r2.py` supplied 15 additional prior-review controls. No requested local node is unrun except the explicitly deselected real-lsof case.

All starts below are UTC on 2026-10-29 after lock admission. Loads are the host 1/5/15-minute averages. Counts use each node's latest ordinary call outcome and require passing teardown; red runs, intermediate checks and mutants are excluded. Initial environment failures remain in the full ledger.

| File | Passed | First ordinary start | Load (1/5/15) |
|---|---:|---|---|
| `review r3 probes.py (two named controls)` | 2 | 20:15:14.720 | 33.94 / 22.49 / 16.82 |
| `tests/fake/test_retention_dependency_review_r2.py` | 21 | 20:15:37.872 | 30.66 / 22.44 / 16.93 |
| `tests/unit/test_retention_alternates_review_r2.py` | 15 | 20:16:51.029 | 29.46 / 23.48 / 17.75 |
| `tests/unit/test_retention_archive.py` | 108 | 20:17:46.848 | 33.09 / 25.46 / 18.84 |
| `tests/fake/test_retention_dependency_review_r1.py` | 18 | 20:19:47.577 | 24.23 / 24.79 / 19.46 |
| `tests/fake/test_retention_dependency_incomplete.py` | 19 | 20:20:03.392 | 22.75 / 24.42 / 19.41 |
| `tests/unit/test_retention_alternates.py` | 23 | 20:20:55.774 | 17.16 / 22.53 / 19.02 |
| `tests/unit/test_retention_archive_properties.py` | 2 | 20:27:20.592 | 53.04 / 35.98 / 25.92 |
| `tests/unit/test_retention_job_dependencies.py` | 12 | 20:28:31.530 | 64.44 / 42.02 / 28.94 |
| `tests/fake/test_retention_dependencies.py` | 30 | 20:29:10.600 | 41.55 / 38.85 / 28.37 |
| `tests/unit/test_retention_nested_leases.py` | 27 | 20:29:43.378 | 52.01 / 41.74 / 29.79 |
| `tests/fake/test_retention_dependency_spelling.py` | 16 | 20:30:22.961 | 46.32 / 41.18 / 30.11 |
| `tests/unit/test_retention_dependency_units.py` | 19 | 20:30:55.745 | 42.63 / 40.93 / 30.46 |
| `tests/unit/test_retention_salvage_properties.py` | 2 | 20:31:26.570 | 31.17 / 38.32 / 29.88 |
| `tests/unit/test_retention_folded_pin.py` | 38 | 20:31:32.653 | 29.23 / 37.80 / 29.75 |
| `tests/unit/test_retention_dependency_properties.py` | 4 | 20:34:34.981 | 27.49 / 34.33 / 29.93 |
| `tests/unit/test_retention_sweep_race.py` | 13 | 20:36:48.657 | 25.66 / 31.54 / 29.45 |
| `tests/unit/test_retention_shared_folders.py` | 62 | 20:43:27.362 | 49.33 / 32.16 / 28.93 |
| `tests/unit/test_retention_81_notes.py` | 38 | 20:44:22.292 | 70.42 / 40.12 / 32.05 |
| `tests/unit/test_retention_dependency_aliases.py` | 10 | 20:48:23.356 | 84.19 / 65.73 / 45.57 |
| `tests/fake/test_detached_retention_fence.py` | 89 | 20:49:11.118 | 71.90 / 65.30 / 46.56 |
| `tests/fake/test_writer_retention_fence.py` | 23 | 20:56:22.909 | 54.27 / 59.64 / 51.65 |
| `tests/fake/test_state_contract.py` | 37 | 20:57:18.645 | 54.99 / 58.59 / 51.74 |
| `tests/unit/test_test_module_names.py` | 1 | 20:57:59.070 | 54.95 / 57.99 / 51.82 |
| `tests/unit/test_invariants_index.py` | 25 | 20:58:08.254 | 53.87 / 57.65 / 51.77 |
| `tests/fake/test_turn_retention_cwd.py` | 6 | 20:58:18.631 | 53.48 / 57.44 / 51.77 |
| `tests/fake/test_turn_retention_quarantine.py` | 10 | 20:59:02.596 | 60.93 / 58.89 / 52.60 |
| `tests/fake/test_turn_retention_cwd_properties.py` | 1 | 20:59:58.751 | 78.12 / 63.57 / 54.70 |
| `tests/fake/test_turn_retention_job_directory.py` | 2 | 21:00:48.833 | 74.75 / 64.94 / 55.71 |
| `tests/fake/test_turn_retention_job_properties.py` | 1 | 21:01:09.394 | 62.74 / 62.83 / 55.17 |
| `tests/fake/test_retention_dependency_review_r3.py` | 6 | 21:01:56.815 | 47.70 / 58.86 / 54.15 |
| `tests/fake/test_retention_recovery_census.py` | 2 | 21:02:27.777 | 45.77 / 57.29 / 53.74 |

### Red/green timing

| Run | P/F | Start UTC | Load (1/5/15) |
|---|---:|---|---|
| red-r3 | 2/4 | 20:06:43.733 | 17.35 / 16.66 / 12.18 |
| green-r3 | 6/0 | 20:11:19.191 | 16.11 / 18.11 / 14.15 |
| red-turn | 0/1 | 20:07:07.000 | 16.60 / 16.58 / 12.28 |
| green-turn | 1/0 | 20:11:55.702 | 18.89 / 18.64 / 14.50 |

### Mutants

Every exact replacement matches once. Each mutant loads one changed module in memory, exits 1 with its named call assertion failing, and passes setup/teardown; production source is never modified. Definitions, original/mutated source hashes and logs are adjacent.

| Mutant | Named killing test | Result | Start UTC | Load (1/5/15) |
|---|---|---|---|---|
| `r2-post-read-identity-check-dropped` | `tests/fake/test_retention_dependency_review_r2.py::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace]` | KILLED | 20:55:53.287 | 62.93 / 61.58 / 52.02 |
| `r2-terminal-legacy-incomplete-dropped` | `tests/fake/test_retention_dependency_review_r2.py::test_c8_4_legacy_incomplete_census_survives_terminal_export[database]` | KILLED | 20:48:22.370 | 84.19 / 65.73 / 45.57 |
| `r3-post-traversal-alias-check-dropped` | `tests/fake/test_retention_dependency_review_r3.py::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live]` | KILLED | 20:48:35.712 | 81.64 / 66.11 / 46.06 |
| `r3-census-moved-after-recovery` | `tests/fake/test_retention_dependency_review_r3.py::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy]` | KILLED | 20:48:50.118 | 75.28 / 65.44 / 46.17 |
| `r3-write-metadata-check-dropped` | `review r3 probes.py::test_changes[same-size-running]` | KILLED | 20:49:09.015 | 71.46 / 65.10 / 46.38 |
| `r3-final-path-check-dropped` | `review r3 probes.py::test_changes[symlink-info-running]` | KILLED | 20:49:23.194 | 73.07 / 65.76 / 46.95 |
| `r3-terminal-census-live-only` | `tests/fake/test_retention_dependency_review_r2.py::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting]` | KILLED | 20:49:39.524 | 76.77 / 66.86 / 47.66 |

### Every hold

| Hold | Exit | Start UTC | Load (1/5/15) | Seconds |
|---|---:|---|---|---:|
| `red-r3` | 1 | 20:06:43.733 | 17.35 / 16.66 / 12.18 | 21.77 |
| `red-turn` | 1 | 20:07:07.000 | 16.60 / 16.58 / 12.28 | 4.95 |
| `f1-r3` | 0 | 20:08:46.805 | 22.56 / 17.80 / 13.17 | 27.56 |
| `green-r3` | 0 | 20:11:19.191 | 16.11 / 18.11 / 14.15 | 16.44 |
| `green-turn` | 0 | 20:11:55.702 | 18.89 / 18.64 / 14.50 | 41.69 |
| `alias-pointers` | 0 | 20:12:37.720 | 20.41 / 19.13 / 14.88 | 8.79 |
| `census-failure` | 0 | 20:12:48.974 | 18.42 / 18.75 / 14.79 | 6.74 |
| `collect` | 0 | 20:13:40.491 | 20.84 / 19.45 / 15.30 | 34.82 |
| `probes-controls` | 1 | 20:15:14.720 | 33.94 / 22.49 / 16.82 | 17.93 |
| `retention_dependency_review_r2` | 1 | 20:15:37.872 | 30.66 / 22.44 / 16.93 | 65.74 |
| `retention_alternates_review_r2` | 0 | 20:16:51.029 | 29.46 / 23.48 / 17.75 | 27.85 |
| `mutant-r2-post-read-identity-check-dropped` | 1 | 20:17:24.533 | 35.87 / 25.35 / 18.64 | 19.88 |
| `retention_archive-1` | 0 | 20:17:46.848 | 33.09 / 25.46 / 18.84 | 49.24 |
| `retention_archive-2` | 0 | 20:18:40.651 | 28.67 / 25.63 / 19.32 | 23.41 |
| `retention_archive-3` | 0 | 20:19:06.380 | 23.98 / 24.75 / 19.18 | 46.49 |
| `retention_dependency_review_r1` | 0 | 20:19:47.577 | 24.23 / 24.79 / 19.46 | 14.12 |
| `retention_archive-4` | 0 | 20:19:54.317 | 22.20 / 24.34 / 19.36 | 34.27 |
| `retention_dependency_incomplete` | 0 | 20:20:03.392 | 22.75 / 24.42 / 19.41 | 48.79 |
| `retention_archive-5` | 0 | 20:20:31.147 | 19.12 / 23.36 / 19.20 | 38.77 |
| `retention_alternates` | 0 | 20:20:55.774 | 17.16 / 22.53 / 19.02 | 63.99 |
| `retention_archive-6` | 0 | 20:21:11.031 | 18.51 / 22.58 / 19.10 | 9.69 |
| `retention_archive-7` | 0 | 20:21:22.875 | 18.16 / 22.38 / 19.07 | 19.46 |
| `retention_archive-8` | 0 | 20:21:43.948 | 21.16 / 22.88 / 19.33 | 22.24 |
| `retention_archive-9` | 0 | 20:22:09.166 | 18.89 / 22.20 / 19.19 | 33.79 |
| `retention_archive-10` | 0 | 20:22:46.593 | 15.02 / 20.78 / 18.81 | 41.46 |
| `retention_archive-11` | 0 | 20:23:36.031 | 26.17 / 23.07 / 19.77 | 62.88 |
| `retention_archive-12` | 0 | 20:24:44.241 | 31.86 / 25.10 / 20.76 | 56.83 |
| `retention_archive-13` | 0 | 20:25:46.488 | 43.98 / 29.96 / 22.89 | 58.25 |
| `retention_archive-14` | 0 | 20:26:49.331 | 45.25 / 33.19 / 24.63 | 25.26 |
| `retention_archive_properties-1` | 0 | 20:27:20.592 | 53.04 / 35.98 / 25.92 | 77.63 |
| `retention_job_dependencies` | 0 | 20:28:31.530 | 64.44 / 42.02 / 28.94 | 33.00 |
| `retention_archive_properties-2` | 0 | 20:28:41.012 | 57.09 / 41.18 / 28.79 | 56.86 |
| `retention_dependencies` | 0 | 20:29:10.600 | 41.55 / 38.85 / 28.37 | 66.42 |
| `retention_nested_leases` | 0 | 20:29:43.378 | 52.01 / 41.74 / 29.79 | 97.08 |
| `retention_dependency_spelling` | 0 | 20:30:22.961 | 46.32 / 41.18 / 30.11 | 28.18 |
| `retention_dependency_units` | 0 | 20:30:55.745 | 42.63 / 40.93 / 30.46 | 35.71 |
| `retention_salvage_properties-1` | 0 | 20:31:26.570 | 31.17 / 38.32 / 29.88 | 137.28 |
| `retention_folded_pin` | 0 | 20:31:32.653 | 29.23 / 37.80 / 29.75 | 87.89 |
| `retention_salvage_properties-2` | 0 | 20:33:48.751 | 31.88 / 36.36 / 30.34 | 177.94 |
| `retention_dependency_properties-1` | 0 | 20:34:34.981 | 27.49 / 34.33 / 29.93 | 110.71 |
| `retention_dependency_properties-2` | 0 | 20:36:31.873 | 28.94 / 32.43 / 29.72 | 83.13 |
| `retention_sweep_race-1` | 0 | 20:36:48.657 | 25.66 / 31.54 / 29.45 | 17.82 |
| `retention_sweep_race-2` | 0 | 20:37:09.989 | 19.46 / 29.63 / 28.82 | 13.30 |
| `retention_sweep_race-3` | 0 | 20:37:24.078 | 17.78 / 28.93 / 28.58 | 20.49 |
| `retention_sweep_race-4` | 0 | 20:37:48.249 | 15.52 / 27.51 / 28.08 | 391.85 |
| `retention_dependency_properties-3` | 0 | 20:37:57.162 | 13.91 / 26.77 / 27.81 | 232.79 |
| `retention_dependency_properties-4` | 0 | 20:41:56.152 | 28.42 / 24.12 / 26.04 | 87.45 |
| `retention_shared_folders-1` | 0 | 20:43:27.362 | 49.33 / 32.16 / 28.93 | 48.52 |
| `retention_81_notes` | 0 | 20:44:22.292 | 70.42 / 40.12 / 32.05 | 177.13 |
| `retention_shared_folders-2` | 0 | 20:44:22.309 | 70.42 / 40.12 / 32.05 | 47.22 |
| `retention_shared_folders-3` | 0 | 20:45:13.017 | 86.62 / 48.14 / 35.42 | 31.89 |
| `retention_shared_folders-4` | 0 | 20:47:26.932 | 85.97 / 62.62 / 43.22 | 50.14 |
| `retention_shared_folders-5` | 0 | 20:48:19.788 | 84.19 / 65.73 / 45.57 | 32.48 |
| `mutant-r2-terminal-legacy-incomplete-dropped` | 1 | 20:48:22.370 | 84.19 / 65.73 / 45.57 | 10.43 |
| `alias-errors` | 0 | 20:48:23.356 | 84.19 / 65.73 / 45.57 | 10.02 |
| `mutant-r3-post-traversal-alias-check-dropped` | 1 | 20:48:35.712 | 81.64 / 66.11 / 46.06 | 11.00 |
| `shared-property-1` | 0 | 20:48:36.095 | 81.64 / 66.11 / 46.06 | 15.21 |
| `mutant-r3-census-moved-after-recovery` | 1 | 20:48:50.118 | 75.28 / 65.44 / 46.17 | 14.99 |
| `shared-property-2` | 0 | 20:48:54.403 | 75.28 / 65.44 / 46.17 | 261.30 |
| `retention_shared_folders-6` | 0 | 20:48:54.885 | 75.18 / 65.58 / 46.33 | 12.70 |
| `mutant-r3-write-metadata-check-dropped` | 1 | 20:49:09.015 | 71.46 / 65.10 / 46.38 | 11.57 |
| `detached_retention_fence-1` | 0 | 20:49:11.118 | 71.90 / 65.30 / 46.56 | 32.12 |
| `mutant-r3-final-path-check-dropped` | 1 | 20:49:23.194 | 73.07 / 65.76 / 46.95 | 12.65 |
| `mutant-r3-terminal-census-live-only` | 1 | 20:49:39.524 | 76.77 / 66.86 / 47.66 | 14.79 |
| `detached_retention_fence-2` | 0 | 20:53:15.305 | 59.96 / 65.51 / 51.57 | 18.07 |
| `shared-property-3` | 0 | 20:53:16.995 | 59.96 / 65.51 / 51.57 | 97.74 |
| `detached_retention_fence-3` | 0 | 20:53:38.870 | 55.95 / 64.19 / 51.42 | 22.98 |
| `detached_retention_fence-4` | 0 | 20:54:03.126 | 45.48 / 61.03 / 50.65 | 12.95 |
| `detached_retention_fence-5` | 0 | 20:54:17.090 | 43.37 / 59.79 / 50.39 | 22.41 |
| `detached_retention_fence-6` | 0 | 20:54:43.137 | 44.57 / 58.71 / 50.27 | 59.01 |
| `shared-property-4` | 0 | 20:54:57.022 | 45.87 / 58.30 / 50.27 | 16.16 |
| `retry-r2-output` | 0 | 20:55:14.828 | 61.22 / 60.84 / 51.36 | 9.50 |
| `retry-probes-controls` | 0 | 20:55:25.957 | 64.36 / 61.55 / 51.72 | 25.57 |
| `detached_retention_fence-7` | 0 | 20:55:49.081 | 64.32 / 61.83 / 52.05 | 19.63 |
| `mutant-r2-post-read-identity-check-dropped-final` | 1 | 20:55:53.287 | 62.93 / 61.58 / 52.02 | 8.62 |
| `detached_retention_fence-8` | 0 | 20:56:12.341 | 55.97 / 60.14 / 51.73 | 9.67 |
| `writer_retention_fence` | 0 | 20:56:22.909 | 54.27 / 59.64 / 51.65 | 53.09 |
| `state_contract` | 0 | 20:57:18.645 | 54.99 / 58.59 / 51.74 | 38.15 |
| `test_module_names` | 0 | 20:57:59.070 | 54.95 / 57.99 / 51.82 | 6.75 |
| `invariants_index` | 0 | 20:58:08.254 | 53.87 / 57.65 / 51.77 | 8.20 |
| `turn_retention_cwd` | 0 | 20:58:18.631 | 53.48 / 57.44 / 51.77 | 39.48 |
| `turn_retention_quarantine` | 0 | 20:59:02.596 | 60.93 / 58.89 / 52.60 | 51.53 |
| `turn_retention_cwd_properties` | 0 | 20:59:58.751 | 78.12 / 63.57 / 54.70 | 44.87 |
| `turn_retention_job_directory` | 0 | 21:00:48.833 | 74.75 / 64.94 / 55.71 | 17.18 |
| `turn_retention_job_properties` | 0 | 21:01:09.394 | 62.74 / 62.83 / 55.17 | 43.06 |
| `retention_dependency_review_r3` | 0 | 21:01:56.815 | 47.70 / 58.86 / 54.15 | 17.99 |
| `retention_dependency_aliases` | 0 | 21:02:17.876 | 46.68 / 57.85 / 53.89 | 7.51 |
| `retention_recovery_census` | 0 | 21:02:27.777 | 45.77 / 57.29 / 53.74 | 5.73 |
| `final-environment-guards` | 0 | 21:03:26.328 | 37.68 / 52.86 / 52.32 | 43.19 |
| `final-environment-probes` | 0 | 21:04:14.257 | 34.46 / 49.96 / 51.29 | 20.41 |

Normal property settings and all phases are retained, with seed 272 for the ordinary plans. No example count or assertion is reduced. The turn regression uses 40 generated plus five explicit examples. The two archive properties each retain their default 10 generated examples; the sweep property retains 40 generated examples plus its explicit schedules. The four shared-folder properties run in separate holds at their normal 150/400/40/150 counts.

Longest observed pytest: **391.85 seconds**. Peak observed pytest RSS: **128.6 MiB**. Six named mutants used the permitted outside-lock fallback after a 30-minute wait; ordinary runs and the final repeated r2 mutant used the shared lock.

Not run: the named real-lsof archive node, full suite, Python 3.12, CI execution and the person-only live frontend. The live frontend cannot run on a lane. No additional scale measurement or external CI/PR read was performed.

PR-body evidence line: Review r3 red **2 passed / 4 failed** → green **6 passed**; unchanged turn-property red **1 failed (all five explicit inputs)** → green **1 passed (40 generated plus five explicit)**; **682 distinct ordinary passes** and **seven killed named mutants**. Link this report for per-file starts/loads, operational adjustments and observation bounds.

## Cleanup and delivery

All test and driver sessions completed before cleanup. The final combined scratch sample was **484,676 KiB (473.3 MiB)**, below 1.5 GB. Both task-owned scratch roots were removed with the prescribed guarded cleanup and their absence verified at **2026-10-29T21:13:10.553977+00:00**. Directory write permissions were restored only inside task-owned roots before removal, without following symlinks. No owned bytecode, Hypothesis state, build output or kept failure root remains outside the preserved evidence.

The assigned branch uses private Git metadata because the shared ref write was denied. The ordered commits are delivered in `retention-fix272r3.bundle`, requiring base `783123230e1e246aacbc61c9e5ff925357d9784b`; bundle creation and verification follow this text commit. No reviewed-repository fetch, merge or push was performed.
