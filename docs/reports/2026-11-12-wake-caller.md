# Wake quiet callers (D-WAKE-CALLER1, package 7)

Implemented durable, bounded completion wakes for quiet ordinary callers, with hook exclusion and restart-safe delivery records.

Started 2026-11-12T05:49:38Z (`date -u`, first command). Base: b51f08f961e1b1dcfb93347eb080f9ddd9a9260d. All work is in `/Users/maxghenis/.subfleet/worktrees/20261111-122601-wc-build-r1s`. No live state, live inbox or live process was inspected.

## Step 0 (recorded before implementation)

- `hooks.session_event` reads `pending`/`offered` rows, prints them, then calls `notice.mark` with `surfaced` and `hook:SessionStart`/`hook:UserPromptSubmit` (`hooks.py:482–515`). The daemon advances the monotone ladder `pending → offered → surfaced → acknowledged`; only pending/offered rows are returned to hooks (`daemon.py:4383–4407`). The read/print/mark gap needs fencing against a concurrent wake.
- `nudge.decide` checks turn classification, retirement, restart source, maximum age, interruption dedupe and cooldown (`sessions/nudge.py:176–232`). Quietness is checked in `sweep`, by comparing transcript fingerprints before/after sampling and applying the manual quiet window (`:355–381`). `TurnState.age_s` rounds seconds; a precise gate must use its timestamp. Registry busy/idle timestamps can supplement transcript activity.
- The task's description of `spawn` is stale in this branch: it launches a detached CLI worker (`sessions/nudge.py:145`); `sweep`/`wake` call `sessions.ping`, which parks a service notice. No daemon call currently invokes `notify_push`. The usable inbox transport is `notify_push.push_to_session` (`notify_push.py:284–332`). It resolves the stable session id afresh, needs a live registry pid, Unix inbox socket and published peer token, and never starts a session. Missing/dead sessions fail safely; closed sessions cannot be reached and will be recorded undeliverable. Temporary missing inbox/token failures can be retried.
- `conversation.wake` / `op_conversation_wake` refuses `no-conversation` unless the caller is bound to an app conversation or names a conversation turn (`conversations/service.py:977–986`). It cannot serve an ordinary unbound Claude Code caller. `WakeEngine` persists requests in `wake_requests`, uses `MAX_STREAK=8` and `COOLDOWN_S=1800`, and resets unattended streak on personal input. This change will reuse those bounds and exclude `_conversation_binding` matches (including turn attempts and canonical UUID spellings).
- `nudge.caps` currently contains time bounds but no session-count cap; one must be added for bounded passes. `nudge.enabled` honors `SUBFLEET_TICKLE`; `source_allows` accepts only absent/startup/resume sources.
- SQLite and the inbox cannot commit atomically and the inbox supplies no acknowledgement/dedupe id. A durable pre-send claim is required for at-most-once sending. An interrupted/ambiguous send must remain consumed and be recorded undeliverable rather than be sent again. Explicit failures before any bytes are sent may retry. This follows the existing `sessions.client.record_nudge` ordering.

## Environment deviations discovered before implementation

- The requested `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"` resolves to `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/`; creating scratch directly there is sandbox-denied. Initial checks used writable `/private/tmp/sf-wc-b1`, but job-isolation checks correctly refuse that shared temporary root. The writable Darwin job root is `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261111-122601-wc-build-r1s-a1-uwzfonyi/sf-wc-b1`. Final checks set `TMPDIR` there, `UV_CACHE_DIR=$TMPDIR/uv-cache`, and a distinct basetemp for every run. The isolated venv, logs and module caches remain in `/private/tmp/sf-wc-b1`. Both scratch roots will be cleaned with the required guarded deletion.
- `uv sync --group dev --extra api` fails because this branch defines no `api` extra. Retry with `uv sync --group dev`; install the required pytest-xdist separately in the isolated venv.
- Git commands inheriting global config stall. Using `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1` resolves `rev-parse`; no persistent git config is changed.

## Changes

- Schema 11 adds notice-local `wake_state`, `wake_attempts`, `wake_next_at`, `wake_at` and `wake_reason`, plus `caller_wake_sessions` for activity, source, streak and cooldown. Columns were chosen over event-only bookkeeping so checking unread state and claiming delivery share the same notice update; a reused notice id cannot inherit an old delivery event. The numbered additive migration preserves old rows and matches a fresh store (`subfleet/store.py:46`, `:135`; `subfleet/store_schema.sql:220`, `:238`, `:242`). Historical schema/event/table-set comparisons were updated.
- `CallerWakeEngine` scans unread non-turn job notices, groups due notices by caller, uses the shared nudge eligibility/caps, excludes app conversations, and sends only `render.notice_header` through the existing inbox transport (`subfleet/caller_wakes.py:69`). The daemon offers its paced worker after startup recovery and every second thereafter (`subfleet/daemon.py:5787`). Session identity is resolved afresh, including after account switches.
- Claims commit before sending. Successful delivery remains `surfaced` and records `delivered`; an interrupted or ambiguous send becomes consumed/undeliverable rather than risking a duplicate. Proven failures before any bytes are attempted, including a failed socket connection, restore the unread notice and retry with durable 2–300 second backoff, expiring 24 hours after notice creation (`subfleet/notify_push.py:61`, `:304`, `:363`). Connection failure details are not persisted; the stored reason is `inbox-unavailable`. Closed/unregistered sessions are undeliverable and no new session is started.
- Online prompt/start hooks opt into an atomic surface claim, preserving plain `notice.pending` and acknowledgement payloads (`subfleet/protocol.py:304`, `subfleet/hooks.py:496`). Offline hooks use a shared per-session lock, durable ids/fingerprints, and a separate activity/source/human-input timestamp spool, including when there are no newly printable notices (`subfleet/notice_claims.py:61`, `:87`, `:112`). Reconciliation runs before a wake claim; it preserves newer activity/source records and never clears bounds belonging to a newer wake (`:138`). Corrupt evidence holds only its own caller. Malformed hook replies retain the established list-of-dictionaries filtering. Claim-producing requests use the request pool and are not cancellable queued reads (`subfleet/descriptors.py:139`).
- `sessions.notice_wake_enabled=true`, `sessions.notice_wake_quiet_s=300`, and `sessions.nudge_max_sessions=8` are validated policy data and exposed in the bundled policy. `nudge.notice_decide` honors source, retirement, environment switch, precise activity, existing nudge cooldown and the imported conversation bounds (`subfleet/policy.py:105`, `subfleet/sessions/nudge.py:238`). Transcript microseconds are preserved (`subfleet/sessions/transcripts.py:225`). All live registry instances contribute activity, including busy siblings (`subfleet/notify_push.py`).
- Added C-15.9 by hand and clarified C-16.5's distinction between plain reads and surface claims (`docs/acceptance-contract.md:607`); no clauses were renumbered.

## Invariants and tests

All six are covered in `tests/fake/test_caller_notice_wakes.py`:

1. **Once:** no notice is woken twice across passes and daemon restarts. Hypothesis: `test_c15_9_once_over_generated_finish_hook_failure_and_restart_histories`.
2. **Never both:** a hook-shown notice is never woken; a woken notice is never shown again as new. Hypothesis: `test_c15_9_never_both_over_generated_finish_hook_failure_and_restart_histories`; explicit online callback and offline-receipt races also covered.
3. **Quiet only:** a session active within the gate is never woken. `test_c15_9_quiet_only_never_wakes_a_recently_active_session`, precise transcript/registry activity cases in `test_sessions_notice_wake.py`, and `test_c15_9_offline_prompt_activity_holds_a_new_notice_until_the_gate` in `test_notice_claims.py` cover online and offline activity.
4. **Eventually:** an eligible pending notice for a reachable quiet caller is delivered in the next pass after its gate/retry becomes due, within the requested caps and cooldowns. Hypothesis: `test_c15_9_eventually_over_generated_finish_hook_failure_and_restart_histories`.
5. **Scrubbed:** the wake contains exactly header fields and no output or notice summary. `test_c15_9_scrubbed_wake_uses_header_fields_never_notice_or_output_text`.
6. **One path:** a conversation-bound caller gets no wake from this feature. `test_c15_9_one_path_conversation_bound_session_is_left_to_wake_engine` uses a real stored conversation binding.

Generated histories include finish/activity times, hook reads, inbox failures, repeated passes and actual SQLite close/reopen. Additional tests cover backoff/expiry, turns, off switches, caps, source checks, streak/cooldown, lost send receipts, notice id reuse, corrupt receipts and failed receipt writes. Offline activity tests cover empty and already-receipted hooks, compact/clear sources, personal-input resets retained through a later restart, stale spool records, and corrupt activity timestamps. Transport tests distinguish proved connect failures from ambiguous send failures.

## Mutants

All three mutant passes, including the final pass after the identity correction, killed all six mutants with assertion failures: 6/6 in each pass. Each mutant was applied alone, restored from pristine bytes, and checked by SHA256 before the next mutation. Test names below have prefix `test_c15_9_` in the fake caller test file.

| Mutant | Killer |
| --- | --- |
| Wake a notice twice | `delivered_notice_survives_restart_without_replay` |
| Wake a hook-shown notice | `hook_shown_notice_never_wakes` |
| Drop the quiet gate | `quiet_only_never_wakes_a_recently_active_session` |
| Lose the delivery record on restart | `delivered_notice_survives_restart_without_replay` |
| Include output text | `scrubbed_wake_uses_header_fields_never_notice_or_output_text` |
| Wake a conversation-bound caller | `one_path_conversation_bound_session_is_left_to_wake_engine` |

Exact patches, hashes, commands and results are retained in the report data directory.

## Verification status

Final focused checks passed in separate file invocations: 35 fake caller tests, 49 eligibility/registry/routing helper tests, 24 offline receipt/activity tests, 7 notice migration tests, 84 hook tests with one skip, and 33 transport tests. The required guard set (`test_state_contract.py`, `test_test_module_names.py`, `test_invariants_index.py`, `test_handler_refusals.py`) passed all 425 tests, including the contract index. Earlier separate targeted checks also passed nudge 44, transcripts 71, QoS migration 13 and general migrations 21.

The initial combined affected-file run finished with **14,308 passed, 108 failed, 115 skipped and 8 xfailed in 3,258.50 seconds**. Seven representative checks against the unchanged base reproduced its seven failure groups; [the baseline summary](2026-11-12-wake-caller-data/baseline-clusters-summary.json) and [source identity proof](2026-11-12-wake-caller-data/baseline-source-proof.json) preserve the comparison. Of those failures, 99 were shared temporary-root guard refusals addressed by the writable Darwin job root. Eight require native sandbox/task-policy capabilities unavailable in this nested sandbox and are explicitly deselected from the final run. The canonical-identity assertion also reproduced at the base under the old temporary root; it remained selected and passed under the Darwin job root.

The final combined run selected all 215 files naming `_notice`, `notices`, `nudge`, `hooks` or `caller_session`, in one `-n 4` invocation: **14,427 passed, 115 skipped, 8 xfailed in 1,934.05 seconds (32m 14s), exit 0**. It ran on `e6e47054a`, before the final narrow identity fence described below; that fence receives a separate complete caller-file run and the required guards. The whole suite was not run.

A late review found that retention could delete a claimed notice during socket I/O and SQLite could reuse its id for another caller. The two new success/failure cases failed against the old code, proving that its transport result could overwrite the replacement. Post-I/O updates now match the full notice identity and claim timestamp (`subfleet/caller_wakes.py:223`, `:238`); `test_c15_9_late_transport_result_never_changes_a_reused_notice_id` covers both outcomes. The complete caller file then passed all 35 tests, and the six mutants were killed again with pristine restoration. The last required guard run then passed all 425 tests in 26.70 seconds, including `test_invariants_index.py`.

Initial failures were retained, corrected and rerun: test job-id prefix matching, conversation fixture settings, the hook fixture's missing creation timestamp, corrupt receipt validation, unordered migration events and the refusal-template literal. Later focused checks cover durable offline activity, malformed hook reply filtering and safe connection retries. No failed result is counted as a pass.

## Commits, commands and cleanup

The shared index rejected writes (`index.lock: Operation not permitted`). The repository explicitly documents an ignored `.git-local` fallback, so commits use isolated metadata on `feat/wake-caller-217`, with the base repository's objects as a read-only alternate. That metadata was moved from workspace `.git-local` to `/private/tmp/sf-wc-b1/git-local` for cleanup; the original `.git` pointer is unchanged. Coherent commits: `f87769aa7` audit, `8f475562f` schema, `7435b519e` delivery, `e6e47054a` offline activity and safe retry corrections, and `d579f82eb` late-result identity fence. This report and its evidence follow in their own commit. The incremental [delivery bundle](2026-11-12-wake-caller-data/delivery.gitbundle) preserves the feature branch from the stated base; [delivery-receipt.json](2026-11-12-wake-caller-data/delivery-receipt.json) records its verified final head, commands, completion time and cleanup. No push or history rewrite occurred.

