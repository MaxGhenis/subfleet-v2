VERDICT #272: REQUEST_CHANGES

Reviewed head: `a3b9cc246701147d09c24980f7602c79c1c61453`. Round range: `373a7f5bf..a3b9cc246`.

**R9-F1 is a P2 under the age-independent stop rule.** The original R8-F1 inputs pass, but covered physical-file and direct-entry locations reached through macOS's Data-volume firmlink still miss the canonical tree comparison. No deeper reader is requested.

## New finding

**R9-F1 — P2: canonicalize auxiliary locations before pin comparisons.**

Locations: [dependencies.py:247](/Users/maxghenis/.subfleet/worktrees/20261031-043007-pr272-review-r9/subfleet/dependencies.py:247) (`_Aliases.text`), line 404 (`_alternates`) and line 267 (`_Aliases.scan`); line 281 forwards the raw locations. `files` and `entries` retain the component walk's spelling, while `links` correctly canonicalizes each physical entry's parent.

On this host, `/System/Volumes/Data/private/tmp/...` and `/private/tmp/...` reach the same inode, and `folders.canonical` returns the latter. Recorded auxiliary locations retain the former prefix. `live`, `users`, `dependents` and fences intentionally use string operations, so those records do not match the canonical allocated owner tree.

Four final executed inputs use an outside checkout and stable Git storage:

| Location in owner | Route Git uses | fsck / clean status / initial read | Incomplete evidence | Retention | Identical read afterward |
|---|---|---|---|---|---|
| Regular gitfile | `checkout/.git -> Data/owner/pointer`, naming stable gitdir | 0 / 0 / 0 | None | Prunes owner | 128 |
| Regular commondir | `admin/commondir -> Data/owner/pointer`, naming stable common | 0 / 0 / 0 | None | Prunes owner | 128 |
| Regular alternates | `objects/info/alternates -> Data/owner/pointer`, naming stable objects | 0 / 0 / 0 | None | Prunes owner | 128 |
| Pack directory | `stable/.git/objects/pack -> Data/owner/packs` | 0 / 0 / 0 | None | Prunes owner | 128 |

Each read serves the owner's commit and `owner-only.txt`. Discovery finishes before a running borrower is recorded; retention then retires the required tree. The final tests fail only at the owner-retention assertion, with passing setup and teardown. These are stable repositories, and both pointer files and direct `objects/pack` links are explicitly within the round's claimed routes.

Evidence: [four executed inputs](/Users/maxghenis/.subfleet/worktrees/20261031-043007-pr272-review-r9/review-r9-data/R9-F1-inputs.json), [final red hold](/Users/maxghenis/.subfleet/worktrees/20261031-043007-pr272-review-r9/review-r9-data/red-r9-firmlink-final.json), [regression file](/Users/maxghenis/.subfleet/worktrees/20261031-043007-pr272-review-r9/tests/fake/test_retention_dependency_review_r9.py:24). Run with `--runxfail` to expose all four failures. Normally the four are strict expected failures naming R9-F1, so the added file stays green. It skips where the macOS Data-volume alias is unavailable.

**Fix:** canonicalize successful physical file locations in `_Aliases.text` and `_alternates`, and canonicalize resolved scan targets before adding them to `entries`, outside transactions. Keep `links` as canonical parent plus name; following the final link entry would lose the location R8-F1 needs.

The [three-site in-memory trial](/Users/maxghenis/.subfleet/worktrees/20261031-043007-pr272-review-r9/review-r9-data/r9-canonical-trial.diff) passes **nine tests**: all four new cases and all five exact routine-record controls. Independently omitting file canonicalization fails `[gitfile]`; independently omitting scanned-target canonicalization fails `[objects-pack]`, both at owner retention after valid Git setup. Product source was not changed.

## R8-F1 and earlier findings

- **Original R8-F1: PASS, bounded repair.** Both canonical-path inputs pass fsck, clean status and owner-commit reads, survive maintenance, and remain readable. Independent forced-removal controls for both change the identical read from 0 to 128. All seven new traversal forms, the sibling storage-target control and all 55 generated location inputs pass their owner-removal checks. Firmlink variants expose R9-F1 above.
- **R6-F1 / R6-F2: PASS.** All 16 r6 cases pass, including named own stores with absent/LF/CRLF commondir and both padded pointers. Pointer-failure controls pass seven cases.
- **R7-F1: PASS.** All three r7 cases pass, including the two original valid-repository/vanished-target inputs. Parent-hint tests pass two. The independent historical matrix passes 37 controls, including healthy unused parents, direct parent-reader checks, pointer-read failures and terminal gone targets.
- R1–R5 files, aliases and recovery census pass; all requested turn, dependency, recovery/archive and contract guards pass. Their exact counts are below.

