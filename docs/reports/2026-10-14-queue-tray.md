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

Python 3.14.7 (free-threaded): the requested initial frontend hold completed
with **15 passed, 1 skipped in 353.34 s**. The live-daemon test skips because
macOS boot identity is unavailable in this sandbox; its codec failure is
covered independently by the exhaustive codec check. `test_core_queue_tray.py` then passed all 50 cases. Its hold was interrupted
after 1,177.27 s to stay below 20 minutes; pytest was confirmed active, not
governor-stopped. The notice and queue engine files had not run and resume
in their own hold. Daemon and remaining frontend coverage are pending.

| File | Passed | Failed | Errors | Skipped |
| --- | ---: | ---: | ---: | ---: |
| `tests/frontend/test_core_live.py` | 0 | 0 | 0 | 1 |
| `tests/frontend/test_core_protocol.py` | 10 | 0 | 0 | 0 |
| `tests/frontend/test_visual_progress.py` | 5 | 0 | 0 | 0 |
| `tests/frontend/test_core_queue_tray.py` | 50 | 0 | 0 | 0 |

Final per-file outcomes, unrun coverage and commits will be recorded before
delivery. Every pytest invocation uses its
own foreground shared-lock hold, one pytest process and the bounded CI
Hypothesis profile. Scratch and uv caches are isolated under that hold's
`TMPDIR`; `PYTHONPATH` and `SUBFLEET_RETENTION_DORMANT` are unset.
