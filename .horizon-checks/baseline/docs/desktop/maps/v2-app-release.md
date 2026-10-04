# v2 native menu app, status JSON, frontend tests, and release/install procedure

Worktree: `/Users/maxghenis/subfleet-v2-lanes/desktop-workspace` at `3f155e5`. Everything below was checked read-only on 2026-09-24 at about 12:33Z. `git diff --quiet 795bfdb HEAD -- app/ tests/frontend/ subfleet/status_json.py` reports no difference, so current main's app source is the same source that was installed as 2.0.2.

## 0. Observed installed state (from commands I ran)

- **Installed app:** `/Applications/Subfleet.app` is `org.maxghenis.subfleet` 2.0.2, build 4, `LSUIElement=true`. Its binary sha256 is `0acdd754…415703`. `codesign -dv` reports an ad-hoc signature, no TeamIdentifier, and a thin arm64 Mach-O.
- **App not running:** `pgrep -fl MacOS/Subfleet` found no process at the time of inspection.
- **Daemon:** pid 1669, started Thu Sep 24 08:25:37 local, running as `…/current/venv/bin/python -E -P …/current/venv/bin/subfleetd --state-root /Users/maxghenis/.subfleet`. `~/.subfleet/daemon.sock` was recreated at 08:26 local. The plan's "connection refused" observation (plan:28) is therefore out of date.
- **Status snapshot:** `~/.subfleet/status.json` was fresh: `generated_at` 12:32:09Z against a clock of 12:32:48Z. It has keys `claude, codex, generated_at, jobs, offline`, 6 Codex homes with 0 dispatchable, and 17 Claude accounts. The file mode is 0600.
- **Guardians:** 4 live `subfleet.guardian` processes, each launched as `…/current/venv/bin/python` (the unresolved symlink path).
- **Backend pointer:** `~/.local/share/subfleet/current` → `releases/20260923T005311Z`. Its `release.json` records source `6420f5b`, wheel sha `0a0c834d…`, which matches `shasum` of the wheel. The venv was built by uv 0.12.13. Its `direct_url.json` points at the release's own wheel, so the install is non-editable. Its `python` symlinks to `~/.local/share/uv/python/cpython-3.14-macos-aarch64-none/bin/python3.14`, currently 3.14.4. That is a floating minor-version link, not a pinned patch release.
- **Codex shim link is broken (new finding):** `~/bin/codex -> ~/.local/share/subfleet/current/bin/codex` points at nothing.
  - Only releases `20260921T232722Z`, `20260921T234310Z`, `20260922T000530Z` and `20260922T011927Z` contain `bin/codex`. The last three releases, including current, do not.
  - `which -a codex` in this shell resolves to `~/.bun/bin/codex`, so the tracked shim (`bin/codex`, sha `b3c78a14…`) is bypassed.
  - This is the exact hazard `docs/private-guard-overlay.md:50-51` warns about ("provider CLI wrappers (including `current/bin/codex` …)").
- **Guard overlay:** `~/.subfleet/guard/never-rules-hook.sh` sha `a60d1c51…` and `TRUST` sha `0b9d688e…` match the pins in `docs/private-guard-overlay.md:14-15`.
- **Legacy CLI on disk:** `~/.local/bin/subfleet-local` still exists. It runs `subfleet.cli` from the dirty legacy checkout (`/Users/maxghenis/chief-of-staff-worktrees/subfleet-traycer-port/subfleet`). `~/bin/subfleet` runs `current/venv/bin/python -E -P -m subfleet.compat` with `SUBFLEET_HOOK_COMMAND` defaulting to `~/bin/subfleet hook`.

## 1. Menu app architecture

