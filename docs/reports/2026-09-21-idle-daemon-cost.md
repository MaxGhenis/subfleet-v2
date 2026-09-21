# What a tick costs, 2026-09-21

On 2026-09-20 the laptop's closed-lid thermal guard SIGSTOPped the daemon from 17:18:38 to 19:22:13 EDT. The guard pauses the operator's command-line processes at or above 20% CPU while the lid is closed on battery, and the daemon qualified every time it looked. This report records why the daemon was that busy, what changed, and what was measured. The CLI side of the same incident (saying "stopped" instead of timing out) is C-5.11 on its own branch.

## What was observed

All of this was read from the live machine on 2026-09-20 and 2026-09-21, before any change.

- With four attempts running and twelve jobs queued or waiting, the daemon (release `20260920T211551Z`, source `4eb412f8`) sat at 90 to 101% of a core: seven CPU-minutes in its first twelve minutes. A three-second `sample` put its on-CPU time in `__fork`, the malloc locking a fork takes, and pipe reads.
- Polling its direct children for eight seconds showed about 120 of them, 15 a second: mostly `/bin/ps -axEww -o pid=,command=`, with `sysctl`, `/bin/ps -axo pid=,ppid=,pgid=,stat=` and `git -C <worktree of a waiting job> add -A`.
- It wrote 2.4 `events` rows and 1.2 `decisions` rows a second. By the next morning `decisions` held 97,816 rows, 97,623 of them with no attempt, averaging 22,247 bytes: 2.17 GB of a 2.33 GB `state.sqlite3`. `events` held 97,747 `attempt.reserved` rows, none with an attempt id, and 97,623 `decisions.insert` rows; 193 attempts had actually been reserved.
- By then the daemon was at 1.0 GB resident with 40 threads. A `ps` started from a process of that shape costs the parent 8.2 ms of CPU when CPython forks and 0.17 ms when it uses `posix_spawn` (measured with a 1 GB heap and 40 idle threads, 40 runs each).
- Idle the next morning, with no job queued or running, `ps` showed the same daemon at 8% of a core.

## Where it went, from the code

- `_control` runs every 50 ms and offers every live attempt and admission to the worker pool. For a running attempt `_process_attempt` called `procs.liveness` on every pass: `ps -p N -o lstart=`, `ps -p N -o stat=` and `sysctl -n kern.boottime`, three processes, with nothing rationing it. Every 0.5 s it also ran a full census for the sole purpose of recording the group's members as owned: two `ps` reads plus three more processes for every live pid it found, then three more to re-check the guardian.
- `_admit` prepared the workspace of every job it evaluated before asking whether any lane could take it: two git processes for a read-only job in a repository and six for a writable one (three `rev-parse`, then `read-tree`, `add -A` and `write-tree`; counted on `origin/main`), after a `git worktree add` on the first pass for a writable job that had never been admitted. A capacity wait's clock is one second, so each evaluable waiting job paid this every second.
- The same pass then opened a transaction named `attempt.reserved`, found no lane or a full fleet, stored the whole routing decision through `Store.add_decision` (a nested `decisions.insert` transaction) and moved the job to `waiting`. The outer transaction had changed rows, so it logged `attempt.reserved` as well. Nothing was reserved.
- A full fleet was discovered inside the first evaluated job's transaction, so that job paid the whole cost each second for as long as the fleet stayed full.
- Each tick also asked `leases` once for every job the store had ever accepted (318 on the morning of 2026-09-21) and parsed the desktop app's 212 KB `~/.claude.json` at the top of every admission pass.

## What changed

