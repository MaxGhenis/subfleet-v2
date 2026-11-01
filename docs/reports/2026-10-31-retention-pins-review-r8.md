VERDICT #272: REQUEST_CHANGES

Reviewed `382a6c05ce9816b7c65f9d36551fa0949a8924d7`, whole branch against the supplied `release/217` at `1f60a368f`. R6-F1, R6-F2 and R7-F1 are repaired on their original inputs. **R8-F1 is a P2 and holds this verdict under the age-independent P2 exception.**

## Finding

**R8-F1 — P2: resolved storage omits required pointer-file and alias locations.** `subfleet/dependencies.py:142–147`, `_Aliases.text` at line 189, and `_gitfile` at line 220. Discovery revalidates aliases during its traversal, but only the resolved gitdir/common/objects/alternate targets feed the retention pins. A physical pointer file or a required symlink entry can therefore live in an allocated tree without that tree appearing in the record.

Two executed inputs use an outside checkout and a healthy external Git repository containing the owner's commit:

1. The outside checkout's regular `.git` file names `<owner-tree>/storage-alias`, a symlink to the external gitdir.
2. The outside checkout's `.git` is a symlink to a **regular pointer file physically inside the owner tree**. That file names the external gitdir.

For both inputs, real Git 2.55.0 returns 0 for `fsck --no-dangling`, clean `status --porcelain`, and `show <owner-commit>:owner-only.txt`. Discovery has **no incomplete evidence** and records only the external gitdir and objects. A running borrower is recorded with those dependencies. Retention prunes `owner`; the identical Git read then returns **128**. There is no external mutation, failed setup or incomplete read in this input.

Both forms reproduce at **base `b2a357b6c` and head**. The base comparison loads the committed base discovery module under retention code that is byte-identical to base; HEAD is never moved. The base dependency source is byte-identical to `beabed1f7`; this is older than both fix rounds. It nevertheless meets the round's P2 definition: a running job reads metadata in the tree retention retires. In the second form the dependency is a regular file in that tree, rather than merely an external object-store target. [Four executed inputs](2026-10-31-retention-pins-review-r8-data/R8-F1-inputs.json) preserve Git outcomes, complete records and retirement results.

**Fix:** keep the physical locations of metadata files Git reads and the containing directories of required symlink entries, including intermediate components, as dependencies; otherwise report incomplete evidence when those dependencies cannot be established. Canonicalizing the symlink pathname itself loses the location of the required directory entry. Revalidating an alias only during discovery does not prevent retention from deleting it afterward.

The new `tests/fake/test_retention_dependency_review_r8.py` contains two strict expected failures naming R8-F1. Both fail at the owner-retention assertion with `--runxfail`, after successful real-Git setup. A narrowly scoped in-memory trial that records pointer-file locations and direct alias-entry parents passes both. Dropping either trial addition kills its corresponding named test. The trial is evidence for these two inputs, **not a complete repair for intermediate symlink chains**. No product repair was written to the branch.

## Earlier findings and checks 1–6

| Check | Verdict and executed evidence |
|---|---|
| 1. R6-F1, R6-F2, R7-F1 | **PASS.** All 16 r6 cases pass: the three named-live absent/LF/CRLF own-store failures and both padded-pointer failures, plus their controls. All three r7 cases pass: valid repository, vanished target with unrelated parent `commondir` directory, and own-objects swap. All five preserved valid-parent controls pass. |
| 2. Repair consequences | **REQUEST_CHANGES for R8-F1.** Plain, worktree-add, shared-clone and no-`.git` inputs retire the unrelated owner at base and head without incomplete evidence. Healthy named admins with unused ordinary/no-objects parents also retire at both revisions. Direct parent Git reads succeed through a parent's own `commondir` and through a real linked admin; the borrower's absent/LF/CRLF own-common layouts do not inherit either pointer. Those rejected borrower inputs establish no missed readable store. Real missing, dangling, directory, permission, oversized and NotADirectory inputs were compared with Git. Same-inode pointer rewrites, final TooLarge and final unreadable seams report incomplete evidence. Ended recorded/legacy jobs with gone targets retire. D-R7-1 reproduces at base and head. The stable alias/pointer-file inputs above expose the remaining P2. |
| 3. Earlier findings | **PASS on focused regressions.** R3 file 6, R4 file 53, R5 file 60; T2 passes its five explicit and 40 generated inputs. Three old-journal probes confirm R4-F2's wording: locked journals persist the upgrade before quarantine; later phases can complete without persisted `job_folder`. Remaining requested file totals are in the final ledger below. |
| 4. Marks and mutants | **PASS.** All twenty requested mutants and three added checks are killed, with setup/teardown passing. All seventeen older assertions match r6. Source audit: only the marks covering the five r6 failures and the r7 parent-pointer regression were removed; all other bytes match. The original property prefix and the acceptance contract are unchanged. The complete named replay is recorded below. |
| 5. Properties | **PASS for the stated class counts, with bounded detection.** All six generated-only reversions ran; exact results and limitations follow. No claimed generated input class is missing. The focused property constructs R7-F1 inputs but its oracle accepts their false incomplete evidence. The named r7 regression detects that error. Neither strategy constructs R8-F1's metadata placement. |
| 6. Text | C-8.3/C-8.4 and the named older readers are byte-identical to base. Both fix reports accurately describe their dated executions; their 947/475 unique pass totals recompute from the committed ledgers. The main report explicitly calls review r7 partial, with no verdict. The class tables reproduce. Current status must now identify R8-F1, and the PR's “all seven checks” scope sentence must say six checks with CI read by the hub. Exact replacements follow. |