**Data source: `status.json` only.** There is no socket, no CLI and no database access.
- The header says so (`app/SubfleetApp.swift:1-3`).
- `statusFileURL` resolves `$SUBFLEET_HOME` (trimmed; handles `~` and `~/`), else `~/.subfleet`, then appends `status.json` (`SubfleetApp.swift:12-27`).
- `QuotaStore.load()` does `JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))`. On failure it clears `snap` and sets one of two error messages: missing file → "The daemon has not written a status snapshot yet.", anything else → "…could not be read." (`SubfleetApp.swift:364-378`).
- A 30 s repeating `Timer` reloads the file (`SubfleetApp.swift:346-354`). `ContentView.onAppear` also reloads (`SubfleetApp.swift:567`).
- "Reload snapshot" only re-reads the file and sets a "Snapshot reloaded at …" or "Reload failed at …" message (`SubfleetApp.swift:356-362`). Its help text says the daemon schedules the probes (`SubfleetApp.swift:601-603`). It never asks for a new observation. The plan's capability map requires keeping that distinction (plan:61).
- The acceptance contract's C-3.4 permits the menu app to open the database read-only, but this app never opens it at all.

**Decoded model.** `Snapshot{generated_at, offline?, jobs?, codex, claude}` (`SubfleetApp.swift:155-169`). `jobs` is optional so the app still works against a daemon that predates C-18.2 (`:158`).
- Codex lanes decode through `CodexHome`/`Windows`, where `five_hour ?? primary` and `seven_day ?? secondary` (`:39-81`).
- Claude lanes decode through `ClaudeAccount` with `probe`/`live` (`:83-121`).
- Jobs decode through `JobRow`/`JobBatch` (`:123-153`).
- Fleet fields the daemon emits but Swift ignores: `earliest_reset` and `reset_credits_remaining` (`status_json.py:198-200`, compared with the `Fleet` struct at `SubfleetApp.swift:72-76`), and `claude.lanes` (`status_json.py:201`).

**Staleness and display logic.**
- `isStale` is true when the snapshot is older than 600 s or more than 60 s in the future (`:163-168`). The daemon publishes every probe cycle, which defaults to a 60 s wait after the previous cycle finishes (acceptance-contract C-18.1 at `docs/acceptance-contract.md:191`).
- A percentage is shown only for `provider` or `stale-provider` evidence, and only if the value is finite and within 0-100 (`:239-243`).
- `laneDisplay` checks conditions in a fixed order: mismatch → auth failure → owner≠v2 → disabled → duplicate → unverified → offline → snapshot stale → usage stale → limited → not dispatchable → ok/ready/provider/admission-observed → Unknown (`:245-289`). An identity mismatch suppresses percentages (`:286-287`).
- Job display rules: a waiting job shows its reason, with `capacity` neutral and any other reason a warning; a failure shows `rc` (`:215-237`). A batch's jobs are grouped where the first member appears (`:190-206`). Recent jobs are cut to 8 *before* grouping (`:504-508`).

**Views** (compiled only when `SUBFLEET_MODEL_TEST` is not set; `:332`):
- `UsageBar` (`:434-446`) and `LaneRow` (`:448-480`)
- `JobRowView` (`:482-499`) and `JobsView` (`:501-534`)
- `ContentView`: header "AI quota" plus "as of"; a 520 pt-high `ScrollView` of offline/stale banners, JOBS, CODEX and CLAUDE (`:536-597`); a footer with Reload, a "Start at login" checkbox and a Quit power button (`:599-611`); width 430 (`:566`).
- The menu bar label is `bolt.fill` or `bolt.trianglebadge.exclamationmark` plus `dispatchable/total` (`:614-627`, `barLabel` `:391-396`, `hasProblem` `:398-404`).

**Scenes and activation.** The only scene is `MenuBarExtra { ContentView } .menuBarExtraStyle(.window)` (`:614-626`). There is no `Window` or `WindowGroup` scene, no `openWindow`, and no `NSApp.activate` (grep found none in `app/`). `Info.plist` sets `LSUIElement=true` (`app/Info.plist:13`), so the app has no Dock icon. The code never calls `setActivationPolicy`; only the test probe does (`tests/frontend/MenuViewProbe.swift:8`).

**"Open Subfleet" action: not present in v2.** The action exists only in the legacy cockpit source:
- `openCockpit()` calls `openWindow(id: "cockpit")` then `NSApp.activate(ignoringOtherApps: true)` (legacy `SubfleetApp.swift:481-484`).
- It is wired to two buttons. The header "Open" button appears only when a snapshot exists (legacy `:285-286, 309-317`). The footer "Open Cockpit" button is always present and bound to ⌘O (legacy `:445-450`).
- The window is declared as `Window("Subfleet", id: "cockpit")`, default size 1120×720 (legacy `:495-498`).
- Legacy `Info.plist` has `LSUIElement` true at HEAD and false in the working tree (`git diff`), with the same bundle id `org.maxghenis.subfleet` and version 0.1.0, build 1 (legacy `Info.plist:7-13`).

