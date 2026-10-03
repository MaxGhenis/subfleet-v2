# Steer provider verification (2026-09-28)

Verified before implementing the drivers against the installed **Claude Code
2.1.280** executable and the committed **Codex 0.153.3** app-server schema
fixture. No provider process or model call was launched, no other account was
used, and no live daemon or user state was changed. `CONFIRMED` below means
confirmed by the stated static evidence, not a successful live steer.

Claude evidence was extracted with bounded excerpts from `strings -n 8
/Users/maxghenis/.local/share/claude/versions/2.1.280`; subsequent bounded byte
reads located the same strings precisely. Byte offsets below refer to that
exact binary. Codex evidence was located with `git grep turn/steer` and inspected
in `tests/fixtures/codex/app-server-0.153.3/`. Its README explicitly says that only
schemas used by existing driver frame validation were retained.

| Design §4 claim | Finding | Evidence and consequence |
| --- | --- | --- |
| A stream-json user frame accepts `priority: "next"`; values are `now`, `next`, `later`. | **CONFIRMED** | Claude byte 172048427: `priority:V(["now","next","later"]).optional()`. Priority ranks at 177418588: `p4={now:0,next:1,later:2}`. Steers explicitly use `next`, independently of rollout-dependent defaults. |
| `next` may fold at the next tool boundary; a late message may start its own turn. | **CONFIRMED** statically; **UNVERIFIABLE** live | Claude 184634398 and 184694907 call `getCommandsByMaxPriority("next")` at turn start and after a tool batch, gated by fold suspension/max turns; 184632509 consumes commands as `absorbed_mid_turn` and emits `started`. The lifecycle schema explicitly describes both folded and fresh-turn paths. The driver keeps stdin open for either path. |
| Lifecycle states are `queued`, `started`, `completed`, `cancelled`, `discarded`, `refused`, keyed by client message UUID. | **CONFIRMED** | Claude 172163165 defines all six states; immediately preceding it, `command_uuid` is described as the inbound client UUID, renamed from engine `uuid`. `queued` means enqueued; `started` means drained into a turn. `refused` may occur without `queued`; terminal states may lack `started`; thrown turns may lack any terminal state. |
| Lifecycle completion can occur before or after `result`. | **CONFIRMED** | The schema description after 172163165 says a folded command emits `completed` **before** its result, and a fresh-turn command **after** its result (potentially delayed by background work). The driver reads lifecycle records even after ending, and uses result consumption IDs instead of requiring lifecycle/result adjacency. |
| `result.user_message_uuids` identifies consumed messages. | **CONFIRMED** | Claude 172098556 describes all consumed client UUIDs, in consumption order: a merged batch followed by folded messages. The list is bounded to 64 entries; `user_message_uuid` is the triggering/last batch member, not the entire set. Both success and error result shapes carry these fields. Lifecycle and replay evidence remain necessary. |
| `result.queued_turn_count` tells the host whether another result may follow. | **CONFIRMED** | Claude 172089108: counts pending **user sends**, not remaining results; sends may coalesce. Positive means more work follows unless cancelled; zero also occurs during shutdown/end-session. It can be absent on startup failures or surfaces without a queue. |
| `result.result_index` orders result delivery. | **CONFIRMED** | Claude 172090331 describes a zero-based result delivery sequence, assigned when written, distinct from `num_turns`; failed writes can produce gaps. It is optional and is not used as consumption evidence. |
| One queued message is cancelled with `cancel_async_message`. | **CONFIRMED** | Claude 172251463: request `{subtype:"cancel_async_message",message_uuid:string}`; success payload `{cancelled:boolean}`. The wrapper is the normal `control_request` with a request ID. `cancelled:false` means already dequeued **or** never enqueued. `control_cancel_request` cancels a control request, not a queued user message. |
| Interrupt can cancel queued messages and report their fates. | **CONFIRMED** | Claude 172208286 defines optional `cancel_queued`; 172210400 defines receipt `still_queued:string[]` and optional `cancelled:string[]`. With support and `cancel_queued:true`, queued/pending-dispatch commands (including a mid-fold UUID) are cancelled. A hosted client's irreversible in-flight sends may remain in `still_queued`; the driver requests individual cancellation for these instead of assuming they were dropped. |
| `system/init` advertises the required capabilities. | **CONFIRMED** | Claude 184859588–184859725 defines `interrupt_receipt_v1`, `msg_lifecycle_v1`, `interrupt_cancel_queued_v1` and the init capability list `oMr=[Su,Jb,bu]`. The blocked R1 stream also advertises these three, plus `mcp_read_resource_v1` and `mcp_tool_ui_meta_v1`. Steer is refused without `msg_lifecycle_v1`; queued interrupt cancellation is sent only when advertised. |
| Any `cancelled` lifecycle is proof that a message was never consumed and can be requeued. | **CONTRADICTED** if read without the design's consumed-first precedence | The lifecycle schema explicitly says already-consumed folds can become `cancelled` when their consuming turn aborts/fails: “cancelled-over-completed is deliberate dup-over-loss.” The driver preserves consumption/delivery evidence over subsequent cancellation. It requeues cancellation only without delivery evidence. |
| A silently dropped steer can always be declared missed after a 15-second timeout. | **CONTRADICTED** | Neither silence nor a negative cancellation receipt distinguishes “never enqueued” from “already dequeued.” After 15 seconds the driver requests cancellation; after a further bounded 15-second receipt grace it closes using the last result. Positive cancellation safely means missed; missing/negative cancellation with no delivery evidence remains `unknown`, which settles `delivery-unknown`. This is necessary for the design's exactly-once invariant. A steer already started/replayed as an own turn is not subject to this unseen-message timer. |
| Codex `turn/steer` parameters are `threadId`, `expectedTurnId`, `input`, optional `clientUserMessageId`. | **CONFIRMED** | `ClientRequest.json:5451` (`TurnSteerParams`) and `:7185` (`turn/steer`). `threadId`, `expectedTurnId`, `input` are required; `input` uses the same `UserInput` definition as `turn/start`. The expected ID is explicitly an active-turn precondition. New driver tests validate generated frames against this exact fixture. |
| Codex success response is `{turnId}`. | **UNVERIFIABLE** from the requested fixture | No `TurnSteerResponse` or general server-response schema is retained. The repo's provider map states `{turnId}`, but is secondary evidence. Driver accepts that form as acceptance and waits for history evidence before marking delivered; no success-response shape alone proves delivery. |
| Codex no-active-turn / mismatched-turn errors use `-32600`. | **UNVERIFIABLE** for the numeric code; mismatch refusal **CONFIRMED** | `TurnSteerParams.expectedTurnId` says mismatch fails. `JSONRPCError.json` permits an integer code without mapping these cases. `ServerNotification.json:774` documents `activeTurnNotSteerable` for active turns such as review/manual compaction. Driver treats any response error as refusal, so the numeric mapping is not relied on. |
| A Codex `userMessage` item carries the submitted ID as `clientId`. | **CONFIRMED** for schema shape; **UNVERIFIABLE** live correlation | `ServerNotification.json:4441` has optional nullable string `clientId` on `UserMessageThreadItem`, whose content is `UserInput[]`. The driver requires exact `clientId` equality, filters other thread/turn notifications, marks an echo without later agent output `unanswered`, and continues reading late responses/items after `turn/completed`. |

## Blocked live run and remaining verification

Read the harness and the original `cli-steer/live-9YO2fv/runs/R1` artifacts.
`R1.summary.json` records one initial message, no tool boundary, no second
message (`msg2_t: null`), one result at 1.225 seconds, and exit 1. The stdout
reports the default login's weekly quota failure. Its initial UUID has
`queued`, `started`, then `cancelled` 0.001 seconds after the error result.
Consequently this run verifies neither a real fold nor a real own-turn steer;
the harness's mock runs are not provider evidence.

Until the first real steer after installation, live fold/own-turn timing,
Claude cancellation acknowledgement timing, Codex success/error response
shape, exact client-ID correlation, and whether later agent output actually
answers the steer remain **UNVERIFIED**. Fakes and driver tests cover both
Claude paths, silent loss, late terminal evidence, and Codex unanswered history
insertion without claiming those tests prove provider behavior.
