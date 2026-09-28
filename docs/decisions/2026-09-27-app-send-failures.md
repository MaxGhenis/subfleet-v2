# Sends that are not going through: decisions (2026-09-27, revised 2026-09-28)

On 2026-09-27 the app was found to send a failing message again in silence, without end, and to show it as "Sending" for good (Max, 2026-09-27: sends that are not going through). The calls below were delegated to the lane that built the change, on branch `fix/outbox-failure-surface` cut from release/217 at 03de432d. That lane was cut off before it committed. On 2026-09-28 its edits were committed as fe624609 and moved onto the queue tray (C-29.7, `feat/queue-tray` c94f7b34) as 965dd135, with conflicts resolved in the tray's favour. Revision 2 (2026-09-28) puts the notice on the queue tray, because under C-29.7 a message waiting behind something leaves the timeline for the tray (§5). Everything said below about the app was read in `app/Sources` at 965dd135 (before the change: at 03de432d). Everything said about the daemon was read in `subfleet/` at 965dd135, whose `attachments.py` and `service.py` match origin/release/217, and on `fix/attachment-followups` at 5402c681. Contract: C-28.3 as amended, and C-29.12.

## 1. The problem

Facts, from `app/Sources` at 03de432d:

- `Outbox.classify` queued a failed send again whenever nothing said it failed on its merits: no whole answer (the daemon down, a timeout, a broken or malformed answer), exit 1 (an operational failure) or exit 69 (busy). The backoff is 0.5 s doubling to 30 s (`Outbox.backoff`), and there was no cap on tries.
- `OutboxFailure` kept the code, the reason, the message and `retryable`. The daemon's `fix` was dropped.
- No view read an outbox entry's failure. The person's bubble said "Sending" (`TurnTimeline.statusText` for a message with no receipt), with a spinner, for as long as the outbox kept trying, and it never stopped.
- A send refused on its merits went `failed`. The status line said "N message(s) could not be sent; see the conversation", and nothing in the conversation said which message or why.
- `Outbox.sendable` offers only the first open message of each conversation (D-22), and `failed` counts as open. So a message that kept failing, or had stopped, also held every later message of its conversation, and each of those said "Sending" too.
- After a relaunch, a message the previous run had left unsent had no row. The timeline gained rows only from the composer's send and from the daemon's receipts.

The case that showed it. `OutboxSender.send` registers each staged image with `attachment.add` before its `message.submit`, and an error from either is the send's failure. When a directory sits at a stored image's name (`attachments/<sha256>.<ext>`), the add cannot rename its copy into place. On release/217 (`attachments.add`, `_copy`) the error reaches the catch-all in `ConversationService.respond`, which logs it and answers exit 1, "operation failed; inspect daemon status", with no reason and no fix. On `fix/attachment-followups` (a673b64, 5402c681) the daemon names the case: exit 1, reason `copy-blocked`, "a directory is in the way of the stored copy at <path>", and the fix "remove the directory at the stored copy's name, then add the image again". Either way the app sent the message again every 30 s, for good, and dropped the fix. The review of 5402c681 found this by reading `app/Sources` and left the app's side as a follow-up. This change is that follow-up.

## 2. Keep retrying transient failures, with no cap; stop only for the person's fix or a refusal

Decision (`Outbox.classify`, `DaemonError.needsPerson`, C-28.3):

- No whole answer, exit 1, exit 69, or the development build's refused endpoint: queued again with the backoff, and no cap on tries (unchanged).
- Exit 2 `out-of-order`: queued again once the conversation has been re-read (D-22, unchanged).
- Exit 1 with a reason in `DaemonError.personActionReasons`, which is only `copy-blocked` today: `failed`, `retryable` false, no next attempt. It waits for the person's Try again or Withdraw.
- Anything else, meaning a refusal on its merits (exit 2 other than `out-of-order`, 7, and the other codes) or a request over the daemon's 1 MiB limit that the app cannot send: `failed`, as before.

Why no cap. A resend is safe: it carries the same key, and a repeated message id with the same digest returns the stored receipt (C-24.2). Exit 1 and 69 are the daemon's answers to conditions that can pass without the person: busy at its connection cap (69, fix "try again shortly", `daemon.busy_answer`), a conversation store already closed as the daemon stops (`store-closed`, exit 1), a stored copy that did not verify (`copy-mismatch`, "try again", exit 1), or an op that raised (the catch-all above). A cap would turn each of these into a message the person has to notice and send again, and until then it would hold every later message of its conversation. The notice in §4 shows the retrying and gives the person the choice, so no cap is needed.