**Bundle identity.** `org.maxghenis.subfleet`, 2.0.2, build 4, executable `Subfleet`, minimum macOS 14.0 (`app/Info.plist:5-14`).

**Login item.** `SMAppService.mainApp.register()` / `unregister()`, with state re-read after the call and an error message on failure (`SubfleetApp.swift:341, 380-389`).

**Snapshot writer (daemon side).**
- `write_status` runs `build_status` → `mkdir(0o700)` → `atomic_publish` of sorted, NaN-free JSON (`subfleet/status_json.py:205-212`).
- `atomic_publish` writes a temp file, fsyncs it, renames it into place and fsyncs the directory (`subfleet/guardian.py:18-37`).
- It is called at the end of every probe cycle after `attach_batches` (`subfleet/timers.py:535-537`) and after a reset-credit action (`timers.py:206-207`). Offline is true only when every Codex probe returned `network-error` (`timers.py:518`).
- `build_status` skips lanes with `superseded_by` set.
  - Codex: `auth-dead` is aliased to `verdict=auth-revoked` with `outcome=auth-dead`; `primary`/`secondary`/`weekly` aliases are added to the window map.
  - Claude: `reset_at` is converted to epoch seconds.
  - Jobs: `live` is ordered running → waiting → queued, `recent` is the latest 8, plus `counts`. `wait_reason` and `next_check_at` appear only while a job is waiting (`status_json.py:77-202`).
- Batch labels come from `job.submitted` events for the displayed job ids only (`status_json.py:89-99`).

## 2. Frontend tests and CI

There are two native probes, both built with `xcrun swiftc` inside pytest fixtures. Both are skipped unless `sys.platform == "darwin"` and `xcrun` is on PATH (`tests/frontend/test_status_model.py:17-18`; the menu tests reuse that `pytestmark`, `test_menu_view.py:9`).

**Foundation-only model probe.**
- Compile command: `swiftc -D SUBFLEET_MODEL_TEST -parse-as-library app/SubfleetApp.swift tests/frontend/StatusModelProbe.swift` (`test_status_model.py:21-29`).
- Under that flag AppKit, ServiceManagement and SwiftUI are not imported (`SubfleetApp.swift:6-10`), and the whole store and view layer is excluded (`:332-629`).
- `StatusModelProbe` handles either `path <home> [SUBFLEET_HOME]` or a `<status.json> <epoch>` pair. It prints JSON projections of `codexDisplay`, `claudeDisplay`, `jobGroups`/`jobDisplay` and `isStale` (`StatusModelProbe.swift:1-40`).
- Fixtures are real `build_status` output from Python (`test_status_model.py:42-51`).
- 19 cases cover: live windows; no invented percentage for `unknown`, `admission-observed` or `local-backoff`; stale, snapshot-stale and offline; v1 ownership; identity-mismatch suppression; `SUBFLEET_HOME` resolution; empty fleet; lanes with no readings; clock boundaries (`11:50:00` fresh, `11:49:59` stale, `+61 s` stale, invalid stale); batch grouping and wait reasons; a missing `jobs` key (`test_status_model.py:54-193`).

**AppKit/SwiftUI view probe (no window).**
- Compile command: `swiftc -D SUBFLEET_VIEW_TEST -parse-as-library app/SubfleetApp.swift tests/frontend/MenuViewProbe.swift` (`test_menu_view.py:13-21`). `SUBFLEET_VIEW_TEST` removes only `@main SubfleetApp` (`SubfleetApp.swift:613-628`).
- The probe sets `.prohibited` activation, hosts `ContentView` in an `NSHostingController`, and measures `sizeThatFits` at zero and at 430×800. It also reads `JobsView.recentGroups` and exercises reload feedback, including an atomic file replacement and then a broken JSON file (`MenuViewProbe.swift:7-49`).
- Every case asserts `visible_windows == 0` (`test_menu_view.py:44,52,79,97`).
- 6 cases:
  - Populated minimum height between 580 and 700, width exactly 430, proposed height ≈ minimum (`:30-44`).
  - Missing snapshot: height between 80 and 350 (`:47-52`).
  - Exactly 8 recent jobs with a completed batch heading (`:55-79`).
  - Reload: acknowledgement, replacement load, then failure that clears the snapshot (`:82-97`).

