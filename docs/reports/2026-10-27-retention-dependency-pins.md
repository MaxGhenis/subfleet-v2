# Retention dependency pins: partial integration, 2026-10-27

**Not ready to land.** Code checkpoint `6997e21273687ab2afdb340f2132f14282e42225` integrates release/217 at `c9430763f` and fixes the compatibility issues found so far. Wider step 2 validation is incomplete. Steps 3–7 have not been completed. No new pin-query timings or mutation kills are claimed.

The lane started at approximately 14:51Z; new holds/steps stop at 17:49:00Z, conservatively before its three-hour start cutoff. Lock waits count toward that wall. The final update is 2026-10-27T18:08:30Z.

## Continuation 1 recovery, 2026-10-28 (in progress)

The preserved replacement attempt started at 06:56Z. This resumed process started at **07:47Z**; it stops starting holds and steps at **11:46Z**. The lost job's 27 salvaged files were verified byte for byte (the original node list is preserved separately). Its `c1-reconstruct.py` correctly reproduces **761** unfinished nodes from **1,104** unique planned nodes and **343** completed planned nodes. `c1b-reconstruct.py` independently verifies every saved plan against pytest's saved collection and keyword matcher. Two additional complete salvaged results leave **759** nodes in `remaining-nodes.txt`; `c1-original-remaining-nodes.txt` preserves the original 761. Lost batches 01 and 03 contain only setup results and count as incomplete.

The preserved replacement attempt completed another **27** nodes in three holds (20 + 6 + 1). Their complete setup/call/teardown evidence is reused. This process therefore starts with **732** unfinished nodes, saved in `c1c-start-remaining.txt`. Its batches and foreground driver resume only nodes without complete successful results.

At unchanged code checkpoint `6ca8f4b0bc36`, the salvage also proves **80 passed, 3 skipped** for dependency units, all three guards and provider shell environment, and **37 passed** for dependency fakes and daemon launch. These are reused. All three provider shell cases were collected and skipped with real provider executables absent from PATH; fake daemon-launch cases executed. No real provider, CLI, store, fetch, push or timing run is used. Each hold has one serial pytest, with at most four such workers, its own lock request, complete phase results, admission/end loads and peak Python RSS. An interrupted node is never counted as complete.

Shared metadata rejects `index.lock` with `Operation not permitted`. The preserved bundle's recovery checkpoint **`4319e7b0c`** is retained unchanged as an ancestor. Further ordered commits use private scratch Git metadata on `fix/retention-dependency-pins-217`; the workspace's `.git` file is unchanged. The delivery bundle is `rdp217c1.bundle`. This process owns only `sf-rdp217c1/recovery-0747` within the named scratch folder and will remove that subtree with guarded cleanup. Its initial scratch footprint was approximately 23 MB.

| Continuation hold | Result | Lock | Admission load | Pytest time |
|---|---|---|---:|---:|
| c1 core-units | 80 passed, 3 skipped | locked | 115.3 | see salvaged log |
| c1 core-fake | 37 passed | locked | 110.9 | see salvaged log |
| c1 remaining-02 | 1 passed | locked | 62.7 | see salvaged log |
| c1 remaining-04 | 1 passed | outside after 30-minute wait | 115.1 | see salvaged log |
| c1b remaining-01 | 20 passed | locked | 50.3 | 158.1 s admission-to-finish |
| c1b remaining-02 | 6 passed | locked | 122.7 | 107.5 s admission-to-finish |
| c1b remaining-03 | 1 passed | locked | 136.9 | 274.2 s admission-to-finish |
| c1c remaining-02 | 50 passed | locked | 104.6 | 262.39 s |

The JSONL hold ledgers and matching collection/results/log files in the data folder are the detailed evidence. All other continuation holds remain in progress or unstarted at this checkpoint.

### Resumed coverage checkpoint, 09:36Z (still step 2)

This process has complete successful phase evidence for **506** of its 732 starting nodes; **226** remain. Finished and interrupted holds are distinguished in `c1c-accounting.json`; `c1c-current-remaining.txt` is the exact continuation list. Release-comparison results never count toward branch coverage.

Integration findings so far (corrections prepared in the working tree; their green reruns are pending):

