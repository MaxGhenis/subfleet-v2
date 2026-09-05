# Gates lane progress

## State

Milestone 7 implementation and recovery fixes are integrated on `lane/gates`; final full-suite verification remains. Final report: `OUTPUT.md`.

## Done

- Read acceptance contract, plans, v2 job/action seams, all v1 gate code/tests, agent command lines, and README gate syntax.
- Committed revision, strict verdict, certificate helpers; three complete redacted v1 replay archives (123 files).
- Committed typed merge actions with injectable runner, preflight/head guard, immutable landing verification, fenced read reconciliation, and startup recovery.
- Committed schema 3 job isolation fields, exact model pins, gate admission leases, fresh per-lane isolation, and daemon gate protocol bridge.
- Added native gate CLI/console entry, v1 compatibility cases, daemon-owned event journal/file projections, copied peer bundles, round consumption and four-round stopping.
- Focused checks: 82 revision/verdict/replay tests; 55 merge tests; 264 related admission/adapter/store tests; 9 fake end-to-end gate tests (7.80 s).

- Independent review fixes now pass: fresh certificates, recovery source checks, empty downgrade rejection, changed-head action continuation, wrong explicit fingerprint rejection.
- Targeted acceptance command with offline `UV_NO_SYNC=1`: 179 passed in 2.08 s.

- Typed gate wire inputs and server-side dry-run prevent malformed clients from dispatching. Offline reader uses the shared schema version.
- Compatibility: 1,536 passed in 4.44 s. Gate plus Claude isolation checks: 202 passed in 2.59 s.
- First full suite: 118 failed, 2,865 passed, 63 skipped in 38.83 s. One memory-removal expectation fixed; remaining failures involve unavailable sockets/process inspection or default scratch paths outside the writable root.
- Confirmed operation keys reject a different approved base or merge method. Added daemon recovery fencing, native gate documentation, and a process-backed fixture peer test (skips where process identity inspection is unavailable).
- Final independent review fixed interrupted merge-result publication and concurrent gates observing the same action. Both poll and continue recover confirmed/unknown landings without checking the moving base or resubmitting.

## Next

- Finish full-suite validation; distinguish sandbox restrictions from regressions.
- Write committed integrator report with exact results and remaining production seams.

## Constraints and findings

- `uv sync --group dev` could not download pytest (DNS); offline build lacks hatchling. Copied already-installed locked test dependencies from the main worktree to this lane's ignored virtualenv; direct Python pytest works.
- Socket/process-dependent existing tests hit sandbox permissions; report full-suite results distinctly from focused tests.
- Installed Codex `--ephemeral` suppresses the rollout attestation consumes; no authenticated served-model exec event fixture exists locally. Those real rounds stay unattested and stop after the cap.
- Current local v1 allows unlimited rounds and explicit same-head merge retries. C-23.53 and C-23.13 take precedence; v1 syntax and exit meanings stay accepted.
- One optional broad Codex source search was refused by the unscoped-search guard; it was not retried or bypassed.
