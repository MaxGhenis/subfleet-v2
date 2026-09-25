# Desktop workspace: design

Revision 3, 2026-09-24 (revision 2 earlier that day). Binding clauses are
C-24 to C-30 in `docs/acceptance-contract.md`; this document is the
specification those clauses cite. Transition plan: `~/subfleet-desktop-transition-20260924.md`.
Code maps of the base revision (`3f155e5`) are in `docs/desktop/maps/`;
citations of the form `file.py:N` refer to that revision.

Revision 2 folds in an adversarial review of revision 1 by five independent
lenses (durability, v2 integration, security, provider protocol, product),
recorded in `docs/desktop/reviews/2026-09-24-contract-review.md` with each
finding's disposition. Probes cited as "verified" were run this session
against the installed Claude Code 2.1.280 and codex-cli 0.153.3 without model
calls (scratch homes with no credentials, or `shouldQuery:false` messages).

Revision 3 follows the review of the first implementation
(`feat/dw-turnctl`): D-13 orders SIGINT before closing stdin, as C-24.7,
review IR-3 and the runner do, and D-14 states the case of a native session
with no record on disk.

## 1. Outcome

Max starts, follows up on, approves or denies, stops, inspects, and reopens
real Claude and Codex coding conversations in the installed Subfleet app,
across all enrolled subscription accounts, without switching to the Claude or
Codex apps. The daemon stays the only execution authority. The app is built
new (Max, 2026-09-24: "feel free to build it as if from scratch"); the legacy
cockpit is a record of requirements and edge cases
(`maps/legacy-swift.md`), not a code base.

What Max asked of the app (archived Codex task `01a04752…`, 2026-08-28 to
2026-09-01), each a row in `docs/desktop/ledger.json`:

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

### Execution

**D-1. A turn is a job, with turn rules.** Every conversation turn is one v2
job of kind `turn` with exactly one attempt, one guardian, and one provider
process that lives for that turn. It is admitted by the same `evaluate` in the
same reserving transaction as every job (C-6.3), so closures, the Fable
reserve, identity, desktop and slot caps all apply, and C-23.54 holds. The
provider runs in its bidirectional mode (Claude stream-json, Codex
app-server) so the daemon can stream output, answer approvals and interrupt.
Turn jobs differ from detached jobs in exactly these ways, each a clause in
C-26:

- only the daemon's dispatcher creates them, through an internal submit; the
  socket `submit` op refuses `kind:"turn"` and request ids beginning `turn:`
  (review F-12);
- `max_attempts` 1, no parent, no caller-session notice (the conversation is
  the delivery channel; review F-10);
- no salvage ref: the receipt records the workspace's HEAD before and after,
  and tree objects of the working tree at start and end for the diff (D-25),
  creating no ref (review F-14);
- no deliverable export; the outcome is the driver's (D-9), classified by a
  turn classifier over structured provider evidence only (review F-01, F-02);
- their own retention budget and pinning rule (§9).

Verified: `codex app-server` answers `initialize` 0.26 s after spawn and
exits rc 0 0.02 s after stdin EOF; Claude with `--input-format stream-json`
answers an SDK `initialize` control request with `account`, `models` and
`fast_mode_state` and exits rc 0 on stdin EOF.

*Rejected for this release: warm workers.* A process that outlives its turn
holds a lane slot while idle, resolves its credential once, escapes per-turn
admission, and cannot give per-turn receipts (`maps/v2-store-jobs.md` §7).
Stage 3 measures per-turn start latency and the release states it.

**D-2. The guardian relays numbered frames.** For a turn attempt the guardian
owns the child's stdin as a pipe and listens on `<state>/run/<16 hex>.sock`,
accepting only the process `daemon.lock` names (pid, boot id, start time via
`LOCAL_PEERPID`, re-read per connection). Frames carry a sequence number, a
tag and the SHA-256 of their line. The guardian logs an intent (fsync), writes
the pipe, then logs `written` or `failed`. A resent number is a duplicate only
when its hash matches and it was written; different content is a conflict; a
failed or unfinished write is never reported as applied and ends relaying.
The guardian never parses provider messages. Implemented and tested
(`subfleet/relay.py`; reviews SEC-6, F5).

**D-3. Drivers are pure and replayable.** A driver per provider turns
(stdout lines with byte offsets, operator commands) into (events, frames,
approvals, at most one outcome) and does no I/O. The runner tails
`<attempt>/stdout`. After a daemon restart the runner rebuilds the driver by
replaying stdout from the start and consults `stdin.jsonl`: a frame whose tag
is logged as written is not sent again. Each attempt keeps a watermark
`(stdout offset, stdin seq)` updated in the transaction that stores the events
it covers; replay stores nothing at or below it (review F6). Events are keyed
`(attempt, source, position, ordinal)` with no NULL part: `source` is
`stdout` (position = byte offset) or `command` (position = the command's tag).

**D-4. Conversations have their own store.** Conversation state lives in
`<state>/conversations.sqlite3` (WAL, `synchronous=FULL`, 0600), written only
by the daemon through its own connection and lock. `state.sqlite3` keeps
schema 5: every retained release still opens it, so daemon rollback stays
possible (review F-11), and streamed events never contend for the main
store's single lock or add `events` audit rows to it (review F-09). The main
store is the source of truth for jobs and attempts; a message's binding to its
turn job is repaired on start-up by looking up request ids `turn:<message
id>:<n>` (review F-07). Long-term history is the native transcript
(Claude `~/.claude/projects/<slug>/<id>.jsonl`, Codex
`<home>/sessions/**/rollout-*-<id>.jsonl`), which records every turn Subfleet
runs; nothing Subfleet does rewrites one.

### Routing and failover

**D-5. Claude conversations move between accounts; Codex conversations stay
home.** A Claude transcript can be resumed under any Claude lane that
launches with the default config directory, which is every lane whose
credential kind is `keychain-token` or `env` (all 17 today, `lanes.json`); a
`home`-kind Claude lane sets `CLAUDE_CONFIG_DIR` and is not a candidate for
turns (review F-13). Admission routes each Claude turn by model across those
lanes, preferring the lane that served the conversation's previous turn while
it stays eligible for the model (not closed, above the headroom floor,
identity verified), so turns keep the account and its prompt cache; a move
happens on a closure, a limit, the floor, or a person's "move", and each move
is a `served` event with its reason (review U-F10). A Codex thread lives in
one `CODEX_HOME`, so a Codex conversation is pinned to the lane holding its
thread; while its lane is limited the app shows the reset time and offers a
labelled handoff (D-18). Stage 3 tests relocating a thread between lane homes
(copy the rollout, `thread/resume`, attest); only a clean result adds a
`relocate` action (review U-F11).

**D-6. Failover is a labelled continuation, never a re-send.** Both providers
take the message into the native transcript before the model answers (Claude
records it before the API request, binary offset 193639230; Codex returns the
turn id before any model call and persists the user message with its
`client_id`, both verified), so a limit is never "before acceptance" in a way
that allows re-sending (reviews P1, P2, F-03). When a Claude turn ends
`failed (limited)`:

- the lane's closure is recorded at finalization (C-9.6) before anything else
  happens;
- if the conversation's `auto_continue` setting is on (default on, because
  Max asked for it), the daemon queues one *continuation*: a new message with
  a new id, `origin:"failover"`, `continues:<original id>`, and the fixed text
  "Continue from where you left off; the previous turn stopped at a usage
  limit.", excluded from the limited lane;
- at most two continuations per original message; the app shows each as
  "continued on <account> after a usage limit".

A Codex conversation whose lane is limited waits for that lane (admission
`capacity` wait on the closure); moving it is a labelled handoff (D-18).

