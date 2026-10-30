# Retention dependency pins: fix round r4, 2026-10-30

Base: `5b94fd32a815`, including the hub's r4 review files and the task-hold merge. First recorded clock: **05:00:22 UTC**; the four-hour new-hold cutoff is **09:00:22 UTC**, and the six-hour wall is **11:00:22 UTC**. First correctness hold: **05:02:02 UTC**. Only the assigned workspace is edited. Shared metadata refuses branch writes; ordered commits use private Git metadata on `fix/retention-dependency-pins-217`, and delivery is a verified bundle excluding that base. No fetch, merge, push, live store, real provider, external message or sub-agent is used.

## Decisions built

1. **R4-F1:** `_storage` now resolves `<common>/objects` through the shared `_Aliases`, records its resolved target beside `gitdir` and `common`, and retains the original spelling for the existing post-traversal check. A changed or vanished target produces incomplete evidence outside transactions, invoking the global allocated-worktree pin while the job is live or exporting. A stable live borrower records the owner-tree storage and pins it through census, fence and final recheck. A terminal exporter with complete discovery retains only its export paths, so the stable-terminal-export negative control may retire. Filesystem work remains outside transactions.
2. **R4-F2:** apply the exact journal wording and comment from `text-corrections.json`; this changes no behavior. New journals record `job_folder` in `begin`. An old journal entering quarantine gains it in memory and is saved before quarantine's renames. Already-quarantined or archived old journals can reach `final_check` and publication without the field; commit computes its fallback outside the transaction. The three executed old-journal controls substantiate those distinctions.
3. **Tests:** remove only the two marks covering the five strict expected failures in review r4's file. Every other byte remains unchanged. Add two head-source r4 mutants (revalidation dropped with recording retained; recording dropped with revalidation retained), and rerun exactly the seven required r2/r3 mutants against their named tests. Source mutation is in memory; original/mutated hashes and setup/call/teardown outcomes are recorded.
4. **Text:** apply the remaining corrections to the contract, desktop design, main report and fix-r2/fix-r3 reports. The initial object-directory omission is now described as repaired, with the swap-back, timestamp and later-dependency bounds retained. C-8.3's census ordering/identity/failure behavior is already accurate and unchanged; C-8.4's normative fail-closed guarantee remains intact. Historical 452/682-node results remain dated evidence. PR replacements below include the new lead and completed local evidence, with approval and CI still external gates.

No hub product decision required a deviation. The supplied `fix-diff.txt` is actually the earlier r3 patch (its `_storage` still passes the initial objects spelling directly); it does not contain the six-case r4 trial. The trial is preserved as `r4-trial-observe-initial-objects-final` in review r4's `definitions.json`. Starting from the supplied diff's existing `_Aliases` implementation, this build follows that trial, but records the initial resolved target unconditionally instead of adding it only when target and spelling differ. The path passed to `_alternates` remains the original spelling, which is revalidated after traversal; recording the target is independent of whether an alternates file exists.

Entry-path audit: both `.git` directories (including symlinks) and `gitdir:` files converge on `_storage`. An explicit `commondir`, an inferred common directory under `worktrees/`, and an ordinary repository with `common == gitdir` all reach the same new resolution and recording. Every parsed alternate entry already resolves through the shared alias observer. There is no other production caller or starting path from `git_storage` into `_alternates` that bypasses recording/tracking. Direct private-helper calls exist only in unit tests and continue to return alternates rather than the starting directory.

## Observation bounds

Discovery revalidates tracked checkout/`.git`, pointer/target, alternate-entry and initial object-directory spellings after traversal. An alias swapped away and back within that traversal can escape final comparison. Descriptor metadata checks cannot detect a write preserving size, mtime and ctime within one timestamp tick; dependencies introduced after discovery remain outside these observations. No atomic filesystem snapshot is claimed. The alias map remains proportional to distinct spellings; the line buffer stays bounded at 1 MiB plus its overflow byte.

## Red and green

At `5b94fd32a815`, the full review-r4 file with `--runxfail` gives **48 passed / 5 failed**: four initial-object retargeting cases (absent alternates or unchanged hard-linked alternates, live and exporting), and one stable live dependency. At fix `e8d3b6091`, without those marks, the entire file gives **53 passed**. The stable terminal-export control passes in both runs. Their original test assertions and the other 48 passing cases are unchanged.

## Execution

