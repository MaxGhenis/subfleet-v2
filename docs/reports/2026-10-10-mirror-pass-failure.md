# A mirror pass that raised was never recorded as failed, 2026-10-10

The fix for the mirror's ranking (#168) found this on the way: a pass ended by
an exception that no pass expects left its record in the sidecar at `running`.
This report records what a fixture store showed before the change, what reads
the failure and what does not, the change, why the exception is still raised,
the invariants the change keeps, how each is checked, and what is left.

No live store was read or written. Every reading here is from a store, a
`~/.claude`, an app log and a state root under a temporary directory.

## What happened

`Mirror.run_once` and `Mirror._run_hot_locked` caught `_Cancelled` and
`OSError`, recorded either as the pass's end, and returned. Any other
exception left the method with the record as the pass's start had written it:
`state: running`, no `finished_at`, no `error`.

The daemon's timer (`Timers._run`) catches the exception, writes a
`timer.error` event that holds the exception's type, and runs the pass again
on its interval. Each run writes a new `started_at` before it fails the same
way. `Mirror.health()` reads a record that is `running` with a start under
thirty minutes old as a pass in flight, so it read `running` after every pass
and never reached `stalled`.

## What a fixture store showed

The passes were run as the daemon runs them, by `Timers._run("mirror")` and
`Timers._run("mirror_hot")` on one `Timers`. The trigger was a monkeypatch
that raises `RuntimeError`; no record held a bad value. `release/217` at
4c6a8a9c9 and #168's head 80324af8e gave the same readings.

| Scene | Before | After |
|---|---|---|
| A full pass whose copy step raises: the sidecar's `pass` | `running`, stage `copying entries`, no finish, no error | `error`, the same stage, a finish, `RuntimeError: … (mirror.py:… in _pass)` |
| `Mirror.health()` after that pass | `running`: "a pass has been in flight for 0.0 min" | `stalled`: "last pass failed: RuntimeError: …" |
| `sessions mirror --status` | exit 0, "mirror running: …" | exit 1, "mirror stalled: last pass failed: …" |
| `doctor`, the mirror row | `pass` | `fail` |
| 46 such passes, one a minute: health after each, and 30 s after the last | `running` every time | `stalled` every time |
| The same 46 passes: `last_ok_at`, and where the session was | none; one folder of three | the same: the pass still fails |
| The same 46 passes: `timer.error` events, `timers.mirror.last_error_type` | 46, `RuntimeError` | 46, `RuntimeError` |
| A full pass ended by `KeyboardInterrupt` | `running` | `cancelled`, `KeyboardInterrupt: (mirror.py:… in _pass)` |
| A hot pass whose copy step raises: the sidecar's `hot` | `running`, stage `starting`, no finish | `error`, stage `copying entries`, a finish, the cause |
| Health after that hot pass | `healthy`, "…; hot pass in flight since …" | `healthy`, "…; hot pass error: RuntimeError: …" |
| 90 hot passes 2 s apart, each of which raises: `hot` after each | `running`, no finish | `error`, a finish |
| A full pass whose embedded flag-only hot service raises: `pass` and `hot` | both `running` | both `error`; health `stalled` |

Each row is also a test (see Verification). The script that took these
readings is with the review's evidence, outside the repository.

A reading taken while a pass is in flight is `running` before and after the
change, also when the pass before it failed. That is C-23.28's rule for a
pass in flight, and it is the same after a pass that failed with `OSError`.

## What reads the failure

- `sessions mirror --status` reads the sidecar (`Mirror.health()`), the load
  gap and the split report. It reads no event and no timer state.
- `doctor`'s mirror row (`check_mirror`) reads `Mirror.health()` and nothing
  else.
- `timer.error` events are written by `Timers._run` and read by no module in
  `subfleet/`. `tools/soak_report.py` counts the day's events by kind.
- `Timers.status()` holds `last_error_type` for each timer. `daemon.status`
  returns it under `timers`, and `subfleet status --json` prints that reply
  whole. The text `status` does not print it, and `doctor` does not read it.

