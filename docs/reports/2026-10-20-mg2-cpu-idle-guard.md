# D-MG2: CPU idle and disk guard — round-1 fix report

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

Final report/PR-body and cleanup commits follow these validation steps.
All owned foreground pytest, wrapper and measurement roots completed and were
reaped. No detached process was started. The requested
`pgrep -fl 'suite_hold.sh|pytest|lockf'` could not enumerate processes in this
sandbox (`sysmond service not found`); no unrelated process was signalled.
Scratch and repository cache cleanup: **pending final cleanup step**.
Only workspace-local Git metadata is retained for the hub to import. Logs, JSON
and XML evidence, venv, caches, probes and bundles are not staged.