The total is 25 frontend tests, counted by hand from the parametrizations. That matches "25 frontend tests" in `~/.subfleet/cutovers/app-2.0.2-20260921T235555Z/installation.json`.

**CI** (`.github/workflows/tests.yml`):
- Runs on `macos-15` with Python 3.12 and 3.14, uv 0.12.17 (`:17-31`).
- Steps: `uv sync --locked --group dev` (`:32`), `uv build` (`:33-34`), then `app/build.sh "$RUNNER_TEMP/subfleet-frontend"`, "Compile native menu bar app without launching" (`:35-36`).
- It then runs `uv run --no-sync pytest -q` with `SUBFLEET_LIVE=0` (`:37-40`). `testpaths=["tests"]` (`pyproject.toml:38-40`), so `tests/frontend` runs in CI.
- No test checks `Info.plist` or `build.sh` contents (grep found only the two frontend tests referring to app sources).

**`app/build.sh`:**
- Refuses non-Darwin systems (`:16-19`).
- Refuses output under `/Applications` or `~/Applications`, both before and after resolving the path (`:23-36`).
- Compiles **only `app/SubfleetApp.swift`** with `-O -parse-as-library` for `$(uname -m)-apple-macos14.0` (`:38-45`), producing a thin binary.
- Copies `Info.plist` and runs `plutil -lint` on it (`:46-47`), ad-hoc signs with `codesign --force --sign -` (`:48`), and atomically moves the bundle into `OUTPUT/Subfleet.app` (default `build/`, which is gitignored) (`:40-53`).
- It never installs or launches anything.

The sdist ships only `/subfleet`, the README, `pyproject.toml` and `uv.lock` (`pyproject.toml:32-33`), so app source never reaches backend release artifacts.

## 3. Release procedure as practised

No install script is tracked in the repo. The working procedure lives in per-cutover scripts under `~/.subfleet/cutovers/*/install.py` (13 of them) plus `release.json` records.

**Backend release directory.** `~/.local/share/subfleet/releases/<UTC ts>/` contains:
- the wheel and sdist from `uv build`;
- a uv-made `venv/` with the release's own wheel installed (observed: `pyvenv.cfg`, `INSTALLER=uv`, `direct_url.json`);
- `release.json` (fields such as `source_commit`, `wheel_sha256`, `schema_version` 5, `ci`, `local_tests`, `authorization`, `backup`, `previous_release`, `installed_at`);
- optionally a copied `Subfleet.app` (the `frontend_note` field says "app copied from the previous release; binary byte-identical");
- optionally `bin/codex`.

The exact commands used to create the venv are **UNVERIFIED**; only the results above were observed.

**Selection by the `current` symlink** (`route-isolation-20260923T005337Z/install.py`):
1. Run the script with the **new** release's `python -E -P`, and assert that `subfleet.__file__` resolves inside the new release (`:4, 25`).
2. Assert `source_commit` and that the plist `ProgramArguments` still equal `current/venv/bin/python -E -P current/venv/bin/subfleetd --state-root ~/.subfleet` with `ProcessType=Standard` (`:24, 32-34`).
3. Check free disk (`:28`). Back up the plist, `install.py`, `policy.json` and `lanes.json` into `~/.subfleet/cutovers/<name>-<ts>/` (`:35-41`).
4. `launchctl bootout gui/$UID/com.subfleet.daemon` (`:48`), then acquire `daemon.lock` with a 20 s timeout (`:50-59`).
5. `VACUUM INTO state-before.sqlite3` (`:60-61`). Record the active attempts (`reserved/starting/running/finalizing/quarantined`, with `guardian_pid, pgid, boot_id, proc_start`) and the leases into `before.json`. Run the integrity and foreign-key checks and write a `cutover.*` event (`:62-72`).
6. Flip the pointer atomically: create a staging symlink, then `replace` it over `current` (`:73-77`). Update `release.json` (`:78-79`).
7. In `finally`: wait until launchd has deregistered the old job, then `launchctl bootstrap` the **unchanged** plist (`:84-99`).

