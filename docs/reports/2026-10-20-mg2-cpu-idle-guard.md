# D-MG2: CPU idle and disk guard — fix report

## Review-r3 P3 follow-up (2026-10-23)

**Change-log entry — 2026-10-23:** implement P3-1, P3-2 and P3-3 from the
approved r3 review; document handle disappearance and one-device baseline scans
in C-6.13; record the read-only cost comparison and this follow-up's unit census.
The existing C-6.13 sentence preserving counters and EWMA on an unreadable read
is unchanged and now holds for failed root stat and lookup samples.

All three P3 fixes are implemented. **426 distinct required cases passed** in
separate guard (**275**) and admission/state (**151**) holds. All three requested
mutants were rejected by their named regressions, with passing positive controls.
The generated topology checks ran **3,000 fixed-seed sequences / 75,000 samples**.

The combined required-gate and whole-unit census is **12,467 passed, 9 failed,
22 skipped**, with no test-case errors or expected failures. Of **347 unit files**,
**285 are complete, 6 partial and 56 unrun**. The five fake gate files are complete,
**114 passed**. This is an **incomplete, non-green whole-unit run**. Collection
manifests identify **at least 1,474 unrun unit cases**, plus the uncollected cases
in `test_retention_shared_folders.py` and `test_retention_sweep_race.py`.

### Branch, merge history and policy

The follow-up starts at release/217 **15a73b7bde23**, which already includes #241.
No merge or fetch was needed. The historical PR description described only the
first release merge, **08ba94992** of **e345f0803**. The hub subsequently merged
release **c2e080e44** in **f5b86cb05**; that second merge is an ancestor of this
follow-up's base. Its release changes and both contract histories remain intact.

The sandbox rejected the shared worktree index lock. As permitted by the brief,
all commits use workspace-local **`.git-local`**, branch **`fix/mg2-p3-217`**,
with the shared object store as a read-only alternate. The normal `.git` pointer
still names the original detached checkout. **The hub must import the local
branch before retiring this workspace.** No caller index, branch or checkout was
written, no history was rewritten, and nothing was pushed.

No live policy was read or edited. `subfleet/default_policy.json`, `policy.py`
and `daemon.py` are unchanged from 15a73b7bde23. The default remains
`machine_guard: null`; the proposal still has no disk or load threshold. Max's
explicit policy edit under d1377 remains the activation step.

### P3-1 — unknown topology preserves smoothing

A failed stat or driver lookup produced `None`, which `_select_device` treated as
a new identity. It discarded the prior counters and disk EWMA, then reset again
when the original device returned. The sampler now keeps the last good root key,
resolved driver, counter baseline and smoothing through either failure. A failed
root check reads no disk counters; a failed resolution skips the counter read
following that resolution. A changed root with a failed lookup stays pending and
is retried, rather than reading its old root's still-readable driver.

A different resolved driver or a root `st_dev` change between good resolved reads
still resets counters and EWMA, including a reused registry ID. CPU and legacy
sources remain independent. The existing native-counter rollback safety is
unchanged.

The review's `disk_busy_max = 4`, 10 ms/op through t=120, then 2 ms/op input now
releases at **t=220** for all three rows: no fault, failed stat at t=121, and failed
read plus lookup at t=121. The faulty implementation released the latter two at
t=133. The smaller 0/1/2/3-second input recovers at approximately **4.6223 ms/op**,
retaining its earlier counters and EWMA.

**The oracle in `tests/unit/test_machine_topology.py` around the former line 103
was corrected.** It previously expected a reset after an unknown sample. It now
compares with the sampler's last good root key and resolved driver, so an unknown
request does not manufacture an identity change.

### P3-2 — retain the resolved handle and reject disappeared devices

The old steady path rebuilt registry-ID matching dictionaries and asked
`IOServiceGetMatchingService` for the driver every read. `_Storage` now owns one
retained driver handle, including the handle obtained during ancestry resolution.
Steady reads use it directly; diagnostic enumeration keeps its previous behavior.
`close()` releases the cached right, and replacement and failed selected reads
release it as appropriate. CF statistics snapshots and keys remain per-read owned
objects and are released on every path.

**Disappearance is defined by detachment from the IOService plane.** The selected
handle must be attached before and after its statistics snapshot, and its registry
ID must match the requested driver. A retained detached object may still expose
old statistics; these are rejected as unknown. The failed read drops the native
handle while retaining the sampler's counters and EWMA. The next selected read
resolves by registry ID, unless the sampler's ancestry recovery has already
supplied a fresh handle. A successful different-device resolution still follows
the normal baseline-only first sample rule.

The existing registry fake was extended with handle identity and attachment. The
regressions cover two steady reads with one lookup, ownership transfer from
ancestry, idempotent close, readable stale properties after detachment, registry-ID
re-resolution on the following read, a mismatched replacement handle, and
detachment during statistics acquisition. The old selected-read release assertion
now explicitly closes the retained handle.

### Read-only cost comparison

One suite-wrapper hold alternated method order over **N=300 measured reads each**,
with one warmup per method. Both read physical driver **4294970958**, resolved from
an owned root on the state volume; every measured result was checked for that
identity. No device writes or saturation workload ran. The host was on **AC**, with
load1 **21.09 before / 22.84 after**; Python was GIL CPython **3.14.7**.

| Storage read method | N | Median, µs | p90, µs |
|---|---:|---:|---:|
| Registry-ID lookup, source at 15a73b7bde23 | 300 | 834.42 | 5,580.71 |
| Retained handle, including identity/attachment checks | 300 | 53.94 | 99.33 |

p90 is the nearest-rank 90th percentile. These are observed reader wall times,
including host scheduling. They exclude CPU, root checks, legacy sampling and
SQLite recording. The retained reader's median was about **15.5× faster** in this
run. The previous complete-sampler measurements below remain historical evidence.
`tools/measure_machine_device_reads.py` provides the same alternating comparison,
using a scratch copy of the base source for the old reader.

### P3-3 — one device identity per baseline

The eligible-row predicate re-read `self.device_id` for every history row while
admission could change it. Value and age could therefore use rows from two
drivers. `_baseline` now captures the identity once and passes it to the shared
eligibility predicate. Percentile and newest observation use that one eligible
set, timestamp and cutoff. The exact two-row history (device 1 at 2 ms/op at 1000;
device 2 at 50 ms/op at 1900; switch between rows; status at 2000) reports
**2.0 ms/op aged 1,000 seconds**.

### Invariants and property evidence

| Invariant | Evidence |
|---|---|
| **One device per reading:** no reading or baseline mixes two devices. | Independent elapsed-weight EWMA/counter state model across replacements, returns and reused IDs; unrelated-driver calibration rows; native mismatched-handle rejection; exact two-row status interleaving. |
| **Unknown is not a change:** an unreadable sample never holds, releases or resets. | Failed stat/lookup continuity checks preserve root/device/counters/time/EWMA; generated unknown rows hold nothing and preserve latch membership; failed-stat counter-call trap. |
| **Resets are real:** counters and EWMA reset only on a different resolved device or a `st_dev` change between two good reads. | Model compares counter baseline, identity, timestamp, EWMA numerator and weight at every generated step; explicit changed-root/failed-lookup recovery and reused-ID regressions. |

`test_machine_device_properties.py` uses seeds **241, 613 and 20261023**, **1,000
sequences each**, **25 sample events per sequence**. Each sequence includes stat
failure, failed lookup, same-device return, a changed root reusing an ID, and a
driver replacement before its bounded random events. The topology model uses
valid nondecreasing counters; existing handling of a genuine native counter
rollback remains separate and unchanged under C-6.13. A resolved topology change
is real evidence even when its first counter sample is baseline-only. Unknown
reads interrupt the latch's recovery timer while preserving counters and EWMA.

Existing bounded Hypothesis device/age and scheduler properties remain intact.
No deep profile ran.

### Tests first and named mutations

The initial pre-fix regression hold produced **14 failures and 1 passing control**,
without collection/setup errors. The small continuity fixture was then corrected
to the review's short intervals before the tests-first commit; an additional
mid-read-detachment regression was added before implementation. The subsequent
required guard hold passed all 275 cases and the admission/state hold all 151.

The mutation file compiles isolated module copies; production source is never
mutated. Anchor validation is outside expected-failure handling. Each mutant's
named assertion failed and its unmodified positive control passed: **6 passed**.

| Mutation | Named rejecting regression |
|---|---|
| Remove the unknown-identity guard so a failed lookup becomes a change | `test_unknown_sample_preserves_counter_baseline_and_smoothing[lookup]` |
| Re-read `device_id` for every history row | `test_status_baseline_captures_device_once_before_history_scan` |
| Read through a detached stale handle by removing attachment checks | `test_disappeared_handle_is_unknown_then_reresolved_by_id` |

Required-file counts, all complete and passing:

| File | Passed |
|---|---:|
| `tests/unit/test_machine_signals.py` | 25 |
| `tests/unit/test_machine_topology.py` | 12 |
| `tests/unit/test_machine_device_continuity.py` | 13 |
| `tests/unit/test_machine_device_properties.py` | 3 |
| `tests/unit/test_machine_device_mutations.py` | 6 |
| `tests/unit/test_scheduler_machine_signals.py` | 13 |
| `tests/unit/test_policy.py` | 203 |
| `tests/unit/test_admission_uncapped.py` | 27 |
| `tests/unit/test_scheduler_priority_callers.py` | 9 |
| `tests/unit/test_test_module_names.py` | 1 |
| `tests/fake/test_machine_guard_daemon.py` | 6 |
| `tests/fake/test_admission_priority.py` | 33 |
| `tests/fake/test_admission_ramp.py` | 19 |
| `tests/fake/test_reset_credit_incident.py` | 19 |
| `tests/fake/test_state_contract.py` | 37 |

`tests/unit/test_daemon_admission*.py` matches no files at this base. Verification
used the actual uncapped admission and scheduler-caller unit modules, the guard
fake, admission priority/ramp fakes, and reset-credit incident fake listed above.
Every `test_machine*.py`, plus scheduler machine signals and policy, ran in the
275-case guard hold. State contract and module-name checks ran in the second hold.

### Whole-unit sweep, failures and unrun work

The census combines required gates and the whole-unit attempt, deduplicated by
pytest node ID and taking each case's latest recorded JUnit outcome. Synthetic
session interruption/harness error records with no test file are excluded from
product case counts. A file is complete only when its collected cases were
recorded, or its earlier complete required/file execution establishes completion.
Partial-file counts include only their recorded cases. Failing cases were retained;
they were not silently deselected as passes in later summaries.

| Directory/scope | Passed | Failed | Skipped | Complete files | Partial | Unrun |
|---|---:|---:|---:|---:|---:|---:|
| All 347 unit files | 12,467 | 9 | 22 | 285 | 6 | 56 |
| Five requested/equivalent fake gate files | 114 | 0 | 0 | 5 | 0 | 0 |

The other **108 of 113 fake modules** were unrun; the follow-up requested the whole
unit directory rather than a whole-fake sweep. A fresh base replay of the nine
failures was also unrun when the hold-start window closed. The inherited brief
already identifies importer, QoS and descriptor baseline failures. Their tests
and the implementation files appearing in these failure stacks, plus `bin/codex`,
are unchanged from 15a73b7bde23; a `git diff --exit-code` check confirmed this. This
follow-up does **not** claim fresh base reproduction of the timeout failures.

| Failed case | Observed result |
|---|---|
| `test_descriptors.py::test_c16_6_the_kernel_ceiling_is_read_on_macos` | Kernel ceiling returned `None` in the sandbox. |
| `test_daemon_qos_tools.py::test_thread_priorities_reads_every_thread_of_a_clamped_process` | Sandbox denied `/bin/ps` with `PermissionError`. |
| `test_importer.py::test_states_map_from_rc` | Expected lost attempt remained running. |
| `test_importer.py::test_the_daemon_never_adopts_an_external_run` | Extra lost attempt classified as live imported work. |
| `test_importer.py::test_a_live_claude_process_outside_subfleet_keeps_history_out_and_fences_nothing[True]` | History stayed held by a legacy owner. |
| `test_retention_archive.py::test_parked_jobs_take_turns_at_the_pass_time` | 180 s per-test timeout in `F_FULLFSYNC`; it had passed in the earlier partial file run. |
| `test_mirror_flags_stateful.py::test_the_mirror_follows_its_flag_protocol_on_random_interleavings[0]` | 180 s timeout during `os.replace`, followed by Hypothesis `FlakyStrategyDefinition`. |
| `test_suggestion_structural_errors.py::test_generated_exception_and_caller_text_never_reaches_any_channel` | 180 s timeout during parser construction, wrapped by Hypothesis. |
| `test_codex_shim.py::test_shim_forwards_model_and_original_argv[args0-gpt-6-astra]` | The test's own subprocess timed out after 5 s. |

Partial files and their known remaining cases:

| File | Passed so far | Unrun cases |
|---|---:|---:|
| `tests/unit/test_launch_env_round4.py` | 26 | 14 |
| `tests/unit/test_launch_env_round5.py` | 21 | 18 |
| `tests/unit/test_reset_credit_properties.py` | 1 | 10 |
| `tests/unit/test_sessions_headless_properties.py` | 3 | 2 |
| `tests/unit/test_sessions_mirror_safety.py` | 25 | 3 |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 11 | 19 |

<details><summary>All 56 wholly unrun unit files</summary>

- `tests/unit/test_limit_from_model_text.py`
- `tests/unit/test_live_store_guard_round9_mutations.py`
- `tests/unit/test_live_store_guard_state_root.py`
- `tests/unit/test_mcp_cli_contract.py`
- `tests/unit/test_notify_push.py`
- `tests/unit/test_outcome_unknown.py`
- `tests/unit/test_prune_decision_backup_verification.py`
- `tests/unit/test_resource_identity.py`
- `tests/unit/test_retention_nested_leases.py`
- `tests/unit/test_retention_regenerable.py`
- `tests/unit/test_retention_regenerable_properties.py`
- `tests/unit/test_retention_salvage_properties.py`
- `tests/unit/test_retention_shared_folders.py`
- `tests/unit/test_retention_sweep_race.py`
- `tests/unit/test_retention_turn_contract.py`
- `tests/unit/test_retention_turn_workdirs.py`
- `tests/unit/test_retention_turns.py`
- `tests/unit/test_review_pr127_pr_wakes.py`
- `tests/unit/test_review_pr127_report.py`
- `tests/unit/test_review_pr127_requests.py`
- `tests/unit/test_review_r2_repro.py`
- `tests/unit/test_review_r3_measure.py`
- `tests/unit/test_review_r5_repro.py`
- `tests/unit/test_scheduler.py`
- `tests/unit/test_scheduler_turns.py`
- `tests/unit/test_scrub_key_order.py`
- `tests/unit/test_sessions_mirror.py`
- `tests/unit/test_sessions_mirror_cache_coherence.py`
- `tests/unit/test_sessions_nudge.py`
- `tests/unit/test_sessions_revive.py`
- `tests/unit/test_soak_tools.py`
- `tests/unit/test_stop_alarm.py`
- `tests/unit/test_stop_watchdog.py`
- `tests/unit/test_stopped_daemon.py`
- `tests/unit/test_store_ids.py`
- `tests/unit/test_store_snapshot_plans.py`
- `tests/unit/test_suggest_hint.py`
- `tests/unit/test_suggestion_error_boundaries.py`
- `tests/unit/test_suggestion_properties.py`
- `tests/unit/test_suggestion_secret_properties.py`
- `tests/unit/test_suggestions_review_multiline.py`
- `tests/unit/test_suggestions_review_response.py`
- `tests/unit/test_tick_prefilter.py`
- `tests/unit/test_timers_alerts.py`
- `tests/unit/test_timers_busy_read.py`
- `tests/unit/test_timers_keepalive.py`
- `tests/unit/test_timers_reset_credits.py`
- `tests/unit/test_title_review_regressions.py`
- `tests/unit/test_turn_attachment_replay.py`
- `tests/unit/test_turn_overlap.py`
- `tests/unit/test_turn_replay.py`
- `tests/unit/test_turn_runner.py`
- `tests/unit/test_ultracode_effort.py`
- `tests/unit/test_unadmittable_pin.py`
- `tests/unit/test_wait_hub.py`
- `tests/unit/test_wake_cli.py`

