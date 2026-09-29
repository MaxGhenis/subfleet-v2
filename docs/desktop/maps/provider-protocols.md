# Installed provider CLIs and interactive protocols (primary-source verification)

Scratch artifacts (read-only work; no model calls): `/tmp/claude-501/proto-schemas/`
- Codex JSON Schema, stable surface: `codex-0.153.3-json/` (39 top-level files; bundle `codex_app_server_protocol.v2.schemas.json`, 622 definitions)
- Codex JSON Schema, experimental surface: `codex-0.153.3-json-experimental/` (47 top-level files)
- Codex TypeScript bindings: `codex-0.153.3-ts/` (94 entries)
- Codex README at the pinned tag: `codex-app-server-README-rust-v0.153.3.md` (2927 lines; fetched from raw.githubusercontent.com, openai/codex tag `rust-v0.153.3`)
- Claude: `claude-help-2.1.280.txt`, and `claude-2.1.280-sdk-schema-region.js`, a 600 KB slice of the binary's embedded zod SDK schemas
- Docs fetched from code.claude.com: `doc-typescript.md`, `doc-cli-reference.md`, `doc-user-input.md`, `doc-permissions.md`

## 1. Versions and pins

| Item | Observed (command output) |
|---|---|
| `which claude` | `/Users/maxghenis/.local/bin/claude`, a symlink to `~/.local/share/claude/versions/2.1.280` (Mach-O arm64, 217 MB, one Bun-compiled binary). Versions 2.1.260 and 2.1.278 are also on disk. |
| `claude --version` | `2.1.280 (Claude Code)` |
| `which codex` | `~/.bun/bin/codex`, which resolves to `~/.bun/install/global/node_modules/@openai/codex/bin/codex.js` (package.json `"version": "0.153.3"`) |
| `codex --version` | `codex-cli 0.153.3` |
| `~/.subfleet/guard/TRUST` line 3 | `"codex_version": "codex-cli 0.153.3"`. Line 7 provenance says: "Codex 0.153.3 is the lane version pin; runtime hooks/list must confirm trust" |
| Latest on npm registry (live, 2026-09-24) | `@openai/codex` 0.156.1; `@anthropic-ai/claude-code` 2.1.281; `@anthropic-ai/claude-agent-sdk` 0.3.281 (claudeCodeVersion 2.1.281) |

What the v2 repo pins (worktree at `3f155e5`):
- **Codex is enforced.** `subfleet/guard/preflight.py:573-575` compares `codex --version` stdout exactly against `trust["codex_version"]` and refuses with `VERSION` on any mismatch. Tests also pin it: `tests/unit/test_guard_trust.py:51,257,640-647` uses 0.153.3, and 0.154.0 as the drift case.
- **Claude is not pinned anywhere.** Neither TRUST nor code pins a Claude version. The stream parser documents shapes "the installed Claude Code 2.1.260 binary validates" (`subfleet/adapters/claude_stream.py:9-10`). Fixtures are built from 2.1.260 (`tests/fixtures/claude/make_fixtures.py:13,138,251`), and `adapters/claude.py:86` cites 2.1.278. The installed binary is 2.1.280, and the symlink changed on Sep 22 12:41.
- **`tools/`** contains no toolchain version pins. The only matches are plist `version="1.0"` at `tools/canary_runbook.sh:78,80`.

How v2 calls the providers today (relevant to the gap):
- **Claude** is one-shot `-p` with `--output-format stream-json --verbose`, the prompt on stdin, and `--session-id` or `--resume` (`adapters/claude.py:1297-1305,1350-1353`). Writable jobs use `--dangerously-skip-permissions` (`adapters/claude.py:1209-1210`), so a permission prompt can never surface. No code under `subfleet/` uses `--input-format`, `control_request` or `stream_event` (grep: no matches).
- **Codex** is `codex exec --json` (`adapters/codex.py:425`), plus `-c model_reasoning_effort=` (`:434-435`), `resume <id> -` (`:441-442`), and the guard hooks passed through `-c hooks=…` (`:438-439`). v2 uses `codex app-server` only for metadata probes: `hooks/list` in `guard/preflight.py:430-455`, and `configRequirements/read` / `config/read` in `adapters/isolation.py:60-68`. No v2 code sends `turn/start` or `turn/interrupt`.

