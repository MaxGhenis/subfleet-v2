# Daemon wedged by per-tick process inspection, 2026-09-24

## What happened

Around 07:45 EDT (11:45Z) the daemon that had started at 02:38:43Z
(pid 31388, release `20260923T005311Z`, commit `6420f5b`) stopped answering in
time. `subfleet daemon status` reported the lock holder alive and the socket
present but `ping unreachable` ("no response from the daemon within 5s", and at
other moments `[Errno 61] Connection refused`). A `subfleet run --batch` from the
subfleet-fanout workflow could not submit, and `subfleet runs` fell back to the
offline store. The daemon was not dead: a `daemon.status` sent with a 120 s
client timeout at 11:48Z came back after 38.8 s, and admission still placed the
fanout's jobs (guard preflights at 11:41-11:54Z). Every client gives up well
before that: 3 s (`daemon start`'s liveness check), 5 s (`daemon status`), 15 s
(the default).

The fanout's attempts went live from 11:41Z. The store's `attempt.running` and
`attempt.finalizing` events give two running at 11:48Z (`fv-s1` from 11:41:29Z,
`pilot-env` from 11:45:24Z) and three from 11:54:50Z (`fv2-s2`, `fv2-s3`,
`fv2-s4`); `fv-s2` to `fv-s5` each lived one to three seconds. The fanout's
session was waiting on those jobs from `subfleet.compat wait` and from
PostToolUse hooks that each long-poll one job. At 11:52Z the daemon held about
80 open connections; the clients still alive then were
`subfleet.compat hook PostToolUse` processes 1 to 4 minutes old and a
`subfleet.compat wait`.

`subfleet daemon stop` at 11:59:35Z sent SIGTERM; the daemon had not exited
2.5 minutes later, so it was killed (SIGKILL, 12:02:13Z) and launchd's KeepAlive
restarted it. The three running guardians (`fv2-s2`, `fv2-s3`, `fv2-s4`) and
their Codex children survived, as C-5.1 designs. Those three jobs later show
`rc 130`: their caller had requested cancellation at 11:58:27-11:59:01Z, the
wedged daemon never acted on it, and the new daemon did at 12:03:03Z.

## Cause

The store's one lock saturated. Two of the loads on it grow with the work in
flight, and they compound: each running attempt was re-examined on every control
tick with subprocesses spawned from a large, many-threaded Python process, which
stretches every lock hold; and every worker pass woke every waiter to re-read
its jobs under that lock. A third, the export sweep below, grows with retained
history and was there on every tick.

- The control loop offers every live attempt to a worker each tick (`tick_s`
  0.05 s, C-5.10), and `_process_attempt` called
  `procs.liveness(guardian)` on every pass for a running attempt.
- `liveness` -> `identity` ran three subprocesses: `ps -p <pid> -o lstart=`,
  `ps -p <pid> -o stat=`, and `sysctl -n kern.bootsessionuuid` (`boot_id()`
  re-read the boot UUID every time, though it cannot change under a live
  process).
- Every 0.5 s after the previous one finished, `_record_owned` ran the full
  C-5.5 census, `procs.containment`: a `ps -axo pid=,ppid=,pgid=,stat=`
  snapshot, `ps -axEww -o pid=,command=` (every process's environment: 2.3 MB
  and 1.45 s on this machine), and an identity (two or three more subprocesses)
  for every pid found. `_record_owned` then kept only the process-group members.
- Each finished worker pass called `_notify()`, which woke every `wait` caller
  (the CLI's `wait`, `subfleet.compat wait`, and the PostToolUse hook's long
  poll). Each waiter re-read every job it watched and a leases row, behind the
  store's single `RLock`, on every wake-up.

Observed on the restarted daemon with one running attempt: at least 59 child
processes in 10 s (a 17 Hz poll misses most of them), among them ten
`ps -axEww` dumps. Its process footprint was 1.0 GB in the morning and 1.1 GB
two hours after the restart.

The store serializes every request on one connection and one lock
(`Store._lock`, held across `execute` and `fetchall`), and every thread must
retake the GIL after each SQLite step and each subprocess wait. A reproduction
run once on 2026-09-24 with the installed release's interpreter and code (6420f5b,
unfixed; `census_storm.py` and `census_storm_bigheap.py`) ran the same inspection loop in one to seven threads
beside sixteen threads issuing a 300-row query under an `RLock`:

