# D-DAEMON-QUIET1 — daemon work per live job

Work began 2026-11-07 13:46 UTC in the assigned detached workspace at a8624a4899a2. No live state or caller workspace was used. Both changes, their tests, contract updates and reproducible evidence are complete. The final regression passed: 617 passed, 5 skipped. Git denied `index.lock`, so no commits were created.

## Change 1: bounded first-submission lookup

Both batch-label callers now share `store.batch_submission_rows`, which bounds each statement to 500 distinct ids and seeks the first `job.submitted` through `events_job(job_id,event_id)` before reading its payload. Later events do not add work.

The only production writer of `job.submitted` is `Daemon.submit`: insertion of the unique job row and submission event share one transaction; an accepted retry returns without another event. Crashes cannot expose the job without its submission. Generic store APIs can fabricate duplicate submissions. There is also a rare supported route to duplicate history: `ids.job_id` uses a one-second timestamp and slug, checking only current job rows; retention archives the folder and deletes the job row while preserving events. Reusing that id after a clock rollback (or completion/pruning/resubmission within the same second) can append another submission. Normal retry/resume does not. The first submission supplies the label, matching `_submitted`, dependencies, detached-folder and offline readers, which already treat the first event as the identity of a job id. This explicitly changes the old scan's last matching-batch result for duplicate histories, including id reuse. A first submission without a batch does not borrow one from a later duplicate. The audit covered `Daemon.submit`, generic `Store.transaction`/`add_event`/`add_job`, the importer, `ids.job_id` and `retention_archive`'s row deletion; no other production submission writer exists. `Store.add_event("job.submitted")` also emits an empty submission audit row; the old payload filter and first-event lookup agree for that pair.

`_batches` keeps truthy batch values and still lets invalid JSON raise. `attach_batches` keeps only dictionaries (including an empty dictionary) and still parses `data_json or "{}"`. The helper returns raw rows; it does not merge these meanings. The old `LIKE` payload prefilter remains after selection of the first submission, retaining existing parsing behavior for a single submission.

Native submission cannot create a job without `job.submitted`, but every v1 import intentionally lacks it: `import_runs` writes `job.imported`, `attempt.imported` and `import.run`. Unknown ids and hand-built/damaged histories can also lack one. The indexed first-submission search then walks that job's events and returns no label. Unchanged import passes are skipped, live imports are never adopted, and these jobs do not accumulate the daemon's per-inspection observation events. Their import history remains a possible scan; no extra schema/index maintenance is warranted for this bounded legacy case. The live prevalence of imported or damaged jobs was not measured, in keeping with the live-state prohibition.

## Change 2: paced liveness durability

`LIVENESS_WRITE_S=60` paces only otherwise unchanged `attempt.observed_alive` writes. Each attempt remembers the monotonic completion time of its last successfully committed observation; identity writes always carry their newest observation and refresh this pace. A failed transaction changes neither the pace nor the table deduplication record nor the caller's evidence snapshot. A restart forgets pace times and saves its first fresh observation. Terminal paths and control-loop pruning remove entries. Saved timestamps are clamped against the latest durable value to remain nondecreasing even if the wall clock moves backwards.

Inspection and its rationing, distinct-table aging, ownership event number/order, receipt/loss/exit decisions and live wall checks are unchanged.

### Readers and charge bound

A worktree-wide exact-key search finds one production reader: `Daemon._lost` reads `evidence_json.last_observed_alive_at` as the receipt-less lost attempt's `finished_at`, falling back to its start for legacy evidence. It clamps a future endpoint to now. `_finalize` preserves that endpoint; `_running_s` and the live-tick running-time SQL consume `started_at`/`finished_at`, not the liveness key. Receipt-based finalization uses the receipt's exit time. Inspection and `_enforce_wall` still decide current liveness from process evidence. C-5.12 and D-WALL1 describe the durable observation and conservative loss endpoint; neither requires subminute persistence. Generic evidence display can expose the saved timestamp but does not use it for another decision.

The direction is undercharging only: a receipt-less lost attempt may spend up to 60 s less of `max_wall_s` than saving every inspection. The total age of a saved observation, for an attempt alive across a full period with successful inspections/writes, is bounded by 60 s plus one inspection interval. Downtime, failed inspections or refused writes cannot promise that freshness; this existing operational limitation does not change live wall enforcement. No reader requires finer persistence, so Change 2 is included.

## Invariants and tests

