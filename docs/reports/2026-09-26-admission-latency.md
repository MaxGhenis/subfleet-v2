# Admission latency on 2026-09-26: a turn queued 12 minutes

Every number below comes from a command run on 2026-09-26, and each run is reproducible with `tools/store_contention_repro.py`. The raw reports are under `docs/reports/2026-09-26-admission-latency/`.

The baseline is `e053b2c`, the desktop release line installed as 2.1.6 at 13:25Z that day, exported with `git archive`. "Fix" is this change.

## What was seen on the live daemon

These observations come from the brief for this work. They were taken from the live daemon's `daemon.log` and `subfleet why`; this session did not read the live state root.

- **Turn waits.** A conversation turn submitted from the app waited about 12 minutes before admission placed it. A Claude turn and a Codex turn each waited about 725 s in `queued`, then finished in about 20 s once placed.
- **`why`.** Twelve minutes in, `subfleet why` said "no admission pass has reached this job yet".
- **Load.** The machine was at load average 110 to 170, with 48 jobs queued.
- **Lock watch (C-3.6):**
  - the admission thread held the store lock for 7.7 to 12.6 s at a time;
  - other threads waited up to 43 s for it;
  - read-pool waits reached 10 s.
- **The holder's stack, every time:** `_admit -> _admit_pass -> _route -> _pick -> _capacity_rows -> store.latest_reading_candidates -> query`. That is the second route evaluation, made inside the `attempt.reserved` transaction.

## Causes

1. **The reservation evaluated the route again inside its transaction whenever any commit had landed since the first evaluation.**
   - C-6.3 reused the early evaluation, made off the lock, only if `store.generation` had not moved.
   - The generation counts every commit on the daemon's store: hook events, notices, attempt states, readings. With dozens of writers, one nearly always landed in between.
   - So nearly every reservation built a whole capacity view with the store lock held. That meant the candidate readings query plus all attempts and jobs, turned into dicts and a view, in Python.
   - On a starved machine, 13–39 ms of query plus the Python work became seconds of wall time with the lock held.
   - The 5 s `ROUTE_REUSE_S` bound made it worse: any reservation that waited longer than 5 s for the lock threw its early evaluation away.
2. **A job was evaluated twice before its reservation, even when nothing moved.** `_prepare_route` evaluated the route to decide on a probe, and the early evaluation came right after it.
3. **Turns queued behind detached work.**
   - Admission was one pass. It took the queue as read at its start and sorted turns only ahead of detached jobs of their own tier.
   - A turn submitted during a pass waited for the whole pass to end. At the next pass it still waited behind every due `trivial` and `easy` detached job, and behind each one's workspace git calls, evaluations and probes (up to 60 s each).
   - With 48 jobs queued and every look slowed by load, one pass could take minutes.

## What changed

### C-6.3: no route is evaluated with the store lock held

- **Reuse of the probe decision.** `_prepare_route`'s evaluation is the one reserved on (`Daemon._early_routes`). A job is evaluated once per try, not twice.
- **What the early evaluation records.** `_pick(basis=)` records what the check needs:
  - the policy object;
  - the job as it was evaluated;
  - the view;
  - the snapshot rows, including the newest reading at the snapshot (`reading_mark`);
  - the reset-credit overrides it held out;
  - the horizon;
  - the desktop identity.
- **The check inside `attempt.reserved`.** `Daemon._route_stands` reads a few rows (`_route_rows`):
  - every lane row, marked and merged as a view does (`capacity.mark_desktop`, `Timers.merge_lane`, `capacity.credential_latched`);
  - the attempts in flight (`attempts_live`), with their job's kind and parent;
  - the probe leases;
  - the readings added since the snapshot, by id;
  - the closures not released (`closures_active`);
  - the reset-credit override context, with each lane's override decided again for the lane rows now;
  - the parents of the jobs a parent cap counts.
