# PR #128, round five: REQUEST CHANGES

Reviewed head `5a11c73edc30` against `integrate/2111-features`. Fixes under review: `142b184bf` (explicit null grants), `a22020d00` (generated evidence removed), `0982b0311` (history-summary assertion), report `5a11c73ed`. No production or repository test source was changed. New tests are in [round-five-tests.patch](round-five-tests.patch).

The round-four P1 is fixed in behaviour. Loaded cards and sheets show explicit nulls, including the Notion removal of Salary and Owner. Named plumbing stays hidden in its own container, and unknown keys stay visible. A 1,000-request differential property test finds no deviation from the stated rule.

The fix over-reaches into the history summaries, though. Every Claude history card, answered questions included, now shows a `blocked_path: null` "grant" that the request never contained. Codex command and file-change history cards gain `null` rows for absent fields too. The round-two test that would have caught this was rewritten to require it.

## Findings

### P2: history cards show adapter placeholders as `null` grants that the request never contained

Where:

- `app/Sources/ApprovalPresentation.swift:117` emits null leaves.
- `ApprovalPresentation.swift:50-63` is the summary source, used whenever `request == nil`.
- The rewritten assertion is `tests/frontend/test_visual_round_two.py:138-140` (commit `0982b0311`).

The daemon's summaries write every absent field as `None`:

- `subfleet/conversations/claude_turn.py:548-556` always sets `"blocked_path": request.get("blocked_path")`.
- `subfleet/conversations/codex_turn.py:676-688` always sets `network`, `execpolicy_amendment` and `network_amendments` for commands, and `grant_root` for file changes.

The providers don't send these keys when they're absent:

- **Claude 2.1.284** builds `can_use_tool` with `blocked_path: g.blockedPath` (undefined, so JSON drops it). Its bridge path spreads `...Me&&{blocked_path:Me}` ([binary excerpt](output/claude-can-use-tool.txt)).
- **Codex 0.159** (`codex app-server generate-ts`, [types](output/codex-0.159-ts/)) declares `networkApprovalContext`, `proposedExecpolicyAmendment`, `proposedNetworkPolicyAmendments` and `grantRoot` as optional (`?:`).

Because the adapter writes `None` whether a key was absent or explicitly null, a top-level summary `null` carries no request information.

Base `05c30b87` dropped these placeholders. Head renders them as grant rows on every card that shows the summary:

- every answered or withdrawn history card, since history calls `approval.get` only when Details is opened (`UIApprovals.swift:92`, `:134`);
- a pending card until its request loads;
- a pending card whose load failed.

**Scenario.** A Claude AskUserQuestion is answered. Its raw request has no `blocked_path`. The history card now shows the question, the answer, and a grant box reading `blocked_path: null`. Details, which shows the raw request, has no such field.

**Executed evidence.** Scenes come from the real `ClaudeTurn`/`CodexTurn` adapters fed real-shaped requests ([generator](probes/real_summaries.py), [scenes](output/real-scenes.json)). Renders are native and offscreen, with OCR, in light and dark themes, with 0 visible windows.

| Request (history card) | Base rows / height | Head rows / height |
| --- | --- | --- |
| Claude AskUserQuestion, answered | question + answer, 132 pt | **+ `blocked_path: null`** box, 182 pt |
| Claude Bash `make test` | `input: …`, 130 pt | **+ `blocked_path: null`**, 148 pt |
| Claude Notion removal | `input.data_source_id`, 130 pt | **+ `blocked_path: null`**, + Salary/Owner `null` (correct), 174 pt |
| Codex command | cwd, command, input_kind, 168 pt | **+ `execpolicy_amendment: null`, `network: null`, `network_amendments: null`**, 234 pt |
| Codex file change, no root | no rows, 80 pt | **+ `grant_root: null`**, 130 pt |
| Repo fixture `question-two` (approval-layout.json) | 184 pt | **+ `blocked_path: null`**, 234 pt |
| Repo fixture `claude-short-bash` | `input: echo done` | **+ `blocked_path: null`** |

Supporting files:

