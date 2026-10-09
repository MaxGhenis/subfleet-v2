# A job that carried admission probes was passed by later jobs (defect D-1)

2026-10-08. The fix is in the same commit as this report; the clauses it changes are C-6.3, C-6.9, C-6.10, C-6.11, C-6.14 and C-11.4.

## What happened

Job `20261003-143627-handoff-rule-review-r1` (task `review`, tier `hard`, read-only, no pin) was submitted at 18:36:27Z on 2026-10-03, sat `waiting` on `capacity` and was cancelled by its caller at 19:40:07Z without starting. In that time 31 `hard` jobs submitted after it had their first attempt reserved, the first at 18:45:33Z, all on `gpt-6.1-sol` on Codex lanes.

Its events in the store (read-only, `~/.subfleet/state.sqlite3`, table `events`, `job_id` = the job):

| Probe reserved | Lane | Probe ended | Ran | Class | Admission evidence |
|---|---|---|---|---|---|
| 18:43:28Z | codex-3 | 18:44:28Z | 60 s | `unknown` | `no successful deliverable`, rc 0 |
| 18:54:08Z | codex-5 | 18:55:08Z | 60 s | `transient` | `failed to refresh available models: request timed out`, rc 0 |
| 19:16:51Z | codex-2 | 19:17:51Z | 60 s | `transient` | the same line, rc 0 |
| 19:35:17Z | codex-2 | 19:36:17Z | 60 s | `unknown` | `no successful deliverable`, rc 0 |

After each, `job.probe_waiting` put it back on a 60 s clock. A second job, `20261003-151026-ukdata-512-review-r2` (submitted 19:10:26Z), drew two `transient` probes on codex-2 the same way, 60 s and 59 s.

## Why the probes said nothing

Every one of the job's four probes ran exactly to its deadline. Its `probe.state` records go `reserved`, `starting`, then `containing` at the second of `deadline_at`, then `contained`, then `completed`. `containing` is written by `Daemon._contain_probe` only when the census still finds the probe's processes alive, so the daemon stopped each probe at its deadline: `_prepare_route` sets `deadline_at` to the reservation plus 60 s, and `_await_probe` stops waiting there. A probe that ends by itself goes from `starting` straight to `contained`.

The receipt each probe left records rc 0 and no signal. Nothing recorded that the daemon had stopped it, so its class came from `CodexAdapter.classify` reading a stream cut short. With no deliverable and rc 0 it returns `unknown`, `no successful deliverable`, unless a stderr line matches `TRANSIENT_RE` (`timed? ?out` among others), in which case it returns `transient`.

`failed to refresh available models: request timed out` is not a failure of the turn. Of the first attempts of Codex jobs submitted 18:20Z to 19:40Z that left a stderr file, 60 of 64 printed it, and 53 of those 60 succeeded. It only decides which label a probe its deadline stopped gets.

