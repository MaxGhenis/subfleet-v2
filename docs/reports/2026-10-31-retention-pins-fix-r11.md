# Retention dependency pins — fix round r11

**R11-F1 repaired.** The product repair is `49dbf61c6`; tests first committed at `0ab48ff5f`. The filter now asks whether the kernel refuses a failed spelling, preserving the old reader's string for every other outcome. No hub decision required a product deviation and no new reader gap was found in the executed inputs.

Started **2026-10-31 18:02:38 UTC**, input head **ced02912bea6f7ada1626f817bb847a90ae90470**. First admitted pytest started **2026-10-31 18:03:52 UTC**. Hold-start cutoff **22:02:38 UTC**; hard job wall **00:02:38 UTC November 1**.

## Decisions 1–6 as built

1. `_spelled_location` retains the two equations. `_unreadable_location` returns true only if the equations fail and `os.stat` raises `ELOOP` or `ENAMETOOLONG`. Both return filters use it. Successful stat and every other error keep the supplied string.
2. The hub's narrow trial is applied exactly. No canonical observation is added. `_target`, `check`, `resolve`, `scan`, `text` and the component walker are unchanged; the list is still built before `check`.
3. Full ordered incomplete lists match both `475f63278` and `ced02912b` (the same reader as `546b63ea1`) on the compared stable inputs. The original readable rename inputs match the older reader: three places and no incomplete entry. No false global evidence is introduced.
4. The new module exercises real Git/files, both return filters, six real stat outcomes, the existing unmodified spelling strategies, long cycles and two further readable rename forms. Only the strict r11 decorator is removed; its bodies are byte-identical. The initial Unicode fixture accidentally added an independently pinning symlink and expected three rather than four baseline places. It was corrected to rename a Unicode source-directory component with no added link, and replayed red before repair. This is a test-fixture correction, not a change to the hub's decisions.
5. All three new, three r10, four r9 and nine r8 named mutants are run against named killers. The table below records whether prior assertions match.
6. The discovery docstring and desktop paragraph scope one spelling to a filesystem that holds still, and explain the stat gate. `_target` and `check` retain their existing observation bounds. The whole acceptance contract is byte-identical; C-8.3/C-8.4 are unchanged. R11-F1 adds no listed difference, and all existing listed differences stay. The main report's dated r10 section is kept verbatim and a new r11 section corrects its overgeneralization.

## Original review table, before and after

| Route | Reader | Places | Incomplete | fsck before / renamed | Read before / renamed / retention | Owner pruned |
|---|---|---:|---:|---|---|---|
| alternates | 475f63278 | 3 | 0 | 0/0 | 0/0/0 | no |
| scan | 475f63278 | 3 | 0 | 0/0 | 0/0/0 | no |
| alternates | ced02912b | 2 | 0 | 0/0 | 0/0/128 | yes |
| scan | ced02912b | 2 | 0 | 0/0 | 0/0/128 | yes |
| alternates | head | 3 | 0 | 0/0 | 0/0/0 | no |
| scan | head | 3 | 0 | 0/0 | 0/0/0 | no |

The original red run uses `--runxfail` at `ced02912b`: **2 passed / 2 failed**, both at owner retention. Green is **4 passed**, also **4 passed** under linked `TMPDIR`. Baseline and head fsck/read setup succeeds. No incomplete evidence appears in either original input.

## Red and green for the new module

The initial new-file red is **24 passed / 10 failed**, with the Unicode fixture correction described above. The corrected rename-only red has **2 failed / 32 deselected**, each losing the third place at the old filter. The final new module replayed against the exact `ced02912b` reader snapshot gives **24 passed / 12 failed**: eight keep-table failures, both further readable rename failures and both original same-input record comparisons. Its receipt records the compiled snapshot hash. The final source is **36 passed** normally and **36 passed** with `TMPDIR` through a symlink (34 in each initial full-file hold plus two added same-input comparisons in focused holds). The requested first nine-file green set covers **112 passing nodes**, including those two additions.

## Differential and five routine controls

There are **496 same-filesystem comparisons** in the completed normal/linked green runs: **256** from the new module (128 each: nine cycle rows, five routine controls, 55 location recipes, 55 spelling recipes and four long-cycle rows), and **240** from the two unchanged strategy modules (120 each environment, including their focused controls). Repeated parameter recipes use fresh filesystem inputs. Each comparison asserts head places are a subset of baseline, every missing string independently raises `ELOOP` or `ENAMETOOLONG` from real stat, and the full ordered incomplete list equals both older readers entry for entry. The stable strategy records are wholly equal. No unsupported-volume skip occurred. Eighteen additional healthy-sibling checks compare the identical stable repository after removing the cycle; both further rename forms compare complete old/head records on a restored identical input. Four additional original-route comparisons (two normal, two linked) restore the exact same r11 fixture and replay the same race against baseline, asserting full record equality before the original owner/read check.

| Routine control | 475f63278 places | Head places | Full record |
|---|---:|---:|---|
| plain | 2 | 2 | equal |
| worktree | 3 | 3 | equal |
| shared | 3 | 3 | equal |
| new-workdir | 3 | 3 | equal |
| none | 0 | 0 | equal |

## Filter table

