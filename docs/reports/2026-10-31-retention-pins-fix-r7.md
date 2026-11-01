# Retention dependency pins: fix round r7, 2026-10-31

Base: `ccc534524ce0c51b7bb36c07e3f445bb05f5b068`, comprising fix r6 and review r7's unchanged test file. Job start **02:34:22 UTC October 31**; first pytest **02:35:53 UTC**, within two minutes. New-hold cutoff **06:34:22 UTC**; six-hour wall **08:34:22 UTC**. Only the assigned worktree is edited. All requested local selections finish before the cutoff.

**R7-F1 is repaired. All 475 unique ordinary nodes pass, and all 20 named mutants are killed.** The seventeen earlier mutants fail at the same source assertions as in fix r6, with successful setup and teardown. The focused property executes all 40 generated inputs and reaches every requested common-pointer/own-store combination. No product decision required a departure, and no additional executed gap was found beyond D-R7-1.

## Decisions built

1. **R7-F1:** replace recursive `_storage(inferred, named=False, …)` with exactly the parent's folder, `aliases.resolve(parent_objects)` and `_alternates(parent_objects, …)`. The inferred parent remains a storage hint; its repository pointers are not probed. The named admin's own objects and alternates remain recorded and revalidated. Only the one strict mark covering `test_unused_parent_pointer_must_not_globally_pin_trees` is removed; every other byte in review r7's file matches base. Repair commit: `c28b7bc4fb14f0da3a38f16ba5c18e428cd415c2`.
2. **Property reach:** keep the original pointer-input file prefix byte-identical, including the 100-example property and 15 explicit examples. First verify review r7's critical counts at supplied source: readable gitfiles naming `worktrees/<id>` draw absent commondir **2**, LF-only **0**, CRLF-only **1**; that CRLF case has an over-bound gitfile and incomplete evidence. The repaired-source run generates the same 100 vectors. Add a separate property with `max_examples=40`, `derandomize=True`, `database=None`, restricted to named admins Git actually reads. It draws absent/LF-only/CRLF-only commondir; plain, symlinked or alternate own stores; absent parent metadata, an ordinary container, a repository, or an unrelated commondir directory. The container must exist to hold the admin: “absent” refers to its repository metadata. Variable slot IDs permit 40 generated inputs despite only 36 categorical layouts. This clarifies the physical layout, without changing the decision's intent.
3. **Mutants:** preserve all seventeen requested r6 names against their named tests, and add the three r7 definitions for recursive parent probing, own-objects revalidation and parent-objects recording. Mutants patch only imported modules in separate test processes. Every setup and teardown succeeds. Both historical probe killers also pass on unmutated source. Property/mutant-definition commit: `58dbd5e5f97bde640caedfee83cea9a53f8933d4`.
4. **Text:** update `_storage`'s comment, `git_storage`'s docstring, the desktop discovery/stale-target description and the main report. Record D-R7-1, preserve existing observation bounds, and explicitly distinguish r7's partial record from a completed review. C-8.3/C-8.4, archive, recovery and other pointer readers remain byte-identical. PR text ends with review r8 as the next gate.

## Review r7 and fix round r7

Review r7 ended in a cold boot at about **02:21 UTC October 31**, roughly 55 minutes into its run. Its notes record passing r6 regressions (16 cases), turn-retention checks, T2, alternates/job-dependency tests, dependency fake/spelling tests and a source audit. Both generated-only reversions failed, showing reach into R6-F1 and R6-F2 without explicit examples. It executed R7-F1's valid-repository and vanished-target comparison: both retired the owner at `b2a357b6c`, and both falsely pinned it at `95e4a2f9a`.

Its remaining checks and named mutants had not completed. These are partial observations, **not a completed review or approval**. The [preserved progress notes](2026-10-31-retention-pins-fix-r7-data/r7-partial-agent-messages.txt) remain a partial record. Review r8 judges this round's diff and reruns the earlier checks; completed CI remains a separate gate.

## Red and green, including earlier regressions

At `ccc534524`, review r7's file with `--runxfail` gives **1 passed / 2 failed**, both at `assert not recorded.get("incomplete")` after successful setup and a retention pass. The own-objects swap control passes. After the repair and sole mark removal, all **3 pass**.

