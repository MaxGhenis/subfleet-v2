# Retention dependency pins: fix round r10

**R10-F1 is repaired: every returned discovery place has one spelling, whether or not discovery is complete.** Canonical storage strings and resolved scan targets must meet both return equations. Physical link entries and successful file locations keep their existing treatment. The returned list is built before the existing alias check. Retention stays dormant pending approving review r11 and completed CI.

Started **2026-10-31 11:33:45 UTC** at **475f6327890e**. The first pytest started at 11:35:55 UTC, inside the first hour. The hold-start cutoff was 15:33:45 UTC. The macOS scratch path was denied; all scratch used `/private/tmp/sf-fix272r10`. Shared Git metadata refused `index.lock`, so the assigned branch `fix/retention-dependency-pins-217` is committed with worktree-local `.lane-git-r10` metadata. The verified delivery bundle is `fix272r10.bundle` in this worktree, with exactly `475f6327890e` as its prerequisite. No shared branch or history was rewritten.

## Decisions as built

1. One small `_spelled_location` predicate states `canonical(dirname(p)) + basename(p) == p` and `canonical(p) == p`. It filters `found`'s canonical strings in `git_storage` and `self.entries` in `locations`. It does not filter `links` or `files`; `scan` has no added guard.
2. `git_storage` builds both portions of its returned list, runs `aliases.check(incomplete)`, then returns. Two real-Git races retarget the scanned link itself or replace its resolved target with a cycle in `locations`, after resolution and after the old check position. Both now produce the existing alias-changed evidence. The named check-before-spelling mutant fails the resolved-target race's evidence assertion, demonstrating the silent-drop regression.
3. An unused cyclic scan reports complete evidence. The filter adds no evidence. On every stable route, the full incomplete list equals the input-head code on the identical filesystem, including order, file and reason. The unit tests pin each entry count and every file independently of errno wording. Every removed baseline string is separately checked with `os.stat` and raises `ELOOP`.
4. The new `tests/unit/test_retention_location_cycles.py` uses real Git for the hub's seven routes plus two-link-suffix scan and alternates. All nine have exact evidence and spelling checks. A healthy sibling repository keeps every previously recorded place, and the report controls compare it to input-head code on the same input with the cycle removed. Both new race variants are covered. Only review r10's strict decorator was removed; its bodies remain byte-identical. Neither spelling-property strategy nor the earlier location-input module changed.
5. All three r10 mutants and the four r9 and nine r8 mutants were run against their named killers, each in one hold. Results and assertion comparisons are below. No other mutation or generated-reversion sweep was run.
6. The discovery docstring and desktop discovery description explain the cost: an unspelled place inside a cycle is omitted because it cannot be read through; physical link entries and incomplete evidence stay. The unconditional invariant stands. Review r10's proposed temporary listed-difference line is retired, not inserted. The existing listed-differences section and entire acceptance contract, including C-8.3 and C-8.4, are unchanged. The main report has a Review r10 and fix round r10 section, and this report supplies PR-body replacements.

No implementation decision was widened or replaced. The hub's route table, rather than the scan-only trial, is the explicit reason both `found` and `entries` are filtered. The 64-hop walker, deeper-reader boundaries and later-change/ABA observations remain unchanged. No new product gap was found.

## Route table: input head and repair

Base is `475f6327890e`. Head columns describe the final repair behavior. The nine comparative controls also include the healthy sibling repository; that adds only healthy storage and no incomplete entries or unspelled strings. Full before/after records, identical incomplete lists, dropped unreadable paths and healthy-storage lists are in `route-comparisons.json`.