- **`route_check.still_stands`.** It compares each lane's facts with the early view: the `scheduler.LANE_FACTS` fields, readings, open closures, attempts in flight in the job's pool, and slot block.
  - A lane whose facts are unchanged is judged as it was. The clock cannot change its verdict before the early decision's horizon, and the check refuses at or past the horizon.
  - Each changed lane is judged again, alone (`scheduler.judge_lane`), and ranked against the candidates that did not change (`scheduler.rank_key`).
- **When the changed lanes decide it alone,** the check returns the decision an evaluation of those rows at this clock would make (`route_check.as_now`), and the transaction goes on with it. Three cases:
  - the chosen lane still ranks first;
  - another lane now ranks first, or takes an earlier model of the chain;
  - for a no-lane decision, a lane now takes a model, or none does and each changed lane's reasons are now's.

  The returned decision's verdict, details and evidence are now's. So a probe need is read from now's detail, and a wait recorded on it is clocked by now's verdict (C-6.10).
- **When they do not decide it,** the check refuses and the transaction rolls back before it has written anything. The route is then evaluated again, off the lock, and checked again, up to `ROUTE_TRIES` (3) times in a pass; after that the job keeps its C-6.9 place as `route-moved`. They do not decide it when:
  - a fleet or parent cap began or ended;
  - the chosen model lost every lane and the chain goes on past it;
  - a pin now names another lane;
  - the policy was reloaded;
  - the overrides are not the ones held out;
  - a snapshot reading was deleted;
  - a row cannot be parsed;
  - the clock is past the horizon, or earlier than the instant the view was built at.
- **`ROUTE_REUSE_S` is gone.** Age alone voids nothing.
- **`scheduler.evaluate` is split** into `prepare`, `judge_lane`, `rank_key`, `model_reason` and `decision_reason`, so one lane can be judged alone. Its behaviour is unchanged: a differential test against the old function, kept verbatim in `tests/reference_scheduler.py`, pins this.

### C-26.9: turns have an admission pass of their own

- **Two passes.** `_admit` runs a turn pass, then a detached pass. The control loop also runs the turn pass alone, on its own worker (`admission:turns`), beside the detached pass.
  - A turn submitted while a detached pass is busy (evaluating, preparing a workspace, waiting on a probe) is placed by the next turn pass.
  - With no turn queued and none held, the turn pass is one indexed statement.
- **Separate state.** Each pass keeps its own holds and freed-lease record.
- **Rules that hold across both passes:**
  - placements are noted for C-6.11 as each is made;
  - the reservation checks inside its transaction that the job has no attempt in flight;
  - a turn half that raises does not stop the detached half, and its error is raised after it (C-6.12).

## Invariants

These hold for every input and are tested as properties:

- **I1. No evaluation under the lock.** No route is evaluated, and no capacity view is read or built, while admission holds the store lock.
- **I2. Every reservation matches a full evaluation made at that moment.** Its lane, model and recorded decision are what `scheduler.evaluate` would return over the rows the reserving transaction reads, at its clock. The only differences are `evaluated_at` and the reading ages of lanes that did not change. So every cap and refusal `evaluate` enforces holds:
  - per-lane slots, measured or unmeasured;
  - the fleet cap, counting reserved probes;
  - the turn pool;
  - parent caps;
  - closures, exclusions, the desktop, identity mismatches, probe-held and latched credentials, the floor, the reserve.
- **I3. No double-booking.** A slot lease is chosen inside the reserving transaction, which the store lock serializes. No two live attempts share one, and a job never has two live attempts.
- **I4. A refused check writes nothing.**
- **I5. Bounded work per job.** A job's route is evaluated at most `ROUTE_TRIES` times per pass. A deferred job keeps its place and is looked at again on the next pass.
- **I6. Turns never wait for detached work.** A turn is only ever in a turn pass, one at a time, so it never waits for a detached job's evaluation, workspace or probe.
- **I7. The split changed nothing.** The split `evaluate` equals the old one on every input, error for error.

Why admission cannot double-book or exceed a cap: the check runs inside the transaction, and no other commit can land until it ends. By I2, it goes on only with the decision the old code's in-transaction evaluation would have made at that instant. The slot search and the lease and attempt checks are unchanged and inside the same transaction. So the old code's safety argument carries over as it stands, and the check adds only the cheap reads.

