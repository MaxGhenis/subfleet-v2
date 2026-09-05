# Fake acceptance progress

## State

Fake provider and socket acceptance harness are implemented. Deterministic state
tests pass. Real process tests are capability-skipped because this managed
sandbox refuses `ps` and `sysctl`; production identity checks are unchanged.

## Done

- Read the acceptance contract, shared contracts/schema/protocol/adapter seams,
  design narrative, and the specified v1 files without executing v1 commands.
- Agreed with the daemon implementation on the crash boundary hook and guardian
  receipt delay injection for deterministic recovery tests.
- Committed the fake provider and injectable adapter (4e96c47). Push failed with
  `Could not resolve host: github.com`; no guard or network restriction was bypassed.
- Added an isolated subprocess/socket harness and named admission, cancellation,
  containment, export, notice, singleton, and malformed request acceptance tests.
- Added the C-20.3 crash matrix covering reserved, starting, running, finalizing,
  terminal, notice, export, and salvage boundaries.
- Added deterministic daemon submission, cancellation, notice atomicity,
  singleton-lock, and malformed socket tests without launching providers.
- `python3 -m pytest -q tests/fake`: **16 passed, 28 skipped in 0.31s**.
- The required `uv run` command is currently blocked by `UV_FROZEN=1` and the
  missing `uv.lock`; `.venv` currently has no pytest. The available system Python
  has pytest 8.4.2 and was used for the recorded test run.

## Next

- Integrate deterministic admission/finalization tests with the control loop.
- Re-run all real process tests when an execution environment permits the
  contract's macOS process inspection commands.
- Run the fake suite within the C-20.2 60-second budget and report results.
