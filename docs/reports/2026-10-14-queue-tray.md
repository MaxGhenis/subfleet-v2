# Queue tray on release/217 — 2026-10-14

Built from `5a711114e9654310f00e8490d5abf16dcdd02920`, following Max's
2026-09-28 queue and opening-scroll requests. The archived tray is reference
only; this implementation retains release/217's steer, recall, final Timeline
API, request-id approval joins, reconciliation and work-group presentation.

## Design as built

`Timeline.layout` splits scrolling items from compact tray rows. A person
message waits in the tray exactly while a live turn, a conversation block or
an earlier dispatchable message is ahead. The first idle send stays in the
timeline across its `queued` receipt. Dispatch order follows the current daemon:
repairs, missed steers, ordinary messages, wakes; sequence within each group,
then arrival order for sends without receipts. Queued failover continuations
remain timeline notices and count as ahead.

The tray is pinned immediately above the composer. Rows show a two-line
preview, optional status, and Withdraw. The eligible head offers Send now
through the existing steer path; slash commands, shell input, narrow permissions,
non-running hosts and busy rows do not offer it. Unblock notes have no Withdraw.
Long queues expand into at most 200 points of rows; short expanded queues shrink.
The preview and controls use the conversation's reading font scale. Pending and
refused steers retain their existing status words on the tray, including after restart. Pre-start withdrawals use a dedicated single-line view,
without the generic notice card or a duplicate status caption.

Withdrawal uses the existing outbox before receipt and `message.cancel` after
receipt. Dispatch races retry at 0.1, 0.25, 0.5 and 1 seconds. Too-late reads
that message's status and interrupts only its own live turn. Steering/steered
messages never cause an interrupt of their host. Not-running reads status again;
a second Withdraw is not an error. Local withdrawals and tombstones retain one
withdrawn line under the original person row id, preserving known text/images.
A turn that already began retains its bubble and work.

Fresh conversation identity and macOS 15's initial bottom anchor establish the
opening viewport. Catch-up changes and opening approval placement disable
animation. The person's own send glides; others follow only with the end visible.
Event-only messages from another client cannot masquerade as local sends. A
local-send marker survives a receipt arriving before SwiftUI's first update;
promoting an older queued message does not count as a new local send.

## Confirmed bugs and audit gaps

- Flicker: membership tests and receipt-transition properties keep an idle send
  in the timeline before receipt, after queued, and after dispatch starts.
- Tombstones: the original withdrawal line remains visible, including real daemon
  tombstones and known attachments.
- Waiting failover continuation: counts as ahead; a later send enters the tray.
  The heading says it follows the message ahead, rather than claiming it is next.
- Expanded layout: content sizing fixes both short expanded queues and queues
  withdrawn to four rows. Both old strict xfails are normal passing regressions.
- M7: state-only sending controls exercise outbox withdrawal without an entry.
- M17: waiting, starting, running and approval-needed each exercise interrupt.
- M19: stop(withdraw) during an in-flight send returns no receipt and no error.
- O1: a steerable busy head has no Send now or second Withdraw.
- O2b: expanded Withdraw buttons are checked in visual top-to-bottom order.
- O8: busy spinner counts match a separately hosted real ProgressView reference.

Every required mutation was applied to a separate workspace-local source copy
and tested under its own lock hold. All ten were killed by assertions, with no
compile errors: flicker (1 failure), tombstone (1), waiting continuation (2),
expanded sizing (2), M7 (1), M17 (1), M19 (1), O1 (1), O2b (1), O8 (1).
The mutation runs selected 41 cases in total.

## Send-failure stack seam (draft PR #213)

The stack is not merged into this branch. `ConversationStoreState.layout(
conversationID:)` is the view's projection; `QueuedMessage.id` is the message id.
`QueueTray.notice` takes that id and returns an optional `AnyView`, rendered
inside its owning row; the hosted-view regression supplies a notice and Try
again action and checks both stay on that row. The notice's actions are disabled
while withdrawing. A stack landing second can pass its existing SendNoticeView
through this closure, attach its status to `layout.tray[index].status`, and keep
the timeline notice only for ids in `layout.items`. Filter the notice's own
Withdraw link because the row supplies it; waiting notices need only the status
caption, so the content closure can return nil for those. This avoids importing the
old stack's SendNotice type or renumbering its contract clause.

The rescued placement tests must use `startReading` (not `beginReading`),
`withdrawLocal(conversationID:messageID:)`, final approval reconciliation and
request-id joins. Do not restore the old rule that all queued messages are in
the tray or that tombstones disappear. `withdrawSend`/`WithdrawResult` distinguish
a true local withdrawal from an in-flight send; UIModel.stop routes sending
withdrawals through the same method as the tray. A daemon-known settled receipt
is retained rather than leaving an old Sending state visible.

## Validation

Completed passing suites: `test_core_queue_tray.py` 44,
`test_core_queue_engine.py` 75, `test_queue_tray_view.py` 15,
`test_core_steer.py` 43, and `test_queue_tray_notice.py` 2. The final Swift source
builds through `app/build.sh` with the release target and signing checks.
Existing Swift Sendable-capture and unnecessary-await warnings remain. The
first broader batch passed 83 of 84 cases in 19m52s. Its one failing
approval-timeline property still excluded queued continuations in its reference,
which conflicts with the required failover fix. Commit `3c0ba3226` updates that
independent reference from `next_dispatchable`, including missed repairs and
wakes, and adds a concrete regression. The corrected 41-case file is queued for
a rerun; Python syntax validation passes. Per-file results and shared-lock
omissions follow.

Tests run as foreground, one pytest per shared lock hold,
with temporary Hypothesis storage and no deep profile. New queue properties
use bounded 40-example CI settings; existing properties keep their bounded settings. The 300-case coverage check runs independent timelines in groups of five
per native process to avoid excessive launches. Native
probes host views offscreen and use synthetic state; the app is built but never
installed or launched.

One follow rerun took 35m50s in its locked pytest process (42 passed, 2 failed),
exceeding the requested 20-minute hold. The run stayed in the foreground and was
not relaunched. It found a missing viewport-change key during catch-up and a
coverage generator that rarely made an idle send while reading higher up. Both
were corrected; the CI sampling and coverage batching above bound later runs.
The repaired two-file run held the lock for 360.9 seconds: foundation 43 passed
and one stale viewport expectation failed, notice 2 passed. That expectation
is corrected in `1ea3bf992`; the final 44-case rerun passed in 256.9 seconds
of pytest time. Broader checks remain queued. Queue wait is separate from time
holding the lock.

## Delivery and remaining work

The shared Git index is read-only in this sandbox. Commits use workspace-local
metadata at `build/queue-tray-git`, branch `feat/queue-tray-217`, with object reads
from the shared repository. No history is rewritten and nothing is pushed.

Known separately filed follow-up `task_94fbb104` remains: watch changes update
only already-known messages, so an externally submitted or daemon-created message (including a CLI send,
unblock note or continuation not yet known locally) first appears after
reopening the conversation. This is outside the rebuild's requested scope.

## Counts per file

These are the latest completed runs per file as of 12:47 UTC. A pending run
is not counted as a pass. The approval-reference rerun and the
remaining broad batches are still queued. The foreground runner remains active;
the sandbox denied a signal to its wrapper, so it was left unchanged.