The locked dev group was synced once with Python 3.14.7 and the prescribed sync wrapper. The sandbox refuses the requested Darwin scratch parent, so the environment, Hypothesis database, logs and most fixture homes use `/private/tmp/sf-fix272r4`. Archive omission controls require paths without a literal `tmp` component; their fixture homes use the sandbox's writable attempt directory under a separate `sf-fix272r4-fixtures` child. Both roots are guarded-cleaned at completion. This follows the prior round's fixture layout and avoids changing its omission oracle.

The preserved driver invokes one pytest per shared hold with `uv run --no-sync`, the requested unset variables and disabled pytest cache. The existing fixture baseline isolates stores and blocks real providers and keychain access. `SUBFLEET_RETENTION_QOS=inherit` avoids the previously documented fixture-only QoS launch issue. There is no xdist; at most three owned hold drivers are active or queued, and the suite wrapper admits at most two. Each pytest has a 570-second alarm; expensive properties and sweep examples/property run in separate holds at their normal settings. Long drivers stay attached to tool-managed sessions and are polled to completion. The observer records actual test phases, post-admission UTC starts, load averages and peak RSS; sandbox sysctl denials only suppress the wrapper's load print.

## PR-body lines for the hub

> Fix round r4 closes R4-F1 by recording the resolved initial `<common>/objects` dependency and revalidating its original spelling after traversal, outside transactions. The five real-Git failures now pass, including live and terminal-export retargets and a stable live borrower; the complete-discovery terminal-export control still permits retirement. R4-F2 qualifies old-journal persistence without changing behavior. Retention remains dormant; approving review and completed CI remain merge gates.

- **Changes:** Round r3 revalidates tracked checkout/`.git`, pointer and alternate-entry aliases; round r4 additionally records and revalidates the initial object-directory target. Incomplete evidence globally retains allocated worktrees while its job is live or exporting; database-only census, fence and final rechecks remain intact.
- **Journal:** New journals record `job_folder` in `begin`. Old journals entering quarantine gain it in memory and save it before quarantine's renames. Already-quarantined or archived old journals may reach `final_check` and publication without it; commit computes its fallback outside the transaction.
- **Historical evidence:** 682 passes and seven killed mutants are fix round r3's result at `4d8fa5d55`. At `e4b20c9e5`, checks did not revalidate original aliases; round r3 fixed its original inputs, and round r4 fixes the separate initial object-directory omission.
- **Invariants:** C-6.5, C-6.11, C-8.3, C-8.4 and I16; filesystem discovery outside transactions, database reads and strings inside them. Swap-back, timestamp and later-dependency observation bounds remain documented.
- **Next:** Obtain approving review and complete CI before merge or re-enablement. No live PR or CI status was read or changed this round.

- **Evidence for this round:** Review r4 red **48 passed / 5 failed** at `5b94fd32a815` → green **53 passed**; **851 distinct ordinary passes** and **nine named mutants killed**. The [report](2026-10-30-retention-pins-fix-r4.md) and [complete ledger](2026-10-30-retention-pins-fix-r4-data/ledger.json) supply per-file counts, UTC starts and loads.

## Exact-list oracle correction

At fix `e8d3b6091`, `tests/unit/test_retention_dependency_units.py::test_record_spells_each_place_once` fails on the case-insensitive fixture volume. Given ordinary repositories `work` and `Review`, the old assertion expects `[work/.git, Review/.git]`; the hub's unconditional initial-object-directory recording returns `[work/.git, work/.git/objects, Review/.git, Review/.git/objects]`, all canonically spelled. The full file records 18 passed / 1 failed at 05:12:17 UTC.

This is an outdated exact-list oracle, not a retention-safety counterexample. The smallest change preserving the explicit hub decision is to add the two newly required dependencies to that assertion. No product behavior or review-r4 assertion changes; the marks covering its five cases remain the only modifications to that review file. The entire unit file is rerun after the expectation update. The review trial's conditional `extra` would avoid the old assertion but omit ordinary initial targets from the recorded list; this build keeps unconditional recording.

## Per-file results

All starts below are UTC on October 30 after lock admission; loads are 1/5/15-minute averages. The 851 final ordinary passes comprise 846 repository nodes and five scratch controls. Ordinary counts exclude the red baseline and mutant failures and require all three phases passing. Scratch probes are listed separately. `F` means `tests/fake/`, `U` means `tests/unit/`.

