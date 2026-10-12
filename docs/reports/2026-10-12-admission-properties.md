# Admission properties, 2026-09-27

## Why

The adversarial review of 2026-09-24 (`~/reviews/formal-verification-2026-09-24/verify-s7-subfleet.md`,
section 3, M1) found that subfleet's live failures cluster in admission liveness and
non-interference, not in the protocol-safety core. There were four incidents in three days:

- 034d3d3: the whole `standard` tier held behind an Opus head on `reserve:fable:unmeasured`, beside eleven free Fable lanes.
- cb83e1b: capacity waits rechecked every second, 98,014 decision rows.
- de95879: three pinned Fable gate reviews on different lanes held each other for six hours.
- b841d0d: one unroutable pin raised inside the pass and stalled the fleet for 198 and 26 minutes.

On 24 September `daemon.log` showed `admission: 5 jobs pending, none placed for 4863 s; 14 lanes
open; behind-older-job x4, reserve:fable:unmeasured x1`.

The review proposed four properties of the admission pass and Hypothesis stateful tests to check
them against the real code. This change adds those tests, fixes the four violations whose fixes are
small and obviously correct, and pins the rest as strict `xfail` tests with their smallest scenario.

## The properties

- **P1 totality** (C-6.11, C-6.12): no error from evaluating one job's route ends the pass, and every
  job a pass leaves pending has a stated hold, with the details C-6.11 lists for its reason.
- **P2 non-interference** (C-6.9, C-11.2): a `behind-older-job` hold names an older job of the same
  tier that competes with the held job and whose own hold holds others back. Competing is judged by
  the models routing actually walks and the lanes the pins name. A younger job is never placed past
  an older job of its tier that competes with it and cannot be placed. A job that passes an older
  waiter leaves one slot free.
- **P3 bounded progress** (C-6.9, C-6.4): make capacity fair (every lane enabled, open and measured
  with room, every sensor sound) and end every attempt as soon as it starts. Then each job some lane
  would take is placed within a bounded number of passes, unless the chain of holds it sits in ends
  at a job that can never be placed. The fleet and every lane stay within their caps.
- **P4 recheck cost** (C-6.10): a look that reaches the verdict (`scheduler.verdict_signature`) its
  last look reached adds no decision row. Every look sets a later clock, no further out than the
  backoff allows. Over quiet stretches of up to twelve minutes, a job is looked at a bounded number
  of times: about six looks to reach the 30 s ceiling, then two a minute, plus a few after each
  change the clock itself brings.

## The harness

`tests/fake/test_admission_stateful.py` is a `RuleBasedStateMachine` around the real `Daemon`,
in-process and in the `state_daemon` shape: fake adapters, stubbed process identity, and no
guardian or provider ever started. Every module admission reads the time from runs on one virtual
clock, so a step can let an hour pass and nothing races a second boundary.

It generates:

- policies: `max_active_attempts` 1 to 4, one or two slots per lane, the Fable reserve on or off;
- lanes whose names collide, including a Codex lane that answers to the email its probe read (the
  2026-09-22 roster);
- queues across every tier. Jobs carry a model, a task, both, a lane pin, an authorization, and
  legacy raw pins: a name several lanes answer to, a name none answers to, a pin of the other
  provider. Some carry malformed exclusions;
- capacity: readings fresh and stale, closures by account or model, lanes enrolled and disabled,
  probe reservations;
- attempts that end `ok`, `transient` or `limited` through the real `_finalize`;
- cancellations and restarts (with pin canonicalization and recovery);
- failing sensors: an exception of each C-6.12 type from `evaluate` or `probe_required` for one
  job, a usage reading whose reset clock does not parse, a workspace that cannot be prepared, and
  admission probes that are limited, inconclusive, cannot run, or cannot be contained.

