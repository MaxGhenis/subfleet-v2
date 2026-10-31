# Retention dependency pins: fix round r6, 2026-10-30

Base: `b2a357b6c66559db3feb354bb6bb0ccf16f3c031`, including review r6's unchanged tests/evidence and the release merge at `cd5392469`. Started **22:09:07 UTC October 30**; first hold admitted **22:10:09 UTC**, with pytest starting **22:10:14 UTC**, within the first hour. New-hold cutoff: **02:09 UTC October 31**; six-hour wall: **04:09 UTC**. Only the assigned workspace is edited. No shared-repository fetch, merge or push, real provider, live process/store access or subagent is used; Git fixture transfers stay local.

## Decisions built

1. **R6-F1:** a named admin under `worktrees/<id>` with absent or CR/LF-blank `commondir` keeps both its own objects/alternates and everything the inferred parent supplies. Each store goes through `aliases.resolve` and `_alternates`, retaining original spellings for post-traversal revalidation. Missing-registration protection and the five valid-parent controls remain. The trial's single long return becomes readable `own_objects`, `own`, `parent`, and `alternates` steps; the discovery docstring now describes both stores.
2. **R6-F2:** keep the **64 KiB** pointer read bound. `_Aliases.text` records `TooLarge` and all other nonmissing `OSError`/`ValueError` failures in `self.errors`. Missing means `FileNotFoundError` where `lexists` is false, or `NotADirectoryError`. A dangling link exists and remains incomplete evidence. Unlike the trial's broad `FileNotFoundError` catch, this follows the exact hub rule. Alias identity observations also treat `NotADirectoryError` as missing so `resolve` does not turn that explicitly routine case into an error before `text` reads it. A stale gitfile with a vanished target demonstrably still retires. No hub decision required a departure.
3. **Git property:** `tests/unit/test_retention_pointer_inputs.py` adds a fixed **100 generated plus 15 explicit examples**, with `database=None` and `derandomize=True`. Its syntax/layout strategy can reach the classes that r5's nonempty pathname-whitespace property cannot. Git children run serially with 30-second bounds; no provider is involved. The property checks the canonical store from `git rev-parse --git-path objects` and an owner-only commit read (`git show <commit>:f.txt`). For accepted reads, discovery must record both the named store and any owner store reached through alternates, or report incomplete. Rejected inputs permit conservative absolute records, require repeatable discovery, and require incomplete evidence for any attempted over-bound pointer read. Git rejection supplies no readable-store oracle.
4. **Tests and mutants:** only the marks covering the five repaired cases change in review r6's test file; every other byte matches base. The five preserved valid-parent controls are rerun from r6's scratch matrix. Seven additional read-failure controls cover dangling/oversized pointers, permission/OSError/ValueError failures, a vanished target, and a pointer beneath a file. All mutations are applied to current source in memory; the three r6 definitions drop their historical repair-baseline patches because current source already includes the repair. Two new mutants separately swallow dangling links and `TooLarge`.
5. **Text:** the main report and desktop describe current behavior and preserve swap-back, timestamp and later-dependency observation bounds. The fix-r5 report explicitly identifies its historical scope and qualifies its original directory-`.git` probes; its old PR-body lines remain historical. D-R6-1 through D-R6-4 are documented under the desktop's **Listed differences** heading with the exact one-line text supplied by review r6. Their readers remain unchanged. The entire acceptance contract, including C-8.3 and C-8.4, is byte-identical to base.

**Required invariant:** for every pointer Git follows, discovery records the target Git reads, or reports incomplete. Incomplete discovery keeps every allocated worktree while the job is live or exporting and names the job and file; admission still proceeds. Alias swap-back within a traversal, writes that preserve metadata within one clock tick, and dependencies introduced after discovery remain outside these observations.

## Installed Git outcomes