| File | Passed | First start UTC | Load 1/5/15 |
|---|---:|---|---|
| `F/test_retention_dependency_review_r4.py` | 53 | 05:04:01 | 28.02/24.66/22.47 |
| `F/test_retention_dependency_review_r3.py` | 6 | 05:07:10 | 19.11/22.24/21.90 |
| `F/test_retention_dependency_review_r2.py` | 21 | 05:07:25 | 20.28/22.35/21.94 |
| `U/test_retention_alternates_review_r2.py` | 15 | 05:08:00 | 21.95/22.56/22.04 |
| `F/test_retention_dependency_review_r1.py` | 18 | 05:08:21 | 24.75/23.13/22.26 |
| `F/test_retention_dependency_incomplete.py` | 19 | 05:08:35 | 24.86/23.22/22.30 |
| `U/test_retention_alternates.py` | 23 | 05:09:26 | 21.32/22.77/22.21 |
| `F/test_state_contract.py` | 37 | 05:10:12 | 16.99/21.50/21.77 |
| `U/test_retention_dependency_aliases.py` | 10 | 05:10:48 | 18.52/21.31/21.69 |
| `U/test_retention_job_dependencies.py` | 12 | 05:10:50 | 18.52/21.31/21.69 |
| `U/test_test_module_names.py` | 1 | 05:11:00 | 18.03/21.11/21.61 |
| `F/test_retention_dependencies.py` | 30 | 05:11:03 | 17.23/20.89/21.53 |
| `U/test_invariants_index.py` | 25 | 05:11:04 | 17.23/20.89/21.53 |
| `probes.py` | 2 | 05:11:10 | 16.65/20.71/21.46 |
| `journal_probes.py` | 3 | 05:11:24 | 16.95/20.60/21.41 |
| `F/test_retention_dependency_spelling.py` | 16 | 05:11:44 | 19.94/21.05/21.55 |
| `F/test_retention_recovery_census.py` | 2 | 05:12:05 | 17.67/20.47/21.33 |
| `U/test_retention_dependency_units.py` | 19 | 05:12:17 | 18.48/20.49/21.32 |
| `U/test_retention_folded_pin.py` | 38 | 05:12:40 | 27.11/22.27/21.94 |
| `U/test_retention_dependency_properties.py` | 4 | 05:13:46 | 22.08/21.97/21.86 |
| `U/test_retention_shared_folders.py` | 62 | 05:18:20 | 17.53/18.43/20.26 |
| `F/test_detached_retention_fence.py` | 89 | 05:27:24 | 19.09/21.95/21.83 |
| `F/test_writer_retention_fence.py` | 23 | 05:30:32 | 23.62/21.99/21.80 |
| `U/test_retention_archive.py` | 108 | 05:31:26 | 24.30/22.46/21.99 |
| `U/test_retention_archive_properties.py` | 2 | 05:34:47 | 11.03/16.55/19.55 |
| `U/test_retention_nested_leases.py` | 27 | 05:35:47 | 13.80/16.17/19.19 |
| `U/test_retention_salvage_properties.py` | 2 | 05:36:30 | 15.29/16.24/19.07 |
| `U/test_retention_sweep_race.py` | 13 | 05:38:19 | 15.44/16.24/18.75 |
| `U/test_retention_81_notes.py` | 38 | 05:40:53 | 13.80/15.10/17.86 |
| `F/test_turn_retention_cwd.py` | 6 | 05:42:07 | 13.57/14.91/17.56 |
| `F/test_turn_retention_cwd_properties.py` | 1 | 05:42:15 | 13.29/14.83/17.52 |
| `F/test_turn_retention_job_directory.py` | 2 | 05:42:26 | 13.20/14.75/17.46 |
| `F/test_turn_retention_job_properties.py` | 1 | 05:42:32 | 15.04/15.09/17.55 |
| `F/test_turn_retention_quarantine.py` | 10 | 05:42:45 | 14.79/15.04/17.50 |
| `U/test_task_holds.py` | 64 | 05:42:57 | 13.97/14.85/17.41 |
| `F/test_task_hold_admission.py` | 49 | 05:43:10 | 13.21/14.63/17.28 |

## Named mutants

R2/R3/R4 refer to `tests/fake/test_retention_dependency_review_rN.py`; probes refer to the preserved `probes.py.txt`. Every named killer passes on unmutated source. All nine mutants run against this head's module bytes; each has one assertion failure with passing setup/teardown. No mutant changed source files on disk.

