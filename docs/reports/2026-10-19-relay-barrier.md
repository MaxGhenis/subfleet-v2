# Relay stop bound and CPU scaling scope (#230 review r1, two P3s)

2026-10-19. Built on `release/217` at `d72597b5c` as Subfleet job
`20261019-002120-relay-barrier-build`, for the branch `fix/relay-barrier-bound-217`.
Not pushed; the hub opens the PR.

| Commit | What |
|---|---|
| `aab0dee5b` | test: a retained stdin reader cannot hold the relay's stop |
| `0c889acd7` | fix: bound the relay stop's wait for an exchange already read, with the C-26.4 amendment |
| `e9432a47b` | test: state and pin the CPU growth criterion's scope |
| this commit | this report |

## 1. The relay's stop barrier

### The cause, as executed

A probe at `d72597b5c` ran the reviewer's input through the real `RelayServer`:

- a child was started with the relay's stdin pipe as its stdin, and exited;
- the probe kept the pipe's read end open and never read it;
- a client sent a 2 MiB `write` frame;
- once the frame's intent was logged, `stop()` ran on a thread.

`stop()` had not returned after 5 s. The relay thread's stack was `_accept_loop` →
`_serve_connection` (`relay.py:312`) → `apply` → `os.write(self._pipe, view)`
(`relay.py:396`). The pipe was full and its only reader never read, so the write
blocked while holding `_lock` and `_exchange_lock`. `stop` blocked at
`with self._exchange_lock: pass` (`relay.py:249`). Closing the read end ended the
wait at once: the write failed with EPIPE, the frame was logged `failed` (errno 32),
and `stop` returned.

The guardian calls `relay.stop()` between `child.wait()` and publishing `exit.json`
(`guardian.py:208-243`). So a process that kept a finished provider's stdin open
without reading held the attempt's exit receipt for as long as it lived.

The old docstring said "Replies have their own bound". That was true of `_reply`, but
the exchange the barrier waits for also spans `apply`, and the pipe write in `apply`
had no bound.

### What the plan as written would not have done

The hub's decision: bound the barrier, then on expiry shut the connection down both
ways and let `stop` go on. Two findings changed how that is done.

1. **Shutting the socket down does not end a pipe write.** The blocked thread is in
   `os.write` on the pipe, not on the socket. Also, `stop`'s `_close_pipe` takes
   `_lock`, which `apply` holds through the write, so `stop` would still block there.
   The mutant `socket-only-cut` keeps the bound and the shutdown but drops the write's
   cut check. It fails the same three regression tests as the unbounded barrier.
2. **On macOS, `shutdown(SHUT_RDWR)` after `shutdown(SHUT_RD)` fails and leaves the
   write half open.** Observed with a socketpair on this host:
   - `SHUT_RDWR` after `SHUT_RD` raised `[Errno 57] Socket is not connected`, and a
     later `sendall` was delivered;
   - `SHUT_WR` after `SHUT_RD` succeeded, and a later send failed with EPIPE.

   `stop` has always shut the read half first, so a single `SHUT_RDWR` call in the
   cut does nothing on macOS. My first version of the fix did exactly that. In all
   three regression tests the writer's `failed` answer reached the client. The mutant
   `one-shut-rdwr` reproduces this.

### The fix (`subfleet/relay.py`)

- **The bound.** `stop` takes `_exchange_lock` with `timeout=REPLY_TIMEOUT_S`
  (`relay.py:267`). That is the reply bound that already existed (`relay.py:69`,
  1 s, used by `_reply`), which is also the relay thread's join budget.
- **The cut**, when the bound passes (`_cut_exchange`, `relay.py:281`), in this order:
  1. Shut the read half and the write half of the connection, each in its own call
     (`relay.py:290`). A reply or read under way ends, and the client reads end of
     stream.
  2. Set the cut flag (`relay.py:295`). Doing this after the shutdown means the cut
     write's answer reaches no one.
  3. Log `CUT_LOG_LINE` (`relay.py:74`) at warning level on the logger
     `subfleet.relay`, with no arguments. It names no frame, tag or client.
- **A stdin write that can be cut.** `serve` makes the pipe's write end non-blocking
  (`relay.py:241`). Only the guardian holds that end: it comes from `os.pipe()`, which
  makes descriptors non-inheritable, and the guardian passes only the read end to the
  child.
  - `_write_line` (`relay.py:447`) writes what fits. When the pipe is full, it waits
    with `poll(POLLOUT)` for up to `WRITE_POLL_S` (50 ms) at a time, and checks the cut
    flag before every write.
  - A cut raises `OSError(ECANCELED)` (`relay.py:456`). The existing failure path in
    `apply` logs that as `failed` with that errno, so `apply` releases `_lock`.
  - `stop` then goes on: thread join (1 s budget), server close, unlink, pipe close.
