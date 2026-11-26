# Retention dependency pins: fix round r9

**Fix round r9 repairs R9-F1: every returned discovery place now has one spelling.** Successful physical pointer files and resolved scan targets take `folders.canonical`; physical symlink entries retain canonical parent plus entry name. Spelling the supplied start before routine-location comparisons keeps routine records unchanged through accepted aliases. Discovery stays outside transactions. Retention remains dormant pending approving review r10 and completed CI.

Started **2026-10-31 09:56:57 UTC**, input head `236c80883ce3a6432e1621fff5a08e73dbc32bf3`. Holds began at 09:58:02 UTC, inside the first hour. The four-hour hold-start cutoff was 13:56:57 UTC. The requested macOS scratch folder was denied; `/private/tmp/sf-fix272r9` was used. Shared Git metadata rejected `index.lock`, so the assigned branch is committed in worktree-local metadata and delivered in `fix272r9.bundle`, requiring exactly `236c80883`. No shared branch or history was rewritten.

## Decisions as built

1. `_Aliases.text`, `_alternates`, and `_Aliases.scan` canonicalize their successful physical file or resolved target exactly as the three-site trial specifies. Link-entry recording is unchanged.
2. `_Aliases.locations` canonicalizes start before its ancestor comparison. The executed linked-checkout counterexample requires the one-line change; plain controls are unchanged.
3. `assert_location_spelling` captures the actual discovery `_Aliases.links` set. Every returned place equals canonical parent plus basename; every non-link place also equals `folders.canonical(place)`. It runs in all four r9 sibling controls, the new property and all five existing routine controls.
4. Only r9's strict xfail decorator was removed. Its remaining bytes and the entire prior location-input module are unchanged.
5. The new `tests/unit/test_retention_location_spelling.py` uses real Git, three trees and an outside checkout; all eleven positions; accepted Data-volume, other-case and Unicode-normalization spellings; 55 generated inputs, `derandomize=True`, `database=None`, no explicit examples. The owner has a non-ASCII name. The checkout also has one so the supplied-folder spelling exercises decision 2. The oracle reads first, requires complete evidence, excludes unrelated, checks the spelling invariant and exact routine `.git` omission, checks owner by `folders.within(place, canonical_owner)` without respelling a recorded place, removes owner and requires the identical read to fail.
6. Four new named mutants die. All nine r8 mutants die at the same assertions as r8. The older twenty were deliberately not rerun.
7. In isolated scratch source copies, each of the four sites is reverted alone. Every new property copy is byte-identical, has no explicit examples, and detects its reversion. First failures before shrinking appear below.
8. The discovery docstring, desktop discovery description and D-R8-2 line, main report, this report and PR-body replacement lines are updated. The acceptance contract is unchanged.

No decision changed the reader's reach. No new product gap was found. The initial unconstrained 55-input strategy left five cells empty (28/33): that was a strategy finding, not complete coverage. The final finite cross product generates all 55 distinct position/variant inputs and all 33 nonempty cells. Named-admin variants add a second Data-volume and case input per position; normalization uses the unnamed admin. The earlier receipts are retained as superseded and excluded from final accounting.

## Decision 2: execution before editing

Both a plain repository and `git worktree add` checkout were passed directly through the Data-volume spelling to `git_storage` and `record`. Neither test caller canonicalizes the supplied folder first. `record` forwards workdir unchanged. Daemon submission uses `Path.resolve`, which preserves this firmlink spelling; its separate canonical `folder` does not replace the workdir passed to `record` (daemon.py around 2989/3106). Legacy discovery likewise calls `record(row["workdir"])` before separately spelling that row's workdir.

| Input | At 236c80883 | Three sites alone | With start canonicalized |
|---|---|---|---|
| Plain | `.git`, `.git/objects` | Same | Same |
| Linked checkout | admin, common, common/objects | Same plus checkout/.git | Exact base list |

`git_storage` returns those lists; `record` wraps each as `{"git": list}`. All six observations report no incomplete evidence. The `.git` file added by the three-site trial is canonical, but start is still Data-volume spelled, so its parent no longer matches the ancestor set. The one-line start fix restores that comparison. Full paths and both APIs are in `start-before.json`.

## Five routine controls against 236c80883

`routine-comparison.json` executes the base code from `git show 236c80883:subfleet/dependencies.py` beside the repair on each identical real-Git fixture. Plain, worktree-add, shared clone, nested git-new-workdir and no-Git all satisfy `base == repaired == expected`. Their existing exact assertions remain, followed by the new spelling helper. The untouched dependency-unit file passes all 19 cases under linked TMPDIR.

## Generated class table

