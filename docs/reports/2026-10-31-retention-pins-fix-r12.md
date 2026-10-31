# Retention dependency pins — fix round r12

R12-F1 is repaired by `e61c9e383`: `_unreadable_location` drops a failed spelling only on `ELOOP`. All other results keep the string `475f63278` kept. The four requested long-cycle rows expose a contradiction in decision 4's exact-baseline expectation; the executed counterexample and smallest test-only adjustment are documented below. No executable product decision is changed.

Started **2026-10-31 20:10:51 UTC**, input head **92a8635a05d6**. First hold wrapper admitted at **20:11:57 UTC**; the pytest evidence starts **20:12:07 UTC**. The hold-start cutoff is **23:10:51 UTC**, with a six-hour wall at **02:10:51 UTC November 1**.

The requested Darwin scratch mkdir was denied. All environments and fixtures use `/private/tmp/sf-fix272r12`; linked holds reach its fixture directory through `link-tmp`. Shared Git refused the branch ref lock. Four ordered commits are built using private metadata, and a verified bundle with `92a8635a0` as prerequisite is the delivery. The caller's checkout and original shared metadata are unchanged.

## Decisions 1–6 as built

1. The product predicate is exactly `return exc.errno == errno.ELOOP`. It runs only after an equation fails. Successful stat, `ENAMETOOLONG`, `ENOENT`, `ENOTDIR`, `EACCES` and every other error preserve the place. No other executable product code changes.
2. Review r12's walked-spelling trial is not applied. No second state map is added. `_target`, `check`, the component walker, `scan`, and list-before-check order remain unchanged.
3. D-R12-1 states the cost: a suffix-growing cycle whose canonical string exceeds `PATH_MAX` is kept unspelled, as at `475f63278`. Executed gitfile and commondir inputs at fixture bases of 371 and 500 bytes exercise real `ENAMETOOLONG`. Nothing reads through that cycle string; its only added effect is at most an extra pin until the job ends. The baseline records are **2 places / 4 incomplete** for gitfile and **3 / 3** for commondir. Each also contains one shorter ELOOP string (701 or 830 bytes), which decision 1 requires omitting. Head therefore records **1 / 4** and **2 / 3**, retaining the ENAMETOOLONG string and exact incomplete evidence. Decision 4's full equality is impossible under the stated invariant. The smallest adjustment is to expect the baseline record minus this shorter ELOOP string; product code remains unchanged. `decision-4-counterexample.json` preserves all four executed inputs.
4. Only the strict xfail decorator is removed from review r12. Its bodies, every earlier review file, both strategy modules and the cycle file are byte-identical. The readable module changes only the ENAMETOOLONG keep expectation, exact long-cycle record/count expectations with the documented decision-4 correction, and the differential's ELOOP-only assertion. Full ordered incomplete evidence is preserved.
5. The named mutant set is 19 definitions / 20 killer tests. The 16 r8/r9/r10 mutants die at byte-identical assertion expressions. `r11-stat-gate-dropped` also dies at the same expression. `r11-any-oserror-drops` dies in the same test at the same assertion site; the expression changes from the old two-error expectation to the required ELOOP-only expectation. The new r12 mutant dies at both named assertions. `r11-enametoolong-kept` is retired because its behavior is now the product.
6. The discovery docstring and desktop paragraph scope one spelling to a filesystem that holds still and strings the kernel can walk. ELOOP identifies a live cycle; ENAMETOOLONG rejects only one spelling. D-R12-1 is added, C-8.3/C-8.4 and the whole acceptance contract stay byte-identical, and the main report retains the entire dated r11 section. Review r12 stopped on a provider content filter with its work kept. Its report is partial and supplies no completed verdict; its opening provisional finding is not an approving review. The PR text asks for review r13.

## R12 routes and red/green

