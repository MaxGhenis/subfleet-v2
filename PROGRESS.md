# Gates lane progress

## State

Milestone 7 implementation is integrated on `lane/gates`; focused review and full-suite verification remain. Final report: `OUTPUT.md`.

## Done

- Read acceptance contract, plans, v2 job/action seams, all v1 gate code/tests, agent command lines, and README gate syntax.
- Committed revision, strict verdict, certificate helpers; three complete redacted v1 replay archives (123 files).
- Committed typed merge actions with injectable runner, preflight/head guard, immutable landing verification, fenced read reconciliation, and startup recovery.
- Committed schema 3 job isolation fields, exact model pins, gate admission leases, fresh per-lane isolation, and daemon gate protocol bridge.
- Added native gate CLI/console entry, v1 compatibility cases, daemon-owned event journal/file projections, copied peer bundles, round consumption and four-round stopping.
- Focused checks: 82 revision/verdict/replay tests; 55 merge tests; 264 related admission/adapter/store tests; 9 fake end-to-end gate tests (7.80 s).

## Next

- Fix independent review findings around continuation, stale certificates, and downgrade evidence.
- Finish CLI compatibility validation and run the targeted and full suites.
- Write committed integrator report with exact results and remaining production seams.

## Constraints and findings

- `uv sync --group dev` could not download pytest (DNS); offline build lacks hatchling. Copied already-installed locked test dependencies from the main worktree to this lane's ignored virtualenv; direct Python pytest works.
- Socket/process-dependent existing tests hit sandbox permissions; report full-suite results distinctly from focused tests.
- Installed Codex `--ephemeral` suppresses the rollout attestation consumes; no authenticated served-model exec event fixture exists locally. Those real rounds stay unattested and stop after the cap.
- Current local v1 allows unlimited rounds and explicit same-head merge retries. C-23.53 and C-23.13 take precedence; v1 syntax and exit meanings stay accepted.
- One optional broad Codex source search was refused by the unscoped-search guard; it was not retried or bypassed.
