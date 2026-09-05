# Lane `cli` progress

State, done, next. Updated with every commit on `lane/cli`.

## State

Complete and green. `uv run pytest -q tests/unit/` — 177 tests, ~17 s. The command-line
client, its offline reader, and its socket client are built against the acceptance
contract, and an adversarial multi-agent review of all three modules and the tests has
been folded in.

## Done

- Read the contract sections 1, 2, 5.8, 6.1, 7.1, 15.4, 16, 17; `protocol.py`;
  `contracts.py`; `store_schema.sql`; v1 `cli.py`, `delegate.py:700-760`, `README.md:1-120`.
- `subfleet/client.py` — one request line, one response line (C-16.1); `DaemonUnavailable`
  to exit 69; a `daemon.lock` whose recorded identity is provably dead is no daemon
  (C-5.8), with the reason it reached that verdict (C-5.3); `ps` pinned to `LC_ALL=C`
  and `TZ=UTC`; a zombie is not alive; the response line is bounded in time and size;
  a daemon code outside the C-17.3 table becomes 1.
- `subfleet/offline.py` — read-only store plus the guardian receipts (C-17.5, C-3.4,
  C-5.2); one read snapshot per call (C-3.2); a newer schema is named (C-3.5); `kill`
  signals only a group whose leader is the verified guardian (C-5.3, C-5.4); a stored
  `provider` reading past its TTL renders stale (C-9.1).
- `subfleet/cli.py` — every verb and alias (C-17.1), the `run` flags (C-17.2), the one
  exit-code table (C-17.3), stdout/stderr and `--json` (C-17.4), offline mode (C-17.5),
  the detached default and four-line hint (C-17.6), notice acknowledgement (C-15.3), the
  long poll with backoff (C-15.4), `daemon start|stop|status|logs|install`, offline
  `doctor`.
- `subfleet/protocol.py` — additive `LanesArgs`, `ReadingsArgs`, `PingArgs`.
- `tests/unit/{conftest,test_cli,test_offline,test_daemon_verbs}.py` — a fake daemon on a
  temp socket, a store built from `store_schema.sql` with the guardian receipts beside
  it, and a stub `subfleetd` (including a self-daemonising one).

## Next

Nothing outstanding in this lane. The open questions for the integrator are in the
final report; the one that blocks integration is the `lstart` rendering seam below.

## Decisions and deviations (carried into the final report)

- **`ps` rendering is a cross-lane seam.** `lstart` is rendered in the reader's locale
  and timezone. The CLI pins `LC_ALL=C TZ=UTC`; `subfleet/procs.py` and the daemon that
  writes `daemon.lock` and `start.json` must do the same or every identity check fails
  for live processes. Demonstrated: a value recorded in local time reads as "gone".
- `runs reap` is a read-only reconciliation report: C-16.2 has no `reap` op and C-3.4
  reserves writes for the daemon.
- Inline prompt text is staged at `$SUBFLEET_HOME/inbox/<sha256 of the request id>.md`
  (0700/0600, pruned after a week); C-2.2 does not list `inbox/`.
- `resume` is `submit` with `kind: "resume"` and `parent_job_id` set; C-16.2 has no
  `resume` op.
- `run` requires `--task/--tier` or a `-m`/`-a`/`-H` pin; v1 classified from prompt
  content, and v2 routing is policy data.
- `--json` output is JSON Lines throughout (v1 printed an indented array). A `run` that
  waits inline prints the dispatch object at once and the terminal state as a second one.
- `kill --wait` returns the job's terminal code, so cancelling returns 130 (C-17.3).
- `daemon install` writes `~/Library/LaunchAgents/…` — outside the state root C-2.1
  bounds, as the lane brief instructs.
- An `outcome_class` of `auth-dead` or `cli-too-old` supplies exit 5 or 6 when the job's
  rc is absent or outside the table; the rc still rules whenever it is in the table.

## Incidents

- 2026-09-05: `uv run subfleet ...` resolved to the **v1** `subfleet` on PATH and
  dispatched a real v1 job (`20260905-115301-hi`). Cancelled immediately with
  `subfleet kill`. All later invocations use `uv run python -m subfleet.cli`.
- The `unscoped-search` guard hook refused one command that paired `grep` with a
  `/tmp` literal; the work was split into separate calls rather than bypassed.
