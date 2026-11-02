# Retire requests (C-8.6), 2026-11-02

## Why

Fleet ops asked whether retention could retire 39 finished Track B jobs now, through its own path (survey, then retire with archives kept for restore), and without deleting anything by hand (d2171). A read-only measure of their trees found 20.36 GB allocated (`~/reviews/subfleet-hub/trackb-retention-2026-11-02/trackb-measure.json`).

Retention in 2.1.12 (C-8.4) retires a pool's oldest unpinned finished jobs, and only while the pool sits over its budget. A read-only query of the live store at 2026-11-02 21:00Z (`trackb_pins_now.py`, below) found:

- all 39 jobs finished (35 `succeeded`, 4 `cancelled`), all of kind `dispatch`;
- none kept by store evidence, and none with a retirement in flight;
- 10,132 finished detached jobs older than the oldest of them (created 2026-10-23T14:26:04Z).

So 2.1.12 reaches these 39 only after it retires those 10,132. It had no way to take named jobs first.

## What C-8.6 adds

- `subfleet retention retire <job>... [--withdraw] [--dry-run] [--reason TEXT]` sends a new daemon op, `retire`. The daemon alone writes the store (C-3.4), so with no daemon the verb records nothing and exits 69.
- One transaction answers every named job. For each job it requests or withdraws, it records one event, `job.retire_requested` or `job.retire_withdrawn`, whose data names the job's `created_at` (and the session and reason when given). Events need no schema change, and they outlive a retired job's rows.
- A pass reads the standing requests when it starts. It takes orphaned `retention:` leases' jobs first, requested jobs next and the rest last, oldest first within each. A request lifts only its pool's budget. Every pin, deferral, holder check, the batch of 32, the pass deadline and archive-first retirement still bind.
- A recorded request (not a withdrawal, not a dry run) makes retention due at once. A request recorded while a pass runs brings the next pass 5 s after that pass ends, in every outcome that would otherwise rearm the hour.
- The answer names each job's outcome and what holds a requested job now: a retirement in flight, a pin (C-8.4's pin read for that job, conversation pins included), or a deferral. Only a pass sees a process holding the tree or a job registered in a repository inside it.
- `subfleet retention survey` counts a standing request as a pass would.

The contract text is C-8.6 in `docs/acceptance-contract.md`.

## Invariants

1. **A request lifts the budget and nothing else.** A pass never retires a requested job that a pin, a deferral or an unfinished state keeps, and the batch and deadline bind requested jobs as they bind the rest.
2. **Order.** Orphaned leases' jobs come first, requested jobs next, the rest last; oldest first within each group.
3. **The latest event decides.** A job's latest request or withdrawal decides whether a request stands. A request binds to the `created_at` it names, so it never takes a later job that reuses the id. An event that names no `created_at` changes nothing.
4. **No request, no change.** Without a standing request, a pass gives exactly the result the code before C-8.6 gives.
5. **Scheduling.** A recorded request makes retention due at once. A request recorded during a pass is never left waiting the hour by that pass's rearm. `_last_maintenance` stays the last thing a rearm sets (C-8.4), so a failed status mark leaves retention due.
6. **Idempotence.** Sending a request twice records it once; a dry run records nothing and leaves the schedule alone.

| Invariant | Example tests | Property, differential and mutant evidence |
|---|---|---|
| 1 | `test_a_request_never_lifts_a_pin`, `test_a_deferred_request_waits_out_its_deferral`, `test_requests_fill_the_batch_and_the_pass_says_more_is_waiting`, `test_a_request_in_one_pool_takes_nothing_from_the_other` | Hypothesis oracle `test_a_pass_retires_what_the_rule_says` (40 examples); differential, 136 worlds with requests only on kept jobs; mutants `lifts-pins`, `lifts-deferral`, `lifts-batch`, `no-lift` |
| 2 | `test_requested_jobs_go_after_leftovers_and_before_the_rest`, control `test_without_a_request_the_oldest_goes_first` | oracle; mutants `ahead-of-leftovers`, `no-priority` |
| 3 | `test_a_request_names_its_job_by_created_at`, `test_a_withdrawn_request_retires_nothing`, `test_a_request_that_names_no_created_at_is_none` (5 shapes), `test_a_request_an_earlier_job_with_the_id_left_does_not_stand` | Hypothesis `test_each_jobs_latest_request_or_withdrawal_decides` (60 examples); mutants `withdrawal-ignored`, `unbound-withdraws`, `not-bound`, `answer-not-bound` |
| 4 | oracle with `requests=False` | differential, 164 worlds with no request |
| 5 | `test_a_request_makes_retention_due_now_and_nothing_else_does`, `test_a_request_during_a_pass_brings_the_next_pass_to_catch_up` (2 outcomes), `test_a_pass_with_no_request_after_it_began_waits_the_hour` (2), `test_a_pass_whose_mark_fails_stays_due` (2), `test_a_request_while_a_pass_marks_its_end_is_not_lost` | mutants `not-due`, `withdrawal-due`, `no-catch-up`, `asked-at-end`, `clock-first`, `mark-unlocked` |
| 6 | `test_a_request_records_one_event_that_names_the_job`, `test_a_dry_run_answers_the_same_and_records_nothing`, `test_each_job_is_answered_once` | mutant `dry-run-records` |
| answer, CLI, survey | `test_what_a_request_cannot_take_is_answered_and_nothing_recorded`, `test_the_answer_names_a_pin_a_deferral_and_a_retirement_in_flight`, `test_the_answer_reads_each_job_by_its_own_events`, the six CLI tests, `test_the_survey_counts_a_request_as_a_pass_does` | mutants `takes-unfinished`, `hides-pin`, `pruned-by-kind`, `cli-retired-refused`, `survey-blind` |