## 2. Claude Code 2.1.280 streaming and control protocol

### Flags (from `claude --help`, `claude-help-2.1.280.txt`)
- `-p/--print` (line 167)
- `--input-format text|stream-json`, "(realtime streaming input)", only with `--print` (119-122)
- `--output-format text|json|stream-json` (142-146)
- `--include-partial-messages`, only with `--print` and stream-json output (116-118)
- `--replay-user-messages`, which re-emits stdin user messages for acknowledgment and needs stream-json on both sides (187-190)
- `--permission-mode acceptEdits|auto|bypassPermissions|manual|dontAsk|plan` (147-150)
- `--permission-prompts host|none`, default `host`, meaning "the SDK host or --permission-prompt-tool" (151-158)
- `--resume [id]` (206), `--session-id <uuid>` (220), `--fork-session` (100)
- `--effort low|medium|high|xhigh|max` (80), `--model` (130), `--fallback-model` (90), `--settings <file-or-json>` (224)
- `--no-session-persistence` (139), `--include-hook-events` (113), `--forward-subagent-text` (103), `--verbose` (256)

**`--permission-prompt-tool` is hidden in 2.1.280.** The binary registers it as `new J("--permission-prompt-tool <tool>","MCP tool to use for permission prompts (only works with --print)")…hideHelp()` (binary offset 186381447). The public CLI reference still documents it (`doc-cli-reference.md:111`).

**How the CLI decides the SDK host answers permission prompts.** The binary contains `function SVt({permissionPromptTool:e,sdkUrl:n}){return n?"stdio":e}` (offset 180347539). A log line nearby renders the value `"stdio"` as "SDK host". So a raw client must launch with `--permission-prompt-tool stdio` to receive `can_use_tool` requests on stdout. I read this from the binary; I did not exercise it live. Per the docs, "In a `-p` run with no host, these requests are denied either way" (code.claude.com/docs/en/headless, "Turn off permission prompts" section).

**Validation errors in the binary:**
- `Error: --input-format=stream-json requires output-format=stream-json.` (offset 72075260)
- `…requires --print.`
- `Error: When using --print, --output-format=stream-json requires --verbose` (offset 80780204)

### Wire shapes (embedded zod schemas; offsets are in the 2.1.280 binary)

**User message on stdin** (`Dl`, offset 172047790):
```
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"…"},
 {"type":"image","source":{"type":"base64","media_type":"image/png","data":"…"}}]},
 "parent_tool_use_id":null,"uuid":"<client uuid>","session_id"?:…,"priority"?:"now"|"next"|"later",
 "shouldQuery"?:bool,"client_composed"?:true}
```
- The image block form is the official example on code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode.
- `SDKUserMessage` fields are documented at `doc-typescript.md:1267-1285`.
- `shouldQuery:false` appends the message to the transcript without starting a turn (offset 172049101).

**Control envelope** (`cs`, offset 172318255):
```
{"type":"control_request","request_id":"<sender-unique>","request":{"subtype":…}}
```
Either side may send one. The describe text says the receiver answers with exactly one `control_response`.

**Responses** (`SM`/`TM`/`kMn`, offsets 172320200 / 172320598):
```
{"type":"control_response","response":{"subtype":"success","request_id":…,"response"?:{…},
  "pending_permission_requests"?:[control_request…],"pending_user_dialog_requests"?:[…]}}
{"type":"control_response","response":{"subtype":"error","request_id":…,"error":"…"}}
```

**Cancel** (`RMn`, offset 172321129):
```
{"type":"control_cancel_request","request_id":…}
```
This withdraws the sender's own in-flight request, for example a pending `can_use_tool` after an interrupt. No reply is sent. There is also `{"type":"keep_alive"}`, which receivers must ignore.

### Approval round trip

The CLI sends a `can_use_tool` request (`Kr`, offset 172215147):
```
{"subtype":"can_use_tool","tool_name":…,"input":{…},"tool_use_id":…,
 "permission_suggestions"?:[PermissionUpdate],"blocked_path"?,"decision_reason"?,"decision_reason_type"?,
 "classifier_approvable"?,"suppress_always_allow_rule"?,"default_to_no"?,"matched_ask_rule"?,
 "title"?,"display_name"?,"agent_id"?,"description"?,"requires_user_interaction"?,"mcp_server"?}
```
- The live emitter is at binary offset 193097823.
- `requires_user_interaction:true` means "one-tap Approve/Deny must not be offered".