## Decisions 1–12

| Decision | Verdict at head | Executed evidence |
|---|---|---|
| 1. Component walk | PASS | Absolute/relative three-link `..` controls; intermediate links; ENOENT/ENOTDIR/ELOOP and EACCES; same-target chain mutation; 64-hop tests. Git reads through 32 hops, rejects tested 33/40/63/64/65/80; no accepted read exceeds walker reach. `334b82f22` preserves bounded stops. |
| 2. Pointer files | REQUEST_CHANGES | Original physical-file regressions pass, but firmlink Gitfile/commondir/alternates inputs retire required metadata with complete evidence (R9-F1). |
| 3. Direct-entry scan | REQUEST_CHANGES | Actual git/common/object/alternate directories scanned; missing scan routine, ENOTDIR/ELOOP/EACCES incomplete; parent and parent alternate hints remain unscanned. Data-alias `objects/pack` target misses its canonical owner (R9-F1). |
| 4. Routine records | PASS on five prescribed controls | `base == head == expected` for plain, worktree-add, shared, nested git-new-workdir and no-Git. Sibling storage target is retained. Untouched 19 dependency-unit cases pass. |
| 5. Consumers | Strings are handled correctly; canonical input fails | Read `needed`, `fenced`, `fences`, `live`, `users`, `dependents` and daemon/retention/archive callers. All are unchanged, string/DB-only and do not respell recorded paths; R9-F1 therefore has to be fixed during discovery. Two normal link-fence stability controls pass, including retarget and unlink, with discovery and canonicalization forbidden under the store transaction. |
| 6. Marks and examples | PASS | R8 body byte-identical except strict decorator; both original cases pass. All seven requested traversal forms pass real-Git removal controls. |
| 7. New property | PASS, bounded | 55 complete owner-dependent inputs, all eleven positions, unrelated omitted, each identical read fails after owner removal; exact class table matches. No firmlink selector. |
| 8. Focused property | PASS | 40 successful complete inputs, all nine common/own combinations; false-incomplete R7-F1 reversion detected. |
| 9. Linked TMPDIR | PASS | Six separate linked-TMPDIR file holds pass. Original helper resolves base; other touched equality checks use physical/canonical paths. Firmlinks expose a separate product gap. |
| 10. Mutants | PASS accounting | All nine new mutants die; thirteen old names die at the same r7 assertions; seven survive and each has an independently killed double-removal trial. |
| 11. Reversions | PASS | Eight generated-only trials preserve settings, seed derivation and selected bodies; 15 explicit examples removed per scratch copy. Table and all first failures match the round byte for byte. Focused lacks location/entry selectors for decisions 1–3. |
| 12. Text | Current-status correction required | C-8.3/C-8.4 unchanged. Canonical spelling required by C-8.4 is violated by R9-F1; retain the contract and repair discovery. Qualify the repair lead and current reports. Listed limits remain truthful; old outside-checkout wording needs the D-R8-2 qualification. |

## Checks 1–7

| Check | Verdict | Evidence |
|---|---|---|
| 1. R8-F1 at head | Original inputs PASS; covered firmlink variants FAIL | Two original r8 tests and seven new traversal forms pass; forced owner-removal controls give 0->128. Four R9-F1 variants have successful fsck/status/read, complete discovery, owner pruned, then read 128. |
| 2. Earlier findings | PASS | R6 file 16, R7 file 3 (valid-repository and vanished-target included), parent hint 2 and pointer failures 7; earlier r1–r5 files pass. |
| 3. Routes and invariant | Bounded statements verified | D-R8-1 gives 0->128 with no owner record. Unused direct-link target gives unchanged reads before/after removal. Wide targets do not pin descendants; owner-contained/equal targets pin owner until job ends. These are listed differences under the stop rule. |
| 4. Terminal paths / transactions | PASS | Recorded and legacy terminal jobs with gone targets, with/without old incomplete evidence, retire; complete exporters retain output paths only. Census precedes recovery. Submit/legacy discovery remains outside transactions. Database-only probes forbid canonicalization/discovery inside checks. |
| 5. Marks | PASS | Literal byte comparison confirms only r8 strict decorator removed. New R9-F1 test has four strict expected failures naming the finding. |
| 6. Linked TMPDIR | PASS | Pointer inputs, locations, location property, r8, dependency units and fake dependencies each pass in a separate linked-TMPDIR hold. New R9-F1 probes run there too and explicitly add a Data-volume alias. |
| 7. Text | Correction required for current status | Repair closure must be qualified by R9-F1. Keep C-8.3/C-8.4 unchanged; their canonical-spelling requirement needs implementation. All class/reversion/mutant accounting reproduces. D-R6-1…4, D-R7-1 and D-R8-1 rerun as documented. |

