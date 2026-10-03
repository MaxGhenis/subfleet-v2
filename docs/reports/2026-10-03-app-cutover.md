# App cutover verification — 2026-10-03

Completed the six-item original brief in the assigned workspace, continuing the unverified `53c6d4c5` draft through `fde19635`. The routing policy and its tests are unchanged from `fde19635`. All commands were awaited; no agents, detached processes, app installation, live daemon changes or live app-file writes were used. The live outbox was inspected read-only: entries 16–17 are refused creates and entry 18 retains the queued first message.

## Changes and regression evidence

Paths below are relative to the repository. Baseline means an isolated archive of `7bfda766`, with the current regression tests overlaid and production source left unchanged.

| Brief item | Implementation | Regression tests and baseline result |
| --- | --- | --- |
| 1. Safe folder and daemon admission | `app/Sources/UIModel.swift:501`, `app/Sources/NewConversationDraft.swift:41`, `app/Sources/NewConversationWorkspace.swift:32`; shared daemon checks at `subfleet/conversations/service.py:497` and `:513`. Recent folders are checked in order; otherwise a dated six-hex scratch name is reserved and its directory created with mode 0700 at Start. A refusal disables Start, offers scratch and removes the refused saved folder. | `tests/frontend/test_app_cutover_start.py:62`–`:98` exercises recent selection, scratch reservation/creation and refusal/recovery. These four tests cannot compile against baseline, which excludes UIModel in the new probe mode and lacks the admission/draft APIs. `tests/unit/test_app_cutover_daemon.py:46`–`:144` covers the public check/create agreement, protection, branches, worktrees and APFS case aliases; all nine cases fail on baseline, which has no `workspace.check`. |
| 2. Predictable provider and model | `app/Sources/StatusModel.swift:135`, `app/Sources/UIModel.swift:471`, `app/Sources/NewConversationDraft.swift:46`; daemon defaults at `subfleet/conversations/service.py:274`. Auto chooses Claude when any Claude lane is ready or readiness is unknown. Per-provider model memory includes sends in existing conversations. Daemon IDs resolve to observed provider values such as `opus[1m]`. The Start line shows provider and model. | `tests/frontend/test_status_model.py:215` tests readiness; its two changed Auto assertions fail on baseline. Model tests at `tests/frontend/test_app_cutover_start.py:115`–`:130` have four baseline compile errors from the missing APIs. `tests/unit/test_app_cutover_daemon.py:195` fails on baseline because defaults are absent. |
| 3. Recoverable failed creates | `app/Sources/Outbox.swift:215`, `:237`, `:252`, `:274`; `app/Sources/UIModel.swift:752`; `app/Sources/UIWindow.swift:47` and `:174`; `app/Sources/UIFailedConversationDraft.swift:5`. Startup exposes both old failures, sidebar rows show reason/message, footer selects a row, and retry/copy/discard are available. Retrying preserves request/message IDs, attachments and original title/worktree/main settings. | Migration/footer and discard tests at `tests/frontend/test_app_cutover_start.py:137` and `:154`, plus real-service retry at `:163`, have three baseline compile errors because recovery APIs are absent. The existing old-journal test at `tests/frontend/test_core_steer.py:884` now checks the additive migration marker as well as preserved entries/steers. |
| 4. Native last activity | `subfleet/conversations/catalog.py:533`, `subfleet/conversations/service.py:362`; `app/Sources/ConversationStore.swift:52` and `:749`. The cached catalog is read without transcript scanning or pagination before sorting/limiting; the later row/transcript time drives sidebar groups. Opening an older daemon reply preserves newer cached activity. Contract C-30.1 and the desktop op table were updated. Huge, NaN, boolean and malformed cached timestamps are ignored. | `tests/frontend/test_core_app_cutover_reading.py:27` and `:40` both fail behaviorally on baseline. Three daemon activity cases at `tests/unit/test_app_cutover_daemon.py:155`–`:182` fail there too. The no-scan test traps catalog rebuilds and tree walks, and checks ordering before a limit of one. |
| 5. Visible blockers and actual choices | `app/Sources/ConversationStore.swift:38`, `app/Sources/UIWindow.swift:243` and `:299`, `app/Sources/UIConversationRecovery.swift:6`. Both blockers show “Needs you”; unfinished turns offer Continue/Leave, and unknown delivery offers the two delivery decisions. | Two model cases at `tests/frontend/test_core_app_cutover_reading.py:56` fail behaviorally on baseline. Two real SwiftUI cases at `tests/frontend/test_conversation_view.py:65` exercise actual button actions and sidebar mark geometry; baseline cannot compile the added view APIs. |
| 6. Compact task notices | `app/Sources/TaskNotification.swift:18`, `app/Sources/Timeline.swift:960`, `app/Sources/UIConversationRecovery.swift:25`. Only one whole Claude transcript user text block becomes a system notice with decoded summary, status and exit code. Mixed text, partial/multiple blocks, assistant messages and Codex remain messages. This changes presentation only. | Three positive cases at `tests/frontend/test_core_app_cutover_reading.py:84` and `:95` fail behaviorally on baseline; all six defensive preservation cases at `:109` pass there. The real compact view test at `tests/frontend/test_conversation_view.py:69` has a baseline compile error. |

