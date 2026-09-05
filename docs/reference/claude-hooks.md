# Claude Code hooks reference (fetched 2026-09-05)

Source: <https://code.claude.com/docs/en/hooks>, fetched 2026-09-05 for the
cutover-compat lane. Everything below is quoted or paraphrased from that page
except the section "What the installed binary actually contains", which is a
local measurement. Cite this file — not memory — for hook behaviour.

Installed harness at the time of the fetch: `claude --version` → `2.1.260`.

## 1. Settings shape

Three levels of nesting: **hook event** → **matcher group** → **hook handlers**.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "/path/to/hook postToolUse",
           "timeout": 5, "asyncRewake": true}
        ]
      }
    ]
  }
}
```

Settings files and precedence: `~/.claude/settings.json` (all projects, not
shared), `.claude/settings.json` (one project, committable),
`.claude/settings.local.json` (one project, gitignored), managed policy
settings, plugin `hooks/hooks.json`, skill frontmatter, subagent frontmatter.
"Hook entries merge across settings levels rather than replacing each other.
User, project, and local settings add their own hooks without removing managed
ones."

### Common handler fields

| Field | Required | Notes |
|---|---|---|
| `type` | yes | `"command"`, `"http"`, `"mcp_tool"`, `"prompt"`, `"agent"` |
| `if` | no | Permission-rule syntax filter, e.g. `"Bash(git *)"`. The hook command only runs if the tool call matches. **Only evaluated on tool events.** |
| `timeout` | no | Seconds before cancelling. Defaults: **600** for `command`/`http`/`mcp_tool`, 30 for `prompt`, 60 for `agent`. Lowered to 30 on `UserPromptSubmit`, `PreModelSwitch`, `PostModelSwitch`; to 10 on `MessageDisplay`. Not enforced on a command hook run with `async: true`. |
| `statusMessage` | no | Spinner message while the hook runs |
| `once` | no | Only honoured in skill frontmatter; ignored in settings files |

### Command handler fields

| Field | Required | Notes |
|---|---|---|
| `command` | yes | Shell command, or the executable when `args` is present |
| `args` | no | Argument vector; spawns the executable directly, no shell |
| `async` | no | Runs in the background without blocking. **The hook's output is discarded.** |
| `asyncRewake` | no | "runs in the background and wakes Claude on exit code 2. The hook's stderr, or stdout if stderr is empty, is shown to Claude as a system reminder so it can react to a long-running background failure" |
| `shell` | no | `"bash"` (default) or `"powershell"` |

> **Important limitation** (quoted): "Async and asyncRewake hooks can only wake
> Claude or provide a system message on failure — they cannot add context or
> influence decisions on successful completion. They are fire-and-forget for
> most outcomes."

That limitation is why layer 2 (C-15.2) delivers a notice by **exiting 2 with
the notice text on stderr** and exits **0 silently** when it has nothing to say:
exit 0 from an `asyncRewake` hook reaches nobody.

## 2. Input JSON

Common fields on every event: `session_id`, `prompt_id`, `transcript_path`,
`cwd`, `permission_mode`, `effort` (`{level}`), `hook_event_name`.

- **PostToolUse** additionally: `tool_name`, `tool_input`, `tool_use_id`, and
  the tool's output. The field table on the page names it `tool_response`; the
  PostToolUse example payload on the same page names it `tool_result`. Both
  strings are present in the installed binary (see section 5), so a hook that
  needs the output **must read both keys**.
- **SessionStart** additionally: `session_start_reason` (`"startup"`,
  `"resume"`, `"clear"`, `"compact"`, `"fork"`) and an optional `model`.
  `session_start_reason` does **not** appear in the installed 2.1.260 binary
  (section 5); v1's shipped hook reads `source` and works today, so a hook must
  accept either key. `matcher` on `SessionStart` filters on that start reason.
- **UserPromptSubmit** additionally: the prompt text. The page's field list
  calls it `prompt`; its example payload calls it `user_prompt`. Read both.
  No matcher support: it fires on every prompt submission.

## 3. Exit codes

**Exit 0** — success. Stdout beginning `{` and ending `}` is parsed as
structured JSON output; plain-text stdout is written to the debug log, except
on `UserPromptSubmit`, `UserPromptExpansion`, `SessionStart`, and
`PostModelSwitch`, where it is added as context Claude can see.

**Exit 2** — blocking error, per event:

| Event | Effect of exit 2 |
|---|---|
| `PreToolUse` | Blocks the tool call |
| `UserPromptSubmit` | Blocks prompt processing **and erases the prompt** |
| `SessionStart` | Blocks session startup; stderr is the blocking reason |
| `PostToolUse` | Does not block (the tool already ran); **shows stderr to Claude as a system message** |
| `Stop` / `SubagentStop` | Prevents stopping |

Consequences this lane is built on: a `SessionStart` or `UserPromptSubmit` hook
that has notices to surface must exit **0** and put them on stdout (exit 2
there would block the session or erase the user's prompt); a `PostToolUse` hook
that has a notice must exit **2** and put it on stderr.

**Other exit codes**: with valid JSON on stdout the JSON decides and the exit
code is ignored; with invalid JSON or plain text it is a non-blocking error and
the action proceeds.

## 4. JSON output schema

```json
{
  "continue": true,
  "stopReason": "string",
  "suppressOutput": false,
  "systemMessage": "string",
  "additionalContext": "string",
  "hookSpecificOutput": {
    "hookEventName": "string",
    "permissionDecision": "allow|deny|block",
    "permissionDecisionReason": "string",
    "additionalContext": "string"
  }
}
```

`additionalContext` is set **at the top level** for `SessionStart` and
`UserPromptSubmit`, and **inside `hookSpecificOutput`** for tool events.
`hookEventName` must match the firing event name.

Supported output fields per event, as listed on the page:

- `PostToolUse`: `systemMessage`, `additionalContext`, `terminalSequence`
- `SessionStart`: `systemMessage`, `additionalContext`, `terminalSequence`
- `UserPromptSubmit`: `systemMessage`, `additionalContext`, `updatedPrompt`,
  `terminalSequence`

v1 emits `{"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}}`
for both session events (`subfleet/cli.py:cmd_session_hook`) and the harness
accepts it; v2 keeps that byte shape so the surfaced text does not change
across the cutover, and it is also valid under the documented top-level form
because plain-text stdout is added as context on these two events anyway.

## 5. What the installed binary actually contains

Measured on 2026-09-05 against
`/Users/maxghenis/.local/share/claude/versions/2.1.260` (a Mach-O arm64
executable) with `LC_ALL=C grep -a -o -c <literal>`:

| Literal | Occurrences |
|---|---|
| `asyncRewake` | 12 |
| `statusMessage` | 139 |
| `additionalContext` | 179 |
| `tool_result` | 660 |
| `tool_response` | 42 |
| `user_prompt` | 7 |
| `session_start_reason` | **0** |

Read: `asyncRewake` is real in the shipped harness, not just documented.
`session_start_reason` is documented but absent from this build, so the
documentation page describes a version newer than the one installed; a hook
that needs the start reason on 2.1.260 must read `source`, which is what v1's
`bin/subfleet-hook` does. A string count is evidence that the literal is in the
binary, not proof of how it is used; the behavioural claims above come from the
documentation page, not from this grep.
