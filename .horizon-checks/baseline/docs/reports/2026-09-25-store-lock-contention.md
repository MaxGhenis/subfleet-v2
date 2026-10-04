# The 2026-09-25 store-lock stall: reproduction, causes, fixes

Branch `fix/store-contention`, cut from `feat/desktop-integration` (ff52def). Every number below comes from a command run on 2026-09-25 and is reproducible with `tools/store_contention_repro.py`; the raw reports are listed at the end.

## What was measured on the live daemon

Measured 17:36–17:50 EDT by the operator's session, with the machine at load 85–100 on 18 cores:

- `subfleet runs --mine` timed out after 15 s. A socket `list` with `{"mine": <session>, "running": true}` took 34–94 s and returned 5 KB; the same SQL on a separate read-only connection takes 1 ms. A `ping` took 0.04–90 s.
- A native `sample` showed 18 threads in `rlock_acquire`, one thread in `sqlite3VdbeExec` under `pysqlite_cursor_fetchall`, all 16 `subfleet-api` threads busy, 41 `subfleet-socket`, 7 `subfleet-wait` and 6 `subfleet-io` threads.
- The daemon used 44 CPU-seconds in 22 minutes (3% of a core). fsync took 0.1 ms and the WAL stayed at 4 MB.

## The daemon that stalled did not have PR #40

The installed release is `20260925T212556Z` (`release.json`: source ad2c208, "fix/daemon-descriptors, cut from 76018ee"). Its `daemon.py` still runs `SELECT * FROM jobs WHERE accepted_attempt_id IS NOT NULL` and then one lease query per row on every 50 ms control tick (installed line 1946), and its `store.py` has no `generation`. So the loads PR MaxGhenis/subfleet-v2#40 removed on 2026-09-24 were all live:

- the export sweep: with 308 accepted jobs, about 310 store-lock acquisitions per tick, up to about 6,200 a second;
- a `notify_all` after every worker pass, whatever it committed;
- every `wait` re-reading every job it watched on every wake-up and every 0.25 s;
- three `ps`/`sysctl` subprocesses per running attempt per tick, and a full environment census every 0.5 s.

The live store's sizes (read-only queries, 2026-09-25 ~17:55 EDT): 383 jobs (308 accepted), 638,591 events (6,478 `probe.state` with 2.5 MB of JSON; 980 `gate.state` with 6.35 MB; 970 `job.submitted`), 34,348 readings, 33,407 decisions, 810 MB file.

## How it was reproduced

`tools/store_contention_repro.py` starts a fake-provider daemon from any checkout, seeds a store shaped like the live one (300 accepted jobs, 200k events, 6,500 `probe.state` events, 980 `gate.state` events of 6.5 KB, 34k readings, 3k decisions), and drives it with:

- 6 long-running jobs, each with a hook-style waiter (60 s `wait` long polls);
- 21 hook emulators, each a "Bash call" every 1–4 s: `list` mine running, sometimes `notice.pending`, and a waiter for any job it has not armed;
- a short job every 8 s, whose waiter measures how long `wait` takes to return after the terminal commit (seen by a read-only connection polling every 10 ms);
- one `subfleet gate`-style poller (`gate.poll` at 4 Hz) and probes timing `list`, `show`, `ping` and `daemon.status` once a second;
- optionally a SIGSTOP/SIGCONT duty cycle that lets the daemon run only a fraction of each 100 ms (`--duty`), which pins it to a small share of one core without loading the machine;
- optionally a 2 ms in-process thread sampler (`--sample`) and C-3.6's lock watch at a lowered threshold (`--hold-s`).

The baseline is ff52def with only the diagnostics commit (e1da6e1) cherry-picked, so it reports its own lock holders; "fixed" is this branch. The harness turns the desktop sidebar mirror off: it writes into the real Claude app session store under the real HOME, and a fresh state root has no merge base (see follow-ups).

### Unthrottled, same load (18:30 EDT, machine load ~30)

| | baseline (ff52def + C-3.6) | this branch |
|---|---|---|
| daemon CPU | 0.81 cores | 0.285 cores |
| `list` from hooks, p50 / p99 / max | 33 ms / 337 ms / 1.56 s | 1 ms / 12 ms / 19 ms |
| `notice.pending` p99 / max | 294 ms / 416 ms | 13 ms / 28 ms |
| `show` p99 | 433 ms | 52 ms |
| `gate.poll` p99 | 298 ms | 20 ms |
| store-lock waits over 0.2 s (C-3.6 reports) | 146 | 0 |
| store-lock holds over 0.2 s | 28, the longest 0.4 s | 0 |
| thread samples waiting on the store lock (80 most frequent positions) | 67,120 of 108,672 (62%) | 0 of 23,393 |
| `wait` return after the terminal commit, max | 114 ms | 0.9 ms |

### Held to 5% of a core, same load (18:34–18:44 EDT, machine load ~40)

