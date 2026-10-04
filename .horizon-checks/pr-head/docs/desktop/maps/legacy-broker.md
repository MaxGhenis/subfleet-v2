# Legacy interactive delivery (broker, outbox, transports): cartography

Legacy root: `/Users/maxghenis/chief-of-staff-worktrees/subfleet-traycer-port/subfleet`. Bare `*.py:N` citations mean `subfleet/*.py`. `v2:` means `/Users/maxghenis/subfleet-v2-lanes/desktop-workspace` at 3f155e5.

**Provenance warning:** `git status` lists these as untracked (`??`), so they exist only in the working tree: `broker.py`, `outbox.py`, `native_dispatch.py`, `claude_transport.py`, `codex_transport.py` and `docs/interactive-delivery.md`. I did not run `git status` on the six test files; they are presumably untracked too (UNVERIFIED).

Installed clients, observed with `--version` only: `codex-cli 0.153.3` and Claude Code `2.1.280`. I did not check the Codex wire field names against the installed client's schema. The module docstring claims a check against `generate-json-schema` (`codex_transport.py:8-10`), but that claim is UNVERIFIED in this session.

## 1. Broker socket protocol

**Transport**
- It is a Unix stream socket at `$SUBFLEET_BROKER_SOCKET` or `<state_dir>/broker.sock` (`broker.py:35-37`).
- Each connection carries one JSON line in and one JSON line out (`broker.py:111-137`). The socket timeout is 5 s (`:113`).
- Requests are capped at 1,100,000 bytes and responses at 8 MiB (`:30-31`).
- The request has no version field. Dispatch is on `payload["op"]` (`:165`).
- The socket is chmod 0600 after bind (`:323`). The parent must not be a symlink, the path is limited to 103 bytes, and the broker refuses to replace a non-socket path (`:310-318`).

**Operations** (`Broker.handle`, `broker.py:164-232`)

| op | Input | Result |
|---|---|---|
| `ping` | — | `{"kind":"subfleet.broker","schema_version":1,"ok":true,"pid"}` (`:166-167`) |
| `enqueue` | `message_id, session_id, prompt, image_paths, service_tier` | Outbox receipt plus optional `activity` (`:168-173`). Refused with `broker-stopping` once stop is set (`:169-170`). |
| `list` | optional `session_id` | `{"kind":"subfleet.outbox","schema_version":1,"ok":true,"messages":[...]}`. At most 100 messages, oldest trimmed first until the reply is under 8 MiB − 4096 (`:174-186`). |
| `get` | `message_id` (str, ≤64 chars; only the length is checked here) | Receipt (`:187-193`, `:218`) |
| `cancel` | `message_id` | `Outbox.cancel`. Works only on `queued` rows (`:194-197`). |
| `resolve` | `message_id, resolution:"handled", confirm:true` (a strict `True` is required; `1` fails) | `Outbox.resolve_handled` sets the row to `cancelled` with `resolution:"handled"` (`:198-203`; test `test_broker.py:264-268`) |
| `interrupt` | `message_id` | Allowed only when status is in `{dispatched, failover-dispatched, delivered-live}` (`:205-206`). Calls `native_dispatch.interrupt` and returns the existing receipt with the message "Interrupt requested; waiting for provider confirmation". The status does not change (`:207-217`). |
| `prepare` | `session_id` (`claude:`/`codex:` prefix, ≤256) | Fire-and-forget prewarm thread, debounced to 30 s per session and capped at `max_workers` concurrent (`:219-231`). Returns `status:"preparing"`. |

**Error envelope** (`_failure`, `broker.py:81-87`): `{"kind":"subfleet.session-continuation","schema_version":1,"ok":false,"accepted":false,"status":"error","code","error"(=code),"message","message_id","session_id","provider":null,"run_id":null,"pid":null}`.

**Lost-acknowledgement rule.** If `enqueue` raises an exception that is not an `OutboxError`, the handler returns without writing any response (`broker.py:125-130`). The client then sees an incomplete response, which is never a false "not admitted" (test `test_broker.py:191-208`). A failed response write is swallowed (`:134-137`).

**Receipt shape** (`Outbox._receipt`, `outbox.py:151-169`): the stored receipt JSON, overlaid with:
- `kind:"subfleet.session-continuation"`, `schema_version:1`, `accepted:true`
- `message_id`, `session_id`, `provider` (the session_id prefix), `status`
- `created_at` and `updated_at` as ISO UTC
- `prompt` (the full prompt is echoed in every receipt and in `list`), `attachment_count`, `service_tier`
- defaults `ok` = status not in {error, delivery-unknown}, `run_id`, `pid` and `error`

