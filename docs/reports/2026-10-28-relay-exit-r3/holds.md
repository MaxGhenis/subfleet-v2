# Review #271 r3 holds

UTC starts and loads are sampled inside each admitted pytest session. All tests are serial.
One pytest runs per shared-lock hold. A dash means no test call completed (for example collection only).

| Hold | Slot | Pytest start UTC | Load 1 / 5 / 15 min | Session elapsed s | P / F / S | Files |
|---|---|---|---|---|---|---|
| hold-01 | A | 2026-10-28T17:07:14.460739+00:00 | 92.7 / 102.4 / 99.2 | 53.9 | 35 / 0 / 0 | test_turn_relay_exit.py: 10/0/0; test_turn_relay_exit_recovery.py: 13/0/0; test_turn_relay_exit_review.py: 12/0/0 |
| hold-02 | A | 2026-10-28T17:10:16.049667+00:00 | 102.4 / 99.9 / 98.6 | 433.8 | 4 / 0 / 0 | test_turn_relay_exit_properties.py: 4/0/0 |
| hold-03 | A | 2026-10-28T17:18:57.029500+00:00 | 118.7 / 122.0 / 113.3 | 164.8 | 23 / 1 / 0 | named_mutants.py: 10/0/0; test_r3_mutants.py: 7/0/0; test_turn_relay_exit_r3.py: 6/1/0 |
| hold-04 | A | 2026-10-28T17:22:37.606984+00:00 | 111.4 / 116.8 / 112.9 | 240.6 | 159 / 0 / 0 | test_relay.py: 30/0/0; test_turn_attachment_replay.py: 13/0/0; test_turn_replay.py: 33/0/0; test_turn_runner.py: 83/0/0 |
| hold-05 | A | 2026-10-28T17:27:08.288662+00:00 | 82.3 / 85.1 / 98.8 | 125.6 | 71 / 2 / 0 | review_mutants.py: 2/0/0; test_state_contract.py: 37/0/0; test_turn_relay_exit_r3.py: 6/2/0; test_invariants_index.py: 25/0/0; test_test_module_names.py: 1/0/0 |
| hold-06 | A | 2026-10-28T17:29:45.679276+00:00 | 57.4 / 79.3 / 94.5 | 122.0 | 48 / 0 / 0 | test_ci_shard_plugin.py: 48/0/0 |
| hold-07 | A | 2026-10-28T17:33:44.138937+00:00 | 94.5 / 78.5 / 89.4 | 304.7 | 177 / 0 / 0 | test_claude_turn.py: 69/0/0; test_codex_turn.py: 67/0/0; test_codex_turn_completed.py: 41/0/0 |
| hold-08 | A | 2026-10-28T17:40:00.619466+00:00 | 96.8 / 98.4 / 95.3 | 112.7 | 109 / 0 / 0 | test_codex_turn_completed_fake.py: 109/0/0 |
| hold-09 | B | 2026-10-28T17:42:51.332635+00:00 | 78.7 / 85.7 / 90.4 | 66.2 | 50 / 0 / 0 | test_cli_turns.py: 23/0/0; test_enrollment_runner.py: 7/0/0; test_retention_turn_contract.py: 2/0/0; test_retention_turn_workdirs.py: 1/0/0; test_retention_turns.py: 7/0/0; test_scheduler_turns.py: 10/0/0 |
| hold-10 | B | 2026-10-28T17:44:30.712038+00:00 | 84.4 / 84.1 / 89.1 | 189.5 | 60 / 0 / 0 | test_turn_listing.py: 9/0/0; test_turn_retention_cwd.py: 6/0/0; test_turn_retention_job_directory.py: 2/0/0; test_turn_retention_quarantine.py: 10/0/0; test_turn_trees.py: 18/0/0; test_turn_wait_reasons.py: 7/0/0; test_turn_overlap.py: 8/0/0 |
| hold-11 | B | 2026-10-28T17:49:41.909741+00:00 | 117.6 / 102.9 / 96.3 | 130.8 | 1 / 0 / 0 | property_counter.py: 1/0/0 |
| hold-12 | B | 2026-10-28T17:59:52.275470+00:00 | 83.4 / 70.5 / 79.0 | 134.1 | 1 / 0 / 0 | test_turn_overlap.py: 1/0/0 |
| hold-13 | A | 2026-10-28T18:04:23.362428+00:00 | 76.4 / 76.3 / 79.5 | 23.3 | 6 / 3 / 0 | test_turn_relay_exit_r3.py: 6/3/0 |
| hold-14 | B | 2026-10-28T18:06:38.294611+00:00 | 59.6 / 71.0 / 77.0 | 28.2 | 2 / 0 / 0 | review_mutants.py: 2/0/0 |
| hold-15 | B | 2026-10-28T18:09:13.445874+00:00 | 54.8 / 64.5 / 73.4 | 144.2 | 1 / 0 / 0 | test_turn_overlap.py: 1/0/0 |
| hold-16 | A | 2026-10-28T18:13:20.577253+00:00 | 41.0 / 54.1 / 66.8 | 288.5 | 4 / 1 / 0 | review_mutants.py: 2/0/0; test_turn_relay_exit_r3.py: 0/1/0; test_turn_retention_cwd_properties.py: 1/0/0; test_turn_retention_job_properties.py: 1/0/0 |
| hold-17 | B | 2026-10-28T18:19:28.283661+00:00 | 57.1 / 57.9 / 64.5 | 157.1 | 51 / 0 / 0 | review_mutants.py: 9/0/0; test_probe_turn.py: 42/0/0 |
| hold-18 | A | 2026-10-28T18:23:06.850487+00:00 | 44.2 / 53.5 / 61.4 | 313.4 | 2 / 0 / 0 | review_mutants.py: 1/0/0; test_probe_turn.py: 1/0/0 |
| hold-19 | A | 2026-10-28T18:37:08.213236+00:00 | 69.0 / 70.3 / 66.0 | 77.6 | 0 / 0 / 0 | collection only |

