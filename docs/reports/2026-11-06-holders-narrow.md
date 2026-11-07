# Retention lists open files less, and less often (D-HOLDERS-LSOF1)

Build job `20261106-140429-holders-narrow-build`, from release/217 at a8624a489.
The job started at 05:01 UTC on 2026-11-07, and the report was written by 07:45 UTC.

## Outcome

| Change | State | Commits |
|---|---|---|
| 1. Count and time each listing | Done | 9218a8732 |
| 2. One listing a pass | Done | 591d8960f (the harness's auto-commit, see deviations) and 1bcc18327 |
| 3. A narrower reader | Measured and left out: the gate failed | none in `subfleet/`; the prototype and its measurements are in this report's folder |

Git let me commit. Every commit went onto the worktree's detached HEAD, because
`fix/holders-narrow-217` is checked out in the caller's worktree. The hub should
move the branch to the last commit of this job, the one that adds this report.

## Change 1: every listing counted, timed and logged

- `_Pass._listing` (subfleet/retention.py) wraps each call to the reader.
  - It writes exactly one line to the logger it is given, whatever the outcome:
    `retention: holder listing check=<first|second|both> jobs=<n> reader=<name> seconds=<s.ss> ok [k=v ...]`,
    or `... failed: <reason>`.
  - The reason has its whitespace collapsed and is cut at 300 characters.
  - A successful line is INFO and a failed one WARNING.
  - Counts and seconds go into the pass's progress in a `finally`. The result
    gains `listings` and `listing_s` (wall seconds, rounded to 3 places).
- The reader's own counts follow `ok`:
  - `lsof_holders` returns `Busy`, a dict with `.stats`, here `processes=<p lines>`.
  - `reader_name()` names the reader: its `reader` attribute, else its `__name__`.
- The daemon passes `log=self.log`, so the lines land in `daemon.log`.
  `test_the_daemons_pass_logs_to_daemon_log` pins this.
- I searched the tests for exact comparisons that a new result key could break,
  such as `assert result == {...}`, `sorted(result) ==`, `set(result) ==` and
  progress-shaped literals. No retention test compares a result exactly; the
  only hits are in unrelated modules.

## Change 2: one listing a pass

### What a pass does now

- After the quarantines, `_Pass._holder_listing` takes one listing:
  - check 1 for each job quarantined before it whose check 1 hasn't run;
  - check 2 for each job archived in an earlier pass.
- A pass makes no listing when no job needs a check.
- The moved-worktree cwd scan still runs first.
- Failures fail closed: a failed listing or cwd scan (`ScanFailed`,
  `InspectionError`, `OSError`, `ValueError`) puts back every job the listing
  was for and sets `holder_scan_failed`. Cancellation still raises `Interrupted`.
- Jobs whose check 2 passed take the final check and commit in this pass,
  straight after the listing.
- A job archived later in the pass waits in state `archived` for the next
  pass's listing:
  - `awaiting_check2` lists it, and so does `in_flight`;
  - the pass reports `more` and `progressed`, so the daemon's next pass comes
    at `RETENTION_CATCH_UP_S` (5 s).
- `_finish` refuses any job that isn't in the pass's `check2` set, so no job
  takes the final check without check 2.

### The ordering rule and its stamps

- **Recorded events.** `quarantine()` saves `quarantined_stamp`. The archive
  builder saves `archived_stamp` in the same journal write that sets state
  `archived`. `rollback` clears both.
- **The listing's stamp.** The listing takes its own stamp first, before the
  cwd scan.
- **The rule.** A listing serves a check only when the job's event stamp is
  below the listing's stamp. The pass records all three (`listing`:
  `{"stamp", "first": {job: stamp}, "second": {job: stamp}}`).
- **How stamps are made.** `Context.stamp()` returns
  `max(time.time_ns(), floor + 1)`.
  - `_recover` raises the floor to every stamp in every journal it reads
    (`Context.saw`).
  - `RetentionState.stamp_floor` carries the floor between passes.
  - So a restart, or a clock stepped back, can't order a listing before an
    event that came before it.
- **Old journals.** A journal written before stamps existed reads as stamp 0.
  That is safe, because the journal was saved before this pass began.

### Unchanged behaviour

- **Resume.** Each retirement resumes from its journal. A job recovered in
  state `archived` takes check 2 from this pass's listing.
- **Cancellation, deadline, slices and deferrals.** All unchanged. A deadline
  reached between steps still raises with progress kept. Busy and scan-failure
  deferrals keep their times.

### Did anything else assume check 2 ran in the pass that archived?

Yes. These parts did, and each is now changed:

