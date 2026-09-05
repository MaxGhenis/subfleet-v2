# E2E lane progress

## State

The suite and seam fixes are committed on `lane/e2e`. Milestone 1 and 2 acceptance
remains blocked by sandbox denial of `ps`, `sysctl kern.boottime`, and AF_UNIX
binding; no bypass attempted. The final E2E run collected and skipped 21 cases.

## Done

- Confirmed the worktree and branch; read the required acceptance clauses.
- Established this committed progress log before implementation.
- Read the required fake providers, fixture expectations, interfaces, and lane reports.
- Added Claude `env` credentials (`84a373b`) and TRUST selection (`8248222`).
- Committed recovery, Claude, and guard/isolation acceptance modules.
- Fixed `why` rendering of policy rejection reasons (`13b4bdf`).
- Reproduced missing raw-stream/launch artifacts with both real adapters in
  deterministic finalization tests; now publish them and Claude's sent prompt.
  All five finalization/replay tests pass.
- Installed development dependencies offline from existing local caches after
  the initial PyPI DNS failure; `uv sync --offline --group dev` succeeds.
- Audited and committed the real `subfleetd` fixture (`4b00096`), including
  isolated Git setup, CLI readiness, and cleanup of guardians without receipts.
- Reproduced missing effective retry exclusions in `show`; persist them in the
  admission transaction. Limited and transient retry regression checks pass.
- All four requested acceptance modules are committed (21 cases before final audit).
- Reproduced lost guard refusal details through daemon finalization and CLI wait;
  retain prelaunch errors and include the latest attempt in terminal wait results.
- Reproduced rc 1 leaking through for provider limits, dead authentication, and
  old CLIs; map terminal job codes to C-17.3 while retaining raw attempt rc.
- Extended the Codex executable fake with metadata RPCs, a delayed `slow` alias,
  and opt-in dirty-worktree bytes for salvage. Its version and `hooks/list`
  replies pass the real guard preflight using the unchanged reviewed TRUST.
- Fixed daemon/client timezone and locale identity mismatch (`5523bad`).
- Preserved exact original prompts and digest inputs, recorded prepared write
  prompts separately, and added Codex sent-prompt capture (`cb144ad`). Eight
  regressions cover both providers, sandboxes, no-preamble, and retry inputs.
- Final focused `uv run pytest` validation: 405 passed, 1 deselected in 12.64 s.
- Final `uv sync --group dev` succeeded. E2E: 21 skipped in 0.12 s (0.54 s wall),
  which does not establish physical acceptance or its execution-time budget.
- All 17 E2E test functions cite clauses; `git diff --check` passed.
- Wrote the final report to `OUTPUT.md` and `docs/lanes/reports/e2e-OUTPUT.md`.

## Next

- Integrator: run all 21 real-daemon cases with permitted inspection and Unix
  sockets, and verify executed passes in under 90 seconds.
- Integrator: address the existing-store credential CHECK migration before
  adopting env credentials there, run broader physical milestone gates, and push
  the local lane commits from outside the sandbox.
