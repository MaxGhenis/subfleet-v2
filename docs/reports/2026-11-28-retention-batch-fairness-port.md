# Retention batch fairness port (#288)

Eligible retention jobs now get tickets, a previously sliced holder can yield a full batch slot, and three consecutive stalled slices trigger error backoff. An archived job awaiting check 2 leaves a slot free for the next eligible start.

Base `f2fa1a6d67d39adf07d3579a265c579467b6bbca` was the detached chain tip after #316. The tracked worktree was clean at 04:42:11 UTC on 2026-11-28. The port rebuilds the seven commits from `18aa2a695` through `pr/288` at `e6a8cbdb32f53780cc8e673910d3aa34d746094c` on the chain tip.

## Scheduling and archive changes

| Ruling | Location and behavior |
|---|---|
| 1 | `subfleet/retention.py:26` documents parking, yielding, stalls and check 2. `STALL_SLICES` is 3 at line 82. `_Hold` and `RetentionState` at lines 406 and 415 keep slice counts, furthest step, consecutive stalls, tickets and the next ticket number. `subfleet/retention_archive.py:425, 861` initializes and publishes the last builder counters. The builder counts completed tree entries at 1902 and 1914, a Git step at 1828, omitted objects at 2075 and 2084, and copy readbacks at 2274 and 2279. The new counters belong to the builder. |
| 2 | `_Pass.run` builds retirements and counts slots at `retention.py:670`. An in-flight journal holds a slot until it is archived or records a pending rollback (`_holds_slot`, line 854). Selection finds the full eligible set at 676 and queues it at 692. Before the listing, `_make_room` at 821 yields the most-sliced quarantined job whose check 1 passed, provided it has parked at least once since starting. Successful yields leave the listing set at 694. Starts at 703 precede the same holder listing at 720. The archive loop still serves the least recently sliced jobs first, after check 1, and reports archives waiting for check 2 at 774. The pass applies the C-8.7 marks at 626 before selection. |
| 3 | `retention.py:1327` reloads the journal when rollback raises because a failed write can have changed the in-memory object. A journal that still holds a slot retains its hold and slice stamp. Failed yield and stall rollbacks in that state add an error without deferral or advancement for the rollback itself. If reloading also fails after a yield attempt, the job keeps its slot and is skipped for that pass's listing and slices. A known in-flight journal whose recovery fails counts toward the batch and stays out of the listing and slices (line 862). |
| 4 | C-8.4 at `docs/acceptance-contract.md:579` and I17 at `docs/desktop/retention-archive.md:1096` define continuous eligibility, cancellation, the fixed batch and the rollback limit. `_queue` at `retention.py:810` drops stale tickets. A yield records its new ticket before the next cancellation check. |
| 5 | `retention_archive.py:1797` restarts the counter at that tree's start and records each `NotRegenerable` discovery. The parked-slice branch at `retention.py:752` resets stalls and the furthest position after new work reaches a further step or records a finding. Three consecutive stalls request the normal error backoff. |
| 6 | `retention.py:841, 1338` yields with zero seconds and increments `failures` only on the error backoff. A real cached yield followed by one archive error backs off for `DEFER_ERROR_S` (6 h). |
| 7 | `retention.py:819` uses `(job_id not in self.leftovers, queue[job_id])` in `_queue`. The candidate scan retains the base's orphan-lease priority when determining which jobs are eligible under each pool budget. |
| 8 | `retention.py:702` gates only attempts after the first on the deadline. Each attempt consumes its ticket, including a start that defers on `repository discovery deadline`. Existing tests required no deadline assertion changes. |
| 9 | `docs/acceptance-contract.md:9, 579` records the change and C-8.4 scheduling rule. Notice expiry, check-2 catch-up, D-RET-WALK1 and D-HOLDERS-LSOF1 remain in the clause. `docs/desktop/retention-archive.md:333, 872, 883, 889, 901, 914, 1096, 1287` updates step 6, progress and deadlines, I17 and the test index. |
| 10 | This report replaces the evidence for the port. The November 2 report, its probes and its mutant runner stay with the old series. The report-only `5823519f6` records 19 mutants killed and 327 existing tests passing at `e6a8cbdb3`. Those counts describe the old series. |

