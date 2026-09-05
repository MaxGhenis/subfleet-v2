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
- Initial required `uv run` attempt was blocked by `UV_FROZEN=1` and missing
  `uv.lock`; the root agent then completed dependency setup from local cache.
- Added state coverage for one-slot reservation, reserved/starting/running
  recovery, cancellation ordering, acceptance-and-notice rollback, immutable
  deliverables, export replay, and ENOSPC at prompt/manifest/deliverable/export.
- Added recorded-signal tests for escalation and quarantine; fsync/rename syscall
  audit; concurrent export and stale-owner fencing; limited/transient retry and
  wall limit coverage.
- `UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run pytest -q tests/fake`:
  **39 passed, 28 skipped in 0.92s**, within C-20.2's 60-second fake budget.

## Next

- Re-run all real process tests when an execution environment permits the
  contract's macOS process inspection commands.
- Integrator reviews the committed fake acceptance suite alongside core code.