Baseline totals: **13 daemon failures**; **9 behavioral frontend failures and 26 passes** (seven reading failures, two Auto failures, six preservation passes and twenty unchanged status passes); **14 additive frontend setup errors**. These arise from the absent UIModel probe mode/initializer and missing workspace/recovery/view Swift APIs; they are not failed behavioral assertions. The corrected old-journal regression is a compatibility check for this implementation, rather than a newly reproduced baseline bug.

The differential Hypothesis test calls both public operations against a real store for **100 generated examples plus five explicit examples**. It covers home, state-root ancestors, state root, `.claude`, `.codex`, nested provider homes, ordinary project folders, symlinks, case variants, both providers and all four permissions. The explicit case-insensitive filesystem test passed on this machine. **Read-only home is allowed** by both current daemon create and check. Writable home remains refused.

`models.list {provider: "claude"}` publishes IDs **`claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001`**, with **`default_models.claude = "claude-opus-5-5"`**. Entries retain short name, observed value(s), efforts, fast/image support and catalog timestamps. The existing Codex routing aliases and policy were preserved.

## Verification

Final changed-tree total across the required suites: **458 passed, two sandbox failures, one sandbox skip**. Baseline and deliberate mutation failures are separate evidence below, not part of this total.

- New daemon regression: **13 passed**, 115.43 seconds, including the huge-integer edge case, differential/property and case-insensitive coverage.
- Conversation service: **91 passed, one sandbox failure**, 102.09 seconds.
- Catalog, lifecycle and fake conversation pools: **35 passed, one sandbox failure**, 28.38 seconds.
- Frontend, including all **nine app-protocol cases**: **319 passed, one skipped**, in **16 foreground slices**, each **39.50–363.19 seconds** (1604.79 seconds across all slices). This includes all new frontend cases, the corrected old-journal test and both existing steer Hypothesis properties. The skip is the native peer/process check requiring sandbox-unavailable `ps`/sysctl visibility.
- Initial full frontend attempt: **318 passed, one failed, one skipped**, 1919.81 seconds. The failure was the directly affected old-journal exact-key expectation, fixed to include `draftRecoveryVersion`. This oversized attempt did not meet the slice requirement; final reruns use automatically collected small slices with content-cached probes. Earlier compilation-heavy focused attempts also exceeded ten minutes (626 and 845 seconds), before switching to the resumable compiler route. The refreshed combined baseline typed-probe attempt also exceeded the limit (948.30 seconds); the three repeated probe slices finished in 209.86, 231.68 and 295.67 seconds, with the same 10 + 1 + 3 setup errors.
- The service sandbox failure is unavailable macOS boot identity; the lifecycle failure is `/bin/ps` denied. Both exact tests also fail unchanged on `7bfda766` (**two failures**, 65.50 seconds in the final refresh). Neither unrelated test was edited or bypassed.
- **`app/build.sh` succeeded**, approximately 356 seconds, producing `build/app-cutover-evidence/product/Subfleet.app`. `codesign --verify --deep --strict` passed and linked libraries resolve to system frameworks/Swift libraries. The app was neither installed nor launched.

