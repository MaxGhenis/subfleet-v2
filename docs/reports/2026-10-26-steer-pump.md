# The live probe's steer check fails under load (D-STEER-PUMP1)

Branch `fix/steer-pump-217`, base `release/217` at `946ea9f96`. Built in Subfleet job
`20261026-002453-steer-pump-build` (a detached worktree: the branch was checked out in the caller's
worktree, so the commits are on the job's HEAD and kept under `refs/subfleet-salvage/`). Not pushed.

**Result: a probe race.** The live probe sent its steer without `into` and judged it after one pump.
A running turn that cannot take a steer yet answers `not-steerable`. The outbox asks again only for
a steer that names its turn (C-29.7), so the probe's steer was refused at once and read as `null`.
The build round changed nothing in `app/` and no contract clause. Fix round 1 (the last section)
changed the app's "Send now" and C-29.7, after review r1 showed that "Send now" did not always name
its turn.

## The CI failure

- **Where:** #262 at `39f081905`, run 40273357312, Python 3.12 on macOS, shard 1/5.
- **What failed:** `tests/frontend/test_core_live.py::test_the_app_core_drives_a_development_daemon`,
  with one failed check, `"steer: the daemon takes the steer"`, at 14.73 s into the probe, `"detail": null`.
- **Not a stop:** no `stopped_at` was recorded, so the run went on. The two later steer checks
  passed: the steered message settled once, as `steered` or `complete`, and was drawn once.
- **Also from the hub's brief (not re-read here):** the same test passed on Python 3.14 at the same
  head, and #262 changes no `app/` or `tests/frontend/` code.

At `946ea9f96` the check was (`tests/frontend/CoreProbeLive.swift:476-482`):

```swift
let steered = try engine.send(conversation: cid, text: "also say steered", settings: settings, steer: true)
let report = engine.pump()
...
run.check("steer: the daemon takes the steer", answer?.state == .acknowledged
          && ["steering", "steered"].contains(answer?.receipt?.state ?? ""),
          answer.map { jsonObject($0.receipt) } ?? NSNull())
```

The steer carries no `into`.

## The mechanism

### What the code does (read this session)

- **The steer is journaled.** `Outbox.enqueueSubmit(steer: true)` journals the steer in the same
  write as its submit, for any conversation that is not a draft key (`app/Sources/Outbox.swift:351-354`).
  A steer leaves the journal only through `appendSteer`'s replacement (`:396`), or through `prune`
  once more than 200 steers are closed (`:739`). The probe's `outbox` and `engine` share one
  instance (`CoreProbeLive.swift:84-85`, and again after the restart at `:322-323`).
- **One pump sends the submit, then the steer.** The steered message's submit is acknowledged first.
  Then the steer becomes its conversation's first open work, and the same pump sends it once
  (`OutboxSender.pump`, `Outbox.swift:822-873`).
- **Retries on `not-steerable` need `into`.** `Outbox.finishSteer` asks again after a
  `not-steerable` answer only when `steer.into != nil` (`Outbox.swift:450`). Otherwise the steer
  closes as `refused`, with `retryable: false` and no receipt (`:458-459`). A transient error is
  retried with or without `into` (`:441-449`).
  - The hub read C-29.7 as asking again after any `not-steerable` answer. C-29.7 says it does so
    "when it names its turn (`into`)".
  - The app names the turn in both places that steer, since fix round 1:
    - Return in the composer: `UIModel.send` reads the turn the composer steers into when Return is
      pressed, and steers only when there is one (`UIModel.swift:820-821`).
    - "Send now" in the queue tray: the row carries the turn it was offered for
      (`QueuedMessage.steerInto`, `QueueTray.swift:33`), the button captures it when it is drawn
      (`UIQueueTray.swift:220`), and `UIModel.steer` takes it as an argument (`UIModel.swift:844`).

    Until fix round 1, "Send now" read the live turn when the click's callback ran (`UIModel.swift:843`
    at `44d4c8b49`), so a click that landed after its turn ended could name none; review r1 ran it.
    The probe named no turn either.