[Source/mark audit](2026-10-31-retention-pins-review-r8-data/source-audit.json), [independent inputs](2026-10-31-retention-pins-review-r8-data/independent-inputs.json), [age hashes](2026-10-31-retention-pins-review-r8-data/dependency-age-hashes.json), [historical count audit](2026-10-31-retention-pins-review-r8-data/historical-count-audit.json).

## Check 5: generated-only reversions

Scratch copies retain the test module/function names, strategies, `max_examples=100/40`, `derandomize=True` and `database=None`. Only the individual repair is reverted and the original property's 15 `@example` decorators removed. Function AST bodies and Hypothesis function digests match head. Observation wrappers do not modify those bodies or settings. The successful generated-only vectors exactly match head's generated vectors.

| Repair reverted alone | Property | Generated failure? | First failing input / reason for none |
|---|---|---|---|
| R6-F1 | Original /100 | **Yes**, first failure attempt 43 | `layout=gitfile, prefix=present, target=padded, common=absent, own=symlink, named=true, padding=over, pad_file=commondir`. Padding is inactive because commondir is absent. Git store/read both return 0; actual storage is omitted without incomplete evidence. |
| R6-F1 | Focused /40 | **Yes**, first input | `slot=0, common=absent, own=plain, parent=absent`. Git store/read return 0; the own store is omitted. |
| R6-F2 | Original /100 | **Yes**, first failure attempt 12 | `layout=gitfile, prefix=colon-only, target=blank, common=empty, own=symlink, named=false, padding=over, pad_file=gitfile`. Git rejects (128/128), but the attempted oversized read loses required incomplete evidence. Generated accepted-read failures also occur, first with `layout=gitfile, prefix=present, target=spaces, common=target, own=alternate, named=true, padding=over, pad_file=commondir`. |
| R6-F2 | Focused /40 | **No**, all 40 pass | `NAMED_OWN_INPUTS` at lines 148–153 has no padding selector; construction at lines 176/188 emits only bounded pointers. |
| R7-F1 | Original /100 | **No**, all 100 pass | `INPUTS` at lines 23–32 has no parent-metadata selector; `check_input` never creates the unrelated `container/commondir` directory. |
| R7-F1 | Focused /40 | **No**, all 40 pass | The strategy **does** reach the unrelated-parent class eight times. Assertions at lines 198/201 accept incomplete evidence, so all eight false global pins pass the oracle. This is an oracle blind spot, not missing generated inputs. |

Head executes **115 original inputs: 43 owner reads / 72 rejections**, with 32 complete successful reads, 11 incomplete successful reads and 13 incomplete rejections. The focused property executes **40 successful reads, all complete**. Both use real Git's object-path output and commit-content reads; discovery's own interpretation is not the Git oracle. Rejected inputs require deterministic absolute records and incomplete evidence for attempted over-bound reads.

| Dimension | Original /100 generated | Focused /40 generated |
|---|---|---|
| Layout | Gitfile 52; directory 48 | Gitfile 40 |
| Prefix | Present 28; absent 27; misspelled 25; colon-only 20 | Present 40 |
| Target | Normal 27; spaces 24; blank 28; padded 21 | Normal 40 |
| Commondir | Absent 23; empty 20; LF 24; CRLF 13; target 20 | Absent 15; LF 10; CRLF 15 |
| Own store | Absent 36; symlink 35; alternates 29 | Plain 12; symlink 12; alternates 16 |
| Named location | True 47; false 53 | True 40 |
| Padding | Short 39; under 23; over 38 | Short 40 |
| Padded file | Gitfile 55; commondir 45 | Gitfile 40 |
| Parent | Not drawn | Absent metadata 10; ordinary 6; repository 16; unrelated commondir 8 |

