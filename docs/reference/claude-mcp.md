# MCP servers in detached Claude jobs

C-12.9 implements d714 for Subfleet 2.1.10: a writable detached Claude job sees
no MCP servers unless it names them with `subfleet run --mcp NAME`. The option
is repeatable, and a batch entry uses a `mcp` list. Submission records the names
and snapshots only their server entries under the job directory. Each launch
writes exactly that set into its attempt config. Retries and resumes use the
snapshot, and a damaged snapshot refuses launch. Policy defaults cannot enable
servers. `subfleet runs show JOB_ID` displays the names.

Claude Code's documented sources are user and local scope in `~/.claude.json`,
and project scope in `.mcp.json`; local overrides project, which overrides user,
by replacing a whole server entry. These are the sources Subfleet resolves.
See Anthropic's [MCP installation scopes](https://code.claude.com/docs/en/mcp#mcp-installation-scopes)
and [scope precedence](https://code.claude.com/docs/en/mcp#scope-hierarchy-and-precedence),
verified on 2026-10-01.

The resolver also follows the installed Claude Code 2.1.284 implementation's
project-directory walk: it reads `.mcp.json` from ancestors toward the submitted
workdir, with nearer entries replacing farther ones. Therefore `~/.mcp.json`
is an ancestor project source for a workdir under the user's home; it is not
the user-scope file. `CLAUDE_CONFIG_DIR` and Claude Code's legacy global-config
path are honored. A linked worktree resolves local scope against its main
checkout. The source functions, version, and offsets are recorded in
[`subfleet/adapters/claude_mcp.py`](../../subfleet/adapters/claude_mcp.py).

The [CLI reference](https://code.claude.com/docs/en/cli-reference#cli-flags)
documents `--strict-mcp-config` as limiting MCP configuration to the explicit
`--mcp-config`. Every writable detached launch passes both flags, with an empty
inline document by default. An opt-in passes a file containing only the named
servers. An unknown name is rejected before a job is created.

The launch audit covers all production callers of `ClaudeAdapter.permission_args`:

| Launch path | MCP behavior |
| --- | --- |
| `ClaudeAdapter.build_launch` | Detached jobs use C-12.9; read-only and isolated jobs retain their existing empty configuration. |
| `ClaudeAdapter.resume_launch` | Detached resume and session-revive jobs use C-12.9. A managed resume retains its source job's opt-in. |
| `Daemon._execute_probe` | Calls `build_launch` with `read-only` and no opt-in, preserving the existing probe configuration. |
| `conversations.launch.claude_launch` | Calls `permission_args` for `read-only` only. Writable `ask`, `accept-edits`, and `bypass` turns construct their existing flags in `claude_turn.argv`; d714 does not change them. |
| `reconstruct_v1_argv` | A test helper with no production callers; it records the writable restriction alongside the existing v1 parity exceptions. |

The daemon dispatches conversation turns through the conversation launch path
before selecting detached `build_launch` or `resume_launch`. Conversation turns
are outside d714, including their existing read-only configuration. Regression
tests compare the full conversation argv for every permission mode, both new
sessions and resumed sessions, against the pre-change arguments.