In the one-listing flow, an earlier archive can finish check 2 in the same listing that serves check 1 of the new start. A new archive reaches `awaiting_check2` and retires in a later pass. A rollback blocked by a live turn frees its slot once the journal records `rollback_pending` and recovery owns the retry. A yield that fails before that write keeps the job eligible for a slice.

The I17 waiting bound is 2(q + 1) passes for a continuously eligible job in a fixed batch, where q is the number of jobs ahead at the end of its first waiting pass. Losing eligibility through protection, deferral or the pool budget resets the wait. Cancellation, a failed yield, interrupted recovery, an unavailable check 1 or a reduction in batch size can delay a start. A new orphan lease may take priority. A cancelled pass can stop after returning a yielded job's bytes and before starting the waiter. The waiter keeps its ticket for the next pass. If a previously tracked holder's journal becomes unreadable, recovery conservatively reserves its last known slot and withholds that job from the listing and slices. An unreadable journal first encountered after restart provides no known slot. The tests exercise recorded and unrecorded rollbacks, and the waiting bound assumes recovery can determine which jobs can proceed. A restart empties in-memory tickets and holds and reconstructs work from journals.

With a failed yield or stall rollback, real archive work or other advancement can still cause a five-second catch-up. With no advancement the existing daemon rearms the hourly interval. The separate zero-work scripted pacing comparison in review r1 is outside the port since the real builder only parks after writing a record.

## Original commits and review findings

| Original commit | Port landing |
|---|---|
| `95a709dc7` | The changing-file starvation regression in `tests/unit/test_retention_batch_fairness.py:79`. |
| `342bfc8ac` | Stall and slot scheduling in `retention.py`, builder progress in `retention_archive.py`, unit regressions and the I17 property. |
| `a65ef370f` | Refused starts at test line 317 and slow readbacks at line 657. |
| `bfe36159c` | Yielded job tickets at test line 343, cancellation before the first slice at line 366, step 4 and the C-8.4 and design text. |
| `a669729b6` | Durable slot accounting and hold retention in `_rollback` and `_holds_slot`. Failed yield, live-turn rollback and real walk progress at test lines 411, 444 and 718. |
| `db1afa04d` | Readback-only progress at test line 690. This report records the port's evidence. |
| `e6a8cbdb3` | Discovery progress at the builder retry and the parked-slice branch. Regenerable-output discovery at test line 772 and the review shape at line 811. |
| `5823519f6` | Old-series evidence only, retained on the backup ref. |

| Review r1 of #288 | Test |
|---|---|
| P2-1 | `test_three_consecutive_nested_repository_discoveries_do_not_stall` uses three real nested Git repositories in regenerable output, with their files hard-linked into an earlier directory. It charges 10 s per classification under a 60 s slice, checks that each of three consecutive parked slices wrote only its discovery record and waits for retirement. |
| P2-2 | `test_repeated_failed_yields_preserve_slice_stamp_and_stall_count` fails actual rollback-pending journal writes. It verifies the slice stamp at the write, accrues stalls through repeat failures, checks no advancement on zero-work passes and verifies the successful rollback reports five stalls. `test_a_yield_whose_rollback_fails_keeps_its_slot` expects two slices on the following yield. |
| P2-3 | `test_a_waiter_that_becomes_protected_loses_its_ticket` verifies a new ticket behind a continuous waiter. `test_cancellation_after_a_yield_keeps_every_byte_and_the_waiters_turn` compares tree snapshots and checks the next pass starts the same waiter. |
| P3-4 | `test_a_yield_does_not_increase_the_backoff_of_the_next_error` checks the next error lasts 6 h and increments the journal to one failure. |

## Baseline evidence

