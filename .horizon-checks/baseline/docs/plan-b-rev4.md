# Subfleet, rebuilt from scratch

Plan of record candidate, revision 4. Written 2026-09-05 by the Fable session Max asked; inputs are the private tree at `~/chief-of-staff/subfleet` (HEAD 57a851a, 859 tests green this morning), the memory files that record its incidents, the live state and ledger, four read-only lane reports and two inline lenses (index in appendix B), one Astra clean-room design, four live probes of the Claude CLI, and one Astra adversarial review whose findings are all dispositioned below. Every mechanism claim names the file or the observation it comes from.

## Verdict

Rebuild it as one supervised job daemon with a SQLite job store, provider adapters in Python, a capacity model that says "unknown" out loud, and a Claude-lane sensor that already exists: the headless stream's `rate_limit_event`, which this session verified on two setup-token lanes at 07:2x this morning and which reports five-hour and seven-day utilization with reset clocks on every attempt (experiment 0 below). Keep the scars (the salvage trap, the never-rules guard, the reset-credit policy, upward-only routing, the desktop login as last resort) as tested invariants, and move the session-resurrection tools and the gates out of the dispatcher's core into clients of the job store. The single biggest change is that a run stops being a bash process with an EXIT trap and becomes a row the daemon owns; kill, wait, notify, orphan, resume, and provenance all follow from that. An Astra adversarial review of revision 3 (reports/G-review.md) found five blockers, all in the gap between that sentence and a buildable specification: tree containment, a crash protocol across spawn, receipt, export, and notice, worktree ownership for salvage, guard parity as a launch prerequisite, and a rollback that fences v2 before reviving v1. Revision 4 closes each of them, and milestone 0 now ends with a binding acceptance contract rather than a list of experiments.

## What subfleet is today

| Measure | Value | Source |
|---|---|---|
| Python package | 23 modules, 15,084 lines, stdlib only | `wc -l subfleet/*.py` |
| bin scripts | 3,464 lines: 2,735 bash (`subfleet-claude` 899, `subfleet-codex` 600, `subfleet-guard-hook` 644, `subfleet-guard` 341, others) and 729 Python (mirror, statusline) | `wc -l bin/*` |
| Tests | 31 files, 18,028 lines, 859 passed + 3 skipped in 181 s | pytest this morning |
| README | 700 lines; it is the specification | `README.md` |
| Launchd jobs | 4 (watchdog 30 min, keepalive 5h05m, mirror 60 s, revive 120 s) | `~/chief-of-staff/launchd` |
| Claude Code hooks | 3 (PreToolUse guard, UserPromptSubmit, SessionStart) + statusLine | `~/.claude/settings.json` |
| State | 3.1 GB; 500 runs (capped), 107 gates, 5,753 tickle records, 44 top-level entries including six lock files and a lock directory | `state/subfleet` |
| CLI verbs | 26 public + 7 hidden | `subfleet --help` |
| Env var families | 4 (`DELEGATE_*`, `CARPOOL_*`, `CLAUDE_LANE_*`, `SUBFLEET_*`) | grep of package and bin |
| Names in 10 weeks | 3 (ai-quota, carpool, subfleet) | git log |
| Unmerged branches | 10, including a 17.6k-line Traycer port and the 2026-09-02 dispatch-stall fix (`claude/nifty-mahavira-661962`, one commit ahead of master and 65 behind it; `~/bin/subfleet` runs master, so the memoised rollout scan and the queued exit code described in memory are not what runs today) | `git worktree list`, `git log master..claude/nifty-mahavira-661962` |
| Ledger outcomes, last 500 runs | rc 0: ~400; rc 4 (hard limit): 54; killed (-9): 19; SIGTERM (143): 13; never finalized: 17 | `runs/*/meta.json` |

Five products share the package: a capacity observatory (probes, table, alerts, brief, menu bar), a job runner (detach, salvage, attest, ledger, notices), a router (task and tier to model to lane), a session-resurrection kit (tickle, muster, revive, mirror, handoff), and a governance layer (gates with merge, never-rules guard for Codex, reset-credit policy, keepalive). Only the first three serve the stated purpose. Max's own definition (2026-08-28): "the point of subfleet is to use multiple subs seamlessly for each provider."

## What I watched it do this morning

These are observations from this session, in the order they happened, each one a requirement for the rebuild.

| Time (EDT) | Observation | Where the behaviour lives |
|---|---|---|
| 06:03 | The cached table shows Claude lanes at 1193% / 4974% and 742% / 2046%, and eight lanes as "? ?" while marked OK (one of the eight carries model cooldowns the compact table hides). | Reproduced exactly by the Astra audit (reports/B-capacity.md, section 2): the numerator is every completed attempt's input, cache, and output tokens in the trailing window (`capacity.rolling_token_sums`); the denominator is the largest account-wide hard-limit token total ever recorded (`learned_capacities`), for these two lanes 7,887,474 and 16,409,292 tokens from 2026-08-22, stored for both windows by one event that never knew which window bound it; the denominator never expires, newer model-scoped limits never replace it, and the ratio is uncapped (`capacity._ratio`). The "?" lanes have no denominator at all and read OK because `status == "ok"` accepts `score is None`. |
| 06:31 | Five Opus research dispatches: four landed on `max@rulesfoundation.org`, the account the desktop app and every interactive session are using. | `delegate._capacity_candidates` ranks the desktop login last, but `_blind_lane_filter` drops every blind lane that already carries a run, leaving the login as the only candidate. |
| 06:32 | `subfleet kill` on four fresh runs: three "escalated" to SIGKILL and now show as ORPHANED with no rc. | `run_ledger.kill_run` escalates after a wait; an escalated kill skips the runner's EXIT trap, so nothing finalizes the row. |
| 06:33 | Re-dispatch with `-x` for the active login: "no dispatchable Claude lane for Opus" while the table says 9/14 lanes dispatchable, and no promotion to Astra although the routing record lists astra as available. | Table predicate (`enrolled and status == ok`, `capacity._claude_rows`) differs from picker predicate (`_blind_lane_filter` drops a blind lane with any run in flight; `BLIND_LANE_MAX_IN_FLIGHT = 0`). Model choice (`select_semantic_model`) reads family-level telemetry, which called Opus available; the lane-level pick then found no candidate and exited 3 at `delegate.py:1215` without trying the next model in the chain. |
| 06:33 | The Sonnet leg accepted `-x`, then re-picked onto the excluded account after a limit. | `bin/subfleet-claude pick_lane` only excludes lanes it rejected itself; the dispatcher's `-x` list is not passed to the runner. |
| 06:33 | Eight enrolled lanes have `confidence: estimated` and `dispatch_score: null` in the routing record. | `capacity._claude_rows`: setup tokens cannot read usage (403), so nothing measured exists for them. |
| 06:32 to 06:47 | Every research lane, all of them read-only, wrote a salvage ref into the chief-of-staff repo on exit (five refs under `refs/claude-salvage/` and `refs/codex-salvage/`), snapshotting other sessions' dirty files. | `bin/subfleet-claude:319` and `bin/subfleet-codex:495`: the salvage trap fires on every exit, whatever the job's permissions and whether or not the attempt changed anything. |
| 06:47 | This planning session restarted while three lanes finished; the parked notices reached the new session process through the UserPromptSubmit hook on the next prompt. | `notify.append_notice` and `bin/subfleet-hook user-prompt`: the hook path delivered where the socket push had no live recipient. |
| 07:2x | Experiment 0: `claude -p "Reply with exactly: ok" --model claude-haiku-4-5-20251001 --output-format stream-json --verbose` under a setup-token lane's `CLAUDE_CODE_OAUTH_TOKEN` (CLI 2.1.260) emitted one `rate_limit_event` per run on both lanes tried: `unifiedWindows.five_hour.utilization` 0.05 with `resetsAt`, `seven_day.utilization` 0.25 with `resetsAt`, `status: allowed`, `overageStatus`. Six seconds and one Haiku turn per lane. | The current runner uses `--output-format json`, which carries no such event (`bin/subfleet-claude:807`), so the sensor exists and is unread. Raw events are in `reports/experiment-0-rate-limit-event.md`. |
| 07:25 | Follow-up probes. A Fable request on `max@policyengine.org` (which carries a local Fable cooldown) exited 1 with a `rate_limit_event` of `status: rejected`, `overageDisabledReason: out_of_credits`, no windows, and the result text "You're out of usage credits. Switch to another model". A Haiku request on `max.ghenis@gmail.com`, which the cached table shows as EXHAUSTED at 147% of its week, was `allowed` with five-hour utilization 0.42 and seven-day 0.33. | The event fires on rejection too, without numbers, so a per-model limit is an `admission-observed` closure for that model alone; and the table's EXHAUSTED verdict for that lane was false, produced by the same expired denominator arithmetic as the 4974% rows. |

None of these are bugs to patch in isolation. They are one design fact: the system cannot see Claude lane capacity, so it estimates, and every layer above the estimate (table, picker, runner, router) has its own idea of what "dispatchable" means.

## Where the README and the code disagree

The invariant harvest (appendix A) and the surface audit (reports/D-surface.md) found these gaps between the specification and the behaviour. Each is a place where the rebuild's tests should pin the intended rule.