## Executed inputs

Common wrapper: the brief's `suite_hold.sh`, `uv run --no-sync`, a scratch venv/cache,
`TMPDIR=<scratch>/<hold>`, `HYPOTHESIS_STORAGE_DIRECTORY=<scratch>/hyp`, and bytecode disabled.
The local audit hook records node outcomes/PIDs and closes any still-running direct subprocess.
For the first holds the interrupt backstop is 550 seconds; later holds use 500 seconds from before pytest configuration.

### hold-01

```text
pytest -q -s -p no:cacheprovider tests/fake/test_turn_relay_exit.py tests/fake/test_turn_relay_exit_recovery.py tests/fake/test_turn_relay_exit_review.py
```

### hold-02

```text
pytest -q -p no:cacheprovider -s --hypothesis-show-statistics tests/fake/test_turn_relay_exit_properties.py
```

### hold-03

```text
pytest -q -p no:cacheprovider -s tests/fake/test_turn_relay_exit_r3.py docs/reports/2026-10-28-relay-exit-r2/named_mutants.py /var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-r271r3/test_r3_mutants.py -k 'not report_whether'
```

### hold-04

```text
pytest -q -p no:cacheprovider tests/unit/test_relay.py tests/unit/test_turn_runner.py tests/unit/test_turn_replay.py tests/unit/test_turn_attachment_replay.py
```

### hold-05

```text
pytest -q -p no:cacheprovider tests/fake/test_turn_relay_exit_r3.py docs/reports/2026-10-28-relay-exit-r3/review_mutants.py::test_transient_stat_failure_candidate_and_mutant tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py
```

### hold-06

```text
pytest -q -p no:cacheprovider tests/unit/test_ci_shard_plugin.py
```

### hold-07

```text
pytest -q -p no:cacheprovider tests/unit/test_claude_turn.py tests/unit/test_codex_turn.py tests/unit/test_codex_turn_completed.py
```

### hold-08

