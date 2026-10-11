# Reset credits, demand only: #33 ported onto release/217 (2026-10-10)

PR #33 (`origin/pr-33`, head 05a038d2e, 2026-09-25, based on main at 3f155e50f) made
reset-credit redemption demand-only. It conflicts with release/217, so its behavior is
ported here onto `origin/release/217` (4c6a8a9c9) rather than its text.

Why now: on 2026-09-22 the installed timer redeemed all six banked credits in about
four hours (the first with zero jobs waiting, each later one because the lane just
reset was busy at its unmeasured slot cap and read as exhausted), and on 2026-09-30 it
redeemed on a lane under an operator hold. `~/.subfleet/policy.json` has had
`reset_credits.enabled: false` since 9/23. Eleven gifted credits are banked.

## Commits (on 4c6a8a9c9)

| Commit | What |
|---|---|
| 778d627a7 | `ResetCredits.evaluate` spends one credit only for a job waiting on that lane (C-23.16); `Daemon._reset_demand`; the marker; `default_policy.json` off; the importer's `reset-settings` row |
| 387b57f23 | The switch and holds bind the operator too; holds read from the store (`store_holds`); #33's unit tests |
| 7188e9b79 | Replays of the 9/22 and 9/30 incidents through the real daemon |
| 978e99f3f | #33's importer, operations and timer end-to-end cases |
| 0e47e1c9e | Hypothesis properties, the switch-off differential, mutation checks |
| 1553faff0 | Contract: C-23.16 (a) to (f), C-23.38, C-11.1, C-18.1; invariant row 160; migration row; timers brief |
| 476055ddb | `test_status_json`'s reset-policy cases expect C-23.16's refusals |

## The rule, clause by clause, in the code

Line numbers are at 476055ddb.

1. **A job is actually held waiting for Codex capacity.**
   `subfleet/daemon.py:1767-1804` `Daemon._reset_demand`: only jobs `waiting` on
   `capacity`, not being cancelled, whose latest admission look evaluated the route
   and found no lane (`lane_wait`, set at `daemon.py:5427-5429` as
   `not decision.chosen_lane`, cleared by `_refresh_hold` at `daemon.py:4736-4742`), and
   whose last pass held them for that same reason (`daemon.py:1789-1790`). Disk and
   machine-busy holds are taken at the door (`daemon.py:5036-5039`) without a route look,
   so their hold reason differs from the wait's label and they are dropped. A
   transient retry waiting for a slot on its own lane is skipped (`daemon.py:1797-1799`).
   `subfleet/actions.py:254-284` `demand_verdict` makes the job `codex-demand` only
   when no lane of its route has room and at least one Codex lane is limited.
   `actions.py:634-651`: with no demand view, or no `codex-demand` job, nothing is
   spent (`no-demand`).
2. **No Codex lane the job's route could use has room; busy, unmeasured and freshly
   reset lanes are room.** `actions.py:217-251` `route_lane_state`: only an account-wide
   `provider-limit`/`credits` closure, a `cooldown`, or `below-floor` on a fresh
   reading is `limited`; everything else that is not out of reach (busy, no slot, a
   probe or pilot holding it, fleet cap, unmeasured) is `capacity`. A freshly reset
   lane has its account closures released (`actions.py:862-867`) and its readings held
   out by the C-23.17 override (`daemon.py` `_pick`), so it is capacity too. The job is
   judged again just before the spend (`actions.py:681-688`, `demand-changed`).
3. **Exactly one credit, on a lane of that job's route; furthest weekly reset, fewest
   in flight, lowest lane number; app-shadowed last.** `actions.py:644-651`: candidates
   are the job's own `limited_lanes` among eligible lanes, sorted by `_order`
   (`actions.py:120-128`). `actions.py:811-860` `_select`: shadowed lanes last
   (`actions.py:823-829`). One `add_action` per evaluation (`actions.py:723`), under a
   per-component lock (`actions.py:596-597`) and the store's one-unsettled-action gate.
4. **Never on a held, auth-dead or disabled lane.** `actions.py:185-214` `lane_hold`
   (disabled, another owner, an open `operator-hold` or `auth-dead` closure of any
   scope, a latched credential, a mismatched identity), fed by `actions.py:149-182`
   `store_holds` (closures plus `lane.held`/`lane.released` events, because
   `Store.put_closure` keeps one open closure per lane and scope). Applied in
   `_eligible` (`actions.py:543-552`), to an operator's lane (`actions.py:621-626`,
   `lane-held`), and again on the store's current rows at the spend
   (`actions.py:678-680`, `713-715`, `_refused` at `792-809`). `_redeemable`
   (`actions.py:135-138`) drops disabled lanes.
