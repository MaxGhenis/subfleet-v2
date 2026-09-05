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
- [x] Adjudicated all 92 rows covered-after-all vs uncovered. None is covered by an existing
      clause under the brief's test; all 92 carry a proposed clause.
- [x] `docs/invariant-gaps.md`, 55 proposals: safety-guard 25 rows / 14 clauses, capacity-truth
      11 / 6, ops-hygiene 15 / 9, session-continuity 14 / 7, routing-policy 3 / 3,
      provenance/attestation 5 / 4, identity 8 / 4, UX-contract 8 / 6, process-survival 3 / 2.
      Row 218 is an ops-hygiene row filed under a provenance clause, which is why those two
      class counts read 15 and 5 rather than the disposition's 16 and 4.
- [x] `## Conflicts`: eleven entries, each with both texts and a recommendation.
- [x] Row-to-clause index for all 92 rows at the end of `invariant-gaps.md`.
- [x] `docs/invariants.md` — Gaps section is now a pointer plus counts; the clause column,
      Counts tables, and the prose all updated. `docs/invariants.json` likewise; a diff against
      HEAD shows `contract_clause` as the only field that changed.
- [x] `tests/unit/test_invariants_index.py` — 25 tests (was 18). A `P-23.<n>` is legal only where
      `invariant-gaps.md` writes the clause, no row may read `GAP`, no proposal may be orphaned,
      and each proposal's `Ledger rows:` list must equal the set of rows citing it. Verified by
      re-introducing a `GAP` and an undefined `P-23.99`: six tests fail.

## Next

- [ ] Fold in the adversarial verification pass (workflow `verify-invariant-gaps`): coverage
      re-check on all 92 rows, conflict hunt, clause fidelity, code check, completeness critic.
- [ ] Final report to the lane output file.

## Notes for the integrator

`uv run pytest -q` is 764 passed, 37 skipped, 1 failed. The failure is
`tests/unit/test_cli.py::test_a_lock_whose_holder_is_dead_is_no_daemon` and is pre-existing: this
lane changed only `PROGRESS.md`, `docs/`, and `tests/unit/test_invariants_index.py`
(`git diff --name-only 0585e57 -- subfleet/` is empty). The CLI prints "there is no live process
with pid 999999" where the test expects "the machine booted at" for a boot-id mismatch.
