# A failed mirror pass was forgotten when the next pass started, 2026-10-10

The fix for passes that an exception ended
(`docs/reports/2026-10-10-mirror-pass-failure.md`) left one gap, which its
report names: the sidecar holds one full pass, and each pass writes its start
before it does any work. The start of the pass after a failed one replaced
the failed pass's record, and every reading taken while that pass was in
flight said only "a pass has been in flight". This report records how long a
pass is in flight, live and on a fixture store, what each reader said before
and after the change, the change, the question it leaves to Max, the
invariants, and how each is checked.

No live store was written. The live sidecar was read, read-only, by a
sampler. Every other reading is from a store, a `~/.claude`, an app log and a
state root under a temporary directory.

## How long a pass is in flight

### Live

A sampler opened `~/.subfleet/sessions/mirror.json` for reading five times a
second for 15 minutes (2026-10-10 20:41 to 20:56 UTC) and kept `pass` from
each reading.

| | |
|---|---|
| Readings | 4,048, none unreadable |
| Readings with `pass.state` `running` | 985 (24.3%) |
| Full passes on record | 15, all `ok`, none with an error: 14 seen from start to end, and one that had ended before the sampler started |
| Passes that did not sweep | 14: 3 to 9 s each from their recorded start and finish (whole seconds), mean 5.9 s |
| Passes that swept (stat every entry) | 1: 133 s, at 20:45:18 |
| Time from one start to the next | 60 s, and 133 s after the swept pass |

The swept pass is the ten-minute stat sweep (`SWEEP_INTERVAL_S`). One swept
pass was seen, so 133 s is one measurement, not a typical length. No live
pass failed while the sampler ran, and the daemon's `timer.error` events were
not read, so how often live passes fail is not known.

### Where a pass fails decides how long a failing pass runs

A pass that fails at its first step is in flight for a moment. One that fails
late runs nearly its whole length. And a pass that fails while it sweeps
leaves the sweep due: `_last_sweep` is set only on `_pass`'s last lines, so
until a pass gets there, every pass sweeps. A new process's first pass always
sweeps. On a fixture store, 12 passes a minute apart that `_spread` ended
with RuntimeError swept 12 times out of 12; 12 clean passes swept twice,
the first and the eleventh, 600 s on (scenes S1 and S2 in the reproduction).

A failure after `_pass`, in its last checkpoint or in the record of its end,
comes after the sweep's clock has moved, and leaves no sweep due.

Deduced from the timer's code, not observed live: `Timers.tick` makes the
mirror due 60 s after the last start and runs one pass at a time, so a pass
longer than 60 s is followed by the next at once. If passes that fail late in
a sweep took as long as the one swept pass seen (133 s), a mirror whose every
pass did so would be in flight nearly all the time. How long a failing pass
takes live, and how often live passes fail, were not measured.

### On a fixture store

The passes were started by the daemon's scheduler, `Timers.tick()`, on its own
mirror worker, on the policy's 60 s interval. Each pass ran a chosen time and
then raised from `Mirror.folders`, its first step. Time was simulated: the
clock moved only when the script moved it, one second at a time. The three
readers were the real ones, asked at every second for 600 seconds from the
start of the second pass, so every reading came after at least one failed
pass: `Mirror.health()`, `subfleet sessions mirror --status`, and `doctor`'s
mirror row (`check_mirror`).

