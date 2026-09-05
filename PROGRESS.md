# Timers lane progress

## State
Implementing milestone 5 on `lane/timers`. Initial worktree is clean. Final report will be committed as `OUTPUT.md` (no other output path was supplied).

## Done
- Read the acceptance clauses and lane brief; confirmed v1 is read-only.
- Identified contract questions to resolve explicitly: C-23.27's twenty-minute probe wording versus C-18.1's five-minute cycles; C-23.13's immutable unknown result versus usage reconciliation; C-23.17's guessed reset clock versus no invented usage numbers.

## Next
- Read implementation and v1 seams; split independent reset-credit and alert/status work.
- Build bounded probe/keepalive timers and wire daemon lifecycle/status.
- Verify retention pins and byte accounting, run focused and full suites, commit the final report.

## Step: bounded timers and daemon seams
- Done: retained guardian process ownership for jobless timer turns; added lane reservations, usage reads with deadlines/cancellation, post-heal cycle publication, keepalive request tracking, lifecycle/status, and policy defaults.
- Done: ping now enqueues jobless operator notices in additive schema v2 `service_notices`; existing notice polling/ack accepts their negative IDs. Alert/status and retention components committed independently.
- Validation: policy/store + alert/status focused suites pass with the existing shared checkout Python. `uv sync --group dev` failed fetching pluggy due DNS; no external endpoint used.
- Next: exercise daemon fakes, review reset propagation and action fencing, complete probe/keepalive regression cases, full suite.

## Step: integrated acceptance and sandbox comparison
- Done: added in-process daemon/fake timer acceptance, shutdown/API responsiveness checks, socket and guardian checks that explicitly skip when their required sandbox operations are denied, schema-v2 upgrade coverage, and foreign-account usage rejection.
- Done: action reconciliation preserves immutable `unknown` and appends a usage-settled event; confirmed actions override lagging capacity without synthetic percentages. Imported account/home subjects and alert latches are supported.
- Validation: focused timer suite passes; full corrected run had 979 passed / 61 skipped / 86 failed in 21.60 s. The untouched starting commit had the exact same 86 failure node IDs (876 passed / 60 skipped / 86 failed in 22.26 s). Existing failures require denied Unix binding, process inspection, or guard scratch writes.
- Environment: copied already-installed dependency files into ignored local `.venv`; `UV_NO_SYNC=1 uv run pytest` works offline. No guard bypass, v1 command, provider endpoint, or push was attempted.
- Next: finish cancellation/propagation edge cases, repeat affected checks, write and commit OUTPUT.md with exact final counts and integration seams.

## Step: final evidence and shutdown fixes
- Done: confirmation and closure reopening are atomic; mismatched-account usage cannot release or extend closures; reset candidates require current usage; old post-consume readings are marked stale in status and ignored for routing until propagation.
- Done: revoked credentials block the real scheduler without losing epoch-based re-probe; guardian request metadata survives recovery; keepalive auth-dead disables immediately; retention accepts cancellation and deadlines.
- Done: added daemon timeout-to-usage-settlement and real scheduler latch tests. The socket/guardian checks skip explicitly under this sandbox; the in-process daemon cycles execute normally.
- Next: final full-suite comparison, report, and clean-worktree verification.

## Step: re-enrolment recovery
- Done: a newly enrolled enabled binding supersedes the old disabled binding for the home's alerts, menu projection, and credit total while preserving every historical row. This permits exactly one auth recovery notice after re-enrolment.
- Validation: focused acceptance command now reports 120 passed, 2 skipped in 2.50 s; every new test has a contract clause in its docstring. Latest full run before this final regression case reported 1000 passed, 62 skipped, 86 baseline-identical sandbox failures in 22.54 s.
- Next: final rerun and committed report.
