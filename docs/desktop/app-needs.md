# What the desktop app needs from the daemon

Gaps found while building the app's non-UI core (`app/Sources/`) against
`subfleet/conversations/` on branch `feat/desktop-app`, 2026-09-24. The app
does not change daemon code; each item says what the app does meanwhile. Items
marked *verified* were reproduced against the daemon's code this session;
the others are read from the code cited.

## Blocking or visible to the person

1. **The person-only check cannot match an app path with a space** (*verified*).
   `peers.judge` takes the executable as `caller.command.split(" ", 1)[0]` of
   `ps -o command`, so `/Applications/Subfleet Dev.app/Contents/MacOS/Subfleet Dev`
   (or `build/Subfleet Dev.app/…`) is read as `…/Subfleet` and never matches
   `SUBFLEET_DEV_APP_EXECUTABLE` or `APP_EXECUTABLES`: approvals, unblock and
   resolve are refused (exit 7) for such a build. `judge(…)` with a command of
   `…/Subfleet Dev.app/Contents/MacOS/Subfleet Dev HOME=…` returns
   `person=False`; the same without the space returns `the Subfleet app`.
   Meanwhile `app/build.sh --dev` builds `SubfleetDev.app` with executable
   `SubfleetDev` (display name "Subfleet Dev"). Ask: read the peer's executable
   with `proc_pidpath` (libproc) instead of splitting the command line.

2. **`status.json` has no model-scoped windows** (D-27, C-29.6). `status_json._windows`
   keeps only readings with `scope == "account"`, and a Claude account carries
   only `five_hour` and `seven_day`. There is no Fable (or other model-scoped)
   weekly window, no Claude `earliest_reset`, and job rows carry no `kind`
   (D-26), so the menu cannot group turn jobs by conversation. The app decodes
   what is there (`StatusModel.swift`, unchanged) and cannot show the Fable
   weekly window yet. Ask: publish every model-scoped window per Claude account
   (`windows: {"<scope>:<window>": {used_percent, reset_at, status}}` or
   similar), `earliest_reset` for Claude, and `kind` on job rows.

3. **`turn.diff` and `conversation.diff` do not exist** (D-25, C-29.10). They
   are in design §5 but not in `CONVERSATION_OPS` or the service, so the Changes
   pane cannot be built.

4. **The request line limit equals the message limit.** The daemon reads at most
   1 MiB per request line (`daemon._connection`), and `capabilities.limits`
   advertises `message_bytes: 1048576`. A message near that size, once
   JSON-escaped inside the envelope with its settings, cannot be sent. The app
   refuses a request line over 1 MiB before sending (`DaemonClientError.requestTooLarge`,
   a permanent outbox failure). Ask: advertise the request line limit in
   `limits`, and keep `message_bytes` well under it.

## Approvals

5. **Resolved (2026-09-28): approval views identify the provider request.**
   Events carry the provider's `request_id`; `approval.list`,
   `conversation.open` and `approval.get` views carry `approval_id` and the
   provider's request id twice: as `request_id` (C-27.5) and as
   `provider_request_id` (C-27.1), the same value. The app joins by message and
   exact provider request, including when the list arrives first or the event
   log is replayed, so identical display text cannot attach a replacement
   approval to a stale card. A view from a daemon older than both fields,
   which names no request, stays on an independent actionable card keyed by
   immutable approval id. Unmatched event cards have no answer controls; a
   whole pending-list reconciliation withdraws them. Kind and display alone
   never join cards, so a legacy replacement cannot inherit stale drafts.

6. **A withdrawn approval has no event and no change row.** When a turn ends,
   the driver withdraws pending requests (`_end` returns them as `resolved`)
   without an `approval.resolved` event, and `store.withdraw_approvals` writes
   no `changes` row. The app marks a turn's pending cards withdrawn on
   `turn.completed`; the badge corrects itself with the next change row for
   that conversation (the settling `set_state` writes one). Ask: an
   `approval.resolved {decision:"withdrawn"}` event per withdrawn request, and
   a change row.

