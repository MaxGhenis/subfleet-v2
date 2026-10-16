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
