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


## PR #124 review fixes — 2026-10-04

This section replies to the complete REQUEST CHANGES review of `75972d4a9506132ac066c2e7474d5a1b9ac20f10`. It supersedes the earlier first-use routing/recovery claims above. There was no P1. Both P2 findings and all runtime correctness findings are fixed; the disclosure and coverage findings are also addressed. Work stayed in the assigned checkout, with task-owned temporary test storage. No live `~/.subfleet`, running daemon, installed app, or live Application Support files were accessed. No sub-agents, detached jobs, pushes, or history rewrites were used.

The shipped routing policy now defines `sol = gpt-6.1-sol` and uses it for its hard-tier Codex routes. The former `astra` and `gpt-6-astra` pins resolve through retired aliases to Sol. The daemon derives the Codex conversation default from the policy's hard tier; the app consumes that published default and rejects retired Astra preferences/catalog choices.

### Replies to every finding

All file:line references are relative to this checkout. UI tests below are in `tests/frontend/test_pr124_fixes_ui.py` (UI); daemon tests are in `tests/unit/test_pr124_fixes.py` (daemon). Baseline means the actual unmodified production sources at `75972d4a`, with the regression tests overlaid. Failures below are assertion failures, not missing-API compilation errors.

| Finding | Fix and location | Regression and result on 75972d4a | Mutation result |
| --- | --- | --- | --- |
| P2-1: retry destroys another draft and can duplicate its message | `app/Sources/UIModel.swift:813`, `:849`, `:584`: keep the ordinary composer intact, persist each retry under its own hashed key, restore the composer on navigation/discard/completion, and label the sheet “Retry saved conversation” (`app/Sources/UINewConversation.swift:19`). Existing atomic retry at `app/Sources/Outbox.swift:252` keeps the original create/message IDs. Stable ordinary-composer IDs at `app/Sources/NewConversationDraft.swift:22` and `UIModel.swift:115` prevent a crash from offering already-journaled words again; `ConversationStore.swift:957` resumes a create already acknowledged before its first message was journaled. | UI `test_retry_preserves_composer_and_original_message_identity`:39 and `test_switching_retries_and_relaunch_preserve_the_composer`:57 **fail**; crash-window tests at :160 and :175 **fail**. `tests/frontend/test_pr124_recovery_properties.py:43` **fails** on the lost “my unsent idea”. | Independently restoring composer-file overwrite, omitting composer restoration, disabling crash reconciliation, and returning the obsolete draft key each cause the corresponding assertion to fail. The Hypothesis property also catches the composer-file overwrite. |
| P2-2: empty-fleet Auto chooses Codex/Astra | `app/Sources/StatusModel.swift:135`: Codex requires Codex > 0 and Claude == 0; all other cases use Claude. `subfleet/conversations/service.py:298` reads hard-tier policy models and excludes Astra defaults; `subfleet/default_policy.json` retires Astra routing. `NewConversationDraft.swift:74` rejects remembered/offered Astra and resolves the published model ID to its offered provider value. | UI Auto cases :69: **0/0 fails, 0/3 fails, 1/3 passes**. Remembered-Astra case :84 **fails**. Daemon shipped-policy :29, custom-hard-tier :39 and legacy-policy default :50 tests all **fail**. | Empty-fleet Codex, Astra policy routing, hard-coded Sol alias selection, retired-model default fallback, and remembered-Astra selection are each caught by assertions. |
| P3-1: optional workspace capability never checked | `UIModel.swift:528`, `:610`: preview and Start recheck run only with `workspace.check.v1`; an older daemon's create decides admission. | UI `test_older_daemon_skips_optional_check_at_open_and_start`:94 **fails**: Start is disabled on the head, whereas the fix makes zero check calls and journals create/message. | Forcing checks despite a missing capability disables Start and fails the assertion. |
| P3-2: unanswered check never retried and drops the folder | `NewConversationDraft.swift:20`, `UIModel.swift:499`, `:579`, `:587`: distinguish transport failure from refusal, retain the folder, and recheck on reconciliation/availability. `UINewConversation.swift:134`, `:146` recheck availability and an explicit folder pick, including the same folder. | UI `test_unanswered_check_keeps_folder_and_rechecks_on_reconcile`:107 **fails**; the same-folder control :116 already **passes** when the model validation method is explicitly called. | Classifying the transport error as a permanent refusal drops the saved folder and fails the assertion. The primary regression also requires exactly two checks and enabled Start after reconciliation. |
| P3-3: create/handoff restrictions undisclosed | `docs/desktop/design.md:785` and this reply explicitly disclose that Ask, Accept edits and Bypass on in-place `main`/`master` require `allow_main`, and Claude Bypass is writable for protected-folder checks, including handoff. The correct protections remain in place. | This is a disclosure finding, not a failing runtime behavior on 75972d4a. Existing real check/create protected-branch and protected-home cases in `tests/unit/test_app_cutover_daemon.py:69` and :46 cover the restrictions and pass. | No production restriction was weakened. Runtime mutations are unnecessary for a documentation-only finding. |
| P3-4: empty folder and missing provider disagree | `service.py:465`, `:501`, `:511`: both operations default a missing provider to Claude; check previews No folder scratch admission without creating a directory, including the shared branch/protection checks. Worktree + No folder remains refused. | Daemon `test_no_folder_check_and_create_agree_without_check_writes`:13 (**empty and null**) and `test_missing_provider_check_and_create_use_claude`:22 all **fail**. | Disabling scratch preview fails both empty/null assertions; removing create's provider default fails the provider agreement assertion. |
| P3-5: resolved refusal leaves stale footer | `UIModel.swift:770`: clear only the refusal notice when no refused drafts remain, including after successful retry. Other problems survive. | UI `test_last_successful_retry_clears_only_its_notice`:124 **fails**; unrelated-problem control :197 **passes**. | Removing notice cleanup leaves the old footer and fails the assertion. |
| P3-6: git folder checks block message journaling | `UIModel.swift:545`, `:566`, `:615`: all folder checks, including the Start recheck, use the concurrent read queue; only journal/sender work uses the serial outbox queue. | UI `test_a_send_does_not_wait_for_folder_git_checks`:141 **fails** with an injected two-second check. | Routing reads back to the outbox queue makes journaling take **2007 ms**, violating the <1000 ms assertion. |
| P3-7: Start-time refusal guard untested | UI `test_start_rechecks_a_folder_and_journals_nothing_if_it_changed`:150 passes a sheet check, then refuses at Start and requires no outbox file plus retained text. The guard remains at `UIModel.swift:620`. | **Passes** on 75972d4a: the existing guard is correct; the finding was missing coverage. A passing baseline is expected for this coverage-only finding. | The original surviving `guard check.ok || true` mutation is now caught because it creates an outbox file. Removing `!failure.retryable` is equivalent under the current state machine (only non-retryable failures enter `.failed`), so it was not reported as a meaningful killed mutant. |
| Minor correctness: future transcript timestamp pins a row | `subfleet/conversations/catalog.py:542`, `:550`: ignore mtimes beyond the current clock while retaining the existing finite/type/range checks and cached-only reads. | Daemon `test_future_activity_cannot_pin_an_older_conversation`:56 **fails**: a future catalog mtime sorts the older row first. | Restoring the year-9999 ceiling without the current-time bound fails the ordering assertion. |

