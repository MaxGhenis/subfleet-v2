# Legacy native Swift cockpit (desktop workspace to recover)

Source: `/Users/maxghenis/chief-of-staff-worktrees/subfleet-traycer-port/subfleet/app/`. HEAD is `8607b3c8`, and five tracked files have uncommitted changes. `app/tests/` is untracked. All paths below are relative to `app/` unless marked otherwise.

## (0) Size and shape

| File | Lines (working tree) | Lines at HEAD | Uncommitted +/- |
|---|---|---|---|
| CockpitStore.swift | 1803 | 1133 | +731 / -61 |
| CockpitView.swift | 3633 | 2335 | +1403 / -105 |
| SubfleetApp.swift | 510 | 482 | +80 / -52 |
| SubfleetCLI.swift | 443 | 295 | +150 / -2 |
| Info.plist | 16 | 16 | +1 / -1 |
| build.sh | 17 | 17 | unchanged |
| tests/BrokerClientTests.swift | 305 | untracked | new |

Sources: `wc -l`, `git diff HEAD --numstat`, and `git show HEAD:… | wc -l`.

Decoding style: nearly everything is decoded with tolerant `JSONSerialization` dictionary accessors that accept several key aliases (`CockpitStore.swift:17-90`), not with `Codable`. The only `Codable`/`Decodable` types are `BrokerRequest`, `PendingBrokerSubmission` and the menu `Snapshot` family.

## (1) Feature inventory

| Feature | Implemented at | Backing data source |
|---|---|---|
| Menu-bar label: Codex lanes dispatchable/total, warning glyph | SubfleetApp.swift:188-200, 500-508 | `~/chief-of-staff/state/subfleet/snapshot.json` (:15-16, :143) |
| Quota popover (Codex homes, Claude accounts, active limit, unenrolled hint) | SubfleetApp.swift:278-441 | Same snapshot, with `claude-statusline.json` `rate_limits.*.used_percentage` taking priority (:151-163). Reloaded every 30 s (:137) |
| "Refresh (live probe)" | SubfleetApp.swift:171-186, 452-461 | `/bin/launchctl kickstart gui/<uid>/com.maxghenis.cos.subfleet`, then 20 reloads at 1.5 s |
| Copy best CODEX_HOME | SubfleetApp.swift:329-340 | `snap.codex.fleet.best_home` → pasteboard |
| Start at login | SubfleetApp.swift:281, 463-469 | `SMAppService.mainApp.register/unregister` |
| Open / Open Cockpit (⌘O), Quit | SubfleetApp.swift:309-317, 445-450, 470-476, 481-484 | `openWindow(id:"cockpit")` plus `NSApp.activate` |
| Main window: split view with Workspace/Fleet/Runtime sidebar | CockpitView.swift:38-101, 103-265 | Store state |
| Fleet family rows and per-account rows (5h/overall/model %, scoped limits, resets, "Probe on use", "Current app", "Next", in-flight) | CockpitView.swift:142-197, 267-526, 3519-3600 | `subfleet capacity --cached --json` at launch (CockpitView:72 → CockpitStore:1188-1225). `--refresh` on user refresh or when stale |
| Stale-capacity banner and Refresh | CockpitView.swift:143-157 | `capacityIsStale` (CockpitStore:1184-1186): cache `fresh`/`ttl`/`age` |
| Runtime: CLI and Logpile path overrides | CockpitView.swift:199-233 | UserDefaults `SubfleetCLIPath`, `LogpileCLIPath` |
| Issue strip (per-source errors) | CockpitView.swift:528-556 | `store.issues` (CockpitStore:1795-1802) |
| Compose: task, tier, workdir, service tier, prompt, one-click "Route & dispatch" (Return sends) | CockpitView.swift:558-794 | `subfleet route …` then `subfleet run …` (CockpitStore:1573-1712) |
| Route inspector (provider/model/lane/service, reason, fallbacks/rejected, cache age, raw JSON) | CockpitView.swift:796-901 | `RoutePreview` |
| Agent runs list (5 s poll while visible) | CockpitView.swift:83-87, 903-1009 | `subfleet runs --last 100 --json` (CockpitStore:1227-1247) |
| Run detail: output/prompt/error/lane log/receipt tabs, Reveal in Finder, 2 MiB truncation note | CockpitView.swift:1011-1178 | `subfleet runs show <id> --json` (CockpitStore:1714-1753) |
| Stop run (confirmation dialog) | CockpitView.swift:961-971, 1044-1050 | `subfleet kill <id> --json` (CockpitStore:1755-1776) |
| Conversations list: search, provider filter, Live toggle, context menu (copy IDs, reveal cwd), ⌘R | CockpitView.swift:1493-1653, 1655-1709 | `subfleet sessions --json` (CockpitStore:1249-1271), repolled every 5 s on this tab (CockpitView:88-94) |
| Transcript with Markdown rendering | CockpitView.swift:1743-1809, 1960-2758 | `subfleet sessions show <id> --json` (CockpitStore:1273-1318) |
| Native binding popover (provider/account/profile/session/workspace/model/permissions plus explanation) | CockpitView.swift:1867-1958 | `SessionRecord` fields |
| Send/queue follow-up (queuing while a turn runs) | CockpitView.swift:2760-3075 | Broker socket `enqueue` (CockpitStore:1320-1386) |
| Local outbox bubbles, cancel queued, copy, "Mark handled…" | CockpitView.swift:1782-1787, 1812-1865 | Broker `list` every 1 s (CockpitView:43, 97-99), plus `cancel` and `resolve` |
| Unconfirmed-ack banner and "Retry saved message" | CockpitView.swift:1788-1794, 2797-2811 | Pending journal plus broker `get`/`enqueue` (CockpitStore:1421-1449) |
| Status rail: live activity label, elapsed timer, queued count, Activity popover, Stop, View run | CockpitView.swift:2812-2892, 3177-3379 | Receipt `activity`; `runs` for the run linked by `run_id` |
| Warm session on selection | CockpitView.swift:1569-1574 | Broker `prepare` (CockpitStore:1451-1454) |
| Image attachments (paste, paperclip, chips) | CockpitView.swift:1188-1491, 2913-3124 | Private files; see (6) |
| Task lineage (read-only history) | CockpitView.swift:3381-3485 | `logpile task-list --json` (SubfleetCLI:432-442) |

