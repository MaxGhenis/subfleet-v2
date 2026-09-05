# Lane brief: invariants (disposition of the 220-row ledger)

You are producing the invariant dispositions for subfleet v2 in the git worktree you were launched in (run `pwd`; it is a worktree of `~/subfleet-v2` on branch `lane/invariants`). subfleet v2 is a from-scratch rebuild of a tool that dispatches delegated agent work across several Claude and Codex subscription accounts. Ten weeks of incidents in v1 were harvested into 220 invariants; milestone 0 requires each one to be adjudicated before code that depends on it is accepted. This lane writes documents only.

## Read first, in this order

1. `docs/reports/A-invariants.md`: the 220-row ledger (id, invariant, enforcing `path:line` in v1, incident or rationale, class, keep verdict).
2. `docs/acceptance-contract.md`: the clauses the rebuild implements; every kept invariant must map to a clause or be flagged as a gap.
3. `docs/plan.md` and `docs/plan-b-rev4.md` sections "What gets dropped", "Appendix A", and "Test strategy": the rows plan B already replaces (125 to 127, 133, 171) and the acceptance owners (unit, fake-provider, process, live).
4. `docs/reports/G-review.md` section "Missing invariants": the rows the adversarial review said need explicit treatment.
5. v1 source, read-only, to verify an enforcing location when a row is ambiguous: `~/chief-of-staff/subfleet/`. Never modify v1, never run its commands, never run `grep -r` or `rg` over `~/chief-of-staff/state` or any broad root; read specific files at the cited `path:line`.

## Deliverables

- `docs/invariants.md`: one table, one row per ledger id, columns: `id`, `invariant` (one sentence), `v1 location`, `class`, `disposition` (`keep`, `replace`, `drop`), `replacement` (the v2 rule, only for `replace`), `contract clause` (`C-x.y`, or `GAP` when no clause covers a kept row), `acceptance owner` (`unit`, `fake`, `process`, `live`), `v2 module` (from the module list in `docs/lanes/*.md` and the contract), `test name` (a proposed pytest function name), `notes` (incident date when the ledger gives one). Below the table: a `Gaps` section listing every `GAP` row with the clause you would add; a `Replaced` section quoting the ledger row and the replacement; a `Dropped` section with the reason; and a `Counts` section by class and by disposition.
- `docs/invariants.json`: the same rows as a JSON array with the same field names, so a test can later assert coverage.
- `tests/unit/test_invariants_index.py`: loads `docs/invariants.json`, asserts 220 rows, unique ids, every disposition in the allowed set, every kept or replaced row has a clause or `GAP`, and every clause cited exists in `docs/acceptance-contract.md` (parse `**C-` markers).

## Rules of adjudication

- Default to the ledger's verdict (214 keep, 4 keep-simplified as `replace`, 2 drop). Change a verdict only with a reason in `notes`.
- A `keep` row whose enforcing mechanism was bash-runner specific (EXIT traps, `pick_lane` in the shell) maps to the contract clause that now owns the behaviour, not to a v2 copy of the shell.
- Rows about the learned-capacity estimator (125 to 127) and the keepalive window reset (171) are `replace` per plan B; quote plan B's replacement.
- Rows the G-review listed as needing explicit treatment must not be `GAP` without a proposed clause text in the Gaps section.
- Do not invent invariants. Every row comes from the ledger.

## Tooling and git

```
export UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --group dev && uv run pytest -q tests/unit/test_invariants_index.py
```

Commit after every 40 rows or so with a message naming the range, and push after every commit: `git push -u origin lane/invariants`. Never commit to `main`, never force-push. If a guard or hook refuses a command, do not work around it; record it in the final message.

## Final message

Your final message is captured for the integrator. Use these headings: Delivered (files); Counts (by disposition and class); Gaps (the list with proposed clause text); Verdict changes (rows where you departed from the ledger and why); Open questions for the integrator. No preamble.