- The detached-fence oracle omitted recorded dependencies. Its red hold produced **49 passed, 1 failed** at load **93.1**, outside the lock after its 30-minute wait. It now compares durable dependency evidence independently and retains both discovered external-Git-storage counterexamples explicitly.
- Its first rerun failed `no-slot` once the shared fixture's readings aged beyond the normal **120 seconds**. The original test at detached release **`46ab44d72` passed (1 passed, 81.28 s, load 23.5)**, so this is **not classified as a pre-existing failure**. The correction refreshes fixture readings per example and leaves the production freshness bound unchanged. The release worktree was removed after the comparison.
- Three old `_fence_hold` stubs need to accept the new `needs` keyword. The affected generated and explicit comparisons retain their intended assertions.
- An interleaving injected its fence after only the first early query; the dependency query then correctly held the job before preparation. The injection is moved after the entire early check so the test still exercises the reserving transaction.

The unverified corrections are preserved as `c1c-integration-fixes-in-progress.diff`; this evidence checkpoint does not assert they are green. No production source or contract change has been made in this resumed process. Steps 3, the spelling gap and step 4 remain unstarted pending completion of the required coverage.

### Integration corrections checkpoint, 10:20Z

The corrected detached dependency oracle passed in `c1c-remaining-04-resume-4` (47.31 s call; its 80-example assertion completed). The rediscovery comparison and full-early-look interleaving passed in `c1c-remaining-11-resume-2` (9 passed overall, load 94.8). That hold also completed the admission state machine and salvage property. `c1c-remaining-13-resume-2` completed the previously interrupted sweep property (40 generated passing examples, plus explicit examples) and all seven combined admission cases: 8 passed at load 80.0.

The writer oracle has now exposed the same omitted Git-storage evidence as the detached oracle. Its equivalent correction and both counterexamples are prepared, with its node assigned to an isolated worker; it is not yet claimed green. Full 400-example fence differential coverage still remains after its keyword-stub correction. The required step 3, spelling-gap red/green and new step 4 properties remain unstarted in the required order.

## Checkpoints and recovery

Shared Git metadata rejected `ORIG_HEAD.lock` with `Operation not permitted`. The original workspace metadata remains at `8f106c64595c`; checkpoints were committed in scratch Git metadata on `fix/retention-dependency-pins-217`. The delivery bundle contains those ordered commits and requires the already-present `8f106c645` and `c9430763f` histories. No shared branch update was possible.

1. `c1c1f6b0d`: merge release/217, retaining the Python dependency pin and every release fence behavior.
2. `6997e2127`: canonical legacy folder discovery outside transactions, output-lease identity assertions, the closed Unicode pin expectation, and the unique unit module basename.

Every checkpoint has the requested co-author footer. [The complete file manifest](2026-10-27-retention-dependency-pins-data/commit-order.txt) lists changes in commit order. The unit file is now `tests/unit/test_retention_dependency_units.py`; its old basename duplicated the fake module and failed the required module-name guard.

## Verified behavior and red runs

On release alone (`c9430763f`, private detached scratch worktree), the dependency fake file produced **25 failures and 3 passes in 348.13 s**. The unit file produced **one collection error** because release has no `subfleet.dependencies`; no unit cases executed. This unit red run used the authorized outside-lock fallback after 30 minutes without admission.

The merged core hold produced **58 passes and 2 failures in 1,150.39 s**. Both failures were obsolete expectations: raw output lease keys versus release's canonical identity, and a legacy Unicode alias expected to miss the pin. After their correction, the follow-up produced **85 passes and one failure in 959.48 s**: the newly added legacy firmlink regression. The three contract guards (63 cases) and desktop ledger checks passed in that follow-up.

Release no longer caches `job.folder_spelled` across admission looks. A legacy job in a plain folder through the Data-volume firmlink therefore needed its canonical workdir added to the pass's dependency cache. That filesystem work runs before the transaction. The regression failed with `legacy={'legacy': []}` before the fix; the three legacy unit checks passed afterward (**3 passed, 14 deselected, 18.59 s**, outside the lock after its 30-minute wait).

## Holds