All 55 inputs have successful initial reads, complete evidence, unrelated omitted, canonical owner matched by consumers' string comparison, and failing identical reads after owner removal. Independent inputs: 0. Skips: 0; this volume accepts every requested form. Linked TMPDIR reproduces the same table.

| Position | Data-volume | Other case | Other normalization |
|---|---:|---:|---:|
| gitfile-target | 2 | 2 | 1 |
| intermediate | 2 | 2 | 1 |
| gitfile-pointer | 2 | 2 | 1 |
| commondir-file | 2 | 2 | 1 |
| commondir-target | 2 | 2 | 1 |
| objects | 2 | 2 | 1 |
| objects-info | 2 | 2 | 1 |
| alternates-file | 2 | 2 | 1 |
| alternates-line | 2 | 2 | 1 |
| git-entry | 2 | 2 | 1 |
| objects-pack | 2 | 2 | 1 |


## Generated-only single-site reversions

Each copy preserves the property bytes and deterministic settings. Recorded inputs below are the first generated failures before shrinking, not inserted examples. The source manifest and attempt receipts retain every trial.

| Single-site reversion | Result | First failing generated input, before shrinking | Assertion |
|---|---|---|---|
| r9-text-canonicalization-dropped | Detected | `{"named": false, "position": "gitfile-target", "spelling": "data-volume"}` | `assert os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place, place` |
| r9-alternates-canonicalization-dropped | Detected | `{"named": true, "position": "alternates-file", "spelling": "data-volume"}` | `assert os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place, place` |
| r9-scan-canonicalization-dropped | Detected | `{"named": false, "position": "commondir-file", "spelling": "data-volume"}` | `assert os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place, place` |
| r9-start-spelling-dropped | Detected | `{"named": false, "position": "gitfile-target", "spelling": "data-volume"}` | `assert str(checkout / ".git") not in recorded.get("git", []), recorded` |


## Red, green and per-file verification

The red hold exposes four failures at owner retention with passing setup/teardown. Green: r9 file **4**, new tests **11**, location tests **22**, unchanged location property **1**. Each required linked-TMPDIR file passes in its own hold: r9 **4**, new tests **11**, locations **22**, location property **1**, pointer inputs **2**, r8 **2**, dependency units **19**, fake dependencies **30**. The R-files, dependency set, all turn-retention files, both admission lease-oracle tests, archive, nested leases and three guards also run. Of the 714 distinct ordinary nodes, 707 are repository tests and seven are the requested pre-repair/routine execution probes. Exact final per-file counts follow; the hold table records every repeated invocation.

