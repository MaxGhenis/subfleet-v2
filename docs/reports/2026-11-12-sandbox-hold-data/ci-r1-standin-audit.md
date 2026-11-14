# CI r1 stand-in audit

Read-only source audit of the assigned worktree, followed by this requested evidence file. The audit ran no pytest commands, changed no production/test files, created no directories, and made no commits. Source anchors below were read at the task baseline before the fixture fixes; the parent agent owns execution results and the final report.

## Production boundary and callers

The production read belongs inside the caller's transaction. `Daemon.__init__` creates `Store(self.root / "state.sqlite3", readers=READ_CONNECTIONS)` at `subfleet/daemon.py:1012`. `Store` sets `connection.row_factory = sqlite3.Row` at `subfleet/store.py:323`; `Store.transaction` yields that actual connection at `store.py:434`. A `sqlite3.Row` supports the positional indices used by `job_held`.

`subfleet/task_holds.py:238` defines `job_held`; its job query at line 242 selects exactly `kind, task, sandbox`. It returns immediately for absent jobs, turns, or jobs with no task, then reads the two exact task/sandbox lease keys and composes their current coverage. Reading stored job fields beside the leases avoids relying on a stale admission snapshot or a current policy default.

All direct production callers found by `rg`:

| Caller | Anchor | Transaction reached |
|---|---:|---|
| `ResetCredits.evaluate` | `subfleet/actions.py:746` | `action.pending`, after rereading the waiting job; returns `demand-gone` with the task or sandbox discriminator when held. |
| `Daemon._task_held_at_write` | `subfleet/daemon.py:1747` | The caller's current admission/write transaction. |
| `Daemon._take_task_jobs` | `daemon.py:1838` | The operator hold transaction; composes the other hold before taking or moving queued/waiting jobs. |
| `Daemon._release_task` | `daemon.py:2091` | The operator release transaction; preserves remaining coverage on the other target before restoring clocks. |

An AST inventory found every daemon method calling `_task_held_at_write` or `job_held`:

| Method definition | Call line(s) |
|---|---|
| `_task_held` (`daemon.py:1592`) | 1628, 1638; the first is `job.task_hold_moved`, the second `job.task_held`. |
| `_task_held_at_write` (1728) | 1747. |
| `_take_task_jobs` (1812) | 1838. |
| `_task_hold_ended` (1902) | 1922. |
| `_release_task` (2047) | 2091. |
| `_push_workspace_ready` (6302) | 6352, inside `job.workspace_preparing`. |
| `_prepare_route` (7191) | 7315, inside `job.probe_waiting`; quarantined probes short-circuit this check. |
| `_fence_hold` (7465) | 7523. |
| `_admit_pass` (7617) | 8116, 8322, 8380. |
| `_stuck_pin` (9394) | 9421. |
| `_unstuck_pin` (9521) | 9536. |
| `_unroutable` (9551) | 9584. |
| `_workspace_failed` (9664) | 9686. |
| `_fail_queued` (9722) | 9731. |

## The three failing stand-ins

| Fixture | Baseline evidence and cause | Correct boundary |
|---|---|---|
| `tests/unit/test_host_push_build_budget.py:179`, `Store` | `transaction` yields itself. Its generic non-lease `SELECT` returns the whole job dictionary, including for `SELECT kind, task, sandbox`; `job_held` then raises `KeyError: 0`. | Return the selected positional row for this query, retaining the existing empty lease rows and all bounds/assertions. |
| `tests/unit/test_host_push_store_completion_pr183.py:73`, `Rows` | This uses a real in-memory SQLite connection, but its miniature `jobs` table at line 79 lacks `kind`, `task`, and `sandbox`. The new query first raises `OperationalError: no such column: kind`. `_push_workspace_ready` classifies this as a permanent store error; `_finish_push_workspace` calls the real `_fail_queued`, whose `_job` then exposes the secondary `AttributeError` because `Rows` has no `get_job`. | Add the production job columns and persisted values to the miniature table. Do not implement the accidental error path or change production's read. |
| `tests/unit/test_host_push_store_interleavings.py:27`, `BusyRows` | Inherits the same incomplete table. Its transaction `Connection` at line 50 delegates `execute` to the real connection, so it receives the same missing-column error. The harness's `_fail_queued` override then indexes a nonexistent transient-failure counter, causing secondary `KeyError: '0'`: a first permanent schema error has no retry counter. | Repair inherited `Rows`; its SQL delegation and fault schedule remain valid. |

