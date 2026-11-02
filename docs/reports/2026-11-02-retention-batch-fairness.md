# Retention's batch is fair (C-8.4, #288; review r1 of #286, P3-4)

Branch `fix/retention-batch-fairness-217` off `release/217` at `18aa2a695`.
Files: `subfleet/retention.py`, `subfleet/retention_archive.py`, two new test
files, C-8.4 and `docs/desktop/retention-archive.md` (§6 step 6, §12, I17, the
test table). Probes and outputs live in `2026-11-02-retention-batch-fairness/`.

## What review r1 found

A pass takes at most `BATCH = 32` jobs into flight, in-flight jobs included.
At `retention.py:601` on base, selection stopped choosing once in-flight jobs
filled the batch, and nothing took a slot back from a parked job. Review r1
replaced `Retirement.archive` with a stub that always parks without work and
ran `batch=1`: over 4 passes `job-b-new` never started. It left open whether a
real archive can park every slice without progress.

## 1. A real archive parks every slice without progress

It can. The trigger: a file that changes between slices and takes longer to
read than a slice.

Why, from the code:
- `_Builder._tick` parks only once `self.work` is nonzero and the slice has run
  out. `_note` raises `work` by one for each progress record (review of
  a9a6cbf4, N3: a slice always does new work before it parks).
- `_file` skips a file whose `rfs.sig_key` (device, inode, size, mtime, ctime)
  has a record; otherwise it ticks, reads the file, stores it and notes a
  record.
- The walk visits names in byte order and starts again from the top every
  slice.
- The holder check runs once before archiving (`check1` in the journal) and
  once after the archive completes; nothing checks holders between slices.

So when the first uncached file in walk order changes between slices, every
slice re-reads it, writes one record (`last_work = 1`), and parks at the next
entry because the read used up the slice. The job never gets past that file.
Each pass reports `progressed=True` and `more=True`. `daemon._retention` then
runs the next pass `RETENTION_CATCH_UP_S` (5 s) later, for ever. Review r1's
stub had `progressed=False`, so the daemon would at least have waited the hour
between passes. With the real trigger it doesn't.

`probe_trigger.py` runs this with the real builder, real files and a real Git
repository; it simulates only the clock and the read's cost (the 64 KiB file
costs 100 s of a 60 s slice). `batch=1`, and a newer small job waits.

On base `18aa2a695` (`probe_trigger.base.txt`):

| pass | pruned | in flight | more | progressed | hot slice (last_work) |
|---|---|---|---|---|---|
| 1 | | job-hot | True | True | 3 |
| 2–8 | | job-hot | True | True | 1 |

`job-new` never starts.

With the fix (`probe_trigger.fix.txt`):

| pass | pruned | in flight | deferred |
|---|---|---|---|
| 1 | | job-hot | |
| 2 | job-new | | job-hot: yielded, 1 slice since it started |
| 3–5 | | job-hot | |
| 6 | | | job-hot: stalled, 3 slices in a row got no further than step 4 |

Other shapes I tried, none of which stall:
- A slice shorter than one file's read, on a tree that doesn't change: each
  slice finishes one more file (`test_a_slice_moves_forward_when_one_file_outlasts_it`).
- A cached re-walk longer than the slice: each slice still reaches one new
  file (`test_a_slice_moves_forward_when_the_cached_rewalk_outlasts_it`).
- Reads that outlast the slice in the read-back phase: each slice reads back
  one more copy (`test_slow_read_backs_are_progress_not_a_stall`, new).
- Review r1's exact shape, a park with no work: the real builder can't produce
  it, since `_tick` needs `work > 0`.

Still UNPROVEN: that a live host produces the trigger. It needs a write, or a
metadata change that moves the ctime `sig_key` includes, to a file in a
quarantined tree after holder check 1, and a slice too short to read that file.
I haven't shown such a writer. The fix doesn't depend on the trigger.

## 2. The fix