| File | Passing nodes | First contributing start (UTC) | Load 1/5/15 | Hold |
|---|---:|---|---|---|
| docs/reports/2026-10-31-retention-pins-fix-r9-data/probes.py | 7 | 2026-10-31T09:58:32.737880+00:00 | 34.61/34.48/36.04 | start-before |
| tests/fake/test_detached_retention_fence.py | 1 | 2026-10-31T10:11:06.611914+00:00 | 36.95/34.42/34.94 | admission-detached |
| tests/fake/test_retention_dependencies.py | 30 | 2026-10-31T10:05:24.205991+00:00 | 31.16/33.74/35.14 | linked-test_retention_dependencies |
| tests/fake/test_retention_dependency_incomplete.py | 19 | 2026-10-31T10:10:14.330758+00:00 | 34.24/33.74/34.75 | regressions-r1 |
| tests/fake/test_retention_dependency_review_r1.py | 18 | 2026-10-31T10:10:14.330758+00:00 | 34.24/33.74/34.75 | regressions-r1 |
| tests/fake/test_retention_dependency_review_r2.py | 21 | 2026-10-31T10:08:43.522131+00:00 | 32.87/33.81/34.91 | regressions-r4-r2 |
| tests/fake/test_retention_dependency_review_r3.py | 6 | 2026-10-31T10:08:43.522131+00:00 | 32.87/33.81/34.91 | regressions-r4-r2 |
| tests/fake/test_retention_dependency_review_r4.py | 53 | 2026-10-31T10:08:43.522131+00:00 | 32.87/33.81/34.91 | regressions-r4-r2 |
| tests/fake/test_retention_dependency_review_r5.py | 60 | 2026-10-31T10:07:16.663907+00:00 | 33.96/34.05/35.11 | regressions-r5 |
| tests/fake/test_retention_dependency_review_r6.py | 16 | 2026-10-31T10:06:33.220516+00:00 | 33.07/34.05/35.16 | regressions-r7-r6 |
| tests/fake/test_retention_dependency_review_r7.py | 3 | 2026-10-31T10:06:33.220516+00:00 | 33.07/34.05/35.16 | regressions-r7-r6 |
| tests/fake/test_retention_dependency_review_r8.py | 2 | 2026-10-31T10:04:42.184563+00:00 | 35.22/34.81/35.57 | linked-test_retention_dependency_review_r8 |
| tests/fake/test_retention_dependency_review_r9.py | 4 | 2026-10-31T10:00:09.268802+00:00 | 35.54/34.41/35.82 | green-r9 |
| tests/fake/test_retention_dependency_spelling.py | 16 | 2026-10-31T10:11:05.709091+00:00 | 36.95/34.42/34.94 | dependencies-light |
| tests/fake/test_retention_parent_hint.py | 2 | 2026-10-31T10:06:33.220516+00:00 | 33.07/34.05/35.16 | regressions-r7-r6 |
| tests/fake/test_retention_pointer_failures.py | 7 | 2026-10-31T10:06:33.220516+00:00 | 33.07/34.05/35.16 | regressions-r7-r6 |
| tests/fake/test_retention_recovery_census.py | 2 | 2026-10-31T10:13:00.746946+00:00 | 34.54/34.25/34.80 | aliases-census |
| tests/fake/test_state_contract.py | 37 | 2026-10-31T10:25:16.129267+00:00 | 34.15/33.79/34.06 | guards |
| tests/fake/test_turn_retention_cwd.py | 6 | 2026-10-31T10:22:56.058582+00:00 | 36.15/34.00/34.19 | test_turn_retention_cwd |
| tests/fake/test_turn_retention_cwd_properties.py | 1 | 2026-10-31T10:23:09.749312+00:00 | 36.31/34.13/34.23 | test_turn_retention_cwd_properties |
| tests/fake/test_turn_retention_job_directory.py | 2 | 2026-10-31T10:23:28.522741+00:00 | 34.22/33.79/34.10 | test_turn_retention_job_directory |
| tests/fake/test_turn_retention_job_properties.py | 1 | 2026-10-31T10:23:43.717754+00:00 | 32.85/33.50/34.00 | test_turn_retention_job_properties |
| tests/fake/test_turn_retention_quarantine.py | 10 | 2026-10-31T10:24:02.286004+00:00 | 33.87/33.68/34.05 | test_turn_retention_quarantine |
| tests/fake/test_writer_retention_fence.py | 1 | 2026-10-31T10:11:26.993613+00:00 | 35.91/34.40/34.92 | admission-writer |
| tests/unit/test_invariants_index.py | 25 | 2026-10-31T10:25:16.129267+00:00 | 34.15/33.79/34.06 | guards |
| tests/unit/test_retention_alternates.py | 23 | 2026-10-31T10:11:05.709091+00:00 | 36.95/34.42/34.94 | dependencies-light |
| tests/unit/test_retention_alternates_review_r2.py | 15 | 2026-10-31T10:08:43.522131+00:00 | 32.87/33.81/34.91 | regressions-r4-r2 |
| tests/unit/test_retention_archive.py | 108 | 2026-10-31T10:05:44.314099+00:00 | 32.88/34.03/35.22 | archive-0 |
| tests/unit/test_retention_dependency_aliases.py | 10 | 2026-10-31T10:13:00.746946+00:00 | 34.54/34.25/34.80 | aliases-census |
| tests/unit/test_retention_dependency_properties.py | 4 | 2026-10-31T10:13:09.415466+00:00 | 34.90/34.34/34.82 | dependency-property-0 |
| tests/unit/test_retention_dependency_units.py | 19 | 2026-10-31T10:04:57.888207+00:00 | 33.36/34.38/35.41 | linked-test_retention_dependency_units |
| tests/unit/test_retention_folded_pin.py | 38 | 2026-10-31T10:11:05.709091+00:00 | 36.95/34.42/34.94 | dependencies-light |
| tests/unit/test_retention_job_dependencies.py | 12 | 2026-10-31T10:11:05.709091+00:00 | 36.95/34.42/34.94 | dependencies-light |
| tests/unit/test_retention_location_inputs.py | 1 | 2026-10-31T10:01:40.979033+00:00 | 32.74/33.95/35.51 | green-location-inputs |
| tests/unit/test_retention_location_spelling.py | 11 | 2026-10-31T10:03:42.495432+00:00 | 35.57/34.66/35.58 | green-spelling-final |
| tests/unit/test_retention_locations.py | 22 | 2026-10-31T10:01:18.497994+00:00 | 34.28/34.30/35.67 | green-locations |
| tests/unit/test_retention_nested_leases.py | 27 | 2026-10-31T10:24:19.863403+00:00 | 34.20/33.75/34.06 | nested-leases |
| tests/unit/test_retention_pointer_inputs.py | 2 | 2026-10-31T10:03:57.543651+00:00 | 35.92/34.78/35.60 | linked-test_retention_pointer_inputs |
| tests/unit/test_retention_pointer_whitespace.py | 9 | 2026-10-31T10:07:16.663907+00:00 | 33.96/34.05/35.11 | regressions-r5 |
| tests/unit/test_retention_shared_folders.py | 62 | 2026-10-31T10:17:48.555052+00:00 | 33.93/34.09/34.55 | shared-examples |
| tests/unit/test_test_module_names.py | 1 | 2026-10-31T10:25:16.129267+00:00 | 34.15/33.79/34.06 | guards |