## (2) External calls

### Process launcher
`SubfleetCLI.callExecutable` (SubfleetCLI.swift:286-384) runs the binary with `Process` and no shell. It has a 60 s default timeout, sends SIGTERM and then SIGKILL after 2 s (:228-237), and extends PATH with Homebrew, `~/.local/bin`, `~/.bun/bin` and `~/bin` (:386-408).

Binary resolution (:247-260): the UserDefaults `SubfleetCLIPath` override is used alone if set. Otherwise the first executable wins, in this order:
1. `~/.local/bin/subfleet-local`
2. `~/bin/subfleet`
3. `/opt/homebrew/bin/subfleet`
4. `/usr/local/bin/subfleet`

### Exact CLI invocations
| Call | Site | Decoder and required fields |
|---|---|---|
| `capacity --cached --json` / `capacity --refresh --json` | CockpitStore:1193-1195 | Envelope `data`/`result`/root (:87-90). **CapacityFamily**: `families{<id>:{available, dispatchable/available_accounts, accounts/total, headroom_score/headroom, best_email/best/best_id, confidence}}`, or grouped from `accounts[]` (:103-147). **CapacityAccount** (:224-266): `family, id/account_id/email/home, email, home, status/base_status, dispatchable, headroom_score, five_hour/primary, weekly/seven_day/secondary, model_windows{}, scoped_limits/limits[], limited_until, short_window_until, confidence, probe_status, active, enrolled, is_primary_home, is_shadowed_by_app, in_flight`. **CapacityUsageWindow**: `used_percent/percent, reset_at/resets_at, confidence/source, tokens, capacity`. **CapacityScopedLimit**: `kind, group, percent, severity, resets_at, is_active, scope_model, scope_surface`. **CapacityFreshness**: `cache{age_seconds, ttl_seconds, fresh, probed_at, hit}, generated_at/as_of` |
| `route --task T --tier X -C <dir> --json` | CockpitStore:1598-1601 | **RoutePreview**: requires `schema_version==1, kind=="subfleet.route-preview", ok, allowed`, and `selected{family, model, lane, model_alias}` must be consistent with `allowed`. Also reads `reason/message, error.message, fallbacks[], rejected[], capacity_freshness.age_seconds` (:317-364) |
| `run --task T --tier X -C <dir> [--fast\|--standard] -d --stdin --json`, prompt on stdin | CockpitStore:1689-1697 | **DispatchReceipt**: `schema_version==1, kind=="subfleet.run", ok, status`. When ok, requires `status=="dispatched"`, `detached==true` and a `run_id`. When not ok, requires `error.message` (:879-920) |
| `runs --last 100 --json` | CockpitStore:1232 | **RunRecord** from a bare array or `runs/items/data/results`: `id/run_id, family/provider, model/model_id, lane/account/home, status/state, rc/result_code/exit_code, started_at, finished_at, duration_s, workdir/cwd, out_path, out_bytes, pid` (:392-424). Running = RUNNING/QUEUED/STARTING/DISPATCHING. Orphaned = ORPHANED. Success = FINISHED with rc 0 (:382-390) |
| `runs show <id> --json` | CockpitStore:1734 | **RunDetail**: `schema_version==1, kind=="subfleet.run-detail", ok==true, id==selected`, `metadata` must be a matching RunRecord, and `artifacts.{prompt,output,error,lane_log}` each need `{path, exists, bytes, content_bytes, truncated, content}` (:437-523) |
| `kill <id> --json` | CockpitStore:1764 | **KillReceipt**: `schema_version==1, kind=="subfleet.kill-results", ok, error_count`, exactly one `results[]` row `{run_id, ok, status, error.message}` (:536-563) |
| `sessions --json` | CockpitStore:1254 | `schema_version==1, kind=="subfleet.sessions", ok==true, items[]`. Every row must decode (:643-664). **SessionRecord**: `id, provider∈{claude,codex}, native_id, display_name, profile.home_ref` (required), plus `display_name_source, cwd, profile.home_path, profile.account, created_at_ms, updated_at_ms, archived, starred, live, connected, resume_capable, continuation_blocker, pid, model, reasoning_effort, permission_mode, sandbox_mode, source_kind` (:608-641) |
| `sessions show <id> --json` | CockpitStore:1293 | **SessionDetail**: `schema_version==1, kind=="subfleet.session-detail", ok, session` (id must match), `messages[]{role∈{user,assistant}, text, id, phase, created_at}, truncated, content_policy, omitted_event_count` (:682-726) |
| `broker start --json` (15 s timeout) | SubfleetCLI:70-79 | Exit code only |
| `logpile task-list --json`, binary `~/logpile-subfleet-integration/.venv/bin/logpile` or `LogpileCLIPath` | SubfleetCLI:415-442 | **HistoryTask**: `tasks/items/data[]{task_id/id, providers[], run_count, attempt_count, handoff_count, first_occurred_at, last_occurred_at, last_outcome_status, last_outcome_exit_code}` (CockpitStore:962-985) |
| `launchctl kickstart gui/<uid>/com.maxghenis.cos.subfleet` | SubfleetApp:174-177 | None |