Baseline regression totals: **7 daemon failures; 11 UI failures and 4 passing controls; 1 Hypothesis property failure**. Thus **19 failing behavioral cases and 4 controls**, with no compile/fixture errors in the accepted baseline evidence. UI baseline wall time was 273.65 seconds; final property baseline was 117.48 seconds (the earlier run took 51.92 seconds). The daemon baseline was run before any production edit, in 8.93 seconds.

Mutation totals: **18 individually activated checks caught by assertion failures** (six daemon, eleven UI, and the recovery property). No compilation/fixture failure counts as a caught mutant. UI faults are independently selected in an isolated generated source copy with `PR124_MUTATION`; that instrumentation does not exist in production. This permits one content-checked compilation while testing each fault separately. Final mutation slices took 11.69–133.01 seconds, including the refreshed final-property check; the separately completed compilation took 254 seconds. Full logs and commands are in ignored `build/pr124-evidence/`.

### Final required-suite results

The accepted changed-tree runs contain **537 passed, four pre-existing sandbox failures, and one sandbox skip** across 542 collected cases. App-protocol is included in the frontend count. All tests ran in awaited foreground slices with a 580-second deadline; the longest accepted required-suite slice took 455.18 seconds.

| Suite | Result | Foreground wall time |
| --- | --- | --- |
| Conversation service | 91 passed, 1 sandbox failure | 350.13 s |
| Catalog | 13 passed | 98.08 s |
| Catalog lifecycle | 20 passed, 1 sandbox failure | 328.91 s |
| Status JSON | 54 passed, 2 sandbox failures | 149.31 s |
| App-cutover daemon + new daemon regressions | 20 passed | 421.40 s |
| Frontend | 339 passed, 1 sandbox skip; 340 collected | 28 slices, 9.88–455.18 s; 3526.38 s total |
| App protocol, included above | 9 passed | 28.28 s |
| Policy and policy-support, additional | 162 passed (182 with the 20 cutover cases repeated) | 18.06 s combined |