`--duty 0.05` lets the daemon run 5 ms of every 100 ms. That is about the 3% of a core the live daemon got during the stall. The baseline reproduces the stall's shape at a smaller scale: hook reads take seconds and `ping` takes seconds.

| | baseline | this branch |
|---|---|---|
| hook `list` served in 120 s | 492 | 875 |
| `list` from hooks, p50 / p99 / max | 1.15 s / 12.6 s / 13.7 s | 0.21 s / 1.56 s / 2.2 s |
| `notice.pending` p99 / max | 7.4 s / 9.9 s | 1.2 s / 1.8 s |
| `ping` p99 / max | 5.0 s / 5.0 s | 0.65 s / 0.75 s |
| `show` p99 | 16.6 s | 1.8 s |
| `gate.poll` p99 | 7.8 s | 1.4 s |
| `daemon.status` p50 / max | 6.7 s / 12.7 s | 7.2 s / 19.3 s |
| `submit` p50 / max | 4.3 s / 10.2 s | 3.1 s / 22.1 s |
| longest store-lock hold | 2.6 s | 17.4 s (22.2 s held back in the summary) |

What this shows:
- **Reads.** Every read the hooks and the CLI make stays fast when the daemon is starved.
- **Views and writes.** Requests that build a capacity view (`daemon.status`) or write (`submit`) are no better in the tail. The long holds are admission evaluating a route inside its reserving transaction. A starved evaluation takes seconds of wall time, so a commit almost always lands during it and the early evaluation cannot be reused.
  - A second run (`--duty 0.05`, 90 s) counted 2 reservations that reused the early evaluation and 4 that evaluated again inside.
  - Unthrottled, the counts were 14 reused and 1 again. That run had hook `list` p99 of 4 ms and no store-lock hold over 0.2 s.
  - The readers no longer queue on the lock, so they compete with the writer for the GIL. That stretches a writer's hold when CPU is scarce.
  - A follow-up proposes a validity check narrower than "no commit since", so starved reservations can reuse their evaluation too.
- **Wake latency.** At 5% duty a `wait` returned about 0.9–1.1 s after its job's terminal commit (2–4 samples per run), bounded by the duty cycle itself, since a stopped process answers nothing. With the CPU it needs, the branch returned within 0.9 ms (13 samples), inside C-15.5's 0.1 s poll and the 0.25 s asked for.


## Causes, ranked

1. **Demand far above the CPU the daemon got.** Under the reproduction's hook load, the pre-PR-40 daemon used 0.81 cores; its control loop alone can make up to ~6,200 store-lock acquisitions a second (310 per 50 ms tick with 308 accepted jobs). The live daemon got about 3% of a core. Held to 5% in the reproduction, the same daemon used 0.06 cores and its hook reads took up to 13.7 s: the work queued behind the lock takes as long as the CPU the daemon gets allows, while the process looks idle.
2. **Every read shared the writer's lock.** `Store.query` and `Store.one` took the one RLock around the one connection. In the reproduced baseline, 62% of the counted thread samples were threads waiting for that lock inside `Store.query` or `Store.one`, so a 1 ms `list` waited behind everything queued ahead of it.
3. **Hot reads that grew with history, some inside transactions.** The capacity view read and parsed every reading (34k) on every admission look and status request. It also fetched every `probe.state` event (6.5k, 2.5 MB) once per probe lease; during a probe cycle up to ~21 lanes hold `probe:timer:*` leases whose holders never have a record, so each view scanned them all. One of these views runs inside admission's `attempt.reserved` transaction. `gate.poll` fetched every `gate.state` event (6.35 MB) four times a second per gate client. `_batches`, on every `list`, walked every `job.submitted` event because SQLite chose the `kind` index over the job ids. And `ResetCredits.confirmed_override` re-read the reset-credit history per lane, twice per view. The sample of one thread in `pysqlite_cursor_fetchall` under the lock fits the first two scans; which one held the live lock is exactly what C-3.6 would now have logged.
4. **Waiters.** Each hook's `wait` re-read its jobs on every wake-up and every 0.25 s, and every worker pass woke every waiter.
5. **Coupling.** `Timers` held its own lock across store writes, and the control loop takes that lock every tick. `ping`, which reads nothing, queued behind all of this on the same 16-thread pool.

## What changed

