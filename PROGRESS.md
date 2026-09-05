# Core lane progress

## State
Building milestone 1 core on `lane/core`, against `docs/acceptance-contract.md`.
Shared seams remain unchanged. Runtime dependencies are standard library only.
Implementation and review are complete for integration. Deterministic acceptance
coverage passes. Physical process/crash acceptance remains unverified because this
sandbox prohibits the required OS inspection commands.

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
- Final exact required command: **119 passed, 33 skipped in 3.33 s**.
- Audited 111 test functions: all contain clause citations. `git diff --check` passed.
- Wrote the final integrator report to `OUTPUT.md`.

## Next
- Integrator: run the 33 physical tests where `ps` and `sysctl` are permitted.
- Integrator: push `lane/core` once GitHub DNS/network access is available.
- Later lanes: full routing, provider/CLI integration, live guard/isolation validation,
  and enforcement of the optional observed-token cap when usage evidence is available.

## Validation and limitations
```sh
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q
```

119 passed, 33 skipped in 3.33 s. The skips are 29 real-daemon cases and four
guardian/process cases; these are implemented, but their physical acceptance claims
are not established by deterministic fixture tests. Full routing and provider
adapters belong to other lanes. No live provider/account commands were run.

Pushes fail: DNS cannot resolve github.com. Initial `uv sync --group dev` failed
because inherited UV_FROZEN=1 requires a missing lockfile; retry with UV_FROZEN=false
failed resolving pypi.org. Dependency setup was completed using read-only copies of
the user's cached distributions into `.uv-cache`; the required uv workflow now works.
The sandbox denies `ps` and `sysctl kern.boottime`: production inspection remains
fail-closed; real process acceptance tests must report capability skips here.
