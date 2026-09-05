## Built

Milestone 5 timers are implemented on `lane/timers`, with coherent commits and a maintained `PROGRESS.md`.

- Daemon-owned probe and keepalive workers, deadlines/cancellation, idle-lane reservations, recovery, shutdown, and per-timer last-run/next-due/error status.
- One post-heal probe verdict; duration-classified Codex readings; disabled auth-dead lanes; one CLI heal per credential epoch with a twenty-minute refresh cooldown; revoked-home latches; duplicate-account detection and re-enrolment recovery.
- Haiku keepalive requests with four-worker/60-second limits, five-hour activity checks, request-time observations, and no synthetic jobs or quota windows.
- Gift-only reset actions with durable UUID requests, unique operation keys, holder fencing, atomic confirmation/closure reopening, imported history, strict shadow ordering, interval/freshness checks, bounded injectable HTTP, and usage reconciliation.
- Durable/imported alert latches, transition/re-alert/recovery behavior, offline silence, daily expiring-capacity notices, and an atomic provider-evidence-only `status.json` projection.
- Hourly retention preserves required pins, accounts for symlink/partial-deletion bytes, and stops cooperatively on cancellation/deadline.

## Tests (command, count, time)

Environment: `UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv" UV_NO_SYNC=1`, with `/usr/sbin` and `/bin` on `PATH`.

- `uv run pytest -q tests/unit/test_timers_*.py tests/unit/test_status_json.py tests/fake/test_timers_end_to_end.py`: **120 passed, 2 skipped, 2.50 s**. The skips require Unix socket binding and actual guardian process inspection. In-process daemon cycles run with both confirmed redemption and timeout-to-usage reconciliation.
- `uv run pytest -q --tb=line`: **1001 passed, 62 skipped, 86 failed, 22.99 s**. The untouched starting commit (`5bd7b788`) had **876 passed, 60 skipped, 86 failed, 22.26 s**. The failing node-ID sets are identical: **zero new failures**. Existing failures are in CLI (74), daemon verbs (5), guard preflight (5), and offline process checks (2).
- `git diff --check` passes; every new test docstring cites contract clauses. Reset/usage HTTP is injected; no real provider endpoint was called.

`uv sync --group dev` failed fetching dependencies because sandbox DNS is unavailable. Already-installed dependencies were copied from the shared checkout into the ignored local `.venv`, then tests ran with `UV_NO_SYNC=1`. No guard was bypassed. No v1 command, push, or main-branch commit was attempted.

## Clauses covered

C-3.1–3.3; C-8.4; C-9.1, C-9.3–9.7; C-16.4; C-18.1; C-19.1; C-23.7, C-23.13, C-23.16–19, C-23.29, C-23.38, C-23.44–47, C-23.52. C-23.27's post-heal verdict/offline behavior is covered, with its cadence ambiguity below.

## Clauses not covered and why

- C-23.20 concerns revive, explicitly assigned to the sessions lane. Mirror, revive, gates, importer, and hooks were not implemented here.
- Live socket/guardian validation and an entirely green full suite cannot be demonstrated inside this sandbox. Existing tests require denied socket binding, process inspection, or guard scratch-directory writes; the exact baseline comparison is recorded above.
- The literal reading of C-23.27 requiring twenty minutes between *all* probes conflicts with C-18.1's 300-second usage cycle. Twenty minutes is enforced between automatic CLI refresh attempts; ordinary usage cycles follow C-18.1.

## Seam changes

- Additive schema **v2** adds `service_notices` for jobless `ping` messages. `notice.pending`/`notice.ack` expose these using negative notice IDs; existing job notices keep positive IDs. Connect the notices lane to this path or consolidate it during integration.
- `alerts.operator_session` selects the recipient; absent configuration, notices park in the `operator` inbox. No timer writes a socket directly.
- `Timers`, `ResetCredits`, and Codex `probe_status`/credit methods provide injectable seams. Guardian probes now support jobless timer recovery and publish a request timestamp. Existing dispatch probes/attempts disable auth-dead lanes immediately.
- Timers share the confirmed-reset routing override with daemon views. Historical percentages remain provider-derived and are marked stale while propagation is pending.
- The v1 Swift reader still targets v1 `snapshot.json`. Integration must point it at v2 `<state-root>/status.json` and teach it the explicit stale metadata. Its JSON shape and auth warning alias are preserved; Swift was left read-only.

## Contract questions

No contract clause was edited.

1. Clarify C-23.27's “two probes” as **“two refresh probes”** if C-18.1's five-minute usage cadence is intended. Both literal cadences cannot hold simultaneously.
2. C-23.13 makes `unknown` results immutable, while C-19.1 requires settlement. The action row remains `unknown`; an `action.reconciled` event records `effective_state: settled`, `outcome: usage-open`. This never invents a successful consume response or overwrites the holder's result. Consumers must use reconciliation evidence.
3. C-23.17's guessed seven-day reset is recorded as action metadata, never fabricated utilization. Jobless guardian timer turns follow C-8.4's explicit no-jobs requirement; clarify that exception if C-23.54's general submission wording is intended to include monitoring.

## Open questions for the integrator

- Set the actual operator session and merge the jobless notice seam with the notices lane.
- Reconcile schema migration numbering with other parallel lanes and update the menu app's path/stale handling.
- Run socket/guardian/full-suite checks outside this sandbox and confirm the cadence/reconciliation interpretations above.
- Push `lane/timers` from outside if required; this lane was not pushed.