- C-6.10, C-6.11, C-5.10 (the first commit of this branch). A capacity wait's recheck clock backs off from 1 s to 30 s while its verdict repeats and a repeated verdict is stored once; every unplaced job has a stated reason; a worker that raises is retried with backoff. They were written on the `fix/admission-visibility` lane on 2026-09-20 and are carried here unchanged from that lane's working tree, because its session stopped before committing and the store is growing now: on 2026-09-21 one job waiting on closed Codex lanes added 190 decision rows to the live store in six minutes. One of its tests was adapted: a job no longer backs off behind a full fleet, because a full fleet no longer evaluates it, so the test backs it off behind a full lane instead.
- C-5.12. A healthy running attempt is inspected once per `inspect_interval_s` (1 s) from one process table shared by every running attempt; the receipt, a cancel request and the wall clock are still read every tick. The table is C-5.5's snapshot with `lstart` added, so it answers the C-5.3 identity question for any number of pids and a census is two `ps` reads instead of `2 + 3N`. The shared table can say "alive" and nothing else: a guardian it does not show is asked about afresh, and every census that decides anything is still read fresh. One `kern.boottime` read is reused for 5 s and a boot-id mismatch is re-read before it counts. A table read that fails is rationed like one that works. `ps` and `sysctl` are started with `posix_spawn`.
- C-6.12. Admission asks whether a job could be placed (fleet below its limit, a lane chosen) before it prepares the workspace or probes. A job that could not is sent to the admission transaction unprepared, where the wait is recorded as before. No git runs and no worktree is allocated for a job with nowhere to go. A job waiting on its own `workspace` is prepared all the same, even under a full fleet, and is not probed until it could be placed.
- C-6.9. The fleet cap is read before the first job, so a full fleet costs a pass one count and writes nothing.
- C-3.6. The admission transaction opens as `job.capacity_waiting` and is retitled `attempt.reserved`, with the attempt id and lane, only once the attempt row is inserted. The job's exclusions are written when they change rather than on every pass.
- The per-tick export check is one join driven from `leases`, and `~/.claude.json` is parsed only when `stat` says it changed.

## What was measured

`tools/measure_idle_cost.py` runs a real `Daemon` with the production tick on a temp state root holding 300 accepted terminal jobs: idle; then with four running attempts (real sleeping processes as guardians) and twelve queued jobs across three tiers and two models; then with those attempts ended and every lane closed, after the recheck clock has reached its ceiling. Thirty-second windows, `getrusage` for the daemon and for the children it reaped, on this Mac, run from a checkout of each revision. The timers are switched off, because they are periodic work of their own and the sessions mirror reads the machine's real transcripts (see the last section).

| | `origin/main` (4eb412f) | this branch |
|---|---:|---:|
| Idle: daemon | 7.4% | 0.5% |
| Four running, twelve waiting: daemon | 58.3% | 2.3% |
| Four running, twelve waiting: its `ps` and `git` children | 82.0% | 2.0% |
| Every lane closed, twelve waiting: daemon | 16.0% | 1.1% |
| Every lane closed, twelve waiting: children | 5.1% | 0.0% |
| Rows written in 30 s under a full fleet | 358 events, 179 decisions | 0 |
| Rows written in 30 s with every lane closed | 360 events, 180 decisions | 6 events, 0 decisions |

The machine was busy during these runs (load average above 20), so the small numbers are upper bounds. The measuring process is also small, and a fork from it is cheap (0.85 ms of parent CPU at 50 MB against 8.2 ms at 1 GB), so the baseline column understates what the live daemon paid: it held a full core where this baseline held 58%.

`tests/fake/test_idle_cost.py` pins the behaviour by counting processes at the one seam every `subprocess` call goes through: a waiting queue starts none across five passes, a full fleet writes nothing across ten, four running attempts ticked twenty times cost one `ps` and one `sysctl`, and no event says `attempt.reserved` unless an attempt was. Its tests fail on `origin/main` except the one that guards C-6.8's workspace wait on a fleet with room, which passes on both. `tests/fake/test_capacity_wait_backoff.py` pins C-6.10.

## What this does not change

- The 97,623 redundant rows already in the store are removed by `python -m subfleet.prune_decisions` (its own branch), not by the daemon. Retention cannot see them: C-8.4's 2 GiB is measured over `jobs/` and worktrees, not over `state.sqlite3`.
- The daemon's resident size (1.0 GB after fifteen hours) was not investigated.
- The sessions mirror timer was not looked at. In the measurement harness, with this machine's real home directory, thread samples taken once it started running (every 60 s) were dominated by `sessions/mirror.py:_pass` reading and globbing transcripts, and the process went from 2% to 15% of a core over the following 45 s. Whether it costs the live daemon the same was not measured.