Menu `Snapshot` Decodables (SubfleetApp.swift:20-125):
- `generated_at`
- `codex{homes[]{home, email, verdict, windows{primary, secondary{used_percent, reset_at}, source, as_of}, duplicate_of}, fleet{total_homes, dispatchable_now, best_home, earliest_reset}}`
- `claude{accounts[]{email, active, enrolled, probe{status, five_hour, seven_day{used_percent, reset_at as string or epoch s/ms}, confidence}, statusline, live{five_hour_pct, seven_day_pct, model_weeks, source}, oauth_status}, statusline, active_limit{kind, reset_at}, tier, account{email}}`

### Broker socket client (SubfleetCLI.swift:4-148)
- **Socket:** `$SUBFLEET_BROKER_SOCKET`, else `<stateDir>/broker.sock`. `stateDir` is `$SUBFLEET_STATE_DIR`, else `~/chief-of-staff/state/subfleet` (:40-54).
- **Framing:** one JSON object per line from `JSONEncoder` plus `\n`, one request per connection. The client reads until the first newline, with a 16 MiB cap and 5 s send/receive timeouts (:91-147).
- **Checks:** `SO_NOSIGPIPE` is set, and `getpeereid` must match `geteuid` or the client refuses to send (:117-121).
- **Auto-start:** if the connect fails (`unavailable`), the client runs `subfleet broker start --json` and retries the identical bytes once (:58-68).
- **`BrokerRequest` (Codable, :7-24):** `op, message_id, session_id, prompt, image_paths, service_tier, resolution, confirm`.