1. Caller compatibility: `test_batch_callers_preserve_old_payload_filters_and_errors` compares the old SQL and parsing for absent submissions, no batch, all JSON batch types, malformed JSON, nonobject JSON, nested/escaped keys and unknown ids. `test_empty_batch_listing_runs_no_statement` covers no ids. `test_batch_lookup_matches_old_statement_for_generated_jobs_and_event_orders` runs 150 Hypothesis examples for both callers. Duplicate histories are the explicit exception, pinned by `test_first_submission_defines_batch_even_if_a_malformed_store_repeats_it`.
2. Work independent of later events: `test_later_events_do_not_increase_batch_lookup_steps` uses `Connection.set_progress_handler` for 10 versus 10,000 later events, with and without batch metadata. New lookup costs 44→35 / 47→38 VM steps (the first execution includes setup); old SQL costs 74→50,020 / 77→50,023. `test_batch_lookup_bounds_each_statement_to_500_ids_and_uses_events_job` checks 1,203 ids, duplicate ids, chunk sizes and the query plan.
3. Nondecreasing saved observation: `test_liveness_wall_clock_rollback_never_moves_saved_observation_backwards` covers standalone and coalesced writes.
4. Freshness: `test_liveness_age_stays_within_one_minute_plus_inspection_interval` checks 300 inspections at 1.3 s spacing; `test_liveness_writes_at_the_minute_boundary_and_keeps_writing` checks both sides of the boundary and successive minutes. `test_liveness_pace_starts_after_a_slow_successful_commit` anchors the pace to commit completion. `test_ownership_write_resets_the_liveness_pace_with_newest_observation`, `test_liveness_pacing_is_independent_for_each_attempt` and `test_liveness_first_fresh_observation_after_restart_writes` cover coalescing, per-attempt clocks and restart.
5. Ownership events unchanged: `test_ownership_events_match_the_original_flow_and_coalesce_fresh_observations` compares number, order and payload to a frozen original `_record_owned` across ownership, counted-identity and expiry changes. Existing census and wall tests retain inspection/decision coverage.
6. Finished entries removed: `test_ended_attempts_leave_no_liveness_pacing_entry` covers success, loss, quarantine and control-loop pruning; `test_terminal_attempt_worker_also_drops_liveness_pacing_entry` covers a terminal row encountered by its worker. Unlaunched and quarantine-release paths also remove entries immediately after their ending transaction.

`test_failed_liveness_commit_retries_the_same_table_and_observation` injects failure after the UPDATE but before commit and verifies rollback, unchanged pace/table/caller snapshot, and successful retry. `test_repeated_table_cannot_advance_saved_liveness_even_after_minute` preserves the distinct-table gate.

## Measurements

Synthetic fixture: 100 open jobs × 5,000 events = 500,000 job events, 50 batch labels; database 88,985,600 bytes. Actual list dispatch and `Timers.publish_status` (including file publication) run on minimal cores without daemon startup, lanes or conversations. Baseline functions are extracted from a8624a4899a2 and run in the same runtime. Figures are medians of five warmed repetitions.

| Operation | Before | After |
|---|---:|---:|
| List | 0.428638 s | 0.003645 s |
| Status write | 0.510169 s | 0.005778 s |
| List SQLite page-cache misses | 13,081 | 271 |
| Status SQLite page-cache misses | 13,073 | 263 |
| `attempt.observed_alive` commits/attempt/hour | 3,600 | 60 |

Page misses replay each operation's traced SQL in a fresh 2,000 KiB SQLite cache; they count page loads from the OS, not physical disk reads. The OS cache was retained. Python 3.14.7 uses SQLite 3.53.1; page replay uses the system SQLite 3.51.0 shell. Fake-hour liveness uses distinct tables each second in `[0, 3600)` with preseeded unchanged ownership: 98.33% fewer standalone observation commits, with final saved time 14:24:59 before versus 14:24:00 after. No measurement sized or accessed live daemon state.

Scripts and raw evidence are in `docs/reports/2026-11-06-daemon-quiet-data/`: `measure.py`, `results.json`, `batch-vm-steps.json`, mutation scripts/logs, `commands.json` with complete command lines, and the final regression log.

## Mutants

Every mutant runs only in an isolated temporary copy. No mutant was applied to the working production files.

| Mutant | Test that kills it | Result |
|---|---|---|
| Walk every event again | `test_later_events_do_not_increase_batch_lookup_steps` | 2 expected assertion failures |
| Last submission instead of first | `test_first_submission_defines_batch_even_if_a_malformed_store_repeats_it` | 4 expected assertion failures |
| No liveness pace | `test_liveness_writes_at_the_minute_boundary_and_keeps_writing` | 1 expected assertion failure |
| Never write after first | `test_liveness_age_stays_within_one_minute_plus_inspection_interval` | 1 expected assertion failure |
| Count failed write as written | `test_failed_liveness_commit_retries_the_same_table_and_observation` | 1 expected assertion failure |
| Repeated table moves observation | `test_repeated_table_cannot_advance_saved_liveness_even_after_minute` | 1 expected assertion failure |
| Keep finished attempt entry | `test_ended_attempts_leave_no_liveness_pacing_entry` | 3 expected assertion failures |