| File | Result |
| --- | --- |
| `tests/frontend/test_app_cutover_start.py` | 13 passed |
| `tests/frontend/test_conversation_view.py` | 6 passed |
| `tests/frontend/test_core_app_cutover_reading.py` | 15 passed |
| `tests/frontend/test_core_approval_notification.py` | 10 passed |
| `tests/frontend/test_core_approval_reach.py` | 39 passed, 1 old continuation-reference property failed; corrected; 41-case rerun queued |
| `tests/frontend/test_core_client.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_diff.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_draft.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_idle.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_live.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_live_failure.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_markdown.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_outbox.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_protocol.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_question_card.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_queue_engine.py` | 75 passed |
| `tests/frontend/test_core_queue_tray.py` | 44 passed |
| `tests/frontend/test_core_search.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_steer.py` | 43 passed |
| `tests/frontend/test_core_steer_properties.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_store.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_text_scale.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_timeline.py` | 0 run: shared-lock queue |
| `tests/frontend/test_core_titles.py` | 0 run: shared-lock queue |
| `tests/frontend/test_disk_ruling.py` | 0 run: shared-lock queue |
| `tests/frontend/test_menu_view.py` | 0 run: shared-lock queue |
| `tests/frontend/test_pr124_fixes_ui.py` | 0 run: shared-lock queue |
| `tests/frontend/test_pr124_recovery_properties.py` | 0 run: shared-lock queue |
| `tests/frontend/test_pr124_review_r2_fixes_ui.py` | 0 run: shared-lock queue |
| `tests/frontend/test_queue_tray_notice.py` | 2 passed |
| `tests/frontend/test_queue_tray_view.py` | 15 passed |
| `tests/frontend/test_reading_view.py` | 0 run: shared-lock queue |
| `tests/frontend/test_review_pr127_timeline.py` | 0 run: shared-lock queue |
| `tests/frontend/test_status_model.py` | 0 run: shared-lock queue |
| `tests/frontend/test_visual_progress.py` | 0 run: shared-lock queue |
| `tests/frontend/test_visual_report_hygiene.py` | 0 run: shared-lock queue |
| `tests/frontend/test_visual_review_fixes.py` | 0 run: shared-lock queue |
| `tests/frontend/test_visual_round_five.py` | 0 run: shared-lock queue |
| `tests/frontend/test_visual_round_four.py` | 0 run: shared-lock queue |
| `tests/frontend/test_visual_round_three.py` | 0 run: shared-lock queue |
| `tests/frontend/test_visual_round_two.py` | 0 run: shared-lock queue |
| `tests/frontend/test_visual_snapshots.py` | 0 run: shared-lock queue |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_catalog.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_connections.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_diff.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_fifo_reads.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_handoff.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_handoff_briefs.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_history.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_peers_attachments.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_reconcile.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_redact.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_service.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_state_reads.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_status.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_steer_service.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_store.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_stream_assertion.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_titles.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_waits.py` | 0 run: shared-lock queue |
| `tests/unit/test_conversation_wakes.py` | 0 run: shared-lock queue |
| `tests/fake/test_conversation_pools.py` | 0 run: shared-lock queue |
| `tests/fake/test_conversation_wakes_fake.py` | 0 run: shared-lock queue |
| `tests/e2e/test_conversation_titles_e2e.py` | 0 run: shared-lock queue |
| `tests/e2e/test_conversation_wakes_e2e.py` | 0 run: shared-lock queue |
| `tests/e2e/test_conversations.py` | 0 run: shared-lock queue |
| `tests/unit/test_desktop_ledger.py` | 0 run: shared-lock queue |

## Commit transfer

All commits are on `feat/queue-tray-217` in workspace-local metadata. An
incremental, verified bundle is saved at `docs/reports/2026-10-14-queue-tray.bundle`
(requires the base `5a711114e965`). The bundle is an ignored delivery artifact and
is never staged. The hub can fetch it locally without a network remote.


## Round 2 — review r1 fixes (2026-10-15)

Implementation is committed in workspace-local `.git-local` metadata on
`feat/queue-tray-217`, based on `8d7cbcb6959e`. The sandbox refuses writes to the
shared worktree index. No shared repository metadata, network remote, installed
app, live daemon, or live state has been changed. Tests were committed before
the implementation. Every new commit carries the requested co-author trailer.

### Findings and contract

- **P2-1:** tray blocking reads `MessageState.live`, the existing app counterpart
  of the daemon's `LIVE_STATES`, including `delivery-unknown` and `steering`.
  A regression compares the two sets. The watch loop reads the currently
  focused conversation after its long poll answers and before publishing its
  message changes. `conversation.open` is the existing targeted read carrying
  `blocked_by` at this head; its outbox chain bookkeeping is bypassed on the feed
  thread. The refreshed conversation and watch page publish together. A failed
  refresh leaves the page unpublished for retry. The watch protocol is unchanged.
- **P2-2:** a persistent `sentAsSteer` marker keeps every steer in the timeline
  through sending, queued receipts, steering, steered, missed and unknown
  delivery, including dispatch as its own turn. Missed status outranks an
  outstanding local request and reads “Unread until the current turn ends.”
  Refusals stay visible on the timeline. Steers never contribute to the tray's
  count or its Send now actions. Receipt, event, local-send and restored outbox
  paths all retain this identity.
- **P3-1:** a `deferred:` queued head is in the tray even without anything ahead,
  shows the existing deferral reason template and retains Withdraw.
- **P3-2:** C-29.7 no longer cites the absent C-29.17 or claims draft #213 is
  integrated. `QueueTray.notice` remains an optional future seam, with its
  existing hosted notice/action test.
- **P3-3:** a shared AppKit observer keeps opening content at the end without
  animation as lazy rows and the first events page grow. Its lifetime ends at
  the person's first live scroll, wheel input or keyboard paging. Only document
  growth and viewport size changes pin; an offset change alone can be a Review
  jump. The existing `FollowKey` policy is unchanged. The adapted review probe
  exercises macOS 15+ and the emulated macOS 14 path, late page growth without a
  follow-key change, and growth after the first person scroll. The unanchored
  case remains a control.

C-29.7 now states these rules explicitly: an eligible queued message is in the
tray exactly when something is ahead or the daemon deferred it; a message sent
as a steer always remains in the timeline. The app and daemon use the same live
state contract, with a set-equality regression. Both property generators include
steering, steered, missed-steer and deferred cases. Membership and receipt
no-flicker properties each request 200 bounded, derandomized examples, and the
coverage check requires steer and missed-steer examples to occur. No deep
profile was used.

`app/build.sh` cleanup now uses Python's `shutil.rmtree`, as this job requires.
The app has only been built, never installed or launched.

### Validation status

<!-- r219f2-validation -->
The final `app/build.sh` succeeds, including plist and signing checks. The final
full-source offscreen scroll probe also compiles. Existing Sendable-capture and
unnecessary-await warnings remain. Python syntax, shell syntax, whitespace, and
all twelve mutation replacement anchors pass their checks.

The prescribed locked `uv run pytest` was attempted. After the lock was acquired,
uv failed before starting pytest because the sandbox rejects its default-cache
write to `/Users/maxghenis/.cache/uv/sdists-v9/.git`. `--no-sync` variants also fail.
The existing repository venv lacks Hypothesis; no cache directory was replaced,
no cache was cleaned and no alternative pytest command was used. An environment
exception was requested and has not been received. Consequently **zero pytest
tests have run in this round**; no r1 or CI passes are counted here.

Supplemental foreground native checks ran under separate shared-lock holds:

- Immutable base `8d7cbcb6959e`, with test-only probe adapters: 9 deterministic
  core checks ran, with 5 passing and 4 expected failures. The failures reproduce
  delivery-unknown not counting as ahead, steering not counting as ahead, a steer
  moving `absent → tray → tray → timeline → tray → timeline → timeline`, and a
  deferred head missing from the tray.
- Offscreen opening-scroll probe: 2 checks passed. Both macOS 15+ and emulated
  macOS 14 ended at 0 points from the bottom after opening, late page growth and
  catch-up; after a person scroll, later growth left them 1,721 and 1,707 points
  above the end. No window was visible. The final updated probe compiles too.
- Final full-source `app/build.sh`: passed, including plist and signing checks.
- Final fixed-core and daemon-backed watch comparisons: unrun. Their supplemental
  foreground hold produced no acquisition marker or check results while queued;
  it was interrupted at 18:16 UTC to finish after the job's 4 h 30 m cutoff.
  No pytest process was started by that hold. The base scroll probe compiles,
  but its additional runtime comparison is unrun too.

No further holds were started after the cutoff, and no foreground command is
left running. These supplemental checks are not counted as pytest passes.
The source build passes, but the required regression and mutation verification
is incomplete; this report does not assert that all round-2 tests pass.
<!-- /r219f2-validation -->

### Required pytest files — this round only

Every row below is unrun because uv cannot initialize its required default cache.
This includes both 200-example properties and the complete review regression
file. Counts are per file, not inherited from earlier reviews.

| File | Passed | Failed | Run | Status |
| --- | ---: | ---: | ---: | --- |
| `tests/frontend/test_app_cutover_start.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_conversation_view.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_app_cutover_reading.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_approval_notification.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_approval_reach.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_client.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_diff.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_draft.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_idle.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_live.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_live_failure.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_markdown.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_outbox.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_protocol.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_question_card.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_queue_engine.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_queue_tray.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_search.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_steer.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_steer_properties.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_store.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_suggestions.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_suggestions_r1b.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_text_scale.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_timeline.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_core_titles.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_disk_ruling.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_menu_view.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_pr124_fixes_ui.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_pr124_recovery_properties.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_pr124_review_r2_fixes_ui.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_queue_tray_notice.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_queue_tray_view.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_reading_view.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_review_pr127_timeline.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_review_r219_probes.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_status_model.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_visual_progress.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_visual_report_hygiene.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_visual_review_fixes.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_visual_round_five.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_visual_round_four.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_visual_round_three.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_visual_round_two.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_visual_snapshots.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/frontend/test_visual_suggestions.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/unit/test_conversation_service.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/unit/test_conversation_steer_service.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/unit/test_steer_invariants.py` | 0 | 0 | 0 | Unrun: environment |
| `tests/unit/test_conversation_status.py` | 0 | 0 | 0 | Unrun: environment |

### Mutations

`tools/queue_tray_round2_mutations.py --list` validates one unique replacement
anchor and a named test for each of the review's nine mutants and all three
round-2 additions. Actual mutation executions are unrun for the same uv-cache
reason; anchor validation is not a killed-mutant result. The runner creates
isolated source copies and runs each pytest synchronously under its own shared
lock, using the default uv cache and temporary Hypothesis storage. It never
changes the production sources.

| Mutation | Named test | Execution |
| --- | --- | --- |
| flicker | `tests/frontend/test_core_queue_tray.py::test_c29_7_an_idle_send_answered_queued_stays_in_the_timeline` | Unrun |
| tombstone | `tests/frontend/test_core_queue_tray.py::test_c29_7_a_send_withdrawn_before_its_receipt_keeps_its_line_after_a_tombstone` | Unrun |
| waiting-continuation | `tests/frontend/test_core_queue_tray.py::test_c29_7_a_person_send_waits_behind_a_queued_failover_continuation` | Unrun |
| expanded-sizing | `tests/frontend/test_queue_tray_view.py::test_c29_7_an_expanded_tray_whose_rows_fit_is_as_tall_as_its_rows` | Unrun |
| M17 | `tests/frontend/test_core_queue_engine.py::test_c29_7_a_message_that_became_its_own_live_turn_is_interrupted[waiting]` | Unrun |
| M19 | `tests/frontend/test_core_queue_engine.py::test_c29_7_stop_withdraw_gives_what_withdraw_send_gives[in-flight]` | Unrun |
| O1 | `tests/frontend/test_queue_tray_view.py::test_c29_7_a_row_being_withdrawn_cannot_be_steered` | Unrun |
| wake-ordering | `tests/frontend/test_core_queue_tray.py::test_c29_7_property_order_matches_the_real_daemons_next_dispatchable` | Unrun |
| steering-as-own-turn | `tests/frontend/test_core_queue_engine.py::test_c29_7_withdraw_of_a_message_in_another_turn_never_interrupts_its_host[steering]` | Unrun |
| steering-not-ahead | `tests/frontend/test_review_r219_probes.py::test_r3b_every_dispatcher_live_state_holds_the_tray[steering]` | Unrun |
| missed-steer-in-tray | `tests/frontend/test_review_r219_probes.py::test_r2_a_missed_steer_stays_in_the_timeline` | Unrun |
| deferred-without-withdraw | `tests/frontend/test_review_r219_probes.py::test_r7_a_deferred_head_has_its_reason_and_withdraw_in_the_tray` | Unrun |

### Transfer and remaining work

The shared index is read-only. New commits live in `.git-local` on
`feat/queue-tray-217`; the caller's checkout is untouched. The verified incremental
bundle `docs/reports/2026-10-14-queue-tray-round2.bundle` requires base
`8d7cbcb6959e` and transfers the local branch without a network remote. Logs, JSON
probe output and bundles remain unstaged. Full pytest and mutant verification,
plus real macOS 14 and 15 hardware checks, remain outstanding.

## Validation round 2 (2026-10-15)

This validation starts from `e1a88f3cb12c` in the assigned isolated workspace.
The checkout was clean. The start was 18:44 UTC; no new lock holds will start
after 23:14 UTC (4 h 30 m). Only failures demonstrated by this validation and
assertions encoding the superseded design will be changed.

The default uv cache rejects a temporary-file write with `Operation not
permitted`. As explicitly authorized for this round, `UV_CACHE_DIR` points to
`uv-cache` inside the job's `sf-r219v2-*` scratch directory. Hypothesis storage
and compiler caches also live there, with `HYPOTHESIS_PROFILE=ci`; the deep
profile is never used. Each pytest runs in the foreground, sequentially, under
its own `lockf -k` hold without an acquisition timeout or pytest workers.
JUnit evidence remains ignored under `build/r219v2/`.

The shared Git metadata is outside the writable workspace. Commits use local
`.git-local` metadata, branch `feat/queue-tray-217`, with read-only object
alternates to the shared repository. Nothing is pushed or rewritten, and no
caller files or live application state are changed.

### Build checkpoint

`app/build.sh` passed: full optimized compilation, plist validation and ad-hoc
signing. Existing Sendable-capture and unnecessary-await warnings remain.
The bundle was built only in this workspace, never installed or launched.
The round-2 replacement of `rm -rf` with Python `shutil.rmtree` is retained as
the behavior-preserving sandbox-compatible cleanup workaround; it is not an
application behavior change. No further build-script change is needed.

### Test checkpoint

The first required hold completed in 588.65 s: `test_core_queue_tray.py`
44 passed / 1 failed; `test_queue_tray_notice.py` 2 passed. The failed test,
`test_c29_7_property_order_matches_the_real_daemons_next_dispatchable`, still
required a missed steer in the tray. Its smallest failing example is one
person message with `steer-missed:host`; the application correctly keeps it in
the timeline under the hub's design. The test now filters missed steers from
the daemon-drained order and separately requires their timeline bubbles.
Explicit examples retain that failure and a mixed wake/person/missed-steer
queue, so dispatcher ordering remains covered. Production code is unchanged.
The corrected property awaits its verification hold. Queue-engine tests are
queued next. The final tables will record all fifty required files and twelve
named mutations, including anything unrun.

The queue-engine file passed all 75 tests (11.73 s); the offscreen tray-view
file passed all 15 (101.88 s). The steer/approval hold completed in 109.87 s:
`test_core_steer.py` 42 passed / 1 failed, `test_core_steer_properties.py`
1 passed / 1 failed, approval reach 41 passed, approval notification 10 passed.

- `test_c24_9_journaled_steers_restore_after_a_restart` encoded the old tray
  design for restored pending and refused steers. It now requires exactly one
  timeline bubble for each and excludes both from the tray, retaining the
  existing status-word and eligibility assertions.
- `test_t1_t4_a_message_is_drawn_once_whatever_the_daemon_says` found a real
  ordering defect: when events make A host B and later put A inside C, B's
  bubble can render before A's relocated bubble. This is not an old-design
  assertion. Its minimized two-event history is retained as an explicit
  Hypothesis example before changing production code.

The six-file conversation/timeline/outbox/store hold is queued. Both new
steer regression updates await verification; the nested placement fix follows
after the active hold, keeping source changes out of a running compilation.

### Continuation — 2026-10-16, head `8057d4f07`

This continuation began at 02:53 UTC in the assigned clean checkout. Its
4 h 30 m cutoff for starting holds is 07:23 UTC. It preserves the completed
validation above and does not count queued runs as passes. The saved
`20261015-144104-pr219-validate2-uncommitted.patch` applies cleanly. Its
nested-delivery fix is being checked against the regression already committed
in `8057d4f07`.

The default uv cache is unwritable, so the explicitly authorized fallback
cache, Hypothesis storage and compiler caches are inside this continuation's
`sf-r219v2-*` scratch directory. All pytest processes are
foreground, sequential and separately locked, with no acquisition timeout or
workers. Bounded properties use their existing settings and the CI profile;
no deep profile is used. Evidence is ignored under
`build/r219v2-continuation/`. Workspace-local `.git-local` metadata retains
branch `feat/queue-tray-217`, with read-only shared-object alternates; the
shared index and caller's checkout are untouched. No sub-agents or network
remotes are used.

The build script's Python `shutil.rmtree` cleanup is retained solely as the
behavior-preserving workaround for the sandbox's recursive-removal guard.
The app is built locally only; native view probes use synthetic state offscreen.

#### Nested delivery fix — verified

`Timeline.placedSteers` now leaves a child in its own timeline place when its
host is itself selected for placement inside another turn. Delivery markers
stay with the host's events; they must not move the child's bubble ahead of
the relocated host's bubble. The minimized two-event history in
`test_t1_t4_a_message_is_drawn_once_whatever_the_daemon_says` was committed
before this production change (`8057d4f07`) and now passes with all 150 bounded
generated histories.

The steer/approval hold passed **96/96** in **512.50 seconds**: steer 43, steer
properties 2, approval reach 41, approval notification 10. This verifies both
the production fix and the already-committed correction to
`test_c24_9_journaled_steers_restore_after_a_restart`: pending/refused steers
belong exactly once in the timeline and never in the tray.

The corrected queue-tray hold passed **47/47**: queue tray **45** and notice
**2**, in **476.01 seconds** of pytest time (**480.80 seconds** holding the
lock). Both 200-example membership/no-flicker properties passed. The corrected
`test_c29_7_property_order_matches_the_real_daemons_next_dispatchable` passed:
it excludes missed steers from expected tray order while independently
requiring their timeline bubbles, including both retained explicit examples.
No queue production code changed for this expectation correction.

The continuation's full release `app/build.sh` passes after the timeline fix,
including plist validation and ad-hoc signing. Existing Sendable-capture and
unnecessary-await warnings remain. No installed app or live state is touched.

The six-file conversation/timeline/outbox/store hold passed **80/80** in
**546.90 seconds** of pytest time (**552.11 seconds** holding the lock):
conversation view 6, core reading 15, app cutover 13, timeline 23, outbox 13,
and store 10. No assertions or production code needed changing.

The four-file daemon hold passed **400/400** in **277.79 seconds** of pytest
time (**283.24 seconds** holding the lock): conversation service 98, steer
service 45, steer invariants 248, and conversation status 9. No test or
production change was needed.

The earlier **15/15** tray-view result is retained without rerunning that hold:
its offscreen probe constructs rows directly and does not exercise the changed
nested timeline placement. The planned engine recheck was deferred to preserve time for mutations;
its prior 75-pass result is retained as described below. All remaining render exports use temporary
test destinations rather than inherited optional export paths.

#### Review probes — opening pin regression found

The full independent review file completed in **282.31 seconds**
(**285.85 seconds** holding the lock): **19 passed, 2 failed**. Both failures
are `test_r8_opening_stays_at_the_end_after_late_measurement`: after settling,
the macOS 15+ path was 41 points above the end and emulated macOS 14 was
12 points above it. Opening and late-page checks passed; no window was visible.
The required two-point end tolerance is retained. The probe now records
document bounds/frame origins, clip document end and content inset alongside
the existing samples, and retains synthetic JSON only in temporary evidence.
This diagnostic test step precedes an application fix.

The offscreen probe also includes a deterministic AppKit replay: it posts a
document-height change and queues a bottom-anchor scroll 12 points above the
end after the observer's work. `test_r8b_an_opening_pin_runs_after_queued_scroll_layout_work`
requires the pin to finish last; `test_r8c_an_offset_only_review_jump_keeps_its_position`
requires a subsequent Review-like offset change to stay at 250 points. Both
regressions are committed before changing `ConversationOpeningPin.swift`.

The diagnostic hold reproduced **3 failures / 1 pass** in **268.31 seconds**
(**282.72 seconds** holding the lock). Both original paths still finish
41/12 points above the end; the deterministic queued-scroll replay finishes
12 points above it. Its later offset-only Review stays at 250 and passes.
Document bounds and frame agree and there is no bottom inset, confirming
ordering rather than a coordinate-oracle error. No window is visible.

An initial run-loop scheduling candidate did not fix the queued-scroll replay
and was discarded without committing application code. Its full review hold
had 3 failures, 1 pass and 19 setup errors (537.42 seconds; 552.73 seconds
holding the lock). The setup errors are a separate environment failure:
`swiftc` attempted to spawn a missing `clang` under `/Applications/Xcode.app`.
This job now selects the existing Command Line Tools and SDK using only its
`DEVELOPER_DIR`/`SDKROOT` environment; the person's Xcode selection is unchanged.

The revised fix coalesces the current SwiftUI update on the main queue before
queueing the actual pin after that update's scroll work. The geometry triggers
and person-scroll release are retained, including offset-only Review behavior.
A small compiled replay runs inside the next review hold before pytest, so a
scheduling failure is detected before another full-source compilation.

The small replay now compiles and links with the pinned Command Line Tools.
One attempted hold ended before pytest because that build had not yet finished
(0 tests, 0.21 seconds holding the lock); it is not counted as verification.
The retry is queued only after the executable exists.

With the pinned toolchain, the replay passed at zero points from the end
(Review stayed at 250), and all 19 core review cases passed. The full-source
view compilation hit its 540-second fixture bound, so the four view cases
had setup errors, not assertion failures. That hold took 776.42 seconds of
pytest time (789.00 seconds holding the lock). The isolated four-case rerun
allows 900 seconds for compilation plus the existing 60-second probe bound,
remaining within a 20-minute hold. Its geometry/assertion tolerances are
unchanged. Parallel preparation is stopped during this rerun. Prepared view
mutants receive the same compile budget outside their eventual pytest holds.

The isolated view compile also timed out at 900 seconds: **4 setup errors**,
912.65 seconds of pytest time / **915.24 seconds** holding the lock. No
rendering assertions ran in that hold. The small real-observer replay passes
after the two-stage main-queue change (end distance 0, Review offset 250),
and at that checkpoint the full SwiftUI checks were unverified. The successful
SDK 26.5 rerun below closes this gap; no assertion or geometry tolerance
has been weakened. All 19 core review probes
passed with the selected toolchain.

To preserve time for the required mutants, the full engine hold retains its
earlier 75-pass result rather than taking the planned additional hold; its
withdrawal branches are unchanged, and the named engine mutation targets
exercise the current code. The unaffected row-only tray-view result remains
15 passed. Remaining files and mutants will be listed explicitly at cutoff.

#### Mutation runner and first verified mutant

The runner can prepare independent native source copies in the foreground,
then reuse the existing exact-input probe cache in each separately locked
pytest. Readiness covers source/test bytes, the runner and selected
compiler/SDK settings; it also checks the exact mutation text. Every runtime
uses its own temporary Hypothesis directory. Preparation alone is never
counted as a mutation result. Acquisition markers separate lock waits from
actual holds. The compile-budget-only Python update was refreshed in three
prepared copies only after reconstructing their prior signatures and checking
all Swift inputs and remaining Python inputs byte for byte.

**flicker: KILLED** by
`test_c29_7_an_idle_send_answered_queued_stays_in_the_timeline`: the queued
receipt incorrectly put the idle send in the tray. One assertion failed;
pytest took 18.07 seconds, the lock hold 39.51 seconds. No compile error.

The attempted SDK 27 release build fails because that SDK's SwiftUI `@State`
requires `SwiftUIMacros.StateMacro`, absent from the Command Line Tools plugin
search path. An installed SDK 26.5 uses the existing State property wrapper;
the UI build and four-case scroll hold are retried with SDK 26.5 using only
job-local environment settings. Model mutants keep their already-working SDK
27 environment. The original post-timeline release build passed before this
SDK selection issue; the final post-scroll SDK 26.5 release build passed, as recorded below.



#### Post-scroll release build — passed

The final source builds successfully with the installed **macOS SDK 26.5** and
Command Line Tools, including plist validation and ad-hoc signing. Existing
Sendable-capture and unnecessary-await warnings remain. SDK 26.5 retains the
State property wrapper and avoids SDK 27's unavailable SwiftUIMacros plugin.
The SDK is selected only through this job's environment; no installed app,
compiler files or global developer selection is changed. The built app is
never installed or launched. The four-case SwiftUI run subsequently passed.

#### Opening pin verification — passed

The isolated SDK 26.5 hold passed **4/4**, with **170.58 seconds** of pytest
time. Both full SwiftUI OS paths finish at exactly zero points from the end after
opening, the late page and lazy measurement. After simulated person scrolling
they stay 1,721 / 1,699 points above the end; the Review replay stays at 250.
No window is visible. These are both code paths on macOS 26.6.2, not additional
physical macOS 14/15 hardware checks. The original two-point tolerance holds.
The deterministic queued-layout replay and offset-only Review regression pass.
Together with the earlier 19 core cases, the review file is **23/23**. This
closes the post-fix verification gap from the SDK 27 compiler timeouts.

**tombstone: KILLED** by
`test_c29_7_a_send_withdrawn_before_its_receipt_keeps_its_line_after_a_tombstone`: the
mutant removes `person:m2` after the tombstone, so the required line lookup
raises `StopIteration` at the test's final text assertion. Pytest reports one
failed named test in **12.04 seconds**, with a **25.96-second** lock hold. The
runner's conservative assertion-only classifier prints NOT VERIFIED for this
lookup failure; manual inspection confirms the intended missing-line defect,
not a compiler or setup failure. The mutation table records this checked result.

#### Hold-start cutoff — 07:23 UTC

No further holds will start. Both mutation waiters were interrupted at
07:22:59 UTC before lock acquisition (`waiting-continuation`, `expanded-sizing`);
neither started pytest. Their acquisition markers remain absent. All twelve
independent mutant probes were prepared, but only **flicker** and **tombstone**
have actual killed-test evidence. The other ten remain unrun.

The 16-file frontend hold acquired the lock at **07:09:58 UTC** and is still
active at this cutoff, with no failure markers so far. Its results are pending
and are not counted as passes. It will finish before cleanup; its final result
will supersede this checkpoint. Fifteen other frontend files remain unrun.

<!-- validation3-latest -->

#### Final continuation results

Generated 2026-10-16T07:26:07.750073+00:00. These per-file results supersede earlier queued-status tables. A retained prior pass is labeled; setup errors are not assertion passes.

| File | Passed | Failed | Setup errors | Skipped | Status |
| --- | ---: | ---: | ---: | ---: | --- |
| `tests/frontend/test_app_cutover_start.py` | 13 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_conversation_view.py` | 6 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_app_cutover_reading.py` | 15 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_approval_notification.py` | 10 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_approval_reach.py` | 41 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_client.py` | 18 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_diff.py` | 7 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_draft.py` | 7 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_idle.py` | 1 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_live.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_core_live_failure.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_core_markdown.py` | 42 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_outbox.py` | 13 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_protocol.py` | 9 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_question_card.py` | 10 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_queue_engine.py` | 75 | 0 | 0 | 0 | retained prior pass; withdrawal branches unchanged |
| `tests/frontend/test_core_queue_tray.py` | 45 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_search.py` | 20 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_steer.py` | 43 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_steer_properties.py` | 2 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_store.py` | 10 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_suggestions.py` | 7 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_suggestions_r1b.py` | 4 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_text_scale.py` | 7 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_timeline.py` | 23 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_core_titles.py` | 1 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_disk_ruling.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_menu_view.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_pr124_fixes_ui.py` | 17 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_pr124_recovery_properties.py` | 1 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_pr124_review_r2_fixes_ui.py` | 19 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_queue_tray_notice.py` | 2 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_queue_tray_view.py` | 15 | 0 | 0 | 0 | retained prior pass; unaffected row-only view |
| `tests/frontend/test_reading_view.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_review_pr127_timeline.py` | 4 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_review_r219_probes.py` | 23 | 0 | 0 | 0 | continuation |
| `tests/frontend/test_status_model.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_visual_progress.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_visual_report_hygiene.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_visual_review_fixes.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_visual_round_five.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_visual_round_four.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_visual_round_three.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_visual_round_two.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_visual_snapshots.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/frontend/test_visual_suggestions.py` | 0 | 0 | 0 | 0 | unrun |
| `tests/unit/test_conversation_service.py` | 98 | 0 | 0 | 0 | continuation |
| `tests/unit/test_conversation_steer_service.py` | 45 | 0 | 0 | 0 | continuation |
| `tests/unit/test_steer_invariants.py` | 248 | 0 | 0 | 0 | continuation |
| `tests/unit/test_conversation_status.py` | 9 | 0 | 0 | 0 | continuation |

#### Mutation checkpoint

| Mutation | Named test | Result |
| --- | --- | --- |
| flicker | `tests/frontend/test_core_queue_tray.py::test_c29_7_an_idle_send_answered_queued_stays_in_the_timeline` | killed |
| tombstone | `tests/frontend/test_core_queue_tray.py::test_c29_7_a_send_withdrawn_before_its_receipt_keeps_its_line_after_a_tombstone` | killed |
| waiting-continuation | `tests/frontend/test_core_queue_tray.py::test_c29_7_a_person_send_waits_behind_a_queued_failover_continuation` | unrun |
| expanded-sizing | `tests/frontend/test_queue_tray_view.py::test_c29_7_an_expanded_tray_whose_rows_fit_is_as_tall_as_its_rows` | unrun |
| M17 | `tests/frontend/test_core_queue_engine.py::test_c29_7_a_message_that_became_its_own_live_turn_is_interrupted[waiting]` | unrun |
| M19 | `tests/frontend/test_core_queue_engine.py::test_c29_7_stop_withdraw_gives_what_withdraw_send_gives[in-flight]` | unrun |
| O1 | `tests/frontend/test_queue_tray_view.py::test_c29_7_a_row_being_withdrawn_cannot_be_steered` | unrun |
| wake-ordering | `tests/frontend/test_core_queue_tray.py::test_c29_7_property_order_matches_the_real_daemons_next_dispatchable` | unrun |
| steering-as-own-turn | `tests/frontend/test_core_queue_engine.py::test_c29_7_withdraw_of_a_message_in_another_turn_never_interrupts_its_host[steering]` | unrun |
| steering-not-ahead | `tests/frontend/test_review_r219_probes.py::test_r3b_every_dispatcher_live_state_holds_the_tray[steering]` | unrun |
| missed-steer-in-tray | `tests/frontend/test_review_r219_probes.py::test_r2_a_missed_steer_stays_in_the_timeline` | unrun |
| deferred-without-withdraw | `tests/frontend/test_review_r219_probes.py::test_r7_a_deferred_head_has_its_reason_and_withdraw_in_the_tray` | unrun |

#### Files not yet run in this continuation

- `tests/frontend/test_core_live.py`
- `tests/frontend/test_core_live_failure.py`
- `tests/frontend/test_disk_ruling.py`
- `tests/frontend/test_menu_view.py`
- `tests/frontend/test_reading_view.py`
- `tests/frontend/test_status_model.py`
- `tests/frontend/test_visual_progress.py`
- `tests/frontend/test_visual_report_hygiene.py`
- `tests/frontend/test_visual_review_fixes.py`
- `tests/frontend/test_visual_round_five.py`
- `tests/frontend/test_visual_round_four.py`
- `tests/frontend/test_visual_round_three.py`
- `tests/frontend/test_visual_round_two.py`
- `tests/frontend/test_visual_snapshots.py`
- `tests/frontend/test_visual_suggestions.py`

All four opening-view cases passed after the fix with SDK 26.5 (170.58 s pytest). Together with the 19 core review probes, this is 23/23. Supplementary replay: end distance 0, offset-only Review 250. Earlier compiler setup errors are superseded by this successful run.

<!-- /validation3-latest -->

#### Final closeout

The final 16-file frontend hold passed **174/174** in **929.35 seconds** of
pytest time, **943.28 seconds** holding the lock (15 min 43 s). Per-file counts
are in the authoritative table above. No assertion or source change was needed.
The job therefore records **820 fresh passes across 33 files**, plus the
retained **75 engine** and **15 tray-view** passes: **910 passes across 35 of
the 50 required files**, with no unresolved assertion failures or setup errors.

**Two of twelve mutations are killed**. The other ten have no actual pytest
execution; compiling their isolated probes is preparation only. Fifteen
frontend files remain unrun and are individually listed above. The pending
mutation commands were stopped before acquisition at the cutoff. No new hold
started after 07:23 UTC. Every completed hold was under 20 minutes, with one
pytest process at a time and no workers. No command is left running.

The two real defects fixed here are nested delivered-steer ordering and
opening-pin scheduling. The two inherited old-design expectation corrections
are explicitly verified above, test by test; no additional expectation was
relaxed. The final release build passes with SDK 26.5. The build script's
Python cleanup remains solely the behavior-preserving sandbox workaround.

Commits remain on `feat/queue-tray-217` in workspace-local `.git-local` metadata
because the shared index is read-only. The incremental transfer bundle is
`docs/reports/2026-10-14-queue-tray-validation3.bundle`, based on `8057d4f07`.
No logs, JSON evidence or bundle is staged. Nothing is pushed, no history is
rewritten, and the caller's checkout and live application state are untouched.
Temporary uv/Hypothesis/compiler scratch and isolated mutant source copies
are removed with Python `shutil.rmtree`; no `.hypothesis` directory is retained.

## Fix-4 — 2026-10-16

Work stayed in the assigned checkout. The shared Git index is read-only, so
commits are on `feat/queue-tray-217` in workspace-local `.git-local` metadata.
The original `.git` pointer and caller checkout are untouched. No push or
history rewrite occurred. The deliverable transfer bundle is
`docs/reports/2026-10-16-queue-tray-fix4.bundle` (not staged).

### Merge and behavior

Merged the existing local `origin/release/217` at `f305f621462a`, including #198
and #224, before implementation. The sole conflict was the acceptance contract's
Changes list; both independent amendments (queue tray and test-store isolation)
were retained. Merge commit: `11ce065a1`. No remote operation was performed.

D1 records first placement and moves it only forward. An unbound claim, retry
or cleared block does not release a tray row. A message already in the timeline
stays there through defer, hold and requeue, naming the reason and offering
Withdraw. Missed steers say unread until the current turn ends only while that
turn is live. Receipts expose job binding separately from a dispatcher claim.

D2's settle re-read uses the new, read-only **`conversation.status`** op, with
capability `conversation.status.v1`. It returns conversation state and every
message identity in sequence order, without opening or reading a transcript.
All identities are intentional: a repair that settled before the next watch
page must still be discovered. `message.status` hydrates unknown/changed IDs
in batches of 200 before the focused update is published. Event pages hydrate
unknown IDs, including referenced steers and reset-replay pages. Unblock refreshes identities; watch
refreshes discover daemon continuations. `turn.completed` sets message state.
Historical accepted, approval and completion events cannot overwrite a newer
receipt's state; receipt timestamps win ties.

D3 suspends the opening pin before the app reveals an approval or search row,
including pin work already scheduled. C-29.7, C-27.5 and C-29.12 now agree in
the acceptance contract and desktop design. Final app-bundle removal propagates
errors; only staging-trap cleanup retains `ignore_errors`.

### Regression and coverage evidence

Tests-first commit `3854b542c` preceded production edits. The initial locked
baseline failed seven retry cases in 18.45 seconds. The full baseline failed
all **27/27** initial cases in 77.64 seconds against unchanged production code
after the release merge, including real-store/service dispatch histories and
both AppKit row-reveal cases. The generated histories include claim → requeue
→ defer and a watch page during submit. Later tests add binding/requeue,
201-ID hydration, event-side repair hydration, external-writer withdrawal,
and newer-receipt versus old-event replay.

`review-r2.md`, `design-r3.md` and `review-r2-probes/` were absent from the
assigned checkout and checked companion locations. Their location was requested
but not supplied. Implementation follows D1–D3 quoted in the brief. Local cases
reconstruct N1/N2 (15 retry variants), N5/N5b/N6, N8 and N9; these are **not
claimed to be the review's exact executed histories**. The three supplied
probe paths below remain unavailable and unrun.

Every generated queue replay now asserts that no person message is drawn in
the timeline and later in the tray. The independent placement oracle remembers
its own history. Preview expectations count Swift `Character` grapheme clusters,
including combining accents, skin-tone/ZWJ emoji and flags.

Coverage retains `REACHED = 5` and 300 histories per seed, runs seeds 0–4 and
requires every case to reach at least **10**. The generator gives held sends an
immediate queued receipt, explicitly creates a repair ahead of an earlier
person message, creates an eligible live steer host, and puts a pending approval
on that host. This raises the weak receipt, steer-offer and approval margins
without lowering `REACHED` or changing the no-flicker assertion.

| Seed | Lowest case count | Send retained through receipt |
| --- | ---: | ---: |
| 0 | 34 | 222 |
| 1 | 19 | 244 |
| 2 | 41 | 237 |
| 3 | 59 | 217 |
| 4 | 23 | 234 |

### Four targeted mutations

Each mutation runs its named regression against a passing control and requires
an assertion failure from an isolated mutant binary. Compilation errors do not
count as kills. Mutants never edit production source in place.

| Mutation | Named regression |
| --- | --- |
| claim moves tray row | `test_n1_n2_claim_requeue_defer_never_releases_a_tray_row` |
| deferred timeline row returns to tray | `test_d1_a_deferred_timeline_send_never_goes_back` |
| skip unknown-ID hydration | `test_n5_n5b_n6_watch_discovers_repairs_before_publishing` |
| app scroll retains opening pin | `test_n9_app_row_scroll_suspends_even_an_already_scheduled_pin` |

### Validation and final follow-up

The app build passed using SDK 26.5 with `app/build.sh build/fix4-app` at
production commit `7e5395e28`; the one-line reset-replay follow-up is `3aa3b8732`.
The final build status is recorded below. Builds stay in this workspace and
are not installed. The broad hold used `7e5395e28`; the queued review, extra
frontend and approval-reach runs include `3aa3b8732`. All four isolated mutant
binaries were rebuilt from `3aa3b8732` before those queued holds acquired the
lock. Two earlier
compiles rejected concurrently modified input; both were rerun successfully.
Earlier iteration failures prompted stronger generator contexts, one corrected
idle-note expectation, and protection against stale event state. The broad hold
also exposed an old approval test expecting a completed turn to remain live.
Commit `cce88b522` instead requires its state to become complete and the older
pending approval to retain Review; the complete approval-reach file is rerun.
Per-file rows use the latest run and the hold table retains that iteration's
failure explicitly.

The broad groups were not repeated after the one-line reset follow-up; the
queued review run explicitly covers ordinary and reset-replay hydration.

All pytest processes use the blocking shared `lockf`, default bounded Hypothesis
profile, no xdist workers, `env -u SUBFLEET_RETENTION_DORMANT -u PYTHONPATH`, and
a job-local uv cache because the default cache is not writable. SDK 26.5 and
`SUBFLEET_SWIFT_NESTED_SANDBOX=off` are required by this environment. Cached
probes consolidate the requested frontend groups into one serialized hold.

| Hold | Passed | Failed | Errors | Skipped | Pytest seconds | Locked seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| daemon | 108 | 0 | 0 | 0 | 86.00 | not instrumented |
| required | 337 | 1 | 0 | 0 | 752.03 | 765 |
| review-final | 66 | 0 | 0 | 0 | 1026.78 | 1039 |

Final build at `3aa3b8732`: **passed**.

Targeted mutation checks: **4/4 passed**, review-final (each pass proves its named mutant fails an assertion while its control passes).

| File | Passed | Failed | Errors | Skipped | Status |
| --- | ---: | ---: | ---: | ---: | --- |
| `tests/frontend/test_app_cutover_start.py` | 13 | 0 | 0 | 0 | required |
| `tests/frontend/test_conversation_view.py` | 6 | 0 | 0 | 0 | required |
| `tests/frontend/test_core_app_cutover_reading.py` | 15 | 0 | 0 | 0 | required |
| `tests/frontend/test_core_approval_notification.py` | 10 | 0 | 0 | 0 | required |
| `tests/frontend/test_core_approval_reach.py` | 40 | 1 | 0 | 0 | required |
| `tests/frontend/test_core_client.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_diff.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_draft.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_idle.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_live.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_live_failure.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_markdown.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_outbox.py` | 13 | 0 | 0 | 0 | required |
| `tests/frontend/test_core_protocol.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_question_card.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_queue_engine.py` | 75 | 0 | 0 | 0 | required |
| `tests/frontend/test_core_queue_tray.py` | 50 | 0 | 0 | 0 | required |
| `tests/frontend/test_core_search.py` | 20 | 0 | 0 | 0 | required |
| `tests/frontend/test_core_steer.py` | 43 | 0 | 0 | 0 | required |
| `tests/frontend/test_core_steer_properties.py` | 2 | 0 | 0 | 0 | required |
| `tests/frontend/test_core_store.py` | 10 | 0 | 0 | 0 | required |
| `tests/frontend/test_core_suggestions.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_suggestions_r1b.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_text_scale.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_core_timeline.py` | 23 | 0 | 0 | 0 | required |
| `tests/frontend/test_core_titles.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_disk_ruling.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_menu_view.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_pr124_fixes_ui.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_pr124_recovery_properties.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_pr124_review_r2_fixes_ui.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_queue_tray_fix4_mutations.py` | 4 | 0 | 0 | 0 | review-final |
| `tests/frontend/test_queue_tray_notice.py` | 2 | 0 | 0 | 0 | required |
| `tests/frontend/test_queue_tray_view.py` | 15 | 0 | 0 | 0 | required |
| `tests/frontend/test_reading_view.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_review_pr127_timeline.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_review_r219_fix4.py` | 39 | 0 | 0 | 0 | review-final |
| `tests/frontend/test_review_r219_probes.py` | 23 | 0 | 0 | 0 | review-final |
| `tests/frontend/test_status_model.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_visual_progress.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_visual_report_hygiene.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_visual_review_fixes.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_visual_round_five.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_visual_round_four.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_visual_round_three.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_visual_round_two.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_visual_snapshots.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/frontend/test_visual_suggestions.py` | 0 | 0 | 0 | 0 | unrun in fix-4 |
| `tests/unit/test_conversation_refresh.py` | 2 | 0 | 0 | 0 | daemon |
| `tests/unit/test_conversation_service.py` | 98 | 0 | 0 | 0 | daemon |
| `tests/unit/test_descriptors_desktop.py` | 8 | 0 | 0 | 0 | daemon |

Unavailable review probes, unrun:

- `review-r2-probes/test_review_r219_r2_probes.py`
- `review-r2-probes/test_review_r219_r1orig_probes.py`
- `review-r2-probes/ReviewR219R2RevealProbe.swift`

Latest recorded per-file total: **511 passed, 1 failed, 0 errors, 0 skipped**, across 21 files.

### Commits

- `11ce065a1` — Merge origin/release/217 for queue tray fix 4
- `3854b542c` — test: pin queue placement, repair discovery, and row reveal regressions
- `aab71829f` — fix: keep queue placement forward and hydrate focused conversation pages
- `199ea374b` — fix: preserve newer receipts when replaying completion events
- `03112b69b` — test: enforce forward placement across every replay and align prior oracles
- `64addba73` — test: kill queue placement, missing hydration, and retained-pin mutations
- `8d217759e` — test: assert the idle repair note remains a non-withdrawable notice
- `ba9a59849` — test: strengthen seeded steer and repair coverage margins
- `a54a5ef3f` — test: ensure pending-card coverage has a seeded margin
- `7e5395e28` — fix: preserve receipt state throughout historical event replay
- `cce88b522` — test: keep approval review reachable after a newer turn completes
- `e7e98d358` — test: hydrate unknown repair identities during reset replay
- `3aa3b8732` — fix: hydrate event identities through reset rebuilding pages
- `c492091bc` — docs: record fix4 cutoff status and pending verification

Checkpoint: final holds are pending or active; pending tests are not counted as
passes. No new hold will be initiated at or after 19:38 UTC (4 h 30 m). Existing
foreground lock waiters will finish before cleanup. A final report commit will
supersede this checkpoint with their results and the remaining unrun files.


## Fix-5 — 2026-10-18

Work stays in the assigned workspace. The shared Git metadata is read-only;
commits use the assigned `feat/queue-tray-217` name in workspace-local
`.git-local`. The original `.git` pointer and caller checkout are untouched.
No remote operation, push or history rewrite was performed.

### Base and regressions

Merged the cached `origin/release/217` at `3fbc4b7c5615` before the fixes
(commit `d5cd1dd8b`). It includes #207, #225 and #227 beyond the supplied head.
The only conflict was the acceptance contract's Changes list; both sets of
independent amendments were retained. The Swift protocol and presentation
sources remain identical to `c7e74a8f6` after that merge.

Tests-first commits `a53c950f8`, `14b3a1737` and `5c69fd4b1` add an exhaustive
codec check, real status result and request fixtures, an independent check of
a held conversation through the existing open codec, and registry-based
documentation checks. The initial locked run reported 12 passed, 3 failed,
1 skipped in 395.11 s. It exposed an incorrect fixture API; the fixture now
uses `set_legacy_hold`, as the import does, rather than `update_conversation`.
The corrected baseline against unchanged production Swift completed with
12 passed, 7 assertion failures and 1 skipped in 20.79 s: missing codec
coverage and request encoding, lost non-null `legacy_hold`, the old waiting
assertion, and three documentation checks. No compilation/setup error counted
as a regression. Final validation results follow below.

### Causes and corrections

The probe maintained its own codec list separately from `Ops.names`.
`Ops.all` now supplies typed operations to both name and codec visitors. The
C-25.2 result test iterates this registry and compares its membership with
Python's `CONVERSATION_OPS`; a new op must have a codec and a real fixture.
The live scenario also requires a `conversation.status` exchange.

`ConversationStatusResult` returns `conversation: Conversation` and
`message_ids: [String]`. The real fixture includes completed, approval-needed
and queued messages in sequence order. The audit of all 22 conversation view
fields found `legacy_hold` absent from Swift's `Conversation`; a non-null
import hold exercises its preservation independently of the status codec.
The remaining nested fields match `ConversationSettings`, `LastMessage` and
the existing open-ended JSON fields.

Item 3 comes from round 4's explicit acknowledgment condition,
`messageState == .waiting && !hasBoundJob`, in `WorkPresentation.swift`.
The review probe assigns states directly; no re-read, hydration or completion
event runs in that scenario. The old test assumed every waiting turn had a
job binding. D1 requires an unbound wait to keep its reason and Withdraw
action visible, already covered by the external-writer regression. The
visual fixture now checks both unbound waiting (acknowledgment shown) and
bound waiting (status in the bottom strip). Production presentation behavior
is retained.

The operation and read-only descriptor sets already contain the new op, and
`CAPABILITIES` already advertises `conversation.status.v1`. The contract now
names its read-only shape and capability in C-25.1/C-25.2; desktop design and
protocol map documentation agree. Generic op-coverage tests also close older
omissions in the contract list and design table.

### Validation and delivery

Python 3.14.7 (free-threaded): **969 passed, 1 failed, 1 skipped** in the
completed final runs. Expected tests-first failures are excluded from these
counts. No pytest invocation was launched after the conservative 4h30 cutoff
at 20:21 UTC. The last queue view hold launched at 20:19 and completed with
15 passes; the 22-file core/review hold passed all 333 cases, including all
39 fix-4 review regressions.

The failure is `test_c16_6_the_kernel_ceiling_is_read_on_macos`:
`/usr/sbin/sysctl -n kern.maxfilesperproc` returns `Operation not permitted`.
Both `subfleet/descriptors.py` and its test are identical to `c7e74a8f6`.
The live-daemon case skips because macOS boot identity is unavailable in this
sandbox; the exhaustive codec regression independently covers its original
missing-codec failure. Neither test nor production behavior was weakened to
hide these environment restrictions.

Every run used one foreground pytest process under its own shared-lock hold,
with the bounded CI Hypothesis profile. The queue tray hold was interrupted
while active at 19m37s to preserve the 20-minute bound, after all 50 tray
cases completed. Only the unfinished notice/engine files resumed in the next
hold. The completed active holds all stayed below 20 minutes. `PYTHONPATH`
and `SUBFLEET_RETENTION_DORMANT` were unset; uv and compiler caches used
owned scratch directories. There was no deep profile or global cache clean.

| Hold | Passed | Failed | Errors | Skipped | Pytest seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| `frontend-required` | 15 | 0 | 0 | 1 | 353.34 |
| `queue-required` | 50 | 0 | 0 | 0 | 1177.27 |
| `queue-tail` | 77 | 0 | 0 | 0 | 476.75 |
| `unit-required` | 479 | 1 | 0 | 0 | 112.27 |
| `frontend-rest` | 333 | 0 | 0 | 0 | 323.31 |
| `frontend-view` | 15 | 0 | 0 | 0 | 122.51 |

Per-file counts below cover the requested frontend tree and daemon group.
`Unrun` means no case from that file ran. The 19 unrun frontend files, Python
3.12 and a separate full app-bundle build were left unrun at the cutoff.

| File | Passed | Failed | Errors | Skipped | Status |
| --- | ---: | ---: | ---: | ---: | --- |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_app_cutover_start.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_conversation_view.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_core_app_cutover_reading.py` | 15 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_approval_notification.py` | 10 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_approval_reach.py` | 41 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_client.py` | 18 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_diff.py` | 7 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_draft.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_core_idle.py` | 1 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_live.py` | 0 | 0 | 0 | 1 | Sandbox boot identity unavailable |
| `tests/frontend/test_core_live_failure.py` | 1 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_markdown.py` | 42 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_outbox.py` | 13 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_protocol.py` | 10 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_question_card.py` | 10 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_queue_engine.py` | 75 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_queue_tray.py` | 50 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_search.py` | 20 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_steer.py` | 43 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_steer_properties.py` | 2 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_store.py` | 10 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_suggestions.py` | 7 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_suggestions_r1b.py` | 4 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_text_scale.py` | 7 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_timeline.py` | 23 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_core_titles.py` | 1 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_disk_ruling.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_menu_view.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_pr124_fixes_ui.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_pr124_recovery_properties.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_pr124_review_r2_fixes_ui.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_queue_tray_fix4_mutations.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_queue_tray_notice.py` | 2 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_queue_tray_view.py` | 15 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_reading_view.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_review_pr127_timeline.py` | 4 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_review_r219_fix4.py` | 39 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_review_r219_probes.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_status_model.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_visual_progress.py` | 5 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_visual_report_hygiene.py` | 15 | 0 | 0 | 0 | Passed |
| `tests/frontend/test_visual_review_fixes.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_visual_round_five.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_visual_round_four.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_visual_round_three.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_visual_round_two.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_visual_snapshots.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/frontend/test_visual_suggestions.py` | 0 | 0 | 0 | 0 | Unrun: cutoff |
| `tests/unit/test_conversation_service.py` | 98 | 0 | 0 | 0 | Passed |
| `tests/unit/test_descriptors.py` | 15 | 1 | 0 | 0 | Sandbox sysctl denied |
| `tests/unit/test_protocol_registry.py` | 4 | 0 | 0 | 0 | Passed |
| `tests/unit/test_protocol_secrecy.py` | 325 | 0 | 0 | 0 | Passed |

### Commits and transfer

All commits use workspace-local `.git-local` metadata on
`feat/queue-tray-217`; the caller checkout and original `.git` pointer are
unchanged. No push or history rewrite occurred. Every new commit carries
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

Implementation and validation commits before this final report update:

- `d5cd1dd8b` — Merge cached origin/release/217 for queue tray fix 5
- `a53c950f8` — test: cover every operation codec and status result field
- `14b3a1737` — test: use legacy hold fixture API and cover status requests
- `5c69fd4b1` — test: expose held conversation field loss independently of status codec
- `ab3405d11` — fix: derive probe codecs from the typed operation registry
- `a7a74deb0` — test: distinguish bound and unbound waiting acknowledgments
- `a86c4ee05` — docs: complete the operation and status capability contracts
- `8a83c9e0f` — docs: record fix5 causes and tests-first regression evidence
- `1b875f026` — docs: record passing fix5 frontend protocol and visual checks
- `4b9f27601` — docs: record queue tray passes and bounded hold split
- `7e97afdcf` — docs: record completed queue notice and engine validation
- `c15633dd9` — docs: record daemon validation and sandbox kernel restriction
- `dfa92c941` — docs: preserve historical report sections when updating fix5
- `948d95b86` — docs: record fix5 cutoff and pending final view hold

The final report update is at the delivered branch head. The ignored transfer
bundle `docs/reports/2026-10-18-queue-tray-fix5.bundle` contains the delivered
branch and is not staged. Its prerequisites are the supplied `c7e74a8f6` and
cached release `3fbc4b7c5`, already present in the shared repository. The
bundle is verified against local metadata. Test logs, XML/JSON evidence and
bundles are excluded from commits. Owned test scratch and generated caches
are removed after validation; `.git-local` and the transfer bundle are kept.

## Fix 6 — 2026-10-20, review r3

### Release integration and delivery

The first change merges cached `origin/release/217` at `473fe2941`, including
#222 (`f99da54b0`) and the subsequent release merges, into the supplied
`c154b2eaa22f`. No remote was contacted. The only conflict was the change log
in `docs/acceptance-contract.md`: both sides' entries were concatenated;
every clause and both sides' change-log entries were retained. Later contract
amendments accompany their corresponding implementation commits.

The shared Git metadata and original `.git` pointer are unwritable in this
sandbox. Commits use workspace-local `.git-local` metadata on
`feat/queue-tray-217`; the caller checkout and shared refs are unchanged.
A verified transfer bundle is provided at
`docs/reports/2026-10-20-queue-tray-fix6.bundle` (deliberately untracked). Its
prerequisites are the supplied `c154b2eaa22f` and merged release `473fe2941`.
No history is rewritten and nothing is pushed. All new commits have the requested
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` trailer.

