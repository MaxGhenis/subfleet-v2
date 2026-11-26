# Issue #322 desktop launch baseline

Started at `2026-11-26T16:20:28Z`. The required HEAD check printed `a4d21501e` and the checkout was detached. Git denied creation of the linked worktree's `index.lock` when staging the first step. The changes are in the assigned worktree for Subfleet salvage. The commit range `a4d21501e..HEAD` is empty.

## Changes and cause

- `app/Sources/UIModel.swift` now snapshots the cursor, focus, and known message IDs for one outbox read at a time, then folds the response into live main-actor state. The drain has no page limit. It completes on `.applied(0)` and retries from the reached cursor after 5 s on a failed or superseded read. A thrown error appears in the problem line. Feed and event polling, the held click, and the launch conversation request follow the completed baseline. The two probe seams are compiled only under `SUBFLEET_UI_MODEL_TEST`.
- `app/Sources/ConversationStore.swift` changes one doc line to point to the app's page-by-page baseline. The existing probe helper still serves the core probes.
- `tests/frontend/ConnectBaselineProbe.swift` runs the real `connect()` and feed loop with scripted daemon answers. `tests/frontend/test_core_connect_baseline.py` provides real service response fixtures and covers state writes, error/retry, the page cap, receipt-time visibility, and seam confinement.
- `tests/frontend/test_core_notifications.py` checks approval reveal on a default delegate response. Its service fixture and the one in `tests/frontend/test_core_store.py` now own their temporary roots and test cleanup when the inner test passes and when it raises.
- `docs/acceptance-contract.md` updates C-29.9 and the version 3 changelog. `docs/desktop/ledger.json` appends to P-13's notes. The ledger parsed and its diff was one line. The changes are confined to desktop app code, tests, and documentation.

At `a4d21501e`, `UIModel.swift:178–183` drained a copy on the outbox queue and replaced the live state after carrying over focus alone. That assignment overwrote sidebar changes and any other state writes that arrived while the main actor awaited the drain. `ConversationStore.swift:1467–1474` returned after at most 64 pages, and `connect()` swallowed a thrown read at line 179. The unconditional mark at line 183 let the remaining backlog through as news under the notification gate at `ConversationStore.swift:869`. Its store-side empty-page mark at line 875 was the intended feed-end signal.

## Invariants and fail-first evidence

| Invariant | Tests and observations |
| --- | --- |
| Kept writes | **(a)** failed at the starting revision with search `""` and grouping `recency`. The original and fixed runs both reached cursor 4 from reads `[0, 4]`. The fixed run retained `probe` and `workspace`. |
| Baseline at feed end | **(b)** originally marked the baseline at cursor 3 with reads `[0, 3]`, problem `null`, and replay `[["completed", "cv-1795710784953-f5cc2c13aac3"]]`. The fixed run read `[0, 3, 3, 4]`, completed at 4, reported `watch read failed`, and replayed silently. **(c)** also checks silent replay and the end marker. |
| No page cap | **(c)** originally read cursors 0–63, marked the baseline at 64, and replayed `[["completed", "cv-1795710786977-ef835a936312"]]`. The fixed run read 0–65, including the final empty page, ended at 65, and replayed silently. |
| Fresh screen reading | **(d) on/off** hold the first feed response until the override flips after `connect()`. Both reach the second long poll. A conversation newly on screen posts nothing, and one newly off screen posts its completion. |
| Seams by construction | **(e)** failed at the starting revision. The use and declaration of `onScreenOverride` on lines 622 and 630 were outside the UI-model-only branch, and `probePosted` was absent. The static check passed after the change. The separate `SUBFLEET_VIEW_TEST` compile and all six view tests passed during recovery. |
| No leaked roots | **(g)** and **(h)** originally each found two surviving roots after the inner session's one pass and one intentional failure. Both passed after the change. |

**(f)** passed at the starting revision and after the change. The default approval response asserted focus `cv-one`, reveal `cv-one`, and no calls. I put that subcase first after the fail-first run so the delegate mutant would reach the new reveal assertion before the existing Allow once checks.

Guarded cleanup removed `/tmp/sf-nt-0p2l89ox`, `/tmp/sf-nt-w_sjm71k`, `/tmp/sf-st-139sto1n`, and `/tmp/sf-st-kfqvl744`. The teardown failures identified all four roots.

## Probe

