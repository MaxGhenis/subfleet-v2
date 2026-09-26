# What inspecting running attempts costs, 2026-09-24

On 2026-09-20 the laptop's closed-lid thermal guard SIGSTOPped the daemon from 17:18:38 to 19:22:13 EDT. The guard pauses the operator's command-line processes at or above 20% CPU while the lid is closed on battery, and the daemon qualified every time it looked. This report covers the process-inspection share of that cost and what C-5.12 changes. The admission share is a separate change.

## What was observed, 2026-09-20 and 2026-09-21

- With four attempts running and twelve jobs waiting, the daemon (release `20260920T211551Z`, source `4eb412f8`) sat at 90 to 101% of a core: seven CPU-minutes in its first twelve minutes. A three-second `sample` put its on-CPU time in `__fork`, the malloc locking a fork takes, and pipe reads.
- Polling its direct children for eight seconds showed about 120 of them, 15 a second, mostly `/bin/ps -axEww -o pid=,command=`, with `sysctl` and `/bin/ps -axo pid=,ppid=,pgid=,stat=`.
- By the next morning the daemon was at 1.0 GB resident with 40 threads. A `ps` started from a process of that shape costs the parent 8.2 ms of CPU when CPython forks and 0.17 ms when it uses `posix_spawn`: measured with a 1 GB heap and 40 idle threads, 40 runs each. At 50 MB the fork costs 0.85 ms.

## Where it went, from the code

- `_control` offers every live attempt to the worker pool every 50 ms. For a running attempt, `_process_attempt` called `procs.liveness` on every pass: `ps -p N -o lstart=`, `ps -p N -o stat=` and a `sysctl` for the boot identity, three processes each time, with nothing rationing them.
- Every 0.5 s it also ran a full census only to record the group's members as owned: two `ps` reads (one of them the environment scan), three more processes for every live pid found, then three more to re-check the guardian.
- Each tick asked `leases` once for every job the store had ever accepted.

## What changed (C-5.12)

- A healthy running attempt's processes are inspected once per `inspect_interval_s` (1 s), from one process table that every running attempt shares. The receipt, a cancel request and the wall clock are still read on every tick.
- The table is C-5.5's snapshot with `lstart` added. It answers the C-5.3 identity question for any number of pids, so a census is two `ps` reads instead of `2 + 3N`.
- The shared table can only say "alive", and only on an exact match. Anything else goes to a fresh `liveness`, and every census that decides anything is still read fresh.
- One boot-identity read is reused for 5 s, if it is the boot session UUID. The `kern.boottime` seconds that a failed UUID read falls back to are not kept: for as long as they were, every process recorded with the UUID would compare as unknown, and the kill protocol could not signal it (found in review, 2026-09-25). A boot mismatch is read again before it counts. A table read that fails is rationed like one that works.
- `ps` and `sysctl` are started with `posix_spawn`.
- The export check is one join driven from `leases`. `~/.claude.json` is parsed only when `stat` says it changed.

## What was measured

`tools/measure_idle_cost.py` runs a real `Daemon` with the production tick on a temp state root holding 300 accepted terminal jobs, with the timers switched off. It measures three states: idle; four running attempts (real sleeping processes as guardians) with twelve queued jobs; then every lane closed with the attempts ended. It takes 20-second windows and uses `getrusage` for the daemon and for the children it reaped. It was run from a checkout of each revision on 2026-09-24, with this Mac at a load average between 40 and 55, so the small numbers are upper bounds.

| | `origin/main` (3f155e5) | this change |
|---|---:|---:|
| Idle: daemon | 7.8% | 0.5% |
| Four running, twelve waiting: daemon | 46.5% | 3.9% |
| Four running, twelve waiting: its `ps` children | 71.5% | 2.7% |
| Every lane closed, twelve waiting: daemon | 9.0% | 1.6% |

The measuring process is small (a fork from it is cheap), so the baseline column understates what a daemon at 1 GB paid.

`tests/fake/test_idle_cost.py` counts processes at the one seam every `subprocess` call goes through: four healthy attempts ticked twenty times cost one `ps` and one `sysctl`, and a guardian that dies is still found by a fresh read.

## What this does not change

- How soon an abnormal guardian death is noticed is now up to 1 s, not 50 ms. A normal end is unaffected, because it is read from `exit.json` on every tick. A new group member is recorded as owned within 1 s, not 0.5 s; one that escapes before then is quarantined, never signalled (C-5.4).
- The daemon's resident size (1.0 GB after fifteen hours on 2026-09-21) was not investigated.
