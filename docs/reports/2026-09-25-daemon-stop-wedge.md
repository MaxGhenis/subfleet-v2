# The daemon that kept its lock after it stopped serving, 2026-09-25

From at least 19:15Z until a restart at 19:26Z on 2026-09-25, every client of
the live daemon was refused, and no replacement daemon could start. The daemon,
pid 93697, had begun to stop. It had shut its listening socket, but it held
`daemon.lock` while it waited, with no deadline, for threads that never
finished. C-5.8a bounds that wait. A stop that has not finished 30 s after it
was armed ends the process. Before it ends, it dumps its threads' Python
stacks, so the next occurrence shows where each thread was stuck. launchd and
`subfleet daemon stop` stand behind it with a SIGKILL at 40 s.

## What was seen

These observations are from the incident brief.

- Every `subfleet run` got `no daemon at ~/.subfleet/daemon.sock: [Errno 61] Connection refused`.
- `subfleet daemon start` ended with `another daemon holds daemon.lock`.
- `ps` showed pid 93697 in state `U`. `daemon.sock` existed, with an mtime of 18:30Z, which is its bind time.

The stack sample is `~/chief-of-staff/state/diag/subfleet-daemon-wedge-93697-20260925T152557.txt`. It ran for 2.3 s at 1 ms from 19:25:58Z. The process was launched at 18:27:04Z and had a physical footprint of 285 MB. In the sample:

- **Main thread:** in `ThreadHandle_join`, a Python `Thread.join`, for every sample.
- **Parked in `RLock.acquire`** (a native sample does not show whether that is one lock or several):
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

- **Which thread held the `RLock`.** A native sample has no Python frames. `subfleet-api_11` was the only thread neither parked nor idle. It spent the whole sample in the collector, and the process was in state `U` (an uninterruptible wait). The machine was swapping heavily later that afternoon (`2026-09-25-mirror-consistency.md`). It may have held the store lock through a slow collection over a paged-out heap. A thread that returned to its pool without releasing the lock would look the same. The store-lock contention behind both possibilities belongs to the store-contention work (C-3.6, C-3.7). The fix here does not depend on which one it was. The next time, the C-5.8a dump carries Python frames. That names a holder that is itself stuck, but not a lock left held by a thread that went back to its pool.
- **Why pid 93697 began to stop.** The other daemon exits that day that left a traceback were EMFILE at `accept()`. `2026-09-25-mirror-consistency.md` attributes this one to the same cause. This report did not verify that.

## The brief's hypotheses

- **A hung accept loop: no.** The main thread was not in `accept`. It was joining threads inside `close()`, which had already closed the listener.
- **Blocked I/O: a factor in speed, not in the lock.** State `U` and a collector running through every sample are consistent with paging. That makes a drain slow. An unbounded join makes it endless.
- **The catalog subprocess: no.** No thread in the sample was waiting on a subprocess. The `catalog run N outlived 60 s; stopping it` lines appear throughout every daemon's log, before and after, and are the conversations service's own timeouts. On the desktop line, `conversations.close()` could also block a stop. PR #47 bounds that.
- **Log size (66 MB): no.** The log is append-only, and no thread in the sample was writing.

## The fix: C-5.8a

- **`watch_stop`.** `main` calls it once. It opens a descriptor on `daemon.log` at start, because a daemon stopping after running out of descriptors could not open one later. It also starts the stop-watch thread. It returns `arm`.
- **`arm`.** It starts `faulthandler.dump_traceback_later(stop_grace_s, exit=True)`, then writes one `stopping:` line. The timer comes first because the write gives up the GIL.
  - It takes no lock. A signal handler can run it on a thread that is already inside it; both calls then arm, microseconds apart. Once the deadline is set, a later call cannot move it.
  - If faulthandler cannot start its timer, the stop-watch thread calls `_exit(1)` at the deadline instead. That fallback needs no new thread, but it writes no dump and it needs the GIL.
  - `arm` never raises, and `close()` goes on draining even if arming fails.
- **Who arms.** The thread that begins the stop arms, before `stopping` is set:
  - `close()`, through `Daemon.on_stop`;
  - `main`, once `serve_forever` ends for any reason;
  - the SIGTERM and SIGINT handler.

  A watching thread also arms when anything else sets `stopping`. A daemon built in a test process has no `on_stop` and ends nothing.
