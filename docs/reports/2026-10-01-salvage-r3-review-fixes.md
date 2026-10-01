# Salvage round 3: review fixes and final verification

The required review fixes are complete at checkpoint `01df8041`. This continuation
independently reviewed `ceacf18b..01df8041`, finished verification, and repaired nine
stale workspace mocks exposed by the full suite. The checkpoint was clean; no
unfinished production edit remained in the inherited WIP snapshot.

## Review findings

| Finding | Result and code | Regression coverage |
|---|---|---|
| **P2**: terminal notices lost the previous attempt's failed salvage and held baseline | `subfleet/daemon.py:4835` (`_earlier_attempt`) appends the attempt's outcome and salvage summary, names held refs, and records an otherwise unowned ref as the last attempt's salvage artifact. Cancel, failed/refused preparation, skipped revive, unlaunched/quarantined retry, and spawn-error finalization use it. `daemon.py:2767` also covers cancellation during the baseline-pin race. The notice says “cancelled while waiting to retry” or “failed while preparing the retry”; a retry's own baseline ref never stands in for its failed salvage. C-13.1 and C-15.1 document this. | Real-git fake-daemon cases (a), (b), (c): `tests/fake/test_salvage_finalization.py:598`, `:616`, `:666`; race `:709`; retry's own failed salvage `:853`; spawn-error predecessor `:882`; malformed held events `:934`. |
| **P3-1**: git crashing on a corrupt seed index defeated fallback | `subfleet/salvage.py:254` (`snapshot_tree`) discards a failed seed and reads the baseline into a fresh temporary directory, avoiding the crashed git's `index.lock`. `_seeded` at `:308` treats failed/signalled seeding steps as no seed. A step stopped at its cap propagates `SalvageError(timed_out=True)` instead of spending another cap on the slower unseeded read. C-6.8 states this exception. | Real corrupt bytes `b"DIRC\x00\x00\x00\x02garbage" * 3`: `tests/unit/test_salvage_unindexable.py:488`, `tests/fake/test_salvage_finalization.py:1036`; signal/EMFILE/exit-128 fallback `test_salvage_unindexable.py:520`; timeout propagation `:536`. Both real-git crash tests ran and passed here. |
| **P3-3**: inherited pathspec variables disabled the nested-repository exclusion | `subfleet/salvage.py:120` strips all four `GIT_*_PATHSPECS` variables declared in `subfleet/contracts.py:37`. `subfleet/cli.py:2079` strips them when starting the daemon. Submit's git probes and `subfleet/conversations/diff.py:402` use the same clean git environment, including when `subfleetd` is invoked directly. | Real-git exclusion under each variable: `tests/unit/test_salvage_unindexable.py:632`; submit path probes `:654`; filtered conversation diff `tests/unit/test_conversation_diff.py`; daemon environment `tests/unit/test_daemon_verbs.py`. |
| **P3-5**: a transient launch-time branch-check failure ended the job permanently | `subfleet/daemon.py:4183` routes a transient failure through `_unlaunched`, without starting a provider. `_defer_after_launch` at `:4583` puts a retriable job in a workspace wait, records the cause, and backs off 5 s, 10 s, etc., capped at 300 s. Consecutive launch failures are counted from stored attempt outcomes, so successful admission does not reset their backoff; `max_attempts` still bounds them. Once git answers “main”, the writable job is refused. One-attempt conversation turns still fail. | `tests/fake/test_workspace_transient.py:446`, `:512`: killed git, no launch, due/early admission, main refusal, consecutive launch backoff, and exhausted attempt budget. |
| **P3-2**: dirty contents under an existing submodule/nested gitlink are unnamed | Left as requested. An empty repository replacing a tracked submodule can retain the old gitlink with `skipped == []`; its new files exist only in the worktree. Dirty files inside a committed nested repository are likewise outside the gitlink snapshot. Retention currently keeps that worktree, but a retry can overwrite those files. | The independent review's real-git probe documents this existing limit. No fix was requested in this round. |
| **P3-4**: salvage retry limits are per daemon run, with short delays | Left as requested. The in-memory count permits three more tries after a restart until the failure receipt is durable. Delays remain 0.5 s then 1 s. No evidence established that salvage itself restarts the daemon. | Existing bounded-retry tests pass; this round does not change restart persistence or delay policy. |