### Findings, causes and fixes

1. **P2, optional status RPC stalled watch.** Readiness requires
   `conversations.v1`, but focused refresh called `conversation.status`
   unconditionally. Two attempts against a ready daemon lacking
   `conversation.status.v1` delivered zero pages. `ConversationEngine` now
   caches the capability discovered by availability/reconnect checks and
   uses the cheap refresh only when advertised. Older daemons use a raw
   `conversation.open` re-read, preserving feed-thread isolation from the
   outbox; changed identities outside that snapshot are fetched separately.
   C-25.1 and C-29.7 describe the fallback. The status handler itself is
   unchanged and the modern refresh still performs no transcript read.

2. **P2, opening omitted older queued messages.** The launch watch baseline
   can consume a queue's changes before focus. `conversation.open` returns
   the latest 50 receipts; a later empty watch page cannot discover the
   missing older identities. Opening with status support now reads the
   complete identity set and hydrates missing receipts in batches of at most
   200, sorts them by sequence, and only then publishes the open result.
   Regressions use 51, 120 and 251 held messages; the last crosses the receipt
   batch boundary. C-29.7 records the complete opening count guarantee.

3. **P2, catch-up undid an explicit reveal.** The opening pin already honored
   row reveals, but `FollowKey.move` independently jumped whenever either
   key was settling. In the real `ConversationView`, a reveal at offset 250
   followed by the first 20 history rows jumped to 8965 with `caughtUp=false`;
   the caught-up control remained at 250. The view now suspends catch-up
   following on its existing row-reveal notification, marks the end as away,
   and clears suspension when the end comes into view. C-27.5/C-29.7 retain
   D3 across later history and event pages.