</details>

### Hold accounting and workflow corrections

Own start was **12:15 UTC**, so new holds stopped at **16:45 UTC**. The last hold
was already started before that cutoff and completed afterward. A runner guard
prevents a queued wrapper from beginning pytest after the cutoff. Each test hold
had one outer pytest invocation, no xdist workers, and unset
`SUBFLEET_RETENTION_DORMANT`. Resumptions got separate wrapper holds and deselected
already recorded cases. The directory census includes earlier complete gates,
so those successful files are not counted as unrun merely because a later sweep
batch was queued.

The following rows show per-invocation counts, which intentionally contain repeat
executions; they are not summed for the deduplicated directory census. Manual
rows give pytest elapsed seconds; automatic rows give observed active-wrapper
seconds, including startup/cleanup.

| Hold/result | Passed / failed / skipped | Seconds | Completion |
|---|---:|---:|---|
| `guard.xml` | 275 / 0 / 0 | 11.37 | complete |
| `admission.xml` | 151 / 0 / 0 | 334.20 | complete |
| `unit-1.xml` | 973 / 1 / 0 | 1221.33 | interrupted |
| `sweep-1.xml` | 30 / 1 / 0 | 908.88 | interrupted |
| `sweep-4.xml` | 496 / 0 / 0 | 922.17 | interrupted |
| `auto-02/results.xml` | 385 / 1 / 0 | 1112.03 | time interruption |
| `auto-03/results.xml` | 528 / 0 / 2 | 1122.09 | time interruption |
| `auto-04/results.xml` | 220 / 0 / 0 | 441.12 | harness interruption |
| `auto-05/results.xml` | 354 / 0 / 3 | 454.66 | harness interruption |
| `auto-06/results.xml` | 1350 / 2 / 0 | 1102.89 | time interruption |
| `auto-07/results.xml` | 828 / 3 / 5 | 1114.88 | time interruption |
| `auto-08/results.xml` | 562 / 0 / 4 | 1094.32 | time interruption |
| `auto-09/results.xml` | 1473 / 0 / 0 | 643.39 | complete |
| `auto-10/results.xml` | 1145 / 0 / 2 | 1008.44 | complete |
| `auto-11/results.xml` | 658 / 0 / 0 | 1098.84 | time interruption |
| `auto-12/results.xml` | 2437 / 1 / 6 | 592.74 | complete |
| `auto-13/results.xml` | 970 / 0 / 0 | 452.24 | complete |
| `auto-14/results.xml` | 78 / 0 / 0 | 255.19 | complete |

Workflow deviations/corrections are explicit:

- The first broad unit invocation exceeded the 20-minute rule: **1221.33 s of
  pytest**, before wrapper overhead. Its attempted 18-minute `SIGALRM` bound was
  overwritten by pytest-timeout's per-test alarms. It was interrupted. Later
  holds used an independent thread sending SIGINT, first at 15 minutes and then
  at 18 minutes with cleanup margin; all subsequent active holds stayed below
  20 minutes. The controller's hard-stop fallback was never needed.
- The first controller argument list used literal newline escapes. It was stopped
  without executing test cases, corrected, and excluded from the product census.
  Process-group signaling was denied in this sandbox; the normal foreground
  process arrangement and directly owned pytest PID were used subsequently.
- Two already-started holds hit an evidence-hook `TMPDIR` lookup while tests
  cleared their environment, stopping after 220 passes and 354 passes/3 skips.
  The hook was corrected to capture its path and I/O before tests run. Their
  recorded cases were preserved and untouched cases queued for later holds;
  remaining interrupted cases are visible in the census.
- Two helpers were briefly started while locating the absent brief files. Once
  the inherited no-sub-agent rule was found, both were interrupted before they
  edited files or ran tests. All implementation and verification then ran here.

Environment setup reused one owned CPython 3.14.7 venv. Offline `uv sync` could
not populate its empty private cache, so the locked test package versions were
repacked from existing cache payloads into a small owned wheelhouse and installed
through the setup wrapper. Pytest **9.1.1**, Hypothesis **6.168.1** and pytest-timeout
**2.4.0** were reused with `uv run --offline --no-sync`; no dependency or lock file
changed. All measurement and pytest invocations used the suite wrapper. No
network remote, GitHub call, fetch, push or saturation run occurred.

The required `pgrep -fl 'suite_hold.sh|pytest|lockf'` was attempted before writing
this section; the sandbox cannot enumerate processes (`sysmond service not
found`). Every recorded owned pytest PID was independently checked and was gone;
all foreground wrapper/controller handles had completed and been reaped. Both
helper turns are interrupted. No unrelated process was signalled.

**Cleanup:** Verified complete. The owned `sf-mg2p3` folder is absent, including its venv, wheelhouse, private uv cache, all hold TMPDIRs, Hypothesis storage and probe/evidence files. Two test-created mode-0555 directories needed owner write permission restored inside that folder before the guarded removal succeeded. Five generated untracked repository bytecode caches were removed after checking that they contained no tracked paths. The shared temporary folder was not removed. Workspace-local Git metadata and committed source/report files remain for the hub.

### Per-file census for this follow-up

P = passed, F = failed, E = test-case error, S = skipped, XF = expected failure.
A zero for an unrun file means no recorded execution, not a passing result.
The two never-collected retention files have unknown remaining case counts.

<details><summary>All 347 unit files and five fake gate files</summary>