So before the change both commands reported a mirror that failed every pass
as running, and the one trace of the failures was in a table and a reply that
neither reads.

## The change

`subfleet/sessions/mirror.py`:

- `run_once` and `_run_hot_locked` keep their handling of `_Cancelled` and
  `OSError` as it was. Around it, any other exception goes to
  `_record_raised` and is then raised again.
- `_record_raised` ends the record: `error` for an exception, `cancelled` for
  an interrupt (`KeyboardInterrupt`, `SystemExit`), with the exception's type,
  its message and the innermost frame of the mirror it came through
  (`_raised_at`). It saves the journal and takes the load-gap report as the
  other endings do. A report that raises is left out. A record that cannot be
  written becomes a note on the exception. No exception from recording takes
  the place of the pass's own.
- `run_once` records only when it holds the lock. An exception before the
  lock writes nothing, as a pass that loses the lock writes nothing.
- The hot pass's end (`finished_at`, the journal, the `hot` record) moved out
  of a `finally` block, so that an exception from the end itself is recorded
  too. For `ok`, `OSError` and cancellation the steps and their order are the
  same.
- `run_once` clears the split report an earlier pass left unrecorded before
  it reads the journal, not after, so that the record of how a pass ended
  cannot carry an earlier pass's report.
- `_service_hot` hands the helper's sampling state back to the full pass also
  when the helper raised. Without that, the instance did not know an `error`
  stood in `hot`, and the next idle hot pass, which records nothing new for a
  minute, left it there.

For `OSError` and cancellation a pass does what it did, with one difference.
When the pass's own write of its end raises `OSError`, that `OSError` still
goes to the caller, and the record is now tried once more.

Health, `sessions mirror --status`, `doctor` and the timer are unchanged.
They now read a record that says what happened.

## Why the exception is still raised

`Timers._run` does three things with an exception: it writes the
`timer.error` event, it sets `last_error_type` in `Timers.status()`, and it
writes that into the pass's `timer.run` event. A pass that returns gives it
none of these. `mirror_cycle` returns nothing, and `mirror_hot_cycle` returns
only whether the run is worth an event, so a returned failure reads in the
daemon's store as a clean run. That is the reading this fix removes from the
sidecar; returning would move it to the timer.

Raising costs nothing in scheduling. `_run` catches the exception, marks the
run and frees the timer's name in a `finally`; the next run is due on the
interval either way. The timer keeps the type only, with no message and no
place, so the sidecar's record is where those are kept.

Three callers see the exception, as before: the timer; `sessions mirror`,
where an exception leaves the command's entry point (`compat.dispatch`) and
Ctrl-C prints "interrupted" and exits 130; and a full pass whose checkpoint
serviced a hot pass that raised. That full pass ends too, as before. It does
not go on: the full pass learns that its helper moved flags from a mark it
reads only after the helper returns (`_service_hot`), so a helper that raised
has not told it to refresh its flag decision.

## Invariants

For a full pass's `pass` record and a hot pass's `hot` record:

1. **Finished.** Once `run_once` or `run_hot` has returned or raised, the
   record of a pass that took the lock is `ok`, `error` or `cancelled` and has
   a finish, whenever the sidecar could be written and no second interrupt
   arrived while the pass recorded its end.