| Check at repaired source | Result |
|---|---:|
| R7 valid repository / vanished target / own-objects swap | 3 passed |
| R6-F1 named-live absent / LF-only / CRLF-only | All 3 passed |
| R6-F2 padded gitfile / padded commondir | Both passed |
| Full review r6 file, including other controls | 16 passed |
| R5-F1 original three inputs; full r5 file | All three; 60 passed |
| Five preserved valid-parent controls | 5 passed |
| Both preserved descriptor/path change probe controls | 2 passed |
| Pointer read-failure controls, including vanished target | 7 passed |

Red starts **02:35:53 UTC**, load **27.29 / 19.60 / 11.51**. Green r7 starts **02:37:44 UTC**, load **31.69 / 24.03 / 14.26**. The [source audit](2026-10-31-retention-pins-fix-r7-data/source-audit.json) checks the sole r7 mark removal, unchanged original property, protected modules and contract.

## Generated classes and real Git

Git **2.55.0** executes **155 inputs at repaired source** across the two properties. The original 115 inputs give **43 owner reads and 72 rejections**: 32 reads have complete discovery and 11 have incomplete evidence for oversized pointers. The focused 40 inputs all read the owner content successfully and all have complete discovery evidence. A plain store copies the owner's objects and checks its own canonical store; symlinked and alternate stores also require the canonical owner store, or incomplete evidence under the original oracle. Git rejection supplies no usable-store oracle; the unchanged property allows conservative absolute records, requires repeatable discovery and checks incomplete evidence for attempted over-bound pointer reads.

Counts below exclude the original's 15 explicit examples. Original selectors can be inactive in directory layouts. Fixed focused classes are inferred from construction; `—` means a dimension is not drawn. Both properties use fresh context-managed directories for each input; only evidence accumulates on the pytest item.

| Dimension | Class | Original /100 | Focused /40 |
|---|---|---:|---:|
| layout | gitfile | 52 | 40 |
| layout | directory | 48 | 0 |
| prefix | present | 28 | 40 |
| prefix | absent | 27 | 0 |
| prefix | misspelled | 25 | 0 |
| prefix | colon-only | 20 | 0 |
| target | normal | 27 | 40 |
| target | spaces | 24 | 0 |
| target | blank | 28 | 0 |
| target | padded | 21 | 0 |
| common | absent | 23 | 15 |
| common | empty | 20 | 0 |
| common | lf | 24 | 10 |
| common | crlf | 13 | 15 |
| common | target | 20 | 0 |
| own | absent | 36 | 0 |
| own | alternate | 29 | 16 |
| own | symlink | 35 | 12 |
| own | plain | 0 | 12 |
| named | false | 53 | 0 |
| named | true | 47 | 40 |
| padding | short | 39 | 40 |
| padding | under | 23 | 0 |
| padding | over | 38 | 0 |
| pad_file | gitfile | 55 | 40 |
| pad_file | commondir | 45 | 0 |
| parent | absent | — | 10 |
| parent | repository | — | 16 |
| parent | not-repository | — | 6 |
| parent | unrelated-commondir | — | 8 |

The complete common-pointer/own-store cross table counts all generated inputs, including rejected original layouts:

| Common pointer | Own store | Original /100 | Focused /40 |
|---|---|---:|---:|
| absent | absent | 8 | 0 |
| absent | plain | 0 | 6 |
| absent | symlink | 11 | 3 |
| absent | alternate | 4 | 6 |
| empty | absent | 5 | 0 |
| empty | plain | 0 | 0 |
| empty | symlink | 8 | 0 |
| empty | alternate | 7 | 0 |
| lf | absent | 8 | 0 |
| lf | plain | 0 | 1 |
| lf | symlink | 8 | 6 |
| lf | alternate | 8 | 3 |
| crlf | absent | 8 | 0 |
| crlf | plain | 0 | 5 |
| crlf | symlink | 4 | 3 |
| crlf | alternate | 1 | 7 |
| target | absent | 7 | 0 |
| target | plain | 0 | 0 |
| target | symlink | 4 | 0 |
| target | alternate | 9 | 0 |