## Generated input classes

The location property runs 55 complete owner-dependent inputs: all eleven positions appear, unrelated is omitted, and the identical real-Git read fails after owner removal. Independent inputs: 0/55. The following table is byte-identical to the round's table.

| Position | Inputs | Chain 1/2/3 | Absolute/relative links | Unnamed/named admin | Needs owner / independent |
|---|---:|---|---|---|---|
| gitfile-target | 6 | 1/2/3 | 3/3 | 5/1 | 6/0 |
| intermediate | 8 | 3/4/1 | 3/5 | 2/6 | 8/0 |
| gitfile-pointer | 2 | 1/1/0 | 0/2 | 1/1 | 2/0 |
| commondir-file | 8 | 4/3/1 | 3/5 | 4/4 | 8/0 |
| commondir-target | 3 | 2/1/0 | 1/2 | 2/1 | 3/0 |
| objects | 4 | 3/1/0 | 3/1 | 4/0 | 4/0 |
| objects-info | 9 | 4/2/3 | 5/4 | 3/6 | 9/0 |
| alternates-file | 3 | 2/1/0 | 1/2 | 1/2 | 3/0 |
| alternates-line | 2 | 2/0/0 | 1/1 | 0/2 | 2/0 |
| git-entry | 7 | 6/0/1 | 4/3 | 3/4 | 7/0 |
| objects-pack | 3 | 2/0/1 | 2/1 | 1/2 | 3/0 |

The original property still has 100 generated + 15 explicit inputs: 43 successful owner reads and 72 rejections, with 32 complete and 11 deliberately over-bound successful reads. The focused property has 40 successful, complete reads; common absent/LF/CRLF = 15/10/15, own plain/link/alternate = 12/12/16 and parent absent/ordinary/repository/unrelated-commondir = 10/6/16/8. All nine common/own combinations appear. No property has a firmlink selector.

## Generated-only reversions

All eight scratch-copy trials preserve property bodies, settings and deterministic seed derivation. They use `max_examples=55/40`, `derandomize=True`, `database=None`; 15 explicit original examples are removed per copy. The following table is byte-identical to the round's, including every first failing generated input before shrinking. The focused strategy has only slot/common/own/parent selectors and records object storage directly, so it cannot isolate decisions 1–3's metadata locations.

| Reverted | Location property /55 | Focused /40 | First failing generated input or strategy boundary |
|---|---|---|---|
| decision1 | Detected | Pass | location: `{"chain": 1, "entry": "HEAD", "named": false, "position": "gitfile-target", "relative": false}` Focused strategy has no metadata-only owner or entry selector (NAMED_OWN_INPUTS); it records the owner’s object storage directly. |
| decision2 | Detected | Pass | location: `{"chain": 2, "entry": "HEAD", "named": false, "position": "gitfile-pointer", "relative": true}` Focused strategy has no metadata-only owner or entry selector (NAMED_OWN_INPUTS); it records the owner’s object storage directly. |
| decision3 | Detected | Pass | location: `{"chain": 1, "entry": "HEAD", "named": false, "position": "git-entry", "relative": false}` Focused strategy has no metadata-only owner or entry selector (NAMED_OWN_INPUTS); it records the owner’s object storage directly. |
| r7-f1 | Detected | Detected | location: `{"chain": 1, "entry": "refs", "named": true, "position": "git-entry", "relative": true}` focused: `{"common": "absent", "own": "plain", "parent": "unrelated-commondir", "slot": 162}` |

## Seven surviving older mutants

All seven pass their named tests. Each has the second required answer: a named newer guard covers the input, and removing that guard along with the old one fails the same named test. Every double-removal setup/teardown passes. The named-input redundancy claim is therefore supported; it does not claim the old guard is universally unnecessary.