The two new modules count raw passes without the `retention_catch_up` marker. Both ran against base production files before the port. Of 23 cases, 14 failed on behavior, seven on missing names, and two passed. The unchanged check-2 slot behavior and the nested repository case already passed at base. The I17 property shrank to `batch=1`, plans `[("progress", 0), ("done", 0)]` and 24 zeros each for rooms and gaps. Only `job-0` started. Its parked journal occupied the sole slot at every selection. `job-1` reached its fifth waiting pass against the base fallback bound of four, with no deferral. The counterexample was recorded before production edits and the property kept its 2(q + 1) bound.

The first unmutated verification run, `03-port`, passed all 131 selected cases. The marker filter deselected one archive test.

| Test (prefix `test_` omitted) | Base | Port |
|---|---|---|
| `a_real_archive_parks_without_progress_while_a_file_changes_between_slices` | Behavior. The newer job never reached `awaiting_check2`. | PASS |
| `a_stalled_job_backs_off_as_for_an_error` | Missing `STALL_SLICES`. | PASS |
| `review_r1_stub_a_job_parking_without_work_no_longer_holds_the_only_slot` | Behavior. No start for the newer job. | PASS |
| `jobs_that_never_finish_take_turns_in_the_order_they_began_waiting` | Behavior. Only `job-a` started. | PASS |
| `parked_jobs_hold_at_most_batch_minus_one_slots_while_a_job_waits` | Behavior. Both slots stayed occupied and pass two started nothing. | PASS |
| `a_pass_past_its_deadline_still_starts_one_job_beside_parked_ones[starts]` | Behavior. No `job-d` attempt. | PASS |
| `a_pass_past_its_deadline_still_starts_one_job_beside_parked_ones[discovery-defers]` | Behavior. No `job-d` attempt. | PASS |
| `a_start_refused_at_selection_queues_behind_the_others` | Behavior. `job-x` used all four turns. | PASS |
| `a_job_that_yields_queues_ahead_of_one_that_begins_waiting_after` | Behavior. No yield reason for `job-a`. | PASS |
| `a_job_cancelled_before_its_first_slice_keeps_its_slot_for_one` | Missing `RetentionState.holds`. | PASS |
| `a_yield_whose_rollback_fails_keeps_its_slot` | Behavior. No attempted rollback or error. | PASS |
| `a_yield_a_live_turn_holds_up_frees_the_slot_as_recovery_does` | Behavior. No yield reason or pending rollback. | PASS |
| `repeated_failed_yields_preserve_slice_stamp_and_stall_count` | Behavior. No yield attempted, so no rollback error. | PASS |
| `a_waiter_that_becomes_protected_loses_its_ticket` | Missing `RetentionState.queue`. | PASS |
| `cancellation_after_a_yield_keeps_every_byte_and_the_waiters_turn` | Behavior. No yield to trigger cancellation. | PASS |
| `a_yield_does_not_increase_the_backoff_of_the_next_error` | Behavior. Prerequisite yield absent. | PASS |
| `a_job_waiting_for_check2_uses_no_batch_slot` | Pass. Both checks served by one listing and a new start. | PASS |
| `slow_read_backs_are_progress_not_a_stall` | Missing `Retirement.last_reached`. | PASS |
| `a_slice_that_only_reads_copies_back_gets_further` | Missing `Retirement.last_reached`. | PASS |
| `a_walk_begun_again_counts_from_the_tree_start` | Missing `Retirement.last_reached`. | PASS |
| `a_repository_found_in_regenerable_output_is_progress` | Missing `Retirement.last_reached`. | PASS |
| `three_consecutive_nested_repository_discoveries_do_not_stall` | Pass. All three findings and retirement observed. | PASS |
| `every_waiting_job_starts_within_a_bounded_number_of_passes` | Behavior. Fifth waiting pass in the two-job example. | PASS |

## Test commands

The test interpreter was Python 3.15.2 with pytest 9.1.1. The import preflight printed this worktree's `subfleet/__init__.py`. The runner used the following environment and command for each numbered label. The selectors appear below.

