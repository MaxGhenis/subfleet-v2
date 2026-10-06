# Conversation continuation — 2026-10-04

Implemented on `feat/conversation-continuation`, from
`f832e6b812bb94ed59318344992d53437010649c`, in the assigned worktree only.
The live state root and daemon were not used. Shared Git metadata is outside
the writable workspace, so commits are in `.git-local`; the accompanying
`2026-10-04-continuation.bundle` names this branch and requires that base.

The daemon now batches completed child runs into a durable Subfleet message
and starts the next turn once the current turn and lease end. Completed run
ids and wake requests are consumed in the message transaction; notice delivery
is repaired after restart. Accepted deliverables use their attempt path;
failed runs never advertise an unexported output. Wakes wait for a running
turn to end because the existing steer path admits personal messages only.

`subfleet wake --runs ID... --pr OWNER/REPO#N... --at ISO --note TEXT`
registers against the calling turn or bound native session. A request fires
once when any of its kinds becomes ready: all named runs, any watched PR event,
or its time. PRs share one batched `gh api graphql` call, at most once a minute.
Requests and polling baselines survive restart. Person input wins at acceptance
and dispatch; unstarted wakes at capacity yield their job and remain queued.

Final-text grammar:

```text
WAKE-ME: runs=ID[,ID...] prs=OWNER/REPO#N[,OWNER/REPO#N...] at=ISO note="TEXT"
```

Only consecutive trailing lines are parsed. Fields are unique, optional,
whitespace-separated, with shell quoting; each line needs a trigger. A line
is an independent idempotent request. There is one pending request per kind
per conversation, at most 16 targets per kind, and timed requests must initially
be at least five minutes away. Eight wakes without personal input throttle the
conversation to one wake every 30 minutes. Eight allows several build/review
rounds; the cooldown bounds loops while allowing unattended progress.

Blocks record their own timestamp. A later catalog transcript mtime clears a
superseded Claude block only after the same fresh external-writer check used
by C-26.3, with no live/queued turn or held lease. The durable event is
`conversation.unblocked`, reason `continued-elsewhere`; unknown delivery is
never claimed or replayed. Native history uses catalog-resolved paths and a
bounded transcript read on the file pool. Outside turns survive binding and
event replay, are labelled “Made in another app”, and appear chronologically;
opening/switching scrolls to the newest row at the bottom.

C-24.10–12 and desktop design D-28–30 specify the behavior and limits.

Validation counts below are per slice, with overlap; they are not a summed
unique-test count. JUnit artifacts are under the ignored `build/` directory.

| Foreground slice | Result |
| --- | --- |
| Final wakes, CLI, scripted provider, native ownership, lost notice | 30 passed, 3:28 |
| Store, service, handoff dispatch | 132 passed, 1 deselected, 7:27 |
| Wakes, history, reconcile | 79 passed, 2:40 |
| Catalog, steer service, state reads | 122 passed, 1 sandbox failure, 4:42 |
| Notices and daemon end-to-end | 136 passed, 43 skipped, 2 failures, 2:45; both notice failures addressed below |
| Operator notices and notice headers, after person-hook isolation | 101 passed; final slice separately passes the remaining lost-notice case |
| Frontend timeline and protocol | 28 passed, 2:25 |
| Frontend store, client, outbox and steer | 83 passed before interruption; one refusal case failed, then passed alone in 1:34 |
| Frontend steer properties | 2 passed, 5:34 |
| Frontend approval/card examples and diff | 46 passed before interruption during a separate draft-probe compile |
| Frontend Markdown static recheck | 27 passed, 1 ten-second nesting timeout, 1:23 |
| Frontend isolated nesting cases | 4 passed, 1 ten-second nesting timeout, 0:43 |
| Frontend question cards, titles and failed-dependency handling | 12 passed, 0:43 |

The final 30 include four real-service/scripted-provider scenarios: run
completion, trailing `WAKE-ME:`, a stubbed PR merge, and a timer each create
exactly one Subfleet message and dispatch a second provider turn. Hypothesis
checks holds, at-most-once requests, throttling, personal FIFO, and the
differential moot-block/live-writer rule. Companion binary-provider end-to-end
tests are present; four new wake cases are among the 43 skipped because this
sandbox denies the required `sysctl`/`ps` identity inspection.