The host answers with a success `control_response` whose `response` is a `PermissionResult` (`yl`, offset 172007027):
```
{"behavior":"allow","updatedInput"?:{…},"updatedPermissions"?:[…],"toolUseID"?,"decisionClassification"?:"user_temporary"|"user_permanent"|"user_reject"}
{"behavior":"deny","message":"…","interrupt"?:bool,"toolUseID"?,"decisionClassification"?}
```
- The public type is `PermissionResult` at `doc-typescript.md:1055-1072`.
- Permission prompts do not time out. If no response is sent, "the tool call [stays] blocked indefinitely" (`doc-typescript.md:1051`).
- On reconnect, a success `initialize` response carries `pending_permission_requests`. The field is always present from v2.1.268, and a client must treat a missing field as an older CLI (`doc-typescript.md:689-693`). Repeated IDs must be handled idempotently.
- Denials also stream as `{"type":"system","subtype":"permission_denied",tool_name,tool_use_id,…}` (`tv` schema) and appear in the result's `permission_denials`.

### Interrupt

Request (`qr`, offset 172207796):
```
{"subtype":"interrupt","cancel_queued"?:bool}
```
Receipt (`hv`, offset 172210388):
```
{"still_queued":[uuid…],"cancelled"?:[uuid…]}
```
- The receipt arrives only when the CLI advertises `interrupt_receipt_v1` in `system/init.capabilities` (offset 172107569). `cancel_queued` needs `interrupt_cancel_queued_v1`.
- On a clean interrupt the receipt arrives before the aborted turn's `result` (`doc-typescript.md:695-720`).
- `cancel_async_message {message_uuid}` drops one queued message (offset 172251463).
- SIGTERM exits with 143, leaves the turn unfinished and records no result; a later resume continues it (headless docs, "Stop a run with SIGTERM").

### Output stream (stdout)

The frame types are:
- `system/init`, which carries model, tools, `capabilities[]`, `fast_mode_state` and `permissionMode`
- `stream_event {event: <Anthropic raw stream event>, uuid, session_id, user_message_uuid?}`, only with `--include-partial-messages` (`Az`, offset 172119524; `doc-typescript.md:1573-1590`)
- complete `assistant` and `user` messages
- `system/api_retry`
- `result` with subtype `success` or `error_*`, `permission_denials` and `fast_mode_state` (`claude_stream.py:12-28`; the fields were re-confirmed in the binary region)

Text deltas are `stream_event` frames with `.event.delta.type=="text_delta"` (headless docs jq example). Per-message lifecycle frames named `command_lifecycle` exist in the binary (8 schema references), with states such as queued, started, completed and cancelled.

#### Not every `result` answers our message (2.1.284, read 2026-09-29)

The CLI runs turns of its own: resuming a session whose background task was
still running when its last process ended, it runs a turn for the task's
notification (`system/task_notification`, `status: stopped`) while our
message waits in its command queue, and ends that turn with its own `result`.
The schemas embedded in the 2.1.284 executable, and every Claude turn attempt
recorded under `~/.subfleet/jobs` on 2026-09-29 (108 attempts, 2.1.280 and
2.1.284), say how to tell the turns apart:

- `command_lifecycle {command_uuid, state}`: `queued` when our message enters
  the queue, `started` when it drains into a turn, then one terminal state:
  `completed`, `cancelled` (swept by an interrupt with `cancel_queued`, or
  consumed into a turn that was aborted or died), `discarded` (the session ended
  with it queued) or `refused` (the receive-side policy declined it; never
  preceded by `queued`). A command that starts a fresh turn emits `completed`
  after that turn's `result`; one folded into a running turn emits it before.
- `result.user_message_uuid` and `result.user_message_uuids` name the client
  messages the turn consumed, folds included. Every one of our 91 results named
  our uuid; none of the 50 notification turns' results named any.
- `result.origin.kind` says where a turn's prompt came from (`human`,
  `channel`, `peer`, `task-notification`, …); all 50 notification results
  carried `task-notification`, `num_turns` 0 and no `user_message_uuid`, and
  arrived after our `queued` and before our `started`.