| running attempts | subprocesses/s | query p50 | query p90 | queries in 12 s |
|---|---|---|---|---|
| 0 | 0 | 0.2 ms | 0.4 ms | 8,325 |
| 3 | 115 | 0.5 ms | 12.0 ms | 6,945 |
| 7 | 161 | 22.8 ms | 104.6 ms | 2,761 |

That run carried a 0.96 GB heap and 71 threads, like the daemon. At the two or
three attempts actually running, this load alone stretches the tail tenfold or
more, not into tens of seconds.

The waiters supply the volume. Measured on the real `Daemon.wait`, eight
waiters watching six running jobs each and woken at 60 Hz made 2,834 store reads
a second. The control loop woke them once per finished worker pass: up to twenty
times a second for each live attempt, for admission, and for each pending
export (a key is not offered again while its worker is still running).

The control loop added a steady term of its own, found in review: to find
pending exports, every tick read every job that had ever been accepted and ran
one lease query per job. With 265 retained jobs that was 266 statements and
2.65 ms of lock-held work a tick uncontended, about 5,300 statements a second,
and it grows with retained history. None of those jobs held a lease.

Together, in one process with the real `Daemon` request path, three inspection
loops, the eight-waiter herd woken at 100 Hz, a stand-in for the session mirror
(a stat walk over `~/.claude/projects`) and a 1 GB heap took an ordinary
`list` plus `show` from 0.5 ms to 4.8 ms at the median and 62 ms at p90 (310 ms
worst), and halved the requests one client completed in 15 s. The production
daemon had more of every term: sixteen API threads and dozens of hook
connections competing, heavier requests (`daemon.status` reads all 31,496
`readings` rows, 46 ms uncontended), the real mirror, and a machine under enough
pressure that it was rebooted at 13:13Z. The reproduction shows the mechanism
and its direction; it does not re-create the 38.8 s.

A second sample, taken while the SIGTERM shutdown hung, showed the main thread
(inside `close()`), the control thread and all sixteen API threads waiting on
`Store._lock`, four worker threads waiting for the GIL inside `poll()`, and
the session mirror's thread holding the GIL while it freed a very large
exception traceback. Two `ps -axEww` children had filled their 64 KiB pipes and
outlived `procs._read`'s 10 s timeout by minutes: the threads reading them could
not take the GIL to read or to time out.

## Fix

- `procs.boot_id()` keeps the boot-session UUID for the life of the process;
  legacy `kern.boottime` seconds are still read every time (the client already
  did this, `client._BOOT_ID`).
- `procs.group_members(pgid)` reads C-5.5's group source alone, with each
  member's start time, from one `ps -axo pid=,pgid=,stat=,lstart=` snapshot.
  `_record_owned` asks for an identity only for a member not recorded under its
  current start and the current boot identity. A pid a new process has taken,
  and a member recorded under another boot identity (legacy seconds once the
  boot UUID can be read, or seconds a clock correction has moved), are recorded
  afresh while still in the group, as the full census did. The snapshot's start time only
  detects the change; the identity is still captured by `procs.identity`
  (C-5.3), and on this machine the two renderings agreed for all 400 processes
  compared. The three-source census is unchanged and still decides every
  release, kill, loss and quarantine.
- `_process_attempt` asks `ps` about a running guardian at most every
  `LIVENESS_INTERVAL_S` (1 s). The exit receipt, the cancel request and the wall
  limit are still read every tick, so a finished attempt moves on at once. A
  guardian that dies without a receipt is noticed at the first inspection due
  one interval after the last, once a worker takes the attempt (an `unknown`
  answer decides nothing, C-4.2). The
  owned-member census runs inside the same paced branch, so it now runs at most
  once a second (it was every 0.5 s). A paced pass that raises clears its
  deadline, so the retry C-5.10 schedules repeats the inspection rather than
  returning at the gate and reading as recovery. The control loop drops pacing
  state for attempts no longer live.
- A running probe's wait loop (`_await_probe`) follows the same budget: its
  receipt, job and deadline every pass, `ps` about its guardian once per
  interval, and its owned group from the group snapshot.
- `_pending_exports` finds accepted jobs that still hold a lease with one
  statement.
