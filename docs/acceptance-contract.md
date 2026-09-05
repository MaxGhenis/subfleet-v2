# Acceptance contract

Version 1, 2026-09-05. This document binds milestones 1 to 3 of `plan.md`. Code is built against it; tests cite its clauses by number (for example `C-5.4`). A clause changes only by editing this file in the same commit as the code and tests that depend on it. Where this file and `plan.md` disagree, this file wins.

Language: Python 3.12 or newer, standard library only. Packaging with `uv`. No bash runners; the one bash file is the never-rules guard hook, copied byte for byte from v1.

## 1. Identifiers and vocabulary

- **C-1.1** A job id is `YYYYMMDD-HHMMSS-<slug>` in local time, the v1 format, unique in the store. `<slug>` is the `-n` name or the task name, lowercased, `[a-z0-9-]` only, at most 40 characters.
- **C-1.2** An attempt id is `<job id>/a<seq>` with `seq` starting at 1.
- **C-1.3** A lane id is `<provider>-<n>` (`codex-4`, `claude-3`). Lane ids are stable; a lane whose account or credential is rebound gets a new id and the old one is disabled.
- **C-1.4** An account key is `<provider>:<account id>`. For Codex the account id is the `account_id` from the home's `auth.json` claims when present, else the email; for Claude it is the email.
- **C-1.5** A request id is a caller-supplied string or a CLI-generated UUID4, at most 128 characters, printed back on submission.
- **C-1.6** Providers are exactly `codex` and `claude`. Model names in policy are short names (`astra`, `opus`) mapped to provider model ids in `policy.json`.
- **C-1.7** Timestamps in the store are ISO 8601 UTC with a `Z` suffix and second precision unless a column says otherwise. Epoch seconds from providers are converted on write.

## 2. State root and files

- **C-2.1** The state root is `$SUBFLEET_HOME`, default `~/.subfleet/`. Nothing is written outside it except: job workdirs and worktrees the caller named or the daemon allocated under `$HOME`, salvage refs inside the job's git repository, and the `-o` export path.
- **C-2.2** Contents: `state.sqlite3` (plus WAL and SHM), `policy.json`, `lanes.json`, `daemon.sock`, `daemon.lock`, `daemon.log`, `jobs/<job id>/`, `lanes/<lane id>/` (provider homes when a lane has one), `worktrees/`.
- **C-2.3** `jobs/<job id>/` holds `prompt.md` (bytes as sent, written once), `manifest.json`, and per attempt `a<seq>/` with `stdout`, `stderr`, `start.json`, `exit.json`, `deliverable.md`, `lane.log`, and any provider raw stream. Files are created with mode 0600 and directories 0700.
- **C-2.4** A workdir under `/tmp` or `/private/tmp` is refused at submission (exit 7) unless `--allow-tmp`.

## 3. Store

- **C-3.1** One SQLite database, `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`. The schema is `subfleet/store_schema.sql`; `store.py` applies it and records `schema_version`. Migrations are additive and numbered.
- **C-3.2** Every state transition of a job, attempt, notice, lease, or action is one transaction, and every transaction that changes state also inserts an `events` row in the same transaction.
- **C-3.3** Transactions never span a subprocess call, a network call, a filesystem sync, or a sleep. Artifact copying, `ps`, `git`, and provider probes run outside transactions.
- **C-3.4** Readers other than the daemon (the CLI in offline mode, the menu bar app, Logpile) open the database read-only. Only the daemon and the guardian receipt path write, and the guardian writes files, not rows.
- **C-3.5** The CLI refuses to write to a store whose `schema_version` is newer than it knows (exit 1 with both versions).

## 4. Job and attempt state machines

- **C-4.1** Job states: `queued`, `running`, `waiting`, `succeeded`, `failed`, `cancelled`, `lost`. A `waiting` job has `wait_reason` in {`capacity`, `dependency`, `approval`, `uncertain`} and a `next_check_at`.
- **C-4.2** Attempt states and the boundary each one guards:

| State | Set when | Recovery if the daemon dies here |
|---|---|---|
| `reserved` | Lane lease, output lease, and any worktree lease taken; attempt row inserted | Release leases, mark attempt `failed` with class `unknown` and detail `reserved-no-launch`; the job may retry |
| `starting` | Guardian spawned; no `start.json` yet | If `start.json` exists, treat as `running`; else wait `start_grace_s` (10) then run containment (C-5.5) on the guardian pid; if empty, release and retry; if not verifiable, `quarantined` |
| `running` | `start.json` read: pid, pgid, boot id, proc start recorded | If `exit.json` exists, move to `finalizing`; else if the guardian is alive by (pid, boot id, proc start), re-adopt; else run containment; empty means `lost`, else kill survivors and re-check, else `quarantined` |
| `finalizing` | `exit.json` read; classification, salvage, export, notice pending | Re-run finalization idempotently; every step checks for its own completed output before acting |
| `succeeded`, `failed`, `interrupted`, `lost`, `cancelled`, `quarantined` | Terminal | None; `quarantined` needs an operator action (C-5.7) |

- **C-4.3** A job has at most one `accepted_attempt_id`. Acceptance and the notice insert are one transaction. A result from an attempt whose `seq` is lower than the job's current attempt, or whose state is not `finalizing`, is never accepted.
- **C-4.4** A job whose final attempt ends without an rc is `lost`, never `succeeded`.
- **C-4.5** `max_attempts` default 3; a new attempt is admitted only for classes `limited` (next candidate), `transient` (same lane after 60 s, once, then next candidate), and `lost` on a read-only job. `auth-dead`, `cli-too-old`, `content-filter`, and any class on a writable job after the workspace changed require reconciliation first (C-13.3).
- **C-4.6** An attempt never changes lane or model. A new lane or model is a new attempt.

## 5. Process ownership and containment

- **C-5.1** The daemon spawns each attempt through `subfleet-guardian`, which calls `os.setsid()` so it leads a new session and process group, then spawns the provider command as its child with the same pgid, stdout and stderr redirected to the attempt directory, and an environment that contains the lane credential, the provider home, `SUBFLEET_ATTEMPT=<attempt id>`, and `SUBFLEET_JOB=<job id>`. The guardian never receives a secret in argv.
- **C-5.2** Before spawning the provider, the guardian writes `start.json` = {`guardian_pid`, `pgid`, `boot_id`, `proc_start`, `started_at`} via temp file and rename. After the provider exits it writes `exit.json` = {`rc`, `signal`, `finished_at`, `wall_s`, `child_pid`} the same way, then exits with the provider's rc. If the provider cannot be spawned, `exit.json` carries `rc: 127` and `spawn_error`.
- **C-5.3** Boot id is `kern.boottime` seconds from `sysctl`. Process start identity is the `lstart` column of `ps -p <pid> -o lstart=`. A pid is "the same process" only if both match the recorded values.
- **C-5.4** The daemon may signal only a process group it recorded, and only after confirming the group leader's identity by C-5.3.
- **C-5.5** Containment enumeration collects live, non-zombie pids from three sources: `ps -o pid=,stat= -g <pgid>`; a walk of `ps -axo pid=,ppid=` descending from the guardian pid and the recorded child pid; and `ps -axEww -o pid=,command=` filtered on `SUBFLEET_ATTEMPT=<attempt id>`. "Verified empty" means all three returned zero live pids. If any `ps` call fails, the result is "unverifiable".
- **C-5.6** Kill protocol: mark the cancel request; SIGTERM the pgid; wait `term_grace_s` (15); SIGKILL the pgid; enumerate (C-5.5); SIGKILL any survivors individually after re-checking identity; enumerate again. Verified empty releases the workspace and finalizes with `killed_by`. Anything else is `quarantined`.
- **C-5.7** A `quarantined` attempt keeps its leases (lane slot excluded), its workspace, and its worktree. `subfleet runs show` prints the evidence. Resolution is `subfleet kill <job> --confirm-dead` after the operator checks, which re-runs C-5.5 and releases only on verified empty, or `--force-release` which records the operator's override in `events`.
- **C-5.8** The daemon holds an exclusive `fcntl.flock` on `daemon.lock` and writes its pid, boot id, proc start, and version there. A second daemon exits 69. The CLI treats a lock whose recorded identity is dead as no daemon.