Every focused cell appears: absent **6/3/6**, LF-only **1/6/3**, CRLF-only **5/3/7**, ordered plain/symlink/alternates. Parent classes count **10 absent metadata, 6 ordinary containers, 16 repositories and 8 unrelated commondir directories**. No required focused combination is missing. Full vectors, Git return codes, paths, pointer sizes and outcomes remain in [the head run](2026-10-31-retention-pins-fix-r7-data/test_retention_pointer_inputs.json); [baseline observations](2026-10-31-retention-pins-fix-r7-data/pointer-original.json) and [class check](2026-10-31-retention-pins-fix-r7-data/original-class-check.json) establish the original thin spot and unchanged generation.

## D-R7-1: unused-parent alternates

Executed input: a healthy standalone checkout whose gitfile names `container/worktrees/slot`, no commondir, its own plain objects, and an unused parent's `objects/info/alternates` containing one **1,048,577-byte** line. `git fsck --no-dangling` and `git show HEAD:f.txt` succeed. Discovery reports the overlong parent alternates line as incomplete; retention globally keeps an unrelated owner tree with `dependency-incomplete`.

This is older than fix r6 and below P2 on this input. The parent alternates traversal stays unchanged, as directed. The desktop documents the difference. The first probe attempt expected fixture content `owner` instead of `outside` and failed before discovery; correcting that assertion yields **2/2 parent-hint controls**. This is a harness correction, not a product counterexample.

## Runs

**475 unique ordinary nodes pass: 468 under tests/ and seven preserved controls.** Every requested local selection is complete. The initial harness expectation above is corrected and rerun; no unresolved ordinary failure remains. Red and mutant failures are intentional evidence.

There are **46 completed pytest runs**, each exactly one serial pytest per hold. Peak owned pytest concurrency is **3**, below four; peak recorded pytest RSS is **137.50 MiB**. The longest run takes **254.35 seconds** including configuration/teardown. No interruption guard fires, and no pytest reaches ten minutes. Independent smaller files share a hold; the four dependency properties run separately with their settings unchanged. Two disjoint mutant queues execute only named selections, prioritizing the r7 names in the second queue.

Four runs receive no admission for 30 minutes. Each owned waiter is stopped before the unchanged selection runs directly:

| Outside-lock run | Start UTC | Load 1/5/15 |
|---|---|---|
| dependency-properties-0 | 04:12:29 | 31.65 / 34.77 / 36.37 |
| test_retention_pointer_inputs | 03:27:14 | 50.77 / 45.34 / 43.95 |
| mutant-r5-gitfile-whitespace-stripping-restored | 03:39:18 | 40.76 / 41.34 / 42.88 |
| test_retention_dependency_review_r3 | 04:11:23 | 36.64 / 36.00 / 36.90 |

F/ means tests/fake/, U/ means tests/unit/, and Data/ means this report's data folder. Counts are unique passing nodes; starts/loads are each file's first successful ordinary hold. Grouped files share their hold's start/load. All dates are **2026-10-31 UTC**. Complete per-phase results, hashes, queue times and timings are in [ledger.json](2026-10-31-retention-pins-fix-r7-data/ledger.json).