Loads below are the observed 1-minute load at admission, except the original fake red hold, whose lock script's `sysctl` read was blocked. Its queue/end loads are given instead. Every pytest was serial; no xdist was installed. Interrupted holds are incomplete coverage, not green suite results. The highest recorded Python RSS so far is 184,696,832 bytes (176.1 MiB); subprocess RSS was not separately sampled.

| Hold | Result | Lock | Load | Admission-to-exit wall |
|---|---|---|---:|---:|
| [red-fake](2026-10-27-retention-dependency-pins-data/red-fake.log) | 25 failed, 3 passed in 348.13s (0:05:48) | locked | unavailable (queue 24.4; end 125.5) | not recorded |
| [merge-core](2026-10-27-retention-dependency-pins-data/merge-core.log) | 2 failed, 58 passed in 1150.39s (0:19:10) | locked | 84.3 | 1190.4 s |
| [retention-archive-a](2026-10-27-retention-dependency-pins-data/retention-archive-a.log) | 15 passed, 42 deselected in 339.58s (0:05:39) | locked | 124.6 | not recorded |
| [red-unit](2026-10-27-retention-dependency-pins-data/red-unit.log) | 1 error in 34.25s | outside | 95.2 | 57.1 s |
| [merge-fixes](2026-10-27-retention-dependency-pins-data/merge-fixes.log) | 1 failed, 85 passed in 959.48s (0:15:59) | locked | 96.5 | 1010.4 s |
| [admission-a](2026-10-27-retention-dependency-pins-data/admission-a.log) | interrupted: 162 passing markers, 1 intended xfail; final 2 nodes scheduled separately | locked | 93.2 | not recorded |
| [retention-light](2026-10-27-retention-dependency-pins-data/retention-light.log) | 37 passed in 1077.07s (0:17:57) | locked | 106.3 | 1100.6 s |
| [retention-archive-1](2026-10-27-retention-dependency-pins-data/retention-archive-1.log) | 22 passed, 87 deselected in 300.91s (0:05:00) | locked | 51.6 | 315.7 s |
| [legacy-fold-green](2026-10-27-retention-dependency-pins-data/legacy-fold-green.log) | 3 passed, 14 deselected in 18.59s | outside | 69.7 | 29.1 s |
| [retention-archive-3](2026-10-27-retention-dependency-pins-data/retention-archive-3.log) | 21 passed, 88 deselected in 212.49s (0:03:32) | locked | 61.8 | 221.6 s |
| [admission-a-tail](2026-10-27-retention-dependency-pins-data/admission-a-tail.log) | 2 passed in 156.30s (0:02:36) | outside | 61.7 | 171.7 s |
| [retention-rest-1](2026-10-27-retention-dependency-pins-data/retention-rest-1.log) | 12 passed in 185.78s (0:03:05) | outside | 217.4 | 769.7 s |
| [admission-b-1](2026-10-27-retention-dependency-pins-data/admission-b-1.log) | 21 passed, 276 deselected in 767.14s (0:12:47) | locked | 174.9 | 1120.4 s |
| [retention-archive_properties](2026-10-27-retention-dependency-pins-data/retention-archive_properties.log) | 2 passed in 332.69s (0:05:32) | outside | 64.4 | 341.1 s |
| [salvage-small-a](2026-10-27-retention-dependency-pins-data/salvage-small-a.log) | 62 passed, 54 deselected in 899.31s (0:14:59) | outside | 13.6 | 1024.9 s |

The admission slice's expected failure is intentional and pre-existing: `test_c6_10_a_wait_of_another_kind_does_not_record_the_same_verdict_again` documents C-6.10's wait-kind reset gap. It is not closed by retention pins. Queued wrappers that were cancelled or resized before admission did not run tests and are omitted above.

## Conflict resolutions

The required contract helper refused C-6.5, C-6.11 and C-8.4. Its non-overlapping edits were retained; only those named clauses were hand-integrated. Clause IDs were checked with `^- \*\*(C-\d+\.\d+)\*\*`: 255 unique IDs.