- `Store.generation` counts committed top-level transactions that changed a row.
  `wait` re-reads the store only when it has moved, or every `WAIT_RECHECK_S`
  (1 s); `_schedule` wakes waiters only when the generation moved while the
  pass ran (a commit by another worker can still wake them, at the cost of one
  comparison), and decides before it releases the key.
- C-5.11 in `docs/acceptance-contract.md` states the budget.

With the fix, the steady-state cost of a running attempt, where the boot UUID
is readable, is three subprocesses a second (`lstart` and `stat` once a second,
one group snapshot). Where only legacy `kern.boottime` seconds are readable,
the boot identity is not cached and each read costs two `sysctl` calls, twice a
pass, so seven. An inspection
that finds N new or changed members also identifies each of them and re-checks
the leader before recording them: with the boot UUID cached that is 3 + 2N + 2
(seven for one new child); with legacy seconds only, every identity also reads
the boot seconds, so 7 + 4N + 4 (fifteen for one).

The reproductions were run once each on 2026-09-24, against the first version of
this fix (8331709). Where the boot UUID is readable, as on the machine measured,
that loop asks the same questions in steady state as the merged one. The other
review fixes touch code that no reproduction runs. Driving the fixed loop at seven attempts
kept the query p50 at 0.2 ms and p90 at 0.7 ms (7,038 queries in 12 s). The
eight-waiter herd fell from 2,834 store reads a second to 386, most of them the
measuring client's own. The combined reproduction ran at 0.5 ms p50 and 1.5 ms
p90 (101 ms worst), and completed 592 requests.
`tests/unit/test_daemon_inspection_load.py` pins the budget. On the unfixed code,
20 ticks of one running attempt asked about the guardian 21 times (now 2), one
waiter re-read the store 190 times across 200 wake-ups (now 1), 20 idle
worker passes woke waiters 20 times (now 0), and the export sweep went from 266
statements a tick to 1.

An independent review (GPT-6 Astra, through `subfleet run --task review --tier
hard`) found three defects in the first version of this fix: a pid taken by a
new group member kept its old identity, a failing paced pass reset C-5.10's
backoff, and pacing state leaked for attempts that finished normally. It also
found the export sweep and the probe loop. All five are fixed above, each with
a test that fails on the first version. A second independent review (Claude Opus
5.5, `--tier standard`) of the final head found that a member recorded under
legacy boot seconds was never refreshed, that the pacing prune walked dicts
workers write to, and that the probe test could race its receipt; those are
fixed too. `tests/unit/test_daemon_inspection_properties.py` states the
invariants as Hypothesis properties: pacing asks exactly when a greedy clock
allows, recording asks exactly about members not recorded as they are, the
export query matches the per-job sweep it replaced for any jobs and leases, and
the generation counts exactly the committed top-level changes. Reintroducing
each defect fails its test.

## Related findings, not fixed here

- **Retention.** `_retention` gives `maintenance()` a 60 s deadline, and every
  pass sizes every job directory and owned worktree. `find` listed the
  worktrees' 606,364 entries in 43.6 s with a cold cache. `retention._size`
  sized all 36 worktrees (10.59 GB) in 19.6 s with a warm one
  (`retention_hog.py`). A pass sizes only the 32 that jobs own, so for that
  part of a pass 19.6 s is an upper bound. The job directories held about 4,500 entries. Passes timed
  out for hours at a time (`worker retention failed: TimeoutError`, 32 in a row
  on the morning daemon), and each interrupted pass started its walk from zero.
  Retention's own `_pins`, run read-only on the store afterwards, pins 331 of
  334 jobs: 256 finished jobs by notices still `pending` or `offered` (the
  PostToolUse hook marks a notice `offered`, and nothing later acknowledges it),
  75 gate reviews, 69 with salvage, 12 parents, and all 32 jobs that own a
  worktree. The 2 GiB cap cannot be met, so a pass that finishes prunes every
  job that has just become unpinned. The pass that finished at 15:36:16Z pruned
  that morning's jobs, `pilot-env` (finished 11:50Z) among them, and kept jobs
  from September 19.
- **Session mirror.** The mirror timer runs in a daemon thread. Its passes over
  119 desktop account folders (153,602 `local_*.json` entries by 16:20Z) take
  longer than the 60 s interval, so they run back to back. On the restarted
  daemon that thread was busy in nearly every sample, and the process held
  1.1 GB. How much that slows requests is not established. In one run on
  2026-09-24, a thread running Python continuously slowed a lock-serialized
  2,000-row query from 1.5 ms to 101 ms (`gil_convoy.py`). Three reruns on
  2026-09-27 (Python 3.14.4, load average about 170) and five by a reviewer
  showed 0.9 to 1.9 ms.