**Progress is how far a slice gets.** `_Builder.reached` counts what a call
completes, cached or new, in the builder's order: every tree's entries, each
omitted object checked, the Git step, each stored copy read back. A slice
parks between two of these, so `reached` says how far it got.
`Retirement.last_reached` keeps it. On a tree that doesn't change, each
parked slice gets strictly further than the last: the cache now holds the
items the last slice finished, and the slice finishes the first one the last
slice didn't before it parks. The one exception: when the walk finds a
repository inside regenerable output (`NotRegenerable`), it begins again
with that output archived and counts from the tree's start
(`_Builder.found`, `Retirement.last_found`).

**Stall rule.** `RetentionState.holds[job]` keeps the slices a job has had
since it started, its furthest `reached`, and its stall count. A park with no
new work, or one that gets no further than the furthest, adds a stall;
progress resets the count. At `STALL_SLICES = 3` the pass rolls the job back
with its archive kept (an idle cache) and defers it through the error backoff:
`_rollback(..., DEFER_ERROR_S)` doubles with the journal's `failures`, so 6 h,
then 12 h, up to 24 h (`test_a_stalled_job_backs_off_as_for_an_error`
measures 6 h then 12 h). Three, not one: a file that changes between two
slices costs the next slice its progress (it reads the file again), and a
writer finishing up can do that twice.

**A finding is progress.** A slice that finds a repository in regenerable
output resets the job's furthest to its own position and its stall count to
zero. Without this, the first version of this change stalled a real shape:
the walk verifies a venv's installed files by hash, one slow file a slice,
then finds an editable clone deeper in the same venv. Every file it verified
must now be stored, so the slices after the finding sit at steps 9, 10, 11,
short of the step 13 the earlier slices reached, and the job went back as
stalled (`test_a_repository_found_in_regenerable_output_is_progress`, red
at `db1afa04d`: "stalled: 3 slices in a row got no further than step 13").
Base had no stall rule, so this was a regression of this change, fixed
before the first review returned. A tree holds finitely many such roots, and
the walk remembers each finding, so findings can't keep a stalled job going.

**Yield rule.** Selection now finds every eligible job, the ones it would
start with no batch, and queues them. While one waits, parked jobs hold at
most `batch - 1` slots. If more are in flight, the parked job (journal state
`quarantined`) with the most slices since it started, at least one, yields:
`_rollback(..., "yielded", 0, ...)` puts it back with its archive kept, no
deferral, and it takes a ticket behind every job waiting then and ahead of
any that begin waiting later. Its pool count goes back up, and the pass
reports `more`, so the daemon comes back in 5 s rather than an hour. A yield
leaves `failures` alone, so it never lengthens a later error backoff.

A job that has had no slice since it started never yields. Within one daemon
run a full batch of such jobs needs a pass cancelled between its starts and
its slices; the next pass then slices them before any yields
(`test_a_job_cancelled_before_its_first_slice_keeps_its_slot_for_one`). After
a restart, recovered jobs have no hold until their first slice, so they can't
yield before it either.

**Slots.** A job holds a slot while its journal is in flight with no rollback
pending. Base already counted it so: when a live turn in the quarantine folder
holds a rollback up (`kept`), recovery leaves the job out of `in_flight` on
every later pass. A yield held up that way frees its slot at once, as a later
pass would have. A yield whose rollback raises before it records
`rollback_pending` leaves the job in flight, so `_make_room` doesn't count it
as yielded, and the pass starts nothing in its place.

**Queue.** `RetentionState.queue` maps each eligible, unstarted job to a
ticket. A job keeps its ticket while it stays eligible; jobs new to the queue
take tickets oldest first; an orphan lease's job (`leftovers`) still goes
first. A start attempt removes the ticket whether `_start` succeeds or
refuses, so a job refused every pass can't keep the head
(`test_a_start_refused_at_selection_queues_behind_the_others`). When every job
becomes eligible in the same pass, this is the old order, oldest first
(`test_batch_bounds_a_pass_and_takes_the_oldest_first` still passes).

**One start a pass.** The start loop now holds back starts after the
deadline only once it has attempted one. Before, it attempted none when the
deadline had passed and any job was in flight, so slow recovery, pin reads or
size measuring on every pass would keep a waiting job out for ever
(`test_a_pass_past_its_deadline_still_starts_one_job_beside_parked_ones`).
A start past the deadline costs no more than the one base already allowed
with nothing in flight.

## Invariants (I17)

Say a job *waits* in a pass when the pass finds it eligible and attempts no
start for it.

