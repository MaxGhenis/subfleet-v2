# v2 daemon wire protocol and clients

Checked at worktree `/Users/maxghenis/subfleet-v2-lanes/desktop-workspace`, HEAD `3f155e5` (`git log -1`). All paths below are relative to that worktree unless stated. Nothing was built, run or changed. The daemon was not contacted.

## 1. Transport

- **Socket path:** `<state root>/daemon.sock`. The state root is `$SUBFLEET_HOME`, otherwise `~/.subfleet`, and is always made absolute (`subfleet/client.py:38,43,47-55,224-226`). The daemon unlinks and binds the socket, then sets mode `0600` (`subfleet/daemon.py:3424-3428`). Access is therefore same-user only. `cli.py:73` defines `AF_UNIX_PATH_MAX = 103`, and doctor checks the path against it (`doctor.py:332-340`).
- **Listener:** a single accept loop in `serve_forever`: `listen(64)` and a 0.2 s accept timeout so it can poll the stop flag. Each accepted connection is handed to the `readers` pool (`daemon.py:3423-3447`).
- **Framing:** newline-delimited JSON. `encode` writes compact UTF-8 JSON (`ensure_ascii=False`) plus `\n` (`protocol.py:275-279`). The daemon reads a connection with `reader.readline(1 MiB + 1)` in a loop (`daemon.py:3391-3395`).
  - **Several requests per connection.** The server keeps reading lines until EOF and sends each request to a pool (`daemon.py:3392-3408`). Responses can therefore come back out of order. A per-connection `write_lock` keeps them from interleaving (`daemon.py:3371-3385`). Only `id` correlates a response with its request.
  - **Python client:** opens one connection per call and reads one line (`client.py:282-309`). It never checks that `response.id` matches the request (`client.py:313-336`).
- **Size limits:**
  - Requests: more than 1 MiB gets `fail("", 2, "request exceeds 1 MiB")` and the connection keeps going (`daemon.py:3397-3403`).
  - Responses: the client aborts after 64 MiB with no newline (`client.py:42,206-209`). The daemon has no server-side cap on response size (none found in `_respond`/`encode`).
- **Timeouts:**
  - Client default: 15 s, applied as a single deadline for the whole read, not per `recv` (`client.py:41,184-210,286-300`). A timeout raises `ProtocolError(..., Exit.OPERATIONAL)` (`client.py:301-304`).
  - Per-call overrides: `wait` uses `WAIT_POLL_MAX_S + 15` (`cli.py:980,996`). `daemon.status` health checks use 3 s and 5 s (`cli.py:1735,1839`). Gate calls use 180 s (`gate/cli.py:97,102`). Doctor ping uses 5 s (`doctor.py:471`). The hook `wait` uses `poll + 10` (`hooks.py:482-483`).
  - Server: accepted connections get no idle timeout (no `settimeout` on `conn` in `daemon.py:3387-3445`). Under CPython's `accept()` semantics this means a blocking socket, so an idle open connection holds one of the 32 `readers` threads until EOF. **UNVERIFIED by test.**
- **Liveness before connecting:** `Client.check_available` reads `daemon.lock` (`{pid, boot_id, proc_start, version}`, written at `daemon.py:233-242`). It checks the recorded process with `ps` and the boot UUID, and raises `DaemonUnavailable` (exit 69) when the holder is provably dead. This runs once per client (`client.py:250-278`). A failed connect is also mapped to `DaemonUnavailable` (`client.py:291-297`).

## 2. Request/response envelope and version handling