| Commondir | Original absent / symlink / alternates | Focused plain / symlink / alternates |
|---|---|---|
| Absent | 8 / 11 / 4 | 6 / 3 / 6 |
| Empty | 5 / 8 / 7 | — |
| LF-only | 8 / 8 / 8 | 1 / 6 / 3 |
| CRLF-only | 8 / 4 / 1 | 5 / 3 / 7 |
| Target | 7 / 4 / 9 | — |

Every focused common/own combination appears. Original readable named gitfiles still draw absent/LF/CRLF **2/0/1**, with the CRLF case masked by an oversized gitfile. Thus the report's explicit thin-spot qualification is accurate. [Full counts, first failures, settings and vector comparison](2026-10-31-retention-pins-review-r8-data/coverage-results.json).

## Listed differences

These older differences remain below P2 on their executed inputs and do not hold the verdict. R8-F1 is separately ranked P2 above.

| Difference | Executed input and result | One line for the doc |
|---|---|---|
| D-R6-1, P3 | Space-suffixed admin, with/without a trimmed twin; common alias with pathname spaces; physical spaced common directory. Archive/survey miss/select a trimmed admin, while the physical common control round-trips. | Outside discovery, archive and survey retain legacy whitespace parsing: some valid spaced admin/common pointer spellings are missed or select a trimmed twin. |
| D-R6-2, P3 | Nested gitfiles naming the enclosing admin plus space or tab are classified internal despite naming external storage. No live borrower in this input. | Nested gitfile classification still strips target bytes and can treat a whitespace-suffixed external admin as internal. |
| D-R6-3, P3 | Valid `common ` and `common` registrations: cleanup deletes the trimmed twin and leaves the actual registration. | Registration cleanup still strips common-directory output and can act on a whitespace target's trimmed twin. |
| D-R6-4, observation | Self-contained bundle verifies despite an incorrectly stripped alternate; no incorrect verification pass is established. | Bundle verification still strips common targets when constructing its alternate; a complete pack can verify without using that alternate. |
| D-R7-1, older conservative pin | Healthy named own-store checkout with one 1,048,577-byte alternates line in its unused parent. Git fsck/read succeed; base and head globally pin an unrelated tree. None of the four routine layouts uses this malformed unused-parent input. | An unused inferred parent's over-bound alternates can still globally pin unrelated trees even when Git uses only the named admin's own store. |

## Holds and mutants

Job start **05:19 UTC October 31**. First pytest **05:21:11 UTC**, load **24.29/18.59/20.21**. New-hold cutoff **09:19 UTC**, wall **11:19 UTC**. Python 3.14.7, Git 2.55.0 and the locked dev dependencies are used. Every pytest is serial, with a 480-second interruption guard and at most four concurrent owned test processes.

All dates below are **October 31 UTC**. Counts are unique passing nodes; each start/load is the first successful ordinary hold for that file. The archive row combines the initial 102 passes and six worktree-temp reruns.

