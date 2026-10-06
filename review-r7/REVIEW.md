# PR #127, round seven: review of 8494bc8e

**APPROVE.** No P1 or P2 findings. The round-six P2 is fixed in behaviour: the
restart crash matrix passes at every boundary, in-process and with hard exits.
I found three P3s. Two need an operational fault that persists over time. The
third is a 10% flake in a round-six regression test. A suggested fix for all three
has been run against the suites; it is in this folder and has not been committed
to the PR.

Head: `8494bc8e8be245339776824f31b2562cd1d586e2`. Base: `origin/release/217`
(merge base `3e13ecde`). Round-six fix under review: `e4ff7ae5..8494bc8e`.

## 1. Round-six P2: fixed in behaviour

`tests/unit/test_review_r7_probes.py::test_restart_crash_matrix_with_two_pending_intents`
and `::test_restart_hard_exit_matrix_with_two_pending_intents`. Setup:

- an old explicit timer at +600 s;
- an older completed final that replaces it (+900 s);
- a newer completed final with two lines (+1100 s, then +1200 s).

Neither intent has replayed. The first restart runs at +950 s, when the old timer and
the older final are overdue. The first production `ConversationService.tick()` crashes
at one boundary, then a second restart runs to +1201 s.

Boundaries:

- before replay;
- after the older line registers;
- after the older acknowledgement executes but before it commits;
- after that acknowledgement commits;
- after the newer first line registers;
- after the newer second line registers;
- before the newer acknowledgement commits;
- after the newer acknowledgement commits;
- before evaluation (hard exit only);
- no crash.

The in-process crash is a `BaseException`, so `tick()` cannot log it and continue.
The hard exit is a foreground child that calls `os._exit(77)`, with no unwinding or
SQLite close. Each one has a "boundary reached" assertion.

**All 18 crash cases and the no-crash control pass.** In every case:

- no wake fires during the crashed run or at +950 s;
- exactly one wake fires at +1201 s, naming only "Newest final";
- `final_wake_intents` ends empty;
- each of the three final lines has exactly one `wake_requests` row;
- request states end superseded, superseded, superseded, fired.

Evidence: [logs/r7-probes-final.log](logs/r7-probes-final.log).

These are not vacuous. Five mutations, each restored afterwards:

| Mutation | Matrix (18) | Round-six tests |
|---|---|---|
| r5 order: no replay step, no direct-evaluation replay, no fence | 18 fail | 8 fail |
| replay newest first | 18 fail | not run |
| intent never acknowledged | 18 fail | not run |
| fence removed only | pass | interleaving probe and claim tests fail |
| replay-before-evaluation removed (fence kept) | pass | 3 restart tests fail |

The last two rows show the fix has two independent guards. Either one alone keeps a
restart safe, because `_replay_unsettled` replays later in the same tick.

Logs: [logs/mutation-*.log](logs).

## 2. Findings

### R7-P3-1: intents replay in message order, not completion order

**`subfleet/conversations/wakes.py:336`** orders replay by `m.seq`, but a
conversation's turns can complete out of message order. A person message overtakes
a yielded wake (`store.py:1206` sorts `origin='wake'` after person messages), and
repair messages and missed steers jump the queue. If an earlier-completed intent is
still unresolved when the overtaken message completes, it replays last and
supersedes the newer re-arm.

Scenario (`test_intents_replay_in_message_order_not_completion_order`):

1. Wake W (seq 1) is accepted. Person message P (seq 2) arrives and runs first.
2. P completes with `at=+900 "Person turn: older re-arm"`. Its replay raises
   `sqlite3.OperationalError: database is locked` at settlement and on the next
   tick, and W still dispatches.
3. W completes with `at=+1200 "Wake turn: newest re-arm"`, after the error has
   cleared. Replay runs W, then P.

Executed result:

- **Failing run:** "Wake turn: newest re-arm" ends superseded and "Person turn:
  older re-arm" ends pending. At +901 s the stale "Person turn: older re-arm" wake
  fires; nothing fires at +1201 s. The newest instruction is lost.
- **Control:** the person re-arm is superseded, and one wake fires at +1201 s
  naming "Wake turn: newest re-arm".

Why P3: P's replay must fail on every attempt from P's completion until W
completes. A crash alone cannot do it, because tick replays before `_dispatch`.