- `result.startup_failure_reason` marks the zeroed `error_during_execution`
  result a stream-json run writes before it exits on a known startup failure
  (`cwd_unavailable`, `org_verify_failed`, `temp_dir_unusable`, …).
- `interrupt {cancel_queued: true}` also cancels our message if it has not
  started, with a `cancelled` lifecycle for it; 2.1.280 and 2.1.284 advertise
  `interrupt_cancel_queued_v1`. Closing stdin cancels nothing: on EOF the CLI
  still runs what is queued (observed 2026-09-28, job
  20260928-152257-turn-cv-1790623376839-ca22af954212).

`claude_turn.py` `_whose` attributes each `result` from these (C-26.5).

### Settings control requests
- `set_model {model?}`: "subsequent conversation turns" (`Yr`, offset 172219988)
- `set_permission_mode {mode}` (`Fr`, offset 172219572)
- `set_max_thinking_tokens`
- `apply_flag_settings {settings:{…}}` (offset 172297348)

The docs say `effortLevel` and `fastMode` passed through `applyFlagSettings` apply "on the next turn", while `model` applies during the current turn (`doc-typescript.md:589-611`). Fast mode needs an opt-in: `fastMode:true` through `--settings` or `apply_flag_settings`, otherwise the reason is `sdk_opt_in_required` (`doc-typescript.md:1419`). The state enum is `off|cooldown|on` (offset 172186902).

### TypeScript types
The installed CLI is a single compiled binary. It ships **no `.d.ts`**, and no `@anthropic-ai/claude-agent-sdk` is installed locally; the only Anthropic package present is `@anthropic-ai/sdk` 0.8.1 in `~/.bun/install/global`. The public types are cited from code.claude.com/docs/en/agent-sdk/typescript instead. **UNVERIFIED:** whether the TS SDK itself passes `--permission-prompt-tool stdio` (I did not read the SDK source).

## 3. Codex app-server 0.153.3 protocol (generated schema)

**Status.** `codex app-server --help` labels the whole command "[experimental]", and `generate-ts` / `generate-json-schema` too. The transports are:
- stdio JSONL (the default)
- `unix://`, as websocket over `$CODEX_HOME/app-server-control/app-server-control.sock`
- websocket, which the README calls "**experimental / unsupported**" (`README@rust-v0.153.3:22-37`)

**Envelope.** JSON-RPC 2.0 "with the `"jsonrpc":"2.0"` header omitted on the wire" (README:22). The schema's `JSONRPCRequest` has only `id`, `method`, `params` and `trace`, with `id` and `method` required.

**Handshake.** The client sends:
```
{"id":1,"method":"initialize","params":{"clientInfo":{name,title?,version},"capabilities"?:{experimentalApi?,optOutNotificationMethods?,…}}}
```
then the notification `{"method":"initialized"}`. The response is `{codexHome,platformFamily,platformOs,userAgent}` (`v1/InitializeParams.json`, `InitializeResponse`). `experimentalApi` is negotiated once per process, and re-initializing is rejected with "Already initialized" (README:2861-2862).

**Direction counts:** 99 stable client requests (155 with experimental), 10 server requests (11 with experimental), 81 server notifications, and 1 client notification (`initialized`).

### Thread and turn requests (client to server)
- **`thread/start`** (`ThreadStartParams`): `approvalPolicy?`, `approvalsReviewer?`, `baseInstructions?`, `config?`, `cwd?`, `developerInstructions?`, `ephemeral?`, `model?`, `modelProvider?`, `personality?`, `sandbox?` (`read-only|workspace-write|danger-full-access`), `serviceName?`, `serviceTier?`, `sessionStartSource?`, `threadSource?`.
  - The response carries `thread`, `model`, `reasoningEffort?`, `serviceTier?`, `approvalPolicy`, `sandbox` and `cwd`.
  - **Side effect:** when `cwd` is given with workspace-write or full access, the app-server "marks that project as trusted in the user `config.toml`" (README:174).