- Model rows: [head](output/real-summary-rows-head.json) and [base](output/real-summary-rows-base.json), via [R5SummaryProbe.swift](probes/R5SummaryProbe.swift).
- Native results: [head](output/history-views-head.json) and [base](output/history-views-base.json), via [R5HistoryViewProbe.swift](probes/R5HistoryViewProbe.swift), which is R2ApprovalViewProbe with OCR on every answered card.
- Repo layout fixture: [head](output/r2-layout-head.json) and [base](output/r2-layout-base.json).
- Renders: question [head](renders/head-history-claude-question.jpg) / [base](renders/base-history-claude-question.jpg), Codex command [head](renders/head-history-codex-command.jpg) / [base](renders/base-history-codex-command.jpg), file change [head](renders/head-history-codex-file-change.jpg) / [base](renders/base-history-codex-file-change.jpg).
- Regression test (in the patch): `test_history_summaries_from_real_adapters_show_no_placeholder_nulls` **fails at head** with `('bash', [['blocked_path', 'null'], …])` and **passes at base** ([head log](output/r5-properties-head.log), [base log](output/r5-properties-base.log)).

**Why the fix's tests missed it.** The round-four projection tests build summaries by hand (`R4NullPresentationProbe.swift`, `approval-null-requests.json`), not from the adapters, so they never see the placeholders. The fix report says the history value is "the explicitly supplied `blocked_path: null` summary value; the raw question request omits that field" (`docs/reports/2026-10-06-pr128-r4-fix.md`). That describes this defect and then asserts it.

**Severity.** P2, not P1: no grant is hidden, and loaded cards and sheets are correct. But every Claude and Codex history card shows rows that contradict the request. The card's content changes between pre-load and post-load: the file change goes from `grant_root: null` to no rows. And round two's single-presentation question card regresses.

**Suggested fix.**

1. In the summary path only (`request == nil`), drop null values at the top level of `display.fields`; they are the adapters' absent-field placeholders.
2. Keep nulls nested inside a decoded `input` or `permissions`, which copy request values. The Notion `input.properties.Salary: null` must stay.
3. Optionally, stop writing `None` placeholders in the two adapters. Stored history still carries them, so the app-side filter is needed regardless.
4. Restore the round-two history assertion's intent: no `null` row that isn't in the request.

### P3: the visual-review report added by this PR links 29 evidence files that were never committed

`docs/reports/2026-10-04-visual-review-fixes/README.md:47,51,72` and `GALLERY.md:3` link `test-summary.json`, `frontend-suite.json`, `app-build.json`, `slices/app-build.log`, `snapshots.json`, `rendered-text.json` and 23 more mutation/JUnit/log files. None of them exists in the tree or in this PR's history. That is correct per the no-run-artifacts rule, but the links now dangle.

Round four cleaned this up only for `2026-10-03-visual-pass/`. `test_visual_report_hygiene.py` scans only that directory. Found by an executed link check over `docs/reports` (the 2026-10-04 README was added in `3641bf787`, not at the merge base).

Fix: reword those links as in `a22020d00`, and extend the hygiene test to every report directory the PR adds.

No other P1–P3 findings.

## Checks

### 1. The round-four P1, in behaviour