| File | P | F | E | S | XF | State | Known unrun cases |
|---|---:|---:|---:|---:|---:|---|---:|
| `tests/unit/test_admission_uncapped.py` | 27 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_agent_secret_credentials.py` | 7 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_app_cutover_daemon.py` | 13 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_approval_commit.py` | 6 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_approval_resolution.py` | 33 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_boot_identity.py` | 10 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_capacity_desktop.py` | 20 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_capacity_steer_route.py` | 22 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_capacity_view.py` | 20 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_census_counted.py` | 5 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_census_identity_uncertainty.py` | 8 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_census_procargs.py` | 52 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_census_recycled_pid.py` | 25 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_census_responsibility_failures.py` | 20 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_census_responsibility_identity.py` | 17 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_census_responsibility_races.py` | 34 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_census_survivors_review.py` | 14 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_ci_retry_known_flakes.py` | 72 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_ci_shard_plugin.py` | 48 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_adapter.py` | 170 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_attest.py` | 19 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_cards.py` | 96 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_cards_wiring.py` | 15 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 8 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_identity.py` | 30 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_large_stream.py` | 30 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_mcp.py` | 32 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_stream.py` | 60 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_synthetic_error.py` | 63 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_turn.py` | 69 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_usage.py` | 28 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_claude_v1_parity.py` | 6 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_cli.py` | 248 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_cli_batch.py` | 18 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_cli_turns.py` | 23 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_client_availability.py` | 22 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_codex_adapter.py` | 172 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_codex_ca_bundle.py` | 46 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_codex_probe.py` | 55 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_codex_shim.py` | 30 | 1 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_codex_turn.py` | 67 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_codex_turn_completed.py` | 41 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_compat.py` | 1615 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_continuation_r2.py` | 11 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_catalog.py` | 15 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 21 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_connections.py` | 9 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_diff.py` | 39 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_fifo_reads.py` | 26 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_handoff.py` | 48 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_handoff_briefs.py` | 28 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 9 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_history.py` | 22 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_peers_attachments.py` | 44 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_reconcile.py` | 36 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_redact.py` | 9 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_service.py` | 100 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_state_reads.py` | 18 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_status.py` | 9 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_steer_service.py` | 45 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_store.py` | 32 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_stream_assertion.py` | 13 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_titles.py` | 89 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_waits.py` | 9 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_conversation_wakes.py` | 28 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_cpu_scaling.py` | 2 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_accept.py` | 5 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_connections.py` | 31 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_fifo_reads.py` | 27 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_init_release.py` | 2 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_inspection_properties.py` | 4 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_integration.py` | 7 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_log_cut_windows.py` | 169 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_log_first_pass.py` | 25 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_log_handler.py` | 127 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_log_mutation_tool.py` | 4 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_log_reminders.py` | 101 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_log_scrubbing.py` | 86 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_log_sites.py` | 80 | 0 | 0 | 2 | 0 | complete | 0 |
| `tests/unit/test_daemon_log_tail.py` | 16 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_qos_tools.py` | 3 | 1 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_settle.py` | 61 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_daemon_verbs.py` | 15 | 0 | 0 | 5 | 0 | complete | 0 |
| `tests/unit/test_decision_horizon.py` | 7 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_descriptors.py` | 15 | 1 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_descriptors_desktop.py` | 8 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_desktop_account_cache.py` | 3 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_desktop_in_use.py` | 13 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_desktop_ledger.py` | 4 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_disk_admission.py` | 27 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_disk_floor.py` | 60 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_doctor.py` | 44 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_doctor_ca_bundle.py` | 16 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_doctor_identity.py` | 13 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_empty_provider_results.py` | 334 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_enrollment_runner.py` | 7 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_failed_keep.py` | 2 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_failed_keep_layout.py` | 4 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_failed_keep_prune.py` | 25 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_failed_keep_teardown.py` | 4 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_fake_codex_http.py` | 7 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_filesystem_identity_agreement.py` | 37 | 0 | 0 | 3 | 0 | complete | 0 |
| `tests/unit/test_folders.py` | 23 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_forget_paced.py` | 2 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_fresh_until.py` | 1 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_frontend_probe_cache.py` | 4 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_frozen_provider_streams.py` | 217 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_gate_admission.py` | 24 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_gate_merge.py` | 59 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_gate_replay.py` | 23 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_gate_revision.py` | 16 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_gate_service.py` | 25 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_gate_verdict.py` | 45 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_guard_overlay.py` | 22 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_guard_trust.py` | 98 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_guardian.py` | 13 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_guardian_qos.py` | 14 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_handler_refusals.py` | 317 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_held_waiter_holdback.py` | 6 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_hidden_environment.py` | 7 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_hook_cost.py` | 5 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_hooks.py` | 72 | 0 | 0 | 1 | 0 | complete | 0 |
| `tests/unit/test_hooks_native_guard.py` | 18 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push.py` | 118 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_admission.py` | 15 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_build.py` | 10 | 0 | 0 | 1 | 0 | complete | 0 |
| `tests/unit/test_host_push_build_budget.py` | 15 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_cli.py` | 7 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_config_cost.py` | 7 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_copy.py` | 11 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_export.py` | 10 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_intake.py` | 13 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_metadata.py` | 33 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_mutations.py` | 24 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_names.py` | 13 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_quarantine_restart.py` | 8 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_restart.py` | 4 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_retention.py` | 2 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_storage_recovery.py` | 2 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_storage_retry_budget.py` | 61 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_store_completion_pr183.py` | 2 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_store_interleavings.py` | 26 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_trees.py` | 5 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_host_push_validated_top.py` | 8 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_importer.py` | 145 | 3 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_keychain_exec_guard_properties.py` | 3 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_lane_answers.py` | 27 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_lanes_transfer.py` | 79 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_launch_env_allowlist.py` | 27 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_launch_env_helpers.py` | 20 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_launch_env_mutations.py` | 41 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_launch_env_policy.py` | 26 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_launch_env_round2.py` | 67 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_launch_env_round3.py` | 69 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_launch_env_round4.py` | 26 | 0 | 0 | 0 | 0 | PARTIAL | 14 |
| `tests/unit/test_launch_env_round5.py` | 21 | 0 | 0 | 0 | 0 | PARTIAL | 18 |
| `tests/unit/test_lease_waiter_selection.py` | 6 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_legacy_hold.py` | 50 | 0 | 0 | 6 | 0 | complete | 0 |
| `tests/unit/test_limit_from_model_text.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 95 |
| `tests/unit/test_live_store_guard.py` | 324 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round10.py` | 66 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round10_mutations.py` | 18 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round3.py` | 22 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round4.py` | 8 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round5.py` | 11 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round6.py` | 112 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round7.py` | 50 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round7_mutations.py` | 23 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round8.py` | 100 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round8_mutations.py` | 4 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round9.py` | 89 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_live_store_guard_round9_mutations.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 12 |
| `tests/unit/test_live_store_guard_state_root.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 22 |
| `tests/unit/test_lockwatch.py` | 20 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_machine_device_continuity.py` | 13 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_machine_device_mutations.py` | 6 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_machine_device_properties.py` | 3 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_machine_signals.py` | 25 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_machine_topology.py` | 12 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_mcp_cli_contract.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 6 |
| `tests/unit/test_mcp_scheduler_contract.py` | 5 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_mirror_flags_faults.py` | 29 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_mirror_flags_model.py` | 12 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_mirror_flags_stateful.py` | 1 | 1 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_native_operations.py` | 28 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_notice_rendering.py` | 21 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_notify_push.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 31 |
| `tests/unit/test_offline.py` | 33 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_operator_hold_closures.py` | 15 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_operator_notices.py` | 79 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_outcome_unknown.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 58 |
| `tests/unit/test_person_boundary.py` | 97 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_person_boundary_identity.py` | 25 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_picker.py` | 39 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_placement_ramp.py` | 26 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_policy.py` | 203 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_policy_disk.py` | 57 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_policy_ramp.py` | 46 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_policy_support.py` | 14 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_policy_tier_preferences.py` | 15 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_pr124_fixes.py` | 10 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_pr124_review_r2_fixes.py` | 6 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_pr192_review_r1.py` | 21 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_pr192_review_r2.py` | 9 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_procs.py` | 48 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_provider_active_cap.py` | 50 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_prune_217.py` | 9 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_prune_decision_backup_verification.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 23 |
| `tests/unit/test_prune_decision_fingerprints.py` | 15 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_prune_decisions.py` | 48 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_prune_restore.py` | 4 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_quota_projection.py` | 28 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_rank_review_report.py` | 3 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_reap_liveness.py` | 7 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_rediscovery_git_trace.py` | 17 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_relay.py` | 30 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_render.py` | 20 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_render_quota_projection.py` | 23 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_reserve_authorization_plumbing.py` | 5 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_reset_credit_claim_timing.py` | 2 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_reset_credit_demand.py` | 68 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_reset_credit_properties.py` | 1 | 0 | 0 | 0 | 0 | PARTIAL | 10 |
| `tests/unit/test_reset_credit_review_r1.py` | 22 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_reset_credit_review_r2.py` | 52 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_reset_credit_review_r3.py` | 23 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | 12 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_resource_identity.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 10 |
| `tests/unit/test_retention_81_notes.py` | 38 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_retention_archive.py` | 108 | 1 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_retention_archive_properties.py` | 2 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_retention_case_alias.py` | 56 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_retention_nested_leases.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 27 |
| `tests/unit/test_retention_ordinary_trees.py` | 0 | 0 | 0 | 4 | 0 | complete | 0 |
| `tests/unit/test_retention_qos.py` | 8 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_retention_readback.py` | 37 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_retention_rediscovery.py` | 17 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_retention_regenerable.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 26 |
| `tests/unit/test_retention_regenerable_properties.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 1 |
| `tests/unit/test_retention_salvage_properties.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 2 |
| `tests/unit/test_retention_shared_folders.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | not collected |
| `tests/unit/test_retention_sweep_race.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | not collected |
| `tests/unit/test_retention_turn_contract.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 2 |
| `tests/unit/test_retention_turn_workdirs.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 1 |
| `tests/unit/test_retention_turns.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 7 |
| `tests/unit/test_retention_worktrees.py` | 15 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_review130_pass.py` | 21 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_review_pr127_fixes.py` | 12 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_review_pr127_history.py` | 6 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_review_pr127_pr_wakes.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 7 |
| `tests/unit/test_review_pr127_report.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 1 |
| `tests/unit/test_review_pr127_requests.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 17 |
| `tests/unit/test_review_pr127_service.py` | 8 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_review_r223_conv_probes.py` | 17 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_review_r2_repro.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 31 |
| `tests/unit/test_review_r3_measure.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 6 |
| `tests/unit/test_review_r3_repro.py` | 12 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_review_r4_repro.py` | 27 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_review_r5_repro.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 17 |
| `tests/unit/test_review_r6_probes.py` | 30 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_review_r6_restart.py` | 5 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_round10_refusal_context.py` | 59 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_route_check.py` | 24 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_salvage.py` | 31 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_salvage_optional_transient.py` | 15 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_salvage_ref_race.py` | 9 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_salvage_unindexable.py` | 86 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_scheduler.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 97 |
| `tests/unit/test_scheduler_machine_signals.py` | 13 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_scheduler_priority_callers.py` | 9 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_scheduler_split.py` | 3 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_scheduler_tier_preferences.py` | 9 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_scheduler_turns.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 10 |
| `tests/unit/test_scheduler_weekly.py` | 33 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_scoped_stream_capacity.py` | 17 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_scrub_key_order.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 70 |
| `tests/unit/test_sessions_cli.py` | 142 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_handoff.py` | 117 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_headless_properties.py` | 3 | 0 | 0 | 0 | 0 | PARTIAL | 2 |
| `tests/unit/test_sessions_mirror.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 41 |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 4 |
| `tests/unit/test_sessions_mirror_fifo.py` | 49 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_mirror_hot_health.py` | 6 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_mirror_hot_progress.py` | 16 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | 22 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_mirror_inventory.py` | 8 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_mirror_load_gap.py` | 85 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_mirror_safety.py` | 25 | 0 | 0 | 0 | 0 | PARTIAL | 3 |
| `tests/unit/test_sessions_mirror_write_churn.py` | 3 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_nudge.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 44 |
| `tests/unit/test_sessions_registry.py` | 24 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_revive.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 45 |
| `tests/unit/test_sessions_revive_reads.py` | 10 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_sessions_transcripts.py` | 71 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_settle_properties.py` | 129 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_soak_tools.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 49 |
| `tests/unit/test_status_json.py` | 56 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_steer_invariants.py` | 248 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_stop_alarm.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 2 |
| `tests/unit/test_stop_watchdog.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 29 |
| `tests/unit/test_stopped_daemon.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 72 |
| `tests/unit/test_store_contention_queries.py` | 212 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_store_ids.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 27 |
| `tests/unit/test_store_migration_2.py` | 10 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_store_properties.py` | 3 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_store_readers.py` | 26 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_store_snapshot_plans.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 4 |
| `tests/unit/test_suggest_cli.py` | 7 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_suggest_hint.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 10 |
| `tests/unit/test_suggestion_error_boundaries.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 22 |
| `tests/unit/test_suggestion_properties.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 1 |
| `tests/unit/test_suggestion_secret_properties.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 7 |
| `tests/unit/test_suggestion_structural_errors.py` | 39 | 1 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_suggestions.py` | 30 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 11 | 0 | 0 | 0 | 0 | PARTIAL | 19 |
| `tests/unit/test_suggestions_r3_channels.py` | 14 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_suggestions_review_boundaries.py` | 7 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_suggestions_review_multiline.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 6 |
| `tests/unit/test_suggestions_review_response.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 3 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_tick_prefilter.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 30 |
| `tests/unit/test_timers_alerts.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 17 |
| `tests/unit/test_timers_busy_read.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 115 |
| `tests/unit/test_timers_daemon.py` | 4 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_timers_keepalive.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 14 |
| `tests/unit/test_timers_probe.py` | 35 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_timers_reset_credits.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 47 |
| `tests/unit/test_timers_retention.py` | 20 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/unit/test_title_review_regressions.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 4 |
| `tests/unit/test_turn_attachment_replay.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 13 |
| `tests/unit/test_turn_overlap.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 10 |
| `tests/unit/test_turn_replay.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 33 |
| `tests/unit/test_turn_runner.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 83 |
| `tests/unit/test_ultracode_effort.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 39 |
| `tests/unit/test_unadmittable_pin.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 12 |
| `tests/unit/test_wait_hub.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 44 |
| `tests/unit/test_wake_cli.py` | 0 | 0 | 0 | 0 | 0 | UNRUN | 2 |
| `tests/unit/test_weekly_rank_vs_base.py` | 26 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/fake/test_admission_priority.py` | 33 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/fake/test_admission_ramp.py` | 19 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/fake/test_machine_guard_daemon.py` | 6 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/fake/test_reset_credit_incident.py` | 19 | 0 | 0 | 0 | 0 | complete | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 | 0 | complete | 0 |

</details>

### Commits

- `b88c59142` — tests first: continuity, handle lifecycle, baseline race and
  3,000-sequence properties; corrected topology oracle and registry fake.
- `9f501267e` — three named mutations and their positive controls.
- `a1a637acb` — unknown-state continuity, retained attached handle, baseline identity.
- `13d03cfb7` — executable read-only 300/300-method cost comparison.
- `4c4f8a4d5` — C-6.13 wording and its change-log entry.
- This report commit records final counts, failures, unrun work and cleanup.

All are on workspace-local `fix/mg2-p3-217`; nothing was pushed. The original
round-2 and round-1 reports below remain historical records.

## Round-2 fix (2026-10-22–23)

Both review findings are fixed. **404 distinct required cases passed**, counting
the additional status-eviction regression. The original required gate passed
403 cases; the final machine/scheduler/policy files and fake guard daemon passed
all **259** cases again after the status follow-up. Four requested mutations were
rejected by named regressions, with passing positive controls.

The directory census is **2,343 passed, 5 failed, 77 skipped and 2 xfailed** across
**120 complete files**. All **113 fake files** completed. Only **7 of 333 unit files**
completed; **326 unit files are unrun**, with no partially executed files. This is
not a green or complete whole-suite run. Shared-lock waits consumed most of the
4h30m hold-start window. The unit sweep and controlled release replay were still
queued and were cancelled before pytest started.

No live policy was read or edited. `subfleet/default_policy.json` is byte-identical
on reviewed head `4f3faebdc`, cached release `e345f0803`, and this tree. The default
remains `machine_guard: null`. `MACHINE_GUARD_PROPOSAL` still proposes background
idle minimum 0.15/memory warn and session idle minimum 0.05/memory critical,
**without load or disk thresholds**. Applying it remains Max's explicit policy edit
under d1377. No fetch, push, GitHub request or other network remote was used.

### Merge and conflict resolution

The first commit, **08ba94992**, merges the hub's already-fetched
`origin/release/217` at **e345f08032bb4ee0d7d0b81385c87751f1d262d6**. The ref had advanced
past the brief's `4f5983f4c` and includes it, plus #243's salvage race merge. No fetch
was made. Release's #223, #232 and #238 changes were retained.

There was one textual conflict: the insertion into
`docs/acceptance-contract.md`'s change log. Both MG2 histories were kept, followed
by release's C-13.1 salvage and C-26.4/C-9.2 send-clock entries. Every clause and
change-log entry from both parents remains, with the requested C-6.13 amendments
added separately.

`subfleet/daemon.py` **auto-merged without a textual conflict**. Inspection against
release shows only the MG2 startup, sampler/guard state, status, sample retention
and detached read-point additions. Admission retains release's liveness, priority,
routing and wall/stop accounting, then takes `_machine_reading()` and advances one
guard snapshot for detached admission. Attended admission still reads no machine
source. Release finalization and relay/receipt ordering were retained; no manual
replacement of the admission or finalization body was made.

Shared Git metadata rejected `ORIG_HEAD.lock` and index writes. The worktree's
`.git` pointer is also read-only. Commits therefore live on
`fix/mg2-cpu-idle-guard-217` in workspace-local **`.git-local`**, addressed explicitly
with `git --git-dir=.git-local`. Its object alternate reads the shared object store;
no caller index, branch or checkout was written. The unchanged `.git` pointer still
names the original detached worktree, so the hub must import the local branch/ref.
No history was rewritten.

### Findings: cause and fix

**P2 — cached device identity could never recover.** The sampler resolved its
state device only when `device_id` was `None`. A disappeared cached driver thus
made every later disk reading unreadable. Each actual sample now stats the root's
`st_dev`; a changed root key or failed selected-driver read re-resolves its IOService
ancestry. Stable reads select that registry ID directly without enumerating
neighbors. A root/driver identity change clears disk counters, counter time, EWMA
and old displayed disk observations. A reused driver ID observed on a changed
root also drops calibration under that ID. The same sampler resumes service and
in-flight readings on its **second successful new-device sample with positive
operation deltas**; the first is baseline-only. Missing or zero-operation samples
extend that wait. A failed read resolving to the same identity preserves counters
and smoothing. Unreadable samples create no hold and release no latch.

The quiet percentile is separate calibration evidence: an unresolved device has
none; a device without retained eligible observations has an unreadable baseline
until its first positive finite minute observation is recorded. One observation
suffices for a provisional percentile. Absolute ms/op admission does not wait for
that percentile. C-6.13 states these distinctions and the recovery sample count.

**P3 — baseline age accepted observations the percentile rejected.** The old age
scan accepted any non-`None` raw service, including zero. `_baseline_eligible` now
supplies one eligible set to both percentile and newest-observation time: positive,
finite service, this resolved device, and the 24-hour window. Value and age use
one timestamp/cutoff, including at the exact window boundary. The review's actual
2 ms/op observation at 1000, followed by 0 ns/1 operation at 1999, reports the same
2 ms/op baseline aged **1000 seconds** at 2000. Status also reads each last value
and timestamp together, so eviction during a device transition cannot lose its
timestamp or trigger a second missing-key lookup.

### Decision 3: repeated timing comparison

One AC hold ran both named cases **five times on reviewed head `4f3faebdc` and five
on release parent `e345f0803`**, alternating version order. Ten sequential two-case
pytest invocations kept one pytest active at a time, satisfying the brief's explicit
single-hold comparison. Source archives were owned scratch; working source was
not swapped. Total pytest-process execution was about **656.5 seconds**, below
20 minutes; queue waits are separate.

Each version had **0/5 guard passes and 3/5 timer passes**. Guard failures were the
same missing stub `report.json` after its deadline. Timer failures occurred on
both versions. There is no consistent head slowdown: head medians are lower and
ranges overlap. `subfleet/guard/preflight.py`, `subfleet/timers.py` and both test
files are unchanged between versions; neither tested path constructs the MG2
sampler. The timing code and bounds were left unchanged.

Times below are JUnit **test-call durations**, excluding fixture setup/teardown,
and include assertions/cleanup inside each call. A passing call can exceed 0.4 s
because that bound measures only `probe_cycle`, not the entire call. `F` marks a
failed case. Load is before each two-case invocation; load1 ranged **37.84–103.58**.

| Case/version | Repetition 1 | 2 | 3 | 4 | 5 | Median, s |
|---|---:|---:|---:|---:|---:|---:|
| guard trust, head | 0.603 F | 0.747 F | 0.567 F | 0.892 F | 0.554 F | 0.603 |
| timers, head | 0.251 | 0.475 F | 0.429 | 0.398 | 0.517 F | 0.429 |
| guard trust, release | 0.568 F | 0.749 F | 0.687 F | 0.782 F | 0.894 F | 0.749 |
| timers, release | 0.483 | 0.402 | 0.407 | 0.570 F | 0.485 F | 0.483 |

| Version/repetition | Load1 | Load5 | Load15 |
|---|---:|---:|---:|
| head 1 | 91.42 | 73.00 | 61.64 |
| release 1 | 91.27 | 77.53 | 64.29 |
| release 2 | 70.83 | 75.14 | 64.35 |
| head 2 | 37.84 | 63.74 | 60.92 |
| head 3 | 71.22 | 66.21 | 61.97 |
| release 3 | 85.17 | 70.70 | 63.86 |
| release 4 | 103.58 | 77.84 | 66.95 |
| head 4 | 100.15 | 82.54 | 69.66 |
| head 5 | 90.16 | 82.77 | 70.67 |
| release 5 | 91.98 | 84.68 | 72.27 |

### Device-change cost

An owned root on the real state-volume SSD was sampled **22 times at 1.01 s
spacing on AC**, load1 **44.73–54.92**. Two root-key changes were injected while
resolving/reading the actual SSD; one selected-read failure was injected. These
measure cache invalidation, not a physical remount.

There were **22 sample-level root checks, 22 selected-read attempts, 4 ancestry resolutions and
0 IOKit neighbor enumerations**. One selected attempt was the injected failure.
The four resolutions were startup, two root-key changes and the failed read.
Each resolution also stats the root; those four extra stats are included in
resolution cost, rather than the per-sample check count.
The source unit test separately forbids enumeration on selected-driver reads.

| Operation | N | Median, µs | p99, µs |
|---|---:|---:|---:|
| Root stat | 22 | 31.00 | 103.33 |
| Ancestry resolution | 4 | 3479.48 | 5832.71 |
| Complete steady sampler read | 18 | 1548.90 | 2711.46 |
| Startup/root-change sampler read | 3 | 5671.50 | 7347.62 |
| Failed-read sampler read (same resolved ID) | 1 | 2022.46 | 2022.46 |

Costs are observed wall time including host scheduling, not latency bounds.
With these small N, p99 is effectively a maximum. Startup/change samples include
cold startup; the failed-read sample resolved the same driver. A failed lookup
resolving another driver can make one bounded extra counter attempt in that tick
to establish its baseline. Stable sampling does no ancestry resolution or neighbor
enumeration. These sampler measurements exclude the SQLite minute transaction;
round 1's complete daemon/store cost remains below.

`df -g /` showed **86 GiB available**. Round 2 started **zero saturation runs** and
suggests no disk threshold. The absence of a controlled saturation distribution
remains explicit. Round-1 measurements and the historical 10/11 unknown-service
caveat are retained below.

### Invariants and evidence

| Invariant | Passing evidence |
|---|---|
| R2-1. Null/load/memory-only decisions and hold records equal 473fe2941; no new native read | Retained frozen `machine_guard_reference.py` differential and generated readings including stray new fields; native-call traps also cover root stat and device resolution; fake daemon legacy admission traps. |
| R2-2. Unreadable never holds or releases a latch | Retained generated bad-signal and timed recovery tests, plus `test_unreadable_topology_does_not_hold_or_release_disk_latch`. |
| R2-3. New-device readings resume without sampler recreation; no cross-device counters or EWMA | Exact four-step input; still-readable old neighbor; failed-read re-resolution with unchanged root stat; reused registry ID with increasing counters; bounded generated appear/disappear/replace/return/reuse sequences. First successful sample is unreadable; next positive delta is readable. |
| R2-4. Baseline age is its newest eligible observation's age | Exact zero-service sampler input, generated zero/negative/missing/non-finite/other-device/stale observations, and one-cutoff boundary regression. |
| R1-3. Monotone | Retained paired generated CPU/disk histories. |
| R1-4. Held session implies held background | Retained stateful ordering with ordered thresholds and memory OR conditions. |
| R1-5. Attended/priority never held | Retained generated exemptions, including invalid policies naming exempt classes. |
| R1-6. Release requires sampled clear time and margins | Retained ten-second, repeated-snapshot, marginal and missing-signal recovery tests. |
| R1-7. First detached pass sees primed data | Real-sampler fake-daemon 18-busy-CPU first-pass regression. |

Both new properties use deterministic bounded CI settings: **40 examples**, no
database and no deep profile. Existing bounded round-1 properties remain intact.

### Tests first, mutations and required files

The initial seven topology/age regressions produced **7 failures in 59.94 s**
before the sampler fix, at intended assertions, with no collection/setup errors.
The merged tree's machine source was still the reviewed head's source.
The first feature gate had **255 passed / 1 failed**: its restored-baseline fixture
queried calibration before resolving its device. The fixture now performs that
first read; the final daemon integration cases pass. Additional tests were
committed before window-boundary, reused-ID and status-eviction fixes.

The first combined required run had **403 product passes**, plus four auxiliary
positive passes, but its cached-resolution mutation unexpectedly passed because
it retained the new failed-read fallback. An EWMA probe also had an ambiguous
anchor. Those probes were **not accepted as evidence**. They were corrected:
cache-only resolution removes both recovery routes, and anchor validation is
outside expected-failure handling. The repaired hold had **16 passed / 4 strict
expected failures**: 11 topology cases, four positive controls and the native cost
probe passed; all four mutated copies failed their named regression. Each mutant
was compiled as an isolated module copy; production files were never mutated.
The final status follow-up is covered by the twelfth topology test and the final
259-case guard gate in the first whole-suite hold.

| Mutation | Named failing regression | Rejected |
|---|---|---:|
| Resolve only while cached ID is None | `test_review_four_step_device_replacement_resumes_without_recreation` | 1 |
| Keep old counters on a device change | `test_reused_registry_id_on_new_root_discards_counter_baseline` | 1 |
| Keep old EWMA on a device change | `test_review_four_step_device_replacement_resumes_without_recreation` | 1 |
| Date baseline from any observation | `test_review_zero_service_observation_does_not_refresh_baseline_age` | 1 |

Required files, using each file's latest complete execution:

| File | Passed |
|---|---:|
| `tests/unit/test_machine_signals.py` | 25 |
| `tests/unit/test_machine_topology.py` | 12 |
| `tests/unit/test_scheduler_machine_signals.py` | 13 |
| `tests/unit/test_policy.py` | 203 |
| `tests/fake/test_machine_guard_daemon.py` | 6 |
| `tests/fake/test_admission_priority.py` | 33 |
| `tests/fake/test_admission_ramp.py` | 19 |
| `tests/unit/test_admission_uncapped.py` | 27 |
| `tests/unit/test_scheduler_priority_callers.py` | 9 |
| `tests/fake/test_reset_credit_incident.py` | 19 |
| `tests/fake/test_state_contract.py` | 37 |
| `tests/unit/test_test_module_names.py` | 1 |

The priority and ramp modules named as unit paths in the brief actually live in
`tests/fake`. `rg -l machine_hold tests/fake` identifies
`test_reset_credit_incident.py`; it passed. Frozen reference/model helpers are not
pytest modules. `git diff --check` passed for owned changes; the merge contains
release's pre-existing blank line at EOF in `tests/wall_delivery.py`.

### Directory census, failures and unrun work

One pytest ran at a time through the hub wrapper; regular holds used one pytest
each, with at most four workers. The explicit timing comparison is the sequential
repeated-invocation exception described above. Python was GIL CPython 3.14.7 with
the same reused venv. Setup used the separate host setup wrapper and cached
packages offline. No dependency or lock file changed.

The table deduplicates required and whole-suite executions by file, choosing the
latest complete run. Timing repetitions and scratch mutation modules are separate
evidence. Four guard/policy unit files were included with the first fake batch to
verify the final status correction.

| Directory | Complete files | Passed | Failed | Skipped | Xfailed | Unrun files | Partial files |
|---|---:|---:|---:|---:|---:|---:|---:|
| `tests/unit` | 7 / 333 | 290 | 0 | 0 | 0 | 326 | 0 |
| `tests/fake` | 113 / 113 | 2,053 | 5 | 77 | 2 | 0 | 0 |
| Total | 120 / 446 | 2,343 | 5 | 77 | 2 | 326 | 0 |

| Whole-suite hold | Files | Passed / failed / skipped / xfailed | Pytest seconds |
|---|---:|---:|---:|
| `whole-fake-00` (57 fake + 4 unit) | 61 | 1459 / 4 / 65 / 2 | 678.03 |
| `whole-fake-01` | 56 | 847 / 1 / 12 / 0 | 688.48 |
| Dedicated unit sweep | Not started; 326 files have no other complete run | Unrun | — |

**The nine review-confirmed base failures remain out of scope and unchanged.**
The four daemon-stack failures occurred again here, before guard initialization.
The other five unit failures were confirmed on base by review r2, but their full
files were unrun in this job:

- `test_importer.py::test_states_map_from_rc`: running instead of lost under
  unavailable PID inspection.
- `test_importer.py::test_the_daemon_never_adopts_an_external_run`: extra lost
  attempt in the external skip list.
- `test_importer.py::test_a_live_claude_process_outside_subfleet_keeps_history_out_and_fences_nothing[True]`:
  unavailable process-environment inspection prevents ownership recognition.
- `test_daemon_qos_tools.py::test_thread_priorities_reads_every_thread_of_a_clamped_process`:
  `/bin/ps` permission failure.
- `test_descriptors.py::test_c16_6_the_kernel_ceiling_is_read_on_macos`: unavailable
  kernel descriptor ceiling.
- `test_daemon_stacks.py::test_a_daemon_without_the_flag_is_never_signalled`.
- `test_daemon_stacks.py::test_a_daemon_whose_lock_says_stack_dumps_is_dumped_and_lives`.
- `test_daemon_stacks.py::test_a_lock_that_changes_between_the_two_reads_is_not_signalled`.
- `test_daemon_stacks.py::test_a_daemon_writes_the_flag_after_the_handler_and_drops_it_before`.

**Additional observed failure:**
`test_unadmittable_pin_admission.py::test_c11_8_held_told_once_blocking_nobody_then_failed_rc_3[unknown]`
expected the younger job on claude-7 but got claude-9. Both lanes are healthy in
this parameterization; only the stuck job's pin becomes nonexistent claude-99.
The retained owned failure DB has these exact readings:

| Lane | Observed at | Weekly reset |
|---|---|---|
| claude-9 | 2026-10-23T06:29:09Z | 2026-10-24T06:29:09Z |
| claude-7 | 2026-10-23T06:29:10Z | 2026-10-24T06:29:10Z |

`rank_key` prefers the earlier reset before lane ID, so these inputs select
claude-9. The test and ranking function are unchanged on release. This supports
a fixture assumption about equal whole-second resets, independent of MG2. A
controlled release replay with equal versus one-second-apart resets was prepared
but **remained queued and was cancelled at the cutoff**. It is not claimed as an
executed base reproduction or added to the nine confirmed cases. No production
routing or unrelated test was edited.

All **326 unrun unit filenames** are explicitly marked `UNRUN` below. They include
the full guard-trust/timers files (only named cases ran in the separate comparison),
importer, QoS, descriptor and `test_prune_restore.py` files. No claim is made about
their full-file results in round 2. Frontend/e2e and other Python versions were not run.

<details><summary>All 446 files: latest complete counts and every unrun filename</summary>

P/F/S/X = passed/failed/skipped/xfailed. Zero observed collection/setup errors.

| File | P | F | S | X | Status / evidence |
|---|---:|---:|---:|---:|---|
| `tests/fake/test_admission_disk.py` | 27 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_admission_held_waiters.py` | 1 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_admission_latency.py` | 14 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_admission_lease_waiters.py` | 16 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_admission_liveness.py` | 32 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_admission_priority.py` | 33 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_admission_properties.py` | 27 | 0 | 0 | 1 | complete; whole-fake-00 |
| `tests/fake/test_admission_ramp.py` | 19 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_admission_ramp_retention.py` | 11 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_admission_route_isolation.py` | 93 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_admission_stateful.py` | 17 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_admission_visibility.py` | 43 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_batch_label.py` | 9 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_canonical_identity.py` | 24 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_canonical_identity_fix2.py` | 21 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_canonical_identity_fix3.py` | 17 | 0 | 0 | 1 | complete; whole-fake-00 |
| `tests/fake/test_canonical_identity_fix4.py` | 11 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_canonical_identity_fix5.py` | 14 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_capacity_steer_daemon.py` | 72 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_capacity_wait_backoff.py` | 30 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_census_escapers_daemon.py` | 0 | 0 | 9 | 0 | complete; whole-fake-00 |
| `tests/fake/test_census_recycled_pid_daemon.py` | 29 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_codex_ca_bundle_fake.py` | 0 | 0 | 6 | 0 | complete; whole-fake-00 |
| `tests/fake/test_codex_turn_completed_fake.py` | 109 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_conversation_pools.py` | 2 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_conversation_wakes_fake.py` | 4 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_crash_matrix.py` | 0 | 0 | 9 | 0 | complete; whole-fake-00 |
| `tests/fake/test_daemon_contract.py` | 0 | 0 | 21 | 0 | complete; whole-fake-00 |
| `tests/fake/test_daemon_descriptors.py` | 12 | 0 | 1 | 0 | complete; whole-fake-00 |
| `tests/fake/test_daemon_launch.py` | 9 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_daemon_stacks.py` | 1 | 4 | 1 | 0 | complete; whole-fake-00 |
| `tests/fake/test_descriptor_exhaustion.py` | 0 | 0 | 2 | 0 | complete; whole-fake-00 |
| `tests/fake/test_detached_cwd_pins.py` | 18 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_detached_retention_fence.py` | 89 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_enrollment_guardian.py` | 0 | 0 | 1 | 0 | complete; whole-fake-00 |
| `tests/fake/test_fake_provider.py` | 13 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_finalization_replay.py` | 10 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_folder_spelling.py` | 7 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_frozen_stream_nonregular.py` | 140 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_gate_end_to_end.py` | 10 | 0 | 1 | 0 | complete; whole-fake-00 |
| `tests/fake/test_identity_rollback.py` | 5 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_idle_cost.py` | 0 | 0 | 2 | 0 | complete; whole-fake-00 |
| `tests/fake/test_interactive_claude_titles.py` | 6 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_interactive_steer.py` | 13 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_lane_fault.py` | 20 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_lane_pilot.py` | 24 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_lanes_enroll.py` | 15 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_lanes_reenroll.py` | 19 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_lanes_reenroll_keychain.py` | 1 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_launch_env_daemon.py` | 49 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_launch_env_providers.py` | 6 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_launch_gates_above_stdio.py` | 0 | 0 | 9 | 0 | complete; whole-fake-00 |
| `tests/fake/test_lost_acknowledgement.py` | 0 | 0 | 3 | 0 | complete; whole-fake-00 |
| `tests/fake/test_machine_guard_daemon.py` | 6 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_main_refusal_write_dir.py` | 5 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_mcp_job_contract.py` | 12 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_native_maintenance_startup.py` | 41 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/fake/test_notice_delivery.py` | 15 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_notice_header.py` | 23 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_picker_native.py` | 3 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_policy_sandbox.py` | 25 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_probe_cancel.py` | 24 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_probe_recovery.py` | 9 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_probe_turn.py` | 43 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_prompt_seam.py` | 8 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_provider_active_cap_fake.py` | 60 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_provider_cap_review_evidence.py` | 5 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_provider_qos.py` | 0 | 0 | 2 | 0 | complete; whole-fake-01 |
| `tests/fake/test_record_secrecy.py` | 44 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_request_routing.py` | 3 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_reserve_authorization.py` | 12 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_reserve_authorization_validation.py` | 11 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_reset_credit_incident.py` | 19 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_resume_cli.py` | 0 | 0 | 1 | 0 | complete; whole-fake-01 |
| `tests/fake/test_resume_contract.py` | 23 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_retention_dormant.py` | 7 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_retry_for_good_refusals.py` | 8 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_review130_ab.py` | 12 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_review_r185_adversarial.py` | 1 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_review_r223_wall_probes.py` | 23 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_route_view.py` | 8 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_routing_end_to_end.py` | 14 | 0 | 2 | 0 | complete; whole-fake-01 |
| `tests/fake/test_routing_tier_preferences.py` | 5 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_salvage_adversarial.py` | 8 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_sessions_end_to_end.py` | 27 | 0 | 6 | 0 | complete; whole-fake-01 |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_state_fixture_processes.py` | 4 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_status_payload.py` | 2 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_suggestions_fake.py` | 1 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_tier_hold_by_demand.py` | 27 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_timers_busy_admission.py` | 1 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_timers_end_to_end.py` | 6 | 0 | 1 | 0 | complete; whole-fake-01 |
| `tests/fake/test_turn_listing.py` | 9 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_turn_retention_cwd.py` | 6 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_turn_retention_cwd_properties.py` | 1 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_turn_retention_job_directory.py` | 2 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_turn_retention_job_properties.py` | 1 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_turn_retention_quarantine.py` | 10 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_turn_trees.py` | 18 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_turn_wait_reasons.py` | 7 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_unadmittable_pin_admission.py` | 23 | 1 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_unadmittable_pin_rereview.py` | 6 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_unadmittable_pin_review.py` | 7 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_wall_cancel_races.py` | 30 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_wall_inspection_order.py` | 18 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_wall_running_time.py` | 35 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_wall_stop_deliveries.py` | 25 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_workspace_contract.py` | 8 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_workspace_enospc.py` | 1 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_workspace_transient.py` | 24 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_writable_fanout.py` | 28 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/fake/test_writer_retention_fence.py` | 23 | 0 | 0 | 0 | complete; whole-fake-01 |
| `tests/unit/test_admission_uncapped.py` | 27 | 0 | 0 | 0 | complete; required gate |
| `tests/unit/test_agent_secret_credentials.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_app_cutover_daemon.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_approval_commit.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_approval_resolution.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_boot_identity.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_capacity_desktop.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_capacity_steer_mutations.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_capacity_steer_route.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_capacity_view.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_census_counted.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_census_identity_uncertainty.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_census_procargs.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_census_recycled_pid.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_census_responsibility_failures.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_census_responsibility_identity.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_census_responsibility_races.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_census_survivors_review.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_ci_retry_known_flakes.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_ci_shard_plugin.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_adapter.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_attest.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_cards.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_cards_wiring.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_conversation_mcp_scope.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_identity.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_large_stream.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_mcp.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_mcp_launch.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_stream.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_synthetic_error.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_turn.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_usage.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_claude_v1_parity.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_cli.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_cli_batch.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_cli_turns.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_client_availability.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_codex_adapter.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_codex_ca_bundle.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_codex_ca_bundle_provider.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_codex_probe.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_codex_shim.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_codex_turn.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_codex_turn_completed.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_compat.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_continuation_r2.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_catalog.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_catalog_lifecycle.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_connections.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_diff.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_fifo_reads.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_handoff.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_handoff_briefs.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_history.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_peers_attachments.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_reconcile.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_redact.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_service.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_state_reads.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_status.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_steer_service.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_store.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_stream_assertion.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_titles.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_waits.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_conversation_wakes.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_cpu_scaling.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_accept.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_connections.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_fifo_reads.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_init_release.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_inspection_load.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_inspection_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_integration.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_log_scrubbing.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_qos_tools.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_refusals_cli.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_settle.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_daemon_verbs.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_decision_horizon.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_descriptor_review_fixes.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_descriptors.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_descriptors_desktop.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_desktop_account_cache.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_desktop_in_use.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_desktop_ledger.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_disk_admission.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_disk_floor.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_doctor.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_doctor_ca_bundle.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_doctor_identity.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_empty_provider_results.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_enrollment_runner.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_failed_keep.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_fake_codex_http.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_filesystem_identity_agreement.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_folders.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_forget_paced.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_fresh_until.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_frozen_provider_streams.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_gate_admission.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_gate_merge.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_gate_replay.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_gate_revision.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_gate_service.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_gate_verdict.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_guard_overlay.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_guard_trust.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_guardian.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_guardian_qos.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_handler_refusals.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_held_waiter_holdback.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_hidden_environment.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_hook_cost.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_hooks.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_hooks_native_guard.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_admission.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_build.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_build_budget.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_cli.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_config_cost.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_copy.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_export.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_intake.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_metadata.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_mutations.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_names.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_quarantine_restart.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_restart.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_retention.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_storage_recovery.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_storage_retry_budget.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_store_completion_pr183.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_store_interleavings.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_trees.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_host_push_validated_top.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_importer.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_inspection_pacing_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_invariants_index.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_keychain_exec_guard_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_lane_answers.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_lanes_transfer.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_launch_env_allowlist.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_launch_env_helpers.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_launch_env_mutations.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_launch_env_policy.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_launch_env_round2.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_launch_env_round3.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_launch_env_round4.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_launch_env_round5.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_lease_waiter_selection.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_legacy_hold.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_limit_from_model_text.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round10.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round10_mutations.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round3.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round4.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round5.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round6.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round7.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round7_mutations.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round8.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round8_mutations.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round9.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_round9_mutations.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_live_store_guard_state_root.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_lockwatch.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_machine_signals.py` | 25 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/unit/test_machine_topology.py` | 12 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/unit/test_mcp_cli_contract.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_mcp_scheduler_contract.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_mirror_flags_faults.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_mirror_flags_model.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_mirror_flags_stateful.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_native_operations.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_notice_rendering.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_notify_push.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_offline.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_operator_hold_closures.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_operator_notices.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_outcome_unknown.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_person_boundary.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_person_boundary_identity.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_picker.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_placement_ramp.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_policy.py` | 203 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/unit/test_policy_disk.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_policy_ramp.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_policy_support.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_policy_tier_preferences.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_pr124_fixes.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_pr124_review_r2_fixes.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_pr192_review_r1.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_pr192_review_r2.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_procs.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_protocol_secrecy.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_provider_active_cap.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_prune_217.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_prune_decision_backup_verification.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_prune_decision_fingerprints.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_prune_decisions.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_prune_restore.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_quota_projection.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_rank_review_report.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_reap_liveness.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_rediscovery_git_trace.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_relay.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_render.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_render_quota_projection.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_reserve_authorization_plumbing.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_reset_credit_claim_timing.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_reset_credit_demand.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_reset_credit_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_reset_credit_review_r1.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_reset_credit_review_r2.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_reset_credit_review_r3.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_resource_identity.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_81_notes.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_archive.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_archive_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_case_alias.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_nested_leases.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_ordinary_trees.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_qos.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_readback.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_rediscovery.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_regenerable.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_regenerable_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_salvage_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_shared_folders.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_sweep_race.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_turn_contract.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_turn_workdirs.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_turns.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_retention_worktrees.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review130_pass.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_pr127_fixes.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_pr127_history.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_pr127_pr_wakes.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_pr127_report.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_pr127_requests.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_pr127_service.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_r223_conv_probes.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_r2_repro.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_r3_measure.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_r3_repro.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_r4_repro.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_r5_repro.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_r6_probes.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_review_r6_restart.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_round10_refusal_context.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_route_check.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_salvage.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_salvage_optional_transient.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_salvage_ref_race.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_salvage_unindexable.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_scheduler.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_scheduler_machine_signals.py` | 13 | 0 | 0 | 0 | complete; whole-fake-00 |
| `tests/unit/test_scheduler_priority_callers.py` | 9 | 0 | 0 | 0 | complete; required gate |
| `tests/unit/test_scheduler_split.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_scheduler_tier_preferences.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_scheduler_turns.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_scheduler_weekly.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_scoped_stream_capacity.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_scrub_key_order.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_cli.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_handoff.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_handoff_excerpts.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_headless_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_mirror.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_mirror_fifo.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_mirror_hot_health.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_mirror_hot_progress.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_mirror_inventory.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_mirror_load_gap.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_mirror_safety.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_mirror_write_churn.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_nudge.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_registry.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_revive.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_revive_reads.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_sessions_transcripts.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_settle_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_soak_tools.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_status_json.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_steer_invariants.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_stop_alarm.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_stop_watchdog.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_stopped_daemon.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_store_contention_queries.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_store_ids.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_store_migration_2.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_store_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_store_readers.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_store_snapshot_plans.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggest_cli.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggest_hint.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggestion_error_boundaries.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggestion_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggestion_secret_properties.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggestion_structural_errors.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggestions.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggestions_r1b_adversarial.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggestions_r3_channels.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggestions_review_boundaries.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggestions_review_multiline.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_suggestions_review_response.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 | 0 | complete; required gate |
| `tests/unit/test_tick_prefilter.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_timers_alerts.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_timers_busy_read.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_timers_daemon.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_timers_keepalive.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_timers_probe.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_timers_reset_credits.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_timers_retention.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_title_review_regressions.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_turn_attachment_replay.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_turn_overlap.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_turn_replay.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_turn_runner.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_ultracode_effort.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_unadmittable_pin.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_wait_hub.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_wake_cli.py` | — | — | — | — | **UNRUN** |
| `tests/unit/test_weekly_rank_vs_base.py` | — | — | — | — | **UNRUN** |

</details>

### Round-2 commits and handoff

- `08ba94992`: merge cached release/217, preserving contract clauses/change logs.
- `ad6c3738b`: tests first for replacement, eligibility and selected-device reads.
- `50d8d7c75`: reused IDs, exact sampler age input, one-cutoff and legacy traps.
- `5c46f40c4`: device recovery, direct reads, reset counters/EWMA and shared eligibility.
- `f697e7704`: test status eviction during a device change.
- `92f9b9254`: fetch each displayed value and timestamp together.
- This documentation/handoff commit records final evidence and the incomplete unit sweep.

All owned foreground pytest, measurement and wrapper roots exited or were reaped.
Queued holds were cancelled before pytest started. No detached process was started.
The required `pgrep -fl 'suite_hold.sh|pytest|lockf'` was attempted before writing
this deliverable, but this sandbox cannot enumerate processes
(`sysmond service not found`). No unrelated process was signalled.

Scratch cleanup is verified complete: the owned sf-r241f2 folder is absent, including venv, uv cache, source archives, probe files and all hold TMPDIRs. Five owned untracked repository bytecode caches were removed after checking that they contained no tracked files. Repository bytecode, pytest-cache and Hypothesis directories now count zero. Cleanup used the requested guarded shutil.rmtree command and never removed the shared temp folder.

The six code/test/merge commits and this report use workspace-local `.git-local`.
Logs, JSON/XML evidence, wheels, venv, caches and source archives are not staged.
No push. The round-1 report is retained below as historical evidence; its whole
suite counts and cost runs are not presented as a round-2 unit sweep.

---

## Round-1 fix (2026-10-22)

All requested unit and fake files ran: **437 files, 15,241 passed, 11 failed,
98 skipped and 2 xfailed**, with **zero unrun or partial files**. The 392 required
feature/admission/state cases passed. All eight mutations were caught. Both
additional timing failures passed on isolated retry. This is not a green whole
suite: the original 11 failures remain recorded below.

Live policy was neither read nor edited. The shipped `machine_guard: null` and
`subfleet/default_policy.json` bytes are unchanged from `f873a3fb3`. Max's live
thresholds remain his. No push or fetch was made; no GitHub/CI query was made.
Background aging is not implemented by this change.

### Merge and workspace

Merged the hub's already-cached `origin/release/217`, **43f42d83d**, without fetching.
It contains the brief's **2eacd5806** (#212 frozen-stream follow-ups), plus the
later cached release merges #232 and #238. Merge commit: **e6cd884ca**.

There was one textual conflict, at the acceptance contract's change-log insertion.
It was resolved by retaining every entry from both sides. Every contract clause
from release was retained, including C-4.3's frozen-stream checks and the separate
C-17.8/C-17.9 clauses. The subsequent C-6.13/C-6.11 amendments have their own entries.
`subfleet/daemon.py` auto-merged without a textual conflict. Its admission pass
keeps release's liveness, priority, lease/quarantine and routing calculations,
then advances one MG2 snapshot for detached admission; release's finalization
changes remain intact. The reader later became `_machine_reading()` at that same
admission point. Attended passes still sample nothing.

The sandbox refused shared Git metadata (`ORIG_HEAD.lock: Operation not permitted`).
Commits therefore use workspace-local **`.git-local`**, branch
`fix/mg2-cpu-idle-guard-217`, with a read-only object alternate to the original
repository. It began as `.mg2-git` and was renamed to the repository's already
ignored `.git-local` location. The caller's checkout and shared index were not
written. History was not rewritten.

### Findings: cause and fix

1. **P1 — activity mistaken for saturation.** Summed overlapping service time per
   wall second is average requests in flight. Capping it at 1.0 made every allowed
   old disk threshold bind at healthy load. The new `disk_busy` is **service ms/op
   on the device holding the state root**, smoothed over time; uncapped requests
   in flight are a separate calibration field. `disk_busy_max` accepts positive
   finite ms/op without a ceiling. The proposal carries no disk threshold.
2. **P2 — startup and lost-read gaps.** The first old sample had no delta, and a
   lost/zero-tick read discarded history and released latches. A new-signal daemon
   now primes once, allows a full one-second interval before offering admission,
   and gives its first pass a usable delta. Lost/zero-tick reads preserve the valid
   baseline and EWMA, hold nothing themselves, and preserve the internal latch;
   they interrupt its recovery timer. Bias-corrected EWMA weights prevent a short
   initial interval from retaining a full sample's weight.
3. **P3 — escaped exceptions.** Narrow exception lists missed `ctypes.ArgumentError`.
   Each source catches `Exception`, never `BaseException`. Tests inject at the
   actual Mach and IOKit calls, as well as at reader boundaries.
4. **P3 — enumeration.** The old 64-loop boundary really accepted only 63 devices,
   and one unreadable neighbor invalidated the reading. Enumeration now includes
   exactly the first **1,024** entries, ignores later entries, skips bad IDs or
   statistics, and releases each acquired device and CF value.
5. **P3 — per-pass overhead.** Native reads previously ran every detached pass,
   including null/legacy policies. They now run at most **once per second**, with
   intervening ticks reusing the snapshot. Null and load/memory-only policies run
   no new CPU/IOKit read and no new calibration write. Legacy field decisions and
   hold records retain the frozen `473fe2941` behavior.
6. **P3 — invisible cost/readings.** `read_cost_s` was returned and dropped.
   `daemon.status` / `status --json` now expose CPU idle, disk ms/op, requests in
   flight, quiet baseline and last read cost, each with units, age and readability.
   At most one sample per minute is persisted. Recording time is separate from
   observation time so restarting on an older snapshot cannot authorize an early
   write. Restored baseline age describes its calibration observations.
7. **P3 — first EWMA weight.** The old first delta initialized the value with full
   weight. Both new signals maintain numerator and accumulated weight from zero,
   using `alpha = -expm1(-elapsed/60)` and returning numerator/weight. Cadence also
   removes 50 ms native samples. A one-second initial sample followed by a minute
   of contrary data has only its appropriate time weight.
8. **P3 — oversized load threshold.** `math.isfinite(10**400)` could raise before
   validation. Positive numeric validation catches conversion overflow and raises
   `PolicyError` naming the load/disk key; CPU fractions reject oversized values
   before conversion. Booleans, non-finite floats, zero disk thresholds and bad
   types remain rejected.
9. **P3 — contract evidence and claims.** C-6.13 restores the full 9/27 “Why” evidence,
   states the real 1,024 limit, changes recovery to ten seconds, documents status
   and retained samples, and withdraws the old 10/11/disk calibration claim.
10. **P3 — coverage.** Timestamped bounded properties exercise the stateful CPU/disk
    guard, ordered classes, memory OR conditions and exemptions even when an
    unvalidated policy names attended/priority. Legacy differentials include stray
    new-signal fields. The process test runs a fresh interpreter with an audit hook
    for `subprocess.Popen`, `os.system`, `os.posix_spawn`, `os.exec*`, `os.fork` and
    `os.forkpty`, including first native-library load.

### Disk design, baseline and release

Mach per-CPU user/system/idle/nice counters give weighted interval CPU idle.
IOKit `IOBlockStorageDriver` statistics give read/write service nanoseconds and
read/write operation counts. The disk term is
`(delta read service + delta write service) / delta operations / 1e6`.
The companion is the same service sum divided by elapsed wall nanoseconds,
**without a cap**. `devname(st_dev)` and the volume's IOService ancestry identify
the state-root driver; an unrelated busy device cannot substitute for it.
Readers use explicit native library paths and start no subprocess.

Both signals use bias-corrected **60 s** EWMAs on a **1 s** cadence. The quiet
baseline is the lower empirical tenth percentile of usable positive service-time
observations among up to **1,440 minute samples in the preceding 24 hours**,
filtered to this device. It is restored from the bounded events on restart.
Early baseline estimates are provisional; the baseline never chooses a policy
threshold automatically. A lost native read retains its prior counter baseline
and EWMA. Counter reset/topology change requires a new counter baseline.

Each class/signal latch releases after **ten seconds of sampled clear recovery**:
CPU idle at least 0.03 above its minimum (clamped to 1), or disk service at most
90% of its maximum. Busy, marginal or unreadable readings interrupt recovery.
Missing data creates no hold and releases no latch. Repeated jobs, cached passes
and `why` cannot turn one observation into recovery time. Threshold edits clear
latches. Conditions combine by OR; legacy load/memory remain stateless.

### Measured shapes

The first recording observed existing activity on **AC power**, for 90 seconds,
using the one-second native cadence. It captured **81 readable CPU/disk samples**
on state-root device **4294970953**. Load1 ranged 22.18–53.27; CPU idle EWMA ranged
31.98–37.24%. Every in-flight value exceeded one, so the old cap was **1.000 on
81/81 samples**. Reconstructed service/operation counter deltas replay through the
real sampler and guard; the proposal holds neither class. The old source fails
that replay at the admission assertion.

| First ordinary recording | N | Minimum | Median | p99 / maximum |
|---|---:|---:|---:|---:|
| Smoothed service, ms/op | 81 | 1.662125 | 2.059045 | 2.263775 |
| Raw interval service, ms/op | 81 | 1.034776 | 1.972808 | 6.432744 |
| Uncapped requests in flight | 81 | 6.950450 | 23.705070 | 76.977192 |

The short recording's raw-service p10 was **1.358420 ms/op**. It does not establish
a full-day quiet baseline. A later AC read-point cost run observed another 79
samples: smoothed service ranged 0.292746–1.352092
ms/op (median 0.379916); this was another existing-load run,
not an artificial saturation control.

**Deliberately saturated device: not measured.** `df -g /` showed 68, then 67
available 1G-blocks before the planned experiment, and 52 at the final measurement.
The required gate is above 70. Consequently **zero stress runs** were started and
no scratch stress file was created. Its distribution is unknown; no disk threshold
is suggested. Separation of quiet and deliberate saturation remains unverified.

**10/11 correction:** the old capped term would have held the 398/316-load shape
by disk, despite idle CPU headroom. The new service-time value for that historical
shape is **unknown**: its per-operation service counters were not recorded.
At 32% CPU idle it starts no CPU hold. The current proposal has no disk condition;
normal/readable memory is a separate assumption, not a reconstructed incident fact.
For 10/20, the builder's direct 0%-idle measurement holds both classes. The earlier
1,358% whole-host CPU sum implies about 24.6% idle on 18 CPUs and alone holds neither.

### Cost at the offered 50 ms control tick

The final **AC** run calls the actual `Daemon._machine_reading` method on an owned
reader object and temporary SQLite store. It starts no running daemon and touches
no live state. It includes policy enablement checks and the bounded minute writes.
Native reads remain at least one second apart. The 90 s run delivered **472 ticks
and 79 native reads**; scheduling/timer coalescing delayed the offered 50 ms ticks.
Rows were buffered until timing completed. Costs are wall time, including host
scheduling; these are observations, not latency bounds. Native-read p99 has coarse
resolution because there are fewer than 100 reads.

| Measured operation | N | Median, µs | p99, µs |
|---|---:|---:|---:|
| Complete daemon read point, per tick | 472 | 33.92 | 1,333.50 |
| Complete read point, ticks doing native reads | 79 | 519.04 | 12,446.08 |
| Native sampler sources, per read | 79 | 481.37 | 12,407.58 |
| Same-run legacy fields alone | 472 | 61.19 | 106.71 |
| Bounded minute store transaction, steady state | 200 | 1,871.29 | 644,686.38 |
| Review's old full reader, every 50 ms tick | — | 330.00 | 3,000.00 |
| Review's legacy fields alone | — | 5.70 | not reported |

The full read-point maximum was 41.371 ms.
The earlier sampler-only tick run measured 6.33 µs median / 2.384 ms p99; it omitted
policy checks and minute writes and is not substituted for the full-path result.
The same-run legacy median also differed from the review's 5.7 µs, so the table
does not attribute every difference to code. Startup priming adds one intentional
second only for a policy naming new signals, outside the steady-state table.

**Store cost/day:** at most **1,440 sample rows plus 1,440 audit rows/day**, retaining
at most **2,880 rows** across those two event kinds. The measured payload was
392 bytes/sample; full retained SQLite growth was
**917,504 bytes (0.918 MB)**. Steady-state WAL growth over
200 transactions scales to approximately **52.1 MB/day of WAL traffic**, plus
checkpoint I/O. Autocheckpoint was disabled in this owned probe only to count WAL
bytes; checkpoint cost was not measured. Retained logical storage stays bounded.

### Seven invariants and their tests

| Invariant | Evidence |
|---|---|
| 1. Legacy equivalence, no new native reads | Frozen `tests/machine_guard_reference.py` from 473fe2941; generated legacy policies/readings including stray new fields; real-reader native-call traps under null/load/memory-only policy. Decisions and hold records match. |
| 2. Unreadable never holds or releases a latch | Generated bad readings plus timestamped busy/missing/recovery sequences; sampler preserves baseline/EWMA for lost and zero-tick reads. |
| 3. Monotone | Paired generated timestamped histories: lower idle and slower disk never free a class held on the healthier history. |
| 4. Class ordering | Stateful generated histories under the proposal and ordered disk thresholds: held session implies held background; memory conditions also participate. |
| 5. Never held | Generated sequences check attended, priority and unnamed classes, including an unvalidated policy explicitly naming the exempt classes. |
| 6. Release takes time | Generated sub-second pass sequences and timestamped recovery: no early release after a busy sample; ten sampled clear seconds and the stated margin are required. |
| 7. First pass | `state_daemon`, real sampler at the old reader entry point, 18 CPUs at 0% idle: startup primes; the first detached pass holds all three jobs at `cpu_idle: 0.0`. |

The empty-queue admission integration test also verifies continuous calibration
without pending work. Sampling and recovery do not depend on how many jobs wait.

### Tests-first, baseline comparison and mutations

The initial tests-first gate against the reviewed implementation produced
**30 failed / 212 passed**. Separate tests-first baseline-age and restart-minute
regressions each failed before their fixes. After the final fixture strengthening,
**19 selected regressions failed against the five `f873a3fb3` guard source files**
(machine, daemon, scheduler, policy and render) on the merged tree: **19 failures,
zero setup/collection errors**. The ordinary recording failed on its admission
assertion, and the startup fixture used the real sampler. Current files were
restored in `finally`; the final positive gate then passed **247 tests**.

Required groups: **247 + 88 + 57 = 392 distinct passed cases**. The two admission
modules named as unit files in the brief actually live under `tests/fake`; those
real paths were run. The grep-discovered admission fake is `test_reset_credit_incident.py`.
The exact frozen oracle/helper files are not pytest modules; their callers ran.

| Required file | Passed |
|---|---:|
| `tests/unit/test_machine_signals.py` | 25 |
| `tests/unit/test_scheduler_machine_signals.py` | 13 |
| `tests/unit/test_policy.py` | 203 |
| `tests/fake/test_machine_guard_daemon.py` | 6 |
| `tests/fake/test_admission_priority.py` | 33 |
| `tests/fake/test_admission_ramp.py` | 19 |
| `tests/unit/test_admission_uncapped.py` | 27 |
| `tests/unit/test_scheduler_priority_callers.py` | 9 |
| `tests/fake/test_reset_credit_incident.py` | 19 |
| `tests/fake/test_state_contract.py` | 37 |
| `tests/unit/test_test_module_names.py` | 1 |

All eight temporary mutations were applied alone, tested through a separate hold,
and restored in `finally`. Each produced the named test's failure, not a collection
or setup error. Product source was clean afterward and the final 247-test gate
passed. The first three build-brief mutations were all rerun here.

| Mutation | Named failing test | Failed cases |
|---|---|---:|
| Drop EWMA | `test_machine_signals.py::test_cpu_ticks_are_interval_deltas_and_ewma_uses_sixty_seconds` | 1 |
| Drop hysteresis | `test_scheduler_machine_signals.py::test_multiple_jobs_and_reused_snapshot_cannot_advance_recovery` | 1 |
| Hold on unreadable | `test_scheduler_machine_signals.py::test_unreadable_never_holds_or_releases_latch` | 1 |
| Cap disk at 1.0 | `test_machine_signals.py::test_disk_measures_service_ms_on_state_device_without_cap` | 1 |
| Release by pass count | `test_scheduler_machine_signals.py::test_release_requires_ten_seconds_not_two_passes` | 1 |
| Remove priming | `test_machine_guard_daemon.py::test_first_detached_pass_sees_primed_eighteen_busy_cpus` | 1 |
| Reset EWMA on lost read | `test_machine_signals.py::test_lost_read_keeps_baseline_and_ewma` | 1 |
| Catch only OSError | `test_machine_signals.py::test_review_argumenterror_at_native_call_is_fail_open` | 2 |

### Whole suites

Every pytest and measurement invocation used the hub suite-hold wrapper, one
pytest process at a time, at most four xdist workers, GIL CPython 3.14.7, the same
owned venv/cache, and bounded CI Hypothesis settings. No deep profile ran.
Environment creation and the scratch-only xdist installation used the separate
setup wrapper. No dependency/lock file changed. A foreground active-time guard
would interrupt at 1,000 s while waiting through stopped owned processes; it never
fired. All 25 whole-suite holds completed, without partial files. Lock waits count
against the job wall budget; suite runtimes below exclude queueing before pytest.

| Directory | Files complete | Passed | Failed | Skipped | Xfailed | Unrun/partial |
|---|---:|---:|---:|---:|---:|---:|
| `tests/unit` | 330 | 13,345 | 7 | 21 | 0 | 0 / 0 |
| `tests/fake` | 107 | 1,896 | 4 | 77 | 2 | 0 / 0 |

The full run has **11 failures** and is not presented as green:

- Four `test_daemon_stacks` cases stop at unavailable macOS boot identity before
  the new guard initializes.
- `test_daemon_qos_tools` cannot execute `/bin/ps`; `test_descriptors` cannot read
  the kernel descriptor ceiling. These are sandbox process/kernel access failures.
- Three unchanged importer cases disagree about lost/running state and legacy
  ownership. These remain failures; no importer fix or baseline waiver is claimed.
- `test_guard_trust` timed out before its stub wrote `report.json`;
  `test_timers_probe` took 0.589 s against a 0.4 s bound. **Both passed on an isolated
  two-case retry (2 passed in 2.48 s)**. The original whole-suite failures are still
  counted. No timing bounds were relaxed.

`tests/unit/test_prune_restore.py`: **4 passed**; the separate D-PRR1 backtick-path
flake did not occur and no prune/restore code was changed. Frontend/e2e suites,
other Python versions and remote CI were outside this local run. The controlled
saturation measurement is the requested measurement left unrun, due to its gate.

<details><summary>Whole-suite holds and runtimes</summary>

| Hold | Files | Passed / failed / skipped / xfailed | Pytest seconds |
|---|---:|---:|---:|
| `whole-fake-00` | 18 | 428 / 0 / 0 / 2 | 510.62 |
| `whole-fake-01` | 18 | 370 / 4 / 50 / 0 | 219.90 |
| `whole-fake-02` | 18 | 331 / 0 / 15 / 0 | 67.74 |
| `whole-fake-03` | 18 | 343 / 0 / 2 / 0 | 173.29 |
| `whole-fake-04` | 18 | 243 / 0 / 9 / 0 | 79.04 |
| `whole-fake-05` | 17 | 181 / 0 / 1 / 0 | 133.21 |
| `whole-unit-00` | 18 | 343 / 0 / 0 / 0 | 331.55 |
| `whole-unit-01` | 18 | 1014 / 0 / 0 / 0 | 116.61 |
| `whole-unit-02` | 18 | 2266 / 0 / 0 / 0 | 187.00 |
| `whole-unit-03` | 18 | 520 / 0 / 0 / 0 | 135.09 |
| `whole-unit-04` | 18 | 401 / 2 / 5 / 0 | 139.37 |
| `whole-unit-05` | 18 | 895 / 0 / 3 / 0 | 94.01 |
| `whole-unit-06` | 18 | 763 / 1 / 2 / 0 | 327.05 |
| `whole-unit-07` | 18 | 401 / 3 / 0 / 0 | 149.16 |
| `whole-unit-08` | 18 | 997 / 0 / 6 / 0 | 408.00 |
| `whole-unit-09` | 18 | 591 / 0 / 0 / 0 | 71.89 |
| `whole-unit-10` | 18 | 800 / 0 / 0 / 0 | 65.02 |
| `whole-unit-11` | 18 | 679 / 0 / 0 / 0 | 112.16 |
| `whole-unit-12` | 18 | 504 / 0 / 4 / 0 | 570.20 |
| `whole-unit-13` | 18 | 233 / 0 / 0 / 0 | 379.40 |
| `whole-unit-14` | 18 | 893 / 0 / 0 / 0 | 878.23 |
| `whole-unit-15` | 18 | 639 / 0 / 0 / 0 | 154.45 |
| `whole-unit-16` | 18 | 805 / 0 / 1 / 0 | 839.28 |
| `whole-unit-17` | 18 | 402 / 1 / 0 / 0 | 147.99 |
| `whole-unit-18` | 6 | 199 / 0 / 0 / 0 | 529.90 |

</details>

<details><summary>All 437 per-file counts</summary>

P/F/S/X = passed/failed/skipped/xfailed. No collection or setup errors.

| File | P | F | S | X |
|---|---:|---:|---:|---:|
| `tests/fake/test_admission_disk.py` | 27 | 0 | 0 | 0 |
| `tests/fake/test_admission_held_waiters.py` | 1 | 0 | 0 | 0 |
| `tests/fake/test_admission_latency.py` | 14 | 0 | 0 | 0 |
| `tests/fake/test_admission_lease_waiters.py` | 16 | 0 | 0 | 0 |
| `tests/fake/test_admission_liveness.py` | 32 | 0 | 0 | 0 |
| `tests/fake/test_admission_priority.py` | 33 | 0 | 0 | 0 |
| `tests/fake/test_admission_properties.py` | 27 | 0 | 0 | 1 |
| `tests/fake/test_admission_ramp.py` | 19 | 0 | 0 | 0 |
| `tests/fake/test_admission_ramp_retention.py` | 11 | 0 | 0 | 0 |
| `tests/fake/test_admission_route_isolation.py` | 93 | 0 | 0 | 0 |
| `tests/fake/test_admission_stateful.py` | 16 | 0 | 0 | 0 |
| `tests/fake/test_admission_visibility.py` | 43 | 0 | 0 | 0 |
| `tests/fake/test_batch_label.py` | 9 | 0 | 0 | 0 |
| `tests/fake/test_canonical_identity.py` | 24 | 0 | 0 | 0 |
| `tests/fake/test_canonical_identity_fix2.py` | 21 | 0 | 0 | 0 |
| `tests/fake/test_canonical_identity_fix3.py` | 17 | 0 | 0 | 1 |
| `tests/fake/test_canonical_identity_fix4.py` | 11 | 0 | 0 | 0 |
| `tests/fake/test_canonical_identity_fix5.py` | 14 | 0 | 0 | 0 |
| `tests/fake/test_capacity_steer_daemon.py` | 54 | 0 | 0 | 0 |
| `tests/fake/test_capacity_wait_backoff.py` | 30 | 0 | 0 | 0 |
| `tests/fake/test_census_escapers_daemon.py` | 0 | 0 | 9 | 0 |
| `tests/fake/test_census_recycled_pid_daemon.py` | 29 | 0 | 0 | 0 |
| `tests/fake/test_codex_ca_bundle_fake.py` | 0 | 0 | 6 | 0 |
| `tests/fake/test_codex_turn_completed_fake.py` | 109 | 0 | 0 | 0 |
| `tests/fake/test_conversation_pools.py` | 2 | 0 | 0 | 0 |
| `tests/fake/test_conversation_wakes_fake.py` | 4 | 0 | 0 | 0 |
| `tests/fake/test_crash_matrix.py` | 0 | 0 | 9 | 0 |
| `tests/fake/test_daemon_contract.py` | 0 | 0 | 21 | 0 |
| `tests/fake/test_daemon_descriptors.py` | 12 | 0 | 1 | 0 |
| `tests/fake/test_daemon_launch.py` | 9 | 0 | 0 | 0 |
| `tests/fake/test_daemon_stacks.py` | 1 | 4 | 1 | 0 |
| `tests/fake/test_descriptor_exhaustion.py` | 0 | 0 | 2 | 0 |
| `tests/fake/test_detached_cwd_pins.py` | 18 | 0 | 0 | 0 |
| `tests/fake/test_detached_retention_fence.py` | 89 | 0 | 0 | 0 |
| `tests/fake/test_enrollment_guardian.py` | 0 | 0 | 1 | 0 |
| `tests/fake/test_fake_provider.py` | 13 | 0 | 0 | 0 |
| `tests/fake/test_finalization_replay.py` | 10 | 0 | 0 | 0 |
| `tests/fake/test_folder_spelling.py` | 7 | 0 | 0 | 0 |
| `tests/fake/test_frozen_stream_nonregular.py` | 140 | 0 | 0 | 0 |
| `tests/fake/test_gate_end_to_end.py` | 10 | 0 | 1 | 0 |
| `tests/fake/test_identity_rollback.py` | 5 | 0 | 0 | 0 |
| `tests/fake/test_idle_cost.py` | 0 | 0 | 2 | 0 |
| `tests/fake/test_interactive_claude_titles.py` | 6 | 0 | 0 | 0 |
| `tests/fake/test_interactive_steer.py` | 13 | 0 | 0 | 0 |
| `tests/fake/test_lane_fault.py` | 20 | 0 | 0 | 0 |
| `tests/fake/test_lane_pilot.py` | 24 | 0 | 0 | 0 |
| `tests/fake/test_lanes_enroll.py` | 15 | 0 | 0 | 0 |
| `tests/fake/test_lanes_reenroll.py` | 19 | 0 | 0 | 0 |
| `tests/fake/test_lanes_reenroll_keychain.py` | 1 | 0 | 0 | 0 |
| `tests/fake/test_launch_env_daemon.py` | 49 | 0 | 0 | 0 |
| `tests/fake/test_launch_env_providers.py` | 6 | 0 | 0 | 0 |
| `tests/fake/test_launch_gates_above_stdio.py` | 0 | 0 | 9 | 0 |
| `tests/fake/test_lost_acknowledgement.py` | 0 | 0 | 3 | 0 |
| `tests/fake/test_machine_guard_daemon.py` | 6 | 0 | 0 | 0 |
| `tests/fake/test_main_refusal_write_dir.py` | 5 | 0 | 0 | 0 |
| `tests/fake/test_mcp_job_contract.py` | 12 | 0 | 0 | 0 |
| `tests/fake/test_native_maintenance_startup.py` | 41 | 0 | 0 | 0 |
| `tests/fake/test_notice_delivery.py` | 15 | 0 | 0 | 0 |
| `tests/fake/test_notice_header.py` | 23 | 0 | 0 | 0 |
| `tests/fake/test_picker_native.py` | 3 | 0 | 0 | 0 |
| `tests/fake/test_policy_sandbox.py` | 25 | 0 | 0 | 0 |
| `tests/fake/test_probe_cancel.py` | 24 | 0 | 0 | 0 |
| `tests/fake/test_probe_recovery.py` | 9 | 0 | 0 | 0 |
| `tests/fake/test_probe_turn.py` | 43 | 0 | 0 | 0 |
| `tests/fake/test_prompt_seam.py` | 8 | 0 | 0 | 0 |
| `tests/fake/test_provider_active_cap_fake.py` | 60 | 0 | 0 | 0 |
| `tests/fake/test_provider_cap_review_evidence.py` | 5 | 0 | 0 | 0 |
| `tests/fake/test_provider_qos.py` | 0 | 0 | 2 | 0 |
| `tests/fake/test_record_secrecy.py` | 44 | 0 | 0 | 0 |
| `tests/fake/test_request_routing.py` | 3 | 0 | 0 | 0 |
| `tests/fake/test_reserve_authorization.py` | 12 | 0 | 0 | 0 |
| `tests/fake/test_reserve_authorization_validation.py` | 11 | 0 | 0 | 0 |
| `tests/fake/test_reset_credit_incident.py` | 19 | 0 | 0 | 0 |
| `tests/fake/test_resume_cli.py` | 0 | 0 | 1 | 0 |
| `tests/fake/test_resume_contract.py` | 23 | 0 | 0 | 0 |
| `tests/fake/test_retention_dormant.py` | 7 | 0 | 0 | 0 |
| `tests/fake/test_review130_ab.py` | 12 | 0 | 0 | 0 |
| `tests/fake/test_review_r185_adversarial.py` | 1 | 0 | 0 | 0 |
| `tests/fake/test_route_view.py` | 8 | 0 | 0 | 0 |
| `tests/fake/test_routing_end_to_end.py` | 14 | 0 | 2 | 0 |
| `tests/fake/test_routing_tier_preferences.py` | 5 | 0 | 0 | 0 |
| `tests/fake/test_salvage_adversarial.py` | 8 | 0 | 0 | 0 |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 | 0 | 0 |
| `tests/fake/test_sessions_end_to_end.py` | 27 | 0 | 6 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 |
| `tests/fake/test_state_fixture_processes.py` | 4 | 0 | 0 | 0 |
| `tests/fake/test_status_payload.py` | 2 | 0 | 0 | 0 |
| `tests/fake/test_suggestions_fake.py` | 1 | 0 | 0 | 0 |
| `tests/fake/test_tier_hold_by_demand.py` | 27 | 0 | 0 | 0 |
| `tests/fake/test_timers_busy_admission.py` | 1 | 0 | 0 | 0 |
| `tests/fake/test_timers_end_to_end.py` | 6 | 0 | 1 | 0 |
| `tests/fake/test_turn_listing.py` | 9 | 0 | 0 | 0 |
| `tests/fake/test_turn_retention_cwd.py` | 6 | 0 | 0 | 0 |
| `tests/fake/test_turn_retention_cwd_properties.py` | 1 | 0 | 0 | 0 |
| `tests/fake/test_turn_retention_job_directory.py` | 2 | 0 | 0 | 0 |
| `tests/fake/test_turn_retention_job_properties.py` | 1 | 0 | 0 | 0 |
| `tests/fake/test_turn_retention_quarantine.py` | 10 | 0 | 0 | 0 |
| `tests/fake/test_turn_trees.py` | 18 | 0 | 0 | 0 |
| `tests/fake/test_turn_wait_reasons.py` | 7 | 0 | 0 | 0 |
| `tests/fake/test_unadmittable_pin_admission.py` | 24 | 0 | 0 | 0 |
| `tests/fake/test_unadmittable_pin_rereview.py` | 6 | 0 | 0 | 0 |
| `tests/fake/test_unadmittable_pin_review.py` | 7 | 0 | 0 | 0 |
| `tests/fake/test_workspace_contract.py` | 8 | 0 | 0 | 0 |
| `tests/fake/test_workspace_enospc.py` | 1 | 0 | 0 | 0 |
| `tests/fake/test_workspace_transient.py` | 24 | 0 | 0 | 0 |
| `tests/fake/test_writable_fanout.py` | 28 | 0 | 0 | 0 |
| `tests/fake/test_writer_retention_fence.py` | 23 | 0 | 0 | 0 |
| `tests/unit/test_admission_uncapped.py` | 27 | 0 | 0 | 0 |
| `tests/unit/test_agent_secret_credentials.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_app_cutover_daemon.py` | 13 | 0 | 0 | 0 |
| `tests/unit/test_approval_commit.py` | 6 | 0 | 0 | 0 |
| `tests/unit/test_approval_resolution.py` | 33 | 0 | 0 | 0 |
| `tests/unit/test_boot_identity.py` | 10 | 0 | 0 | 0 |
| `tests/unit/test_capacity_desktop.py` | 20 | 0 | 0 | 0 |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 0 | 0 | 0 |
| `tests/unit/test_capacity_steer_route.py` | 22 | 0 | 0 | 0 |
| `tests/unit/test_capacity_view.py` | 20 | 0 | 0 | 0 |
| `tests/unit/test_census_counted.py` | 5 | 0 | 0 | 0 |
| `tests/unit/test_census_identity_uncertainty.py` | 8 | 0 | 0 | 0 |
| `tests/unit/test_census_procargs.py` | 52 | 0 | 0 | 0 |
| `tests/unit/test_census_recycled_pid.py` | 25 | 0 | 0 | 0 |
| `tests/unit/test_census_responsibility_failures.py` | 20 | 0 | 0 | 0 |
| `tests/unit/test_census_responsibility_identity.py` | 17 | 0 | 0 | 0 |
| `tests/unit/test_census_responsibility_races.py` | 34 | 0 | 0 | 0 |
| `tests/unit/test_census_survivors_review.py` | 14 | 0 | 0 | 0 |
| `tests/unit/test_ci_retry_known_flakes.py` | 72 | 0 | 0 | 0 |
| `tests/unit/test_ci_shard_plugin.py` | 48 | 0 | 0 | 0 |
| `tests/unit/test_claude_adapter.py` | 170 | 0 | 0 | 0 |
| `tests/unit/test_claude_attest.py` | 19 | 0 | 0 | 0 |
| `tests/unit/test_claude_cards.py` | 96 | 0 | 0 | 0 |
| `tests/unit/test_claude_cards_wiring.py` | 15 | 0 | 0 | 0 |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 8 | 0 | 0 | 0 |
| `tests/unit/test_claude_identity.py` | 30 | 0 | 0 | 0 |
| `tests/unit/test_claude_large_stream.py` | 30 | 0 | 0 | 0 |
| `tests/unit/test_claude_mcp.py` | 32 | 0 | 0 | 0 |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 0 | 0 | 0 |
| `tests/unit/test_claude_stream.py` | 60 | 0 | 0 | 0 |
| `tests/unit/test_claude_synthetic_error.py` | 63 | 0 | 0 | 0 |
| `tests/unit/test_claude_turn.py` | 61 | 0 | 0 | 0 |
| `tests/unit/test_claude_usage.py` | 28 | 0 | 0 | 0 |
| `tests/unit/test_claude_v1_parity.py` | 6 | 0 | 0 | 0 |
| `tests/unit/test_cli.py` | 247 | 0 | 0 | 0 |
| `tests/unit/test_cli_batch.py` | 18 | 0 | 0 | 0 |
| `tests/unit/test_cli_turns.py` | 23 | 0 | 0 | 0 |
| `tests/unit/test_client_availability.py` | 22 | 0 | 0 | 0 |
| `tests/unit/test_codex_adapter.py` | 172 | 0 | 0 | 0 |
| `tests/unit/test_codex_ca_bundle.py` | 46 | 0 | 0 | 0 |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 0 | 0 | 0 |
| `tests/unit/test_codex_probe.py` | 55 | 0 | 0 | 0 |
| `tests/unit/test_codex_shim.py` | 31 | 0 | 0 | 0 |
| `tests/unit/test_codex_turn.py` | 58 | 0 | 0 | 0 |
| `tests/unit/test_codex_turn_completed.py` | 41 | 0 | 0 | 0 |
| `tests/unit/test_compat.py` | 1615 | 0 | 0 | 0 |
| `tests/unit/test_continuation_r2.py` | 11 | 0 | 0 | 0 |
| `tests/unit/test_conversation_catalog.py` | 15 | 0 | 0 | 0 |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 21 | 0 | 0 | 0 |
| `tests/unit/test_conversation_connections.py` | 9 | 0 | 0 | 0 |
| `tests/unit/test_conversation_diff.py` | 39 | 0 | 0 | 0 |
| `tests/unit/test_conversation_fifo_reads.py` | 26 | 0 | 0 | 0 |
| `tests/unit/test_conversation_handoff.py` | 48 | 0 | 0 | 0 |
| `tests/unit/test_conversation_handoff_briefs.py` | 28 | 0 | 0 | 0 |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 9 | 0 | 0 | 0 |
| `tests/unit/test_conversation_history.py` | 22 | 0 | 0 | 0 |
| `tests/unit/test_conversation_peers_attachments.py` | 44 | 0 | 0 | 0 |
| `tests/unit/test_conversation_reconcile.py` | 36 | 0 | 0 | 0 |
| `tests/unit/test_conversation_redact.py` | 9 | 0 | 0 | 0 |
| `tests/unit/test_conversation_service.py` | 98 | 0 | 0 | 0 |
| `tests/unit/test_conversation_state_reads.py` | 18 | 0 | 0 | 0 |
| `tests/unit/test_conversation_status.py` | 9 | 0 | 0 | 0 |
| `tests/unit/test_conversation_steer_service.py` | 45 | 0 | 0 | 0 |
| `tests/unit/test_conversation_store.py` | 32 | 0 | 0 | 0 |
| `tests/unit/test_conversation_stream_assertion.py` | 13 | 0 | 0 | 0 |
| `tests/unit/test_conversation_titles.py` | 89 | 0 | 0 | 0 |
| `tests/unit/test_conversation_waits.py` | 9 | 0 | 0 | 0 |
| `tests/unit/test_conversation_wakes.py` | 22 | 0 | 0 | 0 |
| `tests/unit/test_cpu_scaling.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_daemon_accept.py` | 5 | 0 | 0 | 0 |
| `tests/unit/test_daemon_connections.py` | 31 | 0 | 0 | 0 |
| `tests/unit/test_daemon_fifo_reads.py` | 27 | 0 | 0 | 0 |
| `tests/unit/test_daemon_init_release.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 0 | 0 | 0 |
| `tests/unit/test_daemon_inspection_properties.py` | 4 | 0 | 0 | 0 |
| `tests/unit/test_daemon_integration.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_daemon_log_scrubbing.py` | 86 | 0 | 0 | 0 |
| `tests/unit/test_daemon_qos_tools.py` | 3 | 1 | 0 | 0 |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 | 0 | 0 |
| `tests/unit/test_daemon_settle.py` | 61 | 0 | 0 | 0 |
| `tests/unit/test_daemon_verbs.py` | 15 | 0 | 5 | 0 |
| `tests/unit/test_decision_horizon.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 0 | 0 | 0 |
| `tests/unit/test_descriptors.py` | 15 | 1 | 0 | 0 |
| `tests/unit/test_descriptors_desktop.py` | 8 | 0 | 0 | 0 |
| `tests/unit/test_desktop_account_cache.py` | 3 | 0 | 0 | 0 |
| `tests/unit/test_desktop_in_use.py` | 13 | 0 | 0 | 0 |
| `tests/unit/test_desktop_ledger.py` | 4 | 0 | 0 | 0 |
| `tests/unit/test_disk_admission.py` | 27 | 0 | 0 | 0 |
| `tests/unit/test_disk_floor.py` | 60 | 0 | 0 | 0 |
| `tests/unit/test_doctor.py` | 44 | 0 | 0 | 0 |
| `tests/unit/test_doctor_ca_bundle.py` | 16 | 0 | 0 | 0 |
| `tests/unit/test_doctor_identity.py` | 13 | 0 | 0 | 0 |
| `tests/unit/test_empty_provider_results.py` | 334 | 0 | 0 | 0 |
| `tests/unit/test_enrollment_runner.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_failed_keep.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_fake_codex_http.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_filesystem_identity_agreement.py` | 37 | 0 | 3 | 0 |
| `tests/unit/test_folders.py` | 23 | 0 | 0 | 0 |
| `tests/unit/test_forget_paced.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_fresh_until.py` | 1 | 0 | 0 | 0 |
| `tests/unit/test_frozen_provider_streams.py` | 217 | 0 | 0 | 0 |
| `tests/unit/test_gate_admission.py` | 24 | 0 | 0 | 0 |
| `tests/unit/test_gate_merge.py` | 59 | 0 | 0 | 0 |
| `tests/unit/test_gate_replay.py` | 23 | 0 | 0 | 0 |
| `tests/unit/test_gate_revision.py` | 16 | 0 | 0 | 0 |
| `tests/unit/test_gate_service.py` | 25 | 0 | 0 | 0 |
| `tests/unit/test_gate_verdict.py` | 45 | 0 | 0 | 0 |
| `tests/unit/test_guard_overlay.py` | 22 | 0 | 0 | 0 |
| `tests/unit/test_guard_trust.py` | 97 | 1 | 0 | 0 |
| `tests/unit/test_guardian.py` | 12 | 0 | 0 | 0 |
| `tests/unit/test_guardian_qos.py` | 14 | 0 | 0 | 0 |
| `tests/unit/test_handler_refusals.py` | 317 | 0 | 0 | 0 |
| `tests/unit/test_held_waiter_holdback.py` | 6 | 0 | 0 | 0 |
| `tests/unit/test_hidden_environment.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_hook_cost.py` | 5 | 0 | 0 | 0 |
| `tests/unit/test_hooks.py` | 72 | 0 | 1 | 0 |
| `tests/unit/test_hooks_native_guard.py` | 18 | 0 | 0 | 0 |
| `tests/unit/test_host_push.py` | 118 | 0 | 0 | 0 |
| `tests/unit/test_host_push_admission.py` | 15 | 0 | 0 | 0 |
| `tests/unit/test_host_push_build.py` | 10 | 0 | 1 | 0 |
| `tests/unit/test_host_push_build_budget.py` | 15 | 0 | 0 | 0 |
| `tests/unit/test_host_push_cli.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_host_push_config_cost.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_host_push_copy.py` | 11 | 0 | 0 | 0 |
| `tests/unit/test_host_push_export.py` | 10 | 0 | 0 | 0 |
| `tests/unit/test_host_push_intake.py` | 13 | 0 | 0 | 0 |
| `tests/unit/test_host_push_metadata.py` | 33 | 0 | 0 | 0 |
| `tests/unit/test_host_push_mutations.py` | 24 | 0 | 0 | 0 |
| `tests/unit/test_host_push_names.py` | 13 | 0 | 0 | 0 |
| `tests/unit/test_host_push_quarantine_restart.py` | 8 | 0 | 0 | 0 |
| `tests/unit/test_host_push_restart.py` | 4 | 0 | 0 | 0 |
| `tests/unit/test_host_push_retention.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_host_push_storage_recovery.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_host_push_storage_retry_budget.py` | 60 | 0 | 0 | 0 |
| `tests/unit/test_host_push_store_completion_pr183.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_host_push_store_interleavings.py` | 26 | 0 | 0 | 0 |
| `tests/unit/test_host_push_trees.py` | 5 | 0 | 0 | 0 |
| `tests/unit/test_host_push_validated_top.py` | 8 | 0 | 0 | 0 |
| `tests/unit/test_importer.py` | 145 | 3 | 0 | 0 |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 | 0 | 0 |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 | 0 |
| `tests/unit/test_keychain_exec_guard_properties.py` | 3 | 0 | 0 | 0 |
| `tests/unit/test_lane_answers.py` | 27 | 0 | 0 | 0 |
| `tests/unit/test_lanes_transfer.py` | 79 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_allowlist.py` | 27 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_helpers.py` | 20 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_mutations.py` | 41 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_policy.py` | 26 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_round2.py` | 67 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_round3.py` | 69 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_round4.py` | 40 | 0 | 0 | 0 |
| `tests/unit/test_launch_env_round5.py` | 39 | 0 | 0 | 0 |
| `tests/unit/test_lease_waiter_selection.py` | 6 | 0 | 0 | 0 |
| `tests/unit/test_legacy_hold.py` | 50 | 0 | 6 | 0 |
| `tests/unit/test_limit_from_model_text.py` | 95 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard.py` | 324 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round10.py` | 60 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round10_mutations.py` | 13 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round3.py` | 22 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round4.py` | 8 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round5.py` | 11 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round6.py` | 112 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round7.py` | 50 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round7_mutations.py` | 23 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round8.py` | 100 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round8_mutations.py` | 4 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round9.py` | 89 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_round9_mutations.py` | 12 | 0 | 0 | 0 |
| `tests/unit/test_live_store_guard_state_root.py` | 22 | 0 | 0 | 0 |
| `tests/unit/test_lockwatch.py` | 20 | 0 | 0 | 0 |
| `tests/unit/test_machine_signals.py` | 25 | 0 | 0 | 0 |
| `tests/unit/test_mcp_cli_contract.py` | 6 | 0 | 0 | 0 |
| `tests/unit/test_mcp_scheduler_contract.py` | 5 | 0 | 0 | 0 |
| `tests/unit/test_mirror_flags_faults.py` | 29 | 0 | 0 | 0 |
| `tests/unit/test_mirror_flags_model.py` | 12 | 0 | 0 | 0 |
| `tests/unit/test_mirror_flags_stateful.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_native_operations.py` | 28 | 0 | 0 | 0 |
| `tests/unit/test_notice_rendering.py` | 21 | 0 | 0 | 0 |
| `tests/unit/test_notify_push.py` | 31 | 0 | 0 | 0 |
| `tests/unit/test_offline.py` | 33 | 0 | 0 | 0 |
| `tests/unit/test_operator_hold_closures.py` | 15 | 0 | 0 | 0 |
| `tests/unit/test_operator_notices.py` | 79 | 0 | 0 | 0 |
| `tests/unit/test_outcome_unknown.py` | 58 | 0 | 0 | 0 |
| `tests/unit/test_person_boundary.py` | 97 | 0 | 0 | 0 |
| `tests/unit/test_person_boundary_identity.py` | 25 | 0 | 0 | 0 |
| `tests/unit/test_picker.py` | 39 | 0 | 0 | 0 |
| `tests/unit/test_placement_ramp.py` | 26 | 0 | 0 | 0 |
| `tests/unit/test_policy.py` | 203 | 0 | 0 | 0 |
| `tests/unit/test_policy_disk.py` | 57 | 0 | 0 | 0 |
| `tests/unit/test_policy_ramp.py` | 46 | 0 | 0 | 0 |
| `tests/unit/test_policy_support.py` | 14 | 0 | 0 | 0 |
| `tests/unit/test_policy_tier_preferences.py` | 15 | 0 | 0 | 0 |
| `tests/unit/test_pr124_fixes.py` | 10 | 0 | 0 | 0 |
| `tests/unit/test_pr124_review_r2_fixes.py` | 6 | 0 | 0 | 0 |
| `tests/unit/test_pr192_review_r1.py` | 21 | 0 | 0 | 0 |
| `tests/unit/test_pr192_review_r2.py` | 9 | 0 | 0 | 0 |
| `tests/unit/test_procs.py` | 47 | 0 | 0 | 0 |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 | 0 | 0 |
| `tests/unit/test_provider_active_cap.py` | 50 | 0 | 0 | 0 |
| `tests/unit/test_prune_217.py` | 9 | 0 | 0 | 0 |
| `tests/unit/test_prune_decision_backup_verification.py` | 23 | 0 | 0 | 0 |
| `tests/unit/test_prune_decision_fingerprints.py` | 15 | 0 | 0 | 0 |
| `tests/unit/test_prune_decisions.py` | 48 | 0 | 0 | 0 |
| `tests/unit/test_prune_restore.py` | 4 | 0 | 0 | 0 |
| `tests/unit/test_quota_projection.py` | 28 | 0 | 0 | 0 |
| `tests/unit/test_rank_review_report.py` | 3 | 0 | 0 | 0 |
| `tests/unit/test_reap_liveness.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_rediscovery_git_trace.py` | 17 | 0 | 0 | 0 |
| `tests/unit/test_relay.py` | 24 | 0 | 0 | 0 |
| `tests/unit/test_render.py` | 20 | 0 | 0 | 0 |
| `tests/unit/test_render_quota_projection.py` | 23 | 0 | 0 | 0 |
| `tests/unit/test_reserve_authorization_plumbing.py` | 5 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_claim_timing.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_demand.py` | 68 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_properties.py` | 11 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_review_r1.py` | 22 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_review_r2.py` | 52 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_review_r3.py` | 23 | 0 | 0 | 0 |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | 12 | 0 | 0 | 0 |
| `tests/unit/test_resource_identity.py` | 10 | 0 | 0 | 0 |
| `tests/unit/test_retention_81_notes.py` | 38 | 0 | 0 | 0 |
| `tests/unit/test_retention_archive.py` | 109 | 0 | 0 | 0 |
| `tests/unit/test_retention_archive_properties.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_retention_case_alias.py` | 56 | 0 | 0 | 0 |
| `tests/unit/test_retention_nested_leases.py` | 27 | 0 | 0 | 0 |
| `tests/unit/test_retention_ordinary_trees.py` | 0 | 0 | 4 | 0 |
| `tests/unit/test_retention_qos.py` | 8 | 0 | 0 | 0 |
| `tests/unit/test_retention_readback.py` | 37 | 0 | 0 | 0 |
| `tests/unit/test_retention_rediscovery.py` | 17 | 0 | 0 | 0 |
| `tests/unit/test_retention_regenerable.py` | 26 | 0 | 0 | 0 |
| `tests/unit/test_retention_regenerable_properties.py` | 1 | 0 | 0 | 0 |
| `tests/unit/test_retention_salvage_properties.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_retention_shared_folders.py` | 62 | 0 | 0 | 0 |
| `tests/unit/test_retention_sweep_race.py` | 13 | 0 | 0 | 0 |
| `tests/unit/test_retention_turn_contract.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_retention_turn_workdirs.py` | 1 | 0 | 0 | 0 |
| `tests/unit/test_retention_turns.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_retention_worktrees.py` | 15 | 0 | 0 | 0 |
| `tests/unit/test_review130_pass.py` | 21 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_fixes.py` | 12 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_history.py` | 6 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_pr_wakes.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_report.py` | 1 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_requests.py` | 17 | 0 | 0 | 0 |
| `tests/unit/test_review_pr127_service.py` | 8 | 0 | 0 | 0 |
| `tests/unit/test_review_r2_repro.py` | 31 | 0 | 0 | 0 |
| `tests/unit/test_review_r3_measure.py` | 6 | 0 | 0 | 0 |
| `tests/unit/test_review_r3_repro.py` | 12 | 0 | 0 | 0 |
| `tests/unit/test_review_r4_repro.py` | 27 | 0 | 0 | 0 |
| `tests/unit/test_review_r5_repro.py` | 17 | 0 | 0 | 0 |
| `tests/unit/test_review_r6_probes.py` | 30 | 0 | 0 | 0 |
| `tests/unit/test_review_r6_restart.py` | 5 | 0 | 0 | 0 |
| `tests/unit/test_round10_refusal_context.py` | 59 | 0 | 0 | 0 |
| `tests/unit/test_route_check.py` | 24 | 0 | 0 | 0 |
| `tests/unit/test_salvage.py` | 31 | 0 | 0 | 0 |
| `tests/unit/test_salvage_optional_transient.py` | 15 | 0 | 0 | 0 |
| `tests/unit/test_salvage_unindexable.py` | 86 | 0 | 0 | 0 |
| `tests/unit/test_scheduler.py` | 97 | 0 | 0 | 0 |
| `tests/unit/test_scheduler_machine_signals.py` | 13 | 0 | 0 | 0 |
| `tests/unit/test_scheduler_priority_callers.py` | 9 | 0 | 0 | 0 |
| `tests/unit/test_scheduler_split.py` | 3 | 0 | 0 | 0 |
| `tests/unit/test_scheduler_tier_preferences.py` | 9 | 0 | 0 | 0 |
| `tests/unit/test_scheduler_turns.py` | 10 | 0 | 0 | 0 |
| `tests/unit/test_scheduler_weekly.py` | 33 | 0 | 0 | 0 |
| `tests/unit/test_scoped_stream_capacity.py` | 17 | 0 | 0 | 0 |
| `tests/unit/test_scrub_key_order.py` | 70 | 0 | 0 | 0 |
| `tests/unit/test_sessions_cli.py` | 142 | 0 | 0 | 0 |
| `tests/unit/test_sessions_handoff.py` | 117 | 0 | 0 | 0 |
| `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 0 | 0 | 0 |
| `tests/unit/test_sessions_headless_properties.py` | 5 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror.py` | 41 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | 4 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_fifo.py` | 49 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_hot_health.py` | 6 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_hot_progress.py` | 16 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | 22 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_inventory.py` | 8 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_load_gap.py` | 85 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_safety.py` | 28 | 0 | 0 | 0 |
| `tests/unit/test_sessions_mirror_write_churn.py` | 3 | 0 | 0 | 0 |
| `tests/unit/test_sessions_nudge.py` | 44 | 0 | 0 | 0 |
| `tests/unit/test_sessions_registry.py` | 24 | 0 | 0 | 0 |
| `tests/unit/test_sessions_revive.py` | 45 | 0 | 0 | 0 |
| `tests/unit/test_sessions_revive_reads.py` | 10 | 0 | 0 | 0 |
| `tests/unit/test_sessions_transcripts.py` | 71 | 0 | 0 | 0 |
| `tests/unit/test_settle_properties.py` | 129 | 0 | 0 | 0 |
| `tests/unit/test_soak_tools.py` | 49 | 0 | 0 | 0 |
| `tests/unit/test_status_json.py` | 56 | 0 | 0 | 0 |
| `tests/unit/test_steer_invariants.py` | 248 | 0 | 0 | 0 |
| `tests/unit/test_stop_alarm.py` | 1 | 0 | 1 | 0 |
| `tests/unit/test_stop_watchdog.py` | 29 | 0 | 0 | 0 |
| `tests/unit/test_stopped_daemon.py` | 72 | 0 | 0 | 0 |
| `tests/unit/test_store_contention_queries.py` | 212 | 0 | 0 | 0 |
| `tests/unit/test_store_ids.py` | 27 | 0 | 0 | 0 |
| `tests/unit/test_store_migration_2.py` | 10 | 0 | 0 | 0 |
| `tests/unit/test_store_properties.py` | 3 | 0 | 0 | 0 |
| `tests/unit/test_store_readers.py` | 26 | 0 | 0 | 0 |
| `tests/unit/test_store_snapshot_plans.py` | 4 | 0 | 0 | 0 |
| `tests/unit/test_suggest_cli.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_suggest_hint.py` | 10 | 0 | 0 | 0 |
| `tests/unit/test_suggestion_error_boundaries.py` | 22 | 0 | 0 | 0 |
| `tests/unit/test_suggestion_properties.py` | 1 | 0 | 0 | 0 |
| `tests/unit/test_suggestion_secret_properties.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_suggestion_structural_errors.py` | 40 | 0 | 0 | 0 |
| `tests/unit/test_suggestions.py` | 30 | 0 | 0 | 0 |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 30 | 0 | 0 | 0 |
| `tests/unit/test_suggestions_r3_channels.py` | 14 | 0 | 0 | 0 |
| `tests/unit/test_suggestions_review_boundaries.py` | 7 | 0 | 0 | 0 |
| `tests/unit/test_suggestions_review_multiline.py` | 6 | 0 | 0 | 0 |
| `tests/unit/test_suggestions_review_response.py` | 3 | 0 | 0 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 | 0 |
| `tests/unit/test_tick_prefilter.py` | 30 | 0 | 0 | 0 |
| `tests/unit/test_timers_alerts.py` | 17 | 0 | 0 | 0 |
| `tests/unit/test_timers_busy_read.py` | 115 | 0 | 0 | 0 |
| `tests/unit/test_timers_daemon.py` | 4 | 0 | 0 | 0 |
| `tests/unit/test_timers_keepalive.py` | 14 | 0 | 0 | 0 |
| `tests/unit/test_timers_probe.py` | 34 | 1 | 0 | 0 |
| `tests/unit/test_timers_reset_credits.py` | 47 | 0 | 0 | 0 |
| `tests/unit/test_timers_retention.py` | 20 | 0 | 0 | 0 |
| `tests/unit/test_title_review_regressions.py` | 4 | 0 | 0 | 0 |
| `tests/unit/test_turn_attachment_replay.py` | 13 | 0 | 0 | 0 |
| `tests/unit/test_turn_overlap.py` | 10 | 0 | 0 | 0 |
| `tests/unit/test_turn_replay.py` | 33 | 0 | 0 | 0 |
| `tests/unit/test_turn_runner.py` | 76 | 0 | 0 | 0 |
| `tests/unit/test_ultracode_effort.py` | 39 | 0 | 0 | 0 |
| `tests/unit/test_unadmittable_pin.py` | 12 | 0 | 0 | 0 |
| `tests/unit/test_wait_hub.py` | 44 | 0 | 0 | 0 |
| `tests/unit/test_wake_cli.py` | 2 | 0 | 0 | 0 |
| `tests/unit/test_weekly_rank_vs_base.py` | 26 | 0 | 0 | 0 |