## 6. Admission

- **C-6.1** Submission validates before touching the store: workdir exists and is not under `/tmp` unless allowed; prompt file readable; `-o` path's directory exists; sandbox in {`read-only`, `workspace-write`}; `--task`/`--tier` valid per policy or `-m`/`-a`/`-H` pins consistent; a read-only job with `-o` inside `-C` is allowed (the daemon writes it).
- **C-6.2** The payload digest is SHA-256 over the canonical JSON of: prompt bytes (as base64), resolved workdir, git head of the workdir or null, task, tier, pinned model, pinned lane, sandbox, sorted exclusions, resolved `-o` path or null, `allow_desktop`, policy hash. A repeated request id with the same digest returns the existing job id (exit 0); with a different digest, exit 2.
- **C-6.3** Admission of an attempt is one transaction that: chooses the lane and model from the decision (section 11), takes the lane slot lease `lane:<lane id>:slot:<n>`, takes the output lease `out:<resolved path>` if `-o` is set and not already held by this job, takes the worktree lease if writable, inserts the attempt as `reserved`, and records the decision. Launch happens after commit.
- **C-6.4** Caps with defaults in `policy.json`: `max_active_attempts` 4 fleet-wide; `max_in_flight_per_lane` 2, and 1 while the lane has no `provider` reading fresher than `reading_ttl_s` (120); `max_wall_s` 21600 per job; `max_attempts` 3; `max_child_jobs` 8 per parent; `max_tokens_observed` optional per job.
- **C-6.5** Refusals are exit 7 with the fix named: API-key home, workdir under `/tmp`, writable job whose workdir is checked out on `main` or `master`, `-o` path held by a live job of another request id, a writable job for a session id that already has one running from another instance, a second writable job on the same worktree.
- **C-6.6** A writable job runs in a worktree the daemon allocates under `worktrees/<job id>/` from the caller's repo at its current head, unless the caller passes `--in-place`, in which case the daemon takes the worktree lease `worktree:<realpath>` on the caller's directory.
- **C-6.7** `prompt.md` holds the caller's prompt bytes exactly as submitted; it is the digest input and is never rewritten. Prepended text is applied in two places and recorded separately: at submit, the daemon prepends the write template to a workspace-write job unless `--no-preamble` (the sandbox is known at submit); at launch, the Claude adapter prepends the headless block unless the prompt already contains the marker line `<!-- subfleet:headless -->` (the provider is known only after routing). The adapter writes the text actually sent to `<attempt dir>/prompt.sent.md`, the launch's `stdin_path` points at it, and it is recorded as an artifact with role `prompt-sent`.

## 7. Cancellation

- **C-7.1** `kill` inserts a cancel request in the job row (`cancel_requested_at`) in one transaction and returns; the daemon performs C-5.6 asynchronously. `kill --wait` blocks until terminal.
- **C-7.2** If the cancel commits before acceptance, the job becomes `cancelled` even if an attempt later reaches `finalizing`; that attempt's artifacts are kept and it is marked `interrupted`. If acceptance commits first, `kill` returns 0 and prints "already finished".
- **C-7.3** A parent's cancel cancels every child job not submitted with `--independent`, in the same transaction that records the parent's request.
- **C-7.4** A job that is `queued` when cancelled becomes `cancelled` immediately and releases its leases.

## 8. Artifacts and export