After every pass it checks P1, P2, the caps and P4. It also has rules that run many passes: one
drains the queue under fair capacity (P3), and one lets a quiet fleet sit for minutes (P4's bound).
At teardown it checks C-15.1: every notice agrees with its job row.

The checks compute what the contract says from the job rows (the chain a pinned job walks, the lane
a pin names), not by calling the functions under test. The two exceptions each keep a counterexample
out of the search that is pinned as an `xfail` in `tests/fake/test_admission_properties.py`. Each is
recorded as a Hypothesis `event`, so `--hypothesis-show-statistics` shows how often it happens.

Settings profiles (`HYPOTHESIS_PROFILE`):

- `ci`: the default when `CI` is set. 40 derandomized examples of up to 30 steps; about 10 to 25 s here.
- `dev`: the default otherwise. 60 random examples of up to 40 steps.
- `deep`: 1,000 random examples of up to 80 steps, for a long local search.

`HYPOTHESIS_NO_SHRINK=1` reports the first failure as found. One example can take a second, so
shrinking one takes minutes; rerun its seed without the variable to shrink it.

## Does the search have teeth

Each bug below was put back by hand, and the `deep` search then ran without shrinking. See the table
at the end for what it caught and how fast.

## Findings

### Fixed here

These are contract violations with small fixes. An independent reviewer reproduced each one and
judged its fix small and obviously correct: two reviewers for 1 and 3, one for 2 and 4. The stateful
search also found 1, 2 and the flaw in the first version of 4.

1. **A lane-pinned task job competed on its whole chain** (C-6.9, C-11.2; P2). C-6.9 says a task
   job's models are "exactly the chain routing walks", and C-11.2 says a pinned job "evaluates one
   model, its `-m` model or else the first model of its task's chain from its tier". `evaluate` does
   `if pin: chain = chain[:1]`, but `demand_models` returned the whole chain. So `-a claude-a
   --task review` (walks Opus) held back unpinned Astra work, and was held back by it, although the
   two can never share a model or a lane. Fix: `demand_models` narrows a lane-pinned task to its chain's first
   model.
2. **Fleet occupancy was part of the capacity-wait verdict** (C-6.10; P4). The wait was keyed on
   `verdict_signature` plus whether the fleet was at its limit (`:{live >= limit}`). The suffix is
   always true for a wait with a chosen lane: a job with room is placed. For a job no lane admits it
   only records whether other work happened to fill the fleet. Each time the fleet crossed its limit,
   every such job got a new decision row and its clock went back to 1 s. The regression test's seven
   looks at one verdict left 7 rows before the fix and 1 after. Fix: the key is the signature alone.
   The one existing test that asserted the suffix now asserts what it meant: the look leaves the
   signature untouched.
3. **A quarantined probe's job was a waiter for one pass** (C-4.1, C-6.11). `_prepare_route` sets
   the job `uncertain` when its probe cannot be contained. The `approved is None` branch then still
   registered the job as a C-6.9 waiter and reported it `probe-pending`, so a later job that could
   run elsewhere waited a pass, and `daemon.status` counted the job as pending. Fix: the branch reads
   the job first and reports `uncertain` (or nothing, for a job cancelled mid-pass) without
   registering it.
4. **A stored exclusion that is not a lane name ended every pass** (C-6.12; P1). Submit sorted
   exclusions after its validation, so a one-element list of anything (`[1]`, `[null]`) was stored.
   Once an attempt excluded a lane, the retry merge `sorted({1, "codex-1"})` raised `TypeError`
   inside the reserving transaction, which isolated only route evaluation, and admission stopped for
   everyone. The CLI's `-x` always sends strings, so only a socket client could store such a row. Fix: C-6.1
   now refuses exclusions that are not a list of names (exit 2), and the merge
   (`Daemon._merged_exclusions`) counts as evaluating the route. The first version of this fix did the
   merge inside the transaction. The search then found that its `route` wait never backed off past
   5 s: route preparation had already succeeded, which resets C-6.12's count, so it looked every 5 s,
   71 looks in six quiet minutes. The merge is now checked before the route is prepared, and the
   deferrals go 1, 2, 3.

### Found, not fixed: strict `xfail`

Each of these fails today at its last assertion, and `raises=AssertionError` keeps any other error
from hiding behind the marker. The fix that makes one pass must remove its marker.

5. **A capacity waiter held behind an older job holds nobody** (C-6.9; P2). C-6.9 says a job
   "`waiting` on `capacity` with a `next_check_at` still ahead ... holds back the later jobs of its
   tier that compete with it". `_admit_pass` checks `behind` before it registers a clocked capacity
   wait, so a job held behind W stops holding back the jobs that compete only with it. Adding an older
   job that the younger one does not compete with lets the younger one pass the middle one. The fix
   is two lines, but it adds holds, bounded by the middle job's remaining clock, so it is left for a
   change of its own with its own review.
6. **A retry that let its pin go is passed by a younger competitor** (C-6.12). This has the same root
   cause as 5. C-6.12 says such a job "neither passes an older job it competes with nor is passed by a
   younger one"; when the retry is itself held behind an older job, it is passed.
7. **A wait of another kind records the same verdict again** (C-6.10; P4). The wait's key carries
   its kind: `lease-held:…`, `probe-pending`, `probe-wait:…`, `retry-let-go:…`. A job that alternates
   between two kinds with one verdict therefore adds a row each time it comes back to the
   verdict-keyed kind. The regression scenario is a terra job whose output path another job holds.
   It waits `lease-held` while the fleet has room and `slot-kept` while it does not. The
   `probe-pending` path never has the decision in hand, so keying on the verdict alone is not a small
   change.
8. **A slot kept for a job that can never use it starves its tier** (P3). C-6.9's slot-keeping (a
   passer leaves `max_active_attempts` minus one) is unconditional, and so is "holds back the later
   jobs of its tier that compete with it, and no others". At a cap of 1 the two disagree: an Opus job
   with no Claude lane keeps the only slot for as long as it waits, and a Terra job that does not
   compete with it never runs beside a free Codex lane. A job that is never started is never ended by
   `max_wall_s`. At caps above 1 the tier loses one slot for as long as the waiter waits. This is a
   contract question, queued for a ruling.
