# D-SHUT1: detached Codex turns must complete

## Evidence and rule

The supplied live-store audit reports that the 2026-10-12 11:38:52Z reboot
ended 40 running Codex attempts, all with rc 0 and no signal. Six had both
`turn.completed` and a non-empty `last.md`; the other 34 had neither and
were nevertheless accepted from 87–408-byte interim progress notes. The
daemon sent those processes no signal, so `killed_by` stayed null and C-9.2's
rule that a signalled attempt is never `ok` could not apply.

Correction (hub, from the attempts table): the reboot's 34 attempts are the
only cut-off turns accepted as a job's result. Each has `killed_by` null;
earlier signalled attempts were not accepted. Among 4,830 genuine `ok`
attempts, every one had both `turn.completed` and a non-empty `last.md`, so
requiring `turn.completed` rejects none of them.
This report uses the supplied evidence; no live store or live process was read.

`CodexAdapter.classify()` now requires this attempt's `turn.completed` event,
rc 0, no `turn.failed`, and a non-empty deliverable for `ok`, with the existing
spawn and authentication checks. An unfinished rc-0 turn still passes through
authentication, CLI age, content-filter and quota classification. Otherwise,
it is `transient`, detail **Codex exited before its turn completed**, for
C-4.5's retry. Terminal `turn.failed` classifications remain unchanged.
Evidence records `turn_completed` and `last_event_type`; `why` shows the
latest attempt's recorded cut-off reason and event, and notices keep the reason.

`last.md` alone proves nothing: the 2026-09-24 incident wrote an interim note
there. Deliverable extraction still retains attempt-local text as evidence.
The daemon rechecks completion from this attempt's frozen stream whenever an
adapter verdict is `ok`, including cached `finalization.json`, legacy receipts,
and restart recovery. An incomplete cached `ok` becomes `transient`; the
original verdict remains `provider_verdict` evidence, as for C-9.2's signal
override. Claimed completion fields in a cached verdict cannot replace the
stream's proof. `_export_locked()` still requires `accepted_attempt_id` and
exports only that attempt's artifact.

`runs show` renders the accepted result separately from unaccepted artifact
paths. With no accepted attempt, `runs show --out` emits no stdout, reports
“no accepted deliverable recorded” on stderr, and returns 1. The ordinary
form names unaccepted evidence without printing its contents, and returns 1
for a failed, cancelled or lost job. JSON metadata and `--err` remain available.
It never substitutes an earlier attempt or an existing `-o` file for a missing
accepted artifact.

Recovery preserves `next_check_at` for capacity waits whose latest attempt is
transient, including existing waiting rows; ordinary capacity and route waits
still receive their first-pass recheck. The latest-attempt test deliberately
retains future capacity deadlines after a transient even if admission has
already looked again: recovery cannot prove that such a deadline is safe to
shorten. Admission reserves the retry when the saved deadline is due.

Fresh and `codex exec resume` launches use the same classifier and their own
attempt stream. `model_answered()` agrees on the event spelling: positive
usage in `turn.completed` proves the model answered, while completion itself
does not require usage counters. An interim model item can prove answering
without proving completion. Conversation turns in `codex_turn.py` are
unchanged: their driver handles app-server `turn/completed`, plus the existing
`thread/status/changed: idle` settlement after a grace period for tool turns.
They use the recorded driver outcome rather than this detached classifier.

## Invariants and coverage

- **I1:** adapter `ok` implies the attempt stream contains `turn.completed`,
  rc is 0, no `turn.failed` was read, and a deliverable exists. A detached
  daemon `succeeded` implies its own stream contains `turn.completed` through
  every finalization path, including cached and legacy verdicts.
- **I2:** adding `turn.completed` to a stream lacking it never removes `ok`;
  removing completion from an `ok` stream always disqualifies it.
- **I3:** authentication > admission > quota precedence is unchanged when
  `turn.completed` is present.

`tests/unit/test_codex_turn_completed.py` generates event sequences containing
`thread.started`, `turn.started`, agent-message/command-execution
`item.started` and `item.completed`, `turn.completed`, `turn.failed`, and
`error`. Exit receipts range over rc {0, 1, 143, 137}; `last.md` is absent,
empty, interim or final. I1 and I2 each configure 500 Hypothesis examples and
one explicit regression example. I3 has eight completed-stream combinations
and eight incomplete-stream combinations. The fake module exhausts all 48
combinations of fresh classification, cached replay, legacy receipt and restart
recovery; present/absent completion; rc 0/nonzero; and absent/interim/final
`last.md`. Every case checks acceptance and export, with cached classification
forbidden from running again. Restart tests advance a controlled clock through
the middle and end of a 60-second retry backoff, for current and legacy rows.

Examples cover the unchanged success fixture, three synthetic rc-0 cut-off
fixtures without `last.md`, CA/DNS/write-stdin errors, interim and final
last-message files without completion, terminal failure, and native resume.
`git grep -n 'output-last-message\|last.md' -- tests/` identified the detached
fixture runner in `tests/bin/codex`. Its success fixture already emits
`turn.completed`; stream-derived `last.md` now requires that event, preserving
cut-off fixture shape. The deliberately incomplete SIGTERM fake stays
incomplete. `FakeAdapter` keeps its own classifier; its successful provider
and state receipts now carry an explicit `turn.completed` stream event so they also
satisfy the daemon's acceptance rule. Real Codex fixtures retain their own
stream evidence. App-server fakes are unchanged.

## Validation

Fix round 1 completed **22 foreground acquisitions: 21 denied before pytest,
1 acquired**. Each request used its own shared lock with a 60-second queue
timeout. The acquired mutation hold finished within 20 minutes: pytest took
123.00 seconds; the entire acquisition, run and cleanup took 294.88 seconds.
Every requested hold was bounded to 1,100 seconds plus at most 10 seconds of
interruption grace. Each attempt used a fresh `TMPDIR`, cleaned with the
requested `shutil.rmtree` command. All test and mutation runners ended.

