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

## `opus[1m]` and a resumed follow-up

With `--model opus[1m]` (the catalog value), `system/init` reported
`claude-opus-5-5[1m]`, the transcript's assistant rows record
`claude-opus-5-5`, and the driver's served model (the `[1m]` removed) is the
policy id, so attestation compares like with like. A second turn with
`--resume <that session>` and a new message uuid was acknowledged the same
way, appended to the same transcript, and answered from the first turn's
context ("pong").

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

## Hooks inside a Subfleet launch

Whether a turn's own hooks can tell they run inside a Subfleet launch. The
probe ran Claude Code 2.1.280 with `env -i` (so nothing of the probing
session's environment leaked in), a scratch `HOME` and `CLAUDE_CONFIG_DIR`
holding no settings or login (the `-p` run answered "Not logged in", so no
model call was made), the three C-5.1
variables set to synthetic values, and `--settings` naming a `SessionStart`
and a `UserPromptSubmit` command hook that wrote down only the `SUBFLEET_*`
variables they saw and their hook JSON. Four runs: `-p "hi"`; and
`--input-format stream-json` with one user frame, new without and with
`--session-id <uuid>`, and with `--resume` of the first stream-json session.

| Run | SessionStart `source` | Markers in each hook |
|---|---|---|
| `-p "hi"` | `startup` | `SUBFLEET_ATTEMPT`, `SUBFLEET_JOB`, `SUBFLEET_ROOT` |
| stream-json, new | `startup` | the same three |
| stream-json, `--session-id` | `startup` | the same three |
| stream-json, `--resume` | `resume` | the same three |

In stream-json mode the CLI printed `hook_started` / `hook_response` for
`SessionStart:startup` or `SessionStart:resume` before `system/init`. The
`SessionStart` JSON carried `session_id`, `transcript_path`, `cwd`,
`hook_event_name` and `source`; `UserPromptSubmit` carried `session_id`,
`transcript_path`, `cwd`, `prompt_id`, `permission_mode`, `hook_event_name`
and `prompt`. Each stream-json user frame was written to the transcript with
`promptSource: "sdk"`, and the resumed run added an `isMeta` user row.

So before C-26.13 every conversation turn handed its session to the sessions
kit's nudge worker (`subfleet hook SessionStart` calls `wake_worker`, and
`resume` is a source the worker honours, C-23.33), and every
`UserPromptSubmit` could add pending notices to Claude's context beside the
person's message. A conversation's transcript also gains one `sdk` prompt per
turn, so after two turns `transcripts.headless_transcript` stops treating it
as a lane run (C-23.31). With a marker set, the hook now hands no wake to
the kit and surfaces only notices that name a job (C-26.13); a first version
surfaced nothing at all, which also withheld the completion of a job a turn
had dispatched from every later turn, and review caught it. The end-to-end
tests `test_a_turns_session_hooks_see_the_daemons_markers_and_surface_no_ping`
and `test_a_job_a_turn_dispatched_reaches_the_next_turn_and_a_ping_does_not`
run `subfleet hook` from inside real turn launches with the daemon's own
markers.

## Still to do live

- A Claude approval and an AskUserQuestion with a tool no hook approves.
- Codex approvals (`ask` with a command the sandbox refuses).
- A turn through the daemon on a real lane (credential resolution, relay,
  finalization and attestation together); a Codex `thread/resume`.
- Continuing a Codex-app thread by rollout copy and `thread/fork` (IR-31).
