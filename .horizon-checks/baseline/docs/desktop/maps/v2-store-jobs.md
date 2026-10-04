# v2 durable store, job/attempt lifecycle, admission and scheduling

Repo: `/Users/maxghenis/subfleet-v2-lanes/desktop-workspace` at `3f155e5`. All paths below are relative to `subfleet/`. I read store.py, store_schema.sql, ids.py, scheduler.py, capacity.py, policy.py, picker.py, actions.py, guardian.py, procs.py, salvage.py, retention.py and timers.py in full or by section, plus the job/attempt parts of daemon.py and contracts.py and the adapters' launch/classify paths. I did not read the legacy cockpit tree; this area didn't need it.

## 1. SQLite schema and versioning

**Mechanism**
- `Store` opens a single shared connection (`check_same_thread=False`) behind an `RLock`, with busy_timeout 5000 and foreign_keys ON (store.py:59-69).
- A writable open sets WAL and `synchronous=FULL` and chmods the file to 0600 (store.py:79-80, 106).
- `SCHEMA_VERSION = 5` (store.py:23). `MIGRATIONS` holds only steps 4 and 5 (store.py:29-40):
  - Step 4 adds `lanes.identity`, `label` and `identity_status`.
  - Step 5 adds `jobs.unmeasured_reserve_reason`.
- Upgrade order (store.py:84-105):
  1. Inside `BEGIN IMMEDIATE`, `_migrate` walks each step. It skips an `ADD COLUMN` whose column already exists and writes one `schema_version` row plus a `schema.migrated` event per step (store.py:115-134).
  2. Then all of `store_schema.sql` runs through `executescript` as `CREATE ... IF NOT EXISTS`. This is how the version-2 `service_notices` table appears.
  3. The version-3 gate columns (`isolated_review`, `review_root`, `round_lease`) are added ad hoc after the script, not through `MIGRATIONS` (store.py:92-97).
- A read-only open refuses a store that is uninitialized or newer than 5 (store.py:72-77). Any older build that opens a newer store will therefore fail.
- Every mutating `transaction()` appends an audit row to `events` when `total_changes` moved. Nested calls use savepoints (store.py:141-168).
- `_insert` and `_update` reject unknown columns (store.py:189-203). Attempt `attempt_id`, `job_id`, `seq`, `lane_id` and `model_requested` are immutable (store.py:319-322). A lane's binding (`provider`, `account_key`, `credential_*`, `home`) is immutable; `identity` and `label` can be learned once; only re-enrolment clears `mismatch` (store.py:234-270).
- Doc drift: the schema file header says "version 4" and calls identity "Version 2", while the code numbers it step 4 and the file already contains the v5 column (store_schema.sql:1-4, 78).
- Live store, read-only query I ran: `schema_version` rows are (4, 2026-09-19) and (5, 2026-09-20). `state.sqlite3` is about 798 MB, plus a WAL.

**Tables** (store_schema.sql)

