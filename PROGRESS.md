# Codex adapter lane progress

## State

Resumed on `lane/codex-adapter` after the restored WIP and main merge. The worktree started clean. Adapter, guard, fixtures, fake provider, and existing tests are present and pass; focused review and the missing probe suite are underway. Runtime code uses the standard library and never launches provider processes from the adapter.

## Done

- Confirmed the worktree and clean lane branch.
- Read the acceptance contract, shared dataclasses, adapter interface, captured CLI help, and v1 reference files (read-only).
- Identified lane scope: Codex adapter, pinned guard/preflight, redacted fixtures, fake CLI, and focused unit/process tests.
- Built 12 fixtures and a Python fake provider; all replay/redaction smoke checks passed (0.55 s).
- Real success comes from run `20260905-111854-fix-06-lane-g-r2`, using normalized stream envelopes around recorded thread/deliverable evidence. Eleven failure fixtures are explicitly synthetic: the newest 150 directories contained 29 Codex-family runs and no failed Codex artifact. v1 uses `family`, not `provider`, in these metadata files.
- Prepared local dependencies offline from the existing UV cache after inherited `UV_FROZEN=1` and network DNS failures prevented initial sync; generated `uv.lock`.
- Resume baseline: `uv run pytest -q tests/unit/test_codex_adapter.py tests/unit/test_guard_trust.py tests/process/test_codex_isolation.py`: **97 passed in 4.24 s** (with workspace-local UV cache/environment).
- Re-read C-6.7: Claude owns its headless prepend; the Codex prompt remains unchanged.
- Fixed six classifier regressions (C-9.2–C-9.6): subscription upgrade URLs, observation timestamps mistaken for reset clocks, explicit reset time zones, account scope precedence, structured credit codes, and access-token errors outside usage endpoints. Adapter suite: **59 passed in 0.10 s**; all six new tests failed before the fixes.

## Next

- Review restored adapter classification, attestation, and launch behavior; add missing probe/enrollment tests (C-9, C-10, C-12).
- Independently check copied guard and preflight against v1 (C-14).
- Audit existing fixture provenance, fake replay, and isolation evidence.
- Run the specified acceptance suite within C-20.2 budgets; write the final report to `OUTPUT.md` unless an output path is supplied.
- Commit every coherent step and push each commit to `origin lane/codex-adapter`.

## Constraints and integration notes

- No v1 commands or provider executions; only `codex --version` may run.
- Shared store/daemon/guardian/process/CLI/Claude/registry modules are out of scope.
- The raw exit code and signal can be retained in `Outcome.evidence` without changing shared dataclasses.
- Existing additive seam: optional `Launch.lane_id` binds classifier closures to their lane without filesystem writes or undocumented environment variables.
- Push of resumed progress commit failed with `Could not resolve host: github.com`. Local commits are retained; push after every new commit remains required.