- **C-8.1** Every file the daemon publishes (deliverable, `-o` export, `manifest.json`, receipts) is written as: temp file in the destination directory, `os.fsync(fd)`, `os.rename`, `os.fsync` on the directory descriptor.
- **C-8.2** The deliverable is captured once at `finalizing` from the adapter (C-12.6) and recorded in `artifacts` with role `deliverable`, path, bytes, and SHA-256. The raw stream, stdout, stderr, and lane log are recorded with their own roles. Nothing appended to a transcript after the attempt's exit changes the deliverable.
- **C-8.3** The `-o` export is a copy of the accepted deliverable published by C-8.1 after acceptance. A failed export sets `export_error` on the job and emits an event; the job stays `succeeded`.
- **C-8.4** Retention: newest 500 jobs or 2 GiB, whichever binds, computed outside transactions in a maintenance pass; jobs that are active, `quarantined`, have unread notices, hold salvage refs referenced nowhere else, or are a gate's evidence are never pruned. Probe and keepalive results live in `readings` and `events`, not in `jobs`.

## 9. Evidence labels and classification

- **C-9.1** Reading labels: `provider` (a server reported it; source and age recorded), `stale-provider` (the same beyond `reading_ttl_s`), `admission-observed` (this model recently succeeded or was rejected on this lane; remaining quota unknown), `local-backoff` (a routing decision with an explicit expiry and reason), `unknown`. No other label exists. A percentage is rendered only from a `provider` or `stale-provider` reading, and a stale one is marked stale.
- **C-9.2** Classification precedence is authentication, then admission, then quota, and the classifier records which evidence answered each. Outcome classes: `ok`, `limited`, `auth-dead`, `cli-too-old`, `content-filter`, `transient`, `unknown`. The raw rc and signal are always kept beside the class.
- **C-9.3** `auth-dead` requires a 401 from a usage endpoint, an explicit organisation-block message, or a refresh-token-revoked event. A 403 from the Claude usage endpoint on a setup token is expected scope and is not evidence. A limit-looking phrase with a successful `system/init` is `limited`, not `auth-dead`.
- **C-9.4** `limited` carries scope (`account` or a model id), the provider's reset clock when reported, else a guessed clock of now + 3600 s marked `clock_source: guessed`, and the evidence (event, stderr line, or rc). It writes a closure (C-9.6).
- **C-9.5** `transient` covers 5xx, stream disconnects, DNS and TLS failures, and "model at capacity" messages. It never writes a closure; it allows one same-lane retry after 60 s.
- **C-9.6** A closure is (lane, scope, until, reason, clock source, source event). A new closure on the same lane and scope extends `until` but never shortens it. Closures expire by clock; nothing else releases a provider-limit closure except a later `provider` reading that shows the window reset.
- **C-9.7** Codex windows are classified by duration (`window_minutes` 300 is `five_hour`, 10080 is `seven_day`, anything else keeps its minute count as the key), never by slot position.
- **C-9.8** Claude `rate_limit_event`: `unifiedWindows.<window>.utilization` is a fraction in [0, 1] and `resetsAt` is epoch seconds; `status: allowed` yields two `provider` readings with scope `account`; `status: rejected` yields no utilization reading; it yields one `admission-observed` reading for the requested model whose `window` is the literal `admission` (the only value of `window` that is not a duration key), with `resetsAt` as the closure clock when present, and `errorCode` recorded. `overageStatus` is parsed independently and never treated as admission evidence.

## 10. Lanes and credentials

- **C-10.1** A lane row is an immutable binding of provider, account key, credential reference, credential epoch, optional home, `owner` (`v1` or `v2`), and `desktop` flag. The credential reference is a keychain item name (`claude-quota-<email>`, as v1) or a home directory path; the store never holds a secret.
- **C-10.2** Enrolment probes before recording: Codex reads `auth.json`, refuses an API-key login and a free plan, and probes the usage endpoint; Claude runs one Haiku turn under the token with `--output-format stream-json --verbose` and reads the `rate_limit_event`.
- **C-10.3** A lane whose account is the desktop app's current login (`~/.claude.json` `oauthAccount`, re-read each probe cycle) is `desktop` and is never a candidate unless the job has `allow_desktop`. `~/.codex` is observed and never a lane.
- **C-10.4** A lane with `owner: v1` is never a candidate. Ownership changes only by `subfleet lanes transfer <lane> --to v2` (or `--to v1`), which records an event.
- **C-10.5** The credential value is resolved inside the adapter's launch builder and placed only in the child's environment. It is never logged, never written to `lane.log`, never in `manifest.json`.