- **Request:** `{"v":1,"id":"<str>","op":"<op>","args":{...}}` (`protocol.py:35-40`; contract `docs/acceptance-contract.md:174`). Clients normally send `id: ""`. Only `submit` passes `request_id` as the id (`client.py:283,287`; `cli.py:677`; `sessions/client.py:94`).
- **Response:** `{"id","ok","result"|null,"error":{"code","message","fix"}|null,"v":1}` (`protocol.py:43-56,325-330`). The client returns `result` only if it is a dict, and `{}` otherwise (`client.py:335-336`).
- **Server decode** (`protocol.py:282-297`):
  1. Invalid JSON, or a non-object body: `ProtocolError`.
  2. `data.get("v") != PROTOCOL_VERSION`: "unsupported protocol version …; this daemon speaks 1". This is an exact match. Because Python treats `True == 1` and `1.0 == 1`, a JSON `true` or `1.0` passes.
  3. `op not in OPS`: "unknown op".
  4. `args` that is not an object: error.

  Every decode failure is answered on the reader thread with **id `""` and code 2** (`daemon.py:3399-3403`), so a pipelining client cannot tell which request failed.
- **Handler errors** (`daemon.py:3371-3380`):
  - `ProtocolError`/`AdapterError` → their own `code`/`fix`.
  - `ValueError`/`TypeError`/`KeyError` → code 2, "invalid arguments".
  - Anything else → code 1, "operation failed; inspect daemon status". Only the exception type is logged.
- **Arguments:** `coerce_args` drops unknown keys and turns missing required keys into a `ProtocolError` (`protocol.py:315-322`). There is no type checking. `ping` and `readings` do not coerce at all (`daemon.py:1463-1465,1422-1424`).
- **Client version handling:** `decode_response` defaults a missing `v` to 1 (`protocol.py:311`). `client.call` raises `ProtocolError(..., OPERATIONAL)` if `response.v != 1` (`client.py:317-320`). An error code outside the `Exit` table, or code 0 on a failure, is rewritten to 1 (`client.py:36,321-334`; `Exit` is at `contracts.py:162-175`).
- **Result:** the version check is exact on both sides and there is no negotiation. Raising `PROTOCOL_VERSION` would break every existing client against a new daemon, and the reverse.

## 3. Every accepted op (`OPS`, `protocol.py:18-23`; dispatch at `daemon.py:1353-1481`)

There are 19 ops. Pool assignment comes from `daemon.py:3406`:
- `submit` and `gate.*` run on `workers`.
- `wait` runs on `waiters`.
- Everything else runs on `requests`.