The catalog lifecycle failure is its final `/bin/ps` process-census assertion,
after daemon/catalog shutdown. One existing service-close test was deselected
after its daemon setup failed on the same denied boot-identity inspection.
Notice test isolation removes inherited Subfleet launch markers from the
simulated person's hook and supplies an explicitly absent process table for
the fake lost guardian; production liveness behavior is unchanged. The
unchanged Markdown parser's ten-second bound timed out in a broader run and
its isolated recheck. Several exploratory combined slices exceeded ten minutes
before interruption; final focused runs use an eight-minute ceiling and exact
child-process cleanup. No live application was launched.

Four mutation checks were caught, with no survivors: bypass blocked holds,
disable throttling, leave a fired request pending, and clear a block despite
a live writer. `2026-10-04-continuation-mutations.json` records the results;
the mutation runner restores every source edit in `finally`.

`app/build.sh` succeeded within the 15-minute allowance, validated its plist,
and signed `build/Subfleet.app`. The invocation used writable compiler caches
and `SUBFLEET_SWIFT_NESTED_SANDBOX=off` for Swift's optional inner compiler-plugin
sandbox, which cannot start under this managed sandbox. The mandatory outer
sandbox remained in force. Existing Sendable/unused-await warnings remain.
All foreground command sessions were reaped; no task process is left running.

Implementation commits: `644917e` (backend), `74e8650` (timeline, contract,
design and build/probes), and `8cd43c9` (replay/polling/deliverables and provider
validation). The bundle also contains the final report commit. Every commit
has the requested Claude Opus 5.5 co-author trailer. Nothing was pushed.

## REQUEST CHANGES follow-up (PR 127 at 7504f3b8)

This section supersedes the earlier validation and review claims. Work stays
in the assigned worktree. No live Subfleet state, running daemon, installed app,
Application Support, default policy or routing aliases are changed. Shared Git
metadata denied writes, so commits use `.git-local`, on
`subfleet/pr127-review-fixes`, without pushes or history rewrites.

CI was fixed first. On pristine 7504f3b8, Python 3.12.14 and standard Python
3.14.7 each reproduced 10 failures and 3 passing controls. Eight failures are
exact MCP environment pins, one is the ledger, and the tenth is a service-level
reproduction of CoreProbeLive's empty-history assertion before catalog discovery.
The actual live probe needs macOS process inspection unavailable in this lane;
it is skipped, not claimed as passing. Base f832e6b8 passes all 13 corresponding
checks. The base history proxy omits the source tag assertion absent in that
version; its history-content assertion is identical.

For compact test names below, `fixes`, `requests`, `service`, `history`,
`pr_wakes` and `report` mean `tests/unit/test_review_pr127_<name>.py`.
All listed regressions pass fixed. The Hypothesis no-change property runs 25
generated examples across open/merged/closed states, old checks/reviews and
1–10 re-arm cycles; each example resets the durable poll clock. Positive
controls prove newly dated checks/reviews/merges/closes still wake.

