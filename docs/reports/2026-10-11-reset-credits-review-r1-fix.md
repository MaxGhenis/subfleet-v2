# Reset credits, demand only: fix round 1 for #179 (2026-10-11)

Review: `~/reviews/subfleet-2110/reset-demand-only/review-r1.md` (round 1 at e46cc4815,
REQUEST CHANGES). This round answers each finding. Line numbers are at 0572dfb91. The previous
lane recorded raw output in `~/reviews/subfleet-2110/reset-demand-only/fix1-runs/`.
Finish-run evidence is kept in `~/reviews/subfleet-2110/reset-demand-only/finish-runs/`, outside the
repository.
The finish job starts at 8f0461940; production code is unchanged from 59ab7a8be.

The finish job changed report and contract text only. Its sandbox could not write the shared
Git index, so it committed to workspace-local metadata; the hub carried the result onto the
branch as one commit.

## Commits (on e46cc4815)

| Commit | What |
|---|---|
| 59ab7a8be | `actions.py`: only an answer that spent nothing is `failed`; a call never started is `aborted-before-submission`; the spend is recorded when its consume returns; a held lane is never listed; `capped` is no demand. `daemon.py`: `_reset_demand` reads the running pass's holds |
| 506dfabe3 | Tests: the review's probes as regressions (`tests/unit/test_reset_credit_review_r1.py`), two new properties, the checker fix, a deterministic interval-from-memory mutant, the cascade replay made deterministic, the door-hold window |
| d3aad6ee7 | Tests: the interval property calls `evaluate` with the daemon's clock directly (cheaper) |
| d418540d3 | Contract: C-23.16 (a), (d), (e), C-23.46, C-18.1 |
| fcdc81d82 | This report (results pending) |
| 0572dfb91 | Tests: the door-hold window case under the incident fixture's capped pool; the job-due event matched on its data |

## Each finding

### P2-1: a 2xx that is not a confirmed reset was `failed`, and `failed` blocks nothing

- **Change.** `subfleet/actions.py:29` `NOT_SPENT` names the answers that show no credit was
  spent: `http-error` (a 4xx: the provider refused it), `no-auth` and `inhibited` (nothing was
  sent), and `aborted-before-submission` (the call never started; see P3-5).
  `actions.py:773`: `confirmed` as before (code `reset`, integer `windows_reset > 0`), `failed`
  only for `NOT_SPENT`, and **everything else `unknown`**, so a 2xx of any other shape blocks
  every spend (`_gate`, `actions.py:495-510`) until a usage read reconciles it (C-19.1), and no
  closure is released for it.
