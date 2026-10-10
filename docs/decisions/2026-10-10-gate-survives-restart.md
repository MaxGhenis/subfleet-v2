# A gate rides out a daemon restart (2026-10-10)

Max delegated Subfleet design calls. This record makes the calls for the gate
half of defect D-WT1 on `release/217`. That is the half
`docs/decisions/2026-10-10-wait-survives-restart.md` left alone, because
`gate.poll` is not a read. It amends the acceptance contract at C-15.4, C-16.7
and C-17.1.

## What was wrong

`subfleet gate pr|plan|continue` (`subfleet/gate/cli.py`, `run()`) sends one
`gate.start` or `gate.continue`. It then loops `gate.poll`, each one
`Client.call` with a 180 s deadline (`POLL_TIMEOUT_S`) that asks again after busy
answers. Any `DaemonUnavailable`, `ResponseLost` or `OSError` there ended it with
exit 1 ("subfleet gate: <error>"), while the gate stayed in the daemon. So an
install, a `launchctl kickstart -k`, or a crash under KeepAlive during a gate
ended its caller's `subfleet gate` and woke the session. That session then had to
know to resume, and no command did. `subfleet gate continue <id>` answers 4 at
once while the peer review is still running ("another peer review is already
running; no duplicate launched"). On an agreed `proceed` gate it asks for
`--main-approve` and a fresh round.

Reading the code turned up three more gaps:

- **A poll that reconnects still failed.** A daemon that has just started answers
  every gate op with `{"code": 1, "message": "daemon recovery is in progress; retry
  the gate command"}` (`service.dispatch`). It does so until
  `_recover_then_start_timers`, scheduled on the control loop's first tick, sets
  `_recovery_complete`. So a poll loop that reconnected would still end with exit
  1 at its first answer from the new daemon.
- **A death between a round's job and its journal entry.** A daemon that dies
  after `Daemon.submit` commits a round's job, and before `_save(state,
  "round-submitted")`, leaves the round with no `peer_run_id`. The next poll
  submits again under the round's own request id. C-6.2 answers that with the job
  it holds only when the payload digest matches, and the digest includes the
  policy hash (`ids.payload_digest(..., policy_hash=...)`). After a kickstart on an
  edited policy, which is the 09:08Z event, the second submit is refused as "a
  different payload". The round then ended blocked with its peer job left
  running.
- **A gate left `ready`.** A start that journaled its gate (`created`) and died
  before reserving a round leaves a gate that no poll advances. A poll answers it
  with no code, so a loop on it would poll for ever.

## The calls

**1. Is a `gate.poll` sent again safe? Yes.** The request carries nothing but the
gate id. `GateService.poll` loads the newest journaled state (`events`, kind
`gate.state`) under the gate's lock and takes the step that state calls for. No
step reads anything from the request. So a poll sent again is the poll the loop
would have sent next. The daemon may have run the poll whose answer was lost in
one of three ways:

- **In full.** The state moved on, and the poll sent again takes the next step.
- **In part.** It died between two durable writes, and the poll sent again
  carries on from the later one.
- **Not at all.** It was never read, or it was queued and then cancelled when the
  daemon stopped (`close()` shuts the pools down with `cancel_futures=True`).

A poll that is still running when its client leaves keeps running, because
`gate.poll` is not one of C-16.7's read-only ops. A poll sent again to the same
daemon waits on the gate's lock behind it and then reads what it left.

Every step that acts is fenced by something it leaves in the store:

- **A round's peer job.** `_submit` runs only while the round names no
  `peer_run_id`. It submits under `gate:<id>:round:<n>:<token>`, a request id that
  only this round's record carries. With call 6 it adopts a job that already
  carries that id.
- **A verdict.** `_consume` checks and releases the round lease in the same store
  transaction that journals `round-finished`. `Store.transaction` nests
  `add_event` as a savepoint. Either both are durable, and the gate has moved past
  that round, or neither is. In the second case the poll sent again consumes the
  same deliverable (its sha256 is checked) once.
- **A certificate.** It is issued only when the state holds none, and journaled
  before completion.
- **A merge.** `MergeActions.execute` looks up the operation key in the
  transaction that would add the action. An existing action is answered, or
  reconciled when unknown, and never merged again. Only the claimed holder runs
  `gh pr merge`. Startup recovery (`MergeActions.recover`) marks an orphaned
  `executing` action `unknown`. `reconcile` settles it from GitHub with an
  additive event, never a second submission (C-19.1, C-23.13).
- **A gate that has answered a code.** It only reads. `completed`,
  `changes_requested`, `blocked`, `action_failed` and `action_queued` are
  projected and answered. `action_attempting` (another holder's merge in flight)
  reconciles on each poll and journals what it read. It makes no job, action or
  merge.

So one poll never advances a gate twice. A poll sent again also gains nothing a
caller could not already do: `gate continue` reaches the same steps through
`poll`.

**2. What is asked again, and what ends an outage.** The poll loop asks again
after a lost answer (`ResponseLost`, of any cause: dropped, later than the poll's
180 s budget as from a daemon starved of CPU (D-DS1), or cut short), after a
connect that found the
socket gone or refused (`DaemonUnavailable`), and after a recovering answer. It
waits C-16.7's pause (`busy_pause`) before each one. Busy keeps its own rule
(review of the descriptor hotfix, F8): `Client.call` asks again inside half of
`POLL_TIMEOUT_S`, and busy past that exits 1. Any other answer ends the outage,
and an answer with a code ends the loop, so no poll follows a code. A recovering
answer does not end an outage. A daemon stuck in recovery is then bounded by the
window instead of holding the loop for ever.

**3. The window is `RESTART_WINDOW_S` (300 s) per outage.** It runs from when
the first failed poll of the outage failed, and the code reuses `RestartWindow`,
for the reasons in the wait record (300 s since its review r1 and D-DS1, a
four-minute starvation). A gate has no `--timeout`. A daemon gone for good ends
the loop within the window, one poll's budget and one pause:

- about 301 s for a socket that is gone or refused;
- at most 180 + 300 + 180 + 1 s for a daemon that accepts the connection and
  never answers (its first poll fails only once its 180 s budget has passed).

**4. A start or continue answered "recovering" is sent again.** `dispatch`
answers before it reads the arguments, so nothing was done. The answer now
carries `recovering: true`, and keeps code 1 and its message for older clients.
`gate.start` and `gate.continue` answered that way are sent again, within a
window of their own. A lost start or continue is never sent again. A start makes
a gate. A continue may reserve a round, and a second continue would answer 4
("another peer review is already running").

**5. `subfleet gate poll <id>` resumes a gate.** It is the poll loop on its own.
It approves nothing: a poll advances the gate only along what its start or
continue already approved, because the round records the main approval and
`on_agreement`. Its exit codes are the gate's, 0 to 5 (C-17.1). Its first poll
has not reached a daemon, so if it finds none it ends at once, as the wait
record's call 6 does. A `pr`, `plan` or `continue` has reached the daemon by its
first answer.

**6. A round adopts a job that carries its request id.** Before it submits,
`_submit` looks the round's request id up in `jobs`. A job found there is the
round's own, since no other submit can carry that id, and the round adopts it.
So a daemon that died between the job and `round-submitted` costs neither a
second job nor a blocked round, even under an edited policy.

**7. A poll of a `ready` gate answers 4.** It names `subfleet gate continue <id>
--main-approve`, which reserves the gate's first round. No poll sees `ready`
otherwise. A start holds the gate's lock until its round is reserved, and nobody
has the id until the start answers.

**8. Every exit 1 names the resume.** Once the gate's id is known, every exit 1
prints `gate <id> stays in the daemon with any peer job it started; resume with:
subfleet gate poll <id>`. That covers giving up past the window, an interrupt,
any other operational failure, and an answer with code 1. A lost continue prints
it too, because a poll shows whether the continue took effect and follows it. A
lost start names nothing, since its id never arrived.

**9. The lines.** These are the wait record's call 9, with "still polling gate
<id>" in place of "still waiting":

- When an answer ends an outage, one stderr line says `daemon restarted` if
  `daemon.lock` names a different pid and start time than at the answer before.
  Otherwise it says `daemon answered again`.
- Past the window, one line comes first: `the daemon has not served gate <id> for
  Ns, and the 300s restart window from its first failed poll has passed`, N
  counted from the last answer that was not `recovering`, as `wait`'s line counts
  from its last answer.

The loop reads the lock at every answer, so it compares against the process that
answered last, as `wait` does since its review r1. That is one small file read
per 0.25 s poll.

**10. Exit codes keep C-17.1's meanings, 0 to 5.** Giving up past the window is
1, as every operational failure is. 69 and 124 are not gate exits.

## Known gaps this leaves alone

- **A rerun lost to a death.** A death after a discarded round (C-23.43)
  commits, and before its rerun's round is journaled, leaves the gate blocked
  with the discard's reason (exit 4). The rerun is lost, never doubled, and `gate
  continue --main-approve` runs it. This is older than this change and not
  widened by it: the first poll after any restart takes this path.
- **A lost `gate.start`.** The gate may exist, with a peer job that nobody
  polls, and running the command again starts a second gate. Making `gate.start`
  idempotent needs a client-minted request id in `GateStartArgs` (C-16.3's
  pattern), which is a protocol change of its own.
- **Commands started before this ships.** A `subfleet gate` process started
  before the install that ships this runs the old loop, and that install still
  ends it.

## Invariants

These are on the daemon side, in `tests/unit/test_gate_poll_resend.py`. They are
run on the real `GateService` with the real submit and admission behind it, over
every schedule Hypothesis draws: polls, peer completions, restarts, recovering
daemons, two concurrent pollers, and deaths before or after each durable write,
during `gate.start` and during `gh pr merge`.

- **D1.** One peer job per round, under the round's own request id.
- **D2.** `round-submitted`, `round-finished` and `certificate-issued` are each
  journaled at most once per round, and `completed` at most once.
- **D3.** At most one `gh pr merge` and one merge action per gate.
- **D4.** Once a poll has answered a code, further polls change nothing: no
  journal entry (except `action_attempting`'s reconciliation), job, action or
  merge, and the same answer.
- **D5.** A recovering daemon's answer does nothing.
- **D6.** With no death, the gate ends as a lossless run does. With one, it ends
  in a code a death may leave (blocked, or an unverified merge), and D1 to D4
  still hold.

These are on the CLI side, in `tests/unit/test_gate_restart.py`. Hypothesis runs
them over interleavings of answered, lost, refused, gone, recovering, busy and
hung polls on a fake clock, and each run is compared with `reference`, a separate
statement of calls 2 to 5 and 9 (a differential test of the loop's decisions).

- **G1.** The exit is the gate's code exactly when an answer with a code reached
  the CLI, and 1 otherwise.
- **G2.** Nothing is sent but `gate.poll` for that gate.
- **G3.** No poll follows a code.
- **G4.** The loop never gives up inside the window. A daemon gone for good ends
  it within the window, one poll budget and one pause.
- **G5.** One line per outage that an answer ended. It says "restarted" exactly
  when the lock names another process.
- **G6.** Every exit 1 names `subfleet gate poll <id>`.

Fake-daemon tests over a real socket cover:

- a poll closed unanswered by a stop;
- connects refused for 1.5 s;
- a daemon gone for good;
- a recovering daemon, briefly and past the window;
- a daemon that drops every poll;
- a hung daemon;
- two outages, each with its own window;
- a lost start, a lost continue, and a start answered recovering;
- `gate poll`, with and without a daemon;
- an interrupt.

`tests/e2e/test_gate_restart_e2e.py` stops a real `subfleetd` (SIGTERM, SIGKILL)
while a real `subfleet gate plan` polls a gate whose peer round is held, then
starts the next daemon. The gate ends 0 with one "daemon restarted" line.