- **Offline cycles can still page.** README line 381 says a cycle where every probe is a network error sends no alerts; `watchdog.evaluate_conditions` computes the Claude scoped-limit conditions before the offline check and returns them unsilenced (`watchdog.py:271-281`).
- **Recovery notices are incomplete.** README line 380 promises one recovery notice when conditions clear; the recovery prefix list at `watchdog.py:732-736` omits `codex-fleet-low`, `codex-capacity-expiring`, `claude-limit`, `codex-app-shadow`, and `codex-resets-idle`.
- **The desktop login rule is stronger than documented.** README line 108 describes a 10-point handicap; the picker sorts the login strictly last (`delegate.py:568-584`) and `_best_dispatchable` excludes it whenever any other lane is dispatchable (`capacity.py:1315-1322`).
- **The carpool transition never ended.** README lines 3-4 promise aliases "for a transition week" from 2026-08-23; the aliasing loops in six shell entry points and `subfleet/__init__.py` have no expiry.
- **Detached rotation depends on `-A`.** README lines 552-553 say a detached run rotates lanes; rotation exists only when the lane was auto-picked (`delegate.py:1231-1232`), so an `-a`-pinned run or a gate peer does not rotate.
- **Exit codes collide.** rc 2 means a usage error in five binaries and also a refused guard preflight in `bin/subfleet-codex`; rc 3 means no capacity, an output collision, changes requested, or a parked Codex thread depending on the verb; rc 97 (`cd` into a missing workdir, `bin/subfleet-claude:794`) is reachable and documented nowhere; Codex usage-limit exhaustion has no exit code of its own.
- **`subfleet notify` collides with `bin/notify`.** The chief-of-staff Telegram transport and the session-inbox push share a verb.

## Why rebuild rather than keep refactoring

Three structural reasons, and one honest counterweight.

1. **Capacity truth is architectural.** Codex has a server endpoint per home (`codex.probe_wham`). Claude lanes are keychain setup tokens whose usage endpoint returns 403 (`bin/subfleet-claude:492`), so the whole Claude side rests on transcript token sums and learned capacities. No amount of scoring repairs a missing sensor, and there is no single picker to fix: `capacity._best_dispatchable`, `cli._capacity_lane_ranking`, the delegate's model-aware candidate filter, and `claude.rank_lanes` rank the same rows by four different rules (reports/B-capacity.md, section 3). The fix is a different sensor and one eligibility engine, which touches enrollment, probing, the runner, the picker, and the table at once.
2. **The process model is spread across five mechanisms.** A run today is a bash wrapper with an EXIT trap, launched by Python under `nohup` in a new session, finalized by the wrapper calling back into Python, watched by four launchd jobs, and reported through Claude Code's private session socket, with parked notices surfaced by two hooks. Each mechanism exists because of an incident, and each has its own lock and state file. A daemon that owns the child processes replaces all five with one.
3. **Scope accreted around Claude Code's account switching.** Tickle, muster, revive, mirror, and the completion push all exist because switching the desktop login restarts sessions. They are valuable and they are not about subscriptions. Inside the dispatcher they multiply state (5,753 tickle records), add a launchd job, and produced the 2026-09-04 headless twin. They belong beside the fleet, consuming its records.

The counterweight: the tests encode ten weeks of incidents, the runners' hardening blocks are correct, and a rewrite that forgets any of them repeats an outage. The plan therefore starts with an invariant ledger (appendix A) and ports each invariant as a named test before the code that satisfies it.

## Design principles

- Honesty is structural. Every capacity reading carries one of six labels that describe its evidence: `provider` (the server reported it, with source and age), `stale-provider` (the same beyond its freshness horizon), `admission-observed` (this model recently succeeded or was rejected on this lane; remaining quota unknown), `local-backoff` (a routing decision with an explicit expiry and reason), `derived` (timing inference, display only), or `unknown`. Only `provider` readings populate a percentage. Token totals are shown as consumption facts, never converted to a percentage. A table cell that is unknown says so.
- Identity is the lane, never the login. The desktop login is not a lane. It is excluded by default; `--allow-desktop` opts in for one job.
- Subscriptions only. API-key homes are refused at enrollment as well as at launch.
- Everything is a job. Dispatches, probes, keepalive pings, reset redemptions, gate rounds, revives: each is a row with a kind, a lane, a start, an end, an rc, and artifacts. `runs`, `wait`, `kill`, and the notice path have one implementation.
- One process owns children. The daemon spawns provider processes in their own process groups, records pid and pgid, and is the only thing that signals them.
- Policy is data. Task and tier to model chains, permissions, fallback direction, reset-credit rules, and lane ordering live in one policy file with a `why` command that prints the evaluated decision.
- The daemon validates contracts. Exclusions, pins, output paths, sandbox modes, and headless prompt blocks are checked at submission and carried on the job row; a runner cannot lose them.
- Boring technology. Python 3.12+, SQLite in WAL mode, launchd for one daemon, a unix socket for the CLI. No bash runners.

## Core model

| Noun | Definition | Stable identifier | Lives in |
|---|---|---|---|
| Account | A subscription identity (email, provider, plan, account id). | provider + account id | `lanes` table, seeded from a roster file |
| Lane | A provider home directory bound to one account, logged in once, dispatchable. | lane id (`claude-3`, `codex-4`) | home dir on disk + `lanes` row |
| Reading | One capacity observation for a lane and scope (account, or model scope such as Fable on a Claude account): windows, used, reset time, state, observed at. | (lane, scope, observed_at) | `readings` table; latest per (lane, scope) materialized |
| Closure | A lane and scope closed until a time, with a reason (provider limit, auth dead, operator hold, cooldown after failure). | (lane, scope, until) | `closures` table |
| Job | One unit of work: kind, requested task and tier or pinned model, workdir, prompt, sandbox, exclusions, caller session, state. | job id (timestamp + slug) | `jobs` table + `jobs/<id>/` directory |
| Attempt | One launch of a job on one lane with one model; carries pid, pgid, native session or thread id, rc, classification, served model attestation. | attempt id | `attempts` table |
| Artifact | Prompt as sent, output, stderr, lane log, salvage refs, git heads before and after. | path under the job directory | filesystem |
| Notice | A completion or failure message addressed to a caller session, with delivery state (pushed, parked, surfaced). | (job id, session id) | `notices` table |
| Decision | The evaluated routing for a job: candidates, readings used, exclusions applied, chosen lane and model, reason. | job id | `decisions` table (JSON column) |

Invariants: an attempt never changes lane (a new lane is a new attempt); a job's exclusions apply to every attempt; a closure created by a provider-reported limit carries the provider's reset clock; a reading older than its window's reset is discarded, never displayed as current; the desktop login's account is never a candidate unless the job says so; a job that ends without an attempt rc is `lost`, never silently `ok`.

Two identity rules from the clean-room design (reports/F-design.md) are adopted: every submission carries a request id (`--request-id`, generated by the CLI and echoed when the caller gives none), persisted before submission, and a repeated request id returns the existing job instead of creating a second one, while the same id with a different payload is an error (this is what makes a re-dispatch after a session restart safe); and a live process is identified by pid plus boot id plus process start time, so a reused pid can never be mistaken for a running attempt.

A lane is an immutable binding of an account, a credential reference, and a credential epoch, with an optional provider home and transcript root. Setup tokens are a first-class credential (that is what runs today); a full-login home is another. Rebinding a home or a keychain item to a different account creates a new lane and invalidates the old lane's readings, closures, and native-resume bindings; the adapter checks the binding before every launch and resume.

An attempt moves through persisted states, each with a defined recovery when the daemon dies at that boundary: `reserved` (lane lease taken) → `starting` (guardian spawned, no start receipt yet: on recovery, look for the receipt, else verify the pgid and release the lease) → `running` (start receipt read) → `finalizing` (exit receipt read; salvage, export, and the notice are written in that order, each idempotent, and the terminal state and its notice row commit in one transaction) → terminal (`ok`, `failed`, `limited`, `killed`, `lost`, `quarantined`). Export writes to a temporary file, renames, and records a digest, so a second finalization after a crash compares digests and never overwrites a different deliverable.

## Architecture

### Processes

- `subfleetd`: one daemon under launchd (`KeepAlive`, `RunAtLoad`). It owns the job store, the scheduler (admission, lane pick, retry on limit), child processes, timers (probe cycle, keepalive, reset policy, alerts, retention), and the local API on a unix socket. It never runs as a child of a Claude session.
- `subfleet`: a thin CLI client over the socket. If the daemon is down it says so and offers `subfleet daemon start`; it never launches providers itself. The one exception is `subfleet doctor`, which works offline.
- Provider children: `claude -p ...` and `codex exec ...`, each spawned by a tiny Python guardian that leads its own process group, holds the lane credential in its environment, redirects stdout and stderr to files under the job directory, waits, and writes a receipt (rc, timings, native session or thread id) before exiting. The guardian exists so that an attempt's exit status survives a daemon crash; it does no scheduling, salvage, or notification. Its start is a handshake: before it execs the provider it writes a start receipt naming its pid, pgid, boot id, and process start time, and the daemon moves the attempt from `starting` to `running` only after reading it, so a crash between spawn and record can never leave an untracked writer. (The clean-room design registers each guardian as its own launchd job; this plan keeps guardians as plain detached processes and lets the daemon re-adopt them by receipt, which is one mechanism fewer. Experiments 5 and 6 test that choice.)
- Tree containment is verified, never assumed. A guardian's death says nothing about its provider's descendants. Before an attempt's workspace is released for any reason (finish, kill, `lost`), the daemon enumerates the processes in the recorded pgid, matches them by boot id and start time, signals survivors (SIGTERM, grace, SIGKILL), and confirms the group is empty; if it cannot confirm, the workspace stays reserved in a `quarantined` state and the job reports it. A terminal label in the database is never the evidence that writers stopped.
- Hooks: the three Claude Code hooks stay, reduced to two responsibilities: block direct provider launches from a session's Bash tool, and surface parked notices at SessionStart and UserPromptSubmit. They call the CLI, which talks to the daemon.