Readings out of 600, before the change (#170's head, 05c4e1e7a) and after
carrying the run (#172, before the rule below). The count of readings
`running` is also the count of
`--status` exiting 0 and of `doctor` passing its mirror row: the three
readers gave the same counts.

| Each pass runs, then fails | `running`, before | `running`, after | `doctor`'s line names a failure, before | after |
|---|---|---|---|---|
| 1 s | 10 | 10 | 590 | 600 |
| 5 s | 50 | 50 | 550 | 600 |
| 9 s | 90 | 90 | 510 | 600 |
| 20 s | 200 | 200 | 400 | 600 |
| 40 s | 400 | 400 | 200 | 600 |
| 59 s | 590 | 590 | 10 | 600 |
| 75 s | 593 | 593 | 7 | 600 |

The rows are the same for OSError and for RuntimeError. `running` is the
share of the interval a pass is in flight, or, once a pass outruns the
interval, all but the second between one pass's end and the tick that starts
the next (the script ticked once a second; the daemon ticks every 0.05 s). Before the change,
every reading in flight said "a pass has been in flight for 0.0 min" and
nothing else; only the readings between passes said "last pass failed: …".
After it, every reading names the failure, and the status of each is what it
was. Two control runs of passes that end `ok` (9 s and 40 s) name no failure,
before or after.

## The change

`subfleet/sessions/mirror.py`:

- The full pass's record (`_record`) also writes, beside `last_ok_at`:
  - `last_end`: the last full pass that is over: its state, error, start,
    finish and stage. A pass that the next pass to take the lock finds still
    `running` is over and recorded no end (its process died, or its end could
    not be written), and is kept as state `unfinished`.
  - `not_ok_passes`: how many full passes in a row, that one the last, did
    not end `ok`.
  - `not_ok_since`: when the first of them started.
- A pass works the three out once, when it first records (`_over`): from the
  `pass` record it replaces and the three beside it. Its start and its
  progress carry them as it found them; its end replaces them. An end
  written twice (an error, then the interrupt that cut the first write's
  handler short) counts once.
- `_over` holds the three to the `pass` record. A sidecar written before the
  change has no three, so its record is the whole run that is known: a failed
  one is a run of one. Values that are not the three's are treated the same
  way: a count that is no whole number from 0 to `RUN_LIMIT` (a billion), a
  start that is no time, and a `last_end` that is not the `pass` record field
  for field, as every end the mirror writes leaves it. A count 4,300 digits
  long had made the next end raise when it wrote the count back (review of
  this change).
- Every record, the full pass's and the hot pass's, is written whole over the
  sidecar the writer read (`_writable`). The writers had read it as the
  readers do, taking any failed read for an empty sidecar, so a record
  written after one replaced the run, `last_ok_at` and the other pass's
  record with nothing. The independent review showed it with a real EMFILE:
  two failed passes, then a third whose read at its start failed, recorded a
  run of one. (The hot record's erasure is older than this change.) Now an
  absent sidecar, one that holds no JSON object and one that no one can read
  (no regular file, permission denied: `_permanent`) have nothing to keep and
  are written over, as before; one that cannot be read for a cause that may
  pass (a full descriptor table, an I/O error) is left as it is. A start or progress record is then not written, and the pass's next
  record reads again; an end raises the OSError, which the pass handles as it
  does a failed write of its end (#170): it ends `error` over the sidecar it
  reads next, or, if no read works, leaves its start for the next pass to
  count as unfinished.
- Health (`_full_health`) reads the three: while a pass is in flight, as its
  start found them; once the record is over, as `_over` holds them to it. It
  names them in its detail and returns them, with `last_ok_at`, in its reply.

A hot pass's record, a dry run and a pass that never took the lock are
unchanged, and change none of the three. `doctor` and `sessions mirror
--status` are unchanged; they print the detail health gives them.

## What each reader says now

With the run carried (#172, before the rule below), after one failed pass,
while the next is in flight:

    mirror running: a pass has been in flight for 0.7 min; the pass before it failed: RuntimeError: no pass expects this (mirror.py:2473 in _pass)

After three, while the fourth is in flight:

    mirror running: a pass has been in flight for 0.0 min; the 3 passes before it did not end ok, and the last failed: Odd: third (mirror.py:… in _pass); no pass has ended ok since 2026-10-13T12:26:40Z

Between passes:

    mirror stalled: last pass failed: Odd: third (…); 3 passes in a row did not end ok; no pass has ended ok since 2026-10-13T12:26:40Z

A pass that recorded no end is named as one: "the pass before it recorded no
end (last stage: reading entries)". After a run of one, the detail names the
failed pass only; from two, it adds how many in a row and when a pass last
ended `ok`. `--status --json` carries `last_end`, `not_ok_passes`,
`not_ok_since` and `last_ok_at` whatever the run.

Carrying the run left the status as it was: on the fixture store above, the
readings `running` were as many as before at every pass length, and every
reading after the first failure named one. The rule below changes the first
two lines' `running` to `stalled`.

## The status of a pass in flight

C-23.28's first sentence said a pass in flight is healthy until thirty
minutes after its recorded start. Carrying the run made every reading name
the failure, and left that status as it was, so three readings were put to
Max (d1286):

1. A pass in flight reads `stalled` while the last pass that is over did not
   end `ok`: `doctor` fails and `--status` exits 1 from the first failed pass
   until a pass ends `ok`, in flight or not. It is the reading between passes
   carried across the next start, and it never turns between `stalled` and
   `running`. Its cost: a lone failure, or a daemon stop that caught a pass
   mid-flight (recorded `cancelled`), keeps `doctor` failing through the
   length of the next pass, where it had failed only until that pass started.
2. `stalled` once no pass has ended `ok` for longer than a limit, say
   `mirror_stall_min` (10 min) from `not_ok_since`. Applied only to a pass in
   flight, a lone failure lets the next pass read `running`, but between
   passes it already reads `stalled`, so for the first ten minutes the
   reading turns between the two once a minute. Applied to both (a lone
   failure reads healthy between passes too, until the limit), it does not
   turn; it changes what #170 made a failed pass read between passes, and a
   mirror that never succeeds passes `doctor` for those ten minutes.
3. Keep `running`, with the failure in every reading's detail.

The first is the rule. A pass in flight after a pass that ended `ok`, or
after none, is healthy for thirty minutes, as before; one in flight after a
pass that did not end `ok` reads `stalled` with the same detail:

    mirror stalled: a pass has been in flight for 0.7 min; the pass before it failed: RuntimeError: no pass expects this (mirror.py:… in _pass)

On the fixture store above, every reading after the first failure reads
`stalled`, `--status` exits 1 and `doctor` fails its mirror row: 600 of 600
at every pass length from 1 s to 75 s, for OSError and for RuntimeError. The
two control runs of passes that end `ok` read as before (9 s: 90 `running`,
510 `healthy`; 40 s: 400 and 200).

## Invariants

For the three the full pass's record keeps:

1. **The run.** After any passes, `not_ok_passes` counts the full passes since
   the last that ended `ok` (all of them, if none did) that left a record, a
   pass whose end the next pass found unrecorded among them. `not_ok_since` is
   the first one's start and is null exactly when the count is 0. `last_end`
   is the last pass that is over, `unfinished` for one that recorded no end.
2. **A start carries, an end replaces.** While a pass is in flight the three
   are what they were when it started.
3. **Once.** An end written twice counts once, and two passes that start in
   the same second are two.
4. **Only a full pass that took the lock.** A hot pass, a dry run and a pass
   that never took the lock change none of the three.
5. **Held to the record.** Once `pass` is over, `last_end` is that pass, and
   the count is 0 exactly when it ended `ok`.
6. **One status changes.** A pass in flight, inside the hang limit, after a
   pass that did not end `ok` reads `stalled`. Every other reading has the
   status it has with the three removed, and every detail begins with the
   detail it gives without them.
7. **Named in flight.** While a pass is in flight, the reading names the last
   pass before it that did not end `ok` whenever there was one, and how many
   in a row when there were two or more.
8. **Any value.** No value in the three, from an older mirror or a hand edit,
   makes a reading or a pass raise, and a pass over such a value writes a sane
   run.
9. **Never over a sidecar it could not read.** No record, full or hot, is
   written over a sidecar that exists and could not be read, so a failed read
   loses nothing: a pass that could not read at its start still counts the
   passes before it, and one whose every read failed leaves the sidecar as it
   was.

## Verification

`tests/unit/test_sessions_mirror_failure_carried.py`, 28 tests.

- Example tests: the pass after a failed one, by RuntimeError and by OSError,
  read by `Mirror.health()`, `--status`, `--status --json` and `doctor`'s row
  (2, 7); a run of three failures in three ways between two clean passes,
  between passes and in flight (1, 2, 7); a cancelled pass named as cancelled;
  a pass whose process died, alone and after two failures, and one whose end
  could not be written, counted as unfinished by the next (1); an end written
  twice, two passes in one second, and a start that was written and then
  raised (3); hot passes, dry runs and passes without the lock (4); a sidecar
  written before the change (5); five hand edits of the three (8); a read of
  the sidecar that fails at a pass's start, at its end, at every read of its
  end, and in a hot pass, a FIFO where the sidecar belongs, and a sidecar whose
  read is denied (9); the hang limit itself; passes started by
  `Timers.tick` on the 60 s interval, read every 5 s, where every reading
  after the first failure names one (6, 7).
- `test_the_run_is_carried_across_every_sequence_of_passes` (Hypothesis,
  120 cases): up to seven steps, each a full, hot or dry pass or one without
  the lock, ending `ok`, by OSError, by RuntimeError, by the daemon's stop, by
  KeyboardInterrupt, by its process dying at a checkpoint, with an end the
  sidecar could not take, or with one or two of the writers' reads of the
  sidecar failing (any of the first twelve; a full pass makes 9 to 11, its
  end the last), with a flag-only hot pass serviced at every checkpoint or
  none. A reading is taken at any checkpoint of the pass, after 0 s, 5 s,
  40 s or 31.7 min of it (past the hang limit), and at 0, 30 or 700 s after
  it. A model of the run is checked after every pass and at every reading in
  flight (1, 2, 4, 5, 7, 9), and the differential at every reading (6). One
  run met a failed read in 42% of cases that ended `ok` and 31% that ended
  `error`, readings in flight after one failure and after several, after an
  unfinished pass, and before a pass could record its start.
- `test_no_value_in_the_three_changes_a_status_or_raises` (Hypothesis, 400
  cases, and seven fixed examples that random draws reach only now and then:
  a count that is a boolean, below zero or not whole, a run beside a record
  that ended `ok`, a start that is no string): any JSON value in the three,
  beside records of every state (6, 8).
- `test_a_pass_over_any_value_in_the_three_records_a_sane_run` (Hypothesis,
  25 cases): a real pass over a hand-edited three (8).
- Mutants, each one change to this change, run against the test file. At the
  first commit (52f1ecc24) 25 of 26 were caught: C21 (a count that is a
  boolean is taken for a number) survived; the reviewer's rerun caught C21
  and missed C22 (a negative count). Both had been caught only by a random
  draw, as had C09, C11 and C26, so each now has a fixed case. Eight more
  cover the review's fixes (the read rule, the bound, the field-for-field
  check, a FIFO where the sidecar belongs, a sidecar whose read is denied).
  Over 34 in one run on the fixed tree, 33 were caught; C31 (a sidecar that
  is no regular file taken for an unreadable one) survived, so it now has a
  test, and a rerun caught it; C35 came with its test. Four more cover the rule (undone; every pass in flight `stalled`; only a run
  of two or more; not after a pass that recorded no end). The reviewer's three (a snapshot taken after the first
  write, a pass known by its start, the hang limit's boundary) are now tests.
  The script and its results are with the review's evidence, outside the
  repository.

The pass-failure tests and the mirror's other health tests pass unchanged.

## What it does not do

- **A hot pass's failures stay detail on the full pass's status.** They are
  neither counted in the run nor change it.
- **A pass whose process died still reads `running`** until thirty minutes
  after its start or until the next pass starts, after a pass that ended `ok`
  or none; after one that did not, it reads `stalled`, as any pass in flight
  then does. The next pass names it.
- **A pass that could read the sidecar at none of its records leaves none**,
  and is not counted: the sidecar is as the pass before it left it.
- **The timer's event still holds the type only.**

## Not established

- How long the first pass of a new daemon process takes live (it reads every
  entry). None was sampled.
- How often live passes fail. The store's `timer.error` events would say for
  exceptions other than OSError; they were not read.
