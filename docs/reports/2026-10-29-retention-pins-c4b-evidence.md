# Retention dependency pins: continuation 4b evidence, 2026-10-29

Base: `2f1c81ab982d030eef62e2db0e21fcc773b7585f`. Started 00:19:36 UTC on October 29; new holds and steps stop by 04:49:36 UTC. Product code, the contract and the main report are unchanged. The versioned changes are the unit killing test and this report.

Shared git metadata refused `index.lock`. Commits use private scratch metadata on `fix/retention-dependency-pins-217`; the assigned `.git` file is unchanged. The ordered commits are delivered in `rdp217c4b.bundle`, based on the commit above. No fetch, push, production CLI, provider or live store was used.

## Step 1: the sixteen remaining mutants

All sixteen mutants are killed. Each mutant ran in its own hold. The committed [continuation 3 definitions](2026-10-27-retention-dependency-pins-data/c3-mutant-definitions.json) were applied by an in-memory import loader: each replacement matched exactly once, compiled, and its target module was observed loading. No product file was edited. Settings retained the configured maxima and strategies, seed **217**, and **explicit/generate phases without shrinking** (`-x` stops at the first counterexample). Independent holds used the supplied lock, with at most four pytest processes; no sub-agents or xdist were used.

P/F/U means passed / failed / unfinished selected nodes, requiring all three passing phases for P and completed teardown for F. A mutation F is expected killing evidence; U after `-x` is an unexecuted selected node, not a head failure. All starts are October 29 UTC; load columns give the observed one-minute host load from `os.getloadavg()` inside the admitted process.

| Mutant | Observed killer | Start UTC | Load | P/F/U |
|---|---|---|---:|---|
| `transaction-own-not-filtered` | K1 | 00:25:35 | 59.5 | 0/1/0 |
| `transaction-hold-names-no-place` | K2 | 00:26:38 | 58.6 | 0/1/0 |
| `git-storage-no-commondir` | K3 | 00:29:37 | 41.8 | 0/1/0 |
| `git-storage-no-worktrees-fallback` | K4 | 00:27:39 | 36.3 | 0/1/0 |
| `git-storage-no-alternates` | K5 | 00:29:31 | 41.0 | 0/1/0 |
| `git-storage-alternates-one-level` | K5 | 00:27:49 | 32.8 | 0/1/0 |
| `git-storage-gitfile-relative-to-cwd` | K6 | 00:28:34 | 22.5 | 0/1/0 |
| `git-storage-nearest-ignores-gitfiles` | K7 | 00:31:23 | 55.5 | 0/1/0 |
| `record-raw-spelling` | K8 | 00:30:16 | 53.7 | 0/1/0 |
| `fences-not-retention-only` | K9 | 00:34:17 | 17.0 | 0/1/0 |
| `render-ignores-needs` | K10 | 00:39:41 | 73.9 | 0/1/3 |
| `turn-reason-ignores-needs` | K11 | 00:47:15 | 36.2 | 0/1/0 |
| `collate-nocase-restored` | K12 | 00:47:53 | 33.6 | 0/1/19 |
| `record-no-output` | K13 | 00:46:44 | 40.8 | 0/1/0 |
| `record-no-git` | K14 | 00:46:50 | 39.1 | 0/1/0 |
| `admission-no-needs-role` | K15 | 00:47:20 | 35.2 | 0/1/0 |