## Commands and results

All uv/test commands used `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR=/private/tmp/daemon-quiet-build/uv-cache` and explicit `--cache-dir /private/tmp/daemon-quiet-build/uv-cache`; each pytest run used a new `--basetemp` under that permitted child.

- `uv sync --group dev`: passed, 9 packages installed.
- `uv run --no-sync pytest tests/unit/test_batch_submission_queries.py tests/unit/test_store_contention_queries.py tests/unit/test_status_json.py --basetemp /private/tmp/daemon-quiet-build/batch-first-1 -q`: 318 passed in 402.89 s. The new batch module contributes 50 cases.
- `uv run --no-sync pytest tests/unit/test_liveness_write_pacing.py tests/unit/test_census_merge_liveness.py tests/unit/test_daemon_settle.py --basetemp /private/tmp/daemon-quiet-build/liveness-plus-existing --tb=short -q`: 83 passed in 64.47 s.
- After adding the slow-commit case and anchoring time after commit, `uv run --no-sync pytest tests/unit/test_liveness_write_pacing.py --basetemp /private/tmp/daemon-quiet-build/liveness-final --tb=short -q`: 14 passed in 34.55 s. The new test first caught the precommit-clock implementation; the corrected implementation passed.
- Final regression: **617 passed, 5 skipped in 1270.80 s (21:10)**. Used `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -n 4`, all 13 files required by the brief plus the two new modules, `--basetemp /private/tmp/daemon-quiet-build/final-regression-1 --tb=short -q`.
- `uv run --no-sync python docs/reports/2026-11-06-daemon-quiet-data/measure.py --scratch-root /private/tmp/daemon-quiet-build --output docs/reports/2026-11-06-daemon-quiet-data/results.json`: passed. Batch results used `--skip-liveness`; the corrected fake-hour run used `--liveness-only` to retain batch samples. The final postcommit-clock implementation was checked again with `--liveness-only` and passed both count assertions.
- `uv run --no-sync python docs/reports/2026-11-06-daemon-quiet-data/check_batch_mutants.py`: passed its harness assertions; both mutants exited 1 with the expected 2 and 4 assertion failures.
- `uv run --no-sync python docs/reports/2026-11-06-daemon-quiet-data/liveness_mutants.py --temp-root /private/tmp/daemon-quiet-build`: passed its harness assertions; all five mutants exited 1, with seven expected assertion failures. Logs and `liveness-mutants/verified-results.json` are saved in the worktree. All seven required mutants were killed; none survived.
- `git diff --check`: passed. There is no new durable event/record key and no new refusal, so no new-key exact-compare audit or handler-refusal run was needed.

The final regression covers `tests/unit/test_status_json.py`, `test_store_contention_queries.py`, `test_census_merge_liveness.py`, `test_daemon_settle.py`, `test_review130_pass.py`, `test_daemon_verbs.py`, `test_test_module_names.py`, `test_invariants_index.py`; `tests/fake/test_wall_running_time.py`, `test_wall_inspection_order.py`, `test_review_r223_wall_probes.py`, `test_native_maintenance_startup.py`, `test_state_contract.py`; and both new modules. CI retains responsibility for the full suite.

## Commits and deviations

Git did not permit staging or committing: `git add -- subfleet/store.py subfleet/status_json.py tests/unit/test_batch_submission_queries.py` and both intended commit steps failed because the sandbox denied creation of the worktree's `index.lock`. An intervening explicit-path commit also refused the untracked test after staging failed. The two intended independent commits are (1) the batch helper/callers/tests and batch evidence, and (2) liveness pacing/tests, acceptance-contract corrections and the completed report. Their implementation boundaries remain distinct in the files/diff; no history was rewritten and nothing was pushed. The supplied checkout remains detached; the named branch is checked out elsewhere. Subfleet can collect all workspace changes as instructed.

Deviations: no commits could be created under the supplied git permissions. The benchmark's first temporary-directory attempt was denied under Darwin TMPDIR, so its allowed child is `/private/tmp/daemon-quiet-build`. An initial benchmark-only fake-clock binding error and an initial mutant-harness missing parent directory were corrected and those measurements/runs repeated; neither affected production code. The full generic-writer/importer/archive audit finished during validation after the initial helper edit; its import and id-reuse findings are included in the final rationale and tests. The intentional first-submission behavior for duplicate/reused-id histories is detailed above.