**D-7. Attended turns are ordered first and hold nothing back.** Turn jobs
sort ahead of detached jobs within their tier. A turn that cannot be placed
never enters `waiters` for detached jobs, so it neither holds them
`behind-older-job` nor causes `slot-kept`; turns hold back only other
competing turns (review F-04). Turns never trigger an admission probe: a lane
whose verdict would require one (C-11.4 unmeasured writable, C-11.7
`requires_probe`) is not a candidate for a turn, reason `probe-required`
(review F-03). Turns count against `max_active_attempts` and per-lane slots
like any attempt (review F-05); this is ordering, not reserved capacity
(plan amendment 11). A turn waiting on an approval holds its slot for at most
`approval_wait_s` (policy, default 3600 s), after which its approvals are
withdrawn and the turn is stopped (D-12) with reason `approval-timeout`.

### Safety

**D-8. Only a person approves, resolves, or widens.** Person-only operations
are `approval.respond`, `approval.get` (full input), `message.resolve`,
`conversation.settings` that widens the permission policy or sets
`allow_main`, and `conversation.unblock`. The daemon reads the caller's pid
with `LOCAL_PEERPID` (verified available from Python on this macOS) and
refuses (exit 7) when the caller or any ancestor is a live guardian's
descendant or carries `SUBFLEET_ATTEMPT`/`SUBFLEET_ROOT`, and accepts only
when the caller is the installed Subfleet app's executable or has a
controlling terminal. Approval nonces appear only in person-only results.
This is a boundary against agents Subfleet launched and headless agents; any
process of the same user that drives a terminal remains inside the trust
boundary, as with `daemon.sock` today (review SEC-1).

**D-9. Permission policy.** Per conversation, part of every message's
settings and digest. Mapping from an existing Claude session's last recorded
mode, applied once when the conversation row is created: `bypassPermissions`
→ `bypass`; `acceptEdits` → `accept-edits`; `default`, `manual` → `ask`;
`plan`, `dontAsk` → `read-only`; `auto`, anything else, or none → `ask`
(review SEC-7). A new conversation starts at `ask`. Widening is person-only
with `confirm_widen: true`.

| Setting | Claude flags | Codex thread (`thread/start`, `thread/resume`) | Codex `turn/start` |
|---|---|---|---|
| `ask` | `--permission-mode default --permission-prompt-tool stdio` | `sandbox:"read-only"`, `approvalPolicy:"on-request"`, `approvalsReviewer:"user"` | `sandboxPolicy:{type:"workspaceWrite", networkAccess:false}`, `approvalPolicy:"on-request"` |
| `accept-edits` | `--permission-mode acceptEdits --permission-prompt-tool stdio` | same | same |
| `bypass` | `--permission-mode bypassPermissions --permission-prompt-tool stdio` | `sandbox:"read-only"`, `approvalPolicy:"never"` | `sandboxPolicy` workspaceWrite, `approvalPolicy:"never"` |
| `read-only` | the v2 read-only flag set (`claude.py:1211-1225`) | `sandbox:"read-only"`, `approvalPolicy:"never"` | `sandboxPolicy:{type:"readOnly", networkAccess:false}` |

- Every Claude turn outside `read-only` passes
  `--settings '{"disableAllHooks":false,…}'`, which outranks project and
  local settings, so a workspace file cannot switch off the user's
  never-rules hook (review SEC-2).
- `bypass` still routes the prompts no mode auto-approves (AskUserQuestion,
  ask rules) to the person instead of letting `-p` deny them (review P5).
- The Codex thread is always opened read-only and the writable policy rides
  on each turn. Verified: a `thread/start` with `sandbox:"workspace-write"`
  and a `cwd` adds `[projects."<cwd>"] trust_level = "trusted"` to the home's
  `config.toml`; with `sandbox:"read-only"` plus a `workspaceWrite` turn
  policy it adds nothing (review SEC-4). Stage 3 confirms a live turn writes
  under the turn policy.
- A `bypass` Codex turn's `workspaceWrite` policy carries `networkAccess:
  true` when the policy's `network.codex_workspace_write` is true (d260,
  2026-09-24: parity with a writable Claude turn, whose Bash has the network;
  the table above shows the value with the switch off). Under `ask` and
  `accept-edits` it stays false, so a network command reaches the person as an
  approval, as a Claude turn's Bash does. Such a turn's server also starts with
  `-c features.unified_exec=false` (C-23.6: `write_stdin` bypasses the
  never-rules guard). The value is fixed in the turn's manifest at submit, so
  a replay sends what the first launch sent. A read-only turn never has the
  network. This chooses the turn's own
  sandbox; C-27.2 still forbids granting a network amendment a request asks
  for.
- Codex never receives `dangerFullAccess`, `externalSandbox`, granular or
  experimental policies. Codex turns carry C-23.6's `unified_exec` switch
  whenever exec launches do.
- A writable turn (Claude `accept-edits`, any writable Codex turn) is
  refused, exit 7, when its workspace (realpath) equals or contains the state
  root, `~/.claude`, `~/.codex`, an enrolled lane home, or the guard hook's
  directory (review SEC-8). Claude `bypass` can write anywhere whatever the
  workspace, as the Claude app does; its protection is the never-rules hook.
- Until Stage 3 shows the never-rules hook denying a forbidden Bash command
  and an `apply_patch` inside a live app-server turn, Codex conversations run
  `read-only` only (review SEC-3).

**D-10. The Codex guard covers the turn server.** Each Codex turn process is
`<PreflightResult.executable> app-server --listen stdio:// -c <verified
override>` after the C-14.2 preflight passes for this lane and cwd. Before the
thread opens, the driver calls `hooks/list {cwds:[cwd]}` on the same server
and applies the preflight's own checks; any difference ends the turn
`failed (guard-refused)` before the message is sent. Credential requests
(`account/chatgptAuthTokens/refresh`, `attestation/generate`) and unknown
methods get JSON-RPC -32601. The experimental API is never negotiated.
Implemented in `codex_turn.py`, frames validated against the pinned schema.

**D-11. What is shown, and what is kept.** Events and op results carry the
assistant's text, the reasoning the provider's own app displays (Claude
`thinking` text, Codex reasoning summaries), tool names, scrubbed input
summaries (500 characters) and scrubbed result previews (2 KiB); a
credential-reading call shows as hidden. Codex raw reasoning, Claude
`redacted_thinking` and signatures never enter events or op results. At
rest, the attempt's `stdout`, `stdin.jsonl`, `prompt.md` and the message text
hold unscrubbed provider content: 0600 in 0700 directories, retained under §9,
never exported, never in notices, never in a handoff brief unscrubbed
(review SEC-9, P9). Approvals are the exception to display redaction: the
person sees the full input with token-shaped values masked in place, never
truncated or suppressed (review SEC-5; §8).

### States, stops, ambiguity

**D-12. States are the provider's facts.** Local acknowledgement, provider
acknowledgement and turn completion are separate:

| State | Meaning |
|---|---|
| `queued` | Durably accepted; waiting behind the conversation's current turn |
| `waiting` | Its turn job waits for admission (capacity, lease, external writer, workspace, route) |
| `starting` | An attempt is reserved or starting; the provider has not acknowledged the message |
| `running` | Acknowledged: Claude `command_lifecycle {command_uuid:<id>, state:"started"}` when `system/init.capabilities` has `msg_lifecycle_v1`, else the replayed user message with our uuid (both verified in a `shouldQuery:false` probe); Codex `turn/start` response with a turn id |
| `approval-needed` | Running with an unanswered approval |
| `complete` | The provider reported success (Claude `result` subtype `success`; Codex `turn.status:"completed"`), even if a stop was requested (recorded as `stop_too_late`) |
| `failed` | The provider reported failure, or the message provably never reached it |
| `interrupted` | The provider reported the turn stopped, after a person's stop |
| `cancelled` | Withdrawn before its job had an attempt |
| `delivery-unknown` | The provider may have it and no terminal evidence exists |

**D-13. Stopping a Claude turn never leaves it resumable by accident.**
Claude documents that SIGTERM leaves the turn unfinished and that the next
`--resume` continues it (headless docs, "Stop a run with SIGTERM"), so every
stop path escalates, each step ending at the first `result`: (1) control
`interrupt`; (2) SIGINT to the provider child through the relay's `signal`
op, which the guardian applies to its own unreaped child, so the pid cannot
have been reused (review IR-3); (3) close stdin through the relay; (4) only
then C-5.6 containment. SIGINT comes before closing stdin because it ends the
turn with a `result` (live probe, `reviews/2026-09-24-live-probes.md`), while
a closed stdin only ends the process once the turn has finished (D-15). The
steps are taken at policy clocks (C-24.7).
`turn.interrupt` records `stop_requested_at` on the message; the job's
cancel request is set only at step 4 (reviews F1, F4, P4). A Codex turn stops
with `turn/interrupt`, then containment.

A Claude attempt that ends without a terminal `result` after its message was
acknowledged (killed at step 4, crashed, quarantined) sets the conversation
`blocked_by:"unfinished-turn"`. The next message waits until the person
chooses: *continue it* (the next turn resumes and Claude continues the
unfinished turn) or *leave it* (the next message is sent with a one-line
system note that the previous turn was stopped and must not be resumed). The
choice is person-only and recorded.

**D-14. No re-send after ambiguity.** A message whose delivery is uncertain is
never sent again automatically. Reconciliation, in order: (1) the attempt's
own stdout (acknowledgement events of D-12 mean delivered); (2) the native
transcript after the attempt's recorded `transcript_offset` (Claude: a user
record with our uuid; Codex: a `UserMessage` item with `client_id` equal to
the message id, verified to be persisted in the rollout); (3) otherwise
`delivery-unknown`, blocking only its conversation until the person resolves
it (`message.resolve`, strict `confirm:true`, recorded). Absence is
`not-delivered` only when the process is verified gone, the transcript was
readable, and the relay log shows the user-message frame was not written. A
native session with no record on disk at all (a new Claude session whose
transcript was never created, a Codex attempt with no thread id) counts as
read without the message. That is safe because the relay proof carries it:
the relay logs each frame's intent with fsync before its pipe write (C-26.4),
so a log read whole and consistent with no user-message record proves the
message never reached the pipe.

**D-15. Background work ends with the turn.** Claude keeps `-p` alive after
input closes while a background subagent, workflow or monitor runs, up to
`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (default 600 000; verified in the
binary). Turns launch with the ceiling at 120 000 and without the tools that
schedule work for a session that no longer exists (`Monitor`, `CronCreate`,
`ScheduleWakeup`, `RemoteTrigger`). Output after the terminal event is
attached to the same message and never changes its outcome; a process still
alive `ceiling + 15 s` after `result` is stopped by the D-13 escalation
(review P3).