```zsh
R="$PWD/build/rbf-port-r1"
export TMPDIR="$R/runtime" HYPOTHESIS_STORAGE_DIRECTORY="$R/hypothesis" PYTHONDONTWRITEBYTECODE=1
export UV_PROJECT_ENVIRONMENT=/Users/maxghenis/subfleet-v2-lanes/archive-offload/.venv PYTHONPATH="$PWD"
export UV_CACHE_DIR="$R/cache" RBF_FAILED_ROOT="$TMPDIR/failed-$label"
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider \
  -p build.rbf-port-r1.isolate_failures --timeout=540 \
  --basetemp "$TMPDIR/pytest-$label" -q -m "not real_lsof and not real_cwd" -ra \
  --junitxml "$R/$label.xml" "${selectors[@]}"
```

The scratch pytest plugin set `tests.failed_keep.ROOT` to `Path(os.environ["RBF_FAILED_ROOT"])` before session startup. That kept failure snapshots and their pruning under the worktree. The first pytest invocation (`01-base`) additionally passed `-n 4`. The supplied environment rejected it as unrecognized. Subsequent invocations omitted it.

- `02-base` selected `tests/unit/test_retention_batch_fairness.py` and `tests/unit/test_retention_batch_fairness_properties.py`, in that order. `03-port` appended `tests/unit/test_retention_archive.py` to those selectors.
- For each mutant the `-cases` run selected the first fairness module and `-property` selected the property module. The `-archive-a` and `-archive-b` selectors were the first and last 54 node IDs of `tests/unit/test_retention_archive.py` in the unmutated `03-port.xml`, in collection order. Each node ID was the module path followed by `::` and the testcase `name`. The marker filter had already deselected one archive test. Each mutant used the same 131 selected cases as `03-port`.
- `05-m2-early-drop-property-retry` and `06-m3-yield-failure-property-retry` each selected only the fairness property module and appended `--timeout=900` with their respective mutant still applied. For mutants 3–5 the property ran alone after the cases and archive shards.
- The end-of-job run labels and their selectors are listed with the final module counts.

The mutations were applied in place, one at a time, against a scratch copy of the verified source. After each mutant's runs `cp -p "$R/retention.verified.py" subfleet/retention.py` restored the original port and `cmp -s subfleet/retention.py "$R/retention.verified.py"` verified it.

The end-of-job runs appended `-o tmp_path_retention_policy=failed` after the module selectors to free passing fixtures' temporary directories during each run. Up to four serial pytest processes ran at once. Each used a distinct guarded basetemp.

## Mutant results

| Mutation | Passed | Failed | Detection |
|---|---:|---:|---|
| `04-m1-no-yield` makes `_make_room` return no yields | 118 | 13 | `test_a_real_archive_parks_without_progress_while_a_file_changes_between_slices` found the newer job started too late. The I17 property failed on the third waiting pass against the two-pass ticket bound. |
| `05-m2-early-drop` discards the hold and stamp before rollback | 129 | 2 | `test_a_yield_whose_rollback_fails_keeps_its_slot` detected a reset slice count. `test_repeated_failed_yields_preserve_slice_stamp_and_stall_count` found the stamp gone during the refused write. |
| `06-m3-yield-failure` increments `failures` on yield | 130 | 1 | `test_a_yield_does_not_increase_the_backoff_of_the_next_error` measured 43,200 s against 21,600 s. |
| `07-m4-check2-slot` makes earlier archives count as slot holders | 121 | 10 | `test_a_job_waiting_for_check2_uses_no_batch_slot` found the waiter did not start. The existing `test_batch_bounds_a_pass_and_takes_the_oldest_first` also failed. |
| `08-m5-ignore-finding` drops the `last_found` progress condition | 129 | 2 | `test_three_consecutive_nested_repository_discoveries_do_not_stall` saw the unchanged tree deferred after three stalls at step 15. The original `test_a_repository_found_in_regenerable_output_is_progress` stalled at step 13. |

