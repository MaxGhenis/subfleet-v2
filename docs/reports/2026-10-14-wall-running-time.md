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