Dispatch receipts can add `message`, `native_message_id`, `resolution`, `attachment_delivery` and similar fields. `_with_activity` attaches the run-ledger `activity.json` sidecar if one exists. Any read failure is swallowed so it never changes the receipt (`broker.py:90-103`; test `test_broker.py:146-168`).

**Idempotency** (`outbox.py:237-289`)
- `message_id` must be a canonical UUID (`:77-82`).
- `request_digest` is sha256 of canonical JSON (`sort_keys`, compact) over the validated `{message_id, session_id, prompt, image_paths (source paths), service_tier}` (`:239`, `:45-46`).
- A duplicate with the same digest returns the original receipt. A different digest raises `message-id-conflict` (`:286-289`).
- The duplicate check runs once before snapshotting and again inside `BEGIN IMMEDIATE` (`:241-243`, `:255-259`).
- **`payload_digest`** is sha256 over the payload plus the per-image `image_sha256` byte digests (`:250`). It is stored but **never compared**; grep finds no reader. Idempotency is therefore keyed by image *paths*, not bytes. If the same ID is retried after the source file changes, the original receipt comes back (test `test_outbox.py:44-51` relies on this after the original is deleted). This falls short of the plan's "digest of attachment bytes".

**Singleton and start-up**
- `serve` takes `flock(broker.lock, LOCK_EX|LOCK_NB)`. If that fails it raises `broker-running` (`:303-307`).
- Only the lock holder runs `recover_starting()`, both before binding (`:309`) and on shutdown (`:338-340`). Provider processes are not killed on shutdown (comment `:339`).
- `ensure_running` pings with a 0.15 s timeout. On failure it spawns detached `python -m subfleet.broker --state-dir --socket` (`start_new_session`, all stdio to DEVNULL) and polls for 3 s (`:371-397`).
- `main` exports `SUBFLEET_STATE_DIR` (`:405-407`).

**Coordinator** (`broker.py:267-293`)
- Every 0.25 s it runs `monitor_fn` over `outbox.active()` and applies the outcome through `update_active`. Monitor exceptions are ignored (`:271-278`).
- It claims messages with `claim_next()` while fewer than `max_workers` threads are live. The default is 4, clamped to 1–16 (`:155`). Each claimed message gets a thread running `_dispatch` (`:280-291`).
- It waits on the wake event for 50 ms between passes.

## 2. Outbox state machine and persistence

**Storage** (`outbox.py:108-138`)
- One SQLite file, `<state>/outbox.sqlite3`, opened `O_NOFOLLOW` with mode 0600 and required to be a regular file (`:113-119`). The state directory is 0700 and must not be a symlink (`:49-53`).
- Settings: `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=5000`, autocommit connections with explicit `BEGIN IMMEDIATE` (`:121`, `:142-145`).
- Table `messages(sequence AUTOINCREMENT PK, message_id UNIQUE, session_id, request_digest, payload_digest, payload JSON, status, created_at, updated_at, receipt JSON)` with index `(session_id,status,sequence)` (`:123-136`).

**Attachments** (`_snapshot_images`, `outbox.py:185-235`)
- Each source is opened `O_NOFOLLOW|O_NONBLOCK`, which rejects symlinks and FIFOs.
- Limits: regular file, 0 < size ≤ 20 MB, total ≤ 50 MB, at most 8 images (`:28-30`). The type is checked by magic bytes: PNG, JPEG, GIF or WebP (`:64-73`).
- Each file is copied to `outbox-attachments/message-*/image-N.ext` with mode 0600 and a sha256. The copy is rejected if the size or mtime/ctime changed during the copy.
- The copied file and both directories are fsynced. The bundle is removed on failure.
- After an ambiguous COMMIT, the bundle is deleted only if the database provably does not reference it (`:269-284`).

**Validation** (`validate_request`, `outbox.py:76-105`)
- `session_id` must start with `claude:` or `codex:`, have a non-empty remainder, be ≤256 characters and contain no whitespace or control characters.
- Prompt ≤ 1 MB UTF-8. A non-empty prompt or at least one image is required.
- `service_tier` must be null, `fast` or `standard`, and is accepted only for Codex.

**Statuses** (`outbox.py:31-36`): `TERMINAL={finished,error,cancelled}` and `BLOCKING={starting,dispatched,failover-dispatched,delivered-live,delivery-unknown}`, plus `queued`.