The I17 runs for mutants 2 and 3 hit the 540 s limit. The closest later load samples were 53.19 / 70.36 / 56.12 at 05:49:28 UTC and 47.21 / 34.87 / 37.18 at 06:12:41 UTC. Each isolated 900 s retry passed, in 136.21 s and 405.42 s respectively. The table uses the retry results for those properties. Each mutant was killed by an assertion, independent of the timeouts.

## End-of-job verification

The ordinary runs used the required in-worktree basetemps. Two aspects of that setup affected existing tests. Git and the Python scanner could see the checkout above each fixture. Separately, pytest's early fixture cleanup called a patched `os.unlink` after the timer test closed its Store. The timer module passed under default cleanup in `14-timer-retry` (20 cases). In the shared-folders TURN test, an ostensibly plain directory inherited the enclosing checkout, and the sandbox refused a salvage temp write under its Git admin directory.

The isolated Git reruns modeled a fixture outside the enclosing repository. A scratch pytest plugin made Python's `os.stat` and `os.lstat` return ENOENT for the single checkout-root `.git` pointer. A scratch `git` wrapper prepended `$R` to the child process's `GIT_CEILING_DIRECTORIES` and exec'd the usual Git binary. Synthetic repositories and their metadata were real in both approaches. A nested `sandbox-exec` probe to deny reads of the same pointer was refused with `sandbox_apply: Operation not permitted`.

The `15-git-fixtures` attempt installed the Python plugin, but zsh's startup reordered PATH ahead of the wrapper. It returned 74 passed and 15 failed. The corrected `16-git-boundary` invocation used `/bin/zsh -f` and returned 86 passed and three failed. It recovered 11 of the ordinary repository-walk failures and the three dependency-unit failures. `18-shared-fixture` passed both TURN and READER.

The three remaining failures are `test_ambient_config_matches_git_and_forces_census_fallback[includeif-home]`, `[includeif-global]` and `[includeif-xdg]`. Their `gitdir:**/worktrees/**` pattern matches both repositories because the assigned checkout itself is under `.subfleet/worktrees/`. The final assertion expects the plain repository to be outside that pattern. The checkout path is fixed by the brief. All three failed the same assertion in the base-source control, `19-base-includeif`. The ordinary base-source control, `17-base-topology`, also reproduced the three exact dependency-unit failures and the plain-home ambient probe failure before fixture isolation.

Additional selectors and launch options were as follows. The same pytest options and basetemp guards applied.

| Run | Selectors and extra options |
|---|---|
| `14-timer-retry` | `tests/unit/test_timers_retention.py` with default temporary-directory retention. |
| `15-git-fixtures`, `16-git-boundary` | `-p build.rbf-port-r1.isolate_fixture_git` followed by `test_retention_repository_walk.py`, `test_retention_deadline_r2.py` and `test_retention_dependency_units.py`, all under `tests/unit/`. Both prepended `$R/git-bin` to PATH. Run 16 launched the runner with `/bin/zsh -f`. |
| `17-base-topology` | `-p build.rbf-port-r1.use_base_source` and these four nodes in order under `tests/unit/`: `test_retention_dependency_units.py::test_d1_a_linked_checkout_outside_names_the_repository_it_lives_in`, `test_retention_dependency_units.py::test_d1_a_fifo_where_a_gitfile_is_neither_blocks_nor_counts`, `test_retention_dependency_units.py::test_record_spells_each_place_once`, `test_retention_repository_walk.py::test_ambient_config_matches_git_and_forces_census_fallback[plain-home]`. |
| `18-shared-fixture` | The run 16 launch options and `tests/unit/test_retention_shared_folders.py::test_the_look_before_the_workspace_reads_the_folder_the_transaction_records`. |
| `19-base-includeif` | The run 16 launch options plus `-p build.rbf-port-r1.use_base_source`, selecting the three repository-walk includeIf node IDs listed above in home, global, xdg order. |