| Finding | Fix (file:line) | Regression test | Result on 7504f3b8 / base | Killed mutations |
| --- | --- | --- | --- | --- |
| P1 CI: eight environment pins | tests/unit/test_claude_conversation_mcp_scope.py:66; exact argv pins remain, expected environment includes the two turn markers | test_d714_keeps_every_conversation_permission_mode_argv_unchanged (8 cases) | 8 failed on each Python; base 8 passed | ci-environment-pins |
| P1 CI: orphan clauses | docs/desktop/ledger.json:557,570,583; R-11–13 cite C-24.10–12 and evidence | tests/unit/test_desktop_ledger.py::test_milestone_9_clauses_are_all_cited | 1 failed on each Python; base passed | ci-ledger |
| P1 CI: history before catalog | subfleet/conversations/history.py:199; subfleet/conversations/service.py:597; use catalog, persisted attempt or exact Claude workspace path without discovery scans | fixes::test_history_before_first_catalog_pass | 1 failed on each Python; base passed (base has no source tag) | ci-history |
| P1 unchanged PR wakes loop | subfleet/conversations/wakes.py:443,455; first snapshot is a baseline; new first-poll events require timestamps after registration | pr_wakes::test_property_unchanged_watched_pr_never_wakes; four positive event controls | property failed; 4 controls passed | pr-first-poll-loop |
| P1 one bad PR poisons every watch | subfleet/conversations/wakes.py:307,475; preserve partial GraphQL data even on exit 1; consume and report only the bad watch | pr_wakes::test_partial_graphql_error_does_not_silence_other_conversations (exit 0 and 1) | 2 failed; healthy watch was suppressed | pr-batch-poison |
| P2 dropped final-text requests | subfleet/conversations/wakes.py:75,87,244; accept empty targets, bullets, bold and standard close-outs; isolate bad lines, validate timers at turn creation; app/Sources/Timeline.swift:493 displays refusals | requests::test_final_request_forms_are_accepted (7); test_bad_final_line_does_not_discard_the_valid_line; test_timer_floor_is_checked_at_turn_start; test_only_top_level_final_requests_are_accepted; frontend timeline refusal test | 10 failed, 6 security controls passed; Swift refusal failed | empty-field-drops-pr; closeout-drops-request; bad-line-drops-valid; timer-settlement-floor; bulleted-final-line; bold-final-line; fenced-request-injection; hidden-wake-refusal |
| P2 empty wake for announced runs | subfleet/conversations/wakes.py:335; silently satisfy delivered all-of requests and their alternatives without a message or throttle charge | fixes::test_already_announced_run_request_is_satisfied_without_a_wake | failed: 2 messages instead of 1 | empty-run-wake |
| P2 fan-out spends throttle | subfleet/conversations/wakes.py:342; pending all-of targets suppress individual automatic wakes and emit one message when all finish | fixes::test_all_of_fanout_has_one_wake_and_one_throttle_charge | failed after first completion | fanout-per-run |
| P2 PR poll blocks dispatch | subfleet/conversations/wakes.py:193; subfleet/conversations/service.py:1536; one worker polls with a 20-second subprocess timeout while dispatch continues; close joins it | service::test_pr_poll_does_not_hold_person_dispatch | failed while stubbed gh was held | blocking-pr-poll |
| P2 growing per-tick write work / scans | subfleet/conversations/wakes.py:39,428; durable repair queue, 500 entries per invocation, crash-safe deletion; subfleet/store_schema.sql:174 indexes notices; wakes.py:185 paces completion scans to once/second | fixes::test_notice_repair_does_no_writes_for_already_delivered_history; test_notice_job_lookup_uses_an_index; service::test_control_loop_paces_completion_scans; repair crash/batch controls | 3 failed: 10 historical writes, table scan, 100 completion scans | historical-notice-repair; unindexed-notices; unpaced-completions |
| P2 mid-turn marker relabels / duplicates | subfleet/conversations/history.py:126,188; synthetic user rows retain native ownership; a known prompt UUID still wins | history::test_synthetic_user_row_preserves_owned_turn (3); tests/frontend/test_review_pr127_timeline.py::test_mid_turn_markers_do_not_duplicate_or_relabel_the_answer (3) | all 6 failed; three known-UUID positive controls pass fixed | synthetic-prompt-boundary |
| P2 upgrade announces old runs | subfleet/conversations/wakes.py:166,277; persist activation and snapshot pre-existing completions once; normalize timestamps and retain new same-second results, including earlier-started runs; explicit old requests remain valid | fixes::test_upgrade_does_not_automatically_announce_old_completions; test_upgrade_keeps_runs_that_complete_after_activation; test_existing_completion_and_new_completion_in_one_second | automatic case failed; explicit case passed; in-flight boundary failed on head and the initial creation-time cutoff | old-upgrade-completions; upgrade-drops-inflight-result; upgrade-same-second-history |
| P3 Codex session marker lost | subfleet/conversations/launch.py:81; keep the resumed session marker after inherited-provider cleanup | service::test_resumed_codex_launch_preserves_its_own_session_marker | corrected fixture failed with missing session key | codex-session-lost |
| P3 open queues behind file work | subfleet/conversations/service.py:128,205; separate bounded history/read pool; shutdown joins it | service::test_open_is_not_queued_behind_worktree_and_diff | failed under saturated file pool | open-behind-file-ops |
| P3 moot-block check unpaced | subfleet/conversations/service.py:1556; pace the same block at 5 seconds, bypass delay for a new blocked_at; retain writer/lease/turn guards | service::test_moot_block_writer_and_catalog_checks_are_paced | failed: 100 checks instead of 1 | unpaced-moot-block |
| P3 wait causes duplicate wake | subfleet/daemon.py:2756; subfleet/cli.py:1269; terminal receipts contain notices, CLI acknowledges newly returned own-session results including failures | service::test_wait_acknowledges_only_finished_results_it_returns (2); test_wait_receipt_ack_prevents_a_second_conversation_wake | all 3 failed | wait-does-not-ack; wait-omits-notices |
| P3 PR body overclaims gained turns | docs/reports/2026-10-04-continuation-pr-body.md:17; docs/acceptance-contract.md and docs/desktop/design.md state transcript mtime strictly after blocked_at plus fresh guards | report::test_moot_block_report_uses_the_implemented_mtime_criterion | failed on head: corrected artifact absent | moot-block-report-overclaim |
| Upgrade replay regression found while fixing grammar | subfleet/conversations/wakes.py:244; preserve legacy literal-tail IDs; use content/occurrence IDs for newly recognised forms | requests::test_expanded_grammar_replay_preserves_legacy_timer_identity | failed on head (bullet lost); index-based expansion also repeats fired timer | expanded-grammar-replays-timer |

