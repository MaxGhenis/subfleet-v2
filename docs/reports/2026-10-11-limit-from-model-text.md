# Model text and lane limits — 2026-10-11

Draft, committed before the shared test-lock wait. Work is confined to the assigned worktree; no live state, processes, network remotes, or sub-agents are used.

Incident: between 12:30Z and 13:23Z, seven rc-0 Claude Opus 5.5 attempts quoted usage-limit language in their reports. Three jobs failed and five accounts acquired one-hour guessed closures. Claude's quota checks read `stderr + StreamSummary.texts()` before success, so the reports became provider evidence.

Planned correction: every Claude text classifier and closure clock reads only stderr and `cli_texts()`. Successful rc-0 non-error results precede text failures. C-9.8's structured `rate_limit_event status=rejected` remains authoritative, including over success; `overageStatus` remains diagnostic only. Model text erasure must preserve the adapter's class and closure, so Claude success is determined structurally; the daemon retains C-12.6's existing empty-deliverable validation.

Codex audit: agent messages and `last.md` do not enter its failure patterns; only `turn.failed`, `error`, and stderr do. Its auth check must follow verified success to prevent recovered CLI errors from disabling a successful lane.

Operator relief: `lanes release <lane> --limits --note <text>` will release active guessed provider-limit/credits closures transactionally, preserve provider-reported clocks, and record the note and affected closure ids. Existing operator-hold release stays available.

Validation pending: three reconstructed incident regressions before/after; Hypothesis mutation, real-limit-surrounding-text and erasure invariants; fixture replays; release CLI/protocol/daemon tests. Exact counts and final changes will be recorded after execution.

Git: commits use a workspace-local `.git-local` on `fix/limit-from-model-text`, preserving the detached shared checkout and existing shared branch.
