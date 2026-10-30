VERDICT #271: REQUEST_CHANGES

Reviewed production head `02720082d4ca`, against release `f1d2f116e`. No production code changed in this review.

All **752 pre-existing requested tests pass**, including all 39 PR tests, 63 contract guards and 48 shard-planner
tests. The added review file has **6 passed, 3 failed**, covering the two findings below. Final collection
confirms 761 required nodes across 33 files, with no skipped or unrun nodes. The known probe timing property
passed on its one run here. All 19 holds finished; no pytest ran for ten minutes. E2e was not run.

## Findings

**P2-1: the settings-exit race remains when the guardian's wait hides exit from `Popen.poll()`.**

`subfleet/relay.py:458` tests `child.poll()` for the refusal flag; :451 uses the same test for status.
The guardian is concurrently in its blocking `child.wait()` (`guardian.py:295`). The installed CPython
3.14 POSIX implementation holds `_waitpid_lock` across that blocking wait. `poll()` returns `None` without
checking the kernel when that lock is busy. There is therefore a reachable interval after the child exits
and before the wait thread publishes `returncode` in which both checks report no exit evidence.

Executed input:
`tests/fake/test_turn_relay_exit_r3.py::test_a_real_exited_fake_is_not_a_relay_failure_while_the_guardian_wait_holds_its_lock`.
It uses the tree's actual interactive Claude fake with `[fake:exit-after-ack]`, an owned real `Popen`, the
real stdin pipe and relay, and scratch stdout/state. It holds the wait lock at the guardian's scheduling
boundary and uses `waitid(WNOWAIT)` to prove real kernel exit without reaping the child. The fake exits 1;
the settings write gets EPIPE, the refusal has no `child_exited`, both status reads say `running`, and the
settled reason is **`relay-failed`**. The test fails its expected `ended-without-result`. The child is reaped
before the assertion. This does not rely on an actually live process merely closing stdin.

Fix: make exit evidence authoritative despite the concurrent wait/cache-publication interval, and use that
evidence for refusal/status classification while preserving the live-child negative controls. A local,
test-only non-reaping kernel-exit peek makes this exact regression pass; reverting that peek kills it.
This is a proof-source experiment, not a complete production patch for all wait/reap interleavings.

**P2-2: a failed drain-entry metadata lookup abandons the turn without an outcome.**

`subfleet/conversations/runner.py:433` calls the new `_stdout_size`; at :448–450 it handles only
`FileNotFoundError`. One transient `OSError(EIO)` escapes to `_run`'s exception handler. The runner marks itself
finished, but neither applies EOF, contains, writes an outcome, nor calls `on_outcome`.

Executed input: `tests/fake/test_turn_relay_exit_r3.py::test_a_transient_entry_stat_failure_still_settles_the_exited_turn[proof|owner-ended]`.
Both fail with `reported=0`, `contained=[]`, `outcome=None`. These use a real, readable scratch stdout file,
the real reader and relay, and exactly one injected metadata error; the next stat succeeds. Release did not
make this additional stat before draining.

The owner-ended case matters particularly during recovery: `ConversationService._replay_unsettled`
(`service.py:2902–2909`) marks an attempt replayed before starting its runner and does not retry a runner
that later fails. A transient metadata fault can therefore leave the delivered message unsettled for the
service's lifetime. This service consequence is a code trace; the executed regressions isolate the runner.

Fix: retry obtaining the entry watermark on a recoverable metadata error, or explicitly retain a retryable
recovery state. Do not silently use zero for an unknown backlog. A one-retry candidate passes the new
owner-ended regression; removing that retry fails it. The candidate is confined to the review harness.

## R2 findings and checks

All input names below are in `tests/fake/` unless otherwise stated.

