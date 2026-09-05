# Codex adapter lane progress

## State

Implementation starting on `lane/codex-adapter`. Contract and shared adapter interfaces read. Runtime code will use the standard library and will never launch provider processes from the adapter.

## Done

- Confirmed the worktree and clean lane branch.
- Read the acceptance contract, shared dataclasses, adapter interface, captured CLI help, and v1 reference files (read-only).
- Identified lane scope: Codex adapter, pinned guard/preflight, redacted fixtures, fake CLI, and focused unit/process tests.

## Next

- Port adapter enrollment, usage probe, launches, classifier, attestation, and deliverable handling (C-9, C-10, C-12).
- Copy and pin the guard and test trust prerequisites (C-14).
- Build and record fixture provenance, implement the fake provider, and verify isolation.
- Run the specified acceptance suite within C-20.2 budgets; write the final report to `OUTPUT.md` unless an output path is supplied.
- Commit every coherent step and push each commit to `origin lane/codex-adapter`.

## Constraints and integration notes

- No v1 commands or provider executions; only `codex --version` may run.
- Shared store/daemon/guardian/process/CLI/Claude/registry modules are out of scope.
- The raw exit code and signal can be retained in `Outcome.evidence` without changing shared dataclasses.
