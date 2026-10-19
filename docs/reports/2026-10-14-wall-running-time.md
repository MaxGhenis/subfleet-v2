# D-WALL1 / D-KILL1: running-time wall and attributed cancellation

`max_wall_s` bounds summed provider running time, ending at each known exit;
round 2 below supersedes this report's original finalization accounting.
Admission never applies it to queued or waiting jobs. Every new
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
| Kill waiting jobs from admission again | `tests/fake/test_state_contract.py::test_c6_4_state_wall_does_not_cancel_a_held_retry` | Detected: cancelled instead of waiting |
| Drop the wall cancel reason | `tests/fake/test_wall_running_time.py::test_wall_stops_at_sum_of_attempt_running_times[running]` | Detected: missing required reason |

All three probes produced their intended named assertion failures, with no
collection or fixture error. These three expected mutation failures are separate
from the passing-file counts above. Every task-owned pytest wrapper has exited,
mutation scratch is removed, and the checkout has no `.hypothesis` directory.

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
The delivery bundle is refreshed after the final report commit and verified
against the local branch tip. Local implementation and checkpoint commits before that final
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
| `a53bd1a05` | docs: record completed regression suites and remaining mutation probes |

## Round 2 (2026-10-15)

Final status: implementation committed at `309c432f6`. All 59 requested and
identified files ran successfully during the round. After the final family fix,
23 files were repeated: 743 passed and 1 existing expected failure. The other
36 files and the three mutation probes were not repeated on that final source.
The managed foreground runner was stopped (exit 130); committed source is
unchanged and owned scratch is removed. The clock observation advanced from
15:13 to 17:54 UTC without a new test result, crossing the requested 16:50 UTC
finish checkpoint. No further tests were started. Base:
`bde895c69b25e91a5e8e24e25631ccbda008fe5f`. No round-1 review file existed at
`~/reviews/subfleet-hub/wall-2026-10-14/review-r1.md` when this round started,
so there were no r1 findings to enumerate or conflicting findings to resolve.

### Verified cause and changes

The hub's diagnosis was correct. `_process_attempt` enforced the wall before
reading `exit.json`; `_running_s` replaced even a known live row's provider end
with `now`; and terminal settlement enforced the cap again after salvage and
overwrote the provider's end with settlement time. A cold daemon could cancel
an already completed provider and discard its rc-0 result. The unchanged census
CI regression reproduced exactly `('cancelled', 130)` instead of
`('succeeded', 0)` locally on Python 3.14.

Exit receipts now take precedence over wall enforcement. Accounting uses each
attempt's recorded start and known provider end; only a provider that may still
be running is charged through now. Finalizing attempts cannot be wall-cancelled,
and terminal acceptance preserves the provider's `finished_at`. The job's own
`finished_at` still records settlement. A self-exited provider keeps its own
outcome even after an overrun; its attempt records `wall_overrun` with total
running time, the cap, excess seconds, provider exit time and the reason. Online
and offline `runs show` expose that evidence. Starting timestamps, waiting
semantics, retry caps, client cancellation attribution and live-provider wall
stops are retained. C-4.2, C-6.4 and the contract's version history state this
rule.

Finalization has **no overall elapsed-time bound today**. Its Git calls have
`workspace_git_timeout_s` (60 s by default); transient salvage makes at most
three tries per daemon, then records its failure and settles; failed workers
have capped retry backoff. Regular-file reads avoid FIFOs, but filesystem walks
and stalled filesystem operations have no overall deadline (C-25.3). Export
runs after acceptance, records errors and releases its leases. None of this
post-exit work spends the provider wall.

The follow-up source audit found two further ways to extend a known end:
quarantine replaced the receipt end with its own timestamp, and receipt-less
finalization captured a verified-empty census end only after reading launch
metadata. Three targeted cases reproduced those errors before the fix. The
quarantine now preserves a receipt or recorded end, and a census end is saved
durably before any fallible finalization work. A regression also interrupts
that work and replays it three days later, proving the saved end survives.

The caller sweep also exposed a pre-existing pin-notice fixture failure in
`test_unadmittable_pin_rereview.py`, reproduced against the base daemon too.
That notice-only case launches no provider but asked the real process census
to establish containment; this sandbox could not verify it, so finalization
correctly deferred. The case now supplies an explicit empty census locally.
Its expected success and notice-withdrawal assertions are unchanged; production
containment is unchanged.

`test_daemon_inspection_load.py` also used a September 24 attempt start against
the current wall clock. Thirteen pacing cases consequently entered the wall's
kill path rather than the inspection path. The attempt and receipt now use the
fixture's current UTC time, while its synthetic process identities stay fixed.
Its inspection assertions are unchanged, and the fixture now rejects any
process or group signalling. The original hold completed in 269.40 s before an
interrupt could be sent; no interrupt was sent and the corrected file is rerun.


A final source review found the same finished-provider rule could be violated
by a parent's wall cancellation cascading into an exited child. Both new cases
failed before this fix. Wall cascades now leave known-exited descendants to
settle and export, while still cancelling dependent work that is running or
waiting. Receipt preflight runs outside the SQL transaction; the latest stored
exit state is rechecked inside it, covering an exit recorded between the two.
Unreadable child receipts do not prevent stopping the parent. The new cases
cover receipt-only and finalizing children, a concurrent finalization, and a
still-running sibling. C-4.2 and C-7.3 state this wall-specific exception to
family cancellation; client cancellation behavior is unchanged.

### Deliberate changes of expectation

- `test_finalization_rechecks_wall_at_terminal_commit` became
  `test_finalization_past_wall_keeps_provider_outcome`: slow salvage that crosses
  the cap still succeeds, spends only the provider's four seconds and records no
  wall cancellation. The provider had already exited.
- `test_finalization_time_is_charged_to_later_attempts` became
  `test_finalization_time_is_not_charged_to_later_attempts`: the first attempt's
  two seconds of provider execution remain two after settlement; a later
  one-second attempt brings both exact accounting and the tick estimate to
  three, excluding finalization.
- The stopped-at-the-cap parametrization now covers starting and running;
  finalizing has a known provider exit and is covered by acceptance and
  no-cancellation regressions instead.
- `test_launch_failure_starts_execution_at_finalization_not_reservation` became
  `test_launch_failure_finalization_spends_no_provider_time`: waiting in
  finalization after a failed launch spends zero and cannot cause a wall kill.
- The bounded 40-example Hypothesis property varies attempt durations, waits,
  per-attempt settlement delays, provider exits and execution states. It cancels
  exactly when a provider still running has summed execution at least the cap;
  another three days of settlement delay cannot change that outcome or charge.

### Runner and commit constraints

The prescribed `uv run` failed before collection: initializing the default
cache was denied at `~/.cache/uv/sdists-v9/.git`. `--no-sync` and selecting an
existing environment did not change that failure. No cache override, cache
clean, dependency installation or new virtual environment was used. The
fallback invokes the existing uv-built Python 3.14.4 interpreter at
`/Users/maxghenis/subfleet-v2/.venv/bin/python`, reading the locked Hypothesis
6.168.1, pytest 9.1.1, sortedcontainers 2.4.0 and pytest-timeout 2.4.0 packages
directly from the **default uv cache** through `PYTHONPATH`. No file outside the
assigned workspace or per-hold scratch was written. This is a deviation from
the requested `uv run` entry point, not a claim that it worked.

The inherited `SUBFLEET_RETENTION_DORMANT=1` disabled the isolated retention
scheduling fixtures, causing 25 failures in `test_native_maintenance_startup.py`.
Setting it to `0` only in the pytest subprocess made all 41 cases pass; the
host environment and live daemon were untouched. Later test subprocesses use
that override, and `test_retention_dormant.py` still explicitly tests both
dormant and active behavior (7 passed).

Each pytest is a single foreground process in its own `lockf -k` hold of
`/private/tmp/claude-501/subfleet-suites.lock`, without a lock timeout or xdist.
`HYPOTHESIS_PROFILE=ci` is set, no deep profile is run, and Hypothesis storage
and XML scratch live under the per-hold `$TMPDIR` and are removed with Python
`shutil.rmtree` after every hold. XML is used only to count per-file outcomes;
no logs, JSON evidence, XML or bundles are staged.

An early parser command used zsh's read-only `status` variable and left one
scratch directory when the shell exited; that directory was subsequently
removed with `shutil.rmtree`. The corrected runner uses `suite_rc`.

The shared Git index was also denied (`index.lock`: operation not permitted).
Commits therefore use workspace-local `.git-local` metadata on
`fix/wall-counts-running-time-217`, based on the assigned detached base. The
original shared checkout metadata is unchanged. Every commit carries the
requested co-author trailer. Nothing is pushed and no sub-agents are used.

### Validation

A replay of the first 32 wall cases, the census file and the original pin-notice
fixture against the base daemon had **19 failed,
27 passed** across the wall, census and pin-notice files. The wall/census subset
was **18 failed, 22 passed**: the unchanged CI test, all six delayed-exit cases,
revised finalization expectations, the property, direct enforcement, quarantine
and census/replay cases all failed as intended. The explicit live-provider-at-
six-hours control already passed on the base, as it should: this behavior is
retained. The additional failure was the pre-existing pin-notice fixture below.

Caller discovery used `rg -l 'test_state_contract|_process_attempt' tests -g
'test_*.py'` and an AST cross-check. There are 45 direct import/call files plus
`tests/e2e/test_stop_bound.py`, whose traceback assertion mentions the method;
the broader 46-file grep set is included in validation, together with all
explicitly requested files.

Latest successful per-file results across **59 files**: **1,497 passed,
3 skipped, 1 existing strict expected failure, 0 failures or setup errors**.
These are each file's latest successful counts, excluding base replays and
mutations. The final-source column distinguishes the 23 completed repeats
(743 passed, 1 expected failure) from the 36 files not repeated after the
family-only fix. Every requested file ran earlier; the stopped CLI/offline
repeat returned no test results and is not counted as completed.

The three skipped cases are two in `test_idle_cost.py` and one in
`test_stop_bound.py`, requiring real sysctl/ps process identity unavailable in
this sandbox. Their real-process behavior remains unverified. The expected
failure is the existing C-6.10 wait-kind case in `test_admission_properties.py`.
Files outside the explicit list and caller-discovery set were not run.

The longest completed pytest invocation took **444.63 seconds**, below the
20-minute hold bound. Lock waits had no timeout and are excluded from pytest's
reported duration. The frontend's eight cases passed before the family-only
fix; its temporary build symlink and scratch were removed. No new environment
or uv cache override was created. Final-source mutation repeats remained
unrun; the three earlier named mutation failures are detailed below.