| File | Passed / expected | First successful start UTC | Load 1/5/15 |
|---|---:|---|---|
| tests/fake/test_retention_dependency_review_r7.py | 3 / 3 | 05:21:11 | 24.29/18.59/20.21 |
| tests/fake/test_retention_parent_hint.py | 2 / 2 | 05:21:11 | 24.29/18.59/20.21 |
| tests/fake/test_retention_dependency_review_r6.py | 16 / 16 | 05:21:11 | 24.29/18.59/20.21 |
| tests/fake/test_retention_pointer_failures.py | 7 / 7 | 05:21:11 | 24.29/18.59/20.21 |
| tests/unit/test_retention_pointer_inputs.py | 2 / 2 | 05:50:35 | 28.71/31.17/30.24 |
| tests/fake/test_retention_dependency_review_r5.py | 60 / 60 | 05:51:22 | 26.70/30.22/29.94 |
| tests/unit/test_retention_pointer_whitespace.py | 9 / 9 | 05:51:22 | 26.70/30.22/29.94 |
| tests/fake/test_retention_dependency_review_r4.py | 53 / 53 | 05:53:12 | 25.48/28.48/29.27 |
| tests/fake/test_retention_dependency_review_r3.py | 6 / 6 | 05:53:12 | 25.48/28.48/29.27 |
| tests/unit/test_retention_dependency_aliases.py | 10 / 10 | 06:25:46 | 36.95/36.45/34.33 |
| tests/fake/test_retention_recovery_census.py | 2 / 2 | 06:25:46 | 36.95/36.45/34.33 |
| tests/fake/test_turn_retention_cwd.py | 6 / 6 | 05:22:52 | 36.46/24.05/22.11 |
| tests/fake/test_turn_retention_cwd_properties.py | 1 / 1 | 05:55:55 | 32.27/29.56/29.55 |
| tests/fake/test_turn_retention_job_directory.py | 2 / 2 | 05:22:52 | 36.46/24.05/22.11 |
| tests/fake/test_turn_retention_job_properties.py | 1 / 1 | 06:31:12 | 34.67/33.77/33.60 |
| tests/fake/test_turn_retention_quarantine.py | 10 / 10 | 05:22:52 | 36.46/24.05/22.11 |
| tests/fake/test_retention_dependency_review_r2.py | 21 / 21 | 06:35:35 | 37.44/36.05/34.68 |
| tests/unit/test_retention_alternates_review_r2.py | 15 / 15 | 06:35:35 | 37.44/36.05/34.68 |
| tests/fake/test_retention_dependency_review_r1.py | 18 / 18 | 06:36:55 | 35.55/35.67/34.65 |
| tests/fake/test_retention_dependency_incomplete.py | 19 / 19 | 06:36:55 | 35.55/35.67/34.65 |
| tests/unit/test_retention_alternates.py | 23 / 23 | 06:38:25 | 37.94/36.48/35.07 |
| tests/unit/test_retention_job_dependencies.py | 12 / 12 | 06:38:25 | 37.94/36.48/35.07 |
| tests/fake/test_retention_dependencies.py | 30 / 30 | 06:38:25 | 37.94/36.48/35.07 |
| tests/fake/test_retention_dependency_spelling.py | 16 / 16 | 06:38:25 | 37.94/36.48/35.07 |
| tests/unit/test_retention_dependency_units.py | 19 / 19 | 06:38:25 | 37.94/36.48/35.07 |
| tests/unit/test_retention_folded_pin.py | 38 / 38 | 06:38:25 | 37.94/36.48/35.07 |
| tests/unit/test_retention_dependency_properties.py | 4 / 4 | 05:56:37 | 33.65/30.22/29.78 |
| tests/unit/test_retention_shared_folders.py | 62 / 62 | 06:07:27 | 32.33/33.35/32.09 |
| tests/unit/test_retention_archive.py | 108 / 108 | 05:24:36 | 34.07/27.94/23.92 |
| tests/unit/test_retention_archive_properties.py | 2 / 2 | 05:30:44 | 27.88/29.12/26.12 |
| tests/unit/test_retention_nested_leases.py | 27 / 27 | 05:32:09 | 34.29/31.20/27.22 |
| tests/unit/test_retention_salvage_properties.py | 2 / 2 | 05:39:55 | 32.61/32.50/29.65 |
| tests/unit/test_retention_sweep_race.py | 13 / 13 | 05:50:35 | 28.71/31.17/30.24 |
| tests/unit/test_retention_81_notes.py | 38 / 38 | 05:54:38 | 27.12/28.52/29.23 |
| tests/fake/test_state_contract.py | 37 / 37 | 06:44:00 | 39.17/36.93/35.60 |
| tests/unit/test_test_module_names.py | 1 / 1 | 06:44:00 | 39.17/36.93/35.60 |
| tests/unit/test_invariants_index.py | 25 / 25 | 06:44:00 | 39.17/36.93/35.60 |

The added fake r8 file gives **0 passes / 2 strict expected failures** at **06:25:46 UTC**, load **36.95/36.45/34.33**, in the alias/census hold. The independent data-file controls give **59 unique passes**: pointer matrix 5, journal probes 3, older reader probes 8, preserved alternates-change controls 2, and new r8 probes 41. Their main hold begins **06:18:11 UTC**, load **34.38/32.37/32.12**; the corrected dangling probe passes separately.

All **720 requested nodes pass**; no requested local node remains unrun. There are **63 completed pytest runs**, **59 under the lock and four prescribed direct fallbacks**. Peak observed pytest concurrency is **2**; peak recorded RSS is **338.78 MiB**; longest run is **363.32 seconds** including configuration/teardown. No interruption guard fires and no pytest exceeds ten minutes. Explicit/generated sweep phases preserve all 15/40 inputs. The 400-example nested fence property completes unchanged in 305.27 pytest seconds.