| op | args (dataclass) | result shape | mutates? | code |
|---|---|---|---|---|
| `submit` | `SubmitArgs` (`request_id, kind, workdir, prompt_path, sandbox`, plus ~25 optional fields) `protocol.py:62-98` | `{job_id, request_id, created}`, or `{dry_run:true, decision}` | Yes, unless `dry_run`. Idempotent on `request_id`: the same id with a different `payload_digest` fails (`daemon.py:936-940`). The daemon reads the prompt from a **path** (`daemon.py:870` region) | `daemon.py:1366-1367,825-990` |
| `list` | `ListArgs{mine, running, last}` `protocol.py:138-142` | `{jobs:[row (+batch)]}` | No | `daemon.py:1368-1383` |
| `show` | `ShowArgs{job_id}` | `{job, batch, workspace, attempts, artifacts, notices}` | No (explicitly does not ack) | `daemon.py:1384-1397` |
| `wait` | `WaitArgs{job_ids, mine, last, deadline_s}` | `{jobs:[job+attempt], timeout:false}` or `{timeout:true}`. Server-side long poll capped at 60 s (`contracts.py:217`) | No | `daemon.py:1398-1399,1660-1680` |
| `kill` | `KillArgs{job_id, confirm_dead, force_release, operator_note}` | `{job_id, status}`: "cancel requested", "already finished", "not quarantined" or "resolution requested" | Yes: cancels the job family, or schedules quarantine resolution | `daemon.py:1400-1401,1682-1706` |
| `lanes` | `LanesArgs{action=list\|enroll\|hold\|release\|transfer, lane_id, credential, until, owner, dry_run, confirm_v1_edit}` `protocol.py:198-207` | list: `{lanes, leases}`; transfer: `{transfer, lanes}`; enroll: `{enrolled, lanes}`; hold/release: `{held, released, lanes, closures}` | list: no. The others: yes. Enroll runs a guardian-fenced provider turn **synchronously** under `_enroll_lock` | `daemon.py:1402-1421,314-439,493-522` |
| `readings` | `ReadingsArgs` exists but the daemon ignores it | `{readings, closures, status}` | Incidental only: `_desktop_identity` may append an identity event (`daemon.py:614-618`) | `daemon.py:1422-1424` |
| `why` | `WhyArgs{job_id \| task, tier, pinned_model, exclusions, allow_desktop}` | job: `{decision, decision_source, job, queue, route_error, refused, text}`; ad hoc: `{decision, text}` | No (incidental identity event possible) | `daemon.py:1425-1430,1483-1526` |
| `notice.pending` | `NoticeArgs{session_id}` | `{notices:[...]}`. Service notices appear with negative ids | No | `daemon.py:1431-1434,1459-1462` |
| `notice.ack` | `NoticeArgs{session_id, notice_ids}` | same | Yes | `daemon.py:1435-1442` |
| `notice.mark` | `NoticeMarkArgs{session_id, notice_ids, state, transport}` `protocol.py:183-196` | same | Yes | `daemon.py:1443-1458` |
| `ping` | raw `{text, session_id}` | `{pong:true, version, session_id, text, notice_id}` | **Yes, if `text` is non-empty**: inserts a `service_notices` row | `daemon.py:1463-1473` |
| `daemon.status` | none | capacity view, `status`, `pid`, `version`, `state_root`, `timers`, `active_attempts`, `admission` | Incidental identity event only | `daemon.py:1476-1480` |
| `gate.start` | `GateStartArgs` `protocol.py:101-119` | `{gate_id, status, code, round, subject, action, job_id, message}` (`gate/service.py:164-172`), or preview + `code:0` | Yes, unless `dry_run` | `daemon.py:1363-1365`; `gate/service.py:470-483` |
| `gate.poll` | raw `{gate_id}` | same | **Yes**: may submit a peer job, consume a verdict or reconcile a merge (`gate/service.py:313-331`) | `gate/service.py:484-485` |
| `gate.continue` | `GateContinueArgs` `protocol.py:122-135` | same | Yes, unless `dry_run` | `gate/service.py:486-490` |
| `sessions` | `SessionsArgs{action=state\|revived\|retire\|unretire\|nudged, ...}` `protocol.py:239-270` | state: `{sessions:{id:{retired, last_nudge, last_revive, revive_holder}}, lane_sessions}`; others: `{session_id, recorded, ...}` | state: no. The others append `events` rows | `daemon.py:1474-1475,1582-1658` |
| `pick` | `PickArgs{family, model, exclusions, min_headroom}` | `picker.rank` dict (`{generated_at, best, …}`, `picker.py:135`) | No | `daemon.py:1354-1359` |
| `operations` | `OperationsArgs{command, dry_run, target, hours}` | per command (`operations.py:89-159`) | `errors`, `canonical-model`, `login` and `brief` are reads. `watch`, `keepalive` and `reset` queue timer runs (they add a `timer.requested` event, `timers.py:130-148`) unless `dry_run` | `daemon.py:1360-1362` |

Anything else gets "unknown op" (`daemon.py:1481`); `protocol.py:293` normally rejects it earlier. Two quirks:
- Gate errors come back as **`ok:true` with `{code, status:"error", message}`** (`gate/service.py:471-473,492-493`).
- During startup recovery, gate ops return that error, and operations maintenance returns `status:"recovering"` (`timers.py:137-140`). All other ops are served during recovery.

## 4. Existing capability discovery

