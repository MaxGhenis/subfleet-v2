# PR #127, round seven: review of 8494bc8e

Status: IN PROGRESS. Findings are recorded as they are confirmed.

Head: `8494bc8e8be245339776824f31b2562cd1d586e2`. Base: `origin/release/217`.
Round-six fix under review: `e4ff7ae5..8494bc8e`.

## Round-six P2: fixed in behaviour

Restart crash matrix, `tests/unit/test_review_r7_probes.py`: an old explicit timer
(+600 s), an older completed final replacing it (+900 s), and a newer completed final
with two lines (+1100 s, then +1200 s). Neither intent has replayed. Restart at +950 s,
when the old timer and the older final are overdue. Crash at each boundary of the
first production `tick()`, then restart again:

before replay; after the older line registers; after the older acknowledgement runs
but before it commits; after it commits; after the newer first line registers; after
its second line; before and after the newer acknowledgement commits; before evaluation
(hard exit only); no crash.

Every boundary passes in-process (SimulatedCrash, a BaseException that tick() cannot
log away) and as a hard `os._exit(77)` child with no unwinding or SQLite close:
**18 crash cases and the no-crash control.** In each, no wake fires at +950 s or at
the crash, exactly one wake fires at +1201 s naming only "Newest final", the intents
table ends empty, each of the three final lines is registered exactly once, and the
request states are superseded, superseded, superseded, fired.

## Findings

### R7-P3-1: intents replay in message order, not completion order

`subfleet/conversations/wakes.py:334-336` orders replay by `m.seq`. Completion order
differs from message order when a person message overtakes a yielded wake
(`store.py:1206` sorts `origin='wake'` last), and when repair messages or missed
steers jump the queue. If an older-completed intent is still unresolved when the
overtaken message completes, the older intent replays last and supersedes the newer
re-arm.

Scenario: wake W (seq 1) is accepted, then person message P (seq 2) arrives and runs
first. P completes with `at=+900 "Person turn: older re-arm"`; its replay fails
(`sqlite3.OperationalError: database is locked`) on settlement and on the next tick;
W still dispatches. W completes with `at=+1200 "Wake turn: newest re-arm"` and the
failure has cleared. Replay runs W, then P.

Executed (`test_intents_replay_in_message_order_not_completion_order`):
- Failing: requests end `Wake turn: newest re-arm`=superseded, `Person turn: older
  re-arm`=pending. At +901 s the stale "Person turn: older re-arm" wake fires; at
  +1201 s nothing more. The newest instruction is lost.
- Control (no failure): the person re-arm is superseded, one wake at +1201 s naming
  "Wake turn: newest re-arm".

Premise: P's replay must fail on every attempt from P's completion until W completes.
A crash alone cannot produce it, because tick replays before `_dispatch`. Fix: order
replay by intent insertion (`ORDER BY i.rowid`, or `settled_at, rowid`). Rowid
increases with insertion among coexisting rows, so the order matches completion.

### R7-P3-2: one failing replay stalls wakes and recovery in other conversations

`wakes.py:334-337` has no per-intent isolation. The first exception aborts the loop,
so every conversation sorted after the failing one keeps its intent. The new fence
(`wakes.py:149`) then makes those conversations ineligible. Every completed turn with
any final text records an intent (`service.py:2359-2360`), so in a fleet this covers
any conversation that finished a turn since. The same unguarded replay heads
`_replay_unsettled` (`service.py:2088`) and `op_conversation_wake`
(`service.py:718`).

Executed:
- `test_one_failing_replay_does_not_fence_other_conversations`: two conversations,
  each with an old due timer and a completed +1200 s replacement. Replay for the
  first raises. Failing run: the second conversation gets **0 wakes** at +1201 s and
  2 intents remain. Control: one "Replacement" wake.
- `test_failing_final_replay_blocks_unrelated_recovery_and_explicit_wakes`: the same
  failure in conversation A stops `_replay_unsettled` before it replays conversation
  B's ended turn (`adopted=[]`, B's message stays `running`), and conversation C's
  explicit `subfleet wake` fails with `OperationalError: database is locked`.
  Control: B is replayed and C's wake is recorded.

The fix report says a failed replay "defers only the conversation with an intent";
that holds only when no other conversation has an intent. I found no input that fails
persistently (refusal events are idempotent, parse and validation errors are caught,
and the store serializes writers), so this needs an operational fault: a busy or full
disk, or an I/O error. Fix: catch per conversation in `replay_final`. Stop that
conversation's later intents to keep order, log, continue with other conversations,
and keep `_replay_unsettled` and `conversation.wake` independent of other
conversations' failures.

## Checks run

(pending)