| File | Passed | Skipped | Expected failure | Rerun on `309c432f6` |
| --- | ---: | ---: | ---: | --- |
| `tests/e2e/test_stop_bound.py` | 2 | 1 | 0 | not repeated |
| `tests/fake/test_admission_disk.py` | 27 | 0 | 0 | yes |
| `tests/fake/test_admission_held_waiters.py` | 1 | 0 | 0 | yes |
| `tests/fake/test_admission_latency.py` | 14 | 0 | 0 | yes |
| `tests/fake/test_admission_lease_waiters.py` | 16 | 0 | 0 | yes |
| `tests/fake/test_admission_liveness.py` | 32 | 0 | 0 | yes |
| `tests/fake/test_admission_priority.py` | 32 | 0 | 0 | yes |
| `tests/fake/test_admission_properties.py` | 27 | 0 | 1 | yes |
| `tests/fake/test_admission_ramp.py` | 19 | 0 | 0 | yes |
| `tests/fake/test_admission_ramp_retention.py` | 11 | 0 | 0 | yes |
| `tests/fake/test_admission_route_isolation.py` | 90 | 0 | 0 | yes |
| `tests/fake/test_admission_stateful.py` | 2 | 0 | 0 | yes |
| `tests/fake/test_admission_visibility.py` | 43 | 0 | 0 | yes |
| `tests/fake/test_batch_label.py` | 9 | 0 | 0 | not repeated |
| `tests/fake/test_canonical_identity_fix2.py` | 21 | 0 | 0 | not repeated |
| `tests/fake/test_census_recycled_pid_daemon.py` | 8 | 0 | 0 | yes |
| `tests/fake/test_codex_turn_completed_fake.py` | 105 | 0 | 0 | not repeated |
| `tests/fake/test_conversation_pools.py` | 2 | 0 | 0 | not repeated |
| `tests/fake/test_daemon_launch.py` | 8 | 0 | 0 | not repeated |
| `tests/fake/test_finalization_replay.py` | 10 | 0 | 0 | yes |
| `tests/fake/test_frozen_stream_nonregular.py` | 32 | 0 | 0 | not repeated |
| `tests/fake/test_idle_cost.py` | 0 | 2 | 0 | not repeated |
| `tests/fake/test_lane_fault.py` | 20 | 0 | 0 | not repeated |
| `tests/fake/test_lane_pilot.py` | 24 | 0 | 0 | not repeated |
| `tests/fake/test_main_refusal_write_dir.py` | 5 | 0 | 0 | not repeated |
| `tests/fake/test_mcp_job_contract.py` | 12 | 0 | 0 | not repeated |
| `tests/fake/test_native_maintenance_startup.py` | 41 | 0 | 0 | not repeated |
| `tests/fake/test_notice_delivery.py` | 15 | 0 | 0 | not repeated |
| `tests/fake/test_notice_header.py` | 23 | 0 | 0 | not repeated |
| `tests/fake/test_picker_native.py` | 3 | 0 | 0 | not repeated |
| `tests/fake/test_policy_sandbox.py` | 25 | 0 | 0 | not repeated |
| `tests/fake/test_probe_recovery.py` | 9 | 0 | 0 | yes |
| `tests/fake/test_reserve_authorization.py` | 12 | 0 | 0 | not repeated |
| `tests/fake/test_reserve_authorization_validation.py` | 11 | 0 | 0 | not repeated |
| `tests/fake/test_resume_contract.py` | 23 | 0 | 0 | yes |
| `tests/fake/test_retention_dormant.py` | 7 | 0 | 0 | not repeated |
| `tests/fake/test_review130_ab.py` | 12 | 0 | 0 | not repeated |
| `tests/fake/test_review_r185_adversarial.py` | 1 | 0 | 0 | not repeated |
| `tests/fake/test_salvage_adversarial.py` | 8 | 0 | 0 | not repeated |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 | 0 | yes |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | yes |
| `tests/fake/test_state_fixture_processes.py` | 4 | 0 | 0 | not repeated |
| `tests/fake/test_turn_listing.py` | 9 | 0 | 0 | not repeated |
| `tests/fake/test_turn_trees.py` | 12 | 0 | 0 | not repeated |
| `tests/fake/test_unadmittable_pin_rereview.py` | 6 | 0 | 0 | not repeated |
| `tests/fake/test_wall_running_time.py` | 35 | 0 | 0 | yes |
| `tests/fake/test_workspace_contract.py` | 8 | 0 | 0 | not repeated |
| `tests/fake/test_workspace_enospc.py` | 1 | 0 | 0 | not repeated |
| `tests/fake/test_workspace_transient.py` | 24 | 0 | 0 | not repeated |
| `tests/fake/test_writable_fanout.py` | 28 | 0 | 0 | not repeated |
| `tests/frontend/test_disk_ruling.py` | 8 | 0 | 0 | not repeated |
| `tests/unit/test_cli.py` | 200 | 0 | 0 | not repeated |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 0 | 0 | not repeated |
| `tests/unit/test_daemon_settle.py` | 43 | 0 | 0 | yes |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 | 0 | not repeated |
| `tests/unit/test_offline.py` | 33 | 0 | 0 | not repeated |
| `tests/unit/test_outcome_unknown.py` | 58 | 0 | 0 | yes |
| `tests/unit/test_settle_properties.py` | 129 | 0 | 0 | yes |
| `tests/unit/test_tick_prefilter.py` | 30 | 0 | 0 | yes |

### Round-2 mutation evidence

Before the final family-cascade addition (`bdebb2406`), all three requested
mutants were caught. Each ran in its own foreground lock hold; the runner
restored the source byte for byte in `finally` and required pytest exit 1.
These probes were not repeated after the final family-only addition; no mutation remains in the workspace.

| Mutation | Named test that failed | Failure |
| --- | --- | --- |
| Charge to now after a known exit | `test_wall_property_counts_only_execution_and_never_stops_waiting` | 2 seconds charged for 1 second of execution; 259,201 for 1 after settlement delay |
| Allow the wall to cancel an exited provider | `test_enforcement_never_cancels_a_known_exit[finalizing]` | `_enforce_wall` returned true |
| Drop the overrun record | `test_exited_provider_settled_three_days_later_exports[running-7]` | Missing `wall_overrun` evidence |

All three named tests are in `tests/fake/test_wall_running_time.py`. Their
healthy versions passed in the full wall file. The first mutation took 97.45 s
of pytest time, the second 2.44 s, and the third 1.07 s.

### Round-2 commit steps

These commits are in `.git-local` on the assigned branch; the report is saved
in a subsequent documentation commit.

| Commit | Step |
| --- | --- |
| `22ed46df9` | Tests first: delayed provider exits, overrun evidence and revised finalization expectations |
| `b2cddf7a8` | Provider-only accounting, receipt-first enforcement, overrun persistence and contract changes |
| `638984f8a` | Tests first: direct enforcement, quarantine, census end and interrupted replay |
| `25e5dcbb2` | Preserve receipt ends through quarantine and durably capture census ends before finalization |
| `b62221f80` | Supply explicit empty containment in the pre-existing notice-only fixture |
| `bdebb2406` | Correct inspection-fixture wall timestamps and refuse signals from synthetic-PID tests |
| `171e594ce` | Tests first: preserve exited children through wall cascades, including a finalization race |
| `309c432f6` | Preserve exited family members, recheck exit state atomically and amend C-7.3 |

| `de04bf646` | Save the implementation and validation checkpoint during shared-lock waits |

## Round 3 (2026-10-15)

Checkpoint; validation is still in progress. Base: `9fad7c4fe2c3`. Work began at
19:41 UTC; the 4 h 30 m checkpoint for starting no more holds is 00:11 UTC.
The full r1 review and both probe files were read. Round 2's provider-exit and
overrun rule is retained, including the release merge's fixture resolutions.

### Findings and implementation

1. Inspection-load confirmation: **22 passed** at the assigned head. The
   `started_at=` sweep, cross-checked against AST calls to `add_attempt` and
   `update_attempt`, found the round-2 current-clock fixes intact. Other fixed
   clocks belong to intentional wall tests, identity/census tests, quarantine
   recovery, and fixed-clock timer tests; those files are included below as
   validation completes.
2. Conversation socket cancels pass the peer into message withdrawal and turn
   interruption. Client events retain the stated session and audit-token pid,
   ignore body pid/by fields, and are saved before a running turn can reach
   containment. Internal message-settled cancellations retain daemon attribution.
   The initial regression run reproduced **6 failures, 1 pass**; the first fix
   passed **7/7**, including immediate running-turn containment. Coverage before
   runner adoption was subsequently added and awaits its full rerun.
3. Receipt-less live attempts are inspected before wall enforcement. Confirmed
   liveness is saved durably as `last_observed_alive_at`; a lost provider's
   charge ends at that observation (the launch for a legacy row without one).
   Observations belong to the attempt's recorded boot, so no later boot or host
   downtime extends the charge. Read-only losses retry under C-4.5. The new
   inspection controls passed **4/4**: dead, unknown and alive providers, plus
   durable liveness followed by a six-hour observation gap.
4. Repeat kills traverse dependent descendants even after a single-job cancel.
   Each newly cancelled child gets its own attributed event; the root's first
   event and request time remain unchanged. Nested cancellation events sit
   inside one family transaction, avoiding a second root cancellation event.
5. Unpinned retries every candidate refuses permanently use C-11.8's existing
   durable notice and grace machinery. Operator holds and provider limits with
   known resets never start this retry grace. A temporarily usable candidate
   ends the episode; later permanent refusal starts a new grace. The existing
   pinned-job behavior and conversation-turn exemption remain intact.
6. Online/offline `runs show`, bare and JSON, label client cancellation metadata
   with `session_id_source: client-stated` and
   `pid_source: socket-peer-audit-token`. Stored audit records remain unchanged;
   historical empty events remain readable.

The review probes are retained in `tests/fake/test_review_r223_wall_probes.py`
and `tests/unit/test_review_r223_conv_probes.py`, with historical finding
assertions updated to the hub's accepted outcomes. Their independent oracle,
150 bounded Hypothesis examples and 2,000 seeded SQL comparisons are retained.
The first wall probe run reproduced **8 failures, 14 passes**: downtime,
repeat-kill cascade, unpinned refusal grace, and the four provenance display
forms failed, while the round-2 controls passed.

### Contract and execution constraints

The D-WALL1 and D-KILL1 paragraphs, C-5.12, C-11.8 and version history now state
inspection-first enforcement, downtime accounting, conversation attribution,
repeat-kill descendants, identity-source labels and permanent retry grace.
C-11.8's stale admission `max_wall_s` ordering is removed.

