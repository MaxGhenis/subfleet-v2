# Desktop workspace: design

Status: Stage 1 contract, 2026-09-24. Binding clauses are C-24 to C-30 in
`docs/acceptance-contract.md`; this document is the specification those
clauses cite. Transition plan: `~/subfleet-desktop-transition-20260924.md`.
Code maps of the base revision (`3f155e5`) that this design is built on are in
`docs/desktop/maps/`; citations of the form `file.py:N` refer to that revision.

## 1. Outcome

Max starts, follows up on, approves or denies, stops, inspects, and reopens
real Claude and Codex coding conversations in the installed Subfleet app,
across all enrolled subscription accounts, without switching to the Claude or
Codex apps. The native Swift cockpit built on 2026-08-30 is recovered and
pointed at the v2 daemon. The daemon stays the only execution authority.

What Max asked of the app (archived Codex task `01a04752…`, 2026-08-28 to
2026-09-01), each a ledger row in `docs/desktop/ledger.json`:

- replace the Claude and Codex apps; no more signing in and out of accounts;
  a conversation moves to an account with capacity;
- existing Claude and Codex sessions appear under their display names, and
  clicking one opens it and lets him continue it;
- a normal macOS app (traffic lights, Dock, menus, not an overlay); the menu
  bar bolt stays a quota popover with an obvious way to open the app;
- Return sends; screenshots and images paste into the composer; the composer
  stays editable while a turn runs; sending is as quick as the native apps;
- the app shows what the agent is thinking and doing, live;
- Markdown renders;
- Fast is its own setting, independent of model and reasoning effort;
- per-account usage: percent used, reset times, weekly reset, and the Fable,
  overall and five-hour windows.

## 2. Decisions

Each decision names what it rests on. "Verified" means observed this session
by running the installed tool or reading the code; "doc" means an official
document; anything else is marked.

**D-1. A turn is a job.** Every conversation turn is one v2 job of kind
`turn`, admitted like any job (C-6.3, C-11), with exactly one attempt, one
guardian, and one provider process that lives for that turn only. This keeps
C-23.54 (every provider launch is a submission), C-5.1 (one guardian per
attempt), per-turn credential resolution, closures, the Fable reserve, and
attestation. The provider process runs in its bidirectional mode (Claude
stream-json, Codex app-server), not the one-shot mode v2 uses today, so the
daemon can stream output, answer approvals and interrupt the turn. The daemon
closes the process's stdin after the provider's terminal event for the turn,
and the process exits. Verified: `codex app-server` 0.153.3 exits with rc 0
0.02 s after stdin EOF and answers `initialize` 0.26 s after spawn; Claude
2.1.280 with `--input-format stream-json` exits with rc 0 on stdin EOF after
answering an SDK `initialize` control request. The exit receipt still defines
completion of the attempt; the provider's terminal event defines completion
of the turn (D-9).

*Rejected for this release: warm workers.* A process that outlives its turn
holds a lane slot while idle, resolves its credential once, escapes per-turn
admission, and needs per-turn receipts that the guardian cannot give
(`maps/v2-store-jobs.md` §7). The first release measures per-turn start
latency (Stage 3) and states it; a warm worker is a later change with its own
contract section.

**D-2. The guardian gains a control relay.** For a turn attempt the guardian
owns the child's stdin as a pipe and listens on a private Unix socket. The
daemon sends numbered frames; the guardian appends each accepted frame to
`<attempt>/stdin.jsonl` (fsync) before writing it to the pipe, and
acknowledges by sequence number. A resent frame whose number was already
applied is acknowledged without being written again, so a daemon restart
mid-send never duplicates provider input. The guardian does not parse
provider messages. Section 6.

**D-3. Turn drivers are pure and replayable.** A driver per provider turns
(stdout lines, relay acknowledgements, operator commands) into (events,
outgoing frames, a turn outcome). It holds no I/O. The daemon feeds it by
tailing `<attempt>/stdout` from a persisted byte offset. After a daemon
restart the driver is rebuilt by replaying `stdin.jsonl` and `stdout` from
the start of the attempt; events carry their source offsets, so replay
appends nothing twice. Section 7.

**D-4. Conversations are daemon rows; history is the native transcript.** A
conversation row binds a provider, a native session id (once known), a
workspace, settings, and (for Codex) a lane home. Long-term history is read
from the native transcript (Claude `~/.claude/projects/<slug>/<id>.jsonl`,
Codex `<home>/sessions/**/rollout-*-<id>.jsonl`), which also records every
turn Subfleet runs. Daemon events cover the live turn and are compacted after
it ends. Nothing Subfleet does rewrites a native transcript.

**D-5. Claude conversations move between accounts; Codex conversations stay
in their home.** Every enrolled Claude lane launches with the daemon user's
default config directory and passes only `CLAUDE_CODE_OAUTH_TOKEN`
(`maps/v2-claude-adapter.md` §1; all 17 Claude lanes are `keychain-token`,
observed in `lanes.json`), so a Claude transcript can be resumed under any
Claude lane. Admission therefore routes each Claude turn by model across the
eligible Claude lanes, which is the account failover Max asked for. A Codex
thread lives in one `CODEX_HOME`, so a Codex conversation is pinned to the
lane that holds its thread and waits for that lane's capacity; moving it is
an explicit, labelled handoff (D-15).

