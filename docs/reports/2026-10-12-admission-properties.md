# Admission properties on release/217, 2026-10-12

## Why

The adversarial review of 2026-09-24 (`~/reviews/formal-verification-2026-09-24/verify-s7-subfleet.md`,
section 3, M1) found that subfleet's live failures cluster in admission liveness and
non-interference, not in the protocol-safety core. Four incidents in three days:

- 034d3d3: the whole `standard` tier held behind an Opus head on `reserve:fable:unmeasured`,
  beside eleven free Fable lanes.
- cb83e1b: capacity waits rechecked every second, 98,014 decision rows.
- de95879: three pinned Fable gate reviews on different lanes held each other for six hours.
- b841d0d: one unroutable pin raised inside the pass and stalled the fleet for 198 and 26 minutes.

The review proposed four properties of the admission pass and Hypothesis stateful tests to check
them against the real code. The work was built on main in late September and is ported here to
release/217, the line the installed daemon is built from, at 0298332db. Every finding below was
checked again on release/217 before it was fixed, kept as an `xfail`, or dropped.

## The properties

- **P1 totality** (C-6.11, C-6.12): no error from evaluating one job's route ends the pass. Every
  job the pass leaves pending has a stated hold, with the details C-6.11 lists for its reason. A
  wait that is a person's or a retry's to end is reported as that.
- **P2 non-interference** (C-6.9, C-11.2, C-11.8): a `behind-older-job` hold names an older job of
  the same tier that competes with the held one, by the models routing walks and the lanes the pins
  name, and whose own hold holds others back. A lease, an unadmittable pin and a `for_good` refusal
  hold nobody. In a pool with a count cap, a younger competitor is never placed past an older job
  of its tier that waits for a slot, a lane or a probe (a moved route and a lane held for its pilot
  included, C-6.11, C-6.14), and a job placed past such a waiter, or past a lease waiter, leaves the
  last slot free. A probe turn's `behind` is rebuilt each pass and must name a job ahead of the held
  one that waits on a probe. In a pool with no count cap (the default) no job waits behind another.
- **P3 bounded progress** (C-6.9, C-6.4, C-11.8): the machine makes capacity fair (every lane
  enabled, open and measured with room, every sensor sound) and ends every attempt as soon as it
  starts. Then each job some lane would take is placed within a bounded number of passes, with no
  exception: a job no lane can admit holds nobody back and keeps no slot (C-11.8), so it can never
  be why another waits. The caps that are set hold.
- **P4 recheck cost** (C-6.10): a look that reaches the verdict (`scheduler.verdict_signature`) its
  last look reached adds no decision row. Every look sets a later clock, no further out than the
  backoff allows. Over quiet stretches of up to ten minutes a job is looked at a bounded number
  of times: about six looks to reach the 30 s ceiling, then two a minute, plus a few after each
  distinct instant at which the clock itself changes capacity (a reading going stale, a closure
  ending, a pilot's wait lapsing, a lane's proof going stale). The stretch always runs to its end.

## The harness

`tests/fake/test_admission_stateful.py` is a `RuleBasedStateMachine` around the real `Daemon`. It
runs in-process in the `state_daemon` shape: fake adapters, stubbed process identity, and no
guardian or provider ever started. Every module admission reads the time from runs on one virtual
clock, so a step can let an hour pass and nothing races a second boundary. A fixture session
registry stands in for this host's, so every job is `background` and the pass order is tier, then
age (C-6.9).

It generates:

- policies with no count cap (the default) or `max_active_attempts` 1 to 4, no per-lane cap or one
  or two slots, the reserve on or off, and C-6.14's pilot hold on (`prove_idle_s` 900) or off;
- lanes whose names collide, including a Codex lane that answers to the email its probe read;
- queues across every tier. Jobs carry a model (`fable` exercises the retired alias), a task, both,
  a lane pin, an authorization, and legacy raw pins: a name several lanes answer to, a name none
  answers to, a pin of the other provider. Some carry exclusions that are not names;
- capacity: readings fresh and stale, closures by account or model, lanes enrolled and disabled,
  probe reservations;
