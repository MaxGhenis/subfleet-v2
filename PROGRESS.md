# E2E lane progress

## State

Implementing milestone 1 and 2 acceptance through the real CLI, daemon, and
provider adapters on `lane/e2e`. Physical execution is blocked by sandbox denial
of `ps`, `sysctl kern.boottime`, and AF_UNIX binding; no bypass attempted.

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

## Next

- Finish and audit the real-daemon fixture and Codex acceptance module.
- Fix retry exclusion persistence and CLI error/result propagation separately.
- Run `uv run pytest -q tests/e2e`, record timing and limitations, and write
  `docs/lanes/reports/e2e-OUTPUT.md`.