The implementation diff and changed line ranges are in [implementation.patch](2026-11-12-wake-caller-data/implementation.patch) and [change-lines.txt](2026-11-12-wake-caller-data/change-lines.txt). The complete command/result inventory is in [root-commands.txt](2026-11-12-wake-caller-data/root-commands.txt), the agent ledgers ([schema](2026-11-12-wake-caller-data/schema-commands.txt), [caller tests](2026-11-12-wake-caller-data/caller-tests-commands.txt), [eligibility](2026-11-12-wake-caller-data/nudge-commands.txt), [receipts](2026-11-12-wake-caller-data/notice-claims-commands.txt), [offline activity](2026-11-12-wake-caller-data/offline-activity-commands.txt), [review](2026-11-12-wake-caller-data/final-review-commands.txt)), and [runs.json](2026-11-12-wake-caller-data/runs.json), which records every test invocation and its archived log. Exact-shape searches and the selected regression filenames are also retained. Exploratory reads of nonexistent proposed module paths returned errors and were corrected. One exploratory `head` printed git executable bytes while checking whether it was a wrapper; those bytes were unused, and no live state or credential file was read.

The combined run excludes `real_lsof` and `real_cwd` markers to honor the prohibition on live process inspection. The isolated runner uses the writable Darwin job-specific `sf-wc-b1` root for `TMPDIR`, uv cache and fresh test basetemps; failed-test evidence, the venv, logs and Python/Swift caches stay under `/private/tmp/sf-wc-b1`. Generated workspace folders removed were `.pytest_cache`, `.hypothesis`, `build`, `subfleet/__pycache__`, `subfleet/adapters/__pycache__`, `subfleet/sessions/__pycache__`, `subfleet/conversations/__pycache__`, `subfleet/gate/__pycache__`, `subfleet/guard/__pycache__`, `tests/__pycache__`, `tests/unit/__pycache__`, and `tests/fake/__pycache__`. [workspace-cleanup.json](2026-11-12-wake-caller-data/workspace-cleanup.json) records their absolute paths. Each was checked against the tracked-file inventory, moved into `sf-wc-b1/workspace-caches`, then removed with `shutil.rmtree` guarded by an assertion that `sf-wc-b1` is in the path. The former workspace `.git-local` directory was likewise moved into scratch. The final delivery receipt records guarded removal of both complete scratch roots, including the temporary Git metadata, after bundle verification. Report artifacts and the bundle are retained; no background work remains. Completion is within the three-hour budget.

## Fix r1

This round fixes review r1's findings on #307 (`~/reviews/subfleet-hub/wake-caller-2026-11-11/review-r1.md`), following the hub's rulings in the brief. One headless job did the work, from 05:05Z to about 07:45Z on 2026-11-15, in a detached checkout of `feat/wake-caller-217` at `897fb95b7`. It used no subagents and no workflows, because the brief bars `subfleet` against live state. Scratch was `$TMPDIR/sf-wcall-f1`, with `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`. The measurement script, its results, the mutant driver, the mutant results and the clean-up script are in [fix-r1](2026-11-12-wake-caller-data/fix-r1/). Line numbers below are at `1f52e85f0`.

### Merge

`039c6f169` merges `origin/release/217` at `c8b61c95f4f5772e3ce8c85365b4335e07309c34` into `897fb95b7`, in a commit of its own. It brings in 229 files (+34,728, −266) without conflicts: `git show --cc` of the merge is empty.

### Findings and changes

1. **P1: an `offered` notice was woken.** The scan, the recheck and the claim now take only `pending` notices (`subfleet/caller_wakes.py:261-265`, `_current` at `:214`), and the partial index matches (`subfleet/store_schema.sql:238`). Once `wait_jobs` has printed terminal results, it calls `_offer_waited` (`subfleet/cli.py:1607-1608`, `:1896`). That function marks the caller's own `pending` notices for those jobs `offered`, with transport `wait`, through `notice.mark`. It marks nothing inside a process Subfleet launched. Every command that prints through `wait_jobs` does this: an attached `run` (`cli.py:1121`), an attached `run --batch` (`:1388`), `wait` (`:1642`) and `kill --wait` (`:2190`). Probe A1 is now `test_c15_9_a_notice_posttooluse_offered_is_never_woken` (`tests/fake/test_caller_notice_wakes.py:658`). Probe A2 is `test_c15_9_a_result_wait_printed_is_never_woken[ordinary|subfleet-launched]` (`:680`). C-15.3 and C-15.9 now say so. Deviation 1 explains why `wait` marks the notice `offered` rather than acknowledged.
2. **P1: the wake's own prompt reset the streak.** `notify_push.is_own_envelope` (`subfleet/notify_push.py:292`) accepts only the exact envelope Subfleet writes. For UserPromptSubmit, the hook sends `own_envelope` (`subfleet/hooks.py:494-497`, `:507`; `subfleet/protocol.py:311-319`). The daemon resets `wake_streak` and `last_wake_at` only for personal input (`caller_wakes.py:105`). The offline path does the same: `record_offline_activity(..., personal=)` sets the personal-input time only for a personal prompt (`subfleet/notice_claims.py:88-99`, `:118-120`; `hooks.py:522`). Probe B and its control are `test_c15_9_the_wakes_own_prompt_leaves_the_streak_bound_in_force[wake-prompt|no-hook-control]` (`:728`). Over eleven finish-and-quiet cycles, both give `MAX_STREAK` (8) wakes and then none. `test_c15_9_a_persons_prompt_still_resets_the_streak` (`:738`) checks that a person's prompt still resets the streak (eleven wakes). `test_c15_9_an_offline_wake_prompt_is_activity_but_not_personal_input` (`tests/unit/test_notice_claims.py:228`) covers the offline path. C-15.9's `MAX_STREAK` sentence is now true: at most `MAX_STREAK` wakes land before each `COOLDOWN_S`.
3. **P1: month-old notices were woken.** On a notice's first attempt, if the notice is at or past the 24 h window, the engine records `wake_state='stale'` with reason `older-than-retry-window`, in one `notice.wake_stale` transaction, and never wakes for it (`caller_wakes.py:42`, `:228`, `:266-273`). A notice that has had attempts and is past the window still ends `undeliverable`/`retry-expired`. The migration step that adds `notices.wake_state` marks every `pending` or `offered` notice stale, with reason `before-migration` (`subfleet/store.py:49`, `:454-460`). The code finds that step by the column it adds, so the backfill survives renumbering. The wake-state CHECK accepts `stale` (`store.py:140`, `store_schema.sql:230`). A stale notice keeps its state, so the hooks still show it. Probe C is now `test_c15_9_a_notice_older_than_the_window_is_stale_on_its_first_attempt` (`:743`). Probe E is now `test_c15_9_a_backlog_never_wakes_for_stale_notices_and_wakes_at_most_the_cap_per_pass` (`:764`). Its store has 96 callers (32 of them live), 97 old notices and 32 fresh ones. Each pass sends at most `max_sessions` (8) wakes, with 16 in all, and only for the live callers' fresh notices. The other 16 fresh notices are left retrying. `test_d_wake_caller1_notice_delivery_is_a_numbered_additive_migration` (`tests/unit/test_store_notice_wake_migration.py:37`) checks the backfill.
4. **P2: a lane refusal consumed the notice.** `notify_push.LANE_REFUSAL` names the refusal's fixed words (`notify_push.py:54`, `:369`). It is now in `PREFLIGHT_FAILURES` (`caller_wakes.py:25`), and the recorded reason keeps only those fixed words (`:389-390`). The notice goes back to unread and retries on the backoff. Probe D is now `test_c15_9_a_lane_refusal_leaves_the_notice_for_the_hook` (`:805`), which drives the real transport with the lane markers set.
5. **P2: a late surface claim marked notices.** The hook sends `deadline_s`, which is its client timeout less `SURFACE_REPLY_MARGIN_S` (2 s) (`hooks.py:508`, `:646`; `protocol.py:320`). The daemon passes `arrived` and `client_gone` to `surface` (`subfleet/daemon.py:4368`). Some claims mark nothing: those whose client has gone, and those that run `deadline_s` or more after the daemon read the request. Such a claim still records the activity and an audit event, `notice.hook_abandoned` (`caller_wakes.py:74`, `:129-131`). `test_c15_9_a_surface_claim_after_its_client_gave_up_marks_nothing` (`:829`) uses a hook client that times out after 15 s and prints offline. A claim that arrived 15 s earlier, with its 13 s deadline, marks nothing. So does one whose client is gone. An in-time control marks the notice. C-16.5 now says so. See deviation 2.
6. **P2: an app restart made notices undeliverable.** A missing registry row or a dead pid now retries on the backoff (`caller_wakes.py:308-311`). After 24 h, the same expiry marks the notice `undeliverable`. `test_c15_9_absent_or_closed_session_is_undeliverable_without_starting_it` (`:458`) now checks the retry, and then that the notice is undeliverable at the 24 h end and that nothing has started. `test_c15_9_a_caller_back_from_an_app_restart_is_woken_within_the_window` (`:859`) wakes a caller that registers again inside the window.
7. **P2: compaction held completion wakes.** `nudge.NOTICE_WAKE_SOURCES` adds `compact` to the restart sources, and `notice_decide` now uses `notice_source_allows` (`subfleet/sessions/nudge.py:151-156`, `:267`). `clear` still holds. `test_c15_9_compaction_keeps_completion_wakes_and_clear_holds_them[...]` (`:877`) covers `startup`, `resume`, `compact` and `clear`. The offline source test (`test_notice_claims.py:215`), the unit source rule (`tests/unit/test_sessions_notice_wake.py`) and the off-switch test (`:500`, now with source `clear`) follow the rule. C-15.9 now says so.
8. **P2: passes were unbounded.** One pass examines at most `MAX_GROUPS_PER_PASS` (32) callers. Each pass starts after the last caller the previous one examined, so every caller is reached (`caller_wakes.py:32`, `:279-287`). The engine memoizes each caller it finds held, together with what it judged: the notice ids, the registry row's fields, the wake-clock row, and the transcript's inode, size and mtime. Until one of those changes, the caller's earliest time gate lapses, or `HELD_RECHECK_S` (60 s) passes, the caller is not judged again and its transcript tail is not read (`:36`, `:188`, `:205`, `:312-322`). Busy and invalid-activity holds are decided before the transcript facts are read (`:172-175`). The tests are `test_c15_9_a_held_caller_is_not_read_again_until_something_changes` (`:890`) and `test_c15_9_a_pass_examines_a_bounded_number_of_callers_and_reaches_them_all` (`:923`, with the bound patched to 3). The measurement is below.
9. **P3: the change log.** Two lines now head "Changes in version 3" (`docs/acceptance-contract.md:9-10`). One records this round. The other records the original C-15.9 clause and the C-16.5 amendment. Both follow the file's form, with no blank line inside the list.