The scripts state "Provider guardians are never touched" (`:6`).

**Why old release directories must be retained.**
- Guardians are launched as `[sys.executable, "-m", "subfleet.guardian", …]` with `PYTHONPATH` set to `Path(__file__).resolve().parent.parent`, i.e. the **resolved** release's site-packages (`subfleet/daemon.py:2880-2894`; the same pattern at `:456-467` and `:2089-2102`).
- The live guardians use the unresolved `current/venv/bin/python` path (ps output above).
- Codex launches embed the hook path in `-c hooks=…` (`subfleet/adapters/codex.py:439`; `subfleet/guard/preflight.py:161-168`).
- Hence `docs/private-guard-overlay.md:59-64`: "Retain all old release directories used by active launches."

**Guardian adoption.**
- On startup the daemon runs `_recover_then_start_timers` → `_recover_probes`, action recovery, merge recovery, pin canonicalisation, capacity-wait recovery, then timers (`daemon.py:1802-1814`).
- Running attempts are re-adopted by `procs.liveness(guardian_pid, boot_id, proc_start)`, "solely by receipt identity, not parentage" (`daemon.py:2986-2991`). An `unknown` result decides nothing (`:2992-2995`).
- The recorded verification (`native-completion-20260921T233141Z/verification.json`) lists per-attempt `before`/`after` states with `identity_preserved: true`, plus daemon `boot_id/pid/proc_start`, a ping, a snapshot, and doctor failures/unknowns.

**App bundle install.**
- No retained `install.py` touches `/Applications` (grep found none). The actual install commands are **UNVERIFIED**.
- The record for 2.0.2 (`cutovers/app-2.0.2-20260921T235555Z/installation.json`) lists `source_commit 795bfdb`, `merge_commit d8677f2`, PR #20, version and build, `binary_sha256 0acdd754…`, `previous_binary_sha256 7255f9fd…` and `replaced_pids [33758]`. The previous 2.0.1 bundle is kept beside it as `Subfleet.app`; I observed its sha as `7255f9fd`.
- Signing is ad-hoc only (`build.sh:48`; confirmed by `codesign -dv`).