## 11. Routing evaluation

- **C-11.1** `policy.json` is the routing data. Required keys: `tiers`, `chains` (task to ordered short-model list, one per tier), `fallback: "upward-only"`, `permissions` (task to sandbox), `models` (short name to provider, id, optional effort and scope), `retired` (alias to short name), `desktop_login: "never"`, `caps` (C-6.4), `reset_credits`. The file's SHA-256 is the policy hash on every job.
- **C-11.2** Evaluation for a `--task`/`--tier` job walks the chain from the tier's index upward. For each model, candidates are lanes of the model's provider that are enabled, `owner: v2`, not `desktop` (unless allowed), not in the job's exclusions, not under an unexpired closure for scope `account` or the model id, and with a free slot (C-6.4). The first model with a candidate wins. A `-m` pin evaluates one model and never falls back; `-a` or `-H` evaluates one lane.
- **C-11.3** Codex comparator: eligible when every `provider` window has utilization below `1 - headroom_floor` (floor default 0.15); ordered by `seven_day` reset ascending (soonest first), then lane id. In-flight counts never reorder Codex lanes. Claude comparator: eligible by the same floor on the worst window; ordered by worst-window headroom descending, then in-flight ascending, then lane id. Lanes with no `provider` reading rank after measured lanes on both providers and are "eligible but unmeasured".
- **C-11.4** Before dispatching expensive work (writable, or tier `hard`) to an unmeasured lane, the daemon runs a probe with the model the job wants (C-10.2 style); a `limited` result closes the scope; `ok` records `admission-observed`. For other work the job's first attempt is the probe.
- **C-11.5** The decision record is JSON: chain walked, per-model candidate list with each rejected lane and its reason, readings and closures consulted, chosen lane and model, and the policy hash. It is stored per attempt and printed by `subfleet why <job>`; `subfleet run --why` and `--dry-run` print it without dispatching.
- **C-11.6** The observed 06:33 case (excluded desktop lane, Opus chain exhausted) must evaluate to Astra with the reason "opus: no candidate lanes after exclusions; promoted".

## 12. Adapters

- **C-12.1** `subfleet/adapters/base.py` defines the interface; adapters return data and never touch the store. Methods: `enroll(credential) -> LaneInfo`, `probe(lane) -> list[Reading]`, `build_launch(job, attempt, lane, credential_env) -> Launch`, `classify(attempt_dir, exit_info) -> Outcome`, `attest(attempt_dir, launch) -> Attestation`, `deliverable(attempt_dir, launch) -> bytes | None`, `resume_launch(job, attempt, lane, prompt) -> Launch | None`.
- **C-12.2** A `Launch` is argv, env additions, cwd, and the paths for stdout, stderr, and any raw stream file, plus `native_session_id` when the adapter chose it up front (Claude `--session-id`).
- **C-12.3** Codex launch mirrors v1's `bin/subfleet-codex` argument construction: `codex exec --json -m <model id>`, effort via `-c model_reasoning_effort=<effort>` when the model has one, `--sandbox <read-only|workspace-write>`, `-c hooks=<guard override>` when the guard is armed, `--output-last-message <attempt dir>/last.md`, the prompt on stdin, `CODEX_HOME=<home>`, `CODEX_API_KEY` and `OPENAI_API_KEY` removed from the environment. The thread id is read from the `thread.started` event. `codex exec resume <thread id>` on the same home is the resume launch.
- **C-12.4** Claude launch: `claude -p --model <model id> --session-id <uuid4> --output-format stream-json --verbose`, permission flags per sandbox as v1 `bin/subfleet-claude` builds them, `CLAUDE_CODE_OAUTH_TOKEN=<token>` (or `CLAUDE_CONFIG_DIR=<home>` for a lane home), `ANTHROPIC_API_KEY` removed from the environment, the prompt on stdin. `claude -p --resume <session id>` is the resume launch.
- **C-12.5** Attestation: Codex from the rollout's model field; Claude from the transcript located by session uuid under `~/.claude/projects/`, per-message `model` within the attempt's recorded byte range, exactly one transcript match required. Result is `attested`, `mismatch` (with served model), or `unattested`; never a false positive.
- **C-12.6** Deliverable: Codex from `last.md` when present, else the last `item.completed` agent message in the JSON stream. Claude from the transcript's final assistant text within the attempt's range (v1 `prefer_transcript_text` rule), else the stream's `result` text. An empty deliverable with rc 0 is class `unknown`, not `ok`.
- **C-12.7** Fixtures: `tests/fixtures/<provider>/<case>/` with `stdout`, `stderr`, `rc`, and `expected.json`; cases at minimum: success, hard limit with clock, hard limit without clock, model-scoped limit, credits rejection (the experiment-0 Fable payload), auth dead, refresh-token revoked, CLI too old, content filter, stream disconnect, model downgrade (Claude), spawn failure. Fixtures are redacted: no tokens, no cookies, no `Authorization` values.
- **C-12.8** Fake providers `tests/bin/codex` and `tests/bin/claude` are Python scripts that read `SUBFLEET_FAKE_SCENARIO` and replay the matching fixture, honour `SUBFLEET_FAKE_DELAY_S`, and for the `nested-setsid` scenario spawn a `setsid` grandchild that sleeps 30 s.