- Contract changelog hunk: required helper combined independent entries against the merge base, without duplicating clauses.
- Contract C-6.5 hunk: keep release refusal and resource-identity rules; add dependency recording once (the old branch duplicated that sentence).
- Contract C-6.11 hunk: keep release hold taxonomy and output identities; add the dependency needs role.
- Contract C-8.4 hunk: keep release cwd, rediscovery, job-directory, nested-lease and retention scheduling guarantees; replace the obsolete ASCII pin limit with folded dependency rules.
- retention-archive hunk 1: dependency places and pending exports supersede ASCII-only pin text.
- service hunk 1: keep release job-directory/worktree wording and folded folder comparison, add git-storage explanation.
- daemon hunk 1: union dependency and release imports.
- daemon hunk 2: retain depends and release unspelled metadata.
- daemon hunk 3: retain release early-look signature and semantics; add keyword needs.
- daemon hunk 4: retain release actual cwd/absent/source fence reads; append dependency fence reads.
- daemon hunk 5: combine early fence results and name first dependency role when applicable.
- daemon hunk 6: retain release rediscovery/current-folder flow and compute needs before it.
- daemon hunk 7: retain all release reservation blockers and cwd reader rows; append dependency blockers, excluding own key.
- daemon hunk 8: retain release source-folder attribution; otherwise name folded dependency and needs role.
- folders hunk 1: keep release threading import.
- folders hunk 2: keep release under_git and folded retiring API.
- retention hunk 1: union dependency and release process/TMPDIR imports.
- retention hunk 2: use Python dependency pin instead of both SQL self-joins.
- retention hunk 3: keep release nested worktree leases and jobs-directory turn pins; omit superseded SQL unrecorded pin.
- retention hunk 4: retain both legacy dependencies and orphan-retention lease state.
- retention_archive hunk 1: retain both canonical dependency recheck and release nested exclusive-lease recheck.

## Unbuilt and unrun

- Step 2's remaining retention, admission and salvage coverage is recorded in the saved plans and collected-node lists. A partial or interrupted run is not a substitute for the missing cases.
- Step 3: `454ed9528`'s folded-pin test file was inspected but not added or run. The WIP is **unassessed**; **0 of its 150 real-volume examples were observed**. Its retention implementation was not ported.
- Step 4: the generated selection safety, independent differential, monotonicity and admission-fence properties were prepared only as an unexecuted sketch. No new property example counts are claimed. The selection sketch asserts that unpinned fixtures reach retirement, so it must not pass vacuously; its fixture still needs validation. The sketch also targets exact dependency-fence lookups when quarantine leaves an aliased output-path tail unspellable.
- Step 5: **0/33 mutants ran**. The 29 original definitions and four additions are retained below as a plan, not as kill evidence. The copied runner adapts `early-look-folder-only` to the merged early-look structure; unit selectors use the new basename. No evidence file outside this workspace was changed. Its reusable snapshot was designed to reduce disk writes; that runner was not exercised.
- Step 6: the measurement tool was prepared as text only, not installed under `tools/` or executed. The timing run must still run once inside the test lock. The contract makes no new numeric query-time claim.
- Step 7: the full contract and decision-note audit remains unfinished. Closing documentation qualifies the still-exact dependency admission lookup and removes unsupported timing citations; this is not completion of the requested audit.
- `tests/frontend/test_core_live.py` was not run: the lane rules reserve it for CI because the daemon's person-only probe blocks it. No full suite or Python 3.12 run was attempted.

| Scale | Old pin-query time | New pin-query time | Measurement load |
|---|---:|---:|---:|
| 3,000 jobs / 100 live | not measured | not measured | not measured |
| 6,000 jobs / 600 live | not measured | not measured | not measured |

The files under `pending/` are sketches with references to this run's scratch helpers; they require revalidation and adaptation before use. They are preserved to avoid losing preparation, and are not repository test/tool implementations.

## Mutation plan: all unrun

“Planned test” names the first selected test, not an observed killing test. The original runner's secondary cross-file selections would need separate holds to retain the one-file-per-mutant rule.