1. **Batch bound.** At most `batch` jobs hold a slot after every pass.
2. **Yields make room.** A pass in which a job yields attempts a start.
3. **Bounded waiting.** Some pass attempts to start a waiting job within
   2(q + 1) passes of the first pass it waits in, q being the jobs queued
   ahead of it after that pass. So never more than 2N passes for N jobs.
4. **Stalls end.** A parked job holds its slot through at most
   `STALL_SLICES - 1` slices in a row that neither get further than its
   furthest nor find a repository in regenerable output; the next such slice
   puts it back. A job that gets no further from its start leaves after
   `STALL_SLICES + 1` slices (one to set its furthest, three more).
5. **Slices get further.** On a tree that doesn't change, each parked slice
   reaches strictly further than the one before, or finds a repository in
   regenerable output (at most once per regenerable root).

Why 3 holds:
- Every pass with a job waiting and a free slot attempts the head of the queue.
- With no free slot, the pass yields a parked job that has had a slice since
  it started, which frees one.
- The only full pass without such a job is one whose in-flight jobs have all
  had no slice since they started, or are finishing. Every pass gives at least
  one slice, never-sliced jobs first, so that pass slices or finishes one, and
  in the next pass it can yield or has freed its slot.
- So any two passes in a row attempt the head. Jobs that begin waiting later,
  jobs that yield, and jobs whose start `_start` refused all take tickets behind.
  The waiting job's place in the queue falls by at least one every two passes.

Outside the bound: a pass that a deadline or cancellation ends before
selection (`maintenance` returns at its first checkpoint), a crash's orphan
leases (they go first), and a daemon restart (it empties the queue and the
holds; jobs take tickets again oldest first).

## Tests

New, in `tests/unit/test_retention_batch_fairness.py` (15 tests): the real
trigger, stall backoff, review r1's stub, turns in queue order, the batch
bound with four jobs that never finish, a start past the deadline, a refused
start, a yielded job's ticket against a job that begins waiting a pass later,
a pass cancelled between a start and its slice, a yield whose rollback fails,
a yield a live turn holds up, and three static trees, real builder, on which
every parked slice reaches strictly further: six slow files through the walk,
Git step and read-back; a job folder whose last two files are slow, so one
slice parks where the read-back begins and the next only reads copies back;
and a `node_modules` package holding a repository that sends the first
slice's walk round again. One more, on the real builder, pins that a finding
is progress: a venv verified by hash over several slices, then found to hold
an editable clone. In `tests/unit/test_retention_batch_fairness_properties.py`, a
Hypothesis test of invariants 1–3 on the real pass: generated batch sizes 1–3,
2–5 jobs, each finishing at once, finishing after 1–4 slices, or never
finishing (always further, new work but no further, no work); deadlines with
room for one to four slices; gaps of 7 h that let deferrals run out. 60
examples passed in 133 s here (`RETENTION_FAIRNESS_EXAMPLES` sets the count).

On base `18aa2a695`, all 16 tests in the two files fail
(`tests-on-base.txt`): ten on behaviour, six only because base lacks the
names they read (`STALL_SLICES`, `RetentionState.holds`,
`Retirement.last_reached` four times). The venv test guards a regression of
this change, not of base, which had no stall rule. The property test's shrunk counterexample:
`batch=1`, a job that never finishes ahead of one that finishes at once. The
second waits 5 passes against a bound of 4 and never starts.

Existing suites, run serially here at `342bfc8ac`, 273 passed in 592 s:
`test_retention_archive.py`, `test_review130_pass.py`,
`test_timers_retention.py`, `test_retention_shared_folders.py`,
`test_retention_dependency_units.py`, `tests/fake/test_turn_retention_job_properties.py`,
`tests/fake/test_native_maintenance_startup.py`. A capacity reservation held
the host, so the full suite runs in CI.

## Mutants

`mutants.py` applies each mutant to the checkout, runs the two new files
(property test at 25 examples) and, for the progress counters, the existing
N3 slicing tests, then restores the file. Results: `mutants.txt`,
`mutants.json`.