Broker ops sent by the app:
| op | Site | Payload |
|---|---|---|
| `enqueue` | CockpitStore:1092-1095, 1364 | `message_id` (lowercase UUID), `session_id`, `prompt`, `image_paths` (private snapshot copies), `service_tier` ∈ {fast, standard} or omitted |
| `get` | :1422 | `message_id`. `code=="message-not-found"` is read as not admitted (:1426) |
| `list` | :1462 | `session_id` or null for all sessions |
| `cancel` | :1487 | `message_id`, sent only when status is `queued` |
| `resolve` | :1495-1497 | `message_id, resolution:"handled", confirm:true`, sent only when status is `delivery-unknown` |
| `prepare` | :1453 | `session_id`; result ignored |

Responses:
- **SessionContinuationReceipt** (:804-849): requires `schema_version==1`, `kind=="subfleet.session-continuation"`, `accepted==true`, `session_id, message_id, provider, status`. Also reads `run_id, error.message/message, prompt, created_at, updated_at, attachment_count / image_paths.count, error.code/code, native_message_id, resolution, activity`.
- Active statuses are queued, starting, dispatched, delivered-live, failover-dispatched and running (:786-788). The others are finished, cancelled, delivery-unknown and error.
- **SessionActivity** (:750-766): `schema_version==1, revision, current{sequence, kind, label, occurred_at}, events[]`.
- **Outbox list** (:851-864): `kind=="subfleet.outbox", ok==true, messages[]`.

The legacy server (`subfleet/broker.py`, untracked) also implements `ping` and `interrupt` (broker.py:166, 187, 204). The app never sends `interrupt`. Conversation Stop instead calls `subfleet kill <runID>` on the whole run (CockpitView:3171-3175).

## (3) Persisted state