All 68 completed holds used the shared lock, peak pytest concurrency 2; longest 134.10 seconds, peak pytest RSS 143.23 MiB. No 30-minute direct fallback was used. The wrapper's `sysctl` was sandbox-denied; receipts use actual `os.getloadavg()` at admission. Unresolved ordinary failures: 0.

Archive was split by test function into seven holds so no pytest approached ten minutes. Under `/private/tmp`, these six controls hit only the documented scratch-path classification trap: `test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`, `test_blob_only_in_a_local_branch_is_archived_not_omitted`, `test_local_path_remote_is_not_a_remote`, `test_borrowed_or_shallow_object_store_is_scratch[alternates]`, `test_borrowed_or_shallow_object_store_is_scratch[shallow]`, `test_survey_is_read_only_and_predicts_the_pass`. All six pass unchanged with TMPDIR in the new worktree folder `sf-fix272r9-fixtures`, removed at cleanup. Final archive union: 108 passing nodes.

## Named mutants

Every kill is a test-call assertion failure with passing setup/teardown. All nine r8 assertion expressions equal their recorded r8 expressions, as checked in `mutant-results.json`.

| Mutant | Named killer | Result | Same r8 assertion |
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
| r9-text-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[gitfile] | Killed | New |
| r9-alternates-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[alternates] | Killed | New |
| r9-scan-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[objects-pack] | Killed | New |
| r9-start-spelling-dropped | tests/unit/test_retention_location_spelling.py::test_routine_alias_keeps_the_exact_record[data-volume-worktree] | Killed | New |


## PR-body replacement lines

**Fix round r9 repairs R9-F1: every returned discovery place now has one spelling.** Successful physical pointer files and resolved scan targets take `folders.canonical`; physical symlink entries retain canonical parent plus entry name. Spelling the supplied start before routine-location comparisons keeps routine records unchanged through accepted aliases. Discovery stays outside transactions. Retention remains dormant pending approving review r10 and completed CI.

- **Fix r9 evidence:** review r9 is red 4/4 at `236c80883` and green 4/4 after repair. The new real-Git property has 55 complete owner-dependent inputs covering all 33 position/spelling classes, with zero spelling skips. Five routine records equal `236c80883` exactly. All four new mutants and all nine r8 mutants die; the nine r8 assertions are unchanged. Every single-site generated-only reversion is detected. 714 distinct ordinary nodes pass across 68 completed locked pytest holds; per-file counts, UTC starts and loads are retained. The six documented archive path controls pass with worktree TMPDIR.
- **Reader boundaries:** D-R6-1 through D-R6-4, D-R7-1, D-R8-1 and D-R8-2 remain listed differences. No reader is widened; C-8.3/C-8.4 are unchanged.
- **Next:** approving review r10, then the hub's all-pass CI and mergeability checks before activation or merge.

## Not run and cleanup

Not run: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` (mandated live-case deselection); the older twenty mutants (review r9 settled them); full suite; Python 3.12; person-only `tests/frontend/test_core_live.py`; remote CI. Review r9's additional notes (38), archive properties (2), salvage properties (2), and sweep examples/property (13) are outside this brief's requested Run block and were not repeated. Those 55 older nodes explain the difference from r9's 745-node set; this round adds 11 spelling tests, four r9 regressions and two admission oracle nodes, plus seven execution probes. The hub owns CI and publication. No real provider, live daemon/store, real Subfleet CLI, remote fetch, merge, push or GitHub call was used. No process was detached.

Cleanup measurements and delivery details are in `delivery.json`; final bundle verification is retained beside the bundle in `fix272r9-bundle-verify.txt`. The locked environment uses Python 3.14.7, Git 2.55.0, pytest 9.1.1 and Hypothesis 6.168.1 (`environment.json`). The largest retained scratch measurement is 423 MiB; worktree fixtures measured 3.70 MiB. The first guarded cleanup left 80 KiB in one read-only archive-fixture directory; restoring owner permissions there and repeating the same guarded cleanup removed it. Scratch and worktree fixtures are absent; all owned sessions ended. Durable report, data, tests and a verified bundle remain in this worktree.

DONE
