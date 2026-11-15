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
