# D-RELAY-EXIT1, #271 review r1

Started 2026-10-27 14:41:57 UTC. Base/head: `2f6e1dac714ecd5fcd7b6ff99234a64132fc4a6c`.
The assigned checkout is detached. Shared git metadata denies `index.lock`;
all five requested staging/commit steps were attempted and refused. No fetch,
merge, push, real provider, real Subfleet CLI, or sub-agent was used.

The reviewer file is copied unchanged to `tests/fake/test_turn_relay_exit_recovery.py`.
Its red run on the unchanged base produced **4 failed, 9 passed**, exactly the
three absent-receipt cases and the buffered-initialize later-write case.

P2 keeps the first proved exit timestamp on the runner, including exited status
on count mismatch and unwritten replay. `_process_gone()` remains true without
`exit.json`. Stdout drains to an empty file read and the driver result or EOF
settles it. Continuous output is checked after each chunk against `EXIT_DRAIN_S`
(**30 seconds** from first proof). This uses the default stop containment budget
as a generous drain allowance; no existing constant bounded this EOF loop.
At the deadline it requests containment, records `stdout-drain-timeout`, and
settles the output already read. A proved exit never sets `relay_failed`.

P3 permanently records `relay_io_ended` before handling every relay loss.
Outbox, replay, direct transmit, steering/handover, title writes and close frames
respect it. `RelayClient.close()` in final cleanup still closes the connection;
it does not transmit a frame. The ordinary unproved loss retains its failure
classification, stop and post-result cleanup clocks.

Property counterexamples clarified the intended scope without changing release
behavior: (1) an unproved refusal, then exit/result with no receipt, creates a
terminal outcome immediately but reports cleanup only after the existing
post-result containment clock; the property now bounds the durable terminal
outcome separately from its single eventual notification; (2) existing display
scrubbing trims whitespace-only text, so generated text has visible sentinels
while retaining internal whitespace and arbitrary UTF-8/JSON byte splits.
The initial frame-send stub returned `None`; it was corrected to a valid refusal
so the baseline failure identifies a transmitted frame directly.

One literal invariant in the brief conflicts with its mandatory reviewer
control: `test_an_unproved_exit_recovers_a_stalled_receipt_by_containment`
physically exits the child, hides reply/status proof, and requires
`relay_failed=True`. We preserve that control and classify from exit evidence
available at the loss, rather than an unknowable physical state. Properties
also generate an exited child whose exit evidence is unavailable. This is the
interpretation deviation; the production unproved-loss behavior stays intact.

The Hypothesis schedules cover refusal/exit ordering, chunks, EOF, a receipt
published after exit or never, with and without a provider result. Properties
check one durable terminal outcome, one notification, exact retained text and
stdout offsets, failure classification, the permanent write boundary, and both
sides of the drain deadline. Stdout is a regular file; an empty read is its EOF
boundary, while the deadline prevents a continuous writer holding the drain.