The shared Git index lock was denied. Commits therefore use workspace-local
`.git-local` metadata on `fix/wall-counts-running-time-217`, with the assigned
head as ancestor; the original `.git` pointer and shared index are unchanged.
Each coherent step is committed with the requested co-author trailer. Nothing
is pushed and no sub-agents or network Git remotes are used.

Tests use `uv run --frozen` on Python 3.14.4, one foreground pytest per
`lockf -k` hold, without a lock timeout or xdist. A workspace virtual environment
was installed because this isolated checkout had no dependencies. The sandbox
cannot write the default uv cache, so each hold uses `UV_CACHE_DIR` inside its
own scratch. `SUBFLEET_RETENTION_DORMANT` is unset, `HYPOTHESIS_PROFILE=ci`, and
Hypothesis storage and XML counting evidence stay inside that scratch. Scratch
is removed with `shutil.rmtree` after each invocation; no evidence is staged.

### Validation status

The combined wall/census/state and review regression hold is running. The
conversation group, settle/pacing/prefilter group, admission groups, CLI/offline,
pin-grace/C-11.8 files, remaining fixture-audit files and four required mutation
checks remain to run. Final per-file counts and omissions will replace this
checkpoint as the holds complete.

## Round 4 (2026-10-16): CI at f07751fc1

Final status, 2026-10-16 09:27 UTC: fixed-source pytest invocations completed for
**4 of 54 selected files: 100 passed**.
**50 selected files remain unrun on the fixed source.** This is a
local validation result, not a full CI run. The first clock observation was
04:51 UTC; the workspace job stamp is 04:46 UTC, so the conservative cutoff for
starting further runs was 09:16 UTC. Lock waits count against that wall.

The no-new-test cutoff was enforced. The queued fixed-salvage invocation
acquired the lock at **09:25 UTC**, returned **77** without starting
pytest, and the foreground runner then exited. No pytest ran after the
cutoff. No further selected-file runs were started. The 50 fixed-source
files marked `unrun` below include every requested broad regression
group. This round did not complete all requested validation.

### Causes and changes

Where a requested base run is unrun below, attribution is based on source and
CI evidence rather than a local differential run.

- Round-3 conversation attribution (`3acd284b0`, `a36f2776e`) exposed stop/title
  fixtures with runners named `host/a1` or `job/a1` without corresponding jobs or attempts in their real job store. Parsing those
  attempt ids produced the correct job id, but round 3's new attribution write
  could not find that job. The fixtures now create the missing rows and retain
  every existing stop, title ordering and concurrency assertion. The legacy
  hold double records the newly required cancellation request.
- The frontend harness passed integer pids to an interface that receives a
  saved audit token. `3acd284b0` began reading its pid, exposing the fixture
  mismatch. The harness now supplies audit tokens for direct calls and
  reads them from sockets. Its cancellation seam records requests. The protocol
  stand-in runner now has an attempt id, and the lossless round-trip test also
  asserts the client attribution. Production captures `peer_token(conn)` at
  accept and forwards it unchanged through the reader and request dispatcher.
  No application reply field or wire shape is changed.
- Round 3's `0c86a27ad` made `_record_owned` write a second
  `attempt.processes_recorded` event
  whenever the observation clock advanced, even when membership did not change.
  It also materialized an empty ownership map when a living guardian had left
  its recorded group. Liveness still advances durably from the pass's existing
  inspection; unchanged membership uses `attempt.observed_alive`, and no empty
  ownership claim is created. The pacing regression now advances its clock
  deterministically and checks both the updated observation and the original
  ownership-event count.
- The quarantine reaping failure predates round 3: its test and `_quarantine`
  have identical syntax trees at `9fad7c4fe` and `f07751fc1`. Its `SimpleNamespace`
  double lacked the legitimate receipt reader and root; both are now supplied
  using isolated scratch. Original reaping assertions are unchanged. Its base
  and fixed-source executions remain unrun, so this classification is based on
  the source comparison and the reproduced head failure.
- The admission machine's FIFO oracle omitted `f91b3ee4f`'s new
  `lanes-unadmittable` hold. A retry whose two transient failures excluded the fleet's only lane correctly
  holds nobody back (C-11.8). The oracle now applies that rule and validates the
  hold's required evidence. A deterministic replay retains CI's minimized trace.
  This changes the old oracle's assumption that an unrecognized hold competes;
  it does not relax FIFO for a job that can use a lane. The minimized CI trace logs
  `ok` twice, but its plain fake Codex stream has no `turn.completed`, so the
  existing acceptance rule downgrades those success claims to transient. The
  dedicated regression requests two transient outcomes explicitly.
- The held-start salvage fixture also exhausted its only lane after two
  transients. `f91b3ee4f`'s permanent-refusal check runs before workspace
  preparation, so no a3 baseline was written. That fixture now supplies a spare
  eligible lane before applying a zero fleet cap so it can reach the intended
  held start. All ref, notice and work-preservation assertions remain. The fixed
  file was not executed before the cutoff.

### Validation

- `9fad7c4fe`: 5 completed files, 146 passed.
- `f07751fc1`: 8 locally completed files, 249 passed, 17 failed.
- Fixed source: 4 completed files, 100 passed.

All local runs used Python 3.14.7 free-threading. Python 3.12 and CI's other
Python 3.14 build were not rerun. The head-side legacy-hold, stateful-admission
and salvage failures use the supplied CI logs; those three files were not
rerun locally at the head after prolonged lock waits. No local passing count
is inferred from CI. Each completed base/head/fixed result below is counted
once per file and version. The stateful file has one additional round-4 case.

Each pytest ran in the foreground, alone under its own `lockf -k` hold of
`/private/tmp/claude-501/subfleet-suites.lock`, without a lock timeout or xdist.
The longest reported pytest runtime was 131.42 s;
that duration excludes waiting for the shared lock. Later runs logged lock
acquisition and checked the cutoff before starting pytest. Hypothesis used the
bounded `ci` profile and task scratch storage. The default uv cache is outside
writable roots, so a disposable cache inside task scratch was used.

Rows marked `unrun` are the complete remaining list by file for that version;
`—` means no base/head comparison was requested for a regression-only file.
Skipped cases were not executed. Tests outside these 54 selected files were
not run. No logs, JSON evidence or bundles are staged.

| File | 9fad7c4fe | f07751fc1 | Round 4 |
| --- | --- | --- | --- |
| `tests/fake/test_admission_disk.py` | — | — | unrun |
| `tests/fake/test_admission_held_waiters.py` | — | — | unrun |
| `tests/fake/test_admission_latency.py` | — | — | unrun |
| `tests/fake/test_admission_lease_waiters.py` | — | — | unrun |
| `tests/fake/test_admission_liveness.py` | — | — | unrun |
| `tests/fake/test_admission_priority.py` | — | — | unrun |
| `tests/fake/test_admission_properties.py` | — | — | unrun |
| `tests/fake/test_admission_ramp.py` | — | — | unrun |
| `tests/fake/test_admission_ramp_retention.py` | — | — | unrun |
| `tests/fake/test_admission_route_isolation.py` | — | — | unrun |
| `tests/fake/test_admission_stateful.py` | 2 passed | unrun | 3 passed |
| `tests/fake/test_admission_visibility.py` | — | — | unrun |
| `tests/fake/test_census_recycled_pid_daemon.py` | — | — | unrun |
| `tests/fake/test_retry_for_good_refusals.py` | — | — | unrun |
| `tests/fake/test_review_r223_wall_probes.py` | — | — | unrun |
| `tests/fake/test_salvage_finalization.py` | 47 passed | unrun | unrun |
| `tests/fake/test_state_contract.py` | — | — | unrun |
| `tests/fake/test_wall_inspection_order.py` | — | — | unrun |
| `tests/fake/test_wall_running_time.py` | — | — | unrun |
| `tests/frontend/test_core_app_cutover_reading.py` | — | — | unrun |
| `tests/frontend/test_core_approval_notification.py` | — | — | unrun |
| `tests/frontend/test_core_approval_reach.py` | — | — | unrun |
| `tests/frontend/test_core_client.py` | — | — | unrun |
| `tests/frontend/test_core_diff.py` | — | — | unrun |
| `tests/frontend/test_core_draft.py` | — | — | unrun |
| `tests/frontend/test_core_idle.py` | — | — | unrun |
| `tests/frontend/test_core_live.py` | — | — | unrun |
| `tests/frontend/test_core_live_failure.py` | — | — | unrun |
| `tests/frontend/test_core_markdown.py` | — | — | unrun |
| `tests/frontend/test_core_outbox.py` | unrun | 10 passed, 3 failed | unrun |
| `tests/frontend/test_core_protocol.py` | 9 passed | 8 passed, 1 failed | 9 passed |
| `tests/frontend/test_core_question_card.py` | — | — | unrun |
| `tests/frontend/test_core_search.py` | — | — | unrun |
| `tests/frontend/test_core_steer.py` | unrun | 41 passed, 2 failed | unrun |
| `tests/frontend/test_core_steer_properties.py` | — | — | unrun |
| `tests/frontend/test_core_store.py` | — | — | unrun |
| `tests/frontend/test_core_suggestions.py` | — | — | unrun |
| `tests/frontend/test_core_suggestions_r1b.py` | — | — | unrun |
| `tests/frontend/test_core_text_scale.py` | — | — | unrun |
| `tests/frontend/test_core_timeline.py` | — | — | unrun |
| `tests/frontend/test_core_titles.py` | — | — | unrun |
| `tests/unit/test_census_responsibility_failures.py` | unrun | 19 passed, 1 failed | unrun |
| `tests/unit/test_conversation_handoff.py` | — | — | unrun |
| `tests/unit/test_conversation_service.py` | — | — | unrun |
| `tests/unit/test_conversation_steer_service.py` | 45 passed | 43 passed, 2 failed | 45 passed |
| `tests/unit/test_conversation_titles.py` | unrun | 85 passed, 4 failed | unrun |
| `tests/unit/test_daemon_inspection_load.py` | — | — | unrun |
| `tests/unit/test_daemon_settle.py` | 43 passed | 41 passed, 2 failed | 43 passed |
| `tests/unit/test_inspection_pacing_properties.py` | — | — | unrun |
| `tests/unit/test_legacy_hold.py` | unrun | unrun | unrun |
| `tests/unit/test_review_r223_conv_probes.py` | — | — | unrun |
| `tests/unit/test_settle_properties.py` | — | — | unrun |
| `tests/unit/test_tick_prefilter.py` | — | — | unrun |
| `tests/unit/test_title_review_regressions.py` | unrun | 2 passed, 2 failed | unrun |

### Commit storage and delivery

