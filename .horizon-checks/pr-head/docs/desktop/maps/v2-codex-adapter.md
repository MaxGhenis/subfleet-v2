# v2 Codex provider adapter and guard

Worktree: `/Users/maxghenis/subfleet-v2-lanes/desktop-workspace` at `3f155e5`. All paths below are relative to that worktree unless they are absolute. Besides reading the code, I ran these read-only checks:
- read `launch.json`, `stream.jsonl` and `guard-preflight.json` from real attempts under `~/.subfleet/jobs`
- ran a read-only sqlite query on `~/.subfleet/state.sqlite3`
- read the lane `config.toml` keys and one rollout's record types
- read the launchd plist's PATH

I did not run codex itself. Where I rely on its behaviour, I use the stored help text in `docs/reference/codex-*-help.txt` (codex-cli 0.153.3, per `docs/reference/VERSIONS.md`).

---

## 1. Exact Codex invocation

**How the binary is chosen.**
- `get_adapter("codex")` builds `CodexAdapter()` with no arguments (`subfleet/adapters/registry.py:31-37`), so `codex_bin="codex"` (`adapters/codex.py:214-216`).
- The bare name is looked up on PATH when the guardian starts the process: `subprocess.Popen(argv, cwd, stdin, stdout, stderr)` (`guardian.py:89-96`). The guardian inherits the daemon's environment (`daemon.py:2873-2877`).
- On this Mac the launchd PATH (`~/Library/LaunchAgents/com.subfleet.daemon.plist`) finds `~/.bun/bin/codex`, which links to `@openai/codex/bin/codex.js`. That is the npm launcher, not the repo shim.
- `bin/codex` is on neither the daemon PATH nor this shell's PATH (checked).

**How a launch is built** (`daemon.py:2803-2858`):
1. For workspace-write jobs it re-runs `validate_writable_workdir` (`:2826-2829`).
2. `_validate_home` refuses an API-key `auth.json` (`:1290-1296`).
3. `resolve_credential` returns `{CODEX_HOME: <resolved home>}` (`credentials.py:30-32`).
4. The guard override is fetched unless the job is an isolated review (`daemon.py:2833-2834`).
5. It then calls `resume_launch` or `build_launch` (`:2842-2858`).

**Argument order** (`adapters/codex.py:425-442`):

```
codex exec --json [isolated-review flags] [-m <model_id>] [-c model_reasoning_effort=<effort>]
      --sandbox <read-only|workspace-write> [-c hooks=<override>]
      --output-last-message <attempt_dir>/last.md [resume <thread_id> -]
```

Real `launch.json` (job `20260923-091247-peus-estate-income/a1`):
`['codex','exec','--json','-m','gpt-6-astra','-c','model_reasoning_effort=ultra','--sandbox','workspace-write','-c','hooks={PreToolUse=[…','--output-last-message','…/last.md']`

**Prompt.**
- The prompt is copied atomically to `<adir>/prompt.sent.md` and returned as `stdin_path` (`codex.py:447-452`, `contracts.py:316`).
- The guardian opens it as the child's stdin (`guardian.py:90-95`).
- A fresh exec passes no positional prompt, so Codex reads stdin (`docs/reference/codex-exec-help.txt:13-16`).

**Environment.**
- `env_add` holds `credential_env`, plus `CODEX_HOME=<home expanded>`, `SUBFLEET_ATTEMPT` and `SUBFLEET_JOB` (`codex.py:443-446`).
- `env_remove` is `("CODEX_API_KEY","OPENAI_API_KEY")` (`:450`).
- The daemon also removes `ANTHROPIC_API_KEY` and sets `SUBFLEET_JOB/ATTEMPT/ROOT` (`daemon.py:2875-2877`).
- If `credential_env` names a different `CODEX_HOME` than the lane, the launch is refused (`codex.py:422-424`).

**Per-lane `CODEX_HOME`.** Each lane has its own home: `lane.home or lane.credential.ref`. Real homes live at `~/.subfleet/lanes/codex-N` (store query).