- **Why a C timer.** faulthandler's timer runs in C, so once armed it fires even while another thread holds the GIL, which a watchdog written in Python could not do. `close()` and the end of serving arm at once, because they are already running Python. A signal handler runs only when the main thread next holds the GIL. So a thread that keeps the GIL through the signal delays arming until it lets go, and never arms if it never lets go.
- **The signal handler (`stop_request`).** It arms, then sets `stopping`, skipping `Event.set` once the event is set. `Event.set` takes a lock that is not reentrant, so a second signal landing inside a first handler's `set` blocks the main thread. The earlier draft could deadlock that way before arming. Now the first handler has armed before it enters `set`, so that case ends at the deadline.
- **The backstops.** A signal that never gets handled, and a timer that never fires, are covered by launchd and the CLI:
  - **launchd.** With no `ExitTimeOut` in the plist, `launchctl print` reports an exit timeout of 5 s. That SIGKILLed a launchd stop before any dump. `daemon install` now writes `ExitTimeOut` as `stop_grace_s` + 10 s, which is 40 s. The new value takes effect when the plist is rewritten.
  - **`subfleet daemon stop`.** It waited 15 s flat, so it reported failure in exactly the case the bound ends. It now waits 40 s. If the process it signalled is still running, it verifies the identity again (C-5.4) and sends SIGKILL.
- **What firing does.** It writes the Python stacks of up to 100 threads to `daemon.log`, then calls `_exit(1)`. 100 is faulthandler's cap. It lists the newest threads first, so with more than 100 the oldest, the main and control threads, are left out; the live daemon had 94 threads on 2026-09-26. The kernel releases the flock, and launchd's `KeepAlive` starts a fresh daemon.
- **Guardians are untouched.** A guardian calls `os.setsid()` (`guardian.py`), so it leads its own session and process group, and ending the daemon signals none of them. The next daemon adopts their running attempts (C-4.2), as after a SIGKILL.
- **The grace is 30 s.** That outlasts probe containment during a stop. A stop ends probe waits. Containment then sends SIGTERM, polls the census for up to `term_grace_s` (15 s), then sends SIGKILL and takes one more census (`_contain_probe`). With a 15 s bound, containment's SIGKILL could never run.
- **One writer is kept.** `close()` still releases the lock only after every pool has drained. The bound never releases it early, it only ends the process.
- **What the bound can cut.** It can end a stop that is slow but healthy, and that includes the session mirror's flag publish. Once that publish starts, it does not check for cancellation, and it is not crash-safe (`2026-09-25-mirror-consistency.md`, open items). Before this change:
  - a stop that did not come from launchd waited for the publish without a deadline;
  - a launchd stop cut it at 5 s;
  - a crash, an OOM kill or an operator's SIGKILL cut it at any moment.

  Now every stop gets 30 s. Making the publish crash-safe is the mirror's own open item.

## Tests

`tests/e2e/test_stop_bound.py` runs the real `subfleetd`:

1. A worker commits `attempt.running`, then parks forever at the `running` boundary while the guardian's provider runs.
2. SIGTERM is sent. While the daemon is stopping, `daemon.lock` is still held.
3. The process ends between 5 s and 15 s later with exit 1. The dump names the held worker's frames, `hold` under `_boundary`, and the main thread joining it from `close()`. On Python 3.14, faulthandler also prints thread names, so the dump names the thread, `subfleet-io_*`.
4. The flock is free, and the guardian is still alive.
5. A fresh daemon adopts the attempt. The job succeeds with one attempt and one notice.

With `main`'s arming removed, the test fails with "the stopping daemon still holds daemon.lock 15 s after SIGTERM", which is the incident.

`tests/unit/test_stop_watchdog.py` runs the real `watch_stop` in child processes. It covers four stuck shapes:

- threads parked on a lock whose holder never lets go, which is 93697's shape;
- a thread that holds the GIL in a C loop, started after the stop was armed;
- interpreter shutdown joining a thread that never ends;
- a stop that follows descriptor exhaustion, where EMFILE is confirmed first.

It also covers:

- `stopping` set by something other than the daemon's own stop paths;
- faulthandler failing to start its timer;
- `arm` called again and again after the stop, which must not move the deadline;
- a C loop taking the GIL for good during the stopping line's write;
- a SIGTERM inside `arm`, then another inside `Event.set`, through the real `stop_request`. The tests are exhaustive over those shapes, and a Hypothesis property varies the grace and the moment of the stop. The invariants are:

- the process ends no earlier than the grace and no later than the grace plus 5 s;
- one `stopping:` line precedes a dump that names the stuck frame;
- a stop that finishes in time exits with its own status;
- a daemon that is never stopped is never ended.

Further tests pin the rest:

- **Wiring.** `main` arms with the daemon's own grace, from the signal handler and at the end of serving, even when `serve_forever` raises. `close()` arms first, on its own thread, before it waits on anything, and still drains and unlocks when arming raises.
- **Ordering.** The grace outlasts probe containment, and launchd's `ExitTimeOut` and `daemon stop`'s wait outlast the grace.
- **`daemon stop` against real stub processes.** One ends itself late, like a daemon at its bound, and is reported stopped, not failed, and never killed. One ignores SIGTERM, like a daemon whose handler never ran, and gets SIGKILL. An identity that can no longer be verified is never killed.