The unchanged review-r12 bodies run at `92a8635a0` with `--runxfail`: **2 passed / 2 failed**, both product failures at the owner-preservation assertion. After repair: **4 passed** normally and **4 passed** under linked TMPDIR. Both routes' fsck and pre-retention Git reads return 0. Neither route adds incomplete evidence. The baseline reader is `475f63278` throughout.

| Input / reader | Route | Places / incomplete | Read before / after | Owner retired |
|---|---|---:|---:|---|
| 92a8635a0 / baseline | alternates | 3 / 0 | 0 / 0 | no |
| 92a8635a0 / baseline | scan | 3 / 0 | 0 / 0 | no |
| 92a8635a0 / product | alternates | 2 / 0 | 0 / 128 | yes |
| 92a8635a0 / product | scan | 2 / 0 | 0 / 128 | yes |
| r12 repair / baseline | alternates | 3 / 0 | 0 / 0 | no |
| r12 repair / baseline | scan | 3 / 0 | 0 / 0 | no |
| r12 repair / product | alternates | 3 / 0 | 0 / 0 | no |
| r12 repair / product | scan | 3 / 0 | 0 / 0 | no |

Raw accepted paths are shorter than 1024 bytes while canonical absolute spellings exceed 1024 bytes; the real stale-string stat returns `ENAMETOOLONG`. Full paths, byte counts, records and retention results are retained in `r12-routes.json` and the hold receipts.

## Differential and routine controls

**496 same-input comparisons** across normal and linked runs. Every head git place is in the baseline set; each omitted string's real stat raises ELOOP and only ELOOP. The exact ordered incomplete lists match `475f63278` and the saved r10 reader. Dropped errno observations: **{'62': 28}**. The count includes repeated filesystem comparisons and generated inputs; it is not a count of unique spellings. Strategy bodies and settings remain unchanged.

| Routine layout | Normal | Linked TMPDIR | Record equals 475f63278 |
|---|---|---|---|
| plain | pass | pass | yes |
| worktree | pass | pass | yes |
| shared | pass | pass | yes |
| new-workdir | pass | pass | yes |
| none | pass | pass | yes |

The table's five full records equal `475f63278` in both environments. The two existing strategy modules are also instrumented for same-filesystem baseline/head comparisons without editing them. The 371/500-byte real-cycle records (including the initial expected-count failures) and the stat outcome table are retained in `fixed-rows.json`; only ELOOP drops. ENOENT/ENOTDIR/EACCES/ENAMETOOLONG use controlled equation failures because ordinary canonicalization preserves those unresolved strings; stat remains real. Success is a real case alias and ELOOP a real suffix cycle.

## Green results and scope

397 distinct ordinary nodes pass across 24 files. There are 610 passed call reports across normal, linked and repeated checks; the four initial decision-4 expectation failures are corrected. Every completed hold is locked: **True**. Longest hold: **241.04 seconds**; largest pytest process RSS: **132.98 MiB**. No hold exceeds ten minutes. Each hold runs one serial pytest, within the four-worker ceiling. The harness retains queue receipts, actual pytest starts, loads, per-phase outcomes and observations.

The run scope is exactly the ten green files, those ten plus r8/dependency-units/fake-dependencies through linked TMPDIR, the ten files under brief-fix-r8's **The dependency set**, and the three lane-rule guards. No new adversarial input or worst-case search is added.

Uncompleted planned holds: none. Missing or unfinished collected ordinary nodes: none. The four original long-cycle count expectations failed, then all four passed after the documented test-only correction. One cycle hold was paused while that counterexample was recorded and later rerun to completion.

Raw wrapper logs are retained byte for byte. Their blank `load ` lines contain trailing spaces after the sandbox rejects `sysctl`; the JSON receipts retain actual `os.getloadavg` values. The whitespace check covers source, prose and structured evidence, excluding raw `.log` files.

The R-files, turns, admission/archive holds and r10's extra files are outside this round's narrowed run list; they were not run. In particular, no archive path-trap rows or live-lsof row were run. The full suite and live frontend remain for CI; `tests/frontend/test_core_live.py` cannot run on a lane. No real CLI, provider, live store, fetch, merge, push, GitHub call or remote write is used.