| Route | Cycle | Base fsck/read | Head fsck/read | Base/head incomplete | Base/head unspelled |
|---|---|---|---|---|---|
| control | self-suffix | 0/0 | 0/0 | 0/0 | 0/0 |
| scan | self-suffix | 0/0 | 0/0 | 0/0 | 1/0 |
| alternates | self-suffix | 0/0 | 0/0 | 2/2 | 1/0 |
| alternates-tail | self-suffix | 0/0 | 0/0 | 2/2 | 1/0 |
| gitfile | self-suffix | 128/128 | 128/128 | 4/4 | 2/0 |
| commondir | self-suffix | 128/128 | 128/128 | 3/3 | 2/0 |
| objects-alternates | self-suffix | 0/0 | 0/0 | 2/2 | 1/0 |
| scan | two-link-suffix | 0/0 | 0/0 | 0/0 | 1/0 |
| alternates | two-link-suffix | 0/0 | 0/0 | 2/2 | 1/0 |


Let `L64 = <base>/loop/` followed by 64 `tail` components; for two-link-suffix it is 32 `extra/tail` pairs. The exact error files are:

- Control and scan: none.
- Alternates and objects-link/alternates: `L64` and `L64/info/alternates`.
- Alternates-tail: `L64/x` and `L64/x/info/alternates`.
- Gitfile: `L64`, `<base>/loop/<128 tails>/objects`, `L64/objects/info/alternates`, `L64/commondir`, in that order.
- Commondir: the first three gitfile entries.

The two-link alternates row substitutes its paired suffix in the same two files. These are pinned in the new unit file; no filesystem error prose is pinned there.

## Five routine controls against 475f6327890e

Plain repository, worktree-add checkout, shared clone, nested git-new-workdir layout and no-Git folder each execute the snapshot of the input-head reader on the same real-Git fixture as the repair. All satisfy `baseline == repaired == expected`, including exact list order. `routine-comparisons.json` contains both full records. The unchanged 19-case dependency-unit file also passes under linked TMPDIR.

| Layout | Input-head places | Head places | Exact record equality |
|---|---:|---:|---|
| plain | 2 | 2 | Yes |
| worktree | 3 | 3 | Yes |
| shared | 3 | 3 | Yes |
| new-workdir | 3 | 3 | Yes |
| none | 0 | 0 | Yes |


## Red, green and generated spelling controls

Review r10's original file with `--runxfail`: **10 passed / 2 failed**, both expected invariant failures. The final new cycle module at input-head code: **10 passed / 10 failed**, eight non-control cyclic routes and both race-evidence assertions. An earlier 19-case draft is retained as red-cycles; the final module has 20 cases. At the repair: **12/12** and **20/20**, respectively. All seven green files and all ten linked-TMPDIR files pass. No mark or strategy was weakened.

- green-test_retention_location_spelling: 55 observations, 55 distinct inputs, 33 nonempty cells, {'needs-owner': 55}.
- linked-test_retention_location_spelling: 55 observations, 55 distinct inputs, 33 nonempty cells, {'needs-owner': 55}.

The existing deterministic property still uses 55 draws, no database and no explicit examples. Its unchanged strategy reaches all 33 position/spelling cells in both runs. Per-input observations remain in the receipts and the counted table is in `spelling-classes.json`.

## Requested holds and per-file counts

**761 distinct product-test nodes plus 23 report controls pass.** Repeated linked runs count once in the union. Every hold has one serial pytest, no xdist. The maximum measured pytest concurrency is 2; all 96 completed invocations held the shared lock. No invocation ran outside it. Longest invocation: 587.30 seconds; the runner interrupts at nine minutes. Peak pytest RSS: 174.6 MiB. The wrapper's sysctl load header is denied by the sandbox; complete load vectors are recorded with os.getloadavg inside the admitted pytest process. Per-file union counts and first passing starts/loads (first attempt for zero-pass files) follow; `hold-table.md` includes every repeated, red, environment-failing and mutant hold.