| Table | Columns | Lines |
|---|---|---|
| `schema_version` | version, applied_at | 6-9 |
| `lanes` | lane_id PK; provider (codex, claude); account_key; credential_ref; credential_kind (keychain-token, home, env); credential_epoch; home; owner (v1, v2); desktop; enabled; plan; identity; label; identity_status (verified, enrolled, mismatch, unverified); created_at; updated_at | 12-32 |
| `readings` | reading_id; lane_id FK; scope; window; utilization in [0,1]; resets_at; label (provider, stale-provider, admission-observed, local-backoff, unknown); source; observed_at; attempt_id | 37-48 |
| `closures` | closure_id; lane_id; scope; until_at; reason (provider-limit, credits, auth-dead, operator-hold, cooldown); clock_source (reported, guessed); source_event; created_at; released_at | 52-62 |
| `jobs` | job_id PK; request_id UNIQUE; payload_digest; kind; state; wait_reason; next_check_at; task; tier; pinned_model; pinned_lane; unmeasured_reserve_reason; workdir; workdir_head; worktree; prompt_path; out_path; sandbox (read-only, workspace-write); exclusions (JSON); allow_desktop; in_place; independent; isolated_review; review_root; round_lease; parent_job_id FK; caller_session; caller_pid; name; policy_hash; max_attempts (3); max_wall_s (21600); max_tokens_observed; accepted_attempt_id; rc; export_error; cancel_requested_at; created_at; started_at; finished_at | 66-107 |
| `attempts` | attempt_id PK; job_id FK; seq, with UNIQUE(job_id, seq); lane_id FK; model_requested; model_served; attestation (attested, mismatch, unattested); state; guardian_pid; child_pid; pgid; boot_id; proc_start; native_session_id; transcript_path; transcript_offset; baseline_tree; rc; signal; outcome_class (ok, limited, auth-dead, cli-too-old, content-filter, transient, unknown); outcome_detail; evidence_json; killed_by; quarantine_reason; reserved_at; started_at; finished_at | 113-142 |
| `artifacts` | artifact_id; attempt_id; role; path; sha256; bytes; created_at | 147-155 |
| `notices` | notice_id; job_id (nullable); session_id; text; state (pending, offered, acknowledged, surfaced); transport; created_at; offered_at; acknowledged_at | 161-171 |
| `decisions` | decision_id; job_id; attempt_id; evaluated_at; policy_hash; decision_json | 175-182 |
| `leases` | lease_key PK; holder; acquired_at; expires_at | 186-191 |
| `actions` | action_id; kind; op_key UNIQUE; subject; state (pending, executing, confirmed, failed, unknown); request_json; result_json; created_at; updated_at | 194-204 |
| `events` | event_id; ts; kind; job_id; attempt_id; lane_id; data_json. Append-only | 207-215 |
| `service_notices` | the jobless form of `notices` | 221-230 |

**Identifiers** (ids.py)
- Job id: local time `YYYYMMDD-HHMMSS-<slug≤40>`, with a `-n` suffix on collision (ids.py:24-35).
- Attempt id: `<job>/a<seq>` (ids.py:38-42).
- Request id: caller-supplied (1-128 characters) or a generated UUID4 (ids.py:45-51).
- `payload_digest`: SHA-256 of canonical JSON covering the base64 prompt, resolved workdir, head, task/tier/pins, sandbox, sorted exclusions, out_path, allow_desktop and policy_hash, plus optional isolation, resume and authorization fields (ids.py:54-92). `submit` returns the existing job when request_id and digest match, and fails when the same request_id arrives with a different digest (daemon.py:936-940).

## 2. State machines

The enums are at contracts.py:23-62. The CHECK constraints include attempt state `cancelled`, but no daemon statement writes it (grep of `UPDATE attempts SET state`). The only other writer of job and attempt states is the v1 importer (importer.py:1392, 1405). No code sets `wait_reason` to `approval` or `dependency`; admission only reads `approval` (daemon.py:2421). The `WaitReason` enum lacks `route`, which the daemon writes (daemon.py:2716).

**Job states**
- submit → `queued`. The job row is inserted after prompt.md and manifest.json are published (daemon.py:962-988).
- `queued`/`waiting` → `waiting`:
  - `capacity` on no lane, fleet-full, slot-kept, probe-pending, lease-held or retry-let-go (daemon.py:2564, 2576, 2621, 2492, 2517, 2271).
  - `route` when evaluation raised (daemon.py:2716).
  - `workspace` on a transient git/OS failure (daemon.py:2776).
  - `uncertain` when a probe was quarantined (daemon.py:2020, 2272).
- `waiting(workspace)` → `queued` (daemon.py:2464). `waiting(uncertain)` → `waiting(capacity)` when the probe finishes (daemon.py:2171).
- → `running` when an attempt is reserved; `started_at` is kept from the first reservation (daemon.py:2631). "Running" therefore means "has a live attempt row", not "provider process up".
- → `failed`:
  - `_fail_queued` for an AdapterError in workspace or resume setup, a route refusal (rc 2) or a workspace failure (daemon.py:2452, 2705, 2787, 2790-2801). It becomes `cancelled` if a cancel was already requested (daemon.py:2796).
  - A revive twin is skipped with rc 7 (daemon.py:2737).