| Check | Result |
| --- | --- |
| Reviewer's Notion request (Claude MCP `API-update-a-data-source`, `properties: {Salary: null, Owner: null}`), card and sheet | **Fixed.** `test_null_removals_are_visible_before_allowing` passes in light and dark, for cards and sheets. The round-four reviewer's own `test_r4_null.py` now passes 4/4 (it failed 2/4 in round four). At the largest text size the sheet shows both removals and all actions ([render](renders/large-notion-sheet.jpg), [result](output/large-views-head.json)). |
| Real adapters | The real `ClaudeTurn` keeps both nulls in the request and in the Allow reply. The loaded rows from the real adapter scene are `input.data_source_id`, `input.properties.Owner: null`, `input.properties.Salary: null` ([rows](output/real-summary-rows-head.json)). |
| Named plumbing hidden, unknown keys visible, nulls everywhere | **Differential property test** (in the patch). Hypothesis generated 40 batches of 25 Claude/Codex requests: random nested objects, arrays, nulls, empty containers, quoted keys, and plumbing names at every depth. Each request's loaded rows equal a Python reference of the stated rule: every leaf under its exact path, except the named keys in their own container; keys unique. **Passes at head** ([log](output/r5-properties-head.log)). **Fails at base** with the minimal counterexample `params.data_source_id: null` dropped ([log](output/r5-properties-base.log)). It kills two mutants: plumbing filtered at every depth (counterexample `params.agent_id: null` hidden) and null rendered as an empty string ([A](output/r5-properties-mutA.log), [B](output/r5-properties-mutB.log)). |
| Codex requests | The null fixtures and the review's real-shaped Codex scenes validate against the Codex **0.159.0** schema generated in this run, not only the vendored 0.153.3 schema ([result](output/null-fixtures-codex-0159-schema.txt)). Codex has no MCP approval card: `mcpServer/elicitation/request` is refused (`codex_turn.py:695-696`), so the MCP case is Claude's. |
| History summaries | The Notion removal survives in history. **Spurious placeholder nulls are added: P2 above.** |

Note, not a finding: Codex 0.159 declares `environmentId` (command, permissions) and the `network`/`fileSystem` permission subprofiles as required-nullable keys. So loaded Codex cards now show rows such as `params.environmentId: null` and `params.permissions.fileSystem: null` whenever the server sends null. These are request values, and the brief asks for them to be shown. The literal `null` means "remove" for Notion and "not requested/default" for Codex; the projection is faithful either way.

### 2. The other fixes

| Check | Result |
| --- | --- |
| Parsed actions and callback ids filtered at their container; numeric array order | Pass. Covered by `test_metadata_filter_only_applies_at_its_schema_container`, `test_the_command_precedes_parsed_actions_and_arrays_keep_their_order` and `test_real_shaped_requests_retain_every_grant_field`. The round-four reviewer's paginated `codex-permissions` case passed: 12 write roots in order on card and sheet. The property test also checks `approvalId`/`commandActions` hidden under `params` and visible elsewhere, null or not. |
| Count contrast | Pass: `test_pending_count_contrasts_with_its_rendered_background`, 4/4 theme/focus cases ≥ 4.5:1. |
| History banners | Pass: `test_history_details_load_without_a_false_pending_error`, answered and withdrawn. |
| Shell labels keep the first substantive command | Pass: `test_shell_preludes_keep_the_first_substantive_command`, plus the round-two wrapper/cd/pipe label tests. |
| Fixture schema | Pass: `test_codex_regressions_use_schema_valid_requests`, `test_file_change_fixture_matches_the_request_schema`, `test_file_change_fixture_validates_against_the_vendored_codex_schema`, and the round-four reviewer's `test_real_requests_fit_the_installed_codex_schema` (3/3 against 0.159.0). |
| Largest text keeps request and actions visible | Pass: `test_largest_supported_text_keeps_request_and_actions_visible`, 4/4. Also rendered: the review's real-shaped Notion, Bash, Codex command, file-change and permissions sheets at `TextScale.range.upperBound` all show their grant rows and decision buttons within 720 pt, light and dark ([result](output/large-views-head.json)). |

### 3. No regression

| Check | Result |
| --- | --- |
| Removed evidence files unread | `git grep` at `a22020d00~1` finds **0 references** to any of the nine files in `tests`, `app`, `tools`, `bin`, `.github` or `subfleet`. Snapshot tests pass 3/3 without them. |
| Round-two approval heights | Unchanged from base and from round four. Card/sheet in light and dark: short Bash **164/222**, 120-line Write **364/389**, long Codex command **364/400**, masked Write **390.5/400** pt. `fits_in_400` = 400 for all. `test_large_grants_keep_actions_visible_and_fit_a_400_point_offer` 8/8. The last Write line and masked confirmation are reachable. ([head](output/r2-layout-head.json), [base](output/r2-layout-base.json)) |
| Sidebar keyboard behaviour | Pass: `test_sidebar_uses_native_keyboard_selection_and_separate_badge` and `test_c27_5_the_sidebar_hand_badge_is_a_button_that_shows_the_cards`. |
| Question cards | The picker stays near the top. Pending questions and answers render once and survive reset/replay (`test_core_question_card.py` 10/10, `test_question_picker_is_near_the_top_without_flattened_questions`, `test_submitted_answers_survive_event_reset_and_replay`). **But the answered card now carries a `blocked_path: null` box** (184 → 234 pt): the P2. |