The nine build-budget errors and the two secondary errors match the supplied CI excerpt. No production path with a non-SQLite job store was found. Adding a permissive row-access fallback in `job_held` would hide the fixture/schema mismatch and weaken the transaction contract.

The only additional consumer of these miniature SQL stores is `tests/unit/test_host_push_storage_retry_budget.py:25`, whose `completion` fixture constructs `BusyRows(1)`; it is in the required filename selection. `test_host_push_storage_recovery.py` imports the real-Store `finished` helper from the interleavings module, not its miniature store. No other file imports `gate_harness`, `Rows`, or `BusyRows`.

## Other reachable transaction wrappers

These wrappers preserve real SQLite row shapes. They still belong in validation because their transactions can reach the new reads.

| File | Fixture/wrapper and evidence | Coverage |
|---|---|---|
| `tests/unit/test_host_push_store_interleavings.py` | `fault_transaction` at 235; `Connection` at 243 delegates all unmodified attributes through `__getattr__` and all nonfaulted `execute` calls to the real connection; `wrapped` yields the original `Store.transaction` result. The later `after_worker_starts` wrapper at 587 also yields the real transaction. | Push preparing/completion/error paths. |
| `tests/unit/test_host_push_storage_recovery.py` | Recovery wrapper around the real Store transaction, yielding its `tx`. | Push preparation and completion after quota/write failures. |
| `tests/unit/test_host_push_storage_retry_budget.py` | Uses `BusyRows`, plus transaction fault wrappers; real-world cases use the actual Store. | Dependent miniature-store harness plus preparation/completion retry budgets. |
| `tests/fake/test_task_hold_cas.py:154` | `transaction` commits the foreign replacement through a second real Store before entering the original transaction; yields its actual `tx`. | Hold/release CAS, `_take_task_jobs`, remaining hold coverage. |
| `tests/fake/test_task_hold_clocks.py:679,891` | Finalizer/barrier wrappers defer or advance the clock while yielding the original Store's `tx`. Earlier door-read wrappers modify snapshots only. | Hold/release clocks, hold-move/take, pin and stale admission writers. |
| `tests/fake/test_sandbox_hold_continuity.py:34` | `delayed_writer` commits the overlapping hold before entering the original transaction; yields the actual `tx`. | `_stuck_pin` and composition/restore. |
| `tests/fake/test_probe_turn.py:272,567,1050,1069` | Clock/load wrappers yield original `tx` unchanged. | `_prepare_route`, `job.probe_waiting`, admission reservation paths. |
| `tests/fake/test_probe_cancel.py:92` | `AdmissionHistory.transaction_boundary` wraps `Store.transaction`, performs cancellation at entry, and yields the real `tx`. | `_prepare_route` and `_admit_pass`; outside the filename/hold-reference union, so explicitly added. |
| `tests/fake/test_probe_deadline.py:143` | `after_writer_wait` advances the clock before probe reservation and yields the original real transaction. Its UNKNOWN outcome reaches `job.probe_waiting`. | `_prepare_route`; explicitly added. |
| `tests/unit/test_timers_reset_credits.py:402` | `import_before_claim` inserts a legacy action before `action.pending`, then yields the original real connection. `job_held` runs before the final already-attempted action check. Fixture `store` constructs `Store` at line 33. | `actions.py:746`; explicitly added. |
| `tests/unit/test_reset_credit_claim_timing.py:24,54` | `delayed_commit` wraps `Store.transaction` classwide and yields the original real connection before advancing monotonic time. Imported properties `_build` constructs a real Store (`test_reset_credit_properties.py:189`). | `action.pending` and action claim timing; explicitly added. |

Direct `job_held` tests in `test_sandbox_hold_contract.py:108` and `test_sandbox_hold_properties.py:137` use real Store transactions. The task/sandbox admission, clock, property, CAS, and interleaving suites use `tests/task_hold_support.py`'s actual Daemon/Store fixture; no hand-written transaction result replacement was found there.

## Excluded stand-ins and wrappers