| mutant | what it changes | result | first failing test |
|---|---|---|---|
| `no-yield` | no parked job ever yields | killed | `test_a_real_archive_parks_without_progress_while_a_file_changes_between_slices` |
| `no-reserve` | parked jobs may hold all `batch` slots while a job waits | killed | `test_a_real_archive_parks_without_progress_while_a_file_changes_between_slices` |
| `no-stall` | the stall rule never fires | killed | `test_a_real_archive_parks_without_progress_while_a_file_changes_between_slices` |
| `work-is-progress` | any new work counts as progress (base's test) | killed | `test_a_real_archive_parks_without_progress_while_a_file_changes_between_slices` |
| `age-order` | waiting jobs start oldest first, not in the order they began waiting | killed | `test_jobs_that_never_finish_take_turns_in_the_order_they_began_waiting` |
| `deadline-first-only` | past the deadline, no start while any job is in flight (base) | killed | `test_a_pass_past_its_deadline_still_starts_one_job_beside_parked_ones` |
| `yield-fresh` | a job with no slice since it started may yield | killed | `test_a_job_cancelled_before_its_first_slice_keeps_its_slot_for_one` |
| `victim-least-sliced` | the parked job with the fewest slices yields | killed | `test_parked_jobs_hold_at_most_batch_minus_one_slots_while_a_job_waits` |
| `yield-not-waiting` | a yield alone does not report more work | killed | `test_review_r1_stub_a_job_parking_without_work_no_longer_holds_the_only_slot` |
| `refused-keeps-ticket` | a refused start keeps the job's ticket | killed | `test_jobs_that_never_finish_take_turns_in_the_order_they_began_waiting` |
| `victim-no-ticket` | a yielded job takes no ticket at the yield | killed | `test_a_job_that_yields_queues_ahead_of_one_that_begins_waiting_after` |
| `stall-threshold-1` | `STALL_SLICES = 1` | killed | `test_a_stalled_job_backs_off_as_for_an_error` |
| `no-count-walk` | walked entries don't count toward `reached` | killed | `test_slow_read_backs_are_progress_not_a_stall` |
| `no-count-read-back` | a fresh read-back doesn't count toward `reached` | killed | `test_a_slice_that_only_reads_copies_back_gets_further` |
| `no-retry-reset` | a walk begun again keeps counting from where it was | killed | `test_a_walk_begun_again_counts_from_the_tree_start` |
| `failed-yield-counts` | a yield whose rollback fails still counts as a yield | killed | `test_a_yield_whose_rollback_fails_keeps_its_slot` |
| `pending-holds-slot` | a job with a rollback pending holds a slot | killed | `test_a_yield_a_live_turn_holds_up_frees_the_slot_as_recovery_does` |

MUTANT_RUN_NOTE: the first run left `yield-fresh`, `victim-no-ticket`,
`no-count-read-back` and `no-retry-reset` alive, and it ran `-k` across every
file in one pytest call, which deselected most new tests for the N3 mutants.
The tests added since kill all four; a rerun of all 17 at the final head
follows.

## Costs and changed behaviour

- While parked jobs fill the batch and a job waits, each pass rolls one back
  and starts one: two renames and a store transaction for the rollback, and a
  start's pin reads, quarantine and holder check. The yielded job keeps its
  archive, so its next start re-walks metadata and reads only what changed.
- Start order is the order jobs began waiting. A job that becomes eligible
  late (a pin released, a deferral ended) now queues behind jobs already
  waiting, where before it went first if older.
- A pass past its deadline with jobs in flight now attempts one start.
- A job whose slices stop getting further leaves its slot after 4 slices and
  waits 6 h or more. Before, it kept the slot, and the daemon reran the pass
  every 5 s.

## Limits

- The queue and the holds live in memory (`RetentionState`), as the
  deferrals and sizes do. A restart loses them, which delays no job by more
  than the bound restarted. A recovered job gains a hold at its first slice
  after the restart.
- A writer that adds a repository to a new regenerable root every slice
  would look like progress for ever, as would a tree that gains entries every
  slice.
  While a job waits, the yield rule still frees its slot; when nothing waits,
  it keeps its slot and costs no one else.
- `STALL_SLICES` counts slices, not passes. With 32 jobs in flight and about
  three slices to a pass, the pass can take about 40 passes to put a stalled
  job back, unless a waiting job makes it yield first.
