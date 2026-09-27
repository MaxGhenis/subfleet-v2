# Admission latency on 2026-09-26: a turn queued 12 minutes

Every number below comes from a command run on 2026-09-26, and each run is reproducible with `tools/store_contention_repro.py`. The raw reports are under `docs/reports/2026-09-26-admission-latency/`.

The baseline is `e053b2c`, the desktop release line installed as 2.1.6 at 13:25Z that day, exported with `git archive`. "Fix" is this change.

Revised on 2026-09-27. An independent review of `d04b8b3` found two ways the change could hold a job back forever where `e053b2c` placed it at once; a second review, of the fixes, found two more that were bounded but real. "Review of the change" below says what each was and how it was fixed. The design sections describe the code as of `31c931f`. The measurements were run again that day, beside the baseline and `d04b8b3`, and are tabled next to the 2026-09-26 runs.

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
  - each lane's own horizon (`capacity.lane_horizons`): the first instant at which the clock alone could change how that lane is judged (a reading it counts fresh ages out or reaches its reset, one observed after the view's clock turns fresh, a closure ends);
  - the desktop identity.
- **The check inside `attempt.reserved`.** `Daemon._route_stands` reads a few rows (`_route_rows`):
  - every lane row, marked and merged as a view does (`capacity.mark_desktop`, `Timers.merge_lane`, `capacity.credential_latched`);
  - the attempts in flight (`attempts_live`), with their job's kind and parent;
  - the probe leases;
  - the readings added since the snapshot, by id;
  - the closures not released (`closures_active`);
  - the lanes a confirmed reset-credit override covers now, decided for the lane rows now;
  - the parents of the jobs a parent cap counts.