- → `cancelled` (rc 130): `kill` on a job with no live or quarantined attempt (daemon.py:1700-1704).
- `running` → `queued` when the launch never happened and seq < max (daemon.py:3034-3037).
- `running` → `waiting(capacity)` for a finalized retry: next check +60 s for transient, immediately otherwise (daemon.py:3318-3320).
- `running` → `succeeded`, `failed`, `lost` (rc 125) or `cancelled` at finalization (daemon.py:3295-3320). Rc mapping: limited 4, auth-dead 5, cli-too-old 6 (daemon.py:3313-3316).
- `running` → `lost` or `cancelled` at quarantine (daemon.py:3109-3110).
- The wall clock (`max_wall_s` from `started_at`) triggers a kill both for queued jobs (daemon.py:2398-2400) and for live attempts (daemon.py:2968-2974).

**Attempt states**
- Insert as `reserved` (daemon.py:2626).
- `reserved` → `starting` with guardian pid, pgid, boot and start identity, guarded by `WHERE state='reserved'` (daemon.py:2908).
- `reserved` → `failed`, or `interrupted` if a cancel was requested, via `_unlaunched`. Details: `reserved-no-launch`, `cancelled-before-launch`, `guardian-identity-unavailable` (daemon.py:2945-2950, 2806-2808, 2915, 3027-3040).
- `reserved` → `finalizing` on an adapter or launch error, through a synthesized exit.json with `spawn_error` (daemon.py:2859-2867, 2924-2929).
- `starting` → `running` once start.json is read (daemon.py:2958-2962).
- `starting` past the grace period (10 s; the deadline is in memory) → `_unlaunched("starting-no-receipt")` if the census is empty, else `quarantined` (daemon.py:2976-2985).
- `starting`/`running` → `finalizing` when exit.json is present (daemon.py:2965-2966, 3042-3047).
- `running` with a dead guardian and no receipt: re-read the receipt; if still none, run the census. Survivors lead to a lost-kill; an empty census leads to `_finalize(lost=True)` and state `lost` (daemon.py:2998-3009, 3285, 3295).
- `finalizing` → `succeeded`, `failed`, `interrupted` or `lost` (daemon.py:3299). If writers remain after the 3 s exit settle, → `quarantined` (daemon.py:3214-3225).
- `quarantined` → `interrupted`/`lost` only by operator `kill --confirm-dead` or `--force-release` (daemon.py:1684-1689, 3114-3128).

## 3. Admission

The `_control` loop runs every 50 ms and schedules one paced `admission` worker (daemon.py:1759-1781). `_admit_pass` (daemon.py:2369-2648) works as follows.

- **Candidates:** jobs that are queued or waiting with no cancel request (daemon.py:2372). `ordered_jobs` sorts them by the policy's tier index, then `created_at`: FIFO within a tier, and with the default tier list, `trivial` sorts first (scheduler.py:155-165).
- **Behind an older job:** `waiters[tier]` collects only jobs in capacity waits.
  - A later job is held as `behind-older-job` when `competes()` holds: some model could serve both and some lane could serve both, with unknown counting as overlap (daemon.py:2393, 2414-2420; scheduler.py:168-215).
  - A job that passes an older waiter leaves one slot free: `limit = cap-1`, reported as `slot-kept` (daemon.py:2541, 2554).
  - Once `live >= max_active_attempts` (default 4), the rest of the pass is `fleet-full`.