| Mutant | Named killing test | Result | Start UTC | Load 1/5/15 |
|---|---|---|---|---|
| `r2-post-read-identity-check-dropped` | `R2::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace]` | KILLED | 05:05:54 | 22.80/23.75/22.37 |
| `r2-terminal-legacy-incomplete-dropped` | `R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[database]` | KILLED | 05:06:12 | 19.67/22.95/22.12 |
| `r3-post-traversal-alias-check-dropped` | `R3::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live]` | KILLED | 05:06:22 | 18.08/22.51/21.97 |
| `r3-census-moved-after-recovery` | `R3::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy]` | KILLED | 05:06:29 | 18.39/22.50/21.97 |
| `r3-write-metadata-check-dropped` | `probes.py::test_changes[same-size-running]` | KILLED | 05:06:35 | 19.08/22.58/22.00 |
| `r3-final-path-check-dropped` | `probes.py::test_changes[symlink-info-running]` | KILLED | 05:06:40 | 20.20/22.75/22.06 |
| `r3-terminal-census-live-only` | `R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting]` | KILLED | 05:06:45 | 19.94/22.65/22.03 |
| `r4-initial-objects-revalidation-dropped` | `R4::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live]` | KILLED | 05:06:51 | 20.23/22.62/22.03 |
| `r4-initial-objects-recording-dropped` | `R4::test_stable_objects_alias_keeps_the_tree_git_reads[live]` | KILLED | 05:07:03 | 19.56/22.38/21.95 |

## Complete hold ledger

