# PR #127: continuation round-two fixes

Base: `313c8671198a77d3a1913b1f3136793bc6e7e0e1`, which already includes the release/217 merge and the `.gitignore` conflict resolution. Work stayed in the assigned workspace. No live state, installed app, Application Support files, default policy or external PR was changed. No subagents were used and every command ran in the foreground.

The full review, evidence REVIEW.md, strand turn logs and JUnit summaries were read. The repro patch applied cleanly and was committed unchanged as `091c4ed4`. Afterwards, its timer fixture was updated from 310 seconds after turn creation to 480 seconds: with settlement at +180 seconds, this leaves the newly required full five minutes. The three print-only P3 scenarios now assert the required refusal, timer rejection and pruned-run wake. The original request, notice and timer tests were updated to the restored contract, without changing catalog stubs.

| Finding | Change | Proof |
| --- | --- | --- |
| P1-1, catalog | `subfleet/conversations/wakes.py:187` starts the engine when the job store is available and defers activation otherwise. Historical activation snapshots remain intact. | All eight unchanged `test_the_fence_stays_above_the_standard_streams_whatever_the_service_has_closed` variants passed on retry. |
| P1-1 / P1-2, wait | `subfleet/cli.py:1290` no longer acknowledges terminal receipts. Only explicit `notice.ack` and `runs show` acknowledge. | `test_wait_preserves_notices_for_finished_results_it_returns` (success and failure), `test_wait_receipt_preserves_the_conversation_wake`; real strand and milestone-1 tests attempted but blocked by sandbox process inspection. |
| P2-1 | `subfleet/conversations/wakes.py:429` satisfies only the runs kind when all its results were delivered. | `test_new_stale_runs_on_a_combined_line_cancel_its_other_triggers` passes for timer and PR alternatives; both receive the second wake. |
| P2-2 | `subfleet/conversations/wakes.py:257` carries the latest fired PR request's snapshot, and `:300` uses turn-job creation as the event threshold. An observation that was never delivered is excluded from the baseline. | `test_new_ci_result_for_a_fix_pushed_during_the_woken_turn_is_announced` gets two wakes; `test_first_pr_watch_keeps_events_between_turn_creation_and_settlement` and `test_rearm_does_not_baseline_an_undelivered_mid_turn_pr_event` cover the timestamp and undelivered-baseline paths. |
| P2-3 | `subfleet/conversations/store.py:457` supplies a read guard; `subfleet/conversations/wakes.py:210` paces evaluation, and `:389` reads eligibility and batches target/receipt/lease reads in chunks of at most 500. | `test_idle_control_ticks_do_no_writes_and_batch_target_reads` covers both 40-conversation cases, rejects transactions in either store, and bounds target reads independently of run count. Measurements below. |
| P3-1 | `subfleet/conversations/wakes.py:252` refuses previously refused PR targets using durable per-conversation records, without another wake. | Overnight repro: one refusal turn instead of 23; `test_pr_refusal_survives_restart_and_identical_retry` proves persistence and idempotent retries. |
| P3-2 | `subfleet/conversations/wakes.py:300` validates against the later of now and turn-job creation. | Four boundary cases at -1, 0, 299 and 300 seconds, plus the capacity-wait past-timer repro. |
| P3-3 | `subfleet/conversations/wakes.py:419` represents an absent registered run as terminal `pruned`. | `test_new_pruned_target_of_an_all_of_request_strands_the_others` gets one wake containing `quick finished: pruned`. |
| PR body / contracts | `docs/reports/2026-10-04-continuation-pr-body.md` contains this round's measured results and validation limits. C-15.3, C-24.10 and design D-28 describe receipt-only waits, kind resolution, PR thresholds/refusals, timers, pruning and idle evaluation. | The existing PR-body wording test remains in the wake/review slice. The hub can post the prepared body; no PR edit was made. |

Hypothesis coverage retains at-most-once requests, held/archived/person-queued guards, throttle and person FIFO properties, plus unchanged-PR watches and the moot-block differential property. The new request-resolution property generates combinations of run delivery, termination, pruning, due timers and PR readiness. It requires each silent satisfaction to have delivered run evidence, each fired kind to name its accepted message, unresolved alternatives to remain pending, and no more than one wake per request.

## Idle measurements

Python 3.12.14. The paired measurement loads WakeEngine from `313c8671` and the fixed source into the same process over the same isolated stores. Five alternating samples of twenty forced ticks exclude setup and warm-up. The paced result drives 100 production control ticks over five simulated seconds. The timer-only reader is the real store long-poll with its own 0.5-second periodic check, measured in three one-second bursts per engine; each thread is joined on completion.

| Case | Before | After |
| --- | --- | --- |
| 40 conversations × 16 running runs + 6-hour timer, forced evaluation CPU | 17.810 ms/tick | 3.264 ms/tick |
| Same, forced evaluation wall time | 19.669 ms/tick | 3.501 ms/tick |
| Same, production pacing CPU / wall per control tick | Before evaluated requests every tick | 0.141 / 0.180 ms |
| Idle write transactions / notifications | 40 per evaluation (800/s at 20 Hz) | 0 |
| 40 conversations × 6-hour timer, forced CPU / wall | 1.307 / 1.401 ms/tick | 0.971 / 1.058 ms/tick |
| Timer-only long-poll predicate checks per second | 9,089; 6,326; 8,775 | 2; 2; 2 |

The review reported 20–23 ms CPU and 36–50 ms wall for the first case, and 51–14,460 reader checks/s for the second. Our initial unpaired Python 3.12 repro measured 12.2 ms CPU / 13.7 ms wall and 17,522 checks/s. Host scheduling changes the wall time and notification amplification considerably; the paired figures above isolate the change. The direct reviewer repro after the fix also records zero transactions and two reader checks/s. Startup, a real request-state change and outstanding notice repair still write; a steady idle evaluation does not.