Fix: order by completion. The suggested patch uses `ORDER BY i.settled_at,i.rowid`.
`ORDER BY i.rowid` would be immune to wall-clock steps, but it would break the
round-six test `test_recorded_finals_replay_in_message_order_after_partial_registration`.
That test inserts the newer intent first, a state the serial dispatcher cannot
produce. `settled_at` keeps it green.

### R7-P3-2: one failing replay stalls wakes and turn recovery in other conversations

**`subfleet/conversations/wakes.py:334-337`** has no per-intent isolation. The first
exception aborts the loop, so every conversation sorted after the failing one keeps
its intent, and the new fence (`wakes.py:149`) makes those conversations ineligible.
Every completed turn with any final text records an intent
(`service.py:2359-2360`), so in a fleet the fence covers every later-sorted
conversation that finished a turn since. The same unguarded replay heads two other
paths:

- `_replay_unsettled` (`service.py:2088`), which settles turns whose runner died
  with the daemon (C-25.3);
- `op_conversation_wake` (`service.py:718`).

Executed:

- **`test_one_failing_replay_does_not_fence_other_conversations`.** Two
  conversations each have an old due timer and a completed +1200 s replacement, and
  replay for the first raises. Failing run: the second conversation gets **0 wakes**
  at +1201 s, and 2 intents remain. Control: one "Replacement" wake.