The base-source plugin imported the exact base `subfleet` package extracted with `git archive f2fa1a6d67d39adf07d3579a265c579467b6bbca subfleet | tar -xf - -C "$R/base-source"`. It verified `subfleet.__file__` pointed inside that scratch copy. Tests remained the files in the assigned worktree.

### Module counts

The table gives pass, fail and skip counts by distinct test node. Failures include teardown errors. Verified counts replace the ordinary results for nodes covered by runs 14, 16 and 18. The ordinary runs yielded **1,238 passed, 19 failed, 0 skipped**. The verified counts are **1,254 passed, 3 failed, 0 skipped**. The required marker filter deselected one archive case. All 31 requested modules ran. The three path-dependent includeIf failures are the remaining blocker to a green suite in this checkout.

| Run | Module | Ordinary P/F/S | Verified P/F/S |
|---|---|---:|---:|
| `09-end-contract` | `tests/fake/test_state_contract.py` | 37/0/0 | 37/0/0 |
| `09-end-contract` | `tests/unit/test_invariants_index.py` | 25/0/0 | 25/0/0 |
| `09-end-contract` | `tests/unit/test_test_module_names.py` | 1/0/0 | 1/0/0 |
| `09-end-contract` | `tests/unit/test_handler_refusals.py` | 388/0/0 | 388/0/0 |
| `09-end-contract` | `tests/unit/test_cli.py` | 256/0/0 | 256/0/0 |
| `10-end-archive` | `tests/unit/test_retention_archive.py` | 108/0/0 | 108/0/0 |
| `10-end-archive` | `tests/unit/test_retention_archive_properties.py` | 2/0/0 | 2/0/0 |
| `10-end-archive` | `tests/unit/test_retention_batch_fairness.py` | 22/0/0 | 22/0/0 |
| `10-end-archive` | `tests/unit/test_retention_batch_fairness_properties.py` | 1/0/0 | 1/0/0 |
| `11-end-fake` | `tests/fake/test_native_maintenance_startup.py` | 44/0/0 | 44/0/0 |
| `11-end-fake` | `tests/fake/test_review130_ab.py` | 12/0/0 | 12/0/0 |
| `11-end-fake` | `tests/fake/test_turn_retention_job_properties.py` | 1/0/0 | 1/0/0 |
| `11-end-fake` | `tests/fake/test_turn_retention_quarantine.py` | 10/0/0 | 10/0/0 |
| `11-end-fake` | `tests/fake/test_retention_recovery_census.py` | 2/0/0 | 2/0/0 |
| `12-end-walk` | `tests/unit/test_retention_listings.py` | 12/0/0 | 12/0/0 |
| `12-end-walk` | `tests/unit/test_retention_listings_properties.py` | 1/0/0 | 1/0/0 |
| `12-end-walk` | `tests/unit/test_review130_pass.py` | 21/0/0 | 21/0/0 |
| `12-end-walk` | `tests/unit/test_timers_retention.py` | 19/1/0 | 20/0/0 |
| `12-end-walk` | `tests/unit/test_retention_regenerable.py` | 26/0/0 | 26/0/0 |
| `12-end-walk` | `tests/unit/test_retention_regenerable_properties.py` | 1/0/0 | 1/0/0 |
| `12-end-walk` | `tests/unit/test_retention_readback.py` | 37/0/0 | 37/0/0 |
| `12-end-walk` | `tests/unit/test_retention_repository_walk.py` | 45/14/0 | 56/3/0 |
| `12-end-walk` | `tests/unit/test_retention_shared_folders.py` | 61/1/0 | 62/0/0 |
| `13-end-edges` | `tests/unit/test_retention_dependency_units.py` | 16/3/0 | 19/0/0 |
| `13-end-edges` | `tests/unit/test_retention_turn_contract.py` | 2/0/0 | 2/0/0 |
| `13-end-edges` | `tests/unit/test_retention_deadline_r2.py` | 11/0/0 | 11/0/0 |
| `13-end-edges` | `tests/unit/test_rte_walk_bundle.py` | 16/0/0 | 16/0/0 |
| `13-end-edges` | `tests/unit/test_retention_sweep_race.py` | 13/0/0 | 13/0/0 |
| `13-end-edges` | `tests/unit/test_retention_81_notes.py` | 38/0/0 | 38/0/0 |
| `13-end-edges` | `tests/unit/test_attempt_tmpdir_retention.py` | 6/0/0 | 6/0/0 |
| `13-end-edges` | `tests/unit/test_retention_dependency_properties.py` | 4/0/0 | 4/0/0 |