The probe accepts one JSON file and uses the requested key names. `root` sets rooted model paths, `focused` sets initial focus or null, and `on_screen` sets the initial override. `answers` holds fixed answers for capabilities, model and conversation lists, conversation and message status, and conversation open. `drain` maps cursor strings to lists of zero-wait replies whose last answer repeats. An `error` reply throws `DaemonClientError.unavailable`. `during_first_drain` optionally applies `search` and `grouping` on the main actor before the first reply. `feed` supplies the semaphore-gated first long poll, and `flip_on_screen` updates the override without publishing before release. `after_connect` replays a page against a copy of the connected state. Other long polls and conversation event requests stall for the life of the probe. The sorted JSON output contains `search`, `grouping`, `watch_baselined`, `watch_cursor`, `drain_reads`, `feed_reads`, `backlog`, `posted`, `problem`, and `ops`. Each launch sets `SUBFLEET_HOME` below its test's temporary path.

## Test commands and results

Each run used this command with the arguments shown below.

```sh
uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest -n 4 --dist loadfile --timeout=540 --basetemp "$PWD/build/cbl-build-r1/pytest-<run>" -q <arguments>
```

- `fail-first` and `after-change` used `tests/frontend/test_core_connect_baseline.py tests/frontend/test_core_notifications.py tests/frontend/test_core_store.py -k 'test_core_connect_baseline or the_delegate_hands_each_response_to_the_model or the_notification_harness_leaves_no_root_after_teardown or the_store_harness_leaves_no_root_after_teardown'`.
- `full` used `tests/frontend/test_core_connect_baseline.py tests/frontend/test_core_notifications.py tests/frontend/test_core_store.py tests/frontend/test_app_cutover_start.py tests/frontend/test_pr124_fixes_ui.py tests/frontend/test_conversation_view.py tests/unit/test_desktop_ledger.py tests/unit/test_invariants_index.py tests/unit/test_protocol_registry.py tests/unit/test_test_module_names.py`.
- `view-retry` used only `tests/frontend/test_conversation_view.py`, with `SUBFLEET_SWIFT_NESTED_SANDBOX=off`.
- `M1`–`M4` used `tests/frontend/test_core_connect_baseline.py` and the selectors below. `M5` used `tests/frontend/test_core_notifications.py` and its own selector.

| Run | Start UTC | `df -H /` available | `uptime` local time and load averages | Pass | Fail | Error | Skip | Seconds | Basetemp KiB |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `fail-first` | 16:27:51Z | 75G | 17:27 / 42.07 47.10 54.50 | 1 | 6 | 0 | 0 | 319.00 | 11020 |
| `after-change` | 16:37:20Z | 75G | 17:37 / 35.04 53.05 55.52 | 9 | 0 | 0 | 0 | 210.73 | 11024 |
| `full` | 16:41:36Z | 75G | 17:41 / 64.54 48.88 52.07 | 132 | 0 | 6 | 0 | 859.18 | 50428 |
| `M1` | 17:12:42Z | 73G | 18:12 / 103.98 74.39 60.44 | 0 | 1 | 0 | 0 | 313.28 | 5428 |
| `M2` | 17:19:39Z | 72G | 18:19 / 40.91 66.54 64.12 | 0 | 1 | 0 | 0 | 368.73 | 5432 |
| `M3` | 17:28:02Z | 73G | 18:28 / 62.64 61.46 62.55 | 0 | 1 | 0 | 0 | 340.94 | 5452 |
| `M3b` | 17:34:22Z | 73G | 18:34 / 60.04 53.57 57.82 | 0 | 1 | 0 | 0 | 494.92 | 5432 |
| `M4` | 17:44:16Z | 72G | 18:44 / 60.70 54.59 56.35 | 0 | 2 | 0 | 0 | 281.86 | 5440 |
| `M5` | 17:49:42Z | 72G | 18:49 / 43.76 44.62 50.64 | 0 | 1 | 0 | 0 | 181.25 | 5472 |
| `view-retry` | 18:00:18Z | 72G | 19:00 / 27.38 33.25 40.86 | 6 | 0 | 0 | 0 | 15.99 | 13084 |

Swift compile times by probe. Exact-source cache hits reused the previous binaries.

