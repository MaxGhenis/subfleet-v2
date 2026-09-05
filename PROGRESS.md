# PROGRESS — lane/invariants

Lane: disposition of the 220-row invariant ledger (`docs/reports/A-invariants.md`) against
`docs/acceptance-contract.md`. Documents only.

## State

- 2026-09-05: lane started. Read the ledger (220 rows), the acceptance contract (103 clauses,
  `C-1.1` to `C-20.5`), `docs/plan.md` (17 amendments), `docs/plan-b-rev4.md`
  ("What gets dropped", "Test strategy", "Appendix A"), and `docs/reports/G-review.md`
  "Missing invariants" (56 rows needing explicit treatment).

## Done

1. Shared adjudication guide written: the 103-clause index, the closed v2 module list (23 modules
   named by a lane brief or the contract, 11 more proposed for milestones 4 to 8), the four
   acceptance-owner rules, and the binding disposition policy.
2. `tests/unit/test_invariants_index.py` written and committed: 18 tests over `docs/invariants.json`
   (220 rows, ids 1..220 unique and complete, legal dispositions and classes, replacement present
   iff `replace`, every kept or replaced row cites a clause or `GAP`, every cited clause parsed
   from the contract's `**C-` markers, the mandated dispositions, and md/json agreement).
3. Adjudication of rows 1-220 dispatched as an 11-chunk workflow, each chunk adversarially verified
   against the actual clause texts.

## Next

1. Merge the chunks, apply the verified corrections, resolve contested rows by hand.
2. Emit `docs/invariants.md` and `docs/invariants.json`; make the index test pass.
3. Author the Gaps (proposed clause text), Replaced, Dropped, and Counts sections.

## Decisions taken

- Ledger verdict is the default. Mandated departures: rows 44, 57, 58, 116 (`keep-simplified`
  in the ledger) become `replace`; rows 125, 126, 127, 171 become `replace` per plan B; rows
  219, 220 stay `drop`.
- JSON field names are the snake_case rendering of the table column names
  (`v1_location`, `contract_clause`, `acceptance_owner`, `v2_module`, `test_name`).
