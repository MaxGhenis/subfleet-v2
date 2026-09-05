# subfleet v2

One supervised daemon that dispatches delegated agent work across several Claude and Codex subscription accounts, keeps every run as a durable row it owns, and tells the truth about capacity.

This tree is the from-scratch rebuild. The v1 tree at `~/chief-of-staff/subfleet` keeps running until cutover.

- Plan of record: `docs/plan.md` (plan B revision 4 as amended by the grafts from plan A and the round-2 reviews).
- Binding contract for implementation: `docs/acceptance-contract.md`. Every module is built against it; every acceptance test names the clause it proves.
- Inputs: `docs/plan-a.md`, `docs/plan-b-rev4.md`, `docs/comparison.md`, `docs/reports/`.

Native exact-revision agreement gates: [usage and integration notes](docs/gates.md).

Python 3.12+, standard library only. `uv sync --group dev && uv run pytest`.