**Transitions**
- `queued → cancelled`: `cancel`, only from `queued` (`:291-302`).
- `queued → starting`: `claim_next` (`:329-344`).
- `starting → any status except queued/starting/cancelled`: `complete_dispatch`. Anything else is coerced to `delivery-unknown`, and the update is guarded `WHERE status='starting'` (`:346-354`), so a late dispatcher result after recovery cannot clear the barrier (test `test_outbox.py:131-133`).
- `starting → delivery-unknown`: `recover_starting`, run by the lock holder at start and stop (`:304-310`).
- `{dispatched, failover-dispatched, delivered-live} → finished | error | cancelled | delivery-unknown`, and `delivery-unknown` with a `run_id` → `finished | error | delivery-unknown`: `update_active`. Unchanged values are a no-op, so there is no fake activity (`:365-382`).
- `delivery-unknown → cancelled` with `resolution:"handled"`: `resolve_handled` (`:312-327`).
- `active()` returns dispatched and live rows plus `delivery-unknown` rows **with** a `run_id`. A `delivery-unknown` row without a `run_id` stays manual-only (`:356-363`; test `test_outbox.py:150-165`).

**Per-session serialization** is layered four deep:
1. `claim_next` SQL picks the oldest `queued` row for which no row in the same session is BLOCKING and no earlier row is `queued` (`outbox.py:334-337`). A `delivery-unknown` row therefore blocks its session until it is reconciled.
2. `session_catalog._continuation_lock`, a per-session `flock` on `state/session-locks/<sha256>.lock` (`session_catalog.py:1690-1700`), taken in `native_dispatch.dispatch` (`:731`) and `prepare` (`:163`).
3. `_already_running` scans v1 run-ledger RUNNING rows whose `routing_decision.session_id` matches (`session_catalog.py:1703-1720`).
4. Transport single-writer checks: Claude `_active` (`claude_transport.py:137-138`) and Codex `ActiveTurnError` (`codex_transport.py:395-396`, `:822-823`).

Separate sessions run concurrently (test `test_broker.py:211-233`).

## 3. Claude transport (`claude_transport.py`)

**argv** (`command_for`, `:50-69`):
```
$SUBFLEET_CLAUDE_BIN|claude -p --resume <native_id> --model <model>
  --input-format stream-json --output-format stream-json --verbose
  --include-partial-messages --replay-user-messages --permission-mode <mode>
```
- Plan mode adds `--tools Read,Glob,Grep,WebSearch,WebFetch --allowedTools <same> --setting-sources "" --safe-mode --no-chrome --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands`.
- The model must match `^claude-[A-Za-z0-9][A-Za-z0-9._-]*$` (`session_catalog.py:64`, `:527-531`). The mode must be in `{acceptEdits,auto,bypassPermissions,manual,dontAsk,plan}`, with `default` mapped to `manual` (`:65-72`, `:534-540`).
- **No `--effort` is passed**, so the Claude effort setting is not preserved.
- `claude --help` on 2.1.280 lists all of these flags. It also shows `--permission-prompts host|none`, which defaults to `host` ("the SDK host … answers"); legacy code never sets it.

