# D-RELAY-EXIT1, #271 fix round r2

Base `2be106982` (r1 `8f9f35602` + review r2's red tests `32e703301` + release/217 at `f1d2f116e`). Code head
`5a74be188`; this report and `named_mutants.py` are the docs commit after it. Built 2026-10-28, 11:12Z to 15:15Z,
scratch `$(getconf DARWIN_USER_TEMP_DIR)sf-r271f2`. No fetch, push, GitHub call, real `subfleet` CLI or provider.

## Decisions as built

1. **P2-1** (`14eba7e91`): `_handshake` sets `exit_proved_at` (if unset) whenever a fresh status says the child
   `exited`, whatever the log shows: on a first runner asked again after a lost answer and on a rebuilt one.
   `test_restart_recovers_exit_without_a_receipt[closed]` is green; `[failed]` stays green. New:
   `test_a_status_naming_the_child_exited_proves_the_exit_on_every_handshake[first|rebuilt]` (no refusal, no receipt).
2. **P3-1, P3-2** (`baae01bd2`): `_drain_stdout` replaces the inline loop. Every entry (a proved exit, the
   receipt or an owner-ended attempt) records `drain_entry_size`, the stdout size on disk then. All of that is
   read however long it takes. `EXIT_DRAIN_S` starts at `drain_caught_up_at`, the first check at which the reader's
   position (`offset + len(partial)`) has reached the entry size, and is checked after each chunk. A rebuilt runner
   records its own entry and clock. `test_a_backlog_on_disk_at_the_proof_is_read_in_full` and
   `test_every_exit_drain_entry_arms_the_bound[receipt|owner-ended]` are green. New:
   `test_a_rebuilt_runner_reads_its_whole_backlog_before_the_bound`.
3. **`EXIT_DRAIN_S`** (`c1b337b9b`): still `30.0`, still a constant. Its comment says it is chosen equal to the
   default of policy `conversations.stop_contain_after_s` (C-24.7) and does not follow a customised policy.
4. **Contract** (`a94158693`): C-26.4 and its change-log line (r1's, now dated for both rounds). No other clause.
5. **Properties** (`50a973599`, `c714c32f6`, `86136ecb5`, `5a74be188`): see below.

## Deviation: the decided clock and four tests' fake readers

Decision 2 as written cannot turn `test_every_exit_drain_entry_arms_the_bound[receipt|owner-ended]` green as the
tests stood. Their fake `_read_stdout` returned True without reading, so the reader's position never moved.
Counterexample for `[receipt]`: entry size 291 bytes (the initialize and accepted rows), position 0 for all 90
fake reads, the clock never started, no containment. Three tests that were green at r1 used the same fake and
turned red the same way: `test_a_receipt_after_the_drain_bound_does_not_extend_it`,
`test_restart_in_the_active_drain_rearms_from_available_proof` and
`test_proved_exit_drain_is_bounded_on_the_fake_clock[at-bound]`. No real read can report progress without
advancing the position. The smallest change keeping the decision: each of these fakes now calls the real reader
(reads what is on disk) before returning its scripted value. No assertion changed. These amended cases guard the
bound's arming, a late receipt and rearming, not when the clock starts: review r3 ran all five parameter cases
against `clock-started-at-entry` and all five survived. The backlog tests below (`test_a_backlog_on_disk_at_the_proof_is_read_in_full`,
`test_a_rebuilt_runner_reads_its_whole_backlog_before_the_bound`) guard catch-up timing; each kills that mutant.

## Red run

`tests/fake/test_turn_relay_exit_review.py` at `2be106982`: **4 failed, 5 passed** (direct, 11:54:01Z, load
170). The failures are the four review r2 named; the values match its report (`elapsed 36.0` with no proof;
`elapsed 90.0` for both drain entries; `read 7800 of 10661`, `stdout-drain-timeout`). The lock had admitted
nothing for 30 minutes (queued 11:20:53Z), so I stopped my waiter and ran it directly.

## Holds

Runner code is identical at `a94158693` and `5a74be188`; the commits between change only the properties file.

| Hold | Head | Start (UTC), slot | Load (1 min) | Result |
|---|---|---|---|---|
| red | `2be106982` | 11:54:01, direct | 170 | 4 F, 5 P (review file) |
| green + guards | `a94158693` | 12:29:45, A | 78 | 102 P |
| mutant killers + controls | `a94158693` | 12:40:50, B | 54 | 10 P |
| r2 unrun, `test_probe_turn` (43) | `a94158693` | 12:42:00, B | 59 | 42 P, 1 F (known, below) |
| r2 unrun, the other 50 | `a94158693` | 12:51:28, A | 115 | 50 P (dots); see deviations |
| P1 runner, replay, relay, attachment replay, FIFO reads | `a94158693` | 13:04:10, B | 75 | 185 P |
| P2 titles, reconcile, Claude driver | `a94158693` | 13:10:53, B | 34 | 194 P |
| P3a service, first 50 | `a94158693` | 13:12:24, B | 38 | 50 P |
| P3b service, last 50, steer service | `a94158693` | 13:13:49, B | 57 | 95 P |
| P4 handoff, briefs, dispatch races, peers | `a94158693` | 13:18:10, B | 58 | 129 P |
| P5 Codex turn files | `a94158693` | 13:28:27, A | 85 | 217 P |
| P6a diff, store, state reads, catalog | `a94158693` | 13:36:59, A | 73 | 103 P, 1 deselected (ran above) |
| P6b catalog lifecycle, connections, history, redact, refresh, status, stream, waits, wakes | `a94158693` | 13:41:18, A | 50 | 118 P, 4 deselected |
| P7 CLI turns, enrollment, retention turns, scheduler turns, overlap, MCP scope, pools, wakes fake, trees | `a94158693` | 13:46:04, A | 45 | 87 P, 2 deselected |
| property kills, 3 mutants | `a94158693` | 13:49:08, B | 14 | 3 killed |
| property kills, 2 mutants | `a94158693` | 13:50:05, B | 14 | 1 killed, owner-ended arm not |
| green + guards | `c714c32f6` | 14:08:14, B | 40 | 102 P |
| mutant harness | `c714c32f6` | 14:10:28, A | 41 | 15 P; property kills 4 (not the status proof) |
| green + guards | `86136ecb5` | 14:17:32, A | 53 | 101 P, 1 F (coverage assertion; fixed at `5a74be188`) |
| green + guards | `5a74be188` | 14:32:41, A | 51 | 72 P, then stopped at 17.5 min (load 128) |
| guards | `5a74be188` | 14:53:22, A | 127 | 63 P |
| mutant harness | `5a74be188` | 15:01:55, A | 84 | 15 P; property kills all 5 |
| `test_probe_turn` rerun | `5a74be188` | 15:07:16, A | 84 | 1 F (known, below) |

Every file under `tests/unit` and `tests/fake` whose name contains `relay`, `runner`, `turn` or `conversation`
(53 files, 1,306 nodes) ran, in the holds above. At the final head: this PR's four files 39 of 39, guards 63 of 63.

**Known failure, not this PR's:** `tests/fake/test_probe_turn.py::test_no_sequence_of_probe_outcomes_lets_a_later_job_take_a_waiters_probe`
failed at load 58 (`waited 180, ran 184`) and on its one rerun at load 84 (`waited 61, ran 66`), against its
2 s tolerance (`test_probe_turn.py:884`). It is the scheduler's probe-retry clock, release code this PR does not
touch (the PR diff is `runner.py` and `relay.py`); round 1's brief names it as tracked separately.

## Properties

`test_loss_interleavings_keep_one_outcome_and_all_stdout` draws, per example: 0–3 refusals (`failed` or `closed`,
sent through the runner's own outbox to the real relay), 0–2 restarts (rebuilt runners; owner-ended replays once
the child exited), a receipt or none (forced only when nothing else could end the turn), EOF anywhere after the
bytes, a block of 70 writer-open reads, and slow readers (8, 64 or 1 MiB a read, 0.01–1 fake s each). Four
examples are pinned: review r2's P2-1, and a writer outliving the bound after a proof, a receipt and an
owner-ended replay. It asserts per run at least one restart, one restart after a `closed` refusal, one backlog
and a containment after each entry kind.

Observed at `5a74be188` (154 examples, 4 pinned): backlog at a drain's entry 56; restarts 69 (one 44, two 25;
after a `closed` refusal 19); owner-ended replays 11; contained 21 (after a proof 6, a receipt 14, an owner-ended
replay 1); EOF before the exit 12; a receipt never 59 (26 forced). Refusals, corrected in fix r3 to review r3's
executed counter (`docs/reports/2026-10-28-relay-exit-r3/property_counter.py`): refused relay answers per example
0/1/2+ = 67/72/15. The 67/20/67 first given here counted refusal events the schedules drew, including later
frames the permanent I/O boundary never sends.

## Mutants

All five are killed by their named tests, which pass unmutated (`named_mutants.py`).

| Mutant | Killing test | Property |
|---|---|---|
| Status proof forgotten on a closed restart | `test_restart_recovers_exit_without_a_receipt[closed]` | killed |
| Receipt arm forgotten | `test_every_exit_drain_entry_arms_the_bound[receipt]` | killed |
| Owner-ended arm forgotten | `test_every_exit_drain_entry_arms_the_bound[owner-ended]` | killed |
| Clock started at entry, not at catch-up | `test_a_backlog_on_disk_at_the_proof_is_read_in_full` | killed |
| Entry size never recorded on a rebuilt runner | `test_a_rebuilt_runner_reads_its_whole_backlog_before_the_bound` | killed |

## Other deviations

- **Outside the lock:** only the red run. My dev iterations (single files or nodes, seconds to 5 minutes each)
  also ran directly and are not counted above.
- **Past 10 minutes:** the 50-node unrun hold ran about 11 minutes (its last node, a `test_turn_overlap` property,
  passed; my interrupt at the ceiling landed in pytest's session cleanup, so it printed no summary line). The
  final green hold reached 17.5 minutes at load 128 before I stopped it; its four PR files had all passed, and
  the guards it had not finished ran in their own hold.
- **Left out:** `tests/fake/test_capacity_steer_daemon.py::test_r3_hold_back_excludes_models_steered_away_from_any_older_job`
  (job capacity steering; it ran past 5 minutes in review r2), as the brief directs.
- **Not run:** e2e (lanes can't run it). The derandomized property draws shift with any edit to the test.

## Fix round r3

Base `c3f06bd02` (round 2's `02720082d` plus review r3's regression file and report, committed by the hub). Built
2026-10-28, 19:44Z to 23:52Z, scratch `$(getconf DARWIN_USER_TEMP_DIR)sf-r271f3`. No fetch, push, GitHub
call, real `subfleet` CLI, provider or live store. Harnesses for this round are in
`docs/reports/2026-10-28-relay-exit-r3-fix/`.

### Decision 1, the kernel exit peek: not built (stopped, as the brief directs)

`os.waitid` does not exist on CI's Python 3.12. CPython 3.12 leaves it out of its build on Apple platforms: on this
host `/opt/homebrew/bin/python3.12` (3.12.14) reports `sysconfig` `HAVE_WAITID=1` and has no `os.waitid`, while
3.13.15 and 3.14.7 have it. `os.WNOWAIT`, `os.P_PID` and `os.WEXITED` exist on all three. CI's matrix is macOS 15
with 3.12 and 3.14 (`.github/workflows/tests.yml`), so the decided helper cannot run on the 3.12 job, and
production code is unchanged. Two consequences for the hub:

- Review r3's committed regression
  `test_a_real_exited_fake_is_not_a_relay_failure_while_the_guardian_wait_holds_its_lock` calls `os.waitid` itself.
  On 3.12 it fails with `AttributeError: module 'os' has no attribute 'waitid'` whatever the fix (executed here on a
  3.12 scratch environment: 23 passed, that 1 failed). It needs a 3.12 path or a skip in any case.
- The same system call is reachable on 3.12 through libc. `decision1_candidate.py` holds the decided helper with
  both bindings (`os.waitid`, else libc `waitid` through ctypes; the repo already binds libc this way in
  `subfleet/procs.py`), patched over the three RelayServer sites for one test at a time:

| Case | 3.14.7 (`os.waitid`) | 3.12.14 (libc `waitid`) |
|---|---|---|
| a running child, and one that closed its stdin, read as not exited (lock free, wait lock held) | 2 pass | 2 pass |
| after peeks under a held wait lock, `Popen.wait` returns the child's real status (3) | pass | pass |
| mutant: the peek reaps (`WNOWAIT` dropped): `Popen.wait` records 0 | killed | killed |
| a child the guardian's wait already reaped (ECHILD) reads as exited | pass | pass |
| review r3's wait-lock regression with the helper at the three sites | pass (`ended-without-result`) | skipped (the test needs `os.waitid`) |
| mutant: the helper without the peek | killed (`relay-failed`) | skipped |

Sites the helper would replace (none were changed): `subfleet/relay.py:451` (`status`), `:458` (`_refusal`) and
`:499` (signal delivery reads `poll() is None` as "still running"). `subfleet/guardian.py` reads no `poll()`; its
`child.wait()` at `:295` and `:306` is the reaper and stays the only one. Two fakes give the relay a child with
`poll` and no `returncode` (`tests/fake/test_wall_stop_deliveries.py:41`, `tests/fake/test_wall_cancel_races.py:256`),
and none has a `pid`, so a built helper should read `returncode` with `getattr` and skip the peek without an `int`
pid, as the candidate does.

### Decision 2, a failed entry stat: built (`3a82d8770`), with one deviation

`_stdout_size` tries a recoverable `OSError` (anything but `FileNotFoundError`, which still means size 0) again up
to `STDOUT_STAT_RETRIES` (3) times, `POLL_S` (50 ms) apart on the runner's own `time.sleep`. A size still unknown is
`None`, never zero, and a warning is logged. Review r3's two cases
(`test_a_transient_entry_stat_failure_still_settles_the_exited_turn[proof|owner-ended]`) are green. Added: the
retry count (0, 3 and 4 failures, and a missing file), every stat failing with a backlog on disk at a proof and at
an owner-ended entry (the whole backlog read, the provider's result decides, no containment), and the case below.

**Deviation.** With the size unknown, the decided catch-up, the first empty read, never comes while a descendant
writes without pause, so the bound never starts. Counterexample
(`test_an_unknown_entry_size_still_bounds_a_writer_that_never_pauses`, one line appended before each read): under
the decided rule the drain made 120 reads with no catch-up and no containment. The smallest change keeping the
decision: the first read that reaches the end of the file catches up, a read short of a whole chunk (`READ_CHUNK`)
as well as an empty one. For an append-only file either one has read everything on disk at the entry. With it the
case is contained exactly `EXIT_DRAIN_S` after that read.

### Decision 3, a runner that ends always settles: built (`668ff84e9`, `f046750fd`), with test changes

Yes: before this round any unexpected exception in `_run` (a stdout read failing with EIO, a driver defect) left
the runner finished with no outcome and no `on_outcome` call. `_run`'s handler now logs it (`_failed`, as before)
and calls `_settle_failed`, which settles as `_relay_lost` settles an unproved loss: relay I/O ended, unwritten
steers missed, stop reason `relay-failed` unless the turn already had an outcome or a stop reason, containment
requested at once unless the exit receipt exists or the job store ended the attempt (nothing escalates a stop
after this), then the driver's EOF outcome (or, if the driver fails too, the outcome EOF gives) and the report.
It does nothing once the service is closing (`stop()`, or a `store-closed` refusal): a later daemon adopts or
replays the attempt (C-26.6). Each failing step is logged and the next is tried. New tests: the failure while the
child lives, after a proof, after the receipt and in an owner-ended replay; while the service closes; the driver
failing at EOF too; and a result read before the failure, which still decides the turn (`complete`).

Test changes it needed (`f046750fd`), which the hub should weigh:

- Six tests modelled a daemon crash or restart as a bare exception out of the runner's loop: the properties'
  `Restart`, review r2's `test_restart_in_the_active_drain_rearms_from_available_proof` and the three "simulated
  daemon crash" sites in `tests/unit/test_turn_replay.py`. They now raise the closing store's refusal
  (`store-closed`), which the runner leaves to a later daemon as a crash does. No assertion changed. `Restart`
  changes at module level only, so the property's derandomized draws, seeded from the test's own source, are
  the same.
- **A release test's pinned behaviour changed.** `test_an_ended_attempt_is_replayed_once` pinned "a replay whose
  runner fails at once ends without its report; a later daemon tries again". Now a defect settles the message in
  that replay, from what it read. Here it read nothing, so the message settles `delivery-unknown` and the
  conversation is blocked for the person, where the next daemon's replay had settled it `ended-without-result`.
  A crash still leaves it to a later daemon. Both are now cases of the test.

Not covered, since it needs a change outside the runner: a failure inside `_report` itself after it marks the
outcome reported (the `turn.json` write, or the service's `on_outcome`). The runner then finishes with no report,
as before. Covering it, and keeping a transient failure from settling with less evidence than a later replay would
have, needs the service to retry a replay whose runner finished unreported (`ConversationService._replay_unsettled`
marks an attempt `_replayed` once per service, `service.py:2902-2909`), for example with a bounded backoff, rather
than the runner settling at once. Not built; for the hub.

### Text

C-26.4 (`2d72a1074`) now: states the unknown-entry-size rule; scopes exit proof to when a loss is classified (a
receipt or status that arrives later lets the drain read stdout but does not undo `relay-failed`; review r3 check
7); and states decision 3's settle-on-failure. Its change-log line covers reviews r1 to r3. It does not name the
kernel peek: decision 1 is not built. The Properties section above now gives review r3's executed refusal
counts, and the Deviation section says what the amended cases guard.

### Runs

One pytest per hold, serial, `TMPDIR` and Hypothesis storage in scratch, bytecode off, an in-process alarm at
540-590 s. Counts are passed/failed/skipped per file, from each hold's JUnit file. "Direct" means my waiter had
waited 30 minutes without admission (load 100-135, 7-10 waiters, second slot closed), so I stopped it and ran the
files outside the lock, as the lane rules direct. Code is the same at `f046750fd` and `2d72a1074` (docs only).

| Hold | Head | Start (UTC), slot | Load (1 min) | Per file |
|---|---|---|---|---|
| red | `c3f06bd02` | 20:02:41, B | 102 | r3 6/3/0: the two transient-stat cases and the wait-lock case, as review r3 found |
| PR files | `668ff84e9` | 20:58:12, B | 106 | exit 10/0/0, recovery 13/0/0, review 11/1/0, r3 23/1/0 (found decision 3's test conflict) |
| A: PR files, mutants, candidate | `f046750fd` | 22:47:46, direct | 100 to 133 | exit 10/0/0, recovery 13/0/0, review 12/0/0, r3 23/1/0 (the wait-lock case: decision 1); r2 `named_mutants.py` 10/0/0; `fix_r3_mutants.py` 4/0/0; `decision1_candidate.py` 8/0/0 |
| B: widened property | `2d72a1074` | 23:31:52, direct | 123 to 57 | properties 1/0/0 (`--durations`: 85.5 s call) |
| C: properties, replay, relay reruns | `2d72a1074` | 23:02:12, A | 106 | properties 3/0/0, `test_turn_replay.py` 34/0/0, `test_relay.py` 3/3/0 (rerun of the five below) |
| runner, relay | `668ff84e9` | 21:38:36, direct | 131 | `test_relay.py` 25/5/0, `test_turn_runner.py` 67/0/0, then cut by the alarm |
| D: runner, attachment replay | `2d72a1074` | 23:36:21, direct | 50 | `test_turn_runner.py` 83/0/0, `test_turn_attachment_replay.py` 13/0/0 |
| E: guards | `2d72a1074` | 23:33:24, direct | 61 | `test_state_contract.py` 37/0/0, `test_test_module_names.py` 1/0/0, `test_invariants_index.py` 25/0/0, `test_ci_shard_plugin.py` 48/0/0 |
| F: drivers | `2d72a1074` | 23:36:37, direct | 55 | `test_claude_turn.py` 69/0/0, `test_codex_turn.py` 67/0/0, `test_codex_turn_completed.py` 41/0/0 |
| G: turns, guardian | `2d72a1074` | 23:36:47, direct | 57 | `test_codex_turn_completed_fake.py` 109/0/0, `test_cli_turns.py` 23/0/0, `test_enrollment_runner.py` 7/0/0, `test_retention_turn_contract.py` 2/0/0, `test_retention_turn_workdirs.py` 1/0/0, `test_retention_turns.py` 7/0/0, `test_scheduler_turns.py` 10/0/0, `test_attempt_tmpdir_guardian.py` 2/0/0, `test_guardian.py` 13/0/0, `test_guardian_qos.py` 14/0/0 |
| H: listing, retention | `2d72a1074` | 23:09:59, B | 105 | `test_turn_listing.py` 9/0/0, `test_turn_retention_cwd.py` 5/0/0, then cut by the alarm at 570 s |
| I: retention properties, probe | `2d72a1074` | 23:31:47, A | 129 | `test_enrollment_guardian.py` 1/0/0, `test_probe_turn.py` 42/0/0 (the known slow node deselected), both retention properties 1/0/0 |

- **Dev runs, outside the lock and not counted above:** single files or nodes while building. Two of them are
  evidence: the r3 file on a 3.12 scratch environment (23/1/0; the 1 is the `os.waitid` AttributeError) and
  `decision1_candidate.py` on 3.12 (6/0/2) and 3.14 (8/0/0).
- **`test_relay.py`'s stop-bound tests** failed at load 131 (`test_stop_cuts_an_exchange_a_retained_stdin_reader_holds`,
  `test_stop_still_answers_an_exchange_that_finishes_inside_its_bound`,
  `test_a_stalled_failure_record_cannot_hold_stop[journal-write]`,
  `test_stop_keeps_its_bound_when_its_cleanup_thread_cannot_start`, `test_stop_returns_within_its_bound_whatever_stalls`),
  and three of them again on the one rerun at load 106 (`still_answers`, `stalled[journal-fsync]`,
  `returns_within_its_bound`), with "the acknowledgement was not sent before stop returned" and "timed out". They
  can't reach this round's change: `test_relay.py` imports only `subfleet.relay` and `tests/relay_stalls.py`,
  and the round changes only `runner.py`. Not chased; no bound was loosened.
- **Property timing:** the widened property's call took 85.5 s here (load 123 falling to 57). Review r3 measured
  337.5 s. Its observed counts are round 2's exactly (154 examples, backlog 56, restarts 69, contained 21, refusal
  events 67/20/67), so `Restart`'s change left the draws as they were.

### Mutants

| Mutant | Killer | Result |
|---|---|---|
| Round 2's five (`named_mutants.py`) | their named killers | all 5 killed, 5 controls pass (hold A) |
| Unknown backlog read as zero | `test_every_entry_stat_failing_still_reads_the_whole_backlog_and_settles[proof]` | killed; control passes |
| Short-read catch-up removed (the decision as written) | `test_an_unknown_entry_size_still_bounds_a_writer_that_never_pauses` | killed; control passes |
| The helper without the peek (candidate) | review r3's wait-lock regression | killed on 3.14 (`relay-failed`); not runnable on 3.12 |
| A peek that reaps (`WNOWAIT` dropped; candidate) | `test_a_peek_never_changes_the_status_the_guardian_records` | killed on 3.14 and 3.12 (wait returns 0, not 3) |

### Not run

- After the 4-hour mark, as the brief directs (my waiters stopped unadmitted at 23:44Z):
  `test_turn_retention_job_directory.py` (2), `test_turn_retention_quarantine.py` (10), `test_turn_trees.py` (18),
  `test_turn_wait_reasons.py` (7), `tests/unit/test_turn_overlap.py` (10), and the one `test_turn_retention_cwd.py`
  node hold H's alarm cut (`test_a_missing_actual_cwd_waits_for_workspace_even_when_its_git_hold_exists[case-alias]`).
- `test_probe_turn.py::test_no_sequence_of_probe_outcomes_lets_a_later_job_take_a_waiters_probe`, the known
  load-timing failure, was deselected from hold I and not run.
- e2e and frontend: lanes can't run them. CI runs everything, including the 3.12 job this round can't change:
  review r3's wait-lock regression fails there until decision 1 is settled.

## Fix round r4

Base `3769a17e5` (fix round r3's head). Built 2026-10-29, 00:20Z to 01:36Z, scratch
`$(getconf DARWIN_USER_TEMP_DIR)sf-r271f4`, with one scratch venv per Python synced from the tree's lock (3.14.7
and 3.12.14). No fetch, push, GitHub call, real `subfleet` CLI, provider or live store. The hub's rulings on round
3 stand: decision 2's deviation and decision 3 as built; the two gaps round 3 described (`_report` failing, and
`_replay_unsettled` retrying an unreported replay) are a follow-up, not built here.

### Decision 1, the kernel exit peek: built with two bindings (`0982ab220`)

`subfleet/relay.py:262` `child_exited(child)` says whether the guardian's child has exited:

- Popen's own record first: `returncode` read with `getattr`, then `poll()`.
- Otherwise a peek, `waitid(P_PID, pid, WEXITED | WNOHANG | WNOWAIT)` (`_PEEK_FLAGS`), that never reaps. It goes
  through `os.waitid` where `os` has it (`_waitid_os`), and otherwise through libc's own `waitid` by ctypes with
  Darwin's `siginfo_t` (`_waitid_libc`). This host's 3.12.14 has no `os.waitid`. `EXIT_PEEK` names the binding,
  chosen once at import: `os.waitid` on 3.14, `libc waitid` on 3.12. The libc binding is bound on macOS on every
  Python, so the tests reach it on 3.14 too.
- `ChildProcessError` (ECHILD) reads as exited: the guardian's wait has reaped the child. Any other `OSError`
  reads as "not shown to exit" and is written once per process, as one fixed line naming the errno, by the raw
  descriptor-2 write the relay's cut line already uses (`PEEK_FAILURE_LOG_LINE`).
- A child without an integer pid above 0 (the fakes in `test_wall_stop_deliveries.py`,
  `test_wall_cancel_races.py` and the relay-exit fixtures) is not peeked.

It replaces `poll()` at the three sites: `status` (`:542`), `_refusal`'s `child_exited` flag (`:549`) and signal
delivery's still-running check (`:590`). No peek reaps. The helper keeps Popen's `poll()`, which reaps while Popen's lock is free
(before `guardian.py:295`'s `child.wait()` takes it) and stores the status, as the three sites' `poll()` did;
`send_signal` polls the same way. `relay.py` calls `child.poll()` only in the helper, and `guardian.py` calls
`wait()` only at `:295` and `:306`.

**One visible change at the signal site.** A signal frame for a child that has exited but that the guardian has
not yet reaped is now answered `no-child`. Before, with the wait lock held, `poll()` said "running" and the signal
went to a zombie (`ok: true`). With the lock free, `poll()` reaped it and the answer was already `no-child`, so
the answer no longer depends on the lock. The runner already reads `no-child` on a signal as "the child already
exited": it records the stop as not delivered and goes on (`runner.py:955-972`).

Evidence for the binding: the ctypes binding reads `si_pid` and `si_status` (5 for `exit 5`, 9 for `kill -9`) on
both Pythons, and a child it has read is still in the process table (`kill(pid, 0)` succeeds on a zombie and fails
once it is reaped, checked on this host).

### Tests (`cad35afa2`, `e9bb55001`, `87f73dc1a`, `85a437a66`)

- **Review r3's wait-lock regression** now proves the fake's exit through `child_exited` (it records the binding's
  `si_pid` and `si_status` for its unchanged assertions), not `os.waitid`, so it runs on 3.12. Assertions unchanged.
- **`tests/fake/test_turn_relay_exit_peek.py`**, with real `/bin/sh` children, each started and reaped by its test.
  It checks the binding chosen at import; each binding reading an exit without reaping it; live children (one with
  stdin closed) never reading as exited, lock free or held; a peek never changing the status the guardian's wait
  records; ECHILD reading as exited; another `OSError` proving nothing and logged once (EINVAL, EIO, EPERM);
  pid-less fakes never peeked; the three sites reading an exit the held wait lock hides, and their live-child
  controls (`running`, no `child_exited`, the signal delivered). The ctypes cases run on every Python; the
  `os.waitid` cases skip on 3.12 with the reason.
- **The property** (`test_a_peek_reads_exit_only_after_it_and_never_reaps`, per binding): 1 to 4 children per
  example, all live at once and reaped at its end. Each ends with a random exit code (0 to 255) or signal (9, 15
  or 2), and blocks on a gate until the test opens it, so until then it is provably alive. Its wait lock is free;
  held; held while the test, as the guardian's wait, reaps it once its exit is read and before Popen stores the
  status (the ECHILD window); or held by a real guardian thread in `child.wait()`. The gate opens before the peeks
  or, from another thread after 0 to 20 ms, while they run. It checks that no read that ended before its gate
  began to open says "exited", that once read an exit stays read, that a child under a held lock is still a
  zombie after every peek, and that the guardian's wait (or the test's, then Popen's stored status) returns the
  child's real status. The peek loop pauses 1 ms a pass; an earlier spinning version made 1.1 to 2.7 million reads
  per binding and kept a core busy.

Observed at the final holds (the draws are derandomized and the same on both Pythons; the read counts depend on
timing). Per binding: 60 examples, 155 children (examples with 1/2/3/4 children: 18/3/25/14), lock free 28, held
53, reaped-unpublished 42, guardian thread 32; gate before 63, during 92; exit codes 67, signals 88; 53 never-reap
checks under a held lock (52 with a nonzero status).

| Binding, Python | Peeks: exited / running / ECHILD | Reads (before the gate opened) | Call, load |
|---|---|---|---|
| libc, 3.14 | 243 / 1075 / 87 | 1245 (660) | 19.7 s, 33 to 41 |
| `os.waitid`, 3.14 | 220 / 1052 / 89 | 1222 (649) | 20.4 s, 33 to 41 |
| libc, 3.12 | 216 / 1001 / 90 | 1171 (596) | 20.7 s, 33 to 39 |

At load 13 the same calls took 9.3 to 11.0 s.

### Decision 1's mutants (`158127b48`)

`decision1_candidate.py` is deleted. Its deletion was staged early and landed in `87f73dc1a`, whose message does
not say so. `fix_r3_mutants.py` keeps decision 2's two mutants and gains decision 1's eight, each compiled from
`relay.py`'s own source with one change (or, for the reaping peek, `_PEEK_FLAGS` without `WNOWAIT`), with the
module's globals, so a test's patches reach it. Killers peek through the binding the interpreter uses, except the
ctypes mutant's. It also reports whether the property kills the peek mutants.

| Mutant | Killer | 3.14 | 3.12 |
|---|---|---|---|
| The helper without its peek (round 3) | review r3's wait-lock regression: the helper never shows the exit, so it fails at its 60 s wait | killed | killed |
| A peek that reaps (round 3) | `test_a_peek_never_changes_the_status_the_guardian_records` | killed | killed |
| `status` left on `poll()` | `test_every_relay_site_reads_an_exit_the_guardian_wait_hides` | killed | killed |
| The refusal flag left on `poll()` | the same | killed | killed |
| Signal delivery left on `poll()` | the same | killed | killed |
| ECHILD read as "not exited" | `test_a_child_the_guardian_reaped_reads_as_exited` | killed | killed |
| Another `OSError` read as "exited" | `test_another_peek_failure_proves_nothing_and_is_logged_once[EIO]` | killed | killed |
| The ctypes binding without `WNOWAIT` | `test_a_binding_reads_an_exit_without_reaping_it[exit 5-libc-waitid]` | killed | killed |
| Decision 2's two (round 3) | their named killers | killed | killed |
| Round 2's five (`named_mutants.py`) | their named killers | killed | killed |

Every control passes unmutated. The property also kills the helper without its peek, the reaping peek, ECHILD read
as "not exited" and the ctypes binding without `WNOWAIT`, on both Pythons.

### `test_daemon_log_sites.py`'s two failures in round 3's dev run

| Tree | Start (UTC), slot | Load | Result |
|---|---|---|---|
| head (code of `0982ab220`) | 01:18:16, A | 12.6 | 83/0/0 |
| `c3f06bd02`, export with the worktree's venv | 01:19:08, A | 11.3 | 78/0/5: the five real-daemon tests skip, since the interpreter's `subfleet` is not the export's |
| `c3f06bd02`, export with a venv bound to it | 01:20:51, A | 10.6 | 83/0/0 |
| `3a82d8770` (decision 2 alone, the committed state nearest round 3's dev run), bound venv | 01:21:37, A | 10.9 | 83/0/0 |

Not reproduced, and no record names them: round 3 ran `-q`, stopped the run before pytest's summary, and removed
its scratch. What the file and the code show:

- **Most likely, the two `daemon start` tests:** `test_under_daemon_start_the_daemon_s_own_refusal_reaches_daemon_log_scrubbed`
  and `test_under_daemon_start_an_uncaught_exception_reaches_daemon_log_scrubbed`. They are the file's only tests
  with a wall-clock bound that load can reach: the started `subfleetd` must fail within `cmd_daemon_start`'s fixed
  10 s (`cli.py:2869`), or the CLI reports "did not appear within 10s" without the exit status they assert. They
  took 1.28 s and 0.34 s at load 11; round 3's dev run was outside the lock at load 100 to 135. The lane rules list a
  similar wait (the fake harness's 5 s or 10 s daemon start) as a known load failure. This is inferred from the
  bound, not observed.
- **They don't reach this PR's code.** The daemon refuses or fails at its start, and importing `subfleet.daemon`
  and `subfleet.cli` loads neither `subfleet.relay` nor `subfleet.conversations.runner` (checked on this host).
  The PR changes only those two modules under `subfleet/`.
- **The other way two could fail** is round 3's own uncommitted code at the time (the two runner tests drive `_run`'s
  handler and `_failed`, and the lint test reads every daemon module's log lines). Every committed state passes.

No fix: this PR did not cause them.

### Text (`65b9d510e`, this section)

C-26.4: the relay's reply, a fresh `status` or the exit receipt proves an exit, and the relay learns of it without
reaping the child (Popen's record, or `waitid` with `WNOWAIT` through either binding); ECHILD means the guardian's
wait reaped it; any other failure proves nothing and is logged once; no peek reaps, and Popen stores the real status whoever reaps; a
signal frame for an exited child is answered `no-child`. One change-log line.

### Runs

One pytest per hold, all under the suite lock (none direct), `TMPDIR` and Hypothesis storage in scratch. A driver
stopped its own waiter if it was not admitted in time, so no hold ran past the tool's 10-minute bound. Counts are
passed/failed/skipped per file, from each hold's JUnit file; load is the 1-minute average at admission (and its
range while running).

| Hold | Python, code | Start (UTC), slot | Load | Per file |
|---|---|---|---|---|
| red | 3.14, `3769a17e5` + `e9bb55001`'s tests | 00:48:41, B | 32.7 (31) | wait-lock regression 0/1/0 (`AttributeError`: no `child_exited`); peek 0/27/0 |
| red | 3.12, the same | 00:51:57, B | 15.0 (14) | wait-lock regression 0/1/0 (the same); peek 0/18/9 |
| A: PR exit files | 3.14, `158127b48` | 01:05:09, B | 13.5 (13 to 15) | exit 10/0/0, peek 27/0/0, r3 24/0/0 (the regression green), recovery 13/0/0, review 12/0/0 |
| A: PR exit files | 3.12, `158127b48` | 01:05:48, B | 13.6 (13) | exit 10/0/0, peek 18/0/9, r3 24/0/0 (the regression green), recovery 13/0/0, review 12/0/0 |
| B: properties | 3.14 | 01:06:13, A | 12.9 | properties 4/0/0 (widened call 6.1 s) |
| B: properties | 3.12 | 01:06:37, A | 12.3 | properties 4/0/0 (widened call 10.7 s) |
| C: probe, replay | 3.14 | 01:06:52, A | 12.4 (12 to 13) | `test_probe_turn.py` 43/0/0, `test_turn_replay.py` 34/0/0 |
| C: probe, replay | 3.12 | 01:08:01, B | 12.3 (12 to 13) | `test_probe_turn.py` 43/0/0, `test_turn_replay.py` 34/0/0 |
| D: relay, guardian, runner | 3.14 | 01:08:51, B | 13.3 (13 to 14) | `test_relay.py` 30/0/0, `test_guardian.py` 13/0/0, `test_turn_runner.py` 83/0/0 |
| D: relay, guardian, runner | 3.12 | 01:09:37, A | 13.8 (14) | `test_relay.py` 30/0/0, `test_guardian.py` 13/0/0, `test_turn_runner.py` 83/0/0 |
| E: round 3's unrun list | 3.14 | 01:10:26, A | 13.9 (14) | `test_turn_retention_job_directory.py` 2/0/0, `test_turn_retention_quarantine.py` 10/0/0, `test_turn_trees.py` 18/0/0, `test_turn_wait_reasons.py` 7/0/0, `test_turn_overlap.py` 10/0/0, `test_turn_retention_cwd.py` `[case-alias]` node 1/0/0 |
| M: mutants | 3.14, `85a437a66` | 01:13:56, A | 10.5 (10 to 11) | `fix_r3_mutants.py` 24/0/0, `named_mutants.py` 10/0/0 (its informational property report deselected) |
| M: mutants | 3.12, `85a437a66` | 01:16:00, A | 11.2 (10 to 13) | `fix_r3_mutants.py` 24/0/0, `named_mutants.py` 10/0/0 |
| guards | 3.14, `65b9d510e` | 01:24:49, A | 15.8 (16 to 27) | `test_state_contract.py` 37/0/0, `test_test_module_names.py` 1/0/0, `test_invariants_index.py` 25/0/0 |
| final | 3.14, `65b9d510e` | 01:26:16, A | 32.6 (33 to 41) | peek 27/0/0, r3 24/0/0 |
| final | 3.12, `65b9d510e` | 01:27:52, A | 38.5 (33 to 39) | peek 18/0/9, r3 24/0/0 |

- The PR's files: `git diff --name-only 676906f85 HEAD -- tests` names the six `test_turn_relay_exit*.py` files,
  `test_probe_turn.py` and `tests/unit/test_turn_replay.py`, all in holds A to C on both Pythons.
- `test_relay.py`'s stop-bound tests, which failed at load 100 to 131 in round 3, all passed here at load 13 on
  both Pythons. No bound was loosened.
- The first mutant hold on 3.14 (01:11:16) had 2 failures, both in the informational property report: under a
  reaping mutant, the property's own reap met ECHILD as a `ChildProcessError`, not an assertion. `85a437a66` makes
  that an assertion; the kills and controls were the same in both holds.
- Dev runs while building (the peek file and mutants on 3.14, 00:44 to 01:04) ran under the lock and are not
  counted above.

### Not run

- e2e and frontend: lanes can't run them.
- `test_wall_stop_deliveries.py` and `test_wall_cancel_races.py`, whose fakes give the relay a pid-less child: not
  on the brief's list. The helper reads those fakes exactly as `poll()` did (`test_a_child_without_a_real_pid_is_never_peeked`).
- A run at load 100 or more, to observe round 3's two log-sites failures: starting load is out of bounds for a lane.