| Item | Where | Details |
|---|---|---|
| UserDefaults `SubfleetCLIPath` | SubfleetCLI:243; CockpitView:106 | CLI override; wins over all candidate paths |
| UserDefaults `LogpileCLIPath` | SubfleetCLI:413; CockpitView:107 | |
| `subfleet.cockpit.codex-accounts-expanded` / `claude-accounts-expanded` | CockpitView:108-109 | Sidebar disclosure state |
| Implicit SwiftUI/AppKit keys | observed with `defaults read org.maxghenis.subfleet` | `NSWindow Frame cockpit`, `NSSplitView Subview Frames cockpit, SidebarNavigationSplitView`, and a stale `NSWindow Frame SubfleetCockpitWindow` (the HEAD autosave name). `SubfleetCLIPath` is currently **not** set |
| Pending-send journal `<stateDir>/cockpit-client/pending-messages.json` | CockpitStore:1041-1064 | `[sessionID: PendingBrokerSubmission{request: BrokerRequest, sourceImagePaths}]`. Directory 0700, file 0600, atomic write plus fsync. Loaded in `init`; its keys become `uncertainSessionIDs` (:1033-1039). One slot per session |
| Per-message image snapshots `<stateDir>/cockpit-client/images-<uuid>/<n>.<ext>` | CockpitStore:1066-1104 | Regular files only (no symlinks), 0600, fsynced. Deleted only after acceptance or a definitive rejection (:1106-1118); kept when the outcome is ambiguous |
| Editable composer attachments `~/chief-of-staff/state/subfleet/composer-attachments/<uuid>.png` | CockpitView:1210-1213, 1340-1365 | Hard-coded; ignores `SUBFLEET_STATE_DIR`. Unprotected files older than 7 days are pruned when the Conversations tab appears (:1295-1323, 1564-1568) |
| Drafts (text, attachment list, per-session service tier) | CockpitView:1196-1201, 41 | **In memory only** (`@Published` dictionaries). They survive tab switches but **not an app restart**; after a restart the attachment files are orphaned until the 7-day prune |
| Compose fields (task/tier/workdir/prompt/service tier) | CockpitView:562-566 | `@State` only; not persisted |
| Outbox mirror | CockpitStore:1001, 1401-1419 | In memory; merged by `updated_at` and then activity revision; last 100 per session |

Files the app only reads: `snapshot.json` and `claude-statusline.json` (SubfleetApp:143, 151). `alerts.json` is mentioned in a comment (:4) but never read.

## (4) Window, Dock, menu bar and activation

- **Info.plist:** `LSUIElement` is **false** in the working tree and was true at HEAD, so the app has a Dock icon. `CFBundleIdentifier` is `org.maxghenis.subfleet` and version is 0.1.0 (1) (Info.plist:7-13).
- **Scenes** (SubfleetApp.swift:489-510): a single-instance `Window("Subfleet", id:"cockpit")` with `defaultSize(1120,720)`, plus `MenuBarExtra { ContentView } .menuBarExtraStyle(.window)`.
  - The root view enforces a minimum of 920×610, forces the dark scheme and sets the tint (CockpitView:68-70).
  - `CockpitStore.shared` is a singleton (CockpitStore:990; SubfleetApp:492).
- **Open action:** `openCockpit()` calls `openWindow(id:"cockpit")` and then `NSApp.activate(ignoringOtherApps:true)` (SubfleetApp:481-484).
  - It is wired to the popover header "Open" (:309-317) and footer "Open Cockpit" with `.keyboardShortcut("o")` (:445-450).
  - The ⌘O shortcut is attached to a popover button. Whether it works when the popover is closed is **UNVERIFIED**.
- **What HEAD did:** an AppKit `CockpitWindowController` with an `NSWindow` titled "Subfleet Cockpit", minimum size, frame autosave `SubfleetCockpitWindow`, and a teardown on close (visible as removed lines in the SubfleetApp diff).
- **Not present:** grep finds no `NSApplicationDelegateAdaptor`, `applicationShouldHandleReopen`, `.commands`/`CommandGroup` or `setActivationPolicy`.
  - Whether the window opens at launch, and whether a Dock click reopens a closed window, depends on SwiftUI defaults for `Window` scenes. Both are **UNVERIFIED** (no GUI run).
  - `NSApp.activate(ignoringOtherApps:)` is deprecated on macOS 14+, according to the SDK. Deprecation warnings were not checked with a build this session.
- **Timers:** runs are refreshed every 5 s on the Runs tab; sessions, the selected session and runs every 5 s on the Conversations tab (CockpitView:42, 83-96). Broker `list` runs every 1 s in all sections (:43, 97-99). Launch starts three parallel refreshes, then loads the selected session (:71-82).

## (5) Settings controls

The only pickers are Task, Capability, Service (Compose), Service (Conversation), Artifact and Provider filter (grep `Picker(` in CockpitView.swift).