**Environment** (`:100-114`)
- Removes `SUBFLEET_RUN_*`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_LANE_DETACHED` and `CLAUDE_LANE_OWNED_PROMPT`.
- Sets `CLAUDE_CODE_OAUTH_TOKEN=<token>`. The token is never in argv (test `test_claude_transport.py:76-77`).
- In plan mode it also removes `CLAUDE_MEMORY_*`, `CLAUDE_COWORK_*` and `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`, and sets `CLAUDE_CODE_SAFE_MODE`, `DISABLE_CLAUDE_MDS`, `DISABLE_AUTO_MEMORY` and `DISABLE_ORG_MEMORY` to 1.

**Process**
- `Popen(cwd=row.cwd, stdin/stdout/stderr=PIPE, start_new_session=True)` (`:116-118`). A failure raises `PreCommitError`.
- A reader thread and a stderr-drain thread run alongside. Stderr is discarded (`:250-257`). `_stderr_tail` is allocated at `:99` but never used.

**Input frame** (`input_message`, `:72-87`): `{"type":"user","uuid":<message_id>,"parent_tool_use_id":null,"message":{"role":"user","content":[{"type":"text","text"}, {"type":"image","source":{"type":"base64","media_type","data"}}...]}}`. Images are native base64 blocks, never path references (test `:98-106`).

**send** (`:133-147`): one active turn per worker; otherwise `PreCommitError`. The active callback is registered **before** the write. A write `OSError`/`ValueError` emits `uncertain` (final) and raises `DeliveryUncertain`.

**Output handling** (`_handle`, `:167-232`)
- Events with a non-null `parent_tool_use_id` (subagent frames) are ignored.
- `type:"user"` with `uuid == active id` becomes `provider_accepted`. This relies on `--replay-user-messages`.
- `assistant`:
  - `message.model` differing from `row.model` and not `<synthetic>` becomes `model_mismatch`.
  - `text` blocks become `text`.
  - `tool_use` blocks become `activity` with a fixed label from `tool_activity_label` and `item_id`. Arguments are never forwarded (`:30-47`).
  - `thinking` and `redacted_thinking` become a bodiless `thinking`.
- `stream_event`:
  - `delta.type=="text_delta"` becomes `text_delta`.
  - `thinking_delta` and `signature_delta` become `thinking`.
  - `content_block_start` with a tool_use or thinking block becomes `activity` or `thinking`.
- `result` becomes `completed` (final) with `ok = is_error is False and subtype=="success"`, `text=result` and `subtype`.
- `system/init`, `rate_limit_event` and `api_retry` are **not handled**.
- EOF on stdout sets `_closed` and emits `uncertain` (final) (`:246-248`).

**Control protocol / permissions** (`:225-232`)
- Every `control_request`, whatever its subtype, gets `needs_attention` plus a written `{"type":"control_response","response":{"subtype":"error","request_id":<id>,"error":"Subfleet cannot approve this request automatically."}}`.
- The code never sends a control `initialize` or control `interrupt`.
- How the CLI treats an `error` response to a permission request (deny, or abort the turn) is UNVERIFIED.

**Interrupt** (`:259-268`): `os.killpg(pid, SIGTERM)` on the whole worker process group, only while busy. There is no in-protocol interrupt.
- Tracing the code: SIGTERM leads to EOF, then `uncertain`, then `delivery_uncertain` in the ledger, then the monitor reports `delivery-unknown` (`broker.py:65-66`). A user Stop on Claude therefore ends in the manual-resolve barrier, not in a clean `cancelled`/`interrupted` (end-to-end behaviour UNVERIFIED).

**Idle close** (`:270-276`): closes stdin if no turn is active. Whether the CLI exits on stdin EOF is UNVERIFIED.

## 4. Codex transport (`codex_transport.py`)

**argv/env** (`:261-290`)
- Command: `codex app-server --listen stdio:// -c features.fast_mode=true`, text mode, UTF-8, line-buffered, stderr to DEVNULL, `start_new_session`.
- Env removes `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_THREAD_ID` and `CODEX_SESSION_ID`, and sets `CODEX_HOME=<resolved home>`.
- One server per resolved `CODEX_HOME` (`CodexTransportManager._server`, `:711-726`).

**Framing**
- Frames are `{"id":<int>,"method","params"}`. There is **no `"jsonrpc":"2.0"` member** (`:354`).
- Ids are per-server monotonically increasing integers (`:349-352`).
- The reader classifies frames: `method` with `id` is a server request, `method` alone is a notification, `id` alone is a response (`:486-500`).
- A non-JSON or non-object frame marks the server dead (`:480-485`).

**Handshake** (`:297-309`): `initialize {"clientInfo":{"name":"subfleet","title":"Subfleet","version":"0.1.0"},"capabilities":{"experimentalApi":true}}`, then the notification `initialized {}`. `prepare()` does only this (`:728-734`; test `test_codex_transport.py:742-761`).

**Methods used** (grep confirms **`thread/start` is never called**; only existing threads are resumed):
- `thread/resume`, once per thread per server (`_resumed` set).
  - Params: `{"threadId","cwd","model","approvalPolicy","approvalsReviewer":"user","sandbox":"read-only","excludeTurns":true,"serviceTier"?}` (`:400-417`).
  - Response checks: `thread.id == threadId`; `thread.status.type != "active"`, otherwise `ActiveTurnError`; if `result.model` is present it must equal the requested model (`:418-438`).
- `turn/start` (`:457-462`, params built at `:796-809`).
  - Params: `{"threadId","clientUserMessageId":<message_id>,"input":[{"type":"text","text"},{"type":"localImage","path"}],"model","cwd","approvalPolicy","approvalsReviewer":"user","sandboxPolicy":<normalized>,"effort"?,"serviceTier"?}`.
  - Tier mapping: `standard→default`, `priority→fast` (`:779-783`).
  - The response `result.turn.id` binds the turn. The acknowledgement callback runs on the reader thread and replays buffered early approvals and notifications, then calls `_finish(turn)`, so a terminal turn inside the acknowledgement completes the handle (`:440-455`).
- `turn/interrupt {"threadId","turnId"}` (`:155-166`). Without a known turn id it raises `DeliveryUncertain`.