There is none. No op lists ops or features, and `PROTOCOL_VERSION` is a single integer (`protocol.py:16`). What clients do today:
- **Detect unknown ops by message text.** `sessions/client.py:25,98-115` looks for the substring `"unknown op"` in a `DaemonError`, a pattern its docstring says `cmd_lanes` also uses.
- **Detect missing features from the result shape.** Because unknown argument keys are ignored, an old daemon answers `lanes action=transfer` with the plain roster. The CLI catches this by checking for a missing `transfer` key (`cli.py:1587-1594`).
- **Version fields.** `ping` and `daemon.status` return `version`, which is the static `__version__ = "2.0.0a0"` (`__init__.py:3`), not a release or capability id. `daemon.lock` also records `version` (`daemon.py:233-234`).
- **`ping` is not a free probe.** With non-empty text it writes a service notice. `doctor --live` sends `text:"doctor"` (`doctor.py:471`), so every live doctor run queues an operator notice.
- **Stale docs.** The contract's op list (`docs/acceptance-contract.md:175`) is out of date. It is missing `notice.mark`, `gate.*`, `sessions`, `pick` and `operations`.

## 5. How the CLI and the Swift app consume the daemon today

**Python CLI and helpers:** socket first, falling back to a read-only store.
- `cli._client` builds a `Client(state_root)` for each verb (`cli.py:371-377`). Calls by op:
  - `status` → `daemon.status`, `lanes`, `readings`, `list` (`cli.py:414-424`)
  - `submit` (`cli.py:677,870,1499`), `wait` (`cli.py:996`), `list` (`cli.py:1097`), `notice.ack` (`cli.py:1228`), `show` (`cli.py:1243,1451`), `kill` (`cli.py:1372`), `lanes` (`cli.py:1579`), `why` (`cli.py:1626`), `ping` (`cli.py:1669`), `pick` (`cli.py:452`)
  - `operations` (`operations.py:173`)
  - gate CLI (`gate/cli.py:97,102`)
  - sessions kit (`sessions/client.py:48-115`, built at `sessions/cli.py:84`)
  - hooks: `notice.pending`, `notice.mark`, `list`, `wait` (`hooks.py:296,306,415,482`)
  - `tools/measure_release_gates.py:129`
- **Fallback when the daemon is down:** `DaemonUnavailable` → `Offline`, a `mode=ro` SQLite reader (`offline.py:1-8,125`). This covers `status` (`cli.py:426-431`), `brief` and `errors` (`operations.py:174-186`), hook pending notices (`hooks.py:357-360`), and runs, show and kill according to the offline docstring. Other verbs exit 69 (`cli.py:392-393`).
- **Entry points:** `bin/sf2` → `subfleet.compat` → `cli`. `bin/subfleetd` → `subfleet.daemon`.

**v2 Swift app (`app/SubfleetApp.swift`, 629 lines, menu-bar only):**
- It reads **only** `<SUBFLEET_HOME or ~/.subfleet>/status.json` (`SubfleetApp.swift:1-2,12-27`). It uses no socket, no `Process`, and no CLI (grep found no `Process`, `socket` or `daemon.sock`).
- It reloads on a 30 s `Timer` and on manual Reload (`:344-362`). It treats the snapshot as stale after 600 s (`:163`) and reports a missing or unreadable file (`:365-378`).
- `status.json` is produced by `status_json.build_status` and written atomically (`status_json.py:136-212`). The timers write it at the end of `probe_cycle` (`timers.py:535-537`) and after reset-credit runs (`timers.py:206-207`). Probe cadence is `probe_interval_s` = 60 s after the previous cycle (`default_policy.json:120`; `acceptance-contract.md:191`).
- The build signs ad hoc with `codesign --force --sign -` and no entitlements (`app/build.sh:48`). I infer that App Sandbox is not enabled, so a same-user socket connection should work (inferred, not tested). Bundle 2.0.2 build 4, `LSUIElement` (`Info.plist:7-13`).