5. **Never a second redemption while a lane reset earlier still has room; never within
   `min_interval_min`; the interval survives a restart.** `actions.py:506-529`
   `reset_lanes_open` (lanes with a `confirmed` or reconciled consume in the last seven
   days that still read as room) returns `reset-lane-open` (`actions.py:616-619`).
   `actions.py:477-492` `_gate`: one unsettled action blocks all; the interval is
   measured from the newest `confirmed` or reconciled row in the `actions` table, read
   on every evaluation (`actions.py:613`, again in the write transaction at `707`).
   Nothing about spends is kept in memory.
6. **C-23.16's provider conditions; C-23.7 untouched.** `actions.py:543-552`: a fresh
   `limit_reached: true` read of the lane's own account; `actions.py:67-76`
   `gifted_credits`: `available`, `codex_rate_limits`, never paid. A consume confirms
   only on `code: reset` with `windows_reset > 0` (`actions.py:741-742`). The adapter's
   URL allowlist (`subfleet/adapters/codex.py:27-29`) is unchanged.
7. **`default_policy.json` ships `enabled: false`.** `subfleet/default_policy.json:119-122`;
   code default `actions.py:22`. `headroom_floor_pct` is no longer required
   (`subfleet/policy.py:521-529`: checked if present, never read).
8. **A `no-reset` marker in the state root blocks listing, writing and consuming.**
   `actions.py:42-44`, `390-397` (`lexists`, so a dangling link counts); checked first
   in `evaluate` (`actions.py:607-609`, before the switch), before each listing
   (`actions.py:836-837`), in the write transaction (`actions.py:703-705`) and at the
   consume (`actions.py:729-734`). Wired by `subfleet/timers.py:88-89`. The importer
   turns v1's hold file into the marker and never removes it
   (`subfleet/importer.py:969-1041`). The daemon logs the switch and the marker at
   startup (`daemon.py:798-802`).
9. **A manual `subfleet reset codex <lane>` obeys the same rule and spends only on the
   named lane.** `actions.py:621-626`: only the target is a candidate; the plan uses
   the waiting jobs' `limited_lanes`, so no job naming the lane means no spend; no
   other lane is tried. `timers.py:299-317` `evaluate_resets` passes the same demand
   reader for the timer and the operator; `subfleet/operations.py:147-151` gives the
   dry-run preview the same demand without writing. The switch refuses the operator
   too (`actions.py:610-612`), as release/217 already did.

## Where the port departs from #33's text

- **The operator obeys the rule.** In #33, `reset codex <lane>` spent on the named
  lane with no waiting job, with the switch off, and while a lane reset this week had
  room (#33's `test_operator_names_one_lane_without_demand_or_the_automatic_switch`,
  `test_an_operator_lane_is_not_held_back_by_a_lane_reset_this_week`). Rule 9 (the
  owner, 2026-09-30) says a manual reset obeys the same rule, and release/217 already
  refused it with the switch off, so here it needs a job whose limited lanes include
  the named lane, the switch, one at a time and the interval, and never tries another
  lane (`test_an_operator_reset_obeys_the_same_rule`,
  `test_an_operator_reset_never_substitutes_another_lane`,
  `test_an_operator_lane_is_held_back_by_a_lane_reset_this_week`). Only the timer
  applies the weekly-headroom test of (c).