**Sandbox.**
- Only `read-only` and `workspace-write` exist (`contracts.py:117-119`). Policy maps `build` to `workspace-write` and everything else to `read-only` (`default_policy.json` `permissions`).
- A workspace-write launch with no guard override raises `AdapterError` (`codex.py:420-421`).
- `danger-full-access`, `--add-dir`, `--dangerously-*` and `--approve-for-me` are never emitted (none appear in `codex.py`).
- Observed rollout `turn_context.sandbox_policy`: `{type: workspace-write, network_access: false}`.

**Model and reasoning effort.**
- The model is `policy["models"][evidence["model_short"]]["id"]` (`daemon.py:2814,2858`).
- Effort comes only from that model entry's optional `effort` (`daemon.py:2858`, `policy.py:133`). The default policy has `astra: gpt-6-astra, effort: ultra` and `terra: gpt-5.6-terra`; the live `~/.subfleet/policy.json` adds `luna: gpt-5.6-luna`.
- There is no per-job effort override (grep of `daemon.py`, `protocol.py` and `cli.py` found none).

**Service tier / Fast.**
- v2 has no plumbing for it: `grep -i 'service_tier|serviceTier|fast_mode'` over `subfleet/` returns nothing.
- The tier currently comes only from each lane's `config.toml`. Every enabled lane (`codex-1…6`) sets `service_tier = "default"`, and `codex-2` also sets `model_reasoning_effort = "ultra"` (read-only grep).
- The legacy cockpit transport did pass `-c features.fast_mode=true` and a `serviceTier` turn parameter (`…/subfleet-traycer-port/subfleet/subfleet/codex_transport.py:279-280,779-809`).

**How the hooks config is injected.**
- `-c <override>` carries the verified `PreflightResult.override` (`daemon.py:1319-1324`, `codex.py:438-439`).
- Built by `override_string()` (`guard/preflight.py:161-168`):
  `hooks={PreToolUse=[{matcher="Bash|apply_patch",hooks=[{type="command",command="<installed hook>",timeout=60,statusMessage="never-rules guard"}]}],state={"/<session-flags>/config.toml:pre_tool_use:0:0"={trusted_hash="<sha256>",enabled=true}}}`
- The override states the trust hash for its own session-flags hook key inline.
- It is added to every non-isolated launch, read-only ones included. Observed: the read-only resume `20260920-021137-…/a1` carries it.

**Isolated review variant.**
- Launch flags (`codex.py:426-432`; `adapters/isolation.py:19-29,144-155`):
  - `--skip-git-repo-check --ephemeral --ignore-user-config --ignore-rules`
  - `-c project_doc_max_bytes=0`, and `features.<17 names>=false` (including `hooks`, `plugins`, `multi_agent`, `memories`, `shell_snapshot`)
  - `shell_environment_policy.experimental_use_profile=false`, `history.persistence="none"`, `web_search="disabled"`
  - per server, `mcp_servers.<name>.enabled=false` with a dead command or URL
- No hooks override is added and no preflight runs (`codex.py:438`, `daemon.py:2833`).
- Requires `read-only` plus a review root, and refuses the managed-settings environment variables (`isolation.py:32-40`).

**Model probe.** Uses the same `build_launch` path: read-only, with the guard override, prompt "Reply with exactly OK. Do not use tools." (`daemon.py:2062-2081`).

**Front-door shim** (`bin/codex`):
- It only acts on `exec|e|exec-args|e-args|review` when help/version was not requested (`:52-54`). There it:
  - unsets the API keys (`:56`)
  - requires the native v2 install (`:57-61`)
  - picks a lane with `subfleet pick codex [--model]` when `CODEX_HOME` is unset (`:62-88`)
  - always runs `_api-lane-check` (`:91`; `cli.py:479-487`)
- `app-server` and every other subcommand pass straight through with `exec "$REAL" "$@"` (`:95`).

---

## 2. JSONL event parsing, mapping and model attestation

**Capture.**
- The guardian writes stdout to `<adir>/stdout`.
- After containment, the daemon copies stdout to `stream.jsonl` once (`daemon.py:3228-3233`). Nothing in v2 reads Codex events while the turn is running. The only reader is `adapters/codex.py` after exit.
- `_stream_path` prefers `raw_stream_path`, then `stream.jsonl`, then stdout (`codex.py:153-157`).
- `_events` skips bad or truncated lines (`:138-150`).