## Per-file counts

| File | Unique passed nodes |
|---|---:|
| tests/fake/test_retention_dependencies.py | 30 |
| tests/fake/test_retention_dependency_review_r10.py | 12 |
| tests/fake/test_retention_dependency_review_r11.py | 4 |
| tests/fake/test_retention_dependency_review_r12.py | 4 |
| tests/fake/test_retention_dependency_review_r8.py | 2 |
| tests/fake/test_retention_dependency_review_r9.py | 4 |
| tests/fake/test_retention_dependency_spelling.py | 16 |
| tests/fake/test_retention_recovery_census.py | 2 |
| tests/fake/test_state_contract.py | 37 |
| tests/unit/test_invariants_index.py | 25 |
| tests/unit/test_retention_alternates.py | 23 |
| tests/unit/test_retention_dependency_aliases.py | 10 |
| tests/unit/test_retention_dependency_properties.py | 4 |
| tests/unit/test_retention_dependency_units.py | 19 |
| tests/unit/test_retention_folded_pin.py | 38 |
| tests/unit/test_retention_job_dependencies.py | 12 |
| tests/unit/test_retention_location_cycles.py | 20 |
| tests/unit/test_retention_location_inputs.py | 1 |
| tests/unit/test_retention_location_readable.py | 36 |
| tests/unit/test_retention_location_spelling.py | 11 |
| tests/unit/test_retention_locations.py | 22 |
| tests/unit/test_retention_pointer_inputs.py | 2 |
| tests/unit/test_retention_shared_folders.py | 62 |
| tests/unit/test_test_module_names.py | 1 |

## Every hold, start time and load

Each ordinary hold selects one file, so its receipt gives that file's count, UTC start and load. Mutant receipts select only named killers. A hold interrupted by its nine-minute guard is never counted as a pass.