All four mutations were caught by test assertions, with no compilation or fixture errors:

| Mutation | Result | Slice wall time |
| --- | --- | --- |
| Bypass shared writable-folder protection | Both accept-edits and bypass home-refusal assertions fail | 63.89 s |
| Ignore indexed native transcript mtime | The old native conversation no longer wins the limited list | 56.10 s |
| Restore Auto's greater-ready-lane-count choice | Four ready Codex/one ready Claude incorrectly resolves to Codex | 260.11 s |
| Disable whole-block task-notice parsing | The task remains a user history message | 306.54 s |

The verification-only xcrun wrapper passes `-disable-sandbox -j 4` and routes frontend invocations through `tools/foreground_swift_frontend.py`. This avoids the compiler macro plugin's nested sandbox failure while retaining the outer workspace sandbox. Each actual compiler child is awaited and recorded by exact PID; completed objects and probes are cached from source contents and flags. Existing Swift concurrency warnings and a wrapper-derived unused library-search-path warning remain; neither prevents building. Test runners, sources and caches are inside this workspace; module/download caches are in permitted temporary directories.

Reproduce with the checkout's test interpreter and verification xcrun wrapper on PATH (the wrapper recipe is below):

```sh
.venv/bin/python -m pytest tests/unit/test_app_cutover_daemon.py -q
export PATH="$PWD/build/app-cutover-evidence/bin:$PATH"
.venv/bin/python tools/verify_app_cutover.py baseline
.venv/bin/python tools/verify_app_cutover.py mutations
.venv/bin/python tools/verify_app_cutover.py frontend
app/build.sh "$PWD/build/app-cutover-evidence/product"
```

The baseline/mutation runner overlays tests in isolated copies and restores each mutated source. Logs and machine-readable summaries live under ignored `build/app-cutover-evidence/`; large logs are not committed.

At the end of verification, every recorded compiler start had a completion. The process audit found only the Codex runtime and the audit interpreter using this workspace as their cwd, with no test or build child left running. Every yielded command session was awaited.

Verification wrapper recipe: create executable `build/app-cutover-evidence/bin/swift-frontend` that execs this workspace's `.venv/bin/python tools/foreground_swift_frontend.py` with `"$@"`; create executable `bin/xcrun` that delegates to `/usr/bin/xcrun`, adding `-disable-sandbox -j 4 -driver-use-frontend-path <absolute-path-to-that-wrapper>` only for `swiftc`. Other xcrun operations delegate unchanged. `tools/app_cutover_pytest.py` supplies the probe cache used by the baseline, mutation and frontend runner modes.

## Delivery

Commits are on workspace-local branch **`fix/app-cutover-cont`** in `.git-local`, because the shared git metadata is read-only. `.git-local` is ignored and is not part of the deliverable. Coherent completion commits include `bb75c9e` (daemon/protocol verification), `bd8435a` (recovery flows and isolated frontend evidence), and `c9d18b6` (oversized activity timestamp), followed by the final evidence/report commit. All include the required coauthor.

Bundle: **`docs/reports/2026-10-03-app-cutover.bundle`**, head **`refs/heads/fix/app-cutover-cont`**, prerequisite **`7bfda766`**. Bundle verification and an independent prerequisite-plus-bundle restoration passed; the restored head matched this local branch exactly. The report is committed; the binary bundle is a separate workspace artifact. Nothing was pushed.