| File | Passing nodes | First passing start (first attempt if zero) (UTC) | Load 1/5/15 | Hold |
|---|---:|---|---|---|
| docs/reports/2026-10-31-retention-pins-fix-r10-data/probes.py | 23 | 2026-10-31T11:37:04.445566+00:00 | 62.48/53.36/49.37 | baseline-route-files |
| tests/fake/test_detached_retention_fence.py | 1 | 2026-10-31T15:08:53.425324+00:00 | 78.79/67.92/69.23 | admission-detached |
| tests/fake/test_retention_dependencies.py | 30 | 2026-10-31T12:21:50.209872+00:00 | 82.10/83.44/86.13 | linked-test_retention_dependencies |
| tests/fake/test_retention_dependency_incomplete.py | 19 | 2026-10-31T13:11:46.316143+00:00 | 119.61/129.76/118.13 | regressions-r1 |
| tests/fake/test_retention_dependency_review_r1.py | 18 | 2026-10-31T13:11:46.316143+00:00 | 119.61/129.76/118.13 | regressions-r1 |
| tests/fake/test_retention_dependency_review_r10.py | 12 | 2026-10-31T11:41:09.911727+00:00 | 68.40/58.63/52.51 | green-test_retention_dependency_review_r10 |
| tests/fake/test_retention_dependency_review_r2.py | 21 | 2026-10-31T12:59:46.170559+00:00 | 69.86/97.26/104.84 | regressions-r2 |
| tests/fake/test_retention_dependency_review_r3.py | 6 | 2026-10-31T12:57:04.336601+00:00 | 80.83/102.80/107.27 | regressions-r3 |
| tests/fake/test_retention_dependency_review_r4.py | 53 | 2026-10-31T12:43:00.305505+00:00 | 78.42/98.48/96.27 | regressions-r4-retarget |
| tests/fake/test_retention_dependency_review_r5.py | 60 | 2026-10-31T12:33:05.817963+00:00 | 91.80/90.74/88.12 | regressions-r5 |
| tests/fake/test_retention_dependency_review_r6.py | 16 | 2026-10-31T12:27:56.750492+00:00 | 114.15/92.56/88.37 | regressions-r7-r6 |
| tests/fake/test_retention_dependency_review_r7.py | 3 | 2026-10-31T12:27:56.750492+00:00 | 114.15/92.56/88.37 | regressions-r7-r6 |
| tests/fake/test_retention_dependency_review_r8.py | 2 | 2026-10-31T12:17:52.656980+00:00 | 100.09/90.12/88.66 | linked-test_retention_dependency_review_r8 |
| tests/fake/test_retention_dependency_review_r9.py | 4 | 2026-10-31T11:44:46.782228+00:00 | 80.94/68.54/57.96 | green-test_retention_dependency_review_r9 |
| tests/fake/test_retention_dependency_spelling.py | 16 | 2026-10-31T14:26:54.574271+00:00 | 78.95/72.45/70.31 | early-resume-dependencies-light-1 |
| tests/fake/test_retention_parent_hint.py | 2 | 2026-10-31T12:27:56.750492+00:00 | 114.15/92.56/88.37 | regressions-r7-r6 |
| tests/fake/test_retention_pointer_failures.py | 7 | 2026-10-31T12:27:56.750492+00:00 | 114.15/92.56/88.37 | regressions-r7-r6 |
| tests/fake/test_retention_recovery_census.py | 2 | 2026-10-31T13:30:40.542435+00:00 | 138.22/109.17/105.51 | aliases-census |
| tests/fake/test_state_contract.py | 37 | 2026-10-31T14:37:33.966129+00:00 | 51.75/52.20/59.28 | guards |
| tests/fake/test_turn_retention_cwd.py | 6 | 2026-10-31T14:28:02.798196+00:00 | 64.92/70.22/69.67 | test_turn_retention_cwd |
| tests/fake/test_turn_retention_cwd_properties.py | 1 | 2026-10-31T14:28:39.691539+00:00 | 57.22/67.76/68.80 | test_turn_retention_cwd_properties |
| tests/fake/test_turn_retention_job_directory.py | 2 | 2026-10-31T14:29:27.423875+00:00 | 49.29/63.98/67.34 | test_turn_retention_job_directory |
| tests/fake/test_turn_retention_job_properties.py | 1 | 2026-10-31T14:30:06.862361+00:00 | 50.22/62.18/66.51 | test_turn_retention_job_properties |
| tests/fake/test_turn_retention_quarantine.py | 10 | 2026-10-31T14:32:14.711791+00:00 | 42.58/55.72/63.40 | test_turn_retention_quarantine |
| tests/fake/test_writer_retention_fence.py | 1 | 2026-10-31T15:11:18.825534+00:00 | 81.13/75.26/72.11 | admission-writer |
| tests/unit/test_invariants_index.py | 25 | 2026-10-31T14:37:33.966129+00:00 | 51.75/52.20/59.28 | guards |
| tests/unit/test_retention_alternates.py | 23 | 2026-10-31T13:20:41.944386+00:00 | 108.15/101.38/107.74 | dependencies-light |
| tests/unit/test_retention_alternates_review_r2.py | 15 | 2026-10-31T13:08:37.650762+00:00 | 145.90/129.28/114.81 | regressions-alternates-r2 |
| tests/unit/test_retention_archive.py | 108 | 2026-10-31T14:40:19.239102+00:00 | 64.56/56.14/59.52 | archive-0 |
| tests/unit/test_retention_dependency_aliases.py | 10 | 2026-10-31T13:30:40.542435+00:00 | 138.22/109.17/105.51 | aliases-census |
| tests/unit/test_retention_dependency_properties.py | 4 | 2026-10-31T13:41:42.496172+00:00 | 86.47/84.02/93.07 | dependency-property-1 |
| tests/unit/test_retention_dependency_units.py | 19 | 2026-10-31T12:19:18.834262+00:00 | 95.23/91.46/89.33 | linked-test_retention_dependency_units |
| tests/unit/test_retention_folded_pin.py | 38 | 2026-10-31T13:20:41.944386+00:00 | 108.15/101.38/107.74 | dependencies-light |
| tests/unit/test_retention_job_dependencies.py | 12 | 2026-10-31T13:20:41.944386+00:00 | 108.15/101.38/107.74 | dependencies-light |
| tests/unit/test_retention_location_cycles.py | 20 | 2026-10-31T11:42:34.146672+00:00 | 68.35/60.55/53.79 | green-test_retention_location_cycles |
| tests/unit/test_retention_location_inputs.py | 1 | 2026-10-31T11:51:31.202077+00:00 | 53.09/61.98/58.32 | green-test_retention_location_inputs |
| tests/unit/test_retention_location_spelling.py | 11 | 2026-10-31T11:46:09.894092+00:00 | 38.72/58.74/55.28 | green-test_retention_location_spelling |
| tests/unit/test_retention_locations.py | 22 | 2026-10-31T11:49:54.947963+00:00 | 88.66/68.97/60.08 | green-test_retention_locations |
| tests/unit/test_retention_nested_leases.py | 27 | 2026-10-31T14:33:46.647325+00:00 | 57.82/56.85/63.00 | nested-leases |
| tests/unit/test_retention_pointer_inputs.py | 2 | 2026-10-31T11:53:56.282498+00:00 | 107.64/79.68/65.86 | green-test_retention_pointer_inputs |
| tests/unit/test_retention_pointer_whitespace.py | 9 | 2026-10-31T14:15:47.146972+00:00 | 57.21/60.75/69.39 | early-resume-regressions-r5-2 |
| tests/unit/test_retention_qos.py | 8 | 2026-10-31T15:17:14.105017+00:00 | 83.34/63.56/66.86 | extra-test_retention_qos |
| tests/unit/test_retention_regenerable_properties.py | 0 | 2026-10-31T15:19:08.212729+00:00 | 127.86/84.97/74.77 | extra-test_retention_regenerable_properties |
| tests/unit/test_retention_shared_folders.py | 61 | 2026-10-31T14:02:50.592936+00:00 | 58.18/85.50/87.55 | shared-examples |
| tests/unit/test_retention_worktrees.py | 15 | 2026-10-31T15:13:32.919641+00:00 | 64.60/73.01/71.82 | extra-test_retention_worktrees |
| tests/unit/test_test_module_names.py | 1 | 2026-10-31T14:37:33.966129+00:00 | 51.75/52.20/59.28 | guards |