## Test counts

**Frontend: 561 collected, 560 passed, 0 failed, 0 skipped. 1 excluded:** `test_core_live.py`, which launches a development daemon and is refused inside a Subfleet lane. It ran in nine serial foreground slices ([commands and outcomes](slices.jsonl)).

| Slice | Passed | Seconds |
| --- | ---: | ---: |
| Round four + report hygiene | 28 | 166 |
| Round two/three models | 87 | 53 |
| Round three native | 10 | 114 |
| Round two native | 16 | 174 |
| Core (all `test_core_*` except live and cutover reading) | 261 | 194 |
| Progress, status, review fixes | 73 | 216 |
| PR #124 recovery | 37 | 237 |
| Startup, cutover reading, reading, menu, conversation | 45 | 302 |
| Snapshots (all 40 scenes) | 3 | 171 |
| **Total** | **560** | |

**App protocol: 32 passed, 0 failed** in 9.5 s (`test_app_cutover_daemon`, `test_pr124_fixes`, `test_pr124_review_r2_fixes`, `test_desktop_ledger`) ([log](output/app-protocol.log)).

**`app/build.sh .review-tmp/product`: passed in 152.7 s.** 0 errors and 5 warnings: 4 in `UIModel.swift`, 1 in `UISearchPalette.swift`, unchanged files. `codesign --verify --deep --strict` passed. Bundle id `org.maxghenis.subfleet`. Not installed or launched. ([build](output/app-build.log), [signature](output/app-signature.log))

**Round-four reviewer's supplemental tests: 13 passed, 1 failed (OCR only).** The paginated `codex-write-stdin` card failed because Tesseract read `-lc` as `-le`. The render shows `/bin/zsh -lc 'python3 manage.py migrate'` intact, and the exact field-map assertion before it passed ([render](renders/r4-supplemental-codex-write-stdin-card.jpg), [log](output/r4-supplemental.log)). The Notion null visibility cases, 2/2, now pass.

**Round-five tests ([patch](round-five-tests.patch)):**

- Property test: 1,000 generated requests pass at head and fail at base; it kills 2 of 2 mutants.
- Placeholder-null regression: fails at head (the P2) and passes at base.
- The patch applies cleanly to a fresh export of head, and its test body is byte-identical to the executed file apart from the probe path.
- Two attempts to rerun the patched copy from that export hit the 570 s slice deadline. Machine load average was 85–137, and the session-scoped Swift probe compile never completed, so no test executed in those attempts ([log](output/r5-patch-on-head.log), [retry](output/r5-patch-on-head-retry.log)).

## Execution

- Every test, probe and build used `TMPDIR=$PWD/.review-tmp/`.
- Every slice ran in the foreground in its own process group under a deadline (≤ 570 s for tests, 900 s for the build), via [run_slice.py](probes/run_slice.py).
- Two commands outlived the tool's wait window and were moved to the background by the harness. Both were waited on in the foreground until they exited.
- After the patched-test retry timed out, one orphaned `swift-frontend` from it remained, because swift-driver gives frontends their own process group. It was terminated by exact PID after confirming its arguments named this job's scratch tree. The runner now also signals descendants by parentage.
- Nothing from this job is running.
- No sub-agents, live `~/.subfleet`, daemon, installed app or Application Support state were used. Every offscreen probe reported 0 visible windows.
- The Codex schema and types were generated with `CODEX_HOME` pointed at a scratch directory.
- Nothing was committed to the PR branch or pushed.