- **Batch accounting.** The pass counted every in-flight job, waiting archives
  included, against the batch of 32. The past-deadline first-start permit was
  refused whenever any job was in flight.
  - With waiting archives counted, passes alternated: one archived a batch and
    the next only committed it, so retiring a batch still took two listings.
  - Jobs archived earlier (`finishing`) now hold no batch place and don't
    block the permit. Each pass commits the last pass's archives and starts the
    next batch under one listing.
- **Tests.** Many tests expected one `maintenance` call to retire a job:
  - `_archive_then_stop` (test_review130_pass), which patched `_check_holders`;
  - the f1 test, renamed `..._commits_at_the_next_listing`;
  - the drain, parked-job and failed-scan assertions;
  - `test_batch_bounds_a_pass_and_takes_the_oldest_first`;
  - the commit-time pin test, whose stand-in counted calls to `pins()`;
  - the native maintenance tests, the admission ramp tests and the census
    round-3 lease test;
  - the interleavings property test, whose schedule ran `final_check` in the
    pass that locked;
  - the churn test's per-pass counts;
  - three timers tests: the deadline after the first prune, leftover priority
    and orphan priority.
- **Comments and docs.** The daemon's `RETENTION_PASS_S` comment ("the batch's
  two holder listings"), the `retention_holders.py` docstring and the
  retention doc.

These need nothing:

- Recovery from `committing` back to `archived` is served by the next listing.
- A failed `commit()` saves state `archived` before its rollback.
- The survey (`retention_survey.py`) takes its own single listing.

## Change 3: the narrower reader, and why it is left out

**The prototype.** `retention_vnodes.py`, now in this report's folder, uses
libproc through ctypes and the standard library only. It runs as
`python -I -S` under the same clamp as `lsof`. It walks:

1. `proc_listallpids`;
2. `PROC_PIDT_SHORTBSDINFO` for the uid and command;
3. `PROC_PIDLISTFDS`, then `PROC_PIDFDVNODEPATHINFO` for each vnode descriptor;
4. for mapped files, one `PROC_PIDREGIONPATHINFO3` probe per watched fsid,
   then `PROC_PIDREGIONPATHINFO2` over the file-backed regions.

It checks its struct sizes and runs a self-test against its own process before
listing. It prints `lsof -F`-style records only for the watched devices, and
ends with a counts trailer.

**The measurement.** `measure.py` ran the two readers serially, alternating
which went first, at most one listing at a time. Before each listing it waited
for no other `lsof` (`pgrep -x lsof`). It recorded the 1-minute load, the
seconds and counts only (`measurements.jsonl`). It waited 0.2 to 0.9 s before
each listing and skipped no round.

| Round | First | `lsof` s (load) | narrow s (load) | narrow / `lsof` |
|---|---|---|---|---|
| 0 | `lsof` | 36.47 (51.5) | 20.68 (43.1) | 0.57 |
| 1 | narrow | 25.76 (27.8) | 15.35 (33.6) | 0.60 |
| 2 | `lsof` | 26.32 (37.8) | 18.41 (37.0) | 0.70 |
| 3 | narrow | 13.27 (41.0) | 36.20 (36.9) | 2.73 |
| 4 | `lsof` | 10.31 (39.6) | 8.73 (40.3) | 0.85 |
| 5 | narrow | 3.82 (44.6) | 8.71 (45.0) | 2.28 |

**Totals.** `lsof` took 115.96 s and the narrow reader 108.09 s, a saving of
6.8%. The median ratio is 0.78 and the geometric mean ratio 1.04. The gate
required a saving of at least a third at similar load, so I stopped after
change 2. I ran six of the eight rounds allowed, in the time available. An
earlier run of 34.09 s at load about 53, with two other `lsof` processes
running, is not counted.

**What each reader asked about, per run:**

| | `lsof` | narrow reader |
|---|---|---|
| Processes | 950 to 1,023 listed | 1,249 to 1,300 listed; 932 to 984 read |
| Descriptors | 11.5k to 12.4k | 11.5k to 12.0k |
| Entries | 28k to 31k | — |
| Mapped | 15.5k to 17.9k `txt` | 15.1k to 16.9k mapped entries on the watched volume |
| Other | — | 6 unreadable; about 270 of other users; 32k to 38k region calls |

**Why it isn't faster.** In the runs with timings, the region walk took 8 to
32 s and descriptors 0.8 to 2.6 s. In one probe, a single process's 695 regions
took 3.82 s cold and 0.05 s on a second read, with little CPU in either.
`lsof`'s own time fell from 36.5 s to 3.8 s across the session at a load of
28 to 52.

**What this suggests, but I didn't test.** Both readers pay mostly for reading
memory-region paths, which depends on caches, and a narrower reader that still
has to find mapped files has to do the same walk. Saving a third would need a
different source for mapped files, not a narrower path to the same one.

## Invariants and the tests that check them

