# Relay stop bound and CPU scaling scope (#230 review r1, two P3s)

2026-10-19. Built on `release/217` at `d72597b5c` as Subfleet job
`20261019-002120-relay-barrier-build`, for the branch `fix/relay-barrier-bound-217`.
Not pushed; the hub opens the PR.

Round 2, for #236's review r1 (P2: the cleanup after the cut was unbounded), is
section 4. Round 3, for review r2 (P2: no cleanup thread meant an unbounded cleanup;
P3: a stalled log write held the guardian's exit), is section 5.

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

## 4. Round 2: the whole stop is bounded (#236 review r1, P2)

2026-10-19, Subfleet job `20261019-014133-pr236-fix2`, on `fix/relay-barrier-bound-217` at
`9ec7c87fb`. Not pushed. `release/217` had not moved: the shared repository's
`origin/release/217` is still `d72597b5c` (the job fetches nothing), so there was no merge.

| Commit | What |
|---|---|
| `9ff9535eb` | test: a stalled journal record cannot hold the relay's stop |
| `5151cf5ec` | test: the journal-order property ends when stop overruns its bound |
| `1acabee7f` | fix: the whole stop is bounded; journal records never hold the pipe lock (C-26.4 amended) |
| `2faf4478f` | test: the journal-order property stalls more of its generated examples |
| `e4a1a78d8` | docs: say what stop's cleanup waits on, as measured |
| this commit | this section |

### The cause, as executed

The review's input: a 2 MiB frame, the child exited, a stdin reader kept and never read,
and the fsync of the cut frame's `failed` record held. A probe (`probe_stop.py`, in the
job's scratch, removed) ran it against the real `RelayServer`, with that fsync held by a
patched `os` and released at 4 s:

| Code | `stop` took | fsync still held when it returned |
|---|---|---|
| `9ec7c87fb`, 2 runs | 4.02 s, 4.15 s | no: it returned only once the fsync was released |
| this branch, 5 runs | 2.15 to 2.29 s | yes, every run |

At `9ec7c87fb`, `stop` cut the exchange at its bound and joined the thread for its 1 s.
It then called `_close_pipe`, which takes `_lock` (`relay.py:299` at `9ec7c87fb`).
`apply` held `_lock` from `relay.py:385` to its return. That included the `_append` of
the failure record (439) and that record's `os.fsync` (469). The guardian publishes
`exit.json` only after `stop` returns (`guardian.py:208-243`), so the receipt waited for
the fsync too. The new process test fails there at `9ec7c87fb`: "no exit.json while the
journal fsync was held".

### The lock split (`subfleet/relay.py`)

| Lock | Held by | For |
|---|---|---|
| `_exchange_lock` (unchanged) | the connection's reader | a read, its application and its reply |
| `_journal_lock` (new, 197) | `apply`, from its duplicate and gap checks to its return (429); `status` (405) | every `stdin.jsonl` record, `_records`, `_closed` |
| `_pipe_lock` (was `_lock`, 198) | `_write_line`, called under it (471); `_close_pipe` (340) | the pipe's descriptor: a write or the close |

- **No record holds the pipe lock.** Every `_append` call is inside the journal lock and
  outside the pipe lock. The pipe lock is taken only around the pipe write and inside
  `_close_pipe`. The close frame now closes stdin through `_close_pipe` (464).
- **The rename.** `_lock` became `_pipe_lock` because what it covers shrank. Two
  turn-runner tests named it to stand in for "`apply` holds it for the whole frame".
  They now hold `_journal_lock`, which is that lock now.
- **Lock order:** exchange, then journal, then pipe. Stop's cleanup takes only the pipe
  lock, and `status` takes only the journal lock. No path takes them the other way round.

**Why no reader can see a record in a new order.**

- **One writer.** `_append` is the only code that writes `stdin.jsonl`. `log_path` is
  opened only there (509); the constructor only reads the file through `read_log` (189).
- **Under one lock.** It is called at six sites, all in `apply` and all inside the
  journal lock, which `apply` holds for the whole frame. No `_append` call moved and no
  record's fields changed. So the records are still written one frame at a time: an
  intent, then its outcome, before the next frame's intent.
- **Durability is unchanged.** `_append` still opens with `O_APPEND`, writes one line,
  fsyncs and closes, for every record.
- **The new interleavings write no record.** The only new one is stop's cleanup closing
  the pipe in the middle of a frame:
  - Between a frame's intent and its pipe write: the write finds `_pipe` is `None`, raises
    `ECANCELED` (498), and the frame is logged `failed`. That is the same record sequence
    as a cut write.
  - Between the pipe write and its outcome: the whole line is already in the pipe, and the
    frame is logged `written`.
  - For a close frame: if the cleanup closed stdin first, `_close_pipe` finds nothing to
    close, and the frame is logged `written`. Stdin is closed either way.

The readers, each through `read_log`, which accepts only intent(n), outcome(n), intent(n+1)
and so on:

- **A relay resuming an attempt's log:** `RelayServer.__init__`.
- **The runner's handshake:** `_load_log` (`runner.py:644`), read after `status`. That
  `status` still takes the lock `apply` holds for the frame, so "the log read afterwards
  is final" (`_handshake`) still holds.
- **The runner's steer check:** `steer_written` (`runner.py:265`).
- **Reconciliation:** `frame_status` (`reconcile.py:243`). It also requires every line to
  be accounted for: one per pending frame, two per finished one.
- **Replay:** the service's replay (`service.py:2846`) goes through the runner's
  `_load_log`.

The sequence they read is the base's, so none can see a record in a new order.

**What does change is timing, past the bound.** `exit.json` can now come before the cut
frame's outcome is on disk:

- **A held write:** the record is not there yet, and a reader finds the frame `pending`.
- **A held fsync:** the record is there but not durable, and a reader finds it `failed`.

Neither changes a verdict:

- `decide` (`reconcile.py:119`) answers unknown for any frame state but `absent`, unless
  the frame was acknowledged or a native record was found.
- `_load_log` counts anything but `written` as not written.
- A guardian killed mid-write already left `pending`.

If the guardian exits with the record still held, the record ends as a guardian killed at
that moment would leave it.

### The whole bound, and the paths checked

`stop` returns within `REPLY_TIMEOUT_S` + `JOIN_TIMEOUT_S` + `CLEANUP_TIMEOUT_S`: 1 + 1 + 1 =
3 s. The steps, with what each can wait on:

| Step | Thread | Can wait on | Bound |
|---|---|---|---|
| set the stop flag, shut the read half | `stop` | nothing found: a shutdown took 6 to 7 µs with a send or a receive blocked on the socket (probe below) | none needed |
| the exchange barrier | `stop` | the exchange | `REPLY_TIMEOUT_S` (unchanged) |
| the cut: shut each half, set the cut flag | `stop` | nothing found (as above) | none needed |
| the relay thread join | `stop` | the relay thread: a record's write or fsync, a connection's close | `JOIN_TIMEOUT_S` (71), the 1 s that was a literal |
| close the listening socket | cleanup | nothing found: 9 µs with a connection waiting in the backlog, 18 µs with another thread in `accept` | `CLEANUP_TIMEOUT_S` (72), for the four cleanup steps together |
| close the pipe | cleanup | the pipe lock, held only by a pipe write; after a cut, a write lets go within `WRITE_POLL_S` | the same |
| log the cut line (moved here from the cut) | cleanup | stderr: `/dev/null` under the daemon (`daemon.py:7711-7714`), otherwise whatever started the guardian | the same |
| unlink the socket file | cleanup | the file system of the state root, which also holds the journal | the same |

**Why a cleanup thread.** An unlink and a log write can't be given a timeout on the calling
thread. One thread with one bounded wait covers all four steps.

**The pipe lock.** The cleanup's wait for the pipe lock has no timeout, but `stop`'s wait
for the cleanup does. If the lock can't be had in time, `stop` returns and leaves the
descriptor open, as the hub decided. The cleanup then closes it as soon as the write lets
go, or the guardian's exit closes it. The steps are ordered so that the one that touches
the disk comes last, and a failure in one step does not skip the next.

**The fallbacks.**

- **No thread to be had:** if the cleanup thread can't start (`RuntimeError`), `stop`
  cleans up inline, unbounded, as before.
- **A second `stop`:** it waits, within the bound, for the first one's cleanup.
- **Concurrent `stop` calls:** not supported. The guardian calls `stop` once.

**A side effect.** At `9ec7c87fb`, an `OSError` from the listening socket's close, or from
an unlink failing for a reason other than a missing file, came out of `stop`. In the
guardian, `stop` runs inside `except OSError` (`guardian.py:196-223`). Such an error would
have published `rc` 127 with a `spawn_error`, in place of the child's own status. The
cleanup now absorbs it.

**The probe** (`probe_sock.py`, in scratch, removed), on this host:

- `shutdown(SHUT_RD)` took 6 µs and `SHUT_WR` 3 µs, with another thread blocked in
  `sendall` into a full buffer; that sender then failed with EPIPE.
- With a thread blocked in `recv`, they took 7 µs and 1 µs, and the receiver got end of
  stream.
- Closing a listening socket with a connection in its backlog took 9 µs, and the waiting
  client read end of stream.
- Closing a listening socket while another thread waited in `accept` took 18 µs. That
  thread was not woken; it waited out its own timeout. The relay's accept loop looks at
  the stop flag every 0.25 s and stops on the closed socket.

**The guardian after `stop` (code read):**

- `guardian.py:199` makes the pipe with `os.pipe()`, whose descriptors are not inherited.
  The child gets only the read end (201), and the guardian then closes that (206).
- The write end goes to `relay.serve` (208). The guardian's own code never uses it again.
- `relay.stop()` runs in a `finally` (211). The exit receipt is published after it
  (236-243), then `linger()` runs (245-246), and `main` returns, so the process exits.

So once `stop` returns, only the relay's own threads can write to that descriptor:

- **The relay thread:** `_write_line` looks at the cut flag before every `os.write`, so
  at most one non-blocking write already past that look can land.
- **No new frame:** none can begin. The stop flag is set, the connection is shut, and the
  listening socket is closed.

`linger` (D-CEN1) can keep a disclaimed guardian alive after `exit.json` for as long as
its descendants live, and the retained reader can be one of them. That is why the cleanup
closes the descriptor as soon as the pipe lock is free, rather than leaving it to the exit.

### The contract

C-26.4 now states:

- the whole bound and its three parts;
- the cleanup thread and its steps;
- the journal lock, held for the whole frame and never under the pipe lock, and what that
  keeps: record order, fsyncs, and `status` waiting for a frame;
- that a write finding the pipe closed by the cleanup is logged `failed` with ECANCELED;
- what a reader finds when `exit.json` comes before the cut frame's outcome is durable.

The relay-thread join budget is no longer said to be `REPLY_TIMEOUT_S`; it is
`JOIN_TIMEOUT_S`, with the same 1 s value. The change-log entry is under "Changes in
version 3", above round 1's.

### New tests

- `tests/relay_stalls.py` (a helper, not a test module): the stall points.
  - `StallingOs` stands in for the `os` that `subfleet.relay` calls. It holds a pipe write,
    a journal record's write or fsync (chosen by the record's `kind` and `seq`), or the
    unlink.
  - `StallingServer` holds the listening socket's close, or each accepted connection's
    close.
  - `Gate` holds threads of the test's own process; `FileGate` holds a guardian in a child
    process.