- **Evaluation** (`scheduler.evaluate`, scheduler.py:298-471; pure, no store writes):
  - It walks the task chain upward from the tier, or a single model for a pin; a lane pin narrows to one lane.
  - Per lane, rejection reasons: `excluded`, `desktop` (unless allowed), `owner-v1`, `disabled`, `identity-mismatch`, `closed:<scope>:<until>`, `no-slot`, `below-floor` and `reserve:<model>:<state>` (scheduler.py:388-428).
  - Slot cap: `max_in_flight_per_lane` (2) when the lane has a fresh provider reading, else 1 (scheduler.py:402-405). In-flight counts come only from active attempt rows (capacity.py:253); probe leases make a lane unavailable and count toward the fleet cap (daemon.py:576-580; scheduler.py:354).
  - Ordering: Codex prefers measured lanes, then the earliest seven-day reset. Claude prefers lanes that are "stranded" (a higher model is closed there), then measured lanes, then the largest reserve slack or headroom, then the fewest in flight (scheduler.py:435-444).
- **Fable scoping and the reserve:**
  - Readings and closures apply when `scope ∈ {"account", model id}`, so a Fable-only closure blocks only Fable on that lane (scheduler.py:365-368, 400-401).
  - The default policy reserves `fable` with `cap_ratio 2.0` and `min_slack 0.05` (default_policy.json).
  - A non-Fable Claude model may spend only `slack = (1 - shared weekly) - cap_ratio*(1 - Fable weekly)`. Without a fresh complete seven-day reading the state is `unmeasured` and the lane is rejected, unless the job carries `unmeasured_reserve_reason` with an explicit lane and model pin (scheduler.py:285-295, 414-428, 474-541).
  - A reported Fable-only provider-limit closure turns the reserve into `slack` with `requires_probe` (scheduler.py:504-515).
  - "Higher" models come from chain position and policy `priority`; Fable is 4, the highest (scheduler.py:249-266).
  - The live `~/.subfleet/policy.json` may differ from the default (daemon.py:250-253). I did not read its contents: **UNVERIFIED**.
- **Probe:** `_prepare_route` runs a guardian-supervised "Reply with exactly OK" probe on lease `lane:<id>:slot:0`, holder `probe:<token>`, with a 60 s deadline (daemon.py:2213-2275; scheduler.py:544-576). It runs when:
  - the job is a revive or carries an authorization,
  - the reserve requires a probe, or
  - the job is writable or hard-tier on an unmeasured lane.

  Probes run **inline in the single admission worker**, which serializes them (daemon.py:2214). One probe can block the whole pass for up to about 60 s. This is inferred from the code, not measured.
- **Reservation:** what "reservation" means here. In one `BEGIN IMMEDIATE` transaction (daemon.py:2526-2634), the daemon:
  1. re-reads the job and re-evaluates,
  2. counts live attempts,
  3. picks the first free `lane:<id>:slot:<n>` (holder: attempt id),
  4. adds job-held leases: `native-session:<lane>:<sid>` for a resume or revive, `out:<path>`, `worktree:<target>` for writable jobs, `session:<id>:revive`, and the gate round,
  5. on any contested lease, waits as `lease-held`,
  6. otherwise inserts the leases, the attempt as `reserved` (with `baseline_tree` and `evidence_json{baseline_commit, model_short}`) and a `decisions` row, and sets the job to `running`.

  Launch permission lives only in the in-memory `_pending_launches` (daemon.py:2644).
- **Waits back off** from 1 s doubling to 30 s, and are clamped to the earliest known reset (scheduler.py:584-602; contracts.py:203-204). A freed non-probe lease brings capacity waits forward (daemon.py:2384-2386, 2432).
- **The "reserve" in `timers._reserve`** is a different thing: a timer's lane lease (timers.py:258-275).

## 4. Guardian lifecycle

- `_launch` (daemon.py:2803-2922):
  - re-checks the writable branch, validates the home and resolves the credential into memory,
  - builds the launch (`build_launch`, or `resume_launch` for resume and revive),
  - publishes launch.json without `env_add` (daemon.py:2868-2871),
  - strips `CODEX_API_KEY`, `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` and sets the `SUBFLEET_JOB`, `SUBFLEET_ATTEMPT` and `SUBFLEET_ROOT` markers (daemon.py:2873-2877),
  - spawns `python -m subfleet.guardian` with a pipe `--launch-fd`.
