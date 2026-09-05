# Sessions review progress

## State

- Working on `lane/sessions-review` from `38a7a9a`, offline; commits only, no push.
- Salvage applied cleanly; all eight reported failures reproduced and explained.
- Final report: `docs/lanes/reports/sessions-review-OUTPUT.md`.

## Done

- Confirmed the worktree and clean starting branch.
- Read the original lane brief, report, and relevant contract clauses.
- Identified all twelve files in the salvaged review pass.
- Reproduced the original failures: 8 failed, 231 passed, 1 error in 34.09s.
- Removed the JSON-format-dependent event filter; retained event-id ordering
  so the last retire/unretire action wins even within one second (C-23.35).
- Focused event tests: 4 passed, 13 deselected in 0.24s.
- Kept revive attempts out of the daemon-created lane-session census (C-23.31).
  A desktop session remains eligible for listing/continuation after revival;
  ordinary dispatch attempts still mark headless sessions. Both cases pass.
- Kept C-23.39 model-substitution history and added the real-daemon transport
  and audit regression. Refused submissions create no history.
- Dropped Claude-only revive routing: the six original revive failures explicitly
  requested `astra`; model resolution was unchanged. C-23.39/C-23.54 do not
  authorize this new refusal, and C-6.5 requires refusals to be indexed.
- Dropped repeat-revive dedupe: C-23.33 dedupes nudges; C-23.55 limits concurrent
  revives through leases, not later retries at an unchanged interruption.
- Revive unit + fake suite: 38 passed, 5 failed in 31.63s. Remaining failures
  reach admission but probes quarantine when process enumeration is unavailable.

## Validation environment

- Copied the existing main worktree's uv cache locally. Offline dependency sync
  succeeded with `--no-install-project`; project build lookup could not resolve
  cached hatchling. Tests use `UV_OFFLINE=1 UV_NO_SYNC=1` with the local venv.
- The initial registry socket test failed with `PermissionError: Operation not
  permitted` at Unix socket bind. This sandbox restriction is not bypassed.
- Probe records confirm `group enumeration unavailable`, `descendant enumeration
  unavailable`, and `marker enumeration unavailable`; containment therefore keeps
  the job waiting. No process guards or test expectations are bypassed.

## Next

- Commit the reviewed CLI, nudge, handoff and mirror behaviors.
- Commit corrected behaviors separately, updating this file with each step.
- Run the complete suite and commit the final report.
