# Chips: sessions that suggest sessions

Design for integrate/219 (`52ffde17`), 2026-09-28. A chip is a durable suggestion,
not a dispatch. Only the person starts it. The first child message is exactly the
proposal's prompt; there is no handoff brief or parent turn at Start.

## Host tool compatibility

The installed Claude desktop app supplies these tools through its `ccd_session`
MCP server. They are not built-in Claude CLI tools. Checked in the installed
`/Applications/Claude.app/Contents/Resources/app.asar`, member
`.vite/build/index.chunk-CtwayTMM.js` (minified line 1; embedded offsets 7,805,336
and 7,803,297):

| Tool | Required string parameters | Optional string parameters |
| --- | --- | --- |
| `spawn_task` | `title`, `prompt`, `tldr` | `cwd` |
| `dismiss_task` | `task_id` | `reason` |

Title is an imperative phrase (desktop guidance: under 60 characters); tldr is
one or two plain-English sentences; prompt is self-contained. Cwd is an absolute
directory, defaulting to the parent's workspace. Subfleet bounds title at 200
characters, tldr at 2,000 characters, and prompt at 32 KiB UTF-8. A response to
spawn includes `task_id`, the chip ID, so the model can withdraw it later.

Ship a dependency-free stdio JSON-RPC MCP server as
`python -m subfleet.conversations.chip_host`. It handles initialize, initialized
notifications, ping, tools/list and tools/call, with newline-delimited JSON and
no stdout logging. It exposes precisely these two tools. It sends socket requests
to an explicit Subfleet root; it never launches a daemon or finds a default root.
The shipped command uses an absolute script path with Python's `-I` isolation,
so the same module also runs from an unrelated cwd without inherited PYTHONPATH.
Framing and tool result shapes follow the MCP
[stdio transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
and [tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
specifications; initialization negotiates the supported 2024-11-05, 2025-03-26,
or 2025-06-18 version.

## Turn launch

For each writable Claude turn, the daemon registers a random host capability,
storing its SHA-256 with the parent conversation and originating message. The
launch adds `--mcp-config` with a JSON server definition under `subfleet_chips`;
the command uses the daemon's absolute Python executable and shipped module path
so it works from another project cwd. The definition fixes the state root, parent,
message, and capability outside the tool's model-controlled arguments. Only the
two chip tools are added to `--allowedTools`: suggesting/withdrawing a pending
chip needs no approval dialog; Start remains person-only.

The existing stream-json SDK MCP refusal (C-27.4) stays intact: the CLI launches
this real stdio server. Existing configured MCP servers remain available on
writable turns. Read-only Claude turns retain their strict empty MCP configuration
and do not receive chip tools. Host capabilities survive daemon restart and are
scoped to their parent; they are not a security boundary against the filesystem
owner. The credential is not included in timeline events or tool results.

## Store and daemon operations

Add a `chips` table in `conversations.sqlite3` with chip ID, parent conversation,
originating message, spawn request key/digest, title, tldr, exact prompt, resolved
cwd, state (`pending`, `started`, `dismissed`), child conversation ID, timestamps,
and optional dismissal reason. Add a host-capability table. Add nullable
`parent_conversation_id` and `source_chip_id` columns to conversations using the
store's existing additive migration mechanism; old conversations remain roots.
Initial chip messages alone set an additive `messages.preserve_newlines` flag:
the existing message reader's CRLF/CR normalization must not change the proposal
as it passes from its verified payload into the turn manifest.

| Operation | Arguments | Result |
| --- | --- | --- |
| `chip.spawn` | bound `conversation_id`, `message_id`, `host_token`; `request_id`, title, tldr, prompt, optional cwd | `{chip}` |
| `chip.list` | `conversation_id` | `{chips}` |
| `chip.dismiss` | `chip_id`, optional reason; host binding for tool calls | `{chip}` |
| `chip.start` | `chip_id` | `{chip, conversation, message}` |

Spawn authenticates the capability and originating message, validates the existing
directory and sizes, and deduplicates its request key, rejecting different content
under that key. The MCP server assigns a fresh UUID to each tool call and reuses
it if a socket response is lost. A host can dismiss only its parent's pending
chips. App dismiss and Start require the existing person check. Dismiss after
Start refuses; repeated dismiss is harmless. Chip IDs make Start repeatable.

Start checks the cwd still exists, prepares the prompt payload, then atomically
inserts a fresh child conversation and its queued first message and changes the
chip to started. Concurrent Start calls return the same child and message;
concurrent Start/Dismiss chooses one terminal state without an orphan child.
The ordinary turn dispatcher then schedules the child. It inherits the parent's
provider and current model, effort, fast and permission defaults at Start. It gets
a fresh native session and uses the proposed cwd in place, with no parent brief.
Main-branch permission may be inherited only when the cwd is the same; changing
cwd does not grant an unrelated repository that permission.

`conversation.open` includes a chips snapshot; `chip.created`, `chip.started`,
and `chip.dismissed` events identify the originating message and carry `{chip}`
without the full prompt to respect event-size caps. The full prompt remains in
the table and open/list/start results. Each mutation wakes the conversation watch.
Expose `chips.v1` as an additive capability, without changing wire schema 1.

## App

New chip model/engine helpers and a new SwiftUI card keep most app work in new
files. Small seams connect decoding, timeline projection and the existing journal.
A card displays Suggested task, title, tldr and destination, with Start and Dismiss
while pending. Started cards link to the child; dismissed cards record withdrawal.
Mutations are journaled before sending and replay with the same chip ID. Failure
is visible and leaves the operation recoverable. Opening a conversation merges
the authoritative snapshot with events; old created events never revive a terminal
chip. No prompt expansion or summarization changes what Start sends.

Sidebar rows use parent IDs to flatten visible conversation trees, preserving
siblings' recency order. A child stays beneath its parent even in another cwd or
date bucket. If a parent is filtered or unavailable the child is a root. The helper
guards against cycles. Existing worktree/remote start modes are out of scope.

## Codex scope

This first implementation injects tools into writable Claude turns only. Codex's
app-server has per-thread `config` overrides and MCP server configuration; the
same stdio server is a plausible later integration. It requires separate checks
of config merging, resume behavior and the existing read-only MCP isolation before
enabling it. Backend and app records are provider-neutral; no Codex support is
advertised merely because its config type can represent MCP servers. The vendored
`tests/fixtures/codex/app-server-0.153.3/ClientRequest.json` accepts arbitrary
`config` objects on `thread/start` (line 5075) and `thread/resume` (line 4731).
OpenAI's [MCP configuration documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
defines `mcp_servers.<name>` with command, args, env and cwd, and its
[app-server documentation](https://learn.chatgpt.com/docs/app-server) describes
configured MCP tools. This is evidence for a supported configuration surface,
not a completed Subfleet end-to-end Codex test.

## Validation and contract

Unit tests cover tool schemas/protocol errors, scope, bounds, launch flags,
persistence, exact prompt/settings/cwd inheritance, idempotence, concurrent
Start/Dismiss and rollback. The fake interactive Claude CLI launches the actual
MCP command from `--mcp-config` and calls tools over pipes, exercising a disposable
daemon through the real socket seam. E2E tests cover create, withdrawal, restart,
and person Start through the existing peer boundary. App-core probes cover wire
decoding, event/snapshot folding, journal replay and sidebar nesting. Run the
existing turn/store/service suites, frontend probes and `app/build.sh` locally.
Add a contract clause for durable proposals, person-only atomic Start and exact
child creation. No test contacts the running daemon, launchd, or production state.