| Surviving old mutant | Newer guard and independent removal | Named test that fails when both are removed |
|---|---|---|
| r2-post-read-identity-check-dropped | The new alternates filename registration is revalidated by `_Aliases.check`; replacement changes its identity. Double trial also removes that filename registration. | tests/fake/test_retention_dependency_review_r2.py::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace] |
| r3-final-path-check-dropped | The walked alternates filename and scanned info-link registrations detect the changed info chain. Double trial skips revalidation of those info/alternates paths. | docs/reports/2026-10-31-retention-pins-fix-r7-data/probes.py::test_changes[symlink-info-running] |
| r4-initial-objects-revalidation-dropped | The direct-entry scan registers the objects link, and the alternates filename records its chain. Double trial also disables revalidation of those objects-path registrations. | tests/fake/test_retention_dependency_review_r4.py::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live] |
| r4-initial-objects-recording-dropped | The direct-entry scan retains the resolved objects target. Double trial also removes scans. | tests/fake/test_retention_dependency_review_r4.py::test_stable_objects_alias_keeps_the_tree_git_reads[live] |
| r6-empty-common-initial-objects-not-recorded | The direct-entry scan retains the resolved own objects target despite the old conditional omission. Double trial also removes scans. | tests/fake/test_retention_dependency_review_r6.py::test_empty_common_target_keeps_the_own_store_git_reads[directory-live-control-lf] |
| r6-parent-common-fallback-restored | The scan of the named gitdir retains its own objects-link target even with the parent fallback restored. Double trial also removes scans. | tests/fake/test_retention_dependency_review_r6.py::test_empty_common_target_keeps_the_own_store_git_reads[named-live-lf] |
| r7-own-objects-revalidation-dropped | The direct-entry scan and walked alternates filename retain the original objects chain. Double trial also disables revalidation of objects-path registrations. | tests/fake/test_retention_dependency_review_r7.py::test_named_own_objects_swap_keeps_actual_storage |

## Listed differences

The round's stop rule classifies these as listed differences. R9-F1 uses explicitly covered pointer/direct-entry routes and remains a P2 requiring repair.

| Difference | Executed input and result | One line for the doc |
|---|---|---|
| D-R6-1 | Spaced admin, trimmed twin, spaced common alias and physical common controls; valid Git reads precede archive/survey misses or trimmed metadata selection. `readers_probe.py`, eight controls with rows below. | Outside discovery, archive and survey retain legacy whitespace parsing: some valid spaced admin/common pointer spellings are missed or select a trimmed twin. |
| D-R6-2 | Nested gitfiles target external admin names with space/tab suffixes; Git reads HEAD, survey classifies them internal. | Nested gitfile classification still strips target bytes and can treat a whitespace-suffixed external admin as internal. |
| D-R6-3 | Valid common-space and trimmed registrations; cleanup removes the trimmed twin, leaving the actual admin. | Registration cleanup still strips common-directory output and can act on a whitespace target's trimmed twin. |
| D-R6-4 | A complete bundle verifies despite the stripped alternate; no incorrect verification acceptance is established. | Bundle verification still strips common targets when constructing its alternate; a complete pack can verify without using that alternate. |
| D-R7-1 | Named admin has healthy own objects; unused inferred-parent alternates line is 1,048,577 bytes. Base and head globally pin an unrelated owner. | An unused inferred parent's over-bound alternates can still globally pin trees while the borrower is live or exporting. |
| D-R8-1 | `stable/.git/refs/heads/main -> owner/branch`: complete discovery records no owner place, Git reads before removal (0) and fails after removal (128). | Links deeper than one level inside storage remain outside discovery unless another tracked metadata path traverses them. |
| D-R8-2 | Unused direct entry points to unrelated/unused, owner/broad, owner, worktrees parent, fixture home, fixture root or `/`; commit reads are unchanged by removing unrelated/owner. Targets inside/equal to owner pin owner only while the reader is running; broad ancestors do not pin descendant trees. An outside-checkout input-folder alias also adds an auxiliary entry versus base. | Auxiliary link discovery can retain an unused target or an input-folder link outside Git; only a recorded path equal to or inside a candidate tree pins it, and complete Git pins end with the job. |

The original forward implication has the expressly documented D-R8-1 boundary. The universal converse is false for unused direct links, as the round already states. Width does not turn a broad target into a global descendant pin. With complete evidence, pending output alone retains output paths after the job ends.

One old docstring sentence needs qualification for D-R8-2. Replace `Empty outside a checkout` in `subfleet/dependencies.py:138` with: `Without checkout metadata, discovery may still retain physical symlink entries traversed to reach the supplied folder.` This is the permitted extra-location difference; it does not hold the verdict.

## Holds and coverage

**All 745 requested nodes and 84 independent controls pass: 829 distinct passing nodes.** The new review file has **four strict expected failures**, separately counted. No requested local node or named mutant remains unrun, apart from the mandated live-case deselection.

There were **99 completed locked pytest invocations**, peak pytest concurrency **2**, longest **563.07 seconds**, and peak recorded pytest RSS **202.66 MiB**. None used the 30-minute direct fallback. The first hold began `2026-10-31T08:31:46.723463+00:00` and the last test ended `2026-10-31T09:35:24.412063+00:00`, well before the four-hour start cutoff. Python 3.14.7, Git 2.55.0, locked dev environment; environment-dependent property settings match their normal defaults. The wrapper's `sysctl` is sandbox-denied; admitted runs record actual `os.getloadavg()` independently.

