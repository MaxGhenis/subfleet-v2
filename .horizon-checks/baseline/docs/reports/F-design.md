<!-- Lane report F-design · run 20260905-063120-sfplan-f-design · GPT-6 Astra (~/.codex-4), 10m27s · read-only lane dispatched by the planning session 2026-09-05; verbatim -->

# Subfleet from scratch

## 1. Verdict in three sentences

Subfleet should be a local supervisor that turns subscription capacity into durable, accountable jobs accessible from Claude Code and the terminal. It is not a replacement provider runtime, desktop session manager, commercial workbench, or API client. The biggest change is that a job owns its attempts, workspace, results, and completion notices independently of the session that requested it.

## 2. Core model

Use stable opaque UUIDs, with short display prefixes. Names, emails, paths, models, and PIDs are attributes, not identifiers.

| Noun | Invariant and identity |
|---|---|
| Account | One subscription identity. Stable `account_id`; provider subject recorded when verifiable, otherwise enrollment identity labelled operator-supplied. Email is mutable metadata. |
| Lane | One credential binding through which an account executes. Stable `lane_id`; rebinding to another account creates another lane. Token refresh preserves identity. |
| Capacity reading | Immutable observation keyed by account, scope, source, and observation time. Stable `reading_id`; missing values remain null. |
| Job | One requested outcome, with immutable input, permissions, budget, and acceptance conditions. Stable `job_id` survives retries and handoffs. |
| Attempt | One provider process invocation on one lane and exact model. Stable `attempt_id`; native resume creates another attempt linked to its predecessor. |
| Artifact | Immutable bytes plus role, digest, producer attempt, and provenance. Stable `artifact_id`; content addressed by SHA-256. |
| Notice | Durable event addressed to a logical consumer. Stable `notice_id`; delivery attempts and acknowledgements are separate facts. |

All identities and relationships live in SQLite. Configuration assigns account and lane IDs; credential locators reference existing provider homes or Keychain items, never copied secrets.

A job has at most one active writer. Native session ownership is `(provider, lane_id, native_session_id)`. Process identity additionally requires boot identity and process birth time.

This adopts the branch proposal’s explicit attempt and handoff boundaries without requiring Traycer or Logpile: `feat/subfleet-traycer-port:subfleet/docs/unified-product.md:40`.

## 3. Architecture

Choose Python 3.12+, SQLite, ordinary files, and Unix sockets. Bun offers insufficient benefit to justify another runtime here.

Run one supervisor LaunchAgent with `KeepAlive=true`. Each active attempt gets a small guardian, registered as a separate launchd job with a unique label and persistent descriptor. Guardians capture provider streams and receipts; only the supervisor schedules, changes job state, or publishes notices.

Separate launchd ownership prevents supervisor replacement from terminating execution. Keep guardian `AbandonProcessGroup=false`: launchd documents cleanup of remaining members when its job dies. This is group cleanup, not proof that escaped descendants died. ([launchd.plist.5:609](/usr/share/man/man5/launchd.plist.5:609))

Use SQLite WAL with full synchronization for jobs, attempts, reservations, readings, decisions, and the notice outbox. Guardians append recoverable receipts under attempt directories while the daemon is unavailable. Import receipts idempotently; commit terminal state and its notice together.

Every writable job receives an independent clone with its own object store under the durable state root. Preserve dirty files and unpushed commits until explicitly applied, archived, or discarded. After writers stop, salvage through a private Git index and `commit-tree`, preserving the existing protection against advancing shared branches. ([bin/subfleet-codex:12](/Users/maxghenis/chief-of-staff/subfleet/bin/subfleet-codex:12))

For IPC, use Subfleet’s own user-private Unix socket. Claude hooks connect as clients. The current private inbox coupling resolves a socket and peer token and cannot confirm that the model consumed its message. ([subfleet/notify.py:355](/Users/maxghenis/chief-of-staff/subfleet/subfleet/notify.py:355))