- `tests/unit/test_relay.py`:
  - `test_a_stalled_failure_record_cannot_hold_stop[journal-fsync]` and `[journal-write]`:
    the review's input. While the record's fsync or write is held, `stop` returns within
    3 s plus a 0.5 s margin. By then it has closed the pipe (the reader holds a strict
    prefix, then end of file) and unlinked the socket, and the client reads end of stream.
    The journal shows the frame `failed` (held fsync) or `pending` (held write). Once
    released: `failed`, ECANCELED, and the cut is logged once. Both fail at `9ec7c87fb`:
    "a stalled journal-fsync (journal-write) of the failure record held stop past its
    bound".
  - `test_stop_returns_within_its_bound_whatever_stalls`: the property. It ranges over:
    - the stall point: none, a pipe write, a journal write, a journal fsync, the
      connection's close, the listening socket's close, the unlink;
    - the record held: the intent, the outcome, or a `failed` one;
    - frame size, and how much the reader drains;
    - when the stall ends: a fraction of the bound, or only after `stop` returns.

    What it checks:
    - `stop` returns within the bound;
    - the pipe is closed when it returns, unless a pipe write or the listening socket's
      close is held;
    - once released, nothing is left behind;
    - `written` holds exactly when the reader got the whole line, and otherwise the frame
      is `failed` (ECANCELED) with a strict prefix read;
    - only a `written` frame is acknowledged.

    It has 8 explicit examples, one per stall point and two for the review's record, plus
    16 derandomized generated ones. The bounds are patched to 0.25, 0.5 and 0.5 s. In the
    generated examples every stall point was held at least once. Their frames ended
    `written` 81% and `failed` 19% of the time. At `9ec7c87fb`, 7 of the 8 explicit
    examples fail ("stop past its bound"). The one that passes holds the connection's
    close, which the base's join already bounded.
  - `test_status_waits_for_the_frame_whose_outcome_is_being_logged`: while a frame's
    `written` record is being fsynced, `status` does not answer. When it does, it reports
    the frame applied, and the log shows `written`. This pins kept behaviour, so it passes
    at `9ec7c87fb`. The `status-under-pipe-lock` mutant fails it.
  - `test_the_journal_keeps_its_order_whatever_stop_does`: the order property. It runs any
    sequence of writes (some over the 64 KiB inline limit), closes, signals, resends and
    gaps, with `stop` closing the pipe while a chosen frame's intent or outcome fsync is
    held, or racing the frames. It checks:
    - every line is a record, in the order intent(n), outcome(n);
    - none is left pending, and every line is accounted for;
    - a new relay resumes at the same count;
    - the pipe holds exactly the written frames' lines, in order;
    - only written frames are acknowledged.

    It has 3 explicit examples (between intent and write, between write and outcome, a close
    frame) and up to 25 generated ones. Hypothesis's statistics counted 30 generated runs, 7
    of which stalled. It fails at `9ec7c87fb` on all
    3 explicit examples ("a held journal fsync held stop past its bound").