Qualifications:

- Archive's first hold hit its 540-second interrupt and ended after 563.07 seconds including teardown, below ten minutes. It had 69 passes, five specified path-classification failures and 34 nodes uncalled. All six named path cases passed unchanged with worktree TMPDIR; the other 33 nodes passed in groups of at most twelve. The final union is 108 passes. This was an environment/hold split, not another finding.
- The six worktree retries were `test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`, `test_blob_only_in_a_local_branch_is_archived_not_omitted`, `test_local_path_remote_is_not_a_remote`, `test_borrowed_or_shallow_object_store_is_scratch[alternates]`, `test_borrowed_or_shallow_object_store_is_scratch[shallow]`, and `test_survey_is_read_only_and_predicts_the_pass`.
- Six early R9 probe receipts are explicitly superseded/qualified and excluded from final aggregate coverage. Initial ref-directory and packed-ref symlink forms allowed owner reads but fsck rejected their types. The final objects/pack form passes fsck, clean status and the initial read; all four final cases fail at owner retention. Those superseded invocations are included in the 99 completed-process count.
- Shared-folder properties, dependency properties, salvage properties and sweep examples/property were split into separate holds. Normal example counts were preserved.

Per-file counts, earliest contributing starts and load 1/5/15 follow, all October 31 UTC. Repeated nodes count once. The [complete hold table](/Users/maxghenis/.subfleet/worktrees/20261031-043007-pr272-review-r9/review-r9-data/hold-table.md) records every hold's start/load/duration/status; raw JSON records all phases and per-file node IDs.