- `fail-first` — `subfleet-connect-baseline0/probe` 262.49s, `subfleet-notification-model0/probe` 259.61s.
- `after-change` — `subfleet-notification-model0/probe` 169.28s, `subfleet-connect-baseline0/probe` 171.19s.
- `full` — `subfleet-core0/probe` 199.19s, `subfleet-core0/probe` 199.83s, `review-fixes0/probe` 204.42s, `subfleet-cutover-model0/probe` 202.75s, `subfleet-conversation-view0/probe` 540.31s (timed out).
- First view cache attempt — `subfleet-conversation-view0/probe` 846.59s (SwiftUI macro errors).
- Minimal SwiftUI macro check — 31.39s (nested sandbox denied), then 33.94s with the nested sandbox off (passed).
- `M1` — `subfleet-connect-baseline0/probe` 277.79s.
- `M2` — `subfleet-connect-baseline0/probe` 334.88s.
- `M3` — `subfleet-connect-baseline0/probe` 301.35s.
- `M3b` — `subfleet-connect-baseline0/probe` 448.81s.
- `M4` — `subfleet-connect-baseline0/probe` 263.98s.
- `M5` — `subfleet-notification-model0/probe` 158.18s.
- View cache precompile with the nested sandbox off — `subfleet-conversation-view0/probe` 341.30s.

## Mutants

| Mutant | Selector passed to `-k` | Assertion that failed |
| --- | --- | --- |
| M1, drain into a copy | `keeps_what_reaches_the_state` | **(a)** lost the search and grouping edits. |
| M2, ignore drain failure | `failed_baseline_read` | **(b)** returned at cursor 3 and notified when the completion was replayed. |
| M3, report success at 64 pages | `longer_than_64_pages` | **(c)** stopped at cursor 64 and notified when page 65 was replayed. |
| M3b, omit error reporting | `failed_baseline_read` | **(b)** saw a null problem line. |
| M4, use the published screen reading | `feed_reads_the_screen` | Both **(d)** cases posted the wrong result after the override flipped. |
| M5, forward only the conversation ID | `the_delegate_hands_each_response_to_the_model` | **(f)** focused `cv-one` and returned a null reveal. |

Each mutant recompiled its probe. The blocked commit required restoring the app model from its byte-for-byte final snapshot. Git also denied the delegate's `git restore --source=HEAD` on the index lock. I restored the saved delegate file and verified `git diff --quiet -- app/Sources/UINotifications.swift`. After each mutant, the tracked diff matched the pre-mutation diff byte for byte.

## Limits and run deviations

- The installed app remains the place to verify AppKit's and CoreGraphics' own answers, including `noteOnScreen`'s reads, its observers, and `MainWindowReader`.
- A watch read that keeps failing on a daemon that answers `capabilities`, such as an undecodable page, displays the read error and retries every 5 s. The feed loop, focused conversation's launch events loop, and held click wait for the baseline to reach the feed's end. At `a4d21501e` the launch events loop still started in that case.
- The overlap is with PR #71 in the store test fixture, #63 in `connect()` and `fold(watch:)`, and #189 in `fold(watch:)`.
- The worktree lacked a functional venv. The configured Darwin temp parent denied creation of the job directory and initialization of its pre-existing uv cache. The fallback used `build/cbl-build-r1` for scratch and a local uv cache. The offline sync missed required packages, so I ran `UV_PYTHON=3.14 uv sync --group dev --locked` and resolved xdist once online. The pytest runs used the requested offline command.
- A job-local pytest plugin logged Swift compile times, set a test-local `SUBFLEET_HOME` on legacy probe invocations that lacked one, and disabled pytest's unrelated shared `/tmp/sf-failed` pruning. Swift used its default module cache. I set `SUBFLEET_SWIFT_NESTED_SANDBOX=off` only for view recovery.
- The full run's view build hit pytest's 540 s setup timeout. A longer direct compile returned SwiftUI macro errors, and a minimal `@State` typecheck confirmed `sandbox-exec: sandbox_apply: Operation not permitted`. The minimal check passed with `-Xfrontend -disable-sandbox`. With the documented flag set, I warmed the exact view-probe cache by calling `compile_probe` with `SUBFLEET_VIEW_TEST` and a 1,200 s compiler timeout, then reran only the six view tests with the original 540 s pytest timeout.

## Workspace and cleanup

The probe cache measured 83,944 KiB and the fallback job folder measured 25,108 KiB before removal. The largest measured basetemp was 50,428 KiB in the full run. Every basetemp was measured and removed before the next run. I also removed the worktree-local venv, pytest cache, and Python bytecode caches created for the job.

The only job-owned scratch folder outside the worktree was the 12 KiB `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261126-171639-cbl-build-r1-a1-a_yzp85m/uv-cache`, created by the initial import check. Guarded cleanup removed it. The pre-existing Darwin-parent uv cache was left in place. The temporary harnesses removed their own roots apart from the four fail-first roots removed above. No job-created files were left outside the worktree.

`git diff --check` passed and the exact known-message-ID line used by the existing mutation test occurred once. The report and the other eight requested files are present in the assigned worktree.