Why the list is explicit. An exit-1 answer stops only when its reason is on the list. Adding a reason is a deliberate change to `Protocol.swift`, made with its test. A reason the daemon adds later is retried until then, and retrying is harmless because of the keys.

## 3. Journal the daemon's fix and the count of failures in a row

Decision (`OutboxFailure`):

- `fix` is the error body's `fix`. `consecutive` is the number of failed answers in a row, this one included. Both fields are optional, so a journal written before them still decodes. `count` is `consecutive ?? 1`, so an old failure counts once.
- `classify` sets `consecutive` to the previous failure's count plus one. An `out-of-order` answer counts too.
- An acknowledgement, a tombstone, or a created conversation clears the failure (`Outbox.finish`). Send now, Try again and a relaunch keep it, and its count, until the next answer. A Try again that fails again counts on from where the count was.
- `detail` is the message without the `<reason>: ` prefix that `ConversationService.respond` writes before every `ConversationError` answer. The prefix is removed only when that exact prefix is there.
- `needsPerson` is worked out from the stored code, message and fix, so it still holds after a reload.

## 4. What the person sees: a notice from the second failure, or at once when the send stopped

Decision (`SendNotice`, `Outbox.notice`, `Outbox.notices`):

- A send that stopped (`failed`) shows at once.
  - `needs-person`: "Not sent: this needs you first", then "When that is done, choose Try again."
  - `refused`: "Not sent: the daemon refused it", or "Not sent: the app cannot send it" when the failure has no code.
  - Its actions are Try again, plus Withdraw for a message.
- A send the app is still trying (`queued` or `sending`) shows from its second failed answer in a row (`Outbox.noticeAfterFailures`, 2).
  - `retrying`: "Not sent yet: N tries failed; the app keeps trying".
  - Its actions are Send now while it waits out its backoff (not while a send is under way), plus Withdraw for a message.
- Every notice but `waiting` carries the daemon's words without the reason prefix, its fix, its reason and the count.
- In the same conversation, each later message with no notice of its own is `waiting`: "Waiting: a message above has not been sent". Its only action is Withdraw. The outbox sends a conversation's messages in order, so it goes only after the stuck one.
- Send now and Try again both call `Outbox.sendNow`. A stopped send is queued again, unchanged (`retry`). A queued send is due at once. Neither applies to a send that is under way.
- Withdraw is D-22's withdrawal (`ConversationEngine.withdrawSend`, the same path as the queue tray's Withdraw).
  - A send never sent is dropped.
  - A send the daemon never received is withdrawn with a tombstone, after `message.status` confirms it never arrived.
  - A send the daemon has is cancelled or stopped, as its state requires.
- A notice shows only while the app has no receipt for the message. Once the daemon has the message, its state says more.
- A `waiting` notice shows only while an earlier message of the conversation shows its own notice (`ConversationStoreState.sendNotice`, `stuckSend(in:before:)`).

Why the second failure. A single failure is retried after 0.5 s. A notice from the first failure would flash for a one-off busy answer that the next try clears. A stopped send does not go on its own, so it shows at once.

While the app shows the daemon as unavailable (C-29.2), `UIModel.pump` sends nothing. Nothing counts up then, and the availability banner explains why.

## 5. Where it shows

Decision: each notice shows in one place, where the message's row is. The one exception is the messages that wait behind a stuck message: each says so on its own row.

- In the timeline, under the person's bubble, when nothing is ahead of the message. C-29.7 keeps that message at the timeline's end.
  - The bubble's status line shows the notice's status instead of "Sending". It is orange while the app keeps trying and red once the send has stopped, with no spinner once stopped.
  - Below it, `SendNoticeView` shows the daemon's words, "To fix: <fix>", what to do next, and the actions.
  - The status line's own Withdraw is hidden, because the notice has one (`TurnStatusLine`).
- On its queue-tray row, when it waits behind something: a live turn, a block, or an earlier waiting message (C-29.7, 2026-09-28).
  - The row's status is the notice's, orange while the app keeps trying and red once the send has stopped, with the `exclamationmark.bubble` icon.
  - A second caption says "To fix: <fix>", or the daemon's words when there is no fix.
  - Send now or Try again sits before the row's own Withdraw, which is the outbox's withdrawal for a send with no receipt.
  - The row's tooltip holds the whole notice.
  - `ConversationStoreState.layout` attaches each tray row's notice (`QueuedMessage.attach`, `noticeLine`, `noticeActions`), and it is the view's only source for the layout.