The additional full-suite fix updates the mocked `_workspace` result from three
fields to four, supplying an empty `skipped` list in
`tests/fake/test_admission_latency.py:105`,
`tests/fake/test_admission_liveness.py:51`, and
`tests/fake/test_prompt_seam.py:31` (nine mocks total). The real method gained its
fourth field in the inherited N3 change. The original assertions are retained,
including the pass-for-pass scheduling property. A representative arity failure
was reproduced on `ceacf18b` before the fix.

## Original brief and observed live causes

The prior report, `docs/reports/2026-09-28-salvage-r3.md`, remains the source for
the original live-log evidence. This continuation did not access `~/.subfleet`
or the live daemon, following the continuation's explicit restriction.

| Original finding or observed cause | Verification in this continuation |
|---|---|
| N1: a salvage error containing a non-UTF-8 filename could repeatedly break receipts | Existing UTF-8 escaping in `salvage.utf8_text`, finalization, and turn snapshots is covered by the required salvage/finalization/turn suites. |
| N2: a failed attempt's retry baseline was unreferenced | Existing create-only baseline pins plus the P2 notice/artifact fixes above are covered by the real-git retry and cancellation cases. |
| N3: live diff snapshots silently omitted empty nested repositories | Snapshot omissions remain listed in live diffs; conversation diff and the native Swift diff tests passed. The stale mocks from this change are repaired here. |
| The recorded 512-retry case: one empty nested repository beside eight committed ones | The fake live-shape finalization regression passes; the exclusion also works under inherited pathspec variables. No new live-state claim is made. |
| The recorded short runs of git/worktree timeouts | Admission backoff and bounded salvage finalization tests pass; seed timeouts retain their cap instead of doing a slower second read. |
| Git's full-disk wording, `write error. Out of diskspace` | Transient-git unit and fake workspace tests pass. |
| OS full disk, `OSError(ENOSPC)` | `tests/fake/test_workspace_enospc.py` passes. |

## Commits

Inherited commits after the review baseline, oldest first:

- `f88ccb8c`: P3-3 salvage/daemon environment scrub.
- `3e1f364c`: P3-1 fresh-directory fallback.
- `a0c8d520`: P2 terminal notices, held artifacts, cancellation race.
- `70dee5e2`: P3-5 transient launch retry.
- `03afaf9f`: P2 notice walks back through retries that never launched.
- `63a23a21`: preserved WIP snapshot, including seed-timeout handling.
- `9c58db6e`: P3-1 timeout contract and verification.
- `62e48446`: P3-3 submit/diff environment scrub.
- `1b7cc45b`: P2 own-salvage distinction, spawn-error notices, malformed events.
- `01df8041`: P3-5 durable consecutive launch backoff.

New test repair: `05ced045e3955073b0a45aaadf7d1ca94b571f89`,
“Update admission mocks for snapshot omissions.” This report is the next coherent
commit. Both new commits carry
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

The managed sandbox denied `git add` because the assigned checkout's shared
index is outside its writable roots:
`/Users/maxghenis/subfleet-v2/.git/worktrees/20260930-115146-salvage-r3-cont3-r2/index.lock`.
The assigned checkout therefore still points at `01df8041`. To preserve real
commits without writing outside the workspace, the two new commits live on
`fix/salvage-r3-cont3` in the workspace-local Git metadata at
`build/verification-continuation/commits.git`, with `01df8041` as their parent.
They are exported in
`docs/reports/2026-10-01-salvage-r3-review-fixes.bundle`; that checkpoint is the
bundle's only prerequisite. The normal worktree still contains the complete
changes for Subfleet to preserve. Nothing was pushed or rewritten.