**Legacy cockpit (for contrast; read-only at `/Users/maxghenis/chief-of-staff-worktrees/subfleet-traycer-port/subfleet/app`):**
- It shells out to a CLI through `SubfleetCLI`, trying `~/.local/bin/subfleet-local` first (`SubfleetCLI.swift:255-260`). Verbs used: `capacity`, `route`, `runs`, `sessions`, `sessions show`, `runs show`, `kill` (`CockpitStore.swift:1193-1764`).
- It also talks to a **v1 broker socket**, `~/chief-of-staff/state/subfleet/broker.sock` (`SubfleetCLI.swift:40-54`). It sends a flat envelope, `{op, message_id, session_id, prompt, image_paths, service_tier, resolution, confirm}`, with no `v` and no `args` (`SubfleetCLI.swift:7-24`). Ops used: `enqueue`, `get`, `prepare`, `list`, `cancel`, `resolve` (`CockpitStore.swift:1093,1422,1453,1462,1487,1496`).
- The v2 daemon would reject that envelope at `protocol.py:289` ("unsupported protocol version None"). The broker client code can be reused, but its envelope cannot.

## 6. Daemon concurrency model

**Threads** (`daemon.py:257-260,3431`):
- main accept loop
- `subfleet-control`: ticks every `tick_s = 0.05` (`:179,1759-1781`) and schedules attempt processing, exports, admission, timers and retention onto `workers` via `_schedule` (`:1715-1757`)
- `readers` (32): one per open connection
- `requests` (16)
- `waiters` (16)
- `workers` (12): **shared** by `submit`, `gate.*`, and all control-loop work
- Timers have their own pools (`timers.py:124-127`; sizes not checked)

**Locks:**
- `_submit_lock` (global; every submit is serialized, including its git calls with a 60 s cap, `daemon.py:203,827`; `contracts.py:191`)
- `_enroll_lock` (`:204,315`)
- `_busy_lock` (`:205,1723`)
- per-job export locks (`:3333-3337`)
- `_connection_lock` (`:222`)
- gate `_INIT_LOCK` and per-gate locks (`gate/service.py:121,474`)
- `changed`: a `Condition` that `_notify` broadcasts (`daemon.py:202,554-556`). `wait` no longer sleeps on it: since C-15.5 (2026-09-25) each waiter sleeps on its own event, set by the wait hub (`subfleet/waits.py`), which `_notify` pokes and which otherwise looks at the store's generation every 0.1 s and re-reads every 1 s.

**Store:**
- One `sqlite3` connection with `check_same_thread=False`, WAL, `synchronous=FULL`, `busy_timeout` 5000 (`store.py:64-80`).
- Every `query`, `one` and `transaction` takes **one process-wide `RLock`** (`store.py:59,147,171,175`). All daemon database access is serialized.
- `transaction()` runs `BEGIN IMMEDIATE` (savepoints when nested) and **automatically inserts an audit `events` row whenever `total_changes` moved** (`store.py:140-168`).

**Things that block request threads** despite C-16.4 (`acceptance-contract.md:177`: "never blocks on a provider process, a probe, or `ps`"):
1. `_desktop_identity()` reads the keychain and makes an HTTP profile call with `PROFILE_TIMEOUT_S = 15` (`adapters/claude.py:75,661,766-777`). It runs at most once per `reading_ttl_s` window, and only when a Claude lane exists (`daemon.py:622-643`). The ops that reach it are `lanes`, `readings`, `why`, `daemon.status`, dry-run `submit`, and hold/release, all on the `requests` pool. One such call can take up to the client's 15 s default.
2. `lanes enroll` runs a guardian-fenced provider turn inside the request (`daemon.py:384,441-470`). The CLI calls it with the 15 s default timeout (`cli.py:1579`).
3. `submit` does git work and a `ps` check (`_caller_instance`) under the global submit lock, on `workers` (`daemon.py:1126-1136`).
4. `_capacity_view` reads the lanes, readings, closures, attempts and jobs tables in full under the store lock (`daemon.py:571-584`).
5. Each `wait` holds a `waiters` thread for up to 60 s. The pool has 16 threads.
6. Each idle open connection holds a `readers` thread (see §1). With 32 held, new connections are accepted but never read, and clients time out. **Inferred, UNVERIFIED.**

## 7. Where conversation verbs and a cursor-based events op would fit