- **`route_check.still_stands`.** It looks only at the lanes of the models the decision walks (`scheduler.model_lanes`: each model's provider's, or the one lane the job's pin names).
  - A lane whose facts (the `scheduler.LANE_FACTS` fields, readings, open closures, attempts in flight in the job's pool, slot block, override) are unchanged, and whose own horizon has not passed, is judged as it was.
  - Any other lane is judged again, alone, in memory, at the check's clock (`scheduler.judge_lane`), and ranked against the candidates that did not change (`scheduler.rank_key`).
  - A fleet or parent cap that began or ended refuses or frees every lane alike, so every lane of the walk is judged again then. When the chosen model has no candidate now and the chain goes on, the next models' lanes are judged, as `evaluate` walks them.
  - A clock or a row on any other lane is never read: it is in no decision this job could get.
- **What it returns** is the decision an evaluation of those rows at this clock would make, exactly and in `evaluate`'s order (`route_check.as_now`): every changed lane's verdict, detail and evidence, every carried reading's age and label at the check's clock, and `evaluated_at` that clock's second. So a probe need is read from now's detail, a wait recorded on it is clocked by now's verdict (C-6.10), and the decision row is what `evaluate` would have written.
- **It refuses** only what it cannot rebuild from these rows, and the transaction rolls back before it has written anything. The route is then evaluated again, off the lock, and checked again, up to `ROUTE_TRIES` (3) times in a pass; after that the job keeps its C-6.9 place as `route-moved`. It refuses when:
  - the job's pin names another lane now (a re-enrolment moved the binding it follows);
  - an evaluation now would raise;
  - the policy was reloaded;
  - a snapshot reading was deleted;
  - a row cannot be parsed;
  - the clock is earlier than the instant the view was built at.
- **`ROUTE_REUSE_S` is gone.** Age alone voids nothing.
- **`scheduler.evaluate` is split** into `prepare`, `judge_lane`, `rank_key`, `model_reason` and `decision_reason`, so one lane can be judged alone. Its behaviour is unchanged: a differential test against the old function, kept verbatim in `tests/reference_scheduler.py`, pins this.

### C-26.9: turns have an admission pass of their own

- **Two passes.** `_admit` runs a turn pass, then a detached pass. The control loop also runs the turn pass alone, on its own worker (`admission:turns`), beside the detached pass.
  - A turn submitted while a detached pass is busy (evaluating, preparing a workspace, waiting on a probe) is placed by the next turn pass.
  - With no turn queued and none held, the turn pass is one indexed statement.
- **Separate state.** Each pass keeps its own holds and freed-lease record.
- **Separate slot leases.** A turn takes the lowest free `lane:<id>:slot:turn-<n>`, a detached attempt `lane:<id>:slot:<n>` (`Daemon._slot_lease`). A turn never holds `slot:0`, the lease a detached job's admission probe takes. A probe may run beside a live turn on its lane, as it already could whenever the turn held another slot; while it runs it keeps every job off the lane, a turn too.
- **A turn a probe held is looked at when the probe ends.** The turn pass counts admission probes' leases (not timer probes') among those whose release frees capacity (C-6.10), and adds the probes a held turn's decision names, so a probe that began during the pass counts too.
- **Rules that hold across both passes:**
  - placements are noted for C-6.11 as each is made;
  - the reservation checks inside its transaction that the job has no attempt in flight;
  - a turn half that raises does not stop the detached half, and its error is raised after it (C-6.12).

## Review of the change, 2026-09-27

An independent review of `d04b8b3` ran a 60,000-case differential against a full evaluation and found no unsafe reservation: no mismatch in lane, detail, verdict or probe requirement. It did find two liveness regressions, one evidence mismatch, and a flaky test. A second review, of the fixes, found two more ways to hold a job back and one ordering mismatch. Every fix below has a test that fails on the code before it and passes after.

**1. Another lane's clock refused the check (review of d04b8b3, P1).**
- *What happened.* The check refused any decision whose view's fleet-wide horizon had passed. Sixty Claude lanes with readings staggered two seconds apart, refreshed on a 120 s cycle, kept that horizon one to three seconds away. A Codex job pinned to codex-1, three seconds from evaluation to reservation, was refused on every try of thirty passes (90 checks) and never placed. Every full evaluation chose codex-1.
- *Fix (42289b9).* Each lane has its own horizon (`capacity.lane_horizons`). A lane whose horizon has passed is judged again at the check's clock, like a lane whose rows changed. Only the lanes the decision looks at are judged, so a Claude lane's clock is never read for a Codex job, nor another lane's for a pinned job. Overrides that begin or end are handled per lane the same way.
- *Tests.*
  - `test_c6_3_readings_ageing_out_on_sixty_unrelated_lanes_never_hold_a_job_back` (the reviewer's schedule). On d04b8b3: no reservation in 30 passes. Now: placed on the first pass, and no lane judged again.
  - The route-check properties no longer skip a passed horizon: about a third of their cases cross a lane's clock.
  - `test_c6_3_lanes_a_decision_never_looks_at_cannot_change_it`. Readings, closures, overrides and clocks on lanes the decision does not look at change neither `evaluate`'s decision nor the check's answer.
  - A per-lane horizon property in `test_decision_horizon.py`.

**2. Turns took the lease a detached probe needs (review of d04b8b3, P1).**
- *What happened.* The turn pass runs first, and a turn took the lowest free `lane:<id>:slot:<n>`: on an idle lane, `slot:0`, the one lease a detached job's admission probe takes. With the next turn queued before the last ended, an older `easy` writable job on that unmeasured lane stayed `probe-pending` through ten turns with no probe. `e053b2c`'s one pass put the `easy` job first and placed both. It did the same to a `standard` job, though: a turn went before a detached job of its own tier.
- *Fix (5d14f98).* Turns number their own slots (`slot:turn-<n>`).
- *Tests.*
  - `test_c26_9_a_stream_of_turns_never_keeps_an_older_writable_job_from_its_probe` (the reviewer's ten waves). On d04b8b3: never probed. Now: probed once and placed on the first pass.
  - The same with a `standard` job.

**3. Reused evidence kept its old label (review of d04b8b3, P3).**
- *What happened.* A `provider` reading already past its reset, 119 s old at the evaluation, was still labelled `provider` in the reserved decision two seconds later, where an evaluation labels it `stale-provider`. Only the recorded evidence differed.
- *Fix (42289b9).* The check ages and labels every reading it carries at its own clock, and sets `evaluated_at` to that second. The decision it reserves on is `evaluate`'s exactly.
- *Tests.*
  - `test_c6_3_a_reservation_records_the_evidence_an_evaluation_there_would`. On d04b8b3 it records `provider`.
  - Every differential now compares the whole decision (`routing_strategies.exact`), the daemon-level one with the clock held for the check and its oracle.

**4. A cap that flipped refused the check (review of 5d14f98, P3).**
- *What happened.* The check still refused when a fleet or parent cap began or ended between evaluation and reservation. A timer probe's lease counts toward the detached fleet's cap. If that lease is held at each evaluation and gone at each check, every try is refused: the reviewer's resonant schedule never placed the job in 900 s, where `e053b2c` placed it. With the flip period jittered, placement was 3.4 times slower.
- *Fix (31c931f).* When the capacity blocks differ, the check judges every lane of the walk again. When the chosen model has no candidate now, it walks on to the next models and judges their lanes, building those models' evaluations as `evaluate` does. It now refuses only for the causes listed above, a pin naming another lane chief among them.
- *Tests.*
  - `test_c6_3_a_fleet_cap_that_flips_between_evaluation_and_check_never_holds_a_job_back`, against `e053b2c`'s semantics too. On 09f82e1: never placed.
  - Named unit cases.

**5. A probe that began mid-pass hid its end from the turn it held (review of 5d14f98, P3).**
- *What happened.* 5d14f98 counted only the probe leases the turn pass saw at its start. A turn held by a probe that began later waited out its backed-off clock after the probe ended.
- *Fix (31c931f).* A held turn's decision names the probes that kept it, and their leases count as seen.
- *Test.* `test_c26_9_a_turn_held_by_a_probe_is_looked_at_when_it_ends_whenever_it_began`. On 09f82e1: not placed.

**6. `candidate_details` order (review of 5d14f98, P3).**
- *What happened.* The check listed unchanged lanes first. The dicts equalled `evaluate`'s, but the recorded JSON differed.
- *Fix (31c931f).* Lanes are listed by id, as `evaluate` does.

**7. A flaky FIFO test.** The reviewer's combined run failed `test_c6_9_a_younger_job_does_not_pass_a_retry_that_let_its_pin_go` once. It read the wall clock, and its one-second retry clock could run out before the second pass. It now holds the clock and checks the reviewer's three cases: before the retry is due, at it, and after it.

**The liveness property.**
- `test_c6_3_c26_9_admission_places_what_e053b2c_placed_pass_for_pass` runs this admission and `e053b2c`'s side by side from one fleet, on one simulated clock.
- `e053b2c`'s admission is built from this code: one pass in `ordered_jobs` order, and the route evaluated again, whole, inside the reservation. It also takes turn slots numbered apart, since with one numbering `e053b2c` itself starved a same-tier writable job.
- The schedules are random: sensor readings on related lanes and on up to 24 unrelated ones, closures ending inside a pass, timer probes starting or ending between an evaluation and its check, turns, detached jobs of every tier (writable ones needing probes), pins, and attempts ending. Reservations come 0 to 3 s after their evaluations.
- After every pass both have placed the same jobs on the same lanes and models, and nothing was evaluated again or deferred.
- It fails on d04b8b3 (given only two behaviour-neutral hooks) both ways the first review found, and on 09f82e1 the way the second did.
- The review's concurrency probe (40 detached jobs, 24 turns, four admission calls at once per wave) is kept as a test too.

## Invariants

These hold for every input and are tested as properties:

- **I1. No evaluation under the lock.** No capacity view is read or built, and `scheduler.evaluate` is never called, while admission holds the store lock. The check reads a few rows by index and judges in memory only the lanes it must: those the decision looks at that changed, or, when a cap began or ended or the chain walks further, every lane of the walk.
- **I2. Every reservation matches a full evaluation made at that moment, exactly.** Its lane, model and recorded decision are what `scheduler.evaluate` would return over the rows the reserving transaction reads, at its clock: verdict, details, evidence (each reading's age and label), `evaluated_at`, all in `evaluate`'s order. So every cap and refusal `evaluate` enforces holds:
  - per-lane slots, measured or unmeasured;
  - the fleet cap, counting reserved probes;
  - the turn pool;
  - parent caps;
  - closures, exclusions, the desktop, identity mismatches, probe-held and latched credentials, the floor, the reserve.
- **I3. No double-booking.** A slot lease is chosen inside the reserving transaction, which the store lock serializes. No two live attempts share one, and a job never has two live attempts.
- **I4. A refused check writes nothing.**
- **I5. Bounded work per job.** A job's route is evaluated at most `ROUTE_TRIES` times per pass. A deferred job keeps its place and is looked at again on the next pass. Only a check that cannot rebuild what the decision rests on sends it back; neither a clock nor a cap that began or ended does.
- **I6. Turns never wait for detached work.** A turn is only ever in a turn pass, one at a time, so it never waits for a detached job's evaluation or workspace. It holds no lease a detached job's probe needs. A probe that holds its lane holds it off only while it runs, and the turn is looked at again when the probe ends.
- **I7. The split changed nothing.** The split `evaluate` equals the old one on every input, error for error.
- **I8. Nothing placed later than before.** Over random schedules without a mid-check change the check cannot decide, every job `e053b2c`'s admission places within N passes this one places within N passes, on the same lane and model, for every N. That holds with turn slots numbered apart in both, since `e053b2c` starved a same-tier writable job behind turns.

Why admission cannot double-book or exceed a cap: the check runs inside the transaction, and no other commit can land until it ends. By I2, it goes on only with the decision the old code's in-transaction evaluation would have made at that instant. The slot search (now per pool) and the lease and attempt checks are inside the same transaction. Caps are counted from attempts, not from slot numbers, so numbering turn slots apart moves no cap. So the old code's safety argument carries over as it stands, and the check adds only the cheap reads and in-memory judgements.

## Measurements

All runs used `tools/store_contention_repro.py` on this 18-core machine, in the same rig and with the same columns, described after the 2026-09-27 tables. The session mirror was off in every run: the tool sets `sessions.mirror_interval_s` to 0 and gives each run an empty session store of its own.

### 2026-09-27: after both reviews' fixes (31c931f)

Every run used the same rig as on 2026-09-26. The code under test:
- "Fix" (F1–F7) is `31c931f`, exported with `git archive`.
- The baseline (B1–B5) is `e053b2c`.
- "d04b8b3" (D2, D5) is the code the reviews found the liveness regressions in.

Runs were interleaved (a baseline, then the fix) so that each pair saw the same load. The machine's own load average ran from 32 to 93 during them, from other sessions: higher than on 2026-09-26. It is logged at each run's start and end in `2026-09-27/runner.log`.

#### Unthrottled: 6 running, 21 hook sessions, 48-job backlog, a turn every 10 s (120 s)

| run | reservations that evaluated inside the lock | reserving hold p50 / p99 / max | CPU under the lock p90 / max | turns: n, queued→reserved p50 / p90 / max | turns never reserved | hook `list` p99 | client errors |
|---|---|---|---|---|---|---|---|
| baseline (B1) | 22 of 63 | 1.1 ms / 322.1 ms / 322.1 ms | 24.5 ms / 25.4 ms | 11: 0.99 s / 1.70 s / 3.35 s | 0 | 0.94 s | 0 |
| fix (F1) | 0 of 67 | 1.6 ms / 218.5 ms / 218.5 ms | 1.9 ms / 2.2 ms | 11: 0.43 s / 0.71 s / 0.87 s | 1 | 0.96 s | 4 |

#### Held to 5% of a core (`--duty 0.05`), same load (150 s)

| run | reservations that evaluated inside the lock | reserving hold p50 / p99 / max | CPU under the lock p90 / max | turns: n, queued→reserved p50 / p90 / max | turns never reserved | hook `list` p99 | client errors |
|---|---|---|---|---|---|---|---|
| baseline (B2) | 17 of 59 | 1.9 ms / 820.5 ms / 820.5 ms | 25.3 ms / 30.3 ms | 13: 3.81 s / 8.93 s / 10.72 s | 1 | 4.53 s | 1 |
| fix (F2) | 0 of 65 | 1.8 ms / 283.8 ms / 283.8 ms | 2.1 ms / 6.6 ms | 13: 2.00 s / 5.19 s / 6.23 s | 0 | 5.89 s | 1 |
| d04b8b3 (D2) | 0 of 62 | 2.4 ms / 2254.7 ms / 2254.7 ms | 2.0 ms / 4.1 ms | 13: 4.98 s / 7.66 s / 10.10 s | 0 | 4.60 s | 1 |
| fix (F6) | 0 of 63 | 1.7 ms / 246.4 ms / 246.4 ms | 2.0 ms / 3.1 ms | 13: 2.79 s / 5.26 s / 6.12 s | 0 | 4.13 s | 0 |

#### Each detached job's workspace takes 2 s to prepare, 20 lanes (`--prepare-s 2 --lanes 20`, 150 s)

| run | reservations that evaluated inside the lock | reserving hold p50 / p99 / max | CPU under the lock p90 / max | turns: n, queued→reserved p50 / p90 / max | turns never reserved | hook `list` p99 | client errors |
|---|---|---|---|---|---|---|---|
| baseline (B3) | 5 of 60 | 1.4 ms / 317.2 ms / 317.2 ms | 3.1 ms / 26.3 ms | 13: 1.71 s / 8.80 s / 9.48 s | 0 | 0.87 s | 0 |
| fix (F3) | 0 of 61 | 2.3 ms / 117.1 ms / 117.1 ms | 2.7 ms / 3.8 ms | 13: 0.44 s / 2.55 s / 7.61 s | 0 | 0.68 s | 0 |

#### The same, held to 5% of a core

| run | reservations that evaluated inside the lock | reserving hold p50 / p99 / max | CPU under the lock p90 / max | turns: n, queued→reserved p50 / p90 / max | turns never reserved | hook `list` p99 | client errors |
|---|---|---|---|---|---|---|---|
| baseline (B4) | 10 of 45 | 1.6 ms / 271.8 ms / 271.8 ms | 26.2 ms / 28.5 ms | 12: 6.20 s / 14.57 s / 19.19 s | 1 | 7.73 s | 10 |
| fix (F4) | 0 of 49 | 2.1 ms / 1608.4 ms / 1608.4 ms | 2.7 ms / 3.2 ms | 13: 2.74 s / 5.03 s / 6.54 s | 0 | 4.89 s | 0 |

#### The 48-job backlog is `trivial` tier, preparation 2 s, 20 lanes: the live shape, turns behind lower-tier work

| run | reservations that evaluated inside the lock | reserving hold p50 / p99 / max | CPU under the lock p90 / max | turns: n, queued→reserved p50 / p90 / max | turns never reserved | hook `list` p99 | client errors |
|---|---|---|---|---|---|---|---|
| baseline (B5) | 9 of 74 | 1.8 ms / 357.5 ms / 357.5 ms | 24.0 ms / 28.3 ms | 14: 6.91 s / 14.12 s / 17.78 s | 0 | 0.80 s | 0 |
| fix (F5) | 0 of 73 | 2.4 ms / 242.1 ms / 242.1 ms | 2.9 ms / 3.7 ms | 14: 1.00 s / 1.41 s / 1.47 s | 0 | 0.93 s | 2 |
| d04b8b3 (D5) | 0 of 76 | 1.8 ms / 4.5 ms / 4.5 ms | 2.3 ms / 3.1 ms | 14: 0.28 s / 0.96 s / 1.20 s | 0 | 0.72 s | 0 |
| fix (F7) | 0 of 80 | 1.9 ms / 3.3 ms / 3.3 ms | 2.4 ms / 2.7 ms | 14: 0.44 s / 0.76 s / 0.82 s | 0 | 0.52 s | 0 |

What the 2026-09-27 runs show:

- **Still no evaluation under the lock.**
  - No fix run evaluated a route inside a reservation, and none evaluated a route again at all (`again` 0 in every F run). The fix judged the lanes it had to in memory instead (`rejudged` 0 to 20 per run).
  - The baseline evaluated inside the lock in 5 to 22 reservations per run, up to 35% of them (B1: 22 of 63).
  - d04b8b3 refused two checks in D2 (`moved`) and evaluated those routes again, off the lock.
- **Work under the lock** (CPU; p90 is the second figure in each row's CPU column):
  - Fix: p90 1.9–2.9 ms, max 2.2–6.6 ms.
  - d04b8b3: p90 2.0–2.3 ms, max 3.1–4.1 ms.
  - Baseline: p90 3.1–26.2 ms, max 25.4–30.3 ms.

  Judging expired lanes, carrying evidence at the check's clock, and judging every lane of the walk when a cap changes added no measurable CPU over d04b8b3. At p90, run by run:
  - F6 against D2 (5% duty): 2.0 ms against 2.0 ms;
  - F7 against D5 (trivial backlog): 2.4 ms against 2.3 ms.
- **Wall-time holds.**
  - The fix's worst holds at 5% duty were 1.6 s in F4 and 284 ms in F2. No hold in those runs used more than 3.2 ms and 6.6 ms of CPU respectively.
  - d04b8b3's worst was 2.25 s in D2, with at most 4.1 ms.
  - As on 2026-09-26, the holder spent that time waiting for its run window and the GIL, not working.
  - The baseline's worst was 820 ms in B2, with at most 30.3 ms.
- **Turns behind lower-tier work (B5 against F5, F7).** Turn queued→reserved:

  | run | p50 | p90 | max |
  |---|---|---|---|
  | baseline (B5) | 6.91 s | 14.12 s | 17.78 s |
  | fix (F5) | 1.00 s | 1.41 s | 1.47 s |
  | fix (F7) | 0.44 s | 0.76 s | 0.82 s |
  | d04b8b3 (D5) | 0.28 s | 0.96 s | 1.20 s |

  - F5 ran as the load rose from 62 to 93. F7, run right after D5 at the same load, matched d04b8b3.
  - Every turn in both fix runs was reserved.
- **Turns at 5% duty** (B2 against F2, F6):
  - Baseline: p50 3.81 s, max 10.72 s.
  - Fix: p50 2.00–2.79 s, max 6.12–6.23 s.
  - d04b8b3, beside F6: p50 4.98 s, max 10.10 s.
- **Turns never reserved.** One in each of F1, B2 and B4. Each was the last turn the rig queued, in the run's final 10 s, still queued when measurement ended (from the run's `admission.json`). None in the other runs.
- **Client errors.**
  - The tool counts every request a hook emulator or probe could not complete over the whole run, the daemon's shutdown included.
  - The fix runs had 0 to 4, the baselines 0 to 10, d04b8b3 0 to 1.
  - All were connections closed without a response or refused (`ConnectionError`, `ConnectionRefusedError`). No run had a turn submission error.

### 2026-09-26: the first fix builds

The machine's own load average ran from 21 to 86 during these runs, from other sessions. It is logged at each run's start and end in `runner-1.log` to `runner-5.log` beside the reports.

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
- Two Hypothesis properties, 1,500 examples each, over random commits (overrides that begin and end included) and random delays. The check refuses exactly when the job's pin names another lane now or an evaluation now raises. Otherwise it returns exactly `evaluate`'s decision at its clock, compared whole and in order (`routing_strategies.exact`): lane, model, verdict, details, evidence with each reading's age and label, and `evaluated_at`. About a third of the cases cross a lane's own clock.
  - One draws random commits.
  - The other aims its commits at the lanes the decision walked.
- With nothing committed, the decision stands.
- A metamorphic property, 500 examples: readings, closures, overrides and clocks on the lanes a decision does not look at change neither `evaluate`'s decision nor the check's answer.
- Named cases:
  - a commit that touches no lane;
  - a fresher reading on a lane that still ranks below;
  - another lane put first;
  - a closure;
  - a full fleet: no lane now, `fleet-full`;
  - an unmeasured lane's second slot;
  - an attempt ending;
  - a turn's affinity;
  - a probe;
  - a disabled lane;
  - a chosen model that loses every lane: the walk goes on to Astra;
  - a fleet cap that begins or ends: every lane judged again;
  - a pin that names another lane after a re-enrolment: refused;
  - readings ageing out on another provider's lanes: nothing judged again;
  - readings ageing out on a lane the pin does not name: nothing judged again;
  - the chosen lane's reading ageing out: judged again, another lane chosen;
  - a closure ending on a lane it looks at;
  - an override ending, on a lane it looks at and on one it does not;
  - a reading past its reset, labelled as `evaluate` labels it (P3).

**`tests/unit/test_decision_horizon.py`** gains a per-lane property: each lane is judged the same, for every model of its provider, at every instant before its own horizon, whatever the other lanes' clocks do. The fleet-wide horizon is the earliest lane's.

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

**`tests/fake/test_admission_liveness.py`** (2026-09-27; each named test fails on the code the review found the defect in, run by copying the file onto an export of that commit):
- sixty unrelated Claude lanes' readings ageing out never hold a Codex job back (fails on d04b8b3: never placed in 30 passes);
- a reservation records the evidence an evaluation there would (fails on d04b8b3: `provider`);
- a stream of turns never keeps an older writable job from its probe, `easy` and `standard` tiers (both fail on d04b8b3: never probed);
- a turn held by a probe is looked at when the probe ends, whether the probe began before the turn pass or during it (fails on d04b8b3; the second also on 09f82e1);
- a fleet cap that flips between evaluation and check never holds a job back, both ways round, beside `e053b2c`'s semantics (fails on 09f82e1: never placed);
- the review's concurrency probe: both passes racing keep every cap and each kind's FIFO, and place everything;
- the liveness property above.

**Mechanism assertions rewritten on 2026-09-27.** Tests that asserted the old refusal counters now assert the new ones. Their behavioural assertions stay:
- no second attempt on an unmeasured lane;
- nothing below the floor;
- a probe before a lane newly under an override;
- a full fleet waits `fleet-full`;
- rollback before a route error settles the job;
- `route-moved` keeps its place and holds the younger job back.

Four of these made the check refuse by filling the fleet, which no longer refuses. They now refuse through a pin that moves (codex-1 or codex-2 re-enrolled onto codex-9 between evaluation and check).

`test_c6_9_a_younger_job_does_not_pass_a_retry_that_let_its_pin_go` holds the clock and checks all three of the review's cases.

**Fixture changes.** `tests/unit/test_gate_admission.py` and `tests/unit/test_legacy_hold.py` build bare daemons whose `_pick` returns a fixed decision; they stub `_route_stands` to match. `tests/fake/test_admission_visibility.py` renders `route-moved`, and its failing-pass stub accepts the pass's `kind`.

**Full suite** (`uv run pytest -q --ignore=tests/live`, in nine parallel chunks):
- At 9dd4f35: 6,161 passed, 7 skipped, 2 failed.
  - `test_c6_11_a_detached_pass_that_is_placing_never_reads_as_idle`, fixed in c33b3f6.
  - `tests/frontend/test_core_live.py::test_the_app_core_drives_a_development_daemon`, which fails the same way on `e053b2c`. This session runs inside a Subfleet attempt, and the daemon refuses person-only ops from a caller carrying attempt markers (C-25.6).
- At c33b3f6: 6,163 passed, 7 skipped, 1 failed. The failure is the same app-core live test, for the same reason ("the caller carries Subfleet's attempt markers").
- At 26cb411 (the code of 31c931f and the contract), 2026-09-27: 6,194 passed, 0 skipped, 0 failed, in nine parallel chunks at load 30 to 70.
  - It ran as CI runs it: `SUBFLEET_HOME` and `SUBFLEET_ROOT` pointed at a scratch directory, and the attempt markers (`SUBFLEET_JOB`, `SUBFLEET_ATTEMPT`) unset.
  - So the app-core live test ran, and passed, and nothing skipped for want of a caller outside an attempt.

## Not settled

- **Not yet shown on the live daemon.** The live effect is untested until the daemon is installed. Watch `daemon.status` `admission.route_evaluations` (`again`, `deferred` and `error` should stay small), the C-3.6 hold lines in `daemon.log`, and turn queue times.
- **Probe status after a commit.** `Timers` updates its in-memory probe status just after the commit that records a probe. A check made between the two reads the old status, exactly as the in-transaction evaluation did.
- **Hand edits** of a running daemon's store are not seen by the check, as they were not by the generation. A closure given an empty `released_at` while the daemon was stopped is handled, by id.
- **Turn-only store faults.** A store fault only turns meet also paces the `admission` worker (C-5.10), since `_admit` raises the turn half's error after the detached half; C-6.12's existing test pins that the error is the pass's. `admission:turns` is paced on its own.
- **`_note_admission`'s view build** can run on the turn worker, once per ten minutes of idleness, and delays that worker's next pass by one build.
- **`scheduler.ordered_jobs` is unchanged.** Turns come first because they have their own pass, not because of sorting.
- **A probe beside a live turn.** Since turns hold no `slot:0`, a detached job's admission probe can now run on a lane while a turn runs there. It already could whenever the turn held another slot. Timer probes still wait for an idle lane.
- **What still defers a job.** A check refuses only what it cannot rebuild:
  - a pin that names another lane now;
  - an evaluation that would raise;
  - a policy loaded since;
  - a clock that stepped back;
  - a snapshot reading deleted.

  Each is a one-off event (a re-enrolment, hourly retention), not something that recurs every few seconds. Three refusals in a pass leave the job `route-moved` for the next pass.
- **Work under the lock when a cap changes.** Every lane of the walk is then judged in memory under the lock: at most the lanes of the job's chain, once each. The measured CPU under the lock stayed at or under 6.6 ms.
- **What the liveness property compares against.** CI checks out one commit, so the old admission is rebuilt from this code with `e053b2c`'s semantics, not imported from `e053b2c`. It numbers turn slots apart, as this code does. Its schedules include no change a check cannot decide (a pin that moves), which defers a job by design.
- **The review's reverse-direction note stands.** A turn half run inline by `_admit` delays that call's detached half by the turn pass's work. The single pass of `e053b2c` did the same work in one pass.

## Raw evidence

The runs listed above are in `docs/reports/2026-09-26-admission-latency/`, one `--report` JSON per run, named as in the tables. The 2026-09-27 runs are in its `2026-09-27/` folder, with `runner.log`.