| R2 finding | Executed input | At reviewed head |
|---|---|---|
| P2-1, restart loses status proof | `test_turn_relay_exit_review.py::test_restart_recovers_exit_without_a_receipt[closed]` and `[failed]` | Both pass |
| P3-1, bound consumes entry backlog | `test_a_backlog_on_disk_at_the_proof_is_read_in_full`, `test_a_rebuilt_runner_reads_its_whole_backlog_before_the_bound` | Both pass |
| P3-2, receipt / owner-ended entry does not arm | `test_every_exit_drain_entry_arms_the_bound[receipt]` and `[owner-ended]` | Both pass |
| R1's preserved exit and permanent I/O boundary | Entire `test_turn_relay_exit_recovery.py` | 13 pass |

1. **Status proof: pass.** The two restart inputs above and
   `test_a_status_naming_the_child_exited_proves_the_exit_on_every_handshake[first|rebuilt]` pass.
   I found no reachable stale-status / different-child path. `RelayClient._exchange` is a synchronous request
   and one reply; transport failure closes its socket and reader, so a retry cannot consume a late reply
   from that connection. A rebuilt runner constructs another client. `RelayServer.status` polls its one
   guardian-owned child under the journal lock. `guardian.run_guardian` serves one child, once, for that
   attempt; another attempt gets its own attempt-derived socket and log. No status cache is persisted across
   a daemon restart. Reassigning a fake server's child or mispointing a runner's socket would not reproduce a
   production path. A fresh status can incorrectly say `running` during the wait-lock interval above;
   this is a false negative for exit, not a stale `exited` proof about another child.
2. **Drain entries: pass for normal reads; fail on the metadata error above.** Proof, status-only proof,
   receipt, owner-ended and rebuilt entries all reach `_drain_stdout`. The added
   `test_changed_stdout_cannot_leave_the_drain_polling_forever[shrunk|replaced|zero|missing]` all pass. With
   shrink/replacement, catch-up can remain unset, but the real reader returns empty and the drain returns;
   this does not leave an unarmed polling loop. Byte position increases on every successful real read.
   A zero/missing entry arms immediately. `test_zero_entry_size_arms_the_bound_for_a_real_growing_file` passes
   and contains at 30 fake seconds. Deleted/replaced bytes cannot be recovered; the backlog guarantee assumes
   the guardian's ordinary append-only stdout file.
3. **Deviation: acceptable, with a coverage qualification.** The diff adds a call to the saved real reader
   in each fake; assertions are unchanged. Without consuming the initial disk bytes these fakes could not
   satisfy the newly decided catch-up rule. The amended tests still model later continuous output by returning
   scripted progress. Against `clock-started-at-entry`, all five executed parameter cases survive:
   receipt-after-bound, restart-in-active-drain, at-bound, receipt entry and owner-ended entry. These tests
   verify arming / late receipt / rearming, not backlog preservation. The separate original and rebuilt
   backlog tests kill that same mutant. Thus it would be false to claim the amended cases themselves guard
   entry-versus-catch-up timing; this does not show a lost assertion or lost backlog regression.
   The final at-bound mutant rerun invokes the whole property, exhausting all four step values.