| Hold | Start UTC | Load 1/5/15 | Passed / failed | Seconds |
|---|---|---|---:|---:|
| `r4-red` | 05:02:02 | 19.75/22.90/21.63 | 48 / 5 | 98.25 |
| `r4-green` | 05:04:01 | 28.02/24.66/22.47 | 53 / 0 | 67.65 |
| `mutant-r2-post-read-identity-check-dropped` | 05:05:54 | 22.80/23.75/22.37 | 0 / 1 | 12.97 |
| `mutant-r2-terminal-legacy-incomplete-dropped` | 05:06:12 | 19.67/22.95/22.12 | 0 / 1 | 8.10 |
| `mutant-r3-post-traversal-alias-check-dropped` | 05:06:22 | 18.08/22.51/21.97 | 0 / 1 | 5.75 |
| `mutant-r3-census-moved-after-recovery` | 05:06:29 | 18.39/22.50/21.97 | 0 / 1 | 4.41 |
| `mutant-r3-write-metadata-check-dropped` | 05:06:35 | 19.08/22.58/22.00 | 0 / 1 | 4.35 |
| `mutant-r3-final-path-check-dropped` | 05:06:40 | 20.20/22.75/22.06 | 0 / 1 | 3.50 |
| `mutant-r3-terminal-census-live-only` | 05:06:45 | 19.94/22.65/22.03 | 0 / 1 | 4.93 |
| `mutant-r4-initial-objects-revalidation-dropped` | 05:06:51 | 20.23/22.62/22.03 | 0 / 1 | 10.09 |
| `mutant-r4-initial-objects-recording-dropped` | 05:07:03 | 19.56/22.38/21.95 | 0 / 1 | 4.63 |
| `retention_dependency_review_r3` | 05:07:10 | 19.11/22.24/21.90 | 6 / 0 | 13.62 |
| `retention_dependency_review_r2` | 05:07:25 | 20.28/22.35/21.94 | 21 / 0 | 31.64 |
| `retention_alternates_review_r2` | 05:08:00 | 21.95/22.56/22.04 | 15 / 0 | 19.70 |
| `retention_dependency_review_r1` | 05:08:21 | 24.75/23.13/22.26 | 18 / 0 | 11.82 |
| `retention_dependency_incomplete` | 05:08:35 | 24.86/23.22/22.30 | 19 / 0 | 49.36 |
| `retention_alternates` | 05:09:26 | 21.32/22.77/22.21 | 23 / 0 | 43.59 |
| `test_state_contract` | 05:10:12 | 16.99/21.50/21.77 | 37 / 0 | 30.19 |
| `retention_dependency_aliases` | 05:10:48 | 18.52/21.31/21.69 | 10 / 0 | 1.88 |
| `retention_job_dependencies` | 05:10:50 | 18.52/21.31/21.69 | 12 / 0 | 11.58 |
| `test_test_module_names` | 05:11:00 | 18.03/21.11/21.61 | 1 / 0 | 3.45 |
| `retention_dependencies` | 05:11:03 | 17.23/20.89/21.53 | 30 / 0 | 39.61 |
| `test_invariants_index` | 05:11:04 | 17.23/20.89/21.53 | 25 / 0 | 1.93 |
| `probe-controls` | 05:11:10 | 16.65/20.71/21.46 | 2 / 0 | 13.08 |
| `journal-controls` | 05:11:24 | 16.95/20.60/21.41 | 3 / 0 | 7.78 |
| `retention_dependency_spelling` | 05:11:44 | 19.94/21.05/21.55 | 16 / 0 | 16.94 |
| `retention_recovery_census` | 05:12:05 | 17.67/20.47/21.33 | 2 / 0 | 10.58 |
| `retention_dependency_units` | 05:12:17 | 18.48/20.49/21.32 | 18 / 1 | 21.42 |
| `retention_folded_pin` | 05:12:40 | 27.11/22.27/21.94 | 38 / 0 | 64.13 |
| `retention_dependency_properties-part1` | 05:13:46 | 22.08/21.97/21.86 | 1 / 0 | 56.47 |
| `retention_dependency_properties-part2` | 05:14:44 | 17.81/20.80/21.43 | 1 / 0 | 58.94 |
| `retention_dependency_units-updated-oracle` | 05:14:55 | 19.88/21.15/21.55 | 19 / 0 | 45.38 |
| `retention_dependency_properties-part3` | 05:15:44 | 20.26/20.87/21.41 | 1 / 0 | 106.74 |
| `retention_dependency_properties-part4` | 05:17:35 | 12.21/18.00/20.23 | 1 / 0 | 42.16 |
| `retention_shared_folders-part1` | 05:18:20 | 17.53/18.43/20.26 | 14 / 0 | 38.33 |
| `retention_shared_folders-part2` | 05:19:01 | 21.64/19.38/20.51 | 17 / 0 | 43.11 |
| `retention_shared_folders-part3` | 05:19:47 | 26.38/20.92/21.02 | 8 / 0 | 35.60 |
| `retention_shared_folders-part4` | 05:20:24 | 26.37/21.51/21.23 | 9 / 0 | 24.64 |
| `retention_shared_folders-part5` | 05:20:50 | 27.77/22.16/21.46 | 10 / 0 | 32.36 |
| `retention_shared_folders-part6` | 05:21:25 | 26.57/22.45/21.60 | 1 / 0 | 11.32 |
| `retention_shared_folders-part7` | 05:21:39 | 25.83/22.49/21.62 | 1 / 0 | 225.25 |
| `retention_shared_folders-part8` | 05:25:25 | 24.60/23.54/22.29 | 1 / 0 | 102.03 |
| `retention_shared_folders-part9` | 05:27:09 | 19.18/22.14/21.89 | 1 / 0 | 13.24 |
| `detached_retention_fence-part1` | 05:27:24 | 19.09/21.95/21.83 | 14 / 0 | 32.42 |
| `detached_retention_fence-part2` | 05:28:06 | 19.96/21.78/21.77 | 16 / 0 | 35.25 |
| `detached_retention_fence-part3` | 05:28:43 | 19.74/21.47/21.65 | 29 / 0 | 36.12 |
| `detached_retention_fence-part4` | 05:29:22 | 20.09/21.28/21.57 | 10 / 0 | 36.62 |
| `detached_retention_fence-part5` | 05:30:02 | 18.82/20.78/21.37 | 20 / 0 | 28.45 |
| `writer_retention_fence` | 05:30:32 | 23.62/21.99/21.80 | 23 / 0 | 52.93 |
| `retention_archive-part1` | 05:31:26 | 24.30/22.46/21.99 | 6 / 0 | 14.49 |
| `retention_archive-part2` | 05:31:41 | 22.73/22.21/21.91 | 8 / 0 | 9.01 |
| `retention_archive-part3` | 05:31:51 | 20.70/21.79/21.76 | 6 / 0 | 15.27 |
| `retention_archive-part4` | 05:32:08 | 17.99/21.13/21.53 | 6 / 0 | 20.68 |
| `retention_archive-part5` | 05:32:29 | 15.00/20.25/21.20 | 12 / 0 | 31.30 |
| `retention_archive-part6` | 05:33:05 | 12.22/19.01/20.71 | 23 / 0 | 17.10 |
| `retention_archive-part7` | 05:33:23 | 10.97/18.29/20.42 | 7 / 0 | 12.33 |
| `retention_archive-part8` | 05:33:36 | 10.29/17.90/20.25 | 6 / 0 | 13.57 |
| `retention_archive-part9` | 05:33:50 | 11.38/17.76/20.16 | 6 / 0 | 10.56 |
| `retention_archive-part10` | 05:34:01 | 12.47/17.80/20.15 | 6 / 0 | 9.45 |
| `retention_archive-part11` | 05:34:11 | 11.85/17.49/20.01 | 6 / 0 | 8.55 |
| `retention_archive-part12` | 05:34:20 | 11.56/17.24/19.89 | 6 / 0 | 8.77 |
| `retention_archive-part13` | 05:34:30 | 10.69/16.87/19.73 | 6 / 0 | 10.90 |
| `retention_archive-part14` | 05:34:41 | 11.17/16.76/19.66 | 4 / 0 | 5.27 |
| `retention_archive_properties-part1` | 05:34:47 | 11.03/16.55/19.55 | 1 / 0 | 23.73 |
| `retention_archive_properties-part2` | 05:35:12 | 10.66/16.03/19.27 | 1 / 0 | 34.24 |
| `retention_nested_leases` | 05:35:47 | 13.80/16.17/19.19 | 27 / 0 | 42.23 |
| `retention_salvage_properties-part1` | 05:36:30 | 15.29/16.24/19.07 | 1 / 0 | 51.60 |
| `retention_salvage_properties-part2` | 05:37:22 | 17.32/16.69/19.06 | 1 / 0 | 56.29 |
| `retention_sweep_race-part1` | 05:38:19 | 15.44/16.24/18.75 | 12 / 0 | 24.00 |
| `retention_sweep_race-part2` | 05:38:44 | 15.89/16.28/18.69 | 1 / 0 | 128.13 |
| `retention_81_notes` | 05:40:53 | 13.80/15.10/17.86 | 38 / 0 | 70.14 |
| `turn_retention_cwd` | 05:42:07 | 13.57/14.91/17.56 | 6 / 0 | 6.98 |
| `turn_retention_cwd_properties` | 05:42:15 | 13.29/14.83/17.52 | 1 / 0 | 10.54 |
| `turn_retention_job_directory` | 05:42:26 | 13.20/14.75/17.46 | 2 / 0 | 5.03 |
| `turn_retention_job_properties` | 05:42:32 | 15.04/15.09/17.55 | 1 / 0 | 12.27 |
| `turn_retention_quarantine` | 05:42:45 | 14.79/15.04/17.50 | 10 / 0 | 9.65 |
| `task_holds` | 05:42:57 | 13.97/14.85/17.41 | 64 / 0 | 12.93 |
| `task_hold_admission` | 05:43:10 | 13.21/14.63/17.28 | 49 / 0 | 5.57 |

