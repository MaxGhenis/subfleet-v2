# v2 Claude provider adapter: code map for the desktop-workspace transition

Scope: `subfleet/adapters/{base,registry,isolation,claude,claude_stream}.py`, `subfleet/credentials.py` and `subfleet/hooks.py`, plus the daemon and guardian call sites they depend on. Worktree `/Users/maxghenis/subfleet-v2-lanes/desktop-workspace` at `3f155e5`. Nothing was edited, and no daemon, broker or provider process was touched. I ran `claude --version`/`--help` (installed version is 2.1.280; the repo's reference help is 2.1.260 per `docs/reference/VERSIONS.md`). I also ran one pure `classify()` call on synthetic files in the scratchpad.

## 1. Exact claude CLI invocation

**How the adapter is constructed.** `registry.get_adapter("claude")` imports the module and calls `ClaudeAdapter()` with no arguments (registry.py:29-37). That means `claude_bin="claude"`, found on the daemon's PATH (claude.py:519, 529). The daemon's PATH at runtime is UNVERIFIED. In my shell, `claude` resolves to `~/.local/bin/claude` (2.1.280).

**New attempt: `build_launch`** (claude.py:1281-1331)
```
claude -p --model <model_id> --session-id <uuid4> --output-format stream-json --verbose
       [--effort <effort>] <permission_args(sandbox)>
```
- There is no positional prompt. The prompt goes in on stdin from `<attempt>/prompt.sent.md`, which is `HEADLESS_BLOCK + prompt` unless the prompt already carries `HEADLESS_MARKER` (claude.py:1314, 1247-1251, 323-331). The file is written atomically with mode 0600 (298-320).
- `--input-format` is never passed, so the CLI uses its default of `text`.
- Output files are `<attempt>/stdout`, `<attempt>/stderr` and `raw_stream_path=<attempt>/stream.jsonl` (1322-1324).
- `permission_args` (claude.py:1197-1225):
  - **workspace-write:** `--dangerously-skip-permissions` and nothing else.
  - **read-only:** `--permission-mode plan --tools Read,Glob,Grep,WebSearch,WebFetch --allowedTools <same> --setting-sources "" --safe-mode --no-chrome --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands`.
  - **Isolated review:** tools shrink to `Read,Glob,Grep` and `--add-dir <review_root>` is added. `validate_isolated_review` first refuses any sandbox other than read-only, and refuses when a `CLAUDE_CODE_MANAGED_SETTINGS_PATH`-style variable is present (isolation.py:15-16, 32-40).
- A v1-parity test pins this argv: `reconstruct_v1_argv` (claude.py:1991-2029), asserted in `tests/unit/test_claude_adapter.py:397,406`. Any new flag, for example `--include-partial-messages`, has to update that test deliberately.

**Continuation: `resume_launch`** (claude.py:1333-1380)
```
claude -p --resume <native_session_id> [--model <model>] --output-format stream-json --verbose <permission_args(sandbox)>
```
- No `--session-id`, no `--fork-session` and **no `--effort`**. The base signature has no effort parameter (base.py:70-73).
- Isolated review refuses to resume (1354-1356).
- The model falls back to `job.pinned_model` (1347).

**Environment**
- The credential goes into `env_add` via `credentials.resolve_credential` (daemon.py:2831):
  - `keychain-token` → `CLAUDE_CODE_OAUTH_TOKEN` = the raw stdout of `agent-secret get <ref>` for `claude-quota-*` refs when the helper exists, otherwise `security find-generic-password -s <ref> -w` (credentials.py:13-25, 39-50).
  - `env` → `CLAUDE_CODE_OAUTH_TOKEN` from the daemon's environment (33-38).
  - `home` → `CLAUDE_CONFIG_DIR=<ref>` (30-32).
- `env_remove` is `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` (claude.py:120). For read-only launches it also removes `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`, `CLAUDE_MEMORY_STORES`, `CLAUDE_CODE_REMOTE_MEMORY_DIR` and `CLAUDE_COWORK_MEMORY_*` (claude.py:1311-1312; isolation.py:17-18, 43-46).
- The daemon builds the final environment as `os.environ + env_add − env_remove − {CODEX_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY}`, then adds `SUBFLEET_JOB`, `SUBFLEET_ATTEMPT`, `SUBFLEET_ROOT` and `PYTHONPATH` (daemon.py:2873-2881).
- `launch.json` is published with `env_add` removed (daemon.py:2869-2871).

**cwd:** `job.workdir`, which the daemon sets to the job's worktree when there is one, otherwise its workdir (daemon.py:2832; claude.py:1320).

**Process shape:** the daemon runs `python -m subfleet.guardian --attempt-dir … --cwd … --stdout-path … --stderr-path … --launch-fd N [--stdin-path …] -- <argv>` (daemon.py:2883-2894). The guardian:
- calls `setsid` and blocks on a gate byte (guardian.py:60-74);
- writes `start.json`;
- runs `Popen(argv, stdin=<open file>, stdout=<file fd>, stderr=<file fd>)`, then `wait()`, then writes `exit.json` (guardian.py:85-108).

So there is one process per attempt, stdin is a regular file, and the process exits when the turn exits.

**Account and config dir.** The lane comes from the scheduler (`decision.chosen_lane`, daemon.py:2581), not the adapter.
- Observed in `~/.subfleet/lanes.json` (key names and counts only): all 17 Claude lanes are `keychain-token` (16 enabled, one flagged `desktop`).
- So the adapter never sets `CLAUDE_CONFIG_DIR`, and `com.subfleet.daemon.plist` does not contain it.
- Deduced: children use the daemon user's `~/.claude`, both for settings/hooks and for transcripts at `~/.claude/projects/<encoded cwd>/<sid>.jsonl` (claude.py:1238-1245, 157-166). The daemon's HOME is UNVERIFIED.

**Enrol/probe turn** (not a job path): `claude -p "Reply with exactly: ok" --model claude-haiku-4-5-20251001 --output-format stream-json --verbose --max-turns 1`. It runs in a temporary directory with `stdin=DEVNULL`, the environment is `os.environ − ENV_REMOVE + credential`, and no permission flags are passed (claude.py:807-843).

**Discrepancy.**
- `ClaudeAdapter._resolve_keychain_token` unwraps a `claudeAiOauth` JSON blob to its `accessToken` (claude.py:577-585).
- `credentials.resolve_credential`, which is what launches actually use, passes raw stdout through unchanged (credentials.py:50).
- `ClaudeAdapter.credential_env` has no branch for the `env` kind (claude.py:619-627).

Impact on current lanes: UNVERIFIED. It only matters if a `claude-quota-*` item holds a JSON blob.

## 2. How the stream-json output is parsed

`claude_stream.py` is pure; it does no I/O. The adapter reads the whole file after the process exits:
- `stream_summary` tries, in order, `raw_stream_path`, then `stream.jsonl`, then `stdout`, and feeds the first one to `parse_lines` (claude.py:1405-1425).
- The daemon copies `stdout` to `stream.jsonl` once, at finalize (daemon.py:3228-3233).

**Recognised events** (`_summarize`, claude_stream.py:395-500):

| Row | Result |
|---|---|
| `system/init` | `InitEvent` (session_id, model, cwd, claude_code_version, permissionMode, apiKeySource, tools). First one wins. Its presence is the proof of authentication. |
| `system/api_retry` | `ApiRetry` (attempt, max_retries, retry_delay_ms, error_status, error) |
| any other `system/<subtype>` | appended to `unknown_types` as `"system/<subtype>"` |
| `assistant` | `AssistantMessage(model=message.model, text=message_text(...), stop_reason, session_id, error=row.error, raw)` |
| `rate_limit_event` | `RateLimitInfo` (status, resetsAt, rateLimitType, unifiedWindows→`Window`, overage fields, errorCode) |
| `result` | `ResultEvent` (subtype, is_error, `result` text, `errors[]`, num_turns, duration_ms, api_error_status, stop_reason). The last one wins. |
| any other string `type` (`user`, `stream_event`, …) | appended to `unknown_types` |
| non-dict row or missing type | counted as a bad line |

`session_id` is the first non-empty `session_id` on any row (claude_stream.py:411). A truncated last line sets `truncated_tail` (claude_stream.py:357-385, 522-535).

**How content maps to Subfleet records.** No per-event record exists. Everything collapses into one `Outcome` per attempt at finalize.
- **Assistant text:** `message_text` joins only `type=="text"` blocks (claude_stream.py:302-320). Thinking and `tool_use` blocks are dropped (tested: `test_claude_stream.py:275-287`). The deliverable is `result.result`, or else the last assistant text (claude_stream.py:249-257). It is replaced by the transcript's last assistant text inside the attempt's byte range when that text is longer and different (claude.py:1871-1892). The daemon publishes it as `deliverable.md` (daemon.py:3263-3266).
- **tool_use:** ignored completely. The `permission_denials` field of `result` is not parsed; it is only available in `raw`.
- **tool_result:** these arrive as `type:"user"` rows. They are only recorded as the unknown type `"user"`, and their payloads are released while streaming (claude_stream.py:505-507). I ran `classify` on a synthetic stream: normal runs report `unknown_event_types: ['user']` in evidence.
- **result:** OK requires `rc==0`, a result present, `is_error` false and non-empty text. rc 0 with empty text is UNKNOWN (claude.py:1648-1671). `stop_reason=="refusal"` on the result or any assistant row gives CONTENT_FILTER (1640-1646, 1959-1962). `result.errors` feed the regex corpus (claude_stream.py:259-270).
- **Errors.** Classification order is CLI-too-old regex → org block → auth error kinds → auth-signature regex (AUTH_DEAD without init; with init it is a false positive that becomes TRANSIENT) → `rate_limit_event.status=="rejected"` (LIMITED with a closure) → `CREDITS_RE` → `LIMIT_RE` → refusal → success → transient error kinds / `TRANSIENT_RE` / truncated tail → UNKNOWN (claude.py:1523-1710). The error-kind sets are defined at claude_stream.py:60-63.
- **Evidence** records rc, signal, line counts, init, error kinds, unknown types, identity and rate-limit details (claude.py:1480-1511). Readings come from the **last** `rate_limit_event`, and are dropped when the identity check fails (1455-1469).

**Defect, verified by running it.** `CREDITS_RE`/`LIMIT_RE` (step 3) run over assistant and result prose **before** the success check, with no condition on success. In my synthetic stream (init, an assistant saying "…when a usage limit is reached.", `result` success, rc 0), `classify` returned `('limited', …, closure=True)`. Given the same stream with neutral text it returned `ok`.

The daemon treats LIMITED as not OK. It records the closure and retries if the lane is unpinned (daemon.py:3289-3294, 3310-3311). A desktop conversation that talks about quotas, which is likely for Subfleet work itself, would be misclassified and would cool a healthy lane.

## 3. Model attestation and synthetic API errors

**`attest`** (claude.py:1781-1853) uses the transcript only:
1. The session id comes from `launch.native_session_id`, falling back to `outcome.native_session_id`.
2. It globs `<sid>.jsonl` and `*/<sid>.jsonl` under the recorded `projects_dir` and under the default config projects dir (1714-1735). Anything other than exactly one match is UNATTESTED.
3. It reads assistant rows after `transcript_offset` (the file size at launch), discarding a partial first line and filtering on `sessionId` (1743-1779).
4. It skips synthetic rows. Any served model failing `model_matches_requested` gives MISMATCH. Any row without a model gives UNATTESTED. No rows gives UNATTESTED. Otherwise ATTESTED, with the last served model.

`model_matches_requested` accepts an exact match, the `requested-` prefix, or, for aliases `fable|opus|sonnet|haiku`, `claude-<alias>[-…]` (claude.py:277-295).

`classify` separately computes a stream-side `served_model` with `_single_served_model`. It returns None if any non-synthetic assistant row has no model, or if there is more than one distinct model (claude.py:1951-1956).

**How the daemon uses it.** Attestation is persisted as `attempts.attestation`/`model_served` (daemon.py:3257-3262, 3299-3301). A non-attested status is appended to the notice text (3324-3325). However, `ok = not lost and rc == 0 and outcome.cls == OK` (daemon.py:3289) ignores attestation, so **a MISMATCH attempt is still accepted**. The "verified served model" exit gate is recorded but not enforced.

**Synthetic API errors.** `is_synthetic_api_error` (claude_stream.py:74-113) requires all of:
- `type=="assistant"`;
- `is_api_error_message` or `isApiErrorMessage` exactly true;
- `error` in `ERROR_KINDS`;
- `message.model=="<synthetic>"`, role `assistant`, type `message`;
- all four token counters equal to int 0, and every known detail object recursively zero;
- content a non-empty list of text blocks only.

Such rows are excluded from `assistant_models` (238-241), from attestation (claude.py:1820-1822) and from the missing-model check in `_single_served_model` (1952-1953). Their `error` and text still reach classification (`error_kinds`, claude_stream.py:243-247). Tests: `tests/unit/test_claude_synthetic_error.py`.

## 4. Hooks

**Nothing is injected per launch.** The `build_launch` docstring says `guard_override` is "accepted and unused" and that Claude relies on the global never-rules hook (claude.py:1287-1291). `Daemon._guard_override` returns None for any lane that is not Codex (daemon.py:1310-1312). No `--settings` or `--plugin-dir` is passed.

**What actually applies, by sandbox:**
- **workspace-write:** default setting sources, so the effective user `settings.json` loads. With keychain lanes and no `CLAUDE_CONFIG_DIR`, that is `~/.claude/settings.json` (deduced). Currently observed in that file (key names and commands only):
  - never-rules `PreToolUse` on `Bash`, on `Edit|Write|MultiEdit` and on `Artifact`;
  - `subfleet hook PreToolUse` (Bash, 5 s);
  - `PostToolUse` (Bash, `asyncRewake`, 600 s);
  - `UserPromptSubmit` (30 s) and `SessionStart` (30 s);
  - `permissions.defaultMode: bypassPermissions`.
- **read-only:** `--safe-mode` disables hooks (installed help: "customizations (… hooks, MCP servers …) disabled"), and `--setting-sources ""` loads no user settings. So **neither the never-rules hook nor the subfleet hooks run**. The tool allowlist is the only control.
- `doctor.check_never_rules` only greps `~/.claude/settings.json` for a marker string (doctor.py:345-365). It does not look at lane homes, and v2 never writes that entry.

**What the v2 hooks enforce** (hooks.py). These are written only by `daemon install --hooks`, which shows a diff first (desired_groups 628-651, apply 782-803).
- **PreToolUse[Bash]** (592-604): emits `permissionDecision: "deny"` for direct attached runners: `subfleet codex|claude`, `codex-run`, `claude-lane`, `subfleet-codex`, `subfleet-claude` (unless `-d`), and `codex exec|e|review`. Prefixing the command with `SUBFLEET_ATTACHED_OK=1` bypasses it. The parser skips heredoc bodies (530-589). It always exits 0.
- **SessionStart / UserPromptSubmit** (336-375): surfaces pending notices as `additionalContext`, marks them `surfaced` and always exits 0. SessionStart also spawns the `sessions.nudge` worker (378-409). Whether this fires usefully for headless lane children is UNVERIFIED.
- **PostToolUse[Bash]** (429-527): long-polls the session's running jobs under a file lease and exits 2 with the notice on stderr when a job ends.

The never-rules script itself is not in this repo, so what it enforces is UNVERIFIED here.

## 5. Permissions today

- **workspace-write** is full bypass (`--dangerously-skip-permissions`, claude.py:1209-1210). There is no `--allowedTools`, no `--disallowedTools` and no `--permission-mode`. The only brakes are the user PreToolUse deny hooks.
- **read-only** fails closed: plan mode, a tool allowlist, safe-mode, no settings, no MCP, no slash commands (claude.py:1211-1225).
- **No approval round trip exists in v2.**
  - A grep of `subfleet/`, `tests/` and `docs/` for `permission-prompt-tool`, `input-format`, `control_request`, `can_use_tool` and `include-partial` finds hits only in `docs/reference/claude-help.txt`.
  - stdin is a regular file opened read-only by the guardian (guardian.py:90), so the CLI has no live peer to answer a prompt.
  - What 2.1.280 `-p` does when a prompt would be needed with `--permission-prompts` left at its default of `host` and text input is UNVERIFIED.
- The legacy cockpit had no round trip either. Legacy `claude_transport.py` launched with `--input-format stream-json … --permission-mode <mode>`. It answered every `control_request` with an error `control_response` and emitted `needs_attention` telling the user to open the native session (legacy claude_transport.py:55-60, 225-232).
- Installed help shows these relevant surfaces:
  - `--permission-prompts host|none` ("the SDK host or --permission-prompt-tool"; default `host`);
  - `--input-format stream-json`;
  - `--replay-user-messages`;
  - `--include-partial-messages`;
  - `--permission-mode {acceptEdits,auto,bypassPermissions,manual,dontAsk,plan}`.

  `--permission-prompt-tool` only appears inside another option's description, so its status as a standalone flag in 2.1.280 is UNVERIFIED. The exact `control_request` shape (for example a `can_use_tool` subtype) is UNVERIFIED.

## 6. Session identity

- **Minted up front.** The adapter mints a `uuid4` (claude.py:532, 1294) and passes it as `--session-id`, as `Launch.native_session_id` and in `notes.session_id`. `notes` also records `transcript_path`, `transcript_offset` (the transcript's size at launch) and `projects_dir` (1253-1279).
- **Persisted before the provider runs.** The daemon writes it to `attempts.native_session_id` in the `starting` transaction (daemon.py:2907-2909).
- **Captured again at classify.** `session_id = summary.session_id` (first in the stream), falling back to the launch value (claude.py:1470-1473), and goes into `Outcome.native_session_id`. The daemon applies `COALESCE` and stores `transcript_path` (daemon.py:3299-3301).
- **Inconsistent precedence, with no check.** `classify` and `deliverable` prefer the stream id (1470-1473, 1900-1904); `attest` prefers the launch id (1787-1790). Nothing checks that the stream id equals the minted id.
- **Reuse (resume):**
  - `subfleet resume` submits `kind="resume"` with `parent_job_id` (cli.py:1479-1495).
  - `_resume_submission` requires the source job to be terminal with no live attempt. It takes the native id from the accepted or else the last attempt, and pins lane, model, workdir, sandbox, task and tier. It stores `manifest.resume = {native_session_id, lane_id, model_id, …}` (daemon.py:1031-1065).
  - Admission takes the lease `native-session:{lane_id}:{sid}`, held by the job (daemon.py:119-121, 2586-2590). This is the existing single-writer-per-session primitive.
  - At launch the daemon re-checks the lane (`_resume_lane`, 797-810) and model equality, then calls `resume_launch` (2836-2851).
  - A revive resumes `job.caller_session`, which is the operator's own session (2842-2851).
- **Same id on resume.** Installed help for `--fork-session` reads "create a new session ID instead of reusing the original", so a plain `--resume` keeps the id. The adapter's per-attempt `transcript_offset` depends on this: each resume appends to the same transcript.
- **Lane sessions are hidden.** `_lane_session_ids` marks every non-revive attempt's session as a lane run, which C-23.31 makes un-listable, un-nudgeable and un-revivable (daemon.py:1565-1580). A desktop conversation started through `build_launch` would therefore disappear from `sessions`.

## 7. Extension points

**A. Follow-up turn on the same native session** (job-backed, the smallest path):
1. Reuse `kind="resume"`, `_resume_submission`, the `native-session` lease and `resume_launch`. They already give serialized turns, same lane and model, and per-attempt attestation and deliverable ranges via `transcript_offset`.
2. Add `effort` (and optionally a permission policy and a prompt mode) to `Adapter.resume_launch` (base.py:70-80; claude.py:1333-1380). Today effort is silently lost on follow-ups.
3. Replace `apply_headless_block` for conversation turns. The block tells the model there are "NO later turns" (claude.py:122-134) and is prepended to resume prompts too (claude.py:1361). This needs a conversation-mode writer in `_write_prompt_sent` (1247-1251).
4. Relax `_resume_submission`'s requirement that the source be terminal (daemon.py:1036-1040) in favour of queueing behind the lease, so a follow-up can be accepted durably while a turn is running.
5. Give conversation-created sessions a job kind or flag that `_lane_session_ids` (daemon.py:1576-1579) excludes.
6. Images need `--input-format stream-json` with image content blocks, as legacy `input_message` did. The text stdin file cannot carry them.
7. Warm or persistent workers would break the "one guardian per attempt, exit = completion" model (guardian.py:95-108; daemon.py:3236-3262). Per-turn offsets would have to be captured per turn rather than at launch (claude.py:1257-1260).

**B. Streaming incremental events to a daemon event log:**
1. Bytes already land live in `<attempt>/stdout`, because the guardian gives the child a file fd (guardian.py:89-95). A daemon-side tailer per running attempt can read complete lines from a byte offset. That offset is a natural stable reconnect cursor. Nothing tails it today; parsing happens only at finalize (claude.py:1405-1425; daemon.py:3228-3254).
2. Add a pure `normalize_event(row) -> list[Event]` beside `_summarize` in `claude_stream.py`. `parse_lines` already streams (503-538) but only returns a summary. The function should emit:
   - text blocks (and `stream_event` `text_delta` if `--include-partial-messages` is added);
   - `tool_use {id, name}`;
   - user `tool_result {tool_use_id, is_error}`, with content redacted or bounded;
   - a thinking marker only, never the body;
   - `rate_limit_event`, `api_retry`, `result`;
   - an early model-mismatch check on each `assistant.message.model` using `model_matches_requested`.

   Legacy `claude_transport._handle` is a reference for this mapping (legacy :180-232).
3. Protocol: `OPS` has no events operation (protocol.py:18-23; `PROTOCOL_VERSION = 1`, :16). Add a versioned `conversation.events` with a cursor, plus capability discovery. `_boundary` is a crash-test hook, not an event bus (daemon.py:545-547). `_notify` only wakes `wait` (554-556).
4. Adding flags means updating `reconstruct_v1_argv` and its parity test (claude.py:1991-2029; tests/unit/test_claude_adapter.py:397-406).
5. Fix the step-3 prose-limit classification before live conversations run: restrict `CREDITS_RE`/`LIMIT_RE` to non-success outcomes or to stderr and `result.errors`.

**C. Approval request/response round trip.** No path exists today. The options:
1. **SDK-host control protocol.** Launch with `--input-format stream-json --output-format stream-json`, `--permission-prompts host`, and a non-bypass `--permission-mode` instead of `--dangerously-skip-permissions` (claude.py:1209-1210). The guardian would need a bidirectional stdin: a FIFO or socketpair passed via `pass_fds` instead of `open(stdin_path)` (guardian.py:90; daemon.py:2883-2894). The daemon would own the writer, and it needs the event tail from B to see each `control_request` promptly. Scope each approval to (attempt_id, native session id, request_id / tool_use_id). The protocol shape is UNVERIFIED against 2.1.280.
2. **`--permission-prompt-tool`** backed by a Subfleet stdio MCP server that blocks on the daemon. This keeps file stdin and one-shot `-p`. It conflicts with read-only's empty `--strict-mcp-config` (claude.py:1219-1220), and the flag's availability is UNVERIFIED.
3. **A PreToolUse hook** returning allow/deny after asking the daemon, injected per launch via `--settings <json>`. The hooks module already carries a daemon `Client` (hooks.py:57). This does not work for read-only launches, where `--safe-mode` disables hooks. It is also a pre-execution gate, not a native permission prompt.

In every option, adding an approval path must not silently widen what the never-rules deny hooks block. Workspace-write currently depends on those user-settings hooks being loaded.