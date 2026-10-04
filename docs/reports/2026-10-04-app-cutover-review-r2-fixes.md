# PR #124: round-two review fixes

Replies to the complete REQUEST CHANGES review at `b11f766b7709353de8d8d12db0ba965f710738ea`, against PR base `f832e6b8`. There is no P1. The new P2 and all four new P3 findings are addressed. Work used only the assigned workspace and task-owned temporary storage. No live Subfleet state, running daemon, installed app, or Application Support files were modified. No sub-agents, background jobs, pushes, history rewrites, policy-file changes, or routing-alias changes were used.

## Finding replies and regression proof

UI tests are in `tests/frontend/test_pr124_review_r2_fixes_ui.py`; unit/docs tests are in `tests/unit/test_pr124_review_r2_fixes.py`. Baseline evidence overlays the tests onto an archive of **b11f766b**. Production source files in that archive are unchanged. The UI fixture compiles a temporary copy of `UIModel.swift` with an appended, access-only extension to set availability and apply model responses; identical helpers are used for baseline, fixed, and mutated code. It starts no window or watch loop.

| Finding | Fix (file:line) | Test and result on b11f766b | Mutation result |
| --- | --- | --- | --- |
| **P2: unavailable daemon skips folder admission** | `app/Sources/UIModel.swift:528` blocks unknown/down/busy/incompatible/refused availability with a visible, transient reason. Only a ready daemon known to lack the capability can bypass the optional check. `:524` retains pending recent-folder selection until checks can run. `:612` rejects stale acceptance at Start; `:624` defaults unknown capability support to checking. Readiness recovery checks folders again. | UI `test_unready_daemon_disables_start_with_reason`:18 (**10 failed**), `test_start_rejects_stale_acceptance_after_losing_readiness`:34 (**5 failed**), and `test_unready_initial_default_waits_and_rechecks_recent_folders`:45 (**1 failed**). Assertions require disabled Start, a reason, preserved text, and no journal, then actual folder checks after recovery. | `preview-unready` restores false acceptance: **1 assertion failure**. Independently, `start-unready` removes the Start guard and restores unknown-capability skipping: **1 assertion failure**, with an outbox file incorrectly created. |
| **P3-A: opening before catalog arrival erases saved model** | `app/Sources/NewConversationDraft.swift:84` retains the existing model while no catalog is loaded; a loaded catalog still excludes retired/unoffered picks. | UI `test_saved_model_survives_opening_before_catalog_load`:63 checks Claude Sonnet and Codex Terra on screen, on disk, after catalog arrival and after reload: **2 failed**. | `saved-model` restores the empty-string fallback: **2 assertion failures**. |
| **P3-B: scratch reset changes the draft currently on screen** | `app/Sources/UIModel.swift:659` resets the submitting composer's scratch folder within the same message-identity branches as `journaled()`. `:662` resets `composerBeforeRetry` when a retry has opened meanwhile. | UI `test_scratch_start_resets_its_composer_when_retry_opens_during_check`:77 opens a retry during the Start check, then starts the ordinary composer again and requires two distinct scratch folders: **1 failed**. | `scratch-owner` routes the reset to the displayed retry: **1 assertion failure**, because the restored composer reuses its first scratch folder. |
| **P3-C: inaccurate shipped-policy documentation** | `docs/desktop/design.md:803` describes shipped active Astra, loaded-policy retirement, and fallback selection. `docs/reports/2026-10-03-app-cutover.md:75` and `:84` correct the reverted Sol/Astra claims. The policy-revert report's obsolete incidental-Astra exclusion claim is also corrected. `design.md:786` documents fail-closed readiness. | Unit/docs `test_design_describes_the_shipped_policy_without_claiming_astra_retired`:31 and `test_pr_report_does_not_claim_the_reverted_policy_is_still_shipped`:37: **2 failed**. | Independently restoring the stale design and report claims (`design-stale`, `report-stale`): **1 assertion failure each**. |
| **P3-E: literal Astra exclusion disagrees with app fallback** | `subfleet/conversations/service.py:304` filters through the loaded policy's retirement metadata; `:308` uses those active offerings as fallback without an id-specific exclusion. Existing tests encoding the former exclusion now expect the active model. | Unit `test_active_astra_only_catalog_publishes_fallback`:12, with no hard tier and with a Claude-only hard tier: **2 failed**. Alias/id retirement controls at :24: **2 passed**. | `astra-literal` restores the hard-coded exclusion: **2 assertion failures**. `ignore-retired` disables retirement filtering: **2 assertion failures**. |