Both `found` and `entries` exercise every row; each stat outcome is real, including mode-bit EACCES and an overlong pathname.

| Real stat outcome | Failed equation outcome | Result |
|---|---|---|
| success | real accepted case alias | keep |
| ENOENT | controlled canonical | keep |
| ENOTDIR | controlled canonical | keep |
| EACCES | controlled canonical | keep |
| ELOOP | real suffix-growing symlink | omit |
| ENAMETOOLONG | controlled canonical; real stat refusal | omit |

The isolated found-site table injects a pre-filter discovery string in every row (including a controlled final generator spelling); the controlled failed equations are needed only for ENOENT, ENOTDIR, EACCES and ENAMETOOLONG. The entries-site success and ELOOP rows use real canonical behavior. The existing original and extra rename fixtures use real renames at the canonical/filter boundary rather than manufactured filesystem errors. Stable spelled paths retain the existing fast path without a new stat requirement.

## Real ENAMETOOLONG

The probe is `base/loop/tail×128/objects` with `loop -> loop/tail`. `base` is measured physically in bytes. Every row uses real Git and files at all three readers; discovery and canonicalization return on all rows, with no shared exception to list as a new difference. Ordered full evidence is equal.

| Route | Base bytes | Probe errno | 475f63278 places / incomplete | ced02912b places / incomplete | Head places / incomplete | Discovery exceptions |
|---|---:|---|---|---|---|---|
| gitfile | 371 | ENAMETOOLONG (63) | 2/4 | 0/4 | 0/4 | none |
| gitfile | 500 | ENAMETOOLONG (63) | 2/4 | 0/4 | 0/4 | none |
| commondir | 371 | ENAMETOOLONG (63) | 3/3 | 1/3 | 1/3 | none |
| commondir | 500 | ENAMETOOLONG (63) | 3/3 | 1/3 | 1/3 | none |

## Further readable renames

The alternates route renames the object's parent `.git` to `.GIT`, and separately renames a Unicode source-directory component between normalization forms accepted as the same name by this volume. Discovery is rerun against `475f63278` after restoring the original name; records are identical (three places, no incomplete evidence). With the renamed filesystem left in place, that identical retention input keeps the owner, fsck passes and the same private-commit read remains **0**. Both forms pass normally and under linked `TMPDIR`.

## Counts, holds and omissions

**803 distinct ordinary nodes passed**, across **75 completed ordinary invocations**; red and mutant failures are not counted as ordinary passes. Every completed invocation used the shared suite lock. At most two task-owned pytest processes ran concurrently, one pytest per hold, serial within pytest. Longest completed invocation **467.91 s**; peak measured pytest RSS **134.69 MiB**. No outside-lock fallback was needed.

| File | Unique passed nodes |
|---|---:|
| tests/fake/test_detached_retention_fence.py | 1 |
| tests/fake/test_retention_dependencies.py | 30 |
| tests/fake/test_retention_dependency_incomplete.py | 19 |
| tests/fake/test_retention_dependency_review_r1.py | 18 |
| tests/fake/test_retention_dependency_review_r10.py | 12 |
| tests/fake/test_retention_dependency_review_r11.py | 4 |
| tests/fake/test_retention_dependency_review_r2.py | 21 |
| tests/fake/test_retention_dependency_review_r3.py | 6 |
| tests/fake/test_retention_dependency_review_r4.py | 53 |
| tests/fake/test_retention_dependency_review_r5.py | 60 |
| tests/fake/test_retention_dependency_review_r6.py | 16 |
| tests/fake/test_retention_dependency_review_r7.py | 3 |
| tests/fake/test_retention_dependency_review_r8.py | 2 |
| tests/fake/test_retention_dependency_review_r9.py | 4 |
| tests/fake/test_retention_dependency_spelling.py | 16 |
| tests/fake/test_retention_parent_hint.py | 2 |
| tests/fake/test_retention_pointer_failures.py | 7 |
| tests/fake/test_retention_recovery_census.py | 2 |
| tests/fake/test_state_contract.py | 37 |
| tests/fake/test_turn_retention_cwd.py | 6 |
| tests/fake/test_turn_retention_cwd_properties.py | 1 |
| tests/fake/test_turn_retention_job_directory.py | 2 |
| tests/fake/test_turn_retention_job_properties.py | 1 |
| tests/fake/test_turn_retention_quarantine.py | 10 |
| tests/fake/test_writer_retention_fence.py | 1 |
| tests/unit/test_invariants_index.py | 25 |
| tests/unit/test_retention_alternates.py | 23 |
| tests/unit/test_retention_alternates_review_r2.py | 15 |
| tests/unit/test_retention_archive.py | 108 |
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
| tests/unit/test_retention_nested_leases.py | 27 |
| tests/unit/test_retention_pointer_inputs.py | 2 |
| tests/unit/test_retention_pointer_whitespace.py | 9 |
| tests/unit/test_retention_qos.py | 8 |
| tests/unit/test_retention_regenerable_properties.py | 1 |
| tests/unit/test_retention_shared_folders.py | 62 |
| tests/unit/test_retention_worktrees.py | 15 |
| tests/unit/test_test_module_names.py | 1 |