The four failing nodes also fail unchanged at `75972d4a` in the isolated baseline (**4 failed**, 176.48 seconds). They were neither edited nor bypassed:

- `test_conversation_service.py::test_a_request_that_reaches_its_text_after_the_service_closed_writes_nothing`: macOS boot identity unavailable.
- `test_conversation_catalog_lifecycle.py::test_closing_the_daemon_stops_its_catalog_run_and_the_removed_root_stays_gone`: sandbox denies `/bin/ps`.
- `test_status_json.py::test_c18_1_the_daemon_hands_the_timer_its_probe_records`: macOS boot identity unavailable.
- `test_status_json.py::test_c18_1_a_commit_inside_the_snapshot_after_its_rows_is_not_published`: macOS boot identity unavailable.

The native peer/process frontend case skips for the same unavailable process inspection. Temporary test roots use the lane's own normal macOS temporary directory; recovery-service histories use explicit isolated `/private/tmp` roots. Earlier setup attempts involving shared pytest retention, short roots unsuitable for catalog continuation, undecoded SQLite rows, or an oversized history batch were corrected and rerun; none is counted as accepted regression or mutation proof.

`app/build.sh` **passed in 264.83 seconds** (900-second allowance), producing `build/pr124-evidence/product/Subfleet.app`, bundle ID `org.maxghenis.subfleet`. `codesign --verify --deep --strict` passed; `otool -L` reports only system frameworks and system/Swift libraries. The app was not installed or launched. Existing Swift concurrency diagnostics and the verification-wrapper library-search-path warning remain.

The recovery Hypothesis property passed with seeds **124, 125, 126 and 127**: **40 generated passing histories plus 12 explicit histories**, each up to eight generated actions. The explicit histories cover crashes after the real isolated service accepted create and submit. After every segment a fresh UI process reopens the same durable journal, drains recoverable work, and checks original IDs, unique accepted text, visible failed-row messages, explicit withdrawals, and the preserved ordinary composer. The seed slices took **99.40, 42.39, 54.03 and 44.66 seconds**. The final test also fails on the untouched PR head (**117.48 seconds**) and catches the composer-overwrite mutation (**133.01 seconds**).

Reproduction uses `uv sync --group dev`, the verification-only xcrun wrapper recipe above with its directory changed to `build/pr124-evidence/bin`, and the workspace's `.venv`. The wrapper preserves the outer filesystem sandbox while avoiding the compiler macro plugin's nested sandbox failure. `tools/pr124_verify.py` owns deadlines, per-slice logs and exact-child cleanup; `tools/app_cutover_pytest.py` records child starts/completions and caches probes by source content and flags.

```sh
.venv/bin/python -m tools.pr124_suites daemon
.venv/bin/python -m tools.pr124_suites frontend
.venv/bin/python -m tools.pr124_review_evidence baseline
.venv/bin/python -m tools.pr124_review_evidence daemon-mutations
.venv/bin/python -m tools.pr124_review_evidence ui-mutations
SF_CUTOVER_FRONTEND_SECONDS=850 .venv/bin/python tools/pr124_verify.py app-build 900 app/build.sh "$PWD/build/pr124-evidence/product"
```

Machine-readable accepted results are committed in `docs/reports/2026-10-04-app-cutover-fixes-evidence.json`; full logs and source copies remain in ignored `build/pr124-evidence/`. Only assertion failures count toward baseline/mutation proof. Each returned command session was awaited. The final audit recorded **604 compiler starts and 604 completions**, with no unfinished compiler or test-child records and no task-owned process left running.

### Review-fix delivery

Shared git metadata is read-only in this sandbox, so coherent commits use `.git-local` branch **`fix/pr124-first-use-review`**, descending directly from `75972d4a9506132ac066c2e7474d5a1b9ac20f10`: **`59b166e`** (daemon policy/admission/activity fixes), **`bb26a90`** (UI retry/routing/check fixes and regressions), **`fae703a`** (final property visibility and foreground verification tooling), followed by this report/evidence commit. Every new commit has `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

The separate binary artifact is **`docs/reports/2026-10-04-app-cutover-fixes.bundle`**, head **`refs/heads/fix/pr124-first-use-review`**, sole prerequisite **`75972d4a9506132ac066c2e7474d5a1b9ac20f10`**. Its head includes this report. The final response names the exact head SHA. Bundle verification and an independent prerequisite-plus-bundle restore passed and matched that head; the bundle is ignored to avoid embedding itself in its own commit. Nothing was pushed, and the caller's checkout was not written.
