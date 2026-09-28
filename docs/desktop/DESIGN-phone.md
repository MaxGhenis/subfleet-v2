# Subfleet on the phone through Telegram

Design of record, 2026-09-28. Written before implementation against `52ffde17`.
Acceptance: C-31.1 in `docs/acceptance-contract.md`.

## Purpose and boundary

Max can answer the conversation that needs him regardless of the Claude or Codex
account serving its turn. This uses the chief-of-staff gateway, not provider Remote
Control. Context: `~/reviews/subfleet-ux-vs-claude-code-2026-09-28/REPORT.md`, section
21 and open question 4; Max's reply that account switches prevented phone use.

`~/chief-of-staff/bin/tg-poller` remains the **only** `getUpdates` consumer. Subfleet
never reads Telegram credentials, polls Telegram, starts a second listener, or
sends through anything but `bin/say`. The daemon remains the sole writer of its
conversation store. The phone CLI is a client of its existing local socket.

## Cards and policy

`phone.telegram` is an object in `policy.json`:

```json
{"phone":{"telegram":{"enabled":true,"events":["approvals","questions","blocks"]}}}
```

Omission uses these defaults. `enabled: false` disables new deliveries and phone
mutations; `events` selects these event names plus optional `done`. Unknown names
and malformed values fail policy validation. Completed turns otherwise produce no
Telegram traffic. A one-shot `subfleet phone notify CONVERSATION_ID [--off]`
requests notification of that conversation's next completed turn; it also needs
the `done` event enabled. The subscription is persisted, not inferred from prose.

Every card names its conversation title, serving lane/account (or explicitly
unassigned), and a bounded, scrubbed excerpt. No raw approval payload or nonce is
sent. Approval buttons reflect exactly the provider's offered decisions: Allow,
Allow for session, Deny, and other offered scopes labelled accurately. Questions
show their options and “Reply to answer”; multiple questions are answered in order,
with selections persisted before the final `approval.respond`. Multi-select
questions need an explicit finish action. Free text answers the current question.

Approvals and questions use `say --class decide`; these are decisions requiring
Max and that class is always delivered. Blocks use `alert`, preserving say's
22:00–07:00 quiet hours, budget and holding behavior. Explicitly requested done
notifications use `requested`. Never pass `--urgent`. All sends carry a stable
`--key sf:<card-id>` and a small inline keyboard via `--markup`. Keep each card
under say's single-message limit, so its returned message ID owns the whole card.

An asynchronous, single-worker phone bridge reconciles pending approvals,
person-blocked conversations and requested completions from the conversation
store. The control loop only schedules it. Subprocess calls have a timeout and
hold no database lock. Successful sends persist their Telegram `message_id`.
Resolved or withdrawn approvals are edited with `say --edit MESSAGE_ID`, even
when the app supplied the answer, with an empty keyboard and the recorded outcome.
Edits are safe to retry. Disabling new notifications still permits these edits.

## Durable identity and delivery honesty

A schema migration adds phone card and reply/subscription state to
`conversations.sqlite3`. A card binds an opaque random token to the conversation,
message and optional approval, a unique event key, delivery state, Telegram ID,
offered actions, and question progress. Callback data is `sf:<token>:<action>` and
fits Telegram's 64-byte limit. Tokens select only recorded actions; callback text
never supplies an approval ID, executable, provider request, or arbitrary decision.

Insert the card before calling say. Pending/sending/sent/held/failed (or equivalent
explicit states) distinguish delivery from acceptance. Only output of the form
`sent message_id N` creates an owned Telegram ID. A held, deduped or emailed alert
has no clickable Subfleet card, and must never be assigned a fabricated ID.
Because say dedupes only alerts and Telegram has no send idempotency key, a crash
between sending and recording the ID cannot promise exactly-once delivery.
Interrupted/ambiguous sends remain visibly unknown instead of blindly sending a
second decision card. Failures do not block conversations. Known card IDs survive
daemon restarts; replies to old cards remain bound to their original conversation.

## Incoming actions

The additive patch at `phone/cos-tg-poller.patch` runs only after the poller's
existing owner-chat filter and update recording:

* `sf:` callbacks call `subfleet phone tap DATA` and acknowledge the callback.
* Text replies first call `subfleet phone owns TELEGRAM_MESSAGE_ID`. Exit 0 means
  owned; 1 means unknown and keeps existing CoS handling; other failures are
  reported without accidentally interpreting the text as a CoS decision.
* Owned replies call `subfleet phone reply TELEGRAM_MESSAGE_ID --update-id ID -- TEXT`.
  Route them before CoS commands or `d011 …` parsing. Ignore edited owned replies.

Commands use an argv list, never a shell. `SUBFLEET_BIN` selects the CLI executable
for launchd and tests. A failed command produces a concise visible error, not a
success acknowledgment. Existing decision cards, commands, photos and updates
continue through their original paths.

Phone ops preserve the daemon's person-only checks; a phone flag is not an agent
bypass. Because launchd's poller has no terminal, the peer check additionally accepts
the CLI's immediate Python parent running the exact configured gateway script. It
first refuses any guardian or attempt-marked ancestor, as for the app and terminal.
The script defaults to `~/chief-of-staff/bin/tg-poller`; only a development state root
can override it with `SUBFLEET_PHONE_POLLER`. This is the existing same-user boundary,
not protection from the user impersonating their own gateway. The poller owner
check authenticates the remote user. Tap/reply responses are
idempotent: terminal approvals cannot execute again, and the poller's update ID
gives a deterministic queued-message ID and a persisted reply receipt. A CLI reply
without an update ID is a new intentional message. Phone actions record
`source: phone` in conversation events, without writing provider stream watermarks.

Tap answers use the existing `approval.respond` path, including its stored nonce,
request hash, offered options and live-runner checks. Old/unknown tokens and
conflicting repeats fail safely. A reply to a pending question answers that
question. Otherwise it goes to its mapped conversation: use an existing live steer
operation if available, otherwise `message.submit` queues it. This checkout has no
steer operation or driver capability, so its truthful receipt is **queued**. A
reply never silently resolves delivery-unknown or another safety block; it can
queue behind the block, with that reason visible in the receipt.

## Validation and rollout

Unit tests cover rendering, offered scopes, policy, durable mappings, duplicate
taps/replies, question progress, stale cards, source attribution, failures and
resolution edits. A fake-provider/fake-say end-to-end test exercises the CLI/socket,
approval response and edited card, plus a reply and silent ordinary completion.
The say double follows `SAY_TRANSPORT=file:/path` and records API-shaped JSONL.
Patch tests apply the exact patch to a pinned pre-patch poller fixture and execute
its `--once --updates` mode with fake Subfleet and say, including foreign chats
and regressions for CoS decisions. All roots, credentials, transports and sockets
in tests are temporary. No real Telegram or running personal daemon is used.

The orchestrator reviews and applies the CoS patch after this change. Deploying
the code and restarting the personal daemon are outside this job; this job writes
only its assigned workspace. No edits to chief-of-staff are needed to review or
test the implementation.