| Hold / file selection | Pytest start UTC | Load 1/5/15 | Passed / failed / skipped | Seconds | Exit |
|---|---|---|---:|---:|---:|
| [red-review-r12](2026-10-31-retention-pins-fix-r12-data/red-review-r12.json) | 2026-10-31T20:12:07.256335+00:00 | 80.79/83.11/83.31 | 2/2/0 | 39.40 | 1 |
| [green-test_retention_dependency_review_r12](2026-10-31-retention-pins-fix-r12-data/green-test_retention_dependency_review_r12.json) | 2026-10-31T20:13:30.657551+00:00 | 74.53/80.80/82.43 | 4/0/0 | 31.98 | 0 |
| [green-test_retention_dependency_review_r11](2026-10-31-retention-pins-fix-r12-data/green-test_retention_dependency_review_r11.json) | 2026-10-31T20:14:10.274327+00:00 | 75.82/80.51/82.26 | 4/0/0 | 40.18 | 0 |
| [green-test_retention_dependency_review_r10](2026-10-31-retention-pins-fix-r12-data/green-test_retention_dependency_review_r10.json) | 2026-10-31T20:14:59.015762+00:00 | 86.73/82.79/83.00 | 12/0/0 | 55.21 | 0 |
| [green-test_retention_dependency_review_r9](2026-10-31-retention-pins-fix-r12-data/green-test_retention_dependency_review_r9.json) | 2026-10-31T20:16:02.863537+00:00 | 83.94/82.39/82.81 | 4/0/0 | 35.77 | 0 |
| [green-readable-initial](2026-10-31-retention-pins-fix-r12-data/green-readable-initial.json) | 2026-10-31T20:16:44.812844+00:00 | 83.49/82.46/82.81 | 32/4/0 | 200.89 | 1 |
| [green-test_retention_location_cycles-interrupted](2026-10-31-retention-pins-fix-r12-data/green-test_retention_location_cycles-interrupted.json) | 2026-10-31T20:20:15.834895+00:00 | 69.10/77.48/80.72 | 12/0/0 | ≤44.89 | 2 |
| [green-readable](2026-10-31-retention-pins-fix-r12-data/green-readable.json) | 2026-10-31T20:22:17.192316+00:00 | 74.58/77.69/80.42 | 36/0/0 | 168.13 | 0 |
| [green-corrected-long-cycle](2026-10-31-retention-pins-fix-r12-data/green-corrected-long-cycle.json) | 2026-10-31T20:22:28.655825+00:00 | 73.45/77.34/80.26 | 4/0/0 | 66.47 | 0 |
| [mutant-r8-alias-location-recording-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r8-alias-location-recording-dropped.json) | 2026-10-31T20:24:00.597464+00:00 | 76.62/77.51/80.04 | 0/1/0 | 20.57 | 1 |
| [mutant-r8-pointer-file-location-recording-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r8-pointer-file-location-recording-dropped.json) | 2026-10-31T20:24:25.851686+00:00 | 74.28/76.84/79.72 | 0/1/0 | 17.95 | 1 |
| [mutant-r8-intermediate-link-walk-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r8-intermediate-link-walk-dropped.json) | 2026-10-31T20:24:47.900505+00:00 | 76.10/77.05/79.71 | 0/1/0 | 13.14 | 1 |
| [mutant-r8-lexical-dotdot](2026-10-31-retention-pins-fix-r12-data/mutant-r8-lexical-dotdot.json) | 2026-10-31T20:25:06.760386+00:00 | 75.29/76.79/79.57 | 0/1/0 | 13.06 | 1 |
| [green-test_retention_location_cycles](2026-10-31-retention-pins-fix-r12-data/green-test_retention_location_cycles.json) | 2026-10-31T20:25:10.025395+00:00 | 74.62/76.63/79.49 | 20/0/0 | 38.50 | 0 |
| [mutant-r8-link-location-followed](2026-10-31-retention-pins-fix-r12-data/mutant-r8-link-location-followed.json) | 2026-10-31T20:25:26.074509+00:00 | 73.73/76.34/79.34 | 0/1/0 | 16.60 | 1 |
| [mutant-r8-top-level-links-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r8-top-level-links-dropped.json) | 2026-10-31T20:25:48.352931+00:00 | 74.87/76.36/79.26 | 0/1/0 | 12.47 | 1 |
| [green-test_retention_location_spelling](2026-10-31-retention-pins-fix-r12-data/green-test_retention_location_spelling.json) | 2026-10-31T20:25:53.798580+00:00 | 74.64/76.29/79.22 | 11/0/0 | 50.62 | 0 |
| [mutant-r8-parent-hint-scanned](2026-10-31-retention-pins-fix-r12-data/mutant-r8-parent-hint-scanned.json) | 2026-10-31T20:26:04.239412+00:00 | 71.36/75.54/78.91 | 0/1/0 | 9.62 | 1 |
| [mutant-r8-walk-revalidation-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r8-walk-revalidation-dropped.json) | 2026-10-31T20:26:19.621751+00:00 | 72.32/75.52/78.85 | 0/1/0 | 16.74 | 1 |
| [mutant-r8-routine-skip-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r8-routine-skip-dropped.json) | 2026-10-31T20:26:42.587177+00:00 | 77.84/76.57/79.13 | 0/1/0 | 20.42 | 1 |
| [green-test_retention_locations](2026-10-31-retention-pins-fix-r12-data/green-test_retention_locations.json) | 2026-10-31T20:26:52.728532+00:00 | 77.83/76.61/79.11 | 22/0/0 | 37.15 | 0 |
| [mutant-r9-text-canonicalization-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r9-text-canonicalization-dropped.json) | 2026-10-31T20:27:11.805502+00:00 | 76.81/76.45/79.01 | 0/1/0 | 60.86 | 1 |
| [green-test_retention_location_inputs](2026-10-31-retention-pins-fix-r12-data/green-test_retention_location_inputs.json) | 2026-10-31T20:27:44.223408+00:00 | 69.33/74.97/78.39 | 1/0/0 | 127.47 | 0 |
| [mutant-r9-alternates-canonicalization-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r9-alternates-canonicalization-dropped.json) | 2026-10-31T20:28:31.071790+00:00 | 76.75/75.98/78.57 | 0/1/0 | 48.85 | 1 |
| [mutant-r9-scan-canonicalization-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r9-scan-canonicalization-dropped.json) | 2026-10-31T20:29:49.730857+00:00 | 92.46/80.12/79.86 | 0/1/0 | 28.11 | 1 |
| [green-test_retention_pointer_inputs](2026-10-31-retention-pins-fix-r12-data/green-test_retention_pointer_inputs.json) | 2026-10-31T20:29:59.524595+00:00 | 92.17/80.49/80.00 | 2/0/0 | 76.35 | 0 |
| [mutant-r9-start-spelling-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r9-start-spelling-dropped.json) | 2026-10-31T20:30:23.415389+00:00 | 85.14/79.76/79.75 | 0/1/0 | 15.65 | 1 |
| [mutant-r10-found-filter-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r10-found-filter-dropped.json) | 2026-10-31T20:30:44.342677+00:00 | 82.13/79.38/79.61 | 0/1/0 | 14.62 | 1 |
| [mutant-r10-entries-filter-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r10-entries-filter-dropped.json) | 2026-10-31T20:31:04.029211+00:00 | 77.02/78.41/79.26 | 0/1/0 | 18.30 | 1 |
| [linked-test_retention_dependency_review_r12](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_dependency_review_r12.json) | 2026-10-31T20:31:20.381430+00:00 | 77.17/78.35/79.22 | 4/0/0 | 21.11 | 0 |
| [mutant-r10-check-before-spelling](2026-10-31-retention-pins-fix-r12-data/mutant-r10-check-before-spelling.json) | 2026-10-31T20:31:26.016622+00:00 | 76.44/78.17/79.15 | 0/1/0 | 19.02 | 1 |
| [linked-test_retention_dependency_review_r11](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_dependency_review_r11.json) | 2026-10-31T20:31:48.450531+00:00 | 75.60/77.86/79.01 | 4/0/0 | 20.89 | 0 |
| [mutant-r11-stat-gate-dropped](2026-10-31-retention-pins-fix-r12-data/mutant-r11-stat-gate-dropped.json) | 2026-10-31T20:31:50.830234+00:00 | 75.60/77.86/79.01 | 0/1/0 | 20.88 | 1 |
| [linked-test_retention_dependency_review_r10](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_dependency_review_r10.json) | 2026-10-31T20:32:14.861110+00:00 | 71.16/76.61/78.52 | 12/0/0 | 32.60 | 0 |
| [mutant-r11-any-oserror-drops](2026-10-31-retention-pins-fix-r12-data/mutant-r11-any-oserror-drops.json) | 2026-10-31T20:32:15.156070+00:00 | 71.16/76.61/78.52 | 0/1/0 | 17.77 | 1 |
| [mutant-r12-enametoolong-drops](2026-10-31-retention-pins-fix-r12-data/mutant-r12-enametoolong-drops.json) | 2026-10-31T20:32:38.902744+00:00 | 70.54/76.16/78.31 | 0/2/0 | 24.82 | 1 |
| [linked-test_retention_dependency_review_r9](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_dependency_review_r9.json) | 2026-10-31T20:32:54.237444+00:00 | 72.54/76.33/78.33 | 4/0/0 | 20.72 | 0 |
| [linked-readable](2026-10-31-retention-pins-fix-r12-data/linked-readable.json) | 2026-10-31T20:33:19.858607+00:00 | 71.68/75.78/78.08 | 36/0/0 | 113.59 | 0 |
| [linked-test_retention_location_cycles](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_location_cycles.json) | 2026-10-31T20:35:18.599624+00:00 | 61.47/71.00/75.90 | 20/0/0 | 24.69 | 0 |
| [linked-test_retention_location_spelling](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_location_spelling.json) | 2026-10-31T20:35:46.979005+00:00 | 61.91/70.28/75.50 | 11/0/0 | 45.58 | 0 |
| [shared-examples](2026-10-31-retention-pins-fix-r12-data/shared-examples.json) | 2026-10-31T20:36:01.192842+00:00 | 61.78/69.88/75.27 | 58/0/0 | 140.23 | 0 |
| [linked-test_retention_locations](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_locations.json) | 2026-10-31T20:36:36.487001+00:00 | 61.48/68.78/74.64 | 22/0/0 | 14.72 | 0 |
| [linked-test_retention_location_inputs](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_location_inputs.json) | 2026-10-31T20:36:56.616763+00:00 | 62.06/68.46/74.39 | 1/0/0 | 35.40 | 0 |
| [linked-test_retention_pointer_inputs](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_pointer_inputs.json) | 2026-10-31T20:37:34.837808+00:00 | 55.73/66.03/73.22 | 2/0/0 | 67.28 | 0 |
| [shared-property-0](2026-10-31-retention-pins-fix-r12-data/shared-property-0.json) | 2026-10-31T20:38:24.725147+00:00 | 54.44/64.21/72.15 | 1/0/0 | 14.15 | 0 |
| [shared-property-1](2026-10-31-retention-pins-fix-r12-data/shared-property-1.json) | 2026-10-31T20:38:41.614958+00:00 | 54.51/63.74/71.84 | 1/0/0 | 231.16 | 0 |
| [linked-test_retention_dependency_review_r8](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_dependency_review_r8.json) | 2026-10-31T20:38:44.328491+00:00 | 54.31/63.55/71.73 | 2/0/0 | 9.57 | 0 |
| [linked-test_retention_dependency_units](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_dependency_units.json) | 2026-10-31T20:38:59.426068+00:00 | 56.09/63.47/71.56 | 19/0/0 | 39.95 | 0 |
| [linked-test_retention_dependencies](2026-10-31-retention-pins-fix-r12-data/linked-test_retention_dependencies.json) | 2026-10-31T20:39:41.562688+00:00 | 55.15/62.32/70.76 | 30/0/0 | 61.18 | 0 |
| [dependency-test_retention_alternates](2026-10-31-retention-pins-fix-r12-data/dependency-test_retention_alternates.json) | 2026-10-31T20:40:44.634058+00:00 | 57.05/61.60/69.87 | 23/0/0 | 57.74 | 0 |
| [dependency-test_retention_dependency_aliases](2026-10-31-retention-pins-fix-r12-data/dependency-test_retention_dependency_aliases.json) | 2026-10-31T20:41:46.754763+00:00 | 56.37/60.62/68.94 | 10/0/0 | 13.32 | 0 |
| [dependency-test_retention_job_dependencies](2026-10-31-retention-pins-fix-r12-data/dependency-test_retention_job_dependencies.json) | 2026-10-31T20:42:05.261505+00:00 | 54.10/59.85/68.47 | 12/0/0 | 27.78 | 0 |
| [dependency-test_retention_dependencies](2026-10-31-retention-pins-fix-r12-data/dependency-test_retention_dependencies.json) | 2026-10-31T20:42:38.004615+00:00 | 56.41/59.53/67.99 | 30/0/0 | 90.09 | 0 |
| [shared-property-2](2026-10-31-retention-pins-fix-r12-data/shared-property-2.json) | 2026-10-31T20:42:38.099700+00:00 | 56.41/59.53/67.99 | 1/0/0 | 121.84 | 0 |
| [dependency-test_retention_dependency_spelling](2026-10-31-retention-pins-fix-r12-data/dependency-test_retention_dependency_spelling.json) | 2026-10-31T20:44:14.641753+00:00 | 54.32/58.17/66.59 | 16/0/0 | 27.37 | 0 |
| [shared-property-3](2026-10-31-retention-pins-fix-r12-data/shared-property-3.json) | 2026-10-31T20:44:43.552794+00:00 | 50.17/56.82/65.81 | 1/0/0 | 26.64 | 0 |
| [dependency-test_retention_dependency_units](2026-10-31-retention-pins-fix-r12-data/dependency-test_retention_dependency_units.json) | 2026-10-31T20:44:45.552654+00:00 | 50.17/56.82/65.81 | 19/0/0 | 47.14 | 0 |
| [dependency-test_retention_folded_pin](2026-10-31-retention-pins-fix-r12-data/dependency-test_retention_folded_pin.json) | 2026-10-31T20:45:35.694624+00:00 | 56.89/57.33/65.47 | 38/0/0 | 140.33 | 0 |
| [dependency-property-2](2026-10-31-retention-pins-fix-r12-data/dependency-property-2.json) | 2026-10-31T20:46:26.802303+00:00 | 52.50/56.22/64.60 | 1/0/0 | 241.04 | 0 |
| [dependency-property-0](2026-10-31-retention-pins-fix-r12-data/dependency-property-0.json) | 2026-10-31T20:48:01.497792+00:00 | 46.63/53.01/62.46 | 1/0/0 | 162.34 | 0 |
| [dependency-property-1](2026-10-31-retention-pins-fix-r12-data/dependency-property-1.json) | 2026-10-31T20:50:48.835934+00:00 | 63.03/55.41/61.57 | 1/0/0 | 155.42 | 0 |
| [dependency-property-3](2026-10-31-retention-pins-fix-r12-data/dependency-property-3.json) | 2026-10-31T20:51:21.968409+00:00 | 58.09/54.99/61.21 | 1/0/0 | 113.21 | 0 |
| [dependency-test_retention_recovery_census](2026-10-31-retention-pins-fix-r12-data/dependency-test_retention_recovery_census.json) | 2026-10-31T20:53:32.205865+00:00 | 46.26/52.23/59.28 | 2/0/0 | 13.61 | 0 |
| [guard-test_state_contract](2026-10-31-retention-pins-fix-r12-data/guard-test_state_contract.json) | 2026-10-31T20:53:50.173064+00:00 | 44.49/51.40/58.82 | 37/0/0 | 59.28 | 0 |
| [guard-test_test_module_names](2026-10-31-retention-pins-fix-r12-data/guard-test_test_module_names.json) | 2026-10-31T20:54:55.502404+00:00 | 44.32/50.15/57.81 | 1/0/0 | 13.15 | 0 |
| [guard-test_invariants_index](2026-10-31-retention-pins-fix-r12-data/guard-test_invariants_index.json) | 2026-10-31T20:55:11.122848+00:00 | 45.96/50.25/57.71 | 25/0/0 | 6.10 | 0 |

