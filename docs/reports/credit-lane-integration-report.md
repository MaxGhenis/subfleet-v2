# Credit lanes: the integration build's report

Draft PR #292, `feat/credit-lane-217`, based on `release/217`. The build's task:
`docs/decisions/2026-11-03-api-credit-lanes.md`, item 1 of
`docs/reports/credit-lane-harness-report.md` and section 31 of
`docs/acceptance-contract.md`. The hub ruled on 2026-11-04 that no exec environment
block, argv or file of any process ever holds a credit key; the key travels only over
pipes, and only the harness keeps it, in memory (C-31.17).

This report covers continuation r3, which started from `922d2ecbc` and ran in three
attempts:

- **a1**, from 02:10Z on 2026-11-05: step 1 again, the invariant-6 tests on
  `da2eb8b56`, and two D-CREDIT-MEM1 fixes (`a91f30529`, `42aa3030c`, `a20ca2d9e`). It
  ended at about 03:35Z. Its salvage snapshot, `98046dfa3`, holds the same tree as
  `a20ca2d9e`, so it lost nothing.
- **a2**, from 18:37Z on 2026-11-06: D-CREDIT-MEM1's audit and forced faults, a bound
  on the differential test's reads (`b77f91e6e`), step 1 again at the head, the
  targeted runs, part of shard 12, and most of this report. It ended at about 20:49Z
  without committing the report; its salvage snapshot, `b7128c547`, holds the same
  report and data as the worktree it left.
- **a3**, from 04:53Z on 2026-11-07: committed a2's draft as it stood (`9fc1834db`),
  ran the mutants again (`97eade759`), ran the suite's shards in windows that resume
  where the last one stopped, and finished this report.

The earlier attempts' commit messages cite this file, but none of them committed it,
and nothing uncommitted survived the host crash at 01:23Z on 11/05. r3 wrote it from
the commits and from its own runs. Where a result comes from an earlier commit's
message rather than an r3 run, the text says so.

## Summary

- **Invariant 6 holds at the process level.** No exec block or argv of the guardian,
  the harness, its shell, a background process or the publish hook's self-test holds a
  12-character run of the key. Both tests pass at the head, and both fail on
  `da2eb8b56`, where the guardian's block and the self-test's each held every run of
  the key.
- **Step 1's 41 named tests pass at the head** (`b77f91e6e`): containment, the frame,
  the differential and resume, in a2's runs and again in a3's suite shards.
- **The mutants.** a3 ran `tools/credit_key_mutations.py` again on a copy of the head:
  all nine named tests pass on the unchanged copy, and each of the nine mutants fails
  the tests `4c45fb751`'s message names, each for the leak it plants (a run of the key
  in a block, a pipe held, the environment's key sent to the API, a descriptor left
  open).
- **D-CREDIT-MEM1.** r3 found no proven cause: the code that ran at 13:10Z on 11/04 is
  gone. The strongest candidate is `c8bd8aae4`'s `test_launch_gates_above_stdio`, which
  held its daemon child's stdout and stderr in memory for up to 180 s and then copied
  them once more; at a read rate inside the range r3's forced faults measured, it reaches
  the growth's size, with data that compresses as the incident's did. But r3 found
  nothing in the committed code that would have written to those pipes without end. Every reader that could hold a
  child's output without bound, in the incident's files and in the files this build
  changed, now reads with a bound, except `Harness.request`'s `readline` on the daemon's
  socket (site #7), which predates this build. The per-test memory bound (C-20.7) stays
  the fix for the class; no test reached it in any r3 run. The hub decides whether that
  suffices.
- **The suite: six of twelve shards.** a3 ran shards 1, 2, 3, 4, 7 and 12 in full under
  the suite lock, 7,934 of the suite's 27,012 tests, in windows that resume where the
  last one stopped: 7,855 passed, 57 xfailed, 18 skipped and 4 failed. The four were
  10 s e2e waits that ran out at a host load of 106 to 111, in files this build didn't
  change, and all four pass again at a load of 26. Other sessions held the suite lock
  through the rest of a3's window, so shards 5, 6, 8, 9, 10 and 11 didn't run. CI on
  the PR decides the suite.

## Files changed since `da2eb8b56`

Code (in `c8bd8aae4` and `3d9823d2f`, except where named):

- `subfleet/key_frame.py` (new): one frame, a 4-byte big-endian length, then 1 to
  `MAX_KEY_BYTES` (4,096) bytes of printable ASCII; `encode`, `frame`, `decode`,
  `read_frame` (which never reads past the frame), `write_all` and `wipe`.
- `subfleet/daemon.py`: `_credit_key` picks the key for a credit lane's launch only,
  never a turn's; `_open_gate` writes it as one frame after the release byte; a credit
  launch's guardian gets `--key-frame`. A credit launch takes its hooks from
  `_api_guard`, never the Codex guard preflight, and the daemon assembles no environment
  from its credential.
- `subfleet/guardian.py`: `read_gate`, `key_pipe`, `key_handoff`, and `_run_released`,
  run_guardian's released half, split out so one place wipes the key.
- `subfleet/adapters/claude_api.py`: `env_add` carries `SUBFLEET_API_LANE` only; a key
  that won't fit a frame refuses before any process starts (`3d9823d2f`'s message).
- `subfleet/api_agent/harness.py`, `__main__.py` and `cli.py`: the harness takes its
  key from `--key-fd` only (`read_key`).
- `subfleet/api_agent/exec_env.py` and `subfleet/launch_env.py` (`309cb4e3a`):
  comments only, to match the descriptor protocol.

Tests:

- New: `tests/fake/test_credit_key_containment.py`, `tests/fake/proc_blocks.py` and
  `tests/fake/run_credit_daemon.py` (`8c16e8b1c`); `tests/process/test_credit_key_frame.py`
  (`7007aa93d`); `tests/process/test_credit_lane_differential.py` (`9f573f894`,
  `b77f91e6e`); `tests/memory_bound.py` and `tests/unit/test_memory_bound.py`
  (`82bfd6a9e`, `42aa3030c`).
