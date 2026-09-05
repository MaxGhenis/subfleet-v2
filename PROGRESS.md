# Lane `cli` progress

State, done, next. Updated with every commit on `lane/cli`.

## State

The command-line client is built and green: `subfleet/client.py`, `subfleet/offline.py`,
`subfleet/cli.py`, and three unit modules (106 tests, 5.4 s). Remaining work is the
adversarial review pass and the integrator report.

## Done

- Read the contract sections 1, 2, 5.8, 6.1, 7.1, 15.4, 16, 17; `protocol.py`;
  `contracts.py`; `store_schema.sql`; v1 `cli.py`, `delegate.py:700-760`, `README.md:1-120`.
- `subfleet/client.py`: one request line, one response line (C-16.1); `DaemonUnavailable`
  to exit 69; a `daemon.lock` with a provably dead identity is no daemon (C-5.8, C-5.3).
- `subfleet/offline.py`: read-only store for `runs`, `runs show`, `status`, `kill`
  (C-17.5, C-3.4); offline `kill` signals only a verified pgid (C-5.3, C-5.4).
- `subfleet/cli.py`: every verb and alias (C-17.1), the `run` flags (C-17.2), the one
  exit-code table (C-17.3), stdout/stderr and `--json` (C-17.4), offline mode (C-17.5),
  the detached default and four-line hint (C-17.6), `daemon start|stop|status|logs|install`,
  offline `doctor`.
- `subfleet/protocol.py`: additive `LanesArgs`, `ReadingsArgs`, `PingArgs`.
- `tests/unit/{conftest,test_cli,test_offline,test_daemon_verbs}.py`.

## Next

1. Adversarial review of the three modules; fix what it confirms.
2. Final integrator report.

## Decisions and deviations (carried into the final report)

- `runs reap` is a read-only reconciliation report: C-16.2 has no `reap` op and C-3.4
  reserves writes for the daemon, so it names the orphans and who finalizes them.
- Inline prompt text is staged at `$SUBFLEET_HOME/inbox/<request id>.md`; C-2.2 does
  not list `inbox/`, so the clause needs the directory added or `SubmitArgs` needs a
  `prompt_text` field.
- `resume` is `submit` with `kind: "resume"` and `parent_job_id` set; there is no
  `resume` op in C-16.2.
- `run` requires `--task/--tier` or a `-m`/`-a`/`-H` pin; v1 classified from prompt
  content, and v2 routing is policy data, so silent classification was dropped.
- `run --json` while waiting inline prints the dispatch object at once and the terminal
  state as a second object; C-17.4 allows one JSON object per line.

## Incidents

- 2026-09-05: `uv run subfleet ...` resolved to the **v1** `subfleet` on PATH and
  dispatched a real v1 job (`20260905-115301-hi`). Cancelled immediately with
  `subfleet kill`. All later invocations use `uv run python -m subfleet.cli`.
- The `unscoped-search` guard hook refused one command that paired `grep` with a
  `/tmp` literal; the work was split into separate calls rather than bypassed.