7. **`changes.pending_approvals` has two meanings.** Rows written by
   `add_approval` and `answer_approval` carry the count for the *message*;
   every other row carries the count for the *conversation*. With one live turn
   per conversation they agree; the app treats the value as the conversation's.
   Ask: always the conversation's count.

8. `approval.respond` returns `{approval, duplicate?}`, not the `receipt` design
   §5 lists; the app reads the message's state from the watch feed.

## Events, history, catalog

9. **Compaction is never run** (design §3, C-25.4). `store.compact` exists but
   nothing calls it, so `conversation.events` never answers `reset: true` and
   delta rows are kept forever. The app handles `reset` (drops the event-derived
   timeline and reads again from 0); the frontend tests exercise it by calling
   `store.compact` directly.

10. **History paging drops blocks** (*verified*). `history._claude_items` stops
    after a whole transcript row, truncates to `limit`, and returns the cursor of
    the last item kept; the next page skips every item of that row. A row with
    three text blocks paged with `limit: 2` returns two blocks, then the next page
    starts at the previous row: the third block is never returned. Codex history
    has no paging (`next_before` is always null) and no row ids. Ask: a cursor
    that names the block within a row, and ids for Codex rows.

11. **Receipts carry no text.** The app shows the person's text from its own
    journal or from the transcript's user row whose uuid is the message id
    (Claude only). A message another client sent to a Codex conversation shows
    without its text. Ask: the message text (scrubbed, bounded) in `_receipt` or
    a `message.text` op.

12. **`conversation.list` filters one side each.** `provider` filters only the
    conversations, `query` only the catalog. The app filters both locally
    (`ConversationStoreState.sidebarEntries`), so a provider filter can show
    fewer catalog items than `limit`. The catalog's `next` cursor is an mtime
    compared as strings (`str(mtime) >= str(before)`), which is wrong only for
    mtimes with a different number of integer digits (files from before 2001).
    Ask: filter both sides server-side; compare the cursor as a number.

13. **No current watch cursor.** To post no notifications for what happened
    before launch, the app drains `conversation.watch` from 0 with `wait_s: 0`
    until an empty page, every launch; the `changes` table grows with every state
    change. Ask: `conversation.watch {after: -1}` answering `{changes: [], next:
    <max seq>}`, or the cursor in `capabilities`.

14. `_view.active` is false while the last message is `queued`, even when an
    earlier message's turn is running. The app derives activity from the watch
    feed (live message states per conversation) instead.

15. `catalog.refresh` returns `{requested, running}` without the `generated_at`
    design §5 lists.

## Served facts

16. Claude `served` has no effort, and Codex `served` has no account. The chip
    falls back to the message's requested effort, and to the lane's label from
    `status.json` for a Codex lane. Codex's thread `serviceTier` is null at
    standard speed, and served facts drop nulls when merged (as the runner
    does), so the app reads "Fast off" for Codex once a served model is known.

## Mutating ops without idempotency keys

17. D-22 and C-28.3 ask the app to journal every mutating op and resend it after
    a restart with its original key. Only `conversation.create` (`request_id`)
    and `message.submit` (message id) have keys; the app journals those two.
    `conversation.settings`, `message.cancel`, `turn.interrupt`,
    `conversation.unblock`, `message.resolve` and `approval.respond` are sent
    directly and not replayed after a restart: they are a person's immediate
    decisions, the state they act on may have changed since, several are
    person-only confirmations, and journaling `approval.respond` would put its
    nonce on disk. After a restart the person sees the unchanged state (the card,
    the banner, the running turn) and acts again. Ask, if replay is wanted: a
    client request id on each, answered idempotently.

## Error shape

18. Refusals have no structured reason: `error.message` is
    `"<reason>: <message>"` (`service.respond`), and protocol errors have none.
    The app parses the slug before the first `": "` (`DaemonError.reason`), which
    `out-of-order`, `person-only` and every `ConversationError` follow. Ask: an
    `error.reason` field.