Under `/private/tmp`, these 6 archive nodes fail because their source path is classified as scratch:

- `tests/unit/test_retention_archive.py::test_blob_only_in_a_local_branch_is_archived_not_omitted`
- `tests/unit/test_retention_archive.py::test_borrowed_or_shallow_object_store_is_scratch[alternates]`
- `tests/unit/test_retention_archive.py::test_borrowed_or_shallow_object_store_is_scratch[shallow]`
- `tests/unit/test_retention_archive.py::test_local_path_remote_is_not_a_remote`
- `tests/unit/test_retention_archive.py::test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`
- `tests/unit/test_retention_archive.py::test_survey_is_read_only_and_predicts_the_pass`

They pass unchanged with TMPDIR in this worktree's new `sf-fix272r10-fixtures` folder, removed at cleanup. The live archive case `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` was explicitly excluded from every archive selection. The three additional files are included in the run manifest; any incomplete selection is listed below.

## Bounded holds

- regressions-r5: nine-minute guard; 20 calls were initially unfinished; 0 still lack a completed passing call.
- dependencies-light: nine-minute guard; 24 calls were initially unfinished; 0 still lack a completed passing call.
- shared-property-1: nine-minute guard; 1 calls were initially unfinished; 1 still lack a completed passing call.
- shared-property-1-verbose: nine-minute guard; 1 calls were initially unfinished; 1 still lack a completed passing call.
- final-shared-property-1: nine-minute guard; 1 calls were initially unfinished; 1 still lack a completed passing call.
- extra-test_retention_regenerable_properties: nine-minute guard; 1 calls were initially unfinished; 1 still lack a completed passing call.
- resume-shared-property-1-0: nine-minute guard; 1 calls were initially unfinished; 1 still lack a completed passing call.
- resume-extra-test_retention_regenerable_properties-0: nine-minute guard; 1 calls were initially unfinished; 1 still lack a completed passing call.
- dependency-property-0: nine-minute guard; 1 calls were initially unfinished; 0 still lack a completed passing call.
- dependency-property-2: nine-minute guard; 1 calls were initially unfinished; 0 still lack a completed passing call.