- **`test_failing_final_replay_blocks_unrelated_recovery_and_explicit_wakes`.**
  The same failure in conversation A has two further effects:
  - `_replay_unsettled` stops before it replays conversation B's ended turn
    (`adopted=[]`; B's message stays `running` and B stays occupied);
  - conversation C's explicit `subfleet wake` fails with
    `OperationalError: database is locked`.

  Control: B is replayed and C's wake is recorded.

The fix report says a failed replay "defers only the conversation with an intent".
That holds only when no other conversation has an intent.

Why P3: I found no input that fails persistently.

- Refusal events are idempotent (`INSERT OR IGNORE`).
- Parse and validation errors are caught.
- Writers are serialized by `BEGIN IMMEDIATE` under a lock.

So this needs an operational fault that persists over time, such as a busy or full
disk or an I/O error.

Fix: catch per conversation. Skip that conversation's later intents to keep order,
log the failure, continue with other conversations, and re-raise only to a caller
replaying for that conversation (`conversation.wake`). An explicit re-arm still must
not overtake its own conversation's older intent.

### R7-P3-3: a round-six regression test fails about 10% of runs

**`tests/unit/test_review_r6_restart.py:59`** calls
`wakes.normalize(at=iso(now + 300), ..., now=now)`. `iso()` rounds to microseconds,
so the round trip can land a fraction of a microsecond under the five-minute floor,
and normalize refuses with `ConversationError: at needs an ISO timestamp ... at
least 5 minutes away`.

- Measured: **126,685 of 1,258,291 real `time.time()` readings refused (10.1%)**.
- On the unmodified head: **11 passed, 1 failed in 12 runs**
  ([logs/r6-restart-flake.log](logs/r6-restart-flake.log)). It also failed once in
  my `fence-only-removed` mutation run, with the same `ValueError`.

This test is the round-six replay-order regression, and it is the killing test the
fix report gives for its `replay-newest-first` mutation. About one run in ten it
fails for the wrong reason, in CI and in mutation checks.

Fix: use `iso(now + 301)`. The final-text uses at lines 47, `r5_repro:137` and
`r6_probes:139` are safe, because final-text timers are checked against the
second-resolution turn start.

## 3. The whole wake path, adversarially

No further loss found. Area by area:

- **Re-arms.** Replay at every restart boundary passes (section 1). R2–R6 re-arm
  regressions pass: carried PR events, refusals, stale poll writes, and an explicit
  re-arm after older intents. The remaining ordering hole is R7-P3-1.
- **Restart between ready and accepted.** R4/R5 acceptance cases (inside-claim,
  after-commit, ready carry across re-arms) pass. Acceptance re-checks the intent
  fence inside the claim transaction (`wakes.py:159`); the fence-only mutation shows
  the guard is live.
- **Blocked conversation.** `test_pending_intent_replays_while_held_and_fires_once_after`:
  - blocked (`delivery-unknown`) and legacy-held conversations replay the pending
    replacement while held (0 intents, 0 wakes at +601 s and +1201 s);
  - after release, they fire it exactly once, never the old timer.

  The R5 blocked, legacy and archived restarts pass.
- **Throttle.** Same probe at eight wakes with the cooldown running to +1500 s:
  0 wakes while throttled, one replacement wake at +1501 s. The R5 throttle restart
  passes.
- **Pruned run.** `test_new_pruned_target_of_an_all_of_request_strands_the_others`
  passes. A registered run whose row is pruned is delivered as `pruned`, per
  C-24.10.
- **`subfleet wait` outliving the turn.** Both real-process strand e2e files ran (not
  skipped): 4 passed, automatic and WAKE-ME variants. The daemon's `wait` op reads
  notices and never marks them (`daemon.py:2946-2983`), so a waiter cannot swallow
  the wake.
- **Lock and re-entrancy.** `_replay_final_wakes` takes the service `RLock` and sets
  `_replaying_final_wakes` only while holding it, so another thread never sees the
  flag set. The only new caller, `WakeEngine.tick`, runs only from `control_tick`,
  in the same thread type that already replayed under the lock in round five. There
  is no new lock order.

Design note, not a finding: a malformed or already-past final-text `WAKE-ME` line is
refused with a timeline row and no wake (C-24.10: "Invalid requests are logged and
displayed as a refusal notice"). PR access refusals do wake. An unattended agent
that writes a bad line is never told. That was the documented choice after round
three; changing it is a product call.

## 4. Executed test counts

Everything ran on Python 3.14.7 with locked dependencies and
`TMPDIR=$PWD/.review-tmp/`. Every slice was a foreground pytest process with a
540-second deadline, one at a time, and every e2e file ran alone. None expired; the
longest was 68 s. This lane allows process inspection, so every e2e case actually
ran: round six recorded 17 skips and 2 sandbox failures, and here there are none.

**On the unmodified head:**

| Slice | Passed | Failed | Skipped |
|---|---:|---:|---:|
| Wake unit suites (7 files) | 78 | 0 | 0 |
| R2/R3 repros and R3 measurements | 49 | 0 | 0 |
| R4 repros | 27 | 0 | 0 |
| R5 repros, R6 probes, R6 restart | 52 | 0 | 0 |
| Wake e2e | 4 | 0 | 0 |
| R2 strand e2e | 2 | 0 | 0 |
| R3 strand timing e2e | 2 | 0 | 0 |
| Conversation service | 92 | 0 | 0 |
| Catalog and catalog lifecycle | 36 | 0 | 0 |
| Milestone-1 e2e | 9 | 0 | 0 |
| Conversation store and fake-provider wake pipeline | 36 | 0 | 0 |
| **Existing suites** | **387** | **0** | **0** |
| R7 probes (new) | 24 | 3 | 0 |
| **Unique total: 414** | **411** | **3** | **0** |

The 3 R7 failures are the R7-P3-1 and R7-P3-2 repros; their controls pass. Not
counted above:

- the 12-run flake loop (11 passed, 1 failed);
- the 5 mutation runs;
- the runs with the suggested fix applied: R4–R7 repros 106 passed, wake unit and
  R2/R3 repros 121 passed, conversation service 92 passed, wake e2e 4 passed.

After those runs I restored `subfleet/` and confirmed it is identical to
`8494bc8e`. On that restored head, the R7 probes fail exactly the 3 repros again.

## 5. Artifacts

- [test-probes.patch](test-probes.patch) adds `tests/unit/test_review_r7_probes.py`,
  27 cases. It reverse-applies cleanly against the executed file. Apply to
  `8494bc8e` and run:
  `TMPDIR=$PWD/.review-tmp/ .venv/bin/python review-r7/run_slice.py r7 tests/unit/test_review_r7_probes.py`.
  Expected on head: 24 passed, 3 failed.
- [suggested-fix.patch](suggested-fix.patch) changes `wakes.py` and `service.py`
  (+22/−8). It covers completion-order replay and per-conversation failure
  isolation. It is not committed to any implementation branch, and
  `git apply --check` passes on head. The one-line fix for R7-P3-3 is described
  above and is not in the patch.
- [mutate.py](mutate.py) and [run_slice.py](run_slice.py): the foreground runners,
  each with a deadline, with source restored in `finally`.
- [logs/](logs) and [junit/](junit): every slice.

- [strand-timing-evidence/](strand-timing-evidence): the r3 strand timing e2e's own
  daemon logs and timings.

Review commits are on the workspace-local branch `review/pr127-r7`, rooted at
`8494bc8e`. Nothing was pushed, and nothing was committed to the PR branch.
