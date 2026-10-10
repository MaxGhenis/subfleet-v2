# A wait rides out a daemon restart (2026-10-10)

Max delegated Subfleet design calls. This record makes the calls for defect
D-WT1 on `release/217`, and the client side of D-DS1. It amends the acceptance
contract at C-15.4 and C-16.7.

## What happened

On 2026-10-10 the 2.1.11.4 install (09:06Z) and a `launchctl kickstart -k`
(09:08Z) each ended all five of the Subfleet hub's `subfleet wait`s with "the
daemon closed the connection without a response", while their jobs kept running
(D-WT1). From 22:28Z to 22:32Z the machine ran at load1 about 465 on 18 CPUs.
The daemon, starved of CPU at about 100%, took polls and answered none within
the 75 s budget, so waits ended with "no response from the daemon within 75s".
For a while `daemon.sock` also refused connects. The outage lasted about four
minutes (D-DS1, `~/reviews/subfleet-hub/defects.md`).

`wait_jobs` (`subfleet/cli.py`) re-raised `ResponseLost`, and it re-raised
`DaemonUnavailable` unless a busy answer had come first. Sessions keep a
background `subfleet wait` armed because a finished run does not wake an idle
session, and a waiter that ends does wake it. So each install, each kickstart
after a policy edit, each crash that launchd's KeepAlive restarts, and each
spell of starvation woke every session holding a waiter at once.

## The calls

**1. Who rides out an outage.** Only callers that read: `subfleet wait`,
`run --wait`, `kill --wait`, a batch's wait (all through `wait_jobs`), and the
PostToolUse hook's wait (`hooks._wait_and_deliver`). Each sends the same `wait`
again and nothing else. A submit or kill that was already answered is never
sent again. `subfleet gate`'s poll is left alone: `gate.poll` may submit the
peer job, consume its verdict or merge (`GateService.poll`), so it is not a
read. A separate session is making the gate's own call.

**2. What counts as an outage.** These count:

- A poll whose answer was lost (`ResponseLost`, C-16.3). That covers an
  answer dropped, as by a stopping or crashing daemon; one that came after the
  poll's transport budget (its deadline plus `WAIT_TRANSPORT_SLACK_S`, 15 s), as
  from a starved daemon; and one cut short or undecodable, as a daemon that dies
  part way through its reply leaves it. A reply from something else listening
  on the socket is lost the same way and is also asked again. It ends the wait
  after the window, with exit 1 as before.
- A connect that found the socket gone.
- A connect refused with no busy daemon behind it.

Any answer ends an outage, busy or not.

**3. A poll cut by `--timeout` is not an outage.** With less than a second of
`--timeout` left, the last poll asks the daemon to hold one second and has less
than that to read the answer, so that answer is always lost. That loss is the
wait's own end: it exits 124 and says nothing about the daemon. Review r1 of
2c1a0f0e found this as P2: every `--timeout` wait that ran out printed a line
saying the daemon had stopped answering.

**4. The window: 300 s.** `RESTART_WINDOW_S` is counted from when the first
failed poll of the outage failed (not from when it was sent), and each outage
has its own window. The brief suggested 180 s. That covers a restart: after the
slowest stop launchd allows (the plist's ExitTimeOut, `STOP_GRACE_S` +
`STOP_BACKSTOP_S` = 40 s) and launchd's 10 s respawn throttle
(`man launchd.plist`), 180 s leaves 130 s for a start. It does not cover D-DS1's
four minutes of starvation. Take a poll lost at 75 s, then a retry sent at 230 s
while the daemon is still starved: it fails at 305 s, 230 s after the first
failure, which is past 180 s but inside 300 s. So the window is 300 s, which
leaves 250 s for a start after a restart. The cost is that a wait whose daemon
is gone for good ends at most two minutes later than it would with 180 s.

**5. The bounds.**

- A daemon gone for good (socket gone or refused) ends a wait with no
  `--timeout` within the window plus one pause (at most 1 s) of the first
  failure.
- A daemon that takes polls and never answers ends it within the window, plus
  one poll's transport budget (75 s), plus one pause.
- `--timeout` bounds everything, and when both the window and the timeout have
  passed, the exit is 124, as before.

**6. Past the window, nothing new.** A lost answer exits 1 with its message,
and an absent daemon exits 69 with `subfleet daemon start`, both as before.
First comes one stderr line saying how long the daemon has not answered,
counted from its last answer.