r1's other P3s:
- **Rendering after the claim (r1's `caller_wakes.py:212-213`): fixed.** Headers are now rendered before the claim. A render failure retries as `header-unavailable` and claims nothing (`caller_wakes.py:340-349`). The test is `test_c15_9_a_header_that_cannot_render_leaves_no_claim_behind` (`:950`).
- **The push frame's missing `priority` (pre-existing): unchanged.** The brief gave no ruling on it, and the transport is shared.

### Measurement (finding 8)

[`test_measure_probe_e.py.txt`](2026-11-12-wake-caller-data/fix-r1/test_measure_probe_e.py.txt) builds r1's probe E store: 96 callers, 32 of them registered and quiet, and 744 unresolved notices. It then runs eight passes, 3 s apart. It runs from `git archive` exports of `039c6f169` (before) and `1d0d21680` (after; `subfleet/` is unchanged from there to `1f52e85f0`). It counts calls to `transcripts.turn_state` and `notify_push.find_session`. The rows are in [probe-e.jsonl](2026-11-12-wake-caller-data/fix-r1/probe-e.jsonl). The load was 79 to 99.

| Store | Tree | Passes that wake | Per waking pass | Transcript reads | Registry reads | After 8 passes |
| --- | --- | --- | --- | --- | --- | --- |
| r1 probe E (every notice 1 to 30 days old) | before | 1 to 4 | 8 wakes, 64 headers | 16 a waking pass | 16, 46, 33, 29, then 4 | 256 delivered, 488 undeliverable |
| r1 probe E | after | none | none | 0 | 0 | 744 stale after pass 1 (0.089 s) |
| Fresh backlog (10 to 48 min old) | before | 1 to 4 | 8 wakes, 64 headers | 16 a waking pass | 80, then 16 | 256 delivered, 488 undeliverable on pass 1 |
| Fresh backlog | after | 3 to 6 | 8 wakes, 64 headers | 16 a waking pass, else 0 | 32 every pass | 256 delivered, 488 retrying (`session-not-registered`) |
| Held steady (live callers just active) | before | none | none | 32 every pass | 96, then 32 | 256 pending |
| Held steady | after | none | none | 32 on pass 3 only | 32 every pass | 256 pending |

On the held store, one pass took 0.19 to 2.28 s before and 0.07 to 0.87 s after. The wall-clock figures are noisy at this load; the read counts are not.

Since finding 6, unreachable callers retry, and they now share the 32-caller budget. In the "after" runs, passes 1 and 2 went to the 64 gone callers (in sorted order, they come before the live ones), so live callers' wakes start at pass 3. The rotation then reaches them, and the backoff (2 s, rising to 300 s) thins the gone callers' turns.

### Mutants

[`run_mutants.py.txt`](2026-11-12-wake-caller-data/fix-r1/run_mutants.py.txt) applied each mutant alone to an export of `d7766440a` (`mut-tree` in scratch), never to the worktree. It then ran the mutant's killers with `-x`, wrote the pristine bytes back and checked their sha256. Every restore matched, and afterwards `diff -rq` of the export's `subfleet/` and `tests/` against the worktree's showed no differences. `1f52e85f0` changes only `tests/unit/test_review_pr127_service.py` and `tests/e2e/test_milestone1_codex.py`, so these results hold at the head. On the unmutated export, all 22 killer tests passed (22 passed in 30.56 s, 06:27:51 to 06:28:33Z, load 120 to 122). The results are in [mutants.json](2026-11-12-wake-caller-data/fix-r1/mutants.json).

| Finding | Mutant (undoes the fix) | Killed by |
| --- | --- | --- |
| 1 | `f1a-scan-offered`: the scan takes `pending` and `offered` | `test_c15_9_a_notice_posttooluse_offered_is_never_woken` |
| 1 | `f1b-wait-marks-nothing`: `wait_jobs` no longer calls `_offer_waited` | `test_c15_9_a_result_wait_printed_is_never_woken[ordinary]` |
| 2 | `f2a-envelope-resets-online`: any UserPromptSubmit is personal | `test_c15_9_the_wakes_own_prompt_leaves_the_streak_bound_in_force[wake-prompt]` |
| 2 | `f2b-envelope-resets-offline`: any offline UserPromptSubmit is personal | `test_notice_claims.py::test_c15_9_an_offline_wake_prompt_is_activity_but_not_personal_input` |
| 3 | `f3a-first-attempt-no-age-limit`: a first attempt is due at any age | `test_c15_9_a_notice_older_than_the_window_is_stale_on_its_first_attempt` |
| 3 | `f3b-migration-keeps-old-notices-due`: no backfill | `test_store_notice_wake_migration.py::test_d_wake_caller1_notice_delivery_is_a_numbered_additive_migration` |
| 4 | `f4-lane-refusal-consumes`: the lane refusal is out of `PREFLIGHT_FAILURES` | `test_c15_9_a_lane_refusal_leaves_the_notice_for_the_hook` |
| 5 | `f5-late-claim-marks`: the abandonment check is removed | `test_c15_9_a_surface_claim_after_its_client_gave_up_marks_nothing` |
| 6 | `f6-missing-session-terminal`: a missing or dead session is terminal again (both sites) | `test_c15_9_absent_or_closed_session_is_undeliverable_without_starting_it[None]` |
| 7 | `f7-compact-holds`: `notice_decide` goes back to `source_allows` | `test_c15_9_compaction_keeps_completion_wakes_and_clear_holds_them[compact-True]` |
| 8 | `f8a-unbounded-groups`: no `MAX_GROUPS_PER_PASS` | `test_c15_9_a_pass_examines_a_bounded_number_of_callers_and_reaches_them_all` |
| 8 | `f8b-no-held-memo`: the memo is never used | `test_c15_9_a_held_caller_is_not_read_again_until_something_changes` |
| P3 | `p3-render-after-claim`: headers are rendered after the claim again | `test_c15_9_a_header_that_cannot_render_leaves_no_claim_behind` |

Every one of the 13 mutants was killed: pytest exited 1 with `1 failed`. The first batch (f1a to f3a) ran from 06:28:43 to 06:32:27Z, at load 129 to 151. The second (f3b to p3) ran from 06:32:35 to 06:34:54Z, at load 146 to 116. Finding 9 changes only the documentation, so it has no mutant.

### Commands and results

Python 3.14.7 ran in a scratch venv (`uv sync --locked --group dev`, with `UV_CACHE_DIR` and `UV_PROJECT_ENVIRONMENT` both in scratch; `--extra api` failed because no such extra exists). The venv has no xdist, so every run was serial: one worker, under the four-worker cap. Each run used a private `--basetemp=$S/pytest-<name>` and `HYPOTHESIS_STORAGE_DIRECTORY` in scratch. From guard1 on, every run except pr127b also had `SUBFLEET_JOB`, `SUBFLEET_ATTEMPT`, `CLAUDE_CODE_SESSION_ID` and `PYTHONPATH` unset, as on CI. Most runs went through a SIGALRM wrapper (`alarm_pytest.py`) capped at 560 s. Times are in Z, and load is the one-minute `uptime` figure at the start and the end of each run.

| Run | Selection | Result | Time | Load |
| --- | --- | --- | --- | --- |
| run1 | `tests/fake/test_caller_notice_wakes.py -x` (before the render test) | 52 passed, 214.58 s | 05:50:34 to 05:54:21 | 134 to 152 |
| run2 | same file, `-k backlog` | 1 passed, 58.27 s | 05:55:30 to 05:56:49 | 97 to 87 |
| run3 | 7 unit files: notice_claims, sessions_notice_wake, store_notice_wake_migration, store_migration_2, store_qos_migration, notify_push, hooks | 21 failed, 213 passed, 211.04 s | 05:57:03 to 06:00:58 | 99 to 134 |
| run4 | the 3 migration files | 4 failed, 38 passed, 118.45 s | 06:01:52 to 06:04:07 | 120 to 141 |
| run5 | the 3 migration files | 42 passed, 36.25 s | 06:04:40 to 06:05:26 | 129 to 125 |
| run6 | `tests/unit/test_invariants_index.py` | 25 passed, 26.01 s | 06:07:39 to 06:08:21 | 98 to 116 |
| measure | probe E script, before and after | failed: built 752 notices, asserted 744 | 06:17:36 to 06:20:46 | 159 to 64 |
| measure2 | probe E script, before then after | 3 passed, 62.22 s; 3 passed, 56.11 s | 06:21:21 to 06:23:50 | 77 to 78 |
| run7 | `test_caller_notice_wakes.py -k cannot_render` | 1 passed | 06:24:57 to 06:25:51 | 82 to 96 |
| mutbase | the 22 killers on the unmutated export | 22 passed, 30.56 s | 06:27:51 to 06:28:33 | 120 to 122 |
| mutants | 13 mutants, two batches | 13 killed | 06:28:43 to 06:34:54 | 129 to 116 |
| guard1 | the four required guard files | 435 passed, 282.47 s | 06:38:02 to 06:43:07 | 100 to 108 |
| unitB | 10 unit files, `test_cli.py` first | **did not finish**: the 560 s alarm stopped it at 426 passed, 0 failed (563.78 s) | 06:43:33 to 06:53:14 | 99 to 127 |
| unitC | unitB's files except `test_cli.py` | 374 passed, 481.80 s | 06:53:44 to 07:01:52 | 128 to 73 |
| fakeD | test_caller_notice_wakes, test_notice_delivery, test_codex_turn_completed_fake | 177 passed (53, 15, 109), 368.23 s | 07:03:23 to 07:09:58 | 98 to 116 |
| e2eE | `tests/e2e/test_milestone1_codex.py::test_success_exports_by_rename_and_exposes_notices_and_artifacts` | 1 failed, 70.44 s, at `e2e.start()` | 07:10:14 to 07:11:34 | 119 to 165 |
| fakeF | test_notice_keep_delivery, test_unadmittable_pin_rereview, test_unadmittable_pin_admission, unit test_review_pr127_service | 3 failed, 43 passed, 289.90 s | 07:12:17 to 07:17:17 | 154 to 142 |
| pr127 | `tests/unit/test_review_pr127_service.py` | 10 passed, 13.87 s | 07:19:13 to 07:19:34 | 129 to 121 |
| pr127b | same file, `-k wait`, lane markers set | 5 passed, 5 deselected, 8.54 s | 07:19:45 to 07:19:56 | 121 to 120 |
| e2eE2 | the e2e test again | 1 passed, 20.22 s | 07:20:19 to 07:20:42 | 115 to 119 |
| guard2 | the four required guard files at `1f52e85f0` | 435 passed, 74.93 s | 07:24:31 to 07:25:50 | 102 to 99 |

What the failures were, and how each was settled:
- **run3 and run4.** All 21 failures in run3 were in the migration files. The test helper `_schema_before_migration` dropped `wake_state` while the new partial index still named it. The helper now drops any index whose SQL names a column before dropping that column (`tests/unit/test_store_migration_2.py`). In run4, the renumbering test moves the migration's number, so a backfill keyed on `NOTICE_WAKE_MIGRATION` failed. The backfill is now keyed on the column. run5 passed.
- **The first measure.** It built 752 notices where r1 gives 744 (see deviation 4). measure2 passed.
- **unitB.** Tests run in file order, and `test_cli.py` has 256 tests, so that whole file passed. unitC then ran the other nine files to completion: descriptor_review_fixes 56, hook_cost 5, hooks 85, notice_claims 25, notice_rendering 40, notify_push 33, operator_notices 79, sessions_notice_wake 49 and wake_cli 2.
- **e2eE.** The harness gives the daemon 10 s to answer, and at load 119 to 165 it did not, so the run failed before any assertion. e2eE2 ran the same test, with the updated assertion, and passed.
- **fakeF.** Three tests in `test_review_pr127_service.py` encoded the rule from f09a30772 that `wait` never touches notices: `test_wait_preserves_notices_for_finished_results_it_returns[succeeded-0|failed-1]` and `test_wait_receipt_preserves_the_conversation_wake`. They now encode the new rule and keep their invariant: the conversation's C-24.10 wake still fires after `wait`'s `offered`. Their fake applies the `notice.mark` ladder. pr127 and pr127b passed.

Every other run finished and passed. No run was counted as passed unless it finished.

The required guard set (`tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py`) passed at `1f52e85f0` (guard2). The whole suite was not run; CI runs it.

Key greps, all in the worktree:
- The new event kinds `notice.hook_abandoned` and `notice.wake_stale` and the new pass-result key `examined` appear in no test's exact compare, only in `tests/fake/test_caller_notice_wakes.py`.
- `own_envelope` and the hook's `deadline_s` are asserted only in the new tests. No test compares `notice.pending`'s full argument dict. The other `deadline_s` hits are `wait`'s.
- `wake_state` appears only in the changed files.

### Deviations

1. **`wait` marks the notice `offered`, not acknowledged as `runs show` does.** Since f09a30772, C-15.3 forbids `wait` from acknowledging a notice: a waiter that outlives its turn would otherwise consume C-24.10's only wake, and C-24.10 counts `offered` as unread. `offered` with transport `wait` keeps C-15.9 from waking, because the scan takes only `pending`. Inside a process Subfleet launched, `wait` marks nothing, because C-24.10's repair (`_surface_notices`) surfaces only `pending` notices.
   - Residual: for a native, conversation-bound session that runs `wait` outside a launched process, the notice stays `offered` after the C-24.10 wake, so a prompt hook may show it once more.
2. **The surface deadline is relative.** The hook sends `deadline_s`, and the daemon counts it from its own monotonic `arrived`, as `wait` already does. It does not use a wall-clock instant, so neither clock skew nor a patched clock moves it. The daemon also checks `descriptors.client_gone`.
3. **`stale` is added to the CHECK in migration 11 itself.** Migration 11 has not been released, so no additional migration was needed.
4. **The probe E notice count.** r1 says 744 notices in all, but also 256 woken plus 496 undeliverable, which is 752. The measurement keeps 744: 8 notices for each of the first 72 callers and 7 for the last 24, so 256 + 488.
5. **Tests that encoded the old rule were changed.** These are the three PR-127 unit tests above. They also include the e2e assertion at `tests/e2e/test_milestone1_codex.py:56`, which is now `("offered", "wait")`, and the migration test, whose pending and offered rows are now `stale`.
6. **The test helper changed.** `_schema_before_migration` now drops dependent indexes, so an older schema can be rebuilt under a partial index.
7. **Process.** Every run was serial, because the scratch venv has no xdist. CI-like runs unset the lane markers. No independent review ran here: hub review r2 is the next step.

### Commits

- `039c6f169` Merge release/217 (c8b61c95f) into feat/wake-caller-217
- `48432f617` Fix review r1 of #307: caller wakes (D-WAKE-CALLER1)
- `1d0d21680` Contract: C-15.3, C-15.9, C-16.5 after review r1 of #307
- `d7766440a` Add a test for the render P3; keep the probe E measurement
- `1f52e85f0` Tests: wait marks the caller's notices offered (C-15.3, r1 finding 1)
- This report and its data follow in their own commit.

Across `039c6f169..1f52e85f0`, 20 files changed (+958, −182). Every commit added named files only. Nothing was pushed, and no branch was created, moved or deleted. HEAD stays detached.

### Folders outside scratch

[`cleanup.py.txt`](2026-11-12-wake-caller-data/fix-r1/cleanup.py.txt) moved seven folders into `sf-wcall-f1/outside` and removed each with `shutil.rmtree`, guarded by an assertion that `sf-wcall-f1` is in the path. Before moving each one, it checked that no tracked file is inside and, for the caches, that every file is a `.pyc`. Six were bytecode caches that this job's runs made in the worktree from 06:00Z on: `subfleet/__pycache__` (25 files), `subfleet/adapters/__pycache__` (2), `subfleet/sessions/__pycache__` (11), `subfleet/conversations/__pycache__` (5), `tests/__pycache__` (1) and `tests/fake/__pycache__` (2). The seventh was `/tmp/sf-failed/834`, the state root the e2e harness kept for e2eE's failure: its pid folder held only that test. `/tmp/sf-failed` itself predates this job and stays. The scratch folder stays, with its logs, mutant tree and venv. No process this job started is still running, and no live state was read or changed.

Head built: the commit that adds this section, on top of `1f52e85f0`.

## Fix round 2

This round fixes review r2's two P1s on #307 (`~/reviews/subfleet-hub/wake-caller-2026-11-11/review-r2.md`), under the hub's rulings in the brief. One headless job did the work, from 19:31:38Z (`date -u`, first command) to about 22:35Z on 2026-11-15. It worked in a detached checkout of `feat/wake-caller-217` at `02ae7d1ed`, with scratch `$TMPDIR/sf-wcall-fix2` and `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`. The run ledger, the base failures, the runner, the files not run or only partly run, and the cleanup record are in [fix-r2](2026-11-12-wake-caller-data/fix-r2/). Line numbers below are at `1df275e14`.

### P1 1: PostToolUse and the inbox could deliver the same result

**Cause.** `hooks._deliver` read the job's unread rows with a plain `notice.pending`, printed them, then marked them `offered`. The wake's claim (`caller_wakes.py:358` at `02ae7d1ed`) shared nothing with that read. In one ordering, the hook read the row, the wake claimed and sent it, and the hook then printed it too. In the other, the wake claimed first, the hook's read found no unread row, and the hook printed its job-row line, which describes the same result.

**Change.** Every delivery path now takes one reservation per notice.
- `caller_wakes.reserve` (`subfleet/caller_wakes.py:107`) is that reservation. It is one conditional update, matched on the notice's full identity, that moves the notice out of `pending`. The path whose update matched delivers the notice; every other path delivers nothing. The wake claims through it (`:478`, as `surfaced`, claimed, and also only while `wake_state` is NULL). PostToolUse claims through it as `offered`, with transport `hook:PostToolUse`.
- The PostToolUse claim is `notice.pending` with `surface="PostToolUse"`, `job_id` and `deadline_s` (`NoticePendingArgs` gains `job_id` and `release`, `subfleet/protocol.py:324-325`). The daemon takes it as the wake takes its own (`_post_tool_use`, `caller_wakes.py:148`). It takes the caller's delivery lock, waiting at most `CLAIM_LOCK_WAIT_S` (2 s) within its deadline (`notice_claims.delivery_lock(wait_s=, gone=)`, `subfleet/notice_claims.py:32`). It then reconciles the offline receipts, reads the session's notices for the job, applies r1 finding 5's abandonment check, and reserves the `pending` ones. Its answer holds `notices`, the rows it reserved, and `absent`, which is true only when the session has no notice for the job at all.
- The hook (`subfleet/hooks.py:690`) prints only the rows it reserved. It prints its line from the job row only when `absent` is true: C-15.1 writes the notice in the transaction that makes the job terminal, so then no other path can deliver the result. Otherwise it exits 0 and prints nothing. It no longer marks anything after printing. `hooks._pending` was left unused and is removed.
- **A crash between the claim and the print leaves the notice retryable.** Nothing between the claim and the print can fail except the print itself: the rows' text is joined from the answer, and the job-row line is rendered only when nothing was reserved. A print that raises exits 0, which shows the harness nothing. It then gives the claim back (`_give_back`, `hooks.py:738`): `release` with the reserved ids returns each row still `offered` through `hook:PostToolUse` to `pending`, clearing its transport and `offered_at` (`caller_wakes.py:166`). The wake and the prompt hooks can then deliver it. A hook killed after its claim leaves the notice `offered`. The prompt hooks show it at the next prompt, and the wake never takes it, as for the wake's own claim-before-send (at most once).
- The time split is unchanged. The claim gets at most half the delivery reserve, as `notice.pending` did, and the give-back gets the rest, as `notice.mark` did. `deadline_s` is the claim's timeout less the smaller of 2 s and half that timeout.

**Tests.** `test_posttooluse_pending_read_then_engine_send_never_both` (`tests/fake/test_caller_notice_wakes.py:1002`) runs a wake pass after the hook's first daemon call returns. `test_engine_claim_then_posttooluse_delivery_never_both` (`:1019`) runs the hook on a real thread while the wake holds the caller mid-send, after its claim has committed. On `02ae7d1ed` both failed at `never both: one inbox wake or one hook print`. At the head both pass, 20 times each (40 of 40). Four further fake tests cover the rest of the ruling:
- `test_c15_9_a_posttooluse_print_that_fails_gives_its_claim_back` (`:1093`): the notice is `pending` again, and the wake then delivers it once. On the base, the hook raised `BrokenPipeError`.
- `test_c15_9_posttooluse_prints_its_job_line_only_when_the_session_has_no_notice` (`:1114`): after a prompt hook showed the notice, PostToolUse prints nothing; with no notice row it prints the job-row line. On the base it printed `(2, True)`.
- `test_c15_9_a_posttooluse_claim_after_its_client_gave_up_marks_nothing` (`:1132`). On the base, the daemon refused the surface.
- `test_c15_9_posttooluse_honours_an_offline_hooks_receipt` (`:1148`): a notice an offline prompt hook printed is neither printed again nor woken. On the base it was printed `(2, True)`.

At the head, the offline-receipt and late-claim tests ran 20 times each. The give-back and job-line tests are sequential and ran once in each of fix-new, step1 and step2-a1. A mutant that never gives a claim back (`_give_back` returns at once) fails the give-back test: the notice stays `offered`. The unit test `test_post_tool_use_that_lost_the_claim_prints_nothing` (`tests/unit/test_hooks.py:611`) has three cases: nothing reserved, `absent` false, and `absent` not a boolean. On the base all three printed the fallback.

### P1 2: a human prompt before the claim could be missed

**Cause.** When the delivery lock was taken, `surface` returned without recording activity (`caller_wakes.py:107` at `02ae7d1ed`). The claim at `:353` then judged eligibility on the registry row and transcript state read before rendering.

**Change.** A SessionStart or UserPromptSubmit claim that finds the lock taken now records its activity in its own transaction (`notice.hook_activity`), with the same SQL as the claim path, and claims nothing (`_record_activity`, `caller_wakes.py:138`, `:221`). Inside its claim transaction, after rendering, the wake now re-reads the conversation binding, a fresh registry row and a fresh transcript (`:462-478`). The transcript's last turn must be the one judged before. It then runs `_eligibility`, which re-reads the activity clock. A caller found unreachable at that point is left for the next pass, which records why.

**Test.** `test_human_prompt_during_header_render_prevents_wake[all|prompt-hook|registry|transcript]` (`:1053`). `all` is the review's probe: during header rendering it advances the transcript, makes the registry row `busy` and runs the real UserPromptSubmit hook. Each other case moves one signal alone. All four failed on `02ae7d1ed` at `the caller was active before the claim`. All pass at the head, 20 times each (80 of 80). The `prompt-hook` case also checks that the activity was recorded. The cases that leave the registry idle then wake once after 301 s of quiet.

**Left, for the hub.** An offline hook (no daemon) still records activity in its spool only once it holds the lock. The spool is read-modify-write, and reconciliation unlinks it, so writing it without the lock could lose a record. With the lock taken it records nothing, and only the in-claim transcript and registry re-reads hold the wake. A fix needs per-write activity files that reconciliation consumes by name. The online path, which the finding cites, records activity unconditionally.

### Schema, scope and contract

- **Schema.** Unchanged: `git diff 02ae7d1ed -- subfleet/store.py subfleet/store_schema.sql` is empty. Migration 11's columns, table and index are as #314 and #330 carry them.
- **Out of scope.** r1's finding 11 (the push frame carries no inbox `priority`, `notify_push.py:317`, pre-existing) is unchanged, for the hub to file.
- **Contract.** A change-log line for this round now heads "Changes in version 3" (`docs/acceptance-contract.md:9`). C-15.9 now says:
  - every path takes one reservation;
  - the wake judges the caller again inside its claim;
  - a prompt or start hook that finds the lock taken records its activity;
  - what PostToolUse's claim, its `absent`-only line and its give-back do;
  - the offline residual above;
  - what a crash after a claim leaves.

  C-15.2's time split names the claim and the give-back. C-15.6 says the hook's notice claims write and run on the request pool. C-16.5 names PostToolUse's claim among the surface claims. No clause was renumbered.

### Tests changed because they encoded the old rule

- `tests/unit/test_hooks.py`: the delivery, remaining-deadline, six-second and terminal-poll tests now expect `notice.pending`'s claim and no `notice.mark`. The six-second test is renamed `test_post_tool_six_second_timeout_keeps_its_notice_claim`. The terminal-poll test also gives back a claim whose print fails, inside the deadline. The job-row line's fake now answers `absent`.
- `tests/unit/test_notice_rendering.py::test_c15_1_the_hook_fallback_never_offers_a_cancelled_jobs_o_path`: its fake now answers `absent`.
- `tests/fake/test_notice_delivery.py`: two docstrings no longer say the PostToolUse hook marks `offered` from a read. No assertion changed.

### Commands and results

Python 3.14.7 ran in a scratch venv: `uv sync --group dev`, with `UV_CACHE_DIR=$TMPDIR/uv-cache` and `UV_PROJECT_ENVIRONMENT` in scratch, exited 0 (20:10:55 to 20:11:51). Every run used `uv run --no-sync --with 'pytest-xdist>=3.6'`, plus `pytest-repeat` for the repeats. Each run had a fresh `--basetemp=$S/pytest-<name>`, `HYPOTHESIS_STORAGE_DIRECTORY` and `PYTHONDONTWRITEBYTECODE=1`, and `SUBFLEET_HOME`, `SUBFLEET_JOB`, `SUBFLEET_ATTEMPT`, `SUBFLEET_ROOT`, `CLAUDE_CODE_SESSION_ID` and `PYTHONPATH` unset. The runner is [run.sh.txt](2026-11-12-wake-caller-data/fix-r2/run.sh.txt). "Base" runs used a `git archive` of `02ae7d1ed` in scratch, with the new test files copied in and `PYTHONPATH` pointing at it. Times are Z; load is the one-minute `uptime` figure at the start and the end of each run.

| Run | Selection | Result | Time | Load |
| --- | --- | --- | --- | --- |
| base-probes | the three review probes (six cases), on `02ae7d1ed` | 6 failed, as intended | 20:16:37 to 20:20:08 | 111 to 119 |
| fix-probes | the probes, the r1 offered and late-claim tests | 8 passed | 20:34:33 to 20:39:38 | 203 to 159 |
| fix-new | the four further PostToolUse tests | 4 passed | 20:50:31 to 20:53:11 | 173 to 147 |
| base-new | the same four, on the base tree | 4 failed, as above | 20:54:32 to 20:56:26 | not taken |
| base-unit | the changed and new `test_hooks.py` and rendering tests, on the base tree | 8 failed, 1 passed (the cancelled-job line test, whose rule both trees keep) | about 20:57 to 20:59 | not taken |
| step1 | every new and changed test, once | 22 passed | 21:04:32 to 21:08:54 | 33 to 115 |
| rep20-p1 | the two P1 1 probes, `--count 20` | 40 passed | 21:09:55 to 21:15:22 | 136 to 146 |
| rep20-p2a | `[all]` and `[prompt-hook]` by node id | no tests ran: `pytest-repeat` renames the ids | 21:16:01 to 21:18:56 | 118 to 82 |
| rep20-p2a2 | the same two cases by `-k`, `--count 20` | 40 passed | 21:19:50 to 21:22:27 | 113 to 187 |
| rep20-p2b | `[registry]`, `[transcript]`, the offline-receipt and late-claim tests, `--count 20` | 80 passed | 21:22:53 to 21:26:10 | 203 to 111 |
| collect | the 146 importer files, `--collect-only` | 6,760 collected | 21:26:52 to 21:33:05 | 114 to 100 |
| step2-a1 | `tests/fake/test_caller_notice_wakes.py`, in full | 63 passed | 21:33:49 to 21:36:23 | 102 to 104 |
| step2-a2 | twelve unit importers of `hooks`, `caller_wakes`, `notice_claims` | 499 passed | 21:36:39 to 21:42:25 | 101 to 27 |
| step2-a3 | four fake notice files, `tests/e2e/test_conversations.py`, two live-store guard files | 385 passed; 8 failed and 35 errors, all in the e2e file | 21:42:58 to 21:56:18 | 25 to 111 |
| step2-a4 | `test_compat.py`, `test_protocol_registry.py`, `test_protocol_secrecy.py` | **did not finish**: the 560 s cap stopped it at about 97%, with 1,939 passed and none failed or erred | 21:57:51 to 22:07:13 | 115 to 270 |
| step3 | the four guard files | 435 passed | 22:07:53 to 22:11:57 | 266 to 206 |
| e2e recheck | three of the 43 e2e failures | 2 passed, 1 failed (below) | 22:12:12 to 22:13:14 | 212 to 185 |
| mut-m1 | the give-back test on a head export whose `_give_back` returns at once | 1 failed: killed | 22:16:41 to 22:17:02 | not taken |
| step2-b1 | 45 unit files among the importers not yet run (1,556 tests; [partial-run-files.txt](2026-11-12-wake-caller-data/fix-r2/partial-run-files.txt)) | **did not finish**: the 500 s cap stopped it at about 93%, with about 1,471 passed and none failed or erred | 22:17:32 to 22:25:52 | 249 to 303 |

Each concurrency test ran at least 20 times: each P1 1 probe 20 times, each of the four P1 2 cases 20 times, and the offline-receipt and late-claim tests 20 times each, 160 runs in all, every one passed. The `real_lsof` and `real_cwd` markers were deselected in the importer runs, as round 1 did, since the brief bars inspecting live processes.

**The e2e file.** 42 of its 43 problems were `condition timed out after 10s`, the harness waiting for its daemon; the other was a 20 s `daemon status` timeout. Load rose from 25 to 111 during that run, and round 1's e2eE recorded the same symptom. At recheck, `test_the_daemon_keeps_the_catalog_and_the_list_reports_it` and `test_receipts_carry_the_persons_text_for_other_clients` passed. `test_a_person_can_cancel_a_steer_before_handover` failed on `person-only: ... the caller carries Subfleet's attempt markers`: this job runs inside a lane, and a person-only check sees those markers. It does not touch notices or hooks. The other 40 e2e cases were not rerun.

### What didn't run

- **76 of the 146 importer files never started** ([not-run-files.txt](2026-11-12-wake-caller-data/fix-r2/not-run-files.txt), 1,855 tests). About the last 7% of step2-b1's 45 files and the last 3% of step2-a4's three files did not run either. Each of these files imports only `protocol.py`, whose change adds two defaulted fields to `NoticePendingArgs`. The files that run the protocol registry, secrecy, compatibility and refusal checks ran (`test_handler_refusals.py` in full as a guard). At a load of 100 to 300, all 146 files would not fit the time box. CI runs the whole suite.
- The 40 e2e cases not rechecked, and the whole suite.
- One mutant ran (mut-m1, the give-back). For every other new test, its failure on `02ae7d1ed` stands in for a mutant. The give-back test needed the mutant because its only base failure was the crash itself: the base claimed nothing before printing.

### Deviations

1. The three review probes keep the brief's exact names, without the file's `test_c15_9_` prefix.
2. The give-back is `notice.pending` with `release`, not a new op. A new op would have changed `protocol.OPS` and the op-registry tests, outside the finding.
3. PostToolUse now claims only the job's `notices` rows. An `offered` pin notice naming the job is no longer printed again by layer 2; layer 3 still shows it. A `pending` pin notice is withdrawn at the job's end anyway (`daemon._withdraw_pin_notice`).
4. Version skew. A daemon from before #307 ignores the claim's fields and returns its unread rows unmarked; the hook prints the job's rows, and that daemon has no wake. A daemon at `02ae7d1ed` refuses `surface="PostToolUse"`, so the hook prints nothing and layer 3 delivers.
5. Commits carry the author email `max@optiqal.ai`. Git ran with `GIT_CONFIG_GLOBAL=/dev/null` because global config stalls git here, and earlier commits use another of Max's addresses.
6. **Process.**
   - One run (step2-a3) exceeded the tool's 595 s call limit. The tool moved it to its background mode, and I polled its log until it finished. Later runs carried a 560 s or 500 s cap inside the call.
   - Two early read-only commands also outran the tool's default 120 s limit and were moved to the background; I stopped both. One was the first command: its `git status` took 23 s to list untracked files, and `git branch -a` stalled on global git config. The other was a recursive `grep` over the worktree, replaced by `git grep`.
   - The test runs also unset `SUBFLEET_HOME`, which pointed at the live root. The conftest isolates the home anyway.
   - No subagents and no workflows ran, though ultracode was on. Delegation goes through `subfleet`, which the brief bars against live state.
   - No independent review ran here: hub review r3 is next.

### Commits

- `f2bb4bdc4` Tests: review r2 probes for #307, failing on 02ae7d1ed
- `3fed9272d` Fix review r2 of #307: one reservation per notice; activity under the lock
- `1df275e14` Contract: C-15.2, C-15.6, C-15.9, C-16.5 after review r2 of #307
- This report and its data follow in their own commit.

Across `02ae7d1ed..1df275e14`, 9 files changed (+496, −91). Every commit added named files only. Nothing was pushed, and no branch was created, moved or deleted. HEAD stays detached.

### Folders outside scratch

The runs left six bytecode caches in the worktree, from the e2e subprocesses, whose environment drops `PYTHONDONTWRITEBYTECODE`:
- `subfleet/__pycache__` (27 files)
- `subfleet/adapters/__pycache__` (2)
- `subfleet/sessions/__pycache__` (11)
- `subfleet/conversations/__pycache__` (5)
- `tests/__pycache__` (1)
- `tests/fake/__pycache__` (3)

Each was checked to hold only `.pyc` files and no tracked file. Each was then moved into `sf-wcall-fix2/outside` and removed with `shutil.rmtree`, guarded by an assertion that `sf-wcall-fix2` is in the path ([cleanup.json](2026-11-12-wake-caller-data/fix-r2/cleanup.json)). The later runs (mut-m1, step2-b1) left four again, all removed the same way, through `sf-wcall-fix2/outside2`: `subfleet/__pycache__` (14 files), `subfleet/adapters/__pycache__` (2), `subfleet/conversations/__pycache__` (5) and `subfleet/sessions/__pycache__` (6) ([cleanup-2.json](2026-11-12-wake-caller-data/fix-r2/cleanup-2.json)). `$TMPDIR/uv-cache`, the brief's cache, existed before this job and stays. It holds the `--with` overlay environments. The scratch folder stays, with its logs, base tree and venv. No process this job started is still running, and no live state was read or changed.

Head built: the commit that adds this section, on top of `1df275e14`.

## Fix round 3

Started **2026-11-16 03:47:29 UTC**, from the first command's `date -u`; finished **2026-11-16 04:11:26 UTC**. Base and assigned detached HEAD: `7ce23f716e23`. Work stayed in the assigned workspace and job scratch; the initial tree was clean.

**P1 — offline hook contention lost personal activity: fixed.** The fallback recorded activity only inside `reserve_offline`, after acquiring `delivery_lock`. A contending offline hook therefore saved neither the quiet clock nor personal input that resets the eight-wake streak. Simply moving the old aggregate write would also allow concurrent hooks to overwrite it, and reconciliation to delete a newer write.

`hooks.session_event` now publishes activity before attempting the delivery lock. Each write has its own atomic activity file. Reconciliation merges the latest activity/source and latest personal-input time, preserves newer online clocks and wake bounds, and deletes only its committed snapshot. Legacy aggregate files remain readable. A separate short activity lock orders publication against the final wake transaction, after rendering; that transaction applies pending activity without consuming files, so rollback cannot lose input. Hooks release the activity lock before attempting the delivery lock. C-15.9 and its version-3 change-log entry describe this behavior. This closes round 2's offline residual. Read-only reviews by two helper agents found no correctness blocker.

All seven final regression cases failed against unchanged production code before the fix:

| Test | Before | After |
| --- | --- | --- |
| `test_offline_prompt_under_render_lock_prevents_wake[busy,socket]` | Both sent an unwanted wake | Both pass; **20/20 each under `-n 4` (40/40)** |
| `test_personal_prompt_under_taken_lock_resets_streak[offline]` | Eight-wake streak was not reset | **20/20** pass, delivering after 301 quiet seconds |
| `test_c15_9_offline_activity_survives_a_busy_delivery_lock[prompt,session-start]` | Both lost activity | Both preserve activity, personal reset or source |
| `test_c15_9_overlapping_activity_publications_keep_human_time_and_latest_source` | Shared temporary-file collision | Preserves both publications |
| `test_c15_9_reconcile_preserves_activity_published_after_its_snapshot` | Deleted later activity | Preserves and replays the later prompt/source |

**Commands and results.** `uv sync --group dev` exited 0, using CPython 3.15.2. Every pytest invocation used `uv run --no-sync --with 'pytest-xdist>=3.6' --with 'pytest-repeat>=0.9' pytest -p r3_isolation -p no:cacheprovider -n 4 --timeout=300`, a fresh `--basetemp` named `pytest-<run>`, and scratch-local caches. Exact commands, selections, exit codes and UTC times are in [fix-r3-runs.jsonl](2026-11-12-wake-caller-data/fix-r3-runs.jsonl); the [runner](2026-11-12-wake-caller-data/fix-r3-run.py.txt), [isolation plugin](2026-11-12-wake-caller-data/fix-r3-r3_isolation.py.txt), [command inventory](2026-11-12-wake-caller-data/fix-r3-commands.txt) and all seven logs are retained beside it.

| Run, in execution order | Selection | Result |
| --- | --- | --- |
| `before` | Seven new regression cases plus an online control | **7 failed as intended, 1 control passed**, exit 1 |
| `after` | Same selection after the fix | **8 passed**, exit 0 |
| `repeat20` | Busy/socket render probes and offline streak probe, `--count=20` | **60 passed**, exit 0 |
| `importers` | All 21 importer files, including the extra online control | **2,529 passed, 84 skipped**, exit 0 |
| `final-new` | Final seven regression cases; redundant new online control removed | **7 passed**, exit 0 |
| `final-importers` | All 21 final importer files | **2,528 passed, 84 skipped**, exit 0 |
| `guards` | `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py` | **435 passed**, exit 0 |

`git grep -l` plus import inspection selected the [21 importer files](2026-11-12-wake-caller-data/fix-r3-importer-files.txt), including imports inside child-program strings. Final verification followed the prescribed order: new tests, every importer file, then the four guard files. `git diff --check` passed. `git diff 7ce23f716e23 -- subfleet/store.py subfleet/store_schema.sql` was empty: **migration 11's schema is exactly unchanged**, as D-SCHEMA-SLOTS1 requires.

**P3 — stale PR-body claims: left for the hub's authorized rewrite.** Replace the migration claim with: “Schema 11 adds notice-local wake state and caller activity/streak/cooldown; #314 also carries slot 11, coordinated under D-SCHEMA-SLOTS1.” Replace “first pass” with: “Eligible reachable callers are delivered when reached by the bounded rotation, subject to quiet/retry gates, cooldowns, at most 32 callers examined and at most 8 callers woken per pass.” Also describe offline receipts under the delivery lock, independent activity publication before that lock, and the final claim's activity recheck. The next review is scoped round 4. The PR body was not edited here. No P1 or P2 remains from this review; no other P3 was assigned this round.

**What did not run and limitations.** The whole suite did not run. The 84 skipped importer cases remain unverified; `real_lsof` and `real_cwd` were excluded, and an audit guard skipped prohibited fixture commands before execution. Only synthetic test state was used. The activity journal keeps one small file per offline hook until reconciliation, so a prolonged offline period can accumulate files and increase scanning work.

**Deviations and cleanup.** The prescribed Darwin temporary directory denied scratch creation. Tests instead used `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261115-224656-wcall-fix-r3-a1-7lyc6v5g/sf-wcall-fix3` as `TMPDIR`, with `UV_CACHE_DIR=$TMPDIR/uv-cache`; the initial dependency sync used its writable job-specific parent with the same scratch-local cache and venv. The temporary online control passed before the fix and was removed from the final additions, leaving every final new case failure-proven. Test failure retention was redirected into scratch so it would not inspect or prune another job's state. Two read-only helper agents were used; neither ran live-state commands. Generated folders outside scratch were recorded here before being moved into scratch and deleted with guarded `shutil.rmtree`:

- `/Users/maxghenis/.subfleet/worktrees/20261115-224656-wcall-fix-r3/subfleet/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261115-224656-wcall-fix-r3/subfleet/adapters/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261115-224656-wcall-fix-r3/subfleet/sessions/__pycache__`

The [cleanup record](2026-11-12-wake-caller-data/fix-r3-cleanup.json) records those paths and file counts. Scratch, its venv and uv cache were also removed with an assertion that `sf-wcall-fix3` is in the deletion path. No work started here remains running; no live state was read or changed, and no prohibited publication command ran.

**Commits.** All three named-file staging/commit attempts failed with exit 128 because the sandbox denied the assigned worktree's `index.lock` (`Operation not permitted`). As instructed, work continued; no alternate Git metadata or branch was created. No commit could be made, and HEAD remains the supplied base. No history was rewritten or pushed. The changed files and evidence remain for the hub to collect.

Head built: `7ce23f716e23830243cb2dfc66b87ebcd7eff0b7`.

## Fix round 4

Started **2026-11-17 13:37:54 UTC**, from the first command's `date -u`. The section was written by about 15:35 UTC. The base was `7d7923d4d`: the hub's merge of release/217 (`4aed7a3c9`) onto round 3's fix (`963d1afb9`). It was a clean, detached checkout in the assigned workspace. The findings are review r4's (`review-r4.md`); line numbers below are this head's.

### P1: the prune column test failed on `notices.wake_state` (fixed in the test)

- **Cause.** `test_prune_requires_every_migrated_column` dropped a column's indexes by `PRAGMA index_info`, which lists only an index's key columns. `notices_caller_wake` names `wake_state` only in its WHERE clause, so `DROP COLUMN` failed with `error in index notices_caller_wake after drop column: no such column: wake_state`. Prune itself was right.
- **Change** (`c02d8bd0b`, `tests/unit/test_prune_217.py:196`). The test also drops every index on the table whose `sqlite_master.sql` names the column, by a word-boundary match. Since `\b` counts `_` as a word character, `wake_at` doesn't match `wake_state`. An index missed this way would make `DROP COLUMN` fail loudly. An extra index dropped would still be followed by prune's own column check.
- **Schema unchanged.** Migration 11's schema, partial index included, is exactly as it was (hub ruling; D-SCHEMA-SLOTS1): `git diff 7d7923d4d -- subfleet/store.py subfleet/store_schema.sql` is empty.
- **Test.**
  - At `7d7923d4d`: 1 failed (`[notices.wake_state]`, the error above), 19 passed (run `p1-before`).
  - After the change: 20 passed (`p1-after`). Every parameter, `[notices.wake_state]` included, reaches `PruneRefused` matching `table\.column`; the store is unchanged and no backup is written.
  - The whole file: 33 passed, none skipped (`prune-file`). Round 3 never ran it.

### P2: activity files grew without bound (fixed)

- **Cause.** Each offline or busy-fallback SessionStart or UserPromptSubmit hook published one record in `<key>.activity.d/`, and only `reconcile` removed them. A session that ended while the daemon was down kept its records for good. Each hook read every record first, so an outage cost N² reads.
- **Change** (`6df091e85`). The offline hook calls `notice_claims.fold_activity` (`subfleet/notice_claims.py:216`) once it holds the delivery lock (`subfleet/hooks.py:515`), before it reads the store. The fold, under the delivery lock and then the activity lock (the daemon's order):
  1. snapshots the records: the aggregate (`activity_path`, which `read_activity` and `reconcile` already replayed) and the published ones;
  2. writes `_merge_activity` of them to the aggregate with one atomic replace (a temporary file, fsync, `os.replace`), and fsyncs the folder;
  3. unlinks exactly the published records in its snapshot, as `reconcile` does.

  No reader changed. The properties:
  - **The fold equals `_merge_activity` over the folded set:** the latest `at` with its `source`, and the latest `human_at`.
  - **A record published after the snapshot isn't in it, so it survives.**
  - **A crash between the replace and the unlinks loses nothing.** The folded records stay beside the aggregate, and merging is idempotent, so reads give the same value. `reconcile` applies it once, since `apply_activity` takes maxima.
  - **A corrupt record still fences its caller.** It raises ValueError in the snapshot, before anything is written or removed. The hook calls the fold inside its existing `try`, so a fold that fails, on a corrupt record or an OSError, makes the hook print nothing, just as a failed receipt write does. The notice stays `pending` for the next hook or the wake.
  - **A fold that can't take the activity lock within 0.25 s is skipped,** and the next hook folds.
- **The bound.** Between hooks, a session keeps **one** activity file, the aggregate. Its entries under `hooks/notice-receipts/` are at most **five**: the receipt, the delivery-lock file, the activity-lock file, the aggregate and the empty `.activity.d` folder. While hooks overlap, add one record for each hook that has published but not folded yet. The same goes for one that found the delivery lock taken since the last fold or reconcile. A hook now reads that many records, not every record since the last reconcile.
- **Tests** in `tests/unit/test_notice_claims.py`. Each failed at `7d7923d4d` (runs `p2-before`, `p2-before-counts` and `fold-corrupt-before`) and passes now (`p2-after`, `fold-corrupt-after`):

| Test | At `7d7923d4d` | After |
| --- | --- | --- |
| `test_c15_9_offline_hooks_keep_a_constant_number_of_activity_files` (`:440`): 12 offline hooks with the daemon unreachable | Files after each hook: 1, 2, 3, … 12 | 1 after every hook. `read_activity` returns the merged value: latest time, its source, latest personal time. At most 5 entries. Reconcile replays that value and removes the file. |
| `test_c15_9_a_corrupt_activity_record_still_fences_only_its_own_caller` (`:474`) | The other caller's files: 1, 2, … 6 | The other caller keeps 1 file and is woken. The corrupt caller prints and publishes nothing, keeps its corrupt file and isn't woken. |
| `test_c15_9_a_fold_never_drops_a_corrupt_record` (`:508`; added after the in-session review below) | The hook printed its notice despite a record corrupted after its own check | The hook prints nothing. `fold_activity` raises before writing or removing anything, and `reconcile` refuses too. |
| `test_c15_9_a_record_published_during_a_fold_survives_it[before-replace,after-replace]` (`:546`) | No fold ran (`[] == [True]`) | The late record survives, `read_activity` includes it, and the next fold takes it. |
| `test_c15_9_a_crash_between_a_folds_replace_and_its_unlinks_loses_nothing` (`:588`) | `DID NOT RAISE Crash`: no fold ran | The aggregate equals the merge, beside its 3 records (4 files). Reads are unchanged. Reconcile applies the value once and removes all four. |

### P2: `activity_lock` waited with no timeout (fixed)

**Proved against the engine first.** With the lock held from another file descriptor, at `7d7923d4d` (`p2-before`):
- the `caller-notices` pass (`CallerWakeEngine.pass_once`), with a due notice for that caller, was still blocked after 15 s and hadn't reached the next caller;
- an offline prompt hook was still blocked after 15 s;
- an offline prompt hook that arrived during a real wake claim, held inside `reserve`, was still blocked after 15 s: it waited out the claim.

**Change** (`6df091e85`).
- `activity_lock` takes `wait_s` and yields whether it holds the lock, through `delivery_lock`'s polling, now shared as `_file_lock` (`notice_claims.py:40`). An error opening the lock file counts as not held. `wait_s` has no default (`1b6fe3958`): a caller that forgot it would otherwise run its body unlocked. `git grep` finds no call without it.
- `ACTIVITY_LOCK_WAIT_S = 0.25` (`notice_claims.py:36`).
- **Hook.** Publication waits at most 0.25 s, then `os.replace`s its record whether or not it holds the lock (`notice_claims.py:197`), so round 3's ruling stands: activity is never lost. A hook that publishes without the lock is racing a claim already in progress, the race review r4 accepts in its section 2. A hook that completed before the claim took the lock is still visible to that claim, which lists the records only after taking the lock.
- **Claim** (`subfleet/caller_wakes.py:470`). It waits at most 0.25 s. If it doesn't get the lock, it returns before its transaction: no claim, no attempt counted, and no held memo. The pass goes on to the next caller, and the next pass, about a second later, judges this caller afresh.
- **Lock order is unchanged.** The daemon and a hook's fold take the delivery lock and then the activity lock. A publishing hook takes the activity lock alone and releases it before trying the delivery lock.

**Tests** (call times are from `p2-after`):

| Test | At `7d7923d4d` | After |
| --- | --- | --- |
| `test_c15_9_a_held_activity_lock_skips_its_caller_without_stalling_the_pass` (fake, `tests/fake/test_caller_notice_wakes.py:1264`) | Blocked after 15 s | Returns (0.89 s call). The held caller's notice is `pending`, with `transport` and `wake_state` NULL, `wake_attempts` 0 and no streak. The other caller is woken in the same pass. Once the lock is released, a later pass wakes the held caller once. |
| `test_c15_9_an_offline_hook_does_not_wait_out_a_wake_claim` (fake, `:1295`) | Blocked after 15 s | Returns (0.63 s call) and prints nothing, since the pass holds the delivery lock. Its activity is published, and the wake goes (the accepted race). |
| `test_c15_9_an_offline_hook_returns_while_another_process_holds_the_activity_lock` (unit, `:649`) | Blocked after 15 s | Returns (0.90 s call: two 0.25 s waits plus the hook's work). Its activity is published and it still prints its notice. |
| `test_c15_9_the_activity_lock_waits_a_bounded_time` (unit, `:633`) | TypeError: no `wait_s` | Not held, after at least 0.2 s and under 2.2 s |

**Why 0.25 s for both sides.**
- **The daemon** holds the delivery lock while it waits, so neither a fold nor another claim can hold the activity lock. It can only meet a hook's publication (a rename, microseconds) or a stopped process. 0.25 s is far above the first, and caps the cost of the second at 0.25 s per held caller per pass: at most 8 s if all 32 callers a pass examines were held.
- **A hook** gains nothing by waiting longer for a claim in progress: its record lands after that claim's read either way. Its wait only orders it against other hooks' publications and folds, which take milliseconds. A prompt's added delay is at most 0.5 s: one wait to publish and one to fold.

### P3: the claim's `except` comment (fixed)

The `except (OSError, ValueError, KeyError)` at `caller_wakes.py:494` (`:486` at `7d7923d4d`) now names what it covers: invalid activity, the conversation binding, the registry and transcript reads, the eligibility judgement and the reservation. It also says that the claim's transaction rolls back, the notices stay `pending` and unclaimed, and the next pass retries them. I didn't narrow it.

### P3: the PR body (listed for the hub's rewrite)

Not edited here. It should now say:
- **"What changes", migration bullet:** "Schema 11 (numbered, additive) adds notice-local wake state and a `caller_wake_sessions` table for activity, streak and cooldown. #314 also carries slot 11; the two are coordinated under D-SCHEMA-SLOTS1." This drops "No other open PR on release/217 adds a numbered migration."
- **"What changes", hooks bullet:** "Online hooks take an atomic surface claim, and PostToolUse claims its job's notice, under the caller's delivery lock. With the daemon unreachable or busy, a SessionStart or UserPromptSubmit hook first publishes its activity, one record per hook, even when that lock is taken. Under the lock it writes a receipt before printing and folds the session's activity records into one file, so a session keeps one activity file between hooks. The wake's final claim rereads activity under a short activity lock. No one waits for that lock more than 0.25 s: a hook publishes anyway, and a claim leaves the caller for a later pass."
- **Invariant 4:** replace "is delivered in the first pass after its gate or retry falls due" with "is delivered when the bounded rotation reaches it, at most 32 callers examined and 8 woken per pass, once its gate or retry falls due" (round 3's text).
- **Guard count:** 437 at this head, not 425.
- **"Next":** CI, then review round 5.

### In-session review

One read-only general-purpose agent reviewed `7d7923d4d..cd7ea9d9c`, in the foreground, for about 10 minutes, on synthetic state under the job scratch. It found no defect in the production change. Its scratch probe raced publishers with the lock wait forced to 0 against a folder holding the delivery lock: all 600 records were folded, none were left, and the final value was the maximum. It made two test findings, both fixed in `632fc4499`:
1. The corrupt-record test never reached the fold, because the hook's check before publishing refused the record first. `test_c15_9_a_fold_never_drops_a_corrupt_record` now corrupts a record after that check. At `7d7923d4d` the hook printed its notice anyway (`fold-corrupt-before`).
2. The lock-wait test's upper bound (`wait_s + 15 s`) meant little. It is now `wait_s + 2 s`.

This is supporting evidence only; review round 5 is the independent review.

### Left, with why

- **Daemon-startup sweep of folders whose sessions no longer exist: not done; for the hub to file.**
  - The folder names are SHA-256 hashes of session ids, so a sweep needs every live session id.
  - "No longer exists" can't be proved safely: a registry row is missing during an app restart or an account switch (`CLOSED_FAILURES`), so a sweep could delete a live session's records.
  - With the fold, a gone session leaves at most five entries, not one per hook run.
- **A holder that never lets go of the activity lock** (a stopped hook process; the harness kills a hook at its timeout, which releases the flock).
  - Folds for that session are skipped, so its records can pile up again until a reconcile, which doesn't take the activity lock, or until the holder exits.
  - The pass skips that caller, 0.25 s each pass, until the lock frees or its notices leave the window at 24 h.
- **Two concurrent hooks with exactly equal `at`.** The merge keeps the later record in reading order. A fold can change that order for a record it didn't fold. Both values are equally "latest", so either source is a correct answer.

### Contract

`docs/acceptance-contract.md` (`cd7ea9d9c`):
- **The C-15.9 clause** now states the 0.25 s bound on both sides and what each does when the lock is missed. It also describes the hook's fold under the delivery lock and then the activity lock, the one-file bound with its concurrent excess, and how a late record and a crash are handled.
- **A change-log line** heads "Changes in version 3".

No clause was renumbered.

### Commands and results

**Setup.**
- Scratch: `$TMPDIR/sf-wcall-fix4`, where `TMPDIR` is `getconf DARWIN_USER_TEMP_DIR`. The cache was `UV_CACHE_DIR=$TMPDIR/uv-cache`.
- `uv sync --group dev` with `UV_PROJECT_ENVIRONMENT` in scratch exited 0 at 13:41 UTC (Python 3.15.2).

**Every run** went through [run.py](2026-11-12-wake-caller-data/fix-r4/run.py.txt):
- the command was `uv run --no-sync --with 'pytest-xdist>=3.6'` (plus `--with 'pytest-repeat>=0.9'` for the repeats) `pytest -p r4_isolation -p no:cacheprovider -n 4 --timeout=300`, with a new `--basetemp=$TMPDIR/sf-wcall-fix4/pytest-<run>`;
- `TMPDIR`, `HYPOTHESIS_STORAGE_DIRECTORY` and the failed-state keep folder were in scratch, and `PYTHONDONTWRITEBYTECODE=1` was set;
- `SUBFLEET_HOME`, `SUBFLEET_ROOT`, `SUBFLEET_JOB`, `SUBFLEET_ATTEMPT` and `CLAUDE_CODE_SESSION_ID` were unset;
- each run had a 540 s cap, which no run reached.

The [isolation plugin](2026-11-12-wake-caller-data/fix-r4/r4_isolation.py.txt) skips a test before it runs `ps`, `lsof`, `security`, `launchctl`, `agent-secret`, `git push` or a write-shaped `gh`. Every run's command, exit code, UTC times and load are in [runs.jsonl](2026-11-12-wake-caller-data/fix-r4/runs.jsonl), and each log is beside it.

| Run, in order | Selection | Result | UTC | Load |
| --- | --- | --- | --- | --- |
| `p1-before` | the prune column test, at `7d7923d4d`, `-n 2` | 1 failed (`[notices.wake_state]`), 19 passed | 13:43–13:44 | 81 to 80 |
| `p1-after` | the same, after the fix | 20 passed | 13:54 | 81 to 67 |
| `p2-before` | the nine new P2 cases, at `7d7923d4d` | 9 failed, as intended | 14:01–14:02 | 70 to 67 |
| `p2-before-counts` | the two count cases, reworded to list counts, at `7d7923d4d` | 2 failed: counts 1…12 and 1…6 | 14:03 | 67 to 68 |
| `p2-after` | the nine cases, after the fix | 9 passed | 14:09–14:10 | 115 to 103 |
| `changed-files` | `test_notice_claims.py` and `test_caller_notice_wakes.py` in full | 104 passed | 14:11–14:13 | 93 to 58 |
| `prune-file` | **step 2:** all of `tests/unit/test_prune_217.py` | **33 passed** | 14:14–14:18 | 43 to 80 |
| `repeat20` | **step 3:** `-k` for round 3's three cases, `--count=20 -n 4` | **60 passed: 20 `[busy]`, 20 `[socket]`, 20 `[offline]`** | 14:18–14:20 | 77 to 110 |
| `importers-a` | **step 4:** `test_caller_notice_wakes.py` in full, `test_notice_claims.py`, `test_hooks.py`, `test_sessions_notice_wake.py`, `test_notice_rendering.py` | 280 passed, 1 skipped | 14:23–14:29 | 70 to 54 |
| `importers-b` | `test_notice_delivery.py`, `test_notice_header.py`, both `test_unadmittable_pin_*` files, `test_stopped_daemon.py`, `test_operator_notices.py`, `test_suggest_hint.py` | 177 passed, 36 skipped | 14:30–14:36 | 41 to 83 |
| `importers-c` | `test_descriptor_review_fixes.py`, `test_doctor.py`, `test_hook_cost.py`, `test_hooks_native_guard.py`, `test_review_pr127_service.py`, `test_hooks_install_differential.py`, `test_hooks_install_equality.py` | 163 passed, 3 skipped | 14:37–14:39 | 67 to 61 |
| `importers-d` | `test_live_store_guard.py`, `test_live_store_guard_round4.py`, `test_live_store_guard_st3_children.py` | 340 passed, 6 failed: `test_the_installed_subfleetd_without_subfleet_home`, every case (below) | 14:39–14:43 | 56 to 55 |
| `st3-venv` | those 6, on the scratch venv's own interpreter, one process | 6 passed | 14:43–14:44 | 48 to 47 |
| `compat-collect` | `--collect-only` for `test_compat.py` and the e2e file | 1,615 and 44 collected | 14:44 | 49 to 47 |
| `compat-1`, `compat-2` | `test_compat.py`, in two halves of its collected ids | 808 passed; 807 passed | 14:45–14:48 | 45 to 50 |
| `e2e-conversations` | `tests/e2e/test_conversations.py` | 44 skipped by the isolation plugin | 14:48 | 50 to 48 |
| `guards` | **step 5:** `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py` | **437 passed** | 14:49–14:50 | 45 to 29 |
| `fold-corrupt-before` | the new fold-corrupt case and the tightened bound, on a `git archive` of `7d7923d4d` with this test file copied in | 2 failed, as intended | 15:07–15:08 | 47 to 58 |
| `fold-corrupt-after` | the two, plus the corrupt-caller case | 3 passed | 15:08–15:09 | 58 to 43 |
| `notice-claims-file` | `test_notice_claims.py` in full, after the test fixes | 37 passed | 15:10–15:12 | 32 to 49 |
| `guards-final` | the four guard files, after the contract change | 437 passed | 15:14–15:17 | 72 to 79 |
| `required-wait` | after `wait_s` lost its default: `test_notice_claims.py` in full, the two fake P2 cases, round 3's three cases and `test_human_prompt_during_header_render_prevents_wake` | 46 passed | 15:22–15:23 | 37 to 50 |

**Notes on the runs.**
- **Importer files** ([list](2026-11-12-wake-caller-data/fix-r4/importer-files.txt)). A `git grep -lP` for imports of `hooks`, `notice_claims` and `caller_wakes` under `tests/` found 23 test files and one fixture module. That is round 3's 21, plus `test_hooks_install_differential.py` and `test_hooks_install_equality.py`, which came in with the release/217 merge. The fixture module, `tests/fixtures/home_resolvers_release_217.py`, brought in its user `test_live_store_guard_st3_children.py`. All 24 test files ran, `tests/fake/test_caller_notice_wakes.py` in full.
- **Markers.** Each importer run deselected `real_lsof` and `real_cwd`, as earlier rounds did. None of these files carries either marker, so nothing was deselected.
- **`importers-d`'s 6 failures** come from the environment, not this change. Under `uv run --with`, `sys.executable` is uv's overlay interpreter, whose `bin/` has no `subfleetd` script (`run uv sync --group dev to install subfleetd`). On the project venv's interpreter all 6 passed (`st3-venv`).

### What didn't run

- **`tests/e2e/test_conversations.py`:** all 44 cases were skipped by the isolation plugin, because each starts a daemon that runs a command the brief bars. Round 3 skipped them too.
- **Other skips: 40 in all.**
  - 36 in `test_stopped_daemon.py`, whose real `ps` reads of a SIGSTOPped test holder test the client's lock check, not notice code.
  - `test_hooks.py::test_the_hook_command_with_the_daemons_markers_spawns_nothing_and_surfaces_only_jobs`, which runs `/bin/ps -axww` over the whole machine.
  - 3 in `importers-c`: `test_doctor.py:605` and `:617`, and one the plugin reported at its own line, so its test isn't named in the log.
- **No mutants ran.** Each new test's failure on `7d7923d4d` stands in for one.
- **The whole suite didn't run** (CI runs it). Nothing was pushed, so no CI ran on this head; the run after the hub's push is the first full one.

### Deviations from the brief

1. **No Workflow, though ultracode was on.** In this headless run, a Workflow reports back through a notification that never arrives. I used one foreground review agent instead (above).
2. **A write outside scratch.** My first `git grep` for importers redirected into `/tmp/sf-wcall-fix4-importers.txt`, and the same command deleted it. The grep was then redone into scratch.
3. **Groups and split runs.** Step 4 ran as four file groups, `test_compat.py` in two halves and the e2e file alone, so each fit the cap.
4. **Runs not under `uv run`.** `st3-venv` and `compat-collect` ran on the scratch venv's interpreter directly, in one process, which is still within `-n 4`.
5. **One base run used an exported tree.** `fold-corrupt-before` ran on a `git archive` of `7d7923d4d` in scratch (`base-tree`), with the current test file copied in and `PYTHONPATH` pointing at it. That case was added after the fix, following the in-session review.
6. **Fail-closed fold.** The hook's fold sits inside its existing `try`, so a fold error makes the hook print nothing, as a failed receipt write does. The brief didn't say which way a fold error should go.
7. **A tool output saved under `~/.claude`.** The harness saved one long command output under `~/.claude/projects/…/tool-results/`. I didn't read it; I read the file in the worktree.

### Folders outside scratch

The runs left four bytecode caches in the worktree: `subfleet/__pycache__` (58 files), `subfleet/adapters/__pycache__` (5), `subfleet/conversations/__pycache__` (5) and `subfleet/sessions/__pycache__` (11), all under `/Users/maxghenis/.subfleet/worktrees/20261117-083630-wcall-fix-r4/`. Each was checked to hold only `.pyc` files and no tracked file. Each was moved into `sf-wcall-fix4/outside` and removed with `shutil.rmtree`, guarded by an assertion that `sf-wcall-fix4` is in the path ([cleanup.json](2026-11-12-wake-caller-data/fix-r4/cleanup.json)).

`$TMPDIR/uv-cache`, the brief's cache, existed before this job and stays. The scratch folder stays, with its logs, venv, base tree and the review agent's probe.

No process this job started is still running: every run's runner waited on its pytest child, every test thread was joined, and the review agent reported starting nothing that outlives it. No live state was read or changed, and no push or GitHub write ran.

### Commits

All on the detached HEAD, each adding named files only:
- `c02d8bd0b` Tests: drop partial indexes naming the column in the prune column check
- `d67875011` Tests: review r4 probes for #307's activity journal and activity lock
- `6df091e85` Fix review r4 of #307: fold offline activity; bound the activity lock
- `cd7ea9d9c` Contract: C-15.9 after review r4 of #307
- `632fc4499` Tests: a fold never drops a corrupt record; tighten the lock-wait bound
- `591ab86d2` Report: fix round 4 of #307 (this section and its data)
- `1b6fe3958` Require activity_lock's wait_s
- This list's update and the `required-wait` log follow in their own commit.

Head built: the commit that updates this section, on top of `1b6fe3958`.

## Fix round 5

Fixed both P1s and all three P2s for PR #307. Started **2026-11-25 21:20:32 UTC** from the first command's `date -u`. The assigned checkout was detached at `d37092161599266d52c5921b40ca1326ae639302`. The 13 new cases ran with that implementation before the fixes. The prune helper was extracted with its original match for that run. [Baseline log](2026-11-12-wake-caller-data/fix-r5/before.log) · [Fixed cases](2026-11-12-wake-caller-data/fix-r5/after.log)

### Fold I/O and offline delivery

Round 4's deviation 6 let a fold I/O error suppress the offline hook's receipt and print. It now catches `OSError` around the fold in `session_event`, where the hook can continue to the receipt while `fold_activity` reports the housekeeping failure. A corrupt snapshot still raises `ValueError` into the outer handler. A receipt write failure still stops printing. If syncing the replaced aggregate's directory fails, the fold keeps its input records. If an unlink fails, the record it could not remove and any following records still merge with the aggregate, which also covers records already removed.

Both `test_c15_9_a_fold_io_failure_still_surfaces_the_notice` cases failed at the starting head with empty output and no receipt. The replacement failure was injected at the aggregate destination alone. After the fix both cases printed once, wrote a receipt, preserved readable merged activity, and left no aggregate temporary. The unlink case kept both input records beside the successfully replaced aggregate. The corrupt-record check passed. The two existing receipt-write test cases passed with fault injection scoped to `reserve_offline`, ensuring their fsync and replace faults reached the receipt. [Before](2026-11-12-wake-caller-data/fix-r5/before.log) · [After](2026-11-12-wake-caller-data/fix-r5/after.log)

### Equal activity times

The old merge replaced the source on ties in reading order. Moving a record into the aggregate changed that order, so round 4's conclusion that either source was correct led to different wake decisions. File merges and SQLite reconciliation now use the same order. Latest `at` wins. On a tie, a source that holds wakes according to `notice_source_allows` wins so the notice can wait for the next prompt. For sources with the same eligibility, the lexicographically greatest string wins, with `None` before all strings. The merge takes maximum `human_at` independently. Online `_record_activity` continues to use daemon arrival order.

- `test_c15_9_a_fold_preserves_the_wake_decision_for_equal_activity_times` publishes an earlier-named `compact` from an actual offline hook after the fold's snapshot, with the same `at` and `human_at` as the later-named `clear` in the snapshot. The real engine held the raw representation and sent from the folded one at the starting head. It now holds both.
- `test_c15_9_applying_equal_time_activity_preserves_a_holding_source` previously stored incoming `compact` over equal-time `clear`. It now retains `clear`.
- `test_c15_9_activity_merge_is_independent_of_fold_and_reconcile_grouping` failed on both explicit tied splits. It now passes with `max_examples=100` and two explicit examples. It compares the final claim's read and apply across reconciled, folded and late-published groups with the merge of all raw records. The generated records use three possible activity times, all five requested sources and independently generated personal-input times.
- Existing tests that depend on earlier and later source activity already supplied distinct times. This round required zero timestamp adjustments.

[Before](2026-11-12-wake-caller-data/fix-r5/before.log) · [After](2026-11-12-wake-caller-data/fix-r5/after.log)

### Activity spool bound

A stopped fold could leave its aggregate temporary indefinitely. A fold now removes this session's aggregate temporaries while holding both locks, including when it has no published records. It validates activity before cleanup so a corrupt snapshot still preserves the evidence. The tests created three files with the fold's actual temporary naming format to model stopped folds. Both `test_c15_9_a_fold_removes_stopped_folds_temporaries` cases left eight entries at the starting head and returned to five after the fix, with and without new records to fold.

The bound is **five recursive entries** under `hooks/notice-receipts/` for a session after a successful fold with a receipt and no leftovers. Add one published record for each hook that publishes while the delivery lock is held or whose fold misses the activity lock, until a successful fold or reconcile consumes it. A late publication outside the snapshot survives. The test with eight contending prompts verifies **13 recursive entries and five top-level entries**, then **five recursive entries** after another hook. A fold I/O error or interruption can leave extra files. The fold leaves record temporaries inside `.activity.d/` because an active hook can write one before acquiring the lock. Those temporaries add to the recursive count. [Before](2026-11-12-wake-caller-data/fix-r5/before.log) · [After](2026-11-12-wake-caller-data/fix-r5/after.log)

### Activity-lock wait budget and claim clock

The first pass with 32 held activity locks previously spent **9.903 s** before returning. Each pass now shares **1 s** of activity-lock polling, charging elapsed monotonic time spent acquiring each lock. An acquisition allows up to **0.25 s** or the remaining budget. After exhaustion, later callers get one nonblocking attempt. A hook can spend up to 0.25 s polling for publication and another 0.25 s for its fold. `test_c15_9_activity_lock_waits_share_one_pass_budget` passed with a **125 ms per caller** allowance for ordinary work and scheduling, or **5 s** total for the 32 held callers. It verifies pending and unclaimed notices with zero attempts and a successful next-pass wake for caller 33 while the other locks remain held.

The earlier pass sent the due retry **0.637 s after its 24-hour cutoff** using a timestamp from before expiry. The final claim now reads `self.now()` once after it acquires the activity lock. That reading drives retry expiry, the final eligibility and transcript recheck, `wake_at`, `last_wake_at` and the matching post-send and rollback writes. Using the same time throughout keeps the final judgement and persisted claim consistent. `test_c15_9_a_retry_can_expire_between_the_pass_start_and_claim` now records the retry as `undeliverable` with reason `retry-expired` and sends only the fresh notice for the same caller. [Before](2026-11-12-wake-caller-data/fix-r5/before.log) · [After](2026-11-12-wake-caller-data/fix-r5/after.log)

C-15.9 and the version 3 changelog record the new fold, tie and lock behavior and the claim clock. They describe the reviewed race. In review r5, a prompt published and returned in 0.36 s while reservation was paused after the activity read. Before the final activity read, its activity is visible and holds the wake. After that read, the wake can go through and the next reconcile resets the streak for personal input timestamped at or after the claim.

### Prune index detection

`PRAGMA index_info` lists key columns, so a partial index can hide a column in its predicate. The prune test now calls a helper that matches the column case-insensitively in key columns and SQL. Word boundaries cover bare, double-quoted, bracketed and backticked identifiers. A string literal can also match. Dropping an extra test index is harmless because the disposable test still drops the target column and verifies prune's independent missing-column refusal.

All four `test_prune_finds_partial_indexes_naming_case_variants` cases failed at `DROP COLUMN` with the old match and passed after the fix. All 20 migrated-column parameters reached `PruneRefused` naming the expected `table.column`. `git diff d37092161 -- subfleet/store.py subfleet/store_schema.sql` was empty. [Before](2026-11-12-wake-caller-data/fix-r5/before.log) · [After](2026-11-12-wake-caller-data/fix-r5/after.log) · [Full files](2026-11-12-wake-caller-data/fix-r5/full.log)

### PR body replacements for the hub

- **Migration.** “Schema 11 (numbered, additive) adds notice-local wake state and a `caller_wake_sessions` table for activity, streak and cooldown. #314 also carries slot 11. The hub coordinates the two under D-SCHEMA-SLOTS1.”
- **Hooks.** “Online hooks claim notices atomically. PostToolUse claims its job's notice under the per-caller delivery lock. Offline prompt and start hooks publish an activity record before trying the delivery lock, including after a 0.25 s activity-lock miss. A hook holding both locks folds its snapshot and cleans stopped aggregate temporaries. The receipt still governs delivery if the fold hits an I/O error. The latest activity wins, with equal timestamps favoring sources that hold wakes. A successful fold with a receipt and no leftovers leaves five recursive entries. Contending or skipped folds can leave one record per hook until a successful fold or reconcile, and record temporaries add to the count.”
- **Invariant 4.** “Reachable quiet notices are delivered after their gate or retry falls due when the rotation reaches the caller and the claim can acquire its locks. A pass examines at most 32 callers and wakes at most eight by default. Activity-lock acquisition budgets are 0.25 s per caller and 1 s per pass, followed by a single no-wait attempt for later callers. A missed lock leaves the notice pending for a later pass. Retry expiry and wake timestamps use the claim's clock.”
- **Guards.** “The state contract, module names, invariants index and handler refusals checks passed 437 tests.”
- **Next.** “Scoped review round 6. The hub resolves the acceptance-contract conflict and runs full CI on the resulting head.”

### Setup, isolation and commits

`gh pr view 307 --repo MaxGhenis/subfleet-v2` succeeded as a read. `uv sync --group dev` created the missing project `.venv` on Python 3.15.2. `uv pip install --python .venv/bin/python 'pytest-xdist>=3.6'` added xdist for the three live-store guard files, whose child-script checks use the project interpreter. [Environment setup](2026-11-12-wake-caller-data/fix-r5/uv-sync.log) · [Direct xdist install](2026-11-12-wake-caller-data/fix-r5/uv-xdist.log)

The sandbox denied the requested location under `getconf DARWIN_USER_TEMP_DIR`. The job's writable temp directory held `sf-wcall-fix5` and its private uv cache. A short symlink, `/private/tmp/sf-wcall-fix5-221914`, initially pointed socket fixtures to the in-scratch `roots` directory. The first descriptor run failed because the daemon resolved the symlink before binding. Five connection tests hit the macOS socket path limit and two later tests saw daemon state left by those failures. The retry put socket fixture roots in a second private scratch directory, `/private/tmp/sf-wcall-fix5-221914/sf-wcall-fix5`, which kept their canonical paths short. This was the additional scratch location outside the job temp directory. The runner kept each basetemp, Hypothesis data and the child temp directory in scratch and set `PYTHONDONTWRITEBYTECODE=1`. Each run checked `df -H /` before starting. The isolation plugin came from round 4, with an expanded read-only GitHub command check, socket fixture redirection and exact skipped node IDs. [Runner](2026-11-12-wake-caller-data/fix-r5/run.py.txt) · [Isolation plugin](2026-11-12-wake-caller-data/fix-r5/r5_isolation.py.txt)

After the live-store timeout, a synthetic-home Python probe ran the draw script with a 12 s faulthandler trace and a 35 s cap. It was compiling pytest source when the trace fired. `.venv/bin/python -m compileall -q` over the installed libraries, `subfleet` and the guard fixture sources prepared a 19 MB scratch cache and exited 0, reporting one absent `tests/unit/__init__.py` target. The ordinary isolated pytest retry passed in 286.11 s using the same interpreter command and timeout as the full live-store run. [Import probe](2026-11-12-wake-caller-data/fix-r5/draw-probe.log) · [Compile log](2026-11-12-wake-caller-data/fix-r5/compileall.log)

`git diff --check` passed. Git denied `index.lock` when staging the regressions, the implementation and contract, and the report. The assigned checkout holds the complete changes and evidence for collection.

After copying the logs, I removed both scratch locations with a guarded `shutil.rmtree`. The job scratch held `bytecode`, `compiled`, `draw-probe`, `hypothesis`, `logs`, `plugins`, `roots`, `tmp`, `uv-cache`. Each of the 13 named `pytest-*` basetemps was retired after its log was copied. The first socket symlink was removed before retrying with the short canonical directory. The project `.venv` and shared uv cache are available. Removed worktree bytecode caches from `subfleet/__pycache__`, `subfleet/adapters/__pycache__`, `subfleet/conversations/__pycache__`, `subfleet/sessions/__pycache__`, `.venv/lib/python3.15/site-packages/__pycache__`. [Cleanup manifest](2026-11-12-wake-caller-data/fix-r5/cleanup.json)

### Head built

`d37092161599266d52c5921b40ca1326ae639302`, with the round 5 changes in the assigned worktree. The sandbox prevented creating a new commit.

### Tests run

The overlay command was `uv run --no-sync --with 'pytest-xdist>=3.6' pytest`, with `-p r4_isolation -p no:cacheprovider -n 4 --timeout=300 --basetemp=<scratch>/pytest-<run> -ra --durations=8` and the selection below. The repeats added `--with 'pytest-repeat>=0.9'` before `pytest` and `--count=20` after it. The live-store guard run used `.venv/bin/python -m pytest` with the same pytest options. Exact commands, UTC times, exit codes and disk checks are in the manifest. [Commands](2026-11-12-wake-caller-data/fix-r5/runs.jsonl)

| Run | Selection | Result |
| --- | --- | --- |
| `before` | New regression cases at `d37092161` | **13 expected failures** |
| `after` | Same cases, corrupt folded record, and both receipt-write faults | **16 passed** |
| `full` | `test_notice_claims.py`, `test_caller_notice_wakes.py`, `test_prune_217.py` | **151 passed** (44, 70, 37) |
| `repeat20` | Round 3 cases by exact parameter IDs | 0 collected, exit 5. Pytest-repeat rewrote the IDs. |
| `repeat20-k` | Round 3 cases by name and parameter with `-k` | **60 passed** (20 `[busy]`, 20 `[socket]`, 20 `[offline]`) |
| `importers-a` | Caller notice wakes, notice claims, hooks, sessions notice wake, notice rendering | **290 passed**, 1 skipped |
| `importers-b` | Notice delivery and headers, both pin review modules, stopped daemon, operator notices, suggest hint | **177 passed**, 36 skipped |
| `importers-c` | Descriptor review fixes, doctor, hook cost, native hook guard, PR127 service, both hook-install modules | 156 passed, 3 skipped, **7 socket-fixture failures** |
| `importers-c-short` | Same files with a short canonical socket fixture path | **163 passed**, 3 skipped |
| `live-store-guards` | Three live-store guard modules on the project interpreter | **345 passed**, 1 child-draw property timed out at 300 s |
| `live-store-guards-draws` | `test_the_property_draws_the_same_environments_in_every_process` alone on the project interpreter | **1 passed** (286.11 s in the test) |
| `compat` | `test_compat.py` | **1,614 passed**, 1 skipped |
| `guards` | State contract, test module names, invariants index, handler refusals | **437 passed** |

The `git grep -l` importer list includes the resolver fixture and its ST3 child coverage. `tests/e2e/test_conversations.py` was omitted because its real-daemon fixture needs live process inspection. The importer runs skipped 41 named cases for process inspection or the detached SessionStart worker. Each exact node ID and reason is recorded. [Importer files](2026-11-12-wake-caller-data/fix-r5/importer-files.txt) · [Skipped tests](2026-11-12-wake-caller-data/fix-r5/skipped-tests.txt)

### Remaining

The code, contract, report and logs are ready for hub collection. Creating commits and obtaining clean Git status are blocked by the denied index lock. The hub owns the PR body edits, the contract conflict with `release/217`, scoped review round 6, full CI and the startup sweep follow-up. A missing registry row alone is insufficient grounds to remove a session's activity. Run 44130002325 on `d37092161` had nine passing shards and five cancellations before any steps ran. It supplied no prune result. The full suite stays with CI.