## Measurements

All runs used `tools/store_contention_repro.py` on this 18-core machine. The machine's own load average ran from 21 to 86 during the runs, from other sessions. It is logged at each run's start and end in `runner-1.log` to `runner-5.log` beside the reports.

**The seeded store** was shaped like the live one: 300 accepted jobs, 200k events, 34k readings and 3k decisions.

**The load:**
- 6 long jobs;
- 21 hook sessions;
- a short job every 8 s;
- 18 CPU-bound spinners at `nice 10`;
- a 48-job detached backlog (`--backlog 48 --backlog-s 30`);
- a conversation turn every 10 s (`--turns-every 10`).

**How turns were measured.** Each turn is submitted inside the daemon through the dispatcher's own `submit(args, turn=)` and cancelled as soon as it is reserved, so no provider is ever started. `turn_queued_to_reserved` is from the submit call to the `reserved` boundary.

**What the columns mean:**
- "Reserving hold" is the wall time the `attempt.reserved` transaction held the store lock.
- "CPU under the lock" is that thread's own CPU time during the hold (`thread_time`, recorded from B5 on). At 5% duty, or at high load, the wall time is mostly the holder waiting for its run window or the GIL, so the CPU column is the measure of work done under the lock.

**Code in each run:**
- "Fix, early" (A1–A6) is this change before the idle turn pass returned early; on every tick it ran a turn pass even with no turn queued.
- "Fix" (A7–A10) is the code of 9dd4f35. c33b3f6 changes only error paths, the recorded view instant, and wording.
- The baseline is `e053b2c`.

#### Unthrottled: 6 running, 21 hook sessions, 48-job backlog, a turn every 10 s (120 s)

| run | reservations that evaluated inside the lock | reserving hold p50 / p99 / max | CPU under the lock p90 / max | turns: n, queued→reserved p50 / p90 / max | turns never reserved | hook `list` p99 | client errors |
|---|---|---|---|---|---|---|---|
| baseline (B1) | 21 of 66 | 1.1 ms / 262.7 ms / 262.7 ms | - / - | 11: 0.61 s / 1.32 s / 1.83 s | 0 | 0.80 s | 0 |
| fix, early (A1) | 0 of 74 | 1.4 ms / 26.9 ms / 26.9 ms | - / - | 11: 0.29 s / 0.63 s / 0.84 s | 0 | 1.28 s | 0 |
| fix (A9) | 0 of 65 | 1.3 ms / 9.8 ms / 9.8 ms | 1.7 ms / 3.9 ms | 11: 0.42 s / 1.16 s / 1.45 s | 0 | 0.68 s | 0 |

#### Held to 5% of a core (`--duty 0.05`), same load (120-150 s)

| run | reservations that evaluated inside the lock | reserving hold p50 / p99 / max | CPU under the lock p90 / max | turns: n, queued→reserved p50 / p90 / max | turns never reserved | hook `list` p99 | client errors |
|---|---|---|---|---|---|---|---|
| baseline (B2) | 15 of 46 | 0.9 ms / 473.2 ms / 473.2 ms | - / - | 10: 4.81 s / 15.61 s / 15.61 s | 0 | 7.00 s | 0 |
| baseline (B7) | 21 of 55 | 0.9 ms / 1731.5 ms / 1731.5 ms | 25.8 ms / 36.1 ms | 13: 4.42 s / 8.47 s / 14.40 s | 0 | 6.27 s | 12 |
| baseline (B8) | 15 of 64 | 1.1 ms / 273.9 ms / 273.9 ms | 23.1 ms / 32.7 ms | 13: 4.50 s / 9.45 s / 10.91 s | 1 | 10.24 s | 0 |
| fix, early (A2) | 0 of 46 | 1.4 ms / 20.5 ms / 20.5 ms | - / - | 10: 6.01 s / 12.69 s / 12.69 s | 0 | 18.52 s | 0 |
| fix (A7) | 0 of 64 | 1.3 ms / 247.1 ms / 247.1 ms | 2.0 ms / 4.4 ms | 12: 3.89 s / 7.33 s / 8.24 s | 0 | 5.29 s | 0 |
| fix (A8) | 0 of 61 | 1.4 ms / 254.9 ms / 254.9 ms | 2.0 ms / 3.0 ms | 13: 2.96 s / 5.79 s / 7.82 s | 0 | 6.22 s | 0 |

