# Core lane progress

## State
Building milestone 1 core on `lane/core`, against `docs/acceptance-contract.md`.
Shared seams remain unchanged. Runtime dependencies are standard library only.
Implementation is integrated; deterministic acceptance coverage passes. Real
process/crash verification remains blocked by this sandbox's OS inspection policy.

## Done
- Read the acceptance contract, shared seams, plan, and specified v1 references.
- Confirmed the clean `lane/core` worktree and existing console entry points.
- Added daemon submission validation and digest deduplication, transactional cancellation,
  notices, socket request dispatch, singleton ownership and lifecycle scaffolding.
- Parallel work has produced storage/identifier foundations and the fake provider.
- Integrated atomic admission, guarded guardian launch, all attempt recovery states,
  identity-checked cancellation, retained quarantine evidence, salvage checkpoints,
  immutable artifacts, terminal-plus-notice acceptance and serialized export replay.
- Added per-job wall deadlines, transient/limit/lost retry handling, and hourly retention.
- Created `uv.lock` from existing cached distributions and installed the project offline.
- Review fixes now fence concurrent/stale exports, preserve first transient retries on
  their lane, enforce waiting-job wall limits, record owned process identities, and
  salvage a verified-dead quarantine before release.
- Consumed the Codex lane's existing preflight API without editing shared seams or
  guard files. Added package-root guardian launch and environment-only credential tests.
- Finalization freezes classification/attestation before acceptance and rejects stale
  attempt publication; regression tests cover replay and disk-full failures.
- Retention now accounts for and safely removes selected allocated worktrees under a
  durable removal lease. Fake scenarios survive prompt preambles and checkpoint suffixes.

## Next
- Run the final exact required command and verify all test docstrings cite clauses.
- Write final report to `OUTPUT.md`; commit and attempt the required push.

## Validation and limitations
`UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run pytest -q`:
86 passed, 32 skipped in 1.79 s at lifecycle integration. Full routing and provider
adapters belong to other lanes.

Pushes fail: DNS cannot resolve github.com. Initial `uv sync --group dev` failed
because inherited UV_FROZEN=1 requires a missing lockfile; retry with UV_FROZEN=false
failed resolving pypi.org. Dependency setup was completed using read-only copies of
the user's cached distributions into `.uv-cache`; the required uv workflow now works.
The sandbox denies `ps` and `sysctl kern.boottime`: production inspection remains
fail-closed; real process acceptance tests must report capability skips here.