- **When the daemon answers `not-steerable`.** `op_message_steer` answers it (code 7) in two cases
  (`subfleet/conversations/service.py:1349-1358`): no runner holds the live turn, or the runner's
  `steerable` is false. `TurnRunner.steerable` (`runner.py:266-271`) needs:
  - `replay_caught_up`, which is set on the runner's first empty read of the provider's stdout
    after its handshake (`runner.py:357-359`);
  - the driver's own `steerable`.

  `ClaudeTurn.steerable` (`claude_turn.py:248-250`) needs `msg_lifecycle_v1` among the
  capabilities, and those come from the provider's `system init` row (`claude_turn.py:599`). The
  fake Claude writes that row right after it replays the message, and the replay is what marks
  the turn `accepted` (`tests/fake/interactive_claude.py`, `turn`).
- **What the probe waits for first.** Before steering, the probe waits for the host to be `running`
  and `accepted`. Neither says the runner has caught up, or that the driver has read `system init`.

### Reproduced (executed)

`test_core_live.py` can't put the daemon into this state on purpose. It also can't reach step 14
under a Subfleet lane (see "What did not run"). So the reproduction runs the step's own outbox
sequence through the core probe's `outbox` steps. It runs against the real `ConversationService`
and a real `TurnRunner` (`tests/frontend/daemon_harness.py`, which bypasses the person check).

- **The unready turn:** the host's runner can't take a steer at the first try and can from the
  second. It is made unready in one of two ways:
  - `catching-up`: `replay_caught_up = False`;
  - `no-system-init-yet`: the driver's capabilities are empty.
- **The test:** `tests/frontend/test_core_live_steer.py` at `c46132c57`, run against `946ea9f96`'s
  `CoreProbeLive.swift`. The outbox steps it uses are unchanged from `946ea9f96`.
- **The sequence, as `946ea9f96` runs it:** `submit` with `steer: true` and no `into`, then one
  `engine-pump`, then a read of the steer.

Both causes gave the same row at the moment of the check:

```json
{"attempts": 1, "failure": {"code": 7, "message": "not-steerable: the provider cannot take a steer now",
 "reason": "not-steerable", "retryable": false}, "into": null, "next_attempt_at": null, "order": 4,
 "receipt": null, "state": "refused", ...}
```

`946ea9f96`'s detail is `jsonObject(answer.receipt)`, which is `null` here.

**Which `null`: a steer journaled with no receipt (observed in the reproduction).** It was refused
at once. It was not waiting for a retry, and it was not missing from the journal.

### What CI must have seen (inference, not observed)

The CI artifact's `probe/exchanges.jsonl` holds the daemon's answer to `message.steer` and would
settle this. This job had no network access to fetch it.

**Ruled out:**
- **"No steer journaled":** the steer is journaled by construction, and the probe has one steer,
  far below the 200 that prune keeps.
- **The steer never sent:** that would need the submit before it to fail. The check that followed
  in CI saw the steered message settle in the daemon, so its submit landed. The same pump then
  sends the steer.
- **A 15 s client timeout in step 14:** the check came 14.73 s into the whole probe run.
- **Other refusal reasons:**
  - `not-queued`: the dispatcher claims no message while its conversation has a live turn
    (`store.next_dispatchable`, `store.py:1306-1323`).
  - `no-live-turn`: the slow host runs until it is stopped.
  - `settings-narrower`: the settings are the same.
  - `not-next`: no repair message is queued.
  - `not-steerable` for slash or shell input: the text is plain.

**What remains:**
- **Most likely:** the daemon answered `not-steerable` because the host's runner could not take a
  steer yet, and the steer was closed at once. That is the reproduced row.
  - The likeliest cause under load is a runner that had not caught up. The slow fake writes a delta
    every 50 ms, and `replay_caught_up` waits for an empty read.
  - A driver that had not yet read `system init` would give the same answer.
- **Less likely:** a transient answer (busy, exit 69; an internal error, code 1; a transport fault)
  left the steer `queued` for a retry that the single pump never made.

The fix covers both.

## Why the hub's decision 3 needed one more change (counterexample)

Decision 3 said to pump until the steer is acknowledged or closed. That alone can't fix the failure
above, because the steer is already closed after the first pump. The executed counterexample is
`test_a_steer_without_into_is_refused_at_once_and_no_wait_takes_it`, run with the shared wait:

- the steer is `refused` after 1 attempt (`not-steerable`, `retryable: false`, receipt `null`);
- the wait used 1 pump and 0 s.