- **Unchanged:**
  - an exchange that finishes inside the bound is acknowledged before `stop` returns,
    including a `close` that made the child exit;
  - `SHUT_RD` still wakes an idle read at once;
  - on a blocking pipe, a write behaves as before. That covers the unit tests that
    drive `apply` without `serve`.

**Where the line goes.** The guardian configures no logging handler, so the warning
reaches its stderr through Python's last-resort handler. Under the daemon, that stderr
is `/dev/null` (`daemon.py:7714`, C-5.1). So in production the durable record of a cut
write is its `failed` entry, with errno `ECANCELED`, in `stdin.jsonl`. A cut that comes
after the write finished (in the reply) leaves the frame `written` with no answer sent.
Keeping the line itself would need a guardian log destination. That is a follow-up
below, not done here.

**The contract.** C-26.4 now states:

- the bound;
- the cut, and the separate shutdown of each half;
- the `failed`/`ECANCELED` record and the 50 ms look;
- where the line goes;
- that a retained reader can no longer hold the receipt.

A change-log entry is under "Changes in version 3". C-26.4 is the clause that covers
`stop`. C-24.x and D-13 (the turn's stop escalation) don't describe the relay's own
shutdown, so they are unchanged.

### The invariants, as tested

1. `stop` returns within `REPLY_TIMEOUT_S` plus 1 s, whatever the exchange is doing.
2. An `ok` acknowledgement is only sent for a frame logged `written`.
3. A frame is logged `written` exactly when the reader can read the whole line.
   Otherwise it is `failed` with `ECANCELED`, and the reader holds a strict prefix of
   the line.
4. After `stop`, no relay thread or socket file is left, and the pipe's write end is
   closed.
5. An exchange that finishes inside the bound is acknowledged before `stop` returns.

### The callers when an exchange is cut

None of the callers relies on `stop` returning only after the reply. Each one already
handles an exchange that gets no answer.

- **Guardian** (`guardian.py:208-243`). It calls `relay.stop()` after `child.wait()`
  and uses nothing from the reply. With the cut, `stop` returns about a second after
  the child exits. The guardian then publishes `exit.json` with the child's own exit
  status, and lingers (D-CEN1) as before. The process test shows this: `rc` 0, the
  frame `failed` with `ECANCELED`, the socket gone.
- **Turn runner sending a frame** (`runner.py:134`; `_transmit`; `_unacknowledged` at
  704).
  - Its `RelayClient` waits up to 30 s for an answer. A cut instead gives it end of
    stream within about a second.
  - That raises `RelayError`, counted as unacknowledged (IR-27). For the message frame
    it also sets `handover_tried`.
  - A resend comes after 0.2 s, doubling, at most 5 times. Each finds the relay gone.
  - Once `exit.json` exists, `_process_gone` (396) ends the loop. Stdout is read to the
    end, steers are discarded as `provider-ended`, `driver.eof` runs if there was no
    terminal event, and the runner flushes and reports.
  - At `d72597b5c`, the stuck exchange held the send until its 30 s timeout, and a
    resend could only queue behind it.
- **Turn runner's D-13 escalation** (`_timers`, 1062 and 1072). Its SIGINT and `close`
  frames take the same send path, so they meet a gone relay as above. Its last step,
  `on_contain`, does not use the relay.
- **Conversation containment** (`service.py`).
  - `_on_contain` (2977) records the job's cancel request. Nothing in it waits on the
    relay.
  - `ConversationService.close` (218) calls `TurnRunner.stop`. That only ends the
    runner's loop; it is not `RelayServer.stop`.
  - In the daemon's attempt pass, an exit receipt is handled (`daemon.py:7575`,
    `_begin_finalizing`) before the cancel-and-kill branch (7587, `_kill_attempt`). So
    a cut attempt is finalized from the guardian's receipt.
  - Finalization's census (8344-8354) treats the retained reader like any process it
    finds after a receipt: a settle window (`exit_settle_s`), then quarantine as
    "writers remain after exit receipt".
  - That is how such an attempt already ended when no frame was in flight. At
    `d72597b5c`, a frame in flight held the receipt instead, until the reader closed
    stdin. A reader killed by containment closes its descriptors, which ended the wait
    the same way.
- **Reconciliation** (`reconcile.py`, `frame_status`, `decide`). This applies when a
  turn needs reconciling, because it ended without the provider's own terminal event.
  - A cut message frame reads `failed`.
  - With no acceptance on stdout and no native record, `decide` returns unknown, and
    the message becomes `delivery-unknown` (D-14).
  - The base reached the same verdict: the frame ended `failed` with EPIPE once the
    reader closed, or `pending` if the guardian was killed mid-write.

