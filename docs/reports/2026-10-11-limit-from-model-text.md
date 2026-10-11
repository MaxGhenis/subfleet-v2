# Model text and lane limits — 2026-10-11

Draft, committed before the shared test-lock wait. Work is confined to the assigned worktree; no live state, processes, network remotes, or sub-agents are used.

Incident: between 12:30Z and 13:23Z, seven rc-0 Claude Opus 5.5 attempts quoted usage-limit language in their reports. Three jobs failed and five accounts acquired one-hour guessed closures. Claude's quota checks read `stderr + StreamSummary.texts()` before success, so the reports became provider evidence.

Implemented correction: every Claude text classifier and closure clock reads only stderr and `cli_texts()`. Successful rc-0 non-error results precede text failures. C-9.8's structured `rate_limit_event status=rejected` remains authoritative, including over success; `overageStatus` remains diagnostic only. Model text erasure must preserve the adapter's class and closure, so Claude success is determined structurally; the daemon retains C-12.6's existing empty-deliverable validation.

Codex audit: agent messages and `last.md` do not enter its failure patterns; only `turn.failed`, `error`, and stderr do. Its auth check must follow verified success to prevent recovered CLI errors from disabling a successful lane.

Operator relief: `lanes release <lane> --limits --note <text>` releases active guessed provider-limit/credits closures transactionally, preserve provider-reported clocks, and record the note and affected closure ids. Existing operator-hold release stays available.

Validation pending: three reconstructed incident regressions before/after; Hypothesis mutation, real-limit-surrounding-text and erasure invariants; fixture replays; release CLI/protocol/daemon tests. Exact counts and final changes will be recorded after execution.

Git: commits use a workspace-local `.git-local` on `fix/limit-from-model-text`, preserving the detached shared checkout and existing shared branch.

Implementation checkpoint: all text signatures and clocks are bounded to CLI/provider evidence; structured rejection still overrides success. Codex additionally uses completion/agent-message structure before recovered auth diagnostics. Empty deliverables remain rejected by daemon finalization. Release has mixed-clock selection, refusals, note/event tests and an older-daemon guard. Dependencies installed in `.venv` with workspace-local `.uv-cache`; no shared cache writes. The unchanged base source and initial three regressions are preserved in `.baseline` for the before run. Test lock waiting; no results claimed yet.

Baseline verification: replayed the unchanged source at `4c6a8a9c989e` in `.baseline`, with the initial three incident regressions from `b917b218a`. Locked pytest exited 1: **3 failed in 12.61 s**. Each failed because `OutcomeClass.LIMITED` was returned instead of `OK`, with details quoting the report (triage-first, triage-second, inventory). The frozen classifier clock is 2026-09-05 11:30Z, and these reports contain no provider reset evidence. Fixed-code validation is next.

First fixed-code run: **804 passed, 6 failed, 3 warnings in 256.24 s** across 12 files (810 tests). All three incident regressions, all 600 new Hypothesis examples (150 each; 82 invalid generation attempts), all 20 raw-stream fixture differential checks, and daemon limit-release tests passed. Three newly added CLI test handlers used dictionaries instead of the fake server's required callable API; these are corrected to lambdas. The other three failures concern process identity/liveness (`test_a_lock_whose_holder_is_dead_is_no_daemon`, `test_an_identity_mismatch_names_the_rendering_trap`, `test_c4_4_state_missing_exit_receipt_never_accepts_success`); a base replay is being prepared to check whether they precede this change.

Isolation correction: the three existing process tests depended on the host's process inspection, which is unavailable here. Their inputs now explicitly describe the absent process, changed start time and empty process table/dead guardian they intended to test; production process handling is unchanged. No base process replay is claimed. Together with the callable-handler correction, this is ready for a rerun of the same 810 checks.
