# Lane brief: timers (milestone 5: probe cycle, keepalive, reset-credit actions, alerts, retention, status.json)

You are building the daemon's timers for subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/timers`). v1 ran four launchd jobs (watchdog, keepalive, mirror, revive) with their own locks and state files; v2 runs them as timers inside the one daemon, driven by policy data, writing readings, closures, actions, and events into the store. Milestone 5 replaces three of the four; the mirror and revive belong to the sessions lane.

## Read first, in this order

1. `docs/acceptance-contract.md` sections 9, 18 (C-18.1), 19 (actions), 8.4 (retention), and these carried-forward clauses: C-23.16 to C-23.20 (reset credits and five-hour windows), C-23.27 (one monitoring cycle, one verdict), C-23.29 (a keepalive pass is bounded), C-23.44 to C-23.47 (auth-dead, one account one lane, shadowing, the auth store), C-23.52 (what an alert says and when a recovery is one), C-23.13 (only the holder publishes an action's result). Cite clauses in every test docstring.
2. `docs/plan.md` amendments 5 (typed external actions) and 11; `docs/plan-b-rev4.md` "Reset credits and keepalive" and "Timers".
3. `subfleet/daemon.py` (the control loop, `_schedule`, workers with deadlines; `retention.py` is already called hourly), `subfleet/store.py`, `subfleet/capacity.py`, `subfleet/scheduler.py`, `subfleet/adapters/codex.py` (`probe`, and where reset-credit endpoints would sit), `subfleet/adapters/claude.py` (`probe`), `subfleet/contracts.py` (`ActionState`, defaults).
4. v1, read-only (never modify, never run its commands): `~/chief-of-staff/subfleet/subfleet/reset_policy.py` (the authorized policy: one credit per evaluation, trigger when no lane is dispatchable or weekly headroom is below the floor, candidates ordered furthest natural reset first, then fewest in flight, app-shadowed lanes last, minimum interval), `~/chief-of-staff/subfleet/subfleet/codex.py` (`consume_reset_credit`, `list_reset_credits`, the endpoints and the request id), `~/chief-of-staff/subfleet/subfleet/keepalive.py`, `~/chief-of-staff/subfleet/subfleet/watchdog.py` (`evaluate_conditions`, the alert latch keys, the recovery rule, the offline rule), `~/chief-of-staff/subfleet/app/SubfleetApp.swift` only for the `status.json` shape the menu bar reads. `docs/migration.md` names which v1 records the importer turns into `actions` and `events`; you consume those.

## Scope

- `subfleet/timers.py`: a `Timers` component the daemon owns, each timer a bounded worker with a deadline and cancellation, never blocking the control loop (C-16.4):
  - **Probe cycle** (C-18.1): wait `probe_interval_s` (default 60 seconds) after cycle completion, then probe each idle, enabled, unlatched lane once per window; `auth-dead` lanes are not probed until re-enrolment; a revoked-token Codex home is latched until its `auth.json` changes (C-23.47); readings written per C-9.1; Codex windows classified by duration (C-9.7); an expired Codex token gets exactly one automatic heal (a tiny `codex exec` turn through the guardian, C-23.47), never an in-process refresh.
  - **Keepalive** (C-23.19, C-23.29): every 5 h 05 m, a Haiku turn on each idle Claude lane that made no request in the last five hours, at most four workers and a 60 s per-lane timeout, recorded as `admission-observed` with the request timestamp as the window's start; a running attempt counts as a recent request; never a window reset.
  - **Reset credits** (C-19, C-23.16 to C-23.18, C-23.38): *Superseded on 2026-09-23 (#33; on release/217 2026-10-10). The supply-side trigger this bullet describes spent all six banked credits in about four hours on 2026-09-22, and one went on a lane under an operator hold on 2026-09-30. C-23.16 now spends a credit only for a job waiting on that lane, one at a time, never on a held lane, with automatic redemption off by default. Build against the contract, not this brief.* The original brief: a policy-driven action, evaluated each cycle: trigger when no Codex lane is dispatchable or the weekly headroom across dispatchable lanes is below `headroom_floor`; candidates are limited lanes with a gifted credit, ordered furthest-out weekly reset first, then fewest in flight, `app_shadowed` last; one credit per evaluation; minimum interval `min_interval_min` (30); the `actions` row is `pending` with `op_key` = account key plus credit id and a fresh UUID4 `redeem_request_id` persisted before the call, `executing` during it, `confirmed` only for code `reset` with `windows_reset` greater than zero, `unknown` on a timeout until a usage read settles it; a confirmed consume reopens the lane and clears its closure but writes no window numbers until the usage endpoint reports them (C-23.17); fleet credits remaining is null when any lane's count is unreadable (C-23.18); only gifted entitlements are listed or consumed (C-23.7).
  - **Alerts** (C-23.52, C-23.27): conditions evaluated once per cycle from one snapshot; alert on transition, re-alert at most every 6 h while persisting (once per day for expiring capacity), one recovery notice when the last condition for a home clears and no other is active; a cycle where every Codex probe is a network error is offline and silent; latches live in `events` (kind `alert-latch`) and the importer's latches are honoured. Delivery is through the `ping` verb's notice path to the operator session, never a direct socket write from the timer.
  - **Retention** (C-8.4): keep the hourly pass; pin active, quarantined, unread-notice, salvage-referenced, and gate-evidence jobs; byte accounting.
  - **status.json**: written every probe cycle under the state root in the shape the v1 menu bar app reads, with percentages only from `provider` or `stale-provider` readings (marked stale), words for everything else, and `identity_status` per lane when present.
- `policy.json` keys (additive, with defaults in `subfleet/default_policy.json`): `timers.probe_interval_s`, `timers.keepalive_interval_s`, `reset_credits.enabled|min_interval_min` (originally also `headroom_floor_pct`, which C-23.16 retired), `alerts.realert_hours`, `alerts.expiring_capacity_daily`.
- Daemon wiring: `Timers` started after recovery and stopped on shutdown; `daemon.status` reports each timer's last run, next due, and last error type.
- Tests: `tests/unit/test_timers_probe.py`, `test_timers_keepalive.py`, `test_timers_reset_credits.py` (state machine including `unknown` after a timeout and the settle-by-usage-read path, the one-credit rule, ordering, the interval, the gifted-only rule, the `op_key` uniqueness), `test_timers_alerts.py` (transition, re-alert cadence, recovery, offline silence, imported latches), `test_status_json.py`; `tests/fake/test_timers_end_to_end.py` running the daemon against the fakes with intervals shrunk through policy to seconds. Reset-credit HTTP is stubbed through an injectable opener; no real endpoint is called anywhere in tests.

## Out of scope

Mirror and revive (sessions lane), gates, the importer, hooks. Do not change the meaning of any existing clause; if one must change to be implementable, say exactly what and why in your final message.

## Acceptance for this lane

- Against the fakes with shrunk intervals: readings appear per lane per cycle; a lane closed as `auth-dead` is never probed again; a keepalive skips a lane with a request in the last five hours; a reset credit is consumed once when the trigger holds, its action row goes `pending` to `executing` to `confirmed`, a timeout leaves it `unknown` until a usage read; an alert fires on transition and not again within 6 h; a recovery notice appears once; `status.json` carries no percentage without a provider reading.
- `uv run pytest -q tests/unit/test_timers_*.py tests/unit/test_status_json.py tests/fake/test_timers_end_to_end.py` passes in under 40 s; the full suite stays green. Run with `/usr/sbin` and `/bin` reachable; the sandbox may deny process inspection, in which case say so and rely on the unit tests.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

Standard library only at runtime. Commit after every coherent step. Your sandbox may have no network; if `git push` fails on DNS, do not retry; the integrator pushes `lane/timers` from outside. Never commit to `main`, never force-push. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Built; Tests (command, count, time); Clauses covered; Clauses not covered and why; Seam changes; Contract questions; Open questions for the integrator. No preamble.