### Survival matrix

| Event | Today | Rebuilt |
|---|---|---|
| Claude session restart or account switch | Detached run survives; notice pushed by session id, else parked. | Same result by construction: the child belongs to the daemon. Notice parked in the store; surfaced by hook or by `wait`. |
| Desktop 15-minute idle SIGTERM of the session's group | Survives (new session). | Survives. |
| Kill of a run | SIGTERM to the pgid, escalate to SIGKILL, trap may not run (observed 06:32). | Daemon sends SIGTERM to the pgid, waits, SIGKILLs, verifies the group is empty by boot id and start time, then runs salvage itself and finalizes the attempt with rc and `killed_by`. The trap is no longer the only finalizer; an unverifiable group quarantines the workspace instead of releasing it. |
| Daemon crash | n/a | launchd restarts it; on start it re-adopts attempts whose guardian is alive, finalizes attempts that left a receipt, and for attempts with no receipt verifies the recorded pgid is empty before marking them `lost` and salvaging; a pgid with survivors is killed first, and one that cannot be verified is quarantined. |
| Daemon wedged (alive, not answering) | n/a | The control loop does no blocking I/O: probes, salvage, mirror passes, reset polls, and keychain reads run in bounded workers with deadlines and cancellation, and database transactions are short. `subfleet jobs`, `show`, and `kill` also work with the daemon down, reading the store and the receipts directly and signalling the recorded pgid; disk-full and a corrupt store stop admissions and keep `show` working. |
| Mac sleep | Provider streams drop; runners retry with backoff. | Same retry policy in the adapter; the attempt records `suspended_s`. |
| Reboot | Runs die; `runs reap` marks rc -9 later. | On start the daemon marks live attempts `lost`, runs salvage, and offers `subfleet resume <job>` (native Codex resume; Claude `--resume` on the recorded session id). |
| Wiped temp dir | Lane clones in `/private/tmp` were lost with 8 commits (2026-09-04). | Job directories live under the state root; the daemon refuses a workdir under `/tmp` or `/private/tmp` unless `--allow-tmp`. |
| Provider hard limit mid-run | Runner re-picks (`-A`), exclusions lost. | Adapter classifies the exit; scheduler writes a closure with the provider's reset clock and starts a new attempt on the next candidate with the job's exclusions intact. |

### Storage

One SQLite file `state.sqlite3` (WAL) with the tables above, plus `jobs/<id>/` directories holding prompt, output, stderr, lane log, and `meta.json` (a denormalized copy for humans and for tools that only read files). Today the capacity path alone touches about 25 files (reports/B-capacity.md, section 5); three of them are appended with no lock at all (`lane-usage.jsonl`, the watchdog's `history.jsonl` writes, `decisions.jsonl`), the legacy cooldown writer in `delegate._save_cooldowns` bypasses the cooldown lock, and every count of in-flight runs reads `meta.json` files unlocked. Retention: newest 500 jobs or 2 GiB of job directories, active jobs exempt, with pins that retention never evicts: unread deliverables, `lost` and `quarantined` work, gate evidence, native-resume bindings, and salvage refs not yet landed. Probes, keepalive pings, and reset evaluations are not jobs with directories; they are rows in the `readings` and `events` tables (the review computed that 14 lanes probed every five minutes would fill a 500-job pool in about three hours). Byte accounting covers streams, workspaces, readings, and the WAL, and admissions pause when active data exceeds the limit. One lock: SQLite's. The 44 top-level entries, six lock files, and lock directory in `state/subfleet` today collapse to this file plus the job directories, a `policy.json`, and a `lanes.json` roster.

### Local API

Unix socket, newline-delimited JSON, requests `submit`, `list`, `show`, `wait` (long poll with a deadline), `kill`, `lanes`, `readings`, `why`, `notice.ack`. The CLI is one client; the menu bar app, the morning brief, and Logpile are others. No HTTP port by default.

### Couplings on undocumented interfaces

Every one of these is read or driven today. The rebuild keeps the ones in the first group, isolates the second group behind adapters that degrade to a documented fallback, and drops the third.

| Interface | Used for | Where | Rebuild treatment |
|---|---|---|---|
| `claude -p` flags (`--model`, `--session-id`, `--resume`, `--output-format json`), `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR` | launching and resuming lanes | `bin/subfleet-claude`; CLI help 2.1.260 | keep; documented CLI surface |
| `codex exec` flags, `CODEX_HOME`, `-c hooks=` override, `codex exec resume`, app-server `hooks/list` | launching, guarding, resuming | `bin/subfleet-codex`, `docs/guard.md` | keep; the trust-hash preflight stays in `doctor` |
| `chatgpt.com/backend-api/wham/usage` and the reset-credit endpoints | Codex capacity and redemptions | `codex.probe_wham`, `codex.consume_reset_credit` | keep; mirrors the upstream Codex client |
| `api.anthropic.com/api/oauth/usage` with `anthropic-beta: oauth-2025-04-20` | Claude capacity for a full login | `claude.probe_oauth_usage` | keep; it is the only Claude usage sensor |
| Claude Code hooks (PreToolUse, UserPromptSubmit, SessionStart) | guard and notice surfacing | `subfleet/hooks.py` | keep; documented |
| Transcript JSONL: per-message `model`, `permissionMode`, `isApiErrorMessage` with reset text | attestation, notice mode, limit detection, tickle classification | `bin/subfleet-claude:594`, `notify._last_permission_mode`, `claude.transcript_limit_events`, `tickle.turn_state` | isolate: one `transcript.py` reader with fixture tests per CLI version; attestation degrades to "unattested", never to a false positive |
| Session registry `~/.claude/sessions/<pid>.json`, unix socket, peer token, `<cross-session-message>` envelope | completion push | `notify.find_session`, `notify.send_to_socket` | isolate: best-effort adapter; the store and hooks are the durable path |
| Login keychain item read by `security find-generic-password -s <service> -w` | desktop token for the usage probe | `claude.keychain_credentials` | isolate: only the desktop row needs it once lanes are homes |
| `~/.claude.json` `oauthAccount` | desktop identity | `claude.identity` | isolate |
| Codex rollout JSONL `rate_limits` and error events under `$CODEX_HOME/sessions/**` | capacity fallback, thread ids, limit signals | `codex.scan_rollout_signals`, `run_ledger._codex_rollout` | isolate; the 2026-09-02 stat storm came from here, so the reader is bounded by filename date and memoised |
| Desktop app session index files under `~/Library/Application Support/Claude/claude-code-sessions/` | sidebar mirror | `bin/subfleet-mirror` | isolate inside `subfleet-sessions`; file copy only |
| statusLine command contract | rate-limit tap | `bin/subfleet-statusline` | drop; the desktop app never invokes it |
| launchd PATH order (`/opt/homebrew/bin` first) | which `claude` runs | `paths.claude_bin` | keep the explicit binary resolution; `doctor` reports shadows |

## Capacity truth

### Sensors per provider

| Provider | Sensor | Scope | Freshness | State |
|---|---|---|---|---|
| Codex | `GET chatgpt.com/backend-api/wham/usage` with the home's access token (`codex.probe_wham`) | account windows classified by duration, never by slot position (`codex.classify_windows`; this morning's payloads carried only a weekly window), plan, reset credits | on demand, cached 120 s | `provider` |
| Codex | rollout `rate_limits` snapshots and usage-limit error events (`codex.scan_rollout_signals`) | account | as written | `stale-provider` (used only when the probe fails) |
| Claude desktop login | `GET api.anthropic.com/api/oauth/usage` with the keychain token (`claude.probe_oauth_usage`) | account, with per-model scoped limits in the payload, reported as percentages | on demand | `provider` |
| Claude setup-token lane | none: the endpoint answers 403 (`bin/subfleet-claude:492`) | | | `unknown` |
| Claude lane, any token | a provider-reported limit in the transcript or the `claude -p` result, with a reset clock (`capacity.record_lane_run`, `claude.transcript_limit_events`) | account or model scope | at the event | `admission-observed`, and a closure until the clock |
| Claude lane, any token | the `rate_limit_event` in the headless stream (`--output-format stream-json --verbose`): `unifiedWindows.five_hour` and `seven_day`, each with `utilization` as a 0 to 1 fraction and `resetsAt`, plus `status` and `overageStatus`; verified on three setup-token lanes this morning (experiment 0). On a rejected request the event has `status: rejected` and no windows | account windows; a model-scoped limit shows up as a rejection of that model, never as a window | per attempt | `provider` when allowed; `admission-observed` closure for the requested model when rejected |

The rebuild keeps exactly these sensors, reads the sixth one, and drops the learned-capacity estimator. The fourth row stops being a gap, with the claim stated as narrowly as the evidence: an allowed Claude attempt supplies account windows at no extra cost; a rejected attempt supplies admission evidence and a reset clock but no windows; an attempt that dies before the event supplies nothing, and nothing is invented for it. Admission (`status`) and overage (`overageStatus`) are parsed independently, because every allowed reading this morning also carried `overageStatus: rejected`. Output is preserved even when the event cannot be parsed. Two units rules follow from Astra's audit: the stream event reports fractions and the OAuth endpoint reports percentages, so they get separate parsers; and a reading names the account windows only, so per-model scopes (a Fable limit with Opus still open) stay `admission-observed` from the attempt's own outcome until a follow-up experiment shows the event carrying them.