- **`thread/resume`** `{threadId, model?, cwd?, approvalPolicy?, sandbox?, serviceTier?, excludeTurns?, …}`. The response adds cursors for turns and items.
- **`thread/fork`**, **`thread/read`**, **`thread/turns/list`**, **`thread/items/list`**, **`thread/list`**, **`thread/unsubscribe`**, **`thread/rollback`** / **`thread/revert`**.
- **`turn/start`** (`TurnStartParams`): `threadId`, `input:[UserInput]`, `clientUserMessageId?`, and overrides `model?`, `effort?`, `serviceTier?`, `serviceTierForTurn?`, `approvalPolicy?`, `approvalsReviewer?`, `sandboxPolicy?`, `cwd?`, `summary?`, `personality?`, `outputSchema?`, `toolOutput?`, `turnTrigger?`.
  - The response `{turn}` arrives immediately with `status:"inProgress"`.
  - `clientUserMessageId` is echoed as `clientId` on the `userMessage` item (README:222).
- **`turn/steer`** `{threadId, expectedTurnId, input, clientUserMessageId?}` returns `{turnId}`. It fails if `expectedTurnId` is not the active turn (README:1417-1435).
- **`turn/interrupt`** `{threadId, turnId}` returns `{}`. The authoritative end is `turn/completed` with `status:"interrupted"`. Background terminals are not stopped (README:1315-1327).
- **Catalog calls:** `model/list` returns `Model{id, supportedReasoningEfforts[], defaultReasoningEffort, serviceTiers[{id,name,description}], defaultServiceTier?, inputModalities?[text|image|audio]}`. Others: `account/rateLimits/read`, `hooks/list`, `config/read`.

**`UserInput` variants:**
- `{type:"text",text}`
- `{type:"image",url,detail?}`: data URLs only; remote HTTP(S) is rejected (README:1005)
- `{type:"localImage",path,detail?}`
- `{type:"audio",url}`, `{type:"localAudio",path}`
- `{type:"skill",name,path}`, `{type:"mention",name,path}`

### Approvals (server-to-client JSON-RPC requests, each with an `id`)

**`item/commandExecution/requestApproval`** (`CommandExecutionRequestApprovalParams`)
- Params: `threadId`, `turnId`, `itemId`, `startedAtMs`, `approvalId?`, `command?`, `cwd?`, `commandActions?`, `environmentId?`, `kind?` (`command|writeStdin`), `reason?`, `networkApprovalContext?`, `proposedExecpolicyAmendment?`, `proposedNetworkPolicyAmendments?`.
- Response: `{"decision": "accept"|"acceptForSession"|{"acceptWithExecpolicyAmendment":…}|{"applyNetworkPolicyAmendment":…}|"decline"|"cancel"}`.
- `decline` means the agent continues; `cancel` means the turn "will also be immediately interrupted".

**`item/fileChange/requestApproval`**
- Params: `threadId`, `turnId`, `itemId`, `startedAtMs`, `reason?`, `grantRoot?` (marked [UNSTABLE]).
- Decision: `accept|acceptForSession|decline|cancel`.

**`item/permissions/requestApproval`**
- Params: `threadId`, `turnId`, `itemId`, `cwd`, `permissions{fileSystem?,network?}`, `reason?`, `environmentId?`.
- Response: `{permissions:<granted subset>, scope?:"turn"|"session", strictAutoReview?}`. Omitted permissions count as denied (README:2020-2065).

**Other server requests:**
- `item/tool/requestUserInput`, marked EXPERIMENTAL; answer with `{answers}`
- `mcpServer/elicitation/request`
- `item/tool/call` (dynamic tools)
- `account/chatgptAuthTokens/refresh`
- `attestation/generate`
- legacy `applyPatchApproval` / `execCommandApproval`, which take a `ReviewDecision` of `approved|approved_for_session|{denied}|abort|timed_out|…`

**Message order for one approval** (README:1943-1968):
1. `item/started`
2. the request
3. the client response
4. `serverRequest/resolved {threadId, requestId}`, also emitted when a turn start, completion or interrupt clears the request
5. `item/completed` with status `completed|failed|declined`

`thread/status/changed` carries `activeFlags` of `waitingOnApproval|waitingOnUserInput`.