- The guardian:
  - calls `setsid()` and ignores SIGTERM and SIGINT (guardian.py:60-64),
  - blocks until it reads the gate byte `"1"`; on EOF it returns 127 without spawning (guardian.py:65-74),
  - writes `start.json` `{guardian_pid, pgid, boot_id, proc_start, started_at}` (guardian.py:79-85),
  - runs exactly one `Popen(argv)` with stdin from a file and stdout/stderr to files (guardian.py:89-96),
  - writes `exit.json` `{rc, signal, finished_at, wall_s, child_pid, spawn_error?}` (guardian.py:101-108), all through `atomic_publish` (temp, fsync, rename, dir fsync; guardian.py:18-37).
- The daemon writes the gate byte only after it has committed `starting` with the guardian's identity (daemon.py:2899-2910).
- **Exit classification:** `_finalize` (daemon.py:3206-3331) requires a verified-empty census first. The on-disk receipt decides; no rc means lost (daemon.py:3236-3248). Then:
  - `adapter.classify` produces an Outcome with class, evidence, readings, closure, native_session_id, transcript_path and served_model; `adapter.attest` compares requested and served model. Both are cached in finalization.json (daemon.py:3250-3262).
  - The deliverable goes to deliverable.md. An `ok` class with an empty deliverable becomes `unknown` (daemon.py:3263-3269).
  - Salvage writes a private `refs/subfleet-salvage/...` through salvage.json (daemon.py:3164-3185; salvage.py:250-295).
  - Acceptance happens only when `seq == MAX(seq)` and the attempt is in `finalizing` (daemon.py:3281-3287).
  - Retry rules (daemon.py:3290-3294): lost on a read-only job; `limited` on an unpinned job; `transient` unless pinned and already transient once.
  - `auth-dead` disables the lane. Readings and closures are recorded (daemon.py:3304-3311).
  - One notice per job (daemon.py:1709-1713). Export copies the deliverable to `out_path` after a digest check, then releases the job's leases (daemon.py:3339-3369).
- **Adoption after restart:** a `flock` on daemon.lock keeps a single daemon (daemon.py:225-230). `_control` offers every live attempt to `_process_attempt` each tick, skipping `imported_external` (daemon.py:1765-1768). The per-state recovery is §2.
  - A guardian with a live (pid, boot id, proc start) identity is re-adopted "solely by receipt identity, not parentage" (daemon.py:2986-2991). An `unknown` liveness decides nothing (daemon.py:2992-2997; procs.py:112-133).
  - `close()` does not signal attempts (daemon.py:3449-3474), so guardians outlive the daemon.
  - Probes are recovered before timers or admission start (daemon.py:1802-1814, 2175-2193). Capacity and route waits become due immediately (daemon.py:1863-1874).

## 5. Cancellation, kill and containment

- **`kill`** (daemon.py:1682-1706) sets `cancel_requested_at` on the job and, through a recursive CTE, on its non-`independent` descendants.
  - Jobs with no live or quarantined attempt become `cancelled` immediately.
  - A reserved attempt becomes `interrupted` at launch (daemon.py:2806-2808).
  - A starting or running attempt goes to `_kill_attempt` (daemon.py:2968-2975).
  - A cancel that arrives while an attempt is **finalizing** still turns the job into `cancelled` with no accepted attempt, even at rc 0 (daemon.py:3288, 3295-3296, 3317).
- **`_kill_attempt`** (daemon.py:3049-3101):
  1. Record owned identities, meaning group members observed while the leader is verified ours, in evidence before any signal.
  2. SIGTERM the process group; `signal_group` requires the leader's identity to match and refuses pgid ≤ 1 or its own group (procs.py:258-269). The guardian ignores TERM, so it survives to write a receipt.
  3. Poll the census for `term_grace_s` (15 s), then SIGKILL the group and SIGKILL recorded survivors individually (procs.py:272-282).
  4. Poll the census for `kill_settle_s` (3 s). If it is not verified empty, quarantine. Otherwise synthesize exit.json if missing (`rc=-sig`, `killed_by`) and finalize.