## The differential against 2.1.12

`tools/retire_requests_differential.py` builds pairs of identical worlds (up to nine jobs in both pools, any state, some pinned, some deferred, random budgets and batch) and runs one pass of this tree's retention on one and one pass of `retention.py` at `18aa2a695` (2.1.12) on the other. It compares the whole result, with each root spelled `<root>`, apart from the `requested` key C-8.6 adds.

The first run stopped at world 11 on one difference (`differential-r1-found-deferral-report.log`). There, a requested job that a deferral holds now shows in the pass's `deferred` and `protected` lists, since the pass looks at it although its pool sits within budget. 2.1.12 never looked at that job. Both passes retired the same jobs. That report is intended, so the tool now sets aside exactly those entries, for requested jobs only, and compares the rest.

| Run | Worlds | Result |
|---|---|---|
| no request | 164 | identical results |
| requests only on kept jobs | 136 | identical once 27 deferral reports of requested jobs are set aside |
| control: requests on jobs a pass may retire (`--control`) | 100 | 76 differ, so the comparison sees a real difference |

The pairs ran at 2026-11-02 about 21:00Z with seed 86; logs: `differential.log`, `differential-control.log`.

## Two defects the tests caught in the first draft

- **Rearm order.** The first rearm helper set `_last_maintenance` before the status mark, which breaks C-8.4's rule that a failed mark leaves retention due. Three existing tests in `tests/fake/test_native_maintenance_startup.py` failed on it. The helper now holds the request lock across the mark and sets the clock after it. `test_a_pass_whose_mark_fails_stays_due` and `test_a_request_while_a_pass_marks_its_end_is_not_lost` pin both halves.
- **Audit events read as requests.** `store.add_event` writes an audit event of the same kind with `{}` data beside the row. Read as the latest event, it unbound the request before it. The op now writes its rows directly in a `retire.answered` transaction with no audit, and the reader treats an event that names no `created_at` as no request.

## Reads stay indexed

The answer runs inside the request's write transaction, so its reads must stay small. A read-only plan on the live store at 2026-11-02 21:20Z showed that the first lookup of a retired job's `retention.pruned` event planned on `events_kind`: for an id with no rows, SQLite walked all 4,757 retirements recorded so far, 3.18 ms each (mean of 50), or about 1.6 s for a request naming 500 such ids, and that cost grows with every retirement. With `+kind`, the house idiom (`dependencies.py`, `Daemon._batches`), it plans on `events_job` and takes no measurable time. The per-job request read already planned on `events_job`; `+kind` pins that too. A pass reads every request by kind on `events_kind`, then sorts the few rows it finds. `test_the_answer_reads_each_job_by_its_own_events` and `test_the_request_reads_stay_indexed` check the plans, and the old lookup fails the first.

## Test runs

All from `~/subfleet-v2-lanes/retire-request`, serial (no xdist), under the shared suite lock, with private `--basetemp` folders. Logs sit in `~/reviews/subfleet-hub/retire-request-2026-11-02/`.

| Run | Files | Result |
|---|---|---|
| `targeted-r1.log` | the two new files and 21 retention, protocol, refusal and borrowed-method files | 1,395 passed, 7 failed in 411 s: 3 from the rearm order above; 4 in `test_fake_daemon_borrowed_methods.py`, which read method source by line number after `daemon.py` changed mid-run |
| `targeted-r2.log` | the 2 failing files, the 2 new files and 3 schedule files | 179 passed in 34.6 s |
| final new files | `tests/unit/test_retention_retire_requests.py`, `tests/fake/test_retire_request_daemon.py` | 50 passed in 12.4 s |

CI runs the whole suite on the pull request.

## Mutants

`tools/retire_request_mutants.py` names 22 mutants, one per broken rule; each swaps one function in memory for a copy with one edit and leaves the source alone. Each edit matches its function once and compiles. Fleet ops held serial mutant runs until the Microcosm US release starts, so the runs come after that, and their results go into this report.

## The live read

`trackb_pins_now.py` opens the live store with `mode=ro` and `query_only`, reads no tree, and runs C-8.4's pin read once over every job. It cannot see the conversation service's in-memory pins (these jobs are dispatch jobs, which those pins never name) or the daemon's deferrals. Output: `trackb-pins-now.json`.

## Integration notes

- PR #288 rewrites the selection loop into eligible jobs, FIFO tickets and a yield. Its ticket order sorts leftovers first (`job_id not in self.leftovers`); with C-8.6 that key becomes `self._rank(job_id)`. Whichever merges second makes that change.
- PR #289 also edits `Daemon._retention`; its hunks sit beside these, and the second to merge resolves them.
- Installing needs one daemon restart, in the same release as #289.