| File | Pass | Start (UTC) | Load 1/5/15 | Holds |
|---|---:|---|---|---|
| review-r9-data/historic_probes.py | 37 | 09:04:19 | 63.36/60.35/53.13 | historic-controls |
| review-r9-data/probes_r9.py | 32 | 08:56:44 | 68.47/60.78/48.34 | independent-round, link-fence-stability |
| review-r9-data/readers_probe.py | 8 | 08:59:08 | 63.23/63.83/51.65 | older-differences |
| review-r9-data/round_probes.py | 7 | 08:56:44 | 68.47/60.78/48.34 | independent-round |
| tests/fake/test_retention_dependencies.py | 30 | 08:34:58 | 28.36/26.54/28.53 | linked-dependencies |
| tests/fake/test_retention_dependency_incomplete.py | 19 | 08:42:52 | 31.79/32.34/31.04 | regressions-1 |
| tests/fake/test_retention_dependency_review_r1.py | 18 | 08:42:52 | 31.79/32.34/31.04 | regressions-1 |
| tests/fake/test_retention_dependency_review_r2.py | 21 | 08:42:18 | 33.52/32.70/31.11 | regressions-2 |
| tests/fake/test_retention_dependency_review_r3.py | 6 | 08:41:07 | 36.35/33.16/31.14 | regressions-4-3 |
| tests/fake/test_retention_dependency_review_r4.py | 53 | 08:41:07 | 36.35/33.16/31.14 | regressions-4-3 |
| tests/fake/test_retention_dependency_review_r5.py | 60 | 08:38:08 | 29.19/30.06/29.80 | regressions-5 |
| tests/fake/test_retention_dependency_review_r6.py | 16 | 08:36:36 | 39.14/30.91/30.01 | regressions-7-6 |
| tests/fake/test_retention_dependency_review_r7.py | 3 | 08:36:36 | 39.14/30.91/30.01 | regressions-7-6 |
| tests/fake/test_retention_dependency_review_r8.py | 2 | 08:33:44 | 24.77/25.12/28.24 | linked-r8 |
| tests/fake/test_retention_dependency_spelling.py | 16 | 08:43:46 | 32.39/32.30/31.10 | dependencies |
| tests/fake/test_retention_parent_hint.py | 2 | 08:36:36 | 39.14/30.91/30.01 | regressions-7-6 |
| tests/fake/test_retention_pointer_failures.py | 7 | 08:36:36 | 39.14/30.91/30.01 | regressions-7-6 |
| tests/fake/test_retention_recovery_census.py | 2 | 08:42:07 | 32.66/32.51/31.02 | aliases-census |
| tests/fake/test_state_contract.py | 37 | 09:18:27 | 30.85/33.59/40.65 | guards |
| tests/fake/test_turn_retention_cwd.py | 6 | 09:19:14 | 31.30/33.14/40.11 | test_turn_retention_cwd |
| tests/fake/test_turn_retention_cwd_properties.py | 1 | 09:19:55 | 30.49/32.62/39.56 | test_turn_retention_cwd_properties |
| tests/fake/test_turn_retention_job_directory.py | 2 | 09:21:11 | 33.81/33.52/39.34 | test_turn_retention_job_directory |
| tests/fake/test_turn_retention_job_properties.py | 1 | 09:21:53 | 38.47/34.87/39.57 | test_turn_retention_job_properties |
| tests/fake/test_turn_retention_quarantine.py | 10 | 09:22:55 | 37.26/35.33/39.40 | test_turn_retention_quarantine |
| tests/unit/test_invariants_index.py | 25 | 09:18:27 | 30.85/33.59/40.65 | guards |
| tests/unit/test_retention_81_notes.py | 38 | 09:14:17 | 29.02/36.87/43.92 | notes |
| tests/unit/test_retention_alternates.py | 23 | 08:43:46 | 32.39/32.30/31.10 | dependencies |
| tests/unit/test_retention_alternates_review_r2.py | 15 | 08:42:18 | 33.52/32.70/31.11 | regressions-2 |
| tests/unit/test_retention_archive.py | 108 | 08:48:11 | 45.86/40.66/35.25 | archive, archive-resume-0, archive-resume-1, archive-resume-2, archive-worktree-path |
| tests/unit/test_retention_archive_properties.py | 2 | 08:57:42 | 69.65/62.87/49.98 | archive-properties |
| tests/unit/test_retention_dependency_aliases.py | 10 | 08:42:07 | 32.66/32.51/31.02 | aliases-census |
| tests/unit/test_retention_dependency_properties.py | 4 | 09:23:24 | 35.64/35.07/39.19 | dependency-property-0, dependency-property-1, dependency-property-2, dependency-property-3 |
| tests/unit/test_retention_dependency_units.py | 19 | 08:34:00 | 28.73/25.99/28.47 | linked-units |
| tests/unit/test_retention_folded_pin.py | 38 | 08:43:46 | 32.39/32.30/31.10 | dependencies |
| tests/unit/test_retention_job_dependencies.py | 12 | 08:43:46 | 32.39/32.30/31.10 | dependencies |
| tests/unit/test_retention_location_inputs.py | 1 | 08:33:14 | 26.90/25.52/28.48 | linked-location-inputs |
| tests/unit/test_retention_locations.py | 22 | 08:33:00 | 26.62/25.42/28.48 | linked-locations |
| tests/unit/test_retention_nested_leases.py | 27 | 09:01:01 | 46.13/56.15/50.11 | nested-leases |
| tests/unit/test_retention_pointer_inputs.py | 2 | 08:31:46 | 25.21/25.07/28.66 | linked-pointer |
| tests/unit/test_retention_pointer_whitespace.py | 9 | 08:38:08 | 29.19/30.06/29.80 | regressions-5 |
| tests/unit/test_retention_salvage_properties.py | 2 | 09:04:45 | 58.37/59.46/53.02 | salvage-properties, salvage-properties-second |
| tests/unit/test_retention_shared_folders.py | 62 | 09:12:56 | 34.78/39.88/45.58 | shared-folders, shared-property-0, shared-property-1, shared-property-2, shared-property-3 |
| tests/unit/test_retention_sweep_race.py | 13 | 09:07:48 | 32.68/50.06/50.58 | sweep-examples, sweep-property |
| tests/unit/test_test_module_names.py | 1 | 09:18:27 | 30.85/33.59/40.65 | guards |

New file, separately: `tests/fake/test_retention_dependency_review_r9.py` — 0 passed, 4 strict xfailed in its final ordinary marked hold at `2026-10-31T09:20:38.395822+00:00` (load 36.34/33.89/39.71); the [red hold](/Users/maxghenis/.subfleet/worktrees/20261031-043007-pr272-review-r9/review-r9-data/red-r9-firmlink-final.json) has four failures at the intended assertion. The file's counts are not folded into the 829 passing nodes.