The shared index refused `index.lock` with `Operation not permitted`. Commits
use workspace-local `.git-local`, on `fix/wall-counts-running-time-217`, with
`f07751fc179f` as ancestor. The original `.git` pointer and shared index remain
preserved. Every commit carries the requested Claude Opus 5.5 co-author trailer.
No history was rewritten and nothing was pushed. Source and checkpoint commits
are below; the containing report commit follows them.

| Commit | Step |
| --- | --- |
| `5942d2a77` | test: give cancellation fixtures job identities and socket audit tokens |
| `0050f2e88` | test: separate liveness observations from process ownership changes |
| `2ab39bcdb` | fix: keep paced liveness updates distinct from ownership evidence |
| `62e71c067` | test: retain CI provider-cap trace for an exhausted retry lane |
| `e764f8f3b` | test: apply permanent retry refusal semantics to admission and salvage fixtures |
| `1fc28e91e` | docs: checkpoint round-four CI causes and fixture corrections |
| `5adb653cb` | test: drive permanent retry contention in a bounded Hypothesis context |
| `fd014f227` | test: advance inspection observations relative to the fixture clock |
| `40bb6b554` | test: retain numeric pids in frontend person verdicts |
| `671953e02` | docs: record initial CI reproductions and the conservative test cutoff |
| `917d4cd54` | docs: prioritize base and fixed-source runs after prolonged lock waits |
| `5c8f7cacd` | docs: record the first passing base-head-fixed cancellation comparison |
| `b796695c0` | docs: record verified ownership fix and full per-file validation status |
| `3f98e363a` | docs: record lossless protocol verification and current test ledger |
| `fd3e5be3f` | docs: record bounded admission regression and one hundred passing tests |
| `ca7665e37` | docs: stop test starts at the wall checkpoint and list unrun files |

Delivery: `.git-local/round4.bundle`, prerequisite `f07751fc179f`, advertising
`refs/heads/fix/wall-counts-running-time-217`. The final report commit is the bundle tip;
`git bundle verify` and `git bundle list-heads` verify its prerequisite and
advertised branch before delivery.

Cleanup complete: the foreground runner exited; task scratch (including the
private uv cache and Hypothesis storage) and both comparison archives were
removed with Python `shutil.rmtree`. A workspace scan found no `.hypothesis`
folder. The workspace-local Git metadata, delivery bundle and dependency
environment remain for review. No task-owned pytest or runner remains.

## Round 5 (2026-10-16): receipt races, starting recovery, pending requester

Base: `547a164e5f43` (the release/217 union). The requested `review-r2.md`
was absent from the assigned checkout and checked temporary locations; this
round uses the executed inputs quoted in the fix brief. No caller workspace,
live state, network Git remote or sub-agent is used.

### Reproductions and changes

The tests-first run imported the unchanged base implementation and completed
with **9 failed, 2 passed, 17 deselected** in **519.52 s**. By file:
`tests/fake/test_wall_inspection_order.py`: 7 failed, 2 passed;
`tests/unit/test_review_r223_conv_probes.py`: 2 failed. Both shared and fresh
inspection reproduced `cancelled`, rc 130, for a provider exiting at 21,599 s
with inspection returning at 21,601 s against a 21,600 s cap. Receipts arriving
between cancellation and kill settlement failed too. Starting recovery charged
25,200 s after seven hours with neither receipt. Both pending operations
(`message.cancel`, `turn.interrupt`) lost session and audit-token pid 424242.
The running-without-provider and reserved recovery controls passed.

- D-WALL1: enforce the cap only after re-reading the receipt and checking
  current exit status. Re-read again after the kill's first census and its pre-signal commit. A receipt
  found before signaling takes the finalization path. A late, verified success
  at settlement supersedes the wall cancellation and its killed-attempt veto;
  the original request and the supersession remain in the audit trail. Online
  and offline displays select the effective cancellation, including a later
  explicit client request. Existing completion-proof checks still apply.
- Starting recovery: an attempt that never launched ends its accounting at
  its recorded start, contributing zero seconds. Seven-hour recovery tests
  cover starting, running without a provider, and reserved, then one second
  of the next attempt. Bounded properties cover receipt/inspection/cap/kill
  interleavings (at most 32 examples) and absent-provider downtime (at most 20 examples).
- D-KILL1: persist the first requester with the conversation stop in one
  transaction, keyed by message and turn sequence. Pending submission and
  admission repair apply that requester when the job exists. Both operations
  retain the stated session and audit-token pid 424242, including after the
  conversation store is closed and reopened.

### Validation

Final status, 2026-10-16 18:40 UTC:
**647 passed, 6 skipped, zero failed across all 21 requested files.**
No requested file remains unrun. The six skipped legacy-hold cases use the
`process_inspection_available` prerequisite for real process identity reads;
they are unexecuted and are not counted as passes. The new round-5 regressions
all ran and passed. This is local validation on Python 3.14.7 free-threaded;
other Python versions and a full repository CI run were not executed here.

The six fixed-source groups ran separately, in the requested order, each in
its own foreground `lockf -k` hold, without a lock timeout or xdist. The baseline
regression invocation had its own hold too. Fixed-source results and pytest
runtimes: wall 76 / 633.01 s; conversation 205 passed, 6 skipped / 426.73 s;
daemon/inspection 195 / 114.40 s; admission/retry/salvage/replay 68 / 270.64 s;
native frontend 65 / 120.64 s; state/module names 38 / 23.85 s.
All holds completed below twenty minutes; the longest pytest runtime was
633.01 s, excluding the lock wait. The no-new-holds cutoff was 19:33:33 UTC,
conservatively measured from the job's 15:03:33 UTC stamp.

`SUBFLEET_RETENTION_DORMANT` was unset. Hypothesis used bounded ci settings and
scratch storage; no deep profile ran. The unwritable default uv cache was
replaced with a disposable scratch cache. JUnit counting evidence, uv/Swift
module caches and Hypothesis storage were removed after each invocation. The
frontend's short `/tmp` harness directories were tracked and removed without
changing their socket paths. The dependency environment and workspace build
and Python caches were removed after validation. No `.hypothesis` directory,
task-owned pytest, runner or guardian remains. No logs, JSON evidence or bundles
are staged.

| File | Fixed source |
| --- | --- |
| `tests/fake/test_wall_running_time.py` | 35 passed |
| `tests/fake/test_wall_inspection_order.py` | 18 passed |
| `tests/fake/test_review_r223_wall_probes.py` | 23 passed |
| `tests/unit/test_conversation_steer_service.py` | 45 passed |
| `tests/unit/test_conversation_titles.py` | 89 passed |
| `tests/unit/test_title_review_regressions.py` | 4 passed |
| `tests/unit/test_review_r223_conv_probes.py` | 17 passed |
| `tests/unit/test_legacy_hold.py` | 50 passed, 6 skipped |
| `tests/unit/test_daemon_settle.py` | 43 passed |
| `tests/unit/test_daemon_inspection_load.py` | 22 passed |
| `tests/unit/test_inspection_pacing_properties.py` | 1 passed |
| `tests/unit/test_settle_properties.py` | 129 passed |
| `tests/fake/test_admission_stateful.py` | 3 passed |
| `tests/fake/test_retry_for_good_refusals.py` | 8 passed |
| `tests/fake/test_salvage_finalization.py` | 47 passed |
| `tests/fake/test_finalization_replay.py` | 10 passed |
| `tests/frontend/test_core_protocol.py` | 9 passed |
| `tests/frontend/test_core_outbox.py` | 13 passed |
| `tests/frontend/test_core_steer.py` | 43 passed |
| `tests/fake/test_state_contract.py` | 37 passed |
| `tests/unit/test_test_module_names.py` | 1 passed |

### Commit storage and delivery

