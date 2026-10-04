# PR 124 routing-policy revert — 2026-10-04

Restored `subfleet/default_policy.json` byte-for-byte to `f832e6b8`. Restored the policy-only changes in `tests/unit/test_policy.py`, `tests/unit/test_policy_support.py`, `tests/frontend/test_core_store.py`, and `tests/frontend/test_core_draft.py` to the same revision. The fixture tree already matches that revision. The bundled policy again defines Astra, routes its hard Codex tiers to Astra, and retires `sol` into `astra`; explicit model pins and gate peers remain available. This supersedes the fleet-wide routing change in `59b166e5` without reverting its unrelated app fixes.

The daemon reads `self.daemon.policy` in `subfleet/conversations/service.py:278`, finds each chain's hard-tier entry at lines 300–306, and publishes its Codex choice in the existing `models.list.default_models` field. It publishes retirement metadata from that same loaded policy. Astra can be the daemon default only when a hard-tier chain explicitly routes to it. The read-only check of `~/.subfleet/policy.json` confirmed all five Codex hard-tier chains use `sol61` = `gpt-6.1-sol`, with Astra still defined; neither that file nor the running daemon was changed.

The app stores each provider's published default in `app/Sources/ConversationStore.swift:498`. `app/Sources/NewConversationDraft.swift:74` uses the published Codex default, resolves it to the provider's observed model value, and falls back to the first published non-retired model. Codex model memory cannot override that default; existing explicit draft picks remain selectable. Swift contains no hard-coded model identifier in this selection logic. Optional retirement metadata supports older daemons. The publisher regressions load two different policies and obtain two different answers, retain Astra as an explicit option, and reject an incidental Astra default.

Verification used the existing foreground helpers and the workspace Swift verification wrapper documented in `2026-10-03-app-cutover.md`. Test slices had 580-second deadlines. The 28 frontend slices finished in 7.64–199.75 seconds each (1610.76 seconds total); the combined daemon/status/policy slice took 75.20 seconds. No test or build command was detached.

| Suite | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| status-json | 54 | 2 | 0 |
| policy and policy-support | 162 | 0 | 0 |
| app-cutover-daemon | 13 | 0 | 0 |
| published-default/review daemon regressions | 9 | 0 | 0 |
| frontend, including app-protocol | 343 | 0 | 1 |
| explicit app-protocol rerun | 9 | 0 | 0 |

The focused review UI run also passed all 17 cases. Required suites cover 581 distinct passing cases, two failures and one skip; the explicit protocol and focused UI reruns are duplicates. The two failures (`test_c18_1_the_daemon_hands_the_timer_its_probe_records` and `test_c18_1_a_commit_inside_the_snapshot_after_its_rows_is_not_published`) both raise `InspectionError: macOS boot identity is unavailable` during daemon initialization, before policy loading. Both reproduce on an unchanged `c464ba8b` archive (two failures, 15.22 seconds). The existing native frontend process-identity check skips because the sandbox denies `ps`/sysctl access. These unrelated tests were neither changed nor bypassed.

`app/build.sh` passed in **580.38 seconds**, within its 900-second allowance, producing `build/pr124-evidence/policy-revert-product/Subfleet.app` with bundle ID `org.maxghenis.subfleet`. `codesign --verify --deep --strict` passed, and `otool -L` lists only system frameworks and system/Swift libraries. The app was neither installed nor launched. Existing Swift concurrency diagnostics and the verification wrapper's library-search-path warning remain.

The policy comparison `git diff f832e6b8 -- subfleet/default_policy.json` is empty; byte comparisons and SHA-256 checks passed for all five restored files. Full logs and compiler/test child records remain in ignored `build/pr124-evidence/` and `build/app-cutover-evidence/`; concise results are in `2026-10-04-app-cutover-policy-revert-evidence.json`.

The final child-record audit matched **283 compiler starts/completions** and **2907 test-child starts/completions**, with no unfinished records. Every foreground command was awaited; no task-owned process was left running.

Delivery uses workspace-local branch `fix/app-cutover-policy-revert` in `.git-local`, because the sandbox rejected writes to the shared Git index. Implementation commit: `aac3a93abf14a6368abb2d3666c63d9ccb9ea298`. Every delivery commit has the requested `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` trailer. Nothing was pushed.

Bundle: `docs/reports/2026-10-04-app-cutover-policy-revert.bundle`; head: `refs/heads/fix/app-cutover-policy-revert`; prerequisite: `c464ba8b30c4`. The bundle is a separate artifact; the report and concise machine-readable verification evidence are committed.