**Policies**
- `normalize_sandbox_policy` (`:44-96`) maps kebab/snake spellings to `dangerFullAccess | readOnly{networkAccess} | workspaceWrite{networkAccess,writableRoots(abs),excludeTmpdirEnvVar,excludeSlashTmp}`. Unknown kinds, unknown extra keys or conflicting fields raise `PreCommitError` before any process starts (test `:566-585`).
- `_approval_policy` (`:99-113`) accepts `never | untrusted | on-request` or `{"granular":{mcp_elicitations,rules,sandbox_approval[,request_permissions,skill_approval]: bool}}`.

**Notifications consumed** (`_notification`, `:508-580`)
- Routing is by `params.threadId` to the active handle, and requires `commit_attempted`. The turn id comes from `params.turnId` or `params.turn.id`. Retired `(thread, turn)` pairs are ignored.
- Before the acknowledgement, up to 2048 frames are buffered (`:526-530`).
- Handled methods:
  - `turn/completed`: `_finish`, statuses `completed|failed|interrupted`.
  - `item/agentMessage/delta`: `text_delta`.
  - `item/reasoning/summaryTextDelta`: `reasoning_summary_delta` with text.
  - `item/reasoning/textDelta`: bodiless `thinking`.
  - `turn/plan/updated`: `plan_updated` with the payload dropped.
  - `item/plan/delta`.
  - `item/commandExecution/outputDelta`: fixed label "Running a command"; the output is dropped.
  - `item/started` and `item/completed`: the full `item` goes to the callback.
  - `error`: `provider_error`.
- Final text is concatenated from `agentMessage` items and deltas (`:201-224`).

**Server requests** (`_server_request`, `:582-620`). Nothing is approved automatically:

| Request method | Response |
|---|---|
| `item/commandExecution/requestApproval`, `item/fileChange/requestApproval` | `{"decision":"cancel"}` |
| `applyPatchApproval`, `execCommandApproval` | `{"decision":"abort"}` |
| `item/permissions/requestApproval` | `{"permissions":{},"scope":"turn"}` |
| `item/tool/requestUserInput` | `{"answers":{}}` |
| `mcpServer/elicitation/request` | `{"action":"cancel","content":null}` |
| anything else | error `-32601` |

After responding, every candidate handle gets `needs_attention` and a background `turn/interrupt` (`:622-659`). If the request has **no `threadId`**, the candidates are **all active turns on that server**. For example, `account/chatgptAuthTokens/refresh` interrupts every turn on the shared server (test `:548-563`). Approvals that arrive before the acknowledgement are answered immediately, then bound to the acknowledged turn (test `:666-712`).

**Death and idle**
- `_mark_dead` fails all pending requests and marks every handle uncertain (`:661-672`).
- `close_idle` refuses while any turn is active. Otherwise it closes stdin, waits 2 s, then calls `terminate()` (`:674-689`).

**Manager idempotency**
- Fingerprint is `sha256([home, params])`. The same `message_id` with the same fingerprint returns the same handle; a different fingerprint raises `PreCommitError` (`:810-821`).
- `_handles` is never pruned.

## 5. `native_dispatch`: what v2 must NOT port

**Account picking** (`_lane`, `:68-82`) — **do not port**
- Uses an in-process `_leases[model] = (monotonic, email, token)` with a 600 s TTL.
- Otherwise it calls `tickle.probe_lane`, which:
  - ranks lanes by shelling out to `subfleet pick claude --json --all --model` (`tickle.py:610-620`);
  - reads tokens with `agent-secret get claude-quota-<email>` (`:599-607`);
  - runs a live probe, `claude -p "Reply with exactly: ok" --model <m> --strict-mcp-config`, capped at 15 s per lane and 45 s in total;
  - writes a 600 s disk cache (`:584-586`, `:648-721`).
- This is a parallel scheduler and credential authority. v2 has its own `picker.py`, `capacity.py` and `credentials.py`; I did not read their mechanics.

**Credential cache** — **do not port**: `_leases` (plaintext tokens in memory) plus the token in the `ClaudeWorker` env.

**Worker launch and registry** (`_claude_worker`, `:111-149`) — **do not port** as authority
- Reuses a worker if it is alive and `native_id/model/permission_mode/cwd` are unchanged; otherwise raises `PreCommitError`.
- Refuses when a foreign live Claude instance exists (`catalog._live_claude_instances`).
- Evicts idle workers beyond 8 or idle for more than 600 s (`:127-135`).
- Probes the lane, rechecks liveness, spawns, then runs `_guard_claude_handoff` (a 1 s window, `session_catalog.py:41`, `:1784-1810`).
- Writes `native-workers.json` (`:85-91`).
- For Codex, the "account" is simply the row's `profile.home_path` (`:756-757`).

