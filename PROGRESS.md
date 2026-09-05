# Lane `gaps` — progress

Brief: `docs/lanes/gaps.md`. Close the `contract clause` column for the 92 `GAP` rows of
`docs/invariants.md` / `docs/invariants.json`, either by mapping a row to an existing clause of
`docs/acceptance-contract.md` or by drafting a proposed clause `P-23.<n>` in
`docs/invariant-gaps.md`. One test change; no runtime code.

## State

Reading done: the whole contract (C-1.1 to C-22), `docs/plan.md` amendments 1 to 17,
`docs/invariants.md` (table, Replaced, Dropped, Counts), all 92 `GAP` rows of
`docs/invariants.json` against their ledger text in `docs/reports/A-invariants.md`, and the v1
`path:line` for every row whose clause has to name an enumeration (29, 31, 45, 46, 65, 170, 208,
209, 210). `subfleet/` on this branch read for the rows milestones 1 to 3 already implement
(rows 50, 53, 218 implemented; row 52's cache deliberately absent; row 89 not implemented).

Baseline: `uv run pytest -q tests/unit/test_invariants_index.py` — 18 passed.

## Done

- [x] Read the contract, the plan amendments, the disposition, and the ledger.
- [x] Adjudicated all 92 rows covered-after-all vs uncovered, and grouped the uncovered ones
      into proposed clauses.

## Next

- [ ] `docs/invariant-gaps.md`: safety-guard (25 rows).
- [ ] capacity-truth (11), ops-hygiene (16), session-continuity (14), routing-policy (3),
      provenance/attestation (4), identity (8), UX-contract (8), process-survival (3).
- [ ] `## Conflicts`.
- [ ] `docs/invariants.md` Gaps section → pointer plus counts; clause column updated.
- [ ] `docs/invariants.json` clause column updated for all 92 rows.
- [ ] `tests/unit/test_invariants_index.py` accepts `P-23.<n>` only when defined and fails on any
      remaining `GAP`.
- [ ] Adversarial verification pass over every mapping and proposal.