| File(s) | Why outside this SQL-change surface |
|---|---|
| `tests/unit/test_census_procargs.py:380`, `test_census_recycled_pid.py:179`, `test_census_survivors_review.py:127` | Small Stores implement evidence writes for `_contain`, `_record_owned`, or kill/census paths. They return no jobs and never invoke admission or the hold readers. |
| `tests/unit/test_census_responsibility_failures.py:187`, `test_census_counted_expiry.py:59`, `test_census_inspection_properties.py:56` | Namespace or wrapper transactions serve quarantine/evidence census only, not any listed caller. |
| `tests/unit/test_legacy_hold.py:83,1556` | `FakeJobStore` serves conversation handlers; `_Race.transaction` wraps `ConversationStore`, a separate store. The later daemon admission fixture uses real `Store`. |
| `tests/frontend/daemon_harness.py:47` | `_MainStore` is a conversation-only seam with no scheduler, jobs, or lanes; its harness never dispatches admission. |
| `tests/unit/test_daemon_fifo_reads.py:202` | Local Store belongs only to the `_export_locked` unreadable-deliverable probe. Its separate daemon fixtures use real Store. |
| `tests/unit/test_handler_refusals.py:300` | Local Store serves only a lane-transfer refusal. |
| `tests/unit/test_person_boundary_identity.py:120`, `test_protocol_secrecy.py:24` | `Connection` classes are socket/transport stand-ins, not SQLite transaction stand-ins. |
| `tests/unit/test_review_r5_repro.py:153`, `test_review_r6_probes.py:409`, `test_approval_resolution.py:83`, `test_title_review_regressions.py:255` | Crash/interrupt/commit wrappers operate on the separate `ConversationStore` for conversation intents, approvals, and title events. |
| `tests/fake/test_review130_ab.py:100,228`, `test_native_maintenance_startup.py:321`, `tests/unit/test_timers_retention.py:210`, `tests/fake/test_retention_dependency_review_r5.py:201` | Transaction wrappers run retention selection/pruning. The last file's separate fence/hold test does use actual Store and is selected by the hold-reference search. |
| `tests/fake/test_wall_running_time.py:366`, `test_wall_cancel_races.py:395`, `test_wall_inspection_order.py:69` | Wrappers are installed after admission; the exercised calls are kill, cancellation, receipt, and finalization rather than a listed hold reader. |
| Other bare Daemon fixtures found by source search | Admission-capable fixtures use actual Store; provider-environment, protocol, identity, log, timer, disk-view, and render-only bare objects do not invoke the listed writers. |

## Required filename selection

The original worktree has 44 files under `tests/unit` and `tests/fake` whose names contain `host_push`, `task_hold`, `sandbox_hold`, or `pin`: 14 fake and 30 unit. The new `test_host_push_sandbox_hold.py` makes 45. The parent owns the union with the hold-reference search and four added wrappers; `selected-tests.txt` is the exact runnable inventory.

```text
tests/fake/test_detached_cwd_pins.py
tests/fake/test_sandbox_hold_continuity.py
tests/fake/test_sandbox_hold_interleavings.py
tests/fake/test_sandbox_hold_properties.py
tests/fake/test_task_hold_admission.py
tests/fake/test_task_hold_cas.py
tests/fake/test_task_hold_cas_properties.py
tests/fake/test_task_hold_clocks.py
tests/fake/test_task_hold_mutants.py
tests/fake/test_task_hold_properties.py
tests/fake/test_task_hold_reset_demand.py
tests/fake/test_unadmittable_pin_admission.py
tests/fake/test_unadmittable_pin_rereview.py
tests/fake/test_unadmittable_pin_review.py
tests/unit/test_host_push.py
tests/unit/test_host_push_admission.py
tests/unit/test_host_push_build.py
tests/unit/test_host_push_build_budget.py
tests/unit/test_host_push_cli.py
tests/unit/test_host_push_config_cost.py
tests/unit/test_host_push_copy.py
tests/unit/test_host_push_export.py
tests/unit/test_host_push_intake.py
tests/unit/test_host_push_metadata.py
tests/unit/test_host_push_mutations.py
tests/unit/test_host_push_names.py
tests/unit/test_host_push_quarantine_restart.py
tests/unit/test_host_push_restart.py
tests/unit/test_host_push_retention.py
tests/unit/test_host_push_storage_recovery.py
tests/unit/test_host_push_storage_retry_budget.py
tests/unit/test_host_push_store_completion_pr183.py
tests/unit/test_host_push_store_interleavings.py
tests/unit/test_host_push_trees.py
tests/unit/test_host_push_validated_top.py
tests/unit/test_retention_folded_pin.py
tests/unit/test_sandbox_hold_cli.py
tests/unit/test_sandbox_hold_contract.py
tests/unit/test_sandbox_hold_reapers.py
tests/unit/test_task_hold_cas_cli.py
tests/unit/test_task_hold_cas_integration.py
tests/unit/test_task_hold_cas_wire.py
tests/unit/test_task_holds.py
tests/unit/test_unadmittable_pin.py
```