```text
pytest -q -p no:cacheprovider tests/fake/test_codex_turn_completed_fake.py
```

### hold-09

```text
pytest -q -p no:cacheprovider tests/unit/test_cli_turns.py tests/unit/test_enrollment_runner.py tests/unit/test_retention_turn_workdirs.py tests/unit/test_retention_turns.py tests/unit/test_retention_turn_contract.py tests/unit/test_scheduler_turns.py
```

### hold-10

```text
pytest -q -p no:cacheprovider tests/unit/test_turn_overlap.py tests/fake/test_turn_listing.py tests/fake/test_turn_wait_reasons.py tests/fake/test_turn_trees.py tests/fake/test_turn_retention_cwd.py tests/fake/test_turn_retention_quarantine.py tests/fake/test_turn_retention_job_directory.py -k 'not test_i4_a_turn_is_marked_shared_exactly_when_another_writer_s_window_met_it and not test_i4_an_unrecorded_end_closed_later_leaves_exactly_the_marks_its_window_meets'
```

### hold-11

```text
pytest -q -p no:cacheprovider -s -p tests.conftest docs/reports/2026-10-28-relay-exit-r3/property_counter.py
```

### hold-12

```text
pytest -q -p no:cacheprovider tests/unit/test_turn_overlap.py -k test_i4_a_turn_is_marked_shared_exactly_when_another_writer_s_window_met_it
```

### hold-13

```text
pytest -q -p no:cacheprovider tests/fake/test_turn_relay_exit_r3.py
```

### hold-14

```text
pytest -q -p no:cacheprovider docs/reports/2026-10-28-relay-exit-r3/review_mutants.py::test_kernel_exit_proof_candidate_and_mutant -p tests.conftest
```

### hold-15

```text
pytest -q -p no:cacheprovider tests/unit/test_turn_overlap.py -k test_i4_an_unrecorded_end_closed_later_leaves_exactly_the_marks_its_window_meets
```

### hold-16

```text
pytest -q -p no:cacheprovider tests/fake/test_turn_relay_exit_r3.py::test_a_real_exited_fake_is_not_a_relay_failure_while_the_guardian_wait_holds_its_lock docs/reports/2026-10-28-relay-exit-r3/review_mutants.py::test_kernel_exit_proof_candidate_and_mutant tests/fake/test_turn_retention_cwd_properties.py tests/fake/test_turn_retention_job_properties.py
```

### hold-17

```text
pytest -q -p no:cacheprovider -s tests/fake/test_probe_turn.py docs/reports/2026-10-28-relay-exit-r3/review_mutants.py -k 'not test_no_sequence_of_probe_outcomes_lets_a_later_job_take_a_waiters_probe'
```

### hold-18

```text
pytest -q -p no:cacheprovider -s tests/fake/test_probe_turn.py docs/reports/2026-10-28-relay-exit-r3/review_mutants.py -k 'test_no_sequence_of_probe_outcomes_lets_a_later_job_take_a_waiters_probe or drain_is_bounded'
```

### hold-19

```text
pytest -q -p no:cacheprovider --collect-only tests/fake/test_codex_turn_completed_fake.py tests/fake/test_probe_turn.py tests/fake/test_state_contract.py tests/fake/test_turn_listing.py tests/fake/test_turn_relay_exit.py tests/fake/test_turn_relay_exit_properties.py tests/fake/test_turn_relay_exit_r3.py tests/fake/test_turn_relay_exit_recovery.py tests/fake/test_turn_relay_exit_review.py tests/fake/test_turn_retention_cwd.py tests/fake/test_turn_retention_cwd_properties.py tests/fake/test_turn_retention_job_directory.py tests/fake/test_turn_retention_job_properties.py tests/fake/test_turn_retention_quarantine.py tests/fake/test_turn_trees.py tests/fake/test_turn_wait_reasons.py tests/unit/test_ci_shard_plugin.py tests/unit/test_claude_turn.py tests/unit/test_cli_turns.py tests/unit/test_codex_turn.py tests/unit/test_codex_turn_completed.py tests/unit/test_enrollment_runner.py tests/unit/test_invariants_index.py tests/unit/test_relay.py tests/unit/test_retention_turn_contract.py tests/unit/test_retention_turn_workdirs.py tests/unit/test_retention_turns.py tests/unit/test_scheduler_turns.py tests/unit/test_test_module_names.py tests/unit/test_turn_attachment_replay.py tests/unit/test_turn_overlap.py tests/unit/test_turn_replay.py tests/unit/test_turn_runner.py
```

