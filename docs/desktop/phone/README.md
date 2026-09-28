# Applying the chief-of-staff bridge

`cos-tg-poller.patch` changes only the existing `bin/tg-poller`. It adds no bot
listener. The existing owner-chat check remains ahead of every Subfleet command.

After review, the orchestrator can apply it from chief-of-staff with
`git apply /path/to/subfleet/docs/desktop/phone/cos-tg-poller.patch`. Set
`SUBFLEET_BIN` in the poller's launchd environment to the installed Subfleet CLI
if its executable is not on launchd's PATH. This job does not apply it there or
restart the poller.

The CLI contract is `phone owns ID` (exit 0 owned, 1 unknown, 69 operational
failure), `phone tap DATA`, and `phone reply ID --update-id ID -- TEXT`.
Successful actions print a concise receipt. Failures stay visible to Max. The
poller routes owned replies before CoS commands and decision rulings, passes text
as one argv element, and ignores edits to owned replies.

Run `python -m pytest -q tests/unit/test_phone_poller_patch.py` in this repository.
Tests apply this exact patch to the pinned fixture and execute its `--once` mode
using fake executables and `SAY_TRANSPORT=file:`. The tests require neither a
chief-of-staff checkout nor Telegram credentials.

The daemon defaults to `~/chief-of-staff/bin/say`; `SUBFLEET_SAY` selects another
gateway executable. It never starts or configures a bot. `phone.telegram` in the
daemon policy defaults to `{"enabled":true,"events":["approvals","questions","blocks"]}`.
Set `enabled` false to stop new cards and phone mutations (old ownership mappings
remain, and existing approval cards still get resolution edits). To request a
one-shot completion card, include `"done"` in `events`, then run
`subfleet phone notify CONVERSATION_ID`; `--off` cancels it.

Delivery receipts live in the conversation store's `phone_cards` table. Only
`sent message_id N` maps a Telegram ID. `held` and `deduped` alerts follow say's
normal handling; email fallback has no phone card. Failed or ambiguous sends are
recorded and logged, and are not automatically sent again. Review the gateway
log and conversation in the app before attempting any manual recovery. Known
card resolution edits retry on subsequent worker passes.