Git **2.55.0** executed **115 inputs**: **43 owner-only commit reads and 72 rejections**. Of the 43 reads, **32 recorded the actual storage without incompleteness** and **11 reported incomplete evidence** for oversized pointers. Thirteen rejected cases also recorded incomplete evidence. No accepted input omitted readable storage without incomplete evidence, and no additional P2 or other discovery defect appeared. Exact executed input vectors, pointer lengths, Git paths, return codes and outcomes are in [git-inputs.json](2026-10-30-retention-pins-fix-r6-data/git-inputs.json); the committed property defines their byte-for-byte construction.

| Generated class | Executed Git behavior and discovery assertion |
|---|---|
| Literal prefix present / absent / misspelled / colon-only | Active malformed gitfiles and blank targets are rejected. Directory-`.git` cases have no gitfile prefix. Valid literal-prefix cases can read the owner; discovery records storage or reports incomplete. |
| Blank / spaces-only / padded target | Blank gitfiles reject; existing literal space-only and padded aliases are readable. A blank common target with CR/LF content uses a valid own store; a zero-byte common file rejects. No pathname whitespace is stripped. |
| `commondir` absent / empty / LF-only / CRLF-only / naming a target | Absent and CR/LF-only files use a valid own store. Empty files reject. Valid named common targets read the owner's store, even without own objects. Unusable targets/layouts reject. |
| Own store absent / symlink / alternates | Without own objects and without a usable common target Git rejects. Both symlink and alternates stores support owner-only commit reads; discovery covers the canonical store and its alternates. |
| Admin under `worktrees/<id>` / elsewhere | Git uses the actual admin/common storage in both layouts. Named `worktrees/<id>` fallback records own and inferred storage together. Directory `.git` aliases are also generated. |
| CR/LF padding short / below / above 64 KiB, on either pointer | Below-bound accepted pointers record storage. Above-bound pointers can still be read by Git; discovery reports incomplete for every attempted over-bound read. A padding selector on an absent/inactive pointer introduces no bytes and no read failure. |

The initial property attempt stopped at Hypothesis's function-scoped-fixture health check before generating inputs. Only `record_property` accumulates evidence on the pytest item; each input's filesystem is isolated by a fresh context-managed directory. The documented health-check suppression permits that intentional recording, and the full unchanged example count then passed. This was a test harness correction, not a product counterexample.

## Red and green

At base with `--runxfail`: **11 passed / 5 failed**, all at the intended owner-retirement assertions after successful real-Git setup. At repaired source without those marks: **16 passed**. The extra pointer-read file gives **7 passed**, including real retirement for the vanished target. [Source audit](2026-10-30-retention-pins-fix-r6-data/source-audit.json) verifies the sole mark removals, unchanged contract/readers, read bound and trial departures.

## Run results

**947 unique ordinary nodes passed: 940 repository nodes and seven scratch controls.** There were 950 successful node executions, because three properties ran their explicit and generated phases separately. All 36 files in the ordinary plan, the r6 regression file, and the two new test files are covered; no requested local node remains unrun. No ordinary assertion failed. All seventeen named mutants were killed, with setup and teardown passing.

There were **91 pytest holds**, all using the shared lock: the 90 runs in the ledger plus the initial property health-check attempt. Peak owned pytest concurrency was **two**; peak recorded pytest RSS was **319.53 MiB**. No hold waited thirty minutes, and none ran outside the lock. The red hold started **2026-10-30 22:10:14 UTC**, load **12.66 / 13.83 / 17.44**; the green r6 hold started **22:12:30 UTC**, load **17.76 / 14.96 / 17.33**.

`F/` means `tests/fake/`; `U/` means `tests/unit/`. Counts are unique completed nodes. Starts and loads are the first ordinary selection for each file, including an interrupted attempt where applicable. All dates/times are UTC; full attempt details are in [ledger.json](2026-10-30-retention-pins-fix-r6-data/ledger.json).