- attempts that end `ok`, `transient` or `limited` through the real `_finalize`;
- cancellations, and restarts with pin canonicalization and recovery;
- failing sensors: an error of each C-6.12 type from `evaluate` or `probe_required` for one job, a
  usage reading whose reset clock does not parse, a workspace that cannot be prepared, and admission
  probes that are limited, inconclusive, cannot run, or cannot be contained.

After every pass it checks P1, P2, the caps and P4. Two rules run many passes: one drains the queue
under fair capacity (P3), and one lets a quiet fleet sit for minutes (P4's bound). At teardown it
checks C-15.1: every notice agrees with its job row.

The checks compute what the contract says from the job rows (the chain a pinned job walks, the lane
a pin names) rather than calling the functions under test. One exception keeps a known
counterexample out of the search: a look of another wait kind may record its verdict again
(finding 7). It is pinned as an `xfail` in `tests/fake/test_admission_properties.py` and counted as
a Hypothesis `event`.

It is bounded by default, because every example builds a daemon and its store on disk. A plain
`pytest` run is 40 examples of at most 30 steps and writes no example database:

- `ci` is the default when `CI` is set, derandomized. The machine and its neighbours ran in 80 s
  under load here.
- `dev` is the default otherwise, random.
- `deep` is opt-in through `HYPOTHESIS_PROFILE`: 300 random examples of at most 60 steps, about one
  20-minute hold of the host's shared test lock. `HYPOTHESIS_MAX_EXAMPLES` resizes it.

A quiet stretch of up to ten minutes runs at most one pass a second, so at most 601 passes.

`HYPOTHESIS_NO_SHRINK=1` reports the first failure as found. An example can take a second, so
shrinking one takes minutes; rerun its seed without the variable to shrink it.

## Findings

### Fixed here

Each fix was checked as needed on release/217 at 0298332db. Each has a regression test in
`tests/fake/test_admission_properties.py` that fails on release/217 with the fix reverted.

1. **A lane-pinned task job competed on its whole chain** (C-6.9, C-11.2; P2). C-6.9 says a task
   job's models are "exactly the chain routing walks". `evaluate` gives a pinned job one model, the
   first of `mcp_chain(flatten_chain(...))`, as `pin_provider` already names its provider, but
   `demand_models` returned the whole chain. So `-a claude-a --task review` (walks Opus) held back
   unpinned Astra work in a capped pool, and was held back by it, though the two can never share a
   model. Fix: `demand_models` narrows a lane-pinned task to that one model.
2. **Whether a capped pool was at its limit was part of the capacity-wait verdict** (C-6.10; P4).
   The wait was keyed on `verdict_signature` plus `:{at_limit}`. That is constant for a wait with a
   chosen lane (a job with room is placed) and for any uncapped pool. For a job no lane admits it
   only recorded whether other work had filled the pool, so each time a capped pool crossed its
   limit every such job got a new decision row and its clock went back to 1 s. On main the
   regression test's seven looks at one verdict recorded seven rows; on release/217 it fails the
   same way with the fix reverted, and now records one. Fix: the key is the
   signature alone. The one existing test that asserted the suffix now asserts what it meant: the
   look leaves the signature untouched.
3. **A quarantined probe's job was a waiter for one pass** (C-4.1, C-6.11). On release/217 such a
   job already stays out of the probe line, but the `approved is None` branch still registered it
   as a C-6.9 waiter and reported it `probe-pending`. `daemon.status` therefore counted it as
   pending, and in a capped pool a later job that could run elsewhere waited a pass behind it. Fix:
   the branch reads the job first and reports `uncertain` (or nothing, for a job cancelled
   mid-pass) without registering it.
4. **A stored exclusion that is not a lane name ended every pass** (C-6.12; P1). Submit sorted
   exclusions after its validation, so a one-element list of anything (`[1]`, `[null]`) was stored.
   Once an attempt excluded a lane, the retry merge `sorted({1, "codex-1"})` raised `TypeError`
   inside the reserving transaction, which isolates only route evaluation, and admission stopped
   for everyone. The CLI's `-x` always sends strings, so only a socket client could store such a
   row. Fix: C-6.1 refuses exclusions that are not a list of names (exit 2). The merge
   (`Daemon._merged_exclusions`) is checked where the route is prepared, so once an attempt has
   excluded a lane, a stored one fails the merge and waits on `route` with deferrals 1, 2, 3. A job
   whose attempts excluded no lane never merges, and this change does not alter how it routes. A first version (on main) merged inside the transaction; the
   search found that route preparation had already reset C-6.12's count there, so the job was looked
   at every 5 s (71 looks in six quiet minutes).

### Fixed by release/217's design

Both were found on main in September and their tests are regressions here:

- **A probe always reserved slot 0, behind any attempt** (P3). On main an attempt took the lowest
  free slot from 0, so a job that had to probe waited `probe-pending` behind the attempt on slot 0
  with slot 1 free, up to `max_wall_s`. On release/217 C-6.9 gives `slot:0` to the probe alone.
- **A slot was kept for a job that could never use it** (P3). On main, at `max_active_attempts` 1,
  an Opus job with no Claude lane kept the only slot for as long as it waited, so a Terra job that
  does not compete with it never ran. On release/217 C-11.8 says such a job keeps no slot, and the
  machine now asserts it.

### Found, not fixed: strict `xfail`

Each fails on release/217 at its last assertion, in a capped pool (C-6.9's hold-back exists only
there). `raises=AssertionError` keeps any other error from hiding behind the marker. The fix that
makes one pass must remove its marker.

5. **A capacity waiter held behind an older job holds nobody** (C-6.9; P2). C-6.9 says a job
   "`waiting` on `capacity` with a `next_check_at` still ahead ... holds back the later jobs of its
   tier that compete with it". `_admit_pass` checks `behind` before it registers a clocked capacity
   wait, so a job held behind W stops holding back the jobs that compete only with it. In the test
   the middle job's own looks find no lane and back its clock off; then a cap is set and a Claude
   lane opens. With its clock still ahead it is held behind W, and a younger Opus job, which competes
   with it but not with W, is placed past it. The fix
   adds holds, bounded by the middle job's remaining clock, so it is left for its own change and
   review.
6. **A retry that let its pin go is passed by a younger competitor** (C-6.12). Same root cause as
   5. C-6.12 says such a job "neither passes an older job it competes with nor is passed by a
   younger one"; when the retry is itself held behind an older job, it is passed.
7. **A wait of another kind records the same verdict again** (C-6.10; P4). The wait's key carries
   its kind (`lease-held:…`, `probe-pending`, `probe-wait:…`, `retry-let-go:…`). So a job that
   alternates between two kinds with one verdict adds a row each time it returns to the
   verdict-keyed kind. On release/217 the scenario is a terra job whose output path another job
   holds, in a pool of three, pinned to codex-1. An older job waiting for its own output path keeps
   the last slot (C-6.9), so the job waits `slot-kept` at two live and `lease-held` at one. Every
   evaluation of the job is recorded and reaches one verdict on codex-1, so the rows are not a
   changed verdict. Three alternations record that verdict three times. The `probe-pending` path never has the decision in hand, so
   keying on the verdict alone is not a small change.

### Found on main in September, not re-checked on release/217

The reviewers reproduced these on main. The contract there specified each, so none has a test that
asserts otherwise. Admission on release/217 differs (uncapped pools, C-11.8, probe turns, pilots),
so each may no longer hold:

- A lane pin with no model "competes with every job" (C-6.9), though `evaluate` resolves it to its
  lane's provider's first model.
- A child held only by its parent's cap held back unrelated jobs of its tier.
- Timer probe cycles held every idle lane's slot 0 until the whole cycle ended, which filled the
  fleet cap and flipped `slot-kept` verdicts, up to two rows per cycle per job.
- A closure's `until_at` is part of its rejection reason, so a guessed closure re-extended every
  probe cycle is a new verdict, and a row, each time.
- A `slot-kept` or `lease-held` job that needs a probe ran a provider probe on every look.

### Outside admission's clauses, but they end the pass

C-6.12 isolates route evaluation, not the probe. Both of these were reproduced on main, where they
abort every pass that reaches the job as a store error would (C-5.10 paces the retries). On
release/217 the code is unchanged by reading; neither was run there:

- A Codex lane whose `auth.json` is empty, torn, or not an object: `_validate_home` raises
  `JSONDecodeError` or `AttributeError` from the probe.
- A probe receipt whose timezone key does not normalize: the Codex adapter's `_reset` catches only
  `ZoneInfoNotFoundError`, and `ValueError` escapes classification.

They are split out as their own task.

### Open

On main, one deep run reported a younger job placed past an older job of its tier that competed with
it and waited `excluded`. Two replays of the saved example did not reproduce it, and a scratch test of
the plain case held the younger job correctly. It is unexplained. On release/217 the FIFO check runs
only in a capped pool and only behind a waiter for a slot, a lane or a probe.

## Independent review

GPT-6.1 Sol reviewed 977fcc9f3 (Subfleet job `20261012-165823-review-pr199`) by reading the code
and the contract. It confirmed fixes 1 to 4 and the regressions' premises, and found six faults
in the tests and the contract text. Each is corrected in the commit after it:

- P1 compared a probe turn's `behind` with the last look's, though C-6.9 builds the probe line
  again each pass. It now checks the new owner instead.
- P2 left `route-moved`, `lane-proving` and probe-turn waiters out of capped pools' FIFO check,
  and lease waiters out of the last-slot check.
- P3 excused a job held behind one that could never be placed, which C-6.9 and C-11.8 forbid.
- P4 counted one boundary per reading rather than per instant, so many readings at one instant
  inflated the bound. Its 400-pass limit could also end a stretch early, and the pilot hold was
  never on.
- Finding 5's test set its clocks by hand rather than by a look. Finding 7's job could choose
  another lane between looks, which is a new verdict.
- The new C-6.12 sentence promised a `route` wait for a stored exclusion that is not a name even
  when nothing is merged with it.

## Does the search have teeth

On main (3f155e5 with the fixes above, 2026-09-27), each bug in the table was put back by hand and
the `deep` search then ran without shrinking. A clean baseline passed the same search: 1,000
examples in 18 minutes 44 seconds. The same sweep on release/217 has 15 mutations scripted, including
three for 217's own rules (an uncapped pool holding jobs, a lease waiter holding others back, a job
no lane can admit registered as a waiter). It waits for the host's disk pause to lift.