- **Census** (`procs.containment`, procs.py:174-255) takes the union of:
  - the pgid members,
  - a descendant walk from the guardian and child pids, both from one `ps -axo pid,ppid,pgid,stat` snapshot,
  - `SUBFLEET_ATTEMPT=<id>` plus `SUBFLEET_ROOT=<root>` environment markers from `ps -axEww`, which are never retained.

  Any inspection failure makes the census unverifiable, and nothing is released. Marker-only pids are evidence for quarantine, never signal targets (procs.py:178-181; daemon.py:3052-3054).
- **Quarantine** releases only the `lane:` leases and keeps job leases, including the worktree and native-session leases (daemon.py:3108).

## 6. Identity and accounting recorded per attempt

- **Lane and account:**
  - `attempts.lane_id` is immutable (store.py:320) and the lane binding is immutable (store.py:235-237), so each attempt maps to a stable account_key, identity and label.
  - Claude launch notes in launch.json also carry `account_key`, `identity`, `label`, `model_id`, `session_id`, `transcript_path` and `transcript_offset` (claude.py:1255-1280).
  - Codex `_launch` stores only `lane_id` and no notes (codex.py:458-460).
  - The credential epoch is not recorded per attempt. **UNVERIFIED** whether any other file records it.
- **Model:** `model_requested` holds the policy model id; `evidence_json.model_short` holds the policy short name (daemon.py:2626-2628). `model_served` and `attestation` come from `adapter.attest`; Claude reads the transcript from `transcript_offset` (claude.py:1781-1830).
- **Process and outcome:** guardian_pid/pgid/boot_id/proc_start, child_pid, rc, signal, outcome_class/detail, killed_by, quarantine_reason, and in `evidence_json` the owned identities, classification evidence and checkpoint.
- **Session:** `native_session_id` (Claude pre-chosen, Codex learned late and merged with COALESCE), `transcript_path`. The `attempts.transcript_offset` column is never written by the daemon; the offset lives in launch.json notes.
- **Capacity:** readings carry `attempt_id` when an adapter supplied it; classification readings are dropped if the credential's identity does not bind (claude.py:1463-1469). A `decisions` row is written per reservation with the full Decision and policy_hash (daemon.py:2629-2630).
- **Tokens: nothing recorded.** There is no per-attempt token column, and `jobs.max_tokens_observed` has no reader in daemon.py (grep). The Claude classifier's evidence has no token fields (claude.py:1476-1485).

## 7. Multi-turn: one attempt per turn vs a warm worker

**What exists today is turn = job, not turn = attempt.** A follow-up is a `kind="resume"` job:
- `_resume_submission` requires the source job to be terminal with no live or quarantined attempt, pins the source attempt's lane and `model_requested`, reuses the source worktree in place, and sets `independent=True`. The resume identity goes into the digest (daemon.py:1031-1065).
- Admission takes the job-held lease `native-session:<lane>:<sid>` (daemon.py:2586-2590).
- Launch refuses a changed model or lane, allowing only the re-enrolled successor (daemon.py:2836-2841, 797-810), and runs `claude -p --resume` or `codex exec ... resume <thread> -` (claude.py:1333-1380; codex.py:460-473).

Multiple attempts inside one job are retries. Making each turn an attempt would break at-most-one-accepted-attempt and the stale-seq guard (daemon.py:3209-3211, 3283-3287), plus the retry and exclusion counting (daemon.py:716-741). So per-turn execution should stay one job per turn, grouped under a new conversation entity.