#### Each detached job's workspace takes 2 s to prepare, 20 lanes (`--prepare-s 2 --lanes 20`, 150 s)

| run | reservations that evaluated inside the lock | reserving hold p50 / p99 / max | CPU under the lock p90 / max | turns: n, queued→reserved p50 / p90 / max | turns never reserved | hook `list` p99 | client errors |
|---|---|---|---|---|---|---|---|
| baseline (B3) | 1 of 64 | 1.1 ms / 23.3 ms / 23.3 ms | - / - | 14: 0.49 s / 2.17 s / 6.34 s | 0 | 0.46 s | 0 |
| fix, early (A3) | 0 of 62 | 1.8 ms / 308.0 ms / 308.0 ms | - / - | 14: 0.28 s / 0.76 s / 1.26 s | 0 | 0.56 s | 0 |

#### The same, held to 5% of a core

| run | reservations that evaluated inside the lock | reserving hold p50 / p99 / max | CPU under the lock p90 / max | turns: n, queued→reserved p50 / p90 / max | turns never reserved | hook `list` p99 | client errors |
|---|---|---|---|---|---|---|---|
| baseline (B4) | 8 of 47 | 1.2 ms / 373.1 ms / 373.1 ms | - / - | 12: 5.55 s / 11.89 s / 13.33 s | 1 | 6.53 s | 1 |
| fix, early (A4) | 0 of 49 | 1.9 ms / 1235.3 ms / 1235.3 ms | - / - | 13: 3.51 s / 6.23 s / 9.53 s | 0 | 5.24 s | 0 |
| baseline (B6) | 10 of 49 | 1.2 ms / 2866.4 ms / 2866.4 ms | 25.2 ms / 28.8 ms | 13: 5.85 s / 12.32 s / 13.27 s | 0 | 5.15 s | 0 |
| fix, early (A6) | 0 of 40 | 1.7 ms / 2340.8 ms / 2340.8 ms | 2.7 ms / 3.6 ms | 13: 4.50 s / 8.16 s / 11.66 s | 0 | 14.37 s | 20 |

#### The 48-job backlog is `trivial` tier, preparation 2 s, 20 lanes: the live shape, turns behind lower-tier work

| run | reservations that evaluated inside the lock | reserving hold p50 / p99 / max | CPU under the lock p90 / max | turns: n, queued→reserved p50 / p90 / max | turns never reserved | hook `list` p99 | client errors |
|---|---|---|---|---|---|---|---|
| baseline (B5) | 0 of 81 | 1.0 ms / 14.8 ms / 14.8 ms | 1.5 ms / 2.6 ms | 14: 3.22 s / 7.89 s / 13.48 s | 0 | 0.43 s | 0 |
| fix, early (A5) | 0 of 76 | 1.9 ms / 86.5 ms / 86.5 ms | 2.6 ms / 3.3 ms | 14: 0.50 s / 2.36 s / 2.75 s | 0 | 2.14 s | 0 |
| fix (A10) | 0 of 78 | 1.7 ms / 9.7 ms / 9.7 ms | 2.5 ms / 3.3 ms | 14: 0.43 s / 2.13 s / 2.58 s | 0 | 1.52 s | 0 |

What the runs show:

- **No evaluation under the lock.** No fix run evaluated a route inside a reservation. The baseline did so in 0 to 21 reservations per run, up to 38% of them (B7: 21 of 55).
- **Work under the lock** (CPU, 5% duty, B6–B8 against A6–A8):
  - Baseline: p90 23–26 ms, max 29–36 ms. That is the in-transaction evaluation.
  - Fix: p90 2.0–2.7 ms, max 3.0–4.4 ms.
  - On the live machine a 13–39 ms query plus that Python work, stretched by starvation, held the lock 7.7–12.6 s. That is what the fix removes.