- Changed: `tests/conftest.py` (installs the bound); `tests/fake/conftest.py`
  (`bounded_text`, `OUTPUT_LIMIT`, `watch_output`); `tests/fake/test_launch_gates_above_stdio.py`;
  `tests/fake/test_credit_lane_enroll.py`; `tests/process/test_api_agent_process.py`;
  `tests/unit/test_api_agent_units.py`, `test_claude_api_adapter.py`,
  `test_credit_lane_admission.py`, `test_launch_env_mutations.py` and
  `test_launch_env_round8.py`; three fixtures (`budget`, `turns`, `workspace-limit`).

Tools: `tools/credit_key_mutations.py` (`192385c19`, `4c45fb751`), and
`tools/footprint_watch.py` and `tools/footprint_stacks.py` (`82bfd6a9e`).

Docs: `docs/acceptance-contract.md` (C-31.2, C-31.7 and C-31.12 amended; C-31.17 and
C-20.7; change-log entries at the top of "Changes in version 3");
`docs/decisions/2026-11-03-api-credit-lanes.md` (the interface, how the key arrives,
invariants 1 and 6); `docs/reports/credit-lane-harness-report.md` (item 1 marked
resolved, pointing here); this report, and its data in
`docs/reports/credit-lane-integration-data/`: a2's forced faults (`mem1_faults.py`,
`mem1-run1.jsonl`, `mem1-gate.jsonl`), a3's mutant results (`mutants-a3.json`), and a3's
suite records and runner (`suite-a3.json`, `suite_resume.py`, `run_shard.py`).

## The key's path, end to end

r3 read each step below in the code at the head.

| Step | Where | What it holds, and for how long |
|---|---|---|
| 1 | The daemon | `_launch` resolves the lane's credential into `credential_env`, a local of that call (`_credential_environment`). The key goes into no environment, argv or file: `_launch_environment` removes `ANTHROPIC_API_KEY` from every launch, and a credit launch assembles no environment from its credential. `_credit_key` passes the key on for a credit lane only. After the attempt's starting commit, `_open_gate` builds the release byte and the frame in a `bytearray`, writes them with one `write_all` to the gate's write end, and overwrites both copies. Nothing can overwrite the key's Python strings (`credential_env`, `key`); they go out of scope when `_launch` returns. |
| 2 | The launch gate, a pipe | The release byte and the frame, at most 4,101 bytes, until the guardian reads them. |
| 3 | The guardian | It starts with its standard streams on /dev/null, only the gate's read end inherited, and `--key-frame`, a flag with no value. `read_gate` reads the byte and the frame into a `bytearray` and closes the gate. `key_handoff` puts the frame in a new pipe (`key_pipe`, whose write end closes at once), names the read end in the provider's argv (`--key-fd <n>`), and closes it and overwrites the key as soon as `Popen` returns or fails. While the provider runs, the guardian holds neither the key nor a pipe. A refused gate (any byte but `1`; a missing, cut-short, empty or oversized frame) ends it with 127 before it creates the attempt folder or starts anything. |
| 4 | The key pipe | The frame, until the harness reads it. |
| 5 | The harness | `python -I -m subfleet.api_agent … --key-fd <n>`. Before anything else it overwrites, in the exec block the kernel copied, the value of every `ANTHROPIC_*` and credential-named variable (C-31.12); a launch carries none, so this guards only against a stray one. `read_key` checks that the descriptor is a pipe, reads one frame, decodes it, overwrites the `bytearray` and closes the descriptor. The key then lives in the harness's memory, as a string, for the run. |
| 6 | The harness's shell and tools | Nothing. The containment test reads the shell's environment from inside the shell, and a background process's exec block. |
| 7 | Files | Nothing. After the attempt, no file under the state root holds a run of the key: `stream.jsonl`, `last.md`, stdout, stderr, `messages.jsonl`, the attempt's notes, the store. |

## Step 3's process list

