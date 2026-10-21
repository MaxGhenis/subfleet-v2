# D-MG2: CPU idle and disk machine guard

Built from release/217 at `a26d08336fc4` in the assigned workspace. Live policy,
the shipped `machine_guard: null`, and default policy bytes are unchanged.
No push, live-store access, daemon access or background process is part of this
build. Background ageing is out of scope.

Final feature gates: **52 selected passed**, **61 restored passed** (overlap),
and **3/3 named mutations caught**. The incomplete whole-suite run records
**11,621 passed, 13 failed, 96 skipped, 2 xfailed**. Its per-file matrix includes
**11 partial** and **97 unrun** modules; focused/required results overlap that
matrix and are shown separately. This is not a green whole-suite result.

## Signals and admission

`subfleet/machine.py` reads each native source once per detached admission pass,
through ctypes, including on first library load. It starts no subprocess.
Library paths are explicit; `ctypes.util.find_library` can start tools on some
platforms and is therefore avoided. The attended-turn pass reads neither source.

- CPU: `host_processor_info(PROCESSOR_CPU_LOAD_INFO)` returns each CPU's
  unsigned user/system/idle/nice tick counters. This is the per-CPU counterpart
  of aggregate `HOST_CPU_LOAD_INFO`; the API flavor is **2**, not 3. Idle is
  delta idle ticks / delta total ticks, weighted across CPUs. A 60-second
  time-constant EWMA uses `alpha = 1 - exp(-elapsed / 60)`, so irregular pass
  intervals have the intended smoothing. The first delta initializes it;
  boot-to-now utilization is never used. The Mach allocation and host send
  right are released every read. [Apple's processor definitions](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/mach/processor_info.h).
- Disk: IOKit `IOBlockStorageDriver` Statistics, `Total Time (Read)` and
  `Total Time (Write)`, supply cumulative service nanoseconds. For each device,
  sum their deltas / elapsed wall nanoseconds. Take the busiest device, cap at
  1.0. This gives `disk_busy`, in **service seconds per wall second**, [0, 1].
  It measures storage directly and is independent of CPU runnable work.
  Request service times overlap, so this bounded saturation proxy is not a
  physical utilization percentage. In particular, a device can reach 1 with
  concurrent requests before exhausting all throughput. Registry IDs detect
  changed devices; all CF values, devices and iterators are released.
  Enumeration is bounded at 64 devices.
  [Apple's storage statistics definitions](https://github.com/apple-oss-distributions/IOStorageFamily/blob/main/IOBlockStorageDriver.h).

Unreadable sources, startup, zero intervals, reset counters and changed CPU or
device sets hold nothing; a fresh baseline is needed. Missing CPU readings
reset the EWMA. A missing signal immediately clears its own hold latch and
never masks another readable condition. Unsupported platforms return None.

The daemon owns `scheduler.MachineGuard`, which advances once per detached pass
and gives all jobs the same snapshot. CPU holds below its minimum; disk holds
at or above its maximum. Each class/signal latch releases after **two consecutive
passes** at least **0.03 idle above** its CPU threshold, or **0.10 disk busy below**
its disk threshold. Release boundaries clamp to [0, 1]. An intervening marginal
reading resets the count. Guard threshold edits clear latches. Another queued job and
`why` cannot advance recovery. Hold records and `why` show readings, thresholds,
release targets and progress.

Legacy load and memory keys retain their exact decisions and record shapes,
including load rounding. Hysteresis applies only to the new opt-in signals;
adding it to legacy-only policy would violate the required differential.
Conditions combine by OR. Unnamed classes, attended turns and priority jobs
are never held, including with an unvalidated policy that names them.

## Proposed setting and measurements

This is a proposal in `policy.MACHINE_GUARD_PROPOSAL`, requiring an explicit
policy edit to take effect:

```json
{
  "background": {"cpu_idle_min": 0.15, "disk_busy_max": 0.80, "memory_pressure": "warn"},
  "session": {"cpu_idle_min": 0.05, "disk_busy_max": 0.90, "memory_pressure": "critical"}
}
```

The idle thresholds are the hub's specified proposal. Load is absent: the
10/11 shape (398/316 load, 32% idle) starts no CPU hold: with a fresh guard
and normal memory pressure it holds only by disk, or not at all. An existing
CPU latch still needs its usual two clear passes. The 10/20 saturated CPU shape holds session work.
Memory settings preserve the previous proposal.

The wrapped probe acquired slot B at 22:46:26 UTC. Twelve approximately
five-second intervals all read **0% idle** and **disk busy 1.0**; load1 ranged
292.858–313.084 on 18 logical CPUs. Both signals were readable in 12/12
intervals. The disk proposals are 20% (background) and 10% (session) below
this measured bounded saturation plateau: `1.0 * 0.80` and `1.0 * 0.90`.
Background leaves more storage headroom; a person waiting on session work
gets the higher threshold. Release is at 0.70 and 0.80 respectively.

This is provisional calibration against an observed saturated host, not a
measured quiet-to-saturated performance knee. Concurrent I/O can reach the
plateau before all device throughput is exhausted. No artificial stress was
added, and no claim of an optimal threshold or a measured quiet baseline is
made. Live policy remains the operator's choice.

| Reading | N | Best | Median | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Mach CPU tick snapshot | 100 | 18.1 µs | 19.4 µs | 119.8 µs |
| IOKit disk counters | 100 | 34.3 µs | 37.2 µs | 562.094 ms |
| Full warm machine reading | 100 | **66.7 µs** | **81.9 µs** | 349.295 ms |

The 100-repeat timings are back-to-back; some such intervals contain no new
CPU ticks, so they need not produce a usable idle delta. The cadence readings
all include readable deltas and smoothing. Cold full read: **413.7 µs**.
The 12 cadence reads cost **267.7 µs best**,
414.0 µs median, and 71.399 ms maximum. Wall-time tails include host scheduling
and contention; these are measurements, not a latency bound. The reader stores
its actual per-pass elapsed cost as `read_cost_s`. The standalone
`tools/measure_machine_guard.py` observes existing activity and measures each
source separately; its entire invocation used the required wrapper.

## Tests and status

All pytest and measurement invocations use the hub's suite-hold wrapper, one
pytest invocation at a time, GIL CPython 3.14.7, scratch uv cache/venv and bounded ci
Hypothesis settings. Batches target less than 20 minutes. The first required 13-file group exceeded
20 minutes while emitting only occasional progress. `ps` was blocked; a native
status read found its process running (BSD state 2), and the sandbox denied
SIGINT to that verified owned PID. The runner interrupt input succeeded: the partial run ended with 121 passed,
no assertion failures, exit 130, in 1403.95 s (23m 23s). It is not counted as
a completed required group.
This exception and its final outcome are recorded below; later groups use
smaller batches and collected node splits for expensive files. Scratch Hypothesis
storage is removed after each run; no `.hypothesis` is created in the tree.
Initial focused and mutation checks ran serially. Later multi-case groups use four
workers (`-n 4 --dist worksteal --max-worker-restart=0`), with scratch-only
pytest-xdist 3.8.0 / execnet 2.1.2; project dependency files are unchanged.
[Worker/distribution options](https://pytest-xdist.readthedocs.io/en/stable/distribution.html).
The foreground inner runner budgets 15 minutes of active time, checks owned
manager/worker stop states, and waits through stops. Full-suite batches visit
fakes before unit modules because admission lives in the daemon; expensive
files are split by collected nodes.

| File | Coverage |
| --- | --- |
| `tests/unit/test_machine_signals.py` | Interval deltas, weighted ticks and wrap, 60 s EWMA, topology/reset/missing data, disk bound and busiest device, first-load no subprocess, once-per-pass native reads. |
| `tests/unit/test_scheduler_machine_signals.py` | Bounded properties for idle monotonicity, unnamed/exempt classes, unreadability after a latch, release margins/two consecutive passes; independent OR conditions; policy edits; multiple jobs per snapshot; incident regressions; hold text; differential against 473fe2941. |
| `tests/unit/test_policy.py` | Finite fraction validation, booleans/types/ranges, fraction boundaries and mixing new/legacy keys. |
| `tests/fake/test_admission_priority.py` | Real admission/why holds three jobs with one sample; first recovery pass holds all, second releases; why never advances the counter; no decision while held. Legacy fixtures explicitly use old thresholds. |
| `tests/unit/test_admission_uncapped.py`, `tests/unit/test_scheduler_priority_callers.py`, `tests/fake/test_admission_ramp.py` | Existing load-era properties/fakes use explicit legacy thresholds to keep their old assertions meaningful. |
| `tests/machine_guard_reference.py` | Exact frozen machine_hold from 473fe2941, independently copied from that commit; no execution against live policy. |

Initial focused run: **47 passed, 1 failed, 177 deselected** in 187.51 s. The
failure was Hypothesis's input-generation wall-time health check under host
scheduling delay (5 draws took 30.60 s); bounded properties now suppress only `too_slow`,
as the repository's stateful ci profile does. No behavior assertion failed.
Focused positive gate after the health-check correction: **48 passed,
210 deselected** in 166.56 s. Per file: sampler **14**, scheduler signals **12**,
new policy cases **21**, new admission/why case **1**.

| Temporary mutation | Named failing test | Result |
| --- | --- | --- |
| Replace EWMA output with raw idle | `test_machine_signals.py::test_cpu_ticks_are_interval_deltas_and_ewma_uses_sixty_seconds` | 1 failed as expected, 44.32 s |
| Release after one clear pass | `test_scheduler_machine_signals.py::test_cpu_release_requires_three_point_margin_for_two_consecutive_passes` | 1 failed as expected, 270.30 s |
| Substitute busy values for unreadable signals | `test_scheduler_machine_signals.py::test_unreadable_signal_holds_nothing_and_clears_a_latch` | 1 failed as expected, 7.59 s |

Each mutation was applied alone and restored in `finally`; each failed its
specific named test, rather than merely returning a nonzero status. With all
source restored: **59 passed** in 21.76 s: sampler **14**, scheduler signals
**12**, and all admission-priority fakes **33**. The source diff was clean
before proceeding. Mutants and execution logs are not committed.

The first serial admission batch stopped under the active-time guard with
**30 passed** in 881.71 s (exit 2, incomplete). The four-worker retry stopped
at the same guard with **326 passed, 1 xfailed** in 891.21 s (exit 2,
incomplete), with no unexpected assertion failures. These counts are partial
and overlap later whole-suite coverage. The remaining state/core tests and
whole-suite batches continue below; unfinished cases remain unverified.

The combined state/core group recorded **571 passed** in 905.51 s before its
active-time guard ended it (exit 2, incomplete). No assertion failure was
reported. Completed cases include the module-name check; per-file counts and
remaining coverage are recorded in the final matrix below.

Final review found that `math.isfinite` could overflow while converting an
oversized integer before checking its fraction bounds. Four regressions were
committed first (`c1d0cf303`); all four failed with `OverflowError` in 45.90 s.
`79d36afe0` checks bounds before float conversion in the policy fraction helper
and new-signal reading helper. Valid fraction behavior is unchanged; invalid
large readings now clear their latch and invalid thresholds name their key.
The native sampler is unchanged, so the cost measurements still describe it.

On this final source: **52 passed, 210 deselected** in 59.58 s: sampler **14**,
scheduler signals **14**, new policy cases **23**, new admission/why case **1**.
All three mutations were repeated and failed the same named tests (EWMA
8.80 s, hysteresis 50.44 s, unreadability 27.98 s). After restoring source,
**61 passed** in 72.58 s: sampler **14**, scheduler signals **14**, all
admission-priority fakes **33**.

New holds stopped at the **02:00 UTC cutoff** (4 h 29 min after workspace
creation). The last active hold, `full-15-unit`, acquired at 01:58:14 UTC,
finished with **81 passed in 205.18 s**. Groups **16–23 did not start**. The
whole unit/fake run is incomplete: **318 modules completed without failures,
7 completed with failures, 11 remain partial, and 97 have no whole-suite
results**. The last matrix column identifies prior focused/required coverage
within those modules. Any unfinished cases in partial modules remain
unverified; no claim of a green whole suite is made.
The first recorded **270 passed, 1 xfailed** in 916.00 s before its guard ended
it (exit 2, incomplete). The next recorded **15 passed** in 873.88 s, also
incomplete. Batch 02 completed with **389 passed, 4 failed, 49 skipped,
1 xfailed** in 550.85 s. All four failures are in `test_daemon_stacks.py`: three
child fixtures and one daemon constructor raise `InspectionError: macOS boot
identity is unavailable`. The unchanged boot-identity code cannot obtain that
identity in this sandbox; these failures do not reach guard assertions. They
remain failures, with no baseline rerun or waiver claimed. Batch 03 completed
with **336 passed, 1 failed, 16 skipped** in 506.41 s. Its failure is
`test_gate_end_to_end.py::test_gate_fixture_child_publishes_only_its_explicit_synthetic_attestation`:
a fixture child exceeded the existing 3-second subprocess startup/completion
timeout. It does not fail a guard assertion; no baseline comparison or retry
was performed. Batch 08 completed with **1,813 passed, 5 failed, 8 skipped**
in 250.49 s. The hooks marker test and QoS per-thread test cannot execute
`/bin/ps` (`PermissionError: Operation not permitted`); two doctor daemon-lock
tests receive `unknown` instead of `pass`/`fail` because process identity is
unavailable; the native descriptor-ceiling test receives None from the kernel
ceiling reader in this restricted sysctl environment. Those platform-inspection
paths are unchanged. No baseline rerun or waiver is claimed. The matrix below
distinguishes complete, partial and unrun modules.

Batch 10 completed with **1,625 passed, 3 failed, 6 skipped** in 366.16 s.
All failures are in unchanged `test_importer.py`: `test_states_map_from_rc`
keeps the lost fixture running; `test_the_daemon_never_adopts_an_external_run`
therefore lists that extra external run;
`test_a_live_claude_process_outside_subfleet_keeps_history_out_and_fences_nothing[True]`
reports `session-held-by-legacy-owner` instead of `history`. These tests do not
call the new guard. The conservative states are consistent with unavailable
process inspection, but no baseline comparison establishes their cause; they
remain unresolved failures.

## Final whole-suite coverage matrix

Full-suite counts below are disjoint planned selections on the final source; focused and required runs overlap them and are not added to these totals. Exit 2 means a partial run stopped at its active-time budget. Exit 1 means the batch completed with failures. Skipped includes environment skips; xfails are shown separately in the run summary. A passed case count does not certify that an interrupted module completed.

| Whole-suite batch | Result | Exit |
| --- | --- | ---: |
| `full-00-fake` | 270 passed, 1 xfailed in 916.00s (0:15:16) | 2 |
| `full-01-fake` | 15 passed in 873.88s (0:14:33) | 2 |
| `full-02-fake` | 4 failed, 389 passed, 49 skipped, 1 xfailed in 550.85s (0:09:10) | 1 |
| `full-03-fake` | 1 failed, 336 passed, 16 skipped in 506.41s (0:08:26) | 1 |
| `full-04-fake` | 611 passed, 12 skipped in 355.92s (0:05:55) | 0 |
| `full-05-fake` | 150 passed in 84.47s (0:01:24) | 0 |
| `full-06-unit` | 1764 passed, 4 warnings in 354.32s (0:05:54) | 0 |
| `full-07-unit` | 2305 passed in 238.72s (0:03:58) | 0 |
| `full-08-unit` | 5 failed, 1813 passed, 8 skipped in 250.49s (0:04:10) | 1 |
| `full-09-unit` | 236 passed, 1 skipped in 221.17s (0:03:41) | 0 |
| `full-10-unit` | 3 failed, 1625 passed, 6 skipped in 366.16s (0:06:06) | 1 |
| `full-11-unit` | 1824 passed in 435.99s (0:07:15) | 0 |
| `full-12-unit` | 55 passed in 136.21s (0:02:16) | 0 |
| `full-13-unit` | 54 passed in 88.96s (0:01:28) | 0 |
| `full-14-unit` | 93 passed, 4 skipped in 197.38s (0:03:17) | 0 |
| `full-15-unit` | 81 passed in 205.18s (0:03:25) | 0 |
| `full-16-unit` | Unrun at cutoff | — |
| `full-17-unit` | Unrun at cutoff | — |
| `full-18-unit` | Unrun at cutoff | — |
| `full-19-unit` | Unrun at cutoff | — |
| `full-20-unit` | Unrun at cutoff | — |
| `full-21-unit` | Unrun at cutoff | — |
| `full-22-unit` | Unrun at cutoff | — |
| `full-23-unit` | Unrun at cutoff | — |

Recorded whole-suite case results: **11621 passed**, **2 xfailed**, **13 failed**, **96 skipped**.

Per-file status follows the whole-suite plan. “Partial” includes only completed case results from interrupted batches. “Unrun” means no whole-suite batch for that file completed any case; a separate focused/required count can still be present. The last column is the largest passed count in any one non-mutation focused/required run, not a sum or a claim of module completion. `P/F/S` means passed/failed/skipped (including expected failures in XML).

<details>
<summary>Per-file results, including partial and unrun modules</summary>

| File | Whole-suite status | Whole-suite P/F/S | Focused/required passed |
| --- | --- | ---: | ---: |
| `tests/fake/test_admission_disk.py` | Partial | 27/0/0 | 27 |
| `tests/fake/test_admission_held_waiters.py` | Partial | 1/0/0 | 1 |
| `tests/fake/test_admission_latency.py` | Partial | 13/0/0 | 2 |
| `tests/fake/test_admission_lease_waiters.py` | Partial | 16/0/0 | 6 |
| `tests/fake/test_admission_liveness.py` | Partial | 31/0/0 | 30 |
| `tests/fake/test_admission_priority.py` | Partial | 32/0/0 | 33 |
| `tests/fake/test_admission_properties.py` | Partial | 27/0/1 | 27 |
| `tests/fake/test_admission_ramp.py` | Partial | 19/0/0 | 19 |
| `tests/fake/test_admission_ramp_retention.py` | Partial | 11/0/0 | 11 |
| `tests/fake/test_admission_route_isolation.py` | Partial | 93/0/0 | 93 |
| `tests/fake/test_admission_stateful.py` | Partial | 15/0/0 | 16 |
| `tests/fake/test_admission_visibility.py` | Complete | 43/0/0 | 43 |
| `tests/fake/test_batch_label.py` | Complete | 9/0/0 | — |
| `tests/fake/test_canonical_identity.py` | Complete | 24/0/0 | — |
| `tests/fake/test_canonical_identity_fix2.py` | Complete | 21/0/0 | — |
| `tests/fake/test_canonical_identity_fix3.py` | Complete | 17/0/1 | — |
| `tests/fake/test_canonical_identity_fix4.py` | Complete | 11/0/0 | — |
| `tests/fake/test_canonical_identity_fix5.py` | Complete | 14/0/0 | — |
| `tests/fake/test_capacity_steer_daemon.py` | Complete | 54/0/0 | — |
| `tests/fake/test_capacity_wait_backoff.py` | Complete | 30/0/0 | — |
| `tests/fake/test_census_escapers_daemon.py` | Complete | 0/0/9 | — |
| `tests/fake/test_census_recycled_pid_daemon.py` | Complete | 29/0/0 | — |
| `tests/fake/test_codex_ca_bundle_fake.py` | Complete | 0/0/6 | — |
| `tests/fake/test_codex_turn_completed_fake.py` | Complete | 109/0/0 | — |
| `tests/fake/test_conversation_pools.py` | Complete | 2/0/0 | — |
| `tests/fake/test_conversation_wakes_fake.py` | Complete | 4/0/0 | — |
| `tests/fake/test_crash_matrix.py` | Complete | 0/0/9 | — |
| `tests/fake/test_daemon_contract.py` | Complete | 0/0/21 | — |
| `tests/fake/test_daemon_descriptors.py` | Complete | 12/0/1 | — |
| `tests/fake/test_daemon_launch.py` | Complete | 9/0/0 | — |
| `tests/fake/test_daemon_stacks.py` | Failed | 1/4/1 | — |
| `tests/fake/test_descriptor_exhaustion.py` | Complete | 0/0/2 | — |
| `tests/fake/test_detached_cwd_pins.py` | Complete | 18/0/0 | — |
| `tests/fake/test_detached_retention_fence.py` | Complete | 89/0/0 | — |
| `tests/fake/test_enrollment_guardian.py` | Complete | 0/0/1 | — |
| `tests/fake/test_fake_provider.py` | Complete | 13/0/0 | — |
| `tests/fake/test_finalization_replay.py` | Complete | 10/0/0 | — |
| `tests/fake/test_folder_spelling.py` | Complete | 7/0/0 | — |
| `tests/fake/test_frozen_stream_nonregular.py` | Complete | 32/0/0 | — |
| `tests/fake/test_gate_end_to_end.py` | Failed | 9/1/1 | — |
| `tests/fake/test_identity_rollback.py` | Complete | 5/0/0 | — |
| `tests/fake/test_idle_cost.py` | Complete | 0/0/2 | — |
| `tests/fake/test_interactive_claude_titles.py` | Complete | 6/0/0 | — |
| `tests/fake/test_interactive_steer.py` | Complete | 13/0/0 | — |
| `tests/fake/test_lane_fault.py` | Complete | 20/0/0 | — |
| `tests/fake/test_lane_pilot.py` | Complete | 24/0/0 | — |
| `tests/fake/test_lanes_enroll.py` | Complete | 15/0/0 | — |
| `tests/fake/test_lanes_reenroll.py` | Complete | 19/0/0 | — |
| `tests/fake/test_lanes_reenroll_keychain.py` | Complete | 1/0/0 | — |
| `tests/fake/test_launch_env_daemon.py` | Complete | 49/0/0 | — |
| `tests/fake/test_launch_env_providers.py` | Complete | 6/0/0 | — |
| `tests/fake/test_launch_gates_above_stdio.py` | Complete | 0/0/9 | — |
| `tests/fake/test_lost_acknowledgement.py` | Complete | 0/0/3 | — |
| `tests/fake/test_main_refusal_write_dir.py` | Complete | 5/0/0 | — |
| `tests/fake/test_mcp_job_contract.py` | Complete | 12/0/0 | — |
| `tests/fake/test_native_maintenance_startup.py` | Complete | 41/0/0 | — |
| `tests/fake/test_notice_delivery.py` | Complete | 15/0/0 | — |
| `tests/fake/test_notice_header.py` | Complete | 23/0/0 | — |
| `tests/fake/test_picker_native.py` | Complete | 3/0/0 | — |
| `tests/fake/test_policy_sandbox.py` | Complete | 25/0/0 | — |
| `tests/fake/test_probe_cancel.py` | Complete | 24/0/0 | — |
| `tests/fake/test_probe_recovery.py` | Complete | 9/0/0 | — |
| `tests/fake/test_probe_turn.py` | Complete | 43/0/0 | — |
| `tests/fake/test_prompt_seam.py` | Complete | 8/0/0 | — |
| `tests/fake/test_provider_active_cap_fake.py` | Complete | 60/0/0 | — |
| `tests/fake/test_provider_cap_review_evidence.py` | Complete | 5/0/0 | — |
| `tests/fake/test_provider_qos.py` | Complete | 0/0/2 | — |
| `tests/fake/test_record_secrecy.py` | Complete | 44/0/0 | — |
| `tests/fake/test_request_routing.py` | Complete | 3/0/0 | — |
| `tests/fake/test_reserve_authorization.py` | Complete | 12/0/0 | — |
| `tests/fake/test_reserve_authorization_validation.py` | Complete | 11/0/0 | — |
| `tests/fake/test_reset_credit_incident.py` | Complete | 19/0/0 | 19 |
| `tests/fake/test_resume_cli.py` | Complete | 0/0/1 | — |
| `tests/fake/test_resume_contract.py` | Complete | 23/0/0 | — |
| `tests/fake/test_retention_dormant.py` | Complete | 7/0/0 | — |
| `tests/fake/test_review130_ab.py` | Complete | 12/0/0 | — |
| `tests/fake/test_review_r185_adversarial.py` | Complete | 1/0/0 | — |
| `tests/fake/test_route_view.py` | Complete | 8/0/0 | — |
| `tests/fake/test_routing_end_to_end.py` | Complete | 14/0/2 | — |
| `tests/fake/test_routing_tier_preferences.py` | Complete | 5/0/0 | — |
| `tests/fake/test_salvage_adversarial.py` | Complete | 8/0/0 | — |
| `tests/fake/test_salvage_finalization.py` | Complete | 47/0/0 | — |
| `tests/fake/test_sessions_end_to_end.py` | Complete | 27/0/6 | — |
| `tests/fake/test_state_contract.py` | Complete | 37/0/0 | 37 |
| `tests/fake/test_state_fixture_processes.py` | Complete | 4/0/0 | — |
| `tests/fake/test_status_payload.py` | Complete | 2/0/0 | — |
| `tests/fake/test_suggestions_fake.py` | Complete | 1/0/0 | — |
| `tests/fake/test_tier_hold_by_demand.py` | Complete | 27/0/0 | — |
| `tests/fake/test_timers_busy_admission.py` | Complete | 1/0/0 | — |
| `tests/fake/test_timers_end_to_end.py` | Complete | 6/0/1 | — |
| `tests/fake/test_turn_listing.py` | Complete | 9/0/0 | — |
| `tests/fake/test_turn_retention_cwd.py` | Complete | 6/0/0 | — |
| `tests/fake/test_turn_retention_cwd_properties.py` | Complete | 1/0/0 | — |
| `tests/fake/test_turn_retention_job_directory.py` | Complete | 2/0/0 | — |
| `tests/fake/test_turn_retention_job_properties.py` | Complete | 1/0/0 | — |
| `tests/fake/test_turn_retention_quarantine.py` | Complete | 10/0/0 | — |
| `tests/fake/test_turn_trees.py` | Complete | 12/0/0 | — |
| `tests/fake/test_turn_wait_reasons.py` | Complete | 7/0/0 | — |
| `tests/fake/test_unadmittable_pin_admission.py` | Complete | 24/0/0 | — |
| `tests/fake/test_unadmittable_pin_rereview.py` | Complete | 6/0/0 | — |
| `tests/fake/test_unadmittable_pin_review.py` | Complete | 7/0/0 | — |
| `tests/fake/test_workspace_contract.py` | Complete | 8/0/0 | — |
| `tests/fake/test_workspace_enospc.py` | Complete | 1/0/0 | — |
| `tests/fake/test_workspace_transient.py` | Complete | 24/0/0 | — |
| `tests/fake/test_writable_fanout.py` | Complete | 28/0/0 | — |
| `tests/fake/test_writer_retention_fence.py` | Complete | 23/0/0 | — |
| `tests/unit/test_admission_uncapped.py` | Complete | 27/0/0 | 15 |
| `tests/unit/test_agent_secret_credentials.py` | Complete | 7/0/0 | — |
| `tests/unit/test_app_cutover_daemon.py` | Complete | 13/0/0 | — |
| `tests/unit/test_approval_commit.py` | Complete | 6/0/0 | — |
| `tests/unit/test_approval_resolution.py` | Complete | 33/0/0 | — |
| `tests/unit/test_boot_identity.py` | Complete | 10/0/0 | — |
| `tests/unit/test_capacity_desktop.py` | Complete | 20/0/0 | — |
| `tests/unit/test_capacity_steer_mutations.py` | Complete | 10/0/0 | — |
| `tests/unit/test_capacity_steer_route.py` | Complete | 22/0/0 | — |
| `tests/unit/test_capacity_view.py` | Complete | 20/0/0 | — |
| `tests/unit/test_census_counted.py` | Complete | 5/0/0 | — |
| `tests/unit/test_census_identity_uncertainty.py` | Complete | 8/0/0 | — |
| `tests/unit/test_census_procargs.py` | Complete | 52/0/0 | — |
| `tests/unit/test_census_recycled_pid.py` | Complete | 25/0/0 | — |
| `tests/unit/test_census_responsibility_failures.py` | Complete | 20/0/0 | — |
| `tests/unit/test_census_responsibility_identity.py` | Complete | 17/0/0 | — |
| `tests/unit/test_census_responsibility_races.py` | Complete | 34/0/0 | — |
| `tests/unit/test_census_survivors_review.py` | Complete | 14/0/0 | — |
| `tests/unit/test_ci_retry_known_flakes.py` | Complete | 72/0/0 | — |
| `tests/unit/test_ci_shard_plugin.py` | Complete | 48/0/0 | — |
| `tests/unit/test_claude_adapter.py` | Complete | 170/0/0 | — |
| `tests/unit/test_claude_attest.py` | Complete | 19/0/0 | — |
| `tests/unit/test_claude_cards.py` | Complete | 96/0/0 | — |
| `tests/unit/test_claude_cards_wiring.py` | Complete | 15/0/0 | — |
| `tests/unit/test_claude_conversation_mcp_scope.py` | Complete | 8/0/0 | — |
| `tests/unit/test_claude_identity.py` | Complete | 30/0/0 | — |
| `tests/unit/test_claude_large_stream.py` | Complete | 30/0/0 | — |
| `tests/unit/test_claude_mcp.py` | Complete | 32/0/0 | — |
| `tests/unit/test_claude_mcp_launch.py` | Complete | 11/0/0 | — |
| `tests/unit/test_claude_stream.py` | Complete | 60/0/0 | — |
| `tests/unit/test_claude_synthetic_error.py` | Complete | 63/0/0 | — |
| `tests/unit/test_claude_turn.py` | Complete | 61/0/0 | — |
| `tests/unit/test_claude_usage.py` | Complete | 28/0/0 | — |
| `tests/unit/test_claude_v1_parity.py` | Complete | 6/0/0 | — |
| `tests/unit/test_cli.py` | Complete | 222/0/0 | — |
| `tests/unit/test_cli_batch.py` | Complete | 18/0/0 | — |
| `tests/unit/test_cli_turns.py` | Complete | 23/0/0 | — |
| `tests/unit/test_codex_adapter.py` | Complete | 172/0/0 | — |
| `tests/unit/test_codex_ca_bundle.py` | Complete | 46/0/0 | — |
| `tests/unit/test_codex_ca_bundle_provider.py` | Complete | 6/0/0 | — |
| `tests/unit/test_codex_probe.py` | Complete | 55/0/0 | — |
| `tests/unit/test_codex_shim.py` | Complete | 31/0/0 | — |
| `tests/unit/test_codex_turn.py` | Complete | 58/0/0 | — |
| `tests/unit/test_codex_turn_completed.py` | Complete | 41/0/0 | — |
| `tests/unit/test_compat.py` | Complete | 1615/0/0 | — |
| `tests/unit/test_continuation_r2.py` | Complete | 11/0/0 | — |
| `tests/unit/test_conversation_catalog.py` | Complete | 15/0/0 | — |
| `tests/unit/test_conversation_catalog_lifecycle.py` | Complete | 21/0/0 | — |
| `tests/unit/test_conversation_connections.py` | Complete | 9/0/0 | — |
| `tests/unit/test_conversation_diff.py` | Complete | 39/0/0 | — |
| `tests/unit/test_conversation_fifo_reads.py` | Complete | 26/0/0 | — |
| `tests/unit/test_conversation_handoff.py` | Complete | 48/0/0 | — |
| `tests/unit/test_conversation_handoff_briefs.py` | Complete | 28/0/0 | — |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | Complete | 9/0/0 | — |
| `tests/unit/test_conversation_history.py` | Complete | 22/0/0 | — |
| `tests/unit/test_conversation_peers_attachments.py` | Complete | 44/0/0 | — |
| `tests/unit/test_conversation_reconcile.py` | Complete | 36/0/0 | — |
| `tests/unit/test_conversation_redact.py` | Complete | 9/0/0 | — |
| `tests/unit/test_conversation_service.py` | Complete | 98/0/0 | — |
| `tests/unit/test_conversation_state_reads.py` | Complete | 18/0/0 | — |
| `tests/unit/test_conversation_status.py` | Complete | 9/0/0 | — |
| `tests/unit/test_conversation_steer_service.py` | Complete | 45/0/0 | — |
| `tests/unit/test_conversation_store.py` | Complete | 32/0/0 | — |
| `tests/unit/test_conversation_stream_assertion.py` | Complete | 13/0/0 | — |
| `tests/unit/test_conversation_titles.py` | Complete | 89/0/0 | — |
| `tests/unit/test_conversation_waits.py` | Complete | 9/0/0 | — |
| `tests/unit/test_conversation_wakes.py` | Complete | 22/0/0 | — |
| `tests/unit/test_cpu_scaling.py` | Complete | 2/0/0 | — |
| `tests/unit/test_daemon_accept.py` | Complete | 5/0/0 | — |
| `tests/unit/test_daemon_connections.py` | Complete | 31/0/0 | — |
| `tests/unit/test_daemon_fifo_reads.py` | Complete | 27/0/0 | — |
| `tests/unit/test_daemon_init_release.py` | Complete | 2/0/0 | — |
| `tests/unit/test_daemon_inspection_load.py` | Complete | 22/0/0 | — |
| `tests/unit/test_daemon_inspection_properties.py` | Complete | 4/0/0 | — |
| `tests/unit/test_daemon_integration.py` | Complete | 7/0/0 | — |
| `tests/unit/test_daemon_log_scrubbing.py` | Complete | 86/0/0 | — |
| `tests/unit/test_daemon_qos_tools.py` | Failed | 3/1/0 | — |
| `tests/unit/test_daemon_refusals_cli.py` | Complete | 8/0/0 | — |
| `tests/unit/test_daemon_settle.py` | Complete | 43/0/0 | — |
| `tests/unit/test_daemon_verbs.py` | Complete | 15/0/5 | — |
| `tests/unit/test_decision_horizon.py` | Complete | 7/0/0 | — |
| `tests/unit/test_descriptor_review_fixes.py` | Complete | 56/0/0 | — |
| `tests/unit/test_descriptors.py` | Failed | 15/1/0 | — |
| `tests/unit/test_descriptors_desktop.py` | Complete | 8/0/0 | — |
| `tests/unit/test_desktop_account_cache.py` | Complete | 3/0/0 | — |
| `tests/unit/test_desktop_in_use.py` | Complete | 13/0/0 | — |
| `tests/unit/test_desktop_ledger.py` | Complete | 4/0/0 | — |
| `tests/unit/test_disk_admission.py` | Complete | 27/0/0 | — |
| `tests/unit/test_disk_floor.py` | Complete | 60/0/0 | — |
| `tests/unit/test_doctor.py` | Failed | 42/2/0 | — |
| `tests/unit/test_doctor_ca_bundle.py` | Complete | 16/0/0 | — |
| `tests/unit/test_doctor_identity.py` | Complete | 13/0/0 | — |
| `tests/unit/test_empty_provider_results.py` | Complete | 334/0/0 | — |
| `tests/unit/test_enrollment_runner.py` | Complete | 7/0/0 | — |
| `tests/unit/test_fake_codex_http.py` | Complete | 7/0/0 | — |
| `tests/unit/test_filesystem_identity_agreement.py` | Complete | 37/0/3 | — |
| `tests/unit/test_folders.py` | Complete | 23/0/0 | — |
| `tests/unit/test_forget_paced.py` | Complete | 2/0/0 | — |
| `tests/unit/test_fresh_until.py` | Complete | 1/0/0 | — |
| `tests/unit/test_frozen_provider_streams.py` | Complete | 48/0/0 | — |
| `tests/unit/test_gate_admission.py` | Complete | 24/0/0 | — |
| `tests/unit/test_gate_merge.py` | Complete | 59/0/0 | — |
| `tests/unit/test_gate_replay.py` | Complete | 23/0/0 | — |
| `tests/unit/test_gate_revision.py` | Complete | 16/0/0 | — |
| `tests/unit/test_gate_service.py` | Complete | 25/0/0 | — |
| `tests/unit/test_gate_verdict.py` | Complete | 45/0/0 | — |
| `tests/unit/test_guard_overlay.py` | Complete | 22/0/0 | — |
| `tests/unit/test_guard_trust.py` | Complete | 98/0/0 | — |
| `tests/unit/test_guardian.py` | Complete | 12/0/0 | — |
| `tests/unit/test_guardian_qos.py` | Complete | 14/0/0 | — |
| `tests/unit/test_handler_refusals.py` | Complete | 317/0/0 | — |
| `tests/unit/test_held_waiter_holdback.py` | Complete | 6/0/0 | — |
| `tests/unit/test_hidden_environment.py` | Complete | 7/0/0 | — |
| `tests/unit/test_hook_cost.py` | Complete | 3/0/0 | — |
| `tests/unit/test_hooks.py` | Failed | 66/1/0 | — |
| `tests/unit/test_hooks_native_guard.py` | Complete | 18/0/0 | — |
| `tests/unit/test_host_push.py` | Complete | 118/0/0 | — |
| `tests/unit/test_host_push_admission.py` | Complete | 15/0/0 | — |
| `tests/unit/test_host_push_build.py` | Complete | 10/0/1 | — |
| `tests/unit/test_host_push_build_budget.py` | Complete | 15/0/0 | — |
| `tests/unit/test_host_push_cli.py` | Complete | 7/0/0 | — |
| `tests/unit/test_host_push_config_cost.py` | Complete | 7/0/0 | — |
| `tests/unit/test_host_push_copy.py` | Complete | 11/0/0 | — |
| `tests/unit/test_host_push_export.py` | Complete | 10/0/0 | — |
| `tests/unit/test_host_push_intake.py` | Complete | 13/0/0 | — |
| `tests/unit/test_host_push_metadata.py` | Complete | 33/0/0 | — |
| `tests/unit/test_host_push_mutations.py` | Complete | 24/0/0 | — |
| `tests/unit/test_host_push_names.py` | Complete | 13/0/0 | — |
| `tests/unit/test_host_push_quarantine_restart.py` | Complete | 8/0/0 | — |
| `tests/unit/test_host_push_restart.py` | Complete | 4/0/0 | — |
| `tests/unit/test_host_push_retention.py` | Complete | 2/0/0 | — |
| `tests/unit/test_host_push_storage_recovery.py` | Complete | 2/0/0 | — |
| `tests/unit/test_host_push_storage_retry_budget.py` | Complete | 60/0/0 | — |
| `tests/unit/test_host_push_store_completion_pr183.py` | Complete | 2/0/0 | — |
| `tests/unit/test_host_push_store_interleavings.py` | Complete | 26/0/0 | — |
| `tests/unit/test_host_push_trees.py` | Complete | 5/0/0 | — |
| `tests/unit/test_host_push_validated_top.py` | Complete | 8/0/0 | — |
| `tests/unit/test_importer.py` | Failed | 145/3/0 | — |
| `tests/unit/test_inspection_pacing_properties.py` | Complete | 1/0/0 | — |
| `tests/unit/test_invariants_index.py` | Complete | 25/0/0 | — |
| `tests/unit/test_keychain_exec_guard_properties.py` | Complete | 3/0/0 | — |
| `tests/unit/test_lane_answers.py` | Complete | 27/0/0 | — |
| `tests/unit/test_lanes_transfer.py` | Complete | 79/0/0 | — |
| `tests/unit/test_launch_env_allowlist.py` | Complete | 27/0/0 | — |
| `tests/unit/test_launch_env_helpers.py` | Complete | 20/0/0 | — |
| `tests/unit/test_launch_env_mutations.py` | Complete | 41/0/0 | — |
| `tests/unit/test_launch_env_policy.py` | Complete | 26/0/0 | — |
| `tests/unit/test_launch_env_round2.py` | Complete | 67/0/0 | — |
| `tests/unit/test_launch_env_round3.py` | Complete | 67/0/0 | — |
| `tests/unit/test_launch_env_round4.py` | Complete | 40/0/0 | — |
| `tests/unit/test_launch_env_round5.py` | Complete | 39/0/0 | — |
| `tests/unit/test_lease_waiter_selection.py` | Complete | 6/0/0 | — |
| `tests/unit/test_legacy_hold.py` | Complete | 50/0/6 | — |
| `tests/unit/test_limit_from_model_text.py` | Complete | 95/0/0 | — |
| `tests/unit/test_live_store_guard.py` | Complete | 324/0/0 | — |
| `tests/unit/test_live_store_guard_round10.py` | Complete | 60/0/0 | — |
| `tests/unit/test_live_store_guard_round10_mutations.py` | Complete | 13/0/0 | — |
| `tests/unit/test_live_store_guard_round3.py` | Complete | 22/0/0 | — |
| `tests/unit/test_live_store_guard_round4.py` | Complete | 8/0/0 | — |
| `tests/unit/test_live_store_guard_round5.py` | Complete | 11/0/0 | — |
| `tests/unit/test_live_store_guard_round6.py` | Complete | 112/0/0 | — |
| `tests/unit/test_live_store_guard_round7.py` | Complete | 50/0/0 | — |
| `tests/unit/test_live_store_guard_round7_mutations.py` | Complete | 23/0/0 | — |
| `tests/unit/test_live_store_guard_round8.py` | Complete | 100/0/0 | — |
| `tests/unit/test_live_store_guard_round8_mutations.py` | Complete | 4/0/0 | — |
| `tests/unit/test_live_store_guard_round9.py` | Complete | 89/0/0 | — |
| `tests/unit/test_live_store_guard_round9_mutations.py` | Complete | 12/0/0 | — |
| `tests/unit/test_live_store_guard_state_root.py` | Complete | 22/0/0 | — |
| `tests/unit/test_lockwatch.py` | Complete | 20/0/0 | — |
| `tests/unit/test_machine_signals.py` | Complete | 14/0/0 | 14 |
| `tests/unit/test_mcp_cli_contract.py` | Complete | 6/0/0 | — |
| `tests/unit/test_mcp_scheduler_contract.py` | Complete | 5/0/0 | — |
| `tests/unit/test_mirror_flags_faults.py` | Complete | 29/0/0 | — |
| `tests/unit/test_mirror_flags_model.py` | Complete | 12/0/0 | — |
| `tests/unit/test_mirror_flags_stateful.py` | Complete | 2/0/0 | — |
| `tests/unit/test_native_operations.py` | Complete | 28/0/0 | — |
| `tests/unit/test_notice_rendering.py` | Complete | 21/0/0 | — |
| `tests/unit/test_notify_push.py` | Complete | 31/0/0 | — |
| `tests/unit/test_offline.py` | Complete | 33/0/0 | — |
| `tests/unit/test_operator_hold_closures.py` | Complete | 15/0/0 | — |
| `tests/unit/test_operator_notices.py` | Complete | 79/0/0 | — |
| `tests/unit/test_outcome_unknown.py` | Complete | 58/0/0 | — |
| `tests/unit/test_person_boundary.py` | Complete | 97/0/0 | — |
| `tests/unit/test_person_boundary_identity.py` | Complete | 25/0/0 | — |
| `tests/unit/test_picker.py` | Complete | 39/0/0 | — |
| `tests/unit/test_placement_ramp.py` | Complete | 26/0/0 | — |
| `tests/unit/test_policy.py` | Complete | 201/0/0 | 199 |
| `tests/unit/test_policy_disk.py` | Complete | 57/0/0 | 57 |
| `tests/unit/test_policy_ramp.py` | Complete | 46/0/0 | 46 |
| `tests/unit/test_policy_support.py` | Complete | 14/0/0 | 14 |
| `tests/unit/test_policy_tier_preferences.py` | Complete | 15/0/0 | 15 |
| `tests/unit/test_pr124_fixes.py` | Complete | 10/0/0 | — |
| `tests/unit/test_pr124_review_r2_fixes.py` | Complete | 6/0/0 | — |
| `tests/unit/test_pr192_review_r1.py` | Complete | 21/0/0 | — |
| `tests/unit/test_pr192_review_r2.py` | Complete | 9/0/0 | — |
| `tests/unit/test_procs.py` | Complete | 47/0/0 | — |
| `tests/unit/test_protocol_secrecy.py` | Complete | 322/0/0 | — |
| `tests/unit/test_provider_active_cap.py` | Complete | 50/0/0 | — |
| `tests/unit/test_prune_217.py` | Complete | 9/0/0 | — |
| `tests/unit/test_prune_decision_backup_verification.py` | Complete | 23/0/0 | — |
| `tests/unit/test_prune_decision_fingerprints.py` | Complete | 15/0/0 | — |
| `tests/unit/test_prune_decisions.py` | Complete | 48/0/0 | — |
| `tests/unit/test_prune_restore.py` | Complete | 4/0/0 | — |
| `tests/unit/test_quota_projection.py` | Complete | 28/0/0 | — |
| `tests/unit/test_rank_review_report.py` | Complete | 3/0/0 | — |
| `tests/unit/test_reap_liveness.py` | Complete | 7/0/0 | — |
| `tests/unit/test_rediscovery_git_trace.py` | Complete | 17/0/0 | — |
| `tests/unit/test_relay.py` | Complete | 24/0/0 | — |
| `tests/unit/test_render.py` | Complete | 20/0/0 | — |
| `tests/unit/test_render_quota_projection.py` | Complete | 23/0/0 | — |
| `tests/unit/test_reserve_authorization_plumbing.py` | Complete | 5/0/0 | — |
| `tests/unit/test_reset_credit_claim_timing.py` | Complete | 2/0/0 | — |
| `tests/unit/test_reset_credit_demand.py` | Complete | 68/0/0 | — |
| `tests/unit/test_reset_credit_properties.py` | Complete | 11/0/0 | — |
| `tests/unit/test_reset_credit_review_r1.py` | Complete | 22/0/0 | — |
| `tests/unit/test_reset_credit_review_r2.py` | Complete | 52/0/0 | — |
| `tests/unit/test_reset_credit_review_r3.py` | Complete | 23/0/0 | — |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | Complete | 12/0/0 | — |
| `tests/unit/test_resource_identity.py` | Complete | 10/0/0 | — |
| `tests/unit/test_retention_81_notes.py` | Complete | 38/0/0 | — |
| `tests/unit/test_retention_archive.py` | Complete | 109/0/0 | — |
| `tests/unit/test_retention_archive_properties.py` | Complete | 2/0/0 | — |
| `tests/unit/test_retention_case_alias.py` | Complete | 56/0/0 | — |
| `tests/unit/test_retention_nested_leases.py` | Complete | 27/0/0 | — |
| `tests/unit/test_retention_ordinary_trees.py` | Complete | 0/0/4 | — |
| `tests/unit/test_retention_qos.py` | Complete | 8/0/0 | — |
| `tests/unit/test_retention_readback.py` | Complete | 37/0/0 | — |
| `tests/unit/test_retention_rediscovery.py` | Complete | 17/0/0 | — |
| `tests/unit/test_retention_regenerable.py` | Complete | 26/0/0 | — |
| `tests/unit/test_retention_regenerable_properties.py` | Complete | 1/0/0 | — |
| `tests/unit/test_retention_salvage_properties.py` | Unrun | — | — |
| `tests/unit/test_retention_shared_folders.py` | Unrun | — | — |
| `tests/unit/test_retention_sweep_race.py` | Unrun | — | — |
| `tests/unit/test_retention_turn_contract.py` | Unrun | — | — |
| `tests/unit/test_retention_turn_workdirs.py` | Unrun | — | — |
| `tests/unit/test_retention_turns.py` | Unrun | — | — |
| `tests/unit/test_retention_worktrees.py` | Unrun | — | — |
| `tests/unit/test_review130_pass.py` | Unrun | — | — |
| `tests/unit/test_review_pr127_fixes.py` | Unrun | — | — |
| `tests/unit/test_review_pr127_history.py` | Unrun | — | — |
| `tests/unit/test_review_pr127_pr_wakes.py` | Unrun | — | — |
| `tests/unit/test_review_pr127_report.py` | Unrun | — | — |
| `tests/unit/test_review_pr127_requests.py` | Unrun | — | — |
| `tests/unit/test_review_pr127_service.py` | Unrun | — | — |
| `tests/unit/test_review_r2_repro.py` | Unrun | — | — |
| `tests/unit/test_review_r3_measure.py` | Unrun | — | — |
| `tests/unit/test_review_r3_repro.py` | Unrun | — | — |
| `tests/unit/test_review_r4_repro.py` | Unrun | — | — |
| `tests/unit/test_review_r5_repro.py` | Unrun | — | — |
| `tests/unit/test_review_r6_probes.py` | Unrun | — | — |
| `tests/unit/test_review_r6_restart.py` | Unrun | — | — |
| `tests/unit/test_round10_refusal_context.py` | Unrun | — | — |
| `tests/unit/test_route_check.py` | Unrun | — | — |
| `tests/unit/test_salvage.py` | Unrun | — | — |
| `tests/unit/test_salvage_optional_transient.py` | Unrun | — | — |
| `tests/unit/test_salvage_unindexable.py` | Unrun | — | — |
| `tests/unit/test_scheduler.py` | Unrun | — | 97 |
| `tests/unit/test_scheduler_machine_signals.py` | Unrun | — | 14 |
| `tests/unit/test_scheduler_priority_callers.py` | Unrun | — | 9 |
| `tests/unit/test_scheduler_split.py` | Unrun | — | 2 |
| `tests/unit/test_scheduler_tier_preferences.py` | Unrun | — | 9 |
| `tests/unit/test_scheduler_turns.py` | Unrun | — | 10 |
| `tests/unit/test_scheduler_weekly.py` | Unrun | — | 33 |
| `tests/unit/test_scoped_stream_capacity.py` | Unrun | — | — |
| `tests/unit/test_scrub_key_order.py` | Unrun | — | — |
| `tests/unit/test_sessions_cli.py` | Unrun | — | — |
| `tests/unit/test_sessions_handoff.py` | Unrun | — | — |
| `tests/unit/test_sessions_handoff_excerpts.py` | Unrun | — | — |
| `tests/unit/test_sessions_headless_properties.py` | Unrun | — | — |
| `tests/unit/test_sessions_mirror.py` | Unrun | — | — |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | Unrun | — | — |
| `tests/unit/test_sessions_mirror_fifo.py` | Unrun | — | — |
| `tests/unit/test_sessions_mirror_hot_health.py` | Unrun | — | — |
| `tests/unit/test_sessions_mirror_hot_progress.py` | Unrun | — | — |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | Unrun | — | — |
| `tests/unit/test_sessions_mirror_inventory.py` | Unrun | — | — |
| `tests/unit/test_sessions_mirror_load_gap.py` | Unrun | — | — |
| `tests/unit/test_sessions_mirror_safety.py` | Unrun | — | — |
| `tests/unit/test_sessions_mirror_write_churn.py` | Unrun | — | — |
| `tests/unit/test_sessions_nudge.py` | Unrun | — | — |
| `tests/unit/test_sessions_registry.py` | Unrun | — | — |
| `tests/unit/test_sessions_revive.py` | Unrun | — | — |
| `tests/unit/test_sessions_revive_reads.py` | Unrun | — | — |
| `tests/unit/test_sessions_transcripts.py` | Unrun | — | — |
| `tests/unit/test_settle_properties.py` | Unrun | — | — |
| `tests/unit/test_soak_tools.py` | Unrun | — | — |
| `tests/unit/test_status_json.py` | Unrun | — | — |
| `tests/unit/test_steer_invariants.py` | Unrun | — | — |
| `tests/unit/test_stop_alarm.py` | Unrun | — | — |
| `tests/unit/test_stop_watchdog.py` | Unrun | — | — |
| `tests/unit/test_store_contention_queries.py` | Unrun | — | — |
| `tests/unit/test_store_ids.py` | Unrun | — | — |
| `tests/unit/test_store_migration_2.py` | Unrun | — | — |
| `tests/unit/test_store_properties.py` | Unrun | — | — |
| `tests/unit/test_store_readers.py` | Unrun | — | — |
| `tests/unit/test_store_snapshot_plans.py` | Unrun | — | — |
| `tests/unit/test_suggest_cli.py` | Unrun | — | — |
| `tests/unit/test_suggest_hint.py` | Unrun | — | — |
| `tests/unit/test_suggestion_error_boundaries.py` | Unrun | — | — |
| `tests/unit/test_suggestion_properties.py` | Unrun | — | — |
| `tests/unit/test_suggestion_secret_properties.py` | Unrun | — | — |
| `tests/unit/test_suggestion_structural_errors.py` | Unrun | — | — |
| `tests/unit/test_suggestions.py` | Unrun | — | — |
| `tests/unit/test_suggestions_r1b_adversarial.py` | Unrun | — | — |
| `tests/unit/test_suggestions_r3_channels.py` | Unrun | — | — |
| `tests/unit/test_suggestions_review_boundaries.py` | Unrun | — | — |
| `tests/unit/test_suggestions_review_multiline.py` | Unrun | — | — |
| `tests/unit/test_suggestions_review_response.py` | Unrun | — | — |
| `tests/unit/test_test_module_names.py` | Unrun | — | 1 |
| `tests/unit/test_tick_prefilter.py` | Unrun | — | — |
| `tests/unit/test_timers_alerts.py` | Unrun | — | — |
| `tests/unit/test_timers_busy_read.py` | Unrun | — | — |
| `tests/unit/test_timers_daemon.py` | Unrun | — | — |
| `tests/unit/test_timers_keepalive.py` | Unrun | — | — |
| `tests/unit/test_timers_probe.py` | Unrun | — | — |
| `tests/unit/test_timers_reset_credits.py` | Unrun | — | — |
| `tests/unit/test_timers_retention.py` | Unrun | — | — |
| `tests/unit/test_title_review_regressions.py` | Unrun | — | — |
| `tests/unit/test_turn_attachment_replay.py` | Unrun | — | — |
| `tests/unit/test_turn_overlap.py` | Unrun | — | — |
| `tests/unit/test_turn_replay.py` | Unrun | — | — |
| `tests/unit/test_turn_runner.py` | Unrun | — | — |
| `tests/unit/test_ultracode_effort.py` | Unrun | — | — |
| `tests/unit/test_unadmittable_pin.py` | Unrun | — | — |
| `tests/unit/test_wait_hub.py` | Unrun | — | — |
| `tests/unit/test_wake_cli.py` | Unrun | — | — |
| `tests/unit/test_weekly_rank_vs_base.py` | Unrun | — | — |

</details>

Module statuses: **11 partial**, **318 complete**, **7 failed**, **97 unrun**.

## Cleanup

The foreground runner/wrapper invocation finished, and native checks found
both the final pytest child and the long-lived owned runner absent. Owned
scratch, the GIL 3.14 virtualenv, uv cache, Hypothesis storage, temporary oracle
extract and private runner scripts were removed. One intentionally read-only
retention fixture required restoring its owned directory permissions before
removal. No repository bytecode caches or `.hypothesis` remain. Only the small
workspace-local Git metadata is retained. No push was made.

## Remaining design question

CPU idle removes false CPU holds caused by disk-inflated load. That makes
background ageing less pressing only when measured disk pressure is also below
threshold. The high-disk 10/11 shape may remain disk-held. This does not provide
fairness under sustained real CPU or storage saturation: background jobs can
still wait indefinitely under a continuously binding threshold. The open
ageing question remains; this build adds no ageing or priority promotion.

## Commits and local metadata

The sandbox refused the shared Git index (`index.lock: Operation not permitted`).
As authorized, commits use workspace-local `.mg2-git` metadata and branch
`fix/mg2-cpu-idle-guard-217`, with an object alternate pointing at the original
repository. The original worktree Git metadata and caller checkout are unchanged.
Use `git --git-dir=.mg2-git log` here to inspect them. No history was rewritten.

- `cc16d1921`: tests first, including frozen legacy oracle and admission fake.
- `c8383c98b`: CPU/disk sampler, opt-in guard, hysteresis, validation, why text and measurement tool.
- `c1bc17796`: C-6.13/C-6.11 amendment, change log and report.
- `05db46d0a`: measured cost/calibration and the first hold exception.
- `afcc7feb9`: initial positive gates and named mutation failures.
- `c1d0cf303`: oversized-number regression cases, before the fix.
- `79d36afe0`: safe fraction bounds before float conversion.
- `da3e270d2`: coverage snapshot at the hold cutoff, plus cost and incident wording clarifications.
- Final validation/cleanup documentation: this commit.