All 78 holds used the shared lock; no outside-lock fallback or harness retry was needed. The initial dependency-unit file’s outdated exact-list assertion failed once and passed after the documented oracle update; both runs remain in this ledger. Longest pytest: **225.25 seconds**; peak observed pytest RSS: **316.1 MiB**. The source hashes in all nine mutant records match the final module sources. The observer was extended after the initial runs to retain pre-deselection collection lists; the union verifies that every selected repository module node ran, except the one deliberate live-lsof exclusion. No property settings or property assertions were changed.

## Not run

Deliberately deselected: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`. No other requested local node remains unrun. The full suite, Python 3.12, external CI execution and `tests/frontend/test_core_live.py` were not run. The live frontend is person-only and cannot run on a lane. No current CI result or approving review is claimed.

## Delivery and cleanup

The ordered commits contain the fix with its five flipped cases, the nine named-mutant proofs, the dependency-list oracle update, then text and final execution evidence. Every commit ends with the prescribed co-author footer. Delivery is `retention-fix272r4.bundle` at the workspace root, requiring base `5b94fd32a815`; it is verified before private metadata is removed. The original shared Git metadata remains at the supplied detached base. All owned tool sessions finish before the final answer; both scratch roots are removed with the prescribed guarded cleanup. Combined scratch measured 559 MiB before cleanup, below 1.5 GB. The final answer supplies the bundle head.