Every process the daemon could start for a credit lane, with its result and its test.
"a2" means a2's run at the head; "a3" means a3's runs at the head (the mutant tool's
baseline on a copy, and the suite's shards); "a1" means a1's run on `da2eb8b56`.

| Process | Result | Tests | Runs |
|---|---|---|---|
| The guardian | No run of the key in its exec block or argv; no pipe while its provider runs. | `test_no_process_of_a_credit_attempt_holds_the_key`; `test_the_guardian_holds_no_pipe_while_its_provider_runs` | a2 and a3 (baseline, shard 1): pass. a1: fails, all 88 runs of the key in the guardian's block. |
| The harness | No run in its block or argv; the key comes from `--key-fd` only. | The containment test; `test_the_key_comes_only_from_the_key_fd`; `test_the_harness_reads_no_key_from_its_environment` | a2: the containment test and the source check pass. a3: all three pass, in the mutant tool's baseline and again in the suite (the source check in shard 4, the process test in shard 7). With the harness reading the variable again (mutants c and c-hidden), the process test fails: the fake API records a request sent with the environment's key. |
| The harness's shell, and a background process | No run. | The containment test | a2 and a3: pass |
| The publish hook's self-test | No run in its block, read before it gets its payload. | `test_the_publish_hook_self_test_holds_no_key` | a2 and a3 (baseline, shard 1): pass. a1: fails, all 88 runs in the self-test's block. |
| The Codex guard preflight | Never starts: `_launch` takes a credit launch's hooks from `_api_guard` (r3 read this branch), and per `954f4b0c3`, `_guard_override` returns before the preflight for any lane that isn't Codex's. | `test_no_guard_preflight_starts_for_a_credit_lane` | a2 and a3 (shard 4): pass |
| A probe | Refused before resolution (`dcb481c2a`'s message). | `test_a_credit_lane_reaching_the_probe_resolves_no_key_and_starts_nothing`; `test_a_credit_lane_is_never_probed_before_launch` | a2 and a3 (shard 4): pass |
| A relaunch after a daemon restart | Resolves no credit key (`dcb481c2a`'s message). | `test_a_credit_lane_is_never_resolved_to_rebuild_a_launch` | a2 and a3 (shard 4): pass |
| Enrollment's check and a re-enrollment | Read the registry only, and start no process (`954f4b0c3`'s message). | `test_enrolling_and_re_enrolling_a_credit_lane_reads_no_key_and_starts_nothing` | a2 and a3 (shard 4): pass |

## Invariants this build touches

From the design's list (numbers as there), and the clauses:

- **1. Credit keys only.** `test_only_a_credit_launch_carries_anthropic_api_key_and_only_its_own`
  (Hypothesis, 150 examples over the provider, a daemon environment with or without
  `ANTHROPIC_API_KEY`, a policy that passes it or not, and a leaked `env_add`): a credit
  launch's gate carries exactly its key, as a frame; no other launch sends a frame; and
  no launch's environment carries the variable. Also
  `test_a_credit_launch_never_runs_the_claude_binary`,
  `test_build_launch_is_exactly_the_interface` and
  `test_the_daemon_resolves_a_credit_key_for_the_lanes_own_organization_only`.
- **6. The key stays in the harness.** The two containment tests (process level; the raw
  key and every 12-character run); the frame tests; `test_the_key_comes_only_from_the_key_fd`;
  `test_overwrite_hides_secret_values_from_the_exec_block_and_keeps_it_whole` (C-31.12);
  the containment test's sweep of every file under the state root.
- **3 and 7, through enrollment.** `954f4b0c3` added the re-enrollment test above; the
  rest of `tests/fake/test_credit_lane_enroll.py` stands.
- **C-31.17, the descriptor protocol.** The frame's round trip (Hypothesis, any key of 1
  to 4,096 printable ASCII characters): through `encode`, a pipe, `read_frame` and
  `Daemon._open_gate`, and from the daemon's writer through a real guardian to the
  harness's `read_key`, unchanged, with the descriptor closed afterwards. A frame cut
  anywhere short never reads as a key. Empty, oversized and missing frames, and
  `--key-frame` without a gate, end the guardian with 127 before it writes a file or
  starts its provider.
- **C-4.2, the launch gate.** `tests/unit/test_guardian.py`'s gate tests, which run the
  guardian in the test's own process.
- **C-20.7, the memory bound.** `tests/unit/test_memory_bound.py`, and the two output
  limits: `test_a_daemon_that_writes_without_end_stops_at_the_output_limit` and
  `test_a_harness_that_writes_past_the_output_limit_is_stopped_at_once`.

For the PR, the properties that hold for every input:

- For every key `k` of 1 to 4,096 printable ASCII characters,
  `decode(read_frame(encode(k)))` equals `k`, and the reader consumes exactly the frame.
- For every frame and every cut shorter than it, `read_frame` refuses; no partial key
  ever reads as a key.
- For every roster, daemon environment and policy, a launch sends a frame exactly when
  its lane is a credit lane, and no launch environment holds `ANTHROPIC_API_KEY`.
- For every process a credit attempt starts, no 12-character run of the key appears in
  its exec block or argv. The test checks this by example, at the process level.
- Every test's process stays under the memory bound, or that test fails with its stacks.

## Step 1: the four checks, run again

r3 a1 ran each item's named tests once at `922d2ecbc`, in one pytest run
(`-n 4 --timeout=300`, 02:20Z to 02:23Z on 11/05): 41 passed, none failed, in 158 s.

| Item | Tests | Result |
|---|---|---|
| Invariant 6 at the process level | `tests/fake/test_credit_key_containment.py`: `test_no_process_of_a_credit_attempt_holds_the_key`, `test_the_publish_hook_self_test_holds_no_key` | 2 passed |
| The frame (Hypothesis and examples) | `tests/process/test_credit_key_frame.py`: the round trip through `encode`, a pipe and `read_frame` and `Daemon._open_gate` (50 examples); the real chain, daemon writer to a real guardian to the harness's `read_key` (12 examples and 3 explicit); cut-short frames, in process (50) and through a real guardian (12); 8 bad gates or frames; 3 bad lengths; 6 unframeable keys; `--key-frame` without a gate; the guardian holds no pipe while its provider runs | 23 passed |
| Adapter and harness agree (differential) | `tests/process/test_credit_lane_differential.py::test_the_adapter_reads_the_real_harness_as_it_reads_the_fixture`, 12 scenarios: done, credit, refused-by-kind, workspace-limit, turns, budget, error-401, error-429, error (500), other-model, other-org, no-end (SIGTERM) | 12 passed |
| Resume, end to end | `test_a_stopped_attempt_resumes_with_its_history_only_growing` (same file), with `tests/process/test_api_agent_process.py`'s `test_resume_continues_from_messages_jsonl`, `test_resume_answers_a_call_that_never_got_its_result` and `test_a_fresh_launch_refuses_an_existing_conversation` | 4 passed |

Each item already had its test, so r3 added none.

### Again at the head

a1 and a2 then changed the harness process tests, the differential test and the bound, so
a2 ran the same 41 tests again at `b77f91e6e`, in two runs, both `-n 4`:

- `tests/process/test_credit_lane_differential.py`, the whole file (19:01Z to 19:03Z on
  11/06): 14 passed in 69 s, the 12 scenarios, the resume test and a2's new
  output-limit test.
- The containment file, the frame file and the three resume tests in
  `test_api_agent_process.py` (19:33Z to 19:39Z): 28 passed in 298 s. The slowest:
  `test_resume_continues_from_messages_jsonl` (244 s) and
  `test_the_publish_hook_self_test_holds_no_key` (173 s), at a host load near 50.

### Again in the suite (a3)

All 41 ran a third time inside a3's suite shards at `9fc1834db`, whose code is
`b77f91e6e`'s, and passed: the two containment tests and the frame file's 23 in shard 1
(06:45Z, load 7 to 16; the containment tests took 3.0 s and 2.5 s, against 86 s and
173 s in a2's run at a load near 50); the differential file's 14 in shard 4; and
`test_api_agent_process.py`'s 30, the three resume tests among them, in shard 7.

### The invariant-6 tests on `da2eb8b56`

r3 a1 exported `da2eb8b56` with `git archive` into its temporary folder, outside any git
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

## The differential, per scenario

For each scenario, the real harness runs against the fake API with the launch the real
adapter builds and the key on `--key-fd`. The adapter's `classify`, `attest` and
`deliverable` must decide from what the harness wrote what they decide from the fixture,
and the event kinds and field types must match. a2's run at the head: all 12 pass; they
passed again in a3's shard 4.

| Scenario | What the fake API answers | Exit code | Result |
|---|---|---|---|
| done | a `true` tool call, then text | 0 | pass |
| credit | the credit-exhausted 400 | 3 | pass |
| refused-by-kind | a reply from a model of another kind | 4 | pass |
| workspace-limit | the workspace-limit 400 | 5 | pass |
| turns | a tool call, with a 1-turn cap | 6 | pass |
| budget | a tool call costing past a $1 cap | 6 | pass |
| error-401 | `authentication_error` | 1 | pass |
| error-429 | `rate_limit_error` | 1 | pass |
| error | a 500 `api_error` | 1 | pass |
| other-model | text from another model | 0 | pass |
| other-org | text, from another organization | 0 | pass |
| no-end | a tool that runs until the guardian's SIGTERM | 1 | pass |

The fixes (`9f573f894`'s message): three fixtures departed from the contract, and the
fixtures changed; the harness did not.

- workspace-limit: `check_status` 400 became null (C-31.11: a workspace limit writes
  `check_status` null and sends no check).
- turns and budget: a tool event after the tool_use response (C-31.10: a tool_use
  reply's calls run; the harness checks the caps before the next request).

## The mutants

Source: a3's run of `tools/credit_key_mutations.py`, 05:21Z to 05:45Z on 11/07, at a
host load of 35 to 74. a3 exported `9fc1834db` (whose code is `b77f91e6e`'s) with
`git archive` into its temporary folder, gave the copy its own environment (`uv sync
--group dev --extra api`, so `python -I -m subfleet.api_agent` imports the copy), and
ran the tool from the worktree as its docstring says: `--baseline-only` first, then
`--no-baseline --only …` in four calls, each test under `--timeout=300` and each pytest
run under 540 s. The tool runs every named test on the unchanged copy first; a test
catches a mutant only if it passed there and fails with the mutant. The results, with
the tests' fake key scrubbed from the failure messages, are in
`docs/reports/credit-lane-integration-data/mutants-a3.json`.

The baseline: all nine named tests passed, in 473 s. Every mutant's edit applied, and
every mutant failed each test named for it except one (c-hidden, below):

| Mutant | What it changes | Named tests that fail | What each failure says | Run |
|---|---|---|---|---|
| a | the daemon's launch path adds the resolved key to the guardian's environment | containment | 88 runs of the key in the guardian's exec block (all of them) | 96 s |
| a-renamed | (a) under a name no credential rule knows, `SUBFLEET_CREDIT_HANDOFF` | containment | 88 runs in the guardian's block, and 88 in the harness's too | 118 s |
| a-base | the design's first path: the adapter puts the key in `env_add` and `_launch_environment` puts it back, as at `da2eb8b56` | invariant 1, interface, containment | `ANTHROPIC_API_KEY` in the launch's environment; `env_add` holds one more item than the interface; 88 runs in the guardian's block | 75 s |
| b | `_publish_self_test` adds the lane's credential to the hook's environment | self-test | 88 runs in the self-test's exec block | 72 s |
| c | the harness reads `ANTHROPIC_API_KEY` from its environment when no key came on `--key-fd` | source, process | the source check finds the read at `harness.py:163`; the fake API records a request, which the test expects none of | 159 s |
| c-hidden | (c) with the variable's name built at run time | process | the request, as in (c); the source check passes, as the tool expects, since it can't see a name built at run time | 174 s |
| d | the guardian keeps the key pipe's read end open past the spawn | no-pipe, containment | the guardian holds a pipe (descriptor 8) while its provider runs | 86 s |
| d-write | the guardian keeps the key pipe's write end open past the spawn | no-pipe, containment | the guardian holds a pipe (descriptor 9) | 40 s |
| e | the harness keeps its key descriptor open after its one read | read_key, chain | the descriptor is still open after `read_key` (Hypothesis's shrunk example: the key `0`); the real chain reports `closed: False` | 43 s |

These are the same tests that `4c45fb751`'s message names for each mutant, from its run
on a copy of `192385c19`. No mutant went uncaught, so a3 found no gap to close.

The named tests: containment is
`tests/fake/test_credit_key_containment.py::test_no_process_of_a_credit_attempt_holds_the_key`;
self-test, `…::test_the_publish_hook_self_test_holds_no_key`; invariant 1,
`tests/unit/test_credit_lane_admission.py::test_only_a_credit_launch_carries_anthropic_api_key_and_only_its_own`;
interface, `tests/unit/test_claude_api_adapter.py::test_build_launch_is_exactly_the_interface`;
source, `tests/unit/test_api_agent_units.py::test_the_harness_reads_no_key_from_its_environment`;
process, `tests/process/test_api_agent_process.py::test_the_key_comes_only_from_the_key_fd`;
no-pipe, `tests/process/test_credit_key_frame.py::test_the_guardian_holds_no_pipe_while_its_provider_runs`;
chain, `…test_credit_key_frame.py::test_any_key_reaches_the_harness_reader_unchanged_through_a_real_guardian`;
read_key, `tests/unit/test_api_agent_units.py::test_read_key_returns_the_framed_key_and_closes_the_descriptor`.
`4c45fb751` reports no gap.

## D-CREDIT-MEM1: the 127 GB worker

### What happened, and what survives

At 13:10:14Z on 11/04, attempt 1 of the original build ran r2's step-3 command
(`-n 4 --timeout=300` over `test_guardian.py`, `test_claude_api_adapter.py`,
`test_api_agent_units.py`, `test_credit_lane.py`, `test_credit_lane_admission.py`,
`test_credit_lane_enroll.py`, `test_launch_env_round8.py` and
`test_launch_gates_above_stdio.py`) with step 1 uncommitted. One xdist worker grew to
about 127 GB, 126 GB of it compressed, in about six minutes. The host's operator stopped
it at 13:16:26Z, and it exited at 13:30:20Z.

No copy of the code that ran then survives. Attempt 1 committed step 1 at 13:36Z as
`c8bd8aae4`, the closest surviving state. No loose object in the repository's shared object store has a
modification time between 12:55Z and 13:40Z on 11/04, and no salvage ref covers that
time. `82bfd6a9e`'s message records two runs of the command on `c8bd8aae4` under a 3 GB
cap: worker peaks of 319 MB and 151 MB, with 257 passed and one interface failure that
`3d9823d2f` later fixed. So whatever grew either lived only in the uncommitted state, or
needs a fault that `c8bd8aae4` doesn't produce.

### Every place that could hold without bound what a child, a pipe or a generator gives it

a2 read every test, fixture and helper that `da2eb8b56..922d2ecbc` adds or changes, and
the 13:10Z command's files as `c8bd8aae4` had them. Each site, with its verdict:

| # | Site | In the 13:10Z command | Verdict | Fits the growth's shape |
|---|---|---|---|---|
| 1 | `tests/fake/test_launch_gates_above_stdio.py` at `c8bd8aae4`: `subprocess.run(GATE_CHILD, capture_output=True, text=True, timeout=180)`, 9 cases | yes | Holds all of the daemon child's stdout and stderr for up to 180 s. At the timeout, CPython 3.14's POSIX `_communicate` joins the chunks so far into the `TimeoutExpired` (a second whole copy), while the chunk lists stay on the `Popen` object, which the exception's traceback holds; pytest keeps the exception as `sys.last_value` until the next test's call. Forced faults below. Fixed: `82bfd6a9e` sends the child's output to a file; `42aa3030c`'s `watch_output` kills it past 16 MiB. | Yes: a pipe read into memory until a timeout. If the child wrote at about 350 MB/s, 180 s gives 63 GB, and the join doubles it to about 127 GB. Zeros or a repeated log line compress as the incident's did. |
| 2 | `tests/unit/test_credit_lane_admission.py` at `c8bd8aae4`: `b"".join(iter(lambda: os.read(read, 65536), b""))` | yes | Reads a pipe to end of file with no bound, but its only writer, `Daemon._open_gate`, runs to completion on the same thread before the read and writes at most 4,101 bytes; a writer that never stopped would block once the pipe filled, a deadlock rather than growth. `os.pipe()` descriptors aren't inheritable, so no child holds the write end. Grows only with a second holder of the write end, which the code never had. Fixed anyway: `82bfd6a9e`'s `read_to_end` fails past 4,101 bytes. | The shape only with a writer the code never had. |
| 3 | `tests/unit/test_api_agent_units.py`: `subprocess.run(EXEC_BLOCK_CHILD, capture_output=True, timeout=120)` | yes | The child prints one JSON line (its exec block before and after an overwrite, each under `ARG_MAX`, 1 MiB) and exits; it has no loop that writes. A few MiB at most. | No |
| 4 | `tests/unit/test_guardian.py`: `guardian.run_guardian` in the worker itself | yes | `read_gate` and `read_frame` read 1 byte, a 4-byte length and at most 4,096 bytes into a buffer of that size; a longer length refuses before any read. The providers write to files, which the tests read whole, but each provider is a fixed one-line script. Forced fault below. | No |
| 5 | Hypothesis strategies with no size cap (`st.text()` twice in `test_api_agent_units.py`); the rest have `max_size` | yes | Hypothesis caps each example's choice sequence, so no strategy yields unbounded data. | No |
| 6 | `tests/fake/conftest.py`, `Harness.log_text` | no daemon in the command | Read each daemon log whole at `c8bd8aae4`; reads at most 8 MiB of each since `82bfd6a9e`. A file, read once, on a failure. | No |
| 7 | `tests/fake/conftest.py`, `Harness.request`: `makefile("rb").readline()` on the daemon's socket | no | `readline` has no size bound, so a daemon that sent bytes without a newline would grow it; each receive has a 5 s timeout. Unchanged by this build. | No; an open question below |
| 8 | `tests/fake/conftest.py`: `/bin/ps -p <pid> -o command=` with `capture_output` | no | One line. | No |
| 9 | `tests/fake/run_credit_daemon.py`, `dump_self_tests`: `communicate()` on the publish hook's self-test | no | Runs in the daemon child, not the worker; bounded by the self-test's output and its timeout. A growing daemon child is a process below the test, which the bound kills. | No: another process |
| 10 | `tests/process/test_api_agent_process.py`: `launch`, the not-a-pipe test and the SIGTERM test read the harness with `communicate()` or `capture_output` | no | a1's forced fault (`a20ca2d9e`'s message): a child writing zeros into that reader grew the test's process by about 400 MB/s. Fixed in `a20ca2d9e` (`run_to_files`, `watch_output`). | The shape, but not in the command |
| 11 | `tests/process/test_api_agent_process.py`: the sandboxed `RESOLVE` probe, `capture_output`, 120 s | no | The child prints one word. | No |
| 12 | `tests/process/test_credit_lane_differential.py`, `run_harness`: the harness's streams to files, `proc.wait(timeout=600)` with no size check, and on a wrong exit code `(adir / "stderr").read_text()[-2000:]` | no | A harness that wrote without end would fill the disk for 600 s, then load the whole file. Fixed in `b77f91e6e`; see below. | Not in memory until the read |
| 13 | `tests/process/test_credit_key_frame.py` | no | The chain's processes have their streams on /dev/null; `os.read(read_end, 1 + 4 + len(key) + 1)`; small result files. | No |
| 14 | `tests/fake/test_credit_key_containment.py`, `proc_blocks.py` | no | Exec blocks read with `sysctl` into an `ARG_MAX` buffer; each dump file is one block. The daemon child's output goes to its log. | No |
| 15 | `tests/memory_bound.py`, `_stacks` | — | faulthandler's dump of every thread, last 6,000 characters kept. | No |
| 16 | `tests/unit/test_memory_bound.py` | no | Children that grow on purpose, under the bound; reads each child pytest's output file whole, then keeps its last 20,000 bytes. Each child runs a fixed script. | No |
| 17 | `tools/credit_key_mutations.py`, `tools/footprint_watch.py` | no | The tool sends pytest's output to a file and reads its tail; the watcher reads no child's output. Tools, not tests. | No |

One more mechanism sits outside the diff: pytest's own capture sends whatever the test's
process writes to descriptors 1 and 2, and whatever a child that inherits them writes, to
a temporary file, which pytest reads back whole at the end of each phase (a3 read this in
the environment's pytest 9.1.1: `_pytest/capture.py` opens `TemporaryFile(buffering=0)`,
and each `snap` reads the file whole, then truncates it). In the command,
only code in the worker could write there (no test there lets a child inherit those
descriptors), and growth that way would fill the disk first, then memory in one read at
the phase's end, not memory steadily for six minutes. No r3 run tested it.

### The forced faults

a2 ran each reader from the 13:10Z command's files, as `c8bd8aae4` had it or as the head
has it, against a writer that never stops, under the bound (`-p tests.memory_bound`,
`SUBFLEET_TEST_MEMORY_MB=1024`, so the hard bound is 1,536 MB). The script and its
records are in `docs/reports/credit-lane-integration-data/` (`mem1_faults.py`,
`mem1-*.jsonl`); the host's load was about 20 to 38.

| Reader | Writer | What happened | What the bound did |
|---|---|---|---|
| #1, `c8bd8aae4`'s gate-test reader | a child writing zeros to stderr, 1 MiB a write | footprint 33 → 1,026 MB in 14.8 s, about 67 MB/s; RSS kept pace (1,037 MB) | failed the test at 1,026 MB, the stack in `selectors.select` under `subprocess.run` → `communicate` → `_communicate` |
| #1, the same | a child writing one traceback, 256 times a write, in a loop | started at 1,034 MB (the first case's memory still held), reached 1,286 MB in 8.9 s | the test started above the bound, so it failed at the midpoint to the hard bound, 1,286 MB |
| — | (the next test) | started at 1,288 MB | armed, as `42aa3030c` made it |
| #2, `c8bd8aae4`'s gate reader | a second holder of the write end, a thread writing zeros 64 KiB at a time | footprint 32 → 1,060 MB in 46 s while RSS stayed at 302 MB: the rest compressed, as in the incident; 862 MB written | failed the test at 1,060 MB |
| #2, the head's `read_to_end(fd, 4101)` | the same | `AssertionError: more than 4101 bytes on the launch gate` at once; footprint unchanged | not needed |
| #4, the guardian's `read_gate`, in process | after nothing: zeros without end | refused (`released=False`), in 0.05 s, no growth | not needed |
| #4, the same | after `1` and a length of 0xFFFFFFFF | refused before reading a key, no growth | not needed |
| #4, the same | after a 16-byte frame, zeros without end | released with exactly 16 bytes of key, no growth | not needed |

The rates here (67 MB/s for #1 with zeros; a1 measured about 400 MB/s for the same kind
of reader in `test_api_agent_process.py` at a lower load) bracket the incident's average
of 350 MB/s (127 GB in about six minutes).

### The cause: not proven

The strongest candidate is #1. It sat in the command, its reader held a child's two
streams in memory until a 180 s timeout and then doubled them, and if its child wrote at
the incident's average rate, the doubled size matches the size seen. But it needs a writer: something that wrote to the
daemon child's stdout or stderr without end. Guardians and providers start with their
standard streams on /dev/null (`procs.spawn_disclaimed`, and the `Popen` fallback in
`_spawn_guardian`), and `c8bd8aae4`'s daemon logs to its own `daemon.log` handler, so the
only writer to those pipes would be the daemon child's own Python, an uncaught exception
or a print in a loop. r3 found no such loop in `c8bd8aae4`'s code, and `82bfd6a9e`'s two
runs on `c8bd8aae4` grew nothing. If the uncommitted step 1 had one, no copy of it
survives.

What r3 ruled out, and how:

- The frame reader: bounded by construction (a buffer of the frame's size, at most 4,096
  bytes, a longer length refused before any read), and by the forced faults on
  `read_gate`. `key_frame.py` had the bound from its first version (13:04:31Z, per r2's
  brief).
- `c8bd8aae4`'s gate reader in the admission test: its only writer runs first on the same
  thread, so it holds at most 4,101 bytes; growth needs a second holder of the write end,
  which nothing had.
- The guardian and its provider as writers to the test's pipes: their standard streams
  are /dev/null.
- The exec-block child, the `ps` call and the `RESOLVE` probe: each prints a bounded
  amount once.
- Hypothesis: it caps each example's data.

The fix for the class stays the bound (C-20.7), with every reader above bounded: #1, #2,
#6, #10 and #12 at the head. The hub decides whether that suffices.

### The differential's reads (`b77f91e6e`)

Site #12 broke C-20.7's rule that a test kills a child whose output it sends to a file
once the file passes a stated size. `run_harness` now waits under `watch_output`, which
stops the harness once either file passes `OUTPUT_LIMIT` (16 MiB); it checks the files'
sizes while the no-end case waits for its tool to start; and the failure message reads a
bounded tail. `test_a_harness_that_writes_past_the_output_limit_is_stopped_at_once`
replaces the harness with a stand-in that writes 32 MiB and then waits: with the old
`proc.wait` put back, the test fails with `TimeoutExpired` after 20 s (it cuts the
timeout to 20 s); with the fix it passes in about 5 s.

### The memory bound

`tests/memory_bound.py` (`82bfd6a9e`, `42aa3030c`), installed for every test from
`tests/conftest.py`. Every 0.1 s a thread reads `ri_phys_footprint` (`proc_pid_rusage`,
what `top` shows as MEM, compressed pages included) for the test's process and every
process below it. Past `SUBFLEET_TEST_MEMORY_MB` (4,096 by default; 0 turns it off), the
running phase fails with every thread's stack; the plugin kills any process below the
test that passes it. A test that starts above the bound fails at the midpoint between
its starting footprint and the hard bound. Past the hard bound (the bound plus half of it, at least
512 MB more, at most 7 GB while the bound leaves room), the process writes its stacks to
`memory-bound-<pid>.txt` and kills itself. The forced faults above show it failing #1 and
#2 at the bound, with stacks, and the next test armed. No test in a2's or a3's runs
failed at the bound, and the folder a3 named in `SUBFLEET_TEST_MEMORY_LOG` for its suite
shards and its mutant runs stayed empty, so no process reached the hard bound either.

## Test commands and results

Every run: `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR=$TMPDIR/uv-cache`,
`uv sync --group dev --extra api`, Python 3.14.7, pytest 9.1.1, pytest-xdist 3.8.0, a new
`--basetemp` under TMPDIR, `-p no:cacheprovider`. Runs on 11/06 unless noted; "cut"
means the run hit its wall-clock deadline (below) and a2's driver stopped it whole.

| When (UTC) | What | Result |
|---|---|---|
| 02:20Z–02:23Z, 11/05 (a1) | step 1's 41 tests at `922d2ecbc`, `-n 4 --timeout=300` | 41 passed, 158 s |
| 02:25Z–02:31Z, 11/05 (a1) | the two invariant-6 tests on a `git archive` copy of `da2eb8b56`, serial | 2 failed, 351 s, as required |
| 03:02Z, 11/05 (a1) | `test_api_agent_process.py`, the differential and `test_launch_gates_above_stdio.py` at `a20ca2d9e` (its message) | 53 passed |
| 18:49Z–18:56Z | the forced faults, `SUBFLEET_TEST_MEMORY_MB=1024` | as in the table above |
| 19:00Z–19:02Z | the new output-limit test, with the fix and then with `proc.wait` put back | passed in 5 s; then failed with `TimeoutExpired` at 20 s |
| 19:01Z–19:03Z | `tests/process/test_credit_lane_differential.py`, `-n 4 --timeout=300` | 14 passed, 69 s |
| 19:08Z–19:11Z | the whole suite, `--collect-only` | 27,012 tests collected, no errors, 137 s |
| 19:12Z–19:21Z | shard 12 of 12, `-n 4 -p ci_shard_plugin --ci-shard 12/12 --timeout=540`, under the suite lock | cut at 530 s: 922 passed, 7 xfailed, none failed, about a third of the shard |
| 19:22Z–19:32Z | the process-level files this build touched (`test_api_agent_process.py`, the frame, the differential, `test_pipe_above_stdio.py`, containment, the gate test), `-n 4` | cut at 540 s: 21 passed, none failed |
| 19:33Z–19:39Z | step 1's containment, frame and resume tests at the head, `-n 4 --timeout=540` | 28 passed, 298 s |
| 19:41Z–19:50Z | the unit and fake files this build touched (13 files), `-n 4` | cut at 540 s: 773 passed, none failed, about nine tenths |
| 19:56Z–20:06Z | `test_memory_bound.py`, the gate test and the process-list tests, `-n 4` | cut at 541 s: 22 passed, none failed |
| 20:09Z–20:14Z | the process-list and mutant-named unit tests (14, the invariant-1 property's 150 examples among them), `-n 2`, at a load of 86 | 14 passed, 206 s |
| 05:21Z–05:29Z, 11/07 (a3) | the mutant tool's baseline on a copy of `9fc1834db`, serial, `--timeout=300` | 9 passed, 473 s |
| 05:29Z–05:45Z, 11/07 (a3) | the nine mutants, in four calls of the tool | each caught by its named tests (the mutants' table) |
| 05:04Z–06:59Z, 11/07 (a3) | the suite's shards 1, 2, 3, 4, 7 and 12 of 12, in resumable windows under the suite lock (the suite, below) | 7,934 tests: 7,855 passed, 57 xfailed, 18 skipped, 4 failed (load) |
| 06:07Z, 11/07 (a3) | shard 4's four failures again, `-n 4 --timeout=540`, at a load of 26 | 4 passed, 10 s |

## The suite

r2's rules ask for the whole suite once, in CI's 12 shards under the shared suite lock,
with the counts per shard. It didn't fit in r3: six of the twelve shards ran in full,
7,934 of the suite's 27,012 tests (a2's `--collect-only` count), and none of them
failed a test that a rerun didn't pass. CI on the PR decides the suite. The records,
per shard and per window, are in `credit-lane-integration-data/suite-a3.json`.

**a2 (11/06).** At a host load of 38 to 57 on 18 CPUs, shard 12 got through about a
third (922 passed, 7 xfailed, none failed) before a2's 530 s deadline stopped the run's
whole process group, which lost the rest of that run's place.

**a3 (11/07): windows that resume.** One shard doesn't fit in a 600 s tool call at this
host's load, so a3 ran each shard as a series of windows, each one pytest run holding
the suite lock (`/usr/bin/lockf -k -t 30` to `-t 90
/private/tmp/claude-501/subfleet-suites.lock`) for its whole run. A small plugin,
`suite_resume.py` (in the data folder, with its driver `run_shard.py`), made a cut window
lose only the tests it was running:

- the xdist controller appends each test's outcome to the shard's record as soon as the
  test's teardown report arrives, and a crashed worker's report at once;
- each window deselects every test already recorded;
- after a soft deadline (400 to 420 s) a worker starts no new test: it claims the test
  without running it and reports nothing, so the window ends by itself and the next
  window runs that test;
- at a hard deadline (450 to 510 s) the driver sends SIGINT to the run's process group,
  so pytest tears its fixtures down, and SIGKILL 30 s later if it is still running.
  The controller records no report that arrives after the hard deadline, so an
  interrupted test runs again rather than counting as a failure. After the two windows
  that ran into it, a3 found no process of the run left behind.

Each window: the worktree's `.venv/bin/python -m pytest -n 4 -q -p no:cacheprovider -p
ci_shard_plugin --ci-shard I/12 --timeout=540 -p suite_resume --basetemp=<new>
--ignore=tests/frontend/test_core_live.py`, with `PYTHONPATH` unset except for `tools/`
and the plugin's folder, and `SUBFLEET_TEST_MEMORY_LOG` set to a folder of a3's own,
which stayed empty: no test reached the hard memory bound.

| Shard | Tests | Passed | Failed | xfailed | Skipped | Windows (UTC, 11/07) | Load as each window started |
|---|---|---|---|---|---|---|---|
| 1/12 | 975 | 974 | 0 | 1 | 0 | 1: 06:45Z, after a smoke window at 05:05Z that started no test | 7 to 16 |
| 2/12 | 41 | 41 | 0 | 0 | 0 | 1: 06:08Z | 22 to 31 |
| 3/12 | 149 | 149 | 0 | 0 | 0 | 1: 06:54Z | 7 to 9 |
| 4/12 | 1,903 | 1,884 | 4 | 13 | 2 | 4: 05:08Z to 06:07Z | 111 in the first, then 31 to 44 |
| 7/12 | 2,224 | 2,197 | 0 | 11 | 16 | 1: 06:13Z | 17 to 22 |
| 12/12 | 2,642 | 2,610 | 0 | 32 | 0 | 2: 06:20Z, 06:35Z | 8 to 13 |
| 5, 6, 8, 9, 10, 11 | not run | | | | | | |
| Six shards | 7,934 | 7,855 | 4 | 57 | 18 | | |

Shards 4, 7 and 12 hold most of this build's test files: shard 4 the differential, the
harness and adapter units, the admission and enrollment tests and the stdio gate test;
shard 7 `test_api_agent_process.py`; shard 12 `test_claude_api_adapter.py`; shard 2
`test_memory_bound.py`; shard 1 the frame and containment tests. Every one of this
build's named tests in those files passed there. Between a3's windows other sessions
took the suite lock: from 06:28Z to 07:10Z, fifteen of a3's eighteen waits for it (30 to
120 s each) ran out, so the other six shards didn't run.

**The four failures (shard 4).** `tests/e2e/test_codex_turn_completed_e2e.py`'s three
cases of `test_zero_exit_cut_off_turn_cannot_publish_output` and
`tests/e2e/test_stop_bound.py::test_c5_8a_a_worker_that_never_returns_cannot_keep_the_lock`
each ran out a 10 s wait in the e2e harness (`E2E.until`: for the daemon to come up, for
the job to fail, or for a hook's marker file) in the first window, at a load of 106 to 111; they took 22 to
179 s each. This build changed neither file nor `tests/e2e/conftest.py`. At 06:07Z, at a
load of 26, a3 ran the four again (`-n 4 --timeout=540`): 4 passed in 10 s. They are the
host's load, not this build.

**What traces to the sandbox.** No failure. Skips and the one file left out:

- 15 person-only tests in `tests/e2e/test_conversations.py` (shard 7) skip whenever
  `SUBFLEET_ATTEMPT` or `SUBFLEET_JOB` is set (`IN_A_SUBFLEET_ATTEMPT`), and this lane
  sets both.
- `tests/process/test_guardian_qos_process.py::test_c5_1_inherit_runs_the_provider_at_the_guardians_qos`
  (shard 7) skipped on one of the file's environment checks (no `taskpolicy`, no process
  identity, `ps` denied, or a test process that is itself clamped); a3's records don't
  keep which.
- Two tests in `tests/unit/test_guard_subfleet_root.py` (shard 4) skipped; the file's
  check at line 1668 skips unless `SUBFLEET_CLAUDE_UNSCOPED_SEARCH` names the Claude
  hook's script, which this lane doesn't set.
- a3 left `tests/frontend/test_core_live.py` (shard 1) out with `--ignore`. An earlier
  session's note says that under a Subfleet lane the daemon refuses the test's client,
  whose ancestry carries the lane's attempt markers, and that the test then waits out the
  900 s timeout of its subprocess call (line 125). a3 didn't check the refusal itself,
  so the file's tests count neither as run nor as failed in shard 1's 975.

## Deviations

- **From the design:** none that r3 found beyond the hub's ruling of 11/04, which the
  design now carries (`3d9823d2f`: the interface's argv and `env_add`, "How the key
  arrives", invariants 1 and 6). Nothing can overwrite the key's Python strings in the daemon (step 1
  of the key's path), unlike its `bytearray` copies; they live for the
  `_launch` call.
- **From r2's rules, the suite.**
  - a2 and a3 ran pytest from the worktree's own `.venv` with pytest-xdist 3.8.0
    installed there (a2's `uv pip install`; the version `uv run --with
    'pytest-xdist>=3.6'` resolves), not through `uv run --with`. That kept uv's cache
    lock out of the suite lock's hold and let a deadline stop a run's whole process
    group.
  - "Once, in 12 shards" became windows that resume (the suite, above): each window held
    the suite lock for its own run only, so another session could take the lock between
    two windows of one shard. A shard's tests all ran at one commit, but not in one
    pytest process; a test that depends on state an earlier test of its shard left in the
    same worker would see a different neighbour than in CI.
  - a3 left out `tests/frontend/test_core_live.py`, which an earlier session found to
    fail under any Subfleet lane after a 900 s wait.
  - Six of the twelve shards ran.
  - CI's workflow now runs 5 shards per interpreter (`.github/workflows/tests.yml`);
    r3 kept r2's 12.
- **The mutants:** none. a3 ran the tool as its docstring says, on a copy of the head.

## Open questions

1. **D-CREDIT-MEM1's cause.** Not proven; "The cause: not proven" names the candidate and what r3 ruled out.
   Does the bound, with the bounded readers, close it?
2. **Other lanes' credentials** still ride in their guardians' exec blocks (the harness
   report's item 1). The hub's ruling covers credit lanes; a ruling for the rest is open.
3. **`Harness.request`'s `readline`** on the daemon's socket has no size bound (site #7).
   It predates this build, and C-20.7's rule reaches it; a follow-up could read at most a
   stated size.
4. **pytest's capture** reads a test's captured output whole when each phase ends (pytest
   9.1.1's `snap`, above). No r3 run tested whether the bound's window covers that read: a
   test whose child writes gigabytes to an inherited descriptor 1 or 2 would grow the
   test's process in one read, after the phase.
5. **The rest of the suite.** Shards 5, 6, 8, 9, 10 and 11 didn't run here; CI on the PR
   decides them. The shard count: r2's rules say 12; CI runs 5 per interpreter.