**Ordinary pytest counts: 0 executed, 0 passed, 0 failed.** All required files
were requested separately; the shared lock refused each before pytest started.
The main Codex unit, adapter and fake files were requested again after a
mutation acquired the lock; those repeats were denied too. The unchanged
CLI control (11 selected accepted-result and ownership cases) was also denied.
The hub must run these ordinary checks:

| File | Passed | Failed | Status |
| --- | ---: | ---: | --- |
| `tests/unit/test_codex_turn_completed.py` | 0 | 0 | Unrun: lock denied |
| `tests/unit/test_codex_adapter.py` | 0 | 0 | Unrun: lock denied |
| `tests/fake/test_codex_turn_completed_fake.py` | 0 | 0 | Unrun: lock denied |
| `tests/e2e/test_codex_turn_completed_e2e.py` | 0 | 0 | Unrun: lock denied |
| `tests/e2e/test_notice_job_state.py` | 0 | 0 | Unrun: lock denied |
| `tests/e2e/test_recovery.py` | 0 | 0 | Unrun: lock denied |
| `tests/unit/test_test_module_names.py` | 0 | 0 | Unrun: lock denied |
| `tests/unit/test_cli.py` | 0 | 0 | Unrun: lock denied |
| `tests/fake/test_state_contract.py` | 0 | 0 | Unrun: lock denied |
| `tests/fake/test_finalization_replay.py` | 0 | 0 | Unrun: lock denied |

I1 and I2 still configure 500 generated examples each; **generated examples
executed this round: 0**. The exhaustive daemon matrix has 48 cases; all remain
unrun. The required e2e caller, notice-state and recovery checks remain unrun.

Process-local mutation attempts never edited product files. Removing the
accepted-artifact ownership check produced **4 failed, 4 passed, 178 deselected**
in `tests/unit/test_cli.py`. All four failures were
`test_show_out_never_falls_back_to_unaccepted_artifacts_or_export` cases with
an accepted a2 and an unowned or a1 artifact; they printed interim text and
returned 0. This is an observed mutation failure, **not a confirmed mutation
kill**, because the unchanged control was denied before it could run. The
other seven mutation requests were denied. Expected detectors are identified
below as predictions wherever no test ran:

| Mutation | Detector | Executed result |
| --- | --- | --- |
| Drop adapter completion check | `test_last_message_file_alone_does_not_prove_completion` | Unrun: lock denied |
| Drop daemon completion override | `test_i1_succeeded_requires_completion_on_every_finalization_path[interim-0-False-cached]` | Unrun: lock denied |
| Drop recovery retry deadline protection | `test_restart_in_transient_backoff_preserves_deadline` | Unrun: lock denied |
| Drop CLI accepted-attempt check | `test_show_out_never_falls_back_to_unaccepted_artifacts_or_export` (unowned artifact, no accepted attempt) | Unrun: lock denied |
| Drop CLI artifact ownership check | Same regression, accepted a2 with unowned/a1 artifact | 4 observed failures; unchanged control unrun |
| Accept `last.md` as completion (review mutation) | `test_last_message_file_alone_does_not_prove_completion` | Unrun: lock denied |
| Drop completion evidence fields (review mutation) | `test_cut_off_fixtures_retry_with_completion_evidence` | Unrun: lock denied |
| Classify cut-off fallback as `unknown` (review mutation) | `test_cut_off_fixtures_retry_with_completion_evidence` | Unrun: lock denied |

The reviewer helpers were read from
`/Users/maxghenis/.subfleet/worktrees/20261012-083810-pr195-review-r1/tests/fake/pr195_review_inputs.py`.
Their legacy acceptance observation is now a desired-behavior regression in
the exhaustive finalization matrix. The corrected `runs show` text assertions
are ported with the actual fixture-derived interim text; the restart assertion
now expects the original deadline to survive. The large-stream input is ported
to the unit module. The review's observed unsafe behavior is not kept as a test
expectation.

All ten changed Python files parse, and the committed Python versions compile. All eight process-local mutation
transformations compile without editing product files; compilation does not
establish that a test kills a mutation. `git diff --check` passes.

## Contract and delivery

C-9.2 records the daemon completion check and retained provider verdict.
C-12.6 records the accepted-result rendering and `--out` behavior. C-6.10 now
distinguishes the durable transient retry deadline from ordinary capacity
waits brought forward by recovery.

This change is independent of PR #119. Per the supplied brief, #119 handles
shutdown receipts with rc 143/137 or signal 15/9; D-SHUT1 handles the rc-0,
signal-free receipts it misses and transport errors with the same incomplete
turn shape. The two meet at transient classification and C-4.5 retry; no
#119 changes were imported. Previously finalized false successes are not
retroactively reclassified.

Fix-round commits are on `fix/codex-turn-completed` in workspace-local
`.git-local` metadata, based on assigned head `488e19685db3`. The shared Git
index is outside the writable workspace, and the branch is held by the caller's
checkout. The caller's checkout and its branch were left untouched; no history
was rewritten and nothing was pushed. All commits use the required co-author
trailer. Only named source, test and documentation files were staged; validation
JSON and temporary mutation helpers were not staged.

Code commits in this round:

- `81d527812`: completion proof at daemon acceptance, cached/legacy/recovery
  matrix, large-stream input, and explicit completion in generic fake receipts.
- `95562749e`: accepted-result rendering and CLI/fake/e2e caller assertions.
- `642aa40da`: durable retry deadlines and restart/ordinary-wait regressions.
