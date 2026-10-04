# Lane brief: gaps (clause proposals for the 92 uncovered invariants)

You are completing milestone 0 of subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/gaps`). The invariant disposition (`docs/invariants.md`, `docs/invariants.json`) adjudicated all 220 rows of the v1 ledger; 92 kept or replaced rows have `GAP` in the `contract clause` column because no clause of `docs/acceptance-contract.md` covers them. Your job is to close that column: first by finding the clause that does cover the row where one exists, then by drafting proposed clause text for the rest. This lane writes documents and one test; it writes no runtime code.

## Read first, in this order

1. `docs/acceptance-contract.md`, all of it. You must know every clause before deciding a row is uncovered.
2. `docs/invariants.md` (the table, the Gaps placeholder, Replaced, Dropped, Counts) and `docs/invariants.json`.
3. `docs/reports/A-invariants.md` for the full text and the enforcing location of any row you need to understand.
4. `docs/plan.md` amendments 2 to 8 and 11 to 13 (rules adopted from plan A that the contract states tersely).
5. `subfleet/` on this branch, read-only, to check whether the code already implements a row (for example a rule the core lane enforced without a clause). Never run `grep -r` over `~/chief-of-staff/state` or any broad root; the v1 tree at `~/chief-of-staff/subfleet` is read-only for confirming a cited `path:line`.

## Deliverables

- For every `GAP` row, one of two outcomes:
  - **Covered after all:** the row maps to an existing clause. Set `contract clause` to that clause and add a one-line `notes` entry saying why it covers the row. Be strict: a clause covers a row only when a test of the clause would fail if the invariant were violated.
  - **Uncovered:** draft a proposed clause. Proposed clauses are numbered `P-23.<n>` in a new file `docs/invariant-gaps.md`, grouped by the ledger class (safety-guard, capacity-truth, ops-hygiene, session-continuity, routing-policy, provenance/attestation, identity, UX-contract, process-survival). Each proposal has: the clause text in the contract's voice (one to three sentences, present tense, no rationale inside the clause), the ledger rows it covers (one clause may cover several rows), the acceptance owner, the v2 module, and a one-line rationale quoting the ledger row's incident. Set the row's `contract clause` to `P-23.<n>`.
- Update `docs/invariants.md` and `docs/invariants.json` consistently. The `Gaps` section in `docs/invariants.md` becomes a two-line pointer to `docs/invariant-gaps.md` plus the count of rows resolved each way.
- Extend `tests/unit/test_invariants_index.py` so it accepts `P-23.<n>` clauses only when `docs/invariant-gaps.md` defines them, and fails if any `GAP` remains.
- Where a proposed clause conflicts with an existing clause or a plan amendment, do not paper over it: list it under `## Conflicts` in `docs/invariant-gaps.md` with both texts and your recommendation. Where a row's invariant is already enforced by code on this branch without a clause, say so in the rationale; the clause still gets written, because code without a clause is not accepted.

## Rules

- No invented invariants; every proposal cites its ledger rows.
- Do not renumber or edit existing contract clauses. Proposals live in the new file until the integrator folds them into the contract as section 23.
- Prefer one clause per behaviour over one clause per row; 92 rows should become far fewer clauses.
- Rows about the sessions kit (tickle, muster, revive, mirror, handoff) and gates are milestone 6 and 7 work; still draft their clauses, marked `milestone: 6` or `7`.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q tests/unit/test_invariants_index.py
```

Commit after each class is resolved with a message naming the class and the row count, and push after every commit: `git push -u origin lane/gaps`. Never commit to `main`, never force-push. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Resolved as covered (count, and the rows with the clause each maps to); Proposed clauses (count, by class); Conflicts; Rows you could not resolve and why; Open questions for the integrator. No preamble.