The smallest change that keeps decision 3's intent is to send the probe's steer the way the app
sends it, naming its turn (`into: host.key`), and then wait. With that, a `not-steerable` answer is
asked again at 0.5 s doubling, five tries in all (C-29.7), and the wait sees the final answer.

The check still means "the daemon takes the steer". A steer refused for good, or given up after
five tries, still fails it.

## The change, file by file

- **`tests/fake/interactive_claude.py`** (`9fe84abda`): a test-only knob, `SUBFLEET_FAKE_LATE_INIT_S`.
  - A `slow` turn waits that many seconds between replaying its message and writing
    `system init`. An interrupt cuts the wait short.
  - Meanwhile the turn is running and accepted, but it takes no steer: the daemon answers
    `not-steerable`, "the provider cannot take a steer now".
  - It is read only by the fake. `SUBFLEET_FAKE_*` names reach the fake through the daemon's
    harness prefixes (`subfleet/launch_env.py:77`).
- **`tests/frontend/test_core_live.py`** (`9fe84abda`): the development daemon passes
  `SUBFLEET_FAKE_LATE_INIT_S` to the fake only when the pytest session's environment sets it.
  This is the switch for turning the reproduction on and off in the live test.
- **`tests/frontend/CoreProbeLive.swift`:**
  - **Decision 2** (`29c069203`, then simplified in `9f29ed166`): `steerDetail` gives the whole
    steer row, using the probe's existing `project(OutboxSteer)` (state, attempts,
    `next_attempt_at`, `into`, failure, receipt), or `"no steer journaled"`.
  - **Decision 3** (`9f29ed166`): step 14 sends the steer with `into: host.key`, then calls
    `settleSteer` and judges the result with `steerTaken`.
    - `steerTaken` is the unchanged condition: the steer is acknowledged, with a receipt of
      `steering` or `steered`.
    - The check's detail is `{"steer": <row>, "pumps": n, "waited_s": t}`.
    - `settleSteer` (`:81`) pumps until the steer is no longer open, or until the deadline.
      Between pumps it sleeps until the steer's own `nextAttemptAt`. When no retry is due (the
      steer waits behind its submit) it sleeps 0.5 s, the outbox's shortest backoff. It never
      spins.
- **The build round's deadline, `steerSettleDeadline` (`:70`): 90 s**, counted from the steer's first
  pump. Fix round 1 replaced it with a bound counted from the step's start (see that section): the
  first pump also waits for the steered message's submit, and the wait checked its deadline only
  after each pump, so review r1 ran this one to 97.5 s. The build round reasoned as follows. The
  outbox ends a steer on its own:
  - at most `Outbox.steerAttempts` = 5 tries;
  - 0.5 + 1 + 2 + 4 = 7.5 s of backoff between them;
  - each try waits up to the client's answer timeout, 15 s (`DaemonClient.baseTimeout`), so
    5 × 15 = 75 s.

  That is 82.5 s at most, plus 7.5 s of margin for the pumps' journal writes, giving 90 s. A
  deadline at or above the outbox's own bound never judges a steer the outbox is still entitled to
  ask about. A steer still open at 90 s is stuck, not slow. (The bound left out the submit's own
  answer, up to 15 s, and was not checked before a pump: see fix round 1.)

  This costs nothing on a pass. The wait returns as soon as the pump that answered the steer
  returns, which is the first pump when the turn is ready.

  The slow host streams for 60 s before it would end by itself. A steer still being tried then
  gets `no-live-turn`, is refused, and the check fails at once.
- **`tests/frontend/CoreProbeScenarios.swift`** (`9f29ed166`): a new `outbox` step,
  `settle-steer`, runs the same `settleSteer` and `steerTaken` on the probe's clock. Its pause
  advances the clock instead of sleeping.
- **`tests/frontend/test_core_live_steer.py`** (`0d41fb0d6`, `c46132c57`, `9f29ed166`): 12 tests,
  all against the service harness.
  - The step takes a steer that the turn could not take at the first try. This runs for both
    causes, and is the red reproduction at `0d41fb0d6`, marked `xfail(strict)` there.
  - Every number of unready tries from 0 to 6:
    - one pump per try, waiting exactly the backoff;
    - taken at the next try below five;
    - from five on, `refused` `not-steerable` after five tries, with the wait ending long before
      its deadline.
  - A steer without `into` (the counterexample).
  - A steer refused for good: the turn ended, so the retry gets `no-live-turn`.
  - A deadline shorter than the tries: the steer is still `queued` and the check fails.