The shared index rejected `index.lock` with `Operation not permitted`.
Commits use workspace-local `.git-local` on
`fix/wall-counts-running-time-217`, descending from `547a164e5f43`. Named files
only were staged; the original `.git` pointer and shared index are preserved.
Every commit carries `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
No history was rewritten and nothing was pushed.

Delivery: `.git-local/round5.bundle`, prerequisite
`547a164e5f43427cb8a47effd4b8970c6029c707`, advertising
`refs/heads/fix/wall-counts-running-time-217`. `git bundle verify` and
`git bundle list-heads` verify the prerequisite and advertised branch. The
containing final-report commit is the bundle tip; the preceding steps are below.

| Commit | Step |
| --- | --- |
| `adaf12a7b` | test: reproduce round-five wall races and pending cancellation attribution |
| `abc560b26` | test: isolate generated wall histories and release queued examples |
| `31b15f36c` | fix: let provider receipts win wall races and exclude unlaunched downtime |
| `4df9057c3` | fix: persist the requester across pending conversation submission |
| `8decf0cf1` | test: cover durable stop identity and cancellation after a wall race |
| `2f287d214` | fix: display the effective requester after a superseded wall cancellation |
| `1e1dbea88` | docs: record round-five reproductions and validation checkpoint |
| `f9055594a` | test: bound cap interleavings and retain the incomplete-result veto |
| `3606f4bbe` | test: cover a receipt arriving during the pre-signal database commit |
| `37c4161a5` | fix: recheck the receipt after committing the kill intent |
| `a09f5170a` | docs: record seventy-six passing wall and review regressions |
| `ebc1c6e83` | docs: record passing pending-requester and conversation validation |
| `69ac18f27` | docs: record passing daemon settlement and pacing properties |
| `0ad5d1878` | docs: record passing admission, retry, salvage and replay validation |
| `0da9521bc` | docs: record sixty-five passing native frontend tests |

## Fix round 6 (2026-10-18)

Assigned head: `6b9f054b60d8`. The local `release/217` ref,
`b053e3de8f2e`, is already an ancestor of that head; no merge or conflict
resolution was needed. No Git remote was accessed. The saved regression
commit `dcd76ed82` from `wip/pr223-fix6-a1` matched the brief and was retained
by cherry-picking it as `0302816a8` into workspace-local Git metadata.
Its last-receipt injection was refined to target the third read, since the
initial containment census itself reads the receipt.

### Decisions and implementation

- An acknowledged client kill during a pending wall stop records a new
  `job.cancel_requested` with its session, socket audit pid and reason. It
  becomes effective, is shown online and offline, and survives any subsequent
  provider receipt. Repeated client kills retain that client attribution.
- The wall requests only the parent's cancellation. Dependent cancellation
  commits atomically with the parent's final `interrupted` settlement;
  superseding the wall requires no reversal and leaves dependents untouched.
  Operator and client family cancellation remains immediate.
- Guardian exit timestamps use milliseconds and are captured immediately
  after provider wait, before relay or output cleanup. This is an additive
  ISO timestamp change; old receipts remain readable without a protocol bump.
  Signal callbacks capture the signal clock after identity reads and directly
  before the syscall. The durable `attempt.kill_started` intent is enriched
  with the first delivered signal's millisecond `ts` and `signal_at`. A final
  receipt read that avoids signalling records `signal_sent: false`,
  `signal_at: null` and a millisecond `ts`.
- A natural exit proven earlier than every delivered signal keeps its own
  success or failure class, including a failure from the provider's own signal.
  Supersession is applied before reconciling a legacy cached verdict. A post-signal completion remains
  `interrupted`/`unknown`, `killed_by=max_wall_s`, even with `turn.completed`.
  Equal milliseconds, second-precision ties, historical missing signal clocks
  and a crash before signal-clock persistence are conservative.
- A wall-stopped dependent records `killed_by=max_wall_s` and names its parent
  in the outcome detail and notice. Client terminal notices include the
  effective session, audit pid and reason.
- C-9.2, its summary, C-5.12, D-WALL1, D-KILL1 and the effective `runs show`
  metadata wording now describe these decisions. `job.wall_cancel_superseded`
  retains the old wall request in the audit trail; `runs show` exposes
  `cancel: null`, the provider outcome and any overrun evidence until a
  subsequent client request becomes effective.

### Final validation

**1,063 passed, 10 skipped, zero final failures across 32 files.**
Counts below use each file's final successful attempt, without adding repeats.
All available requested files were selected. The six legacy-hold skips and four
guardian-process skips did not execute their bodies: the sandbox cannot meet
C-5.3's process-identity prerequisite. Native process ownership integration is
therefore not claimed as verified. This is local Python 3.14.7 free-threaded
validation; other Python versions and full repository CI were not run.

Every pytest ran alone in a foreground `lockf -k` hold of
`/private/tmp/claude-501/subfleet-suites.lock`, without a lock timeout or xdist.
All holds finished below twenty minutes; the longest pytest took **329.72 s**.
Lock waits counted against this job's six-hour wall. All holds began before
the 03:57:54 UTC no-new-holds cutoff on 2026-10-19, measured conservatively
from the worktree's 23:27:54 UTC job stamp. `SUBFLEET_RETENTION_DORMANT` was
unset. Hypothesis used bounded ci settings and scratch storage, never deep.
The new property has 60 deterministic examples varying provider exit, receipt
publication, inspection/cap handling, client requests and signalling.

| File | Passed | Skipped |
| --- | ---: | ---: |
| `tests/fake/test_wall_running_time.py` | 35 | 0 |
| `tests/fake/test_wall_inspection_order.py` | 18 | 0 |
| `tests/fake/test_review_r223_wall_probes.py` | 23 | 0 |
| `tests/fake/test_notice_header.py` | 23 | 0 |
| `tests/fake/test_wall_cancel_races.py` | 30 | 0 |
| `tests/unit/test_conversation_steer_service.py` | 45 | 0 |
| `tests/unit/test_conversation_titles.py` | 89 | 0 |
| `tests/unit/test_title_review_regressions.py` | 4 | 0 |
| `tests/unit/test_review_r223_conv_probes.py` | 17 | 0 |
| `tests/unit/test_legacy_hold.py` | 50 | 6 |
| `tests/unit/test_daemon_settle.py` | 43 | 0 |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 0 |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 |
| `tests/unit/test_settle_properties.py` | 129 | 0 |
| `tests/fake/test_admission_stateful.py` | 3 | 0 |
| `tests/fake/test_retry_for_good_refusals.py` | 8 | 0 |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 |
| `tests/fake/test_finalization_replay.py` | 10 | 0 |
| `tests/unit/test_guardian.py` | 13 | 0 |
| `tests/process/test_guardian_process.py` | 0 | 4 |
| `tests/frontend/test_core_protocol.py` | 9 | 0 |
| `tests/frontend/test_core_outbox.py` | 13 | 0 |
| `tests/frontend/test_core_steer.py` | 43 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 |
| `tests/unit/test_procs.py` | 48 | 0 |
| `tests/unit/test_conversation_service.py` | 98 | 0 |
| `tests/unit/test_conversation_handoff.py` | 48 | 0 |
| `tests/fake/test_turn_trees.py` | 12 | 0 |
| `tests/fake/test_codex_turn_completed_fake.py` | 105 | 0 |
| `tests/unit/test_census_survivors_review.py` | 14 | 0 |
| `tests/unit/test_census_recycled_pid.py` | 25 | 0 |

The retained regressions first reproduced **12 failures** (**11** wall races,
**1** guardian timestamp), alongside 12 existing guardian passes. Subsequent
regressions caught and closed an absent caller session in immediate notices,
a late turn completion after client precedence, a stale cached failure kept
authoritative by a superseded wall, and an intent left pending after a skipped
signal. Provider-owned signals before the daemon signal reproduced four
failures; both post-signal controls already passed. All **30** wall-race cases
now pass, including those six signal-order controls, precision/recovery cases,
and atomic cascade rollback/replay.

Intermediate validation also caught conversation/census/state fixture seams
that needed the effective-requester lookup or the signal-clock callback; their
ownership and attribution assertions were preserved. One wait-hub timing
assertion observed only one read during its 600 ms deadline. The entire
inspection-load file then passed all **22** cases without changing wait-hub
behavior. The final wall/inspection-load hold passed **151** tests; the
admission/retry/salvage/replay and conversation-service hold passed **166**;
the guardian/procs/census hold passed **100**, with four integration skips;
and the native frontend passed **65**.

The separately supplied `review-r3.md` and
`review-r3-probes/test_review_r223_r3_probes.py` were absent from the checkout
and task scratch area. Their location was requested and not provided. The
review text has not been read and its separate probe file is **unrun**. The
tracked `tests/fake/test_wall_cancel_races.py` retains and extends the saved
executed-input regressions named in the brief. No other requested file remains
unselected.

### Cleanup and local delivery

JUnit evidence, Hypothesis storage and the fallback uv cache were removed
from each invocation's scratch with Python `shutil.rmtree`. The 57 short
frontend harness roots created by this run were identified by the run's
compiled-probe time, exclusive test hold, and its 4/12/41 fixture cases, then
removed after the frontend exited. Other temporary roots were preserved.
Task-created dependency, build and Python caches were removed. No
`.hypothesis` directory or task-owned pytest, runner, guardian or lock holder remains.
No logs, JSON evidence or bundles are staged.

The sandbox rejected writes to the shared `ORIG_HEAD` and index. All new
commits use workspace-local `.git-local` on
`fix/wall-counts-running-time-217`; the original `.git` pointer and shared
index are preserved. Only named source, test, contract and report files were
staged. Every commit carries
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
No history was rewritten and nothing was pushed.

Delivery is `.git-local/round6.bundle`, advertising
`refs/heads/fix/wall-counts-running-time-217` with prerequisite
`6b9f054b60d890e9ceb5b1f5313c2bbda314ff86`. Bundle verification and its advertised
head were checked. The commit containing this final report is the bundle tip;
the preceding round-6 steps are listed below.

| Commit | Step |
| --- | --- |
| `0302816a8` | test: reproduce pending wall cancellation and signal-order races |
| `80fbd7a4f` | test: pin client precedence and the signal timestamp boundary |
| `e8c142ff5` | fix: preserve client kills and defer wall cascades until settlement |
| `4a2a2e553` | fix: settle wall races using provider exit and signal timestamps |
| `968c81f82` | test: vary receipt publication independently of provider exit |
| `26aaea769` | docs: specify wall receipt precedence and deferred cancellation |
| `2d650b6e6` | test: cover deferred settlement replay and conservative signal recovery |
| `56cd03787` | fix: retain wall notices for dependents without a caller session |
| `c5dcdaa6c` | test: keep late turn completion vetoed when a client replaces the wall |
| `41f24466a` | fix: keep the signal veto when a client replaces a turn wall stop |
| `3d2270c61` | test: bind effective requester lookup in conversation daemon fixtures |
| `77f3cee60` | test: reconcile cached provider verdicts after a receipt wins the wall |
| `00ae4b5fe` | test: adapt containment fakes to fresh requests and signal audit writes |
| `739910c00` | fix: clear a superseded wall before reconciling cached provider verdicts |
| `1dcefa247` | test: distinguish skipped wall signals from pending kill intents |
| `57cca1315` | fix: record skipped wall signals explicitly in the kill audit |
| `805d1f96c` | test: preserve a provider failure by its own pre-wall signal |
| `33537bb1c` | test: retain wall attribution for provider signals after SIGTERM |
| `6a576327d` | fix: preserve provider failures that precede the wall signal |
| `9ba573c74` | docs: record passing round-six wall and recovery validation |
| `648caef3f` | test: accept the signal clock callback in the owned-survivor fixture |

## Fix round 7 (2026-10-18)

Assigned head: `352f4c243dbb`. Its second parent is the locally available
`origin/release/217`, `3fbc4b7c5615`; that release head has already been merged.
The local `release/217` branch is older (`b053e3de8f2e`). No further merge was
needed, and no network remote was accessed.

### Interaction and combined rule

`f91b3ee4f` checks an attempted, unpinned retry with
`unadmittable(..., permanent_only=True)` before FIFO and route evaluation. It
passes the original job plus retry exclusions, before `_route_job` attaches
its live capacity steer. Thus it already judges the whole original chain,
as #207 requires. Once every Codex lane is disabled, an Astra-only build
correctly enters durable refusal grace even though its current routed chain
is empty. The semantic merge lost the `for_good` diagnostic in that new
`lanes-unadmittable` hold. It also retained #207 test expectations that a due
job must reach reservation checks, which the durable grace deliberately
preempts. The baseline reproduced eight failures: four missing `for_good`
and four expecting reservation checks that never ran; the minimized provider
peer regression and eight existing durable-grace cases passed.

C-11.8's durable episode wins over reservation evaluation when all original
candidates are permanently refused: it is checked every pass, retains the
standing refusal in `for_good`, keeps no FIFO slot, notifies once and expires
with rc 3 after its durable grace. A live steer changes only routing and
current model demand, never the chain used to establish permanent refusal.
An empty routed chain alone is temporary. Any original candidate with only
an operator hold or known provider reset prevents permanent retry grace,
even when steered away; a far timed hold may still release FIFO under the
existing standing-refusal rule. Recovery ends an episode; the steer ending
or a restart does not renew its grace. C-9.5 and C-11.8 now state the
combined rule explicitly.

The existing eight disabled-chain cases were deliberately updated, not
weakened silently: they now require no reservation checks, `for_good`, a
durable episode, one notice event, replayed episode state and rc-3 expiry
without renewing the episode when the steer ends. They retain every younger
job's slot and selected-model assertions. Eight added far-operator-hold cases
retain #207's original due and route-moved empty-decision checks without
triggering permanent grace. The two corresponding mutation selections are
retargeted to those reachable cases. Two mixed-chain regressions check that
a temporarily closed steered candidate prevents permanent grace despite
disabled remaining candidates.

### Finalization timing

The merged finalizer already uses the provider receipt's `finished_at` for
both the attempt row and the steer's origin, with the persisted observed
provider end as fallback. Classification, salvage and replay do not renew
that origin. However, `_later` truncated fractional seconds, shortening a
900-second steer by the provider exit's fractional second. Eight new cases
cross second/millisecond exits, 120/901-second salvage delays and normal/
replayed finalization. They require exactly 900 seconds from the provider
exit and immediate expiry after a delay beyond that end. `_later` retains
fractional precision; second-precision timestamps retain their existing
format.

### Final validation

**796 passed, 8 skipped, zero final failures across 22 files.** Counts below
use the final complete-file runs, excluding baseline failures, repeated
regressions and the intentional mutation failures. The test-first run
reproduced **12 failed, 6 passed**: eight missing durable hold diagnostics and
four millisecond-exit cases measuring 899.877 seconds rather than 900. The
four second-precision timing controls and two mixed-chain timed-candidate
controls already passed.

All six requested suite groups ran in separate foreground `lockf -k` holds
of `/private/tmp/claude-501/subfleet-suites.lock`, with no lock timeout, no
xdist and no concurrent pytest processes. Every hold completed below twenty
minutes; the longest full pytest was **262.31 s** (admission). Lock waits
counted against the job wall. All holds started before the 06:10:54 UTC
no-new-holds cutoff on 2026-10-19, measured from the worktree's 01:40:54 UTC
job stamp. `SUBFLEET_RETENTION_DORMANT` and `PYTHONPATH` were unset.
Hypothesis used bounded ci settings, never deep. Python was 3.14.7
free-threaded. Full repository CI and other Python versions were not run.

The six legacy-hold process tests and both capacity-steer end-to-end tests
were selected but skipped before their bodies: the sandbox cannot read
macOS boot identity for the required C-5.3 sysctl/ps inspection. Their real
process behavior is **unverified**; no process-inspection bypass was used.

The requested `tests/fake/test_unadmittable_pin.py` does not exist. The hold
included the existing `tests/fake/test_unadmittable_pin_admission.py` and
`tests/unit/test_unadmittable_pin.py`, plus all eight
`tests/fake/test_retry_for_good_refusals.py` durable-grace cases.

| File | Passed | Skipped |
| --- | ---: | ---: |
| `tests/fake/test_capacity_steer_daemon.py` | 72 | 0 |
| `tests/unit/test_capacity_steer_route.py` | 22 | 0 |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 0 |
| `tests/fake/test_admission_stateful.py` | 12 | 0 |
| `tests/fake/test_admission_route_isolation.py` | 93 | 0 |
| `tests/unit/test_scheduler.py` | 97 | 0 |
| `tests/fake/test_unadmittable_pin_admission.py` | 24 | 0 |
| `tests/unit/test_unadmittable_pin.py` | 12 | 0 |
| `tests/fake/test_retry_for_good_refusals.py` | 8 | 0 |
| `tests/fake/test_wall_running_time.py` | 35 | 0 |
| `tests/fake/test_wall_cancel_races.py` | 30 | 0 |
| `tests/fake/test_wall_inspection_order.py` | 18 | 0 |
| `tests/fake/test_notice_header.py` | 23 | 0 |
| `tests/fake/test_review_r223_wall_probes.py` | 23 | 0 |
| `tests/fake/test_finalization_replay.py` | 10 | 0 |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 |
| `tests/unit/test_daemon_settle.py` | 43 | 0 |
| `tests/unit/test_settle_properties.py` | 129 | 0 |
| `tests/unit/test_legacy_hold.py` | 50 | 6 |
| `tests/e2e/test_capacity_steer_e2e.py` | 0 | 2 |
| `tests/fake/test_state_contract.py` | 37 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 |

The two retargeted empty-decision mutations were also run, sequentially,
each named regression under its own separate lock hold. Both were killed:
omitting the due-look guard failed
`test_r7_an_empty_steered_chain_keeps_no_slot_during_a_far_operator_hold[due-due-look-one-slot]`;
omitting the route-moved guard failed its
`[due-route-moved-one-slot]` case. The mutation tool restored the exact source
bytes after each check. The full mutation matrix was not run.

### Cleanup

Each invocation's scratch, JUnit evidence, Hypothesis storage and fallback uv
cache were removed with Python `shutil.rmtree`. The restricted sandbox cannot
write the default uv cache, so cache storage stayed inside task scratch.
Task-created dependency and Python caches were removed after validation.
No `.hypothesis` directory, task-owned runner, pytest, guardian or lock holder
remains. Only the small workspace-local Git metadata, delivery bundle and
local validation summaries remain as ignored delivery artifacts.

### Local commits and delivery

The shared Git index rejected a write with `Operation not permitted`. All
round-7 commits therefore use workspace-local `.git-local` metadata on
`fix/wall-counts-running-time-217`; the original `.git` pointer and shared
index are preserved. Only named test, source, contract, mutation-tool and
report files are staged. No logs, JSON evidence or bundles are staged.
Every commit carries the required Claude Opus 5.5 coauthor trailer.
No history is rewritten and nothing is pushed.


The local delivery is `.git-local/round7.bundle`, advertising
`refs/heads/fix/wall-counts-running-time-217`, with prerequisite
`352f4c243dbba6b6fa82b349f987913e2594d308`. The bundle includes the commit
containing this final report as its tip. Bundle verification and the
advertised head were checked. The preceding steps are:

| Commit | Step |
| --- | --- |
| `35f5e5624` | Reconcile the eight disabled-chain tests and add exact provider-exit timing regressions |
| `b7997acb7` | Preserve empty-decision route coverage with far timed holds |
| `693d8fcc8` | Specify the combined C-9.5 and C-11.8 rule |
| `a7c3c34fc` | Restore `for_good`, retain fractional steer timing, and retarget mutation probes |
| `e43202baf` | Record the interaction and reproduced failures |


## Round 8 — every stop delivery participates in wall receipt ordering (2026-10-19)

### Release integration and test-first evidence

Merged the existing local `origin/release/217` ref at `d72597b5c`
(including #230) before editing. No network remote was contacted. The only
conflict was the version-3 change log in `docs/acceptance-contract.md`:
kept both the wall-running-time entry and the incoming CI-flakes entry.
All clauses from both sides remain. Merge commit: `4f49eb594`.

The shared Git metadata rejected the merge's `ORIG_HEAD.lock` write with
`Operation not permitted`. This round therefore commits on
`fix/wall-counts-running-time-217` in workspace-local `.git-local`, borrowing
read-only base objects. The original `.git` pointer and shared index remain
unchanged. Every commit has the requested Claude Opus 5.5 trailer; only named
files are staged. No history rewrite or push.

The test-first commit `746a6edb1` adds real `RelayServer.apply` regressions
with a synthetic child and a task-owned pipe, without process signals or
runner threads. Initial merged-source run: **12 failed**. An independent
hold temporarily restored every changed `subfleet/` source file to exact
`1909a53c1` bytes, ran the four requested regressions, and restored the fixed
bytes in `finally`: **4 failed, 8 deselected**. The relay SIGINT, provider
interrupt only, and crash-before-transmission cases all wrongly succeeded.
The pre-delivery receipt control fails at its missing delivery-audit assertion;
its successful provider outcome remains the required behavior.

### Delivery audit design

`TurnRunner._transmit` commits a durable `attempt.stop_delivery` intent with
`via=relay`, frame operation, tag and sequence before transmitting a provider
interrupt (`tag=interrupt`), any `signal` frame, or `close`. The callback is
installed by ordinary conversation adoption before the thread starts and is
also installed by `ConversationService.stop` before queuing a wall stop.
The sibling event avoids interfering with the kill protocol's selection of
its latest `attempt.kill_started` event during concurrent runner activity.
Both kinds are read by `_receipt_precedes_signal` under the same rules.

After the intent commits, a millisecond clock is sampled immediately before
the socket write, after connection setup and frame encoding. A fallback before
the relay call keeps direct test transports observable; production refreshes
it through `RelayClient.before_send`, which is scoped to that stop frame. A
separate test-first regression fails when connection time is charged to the
delivery clock and proves that an exit during connection setup still wins. A successful acknowledgement enriches that intent with
`signal_sent=true` and `signal_at`. A crash before transmission, lost reply,
uncertain/partial write or failed enrichment leaves a conservative pending
intent. Local oversized-frame refusal, relay `no-child` for a signal and
already-closed stdin for a close prove no delivery and record
`signal_sent=false`. An earlier uncertain attempt is never overwritten by a
retry. Missing clocks, exact ties and second-precision ties retain the same
conservative rules as group signals.

The wall request time is not used as delivery time: `_request_cancel`
commits first, `ConversationService.stop` queues a command, and the runner
transmits it later. An exit proven before the first delivery still supersedes
the wall, even if the receipt becomes visible only after transmission.
A restart after an intent commits but before the first transmission keeps
cancellation, because no acknowledgement durably proves the intent harmless.
The restart regression reconstructs an actual `Daemon`, discarding all
runner memory.

### Other relay paths checked

- `turn.interrupt`: `_interrupt` persists the client stop and effective job
  cancel before queuing the adopted runner's interrupt. Its provider write,
  timed SIGINT, late SIGINT, close and any future signal tag use `_transmit`.
- `message.cancel`: `_cancel` withdraws only a message/steer that has not
  reached the provider, with no relay stop; otherwise it refuses `too-late`
  and directs the caller to `turn.interrupt`. A withdrawn message that a
  concurrent adoption observes ends before sending; its close is audited.
- C-26.6 replay: adoption installs the same callback before `start`. A stop
  queued after stdout catch-up and any re-sent interrupt use `_transmit`;
  an already-written frame is skipped, retaining its original durable audit
  rather than inventing a later delivery clock. Lost-ack intents survive.
- Timer and error paths: normal and late SIGINT, stdin closes after results,
  withdrawal/refusal, and later escalation frames all enter `_transmit` by
  operation, regardless of their tag. C-5.6 containment retains the existing
  group-signal audit. Provider interrupt is selected by the tag both pure
  drivers emit (`ClaudeTurn` and `CodexTurn`).

C-7.3 now states the implemented distinction precisely: client/operator
cascades share the request transaction; a wall cascade shares the parent's
interrupted settlement transaction and preserves known-exited descendants.
C-9.2 and the version-3 change log document the expanded delivery audit.

### Validation status

The existing bounded wall interleaving property now mixes real relay frames
(provider interrupt, signal or close) before kill-protocol group signals. A
companion property permutes group, interrupt, SIGINT, late SIGINT and close
with millisecond/legacy-second receipts, checking that supersession holds
exactly when the exit strictly precedes the earliest delivery.

The focused final delivery run passed **14 tests** (13 delivery regressions
and the extended wall interleaving property). The adoption/wiring hold passed
**3 tests**. The connection-delay test-first hold failed **1 test** with a
clock two seconds too early before its fix. The initial full unit hold had
**189 passed, 1 failed** because the newly added replay fixture omitted the
required `requester` argument; that setup was corrected and both adoption
cases passed. The first settlement hold had **273 passed, 6 failed,
6 skipped**: the legacy-hold suite's separate dictionary-backed fake daemon
lacked the new audit callback. Its recording stub now implements the delivery
boundary, and its held/adopted turn test also asserts that close/interrupt
acknowledgements were audited. Real-store fixtures continue to exercise the
production SQL recorder. These exploratory holds are excluded from the final
totals.

Mutation: removing `attempt.stop_delivery` from `_receipt_precedes_signal`'s
SQL kind filter made
`tests/fake/test_wall_stop_deliveries.py::test_relay_sigint_must_not_be_superseded_as_no_signal`
fail (**1 failed**), with the original `succeeded`/`ok` outcome after real
relay SIGINT. The mutation ran alone under its own foreground lock hold;
its `finally` restored the exact daemon bytes, verified by SHA-256 and a clean
source diff. No mutation remains.

**Final complete-file runs: 845 passed, 10 skipped, and 5 expected historical
failures across 25 files. No unexpected final failures.** All 55 current r4
probes passed, including `test_r4_relay_sigint_must_not_be_superseded_as_no_signal`.
The unchanged r3 file retained its one historical rc-0 timestamp-tie failure;
the unchanged original #207 file retained its four due routing-call expectation
failures. Neither historical expectation was altered. The tracked #207 suite's
no-slot, younger-job model, and `for_good: ["disabled"]` checks remain unchanged
and pass. Reviewer files were copied into scratch and verified byte for byte
against the supplied originals, then run in a separate pytest process from
`tests/`, loading test isolation only once.

| File | Passed | Skipped | Expected failures |
| --- | ---: | ---: | ---: |
| `test_review_r223_207_original.py` | 4 | 0 | 4 |
| `test_review_r223_r3_probes.py` | 8 | 0 | 1 |
| `test_review_r223_r4_probes.py` | 55 | 0 | 0 |
| `tests/fake/test_admission_stateful.py` | 12 | 0 | 0 |
| `tests/fake/test_capacity_steer_daemon.py` | 72 | 0 | 0 |
| `tests/fake/test_finalization_replay.py` | 10 | 0 | 0 |
| `tests/fake/test_notice_header.py` | 23 | 0 | 0 |
| `tests/fake/test_review_r223_wall_probes.py` | 23 | 0 | 0 |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 |
| `tests/fake/test_wall_cancel_races.py` | 30 | 0 | 0 |
| `tests/fake/test_wall_inspection_order.py` | 18 | 0 | 0 |
| `tests/fake/test_wall_running_time.py` | 35 | 0 | 0 |
| `tests/fake/test_wall_stop_deliveries.py` | 13 | 0 | 0 |
| `tests/process/test_guardian_process.py` | 0 | 4 | 0 |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 0 | 0 |
| `tests/unit/test_capacity_steer_route.py` | 22 | 0 | 0 |
| `tests/unit/test_conversation_service.py` | 100 | 0 | 0 |
| `tests/unit/test_daemon_settle.py` | 43 | 0 | 0 |
| `tests/unit/test_guardian.py` | 13 | 0 | 0 |
| `tests/unit/test_legacy_hold.py` | 50 | 6 | 0 |
| `tests/unit/test_relay.py` | 14 | 0 | 0 |
| `tests/unit/test_settle_properties.py` | 129 | 0 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 |
| `tests/unit/test_turn_runner.py` | 76 | 0 | 0 |

Every requested group ran in its own foreground `lockf -k` hold of
`/private/tmp/claude-501/subfleet-suites.lock`; no lock timeout, xdist,
concurrent pytest process, detached runner or load-wait under the lock was
used. Each hold finished below twenty minutes; the longest final pytest was
94.75 seconds. Lock waits counted against the job wall. Every hold began
before the 08:37:31 UTC no-new-holds cutoff on 2026-10-19 (4 h 30 m after this
job's 00:07:31 EDT worktree stamp). All requested runs completed by
04:47:51 UTC. `SUBFLEET_RETENTION_DORMANT` and `PYTHONPATH` were unset.
`HYPOTHESIS_PROFILE=ci`; the ordering properties use explicit bounded settings
(60 examples), never deep. Python was 3.14.7 free-threaded.

The six native legacy-hold cases and four native guardian-process cases were
selected but skipped before their bodies. The sandbox denies the C-5.3
`ps`/`sysctl` inspection needed to establish macOS boot/process identity.
No ownership bypass was used; their native behavior is unverified. The
existing relay glob has only `tests/unit/test_relay.py`, which was run in full.
Full repository CI, other Python builds and real-provider execution were not run.

### Cleanup and local delivery

All task pytest, runner and lockf commands exited. Each hold's TMPDIR,
Hypothesis storage and test homes were removed. The task scratch root,
reviewer copies, JUnit evidence, fallback uv cache, installed `.venv`, and
Python/pytest caches were removed with Python `shutil.rmtree`; no
`.hypothesis` folder remains. The original `.git` pointer is byte-identical
to the pointer saved before setup. Only workspace-local `.git-local` metadata
and its small delivery bundle remain as ignored delivery artifacts.

The local delivery is `.git-local/round8.bundle`, advertising
`refs/heads/fix/wall-counts-running-time-217`, with prerequisites `1909a53c1`
and `d72597b5c`. The final report commit is the bundle's advertised tip.
The bundle and advertised head were verified locally. The shared release
tracking ref was checked again after validation and remains `d72597b5c`;
no network remote was contacted. No logs, JSON evidence or bundles were staged.
No shared Git index or caller checkout was written, no history was rewritten,
and nothing was pushed.

| Commit | Step |
| --- | --- |
| `4f49eb594` | Merge release #230, retaining both contract change logs and all clauses |
| `746a6edb1` | Add the four required failing regressions and delivery ordering cases |
| `ff0b1cb64` | Audit relay stops, extend mixed ordering/adoption coverage, clarify C-7.3/C-9.2 |
| `df55aefaa` | Add the failing slow-connection receipt-wins regression |
| `6b7da7f88` | Timestamp transmission after connection setup, before the socket write |
| `c3754eba0` | Record the audit boundary in dictionary-backed legacy-hold fixtures |

## Round 9 — explicit stop capability on every conversation frame

The r5 review found that both providers' `cancel-turn` approval replies set
`interrupt_requested`, suppressing the queued wall interrupt, but their
`approval:<id>` tags bypassed the delivery audit. A completed turn two seconds
later could consequently settle as success. The round-8 assertions and separate
kill bookkeeping remain required.

The local `release/217` branch is `b053e3de8`, an older ancestor of
`d72597b5c`; the local `origin/release/217` tracking ref remains `d72597b5c`.
Both are already ancestors of the assigned `fe8bda1ff` head. No new merge or
conflict resolution is necessary;
all existing contract clauses and both histories' change-log entries are kept.
Commits use workspace-local `.git-local` metadata on
`fix/wall-counts-running-time-217`, because the shared Git metadata is read-only.
The caller's checkout and the workspace's original `.git` pointer are untouched.

### Test-first evidence

The first foreground locked hold ran all 12 new approval delivery cases against
the unchanged production head: **12 failed, 13 deselected**. The failures prove
missing durable intents, missing classification, and missing approval delivery
clocks. The provider wire formats are built by the real drivers. The cancel
regressions settle in the publication-to-close window, so a subsequent stdin
close cannot mask an unaudited approval. Separate cases cover escalation through
SIGINT, close and containment after allow or cancel, exit during connection
before cancel transmission, and lost acknowledgements before/after delivery.

The existing bounded delivery-interleaving property now includes both providers'
approval replies for every supported decision. Protocol guards check the reply's
flag against its actual `interrupt`, `cancel` or `abort` payload, including
Codex legacy approvals and non-stopping MCP elicitation cancellation. An AST
guard requires every production `Frame(...)` site to declare its capability.

The guard-only test-first hold also failed all **18 selected tests** (195
other tests deselected), including the sweep's 29 initially unclassified sites.

### Flag design and frame sweep

`Frame.stops` is a keyword-only boolean. It defaults to true for old callers,
so omission cannot silently remove an audit; every production builder explicitly
sets true, false or a decision-dependent boolean. The AST guard rejects any
unclassified production construction even with that conservative default.
The runtime protocol guards reject wrongly classified cancel/abort replies.
`TurnRunner._transmit` selects the existing durable `StopDelivery` callback
solely by `frame.stops`, with no tag/op inference. The intent, post-connection
clock, acknowledgements, uncertain-delivery handling and receipt ordering are
shared with all other stops. `attempt.kill_started` remains separate.

Approval cancellation keeps the driver's existing suppression of redundant
interrupts. Draining the queued wall command still starts `stop_at` before it
asks the driver to interrupt. The timer's SIGINT, close and containment remain
active even when that driver call emits no frame. No escalation clock is reset.
The non-stopping allow reply creates no intent; its queued explicit interrupt
and the full escalation still run. Receipt proof strictly before the approval's
actual transmission clock continues to win.

The following table lists all 29 production `Frame(...)` construction sites.
Provider protocol references are the repository's pinned
`docs/desktop/maps/provider-protocols.md` (approval/interrupt schemas) and
`docs/steer-provider-claims.md` (queued-message cancellation). “Conditional”
rows list each possible result explicitly.

| Construction | stops | Protocol reason |
| --- | --- | --- |
| `claude_turn.py:223` `start` — `init` | no | Initialize the provider protocol; starts no active turn stop. |
| `claude_turn.py:236` `interrupt` — `interrupt` | yes | Interrupt the active turn. |
| `claude_turn.py:264` `steer` — `steer:{message_id}` | no | Queue/steer user input for the active turn; no cancellation. |
| `claude_turn.py:311` `_cancel_frame` — `cancel-steer:{mid}` | no | cancel_async_message drops one queued input; it does not interrupt the active turn. |
| `claude_turn.py:338` `restore_answer` — `interrupt` | yes | Interrupt the active turn and sweep queued steers on replay. |
| `claude_turn.py:368` `respond` — `approval:{request_id}` | yes: cancel-turn; no: all other decisions | Claude deny with interrupt:true aborts the active turn; allow/answer and deny without interrupt only answer the tool. |
| `claude_turn.py:372` `respond` — `interrupt` | yes | Interrupt the active turn and any requested queued steers. |
| `claude_turn.py:528` `_control_response` — `user-message` | no | Submit/start user input; no stop request. |
| `claude_turn.py:534` `_control_response` — `SETTINGS_FRAME` | no | get_settings reads settings only. |
| `claude_turn.py:593` `_control_request` — `refuse:{request_id}` | no | Control-response error refuses a request; no interrupt bit. |
| `claude_turn.py:874` `_check_model` — `interrupt` | yes | Interrupt the active turn on model mismatch. |
| `claude_turn.py:891` `_end` — `close` | yes | Close stdin (EOF can end the provider), including replay/refusal cleanup. |
| `codex_turn.py:174` `start` — `init` | no | Initialize the provider protocol; starts no active turn stop. |
| `codex_turn.py:201` `_send_interrupt` — `interrupt` | yes | Interrupt the active turn. |
| `codex_turn.py:231` `_send_steers` — `steer:{mid}` | no | Queue/steer user input for the active turn; no cancellation. |
| `codex_turn.py:270` `respond` — `approval:{request_id}` | yes: cancel-turn; no: all other decisions | Command/file cancel interrupts the turn; accept, acceptForSession, decline and permission-subset replies do not. |
| `codex_turn.py:347` `_response` — `initialized` | no | Complete the initialization handshake. |
| `codex_turn.py:348` `_response` — `hooks` | no | hooks/list reads hook configuration. |
| `codex_turn.py:349` `_response` — `models` | no | model/list reads the model catalog. |
| `codex_turn.py:409` `_maybe_thread` — `thread` | no | thread/resume or thread/start opens the thread before turn/start; no active-turn cancellation. |
| `codex_turn.py:412` `_maybe_thread` — `thread` | no | thread/resume or thread/start opens the thread before turn/start; no active-turn cancellation. |
| `codex_turn.py:457` `_thread` — `user-message` | no | Submit/start user input; no stop request. |
| `codex_turn.py:715` `_server_request` — `refuse:{rid}` | yes: legacy abort; no: other refusals | Legacy applyPatchApproval/execCommandApproval reply abort ends the turn; MCP action:cancel only cancels elicitation, empty answers and RPC errors do not abort. |
| `codex_turn.py:734` `_end` — `close` | yes | Close stdin (EOF can end the provider), including replay/refusal cleanup. |
| `runner.py:320` `_run` — `close` | yes | Close stdin (EOF can end the provider), including replay/refusal cleanup. |
| `runner.py:1043` `_refuse_frame` — `close` | yes | Close stdin (EOF can end the provider), including replay/refusal cleanup. |
| `runner.py:1086` `_timers` — `signal:int` | yes | SIGINT ends/aborts provider work during escalation. |
| `runner.py:1089` `_timers` — `close` | yes | Close stdin (EOF can end the provider), including replay/refusal cleanup. |
| `runner.py:1096` `_timers` — `signal:int:late` | yes | SIGINT ends/aborts provider work after result. |

### Validation status

The focused post-fix hold passed **52 tests** (216 deselected), covering every
new approval regression/guard, the bounded delivery-interleaving property,
provider interrupts and existing stop-frame regressions. Both providers settle
post-wall approval cancellation as `cancelled`, rc 130. Pre-transmission exit
proof still settles `succeeded`; non-stopping approvals create no stop intent;
both allow and cancel preserve SIGINT, close and containment escalation.
The separate mutation hold reverted `_transmit` to the original tag/op audit.
`tests/fake/test_wall_stop_deliveries.py::test_approval_cancel_turn_after_wall_keeps_wall_cancel[codex]`
failed (**1 failed**) because the approval created no durable intent before I/O.
The foreground mutation runner restored the exact source bytes in `finally`,
verified by SHA-256 and a clean runner diff. No mutation remains. The test-first,
focused and mutation holds are excluded from the final complete-file totals.

Per-file exploratory evidence (excluded from final totals):

| Hold | File | Passed | Failed | Deselected in hold |
| --- | --- | ---: | ---: | ---: |
| tests-first | `tests/fake/test_wall_stop_deliveries.py` | 0 | 12 | 13 |
| guards-first | `tests/unit/test_turn_runner.py` | 0 | 1 | 195 total |
| guards-first | `tests/unit/test_claude_turn.py` | 0 | 8 | included above |
| guards-first | `tests/unit/test_codex_turn.py` | 0 | 9 | included above |
| focused | `tests/fake/test_wall_cancel_races.py` | 1 | 0 | 216 total |
| focused | `tests/fake/test_wall_stop_deliveries.py` | 15 | 0 | included above |
| focused | `tests/unit/test_turn_runner.py` | 8 | 0 | included above |
| focused | `tests/unit/test_claude_turn.py` | 12 | 0 | included above |
| focused | `tests/unit/test_codex_turn.py` | 16 | 0 | included above |
| mutation | `tests/fake/test_wall_stop_deliveries.py` | 0 | 1 intended | 0 |

The complete requested file groups are recorded below.

**Final complete-file runs: 970 passed, 6 skipped, and 5 expected historical
failures across 24 files. No unexpected failures.** All 24 r5 probes pass,
including both approval-cancel providers; all 55 r4 probes and all 40 dispatch
comparisons pass. The unchanged r3 probe keeps its one historical rc-0 timestamp
tie failure; the unchanged original #207 file keeps its four due routing-call
failures (durable refusal grace preempts the old routing expectation). Neither
historical expectation was changed. Existing wall-property and legacy-hold audit
assertions remain intact; provider assertions were strengthened with flag checks.

| File | Passed | Skipped | Expected historical failures |
| --- | ---: | ---: | ---: |
| `test_review_r223_207_original.py` | 4 | 0 | 4 |
| `test_review_r223_r3_probes.py` | 8 | 0 | 1 |
| `test_review_r223_r4_probes.py` | 55 | 0 | 0 |
| `test_review_r223_r5_dispatch_comparison.py` | 40 | 0 | 0 |
| `test_review_r223_r5_probes.py` | 24 | 0 | 0 |
| `tests/fake/test_finalization_replay.py` | 10 | 0 | 0 |
| `tests/fake/test_notice_header.py` | 23 | 0 | 0 |
| `tests/fake/test_review_r223_wall_probes.py` | 23 | 0 | 0 |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 |
| `tests/fake/test_wall_cancel_races.py` | 30 | 0 | 0 |
| `tests/fake/test_wall_inspection_order.py` | 18 | 0 | 0 |
| `tests/fake/test_wall_running_time.py` | 35 | 0 | 0 |
| `tests/fake/test_wall_stop_deliveries.py` | 25 | 0 | 0 |
| `tests/unit/test_claude_turn.py` | 69 | 0 | 0 |
| `tests/unit/test_codex_turn.py` | 67 | 0 | 0 |
| `tests/unit/test_codex_turn_completed.py` | 41 | 0 | 0 |
| `tests/unit/test_conversation_service.py` | 100 | 0 | 0 |
| `tests/unit/test_daemon_settle.py` | 43 | 0 | 0 |
| `tests/unit/test_legacy_hold.py` | 50 | 6 | 0 |
| `tests/unit/test_relay.py` | 14 | 0 | 0 |
| `tests/unit/test_settle_properties.py` | 129 | 0 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 |
| `tests/unit/test_turn_runner.py` | 77 | 0 | 0 |

Each requested group ran in one separate foreground `lockf -k` hold of
`/private/tmp/claude-501/subfleet-suites.lock`, with exactly one pytest process
at a time, no lock timeout and no xdist workers. No detached process, relaunch
of a paused test or load wait under the lock was used. Final pytest durations
were wall 78.39 s, turn/unit 73.08 s, settlement 39.73 s, contract 39.75 s and
reviewer 28.65 s. Every hold was under twenty minutes, including the observed
lock wait; the longest exploratory hold plus wait was 162.01 s. All test holds
finished by 05:37:29 UTC on 2026-10-19, before the 09:49:16 UTC no-new-holds
cutoff (4 h 30 m after this workspace's 01:19:16 EDT stamp).

`SUBFLEET_RETENTION_DORMANT` and `PYTHONPATH` were unset. Hypothesis used the
bounded settings and `HYPOTHESIS_PROFILE=ci`, never deep; its storage lived in
each hold's TMPDIR. The sandbox denied the default uv cache, so uv's cache and
the installed test environment lived in scratch. Python was 3.14.7. The unit
hold reported one existing `PytestRemovedIn10Warning` for the `zip` parameter
iterator in `test_codex_turn_completed.py`; it was not changed.

All requested files were selected and ran. Six native legacy-hold tests skipped
before their bodies because the sandbox denies the C-5.3 macOS boot/process
identity inspection (`sysctl`/`ps`). Their native behavior remains unverified.
Full repository CI, other Python builds and real-provider execution were not run.
Reviewer Python sources were copied byte for byte into scratch and ran in their
own pytest process, separate from `tests/`, so test isolation loaded once.

### Cleanup and local delivery

All task pytest, runner and lockf commands exited. Each hold's temporary homes,
TMPDIR and Hypothesis storage were removed. The task scratch, reviewer copies,
JUnit evidence, fallback uv cache and installed environment were removed with
Python `shutil.rmtree`; no `.hypothesis` directory remains. The workspace's
original `.git` pointer is byte-identical to its saved starting bytes. The shared
Git index and caller checkout were never written. Local release refs were
rechecked after validation and remain `release/217=b053e3de8` and
`origin/release/217=d72597b5c`; no network remote was contacted.

Only the ignored workspace-local `.git-local` commit metadata and delivery
bundle remain. `.git-local/round9.bundle` advertises
`refs/heads/fix/wall-counts-running-time-217`, with prerequisite `fe8bda1ff`;
its advertised tip is the final report commit. Bundle integrity and the
advertised head were verified locally. No logs, JSON evidence or bundles were
staged. No history was rewritten and nothing was pushed.

| Commit | Coherent step |
| --- | --- |
| `c8eb1cd58` | Test-first approval regressions, decision guards and expanded delivery property |
| `d5aea7ac9` | Explicit stop flag at all 29 sites, flag-based audit, contract amendment and frame sweep |
| final report commit (bundle tip) | Complete per-file results, mutation proof and cleanup/local delivery status |
