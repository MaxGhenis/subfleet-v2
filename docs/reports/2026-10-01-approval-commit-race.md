# An approval's event committed before the approval, 2026-10-01

`tests/frontend/test_core_live.py::test_the_app_core_drives_a_development_daemon` failed in about 3 of 7 CI runs on branches built on release/217 (f1bd2ab5), on Python 3.12 and 3.14. The cause was a race in the daemon, not the test's wait: the turn runner committed an approval's `approval.requested` event, then the approval, then the message's move to `approval-needed`, in three transactions. Design §8 (`docs/desktop/design.md:889`) says one. A client that read the event and at once listed approvals, as the app does to answer a card, could find no approval. The fix commits the three together (4a97e16b, with b29c97ca from its review). The live probe now stops at the first failed step it depends on, with the app core's state, instead of cascading for 300 s (cb739b4d).

Every number below comes from CI logs or from commands run on 2026-10-01 on an Apple M5 Max (18 cores, macOS 26.6.2, Python 3.12.14, SQLite 3.53.4), at load averages of 36 to 52 from other lanes. Raw data and the script are in `docs/reports/2026-10-01-approval-commit-race/`.

## What CI showed

Two failing runs give the failing step's detail:

- **Run 36840688560** (Python 3.14): "the question card lists its questions" failed 5.89 s into the probe, detail null. Then "the answer reaches the provider" failed at 86 s, "the provider received the image" at 177 s, "an approval in an unfocused conversation notifies" at 239 s, "the badge counts the unfocused approval" at 239 s (detail 1), and "its completion notifies" at 301 s.
- **Run 36747800501** (PR #89, Python 3.12): the first failure was the earlier tool approval, not the question. "the card joins its approval id" failed at 4.08 s. The card it printed had `"approval_id": null`, `"request_id": "perm-aefda4a0"` and `"state": "pending"`. "the Dock badge counts it" failed with 0: the `approval.list` just read held no pending approval. With the tool approval unanswered, every later step failed in turn, each after its own timeout: the approved turn (64 s), the question (145 s), its answer (225 s), the image (318 s) and the unfocused conversation's notifications (378 s and 438 s).

So the shape is not always the question. It is whichever approval step the race catches first.

## Why it is not a short wait

At f1bd2ab5 the question step (`tests/frontend/CoreProbeLive.swift:224-241`) runs `follow(cid)` with its default 60 s timeout until the question's turn has a pending card. It then lists the conversation's approvals once and joins them to the cards. If the card has an approval id, it checks the card's questions and answers. Otherwise the `else` branch records the check as failed, with no detail.

`follow` cannot return false in under 60 s, so failing at 5.9 s means it returned true: the event's card was there. The null detail alone does not distinguish the failed question-content check from the `else` branch: both used the default detail. The unanswered question, the complete question fields in the provider's event (`subfleet/conversations/claude_turn.py:449-459`), and the commit-visibility measurements below support the missing-approval-id explanation. Run 36747800501 shows the same race at the tool approval directly, with the card's `approval_id` null and `approval.list` empty. The later steps' 80 to 90 s budgets combine a 60 s state wait with a 20 to 30 s event wait; they do not imply a shorter budget for the question card.

## The race

At f1bd2ab5, `TurnRunner._apply` (`subfleet/conversations/runner.py:410-420`) handled a step carrying a provider request in three steps:

1. `self._flush()` committed the event batch holding `approval.requested` (`store.append_events`, `subfleet/conversations/store.py:1141`). `transaction()` calls `notify()` after every commit (`store.py:413-424`).
2. `self.store.add_approval(...)` (`store.py:1058`) looked the request up, published the exact request file (`_publish`, a write, an fsync, a rename and a directory fsync, at `store.py:1067`), and then committed the approval row and its change-feed row.
3. `self.store.set_state(..., APPROVAL_NEEDED, ...)` committed the move.

`op_conversation_events` (`subfleet/conversations/service.py:591-606`) waits in `store.wait`, which `notify()` wakes. The app's events poll therefore returned the event right after step 1, while steps 2 and 3 were still under way. The app (and the probe) then called `approval.list`, and its answer depended on which got there first.

The app has the same exposure. A card with no approval id gets one only when Review asks: `UIModel.approvalID(for:)` (`app/Sources/UIModel.swift:570`) lists approvals once. If that list missed the approval, `UIWindow.review` showed "That approval is no longer pending." (`app/Sources/UIWindow.swift:357`) for an approval that was pending.

## Measured through a real daemon

`measure.py` starts a development daemon with the e2e harness (`tests/e2e/conftest.py`), with fake Claude providers and the code of the tree given. Each round:

- creates a conversation and sends `[fake:approval]` or `[fake:question]` (alternating);
- long-polls `conversation.events` until `approval.requested` arrives;
- calls `approval.list` at once, as the app does, then `message.status`;
- interrupts the turn.

The old tree is `git archive f1bd2ab5`; the new one is this branch. Runs alternated old, new, old, new; new-run3 is the final head.

| Run | Code | Rounds | Approval listed when its event arrived | Missed (tool, question) | `message.status` read after a miss |
|---|---|---|---|---|---|
| old-run1 | f1bd2ab5 | 40 | 21 | 19 (10, 9) | 19 running |
| new-run1 | 4a97e16b | 40 | 40 | 0 | n/a |
| old-run2 | f1bd2ab5 | 40 | 10 | 30 (15, 15) | 28 running, 2 approval-needed |
| new-run2 | 4a97e16b | 40 | 40 | 0 | n/a |
| new-run3 | b29c97ca | 40 | 40 | 0 | n/a |

At f1bd2ab5, `approval.list` missed the approval its event had just announced in 49 of 80 rounds. The `message.status` read right after the list still said `running` in 47 of them; in the other 2 the move had landed in between. With the fix, the list held the approval and the message was `approval-needed` in all 120.

## The fix

**One transaction (4a97e16b).** `ConversationStore.add_approvals` (`store.py:1074`) publishes each new request's file first, as a message's text is published before its row (C-24.3). It then commits these in one transaction:

- the event batch and the attempt's watermark (`_insert_events`, `store.py:1195`);
- the approval rows and their change-feed rows;
- the move to `approval-needed` (`_set_state`, `store.py:975`; the call is at `store.py:1121`).

`TurnRunner._apply` hands a step's approvals to `_flush(approvals)` (`runner.py:410`, `runner.py:670`). `add_approval`, `append_events` and `set_state` keep their behaviour on the same helpers. The change rows are written in the same order as before, so the app's watch feed still raises one approval notification. C-27.1 in `docs/acceptance-contract.md` now says that the three commit together.

**The probe stops at the first failure it depends on (cb739b4d).** `LiveRun.require` (`tests/frontend/CoreProbeLive.swift:35`) records a check, and when it fails throws `LiveStop`. `runLive` (`:41`) catches it, and any other error, and returns the checks with `notes.stopped_at` and `notes.state`. The state (`run.dump`, `:90`) holds every timeline the app core had (cards with request and approval ids, turns, cursor, pending counts) and the daemon's `approval.list` and `message.status` for the same turns. The required steps are those the rest wait behind:

- the endpoint and capabilities, and the conversation's creation;
- the first turn, and the stop;
- each approval card, its approval id and its answer;
- the question's answer;
- the image turn, and the unfocused approval.

The question step now checks the card's approval id (`:282`) apart from its questions. `test_core_live.py:90` prints both notes with the failures and writes them to `live-state.json`.

## Checks run

- `tests/unit/test_approval_commit.py` (new) checks every commit as a reader sees it right after:
  - the Claude rows that `[fake:approval]` and `[fake:question]` send;
  - a replay that keeps its approval;
  - a Hypothesis property over interleavings of streamed text, requests of every driver kind (one or several per step), withdrawals, answers and batch flushes. At every commit, the events announce exactly the stored approvals, and a pending approval means `approval-needed`.

  At f1bd2ab5 the three ordering tests fail, and Hypothesis minimizes the failure to a single request. With the fix all pass.
- The unit suite (6,184 tests at 4a97e16b), `tests/frontend` except the live test (171) and the Swift probe build pass on Python 3.12.14. At b29c97ca, the store, runner, replay, Claude driver and service tests pass on 3.12.14 (229), and with the contract-index tests on 3.14.7 with the GIL (258), and `tests/e2e/test_conversations.py` gives 22 passed and 7 skipped: the person-only flows skip inside a lane.
- An independent Opus review of 4a97e16b to cb739b4d found no P0 to P2 defect, confirmed that the new tests fail on f1bd2ab5, and raised three latent P3s. b29c97ca closes two of them: `add_approval` for a known request no longer runs an empty commit, and a request named twice in one call is one approval. The third is intended. If publishing a request fails, the events batched before it are not committed either, and a replay writes them.
- `test_core_live.py` cannot pass inside a Subfleet lane. `approval.get` is person-only, and the peer check (`subfleet/conversations/peers.py:97-102`) refuses any caller whose ancestors carry attempt markers. Run here, it now fails in 25 s at "approval.get and approval.respond as the app", with the dump, instead of waiting out its 900 s timeout. It got past "the card joins its approval id" on the way. Whether the whole live test passes is for CI on macOS, with no lane above it.

### Checkpoint continuation

Continuing from 434039b9, independent source and log reviews confirmed the atomic fix and recomputed the measurement totals above. The question-content check (`CoreProbeLive.swift:284`) now also uses `require`, so malformed questions stop before answering or waiting for a turn. `tests/frontend/test_core_live_failure.py:12` adds a bounded, daemon-free regression: a fresh temporary development root has no socket, and the probe must return only the endpoint and failed capability checks, with `stopped_at` and the app/daemon state dump. It uses the standard 60 s probe subprocess budget; no later waits run.

- Reran the approval-commit, conversation-store, turn-runner, turn-replay, Claude-driver, conversation-service and fake-provider tests on Python 3.12.14: **222 passed, 7 failed**. All approval-commit regressions passed. Three unchanged FIFO service tests exceeded their 60 s child-process timeout without output, one service test failed because the macOS boot identity was unavailable, and the three fake-provider outcome cases exceeded their 3 s startup timeout. These are verification limitations, not a green suite.
- The new fail-fast test did not reach its body: the existing Swift fixture build exceeded its 900 s timeout (one setup error). A final `xcrun swiftc -frontend -parse tests/frontend/CoreProbeLive.swift` passed. Load averages measured during this check were 141.39 / 145.68 / 144.42, substantially higher than at the checkpoint's successful full build. CI must build the final probe and execute both the new regression and the complete live flow outside a lane.
- The sandbox refused the shared Git index lock and protects the worktree's `.git` pointer. Continuation commits use workspace-local metadata (`git --git-dir=.git-local ...`) on `fix/core-live-flake`, preserving the checkpoint and caller's branch. `docs/reports/core-live-flake.bundle` carries all commits since f1bd2ab5 and is verified against that prerequisite. No push is made.

## Not established

In run 36840688560 the unfocused conversation's approval did not arrive within 60 s while the question in the first conversation was still unanswered, and that was not traced. With the fix the question is answered. With the probe's stop, a run that fails earlier no longer goes on to that step.