## Inspection command inventory and results

All command scopes were inside the assigned worktree, except the three explicitly supplied review input files. No home/state/configuration search was run. All successful reads exited 0; the scoped `AGENTS.md` search returned 1 because no file matched. An AST scan ran through the tool's own session mechanism and was polled until it exited 0; no process was left running. Independent reads were batched with `Promise.allSettled` where appropriate. This audit did not create test counts: its results are source inventory counts only.

The following are the exact search expressions and read ranges, grouped by purpose to avoid duplicating the same path prefix on every row. Plain `sed`, `cat`, `rg` and Python inventory reads made no mutations.

| Purpose | Commands / scopes | Result |
|---|---|---|
| Inputs | `pwd`; `cat /Users/maxghenis/reviews/subfleet-hub/sandbox-hold-2026-11-10/brief-build.md`; `cat docs/reports/2026-11-12-sandbox-hold.md` | Correct assigned workspace; rules and prior report read. |
| Direct callers | `rg -n 'job_held\(|_task_held_at_write\(' subfleet tests` | Four direct production call sites plus the delegating admission callers and direct tests. |
| Required filenames | `rg --files tests/unit tests/fake` piped to `rg '(host_push|task_hold|sandbox_hold|pin)'`; later Python `Path.glob('*.py')` with those four substrings | 44 original files; 45 after new regression file appeared. |
| Local instructions | `rg --files -g AGENTS.md -g '!docs/reports/**' -g '!*.lock'` | No matches, exit 1. |
| Production implementation | `sed -n '1560,1950p'`, `'1980,2135p'`, `'6310,6385p'`, `'6318,6370p'`, `'6140,6318p'`, `'7178,7327p'` on `subfleet/daemon.py`; `sed -n '700,775p'` and `'728,760p'` on `subfleet/actions.py`; `sed -n '1,310p' subfleet/task_holds.py` | Confirmed transaction-local job/lease read, hold move/take/release and push failure handling. |
| Reachability searches | `rg -n '(_push_workspace_ready|_task_held\b|_take_for_hold|_hold_task|_release_task|_task_held_at_write|wait_hold|start_waiting|admission_demand|_request_clock)' tests`; `rg -n '(_task_hold_ended\(|_prepare_route\(|_fence_hold\(|_admit_pass\(|_stuck_pin\(|_unstuck_pin\(|_unroutable\(|_workspace_failed\(|_fail_queued\(|_take_task_jobs\()' tests` | Enumerated direct test calls, mutants, and related writer paths. |
| Stand-in definitions | `rg -n '(class (.*Store|.*Rows|.*Tx|.*Transaction|.*Connection)|def transaction|def execute\(|\.store\s*=|_task_held_at_write|_task_holds_now|job_held)' tests/unit tests/fake`; `rg -n 'setattr\([^\n]*(store|transaction)|setattr\([^\n]*"(one|query|get_job|_job)"|\.store\s*=|store=' tests/unit tests/fake` | Located handwritten stores, query replacements, and transaction wrappers. |
| Failing stores | `sed -n '1,390p' tests/unit/test_host_push_build_budget.py`; `cat tests/unit/test_host_push_store_completion_pr183.py`; `cat tests/unit/test_host_push_store_interleavings.py`; later `sed -n '1,170p'` and `'230,280p'` on interleavings; `sed -n '1,240p' tests/unit/test_host_push_storage_retry_budget.py` | Three incompatible fixture boundaries plus dependent BusyRows consumer. |
| Secondary-failure evidence | `rg -n '(SELECT.*(kind|sandbox)|no such column|KeyError|AttributeError|E +)' /Users/maxghenis/reviews/subfleet-hub/sandbox-hold-2026-11-10/ci-309-py312-shard3.fail.txt` | CI records both missing-column errors before the secondary failures. |
| Fixture import graph | `rg -n '(from .*test_host_push_build_budget import|from .*test_host_push_store_completion_pr183 import|from .*test_host_push_store_interleavings import|gate_harness\(|Rows\(|BusyRows\()' tests` | Only the four miniature-store consumers documented above. |
| Bare Daemon inventory | `rg -l '(object\.__new__\(Daemon\)|Daemon\.__new__\(Daemon\)|object\.__new__\(daemon.*Daemon\))' tests` | Classified admission-capable real-Store fixtures versus unrelated bare objects. |
| Method AST inventory | Python parsed `subfleet/daemon.py`, visited function definitions, and printed methods containing attribute calls named `_task_held_at_write` or `job_held` | 14 methods, listed above. |
| Test AST inventory | Python parsed scoped `tests/**/*.py` and printed definitions named `execute`, `transaction`, or `transaction_boundary` when the source also referenced `Daemon` or `ResetCredits` | Narrowed handwritten SQL/transaction stand-ins; broader `rg` covered imports and differently named wrappers. |
| All transaction wrappers | `rg -n '(def (transaction|.*transaction)|yield (self|.*[cC]onnection|.*tx)|class .*Store)' tests --glob '*.py'` | Classified additional real-Store, conversation-store, retention, census and kill-only wrappers. |
| Reset-credit scopes | `rg -n '(def |class |_task|reset|inhibited|transaction|conn\.)' tests/unit/reset_credits_release217.py`; `cat tests/unit/test_reset_credit_claim_timing.py`; scoped `rg` over `tests/unit/test_reset*`, `tests/fake/test_reset*`, task/sandbox files for `ResetCredits`, store, transaction, yield and execute references; `sed -n '383,425p' tests/unit/test_timers_reset_credits.py` | Actual Store fixtures; two extra wrapper files added to validation. |
| Hold/CAS wrappers | `sed -n '645,705p'` and `'875,925p' tests/fake/test_task_hold_clocks.py`; `sed -n '130,175p' tests/fake/test_task_hold_cas.py`; `cat tests/fake/test_sandbox_hold_continuity.py` | Real SQLite delegation across hold/release and pin races. |
| Probe wrappers | `sed -n '60,115p' tests/fake/test_probe_cancel.py`; `sed -n '125,156p' tests/fake/test_probe_deadline.py`; `sed -n '265,307p'`, `'550,580p'`, `'1028,1080p' tests/fake/test_probe_turn.py` | Real SQLite delegation; probe_cancel and probe_deadline added. |
| Supporting real-Store evidence | Scoped `rg -n '(def transaction|row_factory|yield self.connection|yield conn|yield tx|self.store =|Store\()'` on store, daemon, task_hold_support, conftest, host_push, reset_credit_properties and timers_reset_credits | Production connection evidence and actual Store fixtures. |
| Census exclusions | `sed -n '170,210p' tests/unit/test_census_recycled_pid.py`; `'375,407p' test_census_procargs.py`; `'120,155p' test_census_survivors_review.py`; `'1,90p' test_census_inspection_properties.py`; `'165,205p' test_census_responsibility_failures.py`; scoped `rg` of census-counted/inspection/expiry and related repro files | Census-only transaction shapes cannot reach admission hold reads. |
| Conversation/transport exclusions | `sed -n '1528,1572p' tests/unit/test_legacy_hold.py`; `'185,220p' test_daemon_fifo_reads.py`; `'285,325p' test_handler_refusals.py`; `'20,60p' test_protocol_secrecy.py`; `'112,151p' test_person_boundary_identity.py`; `'1,115p' tests/frontend/daemon_harness.py`; `'70,108p' test_approval_resolution.py`; `'245,270p' test_title_review_regressions.py` | Separate ConversationStore or transport/export/refusal seams. |
| Retention/wall exclusions | `sed -n '180,218p' tests/fake/test_retention_dependency_review_r5.py`; `'200,220p' tests/unit/test_timers_retention.py`; `'349,381p' test_wall_running_time.py`; `'378,407p' test_wall_cancel_races.py`; `'45,82p' test_wall_inspection_order.py`; scoped `rg` of retention_rediscovery, retention_shared_folders, capacity_wait_backoff, workspace_transient, notice_header and census_round4_endings | Actual Store for hold-capable cases; unrelated wrappers classified above. |
| Remaining classified scopes | Scoped `rg` of bare Daemon references in placement_ramp, disk_admission, daemon_fifo_reads, descriptor_review_fixes, timers_daemon, person_boundary_identity, protocol_secrecy, review_publish_env_round8, daemon_settle, inspection_pacing_properties; scoped `rg` of store/wrapper evidence in probe_cancel, native_maintenance_startup, review130_ab, legacy_hold, gate_admission, handler_refusals, launch_env_round8, render_quota_projection, hold_note and its review tests; `sed -n '45,78p' test_request_routing.py`, `'120,167p' test_provider_active_cap_fake.py`, `'165,185p' test_daemon_log_cut_windows.py`, `'300,328p' test_prune_decisions.py` | No further handwritten SQL results reachable by a new hold read. |
| Hold-reference union | `rg -l 'task_holds|task-held|tasks hold|job_held|wait_hold' tests --glob 'test_*.py'` | 23 files after the new regression appeared; parent combined with required names and extra wrappers. |
| Final evidence anchors | Scoped `rg -n` of Store/Rows/BusyRows/Connection definitions and schema/query terms in the three failing files; scoped `rg -n` of wrapper definition names across the documented reachable/excluded files; `rg --files docs/reports/2026-11-12-sandbox-hold-ci-r1-data` | Verified line anchors and existing evidence destination. |

