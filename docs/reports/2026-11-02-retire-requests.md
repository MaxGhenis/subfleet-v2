# Retire requests (C-8.6), 2026-11-02

## Why

Figures read from the hub's machine (the live store, `~/reviews/...` files) are marked *hub, not re-checked*: the cloud session that revised this report on 2026-11-03 could not reach them.

Fleet ops asked whether retention could retire 39 finished Track B jobs now, through its own path (survey, then retire with archives kept for restore), and without deleting anything by hand (d2171). A read-only measure of their trees found 20.36 GB allocated (`~/reviews/subfleet-hub/trackb-retention-2026-11-02/trackb-measure.json`; hub, not re-checked).

Retention in 2.1.12 (C-8.4) retires a pool's oldest unpinned finished jobs, and only while the pool sits over its budget. A read-only query of the live store at 2026-11-02 21:00Z (`trackb_pins_now.py`, below; hub, not re-checked) found:

- all 39 jobs finished (35 `succeeded`, 4 `cancelled`), all of kind `dispatch`;
- none kept by store evidence, and none with a retirement in flight;
- 10,132 finished detached jobs older than the oldest of them (created 2026-10-23T14:26:04Z).

So 2.1.12 reaches these 39 only after it retires those 10,132. It had no way to take named jobs first.

## What C-8.6 adds

- `subfleet retention retire <job>... [--withdraw] [--dry-run] [--reason TEXT]` sends a new daemon op, `retire`. The daemon alone writes the store (C-3.4), so with no daemon the verb records nothing and exits 69. When the answer is lost, the CLI sends the request once more; if that gets no answer, or finds the daemon gone, the verb exits 1 and says the outcome is unknown, since the first request may have been recorded (`Client.call_settled`).
- One transaction answers every named job. For each job it requests or withdraws, it records one event, `job.retire_requested` or `job.retire_withdrawn`, whose data names the job's `created_at` (and the session and reason when given). Events need no schema change, and they outlive a retired job's rows.
- A pass reads the standing requests when it starts. It takes orphaned `retention:` leases' jobs first, requested jobs next and the rest last, oldest first within each. A request lifts only its pool's budget. Every pin, deferral, holder check, the batch of 32, the pass deadline and archive-first retirement still bind.
- A recorded request (not a withdrawal, not a dry run) makes retention due at once, before the daemon reads what holds each job for the answer. A request recorded while a pass runs brings the next pass 5 s after that pass ends, whenever the pass would otherwise rearm the hour: completed, a batch with no advancement, a deadline with no progress, cancelled, or a failed holder scan. A dormant pass retires nothing, so it keeps the hour. A pass that reaches its deadline after progress raises and stays due, so it needs no catch-up.
- The answer names each job's outcome and what holds a requested job now: a retirement in flight, a pin (C-8.4's pin read for that job, conversation pins included), or a deferral. It reads pins job by job for up to 32 jobs and once for every job above that. Only a pass sees a process holding the tree or a job registered in a repository inside it.
- `subfleet retention survey` counts a standing request as a pass would.

The contract text is C-8.6 in `docs/acceptance-contract.md`.

## Invariants

1. **A request lifts the budget and nothing else.** A pass never retires a requested job that a pin, a deferral or an unfinished state keeps, and the batch and deadline bind requested jobs as they bind the rest.
2. **Order.** Orphaned leases' jobs come first, requested jobs next, the rest last; oldest first within each group.
3. **The latest event decides.** A job's latest request or withdrawal decides whether a request stands. A request binds to the `created_at` it names, so it never takes a later job that reuses the id. An event that names no `created_at` changes nothing.
4. **No request, no change.** Without a standing request, a pass gives the result the code before C-8.6 gives, apart from `added_bytes`, which varies from run to run in both trees (below).
5. **Scheduling.** A recorded request makes retention due at once, and a failed read of the answer after the commit cannot undo that. Every way out of `Daemon._retention` that rearms the hour, except a dormant pass, goes through `_rearm_retention(asked, ...)`; a request recorded after the pass read `_retire_asked` makes that rearm 5 s. A dormant pass retires nothing and keeps the hour; a pass that raises leaves retention due. `_last_maintenance` stays the last thing a rearm sets (C-8.4), so a failed status mark leaves retention due.
6. **Idempotence.** Sending a request twice records it once; a dry run records nothing and leaves the schedule alone.
7. **An unknown outcome is never "no daemon".** The CLI exits 69 only when no daemon answered the first request, or the daemon predates the op. When the first answer is lost, it exits 1 whether the resend gets no answer or finds the daemon gone.