| File | Passed | First start UTC | Load (1/5/15 min) |
|---|---:|---|---|
| F/test_retention_dependency_review_r6.py | 16 | 2026-10-30 22:12:30 | 17.76 / 14.96 / 17.33 |
| F/test_retention_pointer_failures.py | 7 | 2026-10-30 22:15:05 | 29.97 / 19.31 / 18.55 |
| U/test_retention_pointer_inputs.py | 1 | 2026-10-30 22:16:16 | 32.01 / 22.29 / 19.76 |
| F/test_retention_dependency_review_r5.py | 60 | 2026-10-30 22:16:21 | 31.59 / 22.53 / 19.87 |
| U/test_retention_pointer_whitespace.py | 9 | 2026-10-30 22:22:16 | 43.64 / 27.41 / 22.16 |
| F/test_retention_dependency_review_r4.py | 53 | 2026-10-30 22:23:36 | 26.67 / 27.19 / 22.67 |
| F/test_retention_dependency_review_r3.py | 6 | 2026-10-30 22:26:39 | 14.26 / 20.87 / 20.85 |
| F/test_retention_dependency_review_r2.py | 21 | 2026-10-30 22:26:56 | 13.79 / 20.44 / 20.70 |
| Scratch/probes.py | 2 | 2026-10-30 22:28:15 | 16.26 / 19.76 / 20.42 |
| U/test_retention_alternates_review_r2.py | 15 | 2026-10-30 22:28:27 | 20.23 / 20.52 / 20.68 |
| F/test_retention_dependency_review_r1.py | 18 | 2026-10-30 22:29:07 | 36.67 / 24.44 / 22.09 |
| F/test_retention_dependency_incomplete.py | 19 | 2026-10-30 22:30:07 | 53.86 / 31.51 / 24.89 |
| U/test_retention_alternates.py | 23 | 2026-10-30 22:34:23 | 66.23 / 55.65 / 37.77 |
| U/test_retention_dependency_aliases.py | 10 | 2026-10-30 22:36:47 | 30.02 / 46.71 / 37.09 |
| U/test_retention_job_dependencies.py | 12 | 2026-10-30 22:37:04 | 26.26 / 45.06 / 36.67 |
| F/test_retention_dependencies.py | 30 | 2026-10-30 22:38:22 | 16.49 / 37.53 / 34.54 |
| F/test_retention_dependency_spelling.py | 16 | 2026-10-30 22:40:46 | 41.95 / 40.53 / 36.25 |
| F/test_retention_recovery_census.py | 2 | 2026-10-30 22:41:46 | 42.73 / 41.18 / 36.79 |
| U/test_retention_dependency_units.py | 19 | 2026-10-30 22:42:29 | 45.78 / 41.95 / 37.29 |
| U/test_retention_folded_pin.py | 38 | 2026-10-30 22:44:04 | 35.91 / 40.61 / 37.32 |
| Scratch/pointer_matrix.py | 5 | 2026-10-30 22:47:16 | 41.98 / 41.91 / 38.60 |
| U/test_retention_dependency_properties.py | 4 | 2026-10-30 22:50:06 | 39.61 / 42.47 / 39.50 |
| F/test_detached_retention_fence.py | 89 | 2026-10-30 22:55:56 | 31.52 / 36.48 / 37.67 |
| F/test_writer_retention_fence.py | 23 | 2026-10-30 22:59:26 | 35.87 / 37.02 / 37.66 |
| U/test_retention_archive.py | 108 | 2026-10-30 23:01:29 | 27.50 / 34.33 / 36.55 |
| U/test_retention_shared_folders.py | 62 | 2026-10-30 23:11:12 | 34.16 / 33.29 / 34.44 |
| U/test_retention_nested_leases.py | 27 | 2026-10-30 23:14:05 | 70.32 / 48.54 / 40.52 |
| U/test_retention_salvage_properties.py | 2 | 2026-10-30 23:39:54 | 43.20 / 65.99 / 69.08 |
| U/test_retention_sweep_race.py | 13 | 2026-10-30 23:49:34 | 39.68 / 50.36 / 59.71 |
| U/test_retention_81_notes.py | 38 | 2026-10-31 00:00:18 | 40.52 / 38.01 / 46.88 |
| F/test_turn_retention_cwd_properties.py | 1 | 2026-10-31 00:06:56 | 37.62 / 34.91 / 41.60 |
| F/test_turn_retention_job_properties.py | 1 | 2026-10-31 00:08:20 | 41.04 / 36.55 / 41.58 |
| U/test_task_holds.py | 65 | 2026-10-31 00:09:53 | 49.91 / 40.11 / 42.42 |
| U/test_retention_archive_properties.py | 2 | 2026-10-31 00:10:18 | 46.05 / 39.98 / 42.30 |
| F/test_state_contract.py | 37 | 2026-10-31 00:10:59 | 46.98 / 41.18 / 42.63 |
| U/test_invariants_index.py | 25 | 2026-10-31 00:12:13 | 28.92 / 36.54 / 40.74 |
| F/test_turn_retention_cwd.py | 6 | 2026-10-31 00:21:55 | 47.31 / 42.71 / 41.70 |
| F/test_turn_retention_job_directory.py | 2 | 2026-10-31 00:22:43 | 40.70 / 41.75 / 41.40 |
| F/test_turn_retention_quarantine.py | 10 | 2026-10-31 00:23:16 | 37.86 / 40.88 / 41.09 |
| F/test_task_hold_admission.py | 49 | 2026-10-31 00:24:16 | 48.50 / 43.68 / 42.14 |
| U/test_test_module_names.py | 1 | 2026-10-31 00:24:50 | 50.68 / 44.72 / 42.58 |