## Verification

The original required command's files, including their new regressions, passed
**276 tests**: 90 fake tests and 186 unit tests. The extra daemon-environment
regression passed too; running its entire 20-test file produced 15 passes and
five unrelated process-inspection failures, each reproduced on `ceacf18b`.
All **33** tests in the repaired fixture files passed, including the isolated
pass-for-pass admission liveness property. These counts are subsets of the full
suite, not additional tests to add to it.

The full `uv run pytest -q` suite was run in foreground file slices, after
collecting **6,810** nodes. Source-file and node coverage audits found no gaps
or duplicate counting. For the three repaired files, final reruns replace their
original stale-fixture results.

| Scope | Passed | Failed | Setup errors | Skipped | Total |
|---|---:|---:|---:|---:|---:|
| Unit | 5,678 | 124 | 37 | 1 | 5,840 |
| Fake | 637 | 12 | 0 | 53 | 702 |
| Frontend, original compiler invocation | 131 | 0 | 7 | 1 | 139 |
| E2E | 2 | 0 | 0 | 60 | 62 |
| Process and live, default opt-in flags | 51 | 0 | 0 | 16 | 67 |
| **Full suite** | **6,499** | **136** | **44** | **131** | **6,810** |

The full suite remains red in this sandbox. **Every one of its 180 remaining
failure/error nodes was reproduced on exact `ceacf18b` source under the same
restrictions**, with matching node identities and statuses. No timing failure
was called pre-existing without its baseline comparison. The remaining groups:

- **161 unit failures/errors:** process and boot inspection is unavailable;
  direct `ps` calls fail with `Operation not permitted`, boot identity reads
  raise `InspectionError`, and dependent CLI/importer/offline assertions fail
  conservatively. Baseline logs cover all 124 failures and 37 setup errors.
- **12 fake failures:** four daemon-stack tests cannot establish the boot
  identity; lost-notice and missing-exit-receipt fixtures still invoke real
  process liveness, which is unknown here; six session-world cases have
  baseline-identical scheduling/lease/resume assertions or timeouts. Exact
  failures and their baseline logs are indexed in `fake-summary.json`.
- **7 frontend setup errors:** SwiftUI's `StateMacro` plugin server gives a
  malformed response. All seven reproduce with the same compiler diagnostics
  on `ceacf18b`. A verification-only `swiftc -disable-sandbox` invocation makes
  all **7 menu tests pass**, with unchanged assertions and no source change.
  This separate run is not substituted into the default full-suite totals.

`tests/frontend/test_core_diff.py` and the companion protocol tests compiled
with the original fixture and passed all **13 tests** in 72.01 s. The rest of
the native model tests passed 118, with one live-app skip because process/boot
inspection is unavailable. Including the separate menu compiler workaround,
frontend coverage is **138 passed, 1 skipped**. No Swift compile hit its cap.
E2E's 60 skips use its process-inspection guard. Live-provider opt-ins were not
enabled, and the real daemon was never contacted.

Verification evidence is in `build/verification-continuation/`: exact commands
and tracebacks in `unit-required.log`, `unit-00.log` through `unit-08.log`,
`fake-*.log`, `frontend-*.log`, and `e2e.log`; baseline runs in corresponding
`baseline-*.log` and fake/frontend baseline logs; coverage indexes in
`fake-summary.json`, `unit-06-08-summary.json`, and `frontend-summary.json`.
Baseline archives are inside this workspace. Their runs disable `uv` sync so
they cannot replace the shared environment's editable installation.

Every test command had a 570 s foreground cap and descendant cleanup; the
longest original slice completed in 434.21 s. No command was backgrounded.
`git diff --check` passed for both the inherited range and this continuation.
macOS `libproc` supplied process identities because `ps` is denied here. Each
runner's audit is empty, and a final identity check found no live process
started by this continuation. The running daemon and caller's checkout were
not modified.