- **`tests/fake/test_interactive_steer.py`** (`87720ea32`): the knob, run through the real
  `ClaudeTurn`, `TurnRunner` and `op_message_steer` against the fake process:
  - the turn is accepted, and `message.steer` answers `not-steerable` (code 7);
  - once `system init` arrives the turn takes steers;
  - without an interrupt the row comes the knob's seconds later, and an interrupt cuts it short.

**Waiting for a daemon signal (decision 3's last point): not added.**
- No daemon signal says a turn's runner can take a steer. `system init` does emit a `served` event,
  so the timeline's `served.model` shows that the driver has read the capabilities.
- But `served.model` says nothing about `replay_caught_up`, which is the runner's own state, or
  about a runner not yet registered.
- So waiting for it would still race. The outbox's retry (C-29.7) is the app's own answer to all
  three cases.

**Decision 4: not triggered.** The outbox behaved as C-24.9 and C-29.7 say. The build round changed
no `app/` code and no clause. (Fix round 1 changed both, for "Send now"; the outbox is unchanged.)

## Decision 5: the probe's other single-pump reads

Line numbers are `946ea9f96`'s. A pump's own `report` and the journal rows it writes are
synchronous: each send waits for its answer before `pump()` returns.

| Line | Step | What is read after the pump | Can it arrive after the pump returns? | Changed |
|---|---|---|---|---|
| 166 | 2: create, first, follow-up | `report.acknowledged`, `entry(requestID).conversationID`, both receipts' `seq` and `state`, `lastAfterMessageID` | No. Each is the answer to a call the pump made and waited for (`conversation.create`, two `message.submit`). `message.submit` answers `queued` for every new message (C-29.7). | No |
| 237 | 5: tool approval | Nothing from the pump. `follow` waits up to 60 s for the card from events. | The card comes later, but `follow` already waits for it. The submit's answer is synchronous. | No |
| 286 | 6: question | As at 237 (`follow`, 60 s) | As at 237 | No |
| 397 | 12: unfocused conversation | `otherReport`, `entry(otherRequest).conversationID` | No: the create's answer is recorded in the pump. The notification is then waited for by the watch loop (60 s). | No |
| 446 | 13: native session | Nothing. `untilState(continued, complete/failed)` polls for 60 s. | Already waited for | No |
| 467 | 14: the host turn | Nothing. `untilState(host, running)`, then `follow` until accepted | Already waited for | No |
| 477 | 14: the steer | The steer's row | **Yes.** The steer's answer can be "not yet". For a steer that names its turn, the outbox asks again after the pump returns. Without `into`, it was refused for good. | **Yes**: `settleSteer` (above) |

The other pumps are synchronous too:
- 326: the resend returns the stored receipt.
- 334: the out-of-order refusal and its resync.
- 339: after `Thread.sleep(0.6)`. The 0.5 s backoff is stamped inside the pump at 334, so the
  entry is due when this pump runs, and its resync and send happen within the pump.
- 369: the image submit.

## Runs

**Environment:**
- Scratch `$(getconf DARWIN_USER_TEMP_DIR)sf-steerpump`, with a venv on Homebrew
  `python3.14` (GIL build).
- Every pytest ran in the foreground under `suite_hold.sh`, in slot A, with
  `env -u SUBFLEET_RETENTION_DORMANT -u PYTHONPATH ... uv run --no-sync pytest -q -p no:cacheprovider`.
- The core probe was compiled outside the hold into the checkout's probe cache.
- No run went outside the lock.

| Run | Code | Files | Result |
|---|---|---|---|
| A0 ×2 | `946ea9f96` probe, knob 2 s | `tests/frontend/test_core_live.py` | 1 error each, **not red or green**: the development daemon wasn't ready within the harness's 10 s (load 62–98) |
| A2 ×2 | `29c069203`, knob 2 s | `tests/frontend/test_core_live.py` | 1 error each: same setup timeout |
| R, R2 | `946ea9f96` probe, harness reproduction, `--runxfail` | `tests/frontend/test_core_live_steer.py` | **red**: 2 failed. The rows are quoted above (R2 prints them in full). |
| G | `9f29ed166` | `test_core_live_steer.py`, `test_core_steer.py`, `test_core_steer_properties.py` | **green**: 57 passed (12 + 43 + 2) |
| M1 | `9f29ed166` + hand mutant: `settleSteer` returns after one pump | `test_core_live_steer.py` | **red**: 10 failed, 2 passed. The single pump reads `queued`, attempt 1, `not-steerable` `retryable: true`, receipt null. The 2 passes are the turn ready at once, and the steer without `into`. |
| M2 | `9f29ed166` + hand mutant: `steerTaken` also accepts `refused` | `test_core_live_steer.py` | **red**: 4 failed, 8 passed (unready tries 5 and 6, no `into`, refused for good) |
| F | `87720ea32` | `tests/fake/test_interactive_steer.py` | **green**: 15 passed (13 existing + 2 new) |
| C | `87720ea32` | `tests/fake/test_state_contract.py`, `tests/unit/test_test_module_names.py`, `tests/unit/test_invariants_index.py` | **green**: 63 passed |
| L | `87720ea32`, knob off | `tests/frontend/test_core_live.py` | 1 failed, **not red or green**: the probe stopped at step 5, `approval.get` refused person-only, "the caller carries Subfleet's attempt markers" |

## What did not run

The brief's live runs did not run:
- `test_core_live.py` red at `946ea9f96` with the knob on;
- then green with it on, off, and off four more times.

**Why:** they can't pass under a Subfleet lane.
- The daemon's person-only peer check reads every ancestor of the caller. It refuses one that runs
  under a Subfleet guardian or carries `SUBFLEET_JOB`/`SUBFLEET_ATTEMPT`
  (`subfleet/conversations/peers.py:193-197`).
- So the probe stops at step 5, before step 14 (run L).
- At this host's load, the first four attempts didn't even get the development daemon up within
  the harness's 10 s.

**What has to run them:** CI or a terminal session that is not under Subfleet. The commands, from
the repository:

```
SUBFLEET_FAKE_LATE_INIT_S=2 uv run pytest -q tests/frontend/test_core_live.py   # at 946ea9f96 (+ 9fe84abda's knob): red, detail null
SUBFLEET_FAKE_LATE_INIT_S=2 uv run pytest -q tests/frontend/test_core_live.py   # at this branch's head: green
uv run pytest -q tests/frontend/test_core_live.py                               # knob off, five times: green
```

**Expected results.** This is inference from the code above, not observed.
- **At `946ea9f96` with the knob:**
  - the first try is answered `not-steerable`, because the fake holds `system init` for 2 s;
  - with no `into`, the steer is refused at once;
  - the check's detail is `null`.
- **At the head with the knob:** the steer is asked again at 0.5, 1.5 and 3.5 s, and is taken at
  the try after `system init` arrives.
- **Steps 3–4 with the knob:** the knob also delays the `count [fake:slow]` follow-up by 2 s. They
  already wait up to 60 s for streaming.

Also not done: fetching run 40273357312's artifact to read the daemon's actual `message.steer`
answer. There was no network.

## Fix round 1: "Send now" names its turn, an honest deadline, the fixtures' roots (#265 review r1)

Built in Subfleet job `20261026-042028-pr265-fix-r1` (a detached worktree; the commits are on the
job's HEAD and kept under `refs/subfleet-salvage/`). Not pushed. Review r1 (`44d4c8b49`) asked for
changes, with one P2 and two P3s, each executed by the reviewer and reproduced here first.

### 1. "Send now" names the turn the person saw (P2)

**The gap (reproduced at `44d4c8b49`).** `UIModel.steer` read `liveMessageID` when the click's
callback ran, not when the tray offered the action. The regression runs the review's input through
the real UI:
- `UIModel.open`;
- the queue tray built from the model's layout, as the conversation view builds it;
- the tray's "Send now" button, kept and clicked later;
- the daemon's own `message.steer` (`daemon_harness.ServiceServer`).

The sequence:
1. Host A runs. Queued message C heads the tray, and the tray offers "Send now".
2. The model reads the conversation again (a second `conversation.open`) after A completed. It now
   shows no live turn.
3. Successor B starts in the daemon, and the kept button is clicked.

At `44d4c8b49` the request was `{"message_id": C}`, with no `into`:
- **B unready** (its runner still catching up): the steer was refused `not-steerable` after one
  try. C stayed queued, with the status "Queued, not steered: the running turn could not take it".
- **B ready:** C joined B, a turn the person never chose. The daemon showed C `steering`,
  `steered_into` B, and B's runner wrote C's steer frame.

**The fix.**
- `QueuedMessage.steerInto` (`QueueTray.swift:33`) replaces the `canSteer` flag, which is now
  derived from it. It holds the running turn the row's "Send now" steers into, and it is set where
  the layout decides the offer:
  - `ConversationStoreState.layout` (`QueueTray.swift:329-333`) uses `queuedSteerHost`
    (`ConversationStore.swift:204`), the host `canSteer` already required;
  - `Timeline.layout(steerOffered:)` (`QueueTray.swift:173-177`) uses `liveMessageID`. The core
    probe's queue steps use this path.
- The tray row's button captures `steerInto` when it is drawn and passes it
  (`UIQueueTray.swift:220`). A row that names no turn offers no "Send now", so none is offered
  while no turn is live.
- `UIModel.steer(messageID:conversationID:into:)` (`UIModel.swift:844`) takes `into` as a
  non-optional argument and no longer reads `liveMessageID`.
- `QueueTray(model:conversationID:title:rows:)` (`UIQueueTray.swift:119`) wires the tray for the
  conversation view (`UIWindow.swift:419`) and for the view probe, so the regression clicks the
  tray exactly as the window wires it.
- `UIModel.send` steers only when it can name the turn (`UIModel.swift:820-821`); with none, the
  message queues. The composer already asks to steer only then, in the same main-actor turn, so
  nothing changes for it. The change means no caller can journal a steer without a turn.
- `ConversationEngine.steer`'s `into` has no default (`ConversationStore.swift:1119`), so every
  caller states it.

**Every steer call site, and where its `into` comes from.**

| Call site | Calls | Its `into` |
|---|---|---|
| Tray row's "Send now" (`UIQueueTray.swift:220`) | `QueueTray.steer(row, into)` | `row.steerInto`, captured when the button is drawn |
| `QueueTray(model:…)` (`UIQueueTray.swift:119-125`), used by `UIWindow.swift:419` | `UIModel.steer(…, into:)` | passed through from the button |
| `UIModel.steer` (`UIModel.swift:844`) | `ConversationEngine.steer(…, into:)` | its non-optional argument |
| Composer's Return (`UIComposer.swift:329-332`) | `UIModel.send(…, steer:)` | none passed; asks to steer only while `steerHost` is set |
| `UIModel.send` (`UIModel.swift:820-821`, the build round's `:818`) | `ConversationEngine.send(…, steer:, into:)` | `state.steerHost(forComposerOf:)?.messageID` when Return is pressed; steers only if it is set |
| `ConversationEngine.send` (`ConversationStore.swift:1107`) | `Outbox.enqueueSubmit(steer:into:)` | passed through |
| `ConversationEngine.steer` (`ConversationStore.swift:1119`) | `Outbox.enqueueSteer(into:)` | passed through, stated by every caller |
| `Outbox.enqueueSubmit`, for a message already journaled (`Outbox.swift:339`) | `enqueueSteer(into:)` | passed through |
| Test: live probe step 14 (`CoreProbeLive.swift:579`) | `engine.send(steer: true, into:)` | `host.key`, the turn it submitted |
| Test: outbox `submit` and `steer` steps (`CoreProbeScenarios.swift:164`, `:170`) | `enqueueSubmit`, `engine.steer` | the step's `into`; the no-`into` tests leave it out on purpose |

**What the daemon answers for an `into` that has ended.** `op_message_steer` checks `into` before
any runner check (`service.py:1345-1346`). An `into` that is not the conversation's live turn (another
turn runs, or none does) is refused `no-live-turn`, code 7, "the turn it was steered into has ended".
- The outbox treats that as final, not `not-steerable` and not transient (`Outbox.swift:441-459`).
  So the steer is refused after one try whatever B's readiness, and the regression shows it for
  both.
- C is neither dropped nor failed. The daemon never claims it, so it stays `queued` there. The app
  shows it queued, with the status "Queued, not steered: the turn it was sent to had ended".
- There is one window where the answer is the retryable `not-steerable` first: A still recorded as
  running while its runner has already gone (settling, `service.py:1349-1356`).
  - The app asks again at most five times in all, 0.5 s doubling.
  - Once A is recorded as ended, the next try gets `no-live-turn` and the steer closes.
  - `test_a_steer_refused_for_good_fails_the_check_however_long_step_14_waits` runs that path
    (`not-steerable`, then `no-live-turn`, C still queued).
  - Left as it is, as the brief asked.

**The regression** (`tests/frontend/test_queue_tray_view.py`, `QueueTrayViewProbe.swift`
`send-now`), with B unready and with B ready. In both:
- the tray offered "Send now" while A was live, with `steerInto` A;
- at the click the model showed no live turn, and a fresh tray would not offer it;
- every `message.steer` request was `{"message_id": C, "into": A}`;
- the steer was refused `no-live-turn` after one try, with no receipt;
- the daemon kept C `queued`, with no `steered_into`, and B's runner was never given C;
- the app shows C queued, with the status line above.

The control offers "Send now" while B runs: the request names B, and the daemon steers C into B.

### 2. Step 14's deadline is a hard elapsed bound (P3)

**The gap.** The 90 s wait started at the steer's first pump. It left out the steered message's own
submit, which that first pump waits for, and it checked the deadline only after each pump. Run on the
probe's clock, the review's input (a 15 s answer ahead of the steer, then five 15 s steer timeouts)
ended at 97.5 s.

**The fix** (`tests/frontend/CoreProbeLive.swift`):
- **`steerStepDeadline` (`:78`) counts from step 14's start** (`:560`), the prerequisite and the
  submit included. At the client's 15 s answer deadline it is 120 s:
  - 15 s for the prerequisite: the host's submit, and its wait to run and be accepted. That takes a
    second or two on CI; run 40273357312 reached step 14 within 15 s of the probe's start.
  - 15 s for the steered message's own submit.
  - 82.5 s for the steer: five tries with 7.5 s of backoff between them.
  - 7.5 s of margin for the pumps' journal writes.

  The outbox gets every try it is entitled to whenever the prerequisite takes 15 s or less. A slower
  prerequisite leaves the steer less, and the check's detail says how many tries it had.
- **`settleSteer` (`:117`) checks the deadline before every pump and before every wait**, and cuts
  each wait to the time left. Nothing starts at or after the deadline.
- **The pump takes the deadline.** `DeadlineTransport` (`:89`) sits under the live probe's client
  (`:165`). While step 14's pumps run, each exchange waits for its answer at most the client's
  timeout or the time left, whichever is less. An exchange that would start at or after the deadline
  is not sent, and reads as unanswered.
- **So the bound is hard:** step 14 judges its steer by 120 s after it began, plus only the pumps'
  own local journal writes, which wait on no answer.
- The check's detail adds `step_s` and `deadline_s`.
- The outbox scenario's `settle-steer` runs the same code on the probe's clock. A scripted daemon's
  exchanges take time on that clock: `cost` is the seconds before an answer, and `timeout` takes the
  whole timeout the client gave. `step-start` marks where the step began.

**Tests** (`tests/frontend/test_core_live_steer.py`, on the probe's clock at 15 s answers):

| Case | Ends at | Pumps | Waits | Steer |
|---|---|---|---|---|
| The review's input (15 s submit, then five steer timeouts) | 97.5 s | 5 | 0.5, 1, 2, 4 | refused `no-answer` after all five tries |
| Time spent in pumps (40 s prerequisite, then five timeouts) | 120 s exactly: the fifth try's exchange may wait only 12.5 s | 5 | 0.5, 1, 2, 4 | refused `no-answer` |
| Deadline in a backoff (54 s prerequisite) | 120 s exactly; no pump at the deadline | 4 | 0.5, 1, 2, 2.5 | still queued, 4 tries |
| Immediate success | 0 s | 1 | none | acknowledged, taken |

At `44d4c8b49` the first three ended at 97.5 s, 122.5 s and 136.5 s, against 90 s.

Two existing tests changed with the new semantics:
- the deadline at the harness's 3 s answers is now 36 s (3 + 3 + 7.5 + 15 + 7.5);
- `test_step_14_stops_at_its_deadline_and_judges_a_steer_still_waiting` now takes 2 pumps, where it
  took 3: the build round pumped once more at the deadline itself.

### 3. The steer fixtures stop leaking roots (P3)

`test_core_steer.py`'s `harness` and `daemon` fixtures, and the O1-O4 property harness in
`test_core_steer_properties.py`, made their roots with `mkdtemp` and never removed them, about 256 KiB
each. Each now lives in a `tempfile.TemporaryDirectory`. Teardown removes it, also when the test body
raises, and the `daemon` fixture closes its server and harness first.

The root counts:
- `test_the_steer_fixtures_leave_no_root_after_teardown` runs both fixtures in a scratch pytest
  session, passing and raising (4 tests). Every root `mkdtemp` made meanwhile is gone afterwards,
  the socket server's folder included.
- `test_the_property_harness_leaves_no_root_after_each_example` runs the property test's own body
  with its history replaced, returning and raising. Both roots are gone.

At `44d4c8b49` the fixtures left 4 roots and the property harness 2.

The hub folded this into the PR. The reviewer had suggested a separate one.

### 4. The contract

C-29.7 now says that "Send now" names the turn the tray showed when it offered the action, captured
with the action. A click that lands after that turn ended is refused, `no-live-turn` while the message
still waits, so the message stays queued, never joins the next turn, and its status line says why.

The clause's last sentence ("a journaled steer names the turn it was sent to") did not hold for
"Send now". It now names both sources: Return's turn when Return is pressed, and "Send now"'s offered
turn. The change log has an entry. C-24.9 is unchanged: the daemon's behaviour is the same.

### Runs (round 1)

**Environment:**
- Scratch `$(getconf DARWIN_USER_TEMP_DIR)sf-r265f1`, with a venv on Homebrew `python3.14`.
- Every pytest ran in the foreground under `suite_hold.sh`; none ran outside the lock. xdist isn't
  installed, so nothing used `-n`.
- The probes were compiled outside the holds into the checkout's probe cache.

| Run | Code | Files | Result |
|---|---|---|---|
| Red | tests at `e9c23defe` on `44d4c8b49`'s app | the 4 files below, `-k` the new tests | **red**, 7 failed, 2 passed (the two controls) |
| Hold 1 | `6ba0b70b4` | `test_core_live_steer.py` 16, `test_core_steer.py` 44, `test_core_steer_properties.py` 3 | **green**, 63 passed |
| Hold 2 | `c3b71ea70` | `test_queue_tray_view.py` | **green**, 18 passed (load 45) |
| Hold 3 | `38c5e22e3` | `tests/fake/test_interactive_steer.py` | **green**, 15 passed |
| Hold 4 | the docs commit, with this report and C-29.7 | `tests/fake/test_state_contract.py` 37, `tests/unit/test_test_module_names.py` 1, `tests/unit/test_invariants_index.py` 25 | **green**, 63 passed (load 50) |

The red run failed 7 tests:
- both "Send now" cases;
- the three deadline cases;
- both root counts.

Two of hold 2's runs before `c3b71ea70` errored, so that commit exists:
- They ran at load 57-65 and 41-59, with 15 errors and the 3 "Send now" tests passing.
- The views fixture's layout probe ran past its 60 s timeout. Run alone, it took 67.9 s.
- The red commit had made the probe's `main` async. The same layouts under the original synchronous
  `main` took 42.4 s at the same load, so `c3b71ea70` put it back and runs "Send now" as a
  main-actor task.

**Mutants** (each applied by hand, run, and reverted):

| Mutant | Killing test file | Result |
|---|---|---|
| `UIModel.steer` reads `liveMessageID` at call time again | `test_queue_tray_view.py` | **killed**: 2 failed (both "Send now" cases: the request has no `into`), 16 passed |
| `settleSteer` checks the deadline only after a pump | `test_core_live_steer.py` | **killed**: 2 failed, 14 passed |
| The `harness` fixture without cleanup (`mkdtemp`, no removal) | `test_core_steer.py -k leave_no_root` | **killed**: 1 failed (2 roots left) |

The deadline mutant's two killers:
- `test_step_14_stops_at_its_deadline_and_judges_a_steer_still_waiting` took 3 pumps, not 2;
- the deadline-in-a-backoff case took 5 pumps, not 4.

Under that mutant the transport still held the time bound, but a pump ran at the deadline.

**Not run:** `tests/frontend/test_core_live.py`. A Subfleet lane's person-only check stops the probe
at step 5, so CI runs it, step 14's new deadline included.
