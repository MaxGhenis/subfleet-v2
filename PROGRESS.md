# Core lane progress

## State
Building milestone 1 core on `lane/core`, against `docs/acceptance-contract.md`.
Shared seams remain unchanged. Runtime dependencies are standard library only.

## Done
- Read the acceptance contract, shared seams, plan, and specified v1 references.
- Confirmed the clean `lane/core` worktree and existing console entry points.
- Added daemon submission validation and digest deduplication, transactional cancellation,
  notices, socket request dispatch, singleton ownership and lifecycle scaffolding.
- Parallel work has produced storage/identifier foundations and the fake provider.

## Next
- Implement and test SQLite CRUD, identities, policy, credentials and retention.
- Implement and test guardian, containment and salvage independently.
- Build daemon admission, cancellation, finalization, socket API and recovery.
- Run named acceptance and crash tests within C-20.2 budgets.
- Commit each coherent step and push; write final report to `OUTPUT.md` unless another path is supplied.

## Validation and limitations
Daemon scaffold passes `python3 -m py_compile subfleet/daemon.py`; lifecycle workers
are next. Full routing and provider adapters belong to other lanes.

Pushes fail: DNS cannot resolve github.com. Initial `uv sync --group dev` failed
because inherited UV_FROZEN=1 requires a missing lockfile; retry with UV_FROZEN=false
failed resolving pypi.org. System Python has pytest 8.4.2 available for offline checks.
The sandbox denies `ps` and `sysctl kern.boottime`: production inspection remains
fail-closed; real process acceptance tests must report capability skips here.
