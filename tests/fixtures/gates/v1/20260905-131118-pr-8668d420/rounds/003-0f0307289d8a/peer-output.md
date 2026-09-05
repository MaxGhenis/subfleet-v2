---SUBFLEET-VERDICT-BEGIN---
{
  "artifact_revision": {
    "base_sha": "e16e1feb58fb97d53c65c2fe331ac04d4be14664",
    "head_sha": "b31bf822059c64a34f9f93abd3a1879cd41672f0",
    "kind": "pr",
    "number": 519,
    "repository": "TheAxiomFoundation/axiom-oracles"
  },
  "findings": [],
  "notes": [],
  "schema_version": 1,
  "summary": "Static read-only re-review of PR 519 at head b31bf822 against base e16e1feb. The checkout's worktree ref resolves to the exact head sha and its comparator.py, report.py, cli.py, and run_comparison.py contents match artifact.patch hunk-for-hunk. The diff makes comparison completeness strict: Comparator.compare requires a household-ID bijection between engine results (and with outputs_by_case when supplied), rejects duplicate mapping concepts, treats missing/both-missing values as mismatches, requires every summed component to be present, raises on non-finite inputs and differences, and rejects empty per-household comparisons; ComparisonReportAccumulator validates the whole batch (submitted-case/comparison bijection, cross-batch case repeats, engine-pair stability, exact requested-output coverage) before mutating any counter or writing case rows, and counts submitted cases rather than surviving comparisons. The CLI passes each prepared case's outputs as the requested surface and converts ValueError into a ClickException without writing a report; comparable_mappings already guarantees both-engine targets, so the new 'Unmapped requested outputs' path cannot trigger on the CLI happy path, and case outputs are trimmed to the selected concept set before comparison. All other Comparator/build_comparison_report callers were inspected (scripts/run_comparison.py gettsim runner now propagates explicit concepts into case outputs and is covered by the parametrized test; scripts/run_canada_official_comparison.py suites declare exactly the three mapped concepts; the entitledto report compares one aligned household at a time with a two-target mapping) and remain consistent with the stricter contract. New tests cover bijection failures, sum-component semantics, non-finite handling, zero-agreement empty comparisons, no-mutation-on-rejection for streaming batches, per-case scoped outputs, and the CLI error/success paths. Residual, non-actionable observations: heterogeneous suites whose case ends with no comparable declared outputs for a given engine pair now abort the run instead of contributing a vacuous case, which is the documented intent; _filter_comparisons_for_case_outputs remains defined and unit-tested but is no longer used by the CLI compare path. The packet and upstream-audit markdown files referenced by the brief were not present in the review directory, so this verdict rests on artifact.patch plus the clean checkout; the PR diff touches no saved report JSON, so the upstream timestamp-only refreshes do not interact with it. Tests were not executed here; merge remains contingent on the current-head CI completing successfully.",
  "verdict": "approve"
}
---SUBFLEET-VERDICT-END---
