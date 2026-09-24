# Live probes, 2026-09-24

Stage 3 checks of the Claude turn driver against the real CLI, run before any
daemon or app was involved. The probe (kept outside the repository) builds a
`TurnSpec`, spawns `claude` with `claude_turn.argv(spec)` exactly, writes the
driver's frames to its stdin, and feeds each stdout line to
`ClaudeTurn.feed`. Claude Code 2.1.280, Max's own login, model value `haiku`,
permission `ask`, a scratch cwd under `/private/tmp/claude-501/`. Each run is
one short turn; the transcripts are under
`~/.claude/projects/-private-tmp-claude-501-live-claude-probe/`.

## `initialize`

Answer keys: `account`, `agents`, `analytics_disabled`,
`available_output_styles`, `commands`, `current_permission_mode`,
`fast_mode_disabled_reason`, `fast_mode_state`, `models`, `output_style`,
`pid`, `session_state` and remote-control fields. `account` has `email`,
`organization` (a display name), `subscriptionType` and `apiProvider`; no
uuid. `models` values on this account: `default`, `opus[1m]`,
`claude-fable-5-1[1m]`, `sonnet`, `haiku`; entries carry `resolvedModel`,
`supportsEffort`, `supportedEffortLevels`, `supportsFastMode`. This is why
C-26.8 compares the lane's email label and resolves a model id through the
catalog entry for the routed model (design D-19).

## A reply turn

`command_lifecycle` `queued` then `started` for the message's uuid (the
driver's acknowledgement), `system/init` with `model:
claude-haiku-4-5-20251001` and `permissionMode: default`, the replayed user
row (`isReplay: true`), streamed text, `result` `success`; after the driver
closed stdin, `command_lifecycle` `completed` (ignored after the terminal
event) and exit 0. Outcome `complete`, served model as expected.

## Permission prompts: a user hook approves every Bash command

With `--permission-mode default --permission-prompt-tool stdio`, `echo`,
`touch` in the workspace, `python3 -c`, and `touch` outside the workspace all
ran with no `can_use_tool` request. The transcript's `hook_success`
attachment names the reason: `~/.claude/hooks/enforce-package-managers.sh`
prints `{"decision": "approve"}` for every Bash command it does not block,
which Claude Code treats as allow. Max's own sessions run `bypassPermissions`
and are unaffected; a Subfleet conversation in Ask mode gets no Bash prompts
while that hook prints it. Queued for Max as decision d221 (print nothing on
pass-through). The approval path itself is covered end to end with the fake
provider (`tests/e2e/test_conversations.py`) and is still to be seen live
with a tool no hook approves.

## Stopping a turn

| Stop | Provider output | Driver outcome | Exit |
|---|---|---|---|
| control `interrupt` (the driver's own) | `result` `error_during_execution`, `is_error: true`; lifecycle `cancelled` | `interrupted` / `stopped` | 1 after stdin closed |
| SIGINT to the CLI | `result` `error_during_execution`; lifecycle `cancelled` | `failed` / `error_during_execution` when sent alone; `interrupted` / `stopped` when it follows the driver's interrupt, as the runner sends it | 0 after stdin closed |
| SIGTERM to the CLI | nothing: no `result` | `failed` / `ended-without-result` | 143 |

So a SIGINT ends the turn with a terminal event, and SIGTERM ends the process
without one: the transcript is left mid-turn. That confirms design D-13 and
review IR-3 (the escalation sends SIGINT through the relay before closing
stdin, and only containment ever sends SIGTERM), and it is why a SIGTERM or
crash after delivery blocks a Claude conversation (`unfinished-turn`, C-24.8).

## Codex app-server turns

Three turns through the real guard preflight and `CodexTurn`, unchanged, on
lane `codex-6` (codex-cli 0.153.3, `gpt-6-astra`, effort `low`), in a scratch
clone under `/private/tmp/claude-501/codex-guard-probe/work` whose
`origin/main` exists. The preflight (TRUST and hook read from
`~/.subfleet/guard`, its scratch state outside the state root) verified the
guard for that workdir and produced the override the turn server ran with.

- **The never-rules hook fires in app-server turns.** A `bypass` turn (thread
  `read-only`; turn `approvalPolicy: never`, `sandboxPolicy: workspaceWrite`)
  asked to run `git checkout -b never-rules-probe main`. The model called
  `exec_command`; the server sent `hook/started`, then `hook/completed` with
  `status: "blocked"` and the rule's feedback (`[local-main] Branch from
  origin/main, never local main …`); the guard's denial log gained that line
  (2026-09-24T13:59:34-0400); the rollout's tool output reads "Command blocked
  by PreToolUse hook"; the branch was not created. This is the live evidence
  C-26.11 and design D-9 require before writable Codex conversations.
- **`turn/completed` is sent only for a turn that used no tool.** A plain turn
  ("Reply with exactly: pong", read-only) ended with `thread/status/changed:
  idle` then `turn/completed` 0.01 s later, outcome `complete`, exit 0 on
  EOF. A turn whose command was blocked, and a read-only turn that ran `git
  log --oneline -1` (`commandExecution` items arrived as usual), both ended
  with `thread/status/changed: idle` and no `turn/completed` within 45 s,
  although each rollout records `task_complete`. The driver now ends a turn on
  `idle` after it started, once the runner's 2 s grace passes with no
  `turn/completed` (`CodexTurn.settle_idle`), and the fake app-server behaves
  the same way.
- The thread's `client_id` is on the rollout's `UserMessage` item, and the
  rollout's `turn_context` carries the turn id, model, approval policy and
  sandbox policy (the writable policy on a read-only thread).

## Still to do live

- A Claude approval and an AskUserQuestion with a tool no hook approves.
- Codex approvals (`ask` with a command the sandbox refuses).
- `--model opus[1m]` and a `--resume` follow-up through the daemon on a real
  lane, with attestation; the same for a Codex `thread/resume`.
- Continuing a Codex-app thread by rollout copy and `thread/fork` (IR-31).