| Invariant | Example tests | Property, differential and mutant evidence |
|---|---|---|
| 1 | `test_a_request_never_lifts_a_pin`, `test_a_pinned_request_takes_no_place_in_the_batch`, `test_a_deferred_request_waits_out_its_deferral`, `test_requests_fill_the_batch_and_the_pass_says_more_is_waiting`, `test_the_deadline_binds_requested_jobs`, `test_a_request_in_one_pool_takes_nothing_from_the_other` | Hypothesis oracle `test_a_pass_retires_what_the_rule_says` (40 examples); differential, 183 worlds with requests only on kept jobs (seed 4217); mutants `lifts-pins`, `lifts-deferral`, `lifts-batch`, `lifts-deadline`, `no-lift`, `result-requested-all` |
| 2 | `test_requested_jobs_go_after_leftovers_and_before_the_rest`, control `test_without_a_request_the_oldest_goes_first` | oracle; mutants `ahead-of-leftovers`, `no-priority` |
| 3 | `test_a_request_names_its_job_by_created_at`, `test_a_withdrawn_request_retires_nothing`, `test_a_request_that_names_no_created_at_is_none` (5 shapes), `test_a_request_an_earlier_job_with_the_id_left_does_not_stand` | Hypothesis `test_each_jobs_latest_request_or_withdrawal_decides` (60 examples); mutants `withdrawal-ignored`, `unbound-withdraws`, `not-bound`, `answer-not-bound` |
| 4 | oracle with `requests=False` | differential, 217 worlds with no request (seed 4217) |
| 5 | `test_a_request_makes_retention_due_now_and_nothing_else_does`, `test_a_request_during_a_pass_brings_the_next_pass_to_catch_up` (4 outcomes: completed, deadline, holder scan failed, cancelled), `test_a_pass_with_no_request_after_it_began_waits_the_hour` (4), `test_a_pass_whose_mark_fails_stays_due` (4), `test_a_dormant_pass_waits_the_hour_whatever_arrives`, `test_a_request_while_a_pass_marks_its_end_is_not_lost`, `test_a_request_whose_answer_fails_to_read_still_makes_retention_due` | mutants `not-due`, `withdrawal-due`, `no-catch-up`, `asked-at-end`, `deadline-asked-at-end`, `scan-failed-hour`, `cancelled-hour`, `due-after-answer`, `clock-first`, `mark-unlocked` |
| 6 | `test_a_request_records_one_event_that_names_the_job`, `test_a_dry_run_answers_the_same_and_records_nothing`, `test_each_job_is_answered_once`, `test_a_withdrawal_of_an_unfinished_job_records_nothing` | mutants `dry-run-records`, `already-records`, `unfinished-withdraw-says-withdrawn` |
| 7 | `test_the_cli_says_an_unanswered_request_is_safe_to_send_again`, `test_the_cli_after_a_lost_answer_and_a_gone_daemon_says_the_outcome_is_unknown` (2), `test_the_cli_records_nothing_without_a_daemon` | mutant `lost-then-gone-69` |
| answer, CLI, survey | `test_what_a_request_cannot_take_is_answered_and_nothing_recorded`, `test_the_answer_names_a_pin_a_deferral_and_a_retirement_in_flight`, `test_only_retentions_own_retire_lease_says_retiring`, `test_the_answer_reads_each_job_by_its_own_events`, `test_above_the_limit_the_answer_reads_every_jobs_pins_once` (2), `test_pins_that_could_not_be_read_are_named_and_nothing_else_is_lost`, the CLI tests, `test_the_survey_counts_a_request_as_a_pass_does`, `test_the_survey_takes_a_request_before_the_oldest` | mutants `takes-unfinished`, `hides-pin`, `retiring-any-holder`, `pruned-by-kind`, `pins-each-always`, `pins-once-always`, `unread-silent`, `unread-spreads`, `unread-hides-deferral`, `cli-retired-refused`, `survey-blind`, `survey-unordered` |