| File | Passed | Start UTC (2026-10-31) | Load 1/5/15 |
|---|---:|---|---|
| Data/pointer_matrix.py | 5 | 04:20:26 | 33.67 / 32.78 / 34.39 |
| Data/probes.py | 2 | 04:20:26 | 33.67 / 32.78 / 34.39 |
| F/test_retention_dependencies.py | 30 | 03:15:02 | 34.67 / 39.09 / 43.95 |
| F/test_retention_dependency_incomplete.py | 19 | 04:15:54 | 27.52 / 32.27 / 35.03 |
| F/test_retention_dependency_review_r1.py | 18 | 04:15:54 | 27.52 / 32.27 / 35.03 |
| F/test_retention_dependency_review_r2.py | 21 | 04:11:23 | 36.64 / 36.00 / 36.90 |
| F/test_retention_dependency_review_r3.py | 6 | 04:11:23 | 36.64 / 36.00 / 36.90 |
| F/test_retention_dependency_review_r4.py | 53 | 03:39:25 | 38.78 / 40.90 / 42.71 |
| F/test_retention_dependency_review_r5.py | 60 | 03:34:22 | 42.93 / 46.07 / 44.85 |
| F/test_retention_dependency_review_r6.py | 16 | 02:40:41 | 37.10 / 28.23 / 17.67 |
| F/test_retention_dependency_review_r7.py | 3 | 02:37:44 | 31.69 / 24.03 / 14.26 |
| F/test_retention_dependency_spelling.py | 16 | 03:16:39 | 29.09 / 36.09 / 42.25 |
| F/test_retention_parent_hint.py | 2 | 02:43:18 | 49.27 / 38.23 / 23.64 |
| F/test_retention_pointer_failures.py | 7 | 02:56:18 | 52.56 / 62.36 / 48.02 |
| F/test_retention_recovery_census.py | 2 | 03:16:39 | 29.09 / 36.09 / 42.25 |
| F/test_state_contract.py | 37 | 03:41:24 | 43.73 / 40.80 / 42.33 |
| F/test_turn_retention_cwd.py | 6 | 03:36:47 | 37.22 / 42.51 / 43.59 |
| F/test_turn_retention_cwd_properties.py | 1 | 03:37:54 | 37.31 / 41.32 / 43.04 |
| F/test_turn_retention_job_directory.py | 2 | 03:36:47 | 37.22 / 42.51 / 43.59 |
| F/test_turn_retention_job_properties.py | 1 | 03:39:05 | 39.76 / 41.15 / 42.84 |
| F/test_turn_retention_quarantine.py | 10 | 03:36:47 | 37.22 / 42.51 / 43.59 |
| U/test_invariants_index.py | 25 | 03:41:24 | 43.73 / 40.80 / 42.33 |
| U/test_retention_alternates.py | 23 | 02:57:18 | 68.70 / 64.61 / 49.88 |
| U/test_retention_alternates_review_r2.py | 15 | 04:11:23 | 36.64 / 36.00 / 36.90 |
| U/test_retention_dependency_aliases.py | 10 | 03:14:43 | 35.12 / 39.46 / 44.19 |
| U/test_retention_dependency_properties.py | 4 | 04:12:29 | 31.65 / 34.77 / 36.37 |
| U/test_retention_dependency_units.py | 19 | 03:16:39 | 29.09 / 36.09 / 42.25 |
| U/test_retention_folded_pin.py | 38 | 03:16:39 | 29.09 / 36.09 / 42.25 |
| U/test_retention_job_dependencies.py | 12 | 03:15:02 | 34.67 / 39.09 / 43.95 |
| U/test_retention_pointer_inputs.py | 2 | 03:27:14 | 50.77 / 45.34 / 43.95 |
| U/test_retention_pointer_whitespace.py | 9 | 03:36:11 | 38.18 / 43.34 / 43.91 |
| U/test_test_module_names.py | 1 | 03:41:24 | 43.73 / 40.80 / 42.33 |

## Named mutants

All twenty are killed at their named assertions, with setup and teardown passing. R2–R7 denote the fake review files; W is the pointer-whitespace unit file, PF the pointer-failure file, P the preserved probes and PH the new parent-hint file. Each of the seventeen older names matches the assertion text in its fix-r6 log. The three r7 killers fail respectively at the no-incomplete assertion and the two owner-retention assertions. Definitions, source hashes and exact failure text remain in [mutants.json](2026-10-31-retention-pins-fix-r7-data/mutants.json), per-run evidence and [summary.json](2026-10-31-retention-pins-fix-r7-data/summary.json).