Across the window (probes completed 18:00Z to 19:40Z): 63 `ok`, 17 `transient`, 8 `unknown`, all on `gpt-6.1-sol` (ultra effort) but for 9 `ok` Claude probes. The daemon stopped 16 of the 17 `transient` and 7 of the 8 `unknown` at the deadline, and 12 of the 63 `ok` too, which had written their answer before it. So the `unknown` verdicts and the "request timed out" `transient` verdicts are one cause: a probe of an ultra-effort model, under that load (the hub's record of the incident notes about 60 jobs running and a Codex queue delay near 45 minutes), running past 60 s.

Probes stopped at the deadline were common on other days too (probe completions by day, class, and how many ran 58 s or longer):

| Day | `unknown` | of which ran ≥58 s | `transient` | of which ran ≥58 s |
|---|---|---|---|---|
| 2026-10-01 | 219 | 204 | 5 | 5 |
| 2026-10-02 | 308 | 260 | 17 | 14 |
| 2026-10-03 | 163 | 133 | 133 | 121 |
| 2026-10-06 | 11 | 10 | 31 | 29 |

## Why the job that carried them went back to waiting while later jobs started

1. **A probe is the lane's, not the job's, but its verdict landed only on the job.** The probe's prompt is fixed (`Reply with exactly OK. Do not use tools.`) and it runs on the lane's credential in a private directory, so it says nothing about the job that carries it. An inconclusive probe records nothing against the lane, so the very next job in the pass found the same lane unmeasured, reserved its own probe on it and, when that one answered `ok`, started. At 18:44:28Z the job's probe on codex-3 ended `unknown`; at 18:44:29Z `20261003-143826-delta-9764`, submitted two minutes after it, reserved a probe on codex-3, which answered, and that job started at 18:45:33Z.
2. **Its next look came a whole pass later.** Probes run inside the detached admission pass, one after another, each up to 60 s. The job's 60 s clock was set from the probe's end, and the pass had moved on. Its next look came when a later pass reached it again: 9 m 40 s, 21 m 43 s and 17 m 26 s after each probe ended. In those gaps 11, 24 and 21 other probes ran (8.5, 20.6 and 16.1 minutes of probe time).
3. **Nothing bounded how often this could repeat.** Each look drew one more probe with the same chance of running out of time. Twice in a row it drew codex-2. Nothing held later jobs back, since C-6.9's hold-back applies only in a pool with a count cap, and the shipped policy sets none.

## The fix

- **The turn (C-6.9).** The jobs of a detached pass that wait on an admission probe form a line in the pass's order: `Daemon._probe_line`, with `scheduler.probe_turn` and `scheduler.probe_lanes_taken`. A job carries the probe of a model on a lane only when no job ahead of it in the line waits on a probe of that model and could run on that lane. A job whose chosen lane is another job's turn goes to a lane of the same model whose probe is its own. Failing that, it is held `probe-pending` with `behind` naming the older job, on a capacity clock, and is looked at on the first pass that finds nobody ahead of it. Each pass builds the line again, so a job that started, ended or now waits for anything else holds nobody.
- **The clock (C-6.10).** The wait after an inconclusive probe is `PROBE_RETRY_S` (60 s) from the probe's reservation, not its end. A probe its deadline stopped leaves its job due at once, and that job is first in line on the next pass. A probe that failed in a second still waits out the minute, so the provider is not probed in a loop.
- **Rotation (C-11.4).** After a probe of a model on a lane says nothing, the job's next probe of that model goes to a lane of that model it has not had such an answer on this round (`Daemon._probe_choice`). Once no other lane would take the job, the round starts again. A model the chain promotes to is never taken this way. The reservation's evaluation after a moved route leaves the same lanes out. This is what bounds the turn: a later job held behind an older one that could use any lane sees its own lane probed within one of the older job's rounds. Without rotation, the turn alone let an older job stuck on a slow top-ranked lane hold a later job pinned to a healthy lane for as long as the slow lane stayed slow.
- **Evidence (C-11.4).** A probe the deadline stopped records `stopped: deadline` in its record and in the `probe.completed` evidence. `job.probe_waiting` names the lane, the model, the class and the clock. `why` names the probe and whose turn it is (C-6.11).

## Invariants and how they are checked

`tests/fake/test_probe_turn.py` runs the real admission pass against the fake daemon, with `_execute_probe` scripted. No process starts and nothing waits: a probe's reservation is moved into the past by however long it is meant to have run.

- **I1, the turn.** A job carries a probe of a model on a lane only when no job ahead of it in the pass's order waits on a probe of that model and could run on that lane.
- **I2, no passing on a probe.** Among jobs that want the same model on the same lanes, starts follow the pass's order whatever the probes answer.
- **I3, the clock.** After an inconclusive probe its job is due `PROBE_RETRY_S` after the probe's reservation, or at once if that has passed.
- **I4, it ends.** A job that no longer waits on a probe holds nobody. A job held for another's turn is looked at on the first pass that finds nobody ahead of it, and once probes answer every job starts.
- **I5, the hold is narrow.** A probe of another model, or of a lane the waiting job cannot use, is nobody's to wait for. A job that needs no probe is never held. A job whose chosen lane is another's turn takes a lane of the same model whose probe is its own.
- **I6, rotation and the bound.** After a probe says nothing, the job's next probe of that model goes to a lane it has not tried this round. While one lane's probes always run out of time, every job that could run on another lane still starts, within two passes a job.

The property test generates jobs (two models, each pinned to either lane or to none), sequences of probe outcomes that say nothing (cut at the deadline, `transient`, `unknown`, a content filter, of any length from 0 to 90 s), kills and clock movements, and optionally a lane whose every probe runs to its deadline. It checks I1 to I6 after every pass. `test_the_turn_is_the_first_waiter_that_could_use_the_probe` checks the pure rule. The deterministic replay of the incident is `test_d1_a_job_whose_probe_said_nothing_is_not_passed_by_the_jobs_behind_it`.

On release/217 (7e8b4a58), the replay and the rotation, bound, clock and younger-job tests fail on their assertions:

- in the replay, the two jobs behind the first carry probes in the same pass;
- a later job starts ahead of the waiter;
- the clock is 60 s from the probe's end;
- the next probe goes to the same lane.

Seven single-point mutations of the fix were each caught by at least one test:

- no turn;
- no rotation;
- no other lane for a younger job;
- the clock from the probe's end;
- a moved route that forgets the rotation;
- no hurry when the turn comes;
- no line entry for a job whose clock runs.

## What this does not change

- **The 60 s probe deadline itself.** Twelve `ok` probes in the window also ran into it, and the deadline sits at the tail of `gpt-6.1-sol` probe durations under that load. Raising it, or ending a probe once its stream shows the model answered (C-6.14's reader), is a separate change.
- **Ranking.** An inconclusive probe still records nothing against its lane. Rotation is per job, so a lane whose probes keep running out of time costs each job that ranks it first one probe a round.