The complete per-hold table gives exact file selections via each linked receipt's `args`/`collected`, with UTC starts and actual load averages. Intentional red and mutant failures are included transparently.

| Hold / file selection | Pytest start UTC | Load 1/5/15 | Passed / failed / skipped | Seconds | Exit |
|---|---|---|---:|---:|---:|
| [red-review-r11](2026-10-31-retention-pins-fix-r11-data/red-review-r11.json) | 2026-10-31T18:03:52.578259+00:00 | 69.54/57.29/52.29 | 2/2/0 | 53.65 | 1 |
| [red-readable](2026-10-31-retention-pins-fix-r11-data/red-readable.json) | 2026-10-31T18:05:31.481160+00:00 | 72.88/62.28/54.81 | 24/10/0 | 249.72 | 1 |
| [red-rename-final](2026-10-31-retention-pins-fix-r11-data/red-rename-final.json) | 2026-10-31T18:10:15.491621+00:00 | 59.72/62.25/57.13 | 0/2/0 | 31.34 | 1 |
| [green-review-r11](2026-10-31-retention-pins-fix-r11-data/green-review-r11.json) | 2026-10-31T18:11:00.799586+00:00 | 58.54/61.66/57.18 | 4/0/0 | 28.55 | 0 |
| [green-readable](2026-10-31-retention-pins-fix-r11-data/green-readable.json) | 2026-10-31T18:11:00.967283+00:00 | 58.54/61.66/57.18 | 34/0/0 | 142.69 | 0 |
| [green-test_retention_dependency_review_r10](2026-10-31-retention-pins-fix-r11-data/green-test_retention_dependency_review_r10.json) | 2026-10-31T18:11:34.166159+00:00 | 58.72/61.42/57.25 | 12/0/0 | 35.49 | 0 |
| [green-test_retention_dependency_review_r9](2026-10-31-retention-pins-fix-r11-data/green-test_retention_dependency_review_r9.json) | 2026-10-31T18:12:15.052381+00:00 | 57.94/60.90/57.28 | 4/0/0 | 19.27 | 0 |
| [green-test_retention_locations](2026-10-31-retention-pins-fix-r11-data/green-test_retention_locations.json) | 2026-10-31T18:12:37.761147+00:00 | 56.83/60.44/57.20 | 22/0/0 | 14.40 | 0 |
| [green-test_retention_pointer_inputs](2026-10-31-retention-pins-fix-r11-data/green-test_retention_pointer_inputs.json) | 2026-10-31T18:12:56.213050+00:00 | 54.63/59.75/57.03 | 2/0/0 | 58.34 | 0 |
| [green-test_retention_location_cycles](2026-10-31-retention-pins-fix-r11-data/green-test_retention_location_cycles.json) | 2026-10-31T18:13:27.426475+00:00 | 54.09/59.11/56.89 | 20/0/0 | 21.65 | 0 |
| [linked-readable](2026-10-31-retention-pins-fix-r11-data/linked-readable.json) | 2026-10-31T18:13:57.005623+00:00 | 51.55/58.02/56.56 | 34/0/0 | 155.71 | 0 |
| [green-test_retention_location_spelling](2026-10-31-retention-pins-fix-r11-data/green-test_retention_location_spelling.json) | 2026-10-31T18:14:00.897005+00:00 | 50.39/57.67/56.45 | 11/0/0 | 29.75 | 0 |
| [green-test_retention_location_inputs](2026-10-31-retention-pins-fix-r11-data/green-test_retention_location_inputs.json) | 2026-10-31T18:14:41.334091+00:00 | 50.00/56.63/56.12 | 1/0/0 | 32.99 | 0 |
| [linked-review-r11](2026-10-31-retention-pins-fix-r11-data/linked-review-r11.json) | 2026-10-31T18:15:20.864978+00:00 | 49.10/55.58/55.76 | 4/0/0 | 27.91 | 0 |
| [linked-test_retention_dependency_review_r10](2026-10-31-retention-pins-fix-r11-data/linked-test_retention_dependency_review_r10.json) | 2026-10-31T18:15:58.859917+00:00 | 53.05/56.12/55.97 | 12/0/0 | 50.43 | 0 |
| [linked-test_retention_location_cycles](2026-10-31-retention-pins-fix-r11-data/linked-test_retention_location_cycles.json) | 2026-10-31T18:16:43.715244+00:00 | 54.55/56.07/55.96 | 20/0/0 | 44.82 | 0 |
| [linked-test_retention_dependency_review_r9](2026-10-31-retention-pins-fix-r11-data/linked-test_retention_dependency_review_r9.json) | 2026-10-31T18:16:55.258204+00:00 | 52.95/55.67/55.81 | 4/0/0 | 30.23 | 0 |
| [linked-test_retention_locations](2026-10-31-retention-pins-fix-r11-data/linked-test_retention_locations.json) | 2026-10-31T18:17:31.580874+00:00 | 52.36/55.22/55.64 | 22/0/0 | 28.49 | 0 |
| [linked-test_retention_location_spelling](2026-10-31-retention-pins-fix-r11-data/linked-test_retention_location_spelling.json) | 2026-10-31T18:17:36.435018+00:00 | 52.65/55.23/55.65 | 11/0/0 | 82.12 | 0 |
| [linked-test_retention_pointer_inputs](2026-10-31-retention-pins-fix-r11-data/linked-test_retention_pointer_inputs.json) | 2026-10-31T18:18:09.980102+00:00 | 51.74/54.73/55.44 | 2/0/0 | 125.53 | 0 |
| [linked-test_retention_location_inputs](2026-10-31-retention-pins-fix-r11-data/linked-test_retention_location_inputs.json) | 2026-10-31T18:19:10.383435+00:00 | 56.79/55.53/55.69 | 1/0/0 | 69.42 | 0 |
| [linked-test_retention_dependency_units](2026-10-31-retention-pins-fix-r11-data/linked-test_retention_dependency_units.json) | 2026-10-31T18:20:25.859595+00:00 | 59.10/56.45/56.01 | 19/0/0 | 120.02 | 0 |
| [linked-test_retention_dependency_review_r8](2026-10-31-retention-pins-fix-r11-data/linked-test_retention_dependency_review_r8.json) | 2026-10-31T18:20:29.567139+00:00 | 59.10/56.45/56.01 | 2/0/0 | 30.07 | 0 |
| [linked-test_retention_dependencies](2026-10-31-retention-pins-fix-r11-data/linked-test_retention_dependencies.json) | 2026-10-31T18:21:10.693085+00:00 | 60.30/56.95/56.21 | 30/0/0 | 129.56 | 0 |
| [ordinary-test_retention_dependency_review_r7](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_dependency_review_r7.json) | 2026-10-31T18:22:36.637436+00:00 | 63.55/59.03/57.08 | 3/0/0 | 32.93 | 0 |
| [green-original-record-comparison](2026-10-31-retention-pins-fix-r11-data/green-original-record-comparison.json) | 2026-10-31T18:23:27.217312+00:00 | 69.31/61.45/58.10 | 2/0/0 | 26.69 | 0 |
| [ordinary-test_retention_parent_hint](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_parent_hint.json) | 2026-10-31T18:23:58.200705+00:00 | 62.60/60.60/57.91 | 2/0/0 | 21.40 | 0 |
| [linked-original-record-comparison](2026-10-31-retention-pins-fix-r11-data/linked-original-record-comparison.json) | 2026-10-31T18:24:00.352172+00:00 | 62.39/60.59/57.92 | 2/0/0 | 25.17 | 0 |
| [ordinary-test_retention_dependency_review_r6](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_dependency_review_r6.json) | 2026-10-31T18:24:25.237972+00:00 | 61.05/60.45/57.95 | 16/0/0 | 51.49 | 0 |
| [red-readable-final](2026-10-31-retention-pins-fix-r11-data/red-readable-final.json) | 2026-10-31T18:24:31.637813+00:00 | 60.65/60.38/57.94 | 24/12/0 | 226.16 | 1 |
| [ordinary-test_retention_pointer_failures](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_pointer_failures.json) | 2026-10-31T18:25:23.902203+00:00 | 58.43/59.77/57.85 | 7/0/0 | 21.34 | 0 |
| [ordinary-test_retention_dependency_review_r5](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_dependency_review_r5.json) | 2026-10-31T18:25:53.321528+00:00 | 65.05/61.35/58.49 | 60/0/0 | 220.58 | 0 |
| [mutant-r8-alias-location-recording-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r8-alias-location-recording-dropped.json) | 2026-10-31T18:28:30.608633+00:00 | 70.49/65.76/60.84 | 0/1/0 | 39.32 | 1 |
| [mutant-r8-pointer-file-location-recording-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r8-pointer-file-location-recording-dropped.json) | 2026-10-31T18:29:24.554932+00:00 | 69.51/66.15/61.26 | 0/1/0 | 40.51 | 1 |
| [ordinary-test_retention_pointer_whitespace](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_pointer_whitespace.json) | 2026-10-31T18:29:47.534973+00:00 | 68.26/66.13/61.39 | 9/0/0 | 64.25 | 0 |
| [mutant-r8-intermediate-link-walk-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r8-intermediate-link-walk-dropped.json) | 2026-10-31T18:30:20.304812+00:00 | 62.86/65.06/61.20 | 0/1/0 | 26.08 | 1 |
| [mutant-r8-lexical-dotdot](2026-10-31-retention-pins-fix-r11-data/mutant-r8-lexical-dotdot.json) | 2026-10-31T18:30:54.317525+00:00 | 61.86/64.64/61.17 | 0/1/0 | 15.21 | 1 |
| [ordinary-test_retention_dependency_review_r4](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_dependency_review_r4.json) | 2026-10-31T18:30:56.773569+00:00 | 60.91/64.39/61.11 | 53/0/0 | 71.14 | 0 |
| [mutant-r8-link-location-followed](2026-10-31-retention-pins-fix-r11-data/mutant-r8-link-location-followed.json) | 2026-10-31T18:31:14.694513+00:00 | 58.62/63.69/60.91 | 0/1/0 | 14.30 | 1 |
| [mutant-r8-top-level-links-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r8-top-level-links-dropped.json) | 2026-10-31T18:31:34.464136+00:00 | 58.06/63.23/60.81 | 0/1/0 | 14.37 | 1 |
| [mutant-r8-parent-hint-scanned](2026-10-31-retention-pins-fix-r11-data/mutant-r8-parent-hint-scanned.json) | 2026-10-31T18:31:54.963605+00:00 | 53.12/61.76/60.34 | 0/1/0 | 16.18 | 1 |
| [ordinary-test_retention_dependency_review_r3](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_dependency_review_r3.json) | 2026-10-31T18:32:12.863918+00:00 | 54.01/61.34/60.22 | 6/0/0 | 20.86 | 0 |
| [mutant-r8-walk-revalidation-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r8-walk-revalidation-dropped.json) | 2026-10-31T18:32:16.307973+00:00 | 54.33/61.28/60.20 | 0/1/0 | 14.42 | 1 |
| [mutant-r8-routine-skip-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r8-routine-skip-dropped.json) | 2026-10-31T18:32:35.773341+00:00 | 56.11/61.27/60.22 | 0/1/0 | 15.15 | 1 |
| [ordinary-test_retention_dependency_review_r2](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_dependency_review_r2.json) | 2026-10-31T18:32:38.808111+00:00 | 56.11/61.27/60.22 | 21/0/0 | 61.92 | 0 |
| [mutant-r9-text-canonicalization-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r9-text-canonicalization-dropped.json) | 2026-10-31T18:32:59.419689+00:00 | 54.30/60.53/59.99 | 0/1/0 | 22.38 | 1 |
| [mutant-r9-alternates-canonicalization-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r9-alternates-canonicalization-dropped.json) | 2026-10-31T18:33:27.353608+00:00 | 53.66/59.79/59.74 | 0/1/0 | 27.86 | 1 |
| [ordinary-test_retention_alternates_review_r2](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_alternates_review_r2.json) | 2026-10-31T18:33:47.382896+00:00 | 54.76/59.65/59.68 | 15/0/0 | 35.66 | 0 |
| [mutant-r9-scan-canonicalization-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r9-scan-canonicalization-dropped.json) | 2026-10-31T18:34:05.227716+00:00 | 53.35/59.10/59.49 | 0/1/0 | 42.33 | 1 |
| [ordinary-test_retention_dependency_review_r1](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_dependency_review_r1.json) | 2026-10-31T18:34:32.971751+00:00 | 64.38/61.29/60.27 | 18/0/0 | 53.96 | 0 |
| [mutant-r9-start-spelling-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r9-start-spelling-dropped.json) | 2026-10-31T18:34:59.044040+00:00 | 62.91/61.21/60.27 | 0/1/0 | 29.84 | 1 |
| [ordinary-test_retention_dependency_incomplete](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_dependency_incomplete.json) | 2026-10-31T18:35:38.693574+00:00 | 62.90/61.45/60.40 | 19/0/0 | 103.73 | 0 |
| [mutant-r10-found-filter-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r10-found-filter-dropped.json) | 2026-10-31T18:35:43.249825+00:00 | 63.79/61.65/60.48 | 0/1/0 | 27.10 | 1 |
| [mutant-r10-entries-filter-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r10-entries-filter-dropped.json) | 2026-10-31T18:36:27.465008+00:00 | 69.90/63.27/61.12 | 0/1/0 | 29.31 | 1 |
| [mutant-r10-check-before-spelling](2026-10-31-retention-pins-fix-r11-data/mutant-r10-check-before-spelling.json) | 2026-10-31T18:37:07.746944+00:00 | 63.09/62.43/60.91 | 0/1/0 | 30.14 | 1 |
| [ordinary-test_retention_alternates](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_alternates.json) | 2026-10-31T18:37:33.655031+00:00 | 61.14/62.02/60.81 | 23/0/0 | 123.31 | 0 |
| [mutant-r11-stat-gate-dropped](2026-10-31-retention-pins-fix-r11-data/mutant-r11-stat-gate-dropped.json) | 2026-10-31T18:37:50.046689+00:00 | 61.49/62.04/60.83 | 0/1/0 | 42.28 | 1 |
| [mutant-r11-any-oserror-drops](2026-10-31-retention-pins-fix-r11-data/mutant-r11-any-oserror-drops.json) | 2026-10-31T18:38:43.458157+00:00 | 58.75/61.05/60.53 | 0/1/0 | 29.35 | 1 |
| [mutant-r11-enametoolong-kept](2026-10-31-retention-pins-fix-r11-data/mutant-r11-enametoolong-kept.json) | 2026-10-31T18:39:23.518650+00:00 | 60.59/61.14/60.58 | 0/1/0 | 32.13 | 1 |
| [ordinary-test_retention_job_dependencies](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_job_dependencies.json) | 2026-10-31T18:39:47.769881+00:00 | 60.16/61.00/60.55 | 12/0/0 | 28.80 | 0 |
| [ordinary-test_retention_folded_pin](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_folded_pin.json) | 2026-10-31T18:40:21.628887+00:00 | 58.69/60.56/60.41 | 38/0/0 | 283.11 | 0 |
| [test_turn_retention_cwd](2026-10-31-retention-pins-fix-r11-data/test_turn_retention_cwd.json) | 2026-10-31T18:41:23.597993+00:00 | 54.16/58.65/59.69 | 6/0/0 | 72.51 | 0 |
| [test_turn_retention_cwd_properties](2026-10-31-retention-pins-fix-r11-data/test_turn_retention_cwd_properties.json) | 2026-10-31T18:42:46.774489+00:00 | 55.57/58.27/59.46 | 1/0/0 | 107.79 | 0 |
| [test_turn_retention_job_directory](2026-10-31-retention-pins-fix-r11-data/test_turn_retention_job_directory.json) | 2026-10-31T18:44:50.984163+00:00 | 66.69/61.33/60.47 | 2/0/0 | 71.50 | 0 |
| [ordinary-test_retention_dependency_spelling](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_dependency_spelling.json) | 2026-10-31T18:45:25.309075+00:00 | 77.26/64.38/61.60 | 16/0/0 | 99.24 | 0 |
| [test_turn_retention_job_properties](2026-10-31-retention-pins-fix-r11-data/test_turn_retention_job_properties.json) | 2026-10-31T18:46:16.134326+00:00 | 72.29/65.30/62.13 | 1/0/0 | 217.89 | 0 |
| [ordinary-test_retention_dependency_aliases](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_dependency_aliases.json) | 2026-10-31T18:47:24.855047+00:00 | 84.59/70.64/64.38 | 10/0/0 | 51.35 | 0 |
| [ordinary-test_retention_recovery_census](2026-10-31-retention-pins-fix-r11-data/ordinary-test_retention_recovery_census.json) | 2026-10-31T18:48:46.839396+00:00 | 103.23/77.65/67.50 | 2/0/0 | 66.74 | 0 |
| [test_turn_retention_quarantine](2026-10-31-retention-pins-fix-r11-data/test_turn_retention_quarantine.json) | 2026-10-31T18:50:07.207976+00:00 | 96.53/82.76/70.42 | 10/0/0 | 112.09 | 0 |
| [dependency-property-0](2026-10-31-retention-pins-fix-r11-data/dependency-property-0.json) | 2026-10-31T18:50:08.594345+00:00 | 96.53/82.76/70.42 | 1/0/0 | 210.58 | 0 |
| [nested-leases](2026-10-31-retention-pins-fix-r11-data/nested-leases.json) | 2026-10-31T18:52:12.831721+00:00 | 70.79/77.68/70.08 | 27/0/0 | 157.39 | 0 |
| [dependency-property-1](2026-10-31-retention-pins-fix-r11-data/dependency-property-1.json) | 2026-10-31T18:53:46.025823+00:00 | 63.63/74.35/69.66 | 1/0/0 | 175.33 | 0 |
| [ordinary-test_state_contract](2026-10-31-retention-pins-fix-r11-data/ordinary-test_state_contract.json) | 2026-10-31T18:54:56.740259+00:00 | 58.10/70.31/68.49 | 37/0/0 | 73.03 | 0 |
| [ordinary-test_test_module_names](2026-10-31-retention-pins-fix-r11-data/ordinary-test_test_module_names.json) | 2026-10-31T18:56:22.748662+00:00 | 63.04/68.16/67.78 | 1/0/0 | 44.84 | 0 |
| [dependency-property-2](2026-10-31-retention-pins-fix-r11-data/dependency-property-2.json) | 2026-10-31T18:56:59.857062+00:00 | 74.16/70.32/68.58 | 1/0/0 | 467.91 | 0 |
| [ordinary-test_invariants_index](2026-10-31-retention-pins-fix-r11-data/ordinary-test_invariants_index.json) | 2026-10-31T18:57:20.858141+00:00 | 65.71/68.63/68.02 | 25/0/0 | 43.52 | 0 |
| [archive-0](2026-10-31-retention-pins-fix-r11-data/archive-0.json) | 2026-10-31T18:58:41.440716+00:00 | 96.20/76.45/70.98 | 9/5/0 | 154.26 | 1 |
| [archive-1](2026-10-31-retention-pins-fix-r11-data/archive-1.json) | 2026-10-31T19:01:34.523781+00:00 | 66.84/68.86/68.63 | 12/0/0 | 147.91 | 0 |
| [archive-2](2026-10-31-retention-pins-fix-r11-data/archive-2.json) | 2026-10-31T19:04:08.243367+00:00 | 67.26/69.81/69.18 | 35/0/0 | 136.83 | 0 |
| [dependency-property-3](2026-10-31-retention-pins-fix-r11-data/dependency-property-3.json) | 2026-10-31T19:04:54.409511+00:00 | 54.58/66.20/67.89 | 1/0/0 | 90.44 | 0 |
| [shared-examples](2026-10-31-retention-pins-fix-r11-data/shared-examples.json) | 2026-10-31T19:06:48.041117+00:00 | 79.01/68.29/68.30 | 58/0/0 | 340.72 | 0 |
| [archive-3](2026-10-31-retention-pins-fix-r11-data/archive-3.json) | 2026-10-31T19:06:49.450560+00:00 | 79.01/68.29/68.30 | 13/0/0 | 146.10 | 0 |
| [archive-4](2026-10-31-retention-pins-fix-r11-data/archive-4.json) | 2026-10-31T19:09:33.841867+00:00 | 69.87/68.46/68.40 | 11/1/0 | 99.11 | 1 |
| [archive-5](2026-10-31-retention-pins-fix-r11-data/archive-5.json) | 2026-10-31T19:11:27.518414+00:00 | 69.87/69.35/68.79 | 12/0/0 | 102.50 | 0 |
| [shared-property-0](2026-10-31-retention-pins-fix-r11-data/shared-property-0.json) | 2026-10-31T19:12:42.659655+00:00 | 63.67/67.89/68.31 | 1/0/0 | 53.33 | 0 |
| [archive-6](2026-10-31-retention-pins-fix-r11-data/archive-6.json) | 2026-10-31T19:13:24.894775+00:00 | 63.73/67.55/68.17 | 10/0/0 | 56.86 | 0 |
| [shared-property-1](2026-10-31-retention-pins-fix-r11-data/shared-property-1.json) | 2026-10-31T19:13:46.425228+00:00 | 60.41/66.41/67.74 | 1/0/0 | 443.96 | 0 |
| [admission-detached](2026-10-31-retention-pins-fix-r11-data/admission-detached.json) | 2026-10-31T19:14:28.412368+00:00 | 53.28/63.78/66.71 | 1/0/0 | 39.78 | 0 |
| [admission-writer](2026-10-31-retention-pins-fix-r11-data/admission-writer.json) | 2026-10-31T19:15:15.178541+00:00 | 49.83/61.40/65.68 | 1/0/0 | 39.12 | 0 |
| [extra-test_retention_worktrees](2026-10-31-retention-pins-fix-r11-data/extra-test_retention_worktrees.json) | 2026-10-31T19:16:01.566449+00:00 | 48.22/59.22/64.63 | 15/0/0 | 59.43 | 0 |
| [extra-test_retention_qos](2026-10-31-retention-pins-fix-r11-data/extra-test_retention_qos.json) | 2026-10-31T19:17:21.918810+00:00 | 60.53/60.05/64.43 | 8/0/0 | 35.38 | 0 |
| [extra-test_retention_regenerable_properties](2026-10-31-retention-pins-fix-r11-data/extra-test_retention_regenerable_properties.json) | 2026-10-31T19:18:08.259609+00:00 | 57.18/59.40/63.97 | 1/0/0 | 165.04 | 0 |
| [plain-test_retention_dependency_review_r8](2026-10-31-retention-pins-fix-r11-data/plain-test_retention_dependency_review_r8.json) | 2026-10-31T19:21:03.202096+00:00 | 55.67/58.94/63.01 | 2/0/0 | 46.52 | 0 |
| [shared-property-2](2026-10-31-retention-pins-fix-r11-data/shared-property-2.json) | 2026-10-31T19:21:16.867842+00:00 | 55.11/58.62/62.83 | 1/0/0 | 233.84 | 0 |
| [plain-test_retention_dependency_units](2026-10-31-retention-pins-fix-r11-data/plain-test_retention_dependency_units.json) | 2026-10-31T19:22:04.021446+00:00 | 69.20/62.56/64.10 | 19/0/0 | 139.00 | 0 |
| [plain-test_retention_dependencies](2026-10-31-retention-pins-fix-r11-data/plain-test_retention_dependencies.json) | 2026-10-31T19:24:36.379317+00:00 | 58.00/60.98/63.25 | 30/0/0 | 118.19 | 0 |
| [shared-property-3](2026-10-31-retention-pins-fix-r11-data/shared-property-3.json) | 2026-10-31T19:25:21.220814+00:00 | 54.52/59.67/62.66 | 1/0/0 | 66.47 | 0 |
| [archive-path-controls](2026-10-31-retention-pins-fix-r11-data/archive-path-controls.json) | 2026-10-31T19:27:22.127465+00:00 | 46.32/56.16/60.96 | 6/0/0 | 82.80 | 0 |