2. **One account of the end.** A pass that returns left its own record in the
   sidecar (a hot pass that changed nothing is sampled: its record is written
   at most once a minute). A pass that raises left `error` (`cancelled` for
   an interrupt) with the exception's type, and raises that same exception,
   unless the pass had already written the record of its end: that record
   stands. A record that only reads the same (an earlier pass's) does not
   count.
3. **Expected endings return.** `OSError` and cancellation are recorded and
   returned, as before. No other exception that ends a pass returns. An
   `OSError` from a full pass's own write of its end is raised, as before,
   now after one more try at the record; a hot pass's is not raised, as
   before.
4. **Success is a full pass that ended `ok`.** `last_ok_at` moves only when a
   full pass's `ok` is written, and an interrupt after that undoes nothing.
   A hot pass changes nothing of `pass`, `updated_at` or `last_ok_at`.
5. **The lock.** It is free after every pass, and an exception other than
   `OSError` that ends a pass before it takes the lock writes nothing.
6. **Health follows the record.** `healthy` after an `ok` full pass,
   `stalled` after any other, never `running` for a pass that is over.
7. **Two records agree.** A pass has a `timer.error` event exactly when it
   raised an exception; its record then names the place it was raised, and
   both name the same type. That includes an `OSError` from a full pass's
   write of its end. (An interrupt is no `Exception`, and the timer does not
   catch it.)

Not an invariant: a pass that failed stays visible once the next pass starts.
The sidecar holds one pass, so the next start replaces the record.

## Verification

`tests/unit/test_sessions_mirror_pass_failure.py`, 49 tests.

- Example tests for each row of the table above, for the lock, a dry run, a
  report that raises (as a pass's last step, and inside the handler that
  records an `OSError`), a sidecar that cannot be written, an earlier pass's
  split report, the hot pass after a serviced one that raised, the timer's
  events, the place the record names, an interrupt after a pass's end is
  written, a note that cannot be added, a pass without flag sync after one
  that forked a helper, a hot pass that raises beside an equal or earlier
  `ok`, an interrupt before a hot `ok` is in place (in the same second and
  two seconds on), and a sidecar that cannot be parsed when the end check
  reads it.
- `test_every_pass_that_starts_is_recorded_as_finished` (Hypothesis, 250
  cases): up to six passes, full and hot, with the app saving records between
  them. Each pass ends by nothing, by the daemon's stop at some checkpoint, or
  by one exception at one call of one of 19 functions a pass calls. The
  exceptions are nine kinds no handler in a pass names, `ValueError` and
  `TypeError` (which some handlers name), `OSError`, and three interrupts. A
  flag-only hot pass is serviced at every checkpoint, or at none, and each
  pass syncs flags or not. After each
  pass it checks invariants 1 to 6, and at the end that a clean full pass
  reads `healthy`. In one run the cases held full passes that raised an
  exception (24% of cases) and an interrupt (16%), hot passes that raised
  (8%, 13%), and passes of both kinds that returned `cancelled`, `error` and
  `ok` after meeting the fault. It does not reach three cases, which example
  tests cover: an exception after a pass's own end is written, a pass
  without flag sync right after one that forked a helper, and an `OSError`
  from the end's write after an `OSError`. Nor does it check flags.
- `test_the_timer_and_the_sidecar_name_the_same_ending` (Hypothesis, 80
  cases): one faulted pass run by `Timers._run`; invariant 7. With one fault
  per pass it never makes the end's write fail; an example test runs that
  case through the timer.
- Mutants: 35, each one change to the fix: the record not written,
  the exception swallowed, written without the lock, the wrong state, no
  finish, no type, no place, `last_ok_at` advanced, an `OSError` raised
  instead of returned, a written end overwritten, an end taken for this
  pass's because it reads the same, an unguarded note or read, frames found
  by file name or by any `subfleet` module, the helper kept, and so on. Six
  of them are mutants of the second review's that survived the file then.
  The test file fails on all 35, each time at an example test. The script
  and its results are with the review's evidence,
  outside the repository.

## What it does not do

- **A pass in flight still reads `running`, whatever the passes before it
  did.** The sidecar holds one pass, and the next start replaces a failed
  pass's record. On a 60 s interval, a mirror whose passes each run for 40 s
  and then fail would read `running` for 40 s of every minute. This is so
  for `OSError` failures today. Health that carries a failure across the next
  start needs a change to what C-23.28 says a pass in flight means.
- **A failed hot pass still loses what it read.** What it listed is no longer
  new to the next hot pass, so a session it was to spread waits for the next
  full pass, which the daemon runs every `mirror_interval_s` (60 s) and which
  is what spreads it in the tests.
- **The timer's event still holds the type only.** The message and the place
  are in the sidecar and nowhere in the daemon's store. Only `subfleet status
  --json` shows `last_error_type`, in the daemon's reply; no text output
  prints it.

## Review

Round 1 (GPT-6.1 Sol on a Subfleet lane, at `05c4e1e7a`): APPROVE, with
notes it rated P3. Fixed after it:

- **An interrupt after the end was written.** SIGINT delivered just after a
  full pass wrote `ok` left `cancelled` beside that pass's own `last_ok_at`.
  A record of a pass's end that the sidecar already holds now stands
  (`_holds_end`), also when the interrupt lands inside the write once the
  file is in place.
- **A note that cannot be added.** On an exception whose `__notes__` is no
  list, `add_note` raised `TypeError` in place of the pass's exception. It is
  guarded.
- **`.pyc`-only installs.** `_raised_at` matched frames by file name, which
  names the `.pyc` in `__file__` and the source in the code, and so named a
  frame outside the mirror. It matches by module name.
- **A coverage gap.** Removing `run_once`'s drop of its flag-only helper
  failed no test; a later pass without flag sync then synced flags through
  the old helper. An example test now covers it. The property varies flag
  sync too, but never reaches that sequence.
- **A health check.** One property failed Hypothesis's data-generation speed
  check under load. Both properties suppress it.
- **Claims.** The contract, this report and the test docstrings no longer say
  more than the code does about a second interrupt, an `OSError` from the
  end's write, a pass that never took the lock, and `subfleet status --json`.

Noted, not changed. Each is older than this change:

- A hot pass whose own end write fails with `OSError` swallows it and leaves
  its sampled start in place.
- An `OSError` raised inside `_lock`'s own handler reaches the pass's
  `OSError` handler, which writes the sidecar without the lock.
- An exception from unlocking, or from `_give_back` after a dry run, is
  raised in the `finally` block, outside these handlers.

Cost, measured by the review: 30 failing hot ticks wrote the sidecar 31
times where the base wrote it once, and both called `_settle` 30 times. An
error is recorded at once by design.

Round 2 (GPT-6.1 Sol, at `4745eec0a`): APPROVE, with notes it rated P3. It
found the two merges right, every round-1 note fixed or narrowed, and one
regression in the round-1 fix. Fixed after it:

- **An end taken for this pass's because it read the same (F1).** Records
  are stamped to the second, and an idle hot pass's start and end are
  sampled away. A hot pass that ran in the second of one that wrote `ok`,
  and then raised, found an equal `ok` in the sidecar and was not recorded:
  the failure was in `timer.error` and nowhere in the sidecar. The review
  saw it three times in 130 s under the daemon's own scheduling with an
  injected fault. Now a pass remembers the end it began to write
  (`_ending`), and only that one stands. The fix is the review's.
- **Nothing pinned that identity (F2).** Weaker comparisons (the state
  alone; the state and the start; no counts) failed no test. Example tests
  now do: a hot pass that raises beside an equal or an earlier `ok`, and an
  interrupt before a hot pass's `ok` is in place, in the same second (where
  only the counts differ) and two seconds on.
- **The read could replace the exception (F5).** A sidecar too deep to
  parse raised `RecursionError`, which `_load` does not catch. The read is
  guarded, and a pass that began no write reads nothing; a test spoils the
  sidecar after a pass wrote its end.
- **The module match was not pinned (F6).** Counting any `subfleet` module's
  frame as the mirror's failed no test; the module-name test now runs a
  frame of another `subfleet` module.
- **Claims (F3, F4, F7).** An `OSError` from the end's write is raised for a
  full pass only; a hot pass's is not, as before. The timer property never
  makes that write fail, and the sequence property never reaches the
  helper case; this report and the docstrings say so, and an example test
  runs the end-write `OSError` through the timer.

## Not established

- How often a live pass has ended this way. The store's `timer.error` events
  would say; this work read no live store.
