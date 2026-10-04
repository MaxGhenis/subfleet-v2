---SUBFLEET-VERDICT-BEGIN---
{
  "artifact_revision": {
    "bytes": 27462,
    "kind": "plan",
    "sha256": "87344c52906560076f45e066f048145276035eb6a39b1c50e7d2d7bd9dd3767b"
  },
  "findings": [
    {
      "description": "The unresolved-gate rule makes most of the Public Interface view dateless in every scenario, and the plan neither states that expected outcome nor tests it. Fifteen of the 39 non-Axiom family items transitively depend on an unresolved ax-* gate (mc-20 -> mc-50 -> mc-every -> pe-utility -> pe-default; pe-instances -> th-calibration -> th-observatory; th-uplift; bf-safety -> bf-finbot -> bf-85; co-form -> co-products -> co-commons; see public-interface.yaml predecessors), so 32 of 56 family items will export as blocked on a source gap. That is the honest result, but the plan must (a) state this expected blocked set explicitly, (b) define how the 'explicitly labeled source-plan comparison' (line 62) treats edges into unresolved gates, e.g. an assumed-at-source-target mode that is clearly labeled and never shown as feasible, and (c) add an acceptance case that pins the blocked set so a degenerate family view is not silently accepted and the family stress scenarios (no-program-leads, axiom-slips-six) still have observable effect.",
      "location": "artifact.snapshot:58,62,111 (Migration mapping paragraph; source-plan comparison; Revision 2 gate claims)",
      "severity": "medium"
    },
    {
      "description": "The merged priority order between Axiom and family work is unspecified. The existing engine orders items by plan.circles index, then priority, earliest, id (plangraph/src/schedule.ts:142-149); the Axiom plan uses circles finbot/us-tax-benefit/org/elsewhere (plan-data.ts:448) while the family uses spine/family/steward/corollary (public-interface.yaml:29-33). After composition these items compete for the same ceo, president, dop, MTS and pe-team resources, and whichever ordering the adapter picks decides which program's work is starved. Line 42 says 'sorted by explicit priority' but no section states how the two circle schemes and the existing per-item PRIORITY overrides (plan-data.ts:286) map to one explicit priority. Specify the merged order with a reason and add a cross-program contention fixture to the acceptance cases; the current 'priority among ready siblings' case tests only engine mechanics.",
      "location": "artifact.snapshot:42,56-58,85 (Scheduling heuristic; Compose before scheduling; priority acceptance case)",
      "severity": "medium"
    },
    {
      "description": "Dependent ongoing work (mode 5) does not say whether its demands must fit atomically from start to horizon, as duration work does ('shifted as a unit until all execution demands fit'), or are reserved month by month with shortfall findings. Twelve Axiom standing items (c5, n5, g4, cp3, f3, x3, x11, hh2, en1, gv4, ai3, z3; roadmap-phases end 2033.0) share the 17 MTS instances with finite work; under atomic fit any later over-subscribed month pushes their start indefinitely, under month-by-month reservation they can starve later finite items in serial order. State the rule, how partial monthly shortfall on ongoing work is reported, and add it to the standing-work acceptance fixture.",
      "location": "artifact.snapshot:32,128 (Duration mode; Revision 2 fifth mode)",
      "severity": "medium"
    },
    {
      "description": "The dashboard cannot import `plangraph/portfolio` under its current source aliases. vite.config.ts:5,12 aliases the bare key `plangraph` to node_modules/plangraph/src/index.ts and tsconfig.json:10 maps only `plangraph` to that file, so a subpath import resolves to `.../src/index.ts/portfolio` in Vite and to the dist `exports` map in tsc, which the packed tarball only satisfies after a build. The consumption section must specify the alias/paths change (explicit `plangraph/portfolio` entries pointing at src during development, or dropping the src aliases in favor of the packed dist exports) and include it in the typecheck/build and packed-package checks.",
      "location": "artifact.snapshot:154 (Checkout and package consumption)",
      "severity": "low"
    },
    {
      "description": "Half-up rounding is specified on values that are not exactly representable in binary floating point. Loaded annual rates carry up to four decimal places (Medicare 0.0145 in roster-state.ts:37) and economics-fixtures.md forbids rounding the annual rate before dividing by twelve, so exact half-cent ties can misround in IEEE arithmetic. Require exact integer or rational arithmetic for the cents boundary (e.g. carry annual amounts in integer 1/10,000-dollar units and compute monthly cents as floor((units + 600) / 1200)) and for the fixed-total formula halfUp(T*(k+1)/N), so the fingerprint is stable and the fixture vectors are provably reproduced rather than incidentally.",
      "location": "artifact.snapshot:148 (Funding migration and exact cents, rounding sentence)",
      "severity": "low"
    },
    {
      "description": "The resource mapping row for `axiom-lead (2)` conflicts with its source. public-interface.yaml:49 describes the two seats as 'president; rules lead', while the plan maps them to the President and the Director of Product. The DoP already owns x1, x2, rp1, rp2 and contributes on roughly ten other detailed Axiom items (staffing-map.ts:688-725), and ascending-ID first-fit selects `dop` before `president`, so family axiom-lead demands land on the most loaded principal. Record the source label conflict and the chosen interpretation (and the alternative of mapping the second seat to the encoding lead `enc`) in the mapping artifact with a reason.",
      "location": "artifact.snapshot:118 (Resource mapping table, axiom-lead row)",
      "severity": "low"
    },
    {
      "description": "The Revision 2 additions introduce scenario-level behaviors without acceptance coverage: the 0.5 FTE per-executive stress reservation, stale-planned-hire findings with availability clamped to as-of while the original date is retained, and the proposed-versus-committed coverage status split. Add one fixture each so these are verified outputs rather than described intentions.",
      "location": "artifact.snapshot:107,124,146,156 (executive envelope; stale planned hires; coverage status; additional required tests)",
      "severity": "low"
    }
  ],
  "notes": [
    "All eleven round-1 findings are addressed in Revision 2 with concrete rules; none remains open at high severity. Verified against source: 56 family items in public-interface.yaml (17 ax-* plus 39 others); 17 pooled MTS instances from staffing-map.ts starts; pe-migrate note cites rp1; z0 is the only CEO/President-owned detailed item; PLAN_FUNDING comprises PRE_AWARD, CASH_PROPOSALS lines, partner-funded/parent-shared seat lines, reference lines and PLAN_STREAMS, all of which the funding classification covers.",
    "Recomputed economics-fixtures.md independently from roster-state.ts, pay.ts and ops-model.ts constants: the $300k loaded rates (3,207,075 and 3,333,888 cents), Fixture A/B totals, founders' pay-in-place vectors, the 162,500/179,167-cent overhead months, and the 18-month cumulative-rounding marketing vector all check out.",
    "Both prepared worktrees (_worktrees/plangraph-portfolio and _worktrees/roadmap-portfolio) exist with installed dependencies; commit hashes and branch state could not be verified without git access. corpus-release-manifest-inspection.json exists and matches the 20-country cut-plan description.",
    "prior-research.md, oss-scheduling.md, grantkit-boundary.md and ../plangraph-20260905/review.md were not re-read this round; the reuse and GrantKit sections are unchanged from the revision the previous peer assessed, and no finding here depends on them."
  ],
  "schema_version": 1,
  "summary": "Revision 2 resolves the round-1 blockers: the executive envelope, all-56-item work audit, resource mapping, dependent ongoing mode, deterministic assignment, as-of/actuals rules, funding classification, exact-cents rounding and the vendored-package path are now specified and largely verified against source. Remaining gaps are narrower but still change results or buildability: the plan does not state that 32 of 56 family items will be blocked on unresolved gates or how the labeled source-plan comparison handles them, the merged Axiom/family priority order for shared people is unspecified, ongoing-work fit semantics are ambiguous, the dashboard's src aliases will not resolve plangraph/portfolio, rounding needs exact arithmetic, the axiom-lead mapping conflicts with its source label, and three new behaviors lack acceptance fixtures. Changes requested; all are specification edits, no runtime rework.",
  "verdict": "changes_requested"
}
---SUBFLEET-VERDICT-END---