4. **P3, a collapsed tray hid a search match.** Search targeted
   `person:queued5` for the unique sixth of six held messages, but the tray
   still rendered three rows. Tray rows now expose their search identities
   inside their own `ScrollViewReader`. The owning revealer expands the tray
   before positioning its row; the timeline consumes only its own targets,
   so it cannot clear a tray request during expansion. C-29.7/C-29.12 describe
   expansion and revealing within the tray.

The #222 integration also classifies `SUBFLEET_FIX4_MUTANTS`,
`SUBFLEET_SWIFT_NESTED_SANDBOX` and the desktop's
`SUBFLEET_OPEN_CONVERSATION` in `launch_env.LOCAL_TOOL_NAMES`. This inventory
adds no ambient provider/helper authority: the existing selector still drops
all three and doctor lists their names. Swift compilation defines are not
environment controls. The new round-3 inventory regression checks both the
classification and continued exclusion.

### Tests-first evidence

Production Swift stayed byte-for-byte identical to `c154b2eaa22f` through
the final baseline run. All authoritative failures were assertions, with
no compilation/setup errors: four engine cases (legacy watch plus three
queue sizes), two real-view cases, and the environment inventory case.
The final UI baseline rendered three rows before and after activation and
reproduced the 250 → 8965 jump. Earlier view-probe calibration runs are not
counted as regression evidence: the corrected fixture reads its first
history page after revealing, and distinguishes the Withdraw column from
unrelated same-width controls on macOS 26.

The new regressions are in `tests/frontend/test_review_r219_fix6.py`, backed
by `ReviewR219Fix6EngineProbe.swift` and `ReviewR219Fix6ViewProbe.swift`.
The engine uses real service result fixtures, including the older daemon's
capability shape. The view probe hosts the actual conversation view in
windows never ordered front. `tests/unit/test_launch_env_round3.py` carries
the local-name classification regression.


### Validation status

All **482 test files** have collected results: the 48 existing frontend files
plus the fix-6 file, all 327 unit files, and all 106 fake files. The table
counts each test identity once, using its latest outcome; tests-first red
runs and probe-calibration failures are excluded. No test file was omitted.
The distinct totals are **15,576 passed, 12 failed, 98 skipped and two
expected failures**, with no setup errors. No test file remains uncollected.
The final invocation was requested at 11:04 UTC, before the 11:20 UTC
cutoff; no further holds were requested after the cutoff.

| Directory | Files | Passed | Failed | Errors | Skipped | Xfailed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `tests/frontend` | 49 | 828 | 0 | 0 | 1 | 0 |
| `tests/unit` | 327 | 12973 | 8 | 0 | 20 | 0 |
| `tests/fake` | 106 | 1775 | 4 | 0 | 77 | 2 |

All six fix-6 regressions pass, including the 251-message batch boundary,
the real conversation-view offset, and the revealed row's actual `queued5`
action identity. All four mutation checks pass. The 15 expressly required
registry, routing, refresh, state-contract, module-name and launch-environment
files passed **442 cases, no failures or skips**; these cases are included,
not added again, in the directory totals above.

The full unit invocation was interrupted through this job's own foreground
terminal at **18m38s**, preserving 11,711 passes, nine failures and 15 skips
before releasing the lock. The continuation deselected those completed test
IDs and ran the unfinished unit cases plus the entire fake directory with
four workers. It completed in **16m17s**: 3,037 passed, four failed, 82 skipped,
and two expected failures. Its only deliberately repeated unit case was the
app-server crash diagnostic: it passed in 0.322s. The earlier failure saw the
valid broken-pipe probe-refusal branch before the fixture could reach the
specific exit-message assertion. No production guard change was made.
Three unnamed interrupted-setup XML records are not counted as tests; their
unfinished identities were eligible for the continuation.

### Sandbox failures and unexecuted cases

The remaining twelve failures are in Python files unchanged by fix 6. The
only Python production change after the release merge is the local tooling
name inventory; it changes no launch selection or process-inspection path.

| File | Failed cases | Cause |
| --- | ---: | --- |
| `tests/unit/test_hooks.py` | 1 | `/bin/ps -axww` cannot execute (`Operation not permitted`). |
| `tests/unit/test_daemon_qos_tools.py` | 1 | `/bin/ps -M` cannot execute (`Operation not permitted`). |
| `tests/unit/test_descriptors.py` | 1 | `kern.maxfilesperproc` cannot be read; the kernel ceiling is `None`. |
| `tests/unit/test_doctor.py` | 2 | Denied process/boot reads correctly return `unknown`, where these real-process tests expect live/dead verdicts. |
| `tests/unit/test_importer.py` | 3 | Uninspectable identities remain conservatively live/external, and Subfleet process ownership cannot be established. |
| `tests/fake/test_daemon_stacks.py` | 4 | `procs.boot_id()` raises `macOS boot identity is unavailable`, before a fixture can report ready or the test daemon can initialize. |

The live frontend case is unexecuted: `test_core_live.py` reports
`the daemon's process checks need ps/sysctl: macOS boot identity is unavailable`.
The other process-dependent skips are eleven unit daemon/legacy-hold cases,
one guardian case, 75 fake daemon cases, and one bounded-turn integration
case. Three unit filesystem-image cases cannot create HFS+/case-sensitive
HFS+/ExFAT devices (`Device not configured`); one host-push measurement is an
explicit opt-in benchmark; one fake backlog case cannot read a usable
`kern.ipc.somaxconn`. Four retention cases also remain unexecuted. The
owned scratch cache was seeded with `sortedcontainers==2.4.0` before the
last lock request, and its installed package metadata was verified. After
a 33-minute lock wait, the offline helper still required a registry
download, so the rerun reported **four skips in 5.54s**, rather than passes.
The time cutoff prevents another hold. The two expected failures are the existing hung-mount
wall-clock limitation and the C-6.10 alternating-wait verdict limitation.
No live store or live daemon was accessed.

### App build and execution hygiene

The development app build passed with `sh app/build.sh --dev build/fix6-app`,
including plist validation and ad hoc signing. It was not launched. There
is no `Package.swift` in the repository or app directory, so a literal
SwiftPM `swift build` target is unavailable; `app/build.sh` is the app's build.

The first implementation regression run finished in 18m55s with five passes
and one overly broad search-probe assertion: it required six native Withdraw
controls although scrolling materialized five. The refined real-view probe
checks the visible row's `message.cancel` identity instead. All final
frontend files passed together after that refinement. The extracted tray
content's explicit return was corrected in `558f84c53` before this run
completed. Ten model probes, eleven normal view probes, four mutation probes
and the refined real-view probe compiled for the final sources. Preparatory
compile errors are excluded from test counts.

Every top-level pytest invocation used a separate, serial, foreground
`/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock` hold, with no
lock timeout or polling. Every completed hold stayed below 20 minutes, with
at most four workers. No stopped pytest was relaunched. The bounded CI
Hypothesis profile was used, with `PYTHONPATH` and
`SUBFLEET_RETENTION_DORMANT` unset. The default uv cache refused writes;
owned scratch caches were used and removed by their runners, including the
last seeded cache. The generated build directory, dependency environment
and thirteen bytecode-cache directories have been removed. No workspace
`.hypothesis` directory remains. All recorded process identities from this
job, including the final runner and its `lockf`, have exited. The original
`.git` pointer is unchanged. Workspace-local Git metadata and the small
transfer bundle are retained; no logs, JSON evidence or bundles were staged.

### Step commits

This ledger lists the fifteen completed steps before this final report
commit, in first-parent order after `c154b2eaa22f`; it omits the release's
already-existing history. Every new commit has the requested coauthor
trailer. The final report commit is the delivery branch tip in the verified
bundle and the final response. Shared Git metadata remains untouched.

- `8f3e89213` — Merge origin/release/217 into queue tray fix 6
- `438624f13` — test: reproduce queue tray review r3 failures and classify local controls
- `4dd053421` — test: exercise first history arrival and measure actual tray controls
- `a48532c8b` — fix: classify queue tray tooling controls as local-only environment names
- `121d6f779` — test: identify tray Withdraw controls by their column
- `003f6d387` — test: assert search activation actually expands the queued rows
- `6bab1ad49` — fix: gate status refresh and hydrate complete queues before opening
- `6c2d90c13` — fix: suspend catch-up following after explicit row reveals
- `5f217cc8a` — fix: expand and reveal search matches inside the queue tray
- `7917dcffa` — docs: record fix6 causes, contracts and authoritative baseline failures
- `558f84c53` — fix: return the extracted queue tray content view
- `6b4da2e50` — test: verify the revealed queue row by its action and erase host generics
- `b99d12477` — docs: record app build and 442 passing required checks for fix6
- `5c0ec7a43` — docs: record all 49 frontend files passing fix6 validation
- `07af45d98` — docs: record complete unit and fake validation with sandbox diagnostics

### Counts per test file