## Final results per required file

| File | Collected | Passed | Failed | Skipped | Unrun |
|---|---|---|---|---|---|
| tests/fake/test_codex_turn_completed_fake.py | 109 | 109 | 0 | 0 | 0 |
| tests/fake/test_probe_turn.py | 43 | 43 | 0 | 0 | 0 |
| tests/fake/test_state_contract.py | 37 | 37 | 0 | 0 | 0 |
| tests/fake/test_turn_listing.py | 9 | 9 | 0 | 0 | 0 |
| tests/fake/test_turn_relay_exit.py | 10 | 10 | 0 | 0 | 0 |
| tests/fake/test_turn_relay_exit_properties.py | 4 | 4 | 0 | 0 | 0 |
| tests/fake/test_turn_relay_exit_r3.py | 9 | 6 | 3 | 0 | 0 |
| tests/fake/test_turn_relay_exit_recovery.py | 13 | 13 | 0 | 0 | 0 |
| tests/fake/test_turn_relay_exit_review.py | 12 | 12 | 0 | 0 | 0 |
| tests/fake/test_turn_retention_cwd.py | 6 | 6 | 0 | 0 | 0 |
| tests/fake/test_turn_retention_cwd_properties.py | 1 | 1 | 0 | 0 | 0 |
| tests/fake/test_turn_retention_job_directory.py | 2 | 2 | 0 | 0 | 0 |
| tests/fake/test_turn_retention_job_properties.py | 1 | 1 | 0 | 0 | 0 |
| tests/fake/test_turn_retention_quarantine.py | 10 | 10 | 0 | 0 | 0 |
| tests/fake/test_turn_trees.py | 18 | 18 | 0 | 0 | 0 |
| tests/fake/test_turn_wait_reasons.py | 7 | 7 | 0 | 0 | 0 |
| tests/unit/test_ci_shard_plugin.py | 48 | 48 | 0 | 0 | 0 |
| tests/unit/test_claude_turn.py | 69 | 69 | 0 | 0 | 0 |
| tests/unit/test_cli_turns.py | 23 | 23 | 0 | 0 | 0 |
| tests/unit/test_codex_turn.py | 67 | 67 | 0 | 0 | 0 |
| tests/unit/test_codex_turn_completed.py | 41 | 41 | 0 | 0 | 0 |
| tests/unit/test_enrollment_runner.py | 7 | 7 | 0 | 0 | 0 |
| tests/unit/test_invariants_index.py | 25 | 25 | 0 | 0 | 0 |
| tests/unit/test_relay.py | 30 | 30 | 0 | 0 | 0 |
| tests/unit/test_retention_turn_contract.py | 2 | 2 | 0 | 0 | 0 |
| tests/unit/test_retention_turn_workdirs.py | 1 | 1 | 0 | 0 | 0 |
| tests/unit/test_retention_turns.py | 7 | 7 | 0 | 0 | 0 |
| tests/unit/test_scheduler_turns.py | 10 | 10 | 0 | 0 | 0 |
| tests/unit/test_test_module_names.py | 1 | 1 | 0 | 0 | 0 |
| tests/unit/test_turn_attachment_replay.py | 13 | 13 | 0 | 0 | 0 |
| tests/unit/test_turn_overlap.py | 10 | 10 | 0 | 0 | 0 |
| tests/unit/test_turn_replay.py | 33 | 33 | 0 | 0 | 0 |
| tests/unit/test_turn_runner.py | 83 | 83 | 0 | 0 | 0 |