| Direct fallback | Start UTC | Load 1/5/15 |
|---|---|---|
| turn-cwd-property | 05:55:55 | 32.27 / 29.56 / 29.55 |
| historical-controls | 06:18:11 | 34.38 / 32.37 / 32.12 |
| aliases-census | 06:25:46 | 36.95 / 36.45 / 34.33 |
| turn-job-property | 06:31:12 | 34.67 / 33.77 / 33.60 |

**Mutant table.** R2–R8 denote the corresponding fake review files, W the pointer-whitespace unit file, PF pointer-failures, PH parent-hint, P the preserved fix-r7 probes, and R8P this review's probes. Every named test fails in its call phase, with successful setup and teardown and a verified imported-source mutation. The r7 recursive-parent name kills both parameterized inputs. The last two location mutants use the explicit two-input positive trial baseline; they are not claimed as head-source repairs.

| Mutant | Named killer | Result |
|---|---|---|
| r2-post-read-identity-check-dropped | `R2::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace]` | Killed; same r6 assertion |
| r2-terminal-legacy-incomplete-dropped | `R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[database]` | Killed; same r6 assertion |
| r3-post-traversal-alias-check-dropped | `R3::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live]` | Killed; same r6 assertion |
| r3-census-moved-after-recovery | `R3::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy]` | Killed; same r6 assertion |
| r3-write-metadata-check-dropped | `P::test_changes[same-size-running]` | Killed; same r6 assertion |
| r3-final-path-check-dropped | `P::test_changes[symlink-info-running]` | Killed; same r6 assertion |
| r3-terminal-census-live-only | `R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting]` | Killed; same r6 assertion |
| r4-initial-objects-revalidation-dropped | `R4::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live]` | Killed; same r6 assertion |
| r4-initial-objects-recording-dropped | `R4::test_stable_objects_alias_keeps_the_tree_git_reads[live]` | Killed; same r6 assertion |
| r5-commondir-whitespace-stripping-restored | `R5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-commondir-trailing]` | Killed; same r6 assertion |
| r5-gitfile-whitespace-stripping-restored | `R5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-gitfile-leading]` | Killed; same r6 assertion |
| r5-gitfile-colon-without-space-accepted | `W::test_pointer_syntax_and_whitespace_match_real_git[gitfile-no-space]` | Killed; same r6 assertion |
| r6-empty-common-initial-objects-not-recorded | `R6::test_empty_common_target_keeps_the_own_store_git_reads[directory-live-control-lf]` | Killed; same r6 assertion |
| r6-pointer-read-failure-evidence-dropped | `R6::test_crlf_padded_pointer_keeps_the_storage_real_git_reads[padded-gitfile]` | Killed; same r6 assertion |
| r6-parent-common-fallback-restored | `R6::test_empty_common_target_keeps_the_own_store_git_reads[named-live-lf]` | Killed; same r6 assertion |
| r6-dangling-pointer-treated-as-missing | `PF::test_unreadable_common_pointer_keeps_allocated_trees[dangling]` | Killed; same r6 assertion |
| r6-oversized-pointer-treated-as-missing | `PF::test_unreadable_common_pointer_keeps_allocated_trees[oversized]` | Killed; same r6 assertion |
| r7-unused-parent-pointer-probing-restored | `R7::test_unused_parent_pointer_must_not_globally_pin_trees` | Killed |
| r7-own-objects-revalidation-dropped | `R7::test_named_own_objects_swap_keeps_actual_storage` | Killed |
| r7-parent-objects-recording-dropped | `PH::test_inferred_parent_objects_keeps_its_resolved_storage` | Killed |
| r8-final-pointer-content-check-dropped | `R8P::test_final_pointer_failure_cannot_hide_changed_storage[rewrite]` | Killed |
| r8-alias-location-recording-dropped | `R8::test_running_job_keeps_tree_containing_required_git_pointer[admin-path]` | Killed on positive trial baseline |
| r8-pointer-file-location-recording-dropped | `R8::test_running_job_keeps_tree_containing_required_git_pointer[gitfile]` | Killed on positive trial baseline |

[Complete ledger](2026-10-31-retention-pins-review-r8-data/ledger.json), [mutant assertion audit](2026-10-31-retention-pins-review-r8-data/mutant-audit.json), [run metrics](2026-10-31-retention-pins-review-r8-data/run-metrics.json).