### Workspace, identity, settings

**D-16. Workspace.** A conversation is bound to one directory for its life:
an existing session's recorded cwd, or for a new conversation a directory the
person picks (default: a new git worktree when the directory is a
repository). Turns run in place and hold `worktree:<git toplevel or
directory>`; two conversations on one checkout take turns (a lease wait,
never a refusal; review F-06). A directory outside git is allowed. A checkout
on `main` or `master` needs `allow_main`, person-only, settable at creation or
on an existing conversation with confirmation. C-13.2 still binds every
detached job.

**D-17. One writer per native session.** A turn holds `conversation:<id>` and
`native:<provider>:<native session id>` from reservation through finalization
or quarantine. The next message's turn job is submitted only when the
previous one is terminal and has released them (review F9, F-06). Resume,
revive and handoff reservations also check `native:*` and `conversation:*`
(symmetric; review F-08); `_resume_submission` refuses a `turn` source, and
the daemon refuses a resume or revive of a conversation's session at submit
and fails one at admission if its session became a conversation's since.
Conversation-bound sessions, and every session a turn ran, are not the
sessions kit's: never listed, nudged, revived, cold-swept or handed off
(`sessions state` reports them as `conversation_sessions`). A turn's own
`SessionStart` hook wakes nothing, and its `SessionStart` and
`UserPromptSubmit` hooks surface only notices that name a job: the completion
of work the conversation dispatched reaches its next turn, and a `ping` or a
nudge does not (C-26.13). An
external writer (a live pid in `~/.claude/sessions/*.json` naming the session
that carries no Subfleet markers and is not a recorded owned identity) is an
admission wait `external-writer` shown in the app ("open in the Claude app;
close it there to continue here"), not a refusal. Claude's is checked when a
message is dispatched (`ConversationService._hold_for_writer`) and again when
its attempt starts, since a job can wait in admission while the Claude app
takes the session: `_writer_check` records the answer in the attempt's
`held_by.json` before anything is written (a replay reads it back), and a held
attempt ends before `initialize` as `external-writer`, which re-admits the
message. That re-admission never counts toward the three a failing provider
gets; a Codex one (seen only by starting a provider) waits 30 s between tries.
A registry row holds its session only while its pid's start time equals the
row's `procStart` (a reused pid holds nothing), and when `ps` cannot answer.

**D-18. Handoffs are labelled.** Moving a conversation to the other provider,
or out of a Codex home it cannot run in, creates a new conversation whose
first message is a continuity brief (`sessions/handoff.py`, scrubbed,
bounded), recording `handoff_from` (provider, native id, transcript path,
brief SHA-256). The app labels it "handoff from …".

**D-19. Model identity.** Claude settings store the `initialize` catalog's
`value` (for example `opus`, `opus[1m]`, `claude-fable-5-1[1m]`); the expected
served id is that entry's `resolvedModel` with any `[1m]` suffix removed;
`system/init.model` is compared after removing `[1m]`; assistant messages
with model `<synthetic>` are excluded from the check and classified as API
errors. Effort is validated against the chosen entry's
`supportedEffortLevels`; an entry without them accepts none (review P6).
Codex settings are validated against `model/list` for the lane's account
(model, `supportedReasoningEfforts`, Fast = `serviceTiers` id `priority`).
A probe of Claude Code 2.1.280 (2026-09-24) listed only `default`,
`opus[1m]`, `claude-fable-5-1[1m]`, `sonnet` and `haiku`; there is no bare
`opus` or `fable` value. So a Claude model may also be a model id (which
`--model` accepts): the driver then uses the first entry that resolves to the
model admission routed the turn to (`model_ref`), and Fast additionally needs
that entry's `supportsFastMode`. A Codex turn always asks for the routed model
id; Codex has no aliases. Each driver reports its catalog once per turn and
the service merges it into `conversations/models.json`, which `models.list`
reads: per model id, the values that name it (`default` excluded), efforts,
Fast and image input, with the lanes that reported them. The lane's claimed
account is its email label, compared with `initialize`'s `account.email`;
lane `identity` is uuids that no turn protocol reports.
Fast is independent of model and effort on both providers, and bills
differently: Codex `serviceTier:"priority"` draws on plan limits; Claude
`fastMode` draws on usage credits (code.claude.com fast-mode docs), so the
composer labels Claude Fast "bills usage credits", asks once per conversation,
and routes a Fast turn only to lanes last seen with Fast available; a served
Fast state that differs from the request is a visible `served` warning
(review U-F9). Verified: the no-auth fallback catalog lists `priority`
("Fast") per model.

The pickers read `models.list`, built from policy `models` joined with a
per-provider catalog persisted by the drivers after each `initialize` or
`model/list` (and lane homes' `models_cache.json`). `message.submit`
validates against it; a value not yet observed is accepted as `unverified`
and the driver checks it before the message is sent (review U-F8).

**D-20. Clarifying questions are approvals.** Claude's AskUserQuestion reaches
the host as `can_use_tool` with `requires_user_interaction`; it becomes an
approval of kind `question`, the app renders the questions and options, and
the answer is `allow` with `updatedInput` equal to the original input plus
`answers` (the only input change Subfleet ever makes; review P5). An answer
may be one of the offered labels or the person's own text.

### Client and catalog

**D-21. The app is a client of an explicit endpoint.** It talks to
`<SUBFLEET_HOME or ~/.subfleet>/daemon.sock` using protocol v1 and the new
ops after a `capabilities` check; never runs `~/.local/bin/subfleet-local`,
reads `~/chief-of-staff/state/subfleet`, starts a broker, or falls back to
v1. A development build uses bundle id `org.maxghenis.subfleet.dev` and
refuses `~/.subfleet`.

**D-22. The outbox keeps order.** The app journals every mutating op with its
idempotency key before sending it, including `conversation.create` (keyed by
a client draft id). Per conversation at most one `message.submit` is
outstanding, sent in journal order, and each carries `after_message_id` (the
client's previous message in that conversation, or null); the daemon refuses
`out-of-order` (exit 2) until the predecessor is committed. A journaled send
that has no receipt can be withdrawn locally only after a `message.status`
lookup shows the daemon never received it (review F10).

**D-23. Catalog runs out of process.** Discovery of existing native sessions
runs as `python -m subfleet.conversations.catalog`, started by the daemon every
60 s and on request, capped at 20 s wall clock per run, writing
`catalog.json` atomically with titles and first prompts passed through the
scrubber. It indexes every session, not a recent slice: a (path, size, mtime)
cache means a run re-reads only what changed, and a first build that needs
several capped runs publishes `complete:false` until done. Exclusions come
before any limit: Claude lane runs (`headless_transcript`, and the daemon's
attempts' native ids); Codex rollouts whose `session_meta` source is `exec` or
a subagent. `conversation.list` pages newest first and `query` matches title,
cwd and first-prompt preview across the whole index (review U-F5). The daemon
never scans the Claude projects tree or Codex session trees on a request or
control thread.

**D-24. One feed, one focused poll, and notifications.** The app long-polls
one global `conversation.watch` (a compact change feed: conversation, message,
state, pending approvals) and `conversation.events` only for the conversation
on screen. A row whose `state` is null says something else about its message
changed (an approval asked or answered, or its turn's end snapshot recorded,
D-25) and carries no kind: the app fetches that message again. Both polls run
on a dedicated bounded pool (8 threads) with at most one of each per client;
the `requests` pool that `message.submit` and the session hooks use is never
held by a poll (review U-F3). A turn that completes, fails, needs approval, or
becomes `delivery-unknown` while its conversation is not focused posts a local
notification; the Dock badge counts pending approvals.

**D-25. Changes are shown per turn and per conversation.** At the start and
end of each writable turn (any permission but `read-only`) in a workspace that
is a git checkout with a commit, the daemon writes the working tree as a tree
object through a temporary index (the C-6.8 fast snapshot,
`salvage.working_tree`), creating no ref and leaving HEAD, the real index and
the files alone. The start snapshot is the one admission already takes for
every writable job and keeps as the attempt's `baseline_tree`; the end one is
taken at finalization, before the turn's leases are released, so no other
writer's work lands between the turn and it (`daemon._turn_trees`, receipt
`<attempt>/trees.json`, also copied into the attempt's evidence as
`turn_trees` with HEAD before and after). A quarantined turn gets its end
snapshot when it is confirmed dead; a forced release with writers still live
records that it has none. While git fails transiently, the end is tried at
most three times in one daemon run (the first try and two by the finalization
worker, whose count a restart starts again), and once when a quarantine is
released, since nothing offers an operator's request again; then the failure
is recorded and finalization goes on without an end snapshot. The
conversation store keeps both per attempt (`turn_trees`, §3), so a diff
outlives the turn job's retention, and recording the end writes one
`conversation.watch` row for the message with `state` null (D-24).

`turn.diff` compares a message's latest turn attempt's start and end snapshots
(the attempt recorded last, which C-24.5's one turn at a time makes the one
that ran last; `started_at` has whole seconds, and a re-admitted message's two
attempts can share one); before the end snapshot exists it compares the start
with the working tree now and says so (`to.live: true`). `conversation.diff`
compares the start snapshot of the conversation's first writable turn with the
working tree now, so it also shows what the person changed between turns,
which a finished turn's `turn.diff` leaves out. Both run on the file pool
`conversation.history` uses (C-25.3), read git's plumbing (`diff-tree` with no
external diff driver or textconv filter), and return at most 1,000 files and
512 KiB of unified diff, cut at a line with `truncated: true`; the bound is on
the text returned, measured again after decoding (a byte that is not UTF-8
becomes U+FFFD, three bytes) and scrubbing; the stats count every file (only
when git's file listing itself passes 4 MiB do they count what it listed, with
`complete: false` and `files_truncated: true`). `path` names a file or a
directory. The diff text passes the handoff scrubber (C-25.5), after a pass of
its own: the scrubber redacts a private key only between its BEGIN and END
lines, and a diff can show part of a key without one of them (a hunk whose
context reaches into the key, the 512 KiB cut, or a hunk header, where git
repeats the nearest line above the hunk that starts with a letter). That pass
replaces the key's lines, and runs of lines shaped like a key's body, in
place, so each hunk keeps its line counts. The handoff scrubber then runs on
each line by itself, a hunk's line without its `+`, `-` or space prefix: its
header rule starts at a line's beginning and its token, JWT and base64 rules
look behind a value for a character a value may hold, so a prefix would hide
an `Authorization:` value or a removed token, or be taken into an added line's
base64 run. A result with
nothing to compare has the same shape with `available: false` and a reason:
`no-turn`, `read-only-turn`, `no-snapshot` (not a git checkout with a commit),
`snapshot-failed`, `snapshot-pruned` (the snapshots are unreferenced objects,
which git prunes after `gc.pruneExpire`, two weeks by default), or
`workspace-gone` (git can no longer open the workspace as a checkout: it was
moved or removed, a removed linked worktree for example, although the
repository may still hold the snapshots). Implemented in
`subfleet/conversations/diff.py`.

The app shows a Changes pane with Reveal in Finder and Open in editor, and
never commits, pushes or merges. A worktree conversation offers explicit
"Open PR" and "Remove worktree" (refused while dirty) (review U-F13).

**D-26. Detached work keeps its own place.** Turn jobs carry `kind` in
`status.json` and `list`; the menu panel groups them by conversation ("3
conversations active, 1 needs approval") apart from detached jobs, and the
Runs view defaults to non-turn jobs (`list` and `runs` leave turns out
unless asked; `status.json` shape in §12). The app keeps a Compose view for
detached jobs (task, tier, workspace, optional model pin, Fast, sandbox) with
a preview through `submit {dry_run:true}` (lane, model, rejected lanes and
why), dispatch through `submit`, and `why` for waiting jobs (review U-F14,
U-F15).

**D-27. Per-account windows include model scopes.** `status.json` publishes
per Claude account the five-hour, weekly and every model-scoped weekly window
(including Fable) with percent, reset time and evidence label, from
`provider` and `stale-provider` readings only (C-9.1), plus a Claude
`earliest_reset` (review U-F7). Windows are keyed by scope and window, so a
model-scoped window never replaces the account window (§12, review IR-34).

## 3. Data model

`conversations.sqlite3` (D-4), schema version 2, created on first use. An
older store is carried forward in place, one numbered step at a time in one
transaction, as the main store is (C-3.1); a store newer than the build is
refused. Version 2 added `turn_trees` (D-25). The listing below is the design's
core; `subfleet/conversations/store.py` is the full schema (it also keeps a
`changes` feed for `conversation.watch` and a conversation `request_id`).

```sql
CREATE TABLE conversations (
  conversation_id   TEXT PRIMARY KEY,         -- "cv-" + 26-char ULID
  provider          TEXT NOT NULL CHECK (provider IN ('claude','codex')),
  native_session_id TEXT,
  title             TEXT,
  workspace         TEXT NOT NULL,
  workspace_kind    TEXT NOT NULL CHECK (workspace_kind IN ('in-place','worktree')),
  allow_main        INTEGER NOT NULL DEFAULT 0,
  lane_id           TEXT,                     -- Codex: the home holding the thread
  settings_json     TEXT NOT NULL,            -- {model, effort, fast, permission, auto_continue}
  origin            TEXT NOT NULL CHECK (origin IN ('new','native','handoff','legacy')),
  handoff_from_json TEXT,
  worktree_json     TEXT,                     -- a worktree conversation's {path, branch, source, repository, base}
  blocked_by        TEXT,                     -- unfinished-turn | delivery-unknown | quarantined-turn
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT,
  UNIQUE (provider, native_session_id)
);
CREATE TABLE messages (
  message_id        TEXT PRIMARY KEY,         -- client UUID, canonical lowercase
  conversation_id   TEXT NOT NULL REFERENCES conversations,
  seq               INTEGER NOT NULL,
  after_message_id  TEXT,
  origin            TEXT NOT NULL,            -- person | failover | unblock-note
  continues         TEXT,                     -- failover: the original message id
  digest            TEXT NOT NULL,
  text_path         TEXT NOT NULL,            -- conversations/<cv>/messages/<id>.md
  attachments_json  TEXT NOT NULL,
  settings_json     TEXT NOT NULL,
  state             TEXT NOT NULL,
  state_reason      TEXT,
  turn_seq          INTEGER NOT NULL DEFAULT 0,  -- n in request id turn:<id>:<n>
  job_id            TEXT,                     -- main-store job; repaired from request ids
  turn_ref          TEXT,                     -- Claude uuid acknowledgement / Codex turn id
  served_json       TEXT,                     -- lane, account label, model, effort, fast state
  stop_requested_at TEXT,
  resolution_json   TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (conversation_id, seq)
);
CREATE TABLE approvals (
  approval_id       TEXT PRIMARY KEY,
  message_id        TEXT NOT NULL REFERENCES messages,
  attempt_id        TEXT NOT NULL,
  provider_request_id TEXT NOT NULL,
  kind              TEXT NOT NULL,            -- tool | question | command | file-change | permissions
  request_path      TEXT NOT NULL,            -- the exact provider request, 0600
  request_sha256    TEXT NOT NULL,
  display_json      TEXT NOT NULL,
  options_json      TEXT NOT NULL,
  nonce             TEXT NOT NULL,
  state             TEXT NOT NULL CHECK (state IN ('pending','answered','withdrawn')),
  decision_json     TEXT, created_at TEXT NOT NULL, answered_at TEXT,
  UNIQUE (attempt_id, provider_request_id)
);
CREATE TABLE attachments (
  sha256 TEXT PRIMARY KEY, media_type TEXT NOT NULL, bytes INTEGER NOT NULL,
  path TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT NOT NULL
);
CREATE TABLE attempt_marks (
  attempt_id TEXT PRIMARY KEY, stdout_offset INTEGER NOT NULL, stdin_seq INTEGER NOT NULL,
  compacted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL, message_id TEXT, attempt_id TEXT,
  source TEXT NOT NULL, position TEXT NOT NULL, ordinal INTEGER NOT NULL,
  kind TEXT NOT NULL, data_json TEXT NOT NULL, ts TEXT NOT NULL,
  UNIQUE (attempt_id, source, position, ordinal)
);
CREATE TABLE floors (conversation_id TEXT PRIMARY KEY, compacted_through INTEGER NOT NULL);
CREATE TABLE turn_trees (                     -- schema 2 (D-25), one row per turn attempt
  attempt_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
  workspace TEXT NOT NULL, writable INTEGER NOT NULL,
  head_before TEXT, start_tree TEXT,          -- at admission: the attempt's baseline_tree
  head_after TEXT, end_tree TEXT,             -- at finalization, leases still held
  error TEXT, started_at TEXT NOT NULL, ended_at TEXT
);
```

Driver output is persisted in batches: at most one transaction per attempt
per 250 ms or 64 KiB, which also advances `attempt_marks`; the events
long-poll is woken per batch (review F-09). When an attempt is terminal and
its message terminal, its `text.delta`/`thinking.delta` rows are deleted in
one transaction that sets `floors.compacted_through` to the highest sequence
number removed (review F6, IR-6; C-25.4 says when).

A turn job in `state.sqlite3` is an ordinary `jobs` row with `kind='turn'`,
`request_id='turn:<message id>:<n>'`, `in_place=1`, `max_attempts=1`,
`parent_job_id` NULL, `name='turn-<conversation id>'`, and a `manifest.json`
`turn` block naming the conversation and message. Its payload digest is the
message digest (not HEAD or the policy hash).

## 4. Dispatch

The dispatcher runs on the control loop's worker pool. For each conversation
whose lowest-seq live message is `queued`, and whose previous turn job is
terminal with its leases released, it:

1. looks up `jobs.request_id = 'turn:<id>:<turn_seq>'`; if present, binds it;
2. otherwise publishes `prompt.md` and `manifest.json`, then inserts the job
   row in one main-store transaction, then binds `messages.job_id` and sets
   `waiting` in one conversation-store transaction.

A crash between the two transactions is repaired by step 1 on the next pass.
`turn_seq` increments only for a failover continuation, which is a new
message, so no message ever has two live turn jobs.

`message.cancel` withdraws a `queued` message in the conversation store; for
a `waiting` message it sets the job's `cancel_requested_at` in a main-store
transaction guarded by "no attempt row exists", and marks the message
`cancelled` only if that guard held (review F3). `_launch` re-reads the cancel
flag inside the `attempt.starting` transaction and releases the gate only
if it is still null.

## 5. Wire protocol additions

All new ops are protocol `v: 1`; `PROTOCOL_VERSION` does not change. Errors
use `Exit` codes and `ok:false`. Handlers validate, write at most one
transaction per store, wake the control loop and return; none waits on a
provider, a probe, `ps`, git, or a catalog scan, except the two diff ops
(D-25) and `conversation.create`, which cuts a worktree conversation's worktree
with git (D-16); those three run on the file pool with `conversation.history`
and `attachment.add`, never on the pool the other ops use (C-25.3). Person-only
ops (D-8) are marked †.

| Op | Arguments → result |
|---|---|
| `capabilities` | `{}` → `{protocol:1, daemon_version, conversation_schema:1, capabilities:[…], limits:{…}, codex_writable}`. A daemon without it answers "unknown op"; the client then sends no conversation op. `conversation_schema` versions the ops' shapes, not the store (§3): an added op is a capability (`diff.v1` for the two diff ops, with `limits.diff_bytes` and `limits.diff_files`; `runs.v1` for `conversation.runs`), not a new schema. |
| `conversation.list` | `{provider?, query?, limit?, include_catalog?}` → `{conversations:[Conversation], catalog:{generated_at, complete, items:[CatalogItem], state, age_s, stale_after_s, refreshing}}`; `state` is `absent`, `unreadable`, `stale` or `fresh` (C-30.1) |
| `conversation.open` | `{conversation_id}` or `{native:{provider, session_id, home?}}` → `{conversation, messages (latest 50), events_cursor, pending_approvals}`. Opening a native session creates its row once, applying D-9's mapping. |
| `conversation.create` | `{request_id, provider, workspace, workspace_kind, allow_main†, title?, settings}` → `{conversation, created}`; a worktree conversation's `conversation.worktree` is `{path, branch, source, repository, base, created_at}` (C-26.10) |
| `conversation.settings` | `{conversation_id, settings, confirm_widen?†}` → `{conversation}`; widening is person-only |
| `conversation.unblock` † | `{conversation_id, choice:"continue"|"leave", confirm:true}` → `{conversation}` |
| `conversation.history` | `{conversation_id, before?, limit?}` → a page of the native transcript, newest first, scrubbed (D-11); it reads at most 4 MiB of rows below its cursor (or one larger row, up to 64 MiB) and 8 MiB past it for results, and `next_before` is null once the file's first row is reached (C-29.8) |
| `conversation.events` | `{conversation_id, after, limit?, wait_s?}` → `{events, next, reset, floor}`; long-poll ≤ 50 s; page ≤ 256 KiB; `reset:true` exactly when `after` < the floor |
| `message.submit` | `{conversation_id, message_id, after_message_id, text, attachments:[sha256], settings}` → Receipt; same id + digest returns the stored receipt; different digest exit 2 `message-id-conflict`; unknown predecessor exit 2 `out-of-order` |
| `message.status` | `{message_ids}` → `{messages:[Receipt]}` |
| `message.cancel` | `{message_id}` → Receipt (§4) |
| `turn.interrupt` | `{message_id}` → Receipt (D-13) |
| `message.resolve` † | `{message_id, resolution:"not-delivered"|"delivered", confirm:true}` → Receipt |
| `approval.list` | `{conversation_id?}` → `{approvals:[{approval_id, message_id, kind, display, options, created_at, state}]}` (no nonce) |
| `approval.get` † | `{approval_id}` → `{approval, request (masked in place), request_sha256, nonce}` |
| `approval.respond` † | `{approval_id, nonce, request_sha256, decision, answers?, message?}` → `{approval, receipt}` |
| `attachment.add` | `{path, sha256?}` → `{sha256, media_type, bytes}` |
| `catalog.refresh` | `{}` → `{requested, running, generated_at}` |
| `conversation.watch` | `{after, wait_s?}` → `{changes:[{seq, conversation_id, message_id, state, state_reason, pending_approvals}], next}`; `state_reason` says why a message waits, so a hold shows without a second open; `state` is null on a row that reports no state change (an approval asked or answered, or a turn's end snapshot recorded), after which the client fetches that message again (D-24) |
| `conversation.runs` | `{conversation_id, limit?}` → `{runs:[{job_id, name, kind, state, task, tier, sandbox, wait_reason, created_at, started_at, finished_at, out_path, workdir, lane_id, model_served, model_requested, attempt_state, attempts}]}`: the detached jobs whose caller is the conversation's native session (a Claude turn's tools carry it), turn jobs excluded, lane and model from the latest attempt; the app shows the live ones under the header and all of them on click |
| `models.list` | `{provider}` → `{models:[{short, id, value, values, efforts, default_effort, fast:{supported, billing}, image_input, observed_at}], source}` (D-19) |
| `turn.diff` | `{message_id, path?}` → `{message_id, conversation_id, available, root, path, from:{tree, head, message_id, at}, to:{tree, head, live, at}, files:[{path, status, additions, deletions, binary, from?}], files_truncated, stats:{files, additions, deletions, complete}, diff, truncated, scrubbed}`; `status` is `added`, `deleted`, `modified`, `renamed` (with `from`), `type-changed` or `copied`; counts are null for a binary file; `path` names a file or a directory (every changed file under it) relative to `root`, the checkout's top level, and a path with a `.` or `..` part or a leading `/` is exit 2; with `available:false`, `reason` and `detail` instead of `root`, `path`, `from` and `to`, and empty lists (D-25) |
| `conversation.diff` | `{conversation_id, path?}` → as `turn.diff` without `message_id`; `from` is the conversation's first writable turn's start, `to` the working tree now |

Receipt: `{message_id, conversation_id, seq, origin, state, state_reason,
created, settings, served, stop_requested, updated_at}`.

Event kinds: `status`, `accepted`, `served`, `text.delta`, `text`,
`thinking.delta`, `thinking`, `tool.started`, `tool.completed`, `diff`,
`approval.requested`, `approval.resolved`, `limits`, `error`,
`turn.completed`.

Decisions accepted by `approval.respond`: `allow`, `allow-session`,
`allow-turn`, `deny`, `cancel-turn`, `answer` (with `answers`), each only when
the approval's `options` list it (review P8). Codex `acceptForSession` is
labelled "for the rest of this turn" unless Stage 3 shows it survives
`thread/resume` in a new process (review U-F17).

## 6. Guardian control relay

As D-2. Launch: `python -m subfleet.guardian … --control-socket
<state>/run/<16 hex>.sock --relay-peer-lock <state>/daemon.lock -- <argv>`.
The socket is bound before `start.json` names it (`control_socket`). The
daemon resends a frame after reconnecting with the same number and content;
`conflict` or `failed` moves the turn to reconciliation (D-14).

## 7. Provider turn drivers

`subfleet/conversations/claude_turn.py` and `codex_turn.py` (pure, D-3).

**Claude.** Command (all flags verified in 2.1.280; `--permission-prompt-tool`
and `--thinking-display` hidden but registered and accepted):

```
claude -p --input-format stream-json --output-format stream-json --verbose
       --include-partial-messages --replay-user-messages
       --thinking-display summarized
       --model <catalog value> [--effort <level>]
       (--session-id <new uuid> | --resume <native session id>)
       <permission flags, D-9>
       --disallowedTools Monitor,CronCreate,ScheduleWakeup,RemoteTrigger
       --settings '{"disableAllHooks":false[,"fastMode":true]}'
```

Environment as `ClaudeAdapter.build_launch` (credential only in
`CLAUDE_CODE_OAUTH_TOKEN`) plus `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=120000`.
Frames: `initialize` control request; after its success, the user message
with the message id as `uuid`; `close` after `result`. The `initialize`
response is checked before the message is sent: `account.email` against the
lane's identity (C-10.6), effort against the catalog entry (D-19); its
`fast_mode_state` is recorded. Mapping: `command_lifecycle started` or the
replayed user message → `accepted`; text and thinking deltas (scrubbed a line
at a time); complete blocks, each keyed by its ordinal within its message
because 2.1.280 writes every finished block as its own `assistant` row at
content index 0 while the stream numbers it by API index (so a block is shown
once); `content_block_start` → `status` phase `thinking`, `writing` or
`preparing-tool`, announced on change and never after `result` (thinking the
API omits still shows as thinking); `system` `status` rows → phase
`requesting` (before each API request) or `compacting` (an automatic
compaction, which took 107 s on a 972k-token resume), and `compact_boundary` →
a `compacted` status with its trigger and token counts, which the app shows
as a note; `tool_use` / `tool_result`; `can_use_tool` →
approval (`question` when it is AskUserQuestion); other host requests →
error `control_response` and an `error` event; `rate_limit_event` → `limits`;
model check per D-19; `result` → outcome.

**Codex.** Command: `<verified executable> app-server --listen stdio:// -c
<verified override> [-c features.unified_exec=false]`, `CODEX_HOME=<lane
home>`, API keys removed. Frames: `initialize`; then `initialized`,
`hooks/list {cwds:[cwd]}`, `model/list {}`; then `thread/start` (new) or
`thread/resume {threadId, excludeTurns:true}` with `sandbox:"read-only"`;
then `turn/start {threadId, clientUserMessageId, input:[text, localImage…],
model, effort, serviceTier, approvalPolicy, approvalsReviewer:"user",
sandboxPolicy, cwd}`; `turn/interrupt` on stop; `close` after
`turn/completed`. The thread response's `status.type:"active"` means another
writer (`external-writer`). Every frame validates against the pinned
0.153.3 schema (`tests/fixtures/codex/app-server-0.153.3/`). `item/started`
announces a `status` phase on change: `thinking` for a reasoning item,
`writing` for an agent message, `tool` for any tool item.

**Classification.** A turn attempt is classified from the driver's outcome
and structured provider evidence only, never from prose (reviews F-01,
F-02): Claude `result.subtype`, `result.errors`, `api_error_status`,
synthetic API-error rows, `rate_limit_event`, stderr, and the absence of
`system/init`; Codex `turn.status`, `turn.error.codexErrorInfo`
(`usageLimitExceeded`/`rateLimitExceeded` → limited with a closure;
`unauthorized` → auth-dead), JSON-RPC errors, and `account/rateLimits/updated`
readings. Attestation: Claude as `ClaudeAdapter.attest` over the attempt's
transcript range; Codex by `turn_context.turn_id` in the rollout. PR #39
(C-9.2, error text only) is a prerequisite for the Claude path.

## 8. Approvals

A provider request becomes one `approvals` row (exact request stored at
`request_path`, 0600), an `approval.requested` event with display fields, and
the message state `approval-needed`, in one transaction. The display
includes every field that changes what is granted: Codex `grantRoot`,
`networkApprovalContext`, requested `permissions`, `kind:"writeStdin"`,
`cwd`, and whether the command runs outside the sandbox; Claude
`blocked_path`, `decision_reason` and the full input. `approval.get` returns
the exact request with token-shaped values masked in place, never truncated.
`approval.respond` must carry the `request_sha256` the person saw.

| Provider request | Options | Reply |
|---|---|---|
| Claude `can_use_tool` | allow, deny, cancel-turn | `{"behavior":"allow","updatedInput":<original>}`; deny `{"behavior":"deny","message":…}`; cancel-turn adds `"interrupt":true` |
| Claude `can_use_tool`, AskUserQuestion | answer, deny, cancel-turn | `allow` with `updatedInput` = original + `answers` |
| Claude `can_use_tool`, other `requires_user_interaction` | deny, cancel-turn | |
| Codex command / file-change approval | allow, allow-session, deny, cancel-turn | `accept`, `acceptForSession`, `decline`, `cancel` |
| Codex permissions approval | allow-turn, deny | `{"permissions":<requested>,"scope":"turn"}`, `{"permissions":{}}` |
| Codex user input, MCP elicitation, legacy approvals | none (refused) | `{"answers":{}}`, `{"action":"cancel"}`, `{"decision":"abort"}`, plus an `error` event |

`allow` never adds `updatedPermissions`, execpolicy or network amendments.
Pending approvals survive a daemon restart (re-derived by replay; a
re-announced request is matched by id) and are withdrawn when the turn ends
or `approval_wait_s` passes (D-7).

## 9. Attachments, drafts, retention

- The app keeps drafts per conversation (text, attachments, settings) in
  `~/Library/Application Support/<bundle id>/drafts/`, 0600, written
  atomically after each edit (300 ms debounce), deleted after the receipt of
  the message that consumed it. Pasted images are written there first and
  registered with `attachment.add` on send.
- `attachment.add` opens without following symlinks, requires a regular file
  owned by the daemon's user, at most 20 MiB, PNG/JPEG/GIF/WebP magic,
  copies to `attachments/<sha256>.<ext>` (0600, fsync) and re-hashes the copy.
  `message.submit` updates `last_used_at` for each hash in its transaction.
  Retention deletes an attachment row in one transaction that re-checks "no
  non-terminal message references it and last use ≥ 30 days ago", then
  unlinks (review F11). The driver checks size and hash before building
  frames; a missing file fails the message `not-delivered`
  (`attachment-missing`) before any frame is sent. Copies inside attempt
  directories follow job retention.
- Turn jobs have their own retention budget (default 2,000 turn jobs or
  4 GiB). A turn job is pinned while its message is non-terminal or its
  conversation is blocked, and for 14 days after it ends. Pruning copies the
  served facts onto the message row first (they already are, at
  finalization), then removes the job directory and rows; the conversation
  store has no foreign key into the main store.

## 10. Catalog and history

`catalog.json` (D-23):

- Claude: depth-1 `*.jsonl` under `~/.claude/projects`, newest 400 by mtime;
  per file the last `custom-title` from a 256 KiB tail (present in every
  recent transcript inspected: 2,619 records across 25 files), else the first
  real user prompt (160 characters, scrubbed), `cwd`, last model and
  permission mode; lane runs excluded; `live_elsewhere` from the pid
  registry, ignoring Subfleet-owned pids.
- Codex: rollouts under every enrolled Codex lane home and under `~/.codex`,
  newest 400 by path date; title from `session_index.jsonl` `thread_name`;
  `continuable` only in lane homes (`~/.codex` threads continue by handoff,
  C-10.3).
- Per-file cache keyed by (path, size, mtime); 20 s cap; `complete:false`
  when capped.

## 11. Recovery

- Daemon restart: guardians and turn processes keep running (C-5.1); the
  daemon re-adopts attempts as today, repairs message bindings (§4),
  reconnects relays and rebuilds drivers by replay (D-3). Pending approvals
  survive.
- App quit or crash: nothing depends on the app; it reloads drafts and its
  outbox journal and resends unacknowledged ops with their original keys in
  order (D-22).
- A turn process that exits without a terminal event reconciles (D-14) and
  may block its conversation (D-13).
- Daemon rollback: after this release the main store is unchanged (schema 5),
  so any retained release opens it. Before a rollback, drain or cancel every
  queued or live `kind='turn'` job (an older daemon would launch a queued turn
  job in its one-shot mode). `conversations.sqlite3` is ignored by older
  releases and kept.

## 12. Desktop client

Built new in SwiftUI (Max, 2026-09-24). Structure:

- `DaemonClient`: one request per connection over `daemon.sock`, `v:1`,
  unique request ids, 15 s default timeout, `wait_s + 15 s` for
  `conversation.events`; `capabilities` at launch and on reconnect.
- `StatusModel`: the `status.json` decoder the menu panel uses today (kept
  under `SUBFLEET_MODEL_TEST`), extended with `earliest_reset`,
  `reset_credits_remaining` and Claude windows by scope.
- `ConversationStore`: conversations, catalog, per-conversation event loop,
  drafts and outbox journals, approvals.
- Views: sidebar (conversations grouped by recency and workspace, search,
  provider filter, live-elsewhere badges), conversation (Markdown timeline of
  history plus live events, activity strip, approval cards, stop, served
  model/account/Fast chip), composer (Return sends, Shift-Return newline,
  paste and drop images, model/effort/Fast/permission controls, queued
  follow-ups), runs (jobs list, detail, kill), fleet (per-account windows),
  settings.
- Scenes: `Window("Subfleet", id: "main")` and `MenuBarExtra`; `LSUIElement`
  false; an app delegate reopens the window on Dock click; the menu panel
  keeps width 430 and gains an always-present "Open Subfleet" (⌘O).
- A daemon that is down, too old, or reports another schema shows an
  actionable banner; drafts stay editable and the outbox keeps its messages.
- Notifications and badge as D-24; Changes pane as D-25; Compose view for
  detached jobs as D-26; per-account windows including Fable as D-27.
- Status strip per turn with stage timestamps from `status` events
  (admitted, spawned, initialized, resumed, accepted), so time spent waiting
  for capacity, starting the provider, and the model are told apart. While
  the turn is live the strip says where the model is (`Waiting for the
  model`, `Compacting the conversation`, `Thinking`, `Writing`, `Preparing a
  tool call`, or `Running <tool>` while a tool call is open) and
  counts up the seconds since that began: a long think or a slow tool reads
  as work, not as a hang (Max, 2026-09-24: two silent minutes read as broken).
- A conversation whose Claude session a live process outside Subfleet holds
  (`live_elsewhere` on every conversation view, from the last catalog run if
  it is fresh) shows D-17's words above the composer: open in the Claude app
  or a terminal; close it there to continue here, and a message sent meanwhile
  waits. The wait itself reads the registry at dispatch
  (`catalog.external_writers`: a live pid naming the session whose executable
  is Claude's and whose environment has no Subfleet markers), holds the
  message `waiting` with reason `external-writer: pid <n>`, and looks again
  every 5 s without backoff; a stop withdraws it. `status` phases come from
  provider output alone, each in its line's own `<offset>:phase` source, so a
  replay after a restart stores exactly what the first run stored; after a stop
  the app keeps saying Stopping, and once the provider has answered it says
  Finishing with no clock.
- The live turn's status strip is pinned above the composer, with the
  elapsed time; the strip under each person bubble keeps the words only.
- A listed session that cannot continue here (a Codex-app thread) opens as a
  page saying why, instead of a failed `conversation.open`.

### `status.json` (C-18.2, C-29.6; review IR-18, IR-34)

The daemon writes `<state root>/status.json` after every probe cycle and every
reset-credit pass, both through `Timers.publish_status` (`subfleet/timers.py`),
on a timer worker. The projection is `subfleet/status_json.py`. Keys the menu
already decoded keep their meaning; the additions are marked *new*. A decoder
accepts a snapshot without any *new* key, because an app may be newer than its
daemon (as C-18.2 already requires for `jobs`).

```jsonc
{
  "generated_at": "2026-09-24T18:00:00Z",
  "offline": false,
  "jobs": {"live": [JobRow], "recent": [JobRow],
           "counts": {"queued": 0, "running": 1, "waiting": 0}},
  "conversations": {                                          // new
    "available": true, "error": null,
    "counts": {"active": 3, "needs_approval": 1, "blocked": 0},
    "turns": {"queued": 0, "running": 2, "waiting": 1},
    "items": [{"conversation_id": "cv-…", "provider": "claude", "title": "…",
               "state": "approval-needed", "blocked_by": null,
               "pending_approvals": 1, "updated_at": "…", "turn": JobRow}],
    "truncated": false},
  "codex": {"homes": […], "fleet": {…}},                      // unchanged
  "claude": {
    "accounts": [{"lane_id": "claude-3", "email": "…", "probe": {…}, "live": {…},
                  "windows": [                                // new
                    {"scope": "account", "window": "five_hour", "model": null,
                     "status": "provider", "stale": false, "used_percent": 42.0,
                     "reset_at": "2026-09-24T21:00:00Z", "source": "oauth-usage",
                     "as_of": "2026-09-24T17:59:40Z", "age_s": 20.0},
                    {"scope": "account", "window": "seven_day", …},
                    {"scope": "claude-fable-5-1", "window": "seven_day",
                     "model": "fable", …}], …}],
    "earliest_reset": "2026-09-24T21:00:00Z",                 // new
    "lanes": {"enrolled": 17, "dispatchable_now": 12}}
}
```

`JobRow` is `{job_id, kind (new), name, state, wait_reason, next_check_at,
sandbox, workdir, model, lane_id, attempts, created_at, started_at,
finished_at, rc, batch}` (C-18.2).

- `jobs` is detached work only: `live`, `recent` and `counts` leave out every
  job of kind `turn` (C-26.12), so the menu's job list and its counts never
  show a conversation's turn as a job someone dispatched.
- `conversations` is read by `conversations.store.status_summary` through a
  read-only connection of its own (`mode=ro`, `query_only`), in one read
  transaction, without the conversation store's lock; the control loop and
  request threads never wait for it. `available: false` means the store could
  not be read: `error` is `not-read`, `schema` (a newer store), or the name of
  the SQLite or OS exception the reader met (for example `DatabaseError`,
  `OperationalError`, `PermissionError`), or of anything else it raised, which
  `publish_status` catches so the file is still written; `counts` is null and
  `items` is empty, so the menu shows "unknown", never a zero nobody observed.
  No `conversations.sqlite3` yet (`stat` reports `FileNotFoundError`) is
  `available: true` with zero counts; a root the reader may not search is
  `PermissionError`, not "no file".
- `counts`, over conversations not archived: `active` has a message `queued`
  or in a live state (design D-12), `needs_approval` has a pending approval,
  `blocked` has `blocked_by` set. `turns` counts live turn jobs by state from
  the job store, and is present even when `available` is false.
- `items` lists only conversations that are active, need approval or are
  blocked: those needing approval first, then blocked, then the rest, newest
  `updated_at` first and, between equal `updated_at`, the later-created
  conversation first (ids begin with their creation millisecond; two created
  in the same millisecond fall to the id's random tail), at most 20
  (`truncated` says more exist; the counts cover all of them). `state` is `approval-needed` when an approval is
  pending, else `blocked` when `blocked_by` is set, else the state of its live
  message (`waiting`, `starting`, `running`, `delivery-unknown`), else
  `queued`. `title` is cut to 200 characters. `turn` is the conversation's
  live turn job (matched by the job name `turn-<conversation id>`, §3), or
  null when none is queued, waiting or running.
- `claude.accounts[].windows` has one row per (`scope`, `window`) pair, the
  key readings are stored under (C-9.8, C-9.9): `scope` is `account` or a
  policy model id, so the Fable weekly window (`claude-fable-5-1`,
  `seven_day`) and the account's weekly window are separate rows and neither
  replaces the other. Rows come only from `provider` and `stale-provider`
  readings with a utilization in [0, 1] (C-9.1); `status` is that evidence
  label and `stale` is true for `stale-provider`; `admission-observed`
  evidence (window `admission`) never appears. `used_percent` is 0 to 100,
  `reset_at` ISO 8601 UTC or null, `model` the policy's short name for a model
  scope (null for `account`, or for a model id the policy does not name).
  Order: account `five_hour`, account `seven_day`, other account windows, then
  model scopes by name. `probe` and `live` keep reading scope `account` only.
- `claude.earliest_reset` is the soonest `reset_at` after `generated_at` among
  account-scope windows of accounts that are enrolled, owned by v2, not the
  desktop login (`active`) and not `identity_status: "mismatch"` (C-10.6: such
  a lane stays enabled and keeps its last readings, but admission never uses
  it), or null. Unlike `codex.fleet.earliest_reset`, the minimum of every
  home's five-hour and weekly `reset_at`, it ignores resets already past.

## 13. Legacy continuity

The legacy outbox holds 6 messages, all `finished`, in 3 Claude sessions; the
pending-message journal is `{}`; there are no composer attachments; no broker
runs (private recovery package, 2026-09-24). Import is read-only: each
session whose transcript still exists becomes an `origin:"legacy"`
conversation bound to its native id, with the 6 messages as terminal history
references, idempotent by legacy `message_id`. The importer's mapping of
`outbox.sqlite3` onto notices (`importer.py:1530-1545`) and the `cockpit`
drop row (`importer.py:150`) are corrected to this classification.

## 14. Test plan

Every test names its clause (C-20.5). Layers: `unit` (drivers, relay,
redaction, mapping tables, digest, dispatcher decisions), `fake` (the daemon
with fake `claude` and `codex` binaries that speak stream-json and app-server
from scripted scenarios), `process` (guardian relay across a daemon
SIGKILL), `frontend` (Swift model probes over fixture JSON the Python service
produces), `live` (opt-in; one real Claude and one real Codex conversation
with a follow-up, an approval, and attested served models; the Codex
never-rules firing test of D-9; the SIGTERM/SIGINT resume behaviour of D-13;
per-turn latency: local acknowledgement p95; submit to `running` and submit
to first delta, p50 and p95, for a new and a resumed conversation on each
provider, beside the native app on the same account; a miss of more than
1.5 s at p95 reopens warm workers before release (review U-F12)).

## 15. Not in this release

Warm provider workers; mobile or cloud sync; voice; an embedded browser or
IDE; replacement sign-in; continuing `~/.codex` threads in place (handoff
only); approval kinds the provider marks experimental; writable Codex turns
until the D-9 live gate passes; in-place continuation of Codex-app threads
(`~/.codex`) unless the Stage 3 relocation test passes (they continue by
labelled handoff, whose brief now reads Codex rollouts too:
`subfleet/conversations/codex_brief.py`).

## 16. Decisions for Max

- D-6: a Claude conversation that hits a usage limit continues automatically
  on another account with a labelled "continue" turn (at most two), on by
  default per conversation.
- D-7: attended turns go first in their tier but reserve no capacity; a turn
  waiting on an approval holds one slot for up to an hour.
- D-16: attended turns may run outside git, and on `main`/`master` only with a
  per-conversation `allow_main` a person sets.
- D-9: Codex conversations are read-only until the live never-rules test
  passes.
- D-19: Claude Fast bills usage credits on the account that serves the turn;
  the app asks once per conversation.
- Codex-app threads in `~/.codex` continue by labelled handoff unless the
  relocation test passes.
