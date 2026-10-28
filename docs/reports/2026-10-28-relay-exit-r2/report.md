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
(reads what is on disk) before returning its scripted value. No assertion changed.

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
replay 1); refusals 0/1/2+ = 67/20/67; EOF before the exit 12; a receipt never 59 (26 forced).

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