- **Regressions** (`tests/unit/test_reset_credit_review_r1.py`):
  - `test_p2_1_an_unrecognised_2xx_blocks_the_next_cycle_on_the_same_lane[...]`: the
    reviewer's four bodies (`{"code":"reset"}`, `windows_reset` `"2"` and `0`,
    `{"status":"reset",...}`); each is `unknown`, and 60 s later the answer is
    `unsettled-action` with one POST.
  - `test_p2_1_an_unrecognised_2xx_blocks_the_job_s_other_lane_too`: one gift per account;
    no POST goes to codex-2 at +1 min, +2 min, +31 min or +3 h.
  - `test_p2_1_control_a_5xx_is_unknown_and_blocks` (the reviewer's control, unchanged).
  - `test_p2_1_an_unrecognised_2xx_is_settled_by_the_lane_reading_open`: the usage read that
    finds the lane open settles it, makes the job due, and (d) then holds the next spend.
  - `test_only_exact_provider_success_confirms` (`tests/unit/test_timers_reset_credits.py`)
    now expects `unknown`, no released closure, no override and no second POST;
    `test_a_refused_consume_is_failed_and_holds_nothing_back` keeps a 4xx `failed` (it spends
    the next gift a minute later); `test_op_key_is_unique...` uses a 409 for its refusal.
- **Property** `test_no_second_consume_while_an_earlier_one_may_have_spent`: generated
  sequences of 14 answer kinds (a confirmed reset, the four bodies, other 2xx shapes, a body
  that is not JSON, a JSON list, 503, a timeout, a dropped connection, 409, 429) over three
  limited lanes with two gifts each, evaluations a minute to a day apart, usage lagging
  throughout. A consume is sent only if every consume before it is `failed`; after any other,
  every evaluation is held back; each answer records the state the table says; no gift is
  sent twice.

  The brief worded it "no second consume while an earlier consume's outcome is anything but
  `failed`-before-send". `failed` here also covers a 4xx, which was sent and refused: the
  brief keeps `http-error` `failed`, and a refusal spent nothing, so the next gift may go.
  Every other `failed` outcome (`no-auth`, `inhibited`, `aborted-before-submission`) is
  before the send.

### P2-2: the property suite failed on correct code

- **Checker.** `tests/unit/test_reset_credit_properties.py:309-314`: a spend is matched against
  every demand entry of its job, not the last one (the reviewer's hunk).
- **The interval-from-memory mutant** (`:522-549`) is now caught in a fixed world, `RESTARTED`
  (`:506-511`): a spend, the reset lane used up 10 minutes later, a restart a minute after that.
  The mutant spends on codex-2 at the restarted step (`spent 0:11:00 after the last`); the same
  world without the restart decides as the code does. `test_the_interval_survives_a_restart_in_a_fixed_world`
  plays it on the code as it is.
- **The other mutant searches** run `derandomize=True` (`:451-455`): the same examples every
  run, so a search catches its mutant, or fails, the same way each time.
- **The reviewer's false-positive world** is kept:
  `test_p2_2_a_job_named_twice_by_lying_demand_is_matched_on_either_entry`.
- **Counts**: see "Runs".

### P3-1: a fleet or parent cap read as Codex demand

- **Change.** `actions.py:292-297` `demand_verdict`: a decision carrying `capacity_blocks` is
  `capped` (and names them) unless a route lane is room; `ResetCredits.evaluate` spends only
  for `codex-demand`, so a capped job is never spent for. C-23.16 (a) now says so.
- **The door-hold window.** `daemon.py:902-906` `_pass_holds`: the holds dict of the pass of
  each kind now running (set at `:4684`, cleared at `:4693`). `_reset_demand` (`:1794-1805`)
  takes a job's latest hold from the running pass first, then the last whole pass's, so a job
  held at the door (disk, machine-busy, or any other hold the pass makes) is no demand from the
  moment of the hold. The running passes are read before the last whole pass's holds, so a
  pass ending between the two reads is seen whole.
- **Regressions**: `test_p3_1_a_full_fleet_with_every_route_lane_limited_is_capped_not_demand`
  (the reviewer's probe; once the fleet has a slot the same route is `codex-demand`),
  `test_p3_1_a_parent_at_its_cap_with_every_route_lane_limited_is_capped`,
  `test_p3_1_a_capped_job_is_never_spent_for` (timer and operator), and
  `tests/fake/test_reset_credit_incident.py::test_a_job_held_at_the_door_is_no_demand_from_that_moment[machine-busy|disk]`:
  each door look calls `_reset_demand`; at the second job's door the first, held there a
  moment earlier in the same pass, is no longer demand.

### P3-2: the interval ran between pass starts

- **Change.** `actions.py:765`: the action is published at `_iso(current())`, read once the
  bounded consume call has returned; the release, the C-23.17 override (`confirmed_at`, `weekly_reset_at`)
  and the job's due time use it. The pending row (`:734`, `:745-746`) and the claim (`:748`)
  record the clock's readings of then too. `_gate` still measures from the pass's start, the
  stricter end. Without a `clock`, time stands at `now` (`:614-616`; it used to add the
  elapsed monotonic time), so the cases that pass no clock record exact stamps; the daemon
  always passes `Timers.now`.
- **Timestamp basis and resolution.** `_gate` reads the latest `updated_at` among confirmed
  or reconciled action rows. Normal publication stamps the bounded call's return with
  `max(pass_start, clock())`, to whole seconds. Recovery changes an executing orphan's state
  only, preserving its claim timestamp; append-only reconciliation does not change it either.
  Thus the interval is over durable action timestamps, not a reconstruction of an ambiguous
  request's actual send time. The completed-consume regression and clock property below
  verify the slow-listing fix. Rounding a normal stamp up would make an evaluation in the
  same second read `interval-blocked` even with a zero interval.
- **Regressions**: `test_p3_2_two_consumes_never_land_closer_than_the_interval` (the reviewer's
  probe through `Timers`: a 50 s listing; the action is stamped 12:00:50; the pass at +30 min
  is `interval-blocked`; the one at +30 min 50 s spends, exactly 30 minutes after the first
  POST), `test_p3_2_a_clock_that_goes_back_after_a_spend_is_inside_the_interval` (the
  reviewer's control), and the property
  `test_consumes_are_never_closer_than_the_interval_on_the_daemon_s_clock`: generated passes
  1 to 45 minutes apart (scheduled from each pass's start, as a timer's are), listings of 0 to
  90 s, reset lanes used up again or not; no two POSTs are under 30 minutes apart.

### P3-3: a held lane was listed as a shadow blocker

- **Change.** `actions.py:856-861`: blockers are filtered through `lane_hold`; `:876-877`:
  holds are read from the store again before each listing, so a hold that lands during the
  pass stops the next listing too. C-23.46 now counts only lanes a credit may go on.
- **Regressions**: `test_p3_3_a_held_lane_is_not_listed_as_a_shadow_blocker` (the reviewer's
  probe), `test_p3_3_a_held_lane_s_gift_does_not_exclude_a_shadowed_candidate`,
  `test_p3_3_a_hold_that_lands_during_the_pass_stops_the_next_listing`.

### P3-5: an `unknown` that never reached the provider blocks every reset

**Fixed in code where the record can show nothing was sent; argued to wait otherwise.**

- **Change.** `actions.py:329-340` `_bounded`: when it never runs the call (the deadline or a
  cancel came first, or a request still running on the lane holds its slot) it answers
  `aborted-before-submission`, which `NOT_SPENT` makes `failed`. That is the reviewer's first
  source ("the deadline or cancel had already passed when `_bounded` ran after the claim") and
  the slot case beside it. No operator is needed: the answer itself shows nothing was sent,
  the same status `recover()` gives a pending intent (`actions.py:431`).
- **Regressions**: `test_p3_5_a_consume_whose_deadline_passed_after_the_claim_is_failed_not_unknown`
  (no POST; the next evaluation spends the next gift),
  `test_p3_5_a_consume_behind_a_request_still_running_on_the_lane_is_failed`, and the control
  `test_p3_5_a_consume_that_started_and_timed_out_stays_unknown`.
- **Why no operator command for the rest.** The brief's suggested rule allows settling only
  when the action's record shows no request was sent. For new actions written by this code, the change above
  classifies a call it never starts as `failed`; the remaining new `unknown`
  outcomes do not establish that nothing was sent:
  - A crash between the claim and the send leaves `executing`, recovered as `unknown` with no
    result. Claim and send are adjacent, with no durable mark between them, so the record
    cannot say whether the POST left.
  - An exception inside the adapter is ambiguous too. `_bounded` sees it as `network-error`,
    and some such exceptions (`http.client.HTTPException`, e.g. `IncompleteRead`) come after
    the request was sent.

  That explains why no operator command is needed for the newly classified before-submission
  cases. This change does not reclassify historical `unknown` rows written by older code,
  including a recorded `CancelledError` or `PreviousRequestRunning` from the old `_bounded`
  path; an operator command or migration for those legacy records is not included. For the
  ambiguous crash or transport cases, settling on the operator's word alone would remove
  C-23.16 (d)'s protection against another spend.
- **What clears the block.** `settle_by_usage` (`actions.py:905-942`, called from
  `Timers._persist`) reconciles the action on a fresh successful usage read for the same
  account, observed no earlier than the action, with `limit_reached` false and `allowed`
  not false. A confirmed reset or the provider's natural reset can permit that read.
- **When it can last.** There is no seven-day bound: reads can be skipped (held, auth-dead,
  disabled or removed), fail, stay limited, or never observe an open window before work uses
  it up again. Regular reads alone do not guarantee reconciliation. The block lasts until
  a qualifying open read is recorded.
- **Follow-up (not done here).** A durable mark at the transport boundary, written just before
  the request is handed to the opener, would let `recover()` settle a crash-before-send orphan
  itself, with no operator.

### P3-6: the 9/22 cascade replay's failure on the base depended on load

- **Change.** `tests/fake/test_reset_credit_incident.py:236-241`: after the first reset the
  replay makes the job due by fixture before admission. On release/217 the job's own
  backed-off clock decided whether that pass placed it, so the base failed at the placement
  (line 237) at low load and at the cascade at high load. It now reaches the cascade every
  run. That the reset itself makes its job due is a case of its own:
  `test_incident_2026_09_22_the_reset_makes_the_job_it_was_spent_for_due_at_once`. It checks
  the `reset-credit.job-due` event and the placement, and fails on the base deterministically
  because no such event exists there.

### Contract nit: C-18.1 and offline cycles

`docs/acceptance-contract.md` C-18.1 and C-23.16 now place reset evaluation after the
usage-read phase, unless cancelled or offline. Offline requires at least one Codex read and
all Codex reads to be network errors (`timers.py:1060-1062`). A failure or cancellation before
that stage skips evaluation. Alerts and status publication follow evaluation; a failure there
does not undo it, so completion of the entire cycle is not a prerequisite.

### P3-4 (follow-up, not changed)

Attempt-reported limits ignore the C-23.17 override (`daemon.py:7191` at e46cc4815; compare
`timers.py:892-898`). If a reset lane's first attempt comes back `limited` because the
provider's inference path lags a reset, the lane gets an account-wide limit closure, (d) no
longer counts it as room, and the job excludes it for good. That could cost one credit per
interval. It is pre-existing, and nothing shows it happening (on 9/22 the reset lanes took
work). Left for its own change.

## Runs

Every run is one pytest process at a time under the shared suite lock, with its own TMPDIR,
removed after. The previous lane's default-count runs saw load 35 to 230; its larger runs
reached 380. The finish job initially saw load 20, then 398 while waiting for the lock.

### At the head

| Run | Head | Result |
|---|---|---|
| `test_reset_credit_review_r1.py`, `test_timers_reset_credits.py`, `test_reset_credit_demand.py`, `test_reset_credit_properties.py` (defaults: 100 examples, mutant searches 300) | 506dfabe3 | 148 passed (555 s, load 80 to 95) |
| `test_reset_credit_review_r1.py` (the lighter interval property) | fcdc81d82 | 22 passed |
| `tests/fake/test_reset_credit_incident.py` | 0572dfb91 | 19 passed |

The code and the three older unit files have not changed since 506dfabe3. Only the
regression module (d3aad6ee7) and the incident file (0572dfb91) changed, and each was rerun
after.

### Before: the same tests on the code they fix

A `git archive` of e46cc4815 (the reviewed head) with this round's test files copied in:

| Run | Result |
|---|---|
| `test_reset_credit_review_r1.py` | 17 failed, 5 passed |
| `test_timers_reset_credits.py` | 4 failed (`test_only_exact_provider_success_confirms`, all four bodies `failed`), 43 passed |
| `test_reset_credit_incident.py -k "door or makes_the_job or one_reset_at_a_time"` | 2 failed (both door-window cases), 4 passed |

- **The 17 that fail:** every P2-1, P3-1 (both cap cases), P3-2, P3-3 and P3-5 case, and both
  new properties.
- **The 5 that pass are controls:** the 5xx case, the clock going back, a started consume
  timing out, `capped` demand never spent for (the old plan already spent only for
  `codex-demand`), and the P2-2 world (its fix is in the checker).
- **The P2-1 property's first counterexample there:** an `empty-2xx` answer recorded `failed`.
- **The P3-2 regression there:** the action is stamped 12:00:00, the pass's start.
- **The interval property's counterexample there:** two POSTs 29 min 59 s apart (a 1 s
  listing, the next pass 30 minutes after the first started).

A `git archive` of release/217 (4c6a8a9c9) with this round's incident file:
- **The cascade replay** (`test_incident_2026_09_22_replay_one_reset_at_a_time_for_waiting_work`):
  three runs, all failed at line 247, the cascade assertion. The first sweep after the job
  was placed on codex-6 spent again: `{'status': 'confirmed', 'trigger_reason':
  'no-dispatchable-lanes', ...}`.
- **The whole file:** 18 failed, 1 passed. The one that passes is
  `test_a_restart_grants_no_extra_redemption`.

### Properties at the reviewer's counts

**Why the runs are split.** A single run at the reviewer's counts did not fit the brief's
20-minute lock hold on this machine today:
- every-rule ×5,000 was stopped after 17.5 minutes held, at load 45 to 120;
- a seeded 1,000-example chunk was stopped after 20 minutes.

Two causes, both measured (`fix1-runs/status`, `fix1-runs/sample-fast-every.txt`):
- **The disk.** fsync took 20 ms (median) and a `synchronous=FULL` sqlite commit 45 ms. Every
  generated world opens its own store and commits dozens of times.
- **The CPU.** At load 160 to 380 a test process got 1 to 2% of a core.

**How they are run instead.**
- **Seeded chunks.** Each count is run as 1,000-example chunks with fixed
  `--hypothesis-seed` values (reproducible, with different random streams per chunk), one
  lock hold each, each capped at 15 to 20 minutes.
- **`fix1-runs/fastsync_plugin.py`.** A run-only plugin, never committed, that opens the test
  stores with `PRAGMA synchronous=OFF` instead of `FULL`. That lowers crash/power-loss
  durability without changing SQLite transaction visibility. These properties simulate
  component restarts, not a process crash or power loss. Faster commits can also reduce
  wall-clock delays and timeouts; no equivalence of timed execution traces is claimed.
- **Where they ran without the plugin.** The default-count runs above used `FULL`, as CI does.
- **Finish plugin.** The prior lane's FULL 1,000-example chunk exceeded 20 minutes even at
  load 35. Its OFF seed 3 also timed out at 20 minutes under load 237–293. With the finish
  host back above 250, the plugin was recreated for the remaining chunks. It supplies a run-only
  SQLite connection subclass that replaces the initializer's `PRAGMA synchronous=FULL`
  with OFF, so writer transactions use OFF from schema setup onward. Each completed chunk below names its mode. The fresh
  seed 3 FULL attempt is retained separately, and each finish chunk has an 18-minute budget
  beginning at pytest session startup, after its suite-lock wait.

| Property | Reviewer's count | Run at the head (0572dfb91 code) |
|---|---|---|
| `test_every_rule_holds_over_generated_worlds` | 5,000 | seed 1: 1,000 passing, 0 failing (62 invalid), 376 s (previous lane, OFF); seed 2: 1,000 passing, 0 failing (70 invalid), 83 s (previous lane, OFF); seed 3: queued, no completed examples; seeds 4 and 5: unrun behind the suite lock |
| `test_a_restart_before_every_step_changes_no_decision` | 2,000 | 0/2,000 completed; seeds 1 and 2 unrun behind the suite lock |
| `test_real_demand_spends_only_when_no_route_lane_has_room` | 2,000 | 0/2,000 completed; seeds 1 and 2 unrun behind the suite lock |
| `test_with_the_switch_off_every_decision_is_release_217s` | 1,000 | 0/1,000 completed; seed 1 unrun behind the suite lock |

The finish seed 3 FULL attempt exhausted its 18-minute budget and exited interrupted
after 1112.00 s (18 min 32 s), with no completed property and no examples counted toward
the requested total. Its output is saved as `every-rule-seed-3-full.txt`; seed 3 is retried
with the run-only OFF plugin.

The finish FULL review/incident control attempt was interrupted by its budget after
1109.39 s: eight deterministic tests passed, then the first new generated property did
not complete. `control.txt` records this partial run. The deterministic regressions and
incident file are queued for a separate rerun, excluding those two new generated properties; their
prior lane's 100-example passes above remain the completed default-count evidence.

### Mutation checks

Each mutant is an isolated `git archive` copy; the six selective revert patches
are saved beside the run output. The cascade check uses the corrected incident test
against the archived release/217 code. The three completed previous-lane cascade outputs
are preserved here as `base217-cascade-{1,2,3}.out`; its archived incident file was
checked byte-for-byte against the current test. These runs retain SQLite synchronous=FULL.

| Finding | Reverted behavior | Check | Result |
|---|---|---|---|
| P2-1 | Restore the old consume classifier: an unrecognised 2xx is failed | unit regressions `-k p2_1` | killed: 6 failed, 2 passed, 14 deselected in 160.68s |
| P3-1, caps | Drop capped from the demand verdict | unit regressions `-k p3_1` | no completed verdict: the earlier process died after session startup; retry awaits the suite lock |
| P3-1, door | Read only the last completed admission pass's hold | incident door-hold regressions | unrun: shared suite lock prevents startup |
| P3-2 | Record the consume at the evaluation start | interval timestamp regression | unrun: shared suite lock prevents startup |
| P3-3 | Remove both shadow-listing hold filters | unit regressions `-k p3_3` | unrun: shared suite lock prevents startup |
| P3-5 | Return timeout/network-error for calls never started | unit regressions `-k p3_5` | unrun: shared suite lock prevents startup |
| Cascade replay | Archive release/217 (4c6a8a9c9), copy in the corrected replay | 9/22 one-reset-at-a-time replay | killed: all 3 previous-lane runs fail at the cascade assertion (9.33 s, 5.24 s, 1.23 s) |

## Not run

As of 18:02 EDT, every-rule seeds 3 to 5 (3,000 examples), restart
seeds 1 and 2 (2,000), real-demand seeds 1 and 2 (2,000), switch-off seed 1 (1,000),
the five selective mutation checks still lacking a verdict, and the finish deterministic controls have no
completed result: seed 3 is queued behind the shared suite lock, and the sequential
runner has not started the later checks. Interrupted runs above count
no examples toward the requested totals.

- The whole test suite, frontend and e2e tests.
- CI's GIL Python 3.12 and 3.14 interpreters; the finish venv is free-threaded Python 3.14.
- A legacy-unknown migration or operator settlement command (P3-5), the transport-boundary
  mark proposed above, and the pre-existing attempt-limit override follow-up (P3-4).

The resumed finish runner queued seed 3 at 16:33 EDT and still had no pytest
session-start marker at 17:50 EDT. The shared suite lock was held; `lsof` identified
its open descriptor in the separate `20261011-114740-pr68-split-build` worktree
(PID 35288, parent 35249). That job was left untouched. Lock waiting counts no
examples and is separate from a chunk's pytest budget.

Production code remains unchanged. No network remote was used, no live reset policy
was enabled, and all reset-credit HTTP calls in these tests use fake openers.

The finish cleanup removed the five stale per-run TMPDIRs left by the interrupted attempt;
the active queued runner keeps its own TMPDIR and removes it in its finalizer.