### Streaming notifications
- `turn/started {threadId, turn}`
- `item/started {item, threadId, turnId, startedAtMs}`
- `item/agentMessage/delta {delta, itemId, threadId, turnId}`: concatenate per `itemId` (README:1863-1865)
- `item/reasoning/summaryTextDelta`, `item/reasoning/textDelta`
- `item/commandExecution/outputDelta`, `item/fileChange/outputDelta`, `item/fileChange/patchUpdated`
- `turn/diff/updated {diff}`, `turn/plan/updated`
- `item/completed`
- `turn/completed {threadId, turn{id, status: completed|interrupted|failed|inProgress, error?{message, codexErrorInfo?, …}, items, durationMs?}}`
- `thread/tokenUsage/updated`
- `error {error, threadId, turnId, willRetry}`
- `thread/settings/updated`, `model/rerouted`, `account/rateLimits/updated`

**Only in the experimental schema:**
- methods such as `thread/settings/update`, `turn/settings/update`, `thread/queue/*`, `process/*`, `remoteControl/*`
- fields such as `availableDecisions` / `additionalPermissions` on command approvals, `permissions`, `environments` and `collaborationMode` on `turn/start`
- the server request `currentTime/read`

## 4. Capability comparison

| Need | Claude 2.1.280 | Codex 0.153.3 app-server |
|---|---|---|
| Multi-turn on one session | One long-lived `-p --input-format stream-json --output-format stream-json --verbose` process. Write more `type:"user"` lines, and each turn ends in a `result`. Extra messages queue (`priority`). Across processes, use `--session-id <uuid>` then `--resume <id>`. | One process can hold many threads. Call `thread/start` once, then `turn/start` repeatedly. Use `thread/resume` after restart. Use `turn/steer` to add input to an active turn. |
| Interrupt | `control_request {subtype:"interrupt", cancel_queued?}` returns a receipt `{still_queued, cancelled?}`, then the aborted turn's `result`. `control_cancel_request` withdraws pending prompts. | `turn/interrupt {threadId, turnId}` returns `{}`, then `turn/completed status:"interrupted"`. The approval decision `cancel` also interrupts. |
| Approval round trip | Needs `--permission-prompt-tool stdio` (hidden flag), and a permission mode other than `bypassPermissions`. Request is `can_use_tool`; answer with `{behavior:allow\|deny,…}`. No timeout. Pending requests are redelivered on `initialize`. | Server JSON-RPC requests; answer with `{decision}`. `serverRequest/resolved` confirms or clears. Requires `approvalPolicy` other than `never` and `approvalsReviewer:"user"`. |
| Model / effort / fast per turn | Launch flags `--model` and `--effort`. At runtime, `set_model` and `apply_flag_settings {effortLevel, fastMode}` affect the session from the next turn onward; there is no single-turn scope. Fast mode needs an opt-in and reports `fast_mode_state`. | `turn/start` `model` / `effort` / `serviceTier` persist "for this turn and subsequent turns". Only `serviceTierForTurn` and `outputSchema` are turn-scoped. Effort is an open string that must match `model/list` `supportedReasoningEfforts`. Tier IDs come from `serviceTiers[].id`. **UNVERIFIED** which ID means "fast": `model/list` was not called. |
| Images | Base64 `image` content blocks in `message.content`. | `image` (data URL) or `localImage` (path); check `Model.inputModalities` includes `image`. |
| Idempotency hooks | The user message `uuid`, echoed by `--replay-user-messages` and as `user_message_uuid` on `stream_event`; `command_lifecycle` frames. | `clientUserMessageId` echoed as `userMessage.clientId`. |

## 5. Legacy transports (read-only reference)

Paths are under `/Users/maxghenis/chief-of-staff-worktrees/subfleet-traycer-port/subfleet/subfleet/`.

**`claude_transport.py`**
- Launches `-p --resume <id> --model … --input-format stream-json --output-format stream-json --verbose --include-partial-messages --replay-user-messages --permission-mode …` (lines 55-61), without `--permission-prompt-tool stdio`.
- Answers every `control_request` with `subtype:"error"` "Subfleet cannot approve this request automatically" (225-231).

**`codex_transport.py`**
- Initializes with `experimentalApi:true` (299-309).
- Automatically answers `cancel` to command and file approvals (596-601), `abort` to legacy approvals, empty permissions to permission requests (602-603), and `answers:{}` to user-input requests.
- Uses `thread/resume`, `turn/start`, `turn/interrupt` and the notifications `turn/completed`, `item/agentMessage/delta`, reasoning deltas and `item/*` (402-580).