The r4/r2 group is split into six sequential holds; the manifest is `split-holds.json`. All 92 planned holds ended. 2 selected properties remain incomplete after the nine-minute guards: `tests/unit/test_retention_regenerable_properties.py::test_only_and_all_regenerable_output_is_dropped_and_the_rest_restored`, `tests/unit/test_retention_shared_folders.py::test_early_fences_equal_reservation_before_and_after_quarantine`.

## Named mutants

Each mutant has one failing call assertion and passing setup/teardown. The runner records the original and mutated source hashes. The prior-assertion column compares against fix r9; r8 comparisons against fix r8 are also retained in `mutant-results.json`.

| Mutant | Named killer | Result | Same r9 assertion |
|---|---|---|---|
| r8-alias-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[admin-path] | Killed | Yes |
| r8-pointer-file-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[gitfile] | Killed | Yes |
| r8-intermediate-link-walk-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[intermediate-HEAD] | Killed | Yes |
| r8-lexical-dotdot | tests/unit/test_retention_locations.py::test_dotdot_applies_after_the_link_before_it | Killed | Yes |
| r8-link-location-followed | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[gitfile-target-HEAD] | Killed | Yes |
| r8-top-level-links-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[git-entry-HEAD] | Killed | Yes |
| r8-parent-hint-scanned | tests/unit/test_retention_locations.py::test_inferred_parent_is_not_scanned | Killed | Yes |
| r8-walk-revalidation-dropped | tests/unit/test_retention_locations.py::test_changed_link_chain_with_the_same_final_target_is_incomplete | Killed | Yes |
| r8-routine-skip-dropped | tests/unit/test_retention_locations.py::test_routine_records_are_exactly_unchanged[worktree] | Killed | Yes |
| r9-text-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[gitfile] | Killed | Yes |
| r9-alternates-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[alternates] | Killed | Yes |
| r9-scan-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[objects-pack] | Killed | Yes |
| r9-start-spelling-dropped | tests/unit/test_retention_location_spelling.py::test_routine_alias_keeps_the_exact_record[data-volume-worktree] | Killed | Yes |
| r10-found-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[alternates-self-suffix] | Killed | New |
| r10-entries-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[scan-self-suffix] | Killed | New |
| r10-check-before-spelling | tests/unit/test_retention_location_cycles.py::test_scan_retargeted_into_cycle_before_spelling_is_incomplete[resolved-target] | Killed | New |