- **No admission reservation.** #33 kept a reset lane for the job it was spent for
  until that job was placed, gone or expired, and let it pass older jobs for that lane
  (seven #33 cases in `tests/fake/test_reset_credit_incident.py`). release/217's
  admission has moved on (every concurrency cap null by default since 2026-09-27,
  C-6.4; C-6.9's FIFO holds only in a capped pool), so the port makes the job due at once instead (`actions.py:760-771`
  `_job_due`, after a confirmed consume and after an `unknown` one a usage read
  reconciles open, `actions.py:898-905`). Whichever job takes the reset lane, rule 5
  holds: the lane has room, so nothing more is spent until it is used up again.
- **Holds from events too.** #33 read holds from closures only. `Store.put_closure`
  keeps one open closure per lane and scope, so an operator hold ending before an
  open account limit leaves no `operator-hold` row; `store_holds` also reads the
  hold's `lane.held` and `lane.released` events (the 9/30 replay runs both shapes).
- **`headroom_floor_pct` is optional.** A policy that still names it is validated
  and ignored, so today's `~/.subfleet/policy.json` loads unchanged.

Kept from release/217: the probe leases in the status snapshot (C-18.1) are laid after
the reset policy judged it, and admission's view (which `_reset_demand` uses) honours
them, so a probe-held lane is room; lane identity (`identity_blocked` in `lane_hold`);
closures; admission holds (disk, machine-busy, lease, probe, fleet-full, slot-kept,
person) are never demand.

## Invariants (tests/unit/test_reset_credit_properties.py)

Generated worlds: 1 to 5 Codex lanes, each limited, open, unmeasured, busy, under an
operator hold (with or without its own closure row), auth-dead or disabled, with or
without a banked gift and a weekly reset 1 to 6 days out; 0 to 3 waiting jobs, each
with its own exclusions; 1 to 5 steps that move the clock (0, 1, 10, 29, 31 or 120
minutes) and may use a reset lane up again, restart the component, or put down or lift
the marker; the timer or an operator's lane; real demand (`scheduler.evaluate` on the
store's view, judged by `demand_verdict`, as `_reset_demand` does) or adversarial
demand (any job naming any lanes as limited). Judged against the world's own model:

- at most one action per evaluation;
- none with the switch off or the marker down, and then no HTTP request at all;
- none unless demand names that job and that lane as limited;
- with real demand, none while a lane of that job's route has room, and only on its route;
- never on a held, auth-dead or disabled lane;
- none while a lane reset in the last week still has room;
- none within `min_interval_min` of the last spend;
- an operator's spend is only on the lane it names;
- a restart before every step changes no decision.

| Test | Examples |
|---|---|
| `test_every_rule_holds_over_generated_worlds` | 100 (`SUBFLEET_RESET_EXAMPLES`) |
| `test_real_demand_spends_only_when_no_route_lane_has_room` | 100, biased to busy and limited fleets, switch on |
| `test_a_restart_before_every_step_changes_no_decision` | 100 worlds, each played twice |
| `test_with_the_switch_off_every_decision_is_release_217s` | 100, with 0 to 4 seeded earlier actions |
| `test_a_world_where_spending_is_allowed_does_spend` | 1 fixed world (the properties are not met by spending nothing) |

Differential: with `enabled: false` (and no marker, which is new), this code and
release/217's own `actions.py` (kept verbatim as `tests/unit/reset_credits_release217.py`)
both answer `disabled`, make no HTTP request, write the same action rows, report the
same `fleet_credits_remaining`, the same C-23.17 override for every lane, the same
usage reconciliations, and leave the same closures and jobs.

Mutation checks (Hypothesis `find`, up to 300 generated worlds each, generate phase only):
rule 2 (a busy lane counted as limited), rule 4 (holds ignored), rule 5 (one at a time
dropped; the interval dropped; the interval kept in memory, so a restart forgets it).
Results: see "Runs" below.

## Incident replays (tests/fake/test_reset_credit_incident.py)

They drive the real daemon's `_admit`, `reset_credits_cycle`, `probe_cycle` and
`_hold_lane` over the real Codex adapter with a fake WHAM transport, under the slot
caps of the incident (`tests/caps.py`). They use only what release/217 already had, so
the same file runs against a `git archive` of 4c6a8a9c9.

- `test_incident_2026_09_22_no_work_waiting_spends_and_lists_nothing`: six limited
  lanes, six gifts, nothing queued.
- `test_incident_2026_09_22_replay_one_reset_at_a_time_for_waiting_work`: one waiting
  job gets one reset (codex-6, furthest out) and runs there; three more jobs queue
  while codex-6 is busy, through 12 admission and timer sweeps and probe cycles: no
  second spend. Used up again, the next waiting job gets codex-5, and only that.
- `test_incident_2026_09_30_replay_a_held_lane_never_takes_a_credit[before-the-limit|after-the-limit]`:
  codex-6 under an operator hold (its own row, or only its `lane.held` event):
  `reset codex codex-6` is refused and never listed; the timer spends on codex-5.

Before/after: see "Runs" below.

## Runs

Pending at the time of this commit: the shared suite lock was about eight runs deep
(load average 90 to 270). This section is filled in by the next commit.

## Not run

- The whole suite. Only the reset-credit, timers, operations, policy and importer
  files, plus `tests/unit/test_status_json.py` (changed here), were in scope.
- `tests/unit/test_operator_notices.py` (one reset-credit case, switch off) and the
  other files that only mention reset credits in passing
  (`tests/fake/test_native_maintenance_startup.py`,
  `tests/fake/test_unadmittable_pin_rereview.py`,
  `tests/unit/test_store_contention_queries.py`, `tests/unit/test_fake_codex_http.py`).
- CI's GIL interpreters: the worktree's `.venv` is free-threaded CPython 3.14.7t; CI runs
  GIL 3.12 and 3.14.
- No real provider endpoint was called and nothing was redeemed.