1. **Any reader failure makes every job in the batch busy.**
   - `test_a_reader_failure_makes_every_job_of_the_listing_busy`: a failed
     listing for one check-1 job and one check-2 job puts both back, restores
     their folders, defers both and commits neither.
   - `test_a_failed_listing_writes_one_line_with_its_reason`.
   - The property test draws failed listings. Its model records no check after
     a failure, so an archive or commit after one fails the test.
2. **The narrow reader's busy set equals `parse` over `lsof`'s output on every
   fixed row.** Not applicable, because the reader isn't shipped. The rule it
   would have had to match has fixed rows of its own:
   `test_parse_holds_mapped_files_and_writers_but_not_readers` covers a reader,
   a mapping, a writer, a directory descriptor, a cwd and a path outside the
   watch.
3. **A listing serves a check only if it started after that check's event, for
   every job it serves.**
   - `test_a_listing_serves_a_check_only_after_its_event`: journals read inside
     the reader, compared with the listing's stamp.
   - `test_a_restart_with_the_clock_stepped_back_still_serves_the_waiting_job`:
     the first pass stamps at 5e18 ns, the second restarts with a clock reading
     1, and the listing still follows the archive.
   - The property test checks this both by event order and by the persisted
     stamps.
4. **No job reaches its final check without check 2 from a qualifying listing.**
   - The property test's commit wrapper fails unless a listing after the job's
     latest archive served its check 2 and found it free.
   - `test_a_job_archived_in_a_pass_waits_for_the_next_listing`.
   - The `_finish` guard.
5. **A pass takes at most one listing, and none when no job needs a check.**
   - `test_one_listing_a_pass_serves_both_checks` (`first`, `both`, `second`).
   - `test_no_job_needs_a_check_no_listing`.
   - The property test: exactly one listing when a job was quarantined in the
     pass, waited archived or waited for check 1, and none otherwise.
   - The churn test checks one scan per pass over 70 jobs.
6. **Every listing writes exactly one log line.**
   - `test_every_listing_writes_one_line_and_the_result_counts_them`: lines,
     `listings` and reader calls agree, and `listing_s` matches the lines'
     seconds.
   - The failed-listing test.

**The property test.** `tests/unit/test_retention_listings_properties.py`
covers invariants 3 to 5 against a plain model. It draws up to six passes over
three jobs. Each pass draws:

- a batch of 1 to 3;
- whether the daemon restarted before it (a fresh `RetentionState`);
- whether the stamp clock stepped back by 10^12 ns at that restart;
- which jobs the listing finds held;
- whether the listing fails.

The model is the order of what the real pass did, recorded by wrappers round
`quarantine`, `archive`, `commit`, `rollback` and the reader. It knows nothing
of stamps or of how a pass decides. After every pass the test also checks that:

- `awaiting_check2` equals the jobs whose journals say `archived`;
- a waiting job leaves `more` and `progressed` set.

A final drain with free listings must then retire every job.

The test runs 10 examples by default (`RETENTION_PROP_EXAMPLES`, about 10 s);
40 examples passed in 42 s. Its first two failures were bugs in the test, not
in the code:

- wrappers stacked across examples on the function's `monkeypatch`, which I
  fixed with a per-example `monkeypatch.context()`;
- a model that kept committed jobs.

## Mutants

Each mutant ran on a fresh copy of `subfleet/` and `tests/` against the two new
test files and the churn test. `mutants.jsonl` in this report's folder records
each run.