</details>

### Proposal for d1377 (the hub puts this to Max)

Ship the CPU-idle guard and measurable state-device service term, retaining
Max's live policy until his explicit edit. The proposal is background
`cpu_idle_min: 0.15`, memory `warn`; session `cpu_idle_min: 0.05`, memory `critical`.
It names **neither `load_per_cpu` nor `disk_busy_max`**. The old 0.80/0.90 disk
fractions are withdrawn: they were fractions of an artificial cap, and the key
now means ms/op. A disk threshold waits for retained real-world calibration and
a permitted deliberate saturation comparison; this report suggests none.
CPU idle removes disk-inflated load from CPU admission. Sustained real CPU or
configured disk pressure can still delay background work indefinitely, so the
aging question remains open.

```json
{"background":{"cpu_idle_min":0.15,"memory_pressure":"warn"},
 "session":{"cpu_idle_min":0.05,"memory_pressure":"critical"}}
```

### Commits and cleanup

- `e6cd884ca`: Merge cached release/217 into MG2 guard fixes
- `99fec845c`: test: specify MG2 saturation, priming and timed recovery regressions
- `04a5ef022`: fix: sample state-device service latency and prime timed machine guard
- `e9889cea2`: test: replay measured ordinary load and specify restored baseline age
- `908062df8`: test: preserve the minute recording gate across stale snapshots and restart
- `973c3bbbd`: fix: retain calibration observation ages and durable minute recording times
- `eb47f7108`: test: exercise real startup reads, idle-queue sampling and complete read-point cost
- `2e23816ac`: test: replay recorded disk rates through counter deltas before admission

- `1b2de4a00`: docs: report round-one guard fixes, measured cost and complete suite counts
- Final cleanup verification and handoff: this commit.
All owned foreground pytest, wrapper and measurement roots completed and were
reaped. No detached process was started. The requested
`pgrep -fl 'suite_hold.sh|pytest|lockf'` could not enumerate processes in this
sandbox (`sysmond service not found`); no unrelated process was signalled.
Scratch cleanup is **verified complete**: the owned `sf-r241f1` folder is absent,
and repository bytecode caches and `.hypothesis` directories both count zero.
The specified guarded `shutil.rmtree` cleanup was used; no shared temp folder
was removed. Read-only owned test directories had traversal restored first.
Only workspace-local Git metadata is retained for the hub to import. Logs, JSON
and XML evidence, venv, caches, probes and bundles are not staged.