- The messages behind a stuck one say they wait, in grey, with no second caption. They are always tray rows, because the stuck message is ahead of them.
- Above the composer, while a message of the conversation is stuck, a note says: "A message here has not been sent. What you send now waits behind it until it is sent or withdrawn." (`stuckSend(in:)`).
- A new conversation whose create is not going through has no conversation view yet. A banner in the main window names its workspace and carries Send now or Try again (`CreateNoticeBanner`, `createNotices`). It offers no Withdraw, because the outbox withdraws a create only before it was ever sent (`Outbox.planWithdraw`).
- The sidebar row shows `exclamationmark.bubble` for the worst notice among its messages (`sendProblem`): needs-person, then refused, then retrying. It is orange for retrying and red otherwise, and `waiting` never counts.
- When a pump stops a send, the status line points to the notice: "A message was not sent; its conversation says why." or "A new conversation did not start; the banner above says why.", with counts when several stopped (`UIModel.stoppedWords`).
- Unsent messages a previous run left behind are restored as rows (`restoreUnsent`, called from `apply(sends:)` and `focus`), so their notices show where the messages show.
  - Each open `message.submit` of the conversation that the timeline has no row for becomes a local row, with its journaled text, attachments and settings, in outbox order.
  - A row the daemon has sent a receipt for is never changed.
- When the app reads the outbox's open entries (`UIModel.refreshSends`, `applySends`):
  - when it starts, without waiting for the daemon;
  - after each send and create;
  - after Send now, Try again and Withdraw;
  - on every pump (every 2 s), before the pump's early return, so a send that keeps failing still updates its notice.
  - It updates the state only when the entries changed.

Why the tray. After C-29.7, a message that waits behind something is not in the timeline, so a notice under its bubble would have no bubble to sit under. The notice goes with the row. The only message that waits in the timeline is the one with nothing ahead of it, and its bubble shows the notice.

## 6. Alternatives considered

- Cap the retries (for example five tries, then `failed`). Not taken, for the reasons in §2. A cap turns a condition that can pass without the person into a message the person must find and send again, and until then it holds the conversation's later messages. The notice gives the person the choice without a cap.
- Show a notice from the first failure. Not taken, for the reasons in §4.
- Stop on every exit 1. Not taken. Exit 1 is the daemon's answer to many operational failures: its catch-all, `store-closed`, `state-root-gone`, and `copy-mismatch`, whose own words are "try again". A resend clears most of them, and the person would have to press Try again for each one. Only a reason whose fix is the person's stops, and those reasons are named one at a time.
- A global alert only: the old status line, or a window-wide banner. Not taken. It does not say which message is stuck, what the daemon said or what to do. It cannot offer Send now, Try again or Withdraw on the message, and the person cannot tell which of the later messages it holds. The status line stays, and it now points to where the notice is.

## 7. On release/217 and integrate/219

The daemon on those lines does not send `copy-blocked` yet. That answer is on `fix/attachment-followups` (a673b64, 5402c681), and neither line contains it (checked 2026-09-28). On those lines a directory at a stored image's name is answered with the generic exit 1, "operation failed; inspect daemon status", with no reason and no fix. The app therefore treats it as transient, and the case plays out like this until that branch lands:

- From the second failure, the message shows as `retrying`, with those words, Send now and Withdraw, and no fix.
- The app keeps trying, every 30 s once the backoff has grown to that, until the directory is removed or the person withdraws the message.
- The daemon's log names the error.

Once the branch lands, the same case stops at once as `needs-person`, with the daemon's fix and Try again.

## Tests

- `tests/frontend/test_core_send_failures.py`: the outbox's side, through the core probe. It covers the classification of each answer, the count of failures in a row, no cap, the notices and their actions, `waiting`, an older journal, Send now and Try again, the reason prefix, and `needsPerson`. It includes Hypothesis properties checked against a reference model.
- `tests/frontend/test_core_send_failure_placement.py`: where each notice shows, using the store's layout over generated conversations. It covers the timeline or the tray (never both), the tray row's status and actions, `waiting` rows, restored rows, `stuckSend`, `sendProblem` and `createNotices`.
- `tests/frontend/test_menu_view.py` compiles every app source with `SUBFLEET_VIEW_TEST`, the views above included. The core probe does not compile the views.