| Mutant | Planned test | Observed result / killing test |
|---|---|---|
| `submit-records-nothing` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[linked]` | not run / none |
| `live-ignores-depends` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | not run / none |
| `live-ignores-row-places` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | not run / none |
| `live-ignores-recorded-folder` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | not run / none |
| `live-ignores-spelled-folder` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | not run / none |
| `live-ignores-exports` | `tests/unit/test_retention_dependency_units.py::test_d2_a_pending_export_pins_and_a_written_one_does_not` | not run / none |
| `exports-only-recorded-path` | `tests/unit/test_retention_dependency_units.py::test_d2_a_pending_export_pins_and_a_written_one_does_not` | not run / none |
| `no-fold` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | not run / none |
| `within-not-prefix-boundary` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | not run / none |
| `pin-ignores-unrecorded-tree` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | not run / none |
| `pin-counts-own-places` | `tests/unit/test_retention_dependency_units.py::test_d2_d3_worktree_in_use_is_exactly_the_specification` | not run / none |
| `selection-no-dependents` | `tests/unit/test_retention_dependency_units.py::test_the_selecting_transaction_reads_dependents_on_the_fences_spelling` | not run / none |
| `commit-no-dependents` | `tests/unit/test_retention_dependency_units.py::test_the_commit_reads_dependents_on_the_journals_spelling` | not run / none |
| `no-legacy-evidence` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_job_queued_before_dependencies_were_recorded_keeps_the_tree[linked]` | not run / none |
| `legacy-admission-reads-nothing` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_legacy_job_submitted_under_the_fence_waits_on_its_git_storage` | not run / none |
| `early-look-folder-only` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_job_submitted_under_the_fence_waits_while_a_place_it_needs_is_retired[linked-read-only]` | not run / none |
| `transaction-no-needs-blockers` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_fence_on_a_needed_place_taken_after_the_early_look_holds_the_job_in_its_transaction[linked-worktree]` | not run / none |
| `transaction-own-not-filtered` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_fence_on_an_in_place_writers_own_folder_that_its_output_is_in_is_named_once` | not run / none |
| `transaction-hold-names-no-place` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_fence_on_a_needed_place_taken_after_the_early_look_holds_the_job_in_its_transaction[output-read-only]` | not run / none |
| `git-storage-no-commondir` | `tests/unit/test_retention_dependency_units.py::test_d1_a_linked_checkout_outside_names_the_repository_it_lives_in` | not run / none |
| `git-storage-no-worktrees-fallback` | `tests/unit/test_retention_dependency_units.py::test_d1_storage_a_gitfile_names_counts_when_it_is_gone` | not run / none |
| `git-storage-no-alternates` | `tests/unit/test_retention_dependency_units.py::test_d1_alternates_are_followed` | not run / none |
| `git-storage-alternates-one-level` | `tests/unit/test_retention_dependency_units.py::test_d1_alternates_are_followed` | not run / none |
| `git-storage-gitfile-relative-to-cwd` | `tests/unit/test_retention_dependency_units.py::test_d1_a_separate_git_dir_and_a_relative_gitfile` | not run / none |
| `git-storage-nearest-ignores-gitfiles` | `tests/unit/test_retention_dependency_units.py::test_d1_a_linked_checkout_outside_names_the_repository_it_lives_in` | not run / none |
| `record-raw-spelling` | `tests/unit/test_retention_dependency_units.py::test_record_spells_each_place_once` | not run / none |
| `fences-not-retention-only` | `tests/unit/test_retention_dependency_units.py::test_d5_fences_reads_what_retiring_reads_place_by_place` | not run / none |
| `render-ignores-needs` | `tests/fake/test_retention_dependencies.py::test_c6_11_why_names_the_place_a_job_needs_in_the_tree_being_retired` | not run / none |
| `turn-reason-ignores-needs` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_turn_in_a_checkout_whose_repository_is_in_the_tree_waits_for_the_fence[READER]` | not run / none |
| `collate-nocase-restored` | `tests/unit/test_retention_folded_pin.py::test_a_job_id_typed_with_letters_the_volume_folds_keeps_the_tree` | not run / none |
| `record-no-output` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[output]` | not run / none |
| `record-no-git` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[linked]` | not run / none |
| `admission-no-needs-role` | `tests/fake/test_retention_dependencies.py::test_c8_4_a_job_submitted_under_the_fence_waits_while_a_place_it_needs_is_retired[output-read-only]` | not run / none |