## Footprint, deviations and handoff

At 04:42:11 UTC the first command ran `date -u +%FT%TZ && git rev-parse HEAD && git status --porcelain --untracked-files=no`. The test cutoff was 08:12:11 UTC and the job deadline was 08:42:11 UTC. Each row below records the pre-run UTC clock, free space on `/` from `df -H /`, and the three `uptime` load averages. Each preflight checked `~/reviews/fleet-ops/WRITE_STOP` and the 60 GB floor. Scratch is `du -sm "$R"` after pytest and before cleanup. Overlapping runs can share a footprint sample.

| Run | UTC start | Free disk | Load 1m 5m 15m | Scratch MiB |
|---|---|---:|---|---:|
| `01-base` | 2026-11-28T04:47:56Z | 75G | 49.47 37.79 37.23 | — |
| `01-base` | 2026-11-28T04:48:43Z | 76G | 34.25 35.87 36.57 | 1 |
| `02-base` | 2026-11-28T04:49:17Z | 76G | 30.50 34.63 36.09 | 16 |
| `03-port` | 2026-11-28T04:57:05Z | 76G | 22.08 29.60 33.87 | 92 |
| `04-m1-no-yield-archive-a` | 2026-11-28T05:25:54Z | 73G | 23.53 30.49 30.24 | 40 |
| `04-m1-no-yield-archive-b` | 2026-11-28T05:25:54Z | 73G | 23.53 30.49 30.24 | 70 |
| `04-m1-no-yield-cases` | 2026-11-28T05:25:54Z | 73G | 23.53 30.49 30.24 | 60 |
| `04-m1-no-yield-property` | 2026-11-28T05:25:54Z | 73G | 23.53 30.49 30.24 | 51 |
| `05-m2-early-drop-archive-a` | 2026-11-28T05:38:37Z | 73G | 25.10 28.23 29.68 | 40 |
| `05-m2-early-drop-archive-b` | 2026-11-28T05:38:37Z | 73G | 25.10 28.23 29.68 | 70 |
| `05-m2-early-drop-cases` | 2026-11-28T05:38:37Z | 73G | 25.10 28.23 29.68 | 64 |
| `05-m2-early-drop-property` | 2026-11-28T05:38:37Z | 73G | 25.10 28.23 29.68 | 56 |
| `05-m2-early-drop-property-retry` | 2026-11-28T05:52:31Z | 73G | 30.04 49.37 49.79 | 5 |
| `06-m3-yield-failure-archive-a` | 2026-11-28T05:55:00Z | 75G | 22.99 38.51 45.28 | 40 |
| `06-m3-yield-failure-archive-b` | 2026-11-28T05:55:00Z | 75G | 22.99 38.51 45.28 | 70 |
| `06-m3-yield-failure-cases` | 2026-11-28T05:55:00Z | 75G | 22.99 38.51 45.28 | 66 |
| `06-m3-yield-failure-property` | 2026-11-28T06:02:55Z | 73G | 32.03 35.39 40.86 | 5 |
| `06-m3-yield-failure-property-retry` | 2026-11-28T06:14:36Z | 72G | 40.91 39.14 38.75 | 5 |
| `07-m4-check2-slot-archive-a` | 2026-11-28T06:21:42Z | 72G | 30.35 36.08 37.85 | 40 |
| `07-m4-check2-slot-archive-b` | 2026-11-28T06:21:42Z | 72G | 30.35 36.08 37.85 | 70 |
| `07-m4-check2-slot-cases` | 2026-11-28T06:21:42Z | 72G | 30.35 36.08 37.85 | 64 |
| `07-m4-check2-slot-property` | 2026-11-28T06:37:09Z | 72G | 45.44 35.29 35.44 | 5 |
| `08-m5-ignore-finding-archive-a` | 2026-11-28T06:45:29Z | 72G | 29.62 29.58 31.92 | 40 |
| `08-m5-ignore-finding-archive-b` | 2026-11-28T06:45:29Z | 72G | 29.62 29.58 31.92 | 70 |
| `08-m5-ignore-finding-cases` | 2026-11-28T06:45:29Z | 72G | 29.62 29.58 31.92 | 52 |
| `08-m5-ignore-finding-property` | 2026-11-28T06:55:00Z | 72G | 21.86 27.92 29.88 | 5 |
| `09-end-contract` | 2026-11-28T06:59:41Z | 74G | 16.78 22.72 27.02 | 9 |
| `10-end-archive` | 2026-11-28T06:59:41Z | 74G | 16.78 22.72 27.02 | 34 |
| `11-end-fake` | 2026-11-28T06:59:41Z | 74G | 16.78 22.72 27.02 | 8 |
| `12-end-walk` | 2026-11-28T06:59:41Z | 74G | 16.78 22.72 27.02 | 19 |
| `13-end-edges` | 2026-11-28T07:01:59Z | 74G | 26.25 23.87 26.81 | 8 |
| `14-timer-retry` | 2026-11-28T07:09:05Z | 73G | 33.29 31.54 29.58 | 21 |
| `15-git-fixtures` | 2026-11-28T07:10:42Z | 73G | 27.29 29.78 29.09 | 40 |
| `16-git-boundary` | 2026-11-28T07:14:07Z | 74G | 20.63 24.68 26.96 | 40 |
| `17-base-topology` | 2026-11-28T07:17:33Z | 73G | 27.58 23.42 25.70 | 36 |
| `18-shared-fixture` | 2026-11-28T07:18:40Z | 73G | 33.15 25.82 26.43 | 40 |
| `19-base-includeif` | 2026-11-28T07:20:11Z | 73G | 30.19 27.56 27.08 | 21 |