- **Compose:**
  - Task is one of lookup/research/sweep/review/build/authored-prose/strategy/adjudication; tier is trivial/easy/standard/hard (CockpitView:568-569, 612-621).
  - Service tier (`CodexServiceTierOverride` inherit/fast/standard, CockpitStore:923-949) is enabled only when the previewed route selected Codex, and resets to inherit otherwise (CockpitView:577-579, 639-653, 748-750).
  - These are sent as CLI flags `--task`, `--tier`, `-C` and `--fast|--standard` (CockpitStore:1689-1696). A non-Codex override is refused client-side (:1676-1684).
  - Dispatch is gated on an exact route fingerprint (task/tier/normalized workdir) plus fresh capacity. Changing any field invalidates it (CockpitStore:1573-1675; CockpitView:745-747).
- **Conversation:**
  - The service-tier menu appears only for Codex sessions (CockpitView:2954-2966) and is stored per session in memory (:1646-1652).
  - It is sent as broker `service_tier` (CockpitStore:1094) and refused for non-Codex sessions (:1329-1335).
- **Absent:** there are **no model, reasoning-effort, permission-mode, or account/lane pinning controls** in either surface.
  - The model, permission mode and sandbox are only displayed, from the session catalog (binding popover CockpitView:1914-1918; safety caption :3139-3151).
  - `reasoning_effort` is decoded (CockpitStore:636) but never shown.
  - The app never passes v2's `-m`, `-a`, `-H`, `-s` or `--request-id` (v2 `subfleet/cli.py:2210-2255`).

## (6) Image paste and attachments

- **Paste interception:** `ComposerNSTextView` overrides `paste`, `pasteAsPlainText`, `pasteAsRichText` and ⌘V `performKeyEquivalent` (CockpitView:1380-1413).
  - Return sends; Shift- or Option-Return inserts a newline; IME marked text is respected (:1478-1489).
  - Compose passes `onPasteImages: { _ in false }` (:690), so image paste only works in conversations.
- **Import** (:1225-1253):
  - Image file URLs on the pasteboard are tried first, then `NSImage` objects.
  - Each image is re-encoded to PNG, capped at 20 MiB (:1342-1347), with at most 8 per message (:1205, 3084-3092, 2953).
  - Files get UUID names and 0600 permissions under `composer-attachments` (:1348-1359).
  - The file picker allows png/jpeg/gif/webP (:3102-3124).
- **Error handling:** a failed import consumes the paste (so AppKit does not insert text), then shows an accessible announcement (:3094-3099, 3126-3137). Paste and editing are disabled when a session is neither resume-capable nor connected (:2782-2784).
- **Send lifecycle:**
  - `beginUsing` reference-counts the files during a send (:3047, 1268-1293).
  - `preparePendingSubmission` copies them into immutable `images-<messageID>/` snapshots, and those paths go in `image_paths` (CockpitStore:1066-1104).
  - Editable originals are deleted only after acceptance (CockpitView:3050-3073).
  - Ambiguous snapshots survive a restart for retry (confirmed in tests:176-189).
- **Display:** transcript bubbles show "N images attached" (CockpitView:1831-1834). An image-only prompt renders as "Image attachment" (:1822).

## (7) What the uncommitted diff adds over 8607b3c8

- **Delivery path.** HEAD sent follow-ups with `subfleet sessions continue <id> --stdin --json` and locked the composer while a turn ran (removed lines in the CockpitStore/CockpitView diffs). The working tree replaces this with:
  - the broker client (SubfleetCLI:4-148);
  - the durable pending journal and image snapshots;
  - idempotent retry by the same message ID and lost-ack resolution via `get`;
  - the outbox (`list`/`cancel`/`resolve`), `prepare` warm-up, and queue-while-running;
  - transcript reconciliation that hides a local bubble once the prompt appears natively, matched by `native_message_id` or text plus timestamp (CockpitStore:1502-1546);
  - `SessionActivity` live labels with the activity popover and elapsed timer;
  - Stop continuation and the "Mark handled" flow for `delivery-unknown`.