In short, the legacy code **never implemented a real approval round trip for either provider**. That behavior has to be built new, not ported.

## 6. Risks

1. **Codex app-server is experimental.** The whole `app-server` command is "[experimental]" in 0.153.3 help. Its opt-in experimental surface has "no backwards-compatible guarantees" (README:2815-2818), and the legacy client opted in. Build the conversation service against the stable schema and do not set `experimentalApi`. Note that `availableDecisions` and `additionalPermissions` exist only in the experimental schema.
2. **Codex docs disagree with the schema.** The README `turn/start` example uses `"approvalPolicy": "unlessTrusted"`, but the generated `AskForApproval` accepts only `untrusted|on-request|never|{granular}`, and `granular` is experimental-gated (README:2870s). The README on `main` has been restructured (456 lines against 2927 at the tag). Treat the tag README and the generated schema as authoritative.
3. **Codex version drift.** The pin (0.153.3) matches the installed CLI, and npm latest is 0.156.1. Any upgrade trips `guard/preflight.py:574-575`, and the generated schema must be regenerated and diffed. Per the plan, do not move the pin to make a transport work.
4. **Guard coverage on app-server turns is UNVERIFIED.** The guard override is applied to `app-server` through `-c` only in the `hooks/list` probe (`preflight.py:455`). I have not verified that never-rules hooks fire identically for `turn/start` tool calls, compared with `codex exec`.
5. **`thread/start` writes to lane config.** With `cwd` and a writable sandbox, it writes project trust into the lane's `config.toml` (README:174). That mutates lane `CODEX_HOME` state.
6. **Codex credential and attestation requests.** The server-to-client requests `account/chatgptAuthTokens/refresh` and `attestation/generate` exist. The client should refuse them, for example with -32601, as the legacy code did, so the app never handles tokens.
7. **Claude is unpinned and auto-updating.** Three versions are on disk; the symlink moved to 2.1.280 on Sep 22 and npm latest is 2.1.281. Several v2 contracts were written against 2.1.260. A warm process can keep an old binary while new launches use a newer one. Feature-detect through `system/init.capabilities` (`interrupt_receipt_v1`, `interrupt_cancel_queued_v1`) and the `initialize` response, not through version strings.
8. **Claude's raw wire protocol relies on hidden and internal surface.** `--permission-prompt-tool` is hidden in 2.1.280 help, the `"stdio"` host value comes from the binary rather than the CLI docs, and many schema fields are tagged `@internal`. The public docs cover the SDK callback API more than the raw stdio wire.
9. **Enabling Claude approvals changes v2's permission posture.** v2's writable Claude jobs currently use `--dangerously-skip-permissions` (`claude.py:1209-1210`). A real approval round trip needs a prompting permission mode plus `--permission-prompt-tool stdio`, which changes that posture and must keep the never-rules protections.
10. **Pending approvals can wait forever.** Claude prompts never time out (`doc-typescript.md:1051`). The daemon needs its own expiry and cancel path (`control_cancel_request`, or a deny response) and must handle redelivered `pending_permission_requests` idempotently.
11. **Unknown control requests must be refused.** Claude also defines host-directed control requests such as `oauth_token_refresh` and `host_auth_token_refresh` (binary strings). Answer unknown ones with `subtype:"error"`, and never set `CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH`.
12. **Claude fast mode spends usage credits** on Pro/Max plans (code.claude.com/docs/en/fast-mode.md:87) and needs an explicit per-session opt-in. Neither provider exposes a guaranteed single-turn model or effort scope except Codex's `serviceTierForTurn`. The app should re-assert settings before each turn and show the served model from `system/init` / `result.modelUsage` or `turn/completed`.
13. **Delivery ambiguity on termination.** SIGTERM leaves an unfinished Claude turn with no result, which resuming continues (headless docs). Codex `turn/start` returns before `turn/started`. Both cases map to the plan's "delivery-unknown" and reconciliation requirements.

**UNVERIFIED overall:** I observed no live approval, interrupt or multi-turn exchange, because those need model calls that spend quota. Every shape above comes from the installed binaries' embedded schemas, locally generated schemas, and official docs.