**7. A wait that never reached a daemon fails at once.** A wait has reached a
daemon when a poll was answered, busy or not, or was taken and then dropped.
`run --wait`, `kill --wait` and a batch's wait reach it at their submit or kill
(`reached=True`). A plain `subfleet wait` that finds no daemon at its first poll
still exits 69 at once (C-17.5).

Applying the window there would have two costs. A wait on a daemon nobody
started would take 300 s to say so. Under `--timeout` it would end in 124, as
though a job were still running, when nothing about any job was learned.

What the rule costs instead: a waiter armed during the seconds of a restart gap
still ends at once and wakes its session. That gap is far narrower than a
waiter's hours of life.

**8. Backlog only for the daemon that answered busy.** C-16.7 reads a connect
refused right after a busy answer as that busy daemon's full listen backlog,
and asks again with no bound. That reading now holds only while `daemon.lock`
still names the process that answered busy.

An outage clears the busy answer. A lock that a new process wrote says the busy
daemon is gone, so a refusal then is an outage and the window bounds it.

The case this covers is a busy answer, then a crash, then launchd's next daemon
writing the lock and wedging before it listens. That sequence would otherwise
have held a wait with no `--timeout` for ever. Review r1 of 2c1a0f0e found it
as P3, beyond the "lost, then refused" order of 2c1a0f0e's rule.

The hook keeps C-16.7's plain rule (its deadline bounds it).

**9. The unverifiable lock.** In a CLI wait with no `--timeout`, a refused
connect after a busy answer, from a lock that cannot say whether its holder
lives, is now an outage bounded by the window. This replaces the 60 s
`REFUSED_UNVERIFIED_MAX_S`, which is removed. With `--timeout` it is still
busy, bounded by the timeout (review of 1efa0ef, P3).

**10. The line, and its two wordings.** When an outage ends with an answer, the
CLI prints one stderr line:

- `subfleet wait: daemon restarted; still waiting` when `daemon.lock` names a
  different process (pid and start time) than at the answer before;
- `subfleet wait: daemon answered again; still waiting` otherwise.

The lock is read at every answer, which is a file read with no `ps`.

The brief asked for the first wording only. But a lost answer or a refused
connect can come from a daemon that never restarted, such as a starved one, and
a line claiming a restart there would be false.

The hook stays silent, because its stderr is the notice.

## Invariants (tested in `tests/unit/test_wait_restart.py`)

- **W1:** the exit is 0 only when every requested job was answered terminal and
  succeeded. Once all were answered, the exit is theirs.
- **W2:** with `--timeout T`, the wait ends by T plus one poll budget. The tests
  check a tighter bound: T plus the under-0.2 s poll that an immediate answer's
  pause may follow. The wait exits 124 only once T has passed.
- **W3:** the wait stops at the first poll the rule stops it at, so never inside
  the window. A daemon gone for good ends it within the window, one poll budget
  and one pause of when the outage's first failed poll failed.
- **W4:** no job outside the requested set changes the exit, the polls or the
  time (C-17.3).
- **W5:** nothing is sent but `wait`, and only for requested jobs.
- **W6:** there is one line per outage that an answer ended. It says
  "restarted" exactly when the lock names another process than at the answer
  before. Every other line about the daemon is one the rule calls for.

Hypothesis generates interleavings of these polls on a fake clock:

- busy, lost, refused and gone polls;
- a refused connect after a new process wrote the lock;
- an answer from a new process;
- answers.

Every run is compared with `reference`, a three-pass restatement of calls 2 to
9. It classifies each poll, groups the failures into outages, and finds where
the wait must stop. It also predicts every stderr line about the daemon, which
is the check the P2 above slipped past. The hook has its own property test.

Fake-daemon tests over a real socket cover:

- a poll closed unanswered;
- connects refused for 1.5 s;
- the same daemon answering again;
- two outages, each with its own window;
- a daemon gone for good;
- one that drops every poll;
- one that answers every poll too late, then recovers (D-DS1, with the budget
  scaled down);
- `--timeout` shorter than the window;
- `--timeout` running out on a healthy daemon;
- a daemon never reached;
- `run --wait` and `kill --wait`'s `reached`;
- the hook, both riding a restart out and giving up.

`tests/e2e/test_wait_restart_e2e.py` stops a real `subfleetd` while a real
background `subfleet wait` holds a poll, then starts the next daemon on the same
state root. It stops it two ways: with SIGTERM, as a kickstart does, and with
SIGKILL, as a crash does.