Replace it with documented command hooks: `PostToolUse` arms an `asyncRewake` waiter, which blocks on Subfleet’s socket and exits 2 with completion metadata. SessionStart and UserPromptSubmit return catch-up context. Documentation confirms that `asyncRewake` can wake idle Claude; ordinary asynchronous hook output waits for another turn. This composition requires a live Desktop acceptance test. ([Claude hooks reference](https://code.claude.com/docs/en/hooks#command-hook-fields))

Recovery rules:

- **Session restart or account switch:** execution continues; the new session instance reconnects and receives unacknowledged notices.
- **Desktop idle SIGTERM:** only the client and waiter disappear.
- **Sleep:** elapsed heartbeats mark uncertainty, never authorize another writer. Reconcile processes and refresh readings after wake.
- **Reboot:** reconcile receipts and workspace state after login. Resume interrupted work only after checking unfinished side effects.
- **Guardian kill:** launchd cleans its group; the supervisor checks recorded descendants, salvages, and reports interruption.
- **Wiped temporary directory:** no authoritative prompt, transcript copy, receipt, or workspace lives there.

## 4. Capacity truth

Separate three questions: does authentication work, can this model accept a request now, and how much quota remains?

Today, learned capacity takes the largest observed hard-limit token sum; estimated ratios can subsequently mark a lane exhausted. Neither operation establishes subscription quota. ([subfleet/capacity.py:466](/Users/maxghenis/chief-of-staff/subfleet/subfleet/capacity.py:466), [subfleet/capacity.py:1145](/Users/maxghenis/chief-of-staff/subfleet/subfleet/capacity.py:1145))

Store evidence with independent labels:

- `server_reported`: authenticated quota response, timestamp, scope, and reset.
- `request_observed`: successful inference or explicit limit response.
- `estimated`: transcript token totals, incomplete coverage, projected burn.
- `unknown`: absent, stale, contradictory, or unreadable evidence.

Display percentages only from quota responses. Show transcript totals as observed local consumption, without converting them into remaining quota. A successful request proves recent availability, not a full window.

Default unknown lanes to **probe first**: one tool-free request on the requested model, bounded to 30 seconds, with a short shared cache. Probe failure remains unknown unless its evidence establishes authentication failure or a limit. No fleet-wide probe storm.

Model limits are account-scoped buckets: `shared`, `fable`, and any other explicitly reported scope. A Fable rejection leaves Opus eligibility intact unless shared capacity is also blocked. A successful Opus probe cannot clear a Fable hold.

Keep subscription usage endpoints behind replaceable observation adapters. The current Codex endpoint and Claude setup-token restriction are documented locally; neither justifies promising a permanent public quota interface. ([README.md:122](/Users/maxghenis/chief-of-staff/subfleet/README.md:122), [README.md:166](/Users/maxghenis/chief-of-staff/subfleet/README.md:166))

Express policy in TOML:

```toml
[capacity]
unknown = "probe_first"
reading_ttl_seconds = 120
dispatch_order = ["weekly_reset_ascending", "lane_id"]

[reset]
enabled = true
trigger_dispatchable_below = 1
trigger_weekly_headroom_sum_below_pct = 15
candidate_order = ["unshadowed_first", "weekly_reset_descending", "lane_id"]
maximum_per_evaluation = 1
minimum_interval_seconds = 1800
```

Redemption requires fresh limited status and a concrete applicable credit. Persist the redemption request ID before sending; reconcile ambiguous responses before another redemption. Preserve August 22’s staggered resets without manufacturing a new reset timestamp.

## 5. Provider adapters

Adapters implement this versioned contract:

| Operation | Required result |
|---|---|
| `launch(spec)` | Exact argv, sanitized environment description, binary identity, process handle, native binding events, and raw streams. |
| `classify_exit(evidence)` | Success, limit with scope, authentication failure, unsupported runtime/model, refusal, transient failure, cancellation, or unknown. Preserve raw exit status. |
| `salvage(attempt)` | Recover transcript, partial outputs, workspace changes, and checkpoint manifest; report missing evidence. |
| `attest_model(attempt)` | Requested model, observed models, evidence references, and `verified`, `mismatch`, or `unknown`. |
| `read_usage(lane, scope)` | Typed readings with timestamps and provenance; unsupported observations return unknown. |
| `resume(binding, input)` | New invocation pinned to the original lane; unavailable affinity queues rather than migrating native state. |
| `kill_tree(handle, deadline)` | TERM, bounded grace, KILL, then verified process results. Unknown survivors quarantine the workspace. |

The Claude adapter uses a fresh UUID with `claude -p --session-id … --output-format stream-json`, enrollment-token authentication, and explicit tool permissions. Every brief includes a mandatory HEADLESS block: finish this turn, produce the artifact, and never wait for future notifications.

Codex uses `exec --json`; native continuation uses `exec resume`. Its `--output-last-message` writes only `final-message.txt`. These interfaces were checked with the installed absolute-path `claude --help`, `codex exec --help`, and `codex exec resume --help`.

Both adapters reject API authentication and inherited API/provider overrides. Only provider CLIs refresh provider credentials. Explicit Max-driven enrollment may store a Subfleet-owned Keychain item.

Requested configuration is insufficient model attestation. Unknown evidence preserves artifacts but fails an exact-model acceptance condition.

The nine never-rules live in one versioned policy evaluator with provider hook translators. Preserve all nine, preflight enforcement, and disable unguarded tool channels by default. Hooks supplement permissions; existing documentation explicitly identifies fail-open timeout behavior and bypasses. ([docs/guard.md:13](/Users/maxghenis/chief-of-staff/subfleet/docs/guard.md:13), [docs/guard.md:38](/Users/maxghenis/chief-of-staff/subfleet/docs/guard.md:38), [docs/guard.md:145](/Users/maxghenis/chief-of-staff/subfleet/docs/guard.md:145))

## 6. Routing

Keep the supplied September 4 policy as configuration:

| Work | Trivial | Easy | Standard | Hard |
|---|---|---|---|---|
| Lookup, research, review, build | Haiku | Sonnet | Opus | Astra |
| Sweep | Terra | Terra | Terra | Astra |
| Authored prose, strategy, adjudication | Fable 5.1 | Fable 5.1 | Fable 5.1 | Fable 5.1 |

The existing mapping is documented at [README.md:516](/Users/maxghenis/chief-of-staff/subfleet/README.md:516). Store exact model IDs, minimum CLI requirements, and permitted upward edges beside it. Treat capability ordering as operator policy, not a universal model ranking.

Model pins forbid model substitution; lane pins forbid account substitution. Reject retired explicit pins with an actionable replacement instead of silently changing them.

Build grants workspace writes. Other tasks default read-only; research may enable web tools. External drafts, sends, and publishing require Max’s recorded authorization.

Each admission stores candidate order, rejected candidates, reading IDs, configuration digest, permissions, pins, and promotion reasons. `--why` renders that immutable decision.

Start with four active attempts globally. Bound attempts, wall time, child jobs, and batch token estimates. Observed usage stops further admissions at budget exhaustion; in-flight token overshoot remains visible. This addresses the August 22 overnight sweep without claiming precise subscription-token enforcement.

## 7. Surface

Use eleven verbs: `status`, `run`, `jobs`, `wait`, `cancel`, `resume`, `artifact`, `lane`, `events`, `doctor`, and `service`.

One configuration file: `~/.config/subfleet/config.toml`. One Subfleet state root: `~/.local/state/subfleet`. One public environment prefix: `SUBFLEET_`. Provider-owned homes remain external references; adapters generate provider-required environment variables.

JSON is versioned, stdout-only, and explicit:

```json
{
  "schema": 1,
  "request_id": "caller-generated-uuid",
  "accepted": true,
  "job_id": "j_…",
  "state": "queued",
  "reason": "capacity_unknown",
  "retry_at": null,
  "event_cursor": 1042
}
```

Repeated request IDs return the original job; different input with the same ID is an error.

Exit codes: `0` successful command or admission, `2` invalid input, `3` policy/refusal, `4` failed job, `5` authentication/runtime setup problem, `69` unavailable supervisor, `75` durably queued, `124` wait timeout, `130` cancellation. Queue receipts always identify the existing job.

`wait` and `events --follow` block on socket events. Hooks provide agent completion signals without polling. Notices progress through pending, offered, and acknowledged; acknowledgement follows a subsequent consumer call, not merely a successful write.

`artifact export` selects a declared artifact role. A closing message never overwrites a deliverable, and the supervisor exports review output outside the provider’s read-only workspace.

## 8. Session continuity features

Keep job recovery, owned-session resume, session-instance registration, and duplicate-write exclusion in the core.

Move these into small consumers of the store interface:

- **Muster:** list interrupted sessions, pending jobs, artifacts, and exact recovery commands.
- **Handoff:** produce an immutable continuity bundle and a fresh native session.
- **Gates:** bind main and peer approvals to exact artifact digests or PR head/base commits.

Preserve gates’ revision checks, isolated review, and merge reconciliation. They already distinguish unknown post-action state from verified completion. ([subfleet/consensus.py:987](/Users/maxghenis/chief-of-staff/subfleet/subfleet/consensus.py:987), [subfleet/consensus.py:878](/Users/maxghenis/chief-of-staff/subfleet/subfleet/consensus.py:878))

Drop automatic tickle and in-place headless revival of external sessions. Transcript interruption is a heuristic, not exclusive ownership. ([subfleet/tickle.py:205](/Users/maxghenis/chief-of-staff/subfleet/subfleet/tickle.py:205))

Replace revive with an explicit handoff unless Subfleet owns the stopped native session. Register session ID plus process generation; duplicate instances receive no write grant. This addresses both September 4 incidents. Uninstrumented direct provider launches remain outside this guarantee.

Drop desktop database mirroring. Provide a session catalog and explicit resume links; sidebar replication does not justify private database mutation.

## 9. Migration

Run `subfleet-next` beside old Subfleet for seven days, with distinct state roots, service labels, workspaces, and disjoint execution lanes. Compare routing in observation mode before enabling writes. Never let both schedulers redeem credits.

Import through an idempotent importer:

- Ledger metadata, prompts, artifacts, native bindings, and original IDs as aliases.
- Cooldowns with scope, source, and expiry; unscoped legacy holds remain conservative pending a probe.
- Lane roster and credential references, without copying auth homes or extracting tokens.
- Enrolled Keychain references.
- Existing salvage refs and unpushed work as retention-pinned recovery items.

Do not import learned percentages as quota truth. Import unfinished runs as externally owned, requiring reconciliation.

Cutover checklist: reconcile every active writer; verify authentication bindings; recover temporary-directory work; test notices in terminal and Desktop; verify export hashes; disable old schedulers, revival, mirror, reset automation, and dispatch shim; enable new hooks and command entry; retain the old installation read-only.

Rollback changes command entry points and scheduler ownership after draining new attempts. It never starts competing writers.

## 10. Test strategy

Unit-test state transitions, routing tables, scope precedence, confidence labels, budgets, idempotency, artifact selection, and notice replay with an injected clock.

A fake provider CLI covers truncated JSONL, wrong models, hard limits, refusals, hung exits, child processes, guardian death, ambiguous completion, and retries. Tiny Git fixtures verify that cancellation changes neither shared HEAD nor index and preserves untracked work.

Live acceptance uses one lane per provider: minimal inference, attestation evidence, read-only output export, native resume, and cancellation. Separately test Claude wake-up and restart catch-up. Live tests stay outside the default suite.

Budget 30 seconds for units, 60 for fake-provider integration, and 20 for process/Git checks. No sleeps for cooldowns, network dependencies, or full transcript scans. Dispatch reads indexed offsets and known paths.

No tests or provider jobs were run for this design.

## 11. Ranked risks and open questions

1. **Hook wake-up compatibility.** Documentation establishes the mechanism, not this composition across Desktop and terminal. Cheapest experiment: one delayed notice in each, then restart during the wait.
2. **Escaped process descendants.** launchd group cleanup may miss detached children. Cheapest experiment: fake CLI spawning nested groups; kill guardian and daemon independently and verify quarantine.
3. **Claude quota opacity.** Probe success cannot predict remaining work. Cheapest experiment: compare request outcomes against transcript totals for one week without estimate-based admission.
4. **Served-model evidence.** CLI schemas may record requested configuration without proving execution identity. Cheapest experiment: inspect one exact-model result per provider and deliberately mismatch the expected model.
5. **Private subscription endpoints.** Usage and credit contracts may change. Cheapest experiment: read-only fixture capture, then disable the observer and verify honest unknown-state behavior.
6. **Storage growth.** Independent clones and pinned salvage consume disk. Cheapest experiment: measure three representative repositories and restore one archived job before setting retention policy.

## 12. Build sequence

| Milestone | Independently usable result | Acceptance test |
|---|---|---|
| 1. Honest inventory | `status`, lane roster, probes, and `doctor`. | Contradictory or unavailable evidence displays unknown; duplicate credential bindings are excluded. |
| 2. Durable execution | Supervisor, guardian, SQLite store, pinned single-provider jobs. | Kill submitting shell and supervisor; job output remains recoverable. |
| 3. Work preservation | Independent workspaces, artifacts, cancellation, recovery. | Kill guardian with nested children; preserve tracked and untracked edits without touching shared HEAD. |
| 4. Second adapter | Subscription-only execution through either provider. | Reject API authentication; preserve refusal and model-evidence classifications. |
| 5. Capacity routing | Scoped eligibility, queueing, upward fallback, budgets, reset policy. | Fable limitation leaves Opus usable; concurrent evaluations redeem at most one credit. |
| 6. Claude integration | Hooks, event waiters, durable notices, duplicate-instance detection. | Idle session wakes; restart replays notices without creating another writer. |
| 7. Continuity tools | Muster, handoff, and revision-bound gates. | Handoff creates fresh ownership; changed revisions invalidate approvals. |
| 8. Cutover | Importer and seven-day comparison complete. | Every retained run has known ownership, recoverable artifacts, and an actionable terminal or recovery state. |