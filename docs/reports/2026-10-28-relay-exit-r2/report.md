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