### Interrupted attempts and complete properties

Six holds hit their interruption guards without assertion failures. Only unfinished nodes were rerun; no generated example count, strategy or assertion was reduced.

| Hold | Seconds including configuration/cleanup | Completed nodes |
|---|---:|---:|
| test_retention_dependency_properties | 572.54 | 2 |
| test_retention_shared_folders | 593.64 | 32 |
| retention_nested_leases | 585.92 | 26 |
| shared-folders-remaining-1 | 610.31 | 0 |
| test_retention_salvage_properties-1 | 544.79 | 0 |
| shared-folders-400-generated | 500.51 | 0 |

The dependency-properties file completed its first two 100-example functions before its guard, then passed the remaining 100-example and 24-example functions separately. Nested leases completed 26 tests before the guard, then its 150-example property separately. Shared-folder examples resumed in disjoint small selections; its nested property passed all **12 explicit and 400 generated examples** in separate phases, the final generated retry taking **324.12 seconds**. The salvage property passed all **nine explicit and 24 generated examples** separately. The sweep property passed all **15 explicit and 40 generated examples** separately, beside its twelve ordinary examples. The original 400-example phases retain `derandomize=True` and a disabled database. Other existing properties retain their original settings.

### Mutants

`R2`–`R6` name their fake review files; `W` is `tests/unit/test_retention_pointer_whitespace.py`; `PF` is `tests/fake/test_retention_pointer_failures.py`; `P` is r6's preserved scratch `probes.py`. The two probe killers also pass on unmutated source. Definitions, original/mutated source hashes and per-phase outcomes are preserved in the data folder.

| Mutant | Named killer | Result |
|---|---|---|
| r2-post-read-identity-check-dropped | R2::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace] | Killed |
| r2-terminal-legacy-incomplete-dropped | R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[database] | Killed |
| r3-post-traversal-alias-check-dropped | R3::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live] | Killed |
| r3-census-moved-after-recovery | R3::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy] | Killed |
| r3-write-metadata-check-dropped | P::test_changes[same-size-running] | Killed |
| r3-final-path-check-dropped | P::test_changes[symlink-info-running] | Killed |
| r3-terminal-census-live-only | R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting] | Killed |
| r4-initial-objects-revalidation-dropped | R4::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live] | Killed |
| r4-initial-objects-recording-dropped | R4::test_stable_objects_alias_keeps_the_tree_git_reads[live] | Killed |
| r5-commondir-whitespace-stripping-restored | R5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-commondir-trailing] | Killed |
| r5-gitfile-whitespace-stripping-restored | R5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-gitfile-leading] | Killed |
| r5-gitfile-colon-without-space-accepted | W::test_pointer_syntax_and_whitespace_match_real_git[gitfile-no-space] | Killed |
| r6-empty-common-initial-objects-not-recorded | R6::test_empty_common_target_keeps_the_own_store_git_reads[directory-live-control-lf] | Killed |
| r6-pointer-read-failure-evidence-dropped | R6::test_crlf_padded_pointer_keeps_the_storage_real_git_reads[padded-gitfile] | Killed |
| r6-parent-common-fallback-restored | R6::test_empty_common_target_keeps_the_own_store_git_reads[named-live-lf] | Killed |
| r6-dangling-pointer-treated-as-missing | PF::test_unreadable_common_pointer_keeps_allocated_trees[dangling] | Killed |
| r6-oversized-pointer-treated-as-missing | PF::test_unreadable_common_pointer_keeps_allocated_trees[oversized] | Killed |