| Bug put back (main) | Caught by | Seconds |
|---|---|---|
| Fix 1 reverted: a lane-pinned task competes on its whole chain | P2 (`behind-older-job` without competition) | 49 |
| Fix 2 reverted: occupancy suffix back in the key | P4 (a repeated verdict added a row) | 2 |
| Fix 3 reverted: a quarantined probe's job is a waiter | not by the search in 900 s; by its regression test | – |
| Fix 4 reverted: the exclusions merge not isolated | P1 (the pass raised `TypeError`) | 2 |
| Fix 4 reverted: submit accepts exclusions that are not names | P1 (submit accepted `[1]`) | 118 |
| b841d0d: one job's route error ends the pass | P1 (the pass raised) | 6 |
| cb83e1b: every look is a new verdict | P4 (a repeated verdict added a row) | 3 |
| 034d3d3: a waiter holds its whole tier | P2 (held behind a job it does not compete with) | 1 |
| de95879: lane pins do not narrow competition | P2 (held behind a job pinned to another lane) | 14 |
| slot-keeping dropped | P2 (a job passed a waiter and left no slot) | 36 |
| a route wait holds back its tier | P2 (held behind a job whose hold holds nobody) | 6 |

## Running it

Every run of more than one file holds the host's shared test lock
(`~/reviews/subfleet-hub/test-run-rule.md`):

```sh
T="$(getconf DARWIN_USER_TEMP_DIR)sf-admission-$$"; mkdir -p "$T"
/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock env -u PYTHONPATH TMPDIR="$T/" \
  HYPOTHESIS_STORAGE_DIRECTORY="$T/hyp" \
  uv run --no-sync pytest -q -p no:cacheprovider tests/fake/test_admission_stateful.py tests/fake/test_admission_properties.py
/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock env -u PYTHONPATH TMPDIR="$T/" \
  HYPOTHESIS_STORAGE_DIRECTORY="$T/hyp" HYPOTHESIS_PROFILE=deep HYPOTHESIS_NO_SHRINK=1 \
  uv run --no-sync pytest -x -q -p no:cacheprovider -p no:logging tests/fake/test_admission_stateful.py
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$T"
```
