## Built

- `tests/bin/fakeprov`: deterministic success, delay, reported-limit, crash,
  escaped-session, ignored-SIGTERM, and missing-executable scenarios.
- `tests/fake_adapter.py`: injectable adapter matching the shared interface,
  preserving stdout bytes and the reported limit clock.
- `tests/fake/conftest.py`: isolated daemon subprocess/socket harness with
  read-only SQLite observation and capability-gated real process tests.
- `tests/fake/run_daemon.py`: fake daemon launcher with crash/hold hooks and
  fsync/rename instrumentation, with no production process-identity bypass.
- `tests/fake/test_daemon_contract.py`: named socket, lifecycle, cancellation,
  export, process containment, singleton, and receipt acceptance tests.
- `tests/fake/test_crash_matrix.py`: real SIGKILL matrix at reserved, starting,
  running, finalizing, terminal, notice, export, and salvage boundaries.
- `tests/fake/test_fake_provider.py`: direct fake provider and adapter checks.
- `tests/fake/test_state_contract.py`: deterministic durable-state, admission,
  recovery, cancellation, publication, retry, and fault-injection checks.
- `PROGRESS_FAKE.md`: committed state, completed work, next steps, and tool limits.

## Tests

`UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run pytest -q tests/fake`

39 passed, 28 skipped in 0.81 seconds. The passing tests use no production
process-identity substitution: only explicitly scoped deterministic fixtures
stub process inspection and record signals instead of sending them.

## Clauses covered

Deterministic evidence covers C-3.2; C-4.2 to C-4.5; C-5.4 to C-5.8; C-6.1 to
C-6.5; C-7.2 to C-7.4; C-8.1 to C-8.3; C-9.2 and C-9.4 to C-9.6; C-12.1 and
C-12.8; C-15.1 and C-15.3; C-16.1; and publication-fault portions of C-20.3.
This lists the exercised portions, not blanket acceptance of every listed clause.

## Clauses not covered and why

The 28 real daemon process cases are implemented but skipped: this managed
execution sandbox refuses the contract's `ps` and `sysctl` inspection commands.
Actual guardian/client/daemon survival, SIGKILL recovery, ignored-SIGTERM
escalation, escaped-session quarantine, and salvage crash recovery therefore
remain unverified on this host. C-20.2's full process-inclusive fake timing must
also be measured when those cases can run. No restriction was bypassed.

## Seam changes

None. The fake adapter uses the existing shared contracts and adapter signatures.

## Open questions for the integrator

- Run the implemented real process tests where macOS `ps` and `sysctl` are permitted.
- Push the committed lane when DNS is available. Every requested push was attempted
  and failed with `Could not resolve host: github.com`; history was not rewritten.