Commit order for the hub (every message must end with
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`):

1. `test: preserve relay exit recovery regressions` — reviewer test file.
2. `fix: retain exit evidence and bound stdout draining` — `02-p2.patch`.
3. `fix: end relay I/O independently of relay failure` — `03-p3.patch`.
4. `test: generate relay exit and stdout recovery interleavings` — property file.
5. `docs: specify bounded relay exit recovery and permanent I/O end` — contract
   and this report. P2/P3 patches permit staging the two runner steps separately
   even though the working file contains both.

The reviewer red hold ran before production edits. The property red hold used
an exact saved copy of the base runner loaded in memory because it waited for
the shared lock while implementation proceeded; it produced 4 failed cases.
A corrected frame-stub baseline hold is recorded separately below.

The ten named in-memory mutants were killed (10 passing expected-failure
checks), using the original named test functions. The reproducible harness is
`named_mutants.py`; production files were never edited for mutation runs.

| Named mutant | Killing test (one concrete parameter case) |
| --- | --- |
| Exit bypass removed | `test_a_settings_refusal_after_the_child_exits_keeps_its_end_reason` (`failed`, `reply`) |
| Exit bypass unconditional | `test_a_settings_refusal_while_the_child_lives_is_a_relay_failure` (`failed`, status available) |
| Ignore exit argument | `test_a_settings_refusal_after_the_child_exits_keeps_its_end_reason` (`failed`, `reply`) |
| Broken pipe alone proves exit | `test_a_settings_refusal_while_the_child_lives_is_a_relay_failure` (`failed`, status available) |
| Replay always passes false | `test_replay_exit_status_prevents_a_relay_stop_before_receipt` |
| Early return keeps `relay_failed` | `test_a_settings_refusal_after_the_child_exits_keeps_its_end_reason` (`failed`, `reply`) |
| Proved exit evidence dropped (P2 reverted) | `test_proved_exit_without_a_receipt_reaches_eof[reply]` |
| EOF drain bound removed | `test_proved_exit_drain_is_bounded_on_the_fake_clock[at-bound]` (`step=1`) |
| Replay I/O guards removed (P3 reverted) | `test_exit_refusal_does_not_permit_a_later_frame` |
| `relay_failed` set on proved exit | `test_a_settings_refusal_after_the_child_exits_keeps_its_end_reason` (`failed`, `reply`) |

The replay mutant changes both the outbox and direct-transmit guards back to
`relay_failed` on that path; removing only one is masked by the other guard.
The last mutant intentionally repeats review r1's early-return flag mutation.


## Executed results

Latest unique completed results: **1983 passed, 0 failed, 44 skipped**. **39 cases remain unrun**, all listed in `unrun-nodes.txt`. The test controllers stopped at their configured checkpoint; final reporting ran later than the three-hour checkpoint. No further tests were started after it.

| Hold | P/F/S/E | Seconds |
| --- | ---: | ---: |
| red-review | 9/4/0/0 | 47.63 |
| red-properties | 0/4/0/0 | 169.13 |
| regression | 26/1/0/0 | 132.2 |
| core1 | 129/0/0/0 | 556.91 |
| regression-final | 26/1/0/0 | 107.55 |
| regression-green | 27/0/0/0 | 230.2 |
| steer-service | 142/0/0/0 | 567.11 |
| guards | 63/0/0/0 | 100.54 |
| mutants | 10/0/0/0 | 109.61 |
| steer-service-resume | 22/0/0/0 | 179.86 |
| core2-direct * | 280/0/0/0 | 568.61 |
| core1-overlap-direct * | 5/0/0/0 | 564.7 |
| steer-properties | 225/0/0/0 | 556.52 |
| core2-resume-direct * | 6/0/0/0 | 54.75 |
| overlap-resume-direct * | 1/0/0/0 | 392.52 |
| red-properties-frame-direct * | 0/1/0/0 | 34.82 |
| catalog | 34/0/0/0 | 323.09 |
| steer-properties-resume | 16/0/0/0 | 133.04 |
| e2e | 0/0/44/0 | 37.84 |
| overlap-final-direct * | 4/0/0/0 | 85.58 |
| steer-loop-direct * | 7/0/0/0 | 177.06 |
| bulk-fakes-handoff | 22/0/0/0 | 520.9 |
| regression-confirm-direct * | 30/0/0/0 | 316.7 |
| bulk-service-direct * | 14/0/0/0 | 342.34 |
| probe-property | 1/0/0/0 | 235.81 |
| bulk-conversations | 285/0/0/0 | 229.61 |
| regression-guards-direct * | 60/0/0/0 | 63.98 |
| service-parts-1-direct * | 80/0/0/0 | 196.24 |
| service-parts-2-direct * | 80/0/0/0 | 120.68 |
| service-parts-3-direct * | 80/0/0/0 | 4.62 |
| service-parts-4-direct * | 23/0/0/0 | 47.24 |
| fake-handoff-parts-1-direct * | 19/0/0/0 | 305.42 |
| catalog-parts-1-direct * | 36/1/0/0 | 305.56 |
| fake-handoff-parts-2-direct * | 80/0/0/0 | 274.57 |
| fake-handoff-parts-3-direct * | 80/0/0/0 | 193.93 |
| catalog-parts-2-direct * | 62/0/0/0 | 85.54 |
| fake-handoff-parts-4-direct * | 80/0/0/0 | 31.98 |
| fake-handoff-parts-5-direct * | 80/0/0/0 | 109.62 |

`*` denotes a direct fallback after the corresponding original selection waited over 30 minutes. Parts retain that original selection and exclude completed nodes. The fake/handoff original selection first entered the lock after 33 minutes; its subsequent parts used the same fallback. The longest recorded pytest execution was 568.61 seconds; lock waits and temporary-root cleanup are separate from that counter.

The two draft regression holds each had 26 passes and one property-model failure: terminal outcome versus cleanup timing, then existing display whitespace trimming. Both were corrected without production changes. The final feature-and-guard confirmation had 30 + 60 passes, covering 27 feature cases and all 63 guards on the final code/contract.

The initial provider hold was interrupted during fixture setup and pytest raised a temporary-path stash `KeyError` on teardown; its 280 completed tests passed, and all six remaining nodes passed in the continuation.

One FIFO case (`test_an_op_meeting_a_fifo_in_the_state_root_answers_and_close_returns[conversation.open, a message's text]`) exceeded its existing 60-second child timeout after printing `submitted`, `closed`, and a successful result. It passed in the next catalog part without a code change. The failing hold ended at load (147.24, 87.64, 74.36); the continuation started at (161.90, 137.30, 100.29). Its cause was not established.

All 44 e2e cases skipped at the required ps/sysctl process-identity prerequisite (macOS boot identity unavailable). No e2e handoff executions occurred. The unchanged probe clock-tolerance property passed; its source was not edited. Full-suite and frontend live execution were not attempted.

Artifact checks: reviewer test copy is byte-identical (SHA-256 `f106673f703c17bd331705c4e0a18cb2822a8fe1cc61f6b60ec33f05465f7e42`); AST and trailing-whitespace checks passed; `git diff --check` passed; head remains `2f6e1dac714ecd5fcd7b6ff99234a64132fc4a6c`. Assigned branch: `fix/relay-exit-race-217`; the workspace arrived detached.

Logs omit repeated sandbox sysctl warnings. `results.json` preserves each hold and per-file counts; `hold-selections.json` names the broad selections. `named_mutants.py` reproduces the ten named mutations with `python -m pytest -q -p tests.conftest docs/reports/2026-10-27-relay-exit-r1/named_mutants.py` under the required environment/hold wrapper.

All owned test sessions ended before final cleanup. Scratch measured 261 MB before intermediate pruning and 60 MB after completed-root pruning, below the 1 GB budget. Guarded final cleanup removes the entire named scratch folder. No bytecode, `.hypothesis`, or build output is retained in the worktree.