| File | Passed | Failed | Errors | Skipped | Xfailed | Status |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `tests/fake/test_admission_disk.py` | 27 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_admission_held_waiters.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_admission_latency.py` | 14 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_admission_lease_waiters.py` | 16 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_admission_liveness.py` | 32 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_admission_priority.py` | 32 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_admission_properties.py` | 27 | 0 | 0 | 0 | 1 | Completed |
| `tests/fake/test_admission_ramp.py` | 19 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_admission_ramp_retention.py` | 11 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_admission_route_isolation.py` | 93 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_admission_stateful.py` | 16 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_admission_visibility.py` | 43 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_batch_label.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_canonical_identity.py` | 24 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_canonical_identity_fix2.py` | 21 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_canonical_identity_fix3.py` | 17 | 0 | 0 | 0 | 1 | Completed |
| `tests/fake/test_canonical_identity_fix4.py` | 11 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_canonical_identity_fix5.py` | 14 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_capacity_steer_daemon.py` | 54 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_capacity_wait_backoff.py` | 30 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_census_escapers_daemon.py` | 0 | 0 | 0 | 9 | 0 | Completed |
| `tests/fake/test_census_recycled_pid_daemon.py` | 29 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_codex_ca_bundle_fake.py` | 0 | 0 | 0 | 6 | 0 | Completed |
| `tests/fake/test_codex_turn_completed_fake.py` | 109 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_conversation_pools.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_conversation_wakes_fake.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_crash_matrix.py` | 0 | 0 | 0 | 9 | 0 | Completed |
| `tests/fake/test_daemon_contract.py` | 0 | 0 | 0 | 21 | 0 | Completed |
| `tests/fake/test_daemon_descriptors.py` | 12 | 0 | 0 | 1 | 0 | Completed |
| `tests/fake/test_daemon_launch.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_daemon_stacks.py` | 1 | 4 | 0 | 1 | 0 | Completed |
| `tests/fake/test_descriptor_exhaustion.py` | 0 | 0 | 0 | 2 | 0 | Completed |
| `tests/fake/test_detached_cwd_pins.py` | 18 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_detached_retention_fence.py` | 89 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_enrollment_guardian.py` | 0 | 0 | 0 | 1 | 0 | Completed |
| `tests/fake/test_fake_provider.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_finalization_replay.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_folder_spelling.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_frozen_stream_nonregular.py` | 32 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_gate_end_to_end.py` | 10 | 0 | 0 | 1 | 0 | Completed |
| `tests/fake/test_identity_rollback.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_idle_cost.py` | 0 | 0 | 0 | 2 | 0 | Completed |
| `tests/fake/test_interactive_claude_titles.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_interactive_steer.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_lane_fault.py` | 20 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_lane_pilot.py` | 24 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_lanes_enroll.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_lanes_reenroll.py` | 19 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_lanes_reenroll_keychain.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_launch_env_daemon.py` | 49 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_launch_env_providers.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_launch_gates_above_stdio.py` | 0 | 0 | 0 | 9 | 0 | Completed |
| `tests/fake/test_lost_acknowledgement.py` | 0 | 0 | 0 | 3 | 0 | Completed |
| `tests/fake/test_main_refusal_write_dir.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_mcp_job_contract.py` | 12 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_native_maintenance_startup.py` | 41 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_notice_delivery.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_notice_header.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_picker_native.py` | 3 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_policy_sandbox.py` | 25 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_probe_cancel.py` | 24 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_probe_recovery.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_probe_turn.py` | 43 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_prompt_seam.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_provider_active_cap_fake.py` | 60 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_provider_cap_review_evidence.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_provider_qos.py` | 0 | 0 | 0 | 2 | 0 | Completed |
| `tests/fake/test_record_secrecy.py` | 44 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_request_routing.py` | 3 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_reserve_authorization.py` | 12 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_reserve_authorization_validation.py` | 11 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_reset_credit_incident.py` | 19 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_resume_cli.py` | 0 | 0 | 0 | 1 | 0 | Completed |
| `tests/fake/test_resume_contract.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_retention_dormant.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_review130_ab.py` | 12 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_review_r185_adversarial.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_route_view.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_routing_end_to_end.py` | 14 | 0 | 0 | 2 | 0 | Completed |
| `tests/fake/test_routing_tier_preferences.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_salvage_adversarial.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_salvage_finalization.py` | 47 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_sessions_end_to_end.py` | 27 | 0 | 0 | 6 | 0 | Completed |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_state_fixture_processes.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_status_payload.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_suggestions_fake.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_tier_hold_by_demand.py` | 27 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_timers_busy_admission.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_timers_end_to_end.py` | 6 | 0 | 0 | 1 | 0 | Completed |
| `tests/fake/test_turn_listing.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_turn_retention_cwd.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_turn_retention_cwd_properties.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_turn_retention_job_directory.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_turn_retention_job_properties.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_turn_retention_quarantine.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_turn_trees.py` | 12 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_turn_wait_reasons.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_unadmittable_pin_admission.py` | 24 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_unadmittable_pin_rereview.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_unadmittable_pin_review.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_workspace_contract.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_workspace_enospc.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_workspace_transient.py` | 24 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_writable_fanout.py` | 28 | 0 | 0 | 0 | 0 | Completed |
| `tests/fake/test_writer_retention_fence.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_app_cutover_start.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_conversation_view.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_app_cutover_reading.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_approval_notification.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_approval_reach.py` | 41 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_client.py` | 18 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_diff.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_draft.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_idle.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_live.py` | 0 | 0 | 0 | 1 | 0 | Completed |
| `tests/frontend/test_core_live_failure.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_markdown.py` | 42 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_outbox.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_protocol.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_question_card.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_queue_engine.py` | 75 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_queue_tray.py` | 50 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_search.py` | 20 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_steer.py` | 43 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_steer_properties.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_store.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_suggestions.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_suggestions_r1b.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_text_scale.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_timeline.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_core_titles.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_disk_ruling.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_menu_view.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_pr124_fixes_ui.py` | 17 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_pr124_recovery_properties.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_pr124_review_r2_fixes_ui.py` | 19 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_queue_tray_fix4_mutations.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_queue_tray_notice.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_queue_tray_view.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_reading_view.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_review_pr127_timeline.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_review_r219_fix4.py` | 39 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_review_r219_fix6.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_review_r219_probes.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_status_model.py` | 25 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_visual_progress.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_visual_report_hygiene.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_visual_review_fixes.py` | 43 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_visual_round_five.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_visual_round_four.py` | 27 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_visual_round_three.py` | 28 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_visual_round_two.py` | 85 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_visual_snapshots.py` | 3 | 0 | 0 | 0 | 0 | Completed |
| `tests/frontend/test_visual_suggestions.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_admission_uncapped.py` | 27 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_agent_secret_credentials.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_app_cutover_daemon.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_approval_commit.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_approval_resolution.py` | 33 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_boot_identity.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_capacity_desktop.py` | 20 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_capacity_steer_route.py` | 22 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_capacity_view.py` | 20 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_census_counted.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_census_identity_uncertainty.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_census_procargs.py` | 52 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_census_recycled_pid.py` | 25 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_census_responsibility_failures.py` | 20 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_census_responsibility_identity.py` | 17 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_census_responsibility_races.py` | 34 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_census_survivors_review.py` | 14 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_ci_retry_known_flakes.py` | 72 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_ci_shard_plugin.py` | 48 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_adapter.py` | 170 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_attest.py` | 19 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_cards.py` | 96 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_cards_wiring.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_identity.py` | 30 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_large_stream.py` | 30 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_mcp.py` | 32 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_stream.py` | 60 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_synthetic_error.py` | 63 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_turn.py` | 61 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_usage.py` | 28 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_claude_v1_parity.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_cli.py` | 222 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_cli_batch.py` | 18 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_cli_turns.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_codex_adapter.py` | 172 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_codex_ca_bundle.py` | 46 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_codex_probe.py` | 55 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_codex_shim.py` | 31 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_codex_turn.py` | 58 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_codex_turn_completed.py` | 41 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_compat.py` | 1615 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_continuation_r2.py` | 11 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_catalog.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_catalog_lifecycle.py` | 21 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_connections.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_diff.py` | 39 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_fifo_reads.py` | 26 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_handoff.py` | 48 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_handoff_briefs.py` | 28 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_handoff_dispatch_races.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_history.py` | 22 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_peers_attachments.py` | 44 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_reconcile.py` | 36 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_redact.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_refresh.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_service.py` | 98 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_state_reads.py` | 18 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_status.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_steer_service.py` | 45 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_store.py` | 32 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_stream_assertion.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_titles.py` | 89 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_waits.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_conversation_wakes.py` | 22 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_cpu_scaling.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_accept.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_connections.py` | 30 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_fifo_reads.py` | 27 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_init_release.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_inspection_load.py` | 22 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_inspection_properties.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_integration.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_log_scrubbing.py` | 86 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_qos_tools.py` | 3 | 1 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_settle.py` | 43 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_daemon_verbs.py` | 15 | 0 | 0 | 5 | 0 | Completed |
| `tests/unit/test_decision_horizon.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_descriptor_review_fixes.py` | 56 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_descriptors.py` | 15 | 1 | 0 | 0 | 0 | Completed |
| `tests/unit/test_descriptors_desktop.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_desktop_account_cache.py` | 3 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_desktop_in_use.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_desktop_ledger.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_disk_admission.py` | 27 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_disk_floor.py` | 60 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_doctor.py` | 42 | 2 | 0 | 0 | 0 | Completed |
| `tests/unit/test_doctor_ca_bundle.py` | 16 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_doctor_identity.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_empty_provider_results.py` | 334 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_enrollment_runner.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_fake_codex_http.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_filesystem_identity_agreement.py` | 37 | 0 | 0 | 3 | 0 | Completed |
| `tests/unit/test_folders.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_forget_paced.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_fresh_until.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_frozen_provider_streams.py` | 48 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_gate_admission.py` | 24 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_gate_merge.py` | 59 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_gate_replay.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_gate_revision.py` | 16 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_gate_service.py` | 25 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_gate_verdict.py` | 45 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_guard_overlay.py` | 22 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_guard_trust.py` | 98 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_guardian.py` | 12 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_guardian_qos.py` | 14 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_handler_refusals.py` | 317 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_held_waiter_holdback.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_hidden_environment.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_hook_cost.py` | 3 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_hooks.py` | 66 | 1 | 0 | 0 | 0 | Completed |
| `tests/unit/test_hooks_native_guard.py` | 18 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push.py` | 118 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_admission.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_build.py` | 10 | 0 | 0 | 1 | 0 | Completed |
| `tests/unit/test_host_push_build_budget.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_cli.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_config_cost.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_copy.py` | 11 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_export.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_intake.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_metadata.py` | 33 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_mutations.py` | 24 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_names.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_quarantine_restart.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_restart.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_retention.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_storage_recovery.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_storage_retry_budget.py` | 60 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_store_completion_pr183.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_store_interleavings.py` | 26 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_trees.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_host_push_validated_top.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_importer.py` | 145 | 3 | 0 | 0 | 0 | Completed |
| `tests/unit/test_inspection_pacing_properties.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_keychain_exec_guard_properties.py` | 3 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_lane_answers.py` | 27 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_lanes_transfer.py` | 79 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_launch_env_allowlist.py` | 27 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_launch_env_helpers.py` | 20 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_launch_env_mutations.py` | 41 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_launch_env_policy.py` | 26 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_launch_env_round2.py` | 67 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_launch_env_round3.py` | 68 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_launch_env_round4.py` | 40 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_launch_env_round5.py` | 39 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_lease_waiter_selection.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_legacy_hold.py` | 50 | 0 | 0 | 6 | 0 | Completed |
| `tests/unit/test_limit_from_model_text.py` | 95 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard.py` | 324 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round10.py` | 60 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round10_mutations.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round3.py` | 22 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round4.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round5.py` | 11 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round6.py` | 112 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round7.py` | 50 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round7_mutations.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round8.py` | 100 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round8_mutations.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round9.py` | 89 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_round9_mutations.py` | 12 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_live_store_guard_state_root.py` | 22 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_lockwatch.py` | 20 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_mcp_cli_contract.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_mcp_scheduler_contract.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_mirror_flags_faults.py` | 29 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_mirror_flags_model.py` | 12 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_mirror_flags_stateful.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_native_operations.py` | 28 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_notice_rendering.py` | 21 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_notify_push.py` | 31 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_offline.py` | 33 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_operator_hold_closures.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_operator_notices.py` | 79 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_outcome_unknown.py` | 58 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_person_boundary.py` | 97 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_person_boundary_identity.py` | 25 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_picker.py` | 39 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_placement_ramp.py` | 26 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_policy.py` | 178 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_policy_disk.py` | 57 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_policy_ramp.py` | 46 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_policy_support.py` | 14 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_policy_tier_preferences.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_pr124_fixes.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_pr124_review_r2_fixes.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_pr192_review_r1.py` | 21 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_pr192_review_r2.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_procs.py` | 47 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_protocol_registry.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_protocol_secrecy.py` | 325 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_provider_active_cap.py` | 50 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_prune_217.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_prune_decision_backup_verification.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_prune_decision_fingerprints.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_prune_decisions.py` | 48 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_prune_restore.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_quota_projection.py` | 28 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_rank_review_report.py` | 3 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_reap_liveness.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_rediscovery_git_trace.py` | 17 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_relay.py` | 24 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_render.py` | 20 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_render_quota_projection.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_reserve_authorization_plumbing.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_reset_credit_claim_timing.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_reset_credit_demand.py` | 68 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_reset_credit_properties.py` | 11 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_reset_credit_review_r1.py` | 22 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_reset_credit_review_r2.py` | 52 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_reset_credit_review_r3.py` | 23 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | 12 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_resource_identity.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_81_notes.py` | 38 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_archive.py` | 109 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_archive_properties.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_case_alias.py` | 56 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_nested_leases.py` | 27 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_ordinary_trees.py` | 0 | 0 | 0 | 4 | 0 | Completed |
| `tests/unit/test_retention_qos.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_readback.py` | 37 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_rediscovery.py` | 17 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_regenerable.py` | 26 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_regenerable_properties.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_salvage_properties.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_shared_folders.py` | 62 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_sweep_race.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_turn_contract.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_turn_workdirs.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_turns.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_retention_worktrees.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review130_pass.py` | 21 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_pr127_fixes.py` | 12 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_pr127_history.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_pr127_pr_wakes.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_pr127_report.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_pr127_requests.py` | 17 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_pr127_service.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_r2_repro.py` | 31 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_r3_measure.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_r3_repro.py` | 12 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_r4_repro.py` | 27 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_r5_repro.py` | 17 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_r6_probes.py` | 30 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_review_r6_restart.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_round10_refusal_context.py` | 59 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_route_check.py` | 24 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_salvage.py` | 31 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_salvage_optional_transient.py` | 15 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_salvage_unindexable.py` | 86 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_scheduler.py` | 97 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_scheduler_priority_callers.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_scheduler_split.py` | 3 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_scheduler_tier_preferences.py` | 9 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_scheduler_turns.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_scheduler_weekly.py` | 33 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_scoped_stream_capacity.py` | 17 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_scrub_key_order.py` | 70 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_cli.py` | 142 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_handoff.py` | 117 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_headless_properties.py` | 5 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_mirror.py` | 41 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_mirror_cache_coherence.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_mirror_fifo.py` | 49 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_mirror_hot_health.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_mirror_hot_progress.py` | 16 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_mirror_hot_regressions.py` | 22 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_mirror_inventory.py` | 8 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_mirror_load_gap.py` | 85 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_mirror_safety.py` | 28 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_mirror_write_churn.py` | 3 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_nudge.py` | 44 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_registry.py` | 24 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_revive.py` | 45 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_revive_reads.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_sessions_transcripts.py` | 71 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_settle_properties.py` | 129 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_soak_tools.py` | 49 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_status_json.py` | 56 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_steer_invariants.py` | 248 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_stop_alarm.py` | 1 | 0 | 0 | 1 | 0 | Completed |
| `tests/unit/test_stop_watchdog.py` | 29 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_store_contention_queries.py` | 212 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_store_ids.py` | 27 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_store_migration_2.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_store_properties.py` | 3 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_store_readers.py` | 26 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_store_snapshot_plans.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggest_cli.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggest_hint.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggestion_error_boundaries.py` | 22 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggestion_properties.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggestion_secret_properties.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggestion_structural_errors.py` | 40 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggestions.py` | 30 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 30 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggestions_r3_channels.py` | 14 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggestions_review_boundaries.py` | 7 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggestions_review_multiline.py` | 6 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_suggestions_review_response.py` | 3 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_tick_prefilter.py` | 30 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_timers_alerts.py` | 17 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_timers_busy_read.py` | 115 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_timers_daemon.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_timers_keepalive.py` | 14 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_timers_probe.py` | 35 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_timers_reset_credits.py` | 47 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_timers_retention.py` | 20 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_title_review_regressions.py` | 4 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_turn_attachment_replay.py` | 13 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_turn_overlap.py` | 10 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_turn_replay.py` | 33 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_turn_runner.py` | 76 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_ultracode_effort.py` | 39 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_unadmittable_pin.py` | 12 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_wait_hub.py` | 44 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_wake_cli.py` | 2 | 0 | 0 | 0 | 0 | Completed |
| `tests/unit/test_weekly_rank_vs_base.py` | 26 | 0 | 0 | 0 | 0 | Completed |

## Fix 7 — 2026-10-20, CI macOS 15 search reveal

Cached `origin/release/217` was still `473fe2941`, already incorporated in
`a66102fa9`; no merge or conflict resolution was needed and no remote was
contacted. The shared index refused `index.lock` with Operation not permitted.
This round's commits use workspace-local `.git-local` metadata on
`feat/queue-tray-217`, preserving the supplied history and original `.git`
pointer. Named files only are staged. Nothing is pushed.

### Cause and implementation

Expansion previously happened only in `SearchRevealer.Follower`'s background
`.task`, through a closure mutating the tray's `expanded` state. The tray did
not itself observe the palette's published reveal. Consequently its row set
depended on an asynchronous view lifecycle callback, the dependency exposed
by CI's macOS 15 result (three controls both before and after activation).
The old width/position finder was independently fragile: the tests-first
run on this host counted five after expansion and found zero row identifiers.

`QueueTrayPresentation` derives the visible rows in Foundation from
`expanded || revealTargetsTray`, checking both conversation and row identity.
A directly observed palette passes the reveal into the rendering view, which
owns expansion state and keeps the tray open after consumption. No task expands
it. One geometry preference on the whole tray reports its row set, outside
`ViewThatFits`' alternative measurements. Scrolling accepts that layout only
when its reveal token equals the current request and its ids include the target.
This also handles a repeat selection or a match in the original three rows.
An explicit `onChange` schedules the scroll; cancelled or superseded scrolls
cannot clear the current request. C-29.12 and its change log describe the rule.

SwiftUI exposes no AX tree in these offscreen hosts without an assistive client;
its accessibility modifier alone does not populate the actual `NSButton`.
A small AppKit host keeps the original SwiftUI link, action, caption scale and
enabled state, and binds `queue-tray-withdraw-<message-id>` to the real button's
native identifier and accessibility identifier after layout. The finder uses
that stable identity, including the exact sixth row when clicking. Geometry
matching remains a reported legacy fallback, and its use fails the identifier
regression.

The probe polls expansion, layout, target visibility and reveal consumption
against one 10-second deadline. Each iteration advances AppKit layout and yields
the main actor so the scroll continuation can run. A compact assertion string
keeps all required flags, model/layout/control counts, finder, deadline and
elapsed time visible in pytest's concise output; the full result is also saved
inside that fixture's temporary directory. Windows remain offscreen.

### Regression evidence and macOS 15 expectation

Commit `134ec6609` adds the tests before application changes. Against
byte-for-byte `a66102fa9` application sources, both selected regressions failed
by assertion (2 failed, no setup/compile errors; 647.21 seconds): the pure
engine projection returned `[3, 3]` for a reveal of the sixth held row instead
of `[6, 0]`, and identifier discovery returned zero instead of three before
activation. The engine test also keeps unrelated-conversation, absent-row
and absent-reveal cases collapsed. The final probe checks layout and stable
identifiers rather than relying on its previous 1.5-second sleep.