4. **Properties and CI: qualified pass.** The first property run reproduces all reported category counts:
   154 examples including four pinned, backlog 56, restarts 69 (44 once, 25 twice), restart after a closed
   refusal 19, owner-ended 11, containment 21 (proof 6 / receipt 14 / owner-ended 1), refusals 0/1/2+ =
   67/20/67, EOF before exit 12, receipt categories 69 ordinary / 26 forced / 59 absent.
   The bound properties exhaust four step values each, and the frame-path property exhausts 14 combinations.
   The deadline condition is nonvacuous in the 21 contained examples; backlog preservation is exercised in
   56. Schedules without a loss make the no-post-loss-frame check vacuous individually, so it must not be
   described as every example exercising a refusal. The independent execution counter in
   `property_counter.py` also passes: **154 actual plays, 69 cases rebuilding a runner, 56 with an entry
   backlog**. Actual refused relay responses per case are **0/1/2+ = 67/72/15**. Thus multiple real refusals
   are covered, but 67/20/67 counts attempted refusal events, including later frames dropped by the permanent
   I/O boundary. The spy observes guardian receipt publication in 90 cases, and synthetic containment
   receipts in 70 (these categories can overlap). The 21 figure counts exit-drain bound containment, not all
   stop escalation. “59 absent” means no scheduled guardian receipt, not necessarily no synthetic `exit.json`.
   These properties use a fake child's immediately coherent `returncode` / `poll` pair, so their schedules
   do not cover Popen wait-lock contention. The real-child regression covers that additional boundary.

   Executing `tools.ci_shard_plugin.{validate_map,plan,predicted_loads,shard_of}` for the checked-in five-shard
   matrix yields **833.2, 833.1, 833.1, 833.1, 833.1 seconds**. None of the four PR files is measured in the
   map; their fallback shards are exit=1, properties=4, recovery=4, review=3. The planner therefore charges
   the widened property zero. Adding the builder's 50–315 seconds to shard 4 gives 883.1–1148.1 seconds
   (14.7–19.1 minutes), below the 45-minute job timeout and the map guard's 22.5-minute threshold. This
   review measured the widened call at **337.51 seconds**, and its whole four-test pytest hold at 433.77
   seconds. The reviewed head has 206 unmeasured files in total, including this property file; neither
   projection accounts for the other 205. The current
   guard cannot establish actual shard headroom; refresh the duration map from CI. There is no evidence here
   that this individual property exceeds CI's 1200-second per-test timeout.
5. **Named mutants: pass.** All five are killed by their named tests, and all five unmutated controls pass.
   The table below distinguishes those production-head mutants from the two candidate-fix reverts.
6. **Release diff: pass.** `git diff f1d2f116e 02720082d -- subfleet` contains only `runner.py` (+95/-22) and
   `relay.py` (+14/-4). Every hunk serves D-RELAY-EXIT1: child-exit evidence on refusal/replay/status,
   permanent I/O closure including titles and handovers, or the drain watermark and clock. Moving the
   I/O-ended check ahead of the title-held close prevents a title hold bypassing that boundary. No unrelated
   hunk. Saved diff SHA-256: `c70e8efc678f33796e5db221ad2769ce06bf9822ae39ea7c53243056c89ad76c`.
7. **Text: qualified, needs correction for the finding and new evidence.** Only C-26.4 and its r1/r2
   change-log line changed relative to `2be106982`; the original D-RELAY-EXIT1 change-log line stands.
   Status proof, the normal append-only backlog, constant 30 seconds independent of customized policy,
   every normal entry and rebuilt clock agree with the implementation and tests. The unqualified promise
   to drain and settle does not cover the metadata-error counterexample. The report's counts reproduce,
   but its refusal counts count scheduled/attempted refusals, not necessarily distinct refused wire frames;
   later attempts after I/O closure are intentionally dropped. “59 absent, 26 forced” are separate receipt
   categories. The 50–315-second timing was historical and needs the new measurement. The sole permitted
   `gh pr view 271 --json body` succeeded; its body matches `pr-body-r2.md` after stripping trailing whitespace.
   Historical runs and the unclassified host failure remain reported evidence, not new host verification.
   The live invariant should scope exit proof to classification time: a later receipt does not erase an
   already classified unproved loss, as the required live-child control explicitly asserts. The fix narrative
   must also acknowledge P2-1: the very `[fake:exit-after-ack]` trigger can still settle `relay-failed`.