Two mutation runs on 2026-09-26 re-introduced one defect each. Every mutant was caught by at least one of these tests:

- **The first matrix (12).**
  - skip the arm in `close()`;
  - set `stopping` before arming;
  - drop the watching thread;
  - drop the arm when serving ends;
  - open the log lazily;
  - `exit=False`;
  - drop the fallback;
  - let an arming failure abort `close()`;
  - a flat 15 s wait in `daemon stop`;
  - no SIGKILL escalation;
  - no `ExitTimeOut`;
  - a 15 s grace.
- **The second matrix (9), after the second review round.**
  - the non-blocking lock back in `arm`, which is Astra's nested-signal wedge;
  - write the line before starting the timer;
  - let a later arm move the deadline;
  - no stop-watch fallback;
  - a literal 15 s in `daemon stop`;
  - skip the arm in `close()`;
  - set before arming in the handler;
  - `exit=False`;
  - open the log lazily.
- **One targeted check.** A faithful write-before-timer reorder, with one line and no extra write, was caught by the GIL-during-write case alone.

## Review

- **First round.** Two in-session reviewers finished before a usage limit stopped the rest.
  - **Concurrency.** It showed with probes that `daemon stop` reported failure whenever the bound was what ended the daemon. It also showed that a SIGTERM arriving while a thread holds the GIL arms nothing, and that a failure inside faulthandler could disable the bound and abort `close()`.
  - **Safety.** It found that launchd's effective exit timeout here is 5 s, that probe containment's SIGKILL could never run under a 15 s bound, and that the bound can cut the mirror's publish.
- **Second round.** Independent Opus and Astra reviews ran on Subfleet lanes (`~/reviews/c58a-bounded-shutdown/`). Both requested changes.
  - **Astra reproduced two wedges.** A SIGTERM inside `arm`, then another inside `Event.set`, left no timer. A faulthandler failure, followed by a GIL holder or thread exhaustion, left the plain `Timer` fallback unable to run.
  - **Both flagged tests.** The e2e test's windows were tight under load. A deadline-reset mutation and a literal 15 s wait both survived.
  - **Both flagged claims.** Containment does not use `kill_settle_s`; "every thread" meets faulthandler's 100-thread cap; a dump cannot name a lock left held by an idle thread; "one RLock" is not visible in a native sample; and the mutation note for `main`'s arming was stale.
- **What changed.** `arm` lost its lock and cannot move a set deadline. The fallback is now the stop-watch thread. The handler is `stop_request`. The e2e grace is 5 s with a 30 s provider. Tests pin each case above, and the wording is corrected. What remains unbounded is a stop whose faulthandler timer cannot start while another thread holds the GIL for good. For signal-initiated stops, launchd's `ExitTimeOut` and `daemon stop` still end it at 40 s.

## How the claims here were established

- **The sample and `close()`'s steps.** The sample file, and `git show 76018ee:subfleet/daemon.py`.
- **The release.** `release.json` under `~/.local/share/subfleet/releases/`, plus the traceback line numbers against `76018ee` and `14f818c`.
- **The 12 tracebacks.** A `grep` of `~/.subfleet/daemon.log`, dated by the nearest preceding timestamp.
- **The 5 s exit timeout.** `launchctl print gui/501/com.subfleet.daemon`.
- **The refused connect.** A local probe: a closed listener whose socket file remains gives errno 61.
- **faulthandler.** Local probes on 3.12.14, 3.13.9 and 3.14 for thread names, the 100-thread cap, finalization, and GIL holders.
- **Job outcomes.** A read-only query of `state.sqlite3`.
- **Ping times.** `Client(timeout=240).call("ping")` and `subfleet daemon status --json`.

## Restoring service

- **Restart.** Pid 93697 was ended, and a daemon restarted at 19:26Z (`2026-09-25-mirror-consistency.md`).
- **Descriptor fix installed.** Release `20260925T212556Z` (`ad2c208`) was installed at 21:26Z. It raises the open-file limit, caps connections and serves through a failed accept. The running daemon has used release `20260926T012804Z` since 01:33Z on 2026-09-26.
- **The two guardian-supervised jobs in the brief each finished once, with no duplicate attempt.**
  - `20260925-133217-audit-s1-site`: `a1` failed `reserved-no-launch` before any launch, and `a2` succeeded with rc 0.
  - `20260925-134641-audit-s4-proposals-oaif-anthropic`: `a1` succeeded with rc 0.
- **The daemon since.** At 04:45Z on 2026-09-26 it answered `ping` in 25 s, which is past the CLI's 15 s default. It was placing jobs, so it was slow rather than wedged; the store-contention work addressed that. Release `20260926T125212Z` (2.1.6, `e053b2c`, which includes that work) has run since 13:25Z, and it answered `ping` in 3.7 s at 15:42Z. It predates C-5.8a.