Accepted baseline totals: **23 assertion failures and 2 passing controls**, with no fixture or compilation errors. Unit/docs slice: **11.21 s**; UI slice: **108.02 s**. Fixed focused checks: **15 unit/docs passed** (8.87 s) and **36 UI passed** (141.90 s), including all 17 existing review UI cases.

All **8 independently activated mutations** were caught by **11 assertion failures**, with no fixture/compile failures or timeouts. Mutations run only in an isolated copy; their environment switches do not exist in production. Slice times were **3.00–57.00 s**. An earlier baseline attempt exposed a private-setter error in the new probe, corrected by the access-only fixture above. Its 19 setup errors are excluded from regression proof; this was test instrumentation, not a product failure.

## Round-one findings and review nits

The review already verified every round-one fix at b11f766b. These changes retain them, with existing regressions covered by the final required suites:

| Prior finding | Reply and coverage |
| --- | --- |
| P2-1: retry overwrites composer / duplicates message | Composer isolation and original message identities remain; existing retry UI cases and recovery property are retained. |
| P2-2: Auto selects Codex with no ready fleet | Auto still requires ready Codex and zero ready Claude. Loaded-policy defaults remain authoritative; shipped Astra is active and may be a default. |
| P3-1: old daemon lacks workspace.check | The existing ready-old-daemon regression passes; only unknown/unready bypass was removed. |
| P3-2: transient check never retried | Existing retry and same-folder tests pass. New readiness recovery also resumes pending default selection. |
| P3-3: create/handoff restrictions undisclosed | Existing `design.md:785` disclosure remains, including main/master and Claude Bypass protections. |
| P3-4: check/create disagree on empty folder or provider | Existing empty/null and missing-provider agreement tests are retained. |
| P3-5: stale refusal footer | Existing last-retry notice cleanup and unrelated-problem controls are retained. |
| P3-6: checks block message journaling | Checks still use the concurrent read queue; the existing delayed-check send regression is retained. |
| P3-7: no Start-time refusal test | Existing Start recheck/refusal test remains and journals nothing on refusal. |
| Future transcript mtime pins old row | Existing future-activity sorting regression is retained. |

The four nits requested no changes: policy chain ordering remains deterministic, Codex model memory remains subordinate to the published default, availability refreshes may check twice, and successful retry navigation is unchanged.

## Required verification

Required suites contain **730 passed, 4 base-reproduced sandbox failures, and 1 sandbox skip**, across **735 distinct cases**. App protocol is included in the frontend count. Focused checks and the additional seed-9001 property run are duplicates and are not added to that total.

| Suite | Result | Foreground seconds |
| --- | --- | ---: |
| Conversation service | 91 passed, 1 failed | 126.77 |
| Catalog | 13 passed | 4.82 |
| Catalog lifecycle | 20 passed, 1 failed | 19.12 |
| Status JSON | 54 passed, 2 failed | 9.49 |
| App-cutover daemon + existing daemon regressions | 22 passed | 31.95 |
| Policy + policy-support + new unit/docs regressions | 168 passed | 11.13 |
| Frontend | 362 passed, 1 skipped; 363 collected | 29 slices, 1.60–408.89 each; 3206.59 total |
| App protocol, included above | 9 passed | 107.99 |
| Recovery property, additional seed 9001 | 1 passed: 13 passing histories | 147.79 |

All four failures were rerun against an **unchanged f832e6b8 archive, including its original tests**, and fail with the same errors (**4 failures**, 10.84 s across four slices). They were neither edited nor bypassed:

- `tests/unit/test_conversation_service.py::test_a_request_that_reaches_its_text_after_the_service_closed_writes_nothing`: macOS boot identity unavailable; same failure on base (4.18 s).
- `tests/unit/test_conversation_catalog_lifecycle.py::test_closing_the_daemon_stops_its_catalog_run_and_the_removed_root_stays_gone`: sandbox denies `/bin/ps`; same failure on base (3.46 s).
- `tests/unit/test_status_json.py::test_c18_1_the_daemon_hands_the_timer_its_probe_records`: macOS boot identity unavailable; same failure on base (1.63 s).
- `tests/unit/test_status_json.py::test_c18_1_a_commit_inside_the_snapshot_after_its_rows_is_not_published`: macOS boot identity unavailable; same failure on base (1.57 s).

The native frontend development-daemon fixture skips because boot/process inspection is unavailable. Thus the review's lane-marker `approval.get` scenario cannot run in this sandbox; no approval check was weakened. Every executed frontend test passes.

The recovery property passed under its normal seed **124** in the full frontend run, and under **9001** separately. The seed-9001 log records **13 passing histories: 10 generated and 3 explicit**, including crashes after the isolated real service accepts create/submit. Hypothesis also reports one invalid generation attempt, which is not counted as a history.

`app/build.sh` **passed in 149.73 seconds**, producing `build/pr124-evidence/review-r2-product/Subfleet.app`, bundle id `org.maxghenis.subfleet`. `codesign --verify --deep --strict` passes; `otool -L` lists only system frameworks and system/Swift libraries. The app was neither installed nor launched. Existing Swift concurrency diagnostics and the verification-wrapper library-search-path warning remain.

The final audit matches **379 compiler starts/completions** and **3209 test-child starts/completions**, with no unfinished records. All foreground sessions have completed, and no task-owned test/build child is left running. The shipped policy is byte-identical to both b11f766b and f832e6b8.

## Reproduction and delivery

Dependencies: `UV_CACHE_DIR=$PWD/.uv-cache uv sync --group dev`. The existing verification-only xcrun wrapper is generated under `build/pr124-evidence/bin` as documented in the prior report: Swift builds use `-disable-sandbox -j 4` with `tools/foreground_swift_frontend.py`; the outer workspace sandbox remains in force. The content cache keys exact source contents and flags. All test slices use the existing foreground runner's 580-second deadline; the app build gets 900 seconds. Every yielded session is awaited.

```sh
.venv/bin/python -m tools.pr124_review_r2_evidence baseline
.venv/bin/python -m tools.pr124_review_r2_evidence focused
.venv/bin/python -m tools.pr124_review_r2_evidence mutations
.venv/bin/python -m tools.pr124_suites daemon
.venv/bin/python -m tools.pr124_review_r2_evidence policy
.venv/bin/python -m tools.pr124_suites frontend
.venv/bin/python -m tools.pr124_review_r2_evidence property
SF_CUTOVER_FRONTEND_SECONDS=850 .venv/bin/python tools/pr124_verify.py r2-app-build 900 app/build.sh "$PWD/build/pr124-evidence/review-r2-product"
```

Full logs and isolated copies remain in ignored `build/pr124-evidence/`; concise results are committed alongside this report. Shared Git metadata is read-only, so coherent commits are on `.git-local` branch **`fix/pr124-review-r2`**, descending directly from b11f766b: **`8cac9027`** (UI correctness), **`ea89f784`** (policy defaults/docs), **`250b573b`** (foreground verification tooling), followed by the final evidence/report commit. Every commit includes `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

The delivery bundle is **`docs/reports/2026-10-04-app-cutover-review-r2-fixes.bundle`**, head **`refs/heads/fix/pr124-review-r2`**, sole prerequisite **`b11f766b7709353de8d8d12db0ba965f710738ea`**. The bundle is a separate ignored artifact to avoid embedding itself in history. The exact final head is named in the final response. Bundle verification and an independent prerequisite-plus-bundle restoration passed; the restored head matches the local branch. Nothing was pushed, and the caller’s checkout was not written.