**A residual, by design.** The bound covers the whole exchange. So a `close` whose
`written` fsync finishes more than a second after the child exits loses its
acknowledgement. The log still says `written`, and the runner handles the lost answer
as above.

## 2. The CPU scaling criterion's scope

`best_cpu_samples` returns the growth ratio between two input sizes. So it detects
super-linear growth (a quadratic regression), and it cannot see a constant-factor
slowdown.

I re-measured on this host (load average about 13), using `best_cpu_samples` at
64 and 256 KiB:

| Probe | Calls per input | Ratio | CPU per call at 64 KiB |
|---|---:|---:|---:|
| scrubber, `a_` | 1 | 4.967 | 10.5 ms |
| scrubber, `a_` | 2 | 4.999 | 23.0 ms |
| wrapper matcher, `-time ` | 1 | 4.191 | 19.7 ms |
| wrapper matcher, `-time ` | 2 | 4.604 | 39.0 ms |

Doubling the work doubled the CPU per call and left every ratio under 8, as the
reviewer found (4.36× and 4.17× in review r1).

The wrapper test's one absolute bound, its original 0.5 s at 64 KiB, is about 25 times
the measured 19.7 ms. So only a slowdown of that order trips it.

Changes:

- The scope is stated in `best_cpu_samples`' docstring, in the `tests/cpu_scaling.py`
  module docstring, and in the module docstrings of `tests/unit/test_sessions_handoff.py`
  and `tests/unit/test_sessions_handoff_excerpts.py`. The last one also says what its
  absolute bound catches.
- No absolute or constant-factor gate was added. Machine-speed budgets are the D-TM1
  flake class that #230 removed.
- The 8× ceiling is now one constant, `GROWTH_RATIO_MAX` in `tests/cpu_scaling.py`.
  All three scaling assertions use it, so the pin test pins the same criterion.
- `tests/unit/test_cpu_scaling.py` pins the scope at the scaling tests' sizes:
  - linear work passes the criterion, and so does the same work done twice;
  - quadratic work fails it.

The pin test's work loops on `itertools.repeat` and allocates nothing. A first draft
used `range` loops, which allocate ints above the small-int cache, and measured 5.32
for linear work in one sample. With the allocation-free loops, three rounds measured:

- linear: 3.81 to 5.00;
- doubled: 4.00 to 5.03;
- quadratic: 13.71 to 16.79.

This test is not a regression test. The behaviour it pins is the base's. At
`d72597b5c` it would fail only on importing `GROWTH_RATIO_MAX`.

## 3. Tests

### New tests

- `tests/unit/test_relay.py`
  - `test_stop_cuts_an_exchange_a_retained_stdin_reader_holds`: the reviewer's input.
    `stop` returns within the bound plus 1 s and logs exactly `CUT_LOG_LINE`, with no
    arguments. It leaves no thread or socket file. The frame is `failed` with
    `ECANCELED`, the client reads end of stream, and the reader holds a strict prefix
    of the line, then end of file. At `d72597b5c` it fails: "a retained stdin reader
    held stop past its bound".
  - `test_stop_still_answers_an_exchange_that_finishes_inside_its_bound`: the same
    frame, but the reader drains the pipe as `stop` begins. The acknowledgement is
    already in the client's buffer when `stop` returns. The frame is `written`,
    nothing is logged, and nothing is left. This test pins kept behaviour, so it
    passes at `d72597b5c`; the `cut-at-once` mutant fails it.
  - `test_an_acknowledged_frame_is_one_the_reader_can_read_whole`: a Hypothesis
    property over frame size (0 to 1.5 MiB) and the amount the reader drains before it
    stops reading. It checks invariants 1 to 4 above.
    - It runs 12 derandomized examples, with no database, plus 2 explicit ones: the
      reviewer's input, and a 10-byte frame.
    - `REPLY_TIMEOUT_S` is patched to 0.25 s, so examples are quick on the same code
      path.
    - In the final run, its events were 75% `written` and 25% `failed`.
    - At `d72597b5c` it fails on the reviewer's example.
- `tests/process/test_guardian_relay.py`
  - `test_a_retained_stdin_reader_cannot_hold_the_exit_receipt`: a real guardian. The
    provider starts a background process that inherits stdin and never reads it, then
    exits while a 2 MiB frame waits. `exit.json` appears with `rc` 0. The frame is
    `failed` with `ECANCELED`, the client gets `RelayError` with no acknowledgement,
    and the socket is gone. Cleanup kills the guardian's own group. At `d72597b5c` it
    fails: no `exit.json` after 10 s.
- `tests/unit/test_cpu_scaling.py`: the two pin tests above.

### Runs

Each run was one pytest process per hold of the shared lock, without xdist, on GIL
CPython 3.14.7 unless marked otherwise. The final runs are at `e9432a47b`; the report
commit changes no code.