- `tests/process/test_guardian_relay.py`:
  - `test_a_stalled_journal_fsync_cannot_hold_the_exit_receipt`: a real guardian whose
    `subfleet.relay` has the `failed` record's fsync held (a `FileGate`). The provider
    exits while a 2 MiB frame waits and a background process it started keeps stdin
    unread. While the fsync is still held, the guardian publishes `exit.json` with `rc` 0.
    The journal shows `failed`, ECANCELED, the client gets `RelayError`, and the socket is
    gone. It fails at `9ec7c87fb`: "no exit.json while the journal fsync was held".
  - The existing retained-reader test now shares its setup (`_parked_guardian`,
    `_send_2mib`, `_end_parked_guardian`) with the new one. Its assertions are unchanged.
- `tests/unit/test_turn_runner.py`: the two tests that stand in for a frame in flight now
  hold `_journal_lock`.

### Runs

One pytest process per hold of the shared lock, in-process with a 1140 s alarm, no xdist.
Unless marked otherwise, on GIL CPython 3.14.7 (`/opt/homebrew/bin/python3.14`;
`sys._is_gil_enabled()` is True). `uv sync` first picked the free-threaded 3.14.7t, so the
venv was rebuilt on the GIL build before any hold.

- **The final code:** hold 1 ran at `e4a1a78d8`, the last commit that changes code or
  tests.
- **The other holds:** holds 2 to 5 ran at `1acabee7f`. Commits after it change only
  docstrings, and `test_relay.py`'s generator and one docstring. Hold 1 covers
  `test_relay.py`.

