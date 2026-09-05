# Core lane progress

## State
Building milestone 1 core on `lane/core`, against `docs/acceptance-contract.md`.
Shared seams remain unchanged. Runtime dependencies are standard library only.

## Done
- Read the acceptance contract, shared seams, plan, and specified v1 references.
- Confirmed the clean `lane/core` worktree and existing console entry points.

## Next
- Implement and test SQLite CRUD, identities, policy, credentials and retention.
- Implement and test guardian, containment and salvage independently.
- Build daemon admission, cancellation, finalization, socket API and recovery.
- Run named acceptance and crash tests within C-20.2 budgets.
- Commit each coherent step and push; write final report to `OUTPUT.md` unless another path is supplied.

## Validation and limitations
No tests run yet. Full routing and provider adapters belong to other lanes.