**D-6. One writer per native session, whichever lane.** A turn job takes the
lease `conversation:<conversation id>` and the lane-independent lease
`native:<provider>:<native session id>`, and is refused while any existing
`native-session:<lane>:<id>` lease (resume, revive) or a live process outside
Subfleet holds the same session. For Claude, "outside Subfleet" is a live
pid in `~/.claude/sessions/<pid>.json` naming the session id
(`sessions/registry.py`); for Codex, a live `codex` process whose environment
names the lane home and whose rollout is being appended within the last 10 s
(the lane homes are Subfleet's; an external writer there is a bug, reported,
not waited on). The app shows "open in the Claude app; close it there to
continue here" rather than failing silently.

**D-7. Permission policy is per conversation, explicit, and never widened
silently.** The policy is part of every message's settings and its digest.

| Setting | Claude flags | Codex `turn/start` |
|---|---|---|
| `ask` | `--permission-mode default --permission-prompt-tool stdio` | `approvalPolicy:"on-request"`, `approvalsReviewer:"user"`, `sandboxPolicy:{type:"workspaceWrite", networkAccess:false}` |
| `accept-edits` | `--permission-mode acceptEdits --permission-prompt-tool stdio` | same as `ask` (Codex has no separate edit class) |
| `bypass` | `--permission-mode bypassPermissions` | `approvalPolicy:"never"`, `sandboxPolicy:{type:"workspaceWrite", networkAccess:false}` (today's v2 posture) |
| `read-only` | the v2 read-only flag set (`claude.py:1211-1225`) | `approvalPolicy:"never"`, `sandboxPolicy:{type:"readOnly", networkAccess:false}` |

Codex never receives `dangerFullAccess`, `externalSandbox`, or a granular or
experimental policy. The never-rules deny hooks keep applying: Claude loads
user settings in every mode except `read-only` (as today); every Codex turn
server starts with the verified `-c hooks=` override (D-11). A new
conversation's default is the provider's configured default (Claude
`permissions.defaultMode` in `~/.claude/settings.json`; Codex `bypass`, which
is v2's current behaviour), shown in the composer. Continuing an existing
native session defaults to the permission mode its transcript last recorded
(`transcripts.last_permission_mode`). Any change is a person's click, and a
change to a wider policy on an existing conversation asks for confirmation.

**D-8. Displayed reasoning is what the provider's own app displays.** Max
asked for "what it's thinking, like the Claude app." Claude `thinking` block
text and Codex `item/reasoning/summaryTextDelta` summaries are events. Codex
raw reasoning (`item/reasoning/textDelta`), Claude `redacted_thinking`, and
signatures are never stored or sent. Tool activity carries the tool name, a
scrubbed input summary of at most 500 characters, and a scrubbed result
preview of at most 2 KB; a call that reads credentials
(`handoff.sensitive_tool_call`) is shown as "credential access (hidden)" with
neither input nor result. Scrubbing reuses `handoff.scrub_secrets`.

**D-9. States are honest and separate.** Local acknowledgement, provider
acceptance, and turn completion are three facts, never one. Message states:

| State | Meaning |
|---|---|
| `queued` | Durably accepted by the daemon; waiting behind the conversation's current turn |
| `waiting` | Its turn job waits for admission (reason: capacity, lease-held, workspace, route) |
| `starting` | An attempt is reserved or its provider process is starting; the provider has not acknowledged the message |
| `running` | The provider acknowledged the message (Claude replayed user message with our uuid; Codex `turn/start` response with a turn id) |
| `approval-needed` | Running, with at least one unanswered approval request |
| `complete` | The provider reported the turn finished successfully |
| `failed` | The provider reported failure, or the turn could not start and the message provably never reached the provider (reason recorded) |
| `interrupted` | Stopped by the person; the provider confirmed or the process ended after an interrupt request |
| `cancelled` | Withdrawn while `queued` or `waiting`, before any provider saw it |
| `delivery-unknown` | The provider may have received the message and no terminal evidence exists; blocks the conversation until reconciled |

**D-10. No automatic replay after ambiguity.** A message whose delivery is
uncertain is never sent again automatically. Reconciliation reads the native
transcript: Claude records the user message with the uuid Subfleet assigned
(Stage 3 verifies this; until then the Claude path treats absence as
ambiguous), and Codex records `clientId` on the `userMessage` item
(`README@rust-v0.153.3:222`). Present → the message was delivered and its
turn ended without a terminal event (`interrupted` if a stop was requested,
else `failed` with `ended-without-result`). Absent after the process is
verified gone → `failed` with `not-delivered`, and the person may resend
(a new message id). Unreadable or still ambiguous → `delivery-unknown` until
the person resolves it (`message.resolve`, strict `confirm:true`).

**D-11. The Codex guard covers the turn server.** Each Codex turn process is
`codex app-server --listen stdio:// -c <verified override>` launched from
`PreflightResult.executable` after the ordinary preflight (C-14.2) passes for
this lane and cwd. Before `thread/start` or `thread/resume` the driver calls
`hooks/list {cwds:[cwd]}` on the same server and requires exactly the checks
`guard/preflight.py:632-648` makes; any difference ends the turn as `failed`
with `guard-refused` before the message is sent. Server requests for
credentials (`account/chatgptAuthTokens/refresh`, `attestation/generate`) and
unknown methods get JSON-RPC error -32601 and change nothing else. The
experimental API is never negotiated (`capabilities.experimentalApi` absent).

**D-12. Pre-acceptance limits reroute without replay.** A Claude turn that
ends with a rate-limit rejection or credit error before the provider echoed
the user message, or a Codex `turn/start` that fails with a limit before a
turn id exists, provably did not consume the message. The message returns to
`waiting`, the lane's closure is recorded as today (C-9.6), and admission
places the next attempt elsewhere (a new job; the old one keeps its
evidence). After acceptance, a limit ends the turn as `failed` (`limited`);
the app offers "continue on another account", which sends a new message with
a new id and never re-sends the old one.

**D-13. Turns skip admission probes and headless framing.** The turn itself
is the capacity test (D-12), so admission does not run its pre-launch probe
(`daemon.py:2213-2275`) for turn jobs, and turn prompts carry neither
`HEADLESS_BLOCK` (`claude.py:122-134`) nor the write preamble. Turn jobs are
excluded from `_lane_session_ids` (`daemon.py:1565-1580`), so a conversation's
native session stays listable and continuable.

**D-14. Attended turns order ahead, never reserve.** Turn jobs sort ahead of
detached jobs of the same tier in admission ordering (`scheduler.ordered_jobs`)
and are exempt from `behind-older-job` holds by detached jobs. They get no
reserved capacity and bypass no rule in `evaluate`. Plan amendment 11 declined
an interactive allowance; ordering is not an allowance, and the change is
recorded for Max as a decision.

**D-15. Handoffs are labelled.** Moving a conversation to the other provider,
or out of a Codex home it cannot run in, dispatches a new conversation whose
first message is a continuity brief built by `sessions/handoff.py` (scrubbed,
bounded). The new conversation records `handoff_from` (provider, native id,
transcript path, brief sha256). The app labels it "handoff from …", never
"continued".

**D-16. Workspace.** A conversation is bound to one workspace directory for
its life: an existing session's recorded cwd, or for a new conversation a
directory the person picks, with a default of a new git worktree when the
directory is a repository. Turns run in place there and hold the lease
`worktree:<git toplevel or directory>`. Attended turns are not dispatched
jobs, so two dispatched-job guards change for them: a directory outside git
is allowed (salvage is skipped and the receipt says so), and a checkout on
`main` or `master` requires the conversation's explicit `allow_main`, set by a
person when the conversation is created (C-13.2 keeps binding every
dispatched job).

**D-17. The app is a client of an explicit endpoint.** The app talks to
`<SUBFLEET_HOME or ~/.subfleet>/daemon.sock` using protocol v1 and the new
ops, after a `capabilities` check. It never runs `~/.local/bin/subfleet-local`,
never reads `~/chief-of-staff/state/subfleet`, never starts a broker, and
never falls back to a v1 path. A development build uses bundle id
`org.maxghenis.subfleet.dev` and requires `SUBFLEET_HOME` to name a state root
other than `~/.subfleet`.

**D-18. Catalog runs out of process.** Discovery of existing native sessions
scans large trees (35 GB of Claude transcripts, 17,825 Codex rollouts;
`maps/v2-sessions.md` §1). It runs as a separate short-lived process
(`python -m subfleet.conversations.catalog`) that the daemon starts on a timer
and on request, with a wall-clock cap, writing `catalog.json` atomically. The
daemon never scans those trees on a request thread or its control thread.

## 3. Data model

Migration 6 (`store.py` `MIGRATIONS`), additive; `SCHEMA_VERSION = 6`.
Every row change is one transaction with its `events` row (C-3.2).

```sql
CREATE TABLE conversations (
  conversation_id  TEXT PRIMARY KEY,          -- "cv-" + 26-char ULID
  provider         TEXT NOT NULL CHECK (provider IN ('claude','codex')),
  native_session_id TEXT,                     -- Claude session uuid / Codex thread id; NULL until known
  title            TEXT,
  workspace        TEXT NOT NULL,             -- absolute directory
  workspace_kind   TEXT NOT NULL CHECK (workspace_kind IN ('in-place','worktree')),
  allow_main       INTEGER NOT NULL DEFAULT 0,
  lane_id          TEXT REFERENCES lanes(lane_id), -- Codex: the home holding the thread; Claude: NULL
  settings_json    TEXT NOT NULL,             -- {model, effort, fast, permission}
  origin           TEXT NOT NULL CHECK (origin IN ('new','native','handoff','legacy')),
  handoff_from_json TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  archived_at      TEXT,
  UNIQUE (provider, native_session_id)
);

CREATE TABLE messages (
  message_id       TEXT PRIMARY KEY,          -- client UUID, canonical lowercase
  conversation_id  TEXT NOT NULL REFERENCES conversations(conversation_id),
  seq              INTEGER NOT NULL,          -- order within the conversation
  digest           TEXT NOT NULL,             -- sha256 over canonical {conversation_id, text, attachment sha256s, settings}
  text_path        TEXT NOT NULL,             -- <state>/conversations/<cv>/messages/<message id>.md, 0600
  attachments_json TEXT NOT NULL,             -- [{"sha256","media_type","bytes"}]
  settings_json    TEXT NOT NULL,
  state            TEXT NOT NULL,             -- D-9
  state_reason     TEXT,
  job_id           TEXT REFERENCES jobs(job_id),  -- latest turn job
  turn_ref         TEXT,                      -- Claude user message uuid / Codex turn id
  resolution_json  TEXT,                      -- who/when/how a delivery-unknown was resolved
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  UNIQUE (conversation_id, seq)
);

CREATE TABLE approvals (
  approval_id      TEXT PRIMARY KEY,          -- "ap-" + ULID
  message_id       TEXT NOT NULL REFERENCES messages(message_id),
  attempt_id       TEXT NOT NULL REFERENCES attempts(attempt_id),
  provider_request_id TEXT NOT NULL,          -- Claude control request_id / Codex JSON-RPC id
  kind             TEXT NOT NULL,             -- tool | command | file-change | permissions
  summary_json     TEXT NOT NULL,             -- scrubbed display fields
  options_json     TEXT NOT NULL,             -- decisions the provider allows
  nonce            TEXT NOT NULL,             -- 128-bit random, returned to the client, required to respond
  state            TEXT NOT NULL CHECK (state IN ('pending','answered','expired','withdrawn')),
  decision_json    TEXT,
  created_at       TEXT NOT NULL,
  answered_at      TEXT,
  UNIQUE (attempt_id, provider_request_id)
);

CREATE TABLE attachments (
  sha256           TEXT PRIMARY KEY,
  media_type       TEXT NOT NULL CHECK (media_type IN ('image/png','image/jpeg','image/gif','image/webp')),
  bytes            INTEGER NOT NULL,
  path             TEXT NOT NULL,             -- <state>/attachments/<sha256>.<ext>, 0600
  created_at       TEXT NOT NULL,
  last_used_at     TEXT NOT NULL
);

CREATE TABLE conversation_events (
  seq              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id  TEXT NOT NULL REFERENCES conversations(conversation_id),
  message_id       TEXT,
  attempt_id       TEXT,
  source_offset    INTEGER,                   -- byte offset in the attempt stdout that produced it
  kind             TEXT NOT NULL,
  data_json        TEXT NOT NULL,             -- whitelisted fields only; at most 64 KiB
  ts               TEXT NOT NULL,
  UNIQUE (attempt_id, source_offset, kind)
);
CREATE INDEX conversation_events_by_conversation ON conversation_events(conversation_id, seq);
```

`jobs` gains nothing: a turn job is `kind='turn'`, `in_place=1`,
`independent=1`, `max_attempts=1`, `name='turn-<conversation id>'`, and its
`manifest.json` carries `{"turn": {conversation_id, message_id, provider,
native_session_id, lane_id, settings, attachments}}`. Turn prompts live in
`jobs/<id>/prompt.md` as today (C-23.1).

`C-2.2` gains `conversations/`, `attachments/`, `catalog.json` and `run/`
(relay sockets). Files 0600, directories 0700 (C-2.3).

## 4. State machine

```
submit ──► queued ──(no earlier live message)──► waiting ──(attempt reserved)──► starting
queued|waiting ──(message.cancel)──► cancelled
starting ──(provider ack)──► running ◄──► approval-needed
starting ──(proven not delivered: spawn error, guard refused, pre-acceptance limit)──►
          waiting (reroutable, D-12)  |  failed (not reroutable)
running|approval-needed ──(terminal success)──► complete
running|approval-needed ──(terminal failure)──► failed
running|approval-needed ──(stop requested, then terminal or exit)──► interrupted
starting|running|approval-needed ──(exit without terminal event)──► reconcile (D-10)
reconcile ──► failed | interrupted | delivery-unknown
delivery-unknown ──(message.resolve confirm:true)──► failed (resolution recorded)
```

One turn per conversation at a time: the dispatcher submits the turn job for
the lowest-seq `queued` message only when no message of the conversation is in
`waiting`, `starting`, `running`, `approval-needed` or `delivery-unknown`.
Independent conversations run concurrently, limited only by admission.

The dispatcher runs on the control loop's worker pool, never on a request
thread. Submitting the turn job uses the ordinary `submit` path internally
(C-23.54), with `request_id = "turn:" + message_id + ":" + n` where `n` counts
reroutes, so a daemon crash between persisting the message and submitting its
job resubmits idempotently.

## 5. Wire protocol additions

All new ops are protocol `v: 1` (C-16.1). `PROTOCOL_VERSION` does not change.
Errors use `Exit` codes and `ok:false` (never the gate style). Handlers only
validate, write one transaction, call `_notify()` and return; none waits on a
provider, a probe, `ps`, or git.

### `capabilities`

Request `{}`. Result:

```json
{"protocol": 1, "daemon_version": "2.1.0", "schema_version": 6,
 "capabilities": ["conversations.v1", "events.v1", "approvals.v1", "attachments.v1", "catalog.v1"],
 "limits": {"message_bytes": 1048576, "attachment_bytes": 20971520, "attachments_per_message": 8,
            "events_page_bytes": 262144, "events_wait_s": 50}}
```

A daemon without the op answers `ok:false`, code 2, "unknown op"; the client
treats that as "no conversation support" and sends no conversation op. A
client sends a field whose silent omission would change the outcome only after
seeing the capability that defines it (C-16.2's "unknown fields are ignored").

### `conversation.list`

`{"include_catalog": true, "provider": null, "query": null, "limit": 200}` →
`{"conversations": [Conversation], "catalog": {"generated_at", "stale": bool,
"items": [CatalogItem]}}`. Catalog items already bound to a conversation are
returned once, inside the conversation.

`Conversation`: `{conversation_id, provider, native_session_id, title,
workspace, workspace_kind, lane_id, settings, origin, handoff_from,
created_at, updated_at, last_message: {message_id, state, updated_at} | null,
active: bool, blocked_by: null | "external-writer" | "delivery-unknown" |
"lane-disabled"}`.

`CatalogItem`: `{provider, native_session_id, title, title_source, cwd,
model, permission_mode, home, updated_at, live_elsewhere: bool,
continuable: bool, continue_blocker: null | string}`.

### `conversation.open`

`{"conversation_id": "cv-…"}` or `{"native": {"provider": "claude",
"session_id": "…", "home": null}}`. Opening a native session creates (once)
its conversation row with `origin:"native"`, settings from the transcript's
last model and permission mode, and the transcript's cwd as workspace
(`in-place`). Result: `{conversation, messages: [Message] (latest 50),
events_cursor: int, pending_approvals: [Approval]}`.

### `conversation.create`

`{"request_id", "provider", "workspace", "workspace_kind", "allow_main",
"title"?, "settings": {model, effort?, fast: bool, permission}}` →
`{conversation, created: bool}`. `workspace_kind:"worktree"` makes the daemon
cut a worktree from the repository at `workspace` on a new branch
`subfleet/<conversation id>` (git work runs on a worker, not the request
thread; the result says `preparing` until done). Idempotent on `request_id`.

### `conversation.history`

`{"conversation_id", "before": cursor|null, "limit": 50}` → bounded page of
the native transcript rendered as `[{role, text, ts, id, kind}]`, newest
first, with `next_before`. Reads at most 4 MiB of transcript per call, from
the tail, on a dedicated history pool; tool results are scrubbed previews
(D-8).

### `message.submit`

```json
{"conversation_id": "cv-…", "message_id": "<uuid4>", "text": "…",
 "attachments": ["<sha256>", …],
 "settings": {"model": "opus", "effort": "high", "fast": false, "permission": "bypass"}}
```

→ `Receipt`. The daemon computes the digest; a repeated `message_id` with the
same digest returns the stored receipt (`created:false`); a different digest
is exit 2 `message-id-conflict`. Unknown attachment hashes, more than 8
attachments, text over 1 MiB, or settings the provider catalog rejects
(`model`, `effort`, `fast`) are exit 2 before anything is stored.

`Receipt`: `{message_id, conversation_id, seq, state, state_reason, job_id,
created: bool, settings, served: {lane_id, account_label, model, effort,
fast_state} | null, updated_at}`.

### `message.status`

`{"message_ids": [...]}` → `{"messages": [Receipt]}`.

### `message.cancel`

`{"message_id"}` → Receipt. Allowed in `queued` and `waiting` only; anything
later is exit 2 with the fix "use turn.interrupt".

### `turn.interrupt`

`{"message_id"}` → Receipt. Allowed in `starting`, `running`,
`approval-needed`. Records the request; the driver sends Claude
`control_request {subtype:"interrupt"}` or Codex `turn/interrupt {threadId,
turnId}`. If no terminal event arrives within 20 s the attempt is killed
through `_kill_attempt` (C-5.4) and the message becomes `interrupted` or
reconciles (D-10). It never touches another conversation's process.

### `message.resolve`

`{"message_id", "resolution": "not-delivered" | "delivered", "confirm": true}`
→ Receipt. Only from `delivery-unknown`; `confirm` must be the JSON literal
`true`. Records who (client pid/uid) and when. Never resends.

### `conversation.events`

`{"conversation_id", "after": seq, "limit": 500, "wait_s": 25}` →
`{"events": [Event], "next": seq, "reset": false}`. Long-polls up to
`wait_s` (at most 50, below the client's deadline) when no event is newer
than `after`. Pages are cut at 256 KiB. If `after` precedes compacted events,
`reset:true` tells the client to reload `conversation.open`.

`Event`: `{seq, message_id, kind, ts, data}` with kinds `status`,
`accepted`, `text.delta`, `text`, `thinking.delta`, `thinking`,
`tool.started`, `tool.completed`, `approval.requested`,
`approval.resolved`, `limits`, `turn.completed`, `error`, `served`.

### `approval.list` / `approval.respond`

`approval.list {"conversation_id"?}` → `{"approvals": [Approval]}` with
`Approval = {approval_id, message_id, kind, summary, options, nonce,
created_at, state}`.

`approval.respond {"approval_id", "nonce", "decision": "allow" | "allow-session"
| "deny" | "cancel-turn", "message"?}` → `{approval, receipt}`. Refused (exit
2) when the approval is not `pending`, the nonce differs, the attempt is no
longer live, or `decision` is not in `options`. Exactly one response is
written per approval (unique by state transition); a duplicate returns the
recorded decision.

### `attachment.add`

`{"path": "/abs/path.png", "sha256"?}` → `{"sha256", "media_type", "bytes"}`.
The daemon opens the path with `O_NOFOLLOW|O_NONBLOCK`, requires a regular
file owned by the daemon's uid, at most 20 MiB, and PNG, JPEG, GIF or WebP
magic bytes; it copies into `attachments/<sha256>.<ext>` (0600, fsync) and
re-hashes the copy. A supplied `sha256` must match. Runs on a dedicated pool.

### `catalog.refresh`

`{}` → `{"requested": true, "running": bool, "generated_at"}`. Starts the
catalog process unless one is running.

## 6. Guardian control relay

`python -m subfleet.guardian … --control-socket <path> -- <argv>`:

- The socket path is `<state>/run/<first 16 hex of sha256(attempt id)>.sock`
  (under the 103-byte limit; `cli.py:73`), bound before `start.json` is
  written, mode 0600 in a 0700 directory, and recorded in `start.json` as
  `control_socket`.
- The child's stdin is a pipe the guardian creates. stdout and stderr stay
  files (C-5.1).
- The guardian accepts one connection at a time, checks the peer's uid with
  `getpeereid`, and reads NDJSON frames: `{"seq": n, "op": "write", "line":
  "<one JSON line without newline>"}` or `{"seq": n, "op": "close"}`.
- For `seq <= last_applied` it replies `{"seq": n, "ok": true, "dup": true}`.
  For `seq == last_applied + 1` it appends the frame to `stdin.jsonl`, fsyncs,
  writes `line + "\n"` to the pipe (or closes it), and replies `{"seq": n,
  "ok": true}`. Any other `seq` is `{"ok": false, "error": "gap"}`. A write
  after the child closed stdin replies `{"ok": false, "error": "closed"}`.
- `last_applied` is recovered from `stdin.jsonl` if the guardian restarts its
  relay thread; the guardian never re-writes a logged frame.
- A daemon disconnect leaves the pipe open. The child exiting ends the relay;
  `exit.json` is written as today.

The daemon's relay client retries a frame on reconnect with the same `seq`,
so a crash between send and acknowledgement is safe (D-2).

## 7. Provider turn drivers

Both drivers are pure (`subfleet/conversations/claude_turn.py`,
`codex_turn.py`), built from the turn manifest and fed `(stdout_line,
offset)`, `(ack, seq)`, and operator commands (`interrupt`,
`approval_response`). Each emits events (section 5), frames, and at most one
outcome.

### Claude

argv (every flag verified in `claude --help` 2.1.280 except the hidden
`--permission-prompt-tool`, which the binary registers and accepted in the
verified probe):

```
claude -p --input-format stream-json --output-format stream-json --verbose
       --include-partial-messages --replay-user-messages
       --model <model id> [--effort <effort>]
       (--session-id <new uuid> | --resume <native session id>)
       <permission flags from D-7>
       [--settings '{"fastMode": true}']
```

Environment exactly as `ClaudeAdapter.build_launch` (credential only in
`CLAUDE_CODE_OAUTH_TOKEN`; `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`
removed; read-only removals), cwd = workspace.

Frames: (1) `{"type":"control_request","request_id":"init-1","request":{"subtype":"initialize"}}`;
(2) after the `initialize` success, the user message
`{"type":"user","uuid":"<message id>","parent_tool_use_id":null,"message":{"role":"user","content":[{"type":"text","text":…},{"type":"image","source":{"type":"base64","media_type":…,"data":…}}…]}}`;
(3) `close` after `result`.

The `initialize` response carries `account.email` (checked against the
lane's identity, C-10.6: mismatch ends the turn as `failed`, `identity`,
before the message is sent), `models[].supportedEffortLevels` (effort
validation), and `fast_mode_state` (verified in the probe). Mapping:
`type:"user"` with our uuid → `accepted`; `stream_event` text deltas →
`text.delta`; thinking deltas → `thinking.delta`; `assistant` text/thinking
blocks → `text`/`thinking`; `tool_use` → `tool.started`; `user` tool results →
`tool.completed`; `control_request` `can_use_tool` → an approval (section 8);
any other `control_request` → error `control_response` ("not supported by
Subfleet") and an `error` event; `rate_limit_event` → `limits`; `system/init`
and every `assistant.message.model` → model check (mismatch ends the turn with
`control_request interrupt` and `failed`, `model-mismatch`); `result` →
`turn.completed` and the outcome. Classification and attestation reuse
`claude_stream.py` and `ClaudeAdapter.attest` over this attempt's transcript
range, after fixing the prose-limit misclassification
(`maps/v2-claude-adapter.md` §2 "Defect") so a successful turn that mentions
a usage limit is not `limited`.

### Codex

argv: `<PreflightResult.executable> app-server --listen stdio:// -c <override>`
with `CODEX_HOME=<lane home>` and API keys removed (as `codex.py:443-450`).

Frames: `initialize {"clientInfo":{"name":"subfleet","title":"Subfleet","version":…}}`,
`initialized`, `hooks/list {cwds:[workspace]}` (D-11), `model/list {}`
(settings validation: effort in `supportedReasoningEfforts`, Fast only if
`serviceTiers` lists `priority`), then `thread/start {cwd, model, sandbox,
approvalPolicy, approvalsReviewer:"user", serviceTier}` for a new
conversation or `thread/resume {threadId, cwd, model, approvalPolicy,
approvalsReviewer, sandbox, serviceTier, excludeTurns:true}`, then
`turn/start {threadId, clientUserMessageId:<message id>, input:[{type:"text",
text}, {type:"localImage", path:<attachment copy>}…], model, effort,
serviceTier: "priority" | null, approvalPolicy, approvalsReviewer:"user",
sandboxPolicy, cwd}`; `turn/interrupt` on request; `close` after
`turn/completed`. `thread/start` with a writable sandbox writes project trust
into the lane's `config.toml` (README:174); the guard cache fingerprint
includes `config.toml`, so the next preflight re-probes, which is correct.

Mapping: `turn/start` response with `turn.id` → `accepted`;
`item/agentMessage/delta` → `text.delta`; `item/reasoning/summaryTextDelta` →
`thinking.delta`; `item/started`/`item/completed` for `commandExecution`,
`fileChange`, `webSearch`, `mcpToolCall` → `tool.*`; `turn/diff/updated` →
a `tool.completed` "diff" preview; approval server requests → section 8;
`account/rateLimits/updated` and `thread/tokenUsage/updated` → `limits`;
`error` → `error`; `turn/completed` → `turn.completed` and the outcome.
Attestation matches `turn_context.turn_id` in the rollout to our turn id
(`maps/v2-codex-adapter.md` §2) and records `model` and `serviceTier`.

## 8. Approvals

A provider request becomes an `approvals` row, a `approval.requested` event,
and the message state `approval-needed`, in one transaction. The row stores
the provider's request id; the response frame is built only by the driver:

| Provider request | Options | Frames |
|---|---|---|
| Claude `can_use_tool` | allow, deny, cancel-turn | allow → `{"type":"control_response","response":{"subtype":"success","request_id":…,"response":{"behavior":"allow","updatedInput":<original input>}}}`; deny → `{"behavior":"deny","message":<text or "Denied in Subfleet">}`; cancel-turn → deny with `"interrupt":true` |
| Claude `can_use_tool` with `requires_user_interaction:true` | deny, cancel-turn | one-tap allow is not offered (the provider's own rule) |
| Codex `item/commandExecution/requestApproval` | allow, allow-session, deny, cancel-turn | `{"decision":"accept"}`, `"acceptForSession"`, `"decline"`, `"cancel"` |
| Codex `item/fileChange/requestApproval` | same | same |
| Codex `item/permissions/requestApproval` | allow-turn, deny | `{"permissions": <requested>, "scope":"turn"}` / `{"permissions":{}}` |
| Codex `item/tool/requestUserInput`, `mcpServer/elicitation/request` | deny | `{"answers":{}}` / `{"action":"cancel","content":null}`, plus an `error` event naming the unsupported request |

`allow` never widens beyond what the provider asked (no `updatedPermissions`,
no execpolicy or network amendments). Approvals never expire on their own
(Claude prompts do not time out, `doc-typescript.md:1051`); a turn stopped or
ended moves its pending approvals to `withdrawn`, and Claude receives
`control_cancel_request` only for requests the CLI sent. After a daemon
restart, pending approvals are re-derived from the replayed driver state; a
Claude `initialize` response's `pending_permission_requests` is matched by
request id and never creates a duplicate row.

## 9. Attachments and drafts

The app keeps drafts (text, attachment list, settings) per conversation in
`~/Library/Application Support/<bundle id>/drafts/<conversation id>.json`,
0600, written atomically on each edit (debounced 300 ms), deleted only after
a receipt for the message that consumed it. Pasted images are written by the
app to `…/drafts/images/<uuid>.png` (0600), then registered with
`attachment.add` when sent; the daemon's copy is what the provider sees.
Pending sends (message id + payload) are journaled in `…/outbox.json` before
the first `message.submit` and retried with the same id until a receipt
exists (C-24.3). Daemon attachments are kept while any non-terminal message
references them and 30 days after last use; retention deletes nothing else.

## 10. Catalog and history

`subfleet.conversations.catalog` (a process, D-18) writes
`<state>/catalog.json`:

- Claude: depth-1 `*.jsonl` under `~/.claude/projects` (or
  `SUBFLEET_CLAUDE_DIR`), newest 400 by mtime; per file the last
  `custom-title` in a 256 KiB tail (observed in every recent transcript this
  session: 2,619 `custom-title` records across 25 files), else the first real
  user prompt (160 characters), `cwd`, last model, last permission mode;
  excluded when `headless_transcript` says it is a lane run; `live_elsewhere`
  from the pid registry.
- Codex: rollouts under every enrolled Codex lane home and under `~/.codex`
  (the Codex app's home), newest 400 by path date; title from
  `session_index.jsonl` `thread_name` (observed in `~/.codex`), else the first
  user message; `continuable` only for lane homes; `~/.codex` threads are
  history plus handoff (D-15), because no lane owns that home and C-10.3 keeps
  the desktop login out of admission.
- A per-file cache keyed by (path, size, mtime) makes a refresh re-read only
  changed files. Wall-clock cap 20 s; a capped run writes what it has with
  `complete:false`.

`conversation.history` renders transcript pages for a conversation; the app
shows history and the live turn's events in one timeline, de-duplicated by
message uuid (Claude) or turn id (Codex).

## 11. Recovery

- Daemon restart: guardians and turn processes keep running (C-5.1). The
  daemon re-adopts attempts as today, reconnects relays from
  `start.json.control_socket`, and rebuilds each driver by replay (D-3).
  Pending approvals survive; the person can still answer them.
- App quit or crash: nothing in the daemon depends on the app. The app
  reloads drafts and its outbox journal and resends unacknowledged messages
  with their original ids.
- A turn process that exits without a terminal event reconciles (D-10).
- `delivery-unknown` blocks only its conversation.

## 12. Desktop client

SwiftUI, ported from the legacy cockpit with provenance comments, split into
files per `maps/legacy-swift.md` §9. New or replaced parts:

- `DaemonClient`: one request per connection over `daemon.sock` (per
  `maps/v2-protocol.md` §7), `v:1`, unique request ids, 15 s default timeout,
  25 s + margin for `conversation.events`; `capabilities` at launch and after
  each reconnect.
- `StatusModel`: the existing `status.json` decoder (kept under
  `SUBFLEET_MODEL_TEST`), extended with `earliest_reset`,
  `reset_credits_remaining` and Claude windows by scope.
- `ConversationStore`: conversations, catalog, per-conversation event cursor
  loop, drafts and outbox journals, approval sheet.
- Runs view: `list`, `show`, `kill` through the socket.
- Scenes: `Window("Subfleet", id: "main")` plus `MenuBarExtra`; `LSUIElement`
  false; an app delegate reopens the main window on Dock click
  (`applicationShouldHandleReopen`); the menu panel keeps width 430 and gains
  a footer "Open Subfleet" (⌘O in the app menu). The view probe keeps
  `visible_windows == 0` under `.prohibited` activation.
- A daemon that is down, too old (no `capabilities`), or reports a different
  schema shows an actionable banner; drafts stay editable and the outbox
  keeps its messages.

## 13. Legacy continuity

The legacy outbox holds 6 messages, all `finished`, in 3 Claude sessions; the
pending-message journal is `{}`; there are no composer attachments; no broker
runs (inventory in the private recovery package, 2026-09-24). Import is
therefore read-only: each of the 3 sessions becomes an `origin:"legacy"`
conversation bound to its native id (if that session still exists), with the
6 messages as terminal history references. The importer's current mapping of
`outbox.sqlite3` onto notices (`importer.py:1530-1545`) and the `cockpit`
drop row (`importer.py:150`) are corrected to this classification. The import
is idempotent by legacy `message_id`.

## 14. Test plan

Every test names its clause (C-20.5). Layers: `unit` (drivers, digest,
relay framing, redaction), `fake` (daemon with fake provider binaries that
speak the Claude stream-json and Codex app-server protocols from recorded
fixtures), `process` (guardian relay across a daemon SIGKILL), `frontend`
(Swift model probes against fixture JSON produced by the Python service),
`live` (opt-in; one real Claude and one real Codex conversation).

Contract fixtures: the Codex frames the driver emits validate against the
pinned 0.153.3 JSON schema (`tests/fixtures/codex/app-server-0.153.3/`);
Claude frames against the shapes recorded from 2.1.280.

## 15. Not in this release

Warm provider workers (D-1); mobile or cloud sync; voice; an embedded
browser or IDE; replacement sign-in (existing native login remains an account
setup step); continuing `~/.codex` threads in place (handoff only); approval
kinds the provider marks experimental.

## 16. Decisions for Max

- D-14: attended turns ahead of detached jobs in admission order (no reserved
  capacity).
- D-16: attended turns may run outside git, and on `main`/`master` only with a
  per-conversation `allow_main` set by a person.
- D-5: a Claude conversation may continue under any enrolled Claude account
  (prompt caching does not carry across accounts, so the first turn on a new
  account costs more).
