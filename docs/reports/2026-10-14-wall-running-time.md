# D-WALL1 / D-KILL1: execution budgets and cancellation attribution

The cold-boot recovery retained `ebf101720` (regressions) and `e011d11e6`
(contract), both based on `73aff8cd3`. Those steps were read and retained;
the implementation was unfinished. The recovered wall file failed all eight
cases before implementation.

## Changes

- Admission no longer cancels queued or waiting jobs because of their age.
  The wall sums attempt execution through starting, running and finalizing;
  reserved time and all inter-attempt waits are excluded. Legacy rows with no
  start contribute zero. Starting records its timestamp before opening the
  guardian gate (or on direct entry to finalizing after a launch failure),
  the guardian receipt preserves it, and terminal settlement
  records the end of finalization. The cap is checked again in that settlement
  transaction. Retry counting remains governed by `max_attempts`.
- The narrow tick projection includes prior completed execution with a
  conservative allowance for SQLite timestamp rounding. Only the worker's
  exact timestamp calculation can trigger cancellation.
- First-request cancellation metadata commits atomically with cancellation.
  Daemon requests carry source, reason and running seconds. Client requests
  carry session, socket audit-token pid and `--reason` (default
  `client-request`); request-supplied pids are ignored. Dependent descendants
  retain attribution and the initiating parent; independent branches survive.
  Repeated requests preserve the first event and timestamp.
- Another session's cancellation notifies the caller immediately while active,
  or in the terminal notice while waiting. Online and offline `runs show`
  expose the first request as `cancel`, including its timestamp; JSON and the
  bare CLI form include it. Conversation cancellations have explicit causes,
  and handoff recovery accepts both historical and new attribution records.
- Additional regressions cover family events, peer-pid spoofing, finalization
  accounting and crossing the cap during finalization, legacy unstarted rows,
  and fractional-attempt tick rounding. The bounded Hypothesis property varies
  attempt durations, waits, cap and execution state (40 deterministic examples).

## Commit storage

The sandbox refused the shared Git index lock. All new commits are on
`fix/wall-counts-running-time-217` in workspace-local `.git-local`, with the
recovered HEAD as ancestor. The original `.git` pointer and shared index were
left alone. Inspect with `git --git-dir=.git-local log`; all commits include the
requested co-author trailer. An ignored, unstaged delivery bundle will be
written at `docs/reports/2026-10-14-wall-running-time.bundle` after final
validation. No history was rewritten and nothing was pushed.

## Validation

Each pytest invocation runs alone under its own `lockf -k` hold of
`/private/tmp/claude-501/subfleet-suites.lock`, without a lock timeout or xdist.
Hypothesis uses the bounded `ci` profile, with storage in disposable scratch.
Scratch is removed using Python `shutil.rmtree`; no `.hypothesis` directory is
created in the checkout. Test output and intermediate evidence are outside the
repository and are not staged.

Initial verification: recovered wall regressions **8 failed**; extended wall
file **12 passed**. Tick prefilter initially had **30 setup errors** because
this sandbox cannot read the real macOS boot identity. Its scheduling-only
fixture now injects synthetic boot/process identity: **30 passed**.
`tests/unit/test_cli.py`: **200 passed**.

The wall/admission/tick group subsequently passed **360 tests**, with one
existing strict expected failure in `test_admission_properties.py` (C-6.10).
That group held the lock for 293.88 seconds. The state contract passed **37**
tests and conversation handoff passed **48**. Finalization, recovery,
additional final wall cases, and mutation checks remain in progress; this
checkpoint is not a final test claim.

The first six-file batch has now completed: tick prefilter **30 passed**, CLI
**200 passed**, state contract **37 passed**, conversation handoff **48 passed**,
conversation service **97 passed**, and offline **33 passed**. The grouped
finalization/recovery run completed with **442 passed, 28 failed, 23 skipped**
in 238.94 seconds of test runtime (the much longer lock wait is excluded).
Its failures exposed an extra notice for a request with no identified session,
a killed-verdict ordering regression, and stale sibling-attempt timestamps in
inspection fixtures. Commit `a23099d1a` corrects those issues. The first 23 skips
are real-process checks gated by unavailable sandbox ps/sysctl inspection.

Pending at this checkpoint: the six affected files' full rerun (salvage
finalization, notice headers, daemon settlement, wall regressions, finalization
replay and Codex completion); the additional cancellation/final-source group;
real-process recovery/e2e attempts; and all three mutation probes. The wall file
now contains 19 cases. All invocations remain serialized under the shared lock.
No final pass is claimed for the corrected files until that rerun completes.
