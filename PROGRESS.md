# PROGRESS — lane/invariants

Lane: disposition of the 220-row invariant ledger (`docs/reports/A-invariants.md`) against
`docs/acceptance-contract.md`. Documents only.

## State

- 2026-09-05: lane started. Read the ledger (220 rows), the acceptance contract (103 clauses,
  `C-1.1` to `C-20.5`), `docs/plan.md` (17 amendments), `docs/plan-b-rev4.md`
  ("What gets dropped", "Test strategy", "Appendix A"), and `docs/reports/G-review.md`
  "Missing invariants" (56 rows needing explicit treatment).

## Done

- (nothing yet)

## Next

1. Write the shared adjudication guide (module list, owner rules, clause index, verdict policy).
2. Adjudicate rows 1-220 in chunks of 20, each chunk verified adversarially.
3. Emit `docs/invariants.md`, `docs/invariants.json`, `tests/unit/test_invariants_index.py`.
4. Author the Gaps / Replaced / Dropped / Counts sections.

## Decisions taken

- Ledger verdict is the default. Mandated departures: rows 44, 57, 58, 116 (`keep-simplified`
  in the ledger) become `replace`; rows 125, 126, 127, 171 become `replace` per plan B; rows
  219, 220 stay `drop`.
- JSON field names are the snake_case rendering of the table column names
  (`v1_location`, `contract_clause`, `acceptance_owner`, `v2_module`, `test_name`).