macOS 15 execution is available only in CI. Beyond improved diagnostics,
expansion is an ordinary observed state projection checked without AppKit,
scrolling waits for a layout bearing the exact request token, polling yields
the main actor, and discovery reads an explicitly owned native identifier
rather than an AX tree or OS-dependent typography.
These remove the particular lifecycle and geometry dependencies seen on CI.
A draft app build and an initial precompile rejected modified inputs; their
outputs were discarded. The replacement build and precompile use frozen source
files. Implementation commit `5ae9f1350` follows tests-first commit `134ec6609`.

### Validation results and remaining coverage

The final application source builds and signs through app/build.sh; the
native-host revision also passed an independent type check. CPython 3.14.7 is
the GIL build (Py_GIL_DISABLED=0), with bounded CI Hypothesis settings and
storage in job scratch.

The corrected combined view hold passed **50 cases in 287.38 seconds**:
test_review_r219_fix6.py executed 34 cases (four engine cases plus three view
checks across ten independent probe processes), test_queue_tray_view.py passed
15, and the new pure visibility regression passed once. There are ten separate
fix6-view fixture directories. Every probe found three identifiers before and
six afterward, reported six model/layout rows, consumed the reveal, and made
queued5 visible. Completion ranged from **1.08 to 2.39 seconds** against the
10-second deadline. The 30 view assertions ran with the one-minute load average
between **49.6 and 59.1 on 18 CPUs**. The Python contract hold passed
test_state_contract.py **37** and test_test_module_names.py **1**.

The compact diagnostics exposed an additional fixture defect: it folded the
first receipt before creating its held timeline, permanently placing queued0
in the timeline. It therefore had five tray rows, despite claiming six. The
fixture now focuses the held conversation before folding receipts, retaining
the forward-only production placement rule. Before correction, one native pair
had 20 passes and two failures; a focused diagnostic had one pure-engine pass
and two view failures, reporting model/layout/control counts of five.

The queue core/notice/engine/search hold was interrupted to honor the 20-minute
cap, exiting with **37 passed in 1,184.64 seconds (19m44s)** and no test failures.
Those passes are from test_core_queue_tray.py; its remaining cases and the
notice/engine/search files did not run. The conversation view/timeline/review
hold exited after an interrupt with **40 passed in 1,208.54 seconds (20m08s)**:
conversation view 6, core timeline 23, and review probes 11. The first two files
completed; review probes has unfinished cases. The interrupt was requested
before 20 minutes, but exit exceeded the cap by 8.54 seconds. This hold did not
meet the 20-minute requirement; no failed test is hidden by that interruption.
Both groups were requested before the cutoff and neither was relaunched.

Final-source runs completed **165 passing test executions**, representing
**137 distinct test identities** after collapsing repeat indices and the pure
visibility regression run in two holds. They reported zero failures, errors,
skips or xfails. The two interrupted holds were not complete suite passes.
Whole-frontend coverage is incomplete: four frontend files completed,
two have partial coverage, and 43 were entirely unrun. Required outstanding
coverage includes the rest of core queue tray and review probes, plus the
notice, queue-engine and search files. Every file is listed below.
No additional unit or fake files were touched or rerun.

No new holds were requested after **20:10 UTC**, conservatively measured from
workspace creation. Every pytest invocation uses the exact shared lockf -k
command with no acquisition timeout; that lock serializes pytest processes.
Completed positive holds lasted 23.92 seconds and 287.38 seconds, excluding
lock waits. The interrupted holds lasted 1,184.64 and 1,208.54 seconds; the latter
overran the cap as recorded above. The first native pair took 858.89 seconds.
Paused runners were waited on, without relaunching them.

The table counts each test identity once using its latest completed result;
repeat indices are collapsed only for the three view checks. The core queue
file has partial coverage; its 37 passes include the selected visibility
regression already counted in the repeated view hold. Review probes is also
partial. Unfinished cases are unrun, including JUnit’s unnamed placeholders
for interrupted cases. A file marked Unrun has no completed result this round.
The fix-6 row counts seven distinct tests; its repeat hold executed 34 cases.

| File | Passed | Failed | Errors | Skipped | Xfailed | Status |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| tests/frontend/test_app_cutover_start.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_conversation_view.py | 6 | 0 | 0 | 0 | 0 | Completed |
| tests/frontend/test_core_app_cutover_reading.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_approval_notification.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_approval_reach.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_client.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_diff.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_draft.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_idle.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_live.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_live_failure.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_markdown.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_outbox.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_protocol.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_question_card.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_queue_engine.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_queue_tray.py | 37 | 0 | 0 | 0 | 0 | Partial; remaining cases unrun |
| tests/frontend/test_core_search.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_steer.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_steer_properties.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_store.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_suggestions.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_suggestions_r1b.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_text_scale.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_core_timeline.py | 23 | 0 | 0 | 0 | 0 | Completed |
| tests/frontend/test_core_titles.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_disk_ruling.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_menu_view.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_pr124_fixes_ui.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_pr124_recovery_properties.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_pr124_review_r2_fixes_ui.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_queue_tray_fix4_mutations.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_queue_tray_notice.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_queue_tray_view.py | 15 | 0 | 0 | 0 | 0 | Completed |
| tests/frontend/test_reading_view.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_review_pr127_timeline.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_review_r219_fix4.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_review_r219_fix6.py | 7 | 0 | 0 | 0 | 0 | Completed |
| tests/frontend/test_review_r219_probes.py | 11 | 0 | 0 | 0 | 0 | Partial; remaining cases unrun |
| tests/frontend/test_status_model.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_visual_progress.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_visual_report_hygiene.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_visual_review_fixes.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_visual_round_five.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_visual_round_four.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_visual_round_three.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_visual_round_two.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_visual_snapshots.py | Unrun | — | — | — | — | Unrun |
| tests/frontend/test_visual_suggestions.py | Unrun | — | — | — | — | Unrun |
| tests/fake/test_state_contract.py | 37 | 0 | 0 | 0 | 0 | Completed |
| tests/unit/test_test_module_names.py | 1 | 0 | 0 | 0 | 0 | Completed |

Total: **137 distinct passed** (165 executions including repeats and the selected regression rerun).

macOS 15 execution remains CI-only; no local macOS 15 result is claimed.

### Delivery and cleanup

Commits are on feat/queue-tray-217 in workspace-local .git-local, with the
requested co-author trailer. The supplied .git pointer and shared index remain
unchanged. The final report commit is included in a transfer bundle written
outside the worktree at /private/tmp/sf-r219f7-delivery-3vo7khe2/queue-tray-fix7.bundle,
with a66102fa9 as its prerequisite. No log, JSON evidence or bundle is staged.
Nothing is pushed. All foreground runners have exited. Job scratch and the
job's ignored build artifacts are removed before delivery; local commit
metadata and the delivery bundle are retained.


## Fix 8 (2026-10-22, review r4)