No selected ordinary node remains unpassed.

The live process-listing case `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` is deliberately not selected. The full suite, Python 3.12, CI and `tests/frontend/test_core_live.py` are not run on this lane. CI and mergeability remain the hub's responsibility. Any initial archive path-trap failures and guarded-property interruptions are recorded in the receipts and retry section below; no bound or assertion is loosened.

## Archive path-trap rerun

Exactly six initial archive call failures reproduced the documented `/private/tmp` path classification. No product or test assertion was changed. All six pass with `TMPDIR` in the new worktree folder `sf-fix272r11-fixtures`, removed at cleanup.

- `tests/unit/test_retention_archive.py::test_blob_only_in_a_local_branch_is_archived_not_omitted`
- `tests/unit/test_retention_archive.py::test_borrowed_or_shallow_object_store_is_scratch[alternates]`
- `tests/unit/test_retention_archive.py::test_borrowed_or_shallow_object_store_is_scratch[shallow]`
- `tests/unit/test_retention_archive.py::test_local_path_remote_is_not_a_remote`
- `tests/unit/test_retention_archive.py::test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`
- `tests/unit/test_retention_archive.py::test_survey_is_read_only_and_predicts_the_pass`

Rerun pytest start: 2026-10-31T19:27:22.127465+00:00; load 1/5/15: [46.31591796875, 56.158203125, 60.9580078125]; duration: 82.80 seconds. No guard interrupted any property. Both previously unfinished properties now pass: early fences in 443.96 seconds and regenerable output in 165.04 seconds.


