# Credit lanes: the integration build's report

Draft PR #292, `feat/credit-lane-217`, based on `release/217`. This report covers the
integration build's continuation r3, which started at 02:10Z on 2026-11-05 from
`922d2ecbc`. The build's task: `docs/decisions/2026-11-03-api-credit-lanes.md`, item 1 of
`docs/reports/credit-lane-harness-report.md` and section 31 of
`docs/acceptance-contract.md`. The hub ruled on 2026-11-04 that no exec environment
block, argv or file of any process ever holds a credit key; the key travels only over
pipes, and only the harness keeps it, in memory (C-31.17).

The earlier attempts' commit messages cite this file, but none of them committed it, and
nothing uncommitted survived the host crash at 01:23Z on 11/05. r3 wrote it from the
commits and from its own runs. Where a result comes from an earlier commit's message
rather than an r3 run, the text says so.

(Sections still to come in this draft: D-CREDIT-MEM1, the suite, the mutants, the key's
path, the process list, invariants, the differential, deviations and open questions.)

## Step 1: the four checks, run again

r3 ran each item's named tests once at `922d2ecbc`, in one pytest run
(`-n 4 --timeout=300`, 02:20Z to 02:23Z): 41 passed, none failed, in 158 s.

| Item | Tests | Result |
|---|---|---|
| Invariant 6 at the process level | `tests/fake/test_credit_key_containment.py`: `test_no_process_of_a_credit_attempt_holds_the_key`, `test_the_publish_hook_self_test_holds_no_key` | 2 passed |
| The frame (Hypothesis and examples) | `tests/process/test_credit_key_frame.py`: the round trip through `encode`, a pipe and `read_frame` and `Daemon._open_gate` (50 examples); the real chain, daemon writer to a real guardian to the harness's `read_key` (12 examples and 3 explicit); cut-short frames, in process (50) and through a real guardian (12); 8 bad gates or frames; 3 bad lengths; 6 unframeable keys; `--key-frame` without a gate; the guardian holds no pipe while its provider runs | 23 passed |
| Adapter and harness agree (differential) | `tests/process/test_credit_lane_differential.py::test_the_adapter_reads_the_real_harness_as_it_reads_the_fixture`, 12 scenarios: done, credit, refused-by-kind, workspace-limit, turns, budget, error-401, error-429, error (500), other-model, other-org, no-end (SIGTERM) | 12 passed |
| Resume, end to end | `test_a_stopped_attempt_resumes_with_its_history_only_growing` (same file), with `tests/process/test_api_agent_process.py`'s `test_resume_continues_from_messages_jsonl`, `test_resume_answers_a_call_that_never_got_its_result` and `test_a_fresh_launch_refuses_an_existing_conversation` | 4 passed |

Each item already had its test, so r3 added none.

### The invariant-6 tests on `da2eb8b56`

r3 exported `da2eb8b56` with `git archive` into its temporary folder, outside any git
checkout, gave the copy its own environment (`uv sync --group dev --extra api`; its
editable install names the copy, so `python -I -m subfleet.api_agent` imports the copy's
harness), and added `tests/fake/test_credit_key_containment.py` with its two helpers,
`tests/fake/proc_blocks.py` and `tests/fake/run_credit_daemon.py`. Run there
(02:25Z to 02:31Z, serial, `--timeout=300`), both tests fail, each at its first check of
the key:

- `test_no_process_of_a_credit_attempt_holds_the_key` fails at line 200: `runs of the key
  in exec blocks (argv or environment): {'guardian 80789': 88}`. The test's key has 99
  characters, so it has 88 12-character runs, and `proc_blocks.leaks` found every one of
  them in the guardian's exec block.
- `test_the_publish_hook_self_test_holds_no_key` fails at line 233: `runs of the key in
  the self-test's exec blocks: {'self-test-1-39766.bin': 88}`: again every run of the
  key, in the publish hook self-test's exec block.

Result: 2 failed in 351 s. At `922d2ecbc` the same two pass.
