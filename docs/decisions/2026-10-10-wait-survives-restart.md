# A wait rides out a daemon restart (2026-10-10)

Max delegated Subfleet design calls. This record makes the calls for defect
D-WT1 on `release/217`. It amends the acceptance contract at C-15.4 and C-16.7.

## What happened

On 2026-10-10 the 2.1.11.4 install (09:06Z) and a `launchctl kickstart -k`
(09:08Z) each ended all five of the Subfleet hub's `subfleet wait`s with "the
daemon closed the connection without a response", while their jobs kept running.
`wait_jobs` (`subfleet/cli.py`) re-raised `ResponseLost`, and it re-raised
`DaemonUnavailable` unless a busy answer had come first. Sessions keep a
background `subfleet wait` armed because a finished run does not wake an idle
session, and a waiter that ends does wake it. So each install, each kickstart
after a policy edit, and each crash that launchd's KeepAlive restarts woke every
session holding a waiter at once.

## The calls

**1. Who rides out a restart.** Only callers that read: `subfleet wait`,
`run --wait`, `kill --wait`, a batch's wait (all through `wait_jobs`), and the
PostToolUse hook's wait (`hooks._wait_and_deliver`). Each sends the same `wait`
again and nothing else. A submit or kill that was already answered is never
sent again. `subfleet gate`'s poll is left alone: `gate.poll` may submit the
peer job, consume its verdict or merge (`GateService.poll`), so it is not a
read.

**2. What counts as an outage.** These count: a poll whose answer was lost
(`ResponseLost`, C-16.3); a connect that found the socket gone; and a connect
refused with no busy daemon behind it. A refused connect after a busy answer
stays C-16.7's backlog when `daemon.lock` names a living daemon, or when the
lock cannot say and `--timeout` bounds the loop. Any answer ends an outage,
busy or not.

**3. The window.** `RESTART_WINDOW_S` is 180 s, counted from the first poll of
the outage that failed. Each outage has its own window, so an install followed
by a kickstart two minutes later is ridden out twice. After the slowest stop
launchd allows (the plist's ExitTimeOut, `STOP_GRACE_S` + `STOP_BACKSTOP_S` =
40 s) and launchd's 10 s respawn throttle (`man launchd.plist`), 180 s leaves
130 s for a start. A daemon gone for good ends a wait with no `--timeout` within
the window, one poll's transport budget (75 s; this applies only to a daemon
that accepts and never answers), and one pause (at most 1 s).

**4. `--timeout` wins.** The reconnecting stays inside `--timeout`. When both
the window and the timeout have passed, the exit is 124, as before.

**5. Past the window, nothing new.** A lost answer exits 1 with its message, as
before. An absent daemon exits 69 with `subfleet daemon start`, as before. One
stderr line comes first, saying how long the daemon has not answered.

**6. A wait that never reached a daemon fails at once.** A wait has reached a
daemon when a poll was answered, busy or not, or was taken and then dropped.
`run --wait`, `kill --wait` and a batch's wait reach it at their submit or kill
(`reached=True`). A plain `subfleet wait` that finds no daemon at its first poll
still exits 69 at once (C-17.5). Applying the window there would make a wait on
a daemon nobody started take 180 s to say so. Under `--timeout` it would also
end in 124, as though a job were still running, when nothing about any job was
learned. What this costs: a waiter armed during the few seconds of a restart
gap still ends at once and wakes its session. That gap is far narrower than a
waiter's hours of life.

**7. An outage clears busy.** A busy answer from before an outage says nothing
about the daemon that comes back. Without this rule, a busy answer, then a lost
poll, then a connect refused while the next daemon's lock names a living process
would have been read as backlog. That path asks with no bound at all, so a
new daemon wedged before it listens could hold a wait with no `--timeout` for
ever.

**8. The bound on an unverifiable lock.** In a CLI wait with no `--timeout`, a
refused connect after a busy answer, with a lock that cannot say whether its
holder lives, is now an outage bounded by the window. That replaces the 60 s
`REFUSED_UNVERIFIED_MAX_S`, which is removed. With `--timeout` it is still busy,
bounded by the timeout (review of 1efa0ef, P3).

**9. The line, and its two wordings.** When an outage ends with an answer, the
CLI prints one stderr line. If `daemon.lock` names a different process (pid and
start time) than at the answer before, the line is `subfleet wait: daemon
restarted; still waiting`. Otherwise it is `subfleet wait: daemon answered
again; still waiting`. The brief asked for the first wording only. But a lost
answer or a refused connect can come from a daemon that never restarted, and a
line claiming a restart there would be false. Reading the lock is a file read
with no `ps`. The hook stays silent: its stderr is the notice.

## Invariants (tested in `tests/unit/test_wait_restart.py`)

- W1: the exit is 0 only when every requested job was answered terminal and
  succeeded. Once all were answered, the exit is theirs.
- W2: with `--timeout T`, the wait ends by T plus one poll budget. The tests
  check a tighter bound, T plus the under-0.2 s poll that an immediate answer's
  pause may follow. It exits 124 only once T has passed.
- W3: the wait never gives up inside the window. A daemon gone for good ends it
  within the window, one poll budget and one pause of the first poll it failed.
- W4: no job outside the requested set changes the exit, the polls or the
  time (C-17.3).
- W5: nothing is sent but `wait`, and only for requested jobs.
- W6: one line per outage that an answer ended. It says "restarted" exactly when
  the lock names another process.

Hypothesis checks each property over generated interleavings of busy, lost,
refused, gone and answered polls on a fake clock. Every run is compared with
`reference`, a separate statement of calls 2 to 7. That makes it a differential
test of the loop's decisions. The hook has its own property test. Fake-daemon
tests over a real socket cover:

- a poll closed unanswered;
- connects refused for 1.5 s;
- a daemon gone for good;
- one that drops every poll;
- `--timeout` shorter than the window;
- a daemon never reached;
- two outages, each with its own window;
- the hook.

`tests/e2e/test_wait_restart_e2e.py` stops a real `subfleetd` with SIGTERM (as
a kickstart does) or SIGKILL (a crash) while a real background `subfleet wait`
holds a poll, then starts the next daemon on the same state root.