- **Shutdown** is bounded now: PR #48 (C-5.8a) ends a stop that cannot drain, so it can no longer hold the lock as it did here.
- **Restart after SIGKILL.** A killed daemon leaves `daemon.sock` behind, so
  clients got `Connection refused` for the 26 s the new process spent in
  `Daemon.__init__`, and then no response for about 75 s of recovery.
  `daemon start` treats a 3 s ping miss as no daemon and launches a second
  `subfleetd`, which exits on the lock ("another daemon holds daemon.lock").
  That exit is harmless: the flock is taken before the socket is touched.

## Measurement scripts

The reproduction figures come from the scripts in
`2026-09-24-daemon-inspection-wedge/`. Each figure is one run on 2026-09-24 on a
busy machine. They depend on how the OS schedules threads, and reruns vary: see
`gil_convoy.py` below. The scripts only read the live machine (`ps`, `sysctl`,
`os.stat`), and they build throwaway stores and daemons in temporary
directories. Run each from a checkout of the revision it measures, with
`uv run python`.

- `census_storm.py` and `census_storm_bigheap.py`: the unfixed per-attempt
  inspection loop at 0 to 7 simulated attempts, with a small heap and then a
  1 GB heap (the table under Cause). On 2026-09-24 they ran with the installed
  release's interpreter (6420f5b). To rerun against unfixed code, stay in this
  directory and point `uv` at a checkout from before PR #40:
  `uv run --project <checkout of 2f842d3> python census_storm_bigheap.py`.
  Run from this checkout, they would import the fixed subfleet. Their
  subprocess column is computed from the unfixed code's per-call costs (three
  per liveness check; two plus three per pid per census), not counted.
  `census_storm_bigheap.py` reads `census_storm.py` by relative path.
- `census_storm_fixed.py`: the fixed loop at the same loads. It needs PR #40's
  constants, which later lines rename (PR #37 renames `LIVENESS_INTERVAL_S`), so
  run it with `--project` pointed at a checkout of 7fd9cb5. Its 2026-09-24
  figures were measured against 8331709. It differs by one line from what ran
  then: `members.keys()`, because `group_members` has returned pid to start since
  the review fixes. Against 8331709, drop `.keys()`. Its simulated group is the
  script's own process group, which holds the script's own `ps` children, so it
  finds a "new" member at almost every census. It therefore costs more than the
  merged loop (about 5 subprocesses a second for one attempt, against 3), and
  its figures are conservative.
- `waiter_herd.py`: eight waiters on the real `Daemon.wait`, woken at 60 Hz:
  2,834 store reads a second unfixed, 386 fixed. Run it from a checkout of each
  revision, passing a label such as `unfixed` or `fixed`.
- `combined.py`: the combined reproduction. Pass `unfixed` with `--project`
  pointed at a checkout from before PR #40, or `fixed` with 7fd9cb5. Its fixed
  loop has the same one-line `.keys()` change as `census_storm_fixed.py`. It gave 4.8 ms
  p50 and 62 ms p90 unfixed, against 0.5 ms and 1.5 ms for 8331709.
- `retention_hog.py`: `retention._size` over the worktrees only (19.6 s warm,
  10.59 GB), and its effect on a locked query. The 606,364 entries and the
  43.6 s cold time come from `find`. A directory it cannot read ends its walk
  early, and it then prints a partial total.
- `gil_convoy.py`: one CPU-bound thread against lock-serialized SQLite reads.
  On 2026-09-24 it gave 1.5 ms to 101 ms for 2,000 rows. It did not reproduce
  on 2026-09-27: 0.9 to 1.9 ms in eight runs.

These figures have no script. They came from one-off commands and read-only
queries on 2026-09-24: the 2.3 MB and 1.45 s `ps -axEww` dump, the 59 child
processes, the 266 statements and 2.65 ms of lock-held work a tick, the 31,496
`readings` rows and their 46 ms read, the roughly 4,500 job-directory entries,
and the 400 processes whose `lstart` renderings were compared.

The independent reviews of PR #40 are kept outside the repository in
`~/reviews/subfleet-daemon-wedge-2026-09-24/`.