9. **An admission probe always reserves slot 0** (P3; the contract is silent). `_prepare_route`
   leases `lane:<id>:slot:0`, and an attempt takes the lowest free slot. So a job that must probe
   (an authorized job, a revive, a reserve closed by the provider) chooses a measured lane with one
   attempt on slot 0 and a free slot 1 on every look. It waits `probe-pending` until that attempt
   ends, up to `max_wall_s`. With the same attempt on slot 1 it is probed and placed at once.

### Specified, but nothing bounds it

These reproduce, and the contract says to do them. Each would need an amendment, so none has a test
that asserts otherwise:

- A pin that names no lane, or a lane that is disabled or shut for good, waits forever (it is never
  started, so `max_wall_s` never fires). It holds every same-tier job it competes with on models.
  With no model it holds the whole tier, and it keeps the tier at `max_active_attempts` minus one.
  This is the shape of the 24 September line. Whether any of those four held jobs was a lane-pinned
  task that fix 1 would have released depends on rows this report did not read.
- A lane pin with no model "competes with every job" (C-6.9), although `evaluate` resolves it to its
  lane's provider's first model. A Claude-pinned job holds back unpinned Codex work.
- A child held only by its parent's cap holds back unrelated jobs of its tier.
- Timer probe cycles hold every idle lane's slot 0 until the whole cycle ends. That fills the fleet
  cap for the cycle and flips `slot-kept` verdicts, up to two rows per cycle per job.
- A closure's `until_at` is part of its rejection reason. A guessed closure re-extended every probe
  cycle is a new verdict, and a row, each time.
- A `slot-kept` or `lease-held` job that needs a probe runs a real provider probe on every look,
  including each look a released lease brings forward.
- A pass that raises publishes nothing (C-6.11), so `status` and `why` show the last whole pass
  while admission fails.

### Outside admission's clauses, but they end the pass

C-6.12 isolates route evaluation, not the probe. Both of these abort every pass that reaches the job,
as a store error would (C-5.10 paces the retries):

- A Codex lane whose `auth.json` is empty, torn, or not an object: `_validate_home` raises
  `JSONDecodeError` or `AttributeError` from `_execute_probe`.
- A probe receipt whose timezone key does not normalize: the Codex adapter's `_reset` catches only
  `ZoneInfoNotFoundError`, and `ValueError` escapes classification. `_recover_probes` classifies it
  again on the next pass.

Both have one-line fixes. They are split out as their own tasks.

## Also found

`tests/fake/test_admission_route_isolation.py::test_c6_9_a_younger_job_does_not_pass_a_retry_that_let_its_pin_go`
failed once in four runs on `3f155e5`. It sets a 1 s clock with `after(1)` and depends on the next
pass running before that second ends. It is split out to its own task.

## Running it

```sh
uv run pytest -q tests/fake/test_admission_stateful.py tests/fake/test_admission_properties.py
HYPOTHESIS_PROFILE=deep uv run pytest -q tests/fake/test_admission_stateful.py --hypothesis-show-statistics
HYPOTHESIS_PROFILE=deep HYPOTHESIS_NO_SHRINK=1 uv run pytest -x -q -p no:logging tests/fake/test_admission_stateful.py
```

## Mutation results