### Claude lanes as homes, now optional

Codex lanes work as `CODEX_HOME` directories, each holding its own `auth.json`, refreshed by the CLI itself. Before experiment 0 this plan made Claude lanes the same shape (`CLAUDE_CONFIG_DIR=~/.subfleet/lanes/claude-3`, one full `claude login` each) because that was the only route to a server reading. With the stream event verified, homes buy two smaller things: the OAuth usage endpoint with its per-model scoped limits, and independence from the shared keychain and `CLAUDE_CODE_OAUTH_TOKEN` juggling. That is worth experiment 1 (does a per-directory login keep its own credential and refresh unattended?) as a milestone 5 improvement, and it is no longer on the critical path.

### Ordering when capacity is unknown

One eligibility engine answers three separate questions for every (lane, model) pair: does authentication work, can this model accept a request now, and how much quota remains. Today one `OK` conflates all three.

1. Closed lanes (provider limit, auth dead, local backoff) are out until their clock; every closure records its source event and whether its expiry was reported or guessed.
2. Each provider has one exact comparator. Codex: eligible when every reported window has headroom above the floor, then ordered by weekly reset ascending as the primary key (soonest reset first, so perishable capacity is spent; `capacity._codex_dispatch_score` today), then lane id; in-flight counts never reorder Codex lanes. Claude: eligible by the same floor, ordered by worst-window headroom, then in-flight count, then lane id.
3. The desktop reservation is a Claude rule about the interactive account: any lane bound to the account the desktop app is signed into is excluded unless the job says `--allow-desktop`. For Codex the rule stays as it is today: `~/.codex` is observed and never a lane, and a numbered lane that shares the app's account is dispatchable but last for automatic reset credits.
4. Lanes with `unknown` quota rank after measured ones and are "eligible but unmeasured", never zero and never unlimited. Before expensive work, after a limit, or after stale authentication, the daemon runs a probe job with the model the job actually wants (a Haiku success proves Haiku admission and nothing about Opus); for ordinary work on a lane that recently succeeded, the job's own first request is the probe. A limit reply becomes a closure with its clock; a success records `admission-observed`. A probe never writes a number into a window it did not measure (today's just-in-time probe copies one headroom figure into both windows, `delegate.py:488`).
5. Concurrency on an unmeasured lane is an atomic lease in the store (start with one), never an unlocked count of unfinished ledger rows.
6. A hard limit during an attempt closes the lane and scope with the provider's clock. For a read-only job the next attempt starts at once, carrying the job's exclusions. For a writable job the daemon first reconciles the workspace (salvage, checkpoint, side effects recorded by the attempt) and starts the next attempt from that checkpoint; a limit hit after an hour of work is an hour lost, and the plan does not pretend otherwise.

The keepalive ping stays as activity evidence (`admission-observed` for Haiku) and stops being treated as an exact window reset; a request timestamp is weaker evidence than a server clock.

### Classification precedence