| Hold | File | Passed | Failed | Skipped |
|---|---|---:|---:|---:|
| 1 | `tests/unit/test_relay.py` | 22 | 0 | 0 |
| 1 | `tests/unit/test_turn_runner.py` | 76 | 0 | 0 |
| 1 | `tests/unit/test_guardian.py` | 12 | 0 | 0 |
| 1 | `tests/process/test_guardian_relay.py` | 6 | 0 | 0 |
| 2 | `tests/unit/test_conversation_service.py` | 98 | 0 | 0 |
| 2 | `tests/e2e/test_conversations.py` | 29 | 0 | 15 |
| 3 | `tests/unit/test_cpu_scaling.py` | 2 | 0 | 0 |
| 3 | `tests/unit/test_sessions_handoff.py` | 117 | 0 | 0 |
| 3 | `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 0 | 0 |
| 4 | `tests/fake/test_state_contract.py` | 37 | 0 | 0 |
| 4 | `tests/unit/test_test_module_names.py` | 1 | 0 | 0 |
| 5 | `tests/unit/test_turn_replay.py` | 33 | 0 | 0 |
| 5 | `tests/unit/test_title_review_regressions.py` | 4 | 0 | 0 |
| 5 | `tests/unit/test_conversation_titles.py` | 89 | 0 | 0 |
| 5 | `tests/unit/test_legacy_hold.py` | 56 | 0 | 0 |
| 5 | `tests/unit/test_steer_invariants.py` | 248 | 0 | 0 |
| 5 | `tests/fake/test_interactive_steer.py` | 13 | 0 | 0 |
| 5 | `tests/unit/test_conversation_reconcile.py` | 36 | 0 | 0 |
| 5 | `tests/unit/test_invariants_index.py` | 25 | 0 | 0 |
| 5 | `tests/unit/test_desktop_ledger.py` | 4 | 0 | 0 |
| 5 | `tests/unit/test_retention_turn_contract.py` | 2 | 0 | 0 |
| 5 | `tests/unit/test_live_store_guard_round8.py` | 100 | 0 | 0 |
| 3.12.14 | `tests/unit/test_relay.py` | 22 | 0 | 0 |
| 3.12.14 | `tests/process/test_guardian_relay.py` | 6 | 0 | 0 |
| 3.12.14 | `tests/unit/test_guardian.py` | 12 | 0 | 0 |
| 3.12.14 | `tests/unit/test_turn_runner.py` | 76 | 0 | 0 |

- **Holds 1 to 4** are the brief's.
- **Hold 5** is extra:
  - every other test file that builds a `RelayServer`;
  - every test that reads `docs/acceptance-contract.md`.
- **The 3.12.14 hold** ran at `1acabee7f`. A later 3.12.14 hold of the two properties at
  `1acabee7f`, which recorded the Hypothesis events above, also passed.
- **The whole suite was collected** in its own hold at `e4a1a78d8`: 15007 tests (round 1's
  15001 plus the 6 new), no collection error.

**At `9ec7c87fb`.** These ran at commit `9ff9535eb`'s tests on `9ec7c87fb`'s code, with
`JOIN_TIMEOUT_S` and `CLEANUP_TIMEOUT_S` added as names only. Nothing in that code reads
them; the new tests need the names to compute the bound. Two holds:

1. **`test_relay.py` and the process file.** Failed: both stalled-record regressions and
   the stall property (7 of 8 explicit examples). Passed: the status test and the 17
   existing relay tests.
   - The journal-order test hung there. Its `stop` ran on the test thread and waited for
     the very fsync the test released after it.
   - The hold's alarm ended that run at 1140 s, before the process file ran.
   - `5151cf5ec` runs that `stop` on a thread with its bound, as the other stall tests do.
2. **The order test, the status test and the process file**, at `5151cf5ec`'s tests.
   Failed: the order test (3 of 3 explicit examples) and the new process test. Passed:
   the status test and the 5 existing process tests.

### Mutations

Each mutant was built in a scratch copy of HEAD (`git archive`) and run in its own hold
over `tests/unit/test_relay.py` and `tests/process/test_guardian_relay.py`. The worktree was
never mutated, and the copies were removed.

| Mutant | Change | Tests that failed (all others passed) |
|---|---|---|
| `fsync-under-pipe-lock` (the brief's) | `_append` takes `_pipe_lock` around its write and fsync | both stalled-record regressions ("stop left the socket file": the cleanup could not take the pipe lock, so the unlink after it never ran); the stall property, 4 explicit examples ("the relay left the pipe's write end open"); the process test (the socket file was left) |
| `unbounded-cleanup-wait` | `cleanup.join()` without a timeout | the stall property, 3 explicit examples (`unlink` first: "stop past its bound") |
| `cleanup-inline` | `stop` runs the cleanup on its own thread, as before | the stall property, 3 explicit examples, the same way |
| `unbounded-join` | `self._thread.join()` without a timeout | both stalled-record regressions, the stall property (6 explicit examples) and the process test |
| `status-under-pipe-lock` | `status` takes `_pipe_lock` | the status test ("status answered before the frame's outcome was durable") |
| `no-closed-pipe-check` | `_write_line` no longer treats a closed pipe as cancelled | the order test, on the explicit example that closes the pipe between frame 2's intent and its write: the write raised `TypeError`, so frame 2 stayed `pending` (3 lines, 2 records) |

### Repetition

The 13 stop-related tests in `test_relay.py` and the process file ran ten times each in one
hold, through a scratch plugin that parametrizes every test ten times. All 130 passed, at
load average 10 to 16.

| Test | Duration over 10 runs |
|---|---|
| a stalled failure record, `[journal-fsync]` and `[journal-write]` | 2.23 to 2.39 s (its bound: 3.5 s) |
| the stall property | 17.29 to 18.50 s |
| the order property | 0.30 to 0.43 s |
| the status test | 0.37 to 0.45 s |
| the stalled-fsync process test | 2.48 to 4.45 s (10 s wait for `exit.json`) |
| round 1's cut test | 1.15 to 1.27 s |
| round 1's retained-reader process test | 1.31 to 1.54 s |

### Not run, and limits

- **Linux was not run.** The process tests skip off macOS by design, and the shutdown and
  close timings are this host's.
- **No real disk stall.** The stalls are patched calls. A stalled disk would also stall
  the guardian's own `exit.json`, whose `atomic_publish` fsyncs the file and its
  directory (`guardian.py:80-99`). That is outside `stop` and outside this fix.
- **15 skips** in `tests/e2e/test_conversations.py`, for the lane's attempt markers, as in
  round 1. CI runs them.
- **The whole suite** was collected, not run.
- **Hypothesis was bounded:** derandomized, no database, no deep profile. No
  `.hypothesis` folder was written.
- **Concurrent `stop` calls** are not supported, as above.

### Follow-ups for the hub

- Round 1's follow-up stands: the cut line has no durable home in production.
- A guardian on a stalled disk still waits in `exit.json`'s own fsync. Bounding that would
  be a change to C-8.1's atomic publish, not to the relay.

## 5. Round 3: no cleanup path outlasts the bound or holds the exit (#236 review r2)

2026-10-19, Subfleet job `20261019-040720-pr236-fix3`, on `fix/relay-barrier-bound-217` at
`ce226d226`. Not pushed. `release/217` had not moved: the shared repository's
`origin/release/217` is still `d72597b5c` (its last fetch, at 02:23), so there was no
merge.

| Commit | What |
|---|---|
| `169214984` | test: a cleanup thread that cannot start cannot hold the relay's stop |
| `28a67527f` | test: a stalled stderr write cannot hold the guardian's exit |
| `9ce6b787a` | fix: with no cleanup thread, stop runs only what cannot wait (C-26.4 amended) |
| `e2f963569` | fix: the cut line is a raw write of descriptor 2, not `logging` (C-26.4 amended) |
| this commit | this section |

### P2: the fallback for a cleanup thread that cannot start

**The cause, from the code at `ce226d226`.** When the cleanup thread's `start` raised
`RuntimeError`, `stop` called `_clean_up` on its own thread and returned
(`relay.py:292-296`). That ran all four steps there, unbounded: the listening socket's
close, the pipe's close (waiting for the pipe lock), the cut line through `logging`, and
the unlink. The review held the unlink until 4.2 s: `stop` took 4.35 s, and the guardian
still withheld `exit.json` at 3.51 s.

**The fix (`_clean_up_inline`, `relay.py:355`).** With no thread, `stop` runs only the
steps measured not to wait, then returns within the same bound:

- **The socket shutdowns** already ran before the cleanup: `stop`'s `SHUT_RD`, then the
  cut's two shutdowns, at 1 to 7 µs each (round 2's probe).
- **The listening socket's close:** 9 µs with a connection in its backlog, 18 µs with a
  thread in `accept` (round 2's probe).
- **The pipe's close, only if no write holds the pipe lock at that moment.**
  `_close_pipe(wait=False)` tries the lock without blocking. This goes beyond the hub's
  decision, which left the pipe close to the guardian's exit; see below.

Everything else is left:

- **The cut line** is not written. Its write can wait on stderr (P3), and the durable
  trace of a cut write is its `stdin.jsonl` entry, `failed` with ECANCELED.
- **The unlink** can wait on the disk. The socket file is left (see the check below).
- **A pipe a write still holds** closes at the guardian's exit, as the hub decided.

A later `stop` tries the thread again.

**Where I went beyond the hub: the pipe's close.** The close itself does not wait; only
the lock can, and it is held only by a pipe write. `probe_close.py` (scratch, removed)
timed `os.close` on the write end, five runs each, with the descriptor non-blocking as
`serve` leaves it:

| Pipe | `os.close` |
|---|---|
| empty | median 0.5 µs, max 1.5 µs |
| full, read end held in-process | median 0.6 µs, max 0.8 µs |
| a reader process blocked in `read` | median 4.4 µs, max 6.2 µs |
| full, read end held by a process that never reads | median 0.8 µs, max 1.1 µs |

Why close it rather than leave it to the exit:

- **Linger.** It keeps a disclaimed guardian alive after `exit.json` for as long as its
  descendants live (D-CEN1). A retained reader blocked on stdin is such a descendant.
- **The deadlock.** Left open, that reader never gets end of file, so the guardian waits
  for it until the kill protocol. That is round 2's reason for closing the pipe as soon as
  the lock is free.
- **The reviewer's input.** There the cut write gives up within `WRITE_POLL_S`, and the
  relay thread is joined before the fallback runs. So the lock is free and the pipe
  closes; the unit regression asserts it.

The change is one line, if the hub prefers the decision as written.

**The check: a leftover socket file is harmless.** From the code:

- **Nothing listens on it.** The fallback closes the listening socket, so a connect gets
  `ECONNREFUSED`; both regressions assert that. After a normal stop, the path is gone and
  a connect gets `ENOENT`.
  - `RelayClient._connect` raises `RelayError("relay unavailable: …")` for either
    (`relay.py:599-608`).
  - So a runner treats a leftover file as it treats a missing one. `_handshake` and
    `_transmit` call `_unacknowledged`, which retries at most `RESEND_MAX` (5) times, then
    calls `_relay_lost` (`runner.py:654-800`).
  - The runner also reads the guardian as gone from `exit.json` (`_process_gone`,
    `runner.py:396`).
- **A resume connects; it never binds.**
  - A daemon that restarts adopts a live turn through `_adopt`, which reads
    `start.json`'s `control_socket` and builds a `TurnRunner`, a client
    (`service.py:2858-2884`).
  - A replay of an ended attempt goes through the same `_adopt`, with `ended=True`.
  - Either meets the refused connect above.
- **The daemon's finalization removes it.** For a turn job, finalization calls
  `release_socket` (`daemon.py:8372`). That unlinks the attempt's relay path if it is a
  socket (`service.py:3196-3205`), as it already did for a guardian stopped by
  containment, which never unlinks its own.
- **A later relay at that path replaces it.** `RelayServer.bind` unlinks an existing
  socket and binds anew; it refuses only a non-socket (`relay.py:231-238`). The path is
  per attempt: `socket_path` digests the attempt id. A retry is a new attempt, so it gets
  another path.
- **Nothing else reads `run/`.** A search for the socket directory and `socket_path`
  found only the relay, the daemon's launch command (`daemon.py:7462`) and
  `release_socket`. The importer's `.sock` skip (`importer.py:2060`) covers v1 state, not
  `run/`.

### P3: the cut line held the guardian's exit

**The cause.** The guardian configures no `logging` handler, so the cleanup thread's
`_log.warning(CUT_LOG_LINE)` (`relay.py:333` at `ce226d226`) went to `logging.lastResort`,
a `_StderrHandler`. From 3.14.7's `logging/__init__.py`, read this session:

- **The handler lock.** `Handler.handle` holds the handler's lock across `emit` (lines
  1026-1027), and `emit` writes `sys.stderr`.
- **At exit.** `logging.shutdown` is registered with `atexit` (line 2266) and acquires
  every handler's lock in turn (line 2244).

So with the write held, the main thread published `exit.json`, then waited in
`logging.shutdown` until the write let go. That the thread is a daemon makes no
difference: the wait is on a lock, not a join.

**The fix (`_stderr_line`, `relay.py:79`).** The cleanup writes the line with
`os.write(2, …)`, looping on a short write and dropping an `OSError`. The relay no longer
imports `logging`. The line's text is unchanged; under the daemon, stderr is `/dev/null`
(C-5.1).

**The probe** (`probe_shutdown.py`, scratch, removed). A child's descriptor 2 is a pipe
filled until a one-byte write would wait. A daemon thread is held writing one line. The
main thread then returns. On 3.14.7, and the same on 3.12.14:

| The thread's write | The child |
|---|---|
| `os.write(2, …)` | exited 0 after 0.83 s (3.12: 0.78 s) |
| `os.write(2, …)`, `faulthandler` enabled | exited 0 after 0.79 s (3.12: 0.78 s) |
| `sys.stderr.write` | alive at 6 s; exited 0 once the pipe was read |
| `logging` (`lastResort`) | alive at 6 s; exited 0 once read |
| `logging`, `faulthandler` enabled | alive at 6 s; exited 0 once read |
| `logging`, `logging.shutdown` unregistered from `atexit` | alive at 6 s; once read, rc −6: "Fatal Python error: _enter_buffered_busy: could not acquire lock for <_io.BufferedWriter name='<stderr>'> at interpreter shutdown, possibly due to daemon threads" |

The last row shows a second lock. The exit's flush of the standard streams waits on
`sys.stderr`'s buffer lock, which the `logging` write also held. That wait has no bound,
and once the lock is let go the exit aborts.

### The lock sweep: what the cleanup thread can hold at shutdown

The cleanup thread runs `_clean_up` (`relay.py:333`): `self._server.close()`,
`_close_pipe()`, `_stderr_line()` and `os.unlink()`.

| Lock | Taken at shutdown by | Held by the cleanup at `ce226d226` | Held now |
|---|---|---|---|
| each `logging` handler's lock | `logging.shutdown`, at exit (line 2244) | yes: `lastResort`'s, across the stderr write | never: no `logging` call |
| `logging._lock`, the module lock | not by `shutdown`; `isEnabledFor` takes it on a cache miss (line 1779) | only during that miss, before the write | never |
| `sys.stderr`'s buffer lock | the exit's flush of the standard streams (probe, last row) | yes, across the write, through `lastResort` | never: `os.write` bypasses `sys.stderr` |
| `sys.stdout`'s buffer lock | the same flush | never: nothing writes stdout | never |
| `faulthandler` | the cleanup never calls it, and neither does the guardian or the relay (searched) | no | no |
| the import lock | imports | no: the cleanup imports nothing | no |
| `_pipe_lock` | nothing at shutdown: after `stop` the guardian publishes and lingers, and never touches the relay | while closing the pipe | while closing the pipe |
| `_exchange_lock`, `_journal_lock` | nothing at shutdown | never: the cleanup doesn't take them | never |

Two more facts the table rests on:

- **`threading._shutdown`** waits only for non-daemon threads (3.14.7's `threading.py:1582-1583`,
  read this session), and the cleanup thread is a daemon.
- **A blocked raw write holds nothing the exit needs.** The probe shows a daemon thread
  blocked in `os.write` doesn't stop the main thread's exit, with or without `faulthandler`
  enabled. I did not read `faulthandler`'s C source. The claim rests on that probe and on
  the cleanup never calling it.

### The contract

C-26.4 now states:

- **The fallback:** if the cleanup thread can't start, shutdown runs only the steps
  measured not to wait (the listening socket's close, and the pipe's close when no write
  holds the pipe lock) and returns. It writes no cut line. A pipe that a write still holds
  is closed by the guardian's exit.
- **The leftover socket:** nothing listens on it, the daemon's finalization unlinks it,
  and a later bind replaces it.
- **The cut line:** it is written to standard error by a raw write of descriptor 2, not
  through `logging` or `sys.stderr`, so a held write can't hold the guardian's exit. The
  cleanup takes no lock but the pipe lock.
- **The bound:** it now holds whatever the host's thread limit does too.

Two change-log entries are at the top of "Changes in version 3", one per finding.

### New and changed tests

- `tests/relay_stalls.py` (helpers):
  - `StallingOs` gains `stderr-write`, chosen by descriptor 2 whatever it is. Writes to
    descriptor 2 are no longer matched as journal records or pipe writes.
  - `refusing_start` wraps `threading.Thread.start` so that only the thread named
    `subfleet-relay-stop` fails, with `RuntimeError("can't start new thread")`.
- `tests/unit/test_relay.py`:
  - `test_stop_keeps_its_bound_when_its_cleanup_thread_cannot_start` (P2). The setup is
    the review's: a 2 MiB frame, the child exited, the read end held, the thread start
    refused, the unlink held. Within the 3.5 s bound, `stop` returns without reaching the
    unlink. A connect is refused, the pipe is closed (a strict prefix, then end of file)
    and the socket file is left. No cut line is written, the journal shows `failed` with
    ECANCELED, and the client reads end of stream. At `ce226d226`: "with no cleanup
    thread, stop ran past its bound".
  - `test_a_held_cut_line_holds_no_lock_the_guardians_exit_takes` (P3). With the cut
    line's raw write held, `stop` keeps its bound and closes the pipe. No lock interpreter
    shutdown takes is held: `logging._lock`, every live handler's lock, and the standard
    streams' buffers (a flush of `sys.__stdout__` and `sys.__stderr__` finishes). Once
    released, the line lands once, exactly, and the socket file is unlinked. At
    `ce226d226`: "the cut line was not a raw write of descriptor 2".
  - Round 1's cut test and round 2's stalled-record tests now read the line from
    descriptor 2 (`capfd`), exactly once. The kept-behaviour test asserts descriptor 2
    stays empty. The first three fail at `ce226d226`, where the line went through
    `logging`; the kept-behaviour test passes there.
  - The stall property gains an explicit `stderr-write` example (2 MiB, nothing drained),
    and its generated examples draw the new point too.
- `tests/process/test_guardian_relay.py`:
  - `test_a_cleanup_thread_that_cannot_start_cannot_hold_the_exit_receipt` (P2). A real
    guardian, launched by a `-c` script that refuses the cleanup thread's start and holds
    the unlink at a `FileGate`. The provider exits 7. Within the 3 s bound plus 2 s of the
    provider's exit, the guardian publishes `exit.json` with rc 7 and never reaches the
    unlink. The journal shows `failed` with ECANCELED, the client gets `RelayError`, and
    the socket file is left with nothing listening. At `ce226d226`: "no exit.json within
    stop's bound when its cleanup thread could not start".
  - `test_a_stalled_stderr_cannot_hold_the_guardians_exit` (P3). This is a real-process
    test of a **real, unpatched** guardian; no probe stands in for it. Its stderr is a
    pipe the test fills once `start.json` appears (until a one-byte write would wait) and
    never reads, so the stall is the kernel's. It publishes rc 7 and exits 7 within 5 s,
    with the write still held: the pipe holds only the test's fill. At `ce226d226`: "the
    guardian published rc 7 but did not exit while its stderr write was held".
  - `PARK` now exits with the status the test names. `_parked_guardian` takes `rc` and the
    guardian's own `stderr`, and the existing tests keep their defaults.

### Runs

One pytest process per hold of the shared lock, each run in-process under a 1140 s alarm,
with no xdist. Each used the worktree's `.venv/bin/python -m pytest`, not `uv run`: a
note from earlier rounds records `uv run` failing on the uv cache lock after waiting out
the suite lock. The interpreter was GIL CPython 3.14.7 (`sys._is_gil_enabled()` is True)
unless marked otherwise, with `PYTHONPATH` unset. Holds 2 to 9 ran at `e2f963569`, the
last commit that changes code or tests.

**At `ce226d226`'s code (hold 1).** It ran on a `git archive` export of `28a67527f`: both
test commits on the unfixed code. `PYTHONPATH` named the export, and its imports were
checked to resolve there. Result: 7 failed, 25 passed.

- **Failed:** the four regressions, with the messages above, and the three tests that now
  read the cut line from descriptor 2.
- **Passed:** the kept-behaviour test, the stall property (its new example included),
  round 2's status and order tests, and the six existing process tests.

**The fixed code:**

| Hold | File | Passed | Failed | Skipped |
|---|---|---:|---:|---:|
| 2 | `tests/unit/test_relay.py` | 24 | 0 | 0 |
| 2 | `tests/unit/test_turn_runner.py` | 76 | 0 | 0 |
| 2 | `tests/unit/test_guardian.py` | 12 | 0 | 0 |
| 2 | `tests/process/test_guardian_relay.py` | 8 | 0 | 0 |
| 3 | `tests/unit/test_conversation_service.py` | 98 | 0 | 0 |
| 3 | `tests/e2e/test_conversations.py` | 29 | 0 | 15 |
| 4 | `tests/fake/test_state_contract.py` | 37 | 0 | 0 |
| 4 | `tests/unit/test_test_module_names.py` | 1 | 0 | 0 |
| 5 (3.12.14) | `tests/unit/test_relay.py` | 24 | 0 | 0 |
| 5 (3.12.14) | `tests/unit/test_turn_runner.py` | 76 | 0 | 0 |
| 5 (3.12.14) | `tests/unit/test_guardian.py` | 12 | 0 | 0 |
| 5 (3.12.14) | `tests/process/test_guardian_relay.py` | 8 | 0 | 0 |
| 7 | 14 more files (below) | 687 | 0 | 0 |

- **Holds 2 to 4** are the brief's.
- **The 15 e2e skips** are the lane's attempt markers: 10 person-only requests and 5 that
  the markers refuse first. CI runs them.
- **Hold 7** covers every other test file that builds a `RelayServer`, imports the relay,
  or reads the contract:
  - `tests/fake/test_interactive_steer.py` 13; `test_approval_resolution` 33;
  - `test_conversation_fifo_reads` 26; `test_conversation_reconcile` 36;
  - `test_conversation_state_reads` 18; `test_conversation_titles` 89;
  - `test_desktop_ledger` 4; `test_invariants_index` 25; `test_legacy_hold` 56;
  - `test_live_store_guard_round8` 100; `test_retention_turn_contract` 2;
  - `test_steer_invariants` 248; `test_title_review_regressions` 4; `test_turn_replay` 33.
  (The bare names are in `tests/unit/`.)
- **Hold 8** reran the three Hypothesis properties with statistics:
  - **The stall property:** its 16 generated examples drew `stderr-write` twice. Both
    frames finished inside the bound, so neither reached it (no cut, no cut line). The
    explicit example and the P3 unit regression hold it.
  - **The other two:** the read-whole property was `written` 75% and `failed` 25%. The
    order property stalled 23% of its examples.
- **Hold 9:** the whole suite collected 15011 tests (round 2's 15007 plus these 4), with no
  collection error.

### Mutations

Each mutant was built in a `git archive` copy of `e2f963569` in scratch and run in its own
hold over `tests/unit/test_relay.py` and `tests/process/test_guardian_relay.py`. The
worktree was never mutated, and the copies were removed.

| Mutant | Change | Tests that failed (all others passed) |
|---|---|---|
| A: the inline fallback restored | `except RuntimeError:` runs `self._clean_up()` again | `test_stop_keeps_its_bound_when_its_cleanup_thread_cannot_start` ("with no cleanup thread, stop ran past its bound"); `test_a_cleanup_thread_that_cannot_start_cannot_hold_the_exit_receipt` ("no exit.json within stop's bound …") |
| B: the cut line through `logging` again | `_stderr_line(CUT_LOG_LINE)` becomes `logging.getLogger(__name__).warning(CUT_LOG_LINE)` | `test_a_held_cut_line_holds_no_lock_the_guardians_exit_takes` ("the cut line was not a raw write of descriptor 2"); `test_a_stalled_stderr_cannot_hold_the_guardians_exit` ("… did not exit while its stderr write was held"); round 1's cut test and both stalled-record tests (descriptor 2 empty) |

### Repetition

Hold 6 ran the 10 timing-sensitive stop tests 10 times each, through a scratch plugin:
the 4 regressions, the 3 changed cut-line tests, the kept-behaviour test and round 1 and
2's process tests. All 100 passed, at load average about 8 to 10.

| Test | Duration over 10 runs |
|---|---|
| no cleanup thread, unit | 1.14 to 1.29 s (bound 3.5 s) |
| held cut line, unit | 2.29 to 2.52 s (`stop` within 3.5 s, then release and settle) |
| no cleanup thread, process | 1.28 to 1.52 s for the whole test |
| stalled stderr, process | 2.42 to 3.39 s for the whole test |
| round 2's stalled record, `[journal-fsync]` and `[journal-write]` | 2.24 to 3.02 s |
| round 1's cut test | 1.08 to 1.32 s |
| the kept-behaviour test | 0.09 to 0.16 s |
| round 2's stalled-fsync process test | 2.28 to 2.64 s |
| round 1's retained-reader process test | 1.30 to 1.52 s |

**A probe, before the tests.** `probe_fix.py` (scratch, removed) ran both of the review's
inputs once against the fixed code, outside the lock:

- **No cleanup thread**, the unlink held until 4.2 s: `stop` took 1.16 s, against the
  review's 4.35 s. It closed the pipe and never reached the unlink.
- **A real guardian with a full stderr pipe:** it published rc 7 at 2.24 s after the
  provider's exit, and exited 7 at the same moment.

### Not run, and limits

- **Linux was not run.** The process tests skip off macOS by design, and the close and
  shutdown timings are this host's.
- **Real thread exhaustion was not induced.** The P2 tests make the cleanup thread's start
  raise the `RuntimeError` CPython raises when a thread can't be had, and leave other
  threads alone. They do not exhaust the host's threads.
- **The P3 stall is real, but only one kind.** It is a full pipe on descriptor 2, the
  kernel's own wait. Other stderr targets (a tty, a socket) were not tried.
- **CI wasn't run, and the whole suite was only collected.**
- **Hypothesis was bounded:** derandomized, no database, no deep profile. No
  `.hypothesis` folder was left in the worktree.
- **`faulthandler`'s C source was not read.** See the sweep.

### Follow-ups for the hub

- Round 1's follow-up stands: the cut line has no durable home in production. Under the
  daemon, stderr is `/dev/null`, so P3 needs a guardian started some other way, with
  stderr on a pipe nobody reads.
- Round 2's stands too: a guardian on a stalled disk still waits in `exit.json`'s own
  fsync.
