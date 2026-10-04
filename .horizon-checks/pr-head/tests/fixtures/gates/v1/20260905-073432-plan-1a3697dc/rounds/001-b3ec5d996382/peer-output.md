---SUBFLEET-VERDICT-BEGIN---
{
  "artifact_revision": {
    "bytes": 16349,
    "kind": "plan",
    "sha256": "268a2a89442a6b548d8f42096bb88312aa0637008ee5e84eaf4f00cfd67e6ff7"
  },
  "findings": [
    {
      "description": "Executive capacity conflict is unresolved. Axiom's z0 pins the CEO and President at 1.0 FTE standing on nothing else (staffing-map.ts SEATS ceo/president; roadmap-phases z0), while the family plan assigns the CEO 0.1-0.3 FTE on roughly twenty items and makes the CEO the owner of pi-form, pi-governance, pi-transfer, co-counsel, co-form, co-products (public-interface.yaml). Under 'respect every execution demand, including bounded founders' with no CEO fallback and z0 as a fixed reservation, every CEO-owned Public Interface and Corollary item becomes permanently infeasible or a fixed overload, so the family output degenerates. The plan must state the reconciliation rule: carve program-tagged executive reservations out of z0, fold family executive demands into z0 as milestones, or declare z0 movable/bounded below 1.0.",
      "location": "artifact.snapshot:31,42,56-58 (Contract work modes; Scheduling; 'Compose before scheduling')",
      "severity": "high"
    },
    {
      "description": "The double-booking guard only covers family nodes in the Axiom lane. Non-Axiom-lane family items carry Axiom engineering demands and duplicate detailed Axiom work: pe-migrate (notes.yaml cites brief rp1/hh1; 0.5 FTE axiom-team), bf-spec/bf-safety/bf-finbot/bf-85 (overlap f1/f2/fb1/hh2; up to 1.5 FTE axiom-team for 15 months), th-uplift and pe-instances (axiom-team demands). 'Include every non-Axiom family item' as written re-books MTS capacity the detailed Axiom roadmap already consumes. Extend the mapping table and the milestone/unresolved rule to every family item that demands Axiom resources or cites an Axiom brief item, not just lane=axiom nodes.",
      "location": "artifact.snapshot:58 (Migration, mapping table paragraph)",
      "severity": "high"
    },
    {
      "description": "No scheduling mode fits dependent open-ended standing work. Axiom items c5, n5, g4, cp3, f3, x3, x11, hh2, en1, gv4, ai3, z3 have predecessors and run to the horizon (roadmap-phases end 2033.0; PREDECESSORS in plan-data.ts). Mode 1 (fixed reservation) would ignore their dependencies; modes 2-3 require a finite duration or effort. Specify an open-ended dependent mode (or the exact conversion, e.g. movable duration = horizon - start) and how its always-partial status is reported and exported.",
      "location": "artifact.snapshot:29-34 (Contract, scheduling modes)",
      "severity": "medium"
    },
    {
      "description": "Assignment of a demand across multiple eligible resources is unspecified (split evenly, first-fit by ID, least-loaded, etc.). With coverage restricted to explicit resource targets, the carrier chosen changes funder coverage and rate-gap attribution, and determinism/'input order does not affect results' requires a documented rule plus an acceptance case that pins it.",
      "location": "artifact.snapshot:27,42,50 (resource/demand contract; heuristic; coverage)",
      "severity": "medium"
    },
    {
      "description": "Remaining-work rule at as-of is undefined for in-progress items: whether actual bookings reduce remaining effort/duration, whether legacy pinned months before as-of with no evidence count as consumed capacity or as zero, and how a started item with no monthly bookings is resumed. Validation also omits rejecting actual bookings/completions dated on or after as-of and bookings outside a resource's employment window; these belong in the failing-validation acceptance case.",
      "location": "artifact.snapshot:36,83 (Contract actuals paragraph; acceptance validation case)",
      "severity": "medium"
    },
    {
      "description": "'Employee already in place as of the projection' is not defined against planned hires whose planned start has passed without roster evidence (e.g. PBIF-funded October 2026 seats once as-of moves past 2026-10). The 'expired forecast cannot become actual' rule implies they are neither in place nor actual, yet scenario drops/delays on them are neither rejected nor flagged. Define in-place by evidence, and emit a stale-planned-hire finding so scenario semantics do not silently drift as as-of advances.",
      "location": "artifact.snapshot:36-38 (Contract, actuals and scenarios)",
      "severity": "medium"
    },
    {
      "description": "Funding migration classification is incomplete. Existing FundingLines in plan-data.ts include the pre-award bridge (sized exactly to founder costs, i.e. self-balancing), proposal asks spread evenly over terms, the lazily computed Ballmer residual, partner-funded/parent-shared external seat offsets, and reference lines; only Ballmer and quotes are classified. Each must be assigned to expense/coverage/commitment/receipt or dropped with a reconciliation note. Founder pay step-up at PAY_IN_PLACE_UNTIL=2026-10 is an assumption tied to award timing, not an actual founder-pay schedule; label it and decide whether a receipt-delay scenario may move it.",
      "location": "artifact.snapshot:48-52 (Monthly economics)",
      "severity": "medium"
    },
    {
      "description": "The rounding rule is under-specified for the acceptance fixture. Annual loaded cost / 12 does not divide to whole cents; state the remainder allocation (largest-remainder or last-month) or the accepted <=12 cents/year drift, the rounding mode, and that rounding occurs once per resource-month before summation. The midyear hire/exit fixture must also define how annual payroll-tax caps (SS wage base in priceSalaried) are prorated, since priceSalaried caps on an annualized base.",
      "location": "artifact.snapshot:50,89 (rounding sentence; monthly fixture acceptance case)",
      "severity": "medium"
    },
    {
      "description": "Delivery step 2 assumes fresh branches, but `portfolio-projection` already exists in both repos (dashboard b29b4c2, diverged from main ce781af; plangraph at 22eee68) and the dashboard has a `ledger-seam` branch (db439cc) that likely overlaps the single-ledger seam. The plan must inspect, reuse or rename these rather than assume a clean slate. The dashboard also consumes plangraph as `github:...#22eee68` with tsconfig/vite aliases into node_modules/plangraph/src, so a new `plangraph/portfolio` export cannot be consumed locally without either an external push or a file:/link dependency; specify the local consumption path so step 5's packed-package check is runnable within a local-only build.",
      "location": "artifact.snapshot:75,78 (Delivery sequence steps 2 and 5)",
      "severity": "medium"
    },
    {
      "description": "Resource mapping is unspecified while node mapping is required. Family seats (axiom-lead x2 -> president/dop?, axiom-team 21 hires vs the 17 MTS instances plus non-MTS seats, pe-team x6, program leads, steward-ops) need an explicit resource mapping table with assumed/actual status and the stated reason for the 21-vs-17 difference.",
      "location": "artifact.snapshot:56 (Migration, roster expansion)",
      "severity": "low"
    },
    {
      "description": "Acceptance cases omit: as-of outside the horizon; scenario overrides on a resource that has exited; effort work whose eligible resources all exit before completion (partial then unstaffed with reason); a milestone with no dependencies; JSON and YAML inputs yielding an identical projection hash; and a golden projection hash proving UI, CLI and export agreement rather than asserting it.",
      "location": "artifact.snapshot:81-94 (Required acceptance cases)",
      "severity": "low"
    }
  ],
  "notes": [
    "Companion files prior-research.md, oss-scheduling.md, grantkit-boundary.md and ../plangraph-20260905/review.md were not present in the review directory and could not be located on disk; the OSS reuse decision was assessed from the plan text only and appears reasonable (no engine adoption, solver deferred behind fixtures).",
    "Verified against source: 17 pooled MTS instances in staffing-map.ts; 21 axiom-team hires and two family stress scenarios (axiom-slips-six, no-program-leads) in family-data.ts; plangraph main 5007eca is an ancestor claim consistent with review-fixes 22eee68 being the dashboard pin.",
    "Optional, not blocking: solver adoption, daily calendars and stochastic forecasts are correctly scoped out; the September-4 architecture review's lane-1 ledger seam is subsumed by the single-projection requirement."
  ],
  "schema_version": 1,
  "summary": "Scope is coherent and the boundaries (engine vs adapter, coverage vs cash, GrantKit as quote reference) are correct, but the plan is not yet buildable as specified: executive time collides between Axiom's 1.0 FTE z0 reservation and the family's CEO-owned work, the double-booking guard misses non-Axiom-lane items that consume Axiom capacity, dependent standing work has no mode, and carrier assignment, as-of remaining-work, stale planned hires, funding-line classification, rounding, and pre-existing branches are unspecified. Changes requested before implementation.",
  "verdict": "changes_requested"
}
---SUBFLEET-VERDICT-END---