The adapter's classifier answers three questions in a fixed order and records which evidence answered each. Authentication first: a 401 from the usage endpoint or an organisation-block message is `auth-dead`; a 403 from the usage endpoint on a setup token is the expected scope and says nothing; a stream event with `status: rejected` after a successful init is a usable credential. Admission second: `rejected` with `out_of_credits` or a limit message closes the requested model's scope on that lane until the reported clock, or for a conservative local period (one hour, labelled `local-backoff`, as `capacity.py:647` does today) when no clock was reported; whether a rejection is model-scoped or account-wide is decided by evidence, so a rejection followed by an allowed request for another model on the same lane narrows the closure to the model, and two rejections across models widen it to the account. Quota third: only `provider` windows populate numbers. A 5xx, a stream disconnect, or a 429 with no window is `transient` and retried with backoff; an auth-looking phrase in stderr is corroborated against the usage endpoint before it can become `auth-dead` (the 2026-09-03 six-lane parking), and an inconclusive corroboration stays `transient`, never `auth-dead` (today's runner parks the lane on an inconclusive probe, `bin/subfleet-claude:842`). A closure is extended, never shortened, by a later observation; re-enrollment and a confirmed reset redemption supersede it.

Per-model scopes stay first class: a Fable closure on an account leaves Opus open on the same account, exactly as `capacity.model_cooldown_for` does now, and "spend Fable-stranded lanes first for non-Fable work" (Max, 2026-08-26) stays as a policy rule.

### Reset credits and keepalive

Both become daemon timers driven by policy data: one credit per evaluation, furthest natural reset first, minimum interval, app-shadowed lanes last (Max, 2026-08-22; `reset_policy.evaluate`). A confirmed redemption is recorded as an event and the lane reopens, but no window numbers are written until the usage endpoint reports them (today `reset_policy.py:452-528` synthesizes zero-used windows, including a five-hour window nobody observed). Keepalive pings idle lanes every 5h05m and records the ping as activity. Both write jobs, so `subfleet jobs` shows them.

## Routing as data

`policy.json` (illustrative, values from today's README table):

```json
{
  "tiers": ["trivial", "easy", "standard", "hard"],
  "chains": {
    "lookup":         ["haiku", "sonnet", "opus", "astra"],
    "research":       ["haiku", "sonnet", "opus", "astra"],
    "review":         ["haiku", "sonnet", "opus", "astra"],
    "build":          ["haiku", "sonnet", "opus", "astra"],
    "sweep":          ["terra", "terra", "terra", "astra"],
    "authored-prose": ["fable", "fable", "fable", "fable"],
    "strategy":       ["fable", "fable", "fable", "fable"],
    "adjudication":   ["fable", "fable", "fable", "fable"]
  },
  "fallback": "upward-only",
  "permissions": {"build": "workspace-write", "*": "read-only"},
  "models": {
    "fable": {"provider": "claude", "id": "claude-fable-5-1", "scope": "fable"},
    "opus":  {"provider": "claude", "id": "claude-opus-5"},
    "sonnet": {"provider": "claude", "id": "claude-sonnet-5"},
    "haiku": {"provider": "claude", "id": "claude-haiku-4-5-20251001"},
    "astra": {"provider": "codex", "id": "gpt-6-astra", "effort": "ultra"},
    "terra": {"provider": "codex", "id": "gpt-5.6-terra"}
  },
  "retired": {"sol": "astra", "claude-fable-5": "fable"},
  "desktop_login": "never",
  "reset_credits": {"enabled": true, "headroom_floor_pct": 15, "min_interval_min": 30}
}
```

Routing evaluates the chain from the requested tier upward, skipping models whose every lane is closed or unknown-and-probe-failed, and records the full evaluation as the job's decision. `subfleet why <job>` prints it. Pins (`-m`) evaluate one model and never fall back. The observed 06:33 gap (no promotion to Astra when the Opus pick failed) cannot recur because lane pick and model choice are one evaluation.

Admission is bounded by policy data too, with defaults written down: `max_active_attempts` 4 fleet-wide, `max_in_flight_per_lane` 2 (1 while a lane is unmeasured), per-job `max_wall_time` 6 h, `max_attempts` 3, `max_child_jobs` 8, and a per-job `max_tokens_observed` after which the daemon stops admitting further attempts for that job and reports the overshoot. There is no conversion from a task to subscription-window consumption and the plan does not invent one; the defence against another 2.07M-token overnight sweep (2026-08-22) is these caps plus a visible per-lane consumption counter, and a sweep is a set of bounded jobs rather than one long attempt.

## Provider adapter contract

| Method | Claude adapter | Codex adapter |
|---|---|---|
| `enroll(credential)` | a setup token (from `claude setup-token`, stored in the agent keychain as today) or, later, a login home; probe with one Haiku turn and read the stream event; record account, credential reference, and epoch | read `auth.json`, refuse API-key logins (`codex.api_key_login`), refuse free plans, probe wham, record account and epoch |
| `probe(lane)` | one Haiku turn with `--output-format stream-json --verbose` under the lane's token; the `rate_limit_event` is the reading (experiment 0); the OAuth usage endpoint only for the desktop login or a lane home | wham with the home's token; refresh via one tiny `codex exec` when the token is expired (`watchdog.heal_expired_codex_homes`) |
| `launch(attempt)` | `claude -p` with `--model`, `--session-id`, `--output-format stream-json --verbose`, permissions per policy, the lane's token in `CLAUDE_CODE_OAUTH_TOKEN` (or `CLAUDE_CONFIG_DIR` once homes exist); every attempt's stream yields a `provider` reading | `codex exec` with `-m`, `-c hooks=` guard override, sandbox per policy, `CODEX_HOME` = lane home, `CODEX_API_KEY` unset |
| `classify(exit, stderr, stream, transcript)` | the precedence above: authentication, then admission, then quota; classes ok, limited (with scope, clock or local backoff, and the evidence), auth-dead, cli-too-old (400 version, no lane fault), content-filter, transient, unknown; the raw exit status is always kept beside the class | same classes and precedence; content filter is not retried (`bin/subfleet-codex:10`); a usage-limit exit gets its own class instead of today's rc 1 |
| `attest(attempt)` | served model from the transcript's per-message model field, transcript located by session UUID, exactly one match required (`bin/subfleet-claude:594`) | model from the rollout |
| `output(attempt)` | the deliverable is an immutable artifact with a role, captured at process exit: the transcript's final assistant text within this attempt's own transcript range (a resumed transcript is read from the attempt's recorded offset), with the JSON envelope, the raw stream, and any partial output kept beside it and a digest recorded; nothing appended to the transcript after exit can change it, and today's conditional recovery (`prefer_transcript_text`, which keeps the envelope when the transcript is unavailable) becomes the documented fallback. The `-o` file is written once by the daemon, outside the provider sandbox, so a read-only job may name an `-o` inside `-C` | same rule from the rollout |
| `resume(attempt, prompt)` | only when the attempt recorded a native binding (session UUID, transcript root, workdir, model, effort, permissions) and is marked `resumable`; `claude -p --resume <session id>` on the same lane with the same effective settings; refused while another continuation of the same binding is live or when the lane's binding changed; a fresh handoff is offered when no native state exists | same, with `codex exec resume` on the owning home (`resume_codex`); isolated reviews run `--ephemeral` today (`bin/subfleet-codex:356`) and are therefore never `resumable` |
| `salvage(workdir)` | shared: temp index + `commit-tree` to `refs/subfleet-salvage/<branch>-<utc>-<attempt>`; HEAD, index, worktree untouched; a private ref may be written from any branch, and what is refused at admission is a writable job whose workdir is checked out on main or master, or any push of salvage to a branch named main or master. A writable job runs in a worktree the daemon allocated (`git worktree add` under `$HOME`) or in a workdir it holds an exclusive reservation on, and the attempt records a baseline ref of the tree as found at start, so the end-of-attempt snapshot is attributable to this attempt and pre-existing dirty work is preserved once rather than re-snapshotted by every read-only lane (this morning's five refs) | shared; the worktree's git common dir is added to the sandbox's writable roots (`bin/subfleet-codex:309`) |
| `kill(attempt)` | shared: SIGTERM pgid, wait, SIGKILL pgid, salvage, finalize | shared |

The never-rules guard stays a Codex PreToolUse hook armed per launch with the trust hash pinned (`docs/guard.md`), and the Claude side keeps the global hook. The shared `unscoped-search` region and the parity tests carry over unchanged; the guard is the one piece of bash that stays, because Codex runs hook commands through a shell. These are launch prerequisites for the first executable adapter: the Codex adapter cannot launch until the preflight (`hooks/list` reports the hook enabled and trusted, re-run after every lane rotation), the subscription refusal, and the read-only isolation matrix (today's `bin/subfleet-codex:322-472` and `bin/subfleet-claude:769-807`, published as an exact argument and environment table) pass their acceptance tests. The guard's documented unguarded channels (`write_stdin`, effective directory, MCP; `docs/guard.md:145`) mean the tenth rule against `pkill -f` is a bounded safeguard, and the plan says so.

## Surface

### Verbs

| Verb | Does | Replaces |
|---|---|---|
| `subfleet` | status table: lanes, readings with state, closures, running jobs | `status`, `capacity` |
| `subfleet run` | submit a job (`--task --tier` or `-m`, `-C`, `-p`, `-o`, `-n`, `-x`, `-s`, `--allow-desktop`, `--wait`, `--json`, `--dry-run`, `--why`) | `run`, `codex`, `claude`, `-t` legacy classes |
| `subfleet jobs [--mine] [--running] [--last N]` | list | `runs` |
| `subfleet show <job>` | metadata, decision, attempts, artifacts, `--out`, `--err` | `runs show` |
| `subfleet wait <job>... \| --mine \| --last [--timeout S]` | long poll; rc = job rc | `wait` |
| `subfleet kill <job>` | signal the tree, salvage, finalize | `kill`, `runs reap` |
| `subfleet resume <job> [PROMPT]` | native provider resume on the owning lane | `resume-codex`, `handoff` (see continuity) |
| `subfleet lanes [list \| probe \| login <lane> \| enroll <credential> \| hold <lane> --until \| release <lane>]` | lane roster and health | `enroll`, `login`, `pick`, `reset` |
| `subfleet why <job \| --task --tier>` | evaluated routing | `--why`, `pick --json` |
| `subfleet daemon [start \| stop \| status \| logs]` | supervisor control | launchd jobs, `watch`, `keepalive`, `mirror` |
| `subfleet doctor` | offline checks: CLI versions, homes, hooks installed, PATH shadows (the 2.1.87 cask), stale locks | `hooks status`, `errors` |
| `subfleet ping [--session ID] TEXT` | push or park a message into a session inbox (renamed so it stops colliding with the chief-of-staff `bin/notify` transport) | `notify`, `sessions` |

Twelve verbs. `gate` and `sessions` are separate entry points, `subfleet-gate` and `subfleet-sessions`, that speak to the daemon over the same socket. The sessions kit collapses tickle, muster, and revive into one verb, `subfleet-sessions continue --scope interrupted|idle|cold [--session ID | --all]`, with mirror and handoff beside it; three English roots for one family of behaviours was the surface audit's clearest finding.

### Exit codes, one table

0 ok · 1 operational error · 2 invalid input · 3 no lane (with the earliest reset in the message) · 4 hard limit on a pinned lane · 5 auth dead · 6 provider CLI too old · 7 refused (API-key home, main-branch salvage, tmp workdir, output collision, guard preflight) · 69 daemon unavailable · 75 queued · 124 wait timeout · 125 job lost · 130 cancelled. Gate keeps its 0 to 5 map. Every code has exactly one meaning across every verb, which today's surface does not manage (rc 2 and rc 3 each carry several).

### Configuration and environment

One directory `~/.subfleet/` with `policy.json`, `lanes.json`, `state.sqlite3`, `jobs/`, `lanes/<lane>/` homes, `daemon.sock`, `daemon.log`. One env prefix, `SUBFLEET_`, with `SUBFLEET_HOME` as the only override tests need. The `DELEGATE_*`, `CARPOOL_*`, and `CLAUDE_LANE_*` families end.

### The agent contract

The five-line core that lives in `~/.claude/CLAUDE.md` keeps working unchanged. The rest of what agents type today is mapped explicitly: a compatibility parser accepts every v1 invocation through milestone 8 (`runs`, `runs show`, `runs reap`, `status`, `capacity`, `pick`, `resume-codex`, `handoff --to`, `gate pr|plan|continue` with its 0 to 5 codes, `-a`/`-H` pins, `-t` classes, `notify`, `sessions`, `hooks`), prints the new spelling once on stderr, and returns the same exit codes; the review found the earlier claim that the contract "stays valid" false for exactly those verbs. Deliberate breaks (`subfleet codex`, `subfleet claude`, `carpool`, the `CARPOOL_*` and `DELEGATE_*` variables) are enumerated in the cutover checklist and their consumers (skills, launchd lane scripts, memory files) updated before removal. The `bin/codex` shim keeps calling the picker and the API-lane check, and a failed pick fails the shim closed rather than falling through to the desktop home. Three results are reported separately everywhere: the command's own status, the normalized job outcome, and the raw provider exit status.

The core contract:

```
subfleet run --task <task> --tier <tier> -C <dir> -p prompt.md -o out.md   # returns a job id at once
subfleet wait <id>          # run_in_background: the harness notifies on exit; safe to re-run
subfleet jobs --mine        # after any restart, before re-dispatching
subfleet show <id> --out    # the deliverable
subfleet kill <id>          # stops the tree and salvages
```

New protections the contract does not have to mention because the daemon enforces them: the desktop login is never picked; exclusions survive re-picks; `-o` is written once from the attempt's own transcript range, by the daemon, so a read-only job may name an `-o` inside `-C`; a workdir under `/tmp` is refused, and a writable job gets an allocated worktree or an exclusive reservation; a Claude prompt without a headless block gets one prepended by the adapter; a job launched twice with the same request id returns the first job; a tenth never-rule denies `pkill -f` and `kill -9 $(pgrep ...)` by shared token (the 2026-09-04 cross-session kill) as a bounded safeguard, so a caller has to kill by job id.

Today's contract is ten numbered items and 32 lines (reports/D-surface.md, section 4), and six of the known gotchas are protected by nothing but the caller's memory. The rebuild's contract is the five lines above.

## Notices and waiting

The durable path is the store: every finished job writes a notice row for its caller session. Delivery has four layers, from most to least reliable:

1. `subfleet wait` in a background Bash call, which the harness turns into its own completion notification.
2. A documented Claude Code hook. The hooks reference (code.claude.com/docs/en/hooks, fetched 2026-09-05) defines a command-hook field `asyncRewake`: the hook runs in the background and, when it exits 2, its stderr is shown to Claude as a system reminder; the field name is present in the installed binary (2.1.260). The composition: every PostToolUse hook on the Bash tool asks the daemon whether this session has unfinished jobs with no live waiter, and if so arms one waiter per job under a lease in the store (so duplicate hook firings arm nothing twice, and the `if` filter is a cost saver rather than the correlation); the waiter long-polls its job and exits 2 with the notice when the job finishes, or exits 0 silently at the hook's timeout (set explicitly, since the documented default is 600 s) and is re-armed by the session's next tool call. A notice moves from `offered` to `acknowledged` only when a later call from that session reports it consumed; an unacknowledged notice is offered again by layer 3.
3. The SessionStart and UserPromptSubmit hooks surfacing unacknowledged notices (this morning's restart was delivered this way).
4. A best-effort push through Claude Code's cross-session socket (`notify.push_to_session`). It is also today's transport for tickle, muster, and `ping`, which have no submission waiter, so it stays until each of those has a tested replacement, and the acceptance test for layer 2 runs a job longer than the hook timeout and a session teardown during delivery.

## Session continuity, kept beside the fleet

Tickle, muster, revive, mirror, and handoff move to `subfleet-sessions`, a second entry point in the same repo with its own module and tests. They read Claude Code's transcripts and session registry and they submit jobs (a revive is a job of kind `revive` on a lane). Two invariants become locks in the store rather than conventions:

- Ownership is the rule. A lease in subfleet's store binds only launches subfleet makes; the desktop app never takes it, so a lease cannot make a headless revive exclusive against a desktop restart that lands between the census and the launch (the 2026-09-04 twin). Automatic headless revival of externally owned sessions is therefore off by default. Recovery of a cold session defaults to an explicit handoff (a fresh native session with the continuity brief), and native in-place revival runs only for sessions subfleet launched itself or when Max marks a session for it; the census under the lock, the retirement marker, the permission-mode check, the original-model rule, and the age cap (A rows 179 to 192) all stay.
- Tickle stays automatic: it pushes a message into a live session's own inbox and creates no second writer.
- A duplicate live instance of one session id after a restart (the 2026-09-04 amend war) is detected from the registry and reported to both instances; the plan does not claim a notice stops the second instance's writes, so the daemon also refuses to start a writable job for a session id that already has one running from another instance, and the guard hook's `local-main` and `stash-shared` rules remain the last line.

Handoff (Claude transcript to a fresh agent) becomes `subfleet-sessions handoff <session> --to <model>`, which builds the brief exactly as `handoff._build_brief` does and submits a job. Mirror stays a 60 s timer in the daemon; it is a file-copy pass with no provider calls.

## Gates

`subfleet-gate plan|pr|continue` keeps its semantics (fingerprint-bound main approval, one pinned read-only peer, fail-closed merge with `--match-head-commit`, exit codes 0 to 5, four-round cap) and becomes a client: each peer round is a job of kind `gate-review` with the isolation the current `prepare_isolated_review` builds. The 2026-09-04 foreground-timeout death of a peer run cannot recur because the peer is never a child of the caller.

## What the clean-room design changed in this plan

GPT-6 Astra wrote an independent design from the same brief (reports/F-design.md, 10 minutes on `~/.codex-4`). The two designs agree on the shape: one supervisor, SQLite, Python, adapters, jobs with attempts, policy as data, a probe-first stance on unknown capacity, gates and handoff as clients. Where they differ, this is what changed and what did not.

| Astra proposal | Verdict | Why |
|---|---|---|
| Caller-generated request ids; a repeated id returns the existing job | adopted | makes re-dispatch after a restart idempotent |
| Process identity = pid + boot id + start time | adopted | pid reuse after a reboot or a long uptime |
| A guardian process per attempt that writes a receipt, so rc survives a daemon death | adopted, as a detached process rather than a launchd job per attempt | one mechanism fewer; experiments 5 and 6 check it |
| `asyncRewake` hook as the wake path instead of the private socket | adopted | documented field, present in the binary |
| Show transcript token totals as consumption facts, never as a percentage | adopted | same honesty rule, better wording than "drop estimates" |
| Persist the redemption request id before the consume call | adopted | an irreversible action with an interrupted poll |
| Exit codes 69 (daemon unavailable) and 130 (cancelled) | adopted | |
| Global cap on active attempts plus budgets | adopted | 2026-08-22 sweep |
| Test time budgets (30 s unit, 60 s fake-provider, 20 s process and git) | adopted | |
| Every writable job gets an independent clone with its own object store | rejected | Max's rule since 2026-09-04 is worktrees of the main repo under `$HOME`, so commits live in the main object store even when the directory is lost; independent clones cost disk and lose that property |
| Drop automatic tickle and headless revive; replace revive with explicit handoff | open decision 7 | Astra's argument is that an interrupted transcript is a heuristic and establishes no ownership of the session. Tickle only pushes a message into a live session's own inbox and has cost nothing; revive spawns a second writer and produced the 2026-09-04 twin. This plan keeps both behind store leases and a liveness check under the same lock; Max decides whether revive stays automatic |
| Drop the desktop sidebar mirror | open decision 8 | it is a file copy, Max switches accounts daily, and the alternative Astra offers (a catalog with resume links) does not put sessions back in the sidebar |
| TOML config in `~/.config/subfleet`, state in `~/.local/state/subfleet` | not adopted | one directory (`~/.subfleet/`) is simpler to find and to back up; TOML vs JSON is taste |
| Eleven verbs including `artifact` and `events` | partly | `show --out` covers artifact export; `events --follow` is folded into `wait` |

## Adversarial review: findings and dispositions

GPT-6 Astra reviewed revision 3 read-only for 13 minutes (reports/G-review.md). Its verdict: milestone 0 can begin, milestone 1 could not be built against revision 3, and the most valuable change is a binding acceptance contract out of milestone 0. Every finding and what revision 4 did with it:

| # | Finding (severity) | Disposition | Where it landed |
|---|---|---|---|
| 1 | Guardian death is not provider-tree death; `lost` could release a workspace with a writer alive (blocker) | accepted | tree verification by pgid, boot id, and start time before any release; `quarantined` state; experiment 5 acceptance rewritten |
| 2 | No crash protocol across DB, spawn, receipt, export, notice; request id not exposed (blocker) | accepted | attempt state machine with per-boundary recovery; start handshake; idempotent digest-checked export; terminal state and notice in one transaction; `--request-id` with mismatch error |
| 3 | Salvage rules confused refusing a push to main with refusing a private ref on main; changed tree is not ownership (blocker) | accepted | private refs from any branch; writable placement on main refused at admission; allocated worktree or exclusive reservation; baseline ref at attempt start |
| 4 | Guard parity scheduled after executable Codex jobs (blocker) | accepted | preflight, subscription refusal, and isolation matrix are milestone 1 launch prerequisites; tenth rule described as bounded |
| 5 | Rollback leaves v2 timers, hooks, and attempts alive; cutover order mismatched milestones (blocker) | accepted | staged single-owner transfer per capability; rollback fences v2, reconciles writers, then revives v1 |
| 6 | Import inventory incomplete (major) | accepted | import manifest with notices, redemption history, latches, retirement markers, gates, lane identities, incremental cursor |
| 7 | "Agent contract stays valid" was false for `runs show`, `handoff`, `resume-codex`, `gate`, pins (major) | accepted | compatibility parser for every v1 invocation through milestone 8; deliberate-break list; shim fails closed |
| 8 | "Every attempt returns a server reading" overstated; overage parsed as admission would reject working lanes (major) | accepted | claim narrowed to allowed attempts; admission and overage parsed independently |
| 9 | Classifier precedence undefined; inconclusive auth becomes rc 5 today (major) | accepted | classification precedence section; inconclusive corroboration stays `transient` |
| 10 | Codex "5h primary + weekly secondary" repeats the positional-window bug; `live` outside the vocabulary; milestone 2 allowed inferred percentages (major) | accepted | duration-based classification stated; vocabulary unified; milestone 2 is provider-only |
| 11 | Codex weekly-reset waterfall demoted to a tiebreaker; desktop-account rule undefined for Codex (major) | accepted | per-provider comparators; weekly reset is Codex's primary key; desktop reservation is a Claude rule, Codex keeps app-home and shadowed-lane semantics |
| 12 | "Batch waits until a window can hold it" has no estimator; "attempt cost is seconds" false for long writable work (major) | accepted | numeric budgets replace fit claims; writable retries reconcile the checkpoint first |
| 13 | Lane definition required homes while the credential is a setup token; no epoch or rebinding detection (major) | accepted | lane = immutable account, credential reference, epoch, optional home; rebinding invalidates readings and bindings |
| 14 | Resume contract omitted native binding state; ephemeral reviews cannot resume; attestation must respect attempt offsets (major) | accepted | `resumable` capability with a persisted binding; per-attempt transcript range; ephemeral reviews never resumable |
| 15 | `asyncRewake` composition incomplete: 600 s default timeout, duplicate firings, socket removal would break tickle and muster, acknowledgement undefined (major) | accepted | leased waiters re-armed by any tool call; explicit timeout with silent exit 0; `offered` to `acknowledged` on a later call; socket kept until tickle, muster, and ping have tested replacements |
| 16 | A store lease cannot exclude a desktop restart; automatic revival of external sessions keeps the twin (major) | accepted, with a changed recommendation | automatic revival off by default for desktop-owned sessions; handoff is the default recovery; open decision 7 rewritten |
| 17 | The daemon is one failure domain; a hung worker wedges control; CLI useless when it is down (major) | accepted | non-blocking control loop with bounded workers; `jobs`, `show`, `kill` work offline; disk-full and corrupt-store behaviour stated |
| 18 | Last assistant message is not necessarily the deliverable; the export rule contradicted the read-only `-o` refusal (major) | accepted | immutable deliverable captured at exit within the attempt's transcript range with envelope and raw stream kept; `-o` inside `-C` allowed because the daemon writes it |
| 19 | Probes and keepalives as jobs would fill the 500-job pool in hours (major) | accepted | maintenance events live in the readings and events tables; pins for unread, lost, quarantined, gate, and salvage data; byte accounting |
| 20 | "One test per invariant row" is not a disposition; fakes cannot prove CLI compatibility, sandbox, resume, wake-up, attestation (major) | accepted | adjudication of all 220 rows in milestone 0; named real-fixture corpus; opt-in real acceptance before an adapter is promoted |

Rejected: nothing outright. One point of emphasis differs: the review would move guard parity tests themselves into milestone 1; this plan makes the preflight and isolation matrix milestone 1 prerequisites and keeps the byte-identity parity corpus in milestone 8, because the corpus tests the hook file, which does not change in the rebuild.

The review's ten questions, answered:

1. What evidence releases a workspace after a guardian or daemon failure? An enumerated, empty process group matched by boot id and start time; anything less leaves it `quarantined`.
2. What is the persisted transition protocol? The attempt state machine in Core model, with the start handshake, the exit receipt, idempotent export, and the terminal-plus-notice transaction.
3. Who generates the request id, and what if the payload differs? The CLI, unless the caller passes `--request-id`; it is persisted before submission; a mismatched payload is exit 2.
4. What are lane and native-session identities? A lane is account plus credential reference plus epoch (home optional); a native session is provider plus lane plus native UUID plus transcript root, recorded at launch.
5. What is the capacity precedence table? The Classification precedence section: authentication, then admission, then quota, with the transient and local-backoff rules.
6. Which protections must pass before the first writable Codex job? Guard preflight, subscription refusal, the isolation matrix, and salvage-on-verified-kill, all in milestone 1's acceptance.
7. What makes automatic revival exclusive against desktop launches? Nothing can, so it is off by default for desktop-owned sessions.
8. How are sessions woken once the socket goes? It does not go until tickle, muster, and ping have tested replacements; completion uses leased `asyncRewake` waiters plus hook catch-up.
9. What transfers at each cutover step? One owner per capability, transferred in the order dispatch, timers, sessions, gates, each fenced before enabled; rollback in reverse with reconciliation.
10. What are the probe, job, and retention budgets? Probe TTL 120 s and one probe per idle lane per window; the admission caps in Routing as data; retention with pins and byte accounting in Storage.

## Traycer, Logpile, the public repo, and the app

- **Traycer**: integrate loosely, do not build on it. The unified-product doc on the branch says atomic admission and failover need Traycer's Host, which is a signed binary talking to Traycer's production cloud (`~/traycer-subfleet-runtime/AGENTS.md`). That contradicts local, honest, subscription-only operation. Keep the credential-free integration-events spool as the seam (schema v1, `docs/integration-events.md`); the daemon emits `run.started`, `run.bound`, `run.finished`, `handoff.created` from the same code path that writes notices.
- **Logpile**: consumer of the events spool and of job directories. No ingestion into the daemon.
- **Public repo**: freeze it. Extract a public core after milestone 5 from the new repo, with the private roster and the never-rules as an overlay. Until then one tree; PR #1 on the public repo closes with a pointer.
- **Menu bar app**: keep the current single-file Swift app, pointed at a `status.json` the daemon writes every probe cycle; the cockpit branch is not carried.

## What gets dropped

| Dropped | Reason |
|---|---|
| Learned-capacity estimator and `estimated` percentages | produced 4974%; no sensor behind it |
| Bash runners (`subfleet-claude`, `subfleet-codex`) | adapters in Python with tests; the daemon finalizes |
| `subfleet codex` and `subfleet claude` as public verbs | adapters are internal; the hook already blocks direct use |
| `-t fable\|review\|build\|sweep`, `--overflow`, Sol aliases, `claude-fable-5` pin | legacy routing |
| `CARPOOL_*`, `DELEGATE_*`, `CLAUDE_LANE_*` env, `~/.local/state/delegate` | four naming generations |
| Statusline tap | dead for the desktop app since 2026-07-22 (README) |
| `history.jsonl` burn-rate projections | replaced by readings rows; projections computed on read |
| PROGRESS.md preamble injection | clobbered lane files (memory, 2026-08) |
| Four launchd jobs | one daemon with timers |
| Eleven file locks | SQLite |
| The Traycer bridge and cockpit branch | see above; the events spool survives |
| PyPI 0.0.1 | re-publish from the new tree when the public core exists |

## Repo, language, packaging

- New private repo `subfleet` (its own git; today it is a subdirectory of `chief-of-staff` and its commits are interleaved with cadence and receipt work). `~/chief-of-staff/subfleet` stays as the v1 tree until cutover.
- Python 3.12+, stdlib plus `sqlite3`; no third-party runtime dependencies, so launchd contexts and the hooks never need a venv. Packaging with `uv`, a single console script `subfleet` and the two entry points.
- Target size: core (daemon, store, scheduler, two adapters, CLI) under 6,000 lines; sessions and gates under 3,000; tests under 10,000 and under two minutes with fake provider CLIs.

## Migration and cutover

1. Build v2 under `~/.subfleet/` with the CLI installed as `sf2`; v1 keeps running.
2. Import through an idempotent importer with an incremental cursor, from a written manifest that classifies every v1 store as import, retain read-only, or drop with a reason. Import: the roster (`claude-accounts.json`, `codex-accounts.json`), enrolled setup tokens as lanes (probed on import), closures from `~/.local/state/delegate/cooldowns.json` with scope and source (unscoped legacy holds stay conservative until a probe), pending and parked notices (`state/subfleet/notices`), reset-redemption history (`reset-policy.json`, `history.jsonl` reset events; the policy's minimum interval must see them), auth-revocation latches (`refresh-probes.json`), retired-session markers and nudge dedupe records (`tickles/`), open gates with their leases (`gates/`), lane-session identities from the usage ledger (`lanes.py:70`), and the last 500 ledger directories with their original ids as aliases. Still-running v1 entries are imported as externally owned and are never adopted, killed, or reaped by v2. Learned percentages are not imported as quota.
3. Run both for a week: every v1 dispatch from one session is mirrored to v2 with `--dry-run`, and the two decisions are diffed nightly by a `compare` script that never ships. During the week each capability has exactly one owner, and the owners transfer in this order: dispatch and notices (milestone 4), timers including reset credits, keepalive, alerts, and mirror (milestone 5), tickle and revive (milestone 6), gates (milestone 7). Two reset-credit redeemers never run at once. A final incremental import runs after the last transfer.
4. Cutover checklist per transfer: fence the v1 owner first (boot out its plist, or disable the verb), drain or adopt what it owned, then enable the v2 owner; `subfleet` symlink repointed at milestone 4 with the compatibility parser in place; hooks reinstalled by `sf2 daemon install`; CLAUDE.md model-routing section reviewed; skills `tickle`, `muster`, `codex-accounts` repointed; the `bin/codex` shim kept; memory files updated; the deliberate-break list published.
5. Rollback, in this order: stop v2 admissions, stop the daemon's timers, restore the v1 hook configuration, reconcile every v2 attempt (wait, kill with tree verification, or hand it to v1 as externally owned), and only then re-bootstrap the v1 plists and repoint the symlink. Rollback never leaves two schedulers or two credit redeemers alive.

## Test strategy

- Unit: store, scheduler ordering, policy evaluation, classifiers (fed real stderr and transcript fixtures from the v1 ledger, redacted), salvage on a temp repo.
- Fake providers: `tests/bin/claude` and `tests/bin/codex` scripts that emit a transcript or rollout, honour a scenario env var (ok, hard limit with clock, auth dead, content filter, stream drop, CLI too old, model downgrade), and exit accordingly. The daemon runs against them in a temp `SUBFLEET_HOME`.
- Integration: daemon start, submit, kill, crash and re-adopt, reboot marking, notice parking and surfacing, all against fakes, under two minutes.
- Live smoke, opt-in: one Haiku probe on one lane and one wham probe on one home, run by `subfleet doctor --live`.
- Invariant ledger: every row of appendix A is adjudicated before milestone 1 as keep, replace (with the replacement named), or drop (with the reason), and each keep or replace names its acceptance owner (unit, fake-provider, or live). Rows the plan consciously replaces (125 to 127 learned capacity and keepalive resets, 133 Codex ordering is kept, 171 keepalive without a run directory becomes a readings row) get revised dispositions rather than blind ports; tests that pin behaviour the plan rejects (`tests/test_claude_lane_script.py:758`, inconclusive auth becomes rc 5) are rewritten to the new rule.
- Real fixtures: a versioned corpus of captured provider output, named per case: success, truncated stream, shared-account limit, model-scoped limit, credits rejection (this morning's Fable event), auth ambiguity, raw exit-code collisions, content refusal, CLI-too-old. Fakes replay these; they cannot prove CLI argument compatibility, sandbox behaviour, native resume, wake-up delivery, or served-model attestation.
- Opt-in real acceptance before an adapter is promoted: one real launch with the exact argument and environment matrix, one sandbox denial, one attestation with a deliberate model mismatch, one native resume, one cancellation with tree verification, and one hook wake-up in the desktop app and in the terminal.

## Build sequence

| Milestone | Ships | Acceptance |
|---|---|---|
| 0. Experiments and the acceptance contract | answers to the risk table below, and one document that binds milestone 1: the attempt state machine and its per-boundary recovery, the process-ownership and tree-verification rule, the classification precedence table with numeric defaults, the import manifest, the compatibility-parser list, and a disposition for all 220 invariants | each experiment has a written result; every invariant has keep, replace, or drop with an owner; the review's ten questions have written answers |
| 1. Daemon, store, Codex adapter | `sf2 run -m astra`, `jobs`, `show`, `wait`, `kill`, salvage, notices, with the guard preflight, the subscription refusal, and the isolation matrix as launch prerequisites | a Codex job survives a session restart; kill finalizes with salvage refs after verifying the group is empty; a crash of the daemon re-adopts a running job and marks a receipt-less one `lost` only after verifying its pgid; a launch without a trusted guard hook is refused; an API-key home is refused |
| 2. Claude adapter with stream readings | `sf2 lanes enroll`, `provider` Claude readings parsed from every attempt's `rate_limit_event`, a Haiku probe for idle lanes, `unknown` elsewhere, per-model closures | the table shows no percentage without a live or inferred source; a hard limit closes a lane with the provider's clock and the next attempt carries the exclusions |
| 3. Routing as data | `policy.json`, `why`, upward-only chains, permissions per task | the 06:33 case routes to Astra with a recorded reason |
| 4. Agent contract and cutover of `run` | hooks, parked notices, `doctor`, `sf2` becomes `subfleet` | one week of shadow diffs with no decision v2 would have made worse; CLAUDE.md unchanged |
| 5. Timers | probes, alerts to `notify`, keepalive, reset policy, retention, mirror, status.json for the menu bar | four launchd jobs removed; the morning brief section renders from the store |
| 6. Sessions | tickle, muster, revive, handoff on the job API with the twin and duplicate leases | a revive against a live session is refused and logged |
| 7. Gates | `subfleet-gate` on the job API | an existing gate state file replays to the same certificate |
| 8. Guard parity tests, compatibility removal, public core | the parity corpus green against both hooks; deliberate breaks removed after their consumers are updated; public extraction | public tree installs with `uv` and runs `subfleet` against a fake provider; no skill, plist, or memory file still names a removed verb |

## Risks and the experiment that resolves each

| Risk | Experiment | Cost |
|---|---|---|
| 0. Resolved 2026-09-05 07:2x: headless runs on setup-token lanes stream `rate_limit_event` (utilization 0 to 1, reset clocks, both windows) on CLI 2.1.260; on a rejected request the event carries `status: rejected` and no windows, and it names no per-model scope. Remaining unknown: whether the event ever reports a model-scoped window, or whether scopes are visible only through rejections and the desktop OAuth endpoint | watch the events from ordinary Fable and Opus attempts on one lane for a week and diff them against the OAuth endpoint's scoped limits for the same account | none beyond normal use |
| 1. (Optional after experiment 0.) A Claude login in a separate config dir collides with the desktop login's keychain item, or the CLI does not refresh it unattended | create `~/.subfleet/lanes/claude-test`, `CLAUDE_CONFIG_DIR=... claude login` with a spare account, run `claude -p ok` twice a day for three days, probe the usage endpoint with whatever credential the login wrote | one login by Max, three days of a Haiku ping |
| 2. The usage endpoint rate-limits frequent probes across 14 accounts | probe every lane every 5 minutes for a day from the v1 watchdog and count 429s | one day |
| 3. Long-poll `wait` over a unix socket misbehaves under the harness's background-task model | prototype `wait` in milestone 1 and run it under `run_in_background` in a real session | one hour |
| 4. `asyncRewake` does not wake an idle session in the desktop app, or fires only in the terminal | one delayed notice in each client, then a restart during the wait; the docs define the field, the composition is untested | one hour |
| 5. Guardian receipts are lost when the guardian itself is killed, and a provider descendant outlives it | fake provider that spawns nested process groups; kill the guardian, then the daemon; verify the attempt lands as `lost` only after the daemon has enumerated the recorded pgid, killed the survivors, and confirmed the group empty, that the workspace stays `quarantined` when the check cannot run, and that no concurrent write to the workspace happened in between; run under a real LaunchAgent, never only from a terminal | two hours |
| 6. Process-group semantics under launchd differ from a terminal (the daemon's children must be in their own groups and survive the daemon) | milestone 1 test: kill the daemon with SIGKILL while a fake job runs; the job must finish and be re-adopted | one hour |
| 7. The Codex hooks trust hash changes across CLI releases | keep the existing preflight (`subfleet-guard preflight`) and add it to `doctor` | none |
| 8. Max's other sessions depend on v1 verbs during the shadow week | the CLI keeps `runs`, `status`, `capacity` as aliases through milestone 8 | none |
| 9. Rebuild stalls half-way and two systems run for a month | each milestone is usable alone and the rollback is a symlink | none |

## Open decisions for Max

1. Claude lane sensor. Recommendation: read the stream's `rate_limit_event` on every attempt (verified this morning) and keep setup tokens as the lane credential; lane homes become a milestone 5 option for per-model scoped limits and keychain independence, gated on experiment 1.
2. Desktop login as a lane. Recommendation: never by default, `--allow-desktop` per job; your standing order of 2026-09-04 already says this and this morning showed why.
3. Sessions and gates as separate entry points. Recommendation: yes, same repo; they are clients, and the twin lease needs the store.
4. Public repo. Recommendation: freeze now, extract after milestone 5; do not spend a lane keeping two trees in step during the rebuild.
5. Traycer. Recommendation: events spool only.
6. Name. Recommendation: keep subfleet; the third name in ten weeks is enough, and the CLI contract in every session's CLAUDE.md is the asset.
7. Automatic revive. Recommendation changed by the review: off by default for sessions the desktop app owns, because no lease subfleet holds can exclude a desktop restart; cold sessions default to explicit handoff, and native revival stays available for sessions subfleet launched or that you mark. Tickle stays automatic. If you want automatic revival back for everything, say so and it becomes a policy flag with the residual twin risk written next to it.
8. Sidebar mirror. Recommendation: keep it inside the sessions kit as a 60 s timer; Astra recommends dropping it.

## Appendix A: invariant ledger

The full ledger is `reports/A-invariants.md` beside this file: 220 invariants, each with the enforcing `path:line`, the incident or rationale, a class, and a keep verdict. It was produced by an Opus lane with read-only tools in 11 minutes 47 seconds and spot-checked on ten rows by exact-string grep. Counts by class:

| Class | Rows | Examples |
|---|---|---|
| safety-guard | 52 | salvage never touches HEAD; refuse `-b main`; nine never-rules armed per launch with the trust hash pinned; gates fail closed on a changed revision |
| capacity-truth | 40 | unknown renders as unknown; classify windows by duration, never position; one credit per evaluation; count in-flight from the ledger, never `pgrep` |
| ops-hygiene | 33 | ledger caps; idempotent finish; explicit binary resolution for launchd PATHs; incremental rollout scans |
| session-continuity | 20 | resolve the notice recipient by session id at finish time; never revive a headless lane run; refuse a lane session as a notify target |
| routing-policy | 19 | upward only; pins never fall back; desktop login last; Fable-stranded lanes first for non-Fable work; content-filter refusals never retried |
| provenance/attestation | 16 | exactly one transcript match; neither marker on inconclusive evidence; journal every routing decision |
| identity | 13 | never write an auth store; same account in two homes is critical; `~/.codex` is observed, never dispatched |
| UX-contract | 13 | rc token stable; alert on transition then every 6 h; one envelope per notice |
| work-salvage | 7 | commit-tree salvage; prefer transcript text over the JSON `.result` rendering; refuse a live `-o` collision |
| process-survival | 7 | real `setsid`; signal only a group the pid leads; escalate and report killed, escalated, or survived |

Verdicts: 214 keep, 4 keep-simplified (the self-contained API-lane check in bash, the two guard byte-identity contracts, the 30-day rc-5 cooldown), 2 drop (the `CARPOOL_*` aliasing and the `~/.local/state/delegate` cooldown path). A blanket "every row becomes a test" is not a disposition, as the review pointed out; milestone 0 assigns each row keep, replace, or drop with an owner. Rows the review singled out as needing explicit treatment (7, 11, 12, 15, 16, 19, 20, 23, 27, 32, 33, 38, 79, 83, 84, 88, 89, 93, 94, 95, 97, 119, 120, 123, 128, 130, 132, 135, 151, 152, 159, 161, 165, 168, 173 to 186, 188, 191, 215, 216) are all keeps whose enforcement moves from the bash runners into the adapters, the guardian, or the daemon; their acceptance tests are named by the incident date. Rows 125 to 127 are replaced by `provider` readings from the stream event and the usage endpoints; row 171 is replaced by the readings table; row 133 is kept as the Codex primary ordering key. The report also lists invariants that live only in tests or commit messages (guard drift contracts, decision parity, the pinned trust hashes, the "STALE verdict tried and withdrawn within three commits" history) and the README rules the code does not enforce (folded into the section above).

## Appendix B: lane reports

| Lane | Model | Job | Result |
|---|---|---|---|
| A invariants harvest | Opus (max@rules.foundation) | 20260905-063116-sfplan-a-invariants | done, 11m47s, `reports/A-invariants.md` |
| B capacity model audit | GPT-6 Astra (~/.codex-4) | 20260905-063311-sfplan-b-capacity-astra | done, 44m57s, `reports/B-capacity.md` (reproduces the four percentages to the token) |
| C runtime and couplings | none: no Opus lane was dispatchable with the desktop login excluded | | covered inline from the code reads listed in the observations table |
| D surface audit | Sonnet (dispatched on max@policyengine.org, re-picked onto the desktop login) | 20260905-063314-sfplan-d-surface-sonnet | done, `reports/D-surface.md` |
| E branches and Traycer | Opus attempt killed at 06:32 (it had landed on the desktop login); re-dispatch found no lane | 20260905-063120-sfplan-e-branches | covered inline from `git show` of the branch docs and module docstrings |
| F clean-room design | GPT-6 Astra (~/.codex-4) | 20260905-063120-sfplan-f-design | done, 10m27s, `reports/F-design.md` |
| G adversarial review of revision 3 | GPT-6 Astra (~/.codex-4) | 20260905-072246-sfplan-g-review | done, 13m04s, `reports/G-review.md`; five blockers and twelve majors, dispositions in the review section |
| Experiment 0 and follow-ups | this session, three Haiku turns and one Fable turn on setup-token lanes | none (direct `claude -p`) | `reports/experiment-0-rate-limit-event.md` |