The suggested escalating cooldown is a policy recommendation. The specified
eight-wake/30-minute rule remains; no-progress PR and empty-run loops are removed.
Completion discovery is paced and indexed; the bounded queue removes historical
write amplification. Durable receipt history remains for replay guarantees.

The mutation runner restores every edit in `finally`, reaps its own child process,
and counts only assertion failures as kills (not collection/setup/compile errors).
[Machine-readable mutation evidence](2026-10-04-continuation-review-mutations.json)
records **30 killed, 0 survivors**, the precise test nodes and their output.
Extra controls cover cross-store repair crashes, 501 queued repairs, satisfied
alternative triggers, real wait receipts and known prompt UUIDs.

Validation uses Python 3.12.14 for the named suites and standard Python 3.14.7
for the final cross-version regression run. Slices are supervised in the foreground
with a 540-second ceiling; app building has a 900-second ceiling. Counts below
use each distinct test's final completed result, replacing rechecks, and are not
summed across overlapping suites.

| Suite | Passed | Failed | Errors | Skipped |
| --- | ---: | ---: | ---: | ---: |
| Named unit | 998 | 2 | 0 | 0 |
| Fake-provider | 82 | 0 | 0 | 0 |
| End-to-end | 2 | 0 | 0 | 83 |
| Frontend | 300 | 1 | 0 | 1 |
| Additional CLI / wait | 202 | 3 | 16 | 0 |
| Additional store migration / readers | 34 | 2 | 0 | 0 |
| Final Python 3.12 regressions | 84 | 0 | 0 | 0 |
| Final Python 3.14 regressions | 84 | 0 | 0 | 0 |

All **8,424 tests collect without errors** (7,134 unit, 836 fake, 85 end-to-end,
302 frontend, 62 process and 5 live). This is collection, not a claim that the full
suite passed. [Validation evidence](2026-10-04-continuation-review-validation.json)
lists the selected files, every slice, head failures and base comparison nodes.

Every remaining failure is classified against f832e6b8:

- Named unit: catalog lifecycle's final `/bin/ps` census and service-close's
  macOS boot-identity setup both fail identically on base. The other 998 pass.
