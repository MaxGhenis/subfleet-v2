# The daemon that kept its lock after it stopped serving, 2026-09-25

From at least 19:15Z until a restart at 19:26Z on 2026-09-25, every client of
the live daemon was refused, and no replacement daemon could start. The daemon, pid 93697, had
begun to stop. It had shut its listening socket, but it held `daemon.lock`
while it waited, with no deadline, for threads that never finished. C-5.8a
bounds that wait. The process now ends within 15 s of the start of any stop.
It dumps every thread's stack before it ends, so the next occurrence names the
stuck thread.

## What was seen

These observations are from the incident brief.

- Every `subfleet run` got `no daemon at ~/.subfleet/daemon.sock: [Errno 61] Connection refused`.
- `subfleet daemon start` ended with `another daemon holds daemon.lock`.
- `ps` showed pid 93697 in state `U`. `daemon.sock` existed, with an mtime of 18:30Z, which is its bind time.

The stack sample is `~/chief-of-staff/state/diag/subfleet-daemon-wedge-93697-20260925T152557.txt`. It ran for 2.3 s at 1 ms from 19:25:58Z. The process was launched at 18:27:04Z and had a physical footprint of 285 MB. In the sample:

- **Main thread:** in `ThreadHandle_join`, a Python `Thread.join`, for every sample.
- **Parked on an `RLock`:**
  - `subfleet-control`.
  - 15 of the 16 `subfleet-api` threads.
  - `subfleet-timer_0`, while entering a generator-based context manager.
- **`subfleet-api_11`:** in the garbage collector (`_PyGC_Collect`, list and object traversal) for every sample.
- **Idle on their queues:** all 32 `subfleet-socket` threads and every `io`, `wait`, `poll`, `mirror` and `timer-lane` thread.

The installed release was `20260925T013443Z`, commit `76018ee`. The EMFILE tracebacks in `daemon.log` that day cite its line numbers, with `accept()` at `daemon.py:3646`. Commit `14f818c`, the other release prepared that day, has it at 3719.

## Why the socket refused and the lock stayed

In `76018ee`, `Daemon.close()` (and every branch since, until C-5.8a) does these steps in order:

1. It sets `stopping` and closes the listening socket, but leaves `daemon.sock` in place.
2. It joins the control thread for up to 2 s.
3. It calls `timers.stop()`, which runs `shutdown(wait=True)` on the timers' three pools.
4. It calls `shutdown(wait=True)` on the reader, request, waiter and worker pools.
5. Only then does it close the store, unlink the socket and release the flock.

Steps 3 and 4 have no deadline.

That explains each symptom:

- **Refused connections.** A connect to a Unix socket whose listener is closed while the file remains fails with `ConnectionRefusedError`, errno 61. This was checked on this machine. It is the brief's error exactly.
- **The lock stayed held.** The main thread was joining pool threads that were parked on a lock. The flock is released only after that join returns, or when the process ends.
- **No automatic replacement.** launchd's `KeepAlive` restarts a job only after it exits. launchd's own SIGKILL after `ExitTimeOut` applies only to stops that launchd asked for.

A daemon that crashes and then cannot drain is therefore deaf and alive until someone kills it.

The log is consistent with this ordering. An EMFILE traceback prints only when the exception reaches the top level, that is, after `close()` returns. `daemon.log` has 12 of them from 2026-09-25, so each of those daemons finished closing. Pid 93697 was still inside `close()` when it was sampled.

## What the evidence does not show

- **Which thread held the `RLock`.** A native sample has no Python frames. `subfleet-api_11` was the only thread neither parked nor idle. It spent the whole sample in the collector, and the process was in state `U` (an uninterruptible wait). The machine was swapping heavily later that afternoon (`2026-09-25-mirror-consistency.md`). It may have held the store lock through a slow collection over a paged-out heap. A thread that returned to its pool without releasing the lock would look the same. The store-lock contention behind both possibilities belongs to the store-contention work (C-3.6, C-3.7). The fix here does not depend on which one it was. The next time, the C-5.8a dump carries Python frames and names the holder.
- **Why pid 93697 began to stop.** The other daemon exits that day that left a traceback were EMFILE at `accept()`. `2026-09-25-mirror-consistency.md` attributes this one to the same cause. This report did not verify that.

## The brief's hypotheses

- **A hung accept loop: no.** The main thread was not in `accept`. It was joining threads inside `close()`, which had already closed the listener.
- **Blocked I/O: a factor in speed, not in the lock.** State `U` and a collector running through every sample are consistent with paging. That makes a drain slow. An unbounded join makes it endless.
- **The catalog subprocess: no.** No thread in the sample was waiting on a subprocess. The `catalog run N outlived 60 s; stopping it` lines appear throughout every daemon's log, before and after, and are the conversations service's own timeouts. On the desktop line, `conversations.close()` could also block a stop. PR #47 bounds that.
- **Log size (66 MB): no.** The log is append-only, and no thread in the sample was writing.

