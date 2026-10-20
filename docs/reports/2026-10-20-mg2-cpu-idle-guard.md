# D-MG2: CPU idle and disk machine guard

Built from release/217 at `a26d08336fc4` in the assigned workspace. Live policy,
the shipped `machine_guard: null`, and default policy bytes are unchanged.
No push, live-store access, daemon access or background process is part of this
build. Background ageing is out of scope.

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
reading resets the count. Policy edits clear latches. Another queued job and
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
10/11 shape (398/316 load, 32% idle) can hold only by disk, or not at all, with
normal memory pressure. The 10/20 saturated CPU shape holds session work.
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

Cold full read: **413.7 µs**. The 12 cadence reads cost 267.7 µs best,
414.0 µs median, and 71.399 ms maximum. Wall-time tails include host scheduling
and contention; these are measurements, not a latency bound. The reader stores
its actual per-pass elapsed cost as `read_cost_s`. The standalone
`tools/measure_machine_guard.py` observes existing activity and measures each
source separately; its entire invocation used the required wrapper.

## Tests and status

All pytest and measurement invocations use the hub's suite-hold wrapper, one
pytest at a time, GIL CPython 3.14.7, scratch uv cache/venv and bounded ci
Hypothesis settings. Batches target less than 20 minutes. The first required 13-file group exceeded
20 minutes while emitting only occasional progress. `ps` was blocked; a native
status read found its process running (BSD state 2), and the sandbox denied
SIGINT to that verified owned PID. The runner interrupt input succeeded: the partial run ended with 121 passed,
no assertion failures, exit 130, in 1403.95 s (23m 23s). It is not counted as
a completed required group.
This exception and its final outcome are recorded below; later groups use
smaller batches and collected node splits for expensive files. Scratch Hypothesis
storage is removed after each run; no `.hypothesis` is created in the tree.

| File | Coverage |
| --- | --- |
| `tests/unit/test_machine_signals.py` | Interval deltas, weighted ticks and wrap, 60 s EWMA, topology/reset/missing data, disk bound and busiest device, first-load no subprocess, once-per-pass native reads. |
| `tests/unit/test_scheduler_machine_signals.py` | Bounded properties for idle monotonicity, unnamed/exempt classes, unreadability after a latch, release margins/two consecutive passes; independent OR conditions; policy edits; multiple jobs per snapshot; incident regressions; hold text; differential against 473fe2941. |
| `tests/unit/test_policy.py` | Finite fraction validation, booleans/types/ranges, fraction boundaries and mixing new/legacy keys. |
| `tests/fake/test_admission_priority.py` | Real admission/why holds three jobs with one sample; first recovery pass holds all, second releases; why never advances the counter; no decision while held. Legacy fixtures explicitly use old thresholds. |
| `tests/unit/test_admission_uncapped.py`, `tests/unit/test_scheduler_priority_callers.py`, `tests/fake/test_admission_ramp.py` | Existing load-era properties/fakes use explicit legacy thresholds to keep their old assertions meaningful. |
| `tests/machine_guard_reference.py` | Exact frozen machine_hold from 473fe2941, independently copied from that commit; no execution against live policy. |

Initial focused run: **47 passed, 1 failed, 177 deselected** in 187.51 s. The
failure was Hypothesis's input-generation wall-time health check during a host
pause (5 draws took 30.60 s); bounded properties now suppress only `too_slow`,
as the repository's stateful ci profile does. No behavior assertion failed.
Further required groups, mutation results and full-suite counts: in progress.

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
- Final validation results: pending wrapped runs.