Explicitly not run: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`, full suite, Python 3.12, the person-only `tests/frontend/test_core_live.py` probe, and remote CI. The hub verifies CI. No live store, real provider or real Subfleet CLI was used.

## Mutant table

All 29 round-named results reproduce: nine new kills, thirteen older kills at identical r7 assertions, seven old survivors. The seven double-removal trials add seven kills; R9-F1's two partial-repair trials add two. Total: 31 killed, seven surviving old named cases, and one positive trial. Fixtures and teardown pass in every final mutant/trial run.

| Mutant | Named killer | Result | Same r7 assertion |
|---|---|---|---|
| r2-post-read-identity-check-dropped | tests/fake/test_retention_dependency_review_r2.py::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace] | Survived | No — survived |
| r2-terminal-legacy-incomplete-dropped | tests/fake/test_retention_dependency_review_r2.py::test_c8_4_legacy_incomplete_census_survives_terminal_export[database] | Killed | Yes |
| r3-post-traversal-alias-check-dropped | tests/fake/test_retention_dependency_review_r3.py::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live] | Killed | Yes |
| r3-census-moved-after-recovery | tests/fake/test_retention_dependency_review_r3.py::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy] | Killed | Yes |
| r3-write-metadata-check-dropped | docs/reports/2026-10-31-retention-pins-fix-r7-data/probes.py::test_changes[same-size-running] | Killed | Yes |
| r3-final-path-check-dropped | docs/reports/2026-10-31-retention-pins-fix-r7-data/probes.py::test_changes[symlink-info-running] | Survived | No — survived |
| r3-terminal-census-live-only | tests/fake/test_retention_dependency_review_r2.py::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting] | Killed | Yes |
| r4-initial-objects-revalidation-dropped | tests/fake/test_retention_dependency_review_r4.py::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live] | Survived | No — survived |
| r4-initial-objects-recording-dropped | tests/fake/test_retention_dependency_review_r4.py::test_stable_objects_alias_keeps_the_tree_git_reads[live] | Survived | No — survived |
| r5-commondir-whitespace-stripping-restored | tests/fake/test_retention_dependency_review_r5.py::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-commondir-trailing] | Killed | Yes |
| r5-gitfile-whitespace-stripping-restored | tests/fake/test_retention_dependency_review_r5.py::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-gitfile-leading] | Killed | Yes |
| r5-gitfile-colon-without-space-accepted | tests/unit/test_retention_pointer_whitespace.py::test_pointer_syntax_and_whitespace_match_real_git[gitfile-no-space] | Killed | Yes |
| r6-empty-common-initial-objects-not-recorded | tests/fake/test_retention_dependency_review_r6.py::test_empty_common_target_keeps_the_own_store_git_reads[directory-live-control-lf] | Survived | No — survived |
| r6-pointer-read-failure-evidence-dropped | tests/fake/test_retention_dependency_review_r6.py::test_crlf_padded_pointer_keeps_the_storage_real_git_reads[padded-gitfile] | Killed | Yes |
| r6-parent-common-fallback-restored | tests/fake/test_retention_dependency_review_r6.py::test_empty_common_target_keeps_the_own_store_git_reads[named-live-lf] | Survived | No — survived |
| r6-dangling-pointer-treated-as-missing | tests/fake/test_retention_pointer_failures.py::test_unreadable_common_pointer_keeps_allocated_trees[dangling] | Killed | Yes |
| r6-oversized-pointer-treated-as-missing | tests/fake/test_retention_pointer_failures.py::test_unreadable_common_pointer_keeps_allocated_trees[oversized] | Killed | Yes |
| r7-unused-parent-pointer-probing-restored | tests/fake/test_retention_dependency_review_r7.py::test_unused_parent_pointer_must_not_globally_pin_trees | Killed | Yes |
| r7-own-objects-revalidation-dropped | tests/fake/test_retention_dependency_review_r7.py::test_named_own_objects_swap_keeps_actual_storage | Survived | No — survived |
| r7-parent-objects-recording-dropped | tests/fake/test_retention_parent_hint.py::test_inferred_parent_objects_keeps_its_resolved_storage | Killed | Yes |
| r8-alias-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[admin-path] | Killed | New |
| r8-pointer-file-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[gitfile] | Killed | New |
| r8-intermediate-link-walk-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[intermediate-HEAD] | Killed | New |
| r8-lexical-dotdot | tests/unit/test_retention_locations.py::test_dotdot_applies_after_the_link_before_it | Killed | New |
| r8-link-location-followed | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[gitfile-target-HEAD] | Killed | New |
| r8-top-level-links-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[git-entry-HEAD] | Killed | New |
| r8-parent-hint-scanned | tests/unit/test_retention_locations.py::test_inferred_parent_is_not_scanned | Killed | New |
| r8-walk-revalidation-dropped | tests/unit/test_retention_locations.py::test_changed_link_chain_with_the_same_final_target_is_incomplete | Killed | New |
| r8-routine-skip-dropped | tests/unit/test_retention_locations.py::test_routine_records_are_exactly_unchanged[worktree] | Killed | New |
| double-r2-post-read-identity-check-dropped | tests/fake/test_retention_dependency_review_r2.py::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace] | Killed | — |
| double-r3-final-path-check-dropped | docs/reports/2026-10-31-retention-pins-fix-r7-data/probes.py::test_changes[symlink-info-running] | Killed | — |
| double-r4-initial-objects-revalidation-dropped | tests/fake/test_retention_dependency_review_r4.py::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live] | Killed | — |
| double-r4-initial-objects-recording-dropped | tests/fake/test_retention_dependency_review_r4.py::test_stable_objects_alias_keeps_the_tree_git_reads[live] | Killed | — |
| double-r6-empty-common-initial-objects-not-recorded | tests/fake/test_retention_dependency_review_r6.py::test_empty_common_target_keeps_the_own_store_git_reads[directory-live-control-lf] | Killed | — |
| double-r6-parent-common-fallback-restored | tests/fake/test_retention_dependency_review_r6.py::test_empty_common_target_keeps_the_own_store_git_reads[named-live-lf] | Killed | — |
| double-r7-own-objects-revalidation-dropped | tests/fake/test_retention_dependency_review_r7.py::test_named_own_objects_swap_keeps_actual_storage | Killed | — |
| r9-canonical-auxiliary-trial | tests/fake/test_retention_dependency_review_r9.py; tests/unit/test_retention_locations.py::test_routine_records_are_exactly_unchanged | Positive trial | — |
| r9-file-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[gitfile] | Killed | — |
| r9-scanned-target-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[objects-pack] | Killed | — |

## Text and PR-body lines

C-8.3/C-8.4 and the full acceptance-contract file are byte-identical. C-8.3's export/census behavior passes. C-8.4's canonical-spelling requirement is violated by R9-F1; keep the requirement and repair discovery rather than weaken it.

The current repair-closure lead in the PR body, main report and fix report needs this qualification pending repair:

> Fix round r8 passes the original canonical-path R8-F1 regressions. Review r9 at a3b9cc246 requests changes for R9-F1: physical pointer files and scanned targets reached through macOS Data-volume aliases are recorded under noncanonical spellings, so complete discovery can still let retention retire a running borrower's required tree. The five prescribed routine controls, class counts, reversion results and named-mutant accounting remain verified.

Add the same current-status qualification to the desktop discovery description until repaired. In docstrings, qualify the general “spelled once” description for auxiliary records at this head: storage directories are canonicalized, but physical files and scanned targets currently retain the walk's spelling (R9-F1). The in-memory fix restores the intended canonical behavior. Separately replace the old outside-checkout sentence with the D-R8-2 wording above.

Replace the PR's current lead/evidence/Next blocks with the following; preserve earlier rounds as dated history:

**Review r9 at `a3b9cc246` requests changes for R9-F1 (P2).** The original canonical-path R8-F1 regressions and the earlier R6/R7 regressions pass. Auxiliary physical-file locations and scanned targets still retain macOS Data-volume spellings, while allocated trees use canonical spellings. Four healthy real-Git inputs record complete evidence, lose their required owner tree during retention, and then fail the identical read. Canonicalize those auxiliary locations outside transactions while preserving physical symlink-entry names. Retention stays dormant pending repair, approving rereview and completed CI.

- **Review r9 evidence:** all 745 requested nodes and 84 independent controls pass; four new strict expected failures name R9-F1. All nine r8 mutants die; thirteen old mutants die at their same r7 assertions; seven survive and each replacement guard is demonstrated by a failing double-removal trial. Class and generated-only reversion tables match the round exactly. An in-memory three-site canonicalization trial passes the four new cases and five routine controls; independently dropping either repair category fails its matching case.
- **Reader boundaries:** keep D-R6-1 through D-R6-4, D-R7-1 and D-R8-1 as listed differences. D-R8-2 describes auxiliary unused-link records: broad ancestor targets do not pin their contained trees, and complete Git pins end with the job. R9-F1 follows the covered pointer-file and direct-entry routes and requires repair.
- **Next:** canonicalize successful physical file locations and resolved scan targets; preserve the four new regressions and existing passing tests; obtain approving rereview and the hub's all-pass CI/mergeability checks.

## Retained files and cleanup

The requested macOS scratch folder was denied, so this review used `/private/tmp/sf-r272r9`. Final measured scratch was 557.59 MiB, under 1.5 GB; worktree fixtures were 3.70 MiB. Guarded cleanup completed at `2026-10-31T09:40:54.915240+00:00`. Scratch, worktree fixtures, bytecode, `.hypothesis` and build outputs are absent. Every owned test/tool session completed; no run was detached.

Retained review artifacts are `review-r9.md`, `review-r9-data/` and `tests/fake/test_retention_dependency_review_r9.py`. No product source changed. No commit, push, merge or publication was made; the review-only rule prohibits commits. The new regression remains in the worktree for the hub.

DONE
