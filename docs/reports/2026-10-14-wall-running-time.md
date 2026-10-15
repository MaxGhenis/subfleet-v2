# D-WALL1 / D-KILL1: running-time wall and attributed cancellation

`max_wall_s` now bounds summed attempt execution, including starting and
finalization. Admission never applies it to queued or waiting jobs. Every new
`job.cancel_requested` event identifies its source and cause; client requests
include the requesting session and the socket peer pid.

The restart retained `ebf101720` (regressions) and `e011d11e6` (contract), based
on `73aff8cd3`. Those committed steps were preserved. The implementation was
unfinished, and recovered coverage needed additional finalization, family and
legacy-clock cases; this continuation completes that work.

## Changes

- Execution is summed from durable attempt timestamps, independently of the
  job's first `started_at`. Reserved time and gaps between attempts are excluded.
  Starting records its start before the guardian gate opens; its receipt keeps
  that timestamp. A launch failure entering finalizing directly records its
  start at that transition. Finalization stays live after the provider receipt,
  and settlement records its actual end. Legacy rows without a start contribute
  zero. `max_attempts` retains its retry role; no job-age cap was added.
- Both the attempt pass and terminal settlement enforce the cap. The tick's
  narrow SQL projection conservatively allows for timestamp rounding, while
  only the worker's exact calculation can cancel. Wall cancellations preserve
  `killed_by=max_wall_s`. The earlier killed-verdict rule is preserved through
  the settlement check, including Codex's exit-zero signal cases.
- Cancellation and its first-request audit record commit atomically. Daemon
  requests carry `{by: "daemon", reason, running_s}`; the wall reason is
  `max_wall_s`. Client requests carry `{by: "client", session_id, pid, reason}`.
  The CLI supplies its session and optional `--reason` (default
  `client-request`); the saved socket audit token supplies pid. A request's pid
  field is ignored. Unavailable identities are explicit nulls.
- Repeated kills preserve the first event and timestamp. Dependent descendants
  have their own attributed events with the initiating parent; independent
  branches survive. An identified other session leaves the caller an immediate
  service notice while active, or requester text in its waiting-job terminal
  notice. An absent session is recorded without claiming another session asked.
  If settlement wins the cancellation transaction, kill says already finished.
- Online and offline `runs show` expose the first request as `cancel`, including
  its timestamp. The bare and JSON forms include the metadata. Conversation
  cancellations have explicit causes, and handoff recovery accepts historical
  and new attribution formats. Contract clauses C-4.x, C-6.4 and C-17.x and
  version history cite D-WALL1 / D-KILL1.

## Validation (2026-10-15)

Final selected-file results: **1,296 passed, 85 skipped, 1 existing expected
failure, 0 failures or setup errors**, across **42 files**. Counts below are the
latest full-file results, without counting repeat invocations twice.

Every pytest ran alone under its own `lockf -k` hold of
`/private/tmp/claude-501/subfleet-suites.lock`, without a lock timeout or xdist.
The longest pytest invocation took 293.88 seconds; shared-lock waits are excluded
from that duration. Hypothesis used bounded settings and `HYPOTHESIS_PROFILE=ci`;
the new property uses 40 deterministic examples varying attempts, waits, cap and
execution state. The deep profile was never run. Specified scratch is removed
with Python `shutil.rmtree` after each invocation. No logs, XML/JSON evidence or
bundles are staged.

The recovered wall file failed all eight cases before implementation. The tick
fixture initially had 30 boot-identity setup errors; scheduling-only fixtures
now use synthetic process identity. Intermediate validation found 28 failures:
unknown-session alert text, a killed-verdict ordering regression, and stale
sibling-attempt fixture clocks. Those were corrected, and all six affected files
then passed their full rerun (**247 passed**). The final-source cancellation
group passed **114**, including all **19 wall cases**.

The **85 skipped cases were not executed**: the existing C-5.3 fixtures gate
real-process tests when this sandbox cannot read macOS boot/process identity
through sysctl/ps. The single strict expected failure is the pre-existing
C-6.10 wait-kind issue in `test_admission_properties.py`. Tests outside the
selected files were not run. Physical guardian survival, signals and native
peer checks are therefore not claimed as verified here; socket cancellation
plumbing, notices and durable metadata were exercised in the isolated fixtures.

