# Routing lane progress

## State
Milestone 3 implementation started on `lane/routing`. Contract and core interfaces read.

## Done
- Confirmed the routing worktree and clean starting tree.
- Read acceptance clauses C-6.3–6.4, C-9, C-10.3–10.4, C-11, and milestone 3.
- Located the minimal pick, atomic decision persistence, store evidence APIs, and `why` endpoint.

## Next
- Finish the required plan, audit, and read-only v1 references.
- Implement policy validation, capacity view, scheduler, and renderers with clause-citing tests.
- Integrate scheduling and admission probes; run routing and full test suites.
- Commit each coherent step and write the final integrator report to `OUTPUT.md`.

## Seams and assumptions
- Keep the existing Decision dataclass and schema; additional scheduling facts belong in evaluation JSON.
- No output path was supplied, so the final report will be committed as `OUTPUT.md`.