## 13. Salvage and workspaces

- **C-13.1** Salvage runs only for a writable job, only at `finalizing`, `lost`, or kill, and only when the worktree's tree hash differs from the baseline recorded at `reserved`. It builds a temporary index from the working tree (tracked and untracked, honouring `.gitignore`), `commit-tree` with the baseline as parent, and writes `refs/subfleet-salvage/<branch>-<utc>-<attempt seq>`. HEAD, the real index, and the working tree are untouched. The ref is recorded as an artifact with role `salvage`.
- **C-13.2** A private salvage ref may be written from any branch. What is refused at admission is a writable job whose workdir is checked out on `main` or `master`, and any push of a salvage ref to a branch named `main` or `master`.
- **C-13.3** Before a new attempt on a writable job, the daemon reconciles: salvage if dirty, record the salvage ref and the current head as the checkpoint, and start the next attempt from the same worktree with a prompt suffix naming the checkpoint. A limit hit after an hour of work loses nothing committed or salvaged.
- **C-13.4** Lane worktrees the daemon allocates live under `$SUBFLEET_HOME/worktrees/`, never `/tmp`. They are removed only after the job is terminal, its salvage ref exists if the tree was dirty, and retention selects them.

## 14. Guard prerequisites (milestone 1)

- **C-14.1** The never-rules hook file is copied byte for byte from v1 (`~/chief-of-staff/subfleet/bin/subfleet-guard-hook`) into `subfleet/guard/never-rules-hook.sh`, and a test asserts the SHA-256 matches the pinned value in `subfleet/guard/TRUST`.
- **C-14.2** Before the first executable Codex job, `doctor` and the daemon run the trust preflight: the installed `codex` version, the hooks trust hash the guard relies on, and the override string are checked against `TRUST`; a mismatch refuses Codex launches with exit 7 and names the fix.
- **C-14.3** A Codex launch with `workspace-write` always carries the guard override; a Claude launch relies on the global hook and `doctor` reports if it is missing from `~/.claude/settings.json`.
- **C-14.4** The isolation matrix test runs each fake provider under each sandbox and asserts the child could not write outside the workdir in `read-only`, could write inside it in `workspace-write`, and inherited no `CODEX_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY`.

## 15. Notices and waiting