## Named mutants

**19/19 killed.** Mutation source hashes and failing assertions are in `mutant-results.json` and each receipt. A kill requires exactly one call failure at the named killer and no setup/teardown failure. Older-assertion comparisons are made against the committed r10, r9 and r8 receipts; the r8 routine-skip mutation retains this round's filter while removing only the earlier exclusions.

| Mutant | Named killer | Result | Same assertion at r10 / r9 / r8 |
|---|---|---|---|
| r8-alias-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[admin-path] | killed | yes/yes/yes |
| r8-pointer-file-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[gitfile] | killed | yes/yes/yes |
| r8-intermediate-link-walk-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[intermediate-HEAD] | killed | yes/yes/yes |
| r8-lexical-dotdot | tests/unit/test_retention_locations.py::test_dotdot_applies_after_the_link_before_it | killed | yes/yes/yes |
| r8-link-location-followed | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[gitfile-target-HEAD] | killed | yes/yes/yes |
| r8-top-level-links-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[git-entry-HEAD] | killed | yes/yes/yes |
| r8-parent-hint-scanned | tests/unit/test_retention_locations.py::test_inferred_parent_is_not_scanned | killed | yes/yes/yes |
| r8-walk-revalidation-dropped | tests/unit/test_retention_locations.py::test_changed_link_chain_with_the_same_final_target_is_incomplete | killed | yes/yes/yes |
| r8-routine-skip-dropped | tests/unit/test_retention_locations.py::test_routine_records_are_exactly_unchanged[worktree] | killed | yes/yes/yes |
| r9-text-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[gitfile] | killed | yes/yes |
| r9-alternates-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[alternates] | killed | yes/yes |
| r9-scan-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[objects-pack] | killed | yes/yes |
| r9-start-spelling-dropped | tests/unit/test_retention_location_spelling.py::test_routine_alias_keeps_the_exact_record[data-volume-worktree] | killed | yes/yes |
| r10-found-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[alternates-self-suffix] | killed | yes |
| r10-entries-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[scan-self-suffix] | killed | yes |
| r10-check-before-spelling | tests/unit/test_retention_location_cycles.py::test_scan_retargeted_into_cycle_before_spelling_is_incomplete[resolved-target] | killed | yes |
| r11-stat-gate-dropped | tests/fake/test_retention_dependency_review_r11.py::test_return_filter_keeps_readable_store_across_case_rename[alternates] | killed | new |
| r11-any-oserror-drops | tests/unit/test_retention_location_readable.py::test_filter_stat_table[found-ENOENT] | killed | new |
| r11-enametoolong-kept | tests/unit/test_retention_location_readable.py::test_filter_stat_table[found-ENAMETOOLONG] | killed | new |