## Mutants

The 16 r8/r9/r10 mutants die at byte-identical assertion expressions. `r11-stat-gate-dropped` also dies at the same expression. `r11-any-oserror-drops` dies in the same test at the same assertion site; the expression changes from the old two-error expectation to the required ELOOP-only expectation. The new r12 mutant dies at both named assertions. `r11-enametoolong-kept` is retired because its behavior is now the product.

| Mutant | Named killer | Result | Same assertion at r11 / r10 / r9 / r8 |
|---|---|---|---|
| r8-alias-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[admin-path] | killed | yes/yes/yes/yes |
| r8-pointer-file-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[gitfile] | killed | yes/yes/yes/yes |
| r8-intermediate-link-walk-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[intermediate-HEAD] | killed | yes/yes/yes/yes |
| r8-lexical-dotdot | tests/unit/test_retention_locations.py::test_dotdot_applies_after_the_link_before_it | killed | yes/yes/yes/yes |
| r8-link-location-followed | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[gitfile-target-HEAD] | killed | yes/yes/yes/yes |
| r8-top-level-links-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[git-entry-HEAD] | killed | yes/yes/yes/yes |
| r8-parent-hint-scanned | tests/unit/test_retention_locations.py::test_inferred_parent_is_not_scanned | killed | yes/yes/yes/yes |
| r8-walk-revalidation-dropped | tests/unit/test_retention_locations.py::test_changed_link_chain_with_the_same_final_target_is_incomplete | killed | yes/yes/yes/yes |
| r8-routine-skip-dropped | tests/unit/test_retention_locations.py::test_routine_records_are_exactly_unchanged[worktree] | killed | yes/yes/yes/yes |
| r9-text-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[gitfile] | killed | yes/yes/yes |
| r9-alternates-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[alternates] | killed | yes/yes/yes |
| r9-scan-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[objects-pack] | killed | yes/yes/yes |
| r9-start-spelling-dropped | tests/unit/test_retention_location_spelling.py::test_routine_alias_keeps_the_exact_record[data-volume-worktree] | killed | yes/yes/yes |
| r10-found-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[alternates-self-suffix] | killed | yes/yes |
| r10-entries-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[scan-self-suffix] | killed | yes/yes |
| r10-check-before-spelling | tests/unit/test_retention_location_cycles.py::test_scan_retargeted_into_cycle_before_spelling_is_incomplete[resolved-target] | killed | yes/yes |
| r11-stat-gate-dropped | tests/fake/test_retention_dependency_review_r11.py::test_return_filter_keeps_readable_store_across_case_rename[alternates] | killed | yes |
| r11-any-oserror-drops | tests/unit/test_retention_location_readable.py::test_filter_stat_table[found-ENOENT] | killed | NO |
| r12-enametoolong-drops | tests/fake/test_retention_dependency_review_r12.py::test_filter_keeps_readable_normalization_length[alternates]; tests/unit/test_retention_location_readable.py::test_filter_stat_table[found-ENAMETOOLONG] | killed | new |