- **C-15.1** Every terminal job writes a `notices` row for its caller session in the same transaction as the terminal state. Notice text: job id, outcome class, rc, deliverable path, `-o` path if any, one summary line, and any uncertainty (`quarantined`, export failed, unattested).
- **C-15.2** Delivery layers, most to least reliable: `subfleet wait` in a background Bash call; a PostToolUse `asyncRewake` hook (milestone 4); SessionStart and UserPromptSubmit hooks surfacing rows with state `pending` or `offered`; a best-effort socket push through the v1 mechanism, kept until tickle, muster, and `ping` have tested replacements.
- **C-15.3** Notice states: `pending` (written), `offered` (a delivery attempt was made; transport recorded), `acknowledged` (`notice.ack` from the session, or the session ran `runs show <job>`), `surfaced` (printed by a hook). A notice may be offered more than once; it is acknowledged once.
- **C-15.4** `wait` is a server-side long poll with a per-call deadline of at most 60 s; the CLI loops until the job set is terminal or `--timeout` expires (exit 124). `wait` never wakes headless lanes and never targets a lane session.

## 16. Socket protocol

- **C-16.1** Unix domain socket at `daemon.sock`, mode 0600. Newline-delimited JSON, one request then one response per line. Request: `{"v": 1, "id": "<client string>", "op": "<op>", "args": {...}}`. Response: `{"v": 1, "id": "<same>", "ok": true, "result": {...}}` or `{"v": 1, "id": "<same>", "ok": false, "error": {"code": <exit code>, "message": "...", "fix": "..."}}`.
- **C-16.2** Ops: `submit`, `list`, `show`, `wait`, `kill`, `lanes`, `readings`, `why`, `notice.pending`, `notice.ack`, `ping`, `daemon.status`. Argument and result shapes are the dataclasses in `subfleet/protocol.py`; unknown fields are ignored, missing required fields are exit 2.
- **C-16.3** `submit` returns `{job_id, request_id, created: bool}` where `created: false` means an existing job matched the request id. `wait` returns the terminal states of the requested jobs or `{"timeout": true}` after the deadline.
- **C-16.4** The daemon serves requests on a thread pool; a request handler never blocks on a provider process, a probe, or `ps`. Long-running work is queued to workers with deadlines.

## 17. CLI

- **C-17.1** Verbs (v1 spellings permanent, plan amendment 1): `subfleet` and `subfleet status` (table), `run`, `runs [--mine] [--running] [--last N] [--json]`, `runs show <id> [--out|--err|--json]`, `runs reap` (reconcile now), `wait <id>... | --mine | --last [--timeout S]`, `kill <id> [--wait] [--confirm-dead|--force-release]`, `resume <id> [PROMPT]`, `lanes [list|probe|enroll <credential>|hold <lane> --until|release <lane>|transfer <lane> --to v1|v2]`, `why <id> | --task T --tier X`, `daemon [start|stop|status|logs|install]`, `doctor [--live]`, `ping [--session ID] TEXT`. Aliases: `jobs` for `runs`, `show` for `runs show`, `capacity` for `status`, `notify` for `ping`, `resume-codex` for `resume`.
- **C-17.2** `run` flags: `--task`, `--tier`, `-m`, `-a EMAIL`, `-H CODEX_HOME`, `-C DIR`, `-p PROMPTFILE`, `-o OUT`, `-n NAME`, `-s SANDBOX`, `-x EMAIL` (repeatable), `--allow-desktop`, `--allow-tmp`, `--in-place`, `--independent`, `--parent JOB`, `--request-id ID`, `--wait`/`--attach`, `-d`/`--detach` (default inside a Claude session), `--json`, `--dry-run`, `--why`, `--no-preamble`. Deprecated but accepted through milestone 8 with a stderr note: `-t CLASS`, `--overflow`, `-m sol` (remapped to `astra`).
- **C-17.3** Exit codes, one meaning each across every verb: 0 ok · 1 operational error · 2 invalid input · 3 no lane (message names the earliest reset) · 4 hard limit on a pinned lane · 5 auth dead · 6 provider CLI too old · 7 refused (message names the rule and the fix) · 69 daemon unavailable · 75 queued (only with `--json` and `--no-wait-queue`) · 124 wait timeout · 125 job lost · 130 cancelled. `wait` and `--wait` return the job's rc when it is one of these; a provider rc outside the table maps to 1 with the raw rc in the message.
- **C-17.4** Stdout carries the contract (job id on `run`, the table on `runs`, the deliverable on `runs show --out`); human progress goes to stderr; `--json` emits one JSON object per line and no prose.
- **C-17.5** Offline mode: when the daemon is unavailable, `runs`, `runs show`, `status`, and `kill` work by reading the store read-only and the receipts, and `kill` signals the recorded pgid only after C-5.3 identity checks; everything else exits 69 and prints `subfleet daemon start`.
- **C-17.6** `run` inside a Claude Code session (detected as v1 does, by the session registry and env) defaults to detached and prints the four-line hint v1 prints (out path, log path, wait command, status command).