## The fix: C-5.8a

- **`watch_stop`.** `main` calls it once. It opens a descriptor on `daemon.log` at start, because a daemon stopping after running out of descriptors could not open one later. It returns `arm`. `arm` starts `faulthandler.dump_traceback_later(stop_grace_s, exit=True)`, then writes one `stopping:` line.
- **Who arms.** Whoever begins the stop arms on its own thread, before `stopping` is set:
  - the SIGTERM and SIGINT handler;
  - `close()`, through `Daemon.on_stop`;
  - `main`, once `serve_forever` ends for any reason.

  A watching thread also arms when anything else sets `stopping`, and only the first call arms. A daemon built in a test process has no `on_stop` and ends nothing.
- **Why a C timer.** faulthandler's timer runs in C, so once armed it fires even while another thread holds the GIL. A watchdog written in Python would need the GIL to run. Arming on the stopping thread means no other thread has to be scheduled first. A test caught the first version of this fix, which armed from a watching thread, never firing behind a thread that took the GIL right after the stop.
- **What firing does.** It writes every thread's Python stack to `daemon.log`, then calls `_exit(1)`. The kernel releases the flock, and launchd's `KeepAlive` starts a fresh daemon.
- **Guardians are untouched.** They are session leaders of their own process groups, so ending the daemon signals none of them. The next daemon adopts their running attempts (C-4.2), as after a SIGKILL.
- **The grace.** `stop_grace_s` is 15 s, under launchd's default 20 s `ExitTimeOut`, so on a slow `launchctl` stop the daemon's own dump comes before launchd's SIGKILL.
- **One writer is kept.** `close()` still releases the lock only after every pool has drained. The bound never releases it early, it only ends the process.

## Tests

`tests/e2e/test_stop_bound.py` runs the real `subfleetd`:

1. A worker commits `attempt.running`, then parks forever at the `running` boundary while the guardian's provider runs.
2. SIGTERM is sent. While the daemon is stopping, `daemon.lock` is still held.
3. The process ends between 2 s and 10 s later with exit 1. The dump names the held `subfleet-io` thread, in `hold` under `_boundary`.
4. The flock is free, and the guardian is still alive.
5. A fresh daemon adopts the attempt. The job succeeds with one attempt and one notice.

With `main`'s arming removed, the test fails with "the stopping daemon still holds daemon.lock 10 s after SIGTERM", which is the incident.

`tests/unit/test_stop_watchdog.py` runs the real `watch_stop` in child processes. It covers four stuck shapes:

- threads parked on a lock whose holder never lets go, which is 93697's shape;
- a thread that holds the GIL in a C loop;
- interpreter shutdown joining a thread that never ends;
- a stop that follows descriptor exhaustion, where EMFILE is confirmed first.

It also covers `stopping` set by something other than the daemon's own stop paths. The tests are exhaustive over those shapes, and a Hypothesis property varies the grace and the moment of the stop. The invariants are:

- the process ends no earlier than the grace and within the grace plus 5 s;
- the `stopping:` line precedes a dump that names the stuck frame;
- a stop that finishes in time exits with its own status;
- a daemon that is never stopped is never ended.

Two more tests pin the wiring:

- `main` arms with the daemon's own grace, from the signal handler and at the end of serving, even when `serve_forever` raises.
- `close()` arms first, on its own thread, before `stopping` is set and before it waits on anything.

## Restoring service

- **Restart.** Pid 93697 was ended, and a daemon restarted at 19:26Z (`2026-09-25-mirror-consistency.md`).
- **Descriptor fix installed.** Release `20260925T212556Z` (`ad2c208`) was installed at 21:26Z. It raises the open-file limit, caps connections and serves through a failed accept. The running daemon has used release `20260926T012804Z` since 01:33Z on 2026-09-26.
- **The two guardian-supervised jobs in the brief each finished once, with no duplicate attempt.**
  - `20260925-133217-audit-s1-site`: `a1` failed `reserved-no-launch` before any launch, and `a2` succeeded with rc 0.
  - `20260925-134641-audit-s4-proposals-oaif-anthropic`: `a1` succeeded with rc 0.
- **The daemon now.** At 04:45Z on 2026-09-26 it answered `ping` in 25 s, which is past the CLI's 15 s default. That is the store-lock contention stall, not a wedge. The daemon places jobs and answers.