## PR-body replacements

**Fix round r12 (repair `e61c9e383`) repairs R12-F1 without widening the reader.** Discovery omits a place only when a spelling equation fails and `os.stat` raises `ELOOP`. `ENAMETOOLONG` rejects one spelling and keeps the string `475f63278` kept. Both normalization-length routes again record three places, preserve the owner and read 0. `_target`, `check`, the walker, list-before-check order and evidence are unchanged. Review r12 stopped on a provider content filter with its work kept; its report is partial.

- **Fix r12 evidence:** review r12 changes from 2 passed / 2 failed at `92a8635a0` to 4 passed normally and 4 under linked TMPDIR. 496 same-input baseline/head comparisons preserve full ordered incomplete evidence and omit only real ELOOP strings; all five routine records equal `475f63278`. D-R12-1 documents the retained long cycle. The four 371/500-byte cycle rows also contain a shorter ELOOP string, so decision 4's exact-baseline counts contradict decision 1: the smallest test-only correction expects 1/4 and 2/3, versus baseline 2/4 and 3/3, with the ENAMETOOLONG string and all incomplete evidence retained. All 19 named mutants die at 20 named assertions; the 16 older expressions match. 397 distinct ordinary nodes across 24 files pass. The report records per-file counts, UTC starts, loads, the counterexample and every hold. C-8.3/C-8.4 and the dated r11 results remain unchanged.

- **Next:** review r13 and completed CI. Retention remains dormant pending approval.


## Delivery and cleanup

The ordered commits are repair with the flipped mark, tests and ordinary evidence, named mutants, then text and final evidence. Every commit ends with the required co-author trailer. The delivery is `retention-pins-fix-r12.bundle` in the worktree, with `92a8635a0` as prerequisite; verify it before handoff. Cleanup uses the lane rules' guarded deletion and records disk allocation, removal and artifact checks in `cleanup.json`. All test and mutant processes finish before cleanup and delivery.

Scratch allocation before cleanup: 271593472 bytes. Owned scratch roots were removed; bytecode, Hypothesis, build and kept-failure artifacts are absent. Private Git metadata is removed after bundle verification.
