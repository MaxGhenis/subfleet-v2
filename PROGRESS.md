# Lane `cli` progress

State, done, next. Updated with every commit on `lane/cli`.

## State

Building the command-line client of subfleet v2: `subfleet/client.py` (socket client),
`subfleet/offline.py` (read-only store fallback), `subfleet/cli.py` (verbs, exit codes),
and the three unit test modules. Owned clauses: C-17.1 to C-17.6, with C-5.8 (lock
identity), C-15.4 (long-poll `wait`), C-16.1 to C-16.3 (wire), C-3.4 (read-only reads).

## Done

- Read the contract sections 1, 2, 5.8, 6.1, 7.1, 15.4, 16, 17; `protocol.py`;
  `contracts.py`; `store_schema.sql`; v1 `cli.py`, `delegate.py:700-760`, `README.md:1-120`.

## Next

1. `subfleet/client.py` — socket client, lock identity check, `DaemonUnavailable`.
2. `subfleet/offline.py` — read-only SQLite for `runs`, `runs show`, `status`, `kill`.
3. `subfleet/cli.py` — verbs, aliases, deprecations, exit codes, daemon verbs, doctor.
4. `tests/unit/test_cli.py`, `test_offline.py`, `test_daemon_verbs.py`.
5. Adversarial review pass, then the integrator report.

## Decisions and deviations (carried into the final report)

- (none yet)