- Frontend: `test_t1_t4_a_message_is_drawn_once_whatever_the_daemon_says`
  fails with the same nested steer counterexample on base: deliver message 2 into
  1, receive queued message 0, then deliver 1 into 0; message 2 precedes its host.
  This existing invariant failure is outside the review changes. The live core
  probe skips before daemon creation because boot/process inspection is unavailable.
- Additional CLI/wait: both CLI lock/identity assertions, the stopping-daemon
  assertion and all 16 wait-hub fixture errors reproduce on base. The daemon
  failures come from denied boot inspection; the CLI assertions have the same
  captured output on both versions.
- Additional store readers: the two daemon-backed checks fail boot inspection
  on base too. The initial two-second timing assertion took 2.54 seconds; its
  isolated base and fixed rechecks pass. All ten migration tests pass.
- All 83 end-to-end skips cite the C-5.3 `ps/sysctl` boot-inspection guard. No
  guard is bypassed, and no installed/live daemon is used.

Resolved harness/environment failures are not counted as product regressions:
missing basetemp parents caused setup errors; temporary fixtures inside this Git
worktree inherited its enclosing repository, and the `FIX` search also matched
this worktree's path. The repository tests pass with a Git discovery ceiling;
the sidebar test fails identically on base in this path and passes with neutral,
task-owned temporary state in the permitted system temp directory. Early compiler
cache aliasing caused duplicate Clang modules; canonical cache paths passed on
recheck. An initial misplaced Swift notice edit failed compilation and was
corrected before the successful app build and frontend checks. Exploratory
540-second timeouts and the missing optional `test_store.py` command executed no
completed suites and are excluded from totals.

The shared volume also reached nearly full capacity during fake-provider testing.
Its `SQLITE_IOERR_SHMSIZE`/disk-I/O failure occurred in unchanged store setup,
also reproduced using the base schema and a base pool fixture. Only disposable
files created by this task were removed. The admission property's completed
example fleets were then deleted between draws by
[review_bounded_temp.py](../../tools/review_bounded_temp.py), with assertions and
Hypothesis settings unchanged. The admission property, both pool tests and fake
wakes pass on recheck. The final helper/fake run is recorded separately. The
upgrade precision counterexample first failed on the creation/completion-time
fix as well as 7504f3b8; the snapshot and normalized floor now pass on both Pythons.

Reproduction commands use `.venv312/bin/python -m pytest -q` with the files
listed in the validation JSON, splitting as recorded to keep the ceiling. Run
all review regressions, the original wake suite, MCP scope pins and ledger on
both Python environments. For bounded admission temporary state, use
`PYTHONPATH="$PWD/tools" python -m pytest -p review_bounded_temp
 tests/fake/test_admission_liveness.py::test_c6_3_c26_9_admission_places_what_e053b2c_placed_pass_for_pass
 --basetemp=build/review/pytest/admission` (one shell line). Run mutations with
`python tools/review_continuation_mutations.py`; each child has a 180-second
ceiling, and cases can be split with `--only`.

`app/build.sh` **passes**, validates its plist and signs the local app at
`build/review/app/Subfleet.app`. It used canonical writable compiler caches and
`SUBFLEET_SWIFT_NESTED_SANDBOX=off` for Swift's optional inner plugin sandbox;
the mandatory managed sandbox remained active. Existing Sendable/unused-await
warnings remain. The app was neither installed nor launched. Final changes
after that build affect Python and documentation; Swift source is identical.

Implementation commits: `7f7503c4`, `f79288cd`, `7c8da642`, `d25a7631`,
`27761ee7`, `ca1c93cb`, and `4c9785cc`. The final report/verification commit
is the head saved in the verified
[delivery bundle](2026-10-04-continuation-review-fixes.bundle), requiring exactly
7504f3b8. [The head manifest](2026-10-04-continuation-review-fixes.bundle-head.txt)
names the full commit and ref; the following delivery commit stores those
artifacts without changing implementation. Every commit has the requested
Claude Opus 5.5 co-author trailer. Nothing is pushed or history-rewritten.
All foreground sessions are reaped; the final native process census finds no
task-owned test, daemon, guardian, compiler or probe left running.