**Events v2 consumes** (`codex.py:475-497`):
- `thread.started.thread_id` becomes `Outcome.native_session_id`.
- `turn.failed` and `error` are collected as failure signals, and each non-empty stderr line is a signal too (`:498-500`).
- `item.completed` with `item.type=="agent_message"` is the fallback deliverable.

**Events v2 ignores.** Real 0.153.3 streams also contain events that nothing maps (type counts from two real attempts):
- `item.started` / `item.completed` for `command_execution` (fields `command, aggregated_output, exit_code, status`)
- `file_change` (`changes, status`)
- `web_search` (`action, query`)
- `turn.completed` with `usage`

A desktop activity view would need new mapping code for these.

**Classification order** (`codex.py:490-559`, regexes at `:36-64`):
1. `AUTH_RE` gives `AUTH_DEAD`.
2. rc 0, no `turn.failed` and a non-empty deliverable give `OK`.
3. `OLD_CLI_RE` gives `CLI_TOO_OLD`; `CONTENT_RE` gives `CONTENT_FILTER`.
4. `LIMIT_RE` gives `LIMITED` with a `Closure`:
   - scope: an explicit non-"model" `scope`, else a `model_id`/`model` field, else `account`
   - `until` parsed from event fields or text (`_reset`, `:170-208`), else `GUESSED_CLOSURE_S` from now
   - reason `CREDITS` or `PROVIDER_LIMIT`
   - lane id from `launch.lane_id`, falling back to `start.json`
5. `TRANSIENT_RE` gives `TRANSIENT`.
6. Anything else is `UNKNOWN`.

**Deliverable.** Non-empty `last.md` wins, else the last `agent_message` text (`:475-488`).

**Attestation** (`codex.py:561-630`):
- Needs a thread id and `CODEX_HOME`, and no spawn error.
- For resumes, the match is bounded by the guardian's `start.json.started_at` and `exit.json.finished_at` (strict inequality, second precision). If either is missing the result is unattested (`:569-584`).
- It walks `$CODEX_HOME/sessions/**/*.jsonl`, skips files whose rollout name holds a different UUID (`:30-34,589-594`), and gives up as ambiguous above 32 candidates (`:597-599`).
- The first record must be `session_meta` with `payload.id == thread`. Models are taken from `session_meta.payload.model` (fresh runs only) and `turn_context.payload.model` (inside the time window for resumes).
- Exactly one matching rollout is required.
- `served` is the first model that differs from the request, else the last one. The result is `ATTESTED` or `MISMATCH`, with the rollout path as evidence.
- `--ephemeral` runs return a specific unattested reason (`:623-625`).

**Observed on 0.153.3:**
- `session_meta.payload` has no `model` key (keys: `base_instructions, cli_version, …, id, session_id, source, thread_source`). Attestation therefore rests entirely on `turn_context.model`.
- `turn_context` does carry `turn_id` (useful for per-turn attestation later).
- Store totals for codex: attested 120, mismatch 1, unattested 57.
- The resume attempt `20260923-103732-ce-pe-vs-taxsim/a1` is attested as `gpt-6-astra`.

---

## 3. Guard preflight

**When it runs.**
- Every non-isolated Codex launch: fresh, resume and probe (`daemon.py:2079-2081,2833-2834`).
- `doctor --live`, per enabled lane, with `workdir=<state root>` (`doctor.py:404-430`).
- Offline `doctor` validates only the files and the deadline (`doctor.py:368-401`).
- The verdict is published to `<adir>/guard-preflight.json` with one `daemon.log` line (`daemon.py:1326-1351`).
- `not ok` raises `AdapterError(code=7)`, which becomes a launch failure (`:1322-1323,2859-2861`).

**Steps** (`guard/preflight.py:520-689`):
1. **Deadline:** `CODEX_GUARD_PREFLIGHT_TIMEOUT`, default 60 s (`:219-238`).
2. **Overlay files** (`load_guard`, `:185-216`):
   - Hook at `<state root>/guard/never-rules-hook.sh`; TRUST from `SUBFLEET_GUARD_TRUST` or `<state root>/guard/TRUST`.
   - TRUST needs non-empty `hook_sha256, codex_version, reference_hook_path, hooks_trust_hash, override`.
   - The hook's SHA-256 must match and the hook must be executable.
   - `hooks_trust_hash(reference)` and `override_string(reference)` must reproduce the pins. This checks the algorithm matches, not the installed path.
   - Installed TRUST: `codex_version: "codex-cli 0.153.3"`, `reference_hook_path: /Users/maxghenis/chief-of-staff/subfleet/bin/subfleet-guard-hook`.