The only write by this audit is `apply_patch` adding this file, requested by the parent after the read-only findings were delivered. No test assertion, retry bound, production implementation, live state, branch, or history was changed by the audit agent.

## Follow-up regression review

The parent requested a read-only review of `tests/unit/test_host_push_sandbox_hold.py` and the intended fixture repair. No blocking issue was found. The new test uses a file-backed real Store, persists the job's task/sandbox, inserts an actual sandbox lease, and invokes the production hold readers without replacement. Its deterministic submission queue prevents worker/Git activity while checking the committed waiting reason/clock, no attempt/worktree, and the exact `job.task_held` versus `job.workspace_preparing` audit. Capturing the job before inserting the hold exercises the stale-snapshot write boundary.

The planned budget-store positional tuple/None answer and task `build` exercise the exact lease query. Persisting `dispatch`, `build`, and `workspace-write` in `Rows`, and reading those values back through `job()`, makes its shape match the production query; unchanged `BusyRows` delegates to SQLite.

Commands for this review, all read-only and exit 0: `cat tests/unit/test_host_push_sandbox_hold.py`; `sed -n '168,221p' tests/unit/test_host_push_build_budget.py`; `sed -n '67,116p' tests/unit/test_host_push_store_completion_pr183.py`; `sed -n '6288,6370p' subfleet/daemon.py`; `sed -n '400,449p' subfleet/store.py`. No tests or code edits were performed by this review.