**Account failover** (`_try_failover`, `:595-700`) — **do not port** as-is
- Claude only, triggered by `completed ok:false`.
- Requires a transcript hard-limit attestation bound to the exact native user uuid (`catalog._limit_attestation`). `_explicit_claude_limit` means `error=="rate_limit"` or `quotaLimits.status=="rejected"` (`session_catalog.py:320-326`).
- Clears caches, re-probes, stops the old writer, re-validates the attestation, spawns a new worker and sends `tickle.REVIVE_MESSAGE` under a **new uuid**. The original prompt is never replayed. It records `failover:{from,to,original_input_replayed:false}`.

**Live-app inbox path** (`dispatch`, `:709-730`) — legacy-only
- For an externally live Claude session it calls `catalog.continue_session`, which pushes through `notify.push_to_session`, the app's inbox socket (`session_catalog.py:2133-2150`).
- It then starts a `_watch_inbox` transcript tailer (`:255-298`, `_InboxTracker` `:174-241`). The tailer matches `origin.kind=="peer", name=="Subfleet", body==expected` and follows `parentUuid` descendants until `stop_reason=="end_turn"` or an explicit limit.

**Codex admission gate** (`session_catalog.py:1234-1244`)
- Blocks `managed` sandboxes, unknown sandboxes, and **any approval mode other than `never`**.
- The legacy Codex path is therefore only ever exercised with `approvalPolicy:"never"`. It passes only `sandbox_policy={"type": row["sandbox_mode"]}` (`native_dispatch.py:768`). The transport can carry writable roots and network settings, but they are not sourced from the rollout.
- The doc's "preserves saved sandbox/approval policy" is narrower in practice.

**`_RunObserver`** (`:301-593`): observation logic. Port its semantics, not its v1 ledger.
- Creates a v1 run-ledger entry with `routing_decision={kind:"session-continuation",session_id,native_id,provider,transport,message_id,broker_pid,service_tier}` and a Codex binding of `codex_thread_id` and `rollout_path` (`:319-339`).
- Writes streamed text to `out.md` (`:412-415`).
- Maps events to allowlisted activity labels (`:436-452`) with 0.25 s throttling and semantic de-duplication (`:352-379`). Activity goes through a bounded 512-slot queue with a writer thread that drops on overflow (`:24-57`).
- Persists commentary text and reasoning summaries as activity (`:471-506`). Those are model-authored text, so they need a privacy decision in v2.
- A model mismatch sets rc=1 and interrupts the worker (`:528-538`). A late completion removes `delivery_uncertain` (`:580-581`).

## 6. Failure and ambiguity handling (`delivery-unknown`)

Exceptions carry `delivery_started`: `False` for pre-commit (`claude_transport.py:22-23`, `codex_transport.py:28-35`) and `True` for uncertain delivery (`:26-27`, `:38-41`). The broker treats **anything not explicitly `False`** as ambiguous, including a missing attribute (`broker.py:256-260`).

**Sources of `delivery-unknown`**
- Broker start or stop while a row is `starting` (`outbox.py:304-310`).
- A dispatcher exception that is not pre-commit, or an invalid receipt status (`outbox.py:348-350`).
- Claude:
  - a stdin write failure (`claude_transport.py:143-147`);
  - EOF without `result` (`:246-248`).
- Codex:
  - a write failure while holding a turn handle (`codex_transport.py:335-338`);
  - an acknowledgement timeout, default 30 s. The pending entry is **kept** so a late response can bind (`:359-367`);
  - an error or invalid response to `turn/start` (`:379-387`);
  - a missing turn id (`:460-462`);
  - server death (`:661-672`);
  - an unconfirmed interrupt after an approval request (`:652-659`).
