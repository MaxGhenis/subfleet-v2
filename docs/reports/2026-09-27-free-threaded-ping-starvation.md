# The ping that waited for a thread the pool never started, 2026-09-27

`tests/fake/test_daemon_descriptors.py::test_idle_connections_past_the_old_pool_never_starve_a_request`
failed intermittently on the free-threaded interpreter only. It holds 40 idle client
connections open and then sends a `ping`, which must be answered within 1 s. On a
failure the ping timed out in `recv` after 3 s: the daemon never answered.

The cause is in the standard library, not in the daemon's connection code:
`concurrent.futures.ThreadPoolExecutor` can count one idle thread twice, after which a
call waits for a running call to end although the pool has room. In release/217 before
PR #43 each connection's reader was a call on such a pool, so the ping's reader
waited until one of the 40 idle clients closed. The daemon's pools are now
`subfleet.pool.Pool`, which keeps one exact count (C-16.5).

## What was measured

Failure rates of the test, as reported in the brief:

| Interpreter | Commit | Failures |
|---|---|---|
| cpython 3.14.7 free-threaded | e053b2c | 14/40 |
| cpython 3.14.7 free-threaded | 05b849f | 9/40 |
| cpython 3.14.7 free-threaded | 917641c | 3/15 |
| cpython 3.14.4 (GIL; production and `build_release` pin) | 917641c | 0/40 |
| cpython 3.14.4 (GIL) | 324b6d7 | 0/15 |

At 5376718f the test passed 40 of 40 runs in isolation on the free-threaded
interpreter. The machine's load average was about 200 on 18 cores, and the race is
a matter of timing.

## How it was found

An in-process copy of the test ran the same scenario repeatedly on 5376718f,
free-threaded, with the daemon's reader pool instrumented before its first thread
started. Its idle semaphore and its work queue were replaced by wrappers that log each
`acquire`, `release`, `put`, `get_nowait` and blocking `get`, with the thread and a
nanosecond clock. On the first ping not answered in 3 s, the harness dumped the pool's
state and every reader thread's stack.

The failing state (run 23 of that session):

```
{'exc': "TimeoutError('timed out')", 'sem_value': 0, 'qsize': 1, 'pool_threads': 40,
 'alive_socket_threads': 40, 'reading': 41, 'connections': 41, 'max_workers': 512}
busy socket threads (top frames): ... 'return self._sock.recv_into(b)' ...
```

41 connections were admitted: the 40 idle clients and the ping. There were 40 reader
threads, each in `recv_into` on an idle client. The ping's reader was queued, with 472
threads to spare. The start of that run's log shows how it got there. Times are from
the fixture's readiness probe; `socket_0` is the thread that read the probe
connection, which its client had closed.

```
6902.1us subfleet-socket_0   q.get_nowait.empty       probe read; queue empty
6954.2us serve_forever       q.put        item A
6997.3us serve_forever       sem.acquire  got=False value=0   no idle thread: starts socket_2
7018.6us subfleet-socket_0   sem.release  value=1              socket_0 says it is idle...
7022.4us subfleet-socket_0   q.get        item A               ...and takes A itself
7186.6us subfleet-socket_2   q.get_nowait.empty
7192.6us subfleet-socket_2   sem.release  value=2              2 permits, 1 idle thread
7788.4us serve_forever       q.put        item B
7796.8us serve_forever       sem.acquire  got=True  value=1    socket_2 takes B
7822.7us serve_forever       q.put        item C
7823.8us serve_forever       sem.acquire  got=True  value=0    no thread is idle: C waits
7830.9us serve_forever       q.put        item D   size=2
7834.7us serve_forever       sem.acquire  got=False            starts socket_3, which takes C
```

From there each new thread took the call before the last, so the queue stayed one
call behind, and the last call queued, the ping's reader, waited. The same
interleaving appeared in other runs that passed. The harness did not establish what
spent the extra permit in those runs.

## The mechanism