## Validation

| Slice | Passed | Failed | Skipped / excluded | Wall time |
| --- | ---: | ---: | --- | ---: |
| Wake/review unit slice (10 files, including 31 reviewer repros and 10 new cases) | 115 | 0 | 0 | 86.15 s |
| Catalog + lifecycle | 35 | 0 | 1 explicitly excluded after sandbox failure | 181.37 s |
| Notice rendering + operator notices | 100 | 0 | 0 | Part of 11.84 s slice |
| Desktop ledger | 4 | 0 | 0 | Same slice |
| MCP (scope, adapter, launch, CLI, scheduler) | 62 | 0 | 0 | Same slice |
| Conversation service | 91 | 1 | Real-daemon boot identity unavailable | Part of 155.12 s slice |
| Conversation store/state/status/reconcile/FIFO | 121 | 0 | 0 | Same slice |
| Strand e2e | 0 | 0 | 2 sandbox skips | Part of 6.96 s slice |
| Real wake e2e | 0 | 0 | 4 sandbox skips | Same slice |
| Milestone-1 Codex | 0 | 0 | 9 sandbox skips | Same slice |

The five Hypothesis properties in the wake/review slice all pass, with their existing generation budgets retained. This is targeted validation, not a claim about the entire repository suite. All Python slices ran under ten minutes; logs, JUnit XML, benchmark output and build output remain under `/tmp`, not in git.

The sandbox denies `/bin/ps` and macOS boot-identity inspection. The catalog cleanup test failed on denied `/bin/ps`; it was explicitly excluded from the final catalog slice, with no test/stub change. An initial fence variant timed out and passed on retry. A conversation-service test constructing a real daemon failed before reaching its assertion because boot identity was unavailable. Real e2e tests skip through their unchanged process-inspection fixture, so this workspace cannot prove the two strand cases, four real wake cases or nine milestone-1 cases. No skipped test is counted as passed.

The review's flaky Claude conversation e2e and saved steer Hypothesis counterexample on f832e6b8 were not changed. Their fixes remain out of scope; neither was claimed to be fixed or revalidated here.

## Mutation checks

All seven valid mutants were killed by behavioral test failures (not collection errors or skips):

| Mutant | Proving test | Failed / selected |
| --- | --- | ---: |
| Restore wait acknowledgement | `test_wait_preserves_notices_for_finished_results_it_returns` | 2 / 2 |
| Satisfy every kind for delivered runs | Combined-line timer and PR repros | 2 / 2 |
| Copy an undelivered PR observation as baseline | `test_rearm_does_not_baseline_an_undelivered_mid_turn_pr_event` | 1 / 1 |
| Discard the last fired PR baseline | Mid-turn CI-result repro | 1 / 1 |
| Open a write transaction for eligibility | Both idle-control cases | 2 / 2 |
| Validate timers only against old turn creation | Timer remaining-time boundaries | 3 / 4 |
| Treat a missing target as still running | Pruned all-of target repro | 1 / 1 |

Every mutation was restored in a `finally` block. The implementation files and temporary pytest configuration were compared byte-for-byte with the committed head afterwards. The temporary configuration was removed; subsequent slices explicitly use their own `/tmp` base directory.

The optional admission-liveness property was interrupted at the foreground bound: 65 prior fake cases passed in 536.32 seconds (8m56s), comprising wakes 4, notice delivery 15, notice headers 23 and deterministic admission liveness 23. The unfinished generated admission property is not counted as passed. The remaining fake files plus the fake MCP job contract passed 33 cases and skipped 5 process-inspection cases in 92.33 seconds. Across both fake slices: **98 passed, 5 sandbox skips, 1 interrupted property**.

Remaining fake slice breakdown: test_conversation_pools: 2 passed, 0 skipped; test_idle_cost: 0 passed, 2 skipped; test_lost_acknowledgement: 0 passed, 3 skipped; test_turn_listing: 9 passed, 0 skipped; test_turn_trees: 10 passed, 0 skipped; test_mcp_job_contract: 12 passed, 0 skipped.

The restored source passes all 13 mutation-target cases in 46.47 seconds. Across the final unit and fake selections, **626 distinct cases passed**; there was one sandbox-caused unit failure, 20 sandbox skips, one explicitly excluded catalog cleanup case and one interrupted admission property. This total does not count repeats or intentional mutant failures.

## App build and delivery

`app/build.sh /Users/maxghenis/.subfleet/worktrees/20261005-080631-pr127-fix-r2/build/continuation-r2` succeeded in **278.45 seconds**. Its plist was validated and its bundle signed. Module caches were kept under workspace `build/module-cache`, and the build script's documented `SUBFLEET_SWIFT_NESTED_SANDBOX=off` opt-in was used for compiler plugins inside the outer sandbox. The build emitted Swift compiler warnings; the app was neither installed nor launched.

Commits use the requested Claude Opus 5.5 co-author trailer. Implementation commits: `091c4ed4`, `f09a3077`, `0ae35bba`, `3f6fa755`; the final documentation commit follows them. Shared metadata was read-only, so commits live in workspace-local `.git-local` on `feat/conversation-continuation`. The deliverable is `docs/reports/2026-10-05-continuation-r2.bundle`, with prerequisite `313c8671198a77d3a1913b1f3136793bc6e7e0e1` and head named `refs/heads/feat/conversation-continuation`; `git bundle list-heads` gives its exact commit. The bundle is saved without committing it or `.git-local`. Nothing was pushed. All commands we started have finished.