- Monitor (`broker.py:45-78`):
  - the run record is missing;
  - `delivery_uncertain` is set;
  - `needs_attention` is set. **Approval-needed is folded into `delivery-unknown`**; there is no distinct state;
  - `broker_pid != os.getpid()` for a warm transport, i.e. the broker restarted. A live child PID is not trusted;
  - `ORPHANED` (the ledger's pid is dead and the run is unfinished; `run_ledger.py:979-980`).
- A live delivery with no `run_id` never completes by itself (`broker.py:47-50`).

**Reconciliation**
- A late authoritative ledger completion (`FINISHED`, excluding the historical `rc==75` + `delivery_uncertain` case) moves `delivery-unknown` with a run to `finished` or `error` and releases the session queue (`outbox.py:374-375`; test `test_broker.py:310-344`).
- A Codex late acknowledgement plus completion completes the same handle (test `test_codex_transport.py:265-289`).
- Otherwise the only release is manual: `resolve handled confirm:true`, which never replays.

**Outcomes and gaps**
- A Codex approval request (cancel, then interrupt) ends with `turn/completed status:interrupted`, then rc=1, then outbox `error`. The `needs_attention` reason is lost to the `FINISHED` check order (`broker.py:60-64` runs before `:67`).
- Warm workers started with `start_new_session` survive broker death and keep their token env. v2 must prevent that ("no stale account privileges").

## 7. Tests worth porting as contract tests

**Admission and idempotency**
- `test_outbox.py:21-32`: durable, idempotent receipt; different content is rejected.
- `test_outbox.py:35-41`, `:62-70`: parallel duplicate IDs commit once and create one attachment bundle.
- `test_broker.py:171-188`: a lost acknowledgement plus a retried ID dispatches once.
- `test_broker.py:191-208`: a failure after COMMIT never claims non-admission.
- `test_broker.py:413-426`: missing ID, ID conflict and bad JSON are explicit non-admissions; the socket is 0600.

**Attachments and file safety**
- `test_outbox.py:44-59`: snapshot permissions and survival after the original is deleted.
- `test_outbox.py:73-98`: invalid input, symlinks and FIFOs are rejected with no commit.
- `test_outbox.py:168-185`: WAL sidecars are 0600; a database symlink is not followed.

**Ordering and serialization**
- `test_outbox.py:101-114`, `test_broker.py:211-233`: one message at a time per session, concurrency across sessions.
- `test_native_dispatch.py:127-139`: separate session locks do not serialize each other.

**Ambiguity and restart**
- `test_outbox.py:117-133`, `test_broker.py:367-391`: an in-flight message becomes a barrier on restart; queued messages survive; nothing is replayed.
- `test_broker.py:236-270`: an ambiguous exception blocks the session; resolve requires strict confirmation and releases without replay.
- `test_broker.py:273-286`: a known pre-commit failure is a terminal `error`.
- `test_broker.py:310-354`: a late completion releases the barrier once; rc 75 is not authoritative.
- `test_broker.py:357-364`: a live delivery never falsely completes.
- `test_broker.py:394-410`: a second owner cannot recover the first owner's `starting` rows.
- `test_broker.py:466-479`: after a broker (daemon) restart, the new process cannot claim to observe an old warm run.
- `test_outbox.py:150-165`: an unknown row without a run stays manual; repeated checks cause no activity churn.

**Responsiveness**
- `test_broker.py:66-84`, `:429-448`: acknowledgement under 100 ms with dispatch still in progress; prepare does not block; interrupt targets one message.

**Claude transport**
- `test_claude_transport.py:53-95`: process reuse; model and permission-mode argv; token only in env; one turn at a time; a broken pipe is uncertain and not retried.
- `test_claude_transport.py:109-124`: plan-mode isolation; `control_request` is never approved.
- `test_claude_transport.py:127-176`: no raw reasoning or tool input leaks.

**Codex transport**
- `test_codex_transport.py:186-243`: server and thread reuse; completion before the acknowledgement; idempotent dispatch; active-thread refusal; interrupt without killing the server.
- `:246-289`: an ambiguous commit keeps the lease; late reconcile.
- `:292-365`: exact model, effort, images, policy and tier, with tier independent of model and effort.
- `:368-390`: per-home isolation; API keys are not inherited.
- `:449-504`: summaries are forwarded, raw reasoning and command output are not.
- `:507-563`: approvals are never auto-accepted; unknown requests get `-32601`.
- `:566-594`: policies fail closed before process start.
- `:597-607`: an observer crash does not lose completion.
- `:610-712`: stale turn events and approvals are ignored; early approvals bind to the acknowledged turn.
- `:715-739`: final text can come from the acknowledgement alone.

**Observer, activity and binding**
- `test_native_dispatch.py:287-369`: the activity sidecar never persists raw payloads; callbacks never block on persistence; thinking de-duplication.
- `test_native_dispatch.py:372-381`: a late result reconciles the observer.
- `test_native_dispatch.py:142-177`: failover never replays input. Port only as an invariant under v2 handoff.
- `test_run_ledger_native_codex.py` (all 12 tests): an explicit thread/rollout binding is UUID-validated and profile-scoped; symlink escapes and relative paths are rejected; changing the profile invalidates the binding.

## 8. Behaviours to port into a v2 daemon-owned conversation service

1. **Client-UUID idempotent submit** (`outbox.py:237-289`). Add it as a new v2 op with capability discovery.
   - Adapt: include attachment **byte** digests and model, effort, tier and permission settings in the compared digest; the legacy `payload_digest` is dead.
   - Do not echo full prompts in every list reply.
2. **Durable intent before dispatch, with fsync and private permissions** (`outbox.py:108-138`, `:185-235`). Move the table into the v2 store and schema. Keep WAL and `synchronous=FULL`, `O_NOFOLLOW` and the attachment snapshot rules.
3. **State set.** Split legacy `delivery-unknown` into `approval-needed` and `delivery-unknown`. Add `admission-waiting` and `running`, which are distinct from `starting`/`dispatched`. `cancelled` via `resolve` should carry an explicit `resolution`.
4. **Per-conversation serialization in the claim query** (`outbox.py:334-337`). Replace the v1 flock and run-ledger scan with v2 job, attempt and session ownership. Claims must go through v2 admission and reservations, not a broker thread pool.
5. **Recovery barrier** (`outbox.py:304-310`, `broker.py:71-75`). On daemon start, any turn owned by a previous daemon incarnation becomes `delivery-unknown`. Use v2 boot identity instead of `broker_pid`, and never trust a live PID.
6. **Late authoritative reconciliation** (`outbox.py:374-375`). Key it on v2 attempt, result and attestation rather than `run_ledger._row`. Keep the "no replay" rule and the one-time release.
7. **Explicit human resolve** requiring strict `confirm:true` (`broker.py:198-203`). Scope it to the exact message, and record who resolved it and when.
8. **Cancel only while queued; Stop per turn** (`outbox.py:297`, `codex_transport.py:155-166`).
   - Codex: `turn/interrupt` with the bound `turnId` must never touch the shared server.
   - Claude: replace SIGTERM of the process group with a control-protocol interrupt, if the 2.1.280 CLI supports one (UNVERIFIED). Stop must produce an `interrupted` receipt, not `delivery-unknown`.
9. **Claude warm worker** (`claude_transport.py:50-276`). Keep the stream-json input/output, the `--replay-user-messages` acceptance echo keyed on `uuid`, base64 image blocks, the plan-mode isolation flags and env, and the model-mismatch stop.
   - Adapt: get the lane and credential from v2 per turn or with bounded lease expiry. Add `--effort`. Run under a v2 guard and containment.
   - Handle `system/init` (served-model verification), `rate_limit_event` and `result.subtype` errors through v2's `claude_stream.py` classifier (`v2:subfleet/adapters/claude_stream.py:1-35`).
   - Replace the blanket error `control_response` with a real approval round-trip (the `--permission-prompts host` default).
10. **Codex app-server transport** (`codex_transport.py:242-883`). v2 currently uses app-server only for probes (`v2:subfleet/adapters/isolation.py:55-68`, `v2:subfleet/guard/preflight.py:453-456`); its Codex adapter runs `codex exec --json` (`v2:subfleet/adapters/codex.py:425`).
    - Port: the handshake, resume-once, the thread-id and active-status checks, the `turn/start` field set, early event and approval buffering bound to the authoritative turn id, the retired-turn filter, ambiguous-acknowledgement retention, and fail-closed policy normalization.
    - Adapt: verify field names against the installed 0.153.3 schema. Add `thread/start` for new conversations. Scope approval requests without a `threadId` so they cannot interrupt unrelated turns. Prune `_handles`.
11. **Approval handling.** Replace auto-cancel with a pending approvals list and a respond op scoped to `(conversation, turn, request id, method)`. Map responses to the same Codex result shapes listed in section 4. Stale or duplicate replies must be rejected.
12. **Content-minimal activity stream** (`native_dispatch.py:352-506`, `tool_activity_label`). Keep the allowlisted labels, throttling and de-duplication, bounded queues with drop-on-overflow, and "observer failure never changes truth". Expose it through a v2 cursor events API instead of the `activity.json` sidecar polling. Decide explicitly whether commentary and reasoning summaries are shown.
13. **Native identity binding** (`native_dispatch.py:337-339`; `test_run_ledger_native_codex.py`). Store the Codex thread and rollout, and the Claude native session id, on the v2 attempt, with the same validation.
14. **Single-writer guards against external apps** (`native_dispatch.py:119-126`, `:758-760`). Port them as v2 session-ownership checks. The live-inbox path and inbox transcript tailer (`:174-298`) are legacy-only unless an explicit external-writer mode is designed.
15. **Hard-limit handoff without replay** (`native_dispatch.py:595-700`). Port only the invariant: an exact-uuid attestation, a quiesced old writer, and never replaying the original prompt. Execute it as a v2 handoff through v2 admission and credentials.