- **C-3.6 diagnostics.** Both stores serialize on a `lockwatch.WatchedLock`. `daemon.log` gets, rate-limited to one line per lock and kind a minute, every hold over 2 s with the holder's live stack and its length, and every wait over 5 s with the waiter count and both stacks. SIGUSR1 dumps every thread's stack to `daemon.log` from `faulthandler`; `subfleet daemon stacks` sends it and prints the dump.
- **PR #40 merged** into this line (paced inspection, one export statement per tick, generation-gated wake-ups; C-5.11).
- **C-3.7 reads off the lock.** Six read-only connections beside the writer; a read outside a transaction takes one for a statement, a transaction reads its own rows, `Store.snapshot()` gives several reads one committed state. `Timers.snapshot` and every capacity view read in one. The hot reads above are rewritten to return what they need: the newest-reading candidates, the newest probe record and gate state found by SQLite, submissions by job id, session events filtered in SQL, override inputs read once. `Timers` writes outside its own lock. `~/.claude.json` is parsed once per version of the file.
- **C-15.5 one reader for every wait.** A hub reads the watched jobs once per commit for all waiters and wakes only those whose jobs are done. A wait returns within 0.1 s plus three reads (the hub's in progress, the hub's that sees the commit, the waiter's own) of the commit that ends its last job, or within 1 s plus those reads for a commit made by another process; the review of 5841d8b found the earlier "one read" tighter than the code gives. A closing daemon answers every wait before it hangs up.
- **C-16.5 routing.** `ping` without text is answered on the connection's thread. `list`, `show` and `notice.pending` have their own pool.
- **C-15.6 hooks.** A hook's client skips the `ps` and `sysctl` lock check: it runs on every Bash call of every session and exits silently either way.
- **C-6.3 evaluation before the reservation.** Admission evaluates the route in a read snapshot, noting the store generation first. The reserving transaction takes that decision when nothing has committed since, it is at most 5 s old, and none of the clocks it read has moved since (a reading ageing out, a closure or an override ending; `capacity.decision_horizon`, added after the reviews of 5841d8b and f48df54); otherwise it evaluates again inside, as before. `daemon.status` counts both, and why an evaluation was repeated. The evaluation is also cheaper, measured on a copy of a harness store with 34,004 readings: its readings now take 8.4 ms to read (the candidates) and 0.2 ms to build into a view, against 62 ms and 14 ms for every reading; attempts, jobs and `scheduler.evaluate` add about 4.5 ms.

## Invariants, and the tests that pin them

| Invariant | Tests |
|---|---|
| A read outside a transaction never takes the store lock; a transaction reads its own rows; a snapshot is one committed state and holds no writer back | `tests/unit/test_store_readers.py` |
| Each rewritten read answers exactly what the old form did (differential, 40 seeded random stores each) | `tests/unit/test_store_contention_queries.py` |
| A wait returns within `WAIT_POLL_S` plus three reads (the hub's in progress, the hub's that sees the commit, its own) after its last job's terminal commit, with or without a poke; a waiter is always woken or times out, whatever the hub's thread does; hub reads do not grow with waiters; no wake-up is lost under random interleavings; all jobs, exports and unknown jobs behave as before | `tests/unit/test_wait_hub.py`, `tests/unit/test_daemon_inspection_load.py` |
| `ping` and the hook reads answer while every request thread is busy and a transaction is open; a closing daemon answers every wait | `tests/fake/test_request_routing.py` |
| A hook inspects no process; `Timers` never writes under its own lock; the login hint is re-parsed after any write, replace or touch | `tests/unit/test_hook_cost.py`, `tests/unit/test_lockwatch.py`, `tests/unit/test_desktop_account_cache.py` |
| The watched lock is still a correct re-entrant lock; long holds and waits are reported once with live stacks, rate-limited; SIGUSR1 dumps every thread | `tests/unit/test_lockwatch.py`, `tests/fake/test_daemon_stacks.py` |

## Not changed here

- **A starved admission still evaluates inside `attempt.reserved`.** At 5% of a core most reservations saw a commit after their early evaluation, evaluated again under the writer lock, and held it for up to 28.8 s. Writers waited behind it (`submit`, inserts, timer reservations); reads did not.
- **Follow-ups filed as tasks:**
  - Isolate the fake-daemon tests from the real sidebar store: they run the mirror under the real HOME with an empty merge base.
  - Make the in-daemon sidebar mirror incremental: it rescans 21k–75k index entries per pass, about a fifth of busy samples in an early profile.
  - Take one shared `ps` snapshot for all running attempts: liveness forks were the largest remaining busy cost.
  - Give admission a validity check narrower than "no commit since", for example a capacity version kept by triggers, so a starved reservation can reuse its early evaluation.
- Seen and not pursued: SQLite's automatic WAL checkpoint runs inside the committing transaction; `_process_attempt` reads the store and two receipt files every tick per attempt; the live retention pass times out every 60 s (`worker retention failed: TimeoutError`).

## Deploying

Nothing here is installed. The installed release lacks PR #40 as well as everything above, so the next release of the desktop line should carry this branch. Installs go through the Subfleet desktop transition session.

## Raw evidence

Everything is in `~/reviews/store-contention-2026-09-25/`, outside the repo:
- `repro/`: every run's `--report` JSON and text summary.
  - `R1-baseline` and `R2-fixed`: the unthrottled comparison.
  - `T1-baseline-duty05` and `T3-fixed2-duty05`: the 5% comparison.
  - `T4` and `U4`: the reuse counts.
  - `C-pr40-sample`: the first thread-sample profile.
  - The lock-watch excerpts of the throttled runs.
- `audit/`: the audit of every store-lock site (workflow `wf_f0153181-ac1`), six readers and a completeness critic.
- `review-brief.md`: the brief given to the independent reviewers.