| Hold | File | Passed | Failed | Skipped |
|---|---|---:|---:|---:|
| 1 | `tests/unit/test_relay.py` | 17 | 0 | 0 |
| 1 | `tests/unit/test_turn_runner.py` | 76 | 0 | 0 |
| 1 | `tests/unit/test_guardian.py` | 12 | 0 | 0 |
| 1 | `tests/process/test_guardian_relay.py` | 5 | 0 | 0 |
| 2 | `tests/unit/test_conversation_service.py` | 98 | 0 | 0 |
| 2 | `tests/e2e/test_conversations.py` | 29 | 0 | 15 |
| 3 | `tests/unit/test_cpu_scaling.py` | 2 | 0 | 0 |
| 3 | `tests/unit/test_sessions_handoff.py` | 117 | 0 | 0 |
| 3 | `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 0 | 0 |
| 4 | `tests/fake/test_state_contract.py` | 37 | 0 | 0 |
| 4 | `tests/unit/test_test_module_names.py` | 1 | 0 | 0 |
| 3.12.14 | `tests/unit/test_relay.py` | 17 | 0 | 0 |
| 3.12.14 | `tests/process/test_guardian_relay.py` | 5 | 0 | 0 |
| 3.12.14 | `tests/unit/test_cpu_scaling.py` | 2 | 0 | 0 |
| 3.12.14 | `tests/unit/test_guardian.py` | 12 | 0 | 0 |
| base | `tests/unit/test_relay.py` at `aab0dee5b` | 15 | 2 | 0 |
| base | `tests/process/test_guardian_relay.py` at `aab0dee5b` | 4 | 1 | 0 |

- **The base rows** are commit `aab0dee5b`: the code of `d72597b5c` plus the new
  tests. The three failures are the three regressions above.
- **The whole suite was collected** in its own hold: 15001 tests collected, no
  collection error.
- **Hold 3** covers every test file that imports `tests.cpu_scaling`, per
  `grep -rl cpu_scaling tests`.

### Mutations

Each mutant was built in a scratch copy of HEAD's `subfleet/` and `tests/`, and run in
its own hold over the whole of `tests/unit/test_relay.py` and
`tests/process/test_guardian_relay.py`. The worktree was never mutated.

| Mutant | Change | Tests that failed (all others passed) |
|---|---|---|
| `unbounded-barrier` (the brief's) | `with self._exchange_lock: pass` again | the cut test ("a retained stdin reader held stop past its bound"), the property test ("stop past its bound"), the process test |
| `socket-only-cut` | the write never checks the cut | the same three, the same way |
| `one-shut-rdwr` | one `SHUT_RDWR` call in the cut | the same three: the cut was acknowledged (`failed`, errno 89) |
| `cut-at-once` | `acquire(timeout=0)` | the kept-behaviour test, and `test_stop_finishes_the_exchange_already_read[close]` and `[status]` |
| `no-log-line` | the warning call removed | the cut test |
| `blocking-pipe` | `serve` leaves the pipe blocking | the cut test, the property test, the process test |

### Repetition

The six new timing-sensitive tests ran ten times each, in one hold, through a scratch
plugin that parametrizes every test ten times. All 60 passed, at load average 13 to 16.

| Test | Duration over 10 runs (with setup) | Its bound |
|---|---|---|
| The cut test | 1.20 to 1.40 s | 2.0 s |
| The property test | 2.14 to 2.84 s | |
| The process test | 1.32 to 1.59 s | 10 s wait for `exit.json` |
| The two pin tests | 1.54 to 3.74 s | |

### Not run, and limits

- **15 skips** in `tests/e2e/test_conversations.py`:
  - 10 person-only tests;
  - 5 marker tests that refuse inside a Subfleet job.

  The lane's attempt markers cause both. CI runs them.
- **Linux was not run.** The process test skips off macOS by design. The unit tests rely
  on `select.poll`, on separate `SHUT_RD` and `SHUT_WR` calls, and on a 2 MiB frame,
  which is over Linux's 1 MiB maximum pipe size. They ran on macOS only, under
  CPython 3.14 and 3.12.
- **The order inside `_cut_exchange`** (shutdown before the cut flag) has no
  deterministic test. The reverse order loses only a race against the writer's next
  look, at most 50 ms, so a mutant would pass most runs. It was not run as a mutant.
- **The whole suite** was collected but not run.
- **Hypothesis** was bounded: 12 derandomized examples, no database. No deep profile
  was run, and no `.hypothesis` folder was written.

## Follow-ups for the hub

- **The cut line has no durable home in production.** It goes to the guardian's stderr,
  which is `/dev/null` under the daemon. A durable home, such as a guardian log in the
  attempt directory or a field on `exit.json`, would need its own contract change. The
  `failed`/`ECANCELED` record already keeps the fact for a cut write.