- **Composer:** the NSTextView editor, image paste/attach, drafts lifted to `CockpitView` so they survive tab switches, and a per-session service tier.
- **Compose:** a single "Route & dispatch" action with automatic capacity refresh and route validation (CockpitStore:1619-1652), replacing the mandatory preview followed by a ⌘Return Dispatch. Also Return-to-send and the `--fast`/`--standard` override.
- **Fleet:** per-account decoding (`CapacityAccount`/`UsageWindow`/`ScopedLimit`) and sidebar rows (CockpitView:305-526, 3519-3600), freshness metadata (generated/probed/cache hit), and a non-forced capacity read at launch with parallel initial loads.
- **Runs:** `isOrphaned`/`completedSuccessfully`, stricter `DispatchReceipt`, and a `killSelectedRun(expectedID:)` guard.
- **App shell:** SwiftUI `Window` scene plus `openWindow`, replacing the NSWindow controller. Also a popover "Open" button, `LSUIElement` set to false, and quota parsing that accepts `reset_at` as a string or epoch in s/ms, fractional-second ISO dates, a weekly fallback for Codex rows, reset times on Claude rows, and a "probe on use" state.
- **Tests (untracked):** `tests/BrokerClientTests.swift` uses a fake Unix-socket broker and a fake CLI script, run under `/tmp/sf-swift-*`. It covers acknowledgement, dedup of a lost ack, a restart with the 0600 journal, exact-ID retry, image snapshot retention, cancel, activity decoding, reconciliation, a queued follow-up not hiding an active turn, and the compose call sequence `capacity → route → run → runs`.
  - The file header says to compile it with `../SubfleetCLI.swift` and `../CockpitStore.swift`. `build.sh` does not build it, and the exact command is **UNVERIFIED** (not compiled here).

## (8) Coupling points that must change for v2

1. **The CLI resolves to v1 first.** `~/.local/bin/subfleet-local` exists and runs the legacy v1 Python CLI (`subfleet.cli`) from the dirty checkout. `~/bin/subfleet` runs v2 `subfleet.compat` from `~/.local/share/subfleet/current` (both verified with `cat`). The app prefers the former (SubfleetCLI:254-259).
2. **The broker is started implicitly and pinned to legacy state.** Any failed connect runs `broker start` (SubfleetCLI:62-79), and that includes the 1 s outbox poll and the `prepare` on every selection. v2 has no `broker` verb: it is absent from v2 `cli.py` subcommands (2181-2405) and from the compat `PERMANENT` table (compat.py:105-154). The state dir and socket default to `~/chief-of-staff/state/subfleet` via `SUBFLEET_STATE_DIR`/`SUBFLEET_BROKER_SOCKET`, while v2 uses `SUBFLEET_HOME` defaulting to `~/.subfleet` (v2 client.py:48-54).
3. **Client journals live under legacy state.** `cockpit-client/pending-messages.json`, the `images-*` folders and `composer-attachments/` are all there (CockpitStore:1041-1044; CockpitView:1210-1213). They are migration inputs.
4. **The quota menu is v1-sourced.** It reads `snapshot.json`/`claude-statusline.json` and kickstarts `com.maxghenis.cos.subfleet` (SubfleetApp:15-16, 143-186). The current v2 menu reads `<SUBFLEET_HOME>/status.json` (v2 app/SubfleetApp.swift:2, 24-26). The hint text `subfleet enroll <email>` (:385) is `lanes enroll` in v2 (compat.py:132).
5. **Verb and flag mismatches** against the v2 parser:
   - `capacity` is aliased to `status`. `--cached` is dropped (compat.py:119, 254-259), and `status` defines only `--json` (cli.py:2183-2185). What happens with `--refresh` is **UNVERIFIED**.
   - There is **no `route` verb**. The nearest equivalents are `why --task --tier` (cli.py:2339-2347) or `run --dry-run` (:2256-2257).
   - `run` has no `--stdin`, `--fast` or `--standard`. The prompt is positional or `-p` (cli.py:2204-2262), and grep finds `stdin` only in the hook/ping paths.
   - `runs --last`, `runs show`, `kill` and `sessions --json` exist (:2264-2299, 2389-2396).
   - **`sessions show` does not exist.** The v2 sessions verbs are list, continue, tickle, muster, revive, mirror, retire, unretire and handoff (sessions/cli.py:578-645).
   - v2 `sessions continue` is "nudge, roll-call, or recover" (:583), not arbitrary-message send.