8. **E2E handoff: fail; keep the draft.** P2-1 is a deterministic reproduction of a remaining ordering in
   that exact fake-provider scenario. Kernel exit is already observable, but `Popen.poll()` cannot take the
   guardian wait thread's lock and reports `None`. The refusal has no exit flag and status says running.
   `_relay_lost` then permanently selects `relay_failed` / `stop_reason='relay-failed'`. A subsequent receipt
   drains stdout but does not clear that reason; `reconcile.settle` prefers it over EOF's
   `ended-without-result`. Added input
   `test_exit_visibility_after_the_refusal_can_still_settle_as_relay_failed` first characterized that outcome
   with fake child observations. The subsequent real-child wait-lock regression fails at head with a kernel
   exit status of 1 and both statuses `running`. It reproduces the defect's result without running a daemon
   or e2e. It does not prove that the one uncaptured host failure had this particular cause.
   The fake emits acknowledgement and then returns without a result (`interactive_claude.py:359–373`);
   neither an acknowledgement nor a broken pipe proves waitable process exit. With timely exit proof and
   intact fixture output, the tested path ends `ended-without-result`. Timing / infrastructure failures and
   an unproved loss can still produce another result.

   Fix the demonstrated wait-lock race before lifting the draft. The uncaptured failure among 19/20 followed
   by 60/60 cannot be exonerated by those successes. Host evidence
   that would decide it: run the exact handoff test at this head with failure preservation from the first
   iteration; keep daemon.log, stdout/stderr, stdin.jsonl, start/exit/turn receipts, and the refusal/status/
   child-exit observation ordering with message/attempt identity. A minimal content-free trace of the exit
   flag, child poll result and status result/error would separate the remaining ordering from an unrelated
   assertion or timeout. A matching release/control run can classify infrastructure failures. Lanes did not
   run e2e, change PR state, or contact GitHub beyond the allowed body read.

## Mutants

| Mutation | Executed killer / control | Result |
|---|---|---|
| Forget status proof on closed restart | `restart_recovers_exit_without_a_receipt[closed]` | Killed; control passes |
| Forget receipt arm | `every_exit_drain_entry_arms_the_bound[receipt]` | Killed; control passes |
| Forget owner-ended arm | `every_exit_drain_entry_arms_the_bound[owner-ended]` | Killed; control passes |
| Start clock at entry | `a_backlog_on_disk_at_the_proof_is_read_in_full` | Killed; control passes |
| Omit rebuilt entry size | `a_rebuilt_runner_reads_its_whole_backlog_before_the_bound` | Killed; control passes |
| Start clock at entry, against the amended fakes | Five parameter cases in `review_mutants.py::test_deviation_sensitivity` | All survive |
| Remove the kernel-exit peek from the review's proof-source candidate | `test_kernel_exit_proof_candidate_and_mutant[peek-removed]`, real exited fake / held wait lock | Killed; candidate control passes |
| Remove transient-metadata retry from the review's candidate fix | `test_transient_stat_failure_candidate_and_mutant[retry-removed]`, owner-ended regression | Killed; candidate control passes |

The five builder mutations use `docs/reports/2026-10-28-relay-exit-r2/named_mutants.py`. Their optional
property-kill reporting tests were deselected; this review reran the named killers and controls. No production
file is edited by a mutant. The added regression is deliberately red at the reviewed head.

## Holds and cleanup

Detailed inputs, UTC starts, measured loads, file counts and completed process PIDs follow in `holds.md`
and `runs.jsonl`. Tests ran serially, one pytest per shared-lock hold, with a 500–550-second interrupt backstop,
scratch TMPDIR and Hypothesis storage, and bytecode disabled. No fetch, merge, push, real provider,
manual Subfleet CLI invocation, live store access, or sub-agent. The OS denies `ps`/`sysctl`; loads came from
`os.getloadavg()`, and the process ledger records children launched by this review's pytest processes.

The attempted named-file stage/commit failed because the sandbox cannot create the shared Git metadata's
`index.lock`. HEAD stays at `02720082d`; the added test and this report folder remain in the assigned worktree
for salvage. Intended test commit: `test: cover the guardian wait-lock exit race and drain metadata faults (#271 r3)`,
ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

Final test coverage and guarded scratch cleanup are recorded in `holds.md`.

DONE