**Code locations:**
- **Protocol:** add ops to `OPS` (`protocol.py:18-23`), for example `capabilities`, `conversation.list/open/create`, `message.submit/status`, `approval.list/respond`, `turn.cancel`, and `events`. Give each a **new** args dataclass. `NoticeMarkArgs` is the precedent: new fields on shipped ops change their wire shape, and old daemons silently ignore them (`protocol.py:183-196`).
- **Dispatch:** add one branch in `Daemon.dispatch` that lazily delegates to a new module, following the `operations` and `gate` pattern (`daemon.py:1360-1365`). This keeps `daemon.py` (3494 lines) from absorbing the service.
- **Pools:** change the routing at `daemon.py:3406`.
  - `events` long-poll goes to `waiters` or a dedicated pool.
  - Anything that does filesystem work (attachments, workspace checks) goes to a dedicated pool, not the shared 12-thread `workers` pool that the control loop depends on.
  - No provider I/O in handlers (C-16.4). A handler records intent in a transaction, calls `_notify()`, and returns a receipt. Execution belongs to control-loop or worker code.
- **Idempotency:** reuse submit's model, `request_id` plus a payload digest where the same id with a different payload fails (`daemon.py:936-940`; `ids.payload_digest` `ids.py:54-74`). It already matches Stage 1's message-UUID rule.
- **Schema:** bump `SCHEMA_VERSION` (currently 5) with a numbered, additive entry in `MIGRATIONS`, and update `store_schema.sql` (`store.py:23-40,115-134`). Add conversation, message, turn, approval and attachment tables.
- **Cursor:**
  - The existing `events` table has `event_id INTEGER PRIMARY KEY` with indexes `(job_id, event_id)` and `(kind, event_id DESC)` (`store_schema.sql:207-217`). It is never pruned: retention deletes only artifacts, readings, notices, decisions, attempts and jobs (`retention.py:316-319`), and grep found no `DELETE FROM events`. But it has no `AUTOINCREMENT`, it is the global audit log with every transaction's automatic rows, and its `data_json` is unfiltered.
  - I recommend a dedicated append-only `conversation_events` table with `AUTOINCREMENT` (or an explicit seq), a whitelist of user-visible fields (no credentials, no hidden reasoning), and paging by both count and bytes. The daemon has no response size cap, and the client dies at 64 MiB.
  - `changed`/`_notify` already provides the wake-up for a long poll.

**Constraints:**
- **Do not raise `PROTOCOL_VERSION`.** Both ends check it exactly (`protocol.py:289`; `client.py:317`), and the CLI, hooks, sessions kit, gate CLI, doctor and tools all share `Client`. Add the new ops under `v:1`.
- **Old daemons:** a daemon without the new ops answers `ok:false, code 2, "unknown op '…'"` with id `""`. A `capabilities` op whose own "unknown op" reply means "legacy daemon" gives explicit discovery that fails **before any mutation**.
- **Error codes:** use only `Exit` codes (`contracts.py:162-175`) and report errors through `fail`, not the gate style of `ok:true` with an error inside.
- **Request size:** the 1 MiB cap (`daemon.py:3397`) rules out inline images. Pass attachments by path with ownership, symlink and permission checks, or add a chunked upload op.
- **Long-poll timing:** keep the server-side cap below the client deadline, as `wait` does (60 s server, +15 s client).
- **Swift client:** open one connection per request, or use unique ids and treat an id-`""` reply as fatal for the connection. Do not hold idle connections (§6.6).
- **Semantic collisions:** do not overload `sessions`. Its actions are store bookkeeping (`daemon.py:1582-1658`), and its docstring action list omits `revived` (`protocol.py:262`). The plan also warns that CLI `sessions continue` means interruption recovery, not sending a message.
- **Store lock:** the single store `RLock` means event and message reads must be indexed, short queries. Transcript bodies should not be read while holding it.

**UNVERIFIED:** timer pool sizes, gate `start` internals (`capture_pr`/`verify_pr_workspace` subprocess timeouts), whether `operations reset --dry-run`'s `actions.evaluate` makes network calls, and whether the idle-connection starvation actually happens in practice.