**`frontend-releases/`.** Two entries, `20260920T114345Z` (2.0.1, sha `7d242d26`, PR #4) and `20260920T201433Z` (2.0.1, sha `7255f9fd`, PR #12). Each holds `Subfleet.app` plus `release.json` (`source_commit`, `version`, `backup`, `binary_sha256`, `merged_commit`, `pull_request`). **The 2.0.2 install created no `frontend-releases` entry.** It was recorded instead in `cutovers/app-2.0.2-…` and in the backend `release.json` fields `frontend_version`, `frontend_sha256`, `frontend_build`, `frontend_source_commit` and `frontend_installation` (e.g. `releases/20260921T234310Z/release.json`).

**Guard overlay staging.**
- Command: `uv run python -m tools.stage_guard_overlay --source private/guard --state-root "$HOME/.subfleet"` (`docs/private-guard-overlay.md:29`).
- The helper validates the source pair with `load_guard` and refuses a symlinked destination. It is idempotent only when the existing overlay is byte-identical, and otherwise raises "refusing to replace a different overlay". It stages into a mkdtemp directory with modes 755/600, re-validates, then renames into place (`tools/stage_guard_overlay.py:19-45`), exiting 7 on refusal (`:53-56`).
- Afterwards: `cmp` both files and run offline `check_guard_preflight` (`docs/private-guard-overlay.md:40-47`).
- The overlay install additionally asserted `load_guard(root)[0] == root/'guard/never-rules-hook.sh'` and `manifest['guard_runtime_preflight']['ok']` (`cutovers/guard-overlay-20260922T000800Z/install.py:17-19`).

**launchd environment** (`~/Library/LaunchAgents/com.subfleet.daemon.plist`, mode 0600; no secret-looking values):
- Label `com.subfleet.daemon`; KeepAlive and RunAtLoad true; ProcessType Standard.
- ProgramArguments as above; WorkingDirectory `~/.subfleet`; stdout and stderr go to `~/.subfleet/daemon.log`.
- Environment is **only** `SUBFLEET_HOME=/Users/maxghenis/.subfleet` plus a long `PATH`. That `PATH` includes a transient `~/.codex/tmp/arg0/codex-arg0XmU1XY`, which no longer exists, and lists `~/.bun/bin` before `~/bin`.
- No `SUBFLEET_GUARD_TRUST`, `SUBFLEET_CODEX_GUARD_CACHE` or `CODEX_GUARD_PREFLIGHT_TIMEOUT` overrides are present.
- `subfleet daemon install` would regenerate this plist with the *caller's* `PATH` and `sys.executable` (`subfleet/cli.py:1690-1707, 1892-1906`) and then `launchctl unload`/`load -w` it (`cli.py:1964-1972`), which is both a restart and an environment rewrite.

**Rollback.**
- For a release: restore the previous pointer through the normal procedure, and keep both the old releases and the staged overlay (`docs/private-guard-overlay.md:76-81`). `previous_release` in `release.json` names the target. A `failure.json` is written if an install fails (`install.py:81-83`).
- For the store: v2-to-v2 upgrades are "Stop admission; drain or re-adopt; VACUUM INTO …; migrations; integrity_check; resume" (`docs/migration.md:84-86`).
- For the app: the bundle backup lives in `cutovers/app-*/Subfleet.app`, and old bundles live in `frontend-releases/*`.
- The full v2→v1 rollback order is in `docs/migration.md:80-82`. Lines 73-74 of that document are stale: they say compat delegates to the v1 binary, which the 2026-09-21 native-completion report contradicts.

## 4. Constraints Stage 5 must honour (with citations)

1. **Separate build from install.**
   - `app/build.sh` refuses `/Applications` (`:23-36`); keep it that way.
   - The legacy `build.sh` writes straight into `/Applications/Subfleet.app` (legacy `app/build.sh:5-16`). The plan forbids running it (plan:70).
2. **Compile inputs need updating.**
   - `build.sh:44-45` and both test fixtures (`test_status_model.py:25-27`, `test_menu_view.py:17-19`) compile *only* `app/SubfleetApp.swift`.
   - Porting `CockpitView`, `CockpitStore` or `SubfleetCLI.swift` requires changing all three compile lines. Otherwise CI (`tests.yml:35-38`) will fail or silently leave the new code untested.
3. **Model code stays Foundation-only under `SUBFLEET_MODEL_TEST`** (`SubfleetApp.swift:6-10, 332`). The whole `@main` scene set must stay behind `#if !SUBFLEET_VIEW_TEST` (`:613`).
4. **No visible windows in the probe.** The probe runs with `.prohibited` activation and asserts `visible_windows == 0` (`MenuViewProbe.swift:8, 47`; `test_menu_view.py:44,52,79,97`). A new main `Window` scene must not open automatically in test hosts. The menu panel must also keep width 430 and height between 580 and 700 (`test_menu_view.py:41-43, 91`).
5. **Activation policy change.** "Main window, Dock/menu actions, close/reopen, menu-bar Open action" (plan:124) cannot be met while `LSUIElement=true` (`Info.plist:13`) and there is no `Window` scene (`SubfleetApp.swift:614-626`). Options are the legacy `openWindow` plus `NSApp.activate` pattern (legacy `:481-484, 495-498`) or runtime activation-policy switching, which is **UNVERIFIED** as a design choice. Keep the Open action in a footer that is always present (legacy `:445-450`), not only in the header that requires a snapshot (legacy `:285-286`).
6. **Bundle identity and login item.** Production is `org.maxghenis.subfleet`, the same id the legacy cockpit used (`app/Info.plist:7`; legacy `Info.plist:7`). "Start at login" is `SMAppService.mainApp` (`SubfleetApp.swift:341, 380-389`). Development installs need a distinct bundle id and state root (plan:90). Bump `CFBundleVersion`/`CFBundleShortVersionString` beyond 4/2.0.2, and do not derive the version from the Python package version 2.0.0a0 (`pyproject.toml:4`; `docs/private-guard-overlay.md:65-66`).
7. **Keep the quota/status contract.**
   - `status.json` via `SUBFLEET_HOME` (`SubfleetApp.swift:12-27`) with a tolerant `jobs?` (`:158-159`).
   - The 600 s / −60 s staleness rule (`:163-168`) and the provider-only percentages (`:239-243`).
   - Reload must never pretend to be a probe (`:601-603`; plan:61).
   - Any new endpoint must be explicit, with no fallback to `~/.local/bin/subfleet-local` or `~/chief-of-staff/state/subfleet` (plan:45). Those fallbacks are hard-coded in legacy `SubfleetCLI.swift:46,53,255` and `SubfleetApp.swift:15-16`.
8. **Record provenance.** Store the app's source commit, binary sha256, build number and the previous bundle's sha, as `installation.json` does (`cutovers/app-2.0.2-…/installation.json`). Keep the previous bundle as rollback (plan:142); the current 2.0.2 bundle is sha `0acdd754`. Consider restoring a `frontend-releases/<ts>/` entry, since 2.0.2 lacks one.
9. **Backend install mechanics.** Use the established `install.py` pattern:
   - new release's python, plist equality assert, backup directory, bootout, `daemon.lock`, `VACUUM INTO`, `before.json` attempt snapshot, integrity checks, atomic symlink `replace`, bootstrap of the **unchanged** plist (`route-isolation…/install.py:24-99`).
   - Do not run `subfleet daemon install`: it rewrites `PATH`/argv from the caller's shell and unloads the daemon (`cli.py:1892-1906, 1964-1966`).
   - Preserve `SUBFLEET_HOME`, `PATH` and `HOME` in launchd. Any guard override must live in the plist, not the shell (`docs/private-guard-overlay.md:49-57`).
10. **Retain every old release and hook path.** Guardians import from the resolved release they were launched under (`daemon.py:2880-2881`), and Codex argv embeds the hook path (`preflight.py:161-168`, `codex.py:439`). Four guardians are live right now. Keep `~/.subfleet/guard` intact (`docs/private-guard-overlay.md:59-64, 76-81`; plan:140).
11. **Include the provider wrapper.** A new release must contain `bin/codex`, or `~/bin/codex` must be repointed. It is dangling now (section 0). The shim is tracked at `bin/codex` (sha `b3c78a14`) and requires `current/venv/bin/{python,subfleet}` (`bin/codex:57-61`).
12. **Verify guardian adoption after any daemon reload.** Compare `before.json` attempts with post-restart state using `guardian_pid`/`boot_id`/`proc_start` identity (`daemon.py:2986-2991`), as `native-completion…/verification.json` recorded (`identity_preserved`). Coordinate the restart with running jobs (plan:33, 142).
13. **App rollback must not roll back the store.** No old database may be copied over active work, and pending turns may not be replayed (plan:138). Schema compatibility is currently `schema_version` 5 across releases (`release.json`).
14. **Trust and signing.** The current trust level is ad-hoc signing with no TeamIdentifier (`build.sh:48`; `codesign -dv`), plus the guard pins and `codex-cli 0.153.3`, which appears in the `20260922T000530Z` release.json `guard_runtime_preflight`. Stage 5 must record exact source/build hashes, installed version and CI run (plan:134).
15. **GUI QA constraints.** No CDP, no native Playwright/WebKit windows, and only task-owned sessions may be closed (plan:136; `/Users/maxghenis/AGENTS.md`). The existing probes already avoid windows.

## UNVERIFIED
- The exact commands that built each release venv, copied `Subfleet.app` into backend releases, placed `bin/codex` in four releases, and installed the app into `/Applications`. Only the resulting artefacts and records were observed.
- The Swift toolchain version on the `macos-15` runner.
- Whether the login item is currently registered (`SMAppService` status was not queried) and why the menu app is not running.
- Whether interactive shells resolve `codex` to the shim or to bun. Only this agent's shell was checked, and it resolved to `~/.bun/bin/codex`.