6. **Strict schema envelopes.** The app rejects output unless `kind` is one of `subfleet.route-preview`, `.run`, `.run-detail`, `.kill-results`, `.sessions`, `.session-detail`, `.session-continuation` or `.outbox`, with `schema_version:1`. A grep of v2 `subfleet/*.py` and `subfleet/sessions/*.py` found none of these strings. Every strict decoder would therefore fail against v2 even where the verbs match. The capacity decoder is tolerant, but whether it fits v2's `status --json` shape is **UNVERIFIED**.
7. **Run status vocabulary.** RUNNING/QUEUED/STARTING/DISPATCHING/FINISHED/ORPHANED (CockpitStore:382-390). The v2 job-state names are **UNVERIFIED**.
8. **Stop granularity.** Conversation Stop kills the whole run. An owned-turn interrupt is needed, and the legacy `interrupt` op was never wired into the app.
9. **Bundle identity.** The legacy cockpit and the installed v2 menu app (2.0.2, `LSUIElement` true) both use `org.maxghenis.subfleet` (legacy Info.plist:7; v2 app/Info.plist), so they share a UserDefaults domain, including window frames and any `SubfleetCLIPath`. A development build needs a separate identity.
10. **Installation.** Legacy `build.sh` writes straight into `/Applications/Subfleet.app` and ad-hoc signs it (build.sh:5-16). v2 `app/build.sh` refuses `/Applications`.
11. **Logpile path.** It is hard-coded to `~/logpile-subfleet-integration/.venv/bin/logpile`, which does not exist (`ls`), so History currently shows "Logpile not connected".
12. **Drafts persistence gap.** Drafts are not persisted. The plan's "drafts survive app restart" requirement is not met by this code.

## (9) Module boundaries and suggested split

**CockpitStore.swift**
- :1-90 JSON helpers → `JSONAccess.swift`
- :92-297 capacity models → `CapacityModels.swift`
- :299-365, 526-564, 872-921 route, dispatch and kill receipts → `DispatchModels.swift`
- :367-524 runs → `RunModels.swift`
- :566-727 session catalog → `SessionModels.swift`
- :729-870 activity, continuation receipt and pending journal types → `ConversationModels.swift`
- :923-949 service tier → `Settings.swift`
- :951-986 history → `HistoryModels.swift`
- :988-1803 `CockpitStore` could split into:
  - capacity/runs/sessions reads (:1188-1318)
  - conversation outbox and journal (:1033-1118, 1320-1554) → `ConversationStore`
  - route/dispatch (:1573-1712)
  - run detail/kill (:1714-1776)

**CockpitView.swift**
- :5-101 shell
- :103-526 sidebar and fleet rows
- :528-556 issues
- :558-901 Compose and route inspector
- :903-1178 Runs
- :1180-1491 composer infrastructure (drafts, `ComposerAttachmentStore`, NSTextView bridge)
- :1493-1993 conversations list, detail, outbox turn, binding header, transcript turn
- :1995-2758 Markdown renderer (about 760 self-contained lines, with limits at :2067-2077 and an NSCache at :2039-2058) → its own file
- :2760-3379 `ConversationComposer` and activity popover
- :3381-3485 History
- :3487-3633 formatters

**SubfleetApp.swift:** snapshot models (:20-125), `QuotaStore` (:129-201), menu views (:232-485) and scenes (:489-510). Replace the first two with v2's `status.json` model.

**SubfleetCLI.swift:** broker socket client (:4-148) → replace with a v2 daemon protocol client; process runner (:150-409) → keep; Logpile (:411-443) → keep as optional.

Port strategy: the decoders are the v1 contract surface. Put a single typed v2 client module behind the store so the views need only minimal changes.