## The differential against 2.1.12

`tools/retire_requests_differential.py` builds pairs of identical worlds (up to nine jobs in both pools, any state, some pinned, some deferred, random budgets and batch) and runs one pass of this tree's retention on one and one pass of `retention.py` at `18aa2a695` (2.1.12) on the other. It compares the whole result, with each root spelled `<root>`, apart from the `requested` key C-8.6 adds and `added_bytes` (the result's and each pool's). `added_bytes` counts what the archive's own files add, and it differs by a few bytes between two passes of one world in either tree (the PR #290 review saw 2,481 to 2,486 bytes over six runs of one world; a cloud session saw 2,461 and then 2,460 for differential world 250 on 2026-11-03), so it says nothing about what a pass decides. The manifest names each archived file by its device, inode and mtime, so its length follows those.

The first run stopped at world 11 on one difference (`differential-r1-found-deferral-report.log`). There, a requested job that a deferral holds now shows in the pass's `deferred` and `protected` lists, since the pass looks at it although its pool sits within budget. 2.1.12 never looked at that job. Both passes retired the same jobs. That report is intended, so the tool now sets aside exactly those entries, for requested jobs only, and compares the rest.

| Run | Worlds | Result |
|---|---|---|
| no request | 217 | equal, with `added_bytes` set aside |
| requests only on kept jobs | 183 | equal once 52 deferral reports of requested jobs are set aside (217 jobs retired in all) |
| control: requests on jobs a pass may retire (`--control`) | 400 | 287 differ, so the comparison sees a real difference (599 jobs retired) |
| control against this tree itself (`--control --base HEAD`) | 400 | 0 differ, so the control exits 1: it can fail |

These ran on 2026-11-03 with `--seed 4217 --examples 400`, on Linux and Python 3.12.3, in a cloud session. The first version of this report gave a seed-86 run (164 and 136 worlds, and a control in which 76 of 100 differed) as "identical results" on the hub's machine. That run compared `added_bytes`, so it agreed only because no world's archive happened to differ in size; with seed 4217 the same tool stopped at world 250, a world with no request, on `added_bytes` 2,541 against 2,543. The seed-86 logs (`differential.log`, `differential-control.log`) are the hub's, not re-checked.

## Defects found in the first draft

- **Rearm order.** The first rearm helper set `_last_maintenance` before the status mark, which breaks C-8.4's rule that a failed mark leaves retention due. Three existing tests in `tests/fake/test_native_maintenance_startup.py` failed on it. The helper now holds the request lock across the mark and sets the clock after it. `test_a_pass_whose_mark_fails_stays_due` and `test_a_request_while_a_pass_marks_its_end_is_not_lost` pin both halves.
- **Audit events read as requests.** `store.add_event` writes an audit event of the same kind with `{}` data beside the row. Read as the latest event, it unbound the request before it. The op now writes its rows directly in a `retire.answered` transaction with no audit, and the reader treats an event that names no `created_at` as no request.
- **A pin read for every job named.** The answer ran C-8.4's pin read once per job. A read-only measure on the live store at 2026-11-02 21:10Z (`pin_read_cost.py`, `pin-read-cost.json`; hub, not re-checked) found 0.11 s for one job (5 jobs read) and 3.5 s for all 14,848 jobs at once, since most of the read's queries cover every job whatever it names. A request naming 500 jobs would have taken about 55 s, past the client's 15 s timeout. The answer now reads job by job for up to 32 jobs (`RETIRE_PIN_READ_EACH`) and once above that, so it stays near 3.5 s at today's size, and it reads the conversation pins once per answer. `test_above_the_limit_the_answer_reads_every_jobs_pins_once` checks both paths give one answer.
- **A failed pin read hid the deferral.** When the pin read failed, the first draft named `pins_unread` and skipped the deferral. The answer now names both, and one job's failed read leaves the other jobs' pins named (`test_pins_that_could_not_be_read_are_named_and_nothing_else_is_lost`).

## Defects the PR #290 review found

An independent cloud review of 16c48717 found these; the next commit fixes each, with the review's failing case as a test.

- **A request waited the hour behind a failed holder scan or a cancelled pass (P2, P3).** Both branches of `Daemon._retention` still marked the hour and set `_last_maintenance` themselves, outside `_retire_lock`, so a request recorded during the pass lost its catch-up. They now rearm through `_rearm_retention(asked, error=...)`. After a failed scan, a request costs one more pass 5 s later; that pass's own rearm goes back to the hour unless another request arrives.
- **A failed read of the answer lost the wake (P3).** `_retire_annotate` reads the `retire:` lease once per row, outside any `try`, and ran before the due-now. `_retire` now makes retention due first.
- **A lost answer, then no daemon, exited 69 (P3).** `Client.call_settled` re-raised `DaemonUnavailable` on the resend for every op but `submit`. `retire` now joins `submit` there, so the CLI exits 1 and says the outcome is unknown. `kill` still re-raises, since its CLI falls back to offline mode (C-17.5).
- **The differential compared noise (P3).** It now sets `added_bytes` aside (above).
- **Four rules had no mutant (P3).** `lifts-deadline`, `retiring-any-holder`, `survey-unordered` and `unfinished-withdraw-says-withdrawn` survived the review's extra mutants. Each now has a test that kills it.

## Reads stay indexed

The answer runs inside the request's write transaction, so its reads must stay small. A read-only plan on the live store at 2026-11-02 21:17:52Z (`plan_read_cost.py`, `plan-read-cost.json`; hub, not re-checked) showed that the first draft's lookup of a retired job's `retention.pruned` event plans on `events_kind`: for an id with no rows, SQLite walks all 4,777 retirements recorded so far, 1.8 ms each (mean of 50), or about 0.9 s for a request naming 500 such ids, and that cost grows with every retirement. With `+kind`, the house idiom (`dependencies.py`, `Daemon._batches`), it plans on `events_job` and takes 0.002 ms. The per-job request read already planned on `events_job`; `+kind` pins that too. A pass reads every request by kind on `events_kind`, then sorts the few rows it finds. `test_the_answer_reads_each_job_by_its_own_events` and `test_the_request_reads_stay_indexed` check the plans, and the old lookup fails the first.

## Test runs

The first three runs below ran on the hub's machine, from `~/subfleet-v2-lanes/retire-request`, serial (no xdist), under the shared suite lock, with private `--basetemp` folders. Their logs sit in `~/reviews/subfleet-hub/retire-request-2026-11-02/` (hub, not re-checked).

| Run | Files | Result |
|---|---|---|
| `targeted-r1.log` | the two new files and 21 retention, protocol, refusal and borrowed-method files | 1,395 passed, 7 failed in 411 s: 3 from the rearm order above; 4 in `test_fake_daemon_borrowed_methods.py`, which read method source by line number after `daemon.py` changed mid-run |
| `targeted-r2.log` | the 2 failing files, the 2 new files and 3 schedule files | 179 passed in 34.6 s |
| new files, after the pin-read change | `tests/unit/test_retention_retire_requests.py`, `tests/fake/test_retire_request_daemon.py` | 53 passed in 13.2 s |

A cloud session ran these after the review's fixes, on 2026-11-03, on Linux with Python 3.12.3 (3.14 was not installed), serially, with `--basetemp` under `/root`:

| Files | Result |
|---|---|
| the two files above | 68 passed |
| those two and 14 neighbours: `tests/fake/test_native_maintenance_startup.py`, `test_review130_ab.py`, `test_retention_dormant.py`; `tests/unit/test_retention_archive.py`, `test_retention_archive_properties.py`, `test_daemon_settle.py`, `test_fake_daemon_borrowed_methods.py`, `test_outcome_unknown.py`, `test_stopped_daemon.py`, `test_protocol_registry.py`, `test_timers_retention.py`, `test_settle_properties.py`, `test_review130_pass.py`, `test_daemon_refusals_cli.py` | 621 passed, 3 failed, 1 skipped. The 3 (`test_temporary_directory_source_is_scratch_by_default`, `test_an_unmeasurable_job_is_decided_and_backs_off`, `test_the_space_the_archive_adds_is_reported_wherever_freed_bytes_are`, all in `test_retention_archive.py`) fail at `18aa2a695` too, on the same VM, so they are the platform's |
| contract guards and refusals: `tests/fake/test_state_contract.py`, `tests/unit/test_test_module_names.py`, `tests/unit/test_invariants_index.py`, `tests/unit/test_handler_refusals.py` | 427 passed |

CI runs the whole suite on macOS on the pull request.

## Mutants

`tools/retire_request_mutants.py` names 38 mutants, one per broken rule; each swaps one function in memory for a copy with one edit and leaves the source alone. Each edit matches its function once and compiles. The first 27 came with C-8.6. The review added 8 through a plugin of its own and found 4 survived; 7 of its 8 are now in the tool (its eighth, `requested-newest-first`, was an equivalent rewrite kept as a control, which stays out). The last 4 break the rules the review's fixes add: `scan-failed-hour`, `cancelled-hour`, `due-after-answer` and `lost-then-gone-69`.

The PR #290 review ran the first 27 at 16c48717 and killed all 27. The cloud session's first full run of the 38, on 2026-11-03, killed 37. `lifts-pins` survived: `_start` reads a job's pins again inside its transaction, so a pin the selection lifts still retires nothing, and only the Hypothesis oracle sees the batch place the pinned job takes, on the examples it happens to draw. `test_a_pinned_request_takes_no_place_in_the_batch` now kills it every run. The run after that test, on Linux and Python 3.12.3: baseline 68 passed, then 38 killed and 0 survived.

## The live read

`trackb_pins_now.py` opens the live store with `mode=ro` and `query_only`, reads no tree, and runs C-8.4's pin read once over every job. It cannot see the conversation service's in-memory pins (these jobs are dispatch jobs, which those pins never name) or the daemon's deferrals. Output: `trackb-pins-now.json`.

## Known limits

- **A failed read of the answer still fails the reply.** The request is recorded and retention is due, but the client gets an error for the request. Run again, it answers `already-requested`. Only the per-row `retire:` lease read can raise; the pin read names its failure in `pins_unread`.
- **A withdrawal recorded after a pass has read the requests does not stop that pass from retiring the job.** C-8.6 says this only for a retirement already in flight.
- **The survey is not a pass.** It has no orphan-lease rank, no deferrals (daemon memory) and no batch, so with an orphan lease in an over-budget pool it can name one different older job.
- **A probe holder whose events span two jobs splits the pin read.** `probe_tmpdir_rows` with `job_id=` takes the latest event per holder among that job's events only. No writer makes such a holder (`probe:<random token>`, its job fixed in the record), so the answer and a pass agree in practice. This is the review's reading, shown with a hand-made store; the cloud session did not re-read `probe_tmpdir_rows`.
- **The differential's worlds cover no orphan leases, retirements in flight, holders, errors or worktrees.** Unit tests cover orphan order; the rest rests on reading the code.
- **No stress test of the lock order.** The review read `_retire_lock`, `timers._lock` and the store lock and found no cycle; nothing here runs them under load.

## Integration notes

- PR #288 rewrites the selection loop into eligible jobs, FIFO tickets and a yield. Its ticket order sorts leftovers first (`job_id not in self.leftovers`); with C-8.6 that key becomes `self._rank(job_id)`. Whichever merges second makes that change.
- PR #289 also edits `Daemon._retention`; its hunks sit beside these, and the second to merge resolves them.
- Installing needs one daemon restart, in the same release as #289.