| Mutant | Named killer | Killed | Same assertion as r6 |
|---|---|---|---|
| r2-post-read-identity-check-dropped | r2::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace] | Yes | Yes |
| r2-terminal-legacy-incomplete-dropped | r2::test_c8_4_legacy_incomplete_census_survives_terminal_export[database] | Yes | Yes |
| r3-post-traversal-alias-check-dropped | r3::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live] | Yes | Yes |
| r3-census-moved-after-recovery | r3::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy] | Yes | Yes |
| r3-write-metadata-check-dropped | P::test_changes[same-size-running] | Yes | Yes |
| r3-final-path-check-dropped | P::test_changes[symlink-info-running] | Yes | Yes |
| r3-terminal-census-live-only | r2::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting] | Yes | Yes |
| r4-initial-objects-revalidation-dropped | r4::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live] | Yes | Yes |
| r4-initial-objects-recording-dropped | r4::test_stable_objects_alias_keeps_the_tree_git_reads[live] | Yes | Yes |
| r5-commondir-whitespace-stripping-restored | r5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-commondir-trailing] | Yes | Yes |
| r5-gitfile-whitespace-stripping-restored | r5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-gitfile-leading] | Yes | Yes |
| r5-gitfile-colon-without-space-accepted | W::test_pointer_syntax_and_whitespace_match_real_git[gitfile-no-space] | Yes | Yes |
| r6-empty-common-initial-objects-not-recorded | r6::test_empty_common_target_keeps_the_own_store_git_reads[directory-live-control-lf] | Yes | Yes |
| r6-pointer-read-failure-evidence-dropped | r6::test_crlf_padded_pointer_keeps_the_storage_real_git_reads[padded-gitfile] | Yes | Yes |
| r6-parent-common-fallback-restored | r6::test_empty_common_target_keeps_the_own_store_git_reads[named-live-lf] | Yes | Yes |
| r6-dangling-pointer-treated-as-missing | PF::test_unreadable_common_pointer_keeps_allocated_trees[dangling] | Yes | Yes |
| r6-oversized-pointer-treated-as-missing | PF::test_unreadable_common_pointer_keeps_allocated_trees[oversized] | Yes | Yes |
| r7-unused-parent-pointer-probing-restored | r7::test_unused_parent_pointer_must_not_globally_pin_trees | Yes | New |
| r7-own-objects-revalidation-dropped | r7::test_named_own_objects_swap_keeps_actual_storage | Yes | New |
| r7-parent-objects-recording-dropped | PH::test_inferred_parent_objects_keeps_its_resolved_storage | Yes | New |

## Delivery and cleanup

The sandbox denied the requested Darwin temporary folder, so scratch uses **/private/tmp/sf-fix272r7**. The prescribed locked environment sync installs Python 3.14.7 and the locked dev group. TMPDIR stays inside scratch; retirement uses fixture homes, with short socket paths managed by the fake harness's temporary contexts. No real provider or live Subfleet store/CLI is used. No subagents, fetch, merge, push, GitHub call or external network after dependency sync is used. The wrapper's sysctl reads are sandbox-denied; admitted loads come from `os.getloadavg`.

Shared Git staging is denied. The ordered commits use private metadata inside the worktree on `fix/retention-dependency-pins-217`; every commit ends with the required Claude Opus 5.5 footer. Three porcelain commit attempts stalled while refreshing unrelated checkout files and were stopped. Explicit named staging plus `write-tree`, `commit-tree` and a parent-checked `update-ref` create commits without rewriting history. Delivery is **retention-fix272r7.bundle**, requiring **ccc534524**; the preliminary two-commit bundle verifies, and the bundle is refreshed and verified after the text commit. Shared Git metadata and the release merge remain unchanged.

Recorded scratch high-water is **298,220 KiB (291.23 MiB)**. Guarded incremental cleanup reduces it to 22 MiB after completed homes are removed; final scratch is **30,420 KiB** before deletion. All test drivers end before cleanup. The guarded lane cleanup removes scratch after making owned directories removable without following symlinks. No owned worktree cache/bytecode/build directory or kept failure root remains. [Cleanup evidence](2026-10-31-retention-pins-fix-r7-data/cleanup.json) and [incremental cleanup](2026-10-31-retention-pins-fix-r7-data/incremental-cleanup.json) record removal; raw logs and data stay in the worktree. Private metadata is removed only after final bundle verification.

## PR-body lines

> Fix round r7 repairs R7-F1: inferred parents add their folder, resolved objects and alternates without probing their repository pointers. Valid-repository and vanished-target inputs now retire the unrelated owner. Own storage remains recorded and revalidated; R6-F1, R6-F2 and R5-F1 protections pass. Retention stays dormant pending approving review r8 and completed CI.
>
> Evidence: r7 red 1/2 → green 3/0; 475 unique ordinary passes; all 20 named mutants killed, with the seventeen earlier names failing at their r6 assertions. The unchanged property runs 100 generated plus 15 explicit inputs; the focused property runs 40 readable inputs covering all nine common-pointer/own-store combinations. D-R7-1 records the older unused-parent alternates false pin.
>
> Next: Obtain approving review r8 and complete every CI shard before merge or re-enablement.

## What did not run

Additional archive/recovery suites reserved for review r8, the full suite, Python 3.12, external CI and the person-only `tests/frontend/test_core_live.py` probe. No requested local node or named mutant remains unrun. The archive and recovery implementation files are unchanged.