**Changes needed for per-turn jobs**
1. **Schema:** conversation and message tables through a numbered migration 6 (store.py:29-40). A client message UUID maps naturally onto `request_id` + `payload_digest` (daemon.py:936-940). Bumping the version makes older read-only openers fail (store.py:72-73).
2. **Queued follow-ups:** submit refuses a resume while the previous turn is active (daemon.py:1036-1040), so "queued next message" needs a daemon-owned durable queue per conversation, not client retries.
3. **Replay:** C-4.5 automatically retries a lost read-only attempt and a transient one on the same lane (daemon.py:3291-3294). For a turn that may already be in the native transcript, that is replay. It has to be disabled, or gated on transcript reconciliation using `transcript_offset`.
4. **Cancel after exit:** a completed turn is discarded when cancel races finalization (daemon.py:3288, 3317). This needs a distinct "completed, cancel too late" result.
5. **Model changes:** changing model mid-conversation is refused by the equality check (daemon.py:2839), and Codex resume ignores the model (codex.py:470-473).
6. **Approvals:** there is no channel. stdin is a file (guardian.py:90-95), and permissions are fixed at launch: `--dangerously-skip-permissions` or plan mode with a named tool list (claude.py:1208-1225). The `approval` wait reason has no writer.
7. **Live output:** stdout and stream files sit under `jobs/<id>/a<seq>/` while the attempt runs, but in the files I read nothing tails them. A cursor API would have to add that.
8. **Retention:**
   - every job that is a parent is pinned forever (retention.py:77), so a chain of turn jobs is never pruned;
   - `events` is never pruned (retention.py:316-318).
9. **Scaling of long chains:**
   - `_parent_blocks` walks every ancestor for every active attempt, so cost grows with chain depth (scheduler.py:218-242);
   - `max_child_jobs` (8) breaks a star layout where all turns share one parent (daemon.py:1256-1258).
10. **Latency, inferred and not measured:** each turn pays:
    - git inspection at submit and at admission, including a temp-index snapshot for writable jobs (daemon.py:879-885, 1950-1955),
    - two capacity views,
    - a possible inline probe, the Codex guard preflight and credential resolution,
    - `ps` retries, and 50 ms tick hops,
    - waiting for export before the native-session lease frees (daemon.py:3368).

**A warm worker conflicts with these guarantees**
- Completion is defined by the guardian exit receipt.
- Finalization requires an empty census.
- The lane slot lease is held for the attempt's whole life, so an idle worker holds capacity.
- The credential is resolved once at launch, so account privileges can go stale.
- `max_wall_s` kills a long-lived process.
- Readings and closures are recorded only at finalization.

A warm worker therefore needs its own daemon-written per-turn start and end receipts, classification and attestation over a per-turn byte range, re-admission per turn (closures, reserve, identity, desktop), lease release or idle expiry, and per-turn cancellation that does not kill the worker.

**Invariants a per-turn attempt must preserve**
1. Intent is durable before dispatch (prompt and manifest published, row committed; daemon.py:962-988), and the request is idempotent by id and digest.
2. Every turn goes through `evaluate` inside the reserving transaction: closures by scope, the Fable reserve, the floor, desktop, owner, identity and slot caps. No bypass.
3. Lane and model stay immutable per attempt; a resume stays on its lane or the re-enrolled successor, with the same model.
4. One writer per native session (the native-session lease is held through retry, export and quarantine), and one writer per worktree.
5. The launch gate: no provider spawn before the guardian's identity is committed.
6. Only an identity-verified group or process is signalled; an unknown liveness never counts as dead; release requires a verified-empty census, else quarantine.
7. The on-disk exit receipt wins; no rc means `lost`, never `succeeded`.
8. At most one accepted result per job; results from a stale seq are ignored.
9. The served model is attested from this turn's transcript slice starting at `transcript_offset`.
10. Credentials never reach disk (`env_add` stripped; relaunch credentials re-resolved in memory, daemon.py:3133-3151), and API-key variables are removed.
11. Readings count toward capacity only when the credential's identity binds; `auth-dead` disables the lane.
12. No automatic replay after an ambiguous provider write. Current C-4.5 behaviour must change for conversational turns.
13. Cancelling a turn cascades only through non-`independent` children; resume jobs are independent.