| Key | Full observed killing node |
|---|---|
| K1 | `tests/fake/test_retention_dependencies.py::test_c8_4_a_fence_on_an_in_place_writers_own_folder_that_its_output_is_in_is_named_once` |
| K2 | `tests/fake/test_retention_dependencies.py::test_c8_4_a_fence_on_a_needed_place_taken_after_the_early_look_holds_the_job_in_its_transaction[output-read-only]` |
| K3 | `tests/unit/test_retention_dependency_units.py::test_d1_commondir_outside_the_worktrees_layout_is_followed` |
| K4 | `tests/unit/test_retention_dependency_units.py::test_d1_storage_a_gitfile_names_counts_when_it_is_gone` |
| K5 | `tests/unit/test_retention_dependency_units.py::test_d1_alternates_are_followed` |
| K6 | `tests/unit/test_retention_dependency_units.py::test_d1_a_separate_git_dir_and_a_relative_gitfile` |
| K7 | `tests/unit/test_retention_dependency_units.py::test_d1_a_linked_checkout_outside_names_the_repository_it_lives_in` |
| K8 | `tests/unit/test_retention_dependency_units.py::test_record_spells_each_place_once` |
| K9 | `tests/unit/test_retention_dependency_units.py::test_d5_fences_reads_what_retiring_reads_place_by_place` |
| K10 | `tests/fake/test_retention_dependencies.py::test_c6_11_why_names_the_place_a_job_needs_in_the_tree_being_retired[review-root-/s/worktrees/j/vendor/lib-that its review root (-D) /s/worktrees/j/vendor/lib is in]` |
| K11 | `tests/fake/test_retention_dependencies.py::test_c8_4_a_turn_in_a_checkout_whose_repository_is_in_the_tree_waits_for_the_fence[READER]` |
| K12 | `tests/unit/test_retention_folded_pin.py::test_a_job_id_typed_with_letters_the_volume_folds_keeps_the_tree[recorded-submitted-all]` |
| K13 | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[output]` |
| K14 | `tests/fake/test_retention_dependencies.py::test_c8_4_a_queued_job_that_needs_a_place_in_a_tree_keeps_it_until_it_ends[linked]` |
| K15 | `tests/fake/test_retention_dependencies.py::test_c8_4_a_job_submitted_under_the_fence_waits_while_a_place_it_needs_is_retired[output-read-only]` |

`git-storage-no-commondir` initially survived both planned tests: the linked-checkout case and 12 generated passing layout examples. All use a conventional `worktrees/` parent, so the fallback supplies the same common directory. This is a missing test, not an intended equivalent.

Added `test_d1_commondir_outside_the_worktrees_layout_is_followed`: create a repository and linked checkout, move its admin directory to `metadata/slot`, rewrite the gitfile and relative `commondir`, and compare both the explicit storage list and Git’s own reported paths. The mutant omits the common repository; the new test is red against it and green at head. Commit: `18eff6dcda0338f9d49d23ba2fa345016f2b55a5`.

No remaining mutant is labelled equivalent. No product defect has been observed.

## Step 2: the three previously capped properties

Each property runs alone in its own hold at its existing normal settings, with a 2,700-second execution cap. Completed Hypothesis engine evaluations are counted observationally without changing generation. Explicit examples are counted at successful `execute_once` returns. Terminal Hypothesis statistics confirm the first two properties. The fence property wraps a nested given function, so its counts come from the observational engine and explicit-example counters; its outer pytest call emits no nested terminal statistics. Caps would receive only the observed completed prefix.

| Property | Observed examples | Outcome | Start UTC | Load | P/F/U |
|---|---|---|---|---:|---|
| `test_python_pins_and_transaction_agree_with_independent_reference` | 100 generated passing / 1 invalid | passed | 00:55:11 | 19.5 | 1/0/0 |
| `test_live_dependents_are_monotone_and_the_last_ending_allows_retirement` | 100 generated passing / 2 invalid | passed | 00:56:15 | 19.6 | 1/0/0 |
| `test_early_fences_equal_reservation_before_and_after_quarantine` | 12 explicit passing + 400 generated passing / 90 invalid | passed | 01:02:19 | 10.2 | 1/0/0 |

## Step 3: measurement

The existing pending tool was copied unchanged to scratch. Final revalidation passed in its own hold: it checks both old SQL statements byte-for-byte against release `c9430763f`, compares the final owner loop by AST after normalizing the supplied root and empty legacy inputs, and compares a small untimed synthetic store against both queries and the final `_pin_reasons`. Measurement runs use only synthetic scratch-owned stores under the test lock. Each requested scale gets exactly one timed read per side; there are no other timing runs or tuning changes.

| Scale | Old query ms | New query ms | New/old | Run start UTC | Old load (1/5/15) | New load (1/5/15) | Pins / events |
|---|---:|---:|---:|---|---|---|---|
| 3,000 jobs / 100 live | 164.445 | 10.267 | 0.062× | 01:06:23 | 12.7 / 13.8 / 24.0 | 12.7 / 13.8 / 24.0 | 22 / 147,801 |
| 6,000 jobs / 600 live | 1565.193 | 32.488 | 0.021× | 01:08:04 | 12.4 / 13.4 / 22.7 | 12.4 / 13.4 / 22.7 | 117 / 345,201 |

The 3,000-job fixture has 2,900 owners, 100 live jobs, 40 waiting events per ordinary job, five long waiters with 5,000 events each, 10 pending exports and no legacy jobs. The 6,000-job fixture has 5,400 owners, 600 live jobs, 20 long waiters and 50 pending exports. Seed is 7. Both pin sets must agree. These are pin-query comparisons, not end-to-end retention timings. Python is 3.14.7 and SQLite is 3.53.4. Both new queries are below the 2×-old threshold; no conditional query-plan dump or tuning is needed.

## Holds

Environment sync ran once through `uv_sync_hold.sh`: locked Python 3.14.7 / pytest 9.1.1 / Hypothesis 6.168.1. Every test uses `uv run --no-sync`, private TMPDIR and Hypothesis storage, with bytecode disabled. The wrapper’s sandbox-denied `sysctl` output supplies no load evidence; the tables use Python’s actual host readings.

An early optional wrapper RSS check using `ps` was denied. It was removed in favor of `resource.getrusage(RUSAGE_SELF)`. Four affected pytest sessions recorded completed setup, call and teardown and the expected pytest session-finish status 1 before wrapper failure: `commondir-red`, `git-storage-gitfile-relative-to-cwd`, `git-storage-nearest-ignores-gitfiles`, and `record-raw-spelling`. Their JSON phase records and lock-admission log lines establish the results; wrapper exit status is not presented as pytest status. The sequencer resumed only unstarted mutants.

| Hold | Start UTC | Load | P/F/U | Lock | Result |
|---|---|---:|---|---|---|
| `transaction-own-not-filtered` | 00:25:35 | 59.5 | 0/1/0 | locked | exit 1 |
| `git-storage-no-commondir` | 00:26:22 | 59.0 | 2/0/0 | locked | exit 0 |
| `transaction-hold-names-no-place` | 00:26:38 | 58.6 | 0/1/0 | locked | exit 1 |
| `git-storage-no-worktrees-fallback` | 00:27:39 | 36.3 | 0/1/0 | locked | exit 1 |
| `git-storage-alternates-one-level` | 00:27:49 | 32.8 | 0/1/0 | locked | exit 1 |
| `git-storage-gitfile-relative-to-cwd` | 00:28:34 | 22.5 | 0/1/0 | locked (admission line verified) | exit 1 |
| `git-storage-no-alternates` | 00:29:31 | 41.0 | 0/1/0 | locked | exit 1 |
| `commondir-red` | 00:29:37 | 41.8 | 0/1/0 | locked (admission line verified) | exit 1 |
| `record-raw-spelling` | 00:30:16 | 53.7 | 0/1/0 | locked (admission line verified) | exit 1 |
| `commondir-green` | 00:30:50 | 54.3 | 1/0/0 | locked | exit 0 |
| `git-storage-nearest-ignores-gitfiles` | 00:31:23 | 55.5 | 0/1/0 | locked (admission line verified) | exit 1 |
| `fences-not-retention-only` | 00:34:17 | 17.0 | 0/1/0 | locked | exit 1 |
| `render-ignores-needs` | 00:39:41 | 73.9 | 0/1/3 | locked | exit 1 |
| `record-no-output` | 00:46:44 | 40.8 | 0/1/0 | locked | exit 1 |
| `record-no-git` | 00:46:50 | 39.1 | 0/1/0 | locked | exit 1 |
| `turn-reason-ignores-needs` | 00:47:15 | 36.2 | 0/1/0 | locked | exit 1 |
| `admission-no-needs-role` | 00:47:20 | 35.2 | 0/1/0 | locked | exit 1 |
| `collate-nocase-restored` | 00:47:53 | 33.6 | 0/1/19 | locked | exit 1 |
| `guards` | 00:49:56 | 25.9 | 63/0/0 | locked | exit 0 |
| `head-units` | 00:50:32 | 18.1 | 19/0/0 | locked | exit 0 |
| `head-dependencies` | 00:51:58 | 15.0 | 28/0/0 | locked | exit 0 |
| `property-reference` | 00:55:11 | 19.5 | 1/0/0 | locked | exit 0 |
| `property-monotone` | 00:56:15 | 19.6 | 1/0/0 | locked | exit 0 |
| `property-fences` | 01:02:19 | 10.2 | 1/0/0 | locked | exit 0 |
| `measurement-preflight` | 01:04:54 | 14.1 | 1/0/0 | locked | exit 0 |
| `timing-3000` | 01:06:22 | 12.9 | 1/0/0 | locked | exit 0 |
| `timing-6000` | 01:08:03 | 12.3 | 1/0/0 | locked | exit 0 |

## Closeout and open work

All 4b work is complete: sixteen killed mutants, all three normal-setting properties, final tool revalidation, and both one-shot comparisons. All **27 holds were locked**; no outside-lock fallback or cap was used. Head pytest executions are **117 passed / 0 failed / 0 unfinished**. Across all holds, including the expected mutation failures and initial survivor, counts are **119 passed / 16 failed / 22 unfinished selected nodes**. The 22 unfinished mutant selections are later parameters skipped by `-x` after their mutant was killed; they are not open head checks.

All test processes ended by 01:08:09 UTC. All **50 recorded owned PIDs** were absent at closeout. The largest observed Python maximum RSS was **140.03 MiB**; scratch was **75.92 MiB** at the closeout sample, below the 2 GB disk allowance. No host-load generator ran. No bytecode, pytest cache or Hypothesis storage was left in the worktree. The bundle is verified before the supplied guarded scratch cleanup, which is the final delivery action.

No 4b check remains unrun. Continuation 4a's contract audit/integration and CI remain separate work; this continuation does not certify PR readiness. The full suite and Python 3.12 were not run locally. `tests/frontend/test_core_live.py` remains CI-only: the lane's person-only daemon check prevents the probe at step 5, as the supplied rules specify.

Source SHA-256:

| File | SHA-256 |
|---|---|
| `subfleet/dependencies.py` | `1cfdb7bf3c55833b8e1d254a73dc6b0baaee4194990653e8c33aa38c5858f074` |
| `subfleet/retention.py` | `40923324b743565c30c001cc262ca9fc5f354cf970467af17318c759d10b53c4` |
| `subfleet/daemon.py` | `1034ded9f11a39f9d975465fca3f9c29c23322d7aa2647a5b8a17cd02b469d79` |
| `subfleet/conversations/service.py` | `7424d7ab03d13ef2de937d88020691e92e985a8db2df41e02e74366872ccccd0` |
| `subfleet/render.py` | `78b96305741ecac80a9dec7a096e63b366d8b9fb3b7c61c1a34c7d6e2a9a1332` |
| `docs/reports/2026-10-27-retention-dependency-pins-data/pending/measure_retention_pins.py.txt` | `888019271b9e12b9b120bb45a2741a45416771ffb648547a78640ff63acb9540` |