3. **`jq`** must be present (`:359-362`).
4. **Binary and version:** `shutil.which(codex_bin)` is resolved, then `codex --version` runs in its own session with TERM then KILL at the deadline. The output must equal the TRUST version (`:559-575,408-427`).
5. **Paths:** the workdir and lane home must exist (`:576-588`).
6. **Installed-path values:** `override` and `hooks_hash` are computed from the installed hook path (`:589-590`). The hash is SHA-256 over the hook identity serialized as sorted, compact JSON (`:148-158`).
7. **Cache** (`:268-278,595-608,301-318`):
   - key = `sha256(version|home|override|fingerprint(config.toml,hooks.json)|TRUST json)`
   - marker at `<state root>/guard-cache/guard-ok-<key>.json`, 30-day TTL
   - a hit skips only the app-server probe
8. **Scratch home:** `<state root>/tmp/subfleet-guard-preflight-*/home`, containing copies of `config.toml` and `hooks.json` only (`:613-619`). No credentials or session store are copied.
9. **Probe** (`_hooks_list`, `:430-517`):
   - `codex app-server -c features.plugins=false -c <override>` with `CODEX_HOME=scratch`, `cwd=workdir`, API keys stripped (`:58,563`), and a new session.
   - Sends `initialize` (clientInfo `subfleet-guard-preflight`), `initialized`, then `{"id":2,"method":"hooks/list","params":{"cwds":[workdir]}}`.
   - 1 MB response cap. The process group always gets TERM then KILL (`_stop_probe`, `:365-391`).
10. **Response check** (`:632-648`): no JSON-RPC error; exactly one `data` entry with `cwd == workdir`; exactly one hook with `key == "/<session-flags>/config.toml:pre_tool_use:0:0"`, `enabled:true`, `trustStatus:"trusted"` and `currentHash == hooks_hash`; no `errors`.
11. The marker is written after a pass (`:650-660`). Refusals are never cached.

**Observed real response** (`20260924-074650-fv2-s2-axiom-encoding/a1`):

```
{"cwd":…,"hooks":[{"key":"/<session-flags>/config.toml:pre_tool_use:0:0","source":"sessionFlags","command":"/Users/maxghenis/.subfleet/guard/never-rules-hook.sh","matcher":"Bash|apply_patch","timeoutSec":60,"enabled":true,"isManaged":false,"currentHash":"sha256:29ad…","trustStatus":"trusted"}],"warnings":[],"errors":[]}
```

That probe took 12.1 s, with the first byte at 4.5 s. The last 50 attempts had 44 cached verdicts and 6 fresh ones.

**The hook itself.** It is a PreToolUse deny hook for Bash and apply_patch, covering nine "NEVER" rules. A deny prints `{hookSpecificOutput:{permissionDecision:"deny",…}}` and exits 0. Only denials are logged, to `~/.cache/subfleet-codex/guard-denials.log` (`~/.subfleet/guard/never-rules-hook.sh:1-47,97-111`).

**Deployment rules** (`docs/private-guard-overlay.md:49-66`):
- launchd must carry the guard, TRUST, cache and timeout overrides.
- Old release directories and hook paths must be kept while any live provider's recorded `-c hooks=` still names them.

**Claude-side hook** (`hooks.py:530-604`): it denies Bash commands that start `codex exec|e|review`, `subfleet codex|claude` or the old runners, unless prefixed `SUBFLEET_ATTACHED_OK=1`.