The four r9 and nine r8 mutants all die; 13/13 match their previous r9 assertion. All nine r8 rows also match their r8 assertions (9/9). The exact expressions and independent comparisons are recorded without substituting a new killer.

## PR-body lines

**Fix round r10 repairs R10-F1: every returned discovery place has one spelling, including incomplete discoveries.** A shared predicate filters unspelled canonical storage strings and scanned targets at the return boundary. Physical link entries, successful file locations and existing incomplete evidence stay. Discovery builds its return list before alias revalidation, so a link or resolved target changed into a cycle produces the existing alias-changed evidence. The hub's seven-route table widened the scan-only trial to cover both return paths. Retention stays dormant pending approving review r11 and completed CI.

- **Evidence:** review r10 goes from 10 passed / 2 failed with `--runxfail` to 12 passed; the new 20-case cycle file goes from 10 passed / 10 failed to 20 passed. All nine route/cycle rows satisfy both equations; each stable route preserves its exact incomplete list. Every dropped baseline string raises `ELOOP`, and healthy sibling storage matches `475f6327890e` with the cycle removed. Five routine records equal that head. 761 distinct product-test nodes and 23 report controls pass; linked-TMPDIR holds pass. All 92 planned holds ended. 2 selected properties remain incomplete after the nine-minute guards: `tests/unit/test_retention_regenerable_properties.py::test_only_and_all_regenerable_output_is_dropped_and_the_rest_restored`, `tests/unit/test_retention_shared_folders.py::test_early_fences_equal_reservation_before_and_after_quarantine`. All 16 named mutants die; 13/13 prior mutants fail at their r9 assertion. Both spelling-strategy files and C-8.3/C-8.4 are unchanged.

**Next:** review r11 judges fix round r10. The hub completes all-pass CI and mergeability checks. Activation remains a separate step.


## Delivery and cleanup

The commits are ordered repair with the flipped mark, tests and focused evidence, named mutants, then text and remaining receipts. Every message ends with the required co-author trailer. The final head and verified bundle path are given in the delivery response. No fetch, merge or remote push of the working repository, GitHub call, real provider, real Subfleet CLI or live store was used. All Git repositories and Subfleet stores under test are isolated fixtures. Each started process was waited to completion; none detached. Scratch stayed below 1.5 GB and was removed with the prescribed guarded cleanup; `cleanup.json` records the final measurements and removals. The initial guarded removal left the archive interruption fixture's read-only directory. Owner permissions were restored only on that job-owned directory, and the guarded removal then completed. `cleanup-errors.json` retains the initial errors. Bytecode, Hypothesis storage and worktree fixture roots were removed.

Not run: the mandated live-lsof exclusion; the full suite, Python 3.12, person-only frontend live probe and remote CI; older mutants and generated-only reversions outside this round's named list. All 92 planned holds ended. 2 selected properties remain incomplete after the nine-minute guards: `tests/unit/test_retention_regenerable_properties.py::test_only_and_all_regenerable_output_is_dropped_and_the_rest_restored`, `tests/unit/test_retention_shared_folders.py::test_early_fences_equal_reservation_before_and_after_quarantine`.

## Holds and nodes not completed

| Hold not completed | Selection |
|---|---|

Nodes without a completed passing call:

- `tests/unit/test_retention_regenerable_properties.py::test_only_and_all_regenerable_output_is_dropped_and_the_rest_restored`
- `tests/unit/test_retention_shared_folders.py::test_early_fences_equal_reservation_before_and_after_quarantine`