Total required collected nodes: 761; final outcomes: {'passed': 758, 'failed': 3}.

Skipped or unrun nodes:

None.

## Owned run processes

Each row lists the pytest PID and direct-child count. Every direct child PID is listed individually
in `runs.jsonl`; the session-finish hook checks all recorded children, and per-child return codes
are recorded starting with hold-05. Completed foreground launch wrappers are also
retained in the separate PID ledger. `ps` is denied in this sandbox;
this is an ownership ledger, not a census of the host.

- hold-01: pytest 52054; direct children 0; stopped by PID none (already finished).
- hold-02: pytest 83272; direct children 0; stopped by PID none (already finished).
- hold-03: pytest 92026; direct children 0; stopped by PID none (already finished).
- hold-04: pytest 39460; direct children 6; stopped by PID none (already finished).
- hold-05: pytest 7502; direct children 82; stopped by PID none (already finished).
- hold-06: pytest 42214; direct children 0; stopped by PID none (already finished).
- hold-07: pytest 93107; direct children 0; stopped by PID none (already finished).
- hold-08: pytest 87120; direct children 220; stopped by PID none (already finished).
- hold-09: pytest 23884; direct children 66; stopped by PID none (already finished).
- hold-10: pytest 42485; direct children 1439; stopped by PID none (already finished).
- hold-11: pytest 11780; direct children 0; stopped by PID none (already finished).
- hold-12: pytest 70057; direct children 0; stopped by PID none (already finished).
- hold-13: pytest 84939; direct children 1; stopped by PID none (already finished).
- hold-14: pytest 23919; direct children 2; stopped by PID none (already finished).
- hold-15: pytest 77200; direct children 0; stopped by PID none (already finished).
- hold-16: pytest 71456; direct children 533; stopped by PID none (already finished).
- hold-17: pytest 1054; direct children 151; stopped by PID none (already finished).
- hold-18: pytest 93128; direct children 885; stopped by PID none (already finished).
- hold-19: pytest 58626; direct children 0; stopped by PID none (already finished).

E2e was not run, as required. Optional builder property-mutant reporting was not run; the five named killers and controls were run.

The first two holds used the stock session startup. Later holds redirect `tests.failed_keep.ROOT`
into scratch so the test startup cannot prune shared kept-failure directories. No pruning was reported
by the first holds. Tree fixtures that explicitly require short AF_UNIX paths use their own temporary
directories and remove them in their fixture cleanup.

Final verification: 19 of 19 pytest processes finished. All recorded direct children had already
finished; no child needed termination. Maximum recorded pytest RSS was 142,655,488 bytes.
The largest sampled scratch size was 107 MB; the last sample was 23 MB. The first hold started
at 17:06:44Z (pytest hook 17:07:14Z), within five minutes of this review's first clock read,
17:02:18Z. No hold ran outside the lock; the 30-minute fallback was never needed.

The review file grew from 7 nodes in hold-03 to 8 in hold-05 and 9 in hold-13. The final table
uses the current nine-node collection, so an earlier unparameterized metadata failure is not
counted as another finding. Pytest omitted the external scratch helper's file prefix in
hold-03; its seven reports are labeled test_r3_mutants.py in the table and its exact path is
in that hold's inputs.

Files left for salvage (the shared Git metadata refused the named-file stage/commit):

- tests/fake/test_turn_relay_exit_r3.py
- docs/reports/2026-10-28-relay-exit-r3/report.md
- docs/reports/2026-10-28-relay-exit-r3/holds.md
- docs/reports/2026-10-28-relay-exit-r3/review_mutants.py
- docs/reports/2026-10-28-relay-exit-r3/property_counter.py
- docs/reports/2026-10-28-relay-exit-r3/runs.jsonl
- docs/reports/2026-10-28-relay-exit-r3/launch-pids.jsonl

Guarded scratch removal completed at 2026-10-28T18:50:43.090809+00:00. The scratch root is absent. No bytecode, .hypothesis, .pytest_cache or build directory remains in the worktree.