**What a new launch path, including a long-lived `codex app-server`, must keep:**
- **Same binary.** Preflight checks the resolved `which` path (`:559-562`), but launches run bare `codex` (`codex.py:425`), so there is a small window between check and spawn. A warm server should start from `PreflightResult.executable` and re-check the version whenever it is reused.
- **Same argv shape.** Start the server with the verified `-c <override>`. The probe also adds `features.plugins=false`, which exec launches do not. No other `-c` on a guarded server may touch `hooks.*` or `features.hooks`. Extra `-c` flags are not in the cache key and are not probed.
- **One server per lane home.** Never share a server across `CODEX_HOME`s. Do not attach to Codex's own shared local app-server daemon (`codex agents` / `remote-control`, `docs/reference/codex-help.txt:9,18`); how that daemon picks a home is unverified.
- **Check each working directory.** The cache key leaves out the workdir (`:268-278`), but `hooks/list` answers for a specific directory (`:443,637`). A server serving several directories should call `hooks/list {cwds:[cwd]}` on the live server before the first turn in each new directory, in addition to the scratch preflight, which never touches the lane home.
- **Invalidation.** Nothing in v2 checks a process that is already running. If the version, the seed files (including a Fast/`service_tier` edit to `config.toml`), the overlay or TRUST change, or the 30-day TTL passes, a warm server must be drained and restarted under a new verdict. Old hook paths must be kept while it lives.
- **Write access.** A server without the override must refuse any workspace-write turn, matching `codex.py:420-421`.
- **Secrets and containment.** Strip API keys and refuse API-key homes. Keep the server under daemon containment: its own session, TERM then KILL of the process group, and recorded identity, as the guardian does.
- **Isolated reviews** should stay on the exec path. Whether app-server supports ephemeral, no-user-config threads is unverified.

---

## 4. Resume semantics and native thread-id capture

**Capture.**
- For a fresh run the thread id is read from `thread.started` (`codex.py:494-495`) and saved with `native_session_id=COALESCE(?,…)` (`daemon.py:3299-3301`).
- For a resume the id is set before launch in `Launch.native_session_id` (`codex.py:452`) and recorded at start (`daemon.py:2908-2909`).
- A resumed stream starts with `thread.started` carrying the same id (observed).

**Resume submission** (`daemon.py:1031-1065`):
- The source job must be finished, not quarantined and not an isolated review.
- The accepted attempt (or the latest) must have a thread id. For imported v1 attempts the id comes from a `session id: <uuid>` line in stderr or the rollout filename (`:1067-1089`).
- The resume is pinned to the source lane, model, sandbox and workdir or worktree.
- At launch it requires `_resume_lane` (the same lane, or one re-enrolled on the same credential, `:797-810`) and `resume.model_id == policy model id` (`:2839`).

**Resume argv** (`codex.py:460-473,441-442`):
- `-m` and effort are dropped on purpose ("the native thread already holds its resolved model").
- Observed: `['codex','exec','--json','--sandbox','workspace-write','-c','hooks=…','--output-last-message','…','resume','01a0ce4b-…','-']`. The `-` tells Codex to read the prompt from stdin.

**Observed effort loss.** In rollout `…/codex-2/sessions/2026/09/23/rollout-…-01a0ce4b-….jsonl`:
- The original turns' `turn_context` have `effort: "ultra"`.
- The three turns inside the resume window (16:28:51–19:01:53Z) have no `effort` key.
- The effort actually used by resumed turns is unverified, even though codex-2's `config.toml` sets `model_reasoning_effort = "ultra"`.

**Sandbox and hooks placement.**
- `--sandbox` and `-c hooks=` sit before the `resume` subcommand.
- Resumed `turn_context.sandbox_policy` is workspace-write. Whether that comes from the flag or from the stored thread is unverified.
- Whether the hook override is active under `exec resume` is unverified: the rollout has no hook records, and only denials are logged.

**Other paths.**
- A revive with `caller_session` also goes through `resume_launch` (`daemon.py:2842-2851`).
- The shim refuses `exec resume|fork` without `CODEX_HOME` (`bin/codex:62-66`).
- A workspace-write retry (seq > 1) is a fresh exec with a checkpoint suffix, not a resume (`daemon.py:2819-2824`).

---

## 5. Approvals and sandbox escalation today