- **Wall-time holds.**
  - Unthrottled: reserving-hold p99 262.7 ms (B1) against 9.8 ms (A9).
  - At 5% duty, the fix's worst wall holds (A7 247 ms, A8 255 ms, and the early fix's A6 2.34 s) carried at most 3.0–4.4 ms of CPU. The holder spent that time waiting for the SIGCONT window and the GIL, not working. The early fix's A4 hold of 1.24 s ran before CPU was recorded. The baseline's worst were 274 ms to 2.87 s.
- **Turns behind lower-tier work (B5 against A10).** This is the live shape. Turn queued→reserved went from p50 3.22 s, p90 7.89 s, max 13.48 s to p50 0.43 s, p90 2.13 s, max 2.58 s. The baseline evaluated no route under the lock in this run, so this is the turn pass alone.
- **Turns at 5% duty** (B2, B7, B8 against A7, A8): p50 4.4–4.8 s against 3.0–3.9 s; max 10.9–15.6 s against 7.8–8.2 s. At 5% of a core, a turn's own admission work (its workspace git call, one evaluation, the reservation) takes seconds. Waiting for detached work is what the fix removes, and that is only part of it.
- **When nothing moved.** Where the old generation check passed, the fix's in-lock check costs about 1 ms more CPU per reservation: p90 1.5 ms (B5) against 2.5 ms (A10). That is the price of the few reads it makes.
- **Hook reads are noisy between runs.**
  - Two early-fix runs had worse `list` tails at 5% duty: A2 18.5 s and A6 14.4 s; A6 also had 20 client connection errors. A6 ran at the day's highest load (58), so this cannot be pinned on the change.
  - That early code's idle turn pass cost 4 statements and 0.6 ms (the review of this change measured one unthrottled) on every 50 ms tick, about a quarter of a 5% core. It is now one indexed statement.
  - The fix's runs since then had `list` p99 of 5.3 s and 6.2 s, against 6.3 s and 10.2 s for the baselines run beside them, with no client errors.
- **The rig is smaller than the live incident.** It reproduced holds up to 2.9 s and turn waits up to 15.6 s, not 7.7–12.6 s and 725 s. The live daemon had more lanes and readings, Claude reserve logic, probes and real git, at load 110–170.

Commands (from the worktree; the baseline is `git archive e053b2c` unpacked in `/tmp/sfr-base-e053b2c`):

```
uv run python tools/store_contention_repro.py --code <tree> --backlog 48 --backlog-s 30 --turns-every 10 \
    --warmup 20 --duration 120 [--duty 0.05] --report <run>.json                                   # B1, A1, A9, B2, A2
uv run python tools/store_contention_repro.py --code <tree> --backlog 48 --backlog-s 30 --turns-every 10 \
    --warmup 20 --duration 150 --duty 0.05 --report <run>.json                                     # B7, A7, B8, A8 (interleaved)
uv run python tools/store_contention_repro.py --code <tree> --backlog 48 --backlog-s 30 --turns-every 10 \
    --prepare-s 2 --lanes 20 --warmup 20 --duration 150 [--duty 0.05] [--backlog-tier trivial] --report <run>.json
```

## Tests

New tests. Each one that tests changed behaviour fails on `e053b2c`: the files were copied onto `git archive e053b2c` and run.

**`tests/unit/test_scheduler_split.py`**
- The split `evaluate` against the old one, verbatim, over 1,500 random stores, jobs and policies. It passes on `e053b2c` as it must, since it asserts that nothing changed.
- `evaluate` reads no lane field outside `LANE_FACTS`. It fails on `e053b2c`, which has no `LANE_FACTS`.

**`tests/unit/test_route_check.py`** (cannot import on `e053b2c`, which has no `route_check`)
- Two Hypothesis properties, 1,500 examples each: the check refuses exactly when the decision now takes lanes it never judged, and otherwise returns exactly `evaluate`'s decision (lane, model, verdict, details, evidence).
  - One draws random commits.
  - The other aims its commits at the lanes the decision walked.
