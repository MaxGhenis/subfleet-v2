# D-SH1: reviews that require command execution

Built from release/217 at `3f50142ac1cc` for the microcosm-dynamics coordinator's 2026-10-14 report, measured by the hub: 466 successful read-only Claude reviews during October 7–14, 314 prompts asking for commands, 407 deliverables reporting no shell, and two Track B REQUEST_CHANGES verdicts caused solely by inability to run tests.

## Design as built

A submit carries tri-state `needs_shell`: `--needs-shell` and `--no-needs-shell` (or boolean batch `needs_shell`) override policy. Omission resolves the task's optional `requires` list. The loader accepts only known task names and the distinct requirement `shell`, rejecting unknown requirements and malformed lists. The shipped policy defaults reviews to requiring shell and includes Codex `sol61` and `luna`. Live policy installation remains the hub's responsibility after telling Fleet ops; no live state was accessed.

The daemon persists the resolved requirement on the job; native resumes inherit it unless explicitly overridden. Changes to an explicit requirement affect the idempotency digest; old submissions without a requirement retain their old digest. The candidate judge removes launches without command execution before ranking and at reservation recheck. Codex offers shell in both supported sandboxes; Claude offers it only in workspace-write. A filtered chain still tries later capable models in order. Exhausted candidates produce a capacity wait whose `why` explains the requirement, never an incompatible fallback. Incompatible explicit model or lane pins fail before job insertion with exit 7 and the fixes `-s workspace-write` or a Codex model. Competing demand excludes incompatible models too.

Each new attempt records boolean `has_shell` at reservation and confirms it when constructing its launch. Both adapters include the capability in launch notes. `runs show` and its JSON, the completion notice's first line, and CLI wait summaries expose it. Notice state/rc/path fields remain intact; deliverable and `-o` bytes remain intact. Schema version 8 adds job `needs_shell` (false for old jobs) and nullable attempt `has_shell` (unknown for old attempts) without guessing historical launches.

The CLI's known model names include `sol61` and `luna` and appear in help. They do not constrain parsing: the daemon's live model map determines admissibility, so custom policy names work too. Unknown submit pins fail with exit 7 listing policy models. Retired `sol` retains its mapping to `astra` and its stderr note. Batch models follow the same authority.

Existing contract clauses amended: C-11.1 (policy and requirements), C-11.2 (candidate filtering, waits and pins), C-12.4 (Claude tools and attempt evidence), C-15.1 (notice suffix), and C-17.2 (flags, batch key and model pins). The version history cites the report and measurements. No new clause number was needed.

## Invariants and tests

- A needs-shell job can reserve only a shell-capable launch or wait. A full Codex lane and hard review chain `[sol61, opus]` leave the review waiting; freeing Codex places it on sol61.
- Explicit opt-out allows read-only Opus and reports no shell. Workspace-write Opus reports a shell. Both preserve deliverable and export bytes.
- Invalid pins and malformed/unknown requirements are refused before state insertion. Resolved requirements survive policy edits; boolean overrides are validated and participate in idempotency.
- Bounded Hypothesis checks cover four-tier chains, lane sets, exclusions, sandbox and requirement choices (80 examples per property, no database). Without the requirement, the complete decision equals frozen base `prepare`, `judge_lane`, `model_reason` and `evaluate` functions from `3f50142ac1cc`.
- A schema migration regression preserves version-7 rows and leaves historical shell evidence unknown. The shipped-model parser regression prevents CLI/policy drift.
- Named mutation regressions: `test_hard_review_waits_for_codex_then_runs_with_shell` must detect removal of the candidate filter; `test_d_sh1_launch_notes_report_actual_shell` must detect Claude read-only defaulting to shell.

## Verification

Pending: the foreground baseline pytest command has not produced collection output under the shared lock. Implementation has been prepared and committed using a workspace-local staging tree, while the checkout's base source remains intact for this run. Required suites will run serially with their own holds, at most four workers, temporary Hypothesis storage and the ci profile. No deep profile or repository `.hypothesis` directory is used.

## Commits and remaining work

- `e7bb5fb15`: failing regressions and frozen base routing functions (tests first).
- `3ef714a79`: implementation, contract and supporting compatibility tests.
- `205ff2fff`: preserve positional protocol argument compatibility.
- `8eb36a490`: keep the source requirement on native resumes; read notice sandbox from the authoritative job row.

The sandbox refused the shared index. Commits therefore use workspace-local `.git-local` metadata on `fix/review-needs-shell-217`, preserving the base ancestry. No push, history rewrite, live policy edit or caller checkout write occurred.

Remaining: finish the baseline run, publish staged source into the checkout, run required suites and mutations, and replace this verification section with counts.