## Selected-run failure investigation

The completed 55-file run reported **1 failed, 1,310 passed, 7 skipped, 1 xfailed in 3,119.97 seconds**. Its sole failure is `tests/unit/test_host_push_store_interleavings.py::test_restart_finds_unrecorded_completion_after_busy_ownership[False]`. This differs from the eleven baseline fixture/read failures.

The complete traceback in `selected.log` shows the following causal chain:

1. The test at `test_host_push_store_interleavings.py:368` calls `finished(world)`.
2. `finished` at line 277 calls `submit(world)` before preparation or its ownership fault is exercised.
3. `test_host_push.py:145` dispatches `submit`; `Daemon.submit` at `daemon.py:3121` calls `host_push.validate_submit`.
4. `validate_submit` at `host_push.py:614` enters `quarantine`; `quarantine` at line 587 calls the real host Git wrapper for `init --bare --template=... --object-format=sha1 .`.
5. The fixture's `watch` wrapper at `test_host_push.py:128` delegates that command to the real `host_push.git`.
6. The Git wrapper's timeout check at `host_push.py:459` raises `PushError: host git init failed (TimeoutExpired)` under its existing `TIMEOUT_S` bound (60 seconds). `validate_submit` at line 622 wraps that as `AdapterError: host push refused: host git init failed (TimeoutExpired)`.