## Environment, cleanup and delivery

The prescribed macOS scratch path was denied; fallback is `/private/tmp/sf-fix272r11`. Linked TMPDIR uses `link-tmp -> real-tmp`. Sync ran once through `uv_sync_hold.sh`, Python 3.14.7 and the locked dev group; all later runs use that scratch environment and `uv run --no-sync`. Reports, data and tests remain in this worktree throughout. Scratch stays below 1.5 GB and is removed with the rules file's guarded cleanup; the final receipt records its size and removals.

Shared Git refs reject writes, so commits use ignored `.git-local` metadata in this worktree and read shared objects via an alternate. The delivery bundle uses **ced02912b as its prerequisite** and is verified before the final answer. Every commit has the required co-author footer. No history is rewritten; the caller's worktree is untouched. No sub-agents are used, per the lane rules. No real CLI, live stores, providers, live process listing or GitHub calls run. The job repository is never fetched, merged or pushed; Git fixture transfers are local to isolated test repositories. All test and mutant drivers end before cleanup; all task-owned processes end before final delivery.

## PR-body lines

**Fix round r11 repairs R11-F1 without extending the reader.** A place whose spelling changes during discovery keeps the string `475f63278` kept unless a spelling equation fails and the kernel refuses the walk with `ELOOP` or `ENAMETOOLONG`. The original case-renames again record three places, no incomplete entry, and preserve the owner's tree and the identical Git read at 0. `_target`, `check`, the component walker and reader depth are unchanged; retention remains dormant pending approving review r12 and completed CI.

**Evidence:** Review r11 is red 2 passed / 2 failed at `ced02912b`, green 4 passed. The new module passes 36 nodes normally and 36 under linked `TMPDIR`. 496 same-filesystem baseline/head comparisons preserve full ordered incomplete evidence and omit only strings whose real `stat` raises `ELOOP` or `ENAMETOOLONG`. The nine original cycle fixtures' omitted strings raised `ELOOP`; the 371/500-byte long-cycle fixtures exercise real `ENAMETOOLONG`. Five routine records equal `475f63278`; parent-case and Unicode-normalization renames keep the owner and read 0. 19/19 named mutants killed. 803 distinct ordinary nodes passed; see the report for every hold, start/load and any guarded property omission.

**Next:** Review r12 judges fix round r11 and its baseline comparisons; obtain approving review and the hub's all-pass CI/mergeability checks. Activation remains separate.


## New gaps

None found in the executed inputs. No reader, observation set or walker change was made beyond the requested return gate.