The initial archive hold gives **102 passed / six failed / one deselected**. The six named failures are the documented temp-path trap and all **six pass** with TMPDIR in the new worktree folder `sf-r272r8-fixtures`. They are environment failures, not findings. [Exact names and original failures](2026-10-31-retention-pins-review-r8-data/archive-path-trap.json).

The independent dangling-commondir probe initially expected Git to accept the link; actual Git rejects it with failed-to-read ENOENT. Discovery correctly reports incomplete. Correcting that probe expectation gives an isolated pass; product code is unchanged.

The sandbox denied the Darwin scratch folder, so `/private/tmp/sf-r272r8` is used. Three early queued drivers encountered denied `ps` at their 30-minute fallback, before pytest. Their exact owned waiters were checked/stopped and the selections resumed directly. A retry-condition bug then interrupted repeated T2 initialization attempts before pytest started. The runner was corrected; the actual T2 run began **06:31:12.886 UTC**, load **34.67/33.77/33.60**, and passed. The final ledger counts actual pytest runs, excluding those initializations. No product assertion failed in them. The suite wrapper's denied sysctl calls are replaced only in evidence collection by `os.getloadavg`; the prescribed wrapper itself is unchanged.

Not run by design: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree`, the full suite, Python 3.12, external CI and person-only `tests/frontend/test_core_live.py`. The hub reads CI; no shared-repository fetch or merge, GitHub call, push or publication is performed. Git fixture transfers stay local.

## Text and PR-body replacements

Keep C-8.3 and C-8.4 unchanged. Add this current-status qualification to the main report and desktop discovery description until the code repair lands:

> R6-F1, R6-F2 and R7-F1 pass their original inputs at 382a6c05c. Review r8 identifies R8-F1: discovery records resolved storage but omits required physical pointer-file and alias-entry locations. A running job can lose those locations when retention retires another job's tree, despite complete discovery and successful real-Git reads beforehand. Retention remains dormant pending repair, approving rereview and completed CI.

Replace the PR's first lead paragraph with:

> Review r8 at 382a6c05c requests changes for R8-F1 (P2). R6-F1, R6-F2 and R7-F1 pass their original regressions. A running borrower's Git pointer file or gitdir alias can live in another job's tree while discovery records only the resolved external storage. Retention retires that tree without incomplete evidence; healthy real-Git reads then fail. Both stable inputs reproduce before fix r6 and at head. Retention stays dormant pending repair, approving rereview and completed CI.

Replace the PR's evidence paragraph with:

> Review r8: all 720 requested nodes and 59 independent controls pass; two strict expected failures name R8-F1; twenty requested mutants and three added checks are killed. The 100 generated plus 15 explicit original inputs and 40 focused inputs reproduce the reported class counts. Generated-only reversions detect R6-F1 in both properties and R6-F2 in the original; neither property detects R7-F1's false global pin, which the named r7 regression covers. The two-input metadata-location trial passes, but a complete repair remains required.

Replace PR-body line 3's final sentence with:

> Review r8 reruns the six checks in its brief; the hub verifies CI.

Replace line 5 and `Next` item 1 at line 77 with:

> Repair R8-F1, preserve the passing r6/r7 regressions, obtain approving rereview and complete every CI shard before merge or re-enablement.

The dated fix-r6/fix-r7 evidence need not be rewritten as though these alias inputs ran in those rounds. Their generated-count and partial-review descriptions are accurate.

## Delivery state

No product file was changed. The added regression remains unstaged under the review's no-commit rule and read-only shared Git metadata. All review drivers and test processes have ended. At **06:52:47 UTC**, guarded cleanup removes both `/private/tmp/sf-r272r8` and the worktree's `sf-r272r8-fixtures` folder after making owned directories removable without following symlinks. Main scratch is **90,852 KiB (88.72 MiB)** immediately beforehand; the largest sampled rounded `du` result during the job is **203 MiB**, below 1.5 GB. No owned bytecode, build, Hypothesis or pytest cache remains. [Cleanup verification](2026-10-31-retention-pins-review-r8-data/cleanup.json) records removal.

Deliverables left in the worktree: this report, its data folder, and `tests/fake/test_retention_dependency_review_r8.py`. Shared Git metadata is read-only and the review forbids commits, so the test remains unstaged. HEAD remains `382a6c05ce9816b7c65f9d36551fa0949a8924d7`.

DONE.