| File | Passed | Skipped / expected failure |
| --- | ---: | ---: |
| `tests/e2e/test_codex_turn_completed_e2e.py` | 0 | 3 |
| `tests/e2e/test_conversations.py` | 0 | 44 |
| `tests/e2e/test_milestone1_codex.py` | 0 | 9 |
| `tests/e2e/test_notice_job_state.py` | 0 | 1 |
| `tests/e2e/test_recovery.py` | 0 | 2 |
| `tests/fake/test_admission_disk.py` | 27 | 0 |
| `tests/fake/test_admission_held_waiters.py` | 1 | 0 |
| `tests/fake/test_admission_latency.py` | 14 | 0 |
| `tests/fake/test_admission_lease_waiters.py` | 16 | 0 |
| `tests/fake/test_admission_liveness.py` | 32 | 0 |
| `tests/fake/test_admission_priority.py` | 32 | 0 |
| `tests/fake/test_admission_properties.py` | 27 | 1 xfailed |
| `tests/fake/test_admission_ramp.py` | 19 | 0 |
| `tests/fake/test_admission_ramp_retention.py` | 11 | 0 |
| `tests/fake/test_admission_route_isolation.py` | 90 | 0 |
| `tests/fake/test_admission_stateful.py` | 2 | 0 |
| `tests/fake/test_admission_visibility.py` | 43 | 0 |
| `tests/fake/test_codex_turn_completed_fake.py` | 105 | 0 |
| `tests/fake/test_daemon_contract.py` | 0 | 21 |
| `tests/fake/test_finalization_replay.py` | 10 | 0 |
| `tests/fake/test_lost_acknowledgement.py` | 0 | 3 |
| `tests/fake/test_notice_header.py` | 23 | 0 |
| `tests/fake/test_probe_cancel.py` | 24 | 0 |
| `tests/fake/test_probe_recovery.py` | 9 | 0 |
| `tests/fake/test_provider_qos.py` | 0 | 2 |
| `tests/fake/test_resume_contract.py` | 23 | 0 |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 |
| `tests/fake/test_turn_trees.py` | 12 | 0 |
| `tests/fake/test_wall_running_time.py` | 19 | 0 |
| `tests/fake/test_workspace_transient.py` | 24 | 0 |
| `tests/unit/test_cli.py` | 200 | 0 |
| `tests/unit/test_conversation_handoff.py` | 48 | 0 |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 9 | 0 |
| `tests/unit/test_conversation_service.py` | 97 | 0 |
| `tests/unit/test_daemon_settle.py` | 43 | 0 |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 |
| `tests/unit/test_offline.py` | 33 | 0 |
| `tests/unit/test_outcome_unknown.py` | 58 | 0 |
| `tests/unit/test_settle_properties.py` | 129 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 |
| `tests/unit/test_tick_prefilter.py` | 30 | 0 |

## Mutation checks

Mutations replace the relevant daemon method in memory inside an isolated
pytest process. Each probe takes its own shared-lock hold; the committed source
is unchanged. Each expected failure must be in its named regression, rather
than a collection or fixture error.

| Mutation | Named regression | Result |
| --- | --- | --- |
| Measure from job `started_at` again | `tests/fake/test_wall_running_time.py::test_long_gap_does_not_expire_a_short_running_retry` | Detected: “wall counted the wait” |
| Kill waiting jobs from admission again | `tests/fake/test_state_contract.py::test_c6_4_state_wall_does_not_cancel_a_held_retry` | Pending |
| Drop the wall cancel reason | `tests/fake/test_wall_running_time.py::test_wall_stops_at_sum_of_attempt_running_times[running]` | Pending |

**Final mutation completion and cleanup checks remain pending in this draft.**

## Commit storage and delivery

The sandbox refused the shared Git index lock. New commits are on
`fix/wall-counts-running-time-217` in workspace-local `.git-local`, with
`e011d11e6` as ancestor. Inspect them using `git --git-dir=.git-local log`.
The original `.git` pointer and shared index were preserved. Every commit has
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. History was not rewritten
and nothing was pushed.

An ignored, unstaged delivery bundle is at
`docs/reports/2026-10-14-wall-running-time.bundle`. Its prerequisite is
`e011d11e6`; it advertises `refs/heads/fix/wall-counts-running-time-217`.
The checkpoint bundle has been verified and will be refreshed after the final
report commit. Local implementation and checkpoint commits before that final
report are:

| Commit | Step |
| --- | --- |
| `e92ae0c63` | test: cover cancellation propagation and complete attempt accounting |
| `8a209f875` | fix: bound wall time to execution and attribute cancellation requests |
| `63369d52e` | test: check wall at finalization commit and isolate scheduler identity |
| `4e809220f` | fix: recheck execution budget atomically at finalization settlement |
| `a47ea9639` | fix: keep tick wall estimates conservative across fractional attempts |
| `dc8c7ac9d` | test: keep conversation fakes scoped to cancellation store operations |
| `3fa0550b7` | test: preserve kill acknowledgment when settlement wins a race |
| `4d155fd62` | fix: report finished jobs when cancellation loses settlement race |
| `84da48b78` | test: align synthetic inspection attempts with running-time budgets |
| `3e9f2be93` | test: assert default client reason on the client cancellation case |
| `06aeb3e7e` | test: count finalization entered before a guardian starts |
| `8b2643177` | fix: record execution start for direct finalization after launch failure |
| `d766a5975` | docs: checkpoint running-time wall implementation and validation progress |
| `a23099d1a` | fix: preserve killed verdicts and notify only identified other sessions |
| `75c4a2c97` | docs: checkpoint completed suites and targeted validation corrections |