## Operational details and delivery

The sandbox denied `$(getconf DARWIN_USER_TEMP_DIR)sf-fix272r6`, so scratch uses `/private/tmp/sf-fix272r6`. Archive omission fixtures also use the permitted attempt temp directory's `sf-fix272r6-fixtures` root, because a literal `tmp` path component changes those fixtures' omission semantics. The prescribed locked environment sync installed Python 3.14.7 and the locked development dependencies. Shared Git metadata denied staging; the three ordered commits use private metadata on `fix/retention-dependency-pins-217`, with a verified bundle excluding `b2a357b6c`. The release merge is unchanged. Every commit has the required coauthor footer.

Each hold runs exactly one pytest, serially, with its own fixture home and TMPDIR and the prescribed shared suite lock. Early holds used a 570-second interruption guard; later holds use 480 seconds after allowing more time for configuration and fixture cleanup. Two holds may run concurrently. The wrapper's `sysctl` is sandbox-denied; admitted loads are recorded directly with `os.getloadavg`. No run is detached. One original 570-second guarded shared-folder attempt took **610.31 seconds including configuration and cleanup** (pytest reported **568.75 seconds**); this exceeded ten minutes overall and is disclosed rather than counted as a completed test. It reported no assertion failure. The shorter guard keeps additional cleanup margin. Interrupted files resume only unfinished nodes, and long properties split explicit/generated phases while retaining their original example counts, strategies and assertions. Source and mutation hashes, phase outcomes, starts, loads, durations and peak pytest RSS are preserved beside the report. Source/document whitespace checks pass; raw pytest logs retain their original formatting, including trailing spaces.

Combined scratch measured **614.03 MiB** before final delivery metadata (549,980 KiB main scratch; 78,788 KiB archive fixtures), below 1.5 GB. All started pytest and batch processes have ended. After the text commit and bundle verification, both scratch roots are removed with the rules file's guarded `shutil.rmtree` command; the final handoff confirms removal. Worktree bytecode and generated build/cache directories are also removed. Delivery artifact: `retention-fix272r6.bundle`, requiring base `b2a357b6c`.

## PR-body lines for the hub

> Fix round r6 closes R6-F1 and R6-F2: named worktrees/<id> admins with absent or CR/LF-blank commondir now record both own and inferred-parent storage, and nonmissing pointer read failures become incomplete evidence while retaining the 64 KiB bound. The five real-Git regressions now pass. Retention remains dormant pending approving review r7 and completed CI.

- **Invariant:** for every pointer Git follows, discovery records the target Git reads, or reports incomplete; the existing swap-back, timestamp and later-dependency observation bounds remain.
- **Evidence:** r6 red 11/5 → green 16/0; 100 generated plus 15 explicit Git inputs; seven pointer-read controls; seventeen named mutants killed. 947 unique ordinary nodes passed; the complete counts, start times, loads and seventeen mutant kills appear above.
- **Listed differences:** D-R6-1 through D-R6-4 are documented; their archive, survey, cleanup and verification readers remain unchanged.
- **Next:** Obtain approving **review r7** and complete every CI shard before merge or re-enablement.

Not run: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` (explicitly deselected), the full suite, Python 3.12, external CI and person-only `tests/frontend/test_core_live.py`. No other requested local node remains unrun.