- With nothing committed, the decision stands.
- Eleven named cases:
  - a commit that touches no lane;
  - a fresher reading on a lane that still ranks below;
  - another lane put first;
  - a closure;
  - a full fleet;
  - an unmeasured lane's second slot;
  - an attempt ending;
  - a turn's affinity;
  - a probe;
  - a disabled lane;
  - a chosen model that loses every lane.

**`tests/fake/test_admission_latency.py`** (real `Daemon`)

These fail on `e053b2c`:
- no capacity view read or built, and no route evaluated, with the store lock held, across 100 reservations while other threads commit;
- an oracle written apart from `scheduler` finds no cap exceeded and nothing double-booked, over random fleets, jobs, turns and commits between each early evaluation and its reservation;
- at every check, a full evaluation made there agrees with it;
- `route-moved` after `ROUTE_TRIES`, keeping its place;
- a lane enrolled under a reset-credit override is not reserved as measured;
- a row the check cannot read settles only its own job;
- a check that raises on every try settles its job with the error named;
- `route-moved` refreshes `recheck`;
- a clock stepping back is evaluated again;
- a turn queued behind 30 `trivial` jobs is evaluated and placed first;
- a turn is placed through the real control loop while a detached pass is held up;
- a placing detached pass never reads as idle;
- an idle turn pass is one statement.

This one passes on `e053b2c`: a turn half that raises still lets the detached half run. It guards a regression the split would otherwise have introduced, which the old single pass did not have.

**`tests/fake/test_admission_route_isolation.py`**
- The C-6.3 and C-6.12 tests of the old mechanism are rewritten for this one, and fail on `e053b2c`. Their behavioural assertions stay:
  - what is reserved is what an evaluation of the rows at the reservation chooses;
  - a route error settles the job and rolls back;
  - no second attempt lands on an unmeasured lane.
- Their counters and the removed `ROUTE_REUSE_S` are the parts that changed.

**Fixture changes.** `tests/unit/test_gate_admission.py` and `tests/unit/test_legacy_hold.py` build bare daemons whose `_pick` returns a fixed decision; they stub `_route_stands` to match. `tests/fake/test_admission_visibility.py` renders `route-moved`, and its failing-pass stub accepts the pass's `kind`.

**Full suite** (`uv run pytest -q --ignore=tests/live`, in nine parallel chunks):
- At 9dd4f35: 6,161 passed, 7 skipped, 2 failed.
  - `test_c6_11_a_detached_pass_that_is_placing_never_reads_as_idle`, fixed in c33b3f6.
  - `tests/frontend/test_core_live.py::test_the_app_core_drives_a_development_daemon`, which fails the same way on `e053b2c`. This session runs inside a Subfleet attempt, and the daemon refuses person-only ops from a caller carrying attempt markers (C-25.6).
- At c33b3f6: 6,163 passed, 7 skipped, 1 failed. The failure is the same app-core live test, for the same reason ("the caller carries Subfleet's attempt markers").

## Not settled

- **Not yet shown on the live daemon.** The live effect is untested until the daemon is installed. Watch `daemon.status` `admission.route_evaluations` (`again`, `deferred` and `error` should stay small), the C-3.6 hold lines in `daemon.log`, and turn queue times.
- **Probe status after a commit.** `Timers` updates its in-memory probe status just after the commit that records a probe. A check made between the two reads the old status, exactly as the in-transaction evaluation did.
- **Hand edits** of a running daemon's store are not seen by the check, as they were not by the generation. A closure given an empty `released_at` while the daemon was stopped is handled, by id.
- **Turn-only store faults.** A store fault only turns meet also paces the `admission` worker (C-5.10), since `_admit` raises the turn half's error after the detached half; C-6.12's existing test pins that the error is the pass's. `admission:turns` is paced on its own.
- **`_note_admission`'s view build** can run on the turn worker, once per ten minutes of idleness, and delays that worker's next pass by one build.
- **`scheduler.ordered_jobs` is unchanged.** Turns come first because they have their own pass, not because of sorting.

## Raw evidence

The runs listed above are in `docs/reports/2026-09-26-admission-latency/`, one `--report` JSON per run, named as in the tables.