Merged the already available `a26d08336` (#237) first, without fetching. Its only
change is `tests/unit/test_daemon_connections.py`. Both parents' contract clause
ids and change-log entries were checked after the amendments; none is missing.
The duplicated CI-flakes entry now occurs once.

### Findings, causes and fixes

**P2-1 cause.** The tray revealer posted the timeline's row-scroll notice, and
that notice forced `atBottom = false`. A reveal leaving the end visible never
caused another marker appearance, so following remained disabled.

**P2-1 fix.** Only the timeline revealer (`layout == nil`) posts that notice.
Only the end marker's appearance/disappearance writes `atBottom`; a notice
suspends timeline catch-up without claiming the end disappeared. Catch-up still
follows when that marker remains visible. An actual timeline reveal continues
to end the opening pin under D3.

**P2-2 cause.** Opening considered only its latest 50 receipts known, fetched
older settled receipts again on every switch, and sorted the full receipt order
for every receipt on the main thread.

**P2-2 fix.** UIModel captures the app's known receipt identities and passes them
to the engine; hydration subtracts both those identities and the opening
snapshot. A batch sorts receipt order once, and unchanged sequences do not
sort. Placement sorts only waiting messages and skips unchanged writes. Empty
approval batches and empty card lists do no placement/card work; receipt state
is decoded once, and cold batches reserve their storage. Status support still
hydrates every unknown id in batches of at most 200 before publishing the tray.

**P3-1 cause.** Keying the revealer on the entire row-id set cancelled and
restarted its 250/550 ms pair whenever a new row arrived.

**P3-1 fix.** The key contains the request, conversation, and target readiness.
Readiness includes the target's layout presence for the tray. Unrelated rows
neither restart nor repeat the request. Repeat selections retain their request
token semantics.

**P3-2 cause.** An earlier merge retained two identical “CI flakes (2026-10-17,
D-2 and D-RL1)” change-log entries.

**P3-2 fix.** Kept one complete entry. C-27.5 and C-29.12 have the reveal/follow
amendment and its change-log entry; C-29.7 also records the hydration and batch
work budget. Every distinct entry from both merge parents remains.

### Four invariants and their coverage

1. **Follow:** after any reveal, the timeline follows new output if and only if
   the end marker is on screen. A tray-only reveal never changes it. The new
   view regressions cover tray search followed by two 40-line streams, a
   timeline search leaving the end visible, and tray search during ten catch-up
   pages. Fix-6's existing reading-position regressions also pass. In the final
   run, tray reveal/first stream/second stream stayed 0/0/0 pt from the end;
   timeline reveal/streams stayed 25/25/25 pt; opening at the tray target ended
   25 pt from the end. See the inherited offscreen observations below for the
   scope of this claim.
2. **Queue completeness:** after opening, every queued message held by the daemon
   is in the tray, for queues of any size. With status support, the bounded
   Hypothesis property checks open and reopen over sizes 0–601 (16 generated
   examples, up to five queues each), and the real-service 51/120/251-message
   regressions pass. The legacy daemon fallback remains capability-gated; its
   previously documented 50-receipt snapshot limitation is unchanged.
3. **Bounded open:** opening's main-thread work must not grow with settled
   history the app already knows. The budget checks cover N=500/2,000/5,000,
   require open/reopen application below 50 ms, and require no extra receipt
   hydration on reopen. The reverse-sequence 5,003-receipt batch checks ordering
   and the same budget. These are measured bounds over the stated sizes, not
   a claim that every retained-timeline operation is constant-time.
4. **One reveal per request:** a search reveal fires at most twice, its 250/550 ms
   pair, however many rows arrive. Both continuous-row reviewer cases pass;
   the bounded property runs 12 generated schedules plus one saved explicit
   schedule, with at most 18 arrivals through 1.8 s. Shrinking is disabled for
   this UI property to bound foreground work. The final 200 ms case fired at
   0.346/0.898 s, and the 400 ms case at 0.306/0.859 s.

The tests are `test_review_r219_fix8_view.py`,
`test_review_r219_fix8_engine.py`, and the retained fix-6 regressions. The new
Swift probes start from the reviewer's R4 engine/view probes and compile the
real application sources.

### Timings and mutation checks

Main-thread receipt application, milliseconds; engine RPC/hydration work is
outside these application measurements. Each case has three queued messages.

| Settled messages | Open apply ms | Reopen apply ms | Extra reopen receipt ids |
| ---: | ---: | ---: | ---: |
| 500 | 3.93 | 1.29 | 0 |
| 2,000 | 13.36 | 4.08 | 0 |
| 5,000 | 22.62 | 11.22 | 0 |

The reverse-sequence 5,003-receipt batch took **18.36 ms**.
On `ba049fcaf`, the same 5,000-message input took 23,242/24,031 ms for
open/reopen application and refetched 4,953 receipts on reopen; its reverse
batch took 30,580 ms. All 12 named reviewer-input regression assertions failed
on the preserved baseline, as expected.

| Restored mutation | Named test that rejects it |
| --- | --- |
| Notice sets `atBottom = false` | `test_timeline_reveal_leaving_end_visible_keeps_follow` |
| Hydrate the full history again | `test_reopen_fetches_no_known_receipt` |
| Sort per receipt | `test_receipt_batch_sorts_once_within_50ms` |
| Key on all row ids | `test_continuous_rows_reveal_at_most_twice` |

All four mutations were compiled and executed against their named tests. All
four older fix-4 mutations also pass their mutation controls; their deferred
placement mutation was updated to the new batching implementation.

Two intermediate engine holds exposed additional placement overhead (154 pass /
2 fail, then 155 pass / 1 fail); the final source passes the unchanged 50 ms
assertions. The first whole-view attempt completed 26 passes and one failure,
then was interrupted while Hypothesis shrank a schedule (199 attempted
schedules). The busy-case probe had sampled consumption after an awaited
simulated person scroll: the reveal fired at 1.33 s but was recorded as consumed
at 2.23 s. The probe now observes consumption directly from the publisher and
retains each schedule's result. The focused rerun passed all three selected
checks, and the complete final view hold passed all 28. That interrupted attempt
is not counted as a suite pass.

### Final coverage

Both `app/build.sh` runs passed. Final-source whole-frontend coverage is
**848 passed, 1 skipped, zero failures/errors**, across **all 52 files**.
The separately required unit/fake hold adds **38 passed**, for **886 distinct
passing tests and one skipped test** in the final coverage table. Repeated
validation and expected failing baseline/mutant assertions are not added to
those distinct counts.

Each row below is one pytest process through the hub's `suite_hold.sh`, using
the single reusable GIL CPython 3.14.7 venv. Every setup operation used
`uv_sync_hold.sh`; no setup ran inside a test hold. Hypothesis storage, caches
and each hold's TMPDIR stayed under the job scratch folder. No deep profile was
used. Durations below are JUnit pytest durations; all holds remained below
20 minutes, with the longest console duration 664.01 s (11:04).

| Whole-frontend hold | Files | Passed | Skipped | Pytest seconds |
| --- | ---: | ---: | ---: | ---: |
| `whole-01-view` | 3 | 28 | 0 | 112.70 |
| `whole-02-engine` | 5 | 156 | 0 | 153.81 |
| `whole-03-store` | 4 | 62 | 0 | 112.28 |
| `whole-04-mutations` | 2 | 8 | 0 | 258.87 |
| `whole-05-core` | 19 | 239 | 1 | 76.01 |
| `whole-06-views` | 10 | 139 | 0 | 266.26 |
| `whole-07-visual` | 9 | 216 | 0 | 663.13 |

The contract/module-name hold separately passed 38 tests in 8.25 s. No further
unit or fake files were rerun. The first three complete briefed holds also
passed 28/156/62 before the whole-directory pass.

All unlisted failure/error counts in this table are zero.

| Test file | Passed | Skipped |
| --- | ---: | ---: |
| `tests/fake/test_state_contract.py` | 37 | 0 |
| `tests/frontend/test_app_cutover_start.py` | 13 | 0 |
| `tests/frontend/test_conversation_view.py` | 6 | 0 |
| `tests/frontend/test_core_app_cutover_reading.py` | 15 | 0 |
| `tests/frontend/test_core_approval_notification.py` | 10 | 0 |
| `tests/frontend/test_core_approval_reach.py` | 41 | 0 |
| `tests/frontend/test_core_client.py` | 18 | 0 |
| `tests/frontend/test_core_diff.py` | 7 | 0 |
| `tests/frontend/test_core_draft.py` | 7 | 0 |
| `tests/frontend/test_core_idle.py` | 1 | 0 |
| `tests/frontend/test_core_live.py` | 0 | 1 |
| `tests/frontend/test_core_live_failure.py` | 1 | 0 |
| `tests/frontend/test_core_markdown.py` | 42 | 0 |
| `tests/frontend/test_core_outbox.py` | 13 | 0 |
| `tests/frontend/test_core_protocol.py` | 10 | 0 |
| `tests/frontend/test_core_question_card.py` | 10 | 0 |
| `tests/frontend/test_core_queue_engine.py` | 75 | 0 |
| `tests/frontend/test_core_queue_tray.py` | 51 | 0 |
| `tests/frontend/test_core_search.py` | 20 | 0 |
| `tests/frontend/test_core_steer.py` | 43 | 0 |
| `tests/frontend/test_core_steer_properties.py` | 2 | 0 |
| `tests/frontend/test_core_store.py` | 10 | 0 |
| `tests/frontend/test_core_suggestions.py` | 7 | 0 |
| `tests/frontend/test_core_suggestions_r1b.py` | 4 | 0 |
| `tests/frontend/test_core_text_scale.py` | 7 | 0 |
| `tests/frontend/test_core_timeline.py` | 23 | 0 |
| `tests/frontend/test_core_titles.py` | 1 | 0 |
| `tests/frontend/test_disk_ruling.py` | 8 | 0 |
| `tests/frontend/test_menu_view.py` | 8 | 0 |
| `tests/frontend/test_pr124_fixes_ui.py` | 17 | 0 |
| `tests/frontend/test_pr124_recovery_properties.py` | 1 | 0 |
| `tests/frontend/test_pr124_review_r2_fixes_ui.py` | 19 | 0 |
| `tests/frontend/test_queue_tray_fix4_mutations.py` | 4 | 0 |
| `tests/frontend/test_queue_tray_notice.py` | 2 | 0 |
| `tests/frontend/test_queue_tray_view.py` | 15 | 0 |
| `tests/frontend/test_reading_view.py` | 5 | 0 |
| `tests/frontend/test_review_pr127_timeline.py` | 4 | 0 |
| `tests/frontend/test_review_r219_fix4.py` | 39 | 0 |
| `tests/frontend/test_review_r219_fix6.py` | 7 | 0 |
| `tests/frontend/test_review_r219_fix8_engine.py` | 8 | 0 |
| `tests/frontend/test_review_r219_fix8_mutations.py` | 4 | 0 |
| `tests/frontend/test_review_r219_fix8_view.py` | 6 | 0 |
| `tests/frontend/test_review_r219_probes.py` | 23 | 0 |
| `tests/frontend/test_status_model.py` | 25 | 0 |
| `tests/frontend/test_visual_progress.py` | 5 | 0 |
| `tests/frontend/test_visual_report_hygiene.py` | 15 | 0 |
| `tests/frontend/test_visual_review_fixes.py` | 43 | 0 |
| `tests/frontend/test_visual_round_five.py` | 9 | 0 |
| `tests/frontend/test_visual_round_four.py` | 27 | 0 |
| `tests/frontend/test_visual_round_three.py` | 28 | 0 |
| `tests/frontend/test_visual_round_two.py` | 85 | 0 |
| `tests/frontend/test_visual_snapshots.py` | 3 | 0 |
| `tests/frontend/test_visual_suggestions.py` | 1 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 |

### Limits, delivery and cleanup

The sole skip is
`test_core_live.py::test_the_app_core_drives_a_development_daemon`: the sandbox
cannot expose the macOS boot identity needed by its process checks. That case
was collected but not executed. macOS 15 and Python 3.12 were not run locally;
no installed app, live store or live daemon was exercised.

The reviewer's already documented offscreen approval/return-to-end observations
remain recorded: after an approval at the end, subsequent output can lose follow,
and the offscreen return-to-end cycle can do the same. The final diagnostic probe
still measured 1,024 pt and 567 pt from the end in those respective inherited
cases. They fail at the earlier baseline too, and were not attributed to the
four review findings. This report does not claim those broader inherited
visibility cases are fixed, or a universal proof of the follow invariant beyond
the listed acceptance regressions.

The required process-list check (`pgrep -fl 'suite_hold.sh|pytest|lockf'`) was
attempted after all foreground runners exited. The sandbox denied process
listing (`sysmond service not found`), so external process-list verification
was unavailable. No foreign process was signalled. All managed foreground
commands have returned; job scratch, build artifacts and generated Python
caches are removed at delivery. No logs, JSON evidence or bundles are staged.

The shared Git index/ref paths are outside this sandbox's writable roots.
Commits therefore live on `feat/queue-tray-217` in workspace-local
`.git-workspace`; the supplied `.git` pointer and caller's checkout remain
unchanged. The source/test commits, oldest first, are:

- `c01b921a2` Merge release/217 at a26d08336 before queue tray fix 8
- `d4c1d6b5a` test: reproduce r219 follow, hydration cost, and reveal restarts
- `6fb9ebffe` test: retain follow when a timeline reveal leaves its end visible
- `a4c821f46` fix: scope search reveals to their scroll container and target
- `d36e3dce5` fix: reuse known receipts on open and sort each receipt batch once
- `e2ce9e42d` test: require four fix-8 mutations to fail their named regressions
- `06bab9c31` fix: resolve queue placement without sorting settled display history
- `e7d92f8df` perf: skip empty approval placement and decode receipt state once
- `960cdbcbc` perf: reserve cold receipt batches and skip empty card scans
- `5f593dcae` test: retain the deferred-placement mutation after batching refactor
- `ae2ccf999` test: observe reveal consumption directly and bound UI property runs

The report is committed separately with the requested co-author trailer.
Nothing was fetched or pushed. All holds were requested before the 4 h 30 m
cutoff; the job began about 08:14 UTC and whole-frontend validation finished
about 10:51 UTC.

## Fix 9 (2026-10-23, review r5)

**Status: implementation committed; validation incomplete at the prescribed cutoff.**
There are **204 distinct passing tests**, zero final-source assertion failures,
errors or skips in the executed cases. The two final-source pytest invocations
reported 156 and 56 passes; eight engine checks overlap, so their 212 passing
executions are not 212 distinct tests. Only one of the three required stability
holds completed. Four requested mutation controls and the whole-frontend phase
remain unrun. The last slot also exceeded the 20-minute rule by about 12 seconds,
as explained below. This section does not claim completion of the requested
validation.

### Release merge and commits

The first commit merges the explicitly briefed release commit `4f5983f4c`
(#232, #238, #223) into `a01711839139`. No fetch occurred. The already-local
`origin/release/217` was actually newer, `e345f0803` (#243); that later update
was outside the pinned merge target and is not included.

`docs/acceptance-contract.md` had two conflict blocks:

1. At the beginning of Changes, retain the queue-tray fix-8/fix-7/fix-6/fix-4
   entries and all incoming #223, #232 and #212 entries, concatenating both sides.
2. Beside the original queue-tray entry, retain it alongside incoming capture
   amendments. The incoming CI-flakes entry was identical to the copy already
   retained above; the immediate follow-up commit consolidates it to one copy.

Static verification against both merge parents found no missing numbered
clauses and no missing unique line from either conflict block. The CI-flakes
entry appears exactly once. Release's other automatic clause amendments and
the queue-tray clauses remain present; no side's change-log content was dropped.

Commits, oldest first (the report is committed separately):

- `f7cd31322` Merge release/217 at 4f5983f4c before queue tray fix 9
- `c2abab4b5` docs: retain one copy of the shared CI flakes merge entry
- `3a066d468` test: assert queue tray work counts and reviewer r5 positions
- `06378ba08` test: expose sort and reveal-start counters only in probe builds
- `e3226b549` test: establish the live-row catch-up precondition without a timing race
- `127743845` test: wait for reveal completion and check mutant preconditions

The shared Git metadata rejected `ORIG_HEAD` writes. All commits therefore live
on `feat/queue-tray-217` in workspace-local `.git-local`; use
`GIT_DIR="$PWD/.git-local" git log` from this workspace to inspect them. The
supplied `.git` pointer and caller's checkout remain unchanged. Every commit
uses the requested co-author trailer. No push occurred.

### Findings: causes and changes

**P2-1 — debug-build wall-clock assertions measured host scheduling as well as
work.** Replace the 50 ms checks with receipt completeness, zero known-receipt
fetches on reopen, and a counter in the actual sort helper: one sort for a
changed batch and zero for an unchanged batch. Replace reveal-consumption time
bounds with exactly two scroll notices, one eligible reveal start, consumption,
and a continuously present target. A counter records starts cancelled before
any scroll, so postponing one pair through repeated restarts cannot pass merely
by producing two late scrolls. The counters/notification compile only into probe
builds. Shipping product behavior and snapshots are unchanged.

Catch-up waits for its cursor and caught-up state, with a 60-second hang guard.
Mount reveals also wait for consumption. Milliseconds and reveal timestamps
remain recorded output, without performance assertions. Both fix-8 files pass
in the completed paired run, including reveals consumed at 2.751 and 1.647 s.

**P3-1 — the engine probe excludes `UIModel.swift`.** Extend the real view probe
with the reviewer's `UIModel.open`-twice case: 64 receipts, a 50-receipt opening
snapshot, and three tray rows. Count the actual identities requested through
`message.status`: first open asks for 14, reopening asks for zero, all 64 remain
known, and all three tray rows remain visible. The new named regression passed.
The compiled mutation `let known: Set<String> = []` was rejected by
`test_real_uimodel_reopen_fetches_no_known_receipt`; its mutation control passed.

**P3-2 — prior tests did not exercise an end-visible timeline reveal while
catch-up is suspended.** Reuse the reviewer's 120-turn live-row cases with page
settings 250 ms/40 events and 100 ms/20 events (the review's ten/eighteen-page
schedules). An initial ungated transplant failed one head case: 10,288 pt from
the end. It had not established that the reveal left the end visible before
history growth. The final fixture holds the first catch-up read pending until
the opening end is visible and the actual search reveal is consumed, asserts
that setup, then releases the slow pages. Both before/after-reveal distances
were 0 pt; both completed openings and subsequent streams ended 25 pt from the
end, within the unchanged 30 pt padding tolerance. Both named position cases
passed. **The settling-clause mutation was not executed in this job**, so the
new gated fixture's negative-control coverage remains unverified.

### Wall-clock audit

Audited all **33 frontend files changed or added by the PR relative to the
pinned release**, including Python assertions and Swift probe guards. Replaced:

- `test_open_and_reopen_stay_under_50ms`: first/reopen apply bounds become
  complete known-message counts and zero reopen receipt requests; renamed to
  `test_open_and_reopen_apply_complete_receipts`.
- `test_receipt_batch_sorts_once_within_50ms`: order plus sort counts 1/0;
  renamed to `test_receipt_batch_sorts_once`.
- `test_continuous_rows_reveal_at_most_twice`: remove `consumed_at <= 1.5` and
  the position check tied to a person scrolling at one elapsed second. Assert
  two reveals, one start, consumption and target presence. The diagnostic person
  scroll now waits for consumption.
- `test_one_reveal_pair_for_bounded_row_schedules`: remove the additional
  `consumed_at <= 1.5` assertion; assert consumption and exactly one reveal pair
  with one start. Its two-second completion wait becomes a 60-second hang guard.
- `ReviewR219Fix8ViewProbe.waitFolded`: replace the fixed eight-second catch-up
  allowance with a state/cursor wait bounded by 60 seconds. Reveal waits formerly
  bounded by four/six seconds also use the 60-second completion guard.
- The audit's additional completion check was fix 6's `not search["timed_out"]`.
  Keep its laid-out-row/target-visible/consumed assertions, but extend its
  ten-second probe deadline to 60 seconds. This changed probe was **not rerun**.

The only remaining measured-time assertion candidate is that fix-6 completion
flag, now governed by the generous hang deadline. No measured millisecond or
reveal-consumption performance bound remains. Stimulus intervals, layout-pump
waits and subprocess/compiler hang guards are not asserted performance targets.

Recorded debug-build application times (not pass/fail criteria):

| Run | Settled messages | Open apply ms | Reopen apply ms | Reopen receipt ids |
| --- | ---: | ---: | ---: | ---: |
| Brief engine | 500 | 27.15 | 0.81 | 0 |
| Brief engine | 2,000 | 233.54 | 60.74 | 0 |
| Brief engine | 5,000 | 868.24 | 224.70 | 0 |
| Stability 1 | 500 | 20.93 | 10.63 | 0 |
| Stability 1 | 2,000 | 210.34 | 99.40 | 0 |
| Stability 1 | 5,000 | 891.61 | 178.85 | 0 |

Brief engine: batch 282.06 ms, 1 sort, 0 sorts on unchanged reapplication. Stability 1: batch 607.32 ms, 1 sort, 0 sorts on unchanged reapplication.

### Four unchanged invariants

1. **Follow:** after a reveal, the timeline follows new output iff its end marker
   is on screen; a tray-only reveal leaves timeline follow alone. Position tests
   cover tray/timeline reveals, streams and catch-up. The older offscreen cases
   below remain outside this round; fix 6's additional position checks are unrun.
2. **Queue completeness:** status-supported opening includes every queued
   identity, including queues larger than the opening snapshot. The bounded
   0–601-size open/reopen property and core queue-tray/service checks passed.
3. **Bounded reopening work:** reuse known settled receipts, fetch none on
   reopen, and sort at most once per changed receipt batch (zero for unchanged
   sequences). Counts passed at 500/2,000/5,000 settled messages. These are work
   counts over the stated inputs, not a universal constant-time claim for every
   retained-timeline operation.
4. **One reveal per request:** a present target gets its two-scroll pair once;
   unrelated row arrivals neither postpone it by restarting nor repeat it.
   Both busy cases and the 12 generated schedules plus saved explicit example
   passed with two reveals and one start.

These assertions use counts or positions, never wall-clock performance bounds.
The coverage table states what was actually rerun; it is not a universal proof
of all four invariants.

### Holds, stability and mutations

Setup ran once through `uv_sync_hold.sh`, using the same GIL CPython 3.14.7 venv
for every hold. Each pytest used `suite_hold.sh`, one process and no workers,
with job-local TMPDIR, caches and Hypothesis storage. Completed final-source
holds explicitly selected the bounded `ci` profile; no deep profile ran.

| Hold | Pytest outcome | Pytest seconds | Total seconds including queue | Status |
| --- | --- | ---: | ---: | --- |
| Initial view group | Counts unavailable | about 19 min | not retained | Interrupted before 20 min; no count credit |
| Ungated new view cases | 2 passed, 1 failed | 73.18 | 289.06 | Before fixture correction; excluded from final-source totals |
| Barrier-only request | No pytest started | — | 3,090.21 | Queued request cancelled to finish fixture/grouping changes |
| Engine plus new view request | 156 passed | 1,155.11 | 3,634.08 | All five engine files complete; interrupted before new-view checks completed |
| Stability 1, contract, new view, UIModel mutant | 56 passed | 1,196.56 | 5,335.34 | Complete, no failures/errors/skips |

**Operational overrun:** the last pytest duration was 19:56, but the slot header
was 06:14:21 UTC and pytest started about 15 seconds later. Its slot therefore
lasted about **20:12**, exceeding the requested cap by roughly 12 seconds. I
missed the planned interrupt while awaiting the final control. The console
pytest duration alone does not establish compliance with the slot cap.

Work began at about 01:59:44 UTC. No new hold was requested after the prescribed
06:29:44 UTC cutoff. The already-running final hold finished at about 06:34 UTC.
The long shared-lock waits consumed the remaining validation window.

| Required stability run (both fix-8 files together) | Outcome |
| --- | --- |
| 1 | 14 passed (8 engine, 6 view), no failures/errors/skips; in the 56-pass hold |
| 2 | Unrun at cutoff |
| 3 | Unrun at cutoff |

The earlier engine-only execution is not counted as another paired stability
run. The three-run requirement is incomplete.

| Mutation | Named regression | Executed result |
| --- | --- | --- |
| UIModel drops known ids | `test_real_uimodel_reopen_fetches_no_known_receipt` | Killed: compiled mutant triggered the expected assertion |
| `suspended ? .stay : .jump` | `test_live_row_reveal_during_slow_catchup_keeps_the_end`, both schedules | Unrun; gated-fixture negative control remains required |
| Full-history hydration | `test_reopen_fetches_no_known_receipt[500]` | Unrun |
| Per-receipt sort | `test_receipt_batch_sorts_once` | Unrun |
| Revealer keyed on all rows | `test_continuous_rows_reveal_at_most_twice[busy_400ms]` | Unrun |
| Existing notice sets at-bottom false control | `test_timeline_reveal_leaving_end_visible_keeps_follow` | Unrun |

The settling mutation control asserts its initial end-visible setup separately
before expecting final-position failures. The sort mutant uses a small batch;
its rejection depends on counts, not on making a quadratic workload slow.

### Per-file coverage and outstanding suites

All failure/error/skip counts below are zero **for the executed final-source
cases**. Historical pre-correction failures appear in the hold table above.
Unrun-case counts use the prior fix-8 manifest plus the new cases; unrun files
were not recollected here. The manifest matches all 53 current frontend files.
There are **166 distinct frontend passes plus 38 unit/fake passes**, and
**688 manifest cases remain unrun**, including five mutation parameters in a
partially executed file. The live-core case is unrun, not claimed skipped.

The required fix-6/tray-view and conversation-view/timeline/store/reviewer-probe
groups remain incomplete. The required **whole-frontend phase after the briefed
groups did not start**; targeted passes are not relabelled as a whole-suite pass.
No unrequested unit/fake suites were run.

| Test file | Passed | Failed/errors | Skipped | Unrun (manifest) |
| --- | ---: | ---: | ---: | ---: |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 |
| `tests/frontend/test_app_cutover_start.py` | 0 | 0 | 0 | 13 |
| `tests/frontend/test_conversation_view.py` | 0 | 0 | 0 | 6 |
| `tests/frontend/test_core_app_cutover_reading.py` | 0 | 0 | 0 | 15 |
| `tests/frontend/test_core_approval_notification.py` | 0 | 0 | 0 | 10 |
| `tests/frontend/test_core_approval_reach.py` | 0 | 0 | 0 | 41 |
| `tests/frontend/test_core_client.py` | 0 | 0 | 0 | 18 |
| `tests/frontend/test_core_diff.py` | 0 | 0 | 0 | 7 |
| `tests/frontend/test_core_draft.py` | 0 | 0 | 0 | 7 |
| `tests/frontend/test_core_idle.py` | 0 | 0 | 0 | 1 |
| `tests/frontend/test_core_live.py` | 0 | 0 | 0 | 1 |
| `tests/frontend/test_core_live_failure.py` | 0 | 0 | 0 | 1 |
| `tests/frontend/test_core_markdown.py` | 0 | 0 | 0 | 42 |
| `tests/frontend/test_core_outbox.py` | 0 | 0 | 0 | 13 |
| `tests/frontend/test_core_protocol.py` | 0 | 0 | 0 | 10 |
| `tests/frontend/test_core_question_card.py` | 0 | 0 | 0 | 10 |
| `tests/frontend/test_core_queue_engine.py` | 75 | 0 | 0 | 0 |
| `tests/frontend/test_core_queue_tray.py` | 51 | 0 | 0 | 0 |
| `tests/frontend/test_core_search.py` | 20 | 0 | 0 | 0 |
| `tests/frontend/test_core_steer.py` | 0 | 0 | 0 | 43 |
| `tests/frontend/test_core_steer_properties.py` | 0 | 0 | 0 | 2 |
| `tests/frontend/test_core_store.py` | 0 | 0 | 0 | 10 |
| `tests/frontend/test_core_suggestions.py` | 0 | 0 | 0 | 7 |
| `tests/frontend/test_core_suggestions_r1b.py` | 0 | 0 | 0 | 4 |
| `tests/frontend/test_core_text_scale.py` | 0 | 0 | 0 | 7 |
| `tests/frontend/test_core_timeline.py` | 0 | 0 | 0 | 23 |
| `tests/frontend/test_core_titles.py` | 0 | 0 | 0 | 1 |
| `tests/frontend/test_disk_ruling.py` | 0 | 0 | 0 | 8 |
| `tests/frontend/test_menu_view.py` | 0 | 0 | 0 | 8 |
| `tests/frontend/test_pr124_fixes_ui.py` | 0 | 0 | 0 | 17 |
| `tests/frontend/test_pr124_recovery_properties.py` | 0 | 0 | 0 | 1 |
| `tests/frontend/test_pr124_review_r2_fixes_ui.py` | 0 | 0 | 0 | 19 |
| `tests/frontend/test_queue_tray_fix4_mutations.py` | 0 | 0 | 0 | 4 |
| `tests/frontend/test_queue_tray_notice.py` | 2 | 0 | 0 | 0 |
| `tests/frontend/test_queue_tray_view.py` | 0 | 0 | 0 | 15 |
| `tests/frontend/test_reading_view.py` | 0 | 0 | 0 | 5 |
| `tests/frontend/test_review_pr127_timeline.py` | 0 | 0 | 0 | 4 |
| `tests/frontend/test_review_r219_fix4.py` | 0 | 0 | 0 | 39 |
| `tests/frontend/test_review_r219_fix6.py` | 0 | 0 | 0 | 7 |
| `tests/frontend/test_review_r219_fix8_engine.py` | 8 | 0 | 0 | 0 |
| `tests/frontend/test_review_r219_fix8_mutations.py` | 1 | 0 | 0 | 5 |
| `tests/frontend/test_review_r219_fix8_view.py` | 6 | 0 | 0 | 0 |
| `tests/frontend/test_review_r219_fix9_view.py` | 3 | 0 | 0 | 0 |
| `tests/frontend/test_review_r219_probes.py` | 0 | 0 | 0 | 23 |
| `tests/frontend/test_status_model.py` | 0 | 0 | 0 | 25 |
| `tests/frontend/test_visual_progress.py` | 0 | 0 | 0 | 5 |
| `tests/frontend/test_visual_report_hygiene.py` | 0 | 0 | 0 | 15 |
| `tests/frontend/test_visual_review_fixes.py` | 0 | 0 | 0 | 43 |
| `tests/frontend/test_visual_round_five.py` | 0 | 0 | 0 | 9 |
| `tests/frontend/test_visual_round_four.py` | 0 | 0 | 0 | 27 |
| `tests/frontend/test_visual_round_three.py` | 0 | 0 | 0 | 28 |
| `tests/frontend/test_visual_round_two.py` | 0 | 0 | 0 | 85 |
| `tests/frontend/test_visual_snapshots.py` | 0 | 0 | 0 | 3 |
| `tests/frontend/test_visual_suggestions.py` | 0 | 0 | 0 | 1 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 | 0 |

### Open items and cleanup

The older behaviors explicitly excluded from this round remain open: an
approval card's reply can lose follow depending on timing, and following does
not reliably resume after the person scrolls back to the end. Neither behavior
was changed. Pending validation also includes the four requested unrun mutants,
two stability holds, the remaining briefed files and the whole frontend suite.
macOS 15, Python 3.12, the installed app and live daemon were not exercised.
No snapshot changed.

After every managed runner returned, the requested
`pgrep -fl 'suite_hold.sh|pytest|lockf'` check was attempted. The sandbox denied
process listing (`sysmond service not found`), so external process-list
verification was unavailable. All owned managed foreground runners returned;
no foreign process was signalled. At delivery the job scratch, build directory
and generated Python/Hypothesis caches are removed. Workspace-local Git metadata
is retained for the commits. No logs, JSON evidence or bundles are staged, and
nothing was fetched or pushed.


## Fix 10 (2026-10-23, review r6)

**Implementation and requested settling proof complete; whole-frontend validation incomplete at the prescribed cutoff.**
There are **514 distinct frontend passes, 1 skip and 347 unrun cases** across the
54-file manifest. The fake contract file adds 37 passes and the module-name file
adds 1: **552 distinct requested repository cases passed**. No completed
final-source assertion failed. Interrupted invocations are recorded separately;
raw reviewer-probe failures below are diagnostic results, not canonical-suite passes.
No new hold was requested after 14:26 UTC (4 h 30 m from this job's approximately
09:56 UTC start). The final hold was requested at approximately 14:16 UTC and
stopped before 20 minutes. No push occurred.

### Merge and commit storage

The first commit, `fa9cd6ffe`, merges the explicitly pinned `c2e080e44` into
`3f95e80b3c13`. No fetch occurred. The only conflict was
`docs/acceptance-contract.md`. Ran the required command:

```
python3 /Users/maxghenis/reviews/subfleet-hub/host-runs/merge_contract.py HEAD c2e080e44
```

It exited 0, merging against `7e2dc5a35`, and reported **254 clauses, none
duplicated**. The exact requested clause-ID grep/sort/uniq command printed
nothing, both after resolution and at delivery. The merge commit's body
incorrectly says 333 clauses; the verified count is 254. History is preserved.
`subfleet/conversations/service.py` merged automatically; no other file conflicted
and no additional hand resolution was needed.

Shared Git metadata is read-only. Commits are on `feat/queue-tray-217` in this
workspace's `.git-local`; the supplied `.git` pointer and caller's checkout are
unchanged. Inspect with `GIT_DIR="$PWD/.git-local" GIT_WORK_TREE="$PWD" git log`.
Every commit has the requested co-author trailer. Source/test commits, oldest first:

- `fa9cd6ffe` merge release at `c2e080e44`.
- `a94d2f098` tests first: require controlled settling inputs and separate schedules.
- `c81c9c89d` implement the controlled settling page and retain mutation evidence in scratch.
- `bd52d97c5` tests first: port the seven distant-row cases.
- `19a6649d9` mount probe windows offscreen, control transactions and wait for state.

This report is committed separately. No shipping app source changed in this round.

### P2: deterministic settling mutation control

The old fixture inferred branch coverage from page spacing. It could complete a
100 ms mutant opening at the end without ever exercising a changed settling key
with both `suspended` and `atBottom` true. Sequential `pytest.raises` checks also
let a failure on one schedule prevent evaluation of the other.

The named regression now retains the real `UIModel` slow-opening check and adds
a focused real `ScrollViewReader`/`SearchRevealer` fixture. The probe completes the
two-scroll reveal, explicitly positions at and measures the end, retains the
observed reveal suspension, then releases one 4,000-point page that changes the
actual `FollowKey`. Its `.onChange` calls the real `FollowKey.move`. It reports
changed-key, settling, suspension and end-visible inputs. These are asserted as
setup outside mutation rejection. No timing is used to decide whether the
required predicate inputs have been established. The 100/250 ms delays remain
stimuli. The two schedules are separate pytest items.

The focused control is distinct from the retained UIModel integration check.
One original 100 ms mutant still finished 24 points from the end; it is rejected
by the focused changed-key transition. The test no longer infers predicate
coverage from that original opening's final position.

Five repetitions each launched **fresh head and mutant native processes**. They
reuse the exact final-source binaries already compiled in the successful view
hold; the mutant's saved `QueueTray.swift` was checked against the exact expected
single-clause replacement before reuse. Each repetition invokes the original
head regression and the canonical mutation regression separately for each
schedule. This is five new native measurements, not five assertions on one
cached result. All prerequisites passed, each named mutant regression raised,
and every result was identical:

| Fresh run | Head 250 ms (pt) | Mutant 250 ms (pt) | Head 100 ms (pt) | Mutant 100 ms (pt) |
| --- | ---: | ---: | ---: | ---: |
| 1 | 0 | 4000 | 0 | 4000 |
| 2 | 0 | 4000 | 0 | 4000 |
| 3 | 0 | 4000 | 0 | 4000 |
| 4 | 0 | 4000 | 0 | 4000 |
| 5 | 0 | 4000 | 0 | 4000 |

The end tolerance remains 30 points. The controlled mutant is 4,000 points away
on both schedules. The five-run driver produced **10 passing pytest items**.
The canonical frontend mutation file has 6 distinct completed passes and 1
uncompleted case: `key_on_all_rows`. The UIModel known-receipt, notice/atBottom,
full-history hydration and per-receipt-sort mutants were each rejected by their
named regressions. The row-set mutation was attempted twice but interrupted
before completion; it is unrun coverage, not a confirmed rejection.

### Distant-row question: classification (c), probe environment sensitivity

`brief-review-r5.md` explicitly identifies the approved head as `a01711839`.
Thus two requested revision labels name the same commit; they were run
independently. Comparing its app sources with the merged head found only the
previous fix-9 probe counters/notifications, guarded by test compilation flags.
Shipping app code did not regress between those revisions.

Copied the supplied reviewer files into job scratch. Ran the original first
probe's 14 cases plus the second probe's three normal controls and two distant
catch-up assertions, using its `controls,openlive,trayfar,open` groups. Mutant-only
checks were not part of this product-revision comparison. Every invocation used
the required wrapper, CPython 3.14.7 and the same Swift flags.

Release predates `Notification.Name.conversationRowScroll` and
`ConversationStoreState.knownMessageIDs`. The untouched probes first produced
19 compilation setup errors there. To measure the baseline, added scratch-only
observer aliases: the notification's existing string name, and a known-ID getter
returning `Set(timelines[cid]?.displayOrder ?? [])`. They do not post notifications
or alter scrolling. Baseline notice counts are zero; its opening controls also
fail, so large baseline catch-up distances do not demonstrate a successful reveal.

Raw probe results, before any rendering control (P/F counts cover 19 assertions):

| Revision | Passed / failed | Five distant cases passed | Quiet distance (pt) | Round 1 catch-up 250 / 100 ms (pt) | Round 2 catch-up 250 / 100 ms (pt) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Release without PR: `c2e080e44` | 6 / 13 | 0 / 5 | 25 | 22831 / 20645 | 22831 / 20470 |
| R5-approved head: `a01711839` (A) | 10 / 9 | 0 / 5 | 0 | 25 / 25 | 25 / 25 |
| `a01711839` independent repeat (B) | 9 / 10 | 0 / 5 | 0 | 25 / 76 | 25 / 75 |
| Merged head: `fa9cd6ffe` app sources | 9 / 10 | 0 / 5 | 0 | 25 / 25 | 25 / 25 |

All seven distant behaviors fail in the original first probe at both approved
head runs and merged head. The extra failure in repeat B/merged head is an
opening-control outlier. The two round-2 distant behaviors also fail at each.

R5's saved `out/r5-round1/view_head.json` differs concretely: quiet distance
**5,249.5 pt**, catch-up **15,178.5 / 17,402 pt**, with unchanged offsets after
further output. Its review was based on actual positive evidence. Those results
can be reproduced on the same app source once the probe's rendering lifecycle
is controlled; this is not evidence that r5 invented its result or that this
PR's product source later changed.

The original probes create unpresented windows and rely on animated
`ScrollViewProxy` calls and fixed post-layout sampling. A host transaction
modifier alone still failed all seven head checks. Controlling the animation
call site improved five but left two failures. Mounting the windows beyond all
screen bounds and driving AppKit updates restored all seven distant behaviors
at both revisions. Remaining Python failures sampled no-reveal opening controls
at 298/72 pt; a second later they were 24/0 pt. Waiting for the actual end state,
with a 60-second hang guard, also closes those control failures.

The final fixture therefore finishes AppKit launch, mounts each window outside
all screens without taking keyboard focus, drives `updateWindows`/display, uses
a probe-module animation overload with synchronous transactions, and waits for
cursor/caught-up/reveal/end state. Windows are nonrestorable and closed after
each case. Seven permanent regressions cover quiet output, text at 50/120 ms,
rows at 150/400 ms, and both distant catch-up schedules. Their arrivals are finite
counts; they assert positions, consumption and reveal/start counts, never a
performance time bound. Mount counts and animation-call counts make ineffective
controls fail setup. No shipping animation implementation was changed.

After these test-only controls, **all 14 original reviewer assertions passed**
(head and approved source), including the no-reveal controls:

| Controlled revision | Five distant cases passed | Their final distance range (pt) | Catch-up 250 ms (pt) | Catch-up 100 ms (pt) | Offset shift after more output (pt) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `a01711839` | 5 / 5 | 4751–5434 | 17447 | 17985 | 0 |
| `head` | 5 / 5 | 4710–5320 | 17447 | 19018 | 0 |

This establishes (c). The historical host event that changed the old unpresented
window's behavior was not recorded, so no OS-version or HOME-change cause is
claimed. The reproducible dependency is the probe's window/transaction/layout
preparation. Visible production-window animation was not exercised by this
controlled geometry probe.

### Stability, holds and protocol exception

The required three files contain 17 checks (fix-8 view 6, fix-8 engine 8, fix-9
view 3). They completed with **17 passed, 0 failed in each of three separate
holds**: `stability-1`, `stability-2`, and `stability-3-core`. The first two also
completed all 7 new distant-row checks. Their additional mutation/core work was
interrupted separately; the completed required cases did not fail.

| Hold | Completed passes | Skips | Recorded pytest duration | Outcome |
| --- | ---: | ---: | ---: | --- |
| `brief-view-and-deterministic-probes` | 54 | 0 | 18m 32.12s | completed |
| `brief-engine` | 156 | 0 | 13m 35.62s | completed |
| `brief-conversation` | 62 | 0 | 4m 21.19s | completed |
| `brief-contract` | 38 | 0 | 0m 41.91s | completed |
| `stability-1` | 28 | 0 | 21m 42.47s | interrupted after completed cases |
| `stability-2` | 24 | 0 | 17m 53.87s | interrupted after completed cases |
| `stability-3` | 10 | 0 | 15m 38.68s | interrupted after completed cases |
| `stability-3-core` | 108 | 0 | 17m 30.08s | interrupted after completed cases |
| `last-frontend` | 161 | 1 | 17m 58.81s | interrupted after completed cases |

**Protocol exception:** I let `stability-1` run **21m 42.47s**, exceeding the
20-minute maximum, before interrupting it. That was my error in grouping and
monitoring the work. Later holds were stopped below 20 minutes. No completed
assertion failed in interrupted holds; their unfinished cases are not counted
as passes. The first combined approved-head comparison was also split before
20 minutes. No test process was run concurrently with another of mine.

The build passed through the wrapper after the pinned merge. Environment setup
used the prescribed setup wrapper once, with GIL CPython 3.14.7. Tests reused that
venv and a scratch uv cache; no deep Hypothesis profile was run. Remaining runs
explicitly selected the ci profile. The previous work-count audit remains in
force: new assertions are counts or positions; timestamps and milliseconds are
diagnostic, and deadlines are generous completion guards.

### Four retained invariants and open items

1. Follow after a reveal depends on the end being visible; a tray reveal leaves
   timeline follow alone. The explicit settling transition and distant-position
   cases now have deterministic probe controls.
2. Opening includes all queued identities, including queues larger than the
   opening snapshot. The briefed queue/status properties passed.
3. Reopening fetches zero known receipts, and a changed receipt batch sorts once
   (zero for an unchanged batch). These are work counts, not a universal wall-time
   claim; engine histories 500/2,000/5,000 passed.
4. A continuously present target gets one reveal start and its two-scroll pair,
   even while rows arrive. The positive checks passed; the row-set mutation's
   completed negative control remains outstanding.

Open items: complete the 347 unrun frontend cases below, especially the named
row-set mutation; the older approval-card reply/follow timing and return-to-end
follow observations remain outside this round as directed by brief fix 9.
No claim is made that those older observations are fixed.

### Per-file final-source coverage

Unique completed cases, combining briefed and subsequent holds; repetitions are
not added to these counts. Manifest counts are review r6's unchanged file counts,
with the independent second settling case and seven new distant-row cases added.
Blank/incomplete JUnit entries created by interruption are excluded. All completed
repository assertions passed. The skip is the development-daemon case whose
process/boot-identity checks are unavailable in this sandbox.

| Frontend file | Passed | Skipped | Unrun |
| --- | ---: | ---: | ---: |
| `test_app_cutover_start.py` | 13 | 0 | 0 |
| `test_conversation_view.py` | 6 | 0 | 0 |
| `test_core_app_cutover_reading.py` | 15 | 0 | 0 |
| `test_core_approval_notification.py` | 10 | 0 | 0 |
| `test_core_approval_reach.py` | 41 | 0 | 0 |
| `test_core_client.py` | 18 | 0 | 0 |
| `test_core_diff.py` | 7 | 0 | 0 |
| `test_core_draft.py` | 7 | 0 | 0 |
| `test_core_idle.py` | 1 | 0 | 0 |
| `test_core_live.py` | 0 | 1 | 0 |
| `test_core_live_failure.py` | 1 | 0 | 0 |
| `test_core_markdown.py` | 42 | 0 | 0 |
| `test_core_outbox.py` | 13 | 0 | 0 |
| `test_core_protocol.py` | 10 | 0 | 0 |
| `test_core_question_card.py` | 10 | 0 | 0 |
| `test_core_queue_engine.py` | 75 | 0 | 0 |
| `test_core_queue_tray.py` | 51 | 0 | 0 |
| `test_core_search.py` | 20 | 0 | 0 |
| `test_core_steer.py` | 43 | 0 | 0 |
| `test_core_steer_properties.py` | 2 | 0 | 0 |
| `test_core_store.py` | 10 | 0 | 0 |
| `test_core_suggestions.py` | 7 | 0 | 0 |
| `test_core_suggestions_r1b.py` | 4 | 0 | 0 |
| `test_core_text_scale.py` | 7 | 0 | 0 |
| `test_core_timeline.py` | 23 | 0 | 0 |
| `test_core_titles.py` | 1 | 0 | 0 |
| `test_disk_ruling.py` | 0 | 0 | 8 |
| `test_menu_view.py` | 0 | 0 | 8 |
| `test_pr124_fixes_ui.py` | 0 | 0 | 17 |
| `test_pr124_recovery_properties.py` | 0 | 0 | 1 |
| `test_pr124_review_r2_fixes_ui.py` | 0 | 0 | 19 |
| `test_queue_tray_fix4_mutations.py` | 0 | 0 | 4 |
| `test_queue_tray_notice.py` | 2 | 0 | 0 |
| `test_queue_tray_view.py` | 15 | 0 | 0 |
| `test_reading_view.py` | 0 | 0 | 5 |
| `test_review_pr127_timeline.py` | 0 | 0 | 4 |
| `test_review_r219_fix10_view.py` | 7 | 0 | 0 |
| `test_review_r219_fix4.py` | 0 | 0 | 39 |
| `test_review_r219_fix6.py` | 7 | 0 | 0 |
| `test_review_r219_fix8_engine.py` | 8 | 0 | 0 |
| `test_review_r219_fix8_mutations.py` | 6 | 0 | 1 |
| `test_review_r219_fix8_view.py` | 6 | 0 | 0 |
| `test_review_r219_fix9_view.py` | 3 | 0 | 0 |
| `test_review_r219_probes.py` | 23 | 0 | 0 |
| `test_status_model.py` | 0 | 0 | 25 |
| `test_visual_progress.py` | 0 | 0 | 5 |
| `test_visual_report_hygiene.py` | 0 | 0 | 15 |
| `test_visual_review_fixes.py` | 0 | 0 | 43 |
| `test_visual_round_five.py` | 0 | 0 | 9 |
| `test_visual_round_four.py` | 0 | 0 | 27 |
| `test_visual_round_three.py` | 0 | 0 | 28 |
| `test_visual_round_two.py` | 0 | 0 | 85 |
| `test_visual_snapshots.py` | 0 | 0 | 3 |
| `test_visual_suggestions.py` | 0 | 0 | 1 |
| **Total** | **514** | **1** | **347** |

| Other requested file | Passed | Skipped | Unrun |
| --- | ---: | ---: | ---: |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 |

### Delivery and cleanup

The required `pgrep -fl 'suite_hold.sh|pytest|lockf'` check was attempted; the
sandbox denied process listing (`sysmond service not found`). A fallback `lsof`
check of all this job's hold logs returned no open descriptors. Every managed
foreground command returned; no foreign process was signalled. Job scratch,
venv/cache/probes and the generated `build/` directory are removed at delivery.
The workspace-local Git metadata is retained. No logs, JSON evidence or bundles
are staged. No installed app, live store or live daemon was touched. Nothing
was fetched or pushed.