Thus the observed failure occurs during submission's quarantine repository initialization, before `_push_workspace_ready`, its hold query, or the miniature `Rows`/`BusyRows` harness is reached. No error in a hold query is present in this traceback. This establishes the failing boundary, not whether the timeout is transient: at this audit update, the parent is rerunning the entire unchanged 26-case interleavings file serially, and that result remains pending. No timeout or assertion was changed.

During the run, progress output displayed `F` before pytest emitted its traceback. The investigation searched only this job's scratch and assigned worktree. `/private/tmp/sf-sbh-c1/failed` did not exist, so there was no retained daemon root to inspect. `tests/fake/conftest.py:247` stores setup/call reports; its daemon fixture at line 272 copies a failed root through `failed_keep_dir` only after closing that harness. Other unit/world context managers do not necessarily preserve failed roots, so absence alone did not identify a failing module.

Investigation commands and results:

| Command | Result |
|---|---|
| `cat tests/failed_keep.py` | Exit 0; read failed-root naming, setup/call checks, and pruning rules without calling any of them. |
| `rg --files /private/tmp/sf-sbh-c1/failed` | Exit 2: job-owned failed-root directory did not exist. |
| `tail -n 80`, `tail -n 25`, `tail -n 90`, and `tail -n 110` on `docs/reports/2026-11-12-sandbox-hold-ci-r1-data/selected.log` | Exit 0; while the run remained active, only progress and one `F` were available. No traceback had been emitted yet. |
| `rg -n '(failed_keep|pytest_runtest_makereport|pytest_exception_interact|KEEP_FAILED|FAILED_KEEP)' tests/conftest.py tests` | Exit 0; identified the fake/e2e/frontend keep hooks. No hook or process was invoked. |
| `sed -n '235,292p' tests/fake/conftest.py` | Exit 0; confirmed fake-daemon teardown behavior. |
| `cat docs/reports/2026-11-12-sandbox-hold-ci-r1-data/selected-tests.txt` | Exit 0; confirmed the 55-file run's exact scope. |
| Python directory inventory below, started through `exec_command` with `tty` omitted | Interrupted during `Path.is_dir()` before producing output; exit 130 and `KeyboardInterrupt`. No file was changed. |
| `rg -n '(failed|Falsifying|AssertionError|FAILED|E +|nodeid)' /private/tmp/sf-sbh-c1/hypothesis --glob '*.json' --glob '*.txt'` | Exit 1; no textual failure metadata matched. |
| `rg -n '(AssertionError|Falsifying|FAILED|ERROR|KeyError|AttributeError|no such column)' docs/reports/2026-11-12-sandbox-hold-ci-r1-data/selected.log` | Exit 1 while the run was active; failure details were still buffered until pytest completion. |
| `tail -n 170 docs/reports/2026-11-12-sandbox-hold-ci-r1-data/selected.log` after completion | Exit 0; full timeout traceback and final counts read. |
| `sed -n '440,466p' subfleet/host_push.py`; `rg -n '^(TIMEOUT_S|def git\(|def quarantine\(|def validate_submit\()' subfleet/host_push.py` | Exit 0; confirmed timeout boundary and source anchors. |

Exact interrupted inventory script:

```python
from pathlib import Path
from datetime import datetime, timezone
base = Path('/private/tmp/sf-sbh-c1/pytest-selected')
entries = []
if base.is_dir():
    for worker in base.iterdir():
        if worker.is_dir() and not worker.is_symlink():
            for child in worker.iterdir():
                if child.is_dir() and not child.is_symlink():
                    entries.append((child.stat().st_mtime, str(child)))
for stamp, path in sorted(entries, reverse=True)[:30]:
    print(datetime.fromtimestamp(stamp, timezone.utc).isoformat(), path)
```

The script's returned tool session was `79555`. It was polled using `tools.write_stdin` with empty input, then interrupted only through `tools.write_stdin({session_id: 79555, chars: "\u0003", yield_time_ms: 1000, max_output_tokens: 6500})`. The tool immediately returned exit 130 and Python `KeyboardInterrupt` in `pathlib.Path.is_dir`. No `kill`, `ps`, live-process inspection, or signal addressed the parent's pytest run. No investigation command remained running afterward. The interruption's observed tool behavior does not establish its underlying signal implementation.

This follow-up investigation ran no tests, edited only this evidence note at the parent's request, and performed no deletion or commit.
