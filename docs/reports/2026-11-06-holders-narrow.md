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

## Fix round 1

Fix job `20261107-111034-hn-fix-r1`, at 2a6a9f2e6 (d5aa4b3ae plus the hub's
merge of release/217 through #293). It started at 21:50 UTC on 2026-11-13.
Host load ran from 23 to 265, mostly above 60, and other jobs' test runs shared
the machine. A test file that took seconds in CI took minutes here, so several runs
were cut off by my tool's 10-minute limit; each one is listed below with what
it reached.

### CI run 42368334717: causes and fixes

The run's logs were readable. All ten jobs finished: seven failed, and the 3.14
shards 3, 4 and 5 were cancelled. The 3.12 shards name 40 failing tests in six
modules. The 3.14 shards 1 and 2 name the same tests as the 3.12 shards 1 and 2.

| Module | Failed | What the tests assert |
|---|---|---|
| `tests/fake/test_retention_pointer_failures.py` | 1 (shard 1) | `test_stale_gitfile_with_vanished_target_still_retires`: one `maintain` call prunes `owner` |
| `tests/fake/test_retention_dependency_review_r4.py` | 1 (shard 2) | one call prunes an exporting job |
| `tests/fake/test_retention_dependency_review_r5.py` | 11 (shard 2) | one call prunes `owner` (7); a dependency inserted at `commit` keeps the job (`[final-*]`, 4) |
| `tests/fake/test_retention_dependency_review_r6.py` | 6 (shard 5) | one call prunes `owner` |
| `tests/fake/test_retention_dependency_review_r7.py` | 2 (shard 4) | one call prunes `owner` |
| `tests/unit/test_retention_nested_leases.py` | 19 (shard 3) | one `run` prunes the job, or reaches the final check or the commit's lease check |

**Reproduced at 2a6a9f2e6.** All 40 failed locally:

- the 21 fake-provider tests, `-n 4`: **21 failed** in 399 s;
- the 19 nested-lease tests, from a pristine `git archive 2a6a9f2e6` copy,
  `-n 4`: **19 failed** in 290 s.

Each failure was the same kind: after one call, `pruned` was empty and the job
was still in the store. In the four `[final-*]` cases, the `commit` hook never
ran, so `inserted` stayed empty. pytest's `-q` cut the result dicts short, so I
didn't read `awaiting_check2` directly. But the marker's follow-up passes run
only while `awaiting_check2` isn't empty, and they make all 40 pass, so each job
was archived and waiting for check 2.

**Cause: the tests pinned the old listing count, so the fix is in the tests.**
Every one calls `maintenance` once and expects the job retired, or expects its
commit-time recheck reached. Under one listing a pass, a job archived in a
pass takes check 2, its final check and its commit in the next pass, which the
daemon runs at `RETENTION_CATCH_UP_S`. The build marked the modules it ran
`retention_catch_up`, but not these six. Five of them import `world`,
`reader` and `maintain` from `test_retention_dependency_review_r2`, and
nested-leases imports `run` from `test_retention_shared_folders`; both source
modules carry the marker. A marker applies to the requesting test's module, not
the module a helper comes from.

**Fix (b9f70bafa).** Each of the six modules now carries `pytestmark =
pytest.mark.retention_catch_up`, with a comment saying why. No assertion
changed. The tests still check what they did: a stale gitfile retires, a
dependency found at selection or at the commit keeps the job, and a lease
found by the census, the selecting transaction or the commit keeps it. The
marker runs at most three follow-up passes, only while a job waits for its
check 2. No code change was needed: no test showed the PR breaking a behaviour
it rightly pins.

**After the fix.** All 40 pass. Two earlier runs were cut off by the tool,
with no failure before the cut:

- at f4abb4a31's tree, all 40, `-n 4`: **31 passed, 0 failed** before the cut;
- from a `git archive 087bac39c` copy, the 19 nested-lease tests, `-n 2`
  (beside a mutant run): **11 passed, 0 failed** before the cut;
- at 02f3ca168's tree, `-n 4`, once the load fell to about 26: **21 passed**
  in 93 s and **19 passed** in 61 s. All 40 pass.

### Review r1's findings

| Finding | Outcome | Where |
|---|---|---|
| 1. A waiting job can wait the hour | **Fixed** (f4abb4a31) | `daemon.py` `_retention`; `tests/conftest.py` `catch_up`; C-8.4; design §12 |
| 2. The stamp comparison never decides | **Documented**; the comparison stays | design §8; mutant 1 below |
| 3. The survey's listing writes no line | **Documented**: the survey writes no line, by choice | C-8.4; design §8; `retention_survey.py` estimate |
| 4. One bad manifest fails check 1 for the whole batch | **Fixed** (f4abb4a31) | `retention.py` `_holder_listing`, `_unreadable`; C-8.4; design §8 |
| 5. The failure reason goes into `daemon.log` unscrubbed | **Fixed** (f4abb4a31) | `retention.py` `_listing`; design §8 |
| 6. The property test never draws a crash mid-pass | **Fixed** (087bac39c) | `tests/unit/test_retention_listings_properties.py` |

**Finding 1.** When a pass sets `holder_scan_failed` but `awaiting_check2` isn't
empty, the daemon now marks `ScanFailed` and comes back after
`RETENTION_CATCH_UP_S`. It logs `holder scan failed; <n> jobs wait for their
holder check 2; next pass in 5 seconds`. This covers both of r1's cases: a
failed cwd inspection in `_start` while another job archives, and a parked job
that archives after a failed listing. If the next pass's listing fails too, it
puts the waiting job back (that job is now in `wanted`), so `awaiting_check2`
is empty and the hour applies. Each catch-up after a failed scan needs a job
that archived in the failed pass, and the next failed listing puts that job
back, so the extra listings are bounded by the jobs whose check 1 had already
passed. `catch_up` in
`tests/conftest.py` used to stop at any failed scan; it now stops only when no
job waits, as the daemon does. Tests:

- `test_a_failed_holder_scan_keeps_a_job_waiting_for_check_2_on_the_catch_up_clock`:
  the next pass begins 5 s after the failed one, the log line says so, and no
  "next pass in an hour" line appears;
- `test_a_failed_holder_scan_with_a_waiting_job_is_marked_scan_failed`;
- `test_retention_runs_exactly_when_its_two_clocks_allow` (80 examples) now
  draws the outcome `scan-failed-waiting`, and its model expects catch-up after
  it;
- `test_every_rearm_records_its_status_before_setting_last_maintenance` gains
  that result as a case.

The old `scan-failed` outcome, with no job waiting, still waits the hour
(`test_a_failed_holder_scan_does_not_reoffer_batches_on_the_catch_up_clock`,
and the churn test in `test_review130_ab.py`, whose failed scan puts back all 32
jobs it quarantined).

**Finding 2.** The comparison stays. Design §8 now says it is defence that the
code's order already guarantees, and that no test can reach it:

- every quarantine a listing could serve ran before the listing took its stamp;
- every archive it could serve ended in an earlier pass;
- recovery raises the floor above every journal's stamps before the listing
  stamps.

Mutant 1 below confirms that no test reaches it. The comparison guards a future
caller that lists earlier, or a journal stamped by a clock this daemon never saw.

**Finding 3. I chose to say plainly that the survey writes no line.** C-8.4 now
says "each pass's listing", and C-8.4 and §8 say the survey writes none. Why:

- `subfleet retention survey` is a read-only dry run, started by hand in the
  CLI's process, with no `daemon.log`;
- its report already gives the listing's seconds (`holders.scan_seconds`) and
  any failure (`notes`);
- the line exists to count the daemon's listings, and the survey's listing
  isn't one of them.

A line written there would go to a logger with no handler. Reading the survey,
I found one stale assumption the build's report missed: its estimate counted
two listings a pass (`listing_seconds_per_pass: 2 * scan_s`, and `passes * 2` in
`total_hours`). It now counts one listing a pass, plus one more pass for the
last batch's check 2 (`listings: passes + 1`). I grepped the tests for exact
compares on `estimate`: the only hits are `sample`'s `report["estimate"] is
True`, a different report.

**Finding 4.** Each job's watch is built in its own `try` (`OSError`,
`ValueError`, `KeyError`, `TypeError`: an unreadable file, bad JSON, or JSON of
the wrong shape). An unreadable manifest puts back that job alone, as an error
(`error: manifest: <class>: <text>`, the error backoff), before the listing,
which still serves the rest. It neither fails the listing nor sets
`holder_scan_failed`. If the listing itself then fails, the unreadable job is
still put back as an error, and the rest as `holder scan failed`. A batch whose
only job has an unreadable manifest takes no listing. The final check already
treated an unreadable manifest as that job's error. Test:
`test_a_bad_manifest_puts_back_only_its_own_job` corrupts job-a's
`archive/manifest.json` while it waits for check 2. The next pass's listing then
serves only job-b's check 1 (line `check=first`), job-a is deferred
`error: manifest: JSONDecodeError ...` with its folder and row back, job-b
archives, and `holder_scan_failed` is False.

**Finding 5.** `_listing` passes the reason through
`refusals.scrubbed_line(..., 300)`, as the daemon's retention error lines do.
Whitespace is still folded first. `test_a_failed_listings_reason_is_scrubbed`
gives a `ghp_` token in the reason and finds `[REDACTED]` in its place. The
existing `test_a_failed_listing_writes_one_line_with_its_reason` still passes
unchanged.

**Finding 6.** Each drawn pass may now be cut short (`cut`, about three passes
in seven):

- **`committing`**: the daemon dies just after a journal says `committing`, with
  the rows still there (a `BaseException` raised after the save). The next pass
  restarts, recovery puts the journal back to `archived`, and the job takes
  check 2 again;
- **`quarantining`**: the daemon dies once a quarantine's moves are done, with
  the journal still `quarantining` (raised instead of the `quarantined` save).
  The next pass restarts, and recovery reconciles the moves and finishes the
  quarantine;
- **`cancel`**: the cancel is set between the pass's quarantines and its
  listing. The fixture reader raises `lsof cancelled`, as `lsof_holders` does.
  The test checks that the pass reports `cancelled` with more work, and that
  every job the listing was for keeps its journal: `quarantined` without
  `check1`, or `archived`.

The model is stricter too: a job may commit only after its own pass's listing
served its check 2. So a job recovered from `committing` must take check 2
again; it can't commit on the check 2 it had before the crash. Three
`@example`s run each cut whatever Hypothesis draws. Drain passes now jump the
clock before each pass, because a restart recalls a rolled-back job's
wall-clock deferral from its idle journal, and that could otherwise strand the
job.

### Mutants

Each mutant was applied alone, in place, to `subfleet/retention.py` or
`subfleet/daemon.py`. After each run, both files were restored from pristine
copies, and their sha256 was checked against the committed bytes
(`1fac2197545b…` and `4dcb005d672e…`). I didn't use `git checkout`. Copying the
whole tree per mutant was too slow on this disk, so I stopped that copy.

| Mutant | Tests run | Result | Killed by |
|---|---|---|---|
| 1. Stamp comparison replaced by `True` (both checks) | `test_retention_listings.py`, the property test (10 drawn + 3 explicit examples), `-n 2` | **survived**: 13 passed in 832 s | none, as expected (finding 2); documented in design §8 |
| 2. `ctx.saw` dropped from recovery | the clock-stepped-back test and the event-order test, `-n 2` | **killed**: 1 failed, 1 passed, 123 s | `test_a_restart_with_the_clock_stepped_back_still_serves_the_waiting_job` |
| 3. Batch accounting counts `finishing` again | `test_one_listing_a_pass_serves_both_checks`, `test_retention_archive.py::test_batch_bounds_a_pass_and_takes_the_oldest_first`, `-n 2` | **killed**: 2 failed, 148 s | both |
| 4. Finding 1's catch-up rule removed | `test_native_maintenance_startup.py -k "waiting or scan or two_clocks or rearm"`, `-n 4` | **killed**: 3 failed, 8 passed, 187 s | `test_a_failed_holder_scan_keeps_a_job_waiting_for_check_2_on_the_catch_up_clock`, `test_a_failed_holder_scan_with_a_waiting_job_is_marked_scan_failed`, `test_retention_runs_exactly_when_its_two_clocks_allow` |
| 5. Finding 4's per-job `try` removed | `test_a_bad_manifest_puts_back_only_its_own_job`, the reader-failure test, `-n 2` | **killed**: 1 failed, 1 passed, 96 s | `test_a_bad_manifest_puts_back_only_its_own_job` |

### Commands and results

Every run used `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)` and `uv run --no-sync
--with 'pytest-xdist>=3.6' python -m pytest -q -p no:cacheprovider --basetemp
<new dir>`, with at most four workers across concurrent runs.

The `--with` environment in the shared `$TMPDIR/uv-cache` had lost its
`pyvenv.cfg`: uv built an empty overlay, and pytest rejected `-n`. So I set
`UV_CACHE_DIR=$TMPDIR/hn-r1/uv-cache`, a cache of this job's own. `uv sync
--group dev` gave Python 3.15.2.

| Command | Result |
|---|---|
| `gh run view 42368334717 --log-failed` | 40 failing tests (3.12), the same for 3.14 shards 1 and 2; 3.14 shards 3 to 5 cancelled |
| the 6 failing modules at 2a6a9f2e6, `-n 4` | cut off by the tool after 124 tests: 114 passed, 10 failed (the targeted reruns below name every failure) |
| the 21 failing fake-provider node IDs at 2a6a9f2e6, `-n 4` | **21 failed** in 399 s |
| the 19 failing nested-lease node IDs, from a `git archive 2a6a9f2e6` copy, `-n 4` | **19 failed** in 290 s |
| the 40 node IDs with the fix (f4abb4a31's tree), `-n 4` | cut off by the tool: **31 passed, 0 failed** |
| the 19 nested-lease node IDs from a `git archive 087bac39c` copy, `-n 2` | cut off by the tool: **11 passed, 0 failed** |
| the 21 fake-provider node IDs at 02f3ca168's tree, `-n 4` | **21 passed** in 93 s |
| the 19 nested-lease node IDs at 02f3ca168's tree, `-n 4` | **19 passed** in 61 s |
| `test_retention_listings.py test_retention_listings_properties.py tests/fake/test_native_maintenance_startup.py`, `-n 4` | **57 passed** in 559 s (12 listing tests, the property test with 10 drawn + 3 explicit examples, 44 daemon tests including the 80-example pacing property) |
| mutants 1 to 5 | as in the table above |
| the six CI modules whole, plus step 2's eight files, `RETENTION_PROP_EXAMPLES=50`, `-n 4`, at 02f3ca168's tree | cut off by the tool after 20 minutes: **255 passed, 0 failed, 0 errors** of about 313 (from pytest's percentages) |

### What didn't run

- **Step 1 and step 2, in full.** One run held the six CI modules whole and all
  eight of step 2's files, with 50 listing-property examples. It passed 255 of
  about 313 tests, with no failure or error, before the tool cut it off. The
  `-q` dots don't say which tests were left. The slowest are the likely ones: the
  50-example listing property, `test_retention_archive_properties.py` and the
  heavy archive tests. So **the 50-example property run isn't confirmed**. The
  listing property passed twice at 13 examples (10 drawn and 3 explicit): once
  at the fix, and once under mutant 1. Rerun step 2 with
  `RETENTION_PROP_EXAMPLES=50`.
- **Step 3: every other `tests/{fake,unit}/test_retention_*.py` module.** Not run.
- **Step 4.** `test_timers_retention.py`, `tests/fake/test_state_contract.py`,
  `test_test_module_names.py`, `test_invariants_index.py` and
  `test_handler_refusals.py` didn't run: the time ran out.
- **`test_retention_81_notes.py`'s survey tests**, which call `survey()`,
  whose estimate changed (finding 3). `test_retention_archive.py`'s two survey
  tests were in the cut-off run, which doesn't say whether they ran. By grep,
  none of them compares an estimate key.
- **A review of this round.**

CI runs the whole suite on push.

### Commits

- b9f70bafa: the six modules marked `retention_catch_up` (the CI failures).
- f4abb4a31: findings 1, 4 and 5, with their tests and the `catch_up` change.
- 087bac39c: finding 6, the property test.
- 02f3ca168: C-8.4, design §8 and §12, and the survey's estimate (findings 1 to
  5). The brief's `docs/retention-archive.md` is
  `docs/desktop/retention-archive.md`. That file keeps no per-change log: its
  revision list stops at 7, and the PRs since added none. So C-8.4's new
  change-log line in `docs/acceptance-contract.md` covers both files.
- This report section is the next commit.

Git let me commit every step. HEAD is detached in this job's worktree. The hub
should move `fix/holders-narrow-217` to the commit that adds this section.