| Mutant | Killed by |
|---|---|
| M1 check 2 from the listing before the archive: this pass's archives finish at once | the property test, `test_a_job_archived_in_a_pass_waits_for_the_next_listing`, `..._serves_a_check_only_after_its_event`, `..._clock_stepped_back_...`, `test_one_listing_a_pass_serves_both_checks`, the reader-failure test, the churn test |
| M2 finish without check 2: last pass's archives commit before (without) the listing | the same seven |
| M3 a reader failure read as no holders | the property test, the reader-failure test, `test_a_failed_listing_writes_one_line_with_its_reason`, the churn test |
| M4 mapped files dropped (in `parse`, the shared rule; the reader isn't shipped) | `test_parse_holds_mapped_files_and_writers_but_not_readers` |
| M5 read-only descriptors taken as holds (`parse`) | `test_parse_holds_mapped_files_and_writers_but_not_readers` |
| M6 the pass goes idle while a job waits (`progressed` loses `or bool(awaiting)`, `in_flight` loses the waiting jobs) | the property test, `test_a_job_archived_in_a_pass_waits_for_the_next_listing`, the churn test (5 s catch-up) |
| M7 nothing logged for a successful listing | `test_every_listing_writes_one_line_and_the_result_counts_them`, `test_one_listing_a_pass_serves_both_checks` |

I also ran one control: the `_finish` guard alone dropped. It survives (12
passed), as expected. Only jobs whose check 2 the listing passed reach
`_finish`, so the guard protects future callers. M1 and M2 add their jobs to
`check2` to get past it.

## Tests run

Every run used `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)` and
`UV_CACHE_DIR=$TMPDIR/uv-cache`, with `uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -q -n 3|4 -p no:cacheprovider --basetemp <new dir>`.

**The brief's final lists, at 1bcc18327:**

- The new files plus `test_retention_worktrees.py`, `test_retention_qos.py`,
  `test_launch_env_round8.py`, `tests/fake/test_review130_ab.py`,
  `test_timers_retention.py`, `tests/fake/test_state_contract.py`,
  `test_test_module_names.py`, `test_invariants_index.py` and
  `test_handler_refusals.py`: **524 passed** in 78 s.
- `tests/unit/test_retention_archive.py -k 'holder or lsof or busy or check or resume'`:
  **11 passed** in 15 s.

**Wider runs over the modules that call `maintenance`, while I fixed the tests:**

| Set | First result | After the fixes |
|---|---|---|
| Light unit (24 files) | 619 passed, 4 skipped, 4 failed (`test_timers_retention`) | 20 of 20 passed |
| Fake (every retention-touching fake file) | 462 passed, 9 failed | the five files: 161 passed |
| Heavy (`test_retention_archive`, `_81_notes`, `_sweep_race`, the four `*_properties` files, `test_review130_pass`) | 188 passed, 2 failed | both pass |

**The final regression at 1bcc18327, `-n 4`:**

| Set | Result |
|---|---|
| Fake (20 files) | **469 passed** in 114 s |
| Heavy (8 files) | **190 passed** in 370 s |
| Light unit (24 files) | **629 passed, 4 skipped** in 151 s (the same 4 skipped on the first run) |

I didn't run the whole suite, as the brief asked.

## Deviations and notes

- **Auto-commit 591d8960f.** Before a context compaction, the harness
  committed 46 files as `fixup! Count, time and log every retention holder
  listing (... change 1)`.
  - It holds most of change 2, the catch-up marker, many test updates, the
    measurements and the prototype under `subfleet/`.
  - 1bcc18327 moves the prototype out and finishes change 2.
  - I didn't rewrite history. Don't run `git rebase --autosquash` on this
    branch: it would fold change 2 into change 1. If the hub wants clean
    history, squash 591d8960f into 1bcc18327.
- **Sections.** The brief named the retention doc's sections 5, 8 and 12. Its
  holder steps sit in section 4, steps 5 to 8; section 5 covers what the
  archive holds and has no holder text. I edited:
  - section 4, steps 5 and 7;
  - section 8 (one listing, the stamps, `first|second|both`, `listing`,
    `awaiting_check2`);
  - section 12 (the batch, the deadlines, catch-up pacing);
  - section 14 (I7);
  - section 16 (a tests row);
  - C-8.4 in `docs/acceptance-contract.md`, plus its change-log line.
- **Hybrid-logical stamps.** Stamps are nanosecond wall times forced above
  every stamp seen, not raw clock readings, so a restart or a clock stepped
  back can't break the rule. They are kept in journals and in
  `RetentionState.stamp_floor`.
- **The test marker `retention_catch_up`** (tests/conftest.py, `catch_up`).
  - About 45 test modules were written when one pass retired a job. Under the
    marker, one `maintenance` call runs the follow-up passes the daemon's
    catch-up would make (at most three, sharing a `RetentionState`, stopping
    after an interrupted pass or a failed listing), and returns the passes'
    results merged.
  - Tests that count passes or time a pass call the real function
    (`maintenance.real`) and assert the new behaviour of each pass.
  - The risk is that the marker hides a fault that shows only between passes.
    The new example tests, the property test and the churn test all use real
    single passes.
- **Batch and permit.** Jobs archived earlier hold no batch place, so a pass may
  have up to 32 new starts plus up to 32 finishing jobs, under one listing.
- **A renamed test.** `test_f1_a_job_archived_before_the_deadline_commits_in_its_own_pass`
  is now `..._commits_at_the_next_listing`. The historical
  `docs/reports/2026-10-05-pr130-fix.md` keeps the old name.
- **One background run.** The mutant runner passed the shell's 600 s cap, and
  the tool moved it to the background. I read its output file instead of
  waiting on it, and it finished inside the session. Nothing else ran detached.
- **Measurements only.** As the brief allowed, the measurements listed every
  process. They kept only counts and seconds, no path or command, and signalled
  no process.

## Follow-ups

- Change 3 stays open only if a source for mapped files exists that is cheaper
  than walking each process's regions. Measure that first.
- After this lands, `daemon.log` lines will show how often each listing serves
  `both`, and how long listings take under live load.