The first `01-base` setup exited before pytest because the scratch wrapper used zsh's reserved `options` variable. The second reached pytest and encountered the missing xdist flag. An early import probe accidentally created an empty local `.venv` skeleton (76 KiB) before the shared-environment variables were set. I removed it before running tests. No dependencies were installed.

The initial `03-port` and `04-m1-no-yield-archive-a` pytest calls passed their selected cases, then the first cleanup routine encountered deliberately read-only restore fixtures. I removed both basetemps with a guarded routine that restored owner permissions only to directories in those test trees. Later runs used that routine at the end of each invocation. The largest sampled scratch footprint was 92 MiB. The final sample before cleanup was 8 MiB. Scratch held the runner, logs, XML, mutation snapshot, pytest basetemps, Hypothesis data, isolation tools and the base-source control. I removed `build/rbf-port-r1` and its empty `build` parent. The read-only shared venv supplied the interpreter throughout verification.

Named staging for the test, implementation, documentation and report steps was refused at the worktree's Git index lock with `Operation not permitted`. HEAD remains at the base. The following files are left in the assigned worktree for salvage.

| File | Bytes |
|---|---:|
| `subfleet/retention.py` | 76,714 |
| `subfleet/retention_archive.py` | 134,898 |
| `tests/unit/test_retention_batch_fairness.py` | 41,278 |
| `tests/unit/test_retention_batch_fairness_properties.py` | 6,836 |
| `docs/acceptance-contract.md` | 1,107,703 |
| `docs/desktop/retention-archive.md` | 121,194 |
| `docs/reports/2026-11-28-retention-batch-fairness-port.md` | 28,848 |