## 18. Timers (milestone 5, interface only here)

- **C-18.1** Probe cycle every `probe_interval_s` (300) per lane, one probe per idle lane per window; keepalive as `admission-observed` evidence every 5 h 05 m; reset-credit policy as an action (C-19) with v1's rule set; alerts on transition and at most every 6 h while persisting; retention pass every hour; `status.json` for the menu bar app every probe cycle.

## 19. Actions

- **C-19.1** `actions` rows have kind (`merge`, `reset-credit`, later `send`), `op_key` (unique; for a merge the head sha plus PR, for a reset the account key plus the credit id), subject, state `pending` → `executing` → `confirmed` | `failed` | `unknown`, request and result JSON. The row is `pending` before the remote call; `unknown` after a timeout until a read of the remote state settles it. `status`, `why`, and `--dry-run` never create or advance an action.

## 20. Tests and release gates

- **C-20.1** Layout: `tests/unit/` (store, digest, state machine, policy evaluation, classifiers on fixtures, salvage on a temp repo), `tests/fake/` (daemon against fake providers in a temp `SUBFLEET_HOME`), `tests/process/` (guardian, containment, kill, `setsid` escape), `tests/live/` (opt-in with `SUBFLEET_LIVE=1`; one Haiku probe, one wham probe, one real launch per adapter).
- **C-20.2** Time budgets: unit under 30 s total, fake under 60 s, process and git under 20 s, all on this Mac. `uv run pytest -q` runs everything but live.
- **C-20.3** Crash matrix: for each attempt boundary in C-4.2 and for the notice, export, and salvage steps, a test SIGKILLs the daemon at that point with a fake job running and asserts the recovery column. Disk-full is simulated by a write hook that raises `ENOSPC` at each publication step.
- **C-20.4** Release gates before the `run` cutover are plan amendment 10, each as a named test or a recorded measurement in `docs/release-gates.md`.
- **C-20.5** Every test names the clause it proves in its docstring, for example `"""C-5.6 kill quarantines when a setsid grandchild survives."""`.

## 21. Milestone acceptance

| Milestone | Accepted when these tests pass |
|---|---|
| 1 | `fake`: a Codex job survives the submitting process exiting; `kill` finalizes with a salvage ref after verified containment; daemon SIGKILL during `running` re-adopts; SIGKILL during `starting` with no receipt recovers per C-4.2; `nested-setsid` scenario quarantines; guard trust preflight blocks a mismatched hash; isolation matrix; `runs`, `runs show`, `wait`, `kill` offline and online; exit codes table |
| 2 | Claude adapter parses all experiment-0 payloads to `provider` readings; a rejected event closes the model scope with the reported clock; attestation `mismatch` on the model-downgrade fixture; no percentage rendered without a `provider` reading; a hard limit closes the lane and the next attempt carries exclusions |
| 3 | C-11.6 routes to Astra with the recorded reason; `why` prints the decision; pins never fall back; Codex comparator orders by weekly reset; unmeasured lanes rank last and take one slot; golden decision cases from `docs/reports/D-surface.md` and the 06:31 to 06:33 log |

## 22. Open experiments

Numbered as in plan B rev 4: 1 (optional; Claude lane homes), 2 (usage-endpoint rate limits under 14-lane probing), 3 (long-poll `wait` under the harness), 4 (`asyncRewake` in desktop and terminal), 5 (rewritten by amendment 2: `setsid` escape quarantines), 6 (process-group semantics under launchd), 7 (Codex hooks trust hash drift). Each has a written result before the milestone that depends on it.