- **No approval flag.** v2 passes no approval setting; `codex exec` has no `-a` (`codex-exec-help.txt`). Lane `config.toml`s set no `approval_policy` (grep).
- **Observed policy is "never".** Every observed `turn_context` shows `approval_policy: "never"` and `approvals_reviewer: "user"`. That is most likely exec's built-in default (unverified; a project-level config could also set it). With "never", Codex does not stop for approval, and a sandbox refusal goes back to the model as a failure.
- **No escalation.** There is no path to widen access: no `danger-full-access`, `--dangerously-bypass-approvals-and-sandbox`, `--dangerously-bypass-hook-trust`, `--approve-for-me` or `--add-dir`, and `network_access: false` was observed.
- **The only interception at run time** is the never-rules PreToolUse deny.
- **Job-level `approval` is something else.** `wait_reason=approval` (`contracts.py:37-43`, `daemon.py:2421-2423`) is an admission hold waiting on an operator, not a provider tool approval.
- **Legacy behaviour.** The old warm transport answered every approval, permission, user-input or elicitation request with cancel, abort or empty, then interrupted the turn ("Subfleet did not approve it") (`codex_transport.py:582-660`).
- **Plan requirement.** The transition plan requires real approve, deny and cancel round trips; a native-app fallback does not pass the gate (`/Users/maxghenis/subfleet-desktop-transition-20260924.md:57,102`). Nothing in v2 provides this yet.

---

## 6. Extension points for a conversation turn via `codex app-server`

**Where v2 already uses app-server.** Only in two short, metadata-only probes:
- the guard preflight's `hooks/list` (`guard/preflight.py:430-517`)
- isolation inspection, run against the real lane home: `initialize` → `configRequirements/read` → `config/read {includeLayers:true}` (`adapters/isolation.py:53-96`), followed by `codex mcp list --json` (`:99-104`)

No `thread/*` or `turn/*` method appears anywhere in v2 (grep).

**Pieces that can be reused:**
- the line-based JSON-RPC reader with selectors, deadline and size cap (`preflight.py:463-504`, `isolation.py:73-94`)
- `_stop_probe` process-group teardown
- API-key scrubbing
- `override_string` and `PreflightResult.executable`
- `validate_codex_metadata`, if a warm server ever serves isolated turns
- the classifier regexes, which can be applied to `turn/completed` failures and `error` notifications
- `_reset` and `Closure` construction for limits

**Seams that do not fit a warm server.**
- `Adapter` is one launch per attempt (`adapters/base.py:28-80`), and `Launch` is argv, env and file paths (`contracts.py:311-332`).
- Completion is driven by the guardian's `exit.json` (`guardian.py:55-100`; `daemon.py:3226-3301`).
- A warm server needs a new daemon-owned worker per lane home. Each turn needs its own receipts: started, turn id, completed, interrupted or unknown. This matches the plan's line 43.

**Suggested new adapter hooks:**
- `turn_params(job, lane, model, effort, tier, sandbox)`, which must refuse workspace-write without the verified override
- `map_notification(event) -> activity|outcome`
- `attest_turn(home, thread_id, turn_id, model)`, matching `turn_context.turn_id` instead of time windows

**Legacy prior art (not v2).** `codex_transport.py` shows one way to do it:
- It spawns `app-server --listen stdio:// -c features.fast_mode=true` with **no hooks override**; grep for hook, guard or trust finds none (`:273-290`).
- It strips `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_THREAD_ID` and `CODEX_SESSION_ID` (`:264-271`).
- It calls `thread/resume` (never `thread/start`) with `excludeTurns`, `sandbox:"read-only"` and an optional `serviceTier`, and refuses if the returned thread id or model differs (`:400-438`).
- It then calls `turn/start` with `threadId, clientUserMessageId, input[text|localImage], model, cwd, approvalPolicy, approvalsReviewer:"user", sandboxPolicy, effort, serviceTier` (`:796-809`).
- It maps notifications including `turn/completed`, `item/agentMessage/delta`, reasoning summary, command output (label only) and `item/completed` (`:508-580`), and has per-turn `turn/interrupt` (`:155-164`).
- Its wire fields were "checked against `generate-json-schema --experimental`" (`:8-9`). That is the legacy file's own claim, unverified for 0.153.3.

**Must be added before it can be used:**
- inject the hooks override and check `hooks/list` on the live server
- surface approvals as real pending requests
- plumb Fast/effort per turn, with a capability check
- attest the served model per turn
- contain the warm server and cap how long it may hold an account