`ThreadPoolExecutor` (read in cpython 3.14.7t's `concurrent/futures/thread.py`) reuses
idle threads by counting permits in `_idle_semaphore`. A worker adds a permit each
time `get_nowait()` finds its queue empty, then blocks in `get()`. `submit` puts the
call, then takes a permit with `acquire(timeout=0)`, and starts a thread only if there
is none. The count assumes a permit means an idle thread. That breaks when a worker
finds the queue empty, then `submit` puts a call and finds no permit, so it starts a
thread, and then the worker releases its permit and takes the call. The new thread
finds the queue empty and releases a second permit. That makes two permits for one
idle thread. The next submit takes the spare permit and starts no thread, so its call
waits for a running call to end. When the running calls hold their threads (connection
readers until their client closes, `wait` and the conversation long polls for up to a
minute), the wait lasts that long.

The window is the few bytecodes between a worker's `get_nowait()` failing and its
`release()`. Without a GIL, `submit` on another core lands in it. With the GIL, it
takes a thread switch at exactly that point, and the default switch interval is 5 ms.
It is the same race on both interpreters. A churn loop of the same shape as
`tests/unit/test_pool.py`'s last test stranded a call in the standard pool. Each round
used a fresh 64-thread pool, one call that returns at once, 8 calls that hold their
threads, then one more call that must start.

| Interpreter | Rounds | Result |
|---|---|---|
| free-threaded | 300 per run | stranded in 2 runs of 3 (rounds 59, 23) |
| GIL, 5 ms switch interval | 300 per run | no strand in 2 runs |
| GIL, `sys.setswitchinterval(1e-6)` | 300 per run | stranded in 2 runs of 2 (rounds 154, 59) |

The standard pool has a second defect, which the daemon did not depend on.
`shutdown(cancel_futures=True)` cancels queued futures while it holds
`_shutdown_lock`. A done callback that calls `submit` then waits for that lock
forever. The faulthandler dump shows it: `thread.py:266 shutdown` into
`_base.py:374 cancel` into the callback into `thread.py:200 submit`.

## The fix

`subfleet/pool.py`'s `Pool` keeps its queue, its idle threads and its thread count
under one lock. A call is handed straight to an idle thread through that thread's own
slot. Otherwise it starts a new thread while the pool has fewer than `max_workers`, or
it is queued, but only when all `max_workers` threads run calls. A thread that
finishes takes the next queued call before it goes idle. No count can drift, because
the state it would describe is the state itself. Otherwise it keeps the standard
pool's interface and behaviour:

- `submit` raises `RuntimeError` after `shutdown` and when no thread can start. A call
  that could not start a thread is not queued (the standard pool queued it first).
- Threads are kept until `shutdown`.
- `shutdown(wait, cancel_futures)` lets idle threads go, cancels the queued calls
  (outside its lock), and waits for the running ones. It skips joining the calling
  thread.

The threads are daemon threads, so an idle one never holds up the interpreter's exit.
Every owner shuts its pools down before the process ends. `Daemon.close` and
`Timers.stop` wait for the running calls. `ConversationService.close` waits for its
file pool but not for its long polls, which C-16.7 says end when the daemon stops. The
standard pool's non-daemon threads would have held up the exit for those polls.

Every executor the daemon owns is now a `Pool`:

- its own `workers`, `requests`, `lookups` and `waiters`;
- the conversation service's `polls` and `files`;
- the timers' `_cycles`, `_mirror` and `_lanes`.

`readers` no longer exists: PR #43 gives each held connection a reader thread of its
own (C-16.7), which removes the ping's path through a pool. The `wait` and
conversation-poll pools are sized for every connection the daemon may hold. That
sizing only helps if a submit starts a thread whenever the pool has room, and `Pool`
makes that true.

Starting a thread per call was considered and rejected. Starting a thread on the
free-threaded build at this load took 750 µs at the median and 51 ms at p99, against
45 µs and 0.8 ms with the GIL (2000 starts each), so the pool reuses threads.

## Invariants and tests

For every sequence of submits, releases and shutdowns:

- at most `max_workers` calls run at once;
- a call submitted while fewer than `max_workers` calls are unfinished starts without
  any other call ending;
- a call submitted at the cap waits, and waiting calls start in submit order, one as
  each running call ends;
- every call runs exactly once, and a call cancelled by `shutdown` never runs.

The tests:

- `tests/unit/test_pool.py`: example tests for each behaviour above, including a
  thread that cannot start, shutdown from a pool thread, and a done callback that
  calls the pool during shutdown. A Hypothesis test checks the invariants against a
  model after every step of random programs of held, quick and released calls. A
  black-box churn test runs rounds in which a call ends as held calls arrive; it
  uses a 1 µs switch interval on GIL builds.
- `tests/fake/test_daemon_descriptors.py::test_a_thread_going_idle_as_a_call_arrives_leaves_no_call_waiting`,
  on `daemon.waiters` and `daemon.conversations.polls`: the traced interleaving,
  forced. `threading.Semaphore`'s public methods are patched to hold the finishing
  thread just before it counts itself idle, and to let it go once the arriving call
  has found no idle thread. `Pool` counts no permits, so the hook never fires and
  the calls start as they come.
- `test_every_pool_the_daemon_runs_calls_on_starts_them_while_it_has_room`: every
  executor of the daemon, its conversation service and its timers is a `Pool`.

Checked against the code before the fix, on fix/desktop-descriptor-reconcile at
3c8fe558 with only the new tests and `pool.py` added:

- The two race-hook cases and the executor test failed on 3 of 3 runs on each
  interpreter.
- The churn test failed on 3 of 3 runs on each interpreter against `ThreadPoolExecutor`
  behind `Pool`'s interface, in 5.4 to 6.3 s each.
- Three mutants of `Pool` were each caught by 2 to 4 tests: a call queued with room
  left, a thread going idle before it looks at the queue, and a LIFO queue.
